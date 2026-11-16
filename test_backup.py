"""Exercise encrypted snapshots and restore with synthetic sources."""
import hashlib
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen
from unittest.mock import patch

import backup
import catalog


class RecoveryPackage(unittest.TestCase):
    def test_verify_package_from_previous_backend(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            settings = {'bucket': 'test-123', 'prefix': 'backup/v1',
                        'region': 'ap-guangzhou', 'endpoint': 'cos-internal.ap-guangzhou.tencentcos.cn'}
            (root / 'connection.json').write_text(json.dumps(settings))
            args = Namespace(command='verify-key', data_dir=root, file=root / 'recovery.gpg')
            old = {'repository': 'rclone:codexcos:test-123/backup/v1', 'password': 'test-password'}
            with patch.object(backup.subprocess, 'check_output') as decrypt, \
                    patch.object(backup, 'keychain', return_value=b'test-password'), patch('builtins.print'):
                decrypt.return_value = json.dumps(old).encode()
                backup.recovery_key(args)
                old['repository'] = 'rclone:codexcos:test-123/another-prefix'
                decrypt.return_value = json.dumps(old).encode()
                with self.assertRaisesRegex(backup.BackupError, 'different repository'):
                    backup.recovery_key(args)


class DatabaseCapture(unittest.TestCase):
    def test_idle_wal_database_without_sidecars(self):
        with tempfile.TemporaryDirectory() as work:
            source, target = (Path(work) / name for name in ('source.sqlite', 'copy.sqlite'))
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE data(value)')
                db.execute('INSERT INTO data VALUES (42)')
                db.commit()
                self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(), (0, 0, 0))
            wal = source.with_name(source.name + '-wal')
            if wal.exists():
                self.assertEqual(wal.stat().st_size, 0)
                wal.unlink()
            source.with_name(source.name + '-shm').unlink(missing_ok=True)
            with patch.object(catalog, 'readonly', side_effect=sqlite3.OperationalError('unable to open database file')):
                backup.snapshot_db(source, target)
                wal.touch()
                with self.assertRaises(sqlite3.OperationalError):
                    backup.snapshot_db(source, target)
            with closing(catalog.readonly(target)) as db:
                self.assertEqual(db.execute('SELECT value FROM data').fetchone(), (42,))
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone(), ('delete',))


