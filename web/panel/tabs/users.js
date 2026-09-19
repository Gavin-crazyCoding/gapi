/* Users tab (admin): list, create, edit, delete users and grant balance. */

let bound = false;
let _ctx = null;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function formatDate(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d)) return '–';
  return d.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export async function init(context) {
  if (bound) return;
  bound = true;
  _ctx = context;
  const { $, api } = context;

  // Create user form
  $('createUser').onclick = async () => {
    const btn = $('createUser');
    btn.disabled = true;
    $('nuMsg').className = 'msg';
    $('nuMsg').textContent = '创建中…';
    try {
      await api('/users', {
        method: 'POST',
        body: JSON.stringify({
          email: $('nuEmail').value.trim(),
          password: $('nuPass').value,
          role: $('nuRole').value,
          concurrent_limit: Number($('nuConcurrent').value) || 5,
        }),
      });
      $('nuMsg').textContent = '创建成功';
      $('nuMsg').className = 'msg ok';
      $('nuEmail').value = '';
      $('nuPass').value = '';
      refresh(context);
    } catch (err) {
      $('nuMsg').textContent = '创建失败：' + err.message;
      $('nuMsg').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

export async function refresh(context) {
  _ctx = context;
  const { $, api } = context;
  try {
    const users = await api('/users');
    $('uCount').textContent = `(${users.length})`;
    const rows = users.map((u) => `
      <tr data-uid="${u.id}">
        <td>${escapeHtml(u.email)}</td>
        <td><span class="badge">${escapeHtml(u.role)}</span></td>
        <td class="num">${'◎ ' + Number(u.gavincoin_balance || 0).toFixed(2)}</td>
        <td class="num">${u.concurrent_limit}</td>
        <td class="num">${u.concurrent_active || 0}</td>
        <td>${u.is_active ? '<span class="badge ok">启用</span>' : '<span class="badge err">禁用</span>'}</td>
        <td>${formatDate(u.last_login_date)}</td>
        <td>
          <button class="ghost edit-btn" data-uid="${u.id}" data-email="${escapeHtml(u.email)}" data-role="${u.role}" data-active="${u.is_active}" data-climit="${u.concurrent_limit}">编辑</button>
          <button class="ghost danger grant-btn" data-uid="${u.id}">充值</button>
          <button class="ghost fp-btn" data-uid="${u.id}" data-email="${escapeHtml(u.email)}">解绑指纹</button>
          <button class="ghost danger del-btn" data-uid="${u.id}" ${u.role === 'admin' ? 'disabled' : ''}>删除</button>
        </td>
      </tr>
    `).join('');
    $('usersWrap').innerHTML = `
      <table>
        <thead>
          <tr>
            <th>邮箱</th><th>角色</th><th class="num">余额</th><th class="num">并发上限</th><th class="num">活跃并发</th><th>状态</th><th>最后登录</th><th>操作</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="8" class="empty">暂无用户</td></tr>'}</tbody>
      </table>
    `;

    // Bind edit buttons
    $$('.edit-btn').forEach((btn) => {
      btn.onclick = () => openEditModal(btn.dataset, context);
    });

    // Bind grant buttons
    $$('.grant-btn').forEach((btn) => {
      btn.onclick = () => openGrantModal(btn.dataset.uid, context);
    });

    // Bind delete buttons
    $$('.del-btn').forEach((btn) => {
      btn.onclick = () => confirmDelete(btn.dataset.uid, context);
    });

    // Bind fingerprint-clear buttons
    $$('.fp-btn').forEach((btn) => {
      btn.onclick = () => clearFingerprints(btn.dataset.uid, btn.dataset.email, context);
    });

  } catch (err) {
    $('usersWrap').innerHTML = `<div class="empty">加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function $$(sel) {
  return Array.from(document.querySelectorAll(sel));
}

function openEditModal(data, context) {
  const { api } = context;
  const uid = data.uid;
  const html = `
    <div class="modal-overlay" id="editModal">
      <div class="modal">
        <h3>编辑用户 ${escapeHtml(data.email)}</h3>
        <div class="row">
          <div><label>角色</label><select id="editRole">
            <option value="user" ${data.role === 'user' ? 'selected' : ''}>user</option>
            <option value="admin" ${data.role === 'admin' ? 'selected' : ''}>admin</option>
            <option value="moderator" ${data.role === 'moderator' ? 'selected' : ''}>moderator</option>
          </select></div>
          <div><label>并发上限</label><input id="editClimit" type="number" min="1" max="100" value="${data.climit}"></div>
        </div>
        <div><label><input type="checkbox" id="editActive" ${data.active === 'true' ? 'checked' : ''}> 启用账号</label></div>
        <div class="msg" id="editMsg"></div>
        <div class="row" style="justify-content:flex-end">
          <button class="ghost" id="editCancel">取消</button>
          <button class="primary" id="editSave">保存</button>
        </div>
      </div>
    </div>
  `;
  document.body.insertAdjacentHTML('beforeend', html);

  const modal = $('editModal');
  const close = () => modal.remove();

  $('editCancel').onclick = close;
  modal.onclick = (e) => { if (e.target === modal) close(); };

  $('editSave').onclick = async () => {
    const btn = $('editSave');
    btn.disabled = true;
    $('editMsg').className = 'msg';
    $('editMsg').textContent = '保存中…';
    try {
      const patch = {
        role: $('editRole').value,
        concurrent_limit: Number($('editClimit').value) || 5,
        is_active: $('editActive').checked,
      };
      await api(`/users/${uid}`, { method: 'PUT', body: JSON.stringify(patch) });
      $('editMsg').textContent = '已保存';
      $('editMsg').className = 'msg ok';
      setTimeout(close, 500);
      refresh(context);
    } catch (err) {
      $('editMsg').textContent = '保存失败：' + err.message;
      $('editMsg').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

function openGrantModal(uid, context) {
  const { api } = context;
  const amount = prompt('输入要充值的 GavinCoin 数量（可为负数扣除）：');
  if (amount === null || amount.trim() === '') return;
  const val = Number(amount);
  if (!isFinite(val)) { alert('请输入有效数字'); return; }

  const html = `
    <div class="modal-overlay" id="grantModal">
      <div class="modal">
        <h3>确认充值</h3>
        <p>用户 ID: ${uid}</p>
        <p>金额: <b>${val >= 0 ? '+' : ''}${val.toFixed(2)}</b> GavinCoin</p>
        <div class="msg" id="grantMsg"></div>
        <div class="row" style="justify-content:flex-end">
          <button class="ghost" id="grantCancel">取消</button>
          <button class="primary" id="grantConfirm">确认</button>
        </div>
      </div>
    </div>
  `;
  document.body.insertAdjacentHTML('beforeend', html);

  const modal = $('grantModal');
  const close = () => modal.remove();

  $('grantCancel').onclick = close;
  modal.onclick = (e) => { if (e.target === modal) close(); };

  $('grantConfirm').onclick = async () => {
    const btn = $('grantConfirm');
    btn.disabled = true;
    $('grantMsg').className = 'msg';
    $('grantMsg').textContent = '处理中…';
    try {
      await api(`/users/${uid}`, { method: 'PUT', body: JSON.stringify({ grant_balance: val }) });
      $('grantMsg').textContent = '充值成功';
      $('grantMsg').className = 'msg ok';
      setTimeout(close, 500);
      refresh(context);
    } catch (err) {
      $('grantMsg').textContent = '充值失败：' + err.message;
      $('grantMsg').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

async function clearFingerprints(uid, email, context) {
  if (!confirm(`确定清除 ${email} 的全部指纹绑定吗？该用户下次登录会重新绑定。`)) return;
  const { api } = context;
  try {
    const r = await api(`/users/${uid}/fingerprints`, { method: 'DELETE' });
    alert(`已清除 ${r.cleared} 个指纹绑定`);
  } catch (err) {
    alert('操作失败：' + err.message);
  }
}

function confirmDelete(uid, context) {
  if (!confirm('确定要删除这个用户吗？此操作不可恢复。')) return;
  const { api } = context;
  api(`/users/${uid}`, { method: 'DELETE' })
    .then(() => { alert('已删除'); refresh(context); })
    .catch((err) => alert('删除失败：' + err.message));
}
