#!/usr/bin/env python3
"""User-owned, fixed-query SSH gateway. No shell, snapshot, or file commands."""
import argparse
from collections import defaultdict
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import stat
import sys
import time

# The forced command uses -I; explicitly load the adjacent reviewed helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import query_protocol as access

HOUR = access.HOUR
MAX_SOURCE_AGE = 180
GROUPS = {'daemon_group_a', 'daemon_group_b'}
VERSION = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,3}){3}\Z')
SHA256 = re.compile(r'[a-f0-9]{64}\Z')
VIRTUAL_QUERIES = {
    'attested_native_runtime': 'count by(build_id)(onionsurge_attested_runtime_info{type="native"})',
    'attested_directory_defense_runtime': 'count by(build_id)(onionsurge_attested_runtime_info{type="directory_defense"})',
    'attested_custom_runtime': 'count by(build_id)(onionsurge_attested_runtime_info{type="custom"})',
    'attestation_proof_metadata': 'sum by(type,sha256,build_id)(onionsurge_attestation_proof_observed_at)',
}
COHORTS = {
    'native': 'tor_relay_dos_total{job="tor_relays",type="circuit_rejected"}',
    'directory_defense': 'tor_relay_dos_total{job="tor_relays",type="begindir_rejected"}',
    'custom': 'tor_relay_hsdir_fetch_total{job="tor_relays",action="success"}',
}
MEMBER_KEY = 'experiment_id,host,instance,job,kind,member_id,unit'
PROCESS_KEY = 'experiment_id,host,instance,job,member_id,unit,pid,start_ticks,boot_id'
STATE_BINARY = (f'(tor41339_build_info{{kind="state"}} and on({MEMBER_KEY}) (tor41339_member_valid{{kind="state"}} == 1) '
                f'and on({MEMBER_KEY}) (tor41339_fingerprint_verified{{kind="state"}} == 1) '
                f'and on({MEMBER_KEY}) (tor41339_member_source_timestamp_seconds{{kind="state"}} <= time()))')
CPU_IDENTITY = (f'(tor41339_rss_bytes{{kind="cpu"}} and on({MEMBER_KEY}) (tor41339_member_valid{{kind="cpu"}} == 1) '
                f'and on({MEMBER_KEY}) ((tor41339_member_source_timestamp_seconds{{kind="cpu"}} > (time()-180)) '
                'and (tor41339_member_source_timestamp_seconds{kind="cpu"} <= time())))')
MANAGED_RUNTIME = (f'label_replace(label_replace(({STATE_BINARY} and on({PROCESS_KEY}) {CPU_IDENTITY}),"instance","$1","host","(.+)") '
                   '* on(instance,unit) group_left(tor_metrics_instance) onionforge_tor_instance_info,'
                   '"instance","$1","tor_metrics_instance","(.+)") and on(instance) up{job="tor_relays"}')


def require(condition, code):
    if not condition:
        raise access.AccessError(code)


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def non_neg_finite(value):
    return finite(value) and value >= 0 and not (isinstance(value, float) and math.copysign(1.0, value) < 0)


def private_directory(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            'private_directory_required')
    return path


def private_json(path):
    with access.regular_read(path) as stream:
        info = os.fstat(stream.fileno())
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'private_config_required')
        raw = stream.read(access.MAX_RESPONSE + 1)
    require(len(raw) <= access.MAX_RESPONSE, 'json_too_large')
    return access.strict_json(raw), hashlib.sha256(raw).hexdigest()


def catalogue(config_dir):
    queries, _ = private_json(config_dir / 'queries.json')
    access.validate_catalogue(queries)
    for key, descriptor in VIRTUAL_QUERIES.items():
        require(key not in queries or queries[key] == descriptor, 'virtual_query_descriptor_mismatch')
    return access.validate_catalogue(dict(queries, **VIRTUAL_QUERIES))


def parse(command, queries, now):
    request = access.parse(command, queries, now)
    require(request[0] in ('queries', 'prom'), 'command_not_allowed')
    if request[0] == 'prom' and request[1] in VIRTUAL_QUERIES:
        require(request[4] == HOUR and request[3] - request[2] == HOUR,
                'attestation_range_limit')
    return request


