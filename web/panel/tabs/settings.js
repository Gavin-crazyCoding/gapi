/* Settings tab (admin): edit GavinCoin economy parameters.
 * Values come from /admin/settings; edits go to PUT /admin/settings.
 */

let bound = false;

export async function init({ $, api }) {
  if (bound) return;
  bound = true;

  $('saveSettings').onclick = async () => {
    const btn = $('saveSettings');
    btn.disabled = true;
    $('setMsg').className = 'msg';
    $('setMsg').textContent = '保存中…';
    try {
      await api('/admin/settings', {
        method: 'PUT',
        body: JSON.stringify([
          { key: 'rate_multiplier', value: String(Number($('setRateMult').value) || 1) },
          { key: 'coin_rate', value: String(Number($('setCoinRate').value) || 0) },
          { key: 'daily_bonus', value: String(Number($('setDailyBonus').value) || 0) },
          { key: 'registration_bonus', value: String(Number($('setRegBonus').value) || 0) },
          { key: 'max_coin_per_user', value: String(Number($('setMaxCoin').value) || 0) },
          { key: 'registration_open', value: $('setRegOpen').checked ? 'true' : 'false' },
        ]),
      });
      $('setMsg').textContent = '已保存，立即生效';
      $('setMsg').className = 'msg ok';
    } catch (err) {
      $('setMsg').textContent = '保存失败：' + err.message;
      $('setMsg').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

export async function refresh({ $, api }) {
  try {
    const d = await api('/admin/settings');
    $('setRateMult').value = d.rate_multiplier;
    $('setCoinRate').value = d.coin_rate;
    $('setDailyBonus').value = d.daily_bonus;
    $('setRegBonus').value = d.registration_bonus;
    $('setMaxCoin').value = d.max_coin_per_user;
    $('setRegOpen').checked = d.registration_open === 'true';
  } catch (err) {
    $('setMsg').textContent = '加载失败：' + err.message;
    $('setMsg').className = 'msg err';
  }
}