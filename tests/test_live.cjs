'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {tmpdir}=require('node:os');
const {spawnSync}=require('node:child_process');
const {createHash,webcrypto}=require('node:crypto');
const {LIVE_LIMITS,MAX_JSON_DEPTH,parseLiveJson,createReleaseLoader,createLiveController,startLiveObservatory,checkData,checkReference,validateTimeRanges,LEGACY_V5_TIME_RANGES,calendarWindowStart}=require('../site/live.js');
const folder=path.join(__dirname,'../dist');
const source=JSON.parse(fs.readFileSync(path.join(folder,'data.json')));
const originalReference=JSON.parse(fs.readFileSync(path.join(folder,source.reference_document)));
const encode=value=>Buffer.from(JSON.stringify(value)+'\n');
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const entry=bytes=>({sha256:hash(bytes),bytes:bytes.length});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const baseUrl='https://example.test/onionsurge/data/';

test('native parsing and postorder validation preserve the prior accepted values and errors',()=>{
 function previousParser(bytes){
  return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes),(key,value)=>{
   if(['__proto__','constructor','prototype'].includes(key))throw new Error('Reserved JSON key');
   if(typeof value==='number'&&!Number.isFinite(value))throw new Error('Nonfinite JSON number');
   return value;
  });
 }
 const texts=['null','true','false','0','-0','1e-400','1e400','-1e400','"Infinity"','"__proto__"',
  '{"constructor":1}','{"prototype":null}','{"__proto__":{"x":1}}','{"items":[{"constructor":{}}]}',
  String.raw`{"\u005f\u005fproto\u005f\u005f":1}`,'{"x":[1,2,1e400]}','{"x":1e400,"x":1}',
  '{"x":1,"x":1e400}','{"constructor":{"x":1e400}}','{"x":{"prototype":1},"constructor":2}',
  String.raw`{"x":"\ud800"}`,'{','[1,]','{"x":NaN}','undefined'];
 const cases=[...texts.map(text=>Buffer.from(text)),Buffer.from([255]),Buffer.from([195,40]),
  Buffer.concat([Buffer.from([239,187,191]),Buffer.from('{"x":1}')])];
 const outcome=(parse,bytes)=>{try{return {accepted:true,value:parse(bytes)};}catch(error){return {accepted:false,name:error.name,message:error.message};}};
 assert.equal(cases.length,28);
 for(const bytes of cases)assert.deepEqual(outcome(parseLiveJson,bytes),outcome(previousParser,bytes));
 assert.deepEqual(parseLiveJson(Buffer.from('{"x":1e400,"x":1}')),{x:1},'native duplicate-key last-value semantics stay unchanged');
 assert.throws(()=>parseLiveJson(Buffer.from('{"constructor":{"x":1e400}}')),/^Error: Nonfinite JSON number$/,
  'a child value is checked before the reserved parent key');
 assert.throws(()=>parseLiveJson(Buffer.from('{"2":1e400,"1":{"prototype":0}}')),/^Error: Reserved JSON key$/,
  'integer own keys retain native enumeration order');
 assert.deepEqual(parseLiveJson(fs.readFileSync(path.join(folder,'data.json'))),source);
 assert.deepEqual(parseLiveJson(fs.readFileSync(path.join(folder,source.reference_document))),originalReference);
});

test('JSON container nesting has an explicit 256-level limit without charging primitive leaves',()=>{
 assert.equal(MAX_JSON_DEPTH,256);
 for(const [open,close] of [['[',']'],['{"x":','}']]){
  const text=open.repeat(MAX_JSON_DEPTH)+'0'+close.repeat(MAX_JSON_DEPTH);
  assert.deepEqual(parseLiveJson(Buffer.from(text)),JSON.parse(text));
  const tooDeep=open.repeat(MAX_JSON_DEPTH+1)+'0'+close.repeat(MAX_JSON_DEPTH+1);
  assert.throws(()=>parseLiveJson(Buffer.from(tooDeep)),/^Error: JSON nesting exceeds 256 levels$/);
 }
 const emptyAtLimit='['.repeat(MAX_JSON_DEPTH-1)+'[]'+']'.repeat(MAX_JSON_DEPTH-1);
 assert.deepEqual(parseLiveJson(Buffer.from(emptyAtLimit)),JSON.parse(emptyAtLimit));
 assert.equal(parseLiveJson(Buffer.from('0')),0);
});

