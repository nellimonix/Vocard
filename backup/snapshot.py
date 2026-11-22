import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(os.environ.get('BACKUP_PROJECT', '/root/project/YaMusicBot'))
STATE = Path(os.environ.get('BACKUP_STATE', '/var/lib/yamusicbot-backup'))
HERE = Path(__file__).resolve().parent
DB = os.environ.get('BACKUP_DB_CONTAINER', 'vocard-db')
APP = os.environ.get('BACKUP_APP_CONTAINER', 'vocard')
AUTH = ('const a=db.getSiblingDB("admin"); '
        'if (!a.auth(process.env.MONGO_INITDB_ROOT_USERNAME, '
        'process.env.MONGO_INITDB_ROOT_PASSWORD)) quit(10); ')


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def mongo(js):
    result = subprocess.run(
        ['docker', 'exec', DB, 'mongosh', '--quiet', '--eval', AUTH + js],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError('mongo_command_failed')
    return json.loads(result.stdout.strip().splitlines()[-1])


def unlock():
    marker = STATE / 'mongo-lock.json'
    if not marker.exists():
        return
    if json.loads(marker.read_text()).get('container') != DB:
        raise RuntimeError('mongo_lock_container_mismatch')
    result = mongo('print(JSON.stringify(a.runCommand({fsyncUnlock:1})))')
    if result.get('ok') != 1:
        if mongo('print(JSON.stringify(a.currentOp().fsyncLock === true))'):
            raise RuntimeError('mongo_unlock_failed')
    marker.unlink()
    emit('mongo_unlocked')


def cleanup():
    subprocess.run(['docker', 'rm', '-f', 'yamusicbot-backup-run', 'yamusicbot-backup-mongo'],
                   capture_output=True, timeout=30)
    unlock()
    for path in STATE.glob('archive-*'):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    shutil.rmtree(STATE / 'mongo-copy', ignore_errors=True)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stage(destination):
    excluded = {'.git', '.test-data', '.venv', '__pycache__', 'logs',
                '.idea', '.pytest_cache', '.codebase-memory', 'node_modules',
                'PENDING_PRODUCTION.md', 'DEPLOYMENT_LOG.jsonl'}

    def ignore(directory, names):
        raw = {'db', 'conf'} if Path(directory) == ROOT / 'data' / 'mongo' else set()
        return [n for n in names if n in excluded or n in raw or n.endswith(('.pyc', '.bak'))]

    shutil.copytree(ROOT, destination / 'project', ignore=ignore, symlinks=True)
    shutil.copytree(HERE, destination / 'project' / 'backup', ignore=ignore,
                    symlinks=True, dirs_exist_ok=True)
    for path in (destination / 'project').rglob('*'):
        if path.is_symlink():
            raise RuntimeError('project_symlink_requires_review')
    for source in [Path('/etc/wireguard/wg0.conf'),
                   Path('/usr/local/sbin/yamusic-tunnel-health.py'),
                   Path('/etc/systemd/system/yamusic-tunnel-health.service'),
                   Path('/etc/systemd/system/yamusic-tunnel-health.timer')]:
        if source.is_file():
            target = destination / 'infrastructure' / source.relative_to('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for name in ['last-session.json', '.env']:
        probe = subprocess.run(['docker', 'exec', APP, 'test', '-f', '/app/' + name])
        if probe.returncode == 0:
            subprocess.run(['docker', 'cp', APP + ':/app/' + name,
                            str(destination / 'project' / name)], check=True,
                           capture_output=True, timeout=30)
    versions = {}
    for container in [APP, DB, 'lavalink']:
        obj = json.loads(subprocess.check_output(['docker', 'inspect', container]))[0]
        versions[container] = {'image': obj['Image'], 'mounts': obj['Mounts']}
    (destination / 'runtime.json').write_text(json.dumps(versions, indent=2))


def snapshot(destination):
    obj = json.loads(subprocess.check_output(['docker', 'inspect', DB]))[0]
    mounts = [m for m in obj['Mounts'] if m['Destination'] == '/data/db']
    if len(mounts) != 1 or mounts[0]['Type'] != 'bind':
        raise RuntimeError('unsupported_mongo_mount')
    source = Path(mounts[0]['Source'])
    physical = STATE / 'mongo-copy'
    if physical.exists():
        raise RuntimeError('mongo_copy_exists')
    if mongo('print(JSON.stringify(a.currentOp().fsyncLock === true))'):
        raise RuntimeError('mongo_already_locked')
    (STATE / 'mongo-lock.json').write_text(json.dumps({'container': DB}))
    try:
        result = mongo('print(JSON.stringify(a.runCommand({fsync:1,lock:true})))')
        if result.get('ok') != 1:
            raise RuntimeError('mongo_lock_failed')
        started = time.monotonic()
        subprocess.run(['cp', '-a', '--reflink=auto', str(source), str(physical)],
                       check=True, capture_output=True, timeout=60)
        emit('mongo_snapshot', seconds=round(time.monotonic() - started, 3))
    finally:
        unlock()
    try:
        subprocess.run(['docker', 'run', '-d', '--name', 'yamusicbot-backup-mongo',
                        '--network=none', '--memory=1g', '--cpus=1.5', '--pids-limit=128',
                        '-v', str(physical) + ':/data/db', '--entrypoint', 'mongod',
                        obj['Image'], '--bind_ip', '127.0.0.1', '--noauth',
                        '--noscripting', '--wiredTigerCacheSizeGB=0.25'],
                       check=True, capture_output=True, timeout=30)
        deadline = time.monotonic() + 60
        while True:
            result = subprocess.run(['docker', 'exec', 'yamusicbot-backup-mongo',
                                     'mongosh', '--quiet', '--eval', 'quit(db.adminCommand({ping:1}).ok===1?0:1)'],
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError('snapshot_mongo_not_ready')
            time.sleep(2)
        script = ('const result={}; let size=0; '
                  'for(const item of db.adminCommand({listDatabases:1,nameOnly:true}).databases) {'
                  'if (!["admin","config","local"].includes(item.name)) {'
                  'const d=db.getSiblingDB(item.name); const r=d.runCommand({dbHash:1}); '
                  'if(r.ok!==1) quit(12); result[item.name]=r.collections; size+=d.stats().dataSize; }} '
                  'print(JSON.stringify({collections:result,data_bytes:size}))')
        result = subprocess.run(['docker', 'exec', 'yamusicbot-backup-mongo', 'mongosh',
                                 '--quiet', '--eval', script], capture_output=True,
                                text=True, check=True, timeout=180)
        checks = json.loads(result.stdout.strip().splitlines()[-1])
        if shutil.disk_usage(STATE).free < checks['data_bytes'] * 1.2 + 1024**3:
            raise RuntimeError('insufficient_snapshot_space')
        (destination / 'mongo-checks.json').write_text(json.dumps(checks, sort_keys=True))
        with (destination / 'mongo.archive').open('wb') as stream:
            result = subprocess.run(['docker', 'exec', 'yamusicbot-backup-mongo',
                                     'mongodump', '--archive', '--quiet'],
                                    stdout=stream, stderr=subprocess.PIPE, timeout=900)
        if result.returncode:
            raise RuntimeError('mongodump_failed')
        emit('mongo_dump', bytes=(destination / 'mongo.archive').stat().st_size)
    finally:
        subprocess.run(['docker', 'rm', '-f', 'yamusicbot-backup-mongo'],
                       capture_output=True, timeout=30)
        shutil.rmtree(physical, ignore_errors=True)


def health():
    try:
        data = json.loads((STATE / 'status.json').read_text())
        age = time.time() - data['last_success']
        emit('backup_health', age_seconds=int(age), status=data['status'],
             archive=data.get('archive'), bytes=data.get('bytes'))
        return 0 if age <= 7200 and data['status'] == 'ok' else 1
    except (OSError, ValueError, KeyError):
        emit('backup_health', status='missing')
        return 1


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if action == 'health':
        return health()
    if action == 'unlock':
        unlock()
        return 0
    if action == 'cleanup':
        cleanup()
        return 0
    if action != 'run':
        raise ValueError('invalid_action')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'backup.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit('backup_already_running')
            return 0
        cleanup()
        staging = STATE / 'snapshot'
        started = time.time()
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(mode=0o700)
            stage(staging)
            snapshot(staging)
            (staging / 'snapshot-info.json').write_text(json.dumps({'started_at': started}))
            manifest = {p.relative_to(staging).as_posix(): digest(p)
                        for p in staging.rglob('*') if p.is_file()}
            (staging / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
            subprocess.run(['docker', 'compose', '-f', str(HERE / 'compose.yml'),
                            'run', '--rm', '--no-deps', '--name',
                            'yamusicbot-backup-run', 'backup', 'run'], check=True,
                           timeout=1500)
        finally:
            cleanup()
            shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        emit('backup_failed', error_type=type(error).__name__,
             code=str(error) if isinstance(error, (RuntimeError, ValueError)) else None)
        sys.exit(1)
