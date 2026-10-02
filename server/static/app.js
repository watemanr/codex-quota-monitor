'use strict';
const $ = s => document.querySelector(s);
const state = {authenticated:false, csrf:'', authGeneration:0, data:null, tiboData:null, loading:false, view:'overview', showAllEvents:false, eventsContext:''};
sessionStorage.removeItem('quota-read-token');
const apiURL = path => new URL(path, location.href.endsWith('/') ? location.href : location.href + '/').href;
const formatTime = v => v == null ? '未提供' : new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v * 1000));
const pct = n => new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(n);
function el(tag, cls, text){const e=document.createElement(tag); if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
async function api(path, options={}){
  const headers={Accept:'application/json',...options.headers};
  if(options.method&&options.method!=='GET'&&state.csrf&&!headers.Authorization)headers['X-CSRF-Token']=state.csrf;
  const res=await fetch(apiURL(path),{...options,credentials:'same-origin',headers});
  let data;try{data=await res.json();}catch{throw new Error(`服务器响应异常（${res.status}）`);}
  if(!res.ok){const error=new Error(data.error||`请求失败（${res.status}）`);error.status=res.status;throw error;}return data;
}
let tiboLoading=false;
function tiboTime(seconds){
  return seconds==null?'时间未知':new Intl.DateTimeFormat('zh-CN',
    {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})
    .format(new Date(seconds*1000))+' 北京时间';
}
function renderTibo(data){
  state.tiboData=data;if(state.view==='overview')renderTimeline();
  const root=$('#tibo-posts');root.replaceChildren();
  const posts=Array.isArray(data.posts)?data.posts:[];
  if(data.fetched_at){
    const stale=Date.now()/1000-data.fetched_at>1800;
    $('#tibo-status').textContent='RSS 上次成功检查于 '+tiboTime(data.fetched_at)+
      (posts.length?' · 最新帖文发布于 '+tiboTime(posts[0].published_at):'')+
      ' · 服务器约每 10 分钟检查一次'+(data.last_error?' · 最近一次检查失败，以下为旧缓存':stale?' · 缓存超过 30 分钟，请核对原帖':'');
  }else $('#tibo-status').textContent=data.last_error?'暂时无法建立文字缓存，请稍后刷新或打开 X 原页。':'正在建立首次文字缓存，请稍后刷新。';
  if(!posts.length){root.append(el('p','tibo-empty','还没有可显示的帖文。'));return;}
  posts.forEach(post=>{
    const row=el('article','tibo-post'),head=el('div','tibo-post-head');
    const left=el('div');left.append(el('time','',tiboTime(post.published_at)));
    if(/\b(reset|resets|usage|limits?|quota)\b|重置|额度/i.test(post.text))left.append(el('span','tibo-topic','提及额度'));
    const link=el('a','','查看 X 原帖 ↗');link.href=post.url;link.target='_blank';link.rel='noopener noreferrer';
    head.append(left,link);row.append(head,el('p','',post.text));root.append(row);
  });
}
async function loadTibo(){
  if(tiboLoading||!state.authenticated)return;
  tiboLoading=true;const generation=state.authGeneration;
  try{const data=await api('api/tibo');if(generation===state.authGeneration)renderTibo(data);}
  catch(error){if(generation===state.authGeneration)$('#tibo-status').textContent='读取文字缓存失败：'+error.message;}
  finally{tiboLoading=false;}
}
function relative(seconds){if(seconds<=0)return '已到期，等待新采样';const h=Math.floor(seconds/3600),m=Math.floor(seconds%3600/60);return h>=24?`${Math.floor(h/24)} 天 ${h%24} 小时后`:`${h} 小时 ${m} 分钟后`;}
function showLogin(){ $('#login').hidden=false;$('#dashboard').hidden=true;$('#logout').hidden=true;$('#change-password').hidden=true;$('#connection').textContent='等待连接';}
function syncAccounts(accounts){
  const prev=$('#account').value;$('#account').replaceChildren(new Option('全部账号',''));
  accounts.forEach(a=>$('#account').append(new Option(a.account,a.account)));
  if(accounts.some(a=>a.account===prev))$('#account').value=prev;
  if(state.view==='analysis'){$('#account').options[0].disabled=true;if(!$('#account').value)$('#account').value=accounts[0]?.account||'';}
}
async function loadAccounts(){syncAccounts((await api('api/accounts')).accounts);}
function dataPath(){if(state.view==='analysis')return analyticsPath();const account=$('#account').value;if(state.view==='more')return 'api/forecast'+(account?'?account='+encodeURIComponent(account):'');return (account?'api/dashboard?account='+encodeURIComponent(account)+'&':'api/overview?')+'days='+$('#days').value;}
async function refresh(accounts=false){
  if(state.loading){state.pending=true;return;}state.loading=true;$('#refresh').disabled=true;
  const generation=state.authGeneration;if(state.view==='analysis')beginAnalyticsLoad();
  try{
    if(accounts)await loadAccounts();
    const path=dataPath(),data=await api(path);
    if(state.authGeneration!==generation)return;
    if(path!==dataPath()){state.pending=true;return;}
    state.data=data;if(data.accounts)syncAccounts(data.accounts);
    $('#login').hidden=true;$('#dashboard').hidden=false;$('#logout').hidden=false;$('#change-password').hidden=false;
    $('#connection').textContent='服务已连接';$('#notice').hidden=true;render();if(state.view==='overview')loadTibo();
  }catch(err){
    if(state.authGeneration!==generation)return;
    if(err.status===401){clearLoginState();$('#login-error').textContent=err.message;}
    else if($('#dashboard').hidden){$('#login-error').textContent=err.message;}
    else{$('#notice').textContent='刷新失败：'+err.message+'。当前显示的是上次成功读取的数据。';$('#notice').hidden=false;$('#connection').textContent='连接异常';}
  }finally{state.loading=false;$('#refresh').disabled=false;if(state.view==='analysis')$('#analysis-pending').hidden=true;if(state.pending){state.pending=false;if(state.authenticated)refresh();}}
}
function renderCard(w){
  const card=el('article',`quota-card ${w.window_minutes===10080?'week':''}`);const top=el('div','card-top');top.append(el('h2','',w.label),el('span',`chip ${w.stale?'stale':''}`,w.stale?'采样已过期':'最新采样'));card.append(top);
  const reading=el('div','reading');reading.append(el('strong','',pct(w.remaining_percent)),el('span','','%'),el('small','','剩余'));card.append(reading);
  const meter=el('div','meter'),fill=el('span');fill.style.width=`${w.remaining_percent}%`;meter.append(fill);card.append(meter);
  const meta=el('div','meta'),reset=el('div'),eta=el('div');reset.append(el('strong','',formatTime(w.resets_at)),el('span','','预计重置时间'));eta.append(el('strong','',w.resets_at?relative(w.resets_at-state.data.now):'时间未知'),el('span','',`采样 ${formatTime(w.observed_at)}`));meta.append(reset,eta);card.append(meta);card.append(resetHistory(w));return card;
}
function resetHistory(w){
  const box=el('div','last-reset'),r=w.last_reset;
  box.append(el('span','reset-caption','上次重置 · '+(w.window_minutes===10080?'weekly':w.window_minutes===300?'5h':w.window_id)));
  if(!r){box.append(el('strong','','尚未检测到重置'));return box;}
  const when=v=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(v*1000));
  box.append(el('strong','',r.type_label),el('span','','检测于 '+when(r.detected_at)));
  if(r.kind==='scheduled_reset'||r.kind==='scheduled_rollover')box.append(el('small','','原计划 '+when(r.expected_reset)));
  else box.append(el('small','','发生区间 '+when(r.interval_start)+' → '+when(r.detected_at)));
  box.title=r.reason+' 检测时间不代表精确发生时间。';return box;
}
function emptyCard(minutes){const card=el('article','quota-card empty-card');card.append(el('h2','',minutes===300?'5 小时额度':'每周额度'));const r=el('div','reading');r.append(el('strong','','未上报'));card.append(r,el('p','muted','当前没有这个窗口的可用数据。收到采样后会自动显示。'));return card;}
function svgEl(tag,attrs){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));return e;}
function chart(w){
  const block=el('div','chart-block'),label=el('div','chart-label',w.label);label.append(el('span','',`${w.history.length} 条采样${w.history_limited?'（仅显示最近 1,500 条）':''} · ${w.window_id}`));block.append(label);
  if(w.history.length<2){block.append(el('div','empty','等待更多采样，至少两条记录后绘制走势。'));return block;}
  const points=w.history,first=points[0].observed_at,last=points.at(-1).observed_at;
  const width=1080,height=170,x=t=>40+(t-first)/(last-first)*1024,y=n=>145-n/100*125;
  const svg=svgEl('svg',{viewBox:`0 0 ${width} ${height}`,class:'chart',role:'img','aria-label':`${w.label}剩余额度走势`});
  for(const p of [0,50,100]){svg.append(svgEl('line',{x1:40,y1:y(p),x2:1064,y2:y(p),stroke:'#e5ebf3','stroke-dasharray':'3 5'}));const t=svgEl('text',{x:0,y:y(p)+4});t.textContent=p+'%';svg.append(t);}
  const color=w.window_minutes===10080?'#087f86':'#225ad5';let d='';
  points.forEach((p,i)=>{const px=x(p.observed_at),py=y(100-p.used_percent);const gap=i&&p.observed_at-points[i-1].observed_at>Math.min(w.window_minutes*60,7200);d+=`${i&&!gap?'L':'M'}${px.toFixed(2)},${py.toFixed(2)} `;});
  svg.append(svgEl('path',{d,fill:'none',stroke:color,'stroke-width':2.5,'stroke-linejoin':'round'}));
  const final=points.at(-1);svg.append(svgEl('circle',{cx:x(final.observed_at),cy:y(100-final.used_percent),r:4,fill:color}));
  block.append(svg);const axis=el('div','chart-axis');axis.append(el('span','',formatTime(first)),el('span','',formatTime(last)));block.append(axis);return block;
}
function renderEvents(){
  const data=state.data,filter=$('#event-filter').value,list=$('#event-list'),button=$('#more-events');list.replaceChildren();
  const context=`${data.account||'*'}|${data.days}|${filter}`;
  if(state.eventsContext!==context){state.eventsContext=context;state.showAllEvents=false;}
  const kinds=['scheduled_reset','scheduled_rollover','unexpected_reset','uncertain'];
  const events=data.events.filter(e=>filter==='all'||filter==='reset'&&kinds.includes(e.kind)||e.kind===filter);
  const scheduled=(data.counts.scheduled_reset||0)+(data.counts.scheduled_rollover||0);
  $('#event-summary').textContent=`当前范围：${scheduled} 次计划内变化 · ${data.counts.unexpected_reset||0} 次疑似突然重置 · ${data.counts.uncertain||0} 次待确认${data.events.length===200?' · 仅显示最近 200 条变化':''}`;
  if(!events.length){button.hidden=true;list.append(el('div','empty','这个范围内还没有符合条件的变化记录。'));return;}
  events.slice(0,state.showAllEvents?events.length:4).forEach(e=>{const row=el('article','event'),badge=el('div');badge.append(el('span',`event-badge ${e.kind}`,data.labels[e.kind]||e.kind));const content=el('div');content.append(el('h3','',data.accounts?e.account+' · '+e.label:e.label),el('p','',e.reason));if(e.interval_start)content.append(el('small','',`采样区间 ${formatTime(e.interval_start)} → ${formatTime(e.observed_at)} · 原计划 ${formatTime(e.expected_reset)}`));const meta=el('div');meta.append(el('time','',formatTime(e.observed_at)));if(e.before_used!=null)meta.append(el('span','delta',`剩余 ${pct(100-e.before_used)}% → ${pct(100-e.after_used)}%`));meta.append(el('small','',`证据强度：${{high:'较高',medium:'中等',low:'有限',info:'基线'}[e.confidence]||e.confidence}`));row.append(badge,content,meta);list.append(row);});
  button.hidden=events.length<=4;
  button.textContent=state.showAllEvents?'收起记录':`显示更多（另有 ${events.length-4} 条）`;
  button.setAttribute('aria-expanded',String(state.showAllEvents));
}
function fillCards(container,windows){
  container.replaceChildren();const ordered=[...windows];
  for(const minutes of [300,10080])if(!ordered.some(w=>w.window_minutes===minutes))ordered.push({placeholder:true,window_minutes:minutes});
  ordered.sort((a,b)=>a.window_minutes-b.window_minutes).forEach(w=>container.append(w.placeholder?emptyCard(w.window_minutes):renderCard(w)));
}
function ageText(seconds){
  if(seconds<60)return '不到 1 分钟';
  if(seconds<3600)return Math.floor(seconds/60)+' 分钟';
  if(seconds<86400)return Math.floor(seconds/3600)+' 小时';
  return Math.floor(seconds/86400)+' 天';
}
function renderCollectors(){
  const root=$('#collector-status-list');root.replaceChildren();
  const collectors=state.data.collectors||[];
  if(!collectors.length){root.append(el('p','empty','尚无账号上报。'));return;}
  const labels={recent:'最近有上传',delayed:'上传可能延迟',stale:'长时间未上报',never:'尚无采集器上传'};
  collectors.forEach(item=>{
    const card=el('article','collector-item'),top=el('div','collector-head');
    top.append(el('strong','',item.account),el('span','collector-badge '+item.status,labels[item.status]||'状态未知'));
    card.append(top);
    if(item.last_upload_at){
      card.append(el('p','',`服务器收到上传：${tiboTime(item.last_upload_at)}（${ageText(Math.max(0,state.data.now-item.last_upload_at))}前）`));
      card.append(el('p','muted',`最新采样时间：${tiboTime(item.last_sample_at)}`));
      if(state.data.now-item.last_sample_at>1800&&item.status==='recent')card.append(el('p','collector-warning','刚收到上传，但采样数据已超过 30 分钟，可能是积压队列。'));
    }else card.append(el('p','muted','只有手动上报或分析数据，暂时无法判断采集器上传状态。'));
    root.append(card);
  });
}
function renderTimeline(){
  if(state.view!=='overview'||!state.data)return;
  const root=$('#timeline-list');root.replaceChildren();
  const data=state.data,cutoff=data.now-data.days*86400;
  const events=(data.timeline_events||[]).map(e=>({at:e.observed_at,type:'quota',value:e}));
  const posts=(state.tiboData?.posts||[]).filter(p=>p.published_at>=cutoff&&
    /\b(reset|resets|usage|limits?|quota)\b|重置|额度/i.test(p.text)).map(p=>({at:p.published_at,type:'post',value:p}));
  const entries=[...events,...posts].sort((a,b)=>b.at-a.at).slice(0,30);
  if(!entries.length){root.append(el('p','empty','所选范围内没有重置事件或相关公开帖文。'));return;}
  entries.forEach(entry=>{
    const row=el('article','timeline-item '+entry.type),meta=el('div','timeline-meta');
    const stamp=el('time','',tiboTime(entry.at));stamp.dateTime=new Date(entry.at*1000).toISOString();meta.append(stamp);
    if(entry.type==='quota'){
      const e=entry.value;
      meta.append(el('span','timeline-type quota','额度采样事件'));
      row.append(meta,el('strong','',`${e.account||data.account} · ${e.label} · ${data.labels[e.kind]||e.kind}`),el('p','',e.reason));
      if(e.interval_start)row.append(el('small','',`可能发生区间：${tiboTime(e.interval_start)} → ${tiboTime(e.observed_at)}`));
    }else{
      const post=entry.value;meta.append(el('span','timeline-type post','Tibo 帖文'));
      const text=post.text.length>180?post.text.slice(0,180)+'…':post.text;
      row.append(meta,el('p','',text));
      const link=el('a','','查看原帖 ↗');link.href=post.url;link.target='_blank';link.rel='noopener noreferrer';row.append(link);
    }
    root.append(row);
  });
}
function renderForecast(data){
  $('#view-title').textContent='更多 · 额度天气预报';
  $('#freshness').textContent='根据最近连续采样估计 · 每 30 秒刷新 · 北京时间';
  $('#notice').hidden=true;
  const root=$('#forecast-list');root.replaceChildren();
  if(!data.forecasts?.length){root.append(el('p','empty','尚无账号数据。'));return;}
  const messages={exhausted:'最新采样显示额度已用尽。',stale:'最近采样超过 30 分钟，暂停预测。',
    reset_due:'预计重置时间已过，等待新采样。',insufficient:'连续采样不足，暂不预测。',
    flat:'近期额度消耗很少，无法估计耗尽时间。',awaiting_sample:'趋势估计已过时，等待新采样。',
    after_reset:'按当前趋势，预计在计划重置前不会用尽。'};
  data.forecasts.forEach(account=>{
    const group=el('article','forecast-account');group.append(el('h3','',account.account));
    if(!account.windows.length)group.append(el('p','muted','没有 5h 或 weekly 额度采样。'));
    account.windows.forEach(w=>{
      const card=el('div','forecast-card');
      card.append(el('strong','',`${w.label} · 剩余 ${pct(w.remaining_percent)}%`));
      card.append(el('p','',w.status==='before_reset'?`按近期趋势，可能在 ${tiboTime(w.estimated_at)} 左右用尽。`:messages[w.status]||'暂不预测。'));
      if(w.rate_pp_per_hour!=null)card.append(el('small','',`依据 ${w.points} 条连续采样、约 ${pct(w.span_hours)} 小时；已用额度平均每小时增加 ${pct(w.rate_pp_per_hour)} 个百分点。`));
      if(w.resets_at)card.append(el('small','',`当前报告的预计重置时间：${tiboTime(w.resets_at)}`));
      group.append(card);
    });
    root.append(group);
  });
}
function renderOverview(accounts){
  const root=$('#account-overview');root.replaceChildren();
  if(!accounts.length){root.append(el('div','surface empty','还没有账号数据。点击“接入 / 上传”，收到第一条上报后会自动显示。'));return;}
  for(const account of accounts){
    const group=el('article','surface account-group'),head=el('div','account-head'),identity=el('div');
    identity.append(el('h2','account-name',account.account));
    const r=account.last_report;
    identity.append(el('p','muted','最近采样 '+formatTime(r.observed_at)+(r.plan?' · '+r.plan.toUpperCase():'')));
    const detail=el('button','secondary','查看历史');detail.type='button';detail.setAttribute('aria-label','查看 '+account.account+' 的历史');
    detail.addEventListener('click',()=>{$('#account').value=account.account;refresh();window.scrollTo({top:0,behavior:'smooth'});});
    head.append(identity,detail);group.append(head);
    const cards=el('div','cards');fillCards(cards,account.latest);group.append(cards);
    const counts=account.counts,scheduled=(counts.scheduled_reset||0)+(counts.scheduled_rollover||0);
    group.append(el('p','account-changes','所选范围：'+scheduled+' 次计划内变化 · '+(counts.unexpected_reset||0)+' 次疑似突然重置 · '+(counts.uncertain||0)+' 次待确认'));
    root.append(group);
  }
}
function render(){
  if(state.view==='analysis'){renderAnalytics(state.data);return;}
  if(state.view==='more'){renderForecast(state.data);return;}
  const data=state.data,all=Array.isArray(data.accounts);
  $('#account-overview').hidden=!all;$('#quota-cards').hidden=all;$('#history-section').hidden=all;
  $('#view-title').textContent=all?'全部账号额度':data.account+' · 额度概览';
  const windows=all?data.accounts.flatMap(a=>a.latest):data.latest;
  if(all){
    $('#quota-cards').replaceChildren();$('#charts').replaceChildren();renderOverview(data.accounts);
    const stale=data.accounts.filter(a=>a.latest.some(w=>w.stale)).length;
    $('#freshness').textContent=data.accounts.length+' 个账号'+(stale?' · '+stale+' 个账号有过期采样':'')+' · 每 30 秒刷新，新账号自动出现';
  }else{
    $('#account-overview').replaceChildren();fillCards($('#quota-cards'),data.latest);
    const r=data.last_report;$('#freshness').textContent=r?'最近采样 '+formatTime(r.observed_at)+' · '+r.source+(r.plan?' · '+r.plan.toUpperCase():'')+' · 每 30 秒刷新':'等待 GPT 上传第一条额度记录';
    $('#charts').replaceChildren(...data.latest.map(chart));if(!data.latest.length)$('#charts').append(el('div','empty','上传第一条额度记录，即可开始积累历史。'));
  }
  $('#notice').hidden=true;
  if(windows.some(w=>w.stale)){$('#notice').textContent='部分额度已超过 30 分钟未上报。这里显示历史采样值，当前实际额度可能已变化。';$('#notice').hidden=false;}
  $('#rule-text').textContent='已用额度下降至少 '+data.rules.minimum_drop_pp+' 个百分点时，比较前后采样区间与原计划重置时间：跨越计划时间记为“计划内重置”；两次采样都早于原计划 '+data.rules.tolerance_seconds/60+' 分钟以上，记为“疑似突然重置”。每个账号独立判断。';
  renderEvents();renderCollectors();renderTimeline();
}
function clearLoginState(){
  state.authGeneration++;state.authenticated=false;state.csrf='';state.data=null;state.tiboData=null;state.pending=false;state.showAllEvents=false;state.eventsContext='';
  analysisState.data=null;$('#analysis-content').hidden=true;
  for(const id of ['account-overview','quota-cards','charts','event-list','collector-status-list','timeline-list','forecast-list'])$('#'+id).replaceChildren();
  if($('#integration-dialog').open)$('#integration-dialog').close();
  if($('#password-dialog').open)$('#password-dialog').close();
  $('#tibo-posts').replaceChildren();$('#tibo-status').textContent='正在读取文字缓存…';$('#password-form').reset();$('#upload-token').value='';$('#payload').value='';showLogin();
}
async function restoreSession(){
  const generation=state.authGeneration;
  try{
    const result=await api('api/session');
    if(state.authGeneration!==generation)return;
    if(result.authenticated){state.authenticated=true;state.csrf=result.csrf_token;await refresh(true);}
    else showLogin();
  }catch(err){$('#login-error').textContent='连接失败：'+err.message;}
}
$('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();const button=e.currentTarget.querySelector('button');button.disabled=true;
  state.authGeneration++;$('#login-error').textContent='';
  try{
    const result=await api('api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#login-password').value})});
    state.authenticated=true;state.csrf=result.csrf_token;await refresh(true);
  }catch(err){$('#login-error').textContent=err.message;}
  finally{$('#login-password').value='';button.disabled=false;}
});
$('#logout').addEventListener('click',async()=>{
  $('#logout').disabled=true;
  try{await api('api/logout',{method:'POST'});clearLoginState();}
  catch(err){$('#notice').textContent='退出未完成：'+err.message+'。请重试。';$('#notice').hidden=false;}
  finally{$('#logout').disabled=false;}
});
$('#change-password').addEventListener('click',()=>{$('#password-form').reset();$('#password-error').textContent='';$('#password-dialog').showModal();});
$('#close-password').addEventListener('click',()=>$('#password-dialog').close());
$('#password-dialog').addEventListener('close',()=>$('#password-form').reset());
$('#password-form').addEventListener('submit',async e=>{
  e.preventDefault();$('#password-error').textContent='';
  if($('#new-password').value!==$('#confirm-password').value){$('#password-error').textContent='两次新密码输入不一致';return;}
  $('#save-password').disabled=true;
  try{
    const result=await api('api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:$('#current-password').value,new_password:$('#new-password').value,confirm_password:$('#confirm-password').value})});
    clearLoginState();$('#login-error').textContent=result.message;$('#login-password').focus();
  }catch(err){if(err.status===401){clearLoginState();$('#login-error').textContent=err.message;}else $('#password-error').textContent=err.message;}
  finally{$('#save-password').disabled=false;}
});
function managementStatus(message,failed=false){const node=$('#account-management-status');node.textContent=message;node.classList.toggle('error',failed);}
function fillManagementSelect(id,accounts,preferred){
  const select=$(id),previous=select.value;select.replaceChildren();
  accounts.forEach(a=>select.append(new Option(a.account,a.account)));
  const names=accounts.map(a=>a.account);
  select.value=names.includes(previous)?previous:names.includes(preferred)?preferred:names[0]||'';
}
async function loadManagedAccounts(){
  const data=await api('api/accounts/manage'),accounts=data.accounts;
  const root=$('#managed-accounts');root.replaceChildren();
  if(!accounts.length)root.append(el('p','muted','当前没有账号。'));
  accounts.forEach(a=>{
    const row=el('div','managed-row');
    row.append(el('strong','',a.account),el('span','',a.samples+' 条额度采样 · '+a.events+' 条变化 · '+a.analytics_reports+' 份分析上报'));
    if(a.aliases.length)row.append(el('small','', '旧名称：'+a.aliases.join('、')));
    root.append(row);
  });
  for(const id of ['#rename-account','#merge-source','#merge-target','#delete-account'])
    fillManagementSelect(id,accounts,$('#account').value);
  if($('#merge-source').value===$('#merge-target').value&&accounts.length>1)
    $('#merge-target').value=accounts.find(a=>a.account!==$('#merge-source').value).account;
  $('#merge-submit').disabled=accounts.length<2;
  const blocked=$('#blocked-accounts');blocked.replaceChildren();
  if(!data.deleted_names.length)blocked.append(el('p','muted','没有已删除的名称。'));
  data.deleted_names.forEach(name=>{
    const row=el('div','blocked-row'),button=el('button','secondary','允许重新使用');
    button.type='button';
    button.addEventListener('click',()=>postAccountAction('api/accounts/unblock',{account:name},'',name+' 已允许重新使用；新数据上报后才会重新出现。',button));
    row.append(el('span','',name),button);blocked.append(row);
  });
}
async function postAccountAction(path,payload,selected,message,button){
  button.disabled=true;managementStatus('');let completed=false;
  try{
    await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    completed=true;
    await loadAccounts();
    if(selected&&Array.from($('#account').options).some(option=>option.value===selected))$('#account').value=selected;
    await refresh();
    await loadManagedAccounts();
    managementStatus(message);
  }catch(error){managementStatus(completed?'操作已完成，但列表刷新失败：'+error.message:error.message,true);}
  finally{button.disabled=false;}
}
$('#manage-accounts').addEventListener('click',async()=>{
  if($('#manage-accounts').tagName==='A')return;
  managementStatus('正在读取账号…');$('#accounts-dialog').showModal();
  try{await loadManagedAccounts();managementStatus('');}
  catch(error){managementStatus(error.message,true);}
});
$('#close-accounts').addEventListener('click',()=>$('#accounts-dialog').close());
$('#rename-form').addEventListener('submit',async event=>{
  event.preventDefault();const account=$('#rename-account').value,newName=$('#rename-new').value.trim();
  await postAccountAction('api/accounts/rename',{account,new_name:newName},newName,
                          account+' 已改名为 '+newName+'；旧名称的后续上传会自动归入新账号。',event.currentTarget.querySelector('button'));
  if(!$('#account-management-status').classList.contains('error'))$('#rename-new').value='';
});
$('#merge-form').addEventListener('submit',async event=>{
  event.preventDefault();const source=$('#merge-source').value,target=$('#merge-target').value;
  if(source===target){managementStatus('请选择两个不同账号。',true);return;}
  await postAccountAction('api/accounts/merge',{source,target},target,
                          source+' 的历史已并入 '+target+'；旧名称的后续上传也会归入保留账号。',event.currentTarget.querySelector('button'));
});
$('#delete-account').addEventListener('change',()=>$('#delete-confirm').value='');
$('#delete-form').addEventListener('submit',async event=>{
  event.preventDefault();const account=$('#delete-account').value;
  if($('#delete-confirm').value!==account){managementStatus('请完整输入账号名确认删除。',true);return;}
  await postAccountAction('api/accounts/delete',{account,confirm_name:$('#delete-confirm').value},'',
                          account+' 的记录已删除；同名自动上传已被拦截。',event.currentTarget.querySelector('button'));
  if(!$('#account-management-status').classList.contains('error'))$('#delete-confirm').value='';
});
$('#refresh').addEventListener('click',()=>refresh(true));$('#account').addEventListener('change',()=>refresh());$('#days').addEventListener('change',()=>refresh());$('#event-filter').addEventListener('change',renderEvents);$('#more-events').addEventListener('click',()=>{state.showAllEvents=!state.showAllEvents;renderEvents();});
$('#integration').addEventListener('click',()=>{const example={account:$('#account').value||'my-codex',observed_at:new Date().toISOString(),report_id:'为每次采样生成唯一 ID',usage:{rateLimits:{limitId:'codex',primary:{usedPercent:25,windowDurationMins:300,resetsAt:Math.floor(Date.now()/1000)+18000},secondary:{usedPercent:45,windowDurationMins:10080,resetsAt:Math.floor(Date.now()/1000)+604800}}}};$('#endpoint').textContent=apiURL('api/usage');$('#example').textContent=JSON.stringify(example,null,2);$('#payload').placeholder='粘贴真实额度 JSON；不要上传上方的示例数值';$('#integration-dialog').showModal();});
$('#close-dialog').addEventListener('click',()=>$('#integration-dialog').close());
$('#upload-form').addEventListener('submit',async e=>{e.preventDefault();$('#upload-button').disabled=true;try{const payload=JSON.parse($('#payload').value);const result=await api(payload.analytics?'api/analytics':'api/usage',{method:'POST',headers:{Authorization:`Bearer ${$('#upload-token').value.trim()}`,'Content-Type':'application/json'},body:JSON.stringify(payload)});$('#upload-result').textContent=`${result.status==='duplicate'?'重复记录已跳过':'上报成功'}：${JSON.stringify(result.results||result.sections||result.status)}`;$('#upload-token').value='';await refresh(true);}catch(err){$('#upload-result').textContent=err.message;}finally{$('#upload-button').disabled=false;}});
setInterval(()=>{if(state.authenticated&&!$('#dashboard').hidden&&!document.hidden)refresh();},30000);
restoreSession();
// Progressive enhancement; unsupported browsers keep the same working UI.
if(document.modelContext?.registerTool){const lifecycle=new AbortController();window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});Promise.resolve(document.modelContext.registerTool({name:'read_codex_quota_dashboard',title:'读取 Codex 额度看板',description:'读取当前已登录账号的额度与变化记录；不会上传数据。',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},async execute(input){if(!input||typeof input!=='object'||Object.keys(input).length)throw new Error('参数必须是空对象');if(!state.authenticated)throw new Error('请先使用密码登录');const generation=state.authGeneration;const result=await api(dataPath());if(generation!==state.authGeneration)throw new Error('登录状态已变化');state.data=result;render();if(state.view==='analysis')return result;if(state.view==='more')return {forecasts:result.forecasts,method:result.method};return result.accounts?{accounts:result.accounts,counts:result.counts}:{account:result.account,latest:result.latest.map(({history,...w})=>w),counts:result.counts};}},{signal:lifecycle.signal})).catch(()=>{});}
