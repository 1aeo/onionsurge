# Aggregate input contract

`inputs/snapshot.json` is the repository's synthetic schema-4 (`input_schema_version: 4`) example measurement bundle (production deployments configure a private `0600` seed snapshot via `SOURCE_SEED_SNAPSHOT` or `--seed`; `collect.py` also accepts legacy schema-3 seeds via `allow_v3_seed=True`), paired with declarative range definitions in `inputs/time_ranges.json` (or `--time-ranges <path>`).
The builder accepts only the public fields described by `scripts/validation.py` and `scripts/time_ranges.py` and emits `schema_version: 7` with the validated `time_ranges` block embedded.
Computed reference distributions, ranks, display strings and historical averages
are produced by the build, not supplied by the collector.

- `start` and `end` are completed UTC-hour Unix seconds (`end % 3600 == 0`), exactly 24 hours (`86,400` seconds) apart. `window_seconds` is `3600`, hourly `expected_samples` is `30` with `minimum_samples: 24`, and daily windows (`86,400s` `[1d]`) gate at `>= 360 / 720` samples.
- Six `families` partition the charts and identify their primary chart and series.
- Each chart includes exact hourly `[1h]` PromQL (`query`, `quality_query`, and `history_queries.{daily,sampled_hour,current24}.{value,eligible,available}`), units, source kind (`counter_hour_rate` or `gauge_hour_mean`), display scale and declared relay/build cohort.
  Primary, quality, historical, and flag-probe queries must contain non-whitespace
  text. Validation preserves their original text for reproduction and hashing.
- Each series supplies `24` completed hourly `[t, value]` rows (`values`), `hourly_relays`, `hourly_expected_relays`, `current.{hour, day}`, and `history.{daily, sampled_hour}` (`sampled_hour` aligned to `21,600s` `6h` grid points):
  both `current.hour` and `current.day` record `{value, relays, expected_relays, coverage, valid}`.
  The collector supplies `expected_relays` as the larger of that cohort's
  available population at `end` and the preceding 31-day daily (32-day hourly) maximum eligible
  population (including `end`). It is positive and at least the reported count.
  Earlier expected populations need not equal today's cohort.
  A point needs at least 90% of its own expected population (`coverage >= 0.9`);
  otherwise `value` is `null` and `valid` is `false`. `current.hour` agrees with the final `values[23]` entry; when `history.daily` includes `end`, `current.day` agrees with that final daily row within coarsening bounds.
  A successfully queried but currently absent cohort may have `relays: 0`.
  Its current measurements remain unavailable with zero eligible relays and a
  positive retained qualification denominator; past observations are preserved.
- Historical rows are `[interval_end, fleet_value, reporting_relays, coverage, valid]`.
  All `daily` rows end at `00:00:00 UTC` (`interval_end % 86400 == 0`) with strictly increasing timestamps at least `86,400` seconds apart; `sampled_hour` rows end on `21,600s` UTC boundaries with strictly increasing timestamps at least `21,600` seconds apart.
  Qualification requires at least 90% population coverage (`coverage >= 0.9`). Missing rows and
  failed qualification are not zero observations.
- Chart IDs and series keys are stable slugs. Displayed series require historical
  observations. Auxiliary unplotted classes may have empty `history.daily` and `history.sampled_hour` arrays (`[]`)
  with a non-empty `history_status` explanation.
- Deterministic `dir-spec` coarsening (`scripts/coarsening.py`) is enforced across all input counts and measurements:
  standalone public relay, flag, cohort, and `dircache_cohort` counts `< 8` are floored to `0` and `>= 8` are rounded to the nearest multiple of `8` (preserving `classified <= total` and `middle_only + role <= classified`), while partitioned build, cohort, and `middle_only`/non-middle role breakdowns distribute multiples of `8` by largest remainder to sum to the coarsened total (so individual parts may differ from nearest-multiple rounding); `open_circuits` means round to nearest `100` circuits, then to `3` significant digits; `hsdir_fetches`, `hsdir_uploads`, `introduction_rejections`, and `directory_requests` `hs_get`/`hs_post` bin to `0.001/s`, then round to `3` significant digits; other counter series suppress `< 8` events/window, bin to `8`-event increments, and round rates to `3` significant digits; and non-sensitive gauge means (`directory_waiting`) floor `< 0.05` to `0`, quantize `[0.05, 1)` to `0.1`, and round `>= 1` to `3` significant digits.