test('wide JSON validates every primitive within a constrained parser heap',()=>{
 const directory=fs.mkdtempSync(path.join(tmpdir(),'onionsurge-json-width-'));
 const code=String.raw`
  const assert=require('node:assert/strict');
  const parse=require(process.argv[1]).parseLiveJson;
  const count=1000000;
  const bytes=Buffer.from('['+'0,'.repeat(count-1)+'0]');
  const value=parse(bytes);
  assert.equal(bytes.length,2000001);
  assert.equal(value.length,count);
  assert.ok(value.every(item=>item===0));
  assert.throws(()=>parse(Buffer.from('['+'0,'.repeat(count-1)+'1e400]')),/^Error: Nonfinite JSON number$/);
  console.log('wide-parser-ok');
 `;
 try{
  const result=spawnSync(process.execPath,['--max-old-space-size=96','-e',code,require.resolve('../site/live.js')],
   {cwd:directory,encoding:'utf8',timeout:15000,maxBuffer:65536,
    env:{PATH:process.env.PATH||'',HOME:directory,TMPDIR:directory}});
  assert.equal(result.error,undefined);
  assert.equal(result.status,0,result.stderr);
  assert.equal(result.stdout.trim(),'wide-parser-ok');
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
});

function periodStartTs(dayEnd,period){
 if(period.window.unit==='days')return dayEnd-period.window.count*86400;
 if(period.window.unit==='months')return calendarWindowStart(dayEnd,period.window.count,0);
 return null;
}

function fixture(id,{dataChange,referenceChange,manifestChange,objectChange,exportSources={}}={}){
 const data=structuredClone(source),reference=structuredClone(originalReference);
 referenceChange?.(reference);
 const referenceBytes=encode(reference),referenceHash=hash(referenceBytes);
 data.reference_sha256=referenceHash;
 data.reference_id='reference-'+data.reference_window.date+'-'+referenceHash.slice(0,12);
 data.reference_document='reference/'+data.reference_id+'.json';
 dataChange?.(data);
 const objects=new Map([['data.json',encode(data)],[data.reference_document,referenceBytes]]);
 const exportSuffixes=data.schema_version===5?['30d','all-time']:data.time_ranges.periods.map(p=>p.export_suffix);
 for(const chart of data.charts)for(const range of exportSuffixes){
  const file='exports/'+chart.id+'-'+range+'.csv';
  const originalPath=path.join(folder,'exports/'+(exportSources[chart.id]||chart.id)+'-'+range+'.csv');
  const fallbackPath=path.join(folder,'exports/'+(exportSources[chart.id]||chart.id)+'-all-time.csv');
  objects.set(file,fs.existsSync(originalPath)?fs.readFileSync(originalPath):fs.readFileSync(fallbackPath));
 }
 objectChange?.(objects);
 const manifest={format:'onionsurge-release-v1',id,data_schema_version:data.schema_version,observation_end:data.end,
  generated_at:data.end+3600,application_commit:'a'.repeat(40),deployment_commit:'b'.repeat(40),
  query_contract_sha256:data.provenance?.query_contract_sha256||'c'.repeat(64),objects:Object.fromEntries([...objects].map(([name,bytes])=>[name,entry(bytes)]))};
 manifestChange?.(manifest);
 const manifestBytes=encode(manifest);
 return {data,reference,objects,manifest,manifestBytes,descriptor:{id,manifest_sha256:hash(manifestBytes),manifest_bytes:manifestBytes.length}};
}
const defaultObjectCount=2+source.charts.length*source.time_ranges.periods.length;
const current=fixture('current'),previous=fixture('previous');
function server(candidates=[current,previous],selected=current,prior=previous){
 const files=new Map(),calls=[];
 for(const item of candidates){
  const prefix='releases/'+item.descriptor.id+'/';files.set(prefix+'manifest.json',item.manifestBytes);
  for(const [name,bytes] of item.objects)files.set(prefix+name,bytes);
 }
 let root={format:'onionsurge-root-v1',updated_at:source.end+3600,current:structuredClone(selected.descriptor),previous:prior?structuredClone(prior.descriptor):null};
 const fetch=async(url,options)=>{
  assert.equal(new URL(url).origin,'https://example.test');
  assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');assert.equal(options.mode,'same-origin');
  const key=url.slice(baseUrl.length);calls.push({key,options});
  if(key==='manifest.json'){assert.equal(options.cache,'no-store');return new Response(encode(root));}
  return new Response(files.get(key)||'missing',{status:files.has(key)?200:404});
 };
 return {files,calls,fetch,get root(){return root;},set root(value){root=value;}};
}
const loader=(storage,overrides={})=>createReleaseLoader({baseUrl,fetch:storage.fetch,subtle:webcrypto.subtle,...overrides});
function withoutPopulation(series){delete series.relay_population;delete series.relay_counts;}
function withoutComparisons(series){for(const average of Object.values(series.historical_averages))delete average.comparison;}
function withBoundedMinimumCoverage(data,minCov=0.9){
 for(const period of data.time_ranges.periods)if(period.window.unit==='days'||period.window.unit==='months')period.minimum_coverage=minCov;
 for(const chart of data.charts)for(const series of chart.series){withoutPopulation(series);syncHistoricalAverages(series,data.time_ranges.periods,data.end);}
 return data;
}
function syncHistoricalAverages(series,periodDefs,end){
 const dayEnd=Math.floor(end/86400)*86400;
 const validDaily=series.history.daily.filter(row=>row[4]&&row[0]<=dayEnd);
 const next={};
 for(const periodDef of periodDefs){
  if(periodDef.window.unit==='hours')continue;
  const key=periodDef.summary_key,isAllTime=periodDef.window.unit==='all_time';
  const pStart=isAllTime?null:periodStartTs(dayEnd,periodDef);
  const points=pStart===null?validDaily:validDaily.filter(row=>row[0]-86400>=pStart);
  const start=isAllTime?(points.length?points[0][0]-86400:null):pStart;
  const expectedDays=start===null?0:(dayEnd-start)/86400;
  const days=points.length;
  const coverage=expectedDays>0?days/expectedDays:0;
  const ready=days>0&&(isAllTime||(expectedDays>0&&coverage>=periodDef.minimum_coverage));
  const value=ready?points.reduce((sum,row)=>sum+row[1],0)/days:null;
  const prev=series.historical_averages?.[key];
  const item={
   ...(prev||{}),
   value,
   status:ready?'ready':'insufficient_data',
   days,
   expected_days:expectedDays,
   coverage,
   minimum_coverage:periodDef.minimum_coverage,
   start,
   end:dayEnd,
   observed_start:points.length?points[0][0]-86400:null,
   observed_end:points.length?points.at(-1)[0]:null,
   display:ready?(prev&&prev.status==='ready'&&prev.value===value?prev.display:String(value)):'Unavailable',
  };
  delete item.comparison;
  next[key]=item;
 }
 series.historical_averages=next;
}

function toV6Data(src){
 const data=structuredClone(src);
 data.schema_version=6;data.window_seconds=86400;
 const dayEnd=Math.floor(data.end/86400)*86400;
 data.end=dayEnd;data.start=dayEnd-86400;
 const dailyPeriods=data.time_ranges.periods.filter(p=>p.window.unit!=='hours');
 const defId=dailyPeriods.find(p=>p.id==='1mo')?.id||dailyPeriods[0].id;
 data.time_ranges={version:1,default_range:defId,legacy_aliases:{'24h':defId,'30d':defId},card_slots:{fallback_dynamic:defId,fixed:['6mo','all']},periods:dailyPeriods};
 for(const c of data.charts){
  c.kind=c.kind==='gauge_hour_mean'?'gauge_daily_mean':'counter_daily_rate';
  c.start=data.start;c.end=data.end;
  c.query=c.history_queries.daily.value;c.quality_query=c.history_queries.daily.eligible;
  c.history_queries={daily:c.history_queries.daily};
  for(const s of c.series){
   delete s.values;delete s.hourly_relays;delete s.hourly_expected_relays;
   delete s.history.sampled_hour;delete s.current.hour;delete s.reference.hour;delete s.peaks.hour;
   if(s.relay_population){delete s.relay_population.hour;delete s.relay_population.queries.hour;}
   if(s.relay_counts)delete s.relay_counts.hour;
  }
 }
 return data;
}
function toV6Ref(srcRef){
 const ref=structuredClone(srcRef);
 for(const group of Object.values(ref.distributions))for(const periods of Object.values(group))delete periods.hour;
 return ref;
}

function populationData(){
 const data=withBoundedMinimumCoverage(structuredClone(current.data),0.9),chart=data.charts[0],series=chart.series[0],end=data.end;
 withoutComparisons(series);
 const dayEnd=Math.floor(end/86400)*86400;
 const day=series.current.day,expected=day.expected_relays;
 Object.assign(day,{value:null,relays:16,coverage:16/expected,valid:false});
 Object.assign(series.current.hour,{value:null,relays:16,expected_relays:expected,coverage:16/expected,valid:false});
 series.values[23]=[end,null];series.hourly_relays[23]=[end,16];series.hourly_expected_relays[23]=[end,expected];
 series.reference.day.current_percentile=null;series.reference.hour.current_percentile=null;
 series.history.daily=[[dayEnd-2*86400,0,8,1,true],[dayEnd-86400,24,24,1,true],[dayEnd,null,16,16/expected,false]];
 syncHistoricalAverages(series,data.time_ranges.periods,end);
 series.relay_population={method:'eligible-last-flags-v1',queries:{hour:'sum(example_hour)',day:'sum(example_day)'},
  hour:[end,16,16,8,0,0,8,8],day:[end,16,16,8,0,0,8,8],
  daily:[[dayEnd-2*86400,8,8,8,0,0,8,8],[dayEnd-86400,24,24,8,8,0,16,16],[dayEnd,16,16,8,0,0,8,8]]};
 const complete=(total,guard,middle_only,exit,hsdir,v2dir,windows,relayWindows)=>({total,guard,middle_only,exit,hsdir,v2dir,role_bounds:null,
  measured_windows:windows,classified_windows:windows,relay_windows:relayWindows,classified_relay_windows:relayWindows,coverage:1});
 series.relay_counts={hour:complete(16,8,0,0,8,8,1,16),day:complete(16,8,0,0,8,8,1,16)};
 for(const period of data.time_ranges.periods){
  if(period.window.unit==='hours')continue;
  const key=period.summary_key;
  series.relay_counts[key]=period.window.unit==='all_time'
   ?complete(16,8,4,0,12,12,2,32)
   :complete(null,null,null,null,null,null,2,32);
 }
 data.provenance.relay_population_method_sha256='a'.repeat(64);
 return data;
}

function comparisonData(){
 const data=withBoundedMinimumCoverage(structuredClone(current.data),0.9),chart=data.charts[0],series=chart.series[0];
 withoutPopulation(series);withoutComparisons(series);
 const dayEnd=Math.floor(data.end/86400)*86400,cutoff=dayEnd-86400,n=Math.max(8,chart.relays);
 Object.assign(series.current.day,{value:2*n,relays:n,expected_relays:n,coverage:1,valid:true});
 series.history.daily=[[dayEnd-3*86400,8,8,1,true],[dayEnd-2*86400,16,8,1,true],
  [cutoff,24,8,1,true],[dayEnd,2*n,n,1,true]];
 for(const periodDef of data.time_ranges.periods){
  if(periodDef.window.unit==='hours')continue;
  const period=periodDef.summary_key,average=series.historical_averages[period];
  Object.assign(average,{days:4,end:dayEnd,observed_start:dayEnd-4*86400,observed_end:dayEnd});
  if(periodDef.window.unit==='all_time')Object.assign(average,{start:dayEnd-4*86400,status:'ready',value:(48+2*n)/4,display:String((48+2*n)/4),
   expected_days:4,coverage:1,minimum_coverage:0});
  else{
   const pStart=periodStartTs(dayEnd,periodDef);
   average.start=pStart;
   average.expected_days=(dayEnd-pStart)/86400;
   Object.assign(average,{status:'insufficient_data',value:null,display:'Unavailable',coverage:4/average.expected_days});
  }
  average.comparison={method:'daily-per-relay-midrank-v1',cutoff,n:3,start:dayEnd-4*86400,end:cutoff,
   current_percentile:periodDef.window.unit==='all_time'?50:null,flat_zero:false};
 }
 return data;
}

test('verified current release includes all data, reference and CSV bytes',async()=>{
 const storage=server(),result=await loader(storage).load();
 assert.equal(result.descriptor.id,'current');assert.equal(result.fallback,false);
 assert.equal(result.objects.size,defaultObjectCount);assert.deepEqual(result.objects.get('data.json'),new Uint8Array(current.objects.get('data.json')));
 assert.equal(storage.calls.length,defaultObjectCount+2);assert.ok(storage.calls.slice(1).every(call=>call.key.startsWith('releases/current/')));
});

test('Blob download bytes match every verified object and the immutable manifest',async()=>{
 const bundle=await loader(server()).load(),environment=controllerBrowser(),render=renderer();
 const controller=createLiveController(environment.browser,render.make,{loader:{async load(){return bundle;}}});
 await controller.refresh();const app=render.calls.find(call=>call[0]==='make')[1];
 for(const [name,url] of app.context.downloads){
  const blob=environment.created.find(item=>item.url===url).blob;
  const expected=name==='manifest.json'?bundle.descriptor.manifest_sha256:bundle.manifest.objects[name].sha256;
  assert.equal(hash(Buffer.from(await blob.arrayBuffer())),expected,name);
 }
 assert.equal(app.context.downloads.size,defaultObjectCount+1);controller.stop();
});

test('reused objects receive the new release context and new verified Blob downloads',async()=>{
 const next=fixture('next'),storage=server([current,next]),environment=controllerBrowser(),render=renderer();
 const controller=createLiveController(environment.browser,render.make,{loader:loader(storage)});
 await controller.refresh();const app=render.calls.find(call=>call[0]==='make')[1];
 const oldUrls=new Map(app.context.downloads);
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
 assert.equal(await controller.refresh(),true);assert.equal(app.context.id,'next');
 assert.deepEqual(storage.calls.map(call=>call.key),['manifest.json','releases/next/manifest.json']);
 assert.equal(app.context.downloads.size,defaultObjectCount+1);
 for(const [name,url] of app.context.downloads){
  assert.notEqual(url,oldUrls.get(name),'download resources belong to the new release');
  const blob=environment.created.find(item=>item.url===url).blob;
  const expected=name==='manifest.json'?next.descriptor.manifest_sha256:next.manifest.objects[name].sha256;
  assert.equal(hash(Buffer.from(await blob.arrayBuffer())),expected,name);
 }
 controller.stop();
});

test('cold load falls back as a whole for missing or corrupt candidate objects',async t=>{
 for(const key of ['data.json',current.data.reference_document,'exports/transport_rejections-all-time.csv']){
  await t.test(key,async()=>{
   const storage=server();storage.files.set('releases/current/'+key,Buffer.from('bad'));
   const result=await loader(storage).load();assert.equal(result.descriptor.id,'previous');assert.equal(result.fallback,true);
   assert.deepEqual(result.objects.get('data.json'),new Uint8Array(previous.objects.get('data.json')));
   assert.ok([...result.objects].every(([name,bytes])=>hash(bytes)===previous.manifest.objects[name].sha256));
  });
 }
});

test('warm load failure does not select a different fallback release',async()=>{
 const key='exports/transport_rejections-1mo.csv';
 const changed=fixture('changed',{objectChange:objects=>objects.set(key,Buffer.concat([objects.get(key),Buffer.from('\n')]))});
 const storage=server([current,changed]),read=loader(storage),active=await read.load();
 storage.root={...storage.root,current:changed.descriptor,previous:current.descriptor};
 storage.files.delete('releases/changed/'+key);storage.calls.length=0;
 await assert.rejects(read.load(active));
 assert.ok(storage.calls.some(call=>call.key==='releases/changed/'+key));
 assert.ok(storage.calls.slice(1).every(call=>call.key.startsWith('releases/changed/')));
 assert.equal(active.descriptor.id,'current');
});

test('root changes during a load cannot retarget immutable object requests',async()=>{
 const storage=server(),fetch=storage.fetch;
 storage.fetch=async(url,options)=>{
  if(url.endsWith('/current/data.json'))storage.root={...storage.root,current:previous.descriptor,previous:current.descriptor};
  return fetch(url,options);
 };
 const result=await loader(storage).load();assert.equal(result.descriptor.id,'current');
 assert.ok(storage.calls.slice(1).every(call=>call.key.startsWith('releases/current/')));
});

test('unchanged current descriptor reuses the already verified release',async()=>{
 const storage=server(),read=loader(storage),active=await read.load();storage.calls.length=0;
 const result=await read.load({...active,fallback:true});assert.equal(result.data,active.data);assert.equal(result.fallback,false);
 assert.deepEqual(storage.calls.map(call=>call.key),['manifest.json']);
});

test('new release reuses only same-path verified content and owns copied bytes',async()=>{
 const changedKey='exports/transport_rejections-1mo.csv';
 const next=fixture('next',{dataChange:data=>data.generated_at='new generation',
  objectChange:objects=>objects.set(changedKey,Buffer.concat([objects.get(changedKey),Buffer.from('\n')]))});
 const storage=server([current,next]),read=loader(storage),active=await read.load();
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
 const result=await read.load(active);
 assert.equal(result.descriptor.id,'next');assert.equal(result.objects.size,defaultObjectCount);
 assert.deepEqual(storage.calls.map(call=>call.key).sort(),
  ['manifest.json','releases/next/manifest.json','releases/next/data.json','releases/next/'+changedKey].sort());
 for(const [name,bytes] of result.objects){
  assert.equal(hash(bytes),next.manifest.objects[name].sha256,name);
  assert.notEqual(bytes,active.objects.get(name),'admitted arrays do not alias prior release arrays');
  assert.notEqual(bytes.buffer,active.objects.get(name).buffer,'admitted buffers do not alias prior release buffers');
 }
 const key=current.data.reference_document,accepted=Buffer.from(result.objects.get(key));
 active.objects.get(key).fill(0);
 assert.deepEqual(Buffer.from(result.objects.get(key)),accepted,'later cache mutation cannot change admitted bytes');
});

test('cache bytes are snapshotted before asynchronous digest and rehashed',async()=>{
 const next=fixture('next'),storage=server([current,next]);
 const active=await loader(storage).load(),key='exports/transport_rejections-all-time.csv';
 let rehashed=false;
 const subtle={async digest(algorithm,bytes){
  if(hash(bytes)===current.manifest.objects[key].sha256){
   rehashed=true;active.objects.get(key).fill(0);
  }
  return webcrypto.subtle.digest(algorithm,bytes);
 }};
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
 const result=await loader(storage,{subtle}).load(active);
 assert.equal(rehashed,true);
 assert.equal(hash(result.objects.get(key)),next.manifest.objects[key].sha256);
 assert.deepEqual(storage.calls.map(call=>call.key),['manifest.json','releases/next/manifest.json']);
});

test('corrupt or incomplete active cache refetches the new release object',async t=>{
 const key='exports/transport_rejections-1mo.csv';
 for(const mutation of ['bytes','length','absent','prior-hash','prior-size'])await t.test(mutation,async()=>{
  const next=fixture('next'),storage=server([current,next]),read=loader(storage),active=await read.load();
  if(mutation==='bytes')active.objects.get(key)[0]^=1;
  if(mutation==='length')active.objects.set(key,new Uint8Array(1));
  if(mutation==='absent')active.objects.delete(key);
  if(mutation==='prior-hash')active.manifest.objects[key].sha256='0'.repeat(64);
  if(mutation==='prior-size')active.manifest.objects[key].bytes++;
  storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
  const result=await read.load(active);
  assert.equal(hash(result.objects.get(key)),next.manifest.objects[key].sha256);
  assert.deepEqual(storage.calls.map(call=>call.key).sort(),['manifest.json','releases/next/manifest.json','releases/next/'+key].sort());
 });
});

test('failed refetch of corrupt cache rejects the candidate and preserves the active bundle',async()=>{
 const next=fixture('next'),storage=server([current,next]),read=loader(storage),active=await read.load();
 const key='exports/transport_rejections-1mo.csv',data=active.data,descriptor=structuredClone(active.descriptor);
 active.objects.get(key)[0]^=1;
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};
 storage.files.set('releases/next/'+key,Buffer.from('still corrupt'));
 await assert.rejects(read.load(active),/byte count|checksum/);
 assert.equal(active.data,data);assert.deepEqual(active.descriptor,descriptor);
});

test('identical content under a new path is fetched, not found through a global hash cache',async()=>{
 const oldId='protocol_closes',newId='protocol_checks';
 const renamed=fixture('renamed',{
  dataChange:data=>{
   data.charts.find(chart=>chart.id===oldId).id=newId;
   for(const family of data.families){
    family.children=family.children.map(id=>id===oldId?newId:id);
    if(family.primary===oldId)family.primary=newId;
   }
  },
  referenceChange:reference=>{reference.distributions[newId]=reference.distributions[oldId];delete reference.distributions[oldId];},
  exportSources:{[newId]:oldId},
 });
 const storage=server([current,renamed]),read=loader(storage),active=await read.load();
 storage.root={...storage.root,current:renamed.descriptor,previous:current.descriptor};storage.calls.length=0;
 const result=await read.load(active);
 for(const period of source.time_ranges.periods){
  const span=period.export_suffix;
  const key='exports/'+newId+'-'+span+'.csv';
  assert.equal(renamed.manifest.objects[key].sha256,current.manifest.objects['exports/'+oldId+'-'+span+'.csv'].sha256);
  assert.ok(storage.calls.some(call=>call.key==='releases/renamed/'+key));
  assert.equal(hash(result.objects.get(key)),renamed.manifest.objects[key].sha256);
 }
});

test('reused bytes still pass new manifest budgets, data schema and reference validation',async t=>{
 const incompatible=fixture('incompatible',{manifestChange:manifest=>manifest.observation_end+=3600});
 const wrongReference=fixture('wrong-reference',{dataChange:data=>data.reference_window={...data.reference_window,end:data.reference_window.end-86400}});
 for(const next of [incompatible,wrongReference])await t.test(next.descriptor.id,async()=>{
  const storage=server([current,next]),read=loader(storage),active=await read.load();
  storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};
  await assert.rejects(read.load(active));
  assert.equal(active.descriptor.id,'current');
 });
 const storage=server([current,previous]),active=await loader(storage).load();
 storage.root={...storage.root,current:previous.descriptor,previous:current.descriptor};storage.calls.length=0;
 await assert.rejects(loader(storage,{limits:{...LIVE_LIMITS,totalBytes:1}}).load(active));
 assert.deepEqual(storage.calls.map(call=>call.key),['manifest.json','releases/previous/manifest.json']);
});