class BackupRoundTrip(unittest.TestCase):
    def test_two_snapshots_and_recovery(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            source, repository, data = [root / name for name in ('source', 'repo', 'data')]
            (source / 'sessions').mkdir(parents=True)
            (source / 'archived_sessions').mkdir()
            active = source / 'sessions/active.jsonl'
            archived = source / 'archived_sessions/archive.jsonl.zst'
            archived_id = '01a1d534-0b11-7000-8000-000000000001'
            active.write_text('{"type":"synthetic"}\n')
            archived.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=b'{"type":"synthetic"}\n'))
            attached = source / 'attachments/upload/附件 & notes.txt'
            attached.parent.mkdir(parents=True)
            attached.write_bytes(b'first attachment')
            unlisted = attached.with_suffix('.txt.bak')
            unlisted.write_bytes(b'unlisted')
            visualization = source / 'visualizations/2026/11/12' / archived_id / 'result.html'
            visualization.parent.mkdir(parents=True)
            visualization.write_bytes(b'<html>archived artifact</html>')
            (source / 'auth.json').write_text('excluded-token')
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute("""CREATE TABLE threads(id,title,first_user_message,cwd,updated_at,archived,rollout_path)""")
                db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                    ('first', '普通', '', '/demo', 1700000000, 0, str(active)),
                    (archived_id, '旧会话', '', '/demo', 1700000000, 1, str(archived.with_suffix('')))])
            with closing(sqlite3.connect(source / 'thread_history_1.sqlite')) as db, db:
                db.execute('CREATE TABLE thread_history_projection_state(thread_id,next_rollout_byte_offset,next_rollout_ordinal)')
                db.execute('CREATE TABLE thread_items(thread_id,item_id,item_type,item_json,rollout_ordinal)')
                for chat, body in [('first', f'网络请求失败 ABC-123 [附件]({attached.as_uri()})'),
                                   (archived_id, '已归档关键词')]:
                    db.execute('INSERT INTO thread_history_projection_state VALUES (?,0,1)', (chat,))
                    db.execute('INSERT INTO thread_items VALUES (?,1,?,?,1)',
                               (chat, 'agentMessage', json.dumps({'text': body})))
            with closing(sqlite3.connect(source / 'goals_1.sqlite')) as db:
                db.execute('CREATE TABLE thread_goals(id)')
            cli = [str(Path(backup.__file__).with_name('codex-backup')),
                   '--source', str(source), '--local-repo', str(repository), '--data-dir', str(data)]
            env = {**os.environ, 'CODEX_BACKUP_TEST_PASSWORD': secrets.token_urlsafe(32)}

            def run(*args):
                return subprocess.check_output(cli + list(args), env=env, text=True)

            @contextmanager
            def browsing(*args):
                with subprocess.Popen(cli + ['browse', '--no-open', *args], env=env, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        url = process.stdout.readline().strip()
                        self.assertTrue(url.startswith('http://127.0.0.1:'))
                        yield url
                    finally:
                        if process.poll() is None:
                            process.send_signal(signal.SIGINT)
                        process.wait(timeout=10)

            def download_url(url, chat, path):
                return url + '/download?' + urlencode({'id': chat, 'path': path})

            run('init')
            first = json.loads(run('run'))
            documents = root / 'Documents' / 'Codex [demo]'
            report = documents / '2026-11-12' / '报告 & notes.html'
            bare_head = documents / 'archive.git' / 'HEAD'
            excluded = ('node_modules/pkg/index.js', '.venv/bin/python', '2026-11-12/.cache/data',
                        '2026-11-12/.DS_Store', '2026-11-12/checkout/.git/HEAD',
                        '2026-11-12/checkout/inside.md', '2026-11-12/tree [linked]*?/.git',
                        '2026-11-12/tree [linked]*?/inside.md')
            for path in (report, bare_head, *(documents / name for name in excluded)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic')
            (documents / 'empty').mkdir()
            (documents / 'outside').symlink_to(active)
            (data / 'connection.json').write_text(json.dumps({'documents_source': str(documents)}))
            with closing(catalog.readonly(data / ('catalog-' + first['snapshot'] + '.sqlite'))) as db:
                for query in ('网络', '请求失败', 'ABC-123'):
                    self.assertEqual(catalog.results(db, {'q': [query]})[1][0][0], 'first')
                self.assertEqual(catalog.results(db, {'q': ['归档关键词'], 'archived': ['1']})[1][0][0], archived_id)
            with browsing() as url:
                with urlopen(url + '/snapshots', timeout=10) as page:
                    self.assertIn(first['snapshot'], page.read().decode())
                with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                    db.execute('DELETE FROM threads WHERE id=?', (archived_id,))
                archived.unlink()
                attached.write_bytes(b'updated attachment')
                second = json.loads(run('tick'))
                with urlopen(url + '/snapshots?q=ABC-123&page=5', timeout=10) as page:
                    listing = page.read().decode()
                first_link = 'snapshot=' + first['snapshot']
                second_link = 'snapshot=' + second['snapshot']
                self.assertLess(listing.index(second_link), listing.index(first_link + '&amp;q=ABC-123"'))
                self.assertIn(second_link + '&amp;q=ABC-123"', listing)
                self.assertIn(first_link + '&amp;q=ABC-123" aria-current="true"', listing)
                self.assertIn('UTC+8', listing)
                self.assertIn('codex-mac', listing)
                query = urlencode({'snapshot': second['snapshot'], 'q': '归档关键词'})
                with urlopen(url + '?' + query, timeout=10) as page:
                    self.assertIn('没有匹配的会话', page.read().decode())
                with urlopen(url + '?' + urlencode({'q': '归档关键词'}), timeout=10) as page:
                    results = page.read().decode()
                    self.assertIn('旧会话', results)
                    self.assertIn(f'href="codex://threads/{archived_id}"', results)
                    self.assertIn('在 Codex 中打开', results)
                    self.assertIn(f'&amp;id={archived_id}"', results)
            self.assertNotEqual(first['snapshot'], second['snapshot'])
            self.assertEqual(second['archived'], 0)
            self.assertEqual(second['documents'], {'files': 2, 'bytes': 18})
            self.assertEqual(json.loads(run('status'))['scheduled_successes'], 1)
            snapshots = run('probe')
            self.assertIn(first['snapshot'][:8], snapshots)
            self.assertIn(second['snapshot'][:8], snapshots)
            with closing(catalog.readonly(data / ('catalog-' + second['snapshot'] + '.sqlite'))) as db:
                self.assertFalse(catalog.results(db, {'q': ['归档关键词']})[1])
            attachment_path = 'codex/' + attached.relative_to(source).as_posix()
            visualization_path = 'codex/' + visualization.relative_to(source).as_posix()
            with browsing('--snapshot', first['snapshot']) as url:
                with urlopen(url + '/chat?id=first', timeout=10) as page:
                    detail = page.read().decode()
                    self.assertIn('附件 &amp; notes.txt', detail)
                    self.assertIn('download aria-label', detail)
                    self.assertNotIn(quote(visualization_path, safe=''), detail)
                with urlopen(download_url(url, 'first', attachment_path), timeout=10) as file:
                    self.assertEqual(file.read(), b'first attachment')
                    self.assertEqual(file.headers.get_content_type(), 'application/octet-stream')
                    self.assertIn("filename*=UTF-8''" + quote(attached.name), file.headers['Content-Disposition'])
                with urlopen(download_url(url, archived_id, visualization_path), timeout=10) as file:
                    self.assertEqual(file.read(), b'<html>archived artifact</html>')
                for chat, path in [(archived_id, attachment_path), ('first', 'codex/state_5.sqlite'),
                                   ('first', attachment_path + '.bak'), ('first', '../manifest.json')]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(download_url(url, chat, path), timeout=10)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
            with browsing() as url:
                with urlopen(download_url(url, 'first', attachment_path), timeout=10) as file:
                    self.assertEqual(file.read(), b'updated attachment')
                (data / ('catalog-' + first['snapshot'] + '.sqlite')).unlink()
                with urlopen(url + '?' + first_link + '&q=ABC-123', timeout=10) as page:
                    historical = page.read().decode()
                    self.assertIn('name="snapshot" value="' + first['snapshot'] + '"', historical)
                    self.assertIn('/chat?' + first_link + '&amp;q=ABC-123&amp;id=first', historical)
                    self.assertNotIn('codex://threads/', historical)
                with urlopen(url + '/chat?' + first_link + '&q=ABC-123&id=first', timeout=10) as page:
                    historical = page.read().decode()
                    self.assertIn(first_link + '&amp;q=ABC-123', historical)
                    self.assertIn('/export?' + first_link + '&amp;id=first', historical)
                    self.assertIn('/download?' + first_link + '&amp;id=first', historical)
                with urlopen(url + '/export?' + first_link + f'&id={archived_id}', timeout=10) as page:
                    self.assertIn('已归档关键词', page.read().decode())
                with urlopen(download_url(url, 'first', attachment_path) + '&' + first_link, timeout=10) as file:
                    self.assertEqual(file.read(), b'first attachment')
                with self.assertRaises(HTTPError) as error:
                    urlopen(url + '?snapshot=latest', timeout=10)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            restored = root / 'restored'
            run('restore', '--snapshot', first['snapshot'], '--to', str(restored),
                '--codex-root', str(restored / 'codex'))
            restored_archive = restored / 'codex/archived_sessions/archive.jsonl.zst'
            self.assertTrue(restored_archive.is_file())
            self.assertFalse((restored / 'codex/auth.json').exists())
            self.assertFalse((restored / 'documents').exists())
            with closing(catalog.readonly(restored / 'catalog.sqlite')) as db:
                self.assertEqual(catalog.results(db, {'q': ['归档关键词']})[1][0][0], archived_id)
            with closing(catalog.readonly(restored / 'codex/state_5.sqlite')) as db:
                path = db.execute('SELECT rollout_path FROM threads WHERE id=?', (archived_id,)).fetchone()[0]
                self.assertEqual(path + '.zst', str(restored_archive))
            self.assertIn('no errors', run('check', '--full'))
            repository.rename(root / 'unavailable')
            with browsing() as url:
                with self.assertRaises(HTTPError) as error:
                    urlopen(url + '/snapshots', timeout=10)
                with error.exception as page:
                    self.assertEqual(page.code, 503)
                    self.assertIn('暂时无法读取快照列表', page.read().decode())
                with urlopen(url + '/chat?id=first', timeout=10) as page:
                    self.assertIn('暂时无法读取快照附件', page.read().decode())
                with urlopen(url + '/export?id=first', timeout=10) as page:
                    self.assertIn('ABC-123', page.read().decode())
            failed = subprocess.run(cli + ['run'], env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(run('status'))['snapshot'], second['snapshot'])
            (root / 'unavailable').rename(repository)

            joint = root / 'joint'
            run('restore', '--snapshot', second['snapshot'], '--to', str(joint))
            copied = joint / 'documents/Codex'
            self.assertEqual((copied / report.relative_to(documents)).read_bytes(), b'synthetic')
            self.assertEqual((copied / bare_head.relative_to(documents)).read_bytes(), b'synthetic')
            self.assertTrue((copied / 'empty').is_dir())
            self.assertEqual((copied / 'outside').readlink(), active)
            self.assertFalse((copied / '2026-11-12/checkout').exists())
            self.assertFalse((copied / '2026-11-12/tree [linked]*?').exists())
            self.assertFalse((copied / 'node_modules').exists())
            self.assertEqual((joint / attachment_path).read_bytes(), b'updated attachment')

            report.write_bytes(b'next version')
            third = json.loads(run('run'))
            self.assertNotEqual(second['snapshot'], third['snapshot'])
            self.assertEqual(third['documents'], {'files': 2, 'bytes': 21})
            self.assertIn('keep', run('maintain'))

            # Save a real snapshot missing a source file and simulate restic's exit status 3.
            real_run = subprocess.run
            partial = {}

            def incomplete(command, *args, **kwargs):
                if command[0] == 'restic' and 'backup' in command:
                    command = [*command, '--exclude=' + backup.documents.escape_pattern(report)]
                    result = real_run(command, *args, **kwargs)
                    self.assertEqual(result.returncode, 0)
                    summary = next(row for row in map(json.loads, result.stdout.splitlines())
                                   if row.get('message_type') == 'summary')
                    partial['snapshot'] = summary['snapshot_id']
                    return subprocess.CompletedProcess(command, 3, result.stdout, b'source read error')
                return real_run(command, *args, **kwargs)

            with patch.dict(os.environ, env), patch.object(sys, 'argv', [*cli, 'run']), \
                    patch.object(backup.subprocess, 'run', side_effect=incomplete):
                with self.assertRaisesRegex(backup.BackupError, 'restic backup exited 3'):
                    backup.main()
            self.assertEqual(json.loads(run('status'))['snapshot'], third['snapshot'])

            documents.rename(root / 'missing-documents')
            failed = subprocess.run(cli + ['run'], env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(run('status'))['snapshot'], third['snapshot'])

            fresh_cli = [*cli[:-1], str(root / 'fresh-data')]
            rejected = root / 'rejected'
            failed = subprocess.run(fresh_cli + ['restore', '--snapshot', partial['snapshot'],
                                                 '--to', str(rejected)], env=env, capture_output=True)
            self.assertIn(b'Remote documents inventory differs', failed.stderr)
            self.assertFalse(rejected.exists())
            fresh = root / 'fresh'
            subprocess.check_output(fresh_cli + ['restore', '--to', str(fresh)], env=env)
            self.assertEqual((fresh / 'documents/Codex' / report.relative_to(documents)).read_bytes(),
                             b'next version')
            run('check')


if __name__ == '__main__':
    unittest.main()
