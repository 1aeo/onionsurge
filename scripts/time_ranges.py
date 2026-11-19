"""Declarative time-range configuration loading, validation, and UTC window bounds."""
import copy
import math
from pathlib import Path
import re

from io_helpers import read_json
from windows import (DAY, RESERVED_JSON_KEYS, calendar_window, finite_number,
                     require as _require, utc_day_end as _day_end)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIME_RANGES_PATH = ROOT / 'inputs/time_ranges.json'

TOP_KEYS = {'version', 'default_range', 'legacy_aliases', 'card_slots', 'periods'}
PERIOD_KEYS = {
    'id', 'summary_key', 'export_suffix', 'label', 'average_label',
    'plot_label', 'window', 'minimum_coverage', 'minimum_comparison_windows',
}
WINDOW_KEYS = {'unit', 'count', 'comparison_lag_days'}
CARD_SLOT_KEYS = {'fallback_dynamic', 'fixed'}
FORBIDDEN_HOURLY_KEYS = {
    'hour', 'sampled_hour', 'hourly_relays', 'hourly_expected_relays', 'current24',
}

PERIOD_ID_RE = re.compile(r'^(?!.*(?:^|-)(?:24h|1d|[0-9]+[smh])\Z)[a-z0-9][a-z0-9_-]{0,31}\Z')
SUMMARY_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}\Z')
EXPORT_SUFFIX_RE = re.compile(r'^(?!.*(?:^|-)(?:24h|1d|[0-9]+[smh])\Z)[a-z0-9]+(?:-[a-z0-9]+){0,7}\Z')
NON_BLANK_RE = re.compile(r'[^\s\x1c-\x1f\x85\ufeff]')


_WINDOW_BOUNDS = {'days': ('Days', 2, 36500), 'months': ('Months', 1, 1200)}


def _check_unique(value, valid, seen, label):
    _require(valid, f'Invalid time-range {label}')
    _require(value not in seen, f'Duplicate time-range {label}')
    seen.add(value)


def _validate_window(window):
    _require(isinstance(window, dict) and set(window) == WINDOW_KEYS,
             'Period window must contain unit, count and comparison_lag_days')
    unit = window['unit']
    count = window['count']
    lag = window['comparison_lag_days']
    _require(unit in {'days', 'months', 'all_time'}, 'Unsupported time-range window unit')
    if unit in _WINDOW_BOUNDS:
        name, lo, hi = _WINDOW_BOUNDS[unit]
        _require(type(count) is int and lo <= count <= hi, f'{name} window count must be an integer between {lo} and {hi}')
        _require(type(lag) is int and lag in {0, 1}, f'{name} window comparison_lag_days must be 0 or 1')
    else:
        _require(count is None, 'All-time window count must be null')
        _require(type(lag) is int and lag == 0,
                 'All-time window comparison_lag_days must be 0')
    return unit, count, lag


def _validate_period(period, seen_ids, seen_summary_keys, seen_suffixes):
    _require(isinstance(period, dict) and set(period) == PERIOD_KEYS,
             'Time-range period does not match required schema')
    period_id = period['id']
    _check_unique(period_id, isinstance(period_id, str) and PERIOD_ID_RE.fullmatch(period_id)
                  and period_id not in RESERVED_JSON_KEYS, seen_ids, 'period ID')

    summary_key = period['summary_key']
    _check_unique(summary_key, isinstance(summary_key, str) and SUMMARY_KEY_RE.fullmatch(summary_key)
                  and summary_key != 'day' and summary_key not in RESERVED_JSON_KEYS
                  and summary_key not in FORBIDDEN_HOURLY_KEYS, seen_summary_keys, 'summary_key')

    suffix = period['export_suffix']
    _check_unique(suffix, isinstance(suffix, str) and len(suffix) <= 32 and EXPORT_SUFFIX_RE.fullmatch(suffix)
                  and suffix not in RESERVED_JSON_KEYS, seen_suffixes, 'export_suffix')

    for field in ('label', 'average_label', 'plot_label'):
        value = period[field]
        _require(isinstance(value, str) and bool(NON_BLANK_RE.search(value)) and len(value) <= 80,
                 f'Invalid time-range {field}')

    unit, _, _ = _validate_window(period['window'])

    coverage = period['minimum_coverage']
    msg = 'All-time period minimum_coverage must be 0' if unit == 'all_time' else 'Time-range minimum_coverage must be in [0, 1]'
    _require(finite_number(coverage) and not (isinstance(coverage, float) and math.copysign(1.0, coverage) < 0), msg)
    _require(coverage == 0 if unit == 'all_time' else 0 <= coverage <= 1, msg)

    min_comp = period['minimum_comparison_windows']
    _require(type(min_comp) is int and 1 <= min_comp <= 10000,
             'Time-range minimum_comparison_windows must be an integer between 1 and 10000')
    return unit


