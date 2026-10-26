"""Isolated yt-dlp worker. Requests/cookies travel over stdin, never command arguments."""
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys

import imageio_ffmpeg
import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def friendly_error(error):
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    lower = message.lower()
    if "drm" in lower:
        return "Видео защищено DRM. Этот инструмент не снимает такую защиту."
    if "not a bot" in lower:
        return "YouTube запросил подтверждение входа. Откройте ролик в Chrome, пройдите проверку на YouTube и передайте ссылку через расширение с включённым доступом к сайту."
    if "video is unavailable" in lower or "video unavailable" in lower:
        return "Это видео недоступно на видеосервисе. Проверьте ссылку в Chrome; для закрытого видео передайте доступ через расширение."
    if any(s in lower for s in ("403", "401", "login", "sign in", "private video", "password", "not a bot")):
        return "Источник требует подтверждения доступа. Откройте видео в Chrome и передайте его через расширение с включённым доступом к сайту."
    if "unsupported url" in lower or "no video formats" in lower:
        return "По этой ссылке видео не найдено. Откройте урок в Chrome, запустите видео и используйте расширение → Найти потоки."
    if "requested format" in lower:
        return "Это качество больше недоступно. Найдите видео заново или выберите «Лучшее»."
    if any(s in lower for s in ("timed out", "unable to download", "connection", "resolve")):
        return "Не удалось связаться с видеосервисом. Проверьте интернет и повторите. VPN в расширении Chrome не распространяется на локальное приложение."
    message = re.sub(r"https?://[^\s\]\)]+", "[адрес источника]", message)
    return message[-700:]


class Logger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def cookie_jar(cookies):
    jar = YoutubeDLCookieJar()
    for c in cookies:
        domain = c["domain"]
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, domain,
            not c.get("hostOnly", False), domain.startswith("."),
            c.get("path", "/"), True, c.get("secure", False),
            int(c["expirationDate"]) if c.get("expirationDate") else None,
            not bool(c.get("expirationDate")), None, None, {}, False,
        ))
    return jar


def options(source):
    node = shutil.which("node") or str(Path.home() / ".local/bin/node")
    result = {
        "logger": Logger(), "quiet": True, "no_warnings": True,
        "noplaylist": True, "extract_flat": False, "playlistend": 1,
        "socket_timeout": 25, "retries": 3, "fragment_retries": 3,
        "cachedir": False, "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "js_runtimes": {"node": {"path": node}},
        "http_headers": {},
    }
    if source.get("referer"):
        result["http_headers"]["Referer"] = source["referer"]
    if source.get("userAgent"):
        result["http_headers"]["User-Agent"] = source["userAgent"]
    return result


def unwrap(info):
    while info and info.get("entries") is not None:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise ValueError("Видео не найдено на странице.")
    if info.get("is_live"):
        raise ValueError("Это прямой эфир. Дождитесь публикации записи.")
    return info


