const $ = id => document.getElementById(id);
let token = '', studioExtensionId = '', current = null, inspecting = '', pollBusy = false, youtubeControls;
let updateBusy=false, updateRestarting=false, loadedVersion='', restartStarted=0, downloadsOnly=false;
const completed = new Set();
const opening = new Set();
function wakeStudio(extensionId=studioExtensionId){if(extensionId&&window.chrome?.runtime?.sendMessage)try{chrome.runtime.sendMessage(extensionId,{type:'studio-pump',token},()=>{void chrome.runtime.lastError;});}catch{}}
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body ? 'POST' : 'GET', headers: {'X-VideoDrop-Token': token, ...(body ? {'Content-Type':'application/json'} : {})}, body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Ошибка приложения');
  if(path==='studio/status'||path==='studio/connect')studioExtensionId=data.extensionId||'';
  if(body)wakeStudio();
  return data;
}
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : ''; $('notice').hidden = !message; }
function bytes(value) { if (!value) return ''; const n = Math.log(value) / Math.log(1024) | 0; return (value / 1024 ** n).toFixed(n ? 1 : 0) + ' ' + ['Б','КБ','МБ','ГБ','ТБ'][Math.min(n,4)]; }
function duration(value) { if (!value) return ''; return value < 60 ? Math.round(value) + ' сек' : Math.floor(value/60) + ' мин'; }
function result(info) {
  current = info; if(info.storage){youtubeControls.set(info.storage);if(info.storage.enabled)youtubeControls.refresh();} $('video-title').textContent = info.title;
  $('video-meta').textContent = [info.extractor, duration(info.duration), info.heights.length ? 'до ' + info.heights[0] + 'p' : 'исходное качество'].filter(Boolean).join(' · ');
  $('quality').replaceChildren(new Option('Лучшее доступное', '0'), ...info.heights.map(h => new Option(h + 'p', String(h))));
  for (const option of $('format').options) option.disabled = ['mp4','mkv'].includes(option.value) ? !info.hasVideo : !info.hasAudio;
  $('format').value = info.hasVideo ? 'mp4' : 'mp3';
  updateFormat(); $('result').hidden = false; document.querySelector('.advanced').open=false;
}
function updateFormat() {
  const audio = ['mp3','m4a'].includes($('format').value); $('quality').disabled = audio;
  $('format-hint').textContent = audio ? 'Сохранится только звуковая дорожка. MP3 — 192 кбит/с; M4A сохраняет исходный AAC, когда он доступен.' : 'Лучшее доступное качество не выше выбранного. Видео и звук объединяются автоматически.';
}
$('format').addEventListener('change', updateFormat);
$('lookup').addEventListener('submit', async event => {
  event.preventDefault(); if (inspecting) return;
  $('find').disabled = true; $('result').hidden = true; current = null; notice('Ищем видео и доступное качество…');
  try { const storage=await youtubeControls.options(); const data = await api('inspect', {url:$('url').value.trim(), referer:$('referer').value.trim(),storage}); inspecting = data.jobId; history.replaceState(null,'','#job='+inspecting); }
  catch (error) { notice(error.message, true); $('find').disabled = false; }
});
$('download').addEventListener('click', async () => {
  if (!current) return; $('download').disabled = true;
  try { const storage=await youtubeControls.options(); await api('download', {sourceId:current.sourceId, height:Number($('quality').value), format:$('format').value,storage}); notice(storage.enabled?'Добавлено в очередь: скачать → загрузить на YouTube → проверить доступ.':'Добавлено в загрузки. Файл появится в папке «Загрузки».'); await poll(); }
  catch (error) { notice(error.message, true); }
  finally { $('download').disabled = false; }
});
async function openFolder(target) { try { await api('open-folder', {target}); } catch (error) { notice(error.message, true); } }
$('folder').onclick = () => openFolder('downloads');
$('extension-folder').onclick = () => openFolder('extension');
$('extension-help').onclick = () => { $('help').hidden = !$('help').hidden; if (!$('help').hidden) $('help').scrollIntoView({behavior:'smooth',block:'nearest'}); };
function renderJobs(jobs) {
  const downloads = jobs.filter(j => j.kind === 'download').reverse(); $('downloads-section').hidden = !downloads.length;
  $('jobs').replaceChildren(...downloads.map(job => {
    const root = document.createElement('article'); root.className='job';
    const top=document.createElement('div'); top.className='job-top';
    const title=document.createElement('div'); title.className='job-title'; title.textContent=job.title || 'Видео'; top.append(title);
    if (['working','queued'].includes(job.status)) { const cancel=document.createElement('button'); cancel.className='cancel'; cancel.textContent='Отменить'; cancel.onclick=async()=>{try{await api('cancel',{jobId:job.id}); await poll();}catch(e){notice(e.message,true);}}; top.append(cancel); }
    root.append(top);
    const detail=document.createElement('div'); detail.className='job-info';
    if (job.status==='done') detail.textContent='✓ Готово · '+bytes(job.result.size)+' · '+(job.result.youtubeVerified?'YouTube · по ссылке'+' · '+(job.result.localKept?'копия на Mac сохранена':'локальная копия удалена'):job.result.filename);
    else if(job.status==='error') { detail.textContent=job.error; detail.classList.add('job-error'); }
    else if(job.status==='cancelled') detail.textContent=job.result?.localKept?'Отменено · локальная копия сохранена':'Отменено';
    else { detail.textContent=[job.stage, job.format.toUpperCase(), job.height ? 'до '+job.height+'p' : '',job.percent!=null ? job.percent+'%' : '',job.speed ? bytes(job.speed)+'/с' : '',job.eta ? 'осталось '+Math.ceil(job.eta/60)+' мин' : ''].filter(Boolean).join(' · '); }
    root.append(detail);
    if(['done','error','cancelled'].includes(job.status)&&job.result?.localKept){
      const open=document.createElement('button');open.className='open-video';
      const label=['mp3','m4a'].includes(job.format)?'Открыть аудио':'Открыть видео';
      open.textContent=opening.has(job.id)?'Открываем…':label;open.disabled=opening.has(job.id);
      open.onclick=async()=>{
        if(opening.has(job.id))return;opening.add(job.id);open.disabled=true;open.textContent='Открываем…';
        try{await api('open-video',{jobId:job.id});notice('Файл передан в стандартный плеер Mac.');}
        catch(error){notice(error.message,true);}
        finally{opening.delete(job.id);open.disabled=false;open.textContent=label;}
      };root.append(open);
    }
    if(!downloadsOnly&&job.result?.youtubeId&&/^[a-zA-Z0-9_-]{11}$/.test(job.result.youtubeId)){
      const link=document.createElement('a');link.className=job.result.localKept?'text-button youtube-link':'open-video';
      link.textContent='Открыть на YouTube';link.href='https://youtu.be/'+job.result.youtubeId;
      link.target='_blank';link.rel='noopener noreferrer';root.append(link);
    }
    if(!downloadsOnly&&job.storage?.provider==='studio'&&job.storage.enabled&&!job.result?.youtubeVerified&&job.result?.localKept){
      const studio=document.createElement('a');studio.className='text-button youtube-link';studio.textContent='Открыть YouTube Studio';
      studio.href=job.result.youtubeId&&/^[a-zA-Z0-9_-]{11}$/.test(job.result.youtubeId)?'https://studio.youtube.com/video/'+job.result.youtubeId+'/edit':'https://studio.youtube.com/';
      studio.target='_blank';studio.rel='noopener noreferrer';root.append(studio);
    }
    if(!downloadsOnly&&['done','error','cancelled'].includes(job.status)&&job.result?.localKept&&['mp4','mkv'].includes(job.format)&&!job.result.youtubeVerified&&!job.retrying&&!job.uploading&&!job.studioNeedsReview){
      const retry=document.createElement('button');retry.className='text-button';
      retry.textContent=job.result.youtubeId?'Проверить YouTube':job.storage?.enabled?'Продолжить через Chrome':'Загрузить на YouTube';
      retry.onclick=async()=>{retry.disabled=true;try{
        const storage=await youtubeControls.options();if(!storage.enabled)throw new Error('Включите «Загружать на YouTube» и подключите канал в настройках выше.');
        await api('upload-youtube',{jobId:job.id,storage});await poll();
      }catch(error){notice(error.message,true);retry.disabled=false;}};root.append(retry);
    }
    if (job.status==='working') { const p=document.createElement('progress'); p.max=100; if(job.percent!=null)p.value=job.percent; root.append(p); }
    return root;
  }));
}
async function poll() {
  if (!token || pollBusy) return; pollBusy=true;
  try {
    const jobs = await api('jobs'); renderJobs(jobs);
    if(inspecting) {
      const job=jobs.find(j=>j.id===inspecting);
      if(job && ['done','error','cancelled'].includes(job.status)) {
        completed.add(job.id); inspecting=''; $('find').disabled=false;
        if(job.status==='done'){result(job.result);notice('');} else notice(job.error || 'Поиск отменён',true);
      } else if(!job){inspecting='';$('find').disabled=false;notice('Приложение перезапущено. Найдите видео заново.',true);}
    }
  } catch(error) { notice(updateRestarting?'VideoDrop обновляется. Ждём перезапуска…':'Связь с приложением потеряна. Откройте «Запустить VideoDrop.command».',!updateRestarting); }
  finally { pollBusy=false; }
}
function readHash() { const id = new URLSearchParams(location.hash.slice(1)).get('job'); if(id && /^[a-f0-9]{32}$/.test(id) && !completed.has(id)){inspecting=id;$('find').disabled=true;notice('Получено из Chrome. Ищем доступное качество…');} }
function renderUpdates(value){
  if(loadedVersion&&value.version!==loadedVersion){location.reload();return;}
  loadedVersion=value.version;
  if(value.restarting&&!updateRestarting)restartStarted=Date.now();
  updateRestarting=Boolean(value.restarting);
  $('app-version').textContent='v'+value.version;
  $('update-badge').textContent=value.restarting?'Обновляем…':value.available?'Доступна '+value.latestVersion:'';
  $('update-auto').checked=value.automatic;
  const busy=['checking','downloading','preparing','restarting'].includes(value.phase);
  const checked=value.checkedAt?new Date(value.checkedAt*1000).toLocaleString('ru-RU'):'';
  let message=value.message|| (value.available?'Доступна новая версия '+value.latestVersion+'.':checked?'Установлена актуальная версия. Проверено: '+checked:'Наличие обновлений ещё не проверено.');
  if(value.pending&&!busy)message='Обновление ожидает завершения текущих загрузок.';
  $('update-status').textContent=message;
  $('update-status').classList.toggle('update-error',!busy&&value.result?.ok===false);
  $('update-check').disabled=busy;
  $('update-install').hidden=!value.available;
  $('update-install').disabled=busy||value.pending;
  $('update-install').textContent=value.pending?'В очереди':'Установить '+value.latestVersion;
  $('update-rollback').hidden=!value.previousVersion;
  $('update-rollback').disabled=busy||value.pending;
  $('update-rollback').textContent='Вернуть версию '+value.previousVersion;
  $('update-extension').hidden=!value.extensionReload;
  $('update-source').href='https://github.com/'+value.repository+'/releases';
  if(value.extensionReload||value.restarting)$('updates-panel').open=true;
}
async function pollUpdates(){
  if(!token||updateBusy)return;updateBusy=true;
  try{renderUpdates(await api('updates'));}
  catch{
    if(updateRestarting){
      $('update-status').textContent=Date.now()-restartStarted>90000?'Запуск задерживается. Откройте «Запустить VideoDrop.command» — он восстановит прерванное обновление.':'Устанавливаем обновление и перезапускаем приложение…';
    }
  }finally{updateBusy=false;}
}
async function updateAction(action,body={}){
  try{await api('updates/'+action,body);await pollUpdates();}
  catch(error){$('update-status').textContent=error.message;$('update-status').classList.add('update-error');}
}
$('update-check').onclick=()=>updateAction('check');
$('update-install').onclick=()=>updateAction('install');
$('update-rollback').onclick=()=>updateAction('rollback');
$('update-auto').onchange=()=>updateAction('settings',{automatic:$('update-auto').checked});
function updateHash(){if(location.hash==='#updates'){$('updates-panel').open=true;$('updates-panel').scrollIntoView({block:'center'});updateAction('check');}}
window.addEventListener('hashchange',()=>{readHash();poll();updateHash();});
(async()=>{try{
  const config=await api('bootstrap');token=config.token;$('extension-path').textContent=config.extension;
  downloadsOnly=config.edition==='lite';
  youtubeControls=downloadsOnly?{options:async()=>({provider:'local',enabled:false,deleteLocal:false}),set(){},refresh(){}}:await new VideoDropYouTube($('youtube-settings'),{request:api,wake:wakeStudio,
    load:async()=>JSON.parse(localStorage.getItem('studioOptions')||'null'),
    save:async value=>{localStorage.removeItem('cloudOptions');localStorage.removeItem('youtubeOptions');localStorage.setItem('studioOptions',JSON.stringify(value));}}).init();
  readHash();await poll();await pollUpdates();updateHash();setInterval(poll,1000);setInterval(pollUpdates,2000);
}catch(error){notice('Приложение не запущено. Откройте «Запустить VideoDrop.command».',true);}})();
