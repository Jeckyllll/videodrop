"""Single durable worker and Telegram bot; no dependency on the owner's computer."""
import csv
import io
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

from server.media import clean_source
from server.store import Store
from server.youtube import YouTube

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024*1024
GIB = 1024*MIB
VERSION = '2.0.0-preview.2'
FILE_TTL = 24*60*60
LABELS = {'queued':'В очереди','inspecting':'Проверяем ссылку','choice':'Ожидает выбора',
          'downloading':'Скачиваем','uploading':'Загружаем на YouTube','delivering':'Отправляем',
          'done':'Готово','error':'Ошибка','review':'Нужна проверка','cancelled':'Отменено','expired':'Файл удалён по сроку'}


def size_text(size):
    return f'{size/GIB:.2f} ГБ' if size >= GIB else f'{size/MIB:.1f} МБ'


def local_time(value):
    return datetime.fromtimestamp(value, ZoneInfo('Europe/Minsk')).strftime('%d.%m %H:%M')


class Telegram:
    def __init__(self, token, base='https://api.telegram.org', local=False):
        self.token, self.base, self.local = token, base.rstrip('/'), local
        self.limit = 1900*MIB if local else 45*MIB

    def call(self, method, **data):
        if not self.token:
            raise ValueError('Не настроен токен отдельного Telegram-бота.')
        try:
            r = requests.post(f'{self.base}/bot{self.token}/{method}', json=data, timeout=40)
            payload = r.json()
            if not payload.get('ok'):
                raise ValueError('Telegram не подтвердил действие: '+str(payload.get('description','ошибка'))[:200])
            return payload['result']
        except requests.RequestException:
            # requests exceptions contain the bot token in the URL.
            raise ValueError('Telegram временно недоступен. Повторите позже.') from None

    def say(self, uid, text, buttons=None):
        args = {'chat_id':uid, 'text':text[:4000], 'link_preview_options':{'is_disabled':True}}
        if buttons:
            args['reply_markup'] = {'inline_keyboard':buttons}
        return self.call('sendMessage', **args)

    def document(self, uid, path, caption=''):
        if path.stat().st_size > self.limit:
            raise ValueError('Файл превышает лимит отправки в Telegram.')
        if self.local:
            return self.call('sendDocument', chat_id=uid, document='file://'+str(path.resolve()),
                             caption=caption[:1000], disable_content_type_detection=True)
        with path.open('rb') as f:
            return self.bytes_document(uid, path.name, f, caption)

    def bytes_document(self, uid, name, file, caption=''):
        try:
            r = requests.post(f'{self.base}/bot{self.token}/sendDocument',
                data={'chat_id':uid,'caption':caption[:1000],'disable_content_type_detection':'true'},
                files={'document':(name,file,'application/octet-stream')}, timeout=180)
            payload = r.json()
            if not payload.get('ok'):
                raise ValueError('Telegram не принял файл.')
            return payload['result']
        except requests.RequestException:
            raise ValueError('Ответ Telegram не получен. Проверьте чат перед повторной отправкой.') from None


