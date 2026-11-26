#!/usr/bin/env python3
"""Collect public Tor aggregates through a fixed-query SSH gateway."""
import argparse
from collections import OrderedDict, defaultdict, deque
from contextlib import closing, contextmanager
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import query_protocol as access
from coarsening import (
    COARSENING_DESCRIPTION,
    DOS_BEGINDIR_CONFIG_SETTINGS,
    DOS_EFFECTIVE_CONFIG_SETTINGS,
    coarsen_cohort_mapping,
    coarsen_count,
    coarsen_partition,
    coarsen_population_row,
    coarsen_threshold_buckets,
    coarsen_value,
)
from io_helpers import atomic_text, file_lock, filesystem_contains, parse_json as io_parse_json, read_regular_bytes, sha256_hex
from population_statistics import METHOD as POPULATION_METHOD
from query_gateway import VIRTUAL_QUERIES
from validation import RUNTIME_SOURCE_FIELDS as PROOF_LABELS, validate_snapshot
from windows import DAY, HOUR, MINIMUM_POPULATION_COVERAGE, SAMPLED_HOUR_STEP, finite_number, is_sha256, nonblank, require

SSH_FIELDS = ('SOURCE_SSH_HOST', 'SOURCE_SSH_USER', 'SOURCE_SSH_PORT',
              'SOURCE_SSH_IDENTITY_FILE', 'SOURCE_SSH_KNOWN_HOSTS_FILE')
ALIASES = {'circuit_rejections': 'circuit_rejected', 'introduction_rejections': 'introduce2_rejected',
           'exit_stream_rejections': 'stream_rejected'}
SCALARS = {'protocol_closes', 'directory_waiting', 'open_circuits', 'open_connections'}
SUPPLEMENTAL_CHART_SPECS = (
    {
        'id': 'incoming_connections',
        'family': 'Connection floods',
        'family_id': 'transport',
        'title': 'OR incoming connections',
        'definition': 'Received OR connection attempts across IPv4 and IPv6: accepted (created), rejected by DoS defenses, and total attempts (accepted + rejected).',
        'note': 'Received OR connection attempts across IPv4 and IPv6: accepted (created), rejected by DoS defenses, and total attempts (accepted + rejected).',
        'unit': 'attempts/s',
        'display_unit': 'attempts/s',
        'headline_unit': 'attempts/s',
        'display_scale': 1,
        'kind': 'counter_hour_rate',
        'cohort_key': 'native',
        'metrics': ['tor_relay_connections_total'],
        'metric_origin': 'Upstream MetricsPort',
        'introduced_note': None,
        'primary_series': 'total',
        'display_keys': ['total', 'accepted', 'rejected'],
        'series': (
            ('total', 'Total incoming OR (accepted + rejected)', '#55dda3'),
            ('accepted', 'Accepted incoming OR', '#48d9ef'),
            ('rejected', 'Rejected incoming OR', '#ff9873'),
        ),
    },
    {
        'id': 'open_connections',
        'family': 'Connection floods',
        'family_id': 'transport',
        'title': 'Open incoming OR connections',
        'definition': 'Mean active received OR connections currently open across reporting relays (IPv4 + IPv6).',
        'note': 'Mean active received OR connections currently open across reporting relays (IPv4 + IPv6).',
        'unit': 'connections',
        'display_unit': 'connections',
        'headline_unit': 'connections',
        'display_scale': 1,
        'kind': 'gauge_hour_mean',
        'cohort_key': 'native',
        'metrics': ['tor_relay_connections'],
        'metric_origin': 'Upstream MetricsPort',
        'introduced_note': None,
        'primary_series': 'open_connections',
        'display_keys': ['open_connections'],
        'series': (
            ('open_connections', 'Open incoming OR connections', '#48d9ef'),
        ),
    },
)
COHORT_ANCHORS = (
    ('native', 'circuit_rejections', 'circuit_rejections'),
    ('custom', 'hsdir_fetches', 'success'),
    ('directory_defense', 'directory_rejections', 'begindir_rejected'),
)
COHORT_NAMES = tuple(cohort for cohort, _, _ in COHORT_ANCHORS)
METADATA_KEYS = ('flags', 'targets', 'up')
DIRCACHE_OPERATIONS = (
    ('configured_nonexit_zero', 'combined_zero_nonexit'),
    ('observed_controlport_zero', 'observed_zero'),
    ('file_config_fallback_zero', 'file_fallback_zero'),
    ('file0_observed1_conflicts_excluded', 'file0_observed1_conflicts'),
)
POPULATION_BATCH_POINTS = 3
POPULATION_CONTEXT_DAYS = 32
SAMPLED_HOUR_BACKFILL_MAX_DAYS = 560
SOURCE_REQUEST_TIMEOUT = 55
COLLECTION_OVERHEAD_SECONDS = 60
MAX_CONFIG_BYTES = 65536
MAX_KNOWN_HOSTS_BYTES = 65536
MAX_SEED_BYTES = 64 * 1024 * 1024
HOURLY_ARCHIVE_SCHEMA = 1
MAX_ARCHIVE_ROW_BYTES = 8 * 1024 * 1024
HOURLY_CACHE_RETENTION_DAYS = 35
INCREMENTAL_VACUUM_PAGES = 256
ARCHIVE_EXPORT_BATCH_SIZE = 128
SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
_ENV_LINE_RE = re.compile(r'(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)')
_HOME_VAR_RE = re.compile(r'\$\{HOME\}|\$HOME(?![A-Za-z0-9_])')
_POP_CODE_RE = re.compile(r'0|[1-9][0-9]?')
_SLUG_SUB_RE = re.compile(r'[^a-z0-9]+')
_ATTESTED_BUILD_RE = re.compile(r'attested-(?:[0-9]+_){3}[0-9]+')
_TOR_VERSION_RE = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,3}){3}\Z')
_DOS_DIGITS_RE = re.compile(r'[0-9]{1,8}\Z')
_BEGINDIR_VAL_RE = re.compile(r'(?:auto|[0-9]{1,8})\Z')


class CollectionError(ValueError):
    pass


def _require(condition, message):
    require(condition, message, CollectionError)


def parse_env(text):
    """Parse assignment-only KEY=value settings without executing a shell."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = _ENV_LINE_RE.fullmatch(line)
        _require(match, 'Configuration must contain only KEY=value assignments')
        key, raw = match.groups()
        _require(key not in values, f'Duplicate configuration key: {key}')
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError as error:
            raise CollectionError(f'Invalid quoting for configuration key: {key}') from error
        _require(len(tokens) <= 1, f'Quote spaces in configuration key: {key}')
        value = tokens[0] if tokens else ''
        _require(not any(ord(ch) < 32 or ord(ch) == 127 for ch in value),
                 f'Control character in configuration key: {key}')
        values[key] = value
    return values


def expand_path(value):
    home = str(Path.home())
    path = Path(_HOME_VAR_RE.sub(lambda _: home, os.fspath(value))).expanduser()
    _require(path.is_absolute(), 'Configured filesystem paths must be absolute or home-relative')
    return path


def load_source_config(path, required=SSH_FIELDS, private=True, return_bytes=False):
    """Load private assignment-only source SSH settings safely without symlinks."""
    resolved = expand_path(path)
    try:
        raw = read_regular_bytes(resolved, MAX_CONFIG_BYTES, private=private)
    except ValueError as error:
        raise CollectionError(str(error)) from error
    values = parse_env(raw.decode('utf-8'))
    missing = [key for key in required if not values.get(key)]
    _require(not missing, 'Missing configuration keys: ' + ', '.join(missing))
    return (values, raw) if return_bytes else values


def parse_json(raw):
    try:
        return io_parse_json(raw)
    except ValueError as error:
        raise CollectionError(str(error)) from error


def read_json(path):
    return parse_json(Path(path).read_bytes())


def number(value, count=False):
    if isinstance(value, bool):
        raise CollectionError('Invalid aggregate number')
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise CollectionError('Invalid aggregate number') from error
    if not math.isfinite(result) or math.copysign(1.0, result) < 0 or (count and not result.is_integer()):
        raise CollectionError('Invalid aggregate number')
    return int(result) if count else result


def atomic_json(path, value, *, before_replace=None):
    atomic_text(path, json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n',
                mode=0o600, preserve_mode=False, dir_mode=0o700, before_replace=before_replace)


@contextmanager
def state_lock(directory):
    try:
        with file_lock(directory / 'collect.lock', private_dir=True):
            yield
    except ValueError as error:
        raise CollectionError(str(error)) from error


def _validate_ssh_port(value):
    if isinstance(value, bool):
        raise CollectionError('Invalid source port')
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise CollectionError('Invalid source port') from error
    if not 1 <= port <= 65535:
        raise CollectionError('Invalid source port')
    return port


def _validate_ssh_files(config):
    port = _validate_ssh_port(config.get('SOURCE_SSH_PORT'))
    host, user = config.get('SOURCE_SSH_HOST'), config.get('SOURCE_SSH_USER')
    _require(isinstance(host, str) and bool(host.strip()) and isinstance(user, str) and bool(user.strip()),
             'Missing source SSH target settings')
    identity_raw = config.get('SOURCE_SSH_IDENTITY_FILE')
    known_hosts_raw = config.get('SOURCE_SSH_KNOWN_HOSTS_FILE')
    _require(bool(identity_raw) and bool(known_hosts_raw), 'Missing source SSH file paths')
    identity_path = expand_path(identity_raw)
    known_hosts_path = expand_path(known_hosts_raw)
    try:
        with access.regular_read(identity_path):
            pass
        known_hosts_bytes = read_regular_bytes(known_hosts_path, MAX_KNOWN_HOSTS_BYTES)
        identity = identity_path.resolve(strict=True)
        known_hosts = known_hosts_path.resolve(strict=True)
    except (access.AccessError, OSError, ValueError) as error:
        raise CollectionError('Source SSH file must be a readable regular file') from error
    return port, identity_path, known_hosts_path, identity, known_hosts, known_hosts_bytes


class SSHSource:
    def __init__(self, config):
        port, _, _, identity, known_hosts, known_hosts_bytes = _validate_ssh_files(config)
        self.arguments = ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                          '-o', 'UpdateHostKeys=no', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                          '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'PermitLocalCommand=no',
                          '-o', 'ConnectTimeout=12', '-o', 'ConnectionAttempts=1',
                          '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + str(known_hosts),
                          '-i', str(identity), '-p', str(port), '-l', config['SOURCE_SSH_USER'],
                          '--', config['SOURCE_SSH_HOST']]
        # REMOTE_GATEWAY_COMMAND belongs to authorized_keys provisioning. A
        # forced-command session receives only queries/prom as its original command.
        identity_data = [config['SOURCE_SSH_HOST'], config['SOURCE_SSH_USER'], str(port),
                         sha256_hex(known_hosts_bytes)]
        self.identity = sha256_hex(json.dumps(identity_data).encode())
        self.requests = 0

    def command(self, command):
        self.requests += 1
        try:
            result = subprocess.run([*self.arguments, command], capture_output=True,
                                    timeout=SOURCE_REQUEST_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CollectionError('Source SSH request failed') from error
        if result.returncode or len(result.stdout) > access.MAX_RESPONSE:
            raise CollectionError('Source SSH request failed or exceeded its response limit')
        try:
            return parse_json(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise CollectionError('Source returned invalid JSON') from error

    def catalogue(self):
        return self.command('queries')

    def query(self, key, begin, end, step):
        return self.command(f'prom {key} {begin} {end} {step}')


def _clone_payload(payload):
    return {
        'status': payload['status'],
        'data': {
            'resultType': payload['data']['resultType'],
            'result': [
                {'metric': dict(row['metric']), 'values': [pt[:] for pt in row['values']]}
                for row in payload['data']['result']
            ],
        },
    }


def _endpoint_pairs(pairs, target_end):
    return [list(pair) for pair in pairs if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == target_end]


def _build_hourly_archive_entry(data):
    """Extract only the endpoint hour's unique coarsened delta without duplicating 24h windows or histories."""
    target_end = data['end']
    entry = {
        'schema': HOURLY_ARCHIVE_SCHEMA,
        'start': data['start'],
        'end': target_end,
        'window_seconds': data['window_seconds'],
        'fleet': data['fleet'],
        'custom': data['custom'],
        'defense': data['defense'],
        'dircache_cohort': data['dircache_cohort'],
        'flags': data['flags'],
        'snapshot_flags': data['snapshot_flags'],
        'versions': data['versions'],
        'input_provenance': data['input_provenance'],
        'charts': [
            {
                'id': chart['id'],
                'relays': chart['relays'],
                'series': [
                    {
                        'key': series['key'],
                        'values': _endpoint_pairs(series['values'], target_end),
                        'hourly_relays': _endpoint_pairs(series['hourly_relays'], target_end),
                        'hourly_expected_relays': _endpoint_pairs(series['hourly_expected_relays'], target_end),
                        'current': series['current'],
                        'relay_population_current': {
                            'hour': (series.get('relay_population') or {}).get('hour'),
                            'day': (series.get('relay_population') or {}).get('day'),
                        },
                    }
                    for series in chart['series']
                ],
            }
            for chart in data['charts']
        ],
    }
    if 'dos_thresholds' in data:
        entry['dos_thresholds'] = data['dos_thresholds']
    return entry


