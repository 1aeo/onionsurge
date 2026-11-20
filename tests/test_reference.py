import copy
import hashlib
import json
import math
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import utc_timestamp as epoch
from reference_statistics import reference_window, quantile, describe_reference, retained_peak, frozen_reference
from windows import DAY, SAMPLED_HOUR_STEP, calendar_window


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def current(value, relays=1, valid=True):
    return {'value': value, 'relays': relays, 'coverage': 1, 'valid': valid}


def charts(t):
    past = t // DAY * DAY - 3 * DAY
    return [{'id': 'test', 'history_status': 'ready', 'kind': 'counter_hour_rate',
             'query': 'q', 'quality_query': 'cq', 'history_queries': {},
             'series': [{'key': 'a', 'history': {
                 'sampled_hour': [[past, 4, 2, 1, True]],
                 'daily': [[past, 4, 2, 1, True]]}}]}]


class ReferenceTests(unittest.TestCase):
    def test_calendar_months_and_daily_freeze(self):
        a = reference_window(epoch('2026-09-01T03:00:00'))
        self.assertEqual(a, reference_window(epoch('2026-09-01T23:00:00')))
        self.assertEqual(a['start'], epoch('2025-02-28T00:00:00'))
        self.assertEqual(a['end'], epoch('2026-08-31T00:00:00'))

    def test_quantiles_ties_and_equal_duration_normalization(self):
        self.assertEqual(quantile([0, 10], .1), 1)
        ref = describe_reference([[1, 0], [2, 0], [3, 10]], current(0))
        self.assertAlmostEqual(ref['current_percentile'], 100 / 3)
        self.assertEqual(describe_reference([[1, 1], [2, 3]], current(20, 10))['current_percentile'], 50)

    def test_summary_preserves_ties_input_order_and_numeric_representations(self):
        points = [[9, 1], [2, 1.0], [5, 1]]
        before = copy.deepcopy(points)
        result = describe_reference(points, current(1))
        self.assertEqual(points, before)
        self.assertEqual((result['start'], result['end'], result['n']), (2, 9, 3))
        self.assertEqual(result['current_percentile'], 50)
        self.assertTrue(result['all_equal'])
        self.assertIs(type(result['max_per_relay']), int)
        for percentile in (10, 50, 90, 95, 99):
            self.assertEqual(result[f'p{percentile}_per_relay'], 1.0)
            self.assertIs(type(result[f'p{percentile}_per_relay']), float)
        zeros = describe_reference([[1, -0.0], [2, 0.0]], current(0))
        self.assertEqual(math.copysign(1, zeros['max_per_relay']), -1)
        self.assertTrue(zeros['flat_zero'])
        self.assertIsNone(zeros['current_percentile'])

    def test_zero_and_invalid_current_are_not_ranked(self):
        ref = describe_reference([[1, 0], [2, 0]], current(0))
        self.assertIsNone(ref['current_percentile'])
        self.assertTrue(ref['flat_zero'])
        self.assertEqual(describe_reference([[1, 0]], current(1))['current_percentile'], 100)
        self.assertIsNone(describe_reference([[1, 1]], current(100, valid=False))['current_percentile'])

    def test_finite_extreme_reference_means_do_not_overflow_or_erase_subnormals(self):
        for value in [1e308, float.fromhex('0x0.0000000000001p-1022')]:
            with self.subTest(value=value):
                result = describe_reference([[1, value], [2, value]], current(value))
                self.assertEqual(result['mean_per_relay'], value)
                self.assertEqual(result['current_percentile'], 50)
                self.assertFalse(result['reference_all_zero'])

    def test_unsupported_calendar_ranges_raise_validation_errors(self):
        for value in [10**20, 10**100, epoch('9999-12-31T00:00:00')]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'calendar range'):
                reference_window(value)
        for months, lag in [(True, 0), (18.0, 0), (18, False), (18, .5), (120000, 0)]:
            with self.subTest(months=months, lag=lag), self.assertRaises(ValueError):
                calendar_window(epoch('2026-11-18'), months, lag)

    def test_invalid_current_cannot_set_record(self):
        peak = retained_peak([[100, 1, 1, 1, True]], current(100, valid=False), 200,
                             [[200, 100, 1, 1, True]])
        self.assertFalse(peak['current_is_record'])
        self.assertEqual(peak['fleet_peak'], 1)

    def test_future_nonfinite_and_low_coverage_additions_cannot_set_peak(self):
        peak = retained_peak([[100, 1, 1, 1, True]], current(2), 200,
                             [[300, 100, 1, 1, True], [150, float('inf'), 1, 1, True],
                              [160, 99, 1, .1, True]])
        self.assertEqual(peak['fleet_peak'], 2)
        self.assertEqual(peak['fleet_peak_at'], 200)

    def test_unqualified_recent_overlap_removes_sampled_peak_before_final_hour(self):
        sampled = [[100, 7, 1, 1, True], [200, 1000, 1, 1, True]]
        recent = [[200, None, 100, .5, False], [300, 4, 1, 1, True]]
        peak = retained_peak(sampled, current(5), 400, recent)
        self.assertEqual(peak['fleet_peak'], 7)
        self.assertEqual(peak['fleet_peak_at'], 100)
        self.assertEqual(peak['observations'], 3)

    def test_fleet_and_normalized_records_are_distinct(self):
        peak = retained_peak([[100, 100, 100, 1, True]], current(20, 10), 200)
        self.assertFalse(peak['current_is_record'])
        self.assertTrue(peak['current_is_normalized_record'])
        self.assertEqual(peak['fleet_peak'], 100)
        self.assertEqual(peak['value_per_relay'], 2)

    def test_reference_percentile_is_not_all_time_record(self):
        self.assertEqual(describe_reference([[100, 1]], current(2))['current_percentile'], 100)
        peak = retained_peak([[100, 1, 1, 1, True], [150, 3, 1, 1, True]], current(2), 200)
        self.assertFalse(peak['current_is_record'])

    def test_identical_duplicates_do_not_reweight_and_conflicts_fail(self):
        t = epoch('2026-11-18T00:00:00')
        source = charts(t)
        rows = source[0]['series'][0]['history']['daily']
        rows.append(copy.deepcopy(rows[0]))
        source[0]['series'][0]['history']['sampled_hour'].append(
            copy.deepcopy(source[0]['series'][0]['history']['sampled_hour'][0]))
        with tempfile.TemporaryDirectory() as folder:
            payload, _, _, _ = frozen_reference(source, t, Path(folder), sha('input'))
            self.assertEqual(len(payload['distributions']['test']['a']['day']), 1)
            self.assertEqual(len(payload['distributions']['test']['a']['hour']), 1)
        rows[-1][1] += 1
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'Conflicting'):
                frozen_reference(source, t, Path(folder), sha('input'))

    def test_cache_freezes_then_rolls_or_explicitly_corrects(self):
        t = epoch('2026-11-18T00:00:00')
        source = charts(t)
        with tempfile.TemporaryDirectory() as folder:
            first, _, identity, _ = frozen_reference(source, t, Path(folder), sha('first'))
            source[0]['series'][0]['history']['daily'][0][1] = 100
            same, _, same_id, _ = frozen_reference(source, t + 3600, Path(folder), sha('new'))
            self.assertEqual(first, same)
            self.assertEqual(identity, same_id)
            corrected, _, corrected_id, _ = frozen_reference(source, t, Path(folder), sha('new'), refresh=True)
            self.assertNotEqual(identity, corrected_id)
            self.assertEqual(corrected['distributions']['test']['a']['day'][0][1], 50)
            _, _, next_id, _ = frozen_reference(source, t + DAY, Path(folder), sha('new'))
            self.assertNotEqual(corrected_id, next_id)

    def test_cache_validates_payload_even_without_python_assertions(self):
        t = epoch('2026-11-18T00:00:00')
        with tempfile.TemporaryDirectory() as folder:
            frozen_reference(charts(t), t, Path(folder), sha('source'))
            path = next(Path(folder).rglob('*.json'))
            payload = json.loads(path.read_text())
            payload['window']['end'] += DAY
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                frozen_reference(charts(t), t, Path(folder), sha('source'))

    def test_cache_requires_object_maps_and_array_periods(self):
        t = epoch('2026-11-18T00:00:00')
        mutations = [
            lambda p: None,
            lambda p: [],
            lambda p: dict(p, distributions=[]),
            lambda p: dict(p, distributions={'test': None}),
            lambda p: dict(p, distributions={'test': {'a': []}}),
            lambda p: dict(p, distributions={'test': {'a': {'hour': []}}}),
            lambda p: dict(p, distributions={'test': {'a': {'hour': [], 'day': {}}}}),
        ]
        with tempfile.TemporaryDirectory() as folder:
            frozen_reference(charts(t), t, Path(folder), sha('source'))
            path = next(Path(folder).rglob('*.json'))
            original = json.loads(path.read_text())
            for change in mutations:
                with self.subTest(change=change):
                    path.write_text(json.dumps(change(copy.deepcopy(original))))
                    with self.assertRaises(ValueError):
                        frozen_reference(charts(t), t, Path(folder), sha('source'))
            # Empty arrays are a valid reference, unlike empty objects.
            empty = copy.deepcopy(original)
            empty['distributions']['test']['a'] = {'hour': [], 'day': []}
            path.write_text(json.dumps(empty))
            payload, _, _, _ = frozen_reference(charts(t), t, Path(folder), sha('source'))
            self.assertEqual(payload['distributions']['test']['a'], {'hour': [], 'day': []})

    def test_cache_requires_original_generation_provenance(self):
        t = epoch('2026-11-18T00:00:00')
        with tempfile.TemporaryDirectory() as folder:
            first, _, _, _ = frozen_reference(charts(t), t, Path(folder), sha('first'))
            path = next(Path(folder).rglob('*.json'))
            for field, value in [('source_input_sha256', None), ('source_input_sha256', []),
                                 ('source_input_sha256', 'not-a-hash'), ('source_snapshot_end', None),
                                 ('source_snapshot_end', True), ('source_snapshot_end', 'yesterday'),
                                 ('source_snapshot_end', 10**30), ('source_snapshot_end', 10**400)]:
                with self.subTest(field=field, value=value):
                    malformed = copy.deepcopy(first)
                    if value is None:
                        del malformed[field]
                    else:
                        malformed[field] = value
                    path.write_text(json.dumps(malformed))
                    with self.assertRaisesRegex(ValueError, 'provenance'):
                        frozen_reference(charts(t), t+3600, Path(folder), sha('changed'))
            path.write_text(json.dumps(first))
            same, _, _, _ = frozen_reference(charts(t), t+3600, Path(folder), sha('changed'))
            self.assertEqual(same['source_input_sha256'], sha('first'))
            self.assertEqual(same['source_snapshot_end'], t)

    def test_cache_cannot_forward_unrecognized_fields_to_public_reference(self):
        t = epoch('2026-11-18T00:00:00')
        with tempfile.TemporaryDirectory() as folder:
            frozen_reference(charts(t), t, folder, sha('source'))
            path = next(Path(folder).rglob('*.json'))
            payload = json.loads(path.read_text())
            payload['private_note'] = {'token': 'PRIVATE_TEST_MARKER'}
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'public payload fields'):
                frozen_reference(charts(t), t, folder, sha('source'))

    def test_cache_and_new_references_keep_the_declared_utc_midnight_grid(self):
        t = epoch('2026-11-18T00:00:00')
        source = charts(t)
        extra_h = copy.deepcopy(source[0]['series'][0]['history']['sampled_hour'][0])
        extra_h[0] += SAMPLED_HOUR_STEP
        source[0]['series'][0]['history']['sampled_hour'].append(extra_h)
        extra = copy.deepcopy(source[0]['series'][0]['history']['daily'][0])
        extra[0] += 3600
        source[0]['series'][0]['history']['daily'].append(extra)
        with tempfile.TemporaryDirectory() as folder:
            payload, _, _, _ = frozen_reference(source, t, folder, sha('source'))
            self.assertEqual(len(payload['distributions']['test']['a']['day']), 1)
            self.assertEqual(len(payload['distributions']['test']['a']['hour']), 2)
            path = next(Path(folder).rglob('*.json'))
            payload['distributions']['test']['a']['day'].append([extra[0], 1000])
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'Invalid reference cache observation'):
                frozen_reference(source, t, folder, sha('source'))
            payload['distributions']['test']['a']['day'].pop()
            payload['distributions']['test']['a']['hour'].append([extra[0], 1000])
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'Invalid reference cache observation'):
                frozen_reference(source, t, folder, sha('source'))

    def test_partial_inputs_and_new_series(self):
        t = epoch('2026-11-18T00:00:00')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                frozen_reference([{'history_status': 'partial'}], t, Path(folder), sha('source'))
            source = charts(t)
            _, _, first, _ = frozen_reference(source, t, Path(folder), sha('a'))
            series = copy.deepcopy(source[0]['series'][0]); series['key'] = 'b'
            source[0]['series'].append(series)
            _, _, second, _ = frozen_reference(source, t, Path(folder), sha('b'))
            self.assertNotEqual(first, second)

    def test_concurrent_first_writers_share_one_frozen_generation(self):
        t = epoch('2026-11-18T00:00:00')
        with tempfile.TemporaryDirectory() as folder:
            def run(i):
                return frozen_reference(charts(t), t, Path(folder), sha(i))[2]
            with ThreadPoolExecutor(max_workers=4) as workers:
                identities = list(workers.map(run, range(8)))
            self.assertEqual(len(set(identities)), 1)
            self.assertFalse(list(Path(folder).rglob('*.tmp')))

    def test_subnormal_per_relay_current_is_distinct_from_flat_zero(self):
        subnormal = float.fromhex('0x0.0000000000001p-1022')
        ref = describe_reference([[1, 0.0], [2, 0.0]], current(subnormal, relays=2))
        self.assertFalse(ref['flat_zero'])
        self.assertEqual(ref['current_percentile'], 100)
        sub_ref = describe_reference([[1, 0.0], [2, subnormal]], current(subnormal, relays=2))
        self.assertEqual(sub_ref['current_percentile'], 50)
        equal_sub_ref = describe_reference([[1, 0.0], [2, subnormal]], current(subnormal, relays=1))
        self.assertEqual(equal_sub_ref['current_percentile'], 75)
        identical_sub = describe_reference([[1, (4 * subnormal) / 3]], current(4 * subnormal, relays=3))
        self.assertEqual(identical_sub['current_percentile'], 50)
        peak = retained_peak([[100, 0.0, 2, 1, True]], current(subnormal, relays=2), 200)
        self.assertTrue(peak['current_is_record'])
        self.assertTrue(peak['current_is_normalized_record'])
        self.assertEqual(peak['at'], 200)


if __name__ == '__main__':
    unittest.main()
