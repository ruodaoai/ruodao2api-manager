(function () {
  'use strict';

  var state = {
    status: null,
    accounts: [],
    models: [],
    keys: [],
    settings: null,
    usage: null,
    guard: null,
    events: [],
    tab: 'overview',
    accountFilter: 'all',
    modelFilter: 'all',
    modelQuery: '',
    logFilter: 'all',
    usageRange: 86400,
    quickstart: 'openai',
    expanded: {},
    busy: {},
    login: null,
    add: null,
    scan: null
  };

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function num(value) {
    var n = Number(value);
    if (!isFinite(n)) { return '0'; }
    return n.toLocaleString('zh-CN');
  }

  function compact(value) {
    var n = Number(value || 0);
    if (n >= 100000000) { return (n / 100000000).toFixed(n >= 1000000000 ? 0 : 1) + '亿'; }
    if (n >= 10000) { return (n / 10000).toFixed(n >= 100000 ? 0 : 1) + '万'; }
    if (n >= 1000) { return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k'; }
    return String(n);
  }

  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function fmtTime(ts) {
    if (!ts) { return '—'; }
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) { return '—'; }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function fmtDate(ts) {
    if (!ts) { return '—'; }
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) { return '—'; }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function dateInputValue(ts) {
    var text = fmtDate(ts);
    return text === '—' ? '' : text;
  }

  function dateInputTs(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || '').trim());
    if (!match) { return 0; }
    var d = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 23, 59, 59);
    return Math.floor(d.getTime() / 1000);
  }

  function timeAgo(ts) {
    if (!ts) { return '—'; }
    var diff = Date.now() / 1000 - Number(ts);
    if (diff < 60) { return '刚刚'; }
    if (diff < 3600) { return Math.floor(diff / 60) + ' 分钟前'; }
    if (diff < 86400) { return Math.floor(diff / 3600) + ' 小时前'; }
    return Math.floor(diff / 86400) + ' 天前';
  }

  function duration(seconds) {
    var s = Number(seconds || 0);
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d) { return d + ' 天 ' + h + ' 小时'; }
    if (h) { return h + ' 小时 ' + m + ' 分'; }
    return Math.max(1, m) + ' 分钟';
  }

  function channels() { return (state.status && state.status.channels) || []; }

  function channelMeta(name) {
    var list = channels();
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].name === name) { return list[i]; }
    }
    return { name: name, label: name, color: '#8a93a3', realm_options: [], total: 0, ready: 0 };
  }

  function channelLetter(name) {
    if (name === 'qoder') { return 'Q'; }
    if (name === 'codebuddy') { return 'W'; }
    if (name === 'zcode') { return 'Z'; }
    if (name === 'gpt') { return 'G'; }
    return String(name || '?').slice(0, 1).toUpperCase();
  }

  function toast(message, kind) {
    var root = $('#toasts');
    var node = document.createElement('div');
    node.className = 'toast ' + (kind || '');
    node.innerHTML = '<span>' + esc(message) + '</span><button class="close" type="button">×</button>';
    node.querySelector('.close').addEventListener('click', function () { node.remove(); });
    root.appendChild(node);
    setTimeout(function () { if (node.parentNode) { node.remove(); } }, kind === 'err' ? 6500 : 3800);
  }

  function copyText(text) {
    if (!text) { return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast('已复制到剪贴板', 'ok');
      }).catch(function () { fallbackCopy(text); });
      return;
    }
    fallbackCopy(text);
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('已复制到剪贴板', 'ok'); }
    catch (e) { toast('复制失败，请手动选择文本', 'err'); }
    ta.remove();
  }

  function errorMessage(data, status) {
    if (data && typeof data.error === 'string') { return data.error; }
    if (data && data.error && typeof data.error === 'object') {
      return data.error.message || JSON.stringify(data.error);
    }
    if (data && data.message) { return data.message; }
    return '请求失败 HTTP ' + status;
  }

  function api(path, options) {
    options = options || {};
    var method = options.method || 'GET';
    var headers = Object.assign({}, options.headers || {});
    var opts = { method: method, headers: headers };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(options.body);
    }
    if (method !== 'GET') { headers['X-Ruodao-Admin'] = '1'; }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = {};
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = { error: text }; }
        }
        if (!res.ok || data.ok === false) {
          throw new Error(errorMessage(data, res.status));
        }
        return data;
      });
    });
  }

  function loadStatus() {
    return api('/api/status').then(function (data) { state.status = data; });
  }

  function loadAccounts() {
    return api('/api/accounts').then(function (data) { state.accounts = data.accounts || []; });
  }

  function loadModels() {
    return api('/api/models').then(function (data) { state.models = data.data || []; });
  }

  function loadKeys() {
    return api('/api/keys').then(function (data) { state.keys = data.keys || []; });
  }

  function loadSettings() {
    return api('/api/settings').then(function (data) { state.settings = data; });
  }

  function loadUsage() {
    var since = Math.floor(Date.now() / 1000 - state.usageRange);
    return api('/api/usage?since=' + since + '&limit=80').then(function (data) { state.usage = data; });
  }

  function loadGuard() {
    return api('/api/guard').then(function (data) { state.guard = data; });
  }

  function loadEvents() {
    return api('/api/events?limit=150').then(function (data) { state.events = data.events || []; });
  }

  function reloadCore() {
    return Promise.all([loadStatus(), loadAccounts(), loadModels(), loadKeys()]).then(function () {
      renderTabs();
      renderStatus();
      renderTabs();
      renderTabContent();
    });
  }

  // ---------------------------------------------------------------- shell
  function renderTabs() {
    var total = state.accounts.length;
    $('#tabAccountCount').textContent = total ? String(total) : '';
    $('#tabKeyCount').textContent = state.keys.length ? String(state.keys.length) : '';
    $$('#tabs .tab').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.tab === state.tab);
    });
    $$('main .view').forEach(function (view) {
      view.classList.toggle('active', view.id === 'view-' + state.tab);
    });
  }

  function renderStatus() {
    var pill = $('#gatewayPill');
    var label = pill.querySelector('span');
    if (state.status) {
      label.textContent = '运行中 · ' + duration(state.status.uptime);
      pill.classList.remove('off');
    } else {
      label.textContent = '未连接';
      pill.classList.add('off');
    }
  }

  function switchTab(name) {
    state.tab = name;
    try { localStorage.setItem('ruodao.tab', name); } catch (e) { /* ignore */ }
    renderTabs();
    renderTabContent();
    if (name === 'usage') {
      loadUsage().then(renderUsage).catch(function (e) { toast(e.message, 'err'); });
    } else if (name === 'keys') {
      loadKeys().then(function () { renderTabs(); renderKeys(); }).catch(function (e) { toast(e.message, 'err'); });
    } else if (name === 'logs') {
      loadEvents().then(renderLogs).catch(function (e) { toast(e.message, 'err'); });
    } else if (name === 'guard') {
      loadGuard().then(renderGuard).catch(function (e) { toast(e.message, 'err'); });
    } else if (name === 'settings') {
      loadSettings().then(renderSettings).catch(function (e) { toast(e.message, 'err'); });
    }
  }

  function renderTabContent() {
    if (state.tab === 'overview') { renderOverview(); }
    else if (state.tab === 'accounts') { renderAccounts(); }
    else if (state.tab === 'models') { renderModels(); }
    else if (state.tab === 'keys') { renderKeys(); }
    else if (state.tab === 'usage') { renderUsage(); }
    else if (state.tab === 'guard') { renderGuard(); }
    else if (state.tab === 'logs') { renderLogs(); }
    else if (state.tab === 'settings') { renderSettings(); }
  }

  function copyCodeButton(target) {
    return '<button class="btn sm" type="button" data-action="copy-code" data-target="' + target + '">复制</button>';
  }

  // -------------------------------------------------------------- overview
  function quickCode(base, model) {
    if (state.quickstart === 'anthropic') {
      return 'curl ' + base + '/v1/messages \\\n' +
        '  -H "x-api-key: $RUDAO_API_KEY" \\\n' +
        '  -H "anthropic-version: 2023-06-01" \\\n' +
        '  -H "content-type: application/json" \\\n' +
        '  -d \'{"model":"' + model + '","max_tokens":1024,' +
        '"messages":[{"role":"user","content":"你好"}]}\'';
    }
    if (state.quickstart === 'responses') {
      return 'curl ' + base + '/v1/responses \\\n' +
        '  -H "Authorization: Bearer $RUDAO_API_KEY" \\\n' +
        '  -H "Content-Type: application/json" \\\n' +
        '  -d \'{"model":"' + model + '","input":"你好"}\'';
    }
    return 'curl ' + base + '/v1/chat/completions \\\n' +
      '  -H "Authorization: Bearer $RUDAO_API_KEY" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -d \'{"model":"' + model + '","stream":true,' +
      '"messages":[{"role":"user","content":"你好"}]}\'';
  }

  function renderOverview() {
    var view = $('#view-overview');
    var status = state.status || {};
    var u = (state.usage && state.usage.summary) || status.usage || {};
    var accs = status.accounts || { total: 0, ready: 0 };
    var base = status.base_url || location.origin;
    var rate = u.requests ? Math.round((1 - (u.errors || 0) / u.requests) * 1000) / 10 : null;
    var catalog = state.models.filter(function (m) { return !m.alias; });
    var usable = catalog.filter(function (m) {
      return state.accounts.some(function (a) { return a.channel === m.channel && a.enabled; });
    });
    var firstModel = (usable[0] || catalog[0] || {}).id || 'gpt/gpt-5.6';
    var list = channels();

    var channelCards = list.map(function (ch) {
      var mine = state.accounts.filter(function (a) { return a.channel === ch.name; });
      var dots = mine.slice(0, 12).map(function (a) {
        return '<i class="acct-dot ' + esc(a.status) + '" title="' + esc(a.name + ' · ' + a.status_label) + '"></i>';
      }).join('');
      if (!mine.length) {
        dots = '<i class="acct-dot"></i><i class="acct-dot"></i><i class="acct-dot"></i><i class="acct-dot"></i>';
      }
      return '<article class="channel-card card">' +
        '<div class="channel-top"><div class="channel-name">' +
        '<span class="channel-logo" style="--logo:' + esc(ch.color) + '">' + esc(channelLetter(ch.name)) + '</span>' +
        esc(ch.label) + '</div>' +
        '<span class="pill ' + (mine.length ? 'green' : 'plain') + '">' +
        (mine.length ? ch.ready + ' 可用' : '未配置') + '</span></div>' +
        '<div class="channel-count" style="margin-top:10px"><b>' + mine.length + '</b> 个账号' +
        '<span class="faint"> · ' + (mine.length ? ch.total - ch.ready + ' 个待处理' : '添加后即可使用') + '</span></div>' +
        '<div class="channel-foot"><div class="channel-dots">' + dots + '</div>' +
        '<div style="display:flex;gap:4px">' +
        '<button class="btn sm soft" type="button" data-action="open-add" data-channel="' + esc(ch.name) + '">添加</button>' +
        '<button class="btn sm ghost" type="button" data-action="goto-accounts" data-filter="' + esc(ch.name) + '">管理</button>' +
        '</div></div></article>';
    }).join('');

    var recent = ((state.usage && state.usage.recent) || []).slice(0, 6);
    var recentRows = recent.map(function (r) {
      return '<tr><td class="nowrap faint">' + esc(fmtTime(r.ts)) + '</td>' +
        '<td><span class="pill' + (r.channel ? '' : ' plain') + '">' +
        esc(r.channel ? (r.channel_label || channelMeta(r.channel).label) : '未路由') + '</span></td>' +
        '<td class="model-id">' + esc(r.model || '—') + '</td>' +
        '<td class="num">' + num(r.total_tokens) + '</td>' +
        '<td><span class="status-code ' + (r.status < 400 ? 'ok' : 'bad') + '">' + esc(r.status) + '</span></td>' +
        '<td class="num faint">' + num(r.latency_ms) + ' ms</td></tr>';
    }).join('');

    view.innerHTML =
      '<section class="hero card">' +
        '<div><span class="eyebrow">LOCAL AI GATEWAY</span>' +
        '<h1>一个入口，接上你的全部额度</h1>' +
        '<p class="lead">把 Qoder、WorkBuddy、ZCode 和任意 OpenAI 兼容服务聚合为本地接口，' +
        '同时提供 OpenAI Chat、Anthropic Messages 与 Responses API。签到、刷新和余额统计全自动。</p>' +
        '<div class="hero-actions">' +
        '<button class="btn primary" type="button" data-action="open-add">添加账号</button>' +
        '<button class="btn" type="button" data-copy="' + esc(base) + '/v1">复制 Base URL</button>' +
        '<button class="btn ghost" type="button" data-action="goto-settings">接入配置</button>' +
        '</div></div>' +
        '<div class="endpoint-panel"><div class="panel-title"><span>接口地址</span>' +
        '<span class="pill green">本机</span></div>' +
        '<div class="endpoint-row"><span class="label">OpenAI</span><span class="value">' + esc(base) + '/v1</span>' +
        '<button class="copy-btn" type="button" data-copy="' + esc(base) + '/v1">复制</button></div>' +
        '<div class="endpoint-row"><span class="label">Anthropic</span><span class="value">' + esc(base) + '</span>' +
        '<button class="copy-btn" type="button" data-copy="' + esc(base) + '">复制</button></div>' +
        '<div class="endpoint-row"><span class="label">Responses</span><span class="value">' + esc(base) + '/v1</span>' +
        '<button class="copy-btn" type="button" data-copy="' + esc(base) + '/v1">复制</button></div>' +
        '</div></section>' +

      '<div class="stat-grid">' +
        statCard('可用账号', accs.ready + '<small>/ ' + accs.total + '</small>', '全渠道账号池') +
        statCard('近 24h 请求', compact(u.requests), '成功 ' + compact((u.requests || 0) - (u.errors || 0)) + ' 次') +
        statCard('Token 消耗', compact(u.total_tokens), '输入 ' + compact(u.prompt_tokens) + ' · 输出 ' + compact(u.completion_tokens)) +
        statCard('成功率', rate === null ? '—' : rate + '<small>%</small>',
          u.errors ? '<span class="trend warn">失败 ' + compact(u.errors) + '</span>' : '<span class="trend">运行稳定</span>') +
      '</div>' +

      '<div class="section-head"><div><h2>渠道</h2><div class="sub">每个渠道独立轮询，失败自动切换账号</div></div>' +
      '<button class="btn sm" type="button" data-action="goto-accounts">全部账号</button></div>' +
      '<div class="channel-grid">' + channelCards + '</div>' +

      '<div class="split">' +
        '<div class="card"><div class="panel-head"><h3>快速调用</h3>' +
        '<div class="seg"><button type="button" data-action="quickstart" data-mode="openai" class="' +
        (state.quickstart === 'openai' ? 'active' : '') + '">OpenAI</button>' +
        '<button type="button" data-action="quickstart" data-mode="anthropic" class="' +
        (state.quickstart === 'anthropic' ? 'active' : '') + '">Anthropic</button>' +
        '<button type="button" data-action="quickstart" data-mode="responses" class="' +
        (state.quickstart === 'responses' ? 'active' : '') + '">Responses</button></div></div>' +
        '<div class="panel-body"><div class="code-tip"><span>在「密钥」页创建 Key，示例通过环境变量传入</span>' +
        copyCodeButton('#quickCode') + '</div>' +
        '<pre class="code" id="quickCode">' + esc(quickCode(base, firstModel)) + '</pre></div></div>' +
        '<div class="card"><div class="panel-head"><h3>最近请求</h3>' +
        '<button class="btn sm ghost" type="button" data-action="goto-usage">用量明细</button></div>' +
        (recentRows ? '<div class="table-wrap"><table><thead><tr><th>时间</th><th>渠道</th><th>模型</th>' +
          '<th>Token</th><th>状态</th><th>耗时</th></tr></thead><tbody>' + recentRows + '</tbody></table></div>'
          : '<div class="empty"><strong>还没有请求记录</strong>添加账号后，在上方快速调用一次试试。</div>') +
        '</div>' +
      '</div>';
  }

  function statCard(label, value, foot) {
    return '<div class="stat card"><div class="label"><span>' + esc(label) + '</span></div>' +
      '<div class="value">' + value + '</div><div class="foot">' + foot + '</div></div>';
  }

  // -------------------------------------------------------------- accounts
  function sourceLabel(acc) {
    if (acc.source_label) { return acc.source_label; }
    var source = String(acc.source || '');
    if (!source) { return '手动导入'; }
    if (source.indexOf('local') === 0) { return '本地客户端'; }
    if (source === 'oauth') { return 'OAuth 登录'; }
    if (source === 'pat') { return 'PAT'; }
    if (source === 'apikey') { return 'API Key'; }
    return '手动导入';
  }

  function creditView(acc) {
    var c = acc.credits || {};
    if (c.unlimited) {
      return { text: '不限量', pct: 100, cls: '', sub: acc.plan || '企业额度' };
    }
    var remain = Number(c.remain);
    var size = Number(c.size);
    if (isFinite(remain) && isFinite(size) && size > 0) {
      var pct = Math.max(0, Math.min(100, Math.round(remain / size * 100)));
      var cls = pct <= 15 ? 'low' : (pct <= 35 ? 'warn' : '');
      var unit = c.unit ? ' ' + c.unit : '';
      return {
        text: num(remain) + ' / ' + num(size),
        pct: pct,
        cls: cls,
        sub: (acc.credits_updated_iso || c.updated_iso || '') + (unit ? unit : '')
      };
    }
    if (isFinite(remain)) {
      return { text: '剩余 ' + num(remain), pct: null, cls: '',
        sub: acc.credits_updated_iso || c.updated_iso || '' };
    }
    var limit = (c.limits || [])[0];
    if (limit && isFinite(Number(limit.percentage))) {
      return { text: '限额 ' + Math.round(Number(limit.percentage)) + '%', pct: null, cls: '', sub: '来自上游配额' };
    }
    return null;
  }

  function statusPill(acc) {
    var cls = 'pill';
    if (acc.status === 'ready') { cls += ' green'; }
    else if (acc.status === 'cooling' || acc.status === 'warn') { cls += ' amber'; }
    else if (acc.status === 'expired') { cls += ' red'; }
    return '<span class="' + cls + '">' + esc(acc.status_label || acc.status) + '</span>';
  }

  function accountRow(acc) {
    var meta = channelMeta(acc.channel);
    var credit = creditView(acc);
    var expanded = !!state.expanded[acc.id];
    var actions = [];
    if (acc.checkin_supported) {
      actions.push('<button class="btn sm" type="button" data-action="acc-action" data-op="checkin" data-id="' +
        esc(acc.id) + '">签到</button>');
    }
    actions.push('<button class="btn sm" type="button" data-action="acc-action" data-op="refresh" data-id="' +
      esc(acc.id) + '">刷新</button>');
    actions.push('<button class="btn sm" type="button" data-action="acc-action" data-op="test" data-id="' +
      esc(acc.id) + '">测试</button>');
    if (acc.recheck_needed) {
      actions.push('<button class="btn sm soft" type="button" data-action="relogin" data-channel="' +
        esc(acc.channel) + '" data-realm="' + esc(acc.realm) + '">重新登录</button>');
    }
    actions.push('<button class="btn sm ghost" type="button" data-action="toggle-account" data-id="' +
      esc(acc.id) + '" data-enabled="' + (acc.enabled ? '0' : '1') + '">' +
      (acc.enabled ? '停用' : '启用') + '</button>');
    actions.push('<button class="btn sm danger" type="button" data-action="delete-account" data-id="' +
      esc(acc.id) + '" data-name="' + esc(acc.name) + '">删除</button>');

    var metaBits = [];
    metaBits.push('<span>' + esc(sourceLabel(acc)) + '</span>');
    if (acc.realm_label) { metaBits.push('<span>' + esc(acc.realm_label) + '</span>'); }
    if (acc.org_name) { metaBits.push('<span>' + esc(acc.org_name) + '</span>'); }
    if (acc.base_url) { metaBits.push('<span class="mono">' + esc(acc.base_url) + '</span>'); }
    if (acc.expires_at) { metaBits.push('<span>到期 ' + esc(fmtTime(acc.expires_at)) + '</span>'); }
    if (acc.uid) { metaBits.push('<span class="mono">' + esc(acc.uid) + '</span>'); }

    return '<div class="account-row' + (acc.enabled ? '' : ' disabled') + '">' +
      '<div class="acc-avatar" style="background:' + esc(meta.color) + '18;color:' + esc(meta.color) + '">' +
      esc(channelLetter(acc.channel)) + '</div>' +
      '<div class="account-main"><div class="account-title"><strong>' + esc(acc.name || acc.nickname || '未命名') + '</strong>' +
      '<span class="pill">' + esc(meta.label) + '</span>' + statusPill(acc) +
      (acc.enabled ? '' : '<span class="pill plain">已停用</span>') + '</div>' +
      '<div class="account-meta">' + metaBits.map(function (x) { return x; }).join('') + '</div></div>' +
      '<div class="credit-box">' + (credit ?
        '<div class="credit-line"><b>' + esc(credit.text) + '</b><span>' +
        (credit.pct === null ? '' : credit.pct + '%') + '</span></div>' +
        (credit.pct === null ? '' : '<div class="bar"><i class="' + esc(credit.cls) + '" style="width:' +
          credit.pct + '%"></i></div>') +
        '<div class="credit-sub">' + esc(credit.sub || '') + '</div>'
        : '<div class="credit-line"><b class="faint" style="font-weight:500">额度未刷新</b></div>' +
          '<div class="credit-sub">点击「刷新」获取余额</div>') + '</div>' +
      '<div class="account-actions">' + actions.join('') +
      '<button class="btn sm ghost" type="button" data-action="toggle-detail" data-id="' + esc(acc.id) + '">' +
      (expanded ? '收起' : '详情') + '</button></div></div>' +
      (expanded ? accountDetail(acc) : '');
  }

  function accountDetail(acc) {
    var data = acc.data || {};
    var fields = [];
    function field(label, value, wide) {
      if (value === undefined || value === null || value === '') { return; }
      fields.push('<div class="field' + (wide ? ' wide' : '') + '"><div class="k">' + esc(label) +
        '</div><div class="v">' + esc(value) + '</div></div>');
    }
    field('账号 ID', acc.id);
    field('来源', sourceLabel(acc));
    field('区域', acc.realm_label);
    field('组织 / 企业', acc.org_name || data.organization_name || data.domain);
    field('计划', acc.plan);
    field('UID', acc.uid);
    field('到期时间', acc.expires_at ? fmtTime(acc.expires_at) : '');
    field('最后签到', acc.last_checkin ? acc.last_checkin + '（' +
      timeAgo(data.last_checkin_at) + '）' : '');
    field('签到结果', acc.last_checkin_message, true);
    field('余额更新', acc.credits_updated_iso || '');
    if (data.source) { field('凭证来源', data.source); }
    if (data.base_url) { field('上游地址', data.base_url, true); }
    var credits = acc.credits || {};
    if (credits.limits && credits.limits.length) {
      var text = credits.limits.map(function (l) {
        return (l.type || '限额') + '：' + Math.round(Number(l.percentage || 0)) + '%' +
          (l.next_reset_at ? '（下次重置 ' + fmtTime(l.next_reset_at) + '）' : '');
      }).join('；');
      field('上游限额', text, true);
    }
    if (credits.packages && credits.packages.length) {
      var pkgs = credits.packages.map(function (p) {
        return (p.name || '额度') + ' ' + num(p.remain) + ' / ' + num(p.size);
      }).join('；');
      field('额度包', pkgs, true);
    }
    field('最近使用', data.last_used_at ? timeAgo(data.last_used_at) : '');
    if (!fields.length) {
      fields.push('<div class="field wide"><div class="v faint">没有更多信息</div></div>');
    }
    return '<div class="account-detail">' + fields.join('') + '</div>';
  }

  function renderAccounts() {
    var view = $('#view-accounts');
    var filters = '<button class="chip ' + (state.accountFilter === 'all' ? 'active' : '') +
      '" type="button" data-action="filter-account" data-filter="all">全部<span class="n">' +
      state.accounts.length + '</span></button>';
    channels().forEach(function (ch) {
      var count = state.accounts.filter(function (a) { return a.channel === ch.name; }).length;
      filters += '<button class="chip ' + (state.accountFilter === ch.name ? 'active' : '') +
        '" type="button" data-action="filter-account" data-filter="' + esc(ch.name) + '">' +
        esc(ch.label) + '<span class="n">' + count + '</span></button>';
    });
    var list = state.accounts.filter(function (a) {
      return state.accountFilter === 'all' || a.channel === state.accountFilter;
    });
    var rows = list.map(accountRow).join('');
    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>账号池</h2>' +
      '<div class="sub">轮询与失败切换已自动处理；Token 仅保存在本机数据库</div></div></div>' +
      '<div class="toolbar"><div class="filters">' + filters + '</div>' +
      '<button class="btn primary" type="button" data-action="open-add">添加账号</button></div>' +
      (rows ? '<div class="account-list">' + rows + '</div>'
        : '<div class="card"><div class="empty"><strong>还没有账号</strong>' +
          '添加 Qoder、WorkBuddy 或 ZCode 账号，即可开始聚合。' +
          '<div style="margin-top:14px"><button class="btn primary" type="button" data-action="open-add">添加第一个账号</button></div>' +
          '</div></div>');
  }

  // ---------------------------------------------------------------- models
  function modelCaps(m) {
    var caps = m.capabilities || {};
    var out = [];
    if (caps.vision) { out.push('视觉'); }
    if (caps.reasoning) { out.push('推理'); }
    if (caps.tools) { out.push('工具'); }
    if (caps.free) { out.push('免费'); }
    return out.map(function (x) { return '<span class="pill plain">' + esc(x) + '</span>'; }).join('');
  }

  function renderModels() {
    var view = $('#view-models');
    var chips = '<button class="chip ' + (state.modelFilter === 'all' ? 'active' : '') +
      '" type="button" data-action="filter-model" data-filter="all">全部</button>';
    channels().forEach(function (ch) {
      chips += '<button class="chip ' + (state.modelFilter === ch.name ? 'active' : '') +
        '" type="button" data-action="filter-model" data-filter="' + esc(ch.name) + '">' +
        esc(ch.label) + '</button>';
    });
    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>模型目录</h2>' +
      '<div class="sub">模型 ID 可直接使用；给密钥绑定渠道后，同名模型也不会走错通道</div></div>' +
      '<span class="pill">' + (state.models.filter(function (m) { return !m.alias; }).length) + ' 个模型</span></div>' +
      '<div class="model-toolbar"><div class="search">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg>' +
      '<input id="modelSearch" placeholder="搜索模型名称或 ID" autocomplete="off" value="' + esc(state.modelQuery) + '">' +
      '</div><div class="filters">' + chips + '</div></div>' +
      '<div id="modelGroups"></div>';
    var input = $('#modelSearch');
    input.addEventListener('input', function () {
      state.modelQuery = input.value;
      renderModelGroups();
    });
    renderModelGroups();
  }

  function renderModelGroups() {
    var root = $('#modelGroups');
    if (!root) { return; }
    var query = state.modelQuery.trim().toLowerCase();
    var list = state.models.filter(function (m) {
      if (m.alias) { return false; }
      if (state.modelFilter !== 'all' && m.channel !== state.modelFilter) { return false; }
      if (!query) { return true; }
      return (String(m.id || '') + ' ' + String(m.raw_id || '') + ' ' + String(m.display_name || ''))
        .toLowerCase().indexOf(query) >= 0;
    });
    if (!list.length) {
      root.innerHTML = '<div class="card"><div class="empty"><strong>没有匹配的模型</strong>' +
        '换个关键词，或先在账号页添加对应渠道。</div></div>';
      return;
    }
    var order = channels().map(function (c) { return c.name; });
    var groups = {};
    list.forEach(function (m) {
      var key = m.channel || 'other';
      if (!groups[key]) { groups[key] = []; }
      groups[key].push(m);
    });
    var html = order.concat(Object.keys(groups).filter(function (k) { return order.indexOf(k) < 0; }))
      .filter(function (key) { return groups[key] && groups[key].length; })
      .map(function (key) {
        var meta = channelMeta(key);
        var items = groups[key];
        var rows = items.map(function (m) {
          var ctx = Number(m.context_length || 0);
          var mid = String(m.raw_id || m.id);
          return '<tr><td><div class="model-id">' + esc(mid) + '</div>' +
            (m.display_name && m.display_name !== mid ?
              '<div class="faint" style="font-size:11px">' + esc(m.display_name) + '</div>' : '') +
            '</td><td>' + (m.realm_label ? '<span class="pill">' + esc(m.realm_label) + '</span>' : '—') + '</td>' +
            '<td class="num faint">' + (ctx ? compact(ctx) : '—') + '</td>' +
            '<td><div class="model-caps">' + modelCaps(m) + '</div></td>' +
            '<td><button class="copy-btn" type="button" data-copy="' + esc(mid) + '">复制</button></td></tr>';
        }).join('');
        return '<section class="model-group card"><div class="model-group-head">' +
          '<div class="name"><span class="channel-logo" style="--logo:' + esc(meta.color) + '">' +
          esc(channelLetter(key)) + '</span>' + esc(meta.label) +
          '<span class="faint" style="font-weight:400;font-size:11.5px">' + items.length + ' 个</span></div>' +
          '<span class="pill plain">' + esc(meta.realm_options && meta.realm_options.length ?
            meta.realm_options.map(function (x) { return x[1]; }).join(' / ') : '自定义') + '</span></div>' +
          '<div class="table-wrap"><table><thead><tr><th>模型 ID</th><th>区域</th><th>上下文</th>' +
          '<th>能力</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
      }).join('');
    root.innerHTML = html;
  }

  // ------------------------------------------------------------------ keys
  function keyById(id) {
    for (var i = 0; i < state.keys.length; i += 1) {
      if (state.keys[i].id === id) { return state.keys[i]; }
    }
    return null;
  }

  function keyChannelName(channel) {
    return channel ? channelMeta(channel).label : '全部渠道';
  }

  function keyChannelPill(channel) {
    return channel
      ? '<span class="pill">' + esc(keyChannelName(channel)) + '</span>'
      : '<span class="pill plain">全部渠道</span>';
  }

  function keyChannelOptions(current) {
    var html = '<option value=""' + (!current ? ' selected' : '') +
      '>全部渠道（同名模型需加前缀）</option>';
    channels().forEach(function (ch) {
      html += '<option value="' + esc(ch.name) + '"' +
        (current === ch.name ? ' selected' : '') + '>' + esc(ch.label) + '</option>';
    });
    return html;
  }

  function renderKeys() {
    var view = $('#view-keys');
    var keys = state.keys || [];
    var active = keys.filter(function (k) { return k.enabled && !k.expired; }).length;
    var req24 = 0;
    var tok24 = 0;
    var totalTok = 0;
    var totalReq = 0;
    var maxTok = 1;
    keys.forEach(function (k) {
      var u = k.usage || {};
      req24 += Number(u.today_requests || 0);
      tok24 += Number(u.today_total_tokens || 0);
      totalTok += Number(u.total_tokens || 0);
      totalReq += Number(u.requests || 0);
      maxTok = Math.max(maxTok, Number(u.total_tokens || 0));
    });

    var rows = keys.map(function (k) {
      var u = k.usage || {};
      var stateCls = k.expired ? 'red' : (k.enabled ? 'green' : '');
      var stateLabel = k.expired ? '已过期' : (k.enabled ? '启用中' : '已停用');
      var width = Math.round(Number(u.total_tokens || 0) / maxTok * 100);
      return '<tr>' +
        '<td><div class="key-name">' + esc(k.name) + '</div>' +
        '<div class="key-sub">创建于 ' + esc(fmtDate(k.created_at)) + '</div></td>' +
        '<td>' + keyChannelPill(k.channel) + '</td>' +
        '<td><code class="key-mask">' + esc(k.masked) + '</code></td>' +
        '<td><button type="button" class="pill ' + stateCls + ' key-state" data-action="toggle-key" ' +
        'data-id="' + esc(k.id) + '" title="点击' + (k.enabled ? '停用' : '启用') + '">' +
        esc(stateLabel) + '</button></td>' +
        '<td><div class="key-usage"><b>' + compact(u.total_tokens) + '</b><span>tokens</span></div>' +
        '<div class="key-sub">' + num(u.requests) + ' 次请求' +
        (u.errors ? ' · <span class="key-error">' + num(u.errors) + ' 失败</span>' : '') + '</div>' +
        '<div class="key-bar"><i style="width:' + width + '%"></i></div></td>' +
        '<td class="num">' + compact(u.today_total_tokens) +
        '<div class="key-sub">' + num(u.today_requests) + ' 次请求</div></td>' +
        '<td class="nowrap">' + (k.expires_at ? esc(fmtDate(k.expires_at)) : '<span class="faint">永不过期</span>') + '</td>' +
        '<td class="nowrap">' + (k.last_used_at ? '<span title="' + esc(fmtTime(k.last_used_at)) + '">' +
          esc(timeAgo(k.last_used_at)) + '</span>' : '<span class="faint">从未使用</span>') + '</td>' +
        '<td><div class="key-actions">' +
        '<button class="btn sm ghost" type="button" data-action="copy-key" data-id="' + esc(k.id) + '">复制</button>' +
        '<button class="btn sm ghost" type="button" data-action="edit-key" data-id="' + esc(k.id) + '">编辑</button>' +
        '<button class="btn sm ghost" type="button" data-action="rotate-key" data-id="' + esc(k.id) + '">重置</button>' +
        '<button class="btn sm danger" type="button" data-action="delete-key" data-id="' + esc(k.id) + '">删除</button>' +
        '</div></td></tr>';
    }).join('');

    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>API 密钥</h2>' +
      '<div class="sub">为不同客户端分配独立密钥，可绑定单一渠道，避免同名模型走错通道</div></div>' +
      '<button class="btn primary" type="button" data-action="new-key">新建密钥</button></div>' +
      '<div class="stat-grid">' +
        statCard('密钥总数', String(keys.length), '当前启用 ' + active + ' 个') +
        statCard('近 24h 请求', compact(req24), '累计 ' + compact(totalReq) + ' 次') +
        statCard('近 24h Token', compact(tok24), '输入 + 输出合计') +
        statCard('累计 Token', compact(totalTok), '所有密钥合计') +
      '</div>' +
      '<div class="card" style="margin-top:16px;overflow:hidden">' +
        '<div class="panel-head"><h3>密钥列表</h3>' +
        '<span class="pill plain">列表仅显示脱敏 Key，完整密钥需在本地复制</span></div>' +
        (rows ? '<div class="table-wrap"><table class="key-table"><thead><tr><th>名称</th><th>渠道</th><th>API Key</th><th>状态</th>' +
          '<th>累计消耗</th><th>近 24 小时</th><th>有效期</th><th>最近使用</th><th>操作</th></tr></thead>' +
          '<tbody>' + rows + '</tbody></table></div>'
          : '<div class="empty"><strong>还没有 API Key</strong>创建密钥后，客户端即可通过它访问本机网关。' +
            '<div style="margin-top:14px"><button class="btn primary" type="button" data-action="new-key">创建第一个密钥</button></div></div>') +
      '</div>' +
      '<div class="hint" style="margin-top:10px">删除或重置密钥会立即影响使用该 Key 的客户端；系统至少保留一个启用密钥。</div>';
  }

  function showSecretModal(title, name, secret, subtitle) {
    openModal({
      title: title,
      subtitle: subtitle || '',
      size: 'sm',
      body: '<div class="field"><label>' + esc(name || 'API Key') + '</label>' +
        '<div class="key-box"><code id="keySecret">' + esc(secret) + '</code>' +
        '<button class="btn sm" type="button" data-copy="' + esc(secret) + '">复制</button></div>' +
        '<div class="hint">用于 <code>Authorization: Bearer</code> 或 <code>x-api-key</code>。' +
        '完整密钥仅在本机管理界面显示。</div></div>',
      footer: '<button class="btn primary" type="button" data-close>完成</button>'
    });
  }

  function openKeyCreate() {
    openModal({
      title: '新建 API Key',
      subtitle: '为应用、客户端或成员分配独立密钥',
      size: 'sm',
      body: '<div class="field"><label>名称</label>' +
        '<input class="input" id="keyName" maxlength="60" placeholder="例如：Claude Code"></div>' +
        '<div class="field"><label>绑定渠道</label>' +
        '<select class="input" id="keyChannel">' + keyChannelOptions('') + '</select>' +
        '<div class="hint">绑定后该密钥只能调用此渠道的模型，客户端可直接使用裸模型 ID；' +
        '选择全部渠道时，同名模型仍需加渠道前缀。</div></div>' +
        '<div class="field"><label>有效期</label>' +
        '<input class="input" id="keyExpiry" type="date">' +
        '<div class="hint">留空表示永不过期；到期后该密钥立即失效。</div></div>',
      footer: '<button class="btn" type="button" data-close>取消</button>' +
        '<button class="btn primary" type="button" data-action="create-key">创建密钥</button>'
    });
    var input = $('#keyName');
    if (input) { input.focus(); }
  }

  function createKey(btn) {
    var name = ($('#keyName') && $('#keyName').value || '').trim();
    var expiry = dateInputTs($('#keyExpiry') && $('#keyExpiry').value);
    var channel = ($('#keyChannel') && $('#keyChannel').value) || '';
    btn.disabled = true;
    api('/api/keys', {
      method: 'POST', body: { name: name, expires_at: expiry, channel: channel }
    }).then(function (res) {
      return loadKeys().then(function () {
        renderTabs();
        renderKeys();
        showSecretModal('密钥已创建', res.key.name, res.secret,
          '渠道：' + keyChannelName(res.key.channel) + ' · 请复制并保存到客户端配置中');
        toast('API Key 已创建', 'ok');
      });
    }).catch(function (e) {
      toast(e.message, 'err');
      btn.disabled = false;
    });
  }

  function openKeyEdit(id) {
    var key = keyById(id);
    if (!key) { return; }
    openModal({
      title: '编辑密钥',
      subtitle: key.masked,
      size: 'sm',
      body: '<div class="field"><label>名称</label>' +
        '<input class="input" id="keyName" maxlength="60" value="' + esc(key.name) + '"></div>' +
        '<div class="field"><label>绑定渠道</label>' +
        '<select class="input" id="keyChannel">' + keyChannelOptions(key.channel) + '</select>' +
        '<div class="hint">绑定后只能调用此渠道的模型，客户端可直接使用裸模型 ID。</div></div>' +
        '<div class="field"><label>有效期</label>' +
        '<input class="input" id="keyExpiry" type="date" value="' + esc(dateInputValue(key.expires_at)) + '">' +
        '<div class="hint">留空表示永不过期。</div></div>' +
        '<div class="field"><label>状态</label>' +
        '<select class="input" id="keyEnabled"><option value="1"' + (key.enabled ? ' selected' : '') + '>启用</option>' +
        '<option value="0"' + (!key.enabled ? ' selected' : '') + '>停用</option></select></div>',
      footer: '<button class="btn" type="button" data-close>取消</button>' +
        '<button class="btn primary" type="button" data-action="save-key" data-id="' + esc(key.id) + '">保存</button>'
    });
  }

  function saveKeyEdit(btn, id) {
    var name = ($('#keyName') && $('#keyName').value || '').trim();
    if (!name) { toast('密钥名称不能为空', 'err'); return; }
    var body = {
      name: name,
      channel: ($('#keyChannel') && $('#keyChannel').value) || '',
      expires_at: dateInputTs($('#keyExpiry') && $('#keyExpiry').value),
      enabled: ($('#keyEnabled') && $('#keyEnabled').value) === '1'
    };
    btn.disabled = true;
    api('/api/keys/' + encodeURIComponent(id), { method: 'PATCH', body: body }).then(function () {
      closeModal(true);
      return loadKeys().then(function () {
        renderTabs();
        renderKeys();
        toast('密钥已更新', 'ok');
      });
    }).catch(function (e) {
      toast(e.message, 'err');
      btn.disabled = false;
    });
  }

  function toggleKey(el) {
    var key = keyById(el.dataset.id);
    if (!key) { return; }
    el.disabled = true;
    api('/api/keys/' + encodeURIComponent(key.id), {
      method: 'PATCH', body: { enabled: !key.enabled }
    }).then(function () {
      return loadKeys().then(function () {
        renderTabs();
        renderKeys();
        toast(key.enabled ? '密钥已停用' : '密钥已启用', 'ok');
      });
    }).catch(function (e) {
      el.disabled = false;
      toast(e.message, 'err');
    });
  }

  function copyKeySecret(id) {
    api('/api/keys/' + encodeURIComponent(id) + '/reveal', { method: 'POST', body: {} })
      .then(function (res) { copyText(res.key); })
      .catch(function (e) { toast(e.message, 'err'); });
  }

  function rotateKey(el) {
    var key = keyById(el.dataset.id);
    if (!key) { return; }
    confirmDialog('重置密钥', '重置后旧 Key 立即失效，使用该 Key 的客户端需要更新。确定继续？', '重置 Key')
      .then(function (ok) {
        if (!ok) { return; }
        api('/api/keys/' + encodeURIComponent(key.id) + '/rotate', { method: 'POST', body: {} })
          .then(function (res) {
            return loadKeys().then(function () {
              renderTabs();
              renderKeys();
              showSecretModal('密钥已重置', res.key.name, res.secret, '旧 Key 已失效');
            });
          })
          .catch(function (e) { toast(e.message, 'err'); });
      });
  }

  function deleteKey(el) {
    var key = keyById(el.dataset.id);
    if (!key) { return; }
    confirmDialog('删除密钥', '确定删除「' + key.name + '」？使用该 Key 的客户端将立即无法访问。', '删除')
      .then(function (ok) {
        if (!ok) { return; }
        api('/api/keys/' + encodeURIComponent(key.id), { method: 'DELETE' })
          .then(function () {
            return loadKeys().then(function () {
              renderTabs();
              renderKeys();
              toast('密钥已删除', 'ok');
            });
          })
          .catch(function (e) { toast(e.message, 'err'); });
      });
  }

  // ----------------------------------------------------------------- usage
  function renderUsage() {
    var view = $('#view-usage');
    var data = state.usage || { summary: {}, recent: [] };
    var s = data.summary || {};
    var recent = data.recent || [];
    var by = s.by_channel || {};
    var max = 1;
    Object.keys(by).forEach(function (k) { max = Math.max(max, Number(by[k].total_tokens || 0)); });
    var bars = Object.keys(by).map(function (key) {
      var item = by[key] || {};
      var meta = channelMeta(key);
      return '<div class="channel-bar-row"><div class="meta">' +
        '<span class="name">' + esc(key ? meta.label : '未路由') + '</span>' +
        '<span class="val">' + compact(item.total_tokens) + ' · ' + compact(item.requests) + ' 次</span>' +
        '</div><div class="bar"><i style="width:' +
        Math.round(Number(item.total_tokens || 0) / max * 100) + '%"></i></div></div>';
    }).join('');
    var rows = recent.map(function (r) {
      return '<tr><td class="nowrap faint">' + esc(fmtTime(r.ts)) + '</td>' +
        '<td><span class="pill' + (r.channel ? '' : ' plain') + '">' +
        esc(r.channel ? channelMeta(r.channel).label : '未路由') + '</span></td>' +
        '<td>' + esc(r.account_name || '—') + '</td>' +
        '<td class="model-id">' + esc(r.model || '—') + '</td>' +
        '<td class="num">' + num(r.prompt_tokens) + '</td>' +
        '<td class="num">' + num(r.completion_tokens) + '</td>' +
        '<td class="num">' + num(r.total_tokens) + '</td>' +
        '<td><span class="status-code ' + (r.status < 400 ? 'ok' : 'bad') + '">' + esc(r.status) + '</span>' +
        (r.error ? '<div class="faint" style="font-size:10px;max-width:220px;overflow:hidden;' +
          'text-overflow:ellipsis;white-space:nowrap" title="' + esc(r.error) + '">' + esc(r.error) + '</div>' : '') +
        '</td><td class="num faint">' + num(r.latency_ms) + ' ms</td></tr>';
    }).join('');
    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>用量统计</h2>' +
      '<div class="sub">按请求记录聚合，数据保存在本机 SQLite</div></div>' +
      '<div class="seg"><button type="button" data-action="usage-range" data-range="86400" class="' +
      (state.usageRange === 86400 ? 'active' : '') + '">24 小时</button>' +
      '<button type="button" data-action="usage-range" data-range="604800" class="' +
      (state.usageRange === 604800 ? 'active' : '') + '">7 天</button>' +
      '<button type="button" data-action="usage-range" data-range="2592000" class="' +
      (state.usageRange === 2592000 ? 'active' : '') + '">30 天</button></div></div>' +
      '<div class="usage-summary">' +
      statCard('请求数', compact(s.requests), '失败 ' + compact(s.errors) + ' 次') +
      statCard('输入 Token', compact(s.prompt_tokens), '提示词') +
      statCard('输出 Token', compact(s.completion_tokens), '生成') +
      statCard('总 Token', compact(s.total_tokens), '按请求合计') +
      '</div>' +
      '<div class="split usage-split" style="margin-top:0">' +
      '<div class="card"><div class="panel-head"><h3>渠道分布</h3><span class="pill plain">Token</span></div>' +
      '<div class="panel-body">' + (bars ? '<div class="channel-bars">' + bars + '</div>' :
        '<div class="empty" style="padding:24px">暂无数据</div>') + '</div></div>' +
      '<div class="card"><div class="panel-head"><h3>请求明细</h3>' +
      '<button class="btn sm ghost" type="button" data-action="refresh-usage">刷新</button></div>' +
      (rows ? '<div class="table-wrap"><table><thead><tr><th>时间</th><th>渠道</th><th>账号</th><th>模型</th>' +
        '<th>输入</th><th>输出</th><th>总 Token</th><th>状态</th><th>耗时</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>'
        : '<div class="empty"><strong>所选时段没有请求</strong>调用一次 /v1/chat/completions 后会出现在这里。</div>') +
      '</div></div>';
  }

  // ----------------------------------------------------------------- guard
  function cooldownText(seconds) {
    var s = Math.max(0, Number(seconds || 0));
    if (!s) { return '—'; }
    if (s < 60) { return s + ' 秒'; }
    return Math.ceil(s / 60) + ' 分 ' + (s % 60) + ' 秒';
  }

  function guardBar(used, limit, label) {
    var total = Number(limit || 0);
    var value = Number(used || 0);
    if (!total) { return '<span class="faint">' + esc(label || String(value)) + '</span>'; }
    var pct = Math.max(0, Math.min(100, Math.round(value / total * 100)));
    var cls = pct >= 90 ? 'low' : (pct >= 70 ? 'warn' : '');
    return '<div style="min-width:96px"><div class="faint" style="font-size:11.5px">' +
      value + ' / ' + total + '</div><div class="bar"><i class="' + cls + '" style="width:' +
      pct + '%"></i></div></div>';
  }

  function renderGuard() {
    var view = $('#view-guard');
    var g = state.guard;
    if (!g) {
      view.innerHTML = '<div class="card"><div class="empty"><strong>正在读取防护状态</strong>' +
        '稍候即可看到限速与排队情况。</div></div>';
      return;
    }
    var s = g.settings || {};
    var stats = g.stats || {};
    var global = g.global || {};
    var list = g.accounts || [];
    var enabled = !!g.enabled;
    var dailyLimit = Number(s.daily_limit || 0);

    var rows = list.map(function (acc) {
      var meta = channelMeta(acc.channel);
      var dailyCell;
      if (dailyLimit > 0) {
        dailyCell = guardBar(acc.daily_used, dailyLimit);
      } else {
        dailyCell = '<div><b>' + num(acc.daily_used) + '</b> <span class="faint" style="font-size:11px">次 · 不限</span></div>';
      }
      var cooldown = Number(acc.cooldown || 0);
      return '<tr>' +
        '<td><div class="acc-inline"><span class="channel-logo" style="--logo:' + esc(meta.color) + '">' +
        esc(channelLetter(acc.channel)) + '</span><div><div>' + esc(acc.name || acc.id || '未命名') +
        '</div><div class="faint" style="font-size:11px">' + esc(meta.label) +
        (acc.enabled ? '' : ' · 已停用') + '</div></div></div></td>' +
        '<td>' + statusPill(acc) + '</td>' +
        '<td>' + dailyCell + '</td>' +
        '<td>' + guardBar(acc.rpm_used, s.account_rpm) + '</td>' +
        '<td>' + guardBar(acc.inflight, s.max_concurrency) + '</td>' +
        '<td class="num faint">' + (acc.wait_ms ? num(acc.wait_ms) + ' ms' : '—') + '</td>' +
        '<td>' + (cooldown > 0 ?
          '<span class="pill amber">' + esc(cooldownText(cooldown)) + '</span>' :
          '<span class="faint">—</span>') + '</td>' +
        '</tr>';
    }).join('');

    function inputRow(id, label, desc, value, min, max, unit) {
      return '<div class="setting-row"><div><div class="label">' + esc(label) + '</div>' +
        '<div class="desc">' + esc(desc) + '</div></div><div class="control">' +
        '<input class="input narrow" id="' + id + '" type="number" min="' + min + '"' +
        (max ? ' max="' + max + '"' : '') + ' step="1" value="' + esc(value) + '">' +
        '<span class="faint">' + esc(unit) + '</span></div></div>';
    }

    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>防护策略</h2>' +
      '<div class="sub">本机主动限速与排队，降低触发上游风控的概率；不会伪造请求行为</div></div>' +
      '<div style="display:flex;gap:8px;align-items:center">' +
      '<span class="pill ' + (enabled ? 'green' : 'plain') + '">' +
      (enabled ? '防护已开启' : '防护已关闭') + '</span>' +
      '<button class="btn sm" type="button" data-action="reset-guard">重置统计</button></div></div>' +

      '<div class="usage-summary">' +
      statCard('已放行', compact(stats.allowed), '防护启动以来') +
      statCard('排队等待', compact(stats.queued), '平均 ' + num(stats.avg_wait_ms) + ' ms') +
      statCard('已拦截', compact(stats.rejected), stats.last_reject_reason || '暂无拦截') +
      statCard('当前并发', num(global.inflight) + '<small>/ ' + num(s.global_concurrency) + '</small>',
        '每分钟 ' + num(global.rpm_used) + ' / ' + num(s.global_rpm)) +
      '</div>' +

      '<div class="settings-grid">' +
        '<div class="card"><div class="panel-head"><h3>限速与并发</h3>' +
        '<button class="btn sm primary" type="button" data-action="save-guard">保存防护设置</button></div>' +
        '<div class="setting-row"><div><div class="label">启用防护层</div>' +
        '<div class="desc">关闭后不做本机排队和限速，直接透传上游</div></div>' +
        '<div class="control">' + toggleHtml('guard_enabled', enabled) + '</div></div>' +
        inputRow('guardGlobalRpm', '全局限速', '所有账号合计每分钟最多放行的请求数', s.global_rpm, 1, 6000, '次/分') +
        inputRow('guardAccountRpm', '单账号限速', '每个账号每分钟最多放行的请求数', s.account_rpm, 1, 600, '次/分') +
        inputRow('guardDailyLimit', '单账号每日上限', '0 表示不限制；达到上限后当日直接返回 429', s.daily_limit, 0, '', '次/日') +
        inputRow('guardMaxConcurrency', '单账号并发', '同一账号同时进行中的请求数', s.max_concurrency, 1, 32, '个') +
        inputRow('guardGlobalConcurrency', '全局并发', '所有账号合计同时进行中的请求数', s.global_concurrency, 1, 128, '个') +
        '</div>' +
        '<div class="card"><div class="panel-head"><h3>排队与冷却</h3>' +
        '<span class="pill plain">建议保留默认值</span></div>' +
        inputRow('guardMinIntervalMs', '最小请求间隔', '同一账号两次调用之间的最小间隔，附带随机抖动', s.min_interval_ms, 0, 60000, '毫秒') +
        inputRow('guardJitterMs', '随机抖动', '在最小间隔上增加 0 ~ N 毫秒的随机量', s.jitter_ms, 0, 10000, '毫秒') +
        inputRow('guardQueueTimeout', '排队超时', '等待安全槽位的最长时间，超时返回 429', s.queue_timeout, 1, 300, '秒') +
        inputRow('guardCooldown429', '429 冷却', '上游限流后的账号冷却基数，连续触发会翻倍', s.cooldown_429, 5, 1800, '秒') +
        inputRow('guardCooldown5xx', '5xx 冷却', '上游服务异常后的账号冷却时间', s.cooldown_5xx, 1, 600, '秒') +
        '<div class="panel-body" style="padding-top:0"><div class="hint">' +
        '防护参数即时生效，无需重启。若本地 agent 并发较高，可适度调大并发与限速；' +
        '频繁触发 429 时建议保留较长冷却并保持稳定出口 IP。</div></div>' +
        '</div>' +
      '</div>' +

      '<div class="card" style="margin-top:16px"><div class="panel-head"><h3>账号实时状态</h3>' +
      '<button class="btn sm ghost" type="button" data-action="refresh-guard">刷新</button></div>' +
      (rows ? '<div class="table-wrap"><table><thead><tr><th>账号</th><th>状态</th><th>今日请求</th>' +
        '<th>近 1 分钟</th><th>并发</th><th>排队等待</th><th>冷却</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>'
        : '<div class="empty"><strong>还没有账号</strong>添加账号后，这里会显示每个账号的限速与冷却状态。</div>') +
      '</div>';
  }

  function saveGuard(btn) {
    var body = {
      guard_enabled: $('[data-toggle="guard_enabled"]').classList.contains('on'),
      guard_global_rpm: Number($('#guardGlobalRpm').value || 120),
      guard_account_rpm: Number($('#guardAccountRpm').value || 30),
      guard_daily_limit: Number($('#guardDailyLimit').value || 0),
      guard_max_concurrency: Number($('#guardMaxConcurrency').value || 2),
      guard_global_concurrency: Number($('#guardGlobalConcurrency').value || 4),
      guard_min_interval_ms: Number($('#guardMinIntervalMs').value || 0),
      guard_jitter_ms: Number($('#guardJitterMs').value || 0),
      guard_queue_timeout: Number($('#guardQueueTimeout').value || 30),
      guard_cooldown_429: Number($('#guardCooldown429').value || 180),
      guard_cooldown_5xx: Number($('#guardCooldown5xx').value || 30)
    };
    btn.disabled = true;
    api('/api/guard', { method: 'PUT', body: body }).then(function (res) {
      state.guard = res.guard;
      toast('防护设置已保存', 'ok');
      renderGuard();
    }).catch(function (e) {
      toast(e.message, 'err');
    }).then(function () { btn.disabled = false; });
  }

  function resetGuard(btn) {
    confirmDialog('重置防护统计', '只清空本机的放行、排队与拦截计数，不会修改账号或上游状态。', '重置统计')
      .then(function (ok) {
        if (!ok) { return; }
        btn.disabled = true;
        return api('/api/guard/reset', { method: 'POST', body: {} }).then(function (res) {
          state.guard = res;
          toast('防护统计已重置', 'ok');
          renderGuard();
        }).catch(function (e) { toast(e.message, 'err'); btn.disabled = false; });
      });
  }

  // ------------------------------------------------------------------ logs
  function renderLogs() {
    var view = $('#view-logs');
    var levels = ['all', 'INFO', 'WARN', 'ERROR'];
    var names = { all: '全部', INFO: '信息', WARN: '警告', ERROR: '错误' };
    var filters = levels.map(function (level) {
      return '<button class="chip ' + (state.logFilter === level ? 'active' : '') +
        '" type="button" data-action="log-filter" data-level="' + level + '">' + names[level] + '</button>';
    }).join('');
    var list = state.events.filter(function (e) {
      return state.logFilter === 'all' || e.level === state.logFilter;
    });
    var rows = list.map(function (e) {
      var cls = e.level === 'ERROR' ? 'red' : (e.level === 'WARN' ? 'amber' : 'blue');
      return '<div class="log-row"><span class="log-time">' + esc(fmtTime(e.ts)) + '</span>' +
        '<span class="log-level"><span class="pill ' + cls + '">' + esc(e.level) + '</span></span>' +
        '<span class="log-msg">' + esc(e.message) + '</span></div>';
    }).join('');
    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>运行日志</h2>' +
      '<div class="sub">签到、刷新、上游错误与请求记录</div></div>' +
      '<div class="toolbar" style="margin:0"><div class="filters">' + filters + '</div>' +
      '<button class="btn sm" type="button" data-action="refresh-logs">刷新</button></div></div>' +
      (rows ? '<div class="log-list">' + rows + '</div>'
        : '<div class="card"><div class="empty"><strong>暂无日志</strong>操作后会实时记录。</div></div>');
  }

  // -------------------------------------------------------------- settings
  function toggleHtml(key, on) {
    return '<button type="button" class="toggle ' + (on ? 'on' : '') + '" data-toggle="' + key +
      '" aria-pressed="' + (on ? 'true' : 'false') + '"></button>';
  }

  function renderSettings() {
    var view = $('#view-settings');
    var s = state.settings || {};
    var base = s.base_url || (state.status && state.status.base_url) || location.origin;
    var keyText = s.api_key_masked || '未初始化';
    var times = (s.checkin_times || []).join(', ');
    var claudeCode = 'set ANTHROPIC_BASE_URL=' + base + '\n' +
      'set ANTHROPIC_AUTH_TOKEN=粘贴上方的 API Key\n' +
      'claude';
    var openaiCode = 'set OPENAI_BASE_URL=' + base + '/v1\n' +
      'set OPENAI_API_KEY=粘贴上方的 API Key';
    view.innerHTML =
      '<div class="section-head" style="margin-top:0"><div><h2>设置</h2>' +
      '<div class="sub">接入信息、自动化任务与本地运行参数</div></div></div>' +
      '<div class="settings-grid">' +
        '<div class="card"><div class="panel-head"><h3>API 接入</h3>' +
        '<span class="pill green">仅本机回环</span></div><div class="panel-body">' +
        '<div class="field"><label>默认 API Key</label><div class="key-box"><code>' + esc(keyText) + '</code>' +
        '<button class="btn sm" type="button" data-action="goto-keys">管理密钥</button>' +
        '<button class="btn sm" type="button" data-action="copy-api-key">复制</button></div>' +
        '<div class="hint">所有 /v1 接口都需要 API Key；多密钥、有效期与独立用量在「密钥」页管理。</div></div>' +
        '<div class="field"><label>Base URL</label><div class="key-box"><code>' + esc(base) + '</code>' +
        '<button class="btn sm" type="button" data-copy="' + esc(base) + '">复制</button></div></div>' +
        '<div class="kv"><span class="k">OpenAI Chat</span><span class="v mono">' + esc(base) + '/v1/chat/completions</span>' +
        '<span class="k">Anthropic</span><span class="v mono">' + esc(base) + '/v1/messages</span>' +
        '<span class="k">Responses</span><span class="v mono">' + esc(base) + '/v1/responses</span>' +
        '<span class="k">模型列表</span><span class="v mono">' + esc(base) + '/v1/models</span></div>' +
        '</div></div>' +

        '<div class="card"><div class="panel-head"><h3>自动化任务</h3>' +
        '<button class="btn sm primary" type="button" data-action="save-settings">保存设置</button></div>' +
        '<div class="setting-row"><div><div class="label">自动签到</div>' +
        '<div class="desc">每天在指定时间领取可用奖励，成功或已签到都会记录</div></div>' +
        '<div class="control">' + toggleHtml('checkin_enabled', !!s.checkin_enabled) + '</div></div>' +
        '<div class="setting-row"><div><div class="label">后台定时任务</div>' +
        '<div class="desc">凭证刷新、余额同步与自动签到</div></div>' +
        '<div class="control">' + toggleHtml('scheduler_enabled', !!s.scheduler_enabled) + '</div></div>' +
        '<div class="setting-row"><div><div class="label">签到时间</div>' +
        '<div class="desc">多个时间用英文逗号分隔，24 小时制</div></div>' +
        '<div class="control"><input class="input wide" id="setCheckinTimes" value="' + esc(times) + '" placeholder="10:05, 21:05"></div></div>' +
        '<div class="setting-row"><div><div class="label">每日凭证刷新</div>' +
        '<div class="desc">按 HH:MM 执行一次强制刷新</div></div>' +
        '<div class="control"><input class="input narrow" id="setRefreshTime" value="' + esc(s.refresh_time || '22:00') + '"></div></div>' +
        '<div class="setting-row"><div><div class="label">余额刷新间隔</div>' +
        '<div class="desc">分钟，最小 5 分钟</div></div>' +
        '<div class="control"><input class="input narrow" id="setCreditsInterval" type="number" min="5" step="5" value="' +
        esc(s.credits_refresh_minutes || 30) + '"><span class="faint">分钟</span></div></div>' +
        '</div></div>' +

        '<div class="card"><div class="panel-head"><h3>运行参数</h3>' +
        '<span class="pill">' + esc((state.status && state.status.version) || '0.1.0') + '</span></div>' +
        '<div class="panel-body"><div class="kv">' +
        '<span class="k">监听地址</span><span class="v mono">' + esc(s.host || '127.0.0.1') + ':' + esc(s.port || 8388) + '</span>' +
        '<span class="k">运行时长</span><span class="v">' + esc(state.status ? duration(state.status.uptime) : '—') + '</span>' +
        '<span class="k">数据目录</span><span class="v mono">' + esc(s.data_dir || '') + '</span>' +
        '<span class="k">账号</span><span class="v">' + esc(state.status ? (state.status.accounts.ready + ' 可用 / ' +
          state.status.accounts.total + ' 总计') : '—') + '</span></div>' +
        '<div class="setting-row" style="margin-top:12px;padding-left:0;padding-right:0">' +
        '<div><div class="label">请求超时</div><div class="desc">秒，长上下文回答可适当调大</div></div>' +
        '<div class="control"><input class="input narrow" id="setTimeout" type="number" min="30" step="30" value="' +
        esc(s.request_timeout || 600) + '"></div></div>' +
        '<div class="setting-row" style="padding-left:0;padding-right:0">' +
        '<div><div class="label">最大请求体</div><div class="desc">MB，超过会直接拒绝</div></div>' +
        '<div class="control"><input class="input narrow" id="setMaxBody" type="number" min="1" step="1" value="' +
        esc(s.max_body_mb || 32) + '"></div></div>' +
        '<button class="btn primary block" type="button" data-action="save-settings" style="margin-top:14px">保存运行参数</button>' +
        '</div></div>' +

        '<div class="card"><div class="panel-head"><h3>客户端接入</h3>' +
        '<span class="pill plain">环境变量</span></div><div class="panel-body">' +
        '<div class="code-tip"><span>Claude Code / Anthropic 客户端</span>' + copyCodeButton('#claudeEnv') + '</div>' +
        '<pre class="code" id="claudeEnv">' + esc(claudeCode) + '</pre>' +
        '<div class="code-tip" style="margin-top:14px"><span>OpenAI 兼容客户端</span>' + copyCodeButton('#openaiEnv') + '</div>' +
        '<pre class="code" id="openaiEnv">' + esc(openaiCode) + '</pre>' +
        '<div class="hint" style="margin-top:10px">需要模型列表时请求 <code>' + esc(base) + '/v1/models</code>，' +
        '给密钥绑定渠道后客户端直接使用裸模型 ID（如 <code>deepseek-v4.1-flash</code>）；' +
        '未绑定渠道的密钥可用 <code>qoder/cn/</code>、<code>codebuddy/</code>、<code>zcode/</code>、<code>gpt/</code> 前缀区分同名模型。</div>' +
        '</div></div>' +
      '</div>';
  }

  function saveSettings(btn) {
    var times = ($('#setCheckinTimes') && $('#setCheckinTimes').value || '')
      .split(/[,，\s]+/).filter(Boolean);
    for (var i = 0; i < times.length; i += 1) {
      if (!/^\d{1,2}:\d{2}$/.test(times[i])) {
        toast('签到时间格式应为 HH:MM，例如 10:05', 'err');
        return;
      }
    }
    var refresh = ($('#setRefreshTime') && $('#setRefreshTime').value || '').trim();
    if (refresh && !/^\d{1,2}:\d{2}$/.test(refresh)) {
      toast('每日刷新时间格式应为 HH:MM', 'err');
      return;
    }
    var body = {
      checkin_enabled: $('[data-toggle="checkin_enabled"]').classList.contains('on'),
      scheduler_enabled: $('[data-toggle="scheduler_enabled"]').classList.contains('on'),
      checkin_times: times,
      refresh_time: refresh || '22:00',
      credits_refresh_minutes: Number($('#setCreditsInterval').value || 30),
      request_timeout: Number($('#setTimeout').value || 600),
      max_body_mb: Number($('#setMaxBody').value || 32)
    };
    btn.disabled = true;
    api('/api/settings', { method: 'PUT', body: body }).then(function (res) {
      state.settings = res.settings;
      toast('设置已保存', 'ok');
      return loadStatus().then(function () { renderStatus(); renderSettings(); });
    }).catch(function (e) {
      toast(e.message, 'err');
    }).then(function () { btn.disabled = false; });
  }

  // --------------------------------------------------------------- modals
  function openModal(opts) {
    var root = $('#modalRoot');
    root.innerHTML =
      '<div class="modal-backdrop" data-backdrop><div class="modal ' + (opts.size || '') + '">' +
      '<div class="modal-head"><div><h3>' + esc(opts.title || '') + '</h3>' +
      (opts.subtitle ? '<div class="sub">' + esc(opts.subtitle) + '</div>' : '') + '</div>' +
      '<button class="modal-close" type="button" data-close>×</button></div>' +
      '<div class="modal-body">' + (opts.body || '') + '</div>' +
      (opts.footer ? '<div class="modal-foot">' + opts.footer + '</div>' : '') +
      '</div></div>';
    return root.firstElementChild;
  }

  function closeModal(force) {
    if (!force && state.login) { cancelLogin(); }
    state.login = null;
    state.add = null;
    state.scan = null;
    $('#modalRoot').innerHTML = '';
  }

  function cancelLogin() {
    var login = state.login;
    if (!login) { return; }
    if (login.timer) { clearInterval(login.timer); }
    if (!login.done && login.state) {
      api('/api/login/' + login.channel + '/cancel', {
        method: 'POST', body: { state: login.state }
      }).catch(function () { /* ignore */ });
    }
    state.login = null;
  }

  function confirmDialog(title, message, confirmLabel) {
    return new Promise(function (resolve) {
      openModal({
        title: title,
        subtitle: '',
        size: 'sm',
        body: '<p class="muted" style="line-height:1.7">' + esc(message) + '</p>',
        footer: '<button class="btn" type="button" data-confirm="0">取消</button>' +
          '<button class="btn danger" type="button" data-confirm="1">' + esc(confirmLabel || '确认') + '</button>'
      });
      $$('#modalRoot [data-confirm]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var ok = btn.dataset.confirm === '1';
          $('#modalRoot').innerHTML = '';
          resolve(ok);
        });
      });
    });
  }

  function openChannelPicker() {
    state.add = null;
    var options = channels().map(function (ch) {
      var count = state.accounts.filter(function (a) { return a.channel === ch.name; }).length;
      var desc = ch.name === 'gpt' ? 'Base URL + API Key' :
        (ch.name === 'zcode' ? 'OAuth / API Key / 本地凭证' :
          (ch.name === 'codebuddy' ? '扫码登录 / Token / 本地客户端' : 'OAuth / PAT / 本地客户端'));
      return '<button class="channel-option" type="button" data-action="pick-channel" data-channel="' +
        esc(ch.name) + '"><span class="channel-logo" style="--logo:' + esc(ch.color) + '">' +
        esc(channelLetter(ch.name)) + '</span><span><span class="t">' + esc(ch.label) + '</span>' +
        '<span class="d">' + esc(desc) + ' · 已有 ' + count + ' 个账号</span></span></button>';
    }).join('');
    openModal({
      title: '添加账号',
      subtitle: '选择要接入的渠道，凭证只会保存在本机',
      body: '<div class="channel-picker">' + options + '</div>',
      footer: '<button class="btn" type="button" data-close>取消</button>'
    });
  }

  function realmOptionsHtml(channel, value) {
    var meta = channelMeta(channel);
    var options = meta.realm_options || [];
    if (!options.length) { return ''; }
    var selected = value || options[0][0];
    var chips = options.map(function (opt) {
      return '<button class="chip ' + (opt[0] === selected ? 'active' : '') +
        '" type="button" data-realm-opt="' + esc(opt[0]) + '">' + esc(opt[1]) + '</button>';
    }).join('');
    return '<div class="field"><label>区域 / 版本</label><div class="filters">' + chips + '</div>' +
      '<input type="hidden" id="addRealm" value="' + esc(selected) + '"></div>';
  }

  function manualFields(channel) {
    if (channel === 'qoder') {
      return '<div class="note info">支持 Qoder 的 Access Token，或 <code>pt-</code> 开头的个人访问令牌（PAT）。' +
        'Token 仅写入本机数据库，不会展示在页面或日志中。</div>' +
        '<div class="field"><label>Access Token / PAT</label>' +
        '<input class="input" id="mToken" type="password" autocomplete="off" placeholder="粘贴 Qoder Token"></div>' +
        '<div class="field"><label>Refresh Token（可选）</label>' +
        '<input class="input" id="mRefresh" type="password" autocomplete="off" placeholder="用于自动续期"></div>' +
        '<div class="field-row"><div class="field"><label>UID（可选）</label>' +
        '<input class="input" id="mUid" placeholder="留空自动生成"></div>' +
        '<div class="field"><label>昵称（可选）</label><input class="input" id="mNickname" placeholder="展示名称"></div></div>';
    }
    if (channel === 'codebuddy') {
      return '<div class="note info">从 WorkBuddy 客户端或网页登录态中获取 accessToken。' +
        '填写 Refresh Token 后可自动续期。</div>' +
        '<div class="field"><label>Access Token</label>' +
        '<input class="input" id="mToken" type="password" autocomplete="off" placeholder="粘贴 accessToken"></div>' +
        '<div class="field"><label>Refresh Token（可选）</label>' +
        '<input class="input" id="mRefresh" type="password" autocomplete="off"></div>' +
        '<div class="field-row"><div class="field"><label>UID（可选）</label><input class="input" id="mUid"></div>' +
        '<div class="field"><label>昵称（可选）</label><input class="input" id="mNickname"></div></div>' +
        '<div class="field-row"><div class="field"><label>Domain（可选）</label><input class="input" id="mDomain"></div>' +
        '<div class="field"><label>Enterprise ID（可选）</label><input class="input" id="mEnterprise"></div></div>' +
        '<div class="field"><label>过期时间（可选）</label><input class="input" id="mExpires" placeholder="Unix 时间戳或日期"></div>';
    }
    if (channel === 'zcode') {
      return '<div class="note info">API Key 形如 <code>keyId.secret</code>，用于推理与 coding-plan 额度；' +
        'OAuth access token 可用于换取 Key；JWT 用于余额与签到。填写其中一种即可。</div>' +
        '<div class="field"><label>API Key（推荐）</label>' +
        '<input class="input" id="mApiKey" type="password" autocomplete="off" placeholder="粘贴 apiKey.secret"></div>' +
        '<div class="field"><label>OAuth Access Token（可选）</label>' +
        '<input class="input" id="mOAuthToken" type="password" autocomplete="off"></div>' +
        '<div class="field"><label>JWT Token（可选，用于签到/余额）</label>' +
        '<input class="input" id="mJwt" type="password" autocomplete="off"></div>' +
        '<div class="field-row"><div class="field"><label>UID（可选）</label><input class="input" id="mUid"></div>' +
        '<div class="field"><label>昵称（可选）</label><input class="input" id="mNickname"></div></div>';
    }
    return '<div class="field"><label>Base URL</label>' +
      '<input class="input" id="mBaseUrl" placeholder="https://api.example.com/v1"></div>' +
      '<div class="field"><label>API Key</label>' +
      '<input class="input" id="mApiKey" type="password" autocomplete="off" placeholder="Bearer Key"></div>' +
      '<div class="field"><label>名称（可选）</label><input class="input" id="mName" placeholder="用于区分上游"></div>' +
      '<div class="field"><label>模型列表（可选）</label>' +
      '<textarea id="mModels" placeholder="每行或逗号分隔，例如：&#10;gpt-5.6&#10;gpt-4o"></textarea>' +
      '<div class="hint">留空会自动请求上游 /models，失败时使用内置 GPT 模型列表。</div></div>' +
      '<div class="field"><label>模型映射（可选）</label>' +
      '<textarea id="mModelMap" placeholder="每行一个：对外名称=上游模型&#10;my-gpt=gpt-4o"></textarea></div>' +
      '<div class="field"><label>自定义请求头（可选，JSON）</label>' +
      '<textarea id="mHeaders" placeholder=\'{"X-Custom": "value"}\'></textarea>' +
      '<div class="hint">认证类请求头会被忽略，Authorization 始终使用上方 API Key。</div></div>' +
      '<div class="field-row"><div class="field"><label>Organization（可选）</label><input class="input" id="mOrg"></div>' +
      '<div class="field"><label>Project（可选）</label><input class="input" id="mProject"></div></div>';
  }

  function openAddChannel(channel, presetRealm) {
    var meta = channelMeta(channel);
    var methods = [];
    if (meta.login === 'oauth') { methods.push(['oauth', 'OAuth 登录']); }
    methods.push(['manual', '手动凭证']);
    if (meta.local_import) { methods.push(['local', '本地客户端']); }
    var methodButtons = methods.map(function (m, i) {
      return '<button type="button" data-method="' + m[0] + '" class="' + (i === 0 ? 'active' : '') + '">' +
        m[1] + '</button>';
    }).join('');
    var panels = '';
    if (meta.login === 'oauth') {
      panels += '<div data-panel="oauth"><div class="oauth-box"><button class="btn primary" type="button" ' +
        'id="oauthStart" data-action="start-oauth">打开授权页面</button><div class="state" id="oauthState">' +
        '点击后将在系统浏览器打开官方授权页，完成后这里会自动识别。</div>' +
        '<a class="oauth-link hidden" id="oauthLink" target="_blank" rel="noreferrer noopener"></a></div>' +
        '<div class="hint" style="margin-top:10px">授权流程完全在官方页面完成，本工具只接收返回的登录凭证。' +
        '扫码或登录时请确认地址栏域名。</div></div>';
    }
    panels += '<div data-panel="manual" class="' + (meta.login === 'oauth' ? 'hidden' : '') + '">' +
      manualFields(channel) +
      '<button class="btn primary block" type="button" data-action="submit-manual" style="margin-top:6px">保存账号</button></div>';
    if (meta.local_import) {
      panels += '<div data-panel="local" class="hidden"><div class="note info">' +
        '只读扫描本机客户端的登录状态，不会修改任何文件。导入前请确认客户端已登录。</div>' +
        '<button class="btn" type="button" data-action="scan-local">扫描本地凭证</button>' +
        '<div id="scanResult"></div></div>';
    }
    state.add = { channel: channel, realm: presetRealm || (meta.realm_options[0] || [''])[0] };
    state.scan = null;
    openModal({
      title: '添加 ' + meta.label + ' 账号',
      subtitle: '选择授权方式；所有凭证均只保存在本机',
      body: realmOptionsHtml(channel, state.add.realm) +
        '<div class="method-tabs">' + methodButtons + '</div>' + panels,
      footer: '<button class="btn" type="button" data-close>关闭</button>'
    });
    if (state.add.realm) {
      var realmInput = $('#addRealm');
      if (realmInput) { realmInput.value = state.add.realm; }
    }
  }

  function currentRealm() {
    var input = $('#addRealm');
    return input ? input.value : '';
  }

  function openAuthUrl(url) {
    if (!url) { return; }
    var bridge = window.ruodaoDesktop;
    if (bridge && typeof bridge.openExternal === 'function') {
      bridge.openExternal(url).then(function (opened) {
        if (!opened) { window.open(url, '_blank', 'noopener'); }
      }).catch(function () {
        window.open(url, '_blank', 'noopener');
      });
      return;
    }
    window.open(url, '_blank', 'noopener');
  }

  function startOauth(btn) {
    if (state.login) {
      if (state.login.failed) {
        cancelLogin();
      } else {
        openAuthUrl(state.login.authUrl);
        return;
      }
    }
    var channel = state.add.channel;
    var realm = currentRealm();
    btn.disabled = true;
    btn.textContent = '正在创建授权会话…';
    $('#oauthState').textContent = '正在创建授权会话…';
    api('/api/login/' + channel, { method: 'POST', body: { realm: realm } }).then(function (res) {
      var stateNode = $('#oauthState');
      if (!stateNode || !btn.isConnected) {
        api('/api/login/' + channel + '/cancel', {
          method: 'POST', body: { state: res.state }
        }).catch(function () { /* ignore */ });
        return;
      }
      if (!res.authUrl) {
        api('/api/login/' + channel + '/cancel', {
          method: 'POST', body: { state: res.state }
        }).catch(function () { /* ignore */ });
        throw new Error('上游未返回授权链接，请稍后重试或改用手动凭证');
      }
      state.login = {
        channel: channel,
        state: res.state,
        authUrl: res.authUrl,
        timer: null,
        busy: false,
        done: false,
        failed: false
      };
      var link = $('#oauthLink');
      if (link) {
        link.href = res.authUrl;
        link.textContent = '浏览器没有自动打开？点此打开授权页';
        link.classList.remove('hidden');
      }
      $('#oauthState').innerHTML = '<span class="spinner"></span>' +
        '<div style="margin-top:9px">已打开系统浏览器，等待完成授权…</div>';
      openAuthUrl(res.authUrl);
      btn.textContent = '重新打开授权页';
      btn.disabled = false;
      state.login.timer = setInterval(pollLogin, 2500);
      pollLogin();
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = '打开授权页面';
      $('#oauthState').textContent = e.message;
      toast(e.message, 'err');
    });
  }

  function pollLogin() {
    var login = state.login;
    if (!login || login.busy) { return; }
    login.busy = true;
    api('/api/login/' + login.channel + '/poll?state=' + encodeURIComponent(login.state))
      .then(function (res) {
        var node = $('#oauthState');
        if (res.status === 'ok') {
          login.done = true;
          if (login.timer) { clearInterval(login.timer); }
          state.login = null;
          toast('已添加 ' + ((res.account && res.account.name) || '账号'), 'ok');
          $('#modalRoot').innerHTML = '';
          state.add = null;
          return reloadCore();
        }
        if (res.status === 'error' || res.status === 'expired' || res.status === 'unknown') {
          if (login.timer) { clearInterval(login.timer); login.timer = null; }
          login.failed = true;
          if (node) {
            node.innerHTML = '<div class="note error" style="text-align:left;margin:0">' +
              esc(res.message || '授权失败') + '</div>';
          }
          var btn = $('#oauthStart');
          if (btn) { btn.disabled = false; btn.textContent = '重新开始授权'; }
          var link = $('#oauthLink');
          if (link) { link.classList.add('hidden'); link.removeAttribute('href'); }
          return;
        }
        if (node) {
          node.innerHTML = '<span class="spinner"></span><div style="margin-top:9px">' +
            esc(res.message || '等待授权…') + '</div>';
        }
      })
      .catch(function (e) {
        var node = $('#oauthState');
        if (node) { node.textContent = e.message; }
      })
      .then(function () { if (state.login === login) { login.busy = false; } });
  }

  function val(id) {
    var node = document.getElementById(id);
    return node ? String(node.value || '').trim() : '';
  }

  function parseMap(text, label) {
    var trimmed = String(text || '').trim();
    if (!trimmed) { return undefined; }
    if (trimmed.charAt(0) === '{') {
      try {
        var obj = JSON.parse(trimmed);
        if (obj && typeof obj === 'object') { return obj; }
      } catch (e) {
        throw new Error(label + '不是合法 JSON');
      }
    }
    var out = {};
    trimmed.split(/\n/).forEach(function (line) {
      var idx = line.indexOf('=');
      if (idx > 0) {
        out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
      }
    });
    if (!Object.keys(out).length) { throw new Error(label + '格式应为 别名=上游模型'); }
    return out;
  }

  function submitManual(btn) {
    var channel = state.add.channel;
    var realm = currentRealm();
    var payload = { channel: channel, realm: realm };
    try {
      if (channel === 'qoder') {
        var token = val('mToken');
        if (!token) { toast('请填写 Access Token 或 PAT', 'err'); return; }
        payload.access_token = token;
        payload.refresh_token = val('mRefresh');
        payload.uid = val('mUid');
        payload.nickname = val('mNickname');
      } else if (channel === 'codebuddy') {
        var cbToken = val('mToken');
        if (!cbToken) { toast('请填写 Access Token', 'err'); return; }
        payload.access_token = cbToken;
        payload.refresh_token = val('mRefresh');
        payload.uid = val('mUid');
        payload.nickname = val('mNickname');
        payload.domain = val('mDomain');
        payload.enterprise_id = val('mEnterprise');
        payload.expires_at = val('mExpires');
      } else if (channel === 'zcode') {
        var apiKey = val('mApiKey');
        var oauthToken = val('mOAuthToken');
        if (!apiKey && !oauthToken) {
          toast('请填写 API Key 或 OAuth Access Token', 'err');
          return;
        }
        payload.api_key = apiKey;
        payload.oauth_access_token = oauthToken;
        payload.jwt_token = val('mJwt');
        payload.uid = val('mUid');
        payload.nickname = val('mNickname');
      } else {
        var baseUrl = val('mBaseUrl');
        var gptKey = val('mApiKey');
        if (!baseUrl) { toast('请填写 Base URL', 'err'); return; }
        if (!gptKey) { toast('请填写 API Key', 'err'); return; }
        payload.base_url = baseUrl;
        payload.api_key = gptKey;
        payload.name = val('mName');
        var models = val('mModels');
        payload.models = models ? models.split(/[\n,]/).map(function (x) { return x.trim(); })
          .filter(Boolean) : undefined;
        payload.model_map = parseMap(val('mModelMap'), '模型映射');
        var headersText = val('mHeaders');
        if (headersText) {
          try {
            payload.extra_headers = JSON.parse(headersText);
          } catch (e) {
            toast('自定义请求头不是合法 JSON', 'err');
            return;
          }
        }
        payload.organization = val('mOrg');
        payload.project = val('mProject');
      }
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    btn.disabled = true;
    api('/api/accounts', { method: 'POST', body: payload }).then(function (res) {
      toast('已添加 ' + ((res.account && res.account.name) || '账号'), 'ok');
      $('#modalRoot').innerHTML = '';
      state.add = null;
      return reloadCore();
    }).catch(function (e) {
      toast(e.message, 'err');
      btn.disabled = false;
    });
  }

  function scanLocal(btn) {
    var channel = state.add.channel;
    btn.disabled = true;
    api('/api/scan/' + channel).then(function (res) {
      state.scan = res.items || [];
      renderScan();
    }).catch(function (e) {
      toast(e.message, 'err');
    }).then(function () { btn.disabled = false; });
  }

  function renderScan() {
    var root = $('#scanResult');
    if (!root) { return; }
    var items = state.scan || [];
    if (!items.length) {
      root.innerHTML = '<div class="note" style="margin-top:12px;margin-bottom:0">未发现本地登录凭证。' +
        '请先登录对应客户端，或改用 OAuth / 手动凭证。</div>';
      return;
    }
    var rows = items.map(function (item) {
      var meta = [];
      if (item.path) { meta.push(item.path); }
      if (item.realm) { meta.push(item.realm); }
      if (item.uid) { meta.push('UID ' + item.uid); }
      if (item.expires_at) { meta.push('到期 ' + fmtTime(item.expires_at)); }
      var badge = item.encrypted ?
        '<span class="pill amber">加密存储</span>' :
        (item.importable === false || (state.add.channel === 'zcode' && !item.importable) ?
          '<span class="pill red">无法解析</span>' : '<span class="pill green">可导入</span>');
      return '<div class="scan-item"><div><b>' + esc(item.app || item.nickname || '本地账号') + '</b>' +
        (item.nickname ? ' <span class="faint">' + esc(item.nickname) + '</span>' : '') +
        '<div class="meta">' + esc(meta.join(' · ')) + '</div></div>' + badge + '</div>';
    }).join('');
    var warning = items.some(function (i) { return i.encrypted; }) ?
      '<div class="note" style="margin-top:12px">部分凭证由客户端加密保存，无法离线解密，' +
      '请改用 OAuth 登录或手动导入。</div>' : '';
    root.innerHTML = '<div class="scan-list">' + rows + '</div>' + warning +
      '<button class="btn primary block" type="button" data-action="import-local" style="margin-top:12px">' +
      '导入全部可识别账号</button>';
  }

  function importLocal(btn) {
    var channel = state.add.channel;
    btn.disabled = true;
    api('/api/import/' + channel, { method: 'POST', body: { realm: currentRealm() || null } })
      .then(function (res) {
        var imported = res.imported || [];
        var errors = res.errors || [];
        if (imported.length) {
          toast('已导入 ' + imported.length + ' 个账号', 'ok');
          $('#modalRoot').innerHTML = '';
          state.add = null;
          return reloadCore();
        }
        toast(errors[0] || '没有可导入的账号', 'err');
        if (errors.length > 1) {
          var root = $('#scanResult');
          if (root) {
            root.innerHTML = '<div class="note error" style="margin-top:12px">' +
              esc(errors.join('；')) + '</div>' + root.innerHTML;
          }
        }
        btn.disabled = false;
      }).catch(function (e) {
        toast(e.message, 'err');
        btn.disabled = false;
      });
  }

  // --------------------------------------------------------------- actions
  function accountAction(btn) {
    var id = btn.dataset.id;
    var op = btn.dataset.op;
    btn.disabled = true;
    api('/api/accounts/' + encodeURIComponent(id) + '/' + op, { method: 'POST', body: {} })
      .then(function (res) {
        if (op === 'checkin') {
          var result = res.result || {};
          toast(result.message || result.error || '签到完成', result.ok === false ? 'err' : 'ok');
        } else if (op === 'refresh') {
          toast(res.changed ? '凭证已刷新' : '刷新完成', 'ok');
        } else if (op === 'test') {
          var detail = res.result || {};
          toast(detail.message || (res.ok ? '连接正常' : '测试失败'), res.ok ? 'ok' : 'err');
        }
        return reloadCore();
      }).catch(function (e) {
        toast(e.message, 'err');
        btn.disabled = false;
      });
  }

  function deleteAccount(btn) {
    var id = btn.dataset.id;
    var name = btn.dataset.name || '该账号';
    confirmDialog('删除账号', '确定删除「' + name + '」？本机保存的凭证会一并移除，此操作不可撤销。', '删除')
      .then(function (ok) {
        if (!ok) { return; }
        return api('/api/accounts/' + encodeURIComponent(id), { method: 'DELETE' })
          .then(function () {
            toast('账号已删除', 'ok');
            return reloadCore();
          }).catch(function (e) { toast(e.message, 'err'); });
      });
  }

  function toggleAccount(btn) {
    var id = btn.dataset.id;
    var enabled = btn.dataset.enabled === '1';
    btn.disabled = true;
    api('/api/accounts/' + encodeURIComponent(id), {
      method: 'PATCH', body: { enabled: enabled }
    }).then(function () {
      toast(enabled ? '账号已启用' : '账号已停用', 'ok');
      return reloadCore();
    }).catch(function (e) {
      toast(e.message, 'err');
      btn.disabled = false;
    });
  }

  function handleAction(action, el) {
    var type = action.dataset.action;
    if (type === 'open-add') {
      if (action.dataset.channel) { openAddChannel(action.dataset.channel); }
      else { openChannelPicker(); }
    } else if (type === 'pick-channel') {
      openAddChannel(action.dataset.channel);
    } else if (type === 'goto-accounts') {
      state.accountFilter = action.dataset.filter || 'all';
      switchTab('accounts');
    } else if (type === 'goto-settings') {
      switchTab('settings');
    } else if (type === 'goto-usage') {
      switchTab('usage');
    } else if (type === 'quickstart') {
      state.quickstart = action.dataset.mode || 'openai';
      renderOverview();
    } else if (type === 'filter-account') {
      state.accountFilter = action.dataset.filter || 'all';
      renderAccounts();
    } else if (type === 'filter-model') {
      state.modelFilter = action.dataset.filter || 'all';
      renderModels();
    } else if (type === 'usage-range') {
      state.usageRange = Number(action.dataset.range || 86400);
      loadUsage().then(renderUsage).catch(function (e) { toast(e.message, 'err'); });
    } else if (type === 'log-filter') {
      state.logFilter = action.dataset.level || 'all';
      renderLogs();
    } else if (type === 'toggle-detail') {
      var id = action.dataset.id;
      if (state.expanded[id]) { delete state.expanded[id]; }
      else { state.expanded[id] = true; }
      renderAccounts();
    } else if (type === 'acc-action') {
      accountAction(action);
    } else if (type === 'delete-account') {
      deleteAccount(action);
    } else if (type === 'toggle-account') {
      toggleAccount(action);
    } else if (type === 'relogin') {
      openAddChannel(action.dataset.channel, action.dataset.realm);
    } else if (type === 'copy-code') {
      var target = $(action.dataset.target);
      if (target) { copyText(target.textContent); }
    } else if (type === 'start-oauth') {
      startOauth(action);
    } else if (type === 'submit-manual') {
      submitManual(action);
    } else if (type === 'scan-local') {
      scanLocal(action);
    } else if (type === 'import-local') {
      importLocal(action);
    } else if (type === 'copy-api-key') {
      api('/api/config/reveal', { method: 'POST', body: {} })
        .then(function (res) { copyText(res.api_key); })
        .catch(function (e) { toast(e.message, 'err'); });
    } else if (type === 'goto-keys') {
      switchTab('keys');
    } else if (type === 'new-key') {
      openKeyCreate();
    } else if (type === 'create-key') {
      createKey(action);
    } else if (type === 'edit-key') {
      openKeyEdit(action.dataset.id);
    } else if (type === 'save-key') {
      saveKeyEdit(action, action.dataset.id);
    } else if (type === 'toggle-key') {
      toggleKey(action);
    } else if (type === 'copy-key') {
      copyKeySecret(action.dataset.id);
    } else if (type === 'rotate-key') {
      rotateKey(action);
    } else if (type === 'delete-key') {
      deleteKey(action);
    } else if (type === 'save-settings') {
      saveSettings(action);
    } else if (type === 'save-guard') {
      saveGuard(action);
    } else if (type === 'reset-guard') {
      resetGuard(action);
    } else if (type === 'refresh-guard') {
      loadGuard().then(renderGuard).catch(function (e) { toast(e.message, 'err'); });
    } else if (type === 'refresh-usage') {
      loadUsage().then(renderUsage).catch(function (e) { toast(e.message, 'err'); });
    } else if (type === 'refresh-logs') {
      loadEvents().then(renderLogs).catch(function (e) { toast(e.message, 'err'); });
    }
  }

  // ---------------------------------------------------------------- bindings
  function bind() {
    document.addEventListener('click', function (event) {
      var closeBtn = event.target.closest('[data-close]');
      if (closeBtn) { closeModal(); return; }
      if (event.target.matches('[data-backdrop]')) { closeModal(); return; }
      var copyEl = event.target.closest('[data-copy]');
      if (copyEl) { copyText(copyEl.getAttribute('data-copy')); return; }
      var realmOpt = event.target.closest('[data-realm-opt]');
      if (realmOpt) {
        var group = realmOpt.parentNode;
        $$('.chip', group).forEach(function (chip) { chip.classList.toggle('active', chip === realmOpt); });
        var hidden = $('#addRealm');
        if (hidden) { hidden.value = realmOpt.dataset.realmOpt; }
        return;
      }
      var method = event.target.closest('[data-method]');
      if (method) {
        var modal = method.closest('.modal');
        $$('[data-method]', modal).forEach(function (b) { b.classList.toggle('active', b === method); });
        $$('[data-panel]', modal).forEach(function (p) {
          p.classList.toggle('hidden', p.dataset.panel !== method.dataset.method);
        });
        return;
      }
      var toggle = event.target.closest('[data-toggle]');
      if (toggle) {
        var on = !toggle.classList.contains('on');
        toggle.classList.toggle('on', on);
        toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
        return;
      }
      var tabBtn = event.target.closest('[data-tab]');
      if (tabBtn) { switchTab(tabBtn.dataset.tab); return; }
      var actionEl = event.target.closest('[data-action]');
      if (actionEl) {
        event.preventDefault();
        handleAction(actionEl, event);
      }
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && $('#modalRoot').innerHTML) { closeModal(); }
      if (event.key === 'Enter' && event.target.id === 'modelSearch') {
        event.preventDefault();
        renderModelGroups();
      }
    });

    $('#refreshAll').addEventListener('click', function () {
      var btn = $('#refreshAll');
      btn.disabled = true;
      Promise.all([loadStatus(), loadAccounts(), loadModels(), loadKeys(), loadSettings(), loadUsage(), loadGuard(), loadEvents()])
        .then(function () {
          renderTabs();
          renderStatus();
          renderTabs();
          renderTabContent();
          toast('已刷新', 'ok');
        })
        .catch(function (e) { toast(e.message, 'err'); })
        .then(function () { btn.disabled = false; });
    });
  }

  function backgroundRefresh() {
    if (document.hidden) { return; }
    if (state.login) { return; }
    Promise.all([loadStatus(), loadAccounts(), loadKeys()]).then(function () {
      renderStatus();
      var total = state.accounts.length;
      $('#tabAccountCount').textContent = total ? String(total) : '';
      $('#tabKeyCount').textContent = state.keys.length ? String(state.keys.length) : '';
      if (state.tab === 'overview') {
        loadUsage().then(function () {
          renderOverview();
        }).catch(function () { renderOverview(); });
      } else if (state.tab === 'accounts') {
        renderAccounts();
      } else if (state.tab === 'keys') {
        renderTabs();
        renderKeys();
      } else if (state.tab === 'guard') {
        var active = document.activeElement;
        if (!active || !active.closest('#view-guard .input')) {
          loadGuard().then(renderGuard).catch(function () { /* ignore */ });
        }
      } else if (state.tab === 'logs') {
        loadEvents().then(renderLogs).catch(function () { /* ignore */ });
      }
    }).catch(function () { /* keep previous state */ });
  }

  function boot() {
    bind();
    try {
      var saved = localStorage.getItem('ruodao.tab');
      if (saved && $('#view-' + saved)) { state.tab = saved; }
    } catch (e) { /* ignore */ }
    renderTabs();
    Promise.all([loadStatus(), loadAccounts(), loadModels(), loadKeys(), loadSettings(), loadUsage(), loadEvents()])
      .then(function () {
        renderTabs();
        renderStatus();
        renderTabContent();
        if (state.tab !== 'usage') { renderOverview(); }
        var addChannel = new URLSearchParams(location.search).get('add');
        if (['qoder', 'codebuddy', 'zcode', 'gpt'].indexOf(addChannel) >= 0) {
          openAddChannel(addChannel);
          try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ }
        }
      })
      .catch(function (e) {
        $('#view-overview').innerHTML = '<div class="card"><div class="empty"><strong>无法连接管理接口</strong>' +
          esc(e.message) + '<div style="margin-top:12px"><button class="btn" type="button" onclick="location.reload()">' +
          '重新加载</button></div></div></div>';
        toast(e.message, 'err');
      });
    setInterval(backgroundRefresh, 20000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
