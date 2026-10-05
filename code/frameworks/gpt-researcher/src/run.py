"""Native deep report; only HTTP evidence transport is replaced by the Wiki sandbox."""
from pathlib import Path
import asyncio,hashlib,ipaddress,json,os,sys,threading,time,traceback,urllib.parse,urllib.request
CODE=Path(__file__).resolve().parents[4];ROOT=Path(os.environ['FORGE_FRAMEWORK_DIR']);SHARED=ROOT.parent/'shared'
REPO=CODE/'vendor/gpt-researcher'
OUT=ROOT/'results';OUT.mkdir(parents=True,exist_ok=True)
MODEL_GATEWAY_BASE=os.environ.get('FORGE_MODEL_GATEWAY_BASE','http://127.0.0.1:8788').rstrip('/')
WIKI_BASE=os.environ.get('FORGE_WIKI_BASE','http://127.0.0.1:8790').rstrip('/')
os.environ.update(OPENAI_API_KEY='memory-proxy',OPENAI_BASE_URL=MODEL_GATEWAY_BASE+'/gpt-researcher/v1',TAVILY_API_KEY='sandbox-only',TIKTOKEN_CACHE_DIR=str(CODE/'assets/tiktoken'),NO_PROXY='127.0.0.1,localhost',TOKENIZERS_PARALLELISM='false')
sys.path.insert(0,str(SHARED/'src'))
from defense_policies import MODE as DEFENSE_MODE,anchor_recursive_prompt
sys.path.insert(0,str(REPO))
from gpt_researcher.config.variables.default import DEFAULT_CONFIG
MODEL=os.environ['DR_MODEL']
AUX_MODEL=os.environ['DR_AUX_MODEL']
if AUX_MODEL!=MODEL:raise ValueError('Primary and auxiliary models must share the paper backbone')
DEFAULT_CONFIG.update(
    FAST_LLM='openai:'+AUX_MODEL,
    SMART_LLM='openai:'+MODEL,
    STRATEGIC_LLM='openai:'+MODEL,
    EMBEDDING='openai:local-bge-small-en-v1.5',
    EMBEDDING_KWARGS={'check_embedding_ctx_length':False},
)
# Use the repository defaults, not settings inherited from earlier experiments.
for k in DEFAULT_CONFIG:os.environ.pop(k,None)
from gpt_researcher import GPTResearcher
from gpt_researcher.llm_provider.generic.base import GenericLLMProvider

async def _complete_report_stream(self,messages,websocket=None,**kwargs):
    paragraph='';response='';finish_reason=None
    async for chunk in self.llm.astream(messages,**kwargs):
        reason=(getattr(chunk,'response_metadata',None) or {}).get('finish_reason')
        if reason:finish_reason=reason
        content=chunk.content
        if content is not None:
            response+=content;paragraph+=content
            if '\n' in paragraph:
                await self._send_output(paragraph,websocket);paragraph=''
    if paragraph:await self._send_output(paragraph,websocket)
    if finish_reason!='stop':raise RuntimeError('Report stream lacks normal completion; do not score truncated output')
    return response

GenericLLMProvider.stream_response=_complete_report_stream
if DEFENSE_MODE=='root_query_anchoring':
    import gpt_researcher.skills.deep_research as deep_research_module
    _native_completion=deep_research_module.create_chat_completion
    async def _anchored_completion(*args, **kwargs):
        messages=kwargs.get('messages', [])
        planning_systems={
            'You are an expert researcher generating search queries.',
            'You are an expert researcher analyzing search results.',
            'You are an expert researcher. Your task is to analyze the original query and search results, then generate targeted questions that explore different aspects and time periods of the topic.'
        }
        if any(m.get('role')=='system' and m.get('content') in planning_systems for m in messages):
            kwargs=dict(kwargs)
            kwargs['messages']=[{**m, 'content':anchor_recursive_prompt(QUESTION,m['content'],'')} if m.get('role')=='user' else m for m in messages]
        return await _native_completion(*args, **kwargs)
    deep_research_module.create_chat_completion=_anchored_completion
