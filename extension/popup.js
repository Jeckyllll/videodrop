const $=id=>document.getElementById(id);
let tab, youtubeControls;
async function localAPI(path,body){
  let response;
  try{response=await fetch(VIDEODROP.base+'/api/'+path,{method:body?'POST':'GET',headers:{'X-VideoDrop-Token':VIDEODROP.token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});}
  catch{throw new Error('Сначала откройте «Запустить VideoDrop.command» на Mac.');}
  const result=await response.json();if(!response.ok)throw new Error(result.error||'Ошибка приложения');return result;
}
const sourcePattern=/\.(mp4|webm|m3u8|mpd)(?:[?#]|$)/i;
const playerPattern=/https?:\/\/(?:player\.vimeo\.com\/video\/|(?:www\.)?youtube(?:-nocookie)?\.com\/embed\/|rutube\.ru\/play\/embed\/|(?:kinescope\.io|iframe\.mediadelivery\.net|video\.sibnet\.ru|vkvideo\.ru|vk\.com)\/)/i;
function message(text){$('status').textContent=text;}
async function wakeBackground(){
  const type=VIDEODROP_EDITION==='lite'?'extension-ping':'studio-pump';
  // Chrome can replace the worker while this popup is opening after an update.
  for(let attempt=0;attempt<2;attempt++){
    try{const reply=await chrome.runtime.sendMessage({type});if(reply?.ok)return true;}catch{}
    if(attempt===0)await new Promise(resolve=>setTimeout(resolve,250));
  }
  message('Фоновая часть VideoDrop не ответила. Нажмите ↻ на карточке VideoDrop в chrome://extensions и откройте расширение снова.');
  return false;
}
async function send(source, button) {
  try {
    button.disabled=true;let cookies=[];
    if($('access').checked){
      const origins=[...new Set([new URL(tab.url).origin+'/*',new URL(source.url).origin+'/*'])];
      const granted=await chrome.permissions.request({permissions:['cookies'],origins});
      if(!granted)throw new Error('Доступ не предоставлен. Можно отправить видео без входа, сняв галочку.');
      const store=(await chrome.cookies.getAllCookieStores()).find(s=>s.tabIds.includes(tab.id));
      for(const url of [...new Set([tab.url,source.url])]){
        const values=await chrome.cookies.getAll({url,...(store?{storeId:store.id}:{})});
        cookies.push(...values);
      }
      cookies=[...new Map(cookies.map(c=>[c.domain+'|'+c.path+'|'+c.name,c])).values()];
    }
    message('Передаём видео в приложение…');
    const storage=await youtubeControls.options();
    const result=await localAPI('import',{url:source.url,referer:tab.url,userAgent:navigator.userAgent,cookies,storage});
    await chrome.tabs.create({url:result.openUrl});window.close();
  }catch(error){message(error.message==='Failed to fetch'?'Сначала откройте «Запустить VideoDrop.command» на Mac.':error.message);}
  finally{button.disabled=false;}
}
function scanPage() {
  const result=[];
  for(const el of document.querySelectorAll('video,video source,iframe,embed')) {
    const url=el.currentSrc || el.src;
    if(url && /^https?:/.test(url))result.push({url,label:el.tagName==='IFRAME'?'Встроенное видео':'Видеофайл',iframe:el.tagName==='IFRAME'});
  }
  for(const entry of performance.getEntriesByType('resource'))if(/\.(m3u8|mpd)(?:[?#]|$)/i.test(entry.name))result.push({url:entry.name,label:'Поток видео'});
  return result;
}
async function init(){
  [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  if(!tab || !/^https?:/.test(tab.url||'')){message('Откройте страницу с видео.');$('capture').disabled=true;return;}
  $('page').textContent=tab.title || new URL(tab.url).hostname;
  const sources=[];
  try{
    const frames=await chrome.scripting.executeScript({target:{tabId:tab.id,allFrames:true},func:scanPage});
    for(const frame of frames)for(const source of frame.result||[])if(sourcePattern.test(source.url)||playerPattern.test(source.url))sources.push(source);
  }catch{
    // Cross-origin frames may lack permission; always retry the permitted top-level page.
    try{const frames=await chrome.scripting.executeScript({target:{tabId:tab.id},func:scanPage});for(const frame of frames)for(const source of frame.result||[])if(sourcePattern.test(source.url)||playerPattern.test(source.url))sources.push(source);}catch{}
  }
  const state=await chrome.storage.session.get(String(tab.id));
  const capture=state[String(tab.id)];
  if(capture?.page===tab.url){sources.push(...capture.media);$('capture').textContent='Поиск потоков включён · обновить список';}
  sources.push({url:tab.url,label:sources.length?'Попробовать ссылку на страницу':'Видео по ссылке на страницу'});
  const unique=[...new Map(sources.map(s=>[s.url,s])).values()];
  $('items').replaceChildren(...unique.map((source,index)=>{
    const row=document.createElement('div');row.className='item';const text=document.createElement('div');
    const title=document.createElement('strong');title.textContent=source.label+(unique.length>1?' '+(index+1):'');
    const host=document.createElement('small');host.textContent=new URL(source.url).hostname;
    text.append(title,host);const button=document.createElement('button');button.textContent='Выбрать';button.onclick=()=>send(source,button);row.append(text,button);return row;
  }));
}
$('capture').onclick=async()=>{
  try{
    // Cross-origin CDN manifests can have arbitrary hosts; this optional permission is requested only here.
    if(!await chrome.permissions.request({origins:['https://*/*','http://*/*']})){message('Разрешение не предоставлено. Встроенные плееры всё ещё доступны.');return;}
    const state=await chrome.storage.session.get(String(tab.id));
    if(state[String(tab.id)]?.armed){await init();message('Список обновлён. Если потоков нет, запустите видео.');return;}
    await chrome.storage.session.set({[String(tab.id)]:{armed:true,started:Date.now(),page:tab.url,media:[]}});
    await chrome.tabs.reload(tab.id);message('Страница обновлена. Запустите видео и откройте VideoDrop снова.');
  }catch(error){message(error.message);}
};
$('open').onclick=()=>chrome.tabs.create({url:VIDEODROP.base});
(async()=>{
  if(VIDEODROP_EDITION === 'lite'){
    youtubeControls={options:async()=>({provider:'local',enabled:false,deleteLocal:false})};
    await wakeBackground();
    await init();return;
  }
  await wakeBackground();
  youtubeControls=await new VideoDropYouTube($('youtube-settings'),{request:localAPI,wake:wakeBackground,
    load:async()=>(await chrome.storage.local.get('studioOptions')).studioOptions,
    save:async value=>{await chrome.storage.local.remove(['cloudOptions','youtubeOptions']);await chrome.storage.local.set({studioOptions:value});}}).init();
  await init();
})().catch(error=>message(error.message));
