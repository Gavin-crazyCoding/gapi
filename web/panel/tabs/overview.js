/* Overview tab: balance, 30-day spend, token chart, recent requests, account. */

let bound = false;

export async function init({ $, api }) {
  if (bound) return;
  bound = true;

  $('pwBtn').onclick = async () => {
    const out = $('pwMsg');
    out.className = 'msg';
    if ($('pwNew').value.length < 8) {
      out.textContent = '新密码至少 8 位';
      out.className = 'msg err';
      return;
    }
    $('pwBtn').disabled = true;
    out.textContent = '提交中…';
    try {
      await api('/user/password', {
        method: 'POST',
        body: JSON.stringify({ old_password: $('pwOld').value, new_password: $('pwNew').value }),
      });
      out.textContent = '✓ 密码已更新';
      out.className = 'msg ok';
      $('pwOld').value = '';
      $('pwNew').value = '';
    } catch (err) {
      out.textContent = err.message;
      out.className = 'msg err';
    } finally {
      $('pwBtn').disabled = false;
    }
  };

  $('resendBtn').onclick = async () => {
    const out = $('resendMsg');
    out.className = 'msg';
    $('resendBtn').disabled = true;
    out.textContent = '发送中…';
    try {
      await api('/auth/resend-verification', { method: 'POST' });
      out.textContent = '✓ 验证邮件已发送，请查收';
      out.className = 'msg ok';
    } catch (err) {
      out.textContent = err.message;
      out.className = 'msg err';
    } finally {
      $('resendBtn').disabled = false;
    }
  };
}

export async function refresh({ $, api, coin, num, esc, when, table }) {
  const me = await api('/auth/me');
  $('emailStatus').textContent = me.email_verified
    ? `邮箱：${me.email}（已验证）`
    : `邮箱：${me.email}（未验证）`;
  $('resendBtn').hidden = !!me.email_verified;

  const d = await api('/user/dashboard');
  $('stBal').textContent = coin(d.balance);
  $('stSpend').textContent = coin(d.total_spend_30d);
  $('stReq').textContent = num(d.request_count_30d);

  const days = d.usage_by_day || [];
  const max = Math.max(1, ...days.map((x) => x.total_tokens));
  $('chart').innerHTML = days
    .map((x) => `<div class="bar" style="height:${(x.total_tokens / max) * 100}%" title="${esc(x.date)}: ${num(x.total_tokens)} tokens"></div>`)
    .join('');
  $('chart').classList.toggle('hidden', !days.length);
  $('chartEmpty').classList.toggle('hidden', days.length > 0);

  $('recentWrap').innerHTML = table([
    { label: '时间', get: (r) => when(r.created_at) },
    { label: '模型', get: (r) => `<span class="mono">${esc(r.model)}</span>` },
    { label: '端点', cls: 'col-opt', get: (r) => `<span class="mono">${esc(r.endpoint)}</span>` },
    { label: 'Tokens', num: true, get: (r) => num(r.total_tokens) },
    { label: '花费', num: true, cls: 'coin', get: (r) => coin(r.gavincoin_cost) },
    { label: '状态', cls: 'col-opt', get: (r) => r.status === 'ok' ? '<span class="pos">ok</span>' : `<span class="neg">${esc(r.status)}</span>` },
  ], d.recent || [], '还没有请求记录');
}