@contextmanager
def query_lock(state_dir):
    try:
        fd = os.open(state_dir / 'query.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as error:
        if error.errno in (errno.ENXIO, errno.ELOOP, errno.EISDIR):
            raise access.AccessError('invalid_lock') from error
        raise
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077, 'invalid_lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise access.AccessError('query_busy') from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def validate_attestations(document, now):
    require(isinstance(document, dict), 'invalid_attestations')
    selectors, groups, records = (document.get(key) for key in ('selectors', 'groups', 'records'))
    require(isinstance(selectors, dict) and set(selectors) == {'pid', 'start', 'health', 'source_time', 'info'}
            and all(isinstance(value, str) and value.strip() and len(value) <= 10000 for value in selectors.values()),
            'invalid_private_selectors')
    require(isinstance(groups, dict) and set(groups) <= GROUPS, 'invalid_attestation_groups')
    for group in groups.values():
        require(isinstance(group, dict) and non_neg_finite(group.get('proof_observed_at'))
                and group['proof_observed_at'] <= now
                and isinstance(group.get('proof_sha256'), str) and SHA256.fullmatch(group['proof_sha256']),
                'invalid_attestation_proof')
        require('version' not in group or (isinstance(group['version'], str) and VERSION.fullmatch(group['version'])),
                'invalid_attested_version')
    require(isinstance(records, list) and len(records) <= 4096, 'invalid_attestation_records')
    seen, targets = set(), set()
    for record in records:
        require(isinstance(record, dict) and all(isinstance(record.get(key), str) and 0 < len(record[key]) <= 256
                for key in ('source', 'unit', 'metrics_instance', 'public_group', 'version')), 'invalid_attestation_record')
        require(record['public_group'] in groups and VERSION.fullmatch(record['version']), 'invalid_attested_version')
        group = groups[record['public_group']]
        require(group.get('version', record['version']) == record['version'], 'conflicting_attested_version')
        require(finite(record.get('pid')) and record['pid'] > 0 and int(record['pid']) == record['pid']
                and non_neg_finite(record.get('start_time_seconds'))
                and record['start_time_seconds'] <= group['proof_observed_at'], 'invalid_attestation_epoch')
        key = record['source'], record['unit']
        require(key not in seen and record['metrics_instance'] not in targets, 'duplicate_attestation_member')
        seen.add(key)
        targets.add(record['metrics_instance'])
    return document


_PRIV_CODES = ('private_query_failed', 'private_query_warnings', 'private_series_limit', 'invalid_private_labels',
               'invalid_private_points', 'invalid_private_timestamp', 'invalid_private_timestamp',
               'invalid_private_timestamp', 'invalid_private_timestamp', 'invalid_private_value')


def _check_private_labels(labels):
    require(isinstance(labels, dict) and len(labels) <= 32
            and all(isinstance(k, str) and isinstance(v, str) and len(k) <= 256 and len(v) <= 1024
                    for k, v in labels.items()), 'invalid_private_labels')


def private_matrix(payload, begin, end):
    """Validate bounded private source data, which never becomes a response."""
    return access.validate_matrix(payload, begin, end, HOUR, max_series=4096, max_points=3,
                                 check_labels=_check_private_labels, codes=_PRIV_CODES, return_envelope=False)


def private_query(expression, begin, end, port, opener=None):
    return access.fetch_prometheus(
        expression, begin, end, HOUR, port=port, opener=opener,
        validate=lambda payload, b, e, _: private_matrix(payload, b, e),
    )


def indexed(rows, keys):
    result = {}
    for row in rows:
        labels = row['metric']
        require(all(isinstance(labels.get(key), str) for key in keys), 'missing_private_identity')
        identity = tuple(labels[key] for key in keys)
        for timestamp, value in row['values']:
            key = timestamp, identity
            require(key not in result, 'duplicate_private_identity')
            result[key] = (float(value), labels)
    return result


def attested_responses(document, digest, begin, end, port, fetch=None):
    """Revalidate private epochs; expose only canonical counts and proof times."""
    empty = {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
    if not document['records'] or all(row['start_time_seconds'] > end - HOUR for row in document['records']):
        return {key: empty for key in VIRTUAL_QUERIES}
    fetch = private_query if fetch is None else fetch
    first = max(0, begin - HOUR)
    raw = {key: fetch(query, first, end, port) for key, query in document['selectors'].items()}
    processes = {key: indexed(raw[key], ('instance', 'unit')) for key in ('pid', 'start', 'info')}
    sources = {key: indexed(raw[key], ('instance',)) for key in ('health', 'source_time')}
    cohorts = {name: indexed(fetch(query, begin, end, port), ('instance',)) for name, query in COHORTS.items()}
    managed = indexed(fetch(MANAGED_RUNTIME, begin, end, port), ('instance',))
    observations = {key: defaultdict(list) for key in VIRTUAL_QUERIES}
    for endpoint in (begin, end):
        counts = {name: defaultdict(int) for name in COHORTS}
        active_groups = set()
        for record in document['records']:
            key, source = (record['source'], record['unit']), (record['source'],)
            valid = record['start_time_seconds'] <= endpoint - HOUR
            for timestamp in (endpoint - HOUR, endpoint):
                pid = processes['pid'].get((timestamp, key))
                start = processes['start'].get((timestamp, key))
                info = processes['info'].get((timestamp, key))
                health = sources['health'].get((timestamp, source))
                refreshed = sources['source_time'].get((timestamp, source))
                valid &= bool(pid and start and info and health and refreshed
                              and pid[0] == record['pid'] and start[0] == record['start_time_seconds']
                              and info[0] == 1 and info[1].get('tor_metrics_instance') == record['metrics_instance']
                              and health[0] == 1 and 0 <= timestamp - refreshed[0] < MAX_SOURCE_AGE)
            target = (endpoint, (record['metrics_instance'],))
            if not valid or target in managed:
                continue
            build_id = 'attested-' + record['version'].replace('.', '_')
            included = False
            for name, members in cohorts.items():
                if target in members:
                    counts[name][build_id] += 1
                    included = True
            if included:
                active_groups.add((record['public_group'], build_id))
        for name, builds in counts.items():
            for build_id, count in builds.items():
                observations[f'attested_{name}_runtime'][(('build_id', build_id),)].append([endpoint, str(count)])
        for name, build_id in sorted(active_groups):
            proof = document['groups'][name]
            labels = (('build_id', build_id), ('sha256', proof['proof_sha256']), ('type', name))
            observations['attestation_proof_metadata'][labels].append([endpoint, str(proof['proof_observed_at'])])
        if active_groups:
            labels = (('sha256', digest), ('type', 'continuity'))
            observations['attestation_proof_metadata'][labels].append([endpoint, str(endpoint)])
    return {key: access.aggregate_response({'status': 'success', 'data': {'resultType': 'matrix',
            'result': [{'metric': dict(labels), 'values': values} for labels, values in sorted(points.items())]}}, begin, end, HOUR)
            for key, points in observations.items()}


def serve(config_dir, state_dir, command, now=None):
    config_dir, state_dir = private_directory(config_dir), private_directory(state_dir)
    queries = catalogue(config_dir)
    now = time.time() if now is None else now
    request = parse(command, queries, now)
    if request[0] == 'queries':
        return queries
    config, _ = private_json(config_dir / 'gateway.json')
    require(isinstance(config, dict) and set(config) <= {'prometheus_port', 'attestations_file'}, 'invalid_gateway_config')
    port = config.get('prometheus_port')
    require(type(port) is int and 1 <= port <= 65535, 'invalid_local_port')
    _, key, begin, end, step = request
    with query_lock(state_dir):
        if key not in VIRTUAL_QUERIES:
            return access.query_prometheus(queries[key], begin, end, step, port)
        if 'attestations_file' not in config:
            return {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
        filename = config['attestations_file']
        require(isinstance(filename, str) and Path(filename).is_absolute(), 'invalid_attestations_path')
        document, digest = private_json(Path(filename))
        validate_attestations(document, now)
        return attested_responses(document, digest, begin, end, port)[key]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
    signal.alarm(40)
    os.umask(0o077)
    result = serve(args.config_dir, args.state_dir, os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    print(json.dumps(result, allow_nan=False, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except access.AccessError as error:
        print('Operation failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # HTTP/config failures may carry private addresses or source labels.
        print('Operation failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
