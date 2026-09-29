const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('../.state/test-deps/node_modules/linkedom');

for(const edition of ['full','lite']){
  for(const unavailable of [1,Infinity]){
    test(`${edition}: ${unavailable===1?'worker wakes on retry':'missing worker shows recovery hint'} without breaking downloads`,async()=>{
      const {document}=parseHTML(fs.readFileSync('extension/popup.html','utf8'));
      const messages=[],requests=[],tabs=[];let wake;
      const context={console,document,URL,Map,navigator:{userAgent:'test'},window:{close(){}},
        setTimeout:callback=>setImmediate(callback),VIDEODROP_EDITION:edition,
        VIDEODROP:{base:'http://127.0.0.1:8765',token:'LOCAL'},
        VideoDropYouTube:class {
          constructor(root,options){wake=options.wake;}
          async init(){return this;}
          async options(){return {enabled:false,deleteLocal:false};}
        },
        fetch:async(url,options)=>{requests.push(url);return {ok:true,json:async()=>({openUrl:'http://127.0.0.1:8765/#job=test'})};},
        chrome:{runtime:{sendMessage:async message=>{
          messages.push(message.type);
          if(messages.length<=unavailable)throw Error('Could not establish connection. Receiving end does not exist.');
          return {ok:true};
        }},tabs:{query:async()=>[{id:1,url:'https://lesson.example/video',title:'Lesson'}],create:async value=>tabs.push(value)},
          scripting:{executeScript:async()=>[{result:[{url:'https://media.example/video.mp4',label:'Видео'}]}]},
          storage:{session:{get:async()=>({})}}}};
      vm.createContext(context);vm.runInContext(fs.readFileSync('extension/popup.js','utf8'),context);
      // Complete both the retry delay and popup initialization.
      await new Promise(setImmediate);await new Promise(setImmediate);
      assert.deepEqual(messages,Array(2).fill(edition==='full'?'studio-pump':'extension-ping'));
      const button=document.querySelector('#items button');assert.ok(button);
      const status=document.querySelector('#status').textContent;
      if(unavailable===Infinity)assert.match(status,/Фоновая часть VideoDrop не ответила/);
      else assert.equal(status,'');
      await button.onclick();assert.equal(requests.length,1);assert.equal(tabs.length,1);
      if(wake){
        await wake();
        assert.equal(messages.length,unavailable===Infinity?4:3);
      }
    });
  }
}
