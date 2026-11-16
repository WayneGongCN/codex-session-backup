#!/usr/bin/env python3
"""Encrypted, incremental backup of Codex chats and documents."""
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
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from threading import Lock

import catalog
import documents
from configuration import CosTarget, DATA, documents_path

HOME = Path.home()
SOURCE = HOME / '.codex'
KEYCHAIN = b'com.waynegong.codex-session-backup'
DATABASES = ('state_5.sqlite', 'thread_history_1.sqlite', 'goals_1.sqlite')
TREES = ('sessions', 'archived_sessions', *catalog.ATTACHMENT_TREES)
TAG = 'codex-sessions-v1'
DOCUMENTS_TAG = 'codex-documents-v1'
RETENTION = ('--tag', TAG, '--keep-last', '2', '--keep-daily', '30',
             '--keep-weekly', '12', '--keep-monthly', '12')
DAY = 86400
LABEL = 'com.waynegong.codex-session-backup'


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
    with path.open('rb') as stream:
        return stream_digest(stream)


def stream_digest(stream):
    h = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
        size += len(block)
    return {'size': size, 'sha256': h.hexdigest()}


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(('RESTIC_', 'AWS_'))}
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    return env


class Repository:
    def __init__(self, args, maintenance=False):
        self.data = args.data_dir
        self.local = args.local_repo
        self.env = clean_env()
        self.env['RESTIC_CACHE_DIR'] = str(self.data / 'restic-cache')
        self.host = 'codex-mac'
        connection = self.data / 'connection.json'
        if self.local:
            self.url = str(self.local.resolve())
            self.documents_source = documents_path(read_json(connection, {}))
            self.env['RESTIC_PASSWORD'] = os.environ.get('CODEX_BACKUP_TEST_PASSWORD', '')
            if not self.env['RESTIC_PASSWORD']:
                raise BackupError('Set CODEX_BACKUP_TEST_PASSWORD for the local repository')
            return
        self.target = CosTarget.load(connection)
        self.documents_source = self.target.documents_source
        self.url = self.target.repository
        self.host = self.target.backup_host
        self.env['RESTIC_PASSWORD'] = keychain('repository-password').decode()
        credentials = json.loads(keychain('cos-maintenance' if maintenance else 'cos-writer'))
        self.env.update({
            'AWS_ACCESS_KEY_ID': credentials['id'],
            'AWS_SECRET_ACCESS_KEY': credentials['key'],
            'AWS_DEFAULT_REGION': self.target.region,
            'NO_PROXY': self.target.endpoint + ',.' + self.target.endpoint,
            'no_proxy': self.target.endpoint + ',.' + self.target.endpoint,
        })

    def restic(self, *args, cwd=None, output=subprocess.PIPE):
        # COS and the scoped CAM policy use the bucket's virtual-hosted domain.
        options = () if self.local else ('-o', 's3.bucket-lookup=dns')
        result = subprocess.run(['restic', '-r', self.url, *options, *args],
            env=self.env, cwd=cwd, stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            detail = result.stderr.decode(errors='replace').strip().splitlines()
            message = detail[-1][:500] if detail else ''
            raise BackupError(f'restic {args[0]} exited {result.returncode}. {message}')
        return result.stdout

    def dump(self, snapshot, relative, output=subprocess.PIPE):
        return self.restic('dump', snapshot, '/bundle/' + relative, output=output)


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

    try:
        with closing(catalog.readonly(source)) as db, closing(sqlite3.connect(target)) as clone:
            db.backup(clone, pages=256, progress=progress, sleep=0.05)
    except sqlite3.OperationalError as error:
        if str(error) != 'unable to open database file':
            raise
        # An idle WAL database can lack the sidecars needed by a read-only connection.
        # Copy only a stable main file with no pending WAL, then open the copy locally.
        def file_version():
            info = source.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        wal = source.with_name(source.name + '-wal')
        before = file_version()
        with source.open('rb') as stream:
            header = stream.read(20)
        if header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x02\x02' or wal.is_symlink() or wal.exists():
            raise
        target.unlink(missing_ok=True)
        copy_regular(source, target)
        if wal.is_symlink() or wal.exists() or file_version() != before:
            raise BackupError(f'SQLite database changed during capture: {source.name}') from error

    with closing(sqlite3.connect(target)) as clone:
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
    if manifest.get('version') not in (1, 2) or 'catalog.sqlite' not in manifest.get('files', {}):
        raise BackupError('Snapshot has no supported manifest')
    for name, entry in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or (name != 'catalog.sqlite' and not name.startswith('codex/')):
            raise BackupError('Invalid path in snapshot manifest')
        if not re.fullmatch('[a-f0-9]{64}', entry['sha256']) or entry['size'] < 0:
            raise BackupError('Invalid file digest in snapshot manifest')
    if manifest['version'] == 2:
        tree = manifest.get('documents')
        if (not isinstance(tree, dict)
                or any(type(tree.get(key)) is not int or tree[key] < 0 for key in ('files', 'bytes'))
                or not re.fullmatch('[a-f0-9]{64}', tree.get('inventory_sha256', ''))):
            raise BackupError('Invalid documents inventory in snapshot manifest')
        documents.absolute_tree(tree.get('path'))
    return manifest


def remote_inventory(repo, snapshot, manifest):
    files = {}
    for line in repo.restic('ls', '--json', '--recursive', snapshot, '/bundle').splitlines():
        node = json.loads(line)
        if node.get('type') == 'file' and node.get('path', '').startswith('/bundle/'):
            files[node['path'][len('/bundle/'):]] = node.get('size')
    expected = {name: item['size'] for name, item in manifest['files'].items()}
    if set(files) != set(expected) | {'manifest.json'} or any(files.get(k) != v for k, v in expected.items()):
        raise BackupError('Remote snapshot inventory differs from the manifest')
    if manifest['version'] == 2:
        tree = manifest['documents']
        root = Path(tree['path'])
        with tempfile.TemporaryFile(dir=repo.data) as listing:
            repo.restic('ls', '--json', '--recursive', snapshot, str(root), output=listing)
            listing.seek(0)
            actual = documents.inventory(documents.snapshot_entries(listing, root))
        if {'path': str(root), **actual} != tree:
            raise BackupError('Remote documents inventory differs from the manifest')


def repository_snapshots(repo):
    rows = json.loads(repo.restic('snapshots', '--json', '--no-lock', '--tag', TAG))
    for row in rows or []:
        # restic can emit nanoseconds, while Python 3.9 accepts six fractional digits.
        stamp = re.sub(r'\.(\d+)', lambda match: '.' + match[1][:6].ljust(6, '0'), row['time'])
        row['time'] = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return sorted(rows or [], key=lambda row: row['time'], reverse=True)


def snapshot_id(repository, requested, status):
    selected = requested or status.get('snapshot')
    if selected:
        if not catalog.SNAPSHOT_ID.fullmatch(selected):
            raise BackupError('Provide a snapshot ID')
        return selected
    repo = repository()
    for candidate in repository_snapshots(repo):
        try:
            manifest = remote_manifest(repo, candidate['id'])
            remote_inventory(repo, candidate['id'], manifest)
        except (BackupError, ValueError, KeyError, TypeError):
            continue
        return candidate['id']
    raise BackupError('No complete snapshot found')


def browse(args, status):
    @lru_cache(maxsize=1)
    def repository():
        return Repository(args)

    @lru_cache(maxsize=1)
    def manifest(selected):
        return remote_manifest(repository(), selected)

    def list_snapshots():
        try:
            return repository_snapshots(repository())
        except (BackupError, OSError, ValueError, KeyError, TypeError) as error:
            raise catalog.SnapshotError from error

    def load_manifest(selected):
        try:
            value = manifest(selected)
            if not Path(value['source']).is_absolute():
                raise BackupError('Invalid source in snapshot manifest')
            return value
        except (BackupError, OSError, ValueError, KeyError, TypeError) as error:
            raise catalog.AttachmentError from error

    @contextmanager
    def download_file(selected, path, expected):
        with ExitStack() as stack:
            try:
                output = stack.enter_context(tempfile.TemporaryFile(dir=args.data_dir))
                repository().dump(selected, path, output)
                output.seek(0)
                if stream_digest(output) != expected:
                    raise BackupError('Attachment failed checksum')
                output.seek(0)
            except (BackupError, OSError, ValueError) as error:
                raise catalog.AttachmentError from error
            yield output

    with tempfile.TemporaryDirectory(prefix='browse-', dir=args.data_dir) as directory:
        index_lock = Lock()

        def load_index(selected):
            index = Path(directory) / f'catalog-{selected}.sqlite'
            if index.is_file():
                return index
            with index_lock:
                if index.is_file():
                    return index
                cached = args.data_dir / index.name
                try:
                    # Keep the immutable index available even when a later backup prunes the cache.
                    os.link(cached, index)
                    return index
                except FileNotFoundError:
                    pass
                staging = index.with_suffix('.new')
                try:
                    expected = manifest(selected)['files']['catalog.sqlite']
                    with staging.open('xb') as out:
                        repository().dump(selected, 'catalog.sqlite', out)
                    if digest(staging) != expected:
                        raise BackupError('Search index failed checksum')
                    staging.replace(index)
                finally:
                    staging.unlink(missing_ok=True)
                return index

        def open_index(selected):
            try:
                return load_index(selected)
            except (BackupError, OSError, ValueError, KeyError, TypeError) as error:
                raise catalog.SnapshotError from error

        selected = snapshot_id(repository, args.snapshot, status)
        load_index(selected)
        catalog.serve(selected, not args.no_open, list_snapshots=list_snapshots, open_index=open_index,
                      load_manifest=load_manifest, download_file=download_file)


def documents_source(repo, session_source):
    configured = repo.documents_source
    if configured is None:
        return None
    if configured.is_symlink():
        raise BackupError('Documents source must be a directory, not a symlink')
    root = documents.absolute_tree(str(configured.resolve(strict=True)))
    for other in (repo.data, session_source, repo.local.resolve() if repo.local else None):
        if other is not None and (root == other or root in other.parents or other in root.parents):
            raise BackupError('Documents source overlaps session data or the backup repository')
    return root


def run_backup(args, repo, status):
    bundle = args.data_dir / 'bundle'
    previous = args.data_dir / ('catalog-' + status.get('snapshot', 'none') + '.sqlite')
    repo.restic('cat', 'config')  # Check connectivity before reading the live store.
    session_source = args.source.resolve()
    tree_source = documents_source(repo, session_source)
    try:
        for attempt in range(3):
            if bundle.exists():
                shutil.rmtree(bundle)
            try:
                manifest, threads = capture(session_source, bundle, previous)
                break
            except (OSError, sqlite3.Error, BackupError, subprocess.CalledProcessError) as error:
                if attempt == 2:
                    raise BackupError(f'Capture failed after three attempts: {error}') from error
                time.sleep(2 ** attempt)
        filters = []
        sources = ['bundle']
        tag = TAG
        if tree_source is not None:
            tree = {'path': str(tree_source), **documents.scan(tree_source, exclude=filters)}
            manifest.update(version=2, documents=tree)
            write_json(bundle / 'manifest.json', manifest)
            sources.append(str(tree_source))
            tag += ',' + DOCUMENTS_TAG
        output = repo.restic('backup', '--json', '--host', repo.host, '--tag', tag,
                             *(f'--exclude={pattern}' for pattern in filters), *sources, cwd=args.data_dir)
        summary = next((row for row in map(json.loads, output.splitlines())
                        if row.get('message_type') == 'summary'), {})
        if not summary.get('snapshot_id'):
            raise BackupError('restic backup did not return a snapshot ID')
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
        fields = ['snapshot', 'active', 'archived', 'added_bytes']
        if tree_source is not None:
            status['documents'] = {key: manifest['documents'][key] for key in ('files', 'bytes')}
            fields.append('documents')
        else:
            status.pop('documents', None)
        if args.command == 'tick':
            status['scheduled_successes'] = status.get('scheduled_successes', 0) + 1
        write_json(args.data_dir / 'status.json', status)
        for index in args.data_dir.glob('catalog-*.sqlite'):
            if index not in (cached, previous):
                index.unlink()
        print(json.dumps({key: status[key] for key in fields}))
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
        if manifest['version'] == 2:
            tree = manifest['documents']
            destination = scratch / 'documents' / 'Codex'
            destination.mkdir(parents=True)
            repo.restic('restore', snapshot + ':' + tree['path'], '--verify', '--target', str(destination))
            if {'path': tree['path'], **documents.scan(destination)} != tree:
                raise BackupError('Restored documents inventory differs from the manifest')
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
    count = len(manifest['files'])
    if manifest['version'] == 2:
        count += manifest['documents']['files']
    print(f'Restored and verified {count} files in {target}')


def recovery_key(args):
    target = CosTarget.load(args.data_dir / 'connection.json')
    if args.command in ('recover-key', 'verify-key'):
        raw = subprocess.check_output(['gpg', '--decrypt', str(args.file)])
        data = json.loads(raw)
        # Older recovery packages identified the same COS repository by its transport URI.
        repositories = (target.repository_path, target.repository, 'rclone:codexcos:' + target.repository_path)
        if data.get('repository') not in repositories or not data.get('password'):
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
        payload = json.dumps({'repository': target.repository_path, 'password': password.decode()}).encode()
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
               'StartCalendarInterval': {'Hour': 10, 'Minute': 0},
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
    if args.command in ('run', 'tick'):
        with locked(args.data_dir):
            status = read_json(status_file, {})
            if args.command == 'tick':
                schedule(args, status)
            else:
                try:
                    run_backup(args, Repository(args), status)
                except Exception as error:
                    status['error'] = str(error)
                    write_json(status_file, status)
                    raise
        return
    if args.command == 'browse':
        browse(args, status)
        return

    repo = Repository(args, maintenance=args.command == 'maintain' and args.apply)
    if args.command in ('restore', 'dump'):
        selected = snapshot_id(lambda: repo, args.snapshot, status)
        if args.command == 'restore':
            restore(repo, selected, args.to.absolute(), args.codex_root.resolve() if args.codex_root else None)
        else:
            if args.relative not in remote_manifest(repo, selected)['files']:
                raise BackupError('File is absent from snapshot manifest')
            repo.dump(selected, args.relative, sys.stdout.buffer)
        return
    with locked(args.data_dir):
        if args.command == 'init':
            print(repo.restic('init', '--repository-version', '2').decode().strip())
        elif args.command == 'probe':
            print(repo.restic('snapshots', '--no-lock').decode().strip())
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
