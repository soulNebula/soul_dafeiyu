'use strict';

/* ============================================================
 * Soul Console 前端（无框架、无构建、无 CDN）
 * 数据来源：/api/*（tools/console/server.py 聚合本机文件与网关）
 * ============================================================ */

const state = {
  page: 'overview',
  token: sessionStorage.getItem('console_token') || '',
  timer: null,
  feedKind: '',
  feedAuto: true,
  feedSig: '',
  refreshing: false,
  prompts: null,
  promptKey: null,
  config: null,
  models: null,
  memory: null,
  media: null,
  chatMsgs: null,
  chatBusy: false,
  modelConfig: null,
  logsState: null,
  dataInfo: null,
  memoryEdit: null,
  statsState: null,
  bili: null,
  status: null,
  inner: null,
  tone: null,
};

const PAGES = {
  overview: '总览',
  feed: '对话流',
  chat: '对话测试',
  inner: '内状态',
  media: '媒体',
  data: '数据管理',
  knowledge: '记忆编辑',
  memory: '记忆',
  prompts: '提示词',
  models: '模型',
  stats: '统计',
  providers: '模型配置',
  logs: '日志',
  bili: 'B站',
  settings: '设置',
};

const FEED_KINDS = [
  ['', '全部事件'],
  ['inbound', '收消息'],
  ['outbound', '回消息'],
  ['proactive_send', '主动发送'],
  ['proactive_schedule', '主动调度'],
  ['proactive_skipped', '概率跳过'],
  ['proactive_stopped', 'STOP 拦截'],
  ['send_blocked', '发送被拦'],
  ['send_probe', '发送探针'],
  ['draft', '草稿'],
  ['context_budget', '上下文裁剪'],
  ['voice_polished', '语气润色'],
  ['config_reloaded', '配置热加载'],
  ['gateway_started', '网关启动'],
  ['gateway_stale', '死区告警'],
];

/* ---------------- 小工具 ---------------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtTime(ts) {
  if (!ts && ts !== 0) return '—';
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtDateTime(ts) {
  if (!ts && ts !== 0) return '—';
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { hour12: false });
}

function fmtAgo(ts) {
  if (!ts && ts !== 0) return '—';
  const diff = Math.max(0, Date.now() / 1000 - Number(ts));
  if (diff < 60) return Math.round(diff) + ' 秒前';
  if (diff < 3600) return Math.round(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.round(diff / 3600) + ' 小时前';
  return Math.round(diff / 86400) + ' 天前';
}

function pct(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (Number.isNaN(number)) return '—';
  return (number * 100).toFixed(digits) + '%';
}

function num(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

let toastSeq = 0;
function toast(message, type) {
  const wrap = $('#toast-wrap');
  const node = document.createElement('div');
  node.className = 'toast ' + (type || '');
  node.textContent = message;
  wrap.appendChild(node);
  const id = ++toastSeq;
  setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, id > 6 ? 5000 : 4200);
}

function pill(text, kind, dot = true) {
  return `<span class="pill ${kind || ''}">${dot ? '<span class="dot"></span>' : ''}${esc(text)}</span>`;
}

function badge(text, cls) {
  return `<span class="badge ${cls || ''}">${esc(text)}</span>`;
}

function card(title, bodyHtml, extraClass) {
  return `<section class="card ${extraClass || ''}">
    <h3>${esc(title)}</h3>
    ${bodyHtml}
  </section>`;
}

function showModal(title, data) {
  $('#modal-title').textContent = title || '详情';
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  $('#modal-body').textContent = text;
  $('#modal-mask').classList.add('open');
}
function closeModal() { $('#modal-mask').classList.remove('open'); }

function setLastRefresh() {
  const stamp = new Date();
  state.lastRefresh = stamp.getTime();
  $('#last-refresh').textContent = '刷新于 ' + stamp.toLocaleTimeString('zh-CN', { hour12: false });
}

/* ---------------- API ---------------- */
async function api(path, options) {
  const opts = options || {};
  const headers = { 'Accept': 'application/json' };
  if (opts.method === 'POST') headers['Content-Type'] = 'application/json';
  // 注意：GET 也要带 token（有些只读接口也要求 admin token，之前只有 POST 带过）
  if (state.token) headers['X-Admin-Token'] = state.token;
  let response;
  try {
    response = await fetch(path, {
      method: opts.method || 'GET',
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: 'no-store',
    });
  } catch (err) {
    throw new Error('请求失败：' + (err && err.message ? err.message : err) + '（服务没起？）');
  }
  let payload = null;
  try { payload = await response.json(); } catch (err) { payload = null; }
  if (response.status === 401) {
    throw new Error('需要 admin token：请在右上角填入 agi/data/wechat/config.json 的 admin_token');
  }
  if (!response.ok || (payload && payload.ok === false)) {
    const detail = payload ? (payload.error || (payload.errors || []).join('; ')) : response.status;
    throw new Error(detail || ('HTTP ' + response.status));
  }
  return payload || {};
}

/* ---------------- 状态 pills ---------------- */
function renderPills() {
  const pills = [];
  const status = state.status;
  if (status && status.ok && status.status) {
    pills.push(pill('gateway 在线', 'ok'));
    const proactive = status.status.proactive || {};
    pills.push(pill(proactive.enabled ? '主动消息开' : '主动消息关', proactive.enabled ? 'info' : ''));
    if (proactive.stopped) pills.push(pill('STOP 已停发', 'err'));
  } else if (status) {
    pills.push(pill('gateway 未连接', 'err'));
  }
  if (state.config && state.config.config) {
    const cfg = state.config.config;
    pills.push(pill('inner: ' + (cfg.inner_mode || 'off'), cfg.inner_mode && cfg.inner_mode !== 'off' ? 'info' : ''));
    if (cfg.inner_shadow_only) pills.push(pill('shadow 只记录', 'warn'));
  }
  const wrap = $('#status-pills');
  if (wrap) wrap.innerHTML = pills.join('');
}

/* ---------------- 总览 ---------------- */
async function renderOverview() {
  $('#app').innerHTML = '<div class="empty">加载总览…</div>';
  let status = null, feed = null, config = null, models = null;
  const errors = [];
  try { status = await api('/api/status'); } catch (e) { errors.push('gateway: ' + e.message); }
  try { feed = await api('/api/feed?limit=6'); } catch (e) { errors.push('事件流: ' + e.message); }
  try { config = await api('/api/config'); } catch (e) { errors.push('配置: ' + e.message); }
  try { models = await api('/api/models'); } catch (e) { errors.push('模型: ' + e.message); }
  state.status = status; state.config = config; state.models = models;
  renderPills();

  if (!status || !status.ok) {
    const reason = (status && status.error) || (errors[0] || '未知错误');
    $('#app').innerHTML = card('网关状态', `<div class="error-text">gateway 未连接：${esc(reason)}</div>
      <p class="hint">确认 <code>python -m agi.wechat_gateway.server</code> 正在 127.0.0.1:801 运行；本页数据仍会显示本机文件部分。</p>`) +
      card('最近事件', renderEventList((feed && feed.items) || []));
    return;
  }

  const st = status.status || {};
  const proactive = st.proactive || {};
  const innerMode = (config && config.config && config.config.inner_mode) || 'off';
  const innerShadow = config && config.config ? !!config.config.inner_shadow_only : false;
  const sent = (st.send || {}).sent_today;
  const active = (st.backend || {}).active;
  const recent = (st.recent || []).slice(-5).reverse();
  const modelItems = (models && models.models) || [];
  const soul = modelItems.find((m) => m.name === 'soul-05b');
  const innerRows = `<div class="kv">
      <span class="k">模式</span><span class="v">${esc(innerMode)}${innerShadow ? '（shadow 只记录）' : ''}</span>
      <span class="k">scope</span><span class="v">${esc((config && config.config && config.config.inner_mode_scope) || '—')}</span>
      <span class="k">schema</span><span class="v">${esc((config && config.config && config.config.inner_schema) || '—')}</span>
      <span class="k">soul-05b</span><span class="v">${soul ? (soul.healthy ? '健康（端口 ' + esc(soul.port) + '）' : '未运行（端口 ' + esc(soul.port) + '）') : '未登记'}</span>
    </div>`;

  const html = `
    ${errors.length ? `<div class="card"><div class="error-text">部分数据获取失败：${esc(errors.join('；'))}</div></div>` : ''}
    <div class="grid cols-4">
      ${card('主动消息', `<div class="big">${proactive.enabled ? '开' : '关'}</div>
        <div class="muted">概率 ${num((proactive.probability || {}).min)}–${num((proactive.probability || {}).max)} · 间隔 ${num((proactive.interval_s || {}).min)}–${num((proactive.interval_s || {}).max)}s</div>
        <div class="row" style="margin-top:8px">${proactive.stopped ? pill('STOP 已停发', 'err') : pill('未停发', 'ok')}</div>`)}
      ${card('今日发送', `<div class="big">${num(sent)}</div><div class="muted">失败 ${num((st.send || {}).failed)} · dry_run ${(st.send || {}).dry_run ? '是' : '否'}</div>`)}
      ${card('后台监听', `<div class="big">${active ? '运行' : '未运行'}</div><div class="muted">最近事件 ${fmtAgo(state.lastRefresh / 1000)}刷新</div>
        <div class="muted">${proactive.stale ? pill('死区告警', 'warn') : pill('无死区', 'ok')}</div>`)}
      ${card('内状态 / 模型', innerRows)}
    </div>
    <div class="grid cols-2">
      ${card('最近 5 条事件', renderEventList(recent.length ? recent : (feed && feed.items) || []))}
      ${card('最近对话片段', renderMiniMessages((st.recent || []).slice(-6).reverse()))}
    </div>`;
  $('#app').innerHTML = html;
}

function renderEventList(items) {
  if (!items || !items.length) return '<div class="empty">暂无事件</div>';
  return '<div class="list">' + items.map((event) => {
    const kind = event.kind || 'event';
    const text = event.text || event.error || event.reason || event.next_delay_s || '';
    const cls = kindCls(kind);
    return `<div class="list-item" data-json='${esc(JSON.stringify(event))}'>
      <span class="time">${fmtTime(event.ts)}</span>
      <div class="grow"><div class="title">${badge(kind, cls)} ${esc(String(text).slice(0, 140))}</div></div>
      <span class="badge">${esc(event.friend || event.contact || '')}</span>
    </div>`;
  }).join('') + '</div>';
}

function renderMiniMessages(items) {
  if (!items || !items.length) return '<div class="empty">暂无消息</div>';
  return '<div class="feed">' + items.map((m) => {
    const mine = m.role !== 'user';
    return `<div class="bubble-row ${mine ? 'mine' : ''}">
      <div class="bubble"><div class="meta">${esc(m.role)} · ${fmtTime(m.ts)}</div>
      <div class="text">${esc(String(m.text || '').slice(0, 240))}</div></div></div>`;
  }).join('') + '</div>';
}

/* ---------------- 对话流 ---------------- */
function kindCls(kind) {
  if (kind === 'inbound') return 'blue';
  if (kind === 'outbound') return 'green';
  if (kind === 'proactive_send') return 'purple';
  if (kind === 'send_blocked' || kind === 'proactive_stopped') return 'red';
  if (kind === 'proactive_skipped' || kind === 'gateway_stale') return 'orange';
  return '';
}

function feedList(items) {
  if (!items || !items.length) return '<div class="empty">还没有事件（先让网关收/发一条消息）</div>';
  const rows = [];
  for (const event of items) {
    const kind = event.kind || '';
    if (kind === 'inbound' || kind === 'outbound' || kind === 'proactive_send'
        || kind === 'draft' || kind === 'send_blocked') {
      rows.push(feedBubble(event));
    } else {
      rows.push(`<div class="list-item" data-json='${esc(JSON.stringify(event))}'>
        <span class="time">${fmtTime(event.ts)}</span>
        <div class="grow"><div class="title">${badge(kind, kindCls(kind))} ${esc(feedSummary(event))}</div></div>
      </div>`);
    }
  }
  return '<div class="feed">' + rows.join('') + '</div>';
}

function feedSummary(event) {
  if (event.kind === 'proactive_schedule') return '下次检查间隔 ' + num(event.next_delay_s) + 's';
  if (event.kind === 'proactive_skipped') return '概率跳过 p=' + num(event.probability) + ' roll=' + num(event.roll);
  if (event.kind === 'send_blocked') return '被拦：' + (event.reason || event.error || '');
  if (event.kind === 'context_budget') return 'tokens=' + num(event.tokens) + ' dropped=' + esc((event.dropped || []).join(','));
  if (event.kind === 'config_reloaded') return '配置变化：' + esc((event.changed || []).join(','));
  if (event.kind === 'gateway_stale') return '空闲 ' + num(event.idle_s) + 's';
  return event.error || event.reason || event.source || '';
}

function feedBubble(event) {
  const kind = event.kind;
  const text = event.text || (event.result && event.result.text) || event.reason || '';
  const mine = kind === 'outbound' || kind === 'proactive_send' || kind === 'draft';
  const blocked = kind === 'send_blocked';
  const cls = [mine ? 'mine' : '', kind === 'proactive_send' ? 'proactive' : '',
    blocked ? 'blocked' : ''].join(' ');
  const status = event.status ? badge(String(event.status), blocked ? 'red' : 'green') : '';
  const reason = blocked ? `<div class="hint" style="color:var(--error)">${esc(event.reason || event.error || '被拦截')}</div>` : '';
  return `<div class="bubble-row ${cls}">
    <div class="bubble" data-json='${esc(JSON.stringify(event))}'>
      <div class="meta">${badge(kind, kindCls(kind))} ${status} <span>${fmtTime(event.ts)}</span>
        ${event.friend ? `<span>· ${esc(event.friend)}</span>` : ''}</div>
      <div class="text">${esc(String(text || '（无文本）').slice(0, 1000))}</div>
      ${reason}
      <div class="tap">点击查看 JSON</div>
    </div></div>`;
}

function feedSignature(items) {
  return (items || []).slice(0, 60).map((e) => [e.ts, e.kind, e.status || '', e.text || '', e.reason || ''].join('~')).join('|');
}

async function renderFeed(auto) {
  if (!auto) $('#app').innerHTML = '<div class="empty">加载对话流…</div>';
  let feed = null;
  const errors = [];
  try { feed = await api('/api/feed?limit=200&kind=' + encodeURIComponent(state.feedKind)); }
  catch (e) { errors.push(e.message); }
  // 自动刷新：内容没变就直接返回，避免整页重绘闪烁
  if (auto) {
    const items = (feed && feed.items) || [];
    const sig = feedSignature(items);
    const listEl = $('#feed-list');
    const countEl = $('#feed-count');
    if (listEl && countEl && sig === state.feedSig) return;
    if (listEl && countEl) {
      state.feedSig = sig;
      countEl.textContent = items.length + ' 条 · 文件 ' + ((feed && feed.source) || '');
      listEl.innerHTML = feedList(items);
      return;
    }
  }
  const html = `
    ${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="card">
      <div class="row between">
        <div class="row">
          <select id="feed-kind">${FEED_KINDS.map(([value, label]) =>
            `<option value="${value}" ${value === state.feedKind ? 'selected' : ''}>${label}</option>`).join('')}</select>
          <label class="switch"><input type="checkbox" id="feed-auto" ${state.feedAuto ? 'checked' : ''}> 自动刷新 3s</label>
        </div>
        <div class="refresh-bar"><span id="feed-count">${feed ? (feed.items || []).length : 0} 条 · 文件 ${esc((feed && feed.source) || '')}</span></div>
      </div>
      <div id="feed-list" style="margin-top:12px">${feedList((feed && feed.items) || [])}</div>
    </div>`;
  $('#app').innerHTML = html;
  state.feedSig = feedSignature((feed && feed.items) || []);
  const select = $('#feed-kind');
  if (select) select.onchange = () => { state.feedKind = select.value; renderFeed(false); };
  const autoBox = $('#feed-auto');
  if (autoBox) autoBox.onchange = () => { state.feedAuto = autoBox.checked; };
}

