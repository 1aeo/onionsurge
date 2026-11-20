'use strict';
// CPU-only renderer benchmark. Optional baseline comparisons permit only the
// source-credit change; no timing threshold is used as a correctness test.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const {createHash} = require('node:crypto');

const usage = 'Usage: node scripts/benchmark_charts.cjs [--baseline renderer.cjs] [--data data.json]';
const repository = path.resolve(__dirname, '..');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const sourceCredit = 'Source: 1aeo.com - OnionSurge';
let stage = 'arguments';

function normalizeCredit(html) {
  return html.replace(/(<text\b[^>]*\bclass="source">)Source: 1aeo\.com - OnionSurge(?=<\/text>)/g,
    '$1Source: 1aeo.com')
    .replace('<div class="no-data"><div>No retained samples available for this chart.<br><small>Source: 1aeo.com - OnionSurge</small></div></div>',
      '<div class="no-data">No retained samples available for this chart.</div>');
}

// Minimal DOM boundary: captures markup and SVG strings, without pretending to
// measure browser layout or paint. Full-redraw controls must exist in the output.
function context(ui, data, search = '', plotWidth = 1776) {
  const events = new Map();
  let markup = '', generation = 0, plots = [], heroPlots = [];
  const root = {
    get innerHTML() { return markup; },
    set innerHTML(value) {
      markup = value; generation++;
      plots = [...value.matchAll(/data-chart-plot="([^"]+)" data-range="([^"]+)"/g)]
        .map(([,chartPlot,range]) => ({dataset:{chartPlot,range},innerHTML:'',
          getBoundingClientRect:()=>({width:plotWidth})}));
      heroPlots = [...value.matchAll(/data-hero-plot="([^"]+)" data-hero-plot-range="([^"]+)"/g)]
        .map(([,heroPlot,heroPlotRange]) => ({dataset:{heroPlot,heroPlotRange},innerHTML:'',
          getBoundingClientRect:()=>({width:plotWidth})}));
    },
  };
  const browser = {
    location:{search,hash:''},navigator:{},history:{pushState(){}},
    document:{querySelector:selector=>selector==='#app'?root:null,
      querySelectorAll:selector=>selector==='[data-chart-plot]'?plots:selector==='[data-hero-plot]'?heroPlots:[],
      addEventListener:(name,callback)=>events.set(name,callback)},
    addEventListener(){},
  };
  const app = ui.createObservatory(data,browser);
  return {app,redrawRange(range){
    const safeRange = range.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    assert.match(markup,new RegExp('<button\\b[^>]*\\bdata-all-range="'+safeRange+'"'));
    const before = generation;
    events.get('click')({target:{closest:selector=>selector==='[data-all-range]'
      ?{dataset:{allRange:range}}:null}});
    assert.equal(generation,before+1);
  }};
}

function configuredRanges(data) {
  return data.time_ranges?.periods ? data.time_ranges.periods.map(period => period.id) : ['30d','all'];
}

function compare(baseline, current, data) {
  let svgCases = 0, markupCases = 0;
  const hashes = [], ranges = configuredRanges(data);
  for (const chart of data.charts) for (const range of ranges) {
    for (const series of [null,...chart.display_keys]) {
      const params = new URLSearchParams({view:'explorer',chart:chart.id,range});
      if (series) { params.set('series',series); params.set('band','1'); }
      const old = context(baseline,data,'?'+params).app;
      const next = context(current,data,'?'+params).app;
      assert.equal(next.markup(),old.markup()); markupCases++;
      for (const width of [240,262,1168,1776]) {
        const before = {innerHTML:'',getBoundingClientRect:()=>({width})};
        const after = {innerHTML:'',getBoundingClientRect:()=>({width})};
        old.draw(before,chart,range); next.draw(after,chart,range);
        assert.equal(normalizeCredit(after.innerHTML),normalizeCredit(before.innerHTML));
        assert.equal(after.innerHTML.split(sourceCredit).length-1,1);
        hashes.push(hash(normalizeCredit(before.innerHTML))); svgCases++;
      }
    }
  }
  for (const layout of ['full','grid']) for (const range of ranges) {
    const search = '?view=overview&layout='+layout+'&range='+encodeURIComponent(range);
    assert.equal(context(current,data,search).app.markup(),context(baseline,data,search).app.markup());
    markupCases++;
  }
  return {svg_cases:svgCases,markup_cases:markupCases,aggregate_svg_sha256:hash(hashes.join('\n')),
    normalization:'Only the source credit and the credited no-data wrapper'};
}

