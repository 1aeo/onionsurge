import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import query_protocol as access
import query_gateway as gateway

END = 1799971200
BEGIN = END - gateway.HOUR


def attestations():
    return {
        'selectors': dict(zip(('pid', 'start', 'health', 'source_time', 'info'),
                              ('private_pid', 'private_start', 'private_health', 'private_source_time', 'private_info'))),
        'groups': {
            'daemon_group_a': {'version': '0.4.9.17', 'proof_observed_at': END - 600, 'proof_sha256': 'a' * 64},
            'daemon_group_b': {'version': '0.4.9.14', 'proof_observed_at': END - 7200, 'proof_sha256': 'b' * 64},
        },
        'records': [
            {'source': 'PRIVATE_SOURCE_A', 'unit': 'PRIVATE_UNIT_A', 'pid': 101,
             'start_time_seconds': END - 2 * gateway.HOUR, 'metrics_instance': 'PRIVATE_TARGET_A',
             'public_group': 'daemon_group_a', 'version': '0.4.9.17'},
            {'source': 'PRIVATE_SOURCE_B', 'unit': 'PRIVATE_UNIT_B', 'pid': 102,
             'start_time_seconds': END - 3 * gateway.HOUR, 'metrics_instance': 'PRIVATE_TARGET_B',
             'public_group': 'daemon_group_b', 'version': '0.4.9.14'},
        ],
    }