/* ---------------- 内状态 ---------------- */
/* ---------------- 心情卡片（P9-c） ---------------- */
const MOOD_DIMS = [
  ['happy', '开心', 1], ['calm', '踏实', 1], ['sweet', '甜蜜', 1],
  ['heartache', '心疼', -1], ['worry', '担心', -1], ['sad', '难过', -1],
  ['jealous', '吃醋', -1],
];

function moodDimRowsHtml(dims) {
  return '<div class="mood-dims">' + MOOD_DIMS.map(([key, label, polarity]) => {
    const value = Number((dims || {})[key] || 0);
    const width = Math.max(2, Math.min(100, Math.abs(value)));
    return `<div class="mood-dim" title="${esc(label)}：${value}">
      <span class="mood-dim-label">${esc(label)}</span>
      <span class="mood-dim-track"><span class="mood-dim-fill ${polarity >= 0 ? 'pos' : 'neg'}"
        style="width:${width}%"></span></span>
      <span class="mood-dim-value">${value.toFixed(1)}</span>
    </div>`;
  }).join('') + '</div>';
}

function moodTimelineHtml(timeline) {
  const rows = (timeline || []).slice(0, 10);
  if (!rows.length) {
    return '<div class="empty">还没有心情记录（mood_judge 未启用或还没跑过判定）</div>';
  }
  const labelOf = {};
  MOOD_DIMS.forEach(([key, label]) => { labelOf[key] = label; });
  return `<div style="overflow:auto"><table><thead><tr>
      <th>时间</th><th>mood</th><th>主要变化</th><th>reason</th><th>confidence</th><th>mode</th>
    </tr></thead><tbody>${rows.map((row) => {
    const delta = row.dims_delta || {};
    const top = Object.entries(delta)
      .filter(([, value]) => Number(value) !== 0)
      .sort((a, b) => Math.abs(Number(b[1])) - Math.abs(Number(a[1])))
      .slice(0, 3)
      .map(([key, value]) => `${labelOf[key] || key}${Number(value) > 0 ? '+' : ''}${Math.round(Number(value))}`)
      .join(' ');
    return `<tr><td>${row.at ? fmtDateTime(row.at) : '—'}</td>
      <td class="num">${row.mood === undefined ? '—' : esc(row.mood)}</td>
      <td>${esc(top || '—')}</td>
      <td>${esc(String(row.reason || '').slice(0, 80))}</td>
      <td class="num">${esc(row.confidence === undefined ? '—' : row.confidence)}</td>
      <td>${badge(row.mode || '')}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function moodCardHtml(mood) {
  if (!mood) return '';
  if (!mood.ok) {
    return card('心情', `<div class="error-text">心情不可用：${esc(mood.error || 'mood_unavailable')}</div>
      <p class="hint">${esc(mood.hint || '')}</p>`);
  }
  const settings = mood.settings || {};
  const dims = mood.dims || {};
  const pending = !!mood.pending_inject;
  const injectOn = !!settings.inject;
  const judgeOn = !!settings.judge_enabled;
  const preview = String(mood.injected_preview || '').trim();
  const confidenceMin = Number(settings.confidence_min || 0);
  return `<div class="card mood-card">
    <div class="row between">
      <div class="row mood-head">
        <div class="mood-big">${esc(mood.mood === undefined ? '—' : Number(mood.mood).toFixed(0))}</div>
        <div>
          <div class="mood-label">${esc(mood.mood_label || '')}</div>
          <div class="hint">${esc(mood.last_reason || '还没有心情变化记录')}</div>
        </div>
        <div class="row mood-badges">
          ${judgeOn ? badge('判定开', 'green') : badge('判定关', 'orange')}
          ${injectOn ? badge('注入开', 'green') : badge('注入关', '')}
          ${pending ? badge('有待注入变化', 'blue') : badge('无待注入', '')}
          <span class="hint">后端 ${esc(settings.backend || 'rule/model')} · 门槛 ≥${esc(confidenceMin)}</span>
        </div>
      </div>
      <div class="row">
        <button class="btn small" id="mood-refresh">刷新心情</button>
        <button class="btn small" data-mood-toggle="inject">${injectOn ? '关闭注入' : '开启注入'}</button>
        <button class="btn small ${judgeOn ? 'danger' : ''}" data-mood-toggle="enabled">${judgeOn ? '关闭判定' : '开启判定'}</button>
      </div>
    </div>
    ${moodDimRowsHtml(dims)}
    <div class="mood-inject"><span class="hint">下一轮会注入的那句话（预览）：</span>
      ${preview ? `<code class="mood-preview">${esc(preview)}</code>`
        : '<span class="hint">（没有待注入内容：判定关 / 未过 confidence 门槛 / 这次变化已经说过了）</span>'}
    </div>
    <div class="mood-timeline">
      <div class="row between" style="margin-bottom:6px"><b>最近心情时间线</b>
        <span class="hint">${esc(mood.db || '')} · ${esc(mood.settings_path || '')}</span></div>
      ${moodTimelineHtml(mood.timeline)}
    </div>
  </div>`;
}

async function toggleMood(kind, value) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  try {
    const body = kind === 'inject' ? { inject: value } : { enabled: value };
    const result = await api('/api/mood/' + kind, { method: 'POST', body });
    toast('已更新：' + (result.note || '下一轮生效'), 'ok');
    renderInner();
  } catch (err) { toast(err.message, 'err'); }
}
async function renderInner() {
  $('#app').innerHTML = '<div class="empty">加载内状态…</div>';
  let data = null, mood = null;
  try { data = await api('/api/inner?limit=100'); }
  catch (e) {
    $('#app').innerHTML = card('内状态', `<div class="error-text">${esc(e.message)}</div>`);
    return;
  }
  state.inner = data;
  try { mood = await api('/api/mood?limit=10'); }
  catch (e) { mood = { ok: false, error: e.message, hint: '心情卡片需要 admin token' }; }
  state.mood = mood;
  const items = (data && data.items) || [];
  const cards = items.map((item) => innerCard(item)).join('');
  const listError = (!data.ok && !items.length)
    ? `<div class="card"><div class="error-text">${esc(data.error || '读取失败')}</div></div>`
    : '';
  $('#app').innerHTML = `${moodCardHtml(mood)}
    ${listError}
    <div class="card">
      <div class="row between"><div class="hint">来源：${esc((data && data.db) || '')} · 只读</div>
        <span class="hint">“注入”= 这条 inner block 进入了 prompt；shadow 模式只记录不注入。</span></div>
    </div>
    <div class="grid cols-2">${cards || '<div class="empty">还没有 inner_states（INNER_MODE 未开或还没新消息）</div>'}</div>`;
  const refresh = $('#mood-refresh');
  if (refresh) refresh.onclick = () => renderInner();
  $$('[data-mood-toggle]').forEach((button) => {
    button.onclick = () => {
      const kind = button.dataset.moodToggle;
      const settings = (state.mood && state.mood.settings) || {};
      const current = kind === 'inject' ? !!settings.inject : !!settings.judge_enabled;
      if (kind === 'enabled' && !current
          && !window.confirm('当前 mood.json 里 judge_enabled=false（等 P9-b 后端切换），仍要打开模型判定吗？')) {
        return;
      }
      toggleMood(kind, !current);
    };
  });
}

function stateCell(label, value) {
  return `<div class="state-cell"><b>${esc(label)}</b>${esc(value === undefined || value === null ? '—' : value)}</div>`;
}

function innerCard(item) {
  const st = item.state || {};
  const behavior = st.behavior || {};
  const modeBadge = badge(item.mode || '?', item.mode === 'model' ? 'purple' : (item.mode === 'rule' ? 'blue' : ''));
  const injected = item.injected ? badge('已注入 prompt', 'green') : badge('未注入', 'orange');
  const latency = item.latency_ms !== undefined && item.latency_ms !== null
    ? badge(Number(item.latency_ms).toFixed(0) + 'ms') : '';
  const fallback = item.fallback ? badge('fallback', 'red') : '';
  const source = item.source ? badge('source: ' + item.source, '') : '';
  return `<div class="inner-card">
    <div class="top">${modeBadge} ${injected} ${latency} ${fallback} ${source}
      <span class="hint">${fmtDateTime(item.created_at)}</span></div>
    <div class="monologue">“${esc(st.monologue || st.intent || '（无 monologue）')}”</div>
    <div class="state-grid">
      ${stateCell('stance', st.stance)}
      ${stateCell('intent', st.intent)}
      ${stateCell('mood', st.mood)}
      ${stateCell('energy', st.energy)}
      ${stateCell('sharing', st.sharing)}
      ${stateCell('trust', st.trust)}
      ${stateCell('stage', st.stage)}
      ${stateCell('length', behavior.length)}
      ${stateCell('ask', behavior.ask === undefined ? '—' : (behavior.ask ? '是' : '否'))}
      ${stateCell('advice', behavior.advice === undefined ? '—' : (behavior.advice ? '是' : '否'))}
      ${stateCell('emoji', behavior.emoji)}
    </div>
    <div class="block">${esc(String(item.block || '').slice(0, 800))}</div>
  </div>`;
}

