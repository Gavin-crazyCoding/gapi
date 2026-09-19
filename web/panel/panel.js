/* Panel core — served only with a valid session.
 *
 * Tab modules are downloaded ON DEMAND the first time their tab opens, so a
 * normal session only fetches code for features the user actually uses.
 * Every module exports { init(ctx), refresh(ctx) }.
 */

const $ = (id) => document.getElementById(id);

// 浏览器/环境指纹（同源 /fp.js，SHA-256 哈希），注入到所有面板请求头
let fingerprintHash = null;
(async () => {
    try {
        if (window.GapiFingerprint) fingerprintHash = await window.GapiFingerprint.collect();
    } catch (e) {
        console.warn('[fingerprint] 采集失败，继续不带指纹:', e);
    }
})();

function copyCode(btn) {
  const code = btn.closest('.codeblock').querySelector('code');
  const text = code.textContent || code.innerText;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = '✓ 已复制';
    btn.classList.add('ok');
    setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('ok'); }, 1500);
  });
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  // 注入指纹（若已生成）
  if (fingerprintHash) headers['X-Fingerprint'] = fingerprintHash;
  // The HttpOnly session cookie rides along automatically; no token in JS.
  const res = await fetch(path, { ...options, headers });
  if (res.status === 204) return null;

  let body = null;
  try { body = await res.json(); } catch { /* empty or non-JSON */ }

  if (!res.ok) {
    if (res.status === 401) { window.location.replace('/'); return new Promise(() => {}); }
    const err = new Error(body?.error?.message || body?.detail || `HTTP ${res.status}`);
    err.code = body?.error?.code;
    err.status = res.status;
    throw err;
  }
  return body;
}

/* ── shared formatting (used by every tab module) ─────────────────── */

function coin(v) {
  if (v === null || v === undefined || v === '') return '◎ –';
  const n = Number(v);
  if (!isFinite(n)) return '◎ –';
  const a = Math.abs(n);
  const s = a >= 1 || a === 0 ? a.toFixed(2) : a.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
  return `${n < 0 ? '-' : ''}◎ ${s}`;
}
const num = (v) => Number(v || 0).toLocaleString();
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function when(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d)) return '–';
  return d.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function table(cols, rows, emptyText) {
  if (!rows.length) return `<div class="empty">${esc(emptyText)}</div>`;
  const head = cols.map((c) => `<th class="${c.num ? 'num' : ''} ${c.cls || ''}">${esc(c.label)}</th>`).join('');
  const body = rows.map((r) =>
    `<tr>${cols.map((c) => `<td class="${c.num ? 'num' : ''} ${c.cls || ''}">${c.get(r)}</td>`).join('')}</tr>`
  ).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

export const ctx = { $, api, coin, num, esc, when, table, config: { tokenRate: 100 }, getFingerprint: () => fingerprintHash };

/* ── announcement popup (non-admin users see once-per-login) ───────── */

async function showAnnouncementPopup() {
  // Admins manage announcements via the panel; regular users get a one-shot
  // modal on each login until they dismiss it.
  try {
    const list = await api('/user/announcements');
    if (!list || !list.length) return;
    const a = list[0]; // newest first
    // Plain-text announcements MUST be escaped before markup is added — only
    // the explicit as_html mode (trusted admin authors) injects raw markup.
    const html = a.as_html ? a.content : esc(a.content).replace(/\n/g, '<br>');
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal">
        <h3>${esc(a.title)}</h3>
        <div class="ann-content">${html}</div>
        <div style="margin-top:16px;text-align:right">
          <button class="primary" id="annDismiss">我知道了</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('#annDismiss').onclick = async () => {
      overlay.remove();
      try { await api('/user/announcements/read', { method: 'POST' }); } catch {}
    };
    overlay.onclick = (e) => { if (e.target === overlay) { overlay.remove(); } };
  } catch {}
}

/* ── toast ──────────────────────────────────────────────────────────── */

function showToast(msg, ms = 2400) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 400); }, ms);
}

/* ── boot ─────────────────────────────────────────────────────────── */

const TABS = ['overview', 'playground', 'models', 'keys', 'docs', 'billing', 'usage', 'stats', 'settings', 'users', 'announcements'];
const ADMIN_TABS = ['stats', 'settings', 'users', 'announcements'];
const loaded = new Map();
// 角色由 /auth/me 确认前一律按非管理员处理——隐藏与点击守卫都以服务端角色为准。
let isAdminUser = false;

