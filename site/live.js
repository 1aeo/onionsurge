/* Verified same-origin releases. No monitoring credentials or provider URLs. */
'use strict';

(shared => {
const {DAY,RESERVED_JSON_KEYS,FORBIDDEN_HOURLY_KEYS,LEGACY_V5_TIME_RANGES,validateTimeRanges,calendarWindowStart,isPlainRecord:record,hasExactKeys:exactKeys,isFiniteNumber:finite,periodWindowStart,req:requireLive}=shared;
const LIVE_LIMITS=Object.freeze({rootBytes:65536,manifestBytes:131072,objectBytes:8388608,totalBytes:33554432,concurrency:6,requestMs:25000,candidateMs:60000});
const MAX_JSON_DEPTH=256,MINIMUM_POPULATION_COVERAGE=0.9;
const RELEASE_ID=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/,CHART_ID=/^[a-z][a-z0-9_-]{0,95}$/,SHA256=/^[a-f0-9]{64}$/,COMMIT=/^[a-f0-9]{40}$/,REFERENCE_ID=/^reference-\d{4}-\d{2}-\d{2}-[a-f0-9]{12}$/,HEX_COLOR=/^#[0-9a-f]{6}$/i,ISO_DATE=/^\d{4}-\d{2}-\d{2}$/;
const RELAY_ROLES=Object.freeze(['guard','middle_only','exit','hsdir','v2dir']);
const RELAY_SUMMARY_KEYS=Object.freeze(['total',...RELAY_ROLES,'role_bounds','measured_windows','classified_windows','relay_windows','classified_relay_windows','coverage']);
const timestamp=v=>Number.isSafeInteger(v)&&!Object.is(v,-0)&&v>=0&&v<=8.64e12;
const count=v=>Number.isSafeInteger(v)&&!Object.is(v,-0)&&v>=0;
const coarsenedCount=v=>count(v)&&v%8===0;
const nonNegative=v=>finite(v)&&!Object.is(v,-0)&&v>=0;
const byteBudget=(n,max)=>Number.isSafeInteger(n)&&n>0&&n<=max;
const textField=v=>typeof v==='string';
const matches=(pattern,v)=>typeof v==='string'&&pattern.test(v);
function parseLiveJson(bytes){
 const root=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
 const container=v=>v!==null&&typeof v==='object';
 const validate=(key,v)=>{
  if(typeof key==='string'){requireLive(!RESERVED_JSON_KEYS.has(key),'Reserved JSON key');requireLive(!FORBIDDEN_HOURLY_KEYS.has(key),'Sub-24h field in live JSON');}
  requireLive(typeof v!=='number'||Number.isFinite(v),'Nonfinite JSON number');
 };
 const frame=(key,v,depth)=>{requireLive(depth<=MAX_JSON_DEPTH,'JSON nesting exceeds 256 levels');return {key,value:v,keys:Array.isArray(v)?null:Object.keys(v),index:0,depth};};
 if(!container(root)){validate('',root);return root;}
 const pending=[frame('',root,1)];
 while(pending.length){
  const cur=pending[pending.length-1],keys=cur.keys,arr=cur.value;
  if(keys===null){
   while(cur.index<arr.length){
    const i=cur.index++,v=arr[i];
    if(container(v)){pending.push(frame(i,v,cur.depth+1));break;}
    requireLive(typeof v!=='number'||Number.isFinite(v),'Nonfinite JSON number');
   }
   if(cur.index===arr.length&&pending[pending.length-1]===cur){pending.pop();validate(cur.key,arr);}
  }else{
   while(cur.index<keys.length){
    const k=keys[cur.index++],v=arr[k];
    if(container(v)){pending.push(frame(k,v,cur.depth+1));break;}
    validate(k,v);
   }
   if(cur.index===keys.length&&pending[pending.length-1]===cur){pending.pop();validate(cur.key,arr);}
  }
 }
 return root;
}
function checkDescriptor(value,limits){
 exactKeys(value,['id','manifest_sha256','manifest_bytes'],'Invalid release descriptor');
 requireLive(matches(RELEASE_ID,value.id)&&matches(SHA256,value.manifest_sha256)&&byteBudget(value.manifest_bytes,limits.manifestBytes),'Invalid release descriptor');
}
function checkRoot(value,limits){
 exactKeys(value,['format','updated_at','current','previous'],'Invalid root manifest');
 requireLive(value.format==='onionsurge-root-v1'&&timestamp(value.updated_at),'Unsupported root manifest');
 checkDescriptor(value.current,limits);
 if(value.previous!==null){checkDescriptor(value.previous,limits);requireLive(value.previous.id!==value.current.id,'Duplicate root candidates');}
 return value;
}
const objectPath=p=>p==='data.json'||/^reference\/reference-\d{4}-\d{2}-\d{2}-[a-f0-9]{12}\.json$/.test(p)||/^exports\/(?!.*-(?:24h|1d|[0-9]+[smh])\.csv$)[a-z][a-z0-9_-]{0,95}-[a-z0-9]+(?:-[a-z0-9]+){0,7}\.csv$/.test(p);
function checkManifest(value,descriptor,limits,allowLegacy=false){
 exactKeys(value,['format','id','data_schema_version','observation_end','generated_at','application_commit','deployment_commit','query_contract_sha256','objects'],'Invalid release manifest');
 requireLive(value.format==='onionsurge-release-v1'&&value.id===descriptor.id&&(value.data_schema_version===6||(allowLegacy&&value.data_schema_version===5)),'Unsupported release manifest');
 requireLive(timestamp(value.observation_end)&&value.observation_end%DAY===0&&timestamp(value.generated_at)&&value.generated_at>=value.observation_end&&matches(COMMIT,value.application_commit)&&matches(COMMIT,value.deployment_commit)&&matches(SHA256,value.query_contract_sha256),'Invalid release provenance');
 requireLive(record(value.objects)&&Object.keys(value.objects).length<=512,'Invalid release objects');
 let total=0;
 for(const [path,entry] of Object.entries(value.objects)){
  requireLive(objectPath(path),'Unsafe release object path');
  exactKeys(entry,['sha256','bytes'],'Invalid object descriptor');
  requireLive(matches(SHA256,entry.sha256)&&byteBudget(entry.bytes,limits.objectBytes),'Invalid object descriptor');
  total+=entry.bytes;
 }
 requireLive(total<=limits.totalBytes&&Object.hasOwn(value.objects,'data.json'),'Release exceeds byte budget');
 return value;
}
function rows(value,width,end,start=-1,requireCoarsenedCounts=false){
 requireLive(Array.isArray(value)&&value.length<=100000,'Invalid observation array');
 let previous=-Infinity;
 for(const row of value){
  requireLive(Array.isArray(row)&&row.length===width&&timestamp(row[0])&&row[0]%DAY===0&&row[0]>start&&row[0]<=end&&row[0]>previous,'Invalid observation timestamp');
  requireLive(row[1]===null||nonNegative(row[1]),'Invalid observation value');
  if(width===5){
   requireLive((row[2]===null||(requireCoarsenedCounts?coarsenedCount(row[2]):count(row[2])))&&(row[3]===null||(nonNegative(row[3])&&row[3]<=1))&&typeof row[4]==='boolean','Invalid historical observation');
   if(row[2]===null||row[2]===0)requireLive(row[1]===null&&!row[4],'Invalid historical observation');
   if(row[4])requireLive(row[1]!==null&&row[2]>=8&&row[3]!==null&&row[3]>=MINIMUM_POPULATION_COVERAGE,'Invalid qualified observation');
  }
  previous=row[0];
 }
}
function checkCurrentCoverage(current,chart){
 const {relays,expected_relays:expected,coverage}=current;
 requireLive(coarsenedCount(expected)&&expected>=Math.max(8,chart.relays)&&(relays===null||(coarsenedCount(relays)&&relays<=expected)),'Invalid current denominator');
 const inferred=relays===null?null:relays/expected;
 // Match math.isclose(rel_tol=1e-9, abs_tol=1e-12) in the producer, including threshold check.
 requireLive((coverage===null&&(relays===null||relays===0))||(nonNegative(coverage)&&coverage<=1&&inferred!==null&&Math.abs(coverage-inferred)<=Math.max(1e-9*Math.max(coverage,inferred),1e-12)&&(coverage>=MINIMUM_POPULATION_COVERAGE)===(inferred>=MINIMUM_POPULATION_COVERAGE)),'Invalid current coverage');
 if(relays===null||relays===0||!current.valid)requireLive(current.value===null,'Invalid current eligibility');
 if(current.valid)requireLive(current.value!==null&&relays>=8&&coverage!==null&&coverage>=MINIMUM_POPULATION_COVERAGE&&inferred>=MINIMUM_POPULATION_COVERAGE,'Invalid current eligibility');
}
function checkRelayPopulations(series,end,timeRanges,relayKeys){
 const raw=series.relay_population,history=series.history.daily;
 if(raw!==undefined){
  exactKeys(raw,['method','queries','day','daily'],'Invalid relay population fields');
  requireLive(raw.method==='eligible-last-flags-v1','Invalid relay population method');
  exactKeys(raw.queries,['day'],'Invalid relay population queries');
  requireLive(textField(raw.queries.day)&&raw.queries.day.trim(),'Invalid relay population queries');
  const checkRow=(row,t,n)=>{
   requireLive(Array.isArray(row)&&row.length===8&&!Object.is(row[0],-0)&&row[0]===t&&n!==null&&n!==undefined&&row[1]===n,'Invalid relay population measurement');
   for(let i=1;i<8;i++)requireLive(coarsenedCount(row[i]),'Invalid relay population measurement');
   const c=row[2];
   requireLive(c<=row[1],'Invalid relay population classification');
   for(let i=3;i<8;i++)requireLive(row[i]<=c,'Invalid relay population classification');
   requireLive(row[4]<=c-row[3]&&row[4]<=c-row[5]&&row[4]<=c-row[6]&&row[4]<=c-row[7],'Invalid relay population classification');
  };
  if(raw.day!==null)checkRow(raw.day,end,series.current.day.relays);
  requireLive(Array.isArray(raw.daily)&&raw.daily.length<=history.length,'Invalid daily relay population array');
  let hi=0,previous=-1;
  for(const row of raw.daily){
   requireLive(Array.isArray(row)&&row.length===8&&timestamp(row[0])&&row[0]%DAY===0&&row[0]>previous&&row[0]<=end,'Invalid daily relay population timestamp');
   const t=row[0];
   while(hi<history.length&&history[hi][0]<t)hi++;
   checkRow(row,t,hi<history.length&&history[hi][0]===t?history[hi][2]:undefined);
   previous=t;
  }
 }
 if(series.relay_counts===undefined)return;
 exactKeys(series.relay_counts,relayKeys,'Invalid relay count periods');
 const qTime=[],qN=[],qPop=[],dailyRows=raw?.daily||[],periods=timeRanges.periods;
 for(let i=0,di=0;i<history.length;i++){
  const row=history[i];
  if(!row[4])continue;
  const t=row[0];
  while(di<dailyRows.length&&dailyRows[di][0]<t)di++;
  qTime.push(t);qN.push(row[2]);qPop.push(di<dailyRows.length&&dailyRows[di][0]===t?dailyRows[di]:null);
 }
 const near=(a,b)=>finite(a)&&finite(b)&&(b===0?a===0:Math.abs(a-b)<=4*Number.EPSILON*Math.max(1,Math.abs(a),Math.abs(b))),qLen=qTime.length;
 for(let pi=-1;pi<periods.length;pi++){
  const current=pi<0,periodDef=current?null:periods[pi],period=current?'day':periodDef.summary_key,isAllTime=!current&&periodDef.window.unit==='all_time';
  const summary=series.relay_counts[period];
  exactKeys(summary,RELAY_SUMMARY_KEYS,'Invalid relay count summary fields');
  requireLive(count(summary.measured_windows)&&count(summary.classified_windows)&&count(summary.relay_windows)&&count(summary.classified_relay_windows)&&summary.classified_windows<=summary.measured_windows&&summary.classified_relay_windows<=summary.relay_windows,'Invalid relay count summary counters');
  let winCount=0,lo=0,available=false,total=0,classified=0,complete=0;
  const roles=[0,0,0,0,0];
  const addPop=(row,n)=>{total+=n;if(row){classified+=row[2];if(row[2]===n)complete++;for(let r=0;r<5;r++)roles[r]+=row[r+3];}};
  if(current){
   const n=series.current.day.relays;available=n!==null;
   if(available){winCount=1;addPop(raw?.day,n);}
  }else{
   const average=series.historical_averages[period];
   requireLive((average.status==='ready'||average.status==='insufficient_data')&&count(average.days)&&average.end===end&&(average.start===null||(timestamp(average.start)&&average.start%DAY===0&&average.start<=end))&&(isAllTime||average.start!==null),'Invalid relay population historical window');
   if(!isAllTime){const minT=average.start+DAY;let hi=qLen;while(lo<hi){const m=(lo+hi)>>>1;if(qTime[m]<minT)lo=m+1;else hi=m;}}
   winCount=qLen-lo;available=average.status==='ready';
   requireLive(winCount===average.days,'Relay population historical day count mismatch');
   requireLive(!available||winCount>0,'Invalid ready relay population history');
   for(let k=lo;k<qLen;k++)addPop(qPop[k],qN[k]);
  }
  requireLive(count(total)&&count(classified)&&summary.measured_windows===winCount&&summary.classified_windows===complete&&summary.relay_windows===total&&summary.classified_relay_windows===classified,'Relay population summary evidence mismatch');
  const fullyClassified=winCount>0&&complete===winCount,coverage=total?classified/total:fullyClassified?1:null;
  requireLive(coverage===null?summary.coverage===null:near(summary.coverage,coverage)&&nonNegative(summary.coverage)&&summary.coverage<=1,'Invalid relay population coverage');
  const ready=available&&winCount>0;
  requireLive(ready?(current?summary.total===series.current.day.relays:near(summary.total,total/winCount))&&nonNegative(summary.total)&&summary.total<=Number.MAX_SAFE_INTEGER:summary.total===null,'Invalid relay population total');
  for(let i=0;i<5;i++){
   const role=RELAY_ROLES[i];
   requireLive(ready&&fullyClassified?(current?summary[role]===roles[i]:near(summary[role],roles[i]/winCount))&&nonNegative(summary[role])&&summary[role]<=summary.total:summary[role]===null,'Invalid relay population role summary');
  }
  if(ready&&!fullyClassified&&classified>0&&classified<total){
   exactKeys(summary.role_bounds,RELAY_ROLES,'Invalid relay population bounds fields');
   const unknown=total-classified;
   for(let i=0;i<5;i++){
    const iv=summary.role_bounds[RELAY_ROLES[i]];
    // Exact division prevents tolerance from admitting inward-shifted bounds.
    requireLive(Array.isArray(iv)&&iv.length===2&&nonNegative(iv[0])&&nonNegative(iv[1])&&iv[0]===roles[i]/winCount&&iv[1]===(roles[i]+unknown)/winCount&&iv[0]<=iv[1]&&iv[1]<=summary.total,'Invalid relay population bounds');
   }
  }else requireLive(summary.role_bounds===null,'Unexpected relay population bounds');
  if(ready&&fullyClassified)requireLive(roles[1]<=total-roles[0]&&roles[1]<=total-roles[2]&&roles[1]<=total-roles[3]&&roles[1]<=total-roles[4],'Invalid Middle-only population summary');
 }
}
const f64View=new DataView(new ArrayBuffer(8));
function decomposeFloat(value){
 f64View.setFloat64(0,value,false);
 const bits=f64View.getBigUint64(0,false),rawExp=Number((bits>>52n)&0x7ffn),mant=bits&((1n<<52n)-1n);
 return rawExp===0?[mant,-1074]:[(1n<<52n)|mant,rawExp-1075];
}
function compareDecomposed(decA,decB,decC,decD){
 const sigL=decA[0]*decD[0],expL=decA[1]+decD[1],sigR=decC[0]*decB[0],expR=decC[1]+decB[1],diff=expL-expR;
 if(diff>120)return 1;if(diff<-120)return -1;
 const lhs=diff>=0?sigL<<BigInt(diff):sigL,rhs=diff<0?sigR<<BigInt(-diff):sigR;
 return lhs<rhs?-1:lhs>rhs?1:0;
}
function checkHistoricalSeries(series,periodBounds,data){
 const dayEnd=data.end,cutoff=dayEnd-DAY,current=series.current.day,pLen=periodBounds.length,states=[];
 for(let i=0;i<pLen;i++){
  const [periodDef,periodStart,compStart]=periodBounds[i],average=series.historical_averages[periodDef.summary_key];
  requireLive(record(average)&&textField(average.status),'Invalid historical average');
  if(average.status==='ready')requireLive(nonNegative(average.value)&&textField(average.display),'Invalid ready average');
  const comparison=average.comparison,hasComp=comparison!==undefined;
  if(hasComp){
   exactKeys(comparison,['method','cutoff','n','start','end','current_percentile','flat_zero'],'Invalid historical comparison fields');
   requireLive(comparison.method==='daily-per-relay-midrank-v1'&&!Object.is(comparison.cutoff,-0)&&comparison.cutoff===data.reference_cutoff&&comparison.cutoff===cutoff&&count(comparison.n)&&typeof comparison.flat_zero==='boolean','Invalid historical comparison method or cutoff');
  }
  const hasCurrent=hasComp&&average.status==='ready'&&current.valid;
  states.push({periodDef,average,comparison,hasComp,periodStart,compStart,hasCurrent,days:0,n:0,first:null,start:null,end:null,below:0,equal:0,allZero:true});
 }
 const curVal=current.value,curRelays=current.relays;
 let curDec=null;
 for(const row of series.history.daily){
  if(!row[4])continue;
  const t=row[0],prevT=t-DAY;
  let cmp=2;
  for(let i=0;i<pLen;i++){
   const st=states[i];
   if(st.periodStart===null||prevT>=st.periodStart){st.days++;if(st.first===null)st.first=prevT;}
   if(!st.hasComp||t>cutoff||(st.compStart!==null&&prevT<st.compStart))continue;
   st.n++;if(st.start===null)st.start=prevT;st.end=t;
   if(row[1]!==0)st.allZero=false;
   if(st.hasCurrent){
    if(cmp===2){
     const a=row[1],b=row[2];
     if(a===0||curVal===0)cmp=a===0&&curVal===0?0:a===0?-1:1;
     else if(a===curVal&&b===curRelays)cmp=0;
     else{
      if(a>=1e-100&&a<=1e100&&curVal>=1e-100&&curVal<=1e100&&b<=1e12&&curRelays<=1e12){
       const lhs=a*curRelays,rhs=curVal*b,diff=lhs-rhs,tol=1e-12*(lhs>rhs?lhs:rhs);
       if(diff>tol)cmp=1;else if(diff<-tol)cmp=-1;
      }
      if(cmp===2){curDec??=[decomposeFloat(curVal),decomposeFloat(curRelays)];cmp=compareDecomposed(decomposeFloat(a),decomposeFloat(b),curDec[0],curDec[1]);}
     }
    }
    if(cmp<0)st.below++;else if(cmp===0)st.equal++;
   }
  }
 }
 for(let i=0;i<pLen;i++){
  const st=states[i],{periodDef,average,comparison}=st,isAllTime=periodDef.window.unit==='all_time';
  const expectedStart=isAllTime?st.first:st.periodStart,expectedDays=expectedStart===null?0:(dayEnd-expectedStart)/DAY,expectedCoverage=expectedDays>0?st.days/expectedDays:0,minCov=periodDef.minimum_coverage;
  const ready=st.days>0&&(isAllTime||(expectedDays>0&&st.days/expectedDays>=minCov));
  requireLive(!Object.is(average.start,-0)&&average.start===expectedStart&&!Object.is(average.end,-0)&&average.end===dayEnd&&count(average.days)&&average.days===st.days&&count(average.expected_days)&&average.expected_days===expectedDays&&nonNegative(average.coverage)&&Math.abs(average.coverage-expectedCoverage)<=1e-12&&nonNegative(average.minimum_coverage)&&average.minimum_coverage===minCov&&average.status===(ready?'ready':'insufficient_data'),'Historical comparison parent window mismatch');
  if(!st.hasComp)continue;
  requireLive(count(comparison.n)&&comparison.n===st.n&&!Object.is(comparison.start,-0)&&comparison.start===st.start&&!Object.is(comparison.end,-0)&&comparison.end===st.end,'Historical comparison membership mismatch');
  const flatZero=st.n>0&&st.hasCurrent&&curVal===0&&st.allZero,rank=st.n>0&&st.hasCurrent&&!flatZero?100*(st.below+.5*st.equal)/st.n:null;
  requireLive((comparison.current_percentile===null||!Object.is(comparison.current_percentile,-0))&&comparison.current_percentile===rank&&comparison.flat_zero===flatZero,'Historical comparison rank or zero-state mismatch');
 }
}
function checkData(data,manifest){
 requireLive(record(data)&&(data.schema_version===5||data.schema_version===6)&&data.schema_version===manifest.data_schema_version&&data.window_seconds===DAY&&data.end===manifest.observation_end&&timestamp(data.start)&&timestamp(data.end)&&data.start%DAY===0&&data.end%DAY===0&&data.end-data.start===DAY,'Invalid data window');
 const timeRanges=data.schema_version===6?validateTimeRanges(data.time_ranges):(requireLive(data.time_ranges===undefined,'Unexpected time_ranges in legacy release'),LEGACY_V5_TIME_RANGES);
 for(const k of ['fleet','custom','defense'])if(data[k]!==undefined)requireLive(coarsenedCount(data[k]),'Invalid coarsened data population');
 requireLive(Array.isArray(data.families)&&data.families.length===6&&Array.isArray(data.charts)&&data.charts.length>0&&data.charts.length<=64,'Invalid chart catalogue');
 requireLive(matches(REFERENCE_ID,data.reference_id)&&matches(SHA256,data.reference_sha256)&&data.reference_id.endsWith(data.reference_sha256.slice(0,12))&&data.reference_document==='reference/'+data.reference_id+'.json'&&record(data.reference_window)&&record(data.provenance),'Invalid reference identity');
 for(const [key,msg] of [['reference_method_sha256','Invalid reference identity'],['relay_population_method_sha256','Invalid relay population method identity'],['time_ranges_sha256','Invalid time ranges identity'],['time_ranges_method_sha256','Invalid time ranges method identity']]){
  if(data.provenance[key]!==undefined)requireLive(matches(SHA256,data.provenance[key]),msg);
 }
 requireLive(data.provenance.query_contract_sha256===undefined||(matches(SHA256,data.provenance.query_contract_sha256)&&data.provenance.query_contract_sha256===manifest.query_contract_sha256),'Invalid query contract identity');
 const summaryKeys=[],relayKeys=['day'],periodBounds=[];
 for(const p of timeRanges.periods){
  summaryKeys.push(p.summary_key);relayKeys.push(p.summary_key);
  periodBounds.push([p,periodWindowStart(data.end,p,0),periodWindowStart(data.end,p,p.window.comparison_lag_days)]);
 }
 const chartsById=new Map(),familyIds=new Set(),children=new Set();
 for(const chart of data.charts){
  requireLive(record(chart)&&matches(CHART_ID,chart.id)&&!chartsById.has(chart.id),'Invalid chart ID');chartsById.set(chart.id,chart);
  exactKeys(chart.history_queries,['daily'],'Invalid chart metadata');
  requireLive(matches(CHART_ID,chart.family_id)&&['title','definition','display_unit','query','quality_query','metric_origin'].every(k=>textField(chart[k]))&&Array.isArray(chart.metrics)&&chart.metrics.length>0&&chart.metrics.every(textField)&&(chart.kind==='counter_daily_rate'||chart.kind==='gauge_daily_mean')&&finite(chart.display_scale)&&chart.display_scale>0&&chart.start===data.start&&chart.end===data.end&&coarsenedCount(chart.relays),'Invalid chart metadata');
  requireLive(Array.isArray(chart.series)&&chart.series.length>0&&chart.series.length<=64,'Invalid series array');
  const seriesIds=new Set();
  for(const series of chart.series){
   requireLive(record(series)&&matches(CHART_ID,series.key)&&!seriesIds.has(series.key)&&textField(series.name)&&matches(HEX_COLOR,series.color),'Invalid series metadata');seriesIds.add(series.key);
   requireLive(series.values===undefined&&series.hourly_relays===undefined&&series.hourly_expected_relays===undefined,'Sub-24h series fields are forbidden');
   requireLive(record(series.history),'Invalid history');exactKeys(series.history,['daily'],'Invalid history periods');rows(series.history.daily,5,data.end,-1,true);
   requireLive(record(series.current)&&record(series.reference)&&record(series.peaks)&&record(series.historical_averages),'Missing series summaries');
   exactKeys(series.current,['day'],'Invalid current periods');exactKeys(series.reference,['day'],'Invalid reference periods');exactKeys(series.peaks,['day'],'Invalid peak periods');
   const current=series.current.day,reference=series.reference.day,peak=series.peaks.day;
   requireLive(record(current)&&typeof current.valid==='boolean'&&(current.value===null||nonNegative(current.value))&&(current.relays===null||coarsenedCount(current.relays))&&record(reference)&&count(reference.n)&&(reference.current_percentile===null||(nonNegative(reference.current_percentile)&&reference.current_percentile<=100))&&(peak===null||record(peak)),'Invalid current/reference summary');
   checkCurrentCoverage(current,chart);
   if(series.history.daily.length){
    const last=series.history.daily[series.history.daily.length-1];
    if(last[0]===data.end)requireLive(((current.value===null&&last[1]===null)||(current.value!==null&&last[1]!==null&&Math.abs(current.value-last[1])<=Math.max(1e-12*Math.max(Math.abs(current.value),Math.abs(last[1])),1e-12)))&&current.relays===last[2]&&current.valid===last[4]&&(current.coverage===null||last[3]===null||Math.abs(current.coverage-last[3])<=1e-12),'Headline and plotted final day disagree');
   }
   exactKeys(series.historical_averages,summaryKeys,'Invalid historical periods');
   checkHistoricalSeries(series,periodBounds,data);
   if(series.relay_population!==undefined||series.relay_counts!==undefined)checkRelayPopulations(series,data.end,timeRanges,relayKeys);
  }
  const displaySet=new Set();
  requireLive(Array.isArray(chart.display_keys)&&chart.display_keys.length>0,'Invalid displayed series');
  for(const k of chart.display_keys){requireLive(seriesIds.has(k)&&!displaySet.has(k),'Invalid displayed series');displaySet.add(k);}
  requireLive(displaySet.has(chart.primary_series),'Invalid displayed series');
 }
 for(const family of data.families){
  requireLive(record(family)&&matches(CHART_ID,family.id)&&!familyIds.has(family.id)&&textField(family.title)&&textField(family.icon)&&Array.isArray(family.children)&&family.children.length>0&&family.children.includes(family.primary),'Invalid family');
  familyIds.add(family.id);
  for(const id of family.children){requireLive(chartsById.has(id)&&!children.has(id)&&chartsById.get(id).family_id===family.id,'Invalid family membership');children.add(id);}
 }
 requireLive(children.size===chartsById.size,'Incomplete families');
 const exportSuffixes=data.schema_version===5?['30d','all-time']:timeRanges.periods.map(p=>p.export_suffix),required=new Set(['data.json',data.reference_document]);
 for(const c of data.charts)for(const suffix of exportSuffixes){const p=`exports/${c.id}-${suffix}.csv`;requireLive(!required.has(p),'Incomplete release object set');required.add(p);}
 requireLive(required.size===Object.keys(manifest.objects).length,'Incomplete release object set');
 for(const p of required)requireLive(Object.hasOwn(manifest.objects,p),'Incomplete release object set');
 requireLive(manifest.objects[data.reference_document].sha256===data.reference_sha256,'Reference digest mismatch');
 return data;
}
function checkReference(reference,data){
 requireLive(record(reference)&&record(reference.window)&&record(reference.distributions)&&(reference.method_sha256===undefined||matches(SHA256,reference.method_sha256))&&(reference.source_input_sha256===undefined||matches(SHA256,reference.source_input_sha256))&&(data.provenance.reference_method_sha256===undefined||reference.method_sha256===data.provenance.reference_method_sha256)&&timestamp(reference.source_snapshot_end)&&reference.source_snapshot_end<=data.end,'Invalid frozen reference');
 const keys=['date','start','end','months','next_refresh','method_version'];
 exactKeys(reference.window,keys,'Invalid reference window');exactKeys(data.reference_window,keys,'Invalid data reference window');
 requireLive(keys.every(k=>reference.window[k]===data.reference_window[k])&&matches(ISO_DATE,reference.window.date)&&reference.window.months===18&&timestamp(reference.window.next_refresh)&&textField(reference.window.method_version)&&timestamp(reference.window.start)&&timestamp(reference.window.end)&&reference.window.start%DAY===0&&reference.window.end%DAY===0&&reference.window.next_refresh%DAY===0&&reference.window.start<reference.window.end&&reference.window.end<=data.start&&data.reference_cutoff===reference.window.end,'Reference window mismatch');
 requireLive(Object.keys(reference.distributions).length===data.charts.length,'Reference catalogue mismatch');
 for(const chart of data.charts){
  const group=reference.distributions[chart.id];
  requireLive(record(group)&&Object.keys(group).length===chart.series.length,'Reference series mismatch');
  for(const series of chart.series){
   const periods=group[series.key];requireLive(record(periods),'Missing reference series');
   exactKeys(periods,['day'],'Invalid reference period map');rows(periods.day,2,reference.window.end,reference.window.start);
   requireLive(periods.day.length===series.reference.day.n,'Reference count mismatch');
  }
 }
}
function aborted(signal){if(signal?.aborted)throw new Error('Release request cancelled');}
async function deadline(operation,milliseconds,parent){
 const controller=new AbortController(),cancel=()=>controller.abort();
 if(parent?.aborted)controller.abort();else parent?.addEventListener('abort',cancel,{once:true});
 const cancelled=new Promise((_,reject)=>{const fail=()=>reject(new Error('Release request timed out or cancelled'));if(controller.signal.aborted)fail();else controller.signal.addEventListener('abort',fail,{once:true});});
 const timer=setTimeout(cancel,milliseconds);
 let ok=false;
 try{const out=await Promise.race([Promise.resolve().then(()=>{aborted(controller.signal);return operation(controller.signal);}),cancelled]);ok=true;return out;}
 finally{clearTimeout(timer);parent?.removeEventListener('abort',cancel);if(!ok)controller.abort();}
}
const cancelBody=body=>{if(body)body.cancel().catch(()=>{});};
async function readBounded(response,maximum,signal){
 requireLive(response.body,'Empty response body');
 const reader=response.body.getReader(),parts=[],cancel=()=>reader.cancel().catch(()=>{});
 let size=0;
 signal.addEventListener('abort',cancel,{once:true});
 try{
  while(true){
   aborted(signal);const {done,value}=await reader.read();aborted(signal);if(done)break;
   requireLive(value instanceof Uint8Array,'Invalid response chunk');size+=value.byteLength;
   requireLive(size<=maximum,'Response exceeds byte budget');parts.push(value);
  }
  const bytes=new Uint8Array(size);let offset=0;
  for(const part of parts){bytes.set(part,offset);offset+=part.byteLength;}
  return bytes;
 }finally{signal.removeEventListener('abort',cancel);cancel();}
}
const sameDescriptor=(l,r)=>!!l&&!!r&&l.id===r.id&&l.manifest_sha256===r.manifest_sha256&&l.manifest_bytes===r.manifest_bytes;
function createReleaseLoader({baseUrl,fetch:fetcher,subtle,limits=LIVE_LIMITS}){
 const base=new URL(baseUrl);
 requireLive((base.protocol==='http:'||base.protocol==='https:')&&base.pathname.endsWith('/')&&!base.username&&!base.password&&!base.search&&!base.hash,'Invalid public data base');
 async function verifyBytes(value,expected,signal){
  requireLive(value.byteLength===expected.bytes,'Object byte count mismatch');aborted(signal);
  let hash='';
  for(const b of new Uint8Array(await subtle.digest('SHA-256',value)))hash+=b.toString(16).padStart(2,'0');
  aborted(signal);requireLive(hash===expected.sha256,'Object checksum mismatch');
  return value;
 }
 async function bytes(path,maximum,signal,expected=null,cache='force-cache'){
  return deadline(async requestSignal=>{
   const url=new URL(path,base);
   requireLive(url.origin===base.origin&&url.pathname.startsWith(base.pathname),'Unsafe request URL');
   const response=await fetcher(url.href,{signal:requestSignal,credentials:'omit',mode:'same-origin',redirect:'error',cache});
   if(requestSignal.aborted){cancelBody(response.body);aborted(requestSignal);}
   if(response.status!==200||response.redirected||(response.url&&response.url!==url.href)){cancelBody(response.body);throw new Error('Release object unavailable');}
   const value=await readBounded(response,Math.min(maximum,expected?.bytes??maximum),requestSignal);
   if(expected)await verifyBytes(value,expected,requestSignal);
   aborted(requestSignal);return value;
  },limits.requestMs,signal);
 }
 async function objectBytes(path,manifest,prefix,signal,active){
  const expected=manifest.objects[path],prior=active?.manifest?.objects?.[path],cached=active?.objects instanceof Map?active.objects.get(path):null;
  if(prior?.sha256===expected.sha256&&prior?.bytes===expected.bytes&&cached instanceof Uint8Array&&cached.byteLength===expected.bytes){
   try{return await verifyBytes(new Uint8Array(cached),expected,signal);}catch{aborted(signal);}
  }
  return bytes(prefix+path,limits.objectBytes,signal,expected);
 }
 async function candidate(descriptor,parent,fallback,active=null){
  return deadline(async signal=>{
   const prefix=`releases/${descriptor.id}/`,manifestBytes=await bytes(prefix+'manifest.json',limits.manifestBytes,signal,{sha256:descriptor.manifest_sha256,bytes:descriptor.manifest_bytes});
   const manifest=checkManifest(parseLiveJson(manifestBytes),descriptor,limits,fallback),dataBytes=await objectBytes('data.json',manifest,prefix,signal,active);
   const data=checkData(parseLiveJson(dataBytes),manifest),objects=new Map([['data.json',dataBytes]]);
   const pending=Object.keys(manifest.objects).filter(p=>p!=='data.json');let cursor=0;
   await Promise.all(Array.from({length:Math.min(limits.concurrency,pending.length)},async()=>{while(cursor<pending.length){const p=pending[cursor++];objects.set(p,await objectBytes(p,manifest,prefix,signal,active));}}));
   aborted(signal);checkReference(parseLiveJson(objects.get(data.reference_document)),data);
   return {descriptor,manifest,manifestBytes,data,objects,fallback};
  },limits.candidateMs,parent);
 }
 return {async load(active=null,signal){
  const root=checkRoot(parseLiveJson(await bytes('manifest.json',limits.rootBytes,signal,null,'no-store')),limits);
  if(sameDescriptor(root.current,active?.descriptor)&&active?.manifest?.data_schema_version===6)return {...active,fallback:false};
  try{return await candidate(root.current,signal,false,active);}
  catch(error){aborted(signal);if(active||root.previous===null)throw error;return candidate(root.previous,signal,true);}
 }};
}
function releaseResources(bundle,browser){
 const downloads=new Map(),dispose=()=>{for(const url of downloads.values()){try{browser.URL.revokeObjectURL(url);}catch{}}downloads.clear();};
 const add=(path,bytes)=>downloads.set(path,browser.URL.createObjectURL(new browser.Blob([bytes],{type:path.endsWith('.csv')?'text/csv;charset=utf-8':'application/json;charset=utf-8'})));
 try{for(const [path,bytes] of bundle.objects)add(path,bytes);add('manifest.json',bundle.manifestBytes);return {downloads,dispose};}
 catch(error){dispose();throw error;}
}
const showUnavailable=b=>{b.document.getElementById('app').textContent='Verified measurements are unavailable. Please reload this page.';};
function createLiveController(browser,makeApp,options={}){
 const loader=options.loader||createReleaseLoader({baseUrl:new URL('data/',browser.location.href).href,fetch:browser.fetch.bind(browser),subtle:browser.crypto.subtle});
 const now=options.now||(()=>Date.now()/1000),abort=new AbortController(),refreshMs=3600000;
 let active=null,app=null,resources=null,retired=null,inFlight=null,stopped=false,lastAttempt=-Infinity,failed=false,queuedRefresh=false;
 const context=bundle=>({mode:'live',id:bundle.descriptor.id,generatedAt:bundle.manifest.generated_at,fallback:bundle.fallback,downloads:resources.downloads,now});
 function refresh(){
  if(stopped)return Promise.resolve(false);
  if(inFlight)return inFlight;
  lastAttempt=now();
  return inFlight=Promise.resolve().then(async()=>{
   let nextResources=null;
   try{
    const next=await loader.load(active,abort.signal);if(stopped)return false;
    if(!sameDescriptor(next.descriptor,active?.descriptor)){
     nextResources=releaseResources(next,browser);const previousResources=resources;resources=nextResources;
     try{if(app)app.replaceRelease(next.data,context(next));else{const nextApp=makeApp(next.data,browser,context(next));nextApp.start();app=nextApp;}}
     catch(error){resources=previousResources;throw error;}
     retired?.dispose();retired=previousResources;nextResources=null;
    }
    active=next;failed=false;app.updateFreshness({fallback:next.fallback,failed:false});return true;
   }catch{
    nextResources?.dispose();if(stopped)return false;failed=true;
    if(app)app.updateFreshness({failed:true});else showUnavailable(browser);
    return false;
   }finally{
    inFlight=null;
    if(queuedRefresh){queuedRefresh=false;if(!stopped)refresh();}
   }
  });
 }
 const visible=()=>browser.document.visibilityState!=='hidden',onVisible=()=>{if(visible()&&now()-lastAttempt>=refreshMs/1000)refresh();};
 const refreshTimer=browser.setInterval(()=>{if(visible())refresh();},refreshMs),ageTimer=browser.setInterval(()=>{if(visible())app?.updateFreshness({failed});},60000);
 const onPageHide=e=>{if(!e.persisted)stop();};
 const onPageShow=e=>{
  if(stopped||!e.persisted||!visible())return;
  app?.updateFreshness({failed});
  if(now()-lastAttempt>=refreshMs/1000){if(inFlight)queuedRefresh=true;else refresh();}
 };
 browser.document.addEventListener('visibilitychange',onVisible);
 browser.addEventListener('pagehide',onPageHide);
 browser.addEventListener('pageshow',onPageShow);
 function stop(){if(stopped)return;stopped=true;queuedRefresh=false;abort.abort();browser.clearInterval(refreshTimer);browser.clearInterval(ageTimer);browser.document.removeEventListener('visibilitychange',onVisible);browser.removeEventListener?.('pagehide',onPageHide);browser.removeEventListener?.('pageshow',onPageShow);resources?.dispose();retired?.dispose();}
 return {refresh,stop,getState:()=>({releaseId:active?.descriptor.id||null,fallback:active?.fallback||false,failed,inFlight:!!inFlight})};
}
function startLiveObservatory(browser,makeApp){
 if(browser.document.documentElement?.dataset.mode!=='live')return null;
 try{const controller=createLiveController(browser,makeApp);controller.refresh();return controller;}
 catch{showUnavailable(browser);return null;}
}
if(typeof module!=='undefined'&&module.exports){
 module.exports={LIVE_LIMITS,MAX_JSON_DEPTH,parseLiveJson,createReleaseLoader,createLiveController,startLiveObservatory,checkData,checkReference,validateTimeRanges,LEGACY_V5_TIME_RANGES,calendarWindowStart};
}else startLiveObservatory(window,createObservatory);
})(typeof module!=='undefined'&&module.exports?require('./app.js'):{DAY,RESERVED_JSON_KEYS,FORBIDDEN_HOURLY_KEYS,LEGACY_V5_TIME_RANGES,validateTimeRanges,calendarWindowStart,isPlainRecord,hasExactKeys,isFiniteNumber,periodWindowStart,req});