test('cancelled cache verification cannot start a replacement fetch or admit late bytes',async()=>{
 const next=fixture('next'),storage=server([current,next]),active=await loader(storage).load();
 const started=deferred(),finish=deferred(),abort=new AbortController();
 const subtle={async digest(algorithm,bytes){
  if(hash(bytes)===current.manifest.objects['data.json'].sha256){started.resolve();await finish.promise;}
  return webcrypto.subtle.digest(algorithm,bytes);
 }};
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
 const loading=loader(storage,{subtle}).load(active,abort.signal);
 await started.promise;abort.abort();await assert.rejects(loading,/cancelled/);finish.resolve();await pause(0);
 assert.deepEqual(storage.calls.map(call=>call.key),['manifest.json','releases/next/manifest.json']);
 assert.equal(active.descriptor.id,'current');
});

test('cold previous-release fallback cannot reuse a partially loaded failed candidate',async()=>{
 const storage=server();storage.files.delete('releases/current/exports/transport_rejections-1mo.csv');
 const result=await loader(storage).load();assert.equal(result.fallback,true);
 const previousRequests=storage.calls.filter(call=>call.key.startsWith('releases/previous/')).map(call=>call.key);
 assert.equal(previousRequests.length,defaultObjectCount+1);
 for(const path of ['manifest.json',...previous.objects.keys()])assert.ok(previousRequests.includes('releases/previous/'+path));
});

test('candidate schema, required object set and reference semantics are checked',async t=>{
 const changes=[
  {manifestChange:manifest=>manifest.data_schema_version=4},
  {manifestChange:manifest=>manifest.data_schema_version=5},
  {manifestChange:manifest=>manifest.data_schema_version=6},
  {manifestChange:manifest=>manifest.observation_end+=1800},
  {dataChange:data=>data.schema_version=4},
  {dataChange:data=>data.end+=1800},
  {dataChange:data=>data.families[0].primary='missing'},
  {dataChange:data=>data.charts[0].kind='counter_daily_rate'},
  {dataChange:data=>delete data.charts[0].history_queries.current24},
  {dataChange:data=>data.charts[0].history_queries.current24={}},
  {dataChange:data=>data.charts[0].history_queries.daily.value=''},
  {dataChange:data=>data.charts[0].history_queries.sampled_hour.value='   '},
  {dataChange:data=>delete data.charts[0].history_queries.daily.available},
  {dataChange:data=>data.charts[0].series[0].history.daily[0][1]=-1},
  {dataChange:data=>data.charts[0].series[0].history.daily[0][2]=9},
  {dataChange:data=>data.charts[0].series[0].history.sampled_hour.push([data.start+3600,1,8,1,true])},
  {dataChange:data=>data.charts[0].series[0].values=[]},
  {dataChange:data=>{const s=data.charts[0].series[0];s.hourly_relays[0][1]=8;s.hourly_expected_relays[0][1]=96;s.values[0][1]=1;}},
  {dataChange:data=>data.charts[0].series[0].current.day.value+=10},
  {dataChange:data=>data.charts[0].series[0].current.hour.value+=10},
  {manifestChange:manifest=>delete manifest.objects['exports/transport_rejections-1mo.csv']},
  {manifestChange:manifest=>delete manifest.objects['exports/transport_rejections-24h.csv']},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-1h.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-024h.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-23h.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-30m.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-60s.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-01d.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport_rejections-foo-1h.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>{manifest.objects['exports/transport-rejections-24h.csv']=manifest.objects['exports/transport_rejections-1mo.csv'];delete manifest.objects['exports/transport_rejections-1mo.csv'];}},
  {manifestChange:manifest=>manifest.objects['https://bad.test/data.json']=entry(Buffer.from('{}'))},
  {manifestChange:manifest=>manifest.objects['../data.json']=entry(Buffer.from('{}'))},
  {referenceChange:reference=>reference.window.end-=86400},
  {referenceChange:reference=>reference.method_sha256='d'.repeat(64)},
  {referenceChange:reference=>reference.distributions.transport_rejections.conn_rejected.day.pop()},
  {referenceChange:reference=>reference.distributions.transport_rejections.conn_rejected.hour.pop()},
 ];
 for(const [index,change] of changes.entries())await t.test(String(index),async()=>{
  const bad=fixture('bad-'+index,change),storage=server([bad,previous],bad,previous);
  const result=await loader(storage).load();assert.equal(result.descriptor.id,'previous');
 });
 for(const key of ['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24']){
  assert.deepEqual(parseLiveJson(Buffer.from(JSON.stringify({[key]:1}))),{[key]:1});
  const v6Obj=fixture('v6-forbid-'+key,{
   referenceChange:ref=>{for(const g of Object.values(ref.distributions))for(const p of Object.values(g))delete p.hour;},
   dataChange:data=>{Object.assign(data,toV6Data(data));data[key]=1;},
   manifestChange:m=>{m.data_schema_version=6;},
  });
  assert.throws(()=>checkData(v6Obj.data,v6Obj.manifest),/Sub-24h field/);
 }
});

test('daily reference may have an older source snapshot and different input identity',async()=>{
 const reused=fixture('reused',{referenceChange:reference=>{
  reference.source_snapshot_end=source.end-86400;reference.source_input_sha256='e'.repeat(64);
 }});
 const result=await loader(server([reused],reused,null)).load();assert.equal(result.descriptor.id,'reused');
});

test('qualified historical observations require at least 90 percent coverage',()=>{
 const data=structuredClone(current.data),row=data.charts[0].series[0].history.daily.find(row=>row[4]);
 withoutPopulation(data.charts[0].series[0]);
 withoutComparisons(data.charts[0].series[0]);
 assert.ok(row);row[3]=0.899999999999;
 assert.throws(()=>checkData(data,current.manifest),/qualified observation/);
 row[3]=0.9;assert.equal(checkData(data,current.manifest),data);
 if(row[0]!==data.end){row[3]=0.1;row[4]=false;syncHistoricalAverages(data.charts[0].series[0],data.time_ranges.periods,data.end);assert.equal(checkData(data,current.manifest),data);}
});

test('current coverage matches the producer denominator, tolerance and eligibility rules',()=>{
 const invalid=[
  {relays:8,expected_relays:8000,coverage:0.001},
  {expected_relays:0},{expected_relays:null},{expected_relays:1.5},{expected_relays:1144},
  {relays:7201,expected_relays:8000,coverage:7201/8000},
  {relays:8008,expected_relays:8000,coverage:1},
  {coverage:1.01},{coverage:null},{coverage:0.95},
  {relays:7200,expected_relays:8000,coverage:0.89999999995},
  {relays:8999999992,expected_relays:10000000000,coverage:0.9},
 ];
 for(const patch of invalid){
  const data=structuredClone(current.data);
  data.charts[0].series[0].history.daily.pop();
  withoutComparisons(data.charts[0].series[0]);
  Object.assign(data.charts[0].series[0].current.day,patch);
  assert.throws(()=>checkData(data,current.manifest),/Invalid current/);
 }
 for(const coverage of [0.9,0.9000000005]){
  const data=structuredClone(current.data),s=data.charts[0].series[0];
  s.history.daily.pop();
  syncHistoricalAverages(s,data.time_ranges.periods,data.end);
  Object.assign(s.current.day,{relays:7200,expected_relays:8000,coverage,valid:true});
  withoutPopulation(s);
  assert.equal(checkData(data,current.manifest),data);
 }
 const partial=structuredClone(current.data),ps=partial.charts[0].series[0];
 ps.history.daily.pop();
 syncHistoricalAverages(ps,partial.time_ranges.periods,partial.end);
 Object.assign(ps.current.day,{value:null,relays:800,expected_relays:8000,coverage:0.1,valid:false});
 withoutPopulation(ps);
 assert.equal(checkData(partial,current.manifest),partial);
});

test('a successful empty current cohort is an admissible release, not a broken candidate',async()=>{
 const outage=fixture('outage',{dataChange:data=>{
  data.fleet=0;data.custom=0;data.defense=0;
  for(const chart of data.charts){
   chart.relays=0;
   for(const series of chart.series){
    withoutPopulation(series);
    const daily=series.history.daily;
    if(daily.length&&daily.at(-1)[0]===data.end)daily[daily.length-1]=[data.end,null,0,0,false];
    syncHistoricalAverages(series,data.time_ranges.periods,data.end);
    Object.assign(series.current.day,{valid:false,value:null,relays:0,coverage:0,display:'Unavailable'});
    Object.assign(series.current.hour,{valid:false,value:null,relays:0,coverage:0,display:'Unavailable'});
    series.values[23]=[data.end,null];series.hourly_relays[23]=[data.end,0];
    series.reference.day.current_percentile=null;series.reference.hour.current_percentile=null;
   }
  }
 }});
 const result=await loader(server([outage,previous],outage,previous)).load();
 assert.equal(result.descriptor.id,'outage');assert.equal(result.fallback,false);
 assert.ok(result.data.charts.every(chart=>chart.relays===0));
 const expectedPrefix=source.charts[0].series[0].history.daily.slice(0,-1);
 assert.deepEqual(result.data.charts[0].series[0].history.daily.slice(0,-1),expectedPrefix);
});

test('optional data reference method identity is compared when supplied',async()=>{
 const without=fixture('without-method',{dataChange:data=>delete data.provenance.reference_method_sha256});
 assert.equal((await loader(server([without],without,null)).load()).descriptor.id,'without-method');
 const mismatch=fixture('wrong-method',{dataChange:data=>data.provenance.reference_method_sha256='f'.repeat(64)});
 await assert.rejects(loader(server([mismatch],mismatch,null)).load());
});

test('historical comparisons admit old bundles and a complete normalized current-day release',async()=>{
 const old=structuredClone(current.data);
 for(const chart of old.charts)for(const series of chart.series)withoutComparisons(series);
 assert.equal(checkData(old,current.manifest),old);
 const data=comparisonData();assert.equal(checkData(data,current.manifest),data);
 const series=data.charts[0].series[0];
 assert.equal(series.historical_averages.all_time.days,4);
 assert.equal(series.historical_averages.all_time.comparison.n,3,'the overlapping day remains in the mean, not the rank');
 assert.equal(series.historical_averages.all_time.value,(48+2*series.current.day.relays)/4);
 assert.equal(series.historical_averages.all_time.comparison.current_percentile,50);
 assert.equal(series.historical_averages.six_months.comparison.n,3);
 assert.equal(series.historical_averages.six_months.comparison.current_percentile,null,'unavailable six-month mean gates the rank');
 const candidate=fixture('historical-comparison',{dataChange:target=>Object.assign(target,data)});
 const accepted=await loader(server([candidate],candidate,null)).load();
 assert.equal(accepted.objects.size,defaultObjectCount);assert.equal(accepted.descriptor.id,'historical-comparison');
});

test('historical comparisons enforce exact fields, membership, calendar bounds and midrank',()=>{
 const cases=[
  s=>s.historical_averages.all_time.comparison=null,
  s=>s.historical_averages.all_time.comparison.extra='private note',
  s=>s.historical_averages.all_time.comparison.method='raw-fleet-rank',
  s=>s.historical_averages.all_time.comparison.cutoff+=86400,
  s=>s.historical_averages.all_time.comparison.n++,
  s=>s.historical_averages.all_time.comparison.start+=86400,
  s=>s.historical_averages.all_time.comparison.end+=86400,
  s=>s.historical_averages.all_time.comparison.current_percentile=100,
  s=>s.historical_averages.all_time.comparison.flat_zero=true,
  s=>s.historical_averages.all_time.days--,
  s=>s.historical_averages.all_time.start+=86400,
  s=>s.historical_averages.seven_days.start-=86400,
  s=>s.historical_averages.six_months.start-=86400,
  s=>Object.assign(s.historical_averages.six_months,{status:'ready',value:265,display:'265'}),
 ];
 for(const change of cases){const data=comparisonData();change(data.charts[0].series[0]);
  assert.throws(()=>checkData(data,current.manifest),/[Hh]istorical comparison/);}
 for(const key of ['cutoff','n','start','end','current_percentile','flat_zero']){
  for(const value of ['0',NaN,Infinity,{},-1]){
   const data=comparisonData();data.charts[0].series[0].historical_averages.all_time.comparison[key]=value;
   assert.throws(()=>checkData(data,current.manifest),/[Hh]istorical comparison/,`${key}: ${value}`);
  }
 }
 const data=comparisonData();data.reference_cutoff-=86400;
 data.charts[0].series[0].historical_averages.all_time.comparison.cutoff=data.reference_cutoff;
 assert.throws(()=>checkData(data,current.manifest),/cutoff/,'a self-consistent but incorrectly lagged cutoff still fails');
});

test('historical comparison ties use per-relay values and exact half-weight ranks',()=>{
 const data=comparisonData(),series=data.charts[0].series[0],rows=series.history.daily;
 rows[1][1]=32;rows[1][2]=16;rows[2][1]=16;
 series.historical_averages.all_time.value=(40+2*series.current.day.relays)/4;
 series.historical_averages.all_time.comparison.current_percentile=100*(1+.5*2)/3;
 assert.equal(checkData(data,current.manifest),data);
 series.historical_averages.all_time.comparison.current_percentile=100*1/3;
 assert.throws(()=>checkData(data,current.manifest),/rank or zero-state/);
});

