# Hourly and daily aggregate collector

`scripts/collect.py` produces the application's schema-4 (`window_seconds: 3600`, `24 × 1h` current hours + `21,600s` sampled `1h` points + `86,400s` `00:00 UTC` daily history) input,
applies deterministic `dir-spec` coarsening (`0` for `< 8`, nearest multiple of `8` for
`>= 8`, `open_circuits` rounded to `100`, sensitive rates binned to `0.001/s`, counter
rates to `8`-event increments and `3` sig figs), then calls `validation.validate_snapshot`
before atomically replacing the output. It does not upload files, start timers, change
relays, or install access. The application builder remains responsible for schema-7
output, daily frozen `hour`/`day` references, percentiles, retained peaks, and configurable
multi-period historical averages (defaulting to `24h`/`7d`/`1mo`/`3mo`/`6mo`/`12mo`/`all-time`).

```sh
python3 scripts/collect.py \
  --config "$HOME/.config/onionsurge/source.env" \
  --state-dir "$HOME/.local/state/onionsurge/collector" \
  --output "$HOME/.local/state/onionsurge/snapshot.json"
```

Two companion subcommands and a read-only archive exporter support orchestrated hourly runners and lossless multi-year storage without exposing `source.env`, SSH keys, `observations.sqlite3`, or seed snapshots outside the collector:

```sh
python3 scripts/collect.py --plan --config ... --state-dir ... [--previous-end T] [--max-catchup-days 31] [--forbid-overlap PATH ...]
python3 scripts/collect.py --record-checkpoint --config ... --state-dir ... --end T [--forbid-overlap PATH ...]
python3 scripts/collect.py --export-hourly-archive PATH --config ... --state-dir ... [--start T0] [--end T1] [--forbid-overlap PATH ...]
```

- `--application-dir` is optional and defaults to the checkout containing `scripts/collect.py`.
When `catalogue/queries.json` exists under `--application-dir`, that directory is
selected for both `catalogue/queries.json` and `catalogue/routes.json` (the collector
does not fall back to its own checkout's `routes.json`, so an alternate override must
provide both files); otherwise both catalogue files come from the collector's own checkout.
- `--seed PATH` or optional `SOURCE_SEED_SNAPSHOT` in `source.env` selects a private
`0600` non-symlink regular file as the retained-history seed instead of
`inputs/snapshot.json` (so production telemetry stays private while example fixtures
remain synthetic). Repeatable `--forbid-overlap PATH` rejects any path overlap (including
symlinks and non-existent descendants) across `source.env`, `SOURCE_SSH_IDENTITY_FILE`,
`SOURCE_SSH_KNOWN_HOSTS_FILE`, `--state-dir`, and any configured private seed; private
seeds must also be disjoint from `source.env` and `--state-dir`, and `--output` cannot
overlap the seed, `observations.sqlite3`, `checkpoint.json`, `collect.lock`, `queries.json`,
or `routes.json`.
- `--plan` is strictly read-only on missing `--state-dir` (never creates it or opens SSH),
fails closed on symlinked/non-private/corrupt state artifacts, computes a deterministic
`source_identity_sha256` from `source.env` raw bytes, `SOURCE_SSH_KNOWN_HOSTS_FILE`, seed,
`catalogue/queries.json`, and `catalogue/routes.json`, inspects `observations.sqlite3` in
non-mutating read-only locking mode (never `immutable=1`, and skipping unindexed WAL
caches that lack `-shm` without creating sidecars), checks `<state_dir>/checkpoint.json`
against committed `observations` rows and an exact `(source_identity_sha256,
observation_end)` entry in `collection_completions`, enforces `--max-catchup-days`
(distinguishing cold/stale-cache gaps from stale `--previous-end` publication gaps), and
prints `{snapshot_end, catchup_days, recommended_timeout_seconds}` dynamically sized from
`catalogue/routes.json`, protocol batch bounds, and any cold `v3` `sampled_hour` backfill span.
- Normal collection prints `{snapshot_end, charts, series, source_requests,
cache_committed, checkpoint_updated}`. Output publication precedes a single SQLite
transaction that commits `observations` rows, the compressed unique hourly delta in
`hourly_archive`, the `(source_identity_sha256, observation_end)` completion proof in
`collection_completions`, and expired raw `1h` (`step = 3600`, `< end - 35 days`) cache
pruning (`cache_committed: true`), followed by best-effort `PRAGMA incremental_vacuum(256)`
(when `auto_vacuum = INCREMENTAL`) and atomically updating `0600` `<state_dir>/checkpoint.json`
(`checkpoint_updated: true`; if `cache.commit()` fails, both flags are `false` and the
checkpoint is unchanged, while if `checkpoint.json` update fails afterwards,
`cache_committed: true` and `checkpoint_updated: false`). `--record-checkpoint` verifies
`observations.sqlite3` holds committed observation rows and a matching
`(source_identity_sha256, --end)` entry in `collection_completions` under the collector lock
(requiring rollback mode or an active WAL `-shm` index) before recording `checkpoint.json`
explicitly (for example after manual catch-up).
- `--export-hourly-archive PATH` (with optional UTC-hour bounds `--start` and `--end`,
mutually exclusive with `--plan`, `--record-checkpoint`, and `--output`, and respecting
`--forbid-overlap`) opens an existing private `<state_dir>/observations.sqlite3` in strict
non-mutating read-only mode without opening SSH or acquiring `collect.lock`, decompresses
and schema-checks each stored `hourly_archive` row for the configured `source_identity`, and
atomically writes ordered `0600` JSON Lines to `PATH`.
The default endpoint is the latest completed UTC hour
(`(int(time.time()) // 3600) * 3600`). `--end UNIX_SECONDS` selects another
completed UTC-hour timestamp at or after the seed snapshot (`end % 3600 == 0`).
Run hourly (or on the release schedule) after the hour's scrapes settle.

