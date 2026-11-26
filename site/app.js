/* Browser application and pure display helpers; no runtime dependencies. */
'use strict';
const HTML_ENTITIES={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>HTML_ENTITIES[c]);
const isFiniteNumber=n=>typeof n==='number'&&Number.isFinite(n);
const seriesColor=color=>/^#[0-9a-f]{6}$/i.test(color)?color:'#8c9897';
const referenceDocument=value=>/^(?:reference\/)?[a-z0-9][a-z0-9._-]*\.json$/i.test(value)?value:'data.json';
const wholeFormat=new Intl.NumberFormat('en',{maximumFractionDigits:0});
const decimalFormat=new Intl.NumberFormat('en',{maximumFractionDigits:2});
const millionFormat=new Intl.NumberFormat('en',{maximumFractionDigits:1});
const fractionFormat=new Intl.NumberFormat('en',{maximumSignificantDigits:2});
const coverageFormat=new Intl.NumberFormat('en',{style:'percent',maximumFractionDigits:1});
const dayFormat=new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});
const monthFormat=new Intl.DateTimeFormat('en-GB',{month:'short',year:'numeric',timeZone:'UTC'});
const SOURCE_CREDIT='Source: 1aeo.com - OnionSurge';
const DOS_METRIC='tor_relay_dos_total';
const SOURCE_SERIES_ALIASES=new Map([
 ['circuit_rejections','circuit_rejected'],['introduction_rejections','introduce2_rejected'],
 ['exit_stream_rejections','stream_rejected'],
]);
// Reviewed Tor labels, not a parser for the broader collection/coverage queries.
// Chart ID, expected metric, series label, accepted UI keys, fixed matchers.
const SELECTOR_SOURCES=new Map([
 ['transport_rejections',DOS_METRIC,'type','concurrent_conn_rejected conn_rejected'],
 ['incoming_connections','tor_relay_connections_total','state','total accepted rejected',[['type','OR'],['direction','received']]],
 ['open_connections','tor_relay_connections',null,'open_connections',[['type','OR'],['direction','received'],['state','opened']]],
 ['circuit_rejections',DOS_METRIC,'type','circuit_rejections'],
 ['directory_requests','tor_relay_begindir_request_total','type','circuit_dead consensus descriptor hs_get hs_post microdesc no_request other_get other_post unparsed'],
 ['introduction_rejections',DOS_METRIC,'type','introduction_rejections'],
 ['queue_limit_circuits',DOS_METRIC,'type','circuit_killed_max_cell circuit_killed_max_cell_outq'],
 ['protocol_closes','tor_relay_circ_proto_violation_total',null,'protocol_closes'],
 ['onionskin_drops','tor_relay_load_onionskins_total','type','fast ntor ntor_v3 tap',[['action','dropped']]],
 ['directory_rejections',DOS_METRIC,'type','begindir_hsdir_rejected begindir_rate_rejected begindir_rejected'],
 ['hsdir_fetches','tor_relay_hsdir_fetch_total','action','not_found refused single_hop success'],
 ['hsdir_uploads','tor_relay_hsdir_upload_total','action','bad_url invalid not_newer refused single_hop success'],
 ['directory_waiting','tor_relay_begindir_connections',null,'directory_waiting',[['state','command_wait']]],
 ['open_circuits','tor_relay_circuits_total',null,'open_circuits',[['state','opened']]],
 ['exit_stream_rejections',DOS_METRIC,'type','exit_stream_rejections'],
].map(([id,metric,label,keys,fixed=[]])=>[id,{metric,label,keys:new Set(keys.split(' ')),fixed}]));

function expandSelectorValues(chartId,series){
 const out=[],seen=new Set();
 for(const s of series){
  const mapped=chartId==='incoming_connections'
   ?(s.key==='total'?['created','rejected']:s.key==='accepted'?['created']:[s.key])
   :[SOURCE_SERIES_ALIASES.get(s.key)||s.key];
  for(const v of mapped)if(!seen.has(v)){seen.add(v);out.push(v);}
 }
 return out;
}

function metricSelectorTokens(c,series){
 const source=SELECTOR_SOURCES.get(c.id);
 const known=source&&c.metrics.length===1&&c.metrics[0]===source.metric
  &&Array.isArray(series)&&series.length>0&&series.every(s=>source.keys.has(s?.key));
 const matchers=known?source.fixed.map(([label,value])=>[label,[value]]):[];
 if(known&&source.label)matchers.push([source.label,expandSelectorValues(c.id,series)]);
 return c.metrics.map(metric=>{
  const tokens=[{text:metric,breakAfter:matchers.length>0}];
  if(!matchers.length)return tokens;
  tokens.push({text:'{',breakAfter:true});
  for(const [index,[label,values]] of matchers.entries()){
   if(index)tokens.push({text:',',breakAfter:true});
   tokens.push({text:label+(values.length===1?'=':'=~')+'"'});
   for(const [position,value] of values.entries()){
    if(position)tokens.push({text:'|',breakAfter:true});
    tokens.push({text:value,highlight:true});
   }
   tokens.push({text:'"'});
  }
  tokens.push({text:'}'});
  return tokens;
 });
}
const cleanZero=n=>n===0?0:n;
const number=n=>!isFiniteNumber(n)?'Unavailable':n===0?'0':Math.abs(n)<0.001?(n>0?'<0.001':'>−0.001'):Math.abs(n)<1?fractionFormat.format(cleanZero(n)):wholeFormat.format(n);
const count=n=>Number.isInteger(n)&&n>=0?n===0?'0':wholeFormat.format(n):'Unavailable';
const validTime=t=>isFiniteNumber(t)&&Math.abs(t)<=8.64e12;
const date=t=>validTime(t)?dayFormat.format(new Date(t*1000)):'Unavailable';
const time=t=>validTime(t)?new Date(t*1000).toISOString().slice(-13,-8):'Unavailable';
const displayScale=c=>isFiniteNumber(c.display_scale)&&c.display_scale>0?c.display_scale:NaN;
function scaledProduct(perRelay,population,scale){
 // All three factors are finite and nonnegative. Pair opposite magnitudes
 // first so a finite final bound is not lost to intermediate overflow.
 const [small,middle,large]=[perRelay,population,scale].sort((a,b)=>a-b);
 return (small*large)*middle;
}
// Bound the synthesized gap grid if a damaged dataset supplies an extreme range.
const MAX_PLOT_INTERVALS=100000;
const HOUR=3600;
const DAY=86400;
const RESERVED_JSON_KEYS=new Set(['__proto__','constructor','prototype']);
const FORBIDDEN_HOURLY_KEYS=new Set(['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24']);
const SUB_24H_CORE='(?:0*1d|0+[0-9]*h|(?:[0-9]|1[0-9]|2[0-3])h|[0-9]+[sm])';
const TIME_RANGE_ID=new RegExp(`^(?!.*(?:^|[-_])${SUB_24H_CORE}$)(?!.+[-_]24h$)[a-z0-9][a-z0-9_-]{0,31}$`);
const SUMMARY_KEY=/^[a-z][a-z0-9_]{0,63}$/;
const EXPORT_SUFFIX=new RegExp(`^(?!.*(?:^|-)${SUB_24H_CORE}$)(?!.+-24h$)[a-z0-9]+(?:-[a-z0-9]+){0,7}$`);
const NON_BLANK_TEXT=/[^\s\x1c-\x1f\x85\ufeff]/;
const TIME_RANGE_TOP_KEYS=['version','default_range','legacy_aliases','card_slots','periods'];
const TIME_RANGE_PERIOD_KEYS=['id','summary_key','export_suffix','label','average_label','plot_label','window','minimum_coverage','minimum_comparison_windows'];
const TIME_RANGE_WINDOW_KEYS=['unit','count','comparison_lag_days'];
const mkPeriod=([id,summary_key,export_suffix,label,average_label,plot_label,unit,count,comparison_lag_days,minimum_coverage,minimum_comparison_windows])=>
 Object.freeze({id,summary_key,export_suffix,label,average_label,plot_label,window:Object.freeze({unit,count,comparison_lag_days}),minimum_coverage,minimum_comparison_windows});
const V5_PERIODS=Object.freeze([
 ['30d','thirty_days','30d','30 days','Last 30 days average','Last 30 days','days',30,1,0.9,26],
 ['6mo','six_months','6mo','6 months','Last 6 months average','Last 6 months','months',6,0,0.9,30],
 ['all','all_time','all-time','All time','All-time average','All time','all_time',null,0,0,30],
].map(mkPeriod));
const LEGACY_V5_TIME_RANGES=Object.freeze({
 version:1,default_range:'30d',legacy_aliases:Object.freeze({'24h':'30d'}),
 card_slots:Object.freeze({fallback_dynamic:'30d',fixed:Object.freeze(['6mo','all'])}),periods:V5_PERIODS,
});
const STANDALONE_PERIOD_FALLBACKS=Object.freeze([
 mkPeriod(['24h','twenty_four_hours','24h','24 hours','Last 24 hours','24-hour snapshot','hours',24,0,0,30]),
 ...V5_PERIODS,
 ...[
  ['7d','seven_days','7d','7 days','Last 7 days average','Last 7 days','days',7,1,0,6],
  ['1mo','one_month','1mo','1 month','Last 1 month average','Last 1 month','months',1,0,0,25],
  ['3mo','three_months','3mo','3 months','Last 3 months average','Last 3 months','months',3,0,0,30],
  ['12mo','twelve_months','12mo','12 months','Last 12 months average','Last 12 months','months',12,0,0,30],
 ].map(mkPeriod),
]);
const UNIT_COUNT_BOUNDS={days:[2,36500],months:[1,1200]};
const okText=(s,limit)=>typeof s==='string'&&NON_BLANK_TEXT.test(s)&&(s.length<=limit||[...s].length<=limit);
const isPlainRecord=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
function req(ok,msg){if(!ok)throw new Error(msg);}
function hasExactKeys(value,keys,message){
 req(isPlainRecord(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key)),message);
}
 function validateTimeRanges(config){
 hasExactKeys(config,TIME_RANGE_TOP_KEYS,'Invalid time_ranges structure');
 req(config.version===1,'Unsupported time_ranges version');
 req(Array.isArray(config.periods)&&config.periods.length>=1&&config.periods.length<=16,'Invalid time_ranges periods');
 const seenIds=new Set(),seenSummaryKeys=new Set(),seenSuffixes=new Set();
 let allTimeCount=0,hoursCount=0;
 for(const period of config.periods){
  hasExactKeys(period,TIME_RANGE_PERIOD_KEYS,'Invalid time_ranges period');
  const {id,summary_key:skey,export_suffix:sfx,window:w,minimum_coverage:cov,minimum_comparison_windows:mcw}=period;
  req(typeof id==='string'&&TIME_RANGE_ID.test(id)&&!RESERVED_JSON_KEYS.has(id)&&!seenIds.has(id),'Invalid time_ranges period id');
  seenIds.add(id);
  req(typeof skey==='string'&&SUMMARY_KEY.test(skey)&&skey!=='day'&&skey!=='hour'&&!RESERVED_JSON_KEYS.has(skey)&&!FORBIDDEN_HOURLY_KEYS.has(skey)&&!seenSummaryKeys.has(skey),'Invalid time_ranges summary_key');
  seenSummaryKeys.add(skey);
  req(typeof sfx==='string'&&sfx.length<=32&&EXPORT_SUFFIX.test(sfx)&&!RESERVED_JSON_KEYS.has(sfx)&&!seenSuffixes.has(sfx),'Invalid time_ranges export_suffix');
  seenSuffixes.add(sfx);
  for(const field of ['label','average_label','plot_label'])req(okText(period[field],80),'Invalid time_ranges label');
  hasExactKeys(w,TIME_RANGE_WINDOW_KEYS,'Invalid time_ranges window');
  const bounds=typeof w.unit==='string'&&Object.hasOwn(UNIT_COUNT_BOUNDS,w.unit)?UNIT_COUNT_BOUNDS[w.unit]:null;
  if(bounds){
   req(id!=='24h','Invalid time_ranges period id');
   req(sfx!=='24h','Invalid time_ranges export_suffix');
   req(Number.isInteger(w.count)&&w.count>=bounds[0]&&w.count<=bounds[1]&&(Object.is(w.comparison_lag_days,0)||w.comparison_lag_days===1),`Invalid time_ranges ${w.unit} window`);
   req(isFiniteNumber(cov)&&!Object.is(cov,-0)&&cov>=0&&cov<=1,'Invalid time_ranges minimum_coverage');
  }else if(w.unit==='hours'){
   hoursCount++;
   req(hoursCount===1&&id==='24h'&&sfx==='24h','Invalid time_ranges hours period');
   req(w.count===24&&Object.is(w.comparison_lag_days,0),'Invalid time_ranges hours window');
   req(Object.is(cov,0),'Invalid time_ranges hours minimum_coverage');
  }else{
   req(w.unit==='all_time','Unsupported time_ranges window unit');
   req(id!=='24h','Invalid time_ranges period id');
   req(sfx!=='24h','Invalid time_ranges export_suffix');
   allTimeCount++;
   req(w.count===null&&Object.is(w.comparison_lag_days,0),'Invalid time_ranges all_time window');
   req(Object.is(cov,0),'Invalid all_time minimum_coverage');
  }
  req(Number.isInteger(mcw)&&mcw>=1&&mcw<=10000,'Invalid time_ranges minimum_comparison_windows');
 }
 req(allTimeCount===1,'Expected exactly one all_time period');
 req(typeof config.default_range==='string'&&seenIds.has(config.default_range),'Invalid time_ranges default_range');
 req(isPlainRecord(config.legacy_aliases),'Invalid time_ranges legacy_aliases');
 for(const [alias,target] of Object.entries(config.legacy_aliases)){
  req(okText(alias,32)&&!RESERVED_JSON_KEYS.has(alias)&&!FORBIDDEN_HOURLY_KEYS.has(alias)&&!seenIds.has(alias)&&typeof target==='string'&&seenIds.has(target),'Invalid time_ranges legacy_alias');
 }
 hasExactKeys(config.card_slots,['fallback_dynamic','fixed'],'Invalid time_ranges card_slots');
 const {fixed,fallback_dynamic:fallbackDynamic}=config.card_slots;
 req(Array.isArray(fixed)&&fixed.length===2&&typeof fixed[0]==='string'&&typeof fixed[1]==='string'&&seenIds.has(fixed[0])&&seenIds.has(fixed[1])&&fixed[0]!==fixed[1],'Invalid time_ranges card_slots.fixed');
 if(hoursCount>0)req(fixed.includes('24h'),'Invalid time_ranges card_slots.fixed');
 req(typeof fallbackDynamic==='string'&&seenIds.has(fallbackDynamic)&&!fixed.includes(fallbackDynamic),'Invalid time_ranges card_slots.fallback_dynamic');
 return config;
}
function resolveTimeRanges(D){
 return D?.schema_version>=6||D?.time_ranges!==undefined?validateTimeRanges(D.time_ranges):LEGACY_V5_TIME_RANGES;
}

function calendarWindowStart(dayEnd,months,lagDays=0){
 const endDate=new Date((dayEnd-lagDays*DAY)*1000);
 const day=endDate.getUTCDate();
 const totalMonths=endDate.getUTCFullYear()*12+endDate.getUTCMonth()-months;
 const year=Math.floor(totalMonths/12);
 const month=((totalMonths%12)+12)%12;
 const probe=new Date(0);
 probe.setUTCFullYear(year,month+1,0);
 const lastDay=probe.getUTCDate();
 probe.setUTCFullYear(year,month,Math.min(day,lastDay));
 return probe.getTime()/1000;
}

function periodWindowStart(dayEnd,periodDef,lag=0){
 const {unit,count}=periodDef.window;
 if(unit==='days')return dayEnd-(count+lag)*DAY;
 if(unit==='months')return calendarWindowStart(dayEnd,count,lag);
 return null;
}
const isGaugeKind=c=>c?.kind==='gauge_hour_mean'||c?.kind==='gauge_daily_mean';
const qualifiedDailyRow=(p,scale=1)=>Array.isArray(p)&&validTime(p[0])&&p[0]%DAY===0&&isFiniteNumber(p[1])&&p[1]>=0&&isFiniteNumber(p[1]*scale)&&p[4]===true&&Number.isInteger(p[2])&&p[2]>0&&isFiniteNumber(p[3])&&p[3]>=0.9&&p[3]<=1;
const qualifiedSampledHourRow=(p,scale=1)=>Array.isArray(p)&&validTime(p[0])&&p[0]%21600===0&&isFiniteNumber(p[1])&&p[1]>=0&&isFiniteNumber(p[1]*scale)&&p[4]===true&&Number.isInteger(p[2])&&p[2]>=8&&isFiniteNumber(p[3])&&p[3]>=0.9&&p[3]<=1;
const hasHourPayload=D=>!!(D&&(D.window_seconds===HOUR||(D.window_seconds===undefined&&D.charts?.some(c=>c?.series?.some(s=>Array.isArray(s?.values)&&s.values.length>0)))));
function hourlyObservations(s,scale,start,end){
 const valuesByTime=new Map(),populations=new Map();
 const vals=s?.values,rels=s?.hourly_relays,exps=s?.hourly_expected_relays;
 if(!Array.isArray(vals)||!Array.isArray(rels)||!Array.isArray(exps))return {valuesByTime,populations};
 const relByTime=new Map(),expByTime=new Map();
 for(const r of rels)if(Array.isArray(r)&&validTime(r[0])&&r[0]%HOUR===0&&Number.isInteger(r[1])&&r[1]>=8)relByTime.set(r[0],r[1]);
 for(const e of exps)if(Array.isArray(e)&&validTime(e[0])&&e[0]%HOUR===0&&Number.isInteger(e[1])&&e[1]>0)expByTime.set(e[0],e[1]);
 for(const p of vals){
  if(!Array.isArray(p))continue;
  const t=p[0],v=p[1];
  if(!validTime(t)||t%HOUR!==0||t<=start||t>end)continue;
  const n=relByTime.get(t),exp=expByTime.get(t);
  const ok=isFiniteNumber(v)&&v>=0&&isFiniteNumber(v*scale)&&n!==undefined&&exp!==undefined&&n<=exp&&n/exp>=0.9;
  valuesByTime.set(t,ok?v:null);
  if(ok)populations.set(t,n);else populations.delete(t);
 }
 return {valuesByTime,populations};
}
const peakShare=(val,peak)=>{
 if(val==null||!(peak>0))return null;
 const pct=val/peak*100;
 if(pct<0.1&&val>0)return '<0.1%';
 const formatted=decimalFormat.format(cleanZero(pct));
 return (formatted==='-0'?'0':formatted)+'%';
};
function validateDataGraph(D){
 req(D&&Array.isArray(D.families)&&D.families.length&&Array.isArray(D.charts)&&D.charts.length,'Missing chart data');
 if(D.schema_version>=6||D.time_ranges!==undefined)validateTimeRanges(D.time_ranges);
 const seenFamilies=new Set(),chartsById=new Map(),assigned=new Set();
 for(const c of D.charts){
  req(c&&typeof c.id==='string'&&c.id&&!chartsById.has(c.id),'Invalid chart id');
  req(typeof c.family_id==='string'&&c.family_id,'Invalid chart family');
  req(Array.isArray(c.metrics)&&c.metrics.length&&Array.isArray(c.series)&&c.series.length&&Array.isArray(c.display_keys)&&c.display_keys.length,'Invalid chart series structure');
  const seriesKeys=new Set();
  for(const s of c.series){req(s&&typeof s.key==='string'&&s.key&&!seriesKeys.has(s.key),'Invalid series key');seriesKeys.add(s.key);}
  req(c.display_keys.every(k=>typeof k==='string'&&seriesKeys.has(k)),'Invalid chart display keys');
  chartsById.set(c.id,c);
 }
 for(const f of D.families){
  req(f&&typeof f.id==='string'&&f.id&&!seenFamilies.has(f.id),'Invalid family id');
  req(Array.isArray(f.children)&&f.children.length&&typeof f.primary==='string'&&f.children.includes(f.primary),'Invalid family children');
  const primaryChart=chartsById.get(f.primary);
  req(primaryChart&&primaryChart.family_id===f.id,'Invalid family primary chart');
  for(const childId of f.children){
   req(typeof childId==='string'&&!assigned.has(childId),'Duplicate family child');
   const child=chartsById.get(childId);
   req(child&&child.family_id===f.id,'Invalid family child chart');
   assigned.add(childId);
  }
  seenFamilies.add(f.id);
 }
 req(assigned.size===chartsById.size,'Families must partition charts');
}
const HERO_RANGE_META=Object.freeze({
 '24h':Object.freeze({label:'24 hours',title:'Last 24 hours',months:null}),
 '7d':Object.freeze({label:'7 days',title:'Last 7 days',months:null}),
 '1mo':Object.freeze({label:'1 month',title:'Last 1 month',months:1}),
 '6mo':Object.freeze({label:'6 months',title:'Last 6 months',months:6}),
 '12mo':Object.freeze({label:'12 months',title:'Last 12 months',months:12}),
 all:Object.freeze({label:'All time',title:'All time',months:null}),
});
const HERO_HOURLY_LEFT_RANGES=Object.freeze(['24h','7d','1mo','6mo','12mo']);
const HERO_DAILY_LEFT_RANGES=Object.freeze(['7d','1mo','6mo','12mo']);
const isHeroRange=r=>typeof r==='string'&&Object.hasOwn(HERO_RANGE_META,r);
const normalizeHeroRange=(r,hasHours)=>isHeroRange(r)?(r==='24h'&&!hasHours?'7d':r):(hasHours?'24h':'7d');
function parseLocation(data,search='',hash='',defaultDesign='1'){
 const timeRanges=resolveTimeRanges(data),periodIds=new Set(timeRanges.periods.map(p=>p.id));
 const allTimeId=timeRanges.periods.find(p=>p.window.unit==='all_time').id,params=new URLSearchParams(search);
 const hasHours=hasHourPayload(data),reqHero=params.get('hero_range');
 const fragment=hash.startsWith('#')?hash.slice(1):hash;
 const chartFragment=!params.has('view')&&fragment&&!fragment.startsWith('family-')&&fragment!=='reference'&&data.charts.some(c=>c.id===fragment);
 const requested=params.get('view')||(defaultDesign==='3'||chartFragment?'explorer':'overview');
 const allParam=params.get('all');
 const legacyAll=!!allParam&&allParam.split(',').some(id=>data.families.some(f=>f.id===id));
 const defaultAll=!params.has('all')&&!params.has('view')&&!chartFragment&&defaultDesign==='2';
 let selectedFamily=data.families.find(f=>f.id===params.get('family'))||data.families.find(f=>f.id==='directory')||data.families[0];
 const requestedChart=params.get('chart')||fragment||(params.has('family')?selectedFamily.primary:'directory_requests');
 const selectedChart=data.charts.find(c=>c.id===requestedChart)||data.charts.find(c=>c.id===selectedFamily.primary);
 selectedFamily=data.families.find(f=>f.id===selectedChart.family_id);
 const rawRange=params.get('range');
 const aliased=rawRange!=null&&Object.hasOwn(timeRanges.legacy_aliases,rawRange)?timeRanges.legacy_aliases[rawRange]:rawRange;
 const wantedSeries=params.get('series');
 const selectedSeries=wantedSeries?selectedChart.series.find(s=>s.key===wantedSeries):null;
 return {
  view:['overview','explorer','fixes','methods'].includes(requested)?requested:'overview',
  range:periodIds.has(aliased)?aliased:legacyAll||defaultAll?allTimeId:timeRanges.default_range,
  layout:params.get('layout')==='grid'?'grid':'full',
  familyId:selectedFamily.id,chartId:selectedChart.id,seriesKey:selectedSeries?.key||null,
  bandEnabled:params.get('band')==='1',
  heroRange:normalizeHeroRange(reqHero,hasHours),
  heroScale:params.get('hero_scale')==='symlog'?'symlog':'linear',heroHsdir:params.get('hero_hsdir')==='1',
 };
}

