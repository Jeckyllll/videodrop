const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const {webcrypto,randomUUID}=require('node:crypto');
const path=require('node:path');

function setup(){
  const listeners={},store={serverToken:'account-one'},downloads=new Map(),requests=[];
  let online=true;
  const storage={
    async get(key){if(key===null)return structuredClone(store);return {[key]:structuredClone(store[key])};},
    async set(values){Object.assign(store,structuredClone(values));},
    async remove(key){delete store[key];}
  };
  const chrome={
    storage:{local:storage,session:{get:async()=>({}),set:async()=>{},remove:async()=>{}}},
    runtime:{id:'a'.repeat(32),getURL:path=>'chrome-extension://'+('a'.repeat(32))+'/'+path,
      onMessage:{addListener:fn=>listeners.message=fn},onStartup:{addListener:fn=>listeners.start=fn}},
    downloads:{onChanged:{addListener:fn=>listeners.changed=fn},search:async({id})=>downloads.has(id)?[{id,state:downloads.get(id)}]:[]},
    webRequest:{onHeadersReceived:{addListener(){}}},tabs:{onRemoved:{addListener(){}},onUpdated:{addListener(){}}},
    action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}}
  };
  const context={chrome,crypto:{subtle:webcrypto.subtle,randomUUID},TextEncoder,Uint8Array,URL,URLSearchParams,AbortSignal,
    setTimeout,clearTimeout,fetch:async(url,options)=>{if(!online)throw Error('offline');requests.push({url,body:JSON.parse(options.body),auth:options.headers.Authorization});return {ok:true,status:200};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../extension-server/background.js'),'utf8'),context);
  const send=(message,sender={id:chrome.runtime.id,url:chrome.runtime.getURL('popup.html')})=>new Promise(resolve=>{
    const accepted=listeners.message(message,sender,resolve);if(!accepted)resolve(undefined);
  });
  return {store,downloads,requests,send,online:value=>online=value,listeners};
}
const tick=()=>new Promise(resolve=>setTimeout(resolve,20));
test('local journal records only successful Chrome downloads and strips private URL values',async()=>{
  const s=setup();s.downloads.set(7,'in_progress');
  await s.send({type:'track-local-download',downloadId:7,page:'https://edu.example/lesson?id=15&token=secret#section',title:'Урок'});
  assert.equal(s.requests.length,0);
  s.downloads.set(7,'complete');s.listeners.changed({id:7,state:{current:'complete'}});await tick();
  assert.equal(s.requests.length,1);
  assert.equal(s.requests[0].body.url,'https://edu.example/lesson?id=15');
  assert.equal(s.requests[0].body.title,'Урок');
  assert.doesNotMatch(JSON.stringify(s.requests[0].body),/secret|account-one/);
  assert.equal(Object.keys(s.store).filter(key=>key.startsWith('local-history:')).length,0);
  s.downloads.set(8,'interrupted');
  await s.send({type:'track-local-download',downloadId:8,page:'https://edu.example/lesson',title:'Отмена'});
  assert.equal(s.requests.length,1);
});
test('completed HLS record retries offline without assigning to a different account',async()=>{
  const s=setup(),id='1'.repeat(32);
  await s.send({type:'prepare-local-download',eventId:id,page:'https://edu.example/lesson',title:'Лекция'});
  s.online(false);await s.send({type:'record-local-download',eventId:id,height:720});
  assert.equal(s.store['local-history:'+id].ready,true);
  s.online(true);await s.send({type:'retry-local-history'});
  assert.equal(s.requests.length,1);assert.equal(s.requests[0].body.height,720);
  assert.equal(s.requests[0].body.eventId,id);
  const next='2'.repeat(32);
  await s.send({type:'prepare-local-download',eventId:next,page:'https://edu.example/lesson',title:'Лекция'});
  s.online(false);await s.send({type:'record-local-download',eventId:next,height:720});
  s.store.serverToken='account-two';s.online(true);await s.send({type:'retry-local-history'});
  assert.equal(s.requests.length,1);assert.equal(s.store['local-history:'+next],undefined);
});
test('messages from outside the extension are ignored',async()=>{
  const s=setup();
  await s.send({type:'prepare-local-download',eventId:'1'.repeat(32),page:'https://edu.example/lesson',title:'X'}, {id:'evil',url:'https://evil.example/'});
  assert.equal(Object.keys(s.store).length,1);
});
