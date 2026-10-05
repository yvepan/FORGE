"""Model-boundary compatibility adapter for the unchanged WebThinker loop.

Expose text completions to the vendor, reconstruct ChatML messages for the
authorized chat backend, collect reasoning + content, and retain the first native
stop marker. Assistant-prefix continuation requires independently verified provider support.
Message conversion does not establish raw decoder-state equivalence.
"""
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import argparse,json,re,threading,time,urllib.request,urllib.error,os
ROOT=Path(os.environ['FORGE_FRAMEWORK_DIR'])
OUT=ROOT/'results/adapter'
OUT.mkdir(parents=True,exist_ok=True)
LOCK=threading.Lock();COUNT=0

def decode_chatml(prompt):
    messages=[];prefix=''
    pieces=prompt.split('<|im_start|>')
    if pieces[0].strip():raise ValueError('Expected tokenizer ChatML prompt')
    for i,piece in enumerate(pieces[1:]):
        role,content=piece.split('\n',1)
        if role not in ['system','user','assistant','tool']:raise ValueError(f'Unexpected role {role}')
        if '<|im_end|>' in content:
            content=content.split('<|im_end|>',1)[0]
            messages.append({'role':role,'content':content})
        else:
            if role!='assistant':raise ValueError('Unclosed non-assistant ChatML message')
            prefix=content
    return messages,prefix

def translate(payload,config):
    messages,prefix=decode_chatml(payload['prompt'])
    # Classify native prompts for logging without adding instructions.
    action_kind=None
    users=[m['content'] for m in messages if m['role']=='user']
    # The existing RQA policy wraps native chat prompts; classify its subtask,
    # preserving the root query and complete wrapper in the upstream request.
    marker='\nCURRENT SUBTASK OR LEARNINGS:\n'
    users=[m.split(marker,1)[1] if m.lstrip().startswith('ROOT USER QUERY (keep the research directed at this objective):') and marker in m else m for m in users]
    final_report=bool(config.get('final_report_model') and any(m.lstrip().startswith('You are an final-version article editor. Your task is to correct the structure of the following article draft.') for m in users))
    if any(m.lstrip().startswith('You are a research assistant with the ability to perform web searches') for m in users):action_kind='report'
    elif any(m.lstrip().startswith('You are a web explorer analyzing search results') for m in users):action_kind='explorer'
    continuation=bool(prefix.strip() and prefix.strip()!='<think>')
    strategy=config.get('continuation_strategy','partial')
    if continuation:
        if config.get('partial_continuation_verified') is not True:
            raise ValueError('Assistant-prefix continuation requires independently verified provider support; chat replay is not paper-equivalent')
        if strategy=='partial':messages.append({'role':'assistant','content':prefix,'partial':True})
        elif strategy=='vllm_continue':messages.append({'role':'assistant','content':prefix})
        else:raise ValueError(strategy)
    upstream={key:payload[key] for key in ['model','temperature','top_p','max_tokens','top_k','repetition_penalty'] if key in payload}
    upstream['model']=config.get('model_aliases',{}).get(payload['model'],payload['model'])
    if final_report:upstream['model']=config['final_report_model']
    upstream.update(messages=messages,stream=True)
    if continuation and strategy=='vllm_continue':upstream.update(continue_final_message=True,add_generation_prompt=False)
    if config.get('auxiliary_no_thinking') and payload['model']==config['auxiliary_model']:
        upstream['enable_thinking']=False
    if (payload['model']==config.get('primary_model') or final_report) and config.get('primary_enable_thinking') is not None:
        upstream['enable_thinking']=config['primary_enable_thinking']
    cap=config.get('final_report_max_output_tokens') if final_report else config.get('max_output_tokens_by_model',{}).get(payload['model'])
    if cap is not None:upstream['max_tokens']=min(upstream.get('max_tokens',cap),cap)
    # The configured Gemini-compatible provider ignores max_tokens alone.
    # Preserve the configured 16384-token limit in compatible provider transports.
    if config.get('auxiliary_max_completion_tokens') and payload['model']==config.get('auxiliary_model') and not final_report:
        upstream['max_completion_tokens']=upstream['max_tokens']
    for key in config.get('unsupported_sampling_parameters',[]):upstream.pop(key,None)
    # Upstream stop is deliberately omitted: it commonly drops the stop string.
    # We must return it to the untouched vendor loop to select its next action.
    return upstream,{'continuation':continuation,'strategy':strategy if continuation else 'initial_chat',
        'model_role':'final_report' if final_report else ('primary' if action_kind else 'auxiliary'),
        'requested_max_tokens':payload.get('max_tokens'),'effective_max_tokens':upstream.get('max_tokens'),
        'assistant_prefix_chars':len(prefix),'input_messages':len(messages),'action_kind':action_kind,'raw_decoder_state_equivalence_guaranteed':False}

