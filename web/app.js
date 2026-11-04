'use strict';
// ============================================================================
// NAS 域名优选 · 前端
// 数据来源：GET /api/status（只读快照），写操作只有 /api/probe 和 /api/sites。
// 本文件不含任何凭据；CSRF token 由服务端按客户端地址派生，仅存内存。
// ============================================================================
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// 动作 → [中文标签, 样式]。新增动作时只需在这里加一行。
const ACTIONS = {
  speed_shadow: ['提速对照中', ''],
  speed_override: ['优选地址已应用', 'good'],
  speed_keep_verified: ['优选已复测', 'good'],
  speed_return_original: ['原线路更快', 'good'],
  speed_wait_confirmation: ['等待收益复现', ''],
  speed_deferred: ['样本待补齐', 'warn'],
  speed_unstable: ['暂无稳定线路', 'warn'],
  speed_failed_removed: ['失效覆盖已撤回', 'warn'],
  original_path_healthy: ['原解析正常', 'good'],
  intermittent_keep_previous_if_valid: ['连接有波动', 'warn'],
  validated_override: ['候选已通过', 'good'],
  no_verified_candidate_keep_previous_until_expiry: ['没有可用候选', 'warn'],
  probe_error_keep_previous_until_expiry: ['域名查询异常', 'warn'],
  excluded_not_confirmed_cloudflare: ['不满足优选条件', 'warn'],
  origin_only: ['原连接可达', 'good'],
  origin_connection_failed: ['原连接未通过', 'warn'],
  dns_unresolved: ['域名无 IPv4 解析', 'warn'],
  non_public_address_excluded: ['地址不适用', 'warn']
};

// 需要用户关注的动作集合（用于“需要关注”筛选和总览提示）。
const ATTENTION = new Set([
  'speed_unstable', 'speed_deferred', 'speed_failed_removed',
  'intermittent_keep_previous_if_valid',
  'no_verified_candidate_keep_previous_until_expiry',
  'probe_error_keep_previous_until_expiry',
  'origin_connection_failed',
  'dns_unresolved',
  'non_public_address_excluded'
]);

const CLASS_LABEL = {
  cloudflare: 'Cloudflare', multi_cdn: '多 CDN，实测择优', multi_cdn: '多 CDN，实测择优', origin: '普通域名，仅监控', unresolved: '解析待处理',
  excluded: '地址不适用', pending: '待识别'
};

// 阶段 → 中文，用于失败原因展示。
const PHASE_LABEL = { tcp: 'TCP 连接', tls: 'TLS 握手', http: 'HTTP 响应', dns: 'DNS 查询', config: '配置', unknown: '连接' };

let speedDraft = null, speedSaving = false, siteListScroll = 0, statusUnavailable = false, lastInteractionAt = 0;
const sitePolicyBusy=new Set();
for(const event of ['pointerdown','keydown','input']) document.addEventListener(event,()=>{lastInteractionAt=Date.now();},{passive:true});
function interacting(){return Date.now()-lastInteractionAt<1200 || document.activeElement?.tagName==='SELECT';}
let data = null, selected = '', view = 'overview', busy = false, timer;
let pendingRequest = '', removingId = '';
// 过滤状态：同时同步到 URL hash，刷新或切换页面后不丢失。
let filter = '', query = '', category = 'all', siteQuery = '', siteId = '', siteFilter = 'all';
// 记录本程序自己写入的 hash，用于忽略随后触发的 hashchange（否则输入会被回滚）。
let suppressHashChange = null;

const siteName = d => data?.domain_meta?.[d]?.site_name || d;

// ---------------------------------------------------------------------------
// 过滤条件 ↔ URL
// ---------------------------------------------------------------------------
const VIEWS = ['acceptance','health','reports','domains','transfer','diagnosis','changes','advanced','speed', 'overview', 'sites', 'history', 'trends', 'logs', 'policy', 'update', 'help'];

function readStateFromUrl() {
  const raw = location.hash.slice(1) || 'overview';
  const [name, search] = raw.split('?');
  view = VIEWS.includes(name) ? name : 'overview';
  const params = new URLSearchParams(search || '');
  query = params.get('q') || '';
  category = params.get('c') || 'all';
  siteQuery = params.get('sq') || '';
  filter = params.get('f') || '';
  siteId = params.get('site') || ''; siteFilter = params.get('sf') || 'all';
  if(params.has('d')) selected=params.get('d');
  if(view==='overview' && (query || category!=='all')) view='domains';
}

function writeStateToUrl(replace = false) {
  const params = new URLSearchParams();
  if (query) params.set('q', query);
  if (category !== 'all') params.set('c', category);
  if (siteQuery) params.set('sq', siteQuery);
  if (filter) params.set('f', filter);
  if(siteId && view==='sites') params.set('site',siteId);
  if(siteFilter!=='all') params.set('sf',siteFilter);
  if(selected && (view==='domains'||(view==='sites'&&siteId))) params.set('d',selected);
  const search = params.toString();
  const next = '#' + view + (search ? '?' + search : '');
  if (location.hash === next) return;
  // 重要：pushState / replaceState 改变 hash 时浏览器会触发 hashchange，
  // 而 route() 会重新从 URL 读状态，导致刚输入的筛选被立刻覆盖回旧值。
  // 这里在写入前先记录“本程序自己写的那一次”，由 hashchange 处理函数忽略它。
  suppressHashChange = next;
  if (replace) history.replaceState(null, '', next);
  else history.pushState(null, '', next);
}

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------
function time(t, full = false) {
  if (!t) return '—';
  return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', ...(full ? {month:'2-digit',day:'2-digit'} : {}),hour:'2-digit',minute:'2-digit',hour12:false}).format(t*1000);
}
function fullTime(t) {
  return t ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(t*1000) : '—';
}
function message(text, error=false) {$('#message').textContent=text;$('#message').classList.toggle('error',error);}

// 单次探测结果 → 一句可读结论。优先使用后端给出的 reason/phase。
function probeText(p) {
  if (!p) return '无结果';
  if (p.ok) return `已连接 · HTTP ${p.http ?? '?'} · ${p.elapsed_ms.toLocaleString()} ms`;
  const phase = PHASE_LABEL[p.phase] || '连接';
  const reason = p.reason || ({
    TimeoutError: '连接超时', SSLCertVerificationError: '证书校验不通过',
    ConnectionRefusedError: '连接被拒绝', gaierror: '域名解析失败',
    RemoteDisconnected: '服务器没有返回 HTTP 响应'
  })[p.error] || `失败（${p.error || '未知错误'}）`;
  return `${phase}失败：${reason}${p.elapsed_ms ? ` · ${p.elapsed_ms} ms` : ''}`;
}

// 一组基线结果 → 汇总说明（覆盖了哪些地址、是否健康）。
function baselineSummary(row) {
  const items = row.baseline || [];
  if (!items.length) return '本轮尚未完成连接检查。';
  const parts = items.map(item => {
    const results = item.results || [];
    const ok = results.filter(x => x.ok).length;
    return `${item.ip}：${ok}/${results.length} 次通过`;
  });
  const head = row.baseline_healthy ? '原路径可用' : '所有采样地址均未通过';
  return `${head}（${parts.join('，')}）`;
}

// ---------------------------------------------------------------------------
// 域名卡片与详情
// ---------------------------------------------------------------------------
function statusTag(row, ip, domain) {
  if (data.domain_meta?.[domain]?.enabled === false) return [ip ? '已停用 · 待撤销' : '已停用', ''];
  if (ip) return ['正在使用优选', 'good'];
  return ACTIONS[row?.action] || ['等待检测', ''];
}

function attentionOf(row) {
  return ATTENTION.has(row?.action);
}

function domainRow(domain) {
  const row = data.latest.domains[domain] || {};
  const ip = data.published[domain];
  const [label, tone] = statusTag(row, ip, domain);
  const type = CLASS_LABEL[row.classification] || '待识别';
  const meta = data.domain_meta?.[domain];
  const role = meta?.roles.includes('tracker') ? 'Tracker' : '站点网页';
  const note = ip ? `当前地址 ${ip}` : '使用原解析';
  // 明细：优先解释“为什么没通过”，而不是只报成功次数。
  const currentMetric = row.speed?.metrics?.[ip];
  const detail = currentMetric?.stable ? `当前优选 ${currentMetric.successes}/${currentMetric.expected} 次通过；原路径与当前地址分开记录` : row.baseline
    ? (attentionOf(row) && (row.baseline_failure?.reason || row.reason)
        ? `${PHASE_LABEL[row.baseline_failure?.phase] || ''}失败：${row.baseline_failure?.reason || row.reason}`
        : `本轮连接 ${(row.baseline || []).reduce((n, item) => n + (item.results || []).filter(x => x.ok).length, 0)}/${(row.baseline || []).reduce((n, item) => n + (item.results || []).length, 0)} 次成功`)
    : row.reason || (row.error ? '本轮域名查询未完成' : '尚无检测结果');
  return `<article class="domain ${selected===domain?'selected':''} ${attentionOf(row)?'needs-attention':''}"><div class="domain-top"><div class="domain-name">${esc(siteName(domain))}<span class="mono">${esc(domain)}</span><span>${role} · ${type}</span></div><span class="badge ${tone}">${label}</span></div><div class="domain-bottom"><div><span class="${ip?'ip mono':'explain'}">${esc(note)}</span><div class="muted small">${esc(detail)}</div></div><button data-domain="${esc(domain)}" aria-label="查看${esc(domain)}检测详情" aria-pressed="${selected===domain}">详情</button></div></article>`;
}

