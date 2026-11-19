from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('collector', ROOT / 'scripts/collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
DAY = collector.DAY
END = 20000 * DAY


class SyntheticSource:
    identity = 'synthetic-source-v1'

    def __init__(self, queries, handlers):
        self.queries, self.handlers = queries, handlers
        self.calls = []
        self.transform = lambda response: response

    @property
    def requests(self):
        return len(self.calls)

    def catalogue(self):
        return self.queries

    def query(self, key, begin, end, step):
        self.calls.append((key, begin, end, step))
        rows = {}
        for t in range(begin, end + 1, step):
            for labels, value in self.handlers[key](t):
                identity = tuple(sorted(labels.items()))
                rows.setdefault(identity, []).append([t, str(value)])
        response = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
            {'metric': dict(labels), 'values': values} for labels, values in rows.items()]}}
        return self.transform(response)


def seeded_chart(chart_id, cohort, keys, population):
    def series(key, historical=True):
        return {
            'key': key,
            'history_status': 'queried' if historical else 'not_queried',
            'current': {'day': {
                'value': 2.0 if historical else None,
                'relays': population if historical else None,
                'expected_relays': population,
                'coverage': 1.0 if historical else None,
                'valid': historical,
            }},
            'history': {'daily': [[END - 2 * DAY, 2.0, population, 1.0, True]] if historical else []},
        }
    return {'id': chart_id, 'cohort_key': cohort, 'kind': 'counter_daily_rate', 'relays': population,
            'query': 'original-current-query', 'quality_query': 'original-quality-query',
            'series': [series(key, key != 'circuit_dead') for key in keys]}


class CollectorFixture:
    def __init__(self):
        self.queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        self.routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        self.seed = {'input_schema_version': 3, 'end': END - 2 * DAY, 'start': END - 3 * DAY,
                     'window_seconds': DAY, 'minimum_samples': 360,
                     'fleet': 80, 'custom': 32, 'defense': 48,
                     'charts': [seeded_chart('circuit_rejections', 'native', ['circuit_rejections'], 80),
                                seeded_chart('hsdir_fetches', 'custom', ['success'], 32),
                                seeded_chart('directory_rejections', 'directory_defense', ['begindir_rejected'], 48),
                                seeded_chart('directory_requests', 'custom', ['hs_get', 'circuit_dead'], 32)],
                     'versions': {'verified_runtime': [
                         {'build_id': 'stock.15', 'version': '0.4.9.15', 'base_version': '0.4.9.15', 'patched': False, 'relays': 48},
                         {'build_id': 'tor41339.9', 'version': '0.4.9.15 + tor41339.9', 'base_version': '0.4.9.15', 'patched': True, 'relays': 16}],
                         'binary_hash_counts': [
                             {'metric': {'build_id': 'stock.15', 'sha256': 'a' * 64}, 'value': [END - 2 * DAY, '48']},
                             {'metric': {'build_id': 'tor41339.9', 'sha256': 'b' * 64}, 'value': [END - 2 * DAY, '16']},
                         ]},
                     'sources': {'feed': 'synthetic', 'collector': 'synthetic', 'current_observation_end': END - 2 * DAY},
                     'history_methods': {'coarsening': collector.COARSENING_DESCRIPTION},
                     'dircache_cohort': {'method': 'synthetic aggregate', 'source': 'synthetic aggregate'}}
        self.attested = True
        handlers = {}
        for chart in self.seed['charts']:
            for statistic in ['value', 'eligible', 'available']:
                query_id = self.routes[f'{chart["id"]}.daily.{statistic}']
                def data(t, chart=chart, statistic=statistic):
                    rows = []
                    for series in chart['series']:
                        if series['key'] == 'circuit_dead':
                            continue
                        value = chart['relays']
                        if statistic == 'value':
                            value = 77 if t == END else 2
                        elif statistic == 'eligible' and chart['id'] == 'circuit_rejections' and t == END - DAY:
                            value = 64
                        labels = ({'type': 'circuit_rejected'} if chart['id'] == 'circuit_rejections'
                                  else {'action': series['key']} if chart['id'] == 'hsdir_fetches'
                                  else {'type': series['key']})
                        rows.append((labels, value))
                    return rows
                handlers.setdefault(query_id, data)
        for cohort in ['native', 'custom', 'directory_defense']:
            managed = [('stock.15', 'a' * 64, 48), ('UNKNOWN_BUILD', 'd' * 64, 1)] if cohort == 'native' else [
                ('tor41339.9', 'b' * 64, 16)] if cohort == 'custom' else [('stock.15', 'a' * 64, 24)]
            handlers[self.routes[f'cohort.{cohort}_verified_binary']] = lambda t, managed=managed: [
                ({'build_id': build, 'sha256': sha}, n) for build, sha, n in managed]
            handlers[self.routes[f'cohort.{cohort}_attested']] = lambda t, cohort=cohort: [
                ({'build_id': 'attested-0_4_9_17'}, 8)] if self.attested and cohort != 'custom' else []
        handlers[self.routes['version.attestation_proof_metadata']] = lambda t: [
            ({'type': 'daemon_group_a', 'build_id': 'attested-0_4_9_17', 'sha256': 'c' * 64}, END - 600),
            ({'type': 'continuity', 'sha256': 'e' * 64}, t)] if self.attested else []
        handlers[self.routes['metadata.flags']] = lambda t: [({'type': 'Running'}, 80)]
        for name in ['targets', 'up']:
            handlers[self.routes['metadata.' + name]] = lambda t: [({}, 80)]
        for name in ['combined_zero_nonexit', 'observed_zero', 'file_fallback_zero', 'file0_observed1_conflicts']:
            handlers[self.routes['dircache.' + name]] = lambda t: [({}, 0)]
        for key in {self.routes[f'{chart["id"]}.relay_population.day'] for chart in self.seed['charts']}:
            def roles(t, key=key):
                rows = []
                for chart in self.seed['charts']:
                    if self.routes[f'{chart["id"]}.relay_population.day'] != key:
                        continue
                    for series in chart['series']:
                        metric_type = ('circuit_rejected' if chart['id'] == 'circuit_rejections'
                                       else series['key'])
                        n = 64 if chart['id'] == 'circuit_rejections' and t == END - DAY else chart['relays']
                        rows.append(({'type': metric_type, 'action': '59'}, n))
                return rows
            handlers[key] = roles
        self.source = SyntheticSource(self.queries, handlers)

    def instance(self, path):
        cache = collector.ObservationCache(path, self.source, self.queries)
        return collector.Collector(self.source, cache, self.queries, self.routes, self.seed), cache


