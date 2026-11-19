"""Observed fleet averages, distinct from normalized reference statistics."""
from bisect import bisect_left
from fractions import Fraction
import statistics

from time_ranges import comparison_window_bounds, load_time_ranges, resolve_period_bounds, validate_time_ranges
from windows import DAY, require, timestamp, usable_measurement, valid_current, valid_rows

COMPARISON_METHOD = 'daily-per-relay-midrank-v1'


def historical_comparison(points, current_day, cutoff, available, *, ratios=None):
    """Rank the current day against nonoverlapping, already-qualified daily rows."""
    has_current = bool(available and valid_current(current_day))
    cur_ratio = Fraction(current_day['value']) / Fraction(current_day['relays']) if has_current else None
    n = below = equal = 0
    start = end = None
    all_zero = True
    for row in points:
        require(isinstance(row, (list, tuple)) and len(row) >= 3
                and timestamp(row[0]) and usable_measurement(row[1], row[2]),
                'Invalid comparison point')
        t = row[0]
        if t > cutoff:
            continue
        n += 1
        start = t - DAY if start is None else min(start, t - DAY)
        end = t if end is None else max(end, t)
        all_zero = all_zero and row[1] == 0
        if cur_ratio is not None:
            row_ratio = ratios[t] if ratios is not None else Fraction(row[1]) / Fraction(row[2])
            below += row_ratio < cur_ratio
            equal += row_ratio == cur_ratio
    flat_zero = bool(n and cur_ratio == 0 and all_zero)
    rank = 100 * (below + .5 * equal) / n if n and cur_ratio is not None and not flat_zero else None
    return {'method': COMPARISON_METHOD, 'cutoff': cutoff, 'n': n, 'start': start, 'end': end,
            'current_percentile': rank, 'flat_zero': flat_zero}


def period_comparison_rows(rows, snapshot_end, period_def):
    """Return qualified daily rows for one period's non-overlapping comparison window."""
    comp_start, cutoff = comparison_window_bounds(snapshot_end, period_def)
    daily = valid_rows(rows, alignment=DAY)
    if comp_start is None:
        return [row for row in daily if row[0] <= cutoff]
    return [row for row in daily if row[0] - DAY >= comp_start and row[0] <= cutoff]


def historical_averages_and_windows(rows, snapshot_end, time_ranges=None, *, daily_rows=None, bounds=None):
    """Return the summaries and their exact contributing daily observations."""
    if bounds is None:
        config = load_time_ranges() if time_ranges is None else validate_time_ranges(time_ranges)
        day_end, _, period_bounds = resolve_period_bounds(snapshot_end, config)
    else:
        day_end, period_bounds = bounds
    if daily_rows is None:
        source_rows = valid_rows(rows, alignment=DAY)
    elif len(daily_rows) > 1 and any(daily_rows[i][0] > daily_rows[i + 1][0] for i in range(len(daily_rows) - 1)):
        source_rows = sorted(daily_rows, key=lambda row: row[0])
    else:
        source_rows = daily_rows
    daily = [row for row in source_rows if row[0] <= day_end]

    def summarize(points, period_start, minimum_coverage):
        expected = (day_end - period_start) // DAY if period_start is not None else 0
        count = len(points)
        coverage = count / expected if expected else 0
        ready = bool(count and coverage >= minimum_coverage)
        return {
            'value': statistics.mean(row[1] for row in points) if ready else None,
            'status': 'ready' if ready else 'insufficient_data',
            'days': count, 'expected_days': expected, 'coverage': coverage,
            'minimum_coverage': minimum_coverage, 'start': period_start, 'end': day_end,
            'observed_start': points[0][0] - DAY if points else None,
            'observed_end': points[-1][0] if points else None,
        }

    daily_ts = [row[0] for row in daily]
    summaries = {}
    windows = {}
    for period, period_start, _ in period_bounds:
        key = period['summary_key']
        if period['window']['unit'] == 'all_time':
            points = list(daily)
            summary_start = daily[0][0] - DAY if daily else None
        else:
            points = daily[bisect_left(daily_ts, period_start + DAY):]
            summary_start = period_start
        windows[key] = points
        summaries[key] = summarize(points, summary_start, period['minimum_coverage'])
    return summaries, windows


def historical_averages(rows, snapshot_end, time_ranges=None):
    return historical_averages_and_windows(rows, snapshot_end, time_ranges=time_ranges)[0]