function detail() {
  const row = data.latest.domains[selected] || {};
  const dns = row.public_dns;
  const attempts = row.candidate_probes || [];
  const ip = data.published[selected];
  const entry = data.entries[selected];
  let candidate = '候选需要完成逐域名对照，收益达到门槛后才会应用；详细数据见“优选效果”。';
  if (row.selected_ip) candidate = `通过验证并启用：${row.selected_ip}`;
  else if (row.renewed_ip) candidate = `当前地址 ${row.renewed_ip} 已重新完成验证，续期保留。`;
  else if (attempts.length) {
    const stable = attempts.filter(a=>row.speed?.metrics?.[a.ip]?.stable).length;
    candidate = `本轮探索 ${attempts.length} 个候选，${stable} 个完成稳定采样；应用仍需满足收益和确认窗口条件。`;
    if (row.speed?.budget_exhausted || row.budget_exhausted) candidate += '其余样本在后续轮次补齐。';
  }
  const steps = [
    [!!dns?.public, '确认域名归属',
      dns ? (dns.eligible ? `公开 DNS 确认是 Cloudflare 地址（${(dns.addresses || []).join('、')}），可以参与优选。`
        : dns.public ? `普通公开域名（${(dns.addresses || []).join('、')}），只监控连接，不套用 Cloudflare IP。`
        : '没有可用的公开 IPv4 地址，保留为待处理。')
        : '公开 DNS 查询尚未完成；不会当作非 Cloudflare 站点。'],
    [!!row.baseline_healthy, '检查当前连接', baselineSummary(row)],
    [!!row.selected_ip || !!row.renewed_ip, '验证候选地址', candidate],
    [!!ip, '查看实际 DNS 发布', ip ? `正在使用 ${ip}` : '当前没有优选覆盖，沿用原解析。']
  ];
  // 失败原因单独成块，方便直接判断“是站点问题还是网络问题”。
  const failure = row.baseline_failure || (row.reason && !row.selected_ip ? {phase: row.phase, reason: row.reason} : null);
  const failureBlock = failure
    ? `<div class="failure"><strong>本轮检测说明</strong><p>${esc(PHASE_LABEL[failure.phase] || '连接')}：${esc(failure.reason || failure.error || '未知')}</p>${failure.detail ? `<p class="mono small">${esc(failure.detail)}</p>` : ''}</div>`
    : '';
  const attemptsBlock = attempts.length
    ? `<details id="domainAttempts-${esc(selected)}" class="attempts"><summary>候选地址明细（${attempts.length} 个）</summary><ul>${attempts.map(a => `<li><span class="mono">${esc(a.ip)}</span><ul>${(a.results || []).map(r => `<li>${esc(probeText(r))}</li>`).join('')}</ul></li>`).join('')}</ul></details>`
    : '';
  const expires = ip && entry?.expires_at ? `当前优选有效至 ${time(entry.expires_at, true)}。` : '原线路正常也参与提速对照；达到收益门槛并跨窗口确认后切换。';
  return `<div class="panel-head"><h2>${esc(siteName(selected))}检测详情</h2><span class="small muted">${time(data.latest.finished_at)}</span></div><div class="detail"><p class="domain-label mono">${esc(selected)}</p><div class="steps">${steps.map(s => `<div class="step ${s[0]?'pass':''}"><strong>${s[1]}</strong><p>${esc(s[2])}</p></div>`).join('')}</div>${failureBlock}${attemptsBlock}<div class="detail-foot">${expires}<br>HTTPS 根路径的连通结果不代表实际 Tracker 汇报成功。</div></div>`;
}

// ---------------------------------------------------------------------------
// 事件流（从历史里推导出“切换/撤销”节点）
// ---------------------------------------------------------------------------
function events() {
  const list = [];
  const chronological = [...data.history].reverse();
  let previous = {};
  for (const run of chronological) {
    const overrides = run.active_overrides || {};
    for (const [domain, row] of Object.entries(run.domains || {})) {
      if (row.action === 'validated_override') list.push({at: run.finished_at, domain, title: '候选地址通过验证', detail: row.selected_ip, ip: true});
      else if (previous[domain] && !overrides[domain] && row.action === 'original_path_healthy') list.push({at: run.finished_at, domain, title: '优选规则已撤销', detail: '原连接恢复，回到原解析。'});
    }
    previous = overrides;
  }
  return list.reverse();
}

function eventRow(e) {
  return `<div class="event"><time>${time(e.at)}<br>${new Date(e.at*1000).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'})}</time><div><strong>${esc(siteName(e.domain))} · ${e.title}</strong><p class="${e.ip?'ip mono':''}">${esc(e.detail)}</p></div></div>`;
}

// ---------------------------------------------------------------------------
// 各页面
// ---------------------------------------------------------------------------
function classification(d) {
  const row = data.latest.domains[d] || {};
  return row.classification || (row.public_dns?.eligible ? 'cloudflare' : 'pending');
}

function matchingDomains() {
  return data.domains.filter(d => {
    const meta = data.domain_meta[d], c = classification(d), row = data.latest.domains[d] || {};
    if (query && !(siteName(d) + ' ' + d).toLowerCase().includes(query.toLowerCase())) return false;
    if (category === 'disabled') return !meta.enabled;
    if (category === 'cf') return meta.enabled && ['cloudflare','multi_cdn'].includes(c);
    if (category === 'origin') return meta.enabled && c === 'origin';
    if (category === 'pending') return meta.enabled && ['pending','unresolved','excluded'].includes(c);
    if (category === 'attention') return meta.enabled && (!row.action || ATTENTION.has(row.action));
    return true;
  });
}

function overview() {
  const visible = matchingDomains(), active = Object.keys(data.published).length;
  if (visible.length && !visible.includes(selected)) selected = visible[0];
  const tracker = data.domains.filter(d => data.domain_meta[d].roles.includes('tracker')).length;
  const cf = data.domains.filter(d => ['cloudflare','multi_cdn'].includes(classification(d))).length;
  const attention = data.domains.filter(d => data.domain_meta[d].enabled && ATTENTION.has((data.latest.domains[d] || {}).action)).length;
  const summary = data.latest.summary;
  const duration = data.latest.duration_seconds;
  return `<dl class="summary"><div><dt>管理站点</dt><dd class="lead">${data.sites.length}<span class="small muted"> 个</span></dd><div class="sub">站点首页与 Tracker 分开记录</div></div><div><dt>全部域名</dt><dd>${data.domains.length}<span class="small muted"> 个</span></dd><div class="sub">Tracker ${tracker} · 网页 ${data.domains.length-tracker}</div></div><div><dt>当前使用优选</dt><dd>${active}<span class="small muted"> 个</span></dd><div class="sub">已识别 Cloudflare ${cf} 个</div></div><div class="${attention?'attention-card':''}"><dt>需要关注</dt><dd>${attention}<span class="small muted"> 个</span></dd><div class="sub">${attention?'可点下面“需要关注”筛选':'本轮没有异常域名'}</div></div><div><dt>最近检测完成</dt><dd class="time">${time(data.latest.finished_at)}</dd><div class="sub">${duration!=null?`本轮用时 ${duration} 秒 · `:''}下次自动检测 ${time(data.next_run)}</div></div></dl><div class="columns"><section class="panel"><div class="panel-head"><div><h2>全部域名连接</h2><p>Cloudflare 按需优选，普通域名保留原解析并监控。</p></div><span class="small muted">${visible.length} / ${data.domains.length} 个域名</span></div><div class="domain-filters"><input id="domainSearch" type="search" aria-label="搜索站点或域名" placeholder="搜索站点或域名" value="${esc(query)}"><select id="domainCategory" aria-label="按域名状态筛选">${[['all','全部域名'],['cf','Cloudflare'],['origin','普通域名'],['pending','待识别或待处理'],['attention','需要关注'],['disabled','已停用']].map(([v,l])=>`<option value="${v}" ${category===v?'selected':''}>${l}</option>`).join('')}</select>${(query||category!=='all')?`<button type="button" class="link-button" id="clearFilters">清除筛选</button>`:''}</div><div class="domains domain-scroll">${visible.map(domainRow).join('') || '<div class="empty">没有符合筛选条件的域名，可以更换筛选或添加站点。</div>'}</div><div class="note">${data.probing?`本轮已完成 ${data.progress.completed||0} / ${data.progress.total||0} 个域名。`:'所有已启用站点都会参与检测。'}${summary?` 分类统计：Cloudflare ${summary.classifications.cloudflare||0} · 多 CDN ${summary.classifications.multi_cdn||0} · 普通 ${summary.classifications.origin||0} · 未解析 ${summary.classifications.unresolved||0}。`:''} 已配置的排除规则保持生效。</div></section><div><section class="panel" id="domainDetail" aria-label="选中域名的检测详情">${detail()}</section><section class="panel history-preview"><div class="panel-head"><h2>最近优选记录</h2><a class="history-link" href="#history">全部记录</a></div>${events().slice(0,3).map(eventRow).join('') || '<div class="empty">尚无候选通过或规则撤销的记录，完成检测后会在这里显示。</div>'}</section></div></div>`;
}

function domainChips(site, domains, role) {
  return domains.map(d => `<a class="domain-chip mono" href="#domains?q=${encodeURIComponent(d)}" title="查看连接详情： ${esc(d)}">${esc(d)}</a>`).join('');
}

function sitesView() {
  const rows = data.sites.filter(s => !siteQuery || (s.name + ' ' + s.website_domains.join(' ') + ' ' + s.tracker_domains.join(' ')).toLowerCase().includes(siteQuery.toLowerCase()));
  const source = s => s.sources?.some(x => x.includes('MP')) ? '来自 MP 与下载器' : s.sources?.includes('手动添加') ? '手动添加' : '来自下载器';
  return `<div class="toolbar"><span class="muted small">${rows.length} 个站点 · 可自行添加、编辑、启用或停用</span><input id="siteSearch" type="search" aria-label="搜索站点" placeholder="搜索站点或域名" value="${esc(siteQuery)}"></div><section class="panel"><div class="site-list">${rows.map(s=>`<article class="site-item"><div class="site-title"><div><h2>${esc(s.name)}</h2><span class="muted small">${esc(source(s))}</span></div><span class="badge ${s.enabled?'good':''}">${s.enabled?'已启用':'已停用'}</span></div><div class="site-domain-group"><span class="muted small">站点首页</span><div>${domainChips(s, s.website_domains) || '<span class="muted small">未填写</span>'}</div></div><div class="site-domain-group"><span class="muted small">Tracker</span><div>${domainChips(s, s.tracker_domains) || '<span class="muted small">暂无已导入 Tracker，可编辑补充</span>'}</div></div><div class="site-actions"><button data-edit="${s.id}">编辑</button><button data-toggle="${s.id}">${s.enabled?'停用':'启用'}</button><button class="link-button remove-site" data-remove="${s.id}">移除</button></div></article>`).join('') || '<div class="empty">没有找到站点。点击右上角“添加站点”即可添加。</div>'}</div></section>${changesView()}`;
}

