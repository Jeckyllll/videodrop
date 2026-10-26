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
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import imageio_ffmpeg
import yt_dlp
from worker import cookie_jar, friendly_error, download_options, metadata, extract_info


class TikTokFallbackTests(unittest.TestCase):
    def setUp(self):
        self.source={'url':'https://www.tiktok.com/@user/video/123'}
        self.original=yt_dlp.utils.DownloadError('Unexpected response from webpage request')
        self.ydl=Mock(cookiejar=cookie_jar([]))
        self.ydl.extract_info.side_effect=self.original

    def page(self, detail, status=200):
        session=MagicMock()
        response=session.__enter__.return_value.get.return_value.__enter__.return_value
        response.status_code=status
        payload=json.dumps({'__DEFAULT_SCOPE__':{'webapp.video-detail':detail}})
        response.iter_content.return_value=[f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{payload}</script>'.encode()]
        return session

    def test_public_page_fallback_uses_native_parser_and_does_not_follow_redirects(self):
        from yt_dlp.extractor.tiktok import TikTokIE
        detail={'statusCode':0,'itemInfo':{'itemStruct':{'id':'123','video':{'height':1920}}}}
        session=self.page(detail)
        self.ydl.process_ie_result.return_value={'id':'123'}
        with patch('requests.Session',return_value=session), patch.object(TikTokIE,'_parse_aweme_video_web',return_value={'id':'123'}) as parse:
            self.assertEqual(extract_info(self.ydl,self.source),{'id':'123'})
        parse.assert_called_once()
        self.assertFalse(session.__enter__.return_value.get.call_args.kwargs['allow_redirects'])

    def test_private_wrong_id_and_login_redirect_are_not_accepted(self):
        from yt_dlp.extractor.tiktok import TikTokIE
        for detail,status in (({'statusCode':10216},200),
                ({'statusCode':0,'itemInfo':{'itemStruct':{'id':'456','video':{'height':1920}}}},200),
                ({'statusCode':0,'itemInfo':{'itemStruct':{'id':'123','video':{'height':1920},'isContentClassified':True}}},200),
                ({},302)):
            with self.subTest(detail=detail,status=status), patch('requests.Session',return_value=self.page(detail,status)), patch.object(TikTokIE,'_parse_aweme_video_web') as parse:
                with self.assertRaises(yt_dlp.utils.DownloadError): extract_info(self.ydl,self.source)
                parse.assert_not_called()

    def test_other_sites_and_authentication_errors_do_not_trigger_fallback(self):
        with patch('requests.Session') as session:
            with self.assertRaises(yt_dlp.utils.DownloadError):
                extract_info(self.ydl,{'url':'https://example.com/@user/video/123'})
            self.ydl.extract_info.side_effect=yt_dlp.utils.DownloadError('Login required')
            with self.assertRaises(yt_dlp.utils.DownloadError): extract_info(self.ydl,self.source)
            session.assert_not_called()


class WorkerProtocolTests(unittest.TestCase):
    def test_concurrent_fragment_progress_is_valid_json_lines(self):
        import io
        import time
        import worker
        class YieldingOutput(io.StringIO):
            def write(self, value):
                super().write(value)
                time.sleep(0.0001)
                return len(value)
        output=YieldingOutput()
        with patch('sys.stdout',output):
            threads=[threading.Thread(target=lambda i=i: [worker.emit('progress',stream=i,n=n) for n in range(20)]) for i in range(4)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
        events=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events),80)
        self.assertEqual({(e['stream'],e['n']) for e in events},{(i,n) for i in range(4) for n in range(20)})


class FormatSelectionTests(unittest.TestCase):
    def select(self, formats, height=0):
        with yt_dlp.YoutubeDL({'quiet':True}) as ydl:
            return ydl._select_formats(formats,ydl.build_format_selector(download_options('mp4',height)['format']))[0]

    def fmt(self, fid, ext, vcodec, acodec, height=None):
        return {'format_id':fid,'ext':ext,'vcodec':vcodec,'acodec':acodec,'height':height,
                'url':'https://example.com/'+fid,'protocol':'https'}

    def test_mp4_does_not_prefer_lower_resolution_or_worse_audio_by_container(self):
        formats=[self.fmt('aac','m4a','none','mp4a.40.2'),self.fmt('opus','webm','none','opus'),
                 self.fmt('h264-1080','mp4','avc1','none',1080),self.fmt('vp9-2160','webm','vp9','none',2160)]
        chosen=self.select(formats)
        self.assertEqual([f['format_id'] for f in chosen['requested_formats']],['vp9-2160','opus'])
        capped=self.select(formats,1080)
        self.assertEqual(capped['requested_formats'][0]['format_id'],'h264-1080')

    def test_higher_quality_combined_stream_wins_over_separate_lower_resolution(self):
        formats=[self.fmt('aac','m4a','none','aac'),self.fmt('separate-720','mp4','avc1','none',720),
                 self.fmt('combined-1080','mp4','avc1','aac',1080)]
        self.assertEqual(self.select(formats)['format_id'],'combined-1080')

    def sizes(self, formats, duration=10):
        with yt_dlp.YoutubeDL({'quiet':True}) as ydl:
            return metadata({'formats':formats,'duration':duration},ydl)['qualitySizes']

    def test_quality_sizes_follow_selected_video_and_audio_not_container(self):
        formats=[{**self.fmt('aac','m4a','none','aac'),'filesize':50},
                 {**self.fmt('opus','webm','none','opus'),'filesize':100},
                 {**self.fmt('720','mp4','avc1','none',720),'filesize':1000},
                 {**self.fmt('1080','webm','vp9','none',1080),'filesize':3000}]
        sizes=self.sizes(formats)
        self.assertEqual(sizes['0'],{'bytes':3100,'approximate':True})
        self.assertEqual(sizes['720']['bytes'],1100)

    def test_combined_stream_size_does_not_count_audio_twice(self):
        formats=[{**self.fmt('audio','m4a','none','aac'),'filesize':100},
                 {**self.fmt('720','mp4','avc1','none',720),'filesize':1000},
                 {**self.fmt('1080','mp4','avc1','aac',1080),'filesize':3000}]
        self.assertEqual(self.sizes(formats)['0']['bytes'],3000)

    def test_sizes_use_estimate_or_bitrate_but_never_omit_unknown_audio(self):
        audio=self.fmt('audio','m4a','none','aac')
        video={**self.fmt('1080','webm','vp9','none',1080),'filesize_approx':3000}
        self.assertIsNone(self.sizes([audio,video])['0']['bytes'])
        self.assertEqual(self.sizes([{**audio,'abr':128},video])['0']['bytes'],163000)
        self.assertIsNone(self.sizes([{**audio,'abr':float('nan')},video])['0']['bytes'])

    def test_missing_duration_keeps_combined_bitrate_for_later_file_probe(self):
        formats=[{**self.fmt('audio','m4a','none','aac'),'tbr':64},
                 {**self.fmt('1080','webm','vp9','none',1080),'tbr':800}]
        size=self.sizes(formats,duration=None)['0']
        self.assertIsNone(size['bytes'])
        self.assertEqual(size['bytesPerSecond'],108000)

    def test_hls_audio_without_bitrate_is_marked_for_probe_not_silently_omitted(self):
        formats=[self.fmt('audio','m4a','none','aac'),
                 {**self.fmt('1080','mp4','avc1','none',1080),'tbr':1000}]
        size=self.sizes(formats)['0']
        self.assertIsNone(size['bytes'])
        self.assertEqual(size['bytesPerSecond'],125000)
        self.assertTrue(size['needsAudioBitrate'])


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
