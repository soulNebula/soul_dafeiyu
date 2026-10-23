/* =============================================================================
 * WXBOX · 消息分析台 —— 前端主脚本
 *
 * 纯原生 ES2020，无任何依赖 / 构建步骤，浏览器直接加载。
 * 结构：
 *   0. 常量与工具          1. 状态与 DOM
 *   2. 安全渲染（转义 / Markdown / 媒体 URL）
 *   3. HTTP API 与 SSE
 *   4. 顶栏 / 好友 / 消息渲染
 *   5. 朋友圈渲染
 *   6. AI 分析（流式）
 *   7. 模型设置弹窗
 *   8. 日志 / 灯箱 / 提示 / 抽屉
 *   9. 事件绑定与启动
 *  10. 演示模式（?mock=1，仅自测与界面预览用，不参与真实数据）
 *
 * 自测用查询参数（生产环境无副作用）：
 *   ?mock=1     内置样例数据渲染（不请求后端）
 *   ?tab=moments 打开朋友圈页签
 *   ?settings=1 启动后直接打开设置弹窗（截图用）
 *   ?ai=1       启动后展开 AI 抽屉（窄屏截图用）
 *   ?logs=1     启动后展开日志抽屉（截图用）
 *   ?live=1     mock 模式下每 3 秒追加一条演示消息
 * ========================================================================== */
'use strict';