// 操作审计：成功和被拒绝的改动都列出来，便于事后追溯。
function changesView() {
  const rows = data.changes || [];
  const labels = {save: '保存', remove: '移除', ok: '成功', rejected: '被拒绝', conflict: '版本冲突', not_found: '站点不存在'};
  return `<section class="panel changes"><div class="panel-head"><h2>最近操作记录</h2><span class="small muted">含被拒绝的提交 · 最近 ${rows.length} 条</span></div>${rows.length?`<ul class="change-list">${rows.map(c=>`<li class="${c.result==='ok'?'':'warn'}"><time>${fullTime(c.at)}</time><span>${esc(labels[c.operation]||c.operation||'—')}</span><strong>${esc(c.name||'—')}</strong><span class="muted small">${esc(labels[c.result]||c.result||'')}${c.reason?' · '+esc(c.reason):''}</span></li>`).join('')}</ul>`:'<div class="empty">还没有操作记录。</div>'}</section>`;
}

function historyView() {
  const rows = data.history.filter(r => !filter || r.domains[filter]).slice(0, 48);
  const count = events().filter(e => (!filter || e.domain === filter) && e.at >= (rows.at(-1)?.started_at || 0)).length;
  const runRow = r => {
    const entries = Object.entries(r.domains).filter(([d]) => !filter || d === filter);
    const abnormal = entries.filter(([, row]) => ATTENTION.has(row.action) || row.action === 'validated_override');
    return `<article class="run"><div class="run-head"><strong>${fullTime(r.finished_at)}</strong><span class="muted">检测用时 ${r.duration_seconds ?? (r.finished_at - r.started_at)} 秒 · ${entries.length} 个域名</span></div>` +
      (abnormal.length
        ? `<div class="run-actions">${abnormal.map(([d, row]) => `<span class="${ATTENTION.has(row.action)?'warn':''}">${esc(siteName(d))} <span class="muted">·</span> ${esc((ACTIONS[row.action]||['等待结果'])[0])}${row.selected_ip?` → <span class="mono">${esc(row.selected_ip)}</span>`:''}${row.reason?` <span class="small muted">（${esc(row.reason)}）</span>`:''}</span>`).join('')}</div>`
        : '<div class="run-actions"><span class="muted">本轮没有需要关注的域名，也没有新的切换。</span></div>') +
      `<details class="run-detail"><summary>查看本轮全部 ${entries.length} 个域名</summary><ul>${entries.map(([d,row])=>`<li><span class="mono">${esc(d)}</span> · ${esc((ACTIONS[row.action]||['等待结果'])[0])}${row.reason?` · <span class="muted small">${esc(row.reason)}</span>`:''}</li>`).join('')}</ul></details></article>`;
  };
  return `<div class="toolbar"><div class="small muted">最近 ${rows.length} 轮检测 · ${count} 条优选记录 · 磁盘保留 ${data.history_files ?? '?'} 轮</div><select id="historyFilter" aria-label="按域名筛选记录"><option value="">全部域名</option>${data.domains.map(d=>`<option value="${esc(d)}" ${filter===d?'selected':''}>${esc(siteName(d))} · ${esc(d)}</option>`).join('')}</select></div><section class="panel"><div class="panel-head"><h2>检测记录</h2><span class="small muted">从新到旧 · 北京时间</span></div>${rows.map(runRow).join('') || '<div class="empty">尚无检测记录，完成第一轮检测后会在这里显示。</div>'}</section><p class="small muted">这里显示最近 48 轮，磁盘按设置保留最近 ${data.policy.history_rounds ?? '—'} 轮并自动清理更早记录。</p>`;
}

function policy() {
  const kv = (a,b) => `<div class="kv"><span>${a}</span><strong>${b}</strong></div>`;
  const state=data.runtime_policy||{},actual=state.current,desired=state.desired;
  const modes={auto:'主动优选',canary:'仅灰度域名可切换',shadow:'只测不切换'};
  const describe=(v,label)=>{
    if(!v)return `<section class="panel"><div class="panel-head"><div><h2>${esc(label)}</h2><p>执行快照尚未上报，当前无法确认。等待新一轮检测，已保存配置单独列出。</p></div></div></section>`;
    const p=v.settings,s=v.speed;
    return `<div class="policy-section"><div><h2>${esc(label)}</h2><p>${esc(modes[s.mode]||'模式待确认')} · 快照 ${fullTime(v.captured_at)} · 版本 ${esc(v.version)} · 站点清单修订 ${v.registry_revision}</p></div><div class="policy-values">${kv('采样与确认',`${s.samples} 个一致成功样本 · ${s.confirm_windows} 个独立窗口`)}${kv('切换收益门槛',`至少 ${Math.round(s.min_gain_ratio*100)}% 且 ${s.min_gain_ms} ms`)}${kv('确认窗口间隔',`${s.window_gap_seconds} 秒`)}${kv('切换冷却时间',`${s.hold_seconds/60} 分钟`)}${kv('检测间隔',`${p.interval_seconds/60} 分钟`)}${kv('每域名原 IPv4 对照',`最多 ${p.baseline_addresses} 个地址，每个 ${s.samples} 次`)}${kv('本域名探索候选',`最多 ${s.candidates} 个 · 候选池上限 ${s.candidate_pool}`)}${kv('候选 TCP 初筛',`${s.candidate_pool} 个地址 · 最多 ${v.candidate_screen.max_seconds} 秒`)}${kv('初筛线程与探测',`${v.candidate_screen.threads} 线程 · 每地址 ${v.candidate_screen.tcp_attempts} 次 TCP`)}${kv('初筛保留',`最多 ${v.candidate_screen.ranked_limit} 个，仅作排序线索`)}${kv('单域名 / 整轮预算',`${s.domain_budget_seconds} / ${s.round_budget_seconds} 秒`)}${kv('域名并行数量',`${v.parallel_domains}`)}${kv('候选成功缓存上限',`${s.discovery_seconds/3600} 小时`)}${kv('优选最长有效期',`${p.max_age_seconds/3600} 小时`)}${kv('守护心跳判定',`${v.guard_stale_seconds} 秒`)}${kv('历史保留',`${p.history_rounds} 轮`)}</div></div>`;
  };
  const status=state.configuration_error?`<p class="notice warn" role="alert">${esc(state.configuration_error)}。以下保留最近的实际快照，不能据此认为新配置有效。</p>`:state.pending_next_round?'<p class="notice warn" role="status">已保存配置与实际执行快照不同。新配置等待下一轮生效，本轮仍按原参数执行。</p>':'<p class="notice">配置读取时间 '+fullTime(state.observed_at)+'。执行快照来自检测器，已保存配置来自当前配置文件。</p>';
  return `${status}${describe(actual,state.source==='running'?'本轮实际执行参数':'最近完成轮次的实际参数')}${(!actual||state.pending_next_round||state.configuration_error)?describe(desired,'已保存配置 · 待执行核对'):''}<section class="panel"><div class="panel-head"><div><h2>应用与恢复规则</h2><p>适用于当前主动优选引擎。域名访问、真实汇报与文件传输分别判断。</p></div></div><div class="speed-notes"><p>候选必须通过原域名 Host/SNI、真实证书与 HTTP 检查。错误页、限流及挑战页不作为速度赢家。</p><p>当前地址重新验证通过才能续期；样本不足或波动不会据此续期。已确认的连接或证书故障可撤回失效覆盖，其他未确认情况按现有有效期处理。</p><p>原线路已确定失败而候选稳定成功时，可按恢复连通分支处理；恢复可用不计算虚假的提速百分比。</p><p>有公网 IPv6 时纳入原路径对照；对照未确认则暂缓切换或续期。多 CDN 站点比较真实线路，不因解析来源不同就认定原线路失败。</p><p>站点与 Tracker 可在站点管理中维护。当前管理 ${data.domains.length} 个域名；已配置的排除规则保持生效。普通域名保留原解析并监测。</p></div></section>`;
}

