from site_registry import is_excluded
"""Evidence-separated per-site health. Never infer Tracker success from HTTPS."""
GOOD={'original_path_healthy','origin_only','validated_override','speed_override','speed_keep_verified','speed_return_original'}
def build(sites,latest,transfer,policy,published,now):
    result=[];observed=latest.get('finished_at');fresh=isinstance(observed,(int,float)) and 0<=now-observed<7200
    tfresh=0<=now-transfer.get('at',0)<180
    for s in sites:
        if s.get('removed') or is_excluded(str(s.get('name',''))):continue
        domains=list(dict.fromkeys(s.get('website_domains',[])+s.get('tracker_domains',[])))
        if any(is_excluded(d) for d in domains):continue
        rows=[latest.get('domains',{}).get(d,{}) for d in domains]
        good=sum(r.get('action') in GOOD or r.get('action') in ('speed_shadow','speed_wait_confirmation') and r.get('baseline_healthy') for r in rows)
        tested=sum(bool(r) for r in rows);failed=sum(bool(r) and r.get('action') not in GOOD and r.get('action') not in ('speed_deferred','speed_shadow','speed_wait_confirmation') for r in rows)
        trackers={'working':0,'failed':0,'pending':0,'records':0,'last_success_at':None}
        for c in transfer.get('clients',{}).values():
            if not c.get('ok'):continue
            for d in s.get('tracker_domains',[]):
                r=c.get('trackers',{}).get(d,{})
                for k in ('working','failed','pending'):trackers[k]+=r.get(k,0)
                stamp=r.get('last_success_at')
                if stamp:trackers['last_success_at']=max(trackers['last_success_at'] or 0,stamp)
        trackers['records']=sum(trackers[k] for k in ('working','failed','pending'));trackers['fresh']=tfresh
        status='disabled' if not s.get('enabled') else 'stale' if not fresh else 'attention' if failed or tfresh and trackers['failed'] else 'pending' if tested<len(domains) or good<len(domains) else 'healthy'
        result.append({'id':s['id'],'name':s['name'],'enabled':s['enabled'],'desired_policy':policy.get('site_modes',{}).get(s['id'],'inherit'),'status':status,
                       'domains':len(domains),'tested':tested,'https_healthy':good,'https_at':observed,'https_fresh':fresh,'published':sum(d in published for d in domains),
                       'tracker':trackers,'transfer_gain':'待合格对照，不能从站点访问推断文件提速','scope':'Tracker为现有任务自然汇报；QB为轮换抽样，未出现不代表没有配置'})
    return result
