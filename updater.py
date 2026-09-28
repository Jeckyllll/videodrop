#!/usr/bin/env python3
"""GitHub release updates. Only program files are replaced; user data stays put."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
import zipfile

ASSET = 'VideoDrop-mac.zip'
MAX_ARCHIVE = 40 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
TOP_FILES = {'app.py', 'worker.py', 'studio.py', 'updater.py', 'version.json',
             'requirements.txt', 'README.md', 'Запустить VideoDrop.command', 'Обновить VideoDrop.command'}
REQUIRED = TOP_FILES | {'web/index.html', 'web/app.js', 'web/style.css', 'extension/manifest.json',
                        'extension/background.js', 'extension/studio-driver.js', 'extension/studio-page.js'}
BUSY_STATES = {'checking', 'downloading', 'preparing', 'restarting'}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Некорректный номер версии.')
    return tuple(map(int, value.split('.')))


def allowed_file(name):
    path = PurePosixPath(name)
    if str(path) != name or '\\' in name or any(p.startswith('.') for p in path.parts):
        return False
    return name in TOP_FILES or (len(path.parts) == 2 and path.parts[0] in ('web', 'extension')
                                and path.suffix in ('.js', '.css', '.html', '.json', '.png', '.svg')
                                and name != 'extension/config.js')


def program_files(root):
    root = Path(root)
    paths = [root / name for name in TOP_FILES]
    paths += list((root / 'web').glob('*')) + list((root / 'extension').glob('*'))
    return sorted(p.relative_to(root).as_posix() for p in paths
                  if p.is_file() and not p.is_symlink() and allowed_file(p.relative_to(root).as_posix()))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_target(root, name):
    if not allowed_file(name):
        raise ValueError('В обновлении есть посторонние файлы.')
    root = Path(root).resolve()
    target = root / name
    if target.is_symlink() or any(p.is_symlink() for p in target.parents if p.is_relative_to(root)):
        raise ValueError('Папка программы содержит символическую ссылку.')
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('Недопустимый путь обновления.')
    return target


def valid_github_url(url):
    value = urlsplit(url)
    return (value.scheme == 'https' and not value.username and not value.password and value.port in (None, 443)
            and value.hostname in {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                                   'objects.githubusercontent.com'})


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_github_url(newurl):
            raise ValueError('GitHub вернул недопустимый адрес загрузки.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def github_fetch(url, limit, target=None):
    if not valid_github_url(url):
        raise ValueError('Разрешены только обновления из GitHub.')
    request = urllib.request.Request(url, headers={'User-Agent': 'VideoDrop-Updater',
                                    'Accept': 'application/vnd.github+json' if target is None else 'application/octet-stream'})
    try:
        with urllib.request.build_opener(GitHubRedirect).open(request, timeout=30) as response:
            total, chunks, started = 0, [], time.monotonic()
            with Path(target).open('wb') if target else contextlib.nullcontext() as stream:
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit or time.monotonic() - started > 300:
                        raise ValueError('Обновление слишком большое или загрузка заняла слишком много времени.')
                    if stream:
                        stream.write(chunk)
                    else:
                        chunks.append(chunk)
            return b''.join(chunks)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError('На GitHub пока нет опубликованной версии или репозиторий закрыт.') from None
        if error.code in (403, 429):
            raise ValueError('GitHub временно ограничил проверки. Попробуйте позже.') from None
        raise ValueError('GitHub временно недоступен. Текущая версия продолжит работать.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('Не удалось связаться с GitHub. Проверьте интернет; текущая версия работает.') from None


def latest_release(repository):
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Не настроен репозиторий обновлений.')
    release = json.loads(github_fetch(f'https://api.github.com/repos/{repository}/releases/latest', 2 * 1024 * 1024))
    version = str(release.get('tag_name', '')).removeprefix('v')
    version_tuple(version)
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Эта версия ещё не готова для установки.')
    asset = next((a for a in release.get('assets', []) if a.get('name') == ASSET), {})
    digest = asset.get('digest', '')
    url = asset.get('browser_download_url', '')
    expected = f'https://github.com/{repository}/releases/download/v{version}/{ASSET}'
    if url != expected or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest or '') or not 0 < asset.get('size', 0) <= MAX_ARCHIVE:
        raise ValueError('В релизе нет проверяемого установочного пакета VideoDrop для Mac.')
    return {'version': version, 'url': url, 'sha256': digest[7:], 'size': asset['size'],
            'releaseUrl': f'https://github.com/{repository}/releases/tag/v{version}'}


def unpack_package(archive, destination, version, repository):
    """Validate the complete ZIP before writing anything into the staging directory."""
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        names = [info.filename for info in entries]
        if len(entries) > 300 or len(names) != len(set(names)) or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError('Повреждённый пакет обновления.')
        if 'release-manifest.json' not in names:
            raise ValueError('Нет списка файлов обновления.')
        for info in entries:
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode) or info.flag_bits & 1 or (info.filename != 'release-manifest.json' and not allowed_file(info.filename)):
                raise ValueError('В обновлении есть недопустимый файл.')
        manifest = json.loads(bundle.read('release-manifest.json'))
        hashes = manifest.get('files', {})
        if manifest.get('version') != version or not isinstance(hashes, dict) or set(names) != set(hashes) | {'release-manifest.json'} or not REQUIRED <= set(hashes):
            raise ValueError('Неполный пакет обновления.')
        for name, expected in hashes.items():
            if not re.fullmatch(r'[a-f0-9]{64}', expected) or hashlib.sha256(bundle.read(name)).hexdigest() != expected:
                raise ValueError('Контрольная сумма файла не совпала.')
        config = json.loads(bundle.read('version.json'))
        if config.get('version') != version or config.get('repository') != repository or config.get('asset') != ASSET:
            raise ValueError('Обновление предназначено для другой программы.')
        for name in hashes:
            target = safe_target(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(name))
            target.chmod(0o755 if name.endswith('.command') else 0o644)
    return list(hashes)


def copy_atomic(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.update-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def snapshot(root, folder):
    files = program_files(root)
    for name in files:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_target(root, name), target)
    return files


def replace_program(root, source, source_files, old_files):
    # Each file is atomic; the durable journal lets the launcher recover a interrupted group.
    for name in sorted(set(source_files) | set(old_files)):
        safe_target(root, name)
    for name in source_files:
        copy_atomic(source / name, safe_target(root, name))
    for name in set(old_files) - set(source_files):
        safe_target(root, name).unlink(missing_ok=True)


def restore_transaction(root, transaction):
    replace_program(root, Path(transaction['backup']), transaction['oldFiles'], transaction['newFiles'])
    runtime = root / '.state' / 'runtime.json'
    if transaction.get('oldRuntime'):
        write_json(runtime, transaction['oldRuntime'])
    else:
        runtime.unlink(missing_ok=True)


def run_checked(args, cwd):
    result = subprocess.run(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    if result.returncode:
        raise ValueError('Не удалось подготовить новую версию. Текущая версия сохранена.')


def health(base, token, version, pid, timeout=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(base + '/api/health', headers={'X-VideoDrop-Token': token})
            with opener.open(request, timeout=1) as response:
                value = json.load(response)
            if value.get('version') == version and value.get('pid') == pid:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(.25)
    return False


def start_app(root, python):
    with (root / '.state' / 'server.log').open('ab') as log:
        return subprocess.Popen([python, str(root / 'app.py')], cwd=root, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)


def helper(root, plan_path):
    """Runs from a preserved copy, outside the program files it replaces."""
    root = Path(root).resolve()
    state = root / '.state' / 'updates'
    plan = read_json(plan_path)
    journal = state / 'transaction.json'
    transaction = None
    child = None
    update_lock = (state / 'update.lock').open('a')
    fcntl.flock(update_lock, fcntl.LOCK_EX)
    try:
        # Never terminate the live downloader. The server exits itself only while idle.
        for _ in range(160):
            try:
                os.kill(plan['pid'], 0)
            except ProcessLookupError:
                break
            time.sleep(.25)
        else:
            raise ValueError('VideoDrop не завершился. Обновление отложено; файлы не изменены.')
        backup = state / ('backup-' + uuid.uuid4().hex)
        old_files = snapshot(root, backup)
        transaction = {**plan, 'backup': str(backup), 'oldFiles': old_files,
                       'oldRuntime': read_json(root / '.state' / 'runtime.json')}
        write_json(journal, transaction)
        replace_program(root, Path(plan['stage']), plan['newFiles'], old_files)
        write_json(root / '.state' / 'runtime.json', {'python': plan['python']})
        child = start_app(root, plan['python'])
        if not health(plan['base'], plan['token'], plan['version'], child.pid):
            raise ValueError('Новая версия не запустилась. Восстановлена предыдущая версия.')
        write_json(state / 'previous.json', {'folder': str(backup), 'files': old_files,
                   'version': plan['oldVersion'], 'python': plan['oldPython']})
        write_json(state / 'result.json', {'ok': True, 'version': plan['version'], 'time': time.time(),
                   'message': 'Версия восстановлена.' if plan['rollback'] else 'VideoDrop обновлён.',
                   'blockedVersion': plan['oldVersion'] if plan['rollback'] else ''})
        journal.unlink(missing_ok=True)
        # Keep exactly one previous program snapshot; never remove runtimes or user media.
        for folder in state.glob('backup-*'):
            if folder != backup and folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
    except Exception as error:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        if transaction:
            restore_transaction(root, transaction)
            journal.unlink(missing_ok=True)
            restored = start_app(root, plan['oldPython'])
            if not health(plan['base'], plan['token'], plan['oldVersion'], restored.pid, timeout=15):
                error = ValueError('Файлы предыдущей версии восстановлены. Для запуска откройте «Запустить VideoDrop.command».')
        write_json(state / 'result.json', {'ok': False, 'time': time.time(), 'blockedVersion': plan.get('version', ''),
                   'message': str(error) if isinstance(error, ValueError) else 'Обновление не установлено. Сохранена предыдущая версия.'})
    finally:
        (state / 'handoff.json').unlink(missing_ok=True)
        update_lock.close()


class Updater:
    def __init__(self, root, lock, jobs, persist, stop, busy=lambda: False):
        self.root, self.lock, self.jobs = Path(root).resolve(), lock, jobs
        self.persist, self.stop, self.busy = persist, stop, busy
        self.state = self.root / '.state' / 'updates'
        self.config = read_json(self.root / 'version.json')
        self.version = self.config.get('version', '1.4.0')
        self.settings = read_json(self.state / 'settings.json', {'automatic': True})
        self.cache = read_json(self.state / 'check.json')
        self.phase, self.message, self.pending = 'idle', '', False
        self.next_check = max(time.time() + 5, self.cache.get('checkedAt', 0) + 6 * 3600)

    def status(self):
        with self.lock:
            result = read_json(self.state / 'result.json')
            previous = read_json(self.state / 'previous.json')
            latest = self.cache.get('latest') or {}
            available = bool(latest and version_tuple(latest['version']) > version_tuple(self.version))
            return {'version': self.version, 'repository': self.config.get('repository', ''),
                    'automatic': self.settings.get('automatic', True), 'phase': self.phase,
                    'message': self.message or self.cache.get('error') or result.get('message', ''),
                    'checkedAt': self.cache.get('checkedAt'), 'available': available,
                    'latestVersion': latest.get('version'), 'pending': self.pending,
                    'previousVersion': previous.get('version'), 'result': result,
                    'restarting': self.phase == 'restarting' or (self.state / 'handoff.json').exists()}

    def ensure_accepting(self):
        if self.phase == 'restarting':
            raise ValueError('VideoDrop обновляется. Повторите после перезапуска через несколько секунд.')

    def set_automatic(self, automatic):
        if not isinstance(automatic, bool):
            raise ValueError('Некорректная настройка обновлений.')
        with self.lock:
            self.settings = {'automatic': automatic}
            write_json(self.state / 'settings.json', self.settings)
            if not automatic:
                self.pending = False
            self.next_check = time.time() + 2
        return self.status()

    def check(self):
        with self.lock:
            if self.phase in BUSY_STATES or (self.state / 'handoff.json').exists():
                return self.status()
            if time.time() - self.cache.get('checkedAt', 0) < 30:
                return self.status()
            self.phase, self.message = 'checking', 'Проверяем новую версию на GitHub…'
            threading.Thread(target=self._check, daemon=True).start()
        return self.status()

    def _check(self):
        try:
            latest = latest_release(self.config.get('repository', ''))
            value = {'checkedAt': time.time(), 'latest': latest}
        except Exception as error:
            value = {'checkedAt': time.time(), 'error': str(error) if isinstance(error, ValueError)
                     else 'Не удалось проверить обновления. Попробуйте позже.'}
        with self.lock:
            self.cache = value
            write_json(self.state / 'check.json', value)
            self.phase, self.message = 'idle', value.get('error', '')
            self.next_check = time.time() + (1800 if value.get('error') else 6 * 3600)

    def install(self, rollback=False):
        with self.lock:
            if self.phase in BUSY_STATES or (self.state / 'handoff.json').exists():
                return self.status()
            if rollback:
                previous = read_json(self.state / 'previous.json')
                if not previous.get('folder') or not Path(previous['folder']).is_dir():
                    raise ValueError('Предыдущая версия пока не сохранена.')
                self.pending = 'rollback'
            elif self.status()['available']:
                self.pending = True
            else:
                raise ValueError('Сначала проверьте наличие новой версии.')
            self.message = 'Обновление начнётся после завершения загрузок и отправок на YouTube.'
        return self.status()

    def tick(self):
        with self.lock:
            if (self.state / 'handoff.json').exists() or (self.state / 'transaction.json').exists():
                return
            if self.phase in BUSY_STATES:
                return
            if self.settings.get('automatic', True) and time.time() >= self.next_check:
                self.check()
                return
            status = self.status()
            blocked = status['result'].get('blockedVersion') == status['latestVersion']
            if not self.pending and self.settings.get('automatic', True) and status['available'] and not blocked:
                self.pending = True
            active = any(j.get('status') in ('queued', 'working') or j.get('process') for j in self.jobs.values()) or self.busy()
            if self.pending and not active:
                rollback = self.pending == 'rollback'
                self.phase, self.message = 'downloading', 'Готовим предыдущую версию…' if rollback else 'Скачиваем обновление…'
                threading.Thread(target=self._prepare, args=(rollback,), daemon=True).start()

    def _prepare(self, rollback):
        try:
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rollback:
                previous = read_json(self.state / 'previous.json')
                stage, files, version, python = Path(previous['folder']), previous['files'], previous['version'], previous['python']
            else:
                latest = self.cache['latest']
                folder = self.state / ('stage-' + uuid.uuid4().hex)
                folder.mkdir()
                archive = folder / 'package.zip'
                github_fetch(latest['url'], MAX_ARCHIVE, archive)
                if archive.stat().st_size != latest['size'] or sha256(archive) != latest['sha256']:
                    raise ValueError('Контрольная сумма архива не совпала. Обновление отменено.')
                stage, version = folder / 'program', latest['version']
                files = unpack_package(archive, stage, version, self.config['repository'])
                python = sys.executable
                with self.lock:
                    self.phase, self.message = 'preparing', 'Проверяем новую версию…'
                if (stage / 'requirements.txt').read_bytes() != (self.root / 'requirements.txt').read_bytes():
                    env = self.root / '.state' / 'update-runtimes' / uuid.uuid4().hex
                    run_checked([sys.executable, '-m', 'venv', str(env)], self.root)
                    python = str(env / 'bin' / 'python')
                    run_checked([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(stage / 'requirements.txt')], self.root)
                run_checked([python, '-c', 'import app, worker, updater; import py_compile; '
                             '[py_compile.compile(p, doraise=True) for p in ("app.py", "worker.py", "studio.py", "updater.py")]'], stage)
            with self.lock:
                # Check again: new tasks may have arrived while the archive was downloading.
                active = any(j.get('status') in ('queued', 'working') or j.get('process') for j in self.jobs.values()) or self.busy()
                if active:
                    self.phase, self.message = 'idle', 'Ждём завершения текущих загрузок.'
                    return
                self.phase, self.message, self.pending = 'restarting', 'Обновляем VideoDrop. Страница откроется снова автоматически…', False
                self.persist()
                from_config = read_json(self.root / 'version.json')
                plan = {'pid': os.getpid(), 'stage': str(stage), 'newFiles': files, 'version': version,
                        'oldVersion': from_config['version'], 'python': python, 'oldPython': sys.executable,
                        'base': 'http://127.0.0.1:' + os.environ.get('VIDEODROP_PORT', '8765'),
                        'token': (self.root / '.state' / 'token').read_text().strip(), 'rollback': rollback}
                plan_path = self.state / 'handoff.json'
                write_json(plan_path, plan)
                runner = self.state / 'runner.py'
                shutil.copy2(self.root / 'updater.py', runner)
                with (self.state / 'update.log').open('ab') as log:
                    subprocess.Popen([sys.executable, str(runner), '--apply', str(self.root), str(plan_path)],
                                     cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                self.stop()
        except Exception as error:
            with self.lock:
                if self.phase == 'restarting':
                    (self.state / 'handoff.json').unlink(missing_ok=True)
                self.phase, self.pending = 'idle', False
                self.message = str(error) if isinstance(error, ValueError) else 'Не удалось подготовить обновление. Текущая версия сохранена.'
                write_json(self.state / 'result.json', {'ok': False, 'message': self.message, 'time': time.time(),
                           'blockedVersion': self.cache.get('latest', {}).get('version', '')})


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--apply':
        helper(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == '--recover':
        root = Path(sys.argv[2]).resolve()
        path = root / '.state' / 'updates' / 'transaction.json'
        with (path.parent / 'update.lock').open('a') as update_lock:
            fcntl.flock(update_lock, fcntl.LOCK_EX)
            if path.exists():
                transaction = read_json(path)
                restore_transaction(root, transaction)
                write_json(path.parent / 'result.json', {'ok': False, 'time': time.time(),
                           'message': 'Восстановлена версия после прерванного обновления.', 'blockedVersion': transaction['version']})
                path.unlink()
            (path.parent / 'handoff.json').unlink(missing_ok=True)
    elif len(sys.argv) == 3 and sys.argv[1] == '--runtime':
        root = Path(sys.argv[2]).resolve()
        value = read_json(root / '.state' / 'runtime.json').get('python', '')
        candidate = Path(value)
        # Do not resolve the interpreter symlink: virtualenv uses its containing folder.
        valid = candidate.is_absolute() and candidate.is_relative_to(root / '.state' / 'update-runtimes')
        print(str(candidate) if valid and candidate.is_file() else str(root / '.venv' / 'bin' / 'python'))
