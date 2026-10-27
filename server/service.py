"""Single durable worker and Telegram bot; no dependency on the owner's computer."""
import csv
import io
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
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
VERSION = '2.0.0-preview.9'
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

    def edit(self, uid, message_id, text, buttons=None):
        try:
            return self.call('editMessageText',chat_id=uid,message_id=message_id,text=text[:4000],
                link_preview_options={'is_disabled':True},reply_markup={'inline_keyboard':buttons or []})
        except ValueError as error:
            if 'message is not modified' in str(error).lower():
                return {'message_id':message_id}
            raise

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

    def video(self, uid, path, caption=''):
        if path.stat().st_size > self.limit:
            raise ValueError('Видео для просмотра превышает лимит Telegram.')
        if self.local:
            return self.call('sendVideo', chat_id=uid, video='file://'+str(path.resolve()),
                             caption=caption[:1000], supports_streaming=True)
        try:
            with path.open('rb') as f:
                r = requests.post(f'{self.base}/bot{self.token}/sendVideo',
                    data={'chat_id':uid,'caption':caption[:1000],'supports_streaming':'true'},
                    files={'video':(path.name,f,'video/mp4')}, timeout=180)
            payload = r.json()
            if not payload.get('ok'):
                raise ValueError('Telegram не принял видео для просмотра.')
            return payload['result']
        except requests.RequestException:
            raise ValueError('Ответ Telegram не получен. Проверьте чат перед повторной отправкой.') from None


