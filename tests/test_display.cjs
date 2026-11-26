'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const ui = require('../site/app.js');
const data = JSON.parse(fs.readFileSync(path.join(__dirname, '../dist/data.json'), 'utf8'));
const decode = text => text.replace(/&(amp|lt|gt|quot|#39);/g,
  (_, entity) => ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"})[entity]);

function toV7Data(src) {
  if (src.schema_version === 7) return structuredClone(src);
  const d = structuredClone(src);
  d.schema_version = 7;
  d.window_seconds = 3600;
  const p24 = {
    id: '24h', summary_key: 'twenty_four_hours', export_suffix: '24h',
    label: '24 hours', average_label: 'Last 24 hours', plot_label: '24-hour snapshot',
    window: {unit: 'hours', count: 24, comparison_lag_days: 0},
    minimum_coverage: 0, minimum_comparison_windows: 30,
  };
  const {'24h': _drop24, ...restAliases} = d.time_ranges.legacy_aliases || {};
  d.time_ranges = {
    version: 1, default_range: '24h', legacy_aliases: restAliases,
    card_slots: {fallback_dynamic: '6mo', fixed: ['24h', 'all']},
    periods: [p24, ...d.time_ranges.periods],
  };
  for (const c of d.charts) {
    c.kind = c.kind === 'gauge_daily_mean' ? 'gauge_hour_mean' : 'counter_hour_rate';
    const baseDaily = c.history_queries?.daily || {value: c.query, eligible: c.quality_query, available: c.query};
    c.history_queries = {
      daily: structuredClone(baseDaily),
      sampled_hour: {
        value: c.query.replace(/\[1d\]/g, '[1h]'),
        eligible: c.quality_query.replace(/\[1d\]/g, '[1h]'),
        available: baseDaily.available || c.query,
      },
      current24: structuredClone(baseDaily),
    };
    c.query = c.history_queries.sampled_hour.value;
    c.quality_query = c.history_queries.sampled_hour.eligible;
    if (c.builds && typeof c.builds === 'object') c.builds.scope = 'Latest complete hour';
    for (const s of c.series) {
      const dayCur = s.current.day;
      const exp = dayCur.expected_relays ?? Math.max(8, c.relays);
      const qRow = s.history.daily.slice().reverse().find(r => r[4]);
      const vals = [], hRel = [], hExp = [];
      for (let i = 0; i < 24; i++) {
        const t = d.start + (i + 1) * 3600;
        if (i === 23) {
          vals.push([t, dayCur.valid ? dayCur.value : null]);
          hRel.push([t, dayCur.relays]);
          hExp.push([t, exp]);
        } else {
          vals.push([t, qRow ? qRow[1] : null]);
          hRel.push([t, qRow ? exp : null]);
          hExp.push([t, exp]);
        }
      }
      s.values = vals;
      s.hourly_relays = hRel;
      s.hourly_expected_relays = hExp;
      s.history.sampled_hour = s.history.daily.filter(r => r[0] % 21600 === 0).map(r => [...r]);
      s.current.hour = structuredClone(dayCur);
      s.reference.hour = structuredClone(s.reference.day);
      s.peaks.hour = s.peaks.day ? structuredClone(s.peaks.day) : null;
      if (s.relay_population) {
        s.relay_population.queries.hour = 'sum(example_hour)';
        s.relay_population.hour = s.relay_population.day ? [...s.relay_population.day] : null;
      }
      if (s.relay_counts) s.relay_counts.hour = structuredClone(s.relay_counts.day);
    }
  }
  return d;
}
function toV6Data(src) {
  if (src.schema_version === 6) return structuredClone(src);
  const d = structuredClone(src);
  d.schema_version = 6;
  d.window_seconds = 86400;
  const dayEnd = Math.floor(d.end / 86400) * 86400;
  d.end = dayEnd; d.start = dayEnd - 86400;
  const dailyPeriods = d.time_ranges.periods.filter(p => p.window.unit !== 'hours');
  const defaultDaily = dailyPeriods.find(p => p.id === '1mo')?.id || dailyPeriods[0].id;
  d.time_ranges = {
    version: 1, default_range: defaultDaily,
    legacy_aliases: {...(d.time_ranges.legacy_aliases || {}), '24h': defaultDaily, '30d': defaultDaily},
    card_slots: {fallback_dynamic: defaultDaily, fixed: ['6mo', 'all']},
    periods: dailyPeriods,
  };
  for (const c of d.charts) {
    c.kind = c.kind === 'gauge_hour_mean' ? 'gauge_daily_mean' : 'counter_daily_rate';
    c.start = d.start; c.end = d.end;
    if (c.history_queries?.daily) {
      c.query = c.history_queries.daily.value;
      c.quality_query = c.history_queries.daily.eligible;
      c.history_queries = {daily: c.history_queries.daily};
    }
    if (c.builds && typeof c.builds === 'object') c.builds.scope = 'Latest complete UTC day';
    for (const s of c.series) {
      delete s.values; delete s.hourly_relays; delete s.hourly_expected_relays;
      if (s.history) delete s.history.sampled_hour;
      if (s.current) delete s.current.hour;
      if (s.reference) delete s.reference.hour;
      if (s.peaks) delete s.peaks.hour;
      if (s.relay_population) { delete s.relay_population.hour; if (s.relay_population.queries) delete s.relay_population.queries.hour; }
      if (s.relay_counts) delete s.relay_counts.hour;
    }
  }
  return d;
}
const hasHour = data.window_seconds === 3600;
const v7Data = toV7Data(data), v6Data = toV6Data(data);
const v7DailyData = Object.assign(toV6Data(data), {schema_version: 7});

// A small fixture for the markup this renderer produces. It does not model
// layout; it resolves real rendered tags and models programmatic focus, including
// detached nodes, disabled controls, and explicit negative tabindex on headings.
function renderedDocument(events, focusLog) {
  let html = '', generation = 0, nodes = [];
  const body = {tagName: 'BODY'};
  const document = {activeElement: body};
  function matches(node, selector) {
    const tagged = selector.match(/^([a-z][\w-]*)(\[.*\])?$/i);
    if (tagged) return node.tagName === tagged[1].toUpperCase() && (!tagged[2] || matches(node, tagged[2]));
    if (selector.startsWith('#')) return node.getAttribute('id') === selector.slice(1);
    if (/^\.[\w-]+$/.test(selector)) return (node.getAttribute('class') || '').split(/\s+/).includes(selector.slice(1));
    const attribute = selector.match(/^\[([\w-]+)(?:=["']([^"']*)["'])?\]$/);
    return !!attribute && (attribute[2] === undefined
      ? node.hasAttribute(attribute[1]) : node.getAttribute(attribute[1]) === attribute[2]);
  }
  function findAll(selector, scope) {
    return nodes.filter(node => (!scope || (node.offset >= scope.contentStart && node.offset < scope.contentEnd))
      && matches(node, selector));
  }
  function find(selector, scope) {
    const node = findAll(selector, scope)[0] || null;
    if (node) node.lastSelector = selector;
    return node;
  }
  const page = {
    contains(node) { return nodes.includes(node); },
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value; generation++; document.activeElement = body;
      nodes = [...html.matchAll(/<([a-z][\w:-]*)\b([^<>]*?)>/gi)].map(match => {
        const attributes = new Map([...match[2].matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)]
          .map(([, name, double, single, bare]) => [name.toLowerCase(), decode(double ?? single ?? bare ?? '')]));
        const tag = match[1].toUpperCase(), renderedGeneration = generation;
        const contentStart = match.index + match[0].length;
        const closing = html.indexOf('</' + match[1].toLowerCase() + '>', contentStart);
        return {
          tagName: tag, offset: match.index, contentStart, contentEnd: closing < 0 ? contentStart : closing,
          dataset: Object.fromEntries([...attributes].filter(([name]) => name.startsWith('data-'))
            .map(([name, val]) => [name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase()), val])),
          textContent: decode(closing < 0 ? '' : html.slice(contentStart, closing).replace(/<[^>]*>/g, '')),
          getAttribute(name) { return attributes.get(name) ?? null; },
          hasAttribute(name) { return attributes.has(name); },
          setAttribute(name, val) { attributes.set(name, String(val)); },
          querySelector(selector) { return find(selector, this); },
          // Controller tests dispatch directly from rendered controls.
          closest(selector) { return matches(this, selector) ? this : null; },
          getBoundingClientRect() { return {top: 0, width: 360}; },
          scrollIntoView() {},
          focus(options) {
            const control = ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(tag);
            const native = control || (tag === 'A' && attributes.has('href'));
            const explicit = /^-?\d+$/.test(attributes.get('tabindex') ?? '');
            if (renderedGeneration !== generation || attributes.has('hidden') || attributes.has('inert')
                || (control && attributes.has('disabled')) || (tag === 'INPUT' && attributes.get('type') === 'hidden')
                || (!native && !explicit)) return;
            document.activeElement = this;
            focusLog.push({selector: this.lastSelector, generation: renderedGeneration, options});
          },
        };
      });
    },
    get generation() { return generation; },
  };
  Object.assign(document, {
    querySelector: selector => selector === '#app' ? page : find(selector),
    querySelectorAll: selector => findAll(selector),
    getElementById: id => find('#' + id),
    addEventListener(type, callback) { events.set(type, callback); },
  });
  return {document, page};
}

// Test the real controller without source rewriting or a browser-only VM.
function application(search = '', dataset = data, defaultDesign = '1', release = null) {
  const events = new Map(), windowEvents = new Map(), focusLog = [];
  const {document, page} = renderedDocument(events, focusLog);
  const location = {search, hash: '', assign() {}, reload() {}};
  const entries = [search]; let position = 0;
  function applyLocation(value) {
    const parsed = new URL(value || '', 'https://example.test/');
    location.search = parsed.search; location.hash = parsed.hash;
  }
  applyLocation(search);
  const browser = {
    DEFAULT_DESIGN: defaultDesign,
    location, document,
    navigator: {clipboard: {async writeText() {}}},
    history: {
      pushState(_state, _title, url) { entries.splice(++position); entries.push(url); applyLocation(url); },
      replaceState(_state, _title, url) { entries[position] = url; applyLocation(url); },
    },
    addEventListener(type, callback) { windowEvents.set(type, callback); }, scrollTo() {}, scrollBy() {},
  };
  const app = ui.createObservatory(dataset, browser, release);
  function travel(delta) {
    assert.ok(position + delta >= 0 && position + delta < entries.length, 'history entry exists');
    position += delta; applyLocation(entries[position]); windowEvents.get('popstate')();
  }
  return {app, page, browser, events, windowEvents, focusLog, back: () => travel(-1), forward: () => travel(1)};
}
function chart(id, dataset = data) { return dataset.charts.find(c => c.id === id); }
function element(width = 360) {
  const attributes=new Map();
  return {getBoundingClientRect() { return {width}; }, innerHTML: '',
    setAttribute(name,value) { attributes.set(name,String(value)); },
    getAttribute(name) { return attributes.get(name)??null; }};
}
function click(controller, selector, expectedData = {}, modifiers = {}) {
  const {document} = controller.browser;
  const control = document.querySelectorAll(selector).find(node =>
    Object.entries(expectedData).every(([key, value]) => node.dataset[key] === value));
  assert.ok(control, `No rendered control for ${selector} ${JSON.stringify(expectedData)}`);
  assert.ok(['A', 'BUTTON', 'INPUT'].includes(control.tagName), 'Click target must be an interactive control');
  assert.ok(control.tagName !== 'A' || control.hasAttribute('href'), 'Clicked link must have a destination');
  assert.ok(!control.hasAttribute('disabled') && !control.hasAttribute('hidden') && !control.hasAttribute('inert'),
    'Click target must be enabled');
  control.focus({preventScroll: true});
  controller.events.get('click')({target: control, button: 0, preventDefault() {}, ...modifiers});
}

// These expectations come from the source metric/label contract, not from
// parsing the displayed PromQL or reconstructing the implementation's map.
const expectedSelectors = new Map([
  ['transport_rejections','tor_relay_dos_total{type=~"concurrent_conn_rejected|conn_rejected"}'],
  ['circuit_rejections','tor_relay_dos_total{type="circuit_rejected"}'],
  ['directory_requests','tor_relay_begindir_request_total{type=~"hs_get|hs_post|no_request"}'],
  ['introduction_rejections','tor_relay_dos_total{type="introduce2_rejected"}'],
  ['queue_limit_circuits','tor_relay_dos_total{type=~"circuit_killed_max_cell|circuit_killed_max_cell_outq"}'],
  ['protocol_closes','tor_relay_circ_proto_violation_total'],
  ['onionskin_drops','tor_relay_load_onionskins_total{action="dropped",type=~"fast|ntor|tap"}'],
  ['directory_rejections','tor_relay_dos_total{type=~"begindir_hsdir_rejected|begindir_rate_rejected|begindir_rejected"}'],
  ['hsdir_fetches','tor_relay_hsdir_fetch_total{action=~"not_found|refused|single_hop|success"}'],
  ['hsdir_uploads','tor_relay_hsdir_upload_total{action=~"bad_url|invalid|not_newer|refused|single_hop|success"}'],
  ['directory_waiting','tor_relay_begindir_connections{state="command_wait"}'],
  ['open_circuits','tor_relay_circuits_total{state="opened"}'],
  ['exit_stream_rejections','tor_relay_dos_total{type="stream_rejected"}'],
]);
const selectorTokens = (c, series) => ui.metricSelectorTokens(c, series);
const selectorStrings = (c, series) => selectorTokens(c,series).map(tokens=>tokens.map(token=>token.text).join(''));
const displayedSeries = c => c.series.filter(series=>c.display_keys.includes(series.key));
function selectorMarkup(html) {
  return [...html.matchAll(/<code class="metric-selector">([\s\S]*?)<\/code>/g)].map(([,body])=>({
    html:body, text:decode(body.replace(/<[^>]*>/g,'')),
    values:[...body.matchAll(/<span class="metric-selector-value">([\s\S]*?)<\/span>/g)].map(([,text])=>decode(text)),
  }));
}
assert.equal(expectedSelectors.size,13);
const ranges = data.time_ranges.periods.map(period => period.id);
const defaultRange = data.time_ranges.default_range;
for (const [id,expected] of expectedSelectors) {
  const c=chart(id),series=displayedSeries(c),before=JSON.stringify(c);
  assert.deepEqual(selectorStrings(c,series),[expected],id+' selects the actual source labels');
  assert.equal(JSON.stringify(c),before,'selector preparation does not mutate release data');
  const values=[...expected.matchAll(/(?:=|=~)"([^"]+)"/g)].flatMap(([,value])=>value.split('|'));
  const tokens=selectorTokens(c,series)[0];
  assert.deepEqual(tokens.filter(token=>token.highlight).map(token=>token.text),values,id+' highlights values only');
  assert.ok(tokens.every(token=>typeof token.text==='string'&&(!token.breakAfter||!token.highlight)),
    'line-break opportunities occur at selector boundaries, not inside highlighted values');
  for (const range of ranges) {
    const html=application('?view=explorer&chart='+id+'&range='+range).app.markup();
    const rendered=selectorMarkup(html);
    assert.deepEqual(rendered.map(item=>item.text),[expected],id+' Explorer subtitle matches plotted series');
    assert.deepEqual(rendered[0].values,values);
    assert.equal(rendered[0].text,expected,'highlight and wbr tags add no formatting whitespace');
    if(expected.includes('{'))assert.match(rendered[0].html,/<wbr>/,'selectors have mobile wrap opportunities');
    const queries=[...html.matchAll(/<pre>([\s\S]*?)<\/pre>/g)].map(([,text])=>decode(text));
    const isHour = data.time_ranges.periods.find(p => p.id === range)?.window.unit === 'hours';
    assert.equal(queries[0],isHour?c.query:c.history_queries.daily.value,'the full reproduction query matches interval');
    assert.equal(queries[1],isHour?c.quality_query:c.history_queries.daily.eligible,'coverage query matches interval');
    if (isHour && c.history_queries?.current24?.value) {
      assert.equal(queries[2],c.history_queries.current24.value,'24h view also exposes the 24-hour headline query');
    }
  }
}
for (const range of ranges) for (const layout of ['full','grid']) {
  const html=application('?view=overview&range='+range+'&layout='+layout+'&band=1&series=concurrent_conn_rejected').app.markup();
  assert.deepEqual(selectorMarkup(html).map(item=>item.text),data.families.map(family=>expectedSelectors.get(family.primary)),
    'overview uses every plotted series, independently of Explorer selection or band state');
}

const transportSource=chart('transport_rejections');
const rate=transportSource.series.find(series=>series.key==='conn_rejected');
const concurrent=transportSource.series.find(series=>series.key==='concurrent_conn_rejected');
assert.deepEqual(selectorStrings(transportSource,[rate,concurrent,rate]),
  ['tor_relay_dos_total{type=~"conn_rejected|concurrent_conn_rejected"}'],'deduplication preserves first plotted order');
assert.deepEqual(selectorStrings(chart('onionskin_drops'),[{key:'ntor'}]),
  ['tor_relay_load_onionskins_total{action="dropped",type="ntor"}'],'ntor is not expanded to ntor_v3');
assert.deepEqual(selectorStrings(chart('queue_limit_circuits'),[{key:'circuit_killed_max_cell'}]),
  ['tor_relay_dos_total{type="circuit_killed_max_cell"}'],'the total is a real counter, not a sum of both types');
for (const [id,key,expected] of [
  ['circuit_rejections','circuit_rejections','tor_relay_dos_total{type="circuit_rejected"}'],
  ['introduction_rejections','introduction_rejections','tor_relay_dos_total{type="introduce2_rejected"}'],
  ['exit_stream_rejections','exit_stream_rejections','tor_relay_dos_total{type="stream_rejected"}'],
  ['hsdir_uploads','invalid','tor_relay_hsdir_upload_total{action="invalid"}'],
  ['hsdir_fetches','refused','tor_relay_hsdir_fetch_total{action="refused"}'],
  ['directory_requests','circuit_dead','tor_relay_begindir_request_total{type="circuit_dead"}'],
]) assert.deepEqual(selectorStrings(chart(id),[{key}]),[expected]);
for (const [c,series] of [
  [{...transportSource,id:'constructor'},displayedSeries(transportSource)],
  [{...transportSource,metrics:['different_metric']},displayedSeries(transportSource)],
  [{...transportSource,metrics:['tor_relay_dos_total','second_metric']},displayedSeries(transportSource)],
  [transportSource,[rate,{key:'constructor'}]], [transportSource,[{key:'__proto__'}]],
  [transportSource,[{key:'conn_rejected|.*'}]], [transportSource,[]],
  [chart('directory_waiting'),[{key:'unknown'}]],
]) {
  assert.deepEqual(selectorStrings(c,series),c.metrics,'unrecognized source bindings retain bare metric names');
  assert.ok(selectorTokens(c,series).flat().every(token=>!token.highlight),'fallback does not invent or highlight labels');
}

const selectorBandData=structuredClone(data);
for (const c of selectorBandData.charts) for (const series of c.series) {
  Object.assign(series.reference.day,{n:100,p10_per_relay:0,p50_per_relay:1,p90_per_relay:2});
  if (series.reference.hour) Object.assign(series.reference.hour,{n:100,p10_per_relay:0,p50_per_relay:1,p90_per_relay:2});
}
for (const range of ranges) for (const [id,key,focused] of [
  ['transport_rejections','conn_rejected','tor_relay_dos_total{type="conn_rejected"}'],
  ['hsdir_fetches','single_hop','tor_relay_hsdir_fetch_total{action="single_hop"}'],
  ['onionskin_drops','ntor','tor_relay_load_onionskins_total{action="dropped",type="ntor"}'],
]) {
  const full=application('?view=explorer&chart='+id+'&range='+range+'&series='+key,selectorBandData).app.markup();
  assert.deepEqual(selectorMarkup(full).map(item=>item.text),[expectedSelectors.get(id)],'headline-only selection leaves all plot selectors');
  const active=application('?view=explorer&chart='+id+'&range='+range+'&series='+key+'&band=1',selectorBandData).app.markup();
  assert.deepEqual(selectorMarkup(active).map(item=>item.text),[focused],'an available band focuses the selector with the plot');
  for (const cause of ['too-few','flat']) {
    const unavailable=structuredClone(selectorBandData),series=chart(id,unavailable).series.find(series=>series.key===key);
    const refObj=range==='24h'?series.reference.hour:series.reference.day;
    if(cause==='too-few')refObj.n=1;
    else refObj.p90_per_relay=0;
    const html=application('?view=explorer&chart='+id+'&range='+range+'&series='+key+'&band=1',unavailable).app.markup();
    assert.deepEqual(selectorMarkup(html).map(item=>item.text),[expectedSelectors.get(id)],'an unavailable band still plots all selectors');
  }
}
const selectorController=application('?view=explorer&chart=transport_rejections&range=all',selectorBandData);
selectorController.app.start();
const selectorSelect=selectorController.browser.document.querySelector('[data-series-select]');
selectorSelect.value='concurrent_conn_rejected';selectorController.events.get('change')({target:selectorSelect});
assert.deepEqual(selectorMarkup(selectorController.page.innerHTML).map(item=>item.text),[expectedSelectors.get('transport_rejections')]);
click(selectorController,'[data-band-toggle]');
assert.deepEqual(selectorMarkup(selectorController.page.innerHTML).map(item=>item.text),['tor_relay_dos_total{type="concurrent_conn_rejected"}']);
click(selectorController,'[data-band-toggle]');
assert.deepEqual(selectorMarkup(selectorController.page.innerHTML).map(item=>item.text),[expectedSelectors.get('transport_rejections')]);

const hostileSelector=structuredClone(data),hostileMetric='metric<svg onload="alert(1)">&value';
chart('transport_rejections',hostileSelector).metrics=[hostileMetric,'second"metric'];
const escapedSelectors=selectorMarkup(application('?view=explorer&chart=transport_rejections',hostileSelector).app.markup());
assert.deepEqual(escapedSelectors.map(item=>item.text),[hostileMetric,'second"metric']);
assert.ok(escapedSelectors.every(item=>item.values.length===0));
assert.match(escapedSelectors[0].html,/&lt;svg onload=&quot;alert\(1\)&quot;&gt;&amp;value/);
assert.doesNotMatch(escapedSelectors.map(item=>item.html).join(''),/<svg|onload="|class="metric-selector-value"/);
const unknownSelector=structuredClone(data),unknownChart=chart('transport_rejections',unknownSelector);
unknownChart.id='constructor';
for (const family of unknownSelector.families) {
  family.children=family.children.map(id=>id==='transport_rejections'?'constructor':id);
  if(family.primary==='transport_rejections')family.primary='constructor';
}
assert.deepEqual(selectorMarkup(application('?view=explorer&chart=constructor',unknownSelector).app.markup()).map(item=>item.text),
  ['tor_relay_dos_total'],'newer unknown charts remain renderable without fabricated selectors');
const unknownSeriesData=structuredClone(data),unknownSeriesChart=chart('transport_rejections',unknownSeriesData);
unknownSeriesChart.series[0].key='constructor';
unknownSeriesChart.display_keys[0]='constructor';
assert.deepEqual(selectorMarkup(application('?view=explorer&chart=transport_rejections',unknownSeriesData).app.markup()).map(item=>item.text),
  ['tor_relay_dos_total'],'one unknown plotted class makes the whole source selector fall back');

assert.equal(ui.number(451.82), '452');
assert.equal(ui.number(0.044), '0.044');
assert.equal(ui.number(0.0004), '<0.001');
assert.equal(ui.number(-0.0004), '>−0.001');
assert.equal(ui.number(0.996), '1');
assert.equal(ui.number(0), '0');
for (const value of [null, NaN, Infinity, '42']) assert.equal(ui.number(value), 'Unavailable');
assert.equal(ui.axisNumber(2.5), '2.5');
for (const [value, expected] of [
  [0.00009, '9e-5'], [0.0001, '0.0001'], [0.01234, '0.012'],
  [0.9999, '1'], [1.234, '1.23'], [999.999, '1,000'], [1000.1, '1,000'],
  [1e6, '1M'], [1234567, '1.2M'], [1e12, '1e+12'], [-1234, '-1,234'],
]) assert.equal(ui.axisNumber(value), expected);
const tickStart=Date.UTC(2026,9,26)/1000,tickEnd=Date.UTC(2026,10,18)/1000;
for(const width of [240,262]){
  const ticks=ui.xAxisTicks(tickStart,tickEnd,width,62,12,'all');
  assert.deepEqual(ticks.map(t=>t.label),['26 Oct 2026','18 Nov 2026'],
    'a narrow short-history chart retains full endpoint dates without an overlapping interior tick');
  assert.deepEqual(ticks.map(t=>t.t),[tickStart,tickEnd]);
}
for(const width of [262,360,481.6,1100,1920])for(const days of [1,2,23,120,121,365]){
  const end=tickStart+days*86400,ticks=ui.xAxisTicks(tickStart,end,width,62,12,'all');
  assert.equal(ticks[0].t,tickStart);assert.equal(ticks.at(-1).t,end);
  assert.ok(ticks.length>=2&&ticks.length<=9);
  assert.ok(ticks.slice(1).every((tick,i)=>tick.label!==ticks[i].label),'adjacent date/month labels are distinct');
  if(ticks.length>2){
    const boxes=ticks.map(tick=>{
      const position=62+(tick.t-tickStart)/(end-tickStart)*(width-74),size=tick.label.length*8;
      const start=position-(tick.anchor==='end'?size:tick.anchor==='middle'?size/2:0);
      return [start,start+size];
    });
    assert.ok(boxes.slice(1).every((box,i)=>box[0]-boxes[i][1]>=8),'interior ticks leave the conservative eight-pixel gap');
  }
}
assert.equal(ui.xAxisTicks(tickStart,tickStart+86400,1920,62,12,'all').length,2,
  'one daily interval does not generate repeated same-day ticks');
const monthTicks=ui.xAxisTicks(Date.UTC(2026,0,1)/1000,Date.UTC(2026,4,2)/1000,1920,62,12,'all');
assert.ok(monthTicks.length<9,'month formatting reduces a preferred count that would repeat month labels');
assert.ok(monthTicks.every(t=>/^[A-Za-z]+ 2026$/.test(t.label)));
const yearTicks=ui.xAxisTicks(Date.UTC(2025,0,1)/1000,Date.UTC(2026,0,1)/1000,1920,62,12,'all');
assert.equal(yearTicks.length,9);assert.equal(yearTicks[0].label,'Jan 2025');assert.equal(yearTicks.at(-1).label,'Jan 2026');
const thirtyDayTicks=ui.xAxisTicks(tickEnd-30*86400,tickEnd,1920,62,12,'1mo').map(t=>t.label);
assert.equal(thirtyDayTicks.length,9);
assert.equal(thirtyDayTicks[0],'19 Oct 2026');
assert.equal(thirtyDayTicks.at(-1),'18 Nov 2026');
assert.equal(new Set(thirtyDayTicks).size,thirtyDayTicks.length,'30d charts use distinct UTC date labels across both endpoints');
assert.deepEqual(ui.niceScale(913).ticks, [0, 250, 500, 750, 1000]);
assert.equal(ui.ordinal(11), '11th');
assert.equal(ui.ordinal(23), '23rd');
assert.equal(ui.percentile({n: 200, current_percentile: 0}).text, '0th percentile');
assert.equal(ui.percentile({n: 200, current_percentile: 0}).rank, 0);
assert.equal(ui.percentile({n: 200, current_percentile: 0.2}).text, '<1st percentile');
assert.equal(ui.percentile({n: 100, flat_zero: true}).className, 'flat');
assert.equal(ui.percentile({n: 100, reference_all_zero: true, flat_zero: false, current_percentile: 100}).rank, 100);
assert.equal(ui.percentile({n: 23, current_percentile: 100}).className, 'unknown');
assert.equal(ui.percentile({n: 100, current_percentile: 99}).text, '99th percentile');
for (const count of [0, -1, 1.5, '100', Infinity]) {
  assert.equal(ui.percentile({n: count, current_percentile: 99}).rank, null);
}
assert.equal(ui.percentile({n: 100, current_percentile: 89.8}).text, '<90th percentile');
assert.equal(ui.percentile({n: 100, current_percentile: 99, display_rank: 99}).text, '99th percentile');
assert.equal(ui.percentile({n: 100, current_percentile: 99, display_rank: '2'}).text, '99th percentile');
for (const [rank, stale, expected] of [
  [0, '<1', '0th percentile'],
  [0.2, '0', '<1st percentile'],
  [99, '>99', '99th percentile'],
  [99.5, '100', '>99th percentile'],
  [99.2, '<99', '99th percentile'],
  [89.8, '90', '<90th percentile'],
  [94.8, '95', '<95th percentile'],
  [98.8, '99', '<99th percentile'],
  [100, '>99', '100th percentile'],
]) assert.equal(ui.percentile({n: 100, current_percentile: rank, display_rank: stale}).text, expected);

// Finite extremes must produce a finite, increasing linear axis, including
// numbers whose division underflows or whose rounded-up ceiling overflows.
for (const maximum of [Number.MIN_VALUE, 1e-320, 1e-307, 1e307, Number.MAX_VALUE]) {
  const axis = ui.niceScale(maximum);
  assert.ok(Number.isFinite(axis.max) && axis.max >= maximum);
  assert.equal(axis.ticks[0], 0);
  assert.equal(axis.ticks.at(-1), axis.max);
  assert.ok(axis.ticks.length >= 2 && axis.ticks.length <= 6);
  assert.ok(axis.ticks.every((value, index) => Number.isFinite(value) && (!index || value > axis.ticks[index - 1])));
  assert.ok(ui.axisNumber(maximum).length < 20, 'extreme tick labels remain readable');
}

const deep = application('?view=explorer&family=exit&all=directory,exit').app.getState();
assert.equal(deep.chartId, 'exit_stream_rejections');
assert.equal(deep.range, 'all');
assert.equal(deep.layout, 'full');
assert.equal(application('?view=invalid&family=invalid&series=%22bad').app.getState().view, 'overview');
assert.equal(application('?view=explorer&family=exit&chart=invalid').app.getState().chartId, 'exit_stream_rejections');
assert.equal(application('', data, '2').app.getState().range, 'all');
assert.equal(application('', data, '3').app.getState().view, 'explorer');
assert.equal(application('?range=24h&all=transport&layout=grid', v6Data).app.getState().range, '1mo');
assert.equal(application('?range=24h&all=transport&layout=grid', v7Data).app.getState().range, '24h');
assert.equal(application('?range=30d&all=transport&layout=grid').app.getState().range, '1mo');
assert.equal(application('?range=1m&all=transport&layout=grid').app.getState().range, '1mo');
assert.equal(application('?range=6m').app.getState().range, '6mo');
assert.equal(application('?range=7d').app.getState().range, '7d');
assert.equal(application('?range=12mo').app.getState().range, '12mo');
assert.equal(application('?range=all&all=&layout=grid').app.getState().range, 'all');
for (const value of ['stack','invalid','']) assert.equal(application('?layout='+value).app.getState().layout, 'full');
assert.equal(application('?all=unknown').app.getState().range, defaultRange);
assert.doesNotMatch(application().app.markup(), /Earlier running versions are not recorded/);

const ids = data.families.map(f => f.id);
// Every legacy per-card combination migrates to one explicit global period.
for (let mask = 0; mask < 64; mask++) {
  const expanded = ids.filter((_, i) => mask & (1 << i));
  const oldLink = application('?layout=grid&all=' + expanded.join(',')).app;
  assert.equal(oldLink.getState().range, mask ? 'all' : defaultRange);
  assert.equal(oldLink.getState().layout, 'grid');
  const params = new URLSearchParams(oldLink.url('overview').slice(1));
  assert.equal(params.get('range'), mask ? 'all' : defaultRange);
  assert.equal(params.get('layout'), 'grid');
  assert.equal(params.has('all'), false);
}
function assertOverview(controller, expectedLayout, expectedRange) {
  const state = controller.app.getState(), html = controller.page.innerHTML;
  assert.equal(state.layout, expectedLayout); assert.equal(state.range, expectedRange);
  const articles = [...html.matchAll(/<article\b[^>]*>/g)].map(([tag]) => ({
    id: tag.match(/\bid="family-([^"]+)"/)[1],
    range: tag.match(/\bdata-family-range="([^"]+)"/)[1],
  }));
  assert.deepEqual(articles.map(a => a.id), ids);
  assert.ok(articles.every(a => a.range === expectedRange));
  assert.match(html, new RegExp('class="chart-stack chart-' + expectedLayout + '"'));
  assert.equal(controller.browser.document.querySelectorAll('[data-all-range]').length, ranges.length);
  assert.equal(controller.browser.document.querySelectorAll('[data-layout]').length, 2);
  assert.doesNotMatch(html, /data-card-range|data-card-family|data-range-select/);
}
for (const layout of ['full','grid']) for (const range of ranges) {
  const controller = application('?view=overview&layout=' + layout + '&range=' + range);
  controller.app.start(); assertOverview(controller, layout, range);
  for (const family of data.families) {
    const before = controller.app.getState();
    click(controller, '[data-explore]', {explore: family.id, chart: family.primary});
    assert.equal(controller.app.getState().familyId, family.id);
    assert.equal(controller.app.getState().chartId, family.primary);
    assert.equal(controller.app.getState().range, before.range);
    assert.equal(controller.app.getState().layout, before.layout);
    assert.equal(controller.browser.document.activeElement.textContent, 'Evidence explorer');
    assert.equal(controller.browser.document.querySelectorAll('[data-all-range]').length, ranges.length);
    assert.equal(controller.browser.document.querySelectorAll('[data-layout]').length, 0);
    for (const link of controller.browser.document.querySelectorAll('[data-child]')) {
      const params = new URLSearchParams(link.getAttribute('href').slice(1));
      assert.equal(params.get('range'), before.range); assert.equal(params.get('layout'), layout);
    }
    const nextRange = ranges[(ranges.indexOf(before.range) + 1) % ranges.length];
    click(controller, '[data-all-range]', {allRange: nextRange});
    assert.equal(controller.app.getState().range, nextRange);
    assert.equal(controller.app.getState().layout, layout);
    assert.equal(controller.browser.document.activeElement, controller.browser.document.querySelector('[data-all-range="' + nextRange + '"]'));
    click(controller, '[data-view]', {view: 'overview'});
    assertOverview(controller, layout, nextRange);
  }
  const before = controller.app.getState(), nextLayout = layout === 'grid' ? 'full' : 'grid';
  click(controller, '[data-layout]', {layout: nextLayout});
  assertOverview(controller, nextLayout, before.range);
  assert.equal(controller.browser.document.activeElement, controller.browser.document.querySelector('[data-layout="' + nextLayout + '"]'));
  controller.back(); assertOverview(controller, layout, before.range);
  assert.equal(controller.browser.document.activeElement.textContent, 'Overview');
  controller.forward(); assertOverview(controller, nextLayout, before.range);
  const parsed = ui.parseLocation(data, controller.browser.location.search, controller.browser.location.hash);
  assert.deepEqual(parsed, controller.app.getState(), 'URL reconstructs global settings and selected family');
}

