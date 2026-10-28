// Local-only HLS media assembly. The source is selected from the active tab.
import {Input, UrlSource, HLS_FORMATS, Output, Mp4OutputFormat, StreamTarget, Conversion} from './vendor/mediabunny.min.mjs';

export function checkedFetch(root, fetchImpl=fetch) {
  const origin = new URL(root).origin;
  return (request, options={}) => {
    const url = new URL(typeof request === 'string' || request instanceof URL ? request : request.url);
    // Do not let a playlist use the extension's broad permissions to make requests
    // to unrelated origins, or follow a redirect outside the selected origin.
    if (!['http:', 'https:'].includes(url.protocol) || url.origin !== origin)
      throw new Error('Поток обращается к другому сайту и пока не поддерживается.');
    const signals=[AbortSignal.timeout(30000)];
    if (options.signal) signals.push(options.signal);
    return fetchImpl(request, {...options, redirect:'error',signal:AbortSignal.any(signals)});
  };
}

export async function inspectStream(url) {
  const parsed=new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || !/\.m3u8$/i.test(parsed.pathname))
    throw new Error('Нужен обнаруженный на странице поток HLS.');
  const input=new Input({source:new UrlSource(url,{
    fetchFn:checkedFetch(url), requestInit:{credentials:'include'},
    getRetryDelay:n=>n<2?1:null, maxCacheSize:16*1024*1024,
  }),formats:HLS_FORMATS});
  try {
    const videos=await input.getVideoTracks();
    const options=await Promise.all(videos.map(async track=>{
      const audio=await track.getPrimaryPairableAudioTrack();
      const [width,height,codec,audioCodec,videoRate,videoAverage,audioRate,duration,videoConfig,audioConfig]=await Promise.all([
        track.getDisplayWidth(),track.getDisplayHeight(),track.getCodec(),
        audio?audio.getCodec():null,track.getBitrate(),track.getAverageBitrate(),audio?audio.getBitrate():null,
        track.getDurationFromMetadata(),track.getDecoderConfig(),audio?audio.getDecoderConfig():null,
      ]);
      // HLS master bandwidth can include its alternate audio; estimates are not exact.
      const rate=videoAverage||videoRate;
      const size=duration&&rate?Math.round(duration*(rate+(audioRate||0))/8):null;
      const ordinaryAvc=/^avc[13]\.(42|4d|58|64)[0-9a-f]{4}$/i.test(videoConfig?.codec||'');
      return {track,audio,width,height,codec,audioCodec,size,
        supported:codec==='avc'&&audioCodec==='aac'&&ordinaryAvc&&audioConfig?.codec==='mp4a.40.2'&&!!audio};
    }));
    options.sort((a,b)=>b.height-a.height||b.width-a.width);
    if (!options.length) throw new Error('В потоке не найдена видеодорожка.');
    return {input,options};
  } catch(error) { input.dispose(); throw error; }
}

export async function saveStream(input, selected, writable, progress=()=>{}) {
  if (!selected?.supported) throw new Error('Этот поток пока нельзя сохранить как совместимый MP4 со звуком.');
  const output=new Output({format:new Mp4OutputFormat({fastStart:false}),target:new StreamTarget(writable,{chunked:true})});
  try {
    const conversion=await Conversion.init({input,output,tracks:'all',
      video:track=>({discard:track!==selected.track}),
      audio:track=>({discard:track!==selected.audio}),
      copy:{mode:'forced'},showWarnings:false});
    if (!conversion.isValid || !conversion.utilizedTracks.includes(selected.track) ||
        !conversion.utilizedTracks.includes(selected.audio))
      throw new Error('Не удалось объединить видео и звук в MP4.');
    conversion.onProgress=progress;
    await conversion.execute();
  } catch(error) {
    try { await output.cancel(); } catch {}
    throw error;
  }
}
