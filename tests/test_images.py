import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.images import instagram, photo_urls, validate_image
from server.media import image_post


class InstagramPhotosTests(unittest.TestCase):
    def photo(self, url, **extra):
        return {'media_type':1,'image_versions2':{'candidates':[{'url':url,**extra}]}}

    def test_carousel_order_is_preserved_and_video_covers_are_excluded(self):
        a=self.photo('https://cdn.example/one.jpg')
        cover={**self.photo('https://cdn.example/cover.jpg'),'media_type':2}
        b=self.photo('https://cdn.example/two.jpg')
        self.assertEqual(photo_urls({'carousel_media':[a,cover,b]}),
                         ['https://cdn.example/one.jpg','https://cdn.example/two.jpg'])
        self.assertEqual(photo_urls(cover),[])

    def test_largest_photo_and_web_original_without_dimensions(self):
        product=self.photo('https://cdn.example/original.jpg')
        product['image_versions2']['candidates'].append({'url':'https://cdn.example/thumbnail.jpg'})
        self.assertEqual(photo_urls(product),['https://cdn.example/original.jpg'])
        product['image_versions2']['candidates']=[
            {'url':'https://cdn.example/small.jpg','width':640,'height':640},
            {'url':'https://cdn.example/full.jpg','width':1440,'height':1800}]
        self.assertEqual(photo_urls(product),['https://cdn.example/full.jpg'])

    def test_missing_photo_fails_instead_of_silently_sending_incomplete_carousel(self):
        with self.assertRaises(ValueError):
            photo_urls({'carousel_media':[self.photo('https://cdn.example/one.jpg'),{'media_type':1}]})

    def test_post_with_username_is_recognized_without_accepting_other_domains(self):
        self.assertTrue(image_post('https://www.instagram.com/user/p/ABC/'))
        self.assertFalse(image_post('https://instagram.com.evil.example/p/ABC/'))

    def test_download_keeps_bytes_order_and_validates_before_delivery(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('server.images.yt_dlp.YoutubeDL') as cls, \
                patch('server.images.InstagramImagesIE') as extractor, \
                patch('server.images.validate_image') as check:
            extractor.return_value.product={'carousel_media':[
                self.photo('https://cdn.example/one.jpg'),self.photo('https://cdn.example/two.jpg')]}
            def response(request):
                result=MagicMock()
                result.__enter__.return_value=result
                result.headers={'Content-Type':'image/jpeg'}
                result.read=io.BytesIO(request.url.encode()).read
                return result
            cls.return_value.__enter__.return_value.urlopen.side_effect=response
            result=instagram({'mode':'download','source':{'url':'https://www.instagram.com/p/ABC/'},'directory':directory})
            self.assertEqual([Path(p).name for p in result['paths']],['001.jpg','002.jpg'])
            self.assertEqual(Path(result['paths'][0]).read_bytes(),b'https://cdn.example/one.jpg')
            self.assertEqual(check.call_count,2)
            self.assertFalse(list(Path(directory).glob('*.part')))

    def test_login_failure_is_not_replaced_by_a_public_thumbnail(self):
        with patch('server.images.yt_dlp.YoutubeDL'), patch('server.images.InstagramImagesIE') as cls:
            cls.return_value.extract.side_effect=ValueError('Login required')
            with self.assertRaisesRegex(ValueError,'Login required'):
                instagram({'mode':'inspect','source':{'url':'https://www.instagram.com/p/ABC/'}})

    def test_corrupt_image_fails_decode(self):
        import subprocess
        with patch('server.images.subprocess.run',side_effect=subprocess.CalledProcessError(1,'ffmpeg')):
            with self.assertRaisesRegex(ValueError,'проверить изображение'):
                validate_image(Path('bad.jpg'))


if __name__=='__main__':
    unittest.main()