class Service:
    def __init__(self, state, owner, token='', public_base='', key=None, telegram_base='https://api.telegram.org',
                 telegram_local=False):
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.files = self.state/'files'
        self.files.mkdir(exist_ok=True)
        keyfile = self.state/'secret.key'
        if key is None:
            if not keyfile.exists():
                fd = os.open(keyfile, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
                with os.fdopen(fd,'wb') as f:
                    f.write(Fernet.generate_key())
            key = keyfile.read_bytes()
        self.store = Store(self.state/'jobs.sqlite3', key, owner)
        self.tg = Telegram(token, telegram_base, telegram_local)
        self.base = public_base.rstrip('/')
        self.youtube = YouTube(self.state, self.store.cipher, self.base)
        self.budget = int(os.environ.get('VIDEODROP_STORAGE_GIB',20))*GIB
        self.per_job = int(os.environ.get('VIDEODROP_JOB_GIB',8))*GIB
        self.stop = threading.Event()
        self.started = time.time()
        self.worker_seen = self.started
        self.poll_seen = 0
        self.last_error = ''

    def notify(self, uid, text, buttons=None):
        try:
            return self.tg.say(uid,text,buttons)
        except ValueError:
            self.last_error = 'Не удалось доставить уведомление Telegram.'
            return None

    def choice_buttons(self, row):
        jid = row['id']
        return [[{'text':'Получить ссылку','callback_data':'link:'+jid},
                 {'text':'Получить файл','callback_data':'file:'+jid}],
                [{'text':'Качество и формат','callback_data':'quality:'+jid},
                 {'text':'Отменить','callback_data':'cancel:'+jid}]]

    def ask_choice(self, row):
        self.store.update(row['id'],state='choice',expires=time.time()+FILE_TTL)
        where = 'Получить ссылку — загрузить на YouTube с доступом «По ссылке».'
        if not self.youtube.ready():
            where += ' YouTube пока не подключён.'
        file_hint = f'Файл — отправка без сжатия в Telegram, до {size_text(self.tg.limit)}.'
        if self.base:
            file_hint += ' Более крупный — прямая ссылка на скачивание с сервера на 24 часа.'
        else:
            file_hint += ' Для более крупных файлов временные ссылки ещё не подключены.'
        self.notify(row['user_id'], f"{row['title']}\nСкачано на сервер: {size_text(row['size'])}. Как получить видео?\n{where}\n{file_hint}", self.choice_buttons(row))

    def handle(self, update):
        callback = update.get('callback_query')
        msg = callback.get('message',{}) if callback else update.get('message',{})
        sender = callback.get('from',{}) if callback else msg.get('from',{})
        if not sender.get('id') or msg.get('chat',{}).get('type') != 'private':
            return
        uid = sender['id']
        u = self.store.user(uid, ' '.join(filter(None,(sender.get('first_name'),sender.get('last_name')))), sender.get('username',''))
        if callback:
            self.tg.call('answerCallbackQuery',callback_query_id=callback['id'])
        text = msg.get('text','').strip()
        if u['role'] == 'pending':
            if text == '/start' and not callback:
                if not self.store.setting('requested_access_'+str(uid)):
                    self.notify(self.store.owner, f"Запрос доступа: {u['name']} @{u['username']}\nTelegram ID: {uid}",
                        [[{'text':'Разрешить','callback_data':'approve:'+str(uid)},
                          {'text':'Отказать','callback_data':'block:'+str(uid)}]])
                    self.store.setting('requested_access_'+str(uid), '1')
                self.notify(uid,'Запрос отправлен владельцу. После допуска можно присылать ссылки.\n'
                    'Владелец видит историю заданий VideoDrop: имя, ссылку, название, размер и результат. Другие действия в Telegram не отслеживаются.')
            return
        if u['role'] == 'blocked':
            return
        try:
            if callback:
                return self.handle_callback(uid, callback['data'])
            cmd, _, arg = text.partition(' ')
            if cmd in ('/start','/help'):
                content = ('Пришли ссылку на видео или пост. Сначала скачаю видео на сервер, затем предложу «Получить ссылку» (YouTube) или «Получить файл». Картинки поста отправляю файлами без дополнительного сжатия.\n\n'
                    '/history — мои задания\n/cancel ID — отменить\n'
                    'Владелец видит журнал заданий VideoDrop. Личные чаты и историю браузера бот не читает.')
                if u['role'] == 'owner':
                    content += '\n\n/audit — журнал сотрудников\n/users — сотрудники\n/export — журнал CSV\n/status — состояние\n/youtube — подключение канала'
                return self.notify(uid,content)
            if cmd in ('/history','/audit'):
                if cmd == '/audit':
                    self.store.require(uid,owner=True)
                rows = self.store.history(uid, employee=uid if cmd == '/history' else (int(arg) if arg.isdigit() else None),
                                          limit=10, employees_only=cmd == '/audit')
                return self.notify(uid, '\n\n'.join(self.describe(r) for r in rows) or 'Заданий пока нет.')
            if cmd == '/export':
                self.store.require(uid,owner=True)
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['Время (Минск)','Telegram ID','Имя','Username','Источник','Название','Выбор','Статус','Байты','YouTube ID','Запросы файла'])
                offset = 0
                while True:
                    rows = self.store.history(uid,limit=1000,offset=offset)
                    for r in rows:
                        values = [local_time(r['created']),r['user_id'],r['name'],r['username'],r['url'],r['title'],r['mode'],LABELS.get(r['state'],r['state']),r['size'],r['youtube_id'],r['file_requests']]
                        # Prevent spreadsheet formula execution in user-controlled cells.
                        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in values])
                    offset += len(rows)
                    if len(rows) < 1000 or offset >= 50000:
                        break
                return self.tg.bytes_document(uid,'videodrop-history.csv',io.BytesIO(output.getvalue().encode('utf-8-sig')),'Журнал заданий VideoDrop')
            if cmd == '/users':
                self.store.require(uid,owner=True)
                users = self.store.all("SELECT * FROM users WHERE role!='owner' ORDER BY created DESC LIMIT 30")
                for person in users:
                    self.notify(uid, f"{person['name']} @{person['username']}\nID: {person['id']} · {person['role']}",
                        [[{'text':'Разрешить','callback_data':'approve:'+str(person['id'])},
                          {'text':'Отключить','callback_data':'block:'+str(person['id'])},
                          {'text':'История','callback_data':'history:'+str(person['id'])}]])
                if not users:
                    self.notify(uid,'Сотрудников пока нет. Передай им ссылку на этого бота; они нажмут /start, и ты получишь запрос доступа.')
                return
            if cmd == '/status':
                self.store.require(uid,owner=True)
                pending = self.store.one("SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','inspecting','downloading','uploading','delivering')")['n']
                return self.notify(uid,f'VideoDrop {VERSION}\nВ очереди и работе: {pending}\nВременные файлы: {size_text(self.disk_used())} / {size_text(self.budget)}\nYouTube: '+('подключён' if self.youtube.ready() else 'не подключён'))
            if cmd == '/youtube':
                self.store.require(uid,owner=True)
                if self.youtube.ready():
                    return self.notify(uid,'YouTube подключён. Для больших видео выбери «Получить ссылку».')
                if not self.base:
                    return self.notify(uid,'Подключение YouTube готовится. Для первоначального входа нужен OAuth-клиент Google Cloud; пока он не настроен.')
                state = self.store.issue(uid,'oauth',600)
                return self.notify(uid,'Подключи нужный канал через Google. Разрешения на загрузку и проверку роликов получает только сервер VideoDrop.',
                    [[{'text':'Подключить YouTube','url':self.youtube.authorize(state)}]])
            if cmd == '/pair':
                if not self.base:
                    raise ValueError('Подключение расширения пока не включено: сейчас работает только бот.')
                code = self.store.issue(uid,'pair',600)
                return self.notify(uid,f'Адрес сервера: {self.base}\nКод расширения: {code}\nДействует 10 минут, один раз. Не пересылай его другим людям.')
            if cmd in ('/cancel','/delete'):
                return self.handle_callback(uid,cmd[1:]+':'+arg)
            if cmd.startswith('/'):
                return self.notify(uid,'Отправь ссылку или нажми /help.')
            urls = re.findall(r'https?://[^\s<>]+', text)
            if len(urls) != 1:
                raise ValueError('Пришли одну ссылку на видео или пост.')
            row = self.store.create(uid,clean_source({'url':urls[0]}))
            self.notify(uid,'Проверяю ссылку. Задание '+row['id'],[[{'text':'Отменить','callback_data':'cancel:'+row['id']}]])
        except (ValueError,PermissionError) as error:
            self.notify(uid,str(error))

    def handle_callback(self, uid, data):
        action, _, value = data.partition(':')
        if action in ('approve','block'):
            self.store.set_role(uid,int(value),'employee' if action == 'approve' else 'blocked')
            self.notify(uid,'Доступ предоставлен.' if action == 'approve' else 'Доступ отключён.')
            if action == 'approve':
                self.notify(int(value),'Доступ открыт. Отправь ссылку на видео или пост. Владелец видит журнал заданий этого бота.')
            return
        if action == 'history':
            self.store.require(uid,owner=True)
            return self.notify(uid,'\n\n'.join(self.describe(r) for r in self.store.history(uid,employee=int(value),limit=10)) or 'Заданий пока нет.')
        jid = value.split(':')[0]
        row = self.store.job(jid,uid)
        if action in ('link','file'):
            if action == 'link' and row['format'] not in ('mp4','mkv'):
                raise ValueError('Для YouTube выбери видео MP4 или MKV. Аудио можно получить файлом.')
            if action == 'link' and not self.youtube.ready():
                raise ValueError('YouTube ещё не подключён. Пока можно получить файл.')
            self.store.choose(jid,uid,action)
            return self.notify(uid,'Выбор сохранён. Продолжаю обработку.')
        if action == 'quality':
            if row['state'] != 'choice':
                raise ValueError('Качество можно изменить, пока задание ожидает выбора способа получения.')
            heights = [0]+[h for h in json.loads(row['metadata']).get('heights',[]) if h in (360,480,720,1080,1440,2160)]
            buttons = [{'text':'Лучшее' if h==0 else str(h)+'p','callback_data':f'height:{jid}:{h}'} for h in heights]
            return self.notify(uid,'Выбери качество или формат. Если файл уже скачан, изменение потребует скачать его заново.',
                [buttons[i:i+4] for i in range(0,len(buttons),4)]+
                [[{'text':fmt.upper(),'callback_data':f'format:{jid}:{fmt}'} for fmt in ('mp4','mkv','mp3','m4a')]])
        if action in ('height','format'):
            setting = value.split(':',1)[1]
            if action == 'height' and int(setting) in (0,240,360,480,720,1080,1440,2160,4320):
                change = {'height':int(setting)}
            elif action == 'format' and setting in ('mp4','mkv','mp3','m4a'):
                change = {'format':setting}
            else:
                raise ValueError('Некорректный выбор.')
            with self.store.transaction():
                row = self.store.job(jid,uid)
                if row['state'] != 'choice' or (row['expires'] and row['expires'] <= time.time()):
                    raise ValueError('Задание уже выполняется, завершено или срок выбора истёк.')
                if any(row[k] != v for k,v in change.items()):
                    self.remove_files(row)
                    self.store.update(jid,**change,files='[]',size=0)
            return self.notify(uid,'Настройка сохранена. Выбери способ получения.',self.choice_buttons(self.store.job(jid)))
        if action == 'cancel':
            self.store.update(jid,cancelled=1)
            if row['state'] in ('choice','queued'):
                self.remove_files(row)
                self.store.update(jid,state='cancelled',source=None)
            self.store.event(jid,row['user_id'],'cancelled')
            return self.notify(uid,'Отмена запрошена.')
        if action == 'delete':
            if row['state'] not in ('done','error','review','expired','cancelled'):
                raise ValueError('Сначала отмените активное задание.')
            self.remove_files(row)
            self.store.update(jid,state='expired',source=None,files='[]',expires=None)
            self.store.event(jid,row['user_id'],'file_deleted')
            return self.notify(uid,'Временный файл удалён. Запись в журнале сохранена.')
        raise ValueError('Неизвестная кнопка.')

    @staticmethod
    def describe(row):
        return (f"{local_time(row['created'])} · {row['name']} @{row['username']}\n"
                f"{row['title'] or 'Проверка ссылки'}\n{row['url']}\n"
                f"{LABELS.get(row['state'],row['state'])} · {size_text(row['size'])} · {row['mode']}\n"
                f"ID: {row['id']}")

    def disk_used(self, path=None):
        return sum(p.stat().st_size for p in (path or self.files).rglob('*') if p.is_file() and not p.is_symlink())

    def cancelled(self, jid):
        return self.stop.is_set() or bool(self.store.job(jid)['cancelled'])

    def media(self, row, mode):
        directory = self.files/row['id']
        if mode == 'download':
            # Interrupted partial downloads are scoped to this job and never delivered.
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir()
        request = {'source':self.store.source(row['id']), 'mode':mode, 'directory':str(directory),
                   'format':row['format'], 'height':row['height']}
        env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR')}
        env.update(PYTHONUNBUFFERED='1',HOME=str(directory),XDG_CACHE_HOME=str(directory/'.cache'))
        process = subprocess.Popen([sys.executable,'-m','server.media'],cwd=ROOT,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
        process.stdin.write(json.dumps(request)); process.stdin.close()
        messages = queue.Queue()
        def reader():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
            messages.put(None)
        threading.Thread(target=reader,daemon=True).start()
        deadline = time.monotonic()+(180 if mode=='inspect' else 7200)
        result, error = None, 'Не удалось получить медиа. Для закрытого источника нужен доступ через расширение.'
        try:
            while True:
                self.worker_seen = time.time()
                if self.cancelled(row['id']):
                    raise ValueError('Задание отменено.')
                if time.monotonic()>deadline:
                    raise ValueError('Источник слишком долго не отвечает. Отправьте ссылку позже.')
                if self.disk_used()>self.budget or self.disk_used(directory)>self.per_job or shutil.disk_usage(self.files).free<2*GIB:
                    raise ValueError('Достигнут лимит временного места. Скачивание остановлено.')
                try:
                    message = messages.get(timeout=1)
                except queue.Empty:
                    continue
                if message is None:
                    break
                if message.get('event') == 'result':
                    result = message
                elif message.get('event') == 'error':
                    error = message['message']
            process.wait(timeout=5)
            if process.returncode or not result:
                raise ValueError(error)
            return result
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL); process.wait()
            process.stdout.close()

    def paths(self, row):
        paths = []
        directory = (self.files/row['id']).resolve()
        for name in json.loads(row['files']):
            path = directory/name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory):
                raise ValueError('Готовый файл недоступен.')
            paths.append(path)
        return paths

    def remove_files(self, row):
        directory = self.files/row['id']
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
        with self.store.transaction() as db:
            db.execute("DELETE FROM tokens WHERE job_id=? AND kind='file'",(row['id'],))

    def file_links(self, row):
        if not self.base.startswith('https://'):
            raise ValueError('Временные HTTPS-ссылки на скачивание пока не подключены.')
        remaining = int((row['expires'] or 0)-time.time())
        if remaining <= 0:
            return []
        return [{'name':p.name, 'url':self.base+'/download/'+self.store.issue(row['user_id'],'file',remaining,row['id'])+'/'+str(i)}
                for i,p in enumerate(self.paths(row))]

    def deliver(self, row):
        paths = self.paths(row)
        if row['mode']=='link':
            if len(paths)!=1 or paths[0].suffix.lower() not in ('.mp4','.mkv'):
                raise ValueError('YouTube принимает видео. Для картинок и аудио выберите файл.')
            self.store.update(row['id'],state='uploading')
            def checkpoint(kind,value):
                if kind=='video':
                    self.store.update(row['id'],youtube_id=value)
                else:
                    self.store.setting('upload_session_'+row['id'],value)
            video = self.youtube.upload(paths[0],row['title'],checkpoint,lambda:self.cancelled(row['id']),row['youtube_id'])
            self.store.event(row['id'],row['user_id'],'youtube_verified',video)
            self.store.update(row['id'],state='delivering')
            self.tg.say(row['user_id'],'Готово. Доступ к видео — по ссылке.',[[{'text':'Открыть видео','url':'https://youtu.be/'+video}]])
            self.store.event(row['id'],row['user_id'],'telegram_link_delivered')
            self.remove_files(row)
            self.store.update(row['id'],state='done',source=None,files='[]',expires=None)
            return
        if any(p.stat().st_size>self.tg.limit for p in paths) or row['entrypoint']=='extension':
            if not self.base:
                raise ValueError(f'Для файла больше лимита Telegram ({size_text(self.tg.limit)}) или задания из расширения нужна прямая HTTPS-ссылка с сервера. Она пока не подключена. Файл сохранён на 24 часа без сжатия.')
            until = time.time()+FILE_TTL
            self.store.update(row['id'],expires=until,state='delivering')
            buttons = [[{'text':f'Скачать файл {i+1}','url':item['url']}] for i,item in enumerate(self.file_links(self.store.job(row['id'])))]
            for start in range(0,len(buttons),20):
                self.tg.say(row['user_id'],f'Файл готов без сжатия. Прямая ссылка действует 24 часа, до {local_time(until)} (Минск).',buttons[start:start+20])
            self.store.event(row['id'],row['user_id'],'file_link_delivered')
            self.store.update(row['id'],state='done',source=None)
            return
        self.store.update(row['id'],state='delivering')
        for index,path in enumerate(paths):
            if index < row['delivery_index']:
                continue
            if self.cancelled(row['id']):
                raise ValueError('Отправка отменена.')
            self.tg.document(row['user_id'],path,'' if len(paths)>1 or json.loads(row['metadata']).get('kind')=='images' else row['title'])
            self.store.update(row['id'],delivery_index=index+1)
            self.store.event(row['id'],row['user_id'],'telegram_file_delivered',path.name)
        self.remove_files(row)
        self.store.update(row['id'],state='done',source=None,files='[]',expires=None)

    def run_job(self, row):
        jid = row['id']
        try:
            self.store.require(row['user_id'])
            if row['cancelled']:
                raise ValueError('Задание отменено.')
            if not json.loads(row['metadata']):
                self.store.update(jid,state='inspecting')
                info = self.media(row,'inspect')
                self.store.update(jid,metadata=json.dumps(info),title=info.get('title','Видео')[:500])
                row = self.store.job(jid)
            info = json.loads(row['metadata'])
            if not json.loads(row['files']):
                if self.budget-self.disk_used()<GIB:
                    raise ValueError('На сервере мало временного места. Попробуйте позже.')
                self.store.update(jid,state='downloading')
                result = self.media(row,'download')
                directory = (self.files/jid).resolve()
                paths = [Path(p) for p in result.get('paths', [result.get('path','')])]
                for p in paths:
                    if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(directory):
                        raise ValueError('Источник вернул недопустимый файл.')
                size = sum(p.stat().st_size for p in paths)
                if size>self.per_job:
                    raise ValueError('Файл превышает лимит размера одного задания.')
                self.store.update(jid,files=json.dumps([str(p.relative_to(directory)) for p in paths]),size=size)
                self.store.event(jid,row['user_id'],'download_ready',str(size))
                row = self.store.job(jid)
            if row['mode']=='auto' and info.get('kind')!='images':
                return self.ask_choice(row)
            if row['mode']=='auto':
                self.store.update(jid,mode='file')
                row = self.store.job(jid)
            self.deliver(row)
            if row['user_id'] != self.store.owner:
                u = self.store.get_user(row['user_id'])
                self.notify(self.store.owner,f"Скачивание сотрудника: {u['name']} @{u['username']}\n{row['title']}\n{row['url']}\n{size_text(row['size'])} · {row['mode']} · готово")
        except Exception as e:
            row = self.store.job(jid)
            # Never include raw network exception strings (URLs can contain credentials).
            error = str(e)[:700] if isinstance(e,(ValueError,PermissionError)) else 'Не удалось завершить задание. Файл, если готов, сохранён на 24 часа.'
            state = 'cancelled' if row['cancelled'] else ('review' if row['state'] in ('uploading','delivering') else 'error')
            self.store.update(jid,state=state,error=error,source=None,expires=time.time()+86400)
            if state=='cancelled' or not json.loads(row['files']):
                self.remove_files(row)
                self.store.update(jid,files='[]')
            self.store.event(jid,row['user_id'],state,error)
            self.notify(row['user_id'],error+f'\nЗадание: {jid}')

    def cleanup(self):
        # Serialize expiry with choosing a result so an accepted job cannot lose its file.
        with self.store.transaction() as db:
            rows = self.store.all("SELECT * FROM jobs WHERE expires<? AND state IN ('done','error','review','choice','cancelled')",(time.time(),))
            for row in rows:
                self.remove_files(row)
                self.store.update(row['id'],state='expired',source=None,files='[]',expires=None)
                self.store.event(row['id'],row['user_id'],'expired')
            db.execute('DELETE FROM tokens WHERE expires<?',(time.time(),))

    def cleanup_loop(self):
        # A long download/upload must not postpone removing expired temporary files.
        while not self.stop.is_set():
            try:
                self.cleanup()
            except Exception:
                self.last_error='Не удалось очистить временные файлы. Повтор через 30 секунд.'
            self.stop.wait(30)

    def worker_loop(self):
        self.store.recover()
        while not self.stop.is_set():
            self.worker_seen=time.time()
            try:
                row = self.store.one("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1")
                if row:
                    self.run_job(row)
                    continue
            except Exception:
                self.last_error='Ошибка рабочего процесса. Очередь сохранена.'
            self.stop.wait(1)

    def poll_loop(self):
        offset = int(self.store.setting('telegram_offset') or 0)
        while not self.stop.is_set():
            try:
                updates = self.tg.call('getUpdates',offset=offset,timeout=25,allowed_updates=['message','callback_query'])
                self.poll_seen=time.time()
                for item in updates:
                    # Persist claim before side effects: do not repeat uploads/jobs on restart.
                    offset=item['update_id']+1
                    self.store.setting('telegram_offset',offset)
                    self.handle(item)
            except Exception:
                self.last_error='Telegram недоступен или не настроен. Повтор подключения через 10 секунд.'
                self.stop.wait(10)

    def start(self):
        threading.Thread(target=self.cleanup_loop,daemon=True,name='videodrop-cleanup').start()
        threading.Thread(target=self.worker_loop,daemon=True,name='videodrop-worker').start()
        if self.tg.token:
            threading.Thread(target=self.poll_loop,daemon=True,name='videodrop-telegram').start()
