'use strict';
const analysisState={showAllChats:false,showAllPeriods:false,data:null,overviewAccount:'',analysisAccount:'',moreAccount:null};
const featureColors={user:'#2967cd',guardian_review:'#ff8731',thread_title:'#30b8b1',guardian_classifier:'#9270e8',memory_consolidation:'#55bd79',thread_description:'#e86dae',subagent:'#cf9c2a',unknown:'#8795a9'};
const colors=['#2967cd','#ff8731','#30b8b1','#9270e8','#55bd79','#e86dae','#cf9c2a','#607c9a'];
function analyticsPath(){return 'api/analytics?account='+encodeURIComponent($('#account').value)+'&days='+$('#analysis-days').value+'&group_by='+$('#analysis-group').value;}
function beginAnalyticsLoad(){$('#analysis-pending').hidden=false;$('#analysis-content').hidden=true;}
function switchView(view){
  if(state.view===view)return;
  const current=$('#account').value;analysisState[state.view+'Account']=current;
  state.view=view;
  const select=$('#account'),saved=analysisState[view+'Account'],old=view==='more'&&saved===null?current:saved;
  select.value=[...select.options].some(o=>o.value===old)?old:'';
  if(view==='analysis'&&!select.value)select.value=select.options[1]?.value||'';
  if(select.options[0])select.options[0].disabled=view==='analysis';
  $('#overview-pane').hidden=view!=='overview';$('#analysis-pane').hidden=view!=='analysis';$('#more-pane').hidden=view!=='more';
  for(const name of ['overview','analysis','more'])$('#'+name+'-tab').setAttribute('aria-pressed',String(view===name));
  $('#notice').hidden=true;refresh(true);
}
function formatMetric(v,suffix=''){if(v==null)return '—';if(v>0&&v<0.01)return '<0.01'+suffix;return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(v)+suffix;}
function timeFull(t){return t==null?'源数据未提供更新时间':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(t*1000));}
function setLegend(day){
  const d=analysisState.data.daily,root=$('#usage-legend');root.replaceChildren();
  const total=day?Object.values(day.values||{}).reduce((a,b)=>a+b,0):d.total;
  $('#legend-caption').textContent=day?day.date+' 的用量构成':'所选 '+analysisState.data.days+' 天的用量构成';
  d.series.forEach((s,i)=>{const v=day?(day.values?.[s.key]??0):s.total;const item=el('div','legend-item');item.style.borderLeftColor=featureColors[s.key]||colors[i%colors.length];item.append(el('span','',s.label),el('strong','',total>0?formatMetric(v/total*100,'%'):'0%'));root.append(item);});
  if(!d.series.length)root.append(el('p','muted',d.missing_days===d.data.length?'此维度没有可用明细。':'已提供的日期没有记录到用量。'));
}
function drawUsageChart(){
  const data=analysisState.data,d=data?.daily,root=$('#usage-bars');if(!d||!root||state.view!=='analysis')return;
  root.replaceChildren();const width=Math.max(300,root.clientWidth),height=250,left=43,right=12,top=16,bottom=34;
  const sums=d.data.map(p=>p.known?Object.values(p.values).reduce((a,b)=>a+b,0):0),max=Math.max(...sums,1),plotH=height-top-bottom,step=(width-left-right)/d.data.length,bar=Math.min(52,step*.60);
  const svg=svgEl('svg',{viewBox:'0 0 '+width+' '+height,role:'img','aria-label':'按 UTC 日期统计的用量堆叠柱状图'});
  for(let i=0;i<=3;i++){const y=height-bottom-plotH*i/3;svg.append(svgEl('line',{x1:left,y1:y,x2:width-right,y2:y,stroke:'#e6ebf2'}));const t=svgEl('text',{x:left-8,y:y+4,'text-anchor':'end'});t.textContent=new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(max*i/3);svg.append(t);}
  const labelEvery=Math.ceil(d.data.length/(width<600?4:7));
  d.data.forEach((day,i)=>{const x=left+step*(i+.5);let y=height-bottom;const group=svgEl('g',{tabindex:'0',role:'button','aria-label':day.date+(day.known?'，用量 '+formatMetric(sums[i]):'，数据未提供')});
    group.append(svgEl('rect',{x:x-step/2,y:top,width:step,height:plotH,fill:'transparent'}));
    if(!day.known){group.append(svgEl('line',{x1:x-bar/2,y1:y-3,x2:x+bar/2,y2:y-3,stroke:'#a6b1c1','stroke-width':3,'stroke-dasharray':'3 3'}));}
    else d.series.forEach((s,k)=>{const v=day.values[s.key]||0;if(v<=0)return;const h=v/max*plotH;y-=h;group.append(svgEl('rect',{x:x-bar/2,y,width:bar,height:h,fill:featureColors[s.key]||colors[k%colors.length]}));});
    const title=svgEl('title',{});title.textContent=day.date+'：'+(day.known?formatMetric(sums[i])+' '+(d.units==='percent'?'相对用量':d.units):'未提供');group.append(title);
    const show=()=>{if(day.known)setLegend(day);else{$('#legend-caption').textContent=day.date+'：数据未提供';$('#usage-legend').replaceChildren();}};
    group.addEventListener('pointerenter',show);group.addEventListener('focus',show);group.addEventListener('click',show);group.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show();}});
    svg.append(group);
    if(i%labelEvery===0||i===d.data.length-1){const t=svgEl('text',{x,y:height-10,'text-anchor':'middle'});const parts=day.date.split('-');t.textContent=Number(parts[1])+'/'+Number(parts[2]);svg.append(t);}
  });
  svg.addEventListener('pointerleave',()=>setLegend(null));root.append(svg);setLegend(null);
}
function renderChats(){
  const raw=analysisState.data.chats,root=$('#chat-ranking');root.replaceChildren();$('#more-chats').hidden=true;
  if(!raw){root.append(el('p','empty','尚未上传聊天用量明细。'));$('#chat-freshness').textContent='';return;}
  $('#chat-freshness').textContent=raw.scope+'。统计截至 '+timeFull(raw.data_as_of)+(raw.is_truncated?'；列表存在截断。':'。');
  const metric=$('#chat-metric').value,all=[...raw.rows].sort((a,b)=>a[metric]==null?(b[metric]==null?0:1):b[metric]==null?-1:b[metric]-a[metric]);
  if(!all.length){root.append(el('p','empty','本次采集没有符合范围的聊天。'));return;}
  const header=el('div','rank-grid rank-header');header.append(el('span','','聊天'),el('span','',metric==='weekly_limit_percent'?'占当前每周限额':'占当前 5 小时限额'),el('span','','已用 Credits'));root.append(header);
  const shown=analysisState.showAllChats?all:all.slice(0,5);
  shown.forEach(row=>{const details=el('details','rank-row'),summary=el('summary','rank-grid');summary.append(el('span','chat-title',row.title),el('strong','',formatMetric(row[metric],'%')),el('span','',formatMetric(row.balance_usage_credits)));details.append(summary);
    const body=el('div','rank-details');body.append(el('p','muted','已知子任务：'+formatMetric(row.subagent_count)+(row.data_status==='unavailable'?' · 源未提供此聊天的用量':'')));
    if(row.groups.length){const table=el('div','group-table');row.groups.forEach(g=>{const line=el('div','group-line');line.append(el('span','',[g.model,g.reasoning_effort,g.speed].join(' · ')),el('strong','',formatMetric(g[metric],'%')));table.append(line);});body.append(table);}else body.append(el('p','muted','没有可用的模型明细。'));
    details.append(body);root.append(details);
  });
  $('#more-chats').hidden=all.length<=5;$('#more-chats').textContent=analysisState.showAllChats?'收起':'显示更多（'+all.length+' 个聊天）';
}
function renderPeriods(){
  const data=analysisState.data.periods,root=$('#period-history');root.replaceChildren();
  if(!data){root.append(el('p','empty','尚未上传套餐周期历史。'));return;}
  if(!data.periods.length){root.append(el('p','empty','源数据未返回套餐周期。'));return;}
  for(const minutes of [10080,300]){
    const periods=data.periods.filter(p=>p.window_minutes===minutes);if(!periods.length)continue;
    root.append(el('h3','period-type',minutes===10080?'每周限额':'5 小时限额'));
    periods.slice(0,analysisState.showAllPeriods?500:10).forEach((p,i)=>{const card=el('details','period-card');card.open=i===0;
      const summary=el('summary','period-summary');summary.append(el('span','',timeFull(p.starts_at)+' — '+timeFull(p.ends_at)),el('strong','',formatMetric(p.used_percent,'%')));card.append(summary);
      const body=el('div','period-body');
      if(p.reset_at_start){
        body.append(el('p','period-reset','从 '+timeFull(p.reset_at_start.boundary)+' 开始 · '+p.reset_at_start.label));
        if(p.reset_at_start.estimated&&p.reset_at_start.interval_start!=null)
          body.append(el('p','muted','实际重置发生在 '+timeFull(p.reset_at_start.interval_start)+' 至 '+timeFull(p.reset_at_start.detected_at)+' 之间；这里以检测时间分段。'));
      }
      if(p.reset_at_end){
        body.append(el('p','period-reset','此记录于 '+timeFull(p.reset_at_end.boundary)+' 截止 · '+p.reset_at_end.label));
        if(p.reset_at_end.estimated&&p.reset_at_end.interval_start!=null)
          body.append(el('p','muted','实际重置发生在 '+timeFull(p.reset_at_end.interval_start)+' 至 '+timeFull(p.reset_at_end.detected_at)+' 之间；这里以检测时间分段。'));
      }
      if(!p.segment_data_available){
        body.append(el('p','period-warning','这段周期尚无可归属的源统计；不会把重置前用量计入新周期。'));
      }else{
        body.append(el('p','muted',(data.approximate?'约计 · ':'')+p.plan_type.toUpperCase()+' · 统计截至 '+timeFull(p.data_as_of)));
        if(!p.accounting_complete||!data.coverage_complete)body.append(el('p','period-warning','部分用量不可用或尚未结算，此周期的数据可能不完整。'));
        if(p.rows.length)p.rows.forEach(r=>{const line=el('div','group-line');line.append(el('span','',r.label),el('span','',formatMetric(r.percent,'%')));body.append(line);});
        else body.append(el('p','muted','这个周期没有所选维度的分组数据。'));
      }
      card.append(body);root.append(card);
    });
    if(periods.length>10&&!analysisState.showAllPeriods){const more=el('button','secondary','显示更早周期');more.addEventListener('click',()=>{analysisState.showAllPeriods=true;renderPeriods();});root.append(more);}
  }
}
function renderAnalytics(data){
  analysisState.data=data;$('#analysis-pending').hidden=true;$('#analysis-content').hidden=false;
  $('#view-title').textContent=(data.account||'账号')+' · 用量分析';$('#freshness').textContent=data.available?'最近采集 '+timeFull(data.observed_at)+' · '+data.source:'等待上传分析明细';
  $('#analysis-empty').hidden=data.available;$('#analysis-sections').hidden=!data.available;
  if(!data.available)return;
  const daily=data.daily;
  if(daily){drawUsageChart();$('#daily-freshness').textContent=(daily.units==='percent'?'相对用量，用于比较各日期和功能的消耗分布。':'单位：'+daily.units+'。')+' 日期按 UTC 统计。'+(daily.data_as_of?'统计截至 '+timeFull(daily.data_as_of)+'。':'源未提供统计更新时间；采集于 '+timeFull(daily.collected_at)+'。')+(daily.missing_days?' 有 '+daily.missing_days+' 天未提供数据，以虚线标记。':'');}
  else{$('#usage-bars').replaceChildren(el('p','empty','尚未上传每日用量明细。'));$('#usage-legend').replaceChildren();$('#legend-caption').textContent='';$('#daily-freshness').textContent='';}
  renderChats();renderPeriods();
}
$('#overview-tab').addEventListener('click',()=>switchView('overview'));$('#analysis-tab').addEventListener('click',()=>switchView('analysis'));$('#more-tab').addEventListener('click',()=>switchView('more'));
document.querySelectorAll('[data-analysis-days]').forEach(button=>button.addEventListener('click',()=>{const days=button.dataset.analysisDays;$('#analysis-days').value=days;document.querySelectorAll('[data-analysis-days]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));refresh();}));
$('#analysis-group').addEventListener('change',()=>refresh());$('#chat-metric').addEventListener('change',renderChats);$('#more-chats').addEventListener('click',()=>{analysisState.showAllChats=!analysisState.showAllChats;renderChats();});
new ResizeObserver(()=>{if(state.view==='analysis'&&analysisState.data?.daily&&!$('#analysis-content').hidden)drawUsageChart();}).observe($('#usage-bars'));