test('historical comparison calendar starts clamp leap years and month ends',()=>{
 for(const [endText,startText] of [['2024-08-31T00:00:00Z','2024-02-29T00:00:00Z'],
  ['2025-08-31T00:00:00Z','2025-02-28T00:00:00Z'],['2026-03-31T00:00:00Z','2025-09-30T00:00:00Z']]){
  const data=comparisonData(),end=Date.parse(endText)/1000,delta=end-data.end;
  data.start+=delta;data.end=end;data.reference_cutoff+=delta;
  for(const chart of data.charts){
   chart.start+=delta;chart.end+=delta;
   for(const series of chart.series){
    withoutPopulation(series);
    for(const row of series.history.daily)row[0]+=delta;
    for(const row of series.history.sampled_hour)row[0]+=delta;
    for(const arr of [series.values,series.hourly_relays,series.hourly_expected_relays])for(const row of arr)row[0]+=delta;
    if(series!==data.charts[0].series[0])syncHistoricalAverages(series,data.time_ranges.periods,end);
   }
  }
  const series=data.charts[0].series[0];
  for(const periodDef of data.time_ranges.periods){
   if(periodDef.window.unit==='hours')continue;
   const period=periodDef.summary_key,average=series.historical_averages[period];
   average.start=periodDef.window.unit==='all_time'?average.start+delta:periodStartTs(end,periodDef);
   if(period==='six_months')assert.equal(average.start,Date.parse(startText)/1000);
   average.end+=delta;
   if(periodDef.window.unit!=='all_time'){
    average.expected_days=(average.end-average.start)/86400;
    average.coverage=average.days/average.expected_days;
   }
   for(const key of ['cutoff','start','end'])average.comparison[key]+=delta;
  }
  assert.equal(checkData(data,{...current.manifest,observation_end:end}),data);
  series.historical_averages.six_months.start-=86400;
  assert.throws(()=>checkData(data,{...current.manifest,observation_end:end}),/parent window mismatch/);
 }
});

test('historical comparison zero and unavailable states retain their comparison membership',()=>{
 for(const [value,valid,rank,flat] of [[0,true,null,true],[1,true,100,false],[0,false,null,false]]){
  const data=comparisonData(),series=data.charts[0].series[0];
  for(const row of series.history.daily)row[1]=0;
  series.historical_averages.all_time.value=0;series.historical_averages.all_time.display='0';
  const dayVal=valid?value*series.current.day.relays:null;
  series.current.day.value=dayVal;series.current.day.valid=valid;
  series.history.daily.at(-1)[1]=dayVal;series.history.daily.at(-1)[4]=valid;
  for(const avg of Object.values(series.historical_averages)){avg.days=valid?4:3;avg.coverage=avg.days/avg.expected_days;}
  Object.assign(series.historical_averages.all_time.comparison,{current_percentile:rank,flat_zero:flat});
  assert.equal(checkData(data,current.manifest),data);
  assert.equal(series.historical_averages.all_time.comparison.n,3);
  assert.equal(series.historical_averages.six_months.comparison.flat_zero,false);
 }
 const unavailable=comparisonData(),series=unavailable.charts[0].series[0];
 series.current.day.value=null;series.current.day.valid=false;
 series.history.daily.at(-1)[1]=null;series.history.daily.at(-1)[4]=false;
 for(const avg of Object.values(series.historical_averages)){avg.days=3;avg.coverage=avg.days/avg.expected_days;}
 series.historical_averages.all_time.comparison.current_percentile=null;
 assert.equal(checkData(unavailable,current.manifest),unavailable);
 series.historical_averages.all_time.comparison.flat_zero=true;
 assert.throws(()=>checkData(unavailable,current.manifest),/rank or zero-state/);
});

test('empty historical comparisons have null interval bounds and no marker',()=>{
 const data=comparisonData(),series=data.charts[0].series[0];series.history.daily=[];
 for(const [period,average] of Object.entries(series.historical_averages)){
  Object.assign(average,{days:0,coverage:0,status:'insufficient_data',value:null,display:'Unavailable'});
  if(period==='all_time')Object.assign(average,{start:null,expected_days:0});
  Object.assign(average.comparison,{n:0,start:null,end:null,current_percentile:null,flat_zero:false});
 }
 assert.equal(checkData(data,current.manifest),data);
 series.historical_averages.all_time.comparison.start=data.reference_cutoff;
 assert.throws(()=>checkData(data,current.manifest),/membership/);
});

test('optional relay populations admit old releases and exact per-statistic metadata',()=>{
 const old=structuredClone(current.data);
 for(const chart of old.charts)for(const series of chart.series)withoutPopulation(series);
 delete old.provenance.relay_population_method_sha256;
 assert.equal(checkData(old,current.manifest),old);
 const data=populationData();assert.equal(checkData(data,current.manifest),data);
 const series=data.charts[0].series[0];
 assert.equal(series.relay_counts.all_time.total,16,'equal-day mean, not latest population or unique relays');
 assert.ok(series.relay_counts.all_time.guard+series.relay_counts.all_time.hsdir>16,'flags may overlap');
 delete series.relay_counts;
 assert.equal(checkData(data,current.manifest),data,'raw aggregate evidence is independently optional');
});

test('relay population raw maps, method and queries are closed and typed',()=>{
 const cases=[
  raw=>raw.extra='private note',raw=>delete raw.day,raw=>raw.method='current-flags',
  raw=>raw.queries.day=' ',raw=>raw.queries.day={endpoint:'not public'},raw=>raw.queries.extra='query',
  raw=>raw.daily=null,raw=>raw.day.pop(),raw=>raw.day=null,
 ];
 for(const change of cases){
  const data=populationData();change(data.charts[0].series[0].relay_population);
  assert.throws(()=>checkData(data,current.manifest),/[Rr]elay/);
 }
 for(const value of [null,true,-1,.5,1,'2',NaN,Infinity,Number.MAX_SAFE_INTEGER+1])for(let index=0;index<8;index++){
  const data=populationData();data.charts[0].series[0].relay_population.day[index]=value;
  assert.throws(()=>checkData(data,current.manifest),/[Rr]elay/,`${index}: ${value}`);
 }
 for(const value of [null,{},'not-a-hash','A'.repeat(64)]){
  const data=populationData();data.provenance.relay_population_method_sha256=value;
  assert.throws(()=>checkData(data,current.manifest),/method identity/);
 }
});

test('relay population rows match their measured count and exact UTC window',()=>{
 const cases=[
  raw=>raw.day[0]-=86400,raw=>raw.day[1]+=8,raw=>raw.day[2]=24,raw=>raw.day[3]=24,
  raw=>raw.day[4]=16,raw=>raw.daily[0][0]+=3600,raw=>raw.daily[0][0]-=86400,
  raw=>raw.daily[0][1]+=8,raw=>raw.daily.reverse(),raw=>raw.daily.splice(1,0,[...raw.daily[0]]),
 ];
 for(const change of cases){
  const data=populationData();change(data.charts[0].series[0].relay_population);
  assert.throws(()=>checkData(data,current.manifest),/[Rr]elay/);
 }
});

test('relay summaries reconcile current counts and the exact qualifying historical days',()=>{
 const cases=[
  series=>series.relay_counts.extra={},series=>delete series.relay_counts.one_month,
  series=>series.relay_counts.day.note='extra',series=>series.relay_counts.day.total=8,
  series=>delete series.relay_counts.day.role_bounds,series=>series.relay_counts.day.role_bounds={},
  series=>series.relay_counts.day.guard=2.000000000000001,
  series=>series.relay_counts.all_time.total=30,series=>series.relay_counts.all_time.guard=6,
  series=>series.relay_counts.all_time.measured_windows=3,series=>series.relay_counts.all_time.relay_windows=140,
  series=>series.relay_counts.all_time.classified_windows=1,
  series=>series.relay_counts.all_time.classified_relay_windows=31,
  series=>series.relay_counts.all_time.coverage=.99,
  series=>series.relay_counts.six_months.total=16,series=>series.relay_counts.six_months.guard=8,
 ];
 for(const change of cases){
  const data=populationData();change(data.charts[0].series[0]);
  assert.throws(()=>checkData(data,current.manifest),/[Rr]elay/);
 }
 for(const change of [
  series=>series.historical_averages.all_time.days=3,
  series=>series.historical_averages.six_months.start+=3600,
  series=>series.historical_averages.all_time.end+=86400,
  series=>series.historical_averages.six_months.status='unknown',
 ]){
  const data=populationData();change(data.charts[0].series[0]);
  assert.throws(()=>checkData(data,current.manifest),/parent window mismatch/);
 }
 for(const key of ['total','guard','measured_windows','classified_windows','relay_windows','classified_relay_windows','coverage']){
  for(const value of [true,'1',NaN,Infinity,-1,Number.MAX_SAFE_INTEGER+1]){
   const data=populationData();data.charts[0].series[0].relay_counts.all_time[key]=value;
   assert.throws(()=>checkData(data,current.manifest),/[Rr]elay/,`${key}: ${value}`);
  }
 }
});

test('fractional historical means use integer sums for Middle-only exclusivity',async()=>{
 const data=populationData(),chart=data.charts[0],series=chart.series[0],end=Math.floor(data.end/86400)*86400;
 const times=[end-2*86400,end-86400,end];
 chart.relays=104;
 Object.assign(series.current.day,{value:10,relays:104,expected_relays:104,coverage:1,valid:true});
 series.history.daily=times.map((t,index)=>[t,10,index===2?104:96,1,true]);
 series.relay_population.day=[end,104,104,96,8,0,0,0];
 series.relay_population.daily=times.map((t,index)=>[t,index===2?104:96,index===2?104:96,96,index===2?8:0,0,0,0]);
 for(const periodDef of data.time_ranges.periods){
  if(periodDef.window.unit==='hours')continue;
  const period=periodDef.summary_key,avg=series.historical_averages[period];
  avg.days=3;avg.coverage=3/avg.expected_days;
  Object.assign(series.relay_counts[period],{measured_windows:3,classified_windows:3,
   relay_windows:296,classified_relay_windows:296});
 }
 Object.assign(series.relay_counts.day,{total:104,guard:96,middle_only:8,exit:0,hsdir:0,v2dir:0,
  measured_windows:1,classified_windows:1,relay_windows:104,classified_relay_windows:104,coverage:1});
 Object.assign(series.relay_counts.all_time,{total:296/3,guard:96,middle_only:8/3,exit:0,hsdir:0,v2dir:0});
 assert.notEqual(series.relay_counts.all_time.total-96,8/3,'the averaged subtraction loses precision');
 const candidate=fixture('fractional-middle',{dataChange:target=>Object.assign(target,data)});
 const accepted=await loader(server([candidate],candidate,null)).load();
 assert.equal(accepted.descriptor.id,'fractional-middle');
 assert.equal(accepted.objects.size,defaultObjectCount,'the full immutable release is admitted');
 assert.equal(accepted.data.charts[0].series[0].relay_counts.all_time.middle_only,8/3);
 series.relay_population.daily[2][4]=16;
 assert.throws(()=>checkData(data,current.manifest),/Invalid relay population classification/,
  'a real Guard/Middle-only overlap is still rejected before averaging');
});

test('missing and partial flag history cannot silently shorten a historical mean',()=>{
 const periodKeys=current.data.time_ranges.periods.filter(p=>p.window.unit!=='hours').map(p=>p.summary_key);
 for(const partial of [false,true]){
  const data=populationData(),series=data.charts[0].series[0];
  if(partial)series.relay_population.daily[1]=[series.relay_population.daily[1][0],24,16,8,8,0,8,8];
  else series.relay_population.daily.splice(1,1);
  for(const period of periodKeys)Object.assign(series.relay_counts[period],{
   guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,classified_windows:1,
   classified_relay_windows:partial?24:8,coverage:partial?24/32:8/32,
  });
  series.relay_counts.all_time.role_bounds=partial
   ?{guard:[8,12],middle_only:[4,8],exit:[0,4],hsdir:[8,12],v2dir:[8,12]}
   :{guard:[4,16],middle_only:[0,12],exit:[0,12],hsdir:[4,16],v2dir:[4,16]};
  assert.equal(checkData(data,current.manifest),data);
  assert.equal(series.relay_counts.all_time.total,16);
  series.relay_counts.all_time.guard=2;
  assert.throws(()=>checkData(data,current.manifest),/role summary/);
 }
 const data=populationData(),series=data.charts[0].series[0];
 series.relay_population.day=null;
 Object.assign(series.relay_counts.day,{guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,
  classified_windows:0,classified_relay_windows:0,coverage:0});
 assert.equal(checkData(data,current.manifest),data);
 delete series.relay_population;
 assert.throws(()=>checkData(data,current.manifest),/evidence mismatch/,'summaries cannot invent missing flag evidence');
});

test('role bounds include wholly missing windows and reject narrower or fabricated intervals',()=>{
 const periodKeys=current.data.time_ranges.periods.filter(p=>p.window.unit!=='hours').map(p=>p.summary_key);
 const fixture=()=>{
  const data=populationData(),series=data.charts[0].series[0];
  series.relay_population.daily.splice(1,1);
  for(const period of periodKeys)Object.assign(series.relay_counts[period],{
   guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,classified_windows:1,
   classified_relay_windows:8,coverage:.25,
  });
  series.relay_counts.all_time.role_bounds={guard:[4,16],middle_only:[0,12],exit:[0,12],hsdir:[4,16],v2dir:[4,16]};
  return data;
 };
 assert.equal(checkData(fixture(),current.manifest).charts[0].series[0].relay_counts.all_time.measured_windows,2);
 for(const change of [bounds=>bounds.guard[0]+=Number.EPSILON*4,bounds=>bounds.guard[1]-=Number.EPSILON*16,
  bounds=>bounds.guard[0]=0,bounds=>bounds.guard[1]=17,bounds=>bounds.guard=['4',16],
  bounds=>bounds.guard=[4,NaN],bounds=>bounds.guard=[4,16,17],bounds=>delete bounds.exit,bounds=>bounds.extra=[0,1]]){
  const data=fixture();change(data.charts[0].series[0].relay_counts.all_time.role_bounds);
  assert.throws(()=>checkData(data,current.manifest),/population bounds/);
 }
 const missing=fixture();missing.charts[0].series[0].relay_counts.all_time.role_bounds=null;
 assert.throws(()=>checkData(missing,current.manifest),/population bounds/);
 const insufficient=fixture();insufficient.charts[0].series[0].relay_counts.six_months.role_bounds=insufficient.charts[0].series[0].relay_counts.all_time.role_bounds;
 assert.throws(()=>checkData(insufficient,current.manifest),/Unexpected relay population bounds/);
});

