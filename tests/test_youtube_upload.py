import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import requests

from cryptography.fernet import Fernet
from server.youtube import YouTube


class YouTubeUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root/'video.mp4'
        self.video.write_bytes(b'video-with-sound')
        self.youtube = YouTube(self.root, Fernet(Fernet.generate_key()), '')
        self.youtube.channel.write_text(json.dumps({'id':'channel-1'}))
        self.session = Mock()
        self.youtube.session = Mock(return_value=self.session)
        self.session.post.return_value = Mock(headers={'Location':'https://www.googleapis.com/upload/session'})
        self.session.put.return_value = Mock(status_code=201, **{'json.return_value':{'id':'video-1'}})

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, privacy):
        self.session.get.return_value = Mock(**{'json.return_value':{'items': [{
            'snippet':{'channelId':'channel-1'},
            'status':{'privacyStatus':privacy,'uploadStatus':'processed'},
            'processingDetails':{'processingStatus':'succeeded'},
        }]}})

    def test_requests_unlisted_and_verifies_the_result_before_success(self):
        self.status('unlisted')
        checkpoints = []
        result = self.youtube.upload(self.video,'Название',lambda kind,value:checkpoints.append(kind),lambda:False)
        self.assertEqual(result,'video-1')
        self.assertEqual(self.session.post.call_args.kwargs['json']['status']['privacyStatus'],'unlisted')
        self.assertEqual(self.session.post.call_args.kwargs['params']['notifySubscribers'],'false')
        self.assertEqual(checkpoints,['session','video'])
        self.session.get.assert_called_once()

    def test_does_not_report_success_if_youtube_returns_public_video(self):
        self.status('public')
        with self.assertRaisesRegex(ValueError,'доступ'):
            self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)

    def test_private_video_does_not_generate_unlisted_success(self):
        self.status('private')
        with self.assertRaisesRegex(ValueError,'доступ'):
            self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)

    def test_partial_chunk_resumes_at_confirmed_byte(self):
        self.status('unlisted')
        self.session.put.side_effect=[Mock(status_code=308,headers={'Range':'bytes=0-4'}),
            Mock(status_code=201,**{'json.return_value':{'id':'video-1'}})]
        result=self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)
        self.assertEqual(result,'video-1')
        calls=self.session.put.call_args_list
        self.assertEqual(calls[0].kwargs['headers']['Content-Range'],'bytes 0-15/16')
        self.assertEqual(calls[1].kwargs['headers']['Content-Range'],'bytes 5-15/16')
        self.assertFalse(calls[0].kwargs['allow_redirects'])
        self.session.post.assert_called_once()

    def test_timeout_probes_same_session_before_resuming(self):
        self.status('unlisted')
        self.session.put.side_effect=[requests.Timeout(),Mock(status_code=308,headers={'Range':'bytes=0-4'}),
            Mock(status_code=201,**{'json.return_value':{'id':'video-1'}})]
        with patch('server.youtube.time.sleep'):
            self.assertEqual(self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False),'video-1')
        calls=self.session.put.call_args_list
        self.assertEqual(calls[1].kwargs['headers']['Content-Range'],'bytes */16')
        self.assertEqual(calls[1].kwargs['data'],b'')
        self.assertEqual(calls[2].kwargs['headers']['Content-Range'],'bytes 5-15/16')
        self.assertEqual(len({c.args[0] for c in calls}),1)
        self.session.post.assert_called_once()

    def test_probe_can_confirm_already_completed_upload_without_duplicate(self):
        self.status('unlisted')
        self.session.put.side_effect=[requests.Timeout(),Mock(status_code=201,**{'json.return_value':{'id':'video-1'}})]
        checkpoints=[]
        with patch('server.youtube.time.sleep'):
            self.assertEqual(self.youtube.upload(self.video,'Название',lambda k,v:checkpoints.append(k),lambda:False),'video-1')
        self.assertEqual(checkpoints,['session','video'])
        self.assertEqual(self.session.put.call_count,2)
        self.session.post.assert_called_once()

    def test_nonadvancing_or_invalid_ranges_do_not_loop_or_send_a_link(self):
        self.status('unlisted')
        self.session.put.return_value=Mock(status_code=308,headers={})
        with patch('server.youtube.time.sleep'):
            with self.assertRaisesRegex(ValueError,'не продвигается'):
                self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)
        self.assertEqual(self.session.put.call_count,4)
        self.session.get.assert_not_called()
        for value in ('bytes=1-2','bytes=0-9999','nonsense'):
            with self.subTest(value=value):
                self.session.reset_mock();self.session.put.return_value=Mock(status_code=308,headers={'Range':value})
                with self.assertRaises(ValueError):self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)
                self.session.get.assert_not_called()

    def test_empty_video_does_not_start_an_upload(self):
        self.video.write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'пуст'):
            self.youtube.upload(self.video,'Название',lambda *_:None,lambda:False)
        self.session.post.assert_not_called()

    def test_repeated_server_failure_stops_without_a_second_upload(self):
        self.session.put.return_value=Mock(status_code=503,headers={})
        checkpoints=[]
        with patch('server.youtube.time.sleep'):
            with self.assertRaisesRegex(ValueError,'не подтвердил'):
                self.youtube.upload(self.video,'Название',lambda k,v:checkpoints.append(k),lambda:False)
        self.assertEqual(checkpoints,['session'])
        self.assertEqual(self.session.put.call_count,4)
        self.session.post.assert_called_once()
        self.session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
