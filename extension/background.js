importScripts('studio-driver.js');
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
