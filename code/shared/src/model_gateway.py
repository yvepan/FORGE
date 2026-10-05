"""Credential-in-memory gateway; native API JSON and SSE pass through unchanged."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import getpass,json,re,ssl,threading,time,urllib.request,urllib.error,os
import requests
import truststore
truststore.inject_into_ssl()
UPSTREAM_POOL=requests.adapters.HTTPAdapter(pool_connections=4,pool_maxsize=16,pool_block=True,max_retries=0)

def connect(req, trace):
    # requests uses the configured HTTP(S)_PROXY environment variables and has
    # a more reliable Windows TLS path than urllib for this provider.
    headers={k:v for k,v in req.header_items() if k.lower() not in ('content-length','accept-encoding')}
    headers['Accept-Encoding']='identity'
    attempts=[];trace['transport_attempts']=attempts
    retries=int(os.environ.get('FORGE_GATEWAY_RETRIES','1'))
    if not 0<=retries<=2:raise ValueError('Gateway retries must be 0 to 2')
    for attempt in range(retries+1):
        try:
            # Per-request cookies/headers remain isolated; only urllib3's
            # thread-safe connection pool is shared to reuse verified TLS.
            session=requests.Session()
            session.mount('https://',UPSTREAM_POOL)
            session.mount('http://',UPSTREAM_POOL)
            response=session.request(req.get_method(),req.full_url,data=req.data,headers=headers,timeout=(15,600),stream=True)
        except (requests.exceptions.ConnectionError,requests.exceptions.Timeout) as exc:
            attempts.append({'attempt':attempt+1,'error':safe(str(exc))})
            if attempt>=retries:raise
            time.sleep(1);continue
        attempts.append({'attempt':attempt+1,'status':response.status_code})
        if response.status_code in (408,429,502,503,504) and attempt<retries:
            response.close();time.sleep(1);continue
        return RequestsUpstream(response)

class RequestsUpstream:
    """Small response facade shared by JSON and SSE forwarding paths."""
    def __init__(self,response):self._response=response;self.status=response.status_code
    @property
    def headers(self):return self._response.headers
    def read(self):return self._response.content
    def __iter__(self):
        yield from self._response.iter_content(chunk_size=8192)
    def __enter__(self):return self
    def __exit__(self,*exc):self._response.close();return False

ROOT=Path(os.environ.get('FORGE_SHARED_DIR',Path(__file__).resolve().parents[1]));OUT=ROOT/'results/model_api';OUT.mkdir(parents=True,exist_ok=True)
KEY=os.environ.get('FORGE_API_KEY','')
API_ORIGIN=os.environ.get('FORGE_API_ORIGIN','').rstrip('/')
LOCAL_EMBEDDING_PATH=os.environ.get('FORGE_LOCAL_EMBEDDING_MODEL_PATH','')
LOCAL_EMBEDDING_NAME=os.environ.get('FORGE_LOCAL_EMBEDDING_MODEL_NAME','local-bge-small-en-v1.5')
if not API_ORIGIN:raise SystemExit('Set FORGE_API_ORIGIN to the authorized model service origin')
if not KEY:raise SystemExit('Missing key')
LOCK=threading.Lock();COUNT=max((int(p.stem) for p in OUT.glob('*/*.json') if p.stem.isdigit()),default=0);ACTIVE=0
EMBEDDING_LOCK=threading.Lock();EMBEDDING_TOKENIZER=None;EMBEDDING_MODEL=None;EMBEDDING_DEVICE=None

def local_embeddings(payload):
    """Serve one frozen local model through the OpenAI embeddings schema."""
    global EMBEDDING_TOKENIZER,EMBEDDING_MODEL,EMBEDDING_DEVICE
    if not LOCAL_EMBEDDING_PATH:return None
    if payload.get('model') not in (None,'',LOCAL_EMBEDDING_NAME):
        raise ValueError(f"Local embedding gateway only serves {LOCAL_EMBEDDING_NAME}")
    values=payload.get('input',[]);values=[values] if isinstance(values,str) else values
    if not isinstance(values,list) or not all(isinstance(x,str) for x in values):raise ValueError('Embedding input must be a string or list of strings')
    with EMBEDDING_LOCK:
        if EMBEDDING_MODEL is None:
            import torch
            from transformers import AutoModel,AutoTokenizer
            path=str(Path(LOCAL_EMBEDDING_PATH).resolve())
            EMBEDDING_DEVICE='cuda' if os.environ.get('FORGE_EMBEDDING_DEVICE','auto')!='cpu' and torch.cuda.is_available() else 'cpu'
            EMBEDDING_TOKENIZER=AutoTokenizer.from_pretrained(path,local_files_only=True)
            EMBEDDING_MODEL=AutoModel.from_pretrained(path,local_files_only=True).to(EMBEDDING_DEVICE);EMBEDDING_MODEL.eval()
        import torch
        encoded=EMBEDDING_TOKENIZER(values,padding=True,truncation=True,max_length=512,return_tensors='pt')
        encoded={k:v.to(EMBEDDING_DEVICE) for k,v in encoded.items()}
        with torch.no_grad():hidden=EMBEDDING_MODEL(**encoded).last_hidden_state
        pooled=hidden[:,0]
        pooled=torch.nn.functional.normalize(pooled,p=2,dim=1).cpu().tolist()
        token_count=int(encoded['attention_mask'].sum().item())
    return {'object':'list','data':[{'object':'embedding','index':i,'embedding':vector} for i,vector in enumerate(pooled)],
            'model':LOCAL_EMBEDDING_NAME,'usage':{'prompt_tokens':token_count,'total_tokens':token_count}}

def safe(text):return re.sub(r'sk-[A-Za-z0-9_-]{12,}','[REDACTED]',text.replace(KEY,'[REDACTED]'))
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def reply(self,status,obj):
        raw=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/status':return self.reply(200,{'ready':True,'requests':COUNT,'active':ACTIVE,'credentials_saved':False,'streaming':'SSE pass-through'})
        self.forward(None)
    def do_POST(self):
        if self.path=='/shutdown':
            self.reply(200,{'stopping':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
        self.forward(self.rfile.read(int(self.headers.get('Content-Length',0))))
    def forward(self,body):
        global COUNT,ACTIVE
        parts=self.path.strip('/').split('/',1)
        if len(parts)!=2:return self.reply(404,{'error':'Use /framework/v1/... endpoints'})
        fw,tail=parts;route='/'+tail
        if fw not in ('gpt-researcher','perplexica','webthinker','construction','preflight','evaluation'):return self.reply(404,{'error':'Unknown framework'})
        if route not in ('/v1/models','/v1/chat/completions','/v1/completions','/v1/embeddings','/v1/responses','/v1/messages'):return self.reply(404,{'error':'Unknown model endpoint'})
        payload=json.loads(body) if body else {}
        with LOCK:COUNT+=1;idx=COUNT;ACTIVE+=1
        start=time.time();trace={'id':idx,'framework':fw,'endpoint':route,'started_unix':start,'request':payload,'stream':bool(payload.get('stream'))}
        folder=OUT/fw;folder.mkdir(exist_ok=True)
        local_embedding=route=='/v1/embeddings' and bool(LOCAL_EMBEDDING_PATH)
        req=None
        if not local_embedding:
            headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/json'}
            if route=='/v1/messages':headers.update({'x-api-key':KEY,'anthropic-version':'2023-06-01'})
            req=urllib.request.Request(API_ORIGIN+route,data=body,headers=headers)
        raw=b'';status=0;sent=False;disconnected=False;ctype='application/json'
        try:
            if local_embedding:
                raw=json.dumps(local_embeddings(payload),ensure_ascii=False).encode();status=200;trace['local_embedding']=True
            else:
                with connect(req,trace) as response:
                    status=response.status;ctype=response.headers.get('Content-Type',ctype)
                    if 'text/event-stream' in ctype:
                        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-cache');self.send_header('Connection','close');self.end_headers();sent=True;self.close_connection=True
                        for line in response:
                            raw+=line
                            try:self.wfile.write(line);self.wfile.flush()
                            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):disconnected=True;break
                    else:raw=response.read()
        except urllib.error.HTTPError as exc:status=exc.code;raw=exc.read()
        except Exception as exc:status=502;raw=json.dumps({'error':{'message':str(exc)}}).encode()
        finally:
            with LOCK:ACTIVE-=1
        trace.update(status=status,elapsed_seconds=time.time()-start,client_disconnected=disconnected,response_bytes=len(raw))
        decoded=safe(raw.decode('utf-8',errors='replace'))
        if local_embedding and status==200:
            result=json.loads(decoded);trace['response']={'object':'list','model':result['model'],'vectors':len(result['data']),'dimensions':len(result['data'][0]['embedding']) if result['data'] else 0,'usage':result['usage']}
        else:
            try:trace['response']=json.loads(decoded)
            except Exception:trace['response_text']=decoded
        (folder/f'{idx:05d}.json').write_text(safe(json.dumps(trace,ensure_ascii=False,indent=2)),encoding='utf-8')
        ledger={k:v for k,v in trace.items() if k not in ('request','response','response_text')};ledger['model']=payload.get('model')
        with LOCK:
            with (OUT/'ledger.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(ledger,ensure_ascii=False)+'\n')
        if not sent:
            content=decoded.encode();self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(content)));self.end_headers()
            try:self.wfile.write(content)
            except OSError:pass
        print(json.dumps({'framework':fw,'model':payload.get('model'),'status':status,'seconds':round(trace['elapsed_seconds'],2),'cancelled':disconnected}),flush=True)
PORT=int(os.environ.get('FORGE_MODEL_GATEWAY_PORT','8788'))
server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
print(json.dumps({'ready':f'127.0.0.1:{PORT}','local_embedding_model':LOCAL_EMBEDDING_NAME if LOCAL_EMBEDDING_PATH else None,'local_embedding_path':LOCAL_EMBEDDING_PATH or None}),flush=True)
server.serve_forever();KEY='';server.server_close();UPSTREAM_POOL.close()
