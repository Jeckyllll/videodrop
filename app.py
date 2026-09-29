#!/usr/bin/env python3
"""VideoDrop: loopback-only UI and a small queue of isolated yt-dlp workers."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import updater

ROOT = Path(__file__).resolve().parent
CONFIG = updater.read_json(ROOT / 'version.json')
EDITION = updater.edition_of(CONFIG)
UPLOADS_ENABLED = EDITION == 'full'
STATE = ROOT / ".state"
DOWNLOADS = ROOT / "Загрузки"
PORT = int(os.environ.get("VIDEODROP_PORT", str(CONFIG.get('port', 8765))))
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = ""
LOCK = threading.RLock()
SOURCES = {}
JOBS = {}
DOWNLOAD_QUEUE = ThreadPoolExecutor(max_workers=1)
INSPECT_QUEUE = ThreadPoolExecutor(max_workers=2)

class DownloadHistory:
    """Lite has local download history, with no uploader module or account connection."""
    def __init__(self):
        self.extension = {}
        self.connect_request = None

    def settings(self, value):
        if value is not None and (not isinstance(value, dict) or value.get('enabled') or value.get('deleteLocal')):
            raise ValueError('VideoDrop Lite сохраняет файлы только на компьютер.')
        return {'provider': 'local', 'enabled': False, 'deleteLocal': False}

    def persist(self):
        with LOCK:
            fields = ('id', 'kind', 'status', 'created', 'title', 'format', 'height', 'result', 'stage', 'error')
            jobs = [{key: job[key] for key in fields if key in job} for job in JOBS.values()
                    if job.get('kind') == 'download' and job.get('result')]
            updater.write_json(STATE / 'downloads.json', jobs[-100:])

    def restore(self):
        with LOCK:
            for job in updater.read_json(STATE / 'downloads.json', []):
                if not isinstance(job, dict) or not isinstance(job.get('id'), str):
                    continue
                job.update(cancel=False, storage=self.settings(None))
                job['result'] = {k: v for k, v in job.get('result', {}).items() if k in ('filename', 'size', 'localKept')}
                if job.get('status') in ('queued', 'working'):
                    job.update(status='error', error='Скачивание прервано перезапуском. Начните его заново.')
                JOBS[job['id']] = job

    def cancel(self, job):
        self.persist()

    def maintenance(self):
        pass


if UPLOADS_ENABLED:
    import studio
    STUDIO = studio.Broker(JOBS, LOCK, STATE, DOWNLOADS)
else:
    STUDIO = DownloadHistory()
UPDATES = None


def validate_url(value):
    if not isinstance(value, str) or len(value) > 16000 or any(ord(c) < 32 for c in value):
        raise ValueError("Некорректная ссылка.")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Нужна полная ссылка http:// или https:// на видео или урок.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".localhost")):
        raise ValueError("Укажите ссылку на видеосервис в интернете.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Локальные адреса не поддерживаются.")
    return value.strip()


def clean_source(data):
    url = validate_url(data.get("url", ""))
    referer = validate_url(data["referer"]) if data.get("referer") else ""
    hosts = {urlsplit(item).hostname.lower() for item in (url, referer) if item}
    cookies = []
    for cookie in data.get("cookies", [])[:300]:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        host_only = cookie.get("hostOnly", False)
        if not domain or not any(h == domain or (not host_only and h.endswith("." + domain)) for h in hosts):
            continue
        if any("\n" in str(cookie.get(k, "")) or "\r" in str(cookie.get(k, "")) for k in ("name", "value", "domain", "path")):
            continue
        if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
            continue
        cookies.append(cookie)
    user_agent = str(data.get("userAgent", ""))[:1000]
    if "\n" in user_agent or "\r" in user_agent:
        raise ValueError("Некорректные данные браузера.")
    return {"url": url, "referer": referer, "cookies": cookies, "userAgent": user_agent,
            "storage": STUDIO.settings(data.get("storage"))}


def job_view(job):
    result = {k: v for k, v in job.items() if k not in ("process", "source", "cancel", "directory", "_youtube", "_studio", "_studioLease", "_studioSeen")}
    state = job.get("_studio", {})
    result["studioNeedsReview"] = bool(state.get("submitted") and not state.get("videoId"))
    return result


def open_download(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["kind"] != "download" or job["status"] not in ("done", "error", "cancelled"):
            raise ValueError("Готовый файл ещё не доступен.")
        result = job.get("result", {})
        if not result.get("localKept"):
            raise ValueError("Локальная копия не сохранена." + (" Используйте кнопку «Открыть на YouTube»." if UPLOADS_ENABLED else ""))
        filename = result.get("filename", "")
    # Only a completed media file belonging to this job can be opened, never a client-supplied path.
    if not filename or Path(filename).name != filename or Path(filename).suffix.lower() not in (".mp4", ".mkv", ".mp3", ".m4a"):
        raise ValueError("Нельзя открыть этот файл.")
    target = DOWNLOADS / filename
    if target.is_symlink() or not target.is_file() or target.resolve().parent != DOWNLOADS.resolve():
        raise ValueError("Файл не найден в загрузках. Возможно, вы переместили или удалили его.")
    try:
        subprocess.run(["/usr/bin/open", str(target.resolve())], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Не удалось открыть файл. Проверьте, установлен ли плеер для этого формата.") from error


def new_job(kind, source):
    with LOCK:
        if UPDATES:
            UPDATES.ensure_accepting()
        active = sum(j["status"] in ("queued", "working") for j in JOBS.values())
        if active >= 12:
            raise ValueError("В очереди уже 12 заданий. Дождитесь завершения.")
        job = {"id": uuid.uuid4().hex, "kind": kind, "status": "queued", "created": time.time(),
               "stage": "В очереди", "source": source, "cancel": False}
        JOBS[job["id"]] = job
        # Bound memory without removing active jobs.
        for old_id in list(JOBS)[:-100]:
            if JOBS[old_id]["status"] not in ("queued", "working"):
                del JOBS[old_id]
        return job


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    def force():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    timer = threading.Timer(2, force)
    timer.daemon = True
    timer.start()


def upload_saved(job):
    try:
        with LOCK:
            if job.get("cancel"):
                return
            STUDIO.enqueue(job)
    except (ValueError, OSError) as error:
        with LOCK:
            STUDIO.fail(job, str(error))


def work(job, request):
    result = None
    error = "Источник не вернул видео. Попробуйте передать его через расширение."
    directory = DOWNLOADS / ".partial" / job["id"]
    timeout = None
    try:
        with LOCK:
            if job["cancel"]:
                return
            job.update(status="working", stage="Поиск видео" if job["kind"] == "inspect" else "Подготовка")
            process = subprocess.Popen([sys.executable, str(ROOT / "worker.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", start_new_session=True, cwd=ROOT)
            job["process"] = process
        request.update(source=job["source"], mode=job["kind"], directory=str(directory))
        process.stdin.write(json.dumps(request))
        process.stdin.close()
        if job["kind"] == "inspect":
            timeout = threading.Timer(150, lambda: stop_process(process))
            timeout.daemon = True
            timeout.start()
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            with LOCK:
                if message["event"] == "progress":
                    job.update({k: v for k, v in message.items() if k != "event"})
                elif message["event"] == "result":
                    result = {k: v for k, v in message.items() if k != "event"}
                elif message["event"] == "error":
                    error = message["message"]
        code = process.wait()
        process.stdout.close()
        with LOCK:
            job.pop("process", None)
            if job["cancel"]:
                job.update(status="cancelled", stage="Отменено")
            elif code != 0 or not result:
                job.update(status="error", error=error)
            elif job["kind"] == "inspect":
                source_id = uuid.uuid4().hex
                SOURCES[source_id] = {"source": job["source"], "info": result, "created": time.time()}
                job.update(status="done", result={**result, "sourceId": source_id,
                                                 "storage": job["source"].get("storage", STUDIO.settings(None))})
            else:
                path = Path(result["path"]).resolve()
                if not path.is_relative_to(directory.resolve()) or not path.is_file():
                    raise ValueError("Не найден готовый файл.")
                destination = DOWNLOADS / path.name
                if destination.exists():
                    destination = DOWNLOADS / (path.stem + " — " + job["id"][:6] + path.suffix)
                shutil.move(str(path), str(destination))
                job.update(status="working" if job.get("storage", {}).get("enabled") else "done",
                           stage="Сохранено на Mac", percent=100,
                           result={"filename": destination.name, "size": result["size"], "localKept": True})
        if job["kind"] == "download" and job.get("result") and job.get("storage", {}).get("enabled") and not job["cancel"]:
            upload_saved(job)
    except Exception:
        with LOCK:
            if job["cancel"]:
                job.update(status="cancelled", stage="Отменено")
            else:
                job.update(status="error", error="Не удалось обработать видео. Проверьте свободное место и повторите.")
    finally:
        if timeout:
            timeout.cancel()
        with LOCK:
            process = job.pop("process", None)
            if process and process.poll() is None:
                stop_process(process)
            job.pop("source", None)
            if job.get("result") and job["kind"] == "download":
                STUDIO.persist()
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoDrop"

    def log_message(self, *args):
        pass  # Signed media URLs and session data must never end up in access logs.

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def origin_allowed(self):
        origin = self.headers.get("Origin", "")
        return not origin or origin in (BASE, f"http://localhost:{PORT}") or bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin))

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        data = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        origin = self.headers.get("Origin", "")
        if self.origin_allowed() and origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        if not self.valid_host() or not self.origin_allowed():
            return self.respond(403, {"error": "Доступ запрещён"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", BASE))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-VideoDrop-Token, X-VideoDrop-Extension")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def authorized(self):
        return self.valid_host() and self.origin_allowed() and hmac.compare_digest(
            self.headers.get("X-VideoDrop-Token", ""), TOKEN)

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Доступ запрещён"})
        path = urlsplit(self.path).path
        if not self.origin_allowed():
            return self.respond(403, {"error": "Доступ запрещён"})
        if path == "/api/bootstrap":
            # No cross-origin bootstrap: only the local UI can read the pairing secret.
            if self.headers.get("Origin", BASE) not in (BASE, f"http://localhost:{PORT}") or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.respond(403, {"error": "Доступ запрещён"})
            return self.respond(200, {"token": TOKEN, "downloads": str(DOWNLOADS), "extension": str(ROOT / "extension"),
                                      "edition": EDITION, "youtubeUpload": UPLOADS_ENABLED})
        if path.startswith("/api/"):
            if not self.authorized():
                return self.respond(403, {"error": "Нет локального ключа приложения. Перезапустите VideoDrop и перезагрузите расширение."})
            if not UPLOADS_ENABLED and path.startswith('/api/studio/'):
                return self.respond(404, {"error": "В VideoDrop Lite доступно только скачивание."})
            if path == "/api/studio/status":
                return self.respond(200, STUDIO.status())
            if path == "/api/health":
                return self.respond(200, {"version": UPDATES.version if UPDATES else CONFIG['version'], "pid": os.getpid(), "edition": EDITION})
            if path == "/api/updates":
                value = UPDATES.status() if UPDATES else {}
                installed = json.loads((ROOT / "extension" / "manifest.json").read_text())["version"]
                seen = STUDIO.extension.get("version")
                reminder = updater.read_json(STATE / "updates" / "extension.json")
                value.update(extensionVersion=installed, extensionLoadedVersion=seen,
                             extensionReload=bool(reminder.get("needsReload") or STUDIO.extension and seen != installed))
                return self.respond(200, value)
            with LOCK:
                if path == "/api/jobs":
                    return self.respond(200, [job_view(j) for j in JOBS.values()])
                if path.startswith("/api/jobs/"):
                    job = JOBS.get(path.rsplit("/", 1)[-1])
                    return self.respond(200, job_view(job)) if job else self.respond(404, {"error": "Задание не найдено"})
            return self.respond(404, {"error": "Не найдено"})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in ("/youtube-ui.js", "/youtube-ui.css"):
            if not UPLOADS_ENABLED:
                return self.respond(404, {"error": "Не найдено"})
            mime = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            return self.respond(200, (ROOT / "extension" / path[1:]).read_bytes(), mime)
        if path in assets:
            name, mime = assets[path]
            return self.respond(200, (ROOT / "web" / name).read_bytes(), mime)
        return self.respond(404, {"error": "Не найдено"})

    def do_POST(self):
        if not self.authorized():
            return self.respond(403, {"error": "Нет локального ключа приложения. Перезапустите VideoDrop и перезагрузите расширение."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 300000 or self.headers.get_content_type() != "application/json":
                raise ValueError("Некорректный запрос")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Некорректный запрос")
            path = urlsplit(self.path).path
            if not UPLOADS_ENABLED and (path.startswith('/api/studio/') or path == '/api/upload-youtube'):
                return self.respond(404, {"error": "В VideoDrop Lite доступно только скачивание."})
            if path == '/api/extension/heartbeat':
                extension_id = self.headers.get('X-VideoDrop-Extension', '')
                origin = self.headers.get('Origin', '')
                if not re.fullmatch(r'[a-p]{32}', extension_id) or origin and origin != 'chrome-extension://' + extension_id:
                    return self.respond(403, {"error": "Нужно расширение VideoDrop."})
                version = str(data.get('version', ''))
                if not re.fullmatch(r'\d+\.\d+\.\d+', version) or data.get('edition') != EDITION:
                    raise ValueError('Расширение относится к другой редакции VideoDrop.')
                with LOCK:
                    STUDIO.extension = {'id': extension_id, 'seen': time.time(), 'version': version}
                    reminder_path = STATE / 'updates' / 'extension.json'
                    reminder = updater.read_json(reminder_path)
                    if reminder.get('needsReload') and reminder.get('version') == version:
                        updater.write_json(reminder_path, {**reminder, 'needsReload': False})
                return self.respond(200, {'ok': True})
            if path.startswith("/api/updates/"):
                # Only the local UI can choose when to replace executable program files.
                if self.headers.get("Origin", BASE) not in (BASE, f"http://localhost:{PORT}"):
                    return self.respond(403, {"error": "Обновления доступны в локальном окне VideoDrop."})
                if path == "/api/updates/check":
                    return self.respond(200, UPDATES.check())
                if path == "/api/updates/install":
                    return self.respond(200, UPDATES.install())
                if path == "/api/updates/rollback":
                    return self.respond(200, UPDATES.install(rollback=True))
                if path == "/api/updates/settings":
                    return self.respond(200, UPDATES.set_automatic(data.get("automatic")))
            if path == "/api/studio/connect":
                with LOCK:
                    if UPDATES:
                        UPDATES.ensure_accepting()
                    return self.respond(200, STUDIO.connect())
            if path == "/api/studio/disconnect":
                return self.respond(200, STUDIO.disconnect())
            if path == "/api/studio/cancel-connect":
                return self.respond(200, STUDIO.cancel_connect())
            if path in ("/api/studio/heartbeat", "/api/studio/claim", "/api/studio/event", "/api/studio/connected", "/api/studio/connect-status"):
                extension_id = self.headers.get("X-VideoDrop-Extension", "")
                origin = self.headers.get("Origin", "")
                if not re.fullmatch(r"[a-p]{32}", extension_id) or origin and origin != "chrome-extension://" + extension_id:
                    return self.respond(403, {"error": "Нужно расширение VideoDrop."})
                if path == "/api/studio/heartbeat":
                    value = STUDIO.heartbeat(extension_id, data.get("protocol"), data.get("version"))
                    reminder_path = STATE / "updates" / "extension.json"
                    reminder = updater.read_json(reminder_path)
                    if reminder.get("needsReload") and reminder.get("version") == data.get("version"):
                        updater.write_json(reminder_path, {**reminder, "needsReload": False})
                    return self.respond(200, value)
                if extension_id != STUDIO.extension.get("id"):
                    return self.respond(403, {"error": "Расширение не подключено."})
                if path == "/api/studio/claim":
                    return self.respond(200, STUDIO.claim())
                if path == "/api/studio/connected":
                    return self.respond(200, STUDIO.connected(data))
                if path == "/api/studio/connect-status":
                    return self.respond(200, STUDIO.connect_status(data))
                return self.respond(200, STUDIO.event(data))
            if path in ("/api/inspect", "/api/import"):
                source = clean_source(data)
                with LOCK:
                    # Cookies and expiring media links are retained in RAM for at most two hours.
                    for sid in list(SOURCES):
                        if time.time() - SOURCES[sid]["created"] > 7200:
                            del SOURCES[sid]
                job = new_job("inspect", source)
                INSPECT_QUEUE.submit(work, job, {})
                return self.respond(202, {"jobId": job["id"], "openUrl": BASE + "/#job=" + job["id"]})
            if path == "/api/download":
                with LOCK:
                    entry = SOURCES.get(data.get("sourceId"))
                if not entry or time.time() - entry["created"] > 7200:
                    raise ValueError("Ссылка устарела. Найдите видео заново.")
                container = data.get("format", "mp4")
                if container not in ("mp4", "mkv", "mp3", "m4a"):
                    raise ValueError("Неподдерживаемый формат")
                if container in ("mp4", "mkv") and not entry["info"]["hasVideo"]:
                    raise ValueError("Источник содержит только аудио.")
                if container in ("mp3", "m4a") and not entry["info"]["hasAudio"]:
                    raise ValueError("Источник не содержит аудио.")
                height = int(data.get("height") or 0)
                if height and height not in entry["info"]["heights"]:
                    raise ValueError("Такого разрешения нет в источнике")
                storage = STUDIO.settings(data.get("storage", entry["source"].get("storage")))
                if storage["enabled"] and container in ("mp3", "m4a"):
                    raise ValueError("YouTube принимает видео. Выберите MP4/MKV или выключите отправку на YouTube.")
                job = new_job("download", entry["source"])
                with LOCK:
                    job.update(title=entry["info"]["title"], format=container, height=height, storage=storage)
                DOWNLOAD_QUEUE.submit(work, job, {"format": container, "height": height})
                return self.respond(202, {"jobId": job["id"]})
            if path == "/api/upload-youtube":
                with LOCK:
                    old = JOBS.get(data.get("jobId"))
                    if not old or old["status"] not in ("done", "error", "cancelled") or not old.get("result", {}).get("localKept"):
                        raise ValueError("Нет сохранённого файла для повторной отправки.")
                    if old.get("retrying") or old.get("uploading") or old.get("uploadQueued"):
                        raise ValueError("Повторная отправка уже добавлена в очередь.")
                    storage = STUDIO.settings(data.get("storage", old.get("storage")))
                    if not storage["enabled"] or old.get("format") not in ("mp4", "mkv"):
                        raise ValueError("Включите отправку на YouTube и выберите видеофайл MP4/MKV.")
                    if old.get("result", {}).get("youtubeVerified"):
                        raise ValueError("Видео уже загружено. Используйте «Открыть на YouTube».")
                    job = new_job("download", {})
                    job.update(title=old["title"], format=old["format"], height=old["height"],
                               storage=storage, result=dict(old["result"]), _studio=dict(old.get("_studio", {})))
                    old["retrying"] = job["id"]
                DOWNLOAD_QUEUE.submit(upload_saved, job)
                return self.respond(202, {"jobId": job["id"]})
            if path == "/api/cancel":
                with LOCK:
                    job = JOBS.get(data.get("jobId"))
                    if job and job["status"] in ("queued", "working"):
                        job.update(cancel=True, status="cancelled", stage="Отменено")
                        STUDIO.cancel(job)
                        if job.get("process"):
                            stop_process(job["process"])
                return self.respond(200, {"ok": True})
            if path == "/api/open-video":
                open_download(data.get("jobId"))
                return self.respond(200, {"ok": True})
            if path == "/api/open-folder":
                target = DOWNLOADS if data.get("target") != "extension" else ROOT / "extension"
                subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self.respond(200, {"ok": True})
            return self.respond(404, {"error": "Не найдено"})
        except (ValueError, TypeError, KeyError, AttributeError):
            error = sys.exc_info()[1]
            return self.respond(400, {"error": str(error)[:500]})
        except Exception:
            return self.respond(500, {"error": "Локальная ошибка. Перезапустите приложение."})


class LocalServer(ThreadingHTTPServer):
    def service_actions(self):
        STUDIO.maintenance()
        if UPDATES:
            UPDATES.tick()
        now = time.time()
        with LOCK:
            for source_id in list(SOURCES):
                if now - SOURCES[source_id]["created"] > 7200:
                    del SOURCES[source_id]


def initialize():
    global TOKEN
    STATE.mkdir(mode=0o700, exist_ok=True)
    DOWNLOADS.mkdir(exist_ok=True)
    token_path = STATE / "token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
    token_path.chmod(0o600)
    TOKEN = token_path.read_text().strip()
    STUDIO.restore()
    config = ROOT / "extension" / "config.js"
    installed = json.loads((ROOT / "extension" / "manifest.json").read_text())["version"]
    reminder_path = STATE / "updates" / "extension.json"
    reminder = updater.read_json(reminder_path)
    if reminder.get("version") != installed:
        updater.write_json(reminder_path, {"version": installed, "needsReload": config.exists()})
    config.write_text("// Local pairing key. Do not publish this file.\n" +
                      "const VIDEODROP = " + json.dumps({"base": BASE, "token": TOKEN}) + ";\n")
    config.chmod(0o600)


def main():
    global UPDATES
    initialize()
    try:
        server = LocalServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        print(f"Не удалось открыть локальный порт {PORT}: {error}")
        return 1
    (STATE / "server.pid").write_text(str(os.getpid()))
    UPDATES = updater.Updater(ROOT, LOCK, JOBS, STUDIO.persist,
                              lambda: threading.Thread(target=server.shutdown, daemon=True).start(),
                              lambda: bool(STUDIO.connect_request))
    print(f"VideoDrop готов: {BASE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with LOCK:
            for job in JOBS.values():
                job["cancel"] = True
                if job.get("process"):
                    stop_process(job["process"])
        server.server_close()
        DOWNLOAD_QUEUE.shutdown(wait=False, cancel_futures=True)
        INSPECT_QUEUE.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
