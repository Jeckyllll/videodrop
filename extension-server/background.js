// Observes media only in a tab explicitly armed by the user. Nothing is sent automatically.
const mediaType = /(?:application\/(?:vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)|video\/(?:mp4|webm))/i;
const mediaURL = /\.(m3u8|mpd|mp4|webm)(?:[?#]|$)/i;
const chains = new Map();
function enqueue(tabId, action) {
  const next = (chains.get(tabId) || Promise.resolve()).then(action).catch(()=>{});
  chains.set(tabId,next);
  next.finally(()=>{if(chains.get(tabId)===next)chains.delete(tabId);});
}
chrome.webRequest.onHeadersReceived.addListener(details => {
  if (details.tabId < 0 || details.statusCode >= 400) return;
  const type = (details.responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type')?.value || '';
  if (!mediaURL.test(details.url) && !mediaType.test(type)) return;
  // Do not mistake individual DASH/HLS fragments for complete media.
  if (/\.(m4s|ts|aac)(?:[?#]|$)/i.test(details.url) || /(?:[?&])(?:range|bytestart)=/i.test(details.url)) return;
  const key = String(details.tabId);
  enqueue(details.tabId, async () => {
    const stored=await chrome.storage.session.get(key); const record=stored[key];
    if(!record?.armed || Date.now()-record.started>30*60*1000)return;
    if(!record.media.some(m=>m.url===details.url)) {
      const label=/m3u8/i.test(details.url+type)?'HLS · поток':/mpd|dash/i.test(details.url+type)?'DASH · поток':'Видеофайл';
      record.media.push({url:details.url,label});record.media=record.media.slice(-60);
      await chrome.storage.session.set({[key]:record});
      await chrome.action.setBadgeText({tabId:details.tabId,text:String(record.media.length)});
      await chrome.action.setBadgeBackgroundColor({tabId:details.tabId,color:'#6450d8'});
    }
  });
}, {urls:['http://*/*','https://*/*']}, ['responseHeaders']);
chrome.tabs.onRemoved.addListener(tabId=>{chrome.storage.session.remove(String(tabId));});
chrome.tabs.onUpdated.addListener((tabId, change)=>{
  if(!change.url)return;
  enqueue(tabId,async()=>{
    const key=String(tabId), state=await chrome.storage.session.get(key);
    if(state[key] && state[key].page!==change.url){await chrome.storage.session.remove(key);await chrome.action.setBadgeText({tabId,text:''});}
  });
});

// Store only the original page and an account fingerprint; media and site cookies stay in Chrome.
const journalBase='https://videodrop.13-140-180-98.sslip.io';
const journalPrefix='local-history:';
async function tokenFingerprint(token){
  const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token));
  return Array.from(new Uint8Array(hash),n=>n.toString(16).padStart(2,'0')).join('');
}
function journalPage(value){
  const url=new URL(value);
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw new Error('Invalid page');
  const query=new URLSearchParams();
  for(const key of ['v','id'])for(const value of url.searchParams.getAll(key))
    if(/^[a-zA-Z0-9_-]{1,80}$/.test(value))query.append(key,value);
  url.search=query.toString();url.hash='';return url.href;
}
async function journalPrepare(id,page,title,downloadId=null){
  if(!/^[a-f0-9]{32}$/.test(id)||typeof title!=='string')throw new Error('Invalid download');
  const {serverToken}=await chrome.storage.local.get('serverToken');
  if(!serverToken)return;
  const record={eventId:id,url:journalPage(page),title:title.replace(/[\x00-\x1f]/g,' ').slice(0,200),
    height:0,account:await tokenFingerprint(serverToken),created:Date.now(),ready:false,downloadId};
  await chrome.storage.local.set({[journalPrefix+id]:record});
}
async function journalFlush(key,record){
  const {serverToken}=await chrome.storage.local.get('serverToken');
  // Never assign a previously downloaded item to a different signed-in user.
  if(!serverToken||await tokenFingerprint(serverToken)!==record.account){await chrome.storage.local.remove(key);return;}
  const body={eventId:record.eventId,url:record.url,title:record.title,height:record.height};
  try{
    const response=await fetch(journalBase+'/api/local-history',{method:'POST',headers:{Authorization:'Bearer '+serverToken,'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(20000)});
    if(response.ok||[400,403].includes(response.status))await chrome.storage.local.remove(key);
  }catch{} // Keep pending records to retry after the next extension opening.
}
async function journalReady(id,height=0){
  const key=journalPrefix+id,record=(await chrome.storage.local.get(key))[key];
  if(!record)return;
  record.ready=true;record.height=Number.isInteger(height)&&height>=0&&height<=8640?height:0;
  await chrome.storage.local.set({[key]:record});await journalFlush(key,record);
}
async function journalCheck(downloadId){
  const all=await chrome.storage.local.get(null);
  for(const [key,record] of Object.entries(all)){
    if(!key.startsWith(journalPrefix)||record?.downloadId!==downloadId)continue;
    const [item]=await chrome.downloads.search({id:downloadId});
    if(item?.state==='complete')await journalReady(record.eventId);
    else if(!item||item.state==='interrupted')await chrome.storage.local.remove(key);
  }
}
async function journalRetry(){
  const all=await chrome.storage.local.get(null);
  for(const [key,record] of Object.entries(all)){
    if(!key.startsWith(journalPrefix))continue;
    if(!record||Date.now()-record.created>86400000){await chrome.storage.local.remove(key);continue;}
    if(record.ready)await journalFlush(key,record);
    else if(record.downloadId!==null)await journalCheck(record.downloadId);
  }
}
chrome.downloads.onChanged.addListener(change=>{if(change.state)journalCheck(change.id).catch(()=>{});});
chrome.runtime.onStartup.addListener(()=>{journalRetry().catch(()=>{});});
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id||!sender.url?.startsWith(chrome.runtime.getURL('')))return;
  const run=async()=>{
    if(message?.type==='prepare-local-download')await journalPrepare(message.eventId,message.page,message.title);
    else if(message?.type==='record-local-download')await journalReady(message.eventId,message.height);
    else if(message?.type==='track-local-download'){
      if(!Number.isInteger(message.downloadId)||message.downloadId<0)throw new Error('Invalid download');
      const id=crypto.randomUUID().replaceAll('-','');
      await journalPrepare(id,message.page,message.title,message.downloadId);
      await journalCheck(message.downloadId);
    }else if(message?.type==='retry-local-history')await journalRetry();
    else return;
  };
  run().then(()=>respond({ok:true})).catch(()=>respond({ok:false}));return true;
});
