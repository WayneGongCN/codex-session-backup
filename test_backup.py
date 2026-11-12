"""Exercise two encrypted snapshots and restore with a synthetic source."""
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import backup
import catalog


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
            (source / 'auth.json').write_text('excluded-token')
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute("""CREATE TABLE threads(id,title,first_user_message,cwd,updated_at,archived,rollout_path)""")
                db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                    ('first', '普通', '', '/demo', 1700000000, 0, str(active)),
                    ('old', '旧会话', '', '/demo', 1700000000, 1, str(archived.with_suffix('')))])
            with closing(sqlite3.connect(source / 'thread_history_1.sqlite')) as db, db:
                db.execute('CREATE TABLE thread_history_projection_state(thread_id,next_rollout_byte_offset,next_rollout_ordinal)')
                db.execute('CREATE TABLE thread_items(thread_id,item_id,item_type,item_json,rollout_ordinal)')
                for chat, body in [('first', '网络请求失败 ABC-123'), ('old', '已归档关键词')]:
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

            run('init')
            first = json.loads(run('run'))
            with closing(catalog.readonly(data / ('catalog-' + first['snapshot'] + '.sqlite'))) as db:
                for query in ('网络', '请求失败', 'ABC-123'):
                    self.assertEqual(catalog.results(db, {'q': [query]})[1][0][0], 'first')
                self.assertEqual(catalog.results(db, {'q': ['归档关键词'], 'archived': ['1']})[1][0][0], 'old')
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute("DELETE FROM threads WHERE id='old'")
            archived.unlink()
            second = json.loads(run('run'))
            self.assertNotEqual(first['snapshot'], second['snapshot'])
            self.assertEqual(second['archived'], 0)
            with closing(catalog.readonly(data / ('catalog-' + second['snapshot'] + '.sqlite'))) as db:
                self.assertFalse(catalog.results(db, {'q': ['归档关键词']})[1])
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
            failed = subprocess.run(cli + ['run'], env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(run('status'))['snapshot'], second['snapshot'])
            (root / 'unavailable').rename(repository)
            run('check')


if __name__ == '__main__':
    unittest.main()
