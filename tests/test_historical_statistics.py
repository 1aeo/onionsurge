import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import custom_three_period_config, strict_ranges, utc_timestamp as timestamp
from historical_statistics import (historical_averages, historical_averages_and_windows,
                                   historical_comparison, period_comparison_rows, DAY)
from time_ranges import load_time_ranges


class HistoricalAverageChecks(unittest.TestCase):
    def test_current_day_comparison_normalizes_each_population_and_excludes_overlap(self):
        end = timestamp('2026-11-18')
        rows = [[end-3*DAY, 10, 10, 1, True], [end-2*DAY, 40, 20, 1, True],
                [end-DAY, 60, 20, 1, True], [end, 9999, 10, 1, True]]
        current = {'value': 20, 'relays': 10, 'coverage': 1, 'valid': True}
        for hour in (0, 3, 23):
            averages, windows = historical_averages_and_windows(rows, end+hour*3600)
            before = historical_averages(rows, end+hour*3600)
            result = historical_comparison(windows['all_time'], current, end-DAY, True)
            self.assertEqual(result, {'method': 'daily-per-relay-midrank-v1', 'cutoff': end-DAY,
                                     'n': 3, 'start': end-4*DAY, 'end': end-DAY,
                                     'current_percentile': 50, 'flat_zero': False})
            self.assertEqual(averages, before)
            self.assertEqual(averages['all_time']['value'], 2527.25)

    def test_comparison_ties_and_zero_states_match_existing_midrank_rules(self):
        end = timestamp('2026-11-18')
        cases = [([1, 2, 2, 3], 2, True, 50, False), ([2, 2], 2, True, 50, False),
                 ([0, 0], 0, True, None, True), ([0, 0], 1, True, 100, False),
                 ([1, 2], 0, True, 0, False), ([0, 0], 0, False, None, False),
                 ([], 0, True, None, False)]
        for values, value, available, rank, flat in cases:
            points = [[end-(len(values)-index)*DAY, v*10, 10, 1, True] for index, v in enumerate(values)]
            result = historical_comparison(points, {'value': value*20, 'relays': 20, 'valid': True, 'coverage': 1}, end-DAY, available)
            with self.subTest(values=values, current=value, available=available):
                self.assertEqual(result['current_percentile'], rank)
                self.assertEqual(result['flat_zero'], flat)
                self.assertEqual(result['n'], len(values))
                if not values:
                    self.assertIsNone(result['start']); self.assertIsNone(result['end'])

    def test_unavailable_parent_or_current_preserves_the_comparison_membership(self):
        end = timestamp('2026-11-18')
        points = [[end-DAY, 0, 10, 1, True]]
        valid = {'value': 0, 'relays': 10, 'valid': True, 'coverage': 1}
        for change, available in [({}, False), ({'valid': False}, True), ({'coverage': .89}, True),
                                  ({'relays': 0}, True), ({'value': None}, True)]:
            result = historical_comparison(points, dict(valid, **change), end-DAY, available)
            self.assertEqual(result['n'], 1)
            self.assertEqual(result['start'], end-2*DAY)
            self.assertEqual(result['end'], end-DAY)
            self.assertIsNone(result['current_percentile'])
            self.assertFalse(result['flat_zero'])

    def test_six_month_comparison_keeps_existing_start_and_only_removes_overlapping_day(self):
        start, end = timestamp('2026-05-18'), timestamp('2026-11-18')
        rows = [[start+i*DAY, i*100, 100, 1, True] for i in range(185)]
        averages, windows = historical_averages_and_windows(rows, end+3*3600)
        current = {'value': 18300, 'relays': 100, 'coverage': 1, 'valid': True}
        six = historical_comparison(windows['six_months'], current, end-DAY, averages['six_months']['status']=='ready')
        all_time = historical_comparison(windows['all_time'], current, end-DAY, True)
        self.assertEqual(six['n'], 183)
        self.assertEqual(six['start'], start)
        self.assertEqual(six['end'], end-DAY)
        self.assertEqual(six['current_percentile'], 100*(182+.5)/183)
        self.assertEqual(all_time['n'], 184)
        self.assertEqual(all_time['start'], start-DAY)
        self.assertEqual(averages['six_months']['days'], 184)
        self.assertEqual(averages['six_months']['value'], 9250)

    def test_means_keep_fleet_units_zeros_and_equal_day_weight(self):
        end = timestamp('2026-11-18')
        rows = [[end-2*DAY, 0, 10, 1, True], [end-DAY, 30, 300, 1, True],
                [end, 999, 300, .1, False], [end+DAY, 1000, 300, 1, True]]
        result = historical_averages(rows, end+3*3600)['all_time']
        self.assertEqual(result['value'], 15)
        self.assertEqual(result['days'], 2)
        self.assertEqual(result['expected_days'], 3)

    def test_six_month_window_uses_whole_days_and_deduplicates(self):
        start, end = timestamp('2026-05-18'), timestamp('2026-11-18')
        rows = [[start+i*DAY, i, 100, 1, True] for i in range(1, 185)]
        rows += [rows[0], [start, 9999, 100, 1, True], [end+3600, 9999, 100, 1, True]]
        result = historical_averages(rows, end+3*3600)['six_months']
        self.assertEqual(result['start'], start)
        self.assertEqual(result['end'], end)
        self.assertEqual(result['days'], 184)
        self.assertEqual(result['value'], 92.5)

    def test_short_record_and_coverage_threshold(self):
        end = timestamp('2026-11-18')
        rows = [[end-i*DAY, 0, 100, 1, True] for i in range(184)]
        short = historical_averages(rows[:24], end)
        self.assertEqual(short['all_time']['value'], 0)
        self.assertEqual(short['six_months']['status'], 'ready')
        self.assertEqual(short['six_months']['value'], 0)
        self.assertEqual(short['six_months']['days'], 24)
        self.assertEqual(short['six_months']['expected_days'], 184)
        self.assertEqual(historical_averages(rows[:165], end)['six_months']['value'], 0)

        empty = historical_averages([], end)
        self.assertEqual(empty['six_months']['status'], 'insufficient_data')
        self.assertIsNone(empty['six_months']['value'])
        self.assertEqual(empty['six_months']['days'], 0)

        strict = strict_ranges()
        short_strict = historical_averages(rows[:24], end, time_ranges=strict)
        self.assertEqual(short_strict['all_time']['value'], 0)
        self.assertEqual(short_strict['six_months']['status'], 'insufficient_data')
        self.assertIsNone(short_strict['six_months']['value'])
        self.assertIsNone(historical_averages(rows[:165], end, time_ranges=strict)['six_months']['value'])
        self.assertEqual(historical_averages(rows[:166], end, time_ranges=strict)['six_months']['value'], 0)

    def test_boolean_and_numeric_duplicate_flags_conflict_in_both_orders(self):
        end = timestamp('2026-11-18')
        qualified = [end, 10, 100, 1, True]
        numeric_flag = [end, 10, 100, 1, 1]
        for rows in ([qualified, numeric_flag], [numeric_flag, qualified]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'Conflicting'):
                historical_averages(rows, end)

    def test_calendar_month_clamp_and_empty_data(self):
        result = historical_averages([], timestamp('2024-08-31'))
        self.assertEqual(result['six_months']['start'], timestamp('2024-02-29'))
        self.assertIsNone(result['all_time']['value'])
        self.assertEqual(result['all_time']['days'], 0)

    def test_seven_days_and_lagged_days_require_coverage_and_nonoverlapping_comparison(self):
        end = timestamp('2026-11-18')
        rows = [[end - i * DAY, 100 + i, 100, 1, True] for i in range(32)]
        partial_avg, _ = historical_averages_and_windows(rows[:6], end)
        self.assertEqual(partial_avg['seven_days']['status'], 'ready')
        self.assertAlmostEqual(partial_avg['seven_days']['value'], sum(r[1] for r in rows[:6]) / 6)
        self.assertEqual(partial_avg['seven_days']['days'], 6)
        self.assertEqual(partial_avg['seven_days']['expected_days'], 7)

        strict = load_time_ranges()
        for period in strict['periods']:
            if period['window']['unit'] != 'all_time':
                period['minimum_coverage'] = 0.9
        below_avg, _ = historical_averages_and_windows(rows[:6], end, time_ranges=strict)
        self.assertEqual(below_avg['seven_days']['status'], 'insufficient_data')
        self.assertIsNone(below_avg['seven_days']['value'])
        self.assertEqual(below_avg['seven_days']['days'], 6)
        self.assertEqual(below_avg['seven_days']['expected_days'], 7)

        unqualified = [[end, 100, 100, 0.1, False]]
        empty_avg, _ = historical_averages_and_windows(unqualified, end)
        self.assertEqual(empty_avg['seven_days']['status'], 'insufficient_data')
        self.assertIsNone(empty_avg['seven_days']['value'])
        self.assertEqual(empty_avg['seven_days']['days'], 0)

        ready_rows = rows[:7]
        averages, windows = historical_averages_and_windows(ready_rows, end)
        self.assertEqual(averages['seven_days']['status'], 'ready')
        self.assertEqual(averages['seven_days']['days'], 7)
        self.assertEqual(averages['seven_days']['expected_days'], 7)
        self.assertAlmostEqual(averages['seven_days']['value'], sum(r[1] for r in ready_rows) / 7)
        self.assertEqual(len(windows['seven_days']), 7)

        seven_def = next(p for p in load_time_ranges()['periods'] if p['id'] == '7d')
        prior7 = period_comparison_rows(rows, end, seven_def)
        self.assertEqual(len(prior7), 7)
        self.assertEqual(prior7[0][0], end - 7 * DAY)
        self.assertEqual(prior7[-1][0], end - DAY)

        thirty_def = {'window': {'unit': 'days', 'count': 30, 'comparison_lag_days': 1}}
        prior30 = period_comparison_rows(rows, end, thirty_def)
        self.assertEqual(len(prior30), 30)
        self.assertEqual(prior30[-1][0], end - DAY)
        self.assertEqual(prior30[0][0], end - 30 * DAY)
        comp = historical_comparison(prior30, {'value': 100, 'relays': 100, 'coverage': 1, 'valid': True}, end - DAY, True)
        self.assertEqual(comp['n'], 30)
        self.assertEqual(comp['cutoff'], end - DAY)
        self.assertIsNotNone(comp['current_percentile'])

    def test_all_default_periods_and_custom_time_ranges(self):
        end = timestamp('2026-11-18')
        rows = [[end - i * DAY, 10 + i, 100, 1, True] for i in range(400)]
        averages, windows = historical_averages_and_windows(rows, end)
        self.assertEqual(
            list(averages),
            ['seven_days', 'one_month', 'three_months', 'six_months', 'twelve_months', 'all_time'],
        )
        self.assertEqual(averages['one_month']['expected_days'], 31)
        self.assertEqual(averages['three_months']['expected_days'], 92)
        self.assertEqual(averages['six_months']['expected_days'], 184)
        self.assertEqual(averages['twelve_months']['expected_days'], 365)
        self.assertEqual(averages['all_time']['expected_days'], 400)
        self.assertEqual(len(windows['twelve_months']), 365)

        # Custom 3-period config (14d / 2mo / all) works without modifying code
        custom = custom_three_period_config()
        custom['periods'][0]['minimum_coverage'] = 0.9
        custom['periods'][1].update(minimum_coverage=0.9, window={'unit': 'months', 'count': 2, 'comparison_lag_days': 1})
        c_avg, c_win = historical_averages_and_windows(rows, end, time_ranges=custom)
        self.assertEqual(list(c_avg), ['fourteen_days', 'two_months', 'all_time'])
        self.assertEqual(c_avg, historical_averages(rows, end, time_ranges=custom))
        self.assertEqual(len(c_win['fourteen_days']), 14)
        self.assertEqual(c_avg['two_months']['start'], timestamp('2026-09-18'))
        lagged_two_mo = period_comparison_rows(rows, end, custom['periods'][1])
        self.assertEqual(lagged_two_mo[0][0], timestamp('2026-09-18'))
        self.assertEqual(lagged_two_mo[-1][0], end - DAY)
        all_comp = period_comparison_rows(rows, end, custom['periods'][2])
        self.assertEqual(len(all_comp), 399)

    def test_finite_extreme_daily_means_are_representable(self):
        end = timestamp('2026-11-18')
        for value in [1e308, float.fromhex('0x0.0000000000001p-1022')]:
            rows = [[end-DAY, value, 1, 1, True], [end, value, 1, 1, True]]
            with self.subTest(value=value):
                self.assertEqual(historical_averages(rows, end)['all_time']['value'], value)
        subnormal = float.fromhex('0x0.0000000000001p-1022')
        zero_rows = [[end - (30 - i) * DAY, 0.0, 8, 1.0, True] for i in range(30)]
        sub_comp = historical_comparison(zero_rows, {'value': subnormal, 'relays': 8, 'coverage': 1.0, 'valid': True}, end - DAY, True)
        self.assertFalse(sub_comp['flat_zero'])
        self.assertEqual(sub_comp['current_percentile'], 100.0)
        zero_comp = historical_comparison(zero_rows, {'value': 0.0, 'relays': 8, 'coverage': 1.0, 'valid': True}, end - DAY, True)
        self.assertTrue(zero_comp['flat_zero'])
        self.assertIsNone(zero_comp['current_percentile'])
        mixed = [[end - DAY, 8, 24, 1.0, True]]
        mixed_comp = historical_comparison(mixed, {'value': 8, 'relays': 24.0, 'coverage': 1.0, 'valid': True}, end - DAY, True)
        self.assertEqual(mixed_comp['current_percentile'], 50.0)
        rev_mixed = [[end - DAY, 8, 24.0, 1.0, True]]
        rev_comp = historical_comparison(rev_mixed, {'value': 8, 'relays': 24, 'coverage': 1.0, 'valid': True}, end - DAY, True)
        self.assertEqual(rev_comp['current_percentile'], 50.0)
        with self.assertRaisesRegex(ValueError, 'calendar range'):
            historical_averages([], 10**100)

    def test_historical_comparison_validates_points_rows(self):
        end = timestamp('2026-11-18')
        current = {'value': 8, 'relays': 8, 'coverage': 1.0, 'valid': True}
        self.assertEqual(historical_comparison([[end - DAY, 0, 8]], current, end - DAY, True)['n'], 1)
        for bad_row in (
            'not-a-row',
            [end - DAY, 0],
            [-DAY, 0, 8],
            [-0.0, 0, 8],
            [end - 0.5, 0, 8],
            [10**400, 0, 8],
            [True, 0, 8],
            [end + DAY, -0.0, 8],
            [end - DAY, -0.0, 8],
            [end - DAY, -1, 8],
            [end - DAY, float('nan'), 8],
            [end - DAY, float('inf'), 8],
            [end - DAY, True, 8],
            [end - DAY, 1.0, 0],
            [end - DAY, 1.0, -8],
            [end - DAY, 1.0, 8.5],
            [end - DAY, 1.0, -0.0],
            [end - DAY, 1.0, False],
            [end - DAY, 1.0, 10**400],
        ):
            with self.subTest(bad_row=bad_row), self.assertRaisesRegex(ValueError, 'Invalid comparison point'):
                historical_comparison([bad_row], current, end - DAY, True)

    def test_historical_averages_and_windows_sorts_out_of_order_daily_rows(self):
        end = timestamp('2026-11-18')
        ordered = [[end - i * DAY, 10 + i, 100, 1.0, True] for i in range(9, -1, -1)]
        shuffled = list(reversed(ordered))
        expected_avg, expected_win = historical_averages_and_windows([], end, daily_rows=ordered)
        actual_avg, actual_win = historical_averages_and_windows([], end, daily_rows=shuffled)
        self.assertEqual(actual_avg, expected_avg)
        self.assertEqual(actual_win, expected_win)


if __name__ == '__main__':
    unittest.main()
