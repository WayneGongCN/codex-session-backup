"""Exercise two encrypted snapshots and restore with a synthetic source."""
import hashlib
import json
import os
import secrets
import signal
import sqlite3
import subprocess
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


class BackupRoundTrip(unittest.TestCase):
    def test_two_snapshots_and_recovery(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            source, repository, data = [root / name for name in ('source', 'repo', 'data')]
            (source / 'sessions').mkdir(parents=True)
            (source / 'archived_sessions').mkdir()
            active = source / 'sessions/active.jsonl'
            archived = source / 'archived_sessions/archive.jsonl.zst'
            active.write_text('{"type":"synthetic"}\n')
            archived.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=b'{"type":"synthetic"}\n'))
            attached = source / 'attachments/upload/附件 & notes.txt'
            attached.parent.mkdir(parents=True)
            attached.write_bytes(b'first attachment')
            unlisted = attached.with_suffix('.txt.bak')
            unlisted.write_bytes(b'unlisted')
            visualization = source / 'visualizations/2026/11/12/old/result.html'
            visualization.parent.mkdir(parents=True)
            visualization.write_bytes(b'<html>archived artifact</html>')
            (source / 'auth.json').write_text('excluded-token')
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute("""CREATE TABLE threads(id,title,first_user_message,cwd,updated_at,archived,rollout_path)""")
                db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                    ('first', '普通', '', '/demo', 1700000000, 0, str(active)),
                    ('old', '旧会话', '', '/demo', 1700000000, 1, str(archived.with_suffix('')))])
            with closing(sqlite3.connect(source / 'thread_history_1.sqlite')) as db, db:
                db.execute('CREATE TABLE thread_history_projection_state(thread_id,next_rollout_byte_offset,next_rollout_ordinal)')
                db.execute('CREATE TABLE thread_items(thread_id,item_id,item_type,item_json,rollout_ordinal)')
                for chat, body in [('first', f'网络请求失败 ABC-123 [附件]({attached.as_uri()})'),
                                   ('old', '已归档关键词')]:
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
            with closing(catalog.readonly(data / ('catalog-' + first['snapshot'] + '.sqlite'))) as db:
                for query in ('网络', '请求失败', 'ABC-123'):
                    self.assertEqual(catalog.results(db, {'q': [query]})[1][0][0], 'first')
                self.assertEqual(catalog.results(db, {'q': ['归档关键词'], 'archived': ['1']})[1][0][0], 'old')
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute("DELETE FROM threads WHERE id='old'")
            archived.unlink()
            attached.write_bytes(b'updated attachment')
            second = json.loads(run('run'))
            self.assertNotEqual(first['snapshot'], second['snapshot'])
            self.assertEqual(second['archived'], 0)
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
                with urlopen(download_url(url, 'old', visualization_path), timeout=10) as file:
                    self.assertEqual(file.read(), b'<html>archived artifact</html>')
                for chat, path in [('old', attachment_path), ('first', 'codex/state_5.sqlite'),
                                   ('first', attachment_path + '.bak'), ('first', '../manifest.json')]:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(download_url(url, chat, path), timeout=10)
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
            with browsing() as url:
                with urlopen(download_url(url, 'first', attachment_path), timeout=10) as file:
                    self.assertEqual(file.read(), b'updated attachment')
            restored = root / 'restored'
            run('restore', '--snapshot', first['snapshot'], '--to', str(restored),
                '--codex-root', str(restored / 'codex'))
            restored_archive = restored / 'codex/archived_sessions/archive.jsonl.zst'
            self.assertTrue(restored_archive.is_file())
            self.assertFalse((restored / 'codex/auth.json').exists())
            with closing(catalog.readonly(restored / 'catalog.sqlite')) as db:
                self.assertEqual(catalog.results(db, {'q': ['归档关键词']})[1][0][0], 'old')
            with closing(catalog.readonly(restored / 'codex/state_5.sqlite')) as db:
                path = db.execute("SELECT rollout_path FROM threads WHERE id='old'").fetchone()[0]
                self.assertEqual(path + '.zst', str(restored_archive))
            self.assertIn('no errors', run('check', '--full'))
            repository.rename(root / 'unavailable')
            with browsing() as url:
                with urlopen(url + '/chat?id=first', timeout=10) as page:
                    self.assertIn('暂时无法读取快照附件', page.read().decode())
                with urlopen(url + '/export?id=first', timeout=10) as page:
                    self.assertIn('ABC-123', page.read().decode())
            failed = subprocess.run(cli + ['run'], env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(run('status'))['snapshot'], second['snapshot'])
            (root / 'unavailable').rename(repository)
            run('check')


if __name__ == '__main__':
    unittest.main()
