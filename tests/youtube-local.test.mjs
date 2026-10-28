import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {inspectYoutube,selectYoutubeFormats,validMediaURL,youtubeMediaFetch} from '../extension-server/youtube-local.mjs';
const require=createRequire(import.meta.url),bridge=require('../extension-server/bridge.js');
const url='https://r1.example.googlevideo.com/videoplayback?sig=test';
const fixture={playabilityStatus:{status:'OK'},streamingData:{adaptiveFormats:[
  {url,mimeType:'video/mp4; codecs="avc1.64001F"',width:1280,height:720,bitrate:700000,contentLength:'5000000'},
  {url,mimeType:'video/mp4; codecs="avc1.640028"',width:1920,height:1080,bitrate:1200000,contentLength:'9000000'},
  {url,mimeType:'video/mp4; codecs="av01.0.08M.08"',width:3840,height:2160,contentLength:'20000000'},
  {url,mimeType:'audio/mp4; codecs="mp4a.40.2"',bitrate:128000,contentLength:'1000000'}]}};
test('only real current YouTube pages can start direct YouTube extraction',()=>{
  assert.equal(bridge.youtubeId('https://www.youtube.com/watch?v=q9H63HxY3ag'),'q9H63HxY3ag');
  assert.equal(bridge.youtubeId('https://youtube.com/shorts/q9H63HxY3ag'),'q9H63HxY3ag');
  assert.equal(bridge.youtubeId('https://youtu.be/q9H63HxY3ag'),'q9H63HxY3ag');
  for(const url of ['https://youtube.com.evil.test/watch?v=q9H63HxY3ag','https://youtube.com/@channel','http://youtube.com/watch?v=q9H63HxY3ag','https://youtube.com/shorts/too-short'])assert.equal(bridge.youtubeId(url),null);
});
test('only compatible signed video and audio sources on Google video CDN are selected',()=>{
  assert.equal(validMediaURL(url),true);
  for(const url of ['https://googlevideo.com.evil.test/x','https://evil.test/x','http://r1.googlevideo.com/x','https://user@r1.googlevideo.com/x'])assert.equal(validMediaURL(url),false);
  const options=selectYoutubeFormats(fixture);
  assert.deepEqual(options.map(x=>x.height),[1080,720]);
  assert.equal(options[0].size,10000000);
  assert.throws(()=>selectYoutubeFormats({...fixture,playabilityStatus:{status:'LOGIN_REQUIRED'}}),/YouTube/);
  assert.throws(()=>selectYoutubeFormats({streamingData:{adaptiveFormats:[fixture.streamingData.adaptiveFormats[0]]}}),/звук/);
});
test('player request stays in browser and does not send site cookies',async()=>{
  let request;
  const result=await inspectYoutube('q9H63HxY3ag','visitor-token',async(url,options)=>{request={url,options};return {ok:true,json:async()=>fixture};});
  assert.equal(result.options.length,2);
  assert.equal(new URL(request.url).origin,'https://www.youtube.com');
  assert.equal(request.options.credentials,'omit');
  assert.equal(request.options.redirect,'error');
  assert.equal(JSON.parse(request.options.body).videoId,'q9H63HxY3ag');
  assert.equal(Object.keys(request.options.headers).some(k=>k.toLowerCase()==='cookie'),false);
  await assert.rejects(()=>inspectYoutube('invalid','visitor',()=>{}));
});
test('large YouTube responses are read in bounded ranges without cookies',async()=>{
  const bytes=new Uint8Array(2500000);for(let i=0;i<bytes.length;i++)bytes[i]=i%251;
  const requests=[];
  const underlying=async(url,options)=>{
    const range=new Headers(options.headers).get('range'),m=/^bytes=(\d+)-(\d+)$/.exec(range);
    assert.ok(m);const start=Number(m[1]),end=Math.min(Number(m[2]),bytes.length-1);
    requests.push({start,end,credentials:options.credentials,redirect:options.redirect});
    return new Response(bytes.subarray(start,end+1),{status:206,headers:{'Content-Range':`bytes ${start}-${end}/${bytes.length}`}});
  };
  const fetch=youtubeMediaFetch(url,underlying);
  const response=await fetch(url,{headers:{Range:'bytes=500000-'}});
  assert.equal(response.headers.get('Content-Range'),'bytes 500000-2499999/2500000');
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()),bytes.subarray(500000));
  assert.equal(requests.length,2);
  assert.ok(requests.every(r=>r.end-r.start+1<=1048576&&r.credentials==='omit'&&r.redirect==='manual'));
});
test('interrupted chunks are retried from the same offset without losing bytes',async()=>{
  const bytes=new Uint8Array(1500000);for(let i=0;i<bytes.length;i++)bytes[i]=i%251;
  const calls=[];let failures=0;
  const wrapped=youtubeMediaFetch(url,async(_target,options)=>{
    const range=new Headers(options.headers).get('range');calls.push(range);
    const m=/^bytes=(\d+)-(\d+)$/.exec(range),start=Number(m[1]),end=Math.min(Number(m[2]),bytes.length-1);
    if(start===1048576&&failures++===0)throw new TypeError('connection lost');
    if(start===1048576&&failures===2)return new Response(null,{status:503});
    return new Response(bytes.subarray(start,end+1),{status:206,headers:{'Content-Range':`bytes ${start}-${end}/${bytes.length}`}});
  });
  const response=await wrapped(url,{headers:{Range:'bytes=0-'}});
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()),bytes);
  assert.deepEqual(calls,['bytes=0-1048575','bytes=1048576-2097151','bytes=1048576-2097151','bytes=1048576-2097151']);
});
test('invalid content ranges and permanent errors are not retried',async()=>{
  for(const response of [()=>new Response(null,{status:403}),()=>new Response(new Uint8Array([1]),{status:206,headers:{'Content-Range':'bytes 9-9/10'}})]){
    let calls=0;
    const wrapped=youtubeMediaFetch(url,async()=>{calls++;return response();});
    await assert.rejects(()=>wrapped(url,{headers:{Range:'bytes=0-'}}));
    assert.equal(calls,1);
  }
});
test('temporary failures stop after three attempts',async()=>{
  let calls=0;
  const wrapped=youtubeMediaFetch(url,async()=>{calls++;return new Response(null,{status:503});});
  await assert.rejects(()=>wrapped(url,{headers:{Range:'bytes=0-'}}),/соединение/);
  assert.equal(calls,3);
});
test('media redirects are restricted to HTTPS Google video hosts',async()=>{
  const fetch=youtubeMediaFetch(url,async()=>new Response(null,{status:302,headers:{Location:'https://evil.example/collect'}}));
  await assert.rejects(()=>fetch(url,{headers:{Range:'bytes=0-'}}),/Источник видео изменился/);
  const hidden=youtubeMediaFetch(url,async()=>new Response(null,{status:302}));
  await assert.rejects(()=>hidden(url,{headers:{Range:'bytes=0-'}}),/перенаправил/);
  let count=0;
  const allowed=youtubeMediaFetch(url,async(target,options)=>{
    count++;
    if(count===1)return new Response(null,{status:302,headers:{Location:'https://r2.googlevideo.com/file'}});
    assert.equal(new URL(target).hostname,'r2.googlevideo.com');
    assert.equal(options.credentials,'omit');
    return new Response(new Uint8Array([1,2,3]),{status:206,headers:{'Content-Range':'bytes 0-2/3'}});
  });
  assert.deepEqual(new Uint8Array(await (await allowed(url,{headers:{Range:'bytes=0-'}})).arrayBuffer()),new Uint8Array([1,2,3]));
});
test('opaque browser redirects are recovered only for this extension and approved CDN',async()=>{
  let listener,removed=0,count=0;
  globalThis.chrome={runtime:{getURL:path=>'chrome-extension://myextension/'+path},webRequest:{onHeadersReceived:{
    addListener(fn){listener=fn;},removeListener(fn){assert.equal(listener,fn);listener=null;removed++;}}}};
  try{
    const fetch=youtubeMediaFetch(url,async(target,options)=>{
      count++;
      if(count===1){
        listener({initiator:'https://other.example',url:target,method:'GET',statusCode:302,responseHeaders:[{name:'Location',value:'https://evil.example/'}]});
        assert.ok(listener);
        listener({initiator:'chrome-extension://myextension',url:target,method:'GET',statusCode:302,responseHeaders:[{name:'Location',value:'https://r2.googlevideo.com/file'}]});
        return {type:'opaqueredirect',status:0,headers:new Headers(),body:null};
      }
      assert.equal(new URL(target).hostname,'r2.googlevideo.com');assert.equal(options.credentials,'omit');
      return new Response(new Uint8Array([4,5,6]),{status:206,headers:{'Content-Range':'bytes 0-2/3'}});
    });
    assert.deepEqual(new Uint8Array(await (await fetch(url,{headers:{Range:'bytes=0-'}})).arrayBuffer()),new Uint8Array([4,5,6]));
    assert.equal(listener,null);assert.equal(removed,2);
    const blocked=youtubeMediaFetch(url,async(target)=>{
      listener({initiator:'chrome-extension://myextension',url:target,method:'GET',statusCode:302,responseHeaders:[{name:'Location',value:'https://evil.example/collect'}]});
      return {type:'opaqueredirect',status:0,headers:new Headers(),body:null};
    });
    await assert.rejects(()=>blocked(url,{headers:{Range:'bytes=0-'}}),/перенаправил/);
    assert.equal(listener,null);
  }finally{delete globalThis.chrome;}
});
test('redirect observer stays active while a slow fetch is in progress',async()=>{
  let listener,first=true;
  globalThis.chrome={runtime:{getURL:path=>'chrome-extension://myextension/'+path},webRequest:{onHeadersReceived:{
    addListener(fn){listener=fn;},removeListener(){listener=null;}}}};
  try{
    const fetch=youtubeMediaFetch(url,async target=>{
      if(first){first=false;await new Promise(resolve=>setTimeout(resolve,1600));
        assert.ok(listener);listener({initiator:'chrome-extension://myextension',url:target,method:'GET',statusCode:302,responseHeaders:[{name:'Location',value:'https://r2.googlevideo.com/file'}]});
        return {type:'opaqueredirect',status:0,headers:new Headers(),body:null};}
      return new Response(new Uint8Array([7]),{status:206,headers:{'Content-Range':'bytes 0-0/1'}});
    });
    assert.deepEqual(new Uint8Array(await (await fetch(url,{headers:{Range:'bytes=0-'}})).arrayBuffer()),new Uint8Array([7]));
  }finally{delete globalThis.chrome;}
});
