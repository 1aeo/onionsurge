from decimal import Decimal
import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import clone_snapshot, load_source, set_path
import windows
from validation import _promql_ranges, validate_snapshot
from coarsening import (
    _coarsen_count_string, coarsen_cohort_mapping, coarsen_count, coarsen_partition,
    coarsen_population_row, coarsen_snapshot, coarsen_value, is_coarsened_count,
    is_coarsened_value, sigfigs3,
)
from io_helpers import parse_json, javascript_data, json_text

SOURCE = load_source()
DAY = 86400


class InputValidationTests(unittest.TestCase):
    def fixture(self, *, share_rows=True):
        return clone_snapshot(SOURCE, share_rows=share_rows)

    def auxiliary_day(self):
        data = self.fixture()
        chart = next(c for c in data['charts'] if any(s['key'] not in c['display_keys'] for s in c['series']))
        auxiliary = next(s for s in chart['series'] if s['key'] not in chart['display_keys'])
        # The synthetic coverage cases exercise the metadata-absent input form.
        auxiliary.pop('relay_population', None)
        expected = int(chart['relays'])
        reporting = min(expected, ((math.ceil(expected * .9) + 7) // 8) * 8)
        auxiliary['current']['day'] = {'value': 10, 'relays': reporting, 'expected_relays': expected}
        return data, chart, auxiliary

    def test_reviewed_aggregate_input_is_valid(self):
        validate_snapshot(self.fixture())

    def test_zero_observed_cohorts_allow_fresh_gaps_without_erasing_history(self):
        from build import prepare
        data = self.fixture()
        original = clone_snapshot(data['charts'])
        data.update(fleet=0, custom=0, defense=0, flags={})
        versions = data['versions']
        versions.update(native_total=0, runtime_verified_total=0, runtime_unverified_relays=0,
                        runtime_binary_hash_verified_total=0, verified_runtime=[])
        versions.pop('runtime_resolution', None)
        for counts in versions['metric_cohorts'].values():
            counts.update(dict.fromkeys(counts, 0))
        for probe in data['snapshot_flags'].values():
            probe['response']['data']['result'] = []
        for chart in data['charts']:
            prior_relays = max(8, chart['relays'])
            chart['relays'] = 0
            for series in chart['series']:
                # All current populations change in this legacy outage fixture.
                series.pop('relay_population', None)
                rows = series['history']['daily']
                if rows and rows[-1][0] == data['end']:
                    rows[-1] = [data['end'], None, 0, 0, False]
                day = series['current']['day']
                day.update(value=None, relays=0, coverage=0, valid=False)
                day.setdefault('expected_relays', prior_relays)
        with tempfile.TemporaryDirectory() as folder:
            built, _ = prepare(data, 'a'*64, Path(folder))
        for chart, prior in zip(built['charts'], original):
            self.assertEqual(chart['relays'], 0)
            for series, old in zip(chart['series'], prior['series']):
                old_rows = old['history']['daily']
                prior_prefix = old_rows[:-1] if old_rows and old_rows[-1][0] == data['end'] else old_rows
                cur_prefix = series['history']['daily'][:-1] if old_rows and old_rows[-1][0] == data['end'] else series['history']['daily']
                self.assertEqual(cur_prefix, prior_prefix)
                self.assertEqual(series['current']['day']['display'], 'Unavailable')

    def test_nonfinite_and_duplicate_json_keys_rejected(self):
        for encoded in ['{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}']:
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                parse_json(encoded)
        with self.assertRaises(ValueError):
            json_text({'x': float('nan')})

    def test_javascript_json_cannot_close_its_script_element(self):
        value = {'name': '</script><script>alert(1)</script>', 'text': 'a & b'}
        encoded = javascript_data(value)
        self.assertNotIn('<', encoded)
        self.assertEqual(json.loads(encoded.removeprefix('window.ONIONSURGE_DATA = ').removesuffix(';\n')), value)

    def test_path_and_attribute_injection_ids_rejected(self):
        for name in ['../../outside', 'a" onclick="x', '<script>', '=FORMULA()']:
            data = self.fixture(); data['charts'][0]['id'] = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'ID'):
                validate_snapshot(data)

    def test_raw_relay_identity_fields_rejected(self):
        data = self.fixture(); data['extra'] = {'instance': 'private-relay'}
        with self.assertRaisesRegex(ValueError, 'identity'):
            validate_snapshot(data)

    def test_unknown_private_metadata_is_not_forwarded(self):
        data = self.fixture()
        data['internal_note'] = {'server_name': 'PRIVATE_HOST_MARKER', 'api_key': 'PRIVATE_TOKEN_MARKER'}
        with self.assertRaisesRegex(ValueError, 'public snapshot shape'):
            validate_snapshot(data)
        data = self.fixture(); data['versions']['internal_note'] = {'server_name': 'private'}
        with self.assertRaisesRegex(ValueError, 'runtime metadata'):
            validate_snapshot(data)

    def test_public_metadata_maps_reject_unknown_nested_notes(self):
        paths = [['product'], ['history_methods'], ['flags'], ['dircache_cohort'], ['snapshot_flags'],
                 ['snapshot_flags', 'flags'], ['snapshot_flags', 'flags', 'response'],
                 ['snapshot_flags', 'flags', 'response', 'data'],
                 ['snapshot_flags', 'flags', 'response', 'data', 'result', 0],
                 ['snapshot_flags', 'flags', 'response', 'data', 'result', 0, 'metric'],
                 ['snapshot_flags', 'targets', 'response', 'data', 'result', 0, 'metric']]
        for path in paths:
            data = self.fixture(); node = data
            for key in path:
                node = node[key]
            node['operator_note'] = {'server_name': 'PRIVATE_TEST_MARKER'}
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_snapshot(data)

    def test_public_metadata_scalars_and_flag_vectors_have_closed_types(self):
        cases = [(['generated_at'], {}), (['product', 'name'], {}),
                 (['history_methods', 'counter'], {}), (['history_methods', 'row_fields'], ['PRIVATE_TEST_MARKER']),
                 (['flags', 'Guard'], True), (['dircache_cohort', 'method'], {}),
                 (['dircache_cohort', 'configured_nonexit_zero'], {}),
                 (['snapshot_flags', 'flags', 'query'], {}),
                 (['snapshot_flags', 'flags', 'response', 'status'], {}),
                 (['snapshot_flags', 'flags', 'response', 'data', 'result'], {}),
                 (['snapshot_flags', 'flags', 'response', 'data', 'result', 0, 'metric', 'type'], 'PRIVATE_TEST_MARKER'),
                 (['snapshot_flags', 'flags', 'response', 'data', 'result', 0, 'value'], {}),
                 (['charts', 0, 'introduced_note'], {})]
        for path, value in cases:
            data = self.fixture(); node = data
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_snapshot(data)
        data, _, auxiliary = self.auxiliary_day()
        auxiliary['history_status'] = {'operator_note': 'PRIVATE_TEST_MARKER'}
        with self.assertRaisesRegex(ValueError, 'history status'):
            validate_snapshot(data)
        for value in ['NaN', '+Inf', '-1', '1.5', '1153', '1e-9999', '1e9999', True, {}]:
            data = self.fixture()
            data['snapshot_flags']['up']['response']['data']['result'][0]['value'][1] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'count-string'):
                validate_snapshot(data)
        data = self.fixture()
        data['snapshot_flags']['up']['response']['data']['result'][0]['value'][1] = '1.152e3'
        validate_snapshot(data)

    def test_flag_probe_and_chart_grid_timestamps_require_utc_midnight(self):
        data = self.fixture()
        for probe in ['flags', 'targets', 'up']:
            data['snapshot_flags'][probe]['response']['data']['result'][0]['value'][0] = float(data['end'])
        validate_snapshot(data)
        for probe in ['flags', 'targets', 'up']:
            for delta in [.125, 1, 1800, 3600]:
                data = self.fixture()
                data['snapshot_flags'][probe]['response']['data']['result'][0]['value'][0] += delta
                with self.subTest(probe=probe, delta=delta), self.assertRaises(ValueError):
                    validate_snapshot(data)
        data = self.fixture()
        for probe in ['flags', 'targets', 'up']:
            data['snapshot_flags'][probe]['response']['data']['result'][0]['value'][0] -= 3600
        validate_snapshot(data)
        for value in [-.125, float('nan'), float('inf'), float('-inf'), True, False, None, '1.125']:
            data = self.fixture()
            data['snapshot_flags']['up']['response']['data']['result'][0]['value'][0] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_snapshot(data)
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][0][0] += .125
        with self.assertRaisesRegex(ValueError, 'unaligned'):
            validate_snapshot(data)

    def test_current_day_cannot_shrink_expected_population_to_certify_a_subset(self):
        data = self.fixture()
        series = data['charts'][0]['series'][0]
        series.pop('relay_population', None)
        if series['history']['daily'][-1][0] == data['end']:
            series['history']['daily'].pop()
        series['current']['day'] = {
            'value': 10, 'relays': 8, 'expected_relays': 8, 'coverage': 1, 'valid': True}
        with self.assertRaisesRegex(ValueError, 'qualification'):
            validate_snapshot(data)
        # Cohort churn can produce more qualified 24h identities than are active now.
        data = self.fixture(share_rows=False); expected = data['charts'][0]['relays'] + 8
        series = data['charts'][0]['series'][0]
        series.pop('relay_population', None)
        if series['history']['daily'][-1][0] == data['end']:
            series['history']['daily'][-1][2] = expected
            series['history']['daily'][-1][3] = 1
        series['current']['day'].update(
            relays=expected, expected_relays=expected, coverage=1)
        validate_snapshot(data)

    def test_unqualified_current_rows_still_require_truthful_coverage(self):
        source = self.fixture(); expected = source['charts'][0]['relays']
        s0 = source['charts'][0]['series'][0]
        s0.pop('relay_population', None)
        if s0['history']['daily'][-1][0] == source['end']:
            s0['history']['daily'].pop()
        for reporting, coverage in [(0, 1), (expected+8, None), (0, .5), (None, 0)]:
            data = clone_snapshot(source)
            day = data['charts'][0]['series'][0]['current']['day']
            day.update(value=None, relays=reporting, expected_relays=expected, coverage=coverage, valid=False)
            with self.subTest(reporting=reporting, coverage=coverage), self.assertRaisesRegex(ValueError, 'coverage'):
                validate_snapshot(data)
        data = clone_snapshot(source)
        data['charts'][0]['series'][0]['current']['day'].update(
            value=None, relays=8, expected_relays=expected, coverage=8/expected, valid=False)
        validate_snapshot(data)

    def test_runtime_source_keys_are_only_fixed_public_labels(self):
        data = self.fixture()
        data['versions']['runtime_resolution']['source_sha256']['PRIVATE_TEST_MARKER'] = 'a'*64
        with self.assertRaisesRegex(ValueError, 'public runtime source label'):
            validate_snapshot(data)
        for value in ['not-a-digest', 'g'*64, None, True, {}]:
            data = self.fixture()
            data['versions']['runtime_resolution']['source_sha256']['continuity'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'hashes'):
                validate_snapshot(data)

    def test_queries_require_content_without_rewriting_supplied_text(self):
        paths = [['charts', 0, 'query'], ['charts', 0, 'quality_query']]
        paths += [['charts', 0, 'history_queries', p, field]
                  for p in ('daily', 'sampled_hour', 'current24')
                  for field in ('value', 'eligible', 'available')]
        paths += [['snapshot_flags', probe, 'query'] for probe in ('flags', 'targets', 'up')]
        for path in paths:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, '[Qq]uery'):
                validate_snapshot(set_path(self.fixture(), path, ' \t \n'))
        data = self.fixture(); preserved = []
        for path in paths:
            node = data
            for key in path[:-1]:
                node = node[key]
            value = ' \t' + node[path[-1]] + '\n'
            node[path[-1]] = value
            preserved.append((node, path[-1], value))
        validate_snapshot(data)
        self.assertTrue(all(node[key] == value for node, key, value in preserved))

    def test_chart_queries_enforce_hourly_and_daily_ranges(self):
        hourly_paths = [
            ['charts', 0, 'query'], ['charts', 0, 'quality_query'],
            ['charts', 0, 'history_queries', 'sampled_hour', 'value'],
            ['charts', 0, 'history_queries', 'sampled_hour', 'eligible'],
        ]
        bad_hourly = [
            'sum(rate(tor_relay_dos_total[1d]))',
            'sum(rate(tor_relay_dos_total[1h])) + sum(rate(tor_relay_dos_total[1d]))',
            'sum(tor_relay_dos_total)', 'sum(tor_relay_dos_total) # [1h]', '# [1h]',
        ]
        for path in hourly_paths:
            for query in bad_hourly:
                with self.subTest(path=path, query=query), self.assertRaisesRegex(ValueError, r'1h|period contract'):
                    validate_snapshot(set_path(self.fixture(), path, query))
        daily_paths = [
            ['charts', 0, 'history_queries', period, field]
            for period in ('daily', 'current24')
            for field in ('value', 'eligible')
        ]
        bad_daily = [
            'sum(rate(tor_relay_dos_total[1h]))',
            'sum(rate(tor_relay_dos_total[1d])) + sum(rate(tor_relay_dos_total[1h]))',
            'sum(tor_relay_dos_total)', 'sum(tor_relay_dos_total) # [1d]', '# [1d]',
        ]
        for path in daily_paths:
            for query in bad_daily:
                with self.subTest(path=path, query=query), self.assertRaisesRegex(ValueError, 'period contract'):
                    validate_snapshot(set_path(self.fixture(), path, query))
        for bad_available in [
            'count(count_over_time(tor_relay_dos_total[1h]))',
            'count(count_over_time(tor_relay_dos_total[1d]))',
            '# comment only',
        ]:
            data = self.fixture()
            data['charts'][0]['history_queries']['daily']['available'] = bad_available
            with self.subTest(available=bad_available), self.assertRaisesRegex(ValueError, 'period contract'):
                validate_snapshot(data)
        for query, available in [
            ('sum(rate(tor_relay_dos_total{note="[1d]"}[1h]))', 'count(tor_relay_dos_total{note="[1d]"})'),
            ('sum(rate(tor_relay_dos_total[1h])) # previously [1d]', 'count(tor_relay_dos_total) # [1d]'),
            ('sum(rate(tor_relay_dos_total{note="[1d]#notcomment"}[1h]))', 'count(tor_relay_dos_total{note="[1d]#notcomment"})'),
        ]:
            data = self.fixture()
            data['charts'][0]['query'] = query
            data['charts'][0]['history_queries']['daily']['available'] = available
            validated = validate_snapshot(data)
            self.assertEqual(validated['charts'][0]['query'], query)
            self.assertEqual(validated['charts'][0]['history_queries']['daily']['available'], available)

    def test_malformed_catalogue_runtime_and_current_shapes_reject_cleanly(self):
        cases = [(['families', 0, 'children'], [{}]), (['versions', 'verified_runtime'], None),
                 (['versions', 'verified_runtime', 0, 'patched'], 'false'),
                 (['versions', 'runtime_resolution'], []), (['versions', 'runtime_resolution', 'source_sha256'], []),
                 (['versions', 'runtime_resolution', 'observed_at'], {'server_name': 'PRIVATE_TEST_MARKER'}),
                 (['versions', 'runtime_resolution', 'mismatches'], True),
                 (['versions', 'runtime_resolution', 'snapshot_end'], 'yesterday'),
                 (['versions', 'metric_cohorts'], []), (['versions', 'metric_cohorts', 'native'], []),
                 (['charts', 0, 'kind'], {}), (['charts', 0, 'cohort_key'], []),
                 (['charts', 0, 'history_queries'], None), (['charts', 0, 'history_queries', 'daily'], {}),
                 (['charts', 0, 'history_queries', 'daily', 'value'], []),
                 (['charts', 0, 'series', 0, 'current'], ['hour', 'day'])]
        for path, value in cases:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_snapshot(set_path(self.fixture(), path, value))

    def test_unrepresentable_display_scale_fails_clearly_without_nonfinite_output(self):
        from build import prepare
        data = self.fixture(); data['charts'][0]['display_scale'] = 1e308
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, 'finite'):
            prepare(data, 'a'*64, Path(folder))

    def test_csv_units_cannot_hide_formula_prefixes_behind_whitespace(self):
        for unit in ['  =1+1', '\t=1+1', 'events\n/s']:
            data = self.fixture(); data['charts'][0]['unit'] = unit
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, 'CSV'):
                validate_snapshot(data)

    def test_truthy_flags_bad_populations_and_coverage_rejected(self):
        for index, value in [(4, 'false'), (2, -1), (3, .1)]:
            data = self.fixture(share_rows=False)
            row = next(r for r in data['charts'][0]['series'][0]['history']['daily'] if r[4])
            row[index] = value
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_snapshot(data)

    def test_duplicate_or_future_intervals_rejected(self):
        data = self.fixture(); series = data['charts'][0]['series'][0]
        series['history']['daily'].append(list(series['history']['daily'][-1]))
        with self.assertRaisesRegex(ValueError, 'unique'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False); data['charts'][0]['series'][0]['history']['daily'][-1][0] += DAY
        with self.assertRaisesRegex(ValueError, 'beyond'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False); data['charts'][0]['series'][0]['history']['daily'][-1][0] += 3600
        with self.assertRaisesRegex(ValueError, 'unaligned'):
            validate_snapshot(data)

    def test_headline_and_plot_cannot_disagree(self):
        data = self.fixture()
        series = data['charts'][0]['series'][0]
        series['current']['day']['value'] = 200.0 if series['current']['day']['value'] != 200.0 else 100.0
        with self.assertRaisesRegex(ValueError, 'Headline'):
            validate_snapshot(data)

    def test_family_primary_and_cohort_integrity(self):
        data = self.fixture(); data['families'][0]['primary_series'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'primary'):
            validate_snapshot(data)
        data = self.fixture(); data['charts'][0]['primary_series'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'primary'):
            validate_snapshot(data)
        data = self.fixture(); data['charts'][0]['cohort_key'] = 'custom'
        with self.assertRaisesRegex(ValueError, 'population'):
            validate_snapshot(data)
        data = self.fixture(); data['versions']['as_of'] += 3600
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            validate_snapshot(data)

    def test_schema_version_and_runtime_counts_have_their_own_types(self):
        for value in [True, 1, 2.0, None]:
            data = self.fixture(); data['input_schema_version'] = value
            with self.subTest(schema=value), self.assertRaisesRegex(ValueError, 'schema version'):
                validate_snapshot(data)
        for field in ['runtime_verified_total', 'runtime_unverified_relays']:
            for value in [True, -1, .5]:
                data = self.fixture(); data['versions'][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'version count'):
                    validate_snapshot(data)

    def test_public_runtime_totals_reject_booleans_and_nested_metadata(self):
        for value in [True, -1, .5, None, {'server_name': 'PRIVATE_TEST_MARKER'}]:
            data = self.fixture(); data['versions']['runtime_binary_hash_verified_total'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'runtime count'):
                validate_snapshot(data)
        data = self.fixture(); del data['versions']['runtime_binary_hash_verified_total']
        validate_snapshot(data)
        data = self.fixture()
        data['fleet'] = data['versions']['native_total'] = data['versions']['runtime_verified_total'] = 8
        data['versions']['runtime_unverified_relays'] = 0
        data['versions']['verified_runtime'] = data['versions']['verified_runtime'][:1]
        data['versions']['verified_runtime'][0]['relays'] = 8
        counts = data['versions']['metric_cohorts']['native']
        counts.update(dict.fromkeys(counts, 0)); counts[next(iter(counts))] = 8
        for chart in data['charts']:
            if chart['cohort_key'] == 'native':
                chart['relays'] = 8
        validate_snapshot(data)
        data['versions']['native_total'] = True
        with self.assertRaisesRegex(ValueError, 'native runtime count'):
            validate_snapshot(data)

    def test_integral_float_bounds_keep_the_same_daily_grid(self):
        data = self.fixture()
        data['start'], data['end'] = float(data['start']), float(data['end'])
        validate_snapshot(data)

    def test_source_metadata_rejects_extra_inventory_and_nested_labels(self):
        data = self.fixture()
        expected = dict(data['sources'])
        self.assertEqual(validate_snapshot(data)['sources'], expected)
        data['sources']['inventory'] = {'server_name': 'PRIVATE_MARKER'}
        with self.assertRaisesRegex(ValueError, 'source metadata'):
            validate_snapshot(data)
        data = self.fixture(); data['sources']['feed'] = {'label': 'PRIVATE_MARKER'}
        with self.assertRaisesRegex(ValueError, 'Source labels'):
            validate_snapshot(data)

    def test_family_children_must_be_arrays(self):
        for children in [None, 'transport_rejections', {'transport_rejections': True}]:
            data = self.fixture(); data['families'][0]['children'] = children
            with self.subTest(children=children), self.assertRaisesRegex(ValueError, 'children must be arrays'):
                validate_snapshot(data)

    def test_unplotted_auxiliary_history_is_normalized_but_displayed_history_is_required(self):
        data = self.fixture()
        auxiliary = next(s for c in data['charts'] for s in c['series'] if s['key'] not in c['display_keys'])
        auxiliary.pop('relay_population', None)
        auxiliary.pop('history')
        validate_snapshot(data)
        self.assertEqual(auxiliary['history'], {'daily': [], 'sampled_hour': []})
        data = self.fixture(); data['charts'][0]['series'][0].pop('history')
        data['charts'][0]['series'][0].pop('relay_population', None)
        with self.assertRaisesRegex(ValueError, 'missing historical'):
            validate_snapshot(data)

    def test_unqualified_denominators_are_checked_and_omitted_values_use_the_cohort(self):
        for value in [None, 0, -1, True, 'invalid']:
            data = self.fixture()
            data['charts'][0]['series'][0]['current']['day'].update(valid=False, expected_relays=value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'denominator'):
                validate_snapshot(data)
        data = self.fixture(); del data['charts'][0]['series'][0]['current']['day']['expected_relays']
        validate_snapshot(data)

    def test_metric_lists_and_prototype_keys_are_validated(self):
        for value in [None, 'tor_relay_dos_total', [1], [], {'name': 'tor_relay_dos_total'}]:
            data = self.fixture(); data['charts'][0]['metrics'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Metrics must be an array'):
                validate_snapshot(data)
        for value in [None, 'discarded', {'prototype_value': 1}]:
            data = self.fixture(); data['history_methods']['__proto__'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Reserved JSON key'):
                validate_snapshot(data)

    def test_provenance_values_are_public_scalar_metadata(self):
        cases = [('description', {'server_name': 'INTERNAL_INVENTORY'}),
                 ('description', None), ('snapshot_end', None), ('snapshot_end', True),
                 ('snapshot_end', 'yesterday'), ('snapshot_end', SOURCE['end']-3600),
                 ('source_public_data_sha256', None), ('source_public_data_sha256', {'key': 'secret'}),
                 ('source_public_data_sha256', 'not-a-hash')]
        for field, value in cases:
            data = self.fixture(); data['input_provenance'][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'provenance'):
                validate_snapshot(data)
        data = self.fixture(); data['input_provenance'] = {}
        validate_snapshot(data)

    def test_oversized_json_integer_is_a_validation_error_not_overflow(self):
        data = self.fixture(); data['fleet'] = 10**400
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            validate_snapshot(data)

    def test_missing_children_fails_before_later_dictionary_access(self):
        data = self.fixture()
        moved = data['families'][0].pop('children')
        data['families'][1]['children'].extend(moved)
        with self.assertRaisesRegex(ValueError, 'children must be arrays'):
            validate_snapshot(data)

    def test_inferred_validity_requires_consistent_supplied_coverage(self):
        for value in [.5, None, True, {'private_note': 'nested'}]:
            data, _, auxiliary = self.auxiliary_day()
            auxiliary['current']['day']['coverage'] = value
            with self.subTest(coverage=value), self.assertRaisesRegex(ValueError, '[Cc]overage'):
                validate_snapshot(data)
        from build import prepare
        for supplied in [False, True]:
            data, chart, auxiliary = self.auxiliary_day()
            expected_coverage = auxiliary['current']['day']['relays'] / auxiliary['current']['day']['expected_relays']
            if supplied:
                auxiliary['current']['day']['coverage'] = expected_coverage
            with tempfile.TemporaryDirectory() as folder:
                built, _ = prepare(data, 'a'*64, Path(folder))
            result = next(c for c in built['charts'] if c['id']==chart['id'])
            day = next(s for s in result['series'] if s['key']==auxiliary['key'])['current']['day']
            self.assertTrue(day['valid'])
            self.assertEqual(day['coverage'], expected_coverage)

    def test_no_reporters_accept_null_coverage_and_remain_unavailable(self):
        from build import prepare
        for reporting, coverage in [(0, None), (None, None), (0, 0)]:
            data, chart, auxiliary = self.auxiliary_day()
            auxiliary['current']['day'].update(value=None, relays=reporting, coverage=coverage)
            with self.subTest(reporting=reporting, coverage=coverage), tempfile.TemporaryDirectory() as folder:
                built, _ = prepare(data, 'a'*64, Path(folder))
            result = next(c for c in built['charts'] if c['id']==chart['id'])
            day = next(s for s in result['series'] if s['key']==auxiliary['key'])['current']['day']
            self.assertFalse(day['valid'])
            self.assertEqual(day['coverage'], coverage)
            self.assertEqual(day['display'], 'Unavailable')
        data, _, auxiliary = self.auxiliary_day()
        auxiliary['current']['day'].update(value=None, relays=0, coverage=.5)
        with self.assertRaisesRegex(ValueError, 'coverage'):
            validate_snapshot(data)

    def test_coverage_rounding_cannot_cross_qualification_boundaries(self):
        from build import prepare
        from windows import valid_current
        for ratio_n, supplied, qualifies in [(9, .9, True), (9, .9000000000005, True), (8, .8000000000004, False)]:
            data, chart, auxiliary = self.auxiliary_day()
            base = int(chart['relays'])
            auxiliary['current']['day'] = {'value': 10, 'relays': ratio_n*base,
                                          'expected_relays': 10*base, 'coverage': supplied}
            with self.subTest(supplied=supplied), tempfile.TemporaryDirectory() as folder:
                built, _ = prepare(data, 'a'*64, Path(folder))
            result = next(c for c in built['charts'] if c['id']==chart['id'])
            day = next(s for s in result['series'] if s['key']==auxiliary['key'])['current']['day']
            self.assertEqual(day['valid'], qualifies)
            self.assertEqual(valid_current(day), qualifies)
        for case in ['reported_below_threshold', 'ratio_below_threshold', 'ratio_above_one']:
            for explicit_valid in [False, True]:
                data, chart, auxiliary = self.auxiliary_day()
                base = int(chart['relays'])
                supplied, reporting, expected = {
                    'reported_below_threshold': (.8999999999995, 9*base, 10*base),
                    'ratio_below_threshold': (.9, base*(9*10**11-1), base*10**12),
                    'ratio_above_one': (1, base*(10**12+1), base*10**12),
                }[case]
                auxiliary['current']['day'] = {'value': 10, 'relays': reporting,
                                              'expected_relays': expected, 'coverage': supplied}
                if explicit_valid:
                    auxiliary['current']['day']['valid'] = True
                with self.subTest(case=case, explicit=explicit_valid), self.assertRaisesRegex(ValueError, '[Cc]overage'):
                    validate_snapshot(data)

    def test_daily_expected_population_is_shared_with_retained_peak(self):
        from build import prepare
        for expected_multiplier, qualified in [(1.05, True), (2, False)]:
            data, chart, auxiliary = self.auxiliary_day()
            reporting = int(chart['relays'])
            expected = ((math.ceil(reporting * expected_multiplier) + 7) // 8) * 8
            data['start'], data['end'] = float(data['start']), float(data['end'])
            end = int(data['end'])
            auxiliary['history']['daily'] = [[end - DAY, 10, reporting, 1, True]]
            auxiliary['current']['day'] = {
                'value': 1000 if qualified else None, 'relays': reporting,
                'expected_relays': expected, 'coverage': reporting/expected, 'valid': qualified}
            with self.subTest(qualified=qualified), tempfile.TemporaryDirectory() as folder:
                built, _ = prepare(data, 'a'*64, Path(folder))
            result = next(c for c in built['charts'] if c['id']==chart['id'])
            series = next(s for s in result['series'] if s['key']==auxiliary['key'])
            self.assertEqual(series['current']['day']['valid'], qualified)
            self.assertEqual(series['peaks']['day']['fleet_peak'], 1000 if qualified else 10)
            self.assertEqual(series['peaks']['day']['current_is_record'], qualified)
            if not qualified:
                auxiliary['current']['day']['value'] = 1000
                with self.assertRaisesRegex(ValueError, 'Unqualified|Unreported|contradicts'):
                    validate_snapshot(data)

    def test_current_day_cannot_shrink_below_latest_chart_cohort(self):
        data, chart, auxiliary = self.auxiliary_day()
        n = max(8, int(chart['relays']) - 8)
        auxiliary['current']['day'].update(value=10, relays=n, expected_relays=n, coverage=1, valid=True)
        with self.assertRaisesRegex(ValueError, 'denominator'):
            validate_snapshot(data)

    def test_expected_population_must_remain_present_and_positive(self):
        for replacement in [None, 0, -8]:
            data, _, auxiliary = self.auxiliary_day()
            auxiliary['current']['day']['expected_relays'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                validate_snapshot(data)

    def test_display_keys_require_a_unique_array_of_series_names(self):
        keys = SOURCE['charts'][0]['display_keys']
        for value in [dict.fromkeys(keys, True), keys[0], None, [], [keys[0], keys[0]], [[]]]:
            data = self.fixture(); data['charts'][0]['display_keys'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'displayed/primary'):
                validate_snapshot(data)

    def test_series_require_an_array_of_objects_before_key_access(self):
        for value in [None, {}, 'series', []]:
            data = self.fixture(); data['charts'][0]['series'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Series must be'):
                validate_snapshot(data)
        for value in [None, 'series', [], 1]:
            data = self.fixture(); data['charts'][0]['series'][0] = value
            with self.subTest(item=value), self.assertRaisesRegex(ValueError, 'invalid series field'):
                validate_snapshot(data)

    def test_family_and_cohort_labels_cannot_contain_nested_objects(self):
        for field in ['title', 'icon']:
            for value in [{'label': 'nested'}, ['nested'], None, True]:
                data = self.fixture(); data['families'][0][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'Family labels'):
                    validate_snapshot(data)
        for value in [None, [], 'labels', {}]:
            data = self.fixture(); data['versions']['cohort_labels'] = value
            with self.subTest(mapping=value), self.assertRaisesRegex(ValueError, 'Cohort labels'):
                validate_snapshot(data)
        for value in [{'label': 'nested'}, ['nested'], None, True]:
            data = self.fixture(); data['versions']['cohort_labels']['stock_0_4_9_15'] = value
            with self.subTest(label=value), self.assertRaisesRegex(ValueError, 'Cohort labels'):
                validate_snapshot(data)

    def test_non_public_versions_subfields_and_runtime_urls_are_validated(self):
        cases = [
            (['versions', 'default_cli_only'], ['host-record'], 'CLI-only'),
            (['versions', 'provenance', 'runtime_query_file'], '../secret.json', 'provenance filename'),
            (['versions', 'verified_runtime', 0, 'source_url'], 'javascript:alert(1)', 'source URL'),
            (['versions', 'binary_hash_counts', 0, 'metric', 'sha256'], 'bad', 'binary hash'),
            (['versions', 'runtime_resolution', 'resolved_relays'], 7, 'runtime resolution'),
            (['versions', 'runtime_resolution', 'snapshot_end'], SOURCE['end'] + 3600, 'resolution window'),
        ]
        for path, value, pattern in cases:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, pattern):
                validate_snapshot(set_path(self.fixture(), path, value))

    def test_v4_hourly_contract_and_v3_seed_admission(self):
        # Schema v3 is rejected by default, admitted only with allow_v3_seed=True.
        v3 = self.fixture()
        v3.update(input_schema_version=3, window_seconds=DAY, minimum_samples=360, expected_samples=720)
        for chart in v3['charts']:
            chart['kind'] = 'gauge_daily_mean' if chart['kind'] == 'gauge_hour_mean' else 'counter_daily_rate'
            chart['query'] = chart['history_queries']['daily']['value']
            chart['quality_query'] = chart['history_queries']['daily']['eligible']
            chart['history_queries'] = {'daily': chart['history_queries']['daily']}
            for series in chart['series']:
                for legacy in ('values', 'hourly_relays', 'hourly_expected_relays'):
                    series.pop(legacy, None)
                series['history'].pop('sampled_hour', None)
                series['current'] = {'day': series['current']['day']}
                if 'relay_population' in series:
                    series['relay_population'].pop('hour', None)
                    series['relay_population']['queries'] = {'day': series['relay_population']['queries']['day']}
        if 'runtime_resolution' in v3['versions']:
            v3['versions']['runtime_resolution']['snapshot_start'] = v3['start']
        with self.assertRaisesRegex(ValueError, 'schema version'):
            validate_snapshot(v3)
        validate_snapshot(v3, allow_v3_seed=True)

        data = self.fixture()
        data['window_seconds'] = 86400
        with self.assertRaisesRegex(ValueError, 'hourly measurement contract'):
            validate_snapshot(data)
        data = self.fixture()
        data['start'] += 1800
        data['end'] += 1800
        with self.assertRaisesRegex(ValueError, 'UTC-aligned'):
            validate_snapshot(data)
        for field in ('values', 'hourly_relays', 'hourly_expected_relays'):
            data = self.fixture()
            data['charts'][0]['series'][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_snapshot(data)
        data = self.fixture()
        data['charts'][0]['series'][0]['current']['current24'] = {'value': 0, 'relays': 8, 'coverage': 1, 'valid': True}
        with self.assertRaises(ValueError):
            validate_snapshot(data)
        data = self.fixture()
        data['charts'][0]['series'][0]['history']['sampled_hour'] = []
        with self.assertRaises(ValueError):
            validate_snapshot(data)

        # Intra-day UTC-hour boundary (% 3600 == 0, % 86400 != 0) validates when consistent.
        hourly = self.fixture()
        hourly['start'] += 3600
        hourly['end'] += 3600
        hourly['input_provenance']['snapshot_end'] = hourly['end']
        hourly['sources']['current_observation_end'] = hourly['end']
        hourly['versions']['as_of'] = hourly['end']
        if 'runtime_resolution' in hourly['versions']:
            hourly['versions']['runtime_resolution'].update(snapshot_start=hourly['end'] - 3600, snapshot_end=hourly['end'])
        for chart in hourly['charts']:
            chart['start'], chart['end'] = hourly['start'], hourly['end']
            for series in chart['series']:
                for arr in (series['values'], series['hourly_relays'], series['hourly_expected_relays']):
                    for pt in arr:
                        pt[0] += 3600
                if 'relay_population' in series:
                    for p_key in ('hour', 'day'):
                        if series['relay_population'].get(p_key) is not None:
                            series['relay_population'][p_key][0] = hourly['end']
        validate_snapshot(hourly)

    def test_coarsening_rules_and_rejection_of_uncoarsened_inputs(self):
        self.assertEqual([coarsen_count(x) for x in (0, 1, 7, 8, 11, 12, 13)], [0, 0, 0, 8, 8, 16, 16])
        self.assertEqual(coarsen_partition([10, 15, 23], 48), [8, 16, 24])
        self.assertEqual(coarsen_value('open_circuits', 'open_circuits', 'gauge_daily_mean', 7060333.83), 7060000.0)
        self.assertEqual(coarsen_value('open_circuits', 'open_circuits', 'gauge_daily_mean', 1249.0), 1200.0)
        self.assertEqual(coarsen_value('hsdir_fetches', 'success', 'counter_daily_rate', 0.0004), 0.0)
        self.assertEqual(coarsen_value('hsdir_fetches', 'success', 'counter_daily_rate', 0.0014), 0.001)
        self.assertEqual(coarsen_value('transport_rejections', 'conn_rejected', 'counter_daily_rate', 773.92), 774.0)
        self.assertEqual(coarsen_value('transport_rejections', 'conn_rejected', 'counter_daily_rate', 5 / DAY), 0.0)
        self.assertAlmostEqual(sigfigs3(Decimal('0.012345')), 0.0123)
        # Hourly coarsening idempotency and alignment with is_coarsened_value (HOUR=3600 and DAY=86400)
        hour = windows.HOUR
        self.assertEqual(coarsen_value('transport_rejections', 'conn_rejected', 'counter_hour_rate', 8 / hour, window_seconds=hour), 0.00222)
        self.assertEqual(coarsen_value('transport_rejections', 'conn_rejected', 'counter_hour_rate', 0.00222, window_seconds=hour), 0.00222)
        self.assertTrue(is_coarsened_value('transport_rejections', 'conn_rejected', 'counter_hour_rate', 0.00222, window_seconds=hour))
        self.assertEqual(coarsen_value('transport_rejections', 'conn_rejected', 'counter_hour_rate', 0.001, window_seconds=hour), 0.0)
        self.assertFalse(is_coarsened_value('transport_rejections', 'conn_rejected', 'counter_hour_rate', 0.001, window_seconds=hour))
        for sensitive_chart, series_key in (('hsdir_fetches', 'success'), ('hsdir_uploads', 'success'),
                                            ('introduction_rejections', 'introduction_rejections'),
                                            ('directory_requests', 'hs_get'), ('directory_requests', 'hs_post')):
            self.assertEqual(coarsen_value(sensitive_chart, series_key, 'counter_hour_rate', 0.001, window_seconds=hour), 0.0)
            self.assertEqual(coarsen_value(sensitive_chart, series_key, 'counter_hour_rate', 0.00222, window_seconds=hour), 0.0)
            self.assertEqual(coarsen_value(sensitive_chart, series_key, 'counter_hour_rate', 0.003, window_seconds=hour), 0.003)
            self.assertFalse(is_coarsened_value(sensitive_chart, series_key, 'counter_hour_rate', 0.001, window_seconds=hour))
            self.assertFalse(is_coarsened_value(sensitive_chart, series_key, 'counter_hour_rate', 0.00222, window_seconds=hour))
            self.assertTrue(is_coarsened_value(sensitive_chart, series_key, 'counter_hour_rate', 0.003, window_seconds=hour))
            self.assertEqual(coarsen_value(sensitive_chart, series_key, 'counter_daily_rate', 0.001, window_seconds=DAY), 0.001)
            self.assertTrue(is_coarsened_value(sensitive_chart, series_key, 'counter_daily_rate', 0.001, window_seconds=DAY))
        for chart_id, series_key, kind, wsec in (
            ('transport_rejections', 'conn_rejected', 'counter_hour_rate', hour),
            ('hsdir_fetches', 'success', 'counter_hour_rate', hour),
            ('directory_waiting', 'scalar', 'gauge_hour_mean', hour),
            ('open_circuits', 'scalar', 'gauge_hour_mean', hour),
            ('hsdir_fetches', 'success', 'counter_daily_rate', DAY),
            ('directory_waiting', 'scalar', 'gauge_daily_mean', DAY),
        ):
            for raw in (0, 0.001, 0.00222, 0.003, 0.12, 1.234, 1234.56):
                once = coarsen_value(chart_id, series_key, kind, raw, window_seconds=wsec)
                twice = coarsen_value(chart_id, series_key, kind, once, window_seconds=wsec)
                self.assertEqual(once, twice, (chart_id, wsec, raw))
                self.assertTrue(is_coarsened_value(chart_id, series_key, kind, once, window_seconds=wsec), (chart_id, wsec, raw))
        self.assertEqual(coarsen_population_row([1794960000, 100, 100, 30, 20, 3, 50, 80], 104),
                         [1794960000, 104, 104, 32, 24, 0, 48, 80])

        # Uncoarsened relay count (<8 or not multiple of 8) rejected
        data = self.fixture(); data['flags']['Guard'] += 1
        with self.assertRaisesRegex(ValueError, 'aggregate flag count'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][2] -= 1
        with self.assertRaisesRegex(ValueError, 'uncoarsened reporting population'):
            validate_snapshot(data)
        # Uncoarsened open_circuits (<1e4 not divisible by 100) rejected
        data = self.fixture(share_rows=False)
        oc_chart = next(c for c in data['charts'] if c['id'] == 'open_circuits')
        oc_chart['series'][0]['history']['daily'][-1][1] = 1230.0
        with self.assertRaisesRegex(ValueError, 'uncoarsened observation value'):
            validate_snapshot(data)
        # Uncoarsened sub-0.001/s onion-service rate rejected
        data = self.fixture(share_rows=False)
        hs_chart = next(c for c in data['charts'] if c['id'] == 'hsdir_fetches')
        hs_chart['series'][0]['history']['daily'][-1][1] = 0.0005
        with self.assertRaisesRegex(ValueError, 'uncoarsened observation value'):
            validate_snapshot(data)
        # More than 3 significant digits or unbinned counter rates rejected
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][1] = 773.92
        with self.assertRaisesRegex(ValueError, 'uncoarsened observation value'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][1] = 0.0001
        with self.assertRaisesRegex(ValueError, 'uncoarsened observation value'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][1] = 0.0000926
        data['charts'][0]['series'][0]['current']['day']['value'] = 0.0000926
        validate_snapshot(data)
        # Coarsening without a matching final daily row clears value when coverage drops below 0.9
        data = self.fixture()
        target_series = data['charts'][0]['series'][0]
        target_series['history']['daily'] = [r for r in target_series['history']['daily'] if r[0] < data['end']]
        if 'relay_population' in target_series:
            target_series['relay_population']['day'] = None
        target_series['current']['day'] = {'value': 5.0, 'relays': 18, 'expected_relays': 20, 'coverage': 0.9, 'valid': True}
        from coarsening import coarsen_snapshot
        coarsened = coarsen_snapshot(data)
        c_day = coarsened['charts'][0]['series'][0]['current']['day']
        self.assertFalse(c_day['valid'])
        self.assertIsNone(c_day['value'])
        # Coarsening with a matching final daily row preserves daily failure even when current expected_relays is smaller
        data = self.fixture()
        target_series = data['charts'][0]['series'][0]
        chart_relays = data['charts'][0]['relays']
        failing_n = ((int(0.9 * chart_relays) + 7) // 8) * 8
        if failing_n / chart_relays < 0.9:
            failing_n += 8
        higher_exp = chart_relays + 128
        target_series['history']['daily'][-1] = [data['end'], 5.0, failing_n, failing_n / higher_exp, True]
        target_series['current']['day'] = {'value': 5.0, 'relays': failing_n, 'expected_relays': chart_relays,
                                           'coverage': failing_n / chart_relays, 'valid': True}
        if 'relay_population' in target_series and target_series['relay_population'].get('day') is not None:
            pop = target_series['relay_population']
            pop['day'][1] = failing_n
            pop['day'][2] = min(pop['day'][2], failing_n)
            for idx in range(3, 8):
                pop['day'][idx] = min(pop['day'][idx], pop['day'][2])
            if pop['day'][4] > pop['day'][2] - max(pop['day'][3], pop['day'][5], pop['day'][6], pop['day'][7]):
                pop['day'][4] = max(0, pop['day'][2] - max(pop['day'][3], pop['day'][5], pop['day'][6], pop['day'][7]))
            if pop['daily'] and pop['daily'][-1][0] == data['end']:
                pop['daily'][-1] = list(pop['day'])
        coarsened = coarsen_snapshot(data)
        validate_snapshot(coarsened)
        coarsened_series = coarsened['charts'][0]['series'][0]
        c_day = coarsened_series['current']['day']
        c_last = coarsened_series['history']['daily'][-1]
        self.assertFalse(c_last[4])
        self.assertIsNone(c_last[1])
        self.assertFalse(c_day['valid'])
        self.assertIsNone(c_day['value'])
        self.assertGreaterEqual(c_day['expected_relays'], higher_exp)

    def test_unicode_format_csv_units_prototype_keys_and_history_bounds_rejected(self):
        for unit in ['\ufeff=1+1', '\u200b=1+1', '|calc', '\u200b']:
            data = self.fixture(); data['charts'][0]['unit'] = unit
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, 'CSV'):
                validate_snapshot(data)
        data = self.fixture(); data['charts'][0]['unit'] = ' '
        with self.assertRaisesRegex(ValueError, 'Missing query or unit'):
            validate_snapshot(data)
        for key in ['constructor', 'prototype']:
            data = self.fixture(); data['history_methods'][key] = 'x'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Reserved JSON key'):
                validate_snapshot(data)
        data = self.fixture(); del data['history_methods']
        with self.assertRaisesRegex(ValueError, 'method metadata'):
            validate_snapshot(data)
        data = self.fixture(share_rows=False)
        row = next(r for r in data['charts'][0]['series'][0]['history']['daily'] if not r[4])
        row[1], row[2], row[3] = 100.0, 0, 1.0
        with self.assertRaisesRegex(ValueError, 'Unreported interval'):
            validate_snapshot(data)
        data = self.fixture(); data['flags']['Guard'] = 2**53
        with self.assertRaisesRegex(ValueError, 'flag count'):
            validate_snapshot(data)
        with self.assertRaisesRegex(ValueError, 'nesting'):
            parse_json('[' * 100 + '1' + ']' * 100)

    def test_promql_unterminated_quotes_and_redos_rejected(self):
        started = time.monotonic()
        redos = '"' + '\\"' * 5000
        self.assertFalse(_promql_ranges(redos))
        self.assertFalse(_promql_ranges(redos, want_ranges=False))
        self.assertLess(time.monotonic() - started, 0.5)
        for bad_query in (
            'sum(x{a="\\"[1d]})',
            "sum(x{a='\\'[1d]})",
            'sum(x{a=`unclosed[1d]})',
            'sum(rate(x[1d])) # comment\n + rate(x{a="unterminated}[1d])',
        ):
            with self.subTest(query=bad_query):
                self.assertFalse(_promql_ranges(bad_query))
        self.assertTrue(_promql_ranges('sum(rate(x[1d])) # unmatched "\n'))
        data = self.fixture()
        data['charts'][0]['query'] = 'sum(x{a="\\"[1d]})'
        with self.assertRaisesRegex(ValueError, r'\[1h\]'):
            validate_snapshot(data)

    def test_coarsening_hardening_negative_zero_overflow_and_kinds(self):
        with self.assertRaises(ValueError):
            coarsen_count(10**400)
        self.assertFalse(is_coarsened_count(10**400))
        with self.assertRaises(ValueError):
            coarsen_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', 10**400)
        self.assertFalse(is_coarsened_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', 10**400))
        with self.assertRaises(ValueError):
            coarsen_partition([10**400], 8)
        with self.assertRaises(ValueError):
            coarsen_partition([8], 10**400)

        for bad_target in (True, False, 8.9, float('nan'), float('inf'), -8, -0.0):
            with self.subTest(bad_target=bad_target), self.assertRaises(ValueError):
                coarsen_partition([8], bad_target)
        for bad_part in (True, 8.9, float('nan'), float('inf'), -8, -0.0):
            with self.subTest(bad_part=bad_part), self.assertRaises(ValueError):
                coarsen_partition([bad_part, 8], 8)
        for bad_str in ('8.9', 'NaN', 'Infinity', 'bad', '-8', '-0', '1e99999999', True):
            with self.subTest(bad_str=bad_str), self.assertRaises(ValueError):
                _coarsen_count_string(bad_str)

        for bad_kind in ('unknown', 'counter_min_rate'):
            for val in (None, 0, 1.0):
                with self.subTest(kind=bad_kind, val=val):
                    with self.assertRaisesRegex(ValueError, 'kind'):
                        coarsen_value('circuit_rejections', 'circuit_rejections', bad_kind, val)
                    with self.assertRaisesRegex(ValueError, 'kind'):
                        is_coarsened_value('circuit_rejections', 'circuit_rejections', bad_kind, val)

        self.assertFalse(windows.timestamp(-0.0))
        self.assertFalse(windows.usable_measurement(-0.0, 8))
        self.assertFalse(is_coarsened_count(-0.0))
        self.assertFalse(is_coarsened_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', -0.0))
        with self.assertRaises(ValueError):
            coarsen_count(-0.0)
        with self.assertRaises(ValueError):
            coarsen_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', -0.0)

    def test_validation_hardening_negative_zero_runtime_duplicates_and_resolution_window(self):
        # Reject -0.0 in counts, points, and coverages
        data = self.fixture()
        data['flags']['Running'] = -0.0
        with self.assertRaisesRegex(ValueError, 'aggregate flag count'):
            validate_snapshot(data)

        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][1] = -0.0
        with self.assertRaisesRegex(ValueError, 'observation value'):
            validate_snapshot(data)

        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][-1][3] = -0.0
        with self.assertRaisesRegex(ValueError, 'Invalid coverage'):
            validate_snapshot(data)

        data = self.fixture()
        s = data['charts'][0]['series'][0]
        s['current']['day'] = {'value': None, 'relays': 8, 'expected_relays': 8 * 10**12, 'coverage': -0.0, 'valid': False}
        with self.assertRaisesRegex(ValueError, 'Invalid current coverage'):
            validate_snapshot(data)

        data = self.fixture()
        if 'binary_hash_counts' in data['versions']:
            data['versions']['binary_hash_counts'][0]['value'][0] = -0.0
            with self.assertRaisesRegex(ValueError, 'binary hash count'):
                validate_snapshot(data)

        # Require snapshot_start and snapshot_end together in runtime_resolution
        for missing_key in ('snapshot_start', 'snapshot_end'):
            data = self.fixture()
            res = data['versions'].setdefault(
                'runtime_resolution', {'snapshot_start': data['start'], 'snapshot_end': data['end']})
            res.pop(missing_key, None)
            with self.subTest(missing=missing_key), self.assertRaisesRegex(ValueError, 'resolution window'):
                validate_snapshot(data)

        # Reject duplicate build_id in versions['verified_runtime']
        data = self.fixture()
        rt = data['versions']['verified_runtime'][0]
        dup = dict(rt, relays=0)
        data['versions']['verified_runtime'].append(dup)
        with self.assertRaisesRegex(ValueError, 'Duplicate verified runtime build ID'):
            validate_snapshot(data)

    def test_coarsening_round2_overflow_pop_totals_cohort_target_and_cache_keys(self):
        # Unhashable chart_id / series_key / kind rejected cleanly without TypeError
        for args in (
            (['bad'], 's', 'counter_daily_rate', 1.0),
            ('c', ['bad'], 'counter_daily_rate', 1.0),
            ('c', 's', ['bad'], 1.0),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                is_coarsened_value(*args)

        # coarsen_cohort_mapping rejects bool/-0.0 BEFORE short-circuits and missing runtime_unverified when target > 0
        for bad_target in (False, -0.0, -8, 3):
            with self.subTest(bad_target=bad_target), self.assertRaises(ValueError):
                coarsen_cohort_mapping({'a': 8}, bad_target)
        with self.assertRaisesRegex(ValueError, 'runtime_unverified'):
            coarsen_cohort_mapping({'a': 8}, 8)
        self.assertEqual(coarsen_cohort_mapping({'a': 8}, 0), {'a': 0})

        # tiny cov_raw causing n_raw / cov_raw -> inf raises ValueError (not OverflowError)
        data = self.fixture(share_rows=False)
        data['charts'][0]['series'][0]['history']['daily'][0] = [data['end'] - 10 * DAY, 1.0, 16, 1e-320, True]
        with self.assertRaisesRegex(ValueError, 'coverage ratio'):
            coarsen_snapshot(data)

        # invalid cov_raw (-0.0 / True) rejected
        for bad_cov in (-0.0, True):
            data = self.fixture(share_rows=False)
            data['charts'][0]['series'][0]['history']['daily'][0] = [data['end'] - 10 * DAY, 1.0, 16, bad_cov, True]
            with self.subTest(bad_cov=bad_cov), self.assertRaisesRegex(ValueError, 'observation coverage'):
                coarsen_snapshot(data)

        # Unreported history row (n_raw=None) must NOT invent a zero relay_population row
        data = self.fixture(share_rows=False)
        series = data['charts'][0]['series'][0]
        gap_ts = series['history']['daily'][0][0]
        series['history']['daily'][0] = [gap_ts, None, None, None, False]
        if 'relay_population' in series and series['relay_population']['daily']:
            sample_pop = list(series['relay_population']['daily'][0])
            sample_pop[0] = gap_ts
            series['relay_population']['daily'].insert(0, sample_pop)
        coarsened = coarsen_snapshot(data)
        self.assertNotIn(gap_ts, [r[0] for r in coarsened['charts'][0]['series'][0]['relay_population']['daily']])
        validate_snapshot(coarsened)

        # Explicit expected_relays=None and bool/None runtime relays rejected cleanly
        data = self.fixture()
        data['charts'][0]['series'][0]['current']['day']['expected_relays'] = None
        with self.assertRaisesRegex(ValueError, 'expected_relays'):
            coarsen_snapshot(data)

        for field_update in (
            lambda v: v['verified_runtime'][0].update(relays=True),
            lambda v: v.update(runtime_unverified_relays=None),
        ):
            data = self.fixture()
            field_update(data['versions'])
            with self.assertRaises(ValueError):
                coarsen_snapshot(data)


if __name__ == '__main__':
    unittest.main()
