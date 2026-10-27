'use strict';
const $=id=>document.getElementById(id);
const extensionId=new URLSearchParams(location.hash.slice(1)).get('extension');
const parentOrigin=window.parent!==window && /^[a-p]{32}$/.test(extensionId||'') ? 'chrome-extension://'+extensionId : null;
if(parentOrigin)document.body.classList.add('embedded');
let token='',session=null,current=null,timer=null,historyTimer=null,bridgePending=null,showAudit=false,busy=false,viewSequence=0,contextReceived=false;
const labels={queued:'В очереди…',inspecting:'Проверяю ссылку…',choice:'Выберите качество и способ получения',downloading:'Скачиваю на сервер…',uploading:'Загружаю на YouTube…',delivering:'Готовлю результат…',done:'Готово',cancelled:'Отменено',expired:'Срок хранения истёк',error:'Не удалось скачать',review:'Нужна проверка результата'};
const active=new Set(['queued','inspecting','downloading','uploading','delivering']);
function status(text=''){$('status').textContent=text;}
function parentMessage(type,data={}){if(parentOrigin)window.parent.postMessage({type,...data},parentOrigin);}
function saveToken(value){token=value;if(parentOrigin)parentMessage('save-token',{token:value});else{if(value)localStorage.setItem('videodrop-token',value);else localStorage.removeItem('videodrop-token');}}
async function api(path,body){
  let response;
  try{response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(20000)});}
  catch{throw new Error('Сервер не ответил. Нажмите «Обновить». Задание на сервере продолжится.');}
  let data;try{data=await response.json();}catch{throw new Error('Сервер временно недоступен. Попробуйте позже.');}
  if(!response.ok){if(response.status===403 && path==='session')saveToken('');throw new Error(data.error||'Не удалось выполнить запрос.');}
  return data;
}
function size(bytes){return bytes>=1024**3?(bytes/1024**3).toFixed(2)+' ГБ':(bytes/1024**2).toFixed(1)+' МБ';}
function weight(estimate){return estimate?.bytes>0?'≈ '+size(estimate.bytes):'размер неизвестен';}
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function safeResult(url){try{const u=new URL(url);return (u.origin===location.origin&&u.pathname.startsWith('/download/')) || (u.origin==='https://youtu.be'&&/^\/[\w-]{11}$/.test(u.pathname));}catch{return false;}}
function resultLink(text,url){if(!safeResult(url))return null;const a=el('a',text,'button');a.href=url;a.target='_blank';a.rel='noreferrer';return a;}
function pollLater(id){clearTimeout(timer);timer=setTimeout(()=>loadJob(id),2500);}
function qualityOptions(row){
  const info=row.info||{},previous=$('quality').value;
  $('quality').replaceChildren(...[0,...(info.heights||[])].map(h=>{
    const details=info.qualitySizes?.[h],estimate=$('format').value==='mp4compat'?null:details,dimensions=details?.width&&details?.height?details.width+'×'+details.height:('до '+h+'p');
    const option=el('option',(h?dimensions:'Лучшее')+' · '+weight(estimate));option.value=h;return option;
  }));
  $('quality').value=[...$('quality').options].some(o=>o.value===previous)?previous:'0';
}
function formatChanged(){
  if(!current)return;
  const audio=['mp3','m4a'].includes($('format').value),images=current.info?.kind==='images';
  qualityOptions(current);
  $('quality').disabled=audio;
  $('video-options').hidden=images;
  $('youtube').closest('label').hidden=images;
  const youtubeAllowed=!!session?.youtube&&!audio&&!images;
  $('youtube').disabled=!youtubeAllowed;if(!youtubeAllowed)$('youtube').checked=false;
  $('youtube-note').textContent=images?'Из поста будут скачаны только картинки.':!session?.youtube?'Загрузка на YouTube ещё не подключена.':audio?'Аудио можно получить файлом.':'';
  $('estimate').textContent=$('format').value==='mp4compat' ? 'H.264/AAC: разрешение сохраняется, видео перекодируется. Размер станет известен после скачивания.' : audio ? (current.info?.duration?'Размер звука: ≈ '+size(current.info.duration*192000/8):'Размер звука станет известен после скачивания.') : '≈ — оценка видео вместе со звуком. Точный вес — после скачивания.';
  $('start').textContent=$('youtube').checked?'Скачать и загрузить на YouTube':images?'Скачать картинки':'Скачать файл';
}
function renderJob(row){
  const changed=current?.id!==row.id,wasChoice=current?.state==='choice';current=row;
  $('job').hidden=false;$('title').textContent=(row.title||'Видео').slice(0,120);
  $('job-status').textContent=row.cancelled&&active.has(row.state)?'Отменяю…':(['error','review'].includes(row.state)?row.error:labels[row.state]||row.state);
  if(row.state==='choice'&&row.info?.kind==='images')$('job-status').textContent=row.info.imageCount?'Картинок: '+row.info.imageCount:'Картинки поста';
  $('choice').hidden=row.state!=='choice';$('cancel').hidden=!(active.has(row.state)||row.state==='choice');$('cancel').disabled=row.cancelled;
  if(row.state==='choice'&&(changed||!wasChoice)){$('format').value=row.format||'mp4';qualityOptions(row);formatChanged();}
  $('result').replaceChildren();
  if(row.state==='done'){
    if(row.youtubeUrl){const link=resultLink('Открыть видео ↗',row.youtubeUrl);if(link)$('result').append(link);}
    for(const file of row.files||[]){const link=resultLink('Скачать · '+file.name,file.url);if(link)$('result').append(link);}
    if(row.size)$('result').append(el('p',size(row.size)+(row.media?.width?' · '+row.media.width+'×'+row.media.height:''),'muted'));
    if(row.expires && row.files?.length)$('result').append(el('p','Доступно до '+new Date(row.expires*1000).toLocaleString('ru-RU'),'muted'));
    if(row.media?.quicktimeWarning)$('result').append(el('p','Если QuickTime показывает только звук, откройте файл в другом плеере.','muted'));
    if(!row.files?.length&&!row.youtubeUrl)$('result').append(el('p','Результат находится в Telegram или срок хранения файла истёк.','muted'));
  }
}
async function loadJob(id){
  const sequence=++viewSequence;
  clearTimeout(timer);
  try{const row=await api('jobs/'+id);if(sequence!==viewSequence)return;renderJob(row);if(active.has(row.state))pollLater(id);else await history(false);}
  catch(error){if(sequence===viewSequence)status(error.message);}
}
async function history(restore=false){
  clearTimeout(historyTimer);
  const data=await api(showAudit?'audit':'jobs');$('history').replaceChildren();
  for(const row of data.jobs.slice(0,showAudit?15:5)){
    const card=el('div',undefined,'history-row'),content=el('div');
    const open=el('button',(row.title||'Видео').slice(0,90),'quiet');open.onclick=()=>{status();loadJob(row.id);};content.append(open);
    content.append(el('p',(showAudit?row.name+' · ':'')+(labels[row.state]||row.state)+(row.size?' · '+size(row.size):''),'muted'));
    if(showAudit){const source=el('a','Источник ↗');try{const u=new URL(row.url);if(['https:','http:'].includes(u.protocol)){source.href=u.href;source.target='_blank';source.rel='noreferrer';content.append(source);}}catch{}}
    card.append(content);$('history').append(card);
  }
  if(!data.jobs.length)$('history').append(el('p','Заданий пока нет.','muted'));
  const recent=data.jobs.find(row=>row.entrypoint==='extension');
  if(restore&&!current&&!busy&&recent)await loadJob(recent.id);
}
async function connect(){
  if(!token){$('login').hidden=false;$('workspace').hidden=true;return;}
  try{session=await api('session');$('login').hidden=true;$('workspace').hidden=false;$('account').textContent=session.name+ (session.role==='owner'&&session.name!=='Владелец'?' · владелец':'');$('version').textContent='Сервер '+session.version;$('audit').hidden=session.role!=='owner';await history(!parentOrigin);}
  catch(error){status(error.message);if(!token){$('login').hidden=false;$('workspace').hidden=true;}}
}
async function lock(action){if(busy)return;busy=true;$('inspect').disabled=$('start').disabled=true;status();try{await action();}catch(error){status(error.message);}finally{busy=false;$('inspect').disabled=$('start').disabled=false;}}
function sourceFromBrowser(url){return new Promise((resolve,reject)=>{const requestId=crypto.randomUUID();const timeout=setTimeout(()=>{bridgePending=null;reject(new Error('Откройте расширение заново.'));},15000);bridgePending={requestId,resolve,reject,timeout};parentMessage('source',{requestId,url});});}
window.addEventListener('message',event=>{
  if(!parentOrigin||event.origin!==parentOrigin||event.source!==window.parent)return;
  const data=event.data;
  if(data?.type==='context'){
    contextReceived=true;
    token=typeof data.token==='string'?data.token:'';
    const sources=Array.isArray(data.sources)?data.sources:[];
    $('source').replaceChildren(...sources.map(source=>{const option=el('option',source.label+' · '+new URL(source.url).hostname);option.value=source.url;return option;}));
    $('source-list').replaceChildren(...sources.map(source=>{
      const card=el('div',undefined,'source-item'), info=el('div'), button=el('button','Выбрать');
      info.append(el('strong',source.label),el('small',new URL(source.url).hostname));
      button.onclick=()=>{$('source').value=source.url;$('inspect').click();};
      card.append(info,button);return card;
    }));
    $('source-list').hidden=!sources.length;
    $('source').hidden=true;$('source-label').hidden=true;
    $('url').hidden=$('url-label').hidden=!!sources.length;
    $('inspect').hidden=!!sources.length;
    if(data.error)status(data.error);connect();
  }else if(data?.type==='source-result'&&bridgePending?.requestId===data.requestId){
    const pending=bridgePending;bridgePending=null;clearTimeout(pending.timeout);
    if(data.error)pending.reject(new Error(data.error));else pending.resolve(data.source);
  }
});
$('pair').onsubmit=event=>{event.preventDefault();lock(async()=>{const data=await api('pair',{code:$('code').value.trim()});saveToken(data.token);$('code').value='';await connect();});};
$('inspect').onclick=()=>lock(async()=>{
  const source=parentOrigin&&!$('source-list').hidden?await sourceFromBrowser($('source').value):{url:$('url').value.trim(),referer:$('url').value.trim(),userAgent:navigator.userAgent};
  const job=await api('jobs',{...source,prepare:true});await loadJob(job.id);
});
$('start').onclick=()=>lock(async()=>{if(!current)return;const id=current.id;await api('jobs/'+id+'/start',{format:$('format').value,height:Number($('quality').value||0),mode:$('youtube').checked?'link':'file'});await loadJob(id);});
$('cancel').onclick=()=>lock(async()=>{if(current){await api('jobs/'+current.id+'/cancel',{});await loadJob(current.id);}});
$('format').onchange=$('youtube').onchange=formatChanged;
$('refresh').onclick=()=>location.reload();
$('logout').onclick=()=>lock(async()=>{await api('logout',{});saveToken('');clearTimeout(timer);viewSequence++;current=null;session=null;$('job').hidden=true;await connect();});
$('audit').onclick=()=>lock(async()=>{showAudit=!showAudit;$('audit').textContent=showAudit?'Мои задания':'Журнал сотрудников';await history();});
if(parentOrigin){parentMessage('ready');setTimeout(()=>{if(!contextReceived){status('Не удалось подключить расширение. Нажмите «Обновить» вверху окна.');parentMessage('ready');}},5000);}else{token=localStorage.getItem('videodrop-token')||'';$('source-label').hidden=true;connect();}
