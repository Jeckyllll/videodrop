import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import app
import studio

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'testVideo01'
EXTENSION = 'a' * 32


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads'; self.downloads.mkdir()
        self.path = self.downloads / 'Урок.mp4'; self.path.write_bytes(b'test video content')
        self.jobs = {}
        self.broker = studio.Broker(self.jobs, threading.RLock(), self.root / 'state', self.downloads)
        self.broker.heartbeat(EXTENSION, studio.PROTOCOL)
        self.broker.connect()
        request = self.broker.claim()['connect']
        self.broker.connected({'nonce': request['nonce'], 'channelId': CHANNEL, 'channelTitle': 'Канал'})

    def options(self, **changes):
        return {'provider': 'studio', 'enabled': True, 'deleteLocal': False,
                'channelId': CHANNEL, 'madeForKids': False, **changes}

    def job(self, **changes):
        job = {'id': str(len(self.jobs)), 'kind': 'download', 'status': 'done', 'created': time.time(),
               'cancel': False, 'title': 'Урок', 'format': 'mp4', 'height': 720,
               'storage': self.options(**changes), 'result': {'filename': self.path.name, 'localKept': True, 'size': self.path.stat().st_size}}
        self.jobs[job['id']] = job
        return job

    def claimed(self, **changes):
        job = self.job(**changes); self.broker.enqueue(job)
        task = self.broker.claim()['job']
        return job, {'jobId': task['id'], 'lease': task['lease']}

    def proof(self, **changes):
        return {'videoId': VIDEO, 'channelId': CHANNEL, 'filename': self.path.name,
                'privacy': 'unlisted', 'processed': True, 'saved': True,
                'url': f'https://studio.youtube.com/video/{VIDEO}/edit', **changes}

    def finish(self, lease, **proof):
        self.broker.event({**lease, 'event': 'submitted'})
        self.broker.event({**lease, 'event': 'video', 'videoId': VIDEO})
        return self.broker.event({**lease, 'event': 'complete', 'proof': self.proof(**proof)})

    def test_settings_reset_delete_and_reject_legacy_or_wrong_channel(self):
        self.assertFalse(self.broker.settings({'enabled': False, 'deleteLocal': True})['deleteLocal'])
        for value in ({'provider': 'youtube', 'enabled': True}, self.options(channelId='wrong'), {'enabled': 'true'}):
            with self.assertRaises(ValueError): self.broker.settings(value)
        self.broker.extension['seen'] -= 80
        with self.assertRaises(ValueError): self.broker.settings(self.options())

    def test_connection_is_expiring_one_time_and_switch_is_blocked_during_upload(self):
        with self.assertRaises(ValueError): self.broker.connected({'nonce': 'forged', 'channelId': CHANNEL})
        self.broker.connect(); request = self.broker.claim()['connect']
        self.broker.connect_request['created'] -= 601
        with self.assertRaises(ValueError): self.broker.connected({**request, 'channelId': CHANNEL})
        self.broker.connect_request = None
        self.claimed()
        with self.assertRaises(ValueError): self.broker.connect()
        with self.assertRaises(ValueError): self.broker.disconnect()

    def test_cancel_connection_keeps_existing_channel_and_rejects_late_response(self):
        self.broker.connect(); request = self.broker.claim()['connect']
        self.assertFalse(self.broker.connect_status(request)['cancelled'])
        status = self.broker.cancel_connect()
        self.assertFalse(status['connecting'])
        self.assertEqual(status['channelId'], CHANNEL)
        self.assertTrue(self.broker.connect_status(request)['cancelled'])
        self.assertEqual(self.broker.claim(), {})
        with self.assertRaises(ValueError):
            self.broker.connected({**request, 'channelId': 'UC' + 'b' * 22})
        self.assertEqual(self.broker.channel()['id'], CHANNEL)

    def test_connection_without_chrome_expires_in_45_seconds_including_reopened_status(self):
        self.broker.extension = {}
        self.broker.connect()
        self.broker.connect_request['created'] -= 46
        status = self.broker.status()
        self.assertFalse(status['connecting'])
        self.assertIn('45 секунд', status['connectError'])
        self.assertEqual(self.broker.claim(), {})
        self.assertEqual(self.broker.status()['connectError'], status['connectError'])
        self.assertEqual(self.broker.connect()['connectError'], '')

    def test_duplicate_connect_keeps_nonce_and_claimed_login_gets_longer_timeout(self):
        self.broker.connect(); request = self.broker.claim()['connect']
        self.broker.connect()
        self.assertEqual(self.broker.connect_request['nonce'], request['nonce'])
        self.assertEqual(self.broker.claim(), {})
        self.broker.connect_request['created'] -= 46
        self.assertTrue(self.broker.status()['connecting'])
        self.broker.connect_request['created'] -= 600
        self.assertTrue(self.broker.connect_status(request)['cancelled'])
        self.assertFalse(self.broker.status()['connecting'])
        self.assertIn('Время входа', self.broker.status()['connectError'])

    def test_failed_channel_switch_reports_error_without_disconnecting_old_channel(self):
        self.broker.connect(); request = self.broker.claim()['connect']
        self.broker.connected({**request, 'error': True})
        status = self.broker.status()
        self.assertFalse(status['connecting'])
        self.assertEqual(status['channelId'], CHANNEL)
        self.assertIn('Отладчик', status['connectError'])

    def test_upload_claim_is_unique_and_only_returns_saved_file(self):
        job, lease = self.claimed()
        self.assertEqual(self.broker.claim(), {})
        with self.assertRaises(ValueError): self.broker.event({**lease, 'lease': 'forged', 'event': 'submitted'})
        self.broker.event({**lease, 'event': 'submitted'})
        with self.assertRaises(ValueError): self.broker.event({**lease, 'event': 'submitted'})
        self.assertTrue(job['_studio']['submitted'])

    def test_completed_upload_keeps_local_by_default(self):
        job, lease = self.claimed()
        self.finish(lease)
        self.assertEqual(job['status'], 'done')
        self.assertTrue(job['result']['youtubeVerified'])
        self.assertTrue(self.path.exists())
        self.assertEqual(job['result']['youtubeId'], VIDEO)

    def test_deletes_only_confirmed_copy_and_updates_original_record(self):
        old = self.job()
        job, lease = self.claimed(deleteLocal=True)
        self.finish(lease)
        self.assertFalse(self.path.exists())
        self.assertFalse(old['result']['localKept'])
        self.assertEqual(old['result']['youtubeId'], VIDEO)
        self.assertFalse(job['uploading'])

    def test_incomplete_or_wrong_evidence_never_deletes(self):
        for proof in ({'privacy': 'private'}, {'privacy': 'public'}, {'processed': False}, {'processed': 'true'},
                      {'saved': False}, {'channelId': 'UC'+'b'*22}, {'filename': 'other.mp4'},
                      {'videoId': 'otherVid001'}, {'url': 'https://evil.example/'}):
            with self.subTest(proof=proof):
                job, lease = self.claimed(deleteLocal=True)
                self.broker.event({**lease, 'event': 'submitted'})
                self.broker.event({**lease, 'event': 'video', 'videoId': VIDEO})
                with self.assertRaises(ValueError): self.broker.event({**lease, 'event': 'complete', 'proof': self.proof(**proof)})
                self.assertTrue(self.path.exists())
                self.broker.event({**lease, 'event': 'stopped'})

    def test_file_or_channel_change_prevents_deletion(self):
        job, lease = self.claimed(deleteLocal=True)
        self.broker.event({**lease, 'event': 'submitted'})
        self.broker.event({**lease, 'event': 'video', 'videoId': VIDEO})
        self.path.write_bytes(b'a different video')
        reply = self.broker.event({**lease, 'event': 'complete', 'proof': self.proof()})
        self.assertTrue(reply['cancelled'])
        self.assertEqual(self.path.read_bytes(), b'a different video')
        job, lease = self.claimed(deleteLocal=True)
        studio.write_json(self.root/'state/studio-channel.json', {'id':'UC'+'b'*22, 'title':'Other'})
        self.assertTrue(self.broker.event({**lease, 'event':'progress'})['cancelled'])
        self.assertTrue(self.path.exists())

    def test_cancel_timeout_and_closed_browser_keep_local(self):
        job, lease = self.claimed(deleteLocal=True)
        job['cancel'] = True
        self.assertTrue(self.broker.event({**lease, 'event': 'complete', 'proof': self.proof()})['cancelled'])
        self.broker.event({**lease, 'event': 'stopped'})
        self.assertEqual(job['status'], 'cancelled')
        self.assertTrue(self.path.exists())
        job, lease = self.claimed(deleteLocal=True)
        job['_studioSeen'] -= 101; self.broker.maintenance()
        self.assertEqual(job['status'], 'error')
        self.assertTrue(self.path.exists())
        with self.assertRaises(ValueError): self.broker.event({**lease, 'event': 'complete', 'proof': self.proof()})

    def test_no_blind_reupload_after_ambiguous_file_submission(self):
        job, lease = self.claimed()
        self.broker.event({**lease, 'event':'submitted'})
        self.broker.event({**lease, 'event':'stopped'})
        with self.assertRaisesRegex(ValueError, 'дубликат'): self.broker.enqueue(job)
        job['_studio']['videoId'] = VIDEO
        self.broker.enqueue(job)
        claimed = self.broker.claim()['job']
        self.assertEqual(claimed['videoId'], VIDEO)
        self.assertTrue(claimed['submitted'])

    def test_cancel_before_chrome_claims_releases_queue(self):
        job = self.job(); self.broker.enqueue(job)
        job.update(cancel=True, status='cancelled')
        self.broker.cancel(job)
        self.assertEqual(self.broker.claim(), {})
        self.assertTrue(self.broker.connect()['connecting'])

    def test_history_survives_restart_without_secrets_or_automatic_retry(self):
        job, lease = self.claimed()
        job['source']={'cookies':['secret'], 'url':'signed-token'}
        self.broker.event({**lease, 'event':'submitted'})
        self.broker.event({**lease, 'event':'video', 'videoId':VIDEO})
        saved = (self.root/'state/studio-jobs.json').read_text()
        for private in ('signed-token', 'cookies', lease['lease']): self.assertNotIn(private, saved)
        restored = studio.Broker({}, threading.RLock(), self.root/'state', self.downloads)
        restored.restore()
        copy = restored.jobs[job['id']]
        self.assertEqual(copy['status'], 'error')
        self.assertFalse(copy['uploading'])
        self.assertEqual(copy['_studio']['videoId'], VIDEO)
        self.assertEqual((self.root/'state/studio-jobs.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('_studio', app.job_view(job))
        self.assertNotIn(lease['lease'], json.dumps(app.job_view(job)))

    def test_path_traversal_symlink_and_audio_rejected(self):
        outside = self.root/'outside.mp4';outside.write_bytes(b'outside')
        (self.downloads/'link.mp4').symlink_to(outside)
        for filename in ('../outside.mp4', str(outside), 'link.mp4', 'audio.mp3', 'missing.mp4'):
            job = self.job();job['result']['filename']=filename
            with self.assertRaises(ValueError): self.broker.enqueue(job)
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_http_bridge_auth_and_old_google_endpoints_removed(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None, headers=None):
            data=json.dumps(body).encode() if body is not None else None
            return urlopen(Request(base+path, data=data, headers={'Content-Type':'application/json', **(headers or {})}), timeout=5)
        with patch.object(app,'PORT',server.server_port),patch.object(app,'BASE',base),patch.object(app,'TOKEN','test'),patch.object(app,'STUDIO',self.broker):
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                for headers in ({}, {'X-VideoDrop-Token':'test'}, {'X-VideoDrop-Token':'test','X-VideoDrop-Extension':EXTENSION,'Origin':'https://evil.example'}):
                    with self.assertRaises(HTTPError) as error:request('/api/studio/claim',{},headers)
                    self.assertEqual(error.exception.code,403)
                headers={'X-VideoDrop-Token':'test','X-VideoDrop-Extension':EXTENSION,'Origin':'chrome-extension://'+EXTENSION}
                with request('/api/studio/heartbeat',{'protocol':1},headers) as response:self.assertTrue(json.load(response)['ready'])
                self.broker.connect(); pending = self.broker.claim()['connect']
                with request('/api/studio/connect-status', pending, headers) as response:self.assertFalse(json.load(response)['cancelled'])
                with self.assertRaises(HTTPError) as error:request('/api/studio/connect-status', pending, {'X-VideoDrop-Token':'test'})
                self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError) as error:request('/api/studio/cancel-connect', {})
                self.assertEqual(error.exception.code,403)
                with request('/api/studio/cancel-connect', {}, {'X-VideoDrop-Token':'test'}) as response:self.assertFalse(json.load(response)['connecting'])
                with request('/api/studio/connect-status', pending, headers) as response:self.assertTrue(json.load(response)['cancelled'])
                for path in ('/api/youtube/status','/oauth/youtube/callback','/api/cloud/status'):
                    with self.assertRaises(HTTPError) as error:request(path,headers=headers)
                    self.assertEqual(error.exception.code,404)
            finally:server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