- `versions.metric_cohorts` and `versions.cohort_labels` define build counts and labels
  once. The cohort keys are exactly `native`, `custom`, and `directory_defense`.
  Each chart selects a `cohort_key`; generated build descriptions use that mapping.
- Optional `versions.runtime_resolution.source_sha256` uses only the public labels
  `continuity`, `daemon_group_a`, `daemon_group_b`, and `package_lineage`, with
  lowercase 64-character SHA-256 digests as values.

Public metadata maps reject unknown fields and nested notes:

- `product` contains text fields: `name`, `application_repo`, `deployment_repo`,
  `route`, `name_status`, `alternate_url`, and `routing_status`.
- `history_methods` contains the fixed five-name `row_fields` array described above
  and text fields `counter`, `gauge`, `sampling`, `coverage`, `custom_directory`,
  `baseline`, `reference_line`, `percentile`, `reference_exclusion`, `first_window`,
  `source_scope`, `reference_refresh`, `historical_averages`, and `coarsening`.
- `flags` maps `Authority`, `Exit`, `Fast`, `Guard`, `HSDir`, `Running`, `Stable`,
  `Sybil`, and `V2Dir` to coarsened nonnegative integer counts (`0` or `>= 8` and divisible by `8`).
- `snapshot_flags` supports `flags`, `targets`, and `up` probes. Each contains a
  text `query` and a successful Prometheus vector `response`. Vector rows contain
  only `metric` and `[timestamp, count-string]` `value` pairs. `flags` metrics have
  only a recognized `type` label; target/up aggregate metrics have no labels.
  Snapshot probe values and timestamps align to the snapshot `end` UTC hour (`end % 3600 == 0`).
- `dircache_cohort` contains four coarsened nonnegative integer counts:
  `configured_nonexit_zero`, `observed_controlport_zero`,
  `file_config_fallback_zero`, and `file0_observed1_conflicts_excluded`, plus text
  `method` and `source` fields. No relationships between these counts are inferred.

`generated_at` and optional series `history_status` are text scalars; optional
chart `introduced_note` is text or `null`.

The snapshot contains only coarsened fleet aggregates. Do not put relay identities, host
inventories, credentials, `< 1h` slices, raw process records or arbitrary extra metadata in it.

The build records the exact input-byte hash, coarsening method hash, `time_ranges_sha256`, and `time_ranges_method_sha256`. Daily reference caching is keyed by
UTC date, queries, series, qualification rules and statistical implementation.
`--refresh-reference` explicitly regenerates the reference for a corrected input.
Generated CSV files (`exports/<chart-id>-<export_suffix>.csv`, `91` files across `13` charts × `7` default periods: `24h`, `7d`, `1mo`, `3mo`, `6mo`, `12mo`, `all-time`) include every recorded series and retain coarsened unscaled source units (`interval_end_utc,series,value,unit`): `24h` exports the 24 completed hourly rows `[data.end - 86400 + 3600 .. data.end]`, while daily exports retain rows with `period_start < interval_end <= day_end` (or `interval_end <= day_end` for `all_time`).

## Configurable time ranges (`inputs/time_ranges.json`)

`inputs/time_ranges.json` uses a closed schema with exact top-level keys `version` (`1`), `default_range`, `legacy_aliases`, `card_slots` (`fallback_dynamic` and two distinct `fixed` IDs, with `fallback_dynamic` outside `fixed`), and `periods` (`1`–`16` entries, exactly one `window.unit == "all_time"` and at most one `window.unit == "hours"`):