class Service:
    def __init__(self, state, owner, token='', public_base='', key=None, telegram_base='https://api.telegram.org',
                 telegram_local=False, downloads_only=False):
        self.downloads_only = downloads_only
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
        self.ui_lock = threading.RLock()

    def notify(self, uid, text, buttons=None):
        try:
            return self.tg.say(uid,text,buttons)
        except ValueError:
            self.last_error = 'Не удалось доставить уведомление Telegram.'
            return None

    def choice_buttons(self, row):
        jid = row['id']
        receive = []
        if self.youtube.ready() and row['format'] in ('mp4','mp4compat','mkv'):
            receive.append({'text':'Получить ссылку','callback_data':'link:'+jid})
        if self.base or (row['entrypoint']=='telegram' and row['size']<=self.tg.limit):
            receive.append({'text':'Получить файл','callback_data':'file:'+jid})
        return ([receive] if receive else [])+[
            [{'text':'Качество и формат','callback_data':'quality:'+jid},
             {'text':'Отменить','callback_data':'cancel:'+jid}]]

    @staticmethod
    def media_note(row):
        media = json.loads(row['metadata']).get('downloadedMedia',{})
        parts = []
        if media.get('width') and media.get('height'):
            parts.append(f"{media['width']}×{media['height']}")
        if media.get('quicktimeWarning'):
            parts.append('Оригинал может не открываться в стандартном плеере; попробуйте VLC.')
        return '\n'.join(parts)

    @staticmethod
    def user_error(error):
        message = str(error).lower()
        if 'youtube' in message and ('ip сервера' in message or 'not a bot' in message):
            return 'YouTube пока не разрешает скачать этот ролик с сервера.'
        if any(s in message for s in ('требует входа','подтверждения доступа','login','sign in')):
            return 'Для этого видео нужен вход на сайт. По одной ссылке скачать его пока нельзя.'
        if 'drm' in message:
            return 'Это видео защищено от скачивания.'
        if 'tiktok' in message:
            return 'Сейчас не удалось скачать этот ролик из TikTok.'
        if 'youtube' in message and ('подключ' in message or 'oauth' in message):
            return 'Загрузка на YouTube ещё не подключена.'
        if any(s in message for s in ('лимит','временного места','больше лимита')):
            return 'Файл слишком большой для текущего способа получения. Выберите качество ниже.'
        if 'telegram' in message:
            return 'Не удалось подтвердить отправку. Проверьте, появился ли файл в чате.'
        if len(str(error))<=160 and re.search('[А-Яа-я]',str(error)) and not re.search(r'ERROR|Traceback|https?://|yt-dlp|exception',str(error),re.I):
            return str(error)
        return 'Не удалось получить видео. Попробуйте другую ссылку.'

    def quality_card(self, row):
        info = json.loads(row['metadata'])
        heights = [0]+[h for h in info.get('heights',[]) if isinstance(h,int) and 0<h<=16384]
        buttons = []
        if row['format'] in ('mp4','mp4compat','mkv'):
            for h in heights:
                if h and row['height']==0 and row['size'] and h==info.get('downloadedMedia',{}).get('height'):
                    continue  # The same source quality is already available as "Best".
                estimate = None if row['format']=='mp4compat' else info.get('qualitySizes',{}).get(str(h),{}).get('bytes')
                if h == row['height'] and row['size'] and json.loads(row['files']):
                    weight = size_text(row['size'])
                else:
                    weight = '≈ '+size_text(estimate) if estimate else 'размер неизвестен'
                label = 'Лучшее' if h==0 else f'до {h}p'
                buttons.append({'text':f'{label} · {weight}','callback_data':f'height:{row["id"]}:{h}'})
        text = ('Совместимый MP4 · H.264/AAC\nВидео будет перекодировано; разрешение сохраняется.' if row['format']=='mp4compat' else f"Качество и формат · {row['format'].upper()}\n≈ — примерный размер.")
        if row['format'] in ('mp3','m4a'):
            text = f"{row['format'].upper()} · только звук\nРазмер: "+(size_text(row['size']) if row['size'] and json.loads(row['files']) else 'после скачивания')
        keyboard = [buttons[i:i+2] for i in range(0,len(buttons),2)]+[
            [{'text':('MP4 совместимый' if fmt=='mp4compat' else fmt.upper()),'callback_data':f'format:{row["id"]}:{fmt}'} for fmt in ('mp4','mp4compat','mkv','mp3','m4a')],
            [{'text':'Назад','callback_data':'back:'+row['id']}]]
        return text,keyboard

    def job_card(self, row, view='status'):
        title = re.sub(r'\s+',' ',row['title']).strip()[:90] or 'Видео'
        state = row['state']
        if view=='quality' and state=='choice':
            return self.quality_card(row)
        if (view=='result' or state=='done') and row['mode']=='link' and row['youtube_id']:
            return title+'\nГотово · доступ по ссылке',[[{'text':'Открыть видео','url':'https://youtu.be/'+row['youtube_id']}]]
        if (view=='result' or state=='done') and row['mode']=='file' and row['expires'] and json.loads(row['files']) and self.base:
            buttons = [[{'text':f'Скачать файл {i+1}','url':item['url']}] for i,item in enumerate(self.file_links(row))]
            return title+f"\nСкачать до {local_time(row['expires'])} (Минск)",buttons
        if state=='choice':
            parts=[title]
            if row['size'] and json.loads(row['files']):
                media=json.loads(row['metadata']).get('downloadedMedia',{})
                dimensions=f"{media['width']}×{media['height']} · " if media.get('width') and media.get('height') else ''
                parts.append(dimensions+size_text(row['size']))
            else:
                parts.append(row['format'].upper()+' · '+(f"до {row['height']}p" if row['height'] else 'лучшее качество'))
            if not self.base and row['size']>self.tg.limit:
                parts.append('Больше лимита Telegram. Выберите качество ниже; ссылки на большие файлы ещё не настроены.')
            return '\n'.join(parts),self.choice_buttons(row)
        if state in ('error','review'):
            message = 'Не удалось подтвердить отправку. Проверьте результат в чате.' if state=='review' else self.user_error(row['error'])
            return title+'\n'+message,[]
        status = {'queued':'Обрабатываю…','inspecting':'Проверяю ссылку…','downloading':'Скачиваю…',
                  'uploading':'Загружаю на YouTube…','delivering':'Отправляю…','done':'Готово · файл в чате',
                  'cancelled':'Отменено','expired':'Срок хранения истёк'}.get(state,'Обрабатываю…')
        active = state in ('queued','inspecting','downloading','uploading','delivering')
        if active and row['cancelled']:
            status = 'Отменяю…'
        buttons = [[{'text':'Выбрать качество','callback_data':'quality:'+row['id']},
                    {'text':'Отменить','callback_data':'cancel:'+row['id']}]] if active and not row['cancelled'] and row['mode']=='auto' else (
                  [[{'text':'Отменить','callback_data':'cancel:'+row['id']}]] if active and not row['cancelled'] else [])
        return title+'\n'+status,buttons

    def show_job(self, jid, view='status'):
        # All transitions edit one persisted card; a failed edit never creates a second card.
        with self.ui_lock:
            row = self.store.job(jid)
            # Browser tasks are viewed through the authenticated API, not mirrored into Telegram.
            if row['entrypoint'] == 'extension':
                return {'ok': True}
            text,buttons = self.job_card(row,view)
            try:
                if row['telegram_message_id']:
                    return self.tg.edit(row['user_id'],row['telegram_message_id'],text,buttons)
                response = self.tg.say(row['user_id'],text,buttons)
                if isinstance(response,dict) and isinstance(response.get('message_id'),int):
                    self.store.update(jid,telegram_message_id=response['message_id'])
                return response
            except ValueError:
                self.last_error = 'Не удалось обновить карточку задания.'
                return None

    def clear_keyboard(self, uid, message_id):
        if message_id:
            try:
                self.tg.call('editMessageReplyMarkup',chat_id=uid,message_id=message_id,reply_markup={'inline_keyboard':[]})
            except ValueError:
                pass

    def ask_choice(self, row):
        self.store.update(row['id'],state='choice',expires=time.time()+FILE_TTL)
        return self.show_job(row['id'])

    def handle(self, update):
        callback = update.get('callback_query')
        msg = callback.get('message',{}) if callback else update.get('message',{})
        sender = callback.get('from',{}) if callback else msg.get('from',{})
        if not sender.get('id') or msg.get('chat',{}).get('type') != 'private':
            return
        uid = sender['id']
        u = self.store.user(uid, ' '.join(filter(None,(sender.get('first_name'),sender.get('last_name')))), sender.get('username',''))
        text = msg.get('text','').strip()
        if callback and u['role'] not in ('owner','employee'):
            self.tg.call('answerCallbackQuery',callback_query_id=callback['id'],text='Доступ пока не предоставлен.')
            return
        if u['role'] == 'pending':
            if text.split(' ', 1)[0] == '/start' and not callback:
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
        if callback:
            try:
                result = self.handle_callback(uid,callback.get('data',''),msg.get('message_id'))
                toast = result if isinstance(result,str) else ''
            except (ValueError,PermissionError) as error:
                toast = self.user_error(error)
            self.tg.call('answerCallbackQuery',callback_query_id=callback['id'],text=toast[:180])
            return
        try:
            cmd, _, arg = text.partition(' ')
            if cmd == '/start' and arg == 'connect':
                cmd = '/pair'
            if cmd == '/start':
                return self.notify(uid,'Пришли ссылку на видео или пост. Скачаю в лучшем качестве.\n'
                    '/history — история · /help — помощь\nВладелец видит историю скачиваний этого бота.')
            if cmd == '/help':
                content = ('Пришли ссылку на видео или пост. Сначала скачаю видео на сервер, затем предложу «Получить ссылку» (YouTube) или «Получить файл». Картинки поста отправляю файлами без дополнительного сжатия.\n\n'
                    '/history — мои задания\n/quality ССЫЛКА — выбрать качество\n/pair — подключить расширение Chrome\n/cancel ID — отменить\n'
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
                if not self.base or self.downloads_only:
                    return self.notify(uid,'Подключение YouTube готовится. Для первоначального входа нужен OAuth-клиент Google Cloud; пока он не настроен.')
                state = self.store.issue(uid,'oauth',600)
                return self.notify(uid,'Подключи нужный канал через Google. Разрешения на загрузку и проверку роликов получает только сервер VideoDrop.',
                    [[{'text':'Подключить YouTube','url':self.youtube.authorize(state)}]])
            if cmd == '/pair':
                if not self.base or self.downloads_only:
                    raise ValueError('Подключение расширения пока не включено: сейчас работает только бот.')
                code = self.store.issue(uid,'pair',600)
                return self.notify(uid,f'Адрес сервера: {self.base}\nКод расширения: {code}\nДействует 10 минут, один раз. Не пересылай его другим людям.')
            if cmd in ('/cancel','/delete'):
                return self.handle_callback(uid,cmd[1:]+':'+arg)
            if cmd == '/quality':
                urls = re.findall(r'https?://[^\s<>]+', arg)
                if len(urls) != 1:
                    raise ValueError('Напиши /quality и одну ссылку на видео.')
                with self.ui_lock:
                    row = self.store.create(uid,clean_source({'url':urls[0]}))
                    self.store.setting('choose_quality_'+row['id'], '1')
                    self.show_job(row['id'])
                return
            if cmd.startswith('/'):
                return self.notify(uid,'Отправь ссылку или нажми /help.')
            urls = re.findall(r'https?://[^\s<>]+', text)
            if len(urls) != 1:
                raise ValueError('Пришли одну ссылку на видео или пост.')
            with self.ui_lock:
                row = self.store.create(uid,clean_source({'url':urls[0]}))
                self.show_job(row['id'])
        except (ValueError,PermissionError) as error:
            self.notify(uid,self.user_error(error))

    def handle_callback(self, uid, data, message_id=None):
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
        if message_id and row['user_id']==uid:
            if row['telegram_message_id'] and row['telegram_message_id']!=message_id:
                self.clear_keyboard(uid,message_id)
                return 'Эта кнопка устарела. Используйте последнюю карточку видео.'
            if not row['telegram_message_id']:
                self.store.update(jid,telegram_message_id=message_id)
        if action == 'quality' and row['entrypoint']=='telegram' and row['mode']=='auto' and row['state'] in ('queued','inspecting','downloading'):
            self.store.setting('choose_quality_'+jid, '1')
            return 'Покажу выбор качества после скачивания.'
        if action in ('link','file','quality','height','format','back','cancel'):
            if row['state'] in ('done','error','review','expired','cancelled'):
                if message_id:
                    self.clear_keyboard(uid,message_id)
                return 'Видео уже отправлено.' if row['state']=='done' else 'Это задание закрыто. Пришлите ссылку заново.'
            if row['expires'] and row['expires']<=time.time():
                self.remove_files(row)
                self.store.update(jid,state='expired',source=None,files='[]',expires=None)
                self.show_job(jid)
                return 'Срок хранения истёк. Пришлите ссылку заново.'
            if action!='cancel' and row['state']!='choice':
                return 'Уже обрабатываю видео.'
        if action in ('link','file'):
            if action == 'link' and row['format'] not in ('mp4','mp4compat','mkv'):
                raise ValueError('Для YouTube выбери видео MP4 или MKV. Аудио можно получить файлом.')
            if action == 'link' and not self.youtube.ready():
                self.show_job(jid)
                return 'Загрузка на YouTube ещё не подключена.'
            if action=='file' and not self.base and (row['entrypoint']=='extension' or row['size']>self.tg.limit):
                self.show_job(jid)
                return 'Файл больше лимита Telegram. Выберите качество ниже.'
            self.store.choose(jid,uid,action)
            return self.show_job(jid)
        if action == 'quality':
            return self.show_job(jid,'quality')
        if action == 'back':
            return self.show_job(jid)
        if action in ('height','format'):
            setting = value.split(':',1)[1]
            if action == 'height' and int(setting) in [0]+json.loads(row['metadata']).get('heights',[]):
                change = {'height':int(setting)}
            elif action == 'format' and setting in ('mp4','mp4compat','mkv','mp3','m4a'):
                change = {'format':setting}
            else:
                raise ValueError('Некорректный выбор.')
            with self.store.transaction():
                row = self.store.job(jid,uid)
                if row['state'] != 'choice' or (row['expires'] and row['expires'] <= time.time()):
                    raise ValueError('Задание уже выполняется, завершено или срок выбора истёк.')
                if any(row[k] != v for k,v in change.items()):
                    self.remove_files(row)
                    info=json.loads(row['metadata']); info.pop('downloadedMedia',None)
                    self.store.update(jid,**change,files='[]',size=0,metadata=json.dumps(info))
            return self.show_job(jid)
        if action == 'cancel':
            self.store.update(jid,cancelled=1)
            if row['state'] in ('choice','queued'):
                self.remove_files(row)
                self.store.update(jid,state='cancelled',source=None)
            self.store.event(jid,row['user_id'],'cancelled')
            return self.show_job(jid)
        if action == 'delete':
            if row['state'] not in ('done','error','review','expired','cancelled'):
                raise ValueError('Сначала отмените активное задание.')
            self.remove_files(row)
            self.store.update(jid,state='expired',source=None,files='[]',expires=None)
            self.store.event(jid,row['user_id'],'file_deleted')
            return self.show_job(jid)
        raise ValueError('Неизвестная кнопка.')

    @staticmethod
    def describe(row):
        return (f"{local_time(row['created'])} · {row['name']} @{row['username']}\n"
                f"{row['title'] or 'Проверка ссылки'}\n{row['url']}\n"
                f"{LABELS.get(row['state'],row['state'])} · {size_text(row['size'])} · {row['mode']}\n"
                f"ID: {row['id']}")

    def disk_used(self, path=None):
        total = 0
        for item in (path or self.files).rglob('*'):
            try:
                info = item.lstat()
            except FileNotFoundError:
                # FFmpeg/yt-dlp rename .part files while the quota scan is running.
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
        return total

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
                   'format':row['format'], 'height':row['height'],
                   'kind':json.loads(row['metadata']).get('kind')}
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

    def preview(self, row, paths):
        """Make a separate compatible copy; never alter the original media."""
        info = json.loads(row['metadata'])
        media = info.get('downloadedMedia',{})
        if (row['entrypoint'] != 'telegram' or info.get('kind') != 'video'
                or len(paths) != 1 or not media.get('videoCodec')):
            return
        if self.store.one("SELECT 1 FROM events WHERE job_id=? AND kind='telegram_preview_delivered'",(row['id'],)):
            return
        preview = paths[0].parent/'.telegram-preview.mp4'
        # Reserve space for audio and container overhead when targeting the Bot API limit.
        duration = media.get('duration') or 0
        target = int(self.tg.limit * 8 * .85 / duration - 96000) if duration else 1800000
        if target < 80000:
            self.notify(row['user_id'],'Видео слишком длинное для версии просмотра в Telegram. Отправляю оригинал.')
            return
        bitrate = min(target, 900000)
        args = ['ffmpeg','-nostdin','-v','error','-y','-i',str(paths[0]),
            '-map','0:v:0','-map','0:a:0?', '-vf',
            "scale=w='max(2,trunc(iw*min(1,720/max(iw,ih))/2)*2)':h='max(2,trunc(ih*min(1,720/max(iw,ih))/2)*2)'",
            '-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p','-profile:v','main',
            '-crf','28','-maxrate',str(bitrate),'-bufsize',str(bitrate*2),
            '-c:a','aac','-b:a','96k','-movflags','+faststart',str(preview)]
        process = None
        try:
            process = subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            deadline = time.monotonic()+7200
            while process.poll() is None:
                self.worker_seen = time.time()
                if self.cancelled(row['id']):
                    raise ValueError('Отправка отменена.')
                if (time.monotonic()>deadline or self.disk_used()>self.budget
                        or self.disk_used(paths[0].parent)>self.per_job or shutil.disk_usage(self.files).free<2*GIB):
                    raise ValueError('Не удалось подготовить видео для просмотра.')
                self.stop.wait(1)
            if process.returncode or not preview.is_file() or preview.stat().st_size > self.tg.limit:
                raise ValueError('Не удалось подготовить видео для просмотра.')
            self.tg.video(row['user_id'],preview,'Для просмотра')
            self.store.event(row['id'],row['user_id'],'telegram_preview_delivered')
        except (ValueError,OSError):
            if self.cancelled(row['id']):
                raise ValueError('Отправка отменена.')
            self.store.event(row['id'],row['user_id'],'telegram_preview_failed')
            self.notify(row['user_id'],'Не удалось подготовить видео для просмотра. Отправляю оригинал.')
        finally:
            if process and process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL); process.wait()
            preview.unlink(missing_ok=True)

    def deliver(self, row):
        paths = self.paths(row)
        if row['mode']=='link':
            if len(paths)!=1 or paths[0].suffix.lower() not in ('.mp4','.mkv'):
                raise ValueError('YouTube принимает видео. Для картинок и аудио выберите файл.')
            self.store.update(row['id'],state='uploading')
            self.show_job(row['id'])
            def checkpoint(kind,value):
                if kind=='video':
                    self.store.update(row['id'],youtube_id=value)
                else:
                    self.store.setting('upload_session_'+row['id'],value)
            video = self.youtube.upload(paths[0],row['title'],checkpoint,lambda:self.cancelled(row['id']),row['youtube_id'])
            self.store.event(row['id'],row['user_id'],'youtube_verified',video)
            self.store.update(row['id'],state='delivering',youtube_id=video)
            if not self.show_job(row['id'],'result'):
                raise ValueError('Не удалось подтвердить отправку ссылки в Telegram.')
            self.store.event(row['id'],row['user_id'],'telegram_link_delivered')
            self.remove_files(row)
            self.store.update(row['id'],state='done',source=None,files='[]',expires=None)
            return
        self.store.update(row['id'],state='delivering')
        self.show_job(row['id'])
        self.preview(row, paths)
        if any(p.stat().st_size>self.tg.limit for p in paths) or row['entrypoint']=='extension':
            if not self.base:
                raise ValueError(f'Для файла больше лимита Telegram ({size_text(self.tg.limit)}) или задания из расширения нужна прямая HTTPS-ссылка с сервера. Она пока не подключена. Файл сохранён на 24 часа без сжатия.')
            until = time.time()+FILE_TTL
            self.store.update(row['id'],expires=until,state='delivering')
            if not self.show_job(row['id'],'result'):
                raise ValueError('Не удалось подтвердить отправку ссылки в Telegram.')
            self.store.event(row['id'],row['user_id'],'file_link_delivered')
            self.store.update(row['id'],state='done',source=None)
            return
        self.store.update(row['id'],state='delivering')
        self.show_job(row['id'])
        for index,path in enumerate(paths):
            if index < row['delivery_index']:
                continue
            if self.cancelled(row['id']):
                raise ValueError('Отправка отменена.')
            label = 'Оригинал · без дополнительного сжатия' if row['format'] in ('mp4','mkv') else 'Файл · выбранный формат'
            caption = '' if len(paths)>1 or json.loads(row['metadata']).get('kind')=='images' else '\n'.join(filter(None,(label,self.media_note(row))))
            self.tg.document(row['user_id'],path,caption)
            self.store.update(row['id'],delivery_index=index+1)
            self.store.event(row['id'],row['user_id'],'telegram_file_delivered',path.name)
        self.remove_files(row)
        self.store.update(row['id'],state='done',source=None,files='[]',expires=None)
        self.show_job(row['id'])

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
            if row['entrypoint'] == 'extension' and row['mode'] == 'auto':
                # Inspect first: choose quality, size and delivery before downloading bytes.
                return self.ask_choice(row)
            if not json.loads(row['files']):
                if self.budget-self.disk_used()<GIB:
                    raise ValueError('На сервере мало временного места. Попробуйте позже.')
                self.store.update(jid,state='downloading')
                self.show_job(jid)
                result = self.media(row,'download')
                directory = (self.files/jid).resolve()
                paths = [Path(p) for p in result.get('paths', [result.get('path','')])]
                for p in paths:
                    if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(directory):
                        raise ValueError('Источник вернул недопустимый файл.')
                size = sum(p.stat().st_size for p in paths)
                if size>self.per_job:
                    raise ValueError('Файл превышает лимит размера одного задания.')
                if result.get('media'):
                    info['downloadedMedia'] = result['media']
                    if result['media'].get('duration'):
                        for estimate in info.get('qualitySizes',{}).values():
                            if estimate.get('bytes') is None and estimate.get('bytesPerSecond'):
                                rate = estimate['bytesPerSecond']
                                if estimate.get('needsAudioBitrate'):
                                    if not result['media'].get('audioBitrate'):
                                        continue
                                    rate += result['media']['audioBitrate']/8
                                estimate['bytes'] = round(rate*result['media']['duration'])
                    self.store.event(jid,row['user_id'],'media_verified',json.dumps(result['media']))
                self.store.update(jid,files=json.dumps([str(p.relative_to(directory)) for p in paths]),size=size,metadata=json.dumps(info))
                self.store.event(jid,row['user_id'],'download_ready',str(size))
                row = self.store.job(jid)
            if row['mode']=='auto' and info.get('kind')!='images':
                # Telegram can deliver small files immediately without re-encoding.
                # Larger videos must wait for an explicit delivery choice.
                if row['entrypoint'] != 'telegram' or row['size'] > self.tg.limit or self.store.setting('choose_quality_'+jid):
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
            if not isinstance(e,(ValueError,PermissionError)):
                # Keep useful diagnostics without exception strings, request data or secrets.
                frames = [f'{Path(f.filename).name}:{f.name}:{f.lineno}' for f in traceback.extract_tb(e.__traceback__)[-6:]]
                self.store.event(jid,row['user_id'],'internal_error',json.dumps({'type':type(e).__name__,'frames':frames}))
            # Never include raw network exception strings (URLs can contain credentials).
            error = str(e)[:700] if isinstance(e,(ValueError,PermissionError)) else 'Не удалось завершить задание. Файл, если готов, сохранён на 24 часа.'
            state = 'cancelled' if row['cancelled'] else ('review' if row['state'] in ('uploading','delivering') else 'error')
            self.store.update(jid,state=state,error=error,source=None,expires=time.time()+86400)
            if state=='cancelled' or not json.loads(row['files']):
                self.remove_files(row)
                self.store.update(jid,files='[]')
            self.store.event(jid,row['user_id'],state,error)
            self.show_job(jid)

    def cleanup(self):
        # Serialize expiry with choosing a result so an accepted job cannot lose its file.
        with self.store.transaction() as db:
            rows = self.store.all("SELECT * FROM jobs WHERE expires<? AND state IN ('done','error','review','choice','cancelled')",(time.time(),))
            for row in rows:
                self.remove_files(row)
                self.store.update(row['id'],state='expired',source=None,files='[]',expires=None)
                self.store.event(row['id'],row['user_id'],'expired')
            db.execute('DELETE FROM tokens WHERE expires<?',(time.time(),))
        for row in rows:
            if row['telegram_message_id']:
                self.show_job(row['id'])

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
