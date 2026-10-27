/* IDEA 工作台前端（M0，原生 JS，无构建） */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

const state = {
  token: localStorage.getItem('idea_token') || '',
  config: null,          // {items, groups}
  changed: {},           // key -> value
  services: [],
  connected: false,
};

/* ---------------- 基础 ---------------- */
async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': state.token,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { data = { ok: false, error: 'bad_json' }; }
  if (!res.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show ' + kind;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast ' + kind; }, 2600);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ---------------- 标签切换 ---------------- */
$$('.tab').forEach(tab => {
  tab.onclick = () => {
    $$('.tab').forEach(t => t.classList.toggle('active', t === tab));
    $$('.panel').forEach(p => p.classList.remove('active'));
    const id = 'tab-' + tab.dataset.tab;
    $('#' + id).classList.add('active');
    if (tab.dataset.tab === 'config') loadConfig();
    if (tab.dataset.tab === 'git') loadGit();
    if (tab.dataset.tab === 'audit') loadAudit();
  };
});

/* ---------------- 连接 ---------------- */
async function connect() {
  state.token = $('#token').value.trim() || state.token;
  if (!state.token) { toast('先填管理令牌', 'err'); return; }
  try {
    await api('/api/wb/overview');
    localStorage.setItem('idea_token', state.token);
    state.connected = true;
    $('#statusDot').className = 'dot ok';
    toast('已连接', 'ok');
    await loadOverview();
  } catch (e) {
    $('#statusDot').className = 'dot err';
    toast('连接失败：' + e.message, 'err');
  }
}

/* ---------------- 总览 ---------------- */
async function loadOverview() {
  const data = await api('/api/wb/overview');
  state.services = data.services || [];
  const running = state.services.filter(s => s.running).length;
  const cards = [
    ['项目目录', data.soulagent, 'muted'],
    ['服务', `${running}/${state.services.length} 运行中`, running === state.services.length ? 'ok' : 'warn'],
    ['Git 分支', data.git.branch || '(非仓库)', data.git.ok ? 'ok' : 'err'],
    ['Git 变更', data.git.dirty ? `${data.git.changes} 个文件未提交` : '干净', data.git.dirty ? 'warn' : 'ok'],
    ['配置', data.config_path, 'muted'],
    ['上线时间', data.config_mtime ? new Date(data.config_mtime * 1000).toLocaleString() : '-', 'muted'],
  ];
  $('#overview-cards').innerHTML = cards.map(([k, v, cls]) =>
    `<div class="card"><div class="muted small">${esc(k)}</div>
     <div class="${cls}" style="margin-top:6px;word-break:break-all">${esc(v)}</div></div>`).join('');
  $('#overview-raw').textContent = '';
}

/* ---------------- 服务 ---------------- */
async function loadServices() {
  const data = await api('/api/wb/services');
  state.services = data.items || [];
  $('#services').innerHTML = state.services.map(s => `
    <div class="svc">
      <div class="svc-head">
        <div class="svc-name">${esc(s.label)}</div>
        <span class="badge ${s.running ? 'on' : 'off'}">${s.running ? '运行中' : '已停止'}</span>
      </div>
      <div class="svc-meta">端口 ${s.port} · PID ${s.pid ?? '-'}<br>${esc(s.cmd)}</div>
      <div class="svc-actions">
        <button class="btn small" data-act="start" data-name="${esc(s.name)}" ${s.running ? 'disabled' : ''}>启动</button>
        <button class="btn small danger" data-act="stop" data-name="${esc(s.name)}" ${s.running ? '' : 'disabled'}>停止</button>
        <button class="btn small" data-act="restart" data-name="${esc(s.name)}">重启</button>
        <button class="btn small" data-act="logs" data-name="${esc(s.name)}">日志</button>
      </div>
    </div>`).join('');

  $$('#services button').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      const act = btn.dataset.act;
      if (act === 'logs') { showLogs(name); return; }
      btn.disabled = true;
      try {
        const r = await api(`/api/wb/services/${name}/${act}`, { method: 'POST' });
        toast(r.ok ? `${name} ${act} 成功` : `${name} ${act} 失败：${r.error || ''}`, r.ok ? 'ok' : 'err');
      } catch (e) { toast(e.message, 'err'); }
      await loadServices();
      await loadOverview().catch(() => {});
    };
  });
}