/* ---------------- 记忆 ---------------- */
async function renderMemory() {
  $('#app').innerHTML = '<div class="empty">加载记忆指标…</div>';
  let data = null, tone = null;
  const errors = [];
  try { data = await api('/api/memory'); } catch (e) { errors.push('记忆: ' + e.message); }
  try { tone = await api('/api/tone?limit=100'); } catch (e) { errors.push('语气: ' + e.message); }
  state.memory = data; state.tone = tone;
  const m = (data && data.metrics) || {};
  const memories = m.memories || {};
  const messages = m.messages || {};
  const retrieval = m.retrieval || {};
  const tiers = memories.by_tier || {};
  const status = memories.by_status || {};
  const recent = (data && data.recent) || [];

  const metricCards = `
    <div class="grid cols-4">
      ${card('记忆总数', `<div class="big">${num(memories.total)}</div>
        <div class="muted">active ${num(status.active)} · dormant ${num(status.dormant)} · pending ${num(status.pending)}</div>`)}
      ${card('时间分层（active）', `<div class="big">${num(tiers.hot)} / ${num(tiers.warm)} / ${num(tiers.cold)}</div>
        <div class="muted">hot ≤30d · warm 30–90d · cold >90d</div>`)}
      ${card('休眠率', `<div class="big">${pct(memories.dormant_rate)}</div>
        <div class="muted">阈值 >40% 告警（nightly）</div>`)}
      ${card('检索命中率', `<div class="big">${pct(retrieval.hit_rate)}</div>
        <div class="muted">调用 ${num(retrieval.calls)} · 平均 ${num(retrieval.avg_items)} 条/次 · 未落盘 ${num(retrieval.pending)}</div>`)}
    </div>
    <div class="grid cols-3">
      ${card('原文', `<div class="kv">
        <span class="k">总数</span><span class="v">${num(messages.total)}</span>
        <span class="k">已归档</span><span class="v">${num(messages.archived)}</span>
        <span class="k">活跃</span><span class="v">${num(messages.active)}</span>
        <span class="k">最老活跃</span><span class="v">${num(messages.oldest_active_days)} 天</span></div>`)}
      ${card('合并 / 重要性', `<div class="kv">
        <span class="k">30 天内合并</span><span class="v">${num(memories.merged_within_30d)}</span>
        <span class="k">平均重要性</span><span class="v">${num(memories.avg_importance)}</span>
        <span class="k">中位重要性</span><span class="v">${num(memories.median_importance)}</span></div>`)}
      ${card('摘要 / claims', `<div class="kv">
        <span class="k">claims</span><span class="v">${num((m.claims || {}).total)}</span>
        <span class="k">摘要</span><span class="v">${num((m.summaries || {}).total)}</span>
        <span class="k">月 gist</span><span class="v">${num((m.summaries || {}).monthly)}（${esc((m.summaries || {}).latest_month || '—')}）</span></div>`)}
    </div>`;

  const memoryTable = `<table><thead><tr><th>内容</th><th>kind</th><th>重要性</th><th>状态</th><th>创建</th><th>访问</th></tr></thead>
    <tbody>${recent.map((row) => `<tr><td>${esc(String(row.content || '').slice(0, 120))}</td>
      <td>${badge(row.kind || '')}</td><td class="num">${num(row.importance)}</td>
      <td>${badge(row.status || '', row.status === 'active' ? 'green' : (row.status === 'dormant' ? 'orange' : ''))}</td>
      <td>${fmtDateTime(row.created_at)}</td><td class="num">${num(row.access_count)}</td></tr>`).join('') ||
      '<tr><td colspan="6" class="empty">暂无记忆</td></tr>'}</tbody></table>`;

  const toneCard = renderToneCard(tone);
  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${metricCards}
    ${card('最近记忆（只读）', memoryTable)}
    ${toneCard}`;
}

function renderToneCard(tone) {
  if (!tone || !tone.ok) {
    return card('AI 语气 vs 真人基线', `<div class="error-text">${esc((tone && tone.error) || '暂无数据')}</div>`);
  }
  const ai = tone.ai || {};
  const base = tone.baseline || {};
  const ending = ai.ending || {};
  const baseEnding = base.ending || {};
  const rows = [
    ['条数', num(ai.count), num(base.self_rows)],
    ['长度中位数', num(ai.length_median), num(base.length_median)],
    ['无标点句尾', pct(ending.none), pct(baseEnding.none)],
    ['问号收尾', pct(ending.question), pct(baseEnding.question)],
    ['句号收尾', pct(ending.period), pct(baseEnding.period)],
    ['口语词覆盖', pct(ai.filler_rate), '—'],
    ['emoji 使用率', '—', pct(base.emoji_rate)],
  ];
  return card('AI 语气 vs 真人基线（最近 assistant 消息）', `
    <table><thead><tr><th>指标</th><th class="num">AI</th><th class="num">真人 self</th></tr></thead>
    <tbody>${rows.map(([name, a, b]) => `<tr><td>${esc(name)}</td><td class="num">${esc(a)}</td><td class="num">${esc(b)}</td></tr>`).join('')}</tbody></table>
    <p class="hint" style="margin-top:10px">基线来源：${esc(tone.baseline_note || '')}；真人数据见 eval_data/human_tone/profile.md。差距越大越值得调提示词。</p>`);
}

/* ---------------- 提示词（P4-d） ---------------- */
async function renderPrompts() {
  $('#app').innerHTML = '<div class="empty">加载提示词…</div>';
  let data = null;
  try { data = await api('/api/prompts'); }
  catch (e) {
    $('#app').innerHTML = card('提示词', `<div class="error-text">${esc(e.message)}</div>`);
    return;
  }
  state.prompts = data;
  const keys = promptKeys();
  if (!state.promptKey || !keys.includes(state.promptKey)) state.promptKey = keys[0] || null;
  renderPromptsPage();
}

function promptKeys() {
  return Object.keys((state.prompts && state.prompts.effective) || {});
}

function promptOverrides() {
  return (state.prompts && state.prompts.overrides) || {};
}

function isPromptOverridden(key) {
  const overrides = promptOverrides();
  return Object.prototype.hasOwnProperty.call(overrides, key)
    && String(overrides[key] || '') !== '';
}

function promptDefault(key) {
  return ((state.prompts || {}).defaults || {})[key] || '';
}

function promptEffective(key) {
  const effective = (state.prompts || {}).effective || {};
  return effective[key] !== undefined ? effective[key] : promptDefault(key);
}

function renderPromptsPage() {
  const data = state.prompts || {};
  const labels = data.labels || {};
  const config = data.config || {};
  const keys = promptKeys();
  const selected = state.promptKey;
  const degraded = !!data.degraded;
  const overrideCount = keys.filter(isPromptOverridden).length;
  const banner = `<div class="prompt-banner">
      <span>单条 ≤ ${esc(config.max_chars || 4000)} 字 · 删除 <code>agi/data/prompts.json</code> 即全部回滚 · 改完建议先去「对话流」观察效果</span>
      <span class="hint">${overrideCount} 个键已覆盖 / 共 ${keys.length} 个</span>
    </div>` + (degraded
      ? '<div class="prompt-banner warn">prompt_store 不可用：降级为直接读写 prompts.json（看不到代码默认值，也没有 11 键校验）</div>'
      : '');
  const left = keys.length ? keys.map((key) => {
    const label = labels[key] || {};
    const over = isPromptOverridden(key);
    return `<div class="prompt-item ${key === selected ? 'active' : ''}" data-prompt-select="${esc(key)}">
      <div class="row between"><span class="prompt-label">${esc(label.label || key)}</span>
        ${over ? badge('已覆盖', 'orange') : badge('默认', '')}</div>
      <div class="prompt-key mono">${esc(key)}</div>
    </div>`;
  }).join('') : '<div class="empty">没有可编辑的内置提示词</div>';

  let right = '<div class="empty">选择左侧一个键开始编辑</div>';
  if (selected) {
    const label = labels[selected] || {};
    const over = isPromptOverridden(selected);
    const value = promptEffective(selected);
    const def = promptDefault(selected);
    right = `<div class="card prompt-editor">
      <div class="card-head">
        <div><b>${esc(label.label || selected)}</b> <span class="mono hint">${esc(selected)}</span>
          ${over ? badge('已覆盖', 'orange') : badge('默认', '')}</div>
        <div class="row">
          <button class="btn" id="btn-prompt-reset" ${over ? '' : 'disabled'}>恢复默认</button>
          <button class="btn primary" id="btn-prompt-save">保存</button>
        </div>
      </div>
      <p class="hint">${esc(label.desc || '')}</p>
      <textarea id="prompt-editor" class="mono prompt-textarea" spellcheck="false">${esc(value)}</textarea>
      <div class="row between" style="margin-top:8px">
        <span class="hint" id="prompt-count">${esc(String(value).length)} 字</span>
        <span class="hint">${over ? '当前是覆盖值；点「恢复默认」回代码默认' : '当前是代码默认值'}</span>
      </div>
      ${over ? `<details class="prompt-default"><summary>覆盖前默认值对比</summary><pre class="json">${esc(def)}</pre></details>` : ''}
    </div>`;
  }

  $('#app').innerHTML = `${banner}<div class="prompt-layout">
      <div class="card prompt-list">
        <div class="row between" style="margin-bottom:8px"><span class="hint">内置提示词（11 键）</span>
          <button class="btn small danger" id="btn-prompt-reset-all" ${overrideCount ? '' : 'disabled'}>全部恢复默认</button></div>
        ${left}
      </div>
      ${right}
    </div>`;

  $$('[data-prompt-select]').forEach((item) => {
    item.onclick = () => { state.promptKey = item.dataset.promptSelect; renderPromptsPage(); };
  });
  const save = $('#btn-prompt-save');
  if (save) save.onclick = savePromptKey;
  const reset = $('#btn-prompt-reset');
  if (reset) reset.onclick = () => resetPrompt(false);
  const resetAll = $('#btn-prompt-reset-all');
  if (resetAll) resetAll.onclick = () => resetPrompt(true);
  const editor = $('#prompt-editor');
  if (editor) {
    editor.oninput = () => {
      const counter = $('#prompt-count');
      if (counter) counter.textContent = editor.value.length + ' 字';
    };
  }
}

async function refreshPromptsData() {
  try {
    state.prompts = await api('/api/prompts');
    renderPromptsPage();
    setLastRefresh();
  } catch (e) { toast('刷新提示词失败：' + e.message, 'err'); }
}

async function savePromptKey() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const key = state.promptKey;
  const editor = $('#prompt-editor');
  if (!key || !editor) return;
  const text = editor.value;
  const limit = ((state.prompts.config || {}).max_chars) || 4000;
  if (text.length > limit) {
    toast('超过单条 ' + limit + ' 字上限，未保存', 'err');
    return;
  }
  try {
    const result = await api('/api/prompts', {
      method: 'POST', body: { prompts: { [key]: text } },
    });
    const changed = (result.changed_keys || []).length;
    toast(changed ? '已保存，下一条消息 / 下一次 tick 生效' : '内容未变化', 'ok');
    await refreshPromptsData();
  } catch (e) { toast(e.message, 'err'); }
}

async function resetPrompt(all) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const key = state.promptKey;
  if (!all) {
    const label = ((state.prompts.labels || {})[key] || {}).label || key;
    if (!confirm('把「' + label + '」恢复成代码默认？')) return;
  } else if (!confirm('把 11 个内置提示词全部恢复默认（删除所有覆盖）？')) {
    return;
  }
  try {
    const body = all ? { all: true } : { keys: [key] };
    await api('/api/prompts/reset', { method: 'POST', body });
    toast('已恢复默认，下一条消息 / 下一次 tick 生效', 'ok');
    await refreshPromptsData();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 模型 ---------------- */
async function renderModels() {
  $('#app').innerHTML = '<div class="empty">加载模型…</div>';
  let models = null, config = null;
  const errors = [];
  try { models = await api('/api/models'); } catch (e) { errors.push(e.message); }
  try { config = await api('/api/config'); } catch (e) { errors.push(e.message); }
  state.models = models;
  if (config) { state.config = config; renderPills(); }
  const items = (models && models.models) || [];
  const allow = (models && models.allow_start) || [];
  const cards = items.map((model) => `
    <div class="card">
      <div class="row between">
        <div><b>${esc(model.name)}</b> ${badge(model.role || '')} ${model.reserved ? badge('reserved', 'orange') : ''}</div>
        ${model.healthy ? pill('健康', 'ok') : pill('未运行', '')}
      </div>
      <div class="kv" style="margin-top:8px">
        <span class="k">端口</span><span class="v">${num(model.port)}</span>
        <span class="k">状态</span><span class="v">${esc(model.status || '—')}</span>
        <span class="k">用途</span><span class="v">${esc(model.purpose || '')}</span>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn small primary" data-model-start="${esc(model.name)}" ${model.editable ? '' : 'disabled'}>启动</button>
        <button class="btn small danger" data-model-stop="${esc(model.name)}" ${model.editable ? '' : 'disabled'}>停止</button>
        <span class="hint">${model.editable ? '' : (model.reserved ? '预留模型，禁止启停' : '只读')}</span>
      </div>
    </div>`).join('') || '<div class="empty">模型注册表为空（agi/data/local_models.json）</div>';

  const cfg = (config && config.config) || {};
  const innerForm = `
    <div class="grid cols-2">
      <div class="field"><label>inner_mode</label>
        <select id="cfg-inner-mode">${['off', 'static', 'rule', 'model'].map((mode) =>
          `<option value="${mode}" ${cfg.inner_mode === mode ? 'selected' : ''}>${mode}</option>`).join('')}</select></div>
      <div class="field"><label>scope</label>
        <select id="cfg-inner-scope">${['proactive_only', 'all', 'off'].map((scope) =>
          `<option value="${scope}" ${cfg.inner_mode_scope === scope ? 'selected' : ''}>${scope}</option>`).join('')}</select></div>
      <div class="field"><label>schema</label>
        <select id="cfg-inner-schema">${['slim', 'full'].map((schema) =>
          `<option value="${schema}" ${cfg.inner_schema === schema ? 'selected' : ''}>${schema}</option>`).join('')}</select></div>
      <div class="field"><label>shadow（只记录不注入）</label>
        <label class="switch"><input type="checkbox" id="cfg-inner-shadow" ${cfg.inner_shadow_only ? 'checked' : ''}> inner_shadow_only</label></div>
    </div>
    <div class="row" style="margin-top:12px"><button class="btn primary" id="btn-save-inner">保存内状态设置</button>
      <span class="hint">当前 shadow 灰度：mode=${esc(cfg.inner_mode || '—')} · shadow=${cfg.inner_shadow_only ? '开' : '关'} · scope=${esc(cfg.inner_mode_scope || '—')}</span></div>`;

  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="grid cols-3">${cards}</div>
    ${card('内状态注入开关（热加载，保存后下一条消息生效）', innerForm)}
    ${card('说明', `<p class="hint">启停按钮只允许 ${esc(allow.join(', ') || 'soul-05b')}；1.5B 为 reserved，故意拒绝。停止只 kill 本注册表启动的 PID。</p>`)}`;

  $$('[data-model-start]').forEach((btn) => {
    btn.onclick = async () => {
      if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
      btn.disabled = true;
      try {
        const result = await api('/api/models/start', { method: 'POST', body: { name: btn.dataset.modelStart } });
        toast('已启动 ' + btn.dataset.modelStart + '（PID ' + (result.pid || '?') + '）', 'ok');
        renderModels();
      } catch (e) { toast(e.message, 'err'); btn.disabled = false; }
    };
  });
  $$('[data-model-stop]').forEach((btn) => {
    btn.onclick = async () => {
      if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
      if (!confirm('停止 ' + btn.dataset.modelStop + '？')) return;
      btn.disabled = true;
      try {
        await api('/api/models/stop', { method: 'POST', body: { name: btn.dataset.modelStop } });
        toast('已停止 ' + btn.dataset.modelStop, 'ok');
        renderModels();
      } catch (e) { toast(e.message, 'err'); btn.disabled = false; }
    };
  });
  const saveInner = $('#btn-save-inner');
  if (saveInner) saveInner.onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    const updates = {
      inner_mode: $('#cfg-inner-mode').value,
      inner_mode_scope: $('#cfg-inner-scope').value,
      inner_schema: $('#cfg-inner-schema').value,
      inner_shadow_only: $('#cfg-inner-shadow').checked,
    };
    await saveConfig(updates);
    renderModels();
  };
}

/* ---------------- 设置 ---------------- */
function configCardHtml(cfg) {
  return `
    <div class="grid cols-2">
      <div class="field"><label>主动消息开关</label>
        <label class="switch"><input type="checkbox" id="cfg-proactive" ${cfg.proactive_enabled ? 'checked' : ''}> proactive_enabled</label></div>
      <div class="field"><label>概率区间（每轮随机 p）</label>
        <div class="row"><input type="number" id="cfg-pmin" step="0.05" min="0" max="1" value="${esc(cfg.proactive_probability_min)}" style="width:110px">
          <span class="hint">–</span>
          <input type="number" id="cfg-pmax" step="0.05" min="0" max="1" value="${esc(cfg.proactive_probability_max)}" style="width:110px"></div></div>
      <div class="field"><label>间隔区间（秒）</label>
        <div class="row"><input type="number" id="cfg-imin" step="60" min="10" value="${esc(cfg.proactive_interval_min_s)}" style="width:120px">
          <span class="hint">–</span>
          <input type="number" id="cfg-imax" step="60" min="10" value="${esc(cfg.proactive_interval_max_s)}" style="width:120px"></div></div>
      <div class="field"><label>夜间静默（开始–结束，跨零点）</label>
        <div class="row"><input type="number" id="cfg-qstart" min="0" max="24" value="${esc((cfg.quiet_hours || [23, 8])[0])}" style="width:90px">
          <span class="hint">–</span>
          <input type="number" id="cfg-qend" min="0" max="24" value="${esc((cfg.quiet_hours || [23, 8])[1])}" style="width:90px"></div></div>
    </div>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="btn-save-config">保存设置</button>
      <span class="hint">网关热加载：保存后下一轮 proactive / 下一条消息生效；不需要重启。</span>
    </div>`;
}

async function renderSettings() {
  $('#app').innerHTML = '<div class="empty">加载设置…</div>';
  let config = null, status = null;
  const errors = [];
  try { config = await api('/api/config'); } catch (e) { errors.push(e.message); }
  try { status = await api('/api/status'); } catch (e) { errors.push('gateway: ' + e.message); }
  state.config = config; state.status = status;
  renderPills();
  const cfg = (config && config.config) || {};
  const stopped = !!(config && config.stopped);
  const readonly = (config && config.readonly) || {};
  $('#app').innerHTML = `
    ${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${card('主动消息', configCardHtml(cfg))}
    ${card('紧急停止（STOP 文件）', `
      <div class="row between">
        <div>${stopped ? pill('STOP 已启用：所有外发被拦', 'err') : pill('未停发', 'ok')}
          <span class="hint">文件：agi/data/wechat/STOP；创建即停发，删除即恢复。</span></div>
        <div class="row">
          <button class="btn danger" id="btn-stop" ${stopped ? 'disabled' : ''}>立即停发</button>
          <button class="btn" id="btn-resume" ${stopped ? '' : 'disabled'}>恢复发送</button>
        </div></div>`)}
    ${card('网关只读配置', `<pre class="json">${esc(JSON.stringify(readonly, null, 2))}</pre>`)}
    ${card('网关原始状态', status && status.ok ? `<pre class="json">${esc(JSON.stringify(status.status, null, 2))}</pre>` :
      `<div class="error-text">gateway 未连接：${esc((status && status.error) || '未知')}</div>`)}
    ${card('危险区域：恢复出厂设置', `
      <p class="hint">清空所有运行数据（对话 / 记忆 / 人格 / 信任 / 日志 / 提示词覆盖）。模型与评测语料会保留。</p>
      <button class="btn factory" id="btn-factory">清空状态 / 恢复出厂设置</button>`, 'danger-zone')}`;

  const save = $('#btn-save-config');
  if (save) save.onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    const updates = {
      proactive_enabled: $('#cfg-proactive').checked,
      proactive_probability_min: Number($('#cfg-pmin').value),
      proactive_probability_max: Number($('#cfg-pmax').value),
      proactive_interval_min_s: Number($('#cfg-imin').value),
      proactive_interval_max_s: Number($('#cfg-imax').value),
      quiet_hours: [Number($('#cfg-qstart').value), Number($('#cfg-qend').value)],
    };
    await saveConfig(updates);
    renderSettings();
  };
  const stopBtn = $('#btn-stop');
  if (stopBtn) stopBtn.onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    try { await api('/api/proactive/stop', { method: 'POST', body: {} }); toast('已停发（STOP 文件已创建）', 'ok'); renderSettings(); }
    catch (e) { toast(e.message, 'err'); }
  };
  const resumeBtn = $('#btn-resume');
  if (resumeBtn) resumeBtn.onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    try { await api('/api/proactive/resume', { method: 'POST', body: {} }); toast('已恢复发送（STOP 文件已删除）', 'ok'); renderSettings(); }
    catch (e) { toast(e.message, 'err'); }
  };
  const factoryBtn = $('#btn-factory');
  if (factoryBtn) factoryBtn.onclick = openFactory;
}

/* ---------------- P4-c 恢复出厂（4 步挽留） ---------------- */
const FACTORY_PHRASE = '清空我和TA的回忆';
const factory = { step: 0, preview: null, busy: false };

function factoryStatsHtml(stats) {
  const items = [
    ['条对话', stats.messages],
    ['条记忆', stats.memories],
    ['条 claim', stats.claims],
    ['个联系人', stats.contacts],
  ];
  return '<div class="stat">' + items.map(([label, value]) =>
    `<div><b>${value === null || value === undefined ? '—' : esc(value)}</b><span>${label}</span></div>`).join('') + '</div>';
}

async function openFactory() {
  factory.step = 0;
  factory.preview = null;
  factory.busy = false;
  $('#factory-title').textContent = '恢复出厂设置';
  $('#factory-body').innerHTML = '<div class="empty">正在读取真实统计…</div>';
  $('#factory-actions').innerHTML = '<button class="btn" id="factory-cancel-2">取消</button>';
  $('#factory-mask').classList.add('open');
  bindFactoryCancel();
  try {
    factory.preview = await api('/api/factory/preview', { method: 'POST', body: {} });
  } catch (e) {
    factory.preview = { ok: false, error: e.message };
  }
  renderFactoryStep();
}

function bindFactoryCancel() {
  const btn = $('#factory-cancel-2');
  if (btn) btn.onclick = () => closeFactory();
}

function closeFactory(cancelled) {
  if (factory.busy) return;
  $('#factory-mask').classList.remove('open');
  if (cancelled !== false) toast('已取消，什么都没删', '');
}

function renderFactoryActions(buttons) {
  $('#factory-actions').innerHTML = buttons
    .map(([id, label, cls]) => `<button class="btn ${cls || ''}" id="${id}">${esc(label)}</button>`)
    .join('');
}

