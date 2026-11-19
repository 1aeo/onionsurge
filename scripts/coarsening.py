"""Deterministic dir-spec coarsening and validation predicates for public aggregates."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
from functools import lru_cache
import math

from windows import DAY, MAX_SAFE_INT, MINIMUM_POPULATION_COVERAGE, non_negative, require, timestamp

SENSITIVE_CHARTS = frozenset({'hsdir_fetches', 'hsdir_uploads', 'introduction_rejections'})
VALID_KINDS = frozenset({'counter_daily_rate', 'gauge_daily_mean'})
SENSITIVE_BIN = Decimal('0.001')
_D_ONE, _D_EIGHT, _D_HUNDRED, _D_DAY = Decimal(1), Decimal(8), Decimal(100), Decimal(DAY)
_D_RATE_CAP, _D_OPEN_CAP, _D_GAUGE_FLOOR, _D_GAUGE_STEP = Decimal('1e10'), Decimal('1e4'), Decimal('0.05'), Decimal('0.1')
COARSENING_DESCRIPTION = (
    'Deterministic dir-spec coarsening at snapshot boundaries: standalone public relay, flag, '
    'cohort and dircache_cohort counts <8 are floored to 0 and counts >=8 are rounded to the '
    'nearest multiple of 8 (preserving classified <= total and role <= non_middle), while '
    'partitioned build, cohort and middle_only/non-middle role breakdowns distribute multiples '
    'of 8 by largest remainder so parts sum to the coarsened total (individual parts may '
    'differ from nearest-multiple rounding). open_circuits daily means are rounded to the '
    'nearest 100 circuits, then to 3 significant digits; hsdir_fetches, hsdir_uploads, '
    'introduction_rejections, and directory_requests hs_get/hs_post are binned to 0.001/s, '
    'then rounded to 3 significant digits; other counter series suppress <8 events/day, bin '
    'to 8-event increments, and round rates to 3 significant digits; and non-sensitive gauge '
    'means (directory_waiting) floor <0.05 to 0, quantize [0.05, 1) to 0.1, and round >=1 '
    'to 3 significant digits.'
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


def coarsen_value(chart_id, series_key, kind, value):
    """Quantize daily fleet rates and gauge means according to the public coarsening policy."""
    require(isinstance(chart_id, str) and isinstance(series_key, str), 'Chart and series keys must be strings')
    require(isinstance(kind, str) and kind in VALID_KINDS, 'Unsupported measurement kind')
    if value is None:
        return None
    require(non_negative(value), 'Invalid measurement value for coarsening')
    if value == 0:
        return 0.0
    d = Decimal(str(value))
    if chart_id == 'open_circuits':
        q100 = _round_bin(d, _D_HUNDRED)
        if q100 <= 0:
            return 0.0
        q100_sig = _round_bin(Decimal(str(sigfigs3(q100))), _D_HUNDRED)
        return float(q100_sig) if q100_sig > 0 else 0.0
    if is_sensitive_series(chart_id, series_key):
        binned = _round_bin(d, SENSITIVE_BIN)
        return 0.0 if binned <= 0 else sigfigs3(binned)
    if kind == 'counter_daily_rate':
        events = d * _D_DAY
        if events < _D_EIGHT:
            return 0.0
        if events >= _D_RATE_CAP:
            return sigfigs3(d)
        events_int = int(events.to_integral_value(rounding=ROUND_HALF_EVEN))
        return sigfigs3(Decimal(((events_int + 4) // 8) * 8) / _D_DAY)
    if d < _D_GAUGE_FLOOR:
        return 0.0
    if d < _D_ONE:
        return float(d.quantize(_D_GAUGE_STEP, rounding=ROUND_HALF_EVEN))
    return sigfigs3(d)


@lru_cache(maxsize=4096)
def _is_coarsened_float(chart_id, series_key, kind, float_value):
    d = Decimal(str(float_value))
    if not has_at_most_3_sigfigs(d):
        return False
    if chart_id == 'open_circuits':
        if d >= _D_OPEN_CAP:
            return True
        return d == d.to_integral_value() and int(d) % 100 == 0
    if is_sensitive_series(chart_id, series_key):
        if d < SENSITIVE_BIN:
            return False
        if d >= _D_ONE:
            return True
        steps = d / SENSITIVE_BIN
        return steps == steps.to_integral_value()
    if kind == 'counter_daily_rate':
        events = d * _D_DAY
        if events >= _D_RATE_CAP:
            return True
        k0 = int((events / _D_EIGHT).to_integral_value(rounding=ROUND_HALF_EVEN))
        return any(
            k >= 1 and 8 * k < 10**10 and sigfigs3(Decimal(8 * k) / _D_DAY) == float_value
            for k in (k0, k0 - 1, k0 + 1, k0 - 2, k0 + 2)
        )
    return d >= _D_GAUGE_STEP


def is_coarsened_value(chart_id, series_key, kind, value):
    """Validate that a supplied daily rate or gauge mean satisfies public coarsening bounds."""
    require(isinstance(chart_id, str) and isinstance(series_key, str), 'Chart and series keys must be strings')
    require(isinstance(kind, str) and kind in VALID_KINDS, 'Unsupported measurement kind')
    if value is None:
        return True
    if not non_negative(value):
        return False
    if value == 0:
        return True
    return _is_coarsened_float(chart_id, series_key, kind, float(value))


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


def _coarsen_point(chart_id, series_key, kind, raw_val, raw_valid, n_c, cov_c):
    if n_c in (None, 0) or not raw_valid or raw_val is None:
        return None, False
    v_c = coarsen_value(chart_id, series_key, kind, raw_val)
    if v_c is None or cov_c is None or not (MINIMUM_POPULATION_COVERAGE <= cov_c <= 1.0):
        return None, False
    return v_c, True


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

    for chart in data['charts']:
        chart_c = cohort_totals[chart['cohort_key']]
        min_exp = max(8, chart_c)
        chart['relays'] = chart_c
        kind, chart_id = chart['kind'], chart['id']
        for series in chart['series']:
            series_key = series['key']
            coarsened_daily, pop_totals = [], {}
            last_exp_c = min_exp
            for row in series['history']['daily']:
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
                v_c, valid_c = _coarsen_point(chart_id, series_key, kind, v_raw, raw_valid, n_c, cov_c)
                coarsened_daily.append([t, v_c, n_c, cov_c, valid_c])
                pop_totals[t] = n_c
                if t == snapshot_end:
                    last_exp_c = exp_c
            series['history']['daily'] = coarsened_daily

            cur = series['current']['day']
            cur_n_c = coarsen_count(cur.get('relays'))
            expected_raw = chart_c if 'expected_relays' not in cur else cur['expected_relays']
            require(expected_raw is not None, 'Invalid current expected_relays')
            cur_exp_c = max(min_exp, coarsen_count(expected_raw), 0 if cur_n_c is None else cur_n_c)
            last_daily = coarsened_daily[-1] if (coarsened_daily and coarsened_daily[-1][0] == snapshot_end) else None
            if last_daily is not None:
                cur_n_c = last_daily[2]
                cur_exp_c = max(cur_exp_c, last_exp_c, 0 if cur_n_c is None else cur_n_c)
                cur_cov_c = _cov(cur_n_c, cur_exp_c)
                cur_valid_c = bool(last_daily[4] and cur.get('valid', True) and last_daily[1] is not None
                                   and cur_n_c is not None and cur_n_c >= 8
                                   and cur_cov_c is not None and MINIMUM_POPULATION_COVERAGE <= cur_cov_c <= 1.0)
                cur_v_c = last_daily[1] if cur_valid_c else None
                last_daily[1], last_daily[3], last_daily[4] = cur_v_c, cur_cov_c, cur_valid_c
            else:
                cur_cov_c = _cov(cur_n_c, cur_exp_c)
                cur_v_c, cur_valid_c = _coarsen_point(
                    chart_id, series_key, kind, cur.get('value'), cur.get('valid', True), cur_n_c, cur_cov_c
                )
            series['current']['day'] = {
                'value': cur_v_c, 'relays': cur_n_c, 'expected_relays': cur_exp_c,
                'coverage': cur_cov_c, 'valid': cur_valid_c,
            }
            if 'relay_population' in series:
                pop = series['relay_population']
                pop['day'] = None if cur_n_c is None else coarsen_population_row(pop.get('day'), cur_n_c)
                pop['daily'] = [
                    crow for prow in pop.get('daily', [])
                    if (t := int(prow[0])) in pop_totals
                    and (target_n := last_daily[2] if (last_daily is not None and t == last_daily[0]) else pop_totals[t]) is not None
                    and (crow := coarsen_population_row(prow, target_n)) is not None
                ]
    return data
