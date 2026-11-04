"""Read-only interpretation of known task/session speed ceilings; no client writes."""
import math


def number(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 else None


def task_summary(rows, direction, session_cap, transmission=False, unit=None, session_known=True):
    rate_key=('rateUpload' if direction=='up' else 'rateDownload') if transmission else ('upspeed' if direction=='up' else 'dlspeed')
    native='upload' if direction=='up' else 'download'
    summary={'tasks':len(rows),'own_limited_tasks':0,'unlimited_known_tasks':0,'unknown_tasks':0,
             'active_tasks':0,'active_known_tasks':0,'active_own_limited_tasks':0,'active_near_own_limit':0,
             'active_effective_min_Bps':None,'active_effective_max_Bps':None,'own_cap_distribution':{}}
    scope={'honoring_tasks':0,'bypassing_tasks':0,'unknown_tasks':0,'active_honoring':0,'active_bypassing':0,
           'honoring_rate_Bps':0,'bypassing_rate_Bps':0}
    active_caps=[]
    for task in rows:
        rate=number(task.get(rate_key)) or 0;active=rate>0
        honor=task.get('honorsSessionLimits') if transmission else True
        known=True
        if transmission:
            limited=task.get(native+'Limited');amount=number(task.get(native+'Limit'))
            own=amount*unit if limited is True and amount is not None and unit else None
            known=type(limited) is bool and type(honor) is bool and (not limited or own is not None)
            if honor is True and not session_known:known=False
            if task.get('group'):known=False
        else:
            amount=number(task.get('up_limit' if direction=='up' else 'dl_limit'))
            own=amount if amount else None;known=amount is not None
        if honor is True:
            scope['honoring_tasks']+=1;scope['active_honoring']+=int(active);scope['honoring_rate_Bps']+=rate
        elif honor is False:
            scope['bypassing_tasks']+=1;scope['active_bypassing']+=int(active);scope['bypassing_rate_Bps']+=rate
        else:scope['unknown_tasks']+=1
        summary['active_tasks']+=int(active)
        if not known:
            summary['unknown_tasks']+=1;continue
        if own is not None:
            summary['own_limited_tasks']+=1
            key=str(int(own));summary['own_cap_distribution'][key]=summary['own_cap_distribution'].get(key,0)+1
        constraints=[x for x in [own,session_cap if honor else None] if x is not None]
        effective=min(constraints) if constraints else None
        if effective is None:summary['unlimited_known_tasks']+=1
        if active:
            summary['active_known_tasks']+=1
            if effective is not None:active_caps.append(effective)
            if own and (not honor or session_cap is None or own<=session_cap):
                summary['active_own_limited_tasks']+=1
                summary['active_near_own_limit']+=int(rate>=own*.9)
    if active_caps:
        summary['active_effective_min_Bps']=min(active_caps);summary['active_effective_max_Bps']=max(active_caps)
    return summary,scope


def transmission_limits(rows, session):
    unit=session.get('units',{}).get('speed-bytes')
    if unit not in (1000,1024):unit=None
    result={'limit_schema':2,'task_caps':{},'session_scope':{},'native_speed_unit':unit}
    alt=session.get('alt-speed-enabled') is True
    for direction,native in [('up','upload'),('down','download')]:
        enabled=alt or session.get('speed-limit-'+direction+'-enabled') is True
        amount=number(session.get(('alt-speed-' if alt else 'speed-limit-')+direction))
        cap=amount*unit if enabled and amount is not None and unit else None
        session_known=(type(session.get('alt-speed-enabled')) is bool and
                       (alt or type(session.get('speed-limit-'+direction+'-enabled')) is bool) and
                       (not enabled or cap is not None))
        summary,scope=task_summary(rows,direction,cap,True,unit,session_known)
        if enabled and cap is None:scope['unit_or_limit_unconfirmed']=True
        scope['enabled']=enabled;scope['configured_Bps']=cap;scope['alternative']=alt
        result[direction+'_limit_Bps']=cap or 0
        result['global_'+direction+'_Bps']=scope['honoring_rate_Bps']
        result['global_active_'+('uploads' if direction=='up' else 'downloads')]=scope['active_honoring']
        result['task_caps'][direction]=summary;result['session_scope'][direction]=scope
    return result


def qb_limits(rows, upload_cap, download_cap):
    result={'limit_schema':2,'task_caps':{}}
    for direction,cap in [('up',upload_cap),('down',download_cap)]:
        result['task_caps'][direction]=task_summary(rows,direction,cap if cap else None)[0]
    return result
