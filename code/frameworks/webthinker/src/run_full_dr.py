"""Run the upstream WebThinker report sequence; only I/O boundaries are adapted."""
from pathlib import Path
import asyncio,hashlib,importlib.util,json,os,sys,time,traceback,urllib.parse,urllib.request
sys.dont_write_bytecode=True
CODE=Path(__file__).resolve().parents[4]
ROOT=Path(os.environ['FORGE_FRAMEWORK_DIR'])
sys.path.insert(0,str(ROOT.parent/'shared'/'src'))
from defense_policies import MODE as DEFENSE_MODE,anchor_recursive_prompt
REPO=CODE/'vendor/webthinker'
PREVIOUS=CODE/'assets/webthinker'
sys.path[:0]=[str(REPO/'scripts'),str(REPO/'scripts/utils')]
os.environ['HF_HUB_OFFLINE']='1';os.environ['NLTK_DATA']=str(CODE/'assets/webthinker/nltk_data')
os.environ['HF_HOME']=str(ROOT/'cache/huggingface')
os.environ['TRANSFORMERS_CACHE']=str(ROOT/'cache/huggingface/transformers')
Path(os.environ['TRANSFORMERS_CACHE']).mkdir(parents=True,exist_ok=True)
TASK_FILE=ROOT.parent/'perplexica/task.json'
TASK=json.loads(TASK_FILE.read_text(encoding='utf-8'))
QUESTION=TASK.get('question','').strip()
if not QUESTION:raise RuntimeError(f'No question configured in {TASK_FILE}')
CONFIG=json.loads((ROOT/'config/run.json').read_text(encoding='utf-8'))
WIKI_BASE=os.environ.get('FORGE_WIKI_BASE','http://127.0.0.1:8790').rstrip('/')
ADAPTER_BASE=os.environ.get('FORGE_WEBTHINKER_ADAPTER_BASE','http://127.0.0.1:8789').rstrip('/')
CONFIG['wiki_bing_endpoint']=WIKI_BASE+'/webthinker/bing/search'
CONFIG['wiki_page_endpoint']=WIKI_BASE+'/webthinker/page'
ENVIRONMENT_FILE=ROOT.parent/'shared/data/environment_manifest.json'
ENVIRONMENT=json.loads(ENVIRONMENT_FILE.read_text(encoding='utf-8'))
ENVIRONMENT_HASH=hashlib.sha256(ENVIRONMENT_FILE.read_bytes()).hexdigest()
VENDOR=REPO/'scripts/run_web_thinker_report.py'
VENDOR_HASH=hashlib.sha256(VENDOR.read_bytes()).hexdigest()
if '--check' in sys.argv:os.environ['FORGE_CHECK_ARGS']='--check'
sys.argv=[str(VENDOR),'--single_question',QUESTION,
    '--api_base_url',ADAPTER_BASE+'/v1','--aux_api_base_url',ADAPTER_BASE+'/v1',
    '--model_name',CONFIG['primary_model'],'--aux_model_name',CONFIG['auxiliary_model'],
    '--tokenizer_path',str(PREVIOUS/'tokenizers/QwQ-32B'),
    '--aux_tokenizer_path',str(PREVIOUS/'tokenizers/Qwen2.5-32B-Instruct'),
    '--search_engine','bing','--bing_endpoint',CONFIG['wiki_bing_endpoint'],
    '--bing_subscription_key','local-wiki']
# All research budgets and sampling parameters stay at the vendor defaults.
spec=importlib.util.spec_from_file_location('webthinker_native_report',VENDOR)
native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
native_args=vars(native.args)
(ROOT/'config/native_args.json').write_text(json.dumps(native_args,ensure_ascii=False,indent=2),encoding='utf-8')
original_fetch=native.fetch_page_content_async