def run_completion(payload,config):
    global COUNT
    with LOCK:COUNT+=1;idx=COUNT
    start=time.perf_counter();started=time.time()
    upstream,meta=translate(payload,config)
    stops=payload.get('stop') or []
    if isinstance(stops,str):stops=[stops]
    record={'id':idx,'started_unix':started,'native_request':payload,'upstream_request':upstream,'translation':meta}
    (OUT/f'{idx:05d}.request.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    output='';seen_reasoning=False;seen_content=False;events=[];matched=None;first_chunk=None;finish_reason=None;usage=None;stream_received=False;done_received=False
    def consume(event):
        nonlocal output,seen_reasoning,seen_content,matched,first_chunk,finish_reason,usage
        events.append(event)
        if event.get('error'):
            raise RuntimeError('Upstream error event: '+json.dumps(event['error'],ensure_ascii=False))
        if first_chunk is None:first_chunk=time.perf_counter()-start
        if event.get('usage'):usage=event['usage']
        for choice in event.get('choices',[]):
            delta=choice.get('delta') or choice.get('message') or {}
            reason=delta.get('reasoning_content') or ''
            content=delta.get('content') or ''
            if reason:
                seen_reasoning=True
                if config.get('include_reasoning_in_completion',True):output+=reason
            if content:
                if seen_reasoning and not seen_content and config.get('include_reasoning_in_completion',True):output+='</think>\n'
                seen_content=True;output+=content
            if choice.get('finish_reason'):finish_reason=choice['finish_reason']
        matches=[(output.find(stop),stop) for stop in stops if stop in output]
        if matches:
            pos,matched=min(matches,key=lambda pair:pair[0])
            output=output[:pos+len(matched)]
            return True
        return False
    req=urllib.request.Request(config['upstream_base'].rstrip('/')+'/chat/completions',data=json.dumps(upstream).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer memory-proxy'})
    try:
        with urllib.request.urlopen(req,timeout=600) as response:
            record['status']=response.status
            record['response_content_type']=response.headers.get('Content-Type')
            unframed=[]
            for line in response:
                line=line.decode('utf-8').strip()
                if not line.startswith('data:'):
                    if line and not line.startswith(('event:',':')):unframed.append(line)
                    continue
                stream_received=True
                data=line[5:].strip()
                if data=='[DONE]':
                    done_received=True
                    break
                if consume(json.loads(data)):break
            if not stream_received and unframed:
                raw_body='\n'.join(unframed);record['nonstream_response_body']=raw_body
                consume(json.loads(raw_body))
        if not (matched or done_received or finish_reason is not None):
            raise RuntimeError('Incomplete upstream response: EOF without a complete native stop marker, provider finish_reason, or [DONE]; retry this request')
        if not matched and finish_reason in ('length', 'content_filter'):
            raise RuntimeError('Truncated or filtered model response is not a completed native action')
        if not output.strip():raise RuntimeError('Upstream returned no generated text; refusing to treat an empty response as a completed native action')
        record.update(output=output,stop_marker=matched,request_closed_at_stop=bool(matched),stream_received=stream_received,
            reasoning_received=seen_reasoning,content_received=seen_content,time_to_first_chunk=first_chunk,
            seconds=time.perf_counter()-start,upstream_finish_reason=finish_reason,usage=usage,events=events)
        result={'id':f'cmpl-webthinker-{idx}','object':'text_completion','created':int(started),'model':payload['model'],
            'choices':[{'index':0,'text':output,'logprobs':None,'finish_reason':'stop' if matched else (finish_reason or 'stop')}]}
        if usage:result['usage']=usage
        return result
    except urllib.error.HTTPError as error:
        record.update(status=error.code,error=error.read().decode('utf-8',errors='replace'),seconds=time.perf_counter()-start)
        raise RuntimeError(f'Upstream HTTP {error.code}: {record["error"]}') from error
    except Exception as error:
        record.update(error=str(error),seconds=time.perf_counter()-start);raise
    finally:
        record.update(output=output,stop_marker=matched,request_closed_at_stop=bool(matched),
            stream_received=stream_received,upstream_done_received=done_received,
            upstream_finish_reason=finish_reason,
            completion_evidence='native_stop_marker' if matched else ('provider_finish_reason' if finish_reason is not None else ('done_frame' if done_received else None)))
        record.setdefault('events',events)
        (OUT/f'{idx:05d}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'id':idx,'seconds':time.perf_counter()-start,'continuation':meta['continuation'],
                          'stop':matched,'chars':len(output),'error':record.get('error')},ensure_ascii=False),flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,body):
        raw=json.dumps(body,ensure_ascii=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
        try:self.wfile.write(raw)
        except (ConnectionResetError,ConnectionAbortedError,BrokenPipeError):pass
    def do_GET(self):
        if self.path=='/status':return self.reply(200,{'ready':True,'requests':COUNT})
        if self.path=='/shutdown':
            self.reply(200,{'stopping':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
        self.reply(404,{'error':'unknown route'})
    def do_POST(self):
        if self.path!='/v1/completions':return self.reply(404,{'error':'adapter exposes completions only'})
        try:
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            config=json.loads((ROOT/'config/adapter.json').read_text(encoding='utf-8'))
            gateway_base=os.environ.get('FORGE_MODEL_GATEWAY_BASE')
            if gateway_base:config['upstream_base']=gateway_base.rstrip('/')+'/webthinker/v1'
            self.reply(200,run_completion(payload,config))
        except Exception as error:self.reply(502,{'error':{'message':str(error),'type':'adapter_or_upstream_error'}})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=int(os.environ.get('FORGE_WEBTHINKER_ADAPTER_PORT','8789')));args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'WebThinker model-boundary adapter ready on {args.port}',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
