import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import updater
from scripts.build_release import build

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.package = build(ROOT, self.root / 'VideoDrop.zip')

    def tearDown(self):
        self.temp.cleanup()

    def test_distribution_excludes_all_user_data_and_roundtrips(self):
        with zipfile.ZipFile(self.package) as z:
            names = z.namelist()
            self.assertNotIn('extension/config.js', names)
            self.assertFalse(any(n.startswith(('.state/', '.venv/', 'Загрузки/', 'tests/')) for n in names))
        stage = self.root / 'program'
        files = updater.unpack_package(self.package, stage, '1.4.0', 'Jeckyllll/videodrop')
        self.assertEqual(set(files), set(updater.program_files(ROOT)))
        self.assertEqual((stage / 'app.py').read_bytes(), (ROOT / 'app.py').read_bytes())
        self.assertTrue((stage / 'Запустить VideoDrop.command').stat().st_mode & stat.S_IXUSR)

    def test_rejects_zip_traversal_userdata_and_symlinks_before_writing(self):
        for name in ('../escape.py', '/tmp/escape.py', 'extension/config.js', '.state/token', 'Загрузки/a.mp4', 'web/../app.py', 'web\\app.js'):
            with self.subTest(name=name):
                path = self.root / 'unsafe.zip'
                shutil.copy2(self.package, path)
                with zipfile.ZipFile(path, 'a') as z:
                    z.writestr(name, 'malicious')
                with self.assertRaises(ValueError):
                    updater.unpack_package(path, self.root / 'absent', '1.4.0', 'Jeckyllll/videodrop')
                self.assertFalse((self.root / 'absent').exists())
        path = self.root / 'symlink.zip'
        with zipfile.ZipFile(path, 'w') as z:
            item = zipfile.ZipInfo('web/evil.js'); item.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(item, '/tmp/escape')
            z.writestr('release-manifest.json', '{}')
        with self.assertRaises(ValueError):
            updater.unpack_package(path, self.root / 'absent', '1.4.0', 'Jeckyllll/videodrop')

    def test_wrong_version_repository_or_checksum_rejected(self):
        for version, repository in [('1.4.1', 'Jeckyllll/videodrop'), ('1.4.0', 'other/repo')]:
            with self.assertRaises(ValueError):
                updater.unpack_package(self.package, self.root / 'absent', version, repository)
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(bad, 'w') as dest:
            for name in source.namelist():
                dest.writestr(name, b'corrupt' if name == 'worker.py' else source.read(name))
        with self.assertRaises(ValueError):
            updater.unpack_package(bad, self.root / 'absent', '1.4.0', 'Jeckyllll/videodrop')
        self.assertFalse((self.root / 'absent').exists())

    def test_github_metadata_pins_repository_asset_version_and_digest(self):
        release = {'tag_name': 'v1.4.1', 'assets': [{'name': updater.ASSET, 'size': 500,
            'digest': 'sha256:' + 'a'*64, 'browser_download_url': 'https://github.com/Jeckyllll/videodrop/releases/download/v1.4.1/' + updater.ASSET}]}
        with patch.object(updater, 'github_fetch', return_value=json.dumps(release).encode()):
            self.assertEqual(updater.latest_release('Jeckyllll/videodrop')['version'], '1.4.1')
        for field, value in [('digest', ''), ('size', updater.MAX_ARCHIVE+1), ('browser_download_url', 'http://127.0.0.1/package.zip')]:
            damaged = json.loads(json.dumps(release)); damaged['assets'][0][field] = value
            with patch.object(updater, 'github_fetch', return_value=json.dumps(damaged).encode()), self.assertRaises(ValueError):
                updater.latest_release('Jeckyllll/videodrop')
        self.assertFalse(updater.valid_github_url('https://github.com.evil.test/a'))
        self.assertFalse(updater.valid_github_url('https://user:secret@github.com/a'))
        self.assertGreater(updater.version_tuple('1.10.0'), updater.version_tuple('1.9.9'))

    def test_replace_and_restore_preserve_credentials_history_downloads_and_remove_new_files(self):
        target = self.root / 'installed'; stage = self.root / 'stage'; backup = self.root / 'backup'
        updater.unpack_package(self.package, target, '1.4.0', 'Jeckyllll/videodrop')
        updater.unpack_package(self.package, stage, '1.4.0', 'Jeckyllll/videodrop')
        for name in ('.state/token', '.state/studio-jobs.json', 'extension/config.js', 'Загрузки/урок.mp4'):
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'private')
        old = updater.snapshot(target, backup)
        (stage / 'app.py').write_text('changed')
        (stage / 'web/new.js').write_text('new')
        new = updater.program_files(stage)
        updater.replace_program(target, stage, new, old)
        self.assertEqual((target / 'app.py').read_text(), 'changed')
        updater.restore_transaction(target, {'backup': str(backup), 'oldFiles': old, 'newFiles': new})
        self.assertEqual((target / 'app.py').read_bytes(), (ROOT / 'app.py').read_bytes())
        self.assertFalse((target / 'web/new.js').exists())
        for name in ('.state/token', '.state/studio-jobs.json', 'extension/config.js', 'Загрузки/урок.mp4'):
            self.assertEqual((target / name).read_bytes(), b'private')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        updater.write_json(self.root / 'version.json', {'version': '1.4.0', 'repository': 'Jeckyllll/videodrop'})
        self.jobs = {}
        self.updates = updater.Updater(self.root, threading.RLock(), self.jobs, lambda: None, lambda: None)
        self.updates.cache = {'checkedAt': time.time(), 'latest': {'version': '1.4.1'}}
        self.updates.next_check = time.time() + 3600

    def tearDown(self):
        self.temp.cleanup()

    def test_waits_for_download_upload_and_connect_then_starts(self):
        with patch.object(self.updates, '_prepare') as prepare:
            for kind in ('inspect', 'download'):
                self.jobs['a'] = {'kind': kind, 'status': 'working'}
                self.updates.tick(); prepare.assert_not_called()
            self.jobs.clear(); self.updates.busy = lambda: True
            self.updates.tick(); prepare.assert_not_called()
            self.updates.busy = lambda: False
            self.updates.tick()
            for _ in range(20):
                if prepare.called: break
                time.sleep(.01)
            prepare.assert_called_once_with(False)

    def test_disabled_automatic_still_allows_manual_install(self):
        self.updates.set_automatic(False)
        with patch.object(self.updates, '_prepare') as prepare:
            self.updates.tick(); prepare.assert_not_called()
            self.updates.install(); self.updates.tick()
            for _ in range(20):
                if prepare.called: break
                time.sleep(.01)
            prepare.assert_called_once_with(False)

    def test_failed_or_rolled_back_version_not_reinstalled_automatically(self):
        updater.write_json(self.updates.state / 'result.json', {'blockedVersion': '1.4.1'})
        with patch.object(self.updates, '_prepare') as prepare:
            self.updates.tick(); prepare.assert_not_called()
        self.updates.install()
        self.assertTrue(self.updates.pending)

    def test_restarting_rejects_new_jobs(self):
        self.updates.phase = 'restarting'
        with self.assertRaises(ValueError): self.updates.ensure_accepting()

    def test_network_failure_preserves_current_version_and_is_not_retried_every_tick(self):
        self.updates.cache = {}
        with patch.object(updater, 'latest_release', side_effect=ValueError('offline')) as latest:
            self.updates._check()
            self.updates.tick(); self.updates.tick()
            latest.assert_called_once()
        self.assertEqual(self.updates.status()['version'], '1.4.0')
        self.assertEqual(self.updates.status()['message'], 'offline')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / '.state' / 'updates'; self.state.mkdir(parents=True)
        (self.root / 'app.py').write_text('old program')
        updater.write_json(self.root / 'version.json', {'version': '1.4.0'})
        self.stage = self.root / '.state' / 'stage'; self.stage.mkdir()
        (self.stage / 'app.py').write_text('new program')
        updater.write_json(self.stage / 'version.json', {'version': '1.4.1'})
        self.plan = {'pid': 99999999, 'stage': str(self.stage), 'newFiles': ['app.py', 'version.json'],
                     'version': '1.4.1', 'oldVersion': '1.4.0', 'python': sys.executable, 'oldPython': sys.executable,
                     'base': 'http://127.0.0.1:9999', 'token': 'test', 'rollback': False}
        self.path = self.state / 'handoff.json'; updater.write_json(self.path, self.plan)

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_start_restores_old_program_and_blocks_repeat(self):
        with patch.object(updater, 'start_app') as start, patch.object(updater, 'health', return_value=False):
            updater.helper(self.root, self.path)
        self.assertEqual((self.root / 'app.py').read_text(), 'old program')
        self.assertEqual(start.call_count, 2)
        self.assertFalse((self.state / 'transaction.json').exists())
        self.assertEqual(updater.read_json(self.state / 'result.json')['blockedVersion'], '1.4.1')

    def test_success_keeps_one_backup_and_a_manual_rollback_restores_version(self):
        with patch.object(updater, 'start_app'), patch.object(updater, 'health', return_value=True):
            updater.helper(self.root, self.path)
        self.assertEqual((self.root / 'app.py').read_text(), 'new program')
        previous = updater.read_json(self.state / 'previous.json')
        self.assertEqual(previous['version'], '1.4.0')
        rollback = {**self.plan, 'stage': previous['folder'], 'newFiles': previous['files'],
                    'version': '1.4.0', 'oldVersion': '1.4.1', 'rollback': True}
        updater.write_json(self.path, rollback)
        with patch.object(updater, 'start_app'), patch.object(updater, 'health', return_value=True):
            updater.helper(self.root, self.path)
        self.assertEqual((self.root / 'app.py').read_text(), 'old program')
        self.assertEqual(updater.read_json(self.state / 'result.json')['blockedVersion'], '1.4.1')
        self.assertEqual(len(list(self.state.glob('backup-*'))), 1)

    def test_restart_after_crash_recovers_transaction(self):
        backup = self.state / 'backup'; old = updater.snapshot(self.root, backup)
        transaction = {**self.plan, 'backup': str(backup), 'oldFiles': old, 'oldRuntime': {'python': '/original/python'}}
        updater.write_json(self.state / 'transaction.json', transaction)
        (self.root / 'app.py').write_text('partially replaced')
        subprocess.run([sys.executable, str(ROOT / 'updater.py'), '--recover', str(self.root)], check=True)
        self.assertEqual((self.root / 'app.py').read_text(), 'old program')
        self.assertEqual(updater.read_json(self.root / '.state' / 'runtime.json'), {'python': '/original/python'})
        self.assertFalse((self.state / 'transaction.json').exists())


if __name__ == '__main__': unittest.main()