class ObservationCache:
    """Immutable evaluated endpoints; absence is cached separately from zero."""
    MAX_MEM_ENTRIES = 2048

    def __init__(self, path, source, queries):
        self.source, self.queries = source, queries
        self._mem = OrderedDict()
        path = Path(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        else:
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                     'Observation cache must be a private owned regular file')
        self.db = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            if self.db.execute('PRAGMA page_count').fetchone()[0] == 0:
                self.db.execute('PRAGMA auto_vacuum = INCREMENTAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS observations '
                            '(source TEXT, query_sha TEXT, step INTEGER, endpoint INTEGER, payload TEXT, '
                            'PRIMARY KEY(source, query_sha, step, endpoint))')
            self.db.execute('CREATE TABLE IF NOT EXISTS collection_completions '
                            '(source_identity TEXT NOT NULL, observation_end INTEGER NOT NULL, '
                            'PRIMARY KEY (source_identity, observation_end))')
            self.db.execute('CREATE TABLE IF NOT EXISTS hourly_archive '
                            '(source_identity TEXT NOT NULL, observation_end INTEGER NOT NULL, '
                            'archived_at INTEGER NOT NULL, payload_zlib BLOB NOT NULL, '
                            'PRIMARY KEY (source_identity, observation_end))')
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def record_hourly_archive(self, source_identity, observation_end, data, archived_at=None):
        """Store compressed unique hourly coarsened delta in the current transaction."""
        _require(type(observation_end) is int and data.get('end') == observation_end,
                 'Archive observation endpoint mismatch')
        entry = _build_hourly_archive_entry(data)
        raw = json.dumps(entry, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        _require(len(raw) <= MAX_ARCHIVE_ROW_BYTES, 'Archived hourly payload exceeds size limit')
        payload_zlib = zlib.compress(raw, level=6)
        ts = int(time.time()) if archived_at is None else int(archived_at)
        self.db.execute(
            'INSERT OR REPLACE INTO hourly_archive (source_identity, observation_end, archived_at, payload_zlib) '
            'VALUES (?, ?, ?, ?)',
            (source_identity, observation_end, ts, sqlite3.Binary(payload_zlib)),
        )

    def prune_expired_hourly(self, cutoff_end):
        """Prune expired raw 1h scrape cache rows while retaining daily and sampled_hour forever."""
        _require(type(cutoff_end) is int, 'Invalid hourly prune cutoff')
        self.db.execute('DELETE FROM observations WHERE step = ? AND endpoint < ?', (HOUR, cutoff_end))
        self.db.execute(
            'DELETE FROM collection_completions '
            'WHERE observation_end < ? AND observation_end < ('
            'SELECT MAX(c2.observation_end) FROM collection_completions c2 '
            'WHERE c2.source_identity = collection_completions.source_identity)',
            (cutoff_end,),
        )
        for key in [k for k in self._mem if k[2] == HOUR and k[3] < cutoff_end]:
            del self._mem[key]

    def incremental_vacuum(self, pages=INCREMENTAL_VACUUM_PAGES):
        """Return freed pages to the OS cleanly when incremental auto_vacuum is enabled."""
        if type(pages) is not int or pages <= 0:
            return
        try:
            row = self.db.execute('PRAGMA auto_vacuum').fetchone()
            if row is not None and row[0] == 2:
                remaining = pages
                steps = 0
                while remaining > 0 and steps < pages:
                    steps += 1
                    free_row = self.db.execute('PRAGMA freelist_count').fetchone()
                    free = free_row[0] if free_row is not None else 0
                    if free <= 0:
                        break
                    if remaining >= INCREMENTAL_VACUUM_PAGES:
                        self.db.execute('PRAGMA incremental_vacuum(256)').fetchall()
                    else:
                        self.db.execute('PRAGMA incremental_vacuum(1)').fetchall()
                    after_row = self.db.execute('PRAGMA freelist_count').fetchone()
                    after = after_row[0] if after_row is not None else free
                    freed = free - after
                    if freed <= 0:
                        break
                    remaining -= freed
        except sqlite3.Error:
            pass

    def record_completion(self, source_identity, observation_end):
        """Record an in-transaction completion proof committed atomically with observation rows."""
        self.db.execute(
            'INSERT OR IGNORE INTO collection_completions (source_identity, observation_end) VALUES (?, ?)',
            (source_identity, observation_end),
        )

    def commit(self):
        self.db.commit()

    def rollback(self):
        self._mem.clear()
        self.db.rollback()

    def close(self):
        self.db.close()

    def _remember(self, cache_key, payload):
        if cache_key in self._mem:
            self._mem.move_to_end(cache_key)
        self._mem[cache_key] = payload
        while len(self._mem) > self.MAX_MEM_ENTRIES:
            self._mem.popitem(last=False)

    def points(self, key, step, times):
        times = sorted(set(times))
        if not times:
            return {}
        if key in VIRTUAL_QUERIES:
            # Approved/revoked private evidence is mutable even at the same
            # measurement endpoint. Never persist its conclusions as telemetry.
            if step != HOUR or len(times) != 1:
                raise CollectionError('Attestation requests require one current endpoint')
            t = times[0]
            response = access.aggregate_response(self.source.query(key, t - HOUR, t, HOUR), t - HOUR, t, HOUR)
            rows = [{'metric': row['metric'], 'values': [point for point in row['values'] if point[0] == t]}
                    for row in response['data']['result'] if any(point[0] == t for point in row['values'])]
            return {t: {'status': 'success', 'data': {'resultType': 'matrix', 'result': rows}}}
        identity = self.source.identity
        query_sha = sha256_hex(self.queries[key].encode())
        found = {}
        db_needed = []
        for t in times:
            cache_key = (identity, query_sha, step, t)
            cached = self._mem.get(cache_key)
            if cached is not None:
                self._mem.move_to_end(cache_key)
                found[t] = cached
            else:
                db_needed.append(t)
        for start in range(0, len(db_needed), 500):
            chunk = db_needed[start:start + 500]
            placeholders = ','.join('?' * len(chunk))
            for endpoint, encoded in self.db.execute(
                    f'SELECT endpoint,payload FROM observations WHERE source=? AND query_sha=? AND step=? AND endpoint IN ({placeholders})',
                    (identity, query_sha, step, *chunk)):
                payload = access.aggregate_response(parse_json(encoded), endpoint, endpoint, step)
                self._remember((identity, query_sha, step, endpoint), payload)
                found[endpoint] = payload
        missing = deque(t for t in times if t not in found)
        maximum_points = POPULATION_BATCH_POINTS if key.startswith('relay_population_') else access.MAX_POINTS
        while missing:
            batch = [missing.popleft()]
            while (missing and missing[0] == batch[-1] + step and len(batch) < maximum_points
                   and missing[0] - batch[0] <= access.MAX_RANGE):
                batch.append(missing.popleft())
            begin, end = batch[0], batch[-1]
            if begin == end:
                begin -= step
            if begin < 0:
                raise CollectionError('Source range begins before the supported epoch')
            response = access.aggregate_response(self.source.query(key, begin, end, step), begin, end, step)
            per_time = {t: [] for t in batch}
            for row in response['data']['result']:
                for t, value in row['values']:
                    if t in per_time:
                        per_time[t].append({'metric': row['metric'], 'values': [[t, value]]})
            batch_payloads = {
                t: {'status': 'success', 'data': {'resultType': 'matrix', 'result': rows}}
                for t, rows in per_time.items()
            }
            inserts = [
                (identity, query_sha, step, t, json.dumps(payload, separators=(',', ':')))
                for t, payload in batch_payloads.items()
            ]
            self.db.executemany('INSERT INTO observations VALUES (?,?,?,?,?)', inserts)
            for t, payload in batch_payloads.items():
                self._remember((identity, query_sha, step, t), payload)
                found[t] = payload
        return {t: _clone_payload(found[t]) for t in times}


def selected(payload, labels, count=False):
    rows = [row for row in payload['data']['result'] if row['metric'] == labels]
    _require(len(rows) <= 1, 'Duplicate aggregate label set')
    return number(rows[0]['values'][0][1], count) if rows else None


def series_labels(chart, series):
    if chart['id'] in SCALARS:
        return {}
    if chart['id'] in ALIASES:
        return {'type': ALIASES[chart['id']]}
    if chart['id'] == 'incoming_connections':
        return {'state': 'created' if series['key'] == 'accepted' else series['key']}
    return {('action' if chart['id'] in {'hsdir_fetches', 'hsdir_uploads'} else 'type'): series['key']}


def _chart_routes_available(chart_id, routes):
    required = [
        f'{chart_id}.{period}.{stat_name}'
        for period in ('current_hour', 'sampled_hour', 'daily', 'current24')
        for stat_name in ('available', 'eligible', 'value')
    ] + [f'{chart_id}.relay_population.hour', f'{chart_id}.relay_population.day']
    return all(op in routes for op in required)


def _ensure_supplemental_charts(data, routes, added=None):
    """Append missing supplemental charts and family memberships when routes are declared."""
    charts = data.setdefault('charts', [])
    existing = {c['id'] for c in charts}
    families = data.get('families')
    by_family = {f.get('id'): f for f in families} if isinstance(families, list) else {}
    fleet = data.get('fleet', 0)
    expected_default = max(8, int(fleet) if isinstance(fleet, (int, float)) and not isinstance(fleet, bool) else 0)
    missing_routes_cids = set()
    for spec in SUPPLEMENTAL_CHART_SPECS:
        cid = spec['id']
        if not _chart_routes_available(cid, routes):
            missing_routes_cids.add(cid)
            continue
        if cid not in existing:
            empty_cur = {'value': None, 'relays': None, 'expected_relays': expected_default,
                         'coverage': None, 'valid': False}
            chart = {
                'id': cid,
                'family': spec['family'],
                'family_id': spec['family_id'],
                'title': spec['title'],
                'definition': spec['definition'],
                'note': spec['note'],
                'unit': spec['unit'],
                'display_unit': spec['display_unit'],
                'headline_unit': spec['headline_unit'],
                'display_scale': spec['display_scale'],
                'kind': spec['kind'],
                'cohort_key': spec['cohort_key'],
                'metrics': list(spec['metrics']),
                'metric_origin': spec['metric_origin'],
                'introduced_note': spec['introduced_note'],
                'primary_series': spec['primary_series'],
                'display_keys': list(spec['display_keys']),
                'history_status': 'ready',
                'relays': fleet,
                'start': data.get('start'),
                'end': data.get('end'),
                'series': [
                    {
                        'key': key,
                        'name': name,
                        'color': color,
                        'history_status': 'queried',
                        'history': {'daily': [], 'sampled_hour': []},
                        'current': {'hour': dict(empty_cur), 'day': dict(empty_cur)},
                    }
                    for key, name, color in spec['series']
                ],
            }
            charts.append(chart)
            existing.add(cid)
            if added is not None:
                added.add(cid)
        family = by_family.get(spec['family_id'])
        if family is not None and isinstance(family.get('children'), list) and cid not in family['children']:
            family['children'].append(cid)
    if missing_routes_cids:
        family_primary = {f.get('id'): f.get('primary') for f in families} if isinstance(families, list) else {}
        removable = {
            c.get('id') for c in charts
            if c.get('id') in missing_routes_cids and family_primary.get(c.get('family_id')) != c.get('id')
        }
        if removable:
            data['charts'] = [c for c in charts if c.get('id') not in removable]
            if isinstance(families, list):
                for fam in families:
                    if isinstance(fam.get('children'), list):
                        fam['children'] = [cid for cid in fam['children'] if cid not in removable]
    return data


def population_row(payload, metric_type, end, expected_total):
    """Missing or contradictory metadata must not change measured observations."""
    bins = {}
    for row in payload['data']['result']:
        labels = row['metric']
        if labels.get('type') != metric_type:
            continue
        label = labels.get('action', '')
        _require(set(labels) == {'type', 'action'} and _POP_CODE_RE.fullmatch(label), 'Invalid population classification code')
        code = int(label)
        _require(code < 64 and (code == 0 or code % 2 != 0) and code not in bins,
                 'Duplicate population classification bin' if code in bins else 'Invalid population classification code')
        bins[code] = number(row['values'][0][1], count=True)
    total = sum(bins.values())
    if not bins or total != expected_total:
        return None
    classified = total - bins.get(0, 0)
    guard, exits, hsdir, v2dir = (sum(n for code, n in bins.items() if code & bit)
                                 for bit in (2, 4, 8, 16))
    return [end, total, classified, guard, bins.get(33, 0), exits, hsdir, v2dir]


def populations(times, eligible, available):
    """31-day maximum on the supplied measurement grid, including each end."""
    ordered = iter(sorted((t, n) for t, n in eligible.items() if n is not None))
    pending = next(ordered, None); maxima = deque(); result = {}
    for t in sorted(times):
        while pending is not None and pending[0] <= t:
            while maxima and maxima[-1][1] <= pending[1]:
                maxima.pop()
            maxima.append(pending)
            pending = next(ordered, None)
        while maxima and maxima[0][0] <= t - 31 * DAY:
            maxima.popleft()
        result[t] = max(1, available.get(t) or 0, maxima[0][1] if maxima else 0)
    return result


def measurement(value, n, expected):
    coverage = n / expected if n is not None else None
    valid = value is not None and n is not None and n > 0 and coverage is not None and .9 <= coverage <= 1
    return {'value': value, 'relays': n, 'expected_relays': expected, 'coverage': coverage, 'valid': valid}


def iso(t):
    _require(not isinstance(t, bool) and finite_number(t) and t >= 0,
             'Timestamp is outside the supported calendar range')
    try:
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, TypeError, ValueError) as error:
        raise CollectionError('Timestamp is outside the supported calendar range') from error


