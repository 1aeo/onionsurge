"""Shared UTC window and observation rules (standard library only)."""
import calendar
from datetime import datetime, timedelta, timezone
import math
import re

DAY = 86400
MINIMUM_POPULATION_COVERAGE = 0.9
MAX_SAFE_INT = 2**53 - 1
RESERVED_JSON_KEYS = frozenset({'__proto__', 'constructor', 'prototype'})
SHA256_RE = re.compile(r'[0-9a-f]{64}')
_INT_FAST_BOUND = 1 << 500


def require(condition, message, exc=ValueError):
    if not condition:
        raise exc(message)


def is_sha256(value):
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def utc_day_end(snapshot_end):
    if not timestamp(snapshot_end):
        raise ValueError('Invalid snapshot end timestamp')
    day_end = int(snapshot_end) // DAY * DAY
    try:
        datetime.fromtimestamp(day_end, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError('Window is outside the supported calendar range') from error
    return day_end


def finite_number(value):
    t = type(value)
    if t is float:
        return math.isfinite(value)
    if t is int and -_INT_FAST_BOUND <= value <= _INT_FAST_BOUND:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def non_negative(value):
    return finite_number(value) and value >= 0 and not (isinstance(value, float) and math.copysign(1.0, value) < 0)


def timestamp(value):
    return non_negative(value) and int(value) == value


def population(value):
    return finite_number(value) and value > 0 and int(value) == value


def usable_measurement(value, relays):
    return non_negative(value) and population(relays)


def calendar_window(snapshot_end, months, lag_days=0):
    """Complete UTC days, with calendar subtraction and month-end clamping."""
    if (not timestamp(snapshot_end) or type(months) is not int or months < 1
            or type(lag_days) is not int or lag_days < 0):
        raise ValueError('Invalid calendar-window parameters')
    try:
        day = datetime.fromtimestamp(snapshot_end, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        end = day - timedelta(days=lag_days)
        year, month = divmod(end.year * 12 + end.month - 1 - months, 12)
        month += 1
        start = end.replace(year=year, month=month,
                            day=min(end.day, calendar.monthrange(year, month)[1]))
        return day, int(start.timestamp()), int(end.timestamp())
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError('Window is outside the supported calendar range') from error


def valid_rows(rows, alignment=None):
    """Unique qualified [end, value, population, coverage, valid] observations.

    Input validation rejects malformed rows. Independently called statistical
    functions still fail closed, and conflicting duplicate windows fail.
    """
    seen = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            continue
        t, *rest = row
        if not timestamp(t) or (alignment and t % alignment):
            continue
        t = int(t)
        normalized = [t, *rest]
        if t in seen and (seen[t] != normalized or any(
                isinstance(a, bool) != isinstance(b, bool)
                for a, b in zip(seen[t], normalized, strict=True))):
            raise ValueError('Conflicting observations for the same interval')
        seen[t] = normalized
    return [row for _, row in sorted(seen.items())
            if row[4] is True and usable_measurement(row[1], row[2])
            and finite_number(row[3])
            and MINIMUM_POPULATION_COVERAGE <= row[3] <= 1]


def valid_current(current):
    return (current.get('valid') is True
            and usable_measurement(current.get('value'), current.get('relays'))
            and finite_number(current.get('coverage'))
            and MINIMUM_POPULATION_COVERAGE <= current['coverage'] <= 1)