const HERO_VECTORS=Object.freeze([
 {id:'conn',label:'OR connection limits',color:'#48d9ef',sources:[['transport_rejections',['concurrent_conn_rejected','conn_rejected']]]},
 {id:'circ',label:'Circuit, onionskin & protocol drops',color:'#f1d05d',sources:[['circuit_rejections',['circuit_rejections']],['onionskin_drops',['fast','ntor','tap']],['protocol_closes',['protocol_closes']]]},
 {id:'dir',label:'Directory rejections & stalled streams',color:'#ff9873',sources:[['directory_rejections',['begindir_hsdir_rejected','begindir_rate_rejected','begindir_rejected']],['directory_requests',['no_request']]]},
 {id:'hsdir',label:'HSDir GET + POST volume',color:'#aabaff',hsdir:true,sources:[['directory_requests',['hs_get','hs_post']]]},
 {id:'intro',label:'Introduction refusals',color:'#55dda3',sources:[['introduction_rejections',['introduction_rejections']]]},
 {id:'queue',label:'Queue-limit circuit kills',color:'#e580a8',sources:[['queue_limit_circuits',['circuit_killed_max_cell']]]},
 {id:'exit',label:'Exit stream-limit rejections',color:'#9de2bb',sources:[['exit_stream_rejections',['exit_stream_rejections']]]},
]);
const HERO_ALERT_EXCLUDE=new Set([
 'hsdir_fetches:success','hsdir_uploads:success',
 'onionskin_drops:ntor_v3','queue_limit_circuits:circuit_killed_max_cell_outq',
 'incoming_connections:total','incoming_connections:accepted','incoming_connections:rejected',
]);
const HERO_CONTEXT_CHARTS=new Set(['incoming_connections']);
const isAlertEligibleSeries=(c,s)=>!!(c&&!isGaugeKind(c)&&Array.isArray(c.display_keys)&&s&&c.display_keys.includes(s.key)&&!HERO_ALERT_EXCLUDE.has(c.id+':'+s.key));
const alertStateFor=(rank,n,flatZero)=>flatZero||!isFiniteNumber(rank)?'neutral':n>=100&&rank>=99?'high':n>=20&&rank>=90?'elevated':'neutral';
function quantileSorted(sorted,fraction){
 if(!Array.isArray(sorted)||!sorted.length||!isFiniteNumber(fraction)||fraction<0||fraction>1)return null;
 const pos=(sorted.length-1)*fraction,lo=Math.floor(pos),hi=Math.ceil(pos);
 const val=sorted[lo]+(sorted[hi]-sorted[lo])*(pos-lo);
 return isFiniteNumber(val)?val:null;
}
function midrankPercentile(val,sorted,inBase=false){
 const denom=inBase?sorted.length-1:sorted.length;
 if(denom<=0)return {rank:null,n:0,flatZero:false};
 if(val===0&&sorted[sorted.length-1]===0)return {rank:0,n:denom,flatZero:true};
 let lo=0,hi=sorted.length;
 while(lo<hi){const mid=(lo+hi)>>>1;if(sorted[mid]<val)lo=mid+1;else hi=mid;}
 const less=lo;hi=sorted.length;
 while(lo<hi){const mid=(lo+hi)>>>1;if(sorted[mid]<=val)lo=mid+1;else hi=mid;}
 return {rank:100*(less+0.5*Math.max(0,(lo-less)-(inBase?1:0)))/denom,n:denom,flatZero:false};
}

function prepareHeroPlot(D,heroRange=null,includeHsdir=true,cachedSeries=null){
 if(!D||!Array.isArray(D.charts)||!Array.isArray(D.families))return null;
 const r=normalizeHeroRange(heroRange,hasHourPayload(D)),meta=HERO_RANGE_META[r];
 const win=r==='24h'?HOUR:DAY,modePrefix=win===HOUR?'h:':'d:';
 const chartsById=new Map();
 for(const c of D.charts)if(c&&typeof c.id==='string')chartsById.set(c.id,c);
 let rawEnd=validTime(D.end)?D.end:-Infinity;
 if(rawEnd===-Infinity)for(const c of D.charts)if(validTime(c?.end)&&c.end>rawEnd)rawEnd=c.end;
 if(!validTime(rawEnd))return null;
 const end=Math.floor(rawEnd/win)*win,dayBaselineEnd=Math.floor(rawEnd/DAY)*DAY-DAY,rw=D.reference_window;
 const baselineEnd=validTime(rw?.end)&&rw.end<=end-DAY?rw.end:dayBaselineEnd;
 const baselineStart=validTime(rw?.start)&&rw.end===baselineEnd&&rw.start<baselineEnd?rw.start:calendarWindowStart(baselineEnd,18);
 const seriesCache=cachedSeries||new Map();
 const indexSeries=(cid,s,needBaseline=false)=>{
  if(!s)return null;
  const cacheKey=modePrefix+cid+':'+s.key;
  let entry=seriesCache.get(cacheKey);
  if(entry&&(!needBaseline||entry.baseline!==null))return entry;
  const c=entry?.c||chartsById.get(cid),scale=c?displayScale(c):1;
  let values=entry?.values,populations=entry?.populations,minTs=entry?.minTs??Infinity;
  if(!entry){
   if(win===HOUR){
    const obs=hourlyObservations(s,scale,end-DAY,end);
    values=new Map();populations=obs.populations;
    for(const [t,v] of obs.valuesByTime)if(v!==null){values.set(t,v);if(t<minTs)minTs=t;}
   }else{
    values=new Map();populations=new Map();
    for(const p of s.history?.daily||[]){
     if(!qualifiedDailyRow(p,scale)||p[0]>end)continue;
     values.set(p[0],p[1]);populations.set(p[0],p[2]);
     if(p[0]<minTs)minTs=p[0];
    }
   }
  }
  let baseline=entry?.baseline??null,baselineTimes=entry?.baselineTimes??null;
  if(needBaseline){
   baseline=[];baselineTimes=new Set();
   if(win===HOUR){
    const seenHour=new Map();
    for(const p of s.history?.sampled_hour||[]){
     if(!qualifiedSampledHourRow(p,scale))continue;
     const t=p[0],pr=p[1]/p[2];
     if(isFiniteNumber(pr)&&t-HOUR>=baselineStart&&t<=baselineEnd)seenHour.set(t,pr);
    }
    for(const [t,pr] of seenHour){baseline.push(pr);baselineTimes.add(t);}
   }else{
    for(const [t,v] of values){
     const pr=v/populations.get(t);
     if(isFiniteNumber(pr)&&t-DAY>=baselineStart&&t<=baselineEnd){baseline.push(pr);baselineTimes.add(t);}
    }
   }
   baseline.sort((a,b)=>a-b);
  }
  entry={c,s,values,populations,baseline,baselineTimes,minTs};
  seriesCache.set(cacheKey,entry);
  return entry;
 };
 let earliestTs=Infinity,hasMissingSpec=false,configuredSources=0;
 const familySpecs=D.families.map(f=>{
  const members=[];
  for(const cid of f.children||[]){
   const c=chartsById.get(cid);
   if(!c||isGaugeKind(c)||!Array.isArray(c.series)||!Array.isArray(c.display_keys))continue;
   for(const s of c.series){
    if(!isAlertEligibleSeries(c,s))continue;
    const idx=indexSeries(cid,s,true);
    if(idx.minTs<earliestTs)earliestTs=idx.minTs;
    members.push({c,s,idx});
   }
  }
  return {id:f.id,icon:f.icon,title:f.title,primary:f.primary,members};
 });
 const vectors=[];
 for(const spec of HERO_VECTORS){
  if(spec.hsdir&&!includeHsdir)continue;
  const sources=[];let missingSources=0;
  for(const [cid,keys] of spec.sources){
   configuredSources+=keys.length;
   const chartObj=chartsById.get(cid);
   if(!chartObj||!Array.isArray(chartObj.series)){missingSources+=keys.length;continue;}
   for(const key of keys){
    const s=chartObj.series.find(item=>item&&item.key===key);
    if(!s){missingSources++;continue;}
    const idx=indexSeries(cid,s,false);
    if(idx.minTs<earliestTs)earliestTs=idx.minTs;
    sources.push(idx);
   }
  }
  if(missingSources>0)hasMissingSpec=true;
  if(sources.length)vectors.push({...spec,sources,missingSources:missingSources>0,values:[],partial:[]});
 }
 const resolveGauge=(cid,key,unit)=>{
  const s=chartsById.get(cid)?.series?.find(item=>item&&item.key===key);
  const idx=s?indexSeries(cid,s,true):null;
  if(idx&&idx.minTs<earliestTs)earliestTs=idx.minTs;
  let maxHist=0;
  if(idx)for(const v of idx.values.values())if(v>maxHist)maxHist=v;
  const peak=win===HOUR?(s?.peaks?.hour?.fleet_peak??s?.peaks?.day?.fleet_peak):s?.peaks?.day?.fleet_peak;
  const rawPeak=Math.max(isFiniteNumber(peak)&&peak>0?peak:0,maxHist);
  return {cid,key,unit,idx,rawPeak,windowPeak:0,scalePeak:rawPeak>0?rawPeak:1,latestRank:null,values:[],missing:[]};
 };
 const openCircuits=resolveGauge('open_circuits','open_circuits','circuits');
 const directoryWaiting=resolveGauge('directory_waiting','directory_waiting','connections');
 const openConnections=chartsById.has('open_connections')?resolveGauge('open_connections','open_connections','connections'):null;
 const incChart=chartsById.get('incoming_connections');
 let incomingConnections=null;
 if(incChart){
  const resolveInc=key=>{
   const s=incChart.series?.find(item=>item&&item.key===key);
   const idx=s?indexSeries('incoming_connections',s,key==='total'):null;
   if(idx&&idx.minTs<earliestTs)earliestTs=idx.minTs;
   let maxHist=0;
   if(idx)for(const v of idx.values.values())if(v>maxHist)maxHist=v;
   const peak=win===HOUR?(s?.peaks?.hour?.fleet_peak??s?.peaks?.day?.fleet_peak):s?.peaks?.day?.fleet_peak;
   const rawPeak=Math.max(isFiniteNumber(peak)&&peak>0?peak:0,maxHist);
   return {key,idx,rawPeak,values:[],latestRank:null};
  };
  incomingConnections={total:resolveInc('total'),accepted:resolveInc('accepted'),rejected:resolveInc('rejected')};
 }
 if(!Number.isFinite(earliestTs))return null;
 const start=r==='all'?earliestTs-DAY:meta.months!==null?calendarWindowStart(end,meta.months):end-(r==='24h'?DAY:7*DAY);
 if(!validTime(start)||!validTime(end)||end<=start||(end-start)/win>MAX_PLOT_INTERVALS)return null;
 const times=[];
 for(let t=start+win;t<=end;t+=win)times.push(t);
 const totals=[],missingRates=[],partialRates=[],relayRangeByTime=new Map(),gaugeList=[openCircuits,directoryWaiting];
 const notePop=(t,p)=>{
  if(Number.isInteger(p)&&p>0){
   const cur=relayRangeByTime.get(t);
   if(!cur)relayRangeByTime.set(t,[p,p]);
   else{if(p<cur[0])cur[0]=p;if(p>cur[1])cur[1]=p;}
  }
 };
 let maxTotal=0,maxSeries=0,hasAnyMeasured=false,hasPartialRate=false,hasMissingRate=false,hasMissingGauge=false;
 for(let i=0;i<times.length;i++){
  const t=times[i];
  let sum=0,anyRate=false,allSourcesCovered=vectors.length>0&&!hasMissingSpec;
  for(const vec of vectors){
   let vSum=0,vAny=false,vComplete=!vec.missingSources;
   for(const src of vec.sources){
    const val=src.values.get(t);
    if(val!==undefined){vSum+=val;vAny=true;notePop(t,src.populations.get(t));}
    else vComplete=false;
   }
   if(!vComplete)allSourcesCovered=false;
   const val=vAny&&isFiniteNumber(vSum)?vSum:null;
   vec.values.push(val);vec.partial.push(val!=null&&!vComplete);
   if(val!=null){sum+=val;anyRate=true;hasAnyMeasured=true;if(val>maxSeries)maxSeries=val;}
  }
  if(anyRate&&isFiniteNumber(sum)){
   const partial=!allSourcesCovered;
   totals.push(sum);missingRates.push(false);partialRates.push(partial);
   if(partial)hasPartialRate=true;
   if(sum>maxTotal)maxTotal=sum;
  }else{totals.push(null);missingRates.push(true);partialRates.push(false);hasMissingRate=true;}
  for(const g of gaugeList){
   const v=g.idx?g.idx.values.get(t)??null:null;
   g.values.push(v);g.missing.push(v==null);
   if(v!=null){hasAnyMeasured=true;notePop(t,g.idx.populations.get(t));}
   else hasMissingGauge=true;
  }
  if(openConnections){
   const v=openConnections.idx?openConnections.idx.values.get(t)??null:null;
   openConnections.values.push(v);openConnections.missing.push(v==null);
   if(v!=null)hasAnyMeasured=true;
  }
  if(incomingConnections){
   for(const item of [incomingConnections.total,incomingConnections.accepted,incomingConnections.rejected]){
    const v=item.idx?item.idx.values.get(t)??null:null;
    item.values.push(v);
    if(v!=null)hasAnyMeasured=true;
   }
  }
 }
 for(const g of openConnections?[...gaugeList,openConnections]:gaugeList){
  let winPeak=0;
  for(const v of g.values)if(v!=null&&v>winPeak)winPeak=v;
  g.windowPeak=winPeak;
  g.scalePeak=r!=='all'?(winPeak>0?winPeak:1):(g.rawPeak>0?g.rawPeak:1);
 }
 const sevOrder={neutral:1,elevated:2,high:3},refPeriod=win===HOUR?'hour':'day';
 const rankSeriesAt=(s,idx,t)=>{
  const v=idx.values.get(t);
  if(v===undefined)return null;
  const pop=idx.populations.get(t),pr=v/pop;
  if(!isFiniteNumber(pr))return null;
  let rank=null,n=0,flatZero=false;
  const ref=s.reference?.[refPeriod];
  if(t===end&&(refPeriod==='hour'||rawEnd===end)&&ref&&typeof ref==='object'){
   if(s.current?.[refPeriod]?.valid===true&&Number.isInteger(ref.n)&&ref.n>0&&(ref.flat_zero||(isFiniteNumber(ref.current_percentile)&&ref.current_percentile>=0&&ref.current_percentile<=100))){
    flatZero=!!ref.flat_zero;rank=flatZero?0:ref.current_percentile;n=ref.n;
   }
  }else{
   ({rank,n,flatZero}=midrankPercentile(pr,idx.baseline,idx.baselineTimes.has(t)));
  }
  return {rank,n,flatZero,pr,pop};
 };
 const retainedTotals=new Map();
 for(const vec of vectors){
  for(const src of vec.sources){
   if(win===HOUR){
    const scale=src.c?displayScale(src.c):1,srcVals=new Map();
    for(const p of src.s.history?.sampled_hour||[]){
     if(qualifiedSampledHourRow(p,scale)&&p[0]<=end)srcVals.set(p[0],p[1]);
    }
    for(const [t,v] of src.values)srcVals.set(t,v);
    for(const [t,v] of srcVals)retainedTotals.set(t,(retainedTotals.get(t)??0)+v);
   }else{
    for(const [t,v] of src.values)retainedTotals.set(t,(retainedTotals.get(t)??0)+v);
   }
  }
 }
 let ratePeak=0;
 for(const v of retainedTotals.values())if(isFiniteNumber(v)&&v>ratePeak)ratePeak=v;
 let rateRank=null;
 const tEnd=times.length?times[times.length-1]:null;
 if(tEnd!==null&&totals[times.length-1]!=null){
  const activeSources=[];
  let curPr=0;
  for(const vec of vectors){
   for(const src of vec.sources){
    const v=src.values.get(tEnd),pop=src.populations.get(tEnd),pr=Number.isInteger(pop)&&pop>0?v/pop:NaN;
    if(v!==undefined&&isFiniteNumber(pr)){activeSources.push({src,curPop:pop});curPr+=pr;}
   }
  }
  if(activeSources.length&&isFiniteNumber(curPr)){
   const baseSum=new Map(),fleetSum=new Map(),baseCnt=new Map();
   for(const {src,curPop} of activeSources){
    const srcBase=new Map();
    if(win===HOUR){
     const scale=src.c?displayScale(src.c):1;
     for(const p of src.s.history?.sampled_hour||[]){
      if(!qualifiedSampledHourRow(p,scale))continue;
      const t=p[0],pr=p[1]/p[2];
      if(isFiniteNumber(pr)&&t-HOUR>=baselineStart&&t<=baselineEnd)srcBase.set(t,pr);
     }
    }else{
     for(const [t,v] of src.values){
      const pop=src.populations.get(t),pr=Number.isInteger(pop)&&pop>0?v/pop:NaN;
      if(isFiniteNumber(pr)&&t-DAY>=baselineStart&&t<=baselineEnd)srcBase.set(t,pr);
     }
    }
    for(const [t,pr] of srcBase){
     baseSum.set(t,(baseSum.get(t)??0)+pr);
     fleetSum.set(t,(fleetSum.get(t)??0)+pr*curPop);
     baseCnt.set(t,(baseCnt.get(t)??0)+1);
    }
   }
   const baseline=[],fleetBaseline=[],baselineTimes=new Set();
   for(const [t,cnt] of baseCnt)if(cnt===activeSources.length){
    const sumPr=baseSum.get(t),sumFl=fleetSum.get(t);
    if(isFiniteNumber(sumPr)){baseline.push(sumPr);baselineTimes.add(t);}
    if(isFiniteNumber(sumFl))fleetBaseline.push(sumFl);
   }
   baseline.sort((a,b)=>a-b);
   fleetBaseline.sort((a,b)=>a-b);
   const p90Fleet=fleetBaseline.length>=20?quantileSorted(fleetBaseline,0.90):null;
   const p99Fleet=fleetBaseline.length>=100?quantileSorted(fleetBaseline,0.99):null;
   rateRank={...midrankPercentile(curPr,baseline,baselineTimes.has(tEnd)),activeSources:activeSources.length,totalSources:configuredSources,p90Fleet,p99Fleet};
  }
 }
 if(tEnd!==null){
  for(const g of openConnections?[...gaugeList,openConnections]:gaugeList){
   if(g.idx&&g.values[times.length-1]!=null){
    const ranked=rankSeriesAt(g.idx.s,g.idx,tEnd);
    if(ranked)g.latestRank={rank:ranked.rank,n:ranked.n,flatZero:ranked.flatZero};
   }
  }
  if(incomingConnections?.total?.idx&&incomingConnections.total.values[times.length-1]!=null){
   const ranked=rankSeriesAt(incomingConnections.total.idx.s,incomingConnections.total.idx,tEnd);
   if(ranked)incomingConnections.total.latestRank={rank:ranked.rank,n:ranked.n,flatZero:ranked.flatZero};
  }
 }
 const familyThresholds=[];
 for(const fam of familySpecs){
  if(!fam.members.length)continue;
  const pc=chartsById.get(fam.primary);
  if(fam.id==='exit'&&D.flags?.Exit===0&&pc?.series?.length&&pc.series.every(s=>s.reference?.[refPeriod]?.flat_zero===true))continue;
  const famPrimaryKey=pc?.primary_series||pc?.series?.[0]?.key;
  const cands=fam.members.map(({c,s,idx})=>{
   const rawPop=tEnd!==null?idx.populations.get(tEnd):null;
   const pop=(Number.isInteger(rawPop)&&rawPop>0)?rawPop
    :(Number.isInteger(s.current?.[refPeriod]?.relays)&&s.current[refPeriod].relays>0)?s.current[refPeriod].relays
    :(Number.isInteger(c.relays)&&c.relays>0?c.relays:null);
   const ref=s.reference?.[refPeriod];
   let n=0,p90Per=null,p99Per=null;
   if((refPeriod==='hour'||rawEnd===end)&&ref&&Number.isInteger(ref.n)&&ref.n>0&&(isFiniteNumber(ref.p90_per_relay)||isFiniteNumber(ref.p99_per_relay))){
    n=ref.n;
    p90Per=n>=20&&isFiniteNumber(ref.p90_per_relay)?ref.p90_per_relay:(idx.baseline.length>=20?quantileSorted(idx.baseline,0.90):null);
    p99Per=n>=100&&isFiniteNumber(ref.p99_per_relay)?ref.p99_per_relay:(idx.baseline.length>=100?quantileSorted(idx.baseline,0.99):null);
   }else if(idx.baseline.length>0){
    n=idx.baseline.length;
    p90Per=n>=20?quantileSorted(idx.baseline,0.90):null;
    p99Per=n>=100?quantileSorted(idx.baseline,0.99):null;
   }else if(ref&&Number.isInteger(ref.n)&&ref.n>0){
    n=ref.n;
    p90Per=n>=20&&isFiniteNumber(ref.p90_per_relay)?ref.p90_per_relay:null;
    p99Per=n>=100&&isFiniteNumber(ref.p99_per_relay)?ref.p99_per_relay:null;
   }
   const p90=p90Per!==null&&pop>0&&isFiniteNumber(p90Per*pop)?p90Per*pop:null;
   const p99=p99Per!==null&&pop>0&&isFiniteNumber(p99Per*pop)?p99Per*pop:null;
   return {id:fam.id,icon:fam.icon,title:fam.title,chartId:c.id,chartTitle:c.title,seriesKey:s.key,seriesName:s.name,n,pop,p90,p99,isPrimary:c.id===fam.primary&&s.key===famPrimaryKey};
  });
  const chosen=cands.find(x=>x.isPrimary)||cands.slice().sort((a,b)=>((b.p90??-1)-(a.p90??-1))||(b.n-a.n))[0];
  if(chosen)familyThresholds.push(chosen);
 }
 let hasMissingAlerts=false;
 const families=familySpecs.map(fam=>({
  id:fam.id,icon:fam.icon,title:fam.title,primary:fam.primary,
  rows:times.map(t=>{
   let bestSev=-1,bestRank=-Infinity,bestState='missing',bestN=0,bestSeries=null,bestChart=null,bestChartId=null,bestSeriesKey=null,alertCount=0;
   for(const {c,s,idx} of fam.members){
    const ranked=rankSeriesAt(s,idx,t);
    if(!ranked)continue;
    hasAnyMeasured=true;notePop(t,ranked.pop);
    const {rank,n,flatZero}=ranked;
    const state=alertStateFor(rank,n,flatZero);
    if(state==='high'||state==='elevated')alertCount++;
    const sev=sevOrder[state],candRank=isFiniteNumber(rank)?rank:0;
    if(sev>bestSev||(sev===bestSev&&candRank>bestRank)){
     bestSev=sev;bestRank=candRank;bestState=state;bestN=n;bestSeries=s.name;bestChart=c.title;bestChartId=c.id;bestSeriesKey=s.key;
    }
   }
   if(bestSev<0){hasMissingAlerts=true;return {state:'missing',rank:null,n:0,seriesName:null,chartTitle:null,chartId:null,seriesKey:null,alertCount:0};}
   return {state:bestState,rank:bestRank,n:bestN,seriesName:bestSeries,chartTitle:bestChart,chartId:bestChartId,seriesKey:bestSeriesKey,alertCount};
  }),
 }));
 if(!hasAnyMeasured)return null;
 return {range:r,start,end,window:win,times,vectors,totals,maxTotal,maxSeries,ratePeak,rateRank,missingRates,partialRates,families,familyThresholds,
  gauges:{openCircuits,directoryWaiting},openConnections,incomingConnections,relayRangeByTime,
  hasPartialRate,hasAnyMissing:hasMissingRate||hasMissingAlerts||hasMissingGauge};
}

