"""Evidence-scoped read-only diagnosis; observations never prove causal gains."""
import math
from benefit_reports import transfer_summary

def num(v):return v if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0 else None
def qualified(w,task=False):
    return (num(w.get('active_samples' if task else 'limited_active_samples')) or 0)>=5 and (num(w.get('span_seconds')) or 0)>=240

def recent_counter_evidence(transfer,points,now):
    names=set(transfer.get('clients',{}))
    def clean_counters(value):
        if not isinstance(value,dict) or set(value)!=names:return None
        if any(not isinstance(c,dict) or not isinstance(c.get('source'),str) or not c['source'] or
               num(c.get('up')) is None or num(c.get('down')) is None for c in value.values()):return None
        return value
    rows={}
    for point in points or []:
        at=num(point.get('at'))
        if at is not None and 0<=now-at<=900:
            counters=clean_counters(point.get('counters'))
            rows[at]={**point,'counters':counters,'all_clients_ok':bool(point.get('all_clients_ok') and counters)}
    at=num(transfer.get('at'))
    if at is not None and 0<=now-at<=900:
        counters=clean_counters({name:client.get('counters') for name,client in transfer.get('clients',{}).items()})
        rows[at]={'at':at,'counters':counters,'all_clients_ok':bool(len(names)==3 and counters and all(c.get('ok') for c in transfer['clients'].values()))}
    rows=sorted(rows.values(),key=lambda row:row['at'])
    def summary(name=None):
        selected=[{**row,'counters':({name:row['counters'][name]} if name and row.get('counters') else row.get('counters'))} for row in rows]
        value=transfer_summary(selected,now-900,now)
        valid=[(a['at'],b['at']) for a,b in zip(selected,selected[1:]) if transfer_summary([a,b],now-900,now)['counter_covered_seconds']>0]
        covered=value['counter_covered_seconds'];until=valid[-1][1] if valid else None
        value.update(period_seconds=900,since=valid[0][0] if valid else None,until=until,
            fresh=bool(until is not None and 0<=now-until<180),
            mean_up_Bps=round(value['uploaded_bytes']/covered) if covered else None,
            mean_down_Bps=round(value['downloaded_bytes']/covered) if covered else None,
            interpretation='下载器同源累计统计差额；按有效区间计算平均。不能据此认定新文件下载或优选提速，缺失区间不补零。')
        return value
    return {'total':summary(),'clients':{name:summary(name) for name in names}}


