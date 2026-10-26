"""Original Instagram photos, using the same access checks as the video extractor."""
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.extractor.instagram import InstagramIE
from yt_dlp.networking import Request

import worker


class InstagramImagesIE(InstagramIE):
    def _extract_product(self, product, *args, **kwargs):
        self.product = product[0] if isinstance(product, list) else product
        # Let InstagramIE finish its normal public/session access checks. Photos
        # have no video formats, which the ordinary video result would reject.
        return {'_type': 'playlist', 'entries': []}


def photo_urls(product):
    """Keep carousel order; never turn a video's cover into a downloaded photo."""
    items = product.get('carousel_media') or [product]
    if len(items) > 100:
        raise ValueError('В одном посте поддерживается до 100 изображений.')
    urls = []
    for item in items:
        if item.get('media_type') != 1:
            continue
        candidates = [c for c in item.get('image_versions2', {}).get('candidates', [])
                      if isinstance(c.get('url'), str) and c['url'].startswith('https://')]
        if not candidates:
            raise ValueError('Instagram не отдал оригинал одного из изображений.')
        # The web response lists the original first and often omits dimensions.
        # The authenticated response also supplies dimensions: use the largest.
        def area(c):
            w, h = c.get('width'), c.get('height')
            return w*h if isinstance(w, int) and isinstance(h, int) and w>0 and h>0 else 0
        urls.append(max(candidates, key=area)['url'])
    return urls


def validate_image(path):
    try:
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror',
                        '-protocol_whitelist', 'file,pipe', '-i', str(path),
                        '-map', '0:v:0', '-frames:v', '1', '-f', 'null', '-'],
                       capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Не удалось проверить изображение. Отправка остановлена.') from None


def instagram(request):
    """Return a photo result, or None for a post containing only video."""
    source = request['source']
    with yt_dlp.YoutubeDL(worker.options(source)) as ydl:
        ydl.cookiejar = worker.cookie_jar(source.get('cookies', []))
        extractor = InstagramImagesIE(ydl)
        extractor.extract(source['url'])
        urls = photo_urls(extractor.product)
        if not urls:
            return None
        if request['mode'] == 'inspect':
            return {'title': 'Изображения поста', 'kind': 'images',
                    'heights': [], 'duration': 0, 'imageCount': len(urls)}
        directory = Path(request['directory'])
        directory.mkdir(parents=True, exist_ok=True)
        files = []
        extensions = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                      'image/avif': '.avif', 'image/gif': '.gif'}
        for number, url in enumerate(urls, 1):
            # Cookies remain in the scoped jar; none are copied to CDN headers.
            with ydl.urlopen(Request(url, headers={'Referer': 'https://www.instagram.com/'})) as response:
                mime = response.headers.get('Content-Type', '').split(';')[0].lower()
                if mime not in extensions:
                    raise ValueError('Вместо изображения Instagram вернул другой ответ.')
                path = directory/f'{number:03}{extensions[mime]}'
                partial = path.with_suffix(path.suffix+'.part')
                size = 0
                with partial.open('wb') as output:
                    while chunk := response.read(65536):
                        size += len(chunk)
                        if size > 64*1024*1024:
                            raise ValueError('Изображение превышает лимит 64 МБ.')
                        output.write(chunk)
            validate_image(partial)
            partial.replace(path)
            files.append(path)
        return {'paths': [str(p) for p in files],
                'size': sum(p.stat().st_size for p in files), 'kind': 'images'}
