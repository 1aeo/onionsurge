# Verified live releases

The default build remains a saved, offline snapshot. A live build uses the same
charts and global time-range and layout controls, but obtains its measurements from one
verified immutable release.

```sh
python3 scripts/build.py --live --input /path/to/public-snapshot.json --output dist-live
```

`build-manifest.json` keeps the `onionsurge-build-v1` format and declares
`mode: "live"` or `mode: "offline"`. Live output includes the normal data,
reference and CSV artifacts for the publisher. Its `data.json` is compact JSON.
The live HTML sets `data-mode="live"`, loads `app.js` followed by `live.js`, and
never executes the saved `data.js`. Production frontend staging selects only
`index.html`, `app.js`, `live.js` and `style.css`; data publication is separate.
The offline packager rejects a live build before creating a new reservation.

## Public protocol

All requests stay on the page's origin beneath the relative `data/` prefix.
No provider address, credential or monitoring endpoint is supplied by the page,
query string or release manifest. The loader omits credentials and rejects
redirects.

`data/manifest.json` has exactly these fields:

```json
{
  "format": "onionsurge-root-v1",
  "updated_at": 1794975000,
  "current": {"id": "release-A", "manifest_sha256": "<64 lowercase hex>", "manifest_bytes": 4096},
  "previous": {"id": "release-B", "manifest_sha256": "<64 lowercase hex>", "manifest_bytes": 4096}
}
```

The hashes and lengths above are illustrative. `previous` can be `null` for the
first release. IDs are single path components using letters, digits, underscores,
hyphens and dots, beginning with a letter or digit and limited to 96 characters.
The current and previous IDs must differ. Times are integer Unix seconds in UTC.

Each descriptor names `data/releases/<id>/manifest.json`. Its exact bytes and
SHA-256 must match before the loader parses it. The immutable manifest contains
exactly:

| Field | Meaning |
| --- | --- |
| `format` | `onionsurge-release-v1` |
| `id` | The selected root descriptor's ID |
| `data_schema_version` | `6` (`5` retained only for `root.previous` cold fallback) |
| `observation_end` | Completed `00:00:00 UTC` day (`observation_end % 86400 == 0`), matching `data.end` |
| `generated_at` | Release generation time, at or after observation end |
| `application_commit`, `deployment_commit` | Full public commit hashes |
| `query_contract_sha256` | SHA-256 of `catalogue/contract.json`, emitted by `scripts/build.py` into `data.json['provenance']` and `build-manifest.json`, and verified across release metadata and `data.json` when present |
| `objects` | Relative filename to `{ "sha256": ..., "bytes": ... }` (max `512` entries) |

The object set is exact: `data.json`, the data's `reference_document`, and
`exports/<chart-id>-<export_suffix>.csv` for every chart and configured period (`7d`, `1mo`, `3mo`, `6mo`, `12mo`, `all-time` by default, producing `80` objects for `13` charts × `6` periods; legacy `v5` `root.previous` fallback uses `30d` and `all-time`).
Arbitrary URLs, extra object paths, encoded separators and traversal paths are
not accepted (`-24h.csv`, `-1d.csv`, and `-[0-9]+[smh].csv`, including hyphenated chart suffixes, are explicitly rejected). The publisher hashes the final bytes it uploads.

The data uses schema `6` (`time_ranges` validated against `inputs/time_ranges.json` invariants; schema `5` `root.previous` maps to legacy `30d`/`all` periods), strict `86,400`-second `00:00 UTC` observation alignment
(`start`, `end`, `window_seconds: 86400`), deterministic `dir-spec` coarsening, and
rejection of all `< 24h` / hourly keys (`hour`, `sampled_hour`, `hourly_relays`,
`hourly_expected_relays`, `current24`, and `series.values`). Its reference ID,
full hash, document path and `day` window must agree with the verified reference;
a declared method identity must match. The reference's source snapshot can match
or differ in input hash (`references` are frozen daily at `00:00 UTC`). Observation
gaps and null values remain data, rather than causing an older measurement to be
inserted into the selected release.
An observed zero-relay current cohort with null values and invalid-current flags
is admissible; retained history and its positive expected-population context stay
intact. A genuine empty observation is not a failed release fetch.