// Selected series/band survive global controls, view navigation and history.
const controller = application('?view=explorer&chart=transport_rejections&range=all&layout=grid&series=concurrent_conn_rejected&band=1');
controller.app.start();
const selectedState = controller.app.getState();
click(controller, '[data-all-range]', {allRange: '1mo'});
assert.equal(controller.app.getState().seriesKey, selectedState.seriesKey);
assert.equal(controller.app.getState().bandEnabled, true);
click(controller, '[data-view]', {view: 'overview'});
assertOverview(controller, 'grid', '1mo');
assert.deepEqual(ui.parseLocation(data, controller.browser.location.search), controller.app.getState());
click(controller, '[data-methods]', {});
assert.equal(controller.browser.document.activeElement, controller.browser.document.getElementById('reference'));
assert.equal(controller.browser.document.activeElement.tagName, 'H3');
assert.equal(controller.browser.document.activeElement.textContent, '18-month reference');
assert.equal(controller.browser.document.activeElement.getAttribute('tabindex'), '-1');
assert.equal(controller.browser.document.querySelector('[data-view="methods"]').getAttribute('aria-current'), 'page');
controller.back(); assertOverview(controller, 'grid', '1mo');
controller.back();
assert.equal(controller.app.getState().view, 'explorer');
assert.equal(controller.app.getState().seriesKey, selectedState.seriesKey);
assert.equal(controller.app.getState().bandEnabled, true);
assert.equal(controller.browser.document.activeElement.textContent, 'Evidence explorer');
controller.back(); assert.deepEqual(controller.app.getState(), selectedState);
controller.forward(); assert.equal(controller.app.getState().range, '1mo');
assert.throws(() => click(controller, '[data-layout]', {layout: 'full'}), /No rendered control/);
assert.throws(() => click(controller, '[data-methods]', {}), /No rendered control/);

// The static header/footer live outside #app; their links still carry settings
// when opened in another tab instead of intercepted by the click handler.
const outsideLinks = application('?range=all&layout=grid');
const footer = {dataset:{view:'methods'},attributes:new Map(),setAttribute(key,value){this.attributes.set(key,value);}};
const internalLinks = outsideLinks.browser.document.querySelectorAll;
outsideLinks.browser.document.querySelectorAll = selector => selector==='[data-view]'
  ? [...internalLinks(selector),footer] : internalLinks(selector);
outsideLinks.app.start();
assert.equal(footer.attributes.get('href'),outsideLinks.app.url('methods'));
click(outsideLinks,'[data-all-range]',{allRange:'1mo'});
const footerParams = new URLSearchParams(footer.attributes.get('href').slice(1));
assert.equal(footerParams.get('range'),'1mo'); assert.equal(footerParams.get('layout'),'grid');

// The shared navigation wraps on tablets and expands on mobile. Its measured
// height must be reflected before a heading scroll, not just at initial load.
const headerView=application('?view=overview'),headerStyles=new Map(),navClasses=new Set(),menuAttributes=new Map();
let closedHeaderHeight=104,headerOffsetAtScroll=null;
const nav={getBoundingClientRect:()=>({height:navClasses.has('show')?400:closedHeaderHeight})};
const navLinks={classList:{remove:key=>navClasses.delete(key),contains:key=>navClasses.has(key),
  toggle(key){if(navClasses.has(key))navClasses.delete(key);else navClasses.add(key);}}};
const navToggle={setAttribute:(key,value)=>menuAttributes.set(key,String(value))};
const headerQuery=headerView.browser.document.querySelector;
headerView.browser.document.querySelector=selector=>({'.nav':nav,'.nav-links':navLinks,'.nav-toggle':navToggle}[selector]||headerQuery(selector));
headerView.browser.document.documentElement={style:{setProperty:(key,value)=>headerStyles.set(key,value)}};
const headerElement=headerView.browser.document.getElementById;
headerView.browser.document.getElementById=id=>{
  const node=headerElement(id);
  if(node)node.scrollIntoView=()=>{headerOffsetAtScroll=headerStyles.get('--header-offset');};
  return node;
};
const toggleHeader=()=>headerView.events.get('click')({target:{closest:selector=>selector==='.nav-toggle'?navToggle:null},button:0});
headerView.app.start();
assert.equal(headerStyles.get('--header-offset'),'120px');
toggleHeader();assert.equal(headerStyles.get('--header-offset'),'416px');assert.equal(menuAttributes.get('aria-expanded'),'true');
toggleHeader();assert.equal(headerStyles.get('--header-offset'),'120px');assert.equal(menuAttributes.get('aria-expanded'),'false');
closedHeaderHeight=170.5;headerView.windowEvents.get('resize')();
assert.equal(headerStyles.get('--header-offset'),'186.5px','resize immediately updates the anchor clearance');
toggleHeader();click(headerView,'[data-methods]');
assert.equal(headerOffsetAtScroll,'186.5px','the closed header is measured before scrolling to #reference');
assert.equal(navClasses.has('show'),false);assert.equal(menuAttributes.get('aria-expanded'),'false');
closedHeaderHeight=87;click(headerView,'[data-view]',{view:'overview'});
assert.equal(headerStyles.get('--header-offset'),'103px','a render measures the current header geometry');
for(const invalidHeight of [undefined,NaN,Infinity,-1]){
  closedHeaderHeight=invalidHeight;headerView.windowEvents.get('resize')();
  assert.equal(headerStyles.get('--header-offset'),'103px','invalid fake geometry does not write invalid CSS');
}

