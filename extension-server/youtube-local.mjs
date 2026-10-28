// Direct browser-only YouTube extraction for public streams that have direct AVC/AAC URLs.
// No cookies, player scripts or media data are sent to the VideoDrop server.
import {Input, UrlSource, ALL_FORMATS, Output, Mp4OutputFormat, StreamTarget,
  EncodedVideoPacketSource, EncodedAudioPacketSource, EncodedPacketSink} from './vendor/mediabunny.min.mjs';

const client={clientName:'VISIONOS',clientVersion:'1.02',deviceMake:'Apple',deviceModel:'RealityDevice17,1',
  userAgent:'Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15',
  osName:'visionOS',osVersion:'26.5.23O471',hl:'en'};

export function validMediaURL(value){
  try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password&&!u.port&&
    u.hostname.endsWith('.googlevideo.com')&&u.hostname!=='.googlevideo.com';}catch{return false;}
}
export function selectYoutubeFormats(data){
  if(data?.playabilityStatus?.status&&data.playabilityStatus.status!=='OK')
    throw new Error('YouTube пока не разрешает получить видео.');
  const list=data?.streamingData?.adaptiveFormats;
  if(!Array.isArray(list))throw new Error('YouTube пока не разрешает получить видео.');
  const audio=list.filter(f=>validMediaURL(f.url)&&/^audio\/mp4\s*;.*mp4a\.40\.2/i.test(f.mimeType||''))
    .sort((a,b)=>(b.bitrate||0)-(a.bitrate||0))[0];
  if(!audio)throw new Error('YouTube не предоставил совместимый звук.');
  const options=list.filter(f=>validMediaURL(f.url)&&/^video\/mp4\s*;.*avc[13]\.(?:42|4d|58|64)[0-9a-f]{4}/i.test(f.mimeType||'')&&Number.isInteger(f.width)&&Number.isInteger(f.height)&&f.width>0&&f.height>0)
    .sort((a,b)=>b.height-a.height||b.width-a.width||(b.fps||0)-(a.fps||0)||(b.bitrate||0)-(a.bitrate||0));
  const heights=new Set();
  return options.filter(f=>{const key=f.width+'x'+f.height;if(heights.has(key))return false;heights.add(key);return true;})
    .map(video=>{const a=Number(audio.contentLength),v=Number(video.contentLength);return {video,audio,width:video.width,height:video.height,
      size:Number.isSafeInteger(a)&&a>0&&Number.isSafeInteger(v)&&v>0?a+v:null,supported:true};});
}
export async function inspectYoutube(id,visitor,fetchImpl=fetch){
  if(!/^[a-zA-Z0-9_-]{11}$/.test(id)||typeof visitor!=='string'||!/^[a-zA-Z0-9_%=-]{1,2048}$/.test(visitor))
    throw new Error('Не удалось получить данные страницы YouTube. Обновите страницу.');
  const response=await fetchImpl('https://www.youtube.com/youtubei/v1/player?prettyPrint=false',{
    method:'POST',headers:{'Content-Type':'application/json','X-Goog-Visitor-Id':visitor,
      'X-YouTube-Client-Name':'101','X-YouTube-Client-Version':'1.02'},
    body:JSON.stringify({videoId:id,context:{client}}),credentials:'omit',redirect:'error',signal:AbortSignal.timeout(20000)});
  if(!response.ok)throw new Error('YouTube временно недоступен.');
  const options=selectYoutubeFormats(await response.json());
  if(!options.length)throw new Error('YouTube не предоставил совместимое качество.');
  return {input:{dispose(){}},options};
}
// Googlevideo may stall on unbounded range requests. Read in bounded chunks while
// presenting one continuous 206 response to the demuxer.
export function youtubeMediaFetch(root,fetchImpl=fetch,taskSignal){
  const origin=new URL(root).origin,chunkSize=1024*1024;
  return async(request,options={})=>{
    const value=typeof request==='string'||request instanceof URL?request:request.url;
    if(!validMediaURL(value)||new URL(value).origin!==origin)throw new Error('Источник видео изменился.');
    const headers=new Headers(options.headers),match=/^bytes=(\d+)-$/.exec(headers.get('range')||'');
    if(!match)throw new Error('Неподдерживаемый запрос видео.');
    const start=Number(match[1]);if(!Number.isSafeInteger(start))throw new Error('Некорректный диапазон.');
    let position=start,total=null,closed=false;
    const signals=[];if(options.signal)signals.push(options.signal);if(taskSignal)signals.push(taskSignal);
    const signal=signals.length?AbortSignal.any(signals):undefined;
    async function chunk(){
      const partHeaders=new Headers(headers);partHeaders.set('Range',`bytes=${position}-${position+chunkSize-1}`);
      const chunkSignal=signal?AbortSignal.any([signal,AbortSignal.timeout(30000)]):AbortSignal.timeout(30000);
      let target=String(value),response;
      for(let redirects=0;redirects<=3;redirects++){
        response=await fetchImpl(target,{...options,headers:partHeaders,credentials:'omit',redirect:'manual',signal:chunkSignal});
        if(![301,302,303,307,308].includes(response.status))break;
        const location=response.headers.get('location');
        if(!location||redirects===3)throw new Error('Источник видео перенаправил запрос.');
        const next=new URL(location,target);
        if(!validMediaURL(next.href))throw new Error('Источник видео изменился.');
        if(response.body)await response.body.cancel();
        target=next.href;
      }
      const range=/^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('content-range')||'');
      if(response.status!==206||!range)throw new Error('Источник не поддерживает загрузку по частям.');
      const first=Number(range[1]),last=Number(range[2]),length=Number(range[3]);
      if(first!==position||last<first||last-first+1>chunkSize||length<=last||!Number.isSafeInteger(length)||(total!==null&&total!==length))throw new Error('Источник вернул некорректную часть видео.');
      const bytes=new Uint8Array(await response.arrayBuffer());
      if(bytes.length!==last-first+1)throw new Error('Часть видео получена не полностью.');
      total=length;position=last+1;return bytes;
    }
    const first=await chunk();
    const stream=new ReadableStream({start(controller){controller.enqueue(first);},async pull(controller){
      if(closed||position>=total){controller.close();return;}
      try{controller.enqueue(await chunk());}catch(error){controller.error(error);}
    },cancel(){closed=true;}});
    return new Response(stream,{status:206,headers:{'Content-Range':`bytes ${start}-${total-1}/${total}`,
      'Content-Length':String(total-start),'Accept-Ranges':'bytes','Content-Type':'video/mp4'}});
  };
}
function mediaInput(url,taskSignal,fetchImpl=fetch){
  if(!validMediaURL(url))throw new Error('Недопустимый источник видео.');
  return new Input({source:new UrlSource(url,{requestInit:{credentials:'omit'},maxCacheSize:16*1024*1024,
    getRetryDelay:n=>!taskSignal.aborted&&n<2?1:null,fetchFn:youtubeMediaFetch(url,fetchImpl,taskSignal)}),formats:ALL_FORMATS});
}
export async function saveYoutube(_input,selected,writable,progress=()=>{}){
  if(!selected?.supported||!validMediaURL(selected.video?.url)||!validMediaURL(selected.audio?.url))throw new Error('Качество недоступно.');
  const task=new AbortController();let timer;
  function alive(){clearTimeout(timer);timer=setTimeout(()=>task.abort(),120000);}
  alive();
  const videoInput=mediaInput(selected.video.url,task.signal),audioInput=mediaInput(selected.audio.url,task.signal);
  let output;
  try{
    const video=await videoInput.getPrimaryVideoTrack(),audio=await audioInput.getPrimaryAudioTrack();
    if(!video||!audio||await video.getCodec()!=='avc'||await audio.getCodec()!=='aac')throw new Error('Не удалось проверить видео и звук.');
    const [vconfig,aconfig,rotation]=await Promise.all([video.getDecoderConfig(),audio.getDecoderConfig(),video.getRotation()]);
    if(!/^avc[13]\.(42|4d|58|64)[0-9a-f]{4}$/i.test(vconfig?.codec||'')||aconfig?.codec!=='mp4a.40.2')throw new Error('Формат несовместим с обычными проигрывателями.');
    output=new Output({format:new Mp4OutputFormat({fastStart:false}),target:new StreamTarget(writable,{chunked:true})});
    const vsource=new EncodedVideoPacketSource('avc'),asource=new EncodedAudioPacketSource('aac');
    output.addVideoTrack(vsource,{rotation});output.addAudioTrack(asource);await output.start();
    const duration=Math.max(Number(selected.video.approxDurationMs)||0,Number(selected.audio.approxDurationMs)||0)/1000;
    async function pump(track,source,decoderConfig,update){
      const sink=new EncodedPacketSink(track);let count=0;
      for await(const packet of sink.packets()){
        await source.add(packet,{decoderConfig});count++;alive();
        if(update&&duration>0)progress(Math.max(0,Math.min(0.99,(packet.timestamp+packet.duration)/duration)));
      }
      if(!count)throw new Error('Дорожка видео или звука пуста.');
      source.close();
    }
    await Promise.all([pump(video,vsource,vconfig,true),pump(audio,asource,aconfig,false)]);
    await output.finalize();progress(1);
  }catch(error){if(output)try{await output.cancel();}catch{}throw error;}
  finally{clearTimeout(timer);task.abort();videoInput.dispose();audioInput.dispose();}
}
