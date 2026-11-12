#!/usr/bin/env python3
"""Encrypted, incremental backup of the local Codex chat store."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

import catalog
from configuration import CosTarget, DATA

HOME = Path.home()
SOURCE = HOME / '.codex'
KEYCHAIN = b'com.waynegong.codex-session-backup'
DATABASES = ('state_5.sqlite', 'thread_history_1.sqlite', 'goals_1.sqlite')
TREES = ('sessions', 'archived_sessions', 'attachments', 'artifacts', 'generated_images', 'visualizations')
TAG = 'codex-sessions-v1'
RETENTION = ('--tag', TAG, '--keep-last', '2', '--keep-daily', '30',
             '--keep-weekly', '12', '--keep-monthly', '12')
DAY = 86400
LABEL = 'com.waynegong.codex-session-backup'
ID = re.compile(r'[a-f0-9]{8,64}')


class BackupError(Exception):
    pass


def keychain(name, value=None):
    # Security.framework keeps credential bytes out of shell arguments and logs.
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    account = name.encode()
    length, password, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    found = sec.SecKeychainFindGenericPassword(
        None, len(KEYCHAIN), KEYCHAIN, len(account), account,
        ctypes.byref(length) if value is None else None,
        ctypes.byref(password) if value is None else None, ctypes.byref(item))
    try:
        if value is None:
            if found:
                raise BackupError(f'Keychain item {name} unavailable ({found})')
            try:
                return ctypes.string_at(password, length.value)
            finally:
                sec.SecKeychainItemFreeContent(None, password)
        if found == -25300:
            result = sec.SecKeychainAddGenericPassword(None, len(KEYCHAIN), KEYCHAIN,
                len(account), account, len(value), value, None)
        elif found == 0:
            result = sec.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
        else:
            result = found
        if result:
            raise BackupError(f'Keychain write failed ({result})')
    finally:
        if item:
            ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation').CFRelease(item)


def write_json(path, data):
    staging = path.with_suffix(path.suffix + '.new')
    staging.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    staging.replace(path)


def read_json(path, default=None):
    return json.loads(path.read_bytes()) if path.is_file() else default


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return {'size': path.stat().st_size, 'sha256': h.hexdigest()}


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(('RESTIC_', 'RCLONE_', 'AWS_'))}
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    return env


class Repository:
    def __init__(self, args, maintenance=False):
        self.data = args.data_dir
        self.local = args.local_repo
        self.env = clean_env()
        self.env['RESTIC_CACHE_DIR'] = str(self.data / 'restic-cache')
        self.host = 'codex-mac'
        if self.local:
            self.url = str(self.local.resolve())
            self.env['RESTIC_PASSWORD'] = os.environ.get('CODEX_BACKUP_TEST_PASSWORD', '')
            if not self.env['RESTIC_PASSWORD']:
                raise BackupError('Set CODEX_BACKUP_TEST_PASSWORD for the local repository')
            return
        self.target = CosTarget.load(self.data / 'connection.json')
        self.url = self.target.repository
        self.host = self.target.backup_host
        self.env['RESTIC_PASSWORD'] = keychain('repository-password').decode()
        credentials = json.loads(keychain('cos-maintenance' if maintenance else 'cos-writer'))
        self.env.update({
            'RCLONE_CONFIG': '/dev/null',
            'RCLONE_CONFIG_CODEXCOS_TYPE': 's3',
            'RCLONE_CONFIG_CODEXCOS_PROVIDER': 'TencentCOS',
            'RCLONE_CONFIG_CODEXCOS_ENDPOINT': 'https://' + self.target.endpoint,
            'RCLONE_CONFIG_CODEXCOS_REGION': self.target.region,
            'RCLONE_CONFIG_CODEXCOS_FORCE_PATH_STYLE': 'false',
            'RCLONE_CONFIG_CODEXCOS_NO_CHECK_BUCKET': 'true',
            'RCLONE_CONFIG_CODEXCOS_ACL': 'private',
            'RCLONE_CONFIG_CODEXCOS_ACCESS_KEY_ID': credentials['id'],
            'RCLONE_CONFIG_CODEXCOS_SECRET_ACCESS_KEY': credentials['key'],
            'NO_PROXY': self.target.endpoint + ',.' + self.target.endpoint,
            'no_proxy': self.target.endpoint + ',.' + self.target.endpoint,
        })

    def restic(self, *args, cwd=None, output=subprocess.PIPE):
        result = subprocess.run(['restic', '-r', self.url, *args],
            env=self.env, cwd=cwd, stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            detail = result.stderr.decode(errors='replace').strip().splitlines()
            message = detail[-1][:500] if detail else ''
            raise BackupError(f'restic {args[0]} exited {result.returncode}. {message}')
        return result.stdout

    def dump(self, snapshot, relative, output=subprocess.PIPE):
        return self.restic('dump', snapshot, '/bundle/' + relative, output=output)

    def probe(self):
        if self.local:
            return
        def rclone(*args, data=None, expect_denied=False):
            result = subprocess.run(['rclone', *args, '--retries', '1', '--low-level-retries', '1',
                                     '--contimeout', '10s', '--timeout', '30s'],
                                    env=self.env, input=data, capture_output=True)
            if expect_denied:
                if result.returncode and ('AccessDenied' in result.stderr.decode(errors='replace')
                                           or 'StatusCode: 403' in result.stderr.decode(errors='replace')):
                    return
                raise BackupError('COS credentials must be denied listing the bucket root')
            if result.returncode:
                raise BackupError('Signed COS probe failed: ' +
                                  (result.stderr.decode(errors='replace').splitlines() or ['unknown error'])[-1][:500])
            return result.stdout

        rclone('lsf', f'codexcos:{self.target.bucket}', '--max-depth', '1', expect_denied=True)
        probe = self.target.remote + '/locks/probe-' + secrets.token_hex(16)
        sample = secrets.token_bytes(64)
        rclone('rcat', probe, '--size', str(len(sample)), data=sample)
        try:
            rclone('lsf', self.target.remote, '--max-depth', '1')
            if rclone('cat', probe) != sample:
                raise BackupError('COS probe readback differs from upload')
        finally:
            rclone('deletefile', probe)


@contextmanager
def locked(data):
    with (data / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('Another backup or maintenance operation is running')
        yield


def copy_regular(source, target):
    if not source.is_file() or source.is_symlink():
        raise BackupError(f'Expected a regular file: {source.name}')
    if sys.platform == 'darwin':
        # APFS clones make the individual file copy independent of later appends.
        subprocess.run(['/bin/cp', '-c', '-p', str(source), str(target)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        shutil.copy2(source, target)


def snapshot_db(source, target):
    if not source.is_file() or source.is_symlink():
        raise BackupError(f'Missing database: {source.name}')
    deadline = time.monotonic() + 120

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise BackupError(f'SQLite backup timed out: {source.name}')

    with closing(catalog.readonly(source)) as db, closing(sqlite3.connect(target)) as clone:
        db.backup(clone, pages=256, progress=progress, sleep=0.05)
        clone.execute('PRAGMA journal_mode=DELETE')
        if source.name == 'state_5.sqlite':
            clone.execute('PRAGMA secure_delete=ON')
            if clone.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='remote_control_enrollments'").fetchone():
                clone.execute('DELETE FROM remote_control_enrollments')
            clone.commit()
        if clone.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise BackupError(f'SQLite check failed: {source.name}')


def project_map(source, thread_ids):
    settings = read_json(source / '.codex-global-state.json', {})
    projects = settings.get('local-projects', {})
    assignments = settings.get('thread-project-assignments', {})
    return {thread: projects.get(assignment.get('projectId'), {}).get('name')
            for thread, assignment in assignments.items()
            if thread in thread_ids and isinstance(assignment, dict)}


def thread_rows(source, destination):
    with closing(catalog.readonly(destination / 'state_5.sqlite')) as state:
        rows = state.execute("""SELECT id,coalesce(nullif(title,''),substr(nullif(first_user_message,''),1,160),id),
             cwd,updated_at,archived,rollout_path FROM threads""").fetchall()
    with closing(catalog.readonly(destination / 'thread_history_1.sqlite')) as db:
        offsets = dict(db.execute(
            'SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state'))
    for thread, title, cwd, updated, archived, original in rows:
        relative = Path(original).relative_to(source)
        expected_tree = 'archived_sessions' if archived else 'sessions'
        if not relative.parts or relative.parts[0] != expected_tree:
            raise BackupError('Rollout path and archive state disagree')
        path = destination / relative
        if not path.is_file():
            path = path.with_name(path.name + '.zst')
        if not path.is_file():
            raise BackupError('A rollout moved or disappeared during capture')
        if path.suffix != '.zst' and (offsets.get(thread) or 0) > path.stat().st_size:
            raise BackupError('History projection extends past the captured rollout')
        yield thread, title, cwd, updated, archived, path.relative_to(destination.parent).as_posix()


def copy_tree(source, target):
    if source.is_symlink():
        raise BackupError(f'Unexpected symlink at {source.name}')
    for base, dirs, files in os.walk(source):
        base = Path(base)
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        dest = target / base.relative_to(source)
        dest.mkdir(parents=True, exist_ok=True)
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file() or name.endswith(('.tmp', '.lock')):
                continue
            copy_regular(path, dest / name)


def capture(source, bundle, previous):
    destination = bundle / 'codex'
    destination.mkdir(parents=True)
    for db in DATABASES:
        snapshot_db(source / db, destination / db)
    for tree in TREES:
        copy_tree(source / tree, destination / tree)
    if (source / 'session_index.jsonl').is_file():
        copy_regular(source / 'session_index.jsonl', destination / 'session_index.jsonl')
    threads = list(thread_rows(source, destination))
    with closing(catalog.readonly(destination / 'state_5.sqlite')) as copied, closing(
            catalog.readonly(source / 'state_5.sqlite')) as live:
        identities = 'SELECT id, archived, rollout_path FROM threads'
        current = {row[0]: row[1:] for row in live.execute(identities)}
        if any(current.get(row[0]) != row[1:] for row in copied.execute(identities)):
            raise BackupError('Archive paths changed during capture')
    if not threads:
        raise BackupError('Source has no persisted chats')
    inventory = {p.relative_to(bundle).as_posix(): digest(p)
                 for p in destination.rglob('*') if p.is_file()}
    search_db = catalog.build(bundle, previous, threads, inventory,
                              project_map(source, {row[0] for row in threads}))
    inventory[search_db.name] = digest(search_db)
    manifest = {'version': 1, 'created': int(time.time()), 'source': str(source),
                'active': sum(not row[4] for row in threads),
                'archived': sum(bool(row[4]) for row in threads), 'files': inventory}
    write_json(bundle / 'manifest.json', manifest)
    return manifest, threads


def remote_manifest(repo, snapshot):
    raw = repo.dump(snapshot, 'manifest.json')
    manifest = json.loads(raw)
    if manifest.get('version') != 1 or 'catalog.sqlite' not in manifest.get('files', {}):
        raise BackupError('Snapshot has no supported manifest')
    for name, entry in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or (name != 'catalog.sqlite' and not name.startswith('codex/')):
            raise BackupError('Invalid path in snapshot manifest')
        if not re.fullmatch('[a-f0-9]{64}', entry['sha256']) or entry['size'] < 0:
            raise BackupError('Invalid file digest in snapshot manifest')
    return manifest


def remote_inventory(repo, snapshot, manifest):
    files = {}
    for line in repo.restic('ls', '--json', snapshot).splitlines():
        node = json.loads(line)
        if node.get('type') == 'file' and node.get('path', '').startswith('/bundle/'):
            files[node['path'][len('/bundle/'):]] = node.get('size')
    expected = {name: item['size'] for name, item in manifest['files'].items()}
    if set(files) != set(expected) | {'manifest.json'} or any(files.get(k) != v for k, v in expected.items()):
        raise BackupError('Remote snapshot inventory differs from the manifest')


def snapshot_id(repo, requested, status):
    if requested:
        if not ID.fullmatch(requested):
            raise BackupError('Provide a snapshot ID')
        return requested
    if status.get('snapshot'):
        return status['snapshot']
    snapshots = json.loads(repo.restic('snapshots', '--json', '--tag', TAG))
    for candidate in sorted(snapshots or [], key=lambda row: row['time'], reverse=True):
        try:
            manifest = remote_manifest(repo, candidate['id'])
            remote_inventory(repo, candidate['id'], manifest)
        except (BackupError, ValueError, KeyError):
            continue
        return candidate['id']
    raise BackupError('No complete snapshot found')


def run_backup(args, repo, status):
    bundle = args.data_dir / 'bundle'
    previous = args.data_dir / ('catalog-' + status.get('snapshot', 'none') + '.sqlite')
    repo.restic('cat', 'config')  # Check connectivity before reading the live store.
    try:
        for attempt in range(3):
            if bundle.exists():
                shutil.rmtree(bundle)
            try:
                manifest, threads = capture(args.source.resolve(), bundle, previous)
                break
            except (OSError, sqlite3.Error, BackupError, subprocess.CalledProcessError) as error:
                if attempt == 2:
                    raise BackupError(f'Capture failed after three attempts: {error}') from error
                time.sleep(2 ** attempt)
        output = repo.restic('backup', '--json', '--host', repo.host, '--tag', TAG, 'bundle',
                             cwd=args.data_dir)
        summary = next(row for row in map(json.loads, output.splitlines()) if row.get('message_type') == 'summary')
        snapshot = summary['snapshot_id']
        if remote_manifest(repo, snapshot) != manifest:
            raise BackupError('Remote manifest differs from the captured batch')
        remote_inventory(repo, snapshot, manifest)
        samples = [next((row[5] for row in threads if bool(row[4]) == archived and
                         (not archived or row[5].endswith('.zst'))), None) for archived in (False, True)]
        for path in filter(None, samples):
            with tempfile.TemporaryFile(dir=args.data_dir) as saved:
                repo.dump(snapshot, path, saved)
                saved.seek(0)
                h = hashlib.sha256()
                for block in iter(lambda: saved.read(1024 * 1024), b''):
                    h.update(block)
                if h.hexdigest() != manifest['files'][path]['sha256']:
                    raise BackupError('Sampled rollout failed remote checksum validation')
        cached = args.data_dir / ('catalog-' + snapshot + '.sqlite')
        shutil.copyfile(bundle / 'catalog.sqlite', cached.with_suffix('.new'))
        cached.with_suffix('.new').replace(cached)
        status.update(snapshot=snapshot, success=int(time.time()), error=None,
                      active=manifest['active'], archived=manifest['archived'],
                      added_bytes=summary.get('data_added_packed', summary.get('data_added')))
        if args.command == 'tick':
            status['scheduled_successes'] = status.get('scheduled_successes', 0) + 1
        write_json(args.data_dir / 'status.json', status)
        for index in args.data_dir.glob('catalog-*.sqlite'):
            if index not in (cached, previous):
                index.unlink()
        print(json.dumps({key: status[key] for key in ('snapshot', 'active', 'archived', 'added_bytes')}))
    finally:
        if bundle.exists():
            shutil.rmtree(bundle)


def restore(repo, snapshot, target, codex_root):
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise BackupError('Restore target must be an empty directory')
    manifest = remote_manifest(repo, snapshot)
    remote_inventory(repo, snapshot, manifest)
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    target.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=target) as temp:
        scratch = Path(temp)
        repo.restic('restore', snapshot + ':/bundle', '--target', str(scratch))
        actual = {p.relative_to(scratch).as_posix() for p in scratch.rglob('*') if p.is_file()}
        if actual != set(manifest['files']) | {'manifest.json'}:
            raise BackupError('Restore contents differ from manifest')
        for name, expected in manifest['files'].items():
            path = scratch / name
            if path.is_symlink() or digest(path) != expected:
                raise BackupError('Restored file failed checksum: ' + name)
        if codex_root:
            with closing(sqlite3.connect(scratch / 'codex/state_5.sqlite')) as db, db:
                entries = db.execute('SELECT id, rollout_path FROM threads').fetchall()
                db.executemany('UPDATE threads SET rollout_path=? WHERE id=?', [
                    (str(codex_root / Path(path).relative_to(manifest['source'])), thread)
                    for thread, path in entries])
            write_json(scratch / 'restore.json', {'snapshot': snapshot, 'codex_root': str(codex_root),
                                                  'verified_before_path_rewrite': True})
        for path in scratch.iterdir():
            path.rename(target / path.name)
    print(f'Restored and verified {len(manifest["files"])} files in {target}')


def recovery_key(args):
    target = CosTarget.load(args.data_dir / 'connection.json')
    if args.command in ('recover-key', 'verify-key'):
        raw = subprocess.check_output(['gpg', '--decrypt', str(args.file)])
        data = json.loads(raw)
        if data.get('repository') != target.repository or not data.get('password'):
            raise BackupError('Recovery package belongs to a different repository')
        recovered = data['password'].encode()
        if args.command == 'verify-key':
            if not secrets.compare_digest(recovered, keychain('repository-password')):
                raise BackupError('Recovered password differs from Keychain')
            print('Recovery package decrypted and matched Keychain')
        else:
            keychain('repository-password', recovered)
        return
    try:
        password = keychain('repository-password')
    except BackupError as error:
        if '(-25300)' not in str(error):
            raise
        password = secrets.token_urlsafe(48).encode()
    if args.file.exists():
        raise BackupError('Recovery package already exists')
    if not re.fullmatch(r'[A-Fa-f0-9]{40,64}', target.gpg_fingerprint) or not target.gpg_public_key_url.startswith('https://'):
        raise BackupError('Configure the full GPG fingerprint and HTTPS public key URL')
    with tempfile.TemporaryDirectory(prefix='gpg-', dir=args.data_dir) as temp:
        public = Path(temp) / 'public.gpg'
        subprocess.run(['curl', '-fsSL', '--max-time', '30', target.gpg_public_key_url, '-o', str(public)], check=True)
        cmd = ['gpg', '--homedir', temp, '--batch']
        listed = subprocess.check_output(cmd + ['--with-colons', '--import-options', 'show-only',
                                                '--dry-run', '--import', str(public)])
        primary = []
        for line in listed.decode().splitlines():
            if line.startswith('pub:'):
                primary.append(None)
            elif line.startswith('fpr:') and primary and primary[-1] is None:
                primary[-1] = line.split(':')[9]
        if primary != [target.gpg_fingerprint.upper()]:
            raise BackupError('Recovery public-key fingerprint differs from the configured fingerprint')
        subprocess.run(cmd + ['--import', str(public)], check=True, capture_output=True)
        payload = json.dumps({'repository': target.repository, 'password': password.decode()}).encode()
        encrypted = subprocess.check_output(cmd + ['--trust-model', 'always', '--recipient',
                                                    target.gpg_fingerprint, '--encrypt'], input=payload)
        with args.file.open('xb') as out:
            out.write(encrypted)
    keychain('repository-password', password)
    print('GPG recovery package written. Store a copy independently of this Mac and the COS bucket.')


def install_agent(data):
    plist = HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
    plist.parent.mkdir(exist_ok=True, mode=0o700)
    payload = {'Label': LABEL, 'ProgramArguments': [sys.executable, str(Path(__file__).resolve()),
                                                   '--data-dir', str(data), 'tick'],
               'RunAtLoad': True, 'StartInterval': 3600,
               'EnvironmentVariables': {'PATH': clean_env()['PATH']},
               'StandardOutPath': str(data / 'launchd.log'),
               'StandardErrorPath': str(data / 'launchd.log')}
    plist.write_bytes(plistlib.dumps(payload))
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)
    print('LaunchAgent loaded')


def schedule(args, status):
    now = int(time.time())
    started = status.setdefault('enabled_since', now)
    write_json(args.data_dir / 'status.json', status)
    try:
        if now - status.get('success', 0) >= DAY:
            run_backup(args, Repository(args), status)
        if now - status.get('weekly_check', 0) >= 7 * DAY:
            Repository(args).restic('check')
            status['weekly_check'] = now
        if now - status.get('full_check', started) >= 90 * DAY:
            Repository(args).restic('check', '--read-data')
            status['full_check'] = now
        if now - status.get('retention_preview', 0) >= 30 * DAY:
            preview = Repository(args).restic('forget', *RETENTION, '--dry-run')
            (args.data_dir / 'retention-preview.txt').write_bytes(preview)
            status['retention_preview'] = now
        status['error'] = None
    except (BackupError, OSError, ValueError, subprocess.CalledProcessError) as error:
        status['error'] = str(error)
        if now - (status.get('success') or started) >= 2 * DAY and now - status.get('alert', 0) >= DAY:
            subprocess.run(['/usr/bin/osascript', '-e',
                'display notification "超过 48 小时没有已核验快照" with title "Codex 会话备份"'],
                capture_output=True)
            status['alert'] = now
        raise
    finally:
        write_json(args.data_dir / 'status.json', status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--data-dir', type=Path, default=DATA)
    parser.add_argument('--local-repo', type=Path, help='Use a filesystem repository for a restore drill')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'probe', 'run', 'tick', 'status', 'install-agent'):
        commands.add_parser(name)
    for name in ('setup-key', 'recover-key', 'verify-key'):
        commands.add_parser(name).add_argument('file', type=Path)
    credential = commands.add_parser('credentials')
    credential.add_argument('--maintenance', action='store_true')
    for name in ('browse', 'restore', 'dump'):
        sub = commands.add_parser(name)
        sub.add_argument('--snapshot')
        if name == 'browse':
            sub.add_argument('--no-open', action='store_true')
        elif name == 'restore':
            sub.add_argument('--to', type=Path, required=True)
            sub.add_argument('--codex-root', type=Path)
        else:
            sub.add_argument('relative')
    commands.add_parser('check').add_argument('--full', action='store_true')
    commands.add_parser('maintain').add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir = args.data_dir.resolve()
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_file = args.data_dir / 'status.json'
    status = read_json(status_file, {})
    if args.command == 'status':
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    if args.command in ('setup-key', 'recover-key', 'verify-key'):
        recovery_key(args)
        return
    if args.command == 'credentials':
        import getpass
        data = {'id': getpass.getpass('COS SecretId: ').strip(),
                'key': getpass.getpass('COS SecretKey: ').strip()}
        if not all(data.values()):
            raise BackupError('Both COS credentials are required')
        keychain('cos-maintenance' if args.maintenance else 'cos-writer', json.dumps(data).encode())
        return
    if args.command == 'install-agent':
        CosTarget.load(args.data_dir / 'connection.json')
        install_agent(args.data_dir)
        return
    if args.command == 'tick':
        with locked(args.data_dir):
            schedule(args, read_json(status_file, {}))
        return
    previous = args.data_dir / ('catalog-' + status.get('snapshot', 'none') + '.sqlite')
    if args.command == 'browse' and not args.snapshot and status.get('snapshot') and previous.is_file():
        with tempfile.TemporaryDirectory(prefix='browse-', dir=args.data_dir) as directory:
            current = Path(directory) / 'catalog.sqlite'
            shutil.copyfile(previous, current)
            catalog.serve(current, status['snapshot'], not args.no_open)
        return

    repo = Repository(args, maintenance=args.command == 'maintain' and args.apply)
    if args.command in ('browse', 'restore', 'dump'):
        selected = snapshot_id(repo, args.snapshot, status)
        if args.command == 'restore':
            restore(repo, selected, args.to.absolute(), args.codex_root.resolve() if args.codex_root else None)
        elif args.command == 'dump':
            if args.relative not in remote_manifest(repo, selected)['files']:
                raise BackupError('File is absent from snapshot manifest')
            repo.dump(selected, args.relative, sys.stdout.buffer)
        else:
            expected = remote_manifest(repo, selected)['files']['catalog.sqlite']
            with tempfile.TemporaryDirectory(prefix='browse-', dir=args.data_dir) as directory:
                index = Path(directory) / 'catalog.sqlite'
                with index.open('xb') as out:
                    repo.dump(selected, 'catalog.sqlite', out)
                if digest(index) != expected:
                    raise BackupError('Search index failed checksum')
                catalog.serve(index, selected, not args.no_open)
        return
    with locked(args.data_dir):
        if args.command in ('init', 'probe'):
            repo.probe()
            if args.command == 'init':
                print(repo.restic('init', '--repository-version', '2').decode().strip())
            else:
                print('Storage probe passed')
        elif args.command == 'run':
            status = read_json(status_file, {})
            try:
                run_backup(args, repo, status)
            except Exception as error:
                status['error'] = str(error)
                write_json(status_file, status)
                raise
        elif args.command == 'check':
            flags = ('--read-data',) if args.full else ()
            print(repo.restic('check', *flags).decode().strip())
        elif args.command == 'maintain':
            if args.apply:
                checked = json.loads(repo.restic('forget', *RETENTION, '--dry-run', '--json')) or []
                removals = {entry['id'] for group in checked for entry in group.get('remove') or []}
                if read_json(status_file, {}).get('snapshot') in removals:
                    raise BackupError('Retention would remove the last locally verified snapshot')
            flags = ('--prune',) if args.apply else ('--dry-run',)
            print(repo.restic('forget', *RETENTION, *flags).decode().strip())


if __name__ == '__main__':
    try:
        main()
    except (BackupError, ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f'codex-backup: {error}', file=sys.stderr)
        sys.exit(1)
