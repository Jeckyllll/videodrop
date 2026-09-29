const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('../.state/test-deps/node_modules/linkedom');

function fixture(initial={}){
  const {document}=parseHTML('<div id="settings"></div>');
  let status={ready:false,connected:false,channelId:'',connecting:false,connectError:'',...initial};
  const timers=new Map(),requests=[],saved=[];let nextTimer=0;
  const context={window:{},setInterval:callback=>{timers.set(++nextTimer,callback);return nextTimer;},clearInterval:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(fs.readFileSync('extension/youtube-ui.js','utf8'),context);
  const ui=new context.window.VideoDropYouTube(document.querySelector('#settings'),{
    request:async(path)=>{
      requests.push(path);
      if(path==='studio/connect')status={...status,connecting:true,connectError:''};
      if(path==='studio/cancel-connect')status={...status,connecting:false,connectError:''};
      return {...status};
    },
    load:async()=>({provider:'studio',enabled:true,channelId:status.channelId}),
    save:value=>saved.push(value),wake:()=>Promise.reject(Error('Extension unavailable')),
  });
  // linkedom has no select.value setter, unlike the browser DOM.
  Object.defineProperty(ui.get('audience'),'value',{writable:true,value:'no'});
  return {ui,timers,requests,saved,setStatus:value=>{status={...status,...value};}};
}

test('reopening a pending connection keeps polling and unlocks after Chrome timeout',async()=>{
  const f=fixture({connecting:true});await f.ui.init();
  assert.equal(f.ui.get('connect').disabled,true);assert.equal(f.ui.get('cancel').hidden,false);
  assert.equal(f.timers.size,1);
  f.setStatus({connecting:false,connectError:'Расширение Chrome не ответило за 45 секунд.'});
  await [...f.timers.values()][0]();
  assert.equal(f.ui.get('connect').disabled,false);assert.equal(f.ui.get('cancel').hidden,true);
  assert.equal(f.timers.size,0);assert.match(f.ui.get('message').textContent,/45 секунд/);
});

test('cancel is available and preserves the existing channel',async()=>{
  const f=fixture({connected:true,channelId:'existing-channel',channelTitle:'Existing'});await f.ui.init();
  await f.ui.get('connect').onclick();
  assert.equal(f.ui.get('cancel').hidden,false);
  await f.ui.get('cancel').onclick();
  assert.equal(f.ui.account.channelId,'existing-channel');assert.equal(f.ui.get('connect').disabled,false);
  assert.equal(f.timers.size,0);assert.match(f.ui.get('message').textContent,/отменено/);
  assert.ok(f.requests.includes('studio/cancel-connect'));assert.ok(!f.requests.includes('studio/disconnect'));
});

test('turning YouTube off cancels pending connection and keeps downloads local',async()=>{
  const f=fixture({connecting:true});await f.ui.init();
  f.ui.get('enabled').checked=false;await f.ui.get('enabled').onchange();
  assert.equal(f.ui.account.connecting,false);assert.equal(f.timers.size,0);
  const options=await f.ui.options();assert.equal(options.enabled,false);assert.equal(options.deleteLocal,false);
});

test('failed channel switch reports failure instead of claiming success for the old channel',async()=>{
  const f=fixture({connected:true,channelId:'old-channel',connecting:true});await f.ui.init();
  f.setStatus({connecting:false,connectError:'Проверьте разрешение «Отладчик».'});await f.ui.refresh();
  assert.match(f.ui.get('message').textContent,/Отладчик/);assert.equal(f.ui.account.channelId,'old-channel');
});

test('late status response cannot resurrect a cancelled connection',async()=>{
  const f=fixture({connecting:true});await f.ui.init();
  const request=f.ui.request;let resolve;
  f.ui.request=path=>path==='studio/status'?new Promise(done=>{resolve=done;}):request(path);
  const refresh=f.ui.refresh();await f.ui.cancel();
  resolve({connecting:true,channelId:'',connected:false});await refresh;
  assert.equal(f.ui.account.connecting,false);assert.equal(f.ui.get('connect').disabled,false);assert.equal(f.timers.size,0);
});
