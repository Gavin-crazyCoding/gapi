/* Playground tab: browser-based model testing ground.
 *
 * Talks to /user/playground/chat (JWT cookie auth — no gapi key needed),
 * streams SSE chunks like a normal OpenAI client, and reads the settled
 * usage record back from /user/usage/recent for the cost footer.
 * Conversation + sampling settings persist in localStorage.
 */

const STORE_KEY = 'gapi.playground.v1';
const MAX_STORED = 40;

let $;
let api;
let esc;
let coin;
let num;
let getFingerprint = () => null;

const state = {
  // {role: 'user'|'assistant', content, meta?: {model, prompt, completion, cost, ms, status}}
  messages: [],
  settings: { model: 'auto', system: '', temperature: 1, topP: 1, maxTokens: 1024 },
};
let bound = false;
let sending = false;
let abortCtrl = null;

function load() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) Object.assign(state, JSON.parse(raw));
  } catch { /* corrupt storage — start fresh */ }
}

function save() {
  try {
    const slim = {
      settings: state.settings,
      messages: state.messages.slice(-MAX_STORED),
    };
    localStorage.setItem(STORE_KEY, JSON.stringify(slim));
  } catch { /* quota / private mode — persistence is best-effort */ }
}

/* ── tiny safe markdown: escape first, then decorate ─────────────────── */

