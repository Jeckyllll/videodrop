import functools
import http.server
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]


class LocalStreamTests(unittest.TestCase):
    def test_extension_archive_contains_local_media_library(self):
        import zipfile
        from scripts.build_server_extension import build
        with tempfile.TemporaryDirectory() as tmp:
            archive=build(ROOT,Path(tmp)/'extension.zip')
            with zipfile.ZipFile(archive) as package:
                names=set(package.namelist())
                self.assertIn('stream-local.html',names)
                self.assertIn('stream-core.mjs',names)
                self.assertIn('youtube-local.mjs',names)
                self.assertIn('vendor/mediabunny.min.mjs',names)
                self.assertIn('vendor/MEDIABUNNY-LICENSE',names)
                self.assertNotIn('server/data/secret.key',names)

    def test_hls_to_local_mp4_has_video_and_audio(self):
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name,dimensions in [('high','128x96'),('low','64x48')]:
                subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i',f'testsrc=size={dimensions}:rate=10',
                    '-f','lavfi','-i','sine=frequency=440','-t','2','-c:v','libx264','-pix_fmt','yuv420p',
                    '-c:a','aac','-f','hls','-hls_time','1','-hls_segment_type','fmp4',
                    '-hls_fmp4_init_filename',name+'-init.mp4','-hls_playlist_type','vod',str(folder/(name+'.m3u8'))],check=True)
            (folder/'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=128x96,CODECS="avc1.64000a,mp4a.40.2"\nhigh.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=64x48,CODECS="avc1.64000a,mp4a.40.2"\nlow.m3u8\n')
            class SilentHandler(http.server.SimpleHTTPRequestHandler):
                def log_message(self,*args): pass
            handler=functools.partial(SilentHandler,directory=tmp)
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            result=folder/'result.mp4'
            script=folder/'test.mjs'
            script.write_text('''
import fs from 'node:fs';
import {inspectStream,saveStream} from %s;
const {input,options}=await inspectStream(process.argv[2]);
const selected=options.find(x=>x.supported && x.height===48);
if(!selected) throw Error('no playable video and audio');
const fd=fs.openSync(process.argv[3],'w');
const stream=new WritableStream({write(chunk){fs.writeSync(fd,chunk.data,0,chunk.data.length,chunk.position);},close(){fs.closeSync(fd);},abort(){fs.closeSync(fd);}});
await saveStream(input,selected,stream); input.dispose();
''' % json.dumps((ROOT/'extension-server/stream-core.mjs').as_uri()))
            try:
                process=subprocess.run(['node',str(script),f'http://127.0.0.1:{server.server_port}/master.m3u8',str(result)],capture_output=True,text=True,timeout=60)
                self.assertEqual(process.returncode,0,process.stderr)
                decoded=subprocess.run([ffmpeg,'-hide_banner','-i',str(result),'-f','null','-'],capture_output=True,text=True)
                self.assertEqual(decoded.returncode,0)
                self.assertIn('Video: h264',decoded.stderr)
                self.assertIn('Audio: aac',decoded.stderr)
                self.assertIn('64x48',decoded.stderr)
            finally:
                server.shutdown(); server.server_close()

    def test_youtube_separate_video_and_audio_are_muxed_to_playable_mp4(self):
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);video=folder/'video.mp4';audio=folder/'audio.m4a';result=folder/'result.mp4'
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=160x120:rate=25','-t','3',
                '-c:v','libx264','-pix_fmt','yuv420p','-an','-movflags','+faststart',str(video)],check=True)
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','sine=frequency=440','-t','3',
                '-c:a','aac','-vn','-movflags','+faststart',str(audio)],check=True)
            script=folder/'test.mjs'
            script.write_text('''
import fs from 'node:fs';
import {saveYoutube} from %s;
const inputs={video:fs.readFileSync(process.argv[2]),audio:fs.readFileSync(process.argv[3])};
globalThis.fetch=async (request,options={})=>{
 const url=new URL(typeof request==='string'?request:request.url);
 if(url.hostname!=='media.googlevideo.com')throw Error('unexpected network origin');
 const data=inputs[url.searchParams.get('track')];if(!data)throw Error('unexpected input');
 const range=new Headers(options.headers).get('range');
 let start=0,end=data.length-1;
 if(range){const m=/^bytes=(\\d+)-(\\d*)$/.exec(range);if(!m)throw Error('unexpected range');start=Number(m[1]);if(m[2])end=Math.min(Number(m[2]),end);}
 const headers={'Content-Length':String(end-start+1),'Accept-Ranges':'bytes','Content-Type':'video/mp4'};
 if(range)headers['Content-Range']=`bytes ${start}-${end}/${data.length}`;
 return new Response(options.method==='HEAD'?null:data.subarray(start,end+1),{status:range?206:200,headers});
};
const selected={supported:true,video:{url:'https://media.googlevideo.com/file?track=video',approxDurationMs:'3000'},audio:{url:'https://media.googlevideo.com/file?track=audio',approxDurationMs:'3000'}};
const fd=fs.openSync(process.argv[4],'w');
const stream=new WritableStream({write(chunk){fs.writeSync(fd,chunk.data,0,chunk.data.length,chunk.position);},close(){fs.closeSync(fd);},abort(){fs.closeSync(fd);}});
await saveYoutube(null,selected,stream);
''' % json.dumps((ROOT/'extension-server/youtube-local.mjs').as_uri()))
            run=subprocess.run(['node',str(script),str(video),str(audio),str(result)],capture_output=True,text=True,timeout=60)
            self.assertEqual(run.returncode,0,run.stderr)
            decoded=subprocess.run([ffmpeg,'-hide_banner','-i',str(result),'-f','null','-'],capture_output=True,text=True)
            self.assertEqual(decoded.returncode,0,decoded.stderr)
            self.assertIn('Video: h264',decoded.stderr)
            self.assertIn('Audio: aac',decoded.stderr)
            self.assertIn('160x120',decoded.stderr)


if __name__ == '__main__':
    unittest.main()
