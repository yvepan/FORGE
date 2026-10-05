"""One read-only Wiki search environment, exposed in native provider protocols."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import datetime,html,json,re,sqlite3,threading,time,urllib.parse,os
from nltk.stem import PorterStemmer
from defense_policies import MODE,expanded_candidate_k,is_enabled,rewrite_query
ROOT=Path(os.environ.get('FORGE_SHARED_DIR',Path(__file__).resolve().parents[1]));ENV=json.loads((ROOT/'data/environment_manifest.json').read_text(encoding='utf-8'));DB=Path(ENV['database']);N=ENV['articles']
STOP=set('a an the and or of in to for is are was were on with what how which why does do did that from as by it this at be have has their its using use used available up then compare comparison assess develop explain would should could evidence research review based between under into than through such also any each both all not only other most much more less some these those when where who can may will including across against within about over during recent latest current findings implications comprehensive detailed scientific distinction november need prioritize prioritized changing change justify justified please wikipedia wiki snapshot local supplied corpus encyclopedia encyclopaedia'.split())
STEM=PorterStemmer(mode=PorterStemmer.ORIGINAL_ALGORITHM);LOCK=threading.Lock();COUNTER=0;ACTIVE=0
def connect():
    db=sqlite3.connect(f'file:{DB.as_posix()}?mode=ro',uri=True,timeout=60);db.execute('PRAGMA cache_size=-65536');db.execute('PRAGMA temp_store=MEMORY');return db
def log(name,row):
    with LOCK:
        with (ROOT/'results'/name).open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
def domain_matches(pattern):
    domain=urllib.parse.urlparse(pattern if '://' in pattern else '//'+pattern).netloc.lower().lstrip('*.')
    return 'en.wikipedia.org'==domain or 'en.wikipedia.org'.endswith('.'+domain)
def search(query,k=10,include_domains=None,exclude_domains=None,framework='preflight'):
    start=time.perf_counter();original_query=query;native_k=k
    rewritten_query=rewrite_query(query,framework) if is_enabled('query_paraphrasing') else query
    retrieval_k=expanded_candidate_k(k) if is_enabled('knowledge_expansion') else k
    query=rewritten_query
    sites=re.findall(r'(?<!\S)site:([^\s)]+)',query,re.I);excluded=re.findall(r'(?<!\S)-site:([^\s)]+)',query,re.I)
    clean=re.sub(r'(?<!\S)-?(?:site|before|after):[^\s)]+',' ',query,flags=re.I)
    terms=list(dict.fromkeys(w.lower() for w in re.findall(r'[A-Za-z][A-Za-z0-9]+',clean) if len(w)>2 and w.lower() not in STOP))[:40]
    allowed=all(any(domain_matches(d) for d in group) for group in (sites,include_domains or []) if group) and not any(domain_matches(d) for d in (excluded+(exclude_domains or [])))
    rows=[];anchors=[];expr='';fallback=False;title_hits=[]
    with connect() as db:
        if terms and allowed:
            # Encyclopedia title lookup: exact consecutive words in the query.
            # This is shared across all tasks/frameworks, not a query-specific alias list.
            words=re.findall(r'[A-Za-z][A-Za-z0-9-]*',clean)[:60]
            variants={}
            for width in range(min(8,len(words)),0,-1):
                for pos in range(len(words)-width+1):
                    group=words[pos:pos+width]
                    if width==1 and (not group[0].isupper() or group[0].lower() in STOP):continue
                    phrase=' '.join(group)
                    if not any(w.lower() not in STOP for w in group):continue
                    for form in (phrase,phrase[:1].upper()+phrase[1:].lower(),phrase.title()):
                        variants.setdefault(form,(width,pos))
            if variants:
                titles=db.execute('SELECT id,url,title,text FROM articles WHERE title IN ('+','.join('?' for _ in variants)+')',list(variants)).fetchall()
                titles.sort(key=lambda r:(-variants[r[2]][0],variants[r[2]][1],r[0]))
                title_hits=[{'id':r[0],'url':r[1],'title':r[2],'snippet':r[3][:2200],'text':r[3],'bm25':None,'exact_title_match':True,'score':1.0} for r in titles]
            db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main,article_fts,'row')")
            original_by_stem={}
            for term in terms:original_by_stem.setdefault(STEM.stem(term),term)
            stems=list(original_by_stem)
            freq=db.execute('SELECT term,doc FROM temp.vocab WHERE term IN ('+','.join('?' for _ in stems)+')',stems).fetchall()
            # MATCH applies Porter itself. Reusing a stored stem would stem twice
            # (e.g. sonofusion -> sonofus -> sonofu) and silently lose matches.
            anchors=[original_by_stem[s] for s,n in sorted(freq,key=lambda p:(p[1],p[0]))][:2]
            body=' OR '.join('"'+t+'"' for t in terms)
            expr='('+' AND '.join('"'+t+'"' for t in anchors)+') AND ('+body+')' if anchors else body
            sql="SELECT a.id,a.url,a.title,a.text,f.rank FROM (SELECT rowid,rank FROM article_fts WHERE article_fts MATCH ? AND rank MATCH 'bm25(4.0,1.0)' ORDER BY rank LIMIT ?) f JOIN articles a ON a.id=f.rowid ORDER BY f.rank"
            rows=db.execute(sql,(expr,retrieval_k)).fetchall()
            if not rows and len(anchors)>1:
                fallback=True;expr='"'+anchors[0]+'" AND ('+body+')';rows=db.execute(sql,(expr,retrieval_k)).fetchall()
    combined=title_hits;seen={r['id'] for r in combined}
    for r in rows:
        if r[0] not in seen:
            bm25=-r[4];combined.append({'id':r[0],'url':r[1],'title':r[2],'snippet':r[3][:2200],'text':r[3],'bm25':bm25,'exact_title_match':False,'score':bm25/(1+bm25)});seen.add(r[0])
    returned=combined[:retrieval_k]
    return {'query':original_query,'rewritten_query':rewritten_query if rewritten_query!=original_query else None,
            'terms':terms,'anchors':anchors,'fts_expression':expr,'exact_title_matches':[r['title'] for r in title_hits],
            'fallback_to_rarest_anchor':fallback,'domain_filter_permits_wiki':allowed,'requested_results':native_k,
            'retrieval_results':retrieval_k,'defense_mode':MODE,
            'seconds':time.perf_counter()-start,'results':returned}
def article(url):
    parsed=urllib.parse.urlparse(url)
    if parsed.hostname not in ('en.wikipedia.org','en.m.wikipedia.org') or not parsed.path.startswith('/wiki/'):
        return None
    title=urllib.parse.unquote(parsed.path[6:]).replace('_',' ')
    with connect() as db:
        row=db.execute('SELECT id,url,title,text FROM articles WHERE title=?',(title,)).fetchone()
        if not row and title:row=db.execute('SELECT id,url,title,text FROM articles WHERE title=?',(title[:1].upper()+title[1:],)).fetchone()
    return {'id':row[0],'url':row[1],'title':row[2],'text':row[3]} if row else None
def render(a):
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>'+html.escape(a['title'])+' - Wikipedia</title><link rel="canonical" href="'+html.escape(a['url'],quote=True)+'"></head><body><main><article><h1>'+html.escape(a['title'])+'</h1><p>'+html.escape(a['text']).replace('\n','</p><p>')+'</p></article></main></body></html>'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def reply(self,obj,status=200,ctype='application/json; charset=utf-8'):
        raw=obj.encode() if isinstance(obj,str) else json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)));self.end_headers()
        try:self.wfile.write(raw)
        except OSError:pass
    def do_GET(self):self.handle_request({})
    def do_POST(self):
        try:payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or '{}')
        except Exception:return self.reply({'error':'Invalid JSON'},400)
        self.handle_request(payload)
    def handle_request(self,payload):
        global COUNTER,ACTIVE
        parsed=urllib.parse.urlparse(self.path);params=urllib.parse.parse_qs(parsed.query)
        if parsed.path=='/status':return self.reply({'ready':True,'environment_version':ENV['version'],'articles':N,'full_corpus':True,'vectors_built':False,'active':ACTIVE,'requests':COUNTER,'defense_mode':MODE,'backend':'Exact encyclopedia title matches, then shared FTS5 bm25(4,1) conjunctive two rare original query terms; zero-result fallback to one rare term; Porter applied once by MATCH; no per-framework changes'})
        if parsed.path=='/shutdown':
            self.reply({'stopping':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
        parts=parsed.path.strip('/').split('/',1)
        if len(parts)!=2:return self.reply({'error':'Use /framework/provider/path'},404)
        fw,route=parts
        if fw not in ('gpt-researcher','perplexica','webthinker','preflight'):return self.reply({'error':'Unknown framework'},404)
        with LOCK:COUNTER+=1;idx=COUNTER;ACTIVE+=1
        started=time.time();stage=self.headers.get('X-Sandbox-Stage','run')
        try:
            if route in ('page','jina'):
                url=payload.get('url') or params.get('url',[''])[0];a=article(url)
                log('pages.jsonl',{'id':idx,'framework':fw,'stage':stage,'started_unix':started,'url':url,'status':200 if a else 404,'article_id':a['id'] if a else None,'title':a['title'] if a else None,'characters':len(a['text']) if a else 0})
                return self.reply(render(a) if a else '<html><body>Page unavailable in the supplied Wikipedia snapshot.</body></html>',200 if a else 404,'text/html; charset=utf-8')
            if route not in ('tavily/search','searxng/search','bing/search'):return self.reply({'error':'Unknown sandbox route'},404)
            query=payload.get('query') or params.get('q',params.get('query',['']))[0]
            k=int(payload.get('max_results') or params.get('count',[10])[0]);k=max(1,min(k,50))
            result=search(query,k,payload.get('include_domains'),payload.get('exclude_domains'),framework=fw)
            audit={k:v for k,v in result.items() if k!='results'}
            audit.update(id=idx,framework=fw,stage=stage,started_unix=started,provider=route,request={k:v for k,v in payload.items() if k not in ('api_key',)},results=[{k:v for k,v in r.items() if k!='text'} for r in result['results']])
            log('searches.jsonl',audit)
            found=result['results']
            if route=='tavily/search':
                rows=[{'title':r['title'],'url':r['url'],'content':r['snippet'],'score':r['score'],**({'raw_content':r['text']} if payload.get('include_raw_content') else {})} for r in found]
                return self.reply({'query':query,'results':rows,'images':[],'answer':None,'response_time':result['seconds']})
            if route=='searxng/search':return self.reply({'results':[{'title':r['title'],'url':r['url'],'content':r['snippet']} for r in found],'suggestions':[]})
            return self.reply({'webPages':{'value':[{'name':r['title'],'url':r['url'],'snippet':r['snippet']} for r in found]}})
        except Exception as exc:
            log('errors.jsonl',{'id':idx,'framework':fw,'started_unix':started,'route':route,'error':str(exc)});self.reply({'error':str(exc)},500)
        finally:
            with LOCK:ACTIVE-=1
if __name__=='__main__':
    manifest=json.loads((ROOT/'data/corpus_manifest.json').read_text(encoding='utf-8'))
    assert manifest['articles']==N and manifest['global_fts_statistics_cleaned']
    port=int(os.environ.get('FORGE_WIKI_PORT','8790'))
    print(f'Shared full-Wikipedia sandbox ready at 127.0.0.1:{port}',flush=True)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.serve_forever();server.server_close()
