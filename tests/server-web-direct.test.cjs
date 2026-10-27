const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
test('direct download uses the browser bridge without creating a server job',async()=>{
  class Element {
    constructor(){this.children=[];this.hidden=false;this.value='';this.checked=false;this.disabled=false;this.className='';this.textContent='';}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=items;}
    closest(){return this;}
    get options(){return this.children;}
    click(){return this.onclick?.();}
  }
  const nodes=new Map(),messages=[],requests=[],listeners={};
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const parent={postMessage:(data,origin)=>messages.push({data,origin})};
  const ext='a'.repeat(32),origin='chrome-extension://'+ext;
  const window={parent,addEventListener:(type,fn)=>listeners[type]=fn};
  const context={window,document:{getElementById:$,createElement:()=>new Element(),body:{classList:{add(){}}}},
    location:{hash:'#extension='+ext,origin:'https://video.example'},URL,URLSearchParams,
    navigator:{userAgent:'test'},crypto:{randomUUID:()=> 'test-request'},AbortSignal,
    setTimeout,clearTimeout,localStorage:{getItem:()=>null},
    fetch:async(path,options)=>{requests.push({path,method:options.method});return {ok:true,json:async()=>path==='/api/session'?{name:'Test',role:'owner',youtube:false,version:'test'}:{jobs:[]}};}};
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../server/web/app.js'),'utf8'),context);
  await listeners.message({origin,source:parent,data:{type:'context',token:'test',features:{directDownload:true},sources:[{label:'Видеофайл',url:'https://cdn.example/clip.mp4'}]}});
  await new Promise(resolve=>setImmediate(resolve));
  const card=$('source-list').children[0],localButton=card.children[1].children[0];
  localButton.click();
  await new Promise(resolve=>setImmediate(resolve));
  const local=messages.find(m=>m.data.type==='download-local');
  assert.equal(local.data.url,'https://cdn.example/clip.mp4');
  await listeners.message({origin,source:parent,data:{type:'download-local-result',requestId:'test-request'}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.some(r=>r.path==='/api/jobs'&&r.method==='POST'),false);
  assert.match($('status').textContent,/Загрузка запущена/);
});
