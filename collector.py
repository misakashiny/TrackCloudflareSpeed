"""Optional generic QB/TR observer. Credentials stay in memory; no task setters."""
import base64,concurrent.futures,hashlib,http.cookiejar,json,os,re,signal,time
from pathlib import Path
from urllib.parse import urlsplit,urlencode
import urllib.request,urllib.error
from site_registry import load_registry,domain_index,is_excluded
from state_io import read,atomic
import report_store
from transfer_limits import qb_limits,transmission_limits

MAX_RESPONSE=16*1024*1024
ROOT=Path(os.environ.get('COLLECTOR_ROOT','/project'))
CONFIG=Path(os.environ.get('COLLECTOR_CONFIG','/private/downloaders.json'))

def settings(path):
    raw=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw,dict) or set(raw)-{'clients'} or not isinstance(raw.get('clients'),list) or len(raw['clients'])>16:raise ValueError('invalid collector config')
    ids=set();endpoints=set();clients=[]
    for c in raw['clients']:
        if not isinstance(c,dict) or set(c)-{'id','name','type','url','enabled','auth','username_env','password_env','username_file','password_file'}:raise ValueError('invalid client fields')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,39}',str(c.get('id',''))) or c['id'] in ids:raise ValueError('invalid/duplicate id')
        ids.add(c['id'])
        if c.get('type') not in ('qb','tr') or type(c.get('enabled',True)) is not bool:raise ValueError('invalid client type/enabled')
        name=c.get('name',c['id'])
        if not isinstance(name,str) or not 1<=len(name)<=60 or any(ord(x)<32 for x in name):raise ValueError('invalid display name')
        u=urlsplit(c.get('url',''))
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment or not 1<=(u.port or (443 if u.scheme=='https' else 80))<=65535:raise ValueError('invalid URL')
        if any(ord(x)<=32 for x in c['url']) or '%' in u.netloc:raise ValueError('invalid URL characters')
        key=(u.scheme,u.hostname.lower(),u.port or (443 if u.scheme=='https' else 80),u.path.rstrip('/'))
        if key in endpoints:raise ValueError('duplicate endpoint would double-count traffic')
        endpoints.add(key)
        if c.get('auth','credentials') not in ('none','credentials'):raise ValueError('invalid auth')
        if c.get('auth','credentials')=='credentials':
            for field in ('username','password'):
                refs=[c.get(field+'_env'),c.get(field+'_file')]
                if sum(bool(x) for x in refs)!=1 or any(x is not None and not isinstance(x,str) for x in refs):raise ValueError('one credential reference required')
                if refs[0] and not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,100}',refs[0]):raise ValueError('invalid environment reference')
                if refs[1] and not Path(refs[1]).is_absolute():raise ValueError('credential path must be absolute')
        if c.get('enabled',True):clients.append({**c,'name':name,'url':c['url'].rstrip('/')})
    return clients

def credential(c,field):
    if c.get('auth')=='none':return ''
    if c.get(field+'_env'):
        value=os.environ[c[field+'_env']]
    else:
        p=Path(c[field+'_file'])
        if p.stat().st_size>4096:raise ValueError('credential too large')
        value=p.read_text(encoding='utf-8').removesuffix('\n').removesuffix('\r')
    if not value or '\r' in value or '\n' in value or len(value)>4096:raise ValueError('invalid credential')
    return value

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class Transport:
    def __init__(self,c):
        self.c=c;self.deadline=time.monotonic()+12
        self.op=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(self,url,body=None,headers=None):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('client budget exhausted')
        req=urllib.request.Request(url,data=body,headers=headers or {})
        with self.op.open(req,timeout=min(3,remaining)) as r:
            chunks=[];size=0
            while True:
                if time.monotonic()>self.deadline:raise TimeoutError('client budget exhausted')
                chunk=r.read1(min(65536,MAX_RESPONSE+1-size))
                if not chunk:break
                chunks.append(chunk);size+=len(chunk)
                if size>MAX_RESPONSE:raise ValueError('response too large')
            return b''.join(chunks)

