import copy
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, SOURCE_PATH, custom_three_period_config, utc_timestamp as timestamp
import build as builder
from time_ranges import (
    DEFAULT_TIME_RANGES_PATH,
    comparison_window_bounds,
    load_time_ranges,
    period_window_bounds,
    validate_time_ranges,
)
from windows import DAY, HOUR


class TimeRangeConfigTests(unittest.TestCase):
    def fixture(self):
        return load_time_ranges()

    def test_default_time_ranges_config_matches_contract(self):
        self.assertTrue(DEFAULT_TIME_RANGES_PATH.is_file())
        cfg = load_time_ranges()
        self.assertEqual(
            set(cfg),
            {'version', 'default_range', 'legacy_aliases', 'card_slots', 'periods'},
        )
        self.assertEqual(cfg['version'], 1)
        self.assertEqual(cfg['default_range'], '24h')
        self.assertEqual(
            cfg['legacy_aliases'],
            {'30d': '1mo', '1m': '1mo', '3m': '3mo', '6m': '6mo', '12m': '12mo'},
        )
        self.assertEqual(cfg['card_slots'], {'fallback_dynamic': '7d', 'fixed': ['24h', 'all']})
        self.assertEqual(
            [p['id'] for p in cfg['periods']],
            ['24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all'],
        )
        self.assertEqual(
            [p['summary_key'] for p in cfg['periods']],
            ['twenty_four_hours', 'seven_days', 'one_month', 'three_months', 'six_months', 'twelve_months', 'all_time'],
        )
        self.assertEqual(
            [p['export_suffix'] for p in cfg['periods']],
            ['24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all-time'],
        )
        self.assertTrue(all(p['minimum_coverage'] == 0 for p in cfg['periods']))
        for zero_cov in (0, 0.0):
            allowed = self.fixture()
            allowed['periods'][0]['minimum_coverage'] = zero_cov
            validated = validate_time_ranges(allowed)
            self.assertEqual(validated['periods'][0]['minimum_coverage'], zero_cov)
        for bounded_cov in (0, 0.0, 0.9, 1):
            allowed = self.fixture()
            allowed['periods'][1]['minimum_coverage'] = bounded_cov
            allowed['periods'][2]['minimum_coverage'] = bounded_cov
            validated = validate_time_ranges(allowed)
            self.assertEqual(validated['periods'][1]['minimum_coverage'], bounded_cov)
            self.assertEqual(validated['periods'][2]['minimum_coverage'], bounded_cov)

    def test_validation_rejects_sub_24h_and_malformed_ids_and_suffixes(self):
        bad_tokens = [
            '24h', '1d', '1h', '12h', '30m', '1m', '60s', '0s',
            'foo-24h', 'foo-1d', 'foo-1h', 'foo-30m', 'foo-60s',
            '24h\n', '7d\n', '', '-7d', '7D', 'a' * 33, 'constructor', 'prototype', '__proto__',
        ]
        for token in bad_tokens:
            cfg = self.fixture()
            cfg['periods'][1]['id'] = token
            with self.subTest(field='id', token=token), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

            cfg = self.fixture()
            cfg['periods'][1]['export_suffix'] = token
            with self.subTest(field='export_suffix', token=token), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

        for bad_suffix in ['foo_bar', 'a-b-c-d-e-f-g-h-i', 'aaaa-bbbb-cccc-dddd-eeee-ffff-gggg']:
            cfg = self.fixture()
            cfg['periods'][1]['export_suffix'] = bad_suffix
            with self.subTest(bad_suffix=bad_suffix), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

        for bad_summary in [
            'day', '7d', 'foo-bar', '', 'a' * 65, 'constructor', 'prototype',
            'hour', 'sampled_hour', 'hourly_relays', 'hourly_expected_relays', 'current24',
        ]:
            cfg = self.fixture()
            cfg['periods'][1]['summary_key'] = bad_summary
            with self.subTest(bad_summary=bad_summary), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

        for bad_alias in ['hour', 'sampled_hour', 'hourly_relays', 'hourly_expected_relays', 'current24', '24h']:
            cfg = self.fixture()
            cfg['legacy_aliases'] = {bad_alias: '1mo'}
            with self.subTest(bad_alias=bad_alias), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

        # '24h' legacy alias remains valid only when no '24h' hours period is declared
        cfg = custom_three_period_config()
        cfg['legacy_aliases'] = {'24h': '14d'}
        self.assertEqual(validate_time_ranges(cfg)['legacy_aliases'], {'24h': '14d'})

        # Hours period enforces minimum_coverage == 0 cleanly (aligned with site/app.js)
        cfg = self.fixture()
        self.assertEqual(cfg['periods'][0]['window']['unit'], 'hours')
        self.assertEqual(cfg['periods'][0]['minimum_coverage'], 0)
        validate_time_ranges(cfg)
        cfg['periods'][0]['minimum_coverage'] = 0.5
        with self.assertRaisesRegex(ValueError, 'All-time and hours period minimum_coverage must be 0'):
            validate_time_ranges(cfg)

    def test_validation_enforces_schema_uniqueness_bounds_and_slots(self):
        mutations = [
            lambda c: c.update(extra=1),
            lambda c: c.pop('periods'),
            lambda c: c.update(version=2),
            lambda c: c.update(version=True),
            lambda c: c.update(version=1.0),
            lambda c: c.update(periods=[]),
            lambda c: c.update(periods=c['periods'][:-1]),  # 0 all_time
            lambda c: c['periods'].append(dict(c['periods'][-1], id='all2', summary_key='all2', export_suffix='all2')),
            lambda c: c['periods'][1].update(id=c['periods'][0]['id']),
            lambda c: c['periods'][1].update(summary_key=c['periods'][0]['summary_key']),
            lambda c: c['periods'][1].update(export_suffix=c['periods'][0]['export_suffix']),
            lambda c: c['periods'][0].update(extra='no'),
            lambda c: c['periods'][0].update(label='   '),
            lambda c: c['periods'][0].update(label='\ufeff'),
            lambda c: c['periods'][0].update(label='\x85'),
            lambda c: c['periods'][0].update(average_label='x' * 81),
            lambda c: c['periods'][0].update(average_label='\ufeff\x85'),
            lambda c: c['periods'][0].update(plot_label=''),
            lambda c: c['periods'][0].update(plot_label='\ufeff'),
            lambda c: c['periods'][1].update(id='24h'),
            lambda c: c['periods'][1].update(export_suffix='24h'),
            lambda c: c['periods'][0].update(id='24hr'),
            lambda c: c['periods'][0].update(export_suffix='24hr'),
            lambda c: c['periods'][1].update(window={'unit': 'hours', 'count': 24, 'comparison_lag_days': 0}),
            lambda c: c['periods'][0].update(window={'unit': 'hours', 'count': 12, 'comparison_lag_days': 0}),
            lambda c: c['periods'][0].update(window={'unit': 'hours', 'count': 24, 'comparison_lag_days': 1}),
            lambda c: c['periods'][1].update(window={'unit': 'days', 'count': 1, 'comparison_lag_days': 0}),
            lambda c: c['periods'][1].update(window={'unit': 'days', 'count': 36501, 'comparison_lag_days': 0}),
            lambda c: c['periods'][1].update(window={'unit': 'days', 'count': True, 'comparison_lag_days': 0}),
            lambda c: c['periods'][1].update(window={'unit': 'days', 'count': 7.0, 'comparison_lag_days': 0}),
            lambda c: c['periods'][1].update(window={'unit': 'days', 'count': 7, 'comparison_lag_days': 2}),
            lambda c: c['periods'][2].update(window={'unit': 'months', 'count': 0, 'comparison_lag_days': 0}),
            lambda c: c['periods'][2].update(window={'unit': 'months', 'count': 1201, 'comparison_lag_days': 0}),
            lambda c: c['periods'][2].update(window={'unit': 'months', 'count': 1, 'comparison_lag_days': 2}),
            lambda c: c['periods'][-1].update(window={'unit': 'all_time', 'count': 1, 'comparison_lag_days': 0}),
            lambda c: c['periods'][-1].update(window={'unit': 'all_time', 'count': None, 'comparison_lag_days': 1}),
            lambda c: c['periods'][1].update(minimum_coverage=-0.0),
            lambda c: c['periods'][1].update(minimum_coverage=-0.1),
            lambda c: c['periods'][2].update(minimum_coverage=-0.0),
            lambda c: c['periods'][2].update(minimum_coverage=-0.1),
            lambda c: c['periods'][1].update(minimum_coverage=1.1),
            lambda c: c['periods'][1].update(minimum_coverage=True),
            lambda c: c['periods'][1].update(minimum_coverage=float('nan')),
            lambda c: c['periods'][-1].update(minimum_coverage=0.5),
            lambda c: c['periods'][-1].update(minimum_coverage=-0.0),
            lambda c: c['periods'][0].update(minimum_coverage=0.5),
            lambda c: c['periods'][0].update(minimum_coverage=-0.0),
            lambda c: c['periods'][1].update(minimum_comparison_windows=0),
            lambda c: c['periods'][1].update(minimum_comparison_windows=10001),
            lambda c: c['periods'][1].update(minimum_comparison_windows=True),
            lambda c: c.update(default_range='missing'),
            lambda c: c.update(legacy_aliases={'': '1mo'}),
            lambda c: c.update(legacy_aliases={'  ': '1mo'}),
            lambda c: c.update(legacy_aliases={'\ufeff': '1mo'}),
            lambda c: c.update(legacy_aliases={'\x85': '1mo'}),
            lambda c: c.update(legacy_aliases={'\ufeff\x85\x1c ': '1mo'}),
            lambda c: c.update(legacy_aliases={'a' * 33: '1mo'}),
            lambda c: c.update(legacy_aliases={'7d': '1mo'}),
            lambda c: c.update(legacy_aliases={'24h': '1mo'}),
            lambda c: c.update(legacy_aliases={'constructor': '1mo'}),
            lambda c: c.update(legacy_aliases={'alias': 'missing'}),
            lambda c: c.update(card_slots={'fallback_dynamic': '1mo', 'fixed': ['6mo']}),
            lambda c: c.update(card_slots={'fallback_dynamic': '1mo', 'fixed': ['24h', '24h']}),
            lambda c: c.update(card_slots={'fallback_dynamic': '24h', 'fixed': ['24h', 'all']}),
            lambda c: c.update(card_slots={'fallback_dynamic': '1mo', 'fixed': ['6mo', 'all']}),
            lambda c: c.update(card_slots={'fallback_dynamic': '6mo', 'fixed': ['24h', 'all'], 'extra': '1mo'}),
        ]
        for mutate in mutations:
            cfg = self.fixture()
            mutate(cfg)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_time_ranges(cfg)

        # > 16 periods rejected
        cfg = self.fixture()
        while len(cfg['periods']) <= 16:
            idx = len(cfg['periods'])
            cfg['periods'].insert(0, {
                'id': f'p{idx}',
                'summary_key': f'period_{idx}',
                'export_suffix': f'p{idx}',
                'label': f'P{idx}',
                'average_label': f'P{idx} avg',
                'plot_label': f'P{idx}',
                'window': {'unit': 'days', 'count': 7 + idx, 'comparison_lag_days': 0},
                'minimum_coverage': 0.9,
                'minimum_comparison_windows': 10,
            })
        with self.assertRaises(ValueError):
            validate_time_ranges(cfg)

        # Duplicate JSON keys rejected when loading from file
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dup.json'
            path.write_text('{"version":1,"version":1}')
            with self.assertRaisesRegex(ValueError, 'Duplicate JSON object key'):
                load_time_ranges(path)

    def test_window_and_comparison_bounds_math(self):
        cfg = self.fixture()
        by_id = {p['id']: p for p in cfg['periods']}
        end_leap = timestamp('2024-03-31')
        self.assertEqual(
            period_window_bounds(end_leap + 5 * HOUR, by_id['24h']),
            (end_leap + 5 * HOUR - 24 * HOUR, end_leap + 5 * HOUR),
        )
        with self.assertRaises(ValueError):
            comparison_window_bounds(end_leap + 5 * HOUR, by_id['24h'])
        self.assertEqual(
            period_window_bounds(end_leap + 5 * HOUR, by_id['7d']),
            (end_leap - 7 * DAY, end_leap),
        )
        self.assertEqual(
            comparison_window_bounds(end_leap + 5 * 3600, by_id['7d']),
            (end_leap - 8 * DAY, end_leap - DAY),
        )
        self.assertEqual(
            period_window_bounds(end_leap, by_id['1mo']),
            (timestamp('2024-02-29'), end_leap),
        )
        self.assertEqual(
            comparison_window_bounds(end_leap, by_id['1mo']),
            (timestamp('2024-02-29'), end_leap - DAY),
        )
        lagged_month = copy.deepcopy(by_id['1mo'])
        lagged_month['window']['comparison_lag_days'] = 1
        self.assertEqual(
            comparison_window_bounds(timestamp('2026-05-31'), lagged_month),
            (timestamp('2026-04-30'), timestamp('2026-05-30')),
        )
        self.assertEqual(period_window_bounds(end_leap, by_id['all']), (None, end_leap))
        self.assertEqual(comparison_window_bounds(end_leap, by_id['all']), (None, end_leap - DAY))

        for bad_ts in [True, -1, 1.5, float('inf'), 10**100]:
            with self.subTest(bad_ts=bad_ts), self.assertRaises(ValueError):
                period_window_bounds(bad_ts, by_id['7d'])
            with self.subTest(bad_ts=bad_ts), self.assertRaises(ValueError):
                comparison_window_bounds(bad_ts, by_id['7d'])

    def test_build_with_custom_three_period_config_requires_zero_code_edits(self):
        custom = custom_three_period_config()
        validate_time_ranges(custom)
        source_path = SOURCE_PATH
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg_path = root / 'custom_time_ranges.json'
            cfg_bytes = (json.dumps(custom, indent=2) + '\n').encode('utf-8')
            cfg_path.write_bytes(cfg_bytes)
            default_out = root / 'dist-default'
            custom_out = root / 'dist-custom'
            cli_out = root / 'dist-cli'
            cache = root / 'cache'

            builder.build(source_path, default_out, cache)
            builder.build(source_path, custom_out, cache, time_ranges_path=cfg_path)

            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            proc = subprocess.run(
                [
                    sys.executable,
                    '-B',
                    str(ROOT / 'scripts/build.py'),
                    '--input', str(source_path),
                    '--output', str(cli_out),
                    '--cache', str(cache),
                    '--time-ranges', str(cfg_path),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            default_data = json.loads((default_out / 'data.json').read_text())
            data = json.loads((custom_out / 'data.json').read_text())
            cli_data = json.loads((cli_out / 'data.json').read_text())
            self.assertEqual(data, cli_data)

            self.assertEqual(data['schema_version'], 7)
            self.assertEqual(data['time_ranges'], custom)
            self.assertEqual(
                data['provenance']['time_ranges_sha256'],
                hashlib.sha256(cfg_bytes).hexdigest(),
            )
            self.assertEqual(
                data['provenance']['time_ranges_method_sha256'],
                hashlib.sha256((ROOT / 'scripts/time_ranges.py').read_bytes()).hexdigest(),
            )
            # Frozen reference is completely independent of time_ranges config.
            self.assertEqual(data['reference_id'], default_data['reference_id'])
            self.assertEqual(data['reference_sha256'], default_data['reference_sha256'])
            self.assertEqual(data['reference_document'], default_data['reference_document'])

            csv_files = sorted(p.name for p in (custom_out / 'exports').glob('*.csv'))
            expected_csvs = sorted(
                f"{chart['id']}-{p['export_suffix']}.csv"
                for chart in data['charts']
                for p in custom['periods']
            )
            self.assertEqual(csv_files, expected_csvs)
            self.assertEqual(len(csv_files), 15 * 3)

            manifest = json.loads((custom_out / 'build-manifest.json').read_text())
            self.assertEqual(
                len([k for k in manifest['files'] if k.startswith('exports/')]),
                45,
            )

            series = data['charts'][0]['series'][0]
            self.assertEqual(
                set(series['historical_averages']),
                {'fourteen_days', 'two_months', 'all_time'},
            )
            self.assertEqual(
                set(series['relay_counts']),
                {'hour', 'day', 'fourteen_days', 'two_months', 'all_time'},
            )
            self.assertEqual(series['historical_averages']['fourteen_days']['days'], 14)
            self.assertEqual(series['historical_averages']['fourteen_days']['comparison']['n'], 14)
            self.assertEqual(
                series['historical_averages']['fourteen_days']['comparison']['start'],
                data['end'] - 15 * DAY,
            )
            self.assertEqual(
                series['historical_averages']['fourteen_days']['comparison']['end'],
                data['end'] - DAY,
            )

            for period in custom['periods']:
                with (custom_out / 'exports' / f"{data['charts'][0]['id']}-{period['export_suffix']}.csv").open() as handle:
                    rows = list(csv.DictReader(handle))
                series_values = [
                    float(r['value'])
                    for r in rows
                    if r['series'] == series['key'] and r['value']
                ]
                summary = series['historical_averages'][period['summary_key']]
                self.assertEqual(len(series_values), summary['days'])
                self.assertAlmostEqual(sum(series_values) / len(series_values), summary['value'], places=7)


if __name__ == '__main__':
    unittest.main()
