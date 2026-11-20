# OnionSurge

Static telemetry observatory, Prometheus/PromQL query contract & SSH collector, and deterministic offline/live build pipeline for C Tor `MetricsPort` DoS measurements (synthetic example fixture: `240` coarsened reporting relays across `6` families, `13` charts, `37` series / `29` plotted).

Builds either an offline standalone bundle from the synthetic [`inputs/snapshot.json`](docs/input-format.md) fixture (`input_schema_version: 4`, Unix `1794873600`–`1794960000`) or a live frontend bundle (`--live`) for private verified hourly UTC releases (configured via a private `0600` `SOURCE_SEED_SNAPSHOT` / `--seed`), and owns all Tor telemetry source collection/gateway tools (`catalogue/`, `scripts/{collect,query_protocol,query_gateway,install_source,build_catalogue}.py`, `source.env.example`). Layer-3 release staging, private local/VPN or object-storage publication, cron/systemd orchestration, and optional Cloudflare Worker delivery live in [`1aeo/onionsurge-deploy`](https://github.com/1aeo/onionsurge-deploy).

## Build, Live Bundle & Verification

Requires Linux or macOS (`POSIX`), Python `3.11+` (stdlib only), Node.js `22+`, and `make`. Zero runtime dependencies or provider credentials; `build.py` requires zero network access, and non-POSIX hosts fail closed before mutating the filesystem. CI (`make check`) also runs `scripts/build_catalogue.py --check` and evaluates the generated PromQL rules against pinned Prometheus `v3.14.0` `promtool`.

```sh
make check                                      # Build dist/, verify catalogue, run unittest & JS/live UI checks, package previews
make check PYTHON=python3.12 NODE=node          # Override toolchain binaries
make catalogue                                  # Regenerate catalogue/{queries,routes}.json from catalogue/contract.json
make serve                                      # Build dist/ and serve at http://127.0.0.1:8873
make build-live                                 # Build live frontend + publication artifacts in dist-live/
node scripts/benchmark_charts.cjs               # Repeatable SVG/redraw CPU microbenchmark

python3 scripts/collect.py \
  --config "$HOME/.config/onionsurge/source.env" \
  --state-dir "$HOME/.local/state/onionsurge/collector" \
  --output "$HOME/.local/state/onionsurge/snapshot.json"  # Copy source.env.example -> source.env (chmod 600)

python3 scripts/build.py \
  --input inputs/snapshot.json \
  --time-ranges inputs/time_ranges.json \
  --output dist \
  --cache .cache/reference

python3 scripts/package_previews.py \
  --directory dist                              # Embed offline design-{1,2,3}.html & update build-manifest.json
```

Optional `build.py` flags: pass `--time-ranges <path>` to use a custom range configuration without editing code, `--live` for live-release frontend builds, and `--refresh-reference` to rebuild the daily frozen reference cache. Optional `collect.py` flags `--plan` (read-only catch-up/timeout planner), `--record-checkpoint`, and repeatable `--forbid-overlap` isolate collector state, `0600` `checkpoint.json` / `cache_committed` / `checkpoint_updated` tracking, SSH keys, and private seeds from publication runners. See [`docs/collector.md`](docs/collector.md), [`docs/query-gateway.md`](docs/query-gateway.md), and [`docs/source-install.md`](docs/source-install.md) for hourly/daily SSH aggregate collection (`3,600s` UTC hours, `21,600s` sampled 1h reference points, `86,400s` `00:00 UTC` calendar days), gateway installation, and `90` fixed query IDs / `208` operations. See [`docs/live-releases.md`](docs/live-releases.md) for the live `onionsurge-release-v1` SHA-256 verification/refresh/fallback protocol, and [`docs/performance.md`](docs/performance.md) for `Allium` / cache reuse and rendering benchmarks.

## Cohorts, Families & MetricsPort Sources

Synthetic fixture populations at `2026-11-18T00:00:00Z` (`versions.metric_cohorts`; production uses a private `SOURCE_SEED_SNAPSHOT`):

- **`native` (`240` relays)**: `120` Tor `0.4.9.15` (`80` `stock.15`, `40` `pkg.15`) + `80` Patched Tor `0.4.9.15 + tor41339.9` + `40` Tor `0.4.9.14` (`pkg.14`). `dircache_cohort` separately tracks `64` non-exit `DirCache 0` relays (`0` live ControlPort + `64` config-file fallback; `0` conflicts excluded).
- **`directory_defense` (`200` relays)**: `120` Tor `0.4.9.15` + `80` Patched Tor `0.4.9.15 + tor41339.9`.
- **`custom` (`80` relays)**: Patched Tor `0.4.9.15 + tor41339.9`.

| Family | Chart ID | Kind | MetricsPort Source | Unit (Display Scale) | Cohort (Relays) | Series |
| --- | --- | --- | --- | --- | --- | --- |
| `01 transport` | `transport_rejections` | `counter_hour_rate` | `tor_relay_dos_total` | `attempts/s` (`×1`) | `native` (`240`) | `2` |
| `02 circuits` | `circuit_rejections` | `counter_hour_rate` | `tor_relay_dos_total` | `cells/s` (`×1`) | `native` (`240`) | `1` |
| `02 circuits` | `onionskin_drops` | `counter_hour_rate` | `tor_relay_load_onionskins_total` | `tasks/s` (`×1`) | `native` (`240`) | `4` (`3` plotted) |
| `02 circuits` | `open_circuits` | `gauge_hour_mean` | `tor_relay_circuits_total` | `circuits` (`×1`) | `native` (`240`) | `1` |
| `02 circuits` | `protocol_closes` | `counter_hour_rate` | `tor_relay_circ_proto_violation_total` | `requests/s` (`×1`) | `native` (`240`) | `1` |
| `03 directory` | `directory_requests` | `counter_hour_rate` | `tor_relay_begindir_request_total` | `events/s` (`×1`) | `custom` (`80`) | `10` (`3` plotted) |
| `03 directory` | `directory_rejections` | `counter_hour_rate` | `tor_relay_dos_total` | `events/s` (`×1`) | `directory_defense` (`200`) | `3` |
| `03 directory` | `hsdir_fetches` | `counter_hour_rate` | `tor_relay_hsdir_fetch_total` | `requests/s` (`×1`) | `custom` (`80`) | `4` |
| `03 directory` | `hsdir_uploads` | `counter_hour_rate` | `tor_relay_hsdir_upload_total` | `requests/s` (`×1`) | `custom` (`80`) | `6` |
| `03 directory` | `directory_waiting` | `gauge_hour_mean` | `tor_relay_begindir_connections` | `connections` (`×1`) | `custom` (`80`) | `1` |
| `04 introduction` | `introduction_rejections` | `counter_hour_rate` | `tor_relay_dos_total` | `requests/s` (`×1`) | `native` (`240`) | `1` |
| `05 queues` | `queue_limit_circuits` | `counter_hour_rate` | `tor_relay_dos_total` | `hits/s` → `queue-limit events/hour` (`×3600`) | `native` (`240`) | `2` |
| `06 exit` | `exit_stream_rejections` | `counter_hour_rate` | `tor_relay_dos_total` | `requests/s` (`×1`) | `native` (`240`) | `1` |

Chart subtitles render copy-safe MetricsPort source selectors (`type`, `action`, and `state` labels via `metricSelectorTokens`) in uniform color and weight, narrowing to the selected series when a usable Explorer `p10–p90` band is enabled while full aggregation and coverage PromQL remain in Evidence explorer.

## Estimators, Qualification & Statistical Model

- **Aggregation order & hourly/daily boundaries**: `24h` plots evaluate non-overlapping completed `3,600s` (`1h`) UTC intervals across `[end - 86400, end]`, backed by 6-hour sampled `1h` (`21,600s` step) historical baseline points (`sampled_hour`), while multi-day ranges (`7d`–`all`) plot non-overlapping `00:00:00 UTC` (`86,400s`) calendar days (and `current24` evaluates direct `[1d]` at `end`). Counter charts evaluate `rate(metric[1h])` / `rate(metric[1d])` per original scrape series before `sum(...)` so daemon restarts stay isolated per relay; gauges evaluate `sum(avg_over_time(metric[1h]))` / `sum(avg_over_time(metric[1d]))`. Unconfigured `< 24h` slices (`0h`–`23h`, `1d`, `[0-9]+[sm]`, or trailing `-24h` tokens) remain disallowed in `inputs/time_ranges.json`.
- **Sample & population gates**: Qualification requires `>= 24 / 30` scrape samples per `1h` (`3,600s`) window and `>= 360 / 720` samples per `24h` (`86,400s`) window (`directory_requests` requires all 3 `previous_hop` series per relay across all `10` classes in `current_hour` and `hs_get`/`hs_post`/`no_request` in `daily`/`sampled_hour`). Coverage requires `reporting_relays / expected_relays >= 0.90` (`MINIMUM_POPULATION_COVERAGE = 0.9`), where `expected_relays = max(endpoint_available, max_31d_eligible)` (`max_32d` hourly lookback). Unqualified or missing intervals remain `null` (rendered as diagonal SVG hatching, never `0`).
- **`dir-spec` coarsening** (`scripts/coarsening.py`): Standalone public relay, flag, cohort, and `dircache_cohort` counts `< 8` are floored to `0` and `>= 8` are rounded to the nearest multiple of `8` (preserving `classified <= total` and `middle_only + role <= classified`), while partitioned build, cohort, and `middle_only`/non-middle role breakdowns distribute multiples of `8` by largest remainder to sum to the coarsened total (so individual parts may differ from nearest-multiple rounding). `open_circuits` means round to nearest `100`, then to `3` significant digits; `hsdir_fetches`, `hsdir_uploads`, `introduction_rejections`, and `directory_requests` `hs_get`/`hs_post` bin to `0.001/s`, then round to `3` significant digits; other counter series suppress `< 8` events/window, bin to `8`-event increments, and round rates to `3` significant digits; and non-sensitive gauge means (`directory_waiting`) floor `< 0.05` to `0`, quantize `[0.05, 1)` to `0.1`, and round `>= 1` to `3` significant digits.
- **Daily-frozen 18-month reference** (`midrank-linear-quantiles-18-calendar-months-v4`): Complete UTC calendar window `[day_end - 1d - 18mo, day_end - 1d]` ending at prior UTC midnight (where `day_end = floor(end / 86400) * 86400`, excluding `current.day` and current-day hours), cached at `.cache/reference/YYYY-MM-DD/<16-hex>.json` keyed by `SHA-256(METHOD_VERSION || catalogue_sha256 || method_sha256)`. Includes both `hour` (`sampled_hour` on `21,600s` grid) and `day` (`00:00 UTC`, `86,400s`) normalized per-relay (`x = value / relays`) distributions. **18-month average** scales back via `mean_per_relay * current_relays * display_scale` (using `current.hour.relays` for `24h` and `current.day.relays` for daily windows).
- **Percentiles, historical rulers & status bands**: Linear-interpolated `p10, p50, p90, p95, p99` (`n >= 2` enables the Explorer `p10–p90` band across both `24h` hourly and daily ranges, scaled per plotted interval's reporting population). Ranks compute the exact `Fraction` mid-rank empirical CDF `100 * (count(< x) + 0.5 * count(== x)) / n`, suppressed (`flat_zero: true`) when reference and current values are all zero. UI emphasis marks `p90–p99` at `n >= min_comparison_windows` (keeping the hero `18-month` percentile badge gated at `n >= 20`) and `high` (`>= p99`) at `n >= 100`. Configured periods in `inputs/time_ranges.json` accept `0 <= minimum_coverage <= 1` (`0` uses all qualifying `>= 1` days in the window; `0`-day series still mark `insufficient_data`; `24h` requires `minimum_coverage: 0`).
- **Adaptive 2×2 stat grid, Hourly headline & Historical averages**: Every card keeps a 2×2 `.stat-pair` layout driven by `inputs/time_ranges.json` (`card_slots`): on schema-7 hourly releases (`fixed: ["24h", "all"]`, `fallback_dynamic: "6mo"`), top-left displays **Last complete hour** (`18-month` hourly baseline, `Sampled 1h fleet peak`, `18-month` ruler), top-right tracks the active daily `range` (`7d`, `1mo`, `3mo`, `6mo`, `12mo`) or `fallback_dynamic` (`6mo`) when viewing `24h`/`all`, bottom-left holds the fixed `24-hour average` (`current.day` vs `18-month` daily baseline + `Recorded 24h fleet peak`), and bottom-right holds `All-time average` (legacy `v5`/`v6` daily-only releases omit hourly rows and keep the 4-daily-card fallback layout). Historical rulers (`daily-per-relay-midrank-v1` in `historical_statistics.py`) rank `current.day` per relay against non-overlapping qualifying daily values ending at or before `reference_cutoff` (`[day_end - (count + comparison_lag_days)d, day_end - 1d]` for day windows such as `7d`; `calendar_window(day_end, count, lag_days)` through `day_end - 1d` for month windows; and retained history `..day_end - 1d` for `all_time`). `peaks` track `hour` and `day` `fleet_peak` and per-relay `value_per_relay`.
- **Per-statistic relay role populations** (`population_statistics.py`): Decodes coarsened gateway `relay_population` role-bitmask histograms (`Guard`, `Exit`, `HSDir`, `V2Dir`, `Running`, `middle-only = Running & ~(Guard|Exit|HSDir|V2Dir)`) per statistic (`hour`, `day`, plus every configured non-hour `summary_key`: `seven_days`, `one_month`, `three_months`, `six_months`, `twelve_months`, `all_time`). Emits exact counts under complete flag coverage and outward-rounded conservative `[min, max]` `role_bounds` when flag records are partial.

## Architecture, Generated Artifacts & Build Invariants

| Path | Role | Key Technical Details |
| --- | --- | --- |
| [`inputs/snapshot.json`](docs/input-format.md) | Aggregate input | Closed schema (`input_schema_version: 4`, `v3` daily seed upgrade supported in `collect.py`); `validation.py` enforces max JSON depth `64`, unique keys, finite numbers, `PRIVATE_KEYS` denylist, CSV prefix guards (`= + - @ \|`), Unicode `Cf` rejection, `3,600s` (`1h`) current windows + `21,600s` (`sampled_hour`) + `86,400s` (`daily`) history, and `dir-spec` coarsening |
| [`inputs/time_ranges.json`](docs/input-format.md) | Range config | Declarative `version: 1` config (`24h`, `7d`, `1mo`, `3mo`, `6mo`, `12mo`, `all`, `default_range: "24h"`, `legacy_aliases`, `card_slots`); `time_ranges.py` enforces `1`–`16` periods, one `all_time`, at most one exact `hours` (`id="24h"`, `export_suffix="24h"`, `count=24`), `days >= 2`, `months >= 1`, and sub-24h / `1d` / `[0-9]+[smh]` / trailing `-24h` ID & CSV suffix rejection |
| `catalogue/` & `source.env.example` | Query contract & SSH env | Reviewed `contract.json` (`query_contract_sha256` tracked in `data.json['provenance']` and `build-manifest.json`), deterministically expanded into `queries.json` (`90` expressions) and `routes.json` (`208` operations); `source.env.example` templates the 5 `SOURCE_SSH_*` settings plus optional `SOURCE_SEED_SNAPSHOT` |
| `site/` | Static web UI | `index.html`, `style.css`, zero-dependency ES2022 `app.js` + live `live.js`; Overview opens with a three-tier **Concurrent DoS activity** card (selectable `24h\|7d` left panel + `All time` right panel side-by-side on desktop `>=1000px`, `24h\|7d\|all` `Window` toggle on narrower screens, Linear/Symlog, optional `HSDir GET/POST` exclusion, non-overlapping `/s` rate stack, 6-family `>=90th`/`>=99th` strip, normalized state gauges); per-family SVG step renderer (`MAX_PLOT_INTERVALS = 100,000`), pointer/keyboard crosshair tooltip, URL state `?view=overview\|explorer\|fixes\|methods&range=24h\|7d\|1mo\|3mo\|6mo\|12mo\|all&layout=grid\|full&family=&chart=&series=&band=1&hero_range=24h\|7d\|all&hero_scale=linear\|symlog&hero_hsdir=0` (legacy `?range=30d`/`1m`/`1y` map via `time_ranges.legacy_aliases`; legacy `v5`/`v6` rollbacks clamp `24h -> 7d`) |
| `scripts/` | Build, telemetry & bench | `collect.py`, `query_protocol.py`, `query_gateway.py`, `install_source.py`, `build_catalogue.py`, `build.py`, `package_previews.py`, `time_ranges.py`, `coarsening.py`, `validation.py`, `reference_statistics.py`, `historical_statistics.py`, `population_statistics.py`, `windows.py`, `display_numbers.py`, `io_helpers.py`, `benchmark_charts.cjs` |
| `tests/` | Offline, source & live suite | Python `unittest` (`test_{build,collect,display_numbers,historical_statistics,install_source,io_helpers,packaging,population_promql,population_statistics,query_gateway,query_protocol,reference,time_ranges,validation}.py`) + Node (`test_display.cjs`, `test_live.cjs`) |
| `dist/` / `dist-live/` | Built output (`schema_version: 7`) | Git-ignored; `build.py` / `make serve` emits `99` manifest-tracked files (`+ build-manifest.json`), expanding to `102` files after `make previews` / `make check` / `package_previews.py` embeds offline `design-{1,2,3}.html`; `--live` emits `99` files (staging selects `index.html`, `app.js`, `live.js`, `style.css`, while release publication uploads `data.json`, `reference/...json`, and `91` CSVs) |

Generated artifacts (`dist/` offline; keep together when hosting):

- `data.json` & `data.js` (`window.ONIONSURGE_DATA`, `<` escaped as `\u003c`), `catalogue.json` (`families`, `charts`, `methods`), and `reference/reference-YYYY-MM-DD-<12-hex>.json` (frozen distributions + script/input/config SHA-256 provenance).
- `exports/<chart_id>-<export_suffix>.csv` (`91` files for `24h`, `7d`, `1mo`, `3mo`, `6mo`, `12mo`, `all-time`): `interval_end_utc,series,value,unit` in unscaled coarsened source units.
- `design-{1,2,3}.html` (offline only): Single-file previews (`24h` default, `All time`, `Explorer`) with `<meta http-equiv="Content-Security-Policy">` pinning exact `sha256-...` digests for inline `<script>` and `<style>` blocks.
- `build-manifest.json`: Canonical POSIX relative path → `SHA-256` integrity map (with `mode: "offline" | "live"`, `schema_version`, `snapshot_end`, `input_sha256`, `query_contract_sha256`, and `release_objects`).

### Concurrency, Locking & Staged Promotion Invariants

- **Two-tier `fcntl.flock`**: Short-lived `~/.cache/onionsurge/generation-registration.lock` (resolved from `pwd.getpwuid(os.geteuid()).pw_dir`, ignoring `HOME`/`TMPDIR`; pre-create `~/.cache/onionsurge/` if the build-owner home is read-only) serializes overlap checks and claim creation before acquiring persistent sibling `.{name}.build.lock`.
- **Disjoint path boundaries**: Output roots cannot be symlinks or overlap the repo root, `site/`, `scripts/`, `inputs/`, `catalogue/`, `--input`, `--cache`, `cwd`, `~/.cache/onionsurge/`, or any ancestor/descendant generation or `.{name}.build.lock`. Missing outputs probe filesystem aliases via `mkdir(0o700)` + `(st_dev, st_ino)` identity verification before cache writes.
- **Two-step staged promotion**: Builds and repackaging render into sibling `.{name}.staging-*`, park any existing owned generation (`build-manifest.json` `format == onionsurge-build-v1`) at `.{name}.previous-*` via `os.replace`, and promote `.{name}.staging-*` into the target path with a second `os.replace` (rolling back on failure). Because the target path is temporarily absent between the two `os.replace` calls and directory locks serialize writers only, uncoordinated readers must not resolve the active output path mid-promotion.
- **Hash-verified packaging**: `package_previews.py` requires matching `euid`, non-symlink regular files, and full `SHA-256` verification against `build-manifest.json` (`mode == "offline"`) when `build-manifest.json` exists, before regenerating `design-{1,2,3}.html` and restoring `st_gid`/mode prior to promotion.
- **Reservation lifecycle**: `.{name}.build.lock` claims persist across failed runs. Retire an obsolete root only after all writers stop by removing both the output directory and its `.{name}.build.lock`.

## Deployment & License

Layer-3 local/VPN or edge hosting (`local_site.py`, `edge/worker.mjs`), `renameat2(RENAME_NOREPLACE)` / `renamex_np(RENAME_EXCL)` release publication, cron/systemd scheduling, and optional storage mirroring live in [`1aeo/onionsurge-deploy`](https://github.com/1aeo/onionsurge-deploy). Forced-command SSH telemetry access (`scripts/query_gateway.py`, `scripts/query_protocol.py`, `scripts/install_source.py`), the `90`-expression / `208`-operation query catalogue (`catalogue/`), and the aggregate collector (`scripts/collect.py`) live in this repository.

Tor metric names and semantics follow C Tor `MetricsPort`. Operated by 1AEO; not an official Tor Project service. Display-number formatting provenance is recorded in [`NOTICE`](NOTICE). Licensed under [`Apache-2.0`](LICENSE).