test('partial current flags have bounded counts without implying exact role counts',()=>{
 const data=populationData(),series=data.charts[0].series[0];
 series.relay_population.day=[data.end,16,8,0,8,0,0,0];
 Object.assign(series.relay_counts.day,{guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,
  classified_windows:0,classified_relay_windows:8,coverage:.5,
  role_bounds:{guard:[0,8],middle_only:[8,16],exit:[0,8],hsdir:[0,8],v2dir:[0,8]}});
 assert.equal(checkData(data,current.manifest),data);
 const legacy=structuredClone(data);delete legacy.charts[0].series[0].relay_population;
 assert.throws(()=>checkData(legacy,current.manifest),/evidence mismatch/);
});

test('zero and unknown current populations preserve explicit empty-set semantics',()=>{
 for(const verifiedEmpty of [false,true]){
  const data=populationData(),series=data.charts[0].series[0];
  Object.assign(series.current.day,{value:null,relays:0,coverage:0,valid:false});
  series.history.daily.at(-1)[2]=0;series.history.daily.at(-1)[3]=0;
  series.relay_population.day=verifiedEmpty?[data.end,0,0,0,0,0,0,0]:null;
  if(verifiedEmpty)series.relay_population.daily[series.relay_population.daily.length-1]=[data.end,0,0,0,0,0,0,0];
  else series.relay_population.daily.pop();
  Object.assign(series.relay_counts.day,{total:0,guard:verifiedEmpty?0:null,middle_only:verifiedEmpty?0:null,
   exit:verifiedEmpty?0:null,hsdir:verifiedEmpty?0:null,v2dir:verifiedEmpty?0:null,measured_windows:1,
   classified_windows:verifiedEmpty?1:0,relay_windows:0,classified_relay_windows:0,coverage:verifiedEmpty?1:null});
  assert.equal(checkData(data,current.manifest),data);
  series.relay_counts.day.coverage=0;
  assert.throws(()=>checkData(data,current.manifest),/population coverage/);
 }
 const data=populationData(),series=data.charts[0].series[0];
 Object.assign(series.current.day,{value:null,relays:null,coverage:null,valid:false});
 series.history.daily.at(-1)[2]=null;series.history.daily.at(-1)[3]=null;
 series.relay_population.day=null;series.relay_population.daily.pop();
 Object.assign(series.relay_counts.day,{total:null,guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,
  measured_windows:0,classified_windows:0,relay_windows:0,classified_relay_windows:0,coverage:null});
 assert.equal(checkData(data,current.manifest),data);
});

test('complete recorded zero flags are known zeros and do not imply Middle-only',()=>{
 const data=populationData(),series=data.charts[0].series[0];
 for(const row of [series.relay_population.day,...series.relay_population.daily])row.fill(0,3);
 for(const period of ['day','all_time'])for(const role of ['guard','middle_only','exit','hsdir','v2dir'])series.relay_counts[period][role]=0;
 assert.equal(checkData(data,current.manifest),data);
 assert.equal(series.relay_counts.all_time.coverage,1);
 assert.equal(series.relay_counts.all_time.middle_only,0);
 series.relay_counts.all_time.middle_only=8;
 assert.throws(()=>checkData(data,current.manifest),/role summary/);
});

test('population validation failures fall back cold and preserve a verified warm release',async()=>{
 const good=fixture('population-good',{dataChange:data=>Object.assign(data,populationData())});
 const bad=fixture('population-bad',{dataChange:data=>{
  Object.assign(data,populationData());data.charts[0].series[0].relay_counts.all_time.total=99;
 }});
 const storage=server([good,bad],bad,good),read=loader(storage);
 const active=await read.load();assert.equal(active.descriptor.id,'population-good');assert.equal(active.fallback,true);
 await assert.rejects(read.load(active));
 assert.equal(active.data.charts[0].series[0].relay_counts.all_time.total,16);
});

test('invalid root, descriptor and budgets fail before publication',async t=>{
 for(const mutation of [
  root=>root.format='future',root=>root.current.id='../escape',root=>root.current.id=123,
  root=>root.current.manifest_bytes=LIVE_LIMITS.manifestBytes+1,
  root=>root.previous=root.current,root=>root.private_setting='unexpected',
 ])await t.test(String(mutation),async()=>{const storage=server();mutation(storage.root);await assert.rejects(loader(storage).load());});
 await assert.rejects(loader(server(),{limits:{...LIVE_LIMITS,totalBytes:1}}).load());
 await assert.rejects(loader(server(),{limits:{...LIVE_LIMITS,rootBytes:16}}).load());
 for(const baseUrl of ['file:///data/','https://user:secret@example.test/data/','https://example.test/data/?source=other']){
  assert.throws(()=>createReleaseLoader({baseUrl,fetch:()=>assert.fail('unsafe-base fetch'),subtle:webcrypto.subtle}),/Invalid public data base/);
 }
});

test('response bytes are bounded independently of Content-Length',async()=>{
 let cancelled=false;
 const storage=server();storage.fetch=async()=>new Response(new ReadableStream({start(controller){controller.enqueue(new Uint8Array(100));},cancel(){cancelled=true;}}),{headers:{'Content-Length':'1'}});
 await assert.rejects(loader(storage,{limits:{...LIVE_LIMITS,rootBytes:32}}).load());assert.equal(cancelled,true);
});

test('hashes cover decoded object bytes, not encoded transfer length',async()=>{
 const storage=server(),fetch=storage.fetch;
 storage.fetch=async(url,options)=>{const response=await fetch(url,options);response.headers.set('Content-Length','1');response.headers.set('Content-Encoding','gzip');return response;};
 assert.equal((await loader(storage).load()).descriptor.id,'current');
});

test('redirects and a different response URL are rejected',async()=>{
 for(const response of [new Response('redirect',{status:302}),Object.defineProperty(new Response('{}'),'url',{value:'https://bad.test/data.json'})]){
  await assert.rejects(loader({fetch:async()=>response}).load());
 }
});

test('request and candidate deadlines bound noncooperating fetch/body work',async()=>{
 const late=deferred();let cancelled=false;
 const storage={fetch:()=>late.promise};
 const loading=loader(storage,{limits:{...LIVE_LIMITS,requestMs:15,candidateMs:25}}).load();
 await assert.rejects(loading,/timed out/);
 late.resolve(new Response(new ReadableStream({cancel(){cancelled=true;}})));await pause(0);assert.equal(cancelled,true);
 const stalled=server();stalled.fetch=async()=>new Response(new ReadableStream({cancel(){cancelled=true;}}));
 await assert.rejects(loader(stalled,{limits:{...LIVE_LIMITS,requestMs:15}}).load(),/timed out/);
 const timed=server(),fetch=timed.fetch;
 timed.fetch=async(url,options)=>{if(!url.endsWith('/manifest.json'))await pause(30);return fetch(url,options);};
 await assert.rejects(loader(timed,{limits:{...LIVE_LIMITS,requestMs:100,candidateMs:10}}).load());
});

test('parallel object fetches stay within the six-worker budget',async()=>{
 const storage=server(),fetch=storage.fetch;let running=0,peak=0;
 storage.fetch=async(url,options)=>{running++;peak=Math.max(peak,running);try{await pause(1);return await fetch(url,options);}finally{running--;}};
 await loader(storage).load();assert.equal(peak,6);
});

test('mixed reuse and downloads stay inside the existing network concurrency limit',async()=>{
 const next=fixture('next',{dataChange:data=>data.generated_at='new generation',objectChange:objects=>{
  for(const [name,bytes] of objects)if(name.endsWith('-1mo.csv'))objects.set(name,Buffer.concat([bytes,Buffer.from('\n')]));
 }});
 const storage=server([current,next]),active=await loader(storage).load(),fetch=storage.fetch;
 let running=0,peak=0;
 storage.fetch=async(url,options)=>{running++;peak=Math.max(peak,running);try{await pause(2);return await fetch(url,options);}finally{running--;}};
 storage.root={...storage.root,current:next.descriptor,previous:current.descriptor};storage.calls.length=0;
 const result=await loader(storage).load(active);
 assert.equal(result.objects.size,defaultObjectCount);assert.equal(storage.calls.length,16);
 assert.ok(peak>1&&peak<=LIVE_LIMITS.concurrency);
 assert.ok(storage.calls.every(call=>!call.key.endsWith('-all-time.csv')&&!call.key.includes('/reference/')));
});