function renderFactoryStep() {
  const preview = factory.preview || {};
  const body = $('#factory-body');
  const step = factory.step;
  if (!preview.ok) {
    body.innerHTML = `<div class="factory-step"><p class="lead">读不到统计：${esc(preview.error || '未知错误')}</p>
      <p class="hint">为了安全，统计失败时不会允许继续。</p></div>`;
    renderFactoryActions([['factory-close', '关闭', ''], ['factory-cancel-2', '取消', '']]);
    bindFactoryCancel();
    return;
  }
  const stats = preview.stats || {};
  if (step === 0) {
    body.innerHTML = `<div class="factory-step">
      <p class="lead">这里有你跟 TA 的记录——</p>
      ${factoryStatsHtml(stats)}
      <p>还有 <b>${esc(stats.inner_states || 0)}</b> 条内状态、<b>${esc(stats.gateway_events || 0)}</b> 行网关事件、<b>${esc(stats.prompt_overrides || 0)}</b> 条提示词覆盖。</p>
      <p class="warn">真的要走吗？</p>
      <p class="factory-mini">这只是预览：现在点取消，不会删除任何东西。</p>
    </div>`;
    renderFactoryActions([['factory-cancel-2', '取消', ''], ['factory-next', '继续', 'primary']]);
    $('#factory-next').onclick = () => { factory.step = 1; renderFactoryStep(); };
  } else if (step === 1) {
    body.innerHTML = `<div class="factory-step">
      <p class="lead">清空之后，TA 会变成什么样？</p>
      <ul>
        <li>人格、记忆、信任度全部清零；</li>
        <li>TA 会完全不认识你，像第一次见面一样；</li>
        <li>聊天里的情绪、关系进展、内状态都会回到初始值；</li>
        <li>之后要重新花时间建立这些——就像重新认识一个人。</li>
      </ul>
      <p class="hint">如果只是想让 TA 安静一会儿，用「紧急停止」就够了。</p>
    </div>`;
    renderFactoryActions([['factory-back', '返回', ''], ['factory-cancel-2', '取消', ''], ['factory-next', '继续', 'primary']]);
    $('#factory-back').onclick = () => { factory.step = 0; renderFactoryStep(); };
    $('#factory-next').onclick = () => { factory.step = 2; renderFactoryStep(); };
    bindFactoryCancel();
  } else if (step === 2) {
    const keep = (preview.keep || []).map((item) => `<li>保留：${esc(item)}</li>`).join('');
    body.innerHTML = `<div class="factory-step">
      <p class="lead">这一步不可撤销。</p>
      <p>执行前我会先把要清空的内容打包成一份 zip 备份（备份失败就中止，绝不先删）：</p>
      <p><code>${esc(preview.backup_preview || 'E:\\agent\\backups\\factory_reset_<时间>.zip')}</code></p>
      <ul>${keep}</ul>
      <p class="hint">也就是说：你还有后悔药——备份文件会留在上面这个路径。</p>
    </div>`;
    renderFactoryActions([['factory-back', '返回', ''], ['factory-cancel-2', '取消', ''], ['factory-next', '我明白，继续', 'primary']]);
    $('#factory-back').onclick = () => { factory.step = 1; renderFactoryStep(); };
    $('#factory-next').onclick = () => { factory.step = 3; renderFactoryStep(); };
    bindFactoryCancel();
  } else {
    body.innerHTML = `<div class="factory-step">
      <p class="lead">最后一步：请输入下面这句话，才能解锁红色按钮。</p>
      <p><code>${esc(FACTORY_PHRASE)}</code></p>
      <input type="text" id="factory-phrase" class="factory-phrase" placeholder="在这里输入确认短语" autocomplete="off">
      <p class="factory-mini" id="factory-phrase-hint">必须完全一致（包括「TA」两个字母）。</p>
      <p class="hint">点击红色按钮后会：先备份 → 再清空。期间不要关闭页面。</p>
    </div>`;
    renderFactoryActions([['factory-back', '返回', ''], ['factory-cancel-2', '取消', ''], ['factory-confirm', '清空并备份', 'factory-confirm']]);
    const phrase = $('#factory-phrase');
    const confirmBtn = $('#factory-confirm');
    confirmBtn.disabled = true;
    phrase.oninput = () => {
      const ok = phrase.value === FACTORY_PHRASE;
      confirmBtn.disabled = !ok;
      $('#factory-phrase-hint').textContent = ok ? '已解锁：点击红色按钮执行（先备份后清空）' : '必须完全一致（包括「TA」两个字母）。';
    };
    $('#factory-back').onclick = () => { factory.step = 2; renderFactoryStep(); };
    confirmBtn.onclick = doFactoryReset;
    bindFactoryCancel();
    phrase.focus();
    return;
  }
  const cancel = $('#factory-cancel-2');
  if (cancel) cancel.focus();      // 默认焦点在「取消」
}

async function doFactoryReset() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const phrase = $('#factory-phrase').value;
  if (phrase !== FACTORY_PHRASE) return;
  factory.busy = true;
  $('#factory-title').textContent = '正在备份并清空…';
  $('#factory-body').innerHTML = '<div class="empty">正在打包备份，然后清空运行数据，请不要关闭页面…</div>';
  $('#factory-actions').innerHTML = '';
  try {
    const result = await api('/api/factory/reset', {
      method: 'POST',
      body: { confirm: phrase, backup: true },
    });
    factory.busy = false;
    const deleted = result.deleted || {};
    $('#factory-title').textContent = '已清空';
    $('#factory-body').innerHTML = `<div class="factory-step">
      <p class="lead" style="color:var(--success)">已清空完成。</p>
      <p>备份：<code>${esc(result.backup_path || '')}</code>（${esc(result.files_backed || 0)} 个文件）</p>
      <p>删除统计：${esc(deleted.messages || 0)} 条对话 / ${esc(deleted.memories || 0)} 条记忆 /
        ${esc(deleted.claims || 0)} 条 claim / ${esc(deleted.files || 0)} 个文件（${esc(result.took_s || 0)}s）</p>
      <p class="warn">${esc(result.restart_hint || '建议重启网关，让内存状态一起归零')}</p>
      <p class="hint">控制台不会替你杀网关进程；需要时按 README 手动重启。备份 zip 可直接解压恢复。</p>
    </div>`;
    renderFactoryActions([['factory-close', '关闭', 'primary']]);
    $('#factory-close').onclick = () => { $('#factory-mask').classList.remove('open'); go('settings'); };
    setLastRefresh();
  } catch (e) {
    factory.busy = false;
    $('#factory-title').textContent = '未执行（数据仍在）';
    $('#factory-body').innerHTML = `<div class="factory-step">
      <p class="lead warn">中止了：${esc(e.message)}</p>
      <p class="hint">备份失败或校验不通过时，控制台不会删除任何数据；你可以返回重试或取消。</p>
    </div>`;
    renderFactoryActions([['factory-cancel-2', '关闭', ''], ['factory-retry', '返回确认页', '']]);
    bindFactoryCancel();
    $('#factory-retry').onclick = () => { factory.step = 3; renderFactoryStep(); };
  }
}

async function saveConfig(updates) {
  try {
    const result = await api('/api/config', { method: 'POST', body: { updates } });
    toast('已保存：' + (result.note || '热加载生效'), 'ok');
    state.config = result;
    renderPills();
    return result;
  } catch (e) { toast(e.message, 'err'); throw e; }
}

/* ---------------- 媒体（P5-d） ---------------- */
function mediaStatusBadge(status) {
  const map = {
    ok: ['✅ 支持', 'green'],
    degraded: ['⚠️ 降级', 'orange'],
    reserved: ['🔒 预留', 'blue'],
    no: ['❌ 不可用', 'red'],
  };
  const [label, cls] = map[status] || ['· 未知', ''];
  return badge(label, cls);
}

function mediaKindLabel(kind) {
  return ({ text: '文本', emoji_text: 'Unicode emoji', image: '图片',
    gif: 'GIF', sticker: '贴纸', sticker_as_image: '贴纸(降级图片)' })[kind] || kind;
}

async function renderMedia() {
  $('#app').innerHTML = '<div class="empty">加载媒体页…</div>';
  const errors = [];
  let caps = null, stats = null, assets = null, wishes = null;
  try { caps = await api('/api/media/capabilities'); } catch (e) { errors.push('能力矩阵: ' + e.message); }
  try { stats = await api('/api/media/stats'); } catch (e) { errors.push('统计: ' + e.message); }
  try { assets = await api('/api/media/library?limit=300'); } catch (e) { errors.push('素材库: ' + e.message); }
  try { wishes = await api('/api/media/wishes'); } catch (e) { errors.push('愿望清单: ' + e.message); }
  const prev = state.media || {};
  state.media = {
    caps, stats, assets, wishes,
    type: prev.type || 'text',
    assetPath: prev.assetPath || '',
    assetFilter: prev.assetFilter || '',
    result: prev.result || null,
    pendingWish: prev.pendingWish || null,
    importResults: prev.importResults || null,
  };
  renderMediaPage(errors);
}

function mediaCapabilityOptions(caps) {
  const rows = (caps && caps.matrix) || [];
  const preferred = ['text', 'emoji_text', 'image', 'gif', 'sticker', 'native_emoji'];
  const seen = new Set();
  const options = [];
  for (const row of rows) {
    let type = row.key;
    if (type === 'sticker_as_image') type = 'sticker';
    if (type === 'sticker_native') type = 'native_emoji';
    if (!preferred.includes(type) || seen.has(type)) continue;
    seen.add(type);
    options.push([type, mediaKindLabel(type)]);
  }
  return options;
}

let mediaPendingFiles = [];

function mediaImportPanelHtml(data) {
  const pendingWish = data.pendingWish;
  return card('本地导入素材（不联网）', `
    <p class="hint">从本机选图导入素材库（<code>agi/data/selflearn/media</code>）：png/jpg/jpeg/gif/webp，≤2MB；
      自动 SHA256 去重、剥离 EXIF，可作为贴纸（图片形式）发送。不做联网搜索/下载。</p>
    ${pendingWish ? `<div class="row" style="margin-bottom:8px"><span class="pill info"><span class="dot"></span>正在满足愿望：${esc(pendingWish.wish || '')}</span>
      <button class="btn small" id="media-import-cancel-wish">取消</button></div>` : ''}
    <div class="row" style="align-items:flex-end">
      <div class="field"><label>选择图片（可多选）</label>
        <input type="file" id="media-files" multiple accept=".png,.jpg,.jpeg,.gif,.webp"></div>
      <div class="field"><label>名称（可选）</label>
        <input type="text" id="media-import-name" placeholder="名称" value="${esc(pendingWish ? '' : '')}" style="max-width:180px"></div>
      <div class="field"><label>标签（逗号分隔）</label>
        <input type="text" id="media-import-tags" placeholder="标签" value="${esc(pendingWish ? (pendingWish.wish || '') : '')}" style="max-width:240px"></div>
      <button class="btn primary" id="media-import-submit">导入选中文件</button>
    </div>
    <div id="media-import-preview" class="asset-grid" style="margin-top:8px"></div>
    <div class="row" style="margin-top:8px">
      <input type="text" id="media-import-path" placeholder="或从本地路径导入（Desktop / 素材库 / 仓库 media 白名单）" style="flex:1">
      <button class="btn" id="media-import-path-btn">路径导入</button>
    </div>
    <div id="media-import-result" style="margin-top:8px">${renderMediaImportResults(data.importResults)}</div>`);
}