async def local_page_fetch(urls,*args,**kwargs):
    """Preserve the official HTML reader, redirect only the fetched page location."""
    mapped={}
    for url in urls:
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in ['http','https'] or parsed.hostname!='en.wikipedia.org':
            raise RuntimeError(f'URL is outside the fixed Wikipedia sandbox: {url}')
        mapped[url]=CONFIG['wiki_page_endpoint']+'?'+urllib.parse.urlencode({'url':url})
    fetched=await original_fetch(list(mapped.values()),*args,**kwargs)
    with (ROOT/'results/page_transport.jsonl').open('a',encoding='utf-8') as out:
        for original,local in mapped.items():out.write(json.dumps({'unix':time.time(),'original_url':original,'local_url':local,'content_chars':len(fetched.get(local,''))})+'\n')
    return {original:fetched.get(local,'') for original,local in mapped.items()}

native.fetch_page_content_async=local_page_fetch

# Main-experiment wrapper budget from Table 22.
original_generate=native.generate_response
EDIT_END_MARKER=native.END_EDIT_ARTICLE
edit_state={'executed':0,'root_anchor_completion_prompts':0}
edit_cap=8

def anchor_completion_chatml(prompt):
    """Wrap the actual native action context, preserving ChatML framing."""
    marker = '<|im_start|>user\n'
    start = prompt.rfind(marker)
    if start < 0:
        raise ValueError('RQA completion has no native user action context')
    start += len(marker)
    end = prompt.find('<|im_end|>', start)
    if end < 0:
        raise ValueError('RQA completion has an unclosed user context')
    current = prompt[start:end]
    if current.startswith('ROOT USER QUERY (keep the research directed at this objective):'):
        return prompt, False
    return prompt[:start] + anchor_recursive_prompt(QUESTION, current, '') + prompt[end:], True

async def capped_generate(*args,**kwargs):
    if DEFENSE_MODE=='root_query_anchoring':
        stop_values=kwargs.get('stop') or []
        if isinstance(stop_values,str):stop_values=[stop_values]
        if native.END_SEARCH_QUERY in stop_values and isinstance(kwargs.get('prompt'),str):
            kwargs=dict(kwargs)
            if kwargs.get('generate_mode','chat')=='completion':
                kwargs['prompt'],added_anchor=anchor_completion_chatml(kwargs['prompt'])
                if added_anchor:edit_state['root_anchor_completion_prompts']+=1
            else:
                kwargs['prompt']=anchor_recursive_prompt(
                    QUESTION,kwargs['prompt'],
                    'Generate the next search query/action while keeping it materially relevant to the root research objective.'
                )
    formatted_prompt,response=await original_generate(*args,**kwargs)
    stop=kwargs.get('stop') or []
    if EDIT_END_MARKER in stop and response.rstrip().endswith(EDIT_END_MARKER):
        if edit_state['executed']<edit_cap:
            edit_state['executed']+=1
        else:
            stripped=response.rstrip()[:-len(EDIT_END_MARKER)]
            notice=('\n[Wrapper notice] The article edit budget is exhausted. Do not request further edits. '
                    'Proceed to check the article and finish your work now.')
            return formatted_prompt,stripped+notice
    return formatted_prompt,response
native.generate_response=capped_generate

def only_local(event,args):
    if event=='socket.connect':
        host=args[1][0]
        if host not in ['127.0.0.1','::1','localhost']:
            raise RuntimeError(f'Nonlocal research connection blocked: {host}')
sys.addaudithook(only_local)

def save_status(status,**extra):
    (ROOT/'results/live_status.json').write_text(json.dumps({'status':status,'unix':time.time(),**extra},ensure_ascii=False,indent=2),encoding='utf-8')

