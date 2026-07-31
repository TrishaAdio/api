/* RioApis console — vanilla, no build step.
   Admin access is decided server-side from the caller's IP, so nothing
   sensitive is stored client-side. */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  ip: '', models: [], model: 'auto', rate: 0.04,
  messages: [], busy: false, view: 'chat',
  entries: [], keys: [], timer: null,
};

/* ── net ─────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' }, ...options,
  });
  if (res.status === 403) { deny(state.ip); throw new Error('forbidden'); }
  const body = await res.text();
  let data = null;
  try { data = body ? JSON.parse(body) : null; } catch { data = { detail: body }; }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || res.statusText);
  return data;
}

/* ── format ──────────────────────────────────────────────── */

function money(n) {
  const v = Number(n || 0);
  if (v === 0) return '$0.00';
  if (Math.abs(v) < 0.01) return '$' + v.toFixed(4);
  if (Math.abs(v) < 1000) return '$' + v.toFixed(2);
  return '$' + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

const num = (n) => Number(n || 0).toLocaleString();

const credits = (n) => Number(n || 0).toLocaleString(undefined, {
  minimumFractionDigits: 0, maximumFractionDigits: 1,
});

function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function until(ts) {
  if (!ts) return '—';
  const s = ts - Date.now() / 1000;
  if (s <= 0) return 'resets now';
  const d = Math.floor(s / 86400);
  if (d >= 1) return `resets in ${d}d`;
  return `resets in ${Math.max(1, Math.floor(s / 3600))}h`;
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Rendering is delegated to md.js so it stays unit-testable. */
const md = (src) => RioMD.render(src);

const icon = (id) => `<svg><use href="#${id}"/></svg>`;

/* Reasoning streams in before the answer. It is shown open while the model is
   still thinking, then collapsed once the answer starts, since by then it is
   context rather than the result. */
function thinkPanel(turn) {
  if (!turn) return null;
  const host = $('.think', turn);
  if (!host) return null;

  if (!host.dataset.ready) {
    host.dataset.ready = '1';
    host.hidden = false;
    host.innerHTML =
      `<button type="button" class="think-toggle">
         <svg class="think-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
           <path d="M9 6l6 6-6 6"/>
         </svg>
         <span>Thinking</span><em></em>
       </button>
       <div class="think-body"></div>`;

    const toggle = $('.think-toggle', host);
    const body = $('.think-body', host);
    toggle.onclick = () => {
      const open = body.hidden;
      body.hidden = !open;
      host.classList.toggle('open', open);
      $('span', toggle).textContent = open ? 'Hide thinking' : 'Thinking';
    };
  }
  return host;
}

function updateThinking(turn, text, live) {
  const host = thinkPanel(turn);
  if (!host || !text) return;
  const body = $('.think-body', host);
  body.hidden = false;
  host.classList.add('open', 'live');
  body.innerHTML = md(text) + (live ? '<span class="caret"></span>' : '');
  $('.think-toggle em', host).textContent = `${wordCount(text)} words`;
  $('.think-toggle span', host).textContent = live ? 'Thinking' : 'Hide thinking';
  host.classList.toggle('live', !!live);
}

function collapseThinking(turn) {
  const host = turn && $('.think', turn);
  if (!host || !host.dataset.ready) return;
  const body = $('.think-body', host);
  body.innerHTML = body.innerHTML.replace(/<span class="caret"><\/span>$/, '');
  body.hidden = true;
  host.classList.remove('open', 'live');
  $('.think-toggle span', host).textContent = 'Thinking';
}

const wordCount = (s) => String(s).trim().split(/\s+/).filter(Boolean).length;

function toast(message, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = icon(kind === 'ok' ? 'i-check' : 'i-x') + `<span>${esc(message)}</span>`;
  $('#toasts').append(el);
  setTimeout(() => {
    el.classList.add('gone');
    el.addEventListener('animationend', () => el.remove());
  }, 3000);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied');
  } catch {
    // Clipboard API needs a secure context; fall back to a hidden selection.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.append(ta);
    ta.select();
    try { document.execCommand('copy'); toast('Copied'); }
    catch { toast('Copy failed — select it manually', 'err'); }
    ta.remove();
  }
}

/* ── boot ────────────────────────────────────────────────── */

async function boot() {
  let session;
  try { session = await (await fetch('/api/session')).json(); }
  catch { $('#boot').hidden = true; return deny(''); }

  state.ip = session.ip || '';
  if (!session.admin) { $('#boot').hidden = true; return deny(state.ip); }

  $('#boot').hidden = true;
  $('#app').hidden = false;
  $('#set-ip').textContent = state.ip || 'localhost';

  await bootstrap();
  wire();
  loadUsage();
}

function deny(ip) {
  $('#app').hidden = true;
  $('#denied').hidden = false;
  $('#denied-ip').textContent = ip || 'unknown';
}

async function bootstrap() {
  try {
    const data = await api('/bootstrap');
    state.models = data.models || [];
    state.rate = data.usd_per_credit ?? 0.04;
    state.model = data.default_model || 'auto';
    status(data.ready ? 'ok' : 'bad', data.ready ? state.ip || 'localhost' : 'CLI unavailable');
    $('#logo-sub').textContent = data.model_selection ? 'OpenAI compatible' : 'fixed model';
    paintAccess(data.tool_access);
    paintModels();
    paintDocs();
  } catch (err) {
    status('bad', 'error');
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* A standing indicator whenever the model can touch the machine. */
function paintAccess(level) {
  const host = $('#armed');
  if (!host) return;
  host.hidden = level === 'off' || !level;
  host.classList.toggle('console', level === 'console');
  if (!host.hidden) {
    $('#armed-text').textContent = level === 'all' ? 'machine access · api' : 'machine access';
  }
}

function status(kind, text) {
  $('#pip').className = `pip ${kind}`;
  $('#status-text').textContent = text;
}

/* ── nav ─────────────────────────────────────────────────── */

function go(view) {
  state.view = view;
  $$('.item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.page').forEach((p) => p.classList.toggle('active', p.dataset.view === view));

  if (view === 'usage') loadUsage();
  if (view === 'keys') loadKeys();
  if (view === 'access') loadWhitelist();
  if (view === 'settings') loadSettings();
}

/* ── models ──────────────────────────────────────────────── */

function paintModels() {
  const current = state.models.find((m) => m.id === state.model) || state.models[0];
  if (current) {
    state.model = current.id;
    $('#model-current').textContent = current.id;
    $('#model-cost').textContent = money(current.cost * state.rate);
  }

  const select = $('#default-model');
  if (select) {
    select.innerHTML = state.models.map((m) => `<option value="${m.id}">${m.id}</option>`).join('');
    select.value = state.model;
  }
  paintOptions('');
}

function paintOptions(query) {
  const q = query.trim().toLowerCase();
  const rows = state.models.filter((m) => !q || m.id.toLowerCase().includes(q));
  const list = $('#model-list');

  if (!rows.length) { list.innerHTML = '<div class="void">No match</div>'; return; }

  list.innerHTML = rows.map((m) => `
    <button class="opt ${m.id === state.model ? 'on' : ''}" data-model="${m.id}">
      <span class="oid">${m.id}</span>
      <span class="price">${money(m.cost * state.rate)}</span>
      ${m.description ? `<span class="odesc">${esc(m.description)}</span>` : ''}
    </button>`).join('');

  $$('.opt', list).forEach((opt) => opt.onclick = () => {
    state.model = opt.dataset.model;
    paintModels();
    paintExample();
    closePop();
  });
}

const openPop = () => {
  $('#model-pop').hidden = false;
  $('#model-btn').setAttribute('aria-expanded', 'true');
  $('#model-search').value = '';
  paintOptions('');
  $('#model-search').focus();
};

const closePop = () => {
  $('#model-pop').hidden = true;
  $('#model-btn').setAttribute('aria-expanded', 'false');
};

/* ── chat ────────────────────────────────────────────────── */

function turn(role, text) {
  $('#welcome')?.remove();
  const el = document.createElement('div');
  el.className = `turn ${role === 'user' ? 'me' : 'bot'}`;
  el.innerHTML = `<span class="who">${role === 'user' ? 'You' : 'RioApis'}</span>` +
    (role === 'user' ? '' : '<div class="think" hidden></div>') +
    '<div class="body"></div>';
  const body = $('.body', el);
  if (role === 'user') body.textContent = text;
  $('#thread').append(el);
  bottom();
  return body;
}

function bottom() {
  const t = $('#thread');
  t.scrollTop = t.scrollHeight;
}

async function send(text) {
  if (!text.trim() || state.busy) return;

  state.messages.push({ role: 'user', content: text });
  turn('user', text);

  const body = turn('assistant', '');
  body.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';

  state.busy = true;
  state.abort = new AbortController();
  $('#send').hidden = true;
  $('#stop').hidden = false;
  $('#turn-meta').textContent = '';

  const turnEl = body.closest('.turn');
  let answer = '';
  let thoughts = '';
  let collapsed = false;
  let frame = 0;
  let thoughtFrame = 0;
  let dirty = false;

  // Reasoning repaints on the same one-frame budget as the answer.
  const paintThoughts = () => {
    if (thoughtFrame) return;
    thoughtFrame = requestAnimationFrame(() => {
      thoughtFrame = 0;
      // A frame queued before the answer started must not reopen the panel.
      if (collapsed) return;
      updateThinking(turnEl, thoughts, true);
      bottom();
    });
  };

  // Re-parsing markdown on every 3-character delta would burn the main thread
  // on long answers, so repaint at most once per frame.
  // Blocks that already finished must not re-animate on every repaint, or the
  // whole answer flickers. Only the block still being written animates in.
  let settled = 0;
  const mark = (host) => {
    const kids = [...host.children];
    kids.forEach((el, i) => el.classList.add(i < settled ? 'settled' : 'rise'));
    settled = Math.max(settled, kids.length - 1);
  };

  const paint = () => {
    dirty = true;
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!dirty) return;
      dirty = false;
      body.innerHTML = md(answer) + '<span class="caret"></span>';
      mark(body);
      bottom();
    });
  };

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: state.model, messages: state.messages }),
      signal: state.abort.signal,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || err.error || res.statusText);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.trim()) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }

        if (evt.type === 'thinking_delta') {
          if (!thoughts) body.innerHTML = '';
          thoughts += evt.text;
          paintThoughts();
        } else if (evt.type === 'thinking') {
          // CLI backend hands over the whole reasoning block at once.
          thoughts = evt.text;
          updateThinking(turnEl, thoughts, false);
          collapseThinking(turnEl);
        } else if (evt.type === 'delta') {
          if (thoughts && !collapsed) {
            collapsed = true;
            cancelAnimationFrame(thoughtFrame);
            thoughtFrame = 0;
            updateThinking(turnEl, thoughts, false);
            collapseThinking(turnEl);
          }
          answer += evt.text;
          paint();
        } else if (evt.type === 'done') {
          cancelAnimationFrame(frame);
          cancelAnimationFrame(thoughtFrame);
          if (thoughts && !collapsed) collapseThinking(turnEl);
          settled = 0;
          body.innerHTML = md(answer);
          [...body.children].forEach((el) => el.classList.add('settled'));
          const tail = document.createElement('div');
          tail.className = 'tail';
          tail.innerHTML = `
            <span>${evt.model}</span>
            <span>${money(evt.usd)}</span>
            <span>${num(evt.prompt_tokens)} in · ${num(evt.completion_tokens)} out</span>
            <span>${num(evt.latency_ms)} ms</span>`;
          body.append(tail);
          $('#turn-meta').textContent = `${money(evt.usd)} · ${num(evt.latency_ms)} ms`;
        } else if (evt.type === 'error') {
          throw new Error(evt.message);
        }
      }
    }
    if (answer) state.messages.push({ role: 'assistant', content: answer });
  } catch (err) {
    cancelAnimationFrame(frame);
    cancelAnimationFrame(thoughtFrame);

    if (err.name === 'AbortError') {
      // Keep whatever arrived and mark the turn as stopped, rather than
      // discarding partial work.
      if (answer) {
        body.innerHTML = md(answer);
        [...body.children].forEach((el) => el.classList.add('settled'));
        const note = document.createElement('div');
        note.className = 'tail';
        note.innerHTML = '<span>stopped</span>';
        body.append(note);
        state.messages.push({ role: 'assistant', content: answer });
      } else {
        turnEl.remove();
      }
      if (thoughts && !collapsed) collapseThinking(turnEl);
      $('#turn-meta').textContent = 'stopped';
    } else {
      turnEl.remove();
      const oops = document.createElement('div');
      oops.className = 'oops';
      oops.textContent = err.message;
      $('#thread').append(oops);
    }
  } finally {
    state.busy = false;
    state.abort = null;
    $('#stop').hidden = true;
    $('#send').hidden = false;
    $('#send').disabled = false;
    bottom();
  }
}

