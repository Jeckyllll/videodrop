"""Durable queue. Source credentials are encrypted and excluded from audit views."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet

ACTIVE = ('queued', 'inspecting', 'choice', 'downloading', 'uploading', 'delivering')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audit_url(url):
    p = urlsplit(url)
    # Signed URLs and authentication query parameters must not reach the journal.
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k in ('v', 'id')])
    return urlunsplit((p.scheme, p.netloc, p.path, query, ''))


class Store:
    def __init__(self, path, key, owner):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.cipher = Fernet(key)
        self.owner = int(owner)
        with self.transaction() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL,
              source BLOB, url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
              mode TEXT NOT NULL DEFAULT 'auto', format TEXT NOT NULL DEFAULT 'mp4',
              height INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}',
              files TEXT NOT NULL DEFAULT '[]', size INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '', youtube_id TEXT NOT NULL DEFAULT '',
              expires REAL, cancelled INTEGER NOT NULL DEFAULT 0,
              delivery_index INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, job_id TEXT, user_id INTEGER NOT NULL,
              created REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tokens (
              hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              kind TEXT NOT NULL, expires REAL NOT NULL, job_id TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_user_created ON jobs(user_id,created);
            CREATE INDEX IF NOT EXISTS events_job ON events(job_id,created);
            ''')
        self.user(owner, 'Владелец', '')

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def one(self, query, args=()):
        with self.lock:
            r = self.db.execute(query, args).fetchone()
            return dict(r) if r else None

    def all(self, query, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(query, args)]

    def user(self, uid, name, username):
        role = 'owner' if int(uid) == self.owner else 'pending'
        with self.transaction() as db:
            db.execute('INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username',
                       (uid, str(name)[:150], str(username)[:64], role, time.time()))
        return self.get_user(uid)

    def get_user(self, uid):
        return self.one('SELECT * FROM users WHERE id=?', (uid,))

    def require(self, uid, owner=False):
        u = self.get_user(uid)
        if not u or u['role'] not in (('owner',) if owner else ('owner', 'employee')):
            raise PermissionError('Доступ пока не предоставлен.')
        return u

    def set_role(self, actor, uid, role):
        self.require(actor, owner=True)
        if int(uid) == self.owner or role not in ('employee', 'blocked'):
            raise ValueError('Нельзя изменить эту роль.')
        with self.transaction() as db:
            if not db.execute('UPDATE users SET role=? WHERE id=?', (role, uid)).rowcount:
                raise ValueError('Пользователь не найден.')
            if role == 'blocked':
                db.execute('DELETE FROM tokens WHERE user_id=?', (uid,))
                db.execute("UPDATE jobs SET cancelled=1 WHERE user_id=? AND state NOT IN ('done','error','cancelled','expired')", (uid,))
        self.event(None, uid, 'access_' + role)

    def event(self, jid, uid, kind, detail=''):
        with self.transaction() as db:
            db.execute('INSERT INTO events(job_id,user_id,created,kind,detail) VALUES (?,?,?,?,?)',
                       (jid, uid, time.time(), kind, str(detail)[:1000]))

    def create(self, uid, source, mode='auto', container='mp4', height=0):
        u = self.require(uid)
        if mode not in ('auto', 'file', 'link') or container not in ('mp4', 'mkv', 'mp3', 'm4a'):
            raise ValueError('Некорректный формат получения.')
        if height not in (0, 240, 360, 480, 720, 1080, 1440, 2160, 4320):
            raise ValueError('Некорректное разрешение.')
        if mode == 'link' and container not in ('mp4', 'mkv') and u['role'] == 'owner':
            raise ValueError('На YouTube можно отправить только видео.')
        jid, now = secrets.token_hex(12), time.time()
        with self.transaction() as db:
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','inspecting','choice','downloading','uploading','delivering')").fetchone()[0]
            own = db.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND state IN ('queued','inspecting','choice','downloading','uploading','delivering')", (uid,)).fetchone()[0]
            if count >= 20 or own >= 3:
                raise ValueError('Очередь заполнена. Дождитесь завершения предыдущих заданий.')
            db.execute('INSERT INTO jobs(id,user_id,created,updated,state,source,url,mode,format,height) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (jid, uid, now, now, 'queued', self.cipher.encrypt(json.dumps(source).encode()),
                        audit_url(source['url']), mode, container, height))
        self.event(jid, uid, 'requested')
        return self.job(jid, uid)

    def job(self, jid, uid=None):
        row = self.one('SELECT * FROM jobs WHERE id=?', (jid,))
        if not row:
            raise ValueError('Задание не найдено.')
        if uid is not None:
            u = self.require(uid)
            if row['user_id'] != uid and u['role'] != 'owner':
                raise PermissionError('Это задание другого пользователя.')
        return row

    def source(self, jid):
        row = self.job(jid)
        if not row['source']:
            raise ValueError('Доступ к источнику удалён. Отправьте ссылку заново.')
        return json.loads(self.cipher.decrypt(row['source']))

    def update(self, jid, **values):
        allowed = {'state','source','title','mode','height','format','metadata','files','size','error',
                   'youtube_id','expires','cancelled','delivery_index'}
        if not values.keys() <= allowed:
            raise ValueError('Invalid job fields')
        values['updated'] = time.time()
        with self.transaction() as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), jid))

    def choose(self, jid, uid, mode):
        with self.transaction():
            row = self.job(jid, uid)
            if mode not in ('file', 'link') or row['state'] != 'choice':
                raise ValueError('Выбор уже сделан или задание ещё не готово.')
            self.update(jid, mode=mode, state='queued', expires=None)
            self.event(jid, uid, 'selected', mode)

    def issue(self, uid, kind, ttl, jid=None):
        self.require(uid)
        value = secrets.token_urlsafe(32) if kind != 'pair' else secrets.token_hex(6)
        with self.transaction() as db:
            db.execute('INSERT INTO tokens VALUES (?,?,?,?,?)', (digest(value), uid, kind, time.time()+ttl, jid))
        return value

    def resolve(self, value, kind, consume=False):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tokens WHERE hash=? AND kind=? AND expires>?', (digest(value), kind, time.time())).fetchone()
            if not row:
                raise PermissionError('Ссылка или код недействительны либо истекли.')
            self.require(row['user_id'])
            if consume:
                db.execute('DELETE FROM tokens WHERE hash=?', (digest(value),))
            return dict(row)

    def history(self, uid, employee=None, limit=30, offset=0, employees_only=False):
        u = self.require(uid)
        target = employee if u['role'] == 'owner' else uid
        where, args = ('WHERE j.user_id=?', [target]) if target is not None else ('', [])
        if employees_only:
            self.require(uid, owner=True)
            where += (' AND ' if where else 'WHERE ') + 'j.user_id!=?'
            args.append(self.owner)
        return self.all(f'''SELECT j.id,j.user_id,u.name,u.username,j.url,j.title,j.mode,j.format,j.height,
                    j.created,j.state,j.size,j.error,j.youtube_id,j.expires,
                    (SELECT COUNT(*) FROM events e WHERE e.job_id=j.id AND e.kind='download_started') AS file_requests
                    FROM jobs j JOIN users u ON u.id=j.user_id {where} ORDER BY j.created DESC LIMIT ? OFFSET ?''',
                        (*args, min(limit, 1000), max(offset, 0)))

    def setting(self, key, value=None):
        if value is not None:
            with self.transaction() as db:
                db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,str(value)))
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row['value'] if row else None

    def recover(self):
        # Never automatically repeat a possibly completed external upload/delivery.
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='queued' WHERE state IN ('inspecting','downloading')")
            db.execute("UPDATE jobs SET state='review',error='Сервер перезапущен во время отправки. Проверьте результат перед повтором.',expires=? WHERE state IN ('uploading','delivering')", (time.time()+86400,))
