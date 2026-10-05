import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import SearchAgent from '../../../../vendor/perplexica/src/lib/agents/search';
import SessionManager from '../../../../vendor/perplexica/src/lib/session';
import OpenAILLM from '../../../../vendor/perplexica/src/lib/models/providers/openai/openaiLLM';
import OpenAIEmbedding from '../../../../vendor/perplexica/src/lib/models/providers/openai/openaiEmbedding';
import {installWikiBrowserEnvironment} from './browser_environment';
import {anchorPlanningMessages} from './rqa';

const ROOT=process.cwd();
const task=JSON.parse(fs.readFileSync(path.resolve(ROOT,'task.json'),'utf8'));
const QUESTION=task.question;
if(!QUESTION.trim())throw Error('Task question must not be empty');
const MODEL=process.env.DR_MODEL;
if(!MODEL)throw Error('An explicit paper victim API model ID is required');
const EMBEDDING=process.env.DR_EMBEDDING||'local-bge-small-en-v1.5';
const DEFENSE_MODE=(process.env.FORGE_DEFENSE_MODE||'none').toLowerCase();
const API=process.env.DR_API_BASE||'http://127.0.0.1:8788/perplexica/v1';
const PAGE=process.env.WIKI_PAGE_ENDPOINT||'';
const RESULT=path.resolve(ROOT,process.env.RESULT_DIR||'results');
fs.mkdirSync(RESULT,{recursive:true});
const write=(file:string,row:any)=>fs.writeFileSync(path.join(RESULT,file),JSON.stringify(row,null,2));

