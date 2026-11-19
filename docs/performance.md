# Chart computation and reuse

OnionSurge adapts the batching and cache patterns in
[Allium at `5649909`](https://github.com/1aeo/allium/tree/5649909628e510562d45108bd15faa890fbd618e).
The renderer remains SVG. Measurements, interval boundaries, coverage gaps,
statistical formulas, colors, controls and complete verified downloads are retained.
Every chart, including an empty chart, credits `Source: 1aeo.com - OnionSurge`.

| Pattern | Allium implementation | OnionSurge adaptation |
| --- | --- | --- |
| Calculate several statistics from one sorted distribution | [`series._percentiles`](https://github.com/1aeo/allium/blob/5649909628e510562d45108bd15faa890fbd618e/allium/lib/charts/series.py#L97) | Sort once for five reference quantiles and use binary searches for the midrank. Preserve the existing interpolation arithmetic and exact mean calculation. |
| Prepare shared time layouts and indexes once | [`series._slots`](https://github.com/1aeo/allium/blob/5649909628e510562d45108bd15faa890fbd618e/allium/lib/charts/series.py#L37), [`ChartWriter`](https://github.com/1aeo/allium/blob/5649909628e510562d45108bd15faa890fbd618e/allium/lib/charts/render.py#L380) | Reuse fixed UTC/number formatters, prepared plots within one release, and UTC timestamp strings within one CSV export build. A release replacement resets plot preparation; a failed replacement restores the old cache. |
| Reuse work only when its inputs match | [`ChartWriter._png`](https://github.com/1aeo/allium/blob/5649909628e510562d45108bd15faa890fbd618e/allium/lib/charts/render.py#L422) | Reuse active-release object bytes only for the same path, SHA-256 and byte count. Copy and rehash them before admitting the new complete release. |

Profiling also identified JSON reviver callbacks as a browser CPU cost. The live
loader now uses native JSON parsing followed by iterative postorder validation,
with one cursor frame per open container instead of one pending frame per value.
UTF-8 validation, reserved-key rejection, finite-number checks and validation
order are retained. Nesting is explicitly bounded at 256 containers; current
data and reference documents have depths of eight and six. Schema and reference
validation still follow parsing. See the [live-release contract](live-releases.md).

Allium's palette quantization, rebinning, uptime padding and incomplete-tail
removal are measurement or image-rendering choices for a different workload.
OnionSurge keeps its existing samples, gaps, direct 24-hour estimates and SVG
geometry. Its 13 charts do not justify Allium's large raster process pools.

## Reproduce the renderer comparison

Build with Python 3.11+ and run the dependency-free Node benchmark:

```sh
make build
node scripts/benchmark_charts.cjs
node scripts/benchmark_charts.cjs --baseline /path/to/baseline-app.cjs
```

The optional baseline is a CommonJS-compatible `site/app.js` with the same UI
contract. `scripts/benchmark_charts.cjs` checks SVG rendering (`svgCases`) and markup rendering (`markupCases`),
including every configured daily time range (`7d`, `1mo`, `3mo`, `6mo`, `12mo`, and `all` by default) in Grid and Full width. Only source-credit
text and the credited empty-chart wrapper are normalized. Other output
differences fail the comparison; a baseline predating intentional UI changes
must be compared separately for chart geometry and measurements.
The script reports source/input hashes and warmed timing distributions without
machine paths or private identifiers. `--data` selects another built data JSON.

These are CPU/string-generation measurements, excluding browser layout, painting
and network latency. Repeated comparisons must use the same machine, Node version
and input; timing thresholds are deliberately absent from CI.

## Historical baseline (`086ece0`)

Recorded earlier with Node 26.5.0, the prior aggregate input and the initial optimization
comparison against `086ece0`. That historical comparison covered the then-current per-family
controls (336 SVG cases and 148 markup cases) before the `30d | all` daily-only migration. Values below are representative
historical medians, not service-level guarantees.

| Work | Before | After |
| --- | ---: | ---: |
| Six All time SVGs | 2.73 ms | 1.08 ms |
| Full All time overview redraw | 3.40 ms | 1.26 ms |
| Complete 28-object admission, in-memory responses | 63.33 ms | 19.47 ms |
| Warm admission of a changed release | 63.51 ms | 19.50 ms |
| Write all build outputs | 67.35 ms | 59.24 ms |

A captured two-release replay reduced requests from 30 to 16 and decoded response
bytes from 3,465,602 to 2,069,592. The 14 reused objects were 13 All time CSVs and
the frozen reference. Their 1,396,010 bytes were 40.3% of that release's object
payload. Reuse varies with the data: a new UTC day or reference generation can
change those objects. Cold loads still obtain every required object.

The backend's sort-once change saved about 3 ms per build. Whole warm preparation
measured 209.20 ms before and 206.13 ms after; it is a small build improvement.
The renderer and loader reductions must not be described as equivalent reductions
in the entire daily collection/publication job.

A constrained-heap regression also checks one million numeric array elements,
including rejection of a nonfinite final element. It keeps wide JSON validation
from allocating a pending stack entry for every primitive value.

Validation compares all computed values, reference distributions and all 26 CSVs
exactly. Implementation fingerprints and their derived reference IDs change when
the statistical source code changes, even though the numerical results agree.
Browser checks cover narrow/wide layouts, source placement, bands, selected series,
focus, missing data and console errors. Integrity failures retain the existing
verified view, including when a reused byte buffer has been corrupted.