// ---------------------------------------------------------------------------
// 稳定性趋势 + 耗时排行 + 变更时间线（V1.4）
// ---------------------------------------------------------------------------
function trendsView() {
  const trends = data.trends || {};
  // 按"最需要关注"排序：先看连续失败、再看成功率低、再看耗时长。
  const rows = Object.entries(trends)
    .filter(([, t]) => t.rounds)
    .map(([domain, t]) => ({domain, ...t}))
    .sort((a, b) => (b.streak_bad - a.streak_bad) || (a.rate - b.rate) || ((b.avg_ms || 0) - (a.avg_ms || 0)));
  const slowest = data.slowest || [];
  const timeline = data.timeline || [];
  const rateCell = t => {
    if (!t.rate && t.rate !== 0) return '<span class="muted">—</span>';
    const pct = Math.round(t.rate * 100);
    const tone = pct >= 90 ? 'good' : pct >= 50 ? 'warn' : 'bad';
    // 用预定义的宽度类表达百分比，避免内联 style（CSP 的 style-src 'self' 会拦截内联样式）。
    const bucket = Math.max(0, Math.min(100, Math.round(pct / 5) * 5));
    return `<span class="rate"><span class="bar"><i class="${tone} w${bucket}"></i></span>${pct}%</span>`;
  };
  const trendRow = t => {
    const flags = [];
    if (t.streak_bad >= 3) flags.push(`<span class="badge warn">连续 ${t.streak_bad} 轮异常</span>`);
    if (t.transitions >= 2) flags.push(`<span class="badge warn">切换过 ${t.transitions} 次</span>`);
    return `<tr>
      <td><span class="mono">${esc(t.domain)}</span><div class="muted small">${esc(siteName(t.domain))}</div></td>
      <td>${rateCell(t)}<div class="muted small">${t.healthy}/${t.rounds} 轮可用</div></td>
      <td class="num">${t.avg_ms != null ? t.avg_ms + ' ms' : '—'}<div class="muted small">峰值 ${t.max_ms != null ? t.max_ms + ' ms' : '—'}</div></td>
      <td>${esc((ACTIONS[t.last_action] || ['—'])[0])}${t.top_reason ? `<div class="muted small">${esc(t.top_reason)}</div>` : ''}</td>
      <td>${flags.join(' ') || '<span class="muted small">正常</span>'}</td>
    </tr>`;
  };
  const slowRows = slowest.map((s, i) => `<li><span class="rank">${i + 1}</span><span class="mono">${esc(s.domain)}</span><span class="num">${s.ms} ms</span><span class="muted small">${esc((ACTIONS[s.action] || ['—'])[0])}${s.reason ? ' · ' + esc(s.reason) : ''}</span></li>`).join('');
  const timelineRows = timeline.map(e => `<li class="${e.kind === 'change' ? 'change' : ''}"><time>${fullTime(e.at)}</time><span class="kind">${e.kind === 'change' ? '配置' : '检测'}</span><strong>${esc(e.title)}</strong><span class="muted small">${esc(e.detail)}</span></li>`).join('');
  return `<div class="stat-grid">
      <section class="panel"><div class="panel-head"><div><h2>域名稳定性（最近 10 轮）</h2><p>成功率低或连续失败排在最前，方便优先处理。</p></div><span class="small muted">${rows.length} 个域名</span></div>
      ${rows.length ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>域名</th><th>可用率</th><th>平均/峰值耗时</th><th>最近结果</th><th>提示</th></tr></thead><tbody>${rows.map(trendRow).join('')}</tbody></table></div>` : '<div class="empty">还没有足够的历史数据，先完成几轮检测。</div>'}
      </section>
      <section class="panel"><div class="panel-head"><div><h2>本轮耗时排行</h2><p>谁在拖慢整轮检测。耗时长通常意味着连接超时。</p></div></div>
      ${slowRows ? `<ol class="rank-list">${slowRows}</ol>` : '<div class="empty">本轮还没有耗时数据。</div>'}
      </section></div>
      <section class="panel"><div class="panel-head"><div><h2>变更与检测时间线</h2><p>把站点改动和检测结果放在一起，回答“我改完之后发生了什么”。</p></div></div>
      ${timelineRows ? `<ul class="timeline">${timelineRows}</ul>` : '<div class="empty">暂无记录。</div>'}
      </section>`;
}

// ---------------------------------------------------------------------------
// 运行日志（V1.4）
// ---------------------------------------------------------------------------
let logState = {loaded:false,level: '', keyword: '', code: '', loading: false, records: [], summary: null, error: ''};

async function loadLogs(quiet = false) {
  if (!quiet) { logState.loading = true; render(); }
  const params = new URLSearchParams();
  if (logState.level) params.set('level', logState.level);
  if (logState.keyword) params.set('q', logState.keyword);
  if (logState.code) params.set('code', logState.code);
  params.set('limit', '300');
  try {
    const r = await fetch('/api/log?' + params.toString(), {cache: 'no-store'});
    if (!r.ok) throw new Error('日志读取失败（' + r.status + '）');
    const payload = await r.json();
    logState.records = payload.records || [];
    logState.summary = payload.summary || null;
    logState.error = '';
  } catch (e) {
    logState.error = e.message;
    logState.records = [];
  } finally {
    logState.loading = false;logState.loaded=true;
    if (view === 'logs') render();
  }
}

const LEVEL_LABEL = {debug: '调试', info: '信息', warn: '警告', error: '错误'};

function logsView() {
  if (!logState.loaded && !logState.loading) {
    logState.loading = true; // Mark scheduled before the timer, preventing duplicate initial requests.
    setTimeout(() => loadLogs(), 0);
  }
  const summary = logState.summary;
  const levelOptions = [['', '全部级别'], ['info', '信息及以上'], ['warn', '只看警告和错误'], ['error', '只看错误']];
  const rows = logState.records.map(item => {
    const detail = item.detail ? `<details class="log-detail"><summary>详情</summary><pre>${esc(JSON.stringify(item.detail, null, 2))}</pre></details>` : '';
    return `<li class="log ${item.level}"><time>${fullTime(item.at)}</time><span class="log-level ${item.level}">${LEVEL_LABEL[item.level] || item.level}</span><span class="log-code mono">${esc(item.code || '')}</span><span class="log-msg">${esc(item.message)}</span>${detail}</li>`;
  }).join('');
  const codes = summary ? Object.entries(summary.by_code || {}).map(([code, n]) => `<button class="chip ${logState.code === code ? 'on' : ''}" data-logcode="${esc(code)}">${esc(code)} · ${n}</button>`).join('') : '';
  return `<div class="toolbar log-toolbar">
      <select id="logLevel" aria-label="按级别筛选日志">${levelOptions.map(([v, l]) => `<option value="${v}" ${logState.level === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <input id="logSearch" type="search" aria-label="搜索日志" placeholder="搜索关键字，例如域名或失败原因" value="${esc(logState.keyword)}">
      <button id="logReload">刷新日志</button>
      <button id="logExport">导出日志</button>
      <a class="button-like" href="/api/diagnostics" download>导出诊断包</a>
    </div>
    ${summary ? `<div class="log-summary"><span>按级别：${Object.entries(summary.by_level || {}).map(([k, v]) => `${LEVEL_LABEL[k] || k} ${v}`).join(' · ') || '—'}</span>${codes ? `<span class="chips">${codes}</span>` : ''}</div>` : ''}
    <section class="panel"><div class="panel-head"><h2>运行日志</h2><span class="small muted">新 → 旧 · 最多 300 条${logState.loading ? ' · 加载中…' : ''}</span></div>
    ${logState.error ? `<div class="error-box">${esc(logState.error)}</div>` : ''}
    ${rows ? `<ul class="log-list">${rows}</ul>` : (logState.loading ? '' : '<div class="empty">没有符合条件的日志。可调整筛选或稍后刷新，无需为此启动检测。</div>')}
    </section>
    <p class="small muted">日志会进行脱敏处理。诊断包仍可能含站点域名、网络地址及运行记录；仅在需要排障时导出，分享前检查内容。</p>`;
}

// ---------------------------------------------------------------------------
// 版本更新（V1.4）
// ---------------------------------------------------------------------------
let updateState = {status: null, loading: false, message: '', error: '', downloading: false};

async function loadUpdate() {
  updateState.loading = true; render();
  try {
    const r = await fetch('/api/update', {cache: 'no-store'});
    updateState.status = await r.json();
    updateState.error = '';
  } catch (e) {
    updateState.error = e.message;
  } finally {
    updateState.loading = false;
    if (view === 'update') render();
  }
}

function updateView() {
  const s = updateState.status;
  if (!s && !updateState.loading && !updateState.error) setTimeout(() => loadUpdate(), 0);
  const stateTone = !s ? '' : s.state === 'update_available' ? 'warn' : s.state === 'error' ? 'bad' : 'good';
  const stateText = !s ? '读取中…' : ({
    idle: '未启用', up_to_date: '已是最新版本', update_available: '有新版本', error: '检查失败'
  })[s.state] || s.state;
  const assets = (s && s.assets || []).map(a => `<li><span class="mono">${esc(a.name)}</span><span class="muted small">${a.size ? Math.round(a.size / 1024) + ' KB' : ''}${a.sha256 ? ' · 提供 SHA256' : ' · 无 SHA256'}</span></li>`).join('');
  return `<section class="panel">
      <div class="panel-head"><div><h2>版本更新</h2><p>检查 GitHub 发布的新版本，下载并校验。替换与重启需要您在绿联界面完成。</p></div><span class="badge ${stateTone}">${esc(stateText)}</span></div>
      <div class="kv-grid">
        <div class="kv"><span>当前版本</span><strong class="mono">v${esc(s?.current || data.version || '—')}</strong></div>
        <div class="kv"><span>远程最新</span><strong class="mono">${s?.latest ? 'v' + esc(s.latest) : '—'}</strong></div>
        <div class="kv"><span>仓库</span><strong>${s?.repo ? esc(s.repo) : '未配置'}</strong></div>
        <div class="kv"><span>私有仓令牌</span><strong>${s?.token_configured ? '已配置' : '未配置'}</strong></div>
      </div>
      ${s?.message ? `<p class="notice">${esc(s.message)}</p>` : ''}
      ${updateState.error ? `<div class="error-box">${esc(updateState.error)}</div>` : ''}
      ${assets ? `<div class="panel-head"><h2>可选文件</h2></div><ul class="asset-list">${assets}</ul>` : ''}
      ${updateState.message ? `<p class="notice">${esc(updateState.message)}</p>` : ''}
      <div class="dialog-actions left">
        <button id="updateCheck" ${updateState.loading ? 'disabled' : ''}>${updateState.loading ? '检查中…' : '再次检查'}</button>
        <button id="updateDownload" class="primary" ${(!s || s.state !== 'update_available' || updateState.downloading) ? 'disabled' : ''}>${updateState.downloading ? '下载中…' : '下载并校验'}</button>
      </div>
      <div class="steps-guide">
        <h3>下载之后怎么做</h3>
        <ol>
          <li>下载完成后，文件在 NAS 的 <span class="mono">部署目录下的 runtime/control/updates/</span>。</li>
          <li>先按随版本提供的部署说明，将校验通过的新代码安装到项目构建目录，保留 config/ 和 runtime/；仅下载压缩包不会更新运行代码。然后在绿联「Docker → 项目」里重建镜像，再重启 <span class="mono">nas-domain-optimizer</span> 与 <span class="mono">nas-domain-optimizer-ui</span> 两个容器。</li>
          <li>回到页面看底部版本号是否变成新版本。</li>
        </ol>
        <p class="small muted">本功能不会自动替换运行中的文件，也不会自动重启容器——那会让服务在失败时直接停掉。</p>
      </div>
    </section>
    <section class="panel"><div class="panel-head"><div><h2>如何启用</h2><p>在 NAS 的 config/settings.json 里加一段，然后重启管理页面容器。</p></div></div>
      <pre class="code">"update": {
  "enabled": true,
  "repo": "你的用户名/仓库名",
  "asset": "nas-domain-optimizer.tar.gz"
}</pre>
      <p class="small muted">私有仓库需要令牌：在 NAS 上用环境变量 <span class="mono">GITHUB_TOKEN</span> 提供（compose 已支持透传），不要写进 settings.json，也不要写进任何交付文件。</p>
    </section>`;
}

// ---------------------------------------------------------------------------
// 使用帮助（V1.4）
// ---------------------------------------------------------------------------
function helpView() {
  const step = (n, title, body) => `<div class="help-step"><span class="step-no">${n}</span><div><h3>${title}</h3><p>${body}</p></div></div>`;
  return `<section class="panel help">
    <div class="panel-head"><div><h2>快速上手</h2><p>第一次用只需要三步。</p></div></div>
    ${step(1, '添加站点', '进入「站点」，点右上角「添加站点」，填站点名称；<b>站点首页域名</b>和 <b>Tracker 域名</b>至少填一项，多个域名每行一个。只填主机名，例如 <span class="mono">tracker.example.org</span>，<b>不要</b>粘贴带 passkey 的完整汇报链接。')}
    ${step(2, '等待自动检测', '保存后会自动排队检测，总览页能看到进度。一个域名要经过「公开 DNS 确认 → 原连接检查 → 必要时验证候选地址」三步。')}
    ${step(3, '看结果', '在「站点」中选择一个站点，先看实际汇报，再按需展开域名检测详情和本站策略。')}

    <div class="panel-head"><div><h2>看懂几种状态</h2><p>同一句话在不同分类下含义不同。</p></div></div>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>你看到的</th><th>含义</th><th>要不要处理</th></tr></thead><tbody>
      <tr><td>正在使用优选</td><td>该域名已切换到验证过的 Cloudflare 地址</td><td>不用，这是期望状态</td></tr>
      <tr><td>原解析正常</td><td>原线路可用，候选尚未达到收益门槛</td><td>继续自动对照；可用的原线路也会参与选优</td></tr>
      <tr><td>原连接未通过</td><td>本轮根请求未稳定通过；普通线路保留原解析</td><td>结合详情和自然汇报分别判断</td></tr>
      <tr><td>域名无 IPv4 解析</td><td>DNS 查不到 IPv4 地址</td><td>确认域名是否换了或停用</td></tr>
      <tr><td>连接有波动</td><td>本轮样本有失败或结果不一致</td><td>观察趋势页，并核对自然汇报是否同样异常</td></tr>
      <tr><td>没有可用候选</td><td>试了候选地址但都没通过验证</td><td>通常是线路问题，等下一轮</td></tr>
    </tbody></table></div>

    <div class="panel-head"><div><h2>失败原因怎么看</h2><p>原因会告诉你卡在哪一步，这决定了要不要动手。</p></div></div>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>阶段</th><th>常见原因</th><th>通常意味着</th></tr></thead><tbody>
      <tr><td><b>TCP 连接</b></td><td>端口主动拒绝 / 连接超时 / 域名解析失败</td><td>站点没开 HTTPS、被墙、或域名已失效</td></tr>
      <tr><td><b>TLS 握手</b></td><td>证书过期 / 主机名不匹配 / 自签名 / 证书链不完整</td><td>站点证书问题，或该地址不对应这个域名</td></tr>
      <tr><td><b>HTTP 响应</b></td><td>没有返回 HTTP 响应 / 返回内容异常</td><td>服务器活着但没正常应答</td></tr>
    </tbody></table></div>
    <p class="notice">注意：根路径的 HTTP 检查成功，只说明「TLS 通了、服务器有应答」，<b>不等于</b> Tracker 汇报真的成功。真正汇报还取决于账号状态、passkey 和站点服务器。</p>

    <div class="panel-head"><div><h2>常见问题</h2></div></div>
    <details class="faq"><summary>页面显示「自动检测服务失联」</summary><p>检测容器停了或卡住。先去绿联里看 <span class="mono">nas-domain-optimizer</span> 容器状态；再看「运行日志」页筛「只看警告和错误」。</p></details>
    <details class="faq"><summary>优选地址没生效</summary><p>依次核对该域名是否显示「优选地址已应用」或「优选已复测」、发布地址及其有效期，以及实际下载器解析结果。候选通过一次检测不等于已经发布，收益确认窗口和应用模式同样需要满足。</p></details>
    <details class="faq"><summary>保存站点失败，提示域名已属于别的站点</summary><p>一个域名只能属于一个站点。请去那个站点里编辑，而不是重复添加。提示里会写明是哪个域名、哪个站点。</p></details>
    <details class="faq"><summary>为什么有的域名不套 Cloudflare 地址</summary><p>只有公开 DNS 确认是 Cloudflare 的域名才参与优选；普通线路只监控连接，不会套任何地址。</p></details>
    <details class="faq"><summary>移除站点会影响下载器吗</summary><p>不会。这里只管理网络检测与优选记录，不改下载器任务、源文件或做种状态。移除后对应的优选映射会被撤回。</p></details>
    <details class="faq"><summary>出问题了怎么最快找到原因</summary><p>①「运行日志」页筛警告和错误，搜域名；②「稳定性趋势」页看是不是反复失败；③ 点「导出诊断包」把日志+配置+最近几轮结果打包发出来。</p></details>

    <div class="panel-head"><div><h2>这个工具不会做的事</h2></div></div>
    <ul class="plain-list">
      <li>不启停、删除或校验下载器任务，不改源文件、目录或标签；传输验收通过种子港进行 QB 单任务减额或两个 TR 任务的共享额度试行，临时设置均有超时恢复</li>
      <li>不触发文件校验、迁移或重新下载</li>
      <li>不保存账号密码、Cookie 或 passkey</li>
      <li>不会把带密钥的完整汇报地址存下来（会被拒绝并说明原因）</li>
      <li>已配置的排除规则保持生效</li>
    </ul>
  </section>`;
}


// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function render() {
  const titles = {domains:'域名连接详情',transfer:'实际传输',diagnosis:'诊断详情',changes:'操作记录',advanced:'高级诊断与试验',acceptance:'传输验收',health:'站点健康与策略',reports:'周期报告',speed:'优选效果', overview:'总览', sites:'站点', history:'检测与切换记录',
                  trends:'稳定性趋势', logs:'运行日志', policy:'设置',
                  update:'版本更新', help:'使用帮助'};
  $('#title').textContent = titles[view];
  $('#breadcrumb').textContent = titles[view];
  document.querySelectorAll('nav a').forEach(a => {
    if (a.dataset.view === navGroup(view)) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('#nasHost').textContent=location.hostname;
  if (!data) return;
  if (!selected || !data.domains.includes(selected)) selected = data.domains[0];
  $('#serviceLabel').textContent = statusUnavailable?'状态读取失败':data.worker_healthy ? '自动检测服务在线' : '自动检测服务失联';
  $('#serviceDot').className = 'dot ' + (!statusUnavailable&&data.worker_healthy ? 'good' : 'bad');
  $('#finding').textContent = view === 'overview'
    ? (!data.worker_healthy ? '服务心跳已过期，当前状态需要检查。'
      : data.probing ? `正在检测全部已启用域名，已完成 ${data.progress.completed||0} / ${data.progress.total||0} 个。`
      : `共 ${data.sites.length} 个站点、${data.domains.length} 个域名，当前 ${Object.keys(data.published).length} 个域名使用优选。`)
    : view === 'acceptance' ? '限定任务、短时对照、自动恢复；对照不合格时保留原因。'
    : view === 'health' ? '站点访问、自然汇报与实际传输分别判断；策略可按站点收紧。'
    : view === 'reports' ? '核对24小时和7天证据覆盖，以及真实上行和预算的待验收条件。'
    : view === 'sites' ? '自行维护站点首页与 Tracker 域名，保存后自动识别和检测。'
    : view === 'history' ? '每次检测和优选决定，都有实际结果可查。'
    : view === 'speed' ? '对照原线路和候选地址，并结合实际下载、上传与汇报数据判断收益。'
    : view === 'trends' ? '看一个域名是“一直稳偶尔抖”还是“反复失败”，单轮结果看不出来。'
    : view === 'logs' ? '出问题时先看这里：按级别筛 warning/error，或搜索域名。'
    : view === 'update' ? '检查新版本并下载校验；替换与重启由您在绿联界面完成。'
    : view === 'help' ? '第一次用请先看“快速上手”。'
    : '当前自动运行规则，按已部署配置读取。';
  $('#probe').disabled = statusUnavailable || busy || !data.worker_healthy || data.probing || data.pending || data.now - data.last_requested_at < 60;
  $('#probe').textContent = statusUnavailable?'状态不可用':busy ? '正在提交…' : data.probing ? '检测中…' : data.pending ? '等待开始…' : '检测全部域名';
  $('#addSite').disabled=statusUnavailable;
  $('#saveSite').disabled=statusUnavailable;
  $('#sync').textContent = (statusUnavailable?'离线 · 上次更新 ':'更新于 ') + time(data.now) + (data.version ? ` · v${data.version}` : '');
  $('#addSite').hidden = view !== 'sites';
  $('#probe').hidden = !['overview','domains'].includes(view);
  if(view==='sites') $('#finding').textContent='集中查看汇报、连接与域名；编辑只影响优选管理。';
  if(view==='speed') $('#finding').textContent='先看连接改善，文件传输收益单独判断。';
  if(view==='transfer') $('#finding').textContent='读取现有下载器流量，不启动下载或改变额度。';
  if(view==='diagnosis') $('#finding').textContent='按需查看判断依据、采样时间和未确认事项。';
  if(view==='domains') $('#finding').textContent='查看公开连接检测详情；实际汇报请到对应站点查看。';
  if(view==='advanced') $('#finding').textContent='仅在主动操作时启动试验；打开页面不会运行测试。';
  if(view==='changes') $('#finding').textContent='查看配置保存及人工操作的历史结果。';
  if(view==='reports') $('#finding').textContent='按已有数据汇总；覆盖不足如实显示，不启动补样试验。';
  if(statusUnavailable)$('#finding').textContent='当前无法读取 NAS 状态，以下保留上次记录，请刷新后再判断。';
  const input = document.activeElement, focusId = input?.id, position = input?.selectionStart;
  const openDetails=[...document.querySelectorAll('#view details[id][open]')].map(x=>x.id);
  $('#view').innerHTML = subNavigation() + (view === 'domains' ? overview()
    : view === 'transfer' ? speedView('transfer')
    : view === 'diagnosis' ? bottleneckView()
    : view === 'changes' ? changesView()
    : view === 'advanced' ? advancedView()
    : view === 'acceptance' ? acceptanceView()
    : view === 'health' ? siteWorkspace()
    : view === 'reports' ? reportView(false)
    : view === 'speed' ? speedView()
    : view === 'sites' ? siteWorkspace()
    : view === 'history' ? historyView()
    : view === 'trends' ? trendsView()
    : view === 'logs' ? logsView()
    : view === 'policy' ? speedView('settings') + '<details id=effectiveRules class=disclosure><summary>查看实际生效规则与恢复保护</summary>' + policy() + '</details>'
    : view === 'update' ? updateView()
    : view === 'help' ? helpView()
    : homeView());
  document.querySelectorAll('[data-save-policy]').forEach(b=>{if(sitePolicyBusy.has(b.dataset.savePolicy))b.disabled=true;});
  if(statusUnavailable)document.querySelectorAll('#view [data-save-policy],#speedPolicyForm button,#view [data-toggle],#view [data-remove]').forEach(b=>b.disabled=true);
  openDetails.forEach(id=>{const el=document.getElementById(id);if(el)el.open=true;});
  if (focusId && (['siteSearch','domainSearch','logSearch','canaryDomains','speedMode','siteFilter','domainCategory'].includes(focusId) || focusId.startsWith('sitePolicy-'))) {
    const x = document.getElementById(focusId); x?.focus();
    try { x?.setSelectionRange(position, position); } catch {}
  }
}

async function refresh(automatic=false) {
  if(automatic && interacting()){clearTimeout(timer);timer=setTimeout(()=>refresh(true),1500);return;}
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    if (!r.ok) throw new Error('无法读取服务状态（' + r.status + '）');
    data = await r.json(); const recovered=statusUnavailable;statusUnavailable=false;
    if(recovered && $('#message').textContent.startsWith('读取失败：'))message('连接已恢复，状态已更新。');
    if (pendingRequest && data.manual?.id === pendingRequest && ['complete','failed'].includes(data.manual.status)) {
      message(data.manual.status === 'complete'
        ? '本次手动检测已完成，结果更新于 ' + fullTime(data.latest.finished_at) + '。'
        : '本次手动检测未完成' + (data.manual.reason ? '：' + data.manual.reason : '') + '。',
        data.manual.status === 'failed');
      pendingRequest = '';
    }
    if(!automatic || !interacting())render();
  } catch (e) {
    statusUnavailable=true; if(data)render();
    $('#sync').textContent = '连接失败'; $('#probe').disabled = true;
    message('读取失败：' + e.message + '。请检查 NAS 网络后点击刷新。', true);
    if (!data) $('#view').innerHTML = '<div class="error-box">暂时无法读取 NAS 状态，已有检测和做种任务不会因此停止。</div>';
  } finally {
    clearTimeout(timer);
    timer = setTimeout(()=>refresh(true), data?.probing || data?.pending ? 2000 : 10000);
  }
}

// ---------------------------------------------------------------------------
// 交互绑定（全部在脚本顶层绑定站点级按钮；视图内元素用事件委托）
// ---------------------------------------------------------------------------
$('#refresh').addEventListener('click', () => { message(''); refresh(); });
$('#probe').addEventListener('click', async () => {
  if (!data || busy || statusUnavailable) return;
  busy = true; render();
  try {
    const r = await fetch('/api/probe', {method:'POST', headers:{'X-CSRF-Token':data.csrf}});
    const result = await r.json();
    if (!r.ok) throw new Error(result.error || '请求未完成');
    pendingRequest = result.id;
    message(result.message + '，检测结束后自动显示结果。');
  } catch (e) { message(e.message, true); }
  finally { busy = false; await refresh(); }
});
$('#menu').addEventListener('click', () => {
  const open = $('#navigation').classList.toggle('open');
  $('#menu').setAttribute('aria-expanded', String(open));
});

// 视图内点击：域名详情、站点编辑/启停/移除、清除筛选
$('#view').addEventListener('click', async e => {
  const domainBtn = e.target.closest('[data-domain]');
  if (domainBtn) { selected = domainBtn.dataset.domain; writeStateToUrl(true); render(); document.getElementById('domainDetail')?.scrollIntoView({block:'nearest'}); return; }
  if (e.target.id === 'clearFilters') {
    query = ''; category = 'all'; writeStateToUrl(); render(); return;
  }
  const edit = e.target.closest('[data-edit]');
  const toggle = e.target.closest('[data-toggle]');
  const remove = e.target.closest('[data-remove]');
  if (edit) openSite(edit.dataset.edit);
  if (toggle) {
    const s = data.sites.find(x => x.id === toggle.dataset.toggle);
    if (!s) return;
    try { const r = await siteRequest({...s, enabled: !s.enabled}); message(r.message); await refresh(); }
    catch (err) { message(err.message, true); }
  }
  if (remove) {
    removingId = remove.dataset.remove;
    const s = data.sites.find(x => x.id === removingId);
    $('#removeDescription').textContent = '从域名优选管理中移除“' + s.name + '”？';
    $('#removeDialog').showModal();
  }
});

// 视图内输入：搜索（同步到 URL，切换页面不丢）
$('#view').addEventListener('input', e => {
  if (e.target.id === 'domainSearch') { query = e.target.value; writeStateToUrl(true); render(); }
  if (e.target.id === 'siteSearch') { siteQuery = e.target.value; writeStateToUrl(true); render(); }
});
$('#view').addEventListener('change', e => {
  if(e.target.id==='siteFilter'){siteFilter=e.target.value;writeStateToUrl(true);render();}
  if (e.target.id === 'domainCategory') { category = e.target.value; writeStateToUrl(); render(); }
  if (e.target.id === 'historyFilter') { filter = e.target.value; writeStateToUrl(); render(); }
});

// ---------------------------------------------------------------------------
// 站点表单
// ---------------------------------------------------------------------------
async function siteRequest(payload) {
  if(statusUnavailable)throw Error('状态读取失败，请先刷新成功后再保存。');
  const r = await fetch('/api/sites', {method:'POST', headers:{'X-CSRF-Token':data.csrf,'Content-Type':'application/json'},
    body: JSON.stringify({...payload, revision:data.registry_revision})});
  const result = await r.json();
  if (!r.ok) throw new Error(result.error || '保存未完成');
  return result;
}

function openSite(id = '') {
  if (!data) return;
  const s = data.sites.find(x => x.id === id);
  $('#siteId').value = s?.id || '';
  $('#siteName').value = s?.name || '';
  $('#websiteDomains').value = s?.website_domains.join('\n') || '';
  $('#trackerDomains').value = s?.tracker_domains.join('\n') || '';
  $('#siteEnabled').checked = s?.enabled ?? true;
  $('#formError').textContent = ''; $('#domainFeedback').textContent='';
  $('#siteDialogTitle').textContent = s ? '编辑站点' : '添加站点';
  $('#siteDialog').showModal();
  $('#siteName').focus();
}

$('#addSite').addEventListener('click', () => openSite());
$('#closeSiteDialog').addEventListener('click', () => $('#siteDialog').close());
$('#cancelSite').addEventListener('click', () => $('#siteDialog').close());
$('#siteForm').addEventListener('submit', async e => {
  e.preventDefault();
  $('#saveSite').disabled = true; $('#formError').textContent = '';
  try {
    const normalized=validateSiteFields(),adding=!$('#siteId').value,savedName=$('#siteName').value.trim();
    const result = await siteRequest({
      id: $('#siteId').value || undefined,
      name: $('#siteName').value,
      website_domains: normalized.websites,
      tracker_domains: normalized.trackers,
      enabled: $('#siteEnabled').checked
    });
    if(adding){view='sites';siteId='';siteQuery=savedName;siteFilter='all';writeStateToUrl();}
    $('#siteDialog').close(); message(result.message+'。优选管理清单已保存；检测结果稍后更新，下载器 Tracker 地址未改动。'); await refresh();
  } catch (err) { $('#formError').textContent = err.message; }
  finally { $('#saveSite').disabled = false; }
});

$('#cancelRemove').addEventListener('click', () => $('#removeDialog').close());
$('#confirmRemove').addEventListener('click', async () => {
  $('#confirmRemove').disabled = true;
  try {
    const r = await siteRequest({operation:'remove', id:removingId});
    $('#removeDialog').close(); message(r.message); await refresh();
  } catch (e) { $('#formError').textContent = ''; message(e.message, true); $('#removeDialog').close(); }
  finally { $('#confirmRemove').disabled = false; }
});

// ---------------------------------------------------------------------------
// 路由：hash 里同时保存视图与筛选条件
// ---------------------------------------------------------------------------
// 导航：切视图时保留筛选条件。
// 直接点 <a href="#history"> 会把整个 hash 换成 "#history"，筛选参数随之丢失，
// 所以这里拦截点击，自己改写 URL 再渲染。
document.querySelectorAll('nav a[data-view]').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    if(view==='sites'&&!siteId)siteListScroll=window.scrollY;
    view = link.dataset.view; siteId='';message('');
    $('#navigation').classList.remove('open');
    $('#menu').setAttribute('aria-expanded', 'false');
    writeStateToUrl();
    render();window.scrollTo(0,view==='sites'?siteListScroll:0);
  });
});