def explain(transfer,now,counter_points=None):
    at=num(transfer.get('at'));age=now-at if at is not None else None
    fresh=age is not None and 0<=age<180;manager=transfer.get('bandwidth_manager',{});items={}
    counters=recent_counter_evidence(transfer,counter_points,now)
    for name,c in transfer.get('clients',{}).items():
        row={'observed_at':at,'age_seconds':round(age) if age is not None else None,'fresh':fresh,
             'evidence_source':'下载器只读接口；每分钟采样，最近15分钟窗口','findings':[],
             'limitations':['实际吞吐收益尚无同条件对照','入站可达性尚未外部验证','磁盘与链路瓶颈尚无充分证据'],
             'instant':{k:num(c.get(k)) for k in ['up_Bps','down_Bps','active_uploads','active_downloads','peers']},
             'counter_window':counters['clients'].get(name,{})}
        f=row['findings']
        if not fresh:
            row.update(state='stale',title='观测已过期或时间异常',confidence='不足');items[name]=row;continue
        if not c.get('ok'):
            row.update(state='unavailable',title='下载器观测暂不可用',confidence='不足');items[name]=row;continue
        if manager.get('enabled') and manager.get('mode')=='real' and (manager.get('conflicted_records',0) or manager.get('pending_records',0)):
            f.append({'code':'dispatch_blocked','title':'带宽调度存在未解决记录','confidence':'配置证据',
                      'evidence':{'conflicts':manager.get('conflicted_records'),'pending':manager.get('pending_records')},
                      'next':'保留原额度，先核对限速归属与待确认写入。'})
        active=any((num(c.get(k)) or 0)>0 for k in ['active_uploads','active_downloads','up_Bps','down_Bps'])
        windows=transfer.get('window',{}).get('clients',{}).get(name,{})
        for direction,label,activity in [('up','上传','active_uploads'),('down','下载','active_downloads')]:
            w=windows.get(direction,{}) or {};task=w.get('task',{}) or {}
            direction_active=(num(c.get(activity)) or 0)>0 or (num(c.get(direction+'_Bps')) or 0)>0
            for kind,window,is_task in [('global',w,False),('task',task,True)]:
                if not qualified(window,is_task) or window.get('assessment')!='near_limit':continue
                title=label+('单任务额度' if is_task else '全局额度')+('附近持续有活跃样本' if direction_active else '历史窗口曾接近，当前未活跃')
                f.append({'code':direction+'_'+kind,'title':title,'confidence':'持续抽样，尚非因果对照','current_activity':direction_active,
                          'evidence':{'active_samples':window.get('active_samples' if is_task else 'limited_active_samples'),
                                      'near_samples':window.get('near_limit_samples'),'span_seconds':window.get('span_seconds'),
                                      'since':window.get('since'),'until':window.get('until'),
                                      'scope':'单任务额度约束的活跃任务' if is_task else '遵循会话额度的任务' if name=='pt_transmission' else '客户端全局流量'},
                          'next':'有稳定需求时，在原预算内开展短时配额对照；不根据瞬时值永久放开额度。'})
        if not active:
            counter=row['counter_window']
            if counter.get('fresh') and counter.get('counter_covered_seconds',0)>=240 and (counter.get('uploaded_bytes') or counter.get('downloaded_bytes')):
                row.update(state='intermittent_transfer',title='当前瞬时空闲，窗口内仍有累计流量',confidence='同源累计计数')
                row['next']='瞬时值不能代表整个窗口；继续核对持续需求与对端条件，保持现有额度。'
            else:
                row.update(state='idle_observation',title='当前快照未观测到传输',confidence='当前快照')
                row['next']='等待自然活跃任务；现有连接数或做种状态不能证明有传输需求。'
        elif any(x.get('current_activity') for x in f):
            row.update(state='possible_limit',title='活跃窗口提示额度可能形成约束',confidence='持续抽样')
            row['next']='先看具体额度的适用范围，再做合格的实际传输对照。'
        elif any(x['code']=='dispatch_blocked' for x in f):
            row.update(state='dispatch_blocked',title='先解决调度阻塞记录',confidence='配置证据')
            row['next']='先核对管理记录，不清空限速或恢复基线。'
        else:
            enough=any(qualified(windows.get(d,{}) or {}) or qualified((windows.get(d,{}) or {}).get('task',{}) or {},True) for d in ['up','down'])
            row.update(state='no_sustained_limit' if enough else 'insufficient_evidence',
                       title='窗口尚未确认持续额度约束' if enough else '活跃证据尚不足以定位瓶颈',confidence='有限')
            row['next']='结合对端稳定性和累计传输量继续观察，保留当前配额。'
        row['session_scope']=c.get('session_scope',{});reasons={}
        for value in c.get('trackers',{}).values():
            for category,count in value.get('reasons',{}).items():
                if category in ['账号或种子授权','连接或响应超时','DNS 解析','TLS 或证书','连接失败','站点或客户端回复异常']:reasons[category]=reasons.get(category,0)+(num(count) or 0)
        row['tracker_context']={'failed_record_categories':reasons,'scope':'QB轮换抽样；TR现有自然汇报记录，记录数不是独立任务或站点数','at':at};items[name]=row
    return {'observed_at':at,'evaluated_at':int(now),'fresh':fresh,'clients':items,
            'counter_window':counters['total'],'claim':'诊断分别显示瞬时快照与累计统计，不把速率波动归因为域名切换'}