function relayRows(controller, familyId) {
  let html = controller.page.innerHTML;
  if (familyId) {
    html = html.slice(html.indexOf('<article id="family-'+familyId+'"'));
    html = html.slice(0,html.indexOf('</article>'));
  }
  return [...html.matchAll(/<div class="relay-population"([^>]*)>([\s\S]*?)<\/div>/g)]
    .map(([,attributes,body])=>({text:decode(body.replace(/<[^>]*>/g,'')),
      pairs:[...body.matchAll(/<span class="relay-count">([\s\S]*?)<\/span>/g)].map(([,text])=>decode(text)),
      title:decode(attributes.match(/title="([^"]*)"/)?.[1] || '')}));
}
function roleSummary(total, guard, middle_only, exit, hsdir, v2dir, windows=1) {
  return {total,guard,middle_only,exit,hsdir,v2dir,measured_windows:windows,
    classified_windows:windows,relay_windows:total*windows,
    classified_relay_windows:total*windows,coverage:1,role_bounds:null};
}
const roleData = structuredClone(v6Data), roleChart = chart('transport_rejections',roleData);
const rolePrimary = roleChart.series.find(series=>series.key===roleChart.primary_series);
const roleSecondary = roleChart.series.find(series=>series.key!==roleChart.primary_series);
rolePrimary.relay_counts = {
  day:roleSummary(101,80,1,0,90,95),
  seven_days:roleSummary(151,11,21,1,31,41,7),
  one_month:roleSummary(202,12,22,2,32,42,31),
  three_months:roleSummary(252,19,29,9,39,49,92),
  six_months:roleSummary(303,13,23,3,33,43,177),
  twelve_months:roleSummary(353,16,26,6,36,46,365),
  all_time:roleSummary(404,14,24,4,34,44,517),
};
roleSecondary.relay_counts = {
  day:roleSummary(505,15,25,5,35,45),
  seven_days:roleSummary(555,11,21,1,31,41,7),
  one_month:roleSummary(606,16,26,6,36,46,31),
  three_months:roleSummary(656,12,22,2,32,42,92),
  six_months:roleSummary(707,17,27,7,37,47,177),
  twelve_months:roleSummary(757,13,23,3,33,43,365),
  all_time:roleSummary(808,18,28,8,38,48,517),
};
const dynamicSlotPeriod = range => {
  const active = v6Data.time_ranges.card_slots.fixed.includes(range)
    ? v6Data.time_ranges.card_slots.fallback_dynamic : range;
  return v6Data.time_ranges.periods.find(p => p.id === active);
};
const expectedRowText = summaryKey => {
  const c = rolePrimary.relay_counts[summaryKey];
  const prefix = summaryKey === 'hour' || summaryKey === 'day' ? '• Relays' : '• Relays (avg)';
  return `${prefix}: ${c.total} total · ${c.guard} guard · ${c.middle_only} middle-only · ${c.exit} exit · ${c.hsdir} HSDir · ${c.v2dir} V2Dir`;
};
for (const layout of ['full','grid']) for (const range of v6Data.time_ranges.periods.map(p => p.id)) for (const view of ['overview','explorer']) {
  const controller = application('?view='+view+'&range='+range+'&layout='+layout+'&chart=transport_rejections',roleData);
  controller.app.start();
  const rows = relayRows(controller,view==='overview'?'transport':null);
  const dynamicPeriod = dynamicSlotPeriod(range);
  const expectedRows = ['day', dynamicPeriod.summary_key, 'six_months', 'all_time'].map(expectedRowText);
  assert.deepEqual(rows.map(row=>row.text),expectedRows,
    'each statistic uses its own contributor population across the adaptive 2x2 grid');
  for(const row of rows)assert.deepEqual(row.pairs,row.text.split(': ')[1].split(' · '),
    'each complete count/role pair wraps together without changing its visible text');
  assert.match(rows[0].title,/Recorded flag coverage: 100%/);
  assert.match(rows[0].title,/flag groups can overlap/);
  assert.match(rows[1].title,new RegExp(`Mean daily contributing relays across ${rolePrimary.relay_counts[dynamicPeriod.summary_key].measured_windows} measured days`));
  assert.match(rows[2].title,/Mean daily contributing relays across 177 measured days/);
  if (view==='explorer') {
    const select = controller.browser.document.querySelector('[data-series-select]');
    select.value = roleSecondary.key;
    controller.events.get('change')({target:select});
    const selected = relayRows(controller);
    assert.match(selected[0].text,/505 total · 15 guard · 25 middle-only · 5 exit · 35 HSDir · 45 V2Dir/);
    assert.match(selected[1].text,new RegExp(`${roleSecondary.relay_counts[dynamicPeriod.summary_key].total} total`));
    assert.match(selected[2].text,/707 total/);
    assert.match(selected[3].text,/808 total/);
    assert.equal(controller.browser.document.activeElement,controller.browser.document.querySelector('[data-series-select]'));
    assert.equal(controller.app.getState().layout,layout); assert.equal(controller.app.getState().range,range);
  }
}
const roleV7Data = structuredClone(v7Data), roleV7Chart = chart('transport_rejections', roleV7Data);
const roleV7Primary = roleV7Chart.series.find(s => s.key === roleV7Chart.primary_series);
roleV7Primary.relay_counts = {hour: roleSummary(99,70,2,0,80,85), ...rolePrimary.relay_counts};
const roleV7Ctrl = application('?view=explorer&chart=transport_rejections&range=24h', roleV7Data);
roleV7Ctrl.app.start();
const roleV7Rows = relayRows(roleV7Ctrl);
assert.match(roleV7Rows[0].title,/Contributing relays in this 1-hour UTC window\./);
assert.match(roleV7Rows[1].title,/Contributing relays in this 24-hour UTC window\./);
assert.match(roleV7Rows[2].title,/Mean daily contributing relays across 7 measured days/);
assert.match(roleV7Rows[3].title,/Mean daily contributing relays across 517 measured days/);
const expectedPrimaryRows = ['day', 'one_month', 'six_months', 'all_time'].map(expectedRowText);
const unknownRoles = structuredClone(roleData), unknownSeries = chart('transport_rejections',unknownRoles).series.find(series=>series.key===roleChart.primary_series);
unknownSeries.relay_counts.day = roleSummary(0,0,0,0,0,0);
unknownSeries.current.day.relays = 0;
for (const role of ['guard','middle_only','exit','hsdir','v2dir']) unknownSeries.relay_counts.one_month[role] = null;
unknownSeries.relay_counts.one_month.classified_windows = 0;
unknownSeries.relay_counts.one_month.classified_relay_windows = 0;
unknownSeries.relay_counts.one_month.coverage = 0;
unknownSeries.relay_counts.six_months = roleSummary(null,null,null,null,null,null,177);
unknownSeries.historical_averages.six_months.status = 'insufficient_data';
const unknownView = application('?view=explorer&chart=transport_rejections&range=all',unknownRoles);
unknownView.app.start();
const unknownRows = relayRows(unknownView);
assert.equal(unknownRows[0].text,'• Relays: 0 total · 0 guard · 0 middle-only · 0 exit · 0 HSDir · 0 V2Dir');
assert.equal(unknownRows[1].text,'• Relays (avg): 202 total · — guard · — middle-only · — exit · — HSDir · — V2Dir');
assert.match(unknownRows[1].title,/Recorded flag coverage: 0%/);
assert.equal(unknownRows[2].text,'• Relays: —','unavailable six-month metric leaves its average population unavailable');
assert.deepEqual(unknownRows[2].pairs,[],'the unavailable population case remains unchanged');
assert.equal(unknownRows[3].text,expectedPrimaryRows[3]);
const partialRoles = structuredClone(roleData), partialSeries = chart('transport_rejections',partialRoles).series.find(series=>series.key===roleChart.primary_series);
const partial = partialSeries.relay_counts.all_time;
for (const role of ['guard','middle_only','exit','hsdir','v2dir']) partial[role] = null;
partial.role_bounds = {guard:[14.25,16.75],middle_only:[24,26.5],exit:[0.1,2.6],hsdir:[34.7,37.2],v2dir:[44.99,47.49]};
partial.classified_windows = 481;
partial.classified_relay_windows -= 627;
partial.coverage = partial.classified_relay_windows/partial.relay_windows;
for (const layout of ['full','grid']) {
  const bounded = application('?view=explorer&chart=transport_rejections&range=all&layout='+layout,partialRoles); bounded.app.start();
  const row = relayRows(bounded)[3];
  assert.equal(row.text,'• Relays (avg): 404 total · 14–17 guard · 24–27 middle-only · 0–3 exit · 34–38 HSDir · 44–48 V2Dir',
    'partial classifications display outward-rounded bounds without replacing the measured-day total');
  assert.match(row.title,/Recorded flag coverage: 99\.7%/);
  assert.equal(row.pairs[1],'14–17 guard','a complete rounded range and its role remain one wrap unit');
  assert.match(row.title,/Ranges include unclassified relays; endpoints are rounded outward/);
  assert.doesNotMatch(row.title,/confidence/);
  assert.equal(relayRows(bounded)[0].text,expectedPrimaryRows[0],'complete current counts remain exact');
}
for (const [coverage, expected] of [[0.99999,'>99.9%'],[0.00001,'<0.1%'],[1,'100%'],[0,'0%']]) {
  partial.coverage = coverage;
  const edge = application('?view=explorer&chart=transport_rejections&range=all',partialRoles); edge.app.start();
  assert.ok(relayRows(edge)[3].title.includes('Recorded flag coverage: '+expected+'.'),
    'rounded coverage preserves the distinction between partial, absent and complete evidence');
}
for (const invalidCoverage of [null,NaN,Infinity,-1,1.1,'0.9']) {
  partial.coverage = invalidCoverage;
  const invalid = application('?view=explorer&chart=transport_rejections&range=all',partialRoles); invalid.app.start();
  assert.doesNotMatch(relayRows(invalid)[3].title,/Recorded flag coverage/);
}
for (const invalidBounds of [[-1,3],[4,3],[0,405],[0,Infinity],['1',3],[1],null]) {
  partial.role_bounds.guard = invalidBounds;
  const invalid = application('?view=explorer&chart=transport_rejections&range=all',partialRoles); invalid.app.start();
  assert.match(relayRows(invalid)[3].text,/404 total · — guard/);
}
unknownSeries.current.day.relays = null; unknownSeries.relay_counts.day.total = null;
const noPopulation = application('?view=explorer&chart=transport_rejections',unknownRoles); noPopulation.app.start();
assert.equal(relayRows(noPopulation)[0].text,'• Relays: —','unknown population is not reported as zero');
const fractionalRoles=structuredClone(roleData),fractionalPrimary=chart('transport_rejections',fractionalRoles).series.find(s=>s.key===roleChart.primary_series);
fractionalPrimary.relay_counts.all_time.guard=.5;
const fractionalView=application('?view=explorer&chart=transport_rejections&range=all',fractionalRoles);fractionalView.app.start();
assert.equal(relayRows(fractionalView)[3].pairs[1],'<1 guard');
assert.match(fractionalView.page.innerHTML,/<span class="relay-count">&lt;1 guard<\/span>/,
  'less-than counts are escaped without altering the displayed value');
delete unknownSeries.relay_counts;
unknownSeries.current.day.relays = 29;
const legacyPopulation = application('?view=explorer&chart=transport_rejections&range=all',unknownRoles); legacyPopulation.app.start();
assert.deepEqual(relayRows(legacyPopulation).map(row=>row.text),[
  '• Relays: 29 total · — guard · — middle-only · — exit · — HSDir · — V2Dir',
  '• Relays: —','• Relays: —','• Relays: —',
], 'old releases use only current measured totals and do not infer historical roles');
const populationMethods = application('?view=methods',roleData).app.markup();
assert.match(populationMethods,/Middle-only means Running with Guard, Exit, HSDir and V2Dir off/);
assert.match(populationMethods,/Exact role counts require complete flag coverage/);
assert.match(populationMethods,/ranges include unclassified relays over the same contributing windows; endpoints are rounded outward/);
assert.match(populationMethods,/<h3>Dir-spec coarsening<\/h3>/);
assert.match(populationMethods,/<h3>Current DirCache 0 cohort<\/h3>/);
assert.match(populationMethods,/configuration and matching flags for the latest complete UTC day/);

const withGap = structuredClone(chart('transport_rejections'));
withGap.start = 30 * 86400;
withGap.end = 31 * 86400;
for (const series of withGap.series) {
  series.history.daily = Array.from({length: 31}, (_, index) => [(index + 1) * 86400, index + 1, 104, 1, true]);
}
withGap.series[1].history.daily[0][1] = null;
withGap.series[1].history.daily[0][4] = false;
withGap.series[1].history.daily[1][1] = Infinity;
const mobile = element();
application().app.draw(mobile, withGap, '1mo');
assert.equal((mobile.innerHTML.match(/<rect /g) || []).length, 2);
assert.doesNotMatch(mobile.innerHTML, /NaN|Infinity/);
const sourceRows = [...mobile.innerHTML.matchAll(/<text[^>]*y="([^"]+)"[^>]*class="source"/g)].map(x => x[1]);
assert.equal(new Set(sourceRows).size, 2);

const bandApp = application('?view=explorer&family=transport&chart=transport_rejections&range=1mo&band=1').app;
const withBand = structuredClone(chart('transport_rejections'));
withBand.end = 30 * 86400;
withBand.start = withBand.end - 86400;
const selected = withBand.series.find(s => s.key === 'conn_rejected');
selected.history.daily = [[29 * 86400, 100, 100, 1, true], [30 * 86400, 200, 200, 1, true]];
selected.reference.day = {n: 100, p10_per_relay: 1, p50_per_relay: 2, p90_per_relay: 3};
selected.values = Array.from({length: 24}, (_, i) => [withBand.start + (i + 1) * 3600, i === 23 ? 200 : i === 22 ? 100 : null]);
selected.hourly_relays = Array.from({length: 24}, (_, i) => [withBand.start + (i + 1) * 3600, i === 23 ? 200 : i === 22 ? 100 : null]);
selected.hourly_expected_relays = Array.from({length: 24}, (_, i) => [withBand.start + (i + 1) * 3600, i === 23 ? 200 : 100]);
selected.reference.hour = {n: 100, p10_per_relay: 1, p50_per_relay: 2, p90_per_relay: 3};
withBand.series = [selected];
withBand.display_keys = ['conn_rejected'];
for (const bandRange of ranges) {
  const widePlot = element(800);
  bandApp.draw(widePlot, withBand, bandRange);
  const heights = [...widePlot.innerHTML.matchAll(/class="reference-band"[^>]+height="([^"]+)"/g)].map(x => Number(x[1]));
  assert.equal(heights.length, 2, '10th-90th band renders on ' + bandRange);
  assert.ok(Math.abs(heights[1] / heights[0] - 2) < 1e-9, 'each interval uses its own population on ' + bandRange);
  assert.match(widePlot.innerHTML, /reference-median/);
}
const extremeBand = structuredClone(withBand);
extremeBand.display_scale = 0.1;
extremeBand.end = 86400;
extremeBand.series[0].history.daily = [[86400, 100, 3, 1, true]];
extremeBand.series[0].reference.day = {n: 100, p10_per_relay: 2e307, p50_per_relay: 5e307, p90_per_relay: 8e307};
const finiteBand = element(800);
bandApp.draw(finiteBand, extremeBand, 'all');
assert.doesNotMatch(finiteBand.innerHTML, /NaN|Infinity/);
const bound = finiteBand.innerHTML.match(/class="reference-band"[^>]+y="([^"]+)"[^>]+height="([^"]+)"/);
assert.ok(bound, 'three factors with a finite final product retain the band interval');
assert.ok(Math.abs(Number(bound[1]) - 70.4) < 1e-9, 'upper bound is 2.4e307 on a 3e307 axis');
assert.ok(Math.abs(Number(bound[2]) - 133.2) < 1e-9, 'band spans 6e306 through 2.4e307');
const overflowingBand = structuredClone(extremeBand);
overflowingBand.display_scale = 10;
const absentBand = element(800);
bandApp.draw(absentBand, overflowingBand, 'all');
assert.doesNotMatch(absentBand.innerHTML, /reference-band|reference-median|NaN|Infinity/);
assert.match(absentBand.innerHTML, /measurement-series/, 'nonfinite final bounds suppress the band, not valid measurements');
for (const median of [undefined, NaN, Infinity, 4]) {
  const invalid = structuredClone(withBand);
  invalid.series[0].reference.day.p50_per_relay = median;
  const output = element(800);
  bandApp.draw(output, invalid, 'all');
  assert.doesNotMatch(output.innerHTML, /reference-band|reference-median|NaN|Infinity/);
  assert.match(output.innerHTML, /<path class="measurement-series" d="M[^"]+"/,
    'invalid reference suppresses only the band, not measured observations');
}

// Omitted tail rows remain gaps through the latest complete UTC day.
const staleHistory = structuredClone(chart('transport_rejections'));
const day = value => Date.parse(value + 'T00:00:00Z') / 1000;
staleHistory.end = day('2026-11-18');
for (const series of staleHistory.series) {
  series.history.daily = [
    [day('2026-11-15'), 10, 100, 1, true],
    [day('2026-11-16'), 12, 100, 1, true],
  ];
}
const tail = ui.preparePlot(staleHistory, staleHistory.series, 'all');
assert.equal(tail.start, day('2026-11-14'), 'start is the first qualifying interval');
assert.equal(tail.end, day('2026-11-18'));
assert.deepEqual(tail.missing.map(([t]) => t), [day('2026-11-17'), day('2026-11-18')]);
const tailPlot = element(800);
application().app.draw(tailPlot, staleHistory, 'all');
assert.equal((tailPlot.innerHTML.match(/<rect /g) || []).length, 2);

function statSections(html) {
  const starts = [...html.matchAll(/<div class="stat ([^"]*)">/g)];
  return starts.map((match,index)=>({classes:match[1],
    html:html.slice(match.index,starts[index+1]?.index ?? html.length)}));
}
for (const span of v6Data.time_ranges.periods.map(p => p.id)) {
  const viewMarkup = application('?view=explorer&chart=transport_rejections&range=' + span, v6Data).app.markup();
  const active = v6Data.time_ranges.card_slots.fixed.includes(span) ? v6Data.time_ranges.card_slots.fallback_dynamic : span;
  const slot2 = v6Data.time_ranges.periods.find(p => p.id === active);
  const labels = statSections(viewMarkup).map(section => section.html.match(/<div class="stat-label">([^<]+)<\/div>/)?.[1]);
  assert.deepEqual(labels, ['24-hour average', slot2.average_label, 'Last 6 months average', 'All-time average'],
    'adaptive 2x2 stat grid on ' + span);
  assert.equal((viewMarkup.match(/class="stat-number"/g) || []).length, 4, '2x2 stat grid on ' + span);
  const csvPattern = new RegExp(`exports\\/transport_rejections-${slot2 && span ? v6Data.time_ranges.periods.find(p => p.id === span).export_suffix : ''}\\.csv`);
  assert.match(viewMarkup, csvPattern, 'Explorer CSV link uses active period export_suffix on ' + span);
}
const directory = application('?view=explorer&chart=directory_requests&range=all').app.markup();
const directory6mo = application('?view=explorer&chart=directory_requests&range=6mo').app.markup();
const directoryV6 = application('?view=explorer&chart=directory_requests&range=all', v6Data).app.markup();
const sevenDays = chart('directory_requests').series.find(s => s.key === 'hs_get').historical_averages.seven_days;
const sixMonths = chart('directory_requests').series.find(s => s.key === 'hs_get').historical_averages.six_months;
assert.ok(directory.includes(`${sevenDays.days} / ${sevenDays.expected_days} days`));
assert.ok(directory6mo.includes(`${sixMonths.days} / ${sixMonths.expected_days} days`));
assert.equal((statSections(directoryV6).slice(0, 2).map(section=>section.html).join('').match(/class="rank-ruler/g) || []).length, 2,
  'directory_requests 1mo partial window is ready under zero minimum_coverage alongside 24h');
const directoryGated = structuredClone(v6Data);
chart('directory_requests', directoryGated).series.find(s => s.key === 'hs_get').historical_averages.one_month.status = 'insufficient_data';
assert.equal((statSections(application('?view=explorer&chart=directory_requests&range=all', directoryGated).app.markup()).slice(0, 2).map(section=>section.html).join('').match(/class="rank-ruler/g) || []).length, 1,
  'explicit insufficient_data 1mo window still suppresses the Current 24h ruler');
const directory7d = application('?view=explorer&chart=directory_requests&range=7d', v6Data).app.markup();
assert.equal((statSections(directory7d).slice(0, 2).map(section=>section.html).join('').match(/class="rank-ruler/g) || []).length, 2,
  'directory_requests 7d window is ready and displays its Current 24h ruler');
const directoryGatedV7 = structuredClone(v7Data);
chart('directory_requests', directoryGatedV7).series.find(s => s.key === 'hs_get').historical_averages.seven_days.status = 'insufficient_data';
assert.equal((statSections(application('?view=explorer&chart=directory_requests&range=all', directoryGatedV7).app.markup()).slice(1, 3).map(section=>section.html).join('').match(/class="rank-ruler/g) || []).length, 1,
  'explicit insufficient_data 7d fallback window in v7 suppresses the Current 24h ruler');
const directoryGatedV7SixMo = structuredClone(v7Data);
chart('directory_requests', directoryGatedV7SixMo).series.find(s => s.key === 'hs_get').historical_averages.six_months.status = 'insufficient_data';
assert.equal((statSections(application('?view=explorer&chart=directory_requests&range=6mo', directoryGatedV7SixMo).app.markup()).slice(1, 3).map(section=>section.html).join('').match(/class="rank-ruler/g) || []).length, 1,
  'explicit insufficient_data 6mo window in v7 suppresses the Current 24h ruler when viewing 6mo');

const comparisonData = structuredClone(v6Data), comparisonChart = chart('transport_rejections',comparisonData);
const comparisonSeries = comparisonChart.series.find(series=>series.key===comparisonChart.primary_series);
function comparison(n, rank, flat_zero=false) {
  return {method:'daily-per-relay-midrank-v1',cutoff:v6Data.reference_cutoff,n,
    start:n?v6Data.reference_cutoff-n*86400:null,end:n?v6Data.reference_cutoff:null,
    current_percentile:rank,flat_zero};
}
Object.assign(comparisonSeries.current.day,{valid:true,value:12,display:'12'});
comparisonSeries.historical_averages.seven_days.comparison = comparison(7,91);
comparisonSeries.historical_averages.one_month.comparison = comparison(30,92);
comparisonSeries.historical_averages.three_months.comparison = comparison(90,93);
comparisonSeries.historical_averages.six_months.comparison = comparison(116,99);
comparisonSeries.historical_averages.twelve_months.comparison = comparison(300,95);
comparisonSeries.historical_averages.all_time.comparison = comparison(400,99.7);
const v6Ranges = v6Data.time_ranges.periods.map(p => p.id);
for (const layout of ['full','grid']) for (const range of v6Ranges) {
  const view = application('?view=explorer&chart=transport_rejections&layout='+layout+'&range='+range,comparisonData);
  view.app.start();
  const panels = statSections(view.page.innerHTML);
  const active = v6Data.time_ranges.card_slots.fixed.includes(range) ? v6Data.time_ranges.card_slots.fallback_dynamic : range;
  const slot2 = v6Data.time_ranges.periods.find(p => p.id === active);
  assert.equal(panels.length,4);
  assert.equal((view.page.innerHTML.match(/class="historical-comparison"/g) || []).length,3);
  assert.match(view.page.innerHTML,/18-month average: <b>/);
  assert.doesNotMatch(view.page.innerHTML,/Reference mean:|Last complete hour|Sampled 1h fleet peak/);
  assert.match(view.page.innerHTML,/Recorded 24h fleet peak:/);
  assert.match(view.page.innerHTML,/Average of available qualifying 24-hour UTC windows per relay within the 18-month reference, scaled to this period’s reporting-relay count/);
  assert.equal(panels[1].classes,'long-term-stat');
  assert.equal(panels[2].classes,'long-term-stat','the historical mean does not receive the current-rank color class');
  assert.equal(panels[3].classes,'long-term-stat');
  const expectedSlot2Rank = comparisonSeries.historical_averages[slot2.summary_key].comparison.current_percentile;
  assert.match(panels[1].html,new RegExp(`Current 24h: ${ui.ordinal(expectedSlot2Rank)} percentile`));
  assert.match(panels[2].html,/class="percentile high">Current 24h: 99th percentile/);
  assert.match(panels[3].html,/class="percentile high">Current 24h: &gt;99th percentile/);
  assert.match(panels[2].html,/aria-label="Current 24h: 99th percentile on a zero-to-100 percentile scale"/);
  assert.match(panels[2].html,/116 qualifying daily intervals\./);
  assert.match(panels[3].html,/400 qualifying daily intervals\./);
  assert.match(panels[3].html,/\d+ [A-Za-z]+ \d{4}–\d+ [A-Za-z]+ \d{4} UTC/);
  assert.match(panels[3].html,/Current 24-hour and historical values are divided by their reporting-relay counts/);
  assert.ok(panels[1].html.includes('>'+comparisonSeries.historical_averages[slot2.summary_key].display+' <small>'));
  assert.ok(panels[2].html.includes('>'+comparisonSeries.historical_averages.six_months.display+' <small>'));
}
const oldComparisons = structuredClone(comparisonData);
for (const series of chart('transport_rejections',oldComparisons).series) {
  for (const summary of Object.values(series.historical_averages)) delete summary.comparison;
}
assert.doesNotMatch(application('?view=explorer&chart=transport_rejections&range=all',oldComparisons).app.markup(),/historical-comparison/);
for (const [changes,scale] of [
  [{valid:false},1], [{valid:undefined},1], [{value:null,valid:true},1], [{value:-1,valid:true},1],
  [{value:Infinity,valid:true},1], [{value:Number.MAX_VALUE,valid:true},2],
]) {
  const invalid = structuredClone(comparisonData), item = chart('transport_rejections',invalid);
  const series = item.series.find(series=>series.key===item.primary_series);
  Object.assign(series.current.day,changes); item.display_scale=scale;
  const panels = statSections(application('?view=explorer&chart=transport_rejections&range=all',invalid).app.markup()).slice(1);
  for (const panel of panels) {
    assert.match(panel.html,/Current 24h: Percentile unavailable/);
    assert.doesNotMatch(panel.html,/class="rank-ruler/);
  }
}
const comparisonEdges = structuredClone(comparisonData), edgeSeries = chart('transport_rejections',comparisonEdges).series.find(series=>series.key===comparisonChart.primary_series);
edgeSeries.historical_averages.six_months.status='insufficient_data';
edgeSeries.historical_averages.all_time.comparison=comparison(23,99.7);
let edgePanels = statSections(application('?view=explorer&chart=transport_rejections&range=all',comparisonEdges).app.markup());
assert.doesNotMatch(edgePanels[2].html,/class="rank-ruler/);
assert.match(edgePanels[2].html,/Insufficient historical coverage/);
assert.match(edgePanels[3].html,/class="percentile unknown">Current 24h: &gt;99th percentile/);
assert.match(edgePanels[3].html,/23 reference windows/);
edgeSeries.historical_averages.all_time.comparison=comparison(0,null);
edgePanels=statSections(application('?view=explorer&chart=transport_rejections&range=all',comparisonEdges).app.markup());
assert.match(edgePanels[3].html,/No reference observations/);
assert.doesNotMatch(edgePanels[3].html,/class="rank-ruler/);
Object.assign(edgeSeries.current.day,{value:0,valid:true,display:'0'});
edgeSeries.historical_averages.all_time.comparison=comparison(100,null,true);
edgePanels=statSections(application('?view=explorer&chart=transport_rejections&range=all',comparisonEdges).app.markup());
assert.match(edgePanels[3].html,/Current 24h: Percentile —/);
assert.match(edgePanels[3].html,/Reference values are all zero/);
assert.doesNotMatch(edgePanels[3].html,/class="rank-ruler/);
const anotherSeries = comparisonChart.series.find(series=>series.key!==comparisonChart.primary_series);
anotherSeries.historical_averages.one_month.comparison=comparison(30,5);
anotherSeries.historical_averages.six_months.comparison=comparison(100,11);
anotherSeries.historical_averages.all_time.comparison=comparison(300,22);
const selectedComparison = application('?view=explorer&chart=transport_rejections&range=all&layout=grid',comparisonData);
selectedComparison.app.start();
const comparisonSelect = selectedComparison.browser.document.querySelector('[data-series-select]');
comparisonSelect.value=anotherSeries.key; selectedComparison.events.get('change')({target:comparisonSelect});
const selectedPanels=statSections(selectedComparison.page.innerHTML);
assert.match(selectedPanels[1].html,/Current 24h: 5th percentile/);
assert.match(selectedPanels[2].html,/Current 24h: 11th percentile/);
assert.match(selectedPanels[3].html,/Current 24h: 22nd percentile/);
assert.equal(selectedComparison.app.getState().layout,'grid');
assert.equal(selectedComparison.app.getState().range,'all');
assert.equal(selectedComparison.browser.document.activeElement,selectedComparison.browser.document.querySelector('[data-series-select]'));

const valuesTable = directory.match(/<table data-values-table>([\s\S]*?)<\/table>/)[1];
const tableHeaders = [...valuesTable.matchAll(/<th>(.*?)<\/th>/g)].map(([,h]) => decode(h));
const dailyPeriods = data.time_ranges.periods.filter(p => p.window.unit !== 'hours');
const expectedTableHeaders = ['Series', 'Unit', ...(hasHour ? ['Last complete hour'] : []), '24-hour average', ...dailyPeriods.map(p => p.average_label)];
assert.deepEqual(tableHeaders, expectedTableHeaders);
assert.equal(tableHeaders.length, hasHour ? 10 : 9);
const rows = [...valuesTable.matchAll(/<tr><td>(.*?)<\/td><td>(.*?)<\/td>/g)]
  .map(([, name, unit]) => [decode(name), decode(unit)]);
const tableBodyRows = [...valuesTable.matchAll(/<tbody>([\s\S]*?)<\/tbody>/g)][0][1].match(/<tr>[\s\S]*?<\/tr>/g);
assert.ok(tableBodyRows.every(rowHtml => (rowHtml.match(/<td>/g) || []).length === expectedTableHeaders.length));
const expectedUnits = {
  hs_get: 'requests/s', hs_post: 'requests/s', consensus: 'requests/s', microdesc: 'requests/s',
  descriptor: 'requests/s', other_get: 'requests/s', other_post: 'requests/s',
  no_request: 'streams/s', unparsed: 'classifications/s', circuit_dead: 'classifications/s',
};
assert.equal(rows.length, chart('directory_requests').series.length);
assert.deepEqual(rows, chart('directory_requests').series.map(series => [series.name, expectedUnits[series.key]]));

// All dataset text/attributes are encoded; palette and download URLs are bounded.
const hostile = structuredClone(data);
hostile.families[0].icon = '<img src=x onerror=alert(1)>';
hostile.reference_window.date = '<img src=x onerror=alert(1)>';
hostile.reference_document = 'javascript:alert(1)';
hostile.charts[0].series[0].color = '\" onmouseover=\"alert(1)';
const overview = application('', hostile).app.markup();
assert.doesNotMatch(overview, /<img src=x|onmouseover=/);
assert.match(overview, /&lt;img/);
const methods = application('?view=methods', hostile).app.markup();
assert.doesNotMatch(methods, /<img src=x|href="javascript:/);
assert.match(methods, /href="data.json" download data-focus-key="methods-reference">Download the frozen reference/);

// Probe text and attribute contexts that are not ordinary chart titles.
const injected = structuredClone(data), injectedChart = chart('transport_rejections', injected);
const payload = '\"><img src=x onerror=alert(1)>';
const injectedSeries = injectedChart.series.find(s => s.key === injectedChart.primary_series);
injectedSeries.reference.day.n = payload;
injectedSeries.current.day.relays = payload;
injectedSeries.peaks.day.fleet_peak_relays = payload;
injectedChart.id = payload;
const group = injected.families.find(f => f.id === injectedChart.family_id);
group.children = group.children.map(id => id === 'transport_rejections' ? payload : id);
group.primary = payload;
const injectedMarkup = application('?view=explorer&chart=' + encodeURIComponent(payload), injected).app.markup();
assert.doesNotMatch(injectedMarkup, /<img src=x/);
assert.match(injectedMarkup, /data-child="&quot;&gt;&lt;img/);
assert.match(injectedMarkup, /href="exports\/%22%3E%3Cimg/);
assert.doesNotMatch(application('', injected).app.markup(), /<img src=x/);

// Query/hash contents only select known state; modified links keep native
// browser behavior instead of changing the current view.
for (const search of ['?view=' + encodeURIComponent(payload), '?view=explorer&chart=' + encodeURIComponent(payload),
  '?view=explorer&family=' + encodeURIComponent(payload) + '&range=' + encodeURIComponent(payload)]) {
  assert.doesNotMatch(application(search).app.markup(), /<img src=x/);
}
assert.equal(ui.parseLocation(data, '?view=explorer', '#' + payload).chartId, 'directory_requests');
const modified = application(); modified.app.start();
for (const modifiers of [{ctrlKey: true}, {metaKey: true}, {shiftKey: true}, {altKey: true}, {button: 1}]) {
  const generation = modified.page.generation;
  let prevented = 0;
  click(modified, '[data-view]', {view: 'explorer'}, {...modifiers, preventDefault() { prevented++; }});
  assert.equal(prevented, 0, 'modified links preserve native navigation');
  assert.equal(modified.app.getState().view, 'overview');
  assert.equal(modified.page.generation, generation);
}
let ordinaryPrevented = 0;
click(modified, '[data-view]', {view: 'explorer'}, {preventDefault() { ordinaryPrevented++; }});
assert.equal(ordinaryPrevented, 1, 'the spy observes ordinary in-app link cancellation');
assert.equal(modified.app.getState().view, 'explorer');

const extremeChart = structuredClone(withGap);
extremeChart.display_scale = 1;
for (const value of [Number.MIN_VALUE, Number.MAX_VALUE]) {
  for (const series of extremeChart.series) series.history.daily = [1, 2, 3, 4].map(d => [d * 86400, value, 104, 1, true]);
  const output = element(); application().app.draw(output, extremeChart, '1mo');
  assert.doesNotMatch(output.innerHTML, /NaN|Infinity|Invalid Date/);
  assert.match(output.innerHTML, /measurement-series/);
}
extremeChart.display_scale = 3600;
for (const series of extremeChart.series) {
  series.history.daily = [null, [86400, 1, 104, 1, true], [172800, Number.MAX_VALUE, 104, 1, true],
    [259200, -1, 104, 1, true], [345600, 2, 104, 1, true]];
}
extremeChart.end = 4 * 86400;
const scaled = ui.preparePlot(extremeChart, extremeChart.series, 'all', data.time_ranges);
assert.deepEqual(scaled.missing.map(([t]) => t), [172800, 259200]);
const scaledPlot = element(); application().app.draw(scaledPlot, extremeChart, 'all');
assert.doesNotMatch(scaledPlot.innerHTML, /NaN|Infinity/);
assert.equal((scaledPlot.innerHTML.match(/<rect /g) || []).length, 2);
extremeChart.end = Number.MAX_VALUE;
assert.equal(ui.preparePlot(extremeChart, extremeChart.series, '1mo', data.time_ranges), null);
extremeChart.end = 8.64e12;
assert.equal(ui.preparePlot(extremeChart, extremeChart.series, 'all', data.time_ranges), null, 'corrupt range cannot allocate an unbounded gap grid');

for (const value of [undefined, null, -1, Infinity, '1']) {
  const missingCurrent = structuredClone(v6Data), missingChart = chart('transport_rejections', missingCurrent);
  const missingSeries = missingChart.series.find(s => s.key === missingChart.primary_series);
  Object.assign(missingSeries.current.day, {value, valid: true, display: '1234'});
  missingSeries.peaks.day.current_is_record = true;
  const missingMarkup = application('?view=explorer&chart=transport_rejections', missingCurrent).app.markup();
  assert.match(missingMarkup, /24-hour average<\/div><div class="stat-number">Unavailable/);
  assert.equal((missingMarkup.match(/class="rank-ruler/g) || []).length, 0, 'invalid current value has no percentile marker');
  assert.doesNotMatch(missingMarkup, /Highest recorded 24h fleet value/, 'an unavailable current value cannot claim a new record');
}
for (const [scale, value] of [
  [2, Number.MAX_VALUE], [0, 1], [-1, 1], [Infinity, 1], [NaN, 1],
]) {
  const invalidScale = structuredClone(v6Data), scaledChart = chart('transport_rejections', invalidScale);
  scaledChart.display_scale = scale;
  const scaledSeries = scaledChart.series.find(s => s.key === scaledChart.primary_series);
  Object.assign(scaledSeries.current.day, {value, valid: true, display: '1234'});
  scaledSeries.peaks.day.current_is_record = true;
  const scaledMarkup = application('?view=explorer&chart=transport_rejections', invalidScale).app.markup();
  assert.match(scaledMarkup, /24-hour average<\/div><div class="stat-number">Unavailable/);
  assert.equal((scaledMarkup.match(/class="rank-ruler/g) || []).length, 0,
    'headline and percentile use the same scaled-current eligibility');
  assert.doesNotMatch(scaledMarkup, /Highest recorded 24h fleet value/, 'ineligible scaled values cannot claim a new record');
}
const referenceTexts = html => [...html.matchAll(/18-month average: <b>([^<]*)<\/b>/g)].map(match => match[1]);
function assertUnavailableSummaries(html) {
  assert.deepEqual(referenceTexts(html), ['Unavailable']);
  assert.equal((html.match(/Recorded 24h fleet peak: Unavailable<\/div>/g) || []).length, 1);
  for (const label of ['Last 1 month average', 'Last 6 months average', 'All-time average']) {
    assert.match(html, new RegExp(label + '<\\/div><div class="stat-number"[^>]*>Unavailable'));
  }
  const table = html.match(/<table data-values-table>([\s\S]*?)<\/table>/)[1];
  assert.doesNotMatch(table, /<td>9012<\/td>/, 'table averages reject the same invalid scale');
}
for (const scale of [0, -1, Infinity, NaN, undefined, '1']) for (const cached of [false, true]) {
  const fixture = structuredClone(v6Data), item = chart('transport_rejections', fixture);
  const series = item.series.find(s => s.key === item.primary_series);
  item.display_scale = scale;
  series.reference.day.mean_per_relay = 2;
  series.current.day.relays = 3;
  if (cached) series.current.day.reference_display = '1234';
  else delete series.current.day.reference_display;
  series.current.day.peak_display = '5678';
  series.peaks.day.fleet_peak = 2;
  for (const period of v6Data.time_ranges.periods) Object.assign(series.historical_averages[period.summary_key], {status: 'ready', value: 2, display: '9012'});
  assertUnavailableSummaries(application('?view=explorer&chart=transport_rejections&range=all', fixture).app.markup());
}
for (const cached of [false, true]) {
  const fixture = structuredClone(v6Data), item = chart('transport_rejections', fixture);
  const series = item.series.find(s => s.key === item.primary_series);
  Object.assign(series.current.day, {valid: false, value: null, relays: 3});
  series.reference.day.mean_per_relay = 2;
  if (cached) series.current.day.reference_display = '6';
  else delete series.current.day.reference_display;
  const html = application('?view=explorer&chart=transport_rejections&range=all', fixture).app.markup();
  assert.deepEqual(referenceTexts(html), ['6'], 'reference means survive insufficient current coverage');
  assert.doesNotMatch(html, /class="rank-ruler/);
}
const overflowingSummaries = structuredClone(v6Data), overflowingChart = chart('transport_rejections', overflowingSummaries);
const overflowingSeries = overflowingChart.series.find(s => s.key === overflowingChart.primary_series);
overflowingChart.display_scale = 2;
overflowingSeries.reference.day.mean_per_relay = Number.MAX_VALUE;
overflowingSeries.current.day.reference_display = '1234';
overflowingSeries.peaks.day.fleet_peak = Number.MAX_VALUE;
overflowingSeries.current.day.peak_display = '5678';
for (const period of v6Data.time_ranges.periods) Object.assign(overflowingSeries.historical_averages[period.summary_key], {value: Number.MAX_VALUE, display: '9012'});
assertUnavailableSummaries(application('?view=explorer&chart=transport_rejections&range=all', overflowingSummaries).app.markup());
overflowingChart.display_scale = 0.1;
overflowingSeries.reference.day.mean_per_relay = 8e307;
overflowingSeries.current.day.relays = 3;
delete overflowingSeries.current.day.reference_display;
assert.equal(referenceTexts(application('?view=explorer&chart=transport_rejections', overflowingSummaries).app.markup())[0],
  ui.number((8e307 * 0.1) * 3), 'reference scaling retains a finite final product');
const validZero = structuredClone(v6Data), zeroChart = chart('transport_rejections', validZero);
const zeroSeries = zeroChart.series.find(s => s.key === zeroChart.primary_series);
Object.assign(zeroSeries.current.day, {value: 0, valid: true, display: '0'});
Object.assign(zeroSeries.reference.day, {current_percentile: 0, flat_zero: false});
const zeroMarkup = application('?view=explorer&chart=transport_rejections', validZero).app.markup();
assert.match(zeroMarkup, /24-hour average<\/div><div class="stat-number">0 /);
assert.match(zeroMarkup, />0th percentile<\/span>/);
assert.match(zeroMarkup, /rank-marker" style="left:0%"/);
const badTime = structuredClone(data); badTime.end = Number.MAX_VALUE;
assert.match(application('?view=methods', badTime).app.markup(), /Saved snapshot · Unavailable Unavailable UTC/);
for (const [timestamp, expected] of [
  [8.64e12, '00:00'],
  [-8.64e12, '00:00'],
  [Date.parse('+010000-01-01T03:04:00Z') / 1000, '03:04'],
]) {
  const expandedYear = structuredClone(data); expandedYear.end = timestamp;
  assert.match(application('?view=methods', expandedYear).app.markup(),
    new RegExp('Saved snapshot · [^<]* ' + expected + ' UTC'), 'UTC clock is independent of year width');
}

const broken = structuredClone(data); broken.families[0].primary = 'missing-chart';
const failed = application();
failed.browser.ONIONSURGE_DATA = broken;
failed.browser.document.getElementById = () => failed.page;
assert.equal(ui.startObservatory(failed.browser), null);
assert.equal(failed.page.textContent, 'Unable to load chart data. Please reload this page.');
assert.equal(failed.events.size, 0, 'failed startup leaves no active controller handlers');

// All ranges use the same plot dimensions at a given layout width.
const plotDimensions = new Map();
for (const item of data.charts) for (const span of ranges) {
  const app = application('?view=explorer&chart=' + item.id + '&range=' + span).app;
  for (const width of [240, 262, 360, 800, 1680]) {
    const plot = element(width); app.draw(plot, item, span);
    assert.doesNotMatch(plot.innerHTML, /NaN|Infinity|Invalid Date/, `${item.id}/${span}/${width}`);
    assert.match(plot.innerHTML, /<svg|No retained samples/);
    assert.equal((plot.innerHTML.match(/Source: 1aeo\.com - OnionSurge/g) || []).length, 1,
      `one source credit: ${item.id}/${span}/${width}`);
    const viewBox = plot.innerHTML.match(/viewBox="([^"]+)"/)?.[1];
    if (viewBox) {
      const key = item.id + '/' + width;
      if (plotDimensions.has(key)) assert.equal(viewBox, plotDimensions.get(key), `stable range dimensions: ${key}`);
      else plotDimensions.set(key, viewBox);
    }
  }
}

const emptyChart = structuredClone(chart('transport_rejections'));
for (const series of emptyChart.series) {
  series.history.daily = [];
  series.values = [];
}
for (const period of data.time_ranges.periods) {
  const span = period.id;
  const output = element(240);
  const noDataView=application().app;
  noDataView.draw(output, emptyChart, span);
  assert.match(output.innerHTML, /No retained samples available/);
  assert.equal((output.innerHTML.match(/Source: 1aeo\.com - OnionSurge/g) || []).length, 1);
  assert.equal(output.getAttribute('aria-label'),`${emptyChart.title}; ${period.plot_label}; linear scale; No retained samples available for this chart.`);
  const restored=chart(emptyChart.id),baseline=element(240);
  noDataView.draw(output,restored,span);noDataView.draw(baseline,restored,span);
  assert.equal(output.getAttribute('aria-label'),`${restored.title}; ${period.plot_label}; linear scale. Use arrow keys to inspect intervals.`,
    'returning observations restore the normal accessible chart description');
  assert.equal(output.innerHTML,baseline.innerHTML,'accessible state changes do not change SVG output');
  const zeroChart=structuredClone(restored);
  for(const series of zeroChart.series){
    series.history.daily=series.history.daily.map(row=>[row[0],0,...row.slice(2)]);
    if(series.values)series.values=series.values.map(([t])=>[t,0]);
  }
  noDataView.draw(output,zeroChart,span);
  assert.doesNotMatch(output.getAttribute('aria-label'),/No retained samples/,'measured zeros remain observations, not a no-data state');
}

function releaseContext(dataset,id,now) {
  const suffixes=dataset.time_ranges?dataset.time_ranges.periods.map(p=>p.export_suffix):['30d','all-time'];
  const paths=['data.json','manifest.json',dataset.reference_document,
    ...dataset.charts.flatMap(item=>suffixes.map(span=>'exports/'+item.id+'-'+span+'.csv'))];
  return {mode:'live',id,generatedAt:dataset.end+600,now,downloads:new Map(paths.map(file=>[file,'blob:'+id+'/'+file]))};
}

for (const layout of ['full','grid']) for (const range of ranges) {
  for (const [view, selector] of [
    ['overview','[data-all-range="'+range+'"]'],
    ['overview','[data-layout="'+layout+'"]'],
    ['explorer','[data-all-range="'+range+'"]'],
    ['explorer','[data-chart-plot="transport_rejections"]'],
  ]) {
    const focused = application('?view='+view+'&range='+range+'&layout='+layout+'&chart=transport_rejections',
      data,'1',releaseContext(data,'preferences-old',()=>data.end));
    focused.app.start();
    const before = focused.app.getState();
    focused.browser.document.querySelector(selector).focus({preventScroll:true});
    const fresh = structuredClone(data);
    focused.app.replaceRelease(fresh,releaseContext(fresh,'preferences-new',()=>fresh.end));
    assert.deepEqual(focused.app.getState(),before,'refresh preserves global range/layout and selection');
    assert.equal(focused.browser.document.activeElement,focused.browser.document.querySelector(selector),
      'refresh restores the selected control including chart plot focus');
    assert.deepEqual(ui.parseLocation(data,focused.app.url(view)),before);
  }
}

// Count reads of the real observations, rather than exposing cache internals.
// Navigation may reuse a release's prepared plots; a replacement must prepare
// them again, and a failed replacement must restore the prior release's cache.
const cachedData = structuredClone(v6Data), cachedChart = chart('transport_rejections', cachedData);
let dailyReads = 0;
for (const series of cachedChart.series) {
  const daily = series.history.daily;
  Object.defineProperty(series.history, 'daily', {enumerable: true, get() { dailyReads++; return daily; }});
}
const cachedView = application('?view=overview', cachedData);
cachedView.app.start();
const firstDefaultReads = dailyReads;
assert.ok(firstDefaultReads > 0, 'initial default plot preparation reads daily observations');
click(cachedView, '[data-view]', {view: 'methods'});
click(cachedView, '[data-view]', {view: 'overview'});
assert.equal(dailyReads, firstDefaultReads, 'view redraw retains default preparation');
click(cachedView, '[data-all-range]', {allRange: 'all'});
const afterAllReads = dailyReads;
assert.ok(afterAllReads > firstDefaultReads, 'first All time view prepares all-time observations');
click(cachedView, '[data-all-range]', {allRange: v6Data.time_ranges.default_range});
click(cachedView, '[data-all-range]', {allRange: 'all'});
assert.equal(dailyReads, afterAllReads, 'range redraw reuses both default and all-time preparations');
cachedView.app.replaceRelease(cachedData, releaseContext(cachedData, 'cache-new', () => v6Data.end));
assert.ok(dailyReads > afterAllReads, 'replacement resets preparation even for the same chart identities');
const beforeRejectedPlot = element(); cachedView.app.draw(beforeRejectedPlot, cachedChart, 'all');
const rejectedData = structuredClone(v6Data);
for (const series of chart('transport_rejections', rejectedData).series) {
  series.history.daily = series.history.daily.map(row => [row[0], 1000000, ...row.slice(2)]);
}
const originalQueryAll = cachedView.browser.document.querySelectorAll;
let rejectPlots = true;
cachedView.browser.document.querySelectorAll = selector => {
  if (selector === '[data-chart-plot]' && rejectPlots) { rejectPlots = false; throw new Error('reject prepared candidate'); }
  return originalQueryAll(selector);
};
const readsBeforeFailure = dailyReads;
assert.throws(() => cachedView.app.replaceRelease(rejectedData, releaseContext(rejectedData, 'cache-rejected', () => v6Data.end)), /reject prepared candidate/);
const afterRejectedPlot = element(); cachedView.app.draw(afterRejectedPlot, cachedChart, 'all');
assert.equal(afterRejectedPlot.innerHTML, beforeRejectedPlot.innerHTML, 'rollback retains the old plot geometry');
assert.equal(dailyReads, readsBeforeFailure, 'rollback restores the prior preparation cache');

let liveNow=data.end+(hasHour?7201:108001);
const freshNow=data.end+(hasHour?3600:7200);
const firstRelease=releaseContext(data,'first',()=>freshNow);
const liveView=application('?view=explorer&chart=transport_rejections&range=all&layout=grid&series=concurrent_conn_rejected&band=1',data,'1',firstRelease);
liveView.app.start();
assert.match(liveView.app.markup(),/Observed through/);
assert.doesNotMatch(liveView.app.markup(),/Saved snapshot|(?:Daily 00:00|Hourly) UTC publication planned|Update delayed/);
assert.match(liveView.app.markup(),/href="blob:first\/exports\/transport_rejections-all-time.csv" download="first-transport_rejections-all-time.csv"/);
const selectedControl=liveView.browser.document.querySelector('[data-series-select]');selectedControl.focus({preventScroll:true});
const priorState=liveView.app.getState(),clickHandler=liveView.events.get('click');
const nextData=structuredClone(data);
Object.assign(chart('transport_rejections',nextData).series.find(series=>series.key==='concurrent_conn_rejected').current.day,{value:42,display:'42'});
liveView.app.replaceRelease(nextData,releaseContext(nextData,'second',()=>liveNow));
assert.deepEqual(liveView.app.getState(),priorState,'release swap preserves view/range/series/band state');
assert.equal(liveView.events.get('click'),clickHandler,'refresh does not register another controller');
assert.equal(liveView.browser.document.activeElement,liveView.browser.document.querySelector('[data-series-select]'));
assert.doesNotMatch(liveView.page.innerHTML,/blob:first\//);
assert.match(liveView.page.innerHTML,/blob:second\/data.json/);
assert.match(liveView.page.innerHTML,/24-hour average<\/div><div class="stat-number">42 /);
const beforeAge=liveView.page.generation;liveNow++;
liveView.app.updateFreshness({fallback:true,failed:true});
assert.equal(liveView.page.generation,beforeAge,'freshness text does not redraw plots');
assert.match(liveView.app.markup(),/Update delayed/);
assert.match(liveView.app.markup(),/Showing previous verified release/);
assert.match(liveView.app.markup(),/Refresh unavailable; retaining verified release/);
const queryAll=liveView.browser.document.querySelectorAll;let failPlots=true;
liveView.browser.document.querySelectorAll=selector=>{
 if(selector==='[data-chart-plot]'&&failPlots){failPlots=false;throw new Error('plot render failure');}
 return queryAll(selector);
};
assert.throws(()=>liveView.app.replaceRelease(data,releaseContext(data,'rejected',()=>liveNow)),/plot render failure/);
assert.deepEqual(liveView.app.getState(),priorState);
assert.match(liveView.page.innerHTML,/blob:second\/data.json/);
assert.doesNotMatch(liveView.page.innerHTML,/blob:rejected\//);
click(liveView,'[data-view]',{view:'methods'});
assert.match(liveView.app.markup(),/Release <code>second<\/code>/);
assert.match(liveView.app.markup(),/blob:second\/reference\//);
assert.match(liveView.app.markup(),/blob:second\/manifest.json/);

const refreshedReference=structuredClone(nextData);
refreshedReference.reference_sha256='d'.repeat(64);
refreshedReference.reference_id='reference-'+refreshedReference.reference_window.date+'-'+'d'.repeat(12);
refreshedReference.reference_document='reference/'+refreshedReference.reference_id+'.json';
for(const [search,key] of [
 ['?view=explorer&chart=transport_rejections&range=7d','explorer-csv'],
 ['?view=explorer&chart=transport_rejections&range=1mo','explorer-csv'],
 ['?view=explorer&chart=transport_rejections&range=all','explorer-csv'],
 ['?view=explorer&chart=transport_rejections','explorer-json'],
 ['?view=explorer&chart=transport_rejections','explorer-permalink'],
 ['?view=methods','methods-json'],
 ['?view=methods','methods-reference'],
 ['?view=methods','methods-manifest'],
]){
 const focused=application(search,data,'1',releaseContext(data,'focus-old',()=>liveNow));
 focused.app.start();
 const selector='[data-focus-key="'+key+'"]';
 const oldLink=focused.browser.document.querySelector(selector);
 assert.ok(oldLink&&oldLink.tagName==='A',key+' has a stable interactive focus target');
 oldLink.focus({preventScroll:true});
 const oldHref=oldLink.getAttribute('href');
 focused.app.replaceRelease(refreshedReference,releaseContext(refreshedReference,'focus-new',()=>liveNow));
 const nextLink=focused.browser.document.querySelector(selector);
 assert.equal(focused.browser.document.querySelectorAll(selector).length,1,key+' remains unique');
 assert.equal(focused.browser.document.activeElement,nextLink,key+' preserves keyboard focus after refresh');
 assert.notEqual(nextLink,oldLink,'focus moves onto the newly rendered link');
 assert.equal(nextLink.textContent,oldLink.textContent,key+' retains the selected link');
 if(key==='explorer-permalink')assert.equal(nextLink.getAttribute('href'),oldHref);
 else{
  assert.notEqual(nextLink.getAttribute('href'),oldHref,key+' uses the new verified Blob URL');
  assert.match(nextLink.getAttribute('href'),/^blob:focus-new\//);
 }
 if(key==='methods-reference')assert.equal(nextLink.getAttribute('href'),'blob:focus-new/'+refreshedReference.reference_document);
}

const patternSafe=structuredClone(withGap);
patternSafe.id='a)b"c d';
const patternPlot=element(800);
application().app.draw(patternPlot,patternSafe,'1mo');
const patId=patternPlot.innerHTML.match(/<pattern id="([^"]+)"/)?.[1];
assert.ok(patId&&/^[A-Za-z0-9_-]+$/.test(patId),'SVG pattern ID is CSS/fragment-safe');
assert.ok(patternPlot.innerHTML.includes(`fill="url(#${patId})"`));

const shortHist=structuredClone(staleHistory);
shortHist.end=day('2026-11-18')+3*3600;
for(const series of shortHist.series){
 series.history.daily=[[day('2026-11-17'),10,100,1,true],[day('2026-11-18'),12,100,1,true]];
}
const shortOut=element(800);
application().app.draw(shortOut,shortHist,'all');
const shortLabels=[...shortOut.innerHTML.matchAll(/<text[^>]*y="271"[^>]*>([^<]+)<\/text>/g)].map(m=>m[1]);
assert.ok(shortLabels.length>0,'short history renders X-axis ticks');
assert.equal(new Set(shortLabels).size,shortLabels.length,'no duplicate X-axis ticks on short history');

const medHist=structuredClone(staleHistory);
medHist.end=day('2026-11-18')+3*3600;
for(const series of medHist.series){
 series.history.daily=Array.from({length:150},(_,i)=>[day('2026-11-18')-(149-i)*86400,10,100,1,true]);
}
const medOut=element(1680);
application().app.draw(medOut,medHist,'all');
const medLabels=[...medOut.innerHTML.matchAll(/<text[^>]*y="331"[^>]*>([^<]+)<\/text>/g)].map(m=>m[1]);
assert.ok(medLabels.length>0,'150-day history renders X-axis ticks');
assert.equal(new Set(medLabels).size,medLabels.length,'no duplicate X-axis ticks on 150-day history');

const dirReq=chart('directory_requests');
for(const plotWidth of [482,452]){
 const narrowDir=element(plotWidth);
 application().app.draw(narrowDir,dirReq,'all');
 const tickTexts=[...narrowDir.innerHTML.matchAll(/<text[^>]*y="271"[^>]*>([^<]+)<\/text>/g)].map(m=>m[1]);
 assert.ok(tickTexts.length>0,`narrow full-date chart (${plotWidth}px) renders X-axis ticks`);
 assert.ok(tickTexts.length<=4,`narrow full-date chart (${plotWidth}px) limits ticks to <= 4 (${tickTexts.length})`);
}

const keyCtrl=application('?view=explorer&chart=transport_rejections&range=all');
const liveStatus={textContent:''};
let cursorNode=null,tooltipNode=null;
const svgNode={
 querySelector:s=>s==='.plot-cursor'?cursorNode:null,
 appendChild:n=>{cursorNode=n;}
};
const keyPlot={
 innerHTML:'',
 getBoundingClientRect:()=>({width:800,height:300}),
 closest:s=>s==='[data-chart-plot]'?keyPlot:null,
 querySelector:s=>s==='svg'?svgNode:s==='.plot-cursor'?cursorNode:s==='.chart-tooltip'?tooltipNode:null,
 appendChild:n=>{tooltipNode=n;}
};
const origKeyDocQS=keyCtrl.browser.document.querySelector;
keyCtrl.browser.document.querySelector=s=>s==='[data-live-status]'?liveStatus:origKeyDocQS(s);
keyCtrl.browser.document.createElement=tag=>({
 tag,className:'',innerHTML:'',style:{},
 remove(){if(this===tooltipNode)tooltipNode=null;}
});
keyCtrl.browser.document.createElementNS=(ns,tag)=>({
 ns,tag,attrs:{},
 setAttribute(k,v){this.attrs[k]=v;},
 remove(){if(this===cursorNode)cursorNode=null;}
});
keyCtrl.app.start();
keyCtrl.app.draw(keyPlot,chart('transport_rejections'),'all');
let keyPrevented=false;
keyCtrl.events.get('keydown')({key:'ArrowLeft',target:keyPlot,preventDefault(){keyPrevented=true;}});
assert.equal(keyPrevented,true);
assert.ok(cursorNode&&cursorNode.attrs.class==='plot-cursor');
assert.ok(tooltipNode&&tooltipNode.innerHTML.includes('chart-tooltip-time'));
assert.match(tooltipNode.innerHTML,/UTC \(24h\)/);
assert.ok(liveStatus.textContent.includes(chart('transport_rejections').title));
keyCtrl.events.get('keydown')({key:'Escape',target:keyPlot});
assert.equal(cursorNode,null);
assert.equal(tooltipNode,null);

for(const search of ['','?view=methods','?view=fixes']){
 const brokenView=structuredClone(data);brokenView.families[0].primary='missing-chart';
 const bad=application(search);
 bad.browser.ONIONSURGE_DATA=brokenView;
 bad.browser.document.getElementById=()=>bad.page;
 assert.equal(ui.startObservatory(bad.browser),null);
 assert.equal(bad.page.textContent,'Unable to load chart data. Please reload this page.');
 assert.equal(bad.events.size,0,`broken graph fails upfront for ${search}`);
}

const liveCtrl=application('?view=explorer&chart=transport_rejections');
liveCtrl.app.start();
const brokenLive=structuredClone(data);brokenLive.families[0].children=[];
assert.throws(()=>liveCtrl.app.replaceRelease(brokenLive,{id:'rel-bad',generatedAt:data.end,downloads:new Map()}));
assert.equal(liveCtrl.app.getState().chartId,'transport_rejections');

const fragParsed=ui.parseLocation(data,'','#transport_rejections','2');
assert.equal(fragParsed.view,'explorer');
assert.equal(fragParsed.chartId,'transport_rejections');
assert.equal(fragParsed.range,defaultRange);

const badPopChart=structuredClone(staleHistory);
for(const s of badPopChart.series){
 s.history.daily=[[day('2026-11-17'),10,0,1,true],[day('2026-11-18')+60,12,100,1,true]];
}
assert.equal(ui.preparePlot(badPopChart,badPopChart.series,'all'),null);

const noHistQueries=structuredClone(data);
delete chart('transport_rejections',noHistQueries).history_queries;
const noHistMarkup=application('?view=explorer&chart=transport_rejections',noHistQueries).app.markup();
assert.match(noHistMarkup,/PromQL/);

const staleDisplay=structuredClone(v6Data),staleChart=chart('transport_rejections',staleDisplay);
const staleSeries=staleChart.series.find(s=>s.key===staleChart.primary_series);
staleChart.display_scale=1;
Object.assign(staleSeries.current.day,{value:50,valid:true,relays:100,display:'9999'});
staleSeries.current.day.reference_display='7777';
staleSeries.current.day.peak_display='8888';
staleSeries.peaks.day.fleet_peak=100;
staleSeries.reference.day.mean_per_relay=2;
Object.assign(staleSeries.historical_averages.one_month,{status:'ready',value:25,display:'5555'});
Object.assign(staleSeries.historical_averages.six_months,{status:'ready',value:12,display:'6666'});
const staleMarkup=application('?view=explorer&chart=transport_rejections&range=all',staleDisplay).app.markup();
assert.match(staleMarkup,/24-hour average<\/div><div class="stat-number">50 /);
assert.match(staleMarkup,/18-month average: <b>200<\/b>/);
assert.match(staleMarkup,/Recorded 24h fleet peak: 100<\/div>/);
assert.match(staleMarkup,/Last 1 month average<\/div><div class="stat-number"[^>]*>25 /);
assert.match(staleMarkup,/Last 6 months average<\/div><div class="stat-number"[^>]*>12 /);
assert.doesNotMatch(staleMarkup,/9999|7777|8888|6666|5555/);
assert.ok(staleMarkup.indexOf('class="series-toolbar"')!==-1&&staleMarkup.indexOf('class="series-toolbar"')<staleMarkup.indexOf('class="stat-pair'));

const subnormalFixture=structuredClone(v6Data),subnormalChart=chart('transport_rejections',subnormalFixture);
const subnormalSeries=subnormalChart.series.find(s=>s.key===subnormalChart.primary_series);
subnormalChart.display_scale=0.1;
Object.assign(subnormalSeries.current.day,{value:Number.MIN_VALUE,valid:true,relays:8});
const subnormalMarkup=application('?view=explorer&chart=transport_rejections',subnormalFixture).app.markup();
assert.match(subnormalMarkup,/24-hour average<\/div><div class="stat-number">&lt;0\.001 /);

const phantomBand=structuredClone(data),phantomChart=chart('transport_rejections',phantomBand);
const phantomSeries=phantomChart.series.find(s=>s.key===phantomChart.primary_series);
phantomChart.display_scale=10;
Object.assign(phantomSeries.reference.day,{n:100,p10_per_relay:2e307,p50_per_relay:5e307,p90_per_relay:8e307});
const phantomMarkup=application('?view=explorer&chart=transport_rejections&range=all&band=1',phantomBand).app.markup();
assert.match(phantomMarkup,/data-band-toggle[^>]*disabled/);
assert.doesNotMatch(phantomMarkup,/Reference 10th–90th percentiles/);

const missingQuantiles=structuredClone(data),mqChart=chart('transport_rejections',missingQuantiles);
const mqSeries=mqChart.series.find(s=>s.key===mqChart.primary_series);
mqSeries.reference.day={n:100};
if(mqSeries.reference.hour)mqSeries.reference.hour={n:100};
const mqMarkup=application('?view=explorer&chart=transport_rejections',missingQuantiles).app.markup();
assert.match(mqMarkup,/Reference percentile range unavailable/);
assert.doesNotMatch(mqMarkup,/10th and 90th percentiles are equal/);
mqSeries.reference.day={n:-5};
if(mqSeries.reference.hour)mqSeries.reference.hour={n:-5};
assert.match(application('?view=explorer&chart=transport_rejections',missingQuantiles).app.markup(),/Reference percentile range unavailable/);

const nullRefMeta=structuredClone(data),nullRefChart=chart('transport_rejections',nullRefMeta);
const nullRefSeries=nullRefChart.series.find(s=>s.key===nullRefChart.primary_series);
nullRefSeries.reference.day={n:1,start:null,end:null};
nullRefSeries.current.day={value:null,relays:null,valid:false};
if(nullRefSeries.reference.hour)nullRefSeries.reference.hour={n:1,start:null,end:null};
if(nullRefSeries.current.hour)nullRefSeries.current.hour={value:null,relays:null,valid:false};
const nullRefOverview=application('',nullRefMeta).app.markup();
assert.doesNotMatch(nullRefOverview,/1 Jan 1970/);
assert.match(nullRefOverview,/Unavailable reporting relays/);

const partialFlagFixture=structuredClone(data),pfChart=chart('transport_rejections',partialFlagFixture);
const pfSeries=pfChart.series.find(s=>s.key===pfChart.primary_series);
pfSeries.relay_counts={day:{total:104,coverage:0.5,role_bounds:{guard:[24,72],middle_only:[0,48],exit:[8,56],hsdir:[8,56],v2dir:[16,64]}}};
const pfMarkup=application('',partialFlagFixture).app.markup();
assert.match(pfMarkup,/<small class="rank-note">Flag ranges include unclassified relays \(50% flag coverage\)\.<\/small>/);

const fixesMarkup=application('?view=fixes').app.markup();
assert.match(fixesMarkup,/class="status-badge status-open">Open<\/span>/);
assert.match(fixesMarkup,/class="status-badge status-released">Released<\/span>/);
assert.match(fixesMarkup,/rel="noopener noreferrer"/);

let freshSets=0,freshHtml='';
const freshNode={get innerHTML(){return freshHtml;},set innerHTML(v){freshSets++;freshHtml=v;}};
const freshBrowser=application('');
freshBrowser.browser.document.querySelector=s=>s==='.snapshot'?freshNode:freshBrowser.page;
const freshInst=ui.createObservatory(data,freshBrowser.browser,{id:'rel-1',generatedAt:data.end,downloads:new Map(),now:()=>data.end});
freshInst.start();
freshInst.updateFreshness({failed:false});
freshSets=0;
freshInst.updateFreshness({failed:false});
assert.equal(freshSets,0,'unchanged freshness skips DOM innerHTML rewrite');
freshInst.updateFreshness({failed:true});
assert.equal(freshSets,1,'changed freshness updates snapshot DOM');

const winEvents=new Map();
let scrolledTo=[];
const refEl={scrollIntoView(opts){scrolledTo.push(opts);},focus(){}};
const spaCtrl=application('?view=methods#reference');
spaCtrl.browser.addEventListener=(type,cb)=>winEvents.set(type,cb);
spaCtrl.browser.document.getElementById=id=>id==='reference'?refEl:spaCtrl.page;
spaCtrl.app.start();
assert.equal(scrolledTo.length,1);
spaCtrl.browser.location.search='?view=explorer&family=exit&chart=exit_stream_rejections&range=all';
spaCtrl.browser.location.hash='';
winEvents.get('popstate')();
assert.equal(spaCtrl.app.getState().chartId,'exit_stream_rejections');
spaCtrl.browser.location.search='?view=methods';
spaCtrl.browser.location.hash='#reference';
winEvents.get('popstate')();
assert.equal(scrolledTo.length,2);

let menuOpen=true,menuExpanded='true';
const navLinksStub={classList:{contains:c=>c==='show'&&menuOpen,remove:c=>{if(c==='show')menuOpen=false;}}};
const navToggleStub={setAttribute(k,v){if(k==='aria-expanded')menuExpanded=v;},focus(){}};
const origQS=spaCtrl.browser.document.querySelector;
spaCtrl.browser.document.querySelector=s=>s==='.nav-links'?navLinksStub:s==='.nav-toggle'?navToggleStub:origQS(s);
spaCtrl.events.get('keydown')({key:'Escape',target:null});
assert.equal(menuOpen,false);
assert.equal(menuExpanded,'false');
menuOpen=true;menuExpanded='true';
spaCtrl.events.get('click')({target:{closest:s=>null}});
assert.equal(menuOpen,false);
assert.equal(menuExpanded,'false');

// Configurable time_ranges: methods copy, UTC calendar window bounds, strict validation,
// arbitrary custom time_ranges (zero JS changes), and v5 cold-fallback compatibility.
const methodsHtml=application('?view=methods').app.markup();
for(const text of ['7 days','1 month','3 months','6 months','12 months','All time',
 'Last 7 days average','Last 1 month average','Last 3 months average','Last 6 months average','Last 12 months average','All-time average',
 '6 for 7 days','25 for 1 month']){
 assert.ok(methodsHtml.includes(text),`methods() documents ${text}`);
}
if (hasHour) {
 for (const text of ['24 hours','Last complete hour','Sampled 1h fleet peak','Hourly publication and reference updates']) {
  assert.ok(methodsHtml.includes(text),`v7 methods() documents ${text}`);
 }
 assert.doesNotMatch(methodsHtml,/Last 24 hours<\/b> are unweighted|Last 24 hours<\/th>/,'24h snapshot is not mislabeled as a daily average');
}

for(const [endIso,periodId,expectedStartIso] of [
 ['2024-03-31T00:00:00Z','1mo','2024-02-29T00:00:00Z'],
 ['2025-03-31T00:00:00Z','1mo','2025-02-28T00:00:00Z'],
 ['2024-08-31T00:00:00Z','6mo','2024-02-29T00:00:00Z'],
 ['2025-08-31T00:00:00Z','6mo','2025-02-28T00:00:00Z'],
 ['2026-03-31T00:00:00Z','6mo','2025-09-30T00:00:00Z'],
 ['2026-11-18T00:00:00Z','7d','2026-11-11T00:00:00Z'],
 ['2026-11-18T00:00:00Z','12mo','2025-11-18T00:00:00Z'],
]){
 const calChart=structuredClone(chart('transport_rejections'));
 const endTs=Date.parse(endIso)/1000;
 calChart.end=endTs;
 for(const s of calChart.series)s.history.daily=[[endTs,10,100,1,true]];
 const prepared=ui.preparePlot(calChart,calChart.series,periodId,data.time_ranges);
 assert.equal(prepared.start,Date.parse(expectedStartIso)/1000,`calendar plot start for ${endIso} / ${periodId}`);
}

const dayIdx = d => d.time_ranges.periods.findIndex(p => p.window.unit === 'days');
const monIdx = d => d.time_ranges.periods.findIndex(p => p.window.unit === 'months');
for(const mutate of [
 d=>{delete d.time_ranges;},
 d=>{d.time_ranges.version=2;},
 d=>{d.time_ranges.periods=[];},
 d=>{const dp=d.time_ranges.periods[dayIdx(d)];d.time_ranges.periods=Array.from({length:17},(_,i)=>({...structuredClone(dp),id:`p${i}`,summary_key:`p_${i}`,export_suffix:`p${i}`}));},
 d=>{d.time_ranges.periods=d.time_ranges.periods.filter(p=>p.window.unit!=='all_time');d.time_ranges.card_slots.fixed=['7d','6mo'];},
 d=>{const dup=structuredClone(d.time_ranges.periods.at(-1));dup.id='all2';dup.summary_key='all_two';dup.export_suffix='all-two';d.time_ranges.periods.push(dup);},
 d=>{d.time_ranges.periods[dayIdx(d)].id='24h';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='1h';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='30m';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='60s';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='1d';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='024h';},
 d=>{d.time_ranges.periods[dayIdx(d)].id='23h';},
 d=>{d.time_ranges.periods[dayIdx(d)].export_suffix='24h';},
 d=>{d.time_ranges.periods[dayIdx(d)].export_suffix='foo-24h';},
 d=>{d.time_ranges.periods[dayIdx(d)].summary_key='day';},
 ...['hour','sampled_hour','hourly_relays','hourly_expected_relays','current24'].flatMap(key=>[
  d=>{d.time_ranges.periods[dayIdx(d)].summary_key=key;},
  d=>{d.time_ranges.legacy_aliases[key]='1mo';},
 ]),
 d=>{d.time_ranges.periods[dayIdx(d)].window.count=1;},
 d=>{d.time_ranges.periods[dayIdx(d)].minimum_coverage=-0;},
 d=>{d.time_ranges.periods[dayIdx(d)].minimum_coverage=-0.1;},
 d=>{d.time_ranges.periods[dayIdx(d)].minimum_coverage=1.1;},
 d=>{d.time_ranges.periods[monIdx(d)].minimum_coverage=-0;},
 d=>{d.time_ranges.periods[monIdx(d)].minimum_coverage=-0.1;},
 d=>{d.time_ranges.periods[monIdx(d)].minimum_coverage=1.1;},
 d=>{d.time_ranges.periods.at(-1).minimum_coverage=-0;},
 d=>{d.time_ranges.periods.at(-1).minimum_coverage=0.5;},
 d=>{d.time_ranges.card_slots.fixed=['6mo','6mo'];},
 d=>{d.time_ranges.card_slots.fallback_dynamic=d.time_ranges.card_slots.fixed[0];},
 d=>{d.time_ranges.periods[dayIdx(d)].label='🚀'.repeat(81);},
 d=>{d.time_ranges.periods[dayIdx(d)].average_label='🚀'.repeat(81);},
 d=>{d.time_ranges.periods[dayIdx(d)].plot_label='🚀'.repeat(81);},
 d=>{d.time_ranges.periods[dayIdx(d)].label='\uFEFF';},
 d=>{d.time_ranges.periods[dayIdx(d)].average_label='\x85';},
 d=>{d.time_ranges.periods[dayIdx(d)].plot_label='\x1f';},
 d=>{d.time_ranges.legacy_aliases['🚀'.repeat(33)]='1mo';},
 d=>{d.time_ranges.legacy_aliases['\uFEFF']='1mo';},
 d=>{d.time_ranges.legacy_aliases['\x85']='1mo';},
 d=>{d.time_ranges.legacy_aliases['\x1c']='1mo';},
 d=>{d.time_ranges.legacy_aliases['7d']='1mo';},
 d=>{d.time_ranges.legacy_aliases['30d']='missing';},
]){
 const badTr=structuredClone(data);
 mutate(badTr);
 assert.throws(()=>application('',badTr),/time_ranges|all_time|minimum_coverage/);
}
assert.equal(ui.parseLocation(data,'?range=1mo').range,'1mo');

for(const cov of [0,0.9]){
 const covData=structuredClone(data);
 covData.time_ranges.periods[dayIdx(covData)].minimum_coverage=cov;
 covData.time_ranges.periods[monIdx(covData)].minimum_coverage=cov;
 assert.equal(ui.validateTimeRanges(covData.time_ranges).periods[dayIdx(covData)].minimum_coverage,cov,`validateTimeRanges accepts minimum_coverage=${cov} on days/months`);
}

// Partial-period coverage (minimum_coverage: 0), n >= 2 Explorer percentile band,
// .average-dates narrowed hover titles, methods() wording, and 0-day/explicit insufficient_data states.
assert.ok(methodsHtml.includes('uses all available qualifying days in the window'),
 'methods() documents zero minimum_coverage cleanly instead of 0% threshold');
assert.doesNotMatch(methodsHtml,/at least 0% of days|requires ≥ 0%/);
const customMethodsHtml=application('?view=methods',(()=>{
 const d=structuredClone(data);
 d.time_ranges.periods[dayIdx(d)].minimum_coverage=0.9;
 return d;
})()).app.markup();
assert.ok(customMethodsHtml.includes('at least 90% of days required'),'methods() keeps percentage wording for non-zero thresholds');

const v6HistOffset=0,v6HistSliceEnd=2;
for(const cid of ['hsdir_fetches','directory_rejections','protocol_closes']){
 const ch=chart(cid,v6Data),prim=ch.series.find(s=>s.key===ch.primary_series)||ch.series[0];
 for(const p of v6Data.time_ranges.periods){
  assert.equal(prim.historical_averages[p.summary_key].status,'ready',`${cid} primary ${p.id} is ready under partial coverage`);
 }
 for(const r of v6Ranges){
  const html=application(`?view=explorer&chart=${cid}&range=${r}`,v6Data).app.markup();
  const histPanels=statSections(html).slice(1);
  assert.equal(histPanels.length,3);
  for(const panel of histPanels){
   assert.doesNotMatch(panel.html,/<div class="stat-number"[^>]*>— <small>/,`${cid}/${r} shows historical average`);
   assert.doesNotMatch(panel.html,/Insufficient historical coverage/,`${cid}/${r} shows no insufficient coverage fallback`);
   assert.doesNotMatch(panel.html,/Current 24h: Percentile unavailable/,`${cid}/${r} shows historical comparison percentile`);
  }
  const tbl=html.match(/<table data-values-table>([\s\S]*?)<\/table>/)[1];
  const primRow=tbl.match(/<tbody>([\s\S]*?)<\/tbody>/)[1].match(/<tr>[\s\S]*?<\/tr>/g)
   .find(row=>decode(row.match(/<td>(.*?)<\/td>/)[1])===prim.name);
  assert.ok(primRow,`${cid} values table row present`);
  assert.doesNotMatch(primRow,/<td>—<\/td>/,`${cid}/${r} primary row has no blank historical cells`);
 }
}

// 19-reference directory_rejections enables the Explorer percentile band (n >= 2), while n < 2 keeps it disabled.
const dirRejBandHtml=application('?view=explorer&chart=directory_rejections&range=1mo&band=1').app.markup();
assert.doesNotMatch(dirRejBandHtml,/data-band-toggle[^>]*disabled/,'directory_rejections 19-ref band is enabled at n >= 2');
assert.match(dirRejBandHtml,/Reference 10th–90th percentiles/);
assert.match(dirRejBandHtml,/Reference median/);
const dirRejBandPlot=element(800);
application('?view=explorer&chart=directory_rejections&range=1mo&band=1').app.draw(dirRejBandPlot,chart('directory_rejections'),'1mo');
assert.match(dirRejBandPlot.innerHTML,/class="reference-band"/);
assert.match(dirRejBandPlot.innerHTML,/class="reference-median"/);

const bandThresholdData=structuredClone(data),btChart=chart('directory_rejections',bandThresholdData);
const btSeries=btChart.series.find(s=>s.key===btChart.primary_series);
btSeries.reference.day={n:1,p10_per_relay:1,p50_per_relay:2,p90_per_relay:3,mean_per_relay:2,current_percentile:null,flat_zero:false};
const oneRefHtml=application('?view=explorer&chart=directory_rejections&range=1mo&band=1',bandThresholdData).app.markup();
assert.match(oneRefHtml,/data-band-toggle[^>]*disabled/);
assert.match(oneRefHtml,/1 reference windows; 2 required/);
btSeries.reference.day={n:2,p10_per_relay:1,p50_per_relay:2,p90_per_relay:3,mean_per_relay:2,current_percentile:50,flat_zero:false};
assert.doesNotMatch(application('?view=explorer&chart=directory_rejections&range=1mo&band=1',bandThresholdData).app.markup(),/data-band-toggle[^>]*disabled/);

// Narrowed partial window .average-dates hover title vs full-window no-title.
const narrowedHtml=application('?view=explorer&chart=directory_rejections&range=6mo',v6Data).app.markup();
const dirRej6mo=chart('directory_rejections',v6Data).series.find(s=>s.key===chart('directory_rejections',v6Data).primary_series).historical_averages.six_months;
assert.ok(dirRej6mo.observed_start>dirRej6mo.start,'directory_rejections 6mo has narrower observed_start than calendar start');
const avgDateTag=narrowedHtml.match(/<div class="average-dates"[^>]*>[^<]+<\/div>/g)[1];
assert.match(avgDateTag,/title="Measured observations: [^"]+ UTC\."/);
const exactDatesData=structuredClone(v6Data),exChart=chart('transport_rejections',exactDatesData);
const exSeries=exChart.series.find(s=>s.key===exChart.primary_series);
const ex6mo=exSeries.historical_averages.six_months;
ex6mo.observed_start=ex6mo.start;ex6mo.observed_end=ex6mo.end;
const exactDateTag=application('?view=explorer&chart=transport_rejections&range=all',exactDatesData).app.markup().match(/<div class="average-dates"[^>]*>[^<]+<\/div>/g)[1];
assert.doesNotMatch(exactDateTag,/title=/,'full-window summary omits redundant .average-dates title');

// Explicit 0-day / insufficient_data states remain well-behaved under minimum_coverage: 0 and 0.9.
const zeroDayData=structuredClone(v6Data),zdChart=chart('transport_rejections',zeroDayData);
const zdSeries=zdChart.series.find(s=>s.key===zdChart.primary_series);
Object.assign(zdSeries.historical_averages.six_months,{days:0,coverage:0,status:'insufficient_data',value:null,observed_start:null,observed_end:null});
const zeroDayPanels=statSections(application('?view=explorer&chart=transport_rejections&range=all',zeroDayData).app.markup());
assert.match(zeroDayPanels[2].html,/0 \/ \d+ days available/);
assert.match(zeroDayPanels[2].html,/title="No qualifying daily observations are available">— <small>/);
assert.doesNotMatch(zeroDayPanels[2].html,/<div class="average-dates"[^>]*title=/);
assert.match(zeroDayPanels[2].html,/Insufficient historical coverage/);
zeroDayData.time_ranges.periods.find(p=>p.id==='6mo').minimum_coverage=0.9;
const gatedZeroPanels=statSections(application('?view=explorer&chart=transport_rejections&range=all',zeroDayData).app.markup());
assert.match(gatedZeroPanels[2].html,/At least 90% daily coverage is required for last 6 months average/);

const unicodeTr=structuredClone(data);
unicodeTr.time_ranges.periods[dayIdx(unicodeTr)].label='🚀'.repeat(80);
unicodeTr.time_ranges.periods[dayIdx(unicodeTr)].average_label='🚀'.repeat(80);
unicodeTr.time_ranges.periods[dayIdx(unicodeTr)].plot_label='🚀'.repeat(80);
unicodeTr.time_ranges.legacy_aliases['🚀'.repeat(32)]='7d';
assert.equal(ui.parseLocation(unicodeTr,'?range='+encodeURIComponent('🚀'.repeat(32))).range,'7d',
 'supplementary Unicode code-point limits accept 80-char labels and 32-char legacy aliases');

const customData=structuredClone(v6Data);
customData.time_ranges={
 version:1,
 default_range:'14d',
 legacy_aliases:{'24h':'14d','30d':'14d'},
 card_slots:{fallback_dynamic:'14d',fixed:['2mo','all']},
 periods:[
  {id:'14d',summary_key:'fourteen_days',export_suffix:'14d',label:'14 days',average_label:'Last 14 days average',plot_label:'Last 14 days',window:{unit:'days',count:14,comparison_lag_days:1},minimum_coverage:0.9,minimum_comparison_windows:12},
  {id:'2mo',summary_key:'two_months',export_suffix:'2mo',label:'2 months',average_label:'Last 2 months average',plot_label:'Last 2 months',window:{unit:'months',count:2,comparison_lag_days:0},minimum_coverage:0.9,minimum_comparison_windows:30},
  {id:'all',summary_key:'all_time',export_suffix:'all-time',label:'All time',average_label:'All-time average',plot_label:'All time',window:{unit:'all_time',count:null,comparison_lag_days:0},minimum_coverage:0,minimum_comparison_windows:30},
 ],
};
for(const c of customData.charts)for(const s of c.series){
 const all=s.historical_averages.all_time,allRc=s.relay_counts?.all_time;
 s.historical_averages={fourteen_days:{...structuredClone(all),value:14,display:'14'},two_months:{...structuredClone(all),value:60,display:'60'},all_time:all};
 if(s.relay_counts)s.relay_counts={day:s.relay_counts.day,fourteen_days:structuredClone(allRc),two_months:structuredClone(allRc),all_time:allRc};
}
const customOverview=application('?range=24h',customData);
customOverview.app.start();
assert.equal(customOverview.app.getState().range,'14d','custom legacy alias maps 24h to 14d');
assert.deepEqual(customOverview.browser.document.querySelectorAll('[data-all-range]').map(b=>b.textContent),['14 days','2 months','All time']);
for(const [r,expectedCsv] of [['14d','14d'],['2mo','2mo'],['all','all-time']]){
 const customExp=application(`?view=explorer&chart=transport_rejections&range=${r}`,customData).app.markup();
 const labels=statSections(customExp).map(sec=>sec.html.match(/<div class="stat-label">([^<]+)<\/div>/)?.[1]);
 assert.deepEqual(labels,['24-hour average','Last 14 days average','Last 2 months average','All-time average']);
 assert.match(customExp,new RegExp(`exports\\/transport_rejections-${expectedCsv}\\.csv`));
 const customTable=customExp.match(/<table data-values-table>([\s\S]*?)<\/table>/)[1];
 assert.deepEqual([...customTable.matchAll(/<th>(.*?)<\/th>/g)].map(([,h])=>decode(h)),
  ['Series','Unit','24-hour average','Last 14 days average','Last 2 months average','All-time average']);
}

const v5Data=structuredClone(v6Data);
v5Data.schema_version=5;
delete v5Data.time_ranges;
for(const c of v5Data.charts)for(const s of c.series){
 s.historical_averages={thirty_days:s.historical_averages.one_month,six_months:s.historical_averages.six_months,all_time:s.historical_averages.all_time};
 if(s.relay_counts)s.relay_counts={day:s.relay_counts.day,thirty_days:s.relay_counts.one_month,six_months:s.relay_counts.six_months,all_time:s.relay_counts.all_time};
}
const v5Paths=['data.json','manifest.json',v5Data.reference_document,
 ...v5Data.charts.flatMap(item=>['30d','all-time'].map(span=>'exports/'+item.id+'-'+span+'.csv'))];
const v5Release={mode:'live',id:'v5-old',generatedAt:v5Data.end+600,now:()=>v5Data.end,
 downloads:new Map(v5Paths.map(file=>[file,'blob:v5-old/'+file]))};
const v5App=application('?view=explorer&chart=transport_rejections&range=6mo',v5Data,'1',v5Release);
v5App.app.start();
assert.match(v5App.page.innerHTML,/href="blob:v5-old\/exports\/transport_rejections-all-time\.csv" download="v5-old-transport_rejections-all-time\.csv" data-focus-key="explorer-csv">CSV · all-time, source units<\/a>/,'v5 fallback maps missing 6mo CSV to all-time CSV and labels it accurately');
const v5ExactApp=application('?view=explorer&chart=transport_rejections&range=30d',v5Data,'1',v5Release);
v5ExactApp.app.start();
assert.match(v5ExactApp.page.innerHTML,/href="blob:v5-old\/exports\/transport_rejections-30d\.csv"[^>]*>CSV · all series, source units<\/a>/,'exact period CSV retains standard label');
const v5Upgrading=application('?view=overview',v5Data,'1',v5Release);
v5Upgrading.app.start();
assert.equal(v5Upgrading.app.getState().range,'30d');
v5Upgrading.app.replaceRelease(v6Data,releaseContext(v6Data,'v6-new',()=>v6Data.end));
assert.equal(v5Upgrading.app.getState().range,'1mo','live upgrade from v5 30d migrates via legacy_aliases to 1mo');

// V4 Concurrent DoS hero card on v6 fallback: Overview isolation (7d|all), non-overlapping math, responsive SVGs, URL/live/keyboard parity.
const heroDefault=application('?view=overview',v6Data);
heroDefault.app.start();
assert.equal(heroDefault.app.getState().heroRange,'7d');
assert.equal(heroDefault.app.getState().heroScale,'linear');
assert.equal(heroDefault.app.getState().heroHsdir,false);
assert.equal(heroDefault.app.getState().range,'1mo');
const heroHtml=heroDefault.page.innerHTML;
const heroSectionPos=heroHtml.indexOf('<section id="concurrent-dos-overview" class="chart-card hero-card" data-hero-card-range="7d">');
const toolbarPos=heroHtml.indexOf('<div class="overview-toolbar">');
assert.ok(heroSectionPos!==-1&&toolbarPos!==-1&&heroSectionPos<toolbarPos,'hero card leads overview above family toolbar');
assert.equal(heroDefault.browser.document.querySelectorAll('[data-hero-range]').length,2);
assert.equal(heroDefault.browser.document.querySelectorAll('[data-hero-scale]').length,2);
assert.equal(heroDefault.browser.document.querySelectorAll('[data-hero-hsdir]').length,1);
assert.equal(heroDefault.browser.document.querySelectorAll('[data-hero-panel]').length,2);
assert.equal(heroDefault.browser.document.querySelector('[data-hero-panel="7d"]').getAttribute('class'),'hero-panel active desktop-left');
assert.equal(heroDefault.browser.document.querySelector('[data-hero-panel="all"]').getAttribute('class'),'hero-panel desktop-right');
assert.match(heroHtml,/data-hero-plot="concurrent_dos_7d" data-hero-plot-range="7d"/);
assert.match(heroHtml,/data-hero-plot="concurrent_dos_all" data-hero-plot-range="all"/);
assert.match(heroHtml,/Last 7 days[\s\S]*7 × 24h[\s\S]*All time[\s\S]*548 × 24h/);
assert.equal(heroDefault.app.url('overview'),'?view=overview&range=1mo&layout=full&family=directory&chart=directory_requests',
 'default hero settings omit extra query parameters');
click(heroDefault,'[data-hero-range]',{heroRange:'all'});
assert.equal(heroDefault.browser.document.querySelector('[data-hero-panel="7d"]').getAttribute('class'),'hero-panel desktop-left');
assert.equal(heroDefault.browser.document.querySelector('[data-hero-panel="all"]').getAttribute('class'),'hero-panel active desktop-right');
assert.equal(heroDefault.app.getState().heroRange,'all');
assert.equal(heroDefault.app.getState().range,'1mo','hero All time toggle leaves 6-family 1mo range unchanged');
assert.equal(heroDefault.browser.document.activeElement,heroDefault.browser.document.querySelector('[data-hero-range="all"]'));
click(heroDefault,'[data-all-range]',{allRange:'all'});
assert.equal(heroDefault.app.getState().range,'all');
assert.equal(heroDefault.app.getState().heroRange,'all');
click(heroDefault,'[data-all-range]',{allRange:'1mo'});
assert.equal(heroDefault.app.getState().range,'1mo');
assert.equal(heroDefault.app.getState().heroRange,'all','family range toggle leaves hero range unchanged');
click(heroDefault,'[data-hero-scale]',{heroScale:'symlog'});
assert.equal(heroDefault.app.getState().heroScale,'symlog');
assert.equal(heroDefault.browser.document.activeElement,heroDefault.browser.document.querySelector('[data-hero-scale="symlog"]'));
click(heroDefault,'[data-hero-hsdir]',{});
assert.equal(heroDefault.app.getState().heroHsdir,true);
assert.equal(heroDefault.browser.document.activeElement,heroDefault.browser.document.querySelector('[data-hero-hsdir]'));
const heroParams=new URLSearchParams(heroDefault.app.url('overview').slice(1));
assert.equal(heroParams.get('hero_range'),'all');
assert.equal(heroParams.get('hero_scale'),'symlog');
assert.equal(heroParams.get('hero_hsdir'),'1');
assert.deepEqual(ui.parseLocation(v6Data,heroDefault.browser.location.search),heroDefault.app.getState(),
 'hero state round-trips between URL and parseLocation');
heroDefault.back();
assert.equal(heroDefault.app.getState().heroHsdir,false);
assert.equal(heroDefault.app.getState().heroScale,'symlog');
heroDefault.forward();
assert.equal(heroDefault.app.getState().heroHsdir,true);
click(heroDefault,'[data-hero-range]',{heroRange:'7d'});
click(heroDefault,'[data-hero-scale]',{heroScale:'linear'});
click(heroDefault,'[data-hero-hsdir]',{});
assert.deepEqual([heroDefault.app.getState().heroRange,heroDefault.app.getState().heroScale,heroDefault.app.getState().heroHsdir],['7d','linear',false]);
assert.equal(new URLSearchParams(heroDefault.app.url('overview').slice(1)).get('hero_range'),'7d');
click(heroDefault,'[data-hero-range]',{heroRange:'all'});
click(heroDefault,'[data-hero-range]',{heroRange:'7d'});
click(heroDefault,'[data-hero-range]',{heroRange:'7d'});
click(heroDefault,'[data-hero-scale]',{heroScale:'linear'});
heroDefault.back();
assert.equal(heroDefault.app.getState().heroRange,'all','no-op hero clicks never push duplicate history entries');

const h7=ui.prepareHeroPlot(v6Data,'7d',true);
const h7NoHsdir=ui.prepareHeroPlot(v6Data,'7d',false);
const hAll=ui.prepareHeroPlot(v6Data,'all',true);
assert.equal(h7.times.length,7);
assert.equal(h7.end-h7.start,7*86400);
assert.equal(hAll.times.length,548);
assert.ok(h7.vectors.some(v=>v.id==='hsdir'));
assert.equal(h7NoHsdir.vectors.some(v=>v.id==='hsdir'),false);
const hsdirVec=h7.vectors.find(v=>v.id==='hsdir');
for(let i=0;i<7;i++){
 assert.ok(Math.abs((h7.totals[i]-h7NoHsdir.totals[i])-hsdirVec.values[i])<1e-9,'HSDir toggle subtracts hs_get+hs_post cleanly');
}
const overlapCheckData=structuredClone(v6Data);
for(const row of chart('onionskin_drops',overlapCheckData).series.find(s=>s.key==='ntor_v3').history.daily)row[1]=999999;
for(const row of chart('queue_limit_circuits',overlapCheckData).series.find(s=>s.key==='circuit_killed_max_cell_outq').history.daily)row[1]=999999;
const hsSuccess=chart('hsdir_fetches',overlapCheckData).series.find(s=>s.key==='success');
for(const row of hsSuccess.history.daily)row[1]=999999;
hsSuccess.reference.day={n:100,current_percentile:100,flat_zero:false};
const hOverlap=ui.prepareHeroPlot(overlapCheckData,'7d',true);
assert.deepEqual(hOverlap.totals,h7.totals,'subset counters never double-count into total hero rates');
const hsUploadSuccess=chart('hsdir_uploads',overlapCheckData).series.find(s=>s.key==='success');
hsUploadSuccess.reference.day={n:100,current_percentile:100,flat_zero:false};
for(const fam of h7.families)assert.equal(fam.rows.length,7);
const allNormalAlerts=structuredClone(v6Data);
for(const f of allNormalAlerts.families)for(const cid of f.children)for(const s of chart(cid,allNormalAlerts).series){
 s.reference.day={n:100,current_percentile:10,flat_zero:false};
}
chart('hsdir_fetches',allNormalAlerts).series.find(s=>s.key==='success').reference.day={n:100,current_percentile:100,flat_zero:false};
chart('hsdir_uploads',allNormalAlerts).series.find(s=>s.key==='success').reference.day={n:100,current_percentile:100,flat_zero:false};
const noFalseAlert=ui.prepareHeroPlot(allNormalAlerts,'7d',true);
assert.equal(noFalseAlert.families.find(f=>f.id==='directory').rows[6].state,'neutral','benign hsdir success counters never trigger DoS alerts');

for(const heroRange of ['7d','all'])for(const heroScale of ['linear','symlog'])for(const includeHsdir of [true,false]){
 const app=application(`?view=overview&hero_range=${heroRange}&hero_scale=${heroScale}${includeHsdir?'&hero_hsdir=1':''}`,v6Data).app;
 for(const width of [240,262,360,800,1680]){
  const plot=element(width);
  app.drawHero(plot,heroRange,heroScale,includeHsdir);
  assert.doesNotMatch(plot.innerHTML,/NaN|Infinity|undefined|Invalid Date/,`hero/${heroRange}/${heroScale}/${includeHsdir}/${width}`);
  assert.match(plot.innerHTML,/<svg/);
  assert.equal((plot.innerHTML.match(/Source: 1aeo\.com - OnionSurge/g)||[]).length,1);
  assert.match(plot.innerHTML,/24h intervals · UTC/);
  if(heroScale==='symlog')assert.match(plot.innerHTML,/class="hero-rate-series"/);
  else if(heroRange==='7d')assert.match(plot.innerHTML,/class="hero-rate-bar"/);
  else assert.match(plot.innerHTML,/class="hero-rate-area"/);
  if(heroRange==='all')assert.match(plot.innerHTML,/class="hero-alert-missing"/);
 }
}
const heroExtreme=structuredClone(v6Data);
chart('transport_rejections',heroExtreme).series[0].history.daily.forEach(row=>{if(row[4])row[1]=Number.MAX_VALUE;});
const heroExtremePlot=element(800);
application('',heroExtreme).app.drawHero(heroExtremePlot,'7d','symlog',true);
assert.doesNotMatch(heroExtremePlot.innerHTML,/NaN|Infinity|undefined/);
const heroEmpty=structuredClone(v6Data);
for(const item of heroEmpty.charts)for(const series of item.series)series.history.daily=[];
const heroEmptyPlot=element(240);
application('',heroEmpty).app.drawHero(heroEmptyPlot,'7d','linear',true);
assert.match(heroEmptyPlot.innerHTML,/No retained samples available/);
assert.equal((heroEmptyPlot.innerHTML.match(/Source: 1aeo\.com - OnionSurge/g)||[]).length,1);

for(const [dataset,urlQuery,plots] of [
 [v6Data,'?view=overview&hero_range=all&hero_scale=symlog&hero_hsdir=1',['[data-hero-range="all"]','[data-hero-scale="symlog"]','[data-hero-hsdir]','[data-hero-plot="concurrent_dos_7d"]','[data-hero-plot="concurrent_dos_all"]']],
 [data,'?view=overview&hero_range=all&hero_scale=symlog&hero_hsdir=1',['[data-hero-range="all"]','[data-hero-scale="symlog"]','[data-hero-hsdir]','[data-hero-plot="concurrent_dos_24h"]','[data-hero-plot="concurrent_dos_all"]']],
]){
 for(const selector of plots){
  const heroLive=application(urlQuery,dataset,'1',releaseContext(dataset,'hero-old',()=>dataset.end));
  heroLive.app.start();
  const before=heroLive.app.getState();
  heroLive.browser.document.querySelector(selector).focus({preventScroll:true});
  const fresh=structuredClone(dataset);
  heroLive.app.replaceRelease(fresh,releaseContext(fresh,'hero-new',()=>fresh.end));
  assert.deepEqual(heroLive.app.getState(),before,'replaceRelease preserves hero controls');
  assert.equal(heroLive.browser.document.activeElement,heroLive.browser.document.querySelector(selector),
   `replaceRelease restores focus on ${selector}`);
 }
}

const inspectHero=(dataset,r,keys)=>{
 const ctrl=application('?view=overview',dataset),live={textContent:''};
 let cursor=null,tooltip=null;
 const svg={querySelector:s=>s==='.plot-cursor'?cursor:null,appendChild:n=>{cursor=n;}};
 const el={innerHTML:'',getBoundingClientRect:()=>({width:800,height:400}),closest:s=>s==='[data-hero-plot]'?el:null,
  querySelector:s=>s==='svg'?svg:s==='.plot-cursor'?cursor:s==='.chart-tooltip'?tooltip:null,
  appendChild:n=>{tooltip=n;},setAttribute(){},hasAttribute:()=>false};
 const origQS=ctrl.browser.document.querySelector;
 ctrl.browser.document.querySelector=s=>s==='[data-live-status]'?live:origQS(s);
 ctrl.browser.document.createElement=tag=>({tag,className:'',innerHTML:'',style:{},remove(){if(this===tooltip)tooltip=null;}});
 ctrl.browser.document.createElementNS=(ns,tag)=>({ns,tag,attrs:{},setAttribute(k,v){this.attrs[k]=v;},remove(){if(this===cursor)cursor=null;}});
 ctrl.app.start();
 ctrl.app.drawHero(el,r,'linear',true);
 const steps=keys.map(key=>{
  let prevented=false;
  ctrl.events.get('keydown')({key,target:el,preventDefault(){prevented=true;}});
  return {prevented,cursor,tooltipHtml:tooltip?.innerHTML||'',liveText:live.textContent};
 });
 return {ctrl,el,steps};
};

const hDefaultInspect=inspectHero(data,'7d',['ArrowLeft','Escape']);
assert.equal(hDefaultInspect.steps[0].prevented,true);
assert.equal(hDefaultInspect.steps[0].cursor.attrs.class,'plot-cursor');
assert.ok(hDefaultInspect.steps[0].tooltipHtml.includes('Total active rate'));
assert.match(hDefaultInspect.steps[0].tooltipHtml,/Open circuits:/);
assert.ok(hDefaultInspect.steps[0].liveText.includes('Concurrent DoS activity'));
assert.equal(hDefaultInspect.steps[1].cursor,null);

// Hero regression & adversarial hardening checks
const makeRows=(end,totalDays,valFn)=>Array.from({length:totalDays},(_,k)=>[end-(totalDays-1-k)*86400,valFn(k),100,1,true]);
const setFamRows=(doc,fid,rows)=>{for(const cid of doc.families.find(f=>f.id===fid).children)for(const s of chart(cid,doc).series)s.history.daily=rows.map(r=>[...r]);};
const baselineCapBase=structuredClone(data);
setFamRows(baselineCapBase,'transport',makeRows(baselineCapBase.end-86400,21,k=>k===18?10000:k>=19?20000:(k+1)*100));
const baselineCapLow=structuredClone(baselineCapBase),baselineCapHigh=structuredClone(baselineCapBase);
for(const cid of baselineCapLow.families.find(f=>f.id==='transport').children){
 for(const s of chart(cid,baselineCapLow).series)s.history.daily.push([baselineCapLow.end,0,100,1,true]);
 for(const s of chart(cid,baselineCapHigh).series)s.history.daily.push([baselineCapHigh.end,50000,100,1,true]);
}
const candLow=ui.prepareHeroPlot(baselineCapLow,'7d',true).families.find(f=>f.id==='transport').rows[3];
assert.equal(candLow.state,'elevated');assert.equal(candLow.rank,90);
assert.deepEqual(ui.prepareHeroPlot(baselineCapHigh,'7d',true).families.find(f=>f.id==='transport').rows[3],candLow,'latest UTC interval excluded from baseline');

// Duplicate historical rows never inflate baseline denominator or shift ranks.
const dupBaseline=structuredClone(baselineCapLow);
for(const cid of dupBaseline.families.find(f=>f.id==='transport').children){
 for(const s of chart(cid,dupBaseline).series)for(let d=0;d<5;d++)s.history.daily.unshift([...s.history.daily[0]]);
}
assert.deepEqual(ui.prepareHeroPlot(dupBaseline,'7d',true).families.find(f=>f.id==='transport').rows[3],candLow,'duplicate timestamps deduplicated before baseline ranking');

// Exact 18-month reference window boundary (t - DAY >= baselineStart && t <= baselineEnd).
const boundData=structuredClone(baselineCapLow);
boundData.reference_window={...boundData.reference_window,start:boundData.end-21*86400,end:boundData.end-86400};
const boundCell=ui.prepareHeroPlot(boundData,'7d',true).families.find(f=>f.id==='transport').rows[3];
assert.equal(boundCell.n,19,'row starting before reference_window.start (t === start) is excluded from baseline');
assert.equal(boundCell.state,'neutral');

// Out-of-range reference percentiles cannot emit false alerts or bogus ordinal strings.
const badRef=structuredClone(allNormalAlerts);
chart('transport_rejections',badRef).series[0].current.day={valid:true,value:100,relays:100,available:1};
for(const badPct of [150,-5,null]){
 chart('transport_rejections',badRef).series[0].reference.day={n:120,current_percentile:badPct,flat_zero:false};
 assert.equal(ui.prepareHeroPlot(badRef,'7d',true).families.find(f=>f.id==='transport').rows[6].state,'neutral',`invalid percentile ${badPct} stays neutral`);
}

const gaugeAlertGuard=structuredClone(allNormalAlerts);
chart('open_circuits',gaugeAlertGuard).series[0].reference.day={n:500,current_percentile:100,flat_zero:false};
chart('directory_waiting',gaugeAlertGuard).series[0].reference.day={n:500,current_percentile:100,flat_zero:false};
const hGaugeGuard=ui.prepareHeroPlot(gaugeAlertGuard,'7d',true);
assert.equal(hGaugeGuard.families.find(f=>f.id==='circuits').rows[6].state,'neutral');
assert.equal(hGaugeGuard.families.find(f=>f.id==='directory').rows[6].state,'neutral');

const subOneGauge=structuredClone(data),dwSeries=chart('directory_waiting',subOneGauge).series[0];
dwSeries.peaks.day.fleet_peak=0.4;
dwSeries.history.daily=dwSeries.history.daily.map(row=>row[0]===subOneGauge.end?[row[0],0.4,104,1,true]:row[4]?[row[0],0.2,104,1,true]:row);
const hSubOne=ui.prepareHeroPlot(subOneGauge,'7d',true);
assert.equal(hSubOne.gauges.directoryWaiting.scalePeak,0.4);
const subOnePlot=element(800);
application('',subOneGauge).app.drawHero(subOnePlot,'7d','linear',true);
assert.match(subOnePlot.innerHTML,/<line x1="62" y1="256\.00"[^>]*\/>\s*<text[^>]*>100%<\/text>/);
assert.match(subOnePlot.innerHTML,/class="hero-gauge-series" d="M[^"]*L788\.00,256\.00"/);

// Tiny openCircuits (<0.1% fleet record) uses local windowPeak on 24h and 7d and rawPeak on All time.
const tinyGauge=structuredClone(data),ocSeries=chart('open_circuits',tinyGauge).series[0];
ocSeries.peaks.day.fleet_peak=1e6;
ocSeries.peaks.hour.fleet_peak=1e6;
ocSeries.history.daily=ocSeries.history.daily.map(row=>row[0]===tinyGauge.end?[row[0],100,104,1,true]:row[4]?[row[0],50,104,1,true]:row);
ocSeries.values=ocSeries.values.map(([t],idx)=>[t,idx===23?100:50]);
ocSeries.hourly_relays=ocSeries.hourly_relays.map(([t])=>[t,104]);
ocSeries.hourly_expected_relays=ocSeries.hourly_expected_relays.map(([t])=>[t,104]);
const tiny24=ui.prepareHeroPlot(tinyGauge,'24h',true),tiny7=ui.prepareHeroPlot(tinyGauge,'7d',true),tinyAll=ui.prepareHeroPlot(tinyGauge,'all',true);
assert.equal(tiny24.gauges.openCircuits.windowPeak,100);
assert.equal(tiny24.gauges.openCircuits.scalePeak,100,'24h normalizes gauge curve to 24h peak');
assert.equal(tiny7.gauges.openCircuits.windowPeak,100);
assert.equal(tiny7.gauges.openCircuits.scalePeak,100,'7d normalizes gauge curve to 7d peak');
assert.equal(tinyAll.gauges.openCircuits.scalePeak,1e6,'all-time keeps gauge normalized to retained fleet record');
assert.match(application('?view=overview',tinyGauge).app.markup(),/&lt;0\.1% of 1M peak/);
assert.match(inspectHero(tinyGauge,'24h',['End']).steps[0].tooltipHtml,/Open circuits: 100 \(&lt;0\.1%, 100% of 24h peak\)/);
assert.match(inspectHero(tinyGauge,'7d',['End']).steps[0].tooltipHtml,/Open circuits: 100 \(&lt;0\.1%, 100% of 7d peak\)/);
assert.match(inspectHero(tinyGauge,'all',['End']).steps[0].tooltipHtml,/Open circuits: 100 \(&lt;0\.1%\)/);
const zeroWinGauge=structuredClone(tinyGauge);
chart('open_circuits',zeroWinGauge).series[0].history.daily.forEach(row=>{if(row[0]>=zeroWinGauge.end-6*86400)row[1]=0;});
chart('open_circuits',zeroWinGauge).series[0].values=chart('open_circuits',zeroWinGauge).series[0].values.map(([t])=>[t,0]);
assert.equal(ui.prepareHeroPlot(zeroWinGauge,'7d',true).gauges.openCircuits.scalePeak,1,'all-zero 7d gauge falls back cleanly to scalePeak 1');
assert.equal(ui.prepareHeroPlot(zeroWinGauge,'24h',true).gauges.openCircuits.scalePeak,1,'all-zero 24h gauge falls back cleanly to scalePeak 1');

const partialHeroData=structuredClone(data);
for(const row of chart('directory_rejections',partialHeroData).series[0].history.daily){
 if(row[0]===partialHeroData.end){row[1]=null;row[4]=false;}
}
const hPartial=ui.prepareHeroPlot(partialHeroData,'7d',true);
assert.equal(hPartial.partialRates[6],true);assert.equal(hPartial.partialRates[5],false);assert.equal(hPartial.hasPartialRate,true);
const partialHeroMarkup=application('?view=overview&hero_range=7d',partialHeroData).app.markup();
assert.match(partialHeroMarkup,/partial source coverage/);assert.match(partialHeroMarkup,/Partial rate coverage/);
const partialHeroSvg=element(800),partialHeroAllSvg=element(800);
application('?view=overview',partialHeroData).app.drawHero(partialHeroSvg,'7d','linear',true);
application('?view=overview',partialHeroData).app.drawHero(partialHeroAllSvg,'all','linear',true);
assert.ok(partialHeroSvg.innerHTML.lastIndexOf('hero-rate-bar')<partialHeroSvg.innerHTML.indexOf('hero-rate-partial'),'partial hatch after bars');
assert.ok(partialHeroAllSvg.innerHTML.lastIndexOf('hero-rate-area')<partialHeroAllSvg.innerHTML.indexOf('hero-rate-partial'),'partial hatch after areas');

const allZeroRateData=structuredClone(data);
for(const item of allZeroRateData.charts)if(item.kind.startsWith('counter_'))for(const s of item.series)s.history.daily=s.history.daily.map(row=>[row[0],0,104,1,true]);
const hZeroRates=ui.prepareHeroPlot(allZeroRateData,'7d',true);
assert.deepEqual(hZeroRates.partialRates,Array(7).fill(false));
assert.deepEqual(hZeroRates.totals,Array(7).fill(0));
assert.deepEqual(hZeroRates.missingRates,Array(7).fill(false));

const missingHsPost=structuredClone(allZeroRateData);
chart('directory_requests',missingHsPost).series=chart('directory_requests',missingHsPost).series.filter(s=>s.key!=='hs_post');
chart('directory_requests',missingHsPost).series.find(s=>s.key==='hs_get').history.daily.forEach(row=>{row[1]=10;});
assert.equal(ui.prepareHeroPlot(missingHsPost,'7d',true).partialRates.every(Boolean),true);
assert.equal(ui.prepareHeroPlot(missingHsPost,'7d',true).vectors.find(v=>v.id==='hsdir').partial.every(Boolean),true);
assert.equal(ui.prepareHeroPlot(missingHsPost,'7d',false).partialRates.some(Boolean),false);

const pIns=inspectHero(partialHeroData,'all',['End','Home']);
assert.match(pIns.steps[0].tooltipHtml,/Total active rate<\/span><span>[^<]*\(partial coverage\)/);
assert.match(pIns.steps[1].tooltipHtml,/Unmeasured families:[^<]*Directory floods/);
assert.match(pIns.steps[1].liveText,/Unmeasured families:/);

const noQualData=structuredClone(v6Data);
for(const f of noQualData.families)for(const cid of f.children)for(const s of chart(cid,noQualData).series)s.reference.day={n:19,current_percentile:95,flat_zero:false};
const noQualOverview=application('?view=overview',noQualData).app.markup();
assert.match(noQualOverview,/No qualifying family alerts/);
assert.doesNotMatch(noQualOverview,/All families below 90th pct/);
assert.match(noQualOverview,/rank ≥90 with at least 20 reference days \(elevated, unless the high rule applies\)/);
assert.match(noQualOverview,/Alert strip \(01→06\)[\s\S]*01 Connection floods/);

const heroAllSvg=element(800),narrowAllSvg=element(240);
application('?view=overview',data).app.drawHero(heroAllSvg,'all','linear',true);
assert.match(heroAllSvg.innerHTML,/data-family-lane="directory"/);assert.match(heroAllSvg.innerHTML,/>01–06<\/text>/);
application('?view=overview',data).app.drawHero(narrowAllSvg,'all','linear',true);
const byLane=new Map();
for(const m of narrowAllSvg.innerHTML.matchAll(/<rect class="hero-alert-(?:high|elevated|missing)" data-family-lane="([^"]+)" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"/g)){
 const lane=m[1],x=Number(m[2]),w=Number(m[3]);
 assert.ok(x>=62-0.01&&x+w<=228+0.01,`alert rect [${x}, ${x+w}] within [62, 228]`);
 assert.ok(x>=(byLane.get(lane)??62)-0.01,`lane ${lane} non-overlapping`);
 byLane.set(lane,x+w);
}
assert.ok(byLane.size>0);

const singleDayNarrow=structuredClone(allZeroRateData);
for(const f of singleDayNarrow.families)for(const cid of f.children)for(const s of chart(cid,singleDayNarrow).series){
 s.reference.day={n:100,current_percentile:0,flat_zero:true};
 if(cid==='transport_rejections'){const mid=s.history.daily.length>>1;s.history.daily[mid]=[s.history.daily[mid][0],1e6,104,1,true];}
}
const expectedDayW=166/ui.prepareHeroPlot(singleDayNarrow,'all',true).times.length,narrowSingleSvg=element(240);
application('?view=overview',singleDayNarrow).app.drawHero(narrowSingleSvg,'all','linear',true);
const singleMarks=[...narrowSingleSvg.innerHTML.matchAll(/<rect class="hero-alert-high" data-family-lane="transport" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"/g)];
assert.equal(singleMarks.length,1);
assert.ok(Number(singleMarks[0][2])>0&&Number(singleMarks[0][2])<=expectedDayW+0.02);

// High alert state text, >1e9 symlog ticks, bar height clamp, and queue/h overflow guard.
const highStateInspect=structuredClone(data);
chart('transport_rejections',highStateInspect).series[0].current.day={valid:true,value:100,relays:100,available:1};
chart('transport_rejections',highStateInspect).series[0].reference.day={n:120,current_percentile:100,flat_zero:false};
const stateIns=inspectHero(highStateInspect,'7d',['End']);
assert.match(stateIns.steps[0].tooltipHtml,/Alerts ≥90th:[^<]*\(high, 100th\)/);
assert.match(stateIns.steps[0].liveText,/Alerts ≥90th:.*\(high, 100th\)/);

const largeSymlog=structuredClone(allZeroRateData),hugeSvg=element(800);
chart('transport_rejections',largeSymlog).series[0].history.daily.at(-1)[1]=1e12;
application('',largeSymlog).app.drawHero(hugeSvg,'7d','symlog',true);
assert.match(hugeSvg.innerHTML,/>1e\+12</);
assert.doesNotMatch(hugeSvg.innerHTML,/NaN|Infinity|undefined/);

const barClampData=structuredClone(allZeroRateData),barClampSvg=element(800);
chart('transport_rejections',barClampData).series[0].history.daily.at(-1)[1]=1000;
chart('exit_stream_rejections',barClampData).series[0].history.daily.at(-1)[1]=1e-4;
application('',barClampData).app.drawHero(barClampSvg,'7d','linear',true);
for(const m of barClampSvg.innerHTML.matchAll(/<rect class="hero-rate-bar" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"/g)){
 assert.ok(Number(m[1])>=24&&Number(m[1])+Number(m[2])<=194.01,'bar slice stays strictly inside Tier 1 rate bounds');
}

const qOverflow=structuredClone(allZeroRateData);
chart('queue_limit_circuits',qOverflow).series.find(s=>s.key==='circuit_killed_max_cell').history.daily.at(-1)[1]=1e306;
assert.doesNotMatch(inspectHero(qOverflow,'7d',['End']).steps[0].tooltipHtml,/Unavailable\/h/);
const cssText=fs.readFileSync(path.join(__dirname,'..','site','style.css'),'utf8'),mobileTail=cssText.slice(cssText.lastIndexOf('@media(max-width:600px)'));
for(const [base,mob] of [['.hero-card { margin-bottom: 26px','.hero-card { margin-bottom: 20px'],['.hero-kpi-grid { display:','.hero-kpi-grid { gap: 12px'],['.hero-kpi-value { font-size: 26px','.hero-kpi-value { font-size: clamp'],['.hero-plot-wrap { min-height: 360px','.hero-plot-wrap { min-height: 320px']]){
 assert.ok(cssText.includes(base)&&mobileTail.includes(mob)&&cssText.lastIndexOf(mob)>cssText.indexOf(base),`mobile ${mob} overrides base`);
}
const desktopMedia=cssText.match(/@media\(min-width:1000px\)\s*\{[\s\S]*?\n\}/)?.[0]||'';
assert.match(cssText,/\.hero-panel \{ display: none;/);
assert.match(cssText,/\.hero-window-desktop \{ display: none;/);
assert.match(desktopMedia,/\.hero-panels \{ display: grid;/);
assert.match(desktopMedia,/\.hero-panel \{ display: none;/);
assert.match(desktopMedia,/\.hero-panel\.desktop-left,\.hero-panel\.desktop-right \{ display: block;/);
assert.match(desktopMedia,/\.hero-window-desktop \{ display: flex;/);
assert.match(desktopMedia,/\.hero-window-mobile \{ display: none;/);
{
 const savedSetTimeout=globalThis.setTimeout,queued=[];
 globalThis.setTimeout=(cb,ms)=>{queued.push([cb,ms]);return 0;};
 try{
  const hiddenSkip=application('?view=overview');
  hiddenSkip.app.start();
  const p24=hiddenSkip.browser.document.querySelector('[data-hero-plot="concurrent_dos_24h"]');
  const p7=hiddenSkip.browser.document.querySelector('[data-hero-plot="concurrent_dos_7d"]');
  const pAll=hiddenSkip.browser.document.querySelector('[data-hero-plot="concurrent_dos_all"]');
  p7.innerHTML='stale';
  p7.getBoundingClientRect=()=>({top:0,width:0});
  p24.getBoundingClientRect=()=>({top:0,width:500});
  pAll.getBoundingClientRect=()=>({top:0,width:500});
  hiddenSkip.windowEvents.get('resize')();
  queued.at(-1)[0]();
  assert.equal(p7.innerHTML,'stale','hidden zero-width hero panel skips drawHero');
  assert.match(p24.innerHTML,/<svg /,'visible left hero panel draws normally');
  assert.match(pAll.innerHTML,/<svg /,'visible right hero panel draws normally');
 }finally{
  globalThis.setTimeout=savedSetTimeout;
 }
}
assert.match(application('?view=methods',v6Data).app.markup(),/Last 7 days<\/b> and <b>All time<\/b> side by side on desktop/);
assert.match(application('?view=methods',v6Data).app.markup(),/rank ≥90 with at least 20 reference days \(elevated, unless the high rule applies\)/);
assert.match(application('?view=methods',data).app.markup(),/<b>Last 24 hours<\/b> or <b>Last 7 days<\/b> \(selectable via <b>Window<\/b>\) alongside <b>All time<\/b> on desktop/);
assert.match(application('?view=methods',data).app.markup(),/rank ≥90 with at least 20 reference windows \(elevated, unless the high rule applies\)/);

const copiedQueries=[];
const copyCtrl=application('?view=explorer&chart=transport_rejections');
copyCtrl.browser.navigator.clipboard={async writeText(text){copiedQueries.push(text);}};
copyCtrl.app.start();
(async()=>{
 const scheduled=[];
 const origSetTimeout=globalThis.setTimeout;
 globalThis.setTimeout=(cb,ms)=>{scheduled.push([cb,ms]);return {unref(){}};};
 try{
  const firstBtn={dataset:{copy:'transport_rejections',queryKind:'plot'},textContent:'Copy',isConnected:true,closest:s=>s==='[data-copy]'?firstBtn:null};
  await copyCtrl.events.get('click')({target:firstBtn});
  assert.equal(firstBtn.textContent,'Copied');
  assert.equal(scheduled[0][1],2000);
  scheduled[0][0]();
  assert.equal(firstBtn.textContent,'Copy');
  for(const kind of ['coverage']){
   click(copyCtrl,'[data-copy]',{queryKind:kind});
  }
  await new Promise(resolve=>setImmediate(resolve));
  const trChart=chart('transport_rejections');
  assert.deepEqual(copiedQueries,hasHour?[trChart.query,trChart.quality_query]:[trChart.history_queries.daily.value,trChart.history_queries.daily.eligible]);
  copyCtrl.browser.navigator.clipboard={async writeText(){throw new Error('denied');}};
  const errBtn={dataset:{copy:'transport_rejections',queryKind:'plot'},textContent:'Copy',isConnected:true,closest:s=>s==='[data-copy]'?errBtn:null};
  await copyCtrl.events.get('click')({target:errBtn});
  assert.equal(errBtn.textContent,'Select text to copy');

  // Async clipboard stale-request ignoring + clearTimeout race: only latest request updates/schedules timer.
  const raceTimers=[],clearedTimers=[];
  const origClearTimeout=globalThis.clearTimeout;
  const clipLive={textContent:''};
  const origClipQS=copyCtrl.browser.document.querySelector;
  copyCtrl.browser.document.querySelector=s=>s==='[data-live-status]'?clipLive:origClipQS(s);
  let timerSeq=0;
  globalThis.setTimeout=(cb,ms)=>{const id=++timerSeq;raceTimers.push({id,cb,ms});return id;};
  globalThis.clearTimeout=id=>{if(id!==undefined)clearedTimers.push(id);};
  try{
   let calls=[];
   copyCtrl.browser.navigator.clipboard={
    writeText(){return new Promise((resolve,reject)=>{calls.push({resolve,reject});});}
   };
   const raceBtn={dataset:{copy:'transport_rejections',queryKind:'plot'},textContent:'Copy',isConnected:true,closest:s=>s==='[data-copy]'?raceBtn:null};
   const seq1=copyCtrl.events.get('click')({target:raceBtn});
   calls[0].resolve();await seq1;
   assert.equal(raceTimers.length,1);assert.equal(clearedTimers.length,0);
   const seq2=copyCtrl.events.get('click')({target:raceBtn});
   calls[1].resolve();await seq2;
   assert.deepEqual(clearedTimers,[raceTimers[0].id]);
   assert.equal(raceTimers.length,2);
   raceTimers[1].cb();
   assert.equal(raceBtn.textContent,'Copy');

   // Out-of-order overlap: click 1 fails after click 2 succeeds -> stale click 1 is completely ignored.
   calls=[];raceTimers.length=0;clearedTimers.length=0;clipLive.textContent='';
   const staleBtn={dataset:{copy:'transport_rejections',queryKind:'plot'},textContent:'Copy',isConnected:true,closest:s=>s==='[data-copy]'?staleBtn:null};
   const slowFail=copyCtrl.events.get('click')({target:staleBtn});
   const fastOk=copyCtrl.events.get('click')({target:staleBtn});
   calls[1].resolve();await fastOk;
   assert.equal(staleBtn.textContent,'Copied');
   assert.equal(clipLive.textContent,'Copied PromQL query to clipboard.');
   assert.equal(raceTimers.length,1);
   calls[0].reject(new Error('late stale fail'));await slowFail;
   assert.equal(staleBtn.textContent,'Copied');
   assert.equal(clipLive.textContent,'Copied PromQL query to clipboard.');
   assert.equal(raceTimers.length,1);
   assert.equal(clearedTimers.length,0);
   raceTimers[0].cb();
   assert.equal(staleBtn.textContent,'Copy');
  }finally{
   globalThis.clearTimeout=origClearTimeout;
   copyCtrl.browser.document.querySelector=origClipQS;
  }

  // Negative axisNumber symmetry and -0 UI suppression.
  for(const [val,expected] of [[-1.234,'-1.23'],[-555,'-555'],[-1500,'-1,500'],[-1500000,'-1.5M'],[-0.025,'-0.025'],[-1e-5,'-1e-5'],[-0,'0']]){
   assert.equal(ui.axisNumber(val),expected,`axisNumber(${val})`);
  }
  assert.equal(ui.number(-0),'0');
  const negZeroData=structuredClone(data);
  negZeroData.dircache_cohort={configured_nonexit_zero:-0,observed_controlport_zero:-0,file_config_fallback_zero:-0};
  for(const cid of ['open_circuits','directory_waiting']){
   const s=chart(cid,negZeroData).series[0];
   s.history.daily.at(-1)[1]=-0;s.current.day={value:-0,relays:8,expected_relays:8,coverage:1,valid:true};
   s.peaks.day={fleet_peak:100,fleet_peak_at:negZeroData.end,fleet_peak_relays:8,current_is_record:false};
   if(s.current.hour)s.current.hour={value:-0,relays:8,expected_relays:8,coverage:1,valid:true};
   if(s.peaks.hour)s.peaks.hour={fleet_peak:100,fleet_peak_at:negZeroData.end,fleet_peak_relays:8,current_is_record:false};
  }
  const nzSeries=chart('transport_rejections',negZeroData).series[0];
  nzSeries.current.day={value:-0,relays:8,expected_relays:8,coverage:1,valid:true};
  nzSeries.reference.day={n:-0,current_percentile:null,flat_zero:false,mean_per_relay:0,start:negZeroData.reference_window.start,end:negZeroData.reference_window.end};
  if(nzSeries.current.hour)nzSeries.current.hour={value:-0,relays:8,expected_relays:8,coverage:1,valid:true};
  if(nzSeries.reference.hour)nzSeries.reference.hour={n:-0,current_percentile:null,flat_zero:false,mean_per_relay:0,start:negZeroData.reference_window.start,end:negZeroData.reference_window.end};
  nzSeries.historical_averages.seven_days.days=-0;
  nzSeries.relay_counts.day={total:-0,guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,role_bounds:{guard:[-0,-0],middle_only:[0,0],exit:[0,0],hsdir:[0,0],v2dir:[0,0]},measured_windows:-0,classified_windows:0,relay_windows:0,classified_relay_windows:0,coverage:-0};
  if(nzSeries.relay_counts.hour)nzSeries.relay_counts.hour={total:-0,guard:null,middle_only:null,exit:null,hsdir:null,v2dir:null,role_bounds:{guard:[-0,-0],middle_only:[0,0],exit:[0,0],hsdir:[0,0],v2dir:[0,0]},measured_windows:-0,classified_windows:0,relay_windows:0,classified_relay_windows:0,coverage:-0};
  nzSeries.relay_counts.seven_days.coverage=-0;
  for(const viewSearch of ['?view=overview','?view=explorer&chart=transport_rejections','?view=methods']){
   const html=application(viewSearch,negZeroData).app.markup();
   assert.doesNotMatch(html,/>-0(?:<|%| total| \/| non-exit)/,`no -0 UI leak in ${viewSearch}`);
  }
  for(const [patch,rx] of [
   [tr=>{tr.periods.find(p=>p.window.unit==='days').window.comparison_lag_days=-0;},/time_ranges days window/],
   [tr=>{tr.periods.at(-1).window.comparison_lag_days=-0;},/time_ranges all_time window/],
   [tr=>{tr.periods.find(p=>p.window.unit==='days').minimum_coverage=-0;},/time_ranges minimum_coverage/],
   [tr=>{tr.periods.find(p=>p.window.unit==='days').window.unit=['days'];},/Unsupported time_ranges window unit/],
   [tr=>{tr.periods.find(p=>p.window.unit==='months').window.unit=['months'];},/Unsupported time_ranges window unit/],
   [tr=>{tr.periods.find(p=>p.window.unit==='days').window.unit=123;},/Unsupported time_ranges window unit/],
  ]){
   const badTr=structuredClone(data.time_ranges);patch(badTr);
   assert.throws(()=>ui.validateTimeRanges(badTr),rx);
  }

  // replaceRelease preserves focus on [data-query-kind="coverage"] and [data-methods].
  const covCtrl=application('?view=explorer&chart=transport_rejections',data,'1',releaseContext(data,'cov-old',()=>data.end));
  covCtrl.app.start();
  const covBtn=covCtrl.browser.document.querySelectorAll('[data-copy]').find(el=>el.dataset.queryKind==='coverage');
  assert.ok(covBtn);covBtn.focus({preventScroll:true});
  const covFresh=structuredClone(data);
  covCtrl.app.replaceRelease(covFresh,releaseContext(covFresh,'cov-new',()=>covFresh.end));
  assert.equal(covCtrl.browser.document.activeElement,covCtrl.browser.document.querySelectorAll('[data-copy]').find(el=>el.dataset.queryKind==='coverage'));
  const methCtrl=application('?view=overview',data,'1',releaseContext(data,'meth-old',()=>data.end));
  methCtrl.app.start();
  methCtrl.browser.document.querySelector('[data-methods]').focus({preventScroll:true});
  const methFresh=structuredClone(data);
  methCtrl.app.replaceRelease(methFresh,releaseContext(methFresh,'meth-new',()=>methFresh.end));
  assert.equal(methCtrl.browser.document.activeElement,methCtrl.browser.document.querySelector('[data-methods]'));

  // Layout normalization + no-op skip + stale/null target click guards.
  const guardCtrl=application('?view=overview');guardCtrl.app.start();
  const initialState=guardCtrl.app.getState();
  const forgedLayout={dataset:{layout:'bad" onclick="x'},closest:s=>s==='[data-layout]'?forgedLayout:null};
  guardCtrl.events.get('click')({target:forgedLayout,button:0,preventDefault(){}});
  assert.equal(guardCtrl.app.getState().layout,'full');
  const genBefore=guardCtrl.page.generation;
  click(guardCtrl,'[data-layout]',{layout:'full'});
  assert.equal(guardCtrl.page.generation,genBefore,'clicking already-active layout does not redraw');
  for(const [attr,ds] of [['[data-explore]',{explore:'missing',chart:'missing',range:'bad'}],['[data-child]',{child:'missing'}],['[data-family]',{family:'missing'}],['[data-view]',{view:'bogus'}]]){
   const node={dataset:ds,closest:s=>s===attr?node:null};
   guardCtrl.events.get('click')({target:node,button:0,preventDefault(){}});
  }
  assert.deepEqual(guardCtrl.app.getState(),initialState);

  // Empty <path> omission in draw().
  const emptyPathData=structuredClone(data),epChart=chart('transport_rejections',emptyPathData);
  const cutoff7d=emptyPathData.end-7*86400;
  for(const row of epChart.series[0].history.daily)if(row[0]>cutoff7d)row[4]=false;
  const epPlot=element(800);
  application('?view=explorer&chart=transport_rejections&range=7d',emptyPathData).app.draw(epPlot,epChart,'7d');
  assert.equal((epPlot.innerHTML.match(/class="measurement-series"/g)||[]).length,1);

  // Disqualified-day tooltip suppression & resize activeIndex preservation & touch tap & Escape preventDefault.
  const disqData=structuredClone(data),dc=chart('transport_rejections',disqData);
  for(const s of dc.series){
   const lenD=s.history.daily.length;
   if(lenD>=2){
    const dRow=s.history.daily[lenD-2];
    dRow[1]=12;dRow[2]=104;dRow[3]=0.5;dRow[4]=true;
    s.history.daily[lenD-1][4]=true;s.history.daily[lenD-1][3]=1;
   }
   s.reference.day={n:100,p10_per_relay:0.01,p50_per_relay:0.05,p90_per_relay:0.2,mean_per_relay:0.05,current_percentile:50,flat_zero:false,start:disqData.reference_window.start,end:disqData.reference_window.end};
  }
  const hCtrl=application('?view=explorer&chart=transport_rejections&range=7d&band=1',disqData);
  const hLive={textContent:''};
  const makePlotMock=(attrs,isHero)=>{
   let html='';
   const el={cur:null,tip:null,dataset:attrs,
    get innerHTML(){return html;},
    set innerHTML(v){html=v;el.cur=null;el.tip=null;},
    getBoundingClientRect:()=>({width:800,height:isHero?400:300}),
    closest:s=>(isHero?s==='[data-hero-plot]':s==='[data-chart-plot]')?el:null,
    querySelector:s=>s==='svg'?svg:s==='.plot-cursor'?el.cur:s==='.chart-tooltip'?el.tip:null,
    appendChild:n=>{el.tip=n;n.owner=el;},
    contains:node=>node===el,setAttribute(){},hasAttribute:()=>false};
   const svg={getBoundingClientRect:()=>({left:0,width:800,height:300}),querySelector:s=>s==='.plot-cursor'?el.cur:null,appendChild:n=>{el.cur=n;n.owner=el;}};
   return el;
  };
  const heroResizeEl=makePlotMock({heroPlotRange:'7d'},true);
  const chartResizeEl=makePlotMock({chartPlot:'transport_rejections',range:'7d'},false);
  const origQS=hCtrl.browser.document.querySelector,origQSA=hCtrl.browser.document.querySelectorAll;
  hCtrl.browser.document.querySelector=s=>s==='[data-live-status]'?hLive:origQS(s);
  hCtrl.browser.document.querySelectorAll=s=>s==='[data-hero-plot]'?[heroResizeEl]:s==='[data-chart-plot]'?[chartResizeEl]:origQSA(s);
  hCtrl.browser.document.createElement=tag=>({tag,className:'',innerHTML:'',style:{},owner:null,remove(){if(this.owner)this.owner.tip=null;}});
  hCtrl.browser.document.createElementNS=(ns,tag)=>({ns,tag,attrs:{},owner:null,setAttribute(k,v){this.attrs[k]=v;},remove(){if(this.owner)this.owner.cur=null;}});
  const rTimers=[];
  const origClearResize=globalThis.clearTimeout;
  globalThis.setTimeout=(cb,ms)=>{const id=rTimers.length+1;rTimers.push({id,cb,ms});return id;};
  globalThis.clearTimeout=()=>{};
  try{
   hCtrl.app.start();
   const kd=hCtrl.events.get('keydown');
   kd({key:'ArrowLeft',target:chartResizeEl,preventDefault(){}});
   assert.match(chartResizeEl.tip.innerHTML,/Reporting relays:/);assert.match(chartResizeEl.tip.innerHTML,/10th–90th:/);
   kd({key:'ArrowLeft',target:chartResizeEl,preventDefault(){}});
   assert.match(chartResizeEl.tip.innerHTML,/Missing/);
   assert.doesNotMatch(chartResizeEl.tip.innerHTML,/Reporting relays:/);
   assert.doesNotMatch(chartResizeEl.tip.innerHTML,/10th–90th:/);
   kd({key:'ArrowLeft',target:heroResizeEl,preventDefault(){}});
   const savedChartTip=chartResizeEl.tip.innerHTML,savedHeroTip=heroResizeEl.tip.innerHTML;
   hLive.textContent='';
   hCtrl.windowEvents.get('resize')();
   assert.ok(rTimers.length>0);
   rTimers.at(-1).cb();
   assert.ok(chartResizeEl.cur&&chartResizeEl.tip&&chartResizeEl.tip.innerHTML===savedChartTip,'chart activeIndex preserved on resize');
   assert.ok(heroResizeEl.cur&&heroResizeEl.tip&&heroResizeEl.tip.innerHTML===savedHeroTip,'hero activeIndex preserved on resize');
   assert.equal(hLive.textContent,'','resize restore does not re-announce live status');

   // Touch tap inspection stays visible across pointerout (pointerType='touch') until tap outside or Escape.
   const pm=hCtrl.events.get('pointermove'),pd=hCtrl.events.get('pointerdown'),po=hCtrl.events.get('pointerout');
   kd({key:'Escape',target:chartResizeEl,preventDefault(){}});
   assert.equal(chartResizeEl.tip,null);
   pd({target:chartResizeEl,clientX:400,pointerType:'touch'});
   assert.ok(chartResizeEl.cur&&chartResizeEl.tip,'touch pointerdown inspects interval');
   po({target:chartResizeEl,pointerType:'touch',relatedTarget:null});
   assert.ok(chartResizeEl.cur&&chartResizeEl.tip,'touch pointerout does not wipe tooltip');
   const outside={closest:()=>null};
   pd({target:outside,clientX:10,pointerType:'touch'});
   assert.equal(chartResizeEl.tip,null,'tapping outside dismisses touch tooltip');
   pm({target:chartResizeEl,clientX:400,pointerType:'mouse'});
   assert.ok(chartResizeEl.tip);
   po({target:chartResizeEl,pointerType:'mouse',relatedTarget:null});
   assert.equal(chartResizeEl.tip,null,'desktop mouse pointerout still clears tooltip');

   // Escape calls e.preventDefault?.() for active chart inspection and open mobile nav menu.
   pm({target:chartResizeEl,clientX:400,pointerType:'mouse'});
   let chartPrevented=false;
   kd({key:'Escape',target:chartResizeEl,preventDefault(){chartPrevented=true;}});
   assert.equal(chartPrevented,true);assert.equal(chartResizeEl.tip,null);
   const navClasses=new Set(['show']),navLinks={classList:{contains:k=>navClasses.has(k),remove:k=>navClasses.delete(k),toggle:k=>navClasses.has(k)?navClasses.delete(k):navClasses.add(k)}};
   let toggleFocused=false;const navToggle={setAttribute(){},closest:s=>s==='.nav-toggle'?navToggle:null,focus(){toggleFocused=true;}};
   hCtrl.browser.document.querySelector=s=>({'.nav-links':navLinks,'.nav-toggle':navToggle,'[data-live-status]':hLive}[s]||origQS(s));
   let navPrevented=false;
   kd({key:'Escape',target:navToggle,preventDefault(){navPrevented=true;}});
   assert.equal(navPrevented,true);assert.equal(navClasses.has('show'),false);assert.equal(toggleFocused,true);
   navClasses.add('show');
   kd({key:'Escape',target:navToggle});
   assert.equal(navClasses.has('show'),false);

   // Non-finite clientX / index guards on chart and hero plots.
   for(const targetEl of [chartResizeEl,heroResizeEl]){
    kd({key:'Escape',target:targetEl});
    pm({target:targetEl,clientX:undefined,pointerType:'mouse'});
    pm({target:targetEl,clientX:NaN,pointerType:'mouse'});
   }
   assert.equal(chartResizeEl.cur,null);assert.equal(chartResizeEl.tip,null);
   assert.equal(heroResizeEl.cur,null);assert.equal(heroResizeEl.tip,null);
   hCtrl.browser.document.querySelector=origQS;
   hCtrl.browser.document.querySelectorAll=origQSA;
  }finally{
   globalThis.setTimeout=origSetTimeout;
   globalThis.clearTimeout=origClearResize;
  }

  // Mobile (<450px) tooltip uses 8px horizontal insets on left/right halves.
  const mobCtrl=application('?view=explorer&chart=transport_rejections&range=7d');
  let mobCur=null,mobTip=null;
  const mobSvg={getBoundingClientRect:()=>({left:0,width:320,height:300}),querySelector:s=>s==='.plot-cursor'?mobCur:null,appendChild:n=>{mobCur=n;}};
  const mobPlot={dataset:{chartPlot:'transport_rejections',range:'7d'},innerHTML:'',getBoundingClientRect:()=>({width:320,height:300}),
   closest:s=>s==='[data-chart-plot]'?mobPlot:null,
   querySelector:s=>s==='svg'?mobSvg:s==='.plot-cursor'?mobCur:s==='.chart-tooltip'?mobTip:null,
   appendChild:n=>{mobTip=n;},setAttribute(){},hasAttribute:()=>false};
  mobCtrl.browser.document.createElement=tag=>({tag,className:'',innerHTML:'',style:{},remove(){if(this===mobTip)mobTip=null;}});
  mobCtrl.browser.document.createElementNS=(ns,tag)=>({ns,tag,attrs:{},setAttribute(k,v){this.attrs[k]=v;},remove(){if(this===mobCur)mobCur=null;}});
  mobCtrl.app.start();
  mobCtrl.app.draw(mobPlot,chart('transport_rejections'),'7d',320);
  mobCtrl.events.get('pointermove')({target:mobPlot,clientX:290,pointerType:'mouse'});
  assert.equal(mobTip.style.left,'8px');assert.equal(mobTip.style.right,'auto');
  mobCtrl.events.get('pointermove')({target:mobPlot,clientX:75,pointerType:'mouse'});
  assert.equal(mobTip.style.right,'8px');assert.equal(mobTip.style.left,'auto');

  // replaceRelease disabled control & unmarked #app element focus fallback to view heading.
  const bandData=structuredClone(data);
  chart('transport_rejections',bandData).series.forEach(s=>{
   const refVal={n:100,p10_per_relay:0.01,p50_per_relay:0.05,p90_per_relay:0.2,mean_per_relay:0.05,current_percentile:50,flat_zero:false,start:bandData.reference_window.start,end:bandData.reference_window.end};
   s.reference.day=structuredClone(refVal);
   if(s.reference.hour)s.reference.hour=structuredClone(refVal);
  });
  const disRelCtrl=application('?view=explorer&chart=transport_rejections',bandData,'1',releaseContext(bandData,'band-on',()=>bandData.end));
  disRelCtrl.app.start();
  disRelCtrl.browser.document.querySelector('[data-band-toggle]').focus({preventScroll:true});
  assert.equal(disRelCtrl.browser.document.activeElement.getAttribute('data-band-toggle'),'');
  const noBandData=structuredClone(bandData);
  chart('transport_rejections',noBandData).series.forEach(s=>{s.reference.day.n=1;if(s.reference.hour)s.reference.hour.n=1;});
  disRelCtrl.app.replaceRelease(noBandData,releaseContext(noBandData,'band-off',()=>noBandData.end));
  assert.equal(disRelCtrl.browser.document.activeElement,disRelCtrl.browser.document.querySelector('[data-view-heading]'));

  const fixCtrl=application('?view=fixes',data,'1',releaseContext(data,'fix-old',()=>data.end));
  fixCtrl.app.start();
  const extLink=fixCtrl.browser.document.querySelectorAll('a').find(a=>!a.hasAttribute('data-view'));
  assert.ok(extLink);extLink.focus({preventScroll:true});
  assert.equal(fixCtrl.browser.document.activeElement,extLink);
  fixCtrl.app.replaceRelease(structuredClone(data),releaseContext(data,'fix-new',()=>data.end));
  assert.equal(fixCtrl.browser.document.activeElement,fixCtrl.browser.document.querySelector('[data-view-heading]'));

  // render() resets touchPlot so later outside touch taps cannot touch detached plots.
  const tpCtrl=application('?view=explorer&chart=transport_rejections&range=7d');
  let tpCur=null,tpTip=null,detachedWipes=0;
  const tpSvg={getBoundingClientRect:()=>({left:0,width:800,height:300}),querySelector:s=>s==='.plot-cursor'?tpCur:null,appendChild:n=>{tpCur=n;}};
  const tpPlot={dataset:{chartPlot:'transport_rejections',range:'7d'},innerHTML:'',getBoundingClientRect:()=>({width:800,height:300}),
   closest:s=>s==='[data-chart-plot]'?tpPlot:null,
   querySelector:s=>s==='svg'?tpSvg:s==='.plot-cursor'?tpCur:s==='.chart-tooltip'?tpTip:null,
   appendChild:n=>{tpTip=n;},setAttribute(){},hasAttribute:()=>false};
  tpCtrl.browser.document.createElement=tag=>({tag,className:'',innerHTML:'',style:{},remove(){if(this===tpTip){detachedWipes++;tpTip=null;}}});
  tpCtrl.browser.document.createElementNS=(ns,tag)=>({ns,tag,attrs:{},setAttribute(k,v){this.attrs[k]=v;},remove(){if(this===tpCur){detachedWipes++;tpCur=null;}}});
  tpCtrl.app.start();
  tpCtrl.app.draw(tpPlot,chart('transport_rejections'),'7d');
  tpCtrl.events.get('pointerdown')({target:tpPlot,clientX:400,pointerType:'touch'});
  assert.ok(tpCur&&tpTip);
  click(tpCtrl,'[data-all-range]',{allRange:'1mo'});
  detachedWipes=0;
  tpCtrl.events.get('pointerdown')({target:{closest:()=>null},clientX:10,pointerType:'touch'});
  assert.equal(detachedWipes,0);

  // cachedPlot distinguishes single series literally named '*' from multi-series cache key '*'.
  const starData=structuredClone(data),sc=chart('transport_rejections',starData);
  sc.series[0].key='*';sc.display_keys=['*',sc.series[1].key];sc.primary_series='*';
  for(const s of sc.series)s.reference.day={n:100,p10_per_relay:0.01,p50_per_relay:0.05,p90_per_relay:0.2,mean_per_relay:0.05,current_percentile:50,flat_zero:false,start:starData.reference_window.start,end:starData.reference_window.end};
  const starCtrl=application('?view=explorer&chart=transport_rejections&range=7d',starData);
  starCtrl.app.start();
  const starMulti=element(800),starBand=element(800);
  starCtrl.app.draw(starMulti,sc,'7d');
  assert.equal((starMulti.innerHTML.match(/class="measurement-series"/g)||[]).length,2);
  click(starCtrl,'[data-band-toggle]');
  starCtrl.app.draw(starBand,sc,'7d');
  assert.equal((starBand.innerHTML.match(/class="measurement-series"/g)||[]).length,1);

  // Oxford commas in methods() lag sentence, validTime in .average-dates, and xAxisTicks invalid bounds.
  const lag3Data=structuredClone(v6Data);
  const p7=structuredClone(v6Data.time_ranges.periods[0]);
  const p14={...structuredClone(p7),id:'14d',summary_key:'fourteen_days',export_suffix:'14d',label:'14 days',average_label:'Last 14 days average',plot_label:'Last 14 days',window:{unit:'days',count:14,comparison_lag_days:1}};
  const p28={...structuredClone(p7),id:'28d',summary_key:'twenty_eight_days',export_suffix:'28d',label:'28 days',average_label:'Last 28 days average',plot_label:'Last 28 days',window:{unit:'days',count:28,comparison_lag_days:1}};
  lag3Data.time_ranges.periods=[p7,p14,p28,...v6Data.time_ranges.periods.slice(1)];
  for(const c of lag3Data.charts)for(const s of c.series){
   s.historical_averages.fourteen_days={...structuredClone(s.historical_averages.seven_days)};
   s.historical_averages.twenty_eight_days={...structuredClone(s.historical_averages.seven_days)};
   if(s.relay_counts){s.relay_counts.fourteen_days={...structuredClone(s.relay_counts.seven_days)};s.relay_counts.twenty_eight_days={...structuredClone(s.relay_counts.seven_days)};}
  }
  const oxMethods=application('?view=methods',lag3Data).app.markup();
  assert.match(oxMethods,/7 days, 14 days, and 28 days/);

  const badAvgDateData=structuredClone(data);
  chart('transport_rejections',badAvgDateData).series[0].historical_averages.seven_days.start='not-a-timestamp';
  chart('transport_rejections',badAvgDateData).series[0].historical_averages.one_month.end=null;
  const badAvgHtml=application('?view=explorer&chart=transport_rejections',badAvgDateData).app.markup();
  assert.doesNotMatch(badAvgHtml,/average-dates[^<]*Unavailable/);

  assert.deepEqual(ui.xAxisTicks(100,100,800,62,12),[]);
  assert.deepEqual(ui.xAxisTicks(200,100,800,62,12),[]);
  assert.deepEqual(ui.xAxisTicks(NaN,200,800,62,12),[]);
  assert.deepEqual(ui.xAxisTicks(1e15,2e15,800,62,12),[]);

  // replaceRelease restricts focus restoration to #app and preserves occurrence order across identical links.
  const ovCtrl=application('?view=explorer&chart=transport_rejections',data,'1',releaseContext(data,'ov-old',()=>data.end));
  const staticNavOverview={tagName:'A',dataset:{view:'overview'},setAttribute(){},hasAttribute:n=>n==='data-view',getAttribute:n=>n==='data-view'?'overview':null,focus(){}};
  const origOvQSA=ovCtrl.browser.document.querySelectorAll;
  ovCtrl.browser.document.querySelectorAll=s=>s==='[data-view]'?[staticNavOverview,...origOvQSA(s)]:origOvQSA(s);
  const inAppOverviews=()=>origOvQSA('[data-view]').filter(n=>n.getAttribute('data-view')==='overview');
  ovCtrl.app.start();
  const initialOvLinks=inAppOverviews();
  assert.equal(initialOvLinks.length,2,'explorer renders subnav and breadcrumb overview links');
  assert.equal(ovCtrl.page.contains(staticNavOverview),false);
  initialOvLinks[1].focus({preventScroll:true});
  ovCtrl.app.replaceRelease(structuredClone(data),releaseContext(data,'ov-new-1',()=>data.end));
  const afterBreadcrumbLinks=inAppOverviews();
  assert.equal(ovCtrl.browser.document.activeElement,afterBreadcrumbLinks[1]);
  assert.equal(ovCtrl.page.contains(ovCtrl.browser.document.activeElement),true);
  afterBreadcrumbLinks[0].focus({preventScroll:true});
  ovCtrl.app.replaceRelease(structuredClone(data),releaseContext(data,'ov-new-2',()=>data.end));
  assert.equal(ovCtrl.browser.document.activeElement,inAppOverviews()[0]);

  // Custom period id='band' does not collide with bandReference cache key 'band:<r>:<key>'.
  const bandPeriodData=structuredClone(v6Data);
  const p7d=structuredClone(v6Data.time_ranges.periods[0]),p1m=structuredClone(v6Data.time_ranges.periods[1]),pAll=structuredClone(v6Data.time_ranges.periods.at(-1));
  bandPeriodData.time_ranges={
   version:1,default_range:'1',legacy_aliases:{},card_slots:{fallback_dynamic:'1',fixed:['band','all']},
   periods:[
    {...p7d,id:'1',export_suffix:'1p'},
    {...p1m,id:'band',export_suffix:'band'},
    pAll,
   ],
  };
  const bc=chart('transport_rejections',bandPeriodData);
  bc.series.forEach(s=>{
   s.reference.day={n:100,p10_per_relay:0.01,p50_per_relay:0.05,p90_per_relay:0.2,mean_per_relay:0.05,current_percentile:50,flat_zero:false,start:bandPeriodData.reference_window.start,end:bandPeriodData.reference_window.end};
  });
  const bandCollisionCtrl=application('?view=explorer&chart=transport_rejections&range=1&band=1',bandPeriodData);
  bandCollisionCtrl.app.start();
  click(bandCollisionCtrl,'[data-all-range]',{allRange:'band'});
  assert.equal(bandCollisionCtrl.app.getState().range,'band');
  assert.equal(bandCollisionCtrl.app.getState().bandEnabled,true);
  const bandCollisionSvg=element(800);
  bandCollisionCtrl.app.draw(bandCollisionSvg,bc,'band');
  assert.match(bandCollisionSvg.innerHTML,/class="measurement-series"/);
  assert.match(bandCollisionSvg.innerHTML,/class="reference-band"/);

  // .hero-controls keeps margin-bottom spacing when wrapping at <=1200px widths.
  const latestCss=fs.readFileSync(path.join(__dirname,'..','site','style.css'),'utf8');
  assert.match(latestCss,/\.hero-controls\s*\{[^}]*margin-bottom:\s*14px;/);

  // Schema 7 (24h x 1h) coverage: hero 24h, metric 24h plots, 24h band, 4-card statPair, Explorer & live fallback.
  const v7NonMidnight=structuredClone(v7Data);
  const shiftSec=13*3600;
  v7NonMidnight.end+=shiftSec;v7NonMidnight.start+=shiftSec;
  for(const c of v7NonMidnight.charts){
   c.end+=shiftSec;c.start+=shiftSec;
   for(const s of c.series){
    s.values=s.values.map(([t,v])=>[t+shiftSec,v]);
    s.hourly_relays=s.hourly_relays.map(([t,v])=>[t+shiftSec,v]);
    s.hourly_expected_relays=s.hourly_expected_relays.map(([t,v])=>[t+shiftSec,v]);
    s.current.hour.value=s.values.at(-1)[1];
    s.current.hour.relays=s.hourly_relays.at(-1)[1];
    s.current.hour.expected_relays=s.hourly_expected_relays.at(-1)[1];
    s.current.hour.coverage=s.current.hour.relays!=null?s.current.hour.relays/s.current.hour.expected_relays:null;
    s.current.hour.valid=s.current.hour.value!==null&&s.current.hour.relays>=8&&s.current.hour.coverage>=0.9;
   }
  }
  const v7Overview=application('?view=overview',v7NonMidnight);
  v7Overview.app.start();
  assert.equal(v7Overview.app.getState().heroRange,'24h');
  assert.equal(v7Overview.app.getState().range,'24h');
  assert.equal(v7Overview.browser.document.querySelectorAll('[data-hero-range]').length,5);
  assert.equal(v7Overview.browser.document.querySelectorAll('[data-hero-panel]').length,3);
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="24h"]').getAttribute('class'),'hero-panel active desktop-left');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="7d"]').getAttribute('class'),'hero-panel');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="all"]').getAttribute('class'),'hero-panel desktop-right');
  assert.equal(new URLSearchParams(v7Overview.app.url('overview').slice(1)).has('hero_range'),false,'default 24h hero_range is omitted from URL');
  assert.equal(ui.parseLocation(v6Data,'?hero_range=24h').heroRange,'7d','legacy bundle without hourly data clamps hero_range=24h to 7d');
  click(v7Overview,'[data-hero-range]',{heroRange:'7d'});
  assert.equal(new URLSearchParams(v7Overview.app.url('overview').slice(1)).get('hero_range'),'7d');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="24h"]').getAttribute('class'),'hero-panel');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="7d"]').getAttribute('class'),'hero-panel active desktop-left');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="all"]').getAttribute('class'),'hero-panel desktop-right');
  click(v7Overview,'[data-hero-range]',{heroRange:'all'});
  const desktopWinHtml=v7Overview.page.innerHTML.match(/hero-window-desktop"><span>Window<\/span><div class="range-control"[^>]*>(.*?)<\/div><\/div>/)?.[1]||'';
  const mobileWinHtml=v7Overview.page.innerHTML.match(/hero-window-mobile"><span>Window<\/span><div class="range-control"[^>]*>(.*?)<\/div><\/div>/)?.[1]||'';
  assert.equal([...desktopWinHtml.matchAll(/<button /g)].length,2);
  assert.equal([...mobileWinHtml.matchAll(/<button /g)].length,3);
  assert.match(desktopWinHtml,/data-hero-range="24h" class="active" aria-pressed="true"/);
  assert.match(desktopWinHtml,/data-hero-range="7d" class="" aria-pressed="false"/);
  assert.match(mobileWinHtml,/data-hero-range="all" class="active" aria-pressed="true"/);
  assert.match(mobileWinHtml,/data-hero-range="24h" class="" aria-pressed="false"/);
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="24h"]').getAttribute('class'),'hero-panel desktop-left');
  assert.equal(v7Overview.browser.document.querySelector('[data-hero-panel="all"]').getAttribute('class'),'hero-panel active desktop-right');
  click(v7Overview,'[data-hero-range]',{heroRange:'24h'});
  // When hidden control precedes visible control, redrawControl restores focus to the visible one (offsetParent !== null).
  const visCtrl=application('?view=overview',v7NonMidnight);
  const origVisQSA=visCtrl.browser.document.querySelectorAll;
  visCtrl.browser.document.querySelectorAll=s=>{
   const out=origVisQSA(s);
   if(s==='[data-hero-range="7d"]'&&out.length===2){out[0].offsetParent=null;out[1].offsetParent={};}
   return out;
  };
  visCtrl.app.start();
  click(visCtrl,'[data-hero-range]',{heroRange:'7d'});
  assert.equal(visCtrl.browser.document.activeElement,origVisQSA('[data-hero-range="7d"]')[1],'focus skips hidden button (offsetParent===null)');
  assert.match(v7Overview.page.innerHTML,/Hourly UTC publication planned/);
  assert.match(v7Overview.page.innerHTML,/Latest 1h mitigated rate/);
  assert.match(application('?view=overview&hero_hsdir=1',v7NonMidnight).app.markup(),/Latest 1h mitigated &amp; flood rate/);
  assert.match(v7Overview.page.innerHTML,/Concurrent family alerts \(1h\)/);
  assert.match(v7Overview.page.innerHTML,/Open circuits \(1h mean\)/);
  assert.match(v7Overview.page.innerHTML,/Stalled BEGIN_DIR \(1h mean\)/);
  assert.match(v7Overview.page.innerHTML,/sampled 1-hour|20 sampled reference hours/);

  // Null 24h hero plot must not fall back to daily h7/hAll under Latest 1h KPI labels.
  const emptyHourlyOnly=structuredClone(v7NonMidnight);
  for(const c of emptyHourlyOnly.charts)for(const s of c.series){
   s.values=s.values.map(([t])=>[t,null]);
   s.hourly_relays=s.hourly_relays.map(([t])=>[t,0]);
   s.current.hour={value:null,relays:0,expected_relays:Math.max(8,c.relays),coverage:0,valid:false};
  }
  const empty24Kpi=application('?view=overview',emptyHourlyOnly).app.markup();
  assert.match(empty24Kpi,/Latest 1h mitigated rate<\/div><div class="hero-kpi-value unknown">Unavailable/);
  assert.match(empty24Kpi,/No measured family series/);

  // Non-midnight v7 snapshot: 7d hero end cell ranks 00:00 UTC daily bar via baseline rather than rolling reference.day.
  const nonMidHeroAlert=structuredClone(v7NonMidnight);
  const trNm=chart('transport_rejections',nonMidHeroAlert).series[0];
  const nmDayEnd=Math.floor(nonMidHeroAlert.end/86400)*86400;
  trNm.history.daily=Array.from({length:25},(_,k)=>{
   const t=nonMidHeroAlert.reference_window.start+(k+1)*86400;
   return [t,k===24?10:100,100,1,true];
  });
  trNm.history.daily.push([nmDayEnd,10,100,1,true]);
  trNm.reference.day={n:100,current_percentile:99,flat_zero:false,start:nonMidHeroAlert.reference_window.start,end:nonMidHeroAlert.reference_window.end};
  const nonMidHero7=ui.prepareHeroPlot(nonMidHeroAlert,'7d',true);
  assert.equal(nonMidHero7.families.find(f=>f.id==='transport').rows[6].state,'neutral','non-midnight 7d hero ignores current24 reference.day');
  const midSameAlert=structuredClone(nonMidHeroAlert);
  midSameAlert.end=nmDayEnd;midSameAlert.start=nmDayEnd-86400;
  assert.equal(ui.prepareHeroPlot(midSameAlert,'7d',true).families.find(f=>f.id==='transport').rows[6].state,'high','midnight 7d hero still uses matching reference.day');

  // 24h prepareHeroPlot: non-midnight 24h window, sampled_hour baseline ranking (t<end) vs reference.hour (t===end), low-coverage rejection, gauge hour peak.
  const h24Custom=structuredClone(v7NonMidnight);
  const trPrimary24=chart('transport_rejections',h24Custom).series[0];
  trPrimary24.values[22][1]=100;trPrimary24.hourly_relays[22][1]=100;trPrimary24.hourly_expected_relays[22][1]=100;
  trPrimary24.history.sampled_hour=Array.from({length:25},(_,k)=>[h24Custom.reference_window.start+(k+1)*21600,k>=23?150:10,100,1,true]);
  chart('transport_rejections',h24Custom).series[1].hourly_relays[21][1]=80;
  chart('transport_rejections',h24Custom).series[1].hourly_expected_relays[21][1]=100; // 0.8 < 0.9 coverage -> omitted
  chart('open_circuits',h24Custom).series[0].peaks.day={fleet_peak:9000000,fleet_peak_at:h24Custom.end-86400,fleet_peak_relays:100,current_is_record:false};
  chart('open_circuits',h24Custom).series[0].peaks.hour={fleet_peak:5000000,fleet_peak_at:h24Custom.end-3600,fleet_peak_relays:100,current_is_record:false};
  const prep24=ui.prepareHeroPlot(h24Custom,'24h',true);
  assert.equal(prep24.window,3600);
  assert.equal(prep24.times.length,24);
  assert.equal(prep24.end,h24Custom.end);
  assert.equal(prep24.end-prep24.start,86400);
  assert.equal(prep24.gauges.openCircuits.rawPeak,5000000);
  assert.equal(prep24.families.find(f=>f.id==='transport').rows[22].state,'elevated','sampled_hour baseline ranks hour t<end at >=90th');
  for(const heroScale of ['linear','symlog'])for(const width of [240,800]){
   const el=element(width);
   v7Overview.app.drawHero(el,'24h',heroScale,true);
   assert.match(el.innerHTML,/1h intervals · UTC/);
   assert.doesNotMatch(el.innerHTML,/NaN|Infinity|undefined|Invalid Date/);
   assert.match(el.innerHTML,/:\d{2}</,'24h hero chart renders HH:MM X-axis ticks');
  }
  const h24Ins=inspectHero(h24Custom,'24h',['ArrowLeft','ArrowLeft']);
  assert.match(h24Ins.steps[0].tooltipHtml,/UTC \(1h\)/);
  assert.match(h24Ins.steps[1].tooltipHtml,/\d{2}:\d{2}–\d{2}:\d{2} UTC \(1h\)/);

  // Metric 24h plot + Explorer 24h band (uses reference.hour, NOT reference.day) + 2x2 cards + headline Copy button.
  const v7BandDoc=structuredClone(v7NonMidnight),v7Tr=chart('transport_rejections',v7BandDoc);
  const v7TrPrim=v7Tr.series.find(s=>s.key===v7Tr.primary_series);
  v7TrPrim.reference.day={n:100,p10_per_relay:0.01,p50_per_relay:0.02,p90_per_relay:0.03,mean_per_relay:0.02,current_percentile:50,flat_zero:false,start:v7BandDoc.reference_window.start,end:v7BandDoc.reference_window.end};
  v7TrPrim.reference.hour={n:100,p10_per_relay:10,p50_per_relay:20,p90_per_relay:30,mean_per_relay:20,current_percentile:99,flat_zero:false,start:v7BandDoc.reference_window.start,end:v7BandDoc.reference_window.end};
  v7TrPrim.peaks.hour={fleet_peak:777,fleet_peak_at:v7BandDoc.end-3600,fleet_peak_relays:240,current_is_record:true};
  v7TrPrim.current.hour={value:777,relays:240,expected_relays:240,coverage:1,valid:true};
  v7TrPrim.values[23][1]=777;v7TrPrim.hourly_relays[23][1]=240;v7TrPrim.hourly_expected_relays[23][1]=240;
  v7Tr.display_scale=1;
  const v7Exp=application('?view=explorer&chart=transport_rejections&range=24h&band=1',v7BandDoc);
  v7Exp.app.start();
  const v7Labels=statSections(v7Exp.page.innerHTML).map(s=>s.html.match(/<div class="stat-label">([^<]+)<\/div>/)?.[1]);
  assert.deepEqual(v7Labels,['Last complete hour','24-hour average','Last 7 days average','All-time average']);
  assert.match(v7Exp.page.innerHTML,/Sampled 1h fleet peak:/);
  assert.match(v7Exp.page.innerHTML,/Highest sampled hourly fleet value/);
  assert.match(v7Exp.page.innerHTML,/sampled hours \/ .*? days/);
  assert.match(v7Exp.page.innerHTML,/exports\/transport_rejections-24h\.csv/);
  assert.match(v7Exp.page.innerHTML,/PromQL · hourly plot/);
  assert.match(v7Exp.page.innerHTML,/24-hour headline query/);
  assert.match(v7Exp.page.innerHTML,/24 samples per 1-hour UTC interval/);
  assert.match(v7Exp.page.innerHTML,/<th>Last complete hour<\/th><th>24-hour average<\/th>/);
  const v7Copied=[];
  v7Exp.browser.navigator.clipboard={async writeText(txt){v7Copied.push(txt);}};
  const headBtn={dataset:{copy:'transport_rejections',queryKind:'headline'},textContent:'Copy',isConnected:true,closest:s=>s==='[data-copy]'?headBtn:null};
  await v7Exp.events.get('click')({target:headBtn});
  assert.equal(v7Copied[0],v7Tr.history_queries.current24.value);
  const plot24El=element(800);
  v7Exp.app.draw(plot24El,v7Tr,'24h');
  assert.match(plot24El.innerHTML,/1h intervals · UTC/);
  assert.match(plot24El.innerHTML,/<rect class="reference-band"[^>]*y="48\.[12][^>]*height="133\.2/);
  assert.match(plot24El.innerHTML,/<path class="reference-median" d="M62\.00,114\.80/);
  assert.match(plot24El.innerHTML,/>8,000</,'24h band axis scales from reference.hour (7,200 upper bound), not reference.day');
  assert.match(plot24El.innerHTML,/:00</);
  const expLive={textContent:''};
  let expCursor=null,expTooltip=null;
  const expSvg={querySelector:s=>s==='.plot-cursor'?expCursor:null,appendChild:n=>{expCursor=n;}};
  const expWrap={innerHTML:'',getBoundingClientRect:()=>({width:800,height:400}),closest:s=>s==='[data-chart-plot]'?expWrap:null,
   querySelector:s=>s==='svg'?expSvg:s==='.plot-cursor'?expCursor:s==='.chart-tooltip'?expTooltip:null,
   appendChild:n=>{expTooltip=n;},setAttribute(){},hasAttribute:()=>false};
  const expBandCtrl=application('?view=explorer&chart=transport_rejections&range=24h&band=1',v7BandDoc);
  const origExpQS=expBandCtrl.browser.document.querySelector;
  expBandCtrl.browser.document.querySelector=s=>s==='[data-live-status]'?expLive:origExpQS(s);
  expBandCtrl.browser.document.createElement=tag=>({tag,className:'',innerHTML:'',style:{},remove(){if(this===expTooltip)expTooltip=null;}});
  expBandCtrl.browser.document.createElementNS=(ns,tag)=>({ns,tag,attrs:{},setAttribute(k,v){this.attrs[k]=v;},remove(){if(this===expCursor)expCursor=null;}});
  expBandCtrl.app.start();
  expBandCtrl.app.draw(expWrap,v7Tr,'24h');
  expBandCtrl.events.get('keydown')({key:'End',target:expWrap,preventDefault(){}});
  assert.match(expTooltip.innerHTML,/10th–90th: 2,400–7,200 \(median 4,800\)/,'24h band tooltip reflects 240-relay reference.hour');

  // Empty reference.day ({n:0}) must not shadow a valid populated reference.hour span in reference(s).
  const hourOnlyRefDoc=structuredClone(v7BandDoc),trHourOnly=chart('transport_rejections',hourOnlyRefDoc);
  trHourOnly.series.find(s=>s.key===trHourOnly.primary_series).reference.day={n:0,current_percentile:null,flat_zero:false};
  const hourOnlyMarkup=application('?view=explorer&chart=transport_rejections&range=24h',hourOnlyRefDoc).app.markup();
  assert.match(hourOnlyMarkup,/100 sampled hours \/ 0 days/);
  assert.doesNotMatch(hourOnlyMarkup,/No reference observations yet/);

  // Live replaceRelease rollback v7 (24h hero + 24h range) -> v6 (7d hero + 1mo alias + daily freshness), plus implicit v6->v7 upgrade & explicit 7d preservation.
  const v7Rel=releaseContext(v7NonMidnight,'rel-v7',()=>v7NonMidnight.end+300);
  const v6Rel=releaseContext(v6Data,'rel-v6',()=>v6Data.end+300);
  const rbCtrl=application('?view=overview',v7NonMidnight,'1',v7Rel);
  rbCtrl.app.start();
  assert.match(rbCtrl.page.innerHTML,/Updated hourly/);
  assert.equal(rbCtrl.app.getState().heroRange,'24h');
  assert.equal(rbCtrl.app.getState().range,'24h');
  rbCtrl.app.replaceRelease(v6Data,v6Rel);
  assert.equal(rbCtrl.app.getState().heroRange,'7d','rollback to daily v6 clamps heroRange to 7d');
  assert.equal(rbCtrl.app.getState().range,'1mo','rollback to v6 migrates 24h range via legacy_aliases to 1mo');
  assert.match(rbCtrl.page.innerHTML,/Updated daily \(00:00 UTC\)/);
  assert.doesNotMatch(rbCtrl.page.innerHTML,/data-hero-range="24h"/);
  rbCtrl.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(rbCtrl.app.getState().heroRange,'24h','implicit v6->v7 upgrade restores default 24h heroRange');
  const exp7Upgrade=application('?view=overview&hero_range=7d',v6Data,'1',v6Rel);
  exp7Upgrade.app.start();
  exp7Upgrade.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(exp7Upgrade.app.getState().heroRange,'7d','explicit hero_range=7d persists across v6->v7 upgrade');
  assert.throws(()=>exp7Upgrade.app.replaceRelease({},v6Rel),/Missing chart data/);
  exp7Upgrade.app.replaceRelease(v6Data,v6Rel);
  assert.equal(exp7Upgrade.app.getState().heroRange,'7d');
  click(exp7Upgrade,'[data-hero-range]',{heroRange:'all'});
  assert.equal(exp7Upgrade.app.getState().heroRange,'all');
  exp7Upgrade.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(exp7Upgrade.app.getState().heroRange,'all','user-clicked heroRange remains explicit across v6->v7 upgrade');

  // Explicit hero_range=24h on schema-6 fallback renders 7d, preserves request across failed swap & url(), and upgrades to 24h when schema-7 arrives.
  const req24Ctrl=application('?view=overview&hero_range=24h',v6Data,'1',v6Rel);
  req24Ctrl.app.start();
  assert.equal(req24Ctrl.app.getState().heroRange,'7d','explicit hero_range=24h renders as 7d on schema-6 fallback');
  assert.equal(new URLSearchParams(req24Ctrl.app.url('overview').slice(1)).get('hero_range'),'24h','url() retains requested hero_range=24h during schema-6 fallback');
  assert.throws(()=>req24Ctrl.app.replaceRelease({},v6Rel),/Missing chart data/);
  assert.equal(req24Ctrl.app.getState().heroRange,'7d');
  req24Ctrl.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(req24Ctrl.app.getState().heroRange,'24h','explicit hero_range=24h upgrades to 24h on schema-7 payload');
  assert.throws(()=>req24Ctrl.app.replaceRelease({},v7Rel),/Missing chart data/);
  assert.equal(req24Ctrl.app.getState().heroRange,'24h','failed swap restores heroRange');
  req24Ctrl.app.replaceRelease(v6Data,v6Rel);
  assert.equal(req24Ctrl.app.getState().heroRange,'7d');
  req24Ctrl.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(req24Ctrl.app.getState().heroRange,'24h','requestedHeroRange 24h survives rollback->recovery');

  // Explicit ?hero_range=7d on schema-6 survives navigation + popstate + schema-7 upgrade, and clicking 7d updates fallback hero_range=24h URL.
  const expV6Nav=application('?view=overview&hero_range=7d',v6Data,'1',v6Rel);
  expV6Nav.app.start();
  assert.equal(new URLSearchParams(expV6Nav.app.url('overview').slice(1)).get('hero_range'),'7d');
  click(expV6Nav,'[data-all-range]',{allRange:'all'});
  assert.equal(new URLSearchParams(expV6Nav.browser.location.search).get('hero_range'),'7d');
  expV6Nav.back();
  expV6Nav.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(expV6Nav.app.getState().heroRange,'7d','explicit hero_range=7d on v6 survives popstate and v7 upgrade');
  const clampSwitch=application('?view=overview&hero_range=24h',v6Data,'1',v6Rel);
  clampSwitch.app.start();
  click(clampSwitch,'[data-hero-range]',{heroRange:'7d'});
  assert.equal(new URLSearchParams(clampSwitch.browser.location.search).get('hero_range'),'7d','clicking 7d overwrites clamped 24h request in URL');
  clampSwitch.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(clampSwitch.app.getState().heroRange,'7d');

  const impClick=application('?view=overview',v6Data,'1',v6Rel);
  impClick.app.start();
  assert.equal(new URLSearchParams(impClick.app.url('overview').slice(1)).has('hero_range'),false,'implicit hero_range is absent before click');
  click(impClick,'[data-hero-range]',{heroRange:'7d'});
  assert.equal(new URLSearchParams(impClick.app.url('overview').slice(1)).get('hero_range'),'7d','clicking implicit 7d persists hero_range in links');
  assert.equal(new URLSearchParams(impClick.browser.location.search).get('hero_range'),'7d','clicking implicit 7d persists hero_range in URL');
  click(impClick,'[data-hero-range]',{heroRange:'7d'});
  impClick.back();
  assert.equal(new URLSearchParams(impClick.browser.location.search).has('hero_range'),false,'repeated click on explicit 7d pushes no duplicate history entry');
  const impUpgrade=application('?view=overview',v6Data,'1',v6Rel);
  impUpgrade.app.start();
  click(impUpgrade,'[data-hero-range]',{heroRange:'7d'});
  impUpgrade.app.replaceRelease(v7NonMidnight,v7Rel);
  assert.equal(impUpgrade.app.getState().heroRange,'7d','v6->v7 upgrade retains explicitly clicked 7d');

  // validateTimeRanges rejects malformed hours definitions (including 24h minimum_coverage=0.5 parity with Python).
  for(const badHours of [
   tr=>{tr.periods[0].window.count=12;},
   tr=>{tr.periods[0].window.comparison_lag_days=1;},
   tr=>{tr.periods[0].minimum_coverage=0.5;},
   tr=>{tr.periods[0].id='custom24';},
   tr=>{tr.periods[0].export_suffix='custom-24';},
   tr=>{tr.periods.push({...structuredClone(tr.periods[0]),id:'h2',summary_key:'h_2',export_suffix:'h-2'});},
   tr=>{tr.card_slots={fallback_dynamic:'7d',fixed:['6mo','all']};},
  ]){
   const bad=structuredClone(v7NonMidnight.time_ranges);
   badHours(bad);
   assert.throws(()=>ui.validateTimeRanges(bad),/time_ranges|minimum_coverage/);
  }
  const badHourCov=structuredClone(v7NonMidnight.time_ranges);
  badHourCov.periods[0].minimum_coverage=0.5;
  assert.throws(()=>ui.validateTimeRanges(badHourCov),/Invalid time_ranges hours minimum_coverage/);
  const validHourCov=structuredClone(v7NonMidnight.time_ranges);
  validHourCov.periods[0].minimum_coverage=0;
  assert.equal(ui.validateTimeRanges(validHourCov),validHourCov,'validateTimeRanges accepts 24h with minimum_coverage=0');

  // Concurrent family alerts KPI lists all alerted families (no truncation) and links each to #family-<id>.
  const makeAlertDataset=(src,period,states)=>{
   const doc=structuredClone(src);
   for(const [idx,fam]of doc.families.entries()){
    const st=states[idx]||'neutral';
    for(const cid of fam.children){
     for(const s of chart(cid,doc).series){
      s.reference[period]={n:120,current_percentile:st==='high'?99:st==='elevated'?92:25,flat_zero:false,start:doc.reference_window.start,end:doc.reference_window.end};
      if(s.current[period])s.current[period].valid=true;
     }
    }
   }
   return doc;
  };
  const allNormalDaily=makeAlertDataset(v6Data,'day',Array(v6Data.families.length).fill('neutral'));
  const zeroAlertHtml=application('?view=overview',allNormalDaily).app.markup();
  assert.match(zeroAlertHtml,/0 of 6 <small>families ≥90th pct<\/small>.*No qualifying family alerts/);
  assert.doesNotMatch(zeroAlertHtml,/class="hero-alert-list"|data-family-jump/);

  for(const [baseDoc,period,states]of[
   [v7NonMidnight,'hour',['elevated','high','elevated','neutral','high','elevated']],
   [v6Data,'day',['high','neutral','elevated','elevated','high','elevated']],
  ]){
   const multiDoc=makeAlertDataset(baseDoc,period,states);
   const ctrl=application('?view=overview',multiDoc);
   ctrl.app.start();
   const links=ctrl.browser.document.querySelectorAll('[data-family-jump]');
   const expectedFams=multiDoc.families.filter((_,i)=>states[i]==='high'||states[i]==='elevated');
   assert.equal(links.length,expectedFams.length,'lists every alerted family without truncation');
   assert.doesNotMatch(ctrl.page.innerHTML,/>\+\d+</,'never truncates extra alerted families with +N');
   assert.match(ctrl.page.innerHTML,/<ul class="hero-alert-list" aria-label="Alerted families">/);
   const articles=ctrl.browser.document.querySelectorAll('article[tabindex]');
   assert.equal(articles.length,multiDoc.families.length,'all family cards support programmatic focus');
   links.forEach((link,idx)=>{
    const fam=expectedFams[idx],sev=states[multiDoc.families.findIndex(f=>f.id===fam.id)];
    assert.equal(link.getAttribute('href'),'#family-'+encodeURIComponent(fam.id));
    assert.equal(link.dataset.familyJump,fam.id);
    assert.equal(link.textContent,`${fam.icon} ${fam.title}`);
    assert.ok(link.getAttribute('class').split(/\s+/).includes(sev));
    assert.ok(ctrl.browser.document.getElementById('family-'+fam.id),'target family article exists');
   });
  }

  // Clicking a hero alert link preserves query params, scrolls & focuses #family-<id>, skips duplicate history pushes, handles popstate & modifiers.
  const navAlertDoc=makeAlertDataset(v7NonMidnight,'hour',['neutral','high','elevated','neutral','neutral','neutral']);
  const navCtrl=application('?view=overview&range=all&hero_range=7d',navAlertDoc,'1',releaseContext(navAlertDoc,'jump-1',()=>navAlertDoc.end));
  navCtrl.app.start();
  const dirLink=navCtrl.browser.document.querySelector('[data-family-jump="directory"]');
  const dirCard=navCtrl.browser.document.getElementById('family-directory');
  assert.ok(dirLink&&dirCard);
  const scrollCalls=[];
  dirCard.scrollIntoView=opts=>{scrollCalls.push(opts);};
  let prevented=0;
  navCtrl.events.get('click')({target:dirLink,button:0,preventDefault(){prevented++;}});
  assert.equal(prevented,1);
  assert.equal(navCtrl.browser.location.search,'?view=overview&range=all&hero_range=7d','hash jump preserves existing query parameters');
  assert.equal(navCtrl.browser.location.hash,'#family-directory');
  assert.deepEqual(scrollCalls,[{block:'start'}]);
  assert.equal(navCtrl.browser.document.activeElement,dirCard,'focus moves to target family card');

  // Repeat click on the already-active hash scrolls & focuses again without pushing a duplicate history entry.
  navCtrl.events.get('click')({target:dirLink,button:0,preventDefault(){prevented++;}});
  assert.equal(prevented,2);
  assert.equal(scrollCalls.length,2);
  navCtrl.back();
  assert.equal(navCtrl.browser.location.hash,'','single Back returns before repeated jump');
  assert.equal(navCtrl.browser.document.activeElement,navCtrl.browser.document.querySelector('[data-view-heading]'));
  navCtrl.forward();
  assert.equal(navCtrl.browser.location.hash,'#family-directory');
  assert.equal(navCtrl.browser.document.activeElement.getAttribute('id'),'family-directory');

  // Live replaceRelease preserves focus both on #family-<id> cards and on [data-family-jump] links.
  navCtrl.app.replaceRelease(structuredClone(navAlertDoc),releaseContext(navAlertDoc,'jump-2',()=>navAlertDoc.end));
  assert.equal(navCtrl.browser.document.activeElement.getAttribute('id'),'family-directory','replaceRelease restores focus on family card');
  navCtrl.browser.document.querySelector('[data-family-jump="circuits"]').focus({preventScroll:true});
  navCtrl.app.replaceRelease(structuredClone(navAlertDoc),releaseContext(navAlertDoc,'jump-3',()=>navAlertDoc.end));
  assert.equal(navCtrl.browser.document.activeElement,navCtrl.browser.document.querySelector('[data-family-jump="circuits"]'),'replaceRelease restores focus on alert jump link');

  // Modified clicks let the browser open #family-<id> in a new tab/window natively.
  for(const mod of [{metaKey:true},{ctrlKey:true},{shiftKey:true},{altKey:true},{button:1}]){
   let modPrevented=false;
   navCtrl.events.get('click')({target:navCtrl.browser.document.querySelector('[data-family-jump="circuits"]'),button:0,...mod,preventDefault(){modPrevented=true;}});
   assert.equal(modPrevented,false);
   assert.equal(navCtrl.browser.location.hash,'#family-directory');
  }

  // Hostile family id & title are safely escaped in attributes/text and click resolves getElementById safely without selector errors.
  const hostileDoc=makeAlertDataset(v7NonMidnight,'hour',['high','neutral','neutral','neutral','neutral','neutral']);
  const hostileId='bad"id<1>& a%b';
  hostileDoc.families[0].id=hostileId;
  hostileDoc.families[0].title='Conn <script> & "floods"';
  for(const cid of hostileDoc.families[0].children)chart(cid,hostileDoc).family_id=hostileId;
  const hostileCtrl=application('?view=overview',hostileDoc);
  hostileCtrl.app.start();
  const hostileHtml=hostileCtrl.page.innerHTML;
  assert.doesNotMatch(hostileHtml,/<script>/);
  assert.match(hostileHtml,/01 Conn &lt;script&gt; &amp; &quot;floods&quot;/);
  const hostileLink=hostileCtrl.browser.document.querySelectorAll('[data-family-jump]')[0];
  assert.equal(hostileLink.getAttribute('href'),'#family-'+encodeURIComponent(hostileId));
  assert.equal(hostileLink.dataset.familyJump,hostileId);
  const hostileCard=hostileCtrl.browser.document.getElementById('family-'+hostileId);
  assert.ok(hostileCard);
  hostileCtrl.events.get('click')({target:hostileLink,button:0,preventDefault(){}});
  assert.equal(hostileCtrl.browser.location.hash,'#family-'+encodeURIComponent(hostileId));
  assert.equal(hostileCtrl.browser.document.activeElement,hostileCard);

  // Sticky-header scroll-margin, focus outline suppression for article[id], and mobile .hero-alert-link overrides.
  assert.match(latestCss,/h3\[id\],article\[id\]\s*\{\s*scroll-margin-top:\s*var\(--header-offset,80px\);/);
  const artFocusIdx=latestCss.indexOf('article[id]:focus { outline: none; }');
  const artVisIdx=latestCss.indexOf('article[id]:focus-visible');
  assert.ok(artFocusIdx>=0&&artVisIdx>artFocusIdx,'article[id]:focus outline suppression precedes :focus-visible');
  const cssMobTail=latestCss.slice(latestCss.lastIndexOf('@media(max-width:600px)'));
  assert.ok(cssMobTail.includes('.hero-alert-link { font-size: 11px')&&latestCss.lastIndexOf('.hero-alert-link { font-size: 11px')>latestCss.indexOf('.hero-alert-link { display:'),
   'mobile .hero-alert-link overrides base rule');

  // schema_version 7 with window_seconds 86400 renders daily-only UI (no 24h hero panel or Last complete hour cards/columns, 30h freshness threshold).
  const v7DailyPaths=['data.json','manifest.json',v7DailyData.reference_document,
   ...v7DailyData.charts.flatMap(item=>v7DailyData.time_ranges.periods.map(p=>'exports/'+item.id+'-'+p.export_suffix+'.csv'))];
  let dailyNow=v7DailyData.end+10000; // > 2h (7200s) but < 30h (108000s): delayed only if misclassified as hourly
  const v7DailyRelease={mode:'live',id:'v7-daily-rel',generatedAt:v7DailyData.end+600,now:()=>dailyNow,
   downloads:new Map(v7DailyPaths.map(file=>[file,'blob:v7-daily/'+file]))};
  const v7DailyOverview=application('?view=overview',v7DailyData,'1',v7DailyRelease);
  v7DailyOverview.app.start();
  assert.equal(v7DailyOverview.app.getState().heroRange,'7d');
  assert.equal(v7DailyOverview.browser.document.querySelectorAll('[data-hero-panel]').length,2);
  assert.equal(v7DailyOverview.browser.document.querySelector('[data-hero-panel="24h"]'),null);
  assert.match(v7DailyOverview.page.innerHTML,/Concurrent family alerts \(24h\)/);
  assert.match(v7DailyOverview.page.innerHTML,/Updated daily \(00:00 UTC\)/);
  assert.doesNotMatch(v7DailyOverview.page.innerHTML,/Update delayed|Updated hourly|Last complete hour|Sampled 1h fleet peak/);
  dailyNow=v7DailyData.end+108000;
  v7DailyOverview.app.updateFreshness({failed:false});
  assert.doesNotMatch(v7DailyOverview.app.markup(),/Update delayed/);
  dailyNow=v7DailyData.end+108001;
  v7DailyOverview.app.updateFreshness({failed:false});
  assert.match(v7DailyOverview.browser.document.querySelector('.snapshot').innerHTML,/Update delayed/);
  assert.match(v7DailyOverview.app.markup(),/Update delayed/);

  const v7DailyExp=application('?view=explorer&chart=transport_rejections&range=1mo',v7DailyData,'1',v7DailyRelease);
  v7DailyExp.app.start();
  const v7DailyStatLabels=statSections(v7DailyExp.page.innerHTML).map(s=>s.html.match(/<div class="stat-label">([^<]+)<\/div>/)?.[1]);
  assert.deepEqual(v7DailyStatLabels,['24-hour average','Last 1 month average','Last 6 months average','All-time average']);
  const v7DailyTable=v7DailyExp.page.innerHTML.match(/<table data-values-table>([\s\S]*?)<\/table>/)[1];
  const v7DailyHeaders=[...v7DailyTable.matchAll(/<th>(.*?)<\/th>/g)].map(([,h])=>decode(h));
  assert.ok(!v7DailyHeaders.includes('Last complete hour'));
  assert.match(application('?view=methods',v7DailyData,'1',v7DailyRelease).app.markup(),/Daily publication and reference updates/);

  // Hero alert sibling Explorer deep link preserves [data-family-jump] text and routes directly to the alerting series.
  const alertDeepDoc=makeAlertDataset(v7NonMidnight,'hour',['high','neutral','neutral','neutral','neutral','neutral']);
  const alertDeepCtrl=application('?view=overview',alertDeepDoc);
  alertDeepCtrl.app.start();
  const famJumpEl=alertDeepCtrl.browser.document.querySelector('[data-family-jump="transport"]');
  assert.equal(decode(famJumpEl.textContent.trim()),'01 Connection floods');
  const detailDeepEl=alertDeepCtrl.browser.document.querySelector('.hero-alert-detail');
  assert.ok(detailDeepEl,'hero alert includes sibling Explorer detail link');
  assert.equal(detailDeepEl.dataset.explore,'transport');
  assert.equal(detailDeepEl.dataset.chart,'transport_rejections');
  assert.equal(detailDeepEl.dataset.range,'24h');
  const expectedAlertSeries=detailDeepEl.dataset.series;
  assert.ok(expectedAlertSeries);
  alertDeepCtrl.events.get('click')({target:detailDeepEl,button:0,preventDefault(){}});
  assert.equal(alertDeepCtrl.app.getState().view,'explorer');
  assert.equal(alertDeepCtrl.app.getState().chartId,'transport_rejections');
  assert.equal(alertDeepCtrl.app.getState().seriesKey,expectedAlertSeries);
  assert.equal(alertDeepCtrl.app.getState().range,'24h');

  // Inactive 0-exit fleet: active denominator drops to 5, .fleet-strip renders, 0-exit callout shown, and flat-zero SVG uses 140px compact height.
  const zeroExitDoc=structuredClone(v7NonMidnight);
  zeroExitDoc.flags={Running:240,Guard:64,Exit:0,HSDir:48,V2Dir:160};
  const exitChartObj=chart('exit_stream_rejections',zeroExitDoc);
  for(const s of exitChartObj.series){
   s.values=s.values.map(([t])=>[t,0]);
   s.history.daily=s.history.daily.map(([t,v,n,cov,ok])=>[t,0,n,cov,ok]);
   s.current.hour.value=0;s.current.day.value=0;
   s.reference.hour={n:100,current_percentile:null,flat_zero:true,mean_per_relay:0,p10_per_relay:0,p50_per_relay:0,p90_per_relay:0,start:s.history.daily[0][0],end:s.history.daily.at(-1)[0]};
   s.reference.day={n:100,current_percentile:null,flat_zero:true,mean_per_relay:0,p10_per_relay:0,p50_per_relay:0,p90_per_relay:0,start:s.history.daily[0][0],end:s.history.daily.at(-1)[0]};
  }
  const zeroExitCtrl=application('?view=overview',zeroExitDoc);
  zeroExitCtrl.app.start();
  assert.match(zeroExitCtrl.page.innerHTML,/2 of 5 <small>active families ≥90th pct<\/small>/);
  assert.ok(zeroExitCtrl.browser.document.querySelector('.fleet-strip'));
  assert.match(zeroExitCtrl.page.innerHTML,/0 Exit \(non-exit fleet\)/);
  assert.doesNotMatch(zeroExitCtrl.page.innerHTML,/middle-only fleet/);
  assert.match(zeroExitCtrl.page.innerHTML,/class="card-callout"[\s\S]*0 Exit relays/);
  const flatClasses=new Set();
  const flatPlotEl=Object.assign(element(800),{classList:{toggle(k,v){if(v)flatClasses.add(k);else flatClasses.delete(k);},contains(k){return flatClasses.has(k);}}});
  zeroExitCtrl.app.draw(flatPlotEl,exitChartObj,'7d',800);
  assert.match(flatPlotEl.innerHTML,/viewBox="0 0 800 140"/);
  assert.ok(flatPlotEl.classList.contains('plot-flat-zero'));

  // Minimal fixture without flags/versions/dircache_cohort omits .fleet-strip cleanly.
  const minimalStripDoc=structuredClone(v7NonMidnight);
  delete minimalStripDoc.flags;delete minimalStripDoc.versions;delete minimalStripDoc.dircache_cohort;
  const minimalStripCtrl=application('?view=overview',minimalStripDoc);
  minimalStripCtrl.app.start();
  assert.equal(minimalStripCtrl.browser.document.querySelector('.fleet-strip'),null);

  // Multi-chart family .child-strip highlights alerting child chart + series with Explorer deep link.
  const childStripDoc=makeAlertDataset(v7NonMidnight,'hour',['neutral','neutral','neutral','neutral','neutral','neutral']);
  const dirRejSeries=chart('directory_rejections',childStripDoc).series[0];
  dirRejSeries.current.hour.valid=true;
  dirRejSeries.reference.hour={n:120,current_percentile:99.5,flat_zero:false,mean_per_relay:0.01,p10_per_relay:0,p50_per_relay:0.01,p90_per_relay:0.02,start:1,end:2};
  const childStripCtrl=application('?view=overview',childStripDoc);
  childStripCtrl.app.start();
  const dirFamilyEl=childStripCtrl.browser.document.getElementById('family-directory');
  const highChildPill=dirFamilyEl.querySelector('.high');
  assert.ok(highChildPill,'multi-chart directory family shows high child pill');
  assert.ok((highChildPill.getAttribute('class')||'').split(/\s+/).includes('child-pill'));
  assert.equal(highChildPill.dataset.chart,'directory_rejections');
  assert.equal(highChildPill.dataset.series,dirRejSeries.key);

  // Explorer auxiliary directory_requests series selection plots the selected auxiliary series and survives replaceRelease; default still plots multi-series display_keys.
  const auxSelectCtrl=application('?view=explorer&chart=directory_requests&series=consensus&range=1mo',data,'1',releaseContext(data,'aux-1',()=>data.end));
  auxSelectCtrl.app.start();
  const dirReqChart=chart('directory_requests',data);
  assert.equal(auxSelectCtrl.browser.document.querySelectorAll('option').length,dirReqChart.series.length);
  assert.equal(auxSelectCtrl.app.getState().seriesKey,'consensus');
  assert.match(selectorMarkup(auxSelectCtrl.page.innerHTML)[0].text,/consensus/);
  assert.doesNotMatch(selectorMarkup(auxSelectCtrl.page.innerHTML)[0].text,/hs_get/);
  const auxPlot=element(800);
  auxSelectCtrl.app.draw(auxPlot,dirReqChart,'1mo',800);
  assert.match(auxPlot.innerHTML,/class="measurement-series"/);
  const freshAux=structuredClone(data);
  auxSelectCtrl.app.replaceRelease(freshAux,releaseContext(freshAux,'aux-2',()=>freshAux.end));
  assert.equal(auxSelectCtrl.app.getState().seriesKey,'consensus');
  const defDirCtrl=application('?view=explorer&chart=directory_requests&range=1mo',data);
  defDirCtrl.app.start();
  assert.match(selectorMarkup(defDirCtrl.page.innerHTML)[0].text,/hs_get\|hs_post\|no_request/);

  // Partial-window (Xd) badges in Explorer values table, live fleet build mix in Fixes, and bulleted coarsening list in Methods.
  const partialBadgeDoc=structuredClone(data);
  chart('transport_rejections',partialBadgeDoc).series[0].historical_averages.six_months={
   status:'ready',value:1.2,display:'1.2',days:45,expected_days:180,start:1,end:2,observed_start:1,observed_end:2,
  };
  const partialBadgeHtml=application('?view=explorer&chart=transport_rejections&range=6mo',partialBadgeDoc).app.markup();
  assert.match(partialBadgeHtml,/<span class="table-days"[^>]*>\(45d\)<\/span>/);
  const fixesHtml=application('?view=fixes',data).app.markup();
  assert.match(fixesHtml,/22 Nov 2026 UTC/);
  assert.match(fixesHtml,/Current fleet build mix/);
  assert.match(fixesHtml,/Patched/);
  assert.match(fixesHtml,/0\.4\.9\.15/);
  const methodsHtml=application('?view=methods',data).app.markup();
  assert.match(methodsHtml,/<ul class="coarsening-list">([\s\S]*<li>){5}/);

  // Explicit link series selection clears bandEnabled on same chart; neutral child-pill explicit data-series="" clears seriesKey/bandEnabled while omitting data-series preserves.
  const sameChartBandCtrl=application('?view=explorer&chart=transport_rejections&range=24h&band=1',alertDeepDoc);
  sameChartBandCtrl.app.start();
  assert.equal(sameChartBandCtrl.app.getState().bandEnabled,true);
  sameChartBandCtrl.events.get('click')({target:sameChartBandCtrl.browser.document.querySelector('[data-view="overview"]'),button:0,preventDefault(){}});
  const sameDetailEl=sameChartBandCtrl.browser.document.querySelector('.hero-alert-detail');
  sameChartBandCtrl.events.get('click')({target:sameDetailEl,button:0,preventDefault(){}});
  assert.equal(sameChartBandCtrl.app.getState().chartId,'transport_rejections');
  assert.equal(sameChartBandCtrl.app.getState().seriesKey,sameDetailEl.dataset.series);
  assert.equal(sameChartBandCtrl.app.getState().bandEnabled,false);

  const neutralResetCtrl=application('?view=explorer&chart=directory_requests&series=consensus&range=24h',childStripDoc);
  neutralResetCtrl.app.start();
  assert.equal(neutralResetCtrl.app.getState().seriesKey,'consensus');
  neutralResetCtrl.events.get('click')({target:neutralResetCtrl.browser.document.querySelector('[data-view="overview"]'),button:0,preventDefault(){}});
  const dirFamilyResetEl=neutralResetCtrl.browser.document.getElementById('family-directory');
  const neutralChildPill=dirFamilyResetEl.querySelector('[data-chart="directory_requests"]');
  assert.ok(neutralChildPill&&(neutralChildPill.getAttribute('class')||'').split(/\s+/).includes('neutral'));
  assert.equal(neutralChildPill.dataset.series,'');
  neutralResetCtrl.events.get('click')({target:neutralChildPill,button:0,preventDefault(){}});
  assert.equal(neutralResetCtrl.app.getState().chartId,'directory_requests');
  assert.equal(neutralResetCtrl.app.getState().seriesKey,null);

  // 1. Non-midnight hourly Hero 7d KPI labels clarify completed UTC day vs rolling 24h; midnight keeps (24h).
  const nonMidHero7Html=application('?view=overview&hero_range=7d',v7NonMidnight).app.markup();
  assert.match(nonMidHero7Html,/Latest UTC day mitigated rate/);
  assert.match(nonMidHero7Html,/Concurrent family alerts \(UTC day\)/);
  assert.match(nonMidHero7Html,/Open circuits \(UTC day mean\)/);
  assert.match(nonMidHero7Html,/Stalled BEGIN_DIR \(UTC day mean\)/);
  assert.match(nonMidHero7Html,/completed UTC day/);
  assert.match(nonMidHero7Html,/Rolling 24h · /);
  const midHero7Doc=structuredClone(v7NonMidnight);
  const midEnd=Math.floor(midHero7Doc.end/86400)*86400;
  midHero7Doc.end=midEnd;midHero7Doc.start=midEnd-86400;
  const midHero7Html=application('?view=overview&hero_range=7d',midHero7Doc).app.markup();
  assert.match(midHero7Html,/Latest 24h mitigated rate/);
  assert.match(midHero7Html,/Concurrent family alerts \(24h\)/);

  // 2. Hero alert detail & tooltip name non-primary alerting child charts and (+N more), keeping [data-family-jump] text clean.
  const childHeroDoc=makeAlertDataset(v7NonMidnight,'hour',['neutral','neutral','neutral','neutral','neutral','neutral']);
  const hsNotFound=chart('hsdir_fetches',childHeroDoc).series.find(s=>s.key==='not_found');
  const dirRejSer=chart('directory_rejections',childHeroDoc).series.find(s=>s.key==='begindir_rejected');
  hsNotFound.current.hour={value:2400,relays:240,expected_relays:240,coverage:1,valid:true};
  hsNotFound.values[23][1]=2400;
  hsNotFound.reference.hour={n:120,current_percentile:100,flat_zero:false,mean_per_relay:1,p10_per_relay:0.5,p50_per_relay:1,p90_per_relay:2,start:childHeroDoc.reference_window.start,end:childHeroDoc.reference_window.end};
  dirRejSer.current.hour={value:120,relays:240,expected_relays:240,coverage:1,valid:true};
  dirRejSer.values[23][1]=120;
  dirRejSer.reference.hour={n:100,current_percentile:95,flat_zero:false,mean_per_relay:0.2,p10_per_relay:0,p50_per_relay:0.1,p90_per_relay:0.5,start:childHeroDoc.reference_window.start,end:childHeroDoc.reference_window.end};
  const childHeroCtrl=application('?view=overview',childHeroDoc);
  childHeroCtrl.app.start();
  assert.equal(decode(childHeroCtrl.browser.document.querySelector('[data-family-jump="directory"]').textContent.trim()),'03 Directory floods');
  const dirDetailEl=childHeroCtrl.browser.document.querySelector('.hero-alert-detail');
  assert.match(decode(dirDetailEl.textContent),/^HSDir GET request handling: Descriptor not found · 100th pct \(\+1 more\)$/);
  assert.match(inspectHero(childHeroDoc,'24h',['End']).steps[0].tooltipHtml,/03 Directory floods \(high, 100th\) — HSDir GET request handling: Descriptor not found/);

  // 3. Hero gauge legend accurately displays left-panel window peak alongside All-time record.
  const dualGaugeDoc=structuredClone(v7NonMidnight);
  chart('open_circuits',dualGaugeDoc).series[0].peaks.hour={fleet_peak:174e6,fleet_peak_at:dualGaugeDoc.end-3600,fleet_peak_relays:240,current_is_record:false};
  chart('open_circuits',dualGaugeDoc).series[0].peaks.day={fleet_peak:128e6,fleet_peak_at:dualGaugeDoc.end-86400,fleet_peak_relays:240,current_is_record:false};
  chart('open_circuits',dualGaugeDoc).series[0].values=chart('open_circuits',dualGaugeDoc).series[0].values.map(([t],i)=>[t,i===23?18.6e6:10e6]);
  chart('open_circuits',dualGaugeDoc).series[0].history.daily=chart('open_circuits',dualGaugeDoc).series[0].history.daily.map(([t],i)=>[t,i===0?128e6:12e6,240,1,true]);
  const dualGaugeHtml=application('?view=overview',dualGaugeDoc).app.markup();
  assert.match(dualGaugeHtml,/Open circuits \(100% = 18\.6M \(24h\) · 128M \(all-time\)\)/);

  // 4 & 5. Multi-chart card surfaces .card-alert-callout inside .card-context, prefixes .primary-label, and excludes hidden/gauge series from .child-strip.
  const hiddenPolluteDoc=makeAlertDataset(childHeroDoc,'day',['neutral','neutral','neutral','neutral','neutral','neutral']);
  const otherPost=chart('directory_requests',hiddenPolluteDoc).series.find(s=>s.key==='other_post');
  const ocGauge=chart('open_circuits',hiddenPolluteDoc).series[0];
  otherPost.current.day={value:0.001,relays:240,expected_relays:240,coverage:1,valid:true};
  otherPost.reference.day={n:31,current_percentile:97,flat_zero:false,mean_per_relay:0,p10_per_relay:0,p50_per_relay:0,p90_per_relay:0,start:hiddenPolluteDoc.reference_window.start,end:hiddenPolluteDoc.reference_window.end};
  ocGauge.current.day={value:1.5e7,relays:240,expected_relays:240,coverage:1,valid:true};
  ocGauge.reference.day={n:120,current_percentile:100,flat_zero:false,mean_per_relay:1,p10_per_relay:0.5,p50_per_relay:1,p90_per_relay:2,start:hiddenPolluteDoc.reference_window.start,end:hiddenPolluteDoc.reference_window.end};
  const hiddenPolluteCtrl=application('?view=overview&range=7d',hiddenPolluteDoc);
  hiddenPolluteCtrl.app.start();
  const dirArtEl=hiddenPolluteCtrl.browser.document.getElementById('family-directory');
  const dirArtHtml=hiddenPolluteCtrl.page.innerHTML.match(/<article id="family-directory"[\s\S]*?<\/article>/)[0];
  assert.match(dirArtHtml,/class="primary-label">Primary chart: Directory request mix — HSDir GET requests</);
  const calloutEl=dirArtEl.querySelector('.card-alert-callout');
  assert.ok(calloutEl&&calloutEl.getAttribute('class').includes('callout-high'));
  const calloutHtml=dirArtHtml.match(/<div class="card-alert-callout[\s\S]*?<\/ul><\/div>/)[0];
  assert.match(calloutHtml,/HSDir GET request handling: Descriptor not found →/);
  assert.match(calloutHtml,/1h: .* \(100th pct\)/);
  assert.match(calloutHtml,/Directory flood counters: BEGIN_DIR lifetime-limit refusals →/);
  assert.match(dirArtHtml,/Family charts \(5 · rolling 24h status\)/);
  assert.equal(dirArtEl.querySelector('[data-chart="directory_requests"]').getAttribute('class'),'child-pill neutral','hidden other_post never turns primary child pill gold');
  assert.equal(hiddenPolluteCtrl.browser.document.getElementById('family-circuits').querySelector('[data-chart="open_circuits"]').getAttribute('class'),'child-pill neutral','gauge open_circuits never turns child pill gold');

  // 6, 7, 8. Short-history reference labels, neutral short-window 7d comparison badge, and informative invalid coverage reason.
  const dirReqOvHtml=application('?view=overview&range=7d',v7NonMidnight).app.markup();
  const dirCardChunk=dirReqOvHtml.match(/<article id="family-directory"[\s\S]*?<\/article>/)[0];
  const dirPrimRef=chart('directory_requests',v7NonMidnight).series.find(s=>s.key==='hs_get').reference;
  assert.match(dirCardChunk,new RegExp(`Reference average \\(${dirPrimRef.hour.n}h\\):`));
  assert.match(dirCardChunk,new RegExp(`Reference average \\(${dirPrimRef.day.n}d\\):`));
  assert.match(dirCardChunk,/Available reference:/);
  assert.doesNotMatch(dirCardChunk,/18-month average:|18-month reference:/);
  const short7dDoc=structuredClone(v7NonMidnight);
  const short7dTr=chart('transport_rejections',short7dDoc);
  short7dTr.series.find(s=>s.key===short7dTr.primary_series).historical_averages.seven_days.comparison=comparison(7,100);
  const short7dHtml=application('?view=explorer&chart=transport_rejections&range=7d',short7dDoc).app.markup();
  assert.match(short7dHtml,/class="percentile short">Current 24h: 100th percentile/);
  assert.match(short7dHtml,/vs prior 7 days \(short window\)/);
  const lowCovDoc=structuredClone(v7NonMidnight);
  const lowCovDr=chart('directory_rejections',lowCovDoc);
  lowCovDr.series.find(s=>s.key===lowCovDr.primary_series).current.day={value:null,relays:920,expected_relays:1088,coverage:920/1088,valid:false};
  const lowCovHtml=application('?view=explorer&chart=directory_rejections&range=7d',lowCovDoc).app.markup();
  assert.match(lowCovHtml,/Insufficient current coverage \(84\.6% · 920\/1,088 relays; ≥90% required\)/);

  // 9. Fleet build classification distinguishes Patched, Stock, Daemon-attested, and Unverified summing cleanly to native_total.
  const fleetMixDoc=structuredClone(v7NonMidnight);
  fleetMixDoc.versions={
   ...fleetMixDoc.versions,
   native_total:1088,runtime_verified_total:912,runtime_unverified_relays:176,
   verified_runtime:[
    {build_id:'attested-0_4_9_17',version:'0.4.9.17',base_version:'0.4.9.17',relays:136,verification:'Daemon version attested'},
    {build_id:'tor41339.9',version:'0.4.9.15 + tor41339.9',base_version:'0.4.9.15',patched:true,relays:384},
    {build_id:'stock.15',version:'0.4.9.15',base_version:'0.4.9.15',patched:false,relays:392},
   ],
  };
  const fleetOvHtml=application('?view=overview',fleetMixDoc).app.markup();
  assert.match(fleetOvHtml,/Patched 384 · Stock 392 · Attested 136 · Unverified 176/);
  const fleetFixesHtml=application('?view=fixes',fleetMixDoc).app.markup();
  assert.match(fleetFixesHtml,/1,088 relays total · 912 verified · 176 unverified/);
  assert.match(fleetFixesHtml,/0\.4\.9\.17[\s\S]*?Daemon-attested[\s\S]*?136/);
  assert.match(fleetFixesHtml,/Version not recorded[\s\S]*?Unverified[\s\S]*?176/);
  assert.ok(fleetFixesHtml.indexOf('Current fleet build mix')<fleetFixesHtml.indexOf('22 Nov 2026 UTC'),
   'live build mix precedes static 22 Nov 2026 UTC known-issues timestamp');
  assert.ok(latestCss.includes('.callout-high')&&latestCss.includes('.percentile.short')&&latestCss.includes('.status-neutral'),
   'stylesheet defines callout, short-window comparison, and build-badge rules');

  // 10. Alert callout avoids 0th pct on flat-zero non-alerting hours, labels other primary-chart series honestly, and uses 24h on daily-only bundles.
  const flatHrCalloutDoc=makeAlertDataset(v7NonMidnight,'hour',['neutral','neutral','neutral','neutral','neutral','neutral']);
  for(const c of flatHrCalloutDoc.charts)for(const ser of c.series)ser.reference.day={n:120,current_percentile:25,flat_zero:false,start:flatHrCalloutDoc.reference_window.start,end:flatHrCalloutDoc.reference_window.end};
  const noReq=chart('directory_requests',flatHrCalloutDoc).series.find(s=>s.key==='no_request');
  noReq.current.hour={value:0,relays:240,expected_relays:240,coverage:1,valid:true};
  noReq.reference.hour={n:100,current_percentile:null,flat_zero:true,mean_per_relay:0,p10_per_relay:0,p50_per_relay:0,p90_per_relay:0,start:flatHrCalloutDoc.reference_window.start,end:flatHrCalloutDoc.reference_window.end};
  noReq.current.day={value:5,relays:240,expected_relays:240,coverage:1,valid:true};
  noReq.reference.day={n:100,current_percentile:95,flat_zero:false,mean_per_relay:0.01,p10_per_relay:0,p50_per_relay:0.01,p90_per_relay:0.02,start:flatHrCalloutDoc.reference_window.start,end:flatHrCalloutDoc.reference_window.end};
  const flatHrDirHtml=application('?view=overview',flatHrCalloutDoc).app.markup().match(/<article id="family-directory"[\s\S]*?<\/article>/)[0];
  assert.match(flatHrDirHtml,/Active alert in other series/);
  assert.match(flatHrDirHtml,/1h: 0 streams\/s \(flat zero\)/);
  assert.doesNotMatch(flatHrDirHtml,/\b0th pct/);
  const dailyCalloutDoc=makeAlertDataset(v6Data,'day',['neutral','neutral','neutral','neutral','neutral','neutral']);
  const dailyNotFound=chart('hsdir_fetches',dailyCalloutDoc).series.find(s=>s.key==='not_found');
  dailyNotFound.current.day={value:1200,relays:100,expected_relays:100,coverage:1,valid:true};
  dailyNotFound.reference.day={n:120,current_percentile:100,flat_zero:false,mean_per_relay:1,p10_per_relay:0.5,p50_per_relay:1,p90_per_relay:2,start:dailyCalloutDoc.reference_window.start,end:dailyCalloutDoc.reference_window.end};
  const dailyDirHtml=application('?view=overview',dailyCalloutDoc).app.markup().match(/<article id="family-directory"[\s\S]*?<\/article>/)[0];
  assert.match(dailyDirHtml,/24h: 1,200 requests\/s \(100th pct\)/);
  assert.doesNotMatch(dailyDirHtml,/Rolling 24h:/);

  // 11. CodeRabbit review regressions: current card uses 20-window minimum (matching Hero/callout), all-unverified inventory still renders, and Methods reflects hero_range=all KPI scope.
  const ref25Doc=structuredClone(v7NonMidnight);
  const tr25=chart('transport_rejections',ref25Doc).series.find(s=>s.key==='conn_rejected');
  tr25.reference.hour={n:25,current_percentile:95,flat_zero:false,mean_per_relay:0.1,p10_per_relay:0,p50_per_relay:0.1,p90_per_relay:0.2,start:ref25Doc.reference_window.start,end:ref25Doc.reference_window.end};
  const ref25Html=application('?view=explorer&chart=transport_rejections&range=24h',ref25Doc).app.markup();
  assert.match(ref25Html,/class="stat elevated"[\s\S]*?<span class="percentile elevated">95th percentile<\/span>/,'25-window current reference uses 20-window elevated threshold');
  const allUnverifiedDoc=structuredClone(v7NonMidnight);
  allUnverifiedDoc.versions={...allUnverifiedDoc.versions,native_total:240,runtime_verified_total:0,runtime_unverified_relays:240,verified_runtime:[]};
  const unvOvHtml=application('?view=overview',allUnverifiedDoc).app.markup();
  assert.match(unvOvHtml,/Fleet builds<\/span><span class="fleet-strip-value">Unverified 240<\/span>/);
  const unvFixesHtml=application('?view=fixes',allUnverifiedDoc).app.markup();
  assert.match(unvFixesHtml,/240 relays total · 0 verified · 240 unverified[\s\S]*?Version not recorded[\s\S]*?Unverified[\s\S]*?240/);
  assert.match(application('?view=methods',v7NonMidnight).app.markup(),/selecting <b>Last 7 days<\/b> updates Hero KPIs/);

  // 12. Hero mitigated-rate % of peak, all-KPI percentiles (24h & 7d), apples-to-apples rateRank, gauge neutrality, and tooltip peak share.
  assert.ok(latestCss.includes('.hero-kpi-pct')&&latestCss.includes('.hero-kpi-pct-note'),
   'stylesheet defines Hero KPI percentile layout classes');

  // 12a. Mitigated-rate % of peak (1h sampled_hour+values deduplicated overlap & 24h daily), HSDir toggle response, zero/null guards, and tooltip.
  const peakDoc=structuredClone(v7NonMidnight);
  for(const c of peakDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   s.values=s.values.map(([t])=>[t,0]);
   s.hourly_relays=s.hourly_relays.map(([t])=>[t,240]);
   s.hourly_expected_relays=s.hourly_expected_relays.map(([t])=>[t,240]);
   s.history.daily=s.history.daily.map(([t])=>[t,0,240,1,true]);
   if(Array.isArray(s.history.sampled_hour))s.history.sampled_hour=s.history.sampled_hour.map(([t])=>[t,0,240,1,true]);
  }
  const trConn=chart('transport_rejections',peakDoc).series.find(s=>s.key==='conn_rejected');
  const trConc=chart('transport_rejections',peakDoc).series.find(s=>s.key==='concurrent_conn_rejected');
  const hsGet=chart('directory_requests',peakDoc).series.find(s=>s.key==='hs_get');
  const overlapHr=trConn.values[0][0];
  trConn.values[23][1]=30;trConn.values[0][1]=50;trConc.values[0][1]=50;
  trConn.history.sampled_hour=[[overlapHr,60,240,1,true],[peakDoc.reference_window.start+21600,120,240,1,true]];
  trConc.history.sampled_hour=[[overlapHr,50,240,1,true],[peakDoc.reference_window.start+21600,0,240,1,true]];
  hsGet.values[23][1]=270;hsGet.history.sampled_hour=[[peakDoc.reference_window.start+21600,480,240,1,true]];
  const peak24=ui.prepareHeroPlot(peakDoc,'24h',false),peak24Hs=ui.prepareHeroPlot(peakDoc,'24h',true);
  assert.equal(peak24.ratePeak,120,'1h ratePeak overlays values onto sampled_hour without double-counting overlap (50+50=100 < 120)');
  assert.equal(peak24Hs.ratePeak,600,'1h ratePeak includes hs_get+hs_post when hero_hsdir=1 (120+480=600)');
  const peakDayEnd=Math.floor(peakDoc.end/86400)*86400;
  trConn.history.daily=[[peakDayEnd-2*86400,200,240,1,true],[peakDayEnd-86400,100,240,1,true],[peakDayEnd,20,240,1,true]];
  trConc.history.daily=[[peakDayEnd-2*86400,0,240,1,true],[peakDayEnd-86400,0,240,1,true],[peakDayEnd,5,240,1,true]];
  hsGet.history.daily=[[peakDayEnd-2*86400,800,240,1,true],[peakDayEnd-86400,0,240,1,true],[peakDayEnd,75,240,1,true]];
  assert.equal(ui.prepareHeroPlot(peakDoc,'7d',false).ratePeak,200);
  assert.equal(ui.prepareHeroPlot(peakDoc,'7d',true).ratePeak,1000);
  const peak24Html=application('?view=overview',peakDoc).app.markup();
  assert.match(peak24Html,/Latest 1h mitigated rate<\/div><div class="hero-kpi-value neutral">30 <small>events\/s<\/small><\/div><div class="hero-kpi-meta">25% of 120 peak \(1h record\)<\/div><div class="hero-kpi-pct">/);
  const peak24HsHtml=application('?view=overview&hero_hsdir=1',peakDoc).app.markup();
  assert.match(peak24HsHtml,/Latest 1h mitigated &amp; flood rate<\/div><div class="hero-kpi-value neutral">300 <small>events\/s<\/small><\/div><div class="hero-kpi-meta">50% of 600 peak \(1h record\)<\/div><div class="hero-kpi-pct">/);
  const peak7Html=application('?view=overview&hero_range=7d',peakDoc).app.markup();
  assert.match(peak7Html,/Latest UTC day mitigated rate<\/div><div class="hero-kpi-value neutral">25 <small>events\/s<\/small><\/div><div class="hero-kpi-meta">12\.5% of 200 peak \(24h record\)<\/div><div class="hero-kpi-pct">/);
  const peak7HsHtml=application('?view=overview&hero_range=7d&hero_hsdir=1',peakDoc).app.markup();
  assert.match(peak7HsHtml,/Latest UTC day mitigated &amp; flood rate<\/div><div class="hero-kpi-value neutral">100 <small>events\/s<\/small><\/div><div class="hero-kpi-meta">10% of 1,000 peak \(24h record\)<\/div><div class="hero-kpi-pct">/);
  assert.match(inspectHero(peakDoc,'24h',['End']).steps[0].tooltipHtml,/Total active rate<\/span><span>300 events\/s \(50% of peak\)<\/span>/);
  const partialPeakDoc=structuredClone(peakDoc);
  for(const row of chart('directory_rejections',partialPeakDoc).series[0].history.daily){if(row[0]===peakDayEnd){row[1]=null;row[4]=false;}}
  assert.match(inspectHero(partialPeakDoc,'all',['End']).steps[0].tooltipHtml,/Total active rate<\/span><span>[^<]*\(\d+(?:\.\d+)?% of peak\) \(partial coverage\)/);
  assert.doesNotMatch(application('?view=overview',allZeroRateData).app.markup(),/0 peak \(/);
  assert.doesNotMatch(empty24Kpi,/class="hero-kpi-pct"|peak \(1h record\)/);

  // 12b. Apples-to-apples rateRank: late-introduced hs_get compares only against the 25 matching days, preventing false >=90th alert inflation.
  const a2aDoc=structuredClone(v7NonMidnight);
  const a2aDayEnd=Math.floor(a2aDoc.end/86400)*86400;
  const a2aRefEnd=a2aDayEnd-86400,a2aRefStart=a2aRefEnd-225*86400;
  a2aDoc.reference_window={...a2aDoc.reference_window,start:a2aRefStart,end:a2aRefEnd};
  for(const c of a2aDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   if(s.key==='hs_get'){
    s.history.daily=Array.from({length:25},(_,k)=>[a2aRefEnd-(24-k)*86400,4500+k*100,100,1,true]);
    s.history.daily.push([a2aDayEnd,5500,100,1,true]);
   }else if(s.key==='conn_rejected'){
    s.history.daily=Array.from({length:225},(_,k)=>[a2aRefEnd-(224-k)*86400,k<200?100:500,100,1,true]);
    s.history.daily.push([a2aDayEnd,500,100,1,true]);
   }else{
    s.history.daily=Array.from({length:225},(_,k)=>[a2aRefEnd-(224-k)*86400,0,100,1,true]);
    s.history.daily.push([a2aDayEnd,0,100,1,true]);
   }
  }
  const a2aHs=ui.prepareHeroPlot(a2aDoc,'7d',true),a2aNoHs=ui.prepareHeroPlot(a2aDoc,'7d',false);
  assert.equal(a2aHs.rateRank.n,25,'hero_hsdir=1 restricts rateRank baseline to the 25 days with hs_get');
  assert.equal(a2aHs.rateRank.rank,42,'5500+500 ranks at 42nd percentile across the 25 matching days (10 less + 0.5*1 tie / 25), not falsely >=90th');
  assert.equal(a2aNoHs.rateRank.n,225,'hero_hsdir=0 retains full 225-day matching baseline');
  assert.ok(a2aNoHs.rateRank.rank>90,'500 vs 200 days at 100 and 24 ties at 500 ranks >90th independently');
  const a2aHsHtml=application('?view=overview&hero_range=7d&hero_hsdir=1',a2aDoc).app.markup();
  assert.match(a2aHsHtml,/42nd percentile/);
  assert.doesNotMatch(a2aHsHtml,/class="percentile (elevated|high)"[^>]*>[^<]*percentile<\/span>/);

  // 12c. Gauge percentiles stay neutral even at 100th; rateRank uses elevated/high/unknown/flat appropriately; non-midnight vs midnight 7d gauges.
  const stateHeroDoc=structuredClone(v7NonMidnight);
  const ocState=chart('open_circuits',stateHeroDoc).series[0],dwState=chart('directory_waiting',stateHeroDoc).series[0];
  ocState.reference.hour={n:200,current_percentile:100,flat_zero:false,mean_per_relay:100,p10_per_relay:50,p50_per_relay:100,p90_per_relay:150,start:stateHeroDoc.reference_window.start,end:stateHeroDoc.reference_window.end};
  dwState.reference.hour={n:200,current_percentile:99,flat_zero:false,mean_per_relay:10,p10_per_relay:5,p50_per_relay:10,p90_per_relay:15,start:stateHeroDoc.reference_window.start,end:stateHeroDoc.reference_window.end};
  for(const c of stateHeroDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   if(s.key==='conn_rejected'){
    s.values=s.values.map(([t],i)=>[t,i===23?9999:10]);
    s.history.sampled_hour=Array.from({length:120},(_,k)=>[stateHeroDoc.reference_window.start+(k+1)*21600,k+1,240,1,true]);
   }else{
    s.values=s.values.map(([t])=>[t,0]);
    if(Array.isArray(s.history.sampled_hour))s.history.sampled_hour=Array.from({length:120},(_,k)=>[stateHeroDoc.reference_window.start+(k+1)*21600,0,240,1,true]);
   }
  }
  const highHeroHtml=application('?view=overview',stateHeroDoc).app.markup();
  assert.equal((highHeroHtml.match(/class="hero-kpi-pct"/g)||[]).length,3,'all 3 numeric hero KPIs render percentile badges in 24h');
  assert.match(highHeroHtml,/Latest 1h mitigated rate[\s\S]*?<div class="hero-kpi-value neutral">9,999 <small>events\/s<\/small><\/div>[\s\S]*?<span class="percentile high"[^>]*>100th percentile<\/span>/);
  assert.match(highHeroHtml,/Open circuits \(1h mean\)[\s\S]*?<span class="percentile neutral"[^>]*>100th percentile<\/span>/,'open circuits stays neutral at 100th');
  assert.match(highHeroHtml,/Stalled BEGIN_DIR \(1h mean\)[\s\S]*?<span class="percentile neutral"[^>]*>99th percentile<\/span>/,'stalled BEGIN_DIR stays neutral at 99th');
  // 96th percentile with n=25 -> .elevated.
  for(const c of stateHeroDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   if(s.key==='conn_rejected'){
    s.values=s.values.map(([t],i)=>[t,i===23?24.5:1]);
    s.history.sampled_hour=Array.from({length:25},(_,k)=>[stateHeroDoc.reference_window.start+(k+1)*21600,k+1,240,1,true]);
   }
  }
  const elevHeroHtml=application('?view=overview',stateHeroDoc).app.markup();
  assert.match(elevHeroHtml,/Latest 1h mitigated rate[\s\S]*?<span class="percentile elevated"[^>]*>96th percentile<\/span>/);
  // Small baseline (n=10 < 20) uses .unknown + (10h) note.
  for(const c of stateHeroDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   if(s.key==='conn_rejected')s.history.sampled_hour=Array.from({length:10},(_,k)=>[stateHeroDoc.reference_window.start+(k+1)*21600,k+1,240,1,true]);
  }
  const shortHeroHtml=application('?view=overview',stateHeroDoc).app.markup();
  assert.match(shortHeroHtml,/Latest 1h mitigated rate[\s\S]*?<span class="percentile unknown"[^>]*>100th percentile<\/span> <span class="hero-kpi-pct-note">\(10h\)<\/span>/);
  // Flat zero reference renders Percentile — (.flat).
  for(const c of stateHeroDoc.charts)if(c.kind.startsWith('counter_'))for(const s of c.series){
   s.values=s.values.map(([t])=>[t,0]);
   if(Array.isArray(s.history.sampled_hour))s.history.sampled_hour=Array.from({length:40},(_,k)=>[stateHeroDoc.reference_window.start+(k+1)*21600,0,240,1,true]);
  }
  assert.match(application('?view=overview',stateHeroDoc).app.markup(),/Latest 1h mitigated rate[\s\S]*?<span class="percentile flat"[^>]*>Percentile —<\/span>/);
  // Non-midnight 7d gauge ranks completed UTC day row rather than rolling current.day; midnight 7d uses reference.day.
  const nmGaugeDoc=structuredClone(v7NonMidnight);
  const nmDayT=Math.floor(nmGaugeDoc.end/86400)*86400;
  const nmOc=chart('open_circuits',nmGaugeDoc).series[0];
  nmOc.history.daily=Array.from({length:26},(_,k)=>{
   const t=nmGaugeDoc.reference_window.start+(k+1)*86400;
   return [k<25?t:nmDayT,k===25?250:((k+1)*100),100,1,true];
  });
  nmOc.current.day={value:999999,relays:100,expected_relays:100,coverage:1,valid:true};
  nmOc.reference.day={n:100,current_percentile:99,flat_zero:false,mean_per_relay:10,p10_per_relay:5,p50_per_relay:10,p90_per_relay:15,start:nmGaugeDoc.reference_window.start,end:nmGaugeDoc.reference_window.end};
  assert.equal(ui.prepareHeroPlot(nmGaugeDoc,'7d',false).gauges.openCircuits.latestRank.rank,8);
  const nmGaugeHtml=application('?view=overview&hero_range=7d',nmGaugeDoc).app.markup();
  assert.equal((nmGaugeHtml.match(/class="hero-kpi-pct"/g)||[]).length,3,'all 3 numeric hero KPIs render percentile badges in 7d');
  assert.match(nmGaugeHtml,/Open circuits \(UTC day mean\)[\s\S]*?<div class="hero-kpi-value neutral">250 <small>circuits<\/small><\/div>[\s\S]*?<span class="percentile neutral"[^>]*>8th percentile<\/span>/);
  const midGaugeDoc=structuredClone(nmGaugeDoc);
  midGaugeDoc.end=nmDayT;midGaugeDoc.start=nmDayT-86400;
  assert.match(application('?view=overview&hero_range=7d',midGaugeDoc).app.markup(),/Open circuits \(24h mean\)[\s\S]*?<span class="percentile neutral"[^>]*>99th percentile<\/span>/);
 }finally{
  globalThis.setTimeout=origSetTimeout;
 }
 console.log('Display checks passed: source selectors, state/controllers, both layouts and global ranges across six families, legacy migration/history, units/tables, bands/gaps, finite extremes, encoded metadata, modified links, failed startup, adaptive X-axis ticks, keyboard inspection, and frontend hardening.');
})().catch(error=>{
 console.error(error);
 process.exit(1);
});