/* ── usage ───────────────────────────────────────────────── */

function tick(el, target, format) {
  const from = Number(el.dataset.v || 0);
  const to = Number(target || 0);
  el.dataset.v = to;
  if (from === to) { el.textContent = format(to); return; }

  const t0 = performance.now();
  const step = (now) => {
    const p = Math.min(1, (now - t0) / 500);
    el.textContent = format(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function loadUsage() {
  try {
    const [stats, log] = await Promise.all([
      api('/stats'), api('/usage?limit=200' + filters()),
    ]);

    state.rate = stats.usd_per_credit ?? state.rate;
    state.entries = log.entries || [];

    paintBudget(stats.budget);

    tick($('#s-usd'), stats.totals.usd, money);
    tick($('#s-req'), stats.totals.requests, (v) => num(Math.round(v)));
    tick($('#s-tin'), stats.totals.prompt_tokens, (v) => num(Math.round(v)));
    tick($('#s-tout'), stats.totals.completion_tokens, (v) => num(Math.round(v)));
    tick($('#s-ips'), stats.totals.unique_ips, (v) => num(Math.round(v)));
    tick($('#s-lat'), stats.totals.avg_latency_ms, (v) => num(Math.round(v)));

    $('#s-usd-24').textContent = `${money(stats.last_24h.usd)} in 24h`;
    $('#s-req-24').textContent = `${num(stats.last_24h.requests)} in 24h`;
    $('#rate-note').textContent =
      `Each call costs one credit-weighted request, priced at $${state.rate} per credit. ` +
      `The engine reports no billing data, so these are close estimates.`;

    paintGraph(stats.series || []);
    paintRanks('#by-model', (stats.by_model || []).map((r) =>
      ({ label: r.model, value: r.usd, sub: `${num(r.requests)} req` })));
    paintRanks('#by-ip', (stats.by_ip || []).map((r) =>
      ({ label: r.ip || '—', value: r.usd, sub: `${num(r.requests)} req` })));
    paintSelects(stats);
    paintLog();
  } catch (err) {
    if (err.message !== 'forbidden') status('bad', 'error');
  }
}

function paintBudget(b) {
  if (!b) return;

  const used = Math.min(100, b.percent_used);
  const left = Math.max(0, 100 - used);
  // Colour tracks depletion; the bar length tracks what remains, so it
  // agrees with the "credits remaining" headline above it.
  const level = left <= 10 ? 'high' : left <= 30 ? 'mid' : '';

  const headline = $('#b-usd');
  headline.textContent = money(b.remaining_usd);
  headline.className = level;   // keeps the figure and the bar telling one story
  $('#b-credits').textContent = `${credits(b.remaining_credits)} of ${credits(b.allowance_credits)} credits`;
  $('#b-plan').textContent = b.plan;
  $('#b-reset').textContent = `${b.period_label} · ${until(b.reset)}`;

  const fill = $('#b-fill');
  fill.className = 'gauge-fill ' + level + (left === 0 ? ' empty' : '');
  // Deferred so the width transition actually runs on first paint.
  requestAnimationFrame(() => { fill.style.width = left + '%'; });

  $('#b-used-usd').textContent = money(b.used_usd);
  $('#b-used-credits').textContent = `${credits(b.used_credits)} credits`;
  $('#b-left-usd').textContent = money(b.remaining_usd);
  $('#b-requests').textContent = `${num(b.requests)} requests this period`;

  const over = $('#b-over');
  over.hidden = !b.over_credits;
  if (b.over_credits) $('#b-over-usd').textContent = money(b.over_usd);

  // Rail summary mirrors the headline figure.
  $('#mini-budget').hidden = false;
  $('#mini-usd').textContent = money(b.remaining_usd);
  $('#mini-label').textContent = b.over_credits ? 'over allowance' : 'available';
  const mini = $('#mini-fill');
  mini.style.width = left + '%';
  mini.style.background = level === 'high' ? 'var(--danger)'
    : level === 'mid' ? 'var(--warn)' : 'var(--money)';
}

function paintGraph(series) {
  const peak = Math.max(...series.map((p) => p.usd), 0.0001);
  $('#chart-peak').textContent = `peak ${money(peak)}/h`;
  $('#chart').innerHTML = series.map((p, i) => {
    // Floor real bars at 8% so a small hour is still legible; idle hours are
    // styled as a flat stub instead of being scaled at all.
    const h = p.usd ? Math.max(8, (p.usd / peak) * 100) : 0;
    const back = series.length - 1 - i;
    const when = back === 0 ? 'this hour' : `${back}h ago`;
    return `<div class="col ${p.usd ? '' : 'nil'}" style="height:${h}%;animation-delay:${i * 14}ms">
      <span>${money(p.usd)} · ${when}</span></div>`;
  }).join('');
}

function paintRanks(sel, rows) {
  const host = $(sel);
  if (!rows.length) { host.innerHTML = '<p class="fine">No data yet.</p>'; return; }
  const peak = Math.max(...rows.map((r) => r.value), 0.0001);
  host.innerHTML = rows.slice(0, 8).map((r, i) => `
    <div class="rank" style="animation-delay:${i * 32}ms">
      <div class="rank-top"><code>${esc(r.label)}</code><b>${money(r.value)} · ${r.sub}</b></div>
      <div class="rank-bar"><i style="width:${(r.value / peak) * 100}%;animation-delay:${i * 32}ms"></i></div>
    </div>`).join('');
}

function filters() {
  const m = $('#filter-model')?.value || '';
  const ip = $('#filter-ip')?.value || '';
  return (m ? `&model=${encodeURIComponent(m)}` : '') + (ip ? `&ip=${encodeURIComponent(ip)}` : '');
}

function paintSelects(stats) {
  const fill = (select, values, all) => {
    const prev = select.value;
    select.innerHTML = `<option value="">${all}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    select.value = prev;
  };
  fill($('#filter-model'), (stats.by_model || []).map((r) => r.model).filter(Boolean), 'All models');
  fill($('#filter-ip'), (stats.by_ip || []).map((r) => r.ip).filter(Boolean), 'All addresses');
}

function paintLog() {
  $('#log-empty').hidden = state.entries.length > 0;
  $('#log-body').innerHTML = state.entries.map((r, i) => `
    <tr style="animation-delay:${Math.min(i * 10, 240)}ms">
      <td>${ago(r.ts)}</td>
      <td><code>${esc(r.ip || '—')}</code></td>
      <td><code>${esc(r.model || '—')}</code></td>
      <td class="r">${num(r.prompt_tokens)}</td>
      <td class="r">${num(r.completion_tokens)}</td>
      <td class="r">${money(r.usd)}</td>
      <td class="r">${num(r.latency_ms)} ms</td>
      <td><span class="pill ${r.source === 'web' ? 'mine' : ''}">${esc(r.source)}</span></td>
      <td><span class="pill ${r.status < 400 ? 'ok' : 'bad'}"${r.error ? ` title="${esc(r.error)}"` : ''}>${r.status}</span></td>
    </tr>`).join('');
}

/* ── keys ────────────────────────────────────────────────── */

async function loadKeys() {
  try {
    const data = await api('/keys');
    state.keys = data.keys || [];
    $('#keys-empty').hidden = state.keys.length > 0;

    $('#keys-body').innerHTML = state.keys.map((k, i) => `
      <tr style="animation-delay:${i * 26}ms">
        <td><code>${esc(k.prefix)}…</code></td>
        <td>${esc(k.name || '—')}</td>
        <td>${ago(k.created_at)}</td>
        <td>${k.last_used ? ago(k.last_used) : 'never'}</td>
        <td><code>${esc(k.last_ip || '—')}</code></td>
        <td class="r">${num(k.requests)}</td>
        <td class="r">${money(k.usd)}</td>
        <td><span class="pill ${k.revoked ? 'bad' : 'ok'}">${k.revoked ? 'revoked' : 'active'}</span></td>
        <td><div class="acts">
          ${k.revoked ? '' : `<button class="btn ghost xs" data-revoke="${k.id}">Revoke</button>`}
          <button class="btn quiet-danger xs" data-del="${k.id}">Delete</button>
        </div></td>
      </tr>`).join('');

    $$('[data-revoke]').forEach((b) => b.onclick = async () => {
      await api(`/keys/${b.dataset.revoke}/revoke`, { method: 'POST' });
      toast('Key revoked');
      loadKeys();
    });
    $$('[data-del]').forEach((b) => b.onclick = async () => {
      await api(`/keys/${b.dataset.del}`, { method: 'DELETE' });
      toast('Key deleted');
      loadKeys();
    });

    paintDocsKeys();
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── whitelist ───────────────────────────────────────────── */

async function loadWhitelist() {
  try {
    const data = await api('/whitelist');
    $('#wl-body').innerHTML = (data.entries || []).map((e, i) => `
      <tr style="animation-delay:${i * 26}ms">
        <td><code>${esc(e.ip)}</code>${e.ip === state.ip ? ' <span class="pill mine">you</span>' : ''}</td>
        <td>${esc(e.label || '—')}</td>
        <td>${e.created_at ? ago(e.created_at) : '—'}</td>
        <td><code>${esc(e.added_by || '—')}</code></td>
        <td><div class="acts">${e.root
          ? '<span class="fine">owner</span>'
          : `<button class="btn quiet-danger xs" data-wl="${esc(e.ip)}">Remove</button>`}</div></td>
      </tr>`).join('');

    $$('[data-wl]').forEach((b) => b.onclick = async () => {
      try {
        await api(`/whitelist/${encodeURIComponent(b.dataset.wl)}`, { method: 'DELETE' });
        toast('Address removed');
        loadWhitelist();
      } catch (err) { toast(err.message, 'err'); }
    });
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── docs ────────────────────────────────────────────────── */

const base = () => `${location.origin}/v1`;

function paintDocs() {
  $('#doc-base').textContent = base();
  $('#doc-models').innerHTML = state.models.map((m) => `
    <tr><td><code>${m.id}</code></td>
        <td class="r">${money(m.cost * state.rate)}</td>
        <td class="dim">${esc(m.description || '—')}</td></tr>`).join('');
  paintDocsKeys();
  paintExample();
}

function paintDocsKeys() {
  const select = $('#docs-key');
  if (!select) return;
  const prev = select.value;
  select.innerHTML = '<option value="">— your key —</option>' +
    state.keys.filter((k) => !k.revoked)
      .map((k) => `<option value="${k.prefix}…">${k.prefix}… ${esc(k.name || '')}</option>`).join('');
  select.value = prev;
  paintExample();
}

function paintExample() {
  const lang = $('.seg.active')?.dataset.lang || 'curl';
  const key = $('#docs-key')?.value || 'YOUR_KEY';
  const url = base();
  const model = state.model || 'auto';

  const samples = {
    curl: `curl ${url}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Hello"}]
  }'`,
    python: `from openai import OpenAI

client = OpenAI(base_url="${url}", api_key="${key}")

response = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)`,
    node: `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${url}",
  apiKey: "${key}",
});

const response = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(response.choices[0].message.content);`,
  };
  $('#doc-code').textContent = samples[lang];
}

/* ── settings ────────────────────────────────────────────── */

const PLANS = { Free: 50, Pro: 1000, 'Pro+': 2000, 'Pro Max': 5000, Power: 10000 };

const TOOL_NOTES = {
  off: 'The model only answers. It cannot read, change or run anything.',
  console: 'Full tool use for this console, which only whitelisted addresses can open.',
  all: 'Full tool use for the console and for every API key.',
};

function paintToolNote(level) {
  $('#tool-note').textContent = TOOL_NOTES[level] || '';
  $('#tool-warn').hidden = level !== 'all';
}

async function loadSettings() {
  try {
    const d = await api('/settings');
    $('#key-state').textContent = d.kiro_api_key_set
      ? `Set — ${d.kiro_api_key_masked}. Leave blank to keep it.`
      : 'Not set. The CLI falls back to a browser session if signed in.';
    $('#trust-tools').value = d.trust_tools || '';
    $('#rate').value = d.usd_per_credit;
    $('#plan-credits').value = d.plan_credits;
    $('#show-thinking').checked = !!d.show_thinking;
    $('#tool-access').value = d.tool_access || 'off';
    $('#tool-root').value = d.tool_root === d.env_file ? '' : (d.tool_root || '');
    paintToolNote(d.tool_access || 'off');
    $('#plan-name').value = Object.keys(PLANS).includes(d.plan_name) ? d.plan_name : 'Custom';
    $('#cli-path').textContent = d.cli;
    $('#env-path').textContent = d.env_file;
    $('#sel-state').innerHTML = d.model_selection
      ? '<span class="pill ok">supported</span>'
      : '<span class="pill bad">not supported by this CLI build</span>';
    const select = $('#default-model');
    if (select) select.value = d.default_model;
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── wiring ──────────────────────────────────────────────── */

function wire() {
  $$('.item').forEach((b) => b.onclick = () => go(b.dataset.view));

  // composer
  const input = $('#input');
  const grow = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 184) + 'px';
  };
  input.oninput = grow;
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#composer').requestSubmit(); }
  };
  $('#composer').onsubmit = (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = '';
    grow();
    send(text);
  };
  $('#stop').onclick = () => {
    // Aborting the fetch drops the response body, which cancels the server's
    // generator and tells the agent to stop working.
    if (state.abort) state.abort.abort();
  };
  $$('.suggest').forEach((s) => s.onclick = () => send(s.textContent));
  $('#clear-chat').onclick = () => {
    state.messages = [];
    $('#thread').innerHTML = '';
    $('#turn-meta').textContent = '';
  };

  // picker
  $('#model-btn').onclick = (e) => {
    e.stopPropagation();
    $('#model-pop').hidden ? openPop() : closePop();
  };
  $('#model-search').oninput = (e) => paintOptions(e.target.value);
  $('#model-pop').onclick = (e) => e.stopPropagation();
  document.addEventListener('click', closePop);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePop(); });

  // usage
  $('#refresh-usage').onclick = loadUsage;
  $('#filter-model').onchange = loadUsage;
  $('#filter-ip').onchange = loadUsage;
  $('#clear-usage').onclick = async () => {
    if (!confirm('Delete every logged request?')) return;
    await api('/usage', { method: 'DELETE' });
    toast('Usage log cleared');
    loadUsage();
  };
  const live = $('#auto-refresh');
  const arm = () => {
    clearInterval(state.timer);
    if (live.checked) {
      state.timer = setInterval(() => { if (state.view === 'usage') loadUsage(); }, 6000);
    }
  };
  live.onchange = arm;
  arm();

  // keys
  $('#new-key').onclick = async () => {
    const name = prompt('Name this key (optional)');
    if (name === null) return;
    const btn = $('#new-key');
    btn.classList.add('busy');
    try {
      const made = await api('/keys', { method: 'POST', body: JSON.stringify({ name }) });
      $('#fresh-key-value').textContent = made.key;
      $('#fresh-key').hidden = false;
      toast('Key generated');
      loadKeys();
    } catch (err) { toast(err.message, 'err'); }
    finally { btn.classList.remove('busy'); }
  };
  $('#dismiss-key').onclick = () => { $('#fresh-key').hidden = true; };

  // whitelist
  $('#wl-form').onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api('/whitelist', {
        method: 'POST',
        body: JSON.stringify({ ip: $('#wl-ip').value, label: $('#wl-label').value }),
      });
      $('#wl-ip').value = '';
      $('#wl-label').value = '';
      toast('Address allowed');
      loadWhitelist();
    } catch (err) { toast(err.message, 'err'); }
  };

  // docs
  $$('.seg').forEach((t) => t.onclick = () => {
    $$('.seg').forEach((x) => x.classList.toggle('active', x === t));
    paintExample();
  });
  $('#docs-key').onchange = paintExample;

  // settings
  $('#tool-access').onchange = (e) => paintToolNote(e.target.value);

  $('#plan-name').onchange = (e) => {
    const preset = PLANS[e.target.value];
    if (preset) $('#plan-credits').value = preset;
  };

  $('#settings-form').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('button[type=submit]', e.target);
    const note = $('#save-note');
    btn.classList.add('busy');
    note.classList.remove('on', 'bad');

    const payload = {
      default_model: $('#default-model').value,
      trust_tools: $('#trust-tools').value,
      usd_per_credit: parseFloat($('#rate').value || '0.04'),
      plan_name: $('#plan-name').value,
      plan_credits: parseFloat($('#plan-credits').value || '0'),
      show_thinking: $('#show-thinking').checked,
      tool_access: $('#tool-access').value,
      tool_root: $('#tool-root').value,
    };
    const key = $('#kiro-key').value.trim();
    if (key) payload.kiro_api_key = key;

    try {
      await api('/settings', { method: 'POST', body: JSON.stringify(payload) });
      $('#kiro-key').value = '';
      note.textContent = 'Saved';
      note.classList.add('on');
      await bootstrap();
      await loadSettings();
    } catch (err) {
      note.textContent = err.message;
      note.classList.add('on', 'bad');
    } finally {
      btn.classList.remove('busy');
    }
  };

  // reveal + copy
  $$('[data-reveal]').forEach((b) => b.onclick = () => {
    const f = document.getElementById(b.dataset.reveal);
    f.type = f.type === 'password' ? 'text' : 'password';
  });
  document.addEventListener('click', (e) => {
    const target = e.target.closest('[data-copy-el]');
    if (target) copy(document.getElementById(target.dataset.copyEl).textContent);

    // Code blocks are re-rendered on every stream tick, so this is delegated
    // rather than bound per button.
    const code = e.target.closest('.code-copy');
    if (code) {
      copy(decodeURIComponent(code.dataset.raw || ''));
      code.textContent = 'Copied';
      code.classList.add('done');
      setTimeout(() => {
        code.textContent = 'Copy';
        code.classList.remove('done');
      }, 1400);
    }
  });
}

$('#retry').onclick = () => location.reload();
boot();
