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
  trace: null,
  moments: null,
  diary: null,
  web: null,
  sendMode: null,
  library: null,
  status: null,
  inner: null,
  tone: null,
};

const PAGES = {
  overview: '总览',
  feed: '对话流',
  trace: '推理过程',
  chat: '对话测试',
  inner: '内状态',
  media: '媒体',
  library: '素材库',
  data: '数据管理',
  knowledge: '记忆编辑',
  memory: '记忆',
  prompts: '提示词',
  models: '模型',
  stats: '统计',
  providers: '模型配置',
  logs: '日志',
  bili: 'B站',
  moments: '朋友圈',
  diary: '日记',
  web: '上网记录',
  settings: '设置',
};

/* 统一枚举中文映射：接口仍返回英文，前端只负责展示。 */
const ENUM_LABELS = {
  memory_kind: {
    preference: '偏好', insight: '洞察', fact: '事实', plan: '计划', event: '事件',
    skill: '技能', relation: '关系', identity: '身份', habit: '习惯', goal: '目标',
    emotion: '情绪', mood: '心情', rule: '规则', summary: '摘要', preference_note: '偏好补充', other: '其他',
  },
  memory_status: {
    pending: '待生效', active: '生效中', archived: '已归档', superseded: '已替代', forgotten: '已遗忘', full: '完整', decayed: '已衰减', dormant: '休眠', invalid: '已失效',
    merged: '已合并', archived: '已归档',
  },
  memory_tier: { hot: '近期', warm: '中期', cold: '长期' },
  event_kind: {
    event: '事件', inbound: '收到消息', outbound: '回消息', message: '消息',
    surf_skipped: '冲浪跳过', surf_started: '开始冲浪', surf_finished: '冲浪结束',
    quiet_hours: '静音时段', outside_window: '不在允许时段', engine_disabled: '引擎已关闭',
    proactive_skipped: '概率跳过', proactive_stopped: '已停发',
    proactive_send: '主动发送', proactive_schedule: '主动调度', proactive_skipped: '概率跳过',
    proactive_stopped: '已停发', send_blocked: '发送被拦', send_probe: '发送探针',
    draft: '草稿', context_budget: '上下文裁剪', voice_polished: '语气润色',
    config_reloaded: '配置热加载', gateway_started: '网关启动', gateway_stale: '死区告警',
    gate: '闸门', memory: '记忆', claim: '事实', media: '媒体',
  },
  event_status: {
    pending: '待处理', planned: '计划中', sent: '已发送', delivered: '已送达', blocked: '已拦截',
    failed: '失败', error: '失败', queued: '排队中', running: '运行中',
    done: '完成', dry_run: '演练', skipped: '已跳过', cancelled: '已取消', draft: '草稿',
    ok: '正常', active: '生效中',
  },
  log_level: { debug: '调试', info: '信息', warn: '警告', warning: '警告', error: '错误', critical: '严重', fatal: '致命' },
  model_format: { gguf: 'GGUF 模型', hf: 'HuggingFace 目录', adapter: '适配器', safetensors: 'HF 权重', unknown: '未知格式' },
  model_source: { builtin: '内置', downloaded: '在线下载', imported: '本地导入', static: '内置' },
  model_role: { soul: '灵魂模型', chat: '对话模型', inner: '内状态', vision: '视觉模型', embed: '向量模型', reserved: '预留', research: '研究模型', local: '本地', remote: '远程', base: '基础模型' },
  model_status: { running: '运行中', stopped: '已停止', error: '异常', unknown: '未知', reserved: '预留', external: '外部启动', ready: '就绪', reference: '参考模型' },
  media_type: { text: '文本', emoji_text: '表情', emoji: '表情', image: '图片', gif: 'GIF', sticker: '贴纸', sticker_as_image: '贴纸（图片）', video: '视频', voice: '语音', file: '文件' },
  bili_status: { waiting: '等待扫码', scanned: '已扫码待确认', expired: '已过期', ok: '成功', error: '失败', running: '运行中', skipped: '跳过', pending: '待运行', done: '完成', failed: '失败', outside_window: '不在允许时段', quiet_hours: '静音时段', no_contact: '无联系人' },
  send_mode: { auto: '自动发送', confirm: '确认后发送', off: '不发送' },
  trace_mode: { chat: '聊天', proactive: '主动消息' },
  chat_role: { user: '用户', assistant: '她', system: '系统' },
  guard_level: { L0: '通过', L1: '提示', L2: '拦截重写', L3: '拦截' },
  inner_mode: { off: '关闭', static: '静态', rule: '规则', model: '模型' },
  inner_scope: { all: '全部', proactive_only: '仅主动消息', off: '关闭' },
  inner_schema: { slim: '精简', full: '完整' },
};
function enumLabel(group, value, fallback) {
  const table = ENUM_LABELS[group] || {};
  const key = String(value === null || value === undefined ? '' : value);
  return table[key] || table[key.toLowerCase()] || fallback || (key || '—');
}
function enumBadge(group, value, cls) {
  return badge(enumLabel(group, value), cls);
}

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
    // P20：优先展示服务端中文 hint；保留 payload 供外部进程二次确认使用
    const detail = payload ? (payload.hint || payload.error
      || (payload.errors || []).join('; ')) : response.status;
    const error = new Error(detail || ('HTTP ' + response.status));
    error.payload = payload;
    error.httpStatus = response.status;
    throw error;
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
    if (proactive.stopped) pills.push(pill('紧急停止已启用', 'err'));
  } else if (status) {
    pills.push(pill('网关未连接', 'err'));
  }
  const engine = state.engine || (status && status.status && status.status.engine);
  if (engine) {
    pills.push(pill(engine.enabled ? '运行中' : '已停止', engine.enabled ? 'ok' : 'err'));
  }
  if (state.config && state.config.config) {
    const cfg = state.config.config;
    pills.push(pill('内状态：' + enumLabel('inner_mode', cfg.inner_mode || 'off'), cfg.inner_mode && cfg.inner_mode !== 'off' ? 'info' : ''));
    if (cfg.inner_shadow_only) pills.push(pill('内状态只记录不注入', 'warn'));
  }
  const wrap = $('#status-pills');
  if (wrap) wrap.innerHTML = pills.join('');
}

