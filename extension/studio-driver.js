/* Uses only a tab created for an explicitly queued upload/connection. */
importScripts('config.js','studio-page.js');
let studioBusy=false;
const studioProtocol=1;
const studioSleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));

async function studioAPI(path,body={}) {
  const response=await fetch(VIDEODROP.base+'/api/studio/'+path,{method:'POST',
    headers:{'Content-Type':'application/json','X-VideoDrop-Token':VIDEODROP.token,'X-VideoDrop-Extension':chrome.runtime.id},
    body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
  const result=await response.json();if(!response.ok)throw new Error(result.error||'Локальное приложение не ответило.');return result;
}

class StudioRun {
  constructor(job){this.job=job;this.tabId=null;this.attached=false;this.lastPulse=0;this.stopped=false;}
  async command(method,params={}){return chrome.debugger.sendCommand({tabId:this.tabId},method,params);}
  async page(command='snapshot'){
    const input=this.job?{channelId:this.job.channelId,title:this.job.title,madeForKids:this.job.madeForKids}:{};
    const result=await this.command('Runtime.evaluate',{expression:'('+videoDropStudioPage.toString()+')('+JSON.stringify(command)+','+JSON.stringify(input)+')',returnByValue:true});
    if(result.exceptionDetails)throw new Error('Интерфейс YouTube Studio изменился. Локальная копия сохранена.');
    const state=result.result?.value;
    if(!state)throw new Error('Не удалось прочитать YouTube Studio.');
    return state;
  }
  async event(event,extra={}){
    const reply=await studioAPI('event',{jobId:this.job.id,lease:this.job.lease,event,...extra});
    if(reply.cancelled){this.stopped=true;throw new Error('Отправка отменена. Локальная копия сохранена.');}
    return reply;
  }
  async pulse(stage){
    if(this.job&&Date.now()-this.lastPulse>3000){await this.event('progress',{stage});this.lastPulse=Date.now();}
  }
  async wait(check,stage,timeout=120000){
    const end=Date.now()+timeout;
    while(Date.now()<end){
      await this.pulse(stage);
      const state=await this.page();
      if(state.blocked){await this.pulse(state.blocked);await studioSleep(1500);continue;}
      if(state.error)throw new Error(state.error+' Локальный файл сохранён.');
      if(this.job&&state.channelId&&state.channelId!==this.job.channelId)throw new Error('Выбран другой канал YouTube. Файл не удалён.');
      const value=await check(state);if(value)return value;
      await studioSleep(1500);
    }
    throw new Error(stage+': время ожидания истекло. Проверьте вкладку Studio; файл сохранён на Mac.');
  }
  async start(url){
    // Never attach to an existing user tab or to an arbitrary URL from a page.
    if(!/^https:\/\/studio\.youtube\.com\/(?:$|video\/[\w-]{11}\/edit$)/.test(url))throw new Error('Неизвестный адрес Studio.');
    const tab=await chrome.tabs.create({url,active:true});this.tabId=tab.id;
    await chrome.debugger.attach({tabId:this.tabId},'1.3');this.attached=true;
    await chrome.storage.session.set({studioActive:{tabId:this.tabId,jobId:this.job?.id||null}});
  }
  async navigate(url){
    if(!/^https:\/\/studio\.youtube\.com\/video\/[\w-]{11}\/edit$/.test(url))throw new Error('Некорректная ссылка видео.');
    await this.command('Page.navigate',{url});
  }
  async setFile(){
    const state=await this.page();
    if(state.channelId!==this.job.channelId||!state.fileInput)throw new Error('Канал или форма выбора файла изменились.');
    // Google reads the file directly from disk: no multi-gigabyte Blob, no duplicate download.
    const target=await this.command('Runtime.evaluate',{expression:'document.querySelector("ytcp-uploads-dialog ytcp-uploads-file-picker input[type=file]")'});
    const objectId=target.result?.objectId;if(!objectId)throw new Error('Не найден выбор видеофайла.');
    await this.event('submitted'); // Durable before the side effect: ambiguous failures never retry blindly.
    await this.command('DOM.setFileInputFiles',{objectId,files:[this.job.path]});
  }
  async upload(){
    let identifier=this.job.videoId;
    await this.start(identifier?'https://studio.youtube.com/video/'+identifier+'/edit':'https://studio.youtube.com/');
    await this.wait(s=>s.channelId===this.job.channelId,'Войдите в YouTube и выберите подключённый канал',600000);
    if(!identifier){
      if(this.job.submitted)throw new Error('Этот файл уже передавался в Studio. Проверьте его там, чтобы не создать дубликат.');
      let created=false,opened=false;
      await this.wait(async s=>{
        if(s.fileInput)return true;
        if(s.uploadMenu&&!opened){opened=true;await this.page('upload-menu');}
        else if(s.createVisible&&!created){created=true;await this.page('create');}
        return false;
      },'Открываем форму загрузки');
      await this.setFile();
      await this.wait(async s=>{
        if(s.videoId&&!identifier){identifier=s.videoId;await this.event('video',{videoId:identifier});}
        return s.detailsReady;
      },'YouTube читает видеофайл',300000);
      await this.page('details');
      await this.wait(s=>s.detailsSet,'Заполняем название и аудиторию видео');
      let steps=0,lastAdvance=0;
      await this.wait(async s=>{
        if(s.videoId&&!identifier){identifier=s.videoId;await this.event('video',{videoId:identifier});}
        if(s.unlistedVisible)return true;
        if(s.nextEnabled&&Date.now()-lastAdvance>2500){
          if(++steps>4)throw new Error('В Studio появился дополнительный шаг. Завершите его в открытой вкладке; файл сохранён.');
          lastAdvance=Date.now();await this.page('next');
        }
        return false;
      },'Настраиваем доступ к ролику',300000);
      await this.page('unlisted');
      await this.wait(async s=>{
        if(s.videoId&&!identifier){identifier=s.videoId;await this.event('video',{videoId:identifier});}
        return identifier&&s.unlisted&&s.doneEnabled&&s.uploadComplete;
      },'Загрузка видео в YouTube · не закрывайте вкладку',24*60*60*1000);
      const saved=await this.page('save');if(saved.blocked||!saved.clicked)throw new Error(saved.blocked||'Не удалось сохранить доступ «По ссылке».');
      await this.wait(s=>!s.dialog||s.finished,'Сохраняем видео в YouTube',180000);
      await this.navigate('https://studio.youtube.com/video/'+identifier+'/edit');
    }
    let refreshed=Date.now();
    const proof=await this.wait(async s=>{
      if(s.editorId!==identifier||s.channelId!==this.job.channelId)return false;
      if(s.privacy&&s.privacy!=='unlisted')throw new Error('YouTube не подтвердил доступ «По ссылке». Локальная копия сохранена.');
      if(s.filename&&s.filename.normalize('NFC')!==this.job.filename.normalize('NFC'))throw new Error('В Studio другой исходный файл. Локальная копия сохранена.');
      if(s.processed&&s.saved&&s.privacy==='unlisted'&&s.filename)return {...s,videoId:identifier};
      if(Date.now()-refreshed>60000){refreshed=Date.now();await this.navigate('https://studio.youtube.com/video/'+identifier+'/edit');}
      return false;
    },'YouTube обрабатывает видео · проверяем доступ «По ссылке»',4*60*60*1000);
    await this.event('complete',{proof:{videoId:identifier,channelId:proof.channelId,filename:proof.filename,
      privacy:proof.privacy,processed:proof.processed,saved:proof.saved,url:proof.url}});
  }
  async finish(){
    if(this.attached)try{await chrome.debugger.detach({tabId:this.tabId});}catch{}
    if(this.stopped&&this.tabId)try{await chrome.tabs.remove(this.tabId);}catch{}
    await chrome.storage.session.remove('studioActive');
  }
}

async function studioPump(){
  if(studioBusy){try{await studioAPI('heartbeat',{protocol:studioProtocol,version:chrome.runtime.getManifest().version});}catch{}return;}studioBusy=true;
  try{
    await studioAPI('heartbeat',{protocol:studioProtocol,version:chrome.runtime.getManifest().version});
    const task=await studioAPI('claim');
    if(task.connect){
      const run=new StudioRun(null);
      try{
        await run.start('https://studio.youtube.com/');
        const page=await run.wait(s=>s.channelId?s:false,'Войдите в YouTube в открытой вкладке',540000);
        await studioAPI('connected',{nonce:task.connect.nonce,channelId:page.channelId,channelTitle:page.channelTitle});
      }catch(error){try{await studioAPI('connected',{nonce:task.connect.nonce,error:true});}catch{}}
      finally{await run.finish();}
    }else if(task.job){
      const run=new StudioRun(task.job);
      try{await run.upload();}
      catch(error){try{await run.event('stopped',{message:error.message||'Отправка остановлена. Локальная копия сохранена.'});}catch{}}
      finally{await run.finish();}
    }
  }catch{}finally{studioBusy=false;}
}

chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='videodrop-studio')studioPump();});
chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  if(sender.id===chrome.runtime.id&&message?.type==='studio-pump'){studioPump();sendResponse({ok:true});}
});
chrome.runtime.onMessageExternal.addListener((message,sender,sendResponse)=>{
  if(sender.url&&new URL(sender.url).origin===VIDEODROP.base&&message?.token===VIDEODROP.token&&message.type==='studio-pump'){
    studioPump();sendResponse({ok:true});
  }
});
chrome.alarms.create('videodrop-studio',{periodInMinutes:0.5});
studioPump();
