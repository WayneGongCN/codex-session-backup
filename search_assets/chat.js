(() => {
  const toggle = document.querySelector('.markdown-toggle');
  const messages = document.querySelectorAll('.message');
  if (!window.markdownit || !messages.length) return;

  const parser = window.markdownit({ html: false });
  parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
    const alt = renderer.renderInlineAsText(tokens[index].children, options, env);
    return parser.utils.escapeHtml(alt);
  };
  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const href = token.attrGet('href');
    if (/^https?:\/\//i.test(href)) {
      token.attrSet('target', '_blank');
      token.attrSet('rel', 'noopener noreferrer');
    } else if (!/^mailto:/i.test(href)) {
      token.attrs.splice(token.attrIndex('href'), 1);
    }
    return renderer.renderToken(tokens, index, options);
  };
  parser.renderer.rules.table_open = () => '<div class="table-wrap"><table>\n';
  parser.renderer.rules.table_close = () => '</table></div>\n';
  parser.renderer.rules.th_open = parser.renderer.rules.td_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const style = token.attrIndex('style');
    if (style >= 0) {
      const [, alignment] = token.attrs[style][1].split(':');
      token.attrs[style] = ['class', 'align-' + alignment];
    }
    return renderer.renderToken(tokens, index, options);
  };

  const outline = document.querySelectorAll('.outline ol > li');
  for (const [index, message] of messages.entries()) {
    const rendered = document.createElement('div');
    rendered.className = 'markdown';
    rendered.innerHTML = parser.render(message.querySelector('.source').textContent);
    message.append(rendered);

    const preview = rendered.querySelector('p')?.textContent.trim();
    if (preview) {
      outline[index].querySelector('.outline-preview').textContent =
        preview.length > 72 ? preview.slice(0, 72) + '…' : preview;
    }

    const headings = document.createElement('ul');
    headings.className = 'outline-headings';
    for (const [number, heading] of [...rendered.querySelectorAll('h1,h2,h3,h4,h5,h6')].entries()) {
      heading.id = `${message.id}-heading-${number + 1}`;
      const item = document.createElement('li');
      const link = document.createElement('a');
      link.href = '#' + heading.id;
      link.textContent = heading.textContent;
      item.append(link);
      headings.append(item);
    }
    if (headings.childElementCount) outline[index].append(headings);
  }

  const chat = document.querySelector('.chat-page');
  const input = toggle.querySelector('input');
  const syncMode = () => {
    chat.classList.toggle('show-source', !input.checked);
    const target = document.getElementById(location.hash.slice(1));
    if (!input.checked && target?.closest('.markdown')) {
      history.replaceState(null, '', '#' + target.closest('.message').id);
    }
  };
  input.addEventListener('change', syncMode);
  window.addEventListener('pageshow', syncMode);
  syncMode();
  chat.classList.add('markdown-ready');
  toggle.hidden = false;

  const initialTarget = document.getElementById(location.hash.slice(1));
  if (initialTarget?.closest('.markdown')) initialTarget.scrollIntoView();
})();
