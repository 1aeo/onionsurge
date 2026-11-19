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

function metricSelectorTokens(c,series){
 const source=SELECTOR_SOURCES.get(c.id);
 const known=source&&c.metrics.length===1&&c.metrics[0]===source.metric
  &&Array.isArray(series)&&series.length>0&&series.every(s=>source.keys.has(s?.key));
 const matchers=known?source.fixed.map(([label,value])=>[label,[value]]):[];
 if(known&&source.label)matchers.push([source.label,[...new Set(series.map(s=>SOURCE_SERIES_ALIASES.get(s.key)||s.key))]]);
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
const number=n=>!isFiniteNumber(n)?'Unavailable':n===0?'0':Math.abs(n)<1?(n>0?'<1':'>−1'):wholeFormat.format(n);
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
const DAY=86400;
const RESERVED_JSON_KEYS=new Set(['__proto__','constructor','prototype']);
const FORBIDDEN_HOURLY_KEYS=new Set(['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24']);
const TIME_RANGE_ID=/^(?!.*(?:^|-)(?:24h|1d|[0-9]+[smh])$)[a-z0-9][a-z0-9_-]{0,31}$/;
const SUMMARY_KEY=/^[a-z][a-z0-9_]{0,63}$/;
const EXPORT_SUFFIX=/^(?!.*(?:^|-)(?:24h|1d|[0-9]+[smh])$)[a-z0-9]+(?:-[a-z0-9]+){0,7}$/;
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
 let allTimeCount=0;
 for(const period of config.periods){
  hasExactKeys(period,TIME_RANGE_PERIOD_KEYS,'Invalid time_ranges period');
  const {id,summary_key:skey,export_suffix:sfx,window:w,minimum_coverage:cov,minimum_comparison_windows:mcw}=period;
  req(typeof id==='string'&&TIME_RANGE_ID.test(id)&&!RESERVED_JSON_KEYS.has(id)&&!seenIds.has(id),'Invalid time_ranges period id');
  seenIds.add(id);
  req(typeof skey==='string'&&SUMMARY_KEY.test(skey)&&skey!=='day'&&!RESERVED_JSON_KEYS.has(skey)&&!FORBIDDEN_HOURLY_KEYS.has(skey)&&!seenSummaryKeys.has(skey),'Invalid time_ranges summary_key');
  seenSummaryKeys.add(skey);
  req(typeof sfx==='string'&&sfx.length<=32&&EXPORT_SUFFIX.test(sfx)&&!RESERVED_JSON_KEYS.has(sfx)&&!seenSuffixes.has(sfx),'Invalid time_ranges export_suffix');
  seenSuffixes.add(sfx);
  for(const field of ['label','average_label','plot_label'])req(okText(period[field],80),'Invalid time_ranges label');
  hasExactKeys(w,TIME_RANGE_WINDOW_KEYS,'Invalid time_ranges window');
  const bounds=typeof w.unit==='string'&&Object.hasOwn(UNIT_COUNT_BOUNDS,w.unit)?UNIT_COUNT_BOUNDS[w.unit]:null;
  if(bounds){
   req(Number.isInteger(w.count)&&w.count>=bounds[0]&&w.count<=bounds[1]&&(Object.is(w.comparison_lag_days,0)||w.comparison_lag_days===1),`Invalid time_ranges ${w.unit} window`);
   req(isFiniteNumber(cov)&&!Object.is(cov,-0)&&cov>=0&&cov<=1,'Invalid time_ranges minimum_coverage');
  }else{
   req(w.unit==='all_time','Unsupported time_ranges window unit');
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
 req(typeof fallbackDynamic==='string'&&seenIds.has(fallbackDynamic)&&!fixed.includes(fallbackDynamic),'Invalid time_ranges card_slots.fallback_dynamic');
 return config;
}
function resolveTimeRanges(D){
 return D?.schema_version===6||D?.time_ranges!==undefined?validateTimeRanges(D.time_ranges):LEGACY_V5_TIME_RANGES;
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
const isGaugeKind=c=>c?.kind==='gauge_daily_mean';
const qualifiedDailyRow=(p,scale=1)=>Array.isArray(p)&&validTime(p[0])&&p[0]%DAY===0&&isFiniteNumber(p[1])&&p[1]>=0&&isFiniteNumber(p[1]*scale)&&p[4]===true&&Number.isInteger(p[2])&&p[2]>0&&isFiniteNumber(p[3])&&p[3]>=0.9&&p[3]<=1;
const peakShare=(val,peak)=>{
 if(val==null||!(peak>0))return null;
 const pct=val/peak*100;
 if(pct<0.1&&val>0)return '<0.1%';
 const formatted=decimalFormat.format(cleanZero(pct));
 return (formatted==='-0'?'0':formatted)+'%';
};
function validateDataGraph(D){
 req(D&&Array.isArray(D.families)&&D.families.length&&Array.isArray(D.charts)&&D.charts.length,'Missing chart data');
 if(D.schema_version===6||D.time_ranges!==undefined)validateTimeRanges(D.time_ranges);
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
function parseLocation(data,search='',hash='',defaultDesign='1'){
 const timeRanges=resolveTimeRanges(data),periodIds=new Set(timeRanges.periods.map(p=>p.id));
 const allTimeId=timeRanges.periods.find(p=>p.window.unit==='all_time').id,params=new URLSearchParams(search);
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
 const selectedSeries=wantedSeries&&selectedChart.display_keys.includes(wantedSeries)?selectedChart.series.find(s=>s.key===wantedSeries):null;
 return {
  view:['overview','explorer','fixes','methods'].includes(requested)?requested:'overview',
  range:periodIds.has(aliased)?aliased:legacyAll||defaultAll?allTimeId:timeRanges.default_range,
  layout:params.get('layout')==='grid'?'grid':'full',
  familyId:selectedFamily.id,chartId:selectedChart.id,seriesKey:selectedSeries?.key||null,
  bandEnabled:params.get('band')==='1',heroRange:params.get('hero_range')==='all'?'all':'7d',
  heroScale:params.get('hero_scale')==='symlog'?'symlog':'linear',heroHsdir:params.get('hero_hsdir')!=='0',
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
]);

function prepareHeroPlot(D,heroRange='7d',includeHsdir=true,cachedSeries=null){
 if(!D||!Array.isArray(D.charts)||!Array.isArray(D.families))return null;
 const r=heroRange==='all'?'all':'7d';
 const chartsById=new Map();
 for(const c of D.charts)if(c&&typeof c.id==='string')chartsById.set(c.id,c);
 let rawEnd=validTime(D.end)?D.end:-Infinity;
 if(rawEnd===-Infinity)for(const c of D.charts)if(validTime(c?.end)&&c.end>rawEnd)rawEnd=c.end;
 if(!validTime(rawEnd))return null;
 const end=Math.floor(rawEnd/DAY)*DAY,baselineEnd=end-DAY,rw=D.reference_window;
 const baselineStart=validTime(rw?.start)&&rw?.end===baselineEnd&&rw.start<baselineEnd?rw.start:calendarWindowStart(baselineEnd,18);
 const seriesCache=cachedSeries||new Map();
 const indexSeries=(cid,s,needBaseline=false)=>{
  if(!s)return null;
  const cacheKey='s:'+cid+':'+s.key;
  let entry=seriesCache.get(cacheKey);
  if(entry&&(!needBaseline||entry.baseline!==null))return entry;
  const c=entry?.c||chartsById.get(cid),scale=c?displayScale(c):1;
  const values=entry?.values||new Map(),populations=entry?.populations||new Map();
  let minTs=entry?.minTs??Infinity;
  if(!entry){
   for(const p of s.history?.daily||[]){
    if(!qualifiedDailyRow(p,scale)||p[0]>end)continue;
    values.set(p[0],p[1]);populations.set(p[0],p[2]);
    if(p[0]<minTs)minTs=p[0];
   }
  }
  let baseline=entry?.baseline??null;
  if(needBaseline){
   baseline=[];
   for(const [t,v] of values){
    const pr=v/populations.get(t);
    if(isFiniteNumber(pr)&&t-DAY>=baselineStart&&t<=baselineEnd)baseline.push(pr);
   }
   baseline.sort((a,b)=>a-b);
  }
  entry={c,s,values,populations,baseline,minTs};
  seriesCache.set(cacheKey,entry);
  return entry;
 };
 let earliestTs=Infinity,hasMissingSpec=false;
 const familySpecs=D.families.map(f=>{
  const members=[];
  for(const cid of f.children||[]){
   const c=chartsById.get(cid);
   if(!c||isGaugeKind(c)||!Array.isArray(c.series)||!Array.isArray(c.display_keys))continue;
   for(const s of c.series){
    if(!s||!c.display_keys.includes(s.key)||HERO_ALERT_EXCLUDE.has(cid+':'+s.key))continue;
    const idx=indexSeries(cid,s,true);
    if(idx.minTs<earliestTs)earliestTs=idx.minTs;
    members.push({c,s,idx});
   }
  }
  return {id:f.id,icon:f.icon,title:f.title,members};
 });
 const vectors=[];
 for(const spec of HERO_VECTORS){
  if(spec.hsdir&&!includeHsdir)continue;
  const sources=[];let missingSources=0;
  for(const [cid,keys] of spec.sources){
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
  const idx=s?indexSeries(cid,s,false):null;
  if(idx&&idx.minTs<earliestTs)earliestTs=idx.minTs;
  let maxHist=0;
  if(idx)for(const v of idx.values.values())if(v>maxHist)maxHist=v;
  const peak=s?.peaks?.day?.fleet_peak,rawPeak=Math.max(isFiniteNumber(peak)&&peak>0?peak:0,maxHist);
  return {cid,key,unit,idx,rawPeak,scalePeak:rawPeak>0?rawPeak:1,values:[],missing:[]};
 };
 const openCircuits=resolveGauge('open_circuits','open_circuits','circuits');
 const directoryWaiting=resolveGauge('directory_waiting','directory_waiting','connections');
 if(!Number.isFinite(earliestTs))return null;
 const start=r==='all'?earliestTs-DAY:end-7*DAY;
 if(!validTime(start)||!validTime(end)||end<=start||(end-start)/DAY>MAX_PLOT_INTERVALS)return null;
 const times=[];
 for(let t=start+DAY;t<=end;t+=DAY)times.push(t);
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
 }
 const sevOrder={neutral:1,elevated:2,high:3};
 let hasMissingAlerts=false;
 const families=familySpecs.map(fam=>({
  id:fam.id,icon:fam.icon,title:fam.title,
  rows:times.map(t=>{
   const isEnd=t===end,inBase=t-DAY>=baselineStart&&t<=baselineEnd;
   let bestSev=-1,bestRank=-Infinity,bestState='missing',bestN=0,bestSeries=null,bestChart=null;
   for(const {c,s,idx} of fam.members){
    const v=idx.values.get(t);
    if(v===undefined)continue;
    const pop=idx.populations.get(t),pr=v/pop;
    if(!isFiniteNumber(pr))continue;
    hasAnyMeasured=true;notePop(t,pop);
    let rank=null,n=0,flatZero=false;
    const ref=s.reference?.day;
    if(isEnd&&ref&&typeof ref==='object'){
     if(s.current?.day?.valid===true&&Number.isInteger(ref.n)&&ref.n>0&&(ref.flat_zero||(isFiniteNumber(ref.current_percentile)&&ref.current_percentile>=0&&ref.current_percentile<=100))){
      flatZero=!!ref.flat_zero;rank=flatZero?0:ref.current_percentile;n=ref.n;
     }
    }else{
     const sorted=idx.baseline,denom=inBase?sorted.length-1:sorted.length;
     if(denom>0){
      if(pr===0&&sorted[sorted.length-1]===0){flatZero=true;n=denom;}
      else{
       let lo=0,hi=sorted.length;
       while(lo<hi){const mid=(lo+hi)>>>1;if(sorted[mid]<pr)lo=mid+1;else hi=mid;}
       const less=lo;hi=sorted.length;
       while(lo<hi){const mid=(lo+hi)>>>1;if(sorted[mid]<=pr)lo=mid+1;else hi=mid;}
       rank=100*(less+0.5*Math.max(0,(lo-less)-(inBase?1:0)))/denom;n=denom;
      }
     }
    }
    const state=flatZero||!isFiniteNumber(rank)?'neutral':n>=100&&rank>=99?'high':n>=20&&rank>=90?'elevated':'neutral';
    const sev=sevOrder[state],candRank=isFiniteNumber(rank)?rank:0;
    if(sev>bestSev||(sev===bestSev&&candRank>bestRank)){
     bestSev=sev;bestRank=candRank;bestState=state;bestN=n;bestSeries=s.name;bestChart=c.title;
    }
   }
   if(bestSev<0){hasMissingAlerts=true;return {state:'missing',rank:null,n:0,seriesName:null,chartTitle:null};}
   return {state:bestState,rank:bestRank,n:bestN,seriesName:bestSeries,chartTitle:bestChart};
  }),
 }));
 if(!hasAnyMeasured)return null;
 return {start,end,window:DAY,times,vectors,totals,maxTotal,maxSeries,missingRates,partialRates,families,
  gauges:{openCircuits,directoryWaiting},relayRangeByTime,
  hasPartialRate,hasAnyMissing:hasMissingRate||hasMissingAlerts||hasMissingGauge};
}

function preparePlot(c,series,range,timeRanges=null){
 const window=DAY,scale=displayScale(c);
 const period=isPlainRecord(range)&&range.window?range:timeRanges
  ?(timeRanges.periods.find(p=>p.id===range)||timeRanges.periods.find(p=>p.id===timeRanges.default_range))
  :STANDALONE_PERIOD_FALLBACKS.find(p=>p.id===range);
 if(!period||!isFiniteNumber(scale)||!validTime(c.end))return null;
 const end=Math.floor(c.end/window)*window;
 let start;
 if(period.window.unit==='all_time'){
  let minSupported=Infinity;
  for(const s of series)for(const p of s.history?.daily||[])if(qualifiedDailyRow(p,scale)&&p[0]<minSupported)minSupported=p[0];
  if(!Number.isFinite(minSupported))return null;
  start=minSupported-window;
 }else start=periodWindowStart(end,period)??NaN;
 if(!validTime(start)||!validTime(end)||end<=start||(end-start)/window>MAX_PLOT_INTERVALS)return null;
 const steps=(end-start)/window,anyMissing=new Uint8Array(steps),missing=[],list=[];
 let hasPlottedValue=false;
 for(let si=0;si<series.length;si++){
  const s=series[si],valuesByTime=new Map(),populations=new Map(),lastSeries=si===series.length-1;
  for(const p of s.history?.daily||[]){
   if(!Array.isArray(p))continue;
   const t=p[0];
   if(!validTime(t)||t<=start||t>end)continue;
   const ok=qualifiedDailyRow(p,scale);
   valuesByTime.set(t,ok?p[1]:null);
   if(ok)populations.set(t,p[2]);else populations.delete(t);
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
function xAxisTicks(start,end,width,left,right){
 if(!validTime(start)||!validTime(end)||end<=start)return [];
 const span=end-start,plotWidth=width-left-right,format=span<=120*DAY?dayFormat:monthFormat;
 const preferred=width<450?2:width>1600?8:width>1100?6:4,maximum=Math.min(preferred,Math.max(1,Math.floor(span/DAY)));
 const startLbl=format.format(new Date(start*1000)),endLbl=format.format(new Date(end*1000));
 for(let cnt=maximum;cnt>=1;cnt--){
  const ticks=[];
  let previousRight=null,readable=true;
  for(let i=0;i<=cnt;i++){
   const t=start+span*i/cnt,label=i===0?startLbl:i===cnt?endLbl:format.format(new Date(t*1000)),anchor=i===0?'start':i===cnt?'end':'middle';
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
 'data-layout','data-all-range','data-hero-range','data-hero-scale','data-hero-hsdir','data-hero-plot',
 'data-series-select','data-band-toggle','data-family','data-child','data-explore','data-copy','data-query-kind','data-methods','data-chart-plot'];
const DISPLAY_RELAY_ROLES=[['guard','guard'],['middle_only','middle-only'],['exit','exit'],['hsdir','HSDir'],['v2dir','V2Dir']];

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
 let plotCache=new WeakMap(),heroCache=new Map(),urlCache=null,lastHeaderH=null,touchPlot=null;
 const plotMeta=new WeakMap(),copyTimers=new WeakMap(),copyRequests=new WeakMap();
const chartCache=c=>{let cache=plotCache.get(c);if(!cache){cache=new Map();plotCache.set(c,cache);}return cache;};
const shown=c=>{const cc=chartCache(c);let out=cc.get('shown');if(!out){out=c.series.filter(s=>c.display_keys.includes(s.key));cc.set('shown',out);}return out;};
const primary=c=>{const cc=chartCache(c);let def=cc.get('primary');if(!def){def=c.series.find(s=>s.key===c.primary_series)||c.series[0];cc.set('primary',def);}return (view==='explorer'&&c.id===chartId&&seriesKey&&shown(c).find(s=>s.key===seriesKey))||def;};
const currentValue=(s,c)=>{
 const current=s.current?.day,value=current?.value;
 return current?.valid===false||!isFiniteNumber(value)||value<0||!isFiniteNumber(value*displayScale(c))?null:value;
};
const formatScaled=(raw,scaled)=>scaled===0&&raw>0?'<1':number(scaled);
function scaledBand(band,n,scale){
 const sLo=scaledProduct(band.p10_per_relay,n,scale),sMid=scaledProduct(band.p50_per_relay,n,scale),sHi=scaledProduct(band.p90_per_relay,n,scale);
 return isFiniteNumber(sLo)&&isFiniteNumber(sMid)&&isFiniteNumber(sHi)?[sLo,sMid,sHi]:null;
}
const currentText=(s,c)=>scaledText(c,currentValue(s,c));
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
function url(v,r=range,f=familyId,c=chartId){
 const cacheable=urlCache&&r===range&&f===familyId&&c===chartId;
 if(cacheable&&urlCache.has(v))return urlCache.get(v);
 const p=new URLSearchParams({view:v,range:r,layout,family:f,chart:c});
 if(c===chartId&&seriesKey&&shown(chart(c)).some(s=>s.key===seriesKey))p.set('series',seriesKey);
 if(c===chartId&&bandEnabled)p.set('band','1');
 if(heroRange==='all')p.set('hero_range','all');
 if(heroScale==='symlog')p.set('hero_scale','symlog');
 if(!heroHsdir)p.set('hero_hsdir','0');
 const out='?'+p.toString();
 if(cacheable)urlCache.set(v,out);
 return out;
}
const remember=()=>{try{window.history.pushState(null,'',url(view,range,familyId,chartId));return true;}catch{window.location.assign(url(view,range,familyId,chartId));return false;}};
const focusDestination=(selector='[data-view-heading]')=>$(selector)?.focus({preventScroll:true});
const announceStatus=message=>{const rgn=$('[data-live-status]');if(rgn)rgn.textContent=message;};
function scrollToFragment(){
 const raw=location.hash?location.hash.slice(1):'';
 if(!raw)return;
 let id;try{id=decodeURIComponent(raw);}catch{id=raw;}
 document.getElementById?.(id)?.scrollIntoView?.({block:'start'});
}
function updateHeaderOffset(){
 const h=$('.nav')?.getBoundingClientRect?.()?.height;
 if(isFiniteNumber(h)&&h>=0&&h!==lastHeaderH){lastHeaderH=h;document.documentElement?.style?.setProperty?.('--header-offset',`${h+16}px`);}
}
const closeNavigation=()=>{$('.nav-links')?.classList.remove('show');$('.nav-toggle')?.setAttribute('aria-expanded','false');updateHeaderOffset();};
function setView(v,r=range,f=familyId,c=chartId,focusSelector){
 if(chartId!==c){seriesKey=null;bandEnabled=false;}
 view=v;familyId=f;chartId=c;range=r;
 if(!remember())return false;
 render();closeNavigation();window.scrollTo?.(0,0);focusDestination(focusSelector);return true;
}
const redrawControl=selector=>{if(remember()){render();focusDestination(selector);}};
function versions(c){
 const v=c.builds,text=typeof v==='string'?v:v?.display||v?.label||D.versions?.by_chart?.[c.id]?.display;
 return text?escapeHtml(text):'Running version not recorded';
}
function snapshotText(){
 if(!release)return `Saved snapshot · ${date(D.end)} ${time(D.end)} UTC<br><span>Daily 00:00 UTC publication planned</span>`;
 const age=(release.now||(()=>Date.now()/1000))()-D.end,notices=[];
 if(release.fallback)notices.push('Showing previous verified release');
 if(age>108000)notices.push('Update delayed');
 if(release.failed)notices.push('Refresh unavailable; retaining verified release');
 return `Observed through ${date(D.end)} ${time(D.end)} UTC<br><span>Generated ${date(release.generatedAt)} ${time(release.generatedAt)} UTC · Updated daily (00:00 UTC)</span>${notices.length?`<br><span>${notices.join(' · ')}</span>`:''}`;
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
 const before={D,release,timeRanges,periodMap,charts,families,range,familyId,chartId,seriesKey,bandEnabled,plotCache,heroCache,html:appEl.innerHTML};
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
  charts=new Map(D.charts.map(c=>[c.id,c]));families=new Map(D.families.map(f=>[f.id,f]));
  if(!charts.has(chartId)){chartId=(families.get(familyId)||D.families[0]).primary;seriesKey=null;bandEnabled=false;}
  familyId=chart(chartId).family_id;
  if(seriesKey&&!shown(chart(chartId)).some(series=>series.key===seriesKey))seriesKey=null;
  render();restorePosition();
 }catch(error){
  ({D,release,timeRanges,periodMap,charts,families,range,familyId,chartId,seriesKey,bandEnabled,plotCache,heroCache}=before);
  try{render();}catch{appEl.innerHTML=before.html;}
  restorePosition();throw error;
 }
}
const subNavigation=()=>`<nav class="subnav" aria-label="OnionSurge views">${[['overview','Overview'],['explorer','Evidence explorer'],['fixes','Tor fixes'],['methods','Methods']].map(([id,label])=>`<a href="${url(id)}" data-view="${id}" class="${view===id?'active':''}" ${view===id?'aria-current="page"':''}>${label}</a>`).join('')}</nav>`;
function relayCountLine(s,period){
 const summary=s.relay_counts?.[period],historical=period!=='day',total=summary?.total??(historical?null:s.current?.day?.relays);
 const countText=value=>isFiniteNumber(value)&&value>=0?number(value):'—';
 if(!isFiniteNumber(total)||total<0)return '<div class="relay-population">• Relays: —</div>';
 let bounded=false;
 const chips=[`<span class="relay-count">${escapeHtml(countText(total)+' total')}</span>`];
 for(const [key,label] of DISPLAY_RELAY_ROLES){
  const exact=summary?.[key],bounds=summary?.role_bounds?.[key];
  let txt='—';
  if(isFiniteNumber(exact)&&exact>=0)txt=number(exact);
  else if(total>0&&Array.isArray(bounds)&&bounds.length===2&&bounds.every(isFiniteNumber)&&bounds[0]>=0&&bounds[0]<=bounds[1]&&bounds[1]<=total){
   bounded=true;const low=cleanZero(Math.floor(bounds[0])),high=cleanZero(Math.ceil(bounds[1]));
   txt=low===high?wholeFormat.format(low):`${wholeFormat.format(low)}–${wholeFormat.format(high)}`;
  }
  chips.push(`<span class="relay-count">${escapeHtml(`${txt} ${label}`)}</span>`);
 }
 const scope=historical?`Mean daily contributing relays across ${countText(summary?.measured_windows)} measured days.`:'Contributing relays in this 24-hour UTC window.';
 const covered=summary?.coverage,coverageLabel=isFiniteNumber(covered)&&covered>=0&&covered<=1?(covered>0&&covered<.0005?'<0.1%':covered<1&&covered>=.9995?'>99.9%':coverageFormat.format(cleanZero(covered))):'';
 const coverage=coverageLabel?` Recorded flag coverage: ${coverageLabel}.`:'';
 const boundsNote=bounded?' Ranges include unclassified relays; endpoints are rounded outward.':'';
 const visibleNote=bounded?`<small class="rank-note">${escapeHtml(`Flag ranges include unclassified relays${coverageLabel?` (${coverageLabel} flag coverage)`:''}.`)}</small>`:'';
 return `<div class="relay-population" title="${escapeHtml(scope+coverage+' Roles use the last recorded flags in each UTC day; flag groups can overlap.'+boundsNote)}">• Relays${historical?' (avg)':''}: ${chips.join(' · ')}</div>${visibleNote}`;
}
const historicalAverageText=(s,key,c)=>{const sm=s.historical_averages?.[key];return sm?.status==='ready'?scaledText(c,sm.value):'—';};
function historicalComparison(c,s,period){
 const key=period.summary_key,summary=s.historical_averages?.[key],comparison=summary?.comparison;
 if(!comparison)return '';
 const state=summary.status!=='ready'?unavailableRank('Insufficient historical coverage'):s.current?.day?.valid!==true||currentValue(s,c)===null?unavailableRank('Insufficient current coverage'):percentile(comparison,period.minimum_comparison_windows);
 const dates=validTime(comparison.start)&&validTime(comparison.end)?` ${date(comparison.start)}–${date(comparison.end)} UTC.`:'';
 const scope=Number.isInteger(comparison.n)&&comparison.n>0?`${count(comparison.n)} qualifying daily intervals.${dates}`:'No qualifying daily intervals.';
 return `<div class="historical-comparison" title="${escapeHtml(scope+' Current 24-hour and historical values are divided by their reporting-relay counts.')}">${percentileDisplay(state,'Current 24h')}</div>`;
}
function historicalStat(c,s,period){
 const key=period.summary_key,summary=s.historical_averages?.[key],ready=summary?.status==='ready';
 const days=summary?`${count(summary.days)} / ${count(summary.expected_days)} days${ready?' measured':' available'}`:'No daily observations';
 const reason=period.window.unit==='all_time'||!(period.minimum_coverage>0)?'No qualifying daily observations are available':`At least ${coverageFormat.format(cleanZero(period.minimum_coverage))} daily coverage is required for ${period.average_label.toLowerCase()}`;
 const narrowed=validTime(summary?.observed_start)&&validTime(summary?.observed_end)&&validTime(summary?.start)&&validTime(summary?.end)&&(summary.observed_start>summary.start||summary.observed_end<summary.end);
 const datesTitle=narrowed?` title="${escapeHtml(`Measured observations: ${date(summary.observed_start)}–${date(summary.observed_end)} UTC.`)}"`:'';
 return `<div class="stat long-term-stat"><div class="stat-label">${escapeHtml(period.average_label)}</div><div class="stat-number" ${ready?'':`title="${escapeHtml(reason)}"`}>${escapeHtml(historicalAverageText(s,key,c))} <small>${escapeHtml(headlineUnit(c,s))}</small></div><div class="average-coverage">${escapeHtml(days)}</div>${validTime(summary?.start)&&validTime(summary?.end)?`<div class="average-dates"${datesTitle}>${date(summary.start)}–${date(summary.end)}</div>`:''}${historicalComparison(c,s,period)}${relayCountLine(s,key)}</div>`;
}
function statPair(c,s){
 const current=s.current?.day||{},ref=s.reference?.day,peak=s.peaks?.day,curVal=currentValue(s,c),currentReady=curVal!==null;
 const state=!currentReady?unavailableRank('Insufficient current coverage'):percentile(ref);
 const referenceText=scaledText(c,ref?.mean_per_relay,current.relays??null);
 const currentPanel=`<div class="stat ${state.className}"><div class="stat-label">24-hour average</div><div class="stat-number">${escapeHtml(scaledText(c,curVal))} <small>${escapeHtml(headlineUnit(c,s))}</small></div><div class="stat-reference" title="Average of available qualifying 24-hour UTC windows per relay within the 18-month reference, scaled to this period’s reporting-relay count.">18-month average: <b>${escapeHtml(referenceText)}</b></div>${isFiniteNumber(peak?.fleet_peak)&&peak.fleet_peak>0?`<div class="stat-peak" title="Highest retained recorded 24-hour fleet value. ${date(peak.fleet_peak_at)} UTC; ${count(peak.fleet_peak_relays)} reporting relays.">Recorded 24h fleet peak: ${escapeHtml(scaledText(c,peak.fleet_peak))}</div>`:''}${percentileDisplay(state)}${currentReady&&peak?.current_is_record?'<small class="record-note">Highest recorded 24h fleet value</small>':''}${relayCountLine(s,'day')}</div>`;
 const dynamicId=timeRanges.card_slots.fixed.includes(range)?timeRanges.card_slots.fallback_dynamic:range;
 return `<div class="stat-pair">${currentPanel}${historicalStat(c,s,periodFor(dynamicId))}${historicalStat(c,s,periodFor(timeRanges.card_slots.fixed[0]))}${historicalStat(c,s,periodFor(timeRanges.card_slots.fixed[1]))}</div>`;
}
function reference(s){
 const day=s.reference?.day;
 if(!Number.isInteger(day?.n)||day.n<=0||!validTime(day.start)||!validTime(day.end)||day.start>day.end)return '<div class="reference"><small>No reference observations yet</small></div>';
 return `<div class="reference"><small>18-month reference: ${date(day.start)}–${date(day.end)} · ${count(day.n)} days</small><small>18-month averages use per-relay history scaled to the current reporting count.</small></div>`;
}
function cachedPlot(c,list,r){
 const cache=chartCache(c),key='plot:'+r+':'+(list.length===1?'1:'+list[0].key:'*');
 if(cache.has(key))return cache.get(key);
 const result=preparePlot(c,list,periodMap.get(r)||r,timeRanges);
 cache.set(key,result);
 return result;
}
const hasBandQuantiles=r=>!!r&&isFiniteNumber(r.p10_per_relay)&&r.p10_per_relay>=0&&isFiniteNumber(r.p50_per_relay)&&r.p50_per_relay>=r.p10_per_relay&&isFiniteNumber(r.p90_per_relay)&&r.p90_per_relay>=r.p50_per_relay;
function bandReference(c,r){
 const selected=primary(c),cache=chartCache(c),cacheKey='band:'+r+':'+selected.key;
 if(cache.has(cacheKey))return cache.get(cacheKey);
 let out=null;
 const scale=displayScale(c),ref=selected.reference?.day;
 if(isFiniteNumber(scale)&&Number.isInteger(ref?.n)&&ref.n>=2&&hasBandQuantiles(ref)&&ref.p90_per_relay>ref.p10_per_relay){
  const p=cachedPlot(c,[selected],r),vals=p?.list[0]?.values,pops=p?.list[0]?.populations;
  if(vals?.some(([t,v])=>{const n=v!=null?pops.get(t):null;return n>0&&!!scaledBand(ref,n,scale);}))out=ref;
 }
 cache.set(cacheKey,out);
 return out;
}
function activeBand(c,r){return view==='explorer'&&c.id===chartId&&bandEnabled?bandReference(c,r):null;}
function plotted(c,r){return activeBand(c,r)?[primary(c)]:shown(c);}
function bandControl(c){
 const ref=bandReference(c,range),active=!!(ref&&view==='explorer'&&c.id===chartId&&bandEnabled),selected=primary(c),raw=selected.reference?.day;
 const hasCount=Number.isInteger(raw?.n)&&raw.n>=0;
 const unavailable=hasCount&&raw.n<2?`${raw.n} reference windows; 2 required`:hasCount&&hasBandQuantiles(raw)&&raw.p10_per_relay===raw.p90_per_relay?'10th and 90th percentiles are equal':'Reference percentile range unavailable';
 return `<div class="band-controls"><span>${escapeHtml(selected.name)}</span><button data-band-toggle aria-pressed="${active}" ${ref?'':'disabled'} class="${active?'active':''}">Percentile band ${active?'on':'off'}</button><small>${ref?(active?'Selected series only · 10th–90th percentiles; dashed median':'Optional: focus this series with its percentile band'):escapeHtml(unavailable)}</small></div>`;
}
function plotLabel(c,r,hasData=true){
 const period=periodFor(r);
 return `${c.title}; ${period?period.plot_label:r}; linear scale`+(hasData?'. Use arrow keys to inspect intervals.':'; No retained samples available for this chart.');
}
function reading(c,s){
 const shownList=shown(c),selectSeries=view==='explorer'&&c.id===chartId&&shownList.length>1;
 const header=selectSeries
  ?`<div class="series-toolbar"><label>Series <select data-series-select aria-label="Series for headline and percentile band">${shownList.map(item=>`<option value="${escapeHtml(item.key)}" ${item.key===s.key?'selected':''}>${escapeHtml(item.name)}</option>`).join('')}</select></label></div>`
  :`<div class="primary-label">${escapeHtml(s.name)}</div>`;
 return `${header}${statPair(c,s)}${reference(s)}`;
}
function figure(c,r){
 const hasBand=!!activeBand(c,r),plottedSeries=hasBand?[primary(c)]:shown(c),hasGaps=!!cachedPlot(c,plottedSeries,r)?.missing.length;
 const legend=`<div class="legend">${plottedSeries.map(s=>`<span><i style="background:${seriesColor(s.color)}"></i>${escapeHtml(s.name)}</span>`).join('')}${hasBand?'<span><i class="band-key"></i>Reference 10th–90th percentiles</span><span><i class="median-key"></i>Reference median</span>':''}${hasGaps?'<span><i class="gap-key"></i>Missing data in one or more series</span>':''}</div>`;
 const notes=[isGaugeKind(c)?'Daily sample means (00:00 UTC).':'Mean rates over 24-hour UTC intervals.'];
 if(c.display_scale===3600)notes.push('Rates are displayed per hour.');
 if(hasGaps)notes.push('Hatching marks missing or insufficiently covered intervals.');
 if(hasBand)notes.push('The band scales with each interval’s reporting-relay count.');
 const foot=`<figcaption><b>Footnote.</b> ${notes.join(' ')}</figcaption><div class="build-note"><span>${escapeHtml(c.metric_origin)}</span> · Latest UTC day builds: ${versions(c)}${c.introduced_note?`<br>${escapeHtml(c.introduced_note)}`:''}</div>`;
 return `<figure><div class="plot-wrap" data-chart-plot="${escapeHtml(c.id)}" data-range="${escapeHtml(r)}" tabindex="0" role="img" aria-label="${escapeHtml(plotLabel(c,r))}"></div>${legend}${foot}</figure>`;
}
const plotQuery=c=>c.history_queries?.daily?.value||c.query;
const coverageQuery=c=>c.history_queries?.daily?.eligible||c.quality_query;
const controlGroup=(lbl,aria,attr,cur,opts)=>`<div class="control-group"><span>${lbl}</span><div class="range-control" role="group" aria-label="${aria}">${opts.map(([v,t])=>`<button ${attr}="${escapeHtml(v)}" class="${cur===v?'active':''}" aria-pressed="${cur===v}">${escapeHtml(t)}</button>`).join('')}</div></div>`;
function heroPlotData(r=heroRange,includeHsdir=heroHsdir){
 const key='r:'+r+':'+(includeHsdir?'1':'0');
 if(heroCache.has(key))return heroCache.get(key);
 const result=prepareHeroPlot(D,r,includeHsdir,heroCache);
 heroCache.set(key,result);
 return result;
}
function heroPlotLabel(r=heroRange,hasData=true,scaleMode=heroScale){
 return `Concurrent DoS activity; ${r==='all'?'All time':'Last 7 days'}; ${scaleMode==='symlog'?'symlog':'linear'} scale`+(hasData?'. Use arrow keys to inspect intervals.':'; No retained samples available for this chart.');
}
function heroCard(){
 const h=heroPlotData(heroRange,heroHsdir),last=h?h.times.length-1:-1;
 const total=last>=0?h.totals[last]:null,latestPartial=last>=0&&h.partialRates[last];
 const open=last>=0?h.gauges.openCircuits.values[last]:null,wait=last>=0?h.gauges.directoryWaiting.values[last]:null;
 const openPeak=h?.gauges.openCircuits.rawPeak??0,waitPeak=h?.gauges.directoryWaiting.rawPeak??0;
 const openShare=peakShare(open,openPeak),waitShare=peakShare(wait,waitPeak);
 const alerts=[];
 let anyAlertMeasured=false,hasHighAlert=false;
 if(last>=0)for(const f of h.families){
  const st=f.rows[last].state;
  if(st!=='missing')anyAlertMeasured=true;
  if(st==='high'||st==='elevated'){alerts.push(f);if(st==='high')hasHighAlert=true;}
 }
 const alertClass=!anyAlertMeasured?'unknown':hasHighAlert?'high':alerts.length?'elevated':'neutral';
 const alertSummary=alerts.length?alerts.slice(0,3).map(f=>`${f.icon} ${f.title}`).join(' · ')+(alerts.length>3?` +${alerts.length-3}`:''):anyAlertMeasured?'No qualifying family alerts':'No measured family series';
 const endDay=h?date(h.end-DAY)+' UTC':validTime(D.end)?date(Math.floor(D.end/DAY)*DAY-DAY)+' UTC':'Unavailable';
 const controls=`<div class="hero-controls">${controlGroup('Window','Concurrent DoS time range','data-hero-range',heroRange,[['7d','7 days'],['all','All time']])}${controlGroup('Flow scale','Concurrent DoS flow scale','data-hero-scale',heroScale,[['linear','Linear'],['symlog','Symlog']])}<button class="hero-toggle ${heroHsdir?'active':''}" data-hero-hsdir aria-pressed="${heroHsdir}">HSDir GET/POST ${heroHsdir?'included':'excluded'}</button></div>`;
 const kpiItems=[
  [heroHsdir?'Latest 24h mitigated &amp; flood rate':'Latest 24h mitigated rate',total!=null?'neutral':'unknown',`${escapeHtml(total!=null?number(total):'Unavailable')} <small>events/s${latestPartial?' · partial':''}</small>`,`${endDay} · ${heroHsdir?'incl. HSDir GET/POST':'excl. HSDir GET/POST'}${latestPartial?' · partial source coverage':''}`],
  ['Concurrent family alerts (24h)',alertClass,`${count(alerts.length)} of ${count(D.families.length)} <small>families ≥90th pct</small>`,alertSummary],
  ['Open circuits (24h mean)',open!=null?'neutral':'unknown',`${escapeHtml(open!=null?number(open):'Unavailable')} <small>circuits</small>`,openShare?`${openShare} of ${axisNumber(openPeak)} peak`:'Normalized to fleet record'],
  ['Stalled BEGIN_DIR (24h mean)',wait!=null?'neutral':'unknown',`${escapeHtml(wait!=null?number(wait):'Unavailable')} <small>connections</small>`,waitShare?`${waitShare} of ${axisNumber(waitPeak)} peak`:'command_wait state'],
 ];
 const kpis=`<div class="hero-kpi-grid">${kpiItems.map(([lbl,cls,val,meta])=>`<div class="hero-kpi"><div class="hero-kpi-label">${lbl}</div><div class="hero-kpi-value ${cls}">${val}</div><div class="hero-kpi-meta">${escapeHtml(meta)}</div></div>`).join('')}</div>`;
 const vecLegend=(h?.vectors||[]).map(v=>`<span><i style="background:${seriesColor(v.color)}"></i>${escapeHtml(v.label)}</span>`).join('');
 const laneKey=D.families.map(f=>`${escapeHtml(f.icon)} ${escapeHtml(f.title)}`).join(' · ');
 const laneSpan=`${escapeHtml(D.families[0]?.icon||'01')}→${escapeHtml(D.families.at(-1)?.icon||'06')}`;
 const legendHtml=`<div class="legend">${vecLegend}<span><i class="alert-elevated-key"></i>≥90th pct family</span><span><i class="alert-high-key"></i>≥99th pct family</span><span><i style="background:#f1d05d"></i>Open circuits (${escapeHtml(openPeak>0?axisNumber(openPeak):'100%')} peak)</span><span><i class="gauge-dashed-key"></i>Stalled BEGIN_DIR (${escapeHtml(waitPeak>0?axisNumber(waitPeak):'100%')} peak)</span>${h?.hasPartialRate?'<span><i class="partial-key"></i>Partial rate coverage</span>':''}${h?.hasAnyMissing?'<span><i class="gap-key"></i>Unmeasured interval</span>':''}<span>Alert lanes (${laneSpan}): ${laneKey}</span></div>`;
 return `<section id="concurrent-dos-overview" class="chart-card hero-card" data-hero-card-range="${escapeHtml(heroRange)}"><div class="card-heading"><div><div class="family-label">All six families · Concurrent DoS overview</div><h2>Concurrent DoS activity</h2></div>${controls}</div><p class="definition">Synchronized daily UTC view across non-overlapping mitigated &amp; flood rates (/s), a 6-family ≥90th/≥99th percentile alert strip, and normalized open-circuit &amp; stalled <code>BEGIN_DIR</code> resource state.</p>${kpis}<figure><div class="plot-wrap hero-plot-wrap" data-hero-plot="concurrent_dos" data-hero-plot-range="${escapeHtml(heroRange)}" tabindex="0" role="img" aria-label="${escapeHtml(heroPlotLabel(heroRange,!!h,heroScale))}"></div>${legendHtml}<figcaption><b>Footnote.</b> Non-overlapping 24-hour UTC intervals (00:00 UTC). Counter sums exclude overlapping sub-series (<code>ntor_v3</code> within <code>ntor</code>; exitward queue hits within <code>circuit_killed_max_cell</code>; HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>). Alert lanes mark families with a qualifying DoS counter at rank ≥90 with at least 20 reference days (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference days (high).</figcaption></figure></section>`;
}
function card(c,f,r){const s=primary(c),relayCount=s.current?.day&&'relays' in s.current.day?s.current.day.relays:c.relays;return `<article id="family-${escapeHtml(f.id)}" class="chart-card" data-family-range="${escapeHtml(r)}"><div class="card-heading"><div><div class="family-label">Family ${escapeHtml(f.icon)} of 06</div><h2>${escapeHtml(f.title)}</h2></div></div><div class="card-summary"><div class="card-context"><div class="metric-subtitle">${metricSubtitle(c,r)}</div><p class="definition">${escapeHtml(c.definition)}</p></div><div class="reading-block">${reading(c,s)}</div></div>${figure(c,r)}<div class="card-actions"><a href="${url('explorer',r,f.id,c.id)}" data-explore="${escapeHtml(f.id)}" data-chart="${escapeHtml(c.id)}" data-range="${escapeHtml(r)}">Explore ${escapeHtml(f.title)} →</a><span>${count(relayCount)} reporting relays</span></div></article>`;}
function chartControls(){return `<div class="overview-controls">${controlGroup('Time range','Chart time range','data-all-range',range,timeRanges.periods.map(p=>[p.id,p.label]))}${view==='overview'?controlGroup('Layout','Chart layout','data-layout',layout,[['grid','Grid'],['full','Full width']]):''}</div>`;}
function overview(){return `<h2 class="sr-only" data-view-heading tabindex="-1">Overview</h2>${heroCard()}<div class="overview-toolbar"><div><h2>Six chart families</h2><p>A percentile shows where a value sits among reference windows. The ruler runs from 0 to 100. <a href="${url('methods')}#reference" data-methods>How it is calculated</a></p></div>${chartControls()}</div><div class="chart-stack chart-${layout}">${D.families.map(f=>card(chart(f.primary),f,range)).join('')}</div>`;}
function explorer(){
 const c=chart(chartId),f=family(c.family_id),s=primary(c),activePeriod=periodFor(range);
 const csvPath=`exports/${encodeURIComponent(c.id)}-${encodeURIComponent(activePeriod.export_suffix)}.csv`;
 const allTimePeriod=timeRanges.periods.find(p=>p.window.unit==='all_time');
 const resolvedCsvPath=release&&!D.time_ranges&&!release.downloads.has(csvPath)&&allTimePeriod
  ?`exports/${encodeURIComponent(c.id)}-${encodeURIComponent(allTimePeriod.export_suffix)}.csv`
  :csvPath;
 const csvLabel=resolvedCsvPath!==csvPath?'CSV · all-time, source units':'CSV · all series, source units';
 return `<div class="explorer-heading"><div><h2 data-view-heading tabindex="-1">Evidence explorer</h2><p><a href="${url('overview')}" data-view="overview">Six chart families</a> / ${escapeHtml(f.title)} / ${escapeHtml(c.title)}</p></div>${chartControls()}</div><div class="explorer-layout"><aside class="family-tree" aria-label="Chart families">${D.families.map(group=>`<section><h3><button data-family="${escapeHtml(group.id)}" class="${group.id===f.id?'active':''}">${escapeHtml(group.icon)} ${escapeHtml(group.title)}</button></h3>${group.children.map(id=>`<a href="${url('explorer',range,group.id,id)}" data-child="${escapeHtml(id)}" class="${id===c.id?'selected':''}">${escapeHtml(chart(id).title)}</a>`).join('')}</section>`).join('')}</aside><article class="explorer-detail"><div class="family-label">${escapeHtml(f.icon)} / ${escapeHtml(f.title)}</div><h2>${escapeHtml(c.title)}</h2><div class="metric-subtitle">${metricSubtitle(c,range)}</div><p class="definition">${escapeHtml(c.definition)}</p>${reading(c,s)}${bandControl(c)}${figure(c,range)}<div class="download-row">${downloadLink(resolvedCsvPath,csvLabel,'explorer-csv')}${downloadLink('data.json','JSON · values & methods','explorer-json')}<a href="${url('explorer',range,f.id,c.id)}" data-focus-key="explorer-permalink">Permalink</a></div><section class="query-section"><div class="query-header"><h3>PromQL · daily plot</h3><button data-copy="${escapeHtml(c.id)}" data-query-kind="plot">Copy</button></div><pre>${escapeHtml(plotQuery(c))}</pre><div class="query-header"><h3>Coverage query</h3><button data-copy="${escapeHtml(c.id)}" data-query-kind="coverage">Copy</button></div><pre>${escapeHtml(coverageQuery(c))}</pre><p>${isGaugeKind(c)?'Per-source sample means are calculated before aggregation.':'Per-source counter rates are calculated before aggregation.'} A source series needs at least 360 samples per 24-hour UTC interval.</p></section><h3>Values by series</h3><div class="table-scroll"><table data-values-table><thead><tr><th>Series</th><th>Unit</th><th>24-hour average</th>${timeRanges.periods.map(p=>`<th>${escapeHtml(p.average_label)}</th>`).join('')}</tr></thead><tbody>${c.series.map(v=>`<tr><td>${escapeHtml(v.name)}</td><td>${escapeHtml(headlineUnit(c,v))}</td><td>${escapeHtml(currentText(v,c))}</td>${timeRanges.periods.map(p=>`<td>${escapeHtml(historicalAverageText(v,p.summary_key,c))}</td>`).join('')}</tr>`).join('')}</tbody></table></div></article></div>`;
}
function fixes(){return `<section class="text-page"><h2 data-view-heading tabindex="-1">Tor fixes &amp; open issues</h2><p>Read-only status snapshot · 18 Nov 2026 UTC.</p><div class="table-scroll"><table><thead><tr><th>Issue or change</th><th>Status</th><th>Tor release</th><th>Source</th></tr></thead><tbody><tr><td>HSDir descriptor-signature log flood</td><td><span class="status-badge status-open">Open</span></td><td>Related fixes in 0.4.9.15</td><td><a href="https://gitlab.torproject.org/tpo/core/tor/-/issues/41339" rel="noopener noreferrer">#41339</a></td></tr><tr><td>Directory request and HSDir outcome metrics</td><td><span class="status-badge status-open">Open</span></td><td>—</td><td><a href="https://gitlab.torproject.org/tpo/core/tor/-/issues/41460" rel="noopener noreferrer">#41460</a></td></tr><tr><td>MetricsPort response under main-loop load</td><td><span class="status-badge status-open">Open</span></td><td>—</td><td><a href="https://gitlab.torproject.org/tpo/core/tor/-/issues/41458" rel="noopener noreferrer">#41458</a></td></tr><tr><td>Connection gauge accounting</td><td><span class="status-badge status-open">Open</span></td><td>—</td><td><a href="https://gitlab.torproject.org/tpo/core/tor/-/issues/41350" rel="noopener noreferrer">#41350</a></td></tr><tr><td>BEGIN_DIR rate/lifetime limits and HSDir request limit</td><td><span class="status-badge status-released">Released</span></td><td>0.4.9.15</td><td><a href="https://forum.torproject.org/t/tor-relays-tor-0-4-9-15-released-new-dos-defenses/22331" rel="noopener noreferrer">Release</a></td></tr><tr><td>Digest filtering, bounded diagnostics and connection indexing</td><td><span class="status-badge status-released">Released</span></td><td>0.4.9.15</td><td><a href="https://gitlab.torproject.org/tpo/core/tor/-/commit/37a6d04b09a0eb6f1c4fc63324ae245d73c9e3d3" rel="noopener noreferrer">Digest filtering</a> · <a href="https://gitlab.torproject.org/tpo/core/tor/-/commit/426731748e094e077c386af5011e0aa59411191e" rel="noopener noreferrer">Diagnostics</a> · <a href="https://gitlab.torproject.org/tpo/core/tor/-/commit/b035c0f5823313d83273d81856803da85b3023d9" rel="noopener noreferrer">Connection lists</a></td></tr></tbody></table></div></section>`;}
function methods(){
 const labels=timeRanges.periods.map(p=>escapeHtml(p.label));
 const rangeList=labels.length>1?`${labels.slice(0,-1).join(', ')}, or ${labels.at(-1)}`:labels[0]||'';
 const fixedLabels=timeRanges.card_slots.fixed.map(id=>escapeHtml(periodFor(id).average_label));
 const fixedRangeLabels=timeRanges.card_slots.fixed.map(id=>escapeHtml(periodFor(id).label)).join(' or ');
 const fallbackLabel=escapeHtml(periodFor(timeRanges.card_slots.fallback_dynamic).average_label);
 const periodRows=timeRanges.periods.map(p=>{
  const pct=coverageFormat.format(cleanZero(p.minimum_coverage));
  const req=p.minimum_coverage>0?`at least ${pct} of days required`:'uses all available qualifying days in the window';
  const desc=p.window.unit==='all_time'
   ?`Mean of every retained qualifying daily interval on a linear scale ending at ${date(D.end)} ${time(D.end)} UTC.`
   :p.window.unit==='days'
    ?`Mean of qualifying daily fleet values across the ${count(p.window.count)}-day window ending at snapshot end (${req}).`
    :`Mean of qualifying daily fleet values over ${count(p.window.count)} calendar ${p.window.count===1?'month':'months'} ending at snapshot end (${req}).`;
  return `<tr><th>${escapeHtml(p.average_label)}</th><td>${desc}</td></tr>`;
 }).join('');
 const laggedPeriods=timeRanges.periods.filter(p=>p.window.comparison_lag_days>0).map(p=>escapeHtml(p.label));
 const lagText=laggedPeriods.length>2?`${laggedPeriods.slice(0,-1).join(', ')}, and ${laggedPeriods.at(-1)}`:laggedPeriods.join(' and ');
 const lagSentence=laggedPeriods.length
  ?`the prior window shifted back by 1 UTC day for ${lagText}, and each period’s own window through the previous UTC midnight otherwise`
  :'that panel’s period through the previous UTC midnight';
 const customThresholds=timeRanges.periods.filter(p=>p.minimum_comparison_windows!==30)
  .map(p=>`${count(p.minimum_comparison_windows)} for ${escapeHtml(p.label)}`);
 return `<section class="text-page"><h2 data-view-heading tabindex="-1">Metrics and percentiles</h2>${release?`<p>Release <code>${escapeHtml(release.id)}</code> · ${downloadLink('manifest.json','Release manifest','methods-manifest')}</p>`:''}<p>The measurements come from Tor MetricsPort across non-overlapping 24-hour UTC intervals (00:00:00 to 00:00:00 UTC). The Overview opens with a <b>Concurrent DoS activity</b> card fixed to its own <b>7 days</b> or <b>All time</b> window, followed by the six chart families controlled by the global time range (${rangeList}) and Grid/Full width layout. The 2×2 summary panel pairs the <b>24-hour average</b> with the active range’s average (defaulting to <b>${fallbackLabel}</b> when viewing ${fixedRangeLabels}) alongside <b>${fixedLabels[0]}</b> and <b>${fixedLabels[1]}</b>. Hover or use arrow keys on any chart to inspect individual UTC days and reporting-relay counts.</p><h3>Concurrent DoS overview</h3><p>The top Overview chart aligns three vertical tiers across non-overlapping <code>86,400s</code> UTC days on either a stacked linear or per-vector symlog rate scale: (1) additive source-unit rates excluding overlapping subsets (<code>ntor_v3</code> within <code>ntor</code>, exitward queue hits within <code>circuit_killed_max_cell</code>, and HSDir outcome breakdowns within <code>BEGIN_DIR</code> <code>hs_get</code>/<code>hs_post</code>, with optional <code>HSDir GET/POST</code> exclusion), (2) a six-family alert strip marking days where a qualifying DoS counter reaches rank ≥90 with at least 20 reference days (elevated, unless the high rule applies) or rank ≥99 with at least 100 reference days (high), and (3) daily fleet means for open circuits and stalled <code>BEGIN_DIR</code> connections normalized to each gauge’s retained fleet record.</p><h3>Headline and historical windows</h3><table><tbody><tr><th>24-hour average</th><td>Latest completed UTC day (<code>86,400s</code>): per-source <code>rate(metric[1d])</code> before summing (<code>avg_over_time(metric[1d])</code> for gauges).</td></tr>${periodRows}</tbody></table><h3>Historical averages &amp; Current 24h</h3><p>${timeRanges.periods.map(p=>`<b>${escapeHtml(p.average_label)}</b>`).join(', ')} are unweighted arithmetic means of the measured daily fleet values in each period; missing days are excluded. These means keep fleet totals, while their <b>Current 24h</b> rulers normalize current and historical daily values per reporting relay.</p><p>Each historical ruler compares the current 24-hour average against non-overlapping qualifying daily values ending at or before the reference cutoff (${lagSentence}), keeping historical averages in their neutral color.</p><h3 id="reference" tabindex="-1">18-month reference</h3><p>The reference contains qualifying 24-hour UTC windows from the prior 18 calendar months, ending at 00:00 UTC on the day before the reference date. Newer metrics use their shorter available record. Dates and day counts appear below each chart.</p><p>The 18-month average is calculated per reporting relay, then scaled to the current reporting count. Peaks use the fleet total recorded at that time.</p><p>Daily references use complete non-overlapping 24-hour UTC windows. Equal values share the middle percentile rank. The rank marker is omitted when both the reference values and the current measurement are zero.</p><p>Current 24-hour values, percentile labels and markers use the same color. Upper-tail colors start at the 90th percentile; the strongest color needs at least 100 reference windows and a rank at or above the 99th percentile. Fewer than 30 windows use a muted color${customThresholds.length?` (${customThresholds.join(', ')})`:''}.</p><h3>Percentile and peak</h3><p>A percentile describes position in the reference distribution. A peak describes magnitude in the full retained record. The reference excludes the current 24-hour window, so even the 100th percentile can be below a newer recorded peak.</p><p><b>Recorded 24h fleet peak</b> uses retained daily observations plus the current 24-hour measurement. The reporting-relay count at each peak is included in its tooltip and data export.</p><h3>Optional percentile band</h3><p>The explorer band focuses on the selected series. Shading covers its 10th-90th percentile range across 24-hour reference days, with a dashed median, across every configured time range.</p><p>Each band interval is scaled by that interval’s reporting-relay count. All time applies the current reference to older observations. The band is off by default; enabling it focuses the selected series and expands the axis to include its reference range.</p><h3>Daily publication and reference updates</h3><p>Collection, reference generation and live releases run once per UTC day after 00:00 UTC on non-overlapping <code>86,400s</code> intervals; sub-24-hour windows are never published.</p><p>Reference date: <b>${escapeHtml(D.reference_window?.date||'Unavailable')}</b>. ${downloadLink(referenceDocument(D.reference_document),'Download the frozen reference','methods-reference')}.</p><h3>Coverage</h3><p>Daily source series require at least 360 samples per 24-hour UTC window. Qualifying historical populations retain at least 90% of the larger of endpoint availability or the preceding 31-day peak. Missing or insufficiently covered intervals stay hatched.</p><h3>Dir-spec coarsening</h3><p>Published telemetry applies deterministic Tor <code>dir-spec</code> coarsening at collection boundaries: standalone public relay, flag, cohort and <code>dircache_cohort</code> counts below 8 are floored to 0 and counts of 8 or more are rounded to the nearest multiple of 8, while partitioned build, cohort and role breakdowns distribute multiples of 8 by largest remainder to sum to the coarsened total (individual parts may differ from nearest-multiple rounding). Open circuit daily means round to the nearest 100 circuits, then to 3 significant digits; onion-service rates (<code>hsdir_fetches</code>, <code>hsdir_uploads</code>, <code>introduction_rejections</code>, and <code>directory_requests</code> <code>hs_get</code>/<code>hs_post</code>) bin to <code>0.001/s</code>, then round to 3 significant digits; other counter classes suppress below 8 events/day, bin to 8-event increments, and round rates to 3 significant digits; and non-sensitive gauge means (<code>directory_waiting</code>) floor below <code>0.05</code> to <code>0</code>, quantize <code>[0.05, 1)</code> to <code>0.1</code>, and round <code>&gt;= 1</code> to 3 significant digits.</p><h3>Relay counts</h3><p>Counts describe contributors to each statistic. Historical counts are daily averages over the same measured days as the metric average. Roles use the last recorded flags within each UTC day; flag groups can overlap. Middle-only means Running with Guard, Exit, HSDir and V2Dir off. Historical counts require an available metric average. Exact role counts require complete flag coverage. With partial flag evidence, ranges include unclassified relays over the same contributing windows; endpoints are rounded outward. Unavailable counts appear as dashes.</p><h3>Tor builds</h3><p><b>Patched Tor</b> means Tor with our additional tor41339 changes; its upstream base version is shown. Build counts describe the latest complete UTC day. Versions come from running-binary records and direct daemon responses linked to process records.</p><h3>Current DirCache 0 cohort</h3><p><b>Middle (DirCache 0)</b> uses configuration and matching flags for the latest complete UTC day. This cohort contains ${count(D.dircache_cohort?.configured_nonexit_zero)} non-exit relays: ${count(D.dircache_cohort?.observed_controlport_zero)} observed effective configurations and ${count(D.dircache_cohort?.file_config_fallback_zero)} explicit configuration-file records.</p><p>${downloadLink('data.json','Download values, queries and provenance →','methods-json')}</p></section>`;
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
const svgXAxisAndFooter=(start,end,width,height,left,right,x)=>{
 let out='';
 for(const {t,label,anchor} of xAxisTicks(start,end,width,left,right))out+=`<text x="${x(t)}" y="${height-(width<450?41:29)}" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
 return out+`<text x="${left}" y="${height-7}" class="source">${SOURCE_CREDIT}</text><text x="${width-right}" y="${height-(width<450?23:7)}" text-anchor="end" class="source">24h intervals · UTC</text></svg>`;
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
 const band=activeBand(c,r),selected=band?primary(c):null,data=cachedPlot(c,band?[selected]:shown(c),r),hasData=!!data;
 el.setAttribute?.('aria-label',plotLabel(c,r,hasData));
 if(!hasData){resetNoData(el,false);return;}
 const width=Math.max(240,Math.round(measuredWidth??el.getBoundingClientRect().width)),height=width>1200?360:300,left=62,right=12,top=26,bottom=52,pw=width-left-right,ph=height-top-bottom,scale=displayScale(c);
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
 el.innerHTML=out+svgXAxisAndFooter(data.start,data.end,width,height,left,right,x);
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
 const n=data.times.length,x=t=>left+(t-data.start)/(data.end-data.start)*pw,symlog=v=>{if(!(v>0))return 0;const s=v/0.01;return isFiniteNumber(s)?Math.log10(1+s):Math.log10(v)+2;};
 const safeR=r==='all'?'all':'7d',patId=`hero-missing-${safeR}-${scaleMode==='symlog'?'s':'l'}-${includeHsdir?1:0}`;
 const x0s=new Array(n),x1s=new Array(n);
 for(let i=0;i<n;i++){const t=data.times[i];x0s[i]=x(t-DAY).toFixed(2);x1s[i]=x(t).toFixed(2);}
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
  const path=stepPathStr(data.times,vals,DAY,x,yFn,1,x0s,x1s);
  if(path)out+=`<path class="${cls}" d="${path}" fill="none" stroke="${color}" stroke-width="${sw}"${dash} stroke-linejoin="round"/>`;
 };
 hatchRuns(i=>data.missingRates[i],rateTop,rateH,'.55');
 const stepW=pw/n,barMode=scaleMode!=='symlog'&&n<=35,pad=barMode?Math.min(8,stepW*0.17):0;
 if(scaleMode==='symlog'){
  for(const vec of data.vectors)stepPath(vec.values,rateY,'hero-rate-series',seriesColor(vec.color),n<=14?2.1:1.6);
 }else if(barMode){
  for(let i=0;i<n;i++){
   if(data.missingRates[i])continue;
   const t=data.times[i],bx=(x(t-DAY)+pad).toFixed(2),bw=Math.max(1,x(t)-x(t-DAY)-2*pad).toFixed(2);
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
   const rx0=pad?(x(data.times[i]-DAY)+pad).toFixed(2):x0s[i],rx1=pad?(x(data.times[j-1])-pad).toFixed(2):x1s[j-1],rw=(+rx1-+rx0).toFixed(2);
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
 el.innerHTML=out+svgXAxisAndFooter(data.start,data.end,width,height,left,right,x);
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
 const t=meta.hero?data.times[clamped]:data.list[0].values[clamped][0],intervalStart=t-DAY;
 const midX=left+((intervalStart+t)/2-data.start)/(data.end-data.start)*pw,header=`${date(intervalStart)} UTC (24h)`;
 let rows,metaLines,announceTitle;
 if(meta.hero){
  rows=data.vectors.map(vec=>{
   const raw=vec.values[clamped],base=raw==null?'Unmeasured':number(raw)+' events/s';
   const extra=vec.id==='queue'&&raw>0&&isFiniteNumber(raw*3600)?` (${number(raw*3600)}/h)`:'';
   return {name:vec.label,color:seriesColor(vec.color),value:base+extra+(raw!=null&&vec.partial?.[clamped]?' (partial)':'')};
  });
  const total=data.totals[clamped];
  rows.push({name:'Total active rate',color:'#00ff7f',value:total==null?'Missing':number(total)+' events/s'+(data.partialRates?.[clamped]?' (partial coverage)':'')});
  metaLines=[];
  const hot=[],unmeasured=[];
  for(const f of data.families){
   const cell=f.rows[clamped];
   if(cell.state==='missing')unmeasured.push(`${f.icon} ${f.title}`);
   else if(cell.state==='high'||cell.state==='elevated')hot.push(`${f.icon} ${f.title} (${cell.state}, ${formatPercentileOrdinal(cell.rank)})`);
  }
  if(unmeasured.length===data.families.length)metaLines.push(`Unmeasured families: ${unmeasured.join(', ')}`);
  else{
   metaLines.push(hot.length?'Alerts ≥90th: '+hot.join(', '):'Alerts ≥90th: No qualifying family alerts');
   if(unmeasured.length)metaLines.push(`Unmeasured families: ${unmeasured.join(', ')}`);
  }
  for(const [lbl,g] of [['Open circuits',data.gauges.openCircuits],['Stalled BEGIN_DIR',data.gauges.directoryWaiting]]){
   const v=g.values[clamped],pct=peakShare(v,g.rawPeak);
   metaLines.push(`${lbl}: ${v==null?'Missing':number(v)}${pct?` (${pct})`:''}`);
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
 for(let i=0;i<heroes.length;i++){drawHero(heroes[i],heroes[i].dataset.heroPlotRange||heroRange,heroScale,heroHsdir,hW[i]);restore(heroes[i],hActive[i]);}
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
 const heroRangeToggle=hit('[data-hero-range]');if(heroRangeToggle){const next=heroRangeToggle.dataset.heroRange==='all'?'all':'7d';if(next!==heroRange){heroRange=next;redrawControl(`[data-hero-range="${heroRange}"]`);}return;}
 const heroScaleToggle=hit('[data-hero-scale]');if(heroScaleToggle){const next=heroScaleToggle.dataset.heroScale==='symlog'?'symlog':'linear';if(next!==heroScale){heroScale=next;redrawControl(`[data-hero-scale="${heroScale}"]`);}return;}
 if(hit('[data-hero-hsdir]')){heroHsdir=!heroHsdir;redrawControl('[data-hero-hsdir]');return;}
 const allToggle=hit('[data-all-range]');if(allToggle){const nextRange=allToggle.dataset.allRange;if(periodMap.has(nextRange)){range=nextRange;redrawControl(`[data-all-range="${range}"]`);}return;}
 const layoutToggle=hit('[data-layout]');if(layoutToggle){const next=layoutToggle.dataset.layout==='grid'?'grid':'full';if(next!==layout){layout=next;redrawControl(`[data-layout="${layout}"]`);}return;}
 if(hit('[data-band-toggle]')){bandEnabled=!bandEnabled;redrawControl('[data-band-toggle]');return;}
 const nav=hit('[data-view]');if(nav){e.preventDefault();if(['overview','explorer','fixes','methods'].includes(nav.dataset.view))setView(nav.dataset.view);return;}
 const ex=hit('[data-explore]');if(ex){e.preventDefault();const f=family(ex.dataset.explore),c=chart(ex.dataset.chart)||(f&&chart(f.primary)),r=periodMap.has(ex.dataset.range)?ex.dataset.range:range;if(c)setView('explorer',r,c.family_id,c.id);return;}
 const child=hit('[data-child]');if(child){e.preventDefault();const c=chart(child.dataset.child);if(c)setView('explorer',range,c.family_id,c.id);return;}
 const fam=hit('[data-family]');if(fam){const f=family(fam.dataset.family);if(f)setView('explorer',range,f.id,f.primary);return;}
 if(hit('[data-methods]')){e.preventDefault();if(!setView('methods'))return;window.history.replaceState(null,'',url('methods')+'#reference');focusDestination('#reference');document.getElementById('reference')?.scrollIntoView({block:'start'});return;}
 const cp=hit('[data-copy]');if(cp){
  const seq=(copyRequests.get(cp)||0)+1;
  copyRequests.set(cp,seq);
  let ok=false;
  try{
   const c=chart(cp.dataset.copy),text=c?(cp.dataset.queryKind==='coverage'?coverageQuery(c):plotQuery(c)):null;
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
 if(select&&shown(chart(chartId)).some(s=>s.key===select.value)){seriesKey=select.value;redrawControl('[data-series-select]');}
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
 render();closeNavigation();scrollToFragment();focusDestination(view==='methods'&&location.hash==='#reference'?'#reference':'[data-view-heading]');
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
 module.exports={metricSelectorTokens,createObservatory,startObservatory,parseLocation,preparePlot,prepareHeroPlot,validateTimeRanges,LEGACY_V5_TIME_RANGES,calendarWindowStart,periodWindowStart,DAY,RESERVED_JSON_KEYS,FORBIDDEN_HOURLY_KEYS,req,isPlainRecord,hasExactKeys,isFiniteNumber,number,ordinal,percentile,niceScale,axisNumber,xAxisTicks};
}else{
 if(document.documentElement.dataset.mode!=='live')startObservatory(window);
}