/* ---------------- 总览 ---------------- */
async function renderOverview() {
  $('#app').innerHTML = '<div class="empty">加载总览…</div>';
  let status = null, feed = null, config = null, models = null, engine = null;
  const errors = [];
  try { status = await api('/api/status'); } catch (e) { errors.push('gateway: ' + e.message); }
  try { engine = await api('/api/engine'); } catch (e) { errors.push('引擎: ' + e.message); }
  try { feed = await api('/api/feed?limit=6'); } catch (e) { errors.push('事件流: ' + e.message); }
  try { config = await api('/api/config'); } catch (e) { errors.push('配置: ' + e.message); }
  try { models = await api('/api/models'); } catch (e) { errors.push('模型: ' + e.message); }
  state.status = status; state.config = config; state.models = models; state.engine = engine;
  renderPills();

  if (!status || !status.ok) {
    const reason = (status && status.error) || (errors[0] || '未知错误');
    $('#app').innerHTML = renderEngineCard(engine) +
      card('网关状态', `<div class="error-text">网关未连接：${esc(reason)}</div>
      <p class="hint">确认 <code>python -m agi.wechat_gateway.server</code> 正在 127.0.0.1:801 运行；本页数据仍会显示本机文件部分。</p>`) +
      card('最近事件', renderEventList((feed && feed.items) || []));
    bindEngineActions();
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
      <span class="k">模式</span><span class="v">${esc(enumLabel('inner_mode', innerMode))}${innerShadow ? '（只记录不注入）' : ''}</span>
      <span class="k">作用范围</span><span class="v">${esc(enumLabel('inner_scope', (config && config.config && config.config.inner_mode_scope), '—'))}</span>
      <span class="k">字段集</span><span class="v">${esc(enumLabel('inner_schema', config && config.config && config.config.inner_schema, '—'))}</span>
      <span class="k">soul-05b</span><span class="v">${soul ? (soul.healthy ? '健康（端口 ' + esc(soul.port) + '）' : '未运行（端口 ' + esc(soul.port) + '）') : '未登记'}</span>
    </div>`;

  const html = `
    ${errors.length ? `<div class="card"><div class="error-text">部分数据获取失败：${esc(errors.join('；'))}</div></div>` : ''}
    ${renderEngineCard(engine)}
    <div class="grid cols-4">
      ${card('主动消息', `<div class="big">${proactive.enabled ? '开' : '关'}</div>
        <div class="muted">概率 ${num((proactive.probability || {}).min)}–${num((proactive.probability || {}).max)} · 间隔 ${num((proactive.interval_s || {}).min)}–${num((proactive.interval_s || {}).max)}s</div>
        <div class="row" style="margin-top:8px">${proactive.stopped ? pill('紧急停止已启用', 'err') : pill('未停发', 'ok')}</div>`)}
      ${card('今日发送', `<div class="big">${num(sent)}</div><div class="muted">失败 ${num((st.send || {}).failed)} · 演练模式 ${(st.send || {}).dry_run ? '开' : '关'}</div>`)}
      ${card('后台监听', `<div class="big">${active ? '运行' : '未运行'}</div><div class="muted">最近事件 ${fmtAgo(state.lastRefresh / 1000)}刷新</div>
        <div class="muted">${proactive.stale ? pill('死区告警', 'warn') : pill('无死区', 'ok')}</div>`)}
      ${card('内状态 / 模型', innerRows)}
    </div>
    <div class="grid cols-2">
      ${card('最近 5 条事件', renderEventList(recent.length ? recent : (feed && feed.items) || []))}
      ${card('最近对话片段', renderMiniMessages((st.recent || []).slice(-6).reverse()))}
    </div>`;
  $('#app').innerHTML = html;
  bindEngineActions();
}

function renderEngineCard(engine) {
  const available = engine && engine.available !== false;
  const enabled = engine ? !!engine.enabled : null;
  const statusPill = enabled === true ? pill('🟢 运行中', 'ok')
    : (enabled === false ? pill('⏸ 已停止', 'err') : pill('状态未知', 'warn'));
  const since = engine && engine.since ? fmtDateTime(engine.since) : '—';
  const reasonMap = { startup: '开机启动', manual_start: '手动开始', manual_stop: '手动停止',
    config_disabled: '配置禁用', persist_failed: '持久化失败' };
  const reason = engine ? (reasonMap[engine.reason] || engine.reason || '—') : '—';
  const button = enabled === true
    ? '<button class="btn danger" id="engine-stop">停止运行</button>'
    : `<button class="btn success" id="engine-start" ${available && enabled !== null ? '' : 'disabled'}>开始运行</button>`;
  return `<div class="card engine-card">
    <div class="row between">
      <div>
        <div class="row">${statusPill}<b>${enabled === true ? '引擎运行中' : (enabled === false ? '引擎已停止' : '引擎状态未知')}</b></div>
        <div class="hint" style="margin-top:6px">起始 ${esc(since)} · 原因 ${esc(reason)}${engine && engine.manual ? ' · 手动停止' : ''}</div>
        <div class="hint">停止后不再监听和自动回复、主动消息暂停；控制台与对话测试仍可用（停止不会关闭控制台）。</div>
      </div>
      <div class="row">${button}${available ? '' : '<span class="pill err"><span class="dot"></span>网关不可达</span>'}</div>
    </div>
    ${engine && engine.ok === false ? `<div class="error-text" style="margin-top:6px">${esc(engine.error || '')} ${esc(engine.hint || '')}</div>` : ''}
  </div>`;
}

function bindEngineActions() {
  const stop = $('#engine-stop');
  if (stop) stop.onclick = () => toggleEngine('stop');
  const start = $('#engine-start');
  if (start) start.onclick = () => toggleEngine('start');
}

async function toggleEngine(action) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  if (action === 'stop' && !window.confirm('停止后不再监听和自动回复、主动消息暂停；控制台与对话测试仍可用。 确定停止运行？')) return;
  const button = action === 'stop' ? $('#engine-stop') : $('#engine-start');
  const label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = '处理中…'; }
  try {
    const result = await api('/api/engine/' + action, { method: 'POST', body: {} });
    if (result.ok === false) {
      throw new Error(result.error === 'gateway_unreachable' ? '网关不可达' : (result.error || '操作失败'));
    }
    toast(action === 'stop' ? '已停止运行' : '已开始运行', 'ok');
    await renderOverview();
  } catch (err) {
    toast(err.message, 'err');
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

function renderEventList(items) {
  if (!items || !items.length) return '<div class="empty">暂无事件</div>';
  return '<div class="list">' + items.map((event) => {
    const kind = event.kind || 'event';
    const text = event.text || event.error || event.reason || event.next_delay_s || '';
    const cls = kindCls(kind);
    return `<div class="list-item" data-json='${esc(JSON.stringify(event))}'>
      <span class="time">${fmtTime(event.ts)}</span>
      <div class="grow"><div class="title">${badge(enumLabel('event_kind', kind), cls)} ${esc(String(text).slice(0, 140))}</div></div>
      <span class="badge">${esc(event.friend || event.contact || '')}</span>
    </div>`;
  }).join('') + '</div>';
}

function renderMiniMessages(items) {
  if (!items || !items.length) return '<div class="empty">暂无消息</div>';
  return '<div class="feed">' + items.map((m) => {
    const mine = m.role !== 'user';
    return `<div class="bubble-row ${mine ? 'mine' : ''}">
      <div class="bubble"><div class="meta">${esc(enumLabel('chat_role', m.role))} · ${fmtTime(m.ts)}</div>
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
        <div class="grow"><div class="title">${badge(enumLabel('event_kind', kind), kindCls(kind))} ${esc(feedSummary(event))}</div></div>
      </div>`);
    }
  }
  return '<div class="feed">' + rows.join('') + '</div>';
}

function feedSummary(event) {
  if (event.kind === 'proactive_schedule') return '下次检查间隔 ' + num(event.next_delay_s) + 's';
  if (event.kind === 'proactive_skipped') return '概率跳过（概率 ' + num(event.probability) + ' / 随机值 ' + num(event.roll) + '）';
  if (event.kind === 'send_blocked') return '被拦：' + (event.reason || event.error || '');
  if (event.kind === 'context_budget') return '用量=' + num(event.tokens) + ' · 丢弃=' + esc((event.dropped || []).join('，'));
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
  const status = event.status ? badge(enumLabel('event_status', event.status), blocked ? 'red' : 'green') : '';
  const reason = blocked ? `<div class="hint" style="color:var(--error)">${esc(event.reason || event.error || '被拦截')}</div>` : '';
  return `<div class="bubble-row ${cls}">
    <div class="bubble" data-json='${esc(JSON.stringify(event))}'>
      <div class="meta">${badge(enumLabel('event_kind', kind), kindCls(kind))} ${status} <span>${fmtTime(event.ts)}</span>
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
      <th>时间</th><th>心情</th><th>主要变化</th><th>原因</th><th>置信度</th><th>模式</th>
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
      <td>${badge(enumLabel('trace_mode', row.mode), '')}</td></tr>`;
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
          <span class="hint">后端 ${esc(enumLabel('inner_mode', settings.backend, settings.backend || '规则/模型'))} · 门槛 ≥${esc(confidenceMin)}</span>
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
  const source = item.source ? badge('来源：' + enumLabel('inner_mode', item.source, item.source), '') : '';
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
        <div class="muted">生效中 ${num(status.active)} · 休眠 ${num(status.dormant)} · 待生效 ${num(status.pending)}</div>`)}
      ${card('时间分层（生效中）', `<div class="big">${num(tiers.hot)} / ${num(tiers.warm)} / ${num(tiers.cold)}</div>
        <div class="muted">近期 ≤30 天 · 中期 30–90 天 · 长期 >90 天</div>`)}
      ${card('休眠率', `<div class="big">${pct(memories.dormant_rate)}</div>
        <div class="muted">阈值 >40% 告警（夜间任务）</div>`)}
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
      ${card('摘要 / 事实', `<div class="kv">
        <span class="k">事实</span><span class="v">${num((m.claims || {}).total)}</span>
        <span class="k">摘要</span><span class="v">${num((m.summaries || {}).total)}</span>
        <span class="k">月度摘要</span><span class="v">${num((m.summaries || {}).monthly)}（${esc((m.summaries || {}).latest_month || '—')}）</span></div>`)}
    </div>`;

function forgetBar(row) {
  const pct = Math.max(0, Math.min(100, Math.round(Number(
    row.forget_percent ?? (1 - Number(row.strength ?? 1)) * 100))));
  const stage = row.forget_stage_zh || enumLabel('forget_stage', row.forget_stage, row.forget_stage || '完整');
  const strength = row.strength === undefined || row.strength === null ? '—' : Number(row.strength).toFixed(2);
  const half = row.half_life_days === undefined || row.half_life_days === null ? '—' : Number(row.half_life_days).toFixed(1) + ' 天';
  const last = row.last_strength_at ? fmtDateTime(row.last_strength_at) : '—';
  const cls = pct < 30 ? 'fresh' : (pct <= 60 ? 'warm' : 'hot');
  const hint = `保留强度 ${strength} · 遗忘阶段 ${stage} · 半衰期 ${half} · 最近计算 ${last}`;
  return `<div class="forget-cell" title="${esc(hint)}">
    <span class="forget-num">${pct}%</span>
    <span class="forget-track"><span class="forget-fill ${cls}" style="width:${pct}%"></span></span>
    <span class="forget-hint">${esc(stage)}</span>
  </div>`;
}

  const memoryTable = `<table><thead><tr><th>内容</th><th>类型</th><th>重要性</th><th>遗忘度</th><th>状态</th><th>创建时间</th><th>访问次数</th></tr></thead>
    <tbody>${recent.map((row) => `<tr><td>${esc(String(row.content || '').slice(0, 120))}</td>
      <td>${badge(enumLabel('memory_kind', row.kind))}</td><td class="num">${num(row.importance)}</td>
      <td>${forgetBar(row)}</td>
      <td>${badge(enumLabel('memory_status', row.status), row.status === 'active' ? 'green' : (row.status === 'dormant' ? 'orange' : ''))}</td>
      <td>${fmtDateTime(row.created_at)}</td><td class="num">${num(row.access_count)}</td></tr>`).join('') ||
      '<tr><td colspan="7" class="empty">暂无记忆</td></tr>'}</tbody></table>`;

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
const modelBusy = new Set();   // P20：防止启动/停止按钮重复提交/重复 toast

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
        <div><b>${esc(model.name)}</b> ${badge(enumLabel('model_role', model.role))} ${model.reserved ? badge('预留', 'orange') : ''}</div>
        ${model.running
          ? (model.external ? badge('运行中（外部启动）', 'orange') : pill('运行中', 'ok'))
          : (model.healthy ? pill('健康', 'ok') : pill('未运行', ''))}
      </div>
      <div class="kv" style="margin-top:8px">
        <span class="k">端口</span><span class="v">${num(model.port)}</span>
        <span class="k">状态</span><span class="v">${esc(enumLabel('model_status', model.status, model.status || '—'))}</span>
        ${model.pid ? `<span class="k">进程号</span><span class="v">${num(model.pid)}</span>` : ''}
        <span class="k">用途</span><span class="v">${esc(model.purpose || '')}</span>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn small primary" data-model-start="${esc(model.name)}" ${model.editable ? '' : 'disabled'}>启动</button>
        <button class="btn small danger" data-model-stop="${esc(model.name)}" ${model.editable ? '' : 'disabled'}>停止</button>
        <span class="hint">${model.external ? '外部启动的进程，停止前会二次确认' : (model.editable ? '' : (model.reserved ? '预留模型，禁止启停' : '只读'))}</span>
      </div>
    </div>`).join('') || '<div class="empty">模型注册表为空（agi/data/local_models.json）</div>';

  const cfg = (config && config.config) || {};
  const innerForm = `
    <div class="grid cols-2">
      <div class="field"><label>内状态模式</label>
        <select id="cfg-inner-mode">${['off', 'static', 'rule', 'model'].map((mode) =>
          `<option value="${mode}" ${cfg.inner_mode === mode ? 'selected' : ''}>${enumLabel('inner_mode', mode)}</option>`).join('')}</select></div>
      <div class="field"><label>作用范围</label>
        <select id="cfg-inner-scope">${['proactive_only', 'all', 'off'].map((scope) =>
          `<option value="${scope}" ${cfg.inner_mode_scope === scope ? 'selected' : ''}>${enumLabel('inner_scope', scope)}</option>`).join('')}</select></div>
      <div class="field"><label>字段集</label>
        <select id="cfg-inner-schema">${['slim', 'full'].map((schema) =>
          `<option value="${schema}" ${cfg.inner_schema === schema ? 'selected' : ''}>${enumLabel('inner_schema', schema)}</option>`).join('')}</select></div>
      <div class="field"><label>影子模式（只记录不注入）</label>
        <label class="switch"><input type="checkbox" id="cfg-inner-shadow" ${cfg.inner_shadow_only ? 'checked' : ''}> 只记录不注入</label></div>
    </div>
    <div class="row" style="margin-top:12px"><button class="btn primary" id="btn-save-inner">保存内状态设置</button>
      <span class="hint">当前影子灰度：模式=${esc(enumLabel('inner_mode', cfg.inner_mode, '—'))} · 只记录不注入=${cfg.inner_shadow_only ? '开' : '关'} · 作用范围=${esc(enumLabel('inner_scope', cfg.inner_mode_scope, '—'))}</span></div>`;

  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="grid cols-3">${cards}</div>
    ${card('内状态注入开关（热加载，保存后下一条消息生效）', innerForm)}
    ${card('模型库管理（本地导入 / 网址下载 / 回收站删除）', modelAdminHtml(models))}
    ${card('说明', `<p class="hint">启停按钮只允许 ${esc(allow.join(', ') || 'soul-05b')}；1.5B 为 预留模型，故意拒绝。外部启动的 llama-server 停止前会二次确认；不会误杀其它进程。</p>`)}`;

  $$('[data-model-start]').forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.modelStart;
      if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
      if (modelBusy.has(name)) return;           // P20：防重复提交/重复 toast
      modelBusy.add(name);
      btn.disabled = true;
      try {
        const result = await api('/api/models/start', { method: 'POST', body: { name } });
        const pid = (result.pid === null || result.pid === undefined) ? '未知' : result.pid;
        let message;
        if (result.already_running) {
          message = name + (result.external
            ? ' 已在运行（外部启动，PID ' + pid + '）'
            : ' 已在运行（PID ' + pid + '）');
        } else {
          message = '已启动 ' + name + '（PID ' + pid + '）';
        }
        toast(message, 'ok');
        await renderModels();
      } catch (e) { toast(e.message || '启动失败', 'err'); }
      finally { modelBusy.delete(name); btn.disabled = false; }
    };
  });
  $$('[data-model-stop]').forEach((btn) => {
    btn.onclick = async () => {
      const name = btn.dataset.modelStop;
      if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
      if (modelBusy.has(name)) return;
      modelBusy.add(name);
      btn.disabled = true;
      try {
        const result = await api('/api/models/stop', { method: 'POST', body: { name } });
        toast(result.hint || ('已停止 ' + name), 'ok');
        await renderModels();
      } catch (e) {
        const payload = e.payload || {};
        // P20：外部启动的进程不直接 kill；先弹中文二次确认，再带 force_external 重试
        if (payload.status === 'external' || payload.external || payload.needs_confirm) {
          const pid = payload.pid === null || payload.pid === undefined ? '未知' : payload.pid;
          const port = payload.port === null || payload.port === undefined ? '未知' : payload.port;
          const ok = confirm(name + ' 不是控制台启动的（PID ' + pid + '、端口 ' + port + '），仍要停止吗？');
          if (ok) {
            try {
              const forced = await api('/api/models/stop', {
                method: 'POST', body: { name, force_external: true },
              });
              toast(forced.hint || ('已停止 ' + name), 'ok');
              await renderModels();
            } catch (e2) { toast(e2.message || '停止失败', 'err'); }
          } else {
            toast('已取消停止', '');
          }
        } else {
          toast(e.message || '停止失败', 'err');
        }
      } finally { modelBusy.delete(name); btn.disabled = false; }
    };
  });
  bindModelAdmin();
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
    ${card('紧急停止（停止文件）', `
      <div class="row between">
        <div>${stopped ? pill('紧急停止已启用：所有外发被拦截', 'err') : pill('未停发', 'ok')}
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
    ['条事实', stats.claims],
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
        ${esc(deleted.claims || 0)} 条事实 / ${esc(deleted.files || 0)} 个文件（${esc(result.took_s || 0)}s）</p>
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
  return ({ text: '文本', emoji_text: '表情', image: '图片',
    gif: 'GIF', sticker: '贴纸', sticker_as_image: '贴纸(降级图片)' })[kind] || kind;
}

