"""HTML for the local snapshot browser."""
from html import escape
from pathlib import PurePosixPath
from urllib.parse import urlencode

SEARCH_FIELDS = ('q', 'project', 'date', 'archived', 'page')
COPY = {
    'zh-CN': {
        'app': 'Codex 会话备份', 'snapshot': '快照', 'search_title': '搜索会话',
        'keyword': '关键词', 'placeholder': '搜索标题、工单号或正文', 'project': '项目',
        'all_projects': '全部项目', 'date': '更新日期', 'status': '归档状态',
        'all_statuses': '全部状态', 'active': '普通', 'archived': '已归档', 'search': '搜索',
        'recent': '最近更新', 'results': '搜索结果', 'count': '本页 {count} 条',
        'empty': '没有匹配的会话。', 'previous': '上一页', 'next': '下一页', 'pages': '结果分页',
        'back': '返回搜索', 'export': '导出 Markdown', 'attachments': '附件', 'download': '下载',
        'no_files': '此快照没有与该会话关联的附件。',
        'files_unavailable': '暂时无法读取快照附件。请检查备份库连接后刷新。',
        'contents': '目录', 'user': '你', 'assistant': 'Codex', 'no_messages': '此会话没有可显示的消息。',
    },
    'en': {
        'app': 'Codex Session Backup', 'snapshot': 'Snapshot', 'search_title': 'Search sessions',
        'keyword': 'Keyword', 'placeholder': 'Search titles, ticket IDs, or messages',
        'project': 'Project', 'all_projects': 'All projects', 'date': 'Updated on', 'status': 'Status',
        'all_statuses': 'All statuses', 'active': 'Active', 'archived': 'Archived', 'search': 'Search',
        'recent': 'Recently updated', 'results': 'Search results', 'count': 'On this page · {count}',
        'empty': 'No matching sessions.', 'previous': 'Previous', 'next': 'Next', 'pages': 'Result pages',
        'back': 'Back to search', 'export': 'Export Markdown', 'attachments': 'Attachments',
        'download': 'Download', 'no_files': 'This snapshot has no attachments for this session.',
        'files_unavailable': 'Attachments are temporarily unavailable. Check the backup repository and reload.',
        'contents': 'Contents', 'user': 'You', 'assistant': 'Codex', 'no_messages': 'This session has no messages to show.',
    },
}


def query_string(filters, **updates):
    values = {key: filters[key][0] for key in SEARCH_FIELDS if filters.get(key, [''])[0]}
    values.update(updates)
    return escape(urlencode(values))


def document(base, snapshot, lang, body, *, chat_title=None):
    copy = COPY[lang]
    title = copy['app'] if chat_title is None else f'{chat_title} · {copy["app"]}'
    scripts = '' if chat_title is None else (
        f'<script defer src="{base}/assets/markdown-it.min.js"></script>'
        f'<script defer src="{base}/assets/chat.js"></script>'
    )
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{base}/assets/search.css">
{scripts}
</head>
<body>
<header class="topbar"><div class="topbar-inner">
    <a class="brand" href="{base}"><span class="brand-icon" aria-hidden="true"></span>{copy['app']}</a>
    <span class="snapshot" title="{escape(snapshot)}">{copy['snapshot']} <code>{escape(snapshot[:12])}</code></span>
