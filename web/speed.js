'use strict';
function bytesPerSecond(n) {
  if (n == null) return '—';
  return n >= 1048576 ? (n / 1048576).toFixed(2) + ' MiB/s' : (n / 1024).toFixed(1) + ' KiB/s';
}

function speedSummary() {
  const rows = Object.values(data.latest?.domains || {});
  const compared = rows.filter(r => Object.keys(r.speed?.metrics || {}).length).length;
  const gains = rows.filter(r => r.speed?.proposed_ip && r.speed?.gain_ms > 0).length;
  const modes = {shadow:'只测不切换', canary:'单站点灰度', auto:'主动优选'};
  const t = data.transfer || {};
  const deferred = rows.filter(r => r.action === 'speed_deferred').length;
  const dnsFailed = rows.filter(r => r.error === 'PublicDNSError').length;
  const providers = {alidns:'阿里加密 DNS',cloudflare:'Cloudflare',google:'Google'};
  const sources = Object.entries(data.latest?.dns_sources || {}).filter(([,s])=>s.successes>0).map(([n])=>providers[n]||n);
  return `<section class="speed-summary"><div><strong>${esc(modes[data.speed_policy?.mode] || '等待提速检测')}</strong><p>${compared} 个域名已有对照，${gains} 个发现潜在提速机会。以跨窗口验证和实际使用结果为准。</p><p class="${dnsFailed?'warn':'muted'}">本轮公开解析失败 ${dnsFailed} 个 · 暂缓或待补样 ${deferred} 个${sources.length?' · 可用解析源：'+esc(sources.join('、')):''}。处理完全部域名不等于全部样本合格。</p></div><a class="link-button" href="#speed">查看提速收益与实际传输</a><p>${t.fresh ? `实际下载 ${bytesPerSecond(t.total_down_Bps)} · 上传 ${bytesPerSecond(t.total_up_Bps)}${!t.total_down_Bps && !t.total_up_Bps ? ' · 当前没有实际传输样本' : ''}` : '传输观测尚未更新，暂不判断下载或上传收益。'}</p></section>`;
}

function capWindowNote(name, t) {
  const u = t.window?.clients?.[name]?.up;
  if (!t.fresh || !u) return '<div class="small muted">限速判断：等待有效活跃样本</div>';
  const notes = [];
  if (u.assessment !== 'insufficient_samples') notes.push(`全局额度：${u.near_limit_samples}/${u.limited_active_samples} 个活跃样本接近上限${u.assessment==='near_limit'?'，可能受额度约束':'，尚未持续撞限'}`);
  if (u.task?.assessment !== 'insufficient_samples' && u.task) notes.push(`单任务额度：${u.task.near_limit_samples}/${u.task.active_samples} 个活跃样本出现任务接近上限${u.task.assessment==='near_limit'?'，可能受单任务额度约束':'，尚未持续撞限'}`);
  return `<div class="small ${u.assessment==='near_limit'||u.task?.assessment==='near_limit'?'warn':'muted'}">${esc(notes.join('；') || '限速判断：等待至少 5 个活跃样本、跨越 4 分钟')}。</div>`;
}

function effectiveCaps(name,c) {
  const notes = [typeof c.alternative_limits==='boolean' ? (c.alternative_limits ? '备用限速开启' : '备用限速关闭') : '备用限速状态未采集'];
  for (const [d,label] of [['up','上传'],['down','下载']]) {
    const scope=c.session_scope?.[d], cap=c[d+'_limit_Bps'];
    if (scope?.enabled) notes.push(`${label}会话额度 ${cap?bytesPerSecond(cap):'待确认'}，遵循 ${scope.honoring_tasks} 个任务，绕过 ${scope.bypassing_tasks} 个任务${scope.unknown_tasks?'，未知 '+scope.unknown_tasks+' 个':''}`);
    else if (cap) notes.push(`${label}全局额度 ${bytesPerSecond(cap)}`);
    const tasks=c.task_caps?.[d];
    if (tasks?.own_limited_tasks) {
      const caps=Object.entries(tasks.own_cap_distribution).sort((a,b)=>b[1]-a[1]);
      notes.push(`${label}单任务额度：${caps.slice(0,3).map(([n,count])=>bytesPerSecond(Number(n))+' × '+count).join('、')}${caps.length>3?' 等':''}`);
      if (tasks.active_own_limited_tasks) notes.push(`${label}活跃任务中 ${tasks.active_near_own_limit}/${tasks.active_own_limited_tasks} 个接近自身额度`);
    }
    if (tasks?.unknown_tasks) notes.push(`${label}另有 ${tasks.unknown_tasks} 个任务的有效额度未确认`);
  }
  return notes.map(x=>`<div class="small muted">${esc(x)}</div>`).join('');
}

