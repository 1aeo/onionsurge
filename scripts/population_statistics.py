"""Aggregate relay counts for the exact windows contributing to each statistic."""
from windows import MAX_SAFE_INT, finite_number, require, timestamp

METHOD = 'eligible-last-flags-v1'
ROLES = ('guard', 'middle_only', 'exit', 'hsdir', 'v2dir')


def _int_mean(total, count):
    return total // count if total % count == 0 else total / count


def _parse_record(row):
    require(isinstance(row, (list, tuple)) and len(row) == 8 and timestamp(row[0])
            and all(finite_number(c) and int(c) == c and 0 <= c <= MAX_SAFE_INT for c in row),
            'Invalid relay population record')
    ints = [int(c) for c in row]
    require(ints[2] <= ints[1] and all(c <= ints[2] for c in ints[3:]),
            'Invalid relay population classification')
    return ints


def _summary(windows, records, available=True, *, prevalidated_records=False):
    """Windows are (end, eligible count); validated records have eight columns."""
    count = len(windows)
    complete = relay_windows = classified = 0
    known = [0, 0, 0, 0, 0]
    for t, n in windows:
        require(timestamp(t), 'Invalid window timestamp')
        require(timestamp(n), 'Invalid relay population count')
        t, n = int(t), int(n)
        relay_windows += n
        if t in records:
            ints = records[t] if prevalidated_records else _parse_record(records[t])
            if ints[1] == n:
                complete += ints[2] == n
                classified += ints[2]
                for i in range(5):
                    known[i] += ints[i + 3]
    require(finite_number(relay_windows) and relay_windows <= MAX_SAFE_INT,
            'Relay-window count exceeds the supported numeric range')
    ready = available and bool(count)
    all_classified = bool(count) and complete == count
    bounded = ready and not all_classified and 0 < classified < relay_windows
    unknown = relay_windows - classified
    return {
        'total': _int_mean(relay_windows, count) if ready else None,
        **{role: _int_mean(known[i], count) if ready and all_classified else None for i, role in enumerate(ROLES)},
        'role_bounds': {role: [known[i] / count, (known[i] + unknown) / count]
                        for i, role in enumerate(ROLES)} if bounded else None,
        'measured_windows': count,
        'classified_windows': complete,
        'relay_windows': relay_windows,
        'classified_relay_windows': classified,
        'coverage': classified / relay_windows if relay_windows else (1 if all_classified else None),
    }


def relay_counts(series, snapshot_end, historical_averages, historical_windows):
    """Missing role evidence leaves roles unknown without changing the day set."""
    require('day' not in historical_windows and set(historical_windows) == set(historical_averages),
            'Historical averages and windows must share non-day summary keys')
    population = series.get('relay_population', {})
    n = series['current']['day'].get('relays')
    row = population.get('day')
    require(row is None or (isinstance(row, (list, tuple)) and row and timestamp(row[0])),
            'Invalid relay population record')
    result = {'day': _summary([] if n is None else [(snapshot_end, n)],
                              {int(row[0]): row} if row is not None else {})}
    daily = {(ints := _parse_record(item))[0]: ints for item in population.get('daily', [])}
    for period, points in historical_windows.items():
        result[period] = _summary([(pt[0], pt[2]) for pt in points], daily,
                                  historical_averages[period]['status'] == 'ready',
                                  prevalidated_records=True)
    return result
