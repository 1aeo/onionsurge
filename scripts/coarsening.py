"""Deterministic dir-spec coarsening and validation predicates for public aggregates."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
from functools import lru_cache
import math

from windows import DAY, HOUR, MAX_SAFE_INT, MINIMUM_POPULATION_COVERAGE, non_negative, require, timestamp

SENSITIVE_CHARTS = frozenset({'hsdir_fetches', 'hsdir_uploads', 'introduction_rejections'})
_COUNTER_KINDS = frozenset({'counter_hour_rate', 'counter_daily_rate'})
_GAUGE_KINDS = frozenset({'gauge_hour_mean', 'gauge_daily_mean'})
VALID_KINDS = _COUNTER_KINDS | _GAUGE_KINDS
SENSITIVE_BIN = Decimal('0.001')
_D_ONE, _D_EIGHT, _D_HUNDRED = Decimal(1), Decimal(8), Decimal(100)
_D_RATE_CAP, _D_OPEN_CAP, _D_GAUGE_FLOOR, _D_GAUGE_STEP = Decimal('1e10'), Decimal('1e4'), Decimal('0.05'), Decimal('0.1')
DOS_THRESHOLD_SETTINGS = (
    ('dosconnectionenabled', 'DoSConnectionEnabled', 'transport', 'flag'),
    ('dosconnectionmaxconcurrentcount', 'DoSConnectionMaxConcurrentCount', 'transport', 'connections/IP'),
    ('dosconnectionconnectrate', 'DoSConnectionConnectRate', 'transport', 'connections/s'),
    ('dosconnectionconnectburst', 'DoSConnectionConnectBurst', 'transport', 'connections'),
    ('dosconnectiondefensetimeperiod', 'DoSConnectionDefenseTimePeriod', 'transport', 'seconds'),
    ('doscircuitcreationenabled', 'DoSCircuitCreationEnabled', 'circuits', 'flag'),
    ('doscircuitcreationrate', 'DoSCircuitCreationRate', 'circuits', 'circuits/s'),
    ('doscircuitcreationburst', 'DoSCircuitCreationBurst', 'circuits', 'circuits'),
    ('doscircuitcreationminconnections', 'DoSCircuitCreationMinConnections', 'circuits', 'connections'),
    ('doscircuitcreationdefensetimeperiod', 'DoSCircuitCreationDefenseTimePeriod', 'circuits', 'seconds'),
    ('dos_num_circ_max_outq', 'dos_num_circ_max_outq', 'queues', 'circuits'),
    ('dosrefusesinglehopclientrendezvous', 'DoSRefuseSingleHopClientRendezvous', 'introduction', 'flag'),
    ('dosbegindirrateenabled', 'DoSBeginDirRateEnabled', 'directory', 'mode'),
    ('dosbegindirrate', 'DoSBeginDirRate', 'directory', 'requests/s'),
    ('dosbegindirburst', 'DoSBeginDirBurst', 'directory', 'requests'),
    ('dosbegindirdefensetype', 'DoSBeginDirDefenseType', 'directory', 'mode'),
)
DOS_EFFECTIVE_CONFIG_SETTINGS = DOS_THRESHOLD_SETTINGS[:12]
DOS_BEGINDIR_CONFIG_SETTINGS = DOS_THRESHOLD_SETTINGS[12:]
COARSENING_DESCRIPTION = (
    'Deterministic dir-spec coarsening at snapshot boundaries: standalone public relay, flag, '
    'cohort and dircache_cohort counts <8 are floored to 0 and counts >=8 are rounded to the '
    'nearest multiple of 8 (preserving classified <= total and role <= non_middle), while '
    'partitioned build, cohort and middle_only/non-middle role breakdowns distribute multiples '
    'of 8 by largest remainder so parts sum to the coarsened total (individual parts may '
    'differ from nearest-multiple rounding). open_circuits means are rounded to the '
    'nearest 100 circuits, then to 3 significant digits; hsdir_fetches, hsdir_uploads, '
    'introduction_rejections, and directory_requests hs_get/hs_post suppress <8 events/window '
    'and bin to 0.001/s, then round to 3 significant digits; other counter series suppress <8 '
    'events/window, bin to 8-event increments, and round rates to 3 significant digits; and '
    'non-sensitive gauge means (directory_waiting, open_connections) floor <0.05 to 0, quantize [0.05, 1) to 0.1, '
    'and round >=1 to 3 significant digits.'
)


def coarsen_count(value):
    """Floor counts below 8 to 0 and round counts >= 8 to the nearest multiple of 8."""
    if value is None:
        return None
    require(timestamp(value), 'Invalid count for coarsening')
    n = int(value)
    return 0 if n < 8 else ((n + 4) // 8) * 8


def is_coarsened_count(value):
    return timestamp(value) and value <= MAX_SAFE_INT and int(value) % 8 == 0


def coarsen_partition(parts, target_total):
    """Distribute target_total (a multiple of 8) across parts >= 8 using Hamilton largest remainder."""
    raw_parts = list(parts)
    require(all(part is None or timestamp(part) for part in raw_parts), 'Invalid partition part')
    require(timestamp(target_total) and int(target_total) % 8 == 0, 'Partition target must be a multiple of 8')
    adjusted = [0 if (part is None or int(part) < 8) else int(part) for part in raw_parts]
    total = int(target_total)
    total_adjusted = sum(adjusted)
    if total == 0 or total_adjusted <= 0:
        return [0] * len(adjusted)
    units = total // 8
    base = [(part * units) // total_adjusted for part in adjusted]
    remainders = sorted(
        ((Fraction(part * units, total_adjusted) - count, part, -index, index)
         for index, (part, count) in enumerate(zip(adjusted, base, strict=True))),
        reverse=True,
    )
    for _, _, _, index in remainders[:units - sum(base)]:
        base[index] += 1
    return [count * 8 for count in base]


def _quantize_3sig(decimal_value):
    return decimal_value.quantize(Decimal((0, (1,), decimal_value.adjusted() - 2)), rounding=ROUND_HALF_EVEN)


def _round_bin(decimal_value, step):
    return (decimal_value / step).to_integral_value(rounding=ROUND_HALF_EVEN) * step


def sigfigs3(value):
    """Round a positive Decimal to 3 significant digits using half-even rounding."""
    if value <= 0:
        return 0.0
    result = float(_quantize_3sig(value))
    require(math.isfinite(result), 'Invalid measurement value for coarsening')
    return result


def has_at_most_3_sigfigs(decimal_value):
    return decimal_value == 0 or decimal_value == _quantize_3sig(decimal_value)


def is_sensitive_series(chart_id, series_key):
    return chart_id in SENSITIVE_CHARTS or (chart_id == 'directory_requests' and series_key in {'hs_get', 'hs_post'})


def _check_rate_params(chart_id, series_key, kind, window_seconds):
    require(isinstance(chart_id, str) and isinstance(series_key, str), 'Chart and series keys must be strings')
    require(isinstance(kind, str) and kind in VALID_KINDS, 'Unsupported measurement kind')
    require(type(window_seconds) is int and window_seconds > 0, 'Invalid coarsening window')


def coarsen_value(chart_id, series_key, kind, value, *, window_seconds=DAY):
    """Quantize hourly or daily fleet rates and gauge means according to public coarsening policy."""
    _check_rate_params(chart_id, series_key, kind, window_seconds)
    if value is None:
        return None
    require(non_negative(value), 'Invalid measurement value for coarsening')
    if value == 0:
        return 0.0
    d = Decimal(str(value))
    d_window = Decimal(window_seconds)
    if chart_id == 'open_circuits':
        q100 = _round_bin(d, _D_HUNDRED)
        if q100 <= 0:
            return 0.0
        q100_sig = _round_bin(Decimal(str(sigfigs3(q100))), _D_HUNDRED)
        return float(q100_sig) if q100_sig > 0 else 0.0
    if is_sensitive_series(chart_id, series_key):
        if d * d_window < _D_EIGHT - Decimal('1e-12'):
            return 0.0
        binned = _round_bin(d, SENSITIVE_BIN)
        if binned <= 0 or binned * d_window < _D_EIGHT - Decimal('1e-12'):
            return 0.0
        return sigfigs3(binned)
    if kind in _COUNTER_KINDS:
        k1_rate = sigfigs3(_D_EIGHT / d_window)
        events = d * d_window
        if float(d) == k1_rate or (_D_EIGHT - Decimal('1e-12') <= events < _D_EIGHT):
            return k1_rate
        if events < _D_EIGHT:
            return 0.0
        if events >= _D_RATE_CAP:
            return sigfigs3(d)
        events_int = int(events.to_integral_value(rounding=ROUND_HALF_EVEN))
        return sigfigs3(Decimal(((events_int + 4) // 8) * 8) / d_window)
    if d < _D_GAUGE_FLOOR:
        return 0.0
    if d < _D_ONE:
        return float(d.quantize(_D_GAUGE_STEP, rounding=ROUND_HALF_EVEN))
    return sigfigs3(d)


@lru_cache(maxsize=4096)
def _is_coarsened_float(chart_id, series_key, kind, float_value, window_seconds=DAY):
    d = Decimal(str(float_value))
    d_window = Decimal(window_seconds)
    if not has_at_most_3_sigfigs(d):
        return False
    if chart_id == 'open_circuits':
        if d >= _D_OPEN_CAP:
            return True
        return d == d.to_integral_value() and int(d) % 100 == 0
    if is_sensitive_series(chart_id, series_key):
        if d < SENSITIVE_BIN or d * d_window < _D_EIGHT:
            return False
        if d >= _D_ONE:
            return True
        steps = d / SENSITIVE_BIN
        return steps == steps.to_integral_value()
    if kind in _COUNTER_KINDS:
        events = d * d_window
        if events >= _D_RATE_CAP:
            return True
        k0 = int((events / _D_EIGHT).to_integral_value(rounding=ROUND_HALF_EVEN))
        return any(
            k >= 1 and 8 * k < 10**10 and sigfigs3(Decimal(8 * k) / d_window) == float_value
            for k in (k0, k0 - 1, k0 + 1, k0 - 2, k0 + 2)
        )
    if d < _D_ONE:
        return d >= _D_GAUGE_STEP and d == d.quantize(_D_GAUGE_STEP, rounding=ROUND_HALF_EVEN)
    return True


def is_coarsened_value(chart_id, series_key, kind, value, *, window_seconds=DAY):
    """Validate that a supplied hourly/daily rate or gauge mean satisfies public coarsening bounds."""
    _check_rate_params(chart_id, series_key, kind, window_seconds)
    if value is None:
        return True
    if not non_negative(value):
        return False
    if value == 0:
        return True
    return _is_coarsened_float(chart_id, series_key, kind, float(value), window_seconds)


def coarsen_cohort_mapping(raw_map, target):
    """Coarsen a cohort breakdown so suppressed (<8) entries fold into unverified."""
    require(timestamp(target) and int(target) % 8 == 0, 'Cohort target must be a multiple of 8')
    target = int(target)
    require(all(timestamp(v) for v in raw_map.values()), 'Invalid cohort count')
    require('runtime_unverified' in raw_map or target == 0, 'Cohort breakdown missing runtime_unverified')
    keys = list(raw_map)
    non_unverified = [int(raw_map[k]) for k in keys if k != 'runtime_unverified']
    if target == 0:
        return {k: 0 for k in keys}
    if all(v < 8 for v in non_unverified):
        return {k: (target if k == 'runtime_unverified' else 0) for k in keys}
    suppressed = sum(v for v in non_unverified if v < 8)
    adjusted = [
        (int(raw_map[k]) + suppressed) if k == 'runtime_unverified'
        else (0 if int(raw_map[k]) < 8 else int(raw_map[k]))
        for k in keys
    ]
    return dict(zip(keys, coarsen_partition(adjusted, target), strict=True))


def coarsen_population_row(row, coarsened_total):
    """Coarsen one [t, total, classified, guard, middle_only, exit, hsdir, v2dir] record."""
    if row is None:
        return None
    t = int(row[0])
    orig_total = int(row[1])
    total_c = int(coarsened_total)
    if total_c == 0:
        # Preserve only explicit verified empty sets; suppress <8 cohorts as unknown.
        return [t, 0, 0, 0, 0, 0, 0, 0] if orig_total == 0 else None
    classified_orig = int(row[2])
    classified_c = total_c if classified_orig == orig_total else min(total_c, coarsen_count(classified_orig))
    middle_raw = max(0, min(int(row[4]), classified_orig))
    middle_c, non_middle_c = coarsen_partition([middle_raw, classified_orig - middle_raw], classified_c)
    guard_c, exit_c, hsdir_c, v2dir_c = (min(non_middle_c, coarsen_count(row[i])) for i in (3, 5, 6, 7))
    return [t, total_c, classified_c, guard_c, middle_c, exit_c, hsdir_c, v2dir_c]


MAX_DOS_THRESHOLD_VALUES = 16


def coarsen_threshold_buckets(buckets, raw_total, fleet_c, *, suppressed_raw=0):
    """Apply dir-spec coarsening and <8 suppression to one DoS threshold setting."""
    require(timestamp(raw_total) and timestamp(suppressed_raw) and is_coarsened_count(fleet_c),
            'Invalid threshold count for coarsening')
    total_c = min(int(fleet_c), coarsen_count(raw_total))
    if total_c == 0:
        return 0, [], 0
    eligible = []
    sup_raw = int(suppressed_raw)
    for (value, source), raw in buckets:
        require(timestamp(raw), 'Invalid threshold bucket count')
        n = int(raw)
        if n >= 8:
            eligible.append((value, source, n))
        else:
            sup_raw += n
    if not eligible:
        return total_c, [], total_c
    eligible.sort(key=lambda item: (-item[2], item[1], item[0]))
    if len(eligible) > MAX_DOS_THRESHOLD_VALUES:
        sup_raw += sum(r for _, _, r in eligible[MAX_DOS_THRESHOLD_VALUES:])
        eligible = eligible[:MAX_DOS_THRESHOLD_VALUES]
    shown_c, suppressed_c = coarsen_partition(
        [sum(r for _, _, r in eligible), sup_raw],
        total_c,
    )
    values = []
    if shown_c > 0:
        parts = coarsen_partition([r for _, _, r in eligible], shown_c)
        for (value, source, _), count in zip(eligible, parts, strict=True):
            if count > 0:
                values.append({'value': value, 'source': source, 'relays': count})
        values.sort(key=lambda item: (-item['relays'], item['source'], item['value']))
    return total_c, values, suppressed_c


def coarsen_dos_thresholds(dos_thresholds, fleet_c):
    """Coarsen an existing dos_thresholds aggregate mapping against fleet_c."""
    require(isinstance(dos_thresholds, dict) and is_coarsened_count(fleet_c)
            and 'as_of' in dos_thresholds and isinstance(dos_thresholds.get('settings'), list),
            'Invalid DoS thresholds for coarsening')
    fleet_c = int(fleet_c)
    reported = min(fleet_c, coarsen_count(dos_thresholds.get('reported_relays')))
    settings_out = []
    for setting in dos_thresholds['settings']:
        require(isinstance(setting, dict) and isinstance(setting.get('values'), list),
                'Invalid DoS threshold setting')
        values = setting['values']
        sup_raw = setting.get('suppressed_relays', 0)
        raw_total = setting.get('relays')
        require(timestamp(sup_raw) and timestamp(raw_total), 'Invalid DoS threshold count')
        buckets = []
        for item in values:
            require(isinstance(item, dict) and isinstance(item.get('value'), str)
                    and isinstance(item.get('source'), str) and timestamp(item.get('relays')),
                    'Invalid DoS threshold bucket')
            buckets.append(((item['value'], item['source']), int(item['relays'])))
        require(sum(r for _, r in buckets) + int(sup_raw) == int(raw_total),
                'DoS threshold bucket counts do not sum to setting relays')
        relays_c, coarsened_values, suppressed_c = coarsen_threshold_buckets(
            sorted(buckets), int(raw_total), fleet_c, suppressed_raw=int(sup_raw),
        )
        settings_out.append({
            'key': setting['key'],
            'option': setting['option'],
            'family_id': setting['family_id'],
            'unit': setting['unit'],
            'relays': relays_c,
            'values': coarsened_values,
            'suppressed_relays': suppressed_c,
        })
    return {
        'as_of': dos_thresholds['as_of'],
        'reported_relays': reported,
        'settings': settings_out,
    }


def _coarsen_count_string(raw):
    require(isinstance(raw, str) and not isinstance(raw, bool), 'Invalid count string')
    try:
        parsed = Decimal(raw)
        require(parsed.is_finite() and not parsed.is_signed() and parsed == parsed.to_integral_value()
                and parsed.adjusted() <= 308 and math.isfinite(float(parsed)),
                'Invalid count string')
        return str(coarsen_count(int(parsed)))
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('Invalid count string') from error


def _coarsen_fields(mapping, fields):
    for field in fields:
        if field in mapping:
            mapping[field] = coarsen_count(mapping[field])


def _cov(n_c, exp_c):
    return (n_c / exp_c) if (n_c is not None and n_c > 0) else None


def _coarsen_point(chart_id, series_key, kind, raw_val, raw_valid, n_c, cov_c, *, window_seconds=DAY):
    if n_c in (None, 0) or not raw_valid or raw_val is None:
        return None, False
    v_c = coarsen_value(chart_id, series_key, kind, raw_val, window_seconds=window_seconds)
    if v_c is None or cov_c is None or not (MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0):
        return None, False
    return v_c, True


def _coarsen_history_rows(rows, chart_id, series_key, kind, min_exp, snapshot_end, *, window_seconds):
    coarsened, pop_totals = [], {}
    last_exp_c = min_exp
    for row in rows:
        require(timestamp(row[0]), 'Invalid observation timestamp')
        t = int(row[0])
        v_raw, n_raw, cov_raw, raw_valid = row[1], row[2], row[3], bool(row[4])
        require(cov_raw is None or non_negative(cov_raw), 'Invalid observation coverage')
        if n_raw is None:
            n_c, exp_c = None, min_exp
        else:
            n_c = coarsen_count(n_raw)
            if n_raw > 0 and cov_raw and cov_raw > 0:
                ratio = n_raw / cov_raw
                require(math.isfinite(ratio), 'Invalid observation coverage ratio')
                raw_exp = round(ratio)
            else:
                raw_exp = max(8, int(n_raw))
            exp_c = max(8, n_c, coarsen_count(raw_exp))
        cov_c = _cov(n_c, exp_c)
        v_c, valid_c = _coarsen_point(chart_id, series_key, kind, v_raw, raw_valid, n_c, cov_c,
                                      window_seconds=window_seconds)
        coarsened.append([t, v_c, n_c, cov_c, valid_c])
        pop_totals[t] = n_c
        if t == snapshot_end:
            last_exp_c = exp_c
    return coarsened, pop_totals, last_exp_c


def _coarsen_current_period(cur, *, chart_id, series_key, kind, min_exp, window_seconds,
                            last_row=None, last_exp_c=None, enforce_chart_floor=True):
    cur_n_c = coarsen_count(cur.get('relays'))
    default_exp = min_exp if (enforce_chart_floor or last_exp_c is None) else last_exp_c
    expected_raw = default_exp if 'expected_relays' not in cur else cur['expected_relays']
    require(expected_raw is not None, 'Invalid current expected_relays')
    floor = min_exp if enforce_chart_floor else 8
    cur_exp_c = max(floor, coarsen_count(expected_raw), 0 if cur_n_c is None else cur_n_c)
    if last_row is not None:
        cur_n_c = last_row[2]
        cur_exp_c = max(cur_exp_c, last_exp_c, 0 if cur_n_c is None else cur_n_c)
        cur_cov_c = _cov(cur_n_c, cur_exp_c)
        cur_valid_c = bool(last_row[4] and cur.get('valid', True) and last_row[1] is not None
                           and cur_n_c is not None and cur_n_c >= 8
                           and cur_cov_c is not None and MINIMUM_POPULATION_COVERAGE <= cur_cov_c <= 1.0)
        cur_v_c = last_row[1] if cur_valid_c else None
        last_row[1], last_row[3], last_row[4] = cur_v_c, cur_cov_c, cur_valid_c
    else:
        cur_cov_c = _cov(cur_n_c, cur_exp_c)
        cur_v_c, cur_valid_c = _coarsen_point(
            chart_id, series_key, kind, cur.get('value'), cur.get('valid', True), cur_n_c, cur_cov_c,
            window_seconds=window_seconds,
        )
    return {
        'value': cur_v_c, 'relays': cur_n_c, 'expected_relays': cur_exp_c,
        'coverage': cur_cov_c, 'valid': cur_valid_c,
    }, cur_n_c


def coarsen_snapshot(data):
    """Apply dir-spec coarsening in-place across all public counts, populations and rates."""
    data.setdefault('history_methods', {})['coarsening'] = COARSENING_DESCRIPTION
    snapshot_end = int(data['end'])
    fleet_c, custom_c, defense_c = (coarsen_count(data[k]) for k in ('fleet', 'custom', 'defense'))
    data.update(fleet=fleet_c, custom=custom_c, defense=defense_c)
    cohort_totals = {'native': fleet_c, 'custom': custom_c, 'directory_defense': defense_c}

    if 'flags' in data:
        data['flags'] = {flag: coarsen_count(count) for flag, count in data['flags'].items()}
    if 'dircache_cohort' in data:
        _coarsen_fields(data['dircache_cohort'], ('configured_nonexit_zero', 'observed_controlport_zero',
                                                  'file_config_fallback_zero', 'file0_observed1_conflicts_excluded'))
    if 'snapshot_flags' in data:
        for name, probe in data['snapshot_flags'].items():
            for row in probe['response']['data']['result']:
                row['value'][1] = (str(data['flags'][row['metric']['type']])
                                   if name == 'flags' and row['metric'].get('type') in data.get('flags', {})
                                   else _coarsen_count_string(row['value'][1]))

    versions = data['versions']
    versions['native_total'] = fleet_c
    raw_runtimes = versions.get('verified_runtime', [])
    for rt in raw_runtimes:
        require(timestamp(rt.get('relays', 0)), 'Invalid verified runtime relay count')
    unverified_raw = versions.get('runtime_unverified_relays', 0)
    require(timestamp(unverified_raw), 'Invalid unverified relay count')
    eligible_runtimes = [rt for rt in raw_runtimes if int(rt.get('relays', 0)) >= 8]
    suppressed_runtime = sum(int(rt.get('relays', 0)) for rt in raw_runtimes if int(rt.get('relays', 0)) < 8)
    if not eligible_runtimes or fleet_c == 0:
        verified_c, unverified_c, versions['verified_runtime'] = 0, fleet_c, []
    else:
        verified_c, unverified_c = coarsen_partition(
            [sum(int(rt['relays']) for rt in eligible_runtimes),
             int(unverified_raw) + suppressed_runtime],
            fleet_c,
        )
        rt_parts = coarsen_partition([rt['relays'] for rt in eligible_runtimes], verified_c)
        versions['verified_runtime'] = [
            dict(rt, relays=count) for rt, count in zip(eligible_runtimes, rt_parts, strict=True) if count > 0
        ]
    versions.update(runtime_verified_total=verified_c, runtime_unverified_relays=unverified_c)

    cohorts = versions['metric_cohorts']
    for name, target in cohort_totals.items():
        cohorts[name].update(coarsen_cohort_mapping(cohorts[name], target))

    if 'binary_hash_counts' in versions:
        rt_by_id = {rt['build_id']: rt['relays'] for rt in versions['verified_runtime']}
        kept, hash_total = [], 0
        for row in versions['binary_hash_counts']:
            bid = row['metric']['build_id']
            n_c = int(_coarsen_count_string(row['value'][1]))
            if bid in rt_by_id and n_c > 0:
                n_c = min(n_c, rt_by_id[bid])
                row['value'][1] = str(n_c)
                kept.append(row)
                hash_total += n_c
        versions['binary_hash_counts'] = kept
        if 'runtime_binary_hash_verified_total' in versions:
            versions['runtime_binary_hash_verified_total'] = min(verified_c, hash_total)
    elif 'runtime_binary_hash_verified_total' in versions:
        versions['runtime_binary_hash_verified_total'] = min(
            verified_c, coarsen_count(versions['runtime_binary_hash_verified_total']))

    if 'runtime_resolution' in versions:
        res = versions['runtime_resolution']
        _coarsen_fields(res, ('resolved_relays', 'matched_process_epochs', 'mismatches'))
        if 'resolved_relays' in res and 'matched_process_epochs' in res:
            res['matched_process_epochs'] = min(res['resolved_relays'], res['matched_process_epochs'])

    if 'history' in versions:
        for row in versions['history']['response']['data']['result']:
            row['values'] = [[int(pt[0]), _coarsen_count_string(pt[1])] for pt in row['values']]

    if 'dos_thresholds' in data:
        data['dos_thresholds'] = coarsen_dos_thresholds(data['dos_thresholds'], fleet_c)

    for chart in data['charts']:
        chart_c = cohort_totals[chart['cohort_key']]
        min_exp = max(8, chart_c)
        chart['relays'] = chart_c
        kind, chart_id = chart['kind'], chart['id']
        for series in chart['series']:
            series_key = series['key']
            coarsened_daily, pop_totals, last_exp_c = _coarsen_history_rows(
                series['history']['daily'], chart_id, series_key, kind, min_exp, snapshot_end, window_seconds=DAY)
            series['history']['daily'] = coarsened_daily
            if 'sampled_hour' in series['history']:
                coarsened_sh, _, _ = _coarsen_history_rows(
                    series['history']['sampled_hour'], chart_id, series_key, kind, min_exp, snapshot_end,
                    window_seconds=HOUR)
                series['history']['sampled_hour'] = coarsened_sh

            last_hour_row = None
            last_hour_exp = None
            if 'values' in series:
                require('hourly_relays' in series and 'hourly_expected_relays' in series,
                        'Hourly arrays must accompany series values')
                c_values, c_relays, c_expected = [], [], []
                for v_pt, n_pt, e_pt in zip(series['values'], series['hourly_relays'],
                                            series['hourly_expected_relays'], strict=True):
                    require(timestamp(v_pt[0]) and v_pt[0] == n_pt[0] and v_pt[0] == e_pt[0],
                            'Hourly population grid mismatch')
                    t = int(v_pt[0])
                    n_c = coarsen_count(n_pt[1])
                    require(timestamp(e_pt[1]), 'Invalid hourly expected population')
                    exp_c = max(8, 0 if n_c is None else n_c, coarsen_count(e_pt[1]))
                    cov_c = _cov(n_c, exp_c)
                    v_c, valid_c = _coarsen_point(chart_id, series_key, kind, v_pt[1], True, n_c, cov_c,
                                                  window_seconds=HOUR)
                    c_values.append([t, v_c if valid_c else None])
                    c_relays.append([t, n_c])
                    c_expected.append([t, exp_c])
                series['values'], series['hourly_relays'], series['hourly_expected_relays'] = (
                    c_values, c_relays, c_expected)
                if c_expected:
                    last_hour_exp = c_expected[-1][1]
                if c_values and c_values[-1][0] == snapshot_end:
                    last_cov = _cov(c_relays[-1][1], last_hour_exp)
                    last_valid = bool(c_values[-1][1] is not None and c_relays[-1][1] is not None
                                      and c_relays[-1][1] >= 8 and last_cov is not None
                                      and MINIMUM_POPULATION_COVERAGE <= last_cov <= 1.0)
                    last_hour_row = [snapshot_end, c_values[-1][1], c_relays[-1][1], last_cov, last_valid]

            last_daily = coarsened_daily[-1] if (coarsened_daily and coarsened_daily[-1][0] == snapshot_end) else None
            series['current']['day'], cur_n_c = _coarsen_current_period(
                series['current']['day'], chart_id=chart_id, series_key=series_key, kind=kind,
                min_exp=min_exp, window_seconds=DAY,
                last_row=last_daily, last_exp_c=last_exp_c, enforce_chart_floor=True,
            )
            if 'hour' in series['current']:
                series['current']['hour'], cur_hour_n_c = _coarsen_current_period(
                    series['current']['hour'], chart_id=chart_id, series_key=series_key, kind=kind,
                    min_exp=min_exp, window_seconds=HOUR,
                    last_row=last_hour_row, last_exp_c=last_hour_exp, enforce_chart_floor=False,
                )
                if last_hour_row is not None and series['values']:
                    series['values'][-1][1] = series['current']['hour']['value']
                    series['hourly_expected_relays'][-1][1] = series['current']['hour']['expected_relays']
            else:
                cur_hour_n_c = None

            if 'relay_population' in series:
                pop = series['relay_population']
                if 'hour' in pop:
                    pop['hour'] = None if cur_hour_n_c is None else coarsen_population_row(pop.get('hour'), cur_hour_n_c)
                pop['day'] = None if cur_n_c is None else coarsen_population_row(pop.get('day'), cur_n_c)
                pop['daily'] = [
                    crow for prow in pop.get('daily', [])
                    if (t := int(prow[0])) in pop_totals
                    and (target_n := last_daily[2] if (last_daily is not None and t == last_daily[0]) else pop_totals[t]) is not None
                    and (crow := coarsen_population_row(prow, target_n)) is not None
                ]
    return data
