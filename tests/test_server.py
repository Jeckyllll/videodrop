import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.app import create_app
from server.media import clean_source, image_post, public_url, friendly_error
from server.media import run as run_media
from server.service import Service


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(self.tmp.name,1)
        self.s.tg=Mock(limit=45*1024*1024,token='test-only')
        self.s.store.user(2,'Иван','ivan')
        self.s.store.user(3,'Анна','anna')
        self.s.store.set_role(1,2,'employee')
        self.s.store.set_role(1,3,'employee')

    def tearDown(self):
        self.s.store.db.close()
        self.tmp.cleanup()

    def job(self, uid=2, **kwargs):
        return self.s.store.create(uid,{'url':'https://example.com/lesson?token=SECRET&id=55',
             'cookies':[{'name':'session','value':'PRIVATE'}]},**kwargs)

    def file(self,row,size=12):
        d=self.s.files/row['id']; d.mkdir()
        p=d/'video.mp4'; p.write_bytes(b'v'*size)
        self.s.store.update(row['id'],title='Урок',files='["video.mp4"]',size=size,
            metadata=json.dumps({'kind':'video','duration':10,'heights':[1080,720]}))
        return p

    def test_source_is_encrypted_and_audit_redacts_signed_url(self):
        row=self.job()
        self.assertNotIn(b'PRIVATE',row['source'])
        self.assertNotIn('SECRET',row['url'])
        self.assertIn('id=55',row['url'])
        self.assertEqual(self.s.store.source(row['id'])['cookies'][0]['value'],'PRIVATE')
        history=json.dumps(self.s.store.history(1))
        self.assertNotIn('PRIVATE',history)
        self.assertNotIn('SECRET',history)
        self.assertNotIn('source',self.s.store.history(1)[0])

    def test_employee_cannot_read_or_choose_other_employee_jobs(self):
        row=self.job()
        self.s.store.update(row['id'],state='choice')
        with self.assertRaises(PermissionError):
            self.s.store.choose(row['id'],3,'link')
        self.assertEqual(self.s.store.history(3),[])
        self.assertEqual(len(self.s.store.history(1)),1)

    def test_audit_filters_owner_jobs_before_applying_limit(self):
        employee = self.job()
        self.s.store.update(employee['id'], state='done')
        for _ in range(12):
            row = self.job(uid=1)
            self.s.store.update(row['id'], state='done')
        rows = self.s.store.history(1, limit=10, employees_only=True)
        self.assertEqual([r['id'] for r in rows], [employee['id']])

    def test_media_image_inspection_emits_kind_in_json_protocol(self):
        output = io.StringIO()
        with patch('server.media.guard_network'), patch('server.media.clean_source', side_effect=lambda x:x), patch('sys.stdout', output):
            run_media({'source':{'url':'https://www.instagram.com/p/example/'},'mode':'inspect'})
        message = json.loads(output.getvalue())
        self.assertEqual(message['event'], 'result')
        self.assertEqual(message['kind'], 'images')

    def test_employee_cannot_approve_users_or_export_all_history(self):
        with self.assertRaises(PermissionError):
            self.s.store.set_role(2,3,'employee')
        self.s.handle({'message':{'chat':{'type':'private'},'from':{'id':2},'text':'/export'}})
        self.s.tg.bytes_document.assert_not_called()

    def test_pair_code_consumed_once_and_revoked_user_loses_tokens(self):
        code=self.s.store.issue(2,'pair',600)
        self.assertEqual(self.s.store.resolve(code,'pair',consume=True)['user_id'],2)
        with self.assertRaises(PermissionError): self.s.store.resolve(code,'pair')
        token=self.s.store.issue(2,'api',600)
        self.s.store.set_role(1,2,'blocked')
        with self.assertRaises(PermissionError): self.s.store.resolve(token,'api')

    def test_group_messages_never_create_jobs(self):
        self.s.handle({'message':{'chat':{'type':'group'},'from':{'id':2},'text':'https://example.com'}})
        self.assertEqual(self.s.store.history(1),[])

    def test_selection_is_idempotent_and_preserves_files(self):
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='choice')
        self.s.store.choose(row['id'],2,'file')
        with self.assertRaises(ValueError): self.s.store.choose(row['id'],2,'link')
        self.assertTrue(p.exists())

    def test_confirmed_telegram_delivery_deletes_file_but_retains_audit(self):
        row=self.job(mode='file'); p=self.file(row)
        self.s.deliver(self.s.store.job(row['id']))
        self.assertFalse(p.exists())
        done=self.s.store.job(row['id'])
        self.assertEqual(done['state'],'done')
        self.assertIsNone(done['source'])
        self.assertEqual(len(self.s.store.history(1)),1)
        self.assertEqual(self.s.store.one("SELECT COUNT(*) n FROM events WHERE kind='telegram_file_delivered'")['n'],1)

    def test_failed_telegram_delivery_keeps_file_and_does_not_retry_automatically(self):
        row=self.job(mode='file'); p=self.file(row)
        self.s.tg.document.side_effect=ValueError('Ответ Telegram не получен.')
        self.s.run_job(self.s.store.job(row['id']))
        self.assertTrue(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')
        self.s.store.recover()
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')

    def test_large_file_is_not_compressed_or_silently_sent_elsewhere(self):
        self.s.tg.limit=5
        row=self.job(mode='file'); p=self.file(row)
        self.s.run_job(self.s.store.job(row['id']))
        self.assertTrue(p.exists())
        self.s.tg.document.assert_not_called()
        self.assertIn('лимита',self.s.store.job(row['id'])['error'])

    def test_owner_youtube_file_deleted_only_after_verified_upload_and_link_delivery(self):
        row=self.job(uid=1,mode='link'); p=self.file(row)
        self.s.youtube=Mock()
        self.s.youtube.upload.return_value='test-video'
        self.s.deliver(self.s.store.job(row['id']))
        self.assertFalse(p.exists())
        self.s.youtube.upload.assert_called_once()

    def test_unverified_youtube_visibility_preserves_file(self):
        row=self.job(uid=1,mode='link'); p=self.file(row)
        self.s.youtube=Mock()
        self.s.youtube.upload.side_effect=ValueError('YouTube не подтвердил доступ «По ссылке».')
        self.s.run_job(self.s.store.job(row['id']))
        self.assertTrue(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')

    def test_employees_do_not_upload_to_owner_channel_by_default(self):
        row=self.job(mode='link'); p=self.file(row)
        self.s.youtube=Mock()
        self.s.run_job(self.s.store.job(row['id']))
        self.s.youtube.upload.assert_not_called()
        self.assertTrue(p.exists())

    def test_expiry_removes_files_and_tokens_not_history(self):
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='done',expires=time.time()-1)
        token=self.s.store.issue(2,'file',3600,row['id'])
        self.s.cleanup()
        self.assertFalse(p.exists())
        self.assertEqual(self.s.store.history(1)[0]['state'],'expired')
        with self.assertRaises(PermissionError): self.s.store.resolve(token,'file')

    def test_restart_does_not_repeat_ambiguous_upload(self):
        row=self.job()
        self.s.store.update(row['id'],state='uploading')
        self.s.store.recover()
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')

    def test_telegram_only_mode_does_not_expose_api_or_files(self):
        client=create_app(self.s).test_client()
        self.assertEqual(client.get('/health').status_code,200)
        self.assertEqual(client.post('/api/pair',json={'code':'anything'}).status_code,404)
        self.assertEqual(client.get('/api/audit').status_code,404)

    def test_api_requires_auth_and_owner_for_audit(self):
        self.s.base='https://example.com'
        client=create_app(self.s).test_client()
        self.assertEqual(client.get('/api/jobs').status_code,403)
        token=self.s.store.issue(2,'api',600)
        self.assertEqual(client.get('/api/jobs',headers={'Authorization':'Bearer '+token}).status_code,200)
        self.assertEqual(client.get('/api/audit',headers={'Authorization':'Bearer '+token}).status_code,403)

    def test_download_records_request_not_claimed_view(self):
        row=self.job(mode='file'); p=self.file(row)
        self.s.store.update(row['id'],state='done',expires=time.time()+3600)
        token=self.s.store.issue(2,'file',3600,row['id'])
        self.s.base='https://example.com'
        client=create_app(self.s).test_client()
        response=client.get('/download/'+token+'/0')
        self.assertEqual(response.data,p.read_bytes()); response.close()
        self.assertEqual(self.s.store.history(1)[0]['file_requests'],1)

    def test_rejects_private_network_urls_and_out_of_scope_cookies(self):
        for url in ('http://127.0.0.1/a','http://[::1]/a','http://169.254.169.254/','file:///etc/passwd','https://example.com:8765/'):
            with self.assertRaises(ValueError): public_url(url,resolve=False)
        source=clean_source({'url':'https://example.com/a','cookies':[
            {'domain':'.example.com','name':'yes','value':'1'},
            {'domain':'.google.com','name':'no','value':'2'}]},resolve=False)
        self.assertEqual(len(source['cookies']),1)

    def test_images_post_not_reel(self):
        self.assertTrue(image_post('https://www.instagram.com/p/example/'))
        self.assertTrue(image_post('https://www.tiktok.com/@name/photo/123'))
        self.assertFalse(image_post('https://www.instagram.com/reel/example/'))

    def test_server_login_error_does_not_direct_user_to_disabled_extension(self):
        message = friendly_error('Sign in to confirm you are not a bot')
        self.assertIn('IP сервера', message)
        self.assertNotIn('расширение', message)

    def test_quality_menu_keeps_all_available_resolutions(self):
        row = self.job()
        self.s.store.update(row['id'], state='choice', metadata=json.dumps({'heights':[2160,1440,1080,720,480,360]}))
        self.s.handle_callback(2, 'quality:'+row['id'])
        buttons = self.s.tg.say.call_args.args[2]
        labels = [b['text'] for line in buttons for b in line]
        for height in (2160,1440,1080,720,480,360):
            self.assertIn(str(height)+'p', labels)


if __name__=='__main__': unittest.main()