Private source configuration (`source.env`, copied from `source.env.example` with mode
`0600`) uses `SOURCE_SSH_HOST` (an explicit destination, not an ambient SSH alias),
`SOURCE_SSH_USER`, `SOURCE_SSH_PORT`, `SOURCE_SSH_IDENTITY_FILE`, and
`SOURCE_SSH_KNOWN_HOSTS_FILE`. The client sends only `queries` or
`prom ID START END STEP`. It disables ambient SSH configuration and agent forwarding,
supplies the identity and host-key file explicitly, requires strict host-key checking,
and never passes provider credentials or shell snippets to the telemetry source.

## Measurements and retention

The reviewed catalogue (`catalogue/contract.json`, expanded deterministically into
`catalogue/queries.json` and `catalogue/routes.json` via `scripts/build_catalogue.py`)
defines every query and operation (`90` fixed query IDs, `208` operations). Collection
stops if the source advertises a different catalogue. Ordinary requests are sequential,
at most seven days and 169 evaluated points, with `query_protocol.py`'s timeout,
size, aggregate-label and warning checks. Errors and execution warnings abort
collection; they are never converted to zero measurements.

The private SQLite cache (`observations.sqlite3`, initialized with `PRAGMA auto_vacuum = INCREMENTAL` on new DBs) stores immutable evaluated endpoints,
keyed by source identity, exact expression hash, step and time, alongside permanent
compressed hourly snapshot deltas in `hourly_archive`. An empty successful
response is recorded distinctly from a numeric zero. Existing observations are reused;
missing endpoints are fetched in bounded contiguous batches. Multi-year `daily` (`step = 86400`)
and `sampled_hour` (`step = 21600`) histories are retained permanently, while expired raw
`current_hour` scrape rows (`step = 3600`, `< target_end - 35 days`) and older superseded
`collection_completions` rows (always keeping the newest completion per source identity) are
pruned in the commit transaction to bound database growth. The configured seed snapshot
(via `--seed`, `SOURCE_SEED_SNAPSHOT`, or the repository fixture `inputs/snapshot.json`)
accepts both schema-4 and legacy schema-3 (`allow_v3_seed=True`) bundles and seeds all retained daily and sampled-hour history. Already-coarsened historical observations
(`< end`) are preserved unchanged without re-coarsening. Missing completed UTC-midnight `daily` and `21,600 s` `sampled_hour` history tails (up to 560 days when bootstrapping a v3 seed's `sampled_hour`), the 24 completed hourly `[end - 86400 + 3600 .. end]` windows, and intra-day `current24` are queried and coarsened deterministically.

- Hourly observations (`current_hour`, `3,600 s` / `[1h]`) and `21,600 s`-spaced `sampled_hour` baselines evaluate per-source `rate(...[1h])` before summing for counters and `avg_over_time(...[1h])` for gauges (`>= 24` of `30` samples per hour). Daily (`86,400 s` `00:00:00 UTC` / `[1d]`) and rolling `current24` (`[1d]` evaluated at `end`) observations evaluate `rate(...[1d])` / `avg_over_time(...[1d])` (`>= 360` of `720` samples per 24h window).
- The directory request classes require all three previous-hop strata to meet the sample gate
  before rate and relay counts contribute (`previous_hop` itself never crosses the SSH gateway; `current_hour` covers all 10 classes while `daily` and `sampled_hour` cover `hs_get`, `hs_post`, and `no_request`).
  Partial stratum counts are never divided by three.
- Each daily denominator uses the coarsened 31-day maximum eligible population on the UTC
  midnight grid in `(end - 31 days, end]` (and rolling hourly windows use a 32-day context `(end - 32 days, end]`), or endpoint availability, whichever is larger.
  `current.hour` matches the final hour in `series.values` (`end`), and `current.day` matches the `[1d]` measurement ending at `end` (`current24` intra-day, or the final `history.daily` row when `end % 86400 == 0`).
- Qualification requires at least 90% coverage (`>= 8` coarsened relays). Unqualified values
  are `null`. A successful empty cohort produces a zero observed population and
  fresh gaps, while retaining historical data and a positive expected population (`>= 8`).
  Unqueried auxiliary historical values remain unavailable.

Thirty historical series (in both `history.daily` and `history.sampled_hour`) and all 37 current/hourly series remain in the input. The seven
auxiliary directory classes retain current/24h hourly status without fabricated multi-day history. Queue totals retain their included output-queue subset; the two
series are never added together.

## Contributors and recorded flags

Each series also carries optional `relay_population` metadata with method
`eligible-last-flags-v1`. Its `hour` and `day` rows use the exact eligible
contributors for the `1h` and `24h` windows ending at `end` (coarsened to multiples of `8`, `0` below `8`). Its `daily` rows
cover qualifying retained daily observations; no current fleet count is substituted for history. Rows are
`[end, total, classified, guard, middle_only, exit, hsdir, v2dir]`. Unavailable or
total-mismatched current rows are `null`; unavailable daily rows are omitted.
The original measurements remain unchanged.

Here `classified` means all five last-recorded flags—Running, Guard, Exit, HSDir
and V2Dir—are present and binary, with agreeing values across any duplicate
streams for the same relay/flag. Agreeing duplicates collapse to one recorded
value; missing, conflicting or nonbinary observations remain unclassified. The
eligibility mask and its contributor total are unchanged. Flags use
`last_over_time(tor_relay_flag[1h])` for `hour` and `last_over_time(tor_relay_flag[1d])` for `day`/`daily`. This includes an eligible contributor whose last scrape preceded
the endpoint. Guard, Exit, HSDir and V2Dir count their reported positive flags.
Middle-only requires Running=1 and the other four flags explicitly zero. All-zero
flags count as observed records, but never as Middle-only. These are reported Tor
flags, not proof of historical configuration or attribution of attacks to a role.
The roles overlap and do not partition the total.

The gateway returns one aggregate histogram per metric class and window, using
only public `type` and `action` labels. Original metric `action` classes are copied
into `type`; scalar metrics use `type="scalar"`. The histogram's `action` is an
exact integer code: `0` means missing or invalid flag records; otherwise
`1 + 2*Guard + 4*Exit + 8*HSDir + 16*V2Dir + 32*Running`.
The only accepted nonzero codes are odd integers from 1 through 63. The collector
sums bins for totals and flags; code 33 is Middle-only. Thus no individual source
identity leaves the gateway. Missing bins are not missing relays: the sum of
all returned bins must equal the measurement's independently collected eligible
total. An empty histogram remains unavailable rather than becoming zero.

The compact source definitions in `catalogue/contract.json` generate the fixed
queries and routes. Both complete expressions accompany each series for
reproduction. Population backfill requests contain at most three contiguous
daily endpoints (two days of span), within the gateway's unchanged limits.
They run sequentially and reuse the normal query-hash cache; only qualifying
dates in retained history are requested. A single requested endpoint may need
one preceding context endpoint to satisfy the gateway's nonempty-range rule;
that extra endpoint is not added to the requested history.
Validated daily population rows in the public seed are also reused when their
method and query expression match exactly and their totals still match the
qualifying daily measurements. Only missing daily endpoints are queried. Current
day populations use the source-query path when collected cleanly, not stale unverified seed counts. This allows a reviewed aggregate backfill to seed a new collector
without transferring or rebinding a private observation cache.

Historical contributor summaries must average counts over exactly the same
qualifying days as their corresponding statistic. Role means require complete
flag coverage across those contributors; missing classification stays visible.
They describe mean daily contributors, not the number of unique relays seen.

When `promtool` is on `PATH` (as in CI with pinned Prometheus `v3.14.0`), `make check`
also evaluates the generated PromQL against synthetic flag, missing-data,
duplicate-series and sample-gating cases using Prometheus's own test engine.

## Version evidence and public metadata

Managed versions require a recorded state binary hash and verified fingerprint
to match a fresh CPU process identity on member keys, PID, start ticks, and boot
ID. CPU observations must be successful and less than 180 seconds old. Only
binary hashes approved in the public seed registry are named; others contribute
to `runtime_unverified`.

The gateway can additionally revalidate privately held daemon-version evidence;
see [query-gateway.md](query-gateway.md). Its four virtual results always bypass
the persistent telemetry cache, including retries of the same endpoint. Proof
metadata is checked before and after the cohort requests, so a changed evidence
file cannot silently mix provenance. Actual daemon observation times remain in
the output; rechecking an unchanged process does not refresh that observation.
Daemon-only evidence does not claim a live binary hash or infer private patches.

Public output contains only declared series, aggregate counts, approved build
labels and fixed provenance labels. No source addresses, account names, target
identifiers, private selectors or private configuration paths are copied into it.
An absent optional metadata count stays unavailable rather than reusing a stale
seed value. The collector checkout's closed validator (`scripts/validation.py`)
is always the final publication boundary regardless of `--application-dir`. Input
files, cache/state, keys, logs and private evidence must remain outside every public
generation and object-storage prefix.

The state directory is private and owned by the collector account. A persistent
collector lock serializes its writes. A failed collection leaves the preceding
output intact. Publication, CDN staging, and last-good release retention live
separately in `1aeo/onionsurge-deploy`.