</div></header>
{body}
</body>
</html>"""


def status_badge(copy, archived):
    return (f'<span class="status status-archived">{copy["archived"]}</span>' if archived
            else f'<span class="status">{copy["active"]}</span>')


def search_page(base, copy, filters, values, hits, page, more):
    q, project, archived, date = values
    options = ''.join(
        f'<option value="{value}"{" selected" if value == archived else ""}>{copy[label]}</option>'
        for value, label in (('', 'all_statuses'), ('0', 'active'), ('1', 'archived'))
    )
    cards = []
    for chat, title, cwd, day, archived_flag, match in hits:
        snippet = ''
        if match:
            body, pos = match
            pos -= 1
            start, end = max(0, pos - 52), min(len(body), pos + len(q) + 100)
            snippet = (f'<p class="snippet">{"…" if start else ""}{escape(body[start:pos])}'
                       f'<mark>{escape(body[pos:pos + len(q)])}</mark>{escape(body[pos + len(q):end])}'
                       f'{"…" if end < len(body) else ""}</p>')
        query = query_string(filters, id=chat)
        cards.append(f"""<li><a class="result" href="{base}/chat?{query}">
            <div class="result-text">
                <h3>{escape(title)}</h3>{snippet}
                <div class="metadata">{escape(cwd or '')}<time>{escape(day or '')}</time></div>
            </div>
            {status_badge(copy, archived_flag)}<span class="result-arrow" aria-hidden="true">↗</span>
        </a></li>""")
    content = '<ul class="result-list">' + ''.join(cards) + '</ul>' if cards else f'<p class="empty">{copy["empty"]}</p>'
    navigation = []
    for number, label in ((page - 1, 'previous'), (page + 1, 'next')):
        if number >= 0 and (number < page or more):
            query = query_string(filters, page=number)
            navigation.append(f'<a class="button" href="{base}?{query}">{copy[label]}</a>')
    pagination = f'<nav class="pagination" aria-label="{copy["pages"]}">{"".join(navigation)}</nav>' if navigation else ''
    heading = copy['results' if any(values) else 'recent']
    return f"""<main class="search-page">
        <h1>{copy['search_title']}</h1>
        <form class="filters" action="{base}" role="search">
            <label class="keyword">{copy['keyword']}
                <span class="search-field"><input type="search" name="q" placeholder="{copy['placeholder']}" value="{escape(q)}"></span>
            </label>
            <label>{copy['project']}
                <input name="project" placeholder="{copy['all_projects']}" value="{escape(project)}">
            </label>
            <label>{copy['date']}<input name="date" type="date" value="{escape(date)}"></label>
            <label>{copy['status']}<select name="archived">{options}</select></label>
            <button class="button primary">{copy['search']}</button>
        </form>
        <section class="results" aria-labelledby="results-title">
            <div class="section-title"><h2 id="results-title">{heading}</h2>
                <span>{copy['count'].format(count=len(cards))}</span></div>
            {content}
            {pagination}
        </section>
    </main>"""


def attachment_list(base, copy, chat, files):
    if not files:
        key = 'files_unavailable' if files is None else 'no_files'
        return f'<p class="panel-note">{copy[key]}</p>'
    items = []
    for path in sorted(files, key=lambda value: (PurePosixPath(value).name.casefold(), value)):
        name = escape(PurePosixPath(path).name)
        query = escape(urlencode({'id': chat, 'path': path}))
        items.append(f"""<li><span title="{escape(path)}">{name}</span>
            <a class="download" href="{base}/download?{query}" download aria-label="{copy['download']} {name}"
               title="{copy['download']}">↓</a></li>""")
    return '<ul class="file-list">' + ''.join(items) + '</ul>'


def chat_page(base, copy, filters, chat, details, messages, files):
    title, project, day, archived = details
    back_query = query_string(filters)
    back_url = f'{base}?{back_query}' if back_query else base
    export_query = escape(urlencode({'id': chat}))
    articles, outline = [], []
    for number, (role, body) in enumerate(messages, 1):
        label = escape(copy.get(role, role))
        anchor = f'message-{number}'
        first_line = body.lstrip().split('\n', 1)[0].strip()
        preview = escape(first_line[:72] + ('…' if len(first_line) > 72 else ''))
        articles.append(f"""<article id="{anchor}" class="message">
            <h2 class="message-role"><span class="avatar {'assistant' if role == 'assistant' else ''}"
                aria-hidden="true">{'C' if role == 'assistant' else '↗'}</span>{label}</h2>
            <pre class="source">
{escape(body)}</pre>
        </article>""")
        outline.append(f"""<li><a href="#{anchor}">
            <span class="outline-role">{label}</span><span class="outline-preview">{preview}</span></a></li>""")
    empty = f'<p class="panel-note">{copy["no_messages"]}</p>'
    return f"""<main class="chat-page">
        <div class="chat-header">
            <a class="back" href="{back_url}"><span aria-hidden="true">←</span> {copy['back']}</a>
            <div class="chat-heading">
                <div><h1>{escape(title)}</h1>
                    <div class="metadata">{status_badge(copy, archived)}{escape(project or '')}
                        <time>{escape(day or '')}</time></div>
                </div>
                <div class="chat-actions">
                    <label class="markdown-toggle" hidden>
                        <input type="checkbox" role="switch" checked>
                        <span class="switch-track" aria-hidden="true"></span>Markdown
                    </label>
                    <a class="button" href="{base}/export?{export_query}">{copy['export']}</a>
                </div>
            </div>
        </div>
        <div class="chat-layout">
            <aside class="rail attachments">
                <details class="panel" open>
                    <summary><span>{copy['attachments']}</span><span class="panel-count">{len(files or {})}</span></summary>
                    <div class="panel-body">{attachment_list(base, copy, chat, files)}</div>
                </details>
            </aside>
            <nav class="rail outline" aria-label="{copy['contents']}">
                <details class="panel" open>
                    <summary><span>{copy['contents']}</span><span class="panel-count">{len(messages)}</span></summary>
                    <div class="panel-body">{'<ol>' + ''.join(outline) + '</ol>' if outline else empty}</div>
                </details>
            </nav>
            <div class="messages">{''.join(articles) or empty}</div>
        </div>
    </main>"""