async function main(){
 if(process.argv.includes('--check')){console.log('Unmodified native SearchAgent and public providers imported successfully');process.exit(0);}
 if(!PAGE||!process.env.SEARXNG_API_URL)throw Error('Set the shared Wiki page endpoint and public SEARXNG_API_URL');
 if(fs.existsSync(path.join(RESULT,'result.json')))throw Error('Refusing to overwrite a previous result');
 const manifestBytes=fs.readFileSync(path.resolve(ROOT,'../shared/data/environment_manifest.json'));
 const manifest=JSON.parse(manifestBytes.toString('utf8'));
 const sourceHash=crypto.createHash('sha256').update(fs.readFileSync(path.resolve(ROOT,'../shared/src/wiki_sandbox.py'))).digest('hex');
 if(sourceHash!==manifest.source_sha256)throw Error('Shared Wiki implementation changed after freeze');
 const status=await(await fetch(new URL('/status',PAGE))).json();
 if(!status.ready||status.environment_version!==manifest.version||status.articles!==manifest.articles)throw Error('Wrong shared Wiki environment');
 fs.writeFileSync(path.join(RESULT,'environment_manifest.json'),manifestBytes);
const config={framework:'Perplexica / Vane',paper_reference_revision:'348feca',source_revision_verified:false,entrypoint:'native SearchAgent.searchAsync',mode:'quality',native_max_iterations:25,source_selection:['web'],model:MODEL,embedding:EMBEDDING,embedding_location:'local OpenAI-compatible gateway',api_base:API,searxng_url:process.env.SEARXNG_API_URL,searxng_timeout_ms:60000,wiki_page_endpoint:PAGE,browser_executable:process.env.FORGE_CHROMIUM_EXECUTABLE||'playwright-default',environment_version:manifest.version,environment_source_sha256:sourceHash,environment_manifest_sha256:crypto.createHash('sha256').update(manifestBytes).digest('hex'),question:QUESTION,defense_mode:DEFENSE_MODE,system_instructions:'',tool_definitions_modified:false,agent_source_modified:false,agent_timeouts_modified:true,search_query_rewritten_by_launcher:false,framework_sees_original_wikipedia_urls:true,environment_adapter:'Public SearxNG endpoint and Playwright context.route. Browser executable may be replaced by an explicitly audited local system Chromium when the bundled binary is blocked.'};
 write('run_config.json',config);
 const events=fs.createWriteStream(path.join(RESULT,'events.jsonl'),{encoding:'utf8'});
 const requestLog=fs.createWriteStream(path.join(RESULT,'node_requests.jsonl'),{encoding:'utf8'});
 const originalFetch=globalThis.fetch;
 let requestId=0;
 globalThis.fetch=async(input:any,options:any)=>{
   const address=typeof input==='string'?input:input instanceof URL?input.href:input.url;
   if(DEFENSE_MODE==='root_query_anchoring'&&typeof options?.body==='string'){
     try{
       const body=JSON.parse(options.body);
       if(Array.isArray(body.messages)){
         body.messages=anchorPlanningMessages(body.messages,QUESTION);
         options={...options,body:JSON.stringify(body)};
       }
     }catch{}
   }
   let metadata:any={};
   if(typeof options?.body==='string'){
     try{const body=JSON.parse(options.body);metadata={model:body.model,stream:body.stream||false,has_tools:!!body.tools?.length,tool_names:body.tools?.map((t:any)=>t.function?.name),message_count:body.messages?.length,body_sha256:crypto.createHash('sha256').update(options.body).digest('hex')};}catch{}
   }
   requestLog.write(JSON.stringify({id:++requestId,started_unix:Date.now()/1000,url:address,method:options?.method||input?.method||'GET',...metadata})+'\n');
   // Browser evidence is separately routed; Node's framework HTTP transports
   // reach only the configured local search/model environment endpoints.
   if(!['127.0.0.1','localhost','::1'].includes(new URL(address).hostname))throw Error('Unmapped external transport: '+address);
   return originalFetch(input,options);
 };
 const environment=installWikiBrowserEnvironment(PAGE,RESULT);
 const llm=new OpenAILLM({apiKey:'memory-proxy',baseURL:API,model:MODEL});
 const nativeStreamText=llm.streamText.bind(llm);
 llm.streamText=async function*(input:any){
   let finishReason:string|null=null;
   for await(const chunk of nativeStreamText(input)){
     if(chunk.additionalInfo?.finishReason)finishReason=chunk.additionalInfo.finishReason;
     yield chunk;
   }
   if(!finishReason||['length','content_filter'].includes(finishReason))throw Error('Incomplete or truncated native text stream');
 };
 const embedding=new OpenAIEmbedding({apiKey:'memory-proxy',baseURL:API,model:EMBEDDING});
 const session=SessionManager.createSession();
 const start=performance.now(),startedUnix=Date.now()/1000;
 let researchSeconds:number|null=null,lastStatus=0,nativeEnded=false,nativeError=false;
 session.subscribe((event,data)=>{
   if(event==='end')nativeEnded=true;
   if(event==='error')nativeError=true;
   const elapsed=(performance.now()-start)/1000;
   events.write(JSON.stringify({elapsed_seconds:elapsed,event,data})+'\n');
   if(data?.type==='researchComplete')researchSeconds=elapsed;
   if(elapsed-lastStatus>=1||data?.type==='researchComplete'){
     lastStatus=elapsed;write('live_status.json',{phase:researchSeconds===null?'classification_and_research':'writing_report',elapsed_seconds:elapsed,event,event_type:data?.type});
   }
 });
 write('live_status.json',{phase:'starting',started_unix:startedUnix});
 try{
   await new SearchAgent().searchAsync(session,{chatHistory:[],followUp:QUESTION,chatId:'native-wiki-sandbox',messageId:'full-quality-01',config:{sources:['web'],fileIds:[],llm,embedding,mode:'quality',systemInstructions:''}});
   const elapsed=(performance.now()-start)/1000,blocks=session.getAllBlocks();
   const textBlocks=blocks.filter(b=>b.type==='text');
   if(textBlocks.some(b=>typeof b.data!=='string'))throw Error('Unexpected native TextBlock type');
   const report=textBlocks.map((b:any)=>b.data).join('\n\n');
   if(!nativeEnded||nativeError||!report.trim())throw Error('Native report did not complete successfully');
   const sources=blocks.filter(b=>b.type==='source').flatMap((b:any)=>b.data);
   fs.writeFileSync(path.join(RESULT,'report.md'),report);write('sources.json',sources);write('blocks.json',blocks);
   const row={...config,completed:report.trim().length>0,started_unix:startedUnix,elapsed_seconds:elapsed,research_seconds:researchSeconds,report_generation_seconds:researchSeconds===null?null:elapsed-researchSeconds,report_words:report.trim().split(/\s+/).length,source_count:sources.length};
   write('result.json',row);write('live_status.json',{phase:'completed',...row});console.log(JSON.stringify(row));
 }catch(error:any){
   const row={...config,completed:false,started_unix:startedUnix,elapsed_seconds:(performance.now()-start)/1000,research_seconds:researchSeconds,error:String(error),stack:error.stack};
   write('result.json',row);write('blocks.json',session.getAllBlocks());write('live_status.json',{phase:'failed',...row});console.error(error);process.exitCode=1;
 }finally{
   await environment.close();await new Promise<void>(resolve=>events.end(resolve));await new Promise<void>(resolve=>requestLog.end(resolve));
 }
 process.exit(process.exitCode||0);
}
main().catch(error=>{console.error(error);process.exit(1);});
