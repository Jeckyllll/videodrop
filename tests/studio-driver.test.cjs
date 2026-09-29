const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const CHANNEL='UC'+'a'.repeat(22),VIDEO='testVideo01';
function fixture({existing=false,privacy='unlisted',cancel=false}={}){
  let phase=existing?'editor':'home',detailsSet=false,unlisted=false,visibilityReads=0,complete=false,clock=0;
  let connectionCancelled=cancel;
  const events=[],commands=[],tabs=[],closed=[];
  const job={id:'job',lease:'LOCAL-LEASE-SECRET',path:'/Downloads/video.mp4',filename:'video.mp4',title:'A lesson',channelId:CHANNEL,madeForKids:false,videoId:existing?VIDEO:'',submitted:existing};
  const context={console,URL,AbortSignal,VIDEODROP:{base:'http://127.0.0.1:8765',token:'LOCAL-TOKEN'},Date:{now:()=>clock+=1000},
    setTimeout:callback=>setImmediate(callback),importScripts:()=>{},videoDropStudioPage:function adapter(){},
    fetch:async(url,options)=>{
      const data=JSON.parse(options.body);if(url.endsWith('/event'))events.push(data);
      if(url.endsWith('/connect-status'))return {ok:true,json:async()=>({cancelled:connectionCancelled})};
      return {ok:true,json:async()=>cancel&&data.event==='progress'?{cancelled:true}:{}};
    },chrome:{runtime:{id:'a'.repeat(32),getManifest:()=>({version:'1.4.0'}),onMessage:{addListener(){}},onMessageExternal:{addListener(){}}},
      alarms:{onAlarm:{addListener(){}},create:()=>{}},storage:{session:{set:async()=>{},remove:async()=>{}}},
      tabs:{create:async data=>{tabs.push(data);return {id:42};},remove:async id=>closed.push(id)},
      debugger:{attach:async()=>{},detach:async()=>{},sendCommand:async(target,method,data)=>{
        commands.push({method,data});
        if(method==='DOM.setFileInputFiles'){
          assert.ok(events.some(e=>e.event==='submitted'),'must persist submission before sending a file');
          phase='details';return {};
        }
        if(method==='Page.navigate'){
          assert.equal(complete,true,'must finish transfer before navigating away');
          assert.equal(phase,'saved');phase='editor';return {};
        }
        if(data.expression.startsWith('document.querySelector'))return {result:{objectId:'file-input'}};
        assert.ok(!data.expression.includes(job.lease));assert.ok(!data.expression.includes(job.path));assert.ok(!data.expression.includes('LOCAL-TOKEN'));
        const command=data.expression.match(/\)\("([^"]+)",/)[1];
        if(command!=='snapshot'){
          if(command==='create')phase='menu';
          if(command==='upload-menu')phase='file';
          if(command==='details')detailsSet=true;
          if(command==='next')phase=({details:'elements',elements:'checks',checks:'visibility'})[phase];
          if(command==='unlisted')unlisted=true;
          if(command==='save'){assert.equal(unlisted,true);assert.equal(complete,true);phase='saved';}
          return {result:{value:{clicked:true}}};
        }
        if(phase==='visibility'&&unlisted)complete=++visibilityReads>=3;
        return {result:{value:{channelId:CHANNEL,channelTitle:'Channel',dialog:!['home','menu','editor','saved'].includes(phase),
          fileInput:phase==='file',createVisible:phase==='home',uploadMenu:phase==='menu',
          videoId:['details','elements','checks','visibility','editor'].includes(phase)?VIDEO:'',
          detailsReady:phase==='details',detailsSet,nextEnabled:['details','elements','checks'].includes(phase),
          unlistedVisible:phase==='visibility',unlisted,doneEnabled:phase==='visibility',uploadComplete:complete,
          editorId:phase==='editor'?VIDEO:'',filename:phase==='editor'?'video.mp4':'',privacy:phase==='editor'?privacy:'',
          processed:phase==='editor',saved:phase==='editor',url:'https://studio.youtube.com/video/'+VIDEO+'/edit'}}};
      }}}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('extension/studio-driver.js','utf8'),context);
  const Run=vm.runInContext('StudioRun',context);
  return {job,events,commands,tabs,closed,run:()=>new Run(job),connect:()=>new Run(null,'CONNECT-NONCE'),cancelConnection:()=>{connectionCancelled=true;}};
}
test('complete upload: own tab, direct file handoff, unlisted, transfer before navigation, verified receipt',async()=>{
  const f=fixture(),run=f.run();await run.upload();await run.finish();
  assert.equal(f.tabs.length,1);assert.equal(f.tabs[0].url,'https://studio.youtube.com/');
  assert.deepEqual(Array.from(f.commands.find(c=>c.method==='DOM.setFileInputFiles').data.files),[f.job.path]);
  const receipt=f.events.find(e=>e.event==='complete');assert.equal(receipt.proof.privacy,'unlisted');assert.equal(receipt.proof.videoId,VIDEO);
  assert.equal(receipt.proof.processed,true);assert.equal(receipt.proof.saved,true);assert.equal(f.closed.length,0);
});
test('retry verifies known video without reassigning the file',async()=>{
  const f=fixture({existing:true});await f.run().upload();
  assert.equal(f.tabs[0].url,'https://studio.youtube.com/video/'+VIDEO+'/edit');
  assert.equal(f.commands.some(c=>c.method==='DOM.setFileInputFiles'),false);
  assert.equal(f.events.some(e=>e.event==='submitted'),false);assert.equal(f.events.some(e=>e.event==='complete'),true);
});
test('private result never emits completion or deletion authorization',async()=>{
  const f=fixture({existing:true,privacy:'private'});await assert.rejects(()=>f.run().upload(),/По ссылке/);
  assert.equal(f.events.some(e=>e.event==='complete'),false);
});
test('cancellation stops automation and closes only the tab it created',async()=>{
  const f=fixture({cancel:true}),run=f.run();await assert.rejects(()=>run.upload(),/отменена/);await run.finish();
  assert.deepEqual(f.closed,[42]);assert.equal(f.commands.some(c=>c.method==='DOM.setFileInputFiles'),false);
});
test('cancelled connection cannot open a late Studio tab',async()=>{
  const f=fixture({cancel:true}),run=f.connect();
  await assert.rejects(()=>run.start('https://studio.youtube.com/'),/Подключение отменено/);
  await run.finish();assert.equal(f.tabs.length,0);assert.equal(f.closed.length,0);
});
test('cancelling while waiting for login closes only the connection tab',async()=>{
  const f=fixture(),run=f.connect();await run.start('https://studio.youtube.com/');
  f.cancelConnection();run.lastPulse=0;
  await assert.rejects(()=>run.wait(s=>s.channelId,'Вход в YouTube'),/Подключение отменено/);
  await run.finish();assert.deepEqual(f.closed,[42]);assert.equal(f.events.length,0);
});
