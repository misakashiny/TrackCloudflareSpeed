"""Small, bounded acceptance protocol. No downloader credentials or setters."""
import math

ACTIVE={'waiting','baseline','trial','recovering','after'}
FINAL={'complete','inconclusive','expired','stopped','recovery_blocked','failed'}

def request(value,now):
    if not isinstance(value,dict) or set(value)-{'id','op','mode','requested_at'}:raise ValueError('验收请求字段不正确')
    import re
    if not re.fullmatch(r'[a-f0-9]{32}',str(value.get('id',''))):raise ValueError('验收编号不正确')
    if value.get('op') not in ('start','stop'):raise ValueError('仅支持开始或停止验收')
    if not isinstance(value.get('requested_at'),int) or not 0<=now-value['requested_at']<=300:raise ValueError('验收请求已过期')
    if value['op']=='start' and value.get('mode') not in ('observe','quota','tr_share'):raise ValueError('请选择只读观察、单任务减额或 TR 共享试行')
    return value

def quota_witness_window(rows, now):
    """Use the shortest recent suffix spanning at least four minutes."""
    recent = [row for row in rows if 0 <= now - row.get('at', 0) <= 600]
    start = 0
    while len(recent) - start > 5 and recent[-1]['at'] - recent[start + 1]['at'] >= 240:
        start += 1
    return recent[start:]


def background_bounds(rows):
    """Allowed next rate that keeps the whole sample range comparable."""
    if not rows:
        return None
    cap = rows[0].get('cap')
    rates = [row.get('background_rate') for row in rows]
    if not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
        return None
    if any(not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0 for rate in rates):
        return None
    tolerance = max(65536, cap * .15)
    low, high = min(rates), max(rates)
    if high - low > tolerance:
        return None
    return {'min': max(0, high - tolerance), 'max': low + tolerance}


def witness_ready(rows,now,*,require_stable_background=False):
    rows=[r for r in rows if 0<=now-r.get('at',0)<=600]
    if len(rows)<5 or rows[-1]['at']-rows[0]['at']<240:return False,'等待同一任务至少5个样本、覆盖4分钟'
    if now-rows[-1]['at']>90:return False,'最后样本已过期'
    if any(not r.get('eligible') for r in rows):return False,'任务需求或原生额度条件改变'
    first=rows[0]
    if any(r.get('key')!=first.get('key') or r.get('policyHash')!=first.get('policyHash') or r.get('global')!=first.get('global') or r.get('cap')!=first.get('cap') for r in rows):return False,'任务、策略或原生额度不一致'
    peers=set(first.get('peer_fingerprints',[]))
    for r in rows:peers.intersection_update(r.get('peer_fingerprints',[]))
    if not peers or len(peers)<.8*max(len(r.get('peer_fingerprints',[])) for r in rows):return False,'上传对端变化，等待稳定样本'
    if any(b['uploaded']<a['uploaded'] or not 0<b['at']-a['at']<=100 for a,b in zip(rows,rows[1:])):return False,'累计计数回退或采样中断'
    if require_stable_background and background_bounds(rows) is None:
        return False,'其他任务流量未稳定，继续等待可比较的自然上传样本'
    return True,'稳定自然上传样本已齐'

def summarize(phases):
    rates={};allrows=[]
    for name in ('before','trial','after'):
        rows=phases.get(name,[]);allrows.extend(rows)
        if len(rows)<3 or rows[-1]['at']-rows[0]['at']<15:return {'valid':False,'reason':name+'阶段有效样本不足','gain_pct':None}
        elapsed=rows[-1]['at']-rows[0]['at'];delta=rows[-1]['uploaded']-rows[0]['uploaded']
        if delta<0 or any(b['uploaded']<a['uploaded'] for a,b in zip(rows,rows[1:])):return {'valid':False,'reason':'累计计数回退','gain_pct':None}
        rates[name]=delta/elapsed
    if any(b['at']<=a['at'] or b['uploaded']<a['uploaded'] for a,b in zip(allrows,allrows[1:])):
        return {'valid':False,'reason':'跨阶段累计计数回退或采样时间异常','rates':rates,'gain_pct':None}
    peers=set(allrows[0]['peer_fingerprints'])
    for r in allrows:peers.intersection_update(r['peer_fingerprints'])
    if not peers or len(peers)<.8*max(len(r['peer_fingerprints']) for r in allrows):return {'valid':False,'reason':'对端集合变化','rates':rates,'gain_pct':None}
    first=allrows[0]
    if any(r.get('global')!=first.get('global') or r.get('policyHash')!=first.get('policyHash') or r.get('key')!=first.get('key') for r in allrows):return {'valid':False,'reason':'其他原生额度或策略改变','rates':rates,'gain_pct':None}
    if background_bounds(allrows) is None:return {'valid':False,'reason':'其他任务流量变化过大','rates':rates,'gain_pct':None}
    a1,b,a2=(rates[k] for k in ('before','trial','after'))
    if min(a1,a2)<=0 or abs(a2-a1)/max(a1,a2)>.2:return {'valid':False,'reason':'前后基线未复现','rates':rates,'gain_pct':None}
    response=b<min(a1,a2)*.95
    return {'valid':True,'reason':'单任务上传对减额有响应' if response else '本次未检出稳定的减额响应','rates':rates,'quota_sensitivity':response,'gain_pct':None,
            'interpretation':'减额敏感性对照；不代表提高额度或域名优选带来的文件提速'}

def safe_status(job,now):
    public={k:job.get(k) for k in ('id','mode','status','created_at','deadline','updated_at','next_check_at','finished_at','reason','result','lease_expires_at','restored_at') if k in job}
    public['fresh']=bool(0<=now-job.get('updated_at',0)<180)
    public['stoppable']=job.get('status') in ACTIVE
    if job.get('mode')=='tr_share':
        public['protocol']='等待两个已完成TR任务的自然上传；合格后短时遵循现有会话上传上限，最多120秒；沿用当前总限速，不启停任务、不改文件'
        if job.get('shared_cap_Bps') is not None:public['shared_cap_Bps']=job['shared_cap_Bps']
    elif job.get('mode')=='quota':
        public['protocol']='只读等待；合格后单QB任务上传减额12.5%，原额度预留，租约最多120秒；不启停任务、不改文件'
    else:
        public['protocol']='仅观察现有QB任务的自然上传，不写入额度；合格或等待24小时后结束，可随时停止'
    return public