function preparePlot(c,series,range,timeRanges=null){
 const scale=displayScale(c);
 const period=isPlainRecord(range)&&range.window?range:timeRanges
  ?(timeRanges.periods.find(p=>p.id===range)||timeRanges.periods.find(p=>p.id===timeRanges.default_range))
  :STANDALONE_PERIOD_FALLBACKS.find(p=>p.id===range);
 if(!period||!isFiniteNumber(scale)||!validTime(c.end))return null;
 const isHours=period.window.unit==='hours',window=isHours?HOUR:DAY,end=Math.floor(c.end/window)*window;
 let start;
 if(isHours){
  start=validTime(c.start)?Math.floor(c.start/window)*window:end-period.window.count*HOUR;
 }else if(period.window.unit==='all_time'){
  let minSupported=Infinity;
  for(const s of series)for(const p of s.history?.daily||[])if(qualifiedDailyRow(p,scale)&&p[0]<minSupported)minSupported=p[0];
  if(!Number.isFinite(minSupported))return null;
  start=minSupported-window;
 }else start=periodWindowStart(end,period)??NaN;
 if(!validTime(start)||!validTime(end)||end<=start||(end-start)/window>MAX_PLOT_INTERVALS)return null;
 const steps=(end-start)/window,anyMissing=new Uint8Array(steps),missing=[],list=[];
 let hasPlottedValue=false;
 for(let si=0;si<series.length;si++){
  const s=series[si],lastSeries=si===series.length-1;
  let valuesByTime,populations;
  if(isHours){
   ({valuesByTime,populations}=hourlyObservations(s,scale,start,end));
  }else{
   valuesByTime=new Map();populations=new Map();
   for(const p of s.history?.daily||[]){
    if(!Array.isArray(p))continue;
    const t=p[0];
    if(!validTime(t)||t<=start||t>end)continue;
    const ok=qualifiedDailyRow(p,scale);
    valuesByTime.set(t,ok?p[1]:null);
    if(ok)populations.set(t,p[2]);else populations.delete(t);
   }
  }
  const values=[];
  for(let i=0,t=start+window;i<steps;i++,t+=window){
   const value=valuesByTime.get(t)??null;
   values.push([t,value]);
   if(value===null)anyMissing[i]=1;else hasPlottedValue=true;
   if(lastSeries&&anyMissing[i])missing.push([t,null]);
  }
  list.push({...s,values,populations});
 }
 return hasPlottedValue?{start,end,window,list,missing}:null;
}

function ordinal(value){const n=Number(value),mod=n%100;return n+(mod>=11&&mod<=13?'th':n%10===1?'st':n%10===2?'nd':n%10===3?'rd':'th');}
function formatPercentileOrdinal(p){
 const r=Math.round(p),op=p>0&&p<1?'<':p<100&&r===100?'>':p<r&&(r===90||r===95||r===99)?'<':'';
 return op+ordinal(p>0&&p<1?1:p<100&&r===100?99:r);
}
const unavailableRank=reason=>({text:'Percentile unavailable',className:'unknown',rank:null,reason});
function percentile(ref,minWindows=30){
 if(!ref||!Number.isInteger(ref.n)||ref.n<=0)return unavailableRank('No reference observations');
 if(ref.flat_zero)return {text:'Percentile —',className:'flat',rank:null,reason:'Reference values are all zero'};
 const p=ref.current_percentile;
 if(p==null||!Number.isFinite(p)||p<0||p>100)return unavailableRank('Insufficient current coverage');
 const text=formatPercentileOrdinal(p)+' percentile';
 const className=ref.n<minWindows?'unknown':p>=99&&ref.n>=100?'high':p>=90?'elevated':'neutral';
 return {text,className,rank:p,reason:ref.n<minWindows?`${ref.n} reference windows`:''};
}
function percentileRuler(state,label=state.text){if(state.rank==null)return `<div class="rank-empty">${escapeHtml(state.reason)}</div>`;return `<div class="rank-ruler ${state.className}" role="img" aria-label="${escapeHtml(label)} on a zero-to-100 percentile scale"><div class="rank-track"><span class="rank-marker" style="left:${state.rank}%"></span></div><div class="rank-ticks"><span>0</span><span>50</span><span>100</span></div></div>`;}
function percentileDisplay(state,prefix=''){
 const label=prefix?`${prefix}: ${state.text}`:state.text;
 return `<span class="percentile ${state.className}">${escapeHtml(label)}</span>${percentileRuler(state,label)}${state.rank!=null&&state.reason?`<small class="rank-note">${escapeHtml(state.reason)}</small>`:''}`;
}
function niceScale(maximum){
 if(!isFiniteNumber(maximum)||!(maximum>0))return {max:1,ticks:[0,1]};
 const raw=maximum/4,power=10**Math.floor(Math.log10(raw));
 if(!(power>0))return {max:maximum,ticks:[0,maximum]};
 const part=raw/power,factor=(power===1?[1,2,5,10]:[1,2,2.5,5,10]).find(x=>x>=part)||10;
 const step=factor*power,max=Math.ceil(maximum/step)*step;
 if(!isFiniteNumber(max))return {max:maximum,ticks:[0,.25,.5,.75,1].map(fraction=>maximum*fraction)};
 const ticks=[],n=Math.round(max/step);
 for(let i=0;i<=n;i++)ticks.push(i*step);
 return {max,ticks};
}
function axisNumber(n){if(!isFiniteNumber(n))return 'Unavailable';if(n===0)return '0';if(Math.abs(n)>=1e12||Math.abs(n)<1e-4)return n.toExponential(1).replace('.0e','e');if(Math.abs(n)>=1e6)return millionFormat.format(n/1e6)+'M';if(Math.abs(n)>=1e3)return wholeFormat.format(n);if(Math.abs(n)>=1)return (Number.isInteger(n)?wholeFormat:decimalFormat).format(n);return fractionFormat.format(n);}
function xAxisTicks(start,end,width,left,right,win=DAY){
 if(!validTime(start)||!validTime(end)||end<=start)return [];
 const span=end-start,plotWidth=width-left-right,hourly=win===HOUR&&span<=2*DAY;
 const format=hourly?null:span<=120*DAY?dayFormat:monthFormat,stepUnit=win===HOUR?HOUR:DAY;
 const labelFor=t=>hourly?time(t):format.format(new Date(t*1000));
 const preferred=width<450?2:width>1600?8:width>1100?6:4,maximum=Math.min(preferred,Math.max(1,Math.floor(span/stepUnit)));
 const startLbl=labelFor(start),endLbl=labelFor(end);
 for(let cnt=maximum;cnt>=1;cnt--){
  const ticks=[];
  let previousRight=null,readable=true;
  for(let i=0;i<=cnt;i++){
   const t=start+span*i/cnt,label=i===0?startLbl:i===cnt?endLbl:labelFor(t),anchor=i===0?'start':i===cnt?'end':'middle';
   // Eight pixels per character conservatively bounds these English labels in 13px Arial; mobile font is smaller.
   const size=label.length*8,edge=left+(t-start)/span*plotWidth-(anchor==='end'?size:anchor==='middle'?size/2:0);
   if((i>0&&label===ticks[i-1].label)||(previousRight!==null&&edge-previousRight<8)){readable=false;if(cnt>1)break;}
   previousRight=edge+size;ticks.push({t,label,anchor});
  }
  // Actual four-digit-year endpoint labels fit the 240px minimum even with no interior-label room.
  if(readable||cnt===1)return ticks;
 }
}
const FOCUS_ATTRS=['id','data-focus-key','data-view-heading','data-view',
 'data-layout','data-all-range','data-hero-range','data-hero-scale','data-hero-hsdir','data-hero-plot','data-family-jump',
 'data-series-select','data-band-toggle','data-family','data-child','data-explore','data-copy','data-query-kind','data-methods','data-chart-plot'];
const DISPLAY_RELAY_ROLES=[['guard','guard'],['middle_only','middle-only'],['exit','exit'],['hsdir','HSDir'],['v2dir','V2Dir']];
const TOR_FIXES=Object.freeze([
 {issue:'HSDir descriptor-signature log flood',status:'Open',statusClass:'open',release:'Related fixes in 0.4.9.15',links:[['#41339','https://gitlab.torproject.org/tpo/core/tor/-/issues/41339']]},
 {issue:'Directory request and HSDir outcome metrics',status:'Open',statusClass:'open',release:'—',links:[['#41460','https://gitlab.torproject.org/tpo/core/tor/-/issues/41460']]},
 {issue:'MetricsPort response under main-loop load',status:'Open',statusClass:'open',release:'—',links:[['#41458','https://gitlab.torproject.org/tpo/core/tor/-/issues/41458']]},
 {issue:'Connection gauge accounting',status:'Open',statusClass:'open',release:'—',links:[['#41350','https://gitlab.torproject.org/tpo/core/tor/-/issues/41350']]},
 {issue:'BEGIN_DIR rate/lifetime limits and HSDir request limit',status:'Released',statusClass:'released',release:'0.4.9.15',links:[['Release','https://forum.torproject.org/t/tor-relays-tor-0-4-9-15-released-new-dos-defenses/22331']]},
 {issue:'Digest filtering, bounded diagnostics and connection indexing',status:'Released',statusClass:'released',release:'0.4.9.15',links:[
  ['Digest filtering','https://gitlab.torproject.org/tpo/core/tor/-/commit/37a6d04b09a0eb6f1c4fc63324ae245d73c9e3d3'],
  ['Diagnostics','https://gitlab.torproject.org/tpo/core/tor/-/commit/426731748e094e077c386af5011e0aa59411191e'],
  ['Connection lists','https://gitlab.torproject.org/tpo/core/tor/-/commit/b035c0f5823313d83273d81856803da85b3023d9'],
 ]},
]);

