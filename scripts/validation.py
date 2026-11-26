"""Validate the aggregate input boundary before any output is replaced."""
from decimal import Decimal, InvalidOperation
import math
import re
import unicodedata
from coarsening import DOS_THRESHOLD_SETTINGS, is_coarsened_count, is_coarsened_value
from population_statistics import METHOD as POPULATION_METHOD
from windows import (DAY, HOUR, MAX_SAFE_INT, MINIMUM_POPULATION_COVERAGE, RESERVED_JSON_KEYS,
                     SAMPLED_HOUR_STEP, finite_number, hourly_observations, is_sha256, non_negative,
                     nonblank, population, require, timestamp, usable_measurement)

ID = re.compile(r'^[a-z][a-z0-9_-]{0,95}$')
COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
PROMQL_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`|#[^\r\n]*|(["\'`][\s\S]*\Z)',
    re.DOTALL,
)
PROMQL_RANGE = re.compile(r'\[([^\]]*)\]')
_COUNT_STR_RE = re.compile(r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')
_HEX40 = re.compile(r'[0-9a-f]{40}')
_SAFE_FILENAME = re.compile(r'[A-Za-z0-9._-]+')
_DOS_THRESHOLD_VALUE_RE = re.compile(r'(?:auto|unset|[0-9]{1,8})\Z')
_DOS_THRESHOLD_SOURCES = frozenset({'consensus', 'torrc', 'unset', 'observed'})
_D_MAX_SAFE_INT = Decimal(MAX_SAFE_INT)
PRIVATE_KEYS = {'host', 'hostname', 'instance', 'relay_fingerprint', 'member_id',
                'pid', 'boot_id', 'start_ticks', 'cookie_file', 'password',
                'private_key', 'token', 'credentials', 'addr', 'ip', 'target',
                'tor_metrics_instance', 'secret', 'ssh_key'}
BAD_KEYS = RESERVED_JSON_KEYS | PRIVATE_KEYS
TOP_FIELDS = set('generated_at start end window_seconds minimum_samples expected_samples fleet custom defense flags families product history_methods versions snapshot_flags dircache_cohort dos_thresholds sources charts input_schema_version input_provenance'.split())
CHART_FIELDS = set('id family title unit note relays kind query quality_query series display_keys start end definition metrics metric_origin family_id display_scale display_unit headline_unit primary_series introduced_note history_status history_queries cohort_key'.split())
SERIES_FIELDS = set('name key color values history history_status current hourly_relays hourly_expected_relays relay_population'.split())
SERIES_FIELDS_V3 = set('name key color history history_status current relay_population'.split())
CURRENT_FIELDS = set('value relays expected_relays valid coverage'.split())
VERSION_FIELDS = set('as_of verified_runtime runtime_unverified_relays runtime_verified_total native_total default_cli_only verification cli_warning metric_cohorts history earlier_version_scope binary_hash_counts provenance runtime_binary_hash_verified_total runtime_resolution cohort_labels'.split())
PUBLIC_VERSION_FIELDS = set('as_of verified_runtime runtime_unverified_relays runtime_verified_total native_total metric_cohorts cohort_labels runtime_binary_hash_verified_total runtime_resolution'.split())
BUILD_FIELDS = set('build_id version base_version patched relays source_commit source_url verification'.split())
FAMILY_FIELDS = set('id title icon primary primary_series children'.split())
SOURCE_FIELDS = {'feed', 'collector', 'current_observation_end'}
PRODUCT_FIELDS = set('name application_repo deployment_repo route name_status alternate_url routing_status'.split())
METHOD_TEXT_FIELDS = set('counter gauge sampling coverage custom_directory baseline reference_line percentile reference_exclusion first_window source_scope reference_refresh historical_averages coarsening'.split())
FLAG_FIELDS = set('Authority Exit Fast Guard HSDir Running Stable Sybil V2Dir'.split())
DIRCACHE_COUNTS = set('configured_nonexit_zero observed_controlport_zero file_config_fallback_zero file0_observed1_conflicts_excluded'.split())
RUNTIME_SOURCE_FIELDS = {'continuity', 'daemon_group_a', 'daemon_group_b', 'package_lineage'}


def fields(value, allowed, message):
    require(isinstance(value, dict) and set(value) <= allowed, message)


def _public_json(data):
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if (bad := BAD_KEYS.intersection(cur)):
                raise ValueError('Reserved JSON key is not public input' if bad & RESERVED_JSON_KEYS
                                 else 'Per-relay identity or secret field in aggregate input')
            items = cur.values()
        elif isinstance(cur, list):
            items = cur
        else:
            require(not (isinstance(cur, (float, int)) and not isinstance(cur, bool)) or finite_number(cur),
                    'Non-finite number in input')
            continue
        for v in items:
            t = type(v)
            if t is float:
                require(math.isfinite(v), 'Non-finite number in input')
            elif t is int:
                require(finite_number(v), 'Non-finite number in input')
            elif t is dict or t is list or isinstance(v, (dict, list)):
                stack.append(v)
            elif isinstance(v, (float, int)) and not isinstance(v, bool):
                require(finite_number(v), 'Non-finite number in input')


def _count_string(value):
    if not isinstance(value, str) or not _COUNT_STR_RE.fullmatch(value):
        return False
    try:
        parsed = Decimal(value)
        return (parsed.is_finite() and parsed >= 0 and parsed == parsed.to_integral_value()
                and finite_number(float(parsed)) and parsed <= _D_MAX_SAFE_INT and int(parsed) % 8 == 0)
    except (InvalidOperation, ValueError, OverflowError):
        return False


def _promql_ranges(query, *, want_ranges=True, expected={'1d'}):
    if not isinstance(query, str):
        return False
    unterminated = False

    def _strip(match):
        nonlocal unterminated
        if match.group(1) is not None:
            unterminated = True
            return ''
        return '' if match.group(0).startswith('#') else '""'

    stripped = PROMQL_TOKEN.sub(_strip, query)
    if unterminated or not stripped.strip():
        return False
    ranges = {m.group(1) for m in PROMQL_RANGE.finditer(stripped)}
    return ranges == expected if want_ranges else not ranges


def _check_aligned_ts(t, previous, end, context, alignment=DAY):
    require(timestamp(t) and t % alignment == 0 and (end is None or t <= end) and (previous is None or t > previous),
            f'{context} timestamp is unaligned, beyond the snapshot, or not unique and increasing')
    return t


def _prom_result(resp, expected_type, err_resp, err_result):
    fields(resp, {'status', 'data'}, err_resp)
    require(resp.get('status') == 'success', err_resp)
    result = resp.get('data')
    fields(result, {'resultType', 'result'}, err_result)
    require(result.get('resultType') == expected_type and isinstance(result.get('result'), list), err_result)
    return result['result']


def _relay_population(series, end, *, schema_version=4):
    if 'relay_population' not in series:
        return
    value = series['relay_population']
    expected_keys = {'method', 'queries', 'hour', 'day', 'daily'} if schema_version == 4 else {'method', 'queries', 'day', 'daily'}
    expected_query_keys = {'hour', 'day'} if schema_version == 4 else {'day'}
    require(isinstance(value, dict) and set(value) == expected_keys, 'Invalid relay population fields')
    require(value['method'] == POPULATION_METHOD, 'Unsupported relay population method')
    queries = value['queries']
    require(isinstance(queries, dict) and set(queries) == expected_query_keys and all(nonblank(q) for q in queries.values()),
            'Relay population queries must be explicit nonblank text')

    def row_valid(row, t, n):
        require(isinstance(row, list) and len(row) == 8 and timestamp(row[0]) and row[0] <= MAX_SAFE_INT
                and all(is_coarsened_count(cell) for cell in row[1:]),
                'Relay population rows must contain an integer timestamp and seven coarsened safe integers')
        require(row[0] == t and n is not None and row[1] == n, 'Relay population does not match its measured window')
        require(row[2] <= row[1], 'Relay population classification exceeds its eligible count')
        require(all(row[4] + row[index] <= row[2] for index in (3, 5, 6, 7)), 'Middle-only flags overlap another role')

    for period in (('hour', 'day') if schema_version == 4 else ('day',)):
        if value[period] is not None:
            row_valid(value[period], end, series['current'][period].get('relays'))
    require(isinstance(value['daily'], list), 'Daily relay populations must be an array')
    measured = {row[0]: row[2] for row in series['history']['daily']}
    previous = None
    for row in value['daily']:
        require(isinstance(row, list) and len(row) == 8, 'Invalid daily relay population shape')
        previous = _check_aligned_ts(row[0], previous, end, 'Daily relay population')
        row_valid(row, previous, measured.get(previous))


def _metadata(data, *, ts_alignment=DAY):
    require(isinstance(data.get('generated_at'), str), 'Generation label must be text')
    product = data.get('product', {})
    fields(product, PRODUCT_FIELDS, 'Unknown product metadata field')
    require(all(isinstance(v, str) for v in product.values()), 'Product metadata must be text')
    methods = data.get('history_methods')
    fields(methods, METHOD_TEXT_FIELDS | {'row_fields'}, 'Unknown historical method metadata field')
    require(nonblank(methods.get('coarsening')), 'Historical method metadata must document dir-spec coarsening')
    require(all(isinstance(v, str) for k, v in methods.items() if k != 'row_fields'), 'Historical method descriptions must be text')
    require('row_fields' not in methods or methods['row_fields'] == ['interval_end', 'fleet_rate_or_mean', 'eligible_relays', 'coverage', 'valid'],
            'Historical row fields do not match the measurement schema')
    flags = data.get('flags', {})
    fields(flags, FLAG_FIELDS, 'Unknown aggregate flag field')
    require(all(is_coarsened_count(v) for v in flags.values()), 'Invalid coarsened aggregate flag count')
    cohort = data.get('dircache_cohort', {})
    fields(cohort, DIRCACHE_COUNTS | {'method', 'source'}, 'Unknown directory cohort metadata field')
    require(all(is_coarsened_count(v) if k in DIRCACHE_COUNTS else isinstance(v, str) for k, v in cohort.items()),
            'Invalid coarsened directory cohort metadata value')
    probes = data.get('snapshot_flags', {})
    fields(probes, {'flags', 'targets', 'up'}, 'Unknown aggregate flag probe')
    for name, probe in probes.items():
        fields(probe, {'query', 'response'}, 'Unknown aggregate flag probe field')
        require(nonblank(probe.get('query')), 'Aggregate flag query must be nonblank text')
        rows = _prom_result(probe.get('response'), 'vector', 'Unknown aggregate flag response field', 'Unknown aggregate flag result field')
        seen_metrics = set()
        for row in rows:
            fields(row, {'metric', 'value'}, 'Unknown aggregate flag observation field')
            metric = row.get('metric')
            fields(metric, {'type'} if name == 'flags' else set(), 'Unknown aggregate flag metric label')
            if name == 'flags':
                require(isinstance(metric.get('type'), str) and metric['type'] in FLAG_FIELDS, 'Unknown aggregate flag type')
            canonical = tuple(sorted(metric.items()))
            require(canonical not in seen_metrics, 'Duplicate aggregate flag series')
            seen_metrics.add(canonical)
            value = row.get('value')
            require(isinstance(value, list) and len(value) == 2 and timestamp(value[0]) and value[0] % ts_alignment == 0
                    and _count_string(value[1]),
                    'Aggregate flag values must be aligned timestamp/coarsened count-string pairs')


def _points(rows, alignment, end, *, chart_id, series_key, kind, window_seconds=DAY):
    require(isinstance(rows, list), 'Observations must be arrays')
    previous = None
    for row in rows:
        require(isinstance(row, list) and len(row) == 5, 'Invalid observation shape')
        previous = _check_aligned_ts(row[0], previous, end, 'Observation', alignment)
        require(is_coarsened_value(chart_id, series_key, kind, row[1], window_seconds=window_seconds),
                'Invalid or uncoarsened observation value')
        require(row[2] is None or is_coarsened_count(row[2]), 'Invalid or uncoarsened reporting population')
        require(row[3] is None or (non_negative(row[3]) and row[3] <= 1), 'Invalid coverage')
        require(type(row[4]) is bool, 'Qualification must be a boolean')
        if row[2] in (None, 0):
            require(row[1] is None and row[3] in (None, 0) and not row[4],
                    'Unreported interval cannot carry a measurement or positive coverage')
        if row[4]:
            require(usable_measurement(row[1], row[2]) and row[2] >= 8
                    and row[3] is not None and row[3] >= MINIMUM_POPULATION_COVERAGE,
                    'Qualified row contradicts its measurement or coverage')


def _validate_versions(data, start, end, *, ts_alignment=DAY):
    versions = data.get('versions', {})
    fields(versions, VERSION_FIELDS, 'Unknown runtime metadata field')
    labels = versions.get('cohort_labels')
    require(isinstance(labels, dict) and bool(labels) and all(nonblank(k) and nonblank(v) for k, v in labels.items()),
            'Cohort labels must be a mapping of strings')
    runtimes = versions.get('verified_runtime', [])
    require(isinstance(runtimes, list), 'Verified runtime records must be an array')
    seen_build_ids = set()
    for rt in runtimes:
        fields(rt, BUILD_FIELDS, 'Invalid verified runtime record')
        require(is_coarsened_count(rt.get('relays')), 'Invalid coarsened verified runtime record')
        require(all(nonblank(rt.get(k)) for k in ('build_id', 'version', 'base_version'))
                and all(isinstance(v, str) for k, v in rt.items() if k not in ('relays', 'patched')), 'Runtime labels must be text')
        require(rt['build_id'] not in seen_build_ids, 'Duplicate verified runtime build ID')
        seen_build_ids.add(rt['build_id'])
        require('patched' not in rt or type(rt['patched']) is bool, 'Runtime patched flag must be boolean')
        require('source_commit' not in rt or bool(_HEX40.fullmatch(rt['source_commit'])),
                'Invalid verified runtime source commit')
        require('source_url' not in rt or (rt['source_url'].startswith('https://') and not any(ch.isspace() for ch in rt['source_url'])),
                'Invalid verified runtime source URL')
    require('default_cli_only' not in versions or versions['default_cli_only'] == [], 'Unverified CLI-only host records are not public input')
    require(all(k not in versions or isinstance(versions[k], str) for k in ('verification', 'cli_warning', 'earlier_version_scope')),
            'Runtime metadata note must be text')
    if 'binary_hash_counts' in versions:
        counts = versions['binary_hash_counts']
        require(isinstance(counts, list), 'Binary hash counts must be an array')
        seen_hashes = set()
        for row in counts:
            fields(row, {'metric', 'value'}, 'Invalid binary hash count entry')
            metric = row.get('metric')
            fields(metric, {'build_id', 'sha256'}, 'Invalid binary hash metric label')
            require(nonblank(metric.get('build_id')) and is_sha256(metric.get('sha256')), 'Invalid binary hash metric identity')
            pair = (metric['build_id'], metric['sha256'])
            require(pair not in seen_hashes, 'Duplicate binary hash count entry')
            seen_hashes.add(pair)
            value = row.get('value')
            require(isinstance(value, list) and len(value) == 2 and non_negative(value[0]) and _count_string(value[1]),
                    'Invalid binary hash count value')
    if 'provenance' in versions:
        v_prov = versions['provenance']
        fields(v_prov, {'runtime_query_file', 'cohort_join_queries', 'patched_release_sha256', 'stock_catalog_sha256'}, 'Unknown runtime provenance field')
        require(all(isinstance(v_prov[k], str) and _SAFE_FILENAME.fullmatch(v_prov[k]) for k in ('runtime_query_file', 'cohort_join_queries') if k in v_prov),
                'Invalid runtime provenance filename')
        require(all(is_sha256(v_prov[k]) for k in ('patched_release_sha256', 'stock_catalog_sha256') if k in v_prov), 'Invalid runtime provenance digest')
    if 'history' in versions:
        v_hist = versions['history']
        fields(v_hist, {'query', 'start', 'end', 'step', 'response'}, 'Unknown runtime history field')
        require(nonblank(v_hist.get('query')), 'Runtime history query must be text')
        require(isinstance(v_hist.get('start'), str) or (timestamp(v_hist.get('start')) and v_hist['start'] % DAY == 0), 'Invalid runtime history start')
        require(timestamp(v_hist.get('end')) and v_hist['end'] % DAY == 0 and v_hist.get('step') == DAY, 'Invalid runtime history window')
        seen_builds = set()
        for row in _prom_result(v_hist.get('response'), 'matrix', 'Invalid runtime history response', 'Invalid runtime history payload'):
            fields(row, {'metric', 'values'}, 'Invalid runtime history series')
            metric = row.get('metric')
            fields(metric, {'build_id'}, 'Invalid runtime history metric label')
            require(nonblank(metric.get('build_id')), 'Invalid runtime history build label')
            require(metric['build_id'] not in seen_builds, 'Duplicate runtime history build series')
            seen_builds.add(metric['build_id'])
            require(isinstance(row.get('values'), list), 'Runtime history values must be an array')
            prev_t = None
            for pt in row['values']:
                require(isinstance(pt, list) and len(pt) == 2 and _count_string(pt[1]), 'Invalid runtime history point')
                prev_t = _check_aligned_ts(pt[0], prev_t, None, 'Runtime history')
    res = versions.get('runtime_resolution', {})
    fields(res, {'observed_at', 'resolved_relays', 'matched_process_epochs', 'mismatches', 'snapshot_start', 'snapshot_end', 'source_sha256'}, 'Unknown runtime resolution field')
    require('observed_at' not in res or isinstance(res['observed_at'], str), 'Runtime observation label must be text')
    require(all(is_coarsened_count(res[k]) for k in ('resolved_relays', 'matched_process_epochs', 'mismatches') if k in res), 'Invalid coarsened runtime resolution count')
    require(all(timestamp(res[k]) and res[k] % ts_alignment == 0 for k in ('snapshot_start', 'snapshot_end') if k in res),
            'Invalid coarsened runtime resolution timestamp' if ts_alignment == DAY else 'Invalid runtime resolution timestamp')
    if ts_alignment == HOUR:
        require(('snapshot_start' in res) == ('snapshot_end' in res)
                and ('snapshot_start' not in res or (res['snapshot_end'] == end and res['snapshot_start'] in (end - HOUR, start))),
                'Runtime resolution window disagrees with snapshot')
    else:
        require(('snapshot_start' in res) == ('snapshot_end' in res)
                and ('snapshot_start' not in res or (res['snapshot_start'] == start and res['snapshot_end'] == end)),
                'Runtime resolution window disagrees with snapshot day')
    hashes = res.get('source_sha256', {})
    fields(hashes, RUNTIME_SOURCE_FIELDS, 'Unknown public runtime source label')
    require(all(is_sha256(v) for v in hashes.values()), 'Runtime source identities must be hashes')
    require(is_coarsened_count(versions.get('native_total')), 'Invalid native runtime count')
    require('runtime_binary_hash_verified_total' not in versions or is_coarsened_count(versions['runtime_binary_hash_verified_total']),
            'Invalid binary-verified runtime count')
    require(versions.get('as_of') == end and versions['native_total'] == data['fleet'], 'Runtime version snapshot mismatch')
    require(all(is_coarsened_count(versions.get(k)) for k in ('runtime_verified_total', 'runtime_unverified_relays')), 'Invalid runtime version count')
    require(versions['runtime_verified_total'] + versions['runtime_unverified_relays'] == data['fleet'], 'Runtime version totals do not cover the fleet')
    require(sum(v.get('relays', 0) for v in runtimes) == versions['runtime_verified_total'], 'Verified build totals disagree')
    cohorts = versions.get('metric_cohorts')
    require(isinstance(cohorts, dict) and set(cohorts) == {'native', 'custom', 'directory_defense'}, 'Invalid metric version cohorts')
    cohort_totals = {}
    for name, field in (('native', 'fleet'), ('custom', 'custom'), ('directory_defense', 'defense')):
        counts = cohorts[name]
        require(isinstance(counts, dict) and bool(counts) and all(is_coarsened_count(n) for n in counts.values()), 'Metric version cohort does not match population')
        total = sum(counts.values())
        require(total == data[field], 'Metric version cohort does not match population')
        cohort_totals[name] = total
    return cohorts, labels, cohort_totals


def _validate_current_measurement(measurement, chart, *, period, window_seconds, default_expected=None):
    fields(measurement, CURRENT_FIELDS, 'Unknown current measurement field')
    value, n = measurement.get('value'), measurement.get('relays')
    require(is_coarsened_value(chart['id'], chart['_cur_series_key'], chart['kind'], value, window_seconds=window_seconds),
            'Invalid or uncoarsened current value')
    require(n is None or is_coarsened_count(n), 'Invalid or uncoarsened current population')
    fallback_expected = max(8, chart['relays']) if default_expected is None else default_expected
    expected = measurement.get('expected_relays', fallback_expected)
    require(population(expected) and is_coarsened_count(expected)
            and (expected >= 8 if period == 'hour' else expected >= chart['relays']),
            'Invalid current qualification denominator')
    require(n is None or n <= expected, 'Current coverage exceeds its qualification denominator')
    require(n not in (None, 0) or value is None, 'Unreported current interval cannot carry a measurement')
    has_cov, coverage = 'coverage' in measurement, measurement.get('coverage')
    if has_cov:
        require(coverage is None or (non_negative(coverage) and coverage <= 1), 'Invalid current coverage')
        inferred = n / expected if n is not None else None
        require((coverage is None and n in (None, 0))
                or (coverage is not None and inferred is not None and math.isclose(coverage, inferred, abs_tol=1e-12)
                    and (MINIMUM_POPULATION_COVERAGE <= coverage <= 1) == (MINIMUM_POPULATION_COVERAGE <= inferred <= 1)),
                'Supplied coverage contradicts population/denominator')
    if 'valid' in measurement:
        require(type(measurement['valid']) is bool, 'Current validity must be boolean')
        if not measurement['valid']:
            require(value is None, 'Unqualified current interval cannot carry a measurement')
        else:
            require(usable_measurement(value, n) and n >= 8 and finite_number(coverage)
                    and MINIMUM_POPULATION_COVERAGE <= coverage <= 1, 'Current qualification contradicts population/coverage')
    return value, n, expected, has_cov, coverage


def _validate_hourly_series(series, chart, end):
    for field in ('values', 'hourly_relays', 'hourly_expected_relays'):
        require(isinstance(series.get(field), list), f'Hourly {field} must be an array')
    expected_grid = list(range(end - DAY + HOUR, end + 1, HOUR))
    vals, hr, hexp = series['values'], series['hourly_relays'], series['hourly_expected_relays']
    require(len(vals) == len(hr) == len(hexp) == 24,
            'Current measurements and populations must cover the same 24 hourly endpoints')
    for expected_t, v_pt, n_pt, e_pt in zip(expected_grid, vals, hr, hexp, strict=True):
        for pt in (v_pt, n_pt, e_pt):
            require(isinstance(pt, list) and len(pt) == 2 and timestamp(pt[0]), 'Invalid hourly point shape')
        require(v_pt[0] == expected_t and n_pt[0] == expected_t and e_pt[0] == expected_t,
                'Current measurements and populations must cover the same 24 hourly endpoints')
        require(is_coarsened_value(chart['id'], series['key'], chart['kind'], v_pt[1], window_seconds=HOUR),
                'Invalid or uncoarsened hourly value')
        require(n_pt[1] is None or is_coarsened_count(n_pt[1]), 'Invalid or uncoarsened hourly population')
        require(is_coarsened_count(e_pt[1]) and population(e_pt[1])
                and (n_pt[1] is None or n_pt[1] <= e_pt[1]), 'Invalid hourly expected population')
    hourly = hourly_observations(series)
    require(all(row[1] is None or (row[4] and row[2] >= 8) for row in hourly), 'Unqualified hourly value must be null')
    return hourly


def _validate_series(series, chart, end, *, schema_version=4):
    require(nonblank(series.get('name')), 'Unknown or invalid series field')
    require('history_status' not in series or isinstance(series['history_status'], str), 'Series history status must be text')
    require(isinstance(series.get('color'), str) and COLOR.fullmatch(series['color']), 'Invalid series color')
    chart_ctx = dict(chart, _cur_series_key=series['key'])
    hourly = _validate_hourly_series(series, chart, end) if schema_version == 4 else None
    history = series.setdefault('history', {})
    allowed_hist = {'daily', 'sampled_hour'} if schema_version == 4 else {'daily'}
    fields(history, allowed_hist, 'Invalid historical observations')
    rows = history.setdefault('daily', [])
    _points(rows, DAY, end, chart_id=chart['id'], series_key=series['key'], kind=chart['kind'], window_seconds=DAY)
    if schema_version == 4:
        sh_rows = history.setdefault('sampled_hour', [])
        _points(sh_rows, SAMPLED_HOUR_STEP, end, chart_id=chart['id'], series_key=series['key'],
                kind=chart['kind'], window_seconds=HOUR)
    current = series.get('current', {})
    if schema_version == 4:
        require(isinstance(current, dict) and set(current) == {'hour', 'day'}, 'Both current durations must be explicit')
        hourly_expected = series['hourly_expected_relays'][-1][1] if series.get('hourly_expected_relays') else None
        h_val, h_n, h_exp, _, _ = _validate_current_measurement(
            current['hour'], chart_ctx, period='hour', window_seconds=HOUR, default_expected=hourly_expected)
        require(h_exp == series['hourly_expected_relays'][-1][1], 'Headline and plotted expected population disagree')
        if 'valid' in current['hour']:
            require(current['hour']['valid'] == hourly[-1][4], 'Headline and plotted qualification disagree')
        last_h_val = series['values'][-1][1]
        require((h_val is None and last_h_val is None) or (h_val is not None and last_h_val is not None
                and math.isclose(h_val, last_h_val, rel_tol=1e-12, abs_tol=1e-12)),
                'Headline and plotted final hour disagree')
        require(h_n == series['hourly_relays'][-1][1], 'Headline and plotted population disagree')
    else:
        require(isinstance(current, dict) and set(current) == {'day'}, 'Current duration must contain only day')
    value, n, _, has_cov, coverage = _validate_current_measurement(current['day'], chart_ctx, period='day', window_seconds=DAY)
    if rows and rows[-1][0] == end:
        _, last_val, last_relays, last_cov, last_valid = rows[-1]
        require((value is None and last_val is None) or (value is not None and last_val is not None
                and math.isclose(value, last_val, rel_tol=1e-12, abs_tol=1e-12)), 'Headline and plotted final day disagree')
        require(n == last_relays, 'Headline and plotted daily population disagree')
        require('valid' not in current['day'] or current['day']['valid'] == last_valid, 'Headline and plotted daily qualification disagree')
        if has_cov and coverage is not None and last_cov is not None:
            require(math.isclose(coverage, last_cov, abs_tol=1e-12), 'Headline and plotted daily coverage disagree')
    _relay_population(series, end, schema_version=schema_version)
    if series['key'] in chart['display_keys']:
        periods = ('hour', 'day') if schema_version == 4 else ('day',)
        require(all(type(current[p].get('valid')) is bool for p in periods), 'Displayed series needs explicit validity')
        require(series.get('history_status') == 'queried' and all(bool(history.get(p)) for p in allowed_hist),
                'Displayed series is missing historical observations')


def validate_snapshot(data, *, allow_v3_seed=False):
    _public_json(data)
    fields(data, TOP_FIELDS, 'Unknown field outside public snapshot shape')
    schema_version = data.get('input_schema_version')
    require(type(schema_version) is int and (schema_version == 4 or (allow_v3_seed and schema_version == 3)),
            'Unsupported input schema version')
    ts_alignment = HOUR if schema_version == 4 else DAY
    _metadata(data, ts_alignment=ts_alignment)
    require(all(is_coarsened_count(data.get(k)) for k in ('fleet', 'custom', 'defense')), 'Invalid coarsened top-level population')
    provenance = data.get('input_provenance', {})
    fields(provenance, {'description', 'snapshot_end', 'source_public_data_sha256'}, 'Unknown input provenance field')
    require('description' not in provenance or isinstance(provenance['description'], str), 'Input provenance description must be text')
    require('source_public_data_sha256' not in provenance or is_sha256(provenance['source_public_data_sha256']),
            'Input provenance source identity must be a SHA-256 digest')
    start, end = data.get('start'), data.get('end')
    require(timestamp(start) and timestamp(end) and end <= 8640000000000
            and start % ts_alignment == 0 and end % ts_alignment == 0 and end - start == DAY,
            'Snapshot must contain one complete UTC-aligned 24-hour period' if schema_version == 4
            else 'Snapshot must contain one complete UTC-midnight-aligned 24-hour period')
    start, end = int(start), int(end)
    for probe in data.get('snapshot_flags', {}).values():
        for row in probe['response']['data']['result']:
            require(row['value'][0] <= end, 'Aggregate flag timestamp is beyond the snapshot')
    require('snapshot_end' not in provenance or (timestamp(provenance['snapshot_end']) and provenance['snapshot_end'] == end),
            'Input provenance snapshot mismatch')
    if schema_version == 4:
        require(data.get('window_seconds') == HOUR and data.get('minimum_samples') == 24
                and ('expected_samples' not in data or data['expected_samples'] == 30),
                'Unsupported hourly measurement contract')
    else:
        require(data.get('window_seconds') == DAY and data.get('minimum_samples') == 360
                and ('expected_samples' not in data or data['expected_samples'] == 720),
                'Unsupported daily measurement contract')
    charts, families = data.get('charts', []), data.get('families', [])
    require(isinstance(charts, list) and bool(charts) and isinstance(families, list) and len(families) == 6, 'Expected charts and six families')
    for chart in charts:
        fields(chart, CHART_FIELDS, 'Unknown chart input field')
    family_by_chart, family_ids = {}, set()
    for family in families:
        fields(family, FAMILY_FIELDS, 'Unknown family input field')
        fid = family.get('id')
        require(isinstance(fid, str) and bool(ID.fullmatch(fid)) and fid not in family_ids, 'Invalid or duplicate family ID')
        family_ids.add(fid)
        require(isinstance(family.get('children'), list) and all(isinstance(c, str) for c in family['children']),
                'Family children must be arrays of chart IDs')
        require(nonblank(family.get('title')) and nonblank(family.get('icon')), 'Family labels must be text')
        require(family.get('primary') in family['children'], 'Invalid primary chart')
        for child in family['children']:
            require(child not in family_by_chart, 'Families must partition the charts')
            family_by_chart[child] = family
    chart_ids = [c.get('id') for c in charts]
    require(all(isinstance(i, str) and ID.fullmatch(i) for i in chart_ids) and len(set(chart_ids)) == len(chart_ids),
            'Invalid or duplicate chart ID')
    require(set(chart_ids) == set(family_by_chart), 'Families must partition the charts')
    sources = data.get('sources', {})
    fields(sources, SOURCE_FIELDS, 'Unknown source metadata field')
    require(all(isinstance(sources.get(k), str) and sources[k] for k in ('feed', 'collector')), 'Source labels must be text')
    require(sources.get('current_observation_end') == end, 'Source snapshot mismatch')
    cohorts, labels, cohort_totals = _validate_versions(data, start, end, ts_alignment=ts_alignment)
    checked_cohort_labels = set()
    allowed_kinds = {'counter_hour_rate', 'gauge_hour_mean'} if schema_version == 4 else {'counter_daily_rate', 'gauge_daily_mean'}
    expected_cur_range = {'1h'} if schema_version == 4 else {'1d'}
    expected_hq = {'daily': {'1d'}, 'sampled_hour': {'1h'}, 'current24': {'1d'}} if schema_version == 4 else {'daily': {'1d'}}
    series_fields = SERIES_FIELDS if schema_version == 4 else SERIES_FIELDS_V3
    for chart in charts:
        require(all(isinstance(chart.get(k), str) for k in ('family', 'title', 'unit', 'note', 'definition', 'metric_origin', 'display_unit', 'headline_unit')),
                'Chart labels must be text')
        require(all(chart[k].strip() for k in ('title', 'definition', 'display_unit', 'headline_unit')), 'Chart labels must be text')
        require(chart.get('introduced_note') is None or isinstance(chart['introduced_note'], str), 'Introduction note must be text or null')
        require(isinstance(chart.get('metrics'), list) and bool(chart['metrics']) and all(isinstance(m, str) and m for m in chart['metrics']),
                'Metrics must be an array of strings')
        require(chart.get('start') == start and chart.get('end') == end, 'Chart snapshot mismatch')
        require(isinstance(chart.get('kind'), str) and chart['kind'] in allowed_kinds, 'Unknown measurement kind')
        require(is_coarsened_count(chart.get('relays')) and finite_number(chart.get('display_scale')) and chart['display_scale'] > 0,
                'Invalid chart population or scale')
        require(chart.get('history_status') == 'ready', 'History input is not ready')
        require(all(nonblank(chart.get(k)) for k in ('query', 'quality_query', 'unit')), 'Missing query or unit')
        require(_promql_ranges(chart['query'], expected=expected_cur_range)
                and _promql_ranges(chart['quality_query'], expected=expected_cur_range),
                'Chart query must declare only the [1h] hourly range' if schema_version == 4
                else 'Chart query must declare only the [1d] daily range')
        queries = chart.get('history_queries')
        require(isinstance(queries, dict) and set(queries) == set(expected_hq), 'Invalid historical query periods')
        for period_key, query in queries.items():
            require(isinstance(query, dict) and set(query) == {'value', 'eligible', 'available'}
                    and all(nonblank(v) for v in query.values()), 'Invalid historical query definition')
            want_r = expected_hq[period_key]
            require(_promql_ranges(query['value'], expected=want_r)
                    and _promql_ranges(query['eligible'], expected=want_r)
                    and _promql_ranges(query['available'], want_ranges=False),
                    'Historical query ranges do not match the period contract')
        first_nonblank = None
        for ch in chart['unit']:
            cat = unicodedata.category(ch)
            require(not (ord(ch) < 32 or 127 <= ord(ch) <= 159 or cat == 'Cf'), 'Unsafe CSV unit')
            if first_nonblank is None and not (ch.isspace() or cat in {'Cc', 'Cs', 'Zl', 'Zp'}):
                first_nonblank = ch
        require(first_nonblank is not None and first_nonblank not in '=+-@|', 'Unsafe CSV unit')
        family = family_by_chart[chart['id']]
        require(chart.get('family_id') == family['id'], 'Chart family disagrees with catalogue')
        require(isinstance(chart.get('series'), list) and bool(chart['series']), 'Series must be a nonempty array')
        keys = []
        for series in chart['series']:
            fields(series, series_fields, 'Unknown or invalid series field')
            keys.append(series.get('key'))
        require(all(isinstance(k, str) and ID.fullmatch(k) for k in keys) and len(keys) == len(set(keys)), 'Invalid or duplicate series ID')
        require(isinstance(chart.get('display_keys'), list) and bool(chart['display_keys'])
                and all(isinstance(k, str) for k in chart['display_keys'])
                and len(chart['display_keys']) == len(set(chart['display_keys']))
                and set(chart['display_keys']) <= set(keys) and chart.get('primary_series') in chart['display_keys'],
                'Invalid displayed/primary series')
        if chart['id'] == family['primary']:
            require(chart['primary_series'] == family.get('primary_series'), 'Family and chart primary series disagree')
        ckey = chart.get('cohort_key')
        require(isinstance(ckey, str) and ckey in cohort_totals, 'Unknown build cohort or label')
        if ckey not in checked_cohort_labels:
            require(set(cohorts[ckey]) <= set(labels), 'Unknown build cohort or label')
            checked_cohort_labels.add(ckey)
        require(cohort_totals[ckey] == chart['relays'], 'Chart build population mismatch')
        for series in chart['series']:
            _validate_series(series, chart, end, schema_version=schema_version)
    if 'dos_thresholds' in data:
        _validate_dos_thresholds(data, end)
    return data


def _validate_dos_thresholds(data, end):
    dt = data['dos_thresholds']
    require(isinstance(dt, dict) and set(dt) == {'as_of', 'reported_relays', 'settings'},
            'Invalid DoS thresholds fields')
    require(timestamp(dt.get('as_of')) and dt['as_of'] == end, 'DoS thresholds snapshot mismatch')
    require(is_coarsened_count(dt.get('reported_relays')) and int(dt['reported_relays']) <= int(data['fleet']),
            'Invalid coarsened DoS thresholds relay count')
    settings = dt.get('settings')
    require(isinstance(settings, list) and len(settings) == len(DOS_THRESHOLD_SETTINGS),
            'DoS thresholds must match the canonical settings list')
    for (exp_key, exp_option, exp_family, exp_unit), setting in zip(DOS_THRESHOLD_SETTINGS, settings, strict=True):
        require(isinstance(setting, dict)
                and set(setting) == {'key', 'option', 'family_id', 'unit', 'relays', 'values', 'suppressed_relays'},
                'Invalid DoS threshold setting fields')
        require(setting['key'] == exp_key and setting['option'] == exp_option
                and setting['family_id'] == exp_family and setting['unit'] == exp_unit,
                'DoS threshold setting does not match canonical schema')
        require(is_coarsened_count(setting['relays']) and int(setting['relays']) <= int(data['fleet'])
                and is_coarsened_count(setting['suppressed_relays']),
                'Invalid coarsened DoS threshold relay count')
        values = setting['values']
        require(isinstance(values, list) and len(values) <= 16,
                'DoS threshold values must be an array of at most 16 entries')
        seen_pairs = set()
        shown_total = 0
        for entry in values:
            require(isinstance(entry, dict) and set(entry) == {'value', 'source', 'relays'},
                    'Invalid DoS threshold value fields')
            val, src, relays = entry['value'], entry['source'], entry['relays']
            require(isinstance(val, str) and bool(_DOS_THRESHOLD_VALUE_RE.fullmatch(val)),
                    'Invalid DoS threshold value')
            require(isinstance(src, str) and src in _DOS_THRESHOLD_SOURCES,
                    'Invalid DoS threshold source')
            require(is_coarsened_count(relays) and relays > 0,
                    'Invalid coarsened DoS threshold bucket count')
            pair = (val, src)
            require(pair not in seen_pairs, 'Duplicate DoS threshold value/source entry')
            seen_pairs.add(pair)
            shown_total += int(relays)
        require(shown_total + int(setting['suppressed_relays']) == int(setting['relays']),
                'DoS threshold bucket counts do not sum to setting relays')
