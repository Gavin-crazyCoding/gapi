/* Billing tab: balance, token-package purchase, ledger. */

let bound = false;

export async function init({ $, api, coin, num, esc, when, table, config }) {
  if (bound) return;
  bound = true;

  $('redeemBtn').onclick = async () => {
    const code = $('redeemCode').value.trim();
    const out = $('redeemMsg');
    out.className = 'msg';
    if (!code) {
      out.textContent = '请输入兑换码';
      out.className = 'msg err';
      return;
    }
    $('redeemBtn').disabled = true;
    out.textContent = '兑换中…';
    try {
      const r = await api('/user/redeem', {
        method: 'POST',
        body: JSON.stringify({ code }),
      });
      out.textContent = `✓ 已入账 ${coin(r.redeemed)}，当前余额 ${coin(r.balance)}`;
      out.className = 'msg ok';
      $('redeemCode').value = '';
      await refresh({ $, api, coin, num, esc, when, table, config });
    } catch (err) {
      out.textContent = err.message;
      out.className = 'msg err';
    } finally {
      $('redeemBtn').disabled = false;
    }
  };

  $('buyBtn').onclick = async () => {
    const cost = $('buyCost').value.trim();
    const days = $('buyDays').value.trim();
    const out = $('buyMsg');
    out.className = 'msg';
    if (!cost || Number(cost) <= 0) {
      out.textContent = '请输入要花费的 GavinCoin';
      out.className = 'msg err';
      return;
    }
    $('buyBtn').disabled = true;
    out.textContent = '购买中…';
    try {
      const body = { cost };
      if (days) body.valid_days = Number(days);
      const p = await api('/user/packages', { method: 'POST', body: JSON.stringify(body) });
      out.textContent = `✓ 已开通 ${num(p.total_tokens)} tokens 配额${days ? `，${days} 天有效` : '（永久）'}`;
      out.className = 'msg ok';
      $('buyCost').value = '';
      $('buyDays').value = '';
      await refresh({ $, api, coin, num, esc, when, table, config });
    } catch (err) {
      out.textContent = err.message;
      out.className = 'msg err';
    } finally {
      $('buyBtn').disabled = false;
    }
  };
}

export async function refresh({ $, api, coin, num, esc, when, table, config }) {
  const [bal, pkgs, txs] = await Promise.all([
    api('/user/balance'), api('/user/packages'), api('/user/transactions?limit=50'),
  ]);
  const activePlans = pkgs.filter((p) => p.status === 'active');
  $('bBal').textContent = coin(bal.balance);
  $('bTokens').textContent = num(activePlans.reduce((s, p) => s + p.remaining_tokens, 0));
  $('bRate').textContent = `◎1 = ${num((config.tokenRate || 100) * 1000)} tok`;

  const statusBadge = (s) => ({
    active: '<span class="pos">生效中</span>',
    exhausted: '<span class="neg">已用尽</span>',
    expired: '<span class="neg">已过期</span>',
  })[s] || esc(s);

  $('pkgWrap').innerHTML = table([
    { label: '总量', num: true, get: (p) => num(p.total_tokens) },
    { label: '已用', num: true, get: (p) => num(p.used_tokens) },
    { label: '剩余', num: true, get: (p) => num(p.remaining_tokens) },
    { label: '花费', num: true, cls: 'coin', get: (p) => coin(p.cost_gavincoin) },
    { label: '状态', get: (p) => statusBadge(p.status) },
    { label: '到期', get: (p) => p.expires_at ? when(p.expires_at) : '永久' },
    { label: '开通时间', get: (p) => when(p.created_at) },
  ], pkgs, '还没有配额计划');

  $('txWrap').innerHTML = table([
    { label: '时间', get: (t) => when(t.created_at) },
    { label: '类型', get: (t) => esc(t.tx_type) },
    { label: '金额', num: true, get: (t) => {
      const n = Number(t.amount);
      return `<span class="${n >= 0 ? 'pos' : 'neg'}">${n >= 0 ? '+' : ''}${coin(t.amount)}</span>`;
    } },
    { label: '余额', num: true, cls: 'coin', get: (t) => coin(t.balance_after) },
    { label: '备注', get: (t) => esc(t.note || '') },
  ], txs, '还没有流水');
}