class QB(Transport):
    GETS={'/api/v2/transfer/info','/api/v2/torrents/info','/api/v2/torrents/trackers','/api/v2/sync/maindata'}
    def __init__(self,c):
        super().__init__(c)
        if c.get('auth')!='none':
            u=urlsplit(c['url']);origin=u.scheme+'://'+u.netloc
            raw=self.request(c['url']+'/api/v2/auth/login',urlencode({'username':credential(c,'username'),'password':credential(c,'password')}).encode(),{'Referer':c['url']+'/','Origin':origin,'Content-Type':'application/x-www-form-urlencoded'})
            if raw.strip()!=b'Ok.':raise PermissionError('login rejected')
    def get(self,path,params=None):
        if path not in self.GETS:raise ValueError('read-only endpoint rejected')
        return json.loads(self.request(self.c['url']+path+('?' +urlencode(params) if params else '')))

class TR(Transport):
    METHODS={'session-get','session-stats','torrent-get'}
    def __init__(self,c):
        super().__init__(c);self.sid=''
        self.auth=('Basic '+base64.b64encode((credential(c,'username')+':'+credential(c,'password')).encode()).decode()) if c.get('auth')!='none' else None
    def rpc(self,method,args=None):
        if method not in self.METHODS:raise ValueError('read-only RPC rejected')
        headers={'Content-Type':'application/json'}
        if self.auth:headers['Authorization']=self.auth
        for _ in range(2):
            headers['X-Transmission-Session-Id']=self.sid
            try:
                result=json.loads(self.request(self.c['url'],json.dumps({'method':method,'arguments':args or {}}).encode(),headers))
                if result.get('result')!='success':raise ValueError('RPC rejected')
                return result['arguments']
            except urllib.error.HTTPError as e:
                if e.code!=409:raise
                self.sid=e.headers.get('X-Transmission-Session-Id','');e.close()
                if not self.sid or len(self.sid)>1024:raise ValueError('invalid RPC session')
        raise PermissionError('RPC session negotiation failed')

def number(v):
    return v if isinstance(v,(int,float)) and not isinstance(v,bool) and 0<=v<1e20 else 0

def tracker_rows(rows,kind,allowed):
    result={}
    for r in rows:
        try:host=(urlsplit(r.get('announce') or r.get('url') or '').hostname or '').lower()
        except ValueError:continue
        if host not in allowed or is_excluded(host):continue
        item=result.setdefault(host,{'working':0,'failed':0,'pending':0,'reasons':{},'last_announce_at':0,'last_success_at':0})
        if kind=='qb':state={2:'working',4:'failed'}.get(r.get('status'),'pending')
        else:
            at=int(number(r.get('lastAnnounceTime')))
            state='pending' if not at or type(r.get('lastAnnounceSucceeded')) is not bool else 'working' if r['lastAnnounceSucceeded'] else 'failed'
            item['last_announce_at']=max(item['last_announce_at'],at)
            if state=='working':item['last_success_at']=max(item['last_success_at'],at)
        item[state]+=1
        if state=='failed':
            # Never persist the remote error string: it may echo an announce URL/key.
            reason='站点或客户端回复异常';item['reasons'][reason]=item['reasons'].get(reason,0)+1
    return result