class Collector:
    def __init__(self, source, cache, queries, routes, seed):
        self.source, self.cache, self.queries, self.routes, self.seed = source, cache, queries, routes, seed

    def operation(self, name, step, times):
        return self.cache.points(self.routes[name], step, times)

    def endpoint(self, name, end):
        return self.operation(name, HOUR, [end])[end]

    def scalar(self, name, end):
        return selected(self.endpoint(name, end), {}, count=True)

    def values(self, chart, series, period, statistic, step, times):
        return {t: selected(p, series_labels(chart, series), count=statistic != 'value')
                for t, p in self.operation(f'{chart["id"]}.{period}.{statistic}', step, times).items()}

    def _fill_history_rows(self, chart, series, period, step, window_seconds, times, rows, *,
                           chart_c=0, enforce_chart_floor=False):
        if not times:
            return {}, None
        n = self.values(chart, series, period, 'eligible', step, times)
        a = self.values(chart, series, period, 'available', step, times)
        v = self.values(chart, series, period, 'value', step, times)
        history_n = {int(row[0]): row[2] for row in rows if row[2] is not None}
        history_n.update({t: val for t, val in n.items() if val is not None})
        e = populations(times, history_n, a)
        last_exp_c = None
        kind = chart['kind']
        for t in times:
            n_raw, e_raw, v_raw = n[t], e[t], v[t]
            n_c = None if n_raw is None else coarsen_count(n_raw)
            floor = chart_c if (enforce_chart_floor and t == chart['end']) else 0
            exp_c = max(8, floor, 0 if n_c is None else n_c, coarsen_count(e_raw))
            cov_c = (n_c / exp_c) if (n_c is not None and n_c > 0) else None
            if n_c in (None, 0):
                v_c, valid_c = None, False
            else:
                v_c = coarsen_value(chart['id'], series['key'], kind, v_raw, window_seconds=window_seconds)
                valid_c = bool(v_c is not None and n_c >= 8 and cov_c is not None
                               and MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0)
            if t == chart['end'] and not valid_c:
                v_c = None
            rows.append([t, v_c, n_c, cov_c, valid_c])
            if t == chart['end']:
                last_exp_c = exp_c
        return n, last_exp_c

    def relay_population(self, chart, series, end, fetched_raw_n, fetched_hour_raw_n, fetched_cur24_raw_n):
        prefix = f'{chart["id"]}.relay_population.'
        metric_type = next(iter(series_labels(chart, series).values()), 'scalar')
        result = {'method': POPULATION_METHOD,
                  'queries': {period: self.queries[self.routes[prefix + period]] for period in ('hour', 'day')},
                  'hour': None, 'day': None, 'daily': []}
        expected_c = {int(row[0]): row[2] for row in series['history']['daily'] if row[4]}
        retained = series.get('relay_population', {})
        rows = {}
        retained_day_ok = (retained.get('method') == result['method']
                           and retained.get('queries', {}).get('day') == result['queries']['day'])
        retained_hour_ok = (retained.get('method') == result['method']
                            and retained.get('queries') == result['queries'])
        if retained_day_ok:
            # Reuse immutable already-coarsened daily rows from the validated seed without re-coarsening.
            rows = {row[0]: row[:] for row in retained.get('daily', [])
                    if row[0] in expected_c and row[1] == expected_c[row[0]]}
        targets = {t: expected_c[t] for t in sorted(expected_c.keys() - rows.keys())}
        if targets:
            raw_n = dict(fetched_raw_n)
            missing_n = [t for t in targets if t not in raw_n]
            if missing_n:
                raw_n.update(self.values(chart, series, 'daily', 'eligible', DAY, missing_n))
            for t, payload in self.operation(prefix + 'day', DAY, sorted(targets)).items():
                n_raw, target_n = raw_n.get(t), targets[t]
                if n_raw is None or coarsen_count(n_raw) != target_n:
                    continue
                crow = coarsen_population_row(population_row(payload, metric_type, t, n_raw), target_n)
                if crow is not None:
                    rows[t] = crow

        cur_day_n_c = series['current']['day'].get('relays')
        if cur_day_n_c is not None:
            reuse_seed_day = (not fetched_raw_n and not fetched_cur24_raw_n and end == self.seed['end']
                              and end % DAY == 0 and retained_day_ok
                              and retained.get('day') is not None and retained['day'][1] == cur_day_n_c)
            if end in rows:
                result['day'] = rows[end][:]
            elif reuse_seed_day:
                result['day'] = retained['day'][:]
            else:
                n_raw = fetched_cur24_raw_n.get(end, fetched_raw_n.get(end))
                if n_raw is None:
                    n_raw = self.values(chart, series, 'current24', 'eligible', HOUR, [end]).get(end)
                if n_raw is not None and coarsen_count(n_raw) == cur_day_n_c:
                    crow = coarsen_population_row(population_row(self.endpoint(prefix + 'day', end),
                                                                 metric_type, end, n_raw), cur_day_n_c)
                    if crow is not None:
                        result['day'] = crow

        cur_hour_n_c = series['current']['hour'].get('relays')
        if cur_hour_n_c is not None:
            reuse_seed_hour = (not fetched_hour_raw_n and end == self.seed['end'] and retained_hour_ok
                               and retained.get('hour') is not None and retained['hour'][1] == cur_hour_n_c)
            if reuse_seed_hour:
                result['hour'] = retained['hour'][:]
            else:
                n_raw = fetched_hour_raw_n.get(end)
                if n_raw is None:
                    n_raw = self.values(chart, series, 'current_hour', 'eligible', HOUR, [end]).get(end)
                if n_raw is not None and coarsen_count(n_raw) == cur_hour_n_c:
                    crow = coarsen_population_row(population_row(self.endpoint(prefix + 'hour', end),
                                                                 metric_type, end, n_raw), cur_hour_n_c)
                    if crow is not None:
                        result['hour'] = crow

        result['daily'] = [rows[t] for t in sorted(rows)]
        return result

    def versions(self, end, raw_totals, cohort_totals):
        originals = {v['build_id']: {k: value for k, value in v.items() if k != 'relays'}
                     for v in self.seed['versions'].get('verified_runtime', [])}
        approved = {(r['metric']['build_id'], r['metric']['sha256'])
                    for r in self.seed['versions'].get('binary_hash_counts', [])}
        raw_cohorts, descriptions = {}, {}
        binary_keys, config_keys, attested_keys = set(), set(), set()
        binary_raw, config_raw = {}, {}
        proofs = self.endpoint('version.attestation_proof_metadata', end)['data']['result']
        source_hashes, observed, proof_times = {}, {}, []
        for row in proofs:
            labels = row['metric']; label = labels.get('type'); digest = labels.get('sha256')
            _require(label in PROOF_LABELS and is_sha256(digest), 'Unexpected public attestation metadata')
            _require(label not in source_hashes or source_hashes[label] == digest,
                     'Conflicting attestation source digest')
            source_hashes[label] = digest
            if label.startswith('daemon_group_'):
                build_id = labels.get('build_id')
                _require(nonblank(build_id), 'Attestation proof missing build identifier')
                t = number(row['values'][0][1]); proof_times.append(t)
                observed.setdefault(build_id, []).append(t)
        for cohort, total in raw_totals.items():
            counts = defaultdict(int)
            bin_counts = defaultdict(int)
            cfg_counts = defaultdict(int)
            for row in self.endpoint(f'cohort.{cohort}_verified_binary', end)['data']['result']:
                labels = row['metric']; build_id = labels.get('build_id')
                if (build_id, labels.get('sha256')) not in approved or build_id not in originals:
                    continue
                definition = originals[build_id]
                key = ('patched_' if definition.get('patched') else 'stock_') + _SLUG_SUB_RE.sub('_', definition['version'].lower()).strip('_')
                n = number(row['values'][0][1], True)
                counts[key] += n
                bin_counts[key] += n
                binary_keys.add(key)
                descriptions[key] = dict(definition, verification='Approved recorded binary hash matched a fresh process epoch.')
            stock_op = f'cohort.{cohort}_stock_config'
            if stock_op in self.routes:
                for row in self.endpoint(stock_op, end)['data']['result']:
                    version = row['metric'].get('tor_version', '')
                    _require(_TOR_VERSION_RE.fullmatch(version), 'Unexpected public stock Tor version')
                    n = number(row['values'][0][1], True)
                    key = 'stock_' + _SLUG_SUB_RE.sub('_', version.lower()).strip('_')
                    counts[key] += n
                    cfg_counts[key] += n
                    config_keys.add(key)
                    if key not in descriptions:
                        descriptions[key] = {
                            'build_id': 'stock-' + version.replace('.', '_'),
                            'version': version,
                            'base_version': version,
                            'patched': False,
                            'verification': 'Live non-custom process version from Prometheus config inventory.',
                        }
            for row in self.endpoint(f'cohort.{cohort}_attested', end)['data']['result']:
                build_id = row['metric'].get('build_id', '')
                _require(_ATTESTED_BUILD_RE.fullmatch(build_id) and build_id in observed, 'Unexpected public attested build')
                version = build_id.removeprefix('attested-').replace('_', '.')
                key = build_id.replace('-', '_')
                counts[key] += number(row['values'][0][1], True)
                attested_keys.add(key)
                descriptions[key] = {'build_id': build_id, 'version': version, 'base_version': version,
                    'verification': 'Daemon version observed at ' + ', '.join(iso(t) for t in sorted(set(observed[build_id])))
                                    + '; process epochs matched the measured hour. No live binary hash claimed.'}
            known = sum(counts.values())
            _require(known <= total, 'Runtime cohort exceeds its observed population')
            raw_cohorts[cohort] = dict(counts, runtime_unverified=total - known)
            binary_raw[cohort] = dict(bin_counts)
            config_raw[cohort] = dict(cfg_counts)
        _require(self.endpoint('version.attestation_proof_metadata', end)['data']['result'] == proofs,
                 'Runtime attestations changed during collection')
        keys = sorted({key for counts in raw_cohorts.values() for key in counts})
        for key in keys:
            if key == 'runtime_unverified' or key in attested_keys:
                continue
            has_bin = any(binary_raw[c].get(key, 0) > 0 for c in binary_raw)
            has_cfg = any(config_raw[c].get(key, 0) > 0 for c in config_raw)
            if has_bin and has_cfg:
                descriptions[key]['verification'] = (
                    'Approved recorded binary hash matched a fresh process epoch; '
                    'additional stock relays from live Prometheus config inventory.'
                )
            elif has_bin:
                descriptions[key]['verification'] = 'Approved recorded binary hash matched a fresh process epoch.'
            elif key in config_keys:
                descriptions[key]['verification'] = 'Live non-custom process version from Prometheus config inventory.'
        labels = {key: ('Version not recorded' if key == 'runtime_unverified' else
                       ('Patched Tor ' if descriptions[key].get('patched') else 'Tor ') + descriptions[key]['version'])
                  for key in keys}
        cohorts = {
            cohort: coarsen_cohort_mapping({key: raw_cohorts[cohort].get(key, 0) for key in keys}, target)
            for cohort, target in cohort_totals.items()
        }
        native = cohorts['native']
        native_c = cohort_totals['native']
        verified_c = sum(native[k] for k in keys if k != 'runtime_unverified')
        binary_hash_c = sum(
            coarsen_partition([binary_raw['native'].get(k, 0), config_raw['native'].get(k, 0)], native[k])[0]
            for k in binary_keys
            if native.get(k, 0) > 0
        )
        attested_c = sum(native[k] for k in attested_keys if k in native)
        result = {'as_of': end, 'native_total': native_c, 'runtime_verified_total': verified_c,
                  'runtime_unverified_relays': native['runtime_unverified'], 'runtime_binary_hash_verified_total': binary_hash_c,
                  'verified_runtime': [dict(descriptions[key], relays=n) for key, n in native.items()
                                       if key != 'runtime_unverified' and any(cohorts[c].get(key, 0) > 0 for c in cohorts)],
                  'metric_cohorts': cohorts, 'cohort_labels': labels}
        if 'binary_hash_counts' in self.seed['versions']:
            result['binary_hash_counts'] = copy.deepcopy(self.seed['versions']['binary_hash_counts'])
        if proof_times and attested_keys:
            result['runtime_resolution'] = {'observed_at': iso(min(proof_times)), 'resolved_relays': attested_c,
                'matched_process_epochs': attested_c, 'snapshot_start': end - HOUR, 'snapshot_end': end, 'source_sha256': source_hashes}
        return result

    def dos_thresholds(self, end, fleet_c):
        eff_rows = self.endpoint('dos.effective_config', end)['data']['result']
        bg_rows = self.endpoint('dos.begindir_config', end)['data']['result']

        eff_total = 0
        eff_buckets = {key: defaultdict(int) for key, _, _, _ in DOS_EFFECTIVE_CONFIG_SETTINGS}
        eff_raw_totals = {key: 0 for key, _, _, _ in DOS_EFFECTIVE_CONFIG_SETTINGS}
        for row in eff_rows:
            metric = row['metric']
            cnt = number(row['values'][0][1], count=True)
            eff_total += cnt
            for key, _, _, _ in DOS_EFFECTIVE_CONFIG_SETTINGS:
                source_key = f'{key}_source'
                if source_key not in metric:
                    continue
                source = metric[source_key]
                _require(source in {'consensus', 'torrc', 'unset'}, 'Unexpected DoS config source')
                if source == 'unset':
                    val = metric.get(key, 'unset')
                    _require(val == 'unset' or (isinstance(val, str) and _DOS_DIGITS_RE.fullmatch(val)), 'Unexpected DoS config value')
                else:
                    val = metric.get(key)
                    _require(isinstance(val, str) and _DOS_DIGITS_RE.fullmatch(val), 'Unexpected DoS config value')
                eff_buckets[key][(val, source)] += cnt
                eff_raw_totals[key] += cnt

        bg_buckets = {option: defaultdict(int) for _, option, _, _ in DOS_BEGINDIR_CONFIG_SETTINGS}
        bg_raw_totals = {option: 0 for _, option, _, _ in DOS_BEGINDIR_CONFIG_SETTINGS}
        for row in bg_rows:
            metric = row['metric']
            option = metric.get('option')
            val = metric.get('value')
            _require(isinstance(option, str) and option in bg_buckets, 'Unexpected BeginDir config option')
            _require(isinstance(val, str) and _BEGINDIR_VAL_RE.fullmatch(val), 'Unexpected BeginDir config value')
            cnt = number(row['values'][0][1], count=True)
            bg_buckets[option][(val, 'observed')] += cnt
            bg_raw_totals[option] += cnt

        settings = []
        for key, option, family_id, unit in DOS_EFFECTIVE_CONFIG_SETTINGS:
            relays_c, values, suppressed_c = coarsen_threshold_buckets(
                sorted(eff_buckets[key].items()), eff_raw_totals[key], fleet_c,
            )
            settings.append({
                'key': key, 'option': option, 'family_id': family_id, 'unit': unit,
                'relays': relays_c, 'values': values, 'suppressed_relays': suppressed_c,
            })
        for key, option, family_id, unit in DOS_BEGINDIR_CONFIG_SETTINGS:
            relays_c, values, suppressed_c = coarsen_threshold_buckets(
                sorted(bg_buckets[option].items()), bg_raw_totals[option], fleet_c,
            )
            settings.append({
                'key': key, 'option': option, 'family_id': family_id, 'unit': unit,
                'relays': relays_c, 'values': values, 'suppressed_relays': suppressed_c,
            })
        return {
            'as_of': end,
            'reported_relays': min(fleet_c, coarsen_count(eff_total)),
            'settings': settings,
        }

    def collect(self, end, seed_sha):
        data = copy.deepcopy(self.seed)
        data.update(input_schema_version=4, start=end - DAY, end=end, window_seconds=HOUR,
                    minimum_samples=24, expected_samples=30, generated_at=iso(time.time()))
        data.setdefault('history_methods', {})['coarsening'] = COARSENING_DESCRIPTION
        added_supplemental = set()
        _ensure_supplemental_charts(data, self.routes, added=added_supplemental)
        charts_by_id = {c['id']: c for c in data['charts']}
        raw_totals = {}
        for cohort, chart_id, key in COHORT_ANCHORS:
            chart = charts_by_id.get(chart_id)
            _require(chart is not None, f'Missing cohort anchor chart: {chart_id}')
            series = next((s for s in chart['series'] if s['key'] == key), None)
            _require(series is not None, f'Missing cohort anchor series: {chart_id}.{key}')
            total = self.values(chart, series, 'current_hour', 'available', HOUR, [end])[end]
            raw_totals[cohort] = total if total is not None else 0
        cohort_totals = {cohort: coarsen_count(total) for cohort, total in raw_totals.items()}
        data.update(fleet=cohort_totals['native'], custom=cohort_totals['custom'], defense=cohort_totals['directory_defense'])
        current_times = list(range(end - DAY + HOUR, end + 1, HOUR))
        context_times = list(range(end - POPULATION_CONTEXT_DAYS * DAY, end + 1, HOUR))
        target_day = (end // DAY) * DAY
        target_sh = (end // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP
        seed_first_daily = min(
            (int(s['history']['daily'][0][0])
             for c in data['charts'] for s in c.get('series', [])
             if s.get('history', {}).get('daily')),
            default=None,
        )
        for chart in data['charts']:
            seed_chart_relays = chart['relays']
            chart_c = cohort_totals[chart['cohort_key']]
            kind = {'counter_daily_rate': 'counter_hour_rate', 'gauge_daily_mean': 'gauge_hour_mean'}.get(
                chart.get('kind'), chart.get('kind'))
            chart.update(start=data['start'], end=end, relays=chart_c, kind=kind)
            chart['query'] = self.queries[self.routes[f'{chart["id"]}.current_hour.value']]
            chart['quality_query'] = self.queries[self.routes[f'{chart["id"]}.current_hour.eligible']]
            chart['history_queries'] = {
                period: {stat_name: self.queries[self.routes[f'{chart["id"]}.{period}.{stat_name}']]
                         for stat_name in ('value', 'eligible', 'available')}
                for period in ('daily', 'sampled_hour', 'current24')
            }
            chart_first_daily = min(
                (int(s['history']['daily'][0][0]) for s in chart['series'] if s['history']['daily']),
                default=seed_first_daily,
            )
            for series in chart['series']:
                rows = series['history']['daily']
                sh_rows = series['history'].setdefault('sampled_hour', [])

                hourly_n = self.values(chart, series, 'current_hour', 'eligible', HOUR, context_times)
                hourly_a = self.values(chart, series, 'current_hour', 'available', HOUR, current_times)
                hourly_v = self.values(chart, series, 'current_hour', 'value', HOUR, current_times)
                retained_n = {int(row[0]): row[2] for row in sh_rows if row[2] is not None}
                retained_n.update({int(t): n_val for t, n_val in series.get('hourly_relays', []) if n_val is not None})
                retained_n.update({t: val for t, val in hourly_n.items() if val is not None})
                expected_hr = populations(current_times, retained_n, hourly_a)
                values_rows, hr_rows, hexp_rows = [], [], []
                hour_current = None
                for t in current_times:
                    n_raw, e_raw, v_raw = hourly_n[t], expected_hr[t], hourly_v[t]
                    n_c = None if n_raw is None else coarsen_count(n_raw)
                    exp_c = max(8, 0 if n_c is None else n_c, coarsen_count(e_raw))
                    cov_c = (n_c / exp_c) if (n_c is not None and n_c > 0) else None
                    if n_c in (None, 0):
                        v_c, valid_c = None, False
                    else:
                        v_c = coarsen_value(chart['id'], series['key'], kind, v_raw, window_seconds=HOUR)
                        valid_c = bool(v_c is not None and n_c >= 8 and cov_c is not None
                                       and MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0)
                    values_rows.append([t, v_c if valid_c else None])
                    hr_rows.append([t, n_c])
                    hexp_rows.append([t, exp_c])
                    if t == end:
                        hour_current = {
                            'value': v_c if valid_c else None, 'relays': n_c,
                            'expected_relays': exp_c, 'coverage': cov_c, 'valid': valid_c,
                        }
                series['values'] = values_rows
                series['hourly_relays'] = hr_rows
                series['hourly_expected_relays'] = hexp_rows

                fetched_raw_n = {}
                last_exp_c = None
                had_empty_daily = not rows
                final_daily_end = int(rows[-1][0]) if rows else None
                daily_start = (int(rows[-1][0]) + DAY) if rows else chart_first_daily
                has_daily_history = bool(rows or (daily_start is not None and daily_start <= target_day))
                if daily_start is not None:
                    daily_times = list(range(daily_start, target_day + 1, DAY))
                    if daily_times:
                        final_daily_end = daily_times[-1]
                    fetched_raw_n, last_exp_c = self._fill_history_rows(
                        chart, series, 'daily', DAY, DAY, daily_times, rows,
                        chart_c=chart_c, enforce_chart_floor=True,
                    )
                    if had_empty_daily and rows:
                        first_measured = next((i for i, row in enumerate(rows) if row[2] is not None), None)
                        if first_measured is None:
                            rows.clear()
                        elif first_measured > 0:
                            del rows[:first_measured]
                if rows:
                    if not sh_rows:
                        raw_first = max(int(rows[0][0]) - DAY + SAMPLED_HOUR_STEP,
                                        target_sh - SAMPLED_HOUR_BACKFILL_MAX_DAYS * DAY)
                        first_sh = ((raw_first + SAMPLED_HOUR_STEP - 1) // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP
                        sh_times = list(range(first_sh, target_sh + 1, SAMPLED_HOUR_STEP))
                    else:
                        sh_times = list(range(int(sh_rows[-1][0]) + SAMPLED_HOUR_STEP, target_sh + 1, SAMPLED_HOUR_STEP))
                    self._fill_history_rows(chart, series, 'sampled_hour', SAMPLED_HOUR_STEP, HOUR, sh_times, sh_rows)

                fetched_cur24_raw_n = {}
                if rows and end % DAY == 0 and rows[-1][0] == end:
                    if fetched_raw_n:
                        last_row = rows[-1]
                        day_current = {
                            'value': last_row[1] if last_row[4] else None, 'relays': last_row[2],
                            'expected_relays': last_exp_c, 'coverage': last_row[3], 'valid': last_row[4],
                        }
                    else:
                        cur = series['current']['day']
                        had_exp = 'expected_relays' in cur
                        cur_exp = cur['expected_relays'] if had_exp else max(8, seed_chart_relays)
                        if chart_c > cur_exp:
                            cur_n, cur_val = cur.get('relays'), cur.get('value')
                            new_exp = max(8, chart_c, 0 if cur_n is None else cur_n)
                            cov = (cur_n / new_exp) if (cur_n is not None and cur_n > 0) else None
                            cur_valid = cur.get('valid')
                            if cur_valid is None:
                                cur_valid = bool(rows[-1][4])
                            new_valid = bool(cur_valid and cur_val is not None and cur_n is not None
                                             and cur_n >= 8 and cov is not None
                                             and MINIMUM_POPULATION_COVERAGE <= cov <= 1.0)
                            new_val = cur_val if new_valid else None
                            day_current = {'value': new_val, 'relays': cur_n, 'expected_relays': new_exp,
                                           'coverage': cov, 'valid': new_valid}
                            rows[-1] = [end, new_val, cur_n, cov, new_valid]
                        else:
                            day_current = dict(cur)
                            if not had_exp and max(8, chart_c) != cur_exp:
                                day_current['expected_relays'] = cur_exp
                elif has_daily_history and not (end % DAY == 0 and final_daily_end == end):
                    n24 = self.values(chart, series, 'current24', 'eligible', HOUR, [end])
                    a24 = self.values(chart, series, 'current24', 'available', HOUR, [end])
                    v24 = self.values(chart, series, 'current24', 'value', HOUR, [end])
                    fetched_cur24_raw_n = {end: n24[end]} if n24[end] is not None else {}
                    history_n = {int(row[0]): row[2] for row in rows if row[2] is not None}
                    if n24[end] is not None:
                        history_n[end] = n24[end]
                    e24 = populations([end], history_n, {end: a24[end]})[end]
                    n_c = None if n24[end] is None else coarsen_count(n24[end])
                    exp_c = max(8, chart_c, 0 if n_c is None else n_c, coarsen_count(e24))
                    cov_c = (n_c / exp_c) if (n_c is not None and n_c > 0) else None
                    if n_c in (None, 0):
                        v_c, valid_c = None, False
                    else:
                        v_c = coarsen_value(chart['id'], series['key'], kind, v24[end], window_seconds=DAY)
                        valid_c = bool(v_c is not None and n_c >= 8 and cov_c is not None
                                       and MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0)
                    day_current = {'value': v_c if valid_c else None, 'relays': n_c,
                                   'expected_relays': exp_c, 'coverage': cov_c, 'valid': valid_c}
                else:
                    day_current = {'value': None, 'relays': None,
                                   'expected_relays': max(8, chart_c, hexp_rows[-1][1]),
                                   'coverage': None, 'valid': False}
                series['current'] = {'hour': hour_current, 'day': day_current}
                fetched_hr_map = {t: val for t, val in hourly_n.items() if t == end and val is not None}
                series['relay_population'] = self.relay_population(
                    chart, series, end, fetched_raw_n, fetched_hr_map, fetched_cur24_raw_n)
        if added_supplemental:
            families = data.get('families')
            family_primary = {f.get('id'): f.get('primary') for f in families} if isinstance(families, list) else {}
            keep_charts = []
            dropped_cids = set()
            for chart in data['charts']:
                cid = chart['id']
                if cid in added_supplemental and family_primary.get(chart.get('family_id')) != cid:
                    observed = [
                        s for s in chart['series']
                        if s['history'].get('daily') and s['history'].get('sampled_hour')
                    ]
                    obs_keys = {s['key'] for s in observed}
                    if not obs_keys or chart.get('primary_series') not in obs_keys:
                        dropped_cids.add(cid)
                        continue
                    if len(observed) < len(chart['series']):
                        chart['series'] = observed
                        chart['display_keys'] = [k for k in chart['display_keys'] if k in obs_keys]
                keep_charts.append(chart)
            if dropped_cids:
                data['charts'] = keep_charts
                if isinstance(families, list):
                    for fam in families:
                        if isinstance(fam.get('children'), list):
                            fam['children'] = [c for c in fam['children'] if c not in dropped_cids]
        data['versions'] = self.versions(end, raw_totals, cohort_totals)
        data['snapshot_flags'] = {}
        for key in METADATA_KEYS:
            rows = [{'metric': row['metric'], 'value': [row['values'][0][0], str(coarsen_count(number(row['values'][0][1], True)))]}
                    for row in self.endpoint('metadata.' + key, end)['data']['result']]
            data['snapshot_flags'][key] = {'query': self.queries[self.routes['metadata.' + key]],
                'response': {'status': 'success', 'data': {'resultType': 'vector', 'result': rows}}}
        flags = {}
        for row in data['snapshot_flags']['flags']['response']['data']['result']:
            flag_type = row['metric'].get('type')
            _require(isinstance(flag_type, str) and bool(flag_type), 'Aggregate flag observation missing type')
            flags[flag_type] = int(row['value'][1])
        data['flags'] = flags
        seed_dir = data.get('dircache_cohort', {})
        directory = {key: seed_dir[key] for key in ('method', 'source') if key in seed_dir}
        for field, operation in DIRCACHE_OPERATIONS:
            if (val := self.scalar('dircache.' + operation, end)) is not None:
                directory[field] = coarsen_count(val)
        data['dircache_cohort'] = directory
        if 'dos.effective_config' in self.routes and 'dos.begindir_config' in self.routes:
            data['dos_thresholds'] = self.dos_thresholds(end, cohort_totals['native'])
        else:
            data.pop('dos_thresholds', None)
        data['sources']['current_observation_end'] = end
        data['input_provenance'] = {'description': 'Hourly UTC fixed-query aggregates with dir-spec coarsening; retained histories seeded from the configured measurement bundle.',
                                    'snapshot_end': end, 'source_public_data_sha256': seed_sha}
        return data


def resolve_seed_path(config, app, seed=None):
    if seed is not None:
        _require(bool(os.fspath(seed).strip()), 'Seed snapshot path must not be blank')
        return expand_path(seed), True
    if config.get('SOURCE_SEED_SNAPSHOT'):
        return expand_path(config['SOURCE_SEED_SNAPSHOT']), True
    return app / 'inputs/snapshot.json', False


def _read_bounded(path, max_bytes, *, private=False):
    try:
        return read_regular_bytes(path, max_bytes, private=private)
    except ValueError as error:
        raise CollectionError(str(error)) from error


def _resolve_overlap_variants(path):
    expanded = expand_path(path)
    try:
        full_resolved = expanded.resolve()
        parent_resolved = expanded.parent.resolve() / expanded.name
    except OSError as error:
        raise CollectionError('Invalid path in overlap check') from error
    return (full_resolved,) if parent_resolved == full_resolved else (full_resolved, parent_resolved)


def _deepest_existing_decompose(path):
    """Return `(existing_ancestor, tail_parts)` for overlap comparison without modifying disk."""
    for idx, candidate in enumerate((path, *path.parents)):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if idx == 0 and stat.S_ISLNK(info.st_mode):
            continue
        return candidate, path.relative_to(candidate).parts
    return path, ()


def _is_case_insensitive_at(directory):
    target = directory if directory.is_dir() else directory.parent
    try:
        target_dev = target.stat().st_dev
        with os.scandir(target) as it:
            for entry in it:
                swapped = entry.name.swapcase()
                if swapped == entry.name:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    other = (target / swapped).lstat()
                except OSError:
                    return False
                return (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)
        for candidate in (target, *target.parents):
            name = candidate.name
            swapped = name.swapcase()
            if not name or swapped == name:
                continue
            if candidate.parent.stat().st_dev != target_dev:
                break
            try:
                info = candidate.stat()
                other = candidate.with_name(swapped).stat()
            except OSError:
                return False
            return (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)
    except OSError:
        pass
    return sys.platform == 'darwin'


def _variant_overlaps(a, b):
    if filesystem_contains(a, b) or filesystem_contains(b, a):
        return True
    anc_a, tail_a = _deepest_existing_decompose(a)
    anc_b, tail_b = _deepest_existing_decompose(b)
    try:
        info_a, info_b = anc_a.lstat(), anc_b.lstat()
    except OSError:
        return False
    if (info_a.st_dev, info_a.st_ino) != (info_b.st_dev, info_b.st_ino):
        return False
    if _is_case_insensitive_at(anc_a):
        tail_a = tuple(part.casefold() for part in tail_a)
        tail_b = tuple(part.casefold() for part in tail_b)
    return (tail_b[:len(tail_a)] == tail_a) if len(tail_a) <= len(tail_b) else (tail_a[:len(tail_b)] == tail_b)


def _paths_overlap(left, right):
    left_variants = _resolve_overlap_variants(left)
    right_variants = _resolve_overlap_variants(right)
    return any(_variant_overlaps(a, b) for a in left_variants for b in right_variants)


def _assert_disjoint(protected_paths, forbidden_paths, message):
    for candidate in protected_paths:
        for forbidden in forbidden_paths:
            if _paths_overlap(candidate, forbidden):
                raise CollectionError(message)


def _compute_source_identity(config_digest, known_hosts_digest, seed_sha, queries_sha, routes_sha):
    payload = json.dumps(
        {
            'config': config_digest,
            'known_hosts': known_hosts_digest,
            'queries': queries_sha,
            'routes': routes_sha,
            'seed': seed_sha,
        },
        allow_nan=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return sha256_hex(payload)


def _require_private_dir(path, missing_message, invalid_message):
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise CollectionError(missing_message) from error
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077), invalid_message)


def _open_readonly_cache(db_path, *, fail_if_missing=False, fail_if_unindexed_wal=False,
                         missing_message='Observation cache database is missing'):
    try:
        fd = os.open(db_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        if fail_if_missing:
            raise CollectionError(missing_message) from error
        return None
    except OSError as error:
        raise CollectionError('Observation cache must be a private owned regular file') from error
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not (info.st_mode & 0o077),
                 'Observation cache must be a private owned regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            header = stream.read(100) if info.st_size > 0 else b''
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    sidecars = {}
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = db_path.with_name(db_path.name + suffix)
        try:
            s_info = sidecar.lstat()
        except FileNotFoundError:
            sidecars[suffix] = False
        except OSError as error:
            raise CollectionError('Observation cache sidecar must be a private owned regular file') from error
        else:
            _require(stat.S_ISREG(s_info.st_mode) and s_info.st_uid == os.geteuid() and not (s_info.st_mode & 0o077),
                     'Observation cache sidecar must be a private owned regular file')
            sidecars[suffix] = True
    if info.st_size == 0:
        if fail_if_missing:
            raise CollectionError(missing_message)
        return None
    if len(header) < 20 or header[:16] != b'SQLite format 3\x00':
        raise CollectionError('Invalid observation cache database')
    # Never bypass locking with immutable=1; skip opening unindexed WAL caches that lack -shm so inspection stays strictly non-mutating.
    is_wal = header[18] == 2 or header[19] == 2
    if is_wal and not sidecars['-shm']:
        if fail_if_unindexed_wal:
            raise CollectionError('Observation cache WAL requires an active shared-memory index or checkpointed journal')
        return None
    try:
        return sqlite3.connect(f'{db_path.resolve(strict=True).as_uri()}?mode=ro',
                               uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as error:
        raise CollectionError('Invalid observation cache database') from error


def _cache_has_completion(db_path, source_id, obs_end, *, require_match=False):
    missing_msg = 'Observation cache must contain a committed completion record matching this configuration and endpoint'
    conn = _open_readonly_cache(
        db_path,
        fail_if_missing=require_match,
        fail_if_unindexed_wal=require_match,
        missing_message=missing_msg,
    )
    if conn is None:
        return False
    try:
        with closing(conn):
            has_obs = conn.execute('SELECT 1 FROM observations LIMIT 1').fetchone() is not None
            has_comp = conn.execute(
                'SELECT 1 FROM collection_completions WHERE source_identity=? AND observation_end=? LIMIT 1',
                (source_id, obs_end),
            ).fetchone() is not None
            return has_obs and has_comp
    except sqlite3.OperationalError as error:
        if not require_match and 'no such table' in str(error).lower():
            return False
        raise CollectionError('Invalid observation cache database') from error
    except sqlite3.Error as error:
        raise CollectionError('Invalid observation cache database') from error


def _inspect_warm_checkpoint(state, source_identity_sha256, now_hour):
    """Read state checkpoint and cache DB without mutating or creating state_dir."""
    if not os.path.lexists(state):
        return None
    _require_private_dir(state, 'Collector state directory is missing',
                         'Collector state directory must be private owned')
    checkpoint_path = state / 'checkpoint.json'
    cp = None
    if os.path.lexists(checkpoint_path):
        try:
            cp_raw = _read_bounded(checkpoint_path, MAX_CONFIG_BYTES, private=True)
        except OSError as error:
            raise CollectionError('Invalid collector checkpoint') from error
        cp = parse_json(cp_raw)
        _require(isinstance(cp, dict), 'Invalid collector checkpoint')
    obs_end = cp.get('observation_end') if cp is not None else None
    valid_cp = (
        cp is not None
        and cp.get('source_identity_sha256') == source_identity_sha256
        and not isinstance(obs_end, bool)
        and isinstance(obs_end, int)
        and obs_end > 0
        and obs_end % HOUR == 0
        and obs_end <= now_hour
    )
    has_completion = _cache_has_completion(
        state / 'observations.sqlite3',
        source_identity_sha256,
        obs_end if valid_cp else -1,
        require_match=False,
    )
    return obs_end if (valid_cp and has_completion) else None


def _batch_capacity(step, *, population=False):
    max_pts = POPULATION_BATCH_POINTS if population else access.MAX_POINTS
    return max(1, min(max_pts, 1 + (access.MAX_RANGE // step)))


def _contiguous_run_batches(times, step, *, population=False):
    ordered = sorted(set(times))
    if not ordered:
        return 0
    cap = _batch_capacity(step, population=population)
    batches = 0
    run_len = 1
    prev = ordered[0]
    for t in ordered[1:]:
        if t == prev + step:
            run_len += 1
        else:
            batches += math.ceil(run_len / cap)
            run_len = 1
        prev = t
    return batches + math.ceil(run_len / cap)


def _plan_max_requests(routes, queries, seed, end, *, warm_end, baseline_end):
    """Simulate Collector.collect traversal to bound worst-case sequential gateway calls."""
    cached = {}
    requests = 1  # Initial source.catalogue() handshake.
    effective_warm = min(warm_end, baseline_end) if warm_end is not None else None
    warm_day_cutoff = (effective_warm // DAY) * DAY if effective_warm is not None else None
    warm_sh_cutoff = (effective_warm // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP if effective_warm is not None else None

    def fetch(query_id, step, times, *, population=False):
        nonlocal requests
        if not times:
            return
        if query_id in VIRTUAL_QUERIES:
            requests += len(times)
            return
        seen = cached.setdefault((query_id, step), set())
        missing = [t for t in times if t not in seen]
        if not missing:
            return
        # Population requests cover only qualifying dates, so isolated dates can each need a request.
        requests += len(missing) if population else _contiguous_run_batches(missing, step)
        seen.update(missing)

    work_seed = copy.deepcopy(seed)
    _ensure_supplemental_charts(work_seed, routes)
    charts_by_id = {c['id']: c for c in work_seed['charts']}
    for _, chart_id, _ in COHORT_ANCHORS:
        if chart_id in charts_by_id:
            fetch(routes[f'{chart_id}.current_hour.available'], HOUR, [end])

    current_times = list(range(end - DAY + HOUR, end + 1, HOUR))
    context_times = list(range(end - POPULATION_CONTEXT_DAYS * DAY, end + 1, HOUR))
    target_day = (end // DAY) * DAY
    target_sh = (end // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP
    seed_first_daily = min(
        (int(s['history']['daily'][0][0])
         for c in work_seed['charts'] for s in c.get('series', [])
         if s.get('history', {}).get('daily')),
        default=None,
    )

    for chart in work_seed['charts']:
        cid = chart['id']
        cur_hr_elig_q = routes[f'{cid}.current_hour.eligible']
        cur_hr_avail_q = routes[f'{cid}.current_hour.available']
        cur_hr_val_q = routes[f'{cid}.current_hour.value']
        daily_elig_q = routes[f'{cid}.daily.eligible']
        cur24_elig_q = routes[f'{cid}.current24.eligible']
        pop_day_q = routes[f'{cid}.relay_population.day']
        pop_hour_q = routes[f'{cid}.relay_population.hour']
        chart_first_daily = min(
            (int(s['history']['daily'][0][0]) for s in chart['series'] if s.get('history', {}).get('daily')),
            default=seed_first_daily,
        )

        for series in chart['series']:
            fetch(cur_hr_elig_q, HOUR, context_times)
            fetch(cur_hr_avail_q, HOUR, current_times)
            fetch(cur_hr_val_q, HOUR, current_times)

            history = series.get('history', {})
            daily_rows = history.get('daily', [])
            sh_rows = history.get('sampled_hour', [])
            fetched_raw_n = set()
            fetched_cur24 = False
            final_daily_end = int(daily_rows[-1][0]) if daily_rows else None
            effective_first_daily = int(daily_rows[0][0]) if daily_rows else chart_first_daily
            daily_start = (int(daily_rows[-1][0]) + DAY) if daily_rows else chart_first_daily
            has_daily_history = bool(daily_rows or (daily_start is not None and daily_start <= target_day))

            if daily_start is not None:
                if warm_day_cutoff is not None:
                    daily_start = max(daily_start, warm_day_cutoff + DAY)
                daily_times = list(range(daily_start, target_day + 1, DAY))
                if daily_times:
                    fetch(daily_elig_q, DAY, daily_times)
                    fetch(routes[f'{cid}.daily.available'], DAY, daily_times)
                    fetch(routes[f'{cid}.daily.value'], DAY, daily_times)
                    fetched_raw_n.update(daily_times)
                    final_daily_end = daily_times[-1]

            if has_daily_history:
                if not sh_rows and effective_first_daily is not None:
                    raw_first = max(effective_first_daily - DAY + SAMPLED_HOUR_STEP,
                                    target_sh - SAMPLED_HOUR_BACKFILL_MAX_DAYS * DAY)
                    first_sh = ((raw_first + SAMPLED_HOUR_STEP - 1) // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP
                elif sh_rows:
                    first_sh = int(sh_rows[-1][0]) + SAMPLED_HOUR_STEP
                else:
                    first_sh = target_sh + SAMPLED_HOUR_STEP
                if warm_sh_cutoff is not None:
                    first_sh = max(first_sh, warm_sh_cutoff + SAMPLED_HOUR_STEP)
                sh_times = list(range(first_sh, target_sh + 1, SAMPLED_HOUR_STEP))
                if sh_times:
                    fetch(routes[f'{cid}.sampled_hour.eligible'], SAMPLED_HOUR_STEP, sh_times)
                    fetch(routes[f'{cid}.sampled_hour.available'], SAMPLED_HOUR_STEP, sh_times)
                    fetch(routes[f'{cid}.sampled_hour.value'], SAMPLED_HOUR_STEP, sh_times)

                if not (end % DAY == 0 and final_daily_end == end):
                    fetch(cur24_elig_q, HOUR, [end])
                    fetch(routes[f'{cid}.current24.available'], HOUR, [end])
                    fetch(routes[f'{cid}.current24.value'], HOUR, [end])
                    fetched_cur24 = True

            expected_c = {int(r[0]): r[2] for r in daily_rows if r[4]}
            for t in fetched_raw_n:
                expected_c[t] = None
            retained = series.get('relay_population', {})
            retained_day_ok = (retained.get('method') == POPULATION_METHOD
                               and retained.get('queries', {}).get('day') == queries.get(pop_day_q))
            reused_dates = (
                {int(r[0]) for r in retained.get('daily', [])
                 if int(r[0]) in expected_c and expected_c[int(r[0])] is not None and r[1] == expected_c[int(r[0])]}
                if retained_day_ok else set()
            )
            targets = sorted(expected_c.keys() - reused_dates)
            if warm_day_cutoff is not None:
                targets = [t for t in targets if t > warm_day_cutoff]
            if targets:
                missing_n = [t for t in targets if t not in fetched_raw_n]
                if missing_n:
                    fetch(daily_elig_q, DAY, missing_n)
                fetch(pop_day_q, DAY, targets, population=True)

            if has_daily_history:
                cur_day_n = series.get('current', {}).get('day', {}).get('relays')
                reused_day_end = (end in reused_dates)
                reuses_seed_day = (not fetched_raw_n and not fetched_cur24 and end == seed['end']
                                   and end % DAY == 0 and retained_day_ok
                                   and retained.get('day') is not None and retained['day'][1] == cur_day_n)
                if not (reused_day_end and end % DAY == 0 and final_daily_end == end) and not reuses_seed_day:
                    if end not in fetched_raw_n and not fetched_cur24:
                        fetch(cur24_elig_q, HOUR, [end])
                    fetch(pop_day_q, HOUR, [end])

            fetch(cur_hr_elig_q, HOUR, [end])
            fetch(pop_hour_q, HOUR, [end])

    proof_q = routes['version.attestation_proof_metadata']
    fetch(proof_q, HOUR, [end])
    for cohort in COHORT_NAMES:
        fetch(routes[f'cohort.{cohort}_verified_binary'], HOUR, [end])
        if f'cohort.{cohort}_stock_config' in routes:
            fetch(routes[f'cohort.{cohort}_stock_config'], HOUR, [end])
        fetch(routes[f'cohort.{cohort}_attested'], HOUR, [end])
    fetch(proof_q, HOUR, [end])
    for key in METADATA_KEYS:
        fetch(routes[f'metadata.{key}'], HOUR, [end])
    for _, op in DIRCACHE_OPERATIONS:
        fetch(routes[f'dircache.{op}'], HOUR, [end])
    if 'dos.effective_config' in routes and 'dos.begindir_config' in routes:
        fetch(routes['dos.effective_config'], HOUR, [end])
        fetch(routes['dos.begindir_config'], HOUR, [end])
    return requests


def _prepare_context(config, application_dir, state_dir, end=None, seed=None, *,
                     config_path=None, config_bytes=None, forbid_overlap=(), output=None, require_ssh=False,
                     check_end=True):
    app = Path(ROOT if application_dir is None else application_dir).resolve()
    state = Path(state_dir).absolute()
    cfg_path = expand_path(config_path) if config_path is not None else None
    if config_bytes is not None:
        config_digest = sha256_hex(config_bytes)
    elif cfg_path is not None:
        config_digest = sha256_hex(_read_bounded(cfg_path, MAX_CONFIG_BYTES, private=True))
    else:
        config_digest = sha256_hex(
            json.dumps(dict(sorted(config.items())), allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        )
    seed_path, private_seed = resolve_seed_path(config, app, seed)
    catalogue_dir = (app / 'catalogue') if (app / 'catalogue/queries.json').is_file() else (ROOT / 'catalogue')
    queries_path, routes_path = catalogue_dir / 'queries.json', catalogue_dir / 'routes.json'
    known_hosts_bytes = None
    if require_ssh:
        _, identity_path, known_hosts_path, _, _, known_hosts_bytes = _validate_ssh_files(config)
    else:
        if config.get('SOURCE_SSH_PORT') is not None and str(config['SOURCE_SSH_PORT']).strip():
            _validate_ssh_port(config['SOURCE_SSH_PORT'])
        identity_path = expand_path(config['SOURCE_SSH_IDENTITY_FILE']) if config.get('SOURCE_SSH_IDENTITY_FILE') else None
        known_hosts_path = expand_path(config['SOURCE_SSH_KNOWN_HOSTS_FILE']) if config.get('SOURCE_SSH_KNOWN_HOSTS_FILE') else None
        if identity_path is not None and os.path.lexists(identity_path):
            try:
                with access.regular_read(identity_path):
                    pass
            except (access.AccessError, OSError, ValueError) as error:
                raise CollectionError('Source SSH file must be a readable regular file') from error
        if known_hosts_path is not None and os.path.lexists(known_hosts_path):
            known_hosts_bytes = _read_bounded(known_hosts_path, MAX_KNOWN_HOSTS_BYTES)
    if output is not None:
        out = Path(output).absolute()
        state_files = tuple(
            state / name for name in (
                'observations.sqlite3', 'observations.sqlite3-wal', 'observations.sqlite3-shm',
                'observations.sqlite3-journal', 'checkpoint.json', 'collect.lock',
            )
        )
        protected_out = [p for p in (cfg_path, identity_path, known_hosts_path, seed_path,
                                     queries_path, routes_path, *state_files) if p is not None]
        _assert_disjoint([out], protected_out, 'Collector output overlaps its source, catalogue, or cache')
        # Output cannot overwrite state_dir itself or enclose it, while unrelated files inside state_dir remain permitted.
        _require(not any(filesystem_contains(ov, sv)
                         for ov in _resolve_overlap_variants(out)
                         for sv in _resolve_overlap_variants(state)),
                 'Collector output overlaps its source, catalogue, or cache')
    if private_seed:
        seed_conflicts = [state] + ([cfg_path] if cfg_path is not None else [])
        _assert_disjoint([seed_path], seed_conflicts, 'Configured seed snapshot overlaps source config or state directory')
    if forbid_overlap:
        protected = [p for p in (cfg_path, identity_path, known_hosts_path, state,
                                 seed_path if private_seed else None) if p is not None]
        _assert_disjoint(protected, forbid_overlap, 'Collector configuration or state overlaps forbidden directory')

    now_hour = (int(time.time()) // HOUR) * HOUR
    if check_end:
        end = now_hour if end is None else end
        _require(type(end) is int and end % HOUR == 0 and end <= now_hour,
                 'Collection end must be a completed UTC hour at or after the seed')
    seed_bytes = _read_bounded(seed_path, MAX_SEED_BYTES, private=private_seed)
    seed_obj = parse_json(seed_bytes)
    validate_snapshot(seed_obj, allow_v3_seed=True)
    if check_end:
        _require(seed_obj['end'] <= end,
                 'Collection end must be a completed UTC hour at or after the seed')
    queries_bytes = _read_bounded(queries_path, MAX_SEED_BYTES)
    routes_bytes = _read_bounded(routes_path, MAX_SEED_BYTES)
    queries, routes = parse_json(queries_bytes), parse_json(routes_bytes)
    return {
        'state': state,
        'end': end,
        'now_hour': now_hour,
        'config_digest': config_digest,
        'known_hosts_path': known_hosts_path,
        'known_hosts_bytes': known_hosts_bytes,
        'seed': seed_obj,
        'seed_sha': sha256_hex(seed_bytes),
        'queries': queries,
        'queries_sha': sha256_hex(queries_bytes),
        'routes': routes,
        'routes_sha': sha256_hex(routes_bytes),
    }


def plan_collection(config, application_dir, state_dir, previous_end=None, max_catchup_days=31,
                    end=None, seed=None, *, config_path=None, config_bytes=None, forbid_overlap=()):
    _require(type(max_catchup_days) is int and max_catchup_days >= 1,
             'Maximum catch-up days must be a positive integer')
    ctx = _prepare_context(config, application_dir, state_dir, end=end, seed=seed,
                           config_path=config_path, config_bytes=config_bytes,
                           forbid_overlap=forbid_overlap, require_ssh=True)
    target_end = ctx['end']
    if previous_end is not None:
        _require(type(previous_end) is int and previous_end > 0 and target_end >= previous_end,
                 'Collection end precedes previous observation end')
    source_id = _compute_source_identity(
        ctx['config_digest'], sha256_hex(ctx['known_hosts_bytes']),
        ctx['seed_sha'], ctx['queries_sha'], ctx['routes_sha'])
    warm_end = _inspect_warm_checkpoint(ctx['state'], source_id, ctx['now_hour'])

    seed_obj = ctx['seed']
    seed_earliest_end = int(seed_obj['end'])
    for chart in seed_obj['charts']:
        for series in chart['series']:
            history = series.get('history', {})
            for field in ('daily', 'sampled_hour'):
                hist_rows = history.get(field)
                if hist_rows:
                    seed_earliest_end = min(seed_earliest_end, int(hist_rows[-1][0]))

    base_end = warm_end if warm_end is not None else seed_earliest_end
    base_catchup_days = 1 if target_end <= base_end else max(1, math.ceil((target_end - base_end) / DAY))
    baseline_end = min(previous_end, base_end) if previous_end is not None else base_end
    catchup_days = 1 if target_end <= baseline_end else max(1, math.ceil((target_end - baseline_end) / DAY))
    if catchup_days > max_catchup_days:
        if previous_end is not None and base_catchup_days <= max_catchup_days:
            raise CollectionError(
                'Last published observation precedes automated catch-up window; '
                'complete the manual publication workflow before retrying'
            )
        raise CollectionError('Catch-up exceeds automated window; run manual collection first')
    max_requests = _plan_max_requests(ctx['routes'], ctx['queries'], seed_obj, target_end,
                                      warm_end=warm_end, baseline_end=baseline_end)
    timeout_s = max_requests * SOURCE_REQUEST_TIMEOUT + COLLECTION_OVERHEAD_SECONDS
    return {'snapshot_end': target_end, 'catchup_days': catchup_days, 'recommended_timeout_seconds': timeout_s}


def record_checkpoint(config, application_dir, state_dir, end, seed=None, *,
                      config_path=None, config_bytes=None, forbid_overlap=()):
    _require(type(end) is int, 'Checkpoint end must be an integer UTC hour')
    ctx = _prepare_context(config, application_dir, state_dir, end=end, seed=seed,
                           config_path=config_path, config_bytes=config_bytes,
                           forbid_overlap=forbid_overlap, require_ssh=True)
    state = ctx['state']
    _require_private_dir(state, 'Collector state directory must exist before recording a checkpoint',
                         'Collector state directory must be private owned')
    source_id = _compute_source_identity(
        ctx['config_digest'], sha256_hex(ctx['known_hosts_bytes']),
        ctx['seed_sha'], ctx['queries_sha'], ctx['routes_sha'])
    previous_umask = os.umask(0o077)
    try:
        with state_lock(state):
            _require(
                _cache_has_completion(state / 'observations.sqlite3', source_id, ctx['end'], require_match=True),
                'Observation cache must contain a committed completion record matching this configuration and endpoint',
            )
            atomic_json(state / 'checkpoint.json',
                        {'source_identity_sha256': source_id, 'observation_end': ctx['end']})
    finally:
        os.umask(previous_umask)
    return {'status': 'checkpoint_recorded', 'observation_end': ctx['end']}


def collect(config, application_dir, state_dir, output, end=None, source=None, seed=None, *,
            config_path=None, config_bytes=None, forbid_overlap=()):
    ctx = _prepare_context(config, application_dir, state_dir, end=end, seed=seed,
                           config_path=config_path, config_bytes=config_bytes,
                           forbid_overlap=forbid_overlap, output=output, require_ssh=False)
    state, target_end = ctx['state'], ctx['end']
    out = Path(output).absolute()
    queries, routes, seed_obj = ctx['queries'], ctx['routes'], ctx['seed']
    source = source or SSHSource(config)
    kh_path = ctx['known_hosts_path']
    if kh_path is not None and os.path.lexists(kh_path):
        kh_digest = sha256_hex(_read_bounded(kh_path, MAX_KNOWN_HOSTS_BYTES))
    else:
        kh_digest = getattr(source, 'identity', '')
    source_id = _compute_source_identity(
        ctx['config_digest'], kh_digest, ctx['seed_sha'], ctx['queries_sha'], ctx['routes_sha'])
    _require(source.catalogue() == queries, 'Source query catalogue differs from the reviewed release')
    cache_committed = False
    checkpoint_updated = False
    previous_umask = os.umask(0o077)
    try:
        with state_lock(state):
            cache = ObservationCache(state / 'observations.sqlite3', source, queries)
            try:
                try:
                    data = Collector(source, cache, queries, routes, seed_obj).collect(target_end, ctx['seed_sha'])
                    validate_snapshot(data)
                    cache.record_hourly_archive(source_id, target_end, data)
                    cache.record_completion(source_id, target_end)
                    cache.prune_expired_hourly(target_end - HOURLY_CACHE_RETENTION_DAYS * DAY)
                    atomic_json(out, data, before_replace=cache.commit)
                except BaseException:
                    try:
                        cache.rollback()
                    except (sqlite3.Error, OSError):
                        pass
                    raise
                cache_committed = True
                cache.incremental_vacuum()
                try:
                    atomic_json(state / 'checkpoint.json',
                                {'source_identity_sha256': source_id, 'observation_end': target_end})
                except OSError:
                    sys.stderr.write('Collection warning: collector checkpoint update failed after snapshot publication.\n')
                else:
                    checkpoint_updated = True
            finally:
                cache.close()
    finally:
        os.umask(previous_umask)
    return {'snapshot_end': target_end, 'charts': len(data['charts']),
            'series': sum(len(c['series']) for c in data['charts']),
            'source_requests': source.requests, 'cache_committed': cache_committed,
            'checkpoint_updated': checkpoint_updated}


def _decompress_archive_row(blob):
    _require(isinstance(blob, (bytes, bytearray, memoryview)) and len(blob) <= MAX_ARCHIVE_ROW_BYTES + 65536,
             'Invalid archived payload')
    decomp = zlib.decompressobj()
    try:
        raw = decomp.decompress(bytes(blob), MAX_ARCHIVE_ROW_BYTES + 1)
        tail = decomp.flush(MAX_ARCHIVE_ROW_BYTES + 1 - len(raw)) if len(raw) <= MAX_ARCHIVE_ROW_BYTES else b''
    except zlib.error as error:
        raise CollectionError('Corrupt archived hourly payload') from error
    raw += tail
    _require(len(raw) <= MAX_ARCHIVE_ROW_BYTES and not decomp.unconsumed_tail and decomp.eof and not decomp.unused_data,
             'Invalid archived hourly payload')
    return raw


def export_hourly_archive(config, application_dir, state_dir, output, start=None, end=None, seed=None, *,
                          config_path=None, config_bytes=None, forbid_overlap=()):
    """Export verified compressed hourly_archive rows as ordered 0600 JSON Lines without opening SSH."""
    if start is not None:
        _require(type(start) is int and start >= 0 and start % HOUR == 0,
                 'Export start must be a non-negative UTC hour')
    if end is not None:
        _require(type(end) is int and end >= 0 and end % HOUR == 0,
                 'Export end must be a non-negative UTC hour')
    if start is not None and end is not None:
        _require(start <= end, 'Export start must not exceed export end')
    out = expand_path(output)
    try:
        out_info = out.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise CollectionError('Export output must be a regular file') from error
    else:
        _require(not stat.S_ISLNK(out_info.st_mode) and stat.S_ISREG(out_info.st_mode),
                 'Export output must be a regular file')
    ctx = _prepare_context(config, application_dir, state_dir, seed=seed,
                           config_path=config_path, config_bytes=config_bytes,
                           forbid_overlap=forbid_overlap, output=out, require_ssh=True, check_end=False)
    if forbid_overlap:
        _assert_disjoint([out], forbid_overlap, 'Collector output overlaps forbidden directory')
    state = ctx['state']
    _require_private_dir(state, 'Collector state directory is missing',
                         'Collector state directory must be private owned')
    source_id = _compute_source_identity(
        ctx['config_digest'], sha256_hex(ctx['known_hosts_bytes']),
        ctx['seed_sha'], ctx['queries_sha'], ctx['routes_sha'],
    )
    conn = _open_readonly_cache(
        state / 'observations.sqlite3',
        fail_if_missing=True,
        fail_if_unindexed_wal=True,
        missing_message='Observation cache database is missing',
    )
    batch_size = max(1, int(ARCHIVE_EXPORT_BATCH_SIZE))
    rows_written = 0
    first_end = None
    last_end = None
    previous_umask = os.umask(0o077)
    temporary = None
    try:
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n', dir=out.parent,
            prefix='.' + out.name + '.', suffix='.tmp', delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            with closing(conn):
                last_obs = None
                while True:
                    sql = ['SELECT observation_end, payload_zlib FROM hourly_archive WHERE source_identity = ?']
                    params = [source_id]
                    if last_obs is not None:
                        sql.append('AND observation_end > ?')
                        params.append(last_obs)
                    elif start is not None:
                        sql.append('AND observation_end >= ?')
                        params.append(start)
                    if end is not None:
                        sql.append('AND observation_end <= ?')
                        params.append(end)
                    sql.append('ORDER BY observation_end ASC LIMIT ?')
                    params.append(batch_size)
                    try:
                        cursor = conn.execute(' '.join(sql), params)
                        try:
                            rows = cursor.fetchall()
                        finally:
                            cursor.close()
                        conn.rollback()
                    except sqlite3.OperationalError as error:
                        if 'no such table' not in str(error).lower():
                            raise CollectionError('Invalid observation cache database') from error
                        rows = ()
                    except sqlite3.Error as error:
                        raise CollectionError('Invalid observation cache database') from error
                    if not rows:
                        break
                    for obs_end, blob in rows:
                        raw = _decompress_archive_row(blob)
                        entry = parse_json(raw)
                        _require(
                            isinstance(entry, dict) and entry.get('schema') == HOURLY_ARCHIVE_SCHEMA
                            and entry.get('end') == obs_end and isinstance(entry.get('charts'), list),
                            'Invalid archived hourly payload',
                        )
                        handle.write(json.dumps(entry, allow_nan=False, sort_keys=True, separators=(',', ':')) + '\n')
                        if first_end is None:
                            first_end = obs_end
                        last_end = obs_end
                        rows_written += 1
                    last_obs = rows[-1][0]
                    if len(rows) < batch_size:
                        break
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
        temporary = None
        dir_fd = os.open(out.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        os.umask(previous_umask)
    return {
        'status': 'exported',
        'rows': rows_written,
        'first_observation_end': first_end,
        'last_observation_end': last_end,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--application-dir', type=Path, default=ROOT)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--export-hourly-archive', type=Path)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--start', type=int)
    parser.add_argument('--end', type=int)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--record-checkpoint', action='store_true')
    parser.add_argument('--previous-end', type=int)
    parser.add_argument('--max-catchup-days', type=int)
    parser.add_argument('--forbid-overlap', action='append', type=Path, default=[])
    args = parser.parse_args(argv)
    mode_count = (
        int(args.plan)
        + int(args.record_checkpoint)
        + int(args.export_hourly_archive is not None)
        + int(args.output is not None)
    )
    if mode_count > 1:
        parser.error('Cannot combine --plan, --record-checkpoint, --export-hourly-archive, and --output')
    if mode_count == 0:
        parser.error('--output is required unless using --plan, --record-checkpoint, or --export-hourly-archive')
    if args.record_checkpoint and args.end is None:
        parser.error('--end is required with --record-checkpoint')
    if args.start is not None and args.export_hourly_archive is None:
        parser.error('--start requires --export-hourly-archive')
    if not args.plan and (args.previous_end is not None or args.max_catchup_days is not None):
        parser.error('--previous-end and --max-catchup-days require --plan')
    max_catchup_days = 31 if args.max_catchup_days is None else args.max_catchup_days
    failure_message = ('Collection plan failed.\n' if args.plan else
                       'Checkpoint recording failed.\n' if args.record_checkpoint else
                       'Hourly archive export failed.\n' if args.export_hourly_archive is not None else
                       'Collection failed; previous snapshot retained.\n')
    try:
        config_path = expand_path(args.config)
        config, config_bytes = load_source_config(config_path, required=SSH_FIELDS, private=True, return_bytes=True)
        app_dir = ROOT if args.application_dir == ROOT else expand_path(args.application_dir)
        state_dir = expand_path(args.state_dir)
        forbidden = [expand_path(p) for p in args.forbid_overlap]
        if args.plan:
            result = plan_collection(config, app_dir, state_dir, args.previous_end, max_catchup_days,
                                     args.end, seed=args.seed, config_path=config_path,
                                     config_bytes=config_bytes, forbid_overlap=forbidden)
        elif args.record_checkpoint:
            result = record_checkpoint(config, app_dir, state_dir, args.end, seed=args.seed,
                                       config_path=config_path, config_bytes=config_bytes,
                                       forbid_overlap=forbidden)
        elif args.export_hourly_archive is not None:
            result = export_hourly_archive(config, app_dir, state_dir, expand_path(args.export_hourly_archive),
                                           start=args.start, end=args.end, seed=args.seed,
                                           config_path=config_path, config_bytes=config_bytes,
                                           forbid_overlap=forbidden)
        else:
            result = collect(config, app_dir, state_dir, expand_path(args.output), args.end, seed=args.seed,
                             config_path=config_path, config_bytes=config_bytes, forbid_overlap=forbidden)
        print(json.dumps(result))
    except (CollectionError, access.AccessError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(1, failure_message)


if __name__ == '__main__':
    main()
