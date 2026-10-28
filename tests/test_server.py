import io
import json
import tempfile
import time
import unittest
import subprocess
import os
import shutil
import hashlib
from pathlib import Path
from unittest.mock import Mock, patch

from server.app import create_app
from server.media import clean_source, image_post, public_url, friendly_error
from server.media import run as run_media
from server.media import validate_download
from server.service import Service, Telegram
from server.store import Store


class CompatibleVideoTests(unittest.TestCase):
    def test_unknown_audio_codec_is_not_reported_as_present(self):
        from worker import metadata
        no_audio={'formats':[{'vcodec':'h264','acodec':'none','height':720},
                             {'vcodec':'h264','height':360}]}
        self.assertFalse(metadata(no_audio)['hasAudio'])
        self.assertTrue(metadata({'formats':no_audio['formats']+[{'vcodec':'none','acodec':'aac'}]})['hasAudio'])

    def test_telegram_preview_uses_streamable_video_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'preview.mp4'; path.write_bytes(b'video')
            response=Mock(); response.json.return_value={'ok':True,'result':{'message_id':123}}
            with patch('server.service.requests.post',return_value=response) as post:
                result=Telegram('test-token').video(42,path,'Для просмотра')
            self.assertTrue(post.call_args.args[0].endswith('/sendVideo'))
            self.assertEqual(post.call_args.kwargs['data']['supports_streaming'],'true')
            self.assertEqual(result['message_id'],123)

    def test_explicit_conversion_retains_dimensions_and_produces_h264_aac(self):
        import imageio_ffmpeg
        from worker import convert_compatible
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'input.mp4'
            subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=64x96:rate=5',
                '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libvpx-vp9',
                '-c:a','aac','-y',str(source)],check=True)
            output=convert_compatible(source)
            probe=subprocess.run([ffmpeg,'-hide_banner','-i',str(output),'-f','null','-'],capture_output=True,text=True)
            self.assertEqual(probe.returncode,0)
            self.assertIn('Video: h264',probe.stderr)
            self.assertIn('Audio: aac',probe.stderr)
            self.assertIn('64x96',probe.stderr)
            self.assertFalse(source.exists())


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(self.tmp.name,1)
        self.s.tg=Mock(limit=45*1024*1024,token='test-only')
        self.next_message_id=100
        def sent(*args,**kwargs):
            self.next_message_id+=1
            return {'message_id':self.next_message_id}
        self.s.tg.say.side_effect=sent
        self.s.tg.edit.side_effect=lambda uid,mid,*args: {'message_id':mid}
        self.s.store.user(2,'Иван','ivan')
        self.s.store.user(3,'Анна','anna')
        self.s.store.set_role(1,2,'employee')
        self.s.store.set_role(1,3,'employee')

    def tearDown(self):
        self.s.store.db.close()
        self.tmp.cleanup()

    def last_card(self):
        for call in reversed(self.s.tg.mock_calls):
            if call[0] in ('say','edit'):
                start=1 if call[0]=='say' else 2
                return call.args[start],call.args[start+1] if len(call.args)>start+1 else []
        raise AssertionError('No Telegram card')

    def callback(self,row,action,uid=2,message_id=None):
        self.s.handle({'callback_query':{'id':'callback-test','from':{'id':uid,'first_name':'User'},
            'message':{'message_id':message_id or row['telegram_message_id'],'chat':{'id':uid,'type':'private'}},
            'data':action+':'+row['id']}})

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
        with patch('server.media.guard_network'), patch('server.media.clean_source', side_effect=lambda x:x), patch('sys.stdout', output), \
                patch('server.images.instagram',return_value={'title':'Изображения поста','kind':'images','heights':[],'imageCount':5}):
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

    def test_real_codecs_are_reported_without_extra_video_caption(self):
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
                choice=self.last_card()[0]
                self.assertIn('1080×1920',choice)
                self.assertNotIn('QuickTime',choice)
                self.s.store.choose(row['id'],2,'file')
                with patch.object(self.s,'compatible_copy',side_effect=lambda r,p:(r,p)):
                    self.s.deliver(self.s.store.job(row['id']))
                caption=self.s.tg.document.call_args.args[2]
                self.assertEqual(caption,'')

    def test_downloaded_media_metadata_survives_delivery_for_the_audit(self):
        row=self.job()
        def media(current,mode):
            if mode=='inspect':
                return {'kind':'video','title':'Test','heights':[1920]}
            p=self.file(current)
            return {'path':str(p),'media':{'videoCodec':'vp9','audioCodec':'aac','width':1080,'height':1920,'quicktimeWarning':True}}
        self.s.media=Mock(side_effect=media)
        with patch.object(self.s,'compatible_copy',side_effect=lambda r,p:(r,p)):
            self.s.run_job(row)
        done=self.s.store.job(row['id'])
        self.assertEqual(json.loads(done['metadata'])['downloadedMedia']['videoCodec'],'vp9')
        self.assertEqual(done['files'],'[]')
        self.assertEqual(self.s.store.one("SELECT COUNT(*) n FROM events WHERE job_id=? AND kind='media_verified'",(row['id'],))['n'],1)

    def test_source_with_audio_is_not_delivered_if_sound_is_missing(self):
        row=self.job()
        def media(current,mode):
            if mode=='inspect':
                return {'kind':'video','title':'Test','heights':[720],'hasAudio':True}
            directory=self.s.files/current['id'];directory.mkdir()
            path=directory/'video.mp4';path.write_bytes(b'video without audio')
            return {'path':str(path),'media':{'videoCodec':'h264','audioCodec':None}}
        self.s.media=Mock(side_effect=media)
        self.s.run_job(row)
        failed=self.s.store.job(row['id'])
        self.assertEqual(failed['state'],'error')
        self.assertIn('звук',failed['error'])
        self.s.tg.video.assert_not_called()
        self.s.tg.document.assert_not_called()
        self.assertFalse((self.s.files/row['id']).exists())

    def test_intentionally_silent_video_remains_downloadable(self):
        row=self.job()
        def media(current,mode):
            if mode=='inspect':
                return {'kind':'video','title':'Silent clip','heights':[720],'hasAudio':False}
            directory=self.s.files/current['id'];directory.mkdir()
            path=directory/'video.mp4';path.write_bytes(b'silent video')
            return {'path':str(path),'media':{'videoCodec':'h264','pixelFormat':'yuv420p','audioCodec':None}}
        self.s.media=Mock(side_effect=media)
        with patch.object(self.s,'preview'):
            self.s.run_job(row)
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.s.tg.document.assert_called_once()

    def test_video_preview_is_playable_and_original_is_unchanged(self):
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        row = self.job()
        source = self.file(row)
        subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=64x96:rate=5',
            '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libvpx-vp9',
            '-c:a','aac','-y',str(source)],check=True)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.s.store.update(row['id'],mode='file',size=source.stat().st_size,
            metadata=json.dumps({'kind':'video','downloadedMedia':{'videoCodec':'vp9','audioCodec':'aac','duration':1}}))
        preview_copy = Path(self.tmp.name)/'received.mp4'
        def capture(uid,path,caption=''):
            self.assertEqual(caption,'')
            shutil.copyfile(path,preview_copy)
        self.s.tg.video.side_effect = capture
        def original(uid,path,caption):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
        self.s.tg.document.side_effect = original
        with tempfile.TemporaryDirectory() as bindir:
            os.symlink(ffmpeg,Path(bindir)/'ffmpeg')
            with patch.dict(os.environ,{'PATH':bindir+os.pathsep+os.environ.get('PATH','')}), patch.object(self.s,'compatible_copy',side_effect=lambda r,p:(r,p)):
                self.s.deliver(self.s.store.job(row['id']))
        self.s.tg.video.assert_called_once()
        self.s.tg.document.assert_called_once()
        probe = subprocess.run([ffmpeg,'-hide_banner','-i',str(preview_copy),'-f','null','-'],capture_output=True,text=True)
        self.assertEqual(probe.returncode,0)
        self.assertIn('Video: h264',probe.stderr)
        self.assertIn('Audio: aac',probe.stderr)
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')

    def test_preview_failure_does_not_prevent_original_delivery(self):
        row=self.job(); self.file(row)
        self.s.store.update(row['id'],mode='file',metadata=json.dumps(
            {'kind':'video','downloadedMedia':{'videoCodec':'vp9','audioCodec':'aac','duration':1}}))
        with patch('server.service.subprocess.Popen',side_effect=OSError('unavailable')), patch.object(self.s,'compatible_copy',side_effect=lambda r,p:(r,p)):
            self.s.deliver(self.s.store.job(row['id']))
        self.s.tg.video.assert_not_called()
        self.s.tg.document.assert_called_once()
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')

    def test_incompatible_original_gets_full_resolution_compatible_copy(self):
        import imageio_ffmpeg
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        row=self.job(); source=self.file(row)
        subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=64x96:rate=5',
            '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libvpx-vp9',
            '-c:a','aac','-y',str(source)],check=True)
        original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        self.s.store.update(row['id'],mode='file',size=source.stat().st_size,metadata=json.dumps({'kind':'video',
            'downloadedMedia':{'videoCodec':'vp9','audioCodec':'aac','width':64,'height':96,'duration':1}}))
        def verify(path,request):
            decoded=subprocess.run([ffmpeg,'-hide_banner','-i',str(path),'-f','null','-'],capture_output=True,text=True)
            self.assertEqual(decoded.returncode,0)
            self.assertIn('Video: h264',decoded.stderr)
            self.assertIn('Audio: aac',decoded.stderr)
            self.assertIn('64x96',decoded.stderr)
            return {'media':{'videoCodec':'h264','pixelFormat':'yuv420p','audioCodec':'aac','width':64,'height':96}}
        with tempfile.TemporaryDirectory() as bindir:
            os.symlink(ffmpeg,Path(bindir)/'ffmpeg')
            with patch.dict(os.environ,{'PATH':bindir+os.pathsep+os.environ.get('PATH','')}), patch('server.service.validate_download',side_effect=verify):
                updated,paths=self.s.compatible_copy(self.s.store.job(row['id']),[source])
        self.assertEqual(len(paths),1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original_hash)
        self.assertEqual(paths[0].name,'video - MP4.mp4')
        self.assertEqual(json.loads(updated['metadata'])['compatibleCopy'],paths[0].name)
        self.assertEqual(self.s.paths(updated),paths)

    def test_compatible_copy_failure_preserves_original(self):
        row=self.job(mode='file'); source=self.file(row)
        self.s.store.update(row['id'],metadata=json.dumps({'kind':'video','downloadedMedia':{'videoCodec':'vp9'}}))
        with patch('server.service.subprocess.Popen',side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ValueError,'совместимый'):
                self.s.compatible_copy(self.s.store.job(row['id']),[source])
        self.assertEqual(self.s.paths(self.s.store.job(row['id'])),[source])
        self.assertTrue(source.exists())

    def test_video_delivery_sends_preview_and_only_compatible_file(self):
        import imageio_ffmpeg
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        row=self.job(mode='file'); source=self.file(row)
        subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc=size=64x96:rate=5',
            '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libvpx-vp9',
            '-c:a','aac','-y',str(source)],check=True)
        self.s.store.update(row['id'],size=source.stat().st_size,metadata=json.dumps({'kind':'video',
            'downloadedMedia':{'videoCodec':'vp9','audioCodec':'aac','width':64,'height':96,'duration':1}}))
        captured=[]
        def document(uid,path,caption):
            self.assertEqual(caption,'')
            self.assertEqual(path.name,'video - MP4.mp4')
            decoded=subprocess.run([ffmpeg,'-hide_banner','-i',str(path),'-f','null','-'],capture_output=True,text=True)
            self.assertEqual(decoded.returncode,0)
            self.assertIn('Video: h264',decoded.stderr)
            self.assertIn('Audio: aac',decoded.stderr)
            captured.append(path.name)
        self.s.tg.document.side_effect=document
        with tempfile.TemporaryDirectory() as bindir:
            os.symlink(ffmpeg,Path(bindir)/'ffmpeg')
            with patch.dict(os.environ,{'PATH':bindir+os.pathsep+os.environ.get('PATH','')}), patch('server.service.validate_download',return_value={'media':{'videoCodec':'h264','pixelFormat':'yuv420p','audioCodec':'aac','width':64,'height':96}}):
                self.s.deliver(self.s.store.job(row['id']))
        self.s.tg.video.assert_called_once()
        self.assertEqual(len(self.s.tg.video.call_args.args),2)
        self.s.tg.document.assert_called_once()
        self.assertEqual(captured,['video - MP4.mp4'])

    def test_large_compatible_file_has_one_link(self):
        self.s.base='https://files.example.com'
        row=self.job(mode='file'); original=self.file(row)
        compatible=original.parent/'VideoDrop-compatible.mp4'; compatible.write_bytes(b'compat')
        self.s.store.update(row['id'],metadata=json.dumps({'kind':'video','compatibleCopy':compatible.name}),
            files=json.dumps([compatible.name]))
        self.s.tg.limit=5
        with patch.object(self.s,'preview'), patch.object(self.s,'compatible_copy',side_effect=lambda r,p:(r,p)):
            self.s.deliver(self.s.store.job(row['id']))
        done=self.s.store.job(row['id'])
        self.assertEqual(done['state'],'done')
        self.assertEqual(self.s.paths(done),[compatible])
        text,buttons=self.s.job_card(done,'result')
        self.assertEqual([b[0]['text'] for b in buttons],['Скачать видео'])
        client=create_app(self.s).test_client()
        for i,expected in enumerate((compatible.read_bytes(),)):
            with client.get(buttons[i][0]['url'].removeprefix(self.s.base)) as response:
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.data,expected)

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

    def test_hls_size_includes_probed_audio_bitrate(self):
        for bitrate,expected in ((128000,1410000),(None,None)):
            row=self.job()
            def media(current,mode):
                if mode=='inspect':
                    return {'kind':'video','qualitySizes':{'720':{
                        'bytes':None,'bytesPerSecond':125000,'needsAudioBitrate':True}}}
                return {'path':str(self.file(current)),'media':{'duration':10,'audioBitrate':bitrate}}
            self.s.media=Mock(side_effect=media)
            self.s.run_job(row)
            size=json.loads(self.s.store.job(row['id'])['metadata'])['qualitySizes']['720']['bytes']
            self.assertEqual(size,expected)

    def test_tiktok_video_url_with_only_music_is_classified_as_photo_post(self):
        output=io.StringIO()
        with patch('server.media.guard_network'), patch('server.media.clean_source',side_effect=lambda x:x), \
                patch('yt_dlp.YoutubeDL'), patch('worker.extract_info',return_value={}), \
                patch('worker.metadata',return_value={'hasVideo':False,'hasAudio':True}), patch('sys.stdout',output):
            run_media({'mode':'inspect','source':{'url':'https://www.tiktok.com/@user/video/123','cookies':[]}})
        result=json.loads(output.getvalue())
        self.assertEqual(result['kind'],'images')
        self.assertEqual(result['title'],'Изображения поста')

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

    def test_public_file_mode_keeps_extension_and_oauth_closed(self):
        self.s.base='https://files.example.com'
        self.s.downloads_only=True
        client=create_app(self.s).test_client()
        for path in ('/api/jobs','/api/pair','/oauth/callback','/api/audit'):
            self.assertEqual(client.get(path).status_code,404)
        self.assertEqual(client.get('/health').json['mode'],'telegram-with-files')
        row=self.job();p=self.file(row)
        self.s.store.update(row['id'],mode='file',state='done',expires=time.time()+86400)
        link=self.s.file_links(self.s.store.job(row['id']))[0]['url']
        with client.get(link.removeprefix(self.s.base)) as response:
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.data,p.read_bytes())

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

    def test_large_video_skips_telegram_preview_before_file_link(self):
        self.s.tg.limit=5
        self.s.base='https://files.example.com'
        row=self.job(mode='file'); self.file(row)
        self.s.store.update(row['id'],metadata=json.dumps({'kind':'video','downloadedMedia':{
            'videoCodec':'h264','pixelFormat':'yuv420p','audioCodec':'aac'}}))
        with patch('server.service.subprocess.Popen') as ffmpeg:
            self.s.run_job(self.s.store.job(row['id']))
        ffmpeg.assert_not_called()
        self.s.tg.video.assert_not_called()
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.assertTrue(self.s.store.job(row['id'])['expires'])

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

    def test_failed_youtube_upload_can_deliver_saved_file_without_reupload(self):
        row=self.job(uid=1,mode='link'); p=self.file(row)
        self.s.youtube=Mock()
        self.s.youtube.upload.side_effect=ValueError('YouTube не подтвердил результат.')
        self.s.run_job(self.s.store.job(row['id']))
        text,buttons=self.last_card()
        self.assertIn('Проверьте канал',text)
        self.assertEqual(buttons[0][0]['callback_data'],'savedfile:'+row['id'])
        self.s.handle_callback(1,'savedfile:'+row['id'])
        self.s.handle_callback(1,'savedfile:'+row['id'])
        self.assertEqual(self.s.store.job(row['id'])['state'],'queued')
        self.s.run_job(self.s.store.job(row['id']))
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.s.youtube.upload.assert_called_once()
        self.s.tg.document.assert_called_once()
        self.assertFalse(p.exists())

    def test_saved_youtube_file_rejects_other_users_and_expired_job(self):
        row=self.job(uid=2,mode='link'); p=self.file(row)
        self.s.store.update(row['id'],state='review',expires=time.time()+60)
        with self.assertRaises(PermissionError): self.s.handle_callback(3,'savedfile:'+row['id'])
        self.s.store.update(row['id'],expires=time.time()-1)
        self.assertIn('истёк',self.s.handle_callback(2,'savedfile:'+row['id']))
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')
        self.assertTrue(p.exists())

    def test_employee_link_means_youtube_even_when_server_file_links_exist(self):
        row=self.job(mode='link'); p=self.file(row)
        self.s.base='https://example.com'
        self.s.youtube=Mock()
        self.s.youtube.upload.return_value='employee-video'
        self.s.run_job(self.s.store.job(row['id']))
        self.s.youtube.upload.assert_called_once()
        self.assertFalse(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.assertIn('https://youtu.be/employee-video',str(self.s.tg.mock_calls))
        self.assertNotIn('/download/',str(self.s.tg.mock_calls))

    def test_small_telegram_video_is_delivered_and_large_video_waits_for_choice(self):
        for limit,expected in ((9,'done'),(8,'choice')):
            with self.subTest(limit=limit):
                self.s.tg.limit=limit
                row=self.job()
                def media(current, mode):
                    if mode=='inspect':
                        return {'kind':'video','title':'Test','duration':12,'heights':[1080]}
                    d=self.s.files/current['id']; d.mkdir()
                    p=d/'clip.mp4'; p.write_bytes(b'test clip')
                    return {'paths':[str(p)]}
                self.s.media=Mock(side_effect=media)
                self.s.run_job(row)
                current=self.s.store.job(row['id'])
                self.assertEqual(current['state'],expected)
                self.assertEqual(current['size'],9)
                self.assertEqual([c.args[1] for c in self.s.media.call_args_list],['inspect','download'])
                if expected=='done':
                    self.s.tg.document.assert_called_once()
                    self.assertEqual(current['files'],'[]')
                else:
                    self.s.tg.document.assert_not_called()
                    self.assertTrue(json.loads(current['files']))
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
                if entrypoint == 'telegram':
                    self.assertIn('/download/',str(self.s.tg.mock_calls))
                else:
                    self.s.tg.say.assert_not_called()
                    self.assertIn('/download/',self.s.file_links(current)[0]['url'])
                self.s.tg.document.assert_not_called()
                self.s.tg.reset_mock()

    def test_small_telegram_video_can_wait_for_quality_on_request(self):
        row=self.job()
        self.assertIn('выбор качества',self.s.handle_callback(2,'quality:'+row['id']))
        def media(current, mode):
            if mode == 'inspect':
                return {'kind':'video','title':'Test','heights':[1080,720]}
            folder=self.s.files/current['id'];folder.mkdir()
            path=folder/'clip.mp4';path.write_bytes(b'test')
            return {'path':str(path)}
        self.s.media=Mock(side_effect=media)
        self.s.run_job(row)
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.s.tg.document.assert_not_called()
        self.assertIn('Качество',str(self.last_card()))

    def test_unconnected_youtube_never_falls_back_to_server_download_link(self):
        self.s.base='https://example.com'
        row=self.job(); p=self.file(row)
        self.s.store.update(row['id'],state='choice')
        self.assertIn('YouTube',self.s.handle_callback(2,'link:'+row['id']))
        self.assertTrue(p.exists())
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.assertNotIn('/download/',str(self.s.tg.mock_calls))

    def test_database_upgrade_preserves_existing_jobs_as_telegram_jobs(self):
        row=self.job()
        self.s.store.db.execute('ALTER TABLE jobs DROP COLUMN entrypoint')
        self.s.store.db.execute('ALTER TABLE jobs DROP COLUMN telegram_message_id')
        self.s.store.db.commit()
        self.s.store.db.close()
        self.s.store=Store(self.s.state/'jobs.sqlite3',(self.s.state/'secret.key').read_bytes(),1)
        self.assertEqual(self.s.store.job(row['id'])['entrypoint'],'telegram')
        self.assertEqual(self.s.store.job(row['id'])['telegram_message_id'],0)
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
        buttons = self.last_card()[1]
        labels = [b['text'] for line in buttons for b in line]
        for height in (4320,2160,1920,1440,1080,720,480,360,240):
            self.assertIn(f'до {height}p · размер неизвестен', labels)

    def test_quality_menu_shows_actual_current_size_and_estimates_for_alternatives(self):
        row=self.job(); self.file(row)
        self.s.store.update(row['id'],state='choice',size=5*1024*1024,metadata=json.dumps({
            'heights':[1920,1280,640], 'qualitySizes':{'0':{'bytes':6*1024*1024},
            '1920':{'bytes':6*1024*1024},'1280':{'bytes':3*1024*1024},'640':{'bytes':None}}}))
        self.s.handle_callback(2,'quality:'+row['id'])
        labels=[b['text'] for line in self.last_card()[1] for b in line]
        self.assertIn('Лучшее · 5.0 МБ',labels)
        self.assertIn('до 1280p · ≈ 3.0 МБ',labels)
        self.assertIn('до 640p · размер неизвестен',labels)
        self.s.handle_callback(2,'height:'+row['id']+':1280')
        self.s.handle_callback(2,'quality:'+row['id'])
        labels=[b['text'] for line in self.last_card()[1] for b in line]
        self.assertIn('до 1280p · ≈ 3.0 МБ',labels)
        self.assertNotIn('до 1280p · 5.0 МБ',labels)

    def test_audio_format_does_not_show_video_size_estimates(self):
        row=self.job(); self.file(row)
        self.s.store.update(row['id'],state='choice',format='mp3',size=0,files='[]')
        self.s.handle_callback(2,'quality:'+row['id'])
        self.assertIn('после скачивания',self.last_card()[0])
        callbacks=[b['callback_data'] for line in self.last_card()[1] for b in line]
        self.assertFalse(any(c.startswith('height:') for c in callbacks))

    def test_full_task_edits_one_card_and_removes_buttons_after_delivery(self):
        row=self.job(uid=1); self.s.show_job(row['id'])
        mid=self.s.store.job(row['id'])['telegram_message_id']
        self.file(row); self.s.ask_choice(self.s.store.job(row['id']))
        self.s.handle_callback(1,'quality:'+row['id'],mid)
        self.s.handle_callback(1,'back:'+row['id'],mid)
        self.s.handle_callback(1,'file:'+row['id'],mid)
        self.s.run_job(self.s.store.job(row['id']))
        self.s.tg.say.assert_called_once()
        self.s.tg.document.assert_called_once()
        self.assertTrue(all(c.args[1]==mid for c in self.s.tg.edit.call_args_list))
        self.assertEqual(self.last_card()[1],[])
        self.assertEqual(self.s.store.job(row['id'])['state'],'done')
        self.assertNotIn(row['id'],self.last_card()[0])

    def test_completed_and_double_clicks_acknowledge_without_new_messages(self):
        row=self.job(); self.file(row); self.s.ask_choice(self.s.store.job(row['id']))
        row=self.s.store.job(row['id'])
        self.callback(row,'file')
        self.callback(row,'file')
        self.assertEqual(self.s.tg.say.call_count,1)
        self.assertIn('обрабатываю',self.s.tg.call.call_args.kwargs['text'])
        self.s.deliver(self.s.store.job(row['id']))
        for action in ('file','quality','height:720','cancel'):
            if action=='height:720':
                result=self.s.handle_callback(2,'height:'+row['id']+':720',row['telegram_message_id'])
                self.assertIn('уже отправлено',result)
            else:
                self.callback(row,action)
                self.assertIn('уже отправлено',self.s.tg.call.call_args.kwargs['text'])
        self.assertEqual(self.s.tg.say.call_count,1)
        self.assertEqual(self.s.store.job(row['id'])['cancelled'],0)

    def test_foreign_old_card_cannot_replace_current_card_or_choose_quality(self):
        row=self.job(); self.file(row); self.s.ask_choice(self.s.store.job(row['id']))
        row=self.s.store.job(row['id'])
        self.callback(row,'file',message_id=row['telegram_message_id']+1)
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.assertIn('устарела',self.s.tg.call.call_args.kwargs['text'])
        self.callback(row,'file',uid=3,message_id=row['telegram_message_id'])
        self.assertEqual(self.s.store.job(row['id'])['state'],'choice')
        self.assertEqual(self.s.store.job(row['id'])['telegram_message_id'],row['telegram_message_id'])
        self.assertEqual(self.s.tg.say.call_count,1)

    def test_old_completed_card_clears_buttons_without_resending_file(self):
        row=self.job(); self.s.store.update(row['id'],state='done')
        self.callback(row,'quality',message_id=999)
        self.s.tg.say.assert_not_called(); self.s.tg.document.assert_not_called()
        self.assertEqual(self.s.tg.call.call_args.kwargs['text'],'Видео уже отправлено.')
        self.assertEqual(self.s.tg.call.call_args_list[0].args[0],'editMessageReplyMarkup')

    def test_unavailable_delivery_buttons_are_hidden_and_size_is_still_visible(self):
        row=self.job(); self.file(row,1200); self.s.ask_choice(self.s.store.job(row['id']))
        buttons=str(self.last_card()[1])
        self.assertNotIn('link:',buttons); self.assertIn('file:',buttons)
        self.assertLess(len(self.last_card()[0]),180)
        self.s.store.update(row['id'],size=self.s.tg.limit+1)
        self.s.show_job(row['id'])
        buttons=str(self.last_card()[1])
        self.assertNotIn('file:',buttons); self.assertNotIn('link:',buttons)
        self.assertIn('quality:',buttons)
        self.s.youtube=Mock(); self.s.youtube.ready.return_value=True
        self.s.show_job(row['id'])
        self.assertIn('link:',str(self.last_card()[1]))

    def test_error_card_hides_raw_diagnostics_and_keeps_them_in_audit(self):
        row=self.job(uid=1); self.s.show_job(row['id'])
        raw='ERROR: [site] extractor failed; report this issue on yt-dlp; internal-id-123'
        self.s.media=Mock(side_effect=ValueError(raw))
        self.s.run_job(row)
        self.assertEqual(self.s.store.job(row['id'])['error'],raw)
        self.assertNotIn('ERROR',self.last_card()[0]); self.assertNotIn('internal-id',self.last_card()[0])
        self.assertLess(len(self.last_card()[0]),180)
        self.assertEqual(self.last_card()[1],[])
        self.s.tg.say.assert_called_once()

    def test_failed_edit_never_sends_a_duplicate_card(self):
        row=self.job(); self.s.show_job(row['id'])
        self.s.tg.edit.side_effect=ValueError('Telegram временно недоступен')
        self.assertIsNone(self.s.show_job(row['id']))
        self.s.tg.say.assert_called_once()
        self.assertGreater(self.s.store.job(row['id'])['telegram_message_id'],0)

    def test_unconfirmed_link_card_preserves_file_for_review(self):
        row=self.job(uid=1,mode='link'); p=self.file(row); self.s.show_job(row['id'])
        self.s.youtube=Mock(); self.s.youtube.upload.return_value='verified-video'
        self.s.tg.edit.side_effect=ValueError('Telegram не ответил')
        self.s.run_job(self.s.store.job(row['id']))
        self.assertEqual(self.s.store.job(row['id'])['state'],'review')
        self.assertTrue(p.exists())
        self.s.tg.say.assert_called_once()

    def test_card_id_survives_restart_and_expiry_removes_old_buttons(self):
        row=self.job(); self.file(row); self.s.ask_choice(self.s.store.job(row['id']))
        mid=self.s.store.job(row['id'])['telegram_message_id']
        self.s.store.db.close()
        self.s.store=Store(self.s.state/'jobs.sqlite3',(self.s.state/'secret.key').read_bytes(),1)
        self.s.store.update(row['id'],expires=time.time()-1)
        self.s.cleanup()
        self.assertEqual(self.s.store.job(row['id'])['telegram_message_id'],mid)
        self.assertEqual(self.s.store.job(row['id'])['state'],'expired')
        self.assertEqual(self.last_card()[1],[])
        self.s.tg.say.assert_called_once()

    def test_telegram_edit_explicitly_clears_keyboard_and_ignores_no_change(self):
        tg=Telegram('test-only'); tg.call=Mock(return_value={'message_id':123})
        tg.edit(1,123,'Готово')
        self.assertEqual(tg.call.call_args.args[0],'editMessageText')
        self.assertEqual(tg.call.call_args.kwargs['reply_markup'],{'inline_keyboard':[]})
        tg.call.side_effect=ValueError('Bad Request: message is not modified')
        self.assertEqual(tg.edit(1,123,'Готово'),{'message_id':123})


if __name__=='__main__': unittest.main()