`series.relay_population` (optional) and derived `series.relay_counts` (`day` plus each period's `summary_key`, by default `seven_days`, `one_month`, `three_months`, `six_months`, `twelve_months`, and `all_time`) retain the
same schema version and coarsened multiple-of-8 count invariants (`0` or `>= 8`).
Their closed fields and counting rules are documented in [the input contract](input-format.md#relay-populations-for-each-statistic).
The browser checks exact row shapes, safe integer counts, coarsened counts, matching measurement
timestamps and eligible totals, classification bounds and nonoverlap of
Middle-only with the other flags. Complete evidence means all five last reported
flags have consistent binary values. Agreeing binary duplicates are accepted;
missing, conflicting or nonbinary records remain unknown. `Running=0` is recorded evidence. Middle-only
alone requires `Running=1` plus four zero role flags. Recorded all-zero flags do
not establish Middle-only membership or independently verified consensus status.

Every supplied summary is reconciled with its raw evidence and contributing
measurement windows (`days > 0` and `coverage >= period.minimum_coverage`, `0` by default for schema-6 periods, accepting `0 <= minimum_coverage <= 1` and rejecting `-0.0`; schema-5 rollback periods retain `0.9`). Historical population means use the same qualified days as
the corresponding existing historical average, with equal day weights. Counts
and coverage cannot silently select only days with complete flag records.
Missing evidence leaves exact role means null. With some classified contributors,
an available statistic also supplies `role_bounds`: each role's known summed
count and that count plus all unclassified relay-windows, divided by the same
contributing-window count. The loader verifies both endpoints against the raw
evidence; inward shifts or invented ranges are rejected. No day is dropped from
the denominator. Complete or wholly unknown evidence has null bounds, as do
empty populations and unavailable historical statistics. These are missing-data
bounds, not confidence intervals. An insufficient historical metric has null
population means while retaining measured-window and coverage diagnostics.
A known current zero population has zero role counts only with a matching,
explicit empty-set record. Optional population-method digests must be lowercase
SHA-256 values. Validation sums counts in bounded scans of the existing
historical rows; it does not recompute chart values or reference statistics.

Historical-average panels (each configured period's `summary_key`) include a `comparison` object for the
current 24-hour UTC percentile within their daily history. The loader checks its
closed fields, fixed method, calendar/day window (including `comparison_lag_days`), nonoverlap cutoff (`end - 86400`), exact daily
membership and interval bounds, then recomputes the midrank in one linear scan.
It uses each day's own eligible relay count and the current day's eligible
count. The parent average must be available (`days > 0` and `coverage >= period.minimum_coverage`) and the current 24-hour measurement
must qualify; otherwise the rank is null. The all-zero case has no rank marker.
See [the comparison contract](input-format.md#current-24-hour-position-within-historical-periods).
This addition does not change the frozen 18-month reference or the release's
object set. The displayed **18-month average** continues to mean the historical
per-relay average scaled to the reporting-relay count of the current `24-hour`
UTC window.

## Verification and refresh

The loader checks the root, the selected immutable manifest, data JSON, reference
and every configured CSV (`-7d.csv`, `-1mo.csv`, `-3mo.csv`, `-6mo.csv`, `-12mo.csv`, and `-all-time.csv` by default) before displaying a new release. It checks byte counts, SHA-256,
supported schemas (`schema_version === 6`, or `5` for `root.previous` fallback) and renderer relationships. Downloads use Blob URLs made from
those verified bytes, with the release ID in the filename. Hashes provide
integrity under the trusted HTTPS origin; they are not independent signatures or
a substitute for the publisher's public-data privacy checks.

JSON is decoded with fatal UTF-8 handling, parsed natively, then checked across
the complete tree in own-key postorder. A cursor stack retains one frame per
open container, validates primitive values immediately, and iterates dense
JSON arrays by numeric index. It does not allocate a pending frame or key string
for every primitive sibling; auxiliary traversal storage consists of the open
container frames and object-key lists. Reserved keys (including escaped forms)
and nonfinite numbers are still rejected; duplicate-key parsing retains native
last-value behavior. Container nesting has an explicit limit of 256 levels:
the root object/array counts as one, while primitive leaves add no level. This
deterministic bound replaces reliance on an engine's accidental reviver stack
limit; equivalence is not claimed for arbitrarily deep JSON.

When the root selects a new release, the loader still fetches and verifies its
manifest first. An object may then reuse bytes from the active verified bundle
only when its relative path, SHA-256 and byte count all match the new manifest.
It copies those bytes into a new buffer and rehashes that snapshot before use.
A damaged or incomplete cache entry is fetched from the new release's own URL;
if that fetch fails verification, the new candidate fails as a whole. Identical
content at a different path does not qualify for reuse.

Reused objects count toward the same complete-candidate budget and pass the same
data/reference checks as downloaded objects. Every admitted release still has
all of its declared objects, and receives fresh Blob download resources and its
own release ID. The publisher remains responsible for retaining those objects
at their immutable URLs. Only the active bundle is a reuse source: there is no
global, persistent or unbounded object cache, and a failed cold candidate cannot
donate partial results to the previous-release fallback. An unchanged current
descriptor retains the existing one-root-request fast path.

On a cold load, a failed current candidate causes one complete attempt at the
root's previous release. If neither succeeds, the page shows unavailable. During
refresh, the already verified page remains visible until the next complete
candidate is ready. Verification or rendering failure retains that page. A root
change cannot retarget requests already pinned to a candidate ID.

Visible pages check for updates every `3,600` seconds (`1` hour), and on return to the tab when
that interval has elapsed. Only one refresh runs at a time. A successful swap
updates measurements and download resources together, preserving view, global
time range (`7d`, `1mo`, `3mo`, `6mo`, `12mo`, or `all` by default), overview layout, selected series, band state, scroll position and focus when the selected
control still exists. It does not register another renderer. Blob resources for
the active and immediately previous view are retained; older resources are
retired on the next successful swap, and all are released when the page closes.

The header shows observation and generation times separately. It labels a cold
fallback as a previous verified release, and labels data delayed once observation
age exceeds `108,000` seconds (`30` hours, i.e., more than 6 hours past the next `00:00 UTC` daily release). A failed refresh retains the original timestamps.
Freshness text updates once per minute without redrawing charts. The saved
snapshot's `generated_at` is not relabeled as the live publication timestamp.

| Bound | Limit |
| --- | ---: |
| Root manifest | 64 KiB |
| Immutable manifest | 128 KiB |
| Individual data/reference/CSV object | 8 MiB |
| Declared candidate objects combined | 32 MiB |
| JSON object/array nesting | 256 levels |
| Parallel object requests | 6 |
| Request deadline, including body and hash | 25 seconds |
| Candidate deadline | 60 seconds |

Actual decoded bytes enforce limits; `Content-Length` does not override them.
The request deadline permits the Worker's cache lookup and both provider attempts.
Explicit gzip-chunk loading is not part of this initial protocol. Oversized
releases must be partitioned in a future schema rather than truncating history.

`make check` covers complete and failed releases, previous-release fallback,
same-origin object binding, manifest changes during a load,
content-identity reuse, corrupt-cache refetch, copied-buffer ownership,
bounded requests, visible refresh, retained resources and renderer rollback.
Offline previews keep their existing local behavior and make no live requests.
