const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('../.state/test-deps/node_modules/linkedom');
const CHANNEL='UC'+'a'.repeat(22),VIDEO='testVideo01';
const source=fs.readFileSync('extension/studio-page.js','utf8');
function page(markup,origin='https://studio.youtube.com'){
  const {window,document}=parseHTML('<html><body><a id="home-button" href="/channel/'+CHANNEL+'"></a>'+markup+'</body></html>');
  window.HTMLElement.prototype.getClientRects=function(){return this.closest('[hidden]')||this.style.display==='none'?[]:[{}];};
  const context={document,location:{origin,pathname:'/video/'+VIDEO+'/edit'},getComputedStyle:e=>({visibility:e.style.visibility||'visible'}),InputEvent:window.Event,Event:window.Event};
  vm.createContext(context);vm.runInContext(source,context);
  return {document,run:(command='snapshot',data={})=>context.videoDropStudioPage(command,{channelId:CHANNEL,title:'Урок',madeForKids:false,...data})};
}
const editor=`<ytcp-button id="save" disabled><button disabled>Сохранить</button></ytcp-button>
<div id="original-filename">Урок.mp4</div><div id="visibility-text">По ссылке</div>
<div id="video-resolutions"><span id="badge-sd" role="img" aria-label="Обработка версии в стандартном качестве завершена"></span><span id="badge-hd" role="img" aria-label="Обработка версии в высоком разрешении завершена"></span><span id="badge-4k" role="img" hidden aria-label="Обработка версии 4K не завершена"></span></div>
<a href="https://youtu.be/${VIDEO}">Видео</a>`;
function wizard({selected='PRIVATE',complete=false}={}){return `<ytcp-uploads-dialog><ytcp-uploads-file-picker><input type="file"></ytcp-uploads-file-picker><ytcp-video-upload-progress>${complete?'Загрузка завершена':'Загрузка: 10%'}</ytcp-video-upload-progress><ytcp-button id="done-button"><button>Сохранить</button></ytcp-button>${['PRIVATE','UNLISTED','PUBLIC'].map(n=>`<tp-yt-paper-radio-button role="radio" name="${n}" aria-checked="${n===selected}">${n}</tp-yt-paper-radio-button>`).join('')}<a href="https://youtu.be/${VIDEO}">Ссылка</a></ytcp-uploads-dialog>`;}
test('reads saved unlisted video and ignores hidden quality badges',()=>{
  const s=page(editor).run();assert.equal(s.channelId,CHANNEL);assert.equal(s.videoId,VIDEO);assert.equal(s.filename,'Урок.mp4');assert.equal(s.privacy,'unlisted');assert.equal(s.saved,true);assert.equal(s.processed,true);
});
test('no success on pending processing, absent evidence or unsaved changes',()=>{
  assert.equal(page(editor.replace('Обработка версии в высоком разрешении завершена','Обработка версии в высоком разрешении')).run().processed,false);
  assert.equal(page(editor.replace(/ disabled/g,'')).run().saved,false);
  assert.equal(page(editor.replace(/<div id="video-resolutions">[\s\S]*?<\/div>/,'')).run().processed,false);
  assert.equal(page(editor+wizard()).run().processed,false);
});
test('never save with private, public or ambiguous visibility',()=>{
  for(const selected of ['PRIVATE','PUBLIC']){
    const p=page(wizard({selected}));let clicked=false;p.document.querySelector('#done-button button').onclick=()=>clicked=true;
    assert.ok(p.run('save').blocked);assert.equal(clicked,false);
  }
  const p=page(wizard({selected:'UNLISTED'}).replace('name="PUBLIC" aria-checked="false"','name="PUBLIC" aria-checked="true"'));
  assert.ok(p.run('save').blocked);
});
test('save unlisted only and correctly expose completed transfer',()=>{
  const p=page(wizard({selected:'UNLISTED',complete:true}));let clicked=false;p.document.querySelector('#done-button button').onclick=()=>clicked=true;
  assert.equal(p.run().uploadComplete,true);assert.equal(p.run('save').clicked,true);assert.equal(clicked,true);
  assert.equal(page(wizard()).run().uploadComplete,false);
});
test('other origin or switched channel prevents UI actions',()=>{
  assert.ok(page(editor,'https://accounts.google.com').run('save').blocked);
  assert.ok(page(editor).run('save',{channelId:'UC'+'b'.repeat(22)}).blocked);
});
test('title is inserted as text, audience is explicit',()=>{
  const p=page(`<ytcp-uploads-dialog><div contenteditable="true" role="textbox" aria-label="Укажите название" id="textbox"></div><tp-yt-paper-radio-button role="radio" name="VIDEO_MADE_FOR_KIDS_NOT_MFK" aria-checked="false">Нет</tp-yt-paper-radio-button></ytcp-uploads-dialog>`);
  p.document.querySelector('[role=radio]').onclick=function(){this.setAttribute('aria-checked','true');};
  assert.equal(p.run('details',{title:'<script>not code</script>'}).clicked,true);
  assert.equal(p.document.querySelector('#textbox').textContent,'<script>not code</script>');
  assert.equal(p.document.querySelector('script'),null);
  assert.equal(p.run('snapshot',{title:'<script>not code</script>'}).detailsSet,true);
});
