import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

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


if __name__ == '__main__':
    unittest.main()
