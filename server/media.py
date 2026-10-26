"""Bounded media child process. Never pass credentials on command lines."""
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def public_url(value, resolve=True):
    if not isinstance(value, str) or len(value) > 16000 or any(ord(c) < 32 for c in value):
        raise ValueError('Некорректная ссылка.')
    p = urlsplit(value.strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise ValueError('Нужна ссылка http:// или https:// без логина и нестандартного порта.')
    if p.hostname.lower() in ('localhost','metadata.google.internal') or p.hostname.endswith(('.local','.localhost')):
        raise ValueError('Локальные адреса недоступны.')
    try:
        addresses = [ipaddress.ip_address(p.hostname)]
    except ValueError:
        addresses = [ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)] if resolve else []
    if any(not a.is_global for a in addresses):
        raise ValueError('Ссылка ведёт во внутреннюю сеть.')
    return value.strip()


def clean_source(data, resolve=True):
    url = public_url(data.get('url'), resolve)
    referer = public_url(data['referer'], resolve) if data.get('referer') else ''
    hosts = {urlsplit(u).hostname.lower() for u in (url, referer) if u}
    cookies = []
    if not isinstance(data.get('cookies', []), list):
        raise ValueError('Некорректные cookies.')
    for c in data.get('cookies', [])[:300]:
        if not isinstance(c, dict):
            continue
        domain = str(c.get('domain','')).lower().lstrip('.')
        if not domain or not any(h == domain or (not c.get('hostOnly') and h.endswith('.'+domain)) for h in hosts):
            continue
        if not isinstance(c.get('name'),str) or not isinstance(c.get('value'),str):
            continue
        if any('\n' in str(v) or '\r' in str(v) for v in c.values()):
            continue
        cookies.append({k:c[k] for k in ('domain','name','value','path','secure','hostOnly','expirationDate') if k in c})
    ua = str(data.get('userAgent',''))[:1000]
    if '\n' in ua or '\r' in ua:
        raise ValueError('Некорректный User-Agent.')
    return {'url':url,'referer':referer,'cookies':cookies,'userAgent':ua}


def guard_network():
    original = socket.getaddrinfo
    def checked(host, port, *args, **kwargs):
        result = original(host, port, *args, **kwargs)
        if any(not ipaddress.ip_address(r[4][0]).is_global for r in result):
            raise OSError('Private network is unavailable to media downloads')
        return result
    socket.getaddrinfo = checked


def image_post(url):
    p = urlsplit(url)
    host = p.hostname.lower()
    return ((host == 'instagram.com' or host.endswith('.instagram.com')) and p.path.startswith('/p/')) or (
            (host == 'tiktok.com' or host.endswith('.tiktok.com')) and '/photo/' in p.path)


def friendly_error(error):
    import worker
    message = str(error).lower()
    if 'not a bot' in message:
        return 'YouTube запросил подтверждение входа с IP сервера. Этот ролик сейчас не удалось скачать. Подключение канала для загрузки на YouTube не снимает это ограничение скачивания.'
    if 'tiktok' in message and 'unexpected response from webpage request' in message:
        return 'TikTok не отдал доступные данные ролика серверу. Сейчас скачать его не удалось. Попробуйте позже или пришлите другую ссылку.'
    if any(s in message for s in ('403', '401', 'login', 'sign in', 'private video', 'password')):
        return 'Источник требует входа или подтверждения доступа. В текущем режиме бот не получает авторизацию из вашего браузера. Закрытые материалы по одной ссылке недоступны.'
    if 'unsupported url' in message or 'no video formats' in message:
        return 'По этой ссылке доступное видео не найдено. Пришлите прямую ссылку на конкретное видео или пост.'
    if any(s in message for s in ('timed out', 'unable to download', 'connection', 'resolve')):
        return 'Сервер не смог получить медиа с площадки. Попробуйте позже или пришлите другую ссылку.'
    return worker.friendly_error(error)