async function renderMedia() {
  $('#app').innerHTML = '<div class="empty">加载媒体页…</div>';
  const errors = [];
  let caps = null, stats = null, library = null, wishes = null;
  try { caps = await api('/api/media/capabilities'); } catch (e) { errors.push('能力矩阵: ' + e.message); }
  try { stats = await api('/api/media/stats'); } catch (e) { errors.push('统计: ' + e.message); }
  try { library = await api('/api/library/list?enabled=1&limit=300'); } catch (e) { errors.push('素材库: ' + e.message); }
  try { wishes = await api('/api/media/wishes'); } catch (e) { errors.push('愿望清单: ' + e.message); }
  const prev = state.media || {};
  state.media = {
    caps, stats, library, wishes,
    type: prev.type || 'text',
    assetPath: prev.assetPath || '',
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
  const library = data.library || {};
  const wishes = data.wishes || {};
  const options = mediaCapabilityOptions(caps);
  if (!options.length) options.push(['text', '文本']);
  if (!options.some(([type]) => type === data.type)) data.type = options[0][0];
  const isText = ['text', 'emoji_text', 'native_emoji'].includes(data.type);
  // 只展示素材库（media_store 权威数据）里启用且能拿到缩略图的项；软删/停用立即消失
  const assetItems = (library.items || []).filter((item) => item.thumb && item.file_exists !== false);
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
        ${assetItems.map((item) => `<option value="${esc(item.path || '')}" ${item.path === data.assetPath ? 'selected' : ''}>${esc(item.name || item.id)}${item.type ? ' · ' + esc(item.type) : ''}</option>`).join('')}
      </select><div class="hint">${assetItems.length ? (selectedAsset ? '已选：' + esc(selectedAsset.name || selectedAsset.path) : '素材来自「素材库」页（已启用且可预览）')
        : '素材库暂无可用素材，请到「素材库」页新增/启用'}</div></div>`}
    <div class="row" style="grid-column: 1 / -1">
      <button class="btn primary" id="btn-media-send">发送测试</button>
      <span class="hint">只允许发「${esc(caps.test_contact || '文件传输助手')}」；body 里传别的联系人会被强制覆盖。${selectedCap.status === 'no' ? '当前类型不可用。' : ''}</span>
    </div>
    <div style="grid-column: 1 / -1" id="media-send-result">${renderMediaResult(data.result)}</div>
  </div>`;

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
    <div class="hint" style="margin:0 2px">素材管理请到「素材库」页（新增/编辑类型/含义/标签/启用/删除）；本页只负责发送与导入。</div>
    ${statCards}
    ${card('能力矩阵', capTable)}
    ${card('测试发送（只发文件传输助手）', sendPanel)}
    ${mediaImportPanelHtml(data)}
    ${card('愿望清单（本地导入满足 / 联网获取仍为预留）', `
      <p class="hint">${esc(wishes.note || '')}</p>${wishesHtml}`)}`;

  const typeSelect = $('#media-type');
  if (typeSelect) typeSelect.onchange = () => { data.type = typeSelect.value; renderMediaPage([]); };
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
  ['llm_model', '对话模型名称', 'text'],
  ['llm_temperature', '语气变化（0–2，越高越活泼）', 'number'],
  ['llm_max_tokens', '单次最多字数（1–32000）', 'number'],
  ['soul_server_url', '内状态服务地址', 'text'],
  ['soul_timeout_s', '内状态超时（秒）', 'number'],
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
      <div class="row" style="margin-top:10px;gap:8px;align-items:center">
        <input type="password" id="ds-key" placeholder="粘贴 DeepSeek API key（sk-…）" style="flex:1;max-width:420px" autocomplete="off">
        <button class="btn primary" id="ds-key-save">保存 key</button>
        <button class="btn" id="ds-key-clear">清除</button>
      </div>
      <p class="hint" style="margin-top:6px">key 只写入本机 <code>agi/data/secrets.json</code>（不上传、不进 git、不回传明文）；环境变量 <code>DEEPSEEK_API_KEY</code> 仍优先于文件。</p>
    </div>
    ${card('网关模型/内状态配置（保存后热加载生效）', `
      <div class="grid cols-2 provider-form">
        ${MODEL_FIELDS.map(([key, label, type]) => `<div class="field">
          <label>${esc(label)}</label>
          <input type="${type}" id="mc-${esc(key)}" value="${esc(cfg[key] ?? '')}"
                 ${type === 'number' ? 'step="any"' : ''}>
          <span class="hint mono">${esc(key)}</span>
        </div>`).join('')}
        <div class="field"><label>内状态字段集</label>
          <select id="mc-inner_schema">
            <option value="slim" ${cfg.inner_schema === 'slim' ? 'selected' : ''}>精简（4 个字段，省资源）</option>
            <option value="full" ${cfg.inner_schema === 'full' ? 'selected' : ''}>完整（对照）</option>
          </select></div>
        <div class="field"><label>聊天图片识图</label>
          <select id="mc-chat_vision_mode">
            <option value="local_caption" ${cfg.chat_vision_mode === 'local_caption' ? 'selected' : ''}>本地 VL 描述（默认，省钱）</option>
            <option value="api" ${cfg.chat_vision_mode === 'api' ? 'selected' : ''}>云端视觉（旧行为）</option>
          </select>
          <span class="hint mono">chat_vision_mode</span></div>
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
  const keySave = $('#ds-key-save');
  if (keySave) keySave.onclick = () => saveDeepseekKey(false);
  const keyClear = $('#ds-key-clear');
  if (keyClear) keyClear.onclick = () => saveDeepseekKey(true);
}

async function saveDeepseekKey(clear) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const input = $('#ds-key');
  const key = clear ? '' : (input && input.value || '').trim();
  if (!clear && !key) { toast('先粘贴 key', 'err'); return; }
  if (!clear && !confirm('保存到本机密钥库 agi/data/secrets.json？不会上传。')) return;
  try {
    const result = await api('/api/model-config/deepseek-key', {
      method: 'POST', body: { api_key: key, clear },
    });
    toast(result.note || (clear ? '已清除 key' : '已保存 key'), 'ok');
    renderProviders();
  } catch (err) { toast(err.message, 'err'); }
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
  const visionMode = $('#mc-chat_vision_mode');
  if (visionMode) updates.chat_vision_mode = visionMode.value;
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
    const summary = entry.text || entry.raw || (entry.reason ? enumLabel('bili_status', entry.reason, entry.reason) : '')
      || (entry.kind ? enumLabel('event_kind', entry.kind, '') : '');
    return `<div class="log-row ${entry.error ? 'err' : ''}" data-json='${esc(JSON.stringify(entry))}'>
      <span class="time">${esc(ts)}</span>
      <span class="badge ${entry.error ? 'red' : ''}">${esc(entry.kind ? enumLabel('event_kind', entry.kind, enumLabel('log_level', entry.level, '信息')) : (entry.error ? '错误' : enumLabel('log_level', entry.level, '信息')))}</span>
      <span class="log-text">${esc(String(summary).slice(0, 240))}</span>
      <span class="hint mono">${esc(entry._file || '')}:${esc(entry._line || '')}</span>
    </div>`;
  }).join('') : '<div class="empty">没有匹配的日志</div>';
  $('#app').innerHTML = `
    ${card('日志检索', `
      <div class="row">
        <select id="log-source">${LOG_SOURCE_OPTIONS.map(([value, label]) =>
          `<option value="${value}" ${value === params.source ? 'selected' : ''}>${label}</option>`).join('')}</select>
        <input type="text" id="log-q" placeholder="关键词搜索" value="${esc(params.q)}" style="max-width:280px">
        <input type="text" id="log-kind" placeholder="类型筛选" value="${esc(params.kind)}" style="max-width:160px">
        <button class="btn primary" id="log-search">搜索</button>
        <span class="hint">${items.length} 条 · 按时间倒序 · 点一行看详情</span>
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
    ${card('联系人库', `<div class="big">${num(info.contacts)}</div><div class="muted">${num(info.messages)} 条消息 · ${num(info.memories)} 条记忆 · ${num(info.claims)} 条事实</div>`)}
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
      <td>${memory.name ? `<span class="mono hint">${esc(memory.name)}</span><br>${esc(fmtBytes(memory.bytes))} · ${num(memory.messages)} 消息 / ${num(memory.memories)} 记忆 / ${num(memory.claims)} 条事实` : '<span class="hint">—</span>'}</td>
      <td>${persona.name ? `<span class="mono hint">${esc(persona.name)}</span><br>${esc(fmtBytes(persona.bytes))} · ${num(persona.rows)} 行（人格/状态）` : '<span class="hint">—</span>'}</td>
      <td>${row.legacy ? `<span class="badge orange">legacy</span><br><span class="mono hint">${esc(row.legacy.name)}</span><br>${esc(fmtBytes(row.legacy.bytes))}<br><span class="hint">可删除/可回滚</span>` : '<span class="hint">无</span>'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">还没有联系人库</td></tr>';
  const splitCard = card('库文件（记忆库 / 人格库）', `
    <p class="hint">记忆库存对话/记忆/事实，人格库存人格/状态；
      未迁移旧库显示为单库，迁移后旧库改名 <code>*.legacy.sqlite3</code>（可删除/可回滚，不计入统计）。</p>
    <div style="overflow:auto"><table><thead><tr><th>联系人</th><th>记忆库（对话/记忆）</th><th>人格库（人格/状态）</th><th>旧版库</th></tr></thead>
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
const MEMORY_STATUS_OPTIONS = [['active', '生效中'], ['dormant', '休眠'], ['invalid', '已失效']];

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
    ${card('记忆总数', `<div class="big">${num(memMetrics.total)}</div><div class="muted">生效中 ${num((memMetrics.by_status || {}).active)} · 休眠 ${num((memMetrics.by_status || {}).dormant)}</div>`)}
    ${card('休眠率', `<div class="big">${pct(memMetrics.dormant_rate)}</div><div class="muted">阈值 >40% 告警</div>`)}
    ${card('检索命中', `<div class="big">${pct((metrics && metrics.metrics && metrics.metrics.retrieval || {}).hit_rate)}</div><div class="muted">调用 ${num(((metrics || {}).metrics || {}).retrieval ? metrics.metrics.retrieval.calls : null)}</div>`)}
    ${card('当前筛选', `<div class="big">${num(total)}</div><div class="muted">条匹配</div>`)}
  </div>`;

  const rows = items.length ? items.map((item) => `<tr data-memory-id="${esc(item.id)}">
    <td><input type="checkbox" data-merge-pick="${esc(item.id)}" ${(edit.selected || []).includes(item.id) ? 'checked' : ''}></td>
    <td class="mono hint">${esc(item.id)}</td>
    <td>${badge(enumLabel('memory_kind', item.kind))}</td>
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
    <td class="num">${num(item.confidence)}</td><td>${badge(enumLabel('memory_status', item.status))}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">暂无事实</td></tr>';

  const page = Math.floor(edit.offset / edit.limit) + 1;
  const pages = Math.max(1, Math.ceil(total / edit.limit));
  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    ${summary}
    ${card('记忆列表', `
      <div class="row between" style="margin-bottom:10px">
        <div class="row">
          <input type="text" id="mem-q" placeholder="搜索内容/类型/证据" value="${esc(edit.q)}" style="max-width:260px">
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
      <div style="overflow:auto"><table><thead><tr><th></th><th>编号</th><th>类型</th><th>内容（行内编辑，≤500）</th>
        <th>重要性</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="hint" style="margin-top:8px">删除=软删除（状态改为已失效，不物理删除）；合并会保留较早的一条、另一条标为已失效。</p>`)}
    ${card('事实（只读）', `<div style="overflow:auto"><table><thead><tr><th>主体</th><th>关系</th><th>宾语</th>
      <th>置信度</th><th>状态</th></tr></thead><tbody>${claimRows}</tbody></table></div>`)}`;

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
      const confirmText = window.prompt('软删除这条记忆（状态改为已失效，不物理删除）。输入「删除记忆」确认：');
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
        <td>${badge(enumLabel('event_kind', row.kind), row.kind === 'send_blocked' ? 'red' : 'orange')}</td>
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
      <div class="row" style="gap:10px;align-items:center">
        ${status.face ? `<img class="bili-avatar" src="/api/bili/avatar?ts=${Date.now()}" alt="B站头像">` : '<div class="bili-avatar bili-avatar-empty"></div>'}
        <div>
          <div>${pill('已登录', 'ok')} <b>${esc(status.uname || '（已写入凭证）')}</b></div>
          <span class="hint">mid ${esc(status.mid ?? '—')} · 凭证 ${esc(status.credential_path || '')}</span>
        </div>
      </div>
      <button class="btn small" id="bili-relogin">重新登录</button></div>`
      : `<div class="row between">
        <div class="hint">手机 B站 → 我的 → 扫一扫。确认时选「记住我/长期」那张。</div>
        <div class="row" style="gap:8px"><button class="btn primary" id="bili-qr-btn">${qr ? '刷新二维码' : '生成二维码'}</button>
        <button class="btn small" id="bili-check">我已扫码，检查状态</button></div></div>`}
    <div id="bili-login-area" style="margin-top:10px">
      ${qr ? `<div class="bili-qr">
        <img src="${esc(qr.image)}" alt="B站登录二维码">
        <div class="hint" id="bili-login-state">${esc(login.state || '等待扫码…')}</div>
      </div>` : ''}
      ${loggedIn && status && status.credential_path ? `<div class="hint" style="margin-top:6px">登录成功会写 ${esc(status.credential_path)}（本机文件，不落仓库），插件工具立刻可用。</div>` : ''}
    </div>`);
}

const BILI_DATA_TABS = [
  ['videos', '刷到的视频'], ['digests', '每日摘要'],
  ['reports', '视频报告'], ['log', '运行日志'],
];
function biliTabsHtml() {
  const active = state.bili && state.bili.tab ? state.bili.tab : 'videos';
  return `<div class="bili-tabs">${BILI_DATA_TABS.map(([key, label]) =>
    `<button class="bili-tab ${key === active ? 'active' : ''}" data-bili-tab="${key}">${label}</button>`).join('')}</div>`;
}
function bindBiliTabs() {
  $$('[data-bili-tab]').forEach((btn) => {
    btn.onclick = () => {
      if (state.bili) state.bili.tab = btn.dataset.biliTab;
      $$('[data-bili-tab]').forEach((b) => b.classList.toggle('active', b === btn));
      loadBiliTab(btn.dataset.biliTab);
    };
  });
}
function fmtDuration(seconds) {
  const value = Number(seconds || 0);
  if (!value) return '—';
  const m = Math.floor(value / 60);
  const s = Math.round(value % 60);
  return m ? `${m}:${String(s).padStart(2, '0')}` : `${s} 秒`;
}
function biliFeedFilterHtml() {
  const current = (state.bili && state.bili.feedFilter) || 'all';
  return `<div class="bili-feed-filters">${[['all', '全部'], ['shared', '已分享'], ['skipped', '未分享']].map(([key, label]) =>
    `<button class="bili-filter ${key === current ? 'active' : ''}" data-bili-filter="${key}">${label}</button>`).join('')}</div>`;
}
async function loadBiliTab(tab) {
  const panel = $('#bili-data-panel');
  if (!panel) return;
  tab = tab || 'videos';
  panel.innerHTML = '<div class="empty">加载中…</div>';
  try {
    if (tab === 'videos') {
      if (!state.bili.feedFilter) state.bili.feedFilter = 'all';
      const d = await api('/api/bili/feed?limit=200&filter=' + encodeURIComponent(state.bili.feedFilter));
      const items = (d && d.items) || [];
      const cards = items.map((item) => {
        const shared = !!item.shared;
        const link = item.bvid ? `https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}` : (item.url || '');
        return `<div class="bili-video-card">
          <div class="bili-cover">${item.cover_url ? `<img src="${esc(item.cover_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">` : '<span class="muted small">无封面</span>'}</div>
          <div class="bili-video-main">
            <a class="bili-video-title" href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(item.title || item.bvid || '未命名视频')}</a>
            <div class="bili-video-meta">${esc(item.owner || '未知UP')} · ${fmtDuration(item.duration_s)} · ${esc(item.emotion || '未标注情绪')} · 价值 ${num(item.value)} <span class="badge ${shared ? 'green' : ''}">${shared ? '已分享' : '未分享'}</span></div>
            ${item.summary ? `<div class="bili-video-text">${esc(item.summary)}</div>` : ''}
            ${item.why ? `<div class="bili-video-why"><b>选中理由：</b>${esc(item.why)}</div>` : ''}
            ${item.my_take ? `<div class="bili-video-why"><b>我的看法：</b>${esc(item.my_take)}</div>` : ''}
            <div class="bili-video-foot"><span class="hint">${esc(item.time || fmtDateTime(item.ts))}</span>${item.share_line ? `<span class="hint">分享文案：${esc(item.share_line)}</span>` : ''}</div>
          </div>
        </div>`;
      }).join('');
      panel.innerHTML = `${biliFeedFilterHtml()}${cards ? `<div class="bili-video-list">${cards}</div>` : '<div class="empty">还没有刷到的视频记录。后台刷B站后会自动出现在这里。</div>'}`;
      $$('[data-bili-filter]').forEach((btn) => {
        btn.onclick = () => {
          state.bili.feedFilter = btn.dataset.biliFilter;
          loadBiliTab('videos');
        };
      });
    } else if (tab === 'digests') {
      const d = await api('/api/bili/digest');
      const items = (d && d.items) || [];
      panel.innerHTML = items.length ? `<div class="bili-digest-list">${items.map((item) => `<div class="bili-digest-row">
        <div class="bili-digest-date">${esc(item.date || '—')}</div>
        <div class="bili-digest-main">
          ${item.url ? `<a class="bili-digest-title" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.title || item.bvid || '未命名视频')}</a>` : `<div class="bili-digest-title">${esc(item.title || item.bvid || '未解析行')}</div>`}
          <div class="hint">${esc(item.bvid || '')}${item.owner ? ' · ' + esc(item.owner) : ''}${item.reason ? ' · ' + esc(item.reason) : ''}</div>
        </div>
      </div>`).join('')}</div>` : '<div class="empty">还没有每日摘要。插件跑完一天后会写 digest。</div>';
    } else if (tab === 'reports') {
      const d = await api('/api/bili/reports');
      const items = (d && d.items) || [];
      if (!items.length) {
        panel.innerHTML = '<div class="empty">还没有视频报告。</div>';
        return;
      }
      let selected = state.bili.fileSel.reports || items[0].name;
      if (!items.some((item) => item.name === selected)) selected = items[0].name;
      state.bili.fileSel.reports = selected;
      const detail = await api('/api/bili/report?name=' + encodeURIComponent(selected));
      panel.innerHTML = `<div class="bili-file-layout">
        <aside class="bili-file-list">${items.map((item) => `<button class="bili-file-item ${item.name === selected ? 'active' : ''}" data-bili-file="${esc(item.name)}"><span>${esc(item.name)}</span><span class="hint">${item.date ? esc(item.date) : ''}</span></button>`).join('')}</aside>
        <section class="bili-file-body"><div class="bili-file-head">${esc(selected)}</div><pre class="bili-file-text">${esc((detail && detail.text) || (detail && detail.hint) || '（读取失败）')}</pre></section>
      </div>`;
      $$('[data-bili-file]').forEach((btn) => {
        btn.onclick = async () => {
          state.bili.fileSel.reports = btn.dataset.biliFile;
          loadBiliTab('reports');
        };
      });
    } else if (tab === 'log') {
      const d = await api('/api/bili/log?tail=300');
      const lines = (d && d.lines) || [];
      panel.innerHTML = lines.length ? `<pre class="bili-log">${esc(lines.join('\n'))}</pre>` : '<div class="empty">日志为空，或插件尚未运行。</div>';
    }
  } catch (e) {
    panel.innerHTML = `<div class="error-text">读取失败：${esc(e.message)}</div>`;
  }
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
  // 注意：重建对象时必须保留轮询相关的瞬时状态，否则 polledAt 丢失
  // → 过期判断变成 Date.now()-0 > 180s → 二维码一生成就显示"已过期"（实测 bug）
  state.bili = {
    status, configState,
    qr: prev.qr || null,
    timer: null,
    polling: false,
    loginState: prev.loginState || '',
    polledAt: prev.polledAt || 0,
    loginExpired: prev.loginExpired || false,
    tab: (new URLSearchParams(location.search).get('tab') || prev.tab || 'videos'),
    fileSel: prev.fileSel || {},
    feedFilter: prev.feedFilter || 'all',
  };
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
    ${biliTabsHtml()}
    <div id="bili-data-panel" class="bili-data-panel"><div class="empty">加载中…</div></div>
    <div class="grid cols-3">
      ${card('今日状态', `<div class="kv">
        <span class="k">日期</span><span class="v">${esc(stateInfo.day || '—')}</span>
        <span class="k">已跑/已挑</span><span class="v">${num(stateInfo.count)} / ${num(stateInfo.picked)}（额度 ${num(config.perDay)}）</span>
        <span class="k">最近运行</span><span class="v">${esc(stateInfo.lastRunAt || '—')} ${stateInfo.lastWhy ? '· ' + enumLabel('bili_status', stateInfo.lastWhy, stateInfo.lastWhy) : ''}</span>
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
  const checkBtn = $('#bili-check');
  if (checkBtn) checkBtn.onclick = checkBiliLogin;
  const relogin = $('#bili-relogin');
  if (relogin) relogin.onclick = () => { state.bili.qr = null; state.bili.loginState = ''; renderBili(); };
  const save = $('#bili-save');
  if (save) save.onclick = saveBiliConfig;
  const reportFull = $('#bili-report-full');
  if (reportFull) reportFull.onclick = showBiliReport;
  if (!BILI_DATA_TABS.some(([key]) => key === state.bili.tab)) state.bili.tab = 'videos';
  bindBiliTabs();
  loadBiliTab(state.bili.tab);
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

async function checkBiliLogin() {
  try {
    const result = await api('/api/bili/login/poll');
    const node = $('#bili-login-state');
    if (result.status === 'ok') {
      toast('B站登录成功' + (result.uname ? '：' + result.uname : ''), 'ok');
      if (state.bili) state.bili.qr = null;
      await renderBili();
      return;
    }
    const text = result.status === 'scanned' ? '已扫码，请在手机上确认…'
      : result.status === 'waiting' ? '还没扫到（或者二维码已换新，请扫页面上的这张）'
      : result.status === 'expired' ? '二维码已过期，请点「刷新二维码」重新生成'
      : (result.hint || result.error || '未知状态');
    if (state.bili) state.bili.loginState = text;
    if (node) node.textContent = text;
  } catch (err) { toast('检查失败：' + (err && err.message ? err.message : err), 'err'); }
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
      const errNode = $('#bili-login-state');
      if (errNode) errNode.textContent = state.bili.loginState;
    } catch (err) {
      state.bili.loginState = '轮询失败：' + (err && err.message ? err.message : err);
      const node = $('#bili-login-state'); if (node) node.textContent = state.bili.loginState;
    }
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

/* ---------------- 推理过程（P15-B） ---------------- */
let traceCopyBuffer = [];

function traceJson(value, fallback) {
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'string') {
    try { return JSON.parse(value); } catch (err) { return value; }
  }
  return value;
}

function traceText(value) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch (err) { return String(value); }
}

function traceCopyButton(text, label) {
  const index = traceCopyBuffer.push(String(text || '')) - 1;
  return `<button class="btn small" data-trace-copy="${index}">${esc(label || '复制全文')}</button>`;
}

function tracePre(text, cls) {
  return `<pre class="trace-pre ${cls || ''}">${esc(String(text || ''))}</pre>`;
}

function traceStep(title, bodyHtml, open) {
  return `<details class="trace-step" ${open ? 'open' : ''}><summary>${esc(title)}</summary>
    <div class="trace-step-body">${bodyHtml}</div></details>`;
}

function traceKV(rows) {
  return `<div class="kv">${rows.map(([key, value]) =>
    `<span class="k">${esc(key)}</span><span class="v">${esc(value === undefined || value === null ? '—' : value)}</span>`).join('')}</div>`;
}

function traceSummaryText(value, chars) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return esc(text.length > chars ? text.slice(0, chars) + '…' : text);
}

async function renderTrace() {
  $('#app').innerHTML = '<div class="empty">加载推理过程…</div>';
  if (!state.token) {
    $('#app').innerHTML = card('推理过程', '<div class="error-text">推理过程需要 admin token：请在右上角填写。</div>');
    return;
  }
  const prev = state.trace || {};
  state.trace = {
    mode: prev.mode || '', limit: prev.limit || 50,
    auto: prev.auto || false, turns: null, stats: null,
    selected: prev.selected || null, detail: prev.detail || null,
    demo: false, error: '', timer: prev.timer || null,
  };
  await loadTrace();
}

async function loadTrace() {
  const trace = state.trace || {};
  const params = 'limit=' + encodeURIComponent(trace.limit)
    + '&mode=' + encodeURIComponent(trace.mode);
  let turns = null, stats = null, error = '';
  try { turns = await api('/api/trace/turns?' + params); }
  catch (err) { error = err.message; }
  try { stats = await api('/api/trace/stats?days=7'); }
  catch (err) { if (!error) error = err.message; }
  trace.turns = turns;
  trace.stats = stats;
  trace.error = error;
  renderTracePage();
}

function renderTracePage() {
  const trace = state.trace || {};
  const unavailable = !!(trace.error && (String(trace.error).indexOf('trace_backend') >= 0
    || String(trace.error).indexOf('501') >= 0 || String(trace.error).indexOf('尚未就绪') >= 0));
  const turns = (trace.turns && (trace.turns.items || trace.turns.turns || trace.turns.data)) || [];
  const stats = trace.stats || {};
  const detail = trace.detail || null;
  traceCopyBuffer = [];

  const statsCards = `<div class="grid cols-4">
    ${card('近 7 天条数', `<div class="big">${num(stats.total ?? stats.count)}</div><div class="muted">trace 记录</div>`)}
    ${card('平均延迟', `<div class="big">${stats.avg_latency_ms === undefined ? '—' : num(stats.avg_latency_ms) + 'ms'}</div><div class="muted">avg latency</div>`)}
    ${card('跳过率', `<div class="big">${pct(stats.skip_rate)}</div><div class="muted">skipped / total</div>`)}
    ${card('P95 延迟', `<div class="big">${stats.p95_latency_ms === undefined && stats.p95 === undefined ? '—' : num(stats.p95_latency_ms ?? stats.p95) + 'ms'}</div><div class="muted">P95</div>`)}
  </div>`;

  const listHtml = turns.length ? turns.map((item) => {
    const active = trace.selected === item.id;
    return `<div class="trace-turn ${active ? 'active' : ''}" data-trace-id="${esc(item.id)}">
      <div class="row between"><span class="hint">${item.ts ? fmtDateTime(item.ts) : '—'}</span>
        ${badge(enumLabel('trace_mode', item.mode || 'chat'), item.mode === 'proactive' ? 'purple' : 'blue')}
        ${item.skipped ? badge('跳过', 'orange') : ''}</div>
      <div class="trace-turn-user">${traceSummaryText(item.user_text, 40) || '<span class="hint">（无用户原文）</span>'}</div>
      <div class="trace-turn-reply">${traceSummaryText(item.final_reply, 40) || '<span class="hint">（无回复）</span>'}</div>
      <div class="row between"><span class="hint">延迟 ${item.latency_ms === undefined || item.latency_ms === null ? '—' : num(item.latency_ms) + 'ms'}</span>
        ${item.kind ? badge(enumLabel('event_kind', item.kind), '') : ''}</div>
    </div>`;
  }).join('') : '<div class="empty">还没有 trace 记录（trace_enabled 关着或还没跑过新 turn）</div>';

  let detailHtml = '<div class="empty">点左侧一条 turn 查看完整推理链</div>';
  if (detail) detailHtml = traceDetailHtml(detail);

  const unavailableHtml = unavailable ? `<div class="card">
    <div class="error-text">推理过程后端未就绪：${esc(trace.error)}</div>
    <p class="hint">等 w3-latency 的 task-90（tools/console/trace_api.py）落地后自动切真；现在可以先看演示数据。</p>
    <button class="btn" id="trace-demo">加载演示数据</button></div>` : '';

  $('#app').innerHTML = `
    <div class="card">
      <div class="row between">
        <div class="row">
          <span class="hint">模式</span>
          <select id="trace-mode">
            <option value="" ${trace.mode === '' ? 'selected' : ''}>全部</option>
            <option value="chat" ${trace.mode === 'chat' ? 'selected' : ''}>chat</option>
            <option value="proactive" ${trace.mode === 'proactive' ? 'selected' : ''}>proactive</option>
          </select>
          <select id="trace-limit">${[20, 50, 100].map((value) =>
            `<option value="${value}" ${trace.limit === value ? 'selected' : ''}>最近 ${value} 条</option>`).join('')}</select>
          <label class="switch"><input type="checkbox" id="trace-auto" ${trace.auto ? 'checked' : ''}> 自动刷新 5s</label>
          <button class="btn small" id="trace-refresh">刷新</button>
        </div>
        <div class="row">
          ${trace.demo ? badge('演示数据', 'orange') : ''}
          ${pill('含用户原文/模型输出，仅本机', 'warn')}
        </div>
      </div>
      ${unavailableHtml}
      ${trace.error && !unavailable ? `<div class="error-text" style="margin-top:8px">${esc(trace.error)}</div>` : ''}
    </div>
    ${statsCards}
    <div class="trace-layout">
      <div class="card trace-list">${listHtml}</div>
      <div class="card trace-detail">${detailHtml}</div>
    </div>`;

  const mode = $('#trace-mode');
  if (mode) mode.onchange = () => { state.trace.mode = mode.value; state.trace.selected = null; state.trace.detail = null; loadTrace(); };
  const limit = $('#trace-limit');
  if (limit) limit.onchange = () => { state.trace.limit = Number(limit.value); loadTrace(); };
  const refresh = $('#trace-refresh');
  if (refresh) refresh.onclick = () => loadTrace();
  const auto = $('#trace-auto');
  if (auto) auto.onchange = () => {
    state.trace.auto = auto.checked;
    if (state.trace.timer) { clearInterval(state.trace.timer); state.trace.timer = null; }
    if (state.trace.auto) state.trace.timer = setInterval(() => {
      if (state.page !== 'trace') return;
      if (!state.trace.auto) return;
      loadTrace();
    }, 5000);
  };
  const demoBtn = $('#trace-demo');
  if (demoBtn) demoBtn.onclick = loadTraceDemo;
  $$('[data-trace-id]').forEach((node) => {
    node.onclick = () => loadTraceTurn(node.dataset.traceId);
  });
  $$('[data-trace-copy]').forEach((button) => {
    button.onclick = () => copyTraceText(traceCopyBuffer[Number(button.dataset.traceCopy)] || '');
  });
}

async function loadTraceTurn(id) {
  if (!id) return;
  state.trace.selected = id;
  if (state.trace.demo) {
    state.trace.detail = ((state.trace.details || {})[id]) || { id, error: '演示数据里没有这条' };
    renderTracePage();
    return;
  }
  try {
    const detail = await api('/api/trace/turn?id=' + encodeURIComponent(id));
    state.trace.detail = (detail && (detail.item || detail.turn || detail.data)) || detail;
  } catch (err) {
    toast(err.message, 'err');
    state.trace.detail = { id, error: err.message };
  }
  renderTracePage();
}

function traceDetailHtml(raw) {
  if (raw && raw.error) return `<div class="error-text">${esc(raw.error)}</div>`;
  const detail = raw || {};
  const memories = traceJson(detail.memories ?? detail.memories_json, []);
  const inner = traceJson(detail.inner ?? detail.inner_json, {});
  const mood = traceJson(detail.mood ?? detail.mood_json, null);
  const generation = traceJson(detail.generation ?? detail.generation_json, {});
  const polish = traceJson(detail.polish ?? detail.polish_json, {});
  const guard = traceJson(detail.guard ?? detail.guard_json, {});
  const proactive = traceJson(detail.proactive ?? detail.proactive_json, null);
  const userText = detail.user_text || '';
  const systemPrompt = detail.system_prompt || '';
  const promptUser = detail.prompt_user || '';
  const reasoning = generation.reasoning || generation.reasoning_content || '';
  const rawText = generation.raw_text || generation.raw_output || '';

  const contextBody = `
    <div class="trace-label">用户原文</div>${tracePre(userText || '（空）')}
    ${systemPrompt ? `<details class="trace-sub"><summary>system prompt（${systemPrompt.length} 字）</summary>
      <div class="row" style="justify-content:flex-end">${traceCopyButton(systemPrompt)}</div>${tracePre(systemPrompt)}</details>` : ''}
    ${promptUser ? `<details class="trace-sub"><summary>user prompt（${promptUser.length} 字）</summary>
      <div class="row" style="justify-content:flex-end">${traceCopyButton(promptUser)}</div>${tracePre(promptUser)}</details>` : ''}`;

  const memoriesBody = (Array.isArray(memories) && memories.length)
    ? `<div style="overflow:auto"><table><thead><tr><th>编号</th><th>类型</th><th>内容</th><th>评分</th></tr></thead><tbody>
      ${memories.map((item) => `<tr><td class="mono hint">${esc(item.id || '')}</td><td>${badge(enumLabel('memory_kind', item.kind))}</td>
        <td>${esc(item.content || '')}</td><td class="num">${item.score === undefined ? '—' : esc(item.score)}</td></tr>`).join('')}
    </tbody></table></div>` : '<div class="hint">无（本轮没有命中记忆）</div>';

  const innerBody = `<div class="row">${badge('模式：' + enumLabel('trace_mode', inner.mode, '—'))}${badge('来源：' + esc(inner.source || '—'))}
    ${inner.injected ? badge('已注入 prompt', 'green') : badge('未注入', 'orange')}
    <span class="hint">latency ${inner.latency_ms === undefined ? '—' : num(inner.latency_ms) + 'ms'}</span></div>
    ${inner.state_json ? tracePre(traceText(traceJson(inner.state_json, inner.state_json))) : traceKV([
      ['stance', inner.stance], ['intent', inner.intent], ['monologue', inner.monologue],
      ['behavior', traceText(inner.behavior)]])}`;

  const moodBody = (mood && Object.keys(mood).length)
    ? `${traceKV([['delta', mood.delta ?? mood.mood_delta], ['reason', mood.reason],
        ['confidence', mood.confidence], ['backend', mood.backend ?? mood.source]])}
       <details class="trace-sub"><summary>原始 JSON</summary>${tracePre(traceText(mood))}</details>`
    : '<div class="hint">本轮没有心情判定记录</div>';

  const generationBody = `
    ${traceKV([['model', generation.model], ['purpose', generation.purpose],
      ['two_stage', generation.two_stage === undefined ? '—' : String(generation.two_stage)],
      ['usage', traceText(generation.usage)], ['耗时', generation.elapsed ?? generation.latency_ms],
      ['finish_reason', generation.finish_reason]])}
    ${generation.claims && generation.claims.length ? `<div class="trace-label">claims</div>${tracePre(traceText(generation.claims))}` : ''}
    ${reasoning ? `<details class="trace-sub" open><summary>reasoning 原文（${String(reasoning).length} 字）</summary>
      <div class="row" style="justify-content:flex-end">${traceCopyButton(reasoning)}</div>
      ${tracePre(reasoning, 'trace-reasoning')}</details>`
      : '<div class="hint">模型没有返回 reasoning 原文（provider 未提供或非 reasoning 模型）</div>'}
    ${rawText ? `<details class="trace-sub"><summary>raw_text 模型原始输出（${String(rawText).length} 字）</summary>
      <div class="row" style="justify-content:flex-end">${traceCopyButton(rawText)}</div>${tracePre(rawText)}</details>` : ''}`;

  const polishBody = (polish && Object.keys(polish).length)
    ? `<div class="trace-diff"><div><div class="trace-label">before</div>${tracePre(polish.before || '')}</div>
       <div><div class="trace-label">after</div>${tracePre(polish.after || '')}</div></div>
       ${(polish.reasons || []).length ? `<div class="hint">reasons: ${esc((polish.reasons || []).join(' · '))}</div>` : ''}`
    : '<div class="hint">无润色记录（未改或未启用）</div>';

  const guardBody = `${traceKV([['blocked', guard.blocked], ['regenerated', guard.regenerated],
      ['final', guard.final]])}
    ${(guard.violations || []).length ? `<div class="trace-label">violations</div>${tracePre(traceText(guard.violations))}` : '<div class="hint">没有违规项</div>'}
    ${Object.keys(guard).length ? `<details class="trace-sub"><summary>guard 原始 JSON</summary>${tracePre(traceText(guard))}</details>` : ''}`;

  const sendBody = `${traceKV([['skipped', detail.skipped], ['skip_reason', detail.skip_reason],
      ['crisis', detail.crisis], ['status', detail.status || guard.status],
      ['latency_ms', detail.latency_ms]])}
    <div class="trace-label">最终回复</div>${tracePre(detail.final_reply || '')}
    ${detail.media || (generation && generation.media) ? `<details class="trace-sub"><summary>媒体</summary>${tracePre(traceText(detail.media || generation.media))}</details>` : ''}`;

  return `<div class="trace-head">
    <div class="row between"><b class="mono">${esc(detail.id || '')}</b>
      ${badge(enumLabel('trace_mode', detail.mode || 'chat'), detail.mode === 'proactive' ? 'purple' : 'blue')}
      ${detail.skipped ? badge('跳过', 'orange') : ''}
      <span class="hint">${detail.ts ? fmtDateTime(detail.ts) : ''} · ${detail.latency_ms === undefined ? '—' : num(detail.latency_ms) + 'ms'}</span></div>
    <div class="hint">trace 含用户原文与模型输出，只在本机展示，勿外传。</div>
  </div>
  ${traceStep('① 上下文（用户原文 / system / user prompt）', contextBody, true)}
  ${traceStep('② 记忆检索', memoriesBody)}
  ${traceStep('③ 内状态', innerBody)}
  ${traceStep('④ 心情判定', moodBody)}
  ${traceStep('⑤ 生成（model/usage/reasoning/raw_text）', generationBody)}
  ${traceStep('⑥ 润色（before → after）', polishBody)}
  ${traceStep('⑦ 守卫 / 后处理', guardBody)}
  ${traceStep('⑧ 发送结果', sendBody)}
  ${proactive ? traceStep('⑨ 主动消息决策', tracePre(traceText(proactive))) : ''}`;
}

function traceDemoData() {
  const now = Date.now() / 1000;
  const turns = [
    { id: 'T-demo-001', ts: now - 120, mode: 'chat', user_text: '今天有点累，不太想说话',
      final_reply: '那就先歇会儿，不用硬撑着跟我聊', latency_ms: 812.4, skipped: false, kind: 'chat' },
    { id: 'T-demo-002', ts: now - 5400, mode: 'proactive', user_text: '',
      final_reply: '刚看到你昨天说的那家店今天排队少了', latency_ms: 1203.0, skipped: false, kind: 'proactive' },
  ];
  const details = {
    'T-demo-001': {
      id: 'T-demo-001', ts: now - 120, mode: 'chat', contact: 'demo',
      user_text: '今天有点累，不太想说话',
      system_prompt: '你是一个通过长期对话自我改进的 AI……\n[演示 system prompt，真实页面会显示最终版]',
      prompt_user: '用户：今天有点累，不太想说话\n最近记忆：…',
      memories: [{ id: 'UM-demo-1', kind: 'preference', content: '用户不喜欢被催', score: 0.83 },
                 { id: 'UM-demo-2', kind: 'fact', content: '用户最近加班多', score: 0.71 }],
      inner: { mode: 'model', source: 'local', latency_ms: 380.5, injected: true,
        stance: '有点心疼', intent: '先接住情绪，不给建议',
        monologue: '他都说不想说话了，别追问', behavior: { length: 'short', ask: false, advice: false } },
      mood: { delta: -2, reason: '听你说累，我心里有点沉', confidence: 78, backend: 'deepseek' },
      generation: { model: 'deepseek-flash', purpose: 'chat', two_stage: false,
        usage: { prompt_tokens: 1800, completion_tokens: 40 }, elapsed: 640,
        reasoning: '用户说累，先共情，不要讲道理。语气放软，短句。',
        raw_text: '那就先歇会儿，不用硬撑着跟我聊。' },
      polish: { before: '那就先歇会儿，不用硬撑着跟我聊。', after: '那就先歇会儿，不用硬撑着跟我聊',
        reasons: ['strip_period', '口语化'] },
      guard: { blocked: false, regenerated: false, violations: [], final: '那就先歇会儿，不用硬撑着跟我聊' },
      final_reply: '那就先歇会儿，不用硬撑着跟我聊', latency_ms: 812.4, skipped: false, crisis: false,
    },
    'T-demo-002': {
      id: 'T-demo-002', ts: now - 5400, mode: 'proactive', contact: 'demo',
      user_text: '',
      system_prompt: '[演示] 主动消息 prompt：…',
      prompt_user: '[演示] 最近对话与话题种子',
      memories: [{ id: 'UM-demo-3', kind: 'event', content: '用户提过想试那家店', score: 0.66 }],
      inner: { mode: 'rule', source: 'rule', latency_ms: 0, injected: false },
      mood: { delta: 1, reason: '想到能帮上他，心里轻快', confidence: 70, backend: 'deepseek' },
      generation: { model: 'deepseek-flash', purpose: 'proactive', two_stage: true,
        usage: { prompt_tokens: 2200, completion_tokens: 35 }, elapsed: 980,
        reasoning: '不要复述他说过的话；用具体信息开场，一句就够。',
        raw_text: '刚看到你昨天说的那家店今天排队少了' },
      polish: { before: '', after: '', reasons: [] },
      guard: { blocked: false, regenerated: false, violations: [], final: '刚看到你昨天说的那家店今天排队少了' },
      final_reply: '刚看到你昨天说的那家店今天排队少了', latency_ms: 1203.0, skipped: false, crisis: false,
    },
  };
  return { turns, details };
}

function loadTraceDemo() {
  const demo = traceDemoData();
  state.trace.demo = true;
  state.trace.error = '';
  state.trace.turns = { ok: true, items: demo.turns };
  state.trace.stats = { ok: true, total: 2, avg_latency_ms: 1007.7,
    skip_rate: 0, p95_latency_ms: 1203.0 };
  state.trace.details = demo.details;
  state.trace.selected = demo.turns[0].id;
  state.trace.detail = demo.details[demo.turns[0].id];
  renderTracePage();
}

async function copyTraceText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(String(text || ''));
    } else {
      const area = document.createElement('textarea');
      area.value = String(text || '');
      document.body.appendChild(area); area.select();
      document.execCommand('copy'); area.remove();
    }
    toast('已复制全文', 'ok');
  } catch (err) { toast('复制失败：' + err.message, 'err'); }
}

/* ---------------- 素材库（P17） ---------------- */
const LIBRARY_TYPES = [['sticker', '贴纸'], ['image', '图片'], ['gif', 'GIF']];
let libraryFiles = [];

function libraryTypeLabel(type) {
  const row = LIBRARY_TYPES.find(([value]) => value === type);
  return row ? row[1] : (type || '—');
}

async function renderLibrary() {
  $('#app').innerHTML = '<div class="empty">加载素材库…</div>';
  if (!state.token) {
    $('#app').innerHTML = card('素材库', '<div class="error-text">素材库需要 admin token：请在右上角填写。</div>');
    return;
  }
  const prev = state.library || {};
  state.library = {
    q: prev.q || '', type: prev.type || '', enabled: prev.enabled || '',
    limit: prev.limit || 120, offset: prev.offset || 0,
    stats: null, data: null, editing: null, timer: null,
  };
  await loadLibrary();
}

async function loadLibrary() {
  const lib = state.library || {};
  let stats = null, data = null, errors = [];
  try { stats = await api('/api/library/stats'); } catch (err) { errors.push('统计: ' + err.message); }
  const params = new URLSearchParams({
    type: lib.type || '', q: lib.q || '', enabled: lib.enabled || '',
    limit: lib.limit, offset: lib.offset,
  });
  try { data = await api('/api/library/list?' + params.toString()); }
  catch (err) { errors.push('列表: ' + err.message); }
  lib.stats = stats; lib.data = data; lib.errors = errors;
  renderLibraryPage();
}

function libraryCardHtml(asset) {
  const disabled = !asset.enabled;
  return `<div class="library-card ${disabled ? 'disabled' : ''}" data-library-id="${esc(asset.id)}">
    <div class="library-thumb">${asset.thumb
      ? `<img src="${esc(asset.thumb)}&ts=${Date.now()}" alt="${esc(asset.name)}" loading="lazy">`
      : '<div class="library-missing">文件缺失</div>'}</div>
    <div class="library-meta">
      <div class="row between"><b class="library-name">${esc(asset.name || '（未命名）')}</b>
        ${badge(libraryTypeLabel(asset.type), asset.type === 'sticker' ? 'purple' : (asset.type === 'gif' ? 'orange' : 'blue'))}</div>
      <div class="hint library-caption">${esc(asset.caption || '（未填写具体含义）')}</div>
      <div class="library-tags">${(asset.tags || []).slice(0, 3).map((tag) => `<span class="badge">${esc(tag)}</span>`).join('')}</div>
      ${disabled ? '<div class="hint" style="color:var(--warning)">已软删除（可恢复）</div>' : ''}
    </div>
    <div class="library-actions">
      <button class="btn small" data-lib-edit="${esc(asset.id)}">编辑</button>
      ${disabled ? `<button class="btn small success" data-lib-restore="${esc(asset.id)}">恢复</button>`
        : `<button class="btn small danger" data-lib-delete="${esc(asset.id)}">删除</button>`}
    </div>
  </div>`;
}

function renderLibraryPage() {
  const lib = state.library || {};
  const stats = lib.stats || {};
  const items = (lib.data && lib.data.items) || [];
  const total = (lib.data && lib.data.total) || 0;
  const byType = stats.by_type || {};
  const errors = lib.errors || [];

  const typeOptions = '<option value="">全部类型</option>' + LIBRARY_TYPES.map(([value, label]) =>
    `<option value="${value}" ${lib.type === value ? 'selected' : ''}>${label}</option>`).join('');
  const cards = items.length ? items.map(libraryCardHtml).join('') : '<div class="empty">没有匹配的素材（可新增，或清空筛选）</div>';

  $('#app').innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="grid cols-4">
      ${card('贴纸', `<div class="big">${num(byType.sticker)}</div><div class="muted">sticker</div>`)}
      ${card('图片', `<div class="big">${num(byType.image)}</div><div class="muted">image</div>`)}
      ${card('GIF', `<div class="big">${num(byType.gif)}</div><div class="muted">gif</div>`)}
      ${card('软删除', `<div class="big">${num(stats.soft_deleted)}</div><div class="muted">enabled=0，可恢复</div>`)}
    </div>
    ${card('新增素材', `
      <div class="row" style="align-items:flex-end">
        <div class="field"><label>选择图片（可多选）</label><input type="file" id="lib-files" multiple accept=".png,.jpg,.jpeg,.gif,.webp"></div>
        <div class="field"><label>或本地路径</label><input type="text" id="lib-path" placeholder="Desktop / 素材库 / 仓库 media" style="min-width:260px"></div>
      </div>
      <div class="row" style="align-items:flex-end;margin-top:8px">
        <div class="field"><label>类型（必填）</label><select id="lib-create-type">${LIBRARY_TYPES.map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select></div>
        <div class="field"><label>名称（必填，≤30）</label><input type="text" id="lib-create-name" maxlength="30" style="min-width:180px"></div>
        <div class="field"><label>标签（逗号分隔，≤10）</label><input type="text" id="lib-create-tags" style="min-width:200px"></div>
        <button class="btn primary" id="lib-create-btn">新增到素材库</button>
      </div>
      <div class="field" style="margin-top:8px"><label>具体含义（可选，≤200）：它代表什么情绪/场景</label>
        <input type="text" id="lib-create-caption" maxlength="200"></div>
      <div id="lib-create-result" style="margin-top:8px"></div>`)}
    ${card('素材库', `
      <div class="row between" style="margin-bottom:10px">
        <div class="row">
          <input type="text" id="lib-q" placeholder="搜索名称/含义/标签" value="${esc(lib.q)}" style="max-width:240px">
          <select id="lib-type">${typeOptions}</select>
          <select id="lib-enabled"><option value="">全部</option>
            <option value="1" ${lib.enabled === '1' ? 'selected' : ''}>只看启用</option>
            <option value="0" ${lib.enabled === '0' ? 'selected' : ''}>只看软删</option></select>
          <button class="btn" id="lib-search">搜索</button>
        </div>
        <span class="hint">${total} 条 · 类型：贴纸/图片/GIF；标签表达情绪/用途</span>
      </div>
      <div class="library-grid">${cards}</div>`)}`;

  const fileInput = $('#lib-files');
  if (fileInput) fileInput.onchange = () => { libraryFiles = Array.from(fileInput.files || []); };
  const createBtn = $('#lib-create-btn');
  if (createBtn) createBtn.onclick = createLibraryAsset;
  const search = $('#lib-search');
  if (search) search.onclick = () => {
    state.library.q = $('#lib-q').value.trim();
    state.library.type = $('#lib-type').value;
    state.library.enabled = $('#lib-enabled').value;
    state.library.offset = 0;
    loadLibrary();
  };
  $$('[data-lib-edit]').forEach((button) => {
    button.onclick = () => openLibraryEdit(button.dataset.libEdit);
  });
  $$('[data-lib-delete]').forEach((button) => {
    button.onclick = () => deleteLibraryAsset(button.dataset.libDelete, false);
  });
  $$('[data-lib-restore]').forEach((button) => {
    button.onclick = async () => {
      try {
        await api('/api/library/restore', { method: 'POST', body: { id: button.dataset.libRestore } });
        toast('已恢复', 'ok'); loadLibrary();
      } catch (err) { toast(err.message, 'err'); }
    };
  });
}

async function createLibraryAsset() {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const type = $('#lib-create-type').value;
  const name = $('#lib-create-name').value.trim();
  const caption = $('#lib-create-caption').value.trim();
  const tags = ($('#lib-create-tags').value || '').split(/[,，]/).map((item) => item.trim()).filter(Boolean);
  const pathValue = ($('#lib-path').value || '').trim();
  if (!name) { toast('名称必填', 'err'); return; }
  if (!type) { toast('类型必填', 'err'); return; }
  const files = libraryFiles.length ? libraryFiles : [];
  if (!files.length && !pathValue) { toast('先选图片或填本地路径', 'err'); return; }
  const results = [];
  const button = $('#lib-create-btn');
  button.disabled = true;
  try {
    if (files.length) {
      for (const file of files) {
        const data = await readFileAsDataURL(file);
        const result = await api('/api/library/create', {
          method: 'POST', body: { data, type, name, caption, tags },
        });
        results.push({ file: file.name, ...result });
      }
    } else {
      const result = await api('/api/library/create', {
        method: 'POST', body: { path: pathValue, type, name, caption, tags },
      });
      results.push({ file: pathValue, ...result });
    }
    const html = results.map((item) => `<div class="hint">${esc(item.file)}：${item.ok
      ? (item.duplicate ? '已存在（去重）' : '已新增') : ('失败 ' + esc(item.error || ''))}</div>`).join('');
    $('#lib-create-result').innerHTML = html;
    toast(`新增完成：成功 ${results.filter((item) => item.ok).length}/${results.length}`, 'ok');
    libraryFiles = [];
    loadLibrary();
  } catch (err) {
    toast(err.message, 'err');
    $('#lib-create-result').innerHTML = `<div class="error-text">${esc(err.message)}</div>`;
  } finally { button.disabled = false; }
}

function openLibraryEdit(id) {
  const items = (state.library.data && state.library.data.items) || [];
  const asset = items.find((item) => item.id === id);
  if (!asset) { toast('素材不存在（刷新列表试试）', 'err'); return; }
  $('#modal-title').textContent = '编辑素材 · ' + (asset.name || asset.id);
  $('#modal-body').innerHTML = `
    <div class="grid cols-2">
      <div class="field"><label>类型</label><select id="lib-edit-type">${LIBRARY_TYPES.map(([value, label]) =>
        `<option value="${value}" ${asset.type === value ? 'selected' : ''}>${label}</option>`).join('')}</select></div>
      <div class="field"><label>名称（≤30）</label><input type="text" id="lib-edit-name" maxlength="30" value="${esc(asset.name || '')}"></div>
    </div>
    <div class="field" style="margin-top:8px"><label>具体含义/描述（≤200）</label>
      <textarea id="lib-edit-caption" rows="3" maxlength="200">${esc(asset.caption || '')}</textarea></div>
    <div class="field" style="margin-top:8px"><label>标签（逗号分隔，≤10）</label>
      <input type="text" id="lib-edit-tags" value="${esc((asset.tags || []).join(', '))}"></div>
    <label class="switch" style="margin-top:8px"><input type="checkbox" id="lib-edit-enabled" ${asset.enabled ? 'checked' : ''}> 启用（禁用后模型素材清单里不再出现）</label>
    <div class="row" style="margin-top:10px;justify-content:flex-end">
      <button class="btn" id="lib-edit-cancel">取消</button>
      <button class="btn primary" id="lib-edit-save">保存</button>
    </div>`;
  $('#modal-mask').classList.add('open');
  $('#lib-edit-cancel').onclick = closeModal;
  $('#lib-edit-save').onclick = async () => {
    const updates = {
      type: $('#lib-edit-type').value,
      name: $('#lib-edit-name').value.trim(),
      caption: $('#lib-edit-caption').value.trim(),
      tags: ($('#lib-edit-tags').value || '').split(/[,，]/).map((item) => item.trim()).filter(Boolean),
      enabled: $('#lib-edit-enabled').checked,
    };
    try {
      await api('/api/library/update', { method: 'POST', body: { id, ...updates } });
      toast('已保存', 'ok');
      closeModal();
      loadLibrary();
    } catch (err) { toast(err.message, 'err'); }
  };
}

async function deleteLibraryAsset(id, hard) {
  const phrase = window.prompt('删除素材需要输入确认短语「删除素材」' + (hard ? '（彻底删除会把文件移到 media_trash/）' : '（默认软删除，可恢复）') + '：');
  if (phrase === null) return;
  if (!hard && window.confirm('是否同时彻底删除（移到 media_trash/，不可恢复）？\n点「取消」= 只软删除（可恢复）')) {
    hard = true;
  }
  try {
    const result = await api('/api/library/delete', {
      method: 'POST', body: { id, hard, confirm: phrase },
    });
    toast(result.mode === 'hard' ? '已彻底删除（移入 media_trash）' : '已软删除（可恢复）', 'ok');
    loadLibrary();
  } catch (err) { toast(err.message, 'err'); }
}

/* ---------------- 路由 ---------------- */

/* ============================================================
 * P39：发送模式 / 朋友圈 / 模型库管理
 * ============================================================ */
const SEND_MODE_LABELS = { auto: '自动', confirm: '草稿', off: '关闭' };

function renderSendMode(stateData) {
  const wrap = $('#send-mode');
  if (!wrap) return;
  const mode = stateData && stateData.mode ? stateData.mode : '';
  wrap.querySelectorAll('[data-send-mode]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.sendMode === mode);
  });
  const note = $('#send-mode-note');
  if (note) note.textContent = mode ? '当前：' + (SEND_MODE_LABELS[mode] || mode) : '未连接';
}

async function loadSendMode() {
  try {
    const data = await api('/api/control/send_mode');
    state.sendMode = data;
    renderSendMode(data);
  } catch (err) {
    renderSendMode({ mode: '' });
  }
}

async function setSendMode(mode) {
  if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
  const label = SEND_MODE_LABELS[mode] || mode;
  if (!confirm('把发送模式切换为「' + label + '」？\n自动=直接发；草稿=生成后等你批准；关闭=不自动回复。')) return;
  try {
    const result = await api('/api/control/send_mode', { method: 'POST', body: { mode } });
    state.sendMode = result;
    renderSendMode(result);
    toast('已切换为：' + (SEND_MODE_LABELS[result.mode] || result.mode), 'ok');
    if (result.backup) toast('已备份原配置：' + result.backup, '');
  } catch (err) { toast('切换失败：' + err.message, 'err'); }
}

function bindSendMode() {
  const wrap = $('#send-mode');
  if (!wrap) return;
  wrap.querySelectorAll('[data-send-mode]').forEach((btn) => {
    btn.onclick = () => setSendMode(btn.dataset.sendMode);
  });
}

/* ---------------- 上网记录（只读） ---------------- */
async function renderWeb() {
  $('#app').innerHTML = '<div class="empty">加载上网记录…</div>';
  if (!state.web || typeof state.web !== 'object') state.web = { q: '', items: [] };
  try {
    const d = await api('/api/web/research?limit=200&q=' + encodeURIComponent(state.web.q || ''));
    state.web.items = d.items || [];
    state.web.total = d.total || 0;
    const rows = state.web.items.map((item) => `<div class="web-row">
      <div class="web-time">${esc(item.time || fmtDateTime(item.ts))}</div>
      <div class="web-main">
        <div class="web-query">${esc(item.query || '（无查询词）')}</div>
        <div class="web-title">${item.title ? esc(item.title) : '<span class="muted">未取到标题</span>'}</div>
        ${item.snippet ? `<div class="web-snippet">${esc(item.snippet)}</div>` : ''}
        <div class="web-meta"><span class="web-domain">${esc(item.domain || '未知站点')}</span><span class="muted"> · ${esc(item.contact || '本机记录')}</span></div>
        <div class="web-url">${item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(item.url)}</a>` : '<span class="muted">无链接</span>'}</div>
      </div>
    </div>`).join('');
    $('#app').innerHTML = `
      <div class="page-head"><div><h2>上网记录</h2><p class="muted">她主动联网查过的内容，按时间倒序；只读。</p></div><button class="btn" id="web-refresh">刷新</button></div>
      <div class="web-search">
        <input id="web-q" placeholder="搜索关键词 / 标题 / 网址" value="${esc(state.web.q || '')}">
        <button class="btn primary" id="web-search">搜索</button>
        <span class="hint">共 ${num(state.web.total ?? state.web.items.length)} 条</span>
      </div>
      ${rows ? `<div class="web-list">${rows}</div>` : '<div class="empty">还没有上网记录。她遇到需要实时信息的问题时会自己查，查完记录在这里。</div>'}`;
    const search = $('#web-search');
    if (search) search.onclick = () => {
      state.web.q = $('#web-q').value.trim();
      renderWeb();
    };
    const refresh = $('#web-refresh');
    if (refresh) refresh.onclick = () => renderWeb();
    const input = $('#web-q');
    if (input) input.onkeydown = (event) => { if (event.key === 'Enter') search.click(); };
  } catch (e) {
    $('#app').innerHTML = card('上网记录', `<div class="error-text">上网记录加载失败：${esc(e.message)}</div>`);
  }
}

/* ---------------- 日记（只读） ---------------- */
async function renderDiary() {
  $('#app').innerHTML = '<div class="empty">加载日记…</div>';
  if (!state.diary) state.diary = { selected: '', list: [], detail: null, error: '' };
  let data = null;
  try {
    data = await api('/api/diary/list');
  } catch (e) {
    $('#app').innerHTML = card('日记', `<div class="error-text">日记读取失败：${esc(e.message)}</div>`);
    return;
  }
  const items = (data && data.items) || [];
  if (!items.length) {
    $('#app').innerHTML = card('日记', '<div class="empty">还没有日记，今晚 23:00 会自动写第一篇。</div>');
    return;
  }
  if (!state.diary.selected || !items.some((item) => item.date === state.diary.selected)) {
    state.diary.selected = items[0].date;
  }
  const selected = state.diary.selected;
  let detail = null;
  try {
    detail = await api('/api/diary/' + encodeURIComponent(selected));
  } catch (e) {
    detail = { ok: false, error: e.message };
  }
  const listHtml = items.map((item) => `<button class="diary-item ${item.date === selected ? 'active' : ''}" data-diary-date="${esc(item.date)}">
      <span class="diary-date">${esc(item.date)}</span>
      <span class="diary-meta">${num(item.chars)} 字</span>
      <span class="diary-preview">${esc(item.preview || '（暂无内容）')}</span>
    </button>`).join('');
  const body = detail && detail.ok
    ? `<div class="diary-body-head"><b>${esc(detail.date)}</b><span class="hint">${num(detail.chars)} 字</span></div>
       <div class="diary-text">${esc(detail.text || '')}</div>`
    : `<div class="error-text">这篇日记读取失败：${esc((detail && detail.error) || '未知错误')}</div>`;
  $('#app').innerHTML = `
    <div class="page-head"><div><h2>日记</h2><p class="muted">她每晚 23:00 自动写一篇，只读；左侧按日期查看。</p></div></div>
    <div class="diary-layout">
      <aside class="diary-list"><div class="diary-list-head">日期 <span class="muted small">${items.length} 篇</span></div>${listHtml}</aside>
      <section class="diary-reader">${body}</section>
    </div>`;
  $$('[data-diary-date]').forEach((btn) => {
    btn.onclick = () => { state.diary.selected = btn.dataset.diaryDate; renderDiary(); };
  });
}

/* ---------------- 朋友圈（802 控制台） ---------------- */
function momentsRelTime(ts) {
  const value = Number(ts || 0);
  if (!value) return '';
  const date = new Date(value * 1000);
  const now = new Date();
  const day = 86400000;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const diff = start - new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const hm = date.toTimeString().slice(0, 5);
  if (diff === 0) return '今天 ' + hm;
  if (diff === day) return '昨天 ' + hm;
  if (diff === 2 * day) return '前天 ' + hm;
  return date.toLocaleDateString('zh-CN') + ' ' + hm;
}
function momentsImageUrl(md5) {
  const token = state.token ? '&token=' + encodeURIComponent(state.token) : '';
  return '/api/moments/media?md5=' + encodeURIComponent(md5 || '') + token;
}
function momentsAnalysisHtml(analysis) {
  if (!analysis) return '';
  const list = (value) => !value ? '<span class="hint">—</span>'
    : (Array.isArray(value) ? value.map((item) => `<div class="hint">• ${esc(typeof item === 'object' ? JSON.stringify(item) : item)}</div>`).join('')
      : (typeof value === 'object' ? `<pre class="json">${esc(JSON.stringify(value, null, 2))}</pre>` : `<div>${esc(String(value))}</div>`));
  return `<div class="moments-analysis">
    ${analysis.summary ? `<div class="moments-summary">${esc(analysis.summary)}</div>` : ''}
    <div class="grid cols-2">
      <div><div class="hint">话题</div><div class="row wrap">${(analysis.topics || []).map((topic) => `<span class="badge">${esc(typeof topic === 'object' ? JSON.stringify(topic) : topic)}</span>`).join('') || '—'}</div></div>
      <div><div class="hint">心情</div><div>${esc(typeof analysis.mood === 'object' ? JSON.stringify(analysis.mood) : (analysis.mood || '—'))}</div></div>
      <div><div class="hint">社交信号</div>${list(analysis.social_signals)}</div>
      <div><div class="hint">值得注意</div>${list(analysis.notable)}</div>
      <div style="grid-column:1/-1"><div class="hint">建议</div>${list(analysis.suggestions)}</div>
    </div></div>`;
}

async function renderMoments(auto) {
  const wrap = $('#app');
  if (!auto) wrap.innerHTML = '<div class="empty">加载朋友圈…</div>';
  const prev = state.moments || {};
  const keyword = prev.keyword || '';
  const errors = [];
  let data = null, analyses = null;
  try { data = await api('/api/moments?limit=100' + (keyword ? '&keyword=' + encodeURIComponent(keyword) : '')); }
  catch (e) { errors.push('朋友圈: ' + e.message); }
  try { analyses = await api('/api/moments/analyses?limit=10'); } catch (e) { errors.push('历史分析: ' + e.message); }
  const items = (data && data.items) || [];
  const analysisItems = analyses && analyses.items ? analyses.items : [];
  const analysis = prev.analysis || (analysisItems[0] && analysisItems[0].analysis) || null;
  state.moments = { ...prev, items, analyses: analysisItems, keyword, analysis, errors };
  const cards = items.map((item) => {
    const tid = item.tid || item.id || '';
    const images = (item.images || []).map((image) => image.md5
      ? `<img src="${momentsImageUrl(image.md5)}" loading="lazy" onerror="this.style.display='none'">` : '').join('');
    return `<div class="moment-card">
      <div class="moment-head"><b>${esc(item.nickname || item.username || '未知')}</b>
        <span class="hint">${esc(momentsRelTime(item.create_time))}</span></div>
      <div class="moment-text" data-moment-open="${esc(tid)}">${esc(item.text || '（无正文）')}</div>
      ${images ? `<div class="moment-images">${images}</div>` : ''}
      <div class="moment-stats"><span>👍 ${(item.likes || []).length}</span><span>💬 ${(item.comments || []).length}</span>
        <button class="btn small" data-moment-open="${esc(tid)}">详情</button></div></div>`;
  }).join('') || '<div class="empty">还没有朋友圈缓存，点「刷新朋友圈」抓一次</div>';
  const history = (state.moments.analyses || []).map((entry) => `<div class="tree-row" data-moment-analysis="${esc(entry.name)}">
    ${esc(entry.name)} <span class="hint">${esc(entry.summary || '')}</span></div>`).join('') || '<div class="hint">暂无历史分析</div>';
  wrap.innerHTML = `${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="card">
      <div class="row wrap between">
        <div class="row"><button class="btn primary small" id="moments-refresh">刷新朋友圈</button>
          <input id="moments-keyword" placeholder="搜索关键词…" value="${esc(keyword)}" style="width:220px">
          <button class="btn small" id="moments-search">搜索</button>
          <button class="btn small" id="moments-analyze">AI 分析最近 ${Math.min(items.length, 30)} 条</button>
          ${prev.busy ? '<span class="hint">分析中，最长 180s…</span>' : ''}</div>
        <span class="hint">${items.length} 条 · 只读缓存</span>
      </div></div>
    <div class="grid cols-2 moments-grid">
      <div class="card"><h3>朋友圈</h3><div class="moments-list">${cards}</div></div>
      <div class="col-stack">
        <div class="card"><h3>AI 分析结果</h3>${analysis ? momentsAnalysisHtml(analysis) : '<div class="hint">还没分析。点上方「AI 分析」。</div>'}</div>
        <div class="card"><h3>历史分析（最近 10 次）</h3><div class="tree">${history}</div></div>
      </div>
    </div>`;
  const refresh = $('#moments-refresh');
  if (refresh) refresh.onclick = async () => {
    refresh.disabled = true; refresh.textContent = '抓取中…（最长 180s）';
    try {
      const result = await api('/api/moments/refresh', { method: 'POST', body: { limit: 50 } });
      toast('刷新完成：抓取 ' + (result.fetched || 0) + ' 条，新增 ' + (result.added || 0) + ' 条', 'ok');
      await renderMoments();
    } catch (err) { toast('刷新失败：' + err.message, 'err'); refresh.disabled = false; refresh.textContent = '刷新朋友圈'; }
  };
  const search = $('#moments-search');
  if (search) search.onclick = async () => {
    state.moments.keyword = $('#moments-keyword').value.trim();
    await renderMoments();
  };
  const analyze = $('#moments-analyze');
  if (analyze) analyze.onclick = async () => {
    if (!state.token) { toast('先在右上角填 admin token', 'err'); return; }
    state.moments.busy = true; analyze.disabled = true; analyze.textContent = '分析中…';
    try {
      const result = await api('/api/moments/analyze', { method: 'POST', body: { limit: 30 } });
      state.moments.analysis = result.analysis || result;
      state.moments.busy = false;
      toast('分析完成', 'ok');
      await renderMoments();
    } catch (err) { state.moments.busy = false; toast('分析失败：' + err.message, 'err'); analyze.disabled = false; analyze.textContent = 'AI 分析'; }
  };
  $$('[data-moment-open]').forEach((btn) => {
    btn.onclick = async () => {
      const tid = btn.dataset.momentOpen;
      if (!tid) return;
      try {
        const result = await api('/api/moments/' + encodeURIComponent(tid));
        showModal('朋友圈详情', result.item || result);
      } catch (err) { toast('详情失败：' + err.message, 'err'); }
    };
  });
  $$('[data-moment-analysis]').forEach((row) => {
    row.onclick = async () => {
      try {
        const result = await api('/api/moments/analysis/' + encodeURIComponent(row.dataset.momentAnalysis));
        state.moments.analysis = result.analysis || result;
        await renderMoments();
      } catch (err) { toast('读取历史分析失败：' + err.message, 'err'); }
    };
  });
}

/* ---------------- 模型库管理（现有模型页内嵌） ---------------- */
function modelAdminHtml(models) {
  const items = (models && models.items) || [];
  const counts = (models && models.counts) || {};
  const rows = items.map((item) => `<tr>
    <td><b>${esc(item.name)}</b></td><td>${badge(enumLabel('model_format', item.format, '?'))}</td>
    <td>${formatBytes(item.size || 0)}</td><td class="mono">${esc(item.path || '')}</td>
    <td>${esc(enumLabel('model_source', item.source || 'builtin', '内置'))}</td>
    <td><button class="btn small danger" data-model-delete="${esc(item.path || '')}">删除</button></td></tr>`).join('')
    || '<tr><td colspan="6" class="hint">E:/agent/models 下没有识别到模型（gguf/hf/adapter）</td></tr>';
  return `<div class="grid cols-2">
    <div class="field"><label>本地导入（文件夹或单文件）</label>
      <input id="model-import-path" placeholder="E:/.../模型目录 或 xxx.gguf">
      <div class="row" style="margin-top:6px">
        <input id="model-import-name" placeholder="名称（可选）" style="width:150px">
        <select id="model-import-format"><option value="auto">自动识别</option><option value="gguf">GGUF 模型</option><option value="hf">HuggingFace 模型目录</option><option value="adapter">适配器</option></select>
        <button class="btn primary small" id="model-import-btn">导入</button></div></div>
    <div class="field"><label>网址下载（GGUF 直链）</label>
      <input id="model-download-url" placeholder="https://.../model.gguf">
      <div class="row" style="margin-top:6px">
        <input id="model-download-name" placeholder="名称（可选）" style="width:150px">
        <button class="btn primary small" id="model-download-btn">开始下载</button>
        <button class="btn small" id="model-download-refresh">刷新进度</button></div></div>
  </div>
  <div class="hint">统计：GGUF 模型 ${counts.gguf || 0} 个 · HuggingFace 模型目录 ${counts.hf || 0} 个 · 适配器 ${counts.adapter || 0} 个；删除只移入回收站，不直接物理删除。</div>
  <div id="model-admin-result"></div>
  <div class="scroll-box"><table class="table"><thead><tr><th>名称</th><th>格式</th><th>大小</th><th>路径</th><th>来源</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
  <div id="model-download-status"></div>`;
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

async function refreshModelDownloads() {
  try {
    const data = await api('/api/models/download/status');
    const el = $('#model-download-status');
    if (!el) return;
    const items = data.items || [];
    el.innerHTML = items.length ? `<div class="hint">下载任务：</div>${items.map((item) => `<div class="kv">
      <span class="k">${esc(item.name || item.id)}</span><span class="v">${esc(item.status)}
      ${item.total ? Math.round((item.bytes || 0) / item.total * 100) + '%' : ''}
      ${item.error ? esc(item.error) : ''}</span></div>`).join('')}` : '';
  } catch (err) { /* 状态接口失败不打扰页面 */ }
}

function bindModelAdmin() {
  const importBtn = $('#model-import-btn');
  if (importBtn) importBtn.onclick = async () => {
    const path = $('#model-import-path').value.trim();
    if (!path) { toast('先填本地路径', 'err'); return; }
    importBtn.disabled = true;
    try {
      const result = await api('/api/models/import', { method: 'POST', body: {
        path, name: $('#model-import-name').value.trim(),
        format: $('#model-import-format').value } });
      toast('已导入：' + (result.path || ''), 'ok');
      $('#model-admin-result').innerHTML = `<div class="hint">导入成功：${esc(result.path || '')}（${esc(enumLabel('model_format', result.format))}）</div>`;
      await renderModels();
    } catch (err) { toast('导入失败：' + err.message, 'err'); importBtn.disabled = false; }
  };
  const downloadBtn = $('#model-download-btn');
  if (downloadBtn) downloadBtn.onclick = async () => {
    const url = $('#model-download-url').value.trim();
    if (!url) { toast('先填 URL', 'err'); return; }
    downloadBtn.disabled = true;
    try {
      const result = await api('/api/models/download', { method: 'POST', body: {
        url, name: $('#model-download-name').value.trim() } });
      toast('下载已开始：' + (result.name || ''), 'ok');
      $('#model-download-url').value = '';
      $('#model-admin-result').innerHTML = `<div class="hint">下载中：${esc(result.name || '')} → ${esc(result.path || '')}</div>`;
      setTimeout(refreshModelDownloads, 1000);
    } catch (err) { toast('下载失败：' + err.message, 'err'); downloadBtn.disabled = false; }
  };
  const refresh = $('#model-download-refresh');
  if (refresh) refresh.onclick = refreshModelDownloads;
  $$('[data-model-delete]').forEach((btn) => {
    btn.onclick = async () => {
      const path = btn.dataset.modelDelete;
      if (!confirm('删除模型（移到 models/.trash，不物理删）？\n' + path)) return;
      try {
        const result = await api('/api/models/delete', { method: 'POST', body: { path } });
        toast('已移入回收站：' + (result.moved_to || ''), 'ok');
        await renderModels();
      } catch (err) { toast('删除失败：' + err.message, 'err'); }
    };
  });
  refreshModelDownloads();
}

const RENDERERS = {
  overview: renderOverview,
  feed: renderFeed,
  trace: renderTrace,
  chat: renderChatTest,
  inner: renderInner,
  media: renderMedia,
  library: renderLibrary,
  memory: renderMemory,
  data: renderData,
  knowledge: renderMemoryEdit,
  prompts: renderPrompts,
  models: renderModels,
  stats: renderStats,
  providers: renderProviders,
  logs: renderLogs,
  bili: renderBili,
  moments: renderMoments,
  diary: renderDiary,
  web: renderWeb,
  settings: renderSettings,
};

async function go(page, auto) {
  if (!PAGES[page]) page = 'overview';
  if (page !== 'bili' && state.bili && state.bili.timer) {
    clearInterval(state.bili.timer); state.bili.timer = null;
  }
  if (page !== 'trace' && state.trace && state.trace.timer) {
    clearInterval(state.trace.timer); state.trace.timer = null;
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
        await loadSendMode();
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
      loadSendMode();
      go(state.page);
    }
  }).catch(() => {});
  state.timer = setInterval(() => {
    if (document.hidden) return;
    if (state.page === 'feed' && state.feedAuto) renderFeed(true).then(setLastRefresh).catch(() => {});
  }, 3000);
}

bindGlobal();
bindSendMode();
loadSendMode();
go(location.hash.replace('#', '') || 'overview');