(function () {
  // ===========================================================================
  // 0. 常量
  // ===========================================================================
  const MAX_CARDS = 400;        // 消息流最多渲染的卡片数（契约要求）
  const MAX_MESSAGES = 3000;    // 内存中保留的消息上限
  const MAX_MOMENTS = 30;       // 朋友圈最多渲染 / 保留 30 条（v2）
  const MAX_LOGS = 500;         // 日志最多 500 行
  const MAX_HISTORY = 12;       // 分析上下文最多 12 条（6 轮）
  const ANALYSIS_LIMIT = 80;    // 每次取最近 N 条消息 / 朋友圈

  const KIND_LABELS = {
    text: '文字',
    image: '图片',
    emoji: '表情包',
    file: '文件',
    voice: '语音',
    video: '视频',
    system: '系统',
    redpacket: '红包',
    transfer: '转账',
    unknown: '未知',
  };

  const DEFAULT_MODELS = [
    {
      id: 'deepseek-flash',
      label: 'DeepSeek V4.1 Flash 1017',
      description: '1M 上下文 · 支持深度思考与图像理解',
      base_url: 'https://api.deepseek.com',
    },
  ];

  const QUICK_PROMPTS = {
    messages: [
      { label: '摘要', question: '请总结最近的聊天内容，用 3~5 条要点概括，并标注说话人。' },
      { label: '待办', question: '请从最近的聊天记录中提取需要我处理的事项，按优先级列出待办清单。' },
      { label: '情绪', question: '请分析最近对话中对方的情绪状态和语气变化，并说明判断依据。' },
      { label: '风险', question: '请找出最近对话中的风险点、敏感话题和可能的误解，并给出应对建议。' },
      { label: '关键结论', question: '请提炼最近对话中的关键结论、已经确定的事项和需要进一步确认的内容。' },
    ],
    moments: [
      { label: '摘要', question: '请总结这些朋友圈内容的整体主题和值得关注的动态。' },
      { label: '人物关系', question: '请根据这些朋友圈的作者、点赞和评论互动，分析作者之间可能的人物关系。' },
      { label: '情绪', question: '请分析这些朋友圈传达的情绪倾向和可能的生活状态，并说明依据。' },
      { label: '话题', question: '请归纳这些朋友圈里反复出现的话题、兴趣点和关注方向。' },
      { label: '风险', question: '请指出这些朋友圈中可能涉及的风险（隐私泄露、敏感信息、负面情绪等）并给出建议。' },
    ],
  };

  // 内联 SVG 图标（无外部图标资源）
  const ICONS = {
    chat:
      '<path d="M4.5 5.5h15A1.5 1.5 0 0 1 21 7v8.5a1.5 1.5 0 0 1-1.5 1.5h-7L8 20.5V17H4.5A1.5 1.5 0 0 1 3 15.5V7a1.5 1.5 0 0 1 1.5-1.5Z"/>' +
      '<circle cx="8.4" cy="11.2" r="1" fill="currentColor" stroke="none"/>' +
      '<circle cx="12" cy="11.2" r="1" fill="currentColor" stroke="none"/>' +
      '<circle cx="15.6" cy="11.2" r="1" fill="currentColor" stroke="none"/>',
    camera:
      '<rect x="3" y="7" width="18" height="13" rx="2.5"/><circle cx="12" cy="13.5" r="3.6"/>' +
      '<path d="M8.4 7 9.7 4.8h4.6L15.6 7"/>',
    users:
      '<circle cx="9" cy="8" r="3.2"/><path d="M3.6 19c.6-3 2.8-4.6 5.4-4.6s4.8 1.6 5.4 4.6"/>' +
      '<path d="M15.4 5.5a3 3 0 0 1 0 5.6"/><path d="M17.2 14.7c1.8.5 3 2 3.3 4.3"/>',
    sparkles:
      '<path d="M12 3.5 13.5 8 18 9.5 13.5 11 12 15.5 10.5 11 6 9.5 10.5 8Z"/>' +
      '<path d="m18.6 14.6.7 2.1 2.2.8-2.2.8-.7 2.1-.8-2.1-2.1-.8 2.1-.8Z"/>',
    gear:
      '<circle cx="12" cy="12" r="3"/><path d="M19.4 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.6 1.6 0 0 0-1-1.4 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.6 1.6 0 0 0 1.4-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.6 1.6 0 0 0 1 1.4 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.2a1.6 1.6 0 0 0-1.4 1Z"/>',
    power: '<path d="M12 3.5v8"/><path d="M6.6 6.4a7.5 7.5 0 1 0 10.8 0"/>',
    terminal:
      '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="m7 9 3 3-3 3"/><path d="M12.5 15H17"/>',
    refresh:
      '<path d="M20.5 4.5v5h-5"/><path d="M20.2 9.5A8.2 8.2 0 1 0 20.6 14"/>',
    search: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
    'arrow-down': '<path d="M12 4v14"/><path d="m5.5 12.5 6.5 6.5 6.5-6.5"/>',
    alert:
      '<path d="M12 3.5 21 19.5H3Z"/><path d="M12 9v4.5"/>' +
      '<circle cx="12" cy="16.7" r=".9" fill="currentColor" stroke="none"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    plug:
      '<path d="M9 3v5"/><path d="M15 3v5"/><path d="M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0Z"/><path d="M12 17v4"/>',
    'chevron-up': '<path d="m6 14.5 6-6 6 6"/>',
    download: '<path d="M12 4v11"/><path d="m7.5 11 4.5 4.5L16.5 11"/><path d="M4.5 19.5h15"/>',
    file: '<path d="M6 3.5h7l5 5v12H6Z"/><path d="M13 3.5V9h5"/><path d="M9 13h6"/><path d="M9 16.5h4"/>',
    image:
      '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/>' +
      '<path d="m4.5 17.5 5-5.5 3.5 3.5 2.5-2.5 4 4.5"/>',
    mic:
      '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/><path d="M12 18v3"/>',
    heart:
      '<path d="M12 20s-7.5-4.4-7.5-9.3A4.2 4.2 0 0 1 12 8a4.2 4.2 0 0 1 7.5 2.7C19.5 15.6 12 20 12 20Z" fill="currentColor" stroke="none"/>',
    comment: '<path d="M20.5 12a8.5 8.5 0 0 1-12.4 7.6L3.5 20.5l1-4.4A8.5 8.5 0 1 1 20.5 12Z"/>',
    location:
      '<path d="M12 21s6.5-5.6 6.5-10.5a6.5 6.5 0 1 0-13 0C5.5 15.4 12 21 12 21Z"/><circle cx="12" cy="10.3" r="2.3"/>',
    link:
      '<path d="M10 13.5a4 4 0 0 0 5.7 0l3-3a4 4 0 1 0-5.7-5.7L11.5 6.3"/>' +
      '<path d="M14 10.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 1 0 5.7 5.7l1.5-1.5"/>',
    video:
      '<rect x="3" y="6.5" width="12.5" height="11" rx="2.5"/><path d="M15.5 10.5 21 7.5v9l-5.5-3"/>',
    play: '<path d="M8 5.5 18.5 12 8 18.5Z" fill="currentColor" stroke="none"/>',
    check: '<path d="m5 12.5 5 5 9-11"/>',
    history:
      '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5v5h5"/><path d="M12 7.5v5l3.2 1.9"/>',
    trash:
      '<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l.8 12.5h9.4L17.5 7"/>' +
      '<path d="M10 11v5"/><path d="M14 11v5"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="8" r=".95" fill="currentColor" stroke="none"/>',
  };

  // ===========================================================================
  // 1. 环境 / 状态 / DOM
  // ===========================================================================
  const qs = new URLSearchParams(window.location.search);
  const MOCK = qs.has('mock');
  const MOCK_LIVE = MOCK && qs.has('live');
  // mock 模式下的「内存媒体」映射：模拟 /api/moments/media/<key>
  const mockMediaMap = {};

  function metaContent(name) {
    const node = document.querySelector('meta[name="' + name + '"]');
    return node && node.content ? node.content.trim() : '';
  }

  const RAW_TOKEN = metaContent('wxbox-token');
  const TOKEN = RAW_TOKEN && !/^__WXBOX/.test(RAW_TOKEN) ? RAW_TOKEN : '';
  const RAW_MODE = metaContent('wxbox-mode');
  const RAW_VERSION = metaContent('wxbox-version');

  function nowSec() {
    return Date.now() / 1000;
  }

  function newAnalysis() {
    return {
      status: 'idle', // idle | running | done | cancelled | error
      busy: false,
      jobId: '',
      content: '',
      reasoning: '',
      usage: null,
      elapsedMs: 0,
      cancelled: false,
      error: '',
      question: '',
      scopeLabel: '',
      messageCount: 0,
      imageCount: 0,
      modelLabel: '',
      startedAt: 0,
    };
  }

  function defaultLlm() {
    return {
      configured: false,
      model: 'deepseek-flash',
      model_label: 'DeepSeek V4.1 Flash 1017',
      base_url: 'https://api.deepseek.com',
      api_key_masked: '',
      thinking: true,
      attach_images: true,
      busy: false,
      models: DEFAULT_MODELS.slice(),
    };
  }

  const S = {
    tab: 'messages',
    bootstrapped: false,
    refreshing: false,
    server: {},
    state: {},
    friends: [],
    friendMap: new Map(),
    groups: [],
    groupMap: new Map(),
    groupsLoaded: false,
    groupsLoading: false,
    groupsError: '',
    groupsSource: '',
    contactList: 'friends', // friends | groups
    contacts: {
      available: false,
      loaded: false,
      loading: false,
      error: '',
      friends: [],
      groups: [],
      initials: {},
      historyNames: new Set(),
      friendCount: 0,
      groupCount: 0,
      accountWxid: '',
      account: {},
    },
    contactsRefreshing: false,
    contactsCleared: false,
    mockAltAccount: false,
    videoAuto: {
      enabled: false,
      available: true,
      busy: false,
      known: false,
      lastResult: '',
      lastError: '',
      reason: '',
    },
    messages: [],
    msgIndex: new Map(),
    logs: [],
    logSeen: new Set(),
    monitored: new Set(),
    activeFriend: null,
    kind: 'all',
    friendQuery: '',
    groupQuery: '',
    msgQuery: '',
    autoScroll: true,
    paused: false,
    unseen: 0,
    lastRenderedDay: '',
    needsFullRender: false,
    llm: defaultLlm(),
    scope: 'all',
    analysis: newAnalysis(),
    analysisStick: true,
    history: [],
    sse: MOCK ? 'open' : 'connecting',
    logOpen: false,
    settings: { open: false, saving: false, testing: false, clearKey: false },
    moments: {
      status: {},
      items: [],
      count: 0,
      syncedAt: 0,
      account: {},
      loaded: false,
      loading: false,
      syncing: false,
      clearing: false,
      auto: true,          // v8：自动同步默认开启（status.auto 缺失时也按开启显示）
      autoInterval: 30,
      autoSaving: false,
      error: '',
      authorQuery: '',
      author: '',
      query: '',
      onlyImages: false,
      onlyComments: false,
      freshnessDismissed: false,
      syncLimit: 10,      // 用户选择的同步条数（5 / 10 / 20）
      lastLimit: 0,       // 最近一次同步实际使用的条数
      syncLimitTouched: false,
      mockAll: [],
    },
    activeMomentId: '',
    historyLoading: false,
    localClearInFlight: false,
  };

  const dom = {};
  [
    'app', 'statusChip', 'statusDot', 'statusText', 'liveChip', 'liveDot', 'liveText',
    'topChatBox', 'topChat', 'topMonitorBox', 'topMonitoring', 'topNav', 'navMsgCount',
    'navMomentsCount', 'modeBadge', 'modeDot', 'modeText', 'friendsToggle', 'aiToggle',
    'listenActions', 'startListenBtn', 'stopListenBtn',
    'backendToggle', 'logToggle', 'settingsBtn', 'connBanner', 'connBannerText', 'workspace',
    'colLeft', 'friendsPanel', 'friendsTotal', 'selectAllBtn', 'clearAllBtn', 'syncFriendsBtn',
    'contactTabs', 'groupsTotal', 'friendSearchBox', 'friendSearch', 'friendSearchClear',
    'monitorSummary', 'contactsHint', 'contactsRefreshBtn', 'refreshContactsBtn',
    'clearContactsBtn', 'contactsAccount', 'contactsAccountName', 'friendList',
    'followNote', 'friendsFoot', 'momentsSide', 'momentsSideCount', 'momentsAccount', 'momentsAccountAvatar',
    'momentsAccountName', 'momentsAccountMeta', 'momentsSideSyncBtn', 'momentsOnlyImages',
    'momentsLimitSelect', 'momentsClearBtn', 'momentsOnlyComments', 'momentAuthorSearch',
    'momentsAutoRow', 'momentsAutoToggle', 'momentsAutoStatus',
    'videoAutoToggle',
    'momentsAuthorCount', 'momentAuthors',
    'momentsSideFoot', 'colCenter', 'chatMain', 'chatAvatar', 'chatTitle', 'chatSub',
    'chatGroupBadge',
    'syncHistoryBtn', 'clearChatBtn', 'historyPop', 'historyPopFriend', 'historyPopOptions',
    'autoScrollToggle', 'kindFilters', 'messageSearch', 'messageSearchClear', 'messageScroll',
    'messageList', 'messageEmpty', 'messageEmptyIcon', 'messageEmptyTitle', 'messageEmptyDesc',
    'jumpLatest', 'jumpLatestCount', 'momentsMain', 'momentsSub', 'momentsSyncBtn',
    'momentAuthorChip', 'momentAuthorName', 'momentSearch', 'momentSearchClear',
    'momentsSyncStrip', 'momentsSyncText', 'momentsScroll', 'momentList', 'momentsSkeleton',
    'momentsState', 'momentsStateIcon', 'momentsStateTitle', 'momentsStateDesc',
    'momentsStateActions', 'aiPanel', 'closeAiBtn', 'aiScroll', 'modelRow', 'modelDot',
    'modelName', 'modelSub', 'configureModelBtn', 'scopeChips', 'quickChips', 'aiQuestion',
    'aiQuestionCount', 'optThinking', 'optImages', 'aiLimit', 'analyzeBtn', 'stopBtn',
    'aiNotConfigured', 'aiConfigureBtn', 'analysis', 'analysisStatus', 'analysisScope',
    'analysisMeta', 'reasoningBox', 'reasoningLen', 'reasoningBody', 'analysisContent',
    'analysisUsage', 'analysisElapsed', 'copyAnalysis', 'clearAnalysis', 'aiError',
    'aiErrorTitle', 'aiErrorText', 'logDrawer', 'logHead', 'logCount', 'logClear',
    'logChevron', 'logBody', 'scrim', 'settingsModal', 'settingsDialog', 'settingsTitle',
    'settingsClose', 'apiKeyInput', 'toggleKeyVisible', 'apiKeyHint', 'clearKeyBtn',
    'modelSelect', 'modelHint', 'cfgThinking', 'cfgImages', 'advancedBox', 'baseUrlInput',
    'testResult', 'settingsFormError', 'testConnBtn', 'settingsCancel', 'settingsSave',
    'lightbox', 'lightboxClose', 'lightboxImg', 'lightboxCaption', 'toastStack',
  ].forEach(function (id) {
    dom[id] = document.getElementById(id);
  });

  // ===========================================================================
  // 2. 工具与安全渲染
  // ===========================================================================
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svg(name, size) {
    const icon = ICONS[name] || '';
    const s = size || 16;
    return (
      '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      icon +
      '</svg>'
    );
  }

  function iconEl(name, size) {
    const span = el('span', 'icon');
    span.innerHTML = svg(name, size);
    return span;
  }

  function mountIcons(root) {
    (root || document).querySelectorAll('[data-icon]').forEach(function (node) {
      const name = node.getAttribute('data-icon');
      if (ICONS[name] && !node.firstChild) node.innerHTML = svg(name, 16);
    });
  }

  function debounce(fn, ms) {
    let timer = null;
    return function () {
      const args = arguments;
      const self = this;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(self, args);
      }, ms);
    };
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function safeHttpUrl(url) {
    const text = String(url || '').trim();
    return /^https?:\/\//i.test(text) ? text : '';
  }

  function formatInt(num) {
    return String(Math.round(Number(num) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function formatCount(num) {
    const n = Number(num);
    if (!isFinite(n) || n <= 0) return '0';
    if (n < 10000) return String(Math.round(n));
    if (n < 100000000) {
      const w = n / 10000;
      return (w < 10 ? w.toFixed(1) : String(Math.round(w))) + '万';
    }
    return (n / 100000000).toFixed(1) + '亿';
  }

  function pad2(n) {
    return n < 10 ? '0' + n : String(n);
  }

  function formatClock(ts) {
    const d = new Date(Number(ts) * 1000);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function formatTime(ts) {
    const d = new Date(Number(ts) * 1000);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  }

  function isToday(ts) {
    return startOfDay(new Date(Number(ts) * 1000)) === startOfDay(new Date());
  }

  function isYesterday(ts) {
    const d = new Date(Number(ts) * 1000);
    const y = new Date();
    y.setDate(y.getDate() - 1);
    return startOfDay(d) === startOfDay(y);
  }

  function dayKey(ts) {
    const d = new Date(Number(ts) * 1000);
    return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate();
  }

  function dayLabel(ts) {
    if (isToday(ts)) return '今天';
    if (isYesterday(ts)) return '昨天';
    const d = new Date(Number(ts) * 1000);
    const y = new Date().getFullYear();
    const base = d.getMonth() + 1 + '月' + d.getDate() + '日';
    return d.getFullYear() === y ? base : d.getFullYear() + '年' + base;
  }

  function relTime(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    const diff = nowSec() - t;
    if (diff < 45) return '刚刚';
    if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + ' 分钟前';
    if (isToday(t)) return formatClock(t);
    if (isYesterday(t)) return '昨天 ' + formatClock(t);
    const d = new Date(t * 1000);
    if (nowSec() - t < 86400 * 7) {
      return '周' + '日一二三四五六'.charAt(d.getDay()) + ' ' + formatClock(t);
    }
    return d.getMonth() + 1 + '-' + d.getDate();
  }

  function friendTimeText(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    if (isToday(t)) return formatClock(t);
    if (isYesterday(t)) return '昨天';
    const d = new Date(t * 1000);
    return d.getMonth() + 1 + '-' + d.getDate();
  }

  function formatMomentTime(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    if (isToday(t)) return '今天 ' + formatClock(t);
    if (isYesterday(t)) return '昨天 ' + formatClock(t);
    const d = new Date(t * 1000);
    const base = d.getMonth() + 1 + '-' + d.getDate() + ' ' + formatClock(t);
    return d.getFullYear() === new Date().getFullYear() ? base : d.getFullYear() + '-' + base;
  }

  /**
   * 朋友圈时间显示（优先 create_time）：
   * 今年 → MM-DD HH:MM；非今年 → YYYY-MM-DD HH:MM。
   * 避免后端 time_text 只有 MM-DD 时，去年的动态被误读成「10 个月前」。
   */
  function formatMomentDisplay(ts) {
    const t = Number(ts) || 0;
    if (!t) return '';
    const d = new Date(t * 1000);
    const base = pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + formatClock(t);
    return d.getFullYear() === new Date().getFullYear() ? base : d.getFullYear() + '-' + base;
  }

  function momentTimeLabel(item) {
    if (!item) return '';
    const ts = Number(item.create_time) || 0;
    if (ts) return formatMomentDisplay(ts);
    return item.time_text ? String(item.time_text) : '';
  }

  function formatFullTime(ts) {
    const t = Number(ts) || 0;
    if (!t) return '—';
    const d = new Date(t * 1000);
    return (
      d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + formatClock(t)
    );
  }

  function formatDuration(value) {
    if (value === undefined || value === null || value === '') return '';
    const text = String(value).trim();
    if (!text) return '';
    if (text.endsWith('秒')) return text;
    if (/s$/i.test(text)) return text.slice(0, -1).trim() + ' 秒';
    const seconds = Number(text);
    if (!isFinite(seconds)) return text;
    return (seconds === Math.round(seconds) ? String(Math.round(seconds)) : String(seconds)) + ' 秒';
  }

  function formatVideoDuration(value) {
    const sec = Math.max(0, Math.round(Number(value) || 0));
    if (!sec) return '';
    return pad2(Math.floor(sec / 60)) + ':' + pad2(sec % 60);
  }

  function formatElapsed(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + ' 秒';
    return Math.floor(s / 60) + ' 分 ' + Math.round(s % 60) + ' 秒';
  }

  function truncate(text, limit) {
    const str = String(text || '');
    if (str.length <= limit) return str;
    return str.slice(0, Math.max(1, limit - 1)) + '…';
  }

  function hashHue(text) {
    let hash = 0;
    const str = String(text || '');
    for (let i = 0; i < str.length; i++) hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
    return hash % 360;
  }

  function firstChar(name) {
    const chars = Array.from(String(name || '').trim());
    return chars.length ? chars[0] : '?';
  }

  /**
   * v4：头像统一用「拼音首字母」。
   * 优先显式 initial（contacts.initials / group.initial / extra.sender_initial）；
   * 否则英文取首字母大写；再兜底取首字（中文由后端给拼音首字母）。
   */
  function avatarInitial(name, explicitInitial) {
    const explicit = String(explicitInitial || '').trim();
    if (explicit) {
      const ch = Array.from(explicit)[0] || '';
      return /[a-z]/.test(ch) ? ch.toUpperCase() : ch;
    }
    const chars = Array.from(String(name || '').trim());
    if (!chars.length) return '?';
    const first = chars[0];
    return /[a-z]/.test(first) ? first.toUpperCase() : first;
  }

  function contactInitial(name) {
    const key = String(name || '');
    const map = (S.contacts && S.contacts.initials) || {};
    if (map[key]) return map[key];
    const friend = S.friendMap.get(key);
    return (friend && friend.initial) || '';
  }

  function groupInitial(name) {
    const group = S.groupMap.get(String(name || ''));
    return (group && group.initial) || '';
  }

  function conversationInitial(name) {
    return contactInitial(name) || groupInitial(name);
  }

  /** v2：朋友圈媒体改为进程内存接口，key 形如 t_<feedid>_<idx> / a_<sha1>。 */
  function isMomentsMediaKey(value) {
    return /^(t_|a_)[A-Za-z0-9_-]+$/.test(String(value || ''));
  }

  function momentsMediaUrl(key) {
    const clean = String(key || '');
    // mock 模式：用内置 data URL 模拟内存媒体接口
    if (MOCK && mockMediaMap[clean]) return mockMediaMap[clean];
    return '/api/moments/media/' + encodeURIComponent(clean) + '?token=' + encodeURIComponent(TOKEN);
  }

  function mediaUrl(path) {
    const raw = String(path || '').replace(/\\/g, '/').trim();
    if (!raw) return '';
    if (/^(data:|blob:)/i.test(raw)) return raw;
    // 朋友圈 thumb_key / avatar_key（优先于旧的 path/thumb）
    if (isMomentsMediaKey(raw)) return momentsMediaUrl(raw);
    let rel = raw;
    if (rel.indexOf('/media/') === 0) rel = rel.slice(7);
    else if (rel.indexOf('media/') === 0) rel = rel.slice(6);
    const encoded = rel
      .split('/')
      .filter(function (part) {
        return part !== '';
      })
      .map(encodeURIComponent)
      .join('/');
    return '/media/' + encoded + '?token=' + encodeURIComponent(TOKEN);
  }

  /* ---- 安全 Markdown：先整体转义 HTML，再只生成受控标签；链接仅 http/https ---- */
  function renderInlineMarkdown(text) {
    // text 已经是转义后的文本，只做受控标签替换
    const codes = [];
    let out = String(text).replace(/`([^`\n]+)`/g, function (_m, code) {
      codes.push(code);
      return '\u0000CODE' + (codes.length - 1) + '\u0000';
    });
    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    out = out.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_m, label, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    out = out.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, function (_m, prefix, url) {
      return prefix + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
    });
    out = out.replace(/\u0000CODE(\d+)\u0000/g, function (_m, index) {
      return '<code>' + codes[Number(index)] + '</code>';
    });
    return out;
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.indexOf('-') !== -1;
  }

  function splitTableRow(line) {
    return line
      .replace(/^\s*\|/, '')
      .replace(/\|\s*$/, '')
      .split('|')
      .map(function (cell) {
        return cell.trim();
      });
  }

  function safeMarkdown(source) {
    const raw = String(source == null ? '' : source).replace(/\r\n?/g, '\n');
    const lines = escapeHtml(raw).split('\n');
    const html = [];
    let paragraph = [];
    let listType = '';
    let listItems = [];
    let inCode = false;
    let codeLines = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      html.push('<p>' + paragraph.map(renderInlineMarkdown).join('<br>') + '</p>');
      paragraph = [];
    }

    function flushList() {
      if (!listItems.length) return;
      html.push('<' + listType + '>' + listItems.join('') + '</' + listType + '>');
      listItems = [];
      listType = '';
    }

    function flushCode() {
      if (!inCode) return;
      html.push('<pre><code>' + codeLines.join('\n') + '</code></pre>');
      codeLines = [];
      inCode = false;
    }

    function closeBlocks() {
      flushParagraph();
      flushList();
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      if (inCode) {
        if (/^\s*```/.test(line)) {
          flushCode();
        } else {
          codeLines.push(line);
        }
        continue;
      }

      if (/^\s*```/.test(line)) {
        closeBlocks();
        inCode = true;
        codeLines = [];
        continue;
      }

      if (!line.trim()) {
        closeBlocks();
        continue;
      }

      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        closeBlocks();
        const level = Math.min(6, Math.max(3, heading[1].length + 2));
        html.push('<h' + level + '>' + renderInlineMarkdown(heading[2].trim()) + '</h' + level + '>');
        continue;
      }

      if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
        closeBlocks();
        html.push('<hr>');
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        closeBlocks();
        const quote = [line.replace(/^\s*>\s?/, '')];
        while (i + 1 < lines.length && /^\s*>\s?/.test(lines[i + 1])) {
          i += 1;
          quote.push(lines[i].replace(/^\s*>\s?/, ''));
        }
        html.push('<blockquote>' + quote.map(renderInlineMarkdown).join('<br>') + '</blockquote>');
        continue;
      }

      // Markdown 表格
      if (line.indexOf('|') !== -1 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        closeBlocks();
        const headerCells = splitTableRow(line);
        i += 1;
        const rows = [];
        while (i + 1 < lines.length && lines[i + 1].indexOf('|') !== -1 && lines[i + 1].trim()) {
          i += 1;
          rows.push(splitTableRow(lines[i]));
        }
        let table = '<table><thead><tr>';
        headerCells.forEach(function (cell) {
          table += '<th>' + renderInlineMarkdown(cell) + '</th>';
        });
        table += '</tr></thead><tbody>';
        rows.forEach(function (row) {
          table += '<tr>';
          headerCells.forEach(function (_h, index) {
            table += '<td>' + renderInlineMarkdown(row[index] || '') + '</td>';
          });
          table += '</tr>';
        });
        table += '</tbody></table>';
        html.push(table);
        continue;
      }

      const ordered = /^\s*(\d+)[.)]\s+(.*)$/.exec(line);
      const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      if (ordered || bullet) {
        flushParagraph();
        const wanted = ordered ? 'ol' : 'ul';
        const content = ordered ? ordered[2] : bullet[1];
        if (listType && listType !== wanted) flushList();
        listType = wanted;

        const task = /^\[( |x|X)\]\s*(.*)$/.exec(content);
        if (task) {
          const done = task[1].toLowerCase() === 'x';
          listItems.push(
            '<li class="md-task' + (done ? ' is-done' : '') + '"><span class="md-checkbox"></span>' +
              '<span class="md-task-text">' + renderInlineMarkdown(task[2]) + '</span></li>'
          );
        } else {
          listItems.push('<li>' + renderInlineMarkdown(content) + '</li>');
        }
        continue;
      }

      if (listType) flushList();
      paragraph.push(line);
    }

    flushCode();
    closeBlocks();
    return html.join('\n');
  }

  // ===========================================================================
  // 3. HTTP API / SSE
  // ===========================================================================
  class ApiError extends Error {
    constructor(message, code, status) {
      super(message);
      this.name = 'ApiError';
      this.code = code || '';
      this.status = status || 0;
    }
  }

  async function api(path, options) {
    const opts = options || {};
    if (MOCK) throw new ApiError('演示模式不会访问后端接口', 'mock', 0);
    if (!TOKEN) {
      throw new ApiError('页面缺少会话令牌，请通过 web_app.py 打开页面后刷新。', 'unauthorized', 0);
    }
    const init = { method: opts.method || 'GET', headers: { 'X-WXBOX-Token': TOKEN } };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json; charset=utf-8';
      init.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new ApiError('无法连接后端服务（' + (err && err.message ? err.message : '网络错误') + '）', 'network', 0);
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        throw new ApiError('后端返回了无法解析的数据（HTTP ' + response.status + '）', 'bad_response', response.status);
      }
    }
    if (!response.ok || (data && data.ok === false)) {
      const message = (data && data.error) || '请求失败（HTTP ' + response.status + '）';
      throw new ApiError(message, (data && data.code) || '', response.status);
    }
    return data;
  }

  function setSse(state) {
    S.sse = state;
    const offline = state !== 'open';
    dom.connBanner.hidden = !(offline && S.bootstrapped);
    dom.connBannerText.textContent =
      state === 'closed'
        ? '实时连接已断开，请确认后端仍在运行（刷新页面可重连）。'
        : '实时连接已断开，正在自动重连…';
    renderTopbar();
  }

  let eventSource = null;
  const SSE_TYPES = [
    'hello', 'status', 'friends', 'message', 'log', 'resync', 'llm_start', 'llm_reasoning',
    'llm_delta', 'llm_usage', 'llm_done', 'llm_error', 'moments_status', 'moments',
    'messages_cleared',
  ];

  function connectSse() {
    if (MOCK) {
      S.sse = 'open';
      renderTopbar();
      return;
    }
    if (!TOKEN) {
      setSse('closed');
      return;
    }
    try {
      eventSource = new EventSource('/api/events?token=' + encodeURIComponent(TOKEN));
    } catch (err) {
      setSse('closed');
      return;
    }
    eventSource.addEventListener('open', function () {
      if (S.sse !== 'open') setSse('open');
    });
    eventSource.addEventListener('error', function () {
      setSse(eventSource && eventSource.readyState === 2 ? 'closed' : 'connecting');
    });
    SSE_TYPES.forEach(function (type) {
      eventSource.addEventListener(type, function (event) {
        handleSseEvent(type, event);
      });
    });
  }

  function parseSsePayload(event) {
    try {
      const env = JSON.parse(event.data);
      if (!env || typeof env !== 'object') return null;
      return { type: env.type || '', data: env.data || {}, ts: Number(env.ts) || 0 };
    } catch (err) {
      return null;
    }
  }

  function handleStatusEvent(data) {
    S.state = Object.assign({}, S.state, data || {});
    // 只读跟随模式下勾选只是本页筛选，不被后端 status 覆盖
    if (!S.pendingMonitoredSave && !isFollowMode()) {
      const names = Array.isArray(data.monitoring)
        ? data.monitoring
        : Array.isArray(S.state.monitoring)
          ? S.state.monitoring
          : [];
      S.monitored = new Set(names.map(String));
      syncMonitoredUi();
      renderScopeChips();
    }
    renderTopbar();
    renderChatHeader(filteredMessages().length);
  }

  function handleFriendsEvent(data) {
    const list = Array.isArray(data.friends) ? data.friends : [];
    const previous = S.friendMap;
    setFriends(
      list.map(function (friend) {
        return normalizeFriend(friend, previous.get(String((friend && friend.name) || '')));
      })
    );
    mergeContactFriends();
    S.state.friends_count = data.total !== undefined ? data.total : S.friends.length;
    renderFriends();
    renderTopbar();
    renderScopeChips();
  }

  function handleSseEvent(type, event) {
    const payload = parseSsePayload(event);
    if (!payload) return;
    const data = payload.data;

    switch (type) {
      case 'hello':
        setSse('open');
        if (S.bootstrapped) scheduleResync();
        break;
      case 'status':
        handleStatusEvent(data);
        break;
      case 'friends':
        handleFriendsEvent(data);
        break;
      case 'message':
        onLiveMessage(data);
        break;
      case 'log':
        appendLog({ ts: payload.ts || nowSec(), level: data.level || 'info', text: data.text || '' });
        break;
      case 'resync':
        scheduleResync();
        break;
      case 'moments_status':
        handleMomentsStatusEvent(data);
        break;
      case 'moments':
        handleMomentsEvent(data);
        break;
      case 'video_auto_status':
        applyVideoAutoStatus(data);
        updateVideoAutoButton();
        break;
      case 'messages_cleared':
        // 多标签页同步：其他页面清空了聊天记录（本页主动清空时忽略自己触发的广播）
        if (S.localClearInFlight) break;
        clearLocalMessages();
        toast('聊天记录已被清空（其他页面的操作）', 'info', 3200);
        break;
      case 'llm_start':
      case 'llm_reasoning':
      case 'llm_delta':
      case 'llm_usage':
      case 'llm_done':
      case 'llm_error':
        handleLlmEvent(type, data);
        break;
      default:
        break;
    }
  }

  let resyncTimer = null;
  function scheduleResync() {
    if (resyncTimer || MOCK) return;
    resyncTimer = setTimeout(function () {
      resyncTimer = null;
      refreshBootstrap();
    }, 300);
  }

  async function refreshBootstrap() {
    if (S.refreshing || MOCK) return;
    S.refreshing = true;
    try {
      const data = await api('/api/bootstrap');
      applyBootstrap(data, { merge: true });
      setSse('open');
    } catch (err) {
      if (err.code === 'network') setSse('connecting');
    } finally {
      S.refreshing = false;
    }
  }

  // ===========================================================================
  // 4. 数据应用 / 顶栏 / 好友 / 消息渲染
  // ===========================================================================
  function normalizeFriend(raw, previous) {
    const name = String((raw && raw.name) || '');
    const prev = previous || {};
    return {
      name: name,
      wxid: String((raw && raw.wxid) || prev.wxid || ''),
      letter: String((raw && raw.letter) || prev.letter || ''),
      initial: String((raw && raw.initial) || prev.initial || ''),
      count: Number(raw && raw.count !== undefined ? raw.count : prev.count) || 0,
      last_ts: Number(raw && raw.last_ts !== undefined ? raw.last_ts : prev.last_ts) || 0,
      has_history:
        raw && raw.has_history !== undefined ? !!raw.has_history : !!(prev && prev.has_history),
    };
  }

  function setFriends(list) {
    S.friends = Array.isArray(list) ? list : [];
    S.friendMap = new Map();
    S.friends.forEach(function (friend) {
      S.friendMap.set(friend.name, friend);
    });
  }

  /**
   * v4：contacts.friends 是权威列表。后端 friends 可能不全（缓存 293 而 contacts 31），
   * 把 contacts.friends 里尚未出现的人补进 S.friends；后端 friends 事件后也调用，避免被覆盖。
   */
  function mergeContactFriends() {
    if (!S.contacts || !Array.isArray(S.contacts.friends) || !S.contacts.friends.length) return;
    S.contacts.friends.forEach(function (contact) {
      const name = String((contact && contact.name) || '');
      if (!name || S.groupMap.has(name)) return;
      const existing = S.friendMap.get(name);
      if (existing) {
        if (!existing.initial && contact.initial) existing.initial = String(contact.initial);
        if (!existing.wxid && contact.username) existing.wxid = String(contact.username);
        if (contact.has_history !== false) existing.has_history = true;
        return;
      }
      const friend = {
        name: name,
        wxid: String(contact.username || ''),
        letter: String(contact.initial || ''),
        initial: String(contact.initial || ''),
        count: 0,
        last_ts: 0,
        has_history: contact.has_history !== false,
      };
      S.friends.push(friend);
      S.friendMap.set(name, friend);
    });
  }

  function ensureFriend(name) {
    const key = String(name || '');
    // 群消息不能混进好友列表
    if (S.groupMap.has(key)) return ensureGroup(key);
    let friend = S.friendMap.get(key);
    if (!friend) {
      friend = { name: key, wxid: '', letter: '', count: 0, last_ts: 0 };
      S.friends.push(friend);
      S.friendMap.set(key, friend);
    }
    return friend;
  }

  // ---- v3：群聊列表（只读接口，独立于好友；失败不影响好友/聊天）----
  function normalizeGroup(raw, previous) {
    const prev = previous || {};
    return {
      name: String((raw && raw.name) || ''),
      username: String((raw && raw.username) || ''),
      kind: 'group',
      member_hint: String((raw && raw.member_hint) || ''),
      initial: String((raw && raw.initial) || prev.initial || ''),
      has_history:
        raw && raw.has_history !== undefined ? !!raw.has_history : !!(prev && prev.has_history),
      count: 0,
      last_ts: 0,
    };
  }

  function setGroups(list) {
    const previous = S.groupMap;
    S.groups = (Array.isArray(list) ? list : [])
      .map(function (raw) {
        return normalizeGroup(raw, previous.get(String((raw && raw.name) || '')));
      })
      .filter(function (group) {
        return !!group.name;
      });
    S.groupMap = new Map();
    S.groups.forEach(function (group) {
      S.groupMap.set(group.name, group);
    });
    // 群聊加载前可能把群消息当成了好友：迁走，避免好友列表出现群名
    const before = S.friends.length;
    S.friends = S.friends.filter(function (friend) {
      return !S.groupMap.has(friend.name);
    });
    if (S.friends.length !== before) {
      S.friendMap = new Map();
      S.friends.forEach(function (friend) {
        S.friendMap.set(friend.name, friend);
      });
    }
    S.groupsLoaded = true;
    S.groupsError = '';
  }

  function isGroupName(name) {
    return S.groupMap.has(String(name || ''));
  }

  function ensureGroup(name) {
    const key = String(name || '');
    let group = S.groupMap.get(key);
    if (!group) {
      group = {
        name: key,
        username: '',
        kind: 'group',
        member_hint: '',
        initial: '',
        has_history: true,
        count: 0,
        last_ts: 0,
      };
      S.groups.push(group);
      S.groupMap.set(key, group);
    }
    return group;
  }

  // 群消息数量 / 最近时间：从当前内存消息统计（群接口不返回计数）
  function buildGroupStats() {
    const stats = new Map();
    S.messages.forEach(function (message) {
      if (!S.groupMap.has(message.friend)) return;
      const item = stats.get(message.friend) || { count: 0, last_ts: 0 };
      item.count += 1;
      item.last_ts = Math.max(item.last_ts, Number(message.ts) || 0);
      stats.set(message.friend, item);
    });
    return stats;
  }

  // ---- v4：联系人（/api/contacts：只保留有历史记录的联系人 + 拼音首字母）----
  function contactsFilteringActive() {
    return !!(S.contacts && S.contacts.available && S.contacts.loaded);
  }

  function messageFriendNameSet() {
    const set = new Set();
    S.messages.forEach(function (message) {
      set.add(String(message.friend || ''));
    });
    return set;
  }

  /** 返回需要渲染的好友名字集合；null 表示不过滤（联系人接口不可用时兼容全部） */
  function visibleFriendNames() {
    if (!contactsFilteringActive()) return null;
    const set = new Set();
    // contacts.friends 是权威「有历史记录」列表（后端已按 session 过滤）
    (S.contacts.friends || []).forEach(function (friend) {
      if (friend && friend.name) set.add(String(friend.name));
    });
    S.contacts.historyNames.forEach(function (name) {
      set.add(String(name));
    });
    // 注意：不再把「仅页面内存有消息」的非联系人加回来。
    // 例如跟随后端日志里残留的已删除/已改备注联系人（contacts 里 has_history=false），
    // 加回来会让 8081 好友数从 31 变成 32；联系人接口失败时上面的 null 分支会显示全部。
    return set;
  }

  function visibleFriends() {
    const names = visibleFriendNames();
    if (!names) return S.friends.slice();
    return S.friends.filter(function (friend) {
      return names.has(friend.name);
    });
  }

  function hasHistoryFor(name) {
    const key = String(name || '');
    if (S.contacts && S.contacts.historyNames && S.contacts.historyNames.has(key)) return true;
    const friend = S.friendMap.get(key);
    if (friend && friend.has_history) return true;
    const group = S.groupMap.get(key);
    if (group && group.has_history) return true;
    return false;
  }

  function applyContacts(data, options) {
    const opts = options || {};
    const source = data || {};
    S.contacts.available = true;
    S.contacts.loaded = true;
    S.contacts.error = '';
    S.contacts.friends = Array.isArray(source.friends) ? source.friends : [];
    S.contacts.groups = Array.isArray(source.groups) ? source.groups : [];
    S.contacts.initials = Object.assign({}, source.initials || {});
    S.contacts.historyNames = new Set(
      (Array.isArray(source.history_names) ? source.history_names : []).map(String)
    );
    S.contacts.friendCount = Number(source.friend_count) || S.contacts.friends.length;
    S.contacts.groupCount = Number(source.group_count) || S.contacts.groups.length;
    S.contacts.account = source.account || S.contacts.account || {};
    S.contacts.accountWxid = String(
      source.account_wxid || (source.account && source.account.wxid) || S.contacts.accountWxid || ''
    );
    S.contactsCleared = false;

    // 给已有好友对象补 initial（contacts.friends 里带了 initial）
    S.contacts.friends.forEach(function (friend) {
      if (!friend || !friend.name) return;
      if (!S.contacts.initials[friend.name] && friend.initial) {
        S.contacts.initials[friend.name] = String(friend.initial);
      }
      const target = S.friendMap.get(String(friend.name));
      if (target) {
        target.initial = String(friend.initial || target.initial || '');
        if (friend.has_history !== false) target.has_history = true;
      }
    });

    // 群聊列表优先用 contacts.groups（已过滤，包含 initial）
    setGroups(S.contacts.groups);
    S.groupsSource = 'contacts';
    // contacts.friends 是权威列表：补进后端 friends 未覆盖的联系人
    mergeContactFriends();
    if (!opts.silent) {
      renderFriends();
      renderTopbar();
      renderScopeChips();
    }
    updateContactsAccount();
  }

  async function loadContacts(options) {
    const opts = options || {};
    if (S.contacts.loading) return;
    S.contacts.loading = true;
    try {
      if (MOCK) {
        if (qs.has('contactsfail')) {
          S.contacts.available = false;
          S.contacts.loaded = false;
          S.contacts.error = '演示：联系人接口不可用';
          setGroups(MOCK_GROUPS);
          S.groupsSource = 'groups';
          renderFriends();
          renderTopbar();
          return;
        }
        applyContacts(mockContactsData(), { silent: true });
        renderFriends();
        renderTopbar();
        renderScopeChips();
        return;
      }
      const data = await api('/api/contacts' + (opts.refresh ? '?refresh=1' : ''));
      applyContacts(data, { silent: true });
      renderFriends();
      renderTopbar();
      renderScopeChips();
    } catch (err) {
      S.contacts.available = false;
      S.contacts.loaded = false;
      S.contacts.error = err.message || '联系人过滤不可用';
      // 回退：群聊继续用 /api/groups（不过滤），好友显示全部
      try {
        await loadGroups({ refresh: opts.refresh });
      } catch (fallbackError) {
        /* 群聊也失败时保持现有列表 */
      }
      renderFriends();
      renderTopbar();
    } finally {
      S.contacts.loading = false;
      renderFriends();
    }
  }

  function updateContactsHint() {
    if (!dom.contactsHint) return;
    const label = dom.contactsHint.querySelector('span');
    if (contactsFilteringActive()) {
      if (label) label.textContent = '仅显示有聊天记录的好友与群聊';
      dom.contactsHint.hidden = false;
      dom.contactsHint.classList.remove('is-warn');
      return;
    }
    if (S.contactList === 'groups' && S.contacts.error) {
      if (label) label.textContent = '联系人过滤不可用，暂显示全部群聊';
      dom.contactsHint.hidden = false;
      dom.contactsHint.classList.add('is-warn');
      return;
    }
    dom.contactsHint.hidden = true;
    dom.contactsHint.classList.remove('is-warn');
  }

  function updateContactsAccount() {
    if (!dom.contactsAccount) return;
    const account = (S.contacts && S.contacts.account) || {};
    const wxid = String(S.contacts.accountWxid || account.wxid || '');
    const nick = String(account.nick_name || account.nickname || '');
    if (!wxid && !nick) {
      dom.contactsAccount.hidden = true;
      return;
    }
    dom.contactsAccountName.textContent = wxid || nick;
    dom.contactsAccount.title = (nick ? nick : '当前登录账号') + (wxid ? ' · ' + wxid : '');
    dom.contactsAccount.hidden = false;
  }

  /** v6：POST /api/contacts/refresh 成功后的处理（替换列表，必要时清空旧账号消息/勾选） */
  function applyContactRefreshResult(data) {
    const source = (data && data.contacts) || {};
    const friendList = (Array.isArray(source.friends) ? source.friends : [])
      .map(function (friend) {
        return {
          name: String((friend && friend.name) || ''),
          wxid: String((friend && (friend.username || friend.wxid)) || ''),
          letter: String((friend && friend.initial) || ''),
          initial: String((friend && friend.initial) || ''),
          count: 0,
          last_ts: 0,
          has_history: !friend || friend.has_history !== false,
        };
      })
      .filter(function (friend) {
        return !!friend.name;
      });

    setFriends(friendList);
    S.contacts.available = true;
    S.contacts.loaded = true;
    S.contacts.error = '';
    S.contacts.friends = Array.isArray(source.friends) ? source.friends : [];
    S.contacts.groups = Array.isArray(source.groups) ? source.groups : [];
    S.contacts.initials = Object.assign({}, source.initials || {});
    S.contacts.historyNames = new Set(
      (Array.isArray(source.history_names) ? source.history_names : []).map(String)
    );
    S.contacts.friendCount = friendList.length;
    S.contacts.groupCount = S.contacts.groups.length;
    S.contacts.account = source.account || S.contacts.account || {};
    S.contacts.accountWxid = String(
      source.account_wxid || (source.account && source.account.wxid) || S.contacts.accountWxid || ''
    );
    S.contactsCleared = false;

    setGroups(S.contacts.groups);
    S.groupsSource = 'contacts';
    S.groupsLoaded = true;
    S.groupsError = '';

    const accountChanged = !!(data && (data.account_changed || source.account_changed));
    if (accountChanged) {
      // 切换账号：清掉旧账号的内存消息 / 监听勾选 / 当前会话
      S.messages = [];
      S.msgIndex.clear();
      S.state.message_total = 0;
      S.monitored = new Set();
      S.activeFriend = null;
      S.paused = false;
      S.unseen = 0;
      if (S.tab === 'messages') S.scope = 'all';
      closeHistoryPop();
      // 服务端随后会广播 messages_cleared，这里本地已清空，避免重复 toast
      S.localClearInFlight = true;
      setTimeout(function () {
        S.localClearInFlight = false;
      }, 1500);
    } else if (data && Array.isArray(data.monitoring)) {
      S.monitored = new Set(data.monitoring.map(String));
    }

    renderFriends();
    renderTopbar();
    renderMessages({ scrollBottom: true });
    renderScopeChips();
    updateContactsAccount();

    const accountLabel =
      S.contacts.accountWxid ||
      (S.contacts.account && (S.contacts.account.nick_name || S.contacts.account.wxid)) ||
      '未知账号';
    toast(
      '已刷新：账号 ' + accountLabel + '，好友 ' + friendList.length + ' / 群聊 ' + S.contacts.groups.length,
      'ok',
      3800
    );
  }

  async function refreshContacts() {
    if (S.contactsRefreshing) return;
    S.contactsRefreshing = true;
    const button = dom.refreshContactsBtn;
    if (button) {
      button.disabled = true;
      button.textContent = '刷新中…';
    }
    try {
      let data;
      if (MOCK) data = mockRefreshContactsData();
      else data = await api('/api/contacts/refresh', { method: 'POST' });
      applyContactRefreshResult(data);
    } catch (err) {
      toast('刷新列表失败：' + err.message, 'error', 4800);
    } finally {
      S.contactsRefreshing = false;
      if (button) {
        button.disabled = false;
        button.textContent = '刷新列表';
      }
    }
  }

  /** v6：清空好友/群聊列表（只清列表，保留消息与当前账号显示） */
  function applyClearedContacts() {
    setFriends([]);
    setGroups([]);
    S.contacts.friends = [];
    S.contacts.groups = [];
    S.contacts.initials = {};
    S.contacts.historyNames = new Set();
    S.contacts.friendCount = 0;
    S.contacts.groupCount = 0;
    S.contactsCleared = true;
    S.activeFriend = null;
    S.paused = false;
    S.unseen = 0;
    if (S.tab === 'messages') S.scope = 'all';
    closeHistoryPop();
    renderFriends();
    renderTopbar();
    renderMessages({ scrollBottom: false });
    renderScopeChips();
    updateContactsAccount();
  }

  async function clearContacts() {
    if (S.contactsRefreshing) return;
    if (
      !window.confirm(
        '确定清空好友/群聊列表吗？\n（只清空网页端列表，不影响微信；可点「刷新列表」重新读取）'
      )
    ) {
      return;
    }
    const button = dom.clearContactsBtn;
    if (button) button.disabled = true;
    try {
      if (MOCK) {
        applyClearedContacts();
      } else {
        await api('/api/contacts/clear', { method: 'POST', body: {} });
        applyClearedContacts();
      }
      toast('已清空好友/群聊列表', 'ok', 2600);
    } catch (err) {
      toast('清空列表失败：' + err.message, 'error', 4600);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function normalizeLlm(raw) {
    const base = defaultLlm();
    const src = raw || {};
    const llm = Object.assign(base, src);
    llm.configured = !!src.configured;
    llm.thinking = src.thinking !== false;
    llm.attach_images = src.attach_images !== false;
    if (!Array.isArray(llm.models) || !llm.models.length) llm.models = DEFAULT_MODELS.slice();
    if (!llm.model_label) {
      const hit = llm.models.filter(function (m) {
        return m.id === llm.model;
      })[0];
      llm.model_label = (hit && hit.label) || 'DeepSeek V4.1 Flash 1017';
    }
    return llm;
  }

  function applyBootstrap(data, options) {
    const opts = options || {};
    const merge = !!opts.merge;
    S.server = data.server || {};
    S.state = data.state || {};

    const previous = S.friendMap;
    const incomingFriends = Array.isArray(data.friends) ? data.friends : [];
    const perFriend = S.state.per_friend_counts || {};
    setFriends(
      incomingFriends.map(function (friend) {
        const merged = normalizeFriend(friend, previous.get(String((friend && friend.name) || '')));
        if (!merged.count && perFriend[merged.name]) merged.count = Number(perFriend[merged.name]) || 0;
        return merged;
      })
    );
    mergeContactFriends();

    S.llm = normalizeLlm(data.llm || S.llm);

    const incomingMessages = Array.isArray(data.messages) ? data.messages : [];
    incomingMessages.forEach(function (message) {
      addMessage(message);
    });

    const moments = data.moments || {};
    if (moments.status) S.moments.status = moments.status;
    if (moments.status) applyMomentsAutoStatus(moments.status);
    if (moments.count !== undefined) S.moments.count = Number(moments.count) || 0;
    if (moments.synced_at !== undefined) S.moments.syncedAt = Number(moments.synced_at) || 0;
    if (moments.account) S.moments.account = moments.account;
    if (moments.status && moments.status.syncing !== undefined) {
      S.moments.syncing = !!moments.status.syncing;
    }
    if (moments.status && Number(moments.status.limit)) {
      const limit = Number(moments.status.limit);
      S.moments.lastLimit = limit;
      if (!S.moments.syncLimitTouched) S.moments.syncLimit = limit;
    }

    if (!S.pendingMonitoredSave) {
      const names = Array.isArray(S.state.monitoring) ? S.state.monitoring : [];
      S.monitored = new Set(names.map(String));
    }

    const logs = Array.isArray(data.logs) ? data.logs : [];
    if (!merge) {
      S.logs = [];
      S.logSeen = new Set();
      dom.logBody.replaceChildren();
    }
    logs.forEach(function (entry) {
      appendLog({ ts: Number(entry.ts) || nowSec(), level: entry.level || 'info', text: entry.text || '' });
    });

    S.bootstrapped = true;
    if (!S.activeFriend && S.friends.length && !qs.has('tab')) {
      // 首屏默认「全部消息」，不自动选中好友
    }
    renderAll({ scrollBottom: !merge });
  }

  function renderAll(options) {
    const opts = options || {};
    renderTopbar();
    renderFriends();
    renderMessages({ scrollBottom: !!opts.scrollBottom });
    renderMomentsAll();
    renderModelRow();
    renderScopeChips();
    renderQuickChips();
    renderAnalysis();
    updateNavCounts();
  }

  function getMode() {
    if (MOCK) return 'fake';
    if (RAW_MODE === 'real' || RAW_MODE === 'fake' || RAW_MODE === 'follow') return RAW_MODE;
    if (S.server && ['real', 'fake', 'follow'].indexOf(S.server.mode) !== -1) return S.server.mode;
    return '';
  }

  function isFollowMode() {
    return getMode() === 'follow';
  }

  function updateNavCounts() {
    // 以当前实际加载的消息数为准，避免与 state.message_total（后端内存记录数）口径不一致
    dom.navMsgCount.textContent = formatCount(S.messages.length);
    dom.navMomentsCount.textContent = formatCount(S.moments.count || S.moments.items.length);
  }

  function renderTopbar() {
    const st = S.state || {};
    const running = !!st.running;
    const follow = isFollowMode();
    const following = st.monitor_state === 'following';

    if (follow) {
      // 只读跟随模式：不启动采集，文案不显示「等待微信窗口 / 未连接」
      dom.statusDot.className = 'dot dot--info';
      dom.statusText.textContent = following ? '跟随即时同步' : '跟随桌面版日志';
      dom.statusChip.title =
        '只读跟随模式：消息来自桌面版写入的日志；本页不会启动采集、不会操作微信。' +
        (st.error ? '\n错误：' + st.error : '');
    } else {
      dom.statusDot.className = 'dot ' + (running ? (st.wechat_found ? 'dot--online' : 'dot--warn') : 'dot--off');
      dom.statusText.textContent = running ? (st.wechat_found ? '微信监听中' : '等待微信窗口') : '后端未运行';
      dom.statusChip.title =
        '微信监听状态：' +
        (running ? (st.wechat_found ? '已连接' : '未找到微信窗口') : '后端未运行') +
        (st.error ? '\n错误：' + st.error : '');
    }

    dom.liveChip.className =
      'live-chip ' +
      (S.sse === 'open' ? 'is-online' : S.sse === 'closed' ? 'is-offline' : 'is-connecting') +
      (follow ? ' is-follow' : '');
    dom.liveText.textContent = follow
      ? S.sse === 'open'
        ? '跟随'
        : S.sse === 'closed'
          ? '断线'
          : '连接中'
      : S.sse === 'open'
        ? '实时'
        : S.sse === 'closed'
          ? '断线'
          : '连接中';
    dom.liveChip.title = follow
      ? S.sse === 'open'
        ? '实时事件流已连接（只读跟随）'
        : S.sse === 'closed'
          ? '实时事件流已断开'
          : '正在连接实时事件流…'
      : S.sse === 'open'
        ? '实时事件流已连接（SSE）'
        : S.sse === 'closed'
          ? '实时事件流已断开'
          : '正在连接实时事件流…';

    dom.topChat.textContent = S.tab === 'moments' ? '朋友圈' : S.activeFriend || '全部消息';
    dom.topChatBox.title = '当前消息筛选：' + (S.activeFriend || '全部消息');

    const names = Array.from(S.monitored);
    if (!names.length) {
      dom.topMonitoring.textContent = '未选择';
      dom.topMonitoring.classList.remove('is-active');
    } else {
      dom.topMonitoring.textContent =
        names.length <= 2 ? names.join('、') : names[0] + ' +' + (names.length - 1);
      dom.topMonitoring.classList.add('is-active');
    }
    dom.topMonitorBox.title = names.length
      ? (follow ? '本页筛选：' : '监听中：') + names.join('、')
      : follow
        ? '尚未勾选本页筛选好友'
        : '尚未勾选监听好友';

    const mode = getMode();
    dom.modeBadge.className =
      'mode-badge ' +
      (mode === 'real'
        ? 'mode-badge--real'
        : mode === 'fake'
          ? 'mode-badge--fake'
          : mode === 'follow'
            ? 'mode-badge--follow'
            : 'mode-badge--off');
    dom.modeText.textContent =
      mode === 'real' ? '真实微信' : mode === 'fake' ? '演示模式' : mode === 'follow' ? '只读跟随' : '未连接';
    dom.modeBadge.title =
      (mode === 'real'
        ? '真实微信后端（只读采集）'
        : mode === 'fake'
          ? '演示模式：使用内置假后端，不连接真实微信'
          : mode === 'follow'
            ? '只读跟随模式：读取桌面版写入的日志，不启动采集、不操作微信'
            : '尚未获取后端模式') + (S.server.backend_note ? '\n' + S.server.backend_note : '');

    dom.followNote.hidden = !follow;
    dom.backendToggle.hidden = follow;
    dom.listenActions.hidden = follow;
    if (!follow) {
      dom.backendToggle.title = running ? '停止后端监听' : '启动后端监听';
      dom.backendToggle.classList.toggle('is-active', running);
      dom.startListenBtn.disabled = running;
      dom.stopListenBtn.disabled = !running;
      dom.startListenBtn.classList.toggle('is-active', running);
      dom.stopListenBtn.classList.toggle('is-active', !running);
    }

    updateMonitorSummary();
    updateNavCounts();
  }

  function updateMonitorSummary() {
    const suffix =
      ' · 好友 ' + visibleFriends().length + ' · 群聊 ' + S.groups.length;
    dom.monitorSummary.textContent = isFollowMode()
      ? '本页筛选 ' + S.monitored.size + ' 个会话' + suffix
      : '监听中 ' + S.monitored.size + ' 个会话' + suffix;
    dom.monitorSummary.title = isFollowMode()
      ? '只读跟随模式：勾选只影响本页消息筛选与 AI 分析范围，不会修改桌面版的监听配置'
      : '勾选的好友 / 群聊会被持续监听';
  }

  function renderFriends() {
    if (S.contactList === 'groups') renderGroupRows();
    else renderFriendRows();

    const visible = visibleFriends();
    dom.friendsTotal.textContent = String(visible.length);
    dom.groupsTotal.textContent = String(S.groups.length);
    updateMonitorSummary();
    updateContactsHint();
    updateContactsAccount();
    syncMonitoredUi();

    if (S.contactList === 'groups') {
      const groupMessages = S.messages.filter(function (message) {
        return S.groupMap.has(message.friend);
      }).length;
      dom.friendsFoot.textContent =
        '共 ' + S.groups.length + ' 个群聊 · 群消息 ' + formatCount(groupMessages) + ' 条' +
        (S.monitored.size ? ' · 本页筛选 ' + S.monitored.size + ' 个会话' : '');
    } else {
      const recordTotal = Number(S.state.message_total) || S.messages.length;
      dom.friendsFoot.textContent =
        '共 ' + visible.length + ' 位好友 · 已加载 ' + formatCount(S.messages.length) + ' 条' +
        (recordTotal > S.messages.length ? '（后端记录 ' + formatCount(recordTotal) + ' 条）' : '') +
        (S.monitored.size ? ' · 本页筛选 ' + S.monitored.size + ' 位' : '');
    }
  }

  function renderFriendRows() {
    const query = S.friendQuery.trim().toLowerCase();
    const visible = visibleFriends();
    const list = visible
      .filter(function (friend) {
        return !query || String(friend.name).toLowerCase().indexOf(query) !== -1;
      })
      .sort(function (a, b) {
        return (b.last_ts || 0) - (a.last_ts || 0) || String(a.name).localeCompare(String(b.name), 'zh');
      });

    dom.friendList.replaceChildren();

    // 「全部好友」行
    const allRow = el('div', 'friend-row friend-row__all' + (S.activeFriend ? '' : ' is-active'));
    allRow.tabIndex = 0;
    allRow.dataset.name = '__all__';
    allRow.appendChild(el('span', 'friend-row__check'));
    const allAvatar = el('span', 'avatar friend-avatar', '全');
    allRow.appendChild(allAvatar);
    const allMain = el('div', 'friend-main');
    allMain.appendChild(el('div', 'friend-name', '全部好友'));
    allMain.appendChild(el('div', 'friend-meta', visible.length + ' 位好友 · 已加载 ' + formatCount(S.messages.length) + ' 条'));
    allRow.appendChild(allMain);
    allRow.appendChild(el('div', 'friend-right'));
    allRow.addEventListener('click', function () {
      selectFriend(null);
    });
    allRow.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectFriend(null);
      }
    });
    dom.friendList.appendChild(allRow);

    list.forEach(function (friend) {
      const row = el('div', 'friend-row' + (S.activeFriend === friend.name ? ' is-active' : ''));
      row.tabIndex = 0;
      row.dataset.name = friend.name;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', S.activeFriend === friend.name ? 'true' : 'false');

      const checkWrap = el('label', 'friend-row__check');
      checkWrap.title = isFollowMode()
        ? '勾选后只影响本页筛选与 AI 分析范围（只读跟随，不修改桌面配置）'
        : '勾选后监听该好友';
      const checkbox = el('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'checkbox';
      checkbox.checked = S.monitored.has(friend.name);
      checkbox.addEventListener('click', function (event) {
        event.stopPropagation();
      });
      checkbox.addEventListener('change', function () {
        toggleMonitored(friend.name, checkbox.checked);
      });
      checkWrap.appendChild(checkbox);
      checkWrap.addEventListener('click', function (event) {
        event.stopPropagation();
      });
      row.appendChild(checkWrap);

      const avatar = el(
        'span',
        'avatar friend-avatar',
        avatarInitial(friend.name, friend.initial || contactInitial(friend.name))
      );
      avatar.style.setProperty('--hue', String(hashHue(friend.name)));
      row.appendChild(avatar);

      const main = el('div', 'friend-main');
      main.appendChild(el('div', 'friend-name', friend.name));
      const metaParts = [];
      if (friend.count) metaParts.push(friend.count + ' 条');
      else if (hasHistoryFor(friend.name)) metaParts.push('有历史记录');
      else metaParts.push('暂无消息');
      if (friend.last_ts) metaParts.push('最近 ' + relTime(friend.last_ts));
      main.appendChild(el('div', 'friend-meta', metaParts.join(' · ')));
      row.appendChild(main);

      const right = el('div', 'friend-right');
      right.appendChild(el('div', 'friend-time', friendTimeText(friend.last_ts)));
      if (friend.count) {
        right.appendChild(el('span', 'friend-count', friend.count > 999 ? '999+' : String(friend.count)));
      }
      row.appendChild(right);

      row.addEventListener('click', function () {
        selectFriend(friend.name);
      });
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectFriend(friend.name);
        }
      });
      dom.friendList.appendChild(row);
    });

    if (!list.length && visible.length) {
      dom.friendList.appendChild(el('div', 'friend-empty', '没有匹配的好友，试试换个关键词。'));
    }
    if (!visible.length) {
      dom.friendList.appendChild(
        el(
          'div',
          'friend-empty',
          S.contactsCleared
            ? '列表已清空，点「刷新列表」读取当前账号。'
            : contactsFilteringActive()
              ? '当前没有可显示的好友：仅显示有聊天记录的联系人。\n可点击右上角刷新，或先用「同步历史」读取聊天记录。'
              : '还没有好友列表。\n点击右上角「重新同步」从微信读取。'
        )
      );
    }
  }

  function renderGroupRows() {
    dom.friendList.replaceChildren();

    if (S.groupsError) {
      const state = el('div', 'group-state');
      state.appendChild(el('div', null, '群聊列表加载失败：' + S.groupsError));
      const retry = el('button', 'btn btn-ghost btn-sm', '重试');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        S.groupsError = '';
        loadGroups({ refresh: true, retry: true });
      });
      state.appendChild(retry);
      dom.friendList.appendChild(state);
      return;
    }

    if ((S.groupsLoading || !S.groupsLoaded) && !S.groups.length) {
      dom.friendList.appendChild(el('div', 'friend-empty', '正在加载群聊…'));
      return;
    }

    if (!S.groups.length) {
      dom.friendList.appendChild(
        el(
          'div',
          'friend-empty',
          S.contactsCleared
            ? '列表已清空，点「刷新列表」读取当前账号。'
            : '还没有群聊列表。\n点击右上角「重新同步」从微信读取。'
        )
      );
      return;
    }

    const query = S.groupQuery.trim().toLowerCase();
    const stats = buildGroupStats();
    const list = S.groups
      .map(function (group) {
        const stat = stats.get(group.name) || { count: 0, last_ts: 0 };
        return {
          name: group.name,
          username: group.username,
          member_hint: group.member_hint,
          count: stat.count,
          last_ts: stat.last_ts,
        };
      })
      .filter(function (group) {
        return !query || String(group.name).toLowerCase().indexOf(query) !== -1;
      })
      .sort(function (a, b) {
        return (b.last_ts || 0) - (a.last_ts || 0) || String(a.name).localeCompare(String(b.name), 'zh');
      });

    list.forEach(function (group) {
      const row = el('div', 'friend-row' + (S.activeFriend === group.name ? ' is-active' : ''));
      row.tabIndex = 0;
      row.dataset.name = group.name;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', S.activeFriend === group.name ? 'true' : 'false');

      const checkWrap = el('label', 'friend-row__check');
      checkWrap.title = isFollowMode()
        ? '勾选后只影响本页筛选与 AI 分析范围（只读跟随，不修改桌面配置）'
        : '勾选后监听该群聊';
      const checkbox = el('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'checkbox';
      checkbox.checked = S.monitored.has(group.name);
      checkbox.addEventListener('click', function (event) {
        event.stopPropagation();
      });
      checkbox.addEventListener('change', function () {
        toggleMonitored(group.name, checkbox.checked);
      });
      checkWrap.appendChild(checkbox);
      checkWrap.addEventListener('click', function (event) {
        event.stopPropagation();
      });
      row.appendChild(checkWrap);

      const avatar = el(
        'span',
        'avatar friend-avatar',
        avatarInitial(group.name, group.initial || groupInitial(group.name) || contactInitial(group.name))
      );
      avatar.style.setProperty('--hue', String(hashHue(group.name)));
      row.appendChild(avatar);

      const main = el('div', 'friend-main');
      const nameLine = el('div', 'friend-name-line');
      nameLine.appendChild(el('div', 'friend-name', group.name));
      nameLine.appendChild(el('span', 'group-badge', '群'));
      main.appendChild(nameLine);
      const metaParts = [];
      if (group.count) metaParts.push(group.count + ' 条');
      else if (hasHistoryFor(group.name)) metaParts.push('有历史记录');
      else metaParts.push('暂无消息');
      if (group.last_ts) metaParts.push('最近 ' + relTime(group.last_ts));
      main.appendChild(el('div', 'friend-meta', metaParts.join(' · ')));
      row.appendChild(main);

      const right = el('div', 'friend-right');
      right.appendChild(el('div', 'friend-time', friendTimeText(group.last_ts)));
      if (group.count) {
        right.appendChild(el('span', 'friend-count', group.count > 999 ? '999+' : String(group.count)));
      }
      row.appendChild(right);

      row.addEventListener('click', function () {
        selectFriend(group.name);
      });
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectFriend(group.name);
        }
      });
      dom.friendList.appendChild(row);
    });

    if (!list.length) {
      dom.friendList.appendChild(el('div', 'friend-empty', '没有匹配的群聊，试试换个关键词。'));
    }
  }

  function syncMonitoredUi() {
    dom.friendList.querySelectorAll('.friend-row[data-name]').forEach(function (row) {
      const name = row.dataset.name;
      const checkbox = row.querySelector('input.checkbox');
      if (checkbox && name !== '__all__') checkbox.checked = S.monitored.has(name);
    });
    updateMonitorSummary();
  }

  function selectFriend(name) {
    const next = name && S.activeFriend !== name ? name : null;
    S.activeFriend = next;
    if (S.tab === 'messages') {
      S.scope = next ? 'friend' : 'all';
    }
    S.paused = false;
    S.unseen = 0;
    closeHistoryPop();
    renderFriends();
    renderTopbar();
    renderMessages({ scrollBottom: true });
    renderScopeChips();
    closeDrawers();
  }

  let monitoredTimer = null;
  function toggleMonitored(name, checked) {
    if (checked) S.monitored.add(name);
    else S.monitored.delete(name);
    S.pendingMonitoredSave = true;
    renderTopbar();
    syncMonitoredUi();
    renderScopeChips();
    scheduleMonitoredSave();
  }

  function scheduleMonitoredSave() {
    clearTimeout(monitoredTimer);
    monitoredTimer = setTimeout(async function () {
      const names = Array.from(S.monitored);
      if (MOCK) {
        S.pendingMonitoredSave = false;
        renderFriends();
        renderTopbar();
        renderScopeChips();
        toast('演示模式：已更新本页勾选（不会请求后端）', 'info', 2200);
        return;
      }
      if (isFollowMode()) {
        // 只读跟随：勾选只作为本页筛选条件，不写桌面版配置
        S.pendingMonitoredSave = false;
        renderFriends();
        renderTopbar();
        renderScopeChips();
        toast(
          names.length
            ? '已更新本页筛选（只读跟随模式，不会修改桌面配置）'
            : '已清空本页筛选（只读跟随模式，不会修改桌面配置）',
          'info',
          2800
        );
        return;
      }
      try {
        const result = await api('/api/monitored', { method: 'POST', body: { names: names } });
        S.pendingMonitoredSave = false;
        if (result && Array.isArray(result.monitoring)) {
          S.monitored = new Set(result.monitoring.map(String));
        }
        renderFriends();
        renderTopbar();
        renderScopeChips();
        toast(names.length ? '监听名单已更新（' + names.length + ' 个会话）' : '已清空监听名单，停止监听', 'ok', 2400);
      } catch (err) {
        S.pendingMonitoredSave = false;
        toast('保存监听名单失败：' + err.message, 'error', 4000);
      }
    }, 500);
  }

  function currentContactNames() {
    return S.contactList === 'groups'
      ? S.groups.map(function (group) {
          return group.name;
        })
      : visibleFriends().map(function (friend) {
          return friend.name;
        });
  }

  function selectAllFriends() {
    const names = currentContactNames();
    names.forEach(function (name) {
      S.monitored.add(name);
    });
    S.pendingMonitoredSave = true;
    renderFriends();
    renderTopbar();
    renderScopeChips();
    scheduleMonitoredSave();
    toast(
      (isFollowMode() ? '已在本页选择全部 ' : '已勾选全部 ') +
        names.length +
        (S.contactList === 'groups' ? ' 个群聊' : ' 位好友'),
      'ok',
      2200
    );
  }

  function clearAllFriends() {
    currentContactNames().forEach(function (name) {
      S.monitored.delete(name);
    });
    S.pendingMonitoredSave = true;
    renderFriends();
    renderTopbar();
    renderScopeChips();
    scheduleMonitoredSave();
    toast(isFollowMode() ? '已清空本页筛选' : '已取消当前列表的监听', 'info', 2200);
  }

  /* ---- v3：群聊列表加载（异步，不阻塞好友/聊天） ---- */
  async function loadGroups(options) {
    const opts = options || {};
    if (S.groupsLoading) return;
    if (MOCK) {
      if (qs.has('groupsfail') && !opts.retry) {
        S.groupsError = '演示：群聊接口不可用';
        S.groupsLoaded = true;
        renderFriends();
        return;
      }
      if (!S.groups.length) setGroups(MOCK_GROUPS);
      S.groupsLoaded = true;
      S.groupsError = '';
      renderFriends();
      renderTopbar();
      return;
    }
    S.groupsLoading = true;
    S.groupsError = '';
    if (S.contactList === 'groups') renderFriends();
    try {
      const data = await api('/api/groups' + (opts.refresh ? '?refresh=1' : ''));
      setGroups((data && data.groups) || []);
      S.groupsSource = 'groups';
      renderFriends();
      renderTopbar();
      renderScopeChips();
    } catch (err) {
      S.groupsError = err.message || '群聊列表加载失败';
      S.groupsLoaded = true;
      if (S.contactList === 'groups') renderFriends();
    } finally {
      S.groupsLoading = false;
      if (S.contactList === 'groups') renderFriends();
    }
  }

  function refreshContactList() {
    // v4：刷新联系人（带 ?refresh=1），同步过滤后的好友/群聊与首字母
    if (MOCK) {
      loadContacts({ refresh: true });
      toast('演示模式：已刷新联系人列表', 'ok', 1800);
      return;
    }
    setButtonBusy(dom.syncFriendsBtn, true);
    loadContacts({ refresh: true }).finally(function () {
      setButtonBusy(dom.syncFriendsBtn, false);
    });
  }

  function setContactList(list) {
    if (list !== 'friends' && list !== 'groups') return;
    if (S.contactList === list) return;
    S.contactList = list;
    dom.contactTabs.querySelectorAll('.contact-tab').forEach(function (button) {
      const active = button.dataset.list === list;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    dom.friendSearch.value = list === 'groups' ? S.groupQuery : S.friendQuery;
    dom.friendSearch.placeholder = list === 'groups' ? '搜索群聊…' : '搜索好友…';
    dom.friendSearchClear.hidden = !dom.friendSearch.value;
    renderFriends();
    if (list === 'groups' && !S.groupsLoaded && !S.groupsLoading) loadGroups({});
  }

  async function syncFriends() {
    if (MOCK) {
      toast('演示模式：已模拟重新同步好友', 'ok');
      return;
    }
    setButtonBusy(dom.syncFriendsBtn, true);
    try {
      await api('/api/friends/sync', { method: 'POST', body: {} });
      toast('已请求重新同步好友列表，稍后自动刷新…', 'ok', 2400);
    } catch (err) {
      toast('同步好友失败：' + err.message, 'error', 4000);
    } finally {
      setTimeout(function () {
        setButtonBusy(dom.syncFriendsBtn, false);
      }, 700);
    }
  }

  function setButtonBusy(button, busy) {
    if (!button) return;
    button.classList.toggle('is-busy', !!busy);
    button.disabled = !!busy;
  }

  /* ---- 消息数据 ---- */
  function addMessage(message) {
    if (!message || !message.id || S.msgIndex.has(message.id)) return false;
    const normalized = Object.assign({}, message, {
      id: String(message.id),
      ts: Number(message.ts) || 0,
      extra: message.extra || {},
    });
    S.msgIndex.set(normalized.id, normalized);
    insertMessageSorted(normalized);
    if (S.messages.length > MAX_MESSAGES) {
      const removed = S.messages.splice(0, S.messages.length - MAX_MESSAGES);
      removed.forEach(function (item) {
        S.msgIndex.delete(item.id);
      });
      S.needsFullRender = true;
    }
    return true;
  }

  function insertMessageSorted(message) {
    const arr = S.messages;
    const ts = Number(message.ts) || 0;
    if (!arr.length || ts >= (Number(arr[arr.length - 1].ts) || 0)) {
      arr.push(message);
      return;
    }
    let low = 0;
    let high = arr.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if ((Number(arr[mid].ts) || 0) <= ts) low = mid + 1;
      else high = mid;
    }
    arr.splice(low, 0, message);
  }

  function matchesMessageFilter(message) {
    if (S.activeFriend && message.friend !== S.activeFriend) return false;
    if (S.kind !== 'all' && message.kind !== S.kind) return false;
    const query = S.msgQuery.trim().toLowerCase();
    if (query) {
      const hay = [
        message.text,
        message.friend,
        message.extra && message.extra.original_name,
        message.kind_label,
      ]
        .filter(Boolean)
        .join('\n')
        .toLowerCase();
      if (hay.indexOf(query) === -1) return false;
    }
    return true;
  }

  function filteredMessages() {
    const out = [];
    for (let i = 0; i < S.messages.length; i++) {
      if (matchesMessageFilter(S.messages[i])) out.push(S.messages[i]);
    }
    return out;
  }

  function onLiveMessage(message) {
    if (!addMessage(message)) return;
    const extra = message.extra || {};
    const isGroupMessage = !!extra.is_group || isGroupName(message.friend);
    const friend = isGroupMessage ? ensureGroup(message.friend) : ensureFriend(message.friend);
    friend.count = (Number(friend.count) || 0) + 1;
    friend.last_ts = Number(message.ts) || nowSec();
    S.state.message_total = (Number(S.state.message_total) || 0) + 1;
    const visible = matchesMessageFilter(message);
    if (S.needsFullRender) {
      S.needsFullRender = false;
      renderMessages({ scrollBottom: S.autoScroll && !S.paused });
    } else if (visible) {
      appendMessageCard(message);
    }
    updateNavCounts();
    renderChatHeader(filteredMessages().length);
    scheduleFriendsRender();
    if (visible) {
      if (S.autoScroll && !S.paused) scrollMessagesToBottom(false);
      else {
        S.unseen += 1;
        updateJumpLatest();
      }
    }
  }

  const scheduleFriendsRender = debounce(function () {
    renderFriends();
    renderTopbar();
  }, 350);

  function daySeparator(ts) {
    const sep = el('div', 'day-sep');
    sep.appendChild(el('span', null, dayLabel(ts)));
    return sep;
  }

  function renderMessages(options) {
    const opts = options || {};
    const list = filteredMessages();
    const visible = list.slice(-MAX_CARDS);
    const fragment = document.createDocumentFragment();
    let lastDay = '';
    visible.forEach(function (message) {
      const day = dayKey(message.ts);
      if (day !== lastDay) {
        fragment.appendChild(daySeparator(message.ts));
        lastDay = day;
      }
      fragment.appendChild(messageCard(message, { showFriend: !S.activeFriend }));
    });
    dom.messageList.replaceChildren(fragment);
    S.lastRenderedDay = lastDay;
    updateMessageEmpty(list.length);
    renderChatHeader(list.length);
    updateJumpLatest();
    if (opts.scrollBottom) {
      requestAnimationFrame(function () {
        scrollMessagesToBottom(false);
      });
    }
  }

  function appendMessageCard(message) {
    const day = dayKey(message.ts);
    if (day !== S.lastRenderedDay) {
      dom.messageList.appendChild(daySeparator(message.ts));
      S.lastRenderedDay = day;
    }
    dom.messageList.appendChild(messageCard(message, { showFriend: !S.activeFriend, animate: true }));
    trimMessageDom();
    updateMessageEmpty(filteredMessages().length);
  }

  function trimMessageDom() {
    let count = dom.messageList.querySelectorAll('.msg').length;
    while (count > MAX_CARDS && dom.messageList.firstElementChild) {
      const first = dom.messageList.firstElementChild;
      if (first.classList.contains('msg')) count -= 1;
      first.remove();
    }
    // 去掉被裁掉消息遗留的孤立日期分隔
    let lead = dom.messageList.firstElementChild;
    while (lead && lead.classList.contains('day-sep')) {
      if (lead.nextElementSibling && lead.nextElementSibling.classList.contains('day-sep')) {
        lead.remove();
        lead = dom.messageList.firstElementChild;
      } else {
        break;
      }
    }
  }

  function messageCard(message, options) {
    const opts = options || {};
    const isSelf = message.sender === 'self';
    const isVideo = messageIsVideo(message);
    const kind = isVideo ? 'video' : KIND_LABELS[message.kind] ? message.kind : 'unknown';
    const extraEarly = message.extra || {};
    const walletType =
      message.kind === 'redpacket' ||
      message.kind === 'transfer' ||
      extraEarly.app_type === 'hongbao' ||
      extraEarly.app_type === 'transfer';
    const row = el(
      'div',
      'msg ' +
        (isSelf ? 'msg--self ' : 'msg--friend ') +
        (kind === 'system' && !walletType ? 'msg--system ' : '') +
        (opts.animate ? 'msg--new' : '')
    );
    row.dataset.id = message.id;
    if (kind === 'system' && !walletType) {
      row.appendChild(el('div', 'msg__system', message.text || '系统消息'));
      return row;
    }

    const extra = message.extra || {};
    const senderName = extra && extra.sender_name ? String(extra.sender_name) : '';
    const isGroupMessage = !!extra.is_group || isGroupName(message.friend);
    const memberKey = String(senderName || extra.sender_key || extra.sender_initial || '');
    let avatarText;
    let avatarHueSource;
    if (isSelf) {
      avatarText = '我';
      avatarHueSource = message.friend;
    } else if (memberKey) {
      // 群成员：用 sender_name / sender_key / sender_initial 计算首字母与颜色，避免同群同色同字
      avatarText = avatarInitial(
        senderName || String(extra.sender_key || '') || memberKey,
        extra.sender_initial || contactInitial(senderName)
      );
      avatarHueSource = memberKey;
    } else {
      avatarText = avatarInitial(
        message.friend,
        contactInitial(message.friend) || groupInitial(message.friend)
      );
      avatarHueSource = message.friend;
    }
    const avatar = el('div', 'msg__avatar', avatarText);
    avatar.style.setProperty('--hue', String(hashHue(avatarHueSource)));
    row.appendChild(avatar);

    const main = el('div', 'msg__main');
    const meta = el('div', 'msg__meta');
    if (opts.showFriend && message.friend) {
      meta.appendChild(el('span', 'msg__friend', message.friend));
    }
    if (!opts.showFriend) {
      // 群消息有成员名时不再重复显示「对方」
      if (isSelf) meta.appendChild(el('span', 'msg__name', '我'));
      else if (!senderName) meta.appendChild(el('span', 'msg__name', message.sender_label || '对方'));
    } else if (isSelf) {
      meta.appendChild(el('span', 'msg__name', '我'));
    }
    if (senderName) {
      meta.appendChild(el('span', 'msg__member', senderName));
    }
    if (kind !== 'text' && !walletType) {
      meta.appendChild(el('span', 'kind-tag kind-tag--' + kind, KIND_LABELS[kind]));
    }
    meta.appendChild(el('span', 'msg__time', message.time_text || formatTime(message.ts)));
    main.appendChild(meta);

    const content = buildMessageContent(message);
    if (content) main.appendChild(content);
    row.appendChild(main);
    return row;
  }

  function buildMessageContent(message) {
    const extra = message.extra || {};
    if (messageIsVideo(message)) return buildVideoContent(message);
    if (message.kind === 'redpacket' || extra.app_type === 'hongbao') {
      const bubble = el('div', 'bubble bubble--hongbao', message.text || '🧧 微信红包');
      bubble.title = '微信红包';
      return bubble;
    }
    if (message.kind === 'transfer' || extra.app_type === 'transfer') {
      const bubble = el('div', 'bubble bubble--transfer', message.text || '[微信转账]');
      bubble.title = '微信转账';
      return bubble;
    }
    if (message.kind === 'image' || message.kind === 'emoji') return buildMediaContent(message);
    if (message.kind === 'file') return buildFileContent(message);
    if (message.kind === 'voice') return buildVoiceContent(message);
    if (message.kind === 'unknown' && extra.notice) return el('div', 'bubble', extra.notice);
    return el('div', 'bubble', message.text || '（空消息）');
  }

  /**
   * 紧凑媒体失效占位卡片（消息与朋友圈共用）。
   * label 例：「图片已失效」；rawPath 用于显示文件名与点击查看完整路径。
   */
  function brokenMediaCard(label, rawPath) {
    const pathText = String(rawPath || '').replace(/\\/g, '/');
    const fileName =
      pathText && !/^(data:|blob:)/i.test(pathText) && !isMomentsMediaKey(pathText)
        ? pathText.split('/').pop()
        : '';
    const card = el('button', 'media-broken-card');
    card.type = 'button';
    card.title = pathText
      ? '文件已被清理或未缓存\n' + pathText + '\n（点击查看完整路径）'
      : '媒体文件未缓存到本地';
    card.appendChild(iconEl('image', 15));
    card.appendChild(
      el('span', 'media-broken-label', label + (fileName ? ' · ' + fileName : ''))
    );
    card.addEventListener('click', function (event) {
      event.stopPropagation();
      toast(
        pathText && !/^(data:|blob:)/i.test(pathText)
          ? '媒体文件不可用：' + truncate(pathText, 120)
          : '媒体文件未缓存到本地',
        'warn',
        5000
      );
    });
    return card;
  }

  function mediaMissingCard(message, title) {
    const kind = message.kind;
    const rawPath = message.media_path || message.thumb_path || '';
    return brokenMediaCard(title || (kind === 'emoji' ? '表情包已失效' : '图片已失效'), rawPath);
  }

  /** 朋友圈九宫格里单格图片失效时的紧凑占位内容（填满格子，不出现破图/空白） */
  function brokenCellContent() {
    const box = el('div', 'media-cell-broken');
    box.appendChild(iconEl('image', 16));
    box.appendChild(el('span', null, '图片已失效'));
    return box;
  }

  function buildMediaContent(message) {
    const extra = message.extra || {};
    const kind = message.kind;
    const path = message.media_path || message.thumb_path || '';
    const url = path ? mediaUrl(path) : '';
    const holder = el('div', 'media-wrap');

    if (!url) {
      return mediaMissingCard(message, kind === 'emoji' ? '表情包未缓存' : '图片未缓存');
    }

    const box = el('button', 'media-box' + (kind === 'emoji' ? ' media-box--emoji' : ''));
    box.type = 'button';
    box.title = '点击查看大图';
    const img = el('img');
    img.src = url;
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = kind === 'emoji' ? '表情包' : '图片';
    box.appendChild(img);
    box.addEventListener('click', function () {
      openLightbox(url, KIND_LABELS[kind] + ' · ' + (message.friend || '') + ' · ' + (message.time_text || ''));
    });
    img.addEventListener('error', function () {
      // 文件被清理 / 未缓存：用占位卡片替换，避免浏览器破图或巨大空白框
      holder.replaceChildren(mediaMissingCard(message, kind === 'emoji' ? '表情包已失效' : '图片已失效'));
    });
    holder.appendChild(box);

    const caption = [];
    if (extra.animated || /\.gif$/i.test(path)) caption.push('GIF');
    else if (extra.n_frames && Number(extra.n_frames) > 1) caption.push(extra.n_frames + ' 帧');
    if (extra.size_text) caption.push(String(extra.size_text));
    if (caption.length) holder.appendChild(el('div', 'media-meta', caption.join(' · ')));
    return holder;
  }

  function buildFileContent(message) {
    const extra = message.extra || {};
    const name = message.text || extra.original_name || '未命名文件';
    const size = extra.size_text ? String(extra.size_text) : '';
    const url = message.media_path ? mediaUrl(message.media_path) : '';
    const card = el(url ? 'a' : 'div', 'file-card');
    if (url) {
      card.href = url;
      card.setAttribute('download', '');
      card.title = '下载文件';
      card.addEventListener('click', function (event) {
        event.preventDefault();
        downloadFile(url, name, card);
      });
    }
    const icon = el('span', 'file-icon');
    icon.appendChild(iconEl('file', 19));
    card.appendChild(icon);

    const info = el('div', 'file-info');
    info.appendChild(el('div', 'file-name', name));
    info.appendChild(el('div', 'file-meta', size ? '文件 · ' + size : url ? '文件 · 点击下载' : '文件 · 本地未缓存'));
    card.appendChild(info);

    if (url) {
      const download = el('span', 'file-dl');
      download.appendChild(iconEl('download', 15));
      card.appendChild(download);
    }
    return card;
  }

  async function downloadFile(url, name, card) {
    if (card.classList.contains('is-busy')) return;
    card.classList.add('is-busy');
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error('文件已失效（HTTP ' + response.status + '），可能已被清理');
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = el('a');
      link.href = objectUrl;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(objectUrl);
      }, 4000);
      toast('开始下载：' + truncate(name, 40), 'ok', 2000);
    } catch (err) {
      toast('文件下载失败：' + err.message, 'error', 4200);
    } finally {
      card.classList.remove('is-busy');
    }
  }

  // ---- v7：视频消息（本机缓存可播放，未缓存显示明确占位）----
  function messageIsVideo(message) {
    if (!message) return false;
    const extra = message.extra || {};
    return message.kind === 'video' || extra.app_type === 'video';
  }

  function videoDurationLabel(message) {
    const extra = (message && message.extra) || {};
    const raw = Number(extra.duration);
    if (isFinite(raw) && raw > 0) return formatVideoDuration(raw);
    const matched = /(\d+(?:\.\d+)?)\s*秒/.exec(String((message && message.text) || ''));
    if (matched) return formatVideoDuration(Number(matched[1]));
    return '';
  }

  function videoSrcUrl(message) {
    const extra = (message && message.extra) || {};
    const localId = extra.db_local_id;
    if (!message || !message.friend || localId === undefined || localId === null || localId === '' || Number(localId) <= 0) {
      return '';
    }
    return (
      '/api/video?friend=' +
      encodeURIComponent(String(message.friend)) +
      '&local_id=' +
      encodeURIComponent(String(localId)) +
      '&token=' +
      encodeURIComponent(TOKEN)
    );
  }

  function videoFallbackCard(title, desc, duration) {
    const card = el('div', 'video-fallback');
    card.appendChild(iconEl('video', 18));
    const main = el('div', 'video-fallback-main');
    main.appendChild(el('div', 'video-fallback-title', title));
    if (desc) main.appendChild(el('div', 'video-fallback-desc', desc));
    card.appendChild(main);
    if (duration) card.appendChild(el('span', 'video-fallback-duration', duration));
    return card;
  }

  function buildVideoContent(message) {
    const extra = message.extra || {};
    const duration = videoDurationLabel(message);
    const url = videoSrcUrl(message);
    if (!url) {
      return videoFallbackCard('视频（未缓存）', '微信只保留播放过的视频', duration);
    }
    const wrap = el('div', 'msg-video-wrap');
    const video = el('video', 'msg-video');
    video.controls = true;
    video.preload = 'metadata';
    video.setAttribute('playsinline', '');
    video.setAttribute('webkit-playsinline', '');
    video.addEventListener('error', function () {
      const fallback = videoFallbackCard(
        '视频未缓存',
        '无法播放（微信只保留播放过的视频）',
        duration
      );
      if (wrap.parentNode) wrap.replaceWith(fallback);
    });
    video.src = url;
    wrap.appendChild(video);
    if (duration) wrap.appendChild(el('span', 'video-duration', duration));
    if (extra.video_md5) video.title = '视频 · ' + String(extra.video_md5);
    return wrap;
  }

  function buildVoiceContent(message) {
    const extra = message.extra || {};
    const box = el('div', 'voice');
    box.appendChild(iconEl('mic', 15));
    const wave = el('span', 'wave');
    for (let i = 0; i < 6; i++) wave.appendChild(el('i'));
    box.appendChild(wave);
    const duration = formatDuration(extra.duration);
    box.appendChild(el('span', 'voice-dur', duration || '语音'));
    box.appendChild(el('span', 'voice-note', '未接收'));
    return box;
  }

  function updateMessageEmpty(count) {
    const empty = count === 0;
    dom.messageEmpty.hidden = !empty;
    if (!empty) return;
    if (!S.bootstrapped && !MOCK) {
      dom.messageEmptyTitle.textContent = '正在加载消息…';
      dom.messageEmptyDesc.textContent = '正在从后端读取首屏快照。';
      return;
    }
    if (!S.messages.length) {
      dom.messageEmptyTitle.textContent = '还没有收到消息';
      dom.messageEmptyDesc.textContent = isFollowMode()
        ? '桌面版正在运行时会把消息写入日志，本页会自动跟随；也可以先在左侧选择好友或切换筛选。'
        : S.state && S.state.running
          ? '在左侧勾选要监听的好友，收到的新消息会自动出现在这里。'
          : '后端尚未运行。可以在顶栏点击电源按钮启动监听，或运行 python web_app.py --fake 体验演示消息。';
      return;
    }
    dom.messageEmptyTitle.textContent = '没有匹配的消息';
    dom.messageEmptyDesc.textContent = '试试切换消息类型筛选，或清空搜索关键词。';
  }

  function renderChatHeader(count) {
    const active = S.activeFriend;
    const activeIsGroup = !!(active && isGroupName(active));
    dom.chatTitle.textContent = active || '全部消息';
    dom.chatGroupBadge.hidden = !activeIsGroup;
    dom.chatAvatar.textContent = active
      ? avatarInitial(active, conversationInitial(active))
      : '全';
    dom.chatAvatar.style.setProperty('--hue', String(hashHue(active || '全部消息')));
    const parts = [];
    const total = S.messages.length;
    const recordTotal = Number(S.state.message_total) || total;
    if (count !== total) parts.push(formatCount(count) + ' / ' + formatCount(total) + ' 条消息');
    else parts.push(formatCount(total) + ' 条消息');
    if (recordTotal > total) parts.push('后端记录 ' + formatCount(recordTotal) + ' 条');
    if (S.kind !== 'all') parts.push(KIND_LABELS[S.kind] + '筛选');
    if (S.msgQuery) parts.push('搜索「' + truncate(S.msgQuery, 18) + '」');
    if (S.monitored.size) {
      parts.push((isFollowMode() ? '本页筛选 ' : '监听中 ') + S.monitored.size + ' 个会话');
    }
    dom.chatSub.textContent = parts.join(' · ');

    // 历史同步按钮：选中好友或群聊时可用；清空聊天在有消息时可用
    const hasConversation = !!active;
    if (dom.syncHistoryBtn) {
      dom.syncHistoryBtn.disabled = !hasConversation;
      dom.syncHistoryBtn.title = hasConversation
        ? '同步「' + active + '」的历史聊天记录（只读本机缓存）'
        : '先在左侧选择一个好友或群聊，再同步历史聊天';
    }
    if (dom.clearChatBtn) {
      dom.clearChatBtn.disabled = S.messages.length === 0;
      dom.clearChatBtn.title = S.messages.length
        ? '清空本页聊天记录（只清内存，不影响微信）'
        : '当前没有聊天记录';
    }
    if (!hasConversation && dom.historyPop && !dom.historyPop.hidden) closeHistoryPop();
  }

  /* ---- v2：同步所选好友/群聊的历史聊天记录（小弹层 5/10/20/50/100/全部） ---- */
  function openHistoryPop() {
    if (!S.activeFriend) {
      toast('请先在左侧选择一个好友或群聊', 'warn', 2400);
      return;
    }
    dom.historyPopFriend.textContent =
      (isGroupName(S.activeFriend) ? '群聊：' : '好友：') + S.activeFriend;
    dom.historyPop.hidden = false;
    dom.historyPop.classList.remove('is-busy');
    dom.syncHistoryBtn.setAttribute('aria-expanded', 'true');
    const first = dom.historyPopOptions.querySelector('button');
    if (first) first.focus();
  }

  function closeHistoryPop() {
    if (!dom.historyPop) return;
    dom.historyPop.hidden = true;
    if (dom.syncHistoryBtn) dom.syncHistoryBtn.setAttribute('aria-expanded', 'false');
  }

  function toggleHistoryPop() {
    if (dom.historyPop.hidden) openHistoryPop();
    else closeHistoryPop();
  }

  function addHistoryMessage(item) {
    if (!item || typeof item !== 'object') return false;
    const message = Object.assign({}, item);
    const extra = message.extra || {};
    // 群聊历史（extra.is_group）先登记群对象，避免混进好友列表
    if ((extra.is_group || isGroupName(message.friend)) && message.friend) {
      ensureGroup(message.friend);
    }
    if (!message.id) {
      // 后端正常都会给 id；缺失时兜底生成一个稳定 id，避免重复合并
      const seed = [
        message.friend,
        message.sender,
        message.kind,
        message.text,
        Number(message.ts) || 0,
      ].join('|');
      message.id = 'hist-' + hashHue(seed) + '-' + Math.round((Number(message.ts) || 0) * 1000);
    }
    return addMessage(message);
  }

  function refreshFriendStats(friendName) {
    const friend = ensureFriend(friendName);
    const list = S.messages.filter(function (message) {
      return message.friend === friendName;
    });
    friend.count = list.length;
    friend.last_ts = list.length
      ? list.reduce(function (max, message) {
          return Math.max(max, Number(message.ts) || 0);
        }, 0)
      : 0;
  }

  function setHistoryOptionsDisabled(disabled) {
    dom.historyPopOptions.querySelectorAll('button').forEach(function (button) {
      button.disabled = !!disabled;
    });
  }

  async function loadHistory(limit) {
    const friend = S.activeFriend;
    if (!friend) {
      toast('请先在左侧选择一位好友', 'warn', 2400);
      return;
    }
    if (S.historyLoading) return;
    const requested = Number(limit) || 0;
    S.historyLoading = true;
    dom.historyPop.classList.add('is-busy');
    setHistoryOptionsDisabled(true);
    const label = requested ? requested + ' 条' : '全部';
    toast('正在同步「' + friend + '」的 ' + label + ' 历史聊天…', 'info', 1800);

    try {
      if (MOCK) {
        mockLoadHistory(friend, requested);
      } else {
        const data = await api('/api/history/load', {
          method: 'POST',
          body: { friend: friend, limit: requested },
        });
        const items = Array.isArray(data && data.items) ? data.items : [];
        let added = 0;
        items.forEach(function (item) {
          if (addHistoryMessage(item)) added += 1;
        });
        refreshFriendStats(friend);
        S.state.message_total = Math.max(Number(S.state.message_total) || 0, S.messages.length);
        renderMessages({ scrollBottom: true });
        renderFriends();
        renderTopbar();
        updateNavCounts();
        renderChatHeader(filteredMessages().length);
        if (data && data.notice) toast(data.notice, 'warn', 5200);
        else {
          const loaded = Number(data && data.loaded);
          const synced = Number.isFinite(loaded) ? loaded : added;
          toast(
            '已同步 ' + synced + ' 条历史消息' +
              (data && data.available ? '（本机可用 ' + data.available + ' 条）' : ''),
            'ok',
            3200
          );
        }
      }
    } catch (err) {
      toast('同步历史失败：' + err.message, 'error', 4600);
    } finally {
      S.historyLoading = false;
      dom.historyPop.classList.remove('is-busy');
      setHistoryOptionsDisabled(false);
      closeHistoryPop();
    }
  }

  /* ---- v2：清空聊天记录（只清网页端内存） ---- */
  async function clearChat() {
    if (!S.messages.length) {
      toast('当前没有聊天记录', 'info', 2000);
      return;
    }
    if (!window.confirm('确定清空本页的聊天记录吗？\n（只清空网页端内存，不影响微信和桌面版日志）')) {
      return;
    }
    if (MOCK) {
      clearLocalMessages();
      toast('已清空聊天记录（演示，仅内存）', 'ok', 2400);
      return;
    }
    S.localClearInFlight = true;
    try {
      const data = await api('/api/messages/clear', { method: 'POST', body: {} });
      const cleared = data && data.cleared !== undefined ? data.cleared : S.messages.length;
      clearLocalMessages();
      toast('已清空聊天记录' + (cleared ? '（' + cleared + ' 条）' : ''), 'ok', 2600);
    } catch (err) {
      toast('清空聊天失败：' + err.message, 'error', 4200);
    } finally {
      setTimeout(function () {
        S.localClearInFlight = false;
      }, 1500);
    }
  }

  function clearLocalMessages() {
    S.messages = [];
    S.msgIndex.clear();
    S.state.message_total = 0;
    S.friends.forEach(function (friend) {
      friend.count = 0;
      friend.last_ts = 0;
    });
    S.paused = false;
    S.unseen = 0;
    clearLogs();
    renderMessages({ scrollBottom: true });
    renderFriends();
    renderTopbar();
    renderChatHeader(0);
    updateNavCounts();
  }

  function scrollMessagesToBottom(smooth) {
    const node = dom.messageScroll;
    if (!node) return;
    // 程序化滚动期间忽略「用户上滑」判定，避免浮标闪烁
    scrollLockUntil = Date.now() + (smooth ? 700 : 200);
    if (smooth) node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' });
    else node.scrollTop = node.scrollHeight;
  }

  let scrollLockUntil = 0;

  function updateJumpLatest() {
    const show = S.paused || S.unseen > 0;
    dom.jumpLatest.hidden = !show;
    dom.jumpLatestCount.textContent = formatCount(S.unseen);
    dom.jumpLatestCount.hidden = S.unseen === 0;
    dom.jumpLatest.title = S.unseen > 0 ? '有 ' + S.unseen + ' 条新消息，点击回到最新' : '回到最新消息';
  }

  function setKind(kind) {
    S.kind = kind;
    dom.kindFilters.querySelectorAll('.chip').forEach(function (chip) {
      chip.classList.toggle('is-active', chip.dataset.kind === S.kind);
    });
    S.paused = false;
    S.unseen = 0;
    renderMessages({ scrollBottom: true });
  }

  // ===========================================================================
  // 5. 朋友圈
  // ===========================================================================
  function momentsUnavailable() {
    return !!(S.moments.status && S.moments.status.available === false);
  }

  function filteredMomentItems() {
    const query = S.moments.query.trim().toLowerCase();
    return S.moments.items.filter(function (item) {
      if (S.moments.author && item.author !== S.moments.author) return false;
      const media = Array.isArray(item.media) ? item.media : [];
      if (S.moments.onlyImages && !media.length) return false;
      const comments = Array.isArray(item.comments) ? item.comments : [];
      if (S.moments.onlyComments && !comments.length) return false;
      if (query) {
        const hay = [
          item.content,
          item.author,
          item.location,
          item.link && item.link.title,
          item.link && item.link.url,
        ]
          .concat(Array.isArray(item.likes) ? item.likes : [])
          .concat(
            comments.map(function (comment) {
              return (comment.author || '') + ' ' + (comment.content || '');
            })
          )
          .filter(Boolean)
          .join('\n')
          .toLowerCase();
        if (hay.indexOf(query) === -1) return false;
      }
      return true;
    });
  }

  function renderMomentsAll() {
    renderMomentsSide();
    renderMomentsMain();
    updateMomentsFreshness();
    updateNavCounts();
  }

  // ---- 朋友圈新鲜度提示条（本地缓存最新一条 > 48h 时提示去微信刷新/同步）----
  let freshnessBar = null;
  let freshnessText = null;
  let freshnessSyncBtn = null;

  function ensureFreshnessBar() {
    if (freshnessBar || !dom.momentsMain) return;
    freshnessBar = el('div', 'moments-freshness');
    freshnessBar.id = 'momentsFreshness';
    freshnessBar.hidden = true;

    const icon = el('span', 'moments-freshness-icon');
    icon.appendChild(iconEl('info', 15));

    freshnessText = el('div', 'moments-freshness-text');

    const actions = el('div', 'moments-freshness-actions');
    freshnessSyncBtn = el('button', 'btn btn-ghost btn-sm', '去同步');
    freshnessSyncBtn.type = 'button';
    freshnessSyncBtn.title = '重新同步朋友圈';
    freshnessSyncBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      syncMoments();
    });
    const close = el('button', 'icon-btn icon-btn--sm moments-freshness-close', '×');
    close.type = 'button';
    close.title = '关闭提示';
    close.setAttribute('aria-label', '关闭提示');
    close.addEventListener('click', function (event) {
      event.stopPropagation();
      S.moments.freshnessDismissed = true;
      freshnessBar.hidden = true;
    });
    actions.appendChild(freshnessSyncBtn);
    actions.appendChild(close);

    freshnessBar.appendChild(icon);
    freshnessBar.appendChild(freshnessText);
    freshnessBar.appendChild(actions);

    if (dom.momentsSyncStrip && dom.momentsSyncStrip.parentNode === dom.momentsMain) {
      dom.momentsMain.insertBefore(freshnessBar, dom.momentsSyncStrip);
    } else {
      dom.momentsMain.appendChild(freshnessBar);
    }
  }

  function updateMomentsFreshness() {
    ensureFreshnessBar();
    if (!freshnessBar) return;
    const items = S.moments.items || [];
    if (!items.length || S.moments.freshnessDismissed) {
      freshnessBar.hidden = true;
      return;
    }
    let latest = 0;
    items.forEach(function (item) {
      const ts = Number(item.create_time) || 0;
      if (ts > latest) latest = ts;
    });
    if (!latest) {
      freshnessBar.hidden = true;
      return;
    }
    const age = nowSec() - latest;
    if (age <= 48 * 3600) {
      freshnessBar.hidden = true;
      return;
    }
    const days = Math.max(1, Math.floor(age / 86400));
    freshnessText.replaceChildren(
      el('span', null, '本地朋友圈最新一条是 '),
      el('b', null, formatMomentDisplay(latest)),
      el(
        'span',
        null,
        '（' + days + ' 天前）。朋友圈只缓存电脑微信浏览过的内容，请先在电脑微信打开朋友圈刷新，再点「同步朋友圈」。'
      )
    );
    freshnessBar.hidden = false;
    if (freshnessSyncBtn) {
      freshnessSyncBtn.disabled = !!(S.moments.syncing || (S.moments.status || {}).syncing);
    }
  }

  function renderMomentsSide() {
    const st = S.moments.status || {};
    const unavailable = st.available === false;
    const account = st.account || S.moments.account || {};
    const accountName = account.nick_name || account.wxid || '我的朋友圈';
    const count = Number(st.count !== undefined ? st.count : S.moments.items.length) || 0;
    const syncedAt = Number(st.synced_at || S.moments.syncedAt) || 0;
    const limit = Number(st.limit || S.moments.lastLimit || S.moments.syncLimit || 10) || 10;
    S.moments.lastLimit = limit;
    if (!S.moments.syncLimitTouched) S.moments.syncLimit = limit;
    if (dom.momentsLimitSelect && String(S.moments.syncLimit) !== dom.momentsLimitSelect.value) {
      dom.momentsLimitSelect.value = String(S.moments.syncLimit);
    }

    dom.momentsAccountName.textContent = unavailable ? '朋友圈不可用' : accountName;
    dom.momentsAccountAvatar.textContent = unavailable
      ? '!'
      : avatarInitial(accountName, contactInitial(accountName));
    dom.momentsAccountAvatar.style.setProperty('--hue', String(hashHue(accountName)));
    if (unavailable) {
      dom.momentsAccountMeta.textContent = truncate(st.reason || '无法读取本机朋友圈数据', 40);
    } else if (syncingNow()) {
      dom.momentsAccountMeta.textContent = '正在同步最近 ' + limit + ' 条…';
    } else if (count) {
      dom.momentsAccountMeta.textContent =
        '已同步 ' + formatCount(count) + ' 条（最近 ' + limit + ' 条）' +
        (syncedAt ? ' · ' + relTime(syncedAt) : '');
    } else {
      dom.momentsAccountMeta.textContent = '尚未同步 · 数据仅在内存';
    }
    dom.momentsSideCount.textContent = formatCount(count);

    const syncing = syncingNow();
    setMomentsSyncButtons(syncing, unavailable);
    if (dom.momentsLimitSelect) dom.momentsLimitSelect.disabled = syncing || unavailable;
    if (dom.momentsClearBtn) dom.momentsClearBtn.disabled = syncing || unavailable || !S.moments.items.length;
    dom.momentsOnlyImages.checked = S.moments.onlyImages;
    dom.momentsOnlyComments.checked = S.moments.onlyComments;
    // v8：自动同步开关（status.auto 缺失时按默认开启显示）
    if (dom.momentsAutoToggle) {
      dom.momentsAutoToggle.checked = S.moments.auto !== false;
      dom.momentsAutoToggle.disabled = !!S.moments.autoSaving;
    }
    if (dom.momentsAutoRow) {
      dom.momentsAutoRow.classList.toggle('is-saving', !!S.moments.autoSaving);
      const hint = dom.momentsAutoRow.querySelector('.auto-sync-hint');
      if (hint) {
        hint.textContent = '监听期间生效；每 30 分钟自动刷新微信朋友圈（会短暂切窗口）';
      }
      if (dom.momentsAutoStatus) {
        // status.listening 可用时显示真实监听状态；没有该字段回退到自动同步开关状态
        let statusText = '';
        let statusClass = '';
        if (S.moments.auto === false) {
          statusText = '自动同步已关闭';
          statusClass = 'is-off';
        } else if (st.listening === true) {
          statusText = '实时监听中，自动检查已生效';
          statusClass = 'is-on';
        } else if (st.listening === false) {
          statusText = '实时监听已结束，自动检查已暂停';
          statusClass = 'is-off';
        } else {
          statusText = '自动同步已开启';
          statusClass = 'is-on';
        }
        dom.momentsAutoStatus.hidden = false;
        dom.momentsAutoStatus.textContent = statusText;
        dom.momentsAutoStatus.classList.toggle('is-on', statusClass === 'is-on');
        dom.momentsAutoStatus.classList.toggle('is-off', statusClass === 'is-off');
      }
    }

    if (unavailable) {
      dom.momentsSideFoot.textContent = st.reason || '朋友圈暂不可用，不影响消息监听。';
      dom.momentsSideFoot.classList.add('is-error');
    } else if (syncing) {
      dom.momentsSideFoot.textContent = '正在同步朋友圈…';
      dom.momentsSideFoot.classList.remove('is-error');
    } else if (S.moments.error || st.error) {
      dom.momentsSideFoot.textContent = '上次同步失败：' + truncate(S.moments.error || st.error, 60);
      dom.momentsSideFoot.classList.add('is-error');
    } else if (syncedAt) {
      dom.momentsSideFoot.textContent = '最近同步：' + formatFullTime(syncedAt);
      dom.momentsSideFoot.classList.remove('is-error');
    } else {
      dom.momentsSideFoot.textContent = '尚未同步 · 点击上方按钮读取朋友圈';
      dom.momentsSideFoot.classList.remove('is-error');
    }

    renderMomentAuthors();
  }

  function setMomentsSyncButtons(syncing, unavailable) {
    [dom.momentsSideSyncBtn, dom.momentsSyncBtn].forEach(function (button) {
      if (!button) return;
      button.disabled = syncing || unavailable;
      button.classList.toggle('is-syncing', syncing);
      button.title = unavailable
        ? S.moments.status.reason || '朋友圈暂不可用'
        : syncing
          ? '正在同步，请稍候…'
          : '从本机微信缓存同步朋友圈';
      const label = button.querySelector('.btn-text');
      if (label) label.textContent = syncing ? '同步中…' : '同步朋友圈';
    });
  }

  /** v8：切换朋友圈自动同步（失败回滚开关） */
  async function setMomentsAuto(enabled) {
    if (S.moments.autoSaving) return;
    const target = !!enabled;
    const previous = S.moments.auto;
    S.moments.autoSaving = true;
    S.moments.auto = target;
    renderMomentsSide();
    try {
      if (MOCK) {
        await new Promise(function (resolve) {
          setTimeout(resolve, 120);
        });
        S.moments.status = Object.assign({}, S.moments.status, {
          auto: target,
          auto_interval: S.moments.autoInterval || 30,
        });
      } else {
        const data = await api('/api/moments/auto', {
          method: 'POST',
          body: { enabled: target, interval: S.moments.autoInterval || 30 },
        });
        const payload = (data && data.status) || data || {};
        applyMomentsAutoStatus(payload);
        if (data && data.enabled !== undefined) S.moments.auto = !!data.enabled;
        S.moments.status = Object.assign({}, S.moments.status, {
          auto: S.moments.auto,
          auto_interval: S.moments.autoInterval,
        });
      }
      toast('朋友圈自动同步已' + (target ? '开启' : '关闭'), 'ok', 2600);
    } catch (err) {
      S.moments.auto = previous;
      toast('设置自动同步失败：' + err.message, 'error', 4200);
    } finally {
      S.moments.autoSaving = false;
      renderMomentsSide();
    }
  }

  // ---- v10：实验性聊天视频自动缓存开关（默认关闭） ----
  function applyVideoAutoStatus(data) {
    const status = (data && data.status) || data || {};
    if (status.enabled !== undefined) S.videoAuto.enabled = !!status.enabled;
    if (status.last_result !== undefined) S.videoAuto.lastResult = String(status.last_result || '');
    if (status.last_error !== undefined) S.videoAuto.lastError = String(status.last_error || '');
    if (data && data.disabled !== undefined) S.videoAuto.available = !data.disabled;
    if (status.disabled !== undefined) S.videoAuto.available = !status.disabled;
    if (data && data.reason) S.videoAuto.reason = String(data.reason);
    S.videoAuto.known = true;
  }

  function updateVideoAutoButton() {
    if (!dom.videoAutoToggle) return;
    const button = dom.videoAutoToggle;
    const auto = S.videoAuto;
    button.classList.toggle('is-active', !!auto.enabled);
    button.setAttribute('aria-pressed', auto.enabled ? 'true' : 'false');
    button.disabled = !!auto.busy || !auto.available;
    let extra = '';
    if (!auto.available) {
      extra = '\n不可用：' + (auto.reason || '当前后端未启用该实验功能');
    } else {
      if (auto.lastResult) extra += '\n最近：' + auto.lastResult;
      if (auto.lastError) extra += '\n最近错误：' + auto.lastError;
    }
    button.title =
      '实验性功能：会切到对应聊天、点一次视频让微信缓存；会短暂抢窗口，可能点偏，失败只记录' +
      extra;
  }

  async function loadVideoAutoStatus() {
    if (MOCK) {
      S.videoAuto.enabled = false;
      S.videoAuto.available = true;
      S.videoAuto.known = true;
      updateVideoAutoButton();
      return;
    }
    try {
      const data = await api('/api/video/auto');
      applyVideoAutoStatus(data);
    } catch (err) {
      S.videoAuto.available = false;
      S.videoAuto.known = true;
      S.videoAuto.reason =
        err.status === 503
          ? err.message || '实验性视频缓存不可用'
          : err.status === 404
            ? '后端未提供实验性视频缓存接口'
            : err.message || '实验性视频缓存不可用';
    }
    updateVideoAutoButton();
  }

  async function toggleVideoAuto() {
    if (S.videoAuto.busy) return;
    if (!S.videoAuto.available) {
      toast('实验性视频缓存不可用：' + (S.videoAuto.reason || '当前后端未启用该功能'), 'error', 4400);
      return;
    }
    const target = !S.videoAuto.enabled;
    const previous = S.videoAuto.enabled;
    S.videoAuto.enabled = target;
    S.videoAuto.busy = true;
    updateVideoAutoButton();
    try {
      let data;
      if (MOCK) {
        await new Promise(function (resolve) {
          setTimeout(resolve, 150);
        });
        data = { ok: true, status: { enabled: target, last_result: '演示模式：已模拟切换' } };
      } else {
        data = await api('/api/video/auto', { method: 'POST', body: { enabled: target } });
      }
      applyVideoAutoStatus(data);
      toast('实验性视频缓存已' + (target ? '开启' : '关闭'), 'ok', 2800);
    } catch (err) {
      S.videoAuto.enabled = previous;
      if (err.status === 503) {
        S.videoAuto.available = false;
        S.videoAuto.reason = err.message || '实验性视频缓存不可用';
        toast('实验性视频缓存不可用：' + (err.message || '当前后端未启用该功能'), 'error', 4600);
      } else {
        toast('设置实验性视频缓存失败：' + err.message, 'error', 4200);
      }
    } finally {
      S.videoAuto.busy = false;
      updateVideoAutoButton();
    }
  }

  function renderMomentAuthors() {
    const query = S.moments.authorQuery.trim().toLowerCase();    const map = new Map();
    S.moments.items.forEach(function (item) {
      const name = item.author || '未知';
      map.set(name, (map.get(name) || 0) + 1);
    });
    const authors = Array.from(map.entries()).sort(function (a, b) {
      return b[1] - a[1];
    });
    const visible = authors.filter(function (entry) {
      return !query || entry[0].toLowerCase().indexOf(query) !== -1;
    });

    dom.momentAuthors.replaceChildren();
    dom.momentsAuthorCount.textContent = String(authors.length);

    const allRow = el('div', 'author-row' + (S.moments.author ? '' : ' is-active'));
    allRow.tabIndex = 0;
    const allAvatar = el('span', 'avatar', '全');
    allRow.appendChild(allAvatar);
    allRow.appendChild(el('span', 'author-name', '全部作者'));
    allRow.appendChild(el('span', 'author-count', String(S.moments.items.length)));
    allRow.addEventListener('click', function () {
      S.moments.author = '';
      renderMomentsAll();
    });
    dom.momentAuthors.appendChild(allRow);

    visible.forEach(function (entry) {
      const name = entry[0];
      const row = el('div', 'author-row' + (S.moments.author === name ? ' is-active' : ''));
      row.tabIndex = 0;
      const avatar = el('span', 'avatar', firstChar(name));
      avatar.style.setProperty('--hue', String(hashHue(name)));
      row.appendChild(avatar);
      row.appendChild(el('span', 'author-name', name));
      row.appendChild(el('span', 'author-count', String(entry[1])));
      row.addEventListener('click', function () {
        S.moments.author = S.moments.author === name ? '' : name;
        renderMomentsAll();
      });
      dom.momentAuthors.appendChild(row);
    });

    if (!visible.length && authors.length) {
      dom.momentAuthors.appendChild(el('div', 'friend-empty', '没有匹配的作者'));
    }
  }

  function renderMomentsMain() {
    const st = S.moments.status || {};
    const unavailable = st.available === false;
    const syncing = !!(S.moments.syncing || st.syncing);
    const items = filteredMomentItems();
    const total = Number(st.count !== undefined ? st.count : S.moments.items.length) || 0;
    const syncedAt = Number(st.synced_at || S.moments.syncedAt) || 0;

    const subParts = [formatCount(total) + ' 条'];
    if (syncedAt) subParts.push('同步于 ' + relTime(syncedAt));
    if (S.moments.author) subParts.push('作者：' + S.moments.author);
    if (S.moments.query) subParts.push('搜索「' + truncate(S.moments.query, 16) + '」');
    if (S.moments.onlyImages) subParts.push('只看有图片');
    if (S.moments.onlyComments) subParts.push('只看有评论');
    dom.momentsSub.textContent = subParts.join(' · ');

    dom.momentAuthorChip.hidden = !S.moments.author;
    dom.momentAuthorName.textContent = S.moments.author;

    dom.momentsSyncStrip.hidden = !syncing;
    dom.momentsSyncText.textContent = syncing ? '正在同步朋友圈，请稍候…' : '';

    const showSkeleton = (S.moments.loading || syncing) && !S.moments.items.length;
    dom.momentsSkeleton.hidden = !showSkeleton;

    if (showSkeleton) {
      dom.momentList.replaceChildren();
      dom.momentsState.hidden = true;
      return;
    }

    if (items.length) {
      dom.momentsState.hidden = true;
      const fragment = document.createDocumentFragment();
      items.slice(0, MAX_MOMENTS).forEach(function (item) {
        fragment.appendChild(momentCard(item));
      });
      dom.momentList.replaceChildren(fragment);
      return;
    }

    dom.momentList.replaceChildren();
    dom.momentsState.hidden = false;
    renderMomentsState(unavailable, syncing);
  }

  function renderMomentsState(unavailable, syncing) {
    const st = S.moments.status || {};
    const actions = dom.momentsStateActions;
    actions.replaceChildren();

    function setState(icon, title, desc, buttons) {
      dom.momentsStateIcon.innerHTML = svg(icon, 24);
      dom.momentsStateTitle.textContent = title;
      dom.momentsStateDesc.textContent = desc;
      (buttons || []).forEach(function (button) {
        actions.appendChild(button);
      });
    }

    function makeButton(label, primary, handler) {
      const button = el('button', 'btn ' + (primary ? 'btn-primary' : 'btn-ghost') + ' btn-sm', label);
      button.type = 'button';
      button.addEventListener('click', handler);
      return button;
    }

    if (unavailable) {
      setState(
        'alert',
        '朋友圈暂不可用',
        st.reason || '无法从本机微信读取朋友圈数据。请确认微信已登录，并检查 WechatMoments 依赖是否完整。',
        [makeButton('重试同步', true, syncMoments)]
      );
      return;
    }

    if (S.moments.error || st.error) {
      setState('alert', '朋友圈同步失败', S.moments.error || st.error, [
        makeButton('重试同步', true, syncMoments),
      ]);
      return;
    }

    if (S.moments.items.length) {
      setState('camera', '没有匹配的朋友圈', '当前作者 / 关键词 / 开关筛选后没有结果，试试清空筛选条件。', [
        makeButton('清空筛选', false, function () {
          S.moments.author = '';
          S.moments.query = '';
          S.moments.onlyImages = false;
          S.moments.onlyComments = false;
          dom.momentSearch.value = '';
          dom.momentSearchClear.hidden = true;
          dom.momentAuthorSearch.value = '';
          renderMomentsAll();
        }),
      ]);
      return;
    }

    if (syncing) {
      setState('camera', '正在同步朋友圈…', '首次同步会从本机微信缓存读取并解密图片，请稍候。', []);
      return;
    }

    setState(
      'camera',
      '还没有朋友圈数据',
      '点击「同步朋友圈」从本机微信缓存读取最近的朋友圈。首次同步可能需要几秒到半分钟。',
      [makeButton('同步朋友圈', true, syncMoments)]
    );
  }

  function momentCard(item) {
    const card = el('article', 'moment-card' + (item.id === S.activeMomentId ? ' is-selected' : ''));
    card.dataset.id = item.id;

    const avatar = el(
      'div',
      'moment-avatar',
      avatarInitial(item.author || '?', item.author_initial || contactInitial(item.author))
    );
    avatar.style.setProperty('--hue', String(hashHue(item.author || '')));
    if (item.avatar_key || item.avatar) {
      const src = mediaUrl(item.avatar_key || item.avatar);
      if (src) {
        const img = el('img');
        img.src = src;
        img.alt = '';
        img.loading = 'lazy';
        img.addEventListener('error', function () {
          img.remove();
          avatar.textContent = avatarInitial(
            item.author || '?',
            item.author_initial || contactInitial(item.author)
          );
        });
        avatar.textContent = '';
        avatar.appendChild(img);
      }
    }
    card.appendChild(avatar);

    const body = el('div', 'moment-body');

    const top = el('div', 'moment-top');
    top.appendChild(el('span', 'moment-author', item.author || '未知作者'));
    top.appendChild(el('span', 'moment-time', momentTimeLabel(item)));
    body.appendChild(top);

    const content = String(item.content || '').trim();
    const media = Array.isArray(item.media) ? item.media : [];
    if (content) body.appendChild(el('div', 'moment-content', content));

    if (item.location) {
      const location = el('div', 'moment-location');
      location.appendChild(iconEl('location', 13));
      location.appendChild(el('span', null, item.location));
      body.appendChild(location);
    }

    if (item.link && (item.link.title || item.link.url)) {
      body.appendChild(buildMomentLink(item.link));
    }

    if (media.length) body.appendChild(buildMomentMedia(media, item));
    else if (!content) body.appendChild(el('div', 'moment-content is-empty', '（无正文）'));

    const likes = Array.isArray(item.likes) ? item.likes : [];
    const comments = Array.isArray(item.comments) ? item.comments : [];
    if (likes.length || comments.length) body.appendChild(buildMomentSocial(likes, comments));

    const actions = el('div', 'moment-actions');
    const analyze = el('button', 'moment-action-btn is-accent');
    analyze.type = 'button';
    analyze.title = '把这条朋友圈交给 DeepSeek 分析';
    analyze.appendChild(iconEl('sparkles', 13));
    analyze.appendChild(el('span', null, '分析这条'));
    analyze.addEventListener('click', function (event) {
      event.stopPropagation();
      analyzeMoment(item);
    });
    actions.appendChild(analyze);
    if (item.id === S.activeMomentId) {
      actions.appendChild(el('span', 'moment-badge', '已选中'));
    }
    body.appendChild(actions);

    card.appendChild(body);
    card.addEventListener('click', function () {
      selectMoment(item.id);
    });
    return card;
  }

  function buildMomentLink(link) {
    const url = safeHttpUrl(link.url);
    const node = el(url ? 'a' : 'div', 'moment-link');
    if (url) {
      node.href = url;
      node.target = '_blank';
      node.rel = 'noopener noreferrer';
      node.title = '在浏览器中打开分享链接';
    }
    const icon = el('span', 'moment-link-icon');
    icon.appendChild(iconEl('link', 17));
    node.appendChild(icon);
    const info = el('div', 'moment-link-info');
    info.appendChild(el('div', 'moment-link-title', link.title || '分享链接'));
    let host = '';
    if (url) {
      try {
        host = new URL(url).host;
      } catch (err) {
        host = '';
      }
    }
    info.appendChild(el('div', 'moment-link-url', host || (url ? truncate(url, 48) : '链接不可用')));
    node.appendChild(info);
    return node;
  }

  function buildMomentMedia(media, item) {
    const grid = el('div', 'media-grid');
    const count = media.length;
    const single = count === 1;
    grid.classList.add(
      count === 1 ? 'media-grid--1' : count === 2 ? 'media-grid--2' : count === 3 ? 'media-grid--3' : count === 4 ? 'media-grid--4' : 'media-grid--many'
    );
    if (count === 1 && media[0] && media[0].type === 'video') {
      // v9：单条朋友圈视频限宽与消息视频一致（420px）
      grid.style.maxWidth = 'min(420px,100%)';
    }

    media.forEach(function (entry, index) {
      const isVideo = entry.type === 'video';
      const path = String(entry.path || '');
      const thumb = String(entry.thumb || '');
      const thumbKey = String(entry.thumb_key || '');
      const cover = String(entry.cover || '');
      const coverKey = String(entry.cover_key || '');
      const cell = el('button', 'media-cell' + (isVideo ? ' media-cell--video' : ''));
      cell.type = 'button';

      if (isVideo) {
        const feedId = String((item && (item.feed_id || item.id)) || '');
        // v9：显式 video_url='' 表示 mock/旧数据没有服务端视频，保持占位；
        // 其余情况按 feed_id + index 走内存视频接口
        const hasServerVideo = feedId && entry.video_url !== '' && entry.video_url !== null;
        const videoUrl = hasServerVideo
          ? '/api/moments/video/' +
            encodeURIComponent(feedId) +
            '/' +
            index +
            '?token=' +
            encodeURIComponent(TOKEN)
          : '';
        const durationText = entry.duration ? formatVideoDuration(entry.duration) : '';
        const coverSource = coverKey || cover;
        const coverSrc = coverSource ? mediaUrl(coverSource) : '';
        const appendDuration = function (parent) {
          if (!durationText) return;
          const badge = el('span', 'video-duration', durationText);
          badge.style.top = '6px';
          badge.style.right = '6px';
          badge.style.bottom = 'auto';
          parent.appendChild(badge);
        };

        if (videoUrl) {
          // v9：内联播放器（内存下载/解密由 /api/moments/video 流式返回）
          const wrap = el('div', 'media-cell media-cell--video');
          wrap.style.aspectRatio = '16 / 9';
          wrap.style.cursor = 'default';
          wrap.style.background = '#000';
          wrap.style.borderRadius = '12px';
          wrap.style.maxWidth = 'min(420px,100%)';
          wrap.style.lineHeight = '0';
          wrap.addEventListener('click', function (event) {
            event.stopPropagation();
          });
          const video = el('video', 'msg-video');
          video.controls = true;
          video.preload = 'metadata';
          video.setAttribute('playsinline', '');
          video.setAttribute('webkit-playsinline', '');
          video.style.width = '100%';
          video.style.height = '100%';
          video.style.maxWidth = 'min(420px,100%)';
          video.style.maxHeight = '300px';
          video.style.minHeight = '0';
          video.style.borderRadius = '12px';
          video.style.background = '#000';
          if (coverSrc) video.poster = coverSrc;
          video.addEventListener('click', function (event) {
            event.stopPropagation();
          });
          video.addEventListener('error', function () {
            // 未缓存/解密失败：替换为现有视频占位，不 toast
            const fallback = el('div', 'video-placeholder');
            fallback.style.borderRadius = '12px';
            fallback.appendChild(iconEl('video', 18));
            fallback.appendChild(el('span', null, '视频未缓存'));
            const desc = el('span', null, '无法播放（微信只保留播放过的视频）');
            desc.style.fontSize = '9.5px';
            desc.style.lineHeight = '1.4';
            desc.style.padding = '0 6px';
            desc.style.textAlign = 'center';
            fallback.appendChild(desc);
            wrap.replaceChildren(fallback);
            appendDuration(wrap);
          });
          video.src = videoUrl;
          wrap.appendChild(video);
          appendDuration(wrap);
          grid.appendChild(wrap);
          return;
        }

        const placeholder = el('div', 'video-placeholder');
        placeholder.appendChild(iconEl('video', 18));
        placeholder.appendChild(el('span', null, '视频（未缓存）'));
        if (coverSrc) {
          const img = el('img', 'video-cover');
          img.loading = 'lazy';
          img.alt = '视频封面';
          img.addEventListener('error', function () {
            img.remove();
            if (!cell.querySelector('.video-placeholder')) {
              cell.insertBefore(placeholder, cell.firstChild);
            }
          });
          img.src = coverSrc;
          cell.appendChild(img);
        } else {
          cell.appendChild(placeholder);
        }
        const play = el('span', 'video-play');
        play.appendChild(iconEl('play', 15));
        cell.appendChild(play);
        if (durationText) cell.appendChild(el('span', 'video-duration', durationText));
        cell.addEventListener('click', function (event) {
          event.stopPropagation();
          if (!path) {
            toast('该视频没有缓存到本地，无法播放', 'info');
            return;
          }
          openVideoLightbox(mediaUrl(path), (item.author || '') + ' · ' + (momentTimeLabel(item) || ''));
        });
        grid.appendChild(cell);
        return;
      }

      // v2：优先内存 key（thumb_key），兼容旧 path/thumb；都没有才显示「图片未缓存」占位
      const preferred = thumbKey || path || thumb;
      if (!preferred) {
        const placeholder = el('div', 'video-placeholder');
        placeholder.title = '朋友圈图片只存在网页端内存里；请点「同步朋友圈」重新读取';
        placeholder.appendChild(iconEl('image', 18));
        placeholder.appendChild(el('span', null, '图片未缓存'));
        cell.appendChild(placeholder);
        cell.disabled = true;
        grid.appendChild(cell);
        return;
      }

      let usingThumb = !!thumbKey || (!path && !!thumb);
      let thumbTried = usingThumb || !thumb;
      // 旧字段 thumb（无原图 path）时展示角标；内存 key 属于 V2 正常图片，不额外加角标
      const badge = !thumbKey && thumb ? el('span', 'media-thumb-badge', '缩略图') : null;
      if (badge) {
        badge.hidden = !usingThumb;
        cell.appendChild(badge);
      }
      const img = el('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = (item.author || '') + ' 的图片';

      function captionText() {
        const base = (item.author || '') + ' · ' + (momentTimeLabel(item) || '');
        return usingThumb ? base + ' · 缩略图（原图未缓存）' : base;
      }

      function failImage() {
        img.remove();
        if (badge) badge.remove();
        if (single) {
          cell.replaceWith(brokenMediaCard('图片已失效', thumbKey ? '' : preferred));
        } else {
          cell.disabled = true;
          cell.classList.add('media-cell--broken');
          cell.replaceChildren(brokenCellContent());
        }
      }

      img.addEventListener('error', function () {
        // 旧字段兼容：原图失败时先回退到 thumb，仍失败才显示紧凑占位
        if (!thumbTried && thumb) {
          thumbTried = true;
          usingThumb = true;
          if (badge) badge.hidden = false;
          img.src = mediaUrl(thumb);
          return;
        }
        failImage();
      });
      img.src = mediaUrl(preferred);
      cell.appendChild(img);
      cell.addEventListener('click', function (event) {
        event.stopPropagation();
        if (cell.disabled || !img.src) return;
        openLightbox(img.src, captionText());
      });
      grid.appendChild(cell);
    });
    return grid;
  }

  function buildMomentSocial(likes, comments) {
    const box = el('div', 'moment-social');
    if (likes.length) {
      const row = el('div', 'likes');
      row.appendChild(iconEl('heart', 13));
      row.appendChild(el('span', null, likes.join('、')));
      box.appendChild(row);
    }
    if (comments.length) {
      const list = el('div', 'comment-list');
      comments.slice(0, 30).forEach(function (comment) {
        const line = el('div', 'comment');
        line.appendChild(el('span', 'comment-author', comment.author || '未知'));
        line.appendChild(el('span', 'comment-text', comment.content || ''));
        list.appendChild(line);
      });
      if (comments.length > 30) {
        list.appendChild(el('div', 'comment', '（还有更多评论）'));
      }
      box.appendChild(list);
    }
    return box;
  }

  function selectMoment(id) {
    const next = S.activeMomentId === id ? '' : id;
    S.activeMomentId = next;
    if (next) {
      S.scope = 'moment';
    } else if (S.scope === 'moment') {
      S.scope = 'moments';
    }
    renderScopeChips();
    renderMomentsMain();
  }

  function analyzeMoment(item) {
    S.activeMomentId = item.id;
    S.scope = 'moment';
    renderScopeChips();
    renderMomentsMain();
    const question = dom.aiQuestion.value.trim();
    if (!question) {
      const excerpt = truncate(String(item.content || '（无正文）').replace(/\s+/g, ' '), 60);
      dom.aiQuestion.value = '请分析这条朋友圈的内容、情绪和潜在话题：' + excerpt;
      updateQuestionCount();
    }
    if (!S.llm.configured) {
      toast('请先配置 DeepSeek API Key', 'warn');
      openSettings();
      return;
    }
    startAnalysis();
  }

  async function loadMoments(options) {
    const opts = options || {};
    if (MOCK || S.moments.loading) return;
    S.moments.loading = true;
    S.moments.error = '';
    if (!opts.silent) renderMomentsMain();
    try {
      const data = await api('/api/moments?limit=30');
      applyMomentsData(data);
    } catch (err) {
      S.moments.error = err.message;
      if (!opts.silent) toast('加载朋友圈失败：' + err.message, 'error', 4200);
    } finally {
      S.moments.loading = false;
      renderMomentsAll();
    }
  }

  function applyMomentsData(data) {
    const status = data.status || {};
    S.moments.status = status;
    S.moments.items = Array.isArray(data.items) ? data.items.slice(0, MAX_MOMENTS) : [];
    S.moments.count = Number(data.count !== undefined ? data.count : status.count) || 0;
    S.moments.syncedAt = Number(status.synced_at) || 0;
    S.moments.account = status.account || S.moments.account || {};
    S.moments.syncing = !!status.syncing;
    applyMomentsAutoStatus(status);
    S.moments.error = data.error || status.error || '';
    S.moments.loaded = true;
    // 新数据到达后重新计算新鲜度提示（用户此前关闭过也允许再次出现）
    S.moments.freshnessDismissed = false;
    if (S.activeMomentId && !S.moments.items.some(function (item) { return item.id === S.activeMomentId; })) {
      S.activeMomentId = '';
      if (S.scope === 'moment') S.scope = 'moments';
    }
    renderMomentsAll();
    renderScopeChips();
    renderTopbar();
  }

  function ensureMomentsLoaded() {
    if (MOCK || S.moments.loaded || S.moments.loading) return;
    loadMoments({ silent: false });
  }

  async function syncMoments() {
    if (MOCK) {
      mockSyncMoments();
      return;
    }
    if (momentsUnavailable()) {
      toast('朋友圈暂不可用：' + (S.moments.status.reason || '未知原因'), 'error', 4200);
      return;
    }
    let limit = Number(S.moments.syncLimit) || 10;
    if ([5, 10, 20].indexOf(limit) === -1) limit = 10;
    S.moments.syncing = true;
    S.moments.error = '';
    renderMomentsAll();
    try {
      await api('/api/moments/sync', { method: 'POST', body: { limit: limit } });
      toast('已开始同步最近 ' + limit + ' 条朋友圈，完成后会自动刷新…', 'ok', 2600);
    } catch (err) {
      S.moments.syncing = false;
      renderMomentsAll();
      toast(
        err.status === 503 ? '朋友圈暂不可用：' + err.message : '同步朋友圈失败：' + err.message,
        'error',
        4500
      );
    }
  }

  /** 清空朋友圈（仅网页端内存，不影响微信） */
  async function clearMoments() {
    if (syncingNow()) {
      toast('正在同步朋友圈，请稍后再清空', 'warn', 2600);
      return;
    }
    if (!S.moments.items.length) {
      toast('当前没有可清空的朋友圈数据', 'info', 2200);
      return;
    }
    if (!window.confirm('确定清空网页端的朋友圈数据吗？\n（只清空内存，不影响微信，可以重新同步）')) {
      return;
    }
    if (MOCK) {
      applyClearedMoments();
      toast('已清空朋友圈数据（演示，仅内存）', 'ok', 2600);
      return;
    }
    S.moments.clearing = true;
    try {
      await api('/api/moments/clear', { method: 'POST', body: {} });
      applyClearedMoments();
      toast('已清空朋友圈数据（仅内存）', 'ok', 2600);
    } catch (err) {
      S.moments.clearing = false;
      toast('清空朋友圈失败：' + err.message, 'error', 4200);
    } finally {
      setTimeout(function () {
        S.moments.clearing = false;
      }, 1500);
    }
  }

  function applyClearedMoments() {
    S.moments.items = [];
    S.moments.count = 0;
    S.moments.syncedAt = 0;
    S.moments.syncing = false;
    S.moments.error = '';
    S.moments.freshnessDismissed = false;
    S.moments.status = Object.assign({}, S.moments.status, {
      available: S.moments.status.available !== false,
      syncing: false,
      count: 0,
      synced_at: 0,
      error: null,
    });
    S.activeMomentId = '';
    if (S.scope === 'moment') S.scope = 'moments';
    renderMomentsAll();
    renderScopeChips();
    renderTopbar();
  }

  function syncingNow() {
    return !!(S.moments.syncing || (S.moments.status || {}).syncing);
  }

  function handleMomentsStatusEvent(data) {
    S.moments.syncing = !!data.syncing;
    // v8：状态事件里同步自动同步开关显示（其他页面/后端切换后保持一致）
    applyMomentsAutoStatus(data);
    if (data && data.status) applyMomentsAutoStatus(data.status);
    if (data && data.listening !== undefined) {
      S.moments.status = Object.assign({}, S.moments.status, { listening: !!data.listening });
    }
    renderMomentsAll();
  }

  function applyMomentsAutoStatus(status) {
    const st = status || {};
    if (st.auto !== undefined) S.moments.auto = !!st.auto;
    if (st.auto_interval !== undefined) {
      const interval = Number(st.auto_interval);
      if (isFinite(interval) && interval > 0) S.moments.autoInterval = interval;
    }
  }

  function handleMomentsEvent(data) {
    S.moments.syncing = false;
    // 清空事件（服务端在本页或其它页面清空后广播）
    if (data && data.cleared) {
      applyClearedMoments();
      if (S.moments.clearing) {
        S.moments.clearing = false;
        return;
      }
      toast('朋友圈数据已被清空（其他页面的操作）', 'info', 3200);
      return;
    }
    if (data.account) S.moments.account = data.account;
    if (data.count !== undefined) S.moments.count = Number(data.count) || 0;
    if (data.synced_at) S.moments.syncedAt = Number(data.synced_at) || 0;
    if (data.limit !== undefined) {
      S.moments.lastLimit = Number(data.limit) || S.moments.lastLimit;
      S.moments.status = Object.assign({}, S.moments.status, { limit: S.moments.lastLimit });
    }
    if (data.status) S.moments.status = data.status;
    if (data.listening !== undefined) {
      S.moments.status = Object.assign({}, S.moments.status, { listening: !!data.listening });
    }
    if (data.status) applyMomentsAutoStatus(data.status);
    if (data.auto !== undefined || data.auto_interval !== undefined || data.interval !== undefined) {
      applyMomentsAutoStatus({
        auto: data.auto,
        auto_interval: data.auto_interval !== undefined ? data.auto_interval : data.interval,
      });
    }
    if (data.ok === false || data.error) {
      S.moments.error = data.error || '同步失败';
      renderMomentsAll();
      toast('朋友圈同步失败：' + S.moments.error, 'error', 4500);
      return;
    }
    S.moments.error = '';
    toast(
      '朋友圈已同步：' + (data.count !== undefined ? data.count : S.moments.count) + ' 条' +
        (data.limit ? '（最近 ' + data.limit + ' 条）' : ''),
      'ok',
      2600
    );
    loadMoments({ silent: true });
  }

  function setTab(tab) {
    if (tab !== 'messages' && tab !== 'moments') return;
    S.tab = tab;
    document.querySelectorAll('.nav-tab').forEach(function (button) {
      button.classList.toggle('is-active', button.dataset.tab === tab);
    });
    dom.friendsPanel.hidden = tab !== 'messages';
    dom.momentsSide.hidden = tab !== 'moments';
    dom.chatMain.hidden = tab !== 'messages';
    dom.momentsMain.hidden = tab !== 'moments';

    if (tab === 'moments') {
      S.scope = S.activeMomentId ? 'moment' : 'moments';
    } else {
      S.scope = S.activeFriend ? 'friend' : 'all';
    }
    renderTopbar();
    renderScopeChips();
    renderQuickChips();
    renderMomentsAll();
    closeDrawers();
    if (tab === 'moments') ensureMomentsLoaded();
  }

  // ===========================================================================
  // 6. AI 分析
  // ===========================================================================
  function scopeOptions() {
    if (S.tab === 'moments') {
      return [
        { key: 'moments', label: '全部朋友圈' },
        {
          key: 'moment',
          label: '当前动态',
          disabled: !S.activeMomentId,
          title: S.activeMomentId ? '分析当前选中的这条朋友圈' : '先在时间线中点击一条动态',
        },
      ];
    }
    return [
      {
        key: 'friend',
        label: '当前会话',
        disabled: !S.activeFriend,
        title: S.activeFriend ? '分析「' + S.activeFriend + '」的消息' : '先在左侧选择一位好友',
      },
      { key: 'all', label: '全部消息' },
      {
        key: 'monitored',
        label: '监听好友',
        disabled: S.monitored.size === 0,
        title: S.monitored.size
          ? (isFollowMode() ? '分析本页筛选的 ' : '分析监听名单里 ') + S.monitored.size + ' 位好友的消息'
          : isFollowMode()
            ? '先勾选要筛选的好友'
            : '先勾选要监听的好友',
      },
    ];
  }

  function renderScopeChips() {
    const options = scopeOptions();
    const usable = options.filter(function (option) {
      return !option.disabled;
    });
    if (!usable.some(function (option) { return option.key === S.scope; })) {
      S.scope = usable.length ? usable[0].key : options[0].key;
    }
    dom.scopeChips.replaceChildren();
    options.forEach(function (option) {
      const button = el('button', 'is-active', option.label);
      button.type = 'button';
      button.disabled = !!option.disabled;
      button.title = option.title || '';
      if (option.key === S.scope && !option.disabled) button.classList.add('is-active');
      else button.classList.remove('is-active');
      button.addEventListener('click', function () {
        S.scope = option.key;
        renderScopeChips();
      });
      dom.scopeChips.appendChild(button);
    });
    dom.aiLimit.textContent = '最近 ' + ANALYSIS_LIMIT + ' 条';
  }

  function renderQuickChips() {
    const prompts = QUICK_PROMPTS[S.tab] || QUICK_PROMPTS.messages;
    dom.quickChips.replaceChildren();
    prompts.forEach(function (prompt) {
      const button = el('button', 'quick-chip', prompt.label);
      button.type = 'button';
      button.title = prompt.question;
      button.addEventListener('click', function () {
        dom.aiQuestion.value = prompt.question;
        updateQuestionCount();
        if (!S.llm.configured) {
          toast('请先配置 DeepSeek API Key', 'warn');
          openSettings();
          return;
        }
        startAnalysis();
      });
      dom.quickChips.appendChild(button);
    });
  }

  function buildAnalyzeScope() {
    if (S.tab === 'moments') {
      if (S.scope === 'moment' && S.activeMomentId) {
        const item = S.moments.items.filter(function (entry) {
          return entry.id === S.activeMomentId;
        })[0];
        if (!item) return { scope: null, label: '' };
        const when = momentTimeLabel(item);
        return {
          scope: { type: 'moment', id: S.activeMomentId },
          label: '单条朋友圈 · ' + (item.author || '未知') + (when ? ' · ' + when : ''),
        };
      }
      return { scope: { type: 'moments' }, label: '全部朋友圈' };
    }
    if (S.scope === 'friend' && S.activeFriend) {
      return { scope: { type: 'friend', friend: S.activeFriend }, label: S.activeFriend };
    }
    if (S.scope === 'monitored' && S.monitored.size) {
      return {
        scope: { type: 'monitored', friend_names: Array.from(S.monitored) },
        label: '监听好友（' + S.monitored.size + ' 位）',
      };
    }
    return { scope: { type: 'all' }, label: '全部消息' };
  }

  function updateQuestionCount() {
    dom.aiQuestionCount.textContent = dom.aiQuestion.value.length + ' / 4000';
  }

  async function startAnalysis() {
    if (S.analysis.busy) return;
    if (MOCK) {
      mockRunAnalysis();
      return;
    }
    if (!S.llm.configured) {
      toast('请先配置 DeepSeek API Key', 'warn');
      openSettings();
      return;
    }
    const question = dom.aiQuestion.value.trim();
    if (!question) {
      toast('先输入问题，或点一个快捷指令', 'warn');
      dom.aiQuestion.focus();
      return;
    }
    if (question.length > 4000) {
      toast('问题不能超过 4000 字', 'warn');
      return;
    }
    const target = buildAnalyzeScope();
    if (!target.scope) {
      toast('当前分析范围不可用，请先选择好友或动态', 'warn');
      return;
    }

    S.analysis = newAnalysis();
    S.analysis.status = 'running';
    S.analysis.busy = true;
    S.analysis.question = question;
    S.analysis.scopeLabel = target.label;
    S.analysis.modelLabel = S.llm.model_label;
    S.analysis.startedAt = nowSec();
    dom.aiError.hidden = true;
    S.analysisStick = true;
    renderAnalysis();
    startElapsedTimer();

    try {
      const body = {
        scope: target.scope,
        scope_label: target.label,
        question: question,
        history: S.history.slice(-MAX_HISTORY),
        thinking: dom.optThinking.checked,
        attach_images: dom.optImages.checked,
        limit: ANALYSIS_LIMIT,
      };
      const result = await api('/api/llm/analyze', { method: 'POST', body: body });
      if (result && result.job_id) {
        S.analysis.jobId = String(result.job_id);
      }
      renderAnalysisMeta();
    } catch (err) {
      S.analysis.busy = false;
      S.analysis.status = 'error';
      stopElapsedTimer();
      renderAnalysis();
      const friendly =
        err.code === 'busy'
          ? '已有分析任务正在进行，请稍候或先点击「停止」。'
          : err.code === 'not_configured'
            ? '尚未配置 DeepSeek API Key，请先在「模型设置」中填写。'
            : err.message;
      showAiError(friendly, err.code);
      renderAnalysis();
    }
  }

  async function cancelAnalysis() {
    if (!S.analysis.busy) return;
    const jobId = S.analysis.jobId;
    if (!jobId) {
      toast('任务尚未开始，请稍候…', 'info');
      return;
    }
    dom.stopBtn.disabled = true;
    try {
      await api('/api/llm/cancel', { method: 'POST', body: { job_id: jobId } });
      toast('已请求停止分析', 'info', 2000);
    } catch (err) {
      toast('停止失败：' + err.message, 'error');
    } finally {
      setTimeout(function () {
        dom.stopBtn.disabled = false;
      }, 900);
    }
  }

  function handleLlmEvent(type, data) {
    if (!data) return;
    const analysis = S.analysis;
    const jobId = data.job_id ? String(data.job_id) : '';
    if (jobId && analysis.jobId && jobId !== analysis.jobId) return;
    if (jobId && !analysis.jobId) analysis.jobId = jobId;

    if (type === 'llm_start') {
      analysis.jobId = jobId || analysis.jobId;
      analysis.busy = true;
      analysis.status = 'running';
      analysis.cancelled = false;
      analysis.content = '';
      analysis.reasoning = '';
      analysis.usage = null;
      analysis.elapsedMs = 0;
      analysis.modelLabel = data.model_label || S.llm.model_label;
      analysis.scopeLabel = data.scope_label || analysis.scopeLabel;
      analysis.messageCount = Number(data.message_count) || 0;
      analysis.imageCount = Number(data.image_count) || 0;
      analysis.startedAt = Number(data.started_at) || nowSec();
      dom.aiError.hidden = true;
      S.analysisStick = true;
      renderAnalysis();
      startElapsedTimer();
      return;
    }

    if (type === 'llm_reasoning') {
      if (!analysis.busy) {
        analysis.busy = true;
        analysis.status = 'running';
        renderAnalysisMeta();
      }
      analysis.reasoning += data.delta || '';
      scheduleReasoningRender();
      return;
    }

    if (type === 'llm_delta') {
      analysis.content += data.delta || '';
      scheduleContentRender();
      updateAnalysisStatusText();
      return;
    }

    if (type === 'llm_usage') {
      analysis.usage = data.usage || analysis.usage;
      renderAnalysisFooter();
      return;
    }

    if (type === 'llm_done') {
      analysis.busy = false;
      analysis.cancelled = !!data.cancelled;
      analysis.status = analysis.cancelled ? 'cancelled' : 'done';
      if (data.content !== undefined && data.content !== null && data.content !== '') {
        analysis.content = String(data.content);
      }
      if (data.reasoning !== undefined && data.reasoning !== null && data.reasoning !== '') {
        analysis.reasoning = String(data.reasoning);
      }
      if (data.usage) analysis.usage = data.usage;
      analysis.elapsedMs = Number(data.elapsed_ms) || (analysis.startedAt ? (nowSec() - analysis.startedAt) * 1000 : 0);
      stopElapsedTimer();
      if (analysis.content) pushHistory(analysis.question, analysis.content);
      renderAnalysis();
      if (analysis.cancelled) toast('分析已停止', 'info', 2200);
      return;
    }

    if (type === 'llm_error') {
      analysis.busy = false;
      analysis.status = 'error';
      stopElapsedTimer();
      renderAnalysis();
      showAiError(data.error || '模型调用失败，请稍后重试。', data.code || 'llm_error');
    }
  }

  function pushHistory(question, content) {
    if (!question || !content) return;
    S.history.push({ role: 'user', content: String(question).slice(0, 8000) });
    S.history.push({ role: 'assistant', content: String(content).slice(0, 8000) });
    if (S.history.length > MAX_HISTORY) S.history = S.history.slice(-MAX_HISTORY);
  }

  let contentRenderTimer = null;
  function scheduleContentRender() {
    if (contentRenderTimer) return;
    contentRenderTimer = setTimeout(function () {
      contentRenderTimer = null;
      renderAnalysisContent();
      renderAnalysisFooter();
      if (S.analysis.busy) renderAnalysisMeta();
    }, 80);
  }

  let reasoningRenderTimer = null;
  function scheduleReasoningRender() {
    if (reasoningRenderTimer) return;
    reasoningRenderTimer = setTimeout(function () {
      reasoningRenderTimer = null;
      renderReasoning();
    }, 120);
  }

  let elapsedTimer = null;
  function startElapsedTimer() {
    stopElapsedTimer();
    elapsedTimer = setInterval(function () {
      if (!S.analysis.busy) {
        stopElapsedTimer();
        return;
      }
      renderAnalysisMeta();
    }, 500);
  }

  function stopElapsedTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  function usageText(usage) {
    if (!usage) return '';
    const total = Number(usage.total_tokens) || 0;
    const prompt = Number(usage.prompt_tokens) || 0;
    const completion = Number(usage.completion_tokens) || 0;
    const reasoning = Number(usage.reasoning_tokens) || 0;
    if (!total && !prompt && !completion) return '';
    let text = 'tokens ' + formatInt(total);
    const detail = [];
    if (prompt) detail.push('提示 ' + formatInt(prompt));
    if (completion) detail.push('生成 ' + formatInt(completion));
    if (reasoning) detail.push('思考 ' + formatInt(reasoning));
    if (detail.length) text += '（' + detail.join(' · ') + '）';
    return text;
  }

  function updateAnalysisStatusText() {
    const analysis = S.analysis;
    if (!analysis.busy) return;
    dom.analysisStatus.textContent = !analysis.content && analysis.reasoning
      ? '思考中…'
      : analysis.content
        ? '正在输出…'
        : '分析中…';
  }

  function renderAnalysis() {
    renderAnalysisMeta();
    renderReasoning();
    renderAnalysisContent();
    renderAnalysisFooter();
  }

  function renderAnalysisMeta() {
    const analysis = S.analysis;
    const configured = !!S.llm.configured;
    const busy = !!analysis.busy;

    dom.analyzeBtn.hidden = busy;
    dom.stopBtn.hidden = !busy;
    dom.stopBtn.disabled = false;
    dom.analyzeBtn.disabled = !configured || busy;
    dom.aiNotConfigured.hidden = configured;

    const hasContent = !!(analysis.busy || analysis.content || analysis.reasoning || analysis.status === 'done' || analysis.status === 'cancelled');
    dom.analysis.hidden = !hasContent;
    dom.analysis.classList.toggle('is-streaming', busy);

    if (busy) {
      dom.analysisStatus.textContent = !analysis.content && analysis.reasoning
        ? '思考中…'
        : analysis.content
          ? '正在输出…'
          : '分析中…';
    } else if (analysis.status === 'done') {
      dom.analysisStatus.textContent = '分析完成';
    } else if (analysis.status === 'cancelled') {
      dom.analysisStatus.textContent = '已停止（保留已生成内容）';
    } else if (analysis.status === 'error') {
      dom.analysisStatus.textContent = '分析失败';
    } else {
      dom.analysisStatus.textContent = '';
    }

    dom.analysisScope.textContent = analysis.scopeLabel || '';
    dom.analysisScope.hidden = !analysis.scopeLabel;

    const metaParts = [];
    if (analysis.modelLabel) metaParts.push(analysis.modelLabel);
    if (analysis.messageCount) {
      metaParts.push(formatCount(analysis.messageCount) + (S.tab === 'moments' ? ' 条动态' : ' 条消息'));
    }
    if (analysis.imageCount) metaParts.push(analysis.imageCount + ' 张图片');
    if (busy && analysis.startedAt) {
      metaParts.push('已用时 ' + formatElapsed(nowSec() - analysis.startedAt));
    }
    dom.analysisMeta.textContent = metaParts.join(' · ');
  }

  function renderReasoning() {
    const analysis = S.analysis;
    if (!analysis.reasoning) {
      dom.reasoningBox.hidden = true;
      dom.reasoningLen.textContent = '';
      return;
    }
    dom.reasoningBox.hidden = false;
    dom.reasoningLen.textContent = analysis.busy
      ? '生成中…'
      : formatCount(analysis.reasoning.length) + ' 字';
    if (dom.reasoningBody.textContent !== analysis.reasoning) {
      dom.reasoningBody.textContent = analysis.reasoning;
    }
    if (analysis.busy) dom.reasoningBody.scrollTop = dom.reasoningBody.scrollHeight;
  }

  function renderAnalysisContent() {
    const analysis = S.analysis;
    if (!analysis.content) {
      if (analysis.busy) {
        dom.analysisContent.innerHTML = '<p class="md-pending">正在生成…</p>';
      } else if (analysis.status === 'done' || analysis.status === 'cancelled') {
        dom.analysisContent.innerHTML = '<p class="md-pending">模型没有返回内容。</p>';
      } else {
        dom.analysisContent.replaceChildren();
      }
      return;
    }
    // 安全：safeMarkdown 先整体 HTML 转义，再只生成受控标签与 http/https 链接
    dom.analysisContent.innerHTML = safeMarkdown(analysis.content);
    if (S.analysisStick && analysis.busy) {
      dom.aiScroll.scrollTop = dom.aiScroll.scrollHeight;
    }
  }

  function renderAnalysisFooter() {
    const analysis = S.analysis;
    dom.analysisUsage.textContent = usageText(analysis.usage);
    dom.analysisElapsed.textContent = analysis.elapsedMs
      ? '耗时 ' + formatElapsed(analysis.elapsedMs / 1000)
      : '';
    dom.copyAnalysis.disabled = !analysis.content;
    dom.clearAnalysis.disabled = analysis.busy;
  }

  function showAiError(message, code) {
    dom.aiError.hidden = false;
    dom.aiErrorTitle.textContent =
      code === 'busy' ? '分析任务繁忙' : code === 'not_configured' ? '尚未配置模型' : '分析失败';
    dom.aiErrorText.textContent = message;
  }

  function copyAnalysis() {
    const text = S.analysis.content;
    if (!text) return;
    function fallbackCopy() {
      const area = el('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (err) {
        ok = false;
      }
      area.remove();
      toast(ok ? '已复制分析结果' : '复制失败，请手动选择文本复制', ok ? 'ok' : 'error');
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () {
          toast('已复制分析结果', 'ok', 2000);
        },
        fallbackCopy
      );
    } else {
      fallbackCopy();
    }
  }

  function clearAnalysis() {
    if (S.analysis.busy) {
      toast('分析进行中，请先停止再清空', 'warn');
      return;
    }
    S.analysis = newAnalysis();
    S.history = [];
    dom.aiError.hidden = true;
    stopElapsedTimer();
    renderAnalysis();
    toast('已清空分析结果', 'info', 1800);
  }

  function renderModelRow() {
    const llm = S.llm;
    const configured = !!llm.configured;
    dom.modelRow.classList.toggle('is-empty', !configured);
    const hit = (llm.models || []).filter(function (model) {
      return model.id === llm.model;
    })[0];
    const description = (hit && hit.description) || '1M 上下文 · 支持深度思考与图像理解';
    if (configured) {
      dom.modelName.textContent = llm.model_label || (hit && hit.label) || 'DeepSeek V4.1 Flash 1017';
      dom.modelSub.textContent = llm.api_key_masked
        ? llm.api_key_masked + ' · ' + description
        : description;
    } else {
      dom.modelName.textContent = '尚未配置模型';
      dom.modelSub.textContent = '填入 API Key 后即可分析消息与朋友圈';
    }
    dom.configureModelBtn.textContent = configured ? '配置' : '配置模型';
    renderAnalysisMeta();
  }

  // ===========================================================================
  // 7. 模型设置
  // ===========================================================================
  function openSettings() {
    if (S.settings.open) return;
    S.settings.open = true;
    S.settings.clearKey = false;
    const llm = S.llm;

    dom.modelSelect.replaceChildren();
    (llm.models && llm.models.length ? llm.models : DEFAULT_MODELS).forEach(function (model) {
      const option = el('option', null, model.label || model.id);
      option.value = model.id;
      dom.modelSelect.appendChild(option);
    });
    dom.modelSelect.value = llm.model || DEFAULT_MODELS[0].id;
    updateModelHint();

    dom.apiKeyInput.value = '';
    dom.apiKeyInput.type = 'password';
    dom.apiKeyInput.placeholder = llm.configured
      ? '已保存 ' + (llm.api_key_masked || 'sk-****') + '，留空保持不变'
      : 'sk-...';
    dom.clearKeyBtn.hidden = !llm.configured;
    dom.cfgThinking.checked = llm.thinking !== false;
    dom.cfgImages.checked = llm.attach_images !== false;
    dom.baseUrlInput.value = llm.base_url || 'https://api.deepseek.com';
    dom.advancedBox.open = false;
    dom.testResult.hidden = true;
    dom.testResult.className = 'test-result';
    dom.testResult.textContent = '';
    dom.settingsFormError.hidden = true;
    dom.settingsFormError.textContent = '';
    dom.settingsModal.hidden = false;
    requestAnimationFrame(function () {
      dom.apiKeyInput.focus();
    });
  }

  function closeSettings() {
    S.settings.open = false;
    dom.settingsModal.hidden = true;
  }

  function updateModelHint() {
    const id = dom.modelSelect.value;
    const hit = (S.llm.models || DEFAULT_MODELS).filter(function (model) {
      return model.id === id;
    })[0];
    dom.modelHint.textContent = (hit && hit.description) || '1M 上下文 · 支持深度思考与图像理解';
  }

  function showSettingsError(message) {
    dom.settingsFormError.textContent = message;
    dom.settingsFormError.hidden = false;
  }

  function setSettingsBusy(busy) {
    S.settings.saving = busy;
    dom.settingsSave.disabled = busy;
    dom.testConnBtn.disabled = busy;
    dom.settingsSave.textContent = busy ? '保存中…' : '保存';
  }

  async function saveSettings(options) {
    const opts = options || {};
    if (S.settings.saving) return false;
    const keyRaw = dom.apiKeyInput.value.trim();
    if (keyRaw && (keyRaw.length < 8 || keyRaw.length > 512)) {
      showSettingsError('API Key 长度应在 8 ~ 512 个字符之间。');
      return false;
    }
    const baseUrl = (dom.baseUrlInput.value || '').trim() || 'https://api.deepseek.com';
    if (!/^https?:\/\//i.test(baseUrl)) {
      showSettingsError('Base URL 必须以 http:// 或 https:// 开头。');
      return false;
    }
    const patch = {
      model: dom.modelSelect.value,
      base_url: baseUrl,
      thinking: dom.cfgThinking.checked,
      attach_images: dom.cfgImages.checked,
    };
    if (keyRaw) patch.api_key = keyRaw;
    else if (S.settings.clearKey) patch.api_key = 'clear';

    if (MOCK) {
      applyMockSettings(patch);
      return true;
    }

    setSettingsBusy(true);
    try {
      const result = await api('/api/llm/config', { method: 'POST', body: patch });
      S.llm = normalizeLlm((result && result.config) || Object.assign({}, S.llm, patch));
      dom.settingsFormError.hidden = true;
      dom.optThinking.checked = S.llm.thinking !== false;
      dom.optImages.checked = S.llm.attach_images !== false;
      renderModelRow();
      renderAnalysisMeta();
      if (!opts.silent) {
        closeSettings();
        toast('模型配置已保存', 'ok', 2400);
      }
      return true;
    } catch (err) {
      showSettingsError('保存失败：' + err.message);
      return false;
    } finally {
      setSettingsBusy(false);
    }
  }

  async function testConnection() {
    if (S.settings.testing) return;
    S.settings.testing = true;
    dom.testConnBtn.disabled = true;
    const label = dom.testConnBtn.querySelector('.btn-text');
    if (label) label.textContent = '测试中…';
    dom.testResult.hidden = false;
    dom.testResult.className = 'test-result is-busy';
    dom.testResult.textContent = '正在测试连接（会先保存当前配置）…';

    try {
      const saved = await saveSettings({ silent: true });
      if (!saved) {
        dom.testResult.className = 'test-result is-err';
        dom.testResult.textContent = '请先修正表单中的问题再测试。';
        return;
      }
      if (MOCK) {
        const ok = !!S.llm.configured;
        dom.testResult.className = 'test-result ' + (ok ? 'is-ok' : 'is-err');
        dom.testResult.textContent = ok
          ? '连接成功 · 延迟 128 ms · 可用模型 1 个（演示数据）'
          : '连接失败：尚未配置 DeepSeek API Key，请填写后保存。';
        return;
      }
      const result = await api('/api/llm/test', { method: 'POST', body: {} });
      if (result && !result.error) {
        const latency = result.latency_ms !== null && result.latency_ms !== undefined ? result.latency_ms + ' ms' : '—';
        const modelCount = Array.isArray(result.models) ? result.models.length : '—';
        dom.testResult.className = 'test-result is-ok';
        dom.testResult.textContent = '连接成功 · 延迟 ' + latency + ' · 可用模型 ' + modelCount + ' 个';
        toast('连接成功', 'ok', 2400);
      } else {
        dom.testResult.className = 'test-result is-err';
        dom.testResult.textContent = '连接失败：' + ((result && result.error) || '未知错误');
      }
    } catch (err) {
      dom.testResult.className = 'test-result is-err';
      dom.testResult.textContent = '连接失败：' + err.message;
    } finally {
      S.settings.testing = false;
      dom.testConnBtn.disabled = false;
      if (label) label.textContent = '测试连接';
    }
  }

  async function clearSavedKey() {
    if (MOCK) {
      S.llm.configured = false;
      S.llm.api_key_masked = '';
      dom.clearKeyBtn.hidden = true;
      dom.apiKeyInput.placeholder = 'sk-...';
      renderModelRow();
      toast('已清除保存的 API Key（演示）', 'ok');
      return;
    }
    try {
      const result = await api('/api/llm/config', { method: 'POST', body: { api_key: 'clear' } });
      S.llm = normalizeLlm((result && result.config) || Object.assign({}, S.llm, { configured: false }));
      dom.clearKeyBtn.hidden = true;
      dom.apiKeyInput.placeholder = 'sk-...';
      renderModelRow();
      renderAnalysisMeta();
      toast('已清除保存的 API Key', 'ok', 2400);
    } catch (err) {
      showSettingsError('清除失败：' + err.message);
    }
  }

  // ===========================================================================
  // 8. 日志 / 灯箱 / 提示 / 抽屉
  // ===========================================================================
  function appendLog(entry) {
    if (!entry) return;
    const ts = Number(entry.ts) || nowSec();
    const level = ['info', 'warn', 'error', 'debug'].indexOf(entry.level) !== -1 ? entry.level : 'info';
    const text = String(entry.text || '');
    const key = ts.toFixed(3) + '|' + level + '|' + text;
    if (S.logSeen.has(key)) return;
    S.logSeen.add(key);
    if (S.logSeen.size > 2000) {
      const keys = Array.from(S.logSeen);
      keys.slice(0, keys.length - 1500).forEach(function (item) {
        S.logSeen.delete(item);
      });
    }
    S.logs.push({ ts: ts, level: level, text: text });
    if (S.logs.length > MAX_LOGS) S.logs.splice(0, S.logs.length - MAX_LOGS);

    const line = el('div', 'log-line log-line--' + level);
    line.appendChild(el('span', 'log-time', formatTime(ts)));
    line.appendChild(el('span', 'log-text', text));
    dom.logBody.appendChild(line);
    while (dom.logBody.childElementCount > MAX_LOGS) dom.logBody.firstElementChild.remove();
    dom.logCount.textContent = String(dom.logBody.childElementCount);
    if (S.logOpen) {
      const gap = dom.logBody.scrollHeight - dom.logBody.scrollTop - dom.logBody.clientHeight;
      if (gap < 60) dom.logBody.scrollTop = dom.logBody.scrollHeight;
    }
  }

  function clearLogs() {
    S.logs = [];
    S.logSeen = new Set();
    dom.logBody.replaceChildren();
    dom.logCount.textContent = '0';
  }

  function setLogOpen(open) {
    S.logOpen = !!open;
    dom.app.classList.toggle('log-open', S.logOpen);
    if (S.logOpen) {
      requestAnimationFrame(function () {
        dom.logBody.scrollTop = dom.logBody.scrollHeight;
      });
    }
  }

  function openDrawer(kind) {
    if (kind === 'ai') dom.app.classList.add('ai-open');
    if (kind === 'friends') dom.app.classList.add('friends-open');
    updateScrim();
  }

  function closeDrawers() {
    dom.app.classList.remove('ai-open');
    dom.app.classList.remove('friends-open');
    updateScrim();
  }

  function updateScrim() {
    const open = dom.app.classList.contains('ai-open') || dom.app.classList.contains('friends-open');
    dom.scrim.hidden = !open;
  }

  let lightboxVideo = null;
  function clearLightboxMedia() {
    if (lightboxVideo) {
      lightboxVideo.pause();
      lightboxVideo.remove();
      lightboxVideo = null;
    }
    dom.lightboxImg.hidden = true;
    dom.lightboxImg.removeAttribute('src');
  }

  function openLightbox(url, caption) {
    if (!url) return;
    clearLightboxMedia();
    dom.lightboxImg.hidden = false;
    dom.lightboxCaption.classList.remove('is-error');
    dom.lightboxImg.src = url;
    dom.lightboxCaption.textContent = caption || '';
    dom.lightbox.hidden = false;
  }

  function openVideoLightbox(url, caption) {
    if (!url) return;
    clearLightboxMedia();
    const figure = dom.lightbox.querySelector('.lightbox-figure');
    lightboxVideo = el('video', 'lightbox-video');
    lightboxVideo.controls = true;
    lightboxVideo.autoplay = true;
    lightboxVideo.src = url;
    figure.insertBefore(lightboxVideo, dom.lightboxCaption);
    dom.lightboxCaption.textContent = caption || '';
    dom.lightbox.hidden = false;
  }

  function closeLightbox() {
    dom.lightbox.hidden = true;
    clearLightboxMedia();
    dom.lightboxCaption.textContent = '';
  }

  function toast(message, type, duration) {
    const node = el('div', 'toast toast--' + (type || 'info'));
    node.textContent = String(message || '');
    dom.toastStack.appendChild(node);
    const timeout = duration || 3200;
    setTimeout(function () {
      node.classList.add('is-leaving');
      setTimeout(function () {
        node.remove();
      }, 200);
    }, timeout);
  }

  // ===========================================================================
  // 9. 事件绑定
  // ===========================================================================
  function bindEvents() {
    document.querySelectorAll('.nav-tab').forEach(function (button) {
      button.addEventListener('click', function () {
        setTab(button.dataset.tab);
      });
    });

    dom.settingsBtn.addEventListener('click', openSettings);
    dom.configureModelBtn.addEventListener('click', openSettings);
    dom.aiConfigureBtn.addEventListener('click', openSettings);

    dom.backendToggle.addEventListener('click', function () {
      toggleBackend();
    });
    dom.startListenBtn.addEventListener('click', function () {
      toggleBackend(true);
    });
    dom.stopListenBtn.addEventListener('click', function () {
      toggleBackend(false);
    });
    dom.logToggle.addEventListener('click', function () {
      setLogOpen(!S.logOpen);
    });
    dom.logHead.addEventListener('click', function (event) {
      if (event.target.closest('button')) return;
      setLogOpen(!S.logOpen);
    });
    dom.logHead.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        setLogOpen(!S.logOpen);
      }
    });
    dom.logClear.addEventListener('click', function (event) {
      event.stopPropagation();
      clearLogs();
      toast('已清空日志', 'info', 1600);
    });

    dom.friendsToggle.addEventListener('click', function () {
      openDrawer('friends');
    });
    dom.aiToggle.addEventListener('click', function () {
      openDrawer('ai');
    });
    dom.closeAiBtn.addEventListener('click', function () {
      dom.app.classList.remove('ai-open');
      updateScrim();
    });
    dom.scrim.addEventListener('click', closeDrawers);

    dom.contactTabs.addEventListener('click', function (event) {
      const tab = event.target.closest('.contact-tab');
      if (!tab || !tab.dataset.list) return;
      setContactList(tab.dataset.list);
    });

    dom.selectAllBtn.addEventListener('click', selectAllFriends);
    dom.clearAllBtn.addEventListener('click', clearAllFriends);
    dom.syncFriendsBtn.addEventListener('click', refreshContactList);
    if (dom.contactsRefreshBtn) dom.contactsRefreshBtn.addEventListener('click', refreshContactList);
    dom.refreshContactsBtn.addEventListener('click', refreshContacts);
    dom.clearContactsBtn.addEventListener('click', clearContacts);

    dom.friendSearch.addEventListener(
      'input',
      debounce(function () {
        if (S.contactList === 'groups') S.groupQuery = dom.friendSearch.value;
        else S.friendQuery = dom.friendSearch.value;
        dom.friendSearchClear.hidden = !dom.friendSearch.value;
        renderFriends();
      }, 120)
    );
    dom.friendSearchClear.addEventListener('click', function () {
      dom.friendSearch.value = '';
      if (S.contactList === 'groups') S.groupQuery = '';
      else S.friendQuery = '';
      dom.friendSearchClear.hidden = true;
      renderFriends();
      dom.friendSearch.focus();
    });

    dom.kindFilters.addEventListener('click', function (event) {
      const chip = event.target.closest('.chip');
      if (!chip || !chip.dataset.kind) return;
      setKind(chip.dataset.kind);
    });

    dom.messageSearch.addEventListener(
      'input',
      debounce(function () {
        S.msgQuery = dom.messageSearch.value;
        dom.messageSearchClear.hidden = !S.msgQuery;
        S.paused = false;
        S.unseen = 0;
        renderMessages({ scrollBottom: true });
      }, 160)
    );
    dom.messageSearchClear.addEventListener('click', function () {
      dom.messageSearch.value = '';
      S.msgQuery = '';
      dom.messageSearchClear.hidden = true;
      renderMessages({ scrollBottom: true });
      dom.messageSearch.focus();
    });

    dom.autoScrollToggle.addEventListener('change', function () {
      S.autoScroll = dom.autoScrollToggle.checked;
      if (S.autoScroll) {
        S.paused = false;
        S.unseen = 0;
        scrollMessagesToBottom(true);
        updateJumpLatest();
      }
    });

    dom.jumpLatest.addEventListener('click', function () {
      S.paused = false;
      S.unseen = 0;
      scrollMessagesToBottom(true);
      updateJumpLatest();
    });

    dom.messageScroll.addEventListener(
      'scroll',
      function () {
        const gap = dom.messageScroll.scrollHeight - dom.messageScroll.scrollTop - dom.messageScroll.clientHeight;
        const atBottom = gap < 80;
        if (atBottom) {
          S.paused = false;
          S.unseen = 0;
        } else if (Date.now() > scrollLockUntil) {
          S.paused = true;
        }
        updateJumpLatest();
      },
      { passive: true }
    );

    dom.aiScroll.addEventListener(
      'scroll',
      function () {
        const gap = dom.aiScroll.scrollHeight - dom.aiScroll.scrollTop - dom.aiScroll.clientHeight;
        S.analysisStick = gap < 70;
      },
      { passive: true }
    );

    // 朋友圈
    dom.momentsSideSyncBtn.addEventListener('click', syncMoments);
    dom.momentsSyncBtn.addEventListener('click', syncMoments);
    dom.momentsClearBtn.addEventListener('click', clearMoments);
    if (dom.momentsAutoToggle) {
      dom.momentsAutoToggle.addEventListener('change', function () {
        setMomentsAuto(dom.momentsAutoToggle.checked);
      });
    }
    dom.momentsLimitSelect.addEventListener('change', function () {
      let limit = Number(dom.momentsLimitSelect.value) || 10;
      if ([5, 10, 20].indexOf(limit) === -1) limit = 10;
      S.moments.syncLimit = limit;
      S.moments.syncLimitTouched = true;
    });
    dom.momentsOnlyImages.addEventListener('change', function () {
      S.moments.onlyImages = dom.momentsOnlyImages.checked;
      renderMomentsAll();
    });
    dom.momentsOnlyComments.addEventListener('change', function () {
      S.moments.onlyComments = dom.momentsOnlyComments.checked;
      renderMomentsAll();
    });
    dom.momentAuthorSearch.addEventListener(
      'input',
      debounce(function () {
        S.moments.authorQuery = dom.momentAuthorSearch.value;
        renderMomentAuthors();
      }, 120)
    );
    dom.momentSearch.addEventListener(
      'input',
      debounce(function () {
        S.moments.query = dom.momentSearch.value;
        dom.momentSearchClear.hidden = !S.moments.query;
        renderMomentsMain();
      }, 160)
    );
    dom.momentSearchClear.addEventListener('click', function () {
      dom.momentSearch.value = '';
      S.moments.query = '';
      dom.momentSearchClear.hidden = true;
      renderMomentsMain();
      dom.momentSearch.focus();
    });
    dom.momentAuthorChip.addEventListener('click', function () {
      S.moments.author = '';
      renderMomentsAll();
    });

    // 同步历史 / 清空聊天（v2）
    dom.syncHistoryBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      toggleHistoryPop();
    });
    dom.historyPopOptions.addEventListener('click', function (event) {
      const button = event.target.closest('button[data-limit]');
      if (!button || button.disabled) return;
      loadHistory(Number(button.dataset.limit) || 0);
    });
    dom.clearChatBtn.addEventListener('click', clearChat);
    if (dom.videoAutoToggle) dom.videoAutoToggle.addEventListener('click', toggleVideoAuto);
    document.addEventListener('click', function (event) {
      if (dom.historyPop.hidden) return;
      if (dom.historyPop.contains(event.target) || dom.syncHistoryBtn.contains(event.target)) return;
      closeHistoryPop();
    });

    // AI 分析
    dom.aiQuestion.addEventListener('input', updateQuestionCount);
    dom.aiQuestion.addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        startAnalysis();
      }
    });
    dom.analyzeBtn.addEventListener('click', startAnalysis);
    dom.stopBtn.addEventListener('click', cancelAnalysis);
    dom.copyAnalysis.addEventListener('click', copyAnalysis);
    dom.clearAnalysis.addEventListener('click', clearAnalysis);

    // 设置弹窗
    dom.settingsClose.addEventListener('click', closeSettings);
    dom.settingsCancel.addEventListener('click', closeSettings);
    dom.settingsSave.addEventListener('click', function () {
      saveSettings();
    });
    dom.testConnBtn.addEventListener('click', testConnection);
    dom.toggleKeyVisible.addEventListener('click', function () {
      const show = dom.apiKeyInput.type === 'password';
      dom.apiKeyInput.type = show ? 'text' : 'password';
      dom.toggleKeyVisible.title = show ? '隐藏 Key' : '显示 Key';
      dom.toggleKeyVisible.classList.toggle('is-active', show);
    });
    dom.modelSelect.addEventListener('change', updateModelHint);
    dom.clearKeyBtn.addEventListener('click', clearSavedKey);
    dom.settingsModal.addEventListener('click', function (event) {
      if (event.target === dom.settingsModal) closeSettings();
    });
    dom.apiKeyInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        saveSettings();
      }
    });

    // 灯箱
    dom.lightboxClose.addEventListener('click', closeLightbox);
    dom.lightbox.addEventListener('click', function (event) {
      if (event.target === dom.lightbox) closeLightbox();
    });
    dom.lightboxImg.addEventListener('error', function () {
      if (dom.lightbox.hidden) return;
      dom.lightboxImg.hidden = true;
      dom.lightboxCaption.classList.add('is-error');
      dom.lightboxCaption.textContent = '图片加载失败：文件可能已被清理或未缓存';
    });

    // 全局键盘
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!dom.historyPop.hidden) {
        closeHistoryPop();
        return;
      }
      if (!dom.lightbox.hidden) {
        closeLightbox();
        return;
      }
      if (!dom.settingsModal.hidden) {
        closeSettings();
        return;
      }
      closeDrawers();
    });

    // 窗口尺寸变化时收起抽屉（跨断点）
    window.addEventListener('resize', function () {
      if (window.innerWidth > 1279) dom.app.classList.remove('ai-open');
      if (window.innerWidth > 1099) dom.app.classList.remove('friends-open');
      updateScrim();
    });
  }

  async function toggleBackend(forceRunning) {
    if (isFollowMode()) {
      toast('只读跟随模式不支持启动 / 停止采集', 'info', 2600);
      return;
    }
    const running = !!S.state.running;
    const target = typeof forceRunning === 'boolean' ? forceRunning : !running;
    if (target === running) return;
    if (target && S.monitored.size === 0) {
      toast('请先在左栏勾选要监听的好友或群聊', 'warn', 3000);
      return;
    }
    if (MOCK) {
      S.state.running = target;
      S.state.wechat_found = target;
      renderTopbar();
      toast(target ? '已开始监听（演示）' : '已结束监听（演示）', 'ok');
      return;
    }
    try {
      const result = await api(target ? '/api/backend/start' : '/api/backend/stop', {
        method: 'POST',
        body: {},
      });
      if (result && typeof result.running === 'boolean') S.state.running = result.running;
      else S.state.running = target;
      renderTopbar();
      toast(S.state.running ? '已开始监听' : '已结束监听（已加载消息保留）', 'ok', 2400);
    } catch (err) {
      toast('操作失败：' + err.message, 'error', 4200);
    }
  }

  // ===========================================================================
  // 10. 启动 / 演示模式
  // ===========================================================================
  async function init() {
    mountIcons();
    bindEvents();
    updateQuestionCount();
    renderModelRow();
    renderScopeChips();
    renderQuickChips();
    updateNavCounts();
    renderTopbar();

    if (MOCK) {
      initMock();
    } else if (!TOKEN) {
      S.sse = 'closed';
      setSse('closed');
      dom.messageEmpty.hidden = false;
      dom.messageEmptyTitle.textContent = '页面缺少会话令牌';
      dom.messageEmptyDesc.textContent =
        '当前页面不是由 web_app.py 提供的。请运行 python web_app.py，并通过它启动时打印的地址打开页面（不要直接打开静态文件）。';
      toast('页面令牌缺失，请通过 web_app.py 打开页面', 'error', 6000);
      return;
    } else {
      connectSse();
      await loadInitialBootstrap();
      // v4：联系人（过滤 + 首字母）异步加载；失败自动回退 /api/groups + 不过滤
      loadContacts({ silent: true });
    }

    loadVideoAutoStatus();
    handleDebugParams();
  }

  async function loadInitialBootstrap() {
    try {
      const data = await api('/api/bootstrap');
      applyBootstrap(data, { merge: false });
    } catch (err) {
      setSse('closed');
      dom.messageEmpty.hidden = false;
      dom.messageEmptyTitle.textContent = '无法加载后端数据';
      dom.messageEmptyDesc.textContent =
        (err.message || '未知错误') + '。请确认后端仍在运行，然后刷新页面重试。';
      if (err.code === 'network') {
        toast('连接后端失败，2 秒后自动重试…', 'warn', 2600);
        setTimeout(loadInitialBootstrap, 2000);
      } else {
        toast('加载失败：' + err.message, 'error', 5000);
      }
    }
  }

  function handleDebugParams() {
    if (qs.has('tab') && qs.get('tab') === 'moments' && S.tab !== 'moments') setTab('moments');
    if (qs.has('settings')) openSettings();
    if (qs.has('ai') && window.innerWidth <= 1279) openDrawer('ai');
    if (qs.has('friends') && window.innerWidth <= 1099) openDrawer('friends');
    if (qs.has('logs')) setLogOpen(true);
    // 截图 / 自测：?group=1 或 ?group=群名 打开群聊列表并选中群
    if (qs.has('group')) {
      const wanted = qs.get('group');
      const target =
        wanted && wanted !== '1'
          ? S.groups.filter(function (group) {
              return group.name.indexOf(wanted) !== -1;
            })[0]
          : S.groups[0];
      setContactList('groups');
      if (target) selectFriend(target.name);
    }
    // 截图 / 自测：?history=1 打开「同步历史」弹层
    if (qs.has('history')) {
      requestAnimationFrame(function () {
        openHistoryPop();
      });
    }
    // 截图 / 自测：?top=1 把消息流滚到最上面（mock、真实、跟随模式都可用）
    if (qs.has('top')) {
      requestAnimationFrame(function () {
        dom.messageScroll.scrollTop = 0;
        S.paused = true;
        updateJumpLatest();
      });
    }
  }

  // ---- 演示模式（mock） ----
  const mockAssets = {};

  function mockImage(label, sub, options) {
    const opts = options || {};
    const width = opts.width || 480;
    const height = opts.height || 320;
    const hue = opts.hue === undefined ? 150 : opts.hue;
    const key = label + '|' + sub + '|' + width + 'x' + height + '|' + hue;
    if (mockAssets[key]) return mockAssets[key];
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, width, height);
    gradient.addColorStop(0, 'hsl(' + hue + ', 38%, 28%)');
    gradient.addColorStop(1, 'hsl(' + ((hue + 55) % 360) + ', 52%, 15%)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, width, height);
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(width * 0.82, height * 0.2, height * 0.42, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(width * 0.16, height * 0.92, height * 0.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = 'rgba(62, 181, 117, 0.75)';
    ctx.lineWidth = 2;
    ctx.strokeRect(14, 14, width - 28, height - 28);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(255,255,255,0.94)';
    ctx.font = '600 ' + Math.round(width / 13) + 'px "Microsoft YaHei UI", "Microsoft YaHei", sans-serif';
    ctx.fillText(label, width / 2, height / 2 - height * 0.05);
    ctx.fillStyle = 'rgba(255,255,255,0.62)';
    ctx.font = '400 ' + Math.round(width / 26) + 'px "Microsoft YaHei UI", "Microsoft YaHei", sans-serif';
    ctx.fillText(sub, width / 2, height / 2 + height * 0.11);
    const url = canvas.toDataURL('image/png');
    mockAssets[key] = url;
    return url;
  }

  function mockGif(label, hue) {
    const key = 'gif|' + label + '|' + hue;
    if (mockAssets[key]) return mockAssets[key];
    const svgMarkup =
      '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">' +
      '<rect width="160" height="160" rx="20" fill="#1e1f22"/>' +
      '<circle cx="80" cy="72" r="36" fill="hsl(' + hue + ', 62%, 52%)">' +
      '<animate attributeName="cy" values="44;96;44" dur="1.2s" repeatCount="indefinite"/></circle>' +
      '<circle cx="68" cy="64" r="5" fill="#0d1a13">' +
      '<animate attributeName="cy" values="36;88;36" dur="1.2s" repeatCount="indefinite"/></circle>' +
      '<circle cx="92" cy="64" r="5" fill="#0d1a13">' +
      '<animate attributeName="cy" values="36;88;36" dur="1.2s" repeatCount="indefinite"/></circle>' +
      '<text x="80" y="146" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#ffcb6b">' +
      label +
      '</text></svg>';
    const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgMarkup);
    mockAssets[key] = url;
    return url;
  }

  function mockFile(name) {
    return 'data:text/plain;charset=utf-8,' + encodeURIComponent('WXBOX 演示文件：' + name + '\n\n这是网页端演示用的占位文件，不会写入项目目录。');
  }

  function mockMessage(seed, overrides) {
    const base = {
      id: 'mock-msg-' + seed,
      friend: '白云一片去悠悠',
      sender: 'friend',
      kind: 'text',
      text: '',
      media_path: '',
      thumb_path: '',
      extra: {},
      ts: nowSec() - (240 - seed) * 60,
    };
    const message = Object.assign(base, overrides || {});
    message.kind_label = KIND_LABELS[message.kind] || message.kind;
    message.sender_label = message.sender === 'self' ? '我' : '对方';
    message.time_text = formatTime(message.ts);
    return message;
  }

  // v3/v4：演示用群聊与联系人（mock 下模拟 /api/contacts）
  const MOCK_GROUPS = [
    { name: '项目联调群', username: 'mock_chatroom_1@chatroom', kind: 'group', member_hint: '4 人', initial: 'X' },
    { name: '家人群', username: 'mock_chatroom_2@chatroom', kind: 'group', member_hint: '3 人', initial: 'J' },
    { name: '前端小分队', username: 'mock_chatroom_3@chatroom', kind: 'group', member_hint: '6 人', initial: 'Q' },
  ];

  const MOCK_CONTACT_INITIALS = {
    白云一片去悠悠: 'B',
    文件传输助手: 'W',
    '产品-王经理': 'W',
    '测试-小助手': 'C',
    张三: 'Z',
    李四: 'L',
    '设计-陈工': 'C',
    '运维-赵工': 'Z',
    '客户-刘总': 'L',
    '前端-周同学': 'Z',
    '后端-吴同学': 'W',
    '家人-妈妈': 'J',
    刘工: 'L',
    何远: 'H',
    项目联调群: 'X',
    家人群: 'J',
    前端小分队: 'Q',
  };

  function mockContactsData() {
    const friendNames = [
      '白云一片去悠悠', '产品-王经理', '测试-小助手', '设计-陈工',
      '运维-赵工', '客户-刘总', '前端-周同学', '家人-妈妈', '刘工', '何远',
    ];
    const friends = friendNames.map(function (name) {
      return {
        name: name,
        username: '',
        kind: 'friend',
        initial: MOCK_CONTACT_INITIALS[name] || '',
        has_history: true,
      };
    });
    const groups = MOCK_GROUPS.map(function (group) {
      return {
        name: group.name,
        username: group.username,
        kind: 'group',
        initial: group.initial,
        has_history: true,
      };
    });
    return {
      ok: true,
      include_all: false,
      friends: friends,
      groups: groups,
      count: friends.length + groups.length,
      friend_count: friends.length,
      group_count: groups.length,
      initials: Object.assign({}, MOCK_CONTACT_INITIALS),
      history_names: friendNames.concat(
        groups.map(function (group) {
          return group.name;
        })
      ),
      account_wxid: 'wxid_demo_a',
      account: { wxid: 'wxid_demo_a', nick_name: '演示账号 A' },
      cached: false,
    };
  }

  /** mock：模拟切换到另一个微信账号后的联系人列表（用于验证账号切换清理） */
  function mockContactsDataAlt() {
    const friendNames = ['刘工', '何远', '家人-妈妈', '产品-王经理', '设计-陈工', '白云一片去悠悠'];
    const friends = friendNames.map(function (name) {
      return {
        name: name,
        username: '',
        kind: 'friend',
        initial: MOCK_CONTACT_INITIALS[name] || '',
        has_history: true,
      };
    });
    const groups = MOCK_GROUPS.slice(0, 2).map(function (group) {
      return {
        name: group.name,
        username: group.username,
        kind: 'group',
        initial: group.initial,
        has_history: true,
      };
    });
    return {
      ok: true,
      include_all: false,
      friends: friends,
      groups: groups,
      count: friends.length + groups.length,
      friend_count: friends.length,
      group_count: groups.length,
      initials: Object.assign({}, MOCK_CONTACT_INITIALS),
      history_names: friendNames.concat(
        groups.map(function (group) {
          return group.name;
        })
      ),
      account_wxid: 'wxid_demo_b',
      account: { wxid: 'wxid_demo_b', nick_name: '演示账号 B' },
      cached: false,
    };
  }

  /** mock：POST /api/contacts/refresh 的返回（每次切换账号 + 不同列表） */
  function mockRefreshContactsData() {
    S.mockAltAccount = !S.mockAltAccount;
    const contacts = S.mockAltAccount ? mockContactsDataAlt() : mockContactsData();
    return {
      ok: true,
      contacts: contacts,
      replaced_friends: contacts.friends.length,
      account_changed: true,
      monitoring: [],
      mock: true,
    };
  }

  function buildMockMessages() {
    const imageA = mockImage('西湖 · 傍晚', 'WXBOX DEMO · PNG', { hue: 165 });
    const imageB = mockImage('深色主题探索', 'UI DESIGN · PNG', { hue: 255, width: 400, height: 400 });
    const imageC = mockImage('家里的桂花', 'MOMENTS · PNG', { hue: 40, width: 400, height: 300 });
    const gifA = mockGif('好耶', 150);
    const gifB = mockGif('晚安', 270);
    const fileA = mockFile('项目需求文档-v3.pdf');

    const list = [];
    const add = function (seed, overrides) {
      list.push(mockMessage(seed, overrides));
    };

    add(1, { kind: 'system', text: '开始监听「白云一片去悠悠」的消息' });
    add(2, {
      friend: '白云一片去悠悠',
      text: '在吗？今天的需求文档我放群里了，你看一下',
      ts: nowSec() - 172 * 60,
    });
    add(3, {
      friend: '白云一片去悠悠',
      text: '这个功能什么时候能上线？客户那边在催了',
      ts: nowSec() - 168 * 60,
    });
    add(4, {
      friend: '白云一片去悠悠',
      sender: 'self',
      text: '收到，我下午三点前给你反馈 👌',
      ts: nowSec() - 165 * 60,
    });
    add(5, {
      friend: '白云一片去悠悠',
      kind: 'image',
      media_path: imageA,
      extra: { size_text: '216.0 KB' },
      ts: nowSec() - 150 * 60,
    });
    add(6, {
      friend: '白云一片去悠悠',
      kind: 'emoji',
      media_path: gifA,
      extra: { animated: true, n_frames: 12, size_text: '48.6 KB' },
      ts: nowSec() - 148 * 60,
    });
    add(7, {
      friend: '产品-王经理',
      kind: 'file',
      text: '项目需求文档-v3.pdf',
      media_path: fileA,
      extra: { size_text: '1.8 MB' },
      ts: nowSec() - 132 * 60,
    });
    add(8, {
      friend: '白云一片去悠悠',
      kind: 'voice',
      extra: { duration: 8 },
      ts: nowSec() - 120 * 60,
    });
    add(9, {
      friend: '白云一片去悠悠',
      sender: 'self',
      text: '文件已收到，稍后拉个会',
      ts: nowSec() - 118 * 60,
    });
    add(10, {
      friend: '产品-王经理',
      text: '明天上午十点开个短会同步一下进度',
      ts: nowSec() - 100 * 60,
    });
    add(11, {
      friend: '设计-陈工',
      kind: 'image',
      media_path: imageB,
      extra: { size_text: '182.4 KB' },
      ts: nowSec() - 88 * 60,
    });
    add(12, {
      friend: '家人-妈妈',
      text: '晚上回家吃饭吗？',
      ts: nowSec() - 76 * 60,
    });
    add(13, {
      friend: '家人-妈妈',
      sender: 'self',
      text: '回，六点到家 🍲',
      ts: nowSec() - 74 * 60,
    });
    add(14, {
      friend: '测试-小助手',
      text: '截图里这个报错帮忙看下，谢谢！',
      ts: nowSec() - 62 * 60,
    });
    add(15, {
      friend: '运维-赵工',
      text: '服务器证书明天到期，需要续一下',
      ts: nowSec() - 54 * 60,
    });
    add(16, {
      friend: '白云一片去悠悠',
      text: '这个方案我改好了，你有空帮忙过一遍～',
      ts: nowSec() - 40 * 60,
    });
    add(17, {
      friend: '白云一片去悠悠',
      kind: 'emoji',
      media_path: gifB,
      extra: { animated: true, n_frames: 8, size_text: '32.1 KB' },
      ts: nowSec() - 39 * 60,
    });
    add(18, {
      friend: '客户-刘总',
      text: '合同已经收到了，法务这周内回复。另外接口文档能否同步一份？',
      ts: nowSec() - 28 * 60,
    });
    add(19, {
      friend: '白云一片去悠悠',
      sender: 'self',
      text: '好的，我看下再回复你',
      ts: nowSec() - 18 * 60,
    });
    add(20, {
      friend: '前端-周同学',
      kind: 'image',
      media_path: imageC,
      extra: { size_text: '240.8 KB' },
      ts: nowSec() - 9 * 60,
    });
    add(21, {
      friend: '白云一片去悠悠',
      text: '刚发的文件收到了吗？',
      ts: nowSec() - 3 * 60,
    });
    add(22, {
      friend: '测试-小助手',
      kind: 'system',
      text: '消息采集正常，最近一次 OCR 耗时 186ms',
      ts: nowSec() - 1 * 60,
    });
    // v3：群聊消息（extra.is_group + extra.sender_name）
    add(23, {
      friend: '项目联调群',
      text: '下午三点联调，大家把环境和账号准备好，有问题群里说',
      extra: { is_group: true, sender_name: '王经理', sender_initial: 'W' },
      ts: nowSec() - 52 * 60,
    });
    add(24, {
      friend: '项目联调群',
      sender: 'self',
      text: '收到，我这边环境已就绪，三点准时上线',
      extra: { is_group: true },
      ts: nowSec() - 49 * 60,
    });
    add(25, {
      friend: '项目联调群',
      kind: 'image',
      media_path: imageB,
      extra: { is_group: true, sender_name: '陈工', sender_initial: 'C', size_text: '182.4 KB' },
      ts: nowSec() - 41 * 60,
    });
    add(26, {
      friend: '项目联调群',
      text: '接口文档我也更新了一版，记得看最新链接',
      extra: { is_group: true, sender_name: '周同学', sender_initial: 'Z' },
      ts: nowSec() - 33 * 60,
    });
    add(27, {
      friend: '家人群',
      text: '周末回来吃饭吗？给你做红烧肉',
      extra: { is_group: true, sender_name: '妈妈', sender_initial: 'J' },
      ts: nowSec() - 26 * 60,
    });
    add(28, {
      friend: '家人群',
      sender: 'self',
      text: '回，周六下午到，别做太多',
      extra: { is_group: true },
      ts: nowSec() - 24 * 60,
    });
    add(29, {
      friend: '前端小分队',
      text: '新的组件库已经发版了，升级后记得跑一遍回归',
      extra: { is_group: true, sender_name: '周同学', sender_initial: 'Z' },
      ts: nowSec() - 12 * 60,
    });
    add(30, {
      friend: '项目联调群',
      text: '我这边把测试账号也准备好了，发到群文件了',
      extra: { is_group: true, sender_name: '刘工', sender_initial: 'L' },
      ts: nowSec() - 8 * 60,
    });
    add(31, {
      friend: '刘工',
      text: '联调环境我重新部署了一份，地址发你了',
      ts: nowSec() - 20 * 60,
    });
    add(32, {
      friend: '何远',
      text: '周报我晚点发你邮箱，记得查收',
      ts: nowSec() - 6 * 60,
    });
    // v7：视频消息（mock 无 db_local_id，展示「视频（未缓存）」占位）
    add(33, {
      friend: '白云一片去悠悠',
      kind: 'video',
      text: '视频 12秒',
      extra: { app_type: 'video', duration: 12 },
      ts: nowSec() - 2 * 60,
    });
    return list;
  }

  function buildMockMoments() {
    const now = nowSec();
    // v2：朋友圈媒体为进程内存 key（thumb_key / avatar_key），mock 用 data URL 模拟
    const registerMedia = function (key, dataUrl) {
      mockMediaMap[key] = dataUrl;
      return key;
    };
    const imageKey = function (key, label, sub, options) {
      return registerMedia(key, mockImage(label, sub, options));
    };
    const images = [
      imageKey('t_mock_0', '西湖', 'HANGZHOU', { hue: 165, width: 800, height: 600 }),
      imageKey('t_mock_1', '断桥', 'WEST LAKE', { hue: 200, width: 800, height: 600 }),
      imageKey('t_mock_2', '晚霞', 'SUNSET', { hue: 15, width: 800, height: 600 }),
      imageKey('t_mock_3', '深色主题', 'UI DESIGN', { hue: 255, width: 800, height: 800 }),
      imageKey('t_mock_4', '桂花', 'OSMANTHUS', { hue: 42, width: 800, height: 600 }),
      imageKey('t_mock_5', '院子', 'HOMETOWN', { hue: 95, width: 800, height: 600 }),
    ];
    const avatarKey = imageKey('a_mock_0', '白', 'AVATAR', { hue: 150, width: 96, height: 96 });
    // 失效 key：对应 data URL 无法解码，用来验证图片加载失败的紧凑占位
    registerMedia('t_mock_broken', 'data:image/png;base64,AAAA');
    const nine = [];
    for (let i = 0; i < 9; i++) {
      nine.push({
        type: 'image',
        thumb_key: imageKey('t_mock_grid_' + i, '图 ' + (i + 1), 'MOMENTS GRID', {
          hue: 120 + i * 27,
          width: 500,
          height: 500,
        }),
        thumb_mime: 'image/jpeg',
        width: 500,
        height: 500,
      });
    }
    const make = function (index, overrides) {
      const ts = now - (index + 1) * 47 * 60;
      return Object.assign(
        {
          id: 'mock-moment-' + index,
          username: 'wxid_demo_' + index,
          author: '白云一片去悠悠',
          avatar: '',
          avatar_key: '',
          create_time: ts,
          time_text: formatMomentTime(ts),
          content: '',
          location: '',
          link: null,
          media: [],
          comments: [],
          likes: [],
          raw_comment_count: 0,
        },
        overrides || {}
      );
    };
    return [
      make(0, {
        author: '白云一片去悠悠',
        avatar_key: avatarKey,
        content: '周末去了趟西湖，天气很好，随手拍了几张 🌿',
        location: '杭州 · 西湖',
        media: [
          { type: 'image', thumb_key: images[0], thumb_mime: 'image/jpeg', width: 800, height: 600 },
          { type: 'image', thumb_key: images[1], thumb_mime: 'image/jpeg', width: 800, height: 600 },
          // 失效内存 key 示例：验证九宫格内的紧凑「图片已失效」占位
          { type: 'image', thumb_key: 't_mock_broken', thumb_mime: 'image/jpeg', width: 800, height: 600 },
        ],
        likes: ['张三', '李四', '产品-王经理'],
        comments: [
          { author: '张三', content: '拍得真好看！' },
          { author: '家人-妈妈', content: '注意保暖，别着凉' },
        ],
        raw_comment_count: 2,
      }),
      make(1, {
        author: '产品-王经理',
        content: '需求评审会纪要已同步到文档，感谢大家配合。下周开始进入联调阶段。',
        link: { title: 'Q4 产品路线图（内部分享）', url: 'https://example.com/roadmap' },
        media: [{ type: 'image', thumb_key: 't_mock_broken', thumb_mime: 'image/jpeg', width: 800, height: 600 }],
        likes: ['白云一片去悠悠', '设计-陈工'],
        comments: [{ author: '客户-刘总', content: '收到，我们这边同步安排测试资源。' }],
        raw_comment_count: 1,
      }),
      make(2, {
        author: '设计-陈工',
        content: '新版的深色主题探索，欢迎提意见 👇',
        media: [{ type: 'image', thumb_key: images[3], thumb_mime: 'image/jpeg', width: 800, height: 800 }],
        likes: ['前端-周同学', '产品-王经理', '白云一片去悠悠', '测试-小助手'],
        comments: [{ author: '前端-周同学', content: '这个绿色点缀很舒服' }],
        raw_comment_count: 1,
      }),
      make(3, {
        author: '运维-赵工',
        content: '机房巡检 vlog，一切正常。',
        media: [
          { type: 'video', path: '', cover: '', duration: 15, video_url: '' },
          { type: 'image', thumb_key: images[4], thumb_mime: 'image/jpeg', width: 800, height: 600 },
        ],
        likes: ['测试-小助手'],
        comments: [],
        raw_comment_count: 0,
      }),
      make(4, {
        author: '家人-妈妈',
        content: '家里的桂花开了，香得很。',
        location: '老家 · 院子',
        media: [
          { type: 'image', thumb_key: images[4], thumb_mime: 'image/jpeg', width: 800, height: 600 },
          { type: 'image', thumb_key: images[5], thumb_mime: 'image/jpeg', width: 800, height: 600 },
        ],
        likes: ['白云一片去悠悠', '张三'],
        comments: [{ author: '白云一片去悠悠', content: '周末回去看看您' }],
        raw_comment_count: 1,
      }),
      make(5, {
        author: '前端-周同学',
        content: '今天把消息流的虚拟滚动做完了，400 张卡片也不卡 🎉',
        likes: ['设计-陈工', '李四', '后端-吴同学'],
        comments: [
          { author: '后端-吴同学', content: '牛啊' },
          { author: '设计-陈工', content: '终于不用等加载了' },
        ],
        raw_comment_count: 2,
      }),
      make(6, {
        author: '白云一片去悠悠',
        content: '',
        media: nine,
        likes: ['李四'],
        comments: [],
        raw_comment_count: 0,
      }),
      make(7, {
        author: '客户-刘总',
        content: '合同已收到，法务这周内回复。另外接口文档麻烦同步一份给我们。',
        likes: ['产品-王经理'],
        comments: [],
        raw_comment_count: 0,
      }),
    ];
  }

  function mockBootstrap() {
    const messages = buildMockMessages();
    const friendNames = [
      '白云一片去悠悠', '文件传输助手', '产品-王经理', '测试-小助手', '张三', '李四',
      '设计-陈工', '运维-赵工', '客户-刘总', '前端-周同学', '后端-吴同学', '家人-妈妈',
      '刘工', '何远',
    ];
    const stats = new Map();
    messages.forEach(function (message) {
      const item = stats.get(message.friend) || { count: 0, last_ts: 0 };
      item.count += 1;
      item.last_ts = Math.max(item.last_ts, message.ts);
      stats.set(message.friend, item);
    });
    const friends = friendNames.map(function (name) {
      const item = stats.get(name) || { count: 0, last_ts: 0 };
      return {
        name: name,
        wxid: 'wxid_demo_' + hashHue(name),
        letter: name.slice(0, 1),
        count: item.count,
        last_ts: item.last_ts,
      };
    });
    const moments = buildMockMoments();
    const now = nowSec();
    return {
      server: {
        version: '1.0.0',
        mode: 'fake',
        started_at: now - 3600,
        now: now,
        host: '127.0.0.1',
        port: 0,
        backend_note: '演示数据（?mock=1），仅用于界面预览与自测，不连接真实微信。',
      },
      state: {
        running: true,
        wechat_found: true,
        chat_title: '白云一片去悠悠',
        monitoring: ['白云一片去悠悠', '产品-王经理', '家人-妈妈'],
        error: null,
        tick: 186,
        monitor_state: 'monitoring',
        last_ocr_ms: 186,
        friends_count: friends.length,
        message_total: messages.length,
        per_friend_counts: {},
      },
      friends: friends,
      messages: messages,
      logs: [
        { ts: now - 420, level: 'info', text: '演示模式：已加载内置样例消息（?mock=1）' },
        { ts: now - 380, level: 'info', text: '监听已启动：白云一片去悠悠、产品-王经理、家人-妈妈' },
        { ts: now - 260, level: 'info', text: '收到「白云一片去悠悠」的一条图片消息，已保存到 media/originals/' },
        { ts: now - 200, level: 'warn', text: '演示：模拟一次 OCR 置信度偏低的提示（仅演示，不影响真实采集）' },
        { ts: now - 120, level: 'info', text: '收到「产品-王经理」的一条文件消息：项目需求文档-v3.pdf' },
        { ts: now - 42, level: 'error', text: '演示：模拟一条错误日志，用于检查日志抽屉的错误配色' },
      ],
      llm: {
        configured: true,
        model: 'deepseek-flash',
        model_label: 'DeepSeek V4.1 Flash 1017',
        base_url: 'https://api.deepseek.com',
        api_key_masked: 'sk-…9f2c',
        thinking: true,
        attach_images: true,
        busy: false,
        models: DEFAULT_MODELS.slice(),
      },
      moments: {
        status: {
          available: true,
          reason: '',
          syncing: false,
          count: moments.length,
          limit: 10,
          synced_at: now - 600,
          account: { wxid: 'wxid_demo', nick_name: '白云一片去悠悠' },
          error: null,
          auto: true,
          auto_interval: 30,
        },
        count: moments.length,
        synced_at: now - 600,
        account: { wxid: 'wxid_demo', nick_name: '白云一片去悠悠' },
      },
      mockMoments: moments,
    };
  }

  const MOCK_ANALYSIS_MARKDOWN = [
    '## 对话摘要',
    '',
    '最近与 **白云一片去悠悠** 的沟通集中在两件事：',
    '',
    '1. **需求文档 v3**：对方已发送文件，需要你评审后回复修改意见。',
    '2. **上线排期**：客户在催，建议今天下班前给出明确时间点。',
    '',
    '### 待办事项',
    '- [ ] 下午三点前反馈需求文档评审意见',
    '- [x] 确认文件已收到',
    '- [ ] 与产品确认本周五的联调窗口',
    '- [ ] 回复客户关于接口升级的疑问',
    '',
    '> 对方在 14:32 提到「客户那边在催了」，建议优先处理。',
    '',
    '### 风险提示',
    '- 需求文档里 **鉴权方式** 与现有实现不一致，需要提前对齐。',
    '- 有一条语音消息未接收，可能遗漏关键信息。',
    '',
    '| 事项 | 负责人 | 截止时间 |',
    '| --- | --- | --- |',
    '| 评审需求文档 | 我 | 今天 15:00 |',
    '| 确认联调窗口 | 产品-王经理 | 本周五 |',
    '| 回复客户 | 我 | 明天上午 |',
    '',
    '参考文档：[DeepSeek 开放平台](https://platform.deepseek.com)',
  ].join('\n');

  const MOCK_MOMENTS_ANALYSIS_MARKDOWN = [
    '## 朋友圈摘要',
    '',
    '最近的朋友圈呈现出 **工作 / 生活** 两条主线：',
    '',
    '1. **工作**：产品王经理发布了需求评审纪要，联调即将开始；设计陈工在征集深色主题意见。',
    '2. **生活**：白云去了西湖，家人分享了桂花与老家院子，整体情绪温暖平和。',
    '',
    '### 人物关系',
    '- **白云一片去悠悠** 与 **产品-王经理**、**设计-陈工** 互动频繁，可能处于同一项目组。',
    '- **家人-妈妈** 与白云的评论互动自然，关系亲近。',
    '',
    '### 值得关注',
    '- 客户刘总在朋友圈提到合同与接口文档，与聊天里的催办信息一致，建议同步处理。',
    '- 运维赵工的机房巡检动态说明近期可能有变更窗口。',
    '',
    '> 隐私提示：部分动态包含位置信息（西湖、老家院子），转发或引用时注意脱敏。',
  ].join('\n');

  const MOCK_REASONING = [
    '先梳理消息的时间顺序：需求文档在 132 分钟前由王经理发出，随后白云两次追问上线时间。',
    '对方语气从「在吗」到「客户那边在催了」，紧迫感明显上升，需要优先回应。',
    '文件消息之后没有明确的确认回复，可以列为待办；语音消息未接收，作为风险项标注。',
    '客户刘总的合同进度与接口文档需求也需要在摘要里体现，避免遗漏。',
  ].join('\n');

  function initMock() {
    S.mode = 'fake';
    const data = mockBootstrap();
    const initialMoments = qs.get('tab') === 'moments';
    S.server = data.server;
    S.state = data.state;
    S.friends = data.friends;
    S.friendMap = new Map(data.friends.map(function (friend) { return [friend.name, friend]; }));
    S.llm = normalizeLlm(data.llm);
    S.moments.status = data.moments.status;
    S.moments.count = data.moments.count;
    S.moments.syncedAt = data.moments.synced_at;
    S.moments.account = data.moments.account;
    applyMomentsAutoStatus(data.moments.status);
    S.moments.items = data.mockMoments;
    S.moments.loaded = true;
    S.moments.freshnessDismissed = false;
    S.moments.syncLimit = 10;
    S.moments.syncLimitTouched = false;
    S.moments.lastLimit = Number(data.moments.status.limit) || 10;
    S.moments.status.limit = S.moments.lastLimit;
    // 截图 / 自测：?stale=N 把 mock 朋友圈整体往前推 N 天，用于验证新鲜度提示条与年份显示
    if (qs.has('stale')) {
      const days = Math.max(1, Number(qs.get('stale')) || 10);
      S.moments.items = S.moments.items.map(function (item) {
        const createTime = (Number(item.create_time) || nowSec()) - days * 86400;
        return Object.assign({}, item, {
          create_time: createTime,
          time_text: formatMomentTime(createTime),
        });
      });
    }
    // 保留原始 mock 数据，供「清空朋友圈」后再次同步
    S.moments.mockAll = S.moments.items.slice();
    S.sse = 'open';
    S.activeFriend = '白云一片去悠悠';
    S.scope = 'friend';
    S.monitored = new Set(data.state.monitoring);

    data.messages.forEach(function (message) {
      addMessage(message);
    });
    data.logs.forEach(function (entry) {
      appendLog(entry);
    });
    // v4：mock 联系人（已过滤 + 拼音首字母）
    if (qs.has('contactsfail')) {
      S.contacts.available = false;
      S.contacts.loaded = false;
      S.contacts.error = '演示：联系人接口不可用';
      setGroups(MOCK_GROUPS);
      S.groupsSource = 'groups';
    } else {
      applyContacts(mockContactsData(), { silent: true });
    }

    S.analysis = {
      status: 'done',
      busy: false,
      jobId: 'mock-job-1',
      content: initialMoments ? MOCK_MOMENTS_ANALYSIS_MARKDOWN : MOCK_ANALYSIS_MARKDOWN,
      reasoning: MOCK_REASONING,
      usage: { prompt_tokens: 1832, completion_tokens: 486, total_tokens: 2318, reasoning_tokens: 512 },
      elapsedMs: 8420,
      cancelled: false,
      error: '',
      question: '请总结最近对话并列出待办事项',
      scopeLabel: initialMoments ? '全部朋友圈' : '白云一片去悠悠',
      messageCount: initialMoments ? 8 : 42,
      imageCount: initialMoments ? 6 : 2,
      modelLabel: 'DeepSeek V4.1 Flash 1017',
      startedAt: 0,
    };
    dom.aiQuestion.value = initialMoments
      ? '请总结这些朋友圈的整体主题和值得关注的内容。'
      : '请总结最近对话并列出待办事项';
    dom.optThinking.checked = true;
    dom.optImages.checked = true;
    updateQuestionCount();

    // 截图 / 自测：?all=1 时默认展示「全部消息」，方便一次看到所有消息类型
    if (qs.has('all')) {
      S.activeFriend = null;
      S.scope = 'all';
      S.analysis.scopeLabel = '全部消息';
    }
    // 截图 / 自测：?nokey=1 展示「未配置模型」真实状态，避免 mock 脱敏 key 进入截图
    if (qs.has('nokey')) {
      S.llm.configured = false;
      S.llm.api_key_masked = '';
      S.analysis = newAnalysis();
      dom.aiQuestion.value = '';
      updateQuestionCount();
      renderModelRow();
    }

    S.bootstrapped = true;
    renderAll({ scrollBottom: true });

    // mock 模式下的错误收集，便于 headless 自测检查（--dump-dom 可见）
    const sink = el('div', 'mock-error-sink');
    sink.id = 'mockErrorSink';
    sink.hidden = true;
    document.body.appendChild(sink);
    window.addEventListener('error', function (event) {
      sink.textContent += '[error] ' + (event.message || '') + '\n';
    });
    window.addEventListener('unhandledrejection', function (event) {
      sink.textContent += '[rejection] ' + ((event.reason && event.reason.message) || event.reason) + '\n';
    });

    if (MOCK_LIVE) {
      let tick = 0;
      setInterval(function () {
        tick += 1;
        const friend = tick % 3 === 0 ? '产品-王经理' : '白云一片去悠悠';
        const message = mockMessage(1000 + tick, {
          friend: friend,
          text: tick % 4 === 0 ? '演示新消息 #' + tick + '：消息流已实时追加' : '演示新消息 #' + tick + '，用于检查自动滚动与「回到最新」浮标',
          ts: nowSec(),
        });
        onLiveMessage(message);
      }, 3000);
    }
  }

  function mockSyncMoments() {
    if (S.moments.syncing) return;
    let limit = Number(S.moments.syncLimit) || 10;
    if ([5, 10, 20].indexOf(limit) === -1) limit = 10;
    S.moments.syncing = true;
    renderMomentsAll();
    toast('开始同步最近 ' + limit + ' 条朋友圈（演示）…', 'info', 1600);
    setTimeout(function () {
      const all =
        S.moments.mockAll && S.moments.mockAll.length ? S.moments.mockAll : S.moments.items.slice();
      S.moments.syncing = false;
      S.moments.freshnessDismissed = false;
      S.moments.items = all.slice(0, Math.min(limit, all.length));
      S.moments.count = S.moments.items.length;
      S.moments.lastLimit = limit;
      S.moments.status = Object.assign({}, S.moments.status, {
        available: true,
        syncing: false,
        count: S.moments.items.length,
        limit: limit,
        synced_at: nowSec(),
        error: null,
      });
      S.moments.syncedAt = nowSec();
      if (S.activeMomentId && !S.moments.items.some(function (item) { return item.id === S.activeMomentId; })) {
        S.activeMomentId = '';
        if (S.scope === 'moment') S.scope = 'moments';
      }
      renderMomentsAll();
      renderScopeChips();
      toast(
        '朋友圈已同步：' + S.moments.items.length + ' 条（最近 ' + limit + ' 条，演示）',
        'ok',
        2600
      );
    }, 1400);
  }

  /** mock：模拟 /api/history/load（不足条数时返回 notice 提示） */
  function mockLoadHistory(friend, limit) {
    const available = 3 + (hashHue(friend) % 9); // 3 ~ 11 条本机缓存
    const requested = Number(limit) || 0;
    const take = requested > 0 ? Math.min(requested, available) : available;
    const existing = S.messages.filter(function (message) {
      return message.friend === friend;
    });
    const oldest = existing.length
      ? existing.reduce(function (min, message) {
          return Math.min(min, Number(message.ts) || nowSec());
        }, nowSec())
      : nowSec();
    const items = [];
    for (let i = take; i >= 1; i--) {
      const ts = oldest - i * 300;
      items.push({
        id: 'mock-hist-' + hashHue(friend) + '-' + i,
        friend: friend,
        kind: 'text',
        sender: i % 3 === 0 ? 'self' : 'friend',
        text: '（历史消息 ' + i + '）这是从本机缓存同步进来的演示聊天记录。',
        media_path: '',
        thumb_path: '',
        extra: {},
        ts: ts,
        kind_label: '文字',
        sender_label: i % 3 === 0 ? '我' : '对方',
        time_text: formatTime(ts),
      });
    }
    let added = 0;
    items.forEach(function (item) {
      if (addHistoryMessage(item)) added += 1;
    });
    refreshFriendStats(friend);
    S.state.message_total = Math.max(Number(S.state.message_total) || 0, S.messages.length);
    if (S.activeFriend === friend) renderMessages({ scrollBottom: true });
    renderFriends();
    renderTopbar();
    updateNavCounts();
    const notice =
      requested > available
        ? '「' + friend + '」在本机只缓存了 ' + available + ' 条聊天记录，不足 ' + requested + ' 条'
        : null;
    toast(notice || '已同步 ' + added + ' 条历史消息（本机可用 ' + available + ' 条）', notice ? 'warn' : 'ok', 4800);
  }

  function mockRunAnalysis() {
    if (S.analysis.busy) return;
    const question = dom.aiQuestion.value.trim();
    if (!question) {
      toast('先输入问题，或点一个快捷指令', 'warn');
      return;
    }
    const isMoments = S.tab === 'moments';
    S.analysis = newAnalysis();
    S.analysis.busy = true;
    S.analysis.status = 'running';
    S.analysis.question = question;
    S.analysis.scopeLabel = isMoments ? '全部朋友圈' : S.activeFriend || '全部消息';
    S.analysis.modelLabel = 'DeepSeek V4.1 Flash 1017';
    S.analysis.startedAt = nowSec();
    S.analysisStick = true;
    renderAnalysis();
    startElapsedTimer();

    const reasoning = MOCK_REASONING;
    const content = isMoments ? MOCK_MOMENTS_ANALYSIS_MARKDOWN : MOCK_ANALYSIS_MARKDOWN;
    let reasoningIndex = 0;
    let contentIndex = 0;
    const reasoningTimer = setInterval(function () {
      reasoningIndex = Math.min(reasoning.length, reasoningIndex + 14);
      S.analysis.reasoning = reasoning.slice(0, reasoningIndex);
      scheduleReasoningRender();
      if (reasoningIndex >= reasoning.length) clearInterval(reasoningTimer);
    }, 60);
    setTimeout(function () {
      const contentTimer = setInterval(function () {
        contentIndex = Math.min(content.length, contentIndex + 18);
        S.analysis.content = content.slice(0, contentIndex);
        scheduleContentRender();
        if (contentIndex >= content.length) {
          clearInterval(contentTimer);
          S.analysis.busy = false;
          S.analysis.status = 'done';
          S.analysis.usage = { prompt_tokens: 1832, completion_tokens: 486, total_tokens: 2318, reasoning_tokens: 512 };
          S.analysis.elapsedMs = 8420;
          stopElapsedTimer();
          pushHistory(question, content);
          renderAnalysis();
          toast('分析完成（演示）', 'ok', 2000);
        }
      }, 32);
    }, 700);
  }

  function applyMockSettings(patch) {
    if (patch.api_key) {
      const key = String(patch.api_key);
      S.llm.configured = true;
      S.llm.api_key_masked = 'sk-…' + key.slice(-4);
      S.settings.clearKey = false;
      dom.clearKeyBtn.hidden = false;
    } else if (patch.api_key === 'clear' || S.settings.clearKey) {
      S.llm.configured = false;
      S.llm.api_key_masked = '';
    }
    S.llm.model = patch.model;
    S.llm.base_url = patch.base_url;
    S.llm.thinking = !!patch.thinking;
    S.llm.attach_images = !!patch.attach_images;
    const hit = (S.llm.models || DEFAULT_MODELS).filter(function (model) { return model.id === patch.model; })[0];
    S.llm.model_label = (hit && hit.label) || 'DeepSeek V4.1 Flash 1017';
    dom.settingsFormError.hidden = true;
    dom.optThinking.checked = S.llm.thinking;
    dom.optImages.checked = S.llm.attach_images;
    renderModelRow();
    renderAnalysis();
    if (!S.settings.testing) {
      closeSettings();
      toast('模型配置已保存（演示）', 'ok', 2400);
    }
  }

  // ===========================================================================
  // 启动
  // ===========================================================================
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
