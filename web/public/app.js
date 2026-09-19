/* gapi login shell.
 * This is the only script an anonymous visitor can download. It knows about
 * exactly two endpoints; every panel API lives in the authenticated modules
 * under /panel/assets/, which the server refuses to send without a session.
 */

const $ = (id) => document.getElementById(id);
let mode = 'register';

async function fingerprint() {
  try {
    if (window.GapiFingerprint) return await window.GapiFingerprint.collect();
  } catch (e) { /* fingerprinting is best-effort */ }
  return null;
}

async function postJSON(path, body) {
  const fp = await fingerprint();
  if (fp) body.fingerprint = fp;
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error */ }
  if (!res.ok) {
    const err = new Error(data?.error?.message || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function setMode(next) {
  mode = next;
  const isReg = mode === 'register';
  $('authTitle').textContent = isReg ? '创建账号' : '登录';
  $('authSub').textContent = isReg ? '注册即可获得启动额度' : '登录以管理密钥与账单';
  $('authBtn').textContent = isReg ? '注册' : '登录';
  $('password').autocomplete = isReg ? 'new-password' : 'current-password';
  $('bonusNote').classList.toggle('hidden', !isReg);
  $('authSwitch').innerHTML = isReg
    ? '已有账号？<a id="toLogin">登录</a>'
    : '还没有账号？<a id="toLogin">注册</a>';
  $('toLogin').onclick = () => { msg('', ''); setMode(isReg ? 'login' : 'register'); };
  msg('', '');
}

function msg(text, kind) {
  const el = $('authMsg');
  el.textContent = text;
  el.className = `msg ${kind}`;
}

$('authForm').onsubmit = async (e) => {
  e.preventDefault();
  const email = $('email').value.trim();
  const password = $('password').value;

  if (mode === 'register' && password.length < 8) {
    return msg('密码至少 8 位', 'err');
  }

  $('authBtn').disabled = true;
  msg(mode === 'register' ? '注册中…' : '登录中…', '');
  try {
    await postJSON(`/auth/${mode}`, { email, password });
    // The session cookie is HttpOnly; the browser stores it automatically.
    // Navigate to the protected panel shell.
    window.location.assign('/panel');
  } catch (err) {
    msg(err.message, 'err');
  } finally {
    $('authBtn').disabled = false;
  }
};

(async function boot() {
  try {
    const cfg = await (await fetch('/config')).json();
    $('bonusAmt').textContent = Number(cfg.bonus).toFixed(0);
  } catch { /* defaults in the markup are fine */ }

  // If a valid session already exists, skip the shell entirely.
  try {
    const res = await fetch('/auth/me');
    if (res.ok) { window.location.replace('/panel'); return; }
  } catch { /* offline / fresh visitor → show shell */ }

  $('auth').classList.remove('hidden');
  setMode('register');
})();