class SourceConfigAndCliTests(unittest.TestCase):
    def test_load_source_config_enforces_private_regular_files_and_strict_env_syntax(self):
        valid = '\n'.join([
            '# private comment',
            'SOURCE_SSH_HOST="source.example"',
            'SOURCE_SSH_USER="onionsurge-read"',
            'SOURCE_SSH_PORT="22"',
            'SOURCE_SSH_IDENTITY_FILE="${HOME}/.ssh/onionsurge_reader"',
            'SOURCE_SSH_KNOWN_HOSTS_FILE="$HOME/.ssh/onionsurge_known_hosts"',
        ]) + '\n'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg = root / 'source.env'
            cfg.write_text(valid)
            cfg.chmod(0o644)
            with self.assertRaisesRegex(collector.CollectionError, 'mode 0600'):
                collector.load_source_config(cfg)
            cfg.chmod(0o600)
            loaded = collector.load_source_config(cfg)
            self.assertEqual(loaded['SOURCE_SSH_HOST'], 'source.example')
            self.assertEqual(collector.expand_path(loaded['SOURCE_SSH_IDENTITY_FILE']),
                             Path.home() / '.ssh/onionsurge_reader')

            link = root / 'link.env'
            link.symlink_to(cfg)
            with self.assertRaises(OSError):
                collector.load_source_config(link)

            dup = root / 'dup.env'
            dup.write_text(valid + 'SOURCE_SSH_HOST="b"\n')
            dup.chmod(0o600)
            with self.assertRaisesRegex(collector.CollectionError, 'Duplicate configuration key'):
                collector.load_source_config(dup)

            for bad_line, pattern in (
                ('$(id)', 'only KEY=value assignments'),
                ('SOURCE_SSH_HOST=one two', 'Quote spaces'),
                ('SOURCE_SSH_HOST="unterminated', 'Invalid quoting'),
                ('SOURCE_SSH_HOST="bad\x01val"', 'Control character'),
            ):
                bad = root / 'bad.env'
                bad.write_text(valid.replace('SOURCE_SSH_HOST="source.example"', bad_line, 1))
                bad.chmod(0o600)
                with self.subTest(bad_line=bad_line), self.assertRaisesRegex(collector.CollectionError, pattern):
                    collector.load_source_config(bad)

            incomplete = root / 'incomplete.env'
            incomplete.write_text('SOURCE_SSH_HOST="source.example"\n')
            incomplete.chmod(0o600)
            with self.assertRaisesRegex(collector.CollectionError, 'Missing configuration keys'):
                collector.load_source_config(incomplete)

        with self.assertRaisesRegex(collector.CollectionError, 'absolute or home-relative'):
            collector.expand_path('relative/path.env')

    def test_cli_optional_application_dir_defaults_to_repo_root_and_rejects_bad_config_silently(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg = root / 'source.env'
            cfg.write_text('\n'.join([
                'SOURCE_SSH_HOST="secret-host.internal"',
                'SOURCE_SSH_USER="onionsurge-read"',
                'SOURCE_SSH_PORT="22"',
                'SOURCE_SSH_IDENTITY_FILE="${HOME}/.ssh/onionsurge_reader"',
                'SOURCE_SSH_KNOWN_HOSTS_FILE="${HOME}/.ssh/onionsurge_known_hosts"',
            ]) + '\n')
            cfg.chmod(0o600)
            summary = {'snapshot_end': END, 'charts': 4, 'series': 5, 'source_requests': 1}
            stdout = io.StringIO()
            with mock.patch.object(collector, 'collect', return_value=summary) as mocked, redirect_stdout(stdout):
                collector.main([
                    '--config', str(cfg),
                    '--state-dir', str(root / 'state'),
                    '--output', str(root / 'out.json'),
                    '--end', str(END),
                ])
            self.assertEqual(mocked.call_args.args[1], collector.ROOT)
            self.assertEqual(json.loads(stdout.getvalue()), summary)

            cfg.chmod(0o644)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exited:
                    collector.main([
                        '--config', str(cfg),
                        '--state-dir', str(root / 'state'),
                        '--output', str(root / 'out.json'),
                    ])
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(stderr.getvalue(), 'Collection failed; previous snapshot retained.\n')
            self.assertNotIn('secret-host.internal', stderr.getvalue())


class ObservationCacheTests(unittest.TestCase):
    def test_population_backfill_uses_small_contiguous_batches_and_only_requested_days(self):
        key = 'relay_population_synthetic_day'
        times = [END + i * DAY for i in (0, 1, 2, 3, 5, 6, 7, 8)]
        source = SyntheticSource({key: 'sum(metric)'}, {key: lambda t: [({'type': 'probe', 'action': '1'}, 2)]})
        with tempfile.TemporaryDirectory() as folder:
            cache = collector.ObservationCache(Path(folder) / 'cache.sqlite3', source, source.queries)
            try:
                self.assertEqual(set(cache.points(key, DAY, times)), set(times))
                self.assertTrue(all((end - begin) // step + 1 <= 3 for _, begin, end, step in source.calls))
                self.assertEqual([call[1:] for call in source.calls],
                                 [(times[0], times[2], DAY), (times[3] - DAY, times[3], DAY),
                                  (times[4], times[6], DAY), (times[7] - DAY, times[7], DAY)])
                before = source.requests
                cache.points(key, DAY, times)
                self.assertEqual(source.requests, before)
            finally:
                cache.close()

    def test_bounded_batches_cache_absence_separately_from_zero_and_reuse_results(self):
        times = [END - i * DAY for i in range(18, -1, -1)]
        missing, zero = times[2:4]
        source = SyntheticSource({'a': 'sum(metric_a)'}, {'a': lambda t: [] if t == missing else [({}, 0 if t == zero else 7)]})
        with tempfile.TemporaryDirectory() as folder:
            cache = collector.ObservationCache(Path(folder) / 'cache.sqlite3', source, source.queries)
            try:
                result = cache.points('a', DAY, times)
                self.assertEqual(len(source.calls), 3)
                self.assertIsNone(collector.selected(result[missing], {}))
                self.assertEqual(collector.selected(result[zero], {}), 0)
                for _, begin, end, step in source.calls:
                    self.assertLessEqual(end - begin, collector.access.MAX_RANGE)
                    self.assertLessEqual((end - begin) // step + 1, collector.access.MAX_POINTS)
                before = source.requests
                self.assertEqual(cache.points('a', DAY, times), result)
                self.assertEqual(source.requests, before)
            finally:
                cache.close()

    def test_memory_cache_dedup_sparse_chunks_and_rollback_clear(self):
        sparse = [END - i * 3 * DAY for i in range(600)]
        source = SyntheticSource({'a': 'sum(metric_a)'}, {'a': lambda t: [({}, t // DAY)]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            cache = collector.ObservationCache(path, source, source.queries)
            try:
                first = cache.points('a', DAY, sparse)
                cache.commit()
            finally:
                cache.close()

            calls_before = len(source.calls)
            cache = collector.ObservationCache(path, source, source.queries)
            try:
                second = cache.points('a', DAY, sparse)
                self.assertEqual(second, first)
                self.assertEqual(len(source.calls), calls_before)
                with mock.patch.object(cache, 'db') as db_mock:
                    third = cache.points('a', DAY, sparse)
                    db_mock.execute.assert_not_called()
                self.assertEqual(third, first)

                pending_day = END + 5 * DAY
                cache.points('a', DAY, [pending_day])
                cache.rollback()
                self.assertNotIn((source.identity, collector.sha256_hex(b'sum(metric_a)'), DAY, pending_day), cache._mem)
                self.assertEqual(cache.db.execute('SELECT count(*) FROM observations WHERE endpoint=?', (pending_day,)).fetchone()[0], 0)
            finally:
                cache.close()

    def test_query_expression_and_source_identity_partition_the_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            for expression, identity, value in [('sum(a)', 'one', 1), ('sum(b)', 'one', 2), ('sum(b)', 'two', 3)]:
                source = SyntheticSource({'a': expression}, {'a': lambda t, value=value: [({}, value)]})
                source.identity = identity
                cache = collector.ObservationCache(path, source, source.queries)
                try:
                    self.assertEqual(collector.selected(cache.points('a', DAY, [END])[END], {}), value)
                    self.assertEqual(source.requests, 1)
                    cache.commit()
                finally:
                    cache.close()

    def test_source_warnings_or_identity_labels_are_not_cached_as_missing_data(self):
        for kind in ['warning', 'identity']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                source = SyntheticSource({'a': 'sum(metric)'}, {'a': lambda t: [({}, 1)]})
                def transform(response):
                    if kind == 'warning':
                        response['warnings'] = ['synthetic warning']
                    else:
                        response['data']['result'][0]['metric'] = {'instance': 'SYNTHETIC_TARGET'}
                    return response
                source.transform = transform
                cache = collector.ObservationCache(Path(folder) / 'cache.sqlite3', source, source.queries)
                try:
                    with self.assertRaises(ValueError):
                        cache.points('a', DAY, [END])
                    self.assertEqual(cache.db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)
                finally:
                    cache.close()


class CollectorTests(unittest.TestCase):
    def test_population_codes_preserve_unknowns_overlapping_roles_and_matching_totals(self):
        def payload(bins):
            return {'data': {'result': [{'metric': {'type': 'probe', 'action': code}, 'values': [[END, str(n)]]}
                                        for code, n in bins]}}
        # Two unclassified, three middle-only, and four Guard+HSDir+V2Dir.
        observed = payload([('0', 2), ('33', 3), ('59', 4)])
        self.assertEqual(collector.population_row(observed, 'probe', END, 9), [END, 9, 7, 4, 3, 0, 4, 4])
        self.assertIsNone(collector.population_row(observed, 'probe', END, 10))
        self.assertIsNone(collector.population_row(payload([]), 'probe', END, 0))
        self.assertEqual(collector.population_row(payload([('0', 0)]), 'probe', END, 0), [END, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(collector.population_row(payload([('0', 9)]), 'probe', END, 9), [END, 9, 0, 0, 0, 0, 0, 0])
        self.assertEqual(collector.population_row(payload([('1', 9)]), 'probe', END, 9), [END, 9, 9, 0, 0, 0, 0, 0])
        for code in ('2', '64', '-1', '1.0', '01', '1e0', 'unknown'):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, 'classification code'):
                collector.population_row(payload([(code, 1)]), 'probe', END, 1)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            collector.population_row(payload([('1', 1), ('1', 1)]), 'probe', END, 2)

    def test_population_collects_matched_windows_and_reuses_backfill(self):
        fixture = CollectorFixture()
        # One retained unqualified day must not be added to the role backfill.
        fixture.seed['charts'][0]['series'][0]['history']['daily'].insert(0, [END // DAY * DAY - 4 * DAY, None, None, None, False])
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
                calls = [call for call in fixture.source.calls if call[0].startswith('relay_population_')]
                worker.collect(END, 'f' * 64)
                self.assertEqual([call for call in fixture.source.calls if call[0].startswith('relay_population_')], calls)
            finally:
                cache.close()
        for chart in data['charts']:
            for series in chart['series']:
                population = series['relay_population']
                self.assertEqual(population['method'], 'eligible-last-flags-v1')
                self.assertNotIn('hour', population)
                self.assertEqual(set(population['queries']), {'day'})
                self.assertEqual(population['queries']['day'], fixture.queries[fixture.routes[f'{chart["id"]}.relay_population.day']])
                self.assertEqual([row[0] for row in population['daily']],
                                 [row[0] for row in series['history']['daily'] if row[4]])
                if series['key'] == 'circuit_dead':
                    self.assertIsNone(population['day'])
                    self.assertEqual(population['daily'], [])
                elif series['current']['day']['relays'] is not None:
                    self.assertEqual(population['day'][1], series['current']['day']['relays'])

    def test_population_mismatch_omits_metadata_without_replacing_measurements(self):
        fixture = CollectorFixture()
        for key in list(fixture.source.handlers):
            if key.startswith('relay_population_'):
                fixture.source.handlers[key] = lambda t: [({'type': 'circuit_rejected', 'action': '27'}, 999)]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
            finally:
                cache.close()
        circuit = data['charts'][0]['series'][0]
        self.assertNotIn('hour', circuit['current'])
        self.assertNotIn('hour', circuit['relay_population'])
        self.assertEqual(circuit['current']['day']['value'], 77.0)
        self.assertIsNone(circuit['relay_population']['day'])
        self.assertEqual(circuit['relay_population']['daily'], [])

    def test_validated_seed_population_reuse_requires_exact_method_queries_and_totals(self):
        fixture = CollectorFixture()
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'initial.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
            finally:
                cache.close()
            chart = data['charts'][0]
            baseline = chart['series'][0]
            for mutation in ('none', 'method', 'day_query', 'total', 'missing'):
                with self.subTest(mutation=mutation):
                    series = copy.deepcopy(baseline)
                    saved = series['relay_population']
                    if mutation == 'method':
                        saved['method'] = 'old-method'
                    elif mutation == 'day_query':
                        saved['queries']['day'] += ' '
                    elif mutation == 'total':
                        saved['daily'][0][1] += 8
                    elif mutation == 'missing':
                        saved['daily'].pop(1)
                    fixture.source.calls.clear()
                    worker, cache = fixture.instance(Path(folder) / (mutation + '.sqlite3'))
                    try:
                        result = worker.relay_population(chart, series, END, {})
                    finally:
                        cache.close()
                    self.assertEqual(result['daily'], baseline['relay_population']['daily'])
                    self.assertEqual([row[0] for row in result['daily']], sorted(row[0] for row in result['daily']))
                    self.assertFalse(any(call[3] != DAY for call in fixture.source.calls))
                    pop_calls = [call for call in fixture.source.calls if call[0].startswith('relay_population_')]
                    self.assertEqual(bool(pop_calls), mutation != 'none')
                    if mutation in ('total', 'missing'):
                        self.assertEqual(len(pop_calls), 1)
                        self.assertLessEqual(pop_calls[0][2] - pop_calls[0][1], DAY)

    def test_rolling_population_uses_strict_31_day_window_and_not_future_values(self):
        eligible = {END - 31 * DAY - DAY: 99, END - 31 * DAY: 20, END - DAY: 8, END: 9, END + DAY: 1000}
        self.assertEqual(collector.populations([END - DAY, END], eligible, {END - DAY: 7, END: 7}), {END - DAY: 20, END: 9})
        self.assertEqual(collector.populations([END], {}, {}), {END: 1})
        self.assertTrue(collector.measurement(0, 10, 10)['valid'])
        self.assertFalse(collector.measurement(None, 10, 10)['valid'])
        self.assertFalse(collector.measurement(7, 8, 10)['valid'])

    def test_current_masks_aliases_strata_direct_day_and_history_append(self):
        fixture = CollectorFixture(); original = copy.deepcopy(fixture.seed)
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
            finally:
                cache.close()
        self.assertEqual(data['input_schema_version'], 3)
        self.assertEqual(data['window_seconds'], DAY)
        self.assertEqual(data['minimum_samples'], 360)
        charts = {chart['id']: chart for chart in data['charts']}
        circuit = charts['circuit_rejections']['series'][0]
        for legacy in ('values', 'hourly_relays', 'hourly_expected_relays'):
            self.assertNotIn(legacy, circuit)
        self.assertEqual(set(circuit['current']), {'day'})
        self.assertEqual(circuit['current']['day']['value'], 77.0)
        self.assertEqual(circuit['history']['daily'][0], original['charts'][0]['series'][0]['history']['daily'][0])
        self.assertEqual(circuit['history']['daily'][-2], [END - DAY, 2.0, 64, 0.8, False])
        self.assertEqual(circuit['history']['daily'][-1], [END, 77.0, 80, 1.0, True])
        self.assertEqual(set(circuit['history']), {'daily'})
        directory = charts['directory_requests']; _, aux_s = directory['series']
        self.assertIsNone(aux_s['current']['day']['value'])
        self.assertEqual(aux_s['history'], {'daily': []})
        expected = fixture.queries[fixture.routes['directory_requests.daily.value']]
        self.assertEqual(directory['query'], expected)
        self.assertIn('== 3', expected)
        self.assertIn('count by(instance,type)', directory['quality_query'])
        used = {key for key, _, _, _ in fixture.source.calls}
        self.assertIn(fixture.routes['directory_requests.daily.eligible'], used)
        self.assertEqual(fixture.seed, original)

    def test_version_counts_use_approved_hashes_and_actual_attestation_times(self):
        fixture = CollectorFixture()
        totals = {'native': 80, 'custom': 32, 'directory_defense': 48}
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                versions = worker.versions(END, totals, totals)
            finally:
                cache.close()
        self.assertEqual(versions['runtime_verified_total'], 56)
        self.assertEqual(versions['runtime_binary_hash_verified_total'], 48)
        self.assertEqual(versions['runtime_unverified_relays'], 24)
        self.assertEqual(versions['metric_cohorts']['custom']['runtime_unverified'], 16)
        self.assertEqual(versions['runtime_resolution']['observed_at'], collector.iso(END - 600))
        self.assertEqual(versions['runtime_resolution']['snapshot_start'], END - DAY)
        self.assertEqual(versions['runtime_resolution']['snapshot_end'], END)
        self.assertNotIn('UNKNOWN_BUILD', json.dumps(versions))
        attested = next(row for row in versions['verified_runtime'] if row['build_id'].startswith('attested-'))
        self.assertNotIn('patched', attested)
        self.assertIn(collector.iso(END - 600), attested['verification'])
        self.assertIn('24-hour UTC day', attested['verification'])
        self.assertEqual(versions['cohort_labels']['patched_0_4_9_15_tor41339_9'],
                         'Patched Tor 0.4.9.15 + tor41339.9')
        for key, label in versions['cohort_labels'].items():
            if key.startswith('patched_'):
                self.assertTrue(label.startswith('Patched Tor 0.4.9.15'), label)
                self.assertIn('tor41339.9', label)
            else:
                self.assertFalse(label.startswith('Patched '), label)

    def test_refreshed_or_revoked_private_proof_is_rechecked_for_the_same_endpoint(self):
        fixture = CollectorFixture(); fixture.attested = False
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                totals = {'native': 80, 'custom': 32, 'directory_defense': 48}
                self.assertEqual(worker.versions(END, totals, totals)['runtime_verified_total'], 48)
                fixture.attested = True
                self.assertEqual(worker.versions(END, totals, totals)['runtime_verified_total'], 56)
                fixture.attested = False
                self.assertEqual(worker.versions(END, totals, totals)['runtime_verified_total'], 48)
            finally:
                cache.close()

    def test_successful_empty_outage_keeps_history_and_positive_expected_populations(self):
        fixture = CollectorFixture(); original = copy.deepcopy(fixture.seed)
        fixture.source.handlers = {key: lambda t: [] for key in fixture.source.handlers}
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
            finally:
                cache.close()
        self.assertEqual((data['fleet'], data['custom'], data['defense']), (0, 0, 0))
        self.assertEqual(data['versions']['runtime_verified_total'], 0)
        self.assertEqual(data['versions']['verified_runtime'], [])
        self.assertNotIn('runtime_resolution', data['versions'])
        for chart, old in zip(data['charts'], original['charts']):
            self.assertEqual(chart['relays'], 0)
            for series, prior in zip(chart['series'], old['series']):
                self.assertNotIn('values', series)
                self.assertNotIn('hour', series['current'])
                self.assertFalse(series['current']['day']['valid'])
                self.assertGreaterEqual(series['current']['day']['expected_relays'], 8)
                self.assertEqual(series['history']['daily'][:len(prior['history']['daily'])], prior['history']['daily'])
                self.assertTrue(all(row[1] is None and row[4] is False
                                    for row in series['history']['daily'][len(prior['history']['daily']):]))

    def test_changed_attestation_file_during_collection_rejects_mixed_provenance(self):
        fixture = CollectorFixture(); calls = 0
        key = fixture.routes['version.attestation_proof_metadata']
        original = fixture.source.handlers[key]
        def changed(t):
            nonlocal calls
            rows = original(t)
            if t == END:
                calls += 1
            if calls > 1:
                rows[-1][0]['sha256'] = 'f' * 64
            return rows
        fixture.source.handlers[key] = changed
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                totals = {'native': 80, 'custom': 32, 'directory_defense': 48}
                with self.assertRaisesRegex(ValueError, 'attestations changed'):
                    worker.versions(END, totals, totals)
            finally:
                cache.close()

    def test_coarsening_policy_immutable_seed_and_overlap_checks(self):
        self.assertEqual(collector.coarsen_count(7), 0)
        self.assertEqual(collector.coarsen_count(8), 8)
        self.assertEqual(collector.coarsen_count(11), 8)
        self.assertEqual(collector.coarsen_count(12), 16)
        self.assertEqual(collector.coarsen_count(1150), 1152)
        self.assertEqual(collector.coarsen_value('open_circuits', 'open_circuits', 'gauge_daily_mean', 4123.4), 4100.0)
        self.assertEqual(collector.coarsen_value('open_circuits', 'open_circuits', 'gauge_daily_mean', 45.0), 0.0)
        self.assertEqual(collector.coarsen_value('hsdir_fetches', 'success', 'counter_daily_rate', 0.0014), 0.001)
        self.assertEqual(collector.coarsen_value('hsdir_fetches', 'success', 'counter_daily_rate', 0.0004), 0.0)
        self.assertEqual(collector.coarsen_value('directory_requests', 'hs_get', 'counter_daily_rate', 0.1234), 0.123)
        self.assertEqual(collector.coarsen_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', 0.00008), 0.0)
        self.assertEqual(collector.coarsen_value('circuit_rejections', 'circuit_rejections', 'counter_daily_rate', 0.0343), 0.0344)

        fixture = CollectorFixture()
        # Preserve an already-coarsened seed value (0.0343) without re-coarsening it to 0.0344.
        fixture.seed['charts'][0]['series'][0]['history']['daily'][0] = [END - 2 * DAY, 0.0343, 80, 1.0, True]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'cache.sqlite3')
            try:
                data = worker.collect(END, 'f' * 64)
            finally:
                cache.close()
        self.assertEqual(data['charts'][0]['series'][0]['history']['daily'][0], [END - 2 * DAY, 0.0343, 80, 1.0, True])

        with tempfile.TemporaryDirectory() as folder:
            app_dir = Path(folder) / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'catalogue').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(fixture.seed))
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(fixture.queries))
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(fixture.routes))
            with self.assertRaisesRegex(collector.CollectionError, '00:00 UTC'):
                collector.collect({}, app_dir, Path(folder) / 'state', Path(folder) / 'out.json',
                                  end=END + 3600, source=fixture.source)
            for protected in (
                collector.ROOT / 'catalogue/queries.json',
                collector.ROOT / 'catalogue/routes.json',
                app_dir / 'catalogue/queries.json',
                app_dir / 'catalogue/routes.json',
                app_dir / 'inputs/snapshot.json',
                Path(folder) / 'state/observations.sqlite3',
                Path(folder) / 'state/collect.lock',
            ):
                target_app = collector.ROOT if collector.ROOT in protected.parents else app_dir
                with self.subTest(protected=str(protected)), self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                    collector.collect({}, target_app, Path(folder) / 'state', protected,
                                      end=END, source=fixture.source)
            unrelated_in_state = Path(folder) / 'state/output-snapshot.json'
            with mock.patch.object(collector, 'validate_snapshot'):
                collector.collect({}, app_dir, Path(folder) / 'state', unrelated_in_state,
                                  end=END, source=fixture.source)
            self.assertTrue(unrelated_in_state.is_file())

    def test_failed_collect_rolls_back_pending_cache_writes_and_keeps_committed_rows(self):
        fixture = CollectorFixture()
        with tempfile.TemporaryDirectory() as folder:
            app_dir = Path(folder) / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(fixture.seed))
            state_dir = Path(folder) / 'state'
            out_path = Path(folder) / 'snapshot.json'
            with mock.patch.object(collector, 'validate_snapshot'):
                collector.collect({}, app_dir, state_dir, out_path, end=END - DAY, source=fixture.source)
            committed_before = out_path.read_text()
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                before_rows = db.execute('SELECT count(*) FROM observations').fetchone()[0]
                before_end_rows = db.execute('SELECT count(*) FROM observations WHERE endpoint=?', (END,)).fetchone()[0]
            self.assertGreater(before_rows, 0)
            self.assertEqual(before_end_rows, 0)

            (app_dir / 'inputs/snapshot.json').write_text(committed_before)
            for mode in ('validation', 'write', 'commit'):
                calls = {'n': 0}
                def validate_output_fail(data):
                    calls['n'] += 1
                    if calls['n'] == 2:
                        raise ValueError('simulated output validation failure')
                with self.subTest(mode=mode):
                    if mode == 'validation':
                        ctx = mock.patch.object(collector, 'validate_snapshot', side_effect=validate_output_fail)
                    else:
                        ctx = mock.patch.object(collector, 'validate_snapshot')
                    with ctx:
                        if mode == 'write':
                            with mock.patch.object(collector, 'atomic_json', side_effect=OSError('disk full')):
                                with self.assertRaisesRegex(OSError, 'disk full'):
                                    collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(out_path.read_text(), committed_before)
                        elif mode == 'commit':
                            stderr = io.StringIO()
                            real_cache_cls = collector.ObservationCache
                            class FailingCommitCache(real_cache_cls):
                                def commit(self):
                                    raise sqlite3.OperationalError('simulated lock')
                            with mock.patch.object(collector, 'ObservationCache', FailingCommitCache), redirect_stderr(stderr):
                                summary = collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(summary['snapshot_end'], END)
                            self.assertEqual(json.loads(out_path.read_text())['end'], END)
                            self.assertIn('observation cache commit failed after snapshot publication', stderr.getvalue())
                            out_path.write_text(committed_before)
                        else:
                            with self.assertRaisesRegex(ValueError, 'simulated output validation failure'):
                                collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(out_path.read_text(), committed_before)
                    with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                        after_rows = db.execute('SELECT count(*) FROM observations').fetchone()[0]
                        after_end_rows = db.execute('SELECT count(*) FROM observations WHERE endpoint=?', (END,)).fetchone()[0]
                    self.assertEqual(after_rows, before_rows)
                    self.assertEqual(after_end_rows, 0)

    def test_chained_collection_preserves_binary_hash_counts(self):
        fixture = CollectorFixture()
        with tempfile.TemporaryDirectory() as folder:
            app_dir = Path(folder) / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(fixture.seed))
            state_dir = Path(folder) / 'state'
            day1_out = Path(folder) / 'day1.json'
            day2_out = Path(folder) / 'day2.json'
            with mock.patch.object(collector, 'validate_snapshot'):
                collector.collect({}, app_dir, state_dir, day1_out, end=END - DAY, source=fixture.source)
                day1_data = json.loads(day1_out.read_text())
                (app_dir / 'inputs/snapshot.json').write_text(json.dumps(day1_data))
                collector.collect({}, app_dir, state_dir, day2_out, end=END, source=fixture.source)
                day2_data = json.loads(day2_out.read_text())
        self.assertEqual(day1_data['versions']['binary_hash_counts'], fixture.seed['versions']['binary_hash_counts'])
        self.assertEqual(day2_data['versions']['binary_hash_counts'], fixture.seed['versions']['binary_hash_counts'])
        self.assertEqual(day2_data['versions']['metric_cohorts']['custom']['patched_0_4_9_15_tor41339_9'], 16)
        by_build = {row['build_id']: row for row in day2_data['versions']['verified_runtime']}
        self.assertEqual(by_build['tor41339.9']['relays'], 0)
        self.assertEqual(by_build['stock.15']['relays'], 48)
        self.assertEqual(day2_data['versions']['runtime_binary_hash_verified_total'], 48)
        self.assertEqual(day2_data['versions']['runtime_verified_total'], 56)
        self.assertEqual(sum(row['relays'] for row in day2_data['versions']['verified_runtime']),
                         day2_data['versions']['runtime_verified_total'])

    def test_population_backfill_skips_mismatched_coarsened_count_and_unifies_versions_partition(self):
        fixture = CollectorFixture()
        # Simulate seed saying 80 relays on END-2*DAY, but fresh source query returns only 8 eligible relays.
        chart = fixture.seed['charts'][0]
        series = copy.deepcopy(chart['series'][0])
        daily_elig_id = fixture.routes['circuit_rejections.daily.eligible']
        orig_handler = fixture.source.handlers[daily_elig_id]
        fixture.source.handlers[daily_elig_id] = lambda t: [({'type': 'circuit_rejected'}, 8)] if t == END - 2 * DAY else orig_handler(t)
        pop_key = fixture.routes['circuit_rejections.relay_population.day']
        orig_pop = fixture.source.handlers[pop_key]
        fixture.source.handlers[pop_key] = lambda t: [({'type': 'circuit_rejected', 'action': '59'}, 8)] if t == END - 2 * DAY else orig_pop(t)
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'pop.sqlite3')
            try:
                pop = worker.relay_population(chart, series, END, {})
            finally:
                cache.close()
        self.assertNotIn(END - 2 * DAY, [row[0] for row in pop['daily']])

        # Test unified partition when 16 binary + 7 attested (<8) + 1 unverified = 24 relays.
        fixture = CollectorFixture()
        fixture.source.handlers[fixture.routes['cohort.native_verified_binary']] = lambda t: [({'build_id': 'stock.15', 'sha256': 'a' * 64}, 16)]
        fixture.source.handlers[fixture.routes['cohort.native_attested']] = lambda t: [({'build_id': 'attested-0_4_9_17'}, 7)]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'ver.sqlite3')
            try:
                versions = worker.versions(END, {'native': 24, 'custom': 32, 'directory_defense': 48},
                                           {'native': 24, 'custom': 32, 'directory_defense': 48})
            finally:
                cache.close()
        self.assertEqual(versions['runtime_verified_total'], 16)
        self.assertEqual(versions['runtime_binary_hash_verified_total'], 16)
        self.assertEqual(versions['runtime_unverified_relays'], 8)
        self.assertEqual(versions['runtime_resolution']['resolved_relays'], 0)
        self.assertEqual(versions['runtime_resolution']['matched_process_epochs'], 0)
        by_build = {row['build_id']: row for row in versions['verified_runtime']}
        self.assertEqual(by_build['stock.15'], dict(by_build['stock.15'], relays=16))
        self.assertEqual(by_build['tor41339.9']['relays'], 0)
        self.assertEqual(by_build['attested-0_4_9_17']['relays'], 0)
        self.assertEqual({(row['build_id'], row['relays']) for row in versions['verified_runtime']},
                         {('stock.15', 16), ('tor41339.9', 0), ('attested-0_4_9_17', 0)})

    def test_collect_hardening_reconciles_seed_end_and_rejects_bad_paths_and_metadata(self):
        # expand_path preserves $HOME_OTHER untouched
        self.assertEqual(collector.expand_path('/tmp/$HOME_OTHER/x'), Path('/tmp/$HOME_OTHER/x'))
        # iso rejects invalid / out-of-range / non-finite timestamps with CollectionError
        for bad_ts in (-1, 10**20, 10**400, float('inf'), True):
            with self.subTest(bad_ts=bad_ts), self.assertRaises(collector.CollectionError):
                collector.iso(bad_ts)
        # number rejects negative zero strings/floats
        for neg_zero in ('-0', '-0.0', -0.0):
            with self.subTest(neg_zero=neg_zero), self.assertRaises(collector.CollectionError):
                collector.number(neg_zero)

        # SSHSource enforces S_ISREG + O_NOFOLLOW + size bound on identity & known_hosts
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            real_id = root / 'real_id'; real_id.write_text('key'); real_id.chmod(0o600)
            real_kh = root / 'real_kh'; real_kh.write_text('host ssh-ed25519 AAA\n')
            base_cfg = {'SOURCE_SSH_HOST': 'h', 'SOURCE_SSH_USER': 'u', 'SOURCE_SSH_PORT': '22',
                        'SOURCE_SSH_IDENTITY_FILE': str(real_id), 'SOURCE_SSH_KNOWN_HOSTS_FILE': str(real_kh)}
            src = collector.SSHSource(base_cfg)
            self.assertTrue(src.identity)

            id_link = root / 'id_link'; id_link.symlink_to(real_id)
            with self.assertRaises(collector.CollectionError):
                collector.SSHSource(dict(base_cfg, SOURCE_SSH_IDENTITY_FILE=str(id_link)))

            kh_link = root / 'kh_link'; kh_link.symlink_to(real_kh)
            with self.assertRaises(collector.CollectionError):
                collector.SSHSource(dict(base_cfg, SOURCE_SSH_KNOWN_HOSTS_FILE=str(kh_link)))

            fifo = root / 'fifo_kh'; os.mkfifo(fifo)
            with self.assertRaises(collector.CollectionError):
                collector.SSHSource(dict(base_cfg, SOURCE_SSH_KNOWN_HOSTS_FILE=str(fifo)))
            with self.assertRaises(collector.CollectionError):
                collector.SSHSource(dict(base_cfg, SOURCE_SSH_IDENTITY_FILE=str(fifo)))

            big_kh = root / 'big_kh'; big_kh.write_bytes(b'x' * 70000)
            with self.assertRaises(collector.CollectionError):
                collector.SSHSource(dict(base_cfg, SOURCE_SSH_KNOWN_HOSTS_FILE=str(big_kh)))

        # Conflicting source_hashes digests or missing/empty build_id fail cleanly in versions()
        fixture = CollectorFixture()
        fixture.source.handlers[fixture.routes['version.attestation_proof_metadata']] = lambda t: [
            ({'type': 'continuity', 'sha256': 'a' * 64}, t),
            ({'type': 'continuity', 'sha256': 'b' * 64}, t),
        ]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'conflict.sqlite3')
            try:
                with self.assertRaisesRegex(collector.CollectionError, 'Conflicting attestation source digest'):
                    worker.versions(END, {'native': 64, 'custom': 64, 'directory_defense': 64},
                                    {'native': 64, 'custom': 64, 'directory_defense': 64})
            finally:
                cache.close()

        fixture = CollectorFixture()
        fixture.source.handlers[fixture.routes['version.attestation_proof_metadata']] = lambda t: [
            ({'type': 'daemon_group_a', 'sha256': 'c' * 64}, END - 600),
        ]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'missing_bid.sqlite3')
            try:
                with self.assertRaisesRegex(collector.CollectionError, 'build identifier'):
                    worker.versions(END, {'native': 64, 'custom': 64, 'directory_defense': 64},
                                    {'native': 64, 'custom': 64, 'directory_defense': 64})
            finally:
                cache.close()

        # Missing cohort anchor chart / series and missing flag type raise CollectionError
        fixture = CollectorFixture()
        fixture.seed['charts'] = [c for c in fixture.seed['charts'] if c['id'] != 'circuit_rejections']
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'nochart.sqlite3')
            try:
                with self.assertRaisesRegex(collector.CollectionError, 'anchor chart'):
                    worker.collect(fixture.seed['end'], 'a' * 64)
            finally:
                cache.close()

        fixture = CollectorFixture()
        fixture.seed['charts'][0]['series'] = [s for s in fixture.seed['charts'][0]['series'] if s['key'] != 'circuit_rejections']
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'noseries.sqlite3')
            try:
                with self.assertRaisesRegex(collector.CollectionError, 'anchor series'):
                    worker.collect(fixture.seed['end'], 'a' * 64)
            finally:
                cache.close()

        fixture = CollectorFixture()
        fixture.source.handlers[fixture.routes['metadata.flags']] = lambda t: [({}, 80)]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'notype.sqlite3')
            try:
                with self.assertRaisesRegex(collector.CollectionError, 'missing type'):
                    worker.collect(END, 'a' * 64)
            finally:
                cache.close()

        # Re-collecting at end == seed['end'] when live chart_c > cur['expected_relays'] reconciles current & rows[-1]
        for chart_relays, should_stay_valid in ((88, True), (104, False)):
            fixture = CollectorFixture()
            seed_end = fixture.seed['end']
            fixture.source.handlers[fixture.routes['circuit_rejections.daily.available']] = lambda t, r=chart_relays: [
                ({'type': 'circuit_rejected'}, r)]
            with tempfile.TemporaryDirectory() as folder:
                worker, cache = fixture.instance(Path(folder) / 'sameend.sqlite3')
                try:
                    recollected = worker.collect(seed_end, 'a' * 64)
                finally:
                    cache.close()
            cr = next(c for c in recollected['charts'] if c['id'] == 'circuit_rejections')
            import validation
            validation._validate_series(
                dict(cr['series'][0], name='Circuit rejections', color='#123456'),
                dict(cr, display_keys=['circuit_rejections']),
                seed_end,
            )
            cur = cr['series'][0]['current']['day']
            last_row = cr['series'][0]['history']['daily'][-1]
            self.assertEqual(cur['expected_relays'], chart_relays)
            self.assertAlmostEqual(cur['coverage'], 80 / chart_relays)
            self.assertEqual(cur['valid'], should_stay_valid)
            self.assertEqual(cur['value'], last_row[1])
            self.assertEqual(last_row[3], cur['coverage'])
            self.assertEqual(last_row[4], should_stay_valid)
            if not should_stay_valid:
                self.assertIsNone(cur['value'])

    def test_collect_round2_optional_seed_validation_ordering_and_cache_safety(self):
        # SSHSource rejects bad non-numeric port & resolves only AFTER regular/symlink checks
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            real_id = root / 'real_id'; real_id.write_text('key'); real_id.chmod(0o600)
            real_kh = root / 'real_kh'; real_kh.write_text('host ssh-ed25519 AAA\n')
            with self.assertRaisesRegex(collector.CollectionError, 'Invalid source port'):
                collector.SSHSource({'SOURCE_SSH_HOST': 'h', 'SOURCE_SSH_USER': 'u', 'SOURCE_SSH_PORT': 'bad',
                                     'SOURCE_SSH_IDENTITY_FILE': str(real_id), 'SOURCE_SSH_KNOWN_HOSTS_FILE': str(real_kh)})

        # Invalid/empty seed raises ValueError from validate_snapshot before accessing seed['end']
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app_dir = root / 'app'; (app_dir / 'inputs').mkdir(parents=True)
            for bad_seed in ('{}', '{"end":"bad"}'):
                (app_dir / 'inputs/snapshot.json').write_text(bad_seed)
                with self.subTest(bad_seed=bad_seed), self.assertRaisesRegex(ValueError, 'Generation label'):
                    collector.collect({}, app_dir, root / 'state', root / 'out.json',
                                      end=END, source=CollectorFixture().source)

        # Optional auxiliary fields omitted on valid seed do not raise KeyError
        fixture = CollectorFixture()
        del fixture.seed['versions']['verified_runtime']
        del fixture.seed['dircache_cohort']
        for chart in fixture.seed['charts']:
            for series in chart['series']:
                series['current']['day'] = {'value': 2.0 if series['current']['day']['valid'] else None,
                                            'relays': series['current']['day']['relays']}
        fixture.source.handlers[fixture.routes['cohort.native_verified_binary']] = lambda t: []
        fixture.source.handlers[fixture.routes['cohort.custom_verified_binary']] = lambda t: []
        fixture.source.handlers[fixture.routes['cohort.directory_defense_verified_binary']] = lambda t: []
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'opt.sqlite3')
            try:
                out = worker.collect(fixture.seed['end'], 'a' * 64)
            finally:
                cache.close()
        self.assertEqual(out['dircache_cohort'], {'configured_nonexit_zero': 0, 'observed_controlport_zero': 0,
                                                  'file_config_fallback_zero': 0, 'file0_observed1_conflicts_excluded': 0})

        # ObservationCache closes DB on init schema failure & never populates _mem if executemany fails
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / 'fail.sqlite3'
            fake_db = mock.Mock()
            fake_db.execute.side_effect = sqlite3.OperationalError('corrupt')
            with mock.patch.object(collector.sqlite3, 'connect', return_value=fake_db):
                with self.assertRaises(sqlite3.OperationalError):
                    collector.ObservationCache(db_path, CollectorFixture().source, CollectorFixture().queries)
            fake_db.close.assert_called_once()

            fixture = CollectorFixture()
            worker, cache = fixture.instance(Path(folder) / 'mem.sqlite3')
            try:
                with mock.patch.object(cache, 'db') as mock_db:
                    mock_db.execute.return_value = []
                    mock_db.executemany.side_effect = sqlite3.OperationalError('disk full')
                    with self.assertRaises(sqlite3.OperationalError):
                        worker.values(fixture.seed['charts'][0], fixture.seed['charts'][0]['series'][0],
                                      'daily', 'eligible', DAY, [END])
                    self.assertEqual(cache._mem, {})
            finally:
                cache.close()

        # collect() protects rollback failures on both main error and post-commit failure
        fixture = CollectorFixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app_dir = root / 'app'; (app_dir / 'inputs').mkdir(parents=True); (app_dir / 'catalogue').mkdir()
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(fixture.seed))
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(fixture.queries))
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(fixture.routes))
            with mock.patch.object(collector, 'validate_snapshot'):
                fake_cache = mock.Mock()
                fake_cache.rollback.side_effect = sqlite3.OperationalError('rollback failed')
                with mock.patch.object(collector, 'ObservationCache', return_value=fake_cache), \
                        mock.patch.object(collector.Collector, 'collect', side_effect=RuntimeError('primary failure')):
                    with self.assertRaisesRegex(RuntimeError, 'primary failure'):
                        collector.collect({}, app_dir, root / 'state', root / 'out1.json', end=END, source=fixture.source)
                stderr = io.StringIO()
                with mock.patch.object(collector.ObservationCache, 'commit', side_effect=sqlite3.OperationalError('commit failed')), \
                        mock.patch.object(collector.ObservationCache, 'rollback', side_effect=sqlite3.OperationalError('rollback failed')), \
                        redirect_stderr(stderr):
                    summary = collector.collect({}, app_dir, root / 'state', root / 'out2.json', end=END, source=fixture.source)
                self.assertEqual(summary['snapshot_end'], END)
                self.assertIn('cache commit failed', stderr.getvalue())

    def test_same_day_recollect_without_expected_relays_uses_seed_chart_relays_and_passes_validation(self):
        base_seed = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = base_seed['end']
        native_seed_relays = base_seed['fleet']
        for live_native, expect_explicit in (
            (native_seed_relays + 16, True),
            (native_seed_relays - 16, True),
            (native_seed_relays, False),
        ):
            seed = copy.deepcopy(base_seed)
            for chart in seed['charts']:
                if chart['cohort_key'] == 'native':
                    for series in chart['series']:
                        series['current']['day'].pop('expected_relays', None)
            handlers = {
                routes['circuit_rejections.daily.available']: lambda t, n=live_native: [({'type': 'circuit_rejected'}, n)],
                routes['hsdir_fetches.daily.available']: lambda t: [({'action': 'success'}, seed['custom'])],
                routes['directory_rejections.daily.available']: lambda t: [({'type': 'begindir_rejected'}, seed['defense'])],
                routes['version.attestation_proof_metadata']: lambda t: [],
                routes['metadata.flags']: lambda t: [({'type': k}, v) for k, v in seed['flags'].items()],
                routes['metadata.targets']: lambda t, n=live_native: [({}, n)],
                routes['metadata.up']: lambda t, n=live_native: [({}, n)],
            }
            for cohort in ('native', 'custom', 'directory_defense'):
                handlers[routes[f'cohort.{cohort}_verified_binary']] = lambda t: []
                handlers[routes[f'cohort.{cohort}_attested']] = lambda t: []
            for name in ('combined_zero_nonexit', 'observed_zero', 'file_fallback_zero', 'file0_observed1_conflicts'):
                handlers[routes['dircache.' + name]] = lambda t: [({}, 0)]
            with self.subTest(live_native=live_native), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                app_dir = root / 'app'; (app_dir / 'inputs').mkdir(parents=True)
                (app_dir / 'inputs/snapshot.json').write_text(json.dumps(seed))
                out_path = root / 'out.json'
                source = SyntheticSource(queries, handlers)
                collector.collect({}, app_dir, root / 'state', out_path, end=seed_end, source=source)
                published = json.loads(out_path.read_text())
                collector.validate_snapshot(published)
                first_native = next(c for c in published['charts'] if c['cohort_key'] == 'native')
                cur_day = first_native['series'][0]['current']['day']
                self.assertEqual(first_native['relays'], live_native)
                self.assertEqual('expected_relays' in cur_day, expect_explicit)
                expected_denom = live_native if live_native > native_seed_relays else native_seed_relays
                if expect_explicit:
                    self.assertEqual(cur_day['expected_relays'], expected_denom)

    def test_coderabbit_cache_bounds_detachment_mode_and_unqualified_fallback(self):
        import stat
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            # atomic_json enforces 0600 even when replacing an existing 0644 file
            snap = root / 'snapshot.json'
            snap.write_text('{"old":true}')
            snap.chmod(0o644)
            collector.atomic_json(snap, {'new': True})
            self.assertEqual(stat.S_IMODE(snap.lstat().st_mode), 0o600)

            # ObservationCache evicts oldest entries cleanly and falls back to SQLite, returning detached payloads
            fixture = CollectorFixture()
            worker, cache = fixture.instance(root / 'bounded.sqlite3')
            try:
                times = [END - 2 * DAY, END - DAY, END]
                with mock.patch.object(cache, 'MAX_MEM_ENTRIES', 2):
                    first = worker.operation('circuit_rejections.daily.eligible', DAY, times)
                    self.assertEqual(len(cache._mem), 2)
                    # Mutate returned payload and confirm cached/SQLite copy is unaffected
                    first[END]['data']['result'][0]['values'][0][1] = '999999'
                    second = worker.operation('circuit_rejections.daily.eligible', DAY, times)
                    self.assertNotEqual(second[END]['data']['result'][0]['values'][0][1], '999999')
            finally:
                cache.close()

        # Omitted current 'valid' preserves unqualified end-day history row qualification when denominator grows
        fixture = CollectorFixture()
        aux_chart = fixture.seed['charts'][0]
        aux_series = aux_chart['series'][0]
        end_t = fixture.seed['end']
        aux_series['history']['daily'][-1] = [end_t, 2.0, 80, 0.85, False]
        aux_series['current']['day'] = {'value': 2.0, 'relays': 80}
        fixture.source.handlers[fixture.routes['circuit_rejections.daily.available']] = lambda t: [({'type': 'circuit_rejected'}, 88)]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'qual.sqlite3')
            try:
                recollected = worker.collect(end_t, 'a' * 64)
            finally:
                cache.close()
        updated_series = recollected['charts'][0]['series'][0]
        self.assertFalse(updated_series['current']['day']['valid'])
        self.assertIsNone(updated_series['current']['day']['value'])
        self.assertFalse(updated_series['history']['daily'][-1][4])
        self.assertIsNone(updated_series['history']['daily'][-1][1])

    def test_private_seed_configuration_cli_override_and_safety_checks(self):
        fixture = CollectorFixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app_dir = root / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            placeholder = copy.deepcopy(fixture.seed)
            placeholder['fleet'] = 16
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(placeholder))

            private_seed = root / 'seed-snapshot.json'
            private_seed.write_text(json.dumps(fixture.seed))
            private_seed.chmod(0o600)

            out_path = root / 'out.json'
            state_dir = root / 'state'
            cfg_env = root / 'source.env'
            cfg_env.write_text(
                f'SOURCE_SSH_HOST="source.example"\n'
                f'SOURCE_SSH_USER="reader"\n'
                f'SOURCE_SSH_PORT="22"\n'
                f'SOURCE_SSH_IDENTITY_FILE="{root / "id"}"\n'
                f'SOURCE_SSH_KNOWN_HOSTS_FILE="{root / "kh"}"\n'
                f'SOURCE_SEED_SNAPSHOT="{private_seed}"\n'
            )
            cfg_env.chmod(0o600)
            config = collector.load_source_config(cfg_env, required=collector.SSH_FIELDS, private=True)

            # Valid 0600 SOURCE_SEED_SNAPSHOT overrides app/inputs/snapshot.json.
            with mock.patch.object(collector, 'validate_snapshot'):
                summary = collector.collect(config, app_dir, state_dir, out_path, end=END, source=fixture.source)
            self.assertEqual(summary['snapshot_end'], END)
            collected = json.loads(out_path.read_text())
            self.assertEqual(collected['input_provenance']['source_public_data_sha256'],
                             collector.sha256_hex(private_seed.read_bytes()))
            self.assertEqual(collected['charts'][0]['series'][0]['history']['daily'][0],
                             fixture.seed['charts'][0]['series'][0]['history']['daily'][0])

            # Overlap check protects configured private seed too.
            with self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                collector.collect(config, app_dir, state_dir, private_seed, end=END, source=fixture.source)

            # Non-0600 private seed is rejected.
            private_seed.chmod(0o644)
            with self.assertRaisesRegex(collector.CollectionError, 'mode 0600'):
                collector.collect(config, app_dir, state_dir, out_path, end=END, source=fixture.source)
            private_seed.chmod(0o600)

            # Symlink private seed is rejected by O_NOFOLLOW.
            link_seed = root / 'seed-link.json'
            link_seed.symlink_to(private_seed)
            with self.assertRaises(OSError):
                collector.collect(config, app_dir, state_dir, out_path, end=END, source=fixture.source, seed=link_seed)

            # Blank explicit --seed / seed arg is rejected.
            with self.assertRaisesRegex(collector.CollectionError, 'blank'):
                collector.collect(config, app_dir, state_dir, out_path, end=END, source=fixture.source, seed='  ')

            # Explicit --seed via main() overrides SOURCE_SEED_SNAPSHOT.
            cli_seed = root / 'cli-seed.json'
            cli_seed.write_text(json.dumps(fixture.seed))
            cli_seed.chmod(0o600)
            stdout = io.StringIO()
            with mock.patch.object(collector, 'SSHSource', return_value=fixture.source), \
                    mock.patch.object(collector, 'validate_snapshot'), \
                    redirect_stdout(stdout):
                collector.main([
                    '--config', str(cfg_env),
                    '--application-dir', str(app_dir),
                    '--state-dir', str(root / 'state2'),
                    '--output', str(root / 'cli-out.json'),
                    '--seed', str(cli_seed),
                    '--end', str(END),
                ])
            self.assertEqual(json.loads(stdout.getvalue())['snapshot_end'], END)



if __name__ == '__main__':
    unittest.main()
