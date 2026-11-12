"""Build and serve an immutable search catalog for one backup snapshot."""
import html
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import parse_qs, quote, urlencode, urlsplit

ATTACHMENT_TREES = ('attachments', 'artifacts', 'generated_images', 'visualizations')


class AttachmentError(Exception):
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


STYLE = """
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:64rem;margin:3rem auto;padding:0 1.25rem;line-height:1.55}
h1{font-size:1.65rem}form{display:flex;gap:.65rem;flex-wrap:wrap}
input,select,button{font:inherit;padding:.35rem .55rem}
input[name=q]{flex:1;min-width:12rem}article{padding:.85rem 0;border-bottom:1px solid #8885}
small{opacity:.75}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}
.attachments li{margin:.35rem 0;overflow-wrap:anywhere}
.attachments a{margin-inline-start:.6em}
"""


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


def serve(database, snapshot, open_browser=True, *, load_manifest, download_file):
    token = secrets.token_urlsafe(24)
    base = '/' + token
    esc = html.escape

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_headers(self, length, kind, disposition):
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()

        def send(self, content, kind='text/html; charset=utf-8', disposition=None):
            data = content.encode()
            self.send_headers(len(data), kind, disposition)
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path not in (base, base + '/chat', base + '/export', base + '/download'):
                self.send_error(404)
                return
            filters = parse_qs(url.query)
            with closing(readonly(database)) as db:
                if url.path == base:
                    body = self.search(db, filters)
                else:
                    chat = filters.get('id', [''])[0]
                    row = db.execute('SELECT title FROM chats WHERE id=?', (chat,)).fetchone()
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
                        self.download(chat, messages, filters.get('path', [''])[0])
                        return
                    body = f'<a href="{base}">返回搜索</a> · <a href="{base}/export?{urlencode({"id": chat})}">导出 Markdown</a>'
                    body += f'<h1>{esc(row[0])}</h1>' + self.attachments(chat, messages) + ''.join(
                        f'<article><b>{esc(role)}</b><pre>{esc(text)}</pre></article>' for role, text in messages)
            self.send(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Codex 会话备份</title><style>{STYLE}</style><body>{body}</body></html>')

        def attachments(self, chat, messages):
            heading = '<section class="attachments"><h2>附件</h2>'
            try:
                files = attachment_files(load_manifest(), chat, messages)
            except AttachmentError:
                return heading + '<p>暂时无法读取快照附件。请检查备份库连接后刷新。</p></section>'
            if not files:
                return heading + '<p>此快照没有与该会话关联的附件。</p></section>'
            items = []
            for path in sorted(files, key=lambda path: (PurePosixPath(path).name.casefold(), path)):
                name = esc(PurePosixPath(path).name)
                query = esc(urlencode({'id': chat, 'path': path}))
                items.append(f'<li title="{esc(path)}">{name} '
                             f'<a href="{base}/download?{query}" download aria-label="下载 {name}">下载</a></li>')
            return heading + '<ul>' + ''.join(items) + '</ul></section>'

        def download(self, chat, messages, path):
            try:
                entry = attachment_files(load_manifest(), chat, messages).get(path)
                if entry is None:
                    self.send_error(404)
                    return
                name = PurePosixPath(path).name
                fallback = re.sub(r'[^A-Za-z0-9._ -]', '_', name)
                disposition = f"""attachment; filename="{fallback}"; filename*=UTF-8''{quote(name, safe='')}"""
                with download_file(path, entry) as content:
                    self.send_headers(entry['size'], 'application/octet-stream', disposition)
                    shutil.copyfileobj(content, self.wfile)
            except AttachmentError:
                self.send_error(503, 'Snapshot attachment unavailable')
            except ConnectionError:
                pass

        def search(self, db, filters):
            (q, project, archived, date), hits, page, more = results(db, filters)
            options = ''.join(f'<option value="{v}" {"selected" if v == archived else ""}>{label}</option>'
                              for v, label in (('', '全部状态'), ('0', '普通'), ('1', '已归档')))
            body = f"""<h1>Codex 会话备份</h1><p><small>快照 {esc(snapshot)}</small></p>
                <form action="{base}"><input name="q" aria-label="关键词" placeholder="标题、工单号或正文" value="{esc(q)}">
                <input name="project" aria-label="项目" placeholder="项目" value="{esc(project)}">
                <input name="date" aria-label="更新日期" type="date" value="{esc(date)}">
                <select name="archived" aria-label="归档状态">{options}</select><button>搜索</button></form>"""
            for chat, title, cwd, day, archived_flag in hits:
                snippet = ''
                if q:
                    message = db.execute("""SELECT body,instr(lower(body),lower(?)) FROM messages
                        WHERE chat=? AND instr(lower(body),lower(?))>0 ORDER BY id LIMIT 1""", (q, chat, q)).fetchone()
                    if message:
                        text, pos = message
                        snippet = f'<div>{esc(text[max(0,pos-51):pos+110])}</div>'
                body += (f'<article><a href="{base}/chat?{urlencode({"id": chat})}">{esc(title)}</a>'
                         f'<br><small>{esc(day or "")} · {"已归档" if archived_flag else "普通"} · {esc(cwd or "")}</small>'
                         f'{snippet}</article>')
            if not hits:
                body += '<p>没有匹配的会话。</p>'
            for n, label in ((page - 1, '上一页'), (page + 1, '下一页')):
                if n < 0 or (n > page and not more):
                    continue
                query = urlencode({**{k: v[0] for k, v in filters.items()}, 'page': n})
                body += f' <a href="{base}?{esc(query)}">{label}</a>'
            return body

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        url = f'http://127.0.0.1:{server.server_port}{base}'
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
