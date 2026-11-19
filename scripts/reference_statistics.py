"""Daily-frozen references and separate retained fleet/normalized peaks."""
from bisect import bisect_left, bisect_right
from datetime import timedelta
from fractions import Fraction
import math
from pathlib import Path
import statistics

from io_helpers import atomic_text, file_lock, json_text, read_json, sha256_hex
from windows import (DAY, calendar_window, finite_number, is_sha256, require,
                     timestamp, valid_current, valid_rows)

METHOD_VERSION = 'midrank-linear-quantiles-18-calendar-months-daily-v3'


def reference_window(snapshot_end):
    day, start, end = calendar_window(snapshot_end, 18, lag_days=1)
    try:
        refresh = int((day + timedelta(days=1)).timestamp())
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError('Reference window is outside the supported calendar range') from error
    return {'date': day.date().isoformat(), 'start': start, 'end': end,
            'next_refresh': refresh,
            'months': 18, 'method_version': METHOD_VERSION}


def quantile(values, fraction):
    require(0 <= fraction <= 1, 'Quantile fraction must be between zero and one')
    return _quantile_sorted(sorted(values), fraction) if values else None


def _quantile_sorted(values, fraction):
    """Interpolate an already sorted, nonempty distribution without re-sorting."""
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def method_fingerprint():
    paths = [Path(__file__), Path(__file__).with_name('windows.py')]
    return sha256_hex(b'\0'.join(path.read_bytes() for path in paths))


def _check_cache(payload, window, catalogue_sha, method_sha, charts):
    fields = {
            'window', 'catalogue_sha256', 'method_sha256', 'source_input_sha256',
            'source_snapshot_end', 'distributions'}
    if not isinstance(payload, dict) or not set(payload) <= fields:
        raise ValueError('Reference cache must contain exactly the public payload fields')
    if (payload.get('window') != window or payload.get('catalogue_sha256') != catalogue_sha
            or payload.get('method_sha256') != method_sha):
        raise ValueError('Reference cache identity mismatch')
    source_sha = payload.get('source_input_sha256')
    source_end = payload.get('source_snapshot_end')
    require(is_sha256(source_sha)
            and timestamp(source_end) and window['next_refresh'] - DAY <= source_end < window['next_refresh'],
            'Reference cache source provenance is missing or invalid')
    require(set(payload) == fields, 'Reference cache is missing public payload fields')
    distributions = payload['distributions']
    require(isinstance(distributions, dict) and set(distributions) == {c['id'] for c in charts}, 'Reference cache chart mismatch')
    for chart in charts:
        found = distributions[chart['id']]
        require(isinstance(found, dict) and set(found) == {s['key'] for s in chart['series']}, 'Reference cache series mismatch')
        for series in found.values():
            require(isinstance(series, dict) and set(series) == {'day'}, 'Reference cache period mismatch')
            require(isinstance(series['day'], list), 'Reference cache observations must be arrays')
            prior = None
            for point in series['day']:
                require(isinstance(point, list) and len(point) == 2
                        and timestamp(point[0]) and point[0] % DAY == 0
                        and finite_number(point[1]) and point[1] >= 0
                        and window['start'] <= point[0] - DAY and point[0] <= window['end']
                        and (prior is None or point[0] > prior),
                        'Invalid reference cache observation')
                prior = point[0]


def frozen_reference(charts, snapshot_end, cache_directory, input_sha, refresh=False, qualification=None):
    require(all(c.get('history_status') == 'ready' for c in charts), 'Reference creation requires complete history inputs')
    require(is_sha256(input_sha), 'Reference input identity must be a SHA-256 digest')
    window = reference_window(snapshot_end)
    catalogue = {'charts': [dict({k: c[k] for k in
                    ('id', 'kind', 'query', 'quality_query', 'history_queries')},
                    series_keys=sorted(s['key'] for s in c['series'])) for c in charts],
                 'normalization_schema': 'qualified-window-per-reporting-relay-v1',
                 'qualification': qualification}
    catalogue_sha = sha256_hex(json_text(catalogue).encode())
    method_sha = method_fingerprint()
    cache_key = sha256_hex((METHOD_VERSION + catalogue_sha + method_sha).encode())[:16]
    path = Path(cache_directory) / window['date'] / (cache_key + '.json')
    encoded = None
    with file_lock(path.with_suffix('.lock')):
        if path.exists() and not refresh:
            payload = read_json(path)
            _check_cache(payload, window, catalogue_sha, method_sha, charts)
        else:
            distributions = {}
            for chart in charts:
                distributions[chart['id']] = {}
                for series in chart['series']:
                    distributions[chart['id']][series['key']] = {
                        'day': [[row[0], row[1] / row[2]]
                                for row in valid_rows(series['history'].get('daily', []), alignment=DAY)
                                if row[0] - DAY >= window['start'] and row[0] <= window['end']]
                    }
            payload = {'window': window, 'catalogue_sha256': catalogue_sha,
                       'method_sha256': method_sha, 'source_input_sha256': input_sha,
                       'source_snapshot_end': snapshot_end, 'distributions': distributions}
            encoded = json_text(payload)
            atomic_text(path, encoded)
    if encoded is None:
        encoded = json_text(payload)
    digest = sha256_hex(encoded.encode())
    return payload, encoded, f'reference-{window["date"]}-{digest[:12]}', digest