def extract_info(ydl, source):
    try:
        return unwrap(ydl.extract_info(source['url'], download=False))
    except yt_dlp.utils.DownloadError as original:
        # TikTok sometimes returns unusable HTML to yt-dlp while its public page
        # is available via a normal GET. Keep native parsing/format selection.
        match = re.fullmatch(r'https://(?:www\.)?tiktok\.com/@[^/?]+/video/(\d+)/?(?:\?[^#]*)?',source['url'])
        if not match or 'Unexpected response from webpage request' not in str(original):
            raise
        import requests
        from yt_dlp.extractor.tiktok import TikTokIE
        try:
            with requests.Session() as session:
                session.cookies = ydl.cookiejar
                with session.get(source['url'],timeout=25,stream=True,allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise ValueError('Public page unavailable')
                    content = bytearray()
                    for chunk in response.iter_content(65536):
                        content.extend(chunk)
                        if len(content) > 6*1024*1024:
                            raise ValueError('Page too large')
            extractor = TikTokIE(ydl)
            detail = extractor._get_universal_data(content.decode('utf-8'),match[1]).get('webapp.video-detail',{})
            video = detail.get('itemInfo',{}).get('itemStruct',{})
            if (detail.get('statusCode') != 0 or str(video.get('id')) != match[1]
                    or not video.get('video') or video.get('isContentClassified') or video.get('privateItem')):
                raise ValueError('Public video unavailable')
            raw = extractor._parse_aweme_video_web(video,source['url'],match[1])
            raw.update(extractor='tiktok',extractor_key='TikTok',webpage_url=source['url'])
            return unwrap(ydl.process_ie_result(raw,download=False))
        except (requests.RequestException,ValueError,KeyError,TypeError,yt_dlp.utils.ExtractorError):
            raise original from None


def metadata(info, ydl=None):
    formats = [f for f in info.get("formats", []) if not f.get("has_drm")]
    if not formats:
        if info.get("has_drm"):
            raise ValueError("DRM protected")
        raise ValueError("No video formats")
    video = [f for f in formats if f.get("vcodec") != "none" and f.get("ext") not in ("mhtml", "json")]
    heights = sorted({int(f["height"]) for f in video if f.get("height")}, reverse=True)
    return {"title": info.get("title") or "Видео", "duration": info.get("duration"),
            "heights": heights, "hasVideo": bool(video),
            "hasAudio": any(f.get("acodec") != "none" for f in formats),
            "extractor": info.get("extractor_key", "Видео"), "formatCount": len(formats),
            "qualitySizes": quality_sizes(info, formats, heights, ydl) if ydl else {}}


def quality_sizes(info, formats, heights, ydl):
    """Use the same selector as downloads and include every selected audio/video stream.

    Final container overhead is unknown until remuxing, so preflight sizes are estimates.
    Missing one stream's size must not look like a complete, smaller file.
    """
    def positive(value):
        return isinstance(value, (int, float)) and math.isfinite(value) and value > 0

    def stream_size(stream):
        for key in ('filesize', 'filesize_approx'):
            if positive(stream.get(key)):
                return stream[key]
        duration = stream.get('duration') or info.get('duration')
        bitrate = stream.get('tbr')
        if not positive(bitrate):
            bitrate = stream.get('abr') if stream.get('vcodec') == 'none' else None
        if positive(duration) and positive(bitrate):
            return duration * bitrate * 1000 / 8

    result = {}
    for height in [0] + heights:
        selector = ydl.build_format_selector(download_options('mp4', height)['format'])
        selected = ydl._select_formats(formats, selector)
        parts = (selected[0].get('requested_formats') or [selected[0]]) if selected else []
        sizes = [stream_size(part) for part in parts]
        total = round(sum(sizes)) if sizes and all(s is not None for s in sizes) else None
        result[str(height)] = {'bytes': total, 'approximate': True}
        rates = [p.get('tbr') or (p.get('abr') if p.get('vcodec') == 'none' else None) for p in parts]
        if total is None and rates and all(positive(rate) for rate in rates):
            # Some sites omit duration. The server can fill the estimate after
            # probing the first downloaded file, without downloading each quality.
            result[str(height)]['bytesPerSecond'] = sum(rates)*1000/8
    return result


def download_options(container, height):
    if container in ("mp3", "m4a"):
        return {"format": "bestaudio/best", "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": container, "preferredquality": "192"}]}
    ceiling = "[height<=%d]" % height if height else ""
    # Quality wins over source container/QuickTime support. Remux without re-encoding.
    # bv* also preserves a better combined stream when the separate video is worse.
    selector = f"bv*{ceiling}+ba/b{ceiling}"
    return {"format": selector, "merge_output_format": container,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": container}]}


def run(request, validate_download=None):
    source = request["source"]
    opts = options(source)
    if request["mode"] == "inspect":
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.cookiejar = cookie_jar(source.get("cookies", []))
            info = extract_info(ydl, source)
            result = metadata(info, ydl)
        emit("result", **result)
        return

    directory = Path(request["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    def progress(data):
        if data["status"] == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            stream = data.get("info_dict", {})
            stage = "Скачивание звука" if stream.get("vcodec") == "none" else "Скачивание видео"
            emit("progress", downloaded=downloaded, total=total,
                 percent=min(99, round(downloaded * 100 / total, 1)) if total else None,
                 speed=data.get("speed"), eta=data.get("eta"), stage=stage)
        elif data["status"] == "finished":
            emit("progress", stage="Обработка видео и звука", percent=None)

    opts.update(download_options(request["format"], request.get("height")))
    opts.update({"outtmpl": str(directory / "%(title).150B [%(id)s].%(ext)s"),
                 "progress_hooks": [progress], "concurrent_fragment_downloads": 4,
                 "overwrites": False, "windowsfilenames": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.cookiejar = cookie_jar(source.get("cookies", []))
        # Reject live streams before the potentially unbounded download starts.
        info = extract_info(ydl, source)
        metadata(info)
        ydl.process_ie_result(info, download=True)
    files = [p for p in directory.iterdir() if p.suffix.lower() == "." + request["format"]]
    if len(files) != 1 or not files[0].stat().st_size:
        raise ValueError("Не удалось получить готовый файл. Попробуйте другой формат.")
    result = {"path": str(files[0]), "size": files[0].stat().st_size}
    if validate_download is not None:
        result.update(validate_download(files[0], request))
    emit("result", **result)


if __name__ == "__main__":
    try:
        run(json.load(sys.stdin))
    except Exception as error:
        emit("error", message=friendly_error(error))
        sys.exit(1)