function measure(run) {
  for (let i=0;i<20;i++) run();
  const samples = [];
  for (let i=0;i<150;i++) {
    const start = performance.now(); run(); samples.push(performance.now()-start);
  }
  samples.sort((a,b)=>a-b);
  return {median_ms:+samples[75].toFixed(3),p95_ms:+samples[142].toFixed(3)};
}

function timings(ui, data) {
  const primaries = data.families.map(f=>data.charts.find(c=>c.id===f.primary));
  const ranges = configuredRanges(data), initialRange = data.time_ranges?.default_range || ranges[0];
  const search = '?view=overview&layout=full&range='+encodeURIComponent(initialRange);
  const ctx = context(ui,data,search); ctx.app.start();
  const element = {innerHTML:'',getBoundingClientRect:()=>({width:1776})};
  const result = {};
  for (const range of ranges) {
    result[range+'_svg'] = measure(()=>{for (const chart of primaries) ctx.app.draw(element,chart,range);});
  }
  if (typeof ctx.app.drawHero === 'function') {
    if (data.schema_version >= 7 || data.window_seconds === 3600) {
      result.hero_24h_svg = measure(()=>{ctx.app.drawHero(element,'24h');});
    }
    result.hero_7d_svg = measure(()=>{ctx.app.drawHero(element,'7d');});
    result.hero_all_svg = measure(()=>{ctx.app.drawHero(element,'all');});
  }
  for (const layout of ['full','grid']) for (const range of ranges) {
    const view = context(ui,data,'?view=overview&layout='+layout+'&range='+encodeURIComponent(range),layout==='grid'?853:1776);
    view.app.start();
    result[layout+'_'+range+'_redraw'] = measure(()=>view.redrawRange(range));
  }
  return result;
}

try {
  const args = process.argv.slice(2), options = new Map();
  if (args.length===1 && args[0]==='--help') { console.log(usage); process.exit(0); }
  for (let i=0;i<args.length;i+=2) {
    assert.ok(['--baseline','--data'].includes(args[i]) && args[i+1]
      && !args[i+1].startsWith('--') && !options.has(args[i]));
    options.set(args[i],path.resolve(args[i+1]));
  }
  stage = 'input';
  const input = fs.readFileSync(options.get('--data') || path.join(repository,'dist/data.json'));
  const data = JSON.parse(input);
  assert.equal(data.families.length,6); assert.ok(data.charts.length>0);
  if (data.schema_version >= 6 || data.time_ranges !== undefined) {
    assert.ok(Array.isArray(data.time_ranges?.periods) && data.time_ranges.periods.length > 0);
  }
  const currentPath = path.join(repository,'site/app.js'), current = require(currentPath);
  const result = {node:process.version,input_sha256:hash(input),
    current_source_sha256:hash(fs.readFileSync(currentPath)),iterations:150,warmup_iterations:20,
    limits:'CPU-only string generation; excludes DOM layout, paint and network. No timing threshold.'};
  if (options.has('--baseline')) {
    stage = 'baseline';
    const baselinePath = options.get('--baseline'), baseline = require(baselinePath);
    result.baseline_source_sha256 = hash(fs.readFileSync(baselinePath));
    stage = 'parity'; result.parity = compare(baseline,current,data);
    stage = 'baseline timing'; result.baseline = timings(baseline,data);
  }
  stage = 'current timing'; result.current = timings(current,data);
  console.log(JSON.stringify(result,null,2));
} catch {
  // Filenames and arbitrary fixture values may be private. Do not emit them.
  console.error(JSON.stringify({status:'failed',stage,message:stage==='arguments'?usage:'Benchmark check failed.'}));
  process.exitCode = 1;
}
