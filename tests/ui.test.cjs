// Synthetic data only. No NAS, tracker account, or network access is required.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..'),elements=new Map();let scheduled=0,fetches=0;
const el=()=>({addEventListener(){},classList:{toggle(){},remove(){}},setAttribute(){},removeAttribute(){},value:'',focus(){}});
const ctx=vm.createContext({console,URL,URLSearchParams,Intl,Set,Map,Date,CSS:{escape:s=>s},document:{addEventListener(){},querySelector:s=>{if(!elements.has(s))elements.set(s,el());return elements.get(s)},querySelectorAll:()=>[],getElementById:()=>null},window:{addEventListener(){},scrollTo(){},scrollY:0},location:{hash:'',hostname:'nas.example.test'},history:{replaceState(){},pushState(){}},setTimeout:()=>{scheduled++;return 1},clearTimeout(){}});
for(const f of ['speed.js','acceptance.js','app.js']){let s=fs.readFileSync(path.join(root,'web',f),'utf8');if(f==='app.js')s=s.replace(/registerSpeedHandlers\(\);\s*registerAcceptanceHandlers\(\);\s*route\(\);\s*refresh\(\);\s*$/,'');vm.runInContext(s,ctx);}
const run=s=>vm.runInContext(s,ctx),now=1700000000;
ctx.fixture={now,worker_healthy:true,heartbeat_at:now,domains:['example.org'],domain_meta:{'example.org':{site_name:'Example',roles:['tracker'],enabled:true}},sites:[{id:'sample',name:'Example',enabled:true,website_domains:[],tracker_domains:['example.org']}],site_health:[{id:'sample',name:'Example',enabled:true,domains:1,https_healthy:0,https_fresh:true,https_at:now,published:0,desired_policy:'inherit',tracker:{records:2,working:2,failed:0,pending:0,fresh:true}}],published:{},entries:{},latest:{finished_at:now,domains:{'example.org':{action:'speed_unstable'}},summary:{classifications:{}},duration_seconds:10},history:[],changes:[],alerts:[],transfer:{at:now,fresh:true,clients:{}},speed_policy:{mode:'auto',site_modes:{}},runtime_policy:{current:null,last_completed:null},next_run:now+1800,progress:{},last_requested_at:0};
run('data=fixture');
(async()=>{
 const cases=[['working',{records:2,working:2,fresh:true}],['failed',{records:2,failed:1,fresh:true}],['pending',{records:2,pending:2,fresh:true}],['unknown',{records:0}],['stale',{records:2,working:2,fresh:false}]];
 for(const [kind,t] of cases){ctx.tracker=t;assert.equal(run('trackerState({enabled:true,tracker}).kind'),kind)}
 assert.equal(run("validateDomainText('https://example.org/ example.org',false).length"),1);
 assert.throws(()=>run("validateDomainText('https://example.org/announce?passkey=synthetic',true)"));
 assert.throws(()=>run("validateDomainText('user@example.org',false)"));
 for(const f of ['homeView()','siteWorkspace()','speedView()','speedView("transfer")','speedView("settings")','reportView(false)','advancedView()'])assert.equal(typeof run(f),'string');
 run("siteId='sample'");const html=run('siteWorkspace()');assert(html.includes('已观测汇报正常'));assert(html.includes('连接待观察'));
 assert(!run('speedView()').includes('speedPolicyForm'));assert(run('speedView("settings")').includes('speedPolicyForm'));assert(!run('reportView(false)').includes('capacityStop'));
 ctx.fetch=async()=>{fetches++;return {ok:true,json:async()=>({records:[],summary:{}})}};
 await run('loadLogs()');const before=scheduled;run('logsView()');run('logsView()');assert.equal(scheduled,before,'empty logs must not schedule a request loop');assert.equal(fetches,1);
 ctx.fetch=async()=>{throw Error('synthetic offline')};await run('refresh()');assert.equal(run('statusUnavailable'),true);assert(run('homeView()').includes('状态读取失败'));assert(!run('homeView()').includes('优选服务运行正常'));assert.equal(elements.get('#addSite').disabled,true);
 await assert.rejects(()=>run("siteRequest({name:'blocked'})"),/状态读取失败/);
 ctx.fetch=async()=>({ok:true,json:async()=>ctx.fixture});await run('refresh()');assert.equal(run('statusUnavailable'),false);assert.equal(elements.get('#addSite').disabled,false);
 const count=scheduled;run('lastInteractionAt=Date.now()');await run('refresh(true)');assert.equal(scheduled,count+1,'automatic rendering defers while interacting');
 console.log('PASS: tracker states, domain validation, separated views, empty-log loop, offline/recovery state, write guard, interaction deferral');
})().catch(e=>{console.error(e);process.exitCode=1});