def describe_reference(points, current):
    if not points:
        return {'n': 0, 'start': None, 'end': None, 'mean_per_relay': None,
                'current_percentile': None, 'flat_zero': False}
    values = []
    start_t = end_t = points[0][0]
    all_zero = True
    for t, val in points:
        require(finite_number(val) and val >= 0, 'Invalid normalized reference value')
        values.append(val)
        if t < start_t:
            start_t = t
        if t > end_t:
            end_t = t
        if val != 0:
            all_zero = False
    ordered = sorted(values)
    current_valid = valid_current(current)
    normalized = current['value'] / current['relays'] if current_valid else None
    flat_zero = all_zero and current_valid and current['value'] == 0
    if normalized is None or flat_zero:
        rank = None
    else:
        seq, target = (([Fraction(v) for v in ordered], Fraction(current['value']) / int(current['relays']))
                       if normalized == 0 and current['value'] > 0 else (ordered, normalized))
        below = bisect_left(seq, target)
        rank = 100 * (below + .5 * (bisect_right(seq, target) - below)) / len(seq)
    return {'n': len(values), 'start': start_t, 'end': end_t,
            'mean_per_relay': statistics.mean(values),
            **{f'p{p}_per_relay': _quantile_sorted(ordered, p / 100) for p in (10, 50, 90, 95, 99)},
            'max_per_relay': max(values), 'current_percentile': rank,
            'flat_zero': flat_zero, 'reference_all_zero': all_zero,
            'all_equal': ordered[0] == ordered[-1]}


def retained_peak(rows, current, snapshot_end, additional=(), *, valid_daily=None, ratios=None):
    """Additional rows carry the same coverage/validity fields as history rows."""
    base_rows = valid_rows(rows) if valid_daily is None else valid_daily
    points = {
        p[0]: (p[1], p[2], ratios[p[0]] if ratios is not None else Fraction(p[1]) / int(p[2]))
        for p in base_rows if p[0] <= snapshot_end
    }
    additional = list(additional)
    if additional:
        for row in additional:
            if isinstance(row, (list, tuple)) and len(row) == 5 and timestamp(row[0]) and row[0] <= snapshot_end:
                # The recent grid also supersedes a sampled value when it records a gap or failed qualification.
                points.pop(row[0], None)
        for row in valid_rows(additional):
            t, value, relays = row[:3]
            if t <= snapshot_end:
                points[t] = (value, relays, Fraction(value) / int(relays))
    current_valid = valid_current(current)
    if current_valid:
        value, relays = current['value'], current['relays']
        points[snapshot_end] = (value, relays, Fraction(value) / int(relays))
    else:
        points.pop(snapshot_end, None)
    if not points:
        return None
    normalized_at, normalized_peak = max(points.items(), key=lambda item: (item[1][2], -item[0]))
    raw_at, raw_peak = max(points.items(), key=lambda item: (item[1][0], -item[0]))
    has_prior = len(points) > (1 if current_valid else 0)
    return {'value_per_relay': normalized_peak[0] / normalized_peak[1], 'raw_fleet_value': normalized_peak[0],
            'eligible_relays': normalized_peak[1], 'at': normalized_at,
            'observations': len(points),
            'current_is_record': bool(has_prior and current_valid and raw_at == snapshot_end),
            'current_is_normalized_record': bool(has_prior and current_valid and normalized_at == snapshot_end),
            'fleet_peak': raw_peak[0], 'fleet_peak_at': raw_at, 'fleet_peak_relays': raw_peak[1]}
