import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from zoneinfo import ZoneInfo

import paramiko
import py7zr
from backports.zstd import ZstdFile, get_frame_info


STATE = Path('/state')
SECRETS = Path('/secrets')
MOSCOW = ZoneInfo('Europe/Moscow')
ARCHIVE_RE = re.compile(r'([01]\d|2[0-3])(?:-00\.7z|:00\.tar\.zst\.age)')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def slot(now):
    return now.astimezone(MOSCOW).strftime('%Y-%m-%d/%H-00.7z')


def archive_time(day, name):
    if not DATE_RE.fullmatch(day) or not ARCHIVE_RE.fullmatch(name):
        return None
    try:
        return dt.datetime.strptime(day + '/' + name[:5].replace('-', ':'), '%Y-%m-%d/%H:%M').replace(tzinfo=MOSCOW)
    except ValueError:
        return None


@contextlib.contextmanager
def remote():
    config = json.loads((SECRETS / 'storage.json').read_text())
    client = paramiko.SSHClient()
    client.load_host_keys(str(SECRETS / 'known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(config['host'], port=23, username=config['username'],
                       key_filename=str(SECRETS / 'ssh_key'),
                       look_for_keys=False, allow_agent=False,
                       timeout=20, banner_timeout=20, auth_timeout=20)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(120)
            sftp.chdir('.')
            sftp.backup_client = client
            yield sftp
    finally:
        client.close()


def retry(operation):
    for attempt in range(1, 4):
        try:
            with remote() as sftp:
                return operation(sftp)
        except (OSError, EOFError, paramiko.SSHException) as error:
            emit('storage_error', attempt=attempt, error_type=type(error).__name__)
            if attempt == 3:
                raise
            time.sleep(5 * attempt)


def archives(sftp):
    for directory in sorted(sftp.listdir_attr('.'), key=lambda p: p.filename):
        day = directory.filename
        if not DATE_RE.fullmatch(day) or not stat.S_ISDIR(directory.st_mode):
            continue
        for entry in sorted(sftp.listdir_attr(day), key=lambda p: p.filename):
            stamp = archive_time(day, entry.filename)
            if stamp is not None and stat.S_ISREG(entry.st_mode):
                yield day + '/' + entry.filename, stamp, entry.st_size


def prune(sftp, now):
    cutoff = now.astimezone(MOSCOW) - dt.timedelta(days=30)
    removed = 0
    days = set()
    for name, stamp, _ in list(archives(sftp)):
        if stamp < cutoff:
            sftp.remove(name)
            days.add(name.split('/')[0])
            removed += 1
    for day in days:
        if not sftp.listdir(day):
            sftp.rmdir(day)
    emit('retention', removed=removed)


def upload(sftp, local, name):
    day = name.split('/')[0]
    try:
        sftp.mkdir(day)
    except OSError:
        if not stat.S_ISDIR(sftp.lstat(day).st_mode):
            raise
    temporary = name + '.partial'
    sftp.put(str(local), temporary, confirm=True)
    with sftp.open(temporary, 'rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != file_hash(local):
        raise RuntimeError('upload_integrity_failed')
    sftp.posix_rename(temporary, name)
    emit('upload_verified', archive=name, bytes=local.stat().st_size, sha256=actual)


def capacity(sftp):
    try:
        _, stdout, _ = sftp.backup_client.exec_command('df -k .', timeout=20)
        lines = stdout.read().decode().splitlines()
        if stdout.channel.recv_exit_status() == 0:
            return int(lines[-1].split()[3]) * 1024
    except (OSError, ValueError, IndexError, paramiko.SSHException):
        return None
    return None


def archive_password():
    password = (SECRETS / '7z-password').read_text().strip()
    if len(password) < 16:
        raise ValueError('invalid_archive_password')
    return password


def create_archive(source, target, password):
    if not (source / 'mongo.archive').exists():
        return create_7z(source, target, password)
    with tempfile.TemporaryDirectory(prefix='payload-', dir=target.parent) as temporary:
        payload = Path(temporary)
        for path in source.iterdir():
            if path.name == 'mongo.archive':
                continue
            if path.is_dir():
                shutil.copytree(path, payload / path.name)
            else:
                shutil.copy2(path, payload / path.name)
        subprocess.run(['zstd', '-T1', '-10', '--long=27', '-q', '-o',
                        str(payload / 'mongo.archive.zst'), str(source / 'mongo.archive')],
                       check=True, capture_output=True, timeout=900)
        create_7z(payload, target, password)


def create_7z(source, target, password):
    import pty
    import select
    import termios

    master, slave = pty.openpty()
    attributes = termios.tcgetattr(slave)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attributes)
    try:
        process = subprocess.Popen(
            ['7zz', 'a', '-t7z', '-mx=1', '-mmt=1', '-mhe=on',
             '-p', '-bsp0', str(target.resolve()), '.'],
            cwd=source, stdin=slave, stdout=slave, stderr=slave,
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    pending = b''
    sent = 0
    deadline = time.monotonic() + 1200
    try:
        while process.poll() is None:
            if time.monotonic() > deadline:
                raise TimeoutError('archive_creation_timeout')
            if not select.select([master], [], [], 1)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            pending = (pending + chunk)[-65536:]
            while sent < 2:
                match = re.search(rb'(?:enter|verify|confirm) password[^:]*:', pending, re.IGNORECASE)
                if match is None:
                    break
                os.write(master, password.encode() + b'\n')
                pending = pending[match.end():]
                sent += 1
        process.wait(timeout=10)
        if process.returncode != 0:
            raise RuntimeError('archive_creation_failed')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def run():
    started = json.loads((STATE / 'snapshot' / 'snapshot-info.json').read_text())['started_at']
    now = dt.datetime.now(MOSCOW)
    name = slot(now)
    with tempfile.TemporaryDirectory(prefix='archive-', dir=STATE) as work:
        work = Path(work)
        encrypted = work / 'snapshot.7z'
        create_archive(STATE / 'snapshot', encrypted, archive_password())

        def transfer(sftp):
            upload(sftp, encrypted, name)
            prune(sftp, now)
            return capacity(sftp)

        available = retry(transfer)
        status = {'status': 'ok', 'last_success': time.time(), 'archive': name,
                  'bytes': encrypted.stat().st_size, 'sha256': file_hash(encrypted),
                  'duration_seconds': round(time.time() - started, 3),
                  'available_bytes': available}
        temporary = STATE / 'status.json.tmp'
        temporary.write_text(json.dumps(status, indent=2))
        temporary.replace(STATE / 'status.json')
        emit('backup_complete', **status)


def verify_manifest(destination):
    manifest = json.loads((destination / 'manifest.json').read_text())
    actual = {p.relative_to(destination).as_posix(): file_hash(p)
              for p in destination.rglob('*') if p.is_file() and p != destination / 'manifest.json'}
    if manifest != actual:
        raise RuntimeError('restore_integrity_failed')


def extract_7z(source, destination, password):
    limit = max(0, shutil.disk_usage(destination.parent).free - 1024**3)
    with py7zr.SevenZipFile(source, 'r', password=password, max_extract_size=limit) as archive:
        for member in archive.files:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in member.filename or not (member.is_file or member.is_directory):
                raise RuntimeError('unsafe_archive_member')
        archive.extractall(path=destination)
    compressed = destination / 'mongo.archive.zst'
    if compressed.exists():
        with compressed.open('rb') as stream:
            expected = get_frame_info(stream.read(18)).decompressed_size
        if expected is None or expected > shutil.disk_usage(destination).free - 1024**3:
            raise RuntimeError('insufficient_restore_space')
        written = 0
        with ZstdFile(compressed, 'rb') as stream, (destination / 'mongo.archive').open('xb') as output:
            while chunk := stream.read(1024**2):
                written += len(chunk)
                if written > expected:
                    raise RuntimeError('mongo_archive_size_mismatch')
                output.write(chunk)
        if written != expected:
            raise RuntimeError('mongo_archive_size_mismatch')
        compressed.unlink()
    verify_manifest(destination)


def safe_extract(source, destination):
    with tarfile.open(source, 'r|*') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError('unsafe_archive_member')
            archive.extract(member, destination, filter='data')
    verify_manifest(destination)


def restore(name, destination):
    parts = name.split('/')
    if len(parts) != 2 or archive_time(*parts) is None:
        raise ValueError('invalid_archive_path')
    destination = Path(destination)
    restore_root = STATE.resolve() / 'restores'
    if not destination.is_absolute() or not destination.resolve().is_relative_to(restore_root):
        raise ValueError('restore_destination_outside_state_restores')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='restore-', dir=STATE) as work:
            encrypted = Path(work) / 'download.age'
            compressed = Path(work) / 'snapshot.tar.zst'
            uncompressed = Path(work) / 'snapshot.tar'
            retry(lambda sftp: sftp.get(name, str(encrypted)))
            if name.endswith('.7z'):
                extract_7z(encrypted, destination, archive_password())
            else:
                subprocess.run(['age', '-d', '-i', '/recovery/age-identity.txt',
                                '-o', str(compressed), str(encrypted)], check=True,
                               capture_output=True, timeout=600)
                subprocess.run(['zstd', '-d', '--long=27', '-q', '-o', str(uncompressed),
                                str(compressed)], check=True, capture_output=True, timeout=900)
                safe_extract(uncompressed, destination)
        emit('restore_verified', archive=name, destination=str(destination))
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    os.umask(0o077)
    action = sys.argv[1]
    if action == 'run' and len(sys.argv) == 2:
        run()
    elif action == 'list' and len(sys.argv) == 2:
        retry(lambda sftp: [emit('archive', path=n, bytes=size)
                            for n, _, size in archives(sftp)])
    elif action == 'restore' and len(sys.argv) == 4:
        restore(sys.argv[2], sys.argv[3])
    else:
        raise ValueError('invalid_arguments')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('backup_failed', error_type=type(error).__name__)
        sys.exit(1)
