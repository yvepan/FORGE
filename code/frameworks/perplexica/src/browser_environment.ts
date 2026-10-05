import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import {chromium} from 'playwright';

// This is an environment adapter, not a replacement research/scrape tool.
// Native Chromium launch/newContext arguments and original browser URLs survive.
export function installWikiBrowserEnvironment(pageEndpoint:string,output:string){
 const browsers:any[]=[];
 const originalLaunch=chromium.launch.bind(chromium);
 const originalFetch=globalThis.fetch;
 const executablePath=process.env.FORGE_CHROMIUM_EXECUTABLE;
 let pageCount=0;
 fs.mkdirSync(path.join(output,'pages'),{recursive:true});
 const log=(row:any)=>fs.appendFileSync(path.join(output,'browser_requests.jsonl'),JSON.stringify(row)+'\n');
 (chromium as any).launch=async(options:any)=>{
   const browser=await originalLaunch(executablePath?{...options,executablePath}:options);browsers.push(browser);
   const originalContext=browser.newContext.bind(browser);
   (browser as any).newContext=async(contextOptions:any)=>{
     const context=await originalContext(contextOptions);
     await context.route('**/*',async(route:any)=>{
       const request=route.request(),address=request.url();
       const started=Date.now()/1000;
       const u=new URL(address);
       if(!['en.wikipedia.org','en.m.wikipedia.org'].includes(u.hostname)){
         log({started_unix:started,url:address,method:request.method(),resource_type:request.resourceType(),status:404,source:'unavailable_in_wikipedia_environment'});
         await route.fulfill({status:404,contentType:'text/html; charset=utf-8',body:'<!doctype html><html><head><title>Not Found</title></head><body>Not Found</body></html>'});return;
       }
       const endpoint=new URL(pageEndpoint);endpoint.searchParams.set('url',address);endpoint.searchParams.set('framework','perplexica');
       try{
         const response=await originalFetch(endpoint);
         const bytes=Buffer.from(await response.arrayBuffer());
         const hash=crypto.createHash('sha256').update(bytes).digest('hex');
         const filename=`${String(++pageCount).padStart(4,'0')}_${crypto.createHash('sha256').update(address).digest('hex').slice(0,12)}.html`;
         fs.writeFileSync(path.join(output,'pages',filename),bytes);
         log({started_unix:started,elapsed_seconds:Date.now()/1000-started,url:address,method:request.method(),resource_type:request.resourceType(),status:response.status,source:'local_wikipedia_snapshot',backend_url:endpoint.href,content_sha256:hash,bytes:bytes.length,offline_file:path.join(output,'pages',filename)});
         await route.fulfill({status:response.status,contentType:response.headers.get('content-type')||'text/html; charset=utf-8',body:bytes});
       }catch(error){
         log({started_unix:started,url:address,status:502,error:String(error),source:'local_backend_transport_error'});
         await route.fulfill({status:502,contentType:'text/html; charset=utf-8',body:'<!doctype html><html><head><title>Bad Gateway</title></head><body>Bad Gateway</body></html>'});
       }
     });
     return context;
   };
   return browser;
 };
 return {async close(){await Promise.all(browsers.map(b=>b.close().catch(()=>undefined)));}};
}