def fixture_fetch(document):
    def fetch(query, begin, end, port):
        points = list(range(begin, end + 1, gateway.HOUR))
        rows = []
        for record in document['records']:
            if query in document['selectors'].values():
                name = next(k for k, value in document['selectors'].items() if value == query)
                labels = {'instance': record['source']}
                if name in ('pid', 'start', 'info'):
                    labels['unit'] = record['unit']
                if name == 'info':
                    labels['tor_metrics_instance'] = record['metrics_instance']
                values = {'pid': record['pid'], 'start': record['start_time_seconds'], 'health': 1, 'info': 1}
                rows.append({'metric': labels, 'values': [[t, str(t - 30 if name == 'source_time' else values[name])] for t in points]})
            elif query == gateway.COHORTS['native'] or (query in (gateway.COHORTS['directory_defense'], gateway.COHORTS['custom']) and record['public_group'] == 'daemon_group_a'):
                rows.append({'metric': {'instance': record['metrics_instance']}, 'values': [[t, '1'] for t in points]})
        return rows
    return fetch


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class QueryGatewayTests(unittest.TestCase):
    def config(self, folder, evidence=False):
        root = Path(folder).resolve()
        config, state = root / 'config', root / 'state'
        config.mkdir(mode=0o700); state.mkdir(mode=0o700)
        write_private(config / 'queries.json', {'test': 'sum(up)'})
        settings = {'prometheus_port': 9090}
        if evidence:
            settings['attestations_file'] = str(root / 'attestations.json')
            write_private(root / 'attestations.json', attestations())
        write_private(config / 'gateway.json', settings)
        return config, state

    def test_only_fixed_query_commands_and_bounded_virtual_ranges(self):
        queries = dict(gateway.VIRTUAL_QUERIES, test='sum(up)')
        self.assertEqual(gateway.parse('queries', queries, END + 60), ('queries',))
        for command in ['snapshot-index', 'snapshot 20261118T040000Z-aabbccdd manifest.json',
                        'cat /private/file', 'sh', 'prom sum(up) 0 3600 3600',
                        f'prom test {BEGIN} {END} 3600; id',
                        f'prom test {BEGIN} {END} 86400',
                        f'prom test {BEGIN + 3600} {END + 3600} 86400',
                        f'prom attested_native_runtime {END - 86400} {END} 86400',
                        f'prom attested_native_runtime {BEGIN - 3600} {END} 3600']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                gateway.parse(command, queries, END + 60)
        with self.assertRaises(access.AccessError):
            access.validate_catalogue({'bad_hop': 'sum by(previous_hop)(up)'})
        self.assertEqual(gateway.parse(f'prom attested_native_runtime {BEGIN} {END} 3600', queries, END + 60)[1], 'attested_native_runtime')
        self.assertEqual(gateway.parse(f'prom test {BEGIN} {END} 3600', queries, END + 60)[1], 'test')
        self.assertEqual(gateway.parse(f'prom test {END - 7 * 86400} {END} 86400', queries, END + 60)[1], 'test')

    def test_public_catalogue_never_loads_or_returns_private_attestations(self):
        with tempfile.TemporaryDirectory() as folder:
            config, state = self.config(folder, evidence=True)
            with mock.patch.object(gateway, 'validate_attestations', side_effect=AssertionError('private evidence read')):
                result = gateway.serve(config, state, 'queries', END + 60)
            self.assertEqual(result, dict(gateway.VIRTUAL_QUERIES, test='sum(up)'))
            self.assertNotIn('PRIVATE_', json.dumps(result))
            write_private(config / 'queries.json', dict(gateway.VIRTUAL_QUERIES, attested_native_runtime='sum(up)'))
            with self.assertRaisesRegex(ValueError, 'descriptor_mismatch'):
                gateway.serve(config, state, 'queries', END + 60)

    def test_ordinary_query_reuses_reviewed_http_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            config, state = self.config(folder)
            expected = {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
            with mock.patch.object(access, 'query_prometheus', return_value=expected) as query:
                self.assertEqual(gateway.serve(config, state, f'prom test {BEGIN} {END} 3600', END + 60), expected)
                query.assert_called_once_with('sum(up)', BEGIN, END, 3600, 9090)
            self.assertEqual(gateway.serve(config, state, f'prom attested_native_runtime {BEGIN} {END} 3600', END + 60), expected)

    def test_managed_overlap_expression_matches_the_verified_catalogue(self):
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        for name in ['native', 'custom', 'directory_defense']:
            self.assertIn(gateway.MANAGED_RUNTIME, queries[f'cohort_{name}_verified_binary'])
        for name in ['native', 'directory_defense']:
            self.assertIn(gateway.STOCK_CONFIG_RUNTIME, queries[f'cohort_{name}_stock_config'])

    def test_virtual_serve_uses_private_evidence_without_returning_it(self):
        with tempfile.TemporaryDirectory() as folder:
            config, state = self.config(folder, evidence=True)
            with mock.patch.object(gateway, 'private_query', side_effect=fixture_fetch(attestations())):
                result = gateway.serve(config, state, f'prom attested_native_runtime {BEGIN} {END} 3600', END + 60)
            self.assertEqual(len(result['data']['result']), 2)
            self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_private_config_permissions_and_single_query_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            config, state = self.config(folder)
            (config / 'queries.json').chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private_config'):
                gateway.serve(config, state, 'queries', END + 60)
            (config / 'queries.json').chmod(0o600)
            with gateway.query_lock(state):
                with self.assertRaisesRegex(ValueError, 'query_busy'):
                    with gateway.query_lock(state):
                        self.fail('concurrent query accepted')
            alias = Path(folder) / 'alias'; alias.symlink_to(config, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'private_directory'):
                gateway.private_directory(alias)

    def test_matched_cohorts_and_actual_proof_times_contain_no_private_labels(self):
        document = gateway.validate_attestations(attestations(), END + 60)
        result = gateway.attested_responses(document, 'c' * 64, BEGIN, END, 9090, fixture_fetch(document))
        native = result['attested_native_runtime']['data']['result']
        self.assertEqual({r['metric']['build_id']: r['values'][-1][1] for r in native},
                         {'attested-0_4_9_14': '1', 'attested-0_4_9_17': '1'})
        self.assertEqual(len(result['attested_directory_defense_runtime']['data']['result']), 1)
        self.assertEqual(len(result['attested_custom_runtime']['data']['result']), 1)
        metadata = {r['metric']['type']: r for r in result['attestation_proof_metadata']['data']['result']}
        self.assertEqual(float(metadata['daemon_group_a']['values'][-1][1]), END - 600)
        self.assertEqual(metadata['daemon_group_a']['metric']['build_id'], 'attested-0_4_9_17')
        self.assertEqual(float(metadata['daemon_group_b']['values'][-1][1]), END - 7200)
        self.assertEqual(metadata['continuity']['metric']['sha256'], 'c' * 64)
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_restart_missing_stale_and_managed_members_become_unrecorded(self):
        for failure in ['pid', 'start', 'info', 'health', 'source_time', 'stale', 'missing', 'managed', 'stock_config']:
            document = attestations(); real = fixture_fetch(document)
            def fetch(query, begin, end, port):
                rows = real(query, begin, end, port)
                if failure == 'managed' and query == gateway.MANAGED_RUNTIME:
                    return [{'metric': {'instance': 'PRIVATE_TARGET_A'}, 'values': [[t, '1'] for t in (BEGIN, END)]}]
                if failure == 'stock_config' and query == gateway.STOCK_CONFIG_RUNTIME:
                    return [{'metric': {'instance': 'PRIVATE_TARGET_A'}, 'values': [[t, '1'] for t in (BEGIN, END)]}]
                selected = document['selectors'].get('source_time' if failure == 'stale' else failure)
                if query == selected:
                    if failure == 'info':
                        rows[0]['metric']['tor_metrics_instance'] = 'CHANGED_PRIVATE_TARGET'
                    else:
                        rows[0]['values'][-1][1] = str(END + 1 if failure == 'source_time' else END - 180 if failure == 'stale' else 0)
                if failure == 'missing' and query == document['selectors']['start']:
                    rows = rows[1:]
                return rows
            with self.subTest(failure=failure):
                result = gateway.attested_responses(document, 'c' * 64, BEGIN, END, 9090, fetch)
                end_counts = {r['metric']['build_id']: dict(r['values']).get(END) for r in result['attested_native_runtime']['data']['result']}
                self.assertIsNone(end_counts.get('attested-0_4_9_17'))
                self.assertEqual(end_counts['attested-0_4_9_14'], '1')
                proofs = {r['metric']['type']: dict(r['values']).get(END) for r in result['attestation_proof_metadata']['data']['result']}
                self.assertIsNone(proofs.get('daemon_group_a'))

        document = attestations(); real = fixture_fetch(document)
        def all_stock(query, begin, end, port):
            if query == gateway.STOCK_CONFIG_RUNTIME:
                return [{'metric': {'instance': name}, 'values': [[t, '1'] for t in (BEGIN, END)]}
                        for name in ('PRIVATE_TARGET_A', 'PRIVATE_TARGET_B')]
            return real(query, begin, end, port)
        covered = gateway.attested_responses(document, 'c' * 64, BEGIN, END, 9090, all_stock)
        self.assertEqual(covered['attested_native_runtime']['data']['result'], [])
        self.assertEqual(covered['attested_directory_defense_runtime']['data']['result'], [])
        self.assertEqual(covered['attested_custom_runtime']['data']['result'], [])

    def test_bad_attestation_versions_proof_epochs_and_duplicate_targets_fail_closed(self):
        for change in [lambda d: d['records'][0].update(version='PRIVATE_MARKER'),
                       lambda d: d['groups']['daemon_group_a'].update(proof_observed_at=END + 100),
                       lambda d: d['groups']['daemon_group_a'].update(proof_observed_at=-0.0),
                       lambda d: d['groups']['daemon_group_a'].update(proof_sha256='invalid'),
                       lambda d: d['records'][0].update(start_time_seconds=END + 1),
                       lambda d: d['records'][0].update(start_time_seconds=-0.0),
                       lambda d: d['records'].append(copy.deepcopy(d['records'][0]))]:
            document = attestations(); change(document)
            with self.subTest(change=change), self.assertRaises(ValueError):
                gateway.validate_attestations(document, END + 60)

    def test_private_http_boundary_rejects_warnings_duplicates_and_nonfinite_points(self):
        good = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [{'metric': {'instance': 'PRIVATE_SOURCE'}, 'values': [[BEGIN, '1'], [END, '2']]}]}}
        self.assertEqual(gateway.private_matrix(good, BEGIN, END), good['data']['result'])
        for warning in [None, {}, ['PRIVATE_WARNING']]:
            bad = copy.deepcopy(good); bad['warnings'] = warning
            with self.assertRaisesRegex(ValueError, 'private_query_warnings'):
                gateway.private_matrix(bad, BEGIN, END)
        for point in [[BEGIN + 1, '1'], [BEGIN, 'NaN'], [BEGIN, '1_000'], [True, '1']]:
            bad = copy.deepcopy(good); bad['data']['result'][0]['values'] = [point]
            with self.assertRaises(ValueError):
                gateway.private_matrix(bad, BEGIN, END)
        with self.assertRaisesRegex(ValueError, 'duplicate_private_identity'):
            gateway.indexed(good['data']['result'] * 2, ('instance',))

    def test_private_json_depth_and_fifo_lock_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            nested = root / 'deep.json'
            nested.write_text('[' * 100 + '1' + ']' * 100)
            nested.chmod(0o600)
            with self.assertRaisesRegex(access.AccessError, 'json_nesting_limit'):
                gateway.private_json(nested)
            fifo = root / 'query.lock'
            os.mkfifo(fifo)
            with self.assertRaisesRegex(access.AccessError, 'invalid_lock'):
                with gateway.query_lock(root):
                    pass

    def test_forced_command_works_with_isolated_python(self):
        with tempfile.TemporaryDirectory() as folder:
            config, state = self.config(folder)
            result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/query_gateway.py'), '--config-dir', str(config), '--state-dir', str(state)],
                                    env=dict(os.environ, SSH_ORIGINAL_COMMAND='queries'), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('attested_native_runtime', json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
