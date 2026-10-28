import {inspectStream,saveStream} from './stream-core.mjs';
const $=id=>document.getElementById(id);
let input,options=[];
const status=message=>{$('status').textContent=message;};
function safeName(value){return (value||'Видео').replace(/[\\/:*?"<>|\x00-\x1f]/g,' ').replace(/\s+/g,' ').trim().slice(0,120)||'Видео';}
function label(option){
  const dimensions=option.width&&option.height?`${option.width}×${option.height}`:'Разрешение неизвестно';
  const weight=option.size?`≈ ${(option.size/1048576).toFixed(1)} МБ`:'размер неизвестен';
  return `${dimensions} · ${weight}${option.supported?'':' · несовместимый формат'}`;
}
async function init(){
  const id=new URLSearchParams(location.hash.slice(1)).get('id');
  if(!id||!/^[a-f0-9-]{36}$/i.test(id))throw new Error('Откройте поток через расширение заново.');
  const key='local-stream:'+id, item=(await chrome.storage.session.get(key))[key];
  await chrome.storage.session.remove(key);
  if(!item||typeof item.url!=='string'||Date.now()-item.created>300000)throw new Error('Срок выбора истёк. Откройте поток заново.');
  $('title').textContent=safeName(item.title);
  const result=await inspectStream(item.url);input=result.input;options=result.options;
  for(let i=0;i<options.length;i++){
    const option=document.createElement('option');option.value=String(i);option.textContent=label(options[i]);option.disabled=!options[i].supported;$('quality').append(option);
  }
  const first=options.findIndex(option=>option.supported);
  if(first<0){input.dispose();input=null;throw new Error('В потоке не найден совместимый MP4 со звуком.');}
  $('quality').value=String(first);$('options').hidden=false;
  status(first?'Некоторые более высокие качества несовместимы с обычными проигрывателями.':'Выберите качество.');
}
$('save').onclick=async()=>{
  let writable;
  try{
    // The save picker must be opened directly from the user's click.
    const handle=await window.showSaveFilePicker({suggestedName:safeName($('title').textContent)+'.mp4',types:[{description:'Видео MP4',accept:{'video/mp4':['.mp4']}}]});
    $('save').disabled=true;$('quality').disabled=true;
    writable=await handle.createWritable();
    await saveStream(input,options[Number($('quality').value)],writable,p=>status(`Скачиваем: ${Math.round(p*100)}%`));
    input.dispose();input=null;status('Готово. Видео сохранено на компьютер.');$('save').hidden=true;
  }catch(error){
    if(writable)try{await writable.abort();}catch{}
    if(error?.name==='AbortError')status('Сохранение отменено.');
    else status(error?.message?.includes('другому сайту')?error.message:'Не удалось сохранить поток. Проверьте доступ к видео и попробуйте снова.');
    $('save').disabled=false;$('quality').disabled=false;
  }
};
window.addEventListener('pagehide',()=>{if(input)input.dispose();});
init().catch(error=>status(error?.message?.includes('поток')||error?.message?.includes('совместим')?error.message:'Не удалось прочитать поток. Откройте страницу и запустите видео заново.'));
