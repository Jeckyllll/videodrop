const {test}=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'), vm=require('node:vm');
const {parseHTML}=require('../.state/test-deps/node_modules/linkedom');

test('Lite background loads no Studio driver and only reports its own edition',async()=>{
  const imports=[],requests=[];let listener;
  const context={console,Map,URL,AbortSignal,fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true};},
    chrome:{runtime:{id:'a'.repeat(32),getManifest:()=>({version:'1.5.0'}),onMessage:{addListener(callback){listener=callback;}}},
      webRequest:{onHeadersReceived:{addListener(){}}},tabs:{onRemoved:{addListener(){}},onUpdated:{addListener(){}}}}};
  vm.createContext(context);
  context.importScripts=(name)=>{
    imports.push(name);
    if(name==='edition.js')vm.runInContext("const VIDEODROP_EDITION='lite';",context);
    else if(name==='config.js')vm.runInContext("const VIDEODROP={base:'http://127.0.0.1:8766',token:'LOCAL'};",context);
    else throw Error('Unexpected upload script: '+name);
  };
  vm.runInContext(fs.readFileSync('extension/background.js','utf8'),context);
  await new Promise(setImmediate);
  assert.deepEqual(imports,['edition.js','config.js']);
  assert.equal(requests.length,1);
  assert.equal(requests[0].url,'http://127.0.0.1:8766/api/extension/heartbeat');
  assert.equal(requests[0].body.edition,'lite');
  let reply;listener({type:'extension-ping'},{id:context.chrome.runtime.id},value=>{reply=value;});
  assert.equal(reply.ok,true);assert.equal(requests.length,2);
  listener({type:'extension-ping'},{id:'other-extension'},()=>{throw Error('Unexpected reply');});
  assert.equal(requests.length,2);
});

test('Lite popup selects a video without upload controls, stored cloud settings or Studio calls',async()=>{
  const {document}=parseHTML(fs.readFileSync('extension/popup.html','utf8').replace('<div id="youtube-settings"></div>',''));
  const requests=[],tabs=[],messages=[];
  const context={console,document,URL,Map,navigator:{userAgent:'test'},window:{close(){}},
    VIDEODROP_EDITION:'lite',VIDEODROP:{base:'http://127.0.0.1:8766',token:'LOCAL'},
    fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({openUrl:'http://127.0.0.1:8766/#job=test'})};},
    chrome:{runtime:{sendMessage:async message=>{messages.push(message.type);return {ok:true};}},
      tabs:{query:async()=>[{id:1,url:'https://lesson.example/video',title:'Lesson'}],create:async value=>tabs.push(value)},
      scripting:{executeScript:async()=>[{result:[{url:'https://media.example/video.mp4',label:'Видео'}]}]},
      storage:{session:{get:async()=>({})},local:{get:()=>{throw Error('Must not read cloud settings');}}}}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('extension/popup.js','utf8'),context);
  await new Promise(setImmediate);
  const button=document.querySelector('#items button');assert.ok(button);
  await button.onclick();
  assert.deepEqual(messages,['extension-ping']);
  assert.equal(requests.length,1);assert.equal(requests[0].url,'http://127.0.0.1:8766/api/import');
  assert.equal(requests[0].body.storage.enabled,false);assert.equal(requests[0].body.storage.deleteLocal,false);
  assert.equal(tabs.length,1);assert.equal(tabs[0].url,'http://127.0.0.1:8766/#job=test');
  assert.equal(document.querySelector('#youtube-settings'),null);
});