function controllerBrowser(){
 const timers=new Map(),events=new Map(),documentEvents=new Map(),revoked=[],created=[];
 let timerId=0;
 const browser={location:{href:'https://example.test/onionsurge/'},crypto:webcrypto,Blob,
  URL:{createObjectURL(blob){const url='blob:test/'+created.length;created.push({url,blob});return url;},revokeObjectURL(url){revoked.push(url);}},
  document:{visibilityState:'visible',documentElement:{dataset:{mode:'live'}},page:{textContent:''},
   getElementById(){return this.page;},addEventListener(type,fn){documentEvents.set(type,fn);},removeEventListener(type){documentEvents.delete(type);}},
  setInterval(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearInterval(id){timers.delete(id);},addEventListener(type,fn){events.set(type,fn);}};
 return {browser,timers,events,documentEvents,revoked,created};
}

test('classic browser scripts share a scope and bootstrap a verified daily dashboard',{timeout:10000},async t=>{
 const storage=server(),environment=controllerBrowser(),ready=deferred();
 let html='';
 const page={
  get innerHTML(){return html;},
  set innerHTML(value){html=value;if(value.includes('class="chart-stack'))ready.resolve();},
  set textContent(value){ready.reject(new Error('Live bootstrap failed: '+value));}
 };
 Object.assign(environment.browser.location,{search:'',hash:''});
 Object.assign(environment.browser.document,{
  page,
  querySelector:selector=>selector==='#app'?page:null,
  querySelectorAll:()=>[],
 });
 Object.assign(environment.browser,{fetch:storage.fetch,navigator:{},scrollTo(){}});
 t.after(()=>environment.events.get('pagehide')?.({persisted:false}));
 const context=vm.createContext({window:environment.browser,document:environment.browser.document,
  URL,URLSearchParams,TextDecoder,Uint8Array,AbortController,setTimeout,clearTimeout});
 // Deliberately execute the unwrapped files as classic scripts in one global
 // scope. CommonJS require() would hide the browser's lexical collisions.
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/app.js'),'utf8'),context,{filename:'app.js'});
 const factory=vm.runInContext('createObservatory',context);
 assert.equal(typeof factory,'function');
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/live.js'),'utf8'),context,{filename:'live.js'});
 assert.equal(vm.runInContext('createObservatory',context),factory,'the app factory remains available');
 await ready.promise;
 assert.match(html,/Observed through/);
 assert.match(html,/Updated hourly/);
 assert.match(html,/data-all-range="24h"/);
 assert.match(html,/data-hero-range="24h"/);
 assert.match(html,/Last complete hour/);
 assert.match(html,/24-hour average/);
 assert.equal(storage.calls.length,defaultObjectCount+2,'root, manifest and every release object were requested');
 assert.equal(environment.created.length,defaultObjectCount+1,'verified release objects and manifest received Blob downloads');
 assert.equal(vm.runInContext('typeof createReleaseLoader',context),'undefined','loader declarations stay private');
});
function verified(id){return {descriptor:{id,manifest_sha256:'a'.repeat(64),manifest_bytes:2},manifest:{generated_at:source.end+60},
 manifestBytes:Buffer.from('{}'),objects:new Map([['data.json',Buffer.from('{}')]]),data:{id},fallback:false};}
function renderer(){const calls=[];return {calls,make(data,browser,context){const app={data,context,
 start(){calls.push(['start',data.id]);},replaceRelease(next,nextContext){calls.push(['replace',next.id]);app.data=next;app.context=nextContext;},
 updateFreshness(status){calls.push(['status',status]);}};calls.push(['make',app]);return app;}};}

test('refresh is single-flight and keeps an old verified view/resources on failure',async()=>{
 const environment=controllerBrowser(),render=renderer(),pending=deferred();let calls=0,mode='first';
 const read={load(){calls++;if(mode==='first')return pending.promise;throw new Error('broken refresh');}};
 const controller=createLiveController(environment.browser,render.make,{loader:read});
 const one=controller.refresh(),two=controller.refresh();assert.equal(one,two);await pause(0);assert.equal(calls,1);
 pending.resolve(verified('one'));assert.equal(await one,true);const urls=environment.created.map(item=>item.url);
 mode='fail';assert.equal(await controller.refresh(),false);assert.equal(controller.getState().releaseId,'one');
 assert.equal(controller.getState().failed,true);assert.deepEqual(environment.revoked,[]);
 assert.equal(render.calls.filter(call=>call[0]==='start').length,1);
 controller.stop();assert.deepEqual(environment.revoked.sort(),urls.sort());assert.equal(environment.timers.size,0);
});

test('success replaces one controller atomically and retires bounded old resources',async()=>{
 const environment=controllerBrowser(),render=renderer();let next='one';
 const controller=createLiveController(environment.browser,render.make,{loader:{async load(){return verified(next);}}});
 await controller.refresh();const first=environment.created.map(item=>item.url);
 next='two';await controller.refresh();assert.deepEqual(environment.revoked,[]);
 next='three';await controller.refresh();assert.deepEqual(environment.revoked.sort(),first.sort());
 assert.equal(render.calls.filter(call=>call[0]==='make').length,1);assert.equal(controller.getState().releaseId,'three');
 controller.stop();assert.equal(environment.revoked.length,environment.created.length);
});

test('render failure disposes only rejected resources and preserves active identity',async()=>{
 const environment=controllerBrowser(),render=renderer();let next='one';
 const controller=createLiveController(environment.browser,render.make,{loader:{async load(){return verified(next);}}});
 await controller.refresh();const app=render.calls.find(call=>call[0]==='make')[1];
 const oldUrls=[...app.context.downloads.values()];
 app.replaceRelease=()=>{throw new Error('render failure');};next='two';assert.equal(await controller.refresh(),false);
 assert.equal(controller.getState().releaseId,'one');assert.ok(oldUrls.every(url=>!environment.revoked.includes(url)));
 assert.equal(environment.revoked.length,2,'only the rejected data and manifest URLs are retired');
 assert.equal(app.context.downloads.size,2);controller.stop();
});

test('visible timer refreshes every 300 seconds and returning from hidden can refresh',async()=>{
 const environment=controllerBrowser(),render=renderer();let now=0,calls=0;
 const controller=createLiveController(environment.browser,render.make,{now:()=>now,loader:{async load(){calls++;return verified('one');}}});
 await controller.refresh();const refresh=[...environment.timers.values()].find(timer=>timer.ms===300000);
 assert.ok(refresh,'refresh timer runs at 300s');
 environment.browser.document.visibilityState='hidden';now=400;refresh.fn();await pause(0);assert.equal(calls,1);
 environment.browser.document.visibilityState='visible';environment.documentEvents.get('visibilitychange')();await pause(0);assert.equal(calls,2);
 environment.documentEvents.get('visibilitychange')();await pause(0);assert.equal(calls,2);
 controller.stop();
});

test('stop prevents late publication and offline bootstrap makes no requests',async()=>{
 const environment=controllerBrowser(),render=renderer(),pending=deferred();
 const controller=createLiveController(environment.browser,render.make,{loader:{load:()=>pending.promise}});
 const loading=controller.refresh();controller.stop();pending.resolve(verified('late'));assert.equal(await loading,false);assert.equal(render.calls.length,0);
 environment.browser.document.documentElement.dataset.mode='offline';
 environment.browser.fetch=()=>{throw new Error('offline fetch');};assert.equal(startLiveObservatory(environment.browser,render.make),null);
});

test('live failure never displays accidental bundled sample data',async()=>{
 const environment=controllerBrowser(),render=renderer();
 environment.browser.ONIONSURGE_DATA=source;
 environment.browser.fetch=async()=>new Response('unavailable',{status:503});
 const controller=startLiveObservatory(environment.browser,render.make);
 assert.equal(await controller.refresh(),false);assert.equal(render.calls.length,0);
 assert.match(environment.browser.document.page.textContent,/Verified measurements are unavailable/);controller.stop();
});

test('window_seconds, invalid non-null current value, and subnormal comparison checks work properly',async()=>{
 for(const patch of [
  data=>{data.window_seconds=86400;},
  data=>{data.window_seconds=1800;},
  data=>{
   const series=data.charts[0].series[0];
   series.history.daily=series.history.daily.slice(0,-1);
   if(series.relay_population)series.relay_population.day=null;
   withoutPopulation(series);withoutComparisons(series);
   series.current.day={value:24,relays:800,expected_relays:8000,coverage:0.1,valid:false};
  },
 ]){
  const candidate=fixture('bad-win',{dataChange:patch});
  const storage=server([candidate],candidate,null);
  await assert.rejects(loader(storage).load());
 }
 const subnormal=5e-324;
 const subCandidate=fixture('subnormal',{
  referenceChange:ref=>{
   const c=source.charts[0],s=c.series[0];
   ref.distributions[c.id][s.key].day=[[ref.window.end-86400,0]];
   ref.distributions[c.id][s.key].hour=[[ref.window.end-21600,0]];
  },
  dataChange:data=>{
   const c=data.charts[0],s=c.series[0],n=c.relays,end=data.end;
   s.history.daily=Array.from({length:8},(_,i)=>[end-(7-i)*86400,i===7?subnormal:0,n,1,true]);
   s.current.day={value:subnormal,relays:n,expected_relays:Math.max(8,n),coverage:1,valid:true};
   Object.assign(s.reference.day,{n:1,current_percentile:100,flat_zero:false});
   Object.assign(s.reference.hour,{n:1,current_percentile:100,flat_zero:false});
   withoutPopulation(s);
   syncHistoricalAverages(s,data.time_ranges.periods,end);
   s.historical_averages.seven_days.display='<0.01';
   s.historical_averages.seven_days.comparison={
    method:'daily-per-relay-midrank-v1',cutoff:data.reference_cutoff,n:7,
    start:end-8*86400,end:data.reference_cutoff,current_percentile:100,flat_zero:false,
   };
  },
 });
 const subLoaded=await loader(server([subCandidate],subCandidate,null)).load();
 const loadedComp=subLoaded.data.charts[0].series[0].historical_averages.seven_days.comparison;
 assert.equal(loadedComp.flat_zero,false);
 assert.equal(loadedComp.current_percentile,100);
});

test('time_ranges validation rejects invalid bounds, sub-24h tokens and bad provenance',()=>{
 const hourPeriod=d=>d.time_ranges.periods.find(p=>p.window.unit==='hours');
 const dayPeriod=d=>d.time_ranges.periods.find(p=>p.window.unit==='days');
 const monPeriod=d=>d.time_ranges.periods.find(p=>p.window.unit==='months');
 for(const mutate of [
  d=>{delete d.time_ranges;},
  d=>{d.time_ranges.version=2;},
  d=>{d.time_ranges.extra=true;},
  d=>{d.time_ranges.periods=[];},
  d=>{d.time_ranges.periods=Array.from({length:17},(_,i)=>({...structuredClone(dayPeriod(d)),id:`p${i}`,summary_key:`p_${i}`,export_suffix:`p${i}`}));},
  d=>{d.time_ranges.periods=d.time_ranges.periods.filter(p=>p.window.unit!=='all_time');d.time_ranges.card_slots.fixed=['24h','6mo'];},
  d=>{const dup=structuredClone(d.time_ranges.periods.at(-1));dup.id='all2';dup.summary_key='all_two';dup.export_suffix='all-two';d.time_ranges.periods.push(dup);},
  d=>{const dup=structuredClone(hourPeriod(d));dup.id='h2';dup.summary_key='hours_two';dup.export_suffix='h2';d.time_ranges.periods.push(dup);},
  d=>{hourPeriod(d).id='12h';},
  d=>{hourPeriod(d).export_suffix='12h';},
  d=>{hourPeriod(d).window.count=12;},
  d=>{hourPeriod(d).window.comparison_lag_days=1;},
  d=>{hourPeriod(d).minimum_coverage=0.5;},
  d=>{d.time_ranges.card_slots.fixed=['6mo','all'];},
  d=>{d.time_ranges.legacy_aliases['24h']='1mo';},
  d=>{dayPeriod(d).id='24h';},
  d=>{dayPeriod(d).id='1d';},
  d=>{dayPeriod(d).id='1h';},
  d=>{dayPeriod(d).id='30m';},
  d=>{dayPeriod(d).id='60s';},
  d=>{dayPeriod(d).id='1m';},
  d=>{dayPeriod(d).id='foo-24h';},
  d=>{dayPeriod(d).export_suffix='24h';},
  d=>{dayPeriod(d).export_suffix='1h';},
  d=>{dayPeriod(d).export_suffix='30m';},
  d=>{dayPeriod(d).export_suffix='foo-24h';},
  d=>{dayPeriod(d).summary_key='day';},
  ...['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24'].flatMap(key=>[
   d=>{dayPeriod(d).summary_key=key;},
   d=>{d.time_ranges.legacy_aliases[key]='1mo';},
  ]),
  d=>{dayPeriod(d).label='   ';},
  d=>{dayPeriod(d).window.count=1;},
  d=>{dayPeriod(d).window.count=36501;},
  d=>{dayPeriod(d).window.comparison_lag_days=2;},
  d=>{monPeriod(d).window.count=0;},
  d=>{monPeriod(d).window.count=1201;},
  d=>{d.time_ranges.periods.at(-1).window.count=365;},
  d=>{d.time_ranges.periods.at(-1).window.comparison_lag_days=1;},
  d=>{dayPeriod(d).minimum_coverage=-0;},
  d=>{dayPeriod(d).minimum_coverage=-0.1;},
  d=>{dayPeriod(d).minimum_coverage=1.1;},
  d=>{monPeriod(d).minimum_coverage=-0;},
  d=>{monPeriod(d).minimum_coverage=-0.1;},
  d=>{monPeriod(d).minimum_coverage=1.1;},
  d=>{d.time_ranges.periods.at(-1).minimum_coverage=0.5;},
  d=>{d.time_ranges.periods.at(-1).minimum_coverage=-0;},
  d=>{dayPeriod(d).minimum_comparison_windows=0;},
  d=>{dayPeriod(d).minimum_comparison_windows=10001;},
  d=>{d.time_ranges.default_range='missing';},
  d=>{dayPeriod(d).label='🚀'.repeat(81);},
  d=>{dayPeriod(d).average_label='🚀'.repeat(81);},
  d=>{dayPeriod(d).plot_label='🚀'.repeat(81);},
  d=>{dayPeriod(d).label='\uFEFF';},
  d=>{dayPeriod(d).average_label='\x85';},
  d=>{dayPeriod(d).plot_label='\x1f';},
  d=>{d.time_ranges.legacy_aliases['🚀'.repeat(33)]='1mo';},
  d=>{d.time_ranges.legacy_aliases['7d']='1mo';},
  d=>{d.time_ranges.legacy_aliases['   ']='1mo';},
  d=>{d.time_ranges.legacy_aliases['\uFEFF']='1mo';},
  d=>{d.time_ranges.legacy_aliases['\x85']='1mo';},
  d=>{d.time_ranges.legacy_aliases['\x1c']='1mo';},
  d=>{d.time_ranges.legacy_aliases['legacy_missing']='missing';},
  d=>{d.time_ranges.card_slots.fixed=['6mo','6mo'];},
  d=>{d.time_ranges.card_slots.fallback_dynamic='24h';},
  d=>{d.provenance.time_ranges_sha256='bad';},
  d=>{d.provenance.time_ranges_method_sha256='bad';},
 ]){
  const data=structuredClone(current.data);
  mutate(data);
  assert.throws(()=>checkData(data,current.manifest),/time_ranges|all_time|minimum_coverage|identity/);
 }
 const unicodeData=structuredClone(current.data);
 dayPeriod(unicodeData).label='🚀'.repeat(80);
 dayPeriod(unicodeData).average_label='🚀'.repeat(80);
 dayPeriod(unicodeData).plot_label='🚀'.repeat(80);
 unicodeData.time_ranges.legacy_aliases['🚀'.repeat(32)]='7d';
 assert.equal(checkData(unicodeData,current.manifest),unicodeData,
  'supplementary Unicode code-point counts match Python len() at 80 and 32 boundary');
 assert.throws(()=>validateTimeRanges(JSON.parse('{"version":1,"default_range":"7d","legacy_aliases":{"__proto__":"7d"},"card_slots":{"fallback_dynamic":"7d","fixed":["6mo","all"]},"periods":[]}')),/Invalid time_ranges/);
 for(const cov of [0,0.9]){
  const validCov=withBoundedMinimumCoverage(structuredClone(current.data),cov);
  assert.equal(checkData(validCov,current.manifest),validCov,`checkData accepts minimum_coverage=${cov} on days/months`);
 }
 for(const cid of ['hsdir_fetches','directory_rejections','protocol_closes']){
  const ch=current.data.charts.find(c=>c.id===cid);
  const prim=ch.series.find(s=>s.key===ch.primary_series)||ch.series[0];
  for(const p of current.data.time_ranges.periods){
   if(p.window.unit==='hours')continue;
   const avg=prim.historical_averages[p.summary_key];
   assert.equal(avg.status,'ready',`${cid} ${p.id} is ready in rebuilt data`);
   assert.ok(avg.days>0&&Number.isFinite(avg.value));
  }
 }
});

test('custom arbitrary time_ranges and up to 512 manifest objects load with zero code edits',async()=>{
 const customRanges={
  version:1,default_range:'14d',legacy_aliases:{'24h':'14d'},
  card_slots:{fallback_dynamic:'14d',fixed:['2mo','all']},
  periods:[
   {id:'14d',summary_key:'fourteen_days',export_suffix:'14d',label:'14 days',average_label:'Last 14 days average',plot_label:'Last 14 days',window:{unit:'days',count:14,comparison_lag_days:1},minimum_coverage:0.9,minimum_comparison_windows:12},
   {id:'2mo',summary_key:'two_months',export_suffix:'2mo',label:'2 months',average_label:'Last 2 months average',plot_label:'Last 2 months',window:{unit:'months',count:2,comparison_lag_days:0},minimum_coverage:0.9,minimum_comparison_windows:30},
   {id:'all',summary_key:'all_time',export_suffix:'all-time',label:'All time',average_label:'All-time average',plot_label:'All time',window:{unit:'all_time',count:null,comparison_lag_days:0},minimum_coverage:0,minimum_comparison_windows:30},
  ],
 };
 const customCandidate=fixture('custom-ranges',{
  dataChange:data=>{
   data.time_ranges=customRanges;
   for(const chart of data.charts)for(const series of chart.series){
    withoutPopulation(series);
    syncHistoricalAverages(series,customRanges.periods,data.end);
   }
  },
 });
 const customResult=await loader(server([customCandidate],customCandidate,null)).load();
 assert.equal(customResult.objects.size,2+source.charts.length*3);
 assert.deepEqual(customResult.data.time_ranges.periods.map(p=>p.id),['14d','2mo','all']);

 // Parent window validation applies even when comparison is omitted (e.g. 14d cloned from all_time).
 for(const mutate of [
  s=>{s.historical_averages.fourteen_days={...structuredClone(s.historical_averages.all_time),minimum_coverage:0.9};},
  s=>{s.historical_averages.fourteen_days.start+=86400;},
  s=>{s.historical_averages.fourteen_days.end-=86400;},
  s=>{s.historical_averages.fourteen_days.days--;},
  s=>{s.historical_averages.fourteen_days.expected_days++;},
  s=>{s.historical_averages.fourteen_days.coverage=0.5;},
  s=>{s.historical_averages.fourteen_days.minimum_coverage=0.5;},
  s=>{s.historical_averages.fourteen_days.status='insufficient_data';s.historical_averages.fourteen_days.value=null;},
 ]){
  const badCustom=structuredClone(customCandidate.data);
  mutate(badCustom.charts[0].series[0]);
  assert.throws(()=>checkData(badCustom,customCandidate.manifest),/parent window mismatch/);
 }

 // 16 periods x 13 charts + data.json + reference = 210 objects (well above legacy 130 cap, <= 512).
 const manyPeriods=Array.from({length:15},(_,i)=>({
  id:`d${i+2}`,summary_key:`days_${i+2}`,export_suffix:`d${i+2}`,label:`${i+2} days`,
  average_label:`Last ${i+2} days average`,plot_label:`Last ${i+2} days`,
  window:{unit:'days',count:i+2,comparison_lag_days:0},minimum_coverage:0.9,minimum_comparison_windows:10,
 }));
 manyPeriods.push(structuredClone(source.time_ranges.periods.at(-1)));
 const manyCandidate=fixture('many-periods',{
  dataChange:data=>{
   data.time_ranges={version:1,default_range:'d2',legacy_aliases:{},card_slots:{fallback_dynamic:'d2',fixed:['d3','all']},periods:manyPeriods};
   for(const chart of data.charts)for(const series of chart.series){
    withoutPopulation(series);
    syncHistoricalAverages(series,manyPeriods,data.end);
   }
  },
 });
 const manyResult=await loader(server([manyCandidate],manyCandidate,null)).load();
 assert.equal(manyResult.objects.size,2+source.charts.length*16);

 const overCap=fixture('over-cap',{
  manifestChange:manifest=>{
   for(let i=0;i<500;i++)manifest.objects[`exports/extra_chart-p${i}.csv`]=entry(Buffer.from('x'));
  },
 });
 await assert.rejects(loader(server([overCap],overCap,null)).load(),/Invalid release objects/);
});

test('schema_version 6 is admitted on root.current while v5 stays restricted to cold previous fallback',async()=>{
 const v5Previous=fixture('v5-prev',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>{
   Object.assign(data,toV6Data(data));
   data.schema_version=5;
   delete data.time_ranges;
   delete data.provenance.time_ranges_sha256;
   delete data.provenance.time_ranges_method_sha256;
   for(const chart of data.charts)for(const series of chart.series){
    withoutPopulation(series);
    syncHistoricalAverages(series,LEGACY_V5_TIME_RANGES.periods,data.end);
   }
  },
 });
 assert.equal(v5Previous.manifest.data_schema_version,5);
 assert.equal(v5Previous.objects.size,2+source.charts.length*2);
 const v6Release=fixture('v6-rel',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>Object.assign(data,toV6Data(data)),
 });
 assert.equal(v6Release.manifest.data_schema_version,6);
 assert.equal(v6Release.objects.size,2+source.charts.length*6);
 const brokenV7=fixture('v7-bad');
 for(const [label,prev,expectedVer,expectedSize] of [
  ['v5',v5Previous,5,2+source.charts.length*2],
  ['v6',v6Release,6,2+source.charts.length*6],
 ]){
  const storage=server([brokenV7,prev],brokenV7,prev);
  storage.files.delete('releases/v7-bad/exports/transport_rejections-24h.csv');
  const fallbackResult=await loader(storage).load();
  assert.equal(fallbackResult.descriptor.id,prev.descriptor.id);
  assert.equal(fallbackResult.fallback,true);
  assert.equal(fallbackResult.data.schema_version,expectedVer);
  assert.equal(fallbackResult.objects.size,expectedSize);
 }
 // Primary v5 stays rejected, while primary v6 is admitted cleanly for zero-downtime v6->v7 rollout.
 await assert.rejects(loader(server([v5Previous],v5Previous,null)).load(),/Unsupported release manifest/,'v5 rejected as root.current');
 const v6Storage=server([v6Release],v6Release,null),v6Read=loader(v6Storage);
 const v6Primary=await v6Read.load();
 assert.equal(v6Primary.fallback,false);
 assert.equal(v6Primary.data.schema_version,6);
 assert.equal(v6Primary.objects.size,2+source.charts.length*6);
 v6Storage.calls.length=0;
 const v6Reused=await v6Read.load(v6Primary);
 assert.equal(v6Reused.fallback,false);
 assert.equal(v6Reused.descriptor.id,'v6-rel');
 assert.deepEqual(v6Storage.calls.map(c=>c.key),['manifest.json']);
 // Reject schema 6 if observation_end is non-midnight or if it injects an hours period or hourly keys.
 const v6NonMid=fixture('v6-non-mid',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>{Object.assign(data,toV6Data(data));},
  manifestChange:m=>{m.observation_end+=3600;},
 });
 await assert.rejects(loader(server([v6NonMid],v6NonMid,null)).load(),/Invalid release provenance/);
 const v6HoursData=structuredClone(v6Release.data);
 v6HoursData.time_ranges=structuredClone(source.time_ranges);
 assert.throws(()=>checkData(v6HoursData,v6Release.manifest),/Sub-24h time_ranges/);
 // Reject v5 if it injects unexpected time_ranges.
 const badV5=structuredClone(v5Previous.data);
 badV5.time_ranges=structuredClone(source.time_ranges);
 assert.throws(()=>checkData(badV5,v5Previous.manifest),/Unexpected time_ranges/);
 // Reject export path collision even if extra object balances object count.
 const colliding=fixture('collide',{
  exportSources:{connection:source.charts[0].id,'connection-rate':source.charts[1].id},
  dataChange:data=>{
   data.charts[0].id='connection';
   data.charts[1].id='connection-rate';
   for(const f of data.families){
    f.children=f.children.map(id=>id===source.charts[0].id?'connection':id===source.charts[1].id?'connection-rate':id);
    if(f.primary===source.charts[0].id)f.primary='connection';
    if(f.primary===source.charts[1].id)f.primary='connection-rate';
   }
   data.time_ranges={
    version:1,default_range:'24h',legacy_aliases:{},
    card_slots:{fallback_dynamic:'7d',fixed:['24h','all']},
    periods:[
     structuredClone(source.time_ranges.periods[0]),
     {id:'7d',summary_key:'seven_days',export_suffix:'7d',label:'7d',average_label:'7d',plot_label:'7d',window:{unit:'days',count:7,comparison_lag_days:0},minimum_coverage:0.9,minimum_comparison_windows:6},
     {id:'rate7d',summary_key:'rate_seven_days',export_suffix:'rate-7d',label:'r7d',average_label:'r7d',plot_label:'r7d',window:{unit:'days',count:14,comparison_lag_days:0},minimum_coverage:0.9,minimum_comparison_windows:6},
     structuredClone(source.time_ranges.periods.at(-1)),
    ],
   };
   for(const c of data.charts)for(const s of c.series){
    withoutPopulation(s);
    syncHistoricalAverages(s,data.time_ranges.periods,data.end);
   }
  },
 });
 colliding.manifest.objects['exports/extra-7d.csv']=entry(Buffer.from('x'));
 assert.throws(()=>checkData(colliding.data,colliding.manifest),/Incomplete release object set/);
});