async def main():
    result_path=ROOT/'results/result.json'
    if result_path.exists():raise RuntimeError('Refusing to overwrite an existing run result')
    sequence={'item':{'Question':QUESTION},'prompt':'','output':'','finished':False,'history':[],
              'search_count':0,'executed_search_queries':set()}
    searches={};pages={};records=[]
    adapter_before=json.loads(urllib.request.urlopen(ADAPTER_BASE+'/status',timeout=5).read())
    started=time.time();start=time.perf_counter()
    result={'framework':'WebThinker','paper_reference_revision':'db387eb','source_revision_verified':False,
        'completed':False,'started_unix':started,'question':QUESTION,'corpus_articles':ENVIRONMENT['articles'],
        'question_source':str(TASK_FILE),'question_query_id':TASK.get('query_id'),
        'cache_initialization':{'searches':0,'pages':0,'explorer_history':0,'disk_cache_loaded':False},
        'primary_model':CONFIG['primary_model'],'auxiliary_model':CONFIG['auxiliary_model'],
        'defense_mode':DEFENSE_MODE,
        'primary_upstream_model':CONFIG.get('primary_upstream_model',CONFIG['primary_model']),
        'adapter_request_start_exclusive':adapter_before['requests'],
        'frozen_environment':ENVIRONMENT,'frozen_environment_sha256':ENVIRONMENT_HASH,
        'native_main_script_sha256_before':VENDOR_HASH,'native_loop_budgets_retained':True,
        'upstream_auxiliary_output_cap':json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8')).get('max_output_tokens_by_model',{}).get(CONFIG['auxiliary_model']),
        'final_report_upstream_model':json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8')).get('final_report_model',CONFIG['auxiliary_model']),
        'final_report_output_cap':json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8')).get('final_report_max_output_tokens'),
        'upstream_primary_output_cap':json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8')).get('max_output_tokens_by_model',{}).get(CONFIG['primary_model']),
        'page_transport_boundary':'Redirect original Wikipedia page URLs to fixed local snapshot; preserve native fetch/parser functions and original result dictionary keys',
        'model_boundary_adapter':json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8'))}
    save_status('planning_and_research',started_unix=started)
    try:
        async with native.AsyncOpenAI(api_key='memory-proxy',base_url=ADAPTER_BASE+'/v1') as client:
            sequence=await native.process_single_sequence(sequence,client,client,asyncio.Semaphore(native.args.concurrent_limit),native.args,searches,pages,records)
        article=sequence.get('article','')
        (ROOT/'results/report.md').write_text(article,encoding='utf-8')
        has_researched_article=bool(article.strip()) and sequence['search_count']>0
        declared_done='I have finished my work.' in sequence.get('output','')
        result.update(completed=has_researched_article and sequence.get('finished',False) and declared_done,
            report_words=len(article.split()),has_researched_article=has_researched_article,coverage_audit_required=True,
            primary_search_count=sequence['search_count'],all_unique_search_queries=len(searches),
            fetched_page_count=len(pages),web_explorer_count=len(sequence.get('web_explorer',[])),
            native_done_phrase_present=declared_done,
            completion_rule=('declared_phrase' if declared_done else 'none'),
            wrapper_edit_article_cap=edit_cap,wrapper_edit_article_executed=edit_state['executed'],
            root_anchor_completion_prompts=edit_state['root_anchor_completion_prompts'],
            root_anchor_completion_strategy=('wrap_native_user_action_context' if DEFENSE_MODE=='root_query_anchoring' else None),
            all_unique_search_query_text=list(searches))
        if not result['completed']:result['error']='Native sequence did not produce a valid completed report; inspect termination, completion rule and coverage audit'
    except Exception:
        result['error']=traceback.format_exc()
    finally:
        result['elapsed_seconds']=time.perf_counter()-start;result['finished_unix']=time.time()
        try:result['adapter_request_end_inclusive']=json.loads(urllib.request.urlopen(ADAPTER_BASE+'/status',timeout=5).read())['requests']
        except Exception:pass
        result['native_main_script_sha256_after']=hashlib.sha256(VENDOR.read_bytes()).hexdigest()
        result['native_main_script_unchanged']=result['native_main_script_sha256_after']==VENDOR_HASH
        result['frozen_environment_unchanged']=hashlib.sha256(ENVIRONMENT_FILE.read_bytes()).hexdigest()==ENVIRONMENT_HASH
        for name,data in [('sequence.json',sequence),('search_cache.json',searches),('page_cache.json',pages)]:
            (ROOT/'results'/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=list),encoding='utf-8')
        result_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        save_status('completed' if result['completed'] else 'failed',elapsed_seconds=result['elapsed_seconds'],error=result.get('error'))
        print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':
    if '--check' in __import__('os').environ.get('FORGE_CHECK_ARGS',''):
        print('Native WebThinker imports/tokenizers loaded; no inference run')
    else:
        asyncio.run(main())
