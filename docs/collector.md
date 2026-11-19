# Daily aggregate collector

`scripts/collect.py` produces the application's schema-3 (`86400 s` `00:00 UTC`) input,
applies deterministic `dir-spec` coarsening (`0` for `< 8`, nearest multiple of `8` for
`>= 8`, `open_circuits` rounded to `100`, sensitive rates binned to `0.001/s`, counter
rates to `8`-event increments and `3` sig figs), then calls `validation.validate_snapshot`
before atomically replacing the output. It does not upload files, start timers, change
relays, or install access. The application builder remains responsible for schema-6
output, daily frozen references, percentiles, retained peaks, and configurable
multi-period historical averages (defaulting to `7d`/`1mo`/`3mo`/`6mo`/`12mo`/`all-time`).

```sh
python3 scripts/collect.py \
  --config "$HOME/.config/onionsurge/source.env" \
  --state-dir "$HOME/.local/state/onionsurge/collector" \
  --output "$HOME/.local/state/onionsurge/snapshot.json"
```

`--application-dir` is optional and defaults to the checkout containing `scripts/collect.py`.
When `catalogue/queries.json` exists under `--application-dir`, that directory is
selected for both `catalogue/queries.json` and `catalogue/routes.json` (the collector
does not fall back to its own checkout's `routes.json`, so an alternate override must
provide both files); otherwise both catalogue files come from the collector's own checkout.
`--seed PATH` or optional `SOURCE_SEED_SNAPSHOT` in `source.env` selects a private
`0600` non-symlink regular file as the retained-history seed instead of
`inputs/snapshot.json` (so production telemetry stays private while example fixtures
remain synthetic). Output paths overlapping the active seed snapshot (`--seed`,
`SOURCE_SEED_SNAPSHOT`, or `inputs/snapshot.json`), `observations.sqlite3`, `collect.lock`,
`queries.json`, or `routes.json` are rejected.
The default endpoint is the latest completed `00:00:00 UTC` calendar day
(`(int(time.time()) // 86400) * 86400`). `--end UNIX_SECONDS` selects another
completed UTC-midnight timestamp at or after the seed snapshot (`end % 86400 == 0`).
Run once per UTC day after the day's scrapes have settled (default `00:20 UTC`).

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
defines every query and operation (`61` fixed query IDs, `78` operations). Collection
stops if the source advertises a different catalogue. Ordinary requests are sequential,
at most seven days and 169 evaluated points, with `query_protocol.py`'s timeout,
size, aggregate-label and warning checks. Errors and execution warnings abort
collection; they are never converted to zero measurements.

The private SQLite cache (`observations.sqlite3`) stores immutable evaluated endpoints,
keyed by source identity, exact expression hash, step and time. An empty successful
response is recorded distinctly from a numeric zero. Existing observations are reused;
missing endpoints are fetched in bounded contiguous batches. The configured seed snapshot
(via `--seed`, `SOURCE_SEED_SNAPSHOT`, or the repository fixture `inputs/snapshot.json`)
seeds all retained daily history. Its already-coarsened historical observations
(`< end`) are preserved unchanged without re-coarsening. Only missing completed
UTC-midnight daily history tails are appended and coarsened.

- All observations use completed `00:00:00 UTC` (`86,400 s` / `[1d]`) intervals. Counters use per-source
  `rate(...[1d])` before summing; gauges use `avg_over_time(...[1d])` (`>= 360` of `720` samples per day).
  No sub-24h windows (`1h`, `6h`, `current24`, or intra-day rolling snapshots) are queried or emitted.
- The directory request classes require all three previous-hop strata to meet the daily sample gate
  before rate and relay counts contribute (`previous_hop` itself never crosses the SSH gateway).
  Partial stratum counts are never divided by three.
- Each daily denominator uses the coarsened 31-day maximum eligible population on the UTC
  midnight grid in `(end - 31 days, end]`, or endpoint availability, whichever is larger.
  `current.day` matches the completed daily row at `end`.
- Qualification requires at least 90% coverage (`>= 8` coarsened relays). Unqualified values
  on `current.day` are `null`. A successful empty cohort produces a zero observed population and
  fresh gaps, while retaining historical data and a positive expected population (`>= 8`).
  Unqueried auxiliary daily values remain unavailable.

Thirty historical series and all 37 current series remain in the input. The seven
auxiliary directory classes retain current-only status. Their missing history is
not fabricated. Queue totals retain their included output-queue subset; the two
series are never added together.

## Contributors and recorded flags

Each series also carries optional `relay_population` metadata with method
`eligible-last-flags-v1`. Its `day` row uses the exact eligible
contributors for the `end` day (coarsened to multiples of `8`, `0` below `8`). Its `daily` rows
cover qualifying retained daily observations; no current fleet count is substituted for history. Rows are
`[end, total, classified, guard, middle_only, exit, hsdir, v2dir]`. Unavailable or
total-mismatched current rows are `null`; unavailable daily rows are omitted.
The original measurements remain unchanged.

Here `classified` means all five last-recorded flags—Running, Guard, Exit, HSDir
and V2Dir—are present and binary, with agreeing values across any duplicate
streams for the same relay/flag. Agreeing duplicates collapse to one recorded
value; missing, conflicting or nonbinary observations remain unclassified. The
eligibility mask and its contributor total are unchanged. Flags use
`last_over_time(tor_relay_flag[1d])` for the same one-day UTC window
as the metric. This includes an eligible contributor whose last scrape preceded
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
