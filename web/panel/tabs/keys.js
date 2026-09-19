/* Keys tab: list/create/delete gapi API keys.
 *
 * Keys authenticate /v1/* requests with Bearer, x-api-key or x-goog-api-key —
 * both the OpenAI and Anthropic wire formats.
 */

let bound = false;

export async function init({ $, api, esc, when, table }) {
  if (bound) return;
  bound = true;

  $('createKey').onclick = async () => {
    const btn = $('createKey');
    btn.disabled = true;
    $('newKey').className = '';
    try {
      const k = await api('/user/keys', {
        method: 'POST',
        body: JSON.stringify({ name: $('keyName').value.trim() || 'default' }),
      });
      // The plaintext key is rendered into a text node, never concatenated
      // into markup, so it cannot break out of its element.
      $('newKey').innerHTML =
        '<div class="keybox"><div class="lbl">✓ 已创建，请立即保存——这是唯一一次显示明文</div><code id="newKeyValue"></code></div>';
      $('newKeyValue').textContent = k.key;
      $('keyName').value = '';
      await refresh({ $, api, esc, when, table });
    } catch (err) {
      $('newKey').textContent = err.message;
      $('newKey').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

export async function refresh({ $, api, esc, when, table }) {
  const keys = (await api('/user/keys')) || [];
  $('keysWrap').innerHTML = table([
    { label: '前缀', get: (k) => `<span class="mono">${esc(k.key_prefix)}…</span>` },
    { label: '名称', get: (k) => esc(k.name) },
    { label: '30 天 Tokens', num: true, get: (k) => Number(k.tokens_30d || 0).toLocaleString() },
    { label: '30 天请求', num: true, get: (k) => Number(k.requests_30d || 0).toLocaleString() },
    { label: '创建', get: (k) => when(k.created_at) },
    { label: '最近使用', get: (k) => when(k.last_used_at) },
    { label: '', num: true, get: (k) => `<button class="danger" data-del="${Number(k.id) || 0}">删除</button>` },
  ], keys, '还没有密钥，创建一个开始调用 /v1/*');

  $('keysWrap').querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      if (!confirm('删除后使用该密钥的请求会立即失败，确定？')) return;
      await api(`/user/keys/${Number(b.dataset.del)}`, { method: 'DELETE' });
      await refresh({ $, api, esc, when, table });
    };
  });
}
