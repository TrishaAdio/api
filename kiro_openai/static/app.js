/* RioApis console.
   Vanilla, no build step. Admin access is decided by the server from the
   caller's IP, so there is no token to store client-side. */
'use strict';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  ip: '',
  brand: 'RioApis',
  models: [],
  model: 'auto',
  rate: 0.04,
  messages: [],
  streaming: false,
  view: 'chat',
  entries: [],
  keys: [],
  timer: null,
};

/* ── net ───────────────────────────────────────────────── */

async function api(path, options = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 403) { showDenied(state.ip); throw new Error('forbidden'); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || res.statusText);
  return data;
}

/* ── formatting ────────────────────────────────────────── */

const money = (n) => {
  const v = Number(n || 0);
  if (v === 0) return '$0.00';
  if (v < 0.01) return '$' + v.toFixed(4);
  return '$' + v.toFixed(2);
};

const num = (n) => Number(n || 0).toLocaleString();

function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60)    return Math.floor(s) + 's ago';
  if (s < 3600)  return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* Minimal markdown: fenced code, inline code, bold, paragraphs. */
function render(md) {
  const blocks = [];
  let text = String(md).replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    blocks.push(code.replace(/\n$/, ''));
    return `\u0000${blocks.length - 1}\u0000`;
  });

  text = escapeHtml(text)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  const html = text
    .split(/\n{2,}/)
    .map((p) => (p.trim() ? `<p>${p.replace(/\n/g, '<br>')}</p>` : ''))
    .join('');

  return html.replace(/<p>\u0000(\d+)\u0000<\/p>|\u0000(\d+)\u0000/g, (_m, a, b) =>
    `<pre><code>${escapeHtml(blocks[a ?? b])}</code></pre>`);
}

/* ── toasts ────────────────────────────────────────────── */

