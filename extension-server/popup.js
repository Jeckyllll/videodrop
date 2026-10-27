'use strict';
const $=id=>document.getElementById(id),frame=$('app'),base=VideoDropBridge.base;
let tab=null,sources=[],contextError='',ready=false,frameReady=false;
const http=url=>{try{const u=new URL(url);return ['https:','http:'].includes(u.protocol);}catch{return false;}};
const sourcePattern=/\.(mp4|webm|mov|m3u8|mpd)(?:[?#]|$)/i;
const playerPattern=/https?:\/\/(?:player\.vimeo\.com\/video\/|(?:www\.)?youtube(?:-nocookie)?\.com\/embed\/|rutube\.ru\/play\/embed\/|(?:kinescope\.io|iframe\.mediadelivery\.net|video\.sibnet\.ru|vkvideo\.ru|vk\.com)\/)/i;
function status(text){$('status').textContent=text;}
function send(type,data={}){frame.contentWindow.postMessage({type,...data},base);}
function scanPage(){
  const result=[];
  for(const node of document.querySelectorAll('video,video source,iframe,embed')){const url=node.currentSrc||node.src;if(url&&/^https?:/.test(url))result.push({url,label:node.tagName==='IFRAME'?'Встроенное видео':'Видеофайл'});}
  for(const entry of performance.getEntriesByType('resource'))if(/\.(m3u8|mpd|mp4|webm|mov)(?:[?#]|$)/i.test(entry.name)&&!/[?&](?:range|bytestart)=/i.test(entry.name))result.push({url:entry.name,label:/\.(m3u8|mpd)(?:[?#]|$)/i.test(entry.name)?'Поток видео':'Видеофайл'});
  return result;
}
async function context(){
  const saved=await chrome.storage.local.get('serverToken');
  if(ready&&frameReady)send('context',{token:saved.serverToken||'',sources,error:contextError,features:{directDownload:true}});
}
async function init(){
  // Do not expose the API token to content scripts or synchronize it through Google.
  await chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
  const preference=await chrome.storage.local.get('accessEnabled');
  $('access').checked=preference.accessEnabled!==false;
  const selected=new URLSearchParams(location.search).get('tab');
  if(selected&&/^\d+$/.test(selected)){tab=await chrome.tabs.get(Number(selected));document.body.classList.add('expanded');$('expand').hidden=true;}
  else [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  if(tab&&http(tab.url)){
    let frames=[];
    try{frames=await chrome.scripting.executeScript({target:{tabId:tab.id,allFrames:true},func:scanPage});}
    catch{try{frames=await chrome.scripting.executeScript({target:{tabId:tab.id},func:scanPage});}catch{}}
    for(const entry of frames)for(const source of entry.result||[])if(http(source.url)&&(sourcePattern.test(source.url)||playerPattern.test(source.url)))sources.push(source);
    const captured=(await chrome.storage.session.get(String(tab.id)))[String(tab.id)];
    if(captured?.page===tab.url)sources.push(...captured.media.filter(source=>http(source.url)));
    sources.push({url:tab.url,label:sources.length?'Ссылка на страницу':'Видео или пост'});
    sources=[...new Map(sources.map(source=>[source.url,source])).values()].slice(0,60);
  }else{contextError='Откройте страницу с видео или вставьте ссылку.';$('access').disabled=$('capture').disabled=true;}
  ready=true;if(frameReady)await context();
}
$('access').onchange=async()=>{await chrome.storage.local.set({accessEnabled:$('access').checked});};
window.addEventListener('message',async event=>{
  if(!VideoDropBridge.accepts(event,frame))return;
  const data=event.data;
  if(data?.type==='ready'){frameReady=true;if(ready)await context();return;}
  if(data?.type==='save-token'){
    if(typeof data.token==='string'&&/^[A-Za-z0-9_-]{0,128}$/.test(data.token)){if(data.token)await chrome.storage.local.set({serverToken:data.token});else await chrome.storage.local.remove('serverToken');}return;
  }
  if(data?.type==='download-local'&&typeof data.requestId==='string'){
    try{
      const source=VideoDropBridge.findSource(sources,data.url);
      if(!source||!tab||!VideoDropBridge.directMedia(source.url))throw new Error('На странице нет прямого файла MP4, WebM или MOV. Для этого источника прямое скачивание пока недоступно.');
      const current=await chrome.tabs.get(tab.id);
      if(current.url!==tab.url)throw new Error('Страница изменилась. Откройте расширение заново.');
      await chrome.downloads.download({url:source.url,saveAs:false});
      send('download-local-result',{requestId:data.requestId});
    }catch(error){send('download-local-result',{requestId:data.requestId,error:error.message||'Не удалось запустить загрузку в Chrome.'});}
    return;
  }
  if(data?.type!=='source'||typeof data.requestId!=='string')return;
  try{
    const source=VideoDropBridge.findSource(sources,data.url);
    if(!source||!tab)throw new Error('Выберите видео из списка.');
    const current=await chrome.tabs.get(tab.id);
    if(current.url!==tab.url)throw new Error('Страница изменилась. Откройте расширение заново.');
    let cookies=[];
    if($('access').checked){
      const store=(await chrome.cookies.getAllCookieStores()).find(item=>item.tabIds.includes(tab.id));
      for(const url of [...new Set([tab.url,source.url])])cookies.push(...await chrome.cookies.getAll({url,...(store?{storeId:store.id}:{})}));
      cookies=VideoDropBridge.scopedCookies(cookies,[tab.url,source.url]);
    }
    send('source-result',{requestId:data.requestId,source:{url:source.url,referer:tab.url,userAgent:navigator.userAgent,cookies}});
  }catch(error){send('source-result',{requestId:data.requestId,error:error.message||'Не удалось прочитать доступ к странице.'});}
});
$('refresh').onclick=()=>{frameReady=false;frame.src=base+'/app/?refresh='+Date.now()+'#extension='+chrome.runtime.id;status('');};
$('expand').onclick=()=>{chrome.windows.create({url:chrome.runtime.getURL('popup.html')+(tab?'?tab='+tab.id:''),type:'popup',width:540,height:800});window.close();};
$('capture').onclick=async()=>{
  try{
    if(!tab)return;
    const state=(await chrome.storage.session.get(String(tab.id)))[String(tab.id)];
    if(state?.armed&&state.page===tab.url){sources=[];await init();status('Список обновлён.');return;}
    await chrome.storage.session.set({[String(tab.id)]:{armed:true,started:Date.now(),page:tab.url,media:[]}});
    await chrome.tabs.reload(tab.id);status('Запустите видео и откройте расширение снова.');
  }catch{status('Не удалось включить поиск.');}
};
frame.src=base+'/app/#extension='+chrome.runtime.id;
init().catch(()=>{ready=true;contextError='Не удалось прочитать страницу. Вставьте ссылку вручную.';if(frameReady)context();});