- Each period defines `id`, `summary_key` (disallowing `day`, `hour`, `sampled_hour`, `hourly_relays`, `hourly_expected_relays`, `current24`), `export_suffix` (max `32` chars), `label`, `average_label`, `plot_label`, `window` (`unit`, `count`, `comparison_lag_days`), `minimum_coverage`, and `minimum_comparison_windows` (`1`–`10000`).
- `< 24h` (`0h`–`23h`, `1d`, `[0-9]+[sm]`, or trailing `-24h` tokens on multi-token slugs) period IDs and export suffixes are rejected by `scripts/time_ranges.py`; `hours` is permitted exclusively as `id: "24h"`, `export_suffix: "24h"`, `window.count: 24`, `comparison_lag_days: 0`, and `minimum_coverage: 0` (requiring `24h` inside `card_slots.fixed`), while `days`, `months`, and `all_time` still reject `24h`.
- `days` windows require integer `2 <= count <= 36500` and `comparison_lag_days` in `{0, 1}`; `months` windows require integer `1 <= count <= 1200` and `comparison_lag_days` in `{0, 1}` (using UTC calendar-month subtraction with month-end clamping); `all_time` requires `count: null`, `comparison_lag_days: 0`, and `minimum_coverage: 0` (bounded daily periods accept `0 <= minimum_coverage <= 1` and reject `-0.0`; `0` uses all available qualifying `>= 1` days in the window instead of gating below a fixed coverage ratio, while `0`-day series remain `insufficient_data`).
- Default periods in display order: `24h` (`twenty_four_hours`, `24` hours, `lag=0`, `min_coverage=0`, `min_comparison_windows=30`, paired with `default_range: "24h"`, `card_slots: {fallback_dynamic: "6mo", fixed: ["24h", "all"]}`, and `legacy_aliases: {"30d": "1mo", "1m": "1mo", "1y": "12mo"}`), `7d` (`seven_days`, `7` days, `lag=1`, `0`, `6`), `1mo` (`one_month`, `1` calendar month, `lag=0`, `0`, `25`), `3mo` (`three_months`, `3` months, `lag=0`, `0`, `30`), `6mo` (`six_months`, `6` months, `lag=0`, `0`, `30`), `12mo` (`twelve_months`, `12` months, `lag=0`, `0`, `30`), and `all` (`all_time`, `all-time`, `0`, `30`). Changing periods only requires editing `inputs/time_ranges.json` (or passing `--time-ranges`).

## Relay populations for each statistic

A series may include `relay_population` with exactly these fields:

```json
{
  "method": "eligible-last-flags-v1",
  "queries": {"hour": "<exact aggregate PromQL>", "day": "<exact aggregate PromQL>"},
  "hour": [1794960000, 104, 104, 32, 16, 0, 48, 80],
  "day": [1794960000, 104, 104, 32, 16, 0, 48, 80],
  "daily": []
}
```

Each row is `[interval_end, total, classified, guard, middle_only, exit, hsdir,
v2dir]`. All cells are coarsened finite nonnegative integers (`0` or `>= 8` and divisible by `8`, with `classified <= total` and `middle_only + max(guard, exit, hsdir, v2dir) <= classified`). Current `hour` and `day` rows end at the
snapshot's `end` UTC hour; historical `daily` rows end at UTC midnight and have unique increasing
timestamps. Every present row must match the coarsened eligible relay count of that
series and measurement window. A daily population row requires an existing
`history.daily` row. The collector uses `null` for unavailable current metadata
and omits unavailable daily rows; it must not publish a contradictory total or
replace an unavailable count with zero.

The query first selects the metric's eligible contributors, including any
metric-specific qualification gate. It classifies a contributor only when the
last recorded `Running`, `Guard`, `Exit`, `HSDir` and `V2Dir` flags within that
1-hour or 24-hour UTC window are all present with consistent binary values. Duplicate
records are accepted only when every value agrees and is binary; missing,
conflicting or nonbinary records remain unclassified. `Running=0` is a recorded
value, not missing evidence. Guard, Exit, HSDir and V2Dir count
their respective recorded positive flags in this same set. `middle_only` means
`Running=1` and `Guard=Exit=HSDir=V2Dir=0`; its label is **Middle-only (flags)**. It does not assert
historical `DirCache=0`. These are last recorded roles within the window, not
attack attribution or independently verified consensus membership. Tor may
report all flags as zero when its consensus entry is absent; those recorded
zeros never establish Middle-only membership without `Running=1`.
Guard, Exit, HSDir and V2Dir overlap, so their counts
must not be added to estimate a fleet size. Each count is at most `classified`,
which is at most `total`; Middle-only cannot overlap any other listed role.

The builder retains the raw rows and exact queries, then derives
`relay_counts.hour`, `relay_counts.day`, and one summary per non-hour configured daily period (`seven_days`, `one_month`, `three_months`, `six_months`, `twelve_months`, and `all_time` by default). Each summary contains
`total`, the five role fields above, `role_bounds`, `measured_windows`, `classified_windows`,
`relay_windows`, `classified_relay_windows` and `coverage`:

- Current `hour.total` and `day.total` equal `current.hour.relays` and `current.day.relays` respectively, including a known
  zero even if the metric value is unavailable. Missing eligible counts produce
  `null` totals and roles.