async function openTab(name) {
  let mod = loaded.get(name);
  if (!mod) {
    mod = await import(`/panel/assets/tabs/${name}.js?v=20260919a`);
    await mod.init(ctx);
    loaded.set(name, mod);
  }
  await mod.refresh(ctx);
}

const headerEl = document.querySelector('header');
const navEl = $('mainNav');
const navToggle = $('navToggle');

function closeNav() {
  if (!navEl || !navToggle) return;
  navEl.classList.remove('open');
  navToggle.setAttribute('aria-expanded', 'false');
}

/* Mobile browsers ALWAYS get the collapsed bar: width measurement has blind
   spots there (flex shrink wraps button text instead of overflowing), and a
   phone never has the elbow room for ten tabs anyway. Desktop uses the
   measured fit: full bar when every visible button fits, hamburger when not. */
function isMobileEnv() {
  const ua = navigator.userAgentData;
  if (ua && typeof ua.mobile === 'boolean') return ua.mobile;
  if (/Android|webOS|iPhone|iPod|BlackBerry|IEMobile|Opera Mini|Mobile/i.test(navigator.userAgent)) return true;
  // iPadOS in desktop mode reports a Mac UA but has a touchscreen.
  return navigator.maxTouchPoints > 1 && /Mac/.test(navigator.platform);
}

const MOBILE = isMobileEnv();
document.body.classList.toggle('is-mobile', MOBILE);

function fitNav() {
  if (!headerEl || !navEl) return;
  if (MOBILE) {
    headerEl.classList.add('nav-collapsed');
    return;
  }
  headerEl.classList.remove('nav-collapsed');
  // Chrome gives fractional widths; 2px slack avoids oscillation on resize.
  const overflow = navEl.scrollWidth - navEl.clientWidth;
  if (overflow > 2) headerEl.classList.add('nav-collapsed');
  else closeNav();
}

if (navEl && navToggle) {
  navToggle.onclick = (e) => {
    e.stopPropagation();
    const open = navEl.classList.toggle('open');
    navToggle.setAttribute('aria-expanded', String(open));
  };
  // Tap anywhere else closes the dropdown.
  document.addEventListener('click', (e) => {
    if (navEl.classList.contains('open') && !navEl.contains(e.target)) closeNav();
  });
  window.addEventListener('resize', fitNav);
} else {
  console.warn('[nav] mainNav/navToggle missing from the shell');
}

document.querySelectorAll('nav button').forEach((btn) => {
  btn.onclick = () => {
    const tab = btn.dataset.tab;
    // 管理员标签页不只是被 CSS 藏起来：非管理员连打开逻辑都不允许触发，
    // 避免慢网络/按钮重排瞬间用户误触管理面板（服务端仍会 403，这里是双保险）。
    if (ADMIN_TABS.includes(tab) && !isAdminUser) return;
    document.querySelectorAll('nav button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    TABS.forEach((t) => $(`tab-${t}`).classList.toggle('hidden', t !== tab));
    closeNav();
    openTab(tab).catch((err) => console.error(tab, err));
  };
});

$('logout').onclick = async () => {
  try { await api('/auth/logout', { method: 'POST' }); }
  finally { window.location.replace('/'); }
};

(async function boot() {
  try {
    const me = await api('/auth/me');
    if (!me) return; // 401 already redirected
    $('who').textContent = me.email + (me.role === 'admin' ? ' · admin' : '');
    const cfg = await api('/config');
    ctx.config = cfg;
    // 管理员标签页：服务端确认角色后才显示（HTML 默认 hidden，慢网络下
    // 普通用户不会看到管理菜单）。isAdminUser 同时驱动上面的点击守卫。
    isAdminUser = me.role === 'admin';
    ADMIN_TABS.forEach((t) => {
      document.querySelector(`nav button[data-tab="${t}"]`).classList.toggle('hidden', !isAdminUser);
    });
    fitNav();
    // Auto daily check-in: one request per page load; the server-side 24h
    // window (BEGIN IMMEDIATE guarded UPDATE) makes a refresh/duplicate tab
    // impossible to double-claim. Shows a light toast on first claim of the day.
    try {
      const r = await api('/user/checkin');
      if (r && r.checked_in && r.bonus) showToast(`签到 +${r.bonus} ◎`);
    } catch (e) { /* sign-in is best-effort; ignore if it races logout */ }
    await openTab('overview');
    // Non-admin users see the top announcement popup (once per login).
    if (me.role !== 'admin') showAnnouncementPopup();
  } catch (err) {
    console.error('panel boot failed', err);
  }
})();
