"""Local queue for our Chrome extension. No Google API, credentials or browser cookies."""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
import unicodedata

PROTOCOL = 1
CONNECT_WAIT = 45
CONNECT_TIMEOUT = 600


def identifier(value, kind='video'):
    pattern = r'[A-Za-z0-9_-]{11}' if kind == 'video' else r'UC[A-Za-z0-9_-]{22}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('Не удалось определить видео или канал YouTube.')
    return value


def identity(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_name(secrets.token_hex(12) + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Broker:
    def __init__(self, jobs, lock, state, downloads):
        self.jobs, self.lock = jobs, lock
        self.state, self.downloads = Path(state), Path(downloads)
        self.extension = {}
        self.connect_request = None
        self.connect_error = ''

    def channel(self):
        value = read_json(self.state / 'studio-channel.json')
        return value if isinstance(value, dict) else {}

    def status(self):
        with self.lock:
            self.expire_connect()
            channel = self.channel()
            ready = time.time() - self.extension.get('seen', 0) < 75
            return {'provider': 'studio', 'connected': bool(channel), 'ready': ready,
                    'channelId': channel.get('id', ''), 'channelTitle': channel.get('title', ''),
                    'extensionId': self.extension.get('id', ''), 'connecting': bool(self.connect_request),
                    'connectError': self.connect_error}

    def expire_connect(self):
        pending = self.connect_request
        if pending and time.time() - pending['created'] > (CONNECT_TIMEOUT if pending['claimed'] else CONNECT_WAIT):
            self.connect_request = None
            self.connect_error = ('Время входа в YouTube истекло. Повторите подключение.' if pending['claimed'] else
                                  'Расширение Chrome не ответило за 45 секунд. Обновите VideoDrop в chrome://extensions, откройте его и повторите подключение.')

    def settings(self, value):
        value = {} if value is None else value
        if not isinstance(value, dict) or any(type(value.get(key, False)) is not bool for key in ('enabled', 'deleteLocal', 'madeForKids')):
            raise ValueError('Некорректные настройки YouTube.')
        off = {'provider': 'studio', 'enabled': False, 'deleteLocal': False, 'channelId': '', 'madeForKids': False}
        if not value.get('enabled'):
            return off
        current = self.status()
        if value.get('provider') != 'studio':
            raise ValueError('Теперь загрузка работает через Chrome. Обновите расширение VideoDrop и подключите канал заново.')
        if not current['connected'] or current['channelId'] != value.get('channelId'):
            raise ValueError('Подключите нужный канал YouTube в настройках выше.')
        if not current['ready']:
            raise ValueError('Откройте Chrome и расширение VideoDrop. Затем повторите отправку.')
        return {**off, 'enabled': True, 'deleteLocal': value.get('deleteLocal', False),
                'channelId': current['channelId'], 'madeForKids': value.get('madeForKids', False)}

    def heartbeat(self, extension_id, version, app_version=None):
        if not re.fullmatch(r'[a-p]{32}', extension_id or '') or version != PROTOCOL:
            raise ValueError('Обновите расширение VideoDrop до версии 1.3.')
        with self.lock:
            if self.extension.get('id') not in (None, extension_id) and self.status()['ready']:
                raise ValueError('VideoDrop уже подключён в другом расширении Chrome.')
            self.extension = {'id': extension_id, 'seen': time.time(),
                              'version': app_version if isinstance(app_version, str) and re.fullmatch(r'\d+\.\d+\.\d+', app_version) else None}
        return self.status()

    def connect(self):
        with self.lock:
            self.expire_connect()
            if any(j.get('uploading') or j.get('uploadQueued') for j in self.jobs.values()):
                raise ValueError('Дождитесь завершения отправки перед сменой канала.')
            if not self.connect_request:
                self.connect_error = ''
                self.connect_request = {'nonce': secrets.token_urlsafe(24), 'created': time.time(), 'claimed': False}
        return self.status()

    def cancel_connect(self):
        with self.lock:
            self.connect_request = None
            self.connect_error = ''
        return self.status()

    def connect_status(self, data):
        with self.lock:
            self.expire_connect()
            pending = self.connect_request
            return {'cancelled': not pending or not hmac.compare_digest(str(data.get('nonce', '')), pending['nonce'])}

    def disconnect(self):
        with self.lock:
            if any(j.get('uploading') or j.get('uploadQueued') for j in self.jobs.values()):
                raise ValueError('Сначала остановите текущую отправку.')
            self.connect_request = None
            self.connect_error = ''
            (self.state / 'studio-channel.json').unlink(missing_ok=True)
        return self.status()

    def connected(self, data):
        with self.lock:
            self.expire_connect()
            pending = self.connect_request
            if not pending or not pending['claimed'] or not hmac.compare_digest(str(data.get('nonce', '')), pending['nonce']):
                raise ValueError('Подключение устарело. Нажмите «Подключить Chrome» заново.')
            if data.get('error'):
                self.connect_request = None
                self.connect_error = 'Не удалось подключить YouTube Studio. Проверьте вход в YouTube и разрешение «Отладчик» у расширения VideoDrop.'
                return {'ok': False}
            channel = identifier(data.get('channelId'), 'channel')
            title = str(data.get('channelTitle') or channel).strip()[:150]
            write_json(self.state / 'studio-channel.json', {'id': channel, 'title': title})
            self.connect_request = None
            self.connect_error = ''
        return self.status()

    def file(self, job):
        filename = job.get('result', {}).get('filename', '')
        path = self.downloads / filename
        if not filename or Path(filename).name != filename or path.suffix.lower() not in ('.mp4', '.mkv'):
            raise ValueError('Для YouTube нужен сохранённый видеофайл MP4 или MKV.')
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.downloads.resolve():
            raise ValueError('Локальный файл не найден или был перемещён.')
        return path

    def persist(self):
        # Never persist source URLs, cookies, worker handles or bridge lease secrets.
        fields = ('id', 'kind', 'status', 'created', 'title', 'format', 'height', 'storage', 'result',
                  'stage', 'error', 'retrying', '_studio')
        values = [{key: job[key] for key in fields if key in job} for job in self.jobs.values()
                  if job.get('kind') == 'download' and job.get('result')]
        write_json(self.state / 'studio-jobs.json', values[-100:])

    def restore(self):
        with self.lock:
            for job in read_json(self.state / 'studio-jobs.json') or []:
                if not isinstance(job, dict) or not isinstance(job.get('id'), str) or job['id'] in self.jobs:
                    continue
                job.update(cancel=False, uploading=False, uploadQueued=False)
                if job.get('status') in ('working', 'queued'):
                    job.update(status='error', error='Приложение перезапущено. Проверьте отправку в YouTube Studio; локальная копия сохранена.')
                self.jobs[job['id']] = job

    def enqueue(self, job):
        with self.lock:
            options = self.settings(job['storage'])
            path = self.file(job)
            stamp = identity(path)
            previous = job.get('_studio')
            if previous and (previous['fileStat'] != stamp or previous['channelId'] != options['channelId']):
                raise ValueError('Файл или канал изменился. Прежнюю отправку нельзя продолжить.')
            if previous and previous.get('submitted') and not previous.get('videoId'):
                raise ValueError('Файл уже передавался в Studio, но ссылка не получена. Проверьте вкладку Studio: повторная загрузка могла бы создать дубликат. Локальная копия сохранена.')
            job['_studio'] = previous or {'fileStat': stamp, 'channelId': options['channelId'], 'submitted': False}
            job.update(status='working', stage='Ждём Chrome · откройте расширение VideoDrop', uploadQueued=True,
                       uploading=False, percent=None, cancel=False)
            job.pop('error', None)
            self.persist()

    def claim(self):
        with self.lock:
            self.maintenance()
            pending = self.connect_request
            if pending and not pending['claimed']:
                pending['claimed'] = True
                return {'connect': {'nonce': pending['nonce']}}
            if any(j.get('uploading') for j in self.jobs.values()):
                return {}
            for job in self.jobs.values():
                if job.get('status') != 'working' or not job.get('uploadQueued') or job.get('cancel'):
                    continue
                try:
                    path = self.file(job)
                    state = job['_studio']
                    if state['channelId'] != self.channel().get('id') or identity(path) != state['fileStat']:
                        raise ValueError('Файл или подключённый канал изменился. Локальная копия сохранена.')
                    lease = secrets.token_urlsafe(32)
                    job.update(uploadQueued=False, uploading=True, stage='Открываем YouTube Studio',
                               _studioLease=lease, _studioSeen=time.time())
                    self.persist()
                    return {'job': {'id': job['id'], 'lease': lease, 'path': str(path.resolve()), 'filename': path.name,
                            'title': re.sub(r'[<>\x00-\x1f]', '', job['title']).strip()[:100] or 'Видео',
                            'madeForKids': job['storage']['madeForKids'], 'channelId': state['channelId'],
                            'videoId': state.get('videoId', ''), 'submitted': state['submitted']}}
                except (ValueError, OSError) as error:
                    self.fail(job, str(error))
            return {}

    def fail(self, job, message):
        job.update(status='cancelled' if job.get('cancel') else 'error', error=message[:500],
                   stage='Проверьте YouTube Studio · локальная копия сохранена', uploading=False, uploadQueued=False)
        job.pop('_studioLease', None)
        self.persist()

    def cancel(self, job):
        with self.lock:
            if not job.get('uploading'):
                job['uploadQueued'] = False
            self.persist()

    def event(self, data):
        with self.lock:
            job = self.jobs.get(data.get('jobId'))
            if not job or not job.get('_studioLease') or not hmac.compare_digest(str(data.get('lease', '')), job['_studioLease']):
                raise ValueError('Сеанс отправки устарел.')
            if data.get('event') == 'stopped':
                self.fail(job, str(data.get('message') or 'Chrome остановил отправку. Локальная копия сохранена.'))
                return {'ok': True}
            if job.get('cancel') or job.get('status') != 'working':
                return {'cancelled': True}
            job['_studioSeen'] = time.time()
            state = job['_studio']
            if self.channel().get('id') != state['channelId']:
                self.fail(job, 'Подключённый канал изменился. Локальная копия сохранена.')
                return {'cancelled': True}
            event = data.get('event')
            if event == 'progress':
                job['stage'] = str(data.get('stage', 'Загрузка через YouTube Studio'))[:150]
                return {'ok': True}
            path = self.file(job)
            if identity(path) != state['fileStat']:
                self.fail(job, 'Локальный файл изменился. Он не удалён.')
                return {'cancelled': True}
            if event == 'submitted':
                if state['submitted']:
                    raise ValueError('Файл уже передан в Studio. Повторная отправка запрещена.')
                state['submitted'] = True
            elif event == 'video':
                if not state['submitted']:
                    raise ValueError('Файл ещё не передан в Studio.')
                value = identifier(data.get('videoId'))
                if state.get('videoId') not in (None, value):
                    raise ValueError('YouTube показал другой ролик. Проверка остановлена.')
                state['videoId'] = value
                job['result'].update(youtubeId=value, youtubeVerified=False)
            elif event == 'complete':
                proof = data.get('proof', {})
                value = identifier(proof.get('videoId'))
                filename = unicodedata.normalize('NFC', str(proof.get('filename', '')))
                expected = unicodedata.normalize('NFC', path.name)
                if (not state['submitted'] or state.get('videoId') != value or proof.get('channelId') != state['channelId'] or
                        proof.get('privacy') != 'unlisted' or proof.get('processed') is not True or
                        proof.get('saved') is not True or filename != expected or
                        proof.get('url') != f'https://studio.youtube.com/video/{value}/edit'):
                    raise ValueError('Studio не подтвердил обработку, файл, канал или доступ «По ссылке». Локальная копия сохранена.')
                job['result'].update(youtubeId=value, youtubeVerified=True, localKept=True)
                # Persist the verified remote copy before attempting the only destructive operation.
                self.persist()
                if job['storage']['deleteLocal']:
                    if path.is_symlink() or identity(path) != state['fileStat']:
                        raise ValueError('Файл изменился после проверки. Он сохранён на Mac.')
                    path.unlink()
                    for other in self.jobs.values():
                        if other.get('result', {}).get('filename') == path.name:
                            other['result'].update(localKept=False, youtubeId=value, youtubeVerified=True)
                job.update(status='done', stage='На YouTube · доступ по ссылке', uploading=False, uploadQueued=False, percent=100)
                job.pop('_studioLease', None)
                job.pop('error', None)
            else:
                raise ValueError('Неизвестный этап отправки.')
            self.persist()
            return {'ok': True}

    def maintenance(self):
        with self.lock:
            self.expire_connect()
            for job in list(self.jobs.values()):
                if job.get('uploading') and time.time() - job.get('_studioSeen', 0) > 100:
                    self.fail(job, 'Chrome перестал отвечать. Проверьте YouTube Studio. Локальный файл сохранён.')
