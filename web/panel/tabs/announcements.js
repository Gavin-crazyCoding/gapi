/* Announcements tab (admin): CRUD for site-wide announcements. */

let bound = false;

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&', '<': '<', '>': '>', '"': '"' }[c]));
}

function formatDate(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d)) return '–';
  return d.toLocaleString([], { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export async function init(context) {
  if (bound) return;
  bound = true;
  const { $, api } = context;

  // Create announcement form
  $('createAnnouncement').onclick = async () => {
    const btn = $('createAnnouncement');
    btn.disabled = true;
    $('anMsg').className = 'msg';
    $('anMsg').textContent = '发布中…';
    try {
      const start = $('anStart').value.trim();
      const end = $('anEnd').value.trim();
      await api('/admin/announcements', {
        method: 'POST',
        body: JSON.stringify({
          title: $('anTitle').value.trim(),
          content: $('anContent').value,
          priority: Number($('anPriority').value) || 0,
          as_html: $('anHtml').checked,
          start_at: start || null,
          end_at: end || null,
        }),
      });
      $('anMsg').textContent = '发布成功';
      $('anMsg').className = 'msg ok';
      $('anTitle').value = '';
      $('anContent').value = '';
      $('anPriority').value = '0';
      $('anHtml').checked = false;
      $('anStart').value = '';
      $('anEnd').value = '';
      refresh(context);
    } catch (err) {
      $('anMsg').textContent = '发布失败：' + err.message;
      $('anMsg').className = 'msg err';
    } finally {
      btn.disabled = false;
    }
  };
}

export async function refresh(context) {
  const { $, api } = context;
  try {
    const anns = await api('/admin/announcements');
    const rows = anns.map((a) => `
      <tr data-aid="${a.id}">
        <td>${escapeHtml(a.title)}</td>
        <td class="num">${a.priority}</td>
        <td>${a.as_html ? '<span class="badge">HTML</span>' : '<span class="badge">MD</span>'}</td>
        <td>${a.start_at ? formatDate(a.start_at) : '即时'}</td>
        <td>${a.end_at ? formatDate(a.end_at) : '长期'}</td>
        <td>${a.is_active ? '<span class="badge ok">启用</span>' : '<span class="badge err">禁用</span>'}${a.live ? ' <span class="badge ok">生效中</span>' : ''}</td>
        <td>
          <button class="ghost toggle-btn" data-aid="${a.id}" data-active="${a.is_active}">
            ${a.is_active ? '禁用' : '启用'}
          </button>
          <button class="ghost edit-btn" data-aid="${a.id}">编辑</button>
          <button class="ghost danger del-btn" data-aid="${a.id}">删除</button>
        </td>
      </tr>
    `).join('');
    $('announcementsWrap').innerHTML = `
      <table>
        <thead>
          <tr>
            <th>标题</th><th class="num">优先级</th><th>格式</th><th>开始时间</th><th>结束时间</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="7" class="empty">暂无公告</td></tr>'}</tbody>
      </table>
    `;

    // Bind toggle buttons
    $$('.toggle-btn').forEach((btn) => {
      btn.onclick = () => toggleAnnouncement(btn.dataset.aid, btn.dataset.active === 'true', context);
    });

    // Bind edit buttons
    $$('.edit-btn').forEach((btn) => {
      btn.onclick = () => openEditModal(btn.dataset.aid, context);
    });

    // Bind delete buttons
    $$('.del-btn').forEach((btn) => {
      btn.onclick = () => confirmDelete(btn.dataset.aid, context);
    });

  } catch (err) {
    $('announcementsWrap').innerHTML = `<div class="empty">加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function $$(sel) {
  return Array.from(document.querySelectorAll(sel));
}

async function toggleAnnouncement(aid, currentActive, context) {
  const { api } = context;
  try {
    await api(`/admin/announcements/${aid}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active: !currentActive }),
    });
    refresh(context);
  } catch (err) {
    alert('操作失败：' + err.message);
  }
}

function openEditModal(aid, context) {
  const { api } = context;
  // Fetch current announcement data
  api(`/admin/announcements/${aid}`)
    .then((a) => {
      const html = `
        <div class="modal-overlay" id="editAnnModal">
          <div class="modal">
            <h3>编辑公告</h3>
            <div><label for="eAnTitle">标题</label><input id="eAnTitle" value="${escapeHtml(a.title)}"></div>
            <div class="row">
              <div><label for="eAnPriority">优先级</label><input id="eAnPriority" type="number" value="${a.priority}"></div>
              <div><label for="eAnHtml"><input type="checkbox" id="eAnHtml" ${a.as_html ? 'checked' : ''}> HTML 渲染</label></div>
            </div>
            <div><label for="eAnContent">内容</label><textarea id="eAnContent" rows="4">${escapeHtml(a.content)}</textarea></div>
            <div class="row">
              <div><label for="eAnStart">开始时间（UTC，可空）</label><input id="eAnStart" placeholder="2026-01-01T00:00" value="${a.start_at ? a.start_at.slice(0,16) : ''}"></div>
              <div><label for="eAnEnd">结束时间（UTC，可空）</label><input id="eAnEnd" placeholder="2026-12-31T23:59" value="${a.end_at ? a.end_at.slice(0,16) : ''}"></div>
            </div>
            <div><label><input type="checkbox" id="eAnActive" ${a.is_active ? 'checked' : ''}> 启用</label></div>
            <div class="msg" id="eAnMsg"></div>
            <div class="row" style="justify-content:flex-end">
              <button class="ghost" id="eAnCancel">取消</button>
              <button class="primary" id="eAnSave">保存</button>
            </div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML('beforeend', html);

      const modal = $('editAnnModal');
      const close = () => modal.remove();

      $('eAnCancel').onclick = close;
      modal.onclick = (e) => { if (e.target === modal) close(); };

      $('eAnSave').onclick = async () => {
        const btn = $('eAnSave');
        btn.disabled = true;
        $('eAnMsg').className = 'msg';
        $('eAnMsg').textContent = '保存中…';
        try {
          const start = $('eAnStart').value.trim();
          const end = $('eAnEnd').value.trim();
          await api(`/admin/announcements/${aid}`, {
            method: 'PUT',
            body: JSON.stringify({
              title: $('eAnTitle').value.trim(),
              content: $('eAnContent').value,
              priority: Number($('eAnPriority').value) || 0,
              as_html: $('eAnHtml').checked,
              is_active: $('eAnActive').checked,
              start_at: start || null,
              end_at: end || null,
            }),
          });
          $('eAnMsg').textContent = '已保存';
          $('eAnMsg').className = 'msg ok';
          setTimeout(close, 500);
          refresh(context);
        } catch (err) {
          $('eAnMsg').textContent = '保存失败：' + err.message;
          $('eAnMsg').className = 'msg err';
        } finally {
          btn.disabled = false;
        }
      };
    })
    .catch((err) => alert('加载失败：' + err.message));
}

function confirmDelete(aid, context) {
  if (!confirm('确定要删除这个公告吗？此操作不可恢复。')) return;
  const { api } = context;
  api(`/admin/announcements/${aid}`, { method: 'DELETE' })
    .then(() => { alert('已删除'); refresh(context); })
    .catch((err) => alert('删除失败：' + err.message));
}
