"""YouTube OAuth and resumable uploads; visibility is always verified."""
import json
import os
import time
from pathlib import Path

import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly']


class YouTube:
    def __init__(self, root, cipher, base):
        self.root, self.cipher, self.base = Path(root), cipher, base
        self.client = self.root / 'youtube-client.json'
        self.token = self.root / 'youtube-token.enc'
        self.channel = self.root / 'youtube-channel.json'

    def ready(self):
        return self.token.is_file() and self.channel.is_file()

    def flow(self, state):
        if not self.client.is_file():
            raise ValueError('YouTube пока не подключён: сначала настройте OAuth в Google Cloud.')
        return Flow.from_client_secrets_file(str(self.client), scopes=SCOPES, state=state,
                    redirect_uri=self.base+'/oauth/callback')

    def authorize(self, state):
        return self.flow(state).authorization_url(access_type='offline', prompt='consent', include_granted_scopes='false')[0]

    def save_credentials(self, credentials):
        temp = self.token.with_suffix('.tmp')
        temp.write_bytes(self.cipher.encrypt(credentials.to_json().encode()))
        os.chmod(temp, 0o600)
        temp.replace(self.token)

    def finish(self, state, code):
        flow = self.flow(state)
        flow.fetch_token(code=code)
        credentials = flow.credentials
        if not credentials.refresh_token:
            raise ValueError('Google не вернул длительное разрешение. Подключите канал заново.')
        session = AuthorizedSession(credentials)
        response = session.get('https://www.googleapis.com/youtube/v3/channels',
                               params={'part':'snippet','mine':'true'}, timeout=30)
        response.raise_for_status()
        channels = response.json().get('items',[])
        if len(channels) != 1:
            raise ValueError('Не удалось однозначно определить канал. Выберите канал при входе.')
        ch = {'id':channels[0]['id'], 'title':channels[0]['snippet']['title']}
        self.save_credentials(credentials)
        self.channel.write_text(json.dumps(ch, ensure_ascii=False))
        os.chmod(self.channel, 0o600)
        return ch

    def session(self):
        if not self.ready():
            raise ValueError('Канал YouTube ещё не подключён. Можно получить файл.')
        creds = Credentials.from_authorized_user_info(json.loads(self.cipher.decrypt(self.token.read_bytes())))
        if not creds.valid:
            creds.refresh(Request())
            self.save_credentials(creds)
        return AuthorizedSession(creds)

    def upload(self, path, title, checkpoint, cancelled, existing_id=''):
        session = self.session()
        expected = json.loads(self.channel.read_text())['id']
        if existing_id:
            video_id = existing_id
        else:
            size = path.stat().st_size
            response = session.post('https://www.googleapis.com/upload/youtube/v3/videos',
                params={'uploadType':'resumable','part':'snippet,status','notifySubscribers':'false'},
                headers={'X-Upload-Content-Length':str(size),'X-Upload-Content-Type':'application/octet-stream'},
                json={'snippet':{'title':title[:100] or path.name[:100]},
                      'status':{'privacyStatus':'unlisted','selfDeclaredMadeForKids':False}}, timeout=60)
            response.raise_for_status()
            upload_url = response.headers.get('Location','')
            if not upload_url.startswith('https://www.googleapis.com/'):
                raise ValueError('YouTube не вернул адрес загрузки.')
            # Save session URL encrypted before any bytes are sent. Never retry a new insert after ambiguity.
            checkpoint('session', self.cipher.encrypt(upload_url.encode()).decode())
            position, video_id = 0, None
            with path.open('rb') as media:
                while position < size:
                    if cancelled():
                        raise ValueError('Отправка отменена.')
                    media.seek(position)
                    chunk = media.read(8*1024*1024)
                    reply = session.put(upload_url, data=chunk, headers={
                        'Content-Length':str(len(chunk)),
                        'Content-Range':f'bytes {position}-{position+len(chunk)-1}/{size}'}, timeout=180)
                    if reply.status_code == 308:
                        position = int(reply.headers.get('Range','bytes=0--1').split('-')[-1])+1
                    elif reply.status_code in (200,201):
                        video_id = reply.json()['id']
                        checkpoint('video', video_id)
                        break
                    else:
                        reply.raise_for_status()
            if not video_id:
                raise ValueError('YouTube не подтвердил завершение загрузки. Проверьте канал; автоматический повтор отключён.')
        # Keep local file until channel, visibility and processing are all confirmed.
        deadline = time.monotonic()+3600
        while time.monotonic() < deadline:
            if cancelled():
                raise ValueError('Проверка отменена. Видео уже может находиться на YouTube.')
            response = session.get('https://www.googleapis.com/youtube/v3/videos',
                params={'part':'snippet,status,processingDetails','id':video_id}, timeout=30)
            response.raise_for_status()
            items = response.json().get('items',[])
            if items:
                info = items[0]
                status = info['status']
                if info['snippet']['channelId'] != expected:
                    raise ValueError('YouTube вернул другой канал; исходный файл сохранён.')
                if status.get('privacyStatus') != 'unlisted':
                    raise ValueError('YouTube не подтвердил доступ «По ссылке». Исходный файл сохранён.')
                processed = info.get('processingDetails',{}).get('processingStatus')
                if status.get('uploadStatus') in ('failed','rejected','deleted') or processed in ('failed','terminated'):
                    raise ValueError('YouTube отклонил обработку. Исходный файл сохранён.')
                if processed == 'succeeded' and status.get('uploadStatus') == 'processed':
                    return video_id
            time.sleep(15)
        raise ValueError('YouTube ещё обрабатывает видео. Файл сохранён на 24 часа для проверки.')
