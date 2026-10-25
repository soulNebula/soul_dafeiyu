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
  refreshing: false,
  prompts: null,
  promptKey: null,
  config: null,
  models: null,
  memory: null,
  status: null,
  inner: null,
  tone: null,
};

const PAGES = {
  overview: '总览',
  feed: '对话流',
  inner: '内状态',
  memory: '记忆',
  prompts: '提示词',
  models: '模型',
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
  if (opts.method === 'POST') {
    headers['Content-Type'] = 'application/json';
    if (state.token) headers['X-Admin-Token'] = state.token;
  }
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

function renderFeed(items) {
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

async function renderFeed(auto) {
  if (!auto) $('#app').innerHTML = '<div class="empty">加载对话流…</div>';
  let feed = null;
  const errors = [];
  try { feed = await api('/api/feed?limit=200&kind=' + encodeURIComponent(state.feedKind)); }
  catch (e) { errors.push(e.message); }
  const html = `
    ${errors.length ? `<div class="card"><div class="error-text">${esc(errors.join('；'))}</div></div>` : ''}
    <div class="card">
      <div class="row between">
        <div class="row">
          <select id="feed-kind">${FEED_KINDS.map(([value, label]) =>
            `<option value="${value}" ${value === state.feedKind ? 'selected' : ''}>${label}</option>`).join('')}</select>
          <label class="switch"><input type="checkbox" id="feed-auto" ${state.feedAuto ? 'checked' : ''}> 自动刷新 3s</label>
        </div>
        <div class="refresh-bar">${feed ? (feed.items || []).length : 0} 条 · 文件 ${esc((feed && feed.source) || '')}</div>
      </div>
      <div style="margin-top:12px">${renderFeed((feed && feed.items) || [])}</div>
    </div>`;
  $('#app').innerHTML = html;
  const select = $('#feed-kind');
  if (select) select.onchange = () => { state.feedKind = select.value; renderFeed(false); };
  const autoBox = $('#feed-auto');
  if (autoBox) autoBox.onchange = () => { state.feedAuto = autoBox.checked; };
}

/* ---------------- 内状态 ---------------- */
async function renderInner() {
  $('#app').innerHTML = '<div class="empty">加载内状态…</div>';
  let data = null;
  try { data = await api('/api/inner?limit=100'); }
  catch (e) {
    $('#app').innerHTML = card('内状态', `<div class="error-text">${esc(e.message)}</div>`);
    return;
  }
  state.inner = data;
  if (!data.ok && !data.items.length) {
    $('#app').innerHTML = card('内状态', `<div class="error-text">${esc(data.error || '读取失败')}</div>`);
    return;
  }
  const items = data.items || [];
  const cards = items.map((item) => innerCard(item)).join('');
  $('#app').innerHTML = `
    <div class="card">
      <div class="row between"><div class="hint">来源：${esc(data.db || '')} · 只读</div>
        <span class="hint">“注入”= 这条 inner block 进入了 prompt；shadow 模式只记录不注入。</span></div>
    </div>
    <div class="grid cols-2">${cards || '<div class="empty">还没有 inner_states（INNER_MODE 未开或还没新消息）</div>'}</div>`;
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

/* ---------------- 路由 ---------------- */
const RENDERERS = {
  overview: renderOverview,
  feed: renderFeed,
  inner: renderInner,
  memory: renderMemory,
  prompts: renderPrompts,
  models: renderModels,
  settings: renderSettings,
};

async function go(page, auto) {
  if (!PAGES[page]) page = 'overview';
  state.page = page;
  $('#page-title').textContent = PAGES[page];
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
  if (location.hash !== '#' + page && !auto) history.replaceState(null, '', '#' + page);
  try {
    await RENDERERS[page](auto);
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
  tokenInput.oninput = () => {
    state.token = tokenInput.value.trim();
    sessionStorage.setItem('console_token', state.token);
  };
  document.addEventListener('click', (event) => {
    const node = event.target.closest('[data-json]');
    if (!node) return;
    try { showModal('事件详情', JSON.parse(node.dataset.json)); } catch (err) { toast('JSON 解析失败', 'err'); }
  });
  state.timer = setInterval(() => {
    if (document.hidden) return;
    if (state.page === 'feed' && state.feedAuto) renderFeed(true).then(setLastRefresh).catch(() => {});
    else if (state.page === 'overview') renderOverview().then(setLastRefresh).catch(() => {});
  }, 3000);
}

bindGlobal();
go(location.hash.replace('#', '') || 'overview');