import requests
TASK_FILE=ROOT.parent/'perplexica/task.json'
TASK=json.loads(TASK_FILE.read_text(encoding='utf-8'))
QUESTION=TASK.get('question','').strip()
if not QUESTION:raise RuntimeError(f'No question configured in {TASK_FILE}')
LOCK=threading.Lock()
def save(name,obj):(OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
def log(name,obj):
    with LOCK:
        with (OUT/name).open('a',encoding='utf-8') as f:f.write(json.dumps(obj,ensure_ascii=False,default=str)+'\n')
original_request=requests.Session.request
def request(session,method,url,*args,**kwargs):
    original=str(url);p=urllib.parse.urlparse(original);target=original
    if p.hostname=='api.tavily.com' and p.path=='/search':target=WIKI_BASE+'/gpt-researcher/tavily/search'
    elif p.hostname=='en.wikipedia.org' and p.path.startswith('/wiki/'):
        target=WIKI_BASE+'/gpt-researcher/page?'+urllib.parse.urlencode({'url':original})
    elif p.hostname not in ('localhost','127.0.0.1'):
        log('blocked_network.jsonl',{'url':original,'method':method});raise PermissionError('Evidence URL outside Wiki sandbox: '+original)
    start=time.time();response=original_request(session,method,target,*args,**kwargs)
    log('network.jsonl',{'started_unix':start,'seconds':time.time()-start,'method':method,'original_url':original,'transport_url':target,'status':response.status_code})
    if '/gpt-researcher/page?' in target and response.status_code==200:
        folder=OUT/'received_pages';folder.mkdir(exist_ok=True)
        stem=hashlib.sha256(original.encode()).hexdigest()
        (folder/(stem+'.html')).write_bytes(response.content)
        (folder/(stem+'.json')).write_text(json.dumps({'url':original}),encoding='utf-8')
    response.url=original
    return response
requests.Session.request=request
def guard(event,args):
    if event=='socket.connect' and isinstance(args[1],tuple):
        host=str(args[1][0])
        try:allowed=ipaddress.ip_address(host).is_loopback
        except ValueError:allowed=host=='localhost'
        if not allowed:
            log('blocked_network.jsonl',{'socket_host':host});raise PermissionError('Only local evidence and API gateways allowed')
sys.addaudithook(guard)
class Observer:
    async def send_json(self,data):log('events.jsonl',{'observed_unix':time.time(),'event':data})
    async def send_text(self,data):await self.send_json(data)
async def main():
    if (OUT/'result.json').exists():raise RuntimeError('Archive previous result first')
    manifest=(SHARED/'data/environment_manifest.json').read_bytes()
    env=json.loads(manifest.decode('utf-8-sig'))
    with urllib.request.urlopen(WIKI_BASE+'/status') as response:status=json.load(response)
    assert env['version']==status['environment_version'] and env['articles']==status['articles']
    assert hashlib.sha256((SHARED/'src/wiki_sandbox.py').read_bytes()).hexdigest()==env['source_sha256']
    save('configuration.json',{'question':QUESTION,'defaults':DEFAULT_CONFIG,'environment':env,'status':status,'framework_source_modified':False,'transport_only':DEFENSE_MODE!='root_query_anchoring','defense_mode':DEFENSE_MODE})
    started=time.time();start=time.perf_counter();result={'started_unix':started,'question':QUESTION,'environment_version':env['version'],'framework_source_modified':False}
    try:
        researcher=GPTResearcher(query=QUESTION,report_type='deep',report_source='web',websocket=Observer(),verbose=False)
        save('resolved_configuration.json',vars(researcher.cfg))
        save('live_status.json',{'phase':'researching','started_unix':started})
        context=await researcher.conduct_research();result['research_seconds']=time.perf_counter()-start
        (OUT/'context.txt').write_text(str(context),encoding='utf-8')
        save('live_status.json',{'phase':'writing','started_unix':started})
        report=await researcher.write_report()
        if not isinstance(report,str) or not report.strip():raise RuntimeError('Native write_report returned empty output')
        (OUT/'report.md').write_text(report,encoding='utf-8');sources=researcher.get_research_sources();save('sources.json',sources)
        result.update(completed=True,report_words=len(report.split()),source_count=len(sources),context_characters=len(str(context)))
    except Exception as exc:result.update(completed=False,error=str(exc),traceback=traceback.format_exc())
    finally:
        result.update(elapsed_seconds=time.perf_counter()-start,environment_unchanged=manifest==(SHARED/'data/environment_manifest.json').read_bytes())
        save('result.json',result);save('live_status.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)
if '--check' in sys.argv:
    print('Native GPTResearcher imports and canonical task loaded; no inference run')
else:
    asyncio.run(main())