function route() {
  // 浏览器前进/后退始终以 URL 为准。
  suppressHashChange = null;
  readStateFromUrl(); message('');
  $('#navigation').classList.remove('open');
  $('#menu').setAttribute('aria-expanded', 'false');
  render();window.scrollTo(0,view==='sites'&&!siteId?siteListScroll:0);
}
window.addEventListener('hashchange', route);
window.addEventListener('popstate', route);

// ---------------------------------------------------------------------------
// 日志页与更新页的交互（V1.4）
// ---------------------------------------------------------------------------
$('#view').addEventListener('change', e => {
  if (e.target.id === 'logLevel') { logState.level = e.target.value; loadLogs(); }
});
$('#view').addEventListener('click', async e => {
  if (e.target.id === 'logReload') { loadLogs(); return; }
  if (e.target.id === 'logExport') {
    const params = new URLSearchParams();
    if (logState.level) params.set('level', logState.level);
    if (logState.keyword) params.set('q', logState.keyword);
    params.set('limit', '500');
    // 日志导出用 GET 下载，浏览器按 Content-Disposition 处理；这里直接打开让浏览器下载。
    window.open('/api/log?' + params.toString() + '&download=1', '_blank');
    return;
  }
  const codeChip = e.target.closest('[data-logcode]');
  if (codeChip) {
    logState.code = logState.code === codeChip.dataset.logcode ? '' : codeChip.dataset.logcode;
    loadLogs();
    return;
  }
  if (e.target.id === 'updateCheck') { loadUpdate(); return; }
  if (e.target.id === 'updateDownload') {
    updateState.downloading = true; updateState.message = ''; render();
    try {
      const r = await fetch('/api/update', {
        method: 'POST',
        headers: {'X-CSRF-Token': data.csrf, 'Content-Type': 'application/json'},
        body: JSON.stringify({})
      });
      const result = await r.json();
      updateState.message = result.ok ? `更新包已下载${result.verified ? '并校验通过' : '，尚未校验'}，尚未安装。请按版本部署说明安装代码，保留 config/ 与 runtime/，再重建镜像和两个优选容器。文件：${result.path || '见部署说明'}` : '下载未完成：' + (result.message || '');
    } catch (err) {
      updateState.message = '下载未完成：' + err.message;
    } finally {
      updateState.downloading = false;
      if (view === 'update') render();
    }
  }
});

