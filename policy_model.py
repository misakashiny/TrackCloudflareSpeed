"""Shared, public-only policy normalization and verifiable execution snapshots."""
import hashlib
import json
import math
from copy import deepcopy
import speed_engine

PARALLEL_DOMAINS=4
GUARD_STALE_SECONDS=180
CANDIDATE_SCREEN={'max_seconds':90,'threads':4,'tcp_attempts':4,'ranked_limit':8}
GENERAL={'interval_seconds':(900,None,1800),'max_age_seconds':(900,86400,21600),
         'candidate_limit':(4,32,16),'candidate_budget_seconds':(5,60,25),
         'baseline_addresses':(1,4,3),'history_rounds':(24,2000,240)}

def normalized_settings(cfg):
    result={}
    for key,(low,high,default) in GENERAL.items():
        value=max(low,int(cfg.get(key,default)));result[key]=min(high,value) if high else value
    return result

def fingerprint(value):
    public={k:value[k] for k in ['version','registry_revision','settings','speed','parallel_domains','guard_stale_seconds','candidate_screen']}
    return hashlib.sha256(json.dumps(public,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def capture(cfg,speed,revision,version,at):
    value={'schema':1,'captured_at':int(at),'version':version,'registry_revision':revision,
           'settings':normalized_settings(cfg),'speed':{k:deepcopy(speed[k]) for k in speed_engine.DEFAULTS},
           'parallel_domains':PARALLEL_DOMAINS,'guard_stale_seconds':GUARD_STALE_SECONDS,'candidate_screen':dict(CANDIDATE_SCREEN)}
    value['fingerprint']=fingerprint(value);return value

def verified(value):
    try:
        return (isinstance(value,dict) and set(value)=={'schema','captured_at','version','registry_revision','settings','speed','parallel_domains','guard_stale_seconds','candidate_screen','fingerprint'}
                and set(value['settings'])==set(GENERAL) and set(value['speed'])==set(speed_engine.DEFAULTS)
                and value.get('schema')==1 and isinstance(value['captured_at'],int)
                and value['fingerprint']==fingerprint(value))
    except (KeyError,TypeError,ValueError,OverflowError):return False

def execution_view(cfg,control,revision,version,now,active,completed,probing):
    try:
        desired=capture(cfg,speed_engine.policy(cfg,control),revision,version,now)
        error=None
    except (TypeError,ValueError,OverflowError):
        desired=None;error='配置格式无效，无法确认待执行参数'
    previous=completed.get('runtime_policy') if isinstance(completed,dict) else None
    if not verified(previous):previous=None
    current=active if probing and verified(active) and active.get('version')==version else previous if not probing else None
    return {'observed_at':int(now),'desired':desired,'current':current,'last_completed':previous,
            'source':'running' if probing and current else 'last_completed' if current else 'unreported',
            'pending_next_round':bool(desired and current and desired['fingerprint']!=current['fingerprint']),
            'configuration_error':error,'execution_reported':current is not None}