function speedView(tab='connections') {
  const p = data.speed_policy || {}, draft = speedDraft || {mode:p.mode || 'shadow', domains:(p.allow_domains || []).join('\n')};
  const rows = data.domains.map(domain => ({domain, row:data.latest?.domains?.[domain] || {}}))
    .filter(x => x.row.speed).sort((a,b) => (b.row.speed.observed_gain_ms ?? b.row.speed.gain_ms ?? -Infinity) - (a.row.speed.observed_gain_ms ?? a.row.speed.gain_ms ?? -Infinity));
  const ms = v => Number.isFinite(v) ? Math.round(v) + ' ms' : '—';
  const detailRows = rows.map(({domain,row}) => {
    const s = row.speed, metric = s.metrics?.[s.best_ip], observed = s.observed_gain_pct;
    const gain = observed ?? s.gain_pct;
    const restored = s.original_ms == null && s.current_ms != null && s.applied;
    const gainText = restored ? '原路径未通过，当前可用' : Number.isFinite(gain) ? (gain > 0 ? '耗时减少 ' : gain < 0 ? '耗时增加 ' : '耗时相近 ') + Math.abs(gain).toFixed(1) + '%' : '尚无可比样本';
    return `<tr><td><span class="speed-domain">${esc(siteName(domain))}</span><span class="mono">${esc(domain)}</span></td><td class="num">${ms(s.original_ms)}</td><td class="num">${ms(s.current_ms)}<div class="small muted">${esc(data.published[domain] || '原解析')}</div></td><td class="num">${ms(s.best_ms)}<div class="small muted">${metric ? `${metric.successes}/${metric.expected} 次通过` : '样本不足'}</div></td><td><span class="badge ${restored || gain > 0 ? 'good' : gain < 0 ? 'warn' : ''}">${esc(gainText)}</span><div class="small muted">${restored?'恢复可用，不计算提速百分比':observed != null ? '当前 IP 与原解析同期实测' : '候选窗口对照，非下载速度'}</div></td><td>${esc((ACTIONS[row.action] || ['等待检测'])[0])}<div class="small muted">${restored?'当前优选已重新验证':`${s.confirmed_windows || 0}/${s.required_windows || 2} 个收益窗口`}${s.budget_exhausted ? ' · 候选样本待补齐' : ''}</div></td></tr>`;
  }).join('');
  const t = data.transfer || {}, clients = Object.entries(t.clients || {});
  const names = {movie_qbittorrent:'影视 QB', pt_qbittorrent:'PT QB', pt_transmission:'TR 做种'};
  const clientsHtml = clients.map(([name,c]) => `<tr><td>${esc(c.name || names[name] || name)}<div class="small muted">${c.tasks ?? '—'} 个任务 · ${c.peers ?? '—'} 个连接</div></td><td class="num">${c.ok && t.fresh ? bytesPerSecond(c.down_Bps) : '—'}</td><td class="num">${c.ok && t.fresh ? bytesPerSecond(c.up_Bps) : '—'}</td><td>${c.ok ? `下载活跃 ${c.active_downloads || 0} · 上传活跃 ${c.active_uploads || 0}` : '观测暂不可用'}<details id="caps-${esc(name)}" class="inline-evidence"><summary>查看现有限速与判断依据</summary>${c.ok?effectiveCaps(name,c):''}${capWindowNote(name,t)}</details></td></tr>`).join('');
  const trackers = new Map();
  for (const [,c] of clients) for (const [domain,r] of Object.entries(c.trackers || {})) {
    const item = trackers.get(domain) || {working:0,failed:0,pending:0,reasons:new Set(),lastSuccess:0};
    for (const k of ['working','failed','pending']) item[k] += r[k] || 0;
    item.lastSuccess = Math.max(item.lastSuccess,r.last_success_at || 0);
    Object.keys(r.reasons || {}).forEach(x => item.reasons.add(x));trackers.set(domain,item);
  }
  const trackerRows = [...trackers].filter(([d]) => data.domains.includes(d)).sort((a,b)=>b[1].failed-a[1].failed).map(([d,r]) => `<tr><td class="mono">${esc(d)}</td><td>${r.working}<div class="small muted">${r.lastSuccess?'最后成功 '+fullTime(r.lastSuccess):'无成功时间记录'}</div></td><td>${r.failed}</td><td>${r.pending}</td><td>${esc([...r.reasons].join('、') || '—')}</td></tr>`).join('');
  if(tab==='settings') return `<section class="panel speed-section"><div class="panel-head"><div><h2>提速应用方式</h2><p>自动模式也需达到 ${Math.round((p.min_gain_ratio || .2)*100)}% 且 ${p.min_gain_ms || 100} ms 收益，并跨 ${p.confirm_windows || 2} 个窗口确认；故障恢复单独判断。</p></div></div><form id="speedPolicyForm" class="speed-settings"><label for="speedMode">应用模式</label><select id="speedMode">${[['shadow','只测不切换'],['canary','仅灰度域名可切换'],['auto','主动优选：验证后自动应用']].map(([v,n])=>`<option value="${v}" ${draft.mode===v?'selected':''}>${n}</option>`).join('')}</select><label for="canaryDomains">灰度域名（仅灰度模式使用，每行一个已启用域名）</label><textarea id="canaryDomains" rows="2" spellcheck="false">${esc(draft.domains)}</textarea><button class="primary" type="submit" ${speedSaving?'disabled':''}>${speedSaving?'正在保存…':'保存提速模式'}</button><p class="muted">已保存模式：${esc(({auto:"自动优选",shadow:"只测不切换",canary:"仅指定域名优选"})[p.mode]||"尚未确认")}；最近已报告执行：${esc(({auto:"自动优选",shadow:"只测不切换",canary:"仅指定域名优选"})[(data.runtime_policy?.current||data.runtime_policy?.last_completed)?.speed?.mode]||"尚未上报")}（${fullTime((data.runtime_policy?.current||data.runtime_policy?.last_completed)?.captured_at)}）。设置在下一轮检测生效。不会修改下载器任务、下载路径或源文件。</p></form></section>`;
  if(tab==='transfer') return `<div class="transfer-overview">${counterEvidenceView(data.bottleneck?.counter_window)}</div><section class="panel speed-section"><div class="panel-head"><div><h2>实际下载与做种上传</h2><p>只读观测 · ${t.at ? fullTime(t.at) : '尚未采样'}${t.fresh ? '' : ' · 数据未更新，勿作为当前速度'}</p></div></div>${clientsHtml ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>下载器</th><th>下载</th><th>上传</th><th>活跃任务与限速</th></tr></thead><tbody>${clientsHtml}</tbody></table></div>` : '<div class="empty">观测服务尚未提供数据。</div>'}<details id="transferEvidence" class="disclosure"><summary>查看流量统计依据与限速说明</summary><div class="speed-notes">${(t.hints || []).map(x => `<p>${esc(x)}</p>`).join('')}<p>最近15分钟的瞬时抽样均值：${t.fresh && t.window ? `下载 ${bytesPerSecond(t.window.mean_down_Bps)}，上传 ${bytesPerSecond(t.window.mean_up_Bps)}，${t.window.samples} 个样本。` : '等待有效观测。'}种群和需求会波动，不把瞬时速度变化直接归因于优选。</p>${counterEvidenceView(data.bottleneck?.counter_window)}</div></details></section><details id=allTrackerRecords class=disclosure><summary>展开全部 Tracker 汇报记录</summary><section class="panel speed-section"><div class="panel-head"><div><h2>下载器真实汇报记录</h2><p>读取自然汇报结果，不强制重新汇报；记录数是任务与 Tracker 记录，非独立站点数。</p></div></div>${trackerRows ? `<div class="table-scroll"><table class="data-table"><thead><tr><th>域名</th><th>成功</th><th>失败</th><th>等待</th><th>原因分类</th></tr></thead><tbody>${trackerRows}</tbody></table></div>` : '<div class="empty">尚无清单内 Tracker 的自然汇报观测。</div>'}<p class="speed-notes">${esc(t.tracker_scope || '等待观测服务')}。${t.fresh ? '' : '当前记录已过期或未采集。'}</p></section></details>`;
  return `${connectionSummary()}<section class="panel"><div class="panel-head"><div><h2>原线路与优选地址对照</h2><p>相同域名与请求方式交替采样；原解析取已测地址中的最佳值作保守比较。根路径耗时不等于实际汇报或文件传输收益。</p></div></div>${detailRows ? `<div class="table-scroll"><table class="data-table speed-table"><thead><tr><th>站点 / 域名</th><th>原解析</th><th>当前优选</th><th>本轮最快</th><th>对照结果</th><th>决定</th></tr></thead><tbody>${detailRows}</tbody></table></div>` : '<div class="empty">尚未完成提速对照。等待本轮完成后查看真实结果。</div>'}</section>`;
}

function registerSpeedHandlers() {
  $('#view').addEventListener('input', e => {
    if (['speedMode','canaryDomains'].includes(e.target.id)) speedDraft = {mode:$('#speedMode').value,domains:$('#canaryDomains').value};
  });
  $('#view').addEventListener('submit', async e => {
    if (e.target.id !== 'speedPolicyForm') return;
    e.preventDefault();
    if(statusUnavailable || speedSaving){message('请等待状态读取或当前保存完成。',true);return;}
    const mode = $('#speedMode').value, allow_domains = $('#canaryDomains').value.split(/[\s,，]+/).filter(Boolean);
    speedSaving = true; render();
    try {
      const r = await fetch('/api/speed',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify({mode,allow_domains,revision:data.registry_revision,policy_revision:data.speed_revision})});
      const result = await r.json();if (!r.ok) throw new Error(result.error || '保存失败');
      speedDraft = null;message(result.message);
    } catch(e) {message(e.message,true);}
    finally {speedSaving=false;await refresh();}
  });
}

function counterEvidenceView(c){
 if(!c||!c.counter_covered_seconds)return '<p class="counter-window-evidence small muted">最近15分钟累计统计：缺少可比较的同源计数区间，不能按零流量处理。</p>';
 const size=n=>n==null?'未确认':(n/1048576).toFixed(2)+' MiB';
 return `<div class="counter-window-evidence"><p>${c.fresh?'最近15分钟累计统计':'历史累计统计（已过期）'}：上传 ${size(c.uploaded_bytes)} · 下载 ${size(c.downloaded_bytes)}。</p><p>累计统计平均：上传 ${bytesPerSecond(c.mean_up_Bps)} · 下载 ${bytesPerSecond(c.mean_down_Bps)}；仅按有效区间计算。</p><p class="small muted">计数证据 ${c.since?fullTime(c.since):'未确认'} 至 ${c.until?fullTime(c.until):'未确认'} · 有效覆盖 ${(c.counter_covered_seconds/60).toFixed(1)} 分钟 · 计数回退 ${c.counter_resets??0} 次 · 排除间隔 ${c.excluded_intervals??0} 个。</p><p class="small muted">瞬时为0不代表整个窗口无流量；累计统计也不能单独证明新文件下载、补下载或优选提速。缺失区间不补零。</p></div>`;
}

function bottleneckView() {
  const diagnostic=data.bottleneck||{},names={movie_qbittorrent:'影视 QB',pt_qbittorrent:'PT QB',pt_transmission:'TR 做种'};
  const clients=Object.entries(diagnostic.clients||{});
  const rows=clients.map(([name,r])=>{
    const snapshot=r.instant||{},scope=r.session_scope?.up;
    const evidence=(r.findings||[]).map(f=>{
      const e=f.evidence||{};
      const detail=f.code==='dispatch_blocked'?`冲突 ${e.conflicts??'—'} 条 · 待确认 ${e.pending??'—'} 条`:`${e.near_samples??'—'}/${e.active_samples??'—'} 个活跃样本接近额度 · 跨越 ${e.span_seconds??'—'} 秒 · ${e.scope||'适用范围待确认'}`;
      return `<li><strong>${esc(f.title)}</strong><div>${esc(detail)}</div>${e.since?`<div>有效样本 ${fullTime(e.since)} 至 ${fullTime(e.until)}</div>`:''}<div class="muted">依据类型：${esc(f.confidence)}</div></li>`;
    }).join('');
    const reasons=Object.entries(r.tracker_context?.failed_record_categories||{}).map(([reason,count])=>reason+' '+count+' 条').join('、');
    return `<article class="diagnosis-row"><div class="diagnosis-heading"><h3>${esc(data.transfer?.clients?.[name]?.name||names[name]||name)}</h3><span class="badge ${r.state==='possible_limit'||r.state==='dispatch_blocked'||!r.fresh?'warn':''}">${esc(r.confidence)}</span></div><p class="diagnosis-conclusion">${esc(r.title)}</p><p class="small muted">数据 ${fullTime(r.observed_at)} · ${r.age_seconds??'—'} 秒前 · ${esc(r.evidence_source||'来源未上报')}</p>${r.fresh?`<p>当前下载 ${bytesPerSecond(snapshot.down_Bps)} · 上传 ${bytesPerSecond(snapshot.up_Bps)} · 活跃下载 ${snapshot.active_downloads??'—'} / 上传 ${snapshot.active_uploads??'—'} · 连接 ${snapshot.peers??'—'}</p>`:''}<details id="diagnostic-${esc(name)}" class="disclosure"><summary>查看累计流量、限速依据与证据范围</summary>${counterEvidenceView(r.counter_window)}${evidence?`<ul class="diagnosis-evidence">${evidence}</ul>`:'<p class="muted">当前没有足以确认持续额度约束的证据。</p>'}${scope?`<p class="small">上传会话额度：遵循 ${scope.honoring_tasks??'—'} 个任务、绕过 ${scope.bypassing_tasks??'—'} 个任务。全局窗口只统计遵循额度的任务。</p>`:''}<p>${esc(r.next||'等待可用的只读观测数据。')}</p><details id="diagnosticLimits-${esc(name)}"><summary>证据范围与尚未验证的事项</summary><p>${esc(reasons?'自然汇报记录：'+reasons+'。':'这次读取的自然汇报记录未提供可展示的失败分类。')} ${esc(r.tracker_context?.scope||'自然汇报范围未确认')}</p><ul>${(r.limitations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details></details></article>`;
  }).join('');
  return `<section class="panel diagnosis-panel" aria-labelledby="bottleneckHeading"><div class="panel-head"><div><h2 id="bottleneckHeading">瓶颈解释</h2><p>评估时间 ${fullTime(diagnostic.evaluated_at)}。${esc(diagnostic.claim||'等待观测，不根据瞬时速率推断原因。')}</p></div></div>${rows||'<div class="empty">尚无可用的下载器诊断数据，保留现有额度。</div>'}</section>`;
}
