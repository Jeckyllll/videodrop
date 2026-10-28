import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.app import create_app
from server.service import Service


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(self.tmp.name,1,public_base='https://video.example')
        self.s.tg=Mock(limit=45*1024*1024,token='test')
        self.s.youtube.ready=Mock(return_value=False)
        self.s.store.user(2,'Employee','employee')
        self.s.store.set_role(1,2,'employee')
        self.token=self.s.store.issue(2,'api',300)
        self.headers={'Authorization':'Bearer '+self.token}
        self.client=create_app(self.s).test_client()

    def tearDown(self):
        self.s.store.db.close();self.tmp.cleanup()

    def prepare(self):
        source={'url':'https://example.com/video','cookies':[{'name':'auth','value':'private','domain':'example.com'}]}
        with patch('server.app.clean_source',side_effect=lambda body:source):
            response=self.client.post('/api/jobs',headers=self.headers,json={'prepare':True,**source})
        self.assertEqual(response.status_code,202)
        row=self.s.store.job(response.json['id'])
        self.s.media=Mock(return_value={'kind':'video','title':'Test video','heights':[1920,1280],
            'qualitySizes':{'0':{'bytes':10000000,'width':1080,'height':1920,'approximate':True}}})
        self.s.run_job(row)
        return self.s.store.job(row['id'])

    def test_preview_does_not_download_or_message_telegram(self):
        row=self.prepare()
        self.assertEqual(row['state'],'choice')
        self.assertEqual([call.args[1] for call in self.s.media.call_args_list],['inspect'])
        self.s.tg.say.assert_not_called()
        result=self.client.get('/api/jobs/'+row['id'],headers=self.headers).json
        self.assertEqual(result['info']['qualitySizes']['0']['width'],1080)
        self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('cookies',json.dumps(result))

    def test_selected_vertical_quality_downloads_once_and_keeps_file_24h(self):
        row=self.prepare();jid=row['id']
        start={'format':'mp4','height':1920,'mode':'file'}
        response=self.client.post('/api/jobs/'+jid+'/start',headers=self.headers,json=start)
        self.assertEqual(response.status_code,202)
        self.assertEqual(self.client.post('/api/jobs/'+jid+'/start',headers=self.headers,json=start).status_code,400)
        def download(current,mode):
            self.assertEqual(mode,'download');self.assertEqual(current['height'],1920)
            folder=self.s.files/jid;folder.mkdir();path=folder/'video.mp4';path.write_bytes(b'media')
            return {'path':str(path),'media':{'width':1080,'height':1920,'videoCodec':'h264','audioCodec':'aac'}}
        self.s.media=Mock(side_effect=download)
        self.s.run_job(self.s.store.job(jid))
        done=self.client.get('/api/jobs/'+jid,headers=self.headers).json
        self.assertEqual(done['state'],'done');self.assertEqual(len(done['files']),1)
        self.assertTrue(86390<done['expires']-time.time()<=86400)
        self.s.tg.document.assert_not_called()
        # The sole permitted Telegram notification is the employee audit to owner.
        self.assertTrue(all(call.args[0]==1 for call in self.s.tg.say.call_args_list))
        self.assertIsNone(self.s.store.job(jid)['source'])
        path=done['files'][0]['url'].replace('https://video.example','')
        response=self.client.get(path);self.assertEqual(response.data,b'media');response.close()

    def test_employee_cannot_read_or_start_another_employees_job(self):
        row=self.prepare()
        self.s.store.user(3,'Other','other');self.s.store.set_role(1,3,'employee')
        foreign={'Authorization':'Bearer '+self.s.store.issue(3,'api',300)}
        for suffix,body in (('',None),('/start',{'mode':'file'}),('/cancel',{})):
            response=self.client.get('/api/jobs/'+row['id'],headers=foreign) if body is None else self.client.post('/api/jobs/'+row['id']+suffix,headers=foreign,json=body)
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.client.get('/api/audit',headers=self.headers).status_code,403)

    def test_block_revokes_pair_and_api_tokens(self):
        code=self.s.store.issue(2,'pair',300)
        self.s.store.set_role(1,2,'blocked')
        self.assertEqual(self.client.get('/api/session',headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/pair',json={'code':code}).status_code,403)

    def test_pair_is_single_use_and_logout_revokes_only_current_token(self):
        code=self.s.store.issue(2,'pair',300)
        response=self.client.post('/api/pair',json={'code':' '+code.upper()+' '})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.post('/api/pair',json={'code':code}).status_code,403)
        other={'Authorization':'Bearer '+response.json['token']}
        self.assertEqual(self.client.post('/api/logout',headers=self.headers,json={}).status_code,200)
        self.assertEqual(self.client.get('/api/session',headers=self.headers).status_code,403)
        self.assertEqual(self.client.get('/api/session',headers=other).status_code,200)

    def test_completed_local_download_is_idempotent_and_contains_no_credentials(self):
        body={'eventId':'a'*32,'url':'https://edu.example/lesson?id=123&token=secret#fragment',
              'title':'Урок','height':720}
        self.assertEqual(self.client.post('/api/local-history',json=body).status_code,403)
        first=self.client.post('/api/local-history',headers=self.headers,json=body)
        self.assertEqual(first.status_code,200)
        second=self.client.post('/api/local-history',headers=self.headers,json={**body,'title':'Changed'})
        self.assertEqual(first.json,second.json)
        row=self.s.store.job(first.json['id'])
        self.assertEqual(row['url'],'https://edu.example/lesson?id=123')
        self.assertEqual(row['title'],'Урок')
        self.assertEqual(row['state'],'done')
        self.assertIsNone(row['source'])
        result=self.client.get('/api/jobs/'+row['id'],headers=self.headers).json
        self.assertTrue(result['localDownload'])
        self.assertEqual(result['files'],[])
        audit=self.client.get('/api/audit',headers={'Authorization':'Bearer '+self.s.store.issue(1,'api',300)}).json
        self.assertEqual([item['id'] for item in audit['jobs']], [row['id']])
        self.assertNotIn('secret',json.dumps(audit))
        for bad in ({**body,'eventId':'bad'}, {**body,'url':'https://user:pass@edu.example/lesson'},
                    {**body,'height':True}, {**body,'title':'a\nb'}):
            self.assertEqual(self.client.post('/api/local-history',headers=self.headers,json=bad).status_code,400)

    def test_extension_history_endpoint_is_exposed_by_the_proxy_configuration(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'extension-server/background.js').read_text()
        proxy=(root/'server/Caddyfile').read_text()
        # Guard the actual integration: an API can work locally while Caddy blocks it externally.
        self.assertIn("journalBase+'/api/local-history'",source)
        client_block=proxy.split('@client {',1)[1].split('}',1)[0]
        paths=next(line.split()[1:] for line in client_block.splitlines() if line.strip().startswith('path '))
        self.assertIn('/api/local-history',paths)

    def test_youtube_not_connected_is_rejected_before_download(self):
        row=self.prepare()
        response=self.client.post('/api/jobs/'+row['id']+'/start',headers=self.headers,json={'format':'mp4','height':0,'mode':'link'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')

    def test_expired_preview_cannot_start_and_cancellation_erases_source(self):
        row=self.prepare();self.s.store.update(row['id'],expires=time.time()-1)
        self.assertEqual(self.client.post('/api/jobs/'+row['id']+'/start',headers=self.headers,json={'format':'mp4','height':0,'mode':'file'}).status_code,400)
        row=self.prepare()
        self.assertEqual(self.client.post('/api/jobs/'+row['id']+'/cancel',headers=self.headers,json={}).status_code,200)
        self.assertIsNone(self.s.store.job(row['id'])['source'])

    def test_server_ui_has_separate_frame_policy_and_no_secret_assets(self):
        response=self.client.get('/app/')
        self.assertEqual(response.status_code,200)
        self.assertIn('chrome-extension:',response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'],'no-store');response.close()
        self.assertEqual(self.client.get('/app/../secret.key').status_code,404)
        self.assertIn("frame-ancestors 'none'",self.client.get('/api/session').headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/api/session').status_code,403)

    def test_malformed_json_does_not_become_a_server_error(self):
        for path in ('/api/pair','/api/jobs'):
            self.assertEqual(self.client.post(path,headers=self.headers,json=[]).status_code,400)

    def test_quota_scan_tolerates_a_part_file_renamed_during_download(self):
        gone=Mock();gone.lstat.side_effect=FileNotFoundError()
        ready=Mock();ready.lstat.return_value=Mock(st_mode=0o100644,st_size=100)
        symlink=Mock();symlink.lstat.return_value=Mock(st_mode=0o120777,st_size=999)
        directory=Mock();directory.rglob.return_value=[gone,ready,symlink]
        self.assertEqual(self.s.disk_used(directory),100)

    def test_internal_diagnostics_do_not_expose_credentials(self):
        row=self.prepare();self.s.store.choose(row['id'],2,'file')
        self.s.media=Mock(side_effect=RuntimeError('https://secret.example/?token=PRIVATE'))
        self.s.run_job(self.s.store.job(row['id']))
        event=self.s.store.one("SELECT detail FROM events WHERE job_id=? AND kind='internal_error'",(row['id'],))
        self.assertIn('RuntimeError',event['detail']);self.assertNotIn('PRIVATE',event['detail'])


if __name__=='__main__':unittest.main()
