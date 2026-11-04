'use strict';
let acceptanceBusy=false;const sitePolicyDraft={};
const ACCEPT_STATES={waiting:'等待自然样本',baseline:'原状态基线',trial:'短时额度对照中',recovering:'恢复并核对原状态',after:'恢复后复测',complete:'本次验收完成',inconclusive:'本次证据不足',expired:'等待期结束，样本不足',stopped:'已停止',recovery_blocked:'恢复待处理',failed:'执行失败'};
const ACCEPT_MODES={observe:'QB只读观察',quota:'QB单任务减额',tr_share:'TR共享额度试行'};
const SITE_MODES={inherit:'跟随全局优选',shadow:'只做对照，不发布新地址',origin:'使用原解析，仅监控'};
function dateOrEmpty(t){return t?fullTime(t):'暂无记录';}
function budgetCampaignView(){
 const b=data.budget_acceptance||{},completed=b.completed_stages||{},labels={qb_pilot:'单个QB传输对照',all:'三个客户端统一预算对照'};
 return `<section class="panel speed-section" id="budgetCampaignPanel"><div class="panel-head"><div><h2>预算传输对照排期</h2><p>约每分钟检查现有流量，等待期间不改额度。TR仅等待时可独立收集QB样本，实际改额度串行执行；先单QB，再验证三个客户端。</p></div></div><div class="speed-notes"><strong>${esc(b.fresh===false&&b.id?'排期状态未更新':ACCEPT_STATES[b.status]||'尚未安排预算对照')}</strong><p role="status">${esc(b.stop_requested?'已请求停止排期；临时额度须恢复后才确认结束。':b.reason||'尚无排期记录。')}</p>${b.id?`<p>当前阶段：${esc(labels[b.scope]||b.scope)}<br>状态时间 ${dateOrEmpty(b.updated_at)} · 下次检查 ${dateOrEmpty(b.next_check_at)}<br>等待截止 ${dateOrEmpty(b.deadline)} · 恢复确认 ${dateOrEmpty(b.restored_at)}</p><p>预算参考 ${bytesPerSecond(b.budget)} · 持续上传依据 ${dateOrEmpty(b.evidence?.at)}<br>单QB对照 ${completed.qb_pilot?'已完成并恢复':'待验收'} · 三客户端对照 ${completed.all?'已完成并恢复':'待验收'}</p>`:''}<p>至少5个自然样本覆盖4分钟，对端、任务、累计计数和配置须稳定；临时租约最多120秒。条件变化即撤回，回读确认原额度后才结束。</p><p>等待最多24小时，样本不足就结束本轮。对照通过不代表已提速；三客户端试行还须通过前序证明及稳定样本检查，完成后恢复，不持续启用预算。</p><button id="budgetCampaignStop" class="danger" ${acceptanceBusy||!b.stoppable||b.stop_requested?'disabled':''}>停止预算对照排期</button></div></section>`;
}
function nativeBudgetView(){
 const b=data.native_budget||{},states={none:'尚未安排预算试行',unavailable:'预算状态未更新',pending:'等待控制器确认',applying:'正在应用临时额度',active:'临时额度已回读确认',recovering:'正在恢复原额度',blocked:'恢复待处理',restored:'原额度已恢复'};
 const when=t=>dateOrEmpty(t?Math.floor(t/1000):0),single=b.scope==='qb_pilot';
 return `<section class="panel speed-section" id="nativeBudgetPanel"><div class="panel-head"><div><h2>带宽预算试行与恢复</h2><p>${single?'本轮只验证一个 QB 的临时额度和恢复，统一预算尚未应用。':'统一预算仅在所有客户端的额度和适用范围都回读通过时标为生效。'}</p></div></div><div class="speed-notes"><strong>${esc(b.fresh===false?'状态未更新':states[b.state]||'状态待核对')}</strong><p role="status">${b.stop_requested?'已请求停止，等待下载器确认原额度。':b.fresh===false?'状态未更新，不能确认当前额度或恢复结果。':b.globalEnforced?'统一预算已确认生效。':b.enforced?'本轮所选客户端临时额度已确认生效。':'当前没有新鲜证据证明预算生效。'}${b.purpose==='functional'?' 本轮为功能检查，不用于计算提速收益。':''}</p><p>状态采集 ${when(b.observedAt)} · 额度回读 ${when(b.lastConfirmedAt)}<br>临时租约到期 ${when(b.expires)} · 原额度恢复确认 ${when(b.restoredAt)}</p>${b.clients?.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>本轮客户端</th><th>原上传上限</th><th>最后回读上限</th></tr></thead><tbody>${b.clients.map(c=>`<tr><td>${esc(c.name||c.id)}</td><td>${bytesPerSecond(c.originalUploadBps)}</td><td>${bytesPerSecond(c.lastConfirmedUploadBps)}</td></tr>`).join('')}</tbody></table></div>`:''}${b.error?`<p class="bad">${esc(b.error)}</p>`:''}<p>到期或停止后由同一控制器恢复，回读成功才算恢复完成。功能试行最多45秒；不启动任务、不增加下载，也不改源文件。</p><button id="nativeBudgetStop" class="danger" ${acceptanceBusy||!b.stoppable||b.stop_requested?'disabled':''}>停止预算试行并恢复</button></div></section>`;
}
function acceptanceView(){
 const a=data.acceptance||{},r=a.result||{},history=data.acceptance_history||[];
 return nativeBudgetView()+budgetCampaignView()+`<section class="panel"><div class="panel-head"><div><h2>实际传输验收</h2><p>等待现有任务自然产生合格上传，再进行短时额度对照。不会增加下载、启动暂停任务或改动文件。</p></div></div>
 <div class="speed-notes"><strong>${esc(ACCEPT_STATES[a.status]||'尚未安排验收')}</strong><p role="status">${esc(a.reason||'可安排 QB 只读观察、单任务减额，或两个 TR 任务的共享额度试行。')}</p>${a.mode?`<p>本轮方式：${esc(ACCEPT_MODES[a.mode]||a.mode)}<br>${esc(a.protocol||'')}${a.mode==='tr_share'&&a.shared_cap_Bps?`<br>沿用现有 TR 会话上传上限 ${bytesPerSecond(a.shared_cap_Bps)}，不修改会话总限速。`:''}</p>`:''}<p>状态时间 ${dateOrEmpty(a.updated_at)}${a.stoppable&&a.updated_at&&!a.fresh?' · 状态未更新，不能认为仍正常执行':''}<br>下次检查 ${dateOrEmpty(a.next_check_at)} · 等待截止 ${dateOrEmpty(a.deadline)}<br>临时租约到期 ${dateOrEmpty(a.lease_expires_at)} · 原状态恢复确认 ${dateOrEmpty(a.restored_at)}</p>
 <p>到期后请求恢复，只有下载器回读确认才标记成功；连接异常时显示待恢复，并阻止新试验。</p><div class="toolbar"><button id="acceptObserve" ${acceptanceBusy||a.stoppable||a.status==='recovery_blocked'?'disabled':''}>安排只读观察</button><button id="acceptQuota" ${acceptanceBusy||a.stoppable||a.status==='recovery_blocked'?'disabled':''}>安排单任务减额对照</button><button id="acceptTRShare" ${acceptanceBusy||a.stoppable||a.status==='recovery_blocked'?'disabled':''}>安排TR共享额度试行</button><button id="acceptStop" class="danger" ${acceptanceBusy||!a.stoppable?'disabled':''}>停止并恢复原状态</button></div>
 ${r.reason||r.interpretation?`<p>${esc(r.reason||'')}${r.interpretation?' · '+esc(r.interpretation):''}</p>`:''}
 ${r.rates?`<p>原状态 ${bytesPerSecond(r.rates.before)} → ${a.mode==='tr_share'?'共享额度期':'减额期'} ${bytesPerSecond(r.rates.trial)} → 恢复 ${bytesPerSecond(r.rates.after)}。${r.valid?'已通过本次可比条件':'对照不可比，不采用结果'}。</p>`:''}
 </div><details class="diagnosis-limitations"><summary>合格条件与自动恢复规则</summary><ul class="plain-list"><li>至少 5 个自然上传样本覆盖 4 分钟，接收对端、其他任务流量、现有策略和原生额度稳定；肉丝排除。</li><li>QB 对照仅选择一个已完成任务，固定单种额度须归属种子港；短时减额 12.5%，原额度继续预留。</li><li>TR 试行仅选择两个已完成、正在做种的任务，让它们短时遵循现有会话上传上限；不修改会话总限速、下载额度或原有单种额度。</li><li>前后基线与试行期各约 25 秒，临时租约最多 120 秒，到期请求恢复。由同一控制器写入和恢复；有冲突、未确认写入或上次恢复未完成时拒绝开始。</li><li>需求、对端、其他流量、进程或策略改变即撤回；恢复必须原生回读确认，异常时保留记录并阻止新试验。</li><li>等待最多 24 小时，可随时停止。额度响应仅用于验收执行与恢复，真实文件提速收益另行验收。</li></ul></details></section>
 <section class="panel speed-section"><div class="panel-head"><div><h2>近期验收记录</h2><p>保留结果不足和中止原因；不把失败记录隐藏为成功。</p></div></div>${history.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>结束时间</th><th>方式</th><th>状态</th><th>结论</th></tr></thead><tbody>${history.slice().reverse().map(x=>`<tr><td>${dateOrEmpty(x.finished_at)}</td><td>${esc(ACCEPT_MODES[x.mode]||x.mode)}</td><td>${esc(ACCEPT_STATES[x.status]||x.status)}</td><td>${esc(x.reason||'—')}</td></tr>`).join('')}</tbody></table></div>`:'<p class="speed-notes">尚无已结束验收。</p>'}</section>`;
}
function healthView(){
 const rows=data.site_health||[],current=data.runtime_policy?.current?.speed?.site_modes;
 const states={healthy:'访问通过',attention:'需要关注',pending:'证据待补',stale:'检测已过期',disabled:'已停用'};
 return `<section class="panel"><div class="panel-head"><div><h2>站点策略与健康</h2><p>分别列出 HTTPS 检测和下载器自然汇报。无汇报记录表示未采到，不能判断站点正常或失败。</p></div></div><div class="table-scroll"><table class="data-table site-health-table"><thead><tr><th>站点</th><th>访问证据</th><th>自然汇报证据</th><th>站点策略</th></tr></thead><tbody>${rows.map(s=>{
  const effective=current?current[s.id]||'inherit':null,t=s.tracker||{},draft=sitePolicyDraft[s.id]||s.desired_policy;
  return `<tr><td><strong>${esc(s.name)}</strong><div class="small ${s.status==='attention'?'warn':'muted'}">${esc(states[s.status]||s.status)} · 优选 ${s.published}/${s.domains}</div></td><td>${s.https_healthy}/${s.domains} 域名通过<div class="small muted">${dateOrEmpty(s.https_at)}${s.https_fresh?'':' · 非当前证据'}</div></td><td>${t.records?`成功 ${t.working} · 失败 ${t.failed} · 等待 ${t.pending}`:'尚无清单内观测'}<div class="small muted">采样 ${dateOrEmpty(data.transfer?.at)}${t.fresh?'':' · 数据过期'}<br>TR最后成功 ${dateOrEmpty(t.last_success_at)}；QB为轮换抽样</div></td><td><label class="sr-only" for="sitePolicy-${esc(s.id)}">${esc(s.name)}策略</label><select id="sitePolicy-${esc(s.id)}" data-site-policy="${esc(s.id)}">${Object.entries(SITE_MODES).map(([v,n])=>`<option value="${v}" ${draft===v?'selected':''}>${n}</option>`).join('')}</select><button data-save-policy="${esc(s.id)}">保存</button><div class="small muted">上轮执行：${esc(effective?SITE_MODES[effective]:'尚未上报')}${effective!==s.desired_policy?' · 待下轮生效':''}</div></td></tr>`;
 }).join('')}</tbody></table></div><p class="speed-notes">站点策略只能收紧全局范围，不降低收益、证书、样本或恢复保护条件。原解析模式会撤回该站覆盖；只做对照不会发布或续期新优选。暂停任务、目录、标签和源文件不受影响。</p></section>`;
}
function capacityAssessmentView(){
 const c=data.capacity_assessment||{};
 if(!c.id)return '';
 const names={waiting:'等待空闲窗口',running:'限时测量中',complete:'本轮测量完成',aborted:'已触发保护中止',failed:'测量失败',expired:'等待或测量已到期',stopped:'已停止',interrupted:'上轮中断，已停止重试'};
 return `<div class="capacity-assessment"><h3>上行路径测量 · ${esc(names[c.status]||c.status)}</h3><p role="status">${esc(c.stop_requested&&c.stoppable?'已请求停止，等待执行服务确认':c.reason||'等待执行服务更新')}</p><p>状态时间 ${dateOrEmpty(c.updated_at)}<br>下次检查 ${dateOrEmpty(c.next_check_at)} · 等待截止 ${dateOrEmpty(c.deadline)}</p><p>每 ${Math.round(c.check_interval_seconds/60)} 分钟检查空闲条件；最多上传 ${Math.round(c.max_payload_bytes/1048576)} MiB 随机数据，单次最多 ${c.max_seconds_per_upload} 秒。PT 需求回升或延迟超过阈值时中止；测量开始后只尝试本轮。</p>${c.observed_path_lower_bound_Bps?`<p>本轮短时上传测速最高读数 ${bytesPerSecond(c.observed_path_lower_bound_Bps)} · ${c.sample_count} 个短时样本。网关代理可能影响路径，完整宽带容量仍待确认。</p>`:''}<button id="capacityStop" class="danger" ${!c.stoppable||c.stop_requested||acceptanceBusy?'disabled':''}>停止本轮上行测量</button></div>`;
}
function reportView(includeBudget=true){
 const r=data.reports||{},cap=r.capacity||{};
 const size=n=>n==null?'暂无完整计数差额':(n/1073741824).toFixed(2)+' GiB';
 return `<section class="panel"><div class="panel-head"><div><h2>24 小时 / 7 天收益报告</h2><p>生成于 ${dateOrEmpty(r.at)}${r.fresh?'':' · 报告未更新'}。覆盖不足保持“部分窗口”，不外推完整周期收益。</p></div></div>${(r.reports||[]).map(w=>`<div class="report-period"><h3>${w.hours===24?'最近24小时':'最近7天'} · ${w.status==='complete'?'窗口完整':'数据覆盖不足'}</h3><p>域名 ${w.domain_rounds} 轮 · ${dateOrEmpty(w.domain_since)} 至 ${dateOrEmpty(w.domain_until)} · 最大间隔 ${w.max_round_gap_seconds==null?'无可比较间隔':Math.round(w.max_round_gap_seconds/60)+'分钟'}</p><p>HTTPS同期配对 ${w.https_paired_comparisons} 组 · 中位访问收益 ${w.https_median_gain_pct==null?'样本不足':w.https_median_gain_pct+'%'} · 更快 ${w.https_faster} / 更慢 ${w.https_slower}。仅适用于根路径访问。</p><p>累计计数有效覆盖 ${Math.round(w.transfer.counter_covered_seconds/60)} 分钟 · 上传 ${size(w.transfer.uploaded_bytes)} · 下载 ${size(w.transfer.downloaded_bytes)} · 计数回退 ${w.transfer.counter_resets} 次 · 排除间隔 ${w.transfer.excluded_intervals} 个。</p><p>合格 QB 减额对照 ${w.qualified_transfer_trials} 次 · TR 共享额度对照 ${w.qualified_tr_sharing_trials??0} 次 · QB总额对照 ${w.qualified_native_qb_trials??0} 次 · 统一预算对照 ${w.qualified_global_budget_trials??0} 次 · 真实文件提速比例：<strong>尚无可归因证据</strong>。</p><details id="reportCoverage-${w.hours}"><summary>覆盖与解释限制</summary><ul class="plain-list">${w.limitations.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></details></div>`).join('')||'<p class="speed-notes">等待报告服务首次汇总。</p>'}</section>
 ${includeBudget?`<section class="panel speed-section"><div class="panel-head"><div><h2>上行依据与带宽预算</h2><p>先取得流量依据，再小范围试行；不会把现有客户端额度之和当成宽带容量。</p></div></div><div class="speed-notes"><p>近24小时最高持续上传下界：${cap.sustained_upload_lower_bound?bytesPerSecond(cap.sustained_upload_lower_bound.lower_Bps)+' · '+cap.sustained_upload_lower_bound.span_seconds+'秒累计计数窗口 · 测量截至 '+dateOrEmpty(cap.sustained_upload_lower_bound.at):'等待至少4分钟的完整、连续累计计数'}；宽带完整上行容量：尚未确认。</p><p>保守试行预算参考 ${bytesPerSecond(cap.recommended_initial_budget_Bps)}（持续下界的80%，向下取整）。<strong>${data.native_budget?.globalEnforced?'统一预算正在限时试行，尚未持续启用':'统一预算尚未持续启用'}</strong>。</p><p>${esc(cap.meaning||'数据不足，不推定上行能力')}</p>${capacityAssessmentView()}<ul class="plain-list">${(cap.blockers||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul><p>统一预算由原控制器管理客户端总额度，保留单种规则；前序TR和单QB验收通过、流量稳定后才有限试行。</p></div></section>`:''}${alertsView()}`;
}
function alertsView(){const a=data.alerts||[];return `<section class="panel speed-section"><div class="panel-head"><div><h2>异常提示</h2><p>只在证据满足条件时提示；查看时间和影响范围后处理。</p></div></div>${a.length?a.map(x=>`<p class="speed-notes ${x.level==='error'?'bad':'warn'}"><strong>${esc(x.message)}</strong><br><span class="small muted">证据 ${dateOrEmpty(x.evidence_at)} · 检出 ${dateOrEmpty(x.detected_at)}</span></p>`).join(''):'<p class="speed-notes">当前未检出已定义的异常；这不代表所有传输条件均已验收。</p>'}</section>`;}
function registerAcceptanceHandlers(){
 $('#view').addEventListener('change',e=>{if(e.target.dataset.sitePolicy)sitePolicyDraft[e.target.dataset.sitePolicy]=e.target.value;});
 $('#view').addEventListener('click',async e=>{
  const id=e.target.id,save=e.target.closest('[data-save-policy]');
  if(save){const site=save.dataset.savePolicy;if(statusUnavailable||sitePolicyBusy.has(site))return;sitePolicyBusy.add(site);const value=sitePolicyDraft[site]||data.speed_policy.site_modes?.[site]||'inherit';save.disabled=true;try{const r=await fetch('/api/speed',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify({site_modes:{...(data.speed_policy.site_modes||{}),[site]:value},revision:data.registry_revision,policy_revision:data.speed_revision})});const result=await r.json();if(!r.ok)throw Error(result.error);delete sitePolicyDraft[site];message('站点策略已保存，下轮检测生效');}catch(err){message(err.message,true);}finally{sitePolicyBusy.delete(site);await refresh();}return;}
  if(id==='budgetCampaignStop'){
   if(acceptanceBusy)return;
   acceptanceBusy=true;render();
   try{const r=await fetch('/api/budget-acceptance',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify({op:'stop',id:data.budget_acceptance.id})});const result=await r.json();if(!r.ok)throw Error(result.error);message(result.message);}
   catch(err){message(err.message,true);}finally{acceptanceBusy=false;await refresh();}
   return;
  }
  if(id==='nativeBudgetStop'){
   if(acceptanceBusy)return;
   acceptanceBusy=true;render();
   try{const r=await fetch('/api/native-budget',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify({op:'stop',id:data.native_budget.id})});const result=await r.json();if(!r.ok)throw Error(result.error);message(result.message);}
   catch(err){message(err.message,true);}finally{acceptanceBusy=false;await refresh();}
   return;
  }
  if(id==='capacityStop'){
   if(acceptanceBusy)return;
   acceptanceBusy=true;render();
   try{
    const r=await fetch('/api/capacity-assessment',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify({op:'stop',id:data.capacity_assessment.id})});
    const result=await r.json();if(!r.ok)throw Error(result.error);message(result.message);
   }catch(err){message(err.message,true);}finally{acceptanceBusy=false;await refresh();}
   return;
  }
  if(!['acceptObserve','acceptQuota','acceptTRShare','acceptStop'].includes(id)||acceptanceBusy)return;
  acceptanceBusy=true;render();try{const payload=id==='acceptStop'?{op:'stop',id:data.acceptance.id}:{op:'start',mode:({acceptQuota:'quota',acceptTRShare:'tr_share',acceptObserve:'observe'})[id]};const r=await fetch('/api/acceptance',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify(payload)});const result=await r.json();if(!r.ok)throw Error(result.error);message(result.message);}catch(err){message(err.message,true);}finally{acceptanceBusy=false;await refresh();}
 });
}
