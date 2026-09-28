"""Employee package boundaries and a real download through the Lite server."""
import importlib.util
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import imageio_ffmpeg
import updater
from scripts.build_release import ROOT, build

VERSION = updater.read_json(ROOT / 'version.json')['version']


class LitePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.archive = build(ROOT, self.root / 'Lite.zip', 'lite')

    def tearDown(self):
        self.temp.cleanup()

    def test_lite_has_no_upload_modules_controls_privileged_permissions_or_user_data(self):
        with zipfile.ZipFile(self.archive) as z:
            names = set(z.namelist())
            self.assertFalse(names & updater.UPLOAD_FILES)
            self.assertNotIn('extension/config.js', names)
            self.assertFalse(any(n.startswith(('.state/', '.venv/', 'Загрузки/')) for n in names))
            config = json.loads(z.read('version.json'))
            self.assertEqual((config['edition'], config['asset'], config['port']), ('lite', 'VideoDrop-Lite-mac.zip', 8766))
            manifest = json.loads(z.read('extension/manifest.json'))
            self.assertNotIn('debugger', manifest['permissions'])
            self.assertNotIn('alarms', manifest['permissions'])
            self.assertNotIn('externally_connectable', manifest)
            self.assertEqual(manifest['host_permissions'], ['http://127.0.0.1:8766/*'])
            for name in ('web/index.html', 'extension/popup.html'):
                html = z.read(name).decode()
                self.assertIn('VideoDrop Lite', html)
                self.assertNotIn('youtube-ui', html)
                self.assertNotIn('youtube-settings', html)
                self.assertNotIn('YouTube Studio', html)

    def test_cross_edition_updates_rejected_before_any_file_is_written(self):
        full = build(ROOT, self.root / 'Full.zip')
        for archive, edition in ((full, 'lite'), (self.archive, 'full')):
            with self.assertRaises(ValueError):
                updater.unpack_package(archive, self.root / 'absent', VERSION, 'Jeckyllll/videodrop', edition)
            self.assertFalse((self.root / 'absent').exists())
        files = updater.unpack_package(self.archive, self.root / 'lite', VERSION, 'Jeckyllll/videodrop', 'lite')
        self.assertFalse(set(files) & updater.UPLOAD_FILES)

    def test_lite_selects_only_lite_asset_and_rejects_full_only_release(self):
        assets = [{'name': name, 'size': 500, 'digest': 'sha256:' + 'a'*64,
                   'browser_download_url': f'https://github.com/Jeckyllll/videodrop/releases/download/v{VERSION}/{name}'}
                  for name in updater.ASSETS.values()]
        release = {'tag_name': 'v'+VERSION, 'assets': assets}
        with patch.object(updater, 'github_fetch', return_value=json.dumps(release).encode()):
            self.assertTrue(updater.latest_release('Jeckyllll/videodrop', 'lite')['url'].endswith('VideoDrop-Lite-mac.zip'))
            self.assertTrue(updater.latest_release('Jeckyllll/videodrop', 'full')['url'].endswith('/VideoDrop-mac.zip'))
        release['assets'] = assets[:1]
        with patch.object(updater, 'github_fetch', return_value=json.dumps(release).encode()), self.assertRaises(ValueError):
            updater.latest_release('Jeckyllll/videodrop', 'lite')

    def test_lite_cannot_rollback_to_full(self):
        stage = self.root / 'lite'
        updater.unpack_package(self.archive, stage, VERSION, 'Jeckyllll/videodrop', 'lite')
        backup = self.root / 'backup'; updater.write_json(backup / 'version.json', {'edition':'full'})
        updater.write_json(stage / '.state/updates/previous.json', {'folder':str(backup), 'version':'1.4.0'})
        instance = updater.Updater(stage, threading.RLock(), {}, lambda: None, lambda: None)
        with self.assertRaises(ValueError): instance.install(rollback=True)

    def test_full_archive_retains_upload_features(self):
        with zipfile.ZipFile(build(ROOT, self.root / 'Full.zip')) as z:
            self.assertTrue(updater.UPLOAD_FILES <= set(z.namelist()))
            self.assertEqual(json.loads(z.read('version.json'))['edition'], 'full')
            self.assertIn('debugger', json.loads(z.read('extension/manifest.json'))['permissions'])
            self.assertIn('youtube-settings', z.read('web/index.html').decode())


class LiteHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        archive = build(ROOT, cls.root / 'Lite.zip', 'lite')
        cls.install = cls.root / 'install'
        updater.unpack_package(archive, cls.install, VERSION, 'Jeckyllll/videodrop', 'lite')
        spec = importlib.util.spec_from_file_location('lite_app_test', cls.install / 'app.py')
        cls.app = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.app)
        cls.app.initialize()
        cls.server = ThreadingHTTPServer(('127.0.0.1',0), cls.app.Handler)
        cls.app.PORT = cls.server.server_address[1]; cls.app.BASE = f'http://127.0.0.1:{cls.app.PORT}'
        cls.app.UPDATES = updater.Updater(cls.install, cls.app.LOCK, cls.app.JOBS, cls.app.STUDIO.persist, lambda:None)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.app.DOWNLOAD_QUEUE.shutdown(wait=True); cls.app.INSPECT_QUEUE.shutdown(wait=True)
        cls.temp.cleanup()

    def request(self, path, body=None, extra=None):
        headers = {'X-VideoDrop-Token':self.app.TOKEN, **(extra or {})}
        if body is not None: headers['Content-Type'] = 'application/json'
        return urlopen(Request(self.app.BASE + path, headers=headers,
                              data=json.dumps(body).encode() if body is not None else None), timeout=5)

    def test_no_upload_endpoints_or_hidden_upload_settings(self):
        with self.request('/api/bootstrap') as r:
            self.assertEqual(json.load(r)['edition'], 'lite')
        for path in ('/api/studio/status', '/api/studio/connect', '/api/studio/heartbeat', '/api/studio/claim',
                     '/api/studio/event', '/api/studio/connected', '/api/upload-youtube'):
            with self.subTest(path=path), self.assertRaises(HTTPError) as raised:
                self.request(path, None if path.endswith('/status') else {})
            self.assertEqual(raised.exception.code, 404)
        for settings in ({'enabled':True}, {'enabled':False,'deleteLocal':True}):
            with self.assertRaises(HTTPError) as raised:
                self.request('/api/import', {'url':'https://example.com/video.mp4','storage':settings})
            self.assertEqual(raised.exception.code, 400)
        self.assertNotIn('studio', vars(self.app))
        for path in ('/youtube-ui.js','/youtube-ui.css'):
            with self.assertRaises(HTTPError) as raised: self.request(path)
            self.assertEqual(raised.exception.code,404)

    def test_lite_heartbeat_without_debugger_or_studio(self):
        headers = {'X-VideoDrop-Extension':'a'*32, 'Origin':'chrome-extension://'+'a'*32}
        with self.request('/api/extension/heartbeat', {'version':VERSION,'edition':'lite'}, headers) as r:
            self.assertTrue(json.load(r)['ok'])
        with self.assertRaises(HTTPError):
            self.request('/api/extension/heartbeat', {'version':VERSION,'edition':'full'}, headers)

    def test_real_download_has_quality_formats_open_file_and_local_history_only(self):
        media = self.root / 'sample.mp4'
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=10',
                        '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libx264','-pix_fmt','yuv420p',
                        '-c:a','aac','-shortest',str(media)], check=True)
        subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(media),'-c','copy',
                        '-hls_time','1','-hls_list_size','0','-f','hls',str(self.root/'index.m3u8')], check=True)
        (self.root/'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=320x180\nindex.m3u8\n')
        directory = self.root
        class MediaHandler(SimpleHTTPRequestHandler):
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(directory),**kwargs)
            def log_message(self,*args):pass
        source = ThreadingHTTPServer(('127.0.0.1',0), MediaHandler)
        threading.Thread(target=source.serve_forever,daemon=True).start()
        try:
            # Seed only the source URL; execute the real worker and the real /api/download route.
            url = f'http://127.0.0.1:{source.server_address[1]}/master.m3u8'
            job = self.app.new_job('inspect', {'url':url,'storage':self.app.STUDIO.settings(None)})
            self.app.work(job, {})
            self.assertEqual(job['status'], 'done', job)
            self.assertIn(180,job['result']['heights'])
            with self.request('/api/download', {'sourceId':job['result']['sourceId'],'format':'mp4','height':180}) as r:
                job_id = json.load(r)['jobId']
            end = time.monotonic()+20
            while time.monotonic()<end and self.app.JOBS[job_id]['status'] in ('queued','working'):time.sleep(.05)
            finished = self.app.JOBS[job_id]
            self.assertEqual(finished['status'],'done',finished)
            path = self.app.DOWNLOADS / finished['result']['filename']
            self.assertTrue(path.is_file())
            self.assertTrue(finished['result']['localKept'])
            self.assertFalse(finished['storage']['enabled'])
            with patch.object(self.app.subprocess,'run') as player:
                with self.request('/api/open-video', {'jobId':job_id}) as r:self.assertTrue(json.load(r)['ok'])
                player.assert_called_once()
            self.assertTrue((self.install/'.state/downloads.json').is_file())
            self.assertFalse((self.install/'.state/studio-jobs.json').exists())
            self.app.JOBS.clear();self.app.STUDIO.restore()
            self.assertIn(job_id,self.app.JOBS)
            self.assertTrue(path.is_file())
        finally:
            source.shutdown();source.server_close()


if __name__ == '__main__':unittest.main()
