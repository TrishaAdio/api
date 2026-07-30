/* Markdown renderer for assistant output.
   Standalone and dependency-free so it can be unit-tested under node.
   Covers: headings, fenced code with language + copy, tables with alignment,
   nested lists, task lists, blockquotes, rules, links, images, bold, italic,
   strikethrough, inline code, and <thinking> extraction. */
'use strict';

(function (root) {

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* Only allow schemes that cannot execute script. */
  function safeUrl(raw) {
    const url = String(raw).trim();
    if (/^(https?:|mailto:|#|\/)/i.test(url)) return esc(url);
    return '';
  }

  /* ── inline ─────────────────────────────────────────────── */

  function inline(src) {
    // Protect code spans before any other rule can touch their contents.
    const spans = [];
    let s = String(src).replace(/(`+)([\s\S]*?)\1/g, (_m, _t, code) => {
      spans.push(code.trim());
      return `\u0001${spans.length - 1}\u0001`;
    });

    s = esc(s);

    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_m, alt, url) => {
      const href = safeUrl(url);
      return href ? `<img src="${href}" alt="${alt}" loading="lazy">` : alt;
    });

    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_m, label, url) => {
      const href = safeUrl(url);
      return href
        ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
        : label;
    });

    s = s.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
         .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
         .replace(/__([^_]+)__/g, '<strong>$1</strong>')
         .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:!?)]|$)/g, '$1<em>$2</em>')
         .replace(/(^|[\s(])_([^_\n]+)_(?=[\s.,;:!?)]|$)/g, '$1<em>$2</em>')
         .replace(/~~([^~]+)~~/g, '<del>$1</del>');

    // Bare URLs, but not ones already inside an href we just produced.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_m, pre, url) =>
      `${pre}<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`);

    return s.replace(/\u0001(\d+)\u0001/g, (_m, i) => `<code>${esc(spans[i])}</code>`);
  }

  /* ── code blocks ────────────────────────────────────────── */

  function codeBlock(lang, code, open) {
    // The raw source rides along percent-encoded so Copy yields exactly what
    // the model wrote, not the escaped HTML.
    const raw = encodeURIComponent(code);
    return `<figure class="code${open ? ' streaming' : ''}">` +
      `<figcaption><span class="lang">${esc(lang || 'text')}</span>` +
      `<button type="button" class="code-copy" data-raw="${raw}">Copy</button>` +
      `</figcaption><pre><code>${esc(code)}</code></pre></figure>`;
  }

  /* ── tables ─────────────────────────────────────────────── */

  const isDivider = (line) =>
    /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('-');

  const cells = (line) => {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|')) s = s.slice(0, -1);
    return s.split('|').map((c) => c.trim());
  };

  function alignments(divider) {
    return cells(divider).map((c) => {
      const left = c.startsWith(':');
      const right = c.endsWith(':');
      if (left && right) return 'center';
      if (right) return 'right';
      if (left) return 'left';
      return '';
    });
  }

  function table(head, divider, bodyLines) {
    const align = alignments(divider);
    const at = (i) => (align[i] ? ` style="text-align:${align[i]}"` : '');

    const th = cells(head).map((c, i) => `<th${at(i)}>${inline(c)}</th>`).join('');
    const rows = bodyLines.map((line) => {
      const tds = cells(line).map((c, i) => `<td${at(i)}>${inline(c)}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');

    return `<div class="table-scroll"><table class="md">` +
      `<thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  /* ── lists ──────────────────────────────────────────────── */

  const BULLET = /^(\s*)([-*+])\s+(.*)$/;
  const NUMBER = /^(\s*)(\d+)[.)]\s+(.*)$/;

  function listItems(lines, start) {
    const items = [];
    let i = start;

    while (i < lines.length) {
      const line = lines[i];
      const m = BULLET.exec(line) || NUMBER.exec(line);

      if (m) {
        items.push({
          indent: m[1].replace(/\t/g, '    ').length,
          ordered: !BULLET.exec(line),
          text: m[3],
        });
        i++;
        continue;
      }

      // A wrapped continuation line belongs to the previous item.
      if (items.length && line.trim() && !/^\s*(#{1,6}\s|>|```|~~~)/.test(line)) {
        items[items.length - 1].text += '\n' + line.trim();
        i++;
        continue;
      }
      break;
    }
    return [items, i];
  }

  function buildList(items, from, depth) {
    const ordered = items[from].ordered;
    let html = ordered ? '<ol>' : '<ul>';
    let i = from;

    while (i < items.length && items[i].indent >= depth) {
      if (items[i].indent > depth) {
        const [nested, next] = buildList(items, i, items[i].indent);
        html = html.replace(/<\/li>$/, nested + '</li>');
        i = next;
        continue;
      }
      if (items[i].ordered !== ordered) break;

      const task = /^\[([ xX])\]\s+(.*)$/.exec(items[i].text);
      if (task) {
        const done = task[1].toLowerCase() === 'x';
        html += `<li class="task"><input type="checkbox" disabled${done ? ' checked' : ''}>` +
          `<span>${inline(task[2])}</span></li>`;
      } else {
        html += `<li>${inline(items[i].text)}</li>`;
      }
      i++;
    }
    return [html + (ordered ? '</ol>' : '</ul>'), i];
  }

  /* ── block parser ───────────────────────────────────────── */

  const RULE = /^\s*([-*_])\s*(\1\s*){2,}$/;

  function render(src) {
    if (src == null) return '';
    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;

    /* Does a new block start at this line? Used to end a paragraph. It must
       agree exactly with the branches below, or the cursor can stall. */
    const startsBlock = (idx) => {
      const l = lines[idx];
      return /^\s*(#{1,6}\s|>|```|~~~)/.test(l)
        || RULE.test(l)
        || BULLET.test(l) || NUMBER.test(l)
        || (l.includes('|') && idx + 1 < lines.length && isDivider(lines[idx + 1]));
    };

    while (i < lines.length) {
      const line = lines[i];

      // fenced code
      const fence = /^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$/.exec(line);
      if (fence) {
        const marker = fence[1][0].repeat(3);
        const lang = fence[2];
        const body = [];
        i++;
        let closed = false;
        while (i < lines.length) {
          if (new RegExp(`^\\s*${marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}+\\s*$`).test(lines[i])) {
            closed = true;
            i++;
            break;
          }
          body.push(lines[i]);
          i++;
        }
        // An unclosed fence means the answer is still streaming in.
        out.push(codeBlock(lang, body.join('\n'), !closed));
        continue;
      }

      if (!line.trim()) { i++; continue; }

      // table
      if (line.includes('|') && i + 1 < lines.length && isDivider(lines[i + 1])) {
        const head = line;
        const divider = lines[i + 1];
        const body = [];
        i += 2;
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          body.push(lines[i]);
          i++;
        }
        out.push(table(head, divider, body));
        continue;
      }

      // heading
      const heading = /^\s*(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i++;
        continue;
      }

      // rule
      if (RULE.test(line)) { out.push('<hr>'); i++; continue; }

      // blockquote
      if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && (/^\s*>/.test(lines[i]) || (body.length && lines[i].trim()))) {
          body.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        out.push(`<blockquote>${render(body.join('\n'))}</blockquote>`);
        continue;
      }

      // list
      if (BULLET.test(line) || NUMBER.test(line)) {
        const [items, next] = listItems(lines, i);
        const [html] = buildList(items, 0, items[0].indent);
        out.push(html);
        i = next;
        continue;
      }

      // paragraph — always consumes the current line, so the cursor advances
      const para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(i)) {
        para.push(lines[i]);
        i++;
      }
      // Two trailing spaces is a hard line break.
      out.push(`<p>${inline(para.join('\n')).replace(/ {2}\n|\n/g, '<br>')}</p>`);
    }

    return out.join('');
  }

  /* ── thinking ───────────────────────────────────────────── */

  const THINK = /<(thinking|think|reasoning)>([\s\S]*?)(?:<\/\1>|$)/gi;

  /* Pull reasoning out of the answer so it can be shown separately. */
  function splitThinking(src) {
    const text = String(src == null ? '' : src);
    const parts = [];
    const answer = text.replace(THINK, (_m, _tag, body) => {
      parts.push(body.trim());
      return '';
    });
    return {
      thinking: parts.join('\n\n').trim(),
      answer: answer.replace(/\n{3,}/g, '\n\n').trim(),
    };
  }

  const api = { render, inline, splitThinking, escapeHtml: esc };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.RioMD = api;

})(typeof globalThis !== 'undefined' ? globalThis : this);
