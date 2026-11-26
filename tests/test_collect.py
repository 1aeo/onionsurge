from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('collector', ROOT / 'scripts/collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
DAY = collector.DAY
HOUR = collector.HOUR
SAMPLED_HOUR_STEP = collector.SAMPLED_HOUR_STEP
END = 20000 * DAY


class SyntheticSource:
    identity = 'synthetic-source-v1'

    def __init__(self, queries, handlers):
        self.queries, self.handlers = queries, handlers
        self.calls = []
        self.catalogue_calls = 0
        self.transform = lambda response: response

    @property
    def requests(self):
        return len(self.calls)

    def catalogue(self):
        self.catalogue_calls += 1
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


def seeded_chart(chart_id, cohort, keys, population, queries=None, routes=None):
    seed_end = END - 2 * DAY
    grid = list(range(seed_end - DAY + HOUR, seed_end + 1, HOUR))
    sh_grid = list(range(seed_end - DAY + SAMPLED_HOUR_STEP, seed_end + 1, SAMPLED_HOUR_STEP))

    def series(key, historical=True):
        cur_day = {
            'value': 2.0 if historical else None,
            'relays': population if historical else None,
            'expected_relays': population,
            'coverage': 1.0 if historical else None,
            'valid': historical,
        }
        cur_hour = {'value': 2.0, 'relays': population, 'expected_relays': population, 'coverage': 1.0, 'valid': True}
        return {
            'key': key,
            'history_status': 'queried' if historical else 'not_queried',
            'values': [[t, 2.0] for t in grid],
            'hourly_relays': [[t, population] for t in grid],
            'hourly_expected_relays': [[t, population] for t in grid],
            'current': {'hour': cur_hour, 'day': cur_day},
            'history': {
                'daily': [[seed_end, 2.0, population, 1.0, True]] if historical else [],
                'sampled_hour': [[t, 2.0, population, 1.0, True] for t in sh_grid] if historical else [],
            },
        }
    chart = {'id': chart_id, 'cohort_key': cohort, 'kind': 'counter_hour_rate', 'relays': population,
             'query': queries[routes[f'{chart_id}.current_hour.value']] if queries and routes else 'original-current-query',
             'quality_query': queries[routes[f'{chart_id}.current_hour.eligible']] if queries and routes else 'original-quality-query',
             'series': [series(key, key != 'circuit_dead') for key in keys]}
    if queries and routes:
        chart['history_queries'] = {
            p: {s: queries[routes[f'{chart_id}.{p}.{s}']] for s in ('value', 'eligible', 'available')}
            for p in ('daily', 'sampled_hour', 'current24')
        }
    return chart


class CollectorFixture:
    def __init__(self):
        self.queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        self.routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        self.seed = {'input_schema_version': 4, 'end': END - 2 * DAY, 'start': END - 3 * DAY,
                     'window_seconds': HOUR, 'minimum_samples': 24, 'expected_samples': 30,
                     'fleet': 80, 'custom': 32, 'defense': 48,
                     'charts': [seeded_chart('circuit_rejections', 'native', ['circuit_rejections'], 80, self.queries, self.routes),
                                seeded_chart('hsdir_fetches', 'custom', ['success'], 32, self.queries, self.routes),
                                seeded_chart('directory_rejections', 'directory_defense', ['begindir_rejected'], 48, self.queries, self.routes),
                                seeded_chart('directory_requests', 'custom', ['hs_get', 'circuit_dead'], 32, self.queries, self.routes)],
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
        all_charts = collector._ensure_supplemental_charts(copy.deepcopy(self.seed), self.routes)['charts']
        handlers = {}
        for chart in all_charts:
            for period in ['current_hour', 'sampled_hour', 'daily', 'current24']:
                for statistic in ['value', 'eligible', 'available']:
                    query_id = self.routes[f'{chart["id"]}.{period}.{statistic}']
                    def data(t, chart=chart, statistic=statistic, period=period):
                        rows = []
                        for series in chart['series']:
                            value = chart['relays']
                            if statistic == 'value':
                                value = 77 if t >= END else 2
                            elif statistic == 'eligible' and chart['id'] == 'circuit_rejections' and t == END - DAY:
                                value = 64
                            labels = collector.series_labels(chart, series)
                            rows.append((labels, value))
                        return rows
                    handlers.setdefault(query_id, data)
        for cohort in ['native', 'custom', 'directory_defense']:
            managed = [('stock.15', 'a' * 64, 48), ('UNKNOWN_BUILD', 'd' * 64, 1)] if cohort == 'native' else [
                ('tor41339.9', 'b' * 64, 16)] if cohort == 'custom' else [('stock.15', 'a' * 64, 24)]
            handlers[self.routes[f'cohort.{cohort}_verified_binary']] = lambda t, managed=managed: [
                ({'build_id': build, 'sha256': sha}, n) for build, sha, n in managed]
            if f'cohort.{cohort}_stock_config' in self.routes:
                handlers[self.routes[f'cohort.{cohort}_stock_config']] = lambda t: []
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
        if 'dos.effective_config' in self.routes:
            handlers[self.routes['dos.effective_config']] = lambda t: []
        if 'dos.begindir_config' in self.routes:
            handlers[self.routes['dos.begindir_config']] = lambda t: []
        for period in ('hour', 'day'):
            for key in {self.routes[f'{chart["id"]}.relay_population.{period}'] for chart in all_charts}:
                def roles(t, key=key, period=period, all_charts=all_charts):
                    rows = []
                    seen_types = set()
                    for chart in all_charts:
                        if self.routes[f'{chart["id"]}.relay_population.{period}'] != key:
                            continue
                        for series in chart['series']:
                            metric_type = next(iter(collector.series_labels(chart, series).values()), 'scalar')
                            if metric_type in seen_types:
                                continue
                            seen_types.add(metric_type)
                            n = 64 if chart['id'] == 'circuit_rejections' and t == END - DAY else chart['relays']
                            rows.append(({'type': metric_type, 'action': '59'}, n))
                    return rows
                handlers[key] = roles
        self.source = SyntheticSource(self.queries, handlers)

    def instance(self, path):
        cache = collector.ObservationCache(path, self.source, self.queries)
        return collector.Collector(self.source, cache, self.queries, self.routes, self.seed), cache


def _series_promql_labels(chart, series):
    return collector.series_labels(chart, series)


def _snapshot_handlers(seed, routes, *, live_native=None, rate_val=2.0):
    native_pop = seed['fleet'] if live_native is None else live_native
    handlers = {}
    all_charts = collector._ensure_supplemental_charts(copy.deepcopy(seed), routes)['charts']
    pop_for_chart = lambda c: native_pop if c['cohort_key'] == 'native' else c['relays']

    for period in ('current_hour', 'sampled_hour', 'daily', 'current24'):
        for stat_name in ('eligible', 'available', 'value'):
            by_qid = {}
            for c in all_charts:
                by_qid.setdefault(routes[f'{c["id"]}.{period}.{stat_name}'], []).append(c)
            for qid, group_charts in by_qid.items():
                def handler(t, group_charts=group_charts, stat_name=stat_name, period=period):
                    rows = []
                    seen = set()
                    for c in group_charts:
                        pop = pop_for_chart(c)
                        for s in c['series']:
                            labels = _series_promql_labels(c, s)
                            canon = tuple(sorted(labels.items()))
                            if canon in seen:
                                continue
                            seen.add(canon)
                            if stat_name == 'value':
                                val = rate_val if rate_val is not None else ((s['current'].get('hour', s['current']['day'])['value']) or 0)
                            else:
                                val = pop
                            rows.append((labels, val))
                    return rows
                handlers[qid] = handler

    for p in ('hour', 'day'):
        by_qid = {}
        for c in all_charts:
            by_qid.setdefault(routes[f'{c["id"]}.relay_population.{p}'], []).append(c)
        for qid, group_charts in by_qid.items():
            def pop_handler(t, group_charts=group_charts):
                rows = []
                seen = set()
                for c in group_charts:
                    pop = pop_for_chart(c)
                    for s in c['series']:
                        labels = _series_promql_labels(c, s)
                        metric_type = next(iter(labels.values()), 'scalar')
                        if metric_type in seen:
                            continue
                        seen.add(metric_type)
                        rows.append(({'type': metric_type, 'action': '59'}, pop))
                return rows
            handlers[qid] = pop_handler

    handlers[routes['version.attestation_proof_metadata']] = lambda t: []
    handlers[routes['metadata.flags']] = lambda t: [({'type': k}, v) for k, v in seed.get('flags', {}).items()]
    handlers[routes['metadata.targets']] = lambda t: [({}, native_pop)]
    handlers[routes['metadata.up']] = lambda t: [({}, native_pop)]
    for cohort in ('native', 'custom', 'directory_defense'):
        handlers[routes[f'cohort.{cohort}_verified_binary']] = lambda t: []
        if f'cohort.{cohort}_stock_config' in routes:
            handlers[routes[f'cohort.{cohort}_stock_config']] = lambda t: []
        handlers[routes[f'cohort.{cohort}_attested']] = lambda t: []
    for name in ('combined_zero_nonexit', 'observed_zero', 'file_fallback_zero', 'file0_observed1_conflicts'):
        handlers[routes['dircache.' + name]] = lambda t: [({}, 0)]
    if 'dos.effective_config' in routes:
        eff_labels = {}
        for key, _, _, _ in collector.DOS_EFFECTIVE_CONFIG_SETTINGS:
            eff_labels[f'{key}_source'] = 'consensus'
            eff_labels[key] = '1'
        handlers[routes['dos.effective_config']] = lambda t, lbl=eff_labels: [(dict(lbl), native_pop)]
    if 'dos.begindir_config' in routes:
        handlers[routes['dos.begindir_config']] = lambda t: [
            ({'option': opt, 'value': 'auto' if opt.endswith('Enabled') else '10'}, seed['defense'])
            for _, opt, _, _ in collector.DOS_BEGINDIR_CONFIG_SETTINGS
        ]
    return handlers


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
            summary = {
                'snapshot_end': END,
                'charts': 4,
                'series': 5,
                'source_requests': 1,
                'cache_committed': True,
                'checkpoint_updated': True,
            }
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
                self.assertEqual(set(population['queries']), {'hour', 'day'})
                for period in ('hour', 'day'):
                    self.assertEqual(population['queries'][period],
                                     fixture.queries[fixture.routes[f'{chart["id"]}.relay_population.{period}']])
                    if series['current'][period]['relays'] is not None:
                        self.assertEqual(population[period][1], series['current'][period]['relays'])
                    else:
                        self.assertIsNone(population[period])
                self.assertEqual([row[0] for row in population['daily']],
                                 [row[0] for row in series['history']['daily'] if row[4]])
                if series['key'] == 'circuit_dead':
                    self.assertIsNotNone(population['day'])
                    self.assertGreater(len(population['daily']), 0)

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
        self.assertEqual(circuit['current']['hour']['value'], 77.0)
        self.assertEqual(circuit['current']['day']['value'], 77.0)
        self.assertIsNone(circuit['relay_population']['hour'])
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
            for mutation in ('none', 'method', 'day_query', 'hour_query', 'total', 'missing'):
                with self.subTest(mutation=mutation):
                    series = copy.deepcopy(baseline)
                    saved = series['relay_population']
                    if mutation == 'method':
                        saved['method'] = 'old-method'
                    elif mutation == 'day_query':
                        saved['queries']['day'] += ' '
                    elif mutation == 'hour_query':
                        saved['queries']['hour'] += ' '
                    elif mutation == 'total':
                        saved['daily'][0][1] += 8
                    elif mutation == 'missing':
                        saved['daily'].pop(1)
                    fixture.source.calls.clear()
                    worker, cache = fixture.instance(Path(folder) / (mutation + '.sqlite3'))
                    worker.seed = copy.deepcopy(data)
                    try:
                        result = worker.relay_population(chart, series, END, {}, {}, {})
                    finally:
                        cache.close()
                    self.assertEqual(result['daily'], baseline['relay_population']['daily'])
                    self.assertEqual([row[0] for row in result['daily']], sorted(row[0] for row in result['daily']))
                    pop_calls = [call for call in fixture.source.calls if call[0].startswith('relay_population_')]
                    self.assertEqual(bool(pop_calls), mutation != 'none')
                    if mutation in ('total', 'missing'):
                        self.assertEqual(len(pop_calls), 1)
                        self.assertEqual(pop_calls[0][3], DAY)
                        self.assertLessEqual(pop_calls[0][2] - pop_calls[0][1], DAY)
                    elif mutation == 'hour_query':
                        self.assertEqual(len(pop_calls), 1)
                        self.assertEqual(pop_calls[0][3], HOUR)

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
        self.assertEqual(data['input_schema_version'], 4)
        self.assertEqual(data['window_seconds'], HOUR)
        self.assertEqual(data['minimum_samples'], 24)
        self.assertEqual(data['expected_samples'], 30)
        charts = {chart['id']: chart for chart in data['charts']}
        circuit = charts['circuit_rejections']['series'][0]
        self.assertEqual(len(circuit['values']), 24)
        self.assertEqual(len(circuit['hourly_relays']), 24)
        self.assertEqual(len(circuit['hourly_expected_relays']), 24)
        self.assertEqual(set(circuit['current']), {'hour', 'day'})
        self.assertEqual(circuit['current']['hour']['value'], 77.0)
        self.assertEqual(circuit['current']['day']['value'], 77.0)
        self.assertEqual(circuit['history']['daily'][0], original['charts'][0]['series'][0]['history']['daily'][0])
        self.assertEqual(circuit['history']['daily'][-2], [END - DAY, 2.0, 64, 0.8, False])
        self.assertEqual(circuit['history']['daily'][-1], [END, 77.0, 80, 1.0, True])
        self.assertEqual(circuit['history']['sampled_hour'][-1], [END, 77.0, 80, 1.0, True])
        self.assertEqual(set(circuit['history']), {'daily', 'sampled_hour'})
        directory = charts['directory_requests']; hs_get_s, aux_s = directory['series']
        self.assertEqual(aux_s['current']['hour']['value'], 77.0)
        self.assertEqual(aux_s['current']['day']['value'], 77.0)
        self.assertEqual(aux_s['history']['daily'][0], [hs_get_s['history']['daily'][0][0], 2.0, 32, 1.0, True])
        self.assertEqual(aux_s['history']['daily'][-1], [END, 77.0, 32, 1.0, True])
        self.assertGreater(len(aux_s['history']['sampled_hour']), 0)
        self.assertEqual(aux_s['history']['sampled_hour'][-1], [END, 77.0, 32, 1.0, True])
        self.assertGreater(len(aux_s['relay_population']['daily']), 0)
        expected = fixture.queries[fixture.routes['directory_requests.current_hour.value']]
        self.assertEqual(directory['query'], expected)
        self.assertIn('== 3', expected)
        self.assertIn('count by(instance,type)', directory['quality_query'])
        used = {key for key, _, _, _ in fixture.source.calls}
        self.assertIn(fixture.routes['directory_requests.daily.eligible'], used)
        self.assertIn(fixture.routes['directory_requests.current_hour.eligible'], used)
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
        self.assertEqual(versions['runtime_resolution']['snapshot_start'], END - HOUR)
        self.assertEqual(versions['runtime_resolution']['snapshot_end'], END)
        self.assertNotIn('UNKNOWN_BUILD', json.dumps(versions))
        attested = next(row for row in versions['verified_runtime'] if row['build_id'].startswith('attested-'))
        self.assertNotIn('patched', attested)
        self.assertIn(collector.iso(END - 600), attested['verification'])
        self.assertIn('measured hour', attested['verification'])
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

    def test_live_stock_config_merges_versions_avoids_attested_double_count_and_rejects_bad_version(self):
        # Case 1: verified_binary + merged stock_config + empty attested (all unmanaged stock covered by stock_config).
        fixture = CollectorFixture()
        fixture.source.handlers[fixture.routes['cohort.native_verified_binary']] = lambda t: [
            ({'build_id': 'stock.15', 'sha256': 'a' * 64}, 16),
            ({'build_id': 'tor41339.9', 'sha256': 'b' * 64}, 16),
        ]
        fixture.source.handlers[fixture.routes['cohort.native_stock_config']] = lambda t: [
            ({'tor_version': '0.4.9.17'}, 32),
            ({'tor_version': '0.4.9.15'}, 16),  # same version merges cleanly into stock_0_4_9_15
        ]
        fixture.source.handlers[fixture.routes['cohort.directory_defense_verified_binary']] = lambda t: [
            ({'build_id': 'stock.15', 'sha256': 'a' * 64}, 16),
        ]
        fixture.source.handlers[fixture.routes['cohort.directory_defense_stock_config']] = lambda t: [
            ({'tor_version': '0.4.9.17'}, 32),
        ]
        fixture.source.handlers[fixture.routes['cohort.native_attested']] = lambda t: []
        fixture.source.handlers[fixture.routes['cohort.directory_defense_attested']] = lambda t: []
        totals = {'native': 80, 'custom': 16, 'directory_defense': 48}
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'stock.sqlite3')
            try:
                versions = worker.versions(END, totals, totals)
            finally:
                cache.close()
        self.assertEqual(versions['runtime_verified_total'], 80)
        self.assertEqual(versions['runtime_unverified_relays'], 0)
        self.assertEqual(versions['runtime_binary_hash_verified_total'], 32)
        self.assertNotIn('runtime_resolution', versions)
        self.assertEqual(versions['cohort_labels'], {
            'patched_0_4_9_15_tor41339_9': 'Patched Tor 0.4.9.15 + tor41339.9',
            'runtime_unverified': 'Version not recorded',
            'stock_0_4_9_15': 'Tor 0.4.9.15',
            'stock_0_4_9_17': 'Tor 0.4.9.17',
        })
        by_key = {(r['version'], r.get('patched')): r for r in versions['verified_runtime']}
        self.assertEqual(by_key[('0.4.9.17', False)]['relays'], 32)
        self.assertEqual(by_key[('0.4.9.17', False)]['verification'],
                         'Live non-custom process version from Prometheus config inventory.')
        self.assertEqual(by_key[('0.4.9.15', False)]['relays'], 32)
        self.assertEqual(by_key[('0.4.9.15', False)]['verification'],
                         'Approved recorded binary hash matched a fresh process epoch; '
                         'additional stock relays from live Prometheus config inventory.')
        self.assertEqual(by_key[('0.4.9.15 + tor41339.9', True)]['relays'], 16)
        self.assertEqual(len({r['build_id'] for r in versions['verified_runtime']}), len(versions['verified_runtime']))
        for label in versions['cohort_labels'].values():
            self.assertNotIn('(daemon version)', label)

        # Case 2: partial stock_config + disjoint attested relays are preserved instead of dropped.
        partial_fixture = CollectorFixture()
        partial_fixture.source.handlers[partial_fixture.routes['cohort.native_verified_binary']] = lambda t: [
            ({'build_id': 'stock.15', 'sha256': 'a' * 64}, 16),
            ({'build_id': 'tor41339.9', 'sha256': 'b' * 64}, 16),
        ]
        partial_fixture.source.handlers[partial_fixture.routes['cohort.native_stock_config']] = lambda t: [
            ({'tor_version': '0.4.9.16'}, 16),
            ({'tor_version': '0.4.9.15'}, 16),
        ]
        partial_fixture.source.handlers[partial_fixture.routes['cohort.native_attested']] = lambda t: [
            ({'build_id': 'attested-0_4_9_17'}, 16),
        ]
        partial_fixture.source.handlers[partial_fixture.routes['cohort.directory_defense_verified_binary']] = lambda t: [
            ({'build_id': 'stock.15', 'sha256': 'a' * 64}, 16),
        ]
        partial_fixture.source.handlers[partial_fixture.routes['cohort.directory_defense_stock_config']] = lambda t: [
            ({'tor_version': '0.4.9.16'}, 16),
        ]
        partial_fixture.source.handlers[partial_fixture.routes['cohort.directory_defense_attested']] = lambda t: [
            ({'build_id': 'attested-0_4_9_17'}, 16),
        ]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = partial_fixture.instance(Path(folder) / 'partial.sqlite3')
            try:
                partial_versions = worker.versions(END, totals, totals)
            finally:
                cache.close()
        self.assertEqual(partial_versions['runtime_verified_total'], 80)
        self.assertEqual(partial_versions['runtime_unverified_relays'], 0)
        self.assertEqual(partial_versions['runtime_binary_hash_verified_total'], 32)
        self.assertEqual(partial_versions['runtime_resolution']['resolved_relays'], 16)
        self.assertEqual(partial_versions['runtime_resolution']['matched_process_epochs'], 16)
        self.assertEqual(partial_versions['cohort_labels']['attested_0_4_9_17'], 'Tor 0.4.9.17')
        partial_by_key = {(r['build_id'], r['version']): r for r in partial_versions['verified_runtime']}
        self.assertEqual(partial_by_key[('attested-0_4_9_17', '0.4.9.17')]['relays'], 16)

        # Invalid tor_version rejected at protocol validation (AccessError) and in Collector.versions (CollectionError).
        bad_fixture = CollectorFixture()
        bad_fixture.source.handlers[bad_fixture.routes['cohort.native_stock_config']] = lambda t: [
            ({'tor_version': 'bad-version'}, 8),
        ]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = bad_fixture.instance(Path(folder) / 'bad.sqlite3')
            try:
                with self.assertRaisesRegex(collector.access.AccessError, 'invalid_label_value'):
                    worker.versions(END, totals, totals)
                orig_endpoint = worker.endpoint
                def bypassed_endpoint(name, ts):
                    if name == 'cohort.native_stock_config':
                        return {'data': {'result': [{'metric': {'tor_version': 'bad-version'}, 'values': [[ts, '8']]}]}}
                    return orig_endpoint(name, ts)
                with mock.patch.object(worker, 'endpoint', side_effect=bypassed_endpoint):
                    with self.assertRaisesRegex(collector.CollectionError, 'Unexpected public stock Tor version'):
                        worker.versions(END, totals, totals)
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
                self.assertEqual(len(series['values']), 24)
                self.assertTrue(all(pt[1] is None for pt in series['values']))
                self.assertFalse(series['current']['hour']['valid'])
                self.assertGreaterEqual(series['current']['hour']['expected_relays'], 8)
                self.assertFalse(series['current']['day']['valid'])
                self.assertGreaterEqual(series['current']['day']['expected_relays'], 8)
                self.assertEqual(series['history']['daily'][:len(prior['history']['daily'])], prior['history']['daily'])
                self.assertTrue(all(row[1] is None and row[4] is False
                                    for row in series['history']['daily'][len(prior['history']['daily']):]))
                self.assertTrue(all(row[1] is None and row[4] is False
                                    for row in series['history']['sampled_hour'][len(prior['history']['sampled_hour']):]))

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
            with self.assertRaisesRegex(collector.CollectionError, 'completed UTC hour'):
                collector.collect({}, app_dir, Path(folder) / 'state', Path(folder) / 'out.json',
                                  end=END + 1800, source=fixture.source)
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
                initial_summary = collector.collect({}, app_dir, state_dir, out_path, end=END - DAY, source=fixture.source)
            self.assertTrue(initial_summary['cache_committed'])
            self.assertTrue(initial_summary['checkpoint_updated'])
            checkpoint_path = state_dir / 'checkpoint.json'
            self.assertTrue(checkpoint_path.is_file())
            self.assertEqual(stat.S_IMODE(checkpoint_path.lstat().st_mode), 0o600)
            initial_cp = checkpoint_path.read_bytes()
            self.assertEqual(json.loads(initial_cp)['observation_end'], END - DAY)
            committed_before = out_path.read_text()
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                before_rows = db.execute('SELECT count(*) FROM observations').fetchone()[0]
                before_end_rows = db.execute('SELECT count(*) FROM observations WHERE endpoint=?', (END,)).fetchone()[0]
                before_completions = db.execute('SELECT observation_end FROM collection_completions').fetchall()
            self.assertGreater(before_rows, 0)
            self.assertEqual(before_end_rows, 0)
            self.assertEqual(before_completions, [(END - DAY,)])

            (app_dir / 'inputs/snapshot.json').write_text(committed_before)
            for mode in ('validation', 'write', 'record_completion', 'commit', 'checkpoint_write'):
                calls = {'n': 0}
                def validate_output_fail(data, **kwargs):
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
                        elif mode in {'record_completion', 'commit'}:
                            real_cache_cls = collector.ObservationCache
                            if mode == 'record_completion':
                                class FailingCache(real_cache_cls):
                                    def record_completion(self, source_identity, observation_end):
                                        raise sqlite3.OperationalError('simulated completion insert failure')
                            else:
                                class FailingCache(real_cache_cls):
                                    def commit(self):
                                        raise sqlite3.OperationalError('simulated lock')
                            with mock.patch.object(collector, 'ObservationCache', FailingCache), \
                                    self.assertRaises(sqlite3.OperationalError):
                                collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(out_path.read_text(), committed_before)
                            self.assertEqual(list(out_path.parent.glob('.snapshot.json.*.tmp')), [])
                        elif mode == 'checkpoint_write':
                            stderr = io.StringIO()
                            real_atomic_json = collector.atomic_json
                            def fail_checkpoint_only(path, value, **kwargs):
                                if Path(path).name == 'checkpoint.json':
                                    raise OSError('checkpoint disk full')
                                return real_atomic_json(path, value, **kwargs)
                            with mock.patch.object(collector, 'atomic_json', side_effect=fail_checkpoint_only), redirect_stderr(stderr):
                                summary = collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(summary['snapshot_end'], END)
                            self.assertTrue(summary['cache_committed'])
                            self.assertFalse(summary['checkpoint_updated'])
                            self.assertEqual(json.loads(out_path.read_text())['end'], END)
                            self.assertIn('collector checkpoint update failed after snapshot publication', stderr.getvalue())
                            self.assertEqual(checkpoint_path.read_bytes(), initial_cp)
                            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                                completions = sorted(row[0] for row in db.execute(
                                    'SELECT observation_end FROM collection_completions'))
                                archived = sorted(row[0] for row in db.execute(
                                    'SELECT observation_end FROM hourly_archive'))
                            self.assertEqual(completions, [END - DAY, END])
                            self.assertEqual(archived, [END - DAY, END])
                            continue
                        else:
                            with self.assertRaisesRegex(ValueError, 'simulated output validation failure'):
                                collector.collect({}, app_dir, state_dir, out_path, end=END, source=fixture.source)
                            self.assertEqual(out_path.read_text(), committed_before)
                    self.assertEqual(checkpoint_path.read_bytes(), initial_cp)
                    with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                        after_rows = db.execute('SELECT count(*) FROM observations').fetchone()[0]
                        after_end_rows = db.execute('SELECT count(*) FROM observations WHERE endpoint=?', (END,)).fetchone()[0]
                        after_completions = db.execute('SELECT observation_end FROM collection_completions').fetchall()
                        after_archives = db.execute('SELECT count(*) FROM hourly_archive WHERE observation_end=?', (END,)).fetchone()[0]
                    self.assertEqual(after_rows, before_rows)
                    self.assertEqual(after_end_rows, 0)
                    self.assertEqual(after_completions, before_completions)
                    self.assertEqual(after_archives, 0)

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
                pop = worker.relay_population(chart, series, END, {}, {}, {})
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
                with self.subTest(bad_seed=bad_seed), self.assertRaisesRegex(ValueError, 'schema version|Generation label'):
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
                with mock.patch.object(collector.ObservationCache, 'commit', side_effect=sqlite3.OperationalError('commit failed')), \
                        mock.patch.object(collector.ObservationCache, 'rollback', side_effect=sqlite3.OperationalError('rollback failed')):
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'commit failed'):
                        collector.collect({}, app_dir, root / 'state', root / 'out2.json', end=END, source=fixture.source)
                self.assertFalse((root / 'out2.json').exists())
                self.assertEqual(list(root.glob('.out2.json.*.tmp')), [])

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
            handlers = _snapshot_handlers(seed, routes, live_native=live_native, rate_val=None)
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



    def test_v3_seed_upgrades_cleanly_to_v4_with_sampled_hour_backfill_and_intraday_collection(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        v3 = copy.deepcopy(base_v4)
        seed_end = int(v3['end'])
        v3.update(input_schema_version=3, window_seconds=DAY, minimum_samples=360, expected_samples=720)
        if 'runtime_resolution' in v3['versions']:
            v3['versions']['runtime_resolution']['snapshot_start'] = v3['start']
        for chart in v3['charts']:
            chart['kind'] = 'gauge_daily_mean' if chart['kind'] == 'gauge_hour_mean' else 'counter_daily_rate'
            chart['query'] = chart['history_queries']['daily']['value']
            chart['quality_query'] = chart['history_queries']['daily']['eligible']
            chart['history_queries'] = {'daily': chart['history_queries']['daily']}
            for series in chart['series']:
                for field in ('values', 'hourly_relays', 'hourly_expected_relays'):
                    series.pop(field, None)
                series['history'].pop('sampled_hour', None)
                series['current'] = {'day': series['current']['day']}
                if 'relay_population' in series:
                    series['relay_population'].pop('hour', None)
                    series['relay_population']['queries'] = {'day': series['relay_population']['queries']['day']}
        # Seed a recognizable immutable qualified daily row (0.0343) and role row on primary chart.
        first_series = v3['charts'][0]['series'][0]
        first_day_ts = int(first_series['history']['daily'][0][0])
        qual_row = next(row for row in first_series['history']['daily'] if row[4])
        qual_row[1] = 0.0343
        qual_ts = int(qual_row[0])
        original_first_pop = copy.deepcopy(first_series['relay_population']['daily'][0])

        with self.assertRaisesRegex(ValueError, 'schema version'):
            collector.validate_snapshot(v3)
        collector.validate_snapshot(v3, allow_v3_seed=True)

        intraday_end = seed_end + 3 * HOUR
        handlers = _snapshot_handlers(v3, routes, rate_val=100.0)
        source = SyntheticSource(queries, handlers)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            app_dir = root / 'app'; (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(v3))
            out_path = root / 'upgraded.json'
            collector.collect({}, app_dir, root / 'state', out_path, end=intraday_end, source=source)
            upgraded = json.loads(out_path.read_text())

        collector.validate_snapshot(upgraded)
        self.assertEqual(upgraded['input_schema_version'], 4)
        self.assertEqual(upgraded['end'], intraday_end)
        self.assertEqual(upgraded['start'], intraday_end - DAY)
        self.assertEqual(upgraded['window_seconds'], HOUR)
        self.assertEqual(upgraded['minimum_samples'], 24)
        self.assertEqual(upgraded['expected_samples'], 30)

        up_first = upgraded['charts'][0]['series'][0]
        up_qual_row = next(row for row in up_first['history']['daily'] if row[0] == qual_ts)
        self.assertEqual(up_qual_row[1], 0.0343)
        self.assertEqual(up_first['history']['daily'][-1][0], seed_end)
        self.assertEqual([pt[0] for pt in up_first['values']],
                         list(range(intraday_end - DAY + HOUR, intraday_end + 1, HOUR)))
        target_sh = (intraday_end // SAMPLED_HOUR_STEP) * SAMPLED_HOUR_STEP
        expected_first_sh = max(first_day_ts - DAY + SAMPLED_HOUR_STEP, target_sh - 560 * DAY)
        self.assertEqual(up_first['history']['sampled_hour'][0][0], expected_first_sh)
        self.assertEqual(up_first['history']['sampled_hour'][-1][0], target_sh)
        self.assertEqual(up_first['current']['hour']['value'], 100.0)
        self.assertEqual(up_first['current']['day']['value'], 100.0)
        self.assertEqual(up_first['relay_population']['hour'][0], intraday_end)
        self.assertEqual(up_first['relay_population']['day'][0], intraday_end)
        self.assertEqual(up_first['relay_population']['daily'][0], original_first_pop)

    def _private_source_env(self, root, *, extra_env=''):
        id_file = root / 'id'
        kh_file = root / 'kh'
        id_file.write_text('PRIVATE-KEY\n')
        id_file.chmod(0o600)
        kh_file.write_text('source.example ssh-ed25519 AAAA\n')
        kh_file.chmod(0o600)
        cfg_file = root / 'source.env'
        cfg_file.write_text(
            f'SOURCE_SSH_HOST="source.example"\n'
            f'SOURCE_SSH_USER="onionsurge-read"\n'
            f'SOURCE_SSH_PORT="22"\n'
            f'SOURCE_SSH_IDENTITY_FILE="{id_file}"\n'
            f'SOURCE_SSH_KNOWN_HOSTS_FILE="{kh_file}"\n'
            f'{extra_env}'
        )
        cfg_file.chmod(0o600)
        return cfg_file, id_file, kh_file

    def test_plan_collection_cold_warm_checkpoint_and_timeout_scaling(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        v3 = copy.deepcopy(base_v4)
        v3.update(input_schema_version=3, window_seconds=DAY, minimum_samples=360, expected_samples=720)
        v3['versions']['runtime_resolution']['snapshot_start'] = v3['start']
        for chart in v3['charts']:
            chart['kind'] = 'gauge_daily_mean' if chart['kind'] == 'gauge_hour_mean' else 'counter_daily_rate'
            chart['query'] = chart['history_queries']['daily']['value']
            chart['quality_query'] = chart['history_queries']['daily']['eligible']
            chart['history_queries'] = {'daily': chart['history_queries']['daily']}
            for s in chart['series']:
                for f in ('values', 'hourly_relays', 'hourly_expected_relays'):
                    s.pop(f, None)
                s['history'].pop('sampled_hour', None)
                s['current'] = {'day': s['current']['day']}
                if 'relay_population' in s:
                    s['relay_population'].pop('hour', None)
                    s['relay_population']['queries'] = {'day': s['relay_population']['queries']['day']}

        short_v3 = copy.deepcopy(v3)
        cutoff_5d = seed_end - 5 * DAY
        for chart in short_v3['charts']:
            for s in chart['series']:
                if s['history']['daily']:
                    s['history']['daily'] = [r for r in s['history']['daily'] if r[0] >= cutoff_5d]
                if 'relay_population' in s and s['relay_population']['daily']:
                    s['relay_population']['daily'] = [r for r in s['relay_population']['daily'] if r[0] >= cutoff_5d]

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, kh_file = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            app_dir = root / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'catalogue').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(base_v4))
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(queries))
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(routes))

            missing_state = root / 'cold_state'
            end1 = seed_end + 3 * HOUR
            stdout = io.StringIO()
            with mock.patch.object(collector.subprocess, 'run') as sub_run, redirect_stdout(stdout):
                collector.main([
                    '--config', str(cfg_file),
                    '--application-dir', str(app_dir),
                    '--state-dir', str(missing_state),
                    '--end', str(end1),
                    '--plan',
                ])
            sub_run.assert_not_called()
            self.assertFalse(missing_state.exists())
            plan = json.loads(stdout.getvalue())
            self.assertEqual(set(plan), {'snapshot_end', 'catchup_days', 'recommended_timeout_seconds'})
            self.assertEqual((plan['snapshot_end'], plan['catchup_days']), (end1, 1))
            planned_reqs = (plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT

            # Execute cold v4 collect and verify actual requests <= planned requests and checkpoint creation.
            out1 = root / 'out1.json'
            handlers = _snapshot_handlers(base_v4, routes, rate_val=1.0)
            src = SyntheticSource(queries, handlers)
            summary = collector.collect(cfg, app_dir, missing_state, out1, end=end1, source=src, config_path=cfg_file)
            self.assertTrue(summary['cache_committed'])
            self.assertTrue(summary['checkpoint_updated'])
            self.assertEqual(src.catalogue_calls, 1)
            self.assertLessEqual(summary['source_requests'] + src.catalogue_calls, planned_reqs)

            # Warm plan 2 days ahead uses checkpoint observation_end (catchup_days == 2), not seed_end (3).
            end2 = seed_end + 2 * DAY + 3 * HOUR
            warm_plan = collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)
            self.assertEqual(warm_plan['catchup_days'], 2)

            # Changing source.env, known_hosts, seed, queries, or routes invalidates warm_end -> cold (3d).
            cfg_orig = cfg_file.read_text()
            cfg_file.write_text(cfg_orig + '# rotated\n')
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            cfg_file.write_text(cfg_orig)
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 2)

            kh_orig = kh_file.read_text()
            kh_file.write_text(kh_orig + 'extra\n')
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            kh_file.write_text(kh_orig)

            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(base_v4) + ' ')
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(base_v4))

            q_alt = copy.deepcopy(queries)
            q_alt['_note'] = q_alt['metadata_up']
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(q_alt))
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(queries))

            r_alt = copy.deepcopy(routes)
            r_alt['_note'] = r_alt['metadata.up']
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(r_alt))
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(routes))

            # previous_end clamps baseline_end backwards; previous_end > end fails; distinguish previous_end vs base window limits.
            clamped = collector.plan_collection(cfg, app_dir, missing_state, previous_end=seed_end - DAY, end=end2, config_path=cfg_file)
            self.assertEqual(clamped['catchup_days'], 4)
            with self.assertRaisesRegex(collector.CollectionError, 'precedes'):
                collector.plan_collection(cfg, app_dir, missing_state, previous_end=end2 + HOUR, end=end2, config_path=cfg_file)
            with self.assertRaisesRegex(collector.CollectionError, 'manual publication workflow'):
                collector.plan_collection(cfg, app_dir, missing_state, previous_end=seed_end - DAY, max_catchup_days=2, end=end2, config_path=cfg_file)
            with self.assertRaisesRegex(collector.CollectionError, 'manual collection'):
                collector.plan_collection(cfg, app_dir, missing_state, max_catchup_days=1, end=end2, config_path=cfg_file)
            with self.assertRaisesRegex(collector.CollectionError, 'manual collection'):
                collector.plan_collection(cfg, app_dir, missing_state, previous_end=seed_end - 2 * DAY, max_catchup_days=1, end=end2, config_path=cfg_file)

            # Missing completion row (despite non-empty observations) and empty DB (0 committed rows) both disable warm_end cleanly.
            with closing(sqlite3.connect(missing_state / 'observations.sqlite3')) as db:
                comp_backup = db.execute('SELECT source_identity, observation_end FROM collection_completions').fetchall()
                db.execute('DELETE FROM collection_completions')
                db.commit()
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)
            with closing(sqlite3.connect(missing_state / 'observations.sqlite3')) as db:
                db.executemany('INSERT INTO collection_completions VALUES (?, ?)', comp_backup)
                db.execute('DELETE FROM observations')
                db.commit()
            self.assertEqual(collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)['catchup_days'], 3)

            # Cold v3 sampled_hour backfill timeout scales dynamically with seed span and covers actual requests.
            v3_app = root / 'v3_app'
            (v3_app / 'inputs').mkdir(parents=True)
            (v3_app / 'inputs/snapshot.json').write_text(json.dumps(v3))
            v3_short_app = root / 'v3_short_app'
            (v3_short_app / 'inputs').mkdir(parents=True)
            (v3_short_app / 'inputs/snapshot.json').write_text(json.dumps(short_v3))
            full_v3_plan = collector.plan_collection(cfg, v3_app, root / 'v3_s1', end=end1, config_path=cfg_file)
            short_v3_plan = collector.plan_collection(cfg, v3_short_app, root / 'v3_s2', end=end1, config_path=cfg_file)
            self.assertGreater(full_v3_plan['recommended_timeout_seconds'], short_v3_plan['recommended_timeout_seconds'])
            short_src = SyntheticSource(queries, _snapshot_handlers(short_v3, routes, rate_val=1.0))
            short_res = collector.collect(cfg, v3_short_app, root / 'v3_s2', root / 'v3_short_out.json',
                                          end=end1, source=short_src, config_path=cfg_file)
            short_planned = (short_v3_plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT
            self.assertEqual(short_src.catalogue_calls, 1)
            self.assertLessEqual(short_res['source_requests'] + short_src.catalogue_calls, short_planned)

            # A seed whose directory_requests auxiliary series lack daily/sampled_hour/population rows sizes backfill requests accurately.
            dir_full = copy.deepcopy(base_v4)
            for c in dir_full['charts']:
                for s in c['series']:
                    if s['history']['daily']:
                        s['history']['daily'] = [r for r in s['history']['daily'] if r[0] >= cutoff_5d]
                    if s['history'].get('sampled_hour'):
                        s['history']['sampled_hour'] = [r for r in s['history']['sampled_hour'] if r[0] >= cutoff_5d]
                    if 'relay_population' in s and s['relay_population']['daily']:
                        s['relay_population']['daily'] = [r for r in s['relay_population']['daily'] if r[0] >= cutoff_5d]
            dir_empty = copy.deepcopy(dir_full)
            for c in dir_empty['charts']:
                if c['id'] == 'directory_requests':
                    for s in c['series']:
                        if s['key'] not in c['display_keys']:
                            s['history']['daily'] = []
                            s['history']['sampled_hour'] = []
                            s['current']['day'] = {'value': None, 'relays': None, 'expected_relays': c['relays'],
                                                   'coverage': None, 'valid': False}
                            s['relay_population']['daily'] = []
                            s['relay_population']['day'] = None
            dir_full_app, dir_empty_app = root / 'dir_full_app', root / 'dir_empty_app'
            (dir_full_app / 'inputs').mkdir(parents=True)
            (dir_empty_app / 'inputs').mkdir(parents=True)
            (dir_full_app / 'inputs/snapshot.json').write_text(json.dumps(dir_full))
            (dir_empty_app / 'inputs/snapshot.json').write_text(json.dumps(dir_empty))
            full_dir_plan = collector.plan_collection(cfg, dir_full_app, root / 'dir_full_s', end=end1, config_path=cfg_file)
            empty_dir_plan = collector.plan_collection(cfg, dir_empty_app, root / 'dir_empty_s', end=end1, config_path=cfg_file)
            self.assertGreater(empty_dir_plan['recommended_timeout_seconds'], full_dir_plan['recommended_timeout_seconds'])
            dir_src = SyntheticSource(queries, _snapshot_handlers(dir_empty, routes, rate_val=1.0))
            dir_res = collector.collect(cfg, dir_empty_app, root / 'dir_empty_s', root / 'dir_empty_out.json',
                                        end=end1, source=dir_src, config_path=cfg_file)
            dir_planned = (empty_dir_plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT
            self.assertLessEqual(dir_res['source_requests'] + dir_src.catalogue_calls, dir_planned)
            dir_collected = json.loads((root / 'dir_empty_out.json').read_text())
            dir_chart_after = next(c for c in dir_collected['charts'] if c['id'] == 'directory_requests')
            aux_after = next(s for s in dir_chart_after['series'] if s['key'] not in dir_chart_after['display_keys'])
            self.assertEqual(len(aux_after['history']['daily']), len(dir_collected['charts'][0]['series'][0]['history']['daily']))
            self.assertGreater(len(aux_after['history']['sampled_hour']), 0)
            self.assertGreater(len(aux_after['relay_population']['daily']), 0)

            # Alternating qualifying population dates must count one request per target date (not contiguous batches).
            sparse_seed = copy.deepcopy(short_v3)
            sparse_seed_end = int(sparse_seed['end'])
            for c in sparse_seed['charts']:
                for s in c['series']:
                    s['relay_population']['daily'] = []
                    for i, row in enumerate(s['history']['daily']):
                        if i % 2 == 1 and row[0] != sparse_seed_end:
                            row[:] = [row[0], None, None, None, False]
            sparse_app = root / 'sparse_pop_app'
            (sparse_app / 'inputs').mkdir(parents=True)
            (sparse_app / 'inputs/snapshot.json').write_text(json.dumps(sparse_seed))
            sparse_plan = collector.plan_collection(cfg, sparse_app, root / 'sparse_pop_s', end=sparse_seed_end, config_path=cfg_file)
            sparse_budget = (sparse_plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT
            sparse_src = SyntheticSource(queries, _snapshot_handlers(sparse_seed, routes, rate_val=1.0))
            sparse_res = collector.collect(cfg, sparse_app, root / 'sparse_pop_s', root / 'sparse_pop_out.json',
                                           end=sparse_seed_end, source=sparse_src, config_path=cfg_file)
            self.assertLessEqual(sparse_res['source_requests'] + sparse_src.catalogue_calls, sparse_budget)
            # Verify dense seed plan counts each population target individually rather than as a contiguous batch.
            dense_seed = copy.deepcopy(short_v3)
            for c in dense_seed['charts']:
                for s in c['series']:
                    s['relay_population']['daily'] = []
            dense_reqs = collector._plan_max_requests(routes, queries, dense_seed, sparse_seed_end,
                                                      warm_end=None, baseline_end=sparse_seed_end)
            by_qid = {}
            for c in dense_seed['charts']:
                qid = routes[f'{c["id"]}.relay_population.day']
                dates = by_qid.setdefault(qid, set())
                for s in c['series']:
                    dates.update(int(r[0]) for r in s['history']['daily'] if r[4])
            unbatched_extra = sum(len(dates) for dates in by_qid.values())
            batched_extra = sum(collector._contiguous_run_batches(dates, DAY, population=True)
                                for dates in by_qid.values())
            self.assertGreater(unbatched_extra, batched_extra)
            base_with_pop = copy.deepcopy(short_v3)
            base_reqs = collector._plan_max_requests(routes, queries, base_with_pop, sparse_seed_end,
                                                     warm_end=None, baseline_end=sparse_seed_end)
            self.assertGreaterEqual(dense_reqs - base_reqs, unbatched_extra)

            # Fail closed on non-private/symlink/corrupt state artifacts during --plan.
            cp_file = missing_state / 'checkpoint.json'
            cp_file.chmod(0o644)
            with self.assertRaises(collector.CollectionError):
                collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)
            cp_file.chmod(0o600)
            cp_file.write_text('{bad json')
            with self.assertRaises(collector.CollectionError):
                collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)
            cp_file.unlink()
            db_file = missing_state / 'observations.sqlite3'
            db_file.chmod(0o644)
            with self.assertRaises(collector.CollectionError):
                collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)
            db_file.chmod(0o600)
            db_file.write_bytes(b'corrupt bytes not sqlite')
            with self.assertRaisesRegex(collector.CollectionError, 'Invalid observation cache database'):
                collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)
            db_file.write_bytes(b'SQLite format 3')
            with self.assertRaisesRegex(collector.CollectionError, 'Invalid observation cache database'):
                collector.plan_collection(cfg, app_dir, missing_state, end=end2, config_path=cfg_file)

    def test_record_checkpoint_and_cli_mode_validation(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, _ = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            state_dir = root / 'state'

            # Missing state_dir must NOT be created and must fail.
            with self.assertRaises(collector.CollectionError):
                collector.record_checkpoint(cfg, ROOT, state_dir, seed_end, config_path=cfg_file)
            self.assertFalse(state_dir.exists())

            # Populate state with a committed DB, remove checkpoint.json, then re-record via CLI --record-checkpoint.
            out_path = root / 'out.json'
            end1 = seed_end + 2 * DAY + HOUR
            src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            collector.collect(cfg, ROOT, state_dir, out_path, end=end1, source=src, config_path=cfg_file)
            cp_path = state_dir / 'checkpoint.json'
            cp_path.unlink()

            # Empty table -> rejected.
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                row_backup = db.execute('SELECT * FROM observations LIMIT 1').fetchone()
                all_rows = db.execute('SELECT * FROM observations').fetchall()
                db.execute('DELETE FROM observations')
                db.commit()
            with self.assertRaises(collector.CollectionError):
                collector.record_checkpoint(cfg, ROOT, state_dir, end1, config_path=cfg_file)
            self.assertFalse(cp_path.exists())

            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                db.executemany('INSERT INTO observations VALUES (?,?,?,?,?)', all_rows or [row_backup])
                db.commit()

            # Non-matching endpoint or altered source identity must be rejected by --record-checkpoint even with populated observations.
            with self.assertRaisesRegex(collector.CollectionError, 'completion record'):
                collector.record_checkpoint(cfg, ROOT, state_dir, end1 + HOUR, config_path=cfg_file)
            self.assertFalse(cp_path.exists())
            alt_cfg = dict(cfg, SOURCE_SSH_HOST='other-host.internal')
            with self.assertRaisesRegex(collector.CollectionError, 'completion record'):
                collector.record_checkpoint(alt_cfg, ROOT, state_dir, end1)
            self.assertFalse(cp_path.exists())

            # CLI main() reads source.env bytes only once per invocation and passes config_bytes through.
            real_read_regular = collector.read_regular_bytes
            cfg_reads = []
            def counting_read(path, *args, **kwargs):
                if Path(path) == cfg_file:
                    cfg_reads.append(path)
                return real_read_regular(path, *args, **kwargs)

            stdout = io.StringIO()
            with mock.patch.object(collector, 'read_regular_bytes', side_effect=counting_read), redirect_stdout(stdout):
                collector.main([
                    '--config', str(cfg_file),
                    '--state-dir', str(state_dir),
                    '--end', str(end1),
                    '--record-checkpoint',
                ])
            self.assertEqual(len(cfg_reads), 1)
            self.assertEqual(json.loads(stdout.getvalue()), {'status': 'checkpoint_recorded', 'observation_end': end1})
            self.assertEqual(stat.S_IMODE(cp_path.lstat().st_mode), 0o600)
            cfg_reads.clear()
            stdout_plan = io.StringIO()
            with mock.patch.object(collector, 'read_regular_bytes', side_effect=counting_read), redirect_stdout(stdout_plan):
                collector.main([
                    '--config', str(cfg_file),
                    '--state-dir', str(state_dir),
                    '--end', str(end1 + HOUR),
                    '--plan',
                ])
            self.assertEqual(len(cfg_reads), 1)
            self.assertEqual(json.loads(stdout_plan.getvalue())['catchup_days'], 1)

            # Mutually exclusive and required CLI argument combinations fail cleanly.
            bad_invocations = [
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--plan', '--record-checkpoint'],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(out_path), '--plan'],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(out_path), '--end', str(end1), '--record-checkpoint'],
                ['--config', str(cfg_file), '--state-dir', str(state_dir)],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--record-checkpoint'],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(out_path), '--previous-end', str(seed_end)],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(out_path), '--max-catchup-days', '5'],
            ]
            for argv in bad_invocations:
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exited:
                    collector.main(argv)
                self.assertEqual(exited.exception.code, 2)

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as exited:
                collector.main(['--config', str(cfg_file), '--state-dir', str(state_dir), '--plan', '--max-catchup-days', '0'])
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(stderr.getvalue(), 'Collection plan failed.\n')

    def test_forbid_overlap_ssh_and_seed_isolation(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            private_seed = root / 'private-seed.json'
            private_seed.write_text(json.dumps(base_v4))
            private_seed.chmod(0o600)
            cfg_file, id_file, kh_file = self._private_source_env(root, extra_env=f'SOURCE_SEED_SNAPSHOT="{private_seed}"\n')
            cfg = collector.load_source_config(cfg_file)
            state_dir = root / 'state'

            # Every protected artifact rejects overlap via --forbid-overlap, including symlink & nonexistent descendants.
            sym_parent = root / 'sym_parent'
            sym_parent.symlink_to(root)
            for forbidden in (
                cfg_file,
                id_file,
                kh_file,
                state_dir,
                state_dir / 'nested/missing',
                private_seed,
                sym_parent / 'state/nested/missing',
                sym_parent / 'kh',
            ):
                with self.subTest(forbidden=str(forbidden)), self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                    collector.plan_collection(cfg, ROOT, state_dir, end=seed_end, config_path=cfg_file, forbid_overlap=[forbidden])
                src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
                with self.subTest(collect_forbidden=str(forbidden)), self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                    collector.collect(cfg, ROOT, state_dir, root / 'out.json', end=seed_end, source=src,
                                      config_path=cfg_file, forbid_overlap=[forbidden])

            # SOURCE_SEED_SNAPSHOT overlapping state_dir or config_path fails closed.
            in_state_seed = state_dir / 'private-seed.json'
            state_dir.mkdir(mode=0o700)
            in_state_seed.write_text(json.dumps(base_v4))
            in_state_seed.chmod(0o600)
            cfg_in_state = dict(cfg, SOURCE_SEED_SNAPSHOT=str(in_state_seed))
            with self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                collector.plan_collection(cfg_in_state, ROOT, state_dir, end=seed_end, config_path=cfg_file)

            # Invalid SSH port or symlink SSH identity/known_hosts fails closed in --plan and --record-checkpoint.
            bad_port_cfg = dict(cfg, SOURCE_SSH_PORT='70000')
            with self.assertRaisesRegex(collector.CollectionError, 'port'):
                collector.plan_collection(bad_port_cfg, ROOT, state_dir, end=seed_end, config_path=cfg_file)
            with self.assertRaisesRegex(collector.CollectionError, 'port'):
                collector.record_checkpoint(bad_port_cfg, ROOT, state_dir, end=seed_end, config_path=cfg_file)

            sym_kh = root / 'sym-kh'
            sym_kh.symlink_to(kh_file)
            with self.assertRaisesRegex(collector.CollectionError, 'regular file'):
                collector.plan_collection(dict(cfg, SOURCE_SSH_KNOWN_HOSTS_FILE=str(sym_kh)),
                                          ROOT, state_dir, end=seed_end, config_path=cfg_file)

    def test_verifier_edge_cases_staggered_gaps_sidecars_wal_output_and_case_aliases(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, id_file, kh_file = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)

            # 1. Staggered/interleaved relay_population.daily gaps across multi-series/shared-query charts.
            staggered = copy.deepcopy(base_v4)
            for c_idx, chart in enumerate(staggered['charts']):
                for s_idx, series in enumerate(chart['series']):
                    pop_daily = series.get('relay_population', {}).get('daily', [])
                    if len(pop_daily) >= 6:
                        offset = (c_idx + s_idx) % 2
                        series['relay_population']['daily'] = [
                            row for i, row in enumerate(pop_daily)
                            if not (2 <= i < len(pop_daily) - 2 and i % 2 == offset)
                        ]
            stag_app = root / 'stag_app'
            (stag_app / 'inputs').mkdir(parents=True)
            (stag_app / 'inputs/snapshot.json').write_text(json.dumps(staggered))
            end_stag = seed_end + HOUR
            stag_plan = collector.plan_collection(cfg, stag_app, root / 'stag_state', end=end_stag, config_path=cfg_file)
            stag_planned = (stag_plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT
            stag_src = SyntheticSource(queries, _snapshot_handlers(staggered, routes, rate_val=1.0))
            stag_res = collector.collect(cfg, stag_app, root / 'stag_state', root / 'stag_out.json',
                                         end=end_stag, source=stag_src, config_path=cfg_file)
            self.assertEqual(stag_src.catalogue_calls, 1)
            self.assertLessEqual(stag_res['source_requests'] + stag_src.catalogue_calls, stag_planned)

            # 2. Output cannot overwrite source.env, SSH keys, known_hosts, state_dir itself, or SQLite sidecars.
            state_dir = root / 'state_out'
            for bad_out in (
                cfg_file,
                id_file,
                kh_file,
                state_dir,
                state_dir / 'observations.sqlite3-wal',
                state_dir / 'observations.sqlite3-shm',
                state_dir / 'observations.sqlite3-journal',
            ):
                src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
                with self.subTest(bad_out=str(bad_out)), self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                    collector.collect(cfg, ROOT, state_dir, bad_out, end=end_stag, source=src, config_path=cfg_file)

            # 3. Non-mutating locking inspection:
            #    (a) Rollback-journal DB works as warm under mode 0500 state_dir via ?mode=ro without creating sidecars.
            #    (b) WAL-mode DB without -shm (with or without -wal) never creates -shm during --plan, falls back cleanly to cold,
            #        and rejects --record-checkpoint; symlink/bad-mode sidecars and symlink checkpoint still fail closed.
            wal_end = seed_end + DAY + HOUR
            rb_state = root / 'rb_state'
            rb_src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            collector.collect(cfg, ROOT, rb_state, root / 'rb_out.json', end=wal_end, source=rb_src, config_path=cfg_file)
            rb_state.chmod(0o500)
            try:
                rb_plan = collector.plan_collection(cfg, ROOT, rb_state, end=wal_end + HOUR, config_path=cfg_file)
                self.assertEqual(rb_plan['catchup_days'], 1)
                for sfx in ('-wal', '-shm', '-journal'):
                    self.assertFalse((rb_state / f'observations.sqlite3{sfx}').exists())
            finally:
                rb_state.chmod(0o700)

            wal_state = root / 'wal_state'
            wal_src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            collector.collect(cfg, ROOT, wal_state, root / 'wal_out.json', end=wal_end, source=wal_src, config_path=cfg_file)
            db_path = wal_state / 'observations.sqlite3'
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.commit()
            wal_file = wal_state / 'observations.sqlite3-wal'
            shm_file = wal_state / 'observations.sqlite3-shm'
            # WAL with -wal present and no -shm must fall back cleanly to cold (2d) without creating -shm.
            shm_file.unlink(missing_ok=True)
            if not wal_file.exists():
                wal_file.write_bytes(b'\x00' * 32)
                wal_file.chmod(0o600)
            wal_only_plan = collector.plan_collection(cfg, ROOT, wal_state, end=wal_end + HOUR, config_path=cfg_file)
            self.assertEqual(wal_only_plan['catchup_days'], 2)
            self.assertFalse(shm_file.exists())
            with self.assertRaisesRegex(collector.CollectionError, 'shared-memory index'):
                collector.record_checkpoint(cfg, ROOT, wal_state, end=wal_end, config_path=cfg_file)

            # WAL without -wal or -shm under 0o500 also falls back to cold cleanly and creates no sidecars.
            wal_file.unlink(missing_ok=True)
            shm_file.unlink(missing_ok=True)
            wal_state.chmod(0o500)
            try:
                wal_plan = collector.plan_collection(cfg, ROOT, wal_state, end=wal_end + HOUR, config_path=cfg_file)
                self.assertEqual(wal_plan['catchup_days'], 2)
                self.assertFalse(wal_file.exists())
                self.assertFalse(shm_file.exists())
            finally:
                wal_state.chmod(0o700)

            for suffix in ('-wal', '-shm', '-journal'):
                sidecar = wal_state / f'observations.sqlite3{suffix}'
                sidecar.symlink_to(root / 'missing-sidecar')
                with self.subTest(symlink_sidecar=suffix), self.assertRaisesRegex(collector.CollectionError, 'sidecar'):
                    collector.plan_collection(cfg, ROOT, wal_state, end=wal_end + HOUR, config_path=cfg_file)
                with self.subTest(ckpt_symlink_sidecar=suffix), self.assertRaisesRegex(collector.CollectionError, 'sidecar'):
                    collector.record_checkpoint(cfg, ROOT, wal_state, end=wal_end, config_path=cfg_file)
                sidecar.unlink()
                sidecar.write_bytes(b'sidecar')
                sidecar.chmod(0o644)
                with self.subTest(mode_sidecar=suffix), self.assertRaisesRegex(collector.CollectionError, 'sidecar'):
                    collector.plan_collection(cfg, ROOT, wal_state, end=wal_end + HOUR, config_path=cfg_file)
                sidecar.unlink()

            # Symlink checkpoint.json fails closed with CollectionError during --plan.
            cp_file = wal_state / 'checkpoint.json'
            cp_file.unlink()
            cp_file.symlink_to(root / 'missing-cp.json')
            with self.assertRaisesRegex(collector.CollectionError, 'checkpoint'):
                collector.plan_collection(cfg, ROOT, wal_state, end=wal_end + HOUR, config_path=cfg_file)

            # 4. Existing/non-existent case aliases caught on case-insensitive filesystems (e.g. macOS APFS).
            existing_dir = root / 'ExistingDir'
            existing_dir.mkdir()
            case_insensitive = (root / 'existingdir').exists()
            self.assertEqual(collector._paths_overlap(root / 'ExistingDir/sub', root / 'existingdir/sub'), case_insensitive)
            self.assertEqual(collector._paths_overlap(root / 'ExistingDir/Sub/child', root / 'existingdir/sub'), case_insensitive)
            self.assertEqual(collector._paths_overlap(root / 'NonExistent/sub', root / 'nonexistent/sub'), case_insensitive)
            if case_insensitive:
                with self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                    collector.plan_collection(cfg, ROOT, root / 'StateCase/collector', end=seed_end,
                                              config_path=cfg_file, forbid_overlap=[root / 'statecase/collector'])

    def test_hourly_archive_atomic_transaction_auto_vacuum_and_pruning(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        target_end = seed_end + HOUR

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, _ = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            state_dir = root / 'state'

            # Oversized hourly archive fails before snapshot publication and rolls back cache writes.
            oversized_src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            with mock.patch.object(collector, 'MAX_ARCHIVE_ROW_BYTES', 16), \
                    self.assertRaisesRegex(collector.CollectionError, 'Archived hourly payload exceeds size limit'):
                collector.collect(cfg, ROOT, state_dir, root / 'out_oversized.json',
                                  end=target_end, source=oversized_src, config_path=cfg_file)
            self.assertFalse((root / 'out_oversized.json').exists())

            # Rollback on commit failure prevents snapshot publication, cleans temp files, and leaves zero orphan rows.
            fail_src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            with mock.patch.object(collector.ObservationCache, 'commit', side_effect=sqlite3.OperationalError('disk full')), \
                    self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                collector.collect(cfg, ROOT, state_dir, root / 'out_fail.json',
                                  end=target_end, source=fail_src, config_path=cfg_file)
            self.assertFalse((root / 'out_fail.json').exists())
            self.assertEqual(list(root.glob('.out_fail.json.*.tmp')), [])
            self.assertFalse((state_dir / 'checkpoint.json').exists())
            db_path = state_dir / 'observations.sqlite3'
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM hourly_archive').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM collection_completions').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)

            # Seed expired and permanent rows directly to verify selective 35-day raw 1h pruning.
            cutoff = target_end - collector.HOURLY_CACHE_RETENTION_DAYS * DAY
            with closing(sqlite3.connect(db_path)) as db:
                db.executemany(
                    'INSERT INTO observations VALUES (?, ?, ?, ?, ?)',
                    [
                        ('src', 'q', HOUR, cutoff - HOUR, '{}'),
                        ('src', 'q', HOUR, cutoff, '{}'),
                        ('src', 'q', HOUR, cutoff + HOUR, '{}'),
                        ('src', 'q', SAMPLED_HOUR_STEP, cutoff - 400 * DAY, '{}'),
                        ('src', 'q', DAY, cutoff - 400 * DAY, '{}'),
                    ],
                )
                db.executemany(
                    'INSERT INTO collection_completions VALUES (?, ?)',
                    [
                        ('other-source', cutoff - 400 * DAY),
                    ],
                )
                db.commit()

            src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            res = collector.collect(cfg, ROOT, state_dir, root / 'out_ok.json',
                                    end=target_end, source=src, config_path=cfg_file)
            self.assertTrue(res['cache_committed'])
            self.assertTrue(res['checkpoint_updated'])
            source_id = json.loads((state_dir / 'checkpoint.json').read_text())['source_identity_sha256']
            with closing(sqlite3.connect(db_path)) as db:
                rem_first = {r[0] for r in db.execute('SELECT endpoint FROM observations WHERE query_sha=? AND step=?', ('q', HOUR))}
                self.assertEqual(rem_first, {cutoff, cutoff + HOUR})

            # Insert an older completion row for active source_id in next collect pass (cutoff advances to cutoff + HOUR).
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('INSERT INTO collection_completions VALUES (?, ?)', (source_id, cutoff - HOUR))
                db.commit()
            res2 = collector.collect(cfg, ROOT, state_dir, root / 'out_ok2.json',
                                     end=target_end + HOUR, source=src, config_path=cfg_file)
            self.assertTrue(res2['cache_committed'])

            with closing(sqlite3.connect(db_path)) as db:
                rem_hour = {r[0] for r in db.execute('SELECT endpoint FROM observations WHERE query_sha=? AND step=?', ('q', HOUR))}
                self.assertEqual(rem_hour, {cutoff + HOUR})
                self.assertEqual(
                    db.execute('SELECT COUNT(*) FROM observations WHERE query_sha=? AND step=?', ('q', SAMPLED_HOUR_STEP)).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    db.execute('SELECT COUNT(*) FROM observations WHERE query_sha=? AND step=?', ('q', DAY)).fetchone()[0],
                    1,
                )
                other_comps = db.execute(
                    'SELECT observation_end FROM collection_completions WHERE source_identity=?', ('other-source',)
                ).fetchall()
                self.assertEqual(other_comps, [(cutoff - 400 * DAY,)])
                active_comps = sorted(r[0] for r in db.execute(
                    'SELECT observation_end FROM collection_completions WHERE source_identity=?', (source_id,)
                ).fetchall())
                self.assertEqual(active_comps, [target_end, target_end + HOUR])

                rows = db.execute(
                    'SELECT observation_end, payload_zlib FROM hourly_archive WHERE source_identity=? ORDER BY observation_end',
                    (source_id,),
                ).fetchall()
                self.assertEqual([r[0] for r in rows], [target_end, target_end + HOUR])
                decoded = json.loads(zlib.decompress(rows[0][1]))
                self.assertEqual(decoded['schema'], collector.HOURLY_ARCHIVE_SCHEMA)
                self.assertEqual(decoded['end'], target_end)
                self.assertEqual(decoded['window_seconds'], HOUR)
                self.assertIn('dircache_cohort', decoded)
                self.assertIn('snapshot_flags', decoded)
                self.assertIn('versions', decoded)
                self.assertIn('input_provenance', decoded)
                first_series = decoded['charts'][0]['series'][0]
                self.assertNotIn('history', first_series)
                self.assertEqual(set(first_series), {
                    'key', 'values', 'hourly_relays', 'hourly_expected_relays', 'current', 'relay_population_current',
                })
                self.assertEqual(set(first_series['relay_population_current']), {'hour', 'day'})
                for field_name in ('values', 'hourly_relays', 'hourly_expected_relays'):
                    self.assertEqual(len(first_series[field_name]), 1)
                    self.assertEqual(first_series[field_name][0][0], target_end)
                decoded2 = json.loads(zlib.decompress(rows[1][1]))
                second_series = decoded2['charts'][0]['series'][0]
                for field_name in ('values', 'hourly_relays', 'hourly_expected_relays'):
                    self.assertEqual(len(second_series[field_name]), 1)
                    self.assertEqual(second_series[field_name][0][0], target_end + HOUR)

            # Existing non-empty DB with auto_vacuum = NONE keeps NONE cleanly without VACUUM.
            legacy_db = root / 'legacy.sqlite3'
            with closing(sqlite3.connect(legacy_db)) as db:
                db.execute('CREATE TABLE legacy (x INTEGER)')
                db.commit()
                self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 0)
            legacy_db.chmod(0o600)
            cache = collector.ObservationCache(legacy_db, src, queries)
            try:
                cache._mem[('s', 'q', HOUR, cutoff - HOUR)] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
                cache._mem[('s', 'q', HOUR, target_end)] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}
                cache.prune_expired_hourly(cutoff)
                self.assertNotIn(('s', 'q', HOUR, cutoff - HOUR), cache._mem)
                self.assertIn(('s', 'q', HOUR, target_end), cache._mem)
                cache.incremental_vacuum()
                self.assertEqual(cache.db.execute('PRAGMA auto_vacuum').fetchone()[0], 0)
            finally:
                cache.close()

            # Populated freelist on INCREMENTAL DB consumes all requested vacuum steps and shrinks file size.
            vac_db = root / 'vac.sqlite3'
            vac_cache = collector.ObservationCache(vac_db, src, queries)
            try:
                vac_cache.db.executemany(
                    'INSERT INTO observations VALUES (?, ?, ?, ?, ?)',
                    [('s', f'q{i}', HOUR, target_end + i * HOUR, 'x' * 2048) for i in range(120)],
                )
                vac_cache.commit()
                size_peak = vac_db.stat().st_size
                vac_cache.db.execute('DELETE FROM observations WHERE endpoint < ?', (target_end + 100 * HOUR,))
                vac_cache.commit()
                freelist_before = vac_cache.db.execute('PRAGMA freelist_count').fetchone()[0]
                self.assertGreater(freelist_before, 20)
                vac_cache.incremental_vacuum(10)
                freelist_mid = vac_cache.db.execute('PRAGMA freelist_count').fetchone()[0]
                self.assertEqual(freelist_before - freelist_mid, 10)
                vac_cache.incremental_vacuum()
                freelist_after = vac_cache.db.execute('PRAGMA freelist_count').fetchone()[0]
                self.assertEqual(freelist_after, 0)
                self.assertLess(vac_db.stat().st_size, size_peak)
            finally:
                vac_cache.close()

    def test_export_hourly_archive_cli_and_validation(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        t1, t2, t3 = seed_end + HOUR, seed_end + 2 * HOUR, seed_end + 3 * HOUR

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, _ = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            state_dir = root / 'state'

            # Missing state_dir must NOT be created.
            with self.assertRaises(collector.CollectionError):
                collector.export_hourly_archive(cfg, ROOT, state_dir, root / 'missing.jsonl', config_path=cfg_file)
            self.assertFalse(state_dir.exists())

            src = SyntheticSource(queries, _snapshot_handlers(base_v4, routes, rate_val=1.0))
            for idx, end in enumerate((t1, t2, t3)):
                collector.collect(cfg, ROOT, state_dir, root / f'snap_{idx}.json', end=end, source=src, config_path=cfg_file)

            # Insert another source_identity row to prove filtering.
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                db.execute(
                    'INSERT INTO hourly_archive VALUES (?, ?, ?, ?)',
                    ('other-identity', t2, t2, sqlite3.Binary(zlib.compress(b'{"schema":1,"end":0,"charts":[]}'))),
                )
                db.commit()

            # Read-only 0o500 state_dir succeeds via CLI without opening SSH or touching state_dir.
            export_path = root / 'exports/archive.jsonl'
            state_dir.chmod(0o500)
            stdout = io.StringIO()
            try:
                with mock.patch.object(collector, 'SSHSource', side_effect=AssertionError('SSH must not open')), redirect_stdout(stdout):
                    collector.main([
                        '--config', str(cfg_file),
                        '--state-dir', str(state_dir),
                        '--export-hourly-archive', str(export_path),
                    ])
            finally:
                state_dir.chmod(0o700)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary, {
                'status': 'exported',
                'rows': 3,
                'first_observation_end': t1,
                'last_observation_end': t3,
            })
            self.assertEqual(stat.S_IMODE(export_path.lstat().st_mode), 0o600)
            exported_lines = [json.loads(ln) for ln in export_path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([row['end'] for row in exported_lines], [t1, t2, t3])

            # --start and --end slice inclusively (including endpoints before seed['end']).
            sliced_path = root / 'exports/sliced.jsonl'
            sliced = collector.export_hourly_archive(cfg, ROOT, state_dir, sliced_path, start=t2, end=t3, config_path=cfg_file)
            self.assertEqual(sliced, {'status': 'exported', 'rows': 2, 'first_observation_end': t2, 'last_observation_end': t3})
            self.assertEqual([json.loads(ln)['end'] for ln in sliced_path.read_text().splitlines()], [t2, t3])
            pre_seed = collector.export_hourly_archive(
                cfg, ROOT, state_dir, root / 'exports/pre_seed.jsonl',
                start=seed_end - 10 * DAY, end=seed_end - DAY, config_path=cfg_file,
            )
            self.assertEqual(pre_seed['rows'], 0)

            # Missing hourly_archive table exports 0 rows cleanly.
            no_table_state = root / 'no_table_state'
            no_table_state.mkdir(mode=0o700)
            with closing(sqlite3.connect(no_table_state / 'observations.sqlite3')) as db:
                db.execute('CREATE TABLE observations (source TEXT, query_sha TEXT, step INTEGER, endpoint INTEGER, payload TEXT)')
                db.commit()
            (no_table_state / 'observations.sqlite3').chmod(0o600)
            empty_res = collector.export_hourly_archive(cfg, ROOT, no_table_state, root / 'empty.jsonl', config_path=cfg_file)
            self.assertEqual(empty_res, {'status': 'exported', 'rows': 0, 'first_observation_end': None, 'last_observation_end': None})
            self.assertEqual((root / 'empty.jsonl').read_text(), '')

            # Symlink/directory target or --forbid-overlap rejected before touching output.
            sym_out = root / 'sym-out.jsonl'
            sym_out.symlink_to(export_path)
            with self.assertRaisesRegex(collector.CollectionError, 'regular file'):
                collector.export_hourly_archive(cfg, ROOT, state_dir, sym_out, config_path=cfg_file)
            with self.assertRaisesRegex(collector.CollectionError, 'overlaps'):
                collector.export_hourly_archive(cfg, ROOT, state_dir, root / 'blocked/out.jsonl',
                                                config_path=cfg_file, forbid_overlap=[root / 'blocked'])

            # Corrupt blob / mismatched end / oversized zip-bomb leave existing export untouched.
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                orig_blob = db.execute('SELECT payload_zlib FROM hourly_archive WHERE observation_end=?', (t2,)).fetchone()[0]
                bomb_blob = zlib.compress(b'{' + b'"a":"' + b'x' * (collector.MAX_ARCHIVE_ROW_BYTES + 128) + b'"}', level=9)
                for bad_label, bad_blob in (
                    ('corrupt', b'not-zlib'),
                    ('wrong-end', zlib.compress(b'{"schema":1,"end":0,"charts":[]}')),
                    ('zip-bomb', bomb_blob),
                ):
                    db.execute('UPDATE hourly_archive SET payload_zlib=? WHERE observation_end=?', (sqlite3.Binary(bad_blob), t2))
                    db.commit()
                    before = sliced_path.read_bytes()
                    with self.subTest(bad_label=bad_label), self.assertRaises(collector.CollectionError):
                        collector.export_hourly_archive(cfg, ROOT, state_dir, sliced_path, config_path=cfg_file)
                    self.assertEqual(sliced_path.read_bytes(), before)
                db.execute('UPDATE hourly_archive SET payload_zlib=? WHERE observation_end=?', (orig_blob, t2))
                db.commit()

            # Concurrent export + collection commit regression: read transactions are released between batches
            # so a concurrent commit in rollback-journal mode succeeds immediately without missing the archive endpoint.
            t4 = t3 + HOUR
            concurrent_out = root / 'exports/concurrent.jsonl'
            orig_decompress = collector._decompress_archive_row
            real_connect = collector.sqlite3.connect
            commit_done = False

            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                entry_source_id = db.execute(
                    'SELECT source_identity FROM hourly_archive WHERE observation_end=?', (t1,)
                ).fetchone()[0]

            def decompress_and_race(blob):
                nonlocal commit_done
                raw = orig_decompress(blob)
                if not commit_done:
                    commit_done = True
                    # Simulate concurrent collection commit with tiny 50ms busy timeout in rollback mode.
                    next_data = copy.deepcopy(base_v4)
                    next_data.update(start=t4 - DAY, end=t4)
                    with mock.patch.object(
                        collector.sqlite3, 'connect',
                        side_effect=lambda p, *a, **kw: real_connect(p, timeout=0.05),
                    ):
                        wcache = collector.ObservationCache(state_dir / 'observations.sqlite3', src, queries)
                        try:
                            wcache.record_hourly_archive(entry_source_id, t4, next_data)
                            wcache.record_completion(entry_source_id, t4)
                            wcache.commit()
                        finally:
                            wcache.close()
                return raw

            with mock.patch.object(collector, 'ARCHIVE_EXPORT_BATCH_SIZE', 1), \
                    mock.patch.object(collector, '_decompress_archive_row', side_effect=decompress_and_race):
                race_res = collector.export_hourly_archive(
                    cfg, ROOT, state_dir, concurrent_out, config_path=cfg_file,
                )
            self.assertTrue(commit_done)
            self.assertEqual(race_res['rows'], 4)
            self.assertEqual(
                [json.loads(ln)['end'] for ln in concurrent_out.read_text().splitlines()],
                [t1, t2, t3, t4],
            )
            with closing(sqlite3.connect(state_dir / 'observations.sqlite3')) as db:
                self.assertEqual(
                    db.execute(
                        'SELECT COUNT(*) FROM hourly_archive WHERE source_identity=? AND observation_end=?',
                        (entry_source_id, t4),
                    ).fetchone()[0],
                    1,
                )

            # Invalid CLI flags & bounds fail closed cleanly.
            for argv in (
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--plan', '--export-hourly-archive', str(export_path)],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(root / 'o.json'), '--export-hourly-archive', str(export_path)],
                ['--config', str(cfg_file), '--state-dir', str(state_dir), '--output', str(root / 'o.json'), '--start', str(t1)],
            ):
                with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exited:
                    collector.main(argv)
                self.assertEqual(exited.exception.code, 2)

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as exited:
                collector.main([
                    '--config', str(cfg_file), '--state-dir', str(state_dir),
                    '--export-hourly-archive', str(export_path), '--start', str(t3), '--end', str(t1),
                ])
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(stderr.getvalue(), 'Hourly archive export failed.\n')

    def test_thirteen_chart_seed_backfills_supplemental_charts_trims_leading_nulls_and_plans_budget(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed13 = copy.deepcopy(base_v4)
        seed13.pop('dos_thresholds', None)
        seed13['families'][0]['children'] = ['transport_rejections']
        seed13['charts'] = [c for c in seed13['charts'] if c['id'] not in ('incoming_connections', 'open_connections')]
        seed13_snapshot = copy.deepcopy(seed13)
        seed_end = int(seed13['end'])
        target_end = seed_end + 3 * HOUR
        first_observed_day = seed_end - 4 * DAY
        seed_first_daily = min(
            int(s['history']['daily'][0][0])
            for c in seed13['charts'] for s in c['series'] if s['history']['daily']
        )

        handlers = _snapshot_handlers(base_v4, routes, rate_val=10.0)
        # Simulate incoming_connections & open_connections starting only 5 days before target_end.
        for cid in ('incoming_connections', 'open_connections'):
            for period, stat_name in (
                ('daily', 'eligible'), ('daily', 'available'), ('daily', 'value'),
                ('sampled_hour', 'eligible'), ('sampled_hour', 'available'), ('sampled_hour', 'value'),
            ):
                qid = routes[f'{cid}.{period}.{stat_name}']
                orig = handlers[qid]
                cutoff = first_observed_day if period == 'daily' else (first_observed_day - DAY + SAMPLED_HOUR_STEP)
                handlers[qid] = lambda t, orig=orig, cutoff=cutoff: [] if t < cutoff else orig(t)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, _ = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            app_dir = root / 'app'
            (app_dir / 'inputs').mkdir(parents=True)
            (app_dir / 'catalogue').mkdir(parents=True)
            (app_dir / 'inputs/snapshot.json').write_text(json.dumps(seed13))
            (app_dir / 'catalogue/queries.json').write_text(json.dumps(queries))
            (app_dir / 'catalogue/routes.json').write_text(json.dumps(routes))

            plan = collector.plan_collection(cfg, app_dir, root / 'state', end=target_end, config_path=cfg_file)
            self.assertEqual(seed13, seed13_snapshot)
            planned_reqs = (plan['recommended_timeout_seconds'] - collector.COLLECTION_OVERHEAD_SECONDS) // collector.SOURCE_REQUEST_TIMEOUT

            src = SyntheticSource(queries, handlers)
            summary = collector.collect(cfg, app_dir, root / 'state', root / 'out.json',
                                        end=target_end, source=src, config_path=cfg_file)
            self.assertEqual(summary['charts'], 15)
            self.assertLessEqual(summary['source_requests'] + src.catalogue_calls, planned_reqs)
            collected = json.loads((root / 'out.json').read_text())
            collector.validate_snapshot(collected)

        self.assertEqual(collected['families'][0]['children'],
                         ['transport_rejections', 'incoming_connections', 'open_connections'])
        by_id = {c['id']: c for c in collected['charts']}
        inc = by_id['incoming_connections']
        oc = by_id['open_connections']
        self.assertEqual(inc['display_keys'], ['total', 'accepted', 'rejected'])
        self.assertEqual([s['key'] for s in inc['series']], ['total', 'accepted', 'rejected'])
        for s in (*inc['series'], *oc['series']):
            self.assertEqual(s['history']['daily'][0][0], first_observed_day)
            self.assertGreater(s['history']['daily'][0][0], seed_first_daily)
            self.assertEqual(s['history']['daily'][-1][0], seed_end)
            self.assertEqual(s['history']['sampled_hour'][0][0], first_observed_day - DAY + SAMPLED_HOUR_STEP)
            self.assertEqual(len(s['relay_population']['daily']), 5)
            self.assertIsNotNone(s['relay_population']['hour'])
            self.assertIsNotNone(s['relay_population']['day'])

        # If a newly added series has never been observed at all, leading-null trim yields empty daily & sampled_hour.
        worker_fixture = CollectorFixture()
        for cid in ('incoming_connections', 'open_connections'):
            for period in ('daily', 'sampled_hour', 'current24'):
                for stat_name in ('eligible', 'available', 'value'):
                    worker_fixture.source.handlers[worker_fixture.routes[f'{cid}.{period}.{stat_name}']] = lambda t: []
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = worker_fixture.instance(Path(folder) / 'empty_new.sqlite3')
            try:
                empty_data = worker.collect(END, 'a' * 64)
            finally:
                cache.close()
        empty_inc = next(c for c in empty_data['charts'] if c['id'] == 'incoming_connections')
        for s in empty_inc['series']:
            self.assertEqual(s['history']['daily'], [])
            self.assertEqual(s['history']['sampled_hour'], [])
            self.assertFalse(s['current']['day']['valid'])
            self.assertIsNone(s['relay_population']['day'])

    def test_incoming_connections_label_mapping_and_open_connections_scalar(self):
        fixture = CollectorFixture()
        for period in ('current_hour', 'sampled_hour', 'daily', 'current24'):
            fixture.source.handlers[fixture.routes[f'incoming_connections.{period}.value']] = lambda t: [
                ({'state': 'total'}, 120.0),
                ({'state': 'created'}, 100.0),
                ({'state': 'rejected'}, 20.0),
            ]
        for period in ('hour', 'day'):
            fixture.source.handlers[fixture.routes[f'incoming_connections.relay_population.{period}']] = lambda t: [
                ({'type': 'total', 'action': '59'}, 80),
                ({'type': 'created', 'action': '59'}, 80),
                ({'type': 'rejected', 'action': '59'}, 80),
            ]
            fixture.source.handlers[fixture.routes[f'open_connections.relay_population.{period}']] = lambda t: [
                ({'type': 'scalar', 'action': '59'}, 80),
            ]
        with tempfile.TemporaryDirectory() as folder:
            worker, cache = fixture.instance(Path(folder) / 'labels.sqlite3')
            try:
                data = worker.collect(END, 'a' * 64)
            finally:
                cache.close()
        by_id = {c['id']: c for c in data['charts']}
        inc_by_key = {s['key']: s for s in by_id['incoming_connections']['series']}
        self.assertEqual(inc_by_key['total']['current']['hour']['value'], 120.0)
        self.assertEqual(inc_by_key['accepted']['current']['hour']['value'], 100.0)
        self.assertEqual(inc_by_key['rejected']['current']['hour']['value'], 20.0)
        for key in ('total', 'accepted', 'rejected'):
            self.assertEqual(inc_by_key[key]['relay_population']['hour'], [END, 80, 80, 80, 0, 0, 80, 80])
        oc_series = by_id['open_connections']['series'][0]
        self.assertEqual(oc_series['current']['hour']['value'], 77.0)
        self.assertEqual(oc_series['relay_population']['hour'], [END, 80, 80, 80, 0, 0, 80, 80])

    def test_dos_thresholds_collection_unset_suppression_archive_and_route_guards(self):
        base_v4 = json.loads((ROOT / 'inputs/snapshot.json').read_text())
        queries = json.loads((ROOT / 'catalogue/queries.json').read_text())
        routes = json.loads((ROOT / 'catalogue/routes.json').read_text())
        seed_end = int(base_v4['end'])
        target_end = seed_end + HOUR

        def make_eff_metric(overrides):
            m = {}
            for key, _, _, _ in collector.DOS_EFFECTIVE_CONFIG_SETTINGS:
                m[f'{key}_source'] = 'consensus'
                m[key] = '1'
            m.update(overrides)
            return m

        # 240 total effective relays:
        # - dosconnectionenabled: 200 ('1', consensus), 24 ('0', torrc), 6 ('1', torrc) + 4 ('0', consensus) -> 10 suppressed -> 8/16 partitioned
        # - dosconnectionmaxconcurrentcount: 224 ('unset' omitted), 16 ('unset' with empty string '') -> 240 ('unset', unset)
        # - dosconnectionconnectrate: 232 ('unset' with numeric '0' kept), 8 ('25', torrc)
        # - dos_num_circ_max_outq: two small <8 buckets totaling 10 -> suppressed
        m_majority = make_eff_metric({
            'dosconnectionenabled': '1', 'dosconnectionenabled_source': 'consensus',
            'dosconnectionmaxconcurrentcount_source': 'unset',
            'dosconnectionconnectrate': '0', 'dosconnectionconnectrate_source': 'unset',
        })
        m_majority.pop('dosconnectionmaxconcurrentcount')

        m_torrc = make_eff_metric({
            'dosconnectionenabled': '0', 'dosconnectionenabled_source': 'torrc',
            'dosconnectionmaxconcurrentcount': '', 'dosconnectionmaxconcurrentcount_source': 'unset',
            'dosconnectionconnectrate': '25', 'dosconnectionconnectrate_source': 'torrc',
        })

        m_small_a = make_eff_metric({
            'dosconnectionenabled': '1', 'dosconnectionenabled_source': 'torrc',
            'dosconnectionmaxconcurrentcount_source': 'unset',
            'dosconnectionconnectrate': '0', 'dosconnectionconnectrate_source': 'unset',
            'dos_num_circ_max_outq': '2048', 'dos_num_circ_max_outq_source': 'torrc',
        })
        m_small_a.pop('dosconnectionmaxconcurrentcount')

        m_small_b = make_eff_metric({
            'dosconnectionenabled': '0', 'dosconnectionenabled_source': 'consensus',
            'dosconnectionmaxconcurrentcount_source': 'unset',
            'dosconnectionconnectrate': '0', 'dosconnectionconnectrate_source': 'unset',
            'dos_num_circ_max_outq': '1024', 'dos_num_circ_max_outq_source': 'torrc',
        })
        m_small_b.pop('dosconnectionmaxconcurrentcount')

        handlers = _snapshot_handlers(base_v4, routes, live_native=240, rate_val=5.0)
        handlers[routes['dos.effective_config']] = lambda t: [
            (m_majority, 200),
            (m_torrc, 24),
            (m_small_a, 5),
            (m_small_b, 5),
        ]
        # Total = 234 -> coarsen_count(234) = 232!
        handlers[routes['dos.begindir_config']] = lambda t: [
            ({'option': 'DoSBeginDirRateEnabled', 'value': 'auto'}, 72),
            ({'option': 'DoSBeginDirRateEnabled', 'value': '1'}, 5),
            ({'option': 'DoSBeginDirRateEnabled', 'value': '0'}, 3),
            ({'option': 'DoSBeginDirRate', 'value': '10'}, 64),
            ({'option': 'DoSBeginDirRate', 'value': '20'}, 16),
            # DoSBeginDirBurst omitted -> 0 relays, [], 0
            ({'option': 'DoSBeginDirDefenseType', 'value': '2'}, 4),
            ({'option': 'DoSBeginDirDefenseType', 'value': '1'}, 4),
        ]

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cfg_file, _, _ = self._private_source_env(root)
            cfg = collector.load_source_config(cfg_file)
            state_dir = root / 'state'
            out_path = root / 'dos_out.json'
            src = SyntheticSource(queries, handlers)
            # Bypass validate_matrix ONLY for the empty-string unset test by mocking access.aggregate_response?
            # Wait: m_torrc has empty string '' for dosconnectionmaxconcurrentcount which validate_matrix rejects;
            # verify direct Collector.dos_thresholds handles empty-string unset AND test real validate_matrix flow without ''.
            m_torrc_valid = dict(m_torrc)
            m_torrc_valid.pop('dosconnectionmaxconcurrentcount')
            handlers[routes['dos.effective_config']] = lambda t: [
                (m_majority, 200), (m_torrc_valid, 24), (m_small_a, 5), (m_small_b, 5),
            ]
            collector.collect(cfg, ROOT, state_dir, out_path, end=target_end, source=src, config_path=cfg_file)
            published = json.loads(out_path.read_text())
            collector.validate_snapshot(published)

            dt = published['dos_thresholds']
            self.assertEqual(dt['as_of'], target_end)
            self.assertEqual(dt['reported_relays'], 232)
            by_key = {s['key']: s for s in dt['settings']}
            self.assertEqual(by_key['dosconnectionenabled']['values'], [
                {'value': '1', 'source': 'consensus', 'relays': 200},
                {'value': '0', 'source': 'torrc', 'relays': 24},
            ])
            self.assertEqual(by_key['dosconnectionenabled']['suppressed_relays'], 8)
            self.assertEqual(by_key['dosconnectionmaxconcurrentcount']['values'], [
                {'value': 'unset', 'source': 'unset', 'relays': 232},
            ])
            self.assertEqual(by_key['dosconnectionconnectrate']['values'], [
                {'value': '0', 'source': 'unset', 'relays': 208},
                {'value': '25', 'source': 'torrc', 'relays': 24},
            ])
            self.assertEqual(by_key['dos_num_circ_max_outq']['values'], [
                {'value': '1', 'source': 'consensus', 'relays': 224},
            ])
            self.assertEqual(by_key['dos_num_circ_max_outq']['suppressed_relays'], 8)
            self.assertEqual(by_key['dosbegindirrateenabled']['relays'], 80)
            self.assertEqual(by_key['dosbegindirrateenabled']['values'], [
                {'value': 'auto', 'source': 'observed', 'relays': 72},
            ])
            self.assertEqual(by_key['dosbegindirrateenabled']['suppressed_relays'], 8)
            self.assertEqual(by_key['dosbegindirburst'], {
                'key': 'dosbegindirburst', 'option': 'DoSBeginDirBurst',
                'family_id': 'directory', 'unit': 'requests',
                'relays': 0, 'values': [], 'suppressed_relays': 0,
            })
            self.assertEqual(by_key['dosbegindirdefensetype'], {
                'key': 'dosbegindirdefensetype', 'option': 'DoSBeginDirDefenseType',
                'family_id': 'directory', 'unit': 'mode',
                'relays': 8, 'values': [], 'suppressed_relays': 8,
            })

            # Verify hourly_archive stores and exports dos_thresholds.
            export_path = root / 'archive.jsonl'
            collector.export_hourly_archive(cfg, ROOT, state_dir, export_path, config_path=cfg_file)
            archived_row = json.loads(export_path.read_text().splitlines()[0])
            self.assertEqual(archived_row['dos_thresholds'], dt)

            # Verify empty-string value alongside source=='unset' normalizes to 'unset' in dos_thresholds().
            worker_fixture = CollectorFixture()
            worker, cache = worker_fixture.instance(root / 'direct.sqlite3')
            try:
                with mock.patch.object(
                    worker, 'endpoint',
                    side_effect=lambda name, ts: (
                        {'data': {'result': [{'metric': m_torrc, 'values': [[ts, '16']]}]}}
                        if name == 'dos.effective_config'
                        else {'data': {'result': []}}
                    ),
                ):
                    direct_dt = worker.dos_thresholds(END, 80)
                direct_by_key = {s['key']: s for s in direct_dt['settings']}
                self.assertEqual(direct_by_key['dosconnectionmaxconcurrentcount']['values'], [
                    {'value': 'unset', 'source': 'unset', 'relays': 16},
                ])
            finally:
                cache.close()

            # Verify custom subset of routes omits supplemental charts and dos_thresholds cleanly.
            subset_fixture = CollectorFixture()
            subset_routes = {
                k: v for k, v in subset_fixture.routes.items()
                if not k.startswith(('incoming_connections.', 'open_connections.', 'dos.'))
            }
            subset_queries = {v: subset_fixture.queries[v] for v in set(subset_routes.values())}
            subset_src = SyntheticSource(subset_queries, subset_fixture.source.handlers)
            subset_cache = collector.ObservationCache(root / 'subset.sqlite3', subset_src, subset_queries)
            try:
                subset_worker = collector.Collector(subset_src, subset_cache, subset_queries, subset_routes, subset_fixture.seed)
                subset_data = subset_worker.collect(END, 'a' * 64)
            finally:
                subset_cache.close()
            self.assertEqual(len(subset_data['charts']), 4)
            self.assertNotIn('dos_thresholds', subset_data)
            self.assertGreater(
                collector._plan_max_requests(subset_routes, subset_queries, subset_fixture.seed, END, warm_end=None, baseline_end=END),
                0,
            )


if __name__ == '__main__':
    unittest.main()