test('non-midnight hourly schema 7 release and hyphenated -24h.csv exports validate cleanly',async()=>{
 const nonMid=fixture('non-mid-v7',{
  exportSources:{'connection-rate':source.charts[0].id},
  referenceChange:ref=>{
   ref.distributions['connection-rate']=ref.distributions[source.charts[0].id];
   delete ref.distributions[source.charts[0].id];
  },
  dataChange:data=>{
   const shift=13*3600,dayEnd=data.end;
   data.end=dayEnd+shift;data.start=data.end-86400;
   data.charts[0].id='connection-rate';
   for(const f of data.families){
    f.children=f.children.map(id=>id===source.charts[0].id?'connection-rate':id);
    if(f.primary===source.charts[0].id)f.primary='connection-rate';
   }
   for(const c of data.charts){
    c.start=data.start;c.end=data.end;
    for(const s of c.series){
     withoutPopulation(s);
     s.values=s.values.map(([t,v])=>[t+shift,v]);
     s.hourly_relays=s.hourly_relays.map(([t,v])=>[t+shift,v]);
     s.hourly_expected_relays=s.hourly_expected_relays.map(([t,v])=>[t+shift,v]);
     s.current.hour.value=s.values[23][1];s.current.hour.relays=s.hourly_relays[23][1];
     s.current.hour.expected_relays=s.hourly_expected_relays[23][1];
     s.current.hour.coverage=s.current.hour.relays!=null?s.current.hour.relays/s.current.hour.expected_relays:null;
     s.current.hour.valid=s.current.hour.value!==null&&s.current.hour.relays>=8&&s.current.hour.coverage>=0.9;
    }
   }
  },
 });
 const loaded=await loader(server([nonMid],nonMid,null)).load();
 assert.equal(loaded.manifest.data_schema_version,7);
 assert.ok(loaded.objects.has('exports/connection-rate-24h.csv'));
 assert.equal(loaded.data.end%86400,13*3600);
});

test('warm refresh does not promote a schema-5 active fallback bundle to current release status',async()=>{
 const v5Previous=fixture('v5-active',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>{
   Object.assign(data,toV6Data(data));
   data.schema_version=5;
   delete data.time_ranges;
   delete data.provenance.time_ranges_sha256;
   delete data.provenance.time_ranges_method_sha256;
   for(const chart of data.charts)for(const series of chart.series){
    withoutPopulation(series);
    syncHistoricalAverages(series,LEGACY_V5_TIME_RANGES.periods,data.end);
   }
  },
 });
 const brokenV7=fixture('v7-broken');
 const storage=server([brokenV7,v5Previous],brokenV7,v5Previous);
 storage.files.delete('releases/v7-broken/exports/transport_rejections-24h.csv');
 const read=loader(storage);
 const environment=controllerBrowser(),render=renderer();
 const controller=createLiveController(environment.browser,render.make,{loader:read});
 assert.equal(await controller.refresh(),true);
 assert.equal(controller.getState().releaseId,'v5-active');
 assert.equal(controller.getState().fallback,true);
 assert.equal(controller.getState().failed,false);
 const activeBundle=await read.load();
 assert.equal(activeBundle.fallback,true);
 // Now promote the v5 descriptor to root.current; warm refresh must reject it rather than clearing fallback.
 storage.root={...storage.root,current:structuredClone(v5Previous.descriptor),previous:null};
 await assert.rejects(read.load(activeBundle),/Unsupported release manifest/);
 assert.equal(await controller.refresh(),false);
 assert.equal(controller.getState().releaseId,'v5-active');
 assert.equal(controller.getState().fallback,true);
 assert.equal(controller.getState().failed,true);
 const lastStatus=render.calls.filter(c=>c[0]==='status').at(-1)[1];
 assert.deepEqual(lastStatus,{failed:true});
 controller.stop();
});

test('optional query_contract_sha256 provenance matches verified manifest when present',async()=>{
 const contractHash='d'.repeat(64);
 const matching=fixture('contract-match',{dataChange:d=>{d.provenance.query_contract_sha256=contractHash;}});
 assert.equal(matching.manifest.query_contract_sha256,contractHash);
 const loadedMatch=await loader(server([matching],matching,null)).load();
 assert.equal(loadedMatch.data.provenance.query_contract_sha256,contractHash);

 const omitted=fixture('contract-omitted',{dataChange:d=>{delete d.provenance.query_contract_sha256;}});
 const loadedOmitted=await loader(server([omitted],omitted,null)).load();
 assert.equal(loadedOmitted.data.provenance.query_contract_sha256,undefined);

 for(const badValue of ['bad',null,123,'A'.repeat(64),'e'.repeat(64)]){
  const invalidData=structuredClone(matching.data);
  invalidData.provenance.query_contract_sha256=badValue;
  assert.throws(()=>checkData(invalidData,matching.manifest),/Invalid query contract identity/);
 }

 const mismatched=fixture('contract-mismatch',{
  dataChange:d=>{d.provenance.query_contract_sha256=contractHash;},
  manifestChange:m=>{m.query_contract_sha256='e'.repeat(64);},
 });
 await assert.rejects(loader(server([mismatched],mismatched,null)).load(),/Invalid query contract identity/);
});

