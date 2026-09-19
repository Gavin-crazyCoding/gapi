/* Stats tab (admin): global operational overview from /admin/stats. */

export async function init() { /* read-only tab */ }

export async function refresh({ $, api, coin, num, esc, table }) {
  const d = await api('/admin/stats');

  $('stUsers').textContent = `${num(d.total_users)}（活跃 ${num(d.active_users)}）`;
  $('stSignups').textContent = num(d.signups_7d);
  $('stReqs').textContent = num(d.requests_30d);
  $('stToks').textContent = num(d.tokens_30d);
  $('stSpend2').textContent = coin(d.spend_30d);
  $('stRedeems').textContent = num(d.redeems_30d);

  const days = d.daily || [];
  const max = Math.max(1, ...days.map((x) => x.requests));
  $('stChart').innerHTML = days
    .map((x) => `<div class="bar" style="height:${(x.requests / max) * 100}%" title="${esc(x.date)}: ${num(x.requests)} 次 / ${num(x.tokens)} tok"></div>`)
    .join('');
  $('stChart').classList.toggle('hidden', !days.length);
  $('stChartEmpty').classList.toggle('hidden', days.length > 0);

  $('stModels').innerHTML = table([
    { label: '模型', get: (r) => `<span class="mono">${esc(r.model)}</span>` },
    { label: '请求', num: true, get: (r) => num(r.requests) },
    { label: 'Tokens', num: true, get: (r) => num(r.tokens) },
    { label: '消耗', num: true, cls: 'coin', get: (r) => coin(r.cost) },
  ], d.top_models || [], '暂无用量');

  $('stUsersTop').innerHTML = table([
    { label: '用户', get: (r) => esc(r.email) },
    { label: '请求', num: true, get: (r) => num(r.requests) },
    { label: '消耗', num: true, cls: 'coin', get: (r) => coin(r.cost) },
  ], d.top_users || [], '暂无用量');
}
