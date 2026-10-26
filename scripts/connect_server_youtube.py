#!/usr/bin/env python3
"""One-time desktop OAuth setup for Telegram-only servers; no public domain required.

Credentials travel through SSH stdin to the selected VideoDrop server, never in argv/logs.
The Google authorization URL is written to an owner-only temporary file for the browser.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.youtube import SCOPES


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--client',required=True,type=Path,help='Downloaded Desktop OAuth client JSON')
    parser.add_argument('--host',default='contabo-main')
    args=parser.parse_args()
    config=json.loads(args.client.read_text())
    if 'installed' not in config:
        raise SystemExit('Нужен OAuth-клиент типа Desktop app.')
    flow=InstalledAppFlow.from_client_config(config,SCOPES)
    directory=ROOT/'.server-state'
    directory.mkdir(mode=0o700,exist_ok=True)
    urlfile=directory/'youtube-authorization-url.txt'
    original=flow.authorization_url
    def authorization_url(*a,**kw):
        url,state=original(*a,**kw)
        urlfile.write_text(url)
        urlfile.chmod(0o600)
        print('Откройте адрес из .server-state/youtube-authorization-url.txt в браузере. Ожидание до 5 минут.',flush=True)
        return url,state
    flow.authorization_url=authorization_url
    try:
        credentials=flow.run_local_server(host='127.0.0.1',port=0,open_browser=False,
            authorization_prompt_message='',success_message='Разрешение получено. Можно вернуться в VideoDrop.',
            timeout_seconds=300,access_type='offline',prompt='consent')
        if not credentials or not credentials.refresh_token:
            raise SystemExit('Google не выдал длительное разрешение.')
        session=AuthorizedSession(credentials)
        response=session.get('https://www.googleapis.com/youtube/v3/channels',params={'part':'snippet','mine':'true'},timeout=30)
        response.raise_for_status()
        channels=response.json().get('items',[])
        if len(channels)!=1:
            raise SystemExit('При входе нужно выбрать один YouTube-канал.')
        channel={'id':channels[0]['id'],'title':channels[0]['snippet']['title']}
        remote='''import json,os,sys
from pathlib import Path
from cryptography.fernet import Fernet
data=json.load(sys.stdin)
root=Path('/data')
key=(root/'secret.key').read_bytes()
temp=root/'youtube-token.tmp'
temp.write_bytes(Fernet(key).encrypt(data['credentials'].encode()))
temp.chmod(0o600)
temp.replace(root/'youtube-token.enc')
target=root/'youtube-channel.json'
target.write_text(json.dumps(data['channel'],ensure_ascii=False))
target.chmod(0o600)
print('YouTube connected:',data['channel']['title'])
'''
        import shlex
        command='sudo -n docker compose -f /srv/apps/videodrop/server/compose.yaml exec -T app python -c '+shlex.quote(remote)
        subprocess.run(['ssh','-o','BatchMode=yes',args.host,command],
            input=json.dumps({'credentials':credentials.to_json(),'channel':channel}),text=True,check=True)
    finally:
        urlfile.unlink(missing_ok=True)


if __name__=='__main__':
    main()
