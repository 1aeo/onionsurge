"""Shared test path bootstrap, timestamps, time-range configs, and structural snapshot cloning."""
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import sys

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SCRIPTS_DIR = ROOT / 'scripts'
for directory in (TESTS_DIR, SCRIPTS_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

SOURCE_PATH = ROOT / 'inputs/snapshot.json'


def utc_timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


@lru_cache(maxsize=1)
def load_source():
    return json.loads(SOURCE_PATH.read_text())


def clone_snapshot(value, *, share_rows=True):
    if isinstance(value, dict):
        return {
            k: (list(v) if share_rows and k == 'daily' and isinstance(v, list)
                else clone_snapshot(v, share_rows=share_rows))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [clone_snapshot(item, share_rows=share_rows) for item in value]
    return value


def set_path(root, path, value):
    node = root
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return root


def custom_three_period_config():
    return {
        'version': 1,
        'default_range': '14d',
        'legacy_aliases': {'24h': '14d', '30d': '2mo'},
        'card_slots': {'fallback_dynamic': '14d', 'fixed': ['2mo', 'all']},
        'periods': [
            {
                'id': '14d', 'summary_key': 'fourteen_days', 'export_suffix': '14d',
                'label': '14 days', 'average_label': 'Last 14 days average', 'plot_label': 'Last 14 days',
                'window': {'unit': 'days', 'count': 14, 'comparison_lag_days': 1},
                'minimum_coverage': 0.8, 'minimum_comparison_windows': 10,
            },
            {
                'id': '2mo', 'summary_key': 'two_months', 'export_suffix': '2mo',
                'label': '2 months', 'average_label': 'Last 2 months average', 'plot_label': 'Last 2 months',
                'window': {'unit': 'months', 'count': 2, 'comparison_lag_days': 0},
                'minimum_coverage': 0.85, 'minimum_comparison_windows': 20,
            },
            {
                'id': 'all', 'summary_key': 'all_time', 'export_suffix': 'all-time',
                'label': 'All time', 'average_label': 'All-time average', 'plot_label': 'All time',
                'window': {'unit': 'all_time', 'count': None, 'comparison_lag_days': 0},
                'minimum_coverage': 0, 'minimum_comparison_windows': 30,
            },
        ],
    }


def strict_ranges():
    from time_ranges import load_time_ranges
    cfg = load_time_ranges()
    for period in cfg['periods']:
        if period['window']['unit'] not in {'all_time', 'hours'}:
            period['minimum_coverage'] = 0.9
    return cfg
