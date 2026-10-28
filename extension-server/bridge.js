'use strict';
// This is the entire privilege boundary: only this server frame, and only detected URLs.
const VideoDropBridge={
  base:'https://videodrop.13-140-180-98.sslip.io',
  accepts(event,frame){return event.origin===this.base&&event.source===frame.contentWindow;},
  findSource(sources,url){return sources.find(source=>source.url===url);},
  youtubeId(url){
    try{
      const u=new URL(url),host=u.hostname.toLowerCase();
      if(u.protocol!=='https:'||u.username||u.password||u.port)return null;
      let id=null;
      if(host==='youtu.be')id=u.pathname.slice(1).split('/')[0];
      else if(['youtube.com','www.youtube.com','m.youtube.com'].includes(host)){
        if(u.pathname==='/watch')id=u.searchParams.get('v');
        else id=u.pathname.match(/^\/(?:shorts|live)\/([a-zA-Z0-9_-]{11})(?:\/|$)/)?.[1];
      }
      return /^[a-zA-Z0-9_-]{11}$/.test(id||'')?id:null;
    }catch{return null;}
  },
  directMedia(url){
    try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)&&/\.(?:mp4|webm|mov)$/i.test(u.pathname)&&!u.searchParams.has('range')&&!u.searchParams.has('bytestart');}catch{return false;}
  },
  hlsMedia(url){
    try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)&&/\.m3u8$/i.test(u.pathname);}catch{return false;}
  },
  scopedCookies(cookies,urls){
    const hosts=urls.map(url=>new URL(url).hostname.toLowerCase());
    const scoped=cookies.filter(cookie=>{const domain=String(cookie.domain||'').replace(/^\./,'').toLowerCase();return domain&&hosts.some(host=>host===domain||(!cookie.hostOnly&&host.endsWith('.'+domain)));});
    return [...new Map(scoped.map(cookie=>[cookie.domain+'|'+cookie.path+'|'+cookie.name,cookie])).values()];
  }
};
if(typeof module!=='undefined')module.exports=VideoDropBridge;
