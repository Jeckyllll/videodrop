const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const bridge=require('../extension-server/bridge.js');
test('remote message must come from the exact server and the embedded frame',()=>{
  const child={},frame={contentWindow:child};
  assert.equal(bridge.accepts({origin:bridge.base,source:child},frame),true);
  assert.equal(bridge.accepts({origin:bridge.base+'.evil.test',source:child},frame),false);
  assert.equal(bridge.accepts({origin:bridge.base,source:{}},frame),false);
});
test('remote UI cannot request arbitrary URLs or unrelated cookies',()=>{
  const sources=[{url:'https://edu.example/lesson'}];
  assert.equal(bridge.findSource(sources,'https://bank.example/'),undefined);
  assert.equal(bridge.findSource(sources,sources[0].url),sources[0]);
  const cookie=(domain,hostOnly=false)=>({domain,hostOnly,name:'session',path:'/',value:'private'});
  const values=bridge.scopedCookies([cookie('.example'),cookie('edu.example',true),cookie('bank.example',true),cookie('.example.evil.test'),cookie('edu.example',true)],['https://edu.example/lesson']);
  assert.deepEqual(values.map(c=>c.domain),['.example','edu.example']);
});
test('new package has no local host, debugger, Studio, shared secret or remote script execution',()=>{
  const root=path.join(__dirname,'../extension-server'),manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json')));
  assert.equal(manifest.permissions.includes('debugger'),false);
  assert.equal(manifest.permissions.includes('nativeMessaging'),false);
  for(const name of fs.readdirSync(root).filter(name=>/\.(js|json|html)$/.test(name))){
    const source=fs.readFileSync(path.join(root,name),'utf8');
    assert.doesNotMatch(source,/127\.0\.0\.1|localhost|studio-driver|VIDEODROP\.token|\beval\(|new Function\(/);
  }
  assert.match(manifest.content_security_policy.extension_pages,/script-src 'self'/);
});
test('site access defaults on, respects opt-out, and sends only cookies for the selected page',async()=>{
  const vm=require('node:vm');
  for(const preference of [undefined,false]){
    const listeners={},sent=[],saved=[];
    const fields=Object.fromEntries(['app','access','capture','status','expand','refresh'].map(id=>[id,{checked:false,disabled:false,hidden:false,textContent:'',contentWindow:{postMessage:value=>sent.push(value)}}]));
    const chrome={runtime:{id:'a'.repeat(32)},storage:{local:{setAccessLevel:async()=>{},get:async key=>key==='accessEnabled'?{accessEnabled:preference}:{},set:async obj=>saved.push(obj)},session:{get:async()=>({})}},
      tabs:{query:async()=>[{id:1,url:'https://edu.example/lesson'}],get:async()=>({url:'https://edu.example/lesson'})},
      scripting:{executeScript:async()=>[]},cookies:{getAllCookieStores:async()=>[{id:'1',tabIds:[1]}],getAll:async()=>[{domain:'edu.example',hostOnly:true,name:'session',path:'/',value:'s'},{domain:'other.example',hostOnly:true,name:'other',path:'/',value:'x'}]}};
    const context={document:{getElementById:id=>fields[id],body:{classList:{add(){}}}},window:{addEventListener:(type,fn)=>listeners[type]=fn},location:{search:''},navigator:{userAgent:'test'},chrome,URL,URLSearchParams,VideoDropBridge:bridge};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../extension-server/popup.js'),'utf8'),context);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(fields.access.checked,preference!==false);
    await listeners.message({origin:bridge.base,source:fields.app.contentWindow,data:{type:'source',requestId:'1',url:'https://edu.example/lesson'}});
    const result=sent.find(message=>message.type==='source-result');
    assert.equal(result.source.cookies.length,preference===false?0:1);
    if(preference!==false) assert.equal(result.source.cookies[0].domain,'edu.example');
  }
});
test('context waits for the server frame regardless of which side becomes ready first',async()=>{
  const vm=require('node:vm');
  for(const frameFirst of [false,true]){
    const listeners={},sent=[];
    let finishInit;
    const initGate=new Promise(resolve=>{finishInit=resolve;});
    const fields=Object.fromEntries(['app','access','capture','status','expand','refresh'].map(id=>[id,{checked:false,contentWindow:{postMessage:(value,target)=>sent.push({value,target})}}]));
    const chrome={runtime:{id:'a'.repeat(32)},storage:{local:{setAccessLevel:()=>initGate,get:async()=>({}),set:async()=>{}},session:{get:async()=>({})}},tabs:{query:async()=>[{id:1,url:'https://edu.example/lesson'}]},scripting:{executeScript:async()=>[]}};
    const context={document:{getElementById:id=>fields[id],body:{classList:{add(){}}}},window:{addEventListener:(type,fn)=>listeners[type]=fn},location:{search:''},navigator:{userAgent:'test'},chrome,URL,URLSearchParams,VideoDropBridge:bridge};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../extension-server/popup.js'),'utf8'),context);
    const ready=()=>listeners.message({origin:bridge.base,source:fields.app.contentWindow,data:{type:'ready'}});
    if(frameFirst){await ready();assert.equal(sent.length,0);finishInit();await new Promise(resolve=>setImmediate(resolve));}
    else{finishInit();await new Promise(resolve=>setImmediate(resolve));assert.equal(sent.length,0);await ready();}
    assert.equal(sent.length,1);
    assert.equal(sent[0].value.type,'context');
    assert.equal(sent[0].target,bridge.base);
    fields.refresh.onclick();
    assert.equal(sent.length,1);
    await ready();
    assert.equal(sent.length,2);
  }
});
test('direct download is limited to discovered complete-file URLs in the current tab',async()=>{
  const vm=require('node:vm'),listeners={},sent=[],downloads=[];
  const fields=Object.fromEntries(['app','access','capture','status','expand','refresh'].map(id=>[id,{checked:false,contentWindow:{postMessage:value=>sent.push(value)}}]));
  let tabUrl='https://edu.example/lesson';
  const chrome={runtime:{id:'a'.repeat(32)},storage:{local:{setAccessLevel:async()=>{},get:async()=>({}),set:async()=>{}},session:{get:async()=>({})}},
    tabs:{query:async()=>[{id:1,url:tabUrl}],get:async()=>({url:tabUrl})},scripting:{executeScript:async()=>[{result:[{url:'https://cdn.example/video.mp4',label:'Видеофайл'},{url:'https://cdn.example/live.m3u8',label:'Поток видео'}]}]},
    downloads:{download:async options=>{downloads.push(options);return 42;}}};
  const context={document:{getElementById:id=>fields[id],body:{classList:{add(){}}}},window:{addEventListener:(type,fn)=>listeners[type]=fn},location:{search:''},navigator:{userAgent:'test'},chrome,URL,URLSearchParams,VideoDropBridge:bridge};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../extension-server/popup.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  async function request(url,origin=bridge.base){await listeners.message({origin,source:fields.app.contentWindow,data:{type:'download-local',requestId:url,url}});return sent.at(-1);}
  await request('https://cdn.example/video.mp4');assert.equal(downloads.length,1);assert.equal(downloads[0].url,'https://cdn.example/video.mp4');assert.equal(sent.at(-1).error,undefined);
  await request('https://cdn.example/live.m3u8');assert.ok(sent.at(-1).error);assert.equal(downloads.length,1);
  await request('https://unknown.example/movie.mp4');assert.ok(sent.at(-1).error);assert.equal(downloads.length,1);
  tabUrl='https://other.example/';await request('https://cdn.example/video.mp4');assert.ok(sent.at(-1).error);assert.equal(downloads.length,1);
  assert.equal(bridge.directMedia('https://cdn.example/video.mp4?range=0-1'),false);
  assert.equal(bridge.directMedia('javascript:alert(1)'),false);
});
