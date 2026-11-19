#!/usr/bin/env python3
"""Collect public Tor aggregates through a fixed-query SSH gateway."""
import argparse
from collections import OrderedDict, defaultdict, deque
from contextlib import contextmanager
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
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import query_protocol as access
from coarsening import COARSENING_DESCRIPTION, coarsen_cohort_mapping, coarsen_count, coarsen_population_row, coarsen_value
from io_helpers import atomic_text, file_lock, parse_json as io_parse_json, read_regular_bytes, sha256_hex
from population_statistics import METHOD as POPULATION_METHOD
from query_gateway import VIRTUAL_QUERIES
from validation import RUNTIME_SOURCE_FIELDS as PROOF_LABELS, validate_snapshot
from windows import DAY, MINIMUM_POPULATION_COVERAGE, finite_number, is_sha256, nonblank, require

SSH_FIELDS = ('SOURCE_SSH_HOST', 'SOURCE_SSH_USER', 'SOURCE_SSH_PORT',
              'SOURCE_SSH_IDENTITY_FILE', 'SOURCE_SSH_KNOWN_HOSTS_FILE')
ALIASES = {'circuit_rejections': 'circuit_rejected', 'introduction_rejections': 'introduce2_rejected',
           'exit_stream_rejections': 'stream_rejected'}
SCALARS = {'protocol_closes', 'directory_waiting', 'open_circuits'}
POPULATION_BATCH_POINTS = 3
MAX_SEED_BYTES = 64 * 1024 * 1024
_ENV_LINE_RE = re.compile(r'(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)')
_HOME_VAR_RE = re.compile(r'\$\{HOME\}|\$HOME(?![A-Za-z0-9_])')
_POP_CODE_RE = re.compile(r'0|[1-9][0-9]?')
_SLUG_SUB_RE = re.compile(r'[^a-z0-9]+')
_ATTESTED_BUILD_RE = re.compile(r'attested-(?:[0-9]+_){3}[0-9]+')


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


def load_source_config(path, required=SSH_FIELDS, private=True):
    """Load private assignment-only source SSH settings safely without symlinks."""
    resolved = expand_path(path)
    try:
        raw = read_regular_bytes(resolved, 65536, private=private)
    except ValueError as error:
        raise CollectionError(str(error)) from error
    values = parse_env(raw.decode('utf-8'))
    missing = [key for key in required if not values.get(key)]
    _require(not missing, 'Missing configuration keys: ' + ', '.join(missing))
    return values


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


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n',
                mode=0o600, preserve_mode=False, dir_mode=0o700)


@contextmanager
def state_lock(directory):
    try:
        with file_lock(directory / 'collect.lock', private_dir=True):
            yield
    except ValueError as error:
        raise CollectionError(str(error)) from error


