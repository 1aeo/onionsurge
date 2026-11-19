import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import clone_snapshot, load_source, strict_ranges, utc_timestamp
import build
from historical_statistics import historical_averages_and_windows
from population_statistics import METHOD, ROLES, relay_counts
from validation import validate_snapshot
from windows import DAY

SOURCE = load_source()
END = utc_timestamp('2026-11-18')


def role_row(end, n, classified=None):
    classified = n if classified is None else classified
    # The flagged roles overlap; middle-only cannot overlap any of them.
    middle = classified // 4
    return [end, n, classified, classified // 3, middle, 0,
            classified - middle, classified - middle]


def coarsened_role_row(end, n, classified=None):
    classified = n if classified is None else classified
    middle = ((classified // 4) // 8) * 8
    avail = classified - middle
    guard = ((avail // 2) // 8) * 8
    return [end, n, classified, guard, middle, 0, avail, avail]


def metadata(series, end):
    relays = series['current']['day']['relays']
    return {'method': METHOD, 'queries': {'day': 'sum(example_day)'},
            'day': coarsened_role_row(end, relays) if relays is not None else None,
            'daily': [coarsened_role_row(row[0], row[2]) for row in series['history']['daily'] if row[2] is not None]}


class PopulationSummaryTests(unittest.TestCase):
    def calculate(self, rows, population=None, current=None, time_ranges=None):
        series = {'current': current or {'day': {'relays': 20}}}
        if population is not None:
            series['relay_population'] = population
        averages, windows = historical_averages_and_windows(rows, END, time_ranges=time_ranges)
        return averages, relay_counts(series, END, averages, windows)

    @staticmethod
    def strict_ranges():
        return strict_ranges()

    def test_exact_qualifying_days_equal_weight_and_nonadditive_roles(self):
        rows = [[END - 2*DAY, 0, 10, 1, True], [END - DAY, 30, 300, 1, True],
                [END, 999, 300, .1, False], [END + DAY, 1000, 300, 1, True]]
        observations = [role_row(row[0], row[2]) for row in rows]
        averages, counts = self.calculate(rows, {'daily': observations})
        result = counts['all_time']
        self.assertEqual(averages['all_time']['value'], 15)
        self.assertEqual(result['total'], 155)
        self.assertEqual(result['guard'], (3 + 100) / 2)
        self.assertEqual(result['measured_windows'], 2)
        self.assertEqual(result['classified_windows'], 2)
        self.assertEqual(result['relay_windows'], 310)
        self.assertEqual(result['classified_relay_windows'], 310)
        self.assertEqual(result['coverage'], 1)
        self.assertIsNone(result['role_bounds'])
        self.assertGreater(sum(result[role] for role in ROLES), result['total'])

    def test_missing_partial_and_mismatched_evidence_never_shorten_the_day_set(self):
        rows = [[END-DAY, 1, 10, 1, True], [END, 2, 300, 1, True]]
        for later, classified, complete in [(None, 10, 1), (role_row(END, 300, 200), 210, 1),
                                            (role_row(END, 299), 10, 1)]:
            records = [role_row(END-DAY, 10)] + ([] if later is None else [later])
            _, counts = self.calculate(rows, {'daily': records})
            result = counts['all_time']
            with self.subTest(later=later):
                self.assertEqual(result['total'], 155)
                self.assertTrue(all(result[role] is None for role in ROLES))
                self.assertEqual(result['measured_windows'], 2)
                self.assertEqual(result['classified_windows'], complete)
                self.assertEqual(result['relay_windows'], 310)
                self.assertEqual(result['classified_relay_windows'], classified)
                self.assertAlmostEqual(result['coverage'], classified / 310)
                expected_guard = [69/2, (69+100)/2] if classified == 210 else [3/2, (3+300)/2]
                self.assertEqual(result['role_bounds']['guard'], expected_guard)
                self.assertEqual(set(result['role_bounds']), set(ROLES))
                self.assertEqual(counts['six_months']['role_bounds'], result['role_bounds'])
                _, counts_strict = self.calculate(rows, {'daily': records}, time_ranges=self.strict_ranges())
                self.assertIsNone(counts_strict['six_months']['role_bounds'])

    def test_six_month_population_requires_the_headline_coverage_threshold(self):
        rows = [[END-i*DAY, 1, 100, 1, True] for i in range(184)]
        strict = self.strict_ranges()
        for length, ready in [(24, False), (165, False), (166, True), (184, True)]:
            points = rows[:length]
            averages, counts = self.calculate(points, {'daily': [role_row(row[0], 100) for row in points]})
            _, counts_strict = self.calculate(points, {'daily': [role_row(row[0], 100) for row in points]}, time_ranges=strict)
            result = counts['six_months']
            strict_result = counts_strict['six_months']
            with self.subTest(days=length):
                self.assertEqual(result['total'], 100)
                self.assertEqual(result['guard'], 33)
                self.assertEqual(strict_result['total'], 100 if ready else None)
                self.assertEqual(strict_result['guard'], 33 if ready else None)
                self.assertEqual(result['measured_windows'], averages['six_months']['days'])
                self.assertEqual(result['relay_windows'], length * 100)
                self.assertEqual(result['coverage'], 1)
                self.assertEqual(counts['all_time']['total'], 100)

    def test_old_inputs_derive_totals_but_never_infer_flags(self):
        _, counts = self.calculate([[END, 1, 100, 1, True]])
        _, counts_strict = self.calculate([[END, 1, 100, 1, True]], time_ranges=self.strict_ranges())
        self.assertEqual(
            set(counts),
            {'day', 'seven_days', 'one_month', 'three_months', 'six_months', 'twelve_months', 'all_time'},
        )
        for period, total in [
            ('day', 20), ('seven_days', 100), ('one_month', 100), ('three_months', 100),
            ('six_months', 100), ('twelve_months', 100), ('all_time', 100),
        ]:
            result = counts[period]
            self.assertEqual(result['total'], total)
            self.assertTrue(all(result[role] is None for role in ROLES))
            self.assertEqual(result['classified_windows'], 0)
            self.assertEqual(result['classified_relay_windows'], 0)
            self.assertEqual(result['coverage'], 0)
            self.assertIsNone(result['role_bounds'])
        for period in ('seven_days', 'one_month', 'three_months', 'six_months', 'twelve_months'):
            self.assertIsNone(counts_strict[period]['total'])

    def test_custom_time_ranges_and_key_guards(self):
        custom = {
            'version': 1,
            'default_range': '14d',
            'legacy_aliases': {'24h': '14d'},
            'card_slots': {'fallback_dynamic': '14d', 'fixed': ['2mo', 'all']},
            'periods': [
                {
                    'id': '14d', 'summary_key': 'fourteen_days', 'export_suffix': '14d',
                    'label': '14 days', 'average_label': 'Last 14 days average', 'plot_label': 'Last 14 days',
                    'window': {'unit': 'days', 'count': 14, 'comparison_lag_days': 1},
                    'minimum_coverage': 0.9, 'minimum_comparison_windows': 10,
                },
                {
                    'id': '2mo', 'summary_key': 'two_months', 'export_suffix': '2mo',
                    'label': '2 months', 'average_label': 'Last 2 months average', 'plot_label': 'Last 2 months',
                    'window': {'unit': 'months', 'count': 2, 'comparison_lag_days': 0},
                    'minimum_coverage': 0.9, 'minimum_comparison_windows': 20,
                },
                {
                    'id': 'all', 'summary_key': 'all_time', 'export_suffix': 'all-time',
                    'label': 'All time', 'average_label': 'All-time average', 'plot_label': 'All time',
                    'window': {'unit': 'all_time', 'count': None, 'comparison_lag_days': 0},
                    'minimum_coverage': 0, 'minimum_comparison_windows': 30,
                },
            ],
        }
        rows = [[END - i * DAY, 10, 100, 1, True] for i in range(14)]
        averages, windows = historical_averages_and_windows(rows, END, time_ranges=custom)
        series = {'current': {'day': {'relays': 100}}, 'relay_population': {'daily': [role_row(r[0], 100) for r in rows]}}
        counts = relay_counts(series, END, averages, windows)
        self.assertEqual(set(counts), {'day', 'fourteen_days', 'two_months', 'all_time'})
        self.assertEqual(counts['fourteen_days']['total'], 100)
        self.assertIsNone(counts['two_months']['total'])
        with self.assertRaisesRegex(ValueError, 'summary keys'):
            relay_counts(series, END, {'day': {'status': 'ready'}}, {'day': []})
        with self.assertRaisesRegex(ValueError, 'summary keys'):
            relay_counts(series, END, averages, {'all_time': windows['all_time']})

    def test_current_zero_is_only_classified_with_explicit_empty_set_evidence(self):
        for total, evidence in [(0, None), (0, role_row(END, 0))]:
            current = {'day': {'relays': total, 'valid': False}}
            _, counts = self.calculate([], {'day': evidence}, current)
            day = counts['day']
            self.assertEqual(day['total'], 0)
            self.assertEqual(day['guard'], 0 if evidence is not None else None)
            self.assertEqual(day['coverage'], 1 if evidence is not None else None)
            self.assertEqual(day['measured_windows'], 1)
            self.assertIsNone(day['role_bounds'])
            self.assertIsNone(counts['all_time']['total'])
            self.assertIsNone(counts['all_time']['coverage'])
        current_missing = {'day': {'relays': None, 'valid': False}}
        _, missing_counts = self.calculate([], {'day': None}, current_missing)
        self.assertIsNone(missing_counts['day']['total'])
        self.assertIsNone(missing_counts['day']['coverage'])
        self.assertEqual(missing_counts['day']['measured_windows'], 0)

    def test_current_role_counts_use_their_own_population_even_when_rate_invalid(self):
        current = {'day': {'relays': 20, 'valid': False}}
        _, counts = self.calculate([], {'day': role_row(END, 20)}, current)
        self.assertEqual(counts['day']['total'], 20)
        self.assertEqual(counts['day']['guard'], 6)

    def test_complete_recorded_zero_flags_do_not_imply_middle_only(self):
        _, counts = self.calculate([[END, 1, 10, 1, True]],
                                  {'day': [END, 10, 10, 0, 0, 0, 0, 0],
                                   'daily': [[END, 10, 10, 0, 0, 0, 0, 0]]},
                                  current={'day': {'relays': 10}})
        for period in ('day', 'all_time'):
            self.assertEqual(counts[period]['coverage'], 1)
            self.assertEqual(counts[period]['total'], 10)
            self.assertTrue(all(counts[period][role] == 0 for role in ROLES))
            self.assertIsNone(counts[period]['role_bounds'])

    def test_partial_current_bounds_cover_all_unknown_contributors(self):
        _, counts = self.calculate([], {'day': [END, 10, 5, 2, 1, 0, 4, 4]},
                                  current={'day': {'relays': 10}})
        result = counts['day']
        self.assertEqual(result['total'], 10)
        self.assertTrue(all(result[role] is None for role in ROLES))
        self.assertEqual(result['role_bounds'], {'guard': [2, 7], 'middle_only': [1, 6],
                                               'exit': [0, 5], 'hsdir': [4, 9], 'v2dir': [4, 9]})
        self.assertEqual(result['coverage'], .5)

    def test_daily_bounds_keep_fractional_values_and_include_missing_whole_days(self):
        rows = [[END-2*DAY, 1, 7, 1, True], [END-DAY, 1, 11, 1, True], [END, 1, 13, 1, True]]
        _, counts = self.calculate(rows, {'daily': [[END-2*DAY, 7, 5, 2, 1, 0, 4, 4],
                                                   [END, 13, 13, 3, 4, 0, 9, 9]]})
        result = counts['all_time']
        self.assertEqual(result['measured_windows'], 3)
        self.assertEqual(result['total'], 31/3)
        self.assertEqual(result['classified_relay_windows'], 18)
        self.assertEqual(result['role_bounds']['guard'], [5/3, 18/3])
        self.assertEqual(result['role_bounds']['exit'], [0, 13/3])

    def test_aggregate_counts_cannot_overflow_the_public_numeric_range(self):
        for counts in [(1e308, 1e308), (2**53-1, 1)]:
            rows = [[END-DAY, 1, counts[0], 1, True], [END, 1, counts[1], 1, True]]
            with self.subTest(counts=counts), self.assertRaisesRegex(ValueError, 'numeric range'):
                self.calculate(rows)

    def test_direct_relay_counts_reject_bools_negatives_nonintegers_and_invalid_rows(self):
        for bad_relays in [True, -1, 1.5, float('inf')]:
            with self.subTest(bad_relays=bad_relays), self.assertRaises(ValueError):
                self.calculate([], current={'day': {'relays': bad_relays}})
        bad_record = [END, 10, 11, 0, 0, 0, 0, 0]
        with self.assertRaisesRegex(ValueError, 'classification'):
            self.calculate([], {'day': bad_record}, current={'day': {'relays': 10}})


class PopulationInputTests(unittest.TestCase):
    def fixture(self):
        data = clone_snapshot(SOURCE)
        series = data['charts'][0]['series'][0]
        series['relay_population'] = metadata(series, data['end'])
        return data, series['relay_population']

    def test_optional_metadata_and_explicit_missing_rows_are_valid(self):
        validate_snapshot(clone_snapshot(SOURCE))
        data, population = self.fixture()
        validate_snapshot(data)
        population.update(day=None, daily=[])
        validate_snapshot(data)

    def test_closed_maps_method_queries_and_scalar_counts(self):
        mutations = [lambda p: p.update(extra='no'), lambda p: p.pop('day'),
                     lambda p: p.update(method='current-flags'),
                     lambda p: p['queries'].update(extra='no'),
                     lambda p: p['queries'].update(day=' '),
                     lambda p: p['queries'].update(day={}),
                     lambda p: p.update(daily=None)]
        for mutate in mutations:
            data, population = self.fixture(); mutate(population)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                validate_snapshot(data)
        for value in [None, True, -1, .5, 1, '2', float('inf'), 2**53, 10**400]:
            for index in range(8):
                data, population = self.fixture(); population['day'][index] = value
                with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                    validate_snapshot(data)

    def test_counts_match_the_measurement_and_classification_bounds(self):
        for change in ['end', 'total', 'classified', 'role', 'middle_overlap']:
            data, population = self.fixture(); row = population['day']
            if change == 'end': row[0] -= DAY
            if change == 'total': row[1] += 8
            if change == 'classified': row[2] = row[1]+8
            if change == 'role': row[3] = row[2]+8
            if change == 'middle_overlap': row[4] = row[2]
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_snapshot(data)

    def test_daily_rows_require_existing_sorted_unique_midnight_windows(self):
        for change in ['duplicate', 'order', 'unaligned', 'unmeasured', 'mismatch', 'future', 'shape']:
            data, population = self.fixture(); rows = population['daily']
            if change == 'duplicate': rows.insert(0, rows[0].copy())
            if change == 'order': rows.reverse()
            if change == 'unaligned': rows[0][0] += 3600
            if change == 'unmeasured': rows[0][0] -= DAY
            if change == 'mismatch': rows[0][1] += 8
            if change == 'future': rows[-1][0] = data['end'] + DAY
            if change == 'shape': rows[0].pop()
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_snapshot(data)

    def test_population_maps_reject_identity_fields(self):
        data, population = self.fixture(); population['instance'] = 'forbidden-fixture'
        with self.assertRaisesRegex(ValueError, 'identity'):
            validate_snapshot(data)

    def test_role_metadata_preserves_measurements_exports_and_frozen_reference(self):
        data, population = self.fixture()
        expected = clone_snapshot(population, share_rows=False)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before, before_reference = build.prepare(clone_snapshot(SOURCE), 'a'*64, root/'cache')
            after, after_reference = build.prepare(data, 'b'*64, root/'cache')
            self.assertEqual(before_reference, after_reference)
            self.assertEqual(before['reference_sha256'], after['reference_sha256'])
            self.assertEqual(after['charts'][0]['series'][0]['relay_population'], expected)
            self.assertRegex(after['provenance']['relay_population_method_sha256'], r'^[0-9a-f]{64}$')
            for chart_before, chart_after in zip(before['charts'], after['charts'], strict=True):
                for series_before, series_after in zip(chart_before['series'], chart_after['series'], strict=True):
                    for key in ('history', 'current', 'reference', 'peaks', 'historical_averages'):
                        self.assertEqual(series_before[key], series_after[key])
            build.write_outputs(before, before_reference, root/'before')
            build.write_outputs(after, after_reference, root/'after')
            for path in (root/'before'/'exports').glob('*.csv'):
                self.assertEqual(path.read_bytes(), (root/'after'/'exports'/path.name).read_bytes())


if __name__ == '__main__':
    unittest.main()