function createObservatory(data,browser,initialRelease=null){
 validateDataGraph(data);
 let D=data,release=initialRelease;
 const {document,location}=browser;
 const window=browser;
 const $=selector=>document.querySelector(selector);
 let timeRanges=resolveTimeRanges(D);
 let periodMap=new Map(timeRanges.periods.map(p=>[p.id,p]));
 const periodFor=id=>periodMap.get(id)||periodMap.get(timeRanges.default_range);
 let charts=new Map(D.charts.map(c=>[c.id,c]));
 let families=new Map(D.families.map(f=>[f.id,f]));
 const chart=id=>charts.get(id),family=id=>families.get(id);
 let {view,range,layout,familyId,chartId,seriesKey,bandEnabled,heroRange,heroScale,heroHsdir}=parseLocation(D,location.search,location.hash,browser.DEFAULT_DESIGN);
 const parseRequestedHeroRange=search=>{const reqHero=new URLSearchParams(search).get('hero_range');return isHeroRange(reqHero)?reqHero:null;};
 let requestedHeroRange=parseRequestedHeroRange(location.search);
 let plotCache=new WeakMap(),heroCache=new Map(),urlCache=null,lastHeaderH=null,touchPlot=null;
 const plotMeta=new WeakMap(),copyTimers=new WeakMap(),copyRequests=new WeakMap();
const chartCache=c=>{let cache=plotCache.get(c);if(!cache){cache=new Map();plotCache.set(c,cache);}return cache;};
const shown=c=>{const cc=chartCache(c);let out=cc.get('shown');if(!out){out=c.series.filter(s=>c.display_keys.includes(s.key));cc.set('shown',out);}return out;};
const primary=c=>{const cc=chartCache(c);let def=cc.get('primary');if(!def){def=c.series.find(s=>s.key===c.primary_series)||c.series[0];cc.set('primary',def);}return (view==='explorer'&&c.id===chartId&&seriesKey&&c.series.find(s=>s.key===seriesKey))||def;};
const isHourlyRelease=()=>hasHourPayload(D);
const defaultHeroRange=()=>isHourlyRelease()?'24h':'7d';
const resolveHeroRange=()=>normalizeHeroRange(requestedHeroRange,isHourlyRelease());
const currentValue=(s,c,period='day')=>{
 const current=s.current?.[period],value=current?.value;
 return current?.valid===false||!isFiniteNumber(value)||value<0||!isFiniteNumber(value*displayScale(c))?null:value;
};
const formatScaled=(raw,scaled)=>scaled===0&&raw>0?'<0.001':number(scaled);
function scaledBand(band,n,scale){
 const sLo=scaledProduct(band.p10_per_relay,n,scale),sMid=scaledProduct(band.p50_per_relay,n,scale),sHi=scaledProduct(band.p90_per_relay,n,scale);
 return isFiniteNumber(sLo)&&isFiniteNumber(sMid)&&isFiniteNumber(sHi)?[sLo,sMid,sHi]:null;
}
const currentText=(s,c,period='day')=>scaledText(c,currentValue(s,c,period));
function scaledText(c,value,population=1){
 const scale=displayScale(c);
 if(!isFiniteNumber(scale)||!isFiniteNumber(value)||value<0||!isFiniteNumber(population)||population<=0)return 'Unavailable';
 const scaled=scaledProduct(value,population,scale);
 return isFiniteNumber(scaled)?formatScaled(value,scaled):'Unavailable';
}
function headlineUnit(c,s){
 if(c.id==='directory_requests'){
  if(s.key==='no_request')return 'streams/s';
  if(s.key==='unparsed'||s.key==='circuit_dead')return 'classifications/s';
 }
 return c.headline_unit||c.display_unit;
}
const metricSubtitle=(c,r)=>metricSelectorTokens(c,plotted(c,r)).map(tokens=>`<code class="metric-selector">${tokens.map(token=>
 (token.highlight?`<span class="metric-selector-value">${escapeHtml(token.text)}</span>`:escapeHtml(token.text))+(token.breakAfter?'<wbr>':'')).join('')}</code>`).join('');
function url(v,r=range,f=familyId,c=chartId,sKey){
 const cacheable=urlCache&&sKey===undefined&&r===range&&f===familyId&&c===chartId;
 if(cacheable&&urlCache.has(v))return urlCache.get(v);
 const p=new URLSearchParams({view:v,range:r,layout,family:f,chart:c});
 const wantedSeries=sKey!==undefined?sKey:(c===chartId?seriesKey:null);
 const targetChart=chart(c);
 if(wantedSeries&&targetChart?.series?.some(s=>s.key===wantedSeries))p.set('series',wantedSeries);
 if(c===chartId&&bandEnabled&&sKey===undefined)p.set('band','1');
 if(requestedHeroRange!==null)p.set('hero_range',requestedHeroRange);
 if(heroScale==='symlog')p.set('hero_scale','symlog');
 if(heroHsdir)p.set('hero_hsdir','1');
 const out='?'+p.toString();
 if(cacheable)urlCache.set(v,out);
 return out;
}
const remember=()=>{try{window.history.pushState(null,'',url(view,range,familyId,chartId));return true;}catch{window.location.assign(url(view,range,familyId,chartId));return false;}};
const focusDestination=(selector='[data-view-heading]')=>{
 const matches=document.querySelectorAll?.(selector);
 const target=matches?.length?[...matches].find(el=>el.offsetParent!==null)||matches[0]:$(selector);
 target?.focus({preventScroll:true});
};
const announceStatus=message=>{const rgn=$('[data-live-status]');if(rgn)rgn.textContent=message;};
function fragmentTarget(){
 const raw=location.hash?location.hash.slice(1):'';
 if(!raw)return null;
 let id;try{id=decodeURIComponent(raw);}catch{id=raw;}
 return document.getElementById?.(id)||null;
}
function scrollToFragment(){
 const target=fragmentTarget();
 target?.scrollIntoView?.({block:'start'});
 return target;
}
function updateHeaderOffset(){
 const h=$('.nav')?.getBoundingClientRect?.()?.height;
 if(isFiniteNumber(h)&&h>=0&&h!==lastHeaderH){lastHeaderH=h;document.documentElement?.style?.setProperty?.('--header-offset',`${h+16}px`);}
}
const closeNavigation=()=>{$('.nav-links')?.classList.remove('show');$('.nav-toggle')?.setAttribute('aria-expanded','false');updateHeaderOffset();};
function setView(v,r=range,f=familyId,c=chartId,focusSelector,sKey){
 if(chartId!==c||sKey!==undefined){seriesKey=sKey||null;bandEnabled=false;}
 view=v;familyId=f;chartId=c;range=r;
 if(!remember())return false;
 render();closeNavigation();window.scrollTo?.(0,0);focusDestination(focusSelector);return true;
}
const redrawControl=selector=>{if(remember()){render();focusDestination(selector);}};
function versions(c){
 const v=c.builds,raw=typeof v==='string'?v:v?.display||v?.label||D.versions?.by_chart?.[c.id]?.display;
 const text=typeof raw==='string'?raw.replace(/\s*\(daemon version\)/g,''):'';
 return text?escapeHtml(text):'Running version not recorded';
}
function snapshotText(){
 const hourly=isHourlyRelease();
 if(!release)return `Saved snapshot · ${date(D.end)} ${time(D.end)} UTC<br><span>${hourly?'Hourly UTC publication planned':'Daily 00:00 UTC publication planned'}</span>`;
 const age=(release.now||(()=>Date.now()/1000))()-D.end,notices=[];
 if(release.fallback)notices.push('Showing previous verified release');
 if(age>(hourly?7200:108000))notices.push('Update delayed');
 if(release.failed)notices.push('Refresh unavailable; retaining verified release');
 return `Observed through ${date(D.end)} ${time(D.end)} UTC<br><span>Generated ${date(release.generatedAt)} ${time(release.generatedAt)} UTC · ${hourly?'Updated hourly':'Updated daily (00:00 UTC)'}</span>${notices.length?`<br><span>${notices.join(' · ')}</span>`:''}`;
}
const hero=()=>`<header class="page-title"><div><h1>OnionSurge</h1><p>Tor MetricsPort measurements from 1AEO relays.</p></div><div class="snapshot" aria-live="polite">${snapshotText()}</div></header>`;
function downloadLink(path,label,focusKey){
 const href=release?release.downloads.get(path):path;
 req(href,'Missing verified download');
 const name=release?`="${escapeHtml(release.id+'-'+path.split('/').at(-1))}"`:'';
 return `<a href="${escapeHtml(href)}" download${name} data-focus-key="${escapeHtml(focusKey)}">${escapeHtml(label)}</a>`;
}
function updateFreshness(status={}){
 if(!release)return;
 release={...release,...status};
 const target=$('.snapshot'),next=snapshotText();
 if(target&&target.innerHTML!==next)target.innerHTML=next;
}
function replaceRelease(nextData,nextRelease){
 const appEl=$('#app');
 const before={D,release,timeRanges,periodMap,charts,families,range,familyId,chartId,seriesKey,bandEnabled,heroRange,requestedHeroRange,plotCache,heroCache,html:appEl.innerHTML};
 const active=document.activeElement,hadAppFocus=!!appEl.contains?.(active);
 const focus=hadAppFocus?FOCUS_ATTRS.filter(name=>active.hasAttribute(name)).map(name=>[name,active.getAttribute(name)]):[];
 const matchInApp=node=>(!appEl.contains||appEl.contains(node))&&focus.every(([key,value])=>node.getAttribute(key)===value);
 const focusIndex=focus.length?[...document.querySelectorAll('['+focus[0][0]+']')].filter(matchInApp).indexOf(active):-1;
 const x=window.scrollX||0,y=window.scrollY||0;
 const canFocus=node=>!node.disabled&&!node.hasAttribute?.('disabled')&&!node.hasAttribute?.('hidden')&&!node.hasAttribute?.('inert');
 const restorePosition=()=>{
  if(hadAppFocus){
   const matchesAfter=focus.length?[...document.querySelectorAll('['+focus[0][0]+']')].filter(matchInApp):[];
   const exact=focusIndex>=0?matchesAfter[focusIndex]:null;
   const target=(exact&&canFocus(exact)?exact:matchesAfter.find(canFocus))||null;
   (target||$('[data-view-heading]'))?.focus({preventScroll:true});
  }
  window.scrollTo?.(x,y);
 };
 try{
  validateDataGraph(nextData);
  D=nextData;release=nextRelease;plotCache=new WeakMap();heroCache=new Map();
  timeRanges=resolveTimeRanges(D);periodMap=new Map(timeRanges.periods.map(p=>[p.id,p]));
  if(!periodMap.has(range)){
   const aliased=Object.hasOwn(timeRanges.legacy_aliases,range)?timeRanges.legacy_aliases[range]:null;
   range=aliased&&periodMap.has(aliased)?aliased:timeRanges.default_range;
  }
  heroRange=resolveHeroRange();
  charts=new Map(D.charts.map(c=>[c.id,c]));families=new Map(D.families.map(f=>[f.id,f]));
  if(!charts.has(chartId)){chartId=(families.get(familyId)||D.families[0]).primary;seriesKey=null;bandEnabled=false;}
  familyId=chart(chartId).family_id;
  if(seriesKey&&!chart(chartId).series.some(series=>series.key===seriesKey))seriesKey=null;
  render();restorePosition();
 }catch(error){
  ({D,release,timeRanges,periodMap,charts,families,range,familyId,chartId,seriesKey,bandEnabled,heroRange,requestedHeroRange,plotCache,heroCache}=before);
  try{render();}catch{appEl.innerHTML=before.html;}
  restorePosition();throw error;
 }
}
const subNavigation=()=>`<nav class="subnav" aria-label="OnionSurge views">${[['overview','Overview'],['explorer','Evidence explorer'],['fixes','Tor fixes'],['methods','Methods']].map(([id,label])=>`<a href="${url(id)}" data-view="${id}" class="${view===id?'active':''}" ${view===id?'aria-current="page"':''}>${label}</a>`).join('')}</nav>`;
function relayCountLine(s,period){
 const isHour=period==='hour',historical=!isHour&&period!=='day',summary=s.relay_counts?.[period],total=summary?.total??(historical?null:s.current?.[period]?.relays);
 const countText=value=>isFiniteNumber(value)&&value>=0?(value>0&&value<1?'<1':wholeFormat.format(value)):'—';
 if(!isFiniteNumber(total)||total<0)return '<div class="relay-population">• Relays: —</div>';
 let bounded=false;
 const chips=[`<span class="relay-count">${escapeHtml(countText(total)+' total')}</span>`];
 for(const [key,label] of DISPLAY_RELAY_ROLES){
  const exact=summary?.[key],bounds=summary?.role_bounds?.[key];
  let txt='—';
  if(isFiniteNumber(exact)&&exact>=0)txt=countText(exact);
  else if(total>0&&Array.isArray(bounds)&&bounds.length===2&&bounds.every(isFiniteNumber)&&bounds[0]>=0&&bounds[0]<=bounds[1]&&bounds[1]<=total){
   bounded=true;const low=cleanZero(Math.floor(bounds[0])),high=cleanZero(Math.ceil(bounds[1]));
   txt=low===high?wholeFormat.format(low):`${wholeFormat.format(low)}–${wholeFormat.format(high)}`;
  }
  chips.push(`<span class="relay-count">${escapeHtml(`${txt} ${label}`)}</span>`);
 }
 const scope=historical?`Mean daily contributing relays across ${countText(summary?.measured_windows)} measured days.`:isHour?'Contributing relays in this 1-hour UTC window.':'Contributing relays in this 24-hour UTC window.';
 const covered=summary?.coverage,coverageLabel=isFiniteNumber(covered)&&covered>=0&&covered<=1?(covered>0&&covered<.0005?'<0.1%':covered<1&&covered>=.9995?'>99.9%':coverageFormat.format(cleanZero(covered))):'';
 const coverage=coverageLabel?` Recorded flag coverage: ${coverageLabel}.`:'';
 const boundsNote=bounded?' Ranges include unclassified relays; endpoints are rounded outward.':'';
 const visibleNote=bounded?`<small class="rank-note">${escapeHtml(`Flag ranges include unclassified relays${coverageLabel?` (${coverageLabel} flag coverage)`:''}.`)}</small>`:'';
 return `<div class="relay-population" title="${escapeHtml(scope+coverage+' Roles use the last recorded flags in each window; flag groups can overlap.'+boundsNote)}">• Relays${historical?' (avg)':''}: ${chips.join(' · ')}</div>${visibleNote}`;
}
const isShortReference=ref=>!!(validTime(ref?.start)&&validTime(D.reference_window?.start)&&ref.start-D.reference_window.start>35*DAY);
function formatIntervalWindow(intervalStart,endTs,win){
 const sd=date(intervalStart),ed=date(endTs);
 if(win===HOUR)return sd===ed?`${sd} ${time(intervalStart)}–${time(endTs)} UTC`:`${sd} ${time(intervalStart)}–${ed} ${time(endTs)} UTC`;
 return `${sd} UTC`;
}
function currentWindowText(period){
 if(!validTime(D.end))return '';
 if(period==='hour'){const endH=Math.floor(D.end/HOUR)*HOUR;return formatIntervalWindow(endH-HOUR,endH,HOUR);}
 if(isHourlyRelease()&&D.end%DAY!==0)return 'Rolling 24h · '+formatIntervalWindow(D.end-DAY,D.end,HOUR);
 const dayEnd=Math.floor(D.end/DAY)*DAY;
 return formatIntervalWindow(dayEnd-DAY,dayEnd,DAY);
}
function coverageUnavailableReason(current){
 if(current?.valid===false&&Number.isInteger(current.relays)&&Number.isInteger(current.expected_relays)&&current.expected_relays>0&&isFiniteNumber(current.coverage)&&current.coverage<0.9){
  return `Insufficient current coverage (${coverageFormat.format(cleanZero(current.coverage))} · ${count(current.relays)}/${count(current.expected_relays)} relays; ≥90% required)`;
 }
 return 'Insufficient current coverage';
}
const historicalAverageText=(s,key,c)=>{const sm=s.historical_averages?.[key];return sm?.status==='ready'?scaledText(c,sm.value):'—';};
function historicalComparison(c,s,period){
 const key=period.summary_key,summary=s.historical_averages?.[key],comparison=summary?.comparison;
 if(!comparison)return '';
 let state=summary.status!=='ready'?unavailableRank('Insufficient historical coverage'):s.current?.day?.valid!==true||currentValue(s,c)===null?unavailableRank(coverageUnavailableReason(s.current?.day)):percentile(comparison,period.minimum_comparison_windows);
 if(state.className==='elevated'&&Number.isInteger(comparison.n)&&comparison.n<20)state={...state,className:'short'};
 const dates=validTime(comparison.start)&&validTime(comparison.end)?` ${date(comparison.start)}–${date(comparison.end)} UTC.`:'';
 const scope=Number.isInteger(comparison.n)&&comparison.n>0?`${count(comparison.n)} qualifying daily intervals.${dates}`:'No qualifying daily intervals.';
 const shortNote=Number.isInteger(comparison.n)&&comparison.n>0&&comparison.n<20?' Short comparison window (<20 days): neutral accent rather than alert threshold.':'';
 return `<div class="historical-comparison" title="${escapeHtml(scope+' Current 24-hour and historical values are divided by their reporting-relay counts.'+shortNote)}">${percentileDisplay(state,'Current 24h')}</div>`;
}
function historicalStat(c,s,period){
 const key=period.summary_key,summary=s.historical_averages?.[key],ready=summary?.status==='ready';
 const days=summary?`${count(summary.days)} / ${count(summary.expected_days)} days${ready?' measured':' available'}`:'No daily observations';
 const reason=period.window.unit==='all_time'||!(period.minimum_coverage>0)?'No qualifying daily observations are available':`At least ${coverageFormat.format(cleanZero(period.minimum_coverage))} daily coverage is required for ${period.average_label.toLowerCase()}`;
 const narrowed=validTime(summary?.observed_start)&&validTime(summary?.observed_end)&&validTime(summary?.start)&&validTime(summary?.end)&&(summary.observed_start>summary.start||summary.observed_end<summary.end);
 const datesTitle=narrowed?` title="${escapeHtml(`Measured observations: ${date(summary.observed_start)}–${date(summary.observed_end)} UTC.`)}"`:'';
 const compN=summary?.comparison?.n;
 const compVs=period.window.unit==='all_time'?'all-time':period.window.comparison_lag_days>0?`prior ${period.label}`:period.label;
 const sameDayCount=period.window.unit==='days'&&compN===period.window.count;
 const compDetail=sameDayCount?(compN<20?' (short window)':''):` (${count(compN)}d${compN<20?' · short window':''})`;
 const basisSuffix=Number.isInteger(compN)&&compN>0?` vs ${compVs}${compDetail}`:'';
 return `<div class="stat long-term-stat"><div class="stat-label">${escapeHtml(period.average_label)}</div><div class="stat-number" ${ready?'':`title="${escapeHtml(reason)}"`}>${escapeHtml(historicalAverageText(s,key,c))} <small>${escapeHtml(headlineUnit(c,s))} · fleet mean</small></div><div class="average-coverage">${escapeHtml(days)}</div>${validTime(summary?.start)&&validTime(summary?.end)?`<div class="average-dates"${datesTitle}>${date(summary.start)}–${date(summary.end)}</div>`:''}${historicalComparison(c,s,period)}<small class="stat-basis">Daily fleet mean · Current 24h ranked per relay${escapeHtml(basisSuffix)}</small>${relayCountLine(s,key)}</div>`;
}
function statPair(c,s){
 const currentCard=(period,label)=>{
  const isHour=period==='hour',current=s.current?.[period]||{},ref=s.reference?.[period],peak=s.peaks?.[period],curVal=currentValue(s,c,period),currentReady=curVal!==null;
  const state=!currentReady?unavailableRank(coverageUnavailableReason(current)):percentile(ref,20);
  const referenceText=scaledText(c,ref?.mean_per_relay,current.relays??null);
  const shortRef=isShortReference(ref),hasN=Number.isInteger(ref?.n)&&ref.n>0;
  const refAvgLabel=!shortRef?'18-month average':hasN?(isHour?`Reference average (${count(ref.n)}h)`:`Reference average (${count(ref.n)}d)`):'Available reference average';
  const refTitle=shortRef
   ?(isHour?'Average of available qualifying sampled one-hour windows per relay over this series’ available record within the 18-month window, scaled to this period’s reporting-relay count.':'Average of available qualifying 24-hour UTC windows per relay over this series’ available record within the 18-month window, scaled to this period’s reporting-relay count.')
   :(isHour?'Average of available qualifying sampled one-hour windows per relay within the 18-month reference, scaled to this period’s reporting-relay count.':'Average of available qualifying 24-hour UTC windows per relay within the 18-month reference, scaled to this period’s reporting-relay count.');
  const basisText=!shortRef?'Ranked per relay vs 18-mo reference':hasN?(isHour?`Ranked per relay vs ${count(ref.n)} sampled hrs`:`Ranked per relay vs ${count(ref.n)}-day record`):'Ranked per relay vs available record';
  const winText=currentWindowText(period);
  const peakTime=isHour?`${date(peak?.fleet_peak_at)} ${time(peak?.fleet_peak_at)} UTC`:`${date(peak?.fleet_peak_at)} UTC`;
  const peakTitle=isHour?`Highest retained sampled one-hour fleet value. ${peakTime}; ${count(peak?.fleet_peak_relays)} reporting relays.`:`Highest retained recorded 24-hour fleet value. ${peakTime}; ${count(peak?.fleet_peak_relays)} reporting relays.`;
  const peakLabel=isHour?'Sampled 1h fleet peak':'Recorded 24h fleet peak';
  const recordNote=isHour?'Highest sampled hourly fleet value':'Highest recorded 24h fleet value';
  return `<div class="stat ${state.className}"><div class="stat-label">${label}</div><div class="stat-number">${escapeHtml(scaledText(c,curVal))} <small>${escapeHtml(headlineUnit(c,s))} · fleet mean</small></div>${winText?`<div class="stat-window">${escapeHtml(winText)}</div>`:''}<div class="stat-reference" title="${refTitle}">${escapeHtml(refAvgLabel)}: <b>${escapeHtml(referenceText)}</b></div>${isFiniteNumber(peak?.fleet_peak)&&peak.fleet_peak>0?`<div class="stat-peak" title="${escapeHtml(peakTitle)}">${peakLabel}: ${escapeHtml(scaledText(c,peak.fleet_peak))}</div>`:''}${percentileDisplay(state)}${currentReady&&peak?.current_is_record?`<small class="record-note">${recordNote}</small>`:''}<small class="stat-basis">${escapeHtml(basisText)}</small>${relayCountLine(s,period)}</div>`;
 };
 const hasHour=s.current?.hour!==undefined,dynamicId=timeRanges.card_slots.fixed.includes(range)?timeRanges.card_slots.fallback_dynamic:range;
 if(hasHour){
  const fixedHistId=timeRanges.card_slots.fixed.find(id=>periodFor(id).window.unit!=='hours')||timeRanges.card_slots.fixed[1];
  return `<div class="stat-pair">${currentCard('hour','Last complete hour')}${currentCard('day','24-hour average')}${historicalStat(c,s,periodFor(dynamicId))}${historicalStat(c,s,periodFor(fixedHistId))}</div>`;
 }
 return `<div class="stat-pair">${currentCard('day','24-hour average')}${historicalStat(c,s,periodFor(dynamicId))}${historicalStat(c,s,periodFor(timeRanges.card_slots.fixed[0]))}${historicalStat(c,s,periodFor(timeRanges.card_slots.fixed[1]))}</div>`;
}
function reference(s){
 const hour=s.reference?.hour,day=s.reference?.day;
 const validSpan=r=>r&&validTime(r.start)&&validTime(r.end)&&r.start<=r.end;
 const shortRef=isShortReference(hour)||isShortReference(day);
 const refLabel=shortRef?'Available reference':'18-month reference';
 const refNote=shortRef?'Reference averages use per-relay history scaled to the current reporting count.':'18-month averages use per-relay history scaled to the current reporting count.';
 if(hour&&Number.isInteger(hour.n)&&hour.n>=0&&(validSpan(hour)||validSpan(day))&&(hour.n>0||(day&&Number.isInteger(day.n)&&day.n>0))){
  const spans=[hour,day].filter(validSpan),start=Math.min(...spans.map(r=>r.start)),end=Math.max(...spans.map(r=>r.end));
  return `<div class="reference"><small>${refLabel}: ${date(start)}–${date(end)} · ${count(hour.n)} sampled hours / ${count(day?.n??0)} days</small><small>${refNote}</small></div>`;
 }
 if(!Number.isInteger(day?.n)||day.n<=0||!validSpan(day))return '<div class="reference"><small>No reference observations yet</small></div>';
 return `<div class="reference"><small>${refLabel}: ${date(day.start)}–${date(day.end)} · ${count(day.n)} days</small><small>${refNote}</small></div>`;
}
function cachedPlot(c,list,r){
 const cache=chartCache(c),key='plot:'+r+':'+(list.length===1?'1:'+list[0].key:'*');
 if(cache.has(key))return cache.get(key);
 const result=preparePlot(c,list,periodMap.get(r)||r,timeRanges);
 cache.set(key,result);
 return result;
}
const hasBandQuantiles=r=>!!r&&isFiniteNumber(r.p10_per_relay)&&r.p10_per_relay>=0&&isFiniteNumber(r.p50_per_relay)&&r.p50_per_relay>=r.p10_per_relay&&isFiniteNumber(r.p90_per_relay)&&r.p90_per_relay>=r.p50_per_relay;
const isHourRange=r=>periodMap.get(r)?.window.unit==='hours'||r==='24h';
function bandReference(c,r){
 const selected=primary(c),cache=chartCache(c),cacheKey='band:'+r+':'+selected.key;
 if(cache.has(cacheKey))return cache.get(cacheKey);
 let out=null;
 const scale=displayScale(c),ref=selected.reference?.[isHourRange(r)?'hour':'day'];
 if(isFiniteNumber(scale)&&Number.isInteger(ref?.n)&&ref.n>=2&&hasBandQuantiles(ref)&&ref.p90_per_relay>ref.p10_per_relay){
  const p=cachedPlot(c,[selected],r),vals=p?.list[0]?.values,pops=p?.list[0]?.populations;
  if(vals?.some(([t,v])=>{const n=v!=null?pops.get(t):null;return n>0&&!!scaledBand(ref,n,scale);}))out=ref;
 }
 cache.set(cacheKey,out);
 return out;
}
function activeBand(c,r){return view==='explorer'&&c.id===chartId&&bandEnabled?bandReference(c,r):null;}
function plotted(c,r){
 if(activeBand(c,r))return [primary(c)];
 if(view==='explorer'&&c.id===chartId){const sel=primary(c);if(!shown(c).some(s=>s.key===sel.key))return [sel];}
 return shown(c);
}
function bandControl(c){
 const ref=bandReference(c,range),active=!!(ref&&view==='explorer'&&c.id===chartId&&bandEnabled),selected=primary(c),raw=selected.reference?.[isHourRange(range)?'hour':'day'];
 const hasCount=Number.isInteger(raw?.n)&&raw.n>=0;
 const unavailable=hasCount&&raw.n<2?`${raw.n} reference windows; 2 required`:hasCount&&hasBandQuantiles(raw)&&raw.p10_per_relay===raw.p90_per_relay?'10th and 90th percentiles are equal':'Reference percentile range unavailable';
 return `<div class="band-controls"><span>${escapeHtml(selected.name)}</span><button data-band-toggle aria-pressed="${active}" ${ref?'':'disabled'} class="${active?'active':''}">Percentile band ${active?'on':'off'}</button><small>${ref?(active?'Selected series only · 10th–90th percentiles; dashed median':'Optional: focus this series with its percentile band'):escapeHtml(unavailable)}</small></div>`;
}
function plotLabel(c,r,hasData=true){
 const period=periodFor(r);
 return `${c.title}; ${period?period.plot_label:r}; linear scale`+(hasData?'. Use arrow keys to inspect intervals.':'; No retained samples available for this chart.');
}
function reading(c,s){
 const seriesList=c.series,selectSeries=view==='explorer'&&c.id===chartId&&seriesList.length>1;
 const isMultiCard=view!=='explorer'&&(family(c.family_id)?.children?.length||0)>1;
 const labelText=isMultiCard?(c.title===s.name?`Primary chart: ${c.title}`:`Primary chart: ${c.title} — ${s.name}`):s.name;
 const header=selectSeries
  ?`<div class="series-toolbar"><label>Series <select data-series-select aria-label="Series for headline and percentile band">${seriesList.map(item=>`<option value="${escapeHtml(item.key)}" ${item.key===s.key?'selected':''}>${escapeHtml(item.name)}</option>`).join('')}</select></label></div>`
  :`<div class="primary-label">${escapeHtml(labelText)}</div>`;
 return `${header}${statPair(c,s)}${reference(s)}`;
}
function figure(c,r){
 const hasBand=!!activeBand(c,r),plottedSeries=plotted(c,r),hasGaps=!!cachedPlot(c,plottedSeries,r)?.missing.length,hourly=isHourRange(r);
 const legend=`<div class="legend">${plottedSeries.map(s=>`<span><i style="background:${seriesColor(s.color)}"></i>${escapeHtml(s.name)}</span>`).join('')}${hasBand?'<span><i class="band-key"></i>Reference 10th–90th percentiles</span><span><i class="median-key"></i>Reference median</span>':''}${hasGaps?'<span><i class="gap-key"></i>Missing data in one or more series</span>':''}</div>`;
 const notes=[isGaugeKind(c)?(hourly?'Hourly sample means.':'Daily sample means (00:00 UTC).'):(hourly?'Mean rates over 1-hour UTC intervals.':'Mean rates over 24-hour UTC intervals.')];
 if(c.display_scale===3600)notes.push('Rates are displayed per hour.');
 if(hasGaps)notes.push('Hatching marks missing or insufficiently covered intervals.');
 if(hasBand)notes.push('The band scales with each interval’s reporting-relay count.');
 const scopeLabel=c.builds?.scope||(isHourlyRelease()?'Latest-hour':'Latest UTC day');
 const buildPrefix=/builds$/i.test(scopeLabel)?scopeLabel:`${scopeLabel} builds`;
 const foot=`<figcaption><b>Footnote.</b> ${notes.join(' ')}</figcaption><div class="build-note"><span>${escapeHtml(c.metric_origin)}</span> · ${escapeHtml(buildPrefix)}: ${versions(c)}${c.introduced_note?`<br>${escapeHtml(c.introduced_note)}`:''}</div>`;
 return `<figure><div class="plot-wrap" data-chart-plot="${escapeHtml(c.id)}" data-range="${escapeHtml(r)}" tabindex="0" role="img" aria-label="${escapeHtml(plotLabel(c,r))}"></div>${legend}${foot}</figure>`;
}
const plotQuery=(c,r=range)=>isHourRange(r)?c.query:(c.history_queries?.daily?.value||c.query);
const coverageQuery=(c,r=range)=>isHourRange(r)?c.quality_query:(c.history_queries?.daily?.eligible||c.quality_query);
const headlineQuery=c=>c.history_queries?.current24?.value||null;
const controlGroup=(lbl,aria,attr,cur,opts,cls='')=>`<div class="control-group${cls?' '+cls:''}"><span>${lbl}</span><div class="range-control" role="group" aria-label="${aria}">${opts.map(([v,t])=>`<button ${attr}="${escapeHtml(v)}" class="${cur===v?'active':''}" aria-pressed="${cur===v}">${escapeHtml(t)}</button>`).join('')}</div></div>`;
function heroPlotData(r=heroRange,includeHsdir=heroHsdir){
 const key='r:'+r+':'+(includeHsdir?'1':'0');
 if(heroCache.has(key))return heroCache.get(key);
 const result=prepareHeroPlot(D,r,includeHsdir,heroCache);
 heroCache.set(key,result);
 return result;
}
function heroPlotLabel(r=heroRange,hasData=true,scaleMode=heroScale){
 const label=HERO_RANGE_META[r]?.title||'Last 7 days';
 return `Concurrent DoS activity; ${label}; ${scaleMode==='symlog'?'symlog':'linear'} scale`+(hasData?'. Use arrow keys to inspect intervals.':'; No retained samples available for this chart.');
}
function summarizeFleetBuilds(){
 const rows=Array.isArray(D.versions?.verified_runtime)?D.versions.verified_runtime:[];
 const groups=new Map();
 let patchedRelays=0,stockRelays=0;
 for(const item of rows){
  if(!item||typeof item.version!=='string'||!Number.isInteger(item.relays)||item.relays<=0)continue;
  const isPatched=item.patched===true,buildClass=isPatched?'patched':'stock';
  if(isPatched)patchedRelays+=item.relays;
  else stockRelays+=item.relays;
  const key=`${buildClass}:${item.version}`;
  const prev=groups.get(key);
  const safeUrl=typeof item.source_url==='string'&&/^https:\/\//i.test(item.source_url)?item.source_url:null;
  const verNote=typeof item.verification==='string'?item.verification:'';
  if(prev){prev.relays+=item.relays;if(!prev.sourceUrl&&safeUrl)prev.sourceUrl=safeUrl;if(!prev.verification&&verNote)prev.verification=verNote;}
  else groups.set(key,{version:item.version,buildClass,patched:isPatched,relays:item.relays,sourceUrl:safeUrl,verification:verNote});
 }
 const list=[...groups.values()].sort((a,b)=>(b.patched-a.patched)||(b.relays-a.relays)||a.version.localeCompare(b.version));
 return {list,patchedRelays,stockRelays,nativeTotal:D.versions?.native_total,verifiedTotal:D.versions?.runtime_verified_total,unverifiedTotal:D.versions?.runtime_unverified_relays,asOf:D.versions?.as_of};
}
function fleetOpsStrip(){
 const hasFlags=isPlainRecord(D.flags)&&Object.keys(D.flags).length>0;
 const builds=summarizeFleetBuilds();
 const hasBuildSummary=builds.list.length>0||(Number.isInteger(builds.unverifiedTotal)&&builds.unverifiedTotal>0);
 const hasDircache=isPlainRecord(D.dircache_cohort)&&Number.isInteger(D.dircache_cohort.configured_nonexit_zero);
 if(!hasFlags&&!hasBuildSummary&&!hasDircache)return '';
 const chips=[];
 if(hasFlags){
  const parts=[];
  if(Number.isInteger(D.flags.Running))parts.push(`${count(D.flags.Running)} Running`);
  if(Number.isInteger(D.flags.Guard))parts.push(`${count(D.flags.Guard)} Guard`);
  if(Number.isInteger(D.flags.Exit))parts.push(D.flags.Exit===0?'0 Exit (non-exit fleet)':`${count(D.flags.Exit)} Exit`);
  if(Number.isInteger(D.flags.HSDir))parts.push(`${count(D.flags.HSDir)} HSDir`);
  if(Number.isInteger(D.flags.V2Dir))parts.push(`${count(D.flags.V2Dir)} V2Dir`);
  if(parts.length){
   chips.push(`<div class="fleet-strip-item"><span class="fleet-strip-label">Fleet roles</span><span class="fleet-strip-value">${parts.map(p=>p.startsWith('0 Exit')?`<span class="fleet-pill-note">${escapeHtml(p)}</span>`:escapeHtml(p)).join(' · ')}</span></div>`);
  }
 }
 if(hasBuildSummary){
  const items=builds.list.map(b=>`${escapeHtml(b.version)} (${b.patched?'patched':'stock'}): ${count(b.relays)}`);
  if(Number.isInteger(builds.unverifiedTotal)&&builds.unverifiedTotal>0)items.push(`Unrecorded: ${count(builds.unverifiedTotal)}`);
  chips.push(`<div class="fleet-strip-item"><span class="fleet-strip-label">Fleet builds</span><span class="fleet-strip-value">${items.join(' · ')}</span></div>`);
 }
 if(hasDircache){
  chips.push(`<div class="fleet-strip-item"><span class="fleet-strip-label">DirCache 0 cohort</span><span class="fleet-strip-value">${count(D.dircache_cohort.configured_nonexit_zero)} non-exit relays</span></div>`);
 }
 if(!chips.length)return '';
 return `<section class="fleet-strip" aria-label="Measured fleet summary"><div class="fleet-strip-items">${chips.join('')}</div><a class="fleet-strip-link" href="${url('fixes')}" data-view="fixes">Tor builds &amp; fixes →</a></section>`;
}
const DOS_THRESHOLD_GROUPS=[
 ['Connection limits',new Set(['transport'])],
 ['Circuit creation',new Set(['circuits'])],
 ['Queues & Intro',new Set(['queues','introduction'])],
 ['Directory (BEGIN_DIR)',new Set(['directory'])],
 ['Exit streams',new Set(['exit'])],
];
function heroThresholdsSection(h,is24h,isNonMidnightDay){
 const dt=isPlainRecord(D.dos_thresholds)&&Array.isArray(D.dos_thresholds.settings)&&D.dos_thresholds.settings.length?D.dos_thresholds:null;
 const fams=h?.familyThresholds||[];
 const hasComposite=!!(h?.rateRank&&(h.rateRank.p90Fleet!=null||h.rateRank.p99Fleet!=null||h.rateRank.n>0));
 const hasOptionB=hasComposite||fams.length>0;
 if(!dt&&!hasOptionB)return '';
 const unitChar=is24h?'h':'d';
 const fmtVal=(v,unit)=>v==='unset'?'Tor default (unset)':v==='auto'?'auto':(/^\d+$/.test(v)&&unit&&!/^(bool|boolean|flag|mode|enum|none|-|—)$/i.test(unit)?`${v} ${unit}`:v);
 let optionAHtml='';
 if(dt){
  const groupedHtml=DOS_THRESHOLD_GROUPS.map(([title,famSet])=>{
   const items=dt.settings.filter(item=>famSet.has(item.family_id));
   if(!items.length)return '';
   const rows=items.map(item=>{
    const vals=Array.isArray(item.values)?item.values:[],sup=Number.isInteger(item.suppressed_relays)&&item.suppressed_relays>0?item.suppressed_relays:0;
    const split=vals.length>1||sup>0;
    let valHtml;
    if(split){
     const parts=vals.map(b=>`${escapeHtml(fmtVal(b.value,item.unit))} <span class="threshold-source">(${escapeHtml(b.source)}: ${count(b.relays)})</span>`);
     if(sup)parts.push(`<span class="threshold-source">+${count(sup)} other</span>`);
     valHtml=parts.join(' · ');
    }else if(vals.length===1){
     const b=vals[0];
     valHtml=`${escapeHtml(fmtVal(b.value,item.unit))}`+(b.value!=='unset'||b.source!=='unset'?` <span class="threshold-source">(${escapeHtml(b.source)})</span>`:'');
    }else{
     valHtml='Unavailable';
    }
    return `<div class="hero-threshold-row"><span class="hero-threshold-key">${escapeHtml(item.option)}</span><span class="hero-threshold-val">${valHtml}</span></div>`;
   }).join('');
   return `<div class="hero-threshold-group"><div class="hero-threshold-group-title">${escapeHtml(title)}</div>${rows}</div>`;
  }).filter(Boolean).join('');
  optionAHtml=`<div class="hero-threshold-col"><div class="hero-threshold-head"><h3>Tor trigger thresholds</h3><span class="hero-threshold-sub">consensus / torrc · ${count(dt.reported_relays)} relays</span></div>${groupedHtml}</div>`;
 }
 let optionBHtml='';
 if(hasOptionB){
  const winSub=is24h?'1h window':isNonMidnightDay?'UTC day':'24h window';
  const fmtCut=(val,n,minN)=>isFiniteNumber(val)?`${number(val)} events/s`:`— (<${count(minN)}${unitChar})`;
  const bRows=[];
  if(hasComposite){
   bRows.push(`<div class="hero-threshold-row"><span class="hero-threshold-key">${heroHsdir?'Composite mitigated &amp; flood rate':'Composite mitigated rate'}</span><span class="hero-threshold-val"><span class="cutoff-elevated">≥90th: ${escapeHtml(fmtCut(h.rateRank.p90Fleet,h.rateRank.n,20))}</span> · <span class="cutoff-high">≥99th: ${escapeHtml(fmtCut(h.rateRank.p99Fleet,h.rateRank.n,100))}</span> <span class="threshold-n">(${count(h.rateRank.n)}${unitChar})</span></span></div>`);
  }
  for(const fam of fams){
   bRows.push(`<div class="hero-threshold-row"><span class="hero-threshold-key">${escapeHtml(fam.icon)} ${escapeHtml(fam.title)}</span><span class="hero-threshold-val"><span class="cutoff-elevated">≥90th: ${escapeHtml(fmtCut(fam.p90,fam.n,20))}</span> · <span class="cutoff-high">≥99th: ${escapeHtml(fmtCut(fam.p99,fam.n,100))}</span> <span class="threshold-n">(${count(fam.n)}${unitChar})</span></span></div>`);
  }
  optionBHtml=`<div class="hero-threshold-col"><div class="hero-threshold-head"><h3>Dashboard alert cutoffs</h3><span class="hero-threshold-sub">${winSub} · ≥90th / ≥99th pct in events/s</span></div><div class="hero-threshold-list">${bRows.join('')}</div></div>`;
 }
 return `<div class="hero-thresholds" aria-label="DoS thresholds and alert cutoffs"><div class="hero-thresholds-grid${optionAHtml&&optionBHtml?'':' single-col'}">${optionAHtml}${optionBHtml}</div></div>`;
}
function heroCard(){
 const isHourly=isHourlyRelease(),defaultLeft=isHourly?'24h':'7d';
 const leftRange=heroRange==='all'?defaultLeft:normalizeHeroRange(heroRange,isHourly),is24h=leftRange==='24h';
 const hLeft=heroPlotData(leftRange,heroHsdir),hAll=heroPlotData('all',heroHsdir);
 const h=is24h?hLeft:(hLeft||hAll),last=h?h.times.length-1:-1;
 const isNonMidnightDay=!is24h&&isHourly&&validTime(D.end)&&(h?h.end!==D.end:D.end%DAY!==0);
 const total=last>=0?h.totals[last]:null,latestPartial=last>=0&&h.partialRates[last];
 const ratePeak=h?.ratePeak??0,rateShare=peakShare(total,ratePeak);
 const open=last>=0?h.gauges.openCircuits.values[last]:null,wait=last>=0?h.gauges.directoryWaiting.values[last]:null;
 const openPeak=h?.gauges.openCircuits.rawPeak??0,waitPeak=h?.gauges.directoryWaiting.rawPeak??0;
 const openShare=peakShare(open,openPeak),waitShare=peakShare(wait,waitPeak);
 const hasIncomingHero=!!(h?.incomingConnections?.total?.idx||h?.openConnections?.idx||chart('incoming_connections')||chart('open_connections'));
 const incTotal=last>=0?h.incomingConnections?.total?.values?.[last]??null:null;
 const incAcc=last>=0?h.incomingConnections?.accepted?.values?.[last]??null:null;
 const incRej=last>=0?h.incomingConnections?.rejected?.values?.[last]??null:null;
 const incPeak=h?.incomingConnections?.total?.rawPeak??0,incShare=peakShare(incTotal,incPeak);
 const openConn=last>=0?h.openConnections?.values?.[last]??null:null;
 const openConnPeak=h?.openConnections?.rawPeak??0,openConnShare=peakShare(openConn,openConnPeak);
 const alerts=[];
 let anyAlertMeasured=false,hasHighAlert=false;
 if(last>=0)for(const f of h.families){
  const row=f.rows[last],st=row.state;
  if(st!=='missing')anyAlertMeasured=true;
  if(st==='high'||st==='elevated'){alerts.push({f,state:st,row});if(st==='high')hasHighAlert=true;}
 }
 const refP=is24h?'hour':'day';
 const isInactiveExitHero=f=>{
  if(f.id!=='exit'||D.flags?.Exit!==0)return false;
  const pc=chart(f.primary);
  return !!(pc?.series?.length&&pc.series.every(s=>s.reference?.[refP]?.flat_zero===true));
 };
 const activeFamilyCount=D.families.filter(f=>!isInactiveExitHero(f)).length;
 const alertExploreRange=periodMap.has(leftRange)?leftRange:(is24h&&periodMap.has('24h')?'24h':(periodMap.has('7d')?'7d':timeRanges.default_range));
 const alertClass=!anyAlertMeasured?'unknown':hasHighAlert?'high':alerts.length?'elevated':'neutral';
 const endWindow=h?formatIntervalWindow(h.end-h.window,h.end,h.window):validTime(D.end)?(is24h?formatIntervalWindow(Math.floor(D.end/HOUR)*HOUR-HOUR,Math.floor(D.end/HOUR)*HOUR,HOUR):date(Math.floor(D.end/DAY)*DAY-DAY)+' UTC'):'Unavailable';
 const alertWindowLine=isNonMidnightDay?`<div class="hero-kpi-meta">${escapeHtml(endWindow)} · completed UTC day</div>`:'';
 const alertMeta=alerts.length
  ?`${alertWindowLine}<ul class="hero-alert-list" aria-label="Alerted families">${alerts.map(({f,state,row})=>{
   const pct=isFiniteNumber(row.rank)?` · ${formatPercentileOrdinal(row.rank)} pct`:'';
   const chartPrefix=row.chartId&&f.primary&&row.chartId!==f.primary&&row.chartTitle&&row.chartTitle!==row.seriesName?`${row.chartTitle}: `:'';
   const extra=row.alertCount>1?` (+${count(row.alertCount-1)} more)`:'';
   const detail=row.chartId&&row.seriesKey
    ?` <a class="hero-alert-detail" href="${url('explorer',alertExploreRange,f.id,row.chartId,row.seriesKey)}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(row.chartId)}" data-series="${escapeHtml(row.seriesKey)}" data-range="${alertExploreRange}">${escapeHtml(chartPrefix)}${escapeHtml(row.seriesName||row.seriesKey)}${escapeHtml(pct)}${escapeHtml(extra)}</a>`:'';
   return `<li><a class="hero-alert-link ${state}" href="#family-${escapeHtml(encodeURIComponent(f.id))}" data-family-jump="${escapeHtml(f.id)}">${escapeHtml(f.icon)} ${escapeHtml(f.title)}</a>${detail}</li>`;
  }).join('')}</ul>`
  :`${alertWindowLine}<div class="hero-kpi-meta">${anyAlertMeasured?'No qualifying family alerts':'No measured family series'}</div>`;
 const leftOpts=(isHourly?HERO_HOURLY_LEFT_RANGES:HERO_DAILY_LEFT_RANGES).map(id=>[id,HERO_RANGE_META[id].label]);
 const desktopWindow=controlGroup('Window','Concurrent DoS left-panel window','data-hero-range',leftRange,leftOpts,'hero-window-desktop');
 const mobileOpts=[...leftOpts,['all',HERO_RANGE_META.all.label]];
 const mobileWindow=controlGroup('Window','Concurrent DoS time range','data-hero-range',heroRange,mobileOpts,'hero-window-mobile');
 const controls=`<div class="hero-controls">${desktopWindow}${mobileWindow}${controlGroup('Flow scale','Concurrent DoS flow scale','data-hero-scale',heroScale,[['linear','Linear'],['symlog','Symlog']])}<button class="hero-toggle ${heroHsdir?'active':''}" data-hero-hsdir aria-pressed="${heroHsdir}">HSDir GET/POST ${heroHsdir?'included':'excluded'}</button></div>`;
 const recScope=is24h?'1h record':'24h record';
 const dayScopeMeta=isNonMidnightDay?`${endWindow} · `:'';
 const heroPctBadge=(hasVal,rk,{isGauge=false,unitWord,extraScope='',scopeText=null})=>{
  if(!hasVal)return '';
  if(!rk||!Number.isInteger(rk.n)||rk.n<=0||(!rk.flatZero&&!isFiniteNumber(rk.rank))){
   return '<div class="hero-kpi-pct"><span class="percentile unknown" title="No matching reference intervals available">Percentile unavailable</span></div>';
  }
  if(rk.flatZero){
   return `<div class="hero-kpi-pct"><span class="percentile flat" title="${escapeHtml(`All ${count(rk.n)} reference ${unitWord} are zero${extraScope}.`)}">Percentile —</span></div>`;
  }
  const label=`${formatPercentileOrdinal(rk.rank)} percentile`;
  const cls=rk.n<20?'unknown':isGauge?'neutral':alertStateFor(rk.rank,rk.n,false);
  const scope=scopeText||(isGauge
   ?`Per-relay mean ranked vs ${count(rk.n)} reference ${unitWord} (resource gauge; does not trigger family alerts).`
   :`Per-relay sum ranked vs ${count(rk.n)} matching reference ${unitWord}${extraScope}.`);
  const shortTag=rk.n<20?` <span class="hero-kpi-pct-note">(${count(rk.n)}${is24h?'h':'d'})</span>`:'';
  return `<div class="hero-kpi-pct"><span class="percentile ${cls}" title="${escapeHtml(scope)}">${escapeHtml(label)}</span>${shortTag}</div>`;
 };
 const unitWord=is24h?'sampled hours':'days';
 const rateScopeExtra=h?.rateRank?` (${count(h.rateRank.activeSources)}/${count(h.rateRank.totalSources)} sources${latestPartial?'; partial coverage':''})`:'';
 const rateMeta=(rateShare?`<div class="hero-kpi-meta">${escapeHtml(`${rateShare} of ${axisNumber(ratePeak)} peak (${recScope})`)}</div>`:'')
  +heroPctBadge(total!=null,h?.rateRank,{isGauge:false,unitWord,extraScope:rateScopeExtra})
  +`<div class="hero-kpi-meta">${escapeHtml(`${endWindow}${isNonMidnightDay?' (completed UTC day)':''} · ${heroHsdir?'incl. HSDir GET/POST':'excl. HSDir GET/POST'}${latestPartial?' · partial source coverage':''}`)}</div>`;
 const openMeta=`<div class="hero-kpi-meta">${escapeHtml(openShare?`${dayScopeMeta}${openShare} of ${axisNumber(openPeak)} peak (${recScope})`:'Normalized to fleet record')}</div>`
  +heroPctBadge(open!=null,h?.gauges?.openCircuits?.latestRank,{isGauge:true,unitWord});
 const waitMeta=`<div class="hero-kpi-meta">${escapeHtml(waitShare?`${dayScopeMeta}${waitShare} of ${axisNumber(waitPeak)} peak (${recScope})`:'command_wait state')}</div>`
  +heroPctBadge(wait!=null,h?.gauges?.directoryWaiting?.latestRank,{isGauge:true,unitWord});
 const scopeTag=is24h?'1h':isNonMidnightDay?'UTC day':'24h';
 const rateTile=[is24h?(heroHsdir?'Latest 1h mitigated &amp; flood rate':'Latest 1h mitigated rate'):isNonMidnightDay?(heroHsdir?'Latest UTC day mitigated &amp; flood rate':'Latest UTC day mitigated rate'):(heroHsdir?'Latest 24h mitigated &amp; flood rate':'Latest 24h mitigated rate'),total!=null?'neutral':'unknown',`${escapeHtml(total!=null?number(total):'Unavailable')} <small>events/s${latestPartial?' · partial':''}</small>`,rateMeta];
 const alertTile=[is24h?'Concurrent family alerts (1h)':isNonMidnightDay?'Concurrent family alerts (UTC day)':'Concurrent family alerts (24h)',alertClass,`${count(alerts.length)} of ${count(activeFamilyCount)} <small>${activeFamilyCount<D.families.length?'active families ≥90th pct':'families ≥90th pct'}</small>`,alertMeta];
 const openTile=[is24h?'Open circuits (1h mean)':isNonMidnightDay?'Open circuits (UTC day mean)':'Open circuits (24h mean)',open!=null?'neutral':'unknown',`${escapeHtml(open!=null?number(open):'Unavailable')} <small>circuits</small>`,openMeta];
 const waitTile=[is24h?'Stalled BEGIN_DIR (1h mean)':isNonMidnightDay?'Stalled BEGIN_DIR (UTC day mean)':'Stalled BEGIN_DIR (24h mean)',wait!=null?'neutral':'unknown',`${escapeHtml(wait!=null?number(wait):'Unavailable')} <small>connections</small>`,waitMeta];
 let kpiItems;
 if(hasIncomingHero){
  const incRejPct=incRej!=null?(incTotal>0?peakShare(incRej,incTotal):incRej===0?'0%':null):null;
  const incBreakdown=(incAcc!=null||incRej!=null)
   ?`<div class="hero-kpi-breakdown">Accepted: ${escapeHtml(incAcc!=null?number(incAcc)+'/s':'Unavailable')} · Rejected: ${escapeHtml(incRej!=null?number(incRej)+'/s':'Unavailable')}${incRejPct?` (${escapeHtml(incRejPct)} rejected)`:''}</div>`
   :'';
  const incMeta=incBreakdown
   +(incShare?`<div class="hero-kpi-meta">${escapeHtml(`${dayScopeMeta}${incShare} of ${axisNumber(incPeak)} peak (${recScope})`)}</div>`:'')
   +heroPctBadge(incTotal!=null,h?.incomingConnections?.total?.latestRank,{isGauge:true,unitWord,scopeText:`Per-relay rate ranked vs ${count(h?.incomingConnections?.total?.latestRank?.n??0)} reference ${unitWord} (traffic context; does not trigger family alerts).`});
  const incTile=[`Incoming OR connections (${scopeTag})`,incTotal!=null?'neutral':'unknown',`${escapeHtml(incTotal!=null?number(incTotal):'Unavailable')} <small>attempts/s</small>`,incMeta];
  const openConnMeta=`<div class="hero-kpi-meta">${escapeHtml(openConnShare?`${dayScopeMeta}${openConnShare} of ${axisNumber(openConnPeak)} peak (${recScope})`:'OR received opened state')}</div>`
   +heroPctBadge(openConn!=null,h?.openConnections?.latestRank,{isGauge:true,unitWord});
  const openConnTile=[`Open incoming OR (${scopeTag} mean)`,openConn!=null?'neutral':'unknown',`${escapeHtml(openConn!=null?number(openConn):'Unavailable')} <small>connections</small>`,openConnMeta];
  kpiItems=[rateTile,incTile,alertTile,openTile,openConnTile,waitTile];
 }else{
  kpiItems=[rateTile,alertTile,openTile,waitTile];
 }
 const kpis=`<div class="hero-kpi-grid${hasIncomingHero?' hero-kpi-6':''}">${kpiItems.map(([lbl,cls,val,meta])=>`<div class="hero-kpi"><div class="hero-kpi-label">${lbl}</div><div class="hero-kpi-value ${cls}">${val}</div>${meta}</div>`).join('')}</div>`;
 const thresholdsHtml=heroThresholdsSection(h,is24h,isNonMidnightDay);
 const hourSpan=hp=>{if(!hp?.times?.length)return '';const s=hp.start,e=hp.end,sd=date(s),ed=date(e);return sd===ed?`${sd} ${time(s)}–${time(e)}`:`${sd} ${time(s)}–${ed} ${time(e)}`;};
 const daySpan=hp=>hp?.times?.length?(hp.times.length===1?date(hp.start):`${date(hp.start)}–${date(hp.end-DAY)}`):'';
 const heroPanel=(r,title,hp,sub,unitLbl)=>{
  const meta=hp?.times?.length?`${count(hp.times.length)} × ${unitLbl}`:'No retained samples';
  const panelCls='hero-panel'+(heroRange===r?' active':'')+(r===leftRange?' desktop-left':'')+(r==='all'?' desktop-right':'');
  return `<div class="${panelCls}" data-hero-panel="${r}"><div class="hero-panel-head"><span class="hero-panel-title"><b>${title}</b>${sub?` <small>(${escapeHtml(sub)})</small>`:''}</span><span class="hero-panel-meta">${meta}</span></div><div class="plot-wrap hero-plot-wrap" data-hero-plot="concurrent_dos_${r}" data-hero-plot-range="${r}" tabindex="0" role="img" aria-label="${escapeHtml(heroPlotLabel(r,!!hp,heroScale))}"></div></div>`;
 };
 const leftMeta=HERO_RANGE_META[leftRange];
 const leftSpan=is24h?hourSpan(hLeft):daySpan(hLeft);
 const leftSub=is24h?(leftSpan?`${leftSpan} · 1h UTC`:'1h UTC'):(leftSpan?`${leftSpan} · 24h UTC`:'24h UTC');
 const sAll=daySpan(hAll),allDays=hAll?.times?.length?`${count(hAll.times.length)}d${sAll?' · '+sAll:''}`:sAll;
 const panels=`<div class="hero-panels">${heroPanel(leftRange,leftMeta.title,hLeft,leftSub,is24h?'1h':'24h')}${heroPanel('all',HERO_RANGE_META.all.title,hAll,allDays,'24h')}</div>`;
 const hasPartial=!!(hLeft?.hasPartialRate||hAll?.hasPartialRate),hasMissing=!!(hLeft?.hasAnyMissing||hAll?.hasAnyMissing);
 const vecLegend=((h||hAll)?.vectors||[]).map(v=>`<span><i style="background:${seriesColor(v.color)}"></i>${escapeHtml(v.label)}</span>`).join('');
 const laneKey=D.families.map(f=>`${escapeHtml(f.icon)} ${escapeHtml(f.title)}`).join(' · ');
 const laneSpan=`${escapeHtml(D.families[0]?.icon||'01')}→${escapeHtml(D.families.at(-1)?.icon||'06')}`;
 const cohortParts=[
  Number.isInteger(D.fleet)?`Fleet ${count(D.fleet)}`:null,
  Number.isInteger(D.custom)?`Custom ${count(D.custom)}`:null,
  Number.isInteger(D.defense)?`Dir-defense ${count(D.defense)}`:null,
 ].filter(Boolean);
 const cohortTag=cohortParts.length?`<span class="legend-cohort">Cohorts: ${escapeHtml(cohortParts.join(' · '))}</span>`:'';
 const gaugePeakNote=(leftG,allG,fallbackPeak)=>{
  const winP=leftG?.windowPeak??0,allP=allG?.rawPeak??0;
  if(winP>0&&allP>0&&winP!==allP)return `100% = ${axisNumber(winP)} (${leftRange}) · ${axisNumber(allP)} (all-time)`;
  if(winP>0&&leftRange!=='all')return `${axisNumber(winP)} (${leftRange} peak)`;
  if(allP>0)return `${axisNumber(allP)} peak`;
  if(fallbackPeak>0)return `${axisNumber(fallbackPeak)} peak`;
  return '100%';
 };
 const leftGauges=hLeft?.gauges;
 const openLegendPeak=gaugePeakNote(leftGauges?.openCircuits,hAll?.gauges?.openCircuits,openPeak);
 const waitLegendPeak=gaugePeakNote(leftGauges?.directoryWaiting,hAll?.gauges?.directoryWaiting,waitPeak);
 const legendHtml=`<div class="legend hero-legend">`
  +`<div class="legend-tier"><span class="legend-tier-label">Rate vectors</span>${vecLegend}${cohortTag}${hasPartial?'<span><i class="partial-key"></i>Partial rate coverage</span>':''}${hasMissing?'<span><i class="gap-key"></i>Unmeasured interval</span>':''}</div>`
  +`<div class="legend-tier"><span class="legend-tier-label">Alert strip (${laneSpan})</span><span><i class="alert-elevated-key"></i>≥90th pct family</span><span><i class="alert-high-key"></i>≥99th pct family</span><span class="legend-lanes">${laneKey}</span></div>`
  +`<div class="legend-tier"><span class="legend-tier-label">Resource gauges</span><span><i style="background:#f1d05d"></i>Open circuits (${escapeHtml(openLegendPeak)})</span><span><i class="gauge-dashed-key"></i>Stalled BEGIN_DIR (${escapeHtml(waitLegendPeak)})</span></div>`
  +`</div>`;
 const heroDesc=isHourly
  ?'Synchronized hourly and daily UTC views across non-overlapping mitigated &amp; flood rates (/s), a 6-family ≥90th/≥99th percentile alert strip, and normalized open-circuit &amp; stalled <code>BEGIN_DIR</code> resource state.'
  :'Synchronized daily UTC view across non-overlapping mitigated &amp; flood rates (/s), a 6-family ≥90th/≥99th percentile alert strip, and normalized open-circuit &amp; stalled <code>BEGIN_DIR</code> resource state.';
 const heroShortCaption=isHourly
  ?'Non-overlapping 1h UTC intervals (Last 24 hours) and 24h UTC intervals (Last 7 days, Last 1 month, Last 6 months, Last 12 months, All time) across non-overlapping mitigated/flood rates, family alerts, and normalized resource gauges.'
  :'Non-overlapping 24h UTC intervals (00:00 UTC) across Last 7 days, Last 1 month, Last 6 months, Last 12 months, and All time for non-overlapping mitigated/flood rates, family alerts, and normalized resource gauges.';
 const heroFootnote=isHourly
  ?'Non-overlapping 1-hour UTC intervals on Last 24 hours and 24-hour UTC intervals (00:00 UTC) on daily windows (Last 7 days, Last 1 month, Last 6 months, Last 12 months, and All time); desktop displays the selected Window alongside All time, while smaller screens switch among all six views with Window. Counter sums exclude overlapping sub-series (<code>ntor_v3</code> within <code>ntor</code>; exitward queue hits within <code>circuit_killed_max_cell</code>; HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>). Alert lanes mark families with a qualifying DoS counter at rank ≥90 with at least 20 reference windows (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference windows (high), using sampled 1-hour references on Last 24 hours and daily references on daily windows. Bottom resource gauges normalize to each series’ selected window peak on Last 24 hours through Last 12 months and retained fleet record on All time; KPI headlines pair latest absolute values with % of retained fleet peak and per-relay reference percentiles (ranked over matching active sources for the composite mitigated rate).'
  :'Non-overlapping 24-hour UTC intervals (00:00 UTC); desktop displays the selected Window (Last 7 days, Last 1 month, Last 6 months, or Last 12 months) alongside All time, while smaller screens switch among all five views with Window. Counter sums exclude overlapping sub-series (<code>ntor_v3</code> within <code>ntor</code>; exitward queue hits within <code>circuit_killed_max_cell</code>; HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>). Alert lanes mark families with a qualifying DoS counter at rank ≥90 with at least 20 reference days (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference days (high). Bottom resource gauges normalize to each series’ selected window peak on Last 7 days through Last 12 months and retained fleet record on All time; KPI headlines pair latest absolute values with % of retained fleet peak and per-relay reference percentiles (ranked over matching active sources for the composite mitigated rate).';
 return `<section id="concurrent-dos-overview" class="chart-card hero-card" data-hero-card-range="${escapeHtml(heroRange)}"><div class="card-heading"><div><div class="family-label">All six families · Concurrent DoS overview</div><h2>Concurrent DoS activity</h2></div>${controls}</div><p class="definition">${heroDesc}</p>${kpis}${thresholdsHtml}<figure>${panels}${legendHtml}<figcaption><b>Summary.</b> ${heroShortCaption}</figcaption><details class="method-details"><summary>Methodology &amp; overlap rules</summary><p>${heroFootnote}</p></details></figure></section>`;
}
function familyActiveAlerts(f,primaryChart,primarySeries){
 const hourly=isHourlyRelease(),sevOrder={neutral:0,elevated:1,high:2},alerts=[];
 const evalWin=(ch,item,period)=>{
  const cur=item.current?.[period],ref=item.reference?.[period],val=currentValue(item,ch,period);
  if(cur?.valid!==true||val===null){
   if(cur?.valid===false&&isFiniteNumber(cur.coverage)&&cur.coverage<0.9){
    return {valid:false,state:'unknown',rank:null,shortText:`${coverageFormat.format(cleanZero(cur.coverage))} coverage (<90%)`,title:coverageUnavailableReason(cur)};
   }
   return {valid:false,state:'unknown',rank:null,shortText:'unavailable',title:cur?.valid===false?coverageUnavailableReason(cur):''};
  }
  const st=percentile(ref),state=alertStateFor(st.rank,ref?.n??0,ref?.flat_zero);
  const pctText=isFiniteNumber(st.rank)?`${formatPercentileOrdinal(st.rank)} pct`:(ref?.flat_zero?'flat zero':'');
  return {valid:true,state,rank:st.rank,valText:`${scaledText(ch,val)} ${headlineUnit(ch,item)}`,pctText};
 };
 for(const cid of f.children){
  const ch=chart(cid);if(!ch)continue;
  for(const item of ch.series){
   if(!isAlertEligibleSeries(ch,item))continue;
   if(cid===primaryChart.id&&item.key===primarySeries.key)continue;
   const hr=hourly?evalWin(ch,item,'hour'):null,day=evalWin(ch,item,'day');
   const hrHot=hr&&(hr.state==='high'||hr.state==='elevated'),dayHot=day&&(day.state==='high'||day.state==='elevated');
   if(!hrHot&&!dayHot)continue;
   const maxSev=Math.max(sevOrder[hr?.state]??0,sevOrder[day.state]??0);
   const maxRank=Math.max(isFiniteNumber(hr?.rank)?hr.rank:-1,isFiniteNumber(day.rank)?day.rank:-1);
   alerts.push({ch,item,hr,day,hrHot,dayHot,maxSev,maxRank});
  }
 }
 alerts.sort((a,b)=>(b.maxSev-a.maxSev)||(b.maxRank-a.maxRank));
 return alerts;
}
function familyAlertCallout(f,c,s,r){
 const alerts=familyActiveAlerts(f,c,s);
 if(!alerts.length)return '';
 const hasHigh=alerts.some(a=>a.maxSev>=2);
 const allChildCharts=alerts.every(a=>a.ch.id!==c.id);
 const title=allChildCharts?(alerts.length>1?'Active alerts in child charts':'Active alert in child chart'):(alerts.length>1?'Active alerts in other series':'Active alert in other series');
 const dayLbl=isHourlyRelease()&&validTime(D.end)&&D.end%DAY!==0?'Rolling 24h':'24h';
 const fallbackDaily=timeRanges.card_slots.fallback_dynamic||timeRanges.periods.find(p=>p.window.unit!=='hours')?.id||r;
 const items=alerts.map(({ch,item,hr,day,hrHot})=>{
  const label=ch.id===c.id?item.name:(ch.title===item.name?ch.title:`${ch.title}: ${item.name}`);
  const linkRange=hrHot&&periodMap.has('24h')?'24h':(isHourlyRelease()&&isHourRange(r)?fallbackDaily:r);
  const href=url('explorer',linkRange,f.id,ch.id,item.key);
  const chip=(lbl,win,extraTitle='')=>win.valid
   ?`<span class="card-alert-chip chip-${win.state}">${lbl}: ${escapeHtml(win.valText)}${win.pctText?` (${escapeHtml(win.pctText)})`:''}</span>`
   :`<span class="card-alert-chip chip-unknown"${win.title||extraTitle?` title="${escapeHtml(win.title||extraTitle)}"`:''}>${lbl}: ${escapeHtml(win.shortText)}</span>`;
  const chips=[hr?chip('1h',hr):'',chip(dayLbl,day)].filter(Boolean).join('');
  return `<li class="card-alert-item"><a class="card-alert-link" href="${href}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(ch.id)}" data-series="${escapeHtml(item.key)}" data-range="${escapeHtml(linkRange)}">${escapeHtml(label)} →</a><div class="card-alert-chips">${chips}</div></li>`;
 }).join('');
 return `<div class="card-alert-callout ${hasHigh?'callout-high':'callout-elevated'}" role="region" aria-label="${escapeHtml(title)}"><div class="card-alert-title">${escapeHtml(title)}</div><ul class="card-alert-list">${items}</ul></div>`;
}
function childStrip(f,r){
 if(f.children.length<=1)return '';
 const hourRef=isHourRange(r)&&isHourlyRelease(),refPeriod=hourRef?'hour':'day';
 const sevOrder={unknown:0,neutral:1,elevated:2,high:3};
 const pills=f.children.map(cid=>{
  const ch=chart(cid);if(!ch)return '';
  let bestSev=-1,bestRank=-Infinity,bestState='unknown',bestSeries=null;
  const gaugeChart=isGaugeKind(ch),isContext=HERO_CONTEXT_CHARTS.has(cid),neutralOnly=gaugeChart||isContext;
  for(const s of ch.series){
   if(!neutralOnly&&!isAlertEligibleSeries(ch,s))continue;
   const ref=s.reference?.[refPeriod];
   if(s.current?.[refPeriod]?.valid!==true||!ref||!Number.isInteger(ref.n)||ref.n<=0)continue;
   const flatZero=!!ref.flat_zero,rank=flatZero?0:ref.current_percentile;
   if(!isContext&&!flatZero&&!isFiniteNumber(rank))continue;
   const state=neutralOnly?'neutral':alertStateFor(rank,ref.n,flatZero);
   const sev=sevOrder[state],candRank=isFiniteNumber(rank)?rank:0;
   if(sev>bestSev||(sev===bestSev&&candRank>bestRank)){bestSev=sev;bestRank=candRank;bestState=state;bestSeries=s;}
  }
  const alerting=bestState==='elevated'||bestState==='high';
  const detail=alerting&&bestSeries?` · ${bestSeries.name} (${formatPercentileOrdinal(bestRank)} pct)`:'';
  const href=url('explorer',r,f.id,cid,alerting&&bestSeries?bestSeries.key:null);
  const seriesAttr=` data-series="${escapeHtml(alerting&&bestSeries?bestSeries.key:'')}"`;
  return `<a class="child-pill ${bestState}" href="${href}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(cid)}" data-range="${escapeHtml(r)}"${seriesAttr}>${escapeHtml(ch.title)}${escapeHtml(detail)}</a>`;
 }).filter(Boolean).join('');
 const statusScope=hourRef?'1h status':isHourlyRelease()?'rolling 24h status':'24h status';
 return `<div class="child-strip" aria-label="${escapeHtml(f.title)} child charts"><span class="child-strip-label">Family charts (${count(f.children.length)} · ${statusScope})</span><div class="child-pill-list">${pills}</div></div>`;
}
function incomingContextCard(f,r){
 const inc=chart('incoming_connections'),open=chart('open_connections');
 if(!inc&&!open)return '';
 const hourly=isHourlyRelease(),cols=hourly?[['hour','Last hour'],['day','24h']]:[['day','24h']];
 const ser=k=>inc?.series?.find(s=>s.key===k);
 const openSer=open?.series?.find(s=>s.key==='open_connections')||open?.series?.[0];
 const fmtVal=(ch,s,p,unit)=>{
  if(!ch||!s)return 'Unavailable';
  const v=currentValue(s,ch,p);
  return v!==null?`${scaledText(ch,v)} ${unit}`:'Unavailable';
 };
 const fmtRej=p=>{
  if(!inc||!ser('rejected'))return 'Unavailable';
  const rv=currentValue(ser('rejected'),inc,p);
  if(rv===null)return 'Unavailable';
  const tv=ser('total')?currentValue(ser('total'),inc,p):null;
  const share=tv!=null?(tv>0?peakShare(rv,tv):rv===0?'0%':null):null;
  return `${scaledText(inc,rv)} attempts/s${share?` · ${share} of total`:''}`;
 };
 const rows=[];
 if(inc){
  rows.push(['Total attempts',cols.map(([p])=>fmtVal(inc,ser('total'),p,'attempts/s'))]);
  rows.push(['Accepted',cols.map(([p])=>fmtVal(inc,ser('accepted'),p,'attempts/s'))]);
  rows.push(['Rejected',cols.map(([p])=>fmtRej(p))]);
 }
 if(open)rows.push(['Open incoming OR',cols.map(([p])=>fmtVal(open,openSer,p,'connections'))]);
 const targetId=inc?inc.id:open.id,linkLabel=inc?'Explore incoming OR connections →':'Explore open incoming OR →';
 return `<div class="incoming-context-card"><div class="incoming-context-head"><span class="incoming-context-title">Incoming OR connection traffic (context)</span><a href="${url('explorer',r,f.id,targetId)}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(targetId)}" data-range="${escapeHtml(r)}">${linkLabel}</a></div><table class="incoming-context-table"><thead><tr><th>Metric</th>${cols.map(([,lbl])=>`<th>${escapeHtml(lbl)}</th>`).join('')}</tr></thead><tbody>${rows.map(([lbl,vals])=>`<tr><th>${escapeHtml(lbl)}</th>${vals.map(v=>`<td>${escapeHtml(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function card(c,f,r){
 const s=primary(c),relaySrc=s.current?.hour&&'relays' in s.current.hour?s.current.hour:s.current?.day,relayCount=relaySrc&&'relays' in relaySrc?relaySrc.relays:c.relays;
 const refKind=isHourRange(r)&&isHourlyRelease()?'hour':'day';
 const zeroExitNote=f.id==='exit'&&D.flags?.Exit===0&&c.series.every(item=>item.reference?.[refKind]?.flat_zero===true)
  ?'<p class="card-callout">This fleet runs <b>0 Exit relays</b>; exit stream rejections remain zero across retained history.</p>':'';
 const alertCallout=familyAlertCallout(f,c,s,r);
 const incomingCtx=f.id==='transport'?incomingContextCard(f,r):'';
 const familyTotal=String(D.families.length).padStart(2,'0');
 const childCountLabel=f.children.length>1?` (${count(f.children.length)} charts)`:'';
 return `<article id="family-${escapeHtml(f.id)}" class="chart-card" data-family-range="${escapeHtml(r)}" tabindex="-1"><div class="card-heading"><div><div class="family-label">Family ${escapeHtml(f.icon)} of ${familyTotal}</div><h2>${escapeHtml(f.title)}</h2></div></div><div class="card-summary"><div class="card-context"><p class="definition">${escapeHtml(c.definition)}</p><div class="metric-subtitle">${metricSubtitle(c,r)}</div>${alertCallout}${zeroExitNote}${incomingCtx}</div><div class="reading-block">${reading(c,s)}</div></div>${childStrip(f,r)}${figure(c,r)}<div class="card-actions"><a href="${url('explorer',r,f.id,c.id)}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(c.id)}" data-range="${escapeHtml(r)}">Explore ${escapeHtml(f.title)}${childCountLabel} →</a><span>${count(relayCount)} reporting relays</span></div></article>`;
}
function chartControls(){return `<div class="overview-controls">${controlGroup('Time range','Chart time range','data-all-range',range,timeRanges.periods.map(p=>[p.id,p.label]))}${view==='overview'?controlGroup('Layout','Chart layout','data-layout',layout,[['grid','Grid'],['full','Full width']]):''}</div>`;}
function overview(){return `<h2 class="sr-only" data-view-heading tabindex="-1">Overview</h2>${fleetOpsStrip()}${heroCard()}<div class="overview-toolbar"><div><h2>Six chart families</h2><p>A percentile shows where a value sits among reference windows. The ruler runs from 0 to 100. <a href="${url('methods')}#reference" data-methods>How it is calculated</a></p></div>${chartControls()}</div><div class="chart-stack chart-${layout}">${D.families.map(f=>card(chart(f.primary),f,range)).join('')}</div>`;}
function explorer(){
 const c=chart(chartId),f=family(c.family_id),s=primary(c),activePeriod=periodFor(range);
 const csvPath=`exports/${encodeURIComponent(c.id)}-${encodeURIComponent(activePeriod.export_suffix)}.csv`;
 const allTimePeriod=timeRanges.periods.find(p=>p.window.unit==='all_time');
 const resolvedCsvPath=release&&!D.time_ranges&&!release.downloads.has(csvPath)&&allTimePeriod
  ?`exports/${encodeURIComponent(c.id)}-${encodeURIComponent(allTimePeriod.export_suffix)}.csv`
  :csvPath;
 const csvLabel=resolvedCsvPath!==csvPath?'CSV · all-time, source units':'CSV · all series, source units';
 const hourView=isHourRange(range),head24=hourView?headlineQuery(c):null;
 const dailyPeriods=timeRanges.periods.filter(p=>p.window.unit!=='hours'),hasHour=c.series.some(v=>v.current?.hour!==undefined);
 const queryCaption=hourView
  ?`${isGaugeKind(c)?'Per-source sample means are calculated before aggregation.':'Per-source counter rates are calculated before aggregation.'} A source series needs at least 24 samples per 1-hour UTC interval.`
  :`${isGaugeKind(c)?'Per-source sample means are calculated before aggregation.':'Per-source counter rates are calculated before aggregation.'} A source series needs at least 360 samples per 24-hour UTC interval.`;
 return `<div class="explorer-heading"><div><h2 data-view-heading tabindex="-1">Evidence explorer</h2><p><a href="${url('overview')}" data-view="overview">Six chart families</a> / ${escapeHtml(f.title)} / ${escapeHtml(c.title)}</p></div>${chartControls()}</div><div class="explorer-layout"><aside class="family-tree" aria-label="Chart families">${D.families.map(group=>`<section><h3><button data-family="${escapeHtml(group.id)}" class="${group.id===f.id?'active':''}">${escapeHtml(group.icon)} ${escapeHtml(group.title)}</button></h3>${group.children.map(id=>`<a href="${url('explorer',range,group.id,id)}" data-child="${escapeHtml(id)}" class="${id===c.id?'selected':''}">${escapeHtml(chart(id).title)}</a>`).join('')}</section>`).join('')}</aside><article class="explorer-detail"><div class="family-label">${escapeHtml(f.icon)} / ${escapeHtml(f.title)}</div><h2>${escapeHtml(c.title)}</h2><div class="metric-subtitle">${metricSubtitle(c,range)}</div><p class="definition">${escapeHtml(c.definition)}</p>${reading(c,s)}${bandControl(c)}${figure(c,range)}<div class="download-row">${downloadLink(resolvedCsvPath,csvLabel,'explorer-csv')}${downloadLink('data.json','JSON · values & methods','explorer-json')}<a href="${url('explorer',range,f.id,c.id)}" data-focus-key="explorer-permalink">Permalink</a></div><section class="query-section"><div class="query-header"><h3>${hourView?'PromQL · hourly plot':'PromQL · daily plot'}</h3><button data-copy="${escapeHtml(c.id)}" data-query-kind="plot">Copy</button></div><pre>${escapeHtml(plotQuery(c,range))}</pre><div class="query-header"><h3>Coverage query</h3><button data-copy="${escapeHtml(c.id)}" data-query-kind="coverage">Copy</button></div><pre>${escapeHtml(coverageQuery(c,range))}</pre>${head24?`<div class="query-header"><h3>24-hour headline query</h3><button data-copy="${escapeHtml(c.id)}" data-query-kind="headline">Copy</button></div><pre>${escapeHtml(head24)}</pre>`:''}<p>${queryCaption}</p></section><h3>Values by series</h3><div class="table-scroll"><table data-values-table><thead><tr><th>Series</th><th>Unit</th>${hasHour?'<th>Last complete hour</th>':''}<th>24-hour average</th>${dailyPeriods.map(p=>`<th>${escapeHtml(p.average_label)}</th>`).join('')}</tr></thead><tbody>${c.series.map(v=>`<tr><td>${escapeHtml(v.name)}</td><td>${escapeHtml(headlineUnit(c,v))}</td>${hasHour?`<td>${escapeHtml(currentText(v,c,'hour'))}</td>`:''}<td>${escapeHtml(currentText(v,c,'day'))}</td>${dailyPeriods.map(p=>{const sm=v.historical_averages?.[p.summary_key],txt=historicalAverageText(v,p.summary_key,c),partialDays=sm?.status==='ready'&&Number.isInteger(sm.days)&&Number.isInteger(sm.expected_days)&&sm.expected_days>0&&sm.days<sm.expected_days?` <span class="table-days" title="${escapeHtml(`${count(sm.days)} of ${count(sm.expected_days)} days measured`)}">(${count(sm.days)}d)</span>`:'';return `<td>${escapeHtml(txt)}${partialDays}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div></article></div>`;
}
function fixes(){
 const builds=summarizeFleetBuilds();
 const unverified=Number.isInteger(builds.unverifiedTotal)&&builds.unverifiedTotal>0?builds.unverifiedTotal:0;
 const scopeBits=[];
 if(Number.isInteger(builds.nativeTotal))scopeBits.push(`${count(builds.nativeTotal)} relays total`);
 if(builds.patchedRelays)scopeBits.push(`Patched ${count(builds.patchedRelays)}`);
 if(builds.stockRelays)scopeBits.push(`Stock ${count(builds.stockRelays)}`);
 if(unverified)scopeBits.push(`${count(unverified)} unrecorded`);
 const badgeFor=b=>{
  const title=b.verification?` title="${escapeHtml(b.verification)}"`:'';
  return b.patched
   ?`<span class="status-badge status-released"${title}>Patched</span>`
   :`<span class="status-badge status-neutral"${title}>Stock</span>`;
 };
 const unverifiedRow=unverified?`<tr><td>Version not recorded</td><td><span class="status-badge status-muted">Unrecorded</span></td><td>${count(unverified)}</td></tr>`:'';
 const buildMix=(builds.list.length||unverified)
  ?`<h3>Current fleet build mix</h3><p>${validTime(builds.asOf)?`Snapshot as of <b>${date(builds.asOf)} ${time(builds.asOf)} UTC</b>`:'Current runtime inventory'}${scopeBits.length?` · ${scopeBits.join(' · ')}`:''}.</p><div class="table-scroll"><table><thead><tr><th>Tor version</th><th>Build</th><th>Relays</th></tr></thead><tbody>${builds.list.map(b=>`<tr><td>${b.sourceUrl?`<a href="${escapeHtml(b.sourceUrl)}" rel="noopener noreferrer">${escapeHtml(b.version)}</a>`:escapeHtml(b.version)}</td><td>${badgeFor(b)}</td><td>${count(b.relays)}</td></tr>`).join('')}${unverifiedRow}</tbody></table></div>`
  :'';
 const issueRows=TOR_FIXES.map(item=>`<tr><td>${escapeHtml(item.issue)}</td><td><span class="status-badge status-${item.statusClass}">${escapeHtml(item.status)}</span></td><td>${escapeHtml(item.release)}</td><td>${item.links.map(([label,href])=>`<a href="${escapeHtml(href)}" rel="noopener noreferrer">${escapeHtml(label)}</a>`).join(' · ')}</td></tr>`).join('');
 return `<section class="text-page"><h2 data-view-heading tabindex="-1">Tor fixes &amp; open issues</h2>${buildMix}<h3>Known issues &amp; upstream fixes</h3><p>Read-only status snapshot · 22 Nov 2026 UTC.</p><div class="table-scroll"><table><thead><tr><th>Issue or change</th><th>Status</th><th>Tor release</th><th>Source</th></tr></thead><tbody>${issueRows}</tbody></table></div></section>`;
}
function methods(){
 const hasHour=isHourlyRelease()||timeRanges.periods.some(p=>p.window.unit==='hours');
 const dailyPeriods=timeRanges.periods.filter(p=>p.window.unit!=='hours');
 const labels=timeRanges.periods.map(p=>escapeHtml(p.label));
 const rangeList=labels.length>1?`${labels.slice(0,-1).join(', ')}, or ${labels.at(-1)}`:labels[0]||'';
 const fixedIds=hasHour?timeRanges.card_slots.fixed.filter(id=>periodFor(id).window.unit!=='hours'):timeRanges.card_slots.fixed;
 const fixedLabels=fixedIds.map(id=>escapeHtml(periodFor(id).average_label));
 const fixedRangeLabels=timeRanges.card_slots.fixed.map(id=>escapeHtml(periodFor(id).label)).join(' or ');
 const fallbackLabel=escapeHtml(periodFor(timeRanges.card_slots.fallback_dynamic).average_label);
 const dayEndStr=date(Math.floor(D.end/DAY)*DAY);
 const periodRows=dailyPeriods.map(p=>{
  const pct=coverageFormat.format(cleanZero(p.minimum_coverage));
  const req=p.minimum_coverage>0?`at least ${pct} of days required`:'uses all available qualifying days in the window';
  const desc=p.window.unit==='all_time'
   ?`Mean of every retained qualifying daily interval on a linear scale ending at ${dayEndStr} 00:00 UTC.`
   :p.window.unit==='days'
    ?`Mean of qualifying daily fleet values across the ${count(p.window.count)}-day window ending at ${dayEndStr} 00:00 UTC (${req}).`
    :`Mean of qualifying daily fleet values over ${count(p.window.count)} calendar ${p.window.count===1?'month':'months'} ending at ${dayEndStr} 00:00 UTC (${req}).`;
  return `<tr><th>${escapeHtml(p.average_label)}</th><td>${desc}</td></tr>`;
 }).join('');
 const laggedPeriods=dailyPeriods.filter(p=>p.window.comparison_lag_days>0).map(p=>escapeHtml(p.label));
 const lagText=laggedPeriods.length>2?`${laggedPeriods.slice(0,-1).join(', ')}, and ${laggedPeriods.at(-1)}`:laggedPeriods.join(' and ');
 const lagSentence=laggedPeriods.length
  ?`the prior window shifted back by 1 UTC day for ${lagText}, and each period’s own window through the previous UTC midnight otherwise`
  :'that panel’s period through the previous UTC midnight';
 const customThresholds=dailyPeriods.filter(p=>p.minimum_comparison_windows!==30)
  .map(p=>`${count(p.minimum_comparison_windows)} for ${escapeHtml(p.label)}`);
 const summarySentence=hasHour
  ?`The Overview opens with a <b>Concurrent DoS activity</b> card showing <b>Last 24 hours</b>, <b>Last 7 days</b>, <b>Last 1 month</b>, <b>Last 6 months</b>, or <b>Last 12 months</b> (selectable via <b>Window</b>) alongside <b>All time</b> on desktop, or all six windows via <b>Window</b> on narrower screens, followed by the six chart families controlled by the global time range (${rangeList}) and Grid/Full width layout. The 2×2 summary panel pairs the <b>Last complete hour</b> and <b>24-hour average</b> with the active range’s average (defaulting to <b>${fallbackLabel}</b> when viewing ${fixedRangeLabels}) alongside <b>${fixedLabels.join('</b> and <b>')}</b>. Hover or use arrow keys on any chart to inspect individual UTC intervals and reporting-relay counts.`
  :`The Overview opens with a <b>Concurrent DoS activity</b> card showing <b>Last 7 days</b>, <b>Last 1 month</b>, <b>Last 6 months</b>, or <b>Last 12 months</b> (selectable via <b>Window</b>) alongside <b>All time</b> on desktop, or all five windows via <b>Window</b> on narrower screens, followed by the six chart families controlled by the global time range (${rangeList}) and Grid/Full width layout. The 2×2 summary panel pairs the <b>24-hour average</b> with the active range’s average (defaulting to <b>${fallbackLabel}</b> when viewing ${fixedRangeLabels}) alongside <b>${fixedLabels.join('</b> and <b>')}</b>. Hover or use arrow keys on any chart to inspect individual UTC days and reporting-relay counts.`;
 const intervalLead=hasHour
  ?'The measurements come from Tor MetricsPort across non-overlapping 1-hour UTC intervals (<code>3,600s</code>) for the 24-hour snapshot and non-overlapping 24-hour UTC intervals (00:00:00 to 00:00:00 UTC) for daily views.'
  :'The measurements come from Tor MetricsPort across non-overlapping 24-hour UTC intervals (00:00:00 to 00:00:00 UTC).';
 const heroMethodDesc=hasHour
  ?'The top Overview chart aligns three vertical tiers across non-overlapping <code>3,600s</code> UTC hours (for 24 hours) or <code>86,400s</code> UTC days (for 7 days, 1 month, 6 months, 12 months, and All time) on either a stacked linear or per-vector symlog rate scale: (1) additive source-unit rates excluding overlapping subsets (<code>ntor_v3</code> within <code>ntor</code>, exitward queue hits within <code>circuit_killed_max_cell</code>, and HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>, with optional <code>HSDir GET/POST</code> exclusion), (2) a six-family alert strip marking intervals where a qualifying DoS counter reaches rank ≥90 with at least 20 reference windows (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference windows (high), and (3) hourly or daily fleet means for open circuits and stalled <code>BEGIN_DIR</code> connections normalized to each series’ window peak on Last 24 hours through Last 12 months and retained fleet record on All time. KPI headlines pair latest absolute values with % of retained fleet peak and per-relay reference percentiles (ranked over matching active sources for the composite mitigated rate).'
  :'The top Overview chart aligns three vertical tiers across non-overlapping <code>86,400s</code> UTC days on either a stacked linear or per-vector symlog rate scale: (1) additive source-unit rates excluding overlapping subsets (<code>ntor_v3</code> within <code>ntor</code>, exitward queue hits within <code>circuit_killed_max_cell</code>, and HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>, with optional <code>HSDir GET/POST</code> exclusion), (2) a six-family alert strip marking days where a qualifying DoS counter reaches rank ≥90 with at least 20 reference days (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference days (high), and (3) daily fleet means for open circuits and stalled <code>BEGIN_DIR</code> connections normalized to each series’ window peak on Last 7 days through Last 12 months and retained fleet record on All time. KPI headlines pair latest absolute values with % of retained fleet peak and per-relay reference percentiles (ranked over matching active sources for the composite mitigated rate).';
 const hourHeadlineRow=hasHour?'<tr><th>Last complete hour</th><td>Latest completed 1-hour UTC window (<code>3,600s</code>): per-source <code>rate(metric[1h])</code> before summing (<code>avg_over_time(metric[1h])</code> for gauges).</td></tr>':'';
 const headlineDayDesc=hasHour?'Rolling 24-hour UTC window ending at snapshot end (<code>86,400s</code>): per-source <code>rate(metric[1d])</code> before summing (<code>avg_over_time(metric[1d])</code> for gauges). On non-midnight hourly releases, selecting a daily window (<b>Last 7 days</b>, <b>Last 1 month</b>, <b>Last 6 months</b>, or <b>Last 12 months</b>) updates Hero KPIs to the latest completed 00:00 UTC calendar day so their figures match the daily bars or areas (while <b>All time</b> keeps the 1h KPIs).':'Latest completed UTC day (<code>86,400s</code>): per-source <code>rate(metric[1d])</code> before summing (<code>avg_over_time(metric[1d])</code> for gauges).';
 const refSentence=hasHour?'One-hour references sample one window every six hours. Daily references use complete 24-hour windows. Equal values share the middle percentile rank. The rank marker is omitted when both the reference values and the current measurement are zero.':'Daily references use complete non-overlapping 24-hour UTC windows. Equal values share the middle percentile rank. The rank marker is omitted when both the reference values and the current measurement are zero.';
 const peakSentence=hasHour?'Hourly peaks are labeled <b>Sampled 1h fleet peak</b>: older hourly data is sampled four times per day. <b>Recorded 24h fleet peak</b> uses retained daily observations plus the current 24-hour measurement. The reporting-relay count at each peak is included in its tooltip and data export.':'<b>Recorded 24h fleet peak</b> uses retained daily observations plus the current 24-hour measurement. The reporting-relay count at each peak is included in its tooltip and data export.';
 const bandSentence=hasHour?'The explorer band focuses on the selected series. Shading covers its 10th-90th percentile range, with a dashed median. The 24-hour plot uses sampled one-hour reference windows; daily ranges use 24-hour reference days.':'The explorer band focuses on the selected series. Shading covers its 10th-90th percentile range across 24-hour reference days, with a dashed median, across every configured time range.';
 const pubHeading=hasHour?'Hourly publication and reference updates':'Daily publication and reference updates';
 const pubSentence=hasHour?'Hourly builds collect 1-hour (<code>3,600s</code>) and rolling 24-hour telemetry once per UTC hour and reuse a reference frozen for the UTC day; daily series and reference updates advance at 00:00 UTC.':'Collection, reference generation and live releases run once per UTC day after 00:00 UTC on non-overlapping <code>86,400s</code> intervals; sub-24-hour windows are never published.';
 const covSentence=hasHour?'Hourly source series require at least 24 samples per 1-hour UTC window; daily source series require at least 360 samples per 24-hour UTC window. Qualifying historical populations retain at least 90% of the larger of endpoint availability or the preceding 31-day peak. Missing or insufficiently covered intervals stay hatched.':'Daily source series require at least 360 samples per 24-hour UTC window. Qualifying historical populations retain at least 90% of the larger of endpoint availability or the preceding 31-day peak. Missing or insufficiently covered intervals stay hatched.';
 const coarseningIntro=hasHour
  ?'Published telemetry applies deterministic Tor <code>dir-spec</code> coarsening across both hourly (<code>3,600s</code>) and daily (<code>86,400s</code>) collection boundaries:'
  :'Published telemetry applies deterministic Tor <code>dir-spec</code> coarsening at daily (<code>86,400s</code>) collection boundaries:';
 const coarseningItems=[
  '<b>Public relay, flag, cohort, and dircache counts:</b> standalone counts below 8 floor to 0 and counts of 8 or more round to the nearest multiple of 8; partitioned build, cohort, and role breakdowns distribute multiples of 8 by largest remainder so parts sum to the coarsened total.',
  '<b>Open circuit means:</b> rounded to the nearest 100 circuits, then to 3 significant digits.',
  hasHour
   ?'<b>Onion-service rates:</b> <code>hsdir_fetches</code>, <code>hsdir_uploads</code>, <code>introduction_rejections</code>, and <code>directory_requests</code> <code>hs_get</code>/<code>hs_post</code> bin to <code>0.001/s</code> with the 8-event floor applied per measurement window (<code>3,600s</code> or <code>86,400s</code>), then round to 3 significant digits.'
   :'<b>Onion-service rates:</b> <code>hsdir_fetches</code>, <code>hsdir_uploads</code>, <code>introduction_rejections</code>, and <code>directory_requests</code> <code>hs_get</code>/<code>hs_post</code> suppress below 8 events/day, bin to <code>0.001/s</code>, and round to 3 significant digits.',
  '<b>Other counter series:</b> suppress below 8 events per window, bin to 8-event increments, and round rates to 3 significant digits.',
  chart('open_connections')
   ?'<b>Non-sensitive gauge means:</b> <code>directory_waiting</code> and <code>open_connections</code> floor below <code>0.05</code> to <code>0</code>, quantize <code>[0.05, 1)</code> to <code>0.1</code>, and round <code>&gt;= 1</code> to 3 significant digits.'
   :'<b>Non-sensitive gauge means:</b> <code>directory_waiting</code> floors below <code>0.05</code> to <code>0</code>, quantizes <code>[0.05, 1)</code> to <code>0.1</code>, and rounds <code>&gt;= 1</code> to 3 significant digits.',
 ].map(item=>`<li>${item}</li>`).join('');
 const scopeNoun=hasHour?'last complete hour':'latest complete UTC day';
 return `<section class="text-page"><h2 data-view-heading tabindex="-1">Metrics and percentiles</h2>${release?`<p>Release <code>${escapeHtml(release.id)}</code> · ${downloadLink('manifest.json','Release manifest','methods-manifest')}</p>`:''}<p>${intervalLead} ${summarySentence}</p><h3>Concurrent DoS overview</h3><p>${heroMethodDesc}</p><h3>Headline and historical windows</h3><table><tbody>${hourHeadlineRow}<tr><th>24-hour average</th><td>${headlineDayDesc}</td></tr>${periodRows}</tbody></table><h3>Historical averages &amp; Current 24h</h3><p>${dailyPeriods.map(p=>`<b>${escapeHtml(p.average_label)}</b>`).join(', ')} are unweighted arithmetic means of the measured daily fleet values in each period; missing days are excluded. These means keep fleet totals, while their <b>Current 24h</b> rulers normalize current and historical daily values per reporting relay.</p><p>Each historical ruler compares the current 24-hour average against non-overlapping qualifying daily values ending at or before the reference cutoff (${lagSentence}), keeping historical averages in their neutral color.</p><h3 id="reference" tabindex="-1">18-month reference</h3><p>The reference contains qualifying windows from the prior 18 calendar months, ending at 00:00 UTC on the day before the reference date. Newer metrics use their shorter available record (labeled <b>Reference average (N d / N h)</b> on cards). Dates and sample counts appear below each chart.</p><p>The 18-month average is calculated per reporting relay, then scaled to the current reporting count. Peaks use the fleet total recorded at that time.</p><p>${refSentence}</p><p>Current headline values, percentile labels and markers use the same color. Upper-tail colors start at the 90th percentile when at least 20 reference windows are available (the strongest color requires at least 100 reference windows and a rank at or above the 99th percentile); comparison windows with fewer than 20 daily intervals (such as 7 days) use a neutral short-window marker rather than upper-tail alert colors. Fewer than the minimum configured comparison windows use a muted color${customThresholds.length?` (${customThresholds.join(', ')})`:''}.</p><h3>Percentile and peak</h3><p>A percentile describes position in the reference distribution. A peak describes magnitude in the full retained record. The reference excludes the current 24-hour window, so even the 100th percentile can be below a newer recorded peak.</p><p>${peakSentence}</p><h3>Optional percentile band</h3><p>${bandSentence}</p><p>Each band interval is scaled by that interval’s reporting-relay count. All time applies the current reference to older observations. The band is off by default; enabling it focuses the selected series and expands the axis to include its reference range.</p><h3>${pubHeading}</h3><p>${pubSentence}</p><p>Reference date: <b>${escapeHtml(D.reference_window?.date||'Unavailable')}</b>. ${downloadLink(referenceDocument(D.reference_document),'Download the frozen reference','methods-reference')}.</p><h3>Coverage</h3><p>${covSentence}</p><h3>Dir-spec coarsening</h3><p>${coarseningIntro}</p><ul class="coarsening-list">${coarseningItems}</ul><h3>Relay counts</h3><p>Counts describe contributors to each statistic. Historical counts are daily averages over the same measured days as the metric average. Roles use the last recorded flags within each window; flag groups can overlap. Middle-only means Running with Guard, Exit, HSDir and V2Dir off. Historical counts require an available metric average. Exact role counts require complete flag coverage. With partial flag evidence, ranges include unclassified relays over the same contributing windows; endpoints are rounded outward. Unavailable counts appear as dashes.</p><h3>Tor builds</h3><p><b>Patched Tor</b> means Tor with our additional tor41339 changes; its upstream base version is shown. Build counts describe the ${scopeNoun}. Versions come from running-binary records and direct daemon responses linked to process records.</p><h3>Current DirCache 0 cohort</h3><p><b>Middle (DirCache 0)</b> uses configuration and matching flags for the ${scopeNoun}. This cohort contains ${count(D.dircache_cohort?.configured_nonexit_zero)} non-exit relays: ${count(D.dircache_cohort?.observed_controlport_zero)} observed effective configurations and ${count(D.dircache_cohort?.file_config_fallback_zero)} explicit configuration-file records.</p><p>${downloadLink('data.json','Download values, queries and provenance →','methods-json')}</p></section>`;
}
function markup(){return hero()+subNavigation()+(view==='overview'?overview():view==='explorer'?explorer():view==='fixes'?fixes():methods())+'<div class="sr-only" aria-live="polite" data-live-status></div>';}
function render(){
 urlCache=new Map();touchPlot=null;
 try{
  $('#app').innerHTML=markup();
  for(const link of document.querySelectorAll('[data-view]'))link.setAttribute('href',url(link.dataset.view));
 }finally{urlCache=null;}
 updateHeaderOffset();
 renderPlots();
}
const resetNoData=(el,hero)=>{plotMeta.delete(el);if(hero)clearPlotInspect(el);el.innerHTML=`<div class="no-data"><div>No retained samples available for this chart.<br><small>${SOURCE_CREDIT}</small></div></div>`;};
const svgHead=(width,height,left,id,unit)=>`<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" aria-hidden="true"><defs><pattern id="${id}" width="7" height="7" patternUnits="userSpaceOnUse"><path d="M0 7L7 0" stroke="#697373" stroke-width="1"/></pattern></defs><text x="${left}" y="16" class="axis-unit">${escapeHtml(unit)}</text>`;
const svgXAxisAndFooter=(start,end,width,height,left,right,x,win=DAY)=>{
 let out='';
 for(const {t,label,anchor} of xAxisTicks(start,end,width,left,right,win))out+=`<text x="${x(t)}" y="${height-(width<450?41:29)}" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
 return out+`<text x="${left}" y="${height-7}" class="source">${SOURCE_CREDIT}</text><text x="${width-right}" y="${height-(width<450?23:7)}" text-anchor="end" class="source">${win===HOUR?'1h intervals':'24h intervals'} · UTC</text></svg>`;
};
function stepPathStr(times,vals,win,x,yFn,scale=1,x0s=null,x1s=null){
 let path='',prev=null,prevX='';
 for(let i=0;i<times.length;i++){
  const row=times[i],t=vals?row:row[0],v=vals?vals[i]:row[1];
  if(v==null){prev=null;continue;}
  const s0=t-win,yy=yFn(v*scale).toFixed(2),xe=x1s?x1s[i]:x(t).toFixed(2);
  path+=(prev===s0?'L'+prevX:'M'+(x0s?x0s[i]:x(s0).toFixed(2)))+','+yy+'L'+xe+','+yy;
  prev=t;prevX=xe;
 }
 return path;
}
function draw(el,c,r,measuredWidth=null){
 const band=activeBand(c,r),selected=band?primary(c):null,data=cachedPlot(c,plotted(c,r),r),hasData=!!data;
 el.setAttribute?.('aria-label',plotLabel(c,r,hasData));
 if(!hasData){el.classList?.remove?.('plot-flat-zero');resetNoData(el,false);return;}
 const width=Math.max(240,Math.round(measuredWidth??el.getBoundingClientRect().width)),left=62,right=12,top=26,bottom=52,pw=width-left-right,scale=displayScale(c);
 let maximum=0;
 for(const s of data.list)for(const p of s.values)if(p[1]!=null&&Number.isFinite(p[1])){const sv=p[1]*scale;if(sv>maximum)maximum=sv;}
 const bandRows=[];
 let maxBandHi=maximum;
 if(band){
  const refList=data.list[0];
  for(const [t,v] of refList.values){
   const n=v!=null?refList.populations.get(t):0;
   if(!(n>0))continue;
   const bs=scaledBand(band,n,scale);
   if(bs){bandRows.push([t,bs[0],bs[1],bs[2]]);if(bs[2]>maxBandHi)maxBandHi=bs[2];}
  }
 }
 const flatZero=maximum===0&&!bandRows.length,height=flatZero?140:(width>1200?360:300),ph=height-top-bottom;
 el.classList?.toggle?.('plot-flat-zero',flatZero);
 const domain=niceScale(maxBandHi),x=t=>left+(t-data.start)/(data.end-data.start)*pw,y=v=>top+ph-v/domain.max*ph;
 const safeId=String(c.id).replace(/[^A-Za-z0-9_-]/g,ch=>'_'+ch.codePointAt(0).toString(16)+'_'),id=`missing-${safeId}-${r}`;
 let out=svgHead(width,height,left,id,band?headlineUnit(c,selected):c.display_unit);
 for(const v of domain.ticks){const yy=y(v);out+=`<line x1="${left}" y1="${yy}" x2="${width-right}" y2="${yy}" class="gridline"/><text x="${left-9}" y="${yy+4}" text-anchor="end">${axisNumber(v)}</text>`;}
 for(const [t] of data.missing)out+=`<rect x="${x(t-data.window)}" y="${top}" width="${Math.max(.5,x(t)-x(t-data.window))}" height="${ph}" fill="url(#${id})" opacity=".55"/>`;
 if(bandRows.length){
  let median='',previous=null,prevX='';
  for(const [t,lo,mid,hi] of bandRows){
   const start=t-data.window,xx=x(start),x1=x(t),ym=y(mid).toFixed(2),xe=x1.toFixed(2);
   out+=`<rect class="reference-band" x="${xx}" y="${y(hi)}" width="${x1-xx}" height="${Math.max(0,y(lo)-y(hi))}" fill="#55dda3" fill-opacity="0.12"/>`;
   median+=(previous===start?'L'+prevX:'M'+xx.toFixed(2))+','+ym+'L'+xe+','+ym;
   previous=t;prevX=xe;
  }
  out+=`<path class="reference-median" d="${median}" fill="none" stroke="#bdc8c9" stroke-width="1.2" stroke-dasharray="5 4"/>`;
 }
 let x0s=null,x1s=null;
 if(data.list.length>1){
  const refVals=data.list[0].values,m=refVals.length;
  x0s=new Array(m);x1s=new Array(m);
  for(let i=0;i<m;i++){const t=refVals[i][0];x0s[i]=x(t-data.window).toFixed(2);x1s[i]=x(t).toFixed(2);}
 }
 for(const s of data.list){
  const path=stepPathStr(s.values,null,data.window,x,y,scale,x0s,x1s);
  if(path)out+=`<path class="measurement-series" d="${path}" fill="none" stroke="${seriesColor(s.color)}" stroke-width="1.9" stroke-linejoin="round"/>`;
 }
 el.innerHTML=out+svgXAxisAndFooter(data.start,data.end,width,height,left,right,x,data.window);
 plotMeta.set(el,{c,data,width,left,right,top,pw,ph,scale,band,activeIndex:null});
}
function drawHero(el,r=heroRange,scaleMode=heroScale,includeHsdir=heroHsdir,measuredWidth=null){
 const data=heroPlotData(r,includeHsdir),hasData=!!data&&data.times.length>0;
 el.setAttribute?.('aria-label',heroPlotLabel(r,hasData,scaleMode));
 if(!hasData){resetNoData(el,true);return;}
 const width=Math.max(240,Math.round(measuredWidth??el.getBoundingClientRect().width)),height=width>1200?430:width<450?390:400;
 const left=62,right=12,top=24,bottom=width<450?56:52,pw=Math.max(1,width-left-right),ph=height-top-bottom;
 const famCount=Math.max(1,data.families.length),stripGap=12,gaugeGap=12;
 const stripH=Math.max(36,Math.min(44,famCount*6+2)),rateH=Math.round((ph-stripGap-gaugeGap-stripH)*0.65);
 const rateTop=top,stripTop=rateTop+rateH+stripGap,gaugeTop=stripTop+stripH+gaugeGap,gaugeH=Math.max(40,top+ph-gaugeTop);
 const win=data.window||DAY,n=data.times.length,x=t=>left+(t-data.start)/(data.end-data.start)*pw,symlog=v=>{if(!(v>0))return 0;const s=v/0.01;return isFiniteNumber(s)?Math.log10(1+s):Math.log10(v)+2;};
 const safeR=isHeroRange(r)?r:'24h',patId=`hero-missing-${safeR}-${scaleMode==='symlog'?'s':'l'}-${includeHsdir?1:0}`;
 const x0s=new Array(n),x1s=new Array(n);
 for(let i=0;i<n;i++){const t=data.times[i];x0s[i]=x(t-win).toFixed(2);x1s[i]=x(t).toFixed(2);}
 let out=svgHead(width,height,left,patId,scaleMode==='symlog'?'events/s (symlog)':'events/s'),rateY;
 const addGrid=(yy,lbl,dy=4)=>{out+=`<line x1="${left}" y1="${yy.toFixed(2)}" x2="${width-right}" y2="${yy.toFixed(2)}" class="gridline"/><text x="${left-9}" y="${(yy+dy).toFixed(2)}" text-anchor="end">${lbl}</text>`;};
 if(scaleMode==='symlog'){
  const boosted=data.maxSeries*1.15,rawMax=Math.max(1,isFiniteNumber(boosted)?boosted:data.maxSeries),maxS=symlog(rawMax),limit=isFiniteNumber(rawMax*1.05)?rawMax*1.05:rawMax;
  rateY=v=>rateTop+rateH-(symlog(v)/maxS)*rateH;
  const cands=[0];
  for(let p=-1;p<=308;p++){const v=10**p;if(!isFiniteNumber(v)||v>limit||symlog(v)>maxS)break;cands.push(v);}
  let lastY=Infinity;
  for(const v of cands){const yy=rateY(v);if(v===0||lastY-yy>=16){addGrid(yy,axisNumber(v));lastY=yy;}}
 }else{
  const domain=niceScale(data.maxTotal);
  rateY=v=>rateTop+rateH-(v/domain.max)*rateH;
  for(const v of domain.ticks)addGrid(rateY(v),axisNumber(v));
 }
 const hatchRuns=(pred,yy,hh,op,cls='')=>{
  for(let i=0;i<n;){
   if(!pred(i)){i++;continue;}
   let j=i+1;while(j<n&&pred(j))j++;
   const rw=(+x1s[j-1]-+x0s[i]).toFixed(2);
   if(+rw>0)out+=`<rect${cls?` class="${cls}"`:''} x="${x0s[i]}" y="${yy}" width="${rw}" height="${hh}" fill="url(#${patId})" opacity="${op}"/>`;
   i=j;
  }
 };
 const stepPath=(vals,yFn,cls,color,sw,dash='')=>{
  const path=stepPathStr(data.times,vals,win,x,yFn,1,x0s,x1s);
  if(path)out+=`<path class="${cls}" d="${path}" fill="none" stroke="${color}" stroke-width="${sw}"${dash} stroke-linejoin="round"/>`;
 };
 hatchRuns(i=>data.missingRates[i],rateTop,rateH,'.55');
 const stepW=pw/n,barMode=scaleMode!=='symlog'&&n<=35,pad=barMode?Math.min(8,stepW*0.17):0;
 if(scaleMode==='symlog'){
  for(const vec of data.vectors)stepPath(vec.values,rateY,'hero-rate-series',seriesColor(vec.color),n<=14?2.1:1.6);
 }else if(barMode){
  for(let i=0;i<n;i++){
   if(data.missingRates[i])continue;
   const t=data.times[i],bx=(x(t-win)+pad).toFixed(2),bw=Math.max(1,x(t)-x(t-win)-2*pad).toFixed(2);
   let acc=0;
   for(const vec of data.vectors){
    const val=vec.values[i]??0;
    if(!(val>0))continue;
    const y1=rateY(acc+val),y0=rateY(acc),bh=Math.min(rateH,Math.max(0.6,y0-y1)),by=Math.max(rateTop,Math.min(y1,rateTop+rateH-bh));
    out+=`<rect class="hero-rate-bar" x="${bx}" y="${by.toFixed(2)}" width="${bw}" height="${bh.toFixed(2)}" fill="${seriesColor(vec.color)}" fill-opacity="0.9"/>`;
    acc+=val;
   }
  }
 }else{
  const acc=new Float64Array(n),prevY=Array(n).fill(rateY(0).toFixed(2));
  for(const vec of data.vectors){
   for(let i=0;i<n;){
    if(data.missingRates[i]){i++;continue;}
    let j=i+1;while(j<n&&!data.missingRates[j])j++;
    let hasPositive=false,top='';
    const nextY=[];
    for(let k=i;k<j;k++){
     const v=vec.values[k]??0;
     if(v>0)hasPositive=true;
     const a=acc[k]+v,yt=rateY(a).toFixed(2);
     acc[k]=a;nextY.push(yt);
     top+=(k===i?'M':'L')+x0s[k]+','+yt+'L'+x1s[k]+','+yt;
    }
    if(hasPositive){
     let bot='';
     for(let k=j-1;k>=i;k--)bot+='L'+x1s[k]+','+prevY[k]+'L'+x0s[k]+','+prevY[k];
     out+=`<path class="hero-rate-area" d="${top}${bot}Z" fill="${seriesColor(vec.color)}" fill-opacity="0.86"/>`;
     for(let k=i;k<j;k++)prevY[k]=nextY[k-i];
    }
    i=j;
   }
  }
 }
 hatchRuns(i=>data.partialRates[i],rateTop,rateH,'.35','hero-rate-partial');
 const laneRange=`${escapeHtml(data.families[0]?.icon||'01')}–${escapeHtml(data.families.at(-1)?.icon||'06')}`;
 out+=`<rect x="${left}" y="${stripTop}" width="${pw}" height="${stripH}" fill="#3e4b4d" fill-opacity="0.22"/><text x="${left-9}" y="${(stripTop+11).toFixed(2)}" text-anchor="end">${laneRange}</text><text x="${left-9}" y="${(stripTop+stripH-2).toFixed(2)}" text-anchor="end">≥90th</text>`;
 const laneH=stripH/famCount;
 for(let fi=0;fi<data.families.length;fi++){
  const fam=data.families[fi],yy=(stripTop+fi*laneH+0.5).toFixed(2),hh=Math.max(2,laneH-1).toFixed(2);
  for(let i=0;i<n;){
   const st=fam.rows[i].state;
   if(st==='neutral'){i++;continue;}
   let j=i+1;while(j<n&&fam.rows[j].state===st&&!barMode)j++;
   const rx0=pad?(x(data.times[i]-win)+pad).toFixed(2):x0s[i],rx1=pad?(x(data.times[j-1])-pad).toFixed(2):x1s[j-1],rw=(+rx1-+rx0).toFixed(2);
   const fill=st==='high'?'#ff9873':st==='elevated'?'#f1d05d':`url(#${patId})`;
   if(+rw>0)out+=`<rect class="hero-alert-${st}" data-family-lane="${escapeHtml(fam.id)}" x="${rx0}" y="${yy}" width="${rw}" height="${hh}" fill="${fill}"${st==='missing'?' opacity="0.6"':''}/>`;
   i=j;
  }
 }
 for(const [frac,lbl] of [[1,'100%'],[0,'0%']])addGrid(gaugeTop+gaugeH-frac*gaugeH,lbl,frac?8:4);
 hatchRuns(i=>data.gauges.openCircuits.missing[i]||data.gauges.directoryWaiting.missing[i],gaugeTop,gaugeH,'.45');
 const gaugeY=sp=>v=>gaugeTop+gaugeH-Math.min(1,Math.max(0,v/sp))*gaugeH;
 stepPath(data.gauges.openCircuits.values,gaugeY(data.gauges.openCircuits.scalePeak),'hero-gauge-series','#f1d05d',1.9);
 stepPath(data.gauges.directoryWaiting.values,gaugeY(data.gauges.directoryWaiting.scalePeak),'hero-gauge-series','#ff9873',1.9,' stroke-dasharray="5 3"');
 el.innerHTML=out+svgXAxisAndFooter(data.start,data.end,width,height,left,right,x,win);
 plotMeta.set(el,{hero:true,data,width,left,right,top,pw,ph,activeIndex:null});
}
function clearPlotInspect(el){
 if(el===touchPlot)touchPlot=null;
 const meta=plotMeta.get(el);
 if(meta)meta.activeIndex=null;
 el.querySelector?.('.plot-cursor')?.remove();
 el.querySelector?.('.chart-tooltip')?.remove();
}
function inspectPlot(el,index,announce){
 const meta=plotMeta.get(el),svg=el.querySelector?.('svg');
 if(!meta||!svg)return;
 const {data,left,right,top,ph,width,pw}=meta;
 const len=meta.hero?data.times.length:data.list[0]?.values?.length||0;
 if(!len||!Number.isFinite(index))return;
 const clamped=Math.max(0,Math.min(len-1,Math.trunc(index)));
 meta.activeIndex=clamped;
 const win=data.window||DAY,t=meta.hero?data.times[clamped]:data.list[0].values[clamped][0],intervalStart=t-win;
 const midX=left+((intervalStart+t)/2-data.start)/(data.end-data.start)*pw,header=`${formatIntervalWindow(intervalStart,t,win)} (${win===HOUR?'1h':'24h'})`;
 let rows,metaLines,announceTitle;
 if(meta.hero){
  rows=data.vectors.map(vec=>{
   const raw=vec.values[clamped],base=raw==null?'Unmeasured':number(raw)+' events/s';
   const extra=vec.id==='queue'&&raw>0&&isFiniteNumber(raw*3600)?` (${number(raw*3600)}/h)`:'';
   return {name:vec.label,color:seriesColor(vec.color),value:base+extra+(raw!=null&&vec.partial?.[clamped]?' (partial)':'')};
  });
  const total=data.totals[clamped],rateShare=peakShare(total,data.ratePeak);
  rows.push({name:'Total active rate',color:'#00ff7f',value:total==null?'Missing':number(total)+' events/s'+(rateShare?` (${rateShare} of peak)`:'')+(data.partialRates?.[clamped]?' (partial coverage)':'')});
  metaLines=[];
  const hot=[],unmeasured=[];
  for(const f of data.families){
   const cell=f.rows[clamped];
   if(cell.state==='missing')unmeasured.push(`${f.icon} ${f.title}`);
   else if(cell.state==='high'||cell.state==='elevated'){
    const srcPrefix=cell.chartId&&f.primary&&cell.chartId!==f.primary&&cell.chartTitle&&cell.chartTitle!==cell.seriesName?`${cell.chartTitle}: `:'';
    const srcText=cell.seriesName?` — ${srcPrefix}${cell.seriesName}`:'';
    hot.push(`${f.icon} ${f.title} (${cell.state}, ${formatPercentileOrdinal(cell.rank)})${srcText}`);
   }
  }
  if(unmeasured.length===data.families.length)metaLines.push(`Unmeasured families: ${unmeasured.join(', ')}`);
  else{
   metaLines.push(hot.length?'Alerts ≥90th: '+hot.join(', '):'Alerts ≥90th: No qualifying family alerts');
   if(unmeasured.length)metaLines.push(`Unmeasured families: ${unmeasured.join(', ')}`);
  }
  const winLbl=data.range||(win===HOUR?'24h':'7d');
  for(const [lbl,g] of [['Open circuits',data.gauges.openCircuits],['Stalled BEGIN_DIR',data.gauges.directoryWaiting]]){
   const v=g.values[clamped],pct=peakShare(v,g.rawPeak),winShare=g.windowPeak>0&&g.rawPeak>0&&g.scalePeak!==g.rawPeak?peakShare(v,g.scalePeak):null;
   metaLines.push(`${lbl}: ${v==null?'Missing':number(v)}${pct?` (${pct}${winShare?`, ${winShare} of ${winLbl} peak`:''})`:''}`);
  }
  const relays=data.relayRangeByTime.get(t);
  if(relays)metaLines.push(`Reporting relays: ${relays[0]===relays[1]?count(relays[0]):`${count(relays[0])}–${count(relays[1])}`}`);
  announceTitle=announce?'Concurrent DoS activity':null;
 }else{
  const {c,scale,band}=meta,unit=band?headlineUnit(c,primary(c)):c.display_unit;
  rows=[];let minPop=Infinity,maxPop=0;
  for(const s of data.list){
   const raw=s.values[clamped]?.[1],n=raw!=null?s.populations.get(t):null;
   rows.push({name:s.name,color:seriesColor(s.color),value:raw==null?'Missing':scaledText(c,raw)+' '+unit});
   if(raw!=null&&Number.isInteger(n)&&n>0){if(n<minPop)minPop=n;if(n>maxPop)maxPop=n;}
  }
  metaLines=[];
  if(minPop<Infinity)metaLines.push(`Reporting relays: ${minPop===maxPop?count(minPop):`${count(minPop)}–${count(maxPop)}`}`);
  if(band&&minPop<Infinity&&data.list[0].values[clamped]?.[1]!=null){
   const n=data.list[0].populations.get(t);
   if(n>0){
    const bs=scaledBand(band,n,scale);
    if(bs)metaLines.push(`10th–90th: ${formatScaled(band.p10_per_relay,bs[0])}–${formatScaled(band.p90_per_relay,bs[2])} (median ${formatScaled(band.p50_per_relay,bs[1])})`);
   }
  }
  announceTitle=announce?c.title:null;
 }
 let cursor=svg.querySelector?.('.plot-cursor');
 if(!cursor&&typeof document.createElementNS==='function'){
  cursor=document.createElementNS('http://www.w3.org/2000/svg','line');
  cursor.setAttribute('class','plot-cursor');svg.appendChild(cursor);
 }
 if(cursor){cursor.setAttribute('x1',midX.toFixed(2));cursor.setAttribute('x2',midX.toFixed(2));cursor.setAttribute('y1',String(top));cursor.setAttribute('y2',String(top+ph));}
 let tooltip=el.querySelector?.('.chart-tooltip');
 if(!tooltip&&typeof document.createElement==='function'){tooltip=document.createElement('div');tooltip.className='chart-tooltip';el.appendChild(tooltip);}
 if(tooltip){
  const tipLeft=width<450?8:left,tipRight=width<450?8:right;
  tooltip.innerHTML=`<div class="chart-tooltip-time">${escapeHtml(header)}</div>`+rows.map(row=>`<div class="chart-tooltip-row"><span class="chart-tooltip-label"><i style="background:${seriesColor(row.color)}"></i>${escapeHtml(row.name)}</span><span>${escapeHtml(row.value)}</span></div>`).join('')+(metaLines.length?`<div class="chart-tooltip-meta">${metaLines.map(escapeHtml).join(' · ')}</div>`:'');
  if(midX>width*0.55){tooltip.style.left=tipLeft+'px';tooltip.style.right='auto';}else{tooltip.style.right=tipRight+'px';tooltip.style.left='auto';}
 }
 if(announceTitle)announceStatus(`${announceTitle}, ${header}: `+rows.map(row=>`${row.name} ${row.value}`).join(', ')+(metaLines.length?`, ${metaLines.join(', ')}`:''));
}
const plotLength=meta=>meta?.hero?(meta.data?.times?.length||0):(meta?.data?.list[0]?.values?.length||0);
function renderPlots(){
 const heroes=document.querySelectorAll('[data-hero-plot]'),chartsList=document.querySelectorAll('[data-chart-plot]'),hW=[],cW=[],hActive=[],cActive=[];
 for(let i=0;i<heroes.length;i++){hW.push(heroes[i].getBoundingClientRect().width);hActive.push(plotMeta.get(heroes[i])?.activeIndex??null);}
 for(let i=0;i<chartsList.length;i++){cW.push(chartsList[i].getBoundingClientRect().width);cActive.push(plotMeta.get(chartsList[i])?.activeIndex??null);}
 const restore=(el,idx)=>{const meta=plotMeta.get(el);if(meta&&Number.isInteger(idx)&&idx>=0&&idx<plotLength(meta))inspectPlot(el,idx,false);};
 const visibleHW=hW.filter(w=>w>0);
 const dualW=visibleHW.length===2&&Math.abs(visibleHW[0]-visibleHW[1])<=2?Math.min(visibleHW[0],visibleHW[1]):null;
 for(let i=0;i<heroes.length;i++){
  if(!(hW[i]>0)){clearPlotInspect(heroes[i]);plotMeta.delete(heroes[i]);continue;}
  drawHero(heroes[i],heroes[i].dataset.heroPlotRange||heroRange,heroScale,heroHsdir,dualW??hW[i]);
  restore(heroes[i],hActive[i]);
 }
 for(let i=0;i<chartsList.length;i++){const c=chart(chartsList[i].dataset.chartPlot);if(c){draw(chartsList[i],c,chartsList[i].dataset.range,cW[i]);restore(chartsList[i],cActive[i]);}}
}
function start(){
 render();
 scrollToFragment();
const targetEl=e=>e.target&&typeof e.target.closest==='function'?e.target:null;
document.addEventListener('click',async e=>{
 const t=targetEl(e);if(!t)return;
 const hit=s=>t.closest(s),anchor=hit('a[href]');
 if(anchor&&(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button!==0))return;
 const famJump=hit('[data-family-jump]');
 if(famJump){
  const fid=famJump.dataset.familyJump,target=fid&&document.getElementById?.('family-'+fid);
  if(target){
   e.preventDefault();
   closeNavigation();
   const hash='#family-'+encodeURIComponent(fid),nextUrl=(location.search||'')+hash;
   if(location.hash!==hash){try{window.history.pushState(null,'',nextUrl);}catch{try{location.hash=hash;}catch{}}}
   target.scrollIntoView?.({block:'start'});
   target.focus?.({preventScroll:true});
   return;
  }
 }
 const heroRangeToggle=hit('[data-hero-range]');if(heroRangeToggle){const next=normalizeHeroRange(heroRangeToggle.dataset.heroRange,isHourlyRelease()),prevReq=requestedHeroRange;requestedHeroRange=next;if(next!==heroRange||prevReq!==next){heroRange=next;redrawControl(`[data-hero-range="${heroRange}"]`);}return;}
 const heroScaleToggle=hit('[data-hero-scale]');if(heroScaleToggle){const next=heroScaleToggle.dataset.heroScale==='symlog'?'symlog':'linear';if(next!==heroScale){heroScale=next;redrawControl(`[data-hero-scale="${heroScale}"]`);}return;}
 if(hit('[data-hero-hsdir]')){heroHsdir=!heroHsdir;redrawControl('[data-hero-hsdir]');return;}
 const allToggle=hit('[data-all-range]');if(allToggle){const nextRange=allToggle.dataset.allRange;if(periodMap.has(nextRange)){range=nextRange;redrawControl(`[data-all-range="${range}"]`);}return;}
 const layoutToggle=hit('[data-layout]');if(layoutToggle){const next=layoutToggle.dataset.layout==='grid'?'grid':'full';if(next!==layout){layout=next;redrawControl(`[data-layout="${layout}"]`);}return;}
 if(hit('[data-band-toggle]')){bandEnabled=!bandEnabled;redrawControl('[data-band-toggle]');return;}
 const nav=hit('[data-view]');if(nav){e.preventDefault();if(['overview','explorer','fixes','methods'].includes(nav.dataset.view))setView(nav.dataset.view);return;}
 const ex=hit('[data-explore]');if(ex){e.preventDefault();const f=family(ex.dataset.explore),c=chart(ex.dataset.chart)||(f&&chart(f.primary)),r=periodMap.has(ex.dataset.range)?ex.dataset.range:range,hasSeries=!!(ex.hasAttribute?.('data-series')||Object.hasOwn(ex.dataset,'series')),sKey=hasSeries?(ex.getAttribute?.('data-series')??ex.dataset.series??''):undefined;if(c)setView('explorer',r,c.family_id,c.id,undefined,sKey);return;}
 const child=hit('[data-child]');if(child){e.preventDefault();const c=chart(child.dataset.child);if(c)setView('explorer',range,c.family_id,c.id);return;}
 const fam=hit('[data-family]');if(fam){const f=family(fam.dataset.family);if(f)setView('explorer',range,f.id,f.primary);return;}
 if(hit('[data-methods]')){e.preventDefault();if(!setView('methods'))return;window.history.replaceState(null,'',url('methods')+'#reference');focusDestination('#reference');document.getElementById('reference')?.scrollIntoView({block:'start'});return;}
 const cp=hit('[data-copy]');if(cp){
  const seq=(copyRequests.get(cp)||0)+1;
  copyRequests.set(cp,seq);
  let ok=false;
  try{
   const c=chart(cp.dataset.copy),kind=cp.dataset.queryKind,text=c?(kind==='coverage'?coverageQuery(c,range):kind==='headline'?headlineQuery(c):plotQuery(c,range)):null;
   req(text,'Missing query');
   await browser.navigator.clipboard.writeText(text);
   ok=true;
  }catch{}
  if(copyRequests.get(cp)!==seq)return;
  cp.textContent=ok?'Copied':'Select text to copy';
  announceStatus(ok?'Copied PromQL query to clipboard.':'Clipboard unavailable; select query text to copy.');
  clearTimeout(copyTimers.get(cp));
  const timer=setTimeout(()=>{if(copyRequests.get(cp)===seq&&cp.isConnected!==false)cp.textContent='Copy';},2000);
  if(typeof timer?.unref==='function')timer.unref();
  copyTimers.set(cp,timer);
  return;
 }
 const menu=hit('.nav-toggle');
 if(menu){const links=$('.nav-links');links?.classList.toggle('show');menu.setAttribute('aria-expanded',String(!!links?.classList.contains('show')));updateHeaderOffset();return;}
 if(!hit('.nav')&&$('.nav-links')?.classList.contains('show'))closeNavigation();
});
document.addEventListener('change',e=>{
 const select=targetEl(e)?.closest('[data-series-select]');
 if(select&&chart(chartId)?.series.some(s=>s.key===select.value)){seriesKey=select.value;redrawControl('[data-series-select]');}
});
const plotNode=target=>target?.closest?.('[data-hero-plot]')||target?.closest?.('[data-chart-plot]')||null;
document.addEventListener('keydown',e=>{
 if(e.key==='Escape'&&$('.nav-links')?.classList.contains('show')){e.preventDefault?.();closeNavigation();$('.nav-toggle')?.focus?.({preventScroll:true});return;}
 const t=targetEl(e),plot=t&&plotNode(t);if(!plot)return;
 const meta=plotMeta.get(plot),len=plotLength(meta);if(!len)return;
 if(e.key==='ArrowLeft'){e.preventDefault();inspectPlot(plot,meta.activeIndex==null?len-1:Math.max(0,meta.activeIndex-1),true);}
 else if(e.key==='ArrowRight'){e.preventDefault();inspectPlot(plot,meta.activeIndex==null?len-1:Math.min(len-1,meta.activeIndex+1),true);}
 else if(e.key==='Home'){e.preventDefault();inspectPlot(plot,0,true);}
 else if(e.key==='End'){e.preventDefault();inspectPlot(plot,len-1,true);}
 else if(e.key==='Escape'&&meta.activeIndex!=null){e.preventDefault?.();clearPlotInspect(plot);}
});
const inspectPointer=(plot,clientX,isTouch=false)=>{
 if(!isFiniteNumber(clientX))return;
 const meta=plotMeta.get(plot),svg=plot.querySelector?.('svg');if(!meta||!svg)return;
 const rect=svg.getBoundingClientRect();if(!rect||!(rect.width>0))return;
 const svgX=(clientX-rect.left)*(meta.width/rect.width);
 if(!isFiniteNumber(svgX)||svgX<meta.left||svgX>meta.width-meta.right){clearPlotInspect(plot);return;}
 const len=plotLength(meta);if(!len)return;
 const index=Math.max(0,Math.min(len-1,Math.floor((svgX-meta.left)/Math.max(1,meta.pw)*len)));
 if(index!==meta.activeIndex)inspectPlot(plot,index,false);
 if(isTouch)touchPlot=plot;
};
document.addEventListener('pointerdown',e=>{
 if(e.pointerType!=='touch')return;
 const t=targetEl(e),plot=t&&plotNode(t);
 if(touchPlot&&touchPlot!==plot)clearPlotInspect(touchPlot);
 if(plot)inspectPointer(plot,e.clientX,true);
});
document.addEventListener('pointermove',e=>{
 const t=targetEl(e),plot=t&&plotNode(t);if(!plot)return;
 inspectPointer(plot,e.clientX,e.pointerType==='touch');
});
const onPlotLeave=e=>{
 if(e.pointerType==='touch')return;
 const t=targetEl(e),plot=t&&plotNode(t);
 if(plot&&(!e.relatedTarget||!plot.contains?.(e.relatedTarget)))clearPlotInspect(plot);
};
for(const type of ['pointerout','focusout'])document.addEventListener(type,onPlotLeave);
let resizeTimer;window.addEventListener('resize',()=>{updateHeaderOffset();clearTimeout(resizeTimer);resizeTimer=setTimeout(renderPlots,80);});
window.addEventListener('popstate',()=>{
 ({view,range,layout,familyId,chartId,seriesKey,bandEnabled,heroRange,heroScale,heroHsdir}=parseLocation(D,location.search,location.hash,browser.DEFAULT_DESIGN));
 requestedHeroRange=parseRequestedHeroRange(location.search);
 render();closeNavigation();
 const frag=scrollToFragment(),fragId=frag?.getAttribute?.('id')||'';
 if(frag&&((view==='methods'&&fragId==='reference')||(view==='overview'&&fragId.startsWith('family-'))))frag.focus?.({preventScroll:true});
 else focusDestination('[data-view-heading]');
});
}
return {start,markup,draw,drawHero,url,replaceRelease,updateFreshness,getState:()=>({view,range,layout,familyId,chartId,seriesKey,bandEnabled,heroRange,heroScale,heroHsdir})};
}

function startObservatory(browser){
 try{
  const data=browser.ONIONSURGE_DATA;
  req(data?.families?.length&&data?.charts?.length,'Missing chart data');
  const app=createObservatory(data,browser);app.start();return app;
 }catch{
  browser.document.getElementById('app').textContent='Unable to load chart data. Please reload this page.';
  return null;
 }
}

if(typeof module!=='undefined'&&module.exports){
 module.exports={metricSelectorTokens,createObservatory,startObservatory,parseLocation,preparePlot,prepareHeroPlot,validateTimeRanges,LEGACY_V5_TIME_RANGES,HERO_RANGE_META,HERO_HOURLY_LEFT_RANGES,HERO_DAILY_LEFT_RANGES,isHeroRange,normalizeHeroRange,quantileSorted,calendarWindowStart,periodWindowStart,HOUR,DAY,RESERVED_JSON_KEYS,FORBIDDEN_HOURLY_KEYS,req,isPlainRecord,hasExactKeys,isFiniteNumber,number,ordinal,percentile,niceScale,axisNumber,xAxisTicks};
}else{
 if(document.documentElement.dataset.mode!=='live')startObservatory(window);
}
