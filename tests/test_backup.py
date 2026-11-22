import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import py7zr
except ImportError:
    py7zr = None


def load(name, path, substitutes):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, substitutes):
        spec.loader.exec_module(module)
    return module


worker = load('backup_worker', 'backup/worker.py',
              {'paramiko': Mock(), **({'py7zr': Mock(), 'backports': Mock(),
                                      'backports.zstd': Mock()} if py7zr is None else {})})
snapshot = load('backup_snapshot', 'backup/snapshot.py', {'fcntl': Mock()})


class BackupTests(unittest.TestCase):
    def test_moscow_hour_path(self):
        stamp = dt.datetime(2026, 11, 22, 22, 42, tzinfo=dt.timezone.utc)
        self.assertEqual(worker.slot(stamp), '2026-11-23/01-00.7z')

    def test_retention_only_removes_expired_hourly_archives(self):
        directories = ['2026-10-23', '2026-10-22', '2026-11-22', 'other', '.ssh']
        entries = {
            '2026-10-23': ['10-00.7z', '11:00.tar.zst.age', '12:00.tar.zst.age', '12-00.7z', 'unknown.txt'],
            '2026-10-22': ['12:00.tar.zst.age'],
            '2026-11-22': ['12:00.tar.zst.age', '12:00.tar.zst.age.partial'],
        }
        sftp = Mock()
        def listing(path):
            names = directories if path == '.' else entries[path]
            return [SimpleNamespace(filename=n, st_mode=(stat.S_IFDIR if path == '.' else stat.S_IFREG), st_size=10) for n in names]
        sftp.listdir_attr.side_effect = listing
        sftp.listdir.side_effect = lambda day: ['unknown.txt'] if day == '2026-10-23' else []
        worker.prune(sftp, dt.datetime(2026, 11, 22, 12, tzinfo=worker.MOSCOW))
        self.assertEqual([c.args[0] for c in sftp.remove.call_args_list],
                         ['2026-10-22/12:00.tar.zst.age', '2026-10-23/10-00.7z', '2026-10-23/11:00.tar.zst.age'])
        sftp.rmdir.assert_called_once_with('2026-10-22')

    def test_symlinked_remote_date_is_never_traversed(self):
        sftp = Mock()
        sftp.listdir_attr.return_value = [SimpleNamespace(filename='2026-10-01', st_mode=stat.S_IFLNK)]
        self.assertEqual(list(worker.archives(sftp)), [])
        sftp.listdir_attr.assert_called_once_with('.')

    def test_corrupt_upload_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'archive'
            source.write_bytes(b'correct')
            sftp = Mock()
            sftp.open.return_value = io.BytesIO(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, 'integrity'):
                worker.upload(sftp, source, '2026-11-22/12:00.tar.zst.age')
            sftp.posix_rename.assert_not_called()

    def test_restore_rejects_traversal_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                item = tarfile.TarInfo('../escaped')
                item.size = 1
                archive.addfile(item, io.BytesIO(b'x'))
            with self.assertRaisesRegex(RuntimeError, 'unsafe'):
                worker.safe_extract(source, Path(directory) / 'target')
            self.assertFalse((Path(directory) / 'escaped').exists())

    @unittest.skipIf(py7zr is None or shutil.which('7zz') is None, '7z dependencies are available in the backup image')
    def test_encrypted_7z_roundtrip_includes_nested_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'project').mkdir()
            (source / 'project' / 'manifest.json').write_text('private data')
            (source / 'mongo.archive').write_bytes(b'mongodb-backup-fixture' * 100000)
            manifest = {'project/manifest.json': worker.file_hash(source / 'project' / 'manifest.json')}
            manifest['mongo.archive'] = worker.file_hash(source / 'mongo.archive')
            (source / 'manifest.json').write_text(json.dumps(manifest))
            archive = root / 'archive.7z'
            password = 'test-password-not-a-secret'
            worker.create_archive(source, archive, password)
            self.assertEqual(archive.read_bytes()[:6], b'7z\xbc\xaf\x27\x1c')
            self.assertNotIn(b'private data', archive.read_bytes())
            with self.assertRaises(worker.py7zr.exceptions.PasswordRequired):
                worker.py7zr.SevenZipFile(archive, 'r')
            native = shutil.which('7zz')
            if native:
                result = subprocess.run([native, 't', str(archive), '-p' + password],
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0)
            destination = root / 'restore'
            destination.mkdir()
            worker.extract_7z(archive, destination, password)
            self.assertEqual((destination / 'project' / 'manifest.json').read_text(), 'private data')

    @unittest.skipIf(py7zr is None or shutil.which('7zz') is None, '7z dependencies are available in the backup image')
    def test_wrong_7z_password_does_not_extract_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'manifest.json').write_text('{}')
            archive = root / 'archive.7z'
            worker.create_archive(source, archive, 'correct-test-password')
            destination = root / 'restore'
            destination.mkdir()
            with self.assertRaises(Exception):
                worker.extract_7z(archive, destination, 'incorrect-test-password')
            self.assertEqual(list(destination.iterdir()), [])

    def test_restore_checks_file_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                for name, content in [('manifest.json', b'{"data":"invalid"}'), ('data', b'changed')]:
                    item = tarfile.TarInfo(name)
                    item.size = len(content)
                    archive.addfile(item, io.BytesIO(content))
            with self.assertRaisesRegex(RuntimeError, 'integrity'):
                worker.safe_extract(source, Path(directory) / 'target')

    def inspection(self):
        return json.dumps([{'Image': 'sha256:test', 'Mounts': [
            {'Destination': '/data/db', 'Type': 'bind', 'Source': '/db'}]}]).encode()

    def test_copy_timeout_unlocks_database(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(snapshot, 'STATE', Path(directory)), patch.object(snapshot, 'mongo', side_effect=[False, {'ok': 1}]), patch.object(snapshot, 'unlock') as unlock, patch.object(snapshot.subprocess, 'run', side_effect=TimeoutError), patch.object(snapshot.subprocess, 'check_output', return_value=self.inspection()):
            with self.assertRaises(TimeoutError):
                snapshot.snapshot(Path(directory))
            unlock.assert_called_once()

    def test_existing_database_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(snapshot, 'STATE', Path(directory)), patch.object(snapshot, 'mongo', return_value=True), patch.object(snapshot, 'unlock') as unlock, patch.object(snapshot.subprocess, 'check_output', return_value=self.inspection()):
            with self.assertRaisesRegex(RuntimeError, 'already_locked'):
                snapshot.snapshot(Path(directory))
            unlock.assert_not_called()
            self.assertFalse((Path(directory) / 'mongo-lock.json').exists())

    def test_database_unlocked_before_isolated_dump(self):
        events = []
        def execute(args, **kwargs):
            if args[:3] == ['docker', 'run', '-d']:
                self.assertIn('unlock', events)
                self.assertIn('--network=none', args)
            events.append(args[0])
            return SimpleNamespace(returncode=0, stdout='{"collections":{},"data_bytes":0}')
        with tempfile.TemporaryDirectory() as directory, patch.object(snapshot, 'STATE', Path(directory)), patch.object(snapshot, 'mongo', side_effect=[False, {'ok': 1}]), patch.object(snapshot, 'unlock', side_effect=lambda: events.append('unlock')), patch.object(snapshot.subprocess, 'run', side_effect=execute), patch.object(snapshot.subprocess, 'check_output', return_value=self.inspection()):
            snapshot.snapshot(Path(directory))
        self.assertLess(events.index('unlock'), events.index('docker'))

    def test_restore_cannot_target_production(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            worker.restore('2026-11-22/12:00.tar.zst.age', '/root/project/YaMusicBot')

    def test_stale_health_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(snapshot, 'STATE', Path(directory)), patch.object(snapshot.time, 'time', return_value=60000):
            (Path(directory) / 'status.json').write_text(json.dumps({'last_success': 2000, 'status': 'ok'}))
            self.assertEqual(snapshot.health(), 1)

    def test_health_boundary_is_two_hours(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(snapshot, 'STATE', Path(directory)):
            (Path(directory) / 'status.json').write_text(json.dumps({'last_success': 1000, 'status': 'ok'}))
            with patch.object(snapshot.time, 'time', return_value=8200):
                self.assertEqual(snapshot.health(), 0)
            with patch.object(snapshot.time, 'time', return_value=8201):
                self.assertEqual(snapshot.health(), 1)


if __name__ == '__main__':
    unittest.main()