class SSHSource:
    def __init__(self, config):
        try:
            port = int(config['SOURCE_SSH_PORT'])
        except (TypeError, ValueError) as error:
            raise CollectionError('Invalid source port') from error
        if not 1 <= port <= 65535:
            raise CollectionError('Invalid source port')
        identity_path = expand_path(config['SOURCE_SSH_IDENTITY_FILE'])
        known_hosts_path = expand_path(config['SOURCE_SSH_KNOWN_HOSTS_FILE'])
        try:
            with access.regular_read(identity_path):
                pass
            known_hosts_bytes = read_regular_bytes(known_hosts_path, 65536)
            identity = identity_path.resolve(strict=True)
            known_hosts = known_hosts_path.resolve(strict=True)
        except (access.AccessError, OSError, ValueError) as error:
            raise CollectionError('Source SSH file must be a readable regular file') from error
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
            result = subprocess.run([*self.arguments, command], capture_output=True, timeout=55, check=False)
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
        self.db = sqlite3.connect(path)
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS observations '
                            '(source TEXT, query_sha TEXT, step INTEGER, endpoint INTEGER, payload TEXT, '
                            'PRIMARY KEY(source, query_sha, step, endpoint))')
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

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
            if step != DAY or len(times) != 1:
                raise CollectionError('Attestation requests require one current endpoint')
            t = times[0]
            response = access.aggregate_response(self.source.query(key, t - DAY, t, DAY), t - DAY, t, DAY)
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
    return {('action' if chart['id'] in {'hsdir_fetches', 'hsdir_uploads'} else 'type'): series['key']}


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
        return self.operation(name, DAY, [end])[end]

    def scalar(self, name, end):
        return selected(self.endpoint(name, end), {}, count=True)

    def values(self, chart, series, period, statistic, step, times):
        return {t: selected(p, series_labels(chart, series), count=statistic != 'value')
                for t, p in self.operation(f'{chart["id"]}.{period}.{statistic}', step, times).items()}

    def relay_population(self, chart, series, end, fetched_raw_n):
        prefix = f'{chart["id"]}.relay_population.'
        metric_type = next(iter(series_labels(chart, series).values()), 'scalar')
        result = {'method': POPULATION_METHOD,
                  'queries': {'day': self.queries[self.routes[prefix + 'day']]},
                  'day': None, 'daily': []}
        expected_c = {int(row[0]): row[2] for row in series['history']['daily'] if row[4]}
        retained = series.get('relay_population', {})
        rows = {}
        retained_ok = (retained.get('method') == result['method'] and retained.get('queries') == result['queries'])
        if retained_ok:
            # Reuse immutable already-coarsened daily rows from the validated seed without re-coarsening.
            rows = {row[0]: row[:] for row in retained.get('daily', [])
                    if row[0] in expected_c and row[1] == expected_c[row[0]]}
        targets = {t: expected_c[t] for t in sorted(expected_c.keys() - rows.keys())}
        cur_n_c = series['current']['day'].get('relays')
        reuse_seed_day = (not fetched_raw_n and end == self.seed['end'] and retained_ok
                          and retained.get('day') is not None and retained['day'][1] == cur_n_c)
        if cur_n_c is not None and end not in rows and not reuse_seed_day:
            targets[end] = cur_n_c
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
                    if t in expected_c:
                        rows[t] = crow
                    elif t == end and cur_n_c is not None:
                        result['day'] = crow
        if cur_n_c is not None:
            if end in rows:
                result['day'] = rows[end][:]
            elif reuse_seed_day:
                result['day'] = retained['day'][:]
        result['daily'] = [rows[t] for t in sorted(rows)]
        return result

    def versions(self, end, raw_totals, cohort_totals):
        originals = {v['build_id']: {k: value for k, value in v.items() if k != 'relays'}
                     for v in self.seed['versions'].get('verified_runtime', [])}
        approved = {(r['metric']['build_id'], r['metric']['sha256'])
                    for r in self.seed['versions'].get('binary_hash_counts', [])}
        raw_cohorts, descriptions = {}, {}
        binary_keys, attested_keys = set(), set()
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
            for row in self.endpoint(f'cohort.{cohort}_verified_binary', end)['data']['result']:
                labels = row['metric']; build_id = labels.get('build_id')
                if (build_id, labels.get('sha256')) not in approved or build_id not in originals:
                    continue
                definition = originals[build_id]
                key = ('patched_' if definition.get('patched') else 'stock_') + _SLUG_SUB_RE.sub('_', definition['version'].lower()).strip('_')
                counts[key] += number(row['values'][0][1], True)
                binary_keys.add(key)
                descriptions[key] = dict(definition, verification='Approved recorded binary hash matched a fresh process epoch.')
            for row in self.endpoint(f'cohort.{cohort}_attested', end)['data']['result']:
                build_id = row['metric'].get('build_id', '')
                _require(_ATTESTED_BUILD_RE.fullmatch(build_id) and build_id in observed, 'Unexpected public attested build')
                version = build_id.removeprefix('attested-').replace('_', '.')
                key = build_id.replace('-', '_')
                counts[key] += number(row['values'][0][1], True)
                attested_keys.add(key)
                descriptions[key] = {'build_id': build_id, 'version': version, 'base_version': version,
                    'verification': 'Daemon version observed at ' + ', '.join(iso(t) for t in sorted(set(observed[build_id])))
                                    + '; process epochs matched the measured 24-hour UTC day. No live binary hash claimed.'}
            known = sum(counts.values())
            _require(known <= total, 'Runtime cohort exceeds its observed population')
            raw_cohorts[cohort] = dict(counts, runtime_unverified=total - known)
        _require(self.endpoint('version.attestation_proof_metadata', end)['data']['result'] == proofs,
                 'Runtime attestations changed during collection')
        keys = sorted({key for counts in raw_cohorts.values() for key in counts})
        labels = {key: ('Version not recorded' if key == 'runtime_unverified' else
                       ('Patched Tor ' if descriptions[key].get('patched') else 'Tor ')
                       + descriptions[key]['version'] + (' (daemon version)' if key.startswith('attested_') else '')) for key in keys}
        cohorts = {
            cohort: coarsen_cohort_mapping({key: raw_cohorts[cohort].get(key, 0) for key in keys}, target)
            for cohort, target in cohort_totals.items()
        }
        native = cohorts['native']
        native_c = cohort_totals['native']
        verified_c = sum(native[k] for k in keys if k != 'runtime_unverified')
        binary_hash_c = sum(native[k] for k in binary_keys if k in native)
        attested_c = sum(native[k] for k in attested_keys if k in native)
        result = {'as_of': end, 'native_total': native_c, 'runtime_verified_total': verified_c,
                  'runtime_unverified_relays': native['runtime_unverified'], 'runtime_binary_hash_verified_total': binary_hash_c,
                  'verified_runtime': [dict(descriptions[key], relays=n) for key, n in native.items()
                                       if key != 'runtime_unverified' and any(cohorts[c].get(key, 0) > 0 for c in cohorts)],
                  'metric_cohorts': cohorts, 'cohort_labels': labels}
        if 'binary_hash_counts' in self.seed['versions']:
            result['binary_hash_counts'] = copy.deepcopy(self.seed['versions']['binary_hash_counts'])
        if proof_times:
            result['runtime_resolution'] = {'observed_at': iso(min(proof_times)), 'resolved_relays': attested_c,
                'matched_process_epochs': attested_c, 'snapshot_start': end - DAY, 'snapshot_end': end, 'source_sha256': source_hashes}
        return result

    def collect(self, end, seed_sha):
        data = copy.deepcopy(self.seed)
        data.update(input_schema_version=3, start=end - DAY, end=end, window_seconds=DAY, minimum_samples=360,
                    generated_at=iso(time.time()))
        data.setdefault('history_methods', {})['coarsening'] = COARSENING_DESCRIPTION
        charts_by_id = {c['id']: c for c in data['charts']}
        raw_totals = {}
        for cohort, chart_id, key in [('native', 'circuit_rejections', 'circuit_rejections'),
                                     ('custom', 'hsdir_fetches', 'success'),
                                     ('directory_defense', 'directory_rejections', 'begindir_rejected')]:
            chart = charts_by_id.get(chart_id)
            _require(chart is not None, f'Missing cohort anchor chart: {chart_id}')
            series = next((s for s in chart['series'] if s['key'] == key), None)
            _require(series is not None, f'Missing cohort anchor series: {chart_id}.{key}')
            total = self.values(chart, series, 'daily', 'available', DAY, [end])[end]
            raw_totals[cohort] = total if total is not None else 0
        cohort_totals = {cohort: coarsen_count(total) for cohort, total in raw_totals.items()}
        data.update(fleet=cohort_totals['native'], custom=cohort_totals['custom'], defense=cohort_totals['directory_defense'])
        for chart in data['charts']:
            seed_chart_relays = chart['relays']
            chart_c = cohort_totals[chart['cohort_key']]
            chart.update(start=data['start'], end=end, relays=chart_c)
            if chart['id'] == 'directory_requests':
                chart['query'] = self.queries[self.routes['directory_requests.daily.value']]
                chart['quality_query'] = self.queries[self.routes['directory_requests.daily.eligible']]
            for series in chart['series']:
                rows = series['history']['daily']
                fetched_raw_n = {}
                if rows:
                    times = list(range(int(rows[-1][0]) + DAY, end + 1, DAY))
                    if times:
                        n = self.values(chart, series, 'daily', 'eligible', DAY, times)
                        a = self.values(chart, series, 'daily', 'available', DAY, times)
                        v = self.values(chart, series, 'daily', 'value', DAY, times)
                        fetched_raw_n = n
                        history_n = {int(row[0]): row[2] for row in rows}
                        history_n.update({t: val for t, val in n.items() if val is not None})
                        e = populations(times, history_n, a)
                        last_exp_c = None
                        for t in times:
                            n_raw, e_raw, v_raw = n[t], e[t], v[t]
                            n_c = None if n_raw is None else coarsen_count(n_raw)
                            exp_c = max(8, chart_c if t == end else 0, 0 if n_c is None else n_c, coarsen_count(e_raw))
                            cov_c = (n_c / exp_c) if (n_c is not None and n_c > 0) else None
                            if n_c in (None, 0):
                                v_c, valid_c = None, False
                            else:
                                v_c = coarsen_value(chart['id'], series['key'], chart.get('kind', 'counter_daily_rate'), v_raw)
                                valid_c = bool(v_c is not None and n_c >= 8 and cov_c is not None
                                               and MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0)
                            if t == end and not valid_c:
                                v_c = None
                            rows.append([t, v_c, n_c, cov_c, valid_c])
                            if t == end:
                                last_exp_c = exp_c
                        last_row = rows[-1]
                        series['current'] = {'day': {
                            'value': last_row[1] if last_row[4] else None, 'relays': last_row[2],
                            'expected_relays': last_exp_c, 'coverage': last_row[3], 'valid': last_row[4],
                        }}
                    else:
                        cur = series['current']['day']
                        had_exp = 'expected_relays' in cur
                        cur_exp = cur['expected_relays'] if had_exp else max(8, seed_chart_relays)
                        if chart_c > cur_exp:
                            cur_n = cur.get('relays')
                            cur_val = cur.get('value')
                            new_exp = max(8, chart_c, 0 if cur_n is None else cur_n)
                            cov = (cur_n / new_exp) if (cur_n is not None and cur_n > 0) else None
                            cur_valid = cur.get('valid')
                            if cur_valid is None:
                                if rows[-1][0] == end:
                                    cur_valid = bool(rows[-1][4])
                                else:
                                    cur_valid = bool(cur_val is not None and cur_n is not None and cur_n >= 8)
                            new_valid = bool(cur_valid and cur_val is not None and cur_n is not None
                                             and cur_n >= 8 and cov is not None
                                             and MINIMUM_POPULATION_COVERAGE <= cov <= 1.0)
                            new_val = cur_val if new_valid else None
                            series['current']['day'] = {
                                'value': new_val, 'relays': cur_n, 'expected_relays': new_exp,
                                'coverage': cov, 'valid': new_valid,
                            }
                            if rows[-1][0] == end:
                                rows[-1] = [end, new_val, cur_n, cov, new_valid]
                        elif not had_exp and max(8, chart_c) != cur_exp:
                            cur['expected_relays'] = cur_exp
                else:
                    series['current'] = {'day': {
                        'value': None, 'relays': None, 'expected_relays': max(8, chart_c), 'coverage': None, 'valid': False,
                    }}
                series['relay_population'] = self.relay_population(chart, series, end, fetched_raw_n)
        data['versions'] = self.versions(end, raw_totals, cohort_totals)
        data['snapshot_flags'] = {}
        for key in ['flags', 'targets', 'up']:
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
        for field, operation in [('configured_nonexit_zero', 'combined_zero_nonexit'), ('observed_controlport_zero', 'observed_zero'),
                                 ('file_config_fallback_zero', 'file_fallback_zero'), ('file0_observed1_conflicts_excluded', 'file0_observed1_conflicts')]:
            if (val := self.scalar('dircache.' + operation, end)) is not None:
                directory[field] = coarsen_count(val)
        data['dircache_cohort'] = directory
        data['sources']['current_observation_end'] = end
        data['input_provenance'] = {'description': 'Daily 00:00 UTC fixed-query aggregates with dir-spec coarsening; retained histories seeded from the configured measurement bundle.',
                                    'snapshot_end': end, 'source_public_data_sha256': seed_sha}
        return data