// XSS safety: esc() neutralises & < > " BEFORE any markup is added, and the
// replacements below only wrap escaped text in static tags. Model output can
// never introduce a tag, attribute, or quote — so feeding the result to
// innerHTML is safe by construction. Change this only if you add a path that
// injects unescaped model text.
function md(text) {
  let html = esc(text);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) =>
    `<pre class="pg-pre"><span class="pg-lang">${esc(lang || 'code')}</span><code>${code.replace(/\n$/, '')}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, '<code class="pg-code">$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Paragraph-ish breaks, but not inside <pre>.
  html = html.split(/(<pre[\s\S]*?<\/pre>)/g).map((part) =>
    part.startsWith('<pre') ? part : part.replace(/\n/g, '<br>')
  ).join('');
  return html;
}

/* ── rendering ────────────────────────────────────────────────────────── */

function render() {
  const box = $('pgMessages');
  const empty = $('pgEmpty');
  if (empty) empty.remove();
  box.querySelectorAll('.pg-bubble').forEach((n) => n.remove());

  for (const m of state.messages) {
    const wrap = document.createElement('div');
    wrap.className = `pg-bubble pg-${m.role}`;

    // Reasoning/thinking trace (DeepSeek-style models), collapsible.
    if (m.reasoning) {
      const det = document.createElement('details');
      det.className = 'pg-reasoning';
      det.open = !m.content; // open while thinking, collapse once the answer flows
      const sum = document.createElement('summary');
      sum.textContent = '思考过程';
      const pre = document.createElement('div');
      pre.className = 'pg-reasoning-body';
      pre.textContent = m.reasoning;
      det.appendChild(sum);
      det.appendChild(pre);
      wrap.appendChild(det);
    }

    const body = document.createElement('div');
    body.className = 'pg-bubble-body';
    body.innerHTML = m.content
      ? md(m.content)
      : '<span class="pg-cursor">▍</span>';
    wrap.appendChild(body);
    if (m.meta) {
      const f = document.createElement('div');
      f.className = 'pg-meta muted';
      const parts = [m.meta.model];
      if (m.meta.completion != null) {
        parts.push(`${num(m.meta.prompt + m.meta.completion)} tokens`);
        parts.push(coin(m.meta.cost));
      }
      if (m.meta.ms != null) parts.push(`${(m.meta.ms / 1000).toFixed(1)}s`);
      if (m.meta.status && m.meta.status !== 'ok') parts.push(m.meta.status);
      f.textContent = parts.filter(Boolean).join(' · ');
      wrap.appendChild(f);
    }
    box.appendChild(wrap);
  }
  box.scrollTop = box.scrollHeight;
}

function fillModels(models) {
  const sel = $('pgModel');
  const current = state.settings.model;
  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = 'auto';
  auto.textContent = 'auto（跟随我的路由策略）';
  sel.appendChild(auto);

  const routers = models.filter((m) => m.is_router);
  const concrete = models.filter((m) => !m.is_router);

  function group(label, list) {
    if (!list.length) return;
    const g = document.createElement('optgroup');
    g.label = label;
    for (const m of list) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.supports_streaming ? m.id : `${m.id}（不支持流式）`;
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  group('路由器', routers);
  group('具体模型', concrete);
  sel.value = current;
  if (sel.value !== current) sel.value = 'auto';
}

/* ── sending ──────────────────────────────────────────────────────────── */

async function send() {
  if (sending) return;
  const input = $('pgInput');
  const text = input.value.trim();
  if (!text) return;

  state.messages.push({ role: 'user', content: text });
  const assistant = { role: 'assistant', content: '', reasoning: '' };
  state.messages.push(assistant);
  input.value = '';
  autoGrow(input);
  save();
  render();
  setSending(true);

  // Everything except the empty assistant placeholder we just pushed.
  const history = state.messages
    .slice(0, -1)
    .filter((m) => m.content)
    .map((m) => ({ role: m.role, content: m.content }));
  const messages = [];
  if (state.settings.system.trim()) {
    messages.push({ role: 'system', content: state.settings.system.trim() });
  }
  messages.push(...history);

  const body = {
    model: state.settings.model || 'auto',
    messages,
    stream: true,
    stream_options: { include_usage: true },
    temperature: Number(state.settings.temperature),
    top_p: Number(state.settings.topP),
    max_tokens: Number(state.settings.maxTokens) || 1024,
  };

  const started = performance.now();
  abortCtrl = new AbortController();
  let usage = null;
  let streamedModel = null;
  let errorDetail = null;

  try {
    const headers = { 'Content-Type': 'application/json' };
    const fp = getFingerprint();
    if (fp) headers['X-Fingerprint'] = fp;
    const res = await fetch('/user/playground/chat', {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: JSON.stringify(body),
      signal: abortCtrl.signal,
    });

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const j = await res.json(); detail = j?.error?.message || j?.detail || detail; } catch { /* non-JSON */ }
      errorDetail = detail;
    } else {

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const payload = trimmed.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        try {
          const evt = JSON.parse(payload);
          if (evt.model) streamedModel = evt.model;
          const delta = evt.choices?.[0]?.delta;
          if (delta?.reasoning_content) {
            assistant.reasoning = (assistant.reasoning || '') + delta.reasoning_content;
            render();
          }
          if (delta?.content) {
            assistant.content += delta.content;
            render();
          }
          if (evt.usage) usage = evt.usage;
        } catch { /* keepalive / partial frame */ }
      }
    }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      assistant.content = assistant.content || '（已停止）';
    } else {
      errorDetail = `网络错误：${e.message}`;
    }
  } finally {
    abortCtrl = null;
  }

  if (errorDetail) {
    assistant.content = `⚠️ ${errorDetail}`;
    assistant.meta = { model: body.model, status: 'error', ms: performance.now() - started };
    save();
    render();
    setSending(false);
    return;
  }

  // Prefer the upstream usage frame; fall back to gapi's settled usage record.
  let meta = { model: streamedModel || body.model, ms: performance.now() - started, status: 'ok' };
  if (usage) {
    meta.prompt = usage.prompt_tokens || 0;
    meta.completion = usage.completion_tokens || 0;
  }
  try {
    const recent = await api('/user/usage/recent?limit=1');
    const rec = Array.isArray(recent) ? recent[0] : null;
    if (rec && rec.endpoint === '/playground/chat') {
      // Ledger stores the requested model ("auto"); prefer the concrete model
      // the stream actually came back with when we have it.
      if (rec.model && rec.model !== 'auto') meta.model = rec.model;
      else if (streamedModel) meta.model = streamedModel;
      meta.prompt = rec.prompt_tokens;
      meta.completion = rec.completion_tokens;
      meta.cost = rec.gavincoin_cost;
      meta.ms = rec.duration_ms;
      meta.status = rec.status;
    }
  } catch { /* cost display is best-effort */ }
  assistant.content = assistant.content || '（模型没有返回内容）';
  assistant.meta = meta;

  save();
  render();
  setSending(false);
}

function setSending(on) {
  sending = on;
  const btn = $('pgSend');
  btn.textContent = on ? '停止' : '发送';
  btn.classList.toggle('pg-stop', on);
  $('pgInput').disabled = on;
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

/* ── wiring ───────────────────────────────────────────────────────────── */

function bind() {
  if (bound) return;
  bound = true;

  $('pgSend').addEventListener('click', () => {
    if (sending && abortCtrl) abortCtrl.abort();
    else send();
  });

  const input = $('pgInput');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener('input', () => autoGrow(input));

  $('pgClearBtn').addEventListener('click', () => {
    if (sending) return;
    state.messages = [];
    save();
    render();
    input.focus();
  });

  $('pgSettingsBtn').addEventListener('click', () => {
    $('pgSettings').classList.toggle('hidden');
  });

  $('pgModel').addEventListener('change', (e) => { state.settings.model = e.target.value; save(); });

  const system = $('pgSystem');
  system.addEventListener('input', () => { state.settings.system = system.value; save(); });

  const temp = $('pgTemp');
  const topP = $('pgTopP');
  temp.addEventListener('input', () => {
    state.settings.temperature = Number(temp.value);
    $('pgTempVal').textContent = Number(temp.value).toFixed(1);
    save();
  });
  topP.addEventListener('input', () => {
    state.settings.topP = Number(topP.value);
    $('pgTopPVal').textContent = Number(topP.value).toFixed(2);
    save();
  });
  $('pgMaxTokens').addEventListener('input', (e) => {
    state.settings.maxTokens = Number(e.target.value) || 1024;
    save();
  });
}

export async function init(ctx) {
  $ = ctx.$;
  api = ctx.api;
  esc = ctx.esc;
  coin = ctx.coin;
  num = ctx.num;
  if (ctx.getFingerprint) getFingerprint = ctx.getFingerprint;

  load();
  bind();

  // Restore form values from saved settings.
  $('pgModel').value = state.settings.model;
  $('pgSystem').value = state.settings.system || '';
  $('pgTemp').value = state.settings.temperature;
  $('pgTempVal').textContent = Number(state.settings.temperature).toFixed(1);
  $('pgTopP').value = state.settings.topP;
  $('pgTopPVal').textContent = Number(state.settings.topP).toFixed(2);
  $('pgMaxTokens').value = state.settings.maxTokens;
}

export async function refresh(ctx) {
  if (!$) await init(ctx);
  try {
    const d = await api('/user/models');
    fillModels(d.models || []);
  } catch { /* model picker keeps auto only */ }
  render();
  $('pgInput').focus();
}