- Historical counts (each configured `summary_key`) are arithmetic means over the **exact same qualifying daily
  rows** as that historical metric average. They describe mean eligible relays
  per contributing day, not unique relays over the period. An insufficient
  bounded period (`days == 0` or `coverage < period.minimum_coverage`, `0` by default) or empty history has `null` total and role means.
- Role means require every contributing window to have matching population
  evidence with `classified=total`. Otherwise all five role means are `null`;
  the builder never shortens the metric's day set to days with known flags.
- For an available statistic with partial flag evidence, `role_bounds` maps each
  of the five role names to `[lower, upper]`. If `D` is the number of contributing
  windows, `K` is that role's summed count among classified contributors and `U`
  is the summed unclassified population, the bounds are `[K/D, (K+U)/D]`.
  Missing metadata for a whole window contributes its entire eligible count to
  `U`. These are deterministic missing-data bounds, not confidence intervals;
  flag groups still overlap. Exact role fields remain `null` when bounds exist.
  `role_bounds` is `null` for complete evidence, no classified contributors,
  insufficient historical statistics and empty populations. No empty-set or
  wholly unknown role counts are inferred. The display rounds the lower bound
  down and upper bound up to whole relays; the JSON retains fractional bounds.
- `measured_windows` counts the contributing windows; `classified_windows`
  counts those with complete classification. `relay_windows` sums their eligible
  counts; `classified_relay_windows` sums the classified counts in matching
  evidence. These diagnostics remain available for an insufficient historical
  metric. `coverage` is the latter sum divided by the former. With no eligible
  relay-windows it is `1` only for explicitly verified empty sets, otherwise
  `null`. Zero classified coverage means no verified role evidence; it does not
  mean zero Guard, Exit or directory relays.

Built releases also record `provenance.relay_population_method_sha256` and `provenance.coarsening_method_sha256`.
Count totals, including summed relay-windows, must fit a JavaScript safe integer
(`0` through `2^53−1`) for live admission. The builder refuses an overflowing
relay-window sum instead of publishing metadata the browser cannot verify.

## Current 24-hour position within historical periods

The builder adds `historical_averages.<summary_key>.comparison` for every non-hour configured daily period
(`seven_days`, `one_month`, `three_months`, `six_months`, `twelve_months`, and `all_time` by default) to each series. These describe the
current independently measured 24-hour UTC value, not the historical average printed
in the panel. Existing average values, calendar windows, day counts and relay
population summaries are preserved.

Each comparison contains exactly `method`, `cutoff`, `n`, `start`, `end`,
`current_percentile` and `flat_zero`. The method is
`daily-per-relay-midrank-v1`. Every comparison keeps only qualifying daily intervals ending at or before
`reference_cutoff`: 00:00 UTC on the day before the snapshot's UTC date (`day_end - 86400`, where `day_end = floor(end / 86400) * 86400`, which excludes `current.day`).
When `comparison_lag_days == 0` (such as `1mo`, `3mo`, `6mo`, `12mo`, and `all`), `period_comparison_rows` starts from the same window start as that historical average (`period_start` or retained history start); exclusion of `current.day` does not add an older day, so the historical average can contain one more day than its rank comparison.
When `comparison_lag_days == 1` (such as `7d`, whose average spans `[day_end - 7d, day_end]`), `period_comparison_rows` shifts the comparison start back by 1 UTC day for `days` windows (`[day_end - (count + 1)d, day_end - 1d]`) so up to `count` non-overlapping prior days remain available; for `months` windows, `calendar_window(day_end, count, lag_days=1)` instead anchors the calendar subtraction at `day_end - 1d` with month-end clamping rather than shifting the start back by 1 UTC day (for example, 1 month ending `2026-05-31` yields `2026-04-30` for both `lag=0` and `lag=1`).

For each included day, divide its fleet value by its own eligible relay count.
Compare the current 24-hour value divided by its current eligible count with
those daily values. Each day has equal weight. The percentile is
`100 × (days below + 0.5 × equal days) / n`, using the same tie rule as the
18-month reference. `start` is the first included interval's start, `end` is the
last included interval's end, and `n` is the number of included intervals; empty
comparisons have null bounds. These fields remain available when the rank is
unavailable.

A rank requires an available parent historical average and a qualifying current
24-hour measurement. Otherwise `current_percentile` is null and `flat_zero` is
false. When the qualifying current value and every comparison value are zero,
the rank is null and `flat_zero` is true, suppressing the marker. No new queries,
quantiles, cache files or reference distributions are introduced. The existing
historical-method provenance digest covers this calculation.