function toast(message, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="tico">${kind === 'ok' ? '✔' : '✘'}</span><span>${escapeHtml(message)}</span>`;
  $('#toasts').append(el);
  setTimeout(() => {
    el.classList.add('out');
    el.addEventListener('animationend', () => el.remove());
  }, 3200);
}

async function copyText(text, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
    toast(label);
  } catch {
    // Clipboard API needs a secure context; fall back to a temp selection.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    try { document.execCommand('copy'); toast(label); }
    catch { toast('Copy failed — select it manually', 'err'); }
    ta.remove();
  }
}

/* ── boot ──────────────────────────────────────────────── */

async function boot() {
  let session;
  try {
    session = await (await fetch('/api/session')).json();
  } catch {
    $('#boot').hidden = true;
    showDenied('');
    return;
  }

  state.ip = session.ip || '';
  state.brand = session.brand || 'RioApis';

  if (!session.admin) {
    $('#boot').hidden = true;
    showDenied(state.ip);
    return;
  }

  $('#boot').hidden = true;
  $('#app').hidden = false;
  $('#my-ip').textContent = state.ip || 'local';
  $('#set-ip').textContent = state.ip || 'local';

  positionGlow();
  await loadBootstrap();
  wire();
  refreshUsage();
}

function showDenied(ip) {
  $('#app').hidden = true;
  $('#denied').hidden = false;
  $('#denied-ip').textContent = ip || 'unknown';
}

async function loadBootstrap() {
  try {
    const data = await api('/bootstrap');
    state.models = data.models || [];
    state.rate = data.usd_per_credit ?? 0.04;
    state.model = data.default_model || 'auto';
    setStatus(data.ready ? 'ok' : 'bad', data.ready ? 'ready' : 'CLI unavailable');
    $('#brand-sub').textContent = data.model_selection ? 'OpenAI-compatible' : 'fixed model';
    paintModels();
    paintDocs();
  } catch (err) {
    setStatus('bad', 'error');
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

function setStatus(kind, text) {
  const dot = $('#status .dot');
  dot.className = `dot ${kind}`;
  $('#status-text').textContent = text;
}

/* ── navigation ────────────────────────────────────────── */

function positionGlow() {
  const active = $('.nav-item.active');
  const glow = $('#nav-glow');
  if (!active || !glow) return;
  glow.style.transform = `translateY(${active.offsetTop}px)`;
  glow.style.height = `${active.offsetHeight}px`;
}

function go(view) {
  state.view = view;
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.dataset.view === view));
  positionGlow();

  if (view === 'usage')  refreshUsage();
  if (view === 'keys')   refreshKeys();
  if (view === 'access') refreshWhitelist();
  if (view === 'settings') loadSettings();
}

/* ── models ────────────────────────────────────────────── */

function costOf(model) {
  const entry = state.models.find((m) => m.id === model);
  return (entry ? entry.cost : 0) * state.rate;
}

function paintModels() {
  const current = state.models.find((m) => m.id === state.model) || state.models[0];
  if (current) {
    state.model = current.id;
    $('#model-current').textContent = current.id;
    $('#model-cost').textContent = money(current.cost * state.rate);
  }

  const select = $('#default-model');
  if (select) {
    select.innerHTML = state.models
      .map((m) => `<option value="${m.id}">${m.id}</option>`).join('');
    select.value = state.model;
  }

  paintModelList('');
}

function paintModelList(query) {
  const list = $('#model-list');
  const q = query.trim().toLowerCase();
  const rows = state.models.filter((m) => !q || m.id.toLowerCase().includes(q));

  if (!rows.length) {
    list.innerHTML = '<p class="muted tiny pad">No match</p>';
    return;
  }

  list.innerHTML = rows.map((m, i) => `
    <button class="model-row ${m.id === state.model ? 'sel' : ''}" data-model="${m.id}"
            style="animation-delay:${Math.min(i * 18, 220)}ms">
      <span class="mid">${m.id}</span>
      <span class="cost-pill">${money(m.cost * state.rate)}</span>
      ${m.description ? `<span class="mdesc">${escapeHtml(m.description)}</span>` : ''}
    </button>`).join('');

  $$('.model-row', list).forEach((row) => {
    row.onclick = () => {
      state.model = row.dataset.model;
      paintModels();
      closeModelPop();
    };
  });
}

function openModelPop() {
  $('#model-pop').hidden = false;
  $('#model-btn').setAttribute('aria-expanded', 'true');
  $('#model-search').value = '';
  paintModelList('');
  $('#model-search').focus();
}

function closeModelPop() {
  $('#model-pop').hidden = true;
  $('#model-btn').setAttribute('aria-expanded', 'false');
}

/* ── chat ──────────────────────────────────────────────── */

function addMessage(role, content) {
  $('#empty-chat')?.remove();
  const wrap = document.createElement('div');
  wrap.className = `msg ${role === 'user' ? 'user' : 'bot'}`;
  wrap.innerHTML = `
    <div class="avatar">${role === 'user' ? 'You' : '◆'}</div>
    <div class="bubble"></div>`;
  const bubble = $('.bubble', wrap);
  if (role === 'user') bubble.textContent = content;
  $('#thread').append(wrap);
  scrollThread();
  return bubble;
}

function scrollThread() {
  const thread = $('#thread');
  thread.scrollTop = thread.scrollHeight;
}

async function send(text) {
  if (!text.trim() || state.streaming) return;

  state.messages.push({ role: 'user', content: text });
  addMessage('user', text);

  const bubble = addMessage('assistant', '');
  bubble.innerHTML = '<span class="thinking"><i></i><i></i><i></i></span>';

  state.streaming = true;
  $('#send').disabled = true;
  $('#turn-meta').textContent = '';

  let answer = '';
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: state.model, messages: state.messages }),
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

        if (evt.type === 'delta') {
          answer += evt.text;
          bubble.innerHTML = render(answer) + '<span class="cursor"></span>';
          scrollThread();
        } else if (evt.type === 'done') {
          bubble.innerHTML = render(answer);
          const foot = document.createElement('div');
          foot.className = 'msg-foot';
          foot.innerHTML = `
            <span>${evt.model}</span>
            <span>${money(evt.usd)}</span>
            <span>${num(evt.prompt_tokens)} in · ${num(evt.completion_tokens)} out</span>
            <span>${evt.latency_ms} ms</span>`;
          bubble.append(foot);
          $('#turn-meta').textContent = `${money(evt.usd)} · ${evt.latency_ms} ms`;
        } else if (evt.type === 'error') {
          throw new Error(evt.message);
        }
      }
    }

    if (answer) state.messages.push({ role: 'assistant', content: answer });
  } catch (err) {
    bubble.closest('.msg').remove();
    const box = document.createElement('div');
    box.className = 'err-msg';
    box.textContent = err.message;
    $('#thread').append(box);
    scrollThread();
  } finally {
    state.streaming = false;
    $('#send').disabled = false;
    scrollThread();
  }
}

/* ── usage ─────────────────────────────────────────────── */

function countUp(el, target, format) {
  const from = Number(el.dataset.v || 0);
  const to = Number(target || 0);
  el.dataset.v = to;
  if (from === to) { el.textContent = format(to); return; }

  const start = performance.now();
  const dur = 520;
  const tick = (now) => {
    const t = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = format(from + (to - from) * eased);
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

async function refreshUsage() {
  try {
    const [stats, log] = await Promise.all([
      api('/stats'),
      api('/usage?limit=200' + filterQuery()),
    ]);

    state.rate = stats.usd_per_credit ?? state.rate;
    state.entries = log.entries || [];

    countUp($('#s-usd'),  stats.totals.usd,               money);
    countUp($('#s-req'),  stats.totals.requests,          (v) => num(Math.round(v)));
    countUp($('#s-tin'),  stats.totals.prompt_tokens,     (v) => num(Math.round(v)));
    countUp($('#s-tout'), stats.totals.completion_tokens, (v) => num(Math.round(v)));
    countUp($('#s-ips'),  stats.totals.unique_ips,        (v) => num(Math.round(v)));
    countUp($('#s-lat'),  stats.totals.avg_latency_ms,    (v) => num(Math.round(v)));

    $('#s-usd-24').textContent = `${money(stats.last_24h.usd)} in 24h`;
    $('#s-req-24').textContent = `${num(stats.last_24h.requests)} in 24h`;
    $('#rate-note').textContent =
      `Cost is estimated as one credit-weighted request per call at $${state.rate} per credit. ` +
      `The Kiro CLI reports no billing data, so treat these as approximations.`;

    paintChart(stats.series || []);
    paintBars('#by-model', (stats.by_model || []).map((r) => ({ label: r.model, value: r.usd, sub: `${num(r.requests)} req` })));
    paintBars('#by-ip',    (stats.by_ip || []).map((r) => ({ label: r.ip || '—', value: r.usd, sub: `${num(r.requests)} req` })));
    paintFilters(stats);
    paintLog();
  } catch (err) {
    if (err.message !== 'forbidden') setStatus('bad', 'error');
  }
}

function paintChart(series) {
  const peak = Math.max(...series.map((p) => p.usd), 0.0001);
  $('#chart-peak').textContent = `peak ${money(peak)}/h`;
  $('#chart').innerHTML = series.map((p, i) => {
    const pct = Math.max(2, (p.usd / peak) * 100);
    const hoursAgo = series.length - 1 - i;
    return `<div class="bar ${p.usd ? '' : 'zero'}"
                 style="height:${pct}%;animation-delay:${i * 22}ms">
              <span>${money(p.usd)} · ${hoursAgo === 0 ? 'now' : hoursAgo + 'h ago'}</span>
            </div>`;
  }).join('');
}

function paintBars(selector, rows) {
  const host = $(selector);
  if (!rows.length) { host.innerHTML = '<p class="muted tiny">No data yet.</p>'; return; }
  const peak = Math.max(...rows.map((r) => r.value), 0.0001);
  host.innerHTML = rows.slice(0, 8).map((r, i) => `
    <div class="brow" style="animation-delay:${i * 40}ms">
      <div class="brow-top">
        <code>${escapeHtml(r.label)}</code>
        <b>${money(r.value)} · ${r.sub}</b>
      </div>
      <div class="meter"><i style="width:${(r.value / peak) * 100}%;animation-delay:${i * 40}ms"></i></div>
    </div>`).join('');
}

function filterQuery() {
  const model = $('#filter-model')?.value || '';
  const ip = $('#filter-ip')?.value || '';
  return (model ? `&model=${encodeURIComponent(model)}` : '') +
         (ip ? `&ip=${encodeURIComponent(ip)}` : '');
}

function paintFilters(stats) {
  const keep = (select, values, all) => {
    const previous = select.value;
    select.innerHTML = `<option value="">${all}</option>` +
      values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
    select.value = previous;
  };
  keep($('#filter-model'), (stats.by_model || []).map((r) => r.model).filter(Boolean), 'All models');
  keep($('#filter-ip'), (stats.by_ip || []).map((r) => r.ip).filter(Boolean), 'All IPs');
}

function paintLog() {
  const body = $('#log-body');
  const rows = state.entries;
  $('#log-empty').hidden = rows.length > 0;

  body.innerHTML = rows.map((r, i) => `
    <tr style="animation-delay:${Math.min(i * 12, 260)}ms">
      <td>${ago(r.ts)}</td>
      <td><code>${escapeHtml(r.ip || '—')}</code></td>
      <td><code>${escapeHtml(r.model || '—')}</code></td>
      <td class="num">${num(r.prompt_tokens)}</td>
      <td class="num">${num(r.completion_tokens)}</td>
      <td class="num">${money(r.usd)}</td>
      <td class="num">${num(r.latency_ms)} ms</td>
      <td><span class="tag ${r.source === 'web' ? 'web' : ''}">${escapeHtml(r.source)}</span></td>
      <td><span class="tag ${r.status < 400 ? 'ok' : 'bad'}" ${r.error ? `title="${escapeHtml(r.error)}"` : ''}>${r.status}</span></td>
    </tr>`).join('');
}

/* ── keys ──────────────────────────────────────────────── */

async function refreshKeys() {
  try {
    const data = await api('/keys');
    state.keys = data.keys || [];
    const body = $('#keys-body');
    $('#keys-empty').hidden = state.keys.length > 0;

    body.innerHTML = state.keys.map((k, i) => `
      <tr style="animation-delay:${i * 30}ms">
        <td><code>${escapeHtml(k.prefix)}…</code></td>
        <td>${escapeHtml(k.name || '—')}</td>
        <td>${ago(k.created_at)}</td>
        <td>${k.last_used ? ago(k.last_used) : 'never'}</td>
        <td><code>${escapeHtml(k.last_ip || '—')}</code></td>
        <td class="num">${num(k.requests)}</td>
        <td class="num">${money(k.usd)}</td>
        <td><span class="tag ${k.revoked ? 'bad' : 'ok'}">${k.revoked ? 'revoked' : 'active'}</span></td>
        <td class="row-actions">
          ${k.revoked ? '' : `<button class="btn ghost tiny" data-revoke="${k.id}">Revoke</button>`}
          <button class="btn danger tiny" data-del="${k.id}">Delete</button>
        </td>
      </tr>`).join('');

    $$('[data-revoke]', body).forEach((b) => b.onclick = async () => {
      await api(`/keys/${b.dataset.revoke}/revoke`, { method: 'POST' });
      toast('Key revoked');
      refreshKeys();
    });
    $$('[data-del]', body).forEach((b) => b.onclick = async () => {
      await api(`/keys/${b.dataset.del}`, { method: 'DELETE' });
      toast('Key deleted');
      refreshKeys();
    });

    paintDocsKeys();
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── whitelist ─────────────────────────────────────────── */

async function refreshWhitelist() {
  try {
    const data = await api('/whitelist');
    const body = $('#wl-body');
    body.innerHTML = (data.entries || []).map((e, i) => `
      <tr style="animation-delay:${i * 30}ms">
        <td><code>${escapeHtml(e.ip)}</code>${e.ip === state.ip ? ' <span class="tag ok">you</span>' : ''}</td>
        <td>${escapeHtml(e.label || '—')}</td>
        <td>${e.created_at ? ago(e.created_at) : '—'}</td>
        <td><code>${escapeHtml(e.added_by || '—')}</code></td>
        <td class="row-actions">${e.root
          ? '<span class="muted tiny">owner</span>'
          : `<button class="btn danger tiny" data-wl="${escapeHtml(e.ip)}">Remove</button>`}</td>
      </tr>`).join('');

    $$('[data-wl]', body).forEach((b) => b.onclick = async () => {
      try {
        await api(`/whitelist/${encodeURIComponent(b.dataset.wl)}`, { method: 'DELETE' });
        toast('Address removed');
        refreshWhitelist();
      } catch (err) { toast(err.message, 'err'); }
    });
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── docs ──────────────────────────────────────────────── */

function baseUrl() { return `${location.origin}/v1`; }

function paintDocs() {
  $('#doc-base').textContent = baseUrl();
  $('#doc-models').innerHTML = state.models.map((m) => `
    <tr>
      <td><code>${m.id}</code></td>
      <td class="num">${money(m.cost * state.rate)}</td>
      <td class="muted">${escapeHtml(m.description || '—')}</td>
    </tr>`).join('');
  paintDocsKeys();
  paintExample();
}

function paintDocsKeys() {
  const select = $('#docs-key');
  if (!select) return;
  const previous = select.value;
  const active = state.keys.filter((k) => !k.revoked);
  select.innerHTML = '<option value="">— your key —</option>' +
    active.map((k) => `<option value="${k.prefix}…">${k.prefix}… ${escapeHtml(k.name || '')}</option>`).join('');
  select.value = previous;
  paintExample();
}

function paintExample() {
  const lang = $('.tab.active')?.dataset.lang || 'curl';
  const key = $('#docs-key')?.value || 'YOUR_KEY';
  const url = baseUrl();
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

/* ── settings ──────────────────────────────────────────── */

async function loadSettings() {
  try {
    const data = await api('/settings');
    $('#key-state').textContent = data.kiro_api_key_set
      ? `Currently set — ${data.kiro_api_key_masked}. Leave blank to keep it.`
      : 'No key set. The CLI will fall back to a browser session if signed in.';
    $('#trust-tools').value = data.trust_tools || '';
    $('#rate').value = data.usd_per_credit;
    $('#cli-path').textContent = data.cli;
    $('#env-path').textContent = data.env_file;
    $('#sel-state').innerHTML = data.model_selection
      ? '<span class="tag ok">supported</span>'
      : '<span class="tag bad">not supported by this CLI build</span>';
    const select = $('#default-model');
    if (select) select.value = data.default_model;
  } catch (err) {
    if (err.message !== 'forbidden') toast(err.message, 'err');
  }
}

/* ── wiring ────────────────────────────────────────────── */

function wire() {
  $$('.nav-item').forEach((b) => b.onclick = () => go(b.dataset.view));
  window.addEventListener('resize', positionGlow);

  // composer
  const input = $('#input');
  const grow = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 190) + 'px';
  };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('#composer').requestSubmit();
    }
  });
  $('#composer').onsubmit = (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = '';
    grow();
    send(text);
  };
  $$('.chip').forEach((c) => c.onclick = () => send(c.textContent));
  $('#clear-chat').onclick = () => {
    state.messages = [];
    $('#thread').innerHTML = '';
    $('#turn-meta').textContent = '';
  };

  // model picker
  $('#model-btn').onclick = (e) => {
    e.stopPropagation();
    $('#model-pop').hidden ? openModelPop() : closeModelPop();
  };
  $('#model-search').oninput = (e) => paintModelList(e.target.value);
  $('#model-pop').onclick = (e) => e.stopPropagation();
  document.addEventListener('click', closeModelPop);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModelPop();
  });

  // usage
  $('#refresh-usage').onclick = refreshUsage;
  $('#filter-model').onchange = refreshUsage;
  $('#filter-ip').onchange = refreshUsage;
  $('#clear-usage').onclick = async () => {
    if (!confirm('Delete every logged request?')) return;
    await api('/usage', { method: 'DELETE' });
    toast('Usage log cleared');
    refreshUsage();
  };
  const live = $('#auto-refresh');
  const setTimer = () => {
    clearInterval(state.timer);
    if (live.checked) state.timer = setInterval(() => {
      if (state.view === 'usage') refreshUsage();
    }, 6000);
  };
  live.onchange = setTimer;
  setTimer();

  // keys
  $('#new-key').onclick = async () => {
    const name = prompt('Name this key (optional)') ?? '';
    const btn = $('#new-key');
    btn.classList.add('loading');
    try {
      const created = await api('/keys', {
        method: 'POST',
        body: JSON.stringify({ name }),
      });
      $('#fresh-key-value').textContent = created.key;
      $('#fresh-key').hidden = false;
      toast('Key generated');
      refreshKeys();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      btn.classList.remove('loading');
    }
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
      toast('Address whitelisted');
      refreshWhitelist();
    } catch (err) { toast(err.message, 'err'); }
  };

  // docs
  $$('.tab').forEach((t) => t.onclick = () => {
    $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
    paintExample();
  });
  $('#docs-key').onchange = paintExample;

  // settings
  $('#settings-form').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('button[type=submit]', e.target);
    const note = $('#save-note');
    btn.classList.add('loading');
    note.classList.remove('show', 'err');

    const payload = {
      default_model: $('#default-model').value,
      trust_tools: $('#trust-tools').value,
      usd_per_credit: parseFloat($('#rate').value || '0.04'),
    };
    const key = $('#kiro-key').value.trim();
    if (key) payload.kiro_api_key = key;

    try {
      await api('/settings', { method: 'POST', body: JSON.stringify(payload) });
      $('#kiro-key').value = '';
      note.textContent = 'Saved';
      note.classList.add('show');
      await loadBootstrap();
      await loadSettings();
    } catch (err) {
      note.textContent = err.message;
      note.classList.add('show', 'err');
    } finally {
      btn.classList.remove('loading');
    }
  };

  // reveal / copy
  $$('[data-reveal]').forEach((b) => b.onclick = () => {
    const field = document.getElementById(b.dataset.reveal);
    field.type = field.type === 'password' ? 'text' : 'password';
  });
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy-el]');
    if (!btn) return;
    copyText(document.getElementById(btn.dataset.copyEl).textContent);
  });

  // ripple origin for buttons
  document.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest('.btn');
    if (!btn) return;
    const box = btn.getBoundingClientRect();
    btn.style.setProperty('--rx', `${((e.clientX - box.left) / box.width) * 100}%`);
    btn.style.setProperty('--ry', `${((e.clientY - box.top) / box.height) * 100}%`);
  });
}

$('#retry').onclick = () => location.reload();

boot();
