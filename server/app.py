"""Loopback health/API gateway. Public access requires an explicitly configured HTTPS base."""
import json
import os
import threading
import time
from functools import wraps

from flask import Flask, jsonify, redirect, request, send_file
from werkzeug.serving import make_server, WSGIRequestHandler

from server.media import clean_source
from server.service import Service, VERSION


def create_app(service):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 256*1024
    limits, lock = {}, threading.Lock()

    @app.before_request
    def boundary():
        # No public access, pairing, files, or browser credentials in Telegram-only mode.
        if not service.base and request.path != '/health':
            return jsonify(error='Сейчас включён только Telegram-бот.'), 404
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
        if origin.startswith('chrome-extension://') and len(origin)==51:
            response.headers['Access-Control-Allow-Origin']=origin
            response.headers['Vary']='Origin'
            response.headers['Access-Control-Allow-Headers']='Authorization,Content-Type'
            response.headers['Access-Control-Allow-Methods']='GET,POST,OPTIONS'
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
            mode='telegram' if not service.base else 'telegram-and-extension')

    @app.post('/api/pair')
    def pair():
        row=service.store.resolve(str((request.get_json() or {}).get('code','')),'pair',consume=True)
        return jsonify(token=service.store.issue(row['user_id'],'api',90*86400),
                       role=service.store.get_user(row['user_id'])['role'])

    @app.post('/api/jobs')
    @auth()
    def create_job(uid):
        body=request.get_json() or {}
        mode = body.get('mode')
        if mode not in ('link','file'):
            raise ValueError('Выберите способ получения до начала скачивания: link (YouTube) или file (файл).')
        row=service.store.create(uid,clean_source(body),mode=mode,
            container=body.get('format','mp4'),height=int(body.get('height',0)),entrypoint='extension')
        return jsonify(id=row['id'],state=row['state'],message='Задание принято.',resultPath='/api/jobs/'+row['id']),202

    @app.get('/api/jobs/<jid>')
    @auth()
    def job_result(uid,jid):
        row=service.store.job(jid,uid)
        files=service.file_links(row) if row['state']=='done' and row['mode']=='file' and row['expires'] and row['expires']>time.time() else []
        return jsonify(id=row['id'],state=row['state'],title=row['title'],size=row['size'],error=row['error'],
            expires=row['expires'],files=files,media=json.loads(row['metadata']).get('downloadedMedia'),
            youtubeUrl='https://youtu.be/'+row['youtube_id'] if row['state']=='done' and row['youtube_id'] else None)

    @app.get('/api/jobs')
    @auth()
    def history(uid):
        return jsonify(jobs=service.store.history(uid,employee=uid))

    @app.get('/api/audit')
    @auth(owner=True)
    def audit(uid):
        return jsonify(jobs=service.store.history(uid))

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
        telegram_local=os.environ.get('VIDEODROP_TELEGRAM_LOCAL')=='1')
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
