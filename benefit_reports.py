from site_registry import is_excluded
"""Compact seven-day evidence; matched HTTPS pairs are separate from traffic."""
import statistics

def compact_round(r):
    rows={}
    for domain,row in r.get('domains',{}).items():
        if is_excluded(domain):continue
        s=row.get('speed',{});m=s.get('metrics',{});a=m.get(s.get('original_best_ip'),{});b=m.get(s.get('current_ip'),{})
        pair=bool(a.get('stable') and b.get('stable') and a.get('http')==b.get('http') and a.get('median_ms',0)>0 and b.get('median_ms') is not None)
        rows[domain]={'action':row.get('action'),'baseline_healthy':row.get('baseline_healthy'),
                      'before_ms':a['median_ms'] if pair else None,'after_ms':b['median_ms'] if pair else None,'paired':pair}
    return {'at':r.get('finished_at'),'started_at':r.get('started_at'),'domains':rows}

def capacity_evidence(points,now):
    rows=[p for p in points if 0<=now-p.get('at',0)<=86400 and p.get('all_clients_ok') and p.get('counters')]
    windows=[]
    for i,r in enumerate(rows):
        window=rows[max(0,i-5):i+1]
        if len(window)<5 or window[-1]['at']-window[0]['at']<240 or any(not 0<b['at']-a['at']<=100 for a,b in zip(window,window[1:])):continue
        rates=[]
        for a,b in zip(window,window[1:]):
            ca,cb=a['counters'],b['counters']
            if len(ca)!=3 or set(ca)!=set(cb) or any(ca[n]['source']!=cb[n]['source'] or cb[n]['up']<ca[n]['up'] for n in ca):break
            rates.append(sum(cb[n]['up']-ca[n]['up'] for n in ca)/(b['at']-a['at']))
        if len(rates)==len(window)-1 and min(rates)>0:windows.append({'at':r['at'],'lower_Bps':int(min(rates)),'mean_Bps':round(statistics.mean(rates)),'span_seconds':window[-1]['at']-window[0]['at'],'basis':'三个客户端同源累计上传计数差额，连续采样间隔均不超过100秒'})
    best=max(windows,key=lambda x:x['lower_Bps'],default=None)
    quantum=32768
    candidate=best['lower_Bps']*4//(5*quantum)*quantum if best else None
    return {'at':now,'qualified_windows':len(windows),'sustained_upload_lower_bound':best,'wan_capacity_Bps':None,
            'meaning':'现有PT流量持续下界，含协议/路由影响；并非宽带标称上行或完整容量',
            'recommended_initial_budget_Bps':candidate or None,
            'budget_applied':False,'blockers':['宽带完整上行容量尚未确认','持续统一预算及按需分配收益尚未通过验收'] + (['持续上传下界不足以形成 32 KiB/s 的试行预算参考'] if candidate==0 else [])}

def transfer_summary(points,start,end):
    rows=sorted((p for p in points if start<=p.get('at',0)<=end),key=lambda p:p['at'])
    totals={'up':0,'down':0};covered=0;counter_resets=0;excluded=0
    for a,b in zip(rows,rows[1:]):
        dt=b['at']-a['at']
        if not 0<dt<=100 or not a.get('all_clients_ok') or not b.get('all_clients_ok'):excluded+=1;continue
        # Counter deltas require the same complete source set and no rollback.
        ca,cb=a.get('counters'),b.get('counters')
        if ca and cb and set(ca)==set(cb):
            if any(cb[n].get('source')!=ca[n].get('source') or any(cb[n][d]<ca[n][d] for d in ('up','down')) for n in ca):counter_resets+=1;continue
            for d in totals:totals[d]+=sum(cb[n][d]-ca[n][d] for n in ca)
            covered+=dt
    return {'samples':len(rows),'first_at':rows[0]['at'] if rows else None,'last_at':rows[-1]['at'] if rows else None,
            'counter_covered_seconds':covered,'uploaded_bytes':totals['up'] if covered else None,'downloaded_bytes':totals['down'] if covered else None,
            'counter_resets':counter_resets,'excluded_intervals':excluded,'transfer_gain_pct':None,'interpretation':'累计计数差额为实际流量；流量多少不等于优选提速收益'}

def budget_trial_counts(campaigns,start,end):
    records={}
    for campaign in campaigns:
        for scope,stage in campaign.get('completed_stages',{}).items():
            if scope not in ('qb_pilot','all') or not isinstance(stage,dict):continue
            at,restored=stage.get('finished_at'),stage.get('restored_at')
            if (isinstance(at,(int,float)) and isinstance(restored,(int,float)) and start<=at<=end and
                0<restored<=at and stage.get('result',{}).get('valid') is True and stage.get('lease_id')):
                records[(scope,stage['lease_id'])]=True
    return {scope:sum(key[0]==scope for key in records) for scope in ('qb_pilot','all')}


