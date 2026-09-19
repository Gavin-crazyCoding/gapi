/* Usage tab: per-model and per-day aggregates for the last 30 days. */

export async function init() { /* read-only tab */ }

export async function refresh({ $, api, coin, num, esc, table }) {
  const [byModel, byDay] = await Promise.all([
    api('/user/usage/models'), api('/user/usage'),
  ]);
  $('umWrap').innerHTML = table([
    { label: '模型', get: (r) => `<span class="mono">${esc(r.model)}</span>` },
    { label: '输入', num: true, get: (r) => num(r.prompt_tokens) },
    { label: '输出', num: true, get: (r) => num(r.completion_tokens) },
    { label: '合计', num: true, get: (r) => num(r.total_tokens) },
    { label: '花费', num: true, cls: 'coin', get: (r) => coin(r.cost) },
  ], byModel, '暂无用量');

  $('udWrap').innerHTML = table([
    { label: '日期', get: (r) => esc(r.date) },
    { label: '输入', num: true, get: (r) => num(r.prompt_tokens) },
    { label: '输出', num: true, get: (r) => num(r.completion_tokens) },
    { label: '合计', num: true, get: (r) => num(r.total_tokens) },
    { label: '花费', num: true, cls: 'coin', get: (r) => coin(r.cost) },
  ], byDay, '暂无用量');
}
