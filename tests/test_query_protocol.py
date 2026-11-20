import codecs
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import query_protocol as access

spec = importlib.util.spec_from_file_location('build_catalogue', ROOT / 'scripts/build_catalogue.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class QueryProtocolTests(unittest.TestCase):
    def test_catalogue_is_standalone_and_reproducible(self):
        result = builder.build(access.load_json(ROOT / 'catalogue/contract.json'))
        for name, value in result.items():
            self.assertEqual(access.load_json(ROOT / 'catalogue' / name), value)
        self.assertEqual(len(result['queries.json']), 90)
        self.assertEqual(len(result['routes.json']), 208)
        populations = {key: value for key, value in result['queries.json'].items()
                       if key.startswith('relay_population_')}
        self.assertEqual(len(populations), 16)
        self.assertTrue(all(access.safe_aggregate(query) and len(query) < 10000
                            for query in populations.values()))

    def test_contract_routes_cannot_add_unknown_id(self):
        contract = access.load_json(ROOT / 'catalogue/contract.json')
        contract['operations']['bad'] = 'arbitrary'
        with self.assertRaises(ValueError):
            builder.build(contract)

    def test_catalogue_builder_rejects_malformed_contract_shapes(self):
        contract = access.load_json(ROOT / 'catalogue/contract.json')
        for bad in ([], dict(contract, schema_version=True), dict(contract, unknown=1),
                    dict(contract, operations={'bad op': 'circuit_rejections'}),
                    dict(contract, relay_population_groups={'Bad Slug': {'selector': 'up', 'label': '', 'charts': ['x']}})):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(ValueError):
                builder.build(bad)

    def test_full_aggregate_grammar(self):
        for query in ('sum(up) or up', 'sum by(type)(up) + up', 'sum(up) trailing',
                      'topk(1,up)', 'sum by(host)(up)', 'count by(instance)(up)',
                      'sum without(type)(up)', 'sum(up', 'sum(up))', 'count_values("host",up)',
                      'sum(up{job=\'")\'}) or max by(type)(up{job=\'(\'})',
                      'sum(up{job=`")`}) + vector(1)',
                      'sum(\nup # "\n) or max by(type)(tor_relay_flag) # ")',
                      'sum by(type, type)(up)',
                      'count by(type)(label_replace(up,"type","$1","instance","(.+)"))',
                      'sum(label_replace(up,"action","$1","host","(.+)"))',
                      'sum by(previous_hop)(up)',
                      'sum(label_replace(up,"action","$1","previous_hop","(.+)"))',
                      'sum(label_replace(up,"__name__","x","type","(.+)"))',
                      'count by(type)(label_join(up,"type",",","instance"))',
                      'sum(label_join (up,"action",",","host","relay_fingerprint"))',
                      'sum by(type)(label_join(up,"type","","type"))'):
            self.assertFalse(access.safe_aggregate(query), query)
        self.assertTrue(access.safe_aggregate('sum(up{note="label_join(a,b)"})'))
        self.assertTrue(access.safe_aggregate('sum(foo_label_join(up))'))
        with self.assertRaises(access.AccessError):
            access.validate_catalogue({'bad_hop': 'sum by(previous_hop)(up)'})
        self.assertTrue(access.safe_aggregate('count by(type)(label_replace(up,"a","b","c","(.+)"))'))
        self.assertTrue(access.safe_aggregate('sum by(type)(label_replace(up,"type","$1","action","(.+)"))'))
        self.assertTrue(access.safe_aggregate('sum by(type)(label_replace(up,"type","scalar","",".*"))'))
        queries = access.load_json(ROOT / 'catalogue/queries.json')
        for key, expr in queries.items():
            self.assertTrue(access.safe_aggregate(expr), key)

    def test_strict_json_parser_syntax_and_values(self):
        queries = access.load_json(ROOT / 'catalogue/queries.json')
        for bad_cmd in ('prom "', 'prom \\'):
            with self.assertRaisesRegex(access.AccessError, 'invalid_command_syntax'):
                access.parse(bad_cmd, queries, 1795000000)
        for bad_val in ('abc', '', '1_000', ' 42\n', 'NaN', 'Inf', '١٢٣'):
            payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {}, 'values': [[100, bad_val]]}]}}
            with self.subTest(bad_val=bad_val), self.assertRaisesRegex(access.AccessError, 'invalid_value'):
                access.aggregate_response(payload, 100, 200, 100)
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"status":"success","warnings":["err"],"warnings":[]}')
        with self.assertRaisesRegex(access.AccessError, 'duplicate_json_key'):
            access.query_prometheus('sum(up)', 100, 200, 100, opener=opener)
        with tempfile.TemporaryDirectory() as tmp:
            dup = Path(tmp) / 'dup.json'
            dup.write_text('{"a":1,"a":2}')
            with self.assertRaisesRegex(access.AccessError, 'duplicate_json_key'):
                access.load_json(dup)
            nested = Path(tmp) / 'deep.json'
            nested.write_text('[' * 1500 + '1' + ']' * 1500)
            with self.assertRaisesRegex(access.AccessError, 'json_nesting_limit'):
                access.load_json(nested)
        self.assertEqual(access.strict_json(codecs.BOM_UTF16_LE + '{"ok":true}'.encode('utf-16-le')), {'ok': True})
        for bad_raw in (b'"' + b'\\"' * 500_000, b'"' + b'\\"' * 500_000 + b'\\'):
            with self.assertRaisesRegex(access.AccessError, 'json_nesting_limit|invalid_json'):
                access.strict_json(bad_raw)

    def test_fixed_ids_bounds_and_retired_commands(self):
        queries = access.load_json(ROOT / 'catalogue/queries.json')
        self.assertEqual(access.parse('queries', queries, 1795000000), ('queries',))
        for key in queries:
            self.assertEqual(access.parse(f'prom {key} 1794873600 1794960000 86400', queries, 1795000000)[1], key)
        for step, begin, end in (
            (3600, 1794873600, 1794960000),
            (21600, 1794873600, 1794960000),
            (86400, 1794355200, 1794960000),
        ):
            self.assertEqual(access.parse(f'prom transport_rejections_daily_value {begin} {end} {step}', queries, 1795000000)[2:],
                             (begin, end, step))
        for command, pattern in (
            ('sh', 'command_not_allowed'),
            ('cat /etc/shadow', 'command_not_allowed'),
            ('gateway', 'command_not_allowed'),
            ('snapshot-index', 'command_not_allowed'),
            ('snapshot 20261118T040000Z-aabbccdd manifest.json', 'command_not_allowed'),
            ('snapshot ../../etc passwd', 'command_not_allowed'),
            ('snapshot 20261118T040000Z-aabbccdd ../../etc/shadow', 'command_not_allowed'),
            ('prom sum(up) 1794873600 1794960000 86400', 'invalid_query_request'),
            ('prom transport_rejections_daily_value 1794873600 1794960000 1', 'unaligned_range'),
            ('prom transport_rejections_daily_value 1794790800 1794877200 21600', 'unaligned_range'),
            ('prom transport_rejections_daily_value 1794790800 1794877200 86400', 'unaligned_range'),
            ('prom transport_rejections_daily_value 1794960000 1795046400 86400', 'invalid_time_range'),
            ('prom transport_rejections_daily_value 1794999600 1795003200 3600', 'invalid_time_range'),
            ('prom transport_rejections_daily_value 1794182400 1794960000 86400', 'range_limit'),
            ('prom transport_rejections_daily_value 1794873600 9999999999 86400', 'invalid_time_range'),
            ('prom transport_rejections_daily_value 1794873600 1794960000 86400; id', 'command_not_allowed'),
        ):
            with self.subTest(command=command), self.assertRaisesRegex(access.AccessError, pattern):
                access.parse(command, queries, 1795000000)
        for retired in ('SnapshotDurabilityError', 'open_lock', 'snapshot_manifest', 'online_backup', 'make_snapshot', 'memory_index', 'gateway'):
            self.assertFalse(hasattr(access, retired), retired)

    def test_response_label_and_shape_boundary(self):
        clean = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'type': 'test'}, 'values': [[100, '1'], [200, '2']]}]}}
        self.assertEqual(access.aggregate_response(clean, 100, 200, 100), clean)
        for payload in ([], {'status': 'error'}, {'status': 'success', 'data': []},
                        {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}):
            with self.assertRaises(ValueError):
                access.aggregate_response(payload, 100, 200, 100)
        for extra in ({'instance': 'private'}, {'host': 'private'}, {'fingerprint': 'private'}, {'previous_hop': '1'}, {'__name__': 'raw'}):
            for labels in (extra, {'type': 'test', **extra}):
                payload = json.loads(json.dumps(clean))
                payload['data']['result'][0]['metric'] = labels
                with self.assertRaises(ValueError):
                    access.aggregate_response(payload, 100, 200, 100)
        for points in ([[100, 'NaN']], [[99, '1']], [[200, '1'], [100, '1']], [[100, '1']] * 170):
            payload = json.loads(json.dumps(clean))
            payload['data']['result'][0]['values'] = points
            with self.assertRaises(ValueError):
                access.aggregate_response(payload, 100, 200, 100)

    def test_response_grid_allows_gaps_and_rejects_duplicate_series(self):
        payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {'type': 'test', 'action': 'count'}, 'values': [[100, '1'], [300, '2']]}]}}
        self.assertEqual(access.aggregate_response(payload, 100, 300, 100), payload)
        off_grid = json.loads(json.dumps(payload))
        off_grid['data']['result'][0]['values'][1][0] = 250
        with self.assertRaisesRegex(ValueError, 'off_grid_timestamp'):
            access.aggregate_response(off_grid, 100, 300, 100)
        payload['data']['result'].append({'metric': {'action': 'count', 'type': 'test'}, 'values': [[200, '3']]})
        with self.assertRaisesRegex(ValueError, 'duplicate_series'):
            access.aggregate_response(payload, 100, 300, 100)

    def test_oversized_timestamp_returns_fixed_error(self):
        for timestamp in (10**400, -(10**400)):
            payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {}, 'values': [[timestamp, '1']]}]}}
            opener = mock.Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with self.subTest(sign=timestamp > 0):
                with self.assertRaises(access.AccessError) as raised:
                    access.query_prometheus('sum(up)', 100, 200, 100, opener=opener)
                self.assertEqual(str(raised.exception), 'invalid_timestamp')

    def test_query_warnings_cannot_be_published_as_clean_success(self):
        clean = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {}, 'values': [[100, '1']]}]}}
        for warnings in (['partial data from private-instance'], 'unexpected format', None):
            payload = dict(clean, warnings=warnings)
            opener = mock.Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with self.subTest(warnings=warnings):
                with self.assertRaisesRegex(access.AccessError, 'query_warnings') as raised:
                    access.query_prometheus('sum(up)', 100, 200, 100, opener=opener)
                self.assertEqual(str(raised.exception), 'query_warnings')
        self.assertEqual(access.aggregate_response(dict(clean, warnings=[]), 100, 200, 100), clean)
        self.assertEqual(access.aggregate_response(dict(clean, infos=['informational']), 100, 200, 100), clean)

    def test_non_regular_reads_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'original').write_text('private')
            (p / 'link').symlink_to(p / 'original')
            with self.assertRaises(OSError):
                access.regular_read(p / 'link')
            probe_file = p / 'probe'
            probe_file.write_text('ok')
            with probe_file.open('rb') as owned:
                probe_before = os.dup(owned.fileno())
                os.close(probe_before)
                with self.assertRaises(access.AccessError):
                    access.regular_read(p)
                probe_after = os.dup(owned.fileno())
                os.close(probe_after)
                self.assertEqual(probe_after, probe_before)

    def test_oversized_int_string_and_huge_matrix_timestamp_fail_closed(self):
        with self.assertRaisesRegex(access.AccessError, 'invalid_json'):
            access.strict_json('1' * 5000)
        payload = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': {}, 'values': [[100, '1']]}]}}
        with self.assertRaisesRegex(access.AccessError, 'invalid_timestamp'):
            access.aggregate_response({'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {}, 'values': [[10**400, '1']]}]}}, 0, 10**401, 1)

    def test_depth_check_ignores_brackets_inside_strings_without_swallowing_quotes(self):
        for raw in (
            '{"a": "' + '[' * 70 + '"}',
            '[1, "' + '{' * 70 + '"]',
            ('[1, "' + '{' * 70 + '"]').encode('utf-16-le'),
        ):
            with self.subTest(raw=type(raw).__name__):
                self.assertEqual(access.strict_json(raw), json.loads(raw))
        deep_masked = '{"a": "' + ']' * 100 + '", "b": ' + '[' * 120 + '1' + ']' * 120 + '}'
        with self.assertRaisesRegex(access.AccessError, 'json_nesting_limit'):
            access.strict_json(deep_masked)

        large_valid = json.dumps({'ok': 'a' * (4 * 1024 * 1024)})
        tracemalloc.start()
        try:
            self.assertTrue(access._check_json_depth(large_valid))
            _, depth_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(depth_peak, 64 * 1024)
        self.assertEqual(len(access.strict_json(large_valid)['ok']), 4 * 1024 * 1024)

    def test_build_catalogue_preserves_umask_permissions_for_fresh_files(self):
        import shutil
        import stat
        with tempfile.TemporaryDirectory() as folder:
            fake_root = Path(folder)
            (fake_root / 'catalogue').mkdir()
            shutil.copy2(ROOT / 'catalogue/contract.json', fake_root / 'catalogue/contract.json')
            old_umask = os.umask(0o022)
            try:
                with mock.patch.object(builder, 'ROOT', fake_root), \
                        mock.patch.object(sys, 'argv', ['build_catalogue.py']), \
                        mock.patch('sys.stdout', new=io.StringIO()):
                    builder.main()
            finally:
                os.umask(old_umask)
            self.assertEqual(stat.S_IMODE((fake_root / 'catalogue/queries.json').lstat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((fake_root / 'catalogue/routes.json').lstat().st_mode), 0o644)


if __name__ == '__main__':
    unittest.main()