def resolve_seed_path(config, app, seed=None):
    if seed is not None:
        _require(bool(os.fspath(seed).strip()), 'Seed snapshot path must not be blank')
        return expand_path(seed), True
    if config.get('SOURCE_SEED_SNAPSHOT'):
        return expand_path(config['SOURCE_SEED_SNAPSHOT']), True
    return app / 'inputs/snapshot.json', False


def collect(config, application_dir, state_dir, output, end=None, source=None, seed=None):
    app = Path(ROOT if application_dir is None else application_dir).resolve()
    state, output = Path(state_dir).absolute(), Path(output).absolute()
    seed_path, private_seed = resolve_seed_path(config, app, seed)
    catalogue_dir = (app / 'catalogue') if (app / 'catalogue/queries.json').is_file() else (ROOT / 'catalogue')
    queries_path, routes_path = catalogue_dir / 'queries.json', catalogue_dir / 'routes.json'
    _require(output.resolve() not in {seed_path.resolve(), seed_path.absolute(),
                                      (state / 'observations.sqlite3').resolve(),
                                      (state / 'collect.lock').resolve(), queries_path.resolve(), routes_path.resolve()},
             'Collector output overlaps its source, catalogue, or cache')
    now_day = (int(time.time()) // DAY) * DAY
    end = now_day if end is None else end
    _require(type(end) is int and end % DAY == 0 and end <= now_day,
             'Collection end must be a completed 00:00 UTC day at or after the seed')
    if private_seed:
        try:
            seed_bytes = read_regular_bytes(seed_path, MAX_SEED_BYTES, private=True)
        except ValueError as error:
            raise CollectionError(str(error)) from error
    else:
        seed_bytes = seed_path.read_bytes()
    seed = parse_json(seed_bytes)
    validate_snapshot(seed)
    _require(seed['end'] <= end,
             'Collection end must be a completed 00:00 UTC day at or after the seed')
    queries, routes = read_json(queries_path), read_json(routes_path)
    source = source or SSHSource(config)
    _require(source.catalogue() == queries, 'Source query catalogue differs from the reviewed release')
    previous_umask = os.umask(0o077)
    try:
        with state_lock(state):
            cache = ObservationCache(state / 'observations.sqlite3', source, queries)
            try:
                try:
                    data = Collector(source, cache, queries, routes, seed).collect(end, sha256_hex(seed_bytes))
                    validate_snapshot(data)
                    atomic_json(output, data)
                except BaseException:
                    try:
                        cache.rollback()
                    except (sqlite3.Error, OSError):
                        pass
                    raise
                try:
                    cache.commit()
                except (sqlite3.Error, OSError):
                    try:
                        cache.rollback()
                    except (sqlite3.Error, OSError):
                        pass
                    sys.stderr.write('Collection warning: observation cache commit failed after snapshot publication.\n')
            finally:
                cache.close()
    finally:
        os.umask(previous_umask)
    return {'snapshot_end': end, 'charts': len(data['charts']), 'series': sum(len(c['series']) for c in data['charts']),
            'source_requests': source.requests}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--application-dir', type=Path, default=ROOT)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--end', type=int)
    args = parser.parse_args(argv)
    try:
        config = load_source_config(args.config, required=SSH_FIELDS, private=True)
        app_dir = ROOT if args.application_dir == ROOT else expand_path(args.application_dir)
        print(json.dumps(collect(config, app_dir, expand_path(args.state_dir), expand_path(args.output),
                                 args.end, seed=args.seed)))
    except (CollectionError, access.AccessError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(1, 'Collection failed; previous snapshot retained.\n')


if __name__ == '__main__':
    main()