def validate_time_ranges(config):
    """Validate declarative time-range configuration and fail closed on invalid shapes."""
    _require(isinstance(config, dict) and set(config) == TOP_KEYS,
             'Time-range config does not match required top-level schema')
    _require(type(config['version']) is int and config['version'] == 1,
             'Unsupported time-range config version')

    periods = config['periods']
    _require(isinstance(periods, list) and 1 <= len(periods) <= 16,
             'Time-range periods must contain between 1 and 16 entries')

    seen_ids = set()
    seen_summary_keys = set()
    seen_suffixes = set()
    all_time_count = 0
    for period in periods:
        if _validate_period(period, seen_ids, seen_summary_keys, seen_suffixes) == 'all_time':
            all_time_count += 1
    _require(all_time_count == 1, 'Time-range config must declare exactly one all_time period')

    default_range = config['default_range']
    _require(isinstance(default_range, str) and default_range in seen_ids,
             'Time-range default_range must match a declared period ID')

    aliases = config['legacy_aliases']
    _require(isinstance(aliases, dict), 'Time-range legacy_aliases must be a mapping')
    for alias, target in aliases.items():
        _require(isinstance(alias, str) and 0 < len(alias) <= 32 and bool(NON_BLANK_RE.search(alias))
                 and alias not in RESERVED_JSON_KEYS and alias not in FORBIDDEN_HOURLY_KEYS
                 and alias not in seen_ids,
                 'Invalid time-range legacy alias key')
        _require(isinstance(target, str) and target in seen_ids,
                 'Time-range legacy alias must target a declared period ID')

    card_slots = config['card_slots']
    _require(isinstance(card_slots, dict) and set(card_slots) == CARD_SLOT_KEYS,
             'Time-range card_slots must contain fallback_dynamic and fixed')
    fixed = card_slots['fixed']
    _require(isinstance(fixed, list) and len(fixed) == 2
             and all(isinstance(item, str) and item in seen_ids for item in fixed)
             and fixed[0] != fixed[1],
             'Time-range card_slots.fixed must contain two distinct declared period IDs')
    fallback = card_slots['fallback_dynamic']
    _require(isinstance(fallback, str) and fallback in seen_ids and fallback not in fixed,
             'Time-range card_slots.fallback_dynamic must be a period ID outside fixed')

    return copy.deepcopy(config)


def load_time_ranges(path=None):
    """Load and validate a time-range configuration JSON file."""
    target = DEFAULT_TIME_RANGES_PATH if path is None else Path(path)
    return validate_time_ranges(read_json(target))


def _window_context(snapshot_end, period_def):
    day_end = _day_end(snapshot_end)
    _require(isinstance(period_def, dict) and 'window' in period_def,
             'Time-range period must define window')
    unit, count, lag = _validate_window(period_def['window'])
    return day_end, unit, count, lag


def _unit_start(day_end, unit, count, lag=0):
    if unit == 'days':
        return day_end - (count + lag) * DAY
    if unit == 'months':
        return calendar_window(day_end, count, lag_days=lag)[1]
    return None


def resolve_period_bounds(snapshot_end, config):
    """Compute period and comparison UTC bounds once for a validated time-ranges config."""
    day_end = _day_end(snapshot_end)
    result = []
    for period in config['periods']:
        w = period['window']
        period_start = _unit_start(day_end, w['unit'], w['count'])
        comp_start = period_start if w['comparison_lag_days'] == 0 else _unit_start(day_end, w['unit'], w['count'], w['comparison_lag_days'])
        result.append((period, period_start, comp_start))
    return day_end, day_end - DAY, result


def period_window_bounds(snapshot_end, period_def):
    """Return (period_start, day_end) UTC timestamps for a configured period."""
    day_end, unit, count, _ = _window_context(snapshot_end, period_def)
    return _unit_start(day_end, unit, count), day_end


def comparison_window_bounds(snapshot_end, period_def):
    """Return (comparison_start, cutoff) UTC timestamps excluding current.day.

    For ``days``, ``lag`` shifts the start back ``lag`` UTC days so ``count``
    prior days remain; for ``months``, ``calendar_window(..., lag_days=lag)``
    anchors calendar subtraction at ``day_end - lag`` days with month-end clamping.
    """
    day_end, unit, count, lag = _window_context(snapshot_end, period_def)
    return _unit_start(day_end, unit, count, lag), day_end - DAY
