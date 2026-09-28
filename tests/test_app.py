import http.cookiejar
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import imageio_ffmpeg
from worker import cookie_jar, friendly_error


class ValidationTests(unittest.TestCase):
    def test_rejects_non_web_and_local_urls(self):
        for value in ('file:///etc/passwd', 'https://u:p@example.com', 'http://127.0.0.1/a', 'http://[::1]/a', 'http://localhost/a', 'javascript:alert(1)', 'https://example.com/\nheader'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.validate_url(value)

    def test_cookies_are_scoped_and_headers_checked(self):
        valid={'domain':'.example.com','name':'session','value':'ok','path':'/'}
        other={'domain':'.unrelated.com','name':'secret','value':'no'}
        source=app.clean_source({'url':'https://media.example.com/v.mp4','cookies':[valid,other]})
        self.assertEqual(source['cookies'],[valid])
        with self.assertRaises(ValueError):
            app.clean_source({'url':'https://example.com','userAgent':'agent\nInjected: 1'})

    def test_cookiejar_implements_ytdlp_header_api(self):
        jar=cookie_jar([{'domain':'example.com','hostOnly':True,'name':'session','value':'test','path':'/','secure':True}])
        self.assertEqual(jar.get_cookie_header('https://example.com/file'),'session=test')
        self.assertIsNone(jar.get_cookie_header('https://other.example/file'))

    def test_signed_urls_not_leaked_in_errors(self):
        self.assertNotIn('secret',friendly_error('failure https://cdn.example/video?token=secret'))
        self.assertIn('DRM',friendly_error('This video has DRM'))


class HTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        app.PORT=cls.server.server_address[1]
        app.BASE=f'http://127.0.0.1:{app.PORT}'
        app.TOKEN='test-token'
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()

    def request(self,path,headers=None,body=None):
        headers=dict(headers or {})
        if body is not None:headers['Content-Type']='application/json'
        return urlopen(Request(app.BASE+path,headers=headers,data=json.dumps(body).encode() if body is not None else None),timeout=5)

    def test_api_requires_token(self):
        with self.assertRaises(HTTPError) as raised:self.request('/api/jobs')
        self.assertEqual(raised.exception.code,403)
        with self.request('/api/jobs',{'X-VideoDrop-Token':'test-token'}) as response:
            self.assertEqual(json.load(response),[])

    def test_foreign_origin_cannot_read_bootstrap_or_call_api(self):
        for path in ('/api/bootstrap','/api/jobs'):
            with self.assertRaises(HTTPError) as raised:
                self.request(path,{'Origin':'https://evil.example','X-VideoDrop-Token':'test-token'})
            self.assertEqual(raised.exception.code,403)

    def test_rebinding_host_rejected(self):
        with self.assertRaises(HTTPError) as raised:self.request('/api/bootstrap',{'Host':'evil.example'})
        self.assertEqual(raised.exception.code,403)

    def test_extension_cannot_fetch_pairing_key(self):
        with self.assertRaises(HTTPError) as raised:self.request('/api/bootstrap',{'Origin':'chrome-extension://'+'a'*32})
        self.assertEqual(raised.exception.code,403)

    def test_open_media_uses_saved_job_file_for_all_formats(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app,'DOWNLOADS',Path(directory)), patch.dict(app.JOBS,{},clear=True), patch.object(app.subprocess,'run') as launch:
            for extension in ('mp4','mkv','mp3','m4a'):
                file=Path(directory)/f'Урок 1 $(ignored).{extension}'
                file.write_bytes(b'local test media')
                app.JOBS['saved']={'kind':'download','status':'done','result':{'filename':file.name,'localKept':True}}
                with self.request('/api/open-video',{'X-VideoDrop-Token':'test-token'}, {'jobId':'saved','filename':'/etc/passwd'}) as response:
                    self.assertEqual(json.load(response),{'ok':True})
                self.assertEqual(launch.call_args.args[0],['/usr/bin/open',str(file.resolve())])
                self.assertNotIn('shell',launch.call_args.kwargs)
                self.assertTrue(file.exists())

    def test_open_media_rejects_unavailable_and_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app,'DOWNLOADS',Path(directory)/'downloads'), patch.dict(app.JOBS,{},clear=True), patch.object(app.subprocess,'run') as launch:
            app.DOWNLOADS.mkdir()
            outside=Path(directory)/'outside.mp4';outside.write_bytes(b'outside')
            (app.DOWNLOADS/'link.mp4').symlink_to(outside)
            (app.DOWNLOADS/'script.command').write_text('exit 0')
            (app.DOWNLOADS/'video.mp4').write_bytes(b'local')
            cases=[('missing.mp4','done',True),('../outside.mp4','done',True),(str(outside),'done',True),
                   ('link.mp4','done',True),('script.command','done',True),('video.mp4','working',True),
                   ('video.mp4','done',False)]
            for filename,status,kept in cases:
                with self.subTest(filename=filename,status=status,kept=kept):
                    app.JOBS['saved']={'kind':'download','status':status,'result':{'filename':filename,'localKept':kept}}
                    with self.assertRaises(HTTPError) as raised:
                        self.request('/api/open-video',{'X-VideoDrop-Token':'test-token'}, {'jobId':'saved'})
                    self.assertEqual(raised.exception.code,400)
            launch.assert_not_called()

    def test_open_media_requires_token_and_local_origin(self):
        with patch.object(app.subprocess,'run') as launch:
            for headers in ({},{'X-VideoDrop-Token':'test-token','Origin':'https://evil.example'}):
                with self.assertRaises(HTTPError) as raised:
                    self.request('/api/open-video',headers,{'jobId':'saved'})
                self.assertEqual(raised.exception.code,403)
            launch.assert_not_called()

    def test_update_install_requires_token_and_local_ui_origin(self):
        with patch.object(app, 'UPDATES') as updates:
            updates.install.return_value = {'pending': True}
            for headers in ({}, {'X-VideoDrop-Token':'test-token', 'Origin':'chrome-extension://'+'a'*32},
                            {'X-VideoDrop-Token':'test-token', 'Origin':'https://evil.example'}):
                with self.assertRaises(HTTPError) as raised:
                    self.request('/api/updates/install', headers, {})
                self.assertEqual(raised.exception.code, 403)
            updates.install.assert_not_called()
            with self.request('/api/updates/install', {'X-VideoDrop-Token':'test-token', 'Origin':app.BASE}, {}) as response:
                self.assertEqual(json.load(response), {'pending':True})
            updates.install.assert_called_once()

    def test_open_media_survives_upload_error_and_reports_player_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app,'DOWNLOADS',Path(directory)), patch.dict(app.JOBS,{},clear=True), patch.object(app.subprocess,'run') as launch:
            (Path(directory)/'video.mp4').write_bytes(b'local')
            app.JOBS['saved']={'kind':'download','status':'error','result':{'filename':'video.mp4','localKept':True}}
            with self.request('/api/open-video',{'X-VideoDrop-Token':'test-token'}, {'jobId':'saved'}) as response:
                self.assertEqual(response.status,200)
            launch.side_effect=subprocess.CalledProcessError(1,['open'])
            with self.assertRaises(HTTPError) as raised:
                self.request('/api/open-video',{'X-VideoDrop-Token':'test-token'}, {'jobId':'saved'})
            self.assertEqual(raised.exception.code,400)
            self.assertIn('плеер',json.load(raised.exception)['error'])


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        cls.ff('-f','lavfi','-i','testsrc=size=640x360:rate=10','-f','lavfi','-i','sine=frequency=440',
               '-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(cls.root/'sample.mp4'))
        for height,width in ((180,320),(360,640)):
            (cls.root/str(height)).mkdir()
            cls.ff('-i',str(cls.root/'sample.mp4'),'-vf',f'scale={width}:{height}','-c:v','libx264','-c:a','aac',
                   '-hls_time','1','-hls_list_size','0','-f','hls',str(cls.root/str(height)/'index.m3u8'))
        (cls.root/'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=180000,RESOLUTION=320x180\n180/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=640x360\n360/index.m3u8\n')
        (cls.root/'dash').mkdir()
        cls.ff('-i',str(cls.root/'sample.mp4'),'-map','0:v','-map','0:a','-c','copy','-f','dash',str(cls.root/'dash'/'manifest.mpd'))
        root=cls.root
        class MediaHandler(SimpleHTTPRequestHandler):
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(root),**kwargs)
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path.startswith('/protected.mp4'):
                    if self.headers.get('Cookie')!='session=test' or self.headers.get('Referer')!='https://lesson.example/':
                        self.send_error(403);return
                    self.path='/sample.mp4'
                super().do_GET()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),MediaHandler)
        cls.base=f'http://127.0.0.1:{cls.server.server_address[1]}'
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def ff(cls,*args):
        subprocess.run([cls.ffmpeg,'-hide_banner','-loglevel','error','-y',*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def worker(self,mode,path,**options):
        source=options.pop('source',{'url':self.base+path})
        request={'mode':mode,'source':source,'directory':str(self.root/('out-'+self._testMethodName)),**options}
        result=subprocess.run([sys.executable,str(app.ROOT/'worker.py')],input=json.dumps(request),text=True,capture_output=True,timeout=70)
        events=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual(result.returncode,0,events or result.stderr)
        return next(e for e in reversed(events) if e['event']=='result')

    def test_real_hls_quality_selection_and_audio(self):
        info=self.worker('inspect','/master.m3u8')
        self.assertEqual(info['heights'],[360,180])
        result=self.worker('download','/master.m3u8',format='mp4',height=180)
        decode=subprocess.run([self.ffmpeg,'-i',result['path'],'-f','null','-'],capture_output=True,text=True)
        self.assertEqual(decode.returncode,0,decode.stderr)
        self.assertIn('320x180',decode.stderr)
        self.assertIn('Audio:',decode.stderr)

    def test_real_mkv_remux(self):
        result=self.worker('download','/sample.mp4',format='mkv',height=0)
        self.assertTrue(result['path'].endswith('.mkv'))
        self.ff('-i',result['path'],'-f','null','-')

    def test_separate_dash_video_and_audio_are_merged(self):
        result=self.worker('download','/dash/manifest.mpd',format='mp4',height=360)
        decode=subprocess.run([self.ffmpeg,'-i',result['path'],'-f','null','-'],capture_output=True,text=True)
        self.assertEqual(decode.returncode,0,decode.stderr)
        self.assertIn('Video:',decode.stderr)
        self.assertIn('Audio:',decode.stderr)

    def test_real_mp3_conversion(self):
        result=self.worker('download','/sample.mp4',format='mp3',height=0)
        self.assertTrue(result['path'].endswith('.mp3'))
        self.ff('-i',result['path'],'-f','null','-')

    def test_real_m4a_audio(self):
        result=self.worker('download','/sample.mp4',format='m4a',height=0)
        self.assertTrue(result['path'].endswith('.m4a'))
        self.ff('-i',result['path'],'-f','null','-')

    def test_authenticated_video_cookies_and_referer(self):
        source={'url':self.base+'/protected.mp4','referer':'https://lesson.example/',
                'cookies':[{'domain':'127.0.0.1','hostOnly':True,'name':'session','value':'test','path':'/'}]}
        result=self.worker('download','',format='mp4',height=0,source=source)
        self.assertGreater(result['size'],1000)


if __name__=='__main__':unittest.main()