def collect_client(c,allowed,prior):
    if c['type']=='qb':
        api=QB(c);info=api.get('/api/v2/transfer/info');rows=api.get('/api/v2/torrents/info')
        if not isinstance(rows,list) or len(rows)>20000:raise ValueError('task response invalid')
        selected=sorted((x for x in rows if re.fullmatch('[a-fA-F0-9]{40,64}',str(x.get('hash','')))),key=lambda x:x['hash'])
        start=int(prior.get('tracker_cursor',0))%max(1,len(selected));sample=(selected+selected)[start:start+min(6,len(selected))];tracks=[]
        for x in sample:tracks.extend(api.get('/api/v2/torrents/trackers',{'hash':x['hash']}))
        result={'ok':True,'tasks':len(rows),'down_Bps':number(info.get('dl_info_speed')),'up_Bps':number(info.get('up_info_speed')),'peers':sum(number(x.get('num_leechs'))+number(x.get('num_seeds')) for x in rows),'active_uploads':sum(number(x.get('upspeed'))>0 for x in rows),'active_downloads':sum(number(x.get('dlspeed'))>0 for x in rows),'up_limit_Bps':number(info.get('up_rate_limit')),'down_limit_Bps':number(info.get('dl_rate_limit')),'sampled_tasks':len(sample),'tracker_cursor':start+len(sample),'trackers':tracker_rows(tracks,'qb',allowed)}
        result.update(qb_limits(rows,result['up_limit_Bps'],result['down_limit_Bps']))
        try:
            sync=api.get('/api/v2/sync/maindata',{'rid':int(prior.get('counter_rid',0))});old={} if sync.get('full_update') else prior.get('counters',{});ss=sync.get('server_state',{})
            up=ss.get('alltime_ul',old.get('up'));down=ss.get('alltime_dl',old.get('down'))
            if all(type(v) in (int,float) and 0<=v<1e20 for v in (up,down)):result.update(counters={'up':up,'down':down},counter_rid=sync.get('rid',0))
        except (OSError,ValueError,KeyError):pass
    else:
        api=TR(c);info=api.rpc('session-stats');session=api.rpc('session-get')
        rows=api.rpc('torrent-get',{'fields':['status','rateDownload','rateUpload','peersConnected','trackerStats','honorsSessionLimits','uploadLimited','uploadLimit','downloadLimited','downloadLimit','group']})['torrents']
        if not isinstance(rows,list) or len(rows)>20000:raise ValueError('task response invalid')
        result={'ok':True,'tasks':len(rows),'down_Bps':number(info.get('downloadSpeed')),'up_Bps':number(info.get('uploadSpeed')),'active_uploads':sum(number(x.get('rateUpload'))>0 for x in rows),'active_downloads':sum(number(x.get('rateDownload'))>0 for x in rows),'peers':sum(number(x.get('peersConnected')) for x in rows),'trackers':tracker_rows([x for row in rows for x in row.get('trackerStats',[])],'tr',allowed)}
        result.update(transmission_limits(rows,session))
        result['alternative_limits']=session.get('alt-speed-enabled') if type(session.get('alt-speed-enabled')) is bool else None
        totals=info.get('cumulative-stats',{})
        if all(type(totals.get(k)) in (int,float) and 0<=totals[k]<1e20 for k in ['uploadedBytes','downloadedBytes']):result['counters']={'up':totals['uploadedBytes'],'down':totals['downloadedBytes']}
    if result.get('counters'):result['counters']['source']=c['type']+'-'+hashlib.sha256(c['url'].encode()).hexdigest()[:20]
    return result

def run_once(root=ROOT,config=CONFIG):
    state=root/'runtime/state';directory=state/'transfer';previous=read(directory/'latest.json',{})
    now=int(time.time());result={'at':now,'read_only':True,'clients':{},'tracker_scope':'TR为自然汇报；QB每台每轮最多轮换抽样6个任务；只显示站点清单内域名，不强制汇报','bandwidth_manager':{'enabled':False,'ownership_checked':False},'hints':['采集器仅观察，不修改限速或下载器任务。']}
    clients=settings(config)
    cfg=read(root/'config/settings.json',{});registry=load_registry(root/'runtime/control/sites.json',cfg.get('domains',[]));allowed=set(domain_index(registry,enabled_only=True))
    def observe(c):
        source=c['type']+'-'+hashlib.sha256(c['url'].encode()).hexdigest()[:20]
        old=previous.get('clients',{}).get(c['id'],{})
        if old.get('source_id')!=source:old={}
        try:value=collect_client(c,allowed,old)
        except Exception as e:value={'ok':False,'error':type(e).__name__,'message':'采集失败，请检查地址、凭据引用或接口版本；不会修改下载器'}
        return c['id'],{**value,'name':c['name'],'type':c['type'],'source_id':source,'observed_at':int(time.time())}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result['clients']=dict(pool.map(observe,clients))
    result['at']=int(time.time())
    result['total_up_Bps']=sum(x.get('up_Bps',0) for x in result['clients'].values());result['total_down_Bps']=sum(x.get('down_Bps',0) for x in result['clients'].values())
    atomic(directory/'latest.json',result);report_store.update(root,result)
    atomic(directory/'collector.json',{'at':result['at'],'ok':True,'configured_clients':len(clients),'successful_clients':sum(c.get('ok',False) for c in result['clients'].values())})
    return result

def main():
    import argparse,threading
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');args=p.parse_args();stop=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:stop.set())
    while not stop.is_set():
        started=time.monotonic()
        try:run_once()
        except Exception as e:
            # Overwrite old client data so removed/invalid config never looks healthy.
            now=int(time.time());atomic(ROOT/'runtime/state/transfer/latest.json',{'at':now,'read_only':True,'clients':{},'collector_error':type(e).__name__,'hints':['采集配置不可用，请检查私有配置；原样本保留，当前客户端状态未确认。']})
            atomic(ROOT/'runtime/state/transfer/collector.json',{'at':now,'ok':False,'error':type(e).__name__})
        if args.once:return
        stop.wait(max(1,60-(time.monotonic()-started)))

if __name__=='__main__':main()
