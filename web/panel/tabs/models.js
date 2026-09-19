/* Models tab: live upstream catalog + per-user routing strategy. */

let models = [];
let bound = false;
let lastCacheAge = 0;

function render(ctx, list) {
  const { $, esc, num, table } = ctx;
  $('mWrap').innerHTML = table([
    { label: '模型', get: (m) =>
      `<span class="mono">${esc(m.id)}</span>` +
      (m.is_router ? '<span class="badge badge-router">router</span>' : '') +
      (!m.priced ? '<span class="badge badge-fallback">fallback</span>' : '') },
    { label: '输入 @1k', num: true, get: (m) => esc(m.input_per_1k) },
    { label: '输出 @1k', num: true, get: (m) => esc(m.output_per_1k) },
    { label: '上下文', num: true, get: (m) => m.context_window ? num(m.context_window) : '–' },
    { label: '工具', get: (m) => m.supports_tools ? '<span class="pos">✓</span>' : '<span class="neg">–</span>' },
    { label: '流式', get: (m) => m.supports_streaming ? '<span class="pos">✓</span>' : '<span class="neg">–</span>' },
  ], list, '暂无可用模型');
}

export async function init(context) {
  if (bound) return;
  bound = true;
  const { $, api, esc, num, table } = context;

  // Create search input if it doesn't exist
  if (!$('#mFilter')) {
    const input = document.createElement('input');
    input.id = 'mFilter';
    input.placeholder = '输入模型名过滤…';
    input.style.cssText = 'width: 100%; padding: 8px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px;';
    document.querySelector('#tab-models .row').appendChild(input);
  }

  $('#mFilter').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) return render({ $, esc, num, table }, models);
    render({ $, esc, num, table }, models.filter((m) => m.id.toLowerCase().includes(q)));
  });

  $('#mRefresh').onclick = async () => {
    $('#mMeta').textContent = '刷新中…';
    try { await refresh(context, true); }
    catch (err) { $('#mMeta').textContent = '刷新失败：' + err.message; }
  };

  $('#routingSelect').addEventListener('change', async (e) => {
    try {
      await api('/user/routing', {
        method: 'POST',
        body: JSON.stringify({ strategy: e.target.value }),
      });
      $('#mMeta').textContent = '路由策略已更新';
      setTimeout(() => { $('#mMeta').textContent = `来自上游 · 缓存 ${lastCacheAge}s`; }, 2000);
    } catch (err) {
      $('#mMeta').textContent = '更新路由失败：' + err.message;
    }
  });
}

export async function refresh(context, force = false) {
  const { $, api, esc, num, table } = context;
  const [d, rs] = await Promise.all([
    api(force ? '/user/models?force=1' : '/user/models'),
    api('/user/routing'),
  ]);
  models = d.models || [];
  lastCacheAge = d.cached_age_seconds || 0;
  $('#mCount').textContent = `${models.length} 个`;
  $('#mMeta').textContent = `来自上游 · 缓存 ${lastCacheAge}s`;
  $('#routingSelect').value = rs.strategy;
  render({ $, esc, num, table }, models);
}