def validate_download(path, request):
    """Inspect the actual file, rather than trusting its .mp4 extension or extractor metadata."""
    try:
        result = subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
            '-show_entries','stream=codec_name,codec_type,width,height:stream_disposition=attached_pic:format=duration',
            '-of','json',str(path)],capture_output=True,text=True,timeout=30,check=True)
        info = json.loads(result.stdout)
    except (OSError,ValueError,subprocess.SubprocessError):
        raise ValueError('Не удалось проверить готовый медиафайл. Отправка остановлена.') from None
    streams = info.get('streams',[])
    video = next((s for s in streams if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')),None)
    audio = next((s for s in streams if s.get('codec_type')=='audio'),None)
    if request['format'] in ('mp4','mkv') and not video:
        raise ValueError('В готовом файле нет видеодорожки. Такой файл не будет отправлен как видео.')
    if request['format'] in ('mp3','m4a') and not audio:
        raise ValueError('В готовом файле нет аудиодорожки.')
    try:
        duration = float(info.get('format',{}).get('duration'))
        if not math.isfinite(duration) or duration<=0:
            duration = None
    except (TypeError,ValueError):
        duration = None
    media = {'videoCodec':video.get('codec_name') if video else None,
        'audioCodec':audio.get('codec_name') if audio else None,
        'duration':duration,
        'width':video.get('width') if video else None,'height':video.get('height') if video else None,
        'quicktimeWarning':bool(request['format']=='mkv'
            or (video and video.get('codec_name') in ('vp8','vp9','av1','theora'))
            or (audio and audio.get('codec_name') in ('opus','vorbis')))}
    return {'media':media}


def run(request):
    import worker
    import yt_dlp
    guard_network()
    source = clean_source(request['source'])
    request['source'] = source
    if image_post(source['url']):
        if request['mode'] == 'inspect':
            worker.emit('result', title='Изображения поста', kind='images', heights=[], duration=0)
            return
        import gallery_dl
        import gallery_dl.config
        import gallery_dl.job
        directory = Path(request['directory'])
        directory.mkdir(parents=True, exist_ok=True)
        gallery_dl.config.clear()
        gallery_dl.config.set(('extractor',), 'base-directory', str(directory))
        gallery_dl.config.set(('extractor',), 'directory', [])
        gallery_dl.config.set(('extractor',), 'filename', '{num:>03}.{extension}')
        gallery_dl.config.set(('extractor',), 'postprocessors', [])
        gallery_dl.config.set(('extractor',), 'image-filter', "extension in ('jpg','jpeg','png','webp','avif','gif')")
        gallery_dl.config.set(('extractor',), 'retries', 2)
        gallery_dl.config.set(('extractor',), 'timeout', 30)
        gallery_dl.config.set(('extractor',), 'skip', False)
        gallery_dl.config.set(('extractor',), 'image-range', '1-100')
        # gallery-dl logs may contain signed URLs, so never forward them to users.
        import logging
        logging.disable(logging.CRITICAL)
        import tempfile
        with tempfile.TemporaryDirectory(prefix='videodrop-cookies-') as temporary:
            cookie_path = Path(temporary)/'cookies.txt'
            worker.cookie_jar(source['cookies']).save(str(cookie_path),ignore_discard=True,ignore_expires=True)
            os.chmod(cookie_path,0o600)
            gallery_dl.config.set(('extractor',), 'cookies', str(cookie_path))
            status = gallery_dl.job.DownloadJob(source['url']).run()
        files = sorted(p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in ('.jpg','.jpeg','.png','.webp','.avif','.gif'))
        if status or not files:
            raise ValueError('Не удалось получить картинки. Пост может требовать входа: в текущем режиме бот не получает авторизацию из браузера.')
        worker.emit('result', paths=[str(p) for p in files], size=sum(p.stat().st_size for p in files), kind='images')
        return
    if request['mode'] == 'inspect':
        with yt_dlp.YoutubeDL(worker.options(source)) as ydl:
            ydl.cookiejar = worker.cookie_jar(source['cookies'])
            info = worker.extract_info(ydl, source)
            result = worker.metadata(info, ydl)
            result.update(kind='video', size=info.get('filesize') or info.get('filesize_approx'))
            worker.emit('result', **result)
        return
    worker.run(request, validate_download=validate_download)


if __name__ == '__main__':
    try:
        run(json.load(sys.stdin))
    except Exception as e:
        import worker
        worker.emit('error', message=friendly_error(e))
        sys.exit(1)