test('signed negative zero (-0) is rejected across live data, reference and time_ranges checks',()=>{
 assert.deepEqual(Object.is(parseLiveJson(Buffer.from('[-0]'))[0],-0),true,'raw parser keeps parseLiveJson([-0]) contract');
  for(const [label,dataChange,rx] of [
  ['history value',d=>{d.charts[0].series[0].history.daily[0][1]=-0;},/Invalid observation value/],
  ['history relays',d=>{d.charts[0].series[0].history.daily[0][2]=-0;},/Invalid historical observation/],
  ['history coverage',d=>{d.charts[0].series[0].history.daily[0][3]=-0;},/Invalid historical observation/],
  ['hourly value ts',d=>{d.charts[0].series[0].values[0][0]=-0;},/Invalid hourly observation timestamp/],
  ['hourly value',d=>{d.charts[0].series[0].values[0][1]=-0;},/Invalid hourly observation values/],
  ['hourly relays',d=>{const s=d.charts[0].series[0];s.hourly_relays[0][1]=-0;s.values[0][1]=null;},/Invalid hourly observation values/],
  ['current hour value',d=>{d.charts[0].series[0].current.hour.value=-0;},/Invalid current\/reference summary/],
  ['current hour relays',d=>{d.charts[0].series[0].current.hour.relays=-0;},/Invalid current\/reference summary/],
  ['current value',d=>{d.charts[0].series[0].current.day.value=-0;},/Invalid current\/reference summary/],
  ['current relays',d=>{d.charts[0].series[0].current.day.relays=-0;},/Invalid current\/reference summary/],
  ['expected relays',d=>{d.charts[0].series[0].current.day.expected_relays=-0;},/Invalid current denominator/],
  ['current coverage',d=>{const c=d.charts[0].series[0].current.day;c.relays=0;c.coverage=-0;c.valid=false;c.value=null;if(d.charts[0].series[0].relay_population)d.charts[0].series[0].relay_population.day=null;withoutComparisons(d.charts[0].series[0]);},/Invalid current coverage/],
  ['reference n',d=>{d.charts[0].series[0].reference.day.n=-0;},/Invalid current\/reference summary/],
  ['reference percentile',d=>{d.charts[0].series[0].reference.day.current_percentile=-0;},/Invalid current\/reference summary/],
  ['ready historical average',d=>{const a=Object.values(d.charts[0].series[0].historical_averages).find(x=>x.status==='ready');if(a)a.value=-0;},/Invalid ready average/],
  ['chart relays',d=>{d.charts[0].relays=-0;},/Invalid chart metadata/],
  ['data fleet',d=>{d.fleet=-0;},/Invalid coarsened data population/],
  ['relay population coverage',d=>{const s=d.charts[0].series.find(x=>x.relay_counts);if(s)s.relay_counts.day.coverage=-0;},/Invalid relay population coverage/],
 ]){
  const candidate=fixture('neg-zero',{dataChange});
  assert.throws(()=>checkData(candidate.data,candidate.manifest),rx,`checkData rejects -0 in ${label}`);
 }
 const validBounds=fixture('valid-partial-bounds',{dataChange:d=>{
  const s=d.charts.flatMap(c=>c.series).find(sr=>sr.relay_population&&sr.relay_counts?.day?.total>=16);
  assert.ok(s,'fixture has series with partial-bounds candidate');
  const sum=s.relay_counts.day;
  s.relay_population.day=[d.end,sum.total,8,0,0,0,0,0];
  sum.classified_windows=0;sum.classified_relay_windows=8;sum.coverage=8/sum.relay_windows;
  for(const r of ['guard','middle_only','exit','hsdir','v2dir'])sum[r]=null;
  sum.role_bounds={guard:[0,sum.total-8],middle_only:[0,sum.total-8],exit:[0,sum.total-8],hsdir:[0,sum.total-8],v2dir:[0,sum.total-8]};
 }});
 assert.ok(checkData(validBounds.data,validBounds.manifest),'valid partial bounds pass before -0 mutation');
 const badBoundsSeries=validBounds.data.charts.flatMap(c=>c.series).find(sr=>sr.relay_counts?.day?.role_bounds);
 assert.ok(badBoundsSeries);
 badBoundsSeries.relay_counts.day.role_bounds.guard[0]=-0;
 assert.throws(()=>checkData(validBounds.data,validBounds.manifest),/Invalid relay population bounds/);
 const negRef=structuredClone(originalReference);
 negRef.distributions[source.charts[0].id][source.charts[0].series[0].key].day[0][1]=-0;
 assert.throws(()=>checkReference(negRef,source),/Invalid observation value/);
 const dayIdx=tr=>tr.periods.findIndex(p=>p.window.unit==='days');
 const monIdx=tr=>tr.periods.findIndex(p=>p.window.unit==='months');
 for(const [patch,rx] of [
  [tr=>{tr.periods[dayIdx(tr)].window.comparison_lag_days=-0;},/time_ranges.*window/],
  [tr=>{tr.periods.at(-1).window.comparison_lag_days=-0;},/time_ranges.*window/],
  [tr=>{tr.periods[dayIdx(tr)].window.unit=['days'];},/Unsupported time_ranges window unit/],
  [tr=>{tr.periods[monIdx(tr)].window.unit=['months'];},/Unsupported time_ranges window unit/],
 ]){
  const tr=structuredClone(source.time_ranges);patch(tr);
  assert.throws(()=>validateTimeRanges(tr),rx);
 }

 // Signed -0 loopholes in checkHistoricalSeries on empty-history series without relay_counts.
 const makeEmptyHistCandidate=endTs=>{
  const c=fixture('hist-neg-zero',{
   dataChange:d=>{
    d.end=endTs;d.start=endTs-86400;d.reference_cutoff=d.start;
    d.provenance.reference_method_sha256=originalReference.method_sha256;
    d.time_ranges={
     version:1,default_range:'7d',legacy_aliases:{},card_slots:{fallback_dynamic:'7d',fixed:['14d','all']},
     periods:[
      {id:'7d',summary_key:'seven_days',export_suffix:'7d',label:'7d',average_label:'7d',plot_label:'7d',window:{unit:'days',count:7,comparison_lag_days:0},minimum_coverage:0,minimum_comparison_windows:1},
      {id:'14d',summary_key:'fourteen_days',export_suffix:'14d',label:'14d',average_label:'14d',plot_label:'14d',window:{unit:'days',count:14,comparison_lag_days:0},minimum_coverage:0,minimum_comparison_windows:1},
      {id:'all',summary_key:'all_time',export_suffix:'all-time',label:'All',average_label:'All',plot_label:'All',window:{unit:'all_time',count:null,comparison_lag_days:0},minimum_coverage:0,minimum_comparison_windows:1},
     ],
    };
    for(const chart of d.charts){
     chart.start=d.start;chart.end=d.end;
     const expected=Math.max(8,chart.relays);
     for(const s of chart.series){
      withoutPopulation(s);s.history.daily=[];s.history.sampled_hour=[];
      s.values=Array.from({length:24},(_,i)=>[d.start+(i+1)*3600,null]);
      s.hourly_relays=Array.from({length:24},(_,i)=>[d.start+(i+1)*3600,null]);
      s.hourly_expected_relays=Array.from({length:24},(_,i)=>[d.start+(i+1)*3600,expected]);
      s.current.day={value:null,relays:null,expected_relays:expected,coverage:null,valid:false};
      s.current.hour={value:null,relays:null,expected_relays:expected,coverage:null,valid:false};
      s.reference.day={n:0,current_percentile:null,flat_zero:false,mean_per_relay:null};
      s.reference.hour={n:0,current_percentile:null,flat_zero:false,mean_per_relay:null};
      syncHistoricalAverages(s,d.time_ranges.periods,d.end);
     }
    }
   },
  });
  c.manifest.observation_end=endTs;
  return c;
 };
 const epochZero=makeEmptyHistCandidate(86400);
 assert.ok(checkData(epochZero.data,epochZero.manifest));
 epochZero.data.charts[0].series[0].historical_averages.all_time.comparison={method:'daily-per-relay-midrank-v1',cutoff:-0,n:0,start:null,end:null,current_percentile:null,flat_zero:false};
 assert.throws(()=>checkData(epochZero.data,epochZero.manifest),/Invalid historical comparison method or cutoff/);

 for(const field of ['days','expected_days']){
  const cand=makeEmptyHistCandidate(86400*7);
  cand.data.charts[0].series[0].historical_averages.all_time[field]=-0;
  assert.throws(()=>checkData(cand.data,cand.manifest),/Historical comparison parent window mismatch/);
 }
 const zeroPeriodStart=makeEmptyHistCandidate(86400*7);
 zeroPeriodStart.data.charts[0].series[0].historical_averages.seven_days.start=-0;
 assert.throws(()=>checkData(zeroPeriodStart.data,zeroPeriodStart.manifest),/Historical comparison parent window mismatch/);

 const zeroCompStart=makeEmptyHistCandidate(86400*7);
 const cs=zeroCompStart.data.charts[0].series[0];
 cs.history.daily=[[86400,1,8,1,true]];
 syncHistoricalAverages(cs,zeroCompStart.data.time_ranges.periods,zeroCompStart.data.end);
 cs.historical_averages.seven_days.comparison={method:'daily-per-relay-midrank-v1',cutoff:86400*6,n:1,start:-0,end:86400,current_percentile:null,flat_zero:false};
 assert.throws(()=>checkData(zeroCompStart.data,zeroCompStart.manifest),/Historical comparison membership mismatch/);
});

test('pageshow bfcache restore refreshes stale releases and ignores persisted=false or stopped controllers',async()=>{
 const environment=controllerBrowser(),render=renderer();let now=0,calls=0,releaseId='one';
 const controller=createLiveController(environment.browser,render.make,{now:()=>now,loader:{async load(){calls++;return verified(releaseId);}}});
 await controller.refresh();
 assert.equal(calls,1);
 environment.events.get('pagehide')({persisted:true});
 assert.equal(environment.timers.size,2,'persisted pagehide keeps controller alive for bfcache');
 now=4000;releaseId='two';
 environment.events.get('pageshow')({persisted:false});
 await pause(0);
 assert.equal(calls,1,'non-persisted pageshow does not trigger bfcache refresh');
 environment.events.get('pageshow')({persisted:true});
 await pause(0);
 assert.equal(calls,2);
 assert.equal(controller.getState().releaseId,'two');
 controller.stop();
 now=9000;
 environment.events.get('pageshow')({persisted:true});
 await pause(0);
 assert.equal(calls,2,'stopped controller ignores late pageshow');
});

test('stale bfcache pageshow queues one follow-up refresh across pending success, failure and stop()',async()=>{
 const environment=controllerBrowser(),render=renderer(),pending=[];
 let now=0,calls=0;
 const controller=createLiveController(environment.browser,render.make,{now:()=>now,loader:{load(){calls++;const d=deferred();pending.push(d);return d.promise;}}});

 // Pre-restore pending refresh succeeds -> queues exactly one follow-up even on multiple pageshow events.
 const p1=controller.refresh();await pause(0);
 assert.equal(calls,1);
 environment.events.get('pagehide')({persisted:true});
 now=4000;
 environment.events.get('pageshow')({persisted:true});
 environment.events.get('pageshow')({persisted:true});
 pending[0].resolve(verified('one'));
 await p1;await pause(0);
 assert.equal(calls,2,'single follow-up starts once pending refresh settles');
 pending[1].resolve(verified('two'));
 await pause(0);
 assert.equal(controller.getState().releaseId,'two');

 // Pre-restore pending refresh fails -> follow-up still runs on settlement.
 now=8000;
 const p2=controller.refresh();await pause(0);
 assert.equal(calls,3);
 now=12000;
 environment.events.get('pageshow')({persisted:true});
 pending[2].reject(new Error('network blip'));
 await p2;await pause(0);
 assert.equal(calls,4,'follow-up runs even after pending failure');
 pending[3].resolve(verified('three'));
 await pause(0);
 assert.equal(controller.getState().releaseId,'three');

 // stop() cancels any queued follow-up before settlement.
 now=16000;
 const p3=controller.refresh();await pause(0);
 assert.equal(calls,5);
 now=20000;
 environment.events.get('pageshow')({persisted:true});
 controller.stop();
 pending[4].resolve(verified('four'));
 await p3;await pause(0);
 assert.equal(calls,5,'stopped controller does not launch queued follow-up');
});

test('daily schema_version 7 release (86400s) validates end-to-end and rejects hourly fields or non-midnight bounds',async()=>{
 const v7Daily=fixture('v7-daily',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>{
   Object.assign(data,toV6Data(data));
   data.schema_version=7;
  },
 });
 assert.equal(v7Daily.manifest.data_schema_version,7);
 assert.equal(v7Daily.data.window_seconds,86400);
 assert.equal(v7Daily.objects.size,2+source.charts.length*6);
 assert.ok(!v7Daily.objects.has('exports/transport_rejections-24h.csv'));

 const loaded=await loader(server([v7Daily],v7Daily,null)).load();
 assert.equal(loaded.fallback,false);
 assert.equal(loaded.data.schema_version,7);
 assert.equal(loaded.data.window_seconds,86400);
 assert.equal(loaded.objects.size,2+source.charts.length*6);

 // Non-midnight observation_end on an 86400s schema-7 release fails closed.
 const badMidnight=fixture('v7-daily-nonmid',{
  referenceChange:ref=>Object.assign(ref,toV6Ref(ref)),
  dataChange:data=>{
   Object.assign(data,toV6Data(data));
   data.schema_version=7;
   data.end+=3600;
   data.start+=3600;
  },
 });
 await assert.rejects(loader(server([badMidnight],badMidnight,null)).load(),/Invalid data window/);

 // Reject 24h hours period, hourly kinds, forbidden keys, and reference.hour when window_seconds === 86400.
 const badHoursTr=structuredClone(v7Daily.data);
 badHoursTr.time_ranges=structuredClone(source.time_ranges);
 assert.throws(()=>checkData(badHoursTr,v7Daily.manifest),/Sub-24h time_ranges/);

 const badKind=structuredClone(v7Daily.data);
 badKind.charts[0].kind='counter_hour_rate';
 assert.throws(()=>checkData(badKind,v7Daily.manifest),/Invalid chart metadata/);

 for(const badKey of ['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24']){
  const polluted=structuredClone(v7Daily.data);
  polluted[badKey]={};
  assert.throws(()=>checkData(polluted,v7Daily.manifest),/Sub-24h field in live JSON/);
 }

 const badRef=structuredClone(v7Daily.reference);
 badRef.distributions[v7Daily.data.charts[0].id][v7Daily.data.charts[0].series[0].key].hour=[];
 assert.throws(()=>checkReference(badRef,v7Daily.data),/Sub-24h field in live JSON/);
});