// 日志搜索：输入即筛，带防抖避免每敲一个字都请求。
let logTimer = null;
$('#view').addEventListener('input', e => {
  if (e.target.id !== 'logSearch') return;
  logState.keyword = e.target.value;
  clearTimeout(logTimer);
  logTimer = setTimeout(() => loadLogs(true), 350);
});

// Presentation only: reuse the existing status snapshot and mutation handlers.
document.addEventListener('click',e=>{if(e.target.closest('a[href^="#"]')&&view==='sites'&&!siteId)siteListScroll=window.scrollY;});
function navGroup(v){
  if(['sites','health','domains'].includes(v))return 'sites';
  if(['speed','transfer','diagnosis','reports','trends'].includes(v))return 'speed';
  if(['history','changes','logs'].includes(v))return 'history';
  if(['policy','advanced','acceptance','update','help'].includes(v))return 'policy';
  return 'overview';
}
function subNavigation(){
 const group=navGroup(view),tabs={
  sites:[['sites','站点列表'],['domains','域名连接详情']],
  speed:[['speed','连接改善'],['transfer','实际传输'],['diagnosis','诊断详情'],['reports','周期报告'],['trends','稳定性趋势']],
  history:[['history','检测与切换'],['changes','操作记录'],['logs','运行日志']],
  policy:[['policy','优选设置'],['advanced','高级诊断与试验'],['update','版本更新'],['help','使用帮助']]
 };
 return tabs[group]?`<div class="section-tabs" role="navigation" aria-label="页内导航">${tabs[group].map(([v,n])=>`<a href="#${v}" ${view===v||v==='sites'&&view==='health'||v==='advanced'&&view==='acceptance'?'aria-current="page"':''}>${n}</a>`).join('')}</div>`:'';
}
function healthFor(s){return (data.site_health||[]).find(h=>h.id===s.id)||{id:s.id,name:s.name,enabled:s.enabled,domains:new Set([...s.website_domains,...s.tracker_domains]).size,tracker:{}};}
function trackerState(h){
 const t=h.tracker||{};
 if(h.enabled===false)return {label:'已停用',tone:'',kind:'disabled'};
 if(!t.records)return {label:'暂无汇报数据',tone:'',kind:'unknown'};
 if(!t.fresh)return {label:'汇报数据已过期',tone:'warn',kind:'stale'};
 if(t.failed)return {label:`${t.failed} 条汇报失败`,tone:'bad',kind:'failed'};
 if(t.working)return {label:t.pending?'已观测汇报成功，另有等待':'已观测汇报正常',tone:'good',kind:'working'};
 return {label:'等待自然汇报',tone:'',kind:'pending'};
}
function connectionState(h){
 if(!h.enabled)return '已停用';
 if(!h.https_at)return '尚未检测';
 if(!h.https_fresh)return '连接检测已过期';
 return h.https_healthy===h.domains?'公开连接检测通过':`${Math.max(0,(h.domains||0)-(h.https_healthy||0))} 个域名连接待观察`;
}
function healthAdvice(h){
 const st=trackerState(h);
 if(st.kind==='disabled')return '已停用优选管理；下载器任务仍保持原状态。';
 if(st.kind==='stale')return '先等待观测更新，再判断是否需要处理。';
 if(st.kind==='failed')return '查看具体错误及备用地址；失败记录不等于整个站点不可用。';
 if(st.kind==='working')return h.https_healthy===h.domains?'已观测记录未见汇报失败，保持当前策略。':'汇报仍有成功记录；公开连接检测未通过，暂不据此更换 Tracker。';
 return '没有足够的实际汇报记录，不能判断做种汇报正常或失败。';
}
function siteLink(id){const p=new URLSearchParams();p.set('site',id);if(siteQuery)p.set('sq',siteQuery);if(siteFilter!=='all')p.set('sf',siteFilter);return '#sites?'+p;}
function trackerCell(h){const t=h.tracker||{},st=trackerState(h);return `<span class="badge ${st.tone}">${esc(st.label)}</span><div class="small muted">${t.records?`成功 ${t.working||0} · 失败 ${t.failed||0} · 等待 ${t.pending||0}`:'未采到不代表站点故障'}</div>`;}
function homeView(){
 const health=data.site_health||[],enabled=health.filter(x=>x.enabled),fails=enabled.filter(x=>trackerState(x).kind==='failed'),connections=enabled.filter(x=>x.https_fresh&&x.https_healthy<x.domains),unknown=enabled.filter(x=>['unknown','stale','pending'].includes(trackerState(x).kind));
 fails.sort((a,b)=>(b.tracker?.failed||0)-(a.tracker?.failed||0));
 const ordered=[...fails,...connections.filter(x=>!fails.includes(x))];
 const otherAlerts=(data.alerts||[]).filter(x=>x.code!=='domains.unhealthy'&&x.id!=='domains.unhealthy'&&!String(x.message).includes('域名本轮未稳定通过'));
 const healthy=!statusUnavailable&&data.worker_healthy&&!data.config_error&&!data.runtime_policy?.configuration_error;
 const state=statusUnavailable?'状态读取失败 · 显示上次记录':!healthy?'服务状态需要检查':data.probing?'服务正常 · 正在例行检测':data.pending?'服务正常 · 检测已排队':'优选服务运行正常';
 return `<section class="service-strip ${healthy?'':'is-error'}" aria-label="服务状态"><div><span class="dot ${healthy?'good':'bad'}"></span><strong>${state}</strong><p>最近完成 ${fullTime(data.latest.finished_at)} · 下次检测 ${time(data.next_run)}${data.probing?` · 已完成 ${data.progress?.completed||0}/${data.progress?.total||0}`:''}</p></div><a href="#policy">查看运行设置</a></section>
 <div class="coverage-line"><span><strong>${data.sites.length}</strong> 个站点</span><span><strong>${data.domains.length}</strong> 个域名</span><span><strong>${Object.keys(data.published||{}).length}</strong> 个正在使用优选</span><a href="#sites">管理站点</a></div>
 ${otherAlerts.length?`<section class="system-notices" aria-label="系统异常">${otherAlerts.map(x=>`<p><strong>${esc(x.message)}</strong><br><span class="small muted">证据时间 ${fullTime(x.evidence_at)}</span></p>`).join('')}</section>`:''}
 <section class="work-section"><div class="section-heading"><div><h2>需要留意的站点</h2><p>汇报错误与连接检测分开看，先判断是否影响实际使用。</p></div><a href="#sites">查看全部站点</a></div><div class="attention-counts"><span>存在汇报失败 <strong>${fails.length}</strong> 站</span><span>连接待观察 <strong>${connections.length}</strong> 站</span><span>汇报证据不足 <strong>${unknown.length}</strong> 站</span></div><p class="small muted">按站点计数；同一站点可能同时出现在不同类别，不能相加。</p>
 <div class="attention-list">${ordered.slice(0,5).map(h=>`<article class="attention-row"><div><a class="site-link" href="${esc(siteLink(h.id))}">${esc(h.name)}</a><div class="small muted">${esc(connectionState(h))}</div></div><div>${trackerCell(h)}<p>${esc(healthAdvice(h))}</p></div><a class="text-action" href="${esc(siteLink(h.id))}">查看详情</a></article>`).join('')||'<p class="empty">当前未发现有证据支持的汇报错误或连接异常。暂无数据的站点另列，不视为正常。</p>'}</div>${ordered.length>5?`<a class="more-link" href="#sites?sf=attention">还有 ${ordered.length-5} 个站点待查看</a>`:''}</section>
 <section class="work-section"><div class="section-heading"><div><h2>优选起到了什么作用</h2><p>连接改善与文件传输收益分别判断。</p></div><a href="#speed">查看优选效果</a></div>${connectionSummary()}</section>`;
}
function connectionSummary(){
 const rows=Object.values(data.latest?.domains||{}),compared=rows.filter(r=>Object.keys(r.speed?.metrics||{}).length).length;
 const restored=rows.filter(r=>r.speed?.original_ms==null&&r.speed?.current_ms!=null&&r.speed?.applied).length;
 const gains=rows.filter(r=>r.speed?.proposed_ip&&r.speed?.gain_ms>0).length;
 return `<div class="effect-summary"><p><strong>${compared} 个域名已有连接对照</strong> · ${restored} 个原路径未通过、当前优选可用 · ${gains} 个候选存在改善机会</p><p class="muted">候选达到现有门槛后才应用。文件下载与做种上传提速：<strong>尚无足够证据确认</strong>。</p><span class="small muted">连接数据 ${fullTime(data.latest.finished_at)}；连接耗时的百分比不代表文件传输提速。</span></div>`;
}
function siteWorkspace(){
 const current=data.sites.find(s=>s.id===siteId);
 if(siteId && !current)return '<div class="empty">此站点已不在当前清单。<a href="#sites">返回站点列表</a></div>';
 if(current)return siteDetailView(current);
 const rows=data.sites.filter(s=>{
  if(siteQuery&&!(s.name+' '+s.website_domains.join(' ')+' '+s.tracker_domains.join(' ')).toLowerCase().includes(siteQuery.toLowerCase()))return false;
  const h=healthFor(s),k=trackerState(h).kind;
  if(siteFilter==='failed')return k==='failed';
  if(siteFilter==='connection')return h.enabled&&h.https_fresh&&h.https_healthy<h.domains;
  if(siteFilter==='unknown')return ['unknown','pending','stale'].includes(k);
  if(siteFilter==='disabled')return !s.enabled;
  if(siteFilter==='attention')return h.enabled&&(k==='failed'||(h.https_fresh&&h.https_healthy<h.domains));
  return true;
 });
 return `<div class="site-toolbar"><label class="search-label"><span class="sr-only">搜索站点</span><input type="search" id="siteSearch" aria-label="搜索站点" placeholder="搜索站点名称或域名" value="${esc(siteQuery)}"></label><select id="siteFilter" aria-label="筛选站点">${[['all','全部站点'],['failed','存在汇报失败'],['connection','连接待观察'],['unknown','汇报证据不足'],['attention','需要留意'],['disabled','已停用']].map(([v,n])=>`<option value="${v}" ${siteFilter===v?'selected':''}>${n}</option>`).join('')}</select><span class="small muted">${rows.length} / ${data.sites.length} 个站点</span></div>
 <div class="site-table"><div class="site-table-head"><span>站点</span><span>实际 Tracker 汇报</span><span>公开连接 / 优选</span><span>操作</span></div>${rows.map(s=>{const h=healthFor(s);return `<article class="site-table-row"><div><a class="site-link" href="${esc(siteLink(s.id))}">${esc(s.name)}</a><div class="small muted">${s.website_domains.length} 个首页域名 · ${s.tracker_domains.length} 个 Tracker</div></div><div>${trackerCell(h)}</div><div><span>${esc(connectionState(h))}</span><div class="small muted">${h.published||0} / ${h.domains||0} 个域名使用优选</div></div><div class="row-actions"><a href="${esc(siteLink(s.id))}">详情</a><button data-edit="${esc(s.id)}" aria-label="编辑${esc(s.name)}">编辑</button></div></article>`;}).join('')||'<p class="empty">没有符合条件的站点。请修改搜索词或筛选条件。</p>'}</div><p class="small muted">汇报数据 ${fullTime(data.transfer?.at)} · TR 为自然汇报，QB 为轮换抽样。统计单位为记录，不是独立种子数。</p>`;
}
function siteDetailView(s){
 const h=healthFor(s),t=h.tracker||{},domains=[...new Set([...s.website_domains,...s.tracker_domains])];
 if(!domains.includes(selected))selected=domains[0];
 const execution=data.runtime_policy?.current||data.runtime_policy?.last_completed;
 const effective=execution?.speed?.site_modes?.[s.id]||(execution?'inherit':null);
 const desired=h.desired_policy||data.speed_policy?.site_modes?.[s.id]||'inherit',draft=sitePolicyDraft[s.id]||desired;
 const back=new URLSearchParams();if(siteQuery)back.set('sq',siteQuery);if(siteFilter!=='all')back.set('sf',siteFilter);
 const records=[];
 for(const [client,c] of Object.entries(data.transfer?.clients||{}))for(const d of s.tracker_domains){const r=c.trackers?.[d];if(r)records.push({client,d,r});}
 return `<div class="detail-heading"><div><a href="#sites${back.size?'?'+esc(back.toString()):''}" class="back-link">返回站点列表</a><h2>${esc(s.name)}</h2></div><div class="row-actions"><button data-edit="${esc(s.id)}">编辑域名</button><details id="siteMore" class="more-actions"><summary>更多操作</summary><div><button data-toggle="${esc(s.id)}">${s.enabled?'停用优选管理':'启用优选管理'}</button><button class="danger" data-remove="${esc(s.id)}">移除管理记录</button></div></details></div></div>
 <section class="site-status"><div><h3>实际 Tracker 汇报</h3>${trackerCell(h)}<p>${esc(healthAdvice(h))}</p><span class="small muted">采样 ${fullTime(data.transfer?.at)}${t.fresh?'':' · 暂无有效新数据'}</span></div><div><h3>公开连接检测</h3><strong>${esc(connectionState(h))}</strong><p>${h.https_healthy||0}/${h.domains||0} 个域名通过 · ${h.published||0} 个使用优选</p><span class="small muted">检测完成 ${fullTime(h.https_at)}</span></div></section>
 <details id="siteTrackerEvidence" class="disclosure"><summary>查看汇报明细与错误原因</summary><p class="small muted">${esc(h.scope||'TR 为自然汇报，QB 为轮换抽样。')}；备用地址等待不等于汇报失败。</p>${records.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>域名 / 下载器</th><th>成功 / 失败 / 等待</th><th>原因</th></tr></thead><tbody>${records.map(({client,d,r})=>`<tr><td>${esc(d)}<div class="small muted">${esc(data.transfer?.clients?.[client]?.name||client)}</div></td><td>${r.working||0} / ${r.failed||0} / ${r.pending||0}</td><td>${esc(Object.entries(r.reasons||{}).map(([k,v])=>k+' '+v+' 条').join('；')||'未提供错误原因')}</td></tr>`).join('')}</tbody></table></div>`:'<p>暂无本站汇报记录，不能判断正常或失败。</p>'}</details>
 <section class="work-section"><div class="section-heading"><div><h2>域名与当前连接</h2><p>修改这里的域名只影响优选管理，不会更换下载器任务中的汇报地址。</p></div></div><div class="site-domain-layout"><div class="domains">${domains.map(domainRow).join('')}</div><section id="domainDetail" class="panel" aria-label="选中域名的检测详情">${selected?detail():''}</section></div></section>
 <details id="sitePolicyDetails" class="disclosure"><summary>本站优选策略 · ${esc(SITE_MODES[desired]||desired)}</summary><div class="policy-edit"><p>最近已报告执行（${fullTime(execution?.captured_at)}）：<strong>${esc(SITE_MODES[effective]||'尚未上报')}</strong> · 已保存：<strong>${esc(SITE_MODES[desired])}</strong>${effective!==desired?' · 等待下一轮生效':''}</p><label for="sitePolicy-${esc(s.id)}">准备使用的策略</label><select id="sitePolicy-${esc(s.id)}" data-site-policy="${esc(s.id)}">${Object.entries(SITE_MODES).map(([v,n])=>`<option value="${v}" ${draft===v?'selected':''}>${n}</option>`).join('')}</select><button data-save-policy="${esc(s.id)}" ${draft===desired?'disabled':''}>保存站点策略</button><p class="small muted">设置在下一轮检测生效。使用原解析会撤回本站覆盖；只做对照不会发布或续期新地址。原有安全门槛继续生效。</p></div></details>`;
}
function advancedView(){return `<div class="notice">这些功能用于专项诊断。查看历史结果不会启动试验；原有停止状态保持不变。</div><details id="advancedAcceptance" class="disclosure"><summary>传输对照与恢复记录</summary>${acceptanceView()}</details><details id="advancedBudget" class="disclosure"><summary>上行依据、带宽预算与历史报告</summary>${reportView()}</details>`;}
function validateDomainText(value,tracker){
 const result=[];
 for(const raw of value.split(/[\s,，]+/).filter(Boolean)){
  if(/[?#@]/.test(raw))throw Error('请只填写域名，不要填写含账号、密钥或查询参数的地址。');
  if(tracker&&/[/:]/.test(raw))throw Error('Tracker 只填写主机名，例如 tracker.example.org，不含 https:// 或 /announce 路径。');
  let host;
  try{const u=new URL(raw.includes('://')?raw:'https://'+raw);if(!['https:','http:'].includes(u.protocol)||u.port)throw Error();host=u.hostname.toLowerCase().replace(/\.$/,'');}catch{throw Error('域名格式不正确，请参考输入框中的示例。');}
  if(!host.includes('.')||host.length>253||!host.split('.').every(x=>/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(x)))throw Error('请输入有效的完整域名，例如 tracker.example.org。');
  if(!result.includes(host))result.push(host);
 }
 return result;
}
function validateSiteFields(){
 const websites=validateDomainText($('#websiteDomains').value,false),trackers=validateDomainText($('#trackerDomains').value,true);
 if(!$('#siteName').value.trim())throw Error('请填写站点名称。');
 if(!websites.length&&!trackers.length)throw Error('请至少填写一个首页域名或 Tracker 域名。');
 return {websites,trackers};
}
$('#siteForm').addEventListener('input',()=>{
 try{const v=validateSiteFields(),existing=data.sites.filter(s=>s.id!==$('#siteId').value&&[...s.website_domains,...s.tracker_domains].some(d=>[...v.websites,...v.trackers].includes(d)));
 $('#domainFeedback').textContent=`识别到 ${v.websites.length} 个首页域名、${v.trackers.length} 个 Tracker 域名；同一栏重复项会自动合并。`+(existing.length?' 已在这些站点中出现：'+existing.map(s=>s.name).join('、')+'。请核对归属。':'');$('#formError').textContent='';
 }catch(e){$('#domainFeedback').textContent=e.message;}
});
$('#view').addEventListener('change',e=>{if(e.target.dataset.sitePolicy){const id=e.target.dataset.sitePolicy,h=(data.site_health||[]).find(s=>s.id===id);const b=document.querySelector('[data-save-policy="'+CSS.escape(id)+'"]');if(b)b.disabled=e.target.value===(h?.desired_policy||'inherit');}});

registerSpeedHandlers();
registerAcceptanceHandlers();
route();
refresh();