def build(rounds,points,jobs,now,budget_jobs=None):
    reports=[]
    for hours in (24,168):
        start=now-hours*3600;rs=sorted((r for r in rounds if start<=r.get('at',0)<=now),key=lambda r:r['at'])
        pairs=[r for x in rs for r in x['domains'].values() if r['paired']]
        gains=[100*(r['before_ms']-r['after_ms'])/r['before_ms'] for r in pairs]
        span=rs[-1]['at']-rs[0]['at'] if len(rs)>1 else 0
        maxgap=max((b['at']-a['at'] for a,b in zip(rs,rs[1:])),default=None)
        full=bool(rs and rs[0]['at']<=start+2100 and now-rs[-1]['at']<=2100 and maxgap is not None and maxgap<=3900)
        tr=transfer_summary(points,start,now)
        reports.append({'hours':hours,'window_start':start,'window_end':now,'domain_rounds':len(rs),'domain_since':rs[0]['at'] if rs else None,'domain_until':rs[-1]['at'] if rs else None,
                        'domain_span_seconds':span,'max_round_gap_seconds':maxgap,'full_window':full,'status':'complete' if full and tr['counter_covered_seconds']>=hours*3600*.9 else 'partial',
                        'https_paired_comparisons':len(pairs),'https_median_gain_pct':round(statistics.median(gains),1) if gains else None,
                        'https_faster':sum(g>0 for g in gains),'https_slower':sum(g<0 for g in gains),'transfer':tr,
                        'acceptance_jobs':sum(start<=j.get('finished_at',0)<=now for j in jobs),'qualified_transfer_trials':sum(start<=j.get('finished_at',0)<=now and j.get('mode')=='quota' and j.get('result',{}).get('valid',False) for j in jobs),
                        'qualified_tr_sharing_trials':sum(start<=j.get('finished_at',0)<=now and j.get('mode')=='tr_share' and j.get('status')=='complete' and isinstance(j.get('result'),dict) and j['result'].get('valid') is True and isinstance(j.get('restored_at'),(int,float)) and 0<j['restored_at']<=j['finished_at'] for j in jobs),
                        'file_transfer_gain_pct':None,'limitations':['访问百分比仅为同轮HTTPS根路径配对，不代表Tracker业务或文件传输','无数据和采样缺口保持缺失，不补零、不外推','减额试验仅判断额度敏感性，不构成提高额度后的提速证明','TR共享对照必须有原状态恢复确认；仅验收现有上限的执行与恢复，不认定提速收益']})
    for report in reports:
        counts=budget_trial_counts(budget_jobs or [],report['window_start'],now)
        report.update(qualified_native_qb_trials=counts['qb_pilot'],qualified_global_budget_trials=counts['all'])
    return {'at':now,'reports':reports,'capacity':capacity_evidence(points,now)}

def alerts(latest,transfer,acceptance,guard,now,native_budget=None,budget_acceptance=None):
    out=[]
    def add(code,level,text,at):out.append({'code':code,'level':level,'message':text,'evidence_at':at,'detected_at':now})
    if not 0<=now-transfer.get('at',0)<180:add('observer.stale','warning','传输采样已过期，暂停当前瓶颈和收益判断',transfer.get('at'))
    for n,c in transfer.get('clients',{}).items():
        if not c.get('ok'):add('client.unavailable.'+n,'warning','下载器观测失败：'+n,transfer.get('at'))
    m=transfer.get('bandwidth_manager',{})
    if m.get('conflicted_records') or m.get('pending_records'):add('limits.ownership','warning','种子港存在限速冲突或未确认写入；新对照不能开始',transfer.get('at'))
    if acceptance.get('status')=='recovery_blocked':add('acceptance.restore','error','短时验收原额度尚未回读确认，保留恢复记录并阻止新对照',acceptance.get('updated_at'))
    if acceptance.get('stoppable') and not acceptance.get('fresh'):add('acceptance.stale','warning','验收执行状态未更新，请检查验收服务',acceptance.get('updated_at'))
    native=native_budget or {};budget=budget_acceptance or {}
    if native.get('state')=='blocked' or budget.get('status')=='recovery_blocked':
        add('budget.restore','error','预算试行的原额度恢复尚未确认；保留恢复记录并阻止新试行',budget.get('updated_at') or (native.get('observedAt',0)/1000))
    if budget.get('stoppable') and not budget.get('fresh'):
        add('budget.stale','warning','预算对照排期状态未更新，请检查验收服务',budget.get('updated_at'))
    if latest.get('finished_at') and now-latest['finished_at']>7200:add('probe.stale','warning','域名检测超过两小时未完成，当前站点状态可能过期',latest.get('finished_at'))
    failed=[d for d,r in latest.get('domains',{}).items() if r.get('action') in ('origin_connection_failed','dns_unresolved','speed_unstable')]
    if failed:add('domains.unhealthy','warning',f'{len(failed)}个域名本轮未稳定通过；请查看站点总览与自然汇报',latest.get('finished_at'))
    return out