function renderMediaPage(errors) {
  const data = state.media || {};
  const caps = data.caps || {};
  const stats = data.stats || {};
  const assets = data.assets || {};
  const wishes = data.wishes || {};
  const items = (assets.items || []).filter((item) => !data.assetFilter || item.kind === data.assetFilter);
  const options = mediaCapabilityOptions(caps);
  if (!options.length) options.push(['text', '文本']);
  if (!options.some(([type]) => type === data.type)) data.type = options[0][0];
  const isText = ['text', 'emoji_text', 'native_emoji'].includes(data.type);
  const assetItems = assets.items || [];
  const selectedAsset = assetItems.find((item) => item.path === data.assetPath) || null;
  const top = stats.top || [];
  const selectedCap = ((caps.matrix) || []).find((row) => row.key === data.type) || {};

  const statCards = `<div class="grid cols-4">
    ${card('素材总数', `<div class="big">${num(stats.total)}</div><div class="muted">来源：${esc(stats.source || '—')}</div>`)}
    ${card('已使用', `<div class="big">${num(stats.used)}</div><div class="muted">emoji_usage 计数</div>`)}
    ${card('未使用', `<div class="big">${num(stats.unused)}</div><div class="muted">可以先用它们测试发送</div>`)}
    ${card('最常用 top5', top.length ? `<div class="list" style="gap:4px">${top.slice(0, 5).map((row) =>
      `<div class="row between"><span class="mono">${esc(row.name || row.id)}</span><span class="hint">${esc(row.usage_count || 0)} 次</span></div>`).join('')}</div>`
      : '<div class="muted">还没有使用记录</div>')}
  </div>`;

  const capTable = `<table><thead><tr><th>能力</th><th>状态</th><th>说明</th></tr></thead><tbody>
    ${((caps.matrix) || []).map((row) => `<tr><td><b>${esc(row.label)}</b><br><span class="mono hint">${esc(row.key)}</span></td>
      <td>${mediaStatusBadge(row.status)}</td><td class="hint">${esc(row.note || '')}</td></tr>`).join('') ||
      '<tr><td colspan="3" class="empty">能力矩阵不可用</td></tr>'}</tbody></table>
    <p class="hint" style="margin-top:8px">来源：${esc(caps.source || 'static')}；media_enabled=${esc(String(caps.media_enabled))}；sticker_native=${esc(String(caps.sticker_native_enabled))}。${esc(caps.note || '')}</p>`;

  const sendPanel = `<div class="grid cols-2">
    <div class="field"><label>测试类型</label><select id="media-type">
      ${options.map(([type, label]) => `<option value="${esc(type)}" ${type === data.type ? 'selected' : ''}>${esc(label)}</option>`).join('')}
    </select></div>
    <div class="field"><label>联系人（强制）</label><input type="text" value="${esc(caps.test_contact || '文件传输助手')}" disabled></div>
    ${isText ? `<div class="field" style="grid-column: 1 / -1"><label>文本内容</label>
      <input type="text" id="media-text" placeholder="${data.type === 'native_emoji' ? '输入系统表情名称（如「微笑」）' : '输入要测试的文本（可含 emoji）'}"></div>`
      : `<div class="field" style="grid-column: 1 / -1"><label>选择素材</label><select id="media-asset">
        <option value="">— 选一个素材 —</option>
        ${assetItems.map((item) => `<option value="${esc(item.path || '')}" ${item.path === data.assetPath ? 'selected' : ''}>${esc(item.name || item.filename || item.id)}${item.kind ? ' · ' + esc(item.kind) : ''}</option>`).join('')}
      </select><div class="hint">${selectedAsset ? '已选：' + esc(selectedAsset.path) : '也可以直接选下方素材卡'}</div></div>`}
    <div class="row" style="grid-column: 1 / -1">
      <button class="btn primary" id="btn-media-send">发送测试</button>
      <span class="hint">只允许发「${esc(caps.test_contact || '文件传输助手')}」；body 里传别的联系人会被强制覆盖。${selectedCap.status === 'no' ? '当前类型不可用。' : ''}</span>
    </div>
    <div style="grid-column: 1 / -1" id="media-send-result">${renderMediaResult(data.result)}</div>
  </div>`;

  const assetGrid = items.length ? `<div class="asset-grid">${items.map((item) => `
    <div class="asset-card ${item.path === data.assetPath ? 'selected' : ''}" data-asset-path="${esc(item.path || '')}" data-asset-kind="${esc(item.kind || '')}">
      ${item.thumb ? `<img src="${esc(item.thumb)}" alt="${esc(item.name || '')}" loading="lazy">` : `<div class="asset-missing">无预览</div>`}
      <div class="asset-name">${esc(item.name || item.filename || item.id)}</div>
      <div class="asset-meta"><span class="badge">${esc(item.kind || '')}</span>
        ${item.usage_count ? `<span class="badge green">${esc(item.usage_count)} 次</span>` : ''}
        ${item.source === 'scan' ? '<span class="badge blue">扫描</span>' : ''}</div>
      ${(item.tags || []).length ? `<div class="asset-tags">${item.tags.slice(0, 3).map((tag) => `<span class="badge">${esc(tag)}</span>`).join('')}</div>` : ''}
    </div>`).join('')}</div>` : '<div class="empty">素材库为空</div>';

  const wishesHtml = (wishes.items || []).length ? `<div class="list">${wishes.items.map((item) => {
    const fulfilled = item.status === 'fulfilled';
    return `<div class="list-item" style="cursor:default"><div class="grow">
      <div class="title">「${esc(item.wish || '')}」</div>
      <div class="hint">${esc(item.source || item.context || 'model')} · ${item.created_at ? fmtDateTime(item.created_at) : ''}
        ${fulfilled && item.fulfilled_asset ? ' · 已满足：' + esc(item.fulfilled_asset) : ''}</div>
    </div>
    ${fulfilled ? '<span class="badge green">已满足</span>'
      : `<button class="btn small" data-wish-fulfill="${esc(item.wish_id || '')}">导入一张满足它</button><span class="badge orange">待获取</span>`}
    </div>`;
  }).join('')}</div>`
    : `<div class="empty">还没有愿望（${esc(wishes.path || 'agi/data/media_wishes.jsonl')} 暂时为空）</div>`;

  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${statCards}
    ${card('能力矩阵', capTable)}
    ${card('测试发送（只发文件传输助手）', sendPanel)}
    ${mediaImportPanelHtml(data)}
    ${card('素材库', `
      <div class="row between" style="margin-bottom:10px">
        <div class="row"><select id="media-filter">
          <option value="">全部类型</option>
          <option value="image" ${data.assetFilter === 'image' ? 'selected' : ''}>图片</option>
          <option value="gif" ${data.assetFilter === 'gif' ? 'selected' : ''}>GIF</option>
        </select><span class="hint">${items.length} 个素材 · 来源 ${esc(assets.source || '—')}</span></div>
        <span class="hint">${esc(assets.note || '')}</span>
      </div>${assetGrid}`)}
    ${card('愿望清单（本地导入满足 / 联网获取仍为预留）', `
      <p class="hint">${esc(wishes.note || '')}</p>${wishesHtml}`)}`;

  const typeSelect = $('#media-type');
  if (typeSelect) typeSelect.onchange = () => { data.type = typeSelect.value; renderMediaPage([]); };
  const filter = $('#media-filter');
  if (filter) filter.onchange = () => { data.assetFilter = filter.value; renderMediaPage([]); };
  $$('[data-asset-path]').forEach((node) => {
    node.onclick = () => {
      data.assetPath = node.dataset.assetPath;
      const type = node.dataset.assetKind === 'gif' ? 'gif' : 'image';
      if (data.type !== 'sticker') data.type = type;
      renderMediaPage([]);
    };
  });
  const sendBtn = $('#btn-media-send');
  if (sendBtn) sendBtn.onclick = () => sendMediaTest(false);
  const fileInput = $('#media-files');
  if (fileInput) {
    fileInput.onchange = () => {
      mediaPendingFiles = Array.from(fileInput.files || []);
      const preview = $('#media-import-preview');
      if (!preview) return;
      preview.innerHTML = mediaPendingFiles.map((file) => `<div class="asset-card">
        <img src="${URL.createObjectURL(file)}" alt="${esc(file.name)}">
        <div class="asset-name">${esc(file.name)}</div>
        <div class="asset-meta"><span class="badge">${esc((file.size / 1024).toFixed(1))} KB</span></div>
      </div>`).join('');
    };
  }
  const submit = $('#media-import-submit');
  if (submit) submit.onclick = submitMediaImport;
  const pathBtn = $('#media-import-path-btn');
  if (pathBtn) pathBtn.onclick = importMediaFromPath;
  const cancelWish = $('#media-import-cancel-wish');
  if (cancelWish) cancelWish.onclick = () => { data.pendingWish = null; renderMediaPage([]); };
  $$('[data-wish-fulfill]').forEach((button) => {
    button.onclick = () => {
      const wish = ((data.wishes || {}).items || []).find((item) => item.wish_id === button.dataset.wishFulfill);
      if (!wish) return;
      data.pendingWish = { wish_id: wish.wish_id, wish: wish.wish };
      renderMediaPage([]);
      toast('选一张图导入即可满足这条愿望', 'ok');
    };
  });
}

function renderMediaImportResults(results) {
  if (!results || !results.length) return '';
  return '<div class="list">' + results.map((item) => `<div class="list-item" style="cursor:default">
    <div class="grow"><div class="title">${esc(item.filename || item.name || '')}</div>
      <div class="hint">${esc(item.path || '')}${item.reason ? ' · ' + esc(item.reason) : ''}</div></div>
    ${item.ok ? (item.duplicate ? '<span class="badge blue">重复(已存在)</span>' : '<span class="badge green">已导入</span>')
      : '<span class="badge red">被拒</span>'}
  </div>`).join('') + '</div>';
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

async function submitMediaImport() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  if (!mediaPendingFiles.length) { toast('先选择图片', 'err'); return; }
  const data = state.media || {};
  const name = ($('#media-import-name') || {}).value || '';
  const tagsText = ($('#media-import-tags') || {}).value || '';
  const tags = tagsText.split(',').map((item) => item.trim()).filter(Boolean);
  try {
    const files = [];
    for (const file of mediaPendingFiles) {
      files.push({ filename: file.name, data: await readFileAsDataURL(file), name, tags });
    }
    const result = await api('/api/media/import', { method: 'POST', body: { files } });
    data.importResults = result.results || [];
    toast(`导入完成：成功 ${result.imported || 0}，重复 ${result.duplicates || 0}，失败 ${result.failed || 0}`,
      result.ok ? 'ok' : 'err');
    await maybeFulfillWish(data.importResults.find((item) => item.ok && item.path));
    await renderMedia();
  } catch (err) {
    toast(err.message, 'err');
    data.importResults = [{ ok: false, filename: 'request', reason: err.message }];
    renderMediaPage([]);
  }
}

async function importMediaFromPath() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const data = state.media || {};
  const pathInput = $('#media-import-path');
  const path = pathInput ? pathInput.value.trim() : '';
  if (!path) { toast('先填本地路径', 'err'); return; }
  const name = ($('#media-import-name') || {}).value || '';
  const tagsText = ($('#media-import-tags') || {}).value || '';
  const tags = tagsText.split(',').map((item) => item.trim()).filter(Boolean);
  try {
    const result = await api('/api/media/import-path', { method: 'POST', body: { path, name, tags } });
    data.importResults = [result];
    toast(result.ok ? (result.duplicate ? '文件已存在（去重）' : '已导入素材库') : ('被拒：' + (result.reason || result.error)),
      result.ok ? 'ok' : 'err');
    await maybeFulfillWish(result.ok ? result : null);
    await renderMedia();
  } catch (err) { toast(err.message, 'err'); }
}

async function maybeFulfillWish(asset) {
  const data = state.media || {};
  if (!data.pendingWish || !asset || !asset.path) return;
  try {
    await api('/api/media/wish/fulfill', {
      method: 'POST',
      body: { wish_id: data.pendingWish.wish_id, path: asset.path },
    });
    toast('愿望已满足：' + (data.pendingWish.wish || ''), 'ok');
    data.pendingWish = null;
  } catch (err) { toast('满足愿望失败：' + err.message, 'err'); }
}

function renderMediaResult(result) {
  if (!result) return '<span class="hint">还没有发送记录。</span>';
  const status = result.status || (result.ok ? 'sent' : 'error');
  const cls = result.ok ? 'ok' : (status === 'unsupported' ? 'warn' : 'err');
  const detail = result.error || result.hint || result.note || '';
  return `<div class="row"><span class="pill ${cls}">${esc(status)}</span>
    <span class="mono hint">${esc(JSON.stringify(result.gateway || {}).slice(0, 200))}</span></div>
    ${detail ? `<div class="hint">${esc(detail)}</div>` : ''}`;
}

async function sendMediaTest(silent) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const data = state.media || {};
  const type = data.type || 'text';
  const body = { type };
  if (type === 'text' || type === 'emoji_text' || type === 'native_emoji') {
    const input = $('#media-text');
    const value = input ? input.value : '';
    if (!String(value).trim()) { toast('先输入测试内容', 'err'); return; }
    if (type === 'native_emoji') body.name = value;
    else body.text = value;
  } else {
    const select = $('#media-asset');
    body.path = data.assetPath || (select ? select.value : '');
    if (!body.path) { toast('先选一个素材', 'err'); return; }
  }
  try {
    const result = await api('/api/media/send', { method: 'POST', body });
    data.result = result;
    toast(result.ok ? '测试消息已发送（文件传输助手）' : ('未发送：' + (result.error || result.status)), result.ok ? 'ok' : 'err');
    renderMediaPage([]);
  } catch (e) {
    data.result = { ok: false, status: 'error', error: e.message };
    toast(e.message, 'err');
    renderMediaPage([]);
  }
}

/* ---------------- 对话测试（P7-b ChatUI） ---------------- */
function chatDetailHtml(detail) {
  if (!detail) return '';
  const inner = detail.inner || {};
  const media = detail.media || {};
  const summary = [];
  if (detail.latency_ms !== undefined && detail.latency_ms !== null) summary.push(detail.latency_ms + 'ms');
  if (inner && (inner.stance || inner.intent)) summary.push('内状态');
  if (media && Object.keys(media).length) summary.push('媒体');
  if (detail.skipped) summary.push('本条未回复');
  if (!summary.length) return '';
  return `<details class="chat-detail"><summary>${esc(summary.join(' · '))}</summary>
    <pre class="json">${esc(JSON.stringify({
      inner: detail.inner || null, media: detail.media || null,
      guard: detail.guard || null, latency_ms: detail.latency_ms,
      skipped: detail.skipped || false, skip_reason: detail.skip_reason || '',
    }, null, 2))}</pre></details>`;
}

function chatBubblesHtml() {
  const messages = state.chatMsgs || [];
  if (!messages.length) {
    return '<div class="empty">还没有消息。发一句试试提示词/模型的效果（不会影响真实联系人）。</div>';
  }
  return '<div class="chat-log" id="chat-log">' + messages.map((message) => {
    const mine = message.role === 'user';
    const error = message.error ? ' error' : '';
    return `<div class="chat-msg ${mine ? 'user' : 'ai'}">
      <div class="chat-bubble${error}">
        <div class="chat-meta">${mine ? '你' : 'TA'}${message.ts ? ' · ' + fmtTime(message.ts) : ''}</div>
        <div class="chat-text">${esc(message.text || '')}</div>
        ${chatDetailHtml(message.detail)}
      </div>
    </div>`;
  }).join('') + '</div>';
}

async function renderChatTest() {
  if (!state.chatMsgs) {
    state.chatMsgs = [];
    try {
      const history = await api('/api/chat-test/history?limit=50');
      state.chatMsgs = (history.items || []).map((item) => ({
        role: item.role === 'user' ? 'user' : 'assistant',
        text: item.text, ts: item.ts,
      }));
    } catch (err) { /* 历史读不到不阻塞 */ }
  }
  renderChatPage();
}

function renderChatPage() {
  const busy = !!state.chatBusy;
  $('#app').innerHTML = `
    <div class="card chat-note">
      <div class="row between">
        <span>🧪 <b>隔离测试库</b>：<code>agi/data/selflearn/console_test.sqlite3</code>，不影响真实联系人记忆；不会发微信消息。</span>
        <div class="row">
          <button class="btn" id="chat-clear">清空会话</button>
          <button class="btn primary" id="chat-send" ${busy ? 'disabled' : ''}>${busy ? 'TA 正在想…' : '发送'}</button>
        </div>
      </div>
    </div>
    ${card('对话测试', `
      ${chatBubblesHtml()}
      <div class="chat-input-row">
        <input type="text" id="chat-input" placeholder="输入消息，回车发送（使用当前提示词/模型配置）" ${busy ? 'disabled' : ''}>
        <span class="hint">${busy ? '生成中，最多等 60s…' : 'Enter 发送'}</span>
      </div>`)}`;
  const input = $('#chat-input');
  if (input) {
    input.focus();
    input.onkeydown = (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendChatMessage(); }
    };
  }
  const send = $('#chat-send');
  if (send) send.onclick = sendChatMessage;
  const clear = $('#chat-clear');
  if (clear) clear.onclick = clearChatTest;
  const log = $('#chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

async function sendChatMessage() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  if (state.chatBusy) return;
  const input = $('#chat-input');
  const text = input ? input.value.trim() : '';
  if (!text) return;
  state.chatMsgs = state.chatMsgs || [];
  state.chatMsgs.push({ role: 'user', text, ts: Date.now() / 1000 });
  state.chatBusy = true;
  renderChatPage();
  try {
    const result = await api('/api/chat-test', { method: 'POST', body: { text } });
    const replyText = result.reply || (result.skipped ? '（TA 这条选择先不回）' : '');
    state.chatMsgs.push({
      role: 'assistant', text: replyText, ts: Date.now() / 1000,
      detail: {
        inner: result.inner, media: result.media, guard: result.guard,
        latency_ms: result.latency_ms, skipped: result.skipped,
        skip_reason: result.skip_reason,
      },
    });
  } catch (err) {
    state.chatMsgs.push({
      role: 'assistant', text: '发送失败：' + err.message,
      ts: Date.now() / 1000, error: true,
    });
    toast(err.message, 'err');
  } finally {
    state.chatBusy = false;
    renderChatPage();
  }
}

async function clearChatTest() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  if (!confirm('清空隔离测试会话（删除 console_test.sqlite3）？不影响真实联系人。')) return;
  try {
    await api('/api/chat-test', { method: 'POST', body: { reset: true } });
    state.chatMsgs = [];
    toast('测试会话已清空', 'ok');
  } catch (err) { toast(err.message, 'err'); }
  renderChatPage();
}

/* ---------------- 模型配置（P7-b Providers） ---------------- */
const MODEL_FIELDS = [
  ['llm_model', 'LLM 模型名', 'text'],
  ['llm_temperature', '温度 temperature（0–2）', 'number'],
  ['llm_max_tokens', '最大 tokens（1–32000）', 'number'],
  ['soul_server_url', '内状态服务地址 soul_server_url', 'text'],
  ['soul_timeout_s', '内状态超时 soul_timeout_s（秒）', 'number'],
];

async function renderProviders() {
  $('#app').innerHTML = '<div class="empty">加载模型配置…</div>';
  let data = null;
  try { data = await api('/api/model-config'); }
  catch (err) {
    $('#app').innerHTML = card('模型配置', `<div class="error-text">${esc(err.message)}</div>`);
    return;
  }
  state.modelConfig = data;
  const cfg = data.config || {};
  const deepseek = data.deepseek || {};
  const effective = data.effective || {};
  const keyPill = deepseek.configured ? pill('DeepSeek key 已配置', 'ok') : pill('DeepSeek key 未配置', 'err');
  $('#app').innerHTML = `
    <div class="card">
      <div class="row between">
        <div class="row">${keyPill}<span class="hint">来源：${esc(deepseek.source || '未配置')}</span></div>
        <span class="hint">当前生效模型：<b>${esc(effective.llm_model || '—')}</b> · base_url ${esc(effective.llm_base_url || '—')}</span>
      </div>
      <p class="hint" style="margin-top:8px">DeepSeek key 不在这里编辑也不回传明文：在 <code>web_config.json</code> 的 <code>llm.api_key</code>，或环境变量 <code>DEEPSEEK_API_KEY</code> 配置。</p>
    </div>
    ${card('网关模型/内状态配置（保存后热加载生效）', `
      <div class="grid cols-2 provider-form">
        ${MODEL_FIELDS.map(([key, label, type]) => `<div class="field">
          <label>${esc(label)}</label>
          <input type="${type}" id="mc-${esc(key)}" value="${esc(cfg[key] ?? '')}"
                 ${type === 'number' ? 'step="any"' : ''}>
          <span class="hint mono">${esc(key)}</span>
        </div>`).join('')}
        <div class="field"><label>inner_schema</label>
          <select id="mc-inner_schema">
            <option value="slim" ${cfg.inner_schema === 'slim' ? 'selected' : ''}>slim（4 字段省 token）</option>
            <option value="full" ${cfg.inner_schema === 'full' ? 'selected' : ''}>full（对照）</option>
          </select><span class="hint mono">inner_schema</span></div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="mc-save">保存配置</button>
        <span class="hint">网关热加载：保存后下一条消息 / 下一次 proactive tick 生效，无需重启。</span>
      </div>
      <div id="mc-result" style="margin-top:8px"></div>`)}
    ${card('配置位置', `<div class="kv">
      <span class="k">网关配置</span><span class="v mono">${esc(data.path || '')}</span>
      <span class="k">默认值</span><span class="v mono">${esc(JSON.stringify(data.defaults || {}))}</span>
    </div>`)}`;
  const save = $('#mc-save');
  if (save) save.onclick = saveModelConfig;
}

async function saveModelConfig() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const updates = {};
  for (const [key, , type] of MODEL_FIELDS) {
    const node = $('#mc-' + key);
    if (!node) continue;
    updates[key] = type === 'number' ? Number(node.value) : node.value;
  }
  updates.inner_schema = $('#mc-inner_schema').value;
  try {
    const result = await api('/api/model-config', { method: 'POST', body: { updates } });
    toast('已保存：' + (result.note || '热加载生效'), 'ok');
    $('#mc-result').innerHTML = '<span class="pill ok"><span class="dot"></span>已保存，下一条消息生效</span>';
    state.modelConfig = result;
  } catch (err) {
    $('#mc-result').innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
    toast(err.message, 'err');
  }
}

/* ---------------- 日志（P7-b Trace） ---------------- */
const LOG_SOURCE_OPTIONS = [
  ['gateway', '网关事件 gateway.log'], ['inner', '内状态 inner.log'],
  ['prompt', '提示词 prompt_override.log'], ['regen', '重生成失败 regen_failed'],
  ['audit', '审计日志 console_audit.log'],
];

async function renderLogs() {
  const prev = state.logsState || {};
  state.logsState = { source: prev.source || 'gateway', q: prev.q || '', kind: prev.kind || '' };
  await loadLogs();
}

async function loadLogs() {
  const params = state.logsState || { source: 'gateway', q: '', kind: '' };
  $('#app').innerHTML = '<div class="empty">加载日志…</div>';
  let data = null;
  try {
    data = await api('/api/logs?source=' + encodeURIComponent(params.source)
      + '&limit=200&q=' + encodeURIComponent(params.q)
      + '&kind=' + encodeURIComponent(params.kind));
  } catch (err) {
    $('#app').innerHTML = card('日志', `<div class="error-text">${esc(err.message)}</div>`);
    return;
  }
  const items = data.items || [];
  const rows = items.length ? items.map((entry) => {
    const ts = entry.ts ? fmtDateTime(entry.ts) : '—';
    const summary = entry.text || entry.raw || entry.reason || entry.key
      || (entry.kind ? '' : '');
    return `<div class="log-row ${entry.error ? 'err' : ''}" data-json='${esc(JSON.stringify(entry))}'>
      <span class="time">${esc(ts)}</span>
      <span class="badge ${entry.error ? 'red' : ''}">${esc(entry.kind || 'log')}</span>
      <span class="log-text">${esc(String(summary).slice(0, 240))}</span>
      <span class="hint mono">${esc(entry._file || '')}:${esc(entry._line || '')}</span>
    </div>`;
  }).join('') : '<div class="empty">没有匹配的日志</div>';
  $('#app').innerHTML = `
    ${card('日志检索', `
      <div class="row">
        <select id="log-source">${LOG_SOURCE_OPTIONS.map(([value, label]) =>
          `<option value="${value}" ${value === params.source ? 'selected' : ''}>${label}</option>`).join('')}</select>
        <input type="text" id="log-q" placeholder="关键词（JSON 内容搜索）" value="${esc(params.q)}" style="max-width:280px">
        <input type="text" id="log-kind" placeholder="kind 过滤" value="${esc(params.kind)}" style="max-width:160px">
        <button class="btn primary" id="log-search">搜索</button>
        <span class="hint">${items.length} 条 · 按时间倒序 · 点一行看 JSON</span>
      </div>
      <div class="hint" style="margin-top:6px">文件：${esc((data.files || []).join(' · ') || data.note || '—')}</div>
      <div class="log-list" style="margin-top:10px">${rows}</div>`)}`;
  const search = $('#log-search');
  if (search) search.onclick = () => {
    state.logsState.source = $('#log-source').value;
    state.logsState.q = $('#log-q').value.trim();
    state.logsState.kind = $('#log-kind').value.trim();
    loadLogs();
  };
  const q = $('#log-q');
  if (q) q.onkeydown = (event) => { if (event.key === 'Enter') search.click(); };
  const source = $('#log-source');
  if (source) source.onchange = () => {
    state.logsState = { source: source.value, q: '', kind: '' };
    loadLogs();
  };
}

/* ---------------- 数据管理（P7-c DataPage） ---------------- */
function fmtBytes(value) {
  const number = Number(value || 0);
  if (number < 1024) return number + ' B';
  if (number < 1024 * 1024) return (number / 1024).toFixed(1) + ' KB';
  if (number < 1024 * 1024 * 1024) return (number / 1024 / 1024).toFixed(1) + ' MB';
  return (number / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

async function downloadWithToken(path, fallbackName) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  try {
    const response = await fetch(path, { headers: { 'X-Admin-Token': state.token } });
    if (!response.ok) {
      let message = 'HTTP ' + response.status;
      try { const payload = await response.json(); message = payload.error || message; } catch (err) { /* ignore */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const name = match ? match[1] : (fallbackName || 'download.bin');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = name;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('已开始下载：' + name, 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function renderData() {
  $('#app').innerHTML = '<div class="empty">加载数据信息…</div>';
  if (!state.token) {
    $('#app').innerHTML = card('数据管理', '<div class="error-text">数据管理需要 admin token：请在右上角填写。</div>');
    return;
  }
  let info = null;
  try { info = await api('/api/data/info'); }
  catch (err) {
    $('#app').innerHTML = card('数据管理', `<div class="error-text">${esc(err.message)}</div>`);
    return;
  }
  state.dataInfo = info;
  const backups = info.backups || [];
  const aliases = (info.aliases || []);
  const cards = `<div class="grid cols-4">
    ${card('运行数据体积', `<div class="big">${esc(fmtBytes(info.selflearn_bytes))}</div><div class="muted">agi/data/selflearn/</div>`)}
    ${card('联系人库', `<div class="big">${num(info.contacts)}</div><div class="muted">${num(info.messages)} 条消息 · ${num(info.memories)} 条记忆 · ${num(info.claims)} 条 claim</div>`)}
    ${card('网关日志', `<div class="big">${num(info.gateway_log_lines)}</div><div class="muted">行 · ${esc(fmtBytes(info.gateway_log_bytes))}</div>`)}
    ${card('媒体素材', `<div class="big">${num(info.media_files)}</div><div class="muted">本地图片/GIF 文件</div>`)}
  </div>`;

  const backupRows = backups.length ? `<table><thead><tr><th>名称</th><th>大小</th><th>时间</th><th>操作</th></tr></thead><tbody>
    ${backups.slice(0, 20).map((item) => `<tr>
      <td class="mono">${esc(item.name)}</td><td class="num">${esc(fmtBytes(item.bytes))}</td>
      <td>${fmtDateTime(item.mtime)}</td>
      <td class="row"><button class="btn small" data-backup-download="${esc(item.name)}">下载</button>
        <button class="btn small danger" data-backup-delete="${esc(item.name)}">删除</button></td>
    </tr>`).join('')}</tbody></table>` : '<div class="empty">备份目录里还没有 zip</div>';

  const aliasOptions = aliases.length
    ? aliases.map((row) => `<option value="${esc(row.alias)}" ${row.exists ? '' : 'disabled'}>${esc(row.alias)}${row.exists ? '' : '（无库）'}</option>`).join('')
    : '';

  const splitRows = (info.contacts_detail || []).map((row) => {
    const memory = row.memory || {};
    const persona = row.persona || {};
    return `<tr>
      <td><b>${esc(row.alias || row.stem)}</b><br><span class="mono hint">${esc(row.stem)}</span>
        ${row.migrated ? badge('已拆分', 'green') : badge(row.single ? '旧版单库' : '待迁移', 'orange')}</td>
      <td>${memory.name ? `<span class="mono hint">${esc(memory.name)}</span><br>${esc(fmtBytes(memory.bytes))} · ${num(memory.messages)} 消息 / ${num(memory.memories)} 记忆 / ${num(memory.claims)} claim` : '<span class="hint">—</span>'}</td>
      <td>${persona.name ? `<span class="mono hint">${esc(persona.name)}</span><br>${esc(fmtBytes(persona.bytes))} · ${num(persona.rows)} 行（人格/状态）` : '<span class="hint">—</span>'}</td>
      <td>${row.legacy ? `<span class="badge orange">legacy</span><br><span class="mono hint">${esc(row.legacy.name)}</span><br>${esc(fmtBytes(row.legacy.bytes))}<br><span class="hint">可删除/可回滚</span>` : '<span class="hint">无</span>'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">还没有联系人库</td></tr>';
  const splitCard = card('库文件（记忆库 / 人格库）', `
    <p class="hint">记忆库=<code>*.memory.sqlite3</code>（对话/记忆/claims），人格库=<code>*.persona.sqlite3</code>（人格/状态）；
      未迁移旧库显示为单库，迁移后旧库改名 <code>*.legacy.sqlite3</code>（可删除/可回滚，不计入统计）。</p>
    <div style="overflow:auto"><table><thead><tr><th>联系人</th><th>记忆库（对话/记忆）</th><th>人格库（人格/状态）</th><th>旧库 legacy</th></tr></thead>
      <tbody>${splitRows}</tbody></table></div>`);
  $('#app').innerHTML = `
    ${cards}
    ${splitCard}
    ${card('备份', `
      <div class="row between">
        <div class="row"><button class="btn primary" id="data-backup-btn">立即备份运行数据</button>
          <span class="hint">打包到 <code>${esc(info.backup_dir)}</code> 的 manual_&lt;时间&gt;.zip；不改动任何数据</span></div>
        <button class="btn small" id="data-audit-link">查看审计日志</button>
      </div>
      <div style="margin-top:10px">${backupRows}</div>`)}
    ${card('导出', `
      <div class="row" style="align-items:flex-end">
        <div class="field"><label>联系人</label><select id="data-export-contact">
          <option value="">全部联系人</option>${aliasOptions}</select></div>
        <div class="field"><label>格式</label><select id="data-export-format">
          <option value="jsonl">JSONL</option><option value="md">Markdown</option></select></div>
        <div class="field"><label>每个库条数</label><input type="number" id="data-export-limit" value="1000" min="1" max="5000" style="width:110px"></div>
        <button class="btn" id="data-export-btn">导出聊天记录</button>
        <button class="btn" id="data-export-mem-btn">导出记忆 JSON</button>
      </div>
      <p class="hint" style="margin-top:8px">直接以下载流返回，不落盘到仓库；消息含时间/角色/文本/proactive 标记。</p>`)}
    ${card('导入备份（危险）', `
      <div class="row" style="align-items:flex-end">
        <div class="field"><label>备份 zip</label><select id="data-import-zip">
          <option value="">— 选择备份 —</option>${backups.map((item) => `<option value="${esc(item.name)}">${esc(item.name)}</option>`).join('')}
        </select></div>
        <div class="field"><label>模式</label><select id="data-import-mode">
          <option value="merge">merge（同名跳过，不覆盖）</option>
          <option value="overwrite">overwrite（覆盖现有库）</option></select></div>
        <div class="field"><label>确认短语</label><input type="text" id="data-import-confirm" placeholder="导入备份" style="width:170px"></div>
        <div class="field"><label>覆盖再确认</label><input type="text" id="data-import-overwrite" placeholder="仅 overwrite 填：覆盖现有库" style="width:220px"></div>
        <button class="btn factory" id="data-import-btn">导入</button>
      </div>
      <p class="hint" style="margin-top:8px">只允许 <code>E:\\agent\\backups\\</code> 下的 zip；解压到临时目录校验后把联系人库复制进 <code>agi/data/selflearn/wechat_contacts/</code>。
        merge 不覆盖；overwrite 需额外短语。</p>
      <div id="data-import-result" style="margin-top:8px"></div>`)}`;

  $('#data-backup-btn').onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    const button = $('#data-backup-btn');
    button.disabled = true; button.textContent = '备份中…';
    try {
      const result = await api('/api/data/backup', { method: 'POST', body: {} });
      toast(`已备份 ${result.files} 个文件 → ${result.backup_path}`, 'ok');
      renderData();
    } catch (err) { toast(err.message, 'err'); button.disabled = false; button.textContent = '立即备份运行数据'; }
  };
  $('#data-audit-link').onclick = () => {
    state.logsState = { source: 'audit', q: '', kind: '' };
    go('logs');
  };
  $$('[data-backup-download]').forEach((button) => {
    button.onclick = () => downloadWithToken('/api/data/backup/download?name=' +
      encodeURIComponent(button.dataset.backupDownload), button.dataset.backupDownload);
  });
  $$('[data-backup-delete]').forEach((button) => {
    button.onclick = async () => {
      const confirmText = window.prompt('输入「删除备份」确认删除 ' + button.dataset.backupDelete + '：');
      if (confirmText === null) return;
      try {
        await api('/api/data/backup/delete', {
          method: 'POST', body: { name: button.dataset.backupDelete, confirm: confirmText },
        });
        toast('备份已删除', 'ok'); renderData();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  $('#data-export-btn').onclick = () => {
    const contact = $('#data-export-contact').value;
    const format = $('#data-export-format').value;
    const limit = $('#data-export-limit').value || 1000;
    downloadWithToken(`/api/data/export?contact=${encodeURIComponent(contact)}&format=${format}&limit=${limit}`,
      'export.' + format);
  };
  $('#data-export-mem-btn').onclick = () => {
    const contact = $('#data-export-contact').value;
    downloadWithToken('/api/data/export-memories?contact=' + encodeURIComponent(contact),
      'memories.json');
  };
  $('#data-import-btn').onclick = doDataImport;
}

async function doDataImport() {
  const zipName = $('#data-import-zip').value;
  if (!zipName) { toast('先选一个备份 zip', 'err'); return; }
  const mode = $('#data-import-mode').value;
  const confirmText = $('#data-import-confirm').value.trim();
  if (confirmText !== '导入备份') { toast('确认短语必须是「导入备份」', 'err'); return; }
  if (mode === 'overwrite' && !window.confirm('overwrite 会覆盖现有联系人库，确定继续？')) return;
  const button = $('#data-import-btn');
  button.disabled = true;
  try {
    const result = await api('/api/data/import', {
      method: 'POST', body: {
        zip_path: zipName, confirm: confirmText, mode,
        overwrite_confirm: ($('#data-import-overwrite').value || '').trim(),
      },
    });
    $('#data-import-result').innerHTML = `<div class="row"><span class="pill ok">导入完成</span>
      <span class="hint">新增 ${(result.imported || []).length} · 跳过 ${(result.skipped || []).length}</span></div>
      <div class="hint mono">imported: ${esc((result.imported || []).join(', ') || '—')}<br>skipped: ${esc((result.skipped || []).join(', ') || '—')}</div>`;
    toast(result.note || '导入完成', 'ok');
    renderData();
  } catch (err) {
    $('#data-import-result').innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
    toast(err.message, 'err');
  } finally { button.disabled = false; }
}

/* ---------------- 记忆编辑（P7-c KnowledgePage 写操作） ---------------- */
const MEMORY_STATUS_OPTIONS = [['active', 'active'], ['dormant', 'dormant'], ['invalid', 'invalid']];

async function renderMemoryEdit() {
  const prev = state.memoryEdit || {};
  state.memoryEdit = {
    q: prev.q || '', status: prev.status || '', offset: prev.offset || 0,
    limit: prev.limit || 100, selected: prev.selected || [],
  };
  await loadMemoryEdit();
}

async function loadMemoryEdit() {
  const edit = state.memoryEdit;
  $('#app').innerHTML = '<div class="empty">加载记忆…</div>';
  if (!state.token) {
    $('#app').innerHTML = card('记忆编辑', '<div class="error-text">记忆编辑需要 admin token：请在右上角填写。</div>');
    return;
  }
  const params = `contact=${encodeURIComponent(edit.contact || '')}&q=${encodeURIComponent(edit.q)}`
    + `&status=${encodeURIComponent(edit.status)}&limit=${edit.limit}&offset=${edit.offset}`;
  let data = null, claims = null, metrics = null;
  const errors = [];
  try { data = await api('/api/memory/list?' + params); } catch (err) { errors.push('记忆: ' + err.message); }
  try { claims = await api('/api/memory/claims?limit=100'); } catch (err) { errors.push('claims: ' + err.message); }
  try { metrics = await api('/api/memory'); } catch (err) { metrics = null; }
  const items = (data && data.items) || [];
  const total = (data && data.total) || 0;
  const memMetrics = (metrics && metrics.metrics && metrics.metrics.memories) || {};
  const summary = `<div class="grid cols-4">
    ${card('记忆总数', `<div class="big">${num(memMetrics.total)}</div><div class="muted">active ${num((memMetrics.by_status || {}).active)} · dormant ${num((memMetrics.by_status || {}).dormant)}</div>`)}
    ${card('休眠率', `<div class="big">${pct(memMetrics.dormant_rate)}</div><div class="muted">阈值 >40% 告警</div>`)}
    ${card('检索命中', `<div class="big">${pct((metrics && metrics.metrics && metrics.metrics.retrieval || {}).hit_rate)}</div><div class="muted">调用 ${num(((metrics || {}).metrics || {}).retrieval ? metrics.metrics.retrieval.calls : null)}</div>`)}
    ${card('当前筛选', `<div class="big">${num(total)}</div><div class="muted">条匹配</div>`)}
  </div>`;

  const rows = items.length ? items.map((item) => `<tr data-memory-id="${esc(item.id)}">
    <td><input type="checkbox" data-merge-pick="${esc(item.id)}" ${(edit.selected || []).includes(item.id) ? 'checked' : ''}></td>
    <td class="mono hint">${esc(item.id)}</td>
    <td>${badge(item.kind || '')}</td>
    <td><input type="text" class="memory-content" data-memory-content="${esc(item.id)}" value="${esc(item.content || '')}" maxlength="500"></td>
    <td><input type="number" class="memory-importance" data-memory-importance="${esc(item.id)}" value="${esc(item.importance ?? '')}" min="0" max="1" step="0.05" style="width:80px"></td>
    <td><select data-memory-status="${esc(item.id)}">${MEMORY_STATUS_OPTIONS.map(([value, label]) =>
      `<option value="${value}" ${item.status === value ? 'selected' : ''}>${label}</option>`).join('')}</select></td>
    <td class="hint">${fmtDateTime(item.created_at)}</td>
    <td class="row">
      <button class="btn small primary" data-memory-save="${esc(item.id)}">保存</button>
      <button class="btn small" data-memory-mark="${esc(item.id)}">${Number(item.importance || 0) >= 1 ? '取消重要' : '标重要'}</button>
      <button class="btn small danger" data-memory-delete="${esc(item.id)}">删除</button>
    </td>
  </tr>`).join('') : '<tr><td colspan="8" class="empty">没有匹配的记忆</td></tr>';

  const claimRows = ((claims && claims.items) || []).length ? (claims.items || []).map((item) => `<tr>
    <td>${esc(item.subject || '')}</td><td>${esc(item.predicate || '')}</td><td>${esc(item.object || '')}</td>
    <td class="num">${num(item.confidence)}</td><td>${badge(item.status || '')}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">暂无 claims</td></tr>';

  const page = Math.floor(edit.offset / edit.limit) + 1;
  const pages = Math.max(1, Math.ceil(total / edit.limit));
  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${summary}
    ${card('记忆列表', `
      <div class="row between" style="margin-bottom:10px">
        <div class="row">
          <input type="text" id="mem-q" placeholder="搜索内容/kind/evidence" value="${esc(edit.q)}" style="max-width:260px">
          <select id="mem-status"><option value="">全部状态</option>
            ${MEMORY_STATUS_OPTIONS.map(([value, label]) => `<option value="${value}" ${edit.status === value ? 'selected' : ''}>${label}</option>`).join('')}
          </select>
          <button class="btn" id="mem-search">搜索</button>
        </div>
        <div class="row">
          <button class="btn factory small" id="mem-merge" ${(edit.selected || []).length === 2 ? '' : 'disabled'}>合并选中的两条</button>
          <span class="hint">第 ${page}/${pages} 页 · 共 ${total} 条</span>
          <button class="btn small" id="mem-prev" ${edit.offset <= 0 ? 'disabled' : ''}>上一页</button>
          <button class="btn small" id="mem-next" ${edit.offset + edit.limit >= total ? 'disabled' : ''}>下一页</button>
        </div>
      </div>
      <div style="overflow:auto"><table><thead><tr><th></th><th>id</th><th>kind</th><th>content（行内编辑，≤500）</th>
        <th>importance</th><th>status</th><th>创建</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="hint" style="margin-top:8px">删除=软删除（status=invalid，不物理删除）；合并会保留较早的一条、另一条标 invalid。</p>`)}
    ${card('claims（只读）', `<div style="overflow:auto"><table><thead><tr><th>subject</th><th>predicate</th><th>object</th>
      <th>confidence</th><th>status</th></tr></thead><tbody>${claimRows}</tbody></table></div>`)}`;

  $('#mem-search').onclick = () => {
    edit.q = $('#mem-q').value.trim(); edit.status = $('#mem-status').value;
    edit.offset = 0; edit.selected = []; loadMemoryEdit();
  };
  $('#mem-prev').onclick = () => { edit.offset = Math.max(0, edit.offset - edit.limit); edit.selected = []; loadMemoryEdit(); };
  $('#mem-next').onclick = () => { edit.offset += edit.limit; edit.selected = []; loadMemoryEdit(); };
  $$('[data-merge-pick]').forEach((box) => {
    box.onchange = () => {
      const id = box.dataset.mergePick;
      let selected = edit.selected || [];
      if (box.checked) {
        selected = selected.filter((item) => item !== id);
        if (selected.length >= 2) selected.shift();
        selected.push(id);
      } else {
        selected = selected.filter((item) => item !== id);
      }
      edit.selected = selected;
      loadMemoryEdit();
    };
  });
  $$('[data-memory-save]').forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.memorySave;
      const content = $(`[data-memory-content="${CSS.escape(id)}"]`).value;
      const importance = $(`[data-memory-importance="${CSS.escape(id)}"]`).value;
      const status = $(`[data-memory-status="${CSS.escape(id)}"]`).value;
      try {
        await api('/api/memory/update', {
          method: 'POST', body: { id, content, importance: Number(importance), status },
        });
        toast('记忆已更新', 'ok'); loadMemoryEdit();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  $$('[data-memory-mark]').forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.memoryMark;
      const important = button.textContent.indexOf('取消') < 0;
      try {
        const result = await api('/api/memory/mark', { method: 'POST', body: { id, important } });
        toast(important ? '已标记为重要' : '已取消重要', 'ok'); loadMemoryEdit();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  $$('[data-memory-delete]').forEach((button) => {
    button.onclick = async () => {
      const confirmText = window.prompt('软删除这条记忆（status=invalid，不物理删除）。输入「删除记忆」确认：');
      if (confirmText === null) return;
      try {
        await api('/api/memory/delete', {
          method: 'POST', body: { id: button.dataset.memoryDelete, confirm: confirmText },
        });
        toast('已软删除', 'ok'); loadMemoryEdit();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
  const merge = $('#mem-merge');
  if (merge) merge.onclick = async () => {
    const ids = edit.selected || [];
    if (ids.length !== 2) return;
    if (!window.confirm('合并这两条记忆？保留较早的一条，另一条标记 invalid。')) return;
    try {
      const result = await api('/api/memory/merge', { method: 'POST', body: { ids } });
      toast('已合并：保留 ' + result.kept, 'ok');
      edit.selected = []; loadMemoryEdit();
    } catch (err) { toast(err.message, 'err'); }
  };
}

/* ---------------- 统计（P8-a Stats） ---------------- */
function statsBarRows(entries, options) {
  const opts = options || {};
  const rows = (entries || []).filter((entry) => entry && entry.value !== undefined);
  if (!rows.length) return '<div class="empty">暂无数据</div>';
  const max = opts.max || Math.max(1, ...rows.map((entry) => Number(entry.value) || 0));
  return '<div class="stats-bars">' + rows.map((entry) => {
    const value = Number(entry.value) || 0;
    const width = Math.max(2, Math.round(value / max * 100));
    const label = esc(entry.label === undefined ? '' : entry.label);
    const note = entry.note ? `<span class="hint">${esc(entry.note)}</span>` : '';
    return `<div class="bar-row" title="${label}：${value}">
      <span class="bar-label">${label}</span>
      <span class="bar-track"><span class="bar-fill ${esc(entry.cls || '')}" style="width:${width}%"></span></span>
      <span class="bar-value">${value}</span>${note}
    </div>`;
  }).join('') + '</div>';
}

function statsCard(title, value, sub) {
  return card(title, `<div class="big">${esc(value)}</div><div class="muted">${esc(sub || '')}</div>`);
}

async function renderStats() {
  const prev = state.statsState || {};
  state.statsState = { days: prev.days || 7, contact: prev.contact || '' };
  await loadStats();
}

async function loadStats() {
  $('#app').innerHTML = '<div class="empty">加载统计…</div>';
  const settings = state.statsState || { days: 7, contact: '' };
  if (!state.token) {
    $('#app').innerHTML = card('统计', '<div class="error-text">统计页需要 admin token：请在右上角填写。</div>');
    return;
  }
  let data = null, info = null;
  try {
    data = await api('/api/stats?days=' + settings.days
      + '&contact=' + encodeURIComponent(settings.contact));
  } catch (err) {
    $('#app').innerHTML = card('统计', `<div class="error-text">${esc(err.message)}</div>`);
    return;
  }
  try { info = await api('/api/data/info'); } catch (err) { info = null; }
  const aliases = (info && info.aliases) || [];
  const days = data.sent_by_day || [];
  const today = days.length ? days[days.length - 1] : { total: 0, proactive: 0, outbound: 0 };
  const inner = data.inner || {};
  const latency = inner.latency || {};
  const counts = data.counts || {};
  const proactive = data.proactive || {};
  const messages = data.messages || {};
  const media = data.media || {};
  const mediaEntries = Object.entries(media).map(([type, values]) => ({
    label: type, value: (values.sent || 0) + (values.failed || 0),
    note: '成功 ' + (values.sent || 0) + ' / 失败 ' + (values.failed || 0),
  }));

  const blockedEntries = Object.entries(data.blocked || {})
    .map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value).slice(0, 5);
  const discardedEntries = Object.entries(data.discarded || {})
    .map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value).slice(0, 5);
  const regenEntries = Object.entries(data.regen_reasons || {})
    .map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value).slice(0, 5);
  const hourEntries = (data.by_hour || []).map((value, hour) => ({
    label: String(hour).padStart(2, '0') + ':00', value,
  }));
  const dayEntries = days.map((row) => ({
    label: String(row.date).slice(5),
    value: row.total,
    note: '主动 ' + (row.proactive || 0) + ' / 回复 ' + (row.outbound || 0),
  }));
  const stanceEntries = Object.entries(inner.by_stance || {})
    .map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value).slice(0, 6);

  const contactOptions = aliases.map((row) =>
    `<option value="${esc(row.alias)}" ${settings.contact === row.alias ? 'selected' : ''}>${esc(row.alias)}${row.exists ? '' : '（无库）'}</option>`).join('');

  const recentRows = (data.recent_blocked || []).length
    ? data.recent_blocked.map((row) => `<tr>
        <td>${row.ts ? fmtDateTime(row.ts) : '—'}</td>
        <td>${badge(row.kind || '', row.kind === 'send_blocked' ? 'red' : 'orange')}</td>
        <td>${esc(row.reason || '')}</td>
        <td>${esc(String(row.text || '').slice(0, 40))}</td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="empty">窗口内没有被拦/丢弃的事件</td></tr>';

  $('#app').innerHTML = `
    <div class="card">
      <div class="row between">
        <div class="row">
          <span class="hint">窗口</span>
          ${[1, 7, 30].map((value) => `<button class="btn small ${settings.days === value ? 'primary' : ''}"
            data-stats-days="${value}">${value} 天</button>`).join('')}
          <span class="hint">联系人</span>
          <select id="stats-contact"><option value="">全部联系人</option>${contactOptions}</select>
        </div>
        <span class="hint">扫描 ${num((data.window || {}).log_lines_scanned)} 行 · ${esc((data.window || {}).days || '')} 天窗口${(data.errors || []).length ? ' · ' + (data.errors || []).length + ' 个数据源缺失' : ''}</span>
      </div>
    </div>
    <div class="grid cols-4">
      ${statsCard('今日发送', num(today.total), '主动 ' + num(today.proactive) + ' / 回复 ' + num(today.outbound))}
      ${statsCard('成功率', pct(data.success_rate), 'sent ' + num(counts.sent) + ' / failed ' + num(counts.failed) + ' / blocked ' + num(counts.blocked))}
      ${statsCard('丢弃率', pct(data.discard_rate), 'discarded ' + num(Object.values(data.discarded || {}).reduce((a, b) => a + b, 0)))}
      ${statsCard('平均主动间隔', proactive.avg_interval_s === null ? '—' : num(proactive.avg_interval_s) + 's', 'schedule ' + num(proactive.schedule) + ' 次')}
      ${statsCard('内状态 P95', latency.p95 === null ? '—' : num(latency.p95) + 'ms', 'count ' + num(inner.count) + ' · P50 ' + (latency.p50 === null ? '—' : num(latency.p50) + 'ms'))}
    </div>
    <div class="grid cols-2">
      ${card('近 ' + settings.days + ' 天发送量', statsBarRows(dayEntries))}
      ${card('24 小时分布', statsBarRows(hourEntries))}
      ${card('丢弃原因 Top5', statsBarRows(discardedEntries))}
      ${card('被拦原因 Top5', statsBarRows(blockedEntries))}
      ${card('重生成原因 Top5', statsBarRows(regenEntries))}
      ${card('内状态分布（stance）', statsBarRows(stanceEntries) + `<p class="hint" style="margin-top:8px">mode：${esc(JSON.stringify(inner.by_mode || {}))}</p>`)}
    </div>
    <div class="grid cols-3">
      ${card('对话', `<div class="kv">
        <span class="k">总消息</span><span class="v">${num(messages.total)}</span>
        <span class="k">我方/对方</span><span class="v">${num(messages.mine)} / ${num(messages.theirs)}</span>
        <span class="k">平均/中位长度</span><span class="v">${num(messages.avg_len)} / ${num(messages.median_len)} 字</span>
        <span class="k">最近 24h</span><span class="v">${num(messages.active_24h)} 条</span></div>`)}
      ${card('主动消息', `<div class="kv">
        <span class="k">实际发出</span><span class="v">${num(proactive.sent)}</span>
        <span class="k">schedule</span><span class="v">${num(proactive.schedule)} 次</span>
        <span class="k">跳过</span><span class="v">${esc(Object.entries(proactive.skipped || {}).map(([k, v]) => k + ':' + v).join(' · ') || '—')}</span></div>`)}
      ${card('媒体', mediaEntries.length ? statsBarRows(mediaEntries) : '<div class="empty">窗口内没有媒体发送事件</div>')}
    </div>
    ${card('最近 20 条被拦 / 丢弃', `<div style="overflow:auto"><table><thead><tr><th>时间</th><th>类型</th><th>原因</th><th>文本</th></tr></thead><tbody>${recentRows}</tbody></table></div>`)}
    ${card('延迟（事件里的 latency_ms / inner.latency_ms）', `<div class="kv">
      <span class="k">P50</span><span class="v">${data.latency && data.latency.p50 !== null ? num(data.latency.p50) + ' ms' : '—'}</span>
      <span class="k">P95</span><span class="v">${data.latency && data.latency.p95 !== null ? num(data.latency.p95) + ' ms' : '—'}</span>
      <span class="k">内状态 P95</span><span class="v">${latency.p95 !== null ? num(latency.p95) + ' ms' : '—'}</span>
      <span class="k">数据源</span><span class="v mono">${esc(data.db || '—')}</span></div>`)}`;

  $$('[data-stats-days]').forEach((button) => {
    button.onclick = () => {
      state.statsState.days = Number(button.dataset.statsDays);
      loadStats();
    };
  });
  const contact = $('#stats-contact');
  if (contact) contact.onchange = () => {
    state.statsState.contact = contact.value;
    loadStats();
  };
  setLastRefresh();
}

/* ---------------- B站（P11） ---------------- */
function biliFieldControl(field, value) {
  const key = 'bili-' + field.key;
  if (field.type === 'bool') {
    return `<label class="switch"><input type="checkbox" id="${key}" ${value ? 'checked' : ''}>
      <span>${esc(field.label)}</span></label>${field.note ? `<div class="hint">${esc(field.note)}</div>` : ''}`;
  }
  if (field.options) {
    return `<label>${esc(field.label)}</label>
      <select id="${key}">${field.options.map((option) =>
        `<option value="${esc(option)}" ${String(value) === String(option) ? 'selected' : ''}>${esc((field.option_labels || {})[option] || option)}</option>`).join('')}</select>`;
  }
  if (field.type === 'int') {
    return `<label>${esc(field.label)}</label>
      <input type="number" id="${key}" value="${esc(value ?? 0)}" min="${esc(field.min ?? '')}" max="${esc(field.max ?? '')}">
      ${field.note ? `<div class="hint">${esc(field.note)}</div>` : ''}`;
  }
  if (field.type === 'list') {
    return `<label>${esc(field.label)}</label>
      <textarea id="${key}" rows="5" class="mono">${esc((value || []).join('\n'))}</textarea>
      ${field.note ? `<div class="hint">${esc(field.note)}</div>` : ''}`;
  }
  if (field.type === 'text') {
    return `<label>${esc(field.label)}</label>
      <textarea id="${key}" rows="5">${esc(value ?? '')}</textarea>
      ${field.note ? `<div class="hint">${esc(field.note)}</div>` : ''}`;
  }
  return `<label>${esc(field.label)}</label>
    <input type="text" id="${key}" value="${esc(value ?? '')}">
    ${field.note ? `<div class="hint">${esc(field.note)}</div>` : ''}`;
}

function biliLoginCard(status, login) {
  const loggedIn = !!(status && status.logged_in);
  const qr = login && login.qr;
  return card('B站登录', `
    ${loggedIn ? `<div class="row between">
      <div>${pill('已登录', 'ok')} <b>${esc(status.uname || '（已写入凭证）')}</b>
        <span class="hint">mid ${esc(status.mid ?? '—')} · 凭证 ${esc(status.credential_path || '')}</span></div>
      <button class="btn small" id="bili-relogin">重新登录</button></div>`
      : `<div class="row between">
        <div class="hint">手机 B站 → 我的 → 扫一扫。确认时选「记住我/长期」那张。</div>
        <button class="btn primary" id="bili-qr-btn">${qr ? '刷新二维码' : '生成二维码'}</button></div>`}
    <div id="bili-login-area" style="margin-top:10px">
      ${qr ? `<div class="bili-qr">
        <img src="${esc(qr.image)}" alt="B站登录二维码">
        <div class="hint" id="bili-login-state">${esc(login.state || '等待扫码…')}</div>
      </div>` : ''}
      ${loggedIn && status && status.credential_path ? `<div class="hint" style="margin-top:6px">登录成功会写 ${esc(status.credential_path)}（本机文件，不落仓库），插件工具立刻可用。</div>` : ''}
    </div>`);
}

async function renderBili() {
  $('#app').innerHTML = '<div class="empty">加载 B站…</div>';
  if (!state.token) {
    $('#app').innerHTML = card('B站', '<div class="error-text">B站页需要 admin token：请在右上角填写。</div>');
    return;
  }
  if (state.bili && state.bili.timer) { clearInterval(state.bili.timer); }
  let status = null, configState = null;
  const errors = [];
  try { status = await api('/api/bili/status'); } catch (err) { errors.push('状态: ' + err.message); }
  try { configState = await api('/api/bili/config'); } catch (err) { errors.push('配置: ' + err.message); }
  const prev = state.bili || {};
  state.bili = { status, configState, qr: prev.qr || null, timer: null, polling: false };
  if (!status && !configState) {
    $('#app').innerHTML = card('B站', `<div class="error-text">${esc(errors.join('；'))}</div>`);
    return;
  }
  const biliStatus = status || {};
  const config = (configState && configState.config) || biliStatus.config || {};
  const fields = (configState && configState.fields) || [];
  const stateInfo = biliStatus.state || {};
  const report = biliStatus.report || {};
  const login = state.bili.qr ? { qr: state.bili.qr, state: state.bili.loginState || '等待扫码…' } : null;

  const formHtml = fields.map((field) => {
    const full = ['text', 'list'].includes(field.type) || field.key === 'personaPrompt';
    return `<div class="field ${full ? 'span-2' : ''}">${biliFieldControl(field, config[field.key])}</div>`;
  }).join('');

  $('#app').innerHTML = `
    ${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${biliLoginCard(biliStatus, login)}
    <div class="grid cols-3">
      ${card('今日状态', `<div class="kv">
        <span class="k">日期</span><span class="v">${esc(stateInfo.day || '—')}</span>
        <span class="k">已跑/已挑</span><span class="v">${num(stateInfo.count)} / ${num(stateInfo.picked)}（额度 ${num(config.perDay)}）</span>
        <span class="k">最近运行</span><span class="v">${esc(stateInfo.lastRunAt || '—')} ${stateInfo.lastWhy ? '· ' + esc(stateInfo.lastWhy) : ''}</span>
        <span class="k">窗口</span><span class="v">${esc(JSON.stringify(stateInfo.windows || {}))}</span></div>`)}
      ${card('家目录/报告', `<div class="kv">
        <span class="k">家目录</span><span class="v mono">${esc(biliStatus.home || '')}</span>
        <span class="k">今日日报</span><span class="v mono">${esc(report.path || '—')}</span>
        <span class="k">存在</span><span class="v">${report.exists ? '是' : '否'}</span></div>`)}
      ${card('说明', `<div class="hint">登录凭证写入插件家目录（本机文件，不落仓库）；配置保存只改 config.json，不覆盖未知键。
        刷B站触发仍由 DSH 定时任务负责，这里不主动跑。</div>`)}
    </div>
    ${card('设置（与 DSH 刷B站设置页同字段）', `
      <div class="bili-form">${formHtml}</div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="bili-save">保存生效</button>
        <span class="hint">保存后插件下一轮生效；逐字段校验失败会显示原因。</span>
      </div>
      <div id="bili-save-result" style="margin-top:8px"></div>`)}
    ${card('今日日报预览', report.preview
      ? `<pre class="bili-report">${esc(report.preview)}</pre>
         <div class="row"><button class="btn small" id="bili-report-full">查看全文</button>
         <span class="hint">${esc(report.path || '')}</span></div>`
      : '<div class="empty">今天还没有日报（插件还没跑，或 reportLevel=off）</div>')}`;

  const qrBtn = $('#bili-qr-btn');
  if (qrBtn) qrBtn.onclick = startBiliQr;
  const relogin = $('#bili-relogin');
  if (relogin) relogin.onclick = () => { state.bili.qr = null; state.bili.loginState = ''; renderBili(); };
  const save = $('#bili-save');
  if (save) save.onclick = saveBiliConfig;
  const reportFull = $('#bili-report-full');
  if (reportFull) reportFull.onclick = showBiliReport;
  if (state.bili.qr && !state.bili.timer) startBiliPoll();
}

async function startBiliQr() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  try {
    const result = await api('/api/bili/login/qr', { method: 'POST', body: {} });
    if (!result.ok) { toast(result.hint || result.error || '生成二维码失败', 'err'); return; }
    state.bili.qr = { key: result.key, image: result.image, expires_in: result.expires_in };
    state.bili.loginState = '等待扫码…';
    state.bili.polledAt = Date.now();
    state.bili.loginExpired = false;
    renderBili();
    startBiliPoll();
  } catch (err) { toast(err.message, 'err'); }
}

function startBiliPoll() {
  if (state.bili.timer) clearInterval(state.bili.timer);
  state.bili.timer = setInterval(async () => {
    if (state.page !== 'bili' || !state.bili || !state.bili.qr) {
      if (state.bili && state.bili.timer) clearInterval(state.bili.timer);
      return;
    }
    if (Date.now() - (state.bili.polledAt || 0) > (state.bili.qr.expires_in || 180) * 1000) {
      state.bili.loginState = '二维码已过期，点上方「刷新二维码」重新生成';
      state.bili.loginExpired = true;
      clearInterval(state.bili.timer); state.bili.timer = null;
      const node = $('#bili-login-state'); if (node) node.textContent = state.bili.loginState;
      const refreshBtn = $('#bili-qr-btn'); if (refreshBtn) refreshBtn.textContent = '刷新二维码';
      toast('二维码已过期，请点「刷新二维码」', 'err');
      return;
    }
    try {
      const result = await api('/api/bili/login/poll?key=' + encodeURIComponent(state.bili.qr.key));
      if (!result.ok) { state.bili.loginState = result.hint || result.error || '网络错误'; }
      else if (result.status === 'waiting') state.bili.loginState = '等待扫码…';
      else if (result.status === 'scanned') state.bili.loginState = '已扫码，请在手机上确认…';
      else if (result.status === 'expired') state.bili.loginState = '二维码已过期，请重新生成';
      else if (result.status === 'ok') {
        state.bili.loginState = '登录成功' + (result.uname ? '：' + result.uname : '');
        clearInterval(state.bili.timer); state.bili.timer = null;
        toast('B站登录成功' + (result.uname ? '：' + result.uname : ''), 'ok');
        state.bili.qr = null;
        renderBili();
        return;
      }
      const node = $('#bili-login-state');
      if (node) node.textContent = state.bili.loginState;
    } catch (err) { state.bili.loginState = err.message; }
  }, 2000);
}

async function saveBiliConfig() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const fields = (state.bili.configState && state.bili.configState.fields) || [];
  const updates = {};
  for (const field of fields) {
    const node = $('#bili-' + field.key);
    if (!node) continue;
    if (field.type === 'bool') updates[field.key] = node.checked;
    else if (field.type === 'int') updates[field.key] = Number(node.value);
    else if (field.type === 'list') updates[field.key] = node.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    else updates[field.key] = node.value;
  }
  try {
    const result = await api('/api/bili/config', { method: 'POST', body: { updates } });
    if (result.errors && result.errors.length) {
      $('#bili-save-result').innerHTML = `<div class="error-text">${esc(result.errors.join('；'))}</div>`;
      toast('保存失败：字段校验未通过', 'err');
      return;
    }
    $('#bili-save-result').innerHTML = '<span class="pill ok"><span class="dot"></span>已保存，插件下一轮生效</span>';
    toast('B站配置已保存', 'ok');
    state.bili.configState = result;
  } catch (err) {
    $('#bili-save-result').innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
    toast(err.message, 'err');
  }
}

async function showBiliReport() {
  const date = ((state.bili && state.bili.status && state.bili.status.report) || {}).date || '';
  try {
    const result = await api('/api/bili/report' + (date ? '?date=' + encodeURIComponent(date) : ''));
    if (!result.ok) { toast(result.error === 'no_report' ? '今天还没有日报' : (result.hint || result.error), 'err'); return; }
    showModal('B站日报 ' + (result.date || ''),
      `${result.content || ''}${result.truncated ? '\n\n...（超过 200KB 已截断）' : ''}`);
  } catch (err) { toast(err.message, 'err'); }
}

/* ---------------- 路由 ---------------- */
const RENDERERS = {
  overview: renderOverview,
  feed: renderFeed,
  chat: renderChatTest,
  inner: renderInner,
  media: renderMedia,
  memory: renderMemory,
  data: renderData,
  knowledge: renderMemoryEdit,
  prompts: renderPrompts,
  models: renderModels,
  stats: renderStats,
  providers: renderProviders,
  logs: renderLogs,
  bili: renderBili,
  settings: renderSettings,
};

async function go(page, auto) {
  if (!PAGES[page]) page = 'overview';
  if (page !== 'bili' && state.bili && state.bili.timer) {
    clearInterval(state.bili.timer); state.bili.timer = null;
  }
  state.page = page;
  $('#page-title').textContent = PAGES[page];
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  if (location.hash !== '#' + page && !auto) history.replaceState(null, '', '#' + page);
  try {
    await RENDERERS[page](auto);
    // 切页后内容区回到顶部（否则从长页面跳转会停留在底部）
    const appEl = $('#app');
    if (appEl) appEl.scrollTop = 0;
    window.scrollTo(0, 0);
    setLastRefresh();
  } catch (err) {
    $('#app').innerHTML = `<div class="card"><div class="error-text">页面渲染失败：${esc(err.message || err)}</div></div>`;
  }
}

function bindGlobal() {
  $$('.nav-item').forEach((item) => item.addEventListener('click', () => go(item.dataset.page)));
  window.addEventListener('hashchange', () => go(location.hash.replace('#', '') || 'overview'));
  $('#btn-refresh').onclick = () => go(state.page);
  $('#modal-close').onclick = closeModal;
  $('#modal-mask').onclick = (event) => { if (event.target === $('#modal-mask')) closeModal(); };
  $('#factory-cancel').onclick = () => closeFactory();
  $('#factory-mask').onclick = (event) => {
    if (event.target === $('#factory-mask') && !factory.busy) closeFactory();
  };
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if ($('#factory-mask').classList.contains('open') && !factory.busy) closeFactory();
  });
  const tokenInput = $('#token-input');
  tokenInput.value = state.token;
  const connect = async () => {
    const value = tokenInput.value.trim();
    if (!value) { toast('请先填 admin token（本机默认 8888）', 'err'); return; }
    state.token = value;
    sessionStorage.setItem('console_token', state.token);
    try {
      const st = await api('/api/status');
      if (st && st.ok) {
        toast('已连接', 'ok');
        await go(state.page);
      } else {
        toast('连接失败：token 可能无效', 'err');
      }
    } catch (err) {
      toast('连接失败：' + (err && err.message ? err.message : err), 'err');
    }
  };
  const connectBtn = $('#btn-connect');
  if (connectBtn) connectBtn.onclick = connect;
  tokenInput.onkeydown = (event) => { if (event.key === 'Enter') connect(); };
  // 本机便利：自动同步本机 token（后端只允许回环地址）。
  // 注意：即便 sessionStorage 里已有旧 token 也要比对——token 改过后旧值会 401，
  // 这里自动换成新的，避免用户手动清缓存。
  api('/api/token').then((data) => {
    if (data && data.ok && data.token && data.token !== state.token) {
      state.token = data.token;
      tokenInput.value = state.token;
      sessionStorage.setItem('console_token', state.token);
      go(state.page);
    }
  }).catch(() => {});
  state.timer = setInterval(() => {
    if (document.hidden) return;
    if (state.page === 'feed' && state.feedAuto) renderFeed(true).then(setLastRefresh).catch(() => {});
  }, 3000);
}

bindGlobal();
go(location.hash.replace('#', '') || 'overview');
