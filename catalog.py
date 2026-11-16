"""Build and serve immutable search catalogs for backup snapshots."""
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlsplit

import search_ui

ATTACHMENT_TREES = ('attachments', 'artifacts', 'generated_images', 'visualizations')
SNAPSHOT_ID = re.compile(r'[a-f0-9]{8,64}')
ASSET_DIR = Path(__file__).resolve().with_name('search_assets')
ASSETS = {
    'search.css': 'text/css; charset=utf-8',
    'markdown-it.min.js': 'text/javascript; charset=utf-8',
    'chat.js': 'text/javascript; charset=utf-8',
}


class AttachmentError(Exception):
    pass


class SnapshotError(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
 id TEXT PRIMARY KEY, title TEXT, project TEXT, updated INTEGER, archived INTEGER,
 path TEXT, revision TEXT
);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY, chat TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(body, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS message_insert AFTER INSERT ON messages BEGIN
 INSERT INTO search(rowid, body) VALUES(new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS message_delete AFTER DELETE ON messages BEGIN
 INSERT INTO search(search, rowid, body) VALUES('delete', old.id, old.body);
END;
"""


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def projected_messages(db, thread):
    for item_type, raw in db.execute("""
        SELECT item_type, item_json FROM thread_items
        WHERE thread_id=? AND item_type IN ('userMessage','agentMessage')
        ORDER BY rollout_ordinal, item_id
    """, (thread,)):
        item = json.loads(raw)
        if item_type == 'userMessage':
            body = '\n'.join(part.get('text', '') for part in item['content']
                             if part.get('type') == 'text')
            role = 'user'
        else:
            body, role = item.get('text', ''), 'assistant'
        if body:
            yield role, body


def legacy_messages(path):
    # Old rollouts without a history projection store the visible text in events.
    process = None
    if path.suffix == '.zst':
        process = subprocess.Popen(['zstd', '-dcq', '--', str(path)], stdout=subprocess.PIPE)
        stream = process.stdout
    else:
        stream = path.open('rb')
    try:
        for line in stream:
            if not line.endswith(b'\n'):
                continue
            event = json.loads(line)
            if event.get('type') != 'event_msg':
                continue
            payload = event.get('payload', {})
            item = payload.get('item', {})
            kind = payload.get('type')
            if kind == 'item_completed' and item.get('type') in ('UserMessage', 'AgentMessage'):
                role = 'user' if item['type'] == 'UserMessage' else 'assistant'
                body = item.get('text') or item.get('message')
                if not body:
                    body = '\n'.join(part.get('text', '') for part in item.get('content', [])
                                     if isinstance(part, dict))
            elif kind in ('user_message', 'agent_message'):
                role = 'user' if kind == 'user_message' else 'assistant'
                body = payload.get('message')
            else:
                continue
            if isinstance(body, str) and body:
                yield role, body
    finally:
        stream.close()
        if process and process.wait():
            raise ValueError('Unable to decode legacy rollout')


def build(bundle, previous, threads, inventory, project_names=None):
    target = bundle / 'catalog.sqlite'
    if previous.is_file():
        shutil.copyfile(previous, target)
    with closing(sqlite3.connect(target)) as index, closing(
            readonly(bundle / 'codex/thread_history_1.sqlite')) as history:
        index.executescript(SCHEMA)
        old = dict(index.execute('SELECT id, revision FROM chats'))
        projection = {row[0]: row[1:] for row in history.execute(
            'SELECT thread_id,next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state')}
        project_names = project_names or {}
        with index:
            for thread, title, cwd, updated, archived, path in threads:
                revision = json.dumps([inventory[path]['sha256'], projection.get(thread)])
                index.execute('INSERT OR REPLACE INTO chats VALUES (?,?,?,?,?,?,?)',
                              (thread, title, project_names.get(thread) or cwd, updated, archived, path, revision))
                if old.pop(thread, None) == revision:
                    continue
                index.execute('DELETE FROM messages WHERE chat=?', (thread,))
                messages = (projected_messages(history, thread) if thread in projection
                            else legacy_messages(bundle / path))
                index.executemany('INSERT INTO messages(chat,role,body) VALUES (?,?,?)',
                                  ((thread, role, body) for role, body in messages))
            for thread in old:
                index.execute('DELETE FROM messages WHERE chat=?', (thread,))
                index.execute('DELETE FROM chats WHERE id=?', (thread,))
        if index.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid search catalog')
    return target


def results(db, filters):
    q, project, archived, date = (filters.get(key, [''])[0] for key in ('q', 'project', 'archived', 'date'))
    where, args = [], []
    if archived in ('0', '1'):
        where.append('c.archived=?')
        args.append(int(archived))
    if project:
        where.append("instr(lower(coalesce(c.project,'')),lower(?))>0")
        args.append(project)
    if date:
        where.append("date(c.updated,'unixepoch','+8 hours')=?")
        args.append(date)
    if q:
        where.append("""(instr(lower(c.title),lower(?))>0 OR c.id=? OR EXISTS (
            SELECT 1 FROM messages m WHERE m.chat=c.id AND
            """ + ("m.id IN (SELECT rowid FROM search WHERE search MATCH ?))"
                   if len(q) >= 3 else "instr(lower(m.body),lower(?))>0)") + ")")
        term = '"' + q.replace('"', '""') + '"' if len(q) >= 3 else q
        args.extend((q, q, term))
    condition = ' WHERE ' + ' AND '.join(where) if where else ''
    page = filters.get('page', ['0'])[0]
    offset = min(int(page), 10000) * 50 if page.isascii() and page.isdecimal() and len(page) <= 5 else 0
    hits = db.execute(
        "SELECT c.id,c.title,c.project,date(c.updated,'unixepoch','+8 hours'),c.archived FROM chats c"
        + condition + ' ORDER BY c.updated DESC LIMIT 51 OFFSET ?', (*args, offset)).fetchall()
    return (q, project, archived, date), hits[:50], offset // 50, len(hits) == 51


def attachment_files(manifest, chat, messages):
    source = PurePosixPath(manifest['source'])
    conversation = '\n'.join(body for _, body in messages)
    files = {}
    for path, entry in manifest['files'].items():
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != 'codex' or parts[1] not in ATTACHMENT_TREES:
            continue
        # Visualizations are grouped by date before the chat ID.
        owner = 5 if parts[1] == 'visualizations' else 2
        if len(parts) > owner + 1 and parts[owner] == chat:
            files[path] = entry
            continue
        original = str(source.joinpath(*parts[1:]))
        encoded = quote(original)
        variants = {value for value in (original, encoded, 'file://' + encoded) if value in conversation}
        if not variants:
            continue
        pattern = '|'.join(map(re.escape, variants))
        # Match complete paths in text, Markdown links, and file URLs.
        if re.search(r'''(?<![\w./%~+-])(?:''' + pattern + r''')(?=$|[\s)\]}>`'"，。；：]|:\d)''',
                     conversation):
            files[path] = entry
    return files


def serve(default_snapshot, open_browser=True, *, list_snapshots, open_index, load_manifest, download_file):
    token = secrets.token_urlsafe(24)
    base = '/' + token
    assets = {f'{base}/assets/{name}': (ASSET_DIR.joinpath(name).read_bytes(), kind)
              for name, kind in ASSETS.items()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_headers(self, length, kind, disposition, *, status=200):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'self'; "
                             "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()

        def send(self, content, kind='text/html; charset=utf-8', disposition=None, *, status=200):
            data = content.encode()
            self.send_headers(len(data), kind, disposition, status=status)
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path in assets:
                data, kind = assets[url.path]
                self.send_headers(len(data), kind, None)
                self.wfile.write(data)
                return
            if url.path not in (base, base + '/snapshots', base + '/chat', base + '/export', base + '/download'):
                self.send_error(404)
                return
            filters = parse_qs(url.query)
            snapshot = filters.get('snapshot', [default_snapshot])[0]
            if not SNAPSHOT_ID.fullmatch(snapshot):
                self.send_error(400, 'Invalid snapshot ID')
                return
            filters['snapshot'] = [snapshot]
            preferred = self.headers.get('Accept-Language', '').split(',', 1)[0].split(';', 1)[0]
            lang = 'en' if preferred.strip().lower().split('-')[0] == 'en' else 'zh-CN'
            copy = search_ui.COPY[lang]
            if url.path.endswith('/snapshots'):
                try:
                    snapshots = list_snapshots()
                except SnapshotError:
                    snapshots = None
                body = search_ui.snapshots_page(base, copy, filters, snapshots)
                self.send(search_ui.document(base, filters, lang, body), status=503 if snapshots is None else 200)
                return
            try:
                database = open_index(snapshot)
            except SnapshotError:
                self.send_error(503, 'Snapshot search index unavailable')
                return
            title = None
            with closing(readonly(database)) as db:
                if url.path == base:
                    values, hits, page, more = results(db, filters)
                    q = values[0]
                    cards = []
                    for hit in hits:
                        match = db.execute("""SELECT body,instr(lower(body),lower(?)) FROM messages
                            WHERE chat=? AND instr(lower(body),lower(?))>0 ORDER BY id LIMIT 1""",
                            (q, hit[0], q)).fetchone() if q else None
                        cards.append((*hit, match))
                    body = search_ui.search_page(base, copy, filters, values, cards, page, more)
                else:
                    chat = filters.get('id', [''])[0]
                    row = db.execute("""SELECT title,project,date(updated,'unixepoch','+8 hours'),archived
                        FROM chats WHERE id=?""", (chat,)).fetchone()
                    if not row:
                        self.send_error(404)
                        return
                    messages = list(db.execute('SELECT role,body FROM messages WHERE chat=? ORDER BY id', (chat,)))
                    if url.path.endswith('/export'):
                        self.send('# ' + row[0] + '\n\n' + '\n\n'.join(
                            '## ' + role + '\n\n' + body for role, body in messages),
                            'text/markdown; charset=utf-8', 'attachment; filename="codex-chat.md"')
                        return
                    if url.path.endswith('/download'):
                        self.download(snapshot, chat, messages, filters.get('path', [''])[0])
                        return
                    try:
                        files = attachment_files(load_manifest(snapshot), chat, messages)
                    except AttachmentError:
                        files = None
                    title = row[0]
                    body = search_ui.chat_page(base, copy, filters, chat, row, messages, files)
            self.send(search_ui.document(base, filters, lang, body, chat_title=title))

        def download(self, snapshot, chat, messages, path):
            try:
                entry = attachment_files(load_manifest(snapshot), chat, messages).get(path)
                if entry is None:
                    self.send_error(404)
                    return
                name = PurePosixPath(path).name
                fallback = re.sub(r'[^A-Za-z0-9._ -]', '_', name)
                disposition = f"""attachment; filename="{fallback}"; filename*=UTF-8''{quote(name, safe='')}"""
                with download_file(snapshot, path, entry) as content:
                    self.send_headers(entry['size'], 'application/octet-stream', disposition)
                    shutil.copyfileobj(content, self.wfile)
            except AttachmentError:
                self.send_error(503, 'Snapshot attachment unavailable')
            except ConnectionError:
                pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        url = f'http://127.0.0.1:{server.server_port}{base}'
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
