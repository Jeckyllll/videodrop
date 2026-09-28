"""Isolated yt-dlp worker. Requests/cookies travel over stdin, never command arguments."""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import sys

import imageio_ffmpeg
import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar


def emit(kind, **data):
    print(json.dumps({"event": kind, **data}, ensure_ascii=False), flush=True)


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


def metadata(info):
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
            "extractor": info.get("extractor_key", "Видео"), "formatCount": len(formats)}


def download_options(container, height):
    if container in ("mp3", "m4a"):
        return {"format": "bestaudio/best", "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": container, "preferredquality": "192"}]}
    ceiling = "[height<=%d]" % height if height else ""
    # Never silently exceed the chosen resolution. Separate audio is merged by FFmpeg.
    if container == "mp4":
        selector = (f"bv{ceiling}[ext=mp4]+ba[ext=m4a]/b{ceiling}[ext=mp4]/"
                    f"bv{ceiling}+ba/b{ceiling}")
    else:
        selector = f"bv{ceiling}+ba/b{ceiling}"
    return {"format": selector, "merge_output_format": container,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": container}]}


def run(request):
    source = request["source"]
    opts = options(source)
    if request["mode"] == "inspect":
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.cookiejar = cookie_jar(source.get("cookies", []))
            info = unwrap(ydl.extract_info(source["url"], download=False))
        emit("result", **metadata(info))
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
        info = unwrap(ydl.extract_info(source["url"], download=False))
        metadata(info)
        ydl.process_ie_result(info, download=True)
    files = [p for p in directory.iterdir() if p.suffix.lower() == "." + request["format"]]
    if len(files) != 1 or not files[0].stat().st_size:
        raise ValueError("Не удалось получить готовый файл. Попробуйте другой формат.")
    emit("result", path=str(files[0]), size=files[0].stat().st_size)


if __name__ == "__main__":
    try:
        run(json.load(sys.stdin))
    except Exception as error:
        emit("error", message=friendly_error(error))
        sys.exit(1)
