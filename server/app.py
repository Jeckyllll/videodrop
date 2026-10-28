"""Loopback health/API gateway. Public access requires an explicitly configured HTTPS base."""
import json
import os
import re
import threading
import time
from functools import wraps

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory
from pathlib import Path
from werkzeug.serving import make_server, WSGIRequestHandler

from server.media import clean_source, public_url
from server.service import Service, VERSION


def create_app(service):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 256*1024
    limits, lock = {}, threading.Lock()
    web = Path(__file__).with_name('web')

    def body_json():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError('Некорректный запрос.')
        return body

    @app.before_request
    def boundary():
        # No public access, pairing, files, or browser credentials in Telegram-only mode.
        if not service.base and request.path != '/health':
            return jsonify(error='Сейчас включён только Telegram-бот.'), 404
        if service.downloads_only and request.path != '/health' and not request.path.startswith('/download/'):
            return jsonify(error='Сейчас доступны только файлы из Telegram-бота.'), 404
        if service.base and not service.base.startswith('https://'):
            return jsonify(error='Серверу требуется HTTPS.'),503
        if request.path in ('/api/pair','/oauth/callback'):
            key = (request.remote_addr, int(time.time()//60))
            with lock:
                limits[key] = limits.get(key,0)+1
                if limits[key]>10:
                    return jsonify(error='Слишком много попыток. Подождите минуту.'),429
                for old in list(limits):
                    if old[1] < key[1]-1:
                        del limits[old]

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
            'X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})
        origin = request.headers.get('Origin','')
        if re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
            response.headers['Access-Control-Allow-Origin']=origin
            response.headers['Vary']='Origin'
            response.headers['Access-Control-Allow-Headers']='Authorization,Content-Type'
            response.headers['Access-Control-Allow-Methods']='GET,POST,OPTIONS'
        if request.path.startswith('/app'):
            response.headers['Content-Security-Policy'] = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'self' chrome-extension:")
        return response

    @app.errorhandler(ValueError)
    def invalid(e):
        return jsonify(error=str(e)),400

    @app.errorhandler(PermissionError)
    def forbidden(e):
        return jsonify(error=str(e)),403

    def auth(owner=False):
        def decorate(fn):
            @wraps(fn)
            def wrapped(*args,**kwargs):
                token = request.headers.get('Authorization','').removeprefix('Bearer ')
                user = service.store.resolve(token,'api')['user_id']
                service.store.require(user,owner=owner)
                return fn(user,*args,**kwargs)
            return wrapped
        return decorate

    @app.get('/health')
    def health():
        return jsonify(service='VideoDrop',version=VERSION,telegramConfigured=bool(service.tg.token),
            telegramConnected=time.time()-service.poll_seen<90,youtubeConnected=service.youtube.ready(),
            mode='telegram' if not service.base else ('telegram-with-files' if service.downloads_only else 'telegram-and-extension'))

    @app.post('/api/pair')
    def pair():
        row=service.store.resolve(str(body_json().get('code','')).strip().lower(),'pair',consume=True)
        return jsonify(token=service.store.issue(row['user_id'],'api',90*86400),
                       role=service.store.get_user(row['user_id'])['role'])

    @app.get('/app/')
    def browser_app():
        return send_from_directory(web, 'index.html')

    @app.get('/app/<path:name>')
    def browser_asset(name):
        if name not in ('app.js', 'app.css', 'VideoDrop-Chrome.zip'):
            return '', 404
        return send_from_directory(web, name, as_attachment=name.endswith('.zip'))

    @app.get('/api/session')
    @auth()
    def session(uid):
        user=service.store.get_user(uid)
        return jsonify(name=user['name'], role=user['role'], version=VERSION,
            youtube=service.youtube.ready(), fileTTL=86400,
            maxFileBytes=service.per_job)

    @app.post('/api/logout')
    @auth()
    def logout(uid):
        from server.store import digest
        token=request.headers.get('Authorization','').removeprefix('Bearer ')
        with service.store.transaction() as db:
            db.execute("DELETE FROM tokens WHERE hash=? AND user_id=? AND kind='api'",(digest(token),uid))
        return jsonify(ok=True)

    @app.post('/api/jobs')
    @auth()
    def create_job(uid):
        body=body_json()
        mode = body.get('mode')
        prepare = body.get('prepare') is True
        if not prepare and mode not in ('link','file'):
            raise ValueError('Выберите способ получения до начала скачивания: link (YouTube) или file (файл).')
        if not prepare and mode=='link' and not service.youtube.ready():
            raise ValueError('Загрузка на YouTube ещё не подключена. Можно получить файл.')
        row=service.store.create(uid,clean_source(body),mode='auto' if prepare else mode,
            container=body.get('format','mp4'),height=int(body.get('height',0)),entrypoint='extension')
        return jsonify(id=row['id'],state=row['state'],message='Задание принято.',resultPath='/api/jobs/'+row['id']),202

    @app.get('/api/jobs/<jid>')
    @auth()
    def job_result(uid,jid):
        row=service.store.job(jid,uid)
        metadata=json.loads(row['metadata'])
        files=service.file_links(row) if row['state']=='done' and row['mode']=='file' and row['expires'] and row['expires']>time.time() else []
        info={key:metadata.get(key) for key in ('kind','heights','qualitySizes','duration','imageCount') if key in metadata}
        return jsonify(id=row['id'],state=row['state'],title=row['title'],size=row['size'],error=service.user_error(row['error']) if row['error'] else '',
            expires=row['expires'],files=files,media=json.loads(row['metadata']).get('downloadedMedia'),
            info=info,format=row['format'],height=row['height'],mode=row['mode'],cancelled=bool(row['cancelled']),
            localDownload=metadata.get('localDownload') is True,
            youtubeUrl='https://youtu.be/'+row['youtube_id'] if row['state']=='done' and row['youtube_id'] else None)

    @app.post('/api/local-history')
    @auth()
    def local_history(uid):
        body=body_json()
        # This validates only the page URL; no fetch is made and authentication query fields are discarded.
        url=public_url(body.get('url'),resolve=False)
        row=service.store.record_local_download(uid,body.get('eventId'),url,body.get('title',''),body.get('height',0))
        return jsonify(id=row['id']),200

    @app.post('/api/jobs/<jid>/start')
    @auth()
    def start_job(uid,jid):
        body=body_json()
        with service.store.transaction():
            row=service.store.job(jid,uid)
            info=json.loads(row['metadata'])
            mode,container=body.get('mode'),body.get('format','mp4')
            height=body.get('height',0)
            if row['entrypoint']!='extension' or row['state']!='choice' or row['cancelled']:
                raise ValueError('Задание уже запущено или закрыто.')
            if mode not in ('file','link') or container not in ('mp4','mp4compat','mkv','mp3','m4a'):
                raise ValueError('Выберите способ получения и формат.')
            if type(height) is not int or height not in [0]+info.get('heights',[]):
                raise ValueError('Выберите доступное качество.')
            if mode=='link' and (container not in ('mp4','mp4compat','mkv') or info.get('kind')=='images'):
                raise ValueError('На YouTube можно загрузить только видео.')
            if mode=='link' and not service.youtube.ready():
                raise ValueError('Загрузка на YouTube ещё не подключена. Можно получить файл.')
            service.store.choose(jid,uid,mode)
            service.store.update(jid,format=container,height=height)
        return jsonify(id=jid,state='queued'),202

    @app.post('/api/jobs/<jid>/cancel')
    @auth()
    def cancel_job(uid,jid):
        with service.ui_lock:
            row=service.store.job(jid,uid)
            if row['entrypoint']!='extension':
                raise ValueError('Это задание создано в Telegram.')
            service.handle_callback(uid,'cancel:'+jid)
        return jsonify(ok=True)

    @app.get('/api/jobs')
    @auth()
    def history(uid):
        rows=service.store.history(uid,employee=uid)
        for row in rows:
            row['error']=service.user_error(row['error']) if row['error'] else ''
        return jsonify(jobs=rows)

    @app.get('/api/audit')
    @auth(owner=True)
    def audit(uid):
        rows=service.store.history(uid,employees_only=True)
        for row in rows:
            row['error']=service.user_error(row['error']) if row['error'] else ''
        return jsonify(jobs=rows)

    @app.get('/oauth/callback')
    def oauth_callback():
        state=request.args.get('state','')
        row=service.store.resolve(state,'oauth',consume=True)
        service.store.require(row['user_id'],owner=True)
        if request.args.get('error'):
            return 'Подключение отменено. Вернитесь в Telegram.',400
        ch=service.youtube.finish(state,request.args.get('code',''))
        service.notify(row['user_id'],'Канал подключён: '+ch['title']+'. Перед рабочими загрузками проверим тестовый ролик с доступом «По ссылке».')
        return 'YouTube подключён. Можно вернуться в Telegram.'

    @app.get('/download/<token>/<int:index>')
    def download(token,index):
        permit=service.store.resolve(token,'file')
        row=service.store.job(permit['job_id'],permit['user_id'])
        if row['state']!='done' or not row['expires'] or row['expires']<time.time():
            return 'Срок хранения файла истёк.',410
        paths=service.paths(row)
        if index<0 or index>=len(paths):
            return 'Файл не найден.',404
        service.store.event(row['id'],row['user_id'],'download_started',str(index))
        return send_file(paths[index],as_attachment=True,conditional=True,max_age=0)

    return app


def main():
    os.umask(0o077)
    owner=int(os.environ['VIDEODROP_OWNER_ID'])
    service=Service(os.environ.get('VIDEODROP_STATE','/data'),owner,
        token=os.environ.get('VIDEODROP_BOT_TOKEN',''),public_base=os.environ.get('VIDEODROP_PUBLIC_URL',''),
        telegram_base=os.environ.get('VIDEODROP_TELEGRAM_URL','https://api.telegram.org'),
        telegram_local=os.environ.get('VIDEODROP_TELEGRAM_LOCAL')=='1',
        downloads_only=os.environ.get('VIDEODROP_PUBLIC_MODE','files')=='files')
    service.start()
    # Reverse proxy, if added later, must terminate HTTPS. Compose only publishes loopback.
    class QuietHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs):
            # Download/OAuth URLs contain bearer capabilities. Never log request targets.
            pass
    server=make_server('0.0.0.0',8080,create_app(service),threaded=True,request_handler=QuietHandler)
    try:
        server.serve_forever()
    finally:
        service.stop.set()


if __name__=='__main__':
    main()
