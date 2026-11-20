# User-owned query gateway

`scripts/query_gateway.py` is a forced-command SSH entry point for the collector. It accepts only `queries` and `prom QUERY_ID START END STEP` (with `STEP` in `{3600, 21600, 86400}` on aligned UTC boundaries: `3600s` for `current_hour`, `current24`, metadata, and population `hour`; `21600s` for `sampled_hour`; `86400s` UTC midnight for `daily` and population `day`) through `SSH_ORIGINAL_COMMAND`. Paths, arbitrary PromQL,
and shell commands are not accepted. No account or SSH configuration is changed by this
program.

Install `scripts/query_gateway.py`, the adjacent `scripts/query_protocol.py` module,
and `catalogue/queries.json` in the pinned code directory using
[`scripts/install_source.py`](source-install.md). The fixed authorized-key command invokes
Python 3.11+ with `-I` and explicit `--config-dir` and `--state-dir` absolute paths.
Configure the key's source restriction and `restrict` option separately. Keep the
code and all paths under the approved account's control; do not place them in published assets.

Both directories must already exist, be owned by the running account, and have
mode 0700. Configuration and attestation files must be owned regular files with
mode 0600. Final symlinks are rejected. The state directory contains a single
nonblocking query lock; a concurrent request fails with `query_busy`.

The configuration directory contains the reviewed public `queries.json` and a
private `gateway.json` with an integer `prometheus_port` and optional absolute
`attestations_file` path. The source API is always loopback; proxy environment
settings and redirects are disabled. Ordinary queries reuse `query_protocol.py`'s
range, response, label and warning checks. A request has a 40-second process
deadline; Linux also applies the 384 MiB address-space limit. macOS supports
authoring checks without that unsupported address-space control.

## Private daemon-version attestations

The optional file binds prior direct daemon-version observations to private
process identities. It contains:

- `selectors`: private, fixed `pid`, `start`, `health`, `source_time`, and `info`
  PromQL selectors for the local monitoring API.
- `groups`: only `daemon_group_a` and/or `daemon_group_b`, each with numeric
  `proof_observed_at`, a lowercase SHA-256 `proof_sha256`, and optional numeric
  four-component `version` that agrees with its records.
- `records`: `source`, `unit`, positive integer `pid`, `start_time_seconds`,
  `metrics_instance`, `public_group`, and `version`. Members and targets must be
  unique. A recorded process must have started before its daemon observation.

The file must be prepared from independently reviewed daemon observations and
process mappings. The gateway does not obtain a new daemon version or infer one
from a process name, installed package, metric introduction, or default executable.
Version observations are valid only while the bound process identity remains
unchanged. Refreshing a file's timestamp is not a new version observation.

Four fixed virtual IDs are gateway-computed descriptors, not Prometheus metrics:

| Query ID | Result |
| --- | --- |
| `attested_native_runtime` | Native cohort counts by canonical `attested-VERSION` build ID |
| `attested_directory_defense_runtime` | Directory-defense cohort counts by that build ID |
| `attested_custom_runtime` | Custom-metric cohort counts by that build ID |
| `attestation_proof_metadata` | Actual observation timestamps and hashes by active public proof group and canonical build ID; a continuity row contains the attestation-file hash and evaluation endpoint |

Virtual requests must use exactly two adjacent completed UTC-hour endpoints (`3,600 s` apart, `STEP=3600`).
For each endpoint, PID, start time, and the metric mapping must match the evidence both
there and one hour (`3,600 s`) earlier. Source health must be 1 and its timestamp must be
within the preceding 180 seconds at both points. Missing, restarted, remapped or
stale members contribute no verified count. Current metric membership determines
each cohort. Members already represented by fresh, fingerprint-verified managed
build metadata are excluded to avoid double counting.

Managed proof uses the state record's binary hash and fingerprint, joined to
fresh CPU observations on the full process epoch (PID, start ticks, boot ID and
member identity). State and CPU records have different `kind` labels; the join
deliberately compares their process identity rather than treating them as the
same collection kind. A recorded state hash remains usable only while that
fresh process identity matches.

Only fixed public group labels, canonical numeric-version build IDs, hashes,
counts and proof times can leave the virtual handler. Private selectors,
identifiers, paths and source error text are never returned, including by
`queries`. Groups without qualifying members have no active proof metadata.
Absent optional evidence produces empty virtual matrices; configured evidence
that is unreadable or malformed fails the request. The collector must classify
unresolved members honestly and must not treat empty proof data as a zero fleet.