async function showLogs(name) {
  try {
    const r = await api(`/api/wb/services/${name}/logs?lines=400`);
    $('#modal-title').textContent = `${name} 日志（${r.path || ''}）`;
    $('#modal-body').textContent = r.text || '(空)';
    $('#modal').classList.remove('hidden');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 配置 ---------------- */
async function loadConfig(force = false) {
  if (state.config && !force) return renderConfig();
  const data = await api('/api/wb/config');
  state.config = data;
  state.changed = {};
  renderConfig();
  loadBackups();
}

function renderConfig() {
  const cfg = state.config;
  if (!cfg) return;
  const filter = ($('#config-filter').value || '').trim().toLowerCase();
  const byGroup = new Map();
  for (const item of cfg.items) {
    if (filter && !(`${item.key} ${item.label} ${item.desc}`.toLowerCase().includes(filter))) continue;
    if (!byGroup.has(item.group)) byGroup.set(item.group, []);
    byGroup.get(item.group).push(item);
  }
  const html = [];
  for (const [group, items] of byGroup) {
    html.push(`<div class="cfg-group"><div class="cfg-group-title">${esc(group)}</div>`);
    for (const item of items) {
      const changed = Object.prototype.hasOwnProperty.call(state.changed, item.key);
      const value = changed ? state.changed[item.key] : item.value;
      html.push(`<div class="cfg-item ${changed ? 'changed' : ''}" data-key="${esc(item.key)}">
        <div class="cfg-label">
          <span>${esc(item.label)} <span class="muted small">${esc(item.key)}</span></span>
          <span class="desc">${esc(item.desc || '')}</span>
          ${item.restart ? '<span class="tag">需重启生效</span>' : ''}
        </div>
        <div class="cfg-input">${inputHtml(item, value)}</div>
      </div>`);
    }
    html.push('</div>');
  }
  $('#config-form').innerHTML = html.join('') || '<div class="muted">没有匹配的配置项</div>';
  bindConfigInputs();
}

function inputHtml(item, value) {
  const key = esc(item.key);
  if (item.type === 'bool') {
    return `<input type="checkbox" data-k="${key}" ${value ? 'checked' : ''}>`;
  }
  if (item.type === 'str_list') {
    const text = Array.isArray(value) ? value.join('\n') : '';
    return `<textarea data-k="${key}" rows="2" style="width:100%;max-width:420px">${esc(text)}</textarea>`;
  }
  if (item.type === 'hour_range') {
    const v = Array.isArray(value) ? value : [22, 9];
    return `<input type="number" min="0" max="23" data-k="${key}" data-part="0" value="${v[0]}" style="width:70px">
            <span class="muted">→</span>
            <input type="number" min="0" max="23" data-k="${key}" data-part="1" value="${v[1]}" style="width:70px">`;
  }
  if (item.type === 'int' || item.type === 'float') {
    const step = item.type === 'float' ? 'any' : '1';
    const min = item.min ?? ''; const max = item.max ?? '';
    return `<input type="number" step="${step}" data-k="${key}" value="${esc(value ?? '')}" min="${min}" max="${max}" style="width:140px">`;
  }
  if (item.enum) {
    return `<select data-k="${key}">${item.enum.map(v => `<option value="${esc(v)}" ${v === value ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`;
  }
  return `<input type="text" data-k="${key}" value="${esc(value ?? '')}" ${item.secret ? 'placeholder="******"' : ''}>`;
}

function bindConfigInputs() {
  $$('#config-form [data-k]').forEach(el => {
    el.onchange = () => {
      const key = el.dataset.k;
      const item = state.config.items.find(i => i.key === key);
      if (!item) return;
      let value;
      if (item.type === 'bool') value = el.checked;
      else if (item.type === 'str_list') value = el.value.split('\n').map(s => s.trim()).filter(Boolean);
      else if (item.type === 'int') value = parseInt(el.value, 10);
      else if (item.type === 'float') value = parseFloat(el.value);
      else if (item.type === 'hour_range') {
        const part = parseInt(el.dataset.part, 10);
        const cur = state.changed[key] ?? item.value ?? [0, 0];
        const arr = [...cur]; arr[part] = parseInt(el.value, 10);
        value = arr;
      } else value = el.value;
      state.changed[key] = value;
      const row = el.closest('.cfg-item');
      if (row) row.classList.add('changed');
    };
  });
}

async function saveConfig() {
  const patch = state.changed;
  if (!Object.keys(patch).length) { toast('没有改动'); return; }
  try {
    const v = await api('/api/wb/config/validate', { method: 'POST', body: { patch } });
    if (!v.ok) { toast('校验失败：' + v.errors.map(e => `${e.key}: ${e.message}`).join('; '), 'err'); return; }
    const r = await api('/api/wb/config/save', {
      method: 'POST', body: { patch, auto_commit: true, auto_push: false },
    });
    if (r.ok) {
      const gitInfo = r.git && r.git.sha ? `，已提交 ${r.git.sha}` : '';
      const restart = r.restart_required?.length ? `（需重启：${r.restart_required.join(', ')}）` : '';
      toast(`已保存 ${r.changed.length} 项${gitInfo}${restart}`, 'ok');
      state.config = null;
      await loadConfig(true);
      await loadOverview().catch(() => {});
    } else {
      toast('保存失败：' + (r.error || ''), 'err');
    }
  } catch (e) { toast(e.message, 'err'); }
}

async function loadBackups() {
  try {
    const r = await api('/api/wb/config/backups');
    const items = r.items || [];
    $('#config-backups').innerHTML = items.length
      ? items.slice(0, 20).map(n => `<div>${esc(n)} <button class="btn small" data-restore="${esc(n)}">恢复</button></div>`).join('')
      : '（暂无备份）';
    $$('#config-backups [data-restore]').forEach(b => {
      b.onclick = async () => {
        if (!confirm(`恢复 ${b.dataset.restore} ?`)) return;
        try {
          await api('/api/wb/config/restore', { method: 'POST', body: { name: b.dataset.restore } });
          toast('已恢复', 'ok'); state.config = null; await loadConfig(true);
        } catch (e) { toast(e.message, 'err'); }
      };
    });
  } catch (e) { /* 未连接时忽略 */ }
}

/* ---------------- Git ---------------- */
async function loadGit() {
  try {
    const st = await api('/api/wb/git/status');
    const cards = [
      ['分支', st.branch || '-', st.ok ? 'ok' : 'err'],
      ['状态', st.dirty ? `${st.changes.length} 个变更` : '干净', st.dirty ? 'warn' : 'ok'],
      ['仓库', st.path || '-', 'muted'],
    ];
    $('#git-cards').innerHTML = cards.map(([k, v, cls]) =>
      `<div class="card"><div class="muted small">${esc(k)}</div>
       <div class="${cls}" style="margin-top:6px;word-break:break-all">${esc(v)}</div></div>`).join('');
    if (st.dirty) {
      const d = await api('/api/wb/git/diff');
      $('#git-diff').textContent = (d.stat || '') + '\n' + (d.diff || '');
    } else {
      $('#git-diff').textContent = '（工作区干净）';
    }
    const log = await api('/api/wb/git/log?limit=20');
    $('#git-log').innerHTML = (log.items || []).map(i =>
      `<div>${esc(i.sha)} · ${esc(i.date)} · ${esc(i.subject)}</div>`).join('') || '（无提交）';
  } catch (e) { toast(e.message, 'err'); }
}

async function gitCommit() {
  const raw = $('#git-message').value.trim();
  const message = raw || `wb: 手动提交 ${new Date().toLocaleString()}`;
  try {
    const r = await api('/api/wb/git/commit', {
      method: 'POST', body: { message, push: $('#git-push').checked },
    });
    if (r.ok) toast(r.noop ? '没有变更可提交' : `已提交 ${r.sha || ''}`, 'ok');
    else toast('提交失败：' + (r.error || r.detail || ''), 'err');
    $('#git-message').value = '';
    await loadGit();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 审计 ---------------- */
async function loadAudit() {
  try {
    const r = await api('/api/wb/audit?limit=100');
    const items = r.items || [];
    if (!items.length) { $('#audit').textContent = '（暂无记录）'; return; }
    $('#audit').innerHTML = `<table class="audit">
      <tr><th>时间</th><th>操作</th><th>目标</th><th>详情</th></tr>
      ${items.map(i => `<tr>
        <td>${new Date(i.ts * 1000).toLocaleString()}</td>
        <td>${esc(i.action)}</td>
        <td>${esc(i.target)}</td>
        <td>${esc(JSON.stringify(i.meta || {}).slice(0, 160))}</td>
      </tr>`).join('')}</table>`;
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 初始化 ---------------- */
$('#token').value = state.token;
$('#connect').onclick = connect;
$('#token').onkeydown = e => { if (e.key === 'Enter') connect(); };
$('#config-save').onclick = saveConfig;
$('#config-reset').onclick = () => { state.config = null; state.changed = {}; loadConfig(true); };
$('#config-filter').oninput = () => renderConfig();
$('#git-commit').onclick = gitCommit;
$('#audit-refresh').onclick = loadAudit;
$('#modal-close').onclick = () => $('#modal').classList.add('hidden');

if (state.token) connect();
setInterval(() => { if (state.connected) loadServices().catch(() => {}); }, 10000);
