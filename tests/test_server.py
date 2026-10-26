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
from server.media import validate_download
from server.service import Service
from server.store import Store


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

    def test_download_validation_rejects_audio_only_and_cover_art_as_video(self):
        for streams in ([{'codec_type':'audio','codec_name':'aac'}],
                [{'codec_type':'video','codec_name':'mjpeg','disposition':{'attached_pic':1}},
                 {'codec_type':'audio','codec_name':'aac'}]):
            with self.subTest(streams=streams), patch('server.media.subprocess.run',return_value=Mock(stdout=json.dumps({'streams':streams}))):
                with self.assertRaisesRegex(ValueError,'нет видеодорожки'):
                    validate_download(Path('video.mp4'),{'format':'mp4'})

    def test_real_codecs_are_reported_and_vp9_is_warned_before_and_after_selection(self):
        for codec,warning in (('vp9',True),('h264',False)):
            with self.subTest(codec=codec):
                probe={'streams':[{'codec_type':'video','codec_name':codec,'width':1080,'height':1920},
                                  {'codec_type':'audio','codec_name':'aac'}]}
                with patch('server.media.subprocess.run',return_value=Mock(stdout=json.dumps(probe))):
                    media=validate_download(Path('video.mp4'),{'format':'mp4'})['media']
                self.assertEqual(media['quicktimeWarning'],warning)
                row=self.job(); self.file(row)
                self.s.store.update(row['id'],metadata=json.dumps({'kind':'video','downloadedMedia':media}))
                self.s.ask_choice(self.s.store.job(row['id']))
                choice=self.s.tg.say.call_args.args[1]
                self.assertIn('1080×1920',choice)
                self.assertEqual('QuickTime' in choice,warning)
                self.s.store.choose(row['id'],2,'file')
                self.s.deliver(self.s.store.job(row['id']))
                caption=self.s.tg.document.call_args.args[2]
                self.assertEqual('QuickTime' in caption,warning)

    def test_downloaded_media_metadata_survives_delivery_for_the_audit(self):
        row=self.job()
        def media(current,mode):
            if mode=='inspect':
                return {'kind':'video','title':'Test','heights':[1920]}
            p=self.file(current)
            return {'path':str(p),'media':{'videoCodec':'vp9','audioCodec':'aac','width':1080,'height':1920,'quicktimeWarning':True}}
        self.s.media=Mock(side_effect=media)
        self.s.run_job(row)
        self.s.store.choose(row['id'],2,'file')
        self.s.run_job(self.s.store.job(row['id']))
        done=self.s.store.job(row['id'])
        self.assertEqual(json.loads(done['metadata'])['downloadedMedia']['videoCodec'],'vp9')
        self.assertEqual(done['files'],'[]')
        self.assertEqual(self.s.store.one("SELECT COUNT(*) n FROM events WHERE job_id=? AND kind='media_verified'",(row['id'],))['n'],1)

    def test_quality_estimate_uses_probed_duration_when_source_omits_it(self):
        row=self.job()
        def media(current,mode):
            if mode=='inspect':
                return {'kind':'video','heights':[1920,1280], 'qualitySizes':{
                    '1280':{'bytes':None,'bytesPerSecond':100000,'approximate':True},
                    '1920':{'bytes':20000000,'approximate':True}}}
            p=self.file(current)
            return {'path':str(p),'media':{'duration':52,'videoCodec':'vp9'}}
        self.s.media=Mock(side_effect=media)
        self.s.run_job(row)
        sizes=json.loads(self.s.store.job(row['id'])['metadata'])['qualitySizes']
        self.assertEqual(sizes['1280']['bytes'],5200000)
        self.assertEqual(sizes['1920']['bytes'],20000000)

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

    def test_employee_link_means_youtube_even_when_server_file_links_exist(self):
        row=self.job(mode='link'); p=self.file(row)
        self.s.base='https://example.com'
        self.s.youtube=Mock()
        self.s.youtube.upload.return_value='employee-video'
        self.s.run_job(self.s.store.job(row['id']))
        self.s.youtube.upload.assert_called_once()
        self.assertFalse(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.assertIn('https://youtu.be/employee-video',str(self.s.tg.say.call_args_list))
        self.assertNotIn('/download/',str(self.s.tg.say.call_args_list))

    def test_telegram_downloads_video_before_asking_how_to_deliver(self):
        for duration in (12,3600,None):
            with self.subTest(duration=duration):
                row=self.job()
                def media(current, mode):
                    if mode=='inspect':
                        return {'kind':'video','title':'Test','duration':duration,'heights':[1080]}
                    d=self.s.files/current['id']; d.mkdir()
                    p=d/'clip.mp4'; p.write_bytes(b'test clip')
                    return {'paths':[str(p)]}
                self.s.media=Mock(side_effect=media)
                self.s.run_job(row)
                current=self.s.store.job(row['id'])
                self.assertEqual(current['state'],'choice')
                self.assertEqual(current['size'],9)
                self.assertEqual([c.args[1] for c in self.s.media.call_args_list],['inspect','download'])
                self.s.tg.document.assert_not_called()
                self.s.handle_callback(2,'file:'+row['id'])
                self.s.run_job(self.s.store.job(row['id']))
                self.assertEqual(self.s.media.call_count,2)
                self.assertEqual(self.s.store.job(row['id'])['state'],'done')
                self.s.tg.reset_mock()

    def test_large_telegram_file_and_small_extension_file_get_download_links_for_24_hours(self):
        self.s.base='https://example.com'
        for entrypoint,size in (('telegram',12),('extension',2)):
            with self.subTest(entrypoint=entrypoint):
                self.s.tg.limit=5
                row=self.job(mode='file',entrypoint=entrypoint); p=self.file(row,size)
                before=time.time()
                self.s.deliver(self.s.store.job(row['id']))
                current=self.s.store.job(row['id'])
                self.assertEqual(current['state'],'done')
                self.assertTrue(p.exists())
                self.assertGreaterEqual(current['expires'],before+86400)
                self.assertLess(current['expires'],time.time()+86401)
                self.assertIn('/download/',str(self.s.tg.say.call_args))
                self.s.tg.document.assert_not_called()
                self.s.tg.reset_mock()

    def test_unconnected_youtube_never_falls_back_to_server_download_link(self):
        self.s.base='https://example.com'
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='choice')
        with self.assertRaisesRegex(ValueError,'YouTube'):
            self.s.handle_callback(2,'link:'+row['id'])
        self.assertTrue(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.assertNotIn('/download/',str(self.s.tg.say.call_args_list))

    def test_database_upgrade_preserves_existing_jobs_as_telegram_jobs(self):
        row=self.job()
        self.s.store.db.execute('ALTER TABLE jobs DROP COLUMN entrypoint')
        self.s.store.db.commit()
        self.s.store.db.close()
        self.s.store=Store(self.s.state/'jobs.sqlite3',(self.s.state/'secret.key').read_bytes(),1)
        self.assertEqual(self.s.store.job(row['id'])['entrypoint'],'telegram')
        self.assertEqual(self.s.store.source(row['id'])['cookies'][0]['value'],'PRIVATE')

    def test_extension_requires_preset_mode_and_result_never_extends_file_expiry(self):
        self.s.base='https://example.com'
        client=create_app(self.s).test_client()
        token=self.s.store.issue(2,'api',600)
        headers={'Authorization':'Bearer '+token}
        source={'url':'https://example.com/clip'}
        with patch('server.app.clean_source',return_value=source):
            self.assertEqual(client.post('/api/jobs',headers=headers,json=source).status_code,400)
            response=client.post('/api/jobs',headers=headers,json={**source,'mode':'file','entrypoint':'telegram'})
        self.assertEqual(response.status_code,202)
        jid=response.json['id'];row=self.s.store.job(jid)
        self.assertEqual(row['entrypoint'],'extension')
        p=self.file(row)
        self.s.deliver(self.s.store.job(jid))
        expiry=self.s.store.job(jid)['expires']
        result=client.get('/api/jobs/'+jid,headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['expires'],expiry)
        self.assertEqual(result.json['files'][0]['name'],p.name)
        self.assertIn('/download/',result.json['files'][0]['url'])
        foreign=self.s.store.issue(3,'api',600)
        self.assertEqual(client.get('/api/jobs/'+jid,headers={'Authorization':'Bearer '+foreign}).status_code,403)
        self.s.store.update(jid,expires=time.time()-1)
        self.assertEqual(client.get('/api/jobs/'+jid,headers=headers).json['files'],[])

    def test_expiry_removes_files_and_tokens_not_history(self):
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='done',expires=time.time()-1)
        token=self.s.store.issue(2,'file',3600,row['id'])
        self.s.cleanup()
        self.assertFalse(p.exists())
        self.assertEqual(self.s.store.history(1)[0]['state'],'expired')
        with self.assertRaises(PermissionError): self.s.store.resolve(token,'file')

    def test_expired_choice_cannot_reactivate_a_file_before_cleanup(self):
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='choice',expires=time.time()-1)
        with self.assertRaisesRegex(ValueError,'истёк'):
            self.s.store.choose(row['id'],2,'file')
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.s.cleanup()
        self.assertFalse(p.exists())

    def test_quality_change_preserves_source_and_redownloads_only_after_selection(self):
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='choice',expires=time.time()+86400)
        self.s.media=Mock()
        self.s.handle_callback(2,'height:'+row['id']+':720')
        self.assertFalse(p.exists())
        self.assertEqual(self.s.store.source(row['id'])['cookies'][0]['value'],'PRIVATE')
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.s.media.assert_not_called()
        def download(current,mode):
            self.assertEqual(mode,'download')
            self.assertEqual(current['height'],720)
            self.file(current)
            return {'paths':[str(p)]}
        self.s.media.side_effect=download
        self.s.handle_callback(2,'file:'+row['id'])
        self.s.run_job(self.s.store.job(row['id']))
        self.s.media.assert_called_once()
        self.s.tg.document.assert_called_once()
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')

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
        self.s.store.update(row['id'], state='choice', metadata=json.dumps({'heights':[4320,2160,1920,1440,1080,720,480,360,240]}))
        self.s.handle_callback(2, 'quality:'+row['id'])
        buttons = self.s.tg.say.call_args.args[2]
        labels = [b['text'] for line in buttons for b in line]
        for height in (4320,2160,1920,1440,1080,720,480,360,240):
            self.assertIn(f'до {height}p · размер неизвестен', labels)

    def test_quality_menu_shows_actual_current_size_and_estimates_for_alternatives(self):
        row=self.job(); self.file(row)
        self.s.store.update(row['id'],state='choice',size=5*1024*1024,metadata=json.dumps({
            'heights':[1920,1280,640], 'qualitySizes':{'0':{'bytes':6*1024*1024},
            '1920':{'bytes':6*1024*1024},'1280':{'bytes':3*1024*1024},'640':{'bytes':None}}}))
        self.s.handle_callback(2,'quality:'+row['id'])
        labels=[b['text'] for line in self.s.tg.say.call_args.args[2] for b in line]
        self.assertIn('Лучшее · 5.0 МБ',labels)
        self.assertIn('до 1280p · ≈ 3.0 МБ',labels)
        self.assertIn('до 640p · размер неизвестен',labels)
        self.s.handle_callback(2,'height:'+row['id']+':1280')
        self.s.handle_callback(2,'quality:'+row['id'])
        labels=[b['text'] for line in self.s.tg.say.call_args.args[2] for b in line]
        self.assertIn('до 1280p · ≈ 3.0 МБ',labels)
        self.assertNotIn('до 1280p · 5.0 МБ',labels)

    def test_audio_format_does_not_show_video_size_estimates(self):
        row=self.job(); self.file(row)
        self.s.store.update(row['id'],state='choice',format='mp3',size=0,files='[]')
        self.s.handle_callback(2,'quality:'+row['id'])
        self.assertIn('после скачивания',self.s.tg.say.call_args.args[1])
        callbacks=[b['callback_data'] for line in self.s.tg.say.call_args.args[2] for b in line]
        self.assertFalse(any(c.startswith('height:') for c in callbacks))


if __name__=='__main__': unittest.main()
