import copy
from contextlib import contextmanager, redirect_stderr
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, SOURCE_PATH as SOURCE, clone_snapshot, strict_ranges
import build as builder
import io_helpers
from io_helpers import parse_json


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temporary.name)
        cls.output, cls.cache = cls.folder / 'dist', cls.folder / 'cache'
        builder.build(SOURCE, cls.output, cls.cache)
        cls.data = json.loads((cls.output / 'data.json').read_text())
        cls.input = json.loads(SOURCE.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_live_build_keeps_verified_data_but_loads_only_the_live_bootstrap(self):
        import package_previews
        with tempfile.TemporaryDirectory() as folder:
            output, cache = Path(folder) / 'live', Path(folder) / 'cache'
            builder.build(SOURCE, output, cache, live=True)
            page = (output / 'index.html').read_text()
            self.assertIn('data-mode="live"', page)
            self.assertNotIn('<script src="data.js">', page)
            self.assertIn('<script src="app.js"></script><script src="live.js"></script>', page)
            self.assertIn('<meta name="robots" content="noindex,nofollow">', page)
            self.assertNotIn('Local preview', page)
            self.assertIn('Hourly UTC measurements', page)
            self.assertIn('data/manifest.json', page)
            self.assertEqual(json.loads((output / 'data.json').read_text()), self.data)
            self.assertLess((output / 'data.json').stat().st_size, (self.output / 'data.json').stat().st_size)
            self.assertFalse((output / 'data.js').exists())
            self.assertFalse((output / 'catalogue.json').exists())
            with self.assertRaisesRegex(ValueError, 'Live builds cannot'):
                package_previews.package(output)
            manifest = json.loads((output / 'build-manifest.json').read_text())
            self.assertEqual(manifest['mode'], 'live')
            self.assertEqual(manifest['schema_version'], 7)
            self.assertNotIn('data.js', manifest['files'])
            self.assertNotIn('catalogue.json', manifest['files'])
            self.assertEqual(len(manifest['files']), 97)
            expected_contract_sha = hashlib.sha256((ROOT / 'catalogue/contract.json').read_bytes()).hexdigest()
            self.assertEqual(manifest['query_contract_sha256'], expected_contract_sha)
            expected_release_objects = sorted([
                'data.json',
                self.data['reference_document'],
                *(f"exports/{c['id']}-{p['export_suffix']}.csv"
                  for c in self.data['charts'] for p in self.data['time_ranges']['periods']),
            ])
            self.assertEqual(manifest['release_objects'], expected_release_objects)
            for name, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)
            offline_manifest = json.loads((self.output / 'build-manifest.json').read_text())
            self.assertEqual(offline_manifest['mode'], 'offline')
            self.assertEqual(offline_manifest['schema_version'], 7)
            self.assertIn('data.js', offline_manifest['files'])
            self.assertIn('catalogue.json', offline_manifest['files'])
            self.assertEqual(len(offline_manifest['files']), 99)
            self.assertEqual(offline_manifest['release_objects'], expected_release_objects)
            self.assertEqual(offline_manifest['query_contract_sha256'], expected_contract_sha)

    def test_source_measurements_and_queries_preserved(self):
        self.assertEqual(len(self.data['families']), 6)
        for chart, original in zip(self.data['charts'], self.input['charts'], strict=True):
            self.assertEqual(chart['query'], original['query'])
            self.assertEqual(chart['history_queries'], original['history_queries'])
            self.assertEqual(chart['relays'], sum(chart['builds']['cohort'].values()))
            for series, prior in zip(chart['series'], original['series'], strict=True):
                self.assertEqual(series['history'], prior['history'])
                self.assertEqual(series['current']['day']['value'], prior['current']['day']['value'])

    def test_reference_mean_scaling_avoids_intermediate_overflow(self):
        from decimal import Decimal
        self.assertAlmostEqual(builder.scaled_reference_mean(8e307, 8, 0.1) / 6.4e307, 1, places=14)
        snapshot = copy.deepcopy(self.input)
        chart = snapshot['charts'][0]
        # This legacy numeric fixture replaces the historical populations below.
        chart['series'][0].pop('relay_population', None)
        chart['display_scale'] = 1 / chart['relays']
        for rows in chart['series'][0]['history'].values():
            for row in rows:
                row[1:] = [8e307, chart['relays'], 1, True]
        chart['series'][0]['current']['day'].update(
            value=8e307, relays=chart['relays'], expected_relays=chart['relays'], coverage=1, valid=True)
        with tempfile.TemporaryDirectory() as folder:
            data, _ = builder.prepare(snapshot, 'a' * 64, Path(folder))
            current = data['charts'][0]['series'][0]['current']['day']
            expected = Decimal('8e307') / Decimal(chart['relays'])
            observed = Decimal(current['reference_display'].replace(',', ''))
            self.assertLess(abs(observed / expected - 1), Decimal('1e-14'))
            chart['display_scale'] = 10
            with self.assertRaisesRegex(ValueError, 'finite'):
                builder.prepare(snapshot, 'a' * 64, Path(folder))

    def test_build_refuses_to_replace_its_working_directory_or_ancestor(self):
        with tempfile.TemporaryDirectory() as folder:
            output, cache = Path(folder) / 'dist', Path(folder) / 'cache'
            builder.build(SOURCE, output, cache)
            (output / 'nested').mkdir()
            original = Path.cwd()
            before = (output / 'data.json').read_bytes()
            try:
                with mock.patch.object(builder, 'output_lock') as lock:
                    for current in (output, output / 'nested'):
                        os.chdir(current)
                        with self.assertRaisesRegex(ValueError, 'outside directory'):
                            builder.build(SOURCE, output, cache)
                        self.assertEqual(Path.cwd(), current.resolve())
                        self.assertEqual((output / 'data.json').read_bytes(), before)
                    lock.assert_not_called()
            finally:
                os.chdir(original)

    def test_absolute_build_can_run_from_a_removed_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            removed = root / 'removed'
            removed.mkdir()
            original = Path.cwd()
            try:
                os.chdir(removed)
                removed.rmdir()
                builder.build(SOURCE, root / 'dist', root / 'cache')
                self.assertTrue((root / 'dist' / 'index.html').is_file())
                with self.assertRaises(FileNotFoundError):
                    Path.cwd()
            finally:
                os.chdir(original)

    def test_reference_ranks_match_frozen_distribution_and_cutoff(self):
        ref = json.loads((self.output / self.data['reference_document']).read_text())
        for chart in self.data['charts']:
            for series in chart['series']:
                points = ref['distributions'][chart['id']][series['key']]['day']
                stats = series['reference']['day']
                self.assertEqual(stats['n'], len(points))
                self.assertTrue(all(p[0]-86400 >= ref['window']['start'] and p[0] <= ref['window']['end'] for p in points))
                self.assertLessEqual(ref['window']['end'], self.data['start'])
                current = series['current']['day']
                if not points or not current['valid']:
                    self.assertIsNone(stats['current_percentile'])
                    continue
                values = [p[1] for p in points]
                x = current['value'] / current['relays']
                if all(v == 0 for v in values) and x == 0:
                    self.assertIsNone(stats['current_percentile'])
                else:
                    rank = 100 * (sum(v < x for v in values) + .5*sum(v == x for v in values)) / len(values)
                    self.assertAlmostEqual(rank, stats['current_percentile'], places=10)

    def test_exports_match_qualified_daily_means_and_source_units(self):
        from time_ranges import DEFAULT_TIME_RANGES_PATH, period_window_bounds
        self.assertEqual(self.data['schema_version'], 7)
        periods = self.data['time_ranges']['periods']
        self.assertEqual(
            [p['id'] for p in periods],
            ['24h', '7d', '1mo', '3mo', '6mo', '12mo', 'all'],
        )
        expected_csvs = {
            f"{chart['id']}-{period['export_suffix']}.csv"
            for chart in self.data['charts']
            for period in periods
        }
        actual_csvs = {p.name for p in (self.output / 'exports').glob('*.csv')}
        self.assertEqual(actual_csvs, expected_csvs)
        self.assertEqual(len(actual_csvs), 91)
        self.assertEqual(
            self.data['provenance']['time_ranges_sha256'],
            hashlib.sha256(DEFAULT_TIME_RANGES_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            self.data['provenance']['time_ranges_method_sha256'],
            hashlib.sha256((ROOT / 'scripts/time_ranges.py').read_bytes()).hexdigest(),
        )
        self.assertEqual(
            self.data['provenance']['query_contract_sha256'],
            hashlib.sha256((ROOT / 'catalogue/contract.json').read_bytes()).hexdigest(),
        )
        def hourly_csv_pairs(csv_rows, target_series):
            actual = [
                (r['interval_end_utc'], '' if r['value'] == '' else float(r['value']))
                for r in csv_rows
                if r['series'] == target_series['key']
            ]
            expected = [
                (
                    builder.datetime.fromtimestamp(t, builder.timezone.utc).isoformat(),
                    '' if v is None else float(v),
                )
                for t, v in target_series['values']
            ]
            return actual, expected

        for chart in self.data['charts']:
            for period in periods:
                with (self.output / 'exports' / f"{chart['id']}-{period['export_suffix']}.csv").open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual({r['unit'] for r in rows}, {chart['unit']})
                self.assertEqual({r['series'] for r in rows}, {s['key'] for s in chart['series']})
                if rows and period['window']['unit'] != 'all_time':
                    period_start, _ = period_window_bounds(self.data['end'], period)
                    for r in rows:
                        ts = int(builder.datetime.fromisoformat(r['interval_end_utc']).timestamp())
                        self.assertGreater(ts, period_start)
                        self.assertLessEqual(ts, self.data['end'])
                for series in chart['series']:
                    if period['window']['unit'] == 'hours':
                        actual_pairs, expected_pairs = hourly_csv_pairs(rows, series)
                        self.assertEqual(actual_pairs, expected_pairs)
                        continue
                    values = [float(r['value']) for r in rows if r['series'] == series['key'] and r['value']]
                    summary = series['historical_averages'][period['summary_key']]
                    self.assertEqual(len(values), summary['days'])
                    if summary['status'] == 'ready':
                        self.assertAlmostEqual(sum(values) / len(values), summary['value'], places=7)
                    else:
                        self.assertIsNone(summary['value'])
                    self.assertEqual(
                        summary['value'] is not None,
                        summary['days'] > 0 and summary['coverage'] >= period['minimum_coverage'],
                    )

        # Missing hourly observations must preserve ordered timestamps with blank ('') cells.
        gapped = copy.deepcopy(self.data)
        gapped_chart = gapped['charts'][0]
        gapped_series = gapped_chart['series'][0]
        gapped_series['values'][3][1] = None
        gapped_series['values'][12][1] = None
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder) / 'stage'
            stage.mkdir()
            ref_json = (self.output / self.data['reference_document']).read_text()
            builder.write_outputs(gapped, ref_json, stage)
            with (stage / 'exports' / f"{gapped_chart['id']}-24h.csv").open() as handle:
                gap_rows = list(csv.DictReader(handle))
            actual_pairs, expected_pairs = hourly_csv_pairs(gap_rows, gapped_series)
            self.assertEqual(actual_pairs, expected_pairs)
            self.assertEqual(actual_pairs[3][1], '')
            self.assertEqual(actual_pairs[12][1], '')

    def test_partial_period_coverage_and_explicit_threshold_override(self):
        by_id = {chart['id']: chart for chart in self.data['charts']}
        periods = self.data['time_ranges']['periods']
        for chart_id in ('hsdir_fetches', 'directory_rejections', 'protocol_closes'):
            for series in by_id[chart_id]['series']:
                for period in periods:
                    if period['window']['unit'] == 'hours':
                        continue
                    key = period['summary_key']
                    summary = series['historical_averages'][key]
                    self.assertGreaterEqual(summary['days'], 1, (chart_id, series['key'], key))
                    self.assertEqual(summary['status'], 'ready', (chart_id, series['key'], key))
                    self.assertIsNotNone(summary['value'], (chart_id, series['key'], key))
                    comparison = summary['comparison']
                    if comparison['n'] > 0 and not comparison['flat_zero']:
                        self.assertIsNotNone(comparison['current_percentile'], (chart_id, series['key'], key))
                    self.assertIsNotNone(series['relay_counts'][key]['total'], (chart_id, series['key'], key))

        self.assertEqual(by_id['directory_rejections']['series'][0]['reference']['day']['n'], 19)

        for series in by_id['directory_requests']['series']:
            if series['key'] in {'hs_get', 'hs_post', 'no_request'}:
                continue
            for period in periods:
                if period['window']['unit'] == 'hours':
                    continue
                key = period['summary_key']
                summary = series['historical_averages'][key]
                self.assertEqual(summary['days'], 0, (series['key'], key))
                self.assertEqual(summary['status'], 'insufficient_data', (series['key'], key))
                self.assertIsNone(summary['value'], (series['key'], key))
                self.assertIsNone(summary['observed_start'], (series['key'], key))
                self.assertIsNone(summary['observed_end'], (series['key'], key))
                self.assertIsNone(series['relay_counts'][key]['total'], (series['key'], key))

        strict = strict_ranges()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            strict_path = root / 'strict_time_ranges.json'
            strict_path.write_text(json.dumps(strict))
            strict_data, _ = builder.prepare(
                clone_snapshot(self.input), 'a' * 64, root / 'cache', time_ranges_path=strict_path,
            )
        strict_charts = {chart['id']: chart for chart in strict_data['charts']}
        for chart_id in ('hsdir_fetches', 'directory_rejections'):
            series = strict_charts[chart_id]['series'][0]
            self.assertEqual(series['historical_averages']['seven_days']['status'], 'ready')
            self.assertEqual(series['historical_averages']['all_time']['status'], 'ready')
            for key in ('one_month', 'three_months', 'six_months', 'twelve_months'):
                self.assertEqual(series['historical_averages'][key]['status'], 'insufficient_data')
                self.assertIsNone(series['historical_averages'][key]['value'])
                self.assertIsNone(series['historical_averages'][key]['comparison']['current_percentile'])
                self.assertIsNone(series['relay_counts'][key]['total'])
        proto = strict_charts['protocol_closes']['series'][0]
        self.assertEqual(proto['historical_averages']['six_months']['status'], 'ready')
        self.assertEqual(proto['historical_averages']['twelve_months']['status'], 'insufficient_data')
        self.assertIsNone(proto['historical_averages']['twelve_months']['value'])
        self.assertIsNone(proto['relay_counts']['twelve_months']['total'])

    def test_manifest_and_script_json_integrity(self):
        manifest = json.loads((self.output/'build-manifest.json').read_text())
        for name, expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.output/name).read_bytes()).hexdigest(), expected)
        script = (self.output/'data.js').read_text()
        self.assertEqual(parse_json(script.removeprefix('window.ONIONSURGE_DATA = ').removesuffix(';\n')), self.data)
        self.assertNotIn('<', script)
        reference = (self.output / self.data['reference_document']).read_bytes()
        self.assertEqual(hashlib.sha256(reference).hexdigest(), self.data['reference_sha256'])
        self.assertTrue(reference.endswith(b'\n'))
        self.assertNotIn(b'\r\n', reference)
        self.assertFalse((self.output/'inputs').exists())
        self.assertFalse((self.output/'research').exists())

    def test_input_bytes_read_once_for_matching_data_and_provenance(self):
        original = Path.read_bytes
        calls = []
        source_bytes = SOURCE.read_bytes()
        def read(path):
            if path == SOURCE:
                calls.append(path)
                return source_bytes
            return original(path)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(Path, 'read_bytes', read):
            output = Path(folder)/'dist'
            builder.build(SOURCE, output, Path(folder)/'cache')
            result = json.loads((output/'data.json').read_text())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['provenance']['aggregate_input_sha256'], hashlib.sha256(source_bytes).hexdigest())

    def test_snapshot_is_read_after_output_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'snapshot.json'
            source.write_bytes(SOURCE.read_bytes())
            updated = json.loads(source.read_text())
            updated['generated_at'] = '2026-11-18T04:00:00+00:00'
            encoded = json.dumps(updated).encode()
            @contextmanager
            def acquired(directory, **kwargs):
                source.write_bytes(encoded)
                yield
            output = Path(folder)/'dist'
            with mock.patch.object(builder, 'output_lock', acquired):
                builder.build(source, output, Path(folder)/'cache')
            result = json.loads((output/'data.json').read_text())
            self.assertEqual(result['generated_at'], updated['generated_at'])
            self.assertEqual(result['provenance']['aggregate_input_sha256'], hashlib.sha256(encoded).hexdigest())

    def test_output_generation_is_inspected_only_under_the_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            output, cache = (Path(folder)/'dist').resolve(), (Path(folder)/'cache').resolve()
            builder.build(SOURCE, output, cache)
            locked = False
            original_iterdir = Path.iterdir
            @contextmanager
            def acquired(directory, **kwargs):
                nonlocal locked
                locked = True
                try:
                    yield
                finally:
                    locked = False
            def guarded_iterdir(path):
                if path == output and not locked:
                    raise FileNotFoundError('another builder moved the unlocked generation')
                return original_iterdir(path)
            with mock.patch.object(builder, 'output_lock', acquired), mock.patch.object(Path, 'iterdir', guarded_iterdir):
                builder.build(SOURCE, output, cache)
            self.assertTrue((output/'build-manifest.json').is_file())

    def test_non_object_ownership_markers_are_refused_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'output'; output.mkdir()
            for marker in [None, [], 'not an object', 42]:
                (output/'build-manifest.json').write_text(json.dumps(marker))
                with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, 'not owned'):
                    builder.safe_output(output, SOURCE, Path(folder)/'cache')

    def test_failed_backup_move_preserves_original_exception_and_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output, cache = (Path(folder)/'dist').resolve(), (Path(folder)/'cache').resolve()
            builder.build(SOURCE, output, cache)
            before = (output/'data.json').read_bytes()
            replace = os.replace
            def fail_move(source, destination):
                if Path(source) == output:
                    raise OSError('original backup move error')
                return replace(source, destination)
            with mock.patch.object(io_helpers.os, 'replace', fail_move), self.assertRaisesRegex(OSError, 'original backup move error'):
                builder.build(SOURCE, output, cache)
            self.assertEqual((output/'data.json').read_bytes(), before)

    def test_failed_promotion_restores_output_or_retains_recoverable_backup(self):
        for restore_fails in [False, True]:
            with self.subTest(restore_fails=restore_fails), tempfile.TemporaryDirectory() as folder:
                output, cache = (Path(folder)/'dist').resolve(), (Path(folder)/'cache').resolve()
                builder.build(SOURCE, output, cache)
                before = (output/'data.json').read_bytes()
                replace = os.replace
                def fail_promotion(source, destination):
                    if Path(source).name.startswith('.dist.staging-') and Path(destination) == output:
                        if restore_fails:
                            output.mkdir()
                            (output/'external-file').write_text('do not delete')
                        raise OSError('promotion denied')
                    return replace(source, destination)
                with mock.patch.object(io_helpers.os, 'replace', fail_promotion), self.assertRaises(OSError) as raised:
                    builder.build(SOURCE, output, cache)
                backups = list(Path(folder).glob('.dist.previous-*'))
                if restore_fails:
                    self.assertEqual(len(backups), 1)
                    self.assertEqual((backups[0]/'data.json').read_bytes(), before)
                    self.assertIn(str(backups[0]), str(raised.exception))
                    self.assertEqual((output/'external-file').read_text(), 'do not delete')
                else:
                    self.assertEqual(backups, [])
                    self.assertEqual((output/'data.json').read_bytes(), before)

    def test_recovery_path_survives_a_staging_cleanup_error(self):
        with tempfile.TemporaryDirectory() as folder:
            output, cache = (Path(folder)/'dist').resolve(), (Path(folder)/'cache').resolve()
            builder.build(SOURCE, output, cache)
            before = (output/'data.json').read_bytes()
            replace, rmtree = os.replace, shutil.rmtree
            errors = io.StringIO()
            def fail_promotion(source, destination):
                if Path(source).name.startswith('.dist.staging-') and Path(destination) == output:
                    output.mkdir()
                    (output/'external-file').write_text('do not delete')
                    raise OSError('promotion denied')
                return replace(source, destination)
            def fail_stage_cleanup(path, *args, **kwargs):
                if Path(path).name.startswith('.dist.staging-'):
                    raise OSError('staging cleanup denied')
                return rmtree(path, *args, **kwargs)
            with mock.patch.object(io_helpers.os, 'replace', fail_promotion), \
                    mock.patch.object(io_helpers.shutil, 'rmtree', fail_stage_cleanup), \
                    redirect_stderr(errors), self.assertRaises(OSError) as raised:
                builder.build(SOURCE, output, cache)
            backups = list(Path(folder).glob('.dist.previous-*'))
            self.assertEqual(len(backups), 1)
            self.assertIn(str(backups[0]), str(raised.exception))
            self.assertIn('promotion denied', str(raised.exception))
            self.assertEqual((backups[0]/'data.json').read_bytes(), before)
            self.assertIn('staging cleanup denied', errors.getvalue())

    def test_failed_generation_keeps_previous_complete_output(self):
        before = (self.output/'data.json').read_bytes()
        def fail(data, reference, stage):
            (stage/'partial.txt').write_text('unfinished')
            raise ValueError('deliberate generation failure')
        with mock.patch.object(builder, 'write_outputs', fail), self.assertRaisesRegex(ValueError, 'deliberate'):
            builder.build(SOURCE, self.output, self.cache)
        self.assertEqual((self.output/'data.json').read_bytes(), before)
        self.assertFalse((self.output/'partial.txt').exists())
        self.assertFalse(list(self.folder.glob('.dist.staging-*')))

    def test_bad_output_paths_cannot_replace_source_or_unowned_directory(self):
        for output in [ROOT, ROOT/'site', ROOT/'site/dist', ROOT/'inputs', ROOT/'scripts', ROOT/'catalogue', self.cache]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                builder.safe_output(output, SOURCE, self.cache)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'unowned'; output.mkdir(); (output/'precious').write_text('keep')
            with self.assertRaisesRegex(ValueError, 'not owned'):
                builder.build(SOURCE, output, Path(folder)/'cache')
            link = Path(folder)/'linked'; link.symlink_to(output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                builder.build(SOURCE, link, Path(folder)/'cache')
            self.assertEqual((output/'precious').read_text(), 'keep')
            tr_overlap = Path(folder)/'cfg_dir'
            tr_overlap.mkdir()
            tr_file = tr_overlap/'time_ranges.json'
            tr_file.write_bytes((ROOT/'inputs/time_ranges.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                builder.safe_output(tr_overlap, SOURCE, Path(folder)/'cache', tr_file)

    def test_terminal_parent_path_is_rejected_before_build_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'dist'; (output/'exports').mkdir(parents=True)
            with mock.patch.object(builder, 'output_lock') as lock:
                with self.assertRaisesRegex(ValueError, 'cannot end with'):
                    builder.build(SOURCE, output/'exports'/'..', Path(folder)/'cache')
                lock.assert_not_called()

    def test_unsupported_write_platforms_fail_before_creating_files(self):
        import package_previews
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for platform in ['win32', 'freebsd14']:
                with self.subTest(platform=platform), mock.patch.object(io_helpers.sys, 'platform', platform):
                    actions = [lambda: builder.build(SOURCE, root/'dist', root/'cache'),
                               lambda: package_previews.package(root/'dist'),
                               lambda: io_helpers.atomic_text(root/'nested/file.json', '{}')]
                    for action in actions:
                        with self.assertRaisesRegex(ValueError, 'Linux or macOS'):
                            action()
                    with self.assertRaisesRegex(ValueError, 'Linux or macOS'):
                        with io_helpers.file_lock(root/'nested/file.lock'):
                            self.fail('unsupported lock acquired')
                    with self.assertRaisesRegex(ValueError, 'Linux or macOS'):
                        with io_helpers.staged_generation(root/'dist'):
                            self.fail('unsupported staging created')
            self.assertEqual(list(root.iterdir()), [])

    def test_unowned_directory_actually_moved_to_backup_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); output, cache = root/'dist', root/'cache'
            builder.build(SOURCE, output, cache)
            writer = builder.write_outputs
            def replace_generation(data, reference, stage):
                writer(data, reference, stage)
                output.rename(root/'previous-owned')
                output.mkdir()
                (output/'user-data.txt').write_text('not a generated artifact')
            messages = io.StringIO()
            with mock.patch.object(builder, 'write_outputs', replace_generation), redirect_stderr(messages):
                builder.build(SOURCE, output, cache)
            backups = list(root.glob('.dist.previous-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0]/'user-data.txt').read_text(), 'not a generated artifact')
            self.assertIn(str(backups[0]), messages.getvalue())
            self.assertTrue((output/'build-manifest.json').is_file())
            builder.build(SOURCE, output, cache)
            self.assertEqual(list(root.glob('.dist.previous-*')), backups)

    def test_unreadable_ownership_and_failed_diagnostics_do_not_delete_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(Path, 'is_file', side_effect=PermissionError('no access')):
                self.assertFalse(io_helpers.owned_generation(root))
            with mock.patch('builtins.print', side_effect=OSError('closed stderr')):
                io_helpers.cleanup_warning('retained backup')

    def test_post_commit_cleanup_failure_keeps_the_complete_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); output, cache = root/'dist', root/'cache'
            builder.build(SOURCE, output, cache)
            remove = io_helpers.shutil.rmtree
            def deny_backup_cleanup(path, *args, **kwargs):
                if '.previous-' in Path(path).name:
                    raise PermissionError('backup cleanup denied')
                return remove(path, *args, **kwargs)
            messages = io.StringIO()
            with mock.patch.object(io_helpers.shutil, 'rmtree', deny_backup_cleanup), redirect_stderr(messages):
                result = builder.build(SOURCE, output, cache)
            self.assertEqual(result['snapshot_end'], self.input['end'])
            self.assertTrue((output/'build-manifest.json').is_file())
            self.assertEqual(len(list(root.glob('.dist.previous-*'))), 1)
            self.assertIn('backup cleanup denied', messages.getvalue())

    def test_cleanup_warning_ignores_closed_stderr(self):
        stream = io.StringIO(); stream.close()
        with mock.patch.object(io_helpers.sys, 'stderr', stream):
            io_helpers.cleanup_warning('preserve the original failure')

    def test_generation_roots_reject_overlap_but_allow_independent_siblings(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); parent = root/'public'; child = parent/'nested'
            with io_helpers.output_lock(parent):
                with self.assertRaisesRegex(ValueError, 'ancestor'):
                    with io_helpers.output_lock(child):
                        self.fail('nested output accepted')
                with io_helpers.output_lock(root/'sibling'):
                    pass
            self.assertFalse(child.exists())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); parent = root/'public'; child = parent/'nested'
            with io_helpers.output_lock(child):
                with self.assertRaisesRegex(ValueError, 'descendant'):
                    with io_helpers.output_lock(parent):
                        self.fail('parent output accepted')
            self.assertFalse((root/'.public.build.lock').exists())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); parent = root/'public'; parent.mkdir()
            (parent/'build-manifest.json').write_text(json.dumps({'format': builder.BUILD_FORMAT}))
            with self.assertRaisesRegex(ValueError, 'ancestor'):
                with io_helpers.output_lock(parent/'nested'):
                    self.fail('legacy generation ancestor accepted')

    def test_overlap_is_rechecked_before_promotion(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); output, cache = root/'dist', root/'cache'
            builder.build(SOURCE, output, cache)
            before = (output/'data.json').read_bytes()
            writer = builder.write_outputs
            def concurrent_claim(data, reference, stage):
                writer(data, reference, stage)
                (output/'.nested.build.lock').touch()
            with mock.patch.object(builder, 'write_outputs', concurrent_claim):
                with self.assertRaisesRegex(ValueError, 'descendant'):
                    builder.build(SOURCE, output, cache)
            self.assertEqual((output/'data.json').read_bytes(), before)

    def test_registration_race_rejects_parent_without_leaving_a_parent_claim(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); parent, child = root/'public', root/'public/nested'
            coordinator = root/'coordination.lock'
            checked, allow_child, parent_attempted, finish_child = [threading.Event() for _ in range(4)]
            parent_finished = threading.Event(); failures = {}; paused = False
            check, file_lock = io_helpers.reject_overlapping_outputs, io_helpers.file_lock
            def paused_check(directory):
                nonlocal paused
                check(directory)
                if directory == child and not paused:
                    paused = True; checked.set()
                    if not allow_child.wait(5):
                        raise TimeoutError('test child was not released')
            @contextmanager
            def observed_lock(path):
                if path == coordinator and threading.current_thread().name == 'parent-registration':
                    parent_attempted.set()
                with file_lock(path):
                    yield
            def child_job():
                try:
                    with io_helpers.output_lock(child):
                        if not finish_child.wait(5):
                            raise TimeoutError('test parent did not finish')
                        with io_helpers.staged_generation(child) as stage:
                            (stage/'complete').write_text('child generation')
                except BaseException as error:
                    failures['child'] = error
            def parent_job():
                try:
                    with io_helpers.output_lock(parent):
                        failures['parent'] = AssertionError('overlapping parent accepted')
                except ValueError:
                    pass
                except BaseException as error:
                    failures['parent'] = error
                finally:
                    parent_finished.set()
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator), \
                    mock.patch.object(io_helpers, 'reject_overlapping_outputs', paused_check), \
                    mock.patch.object(io_helpers, 'file_lock', observed_lock):
                child_thread = threading.Thread(target=child_job, daemon=True)
                parent_thread = threading.Thread(target=parent_job, name='parent-registration', daemon=True)
                child_thread.start()
                try:
                    self.assertTrue(checked.wait(5))
                    parent_thread.start()
                    self.assertTrue(parent_attempted.wait(5))
                    self.assertFalse(io_helpers.output_claim(parent).exists())
                    allow_child.set()
                    self.assertTrue(parent_finished.wait(5), 'registration lock held during generation work')
                    self.assertFalse(io_helpers.output_claim(parent).exists())
                finally:
                    allow_child.set(); finish_child.set()
                    child_thread.join(5)
                    if parent_thread.ident is not None:
                        parent_thread.join(5)
            self.assertEqual(failures, {})
            self.assertTrue((child/'complete').is_file())
            self.assertTrue(io_helpers.output_claim(child).is_file())

    def test_unreadable_overlap_scan_rejects_before_claim_creation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); output = root/'public'; output.mkdir()
            def unreadable(*args, **kwargs):
                kwargs['onerror'](PermissionError('unreadable descendant'))
                return iter(())
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=root/'coordination.lock'), \
                    mock.patch.object(io_helpers.os, 'walk', unreadable):
                with self.assertRaisesRegex(PermissionError, 'unreadable descendant'):
                    with io_helpers.output_lock(output):
                        self.fail('scan failure accepted')
            self.assertFalse(io_helpers.output_claim(output).exists())

    def test_registration_validation_failure_leaves_no_claim_and_allows_parent_retry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); parent = root/'public'; child = parent/'level/nested'
            coordinator = root/'registration.lock'
            held = set()
            file_lock = io_helpers.file_lock
            @contextmanager
            def tracked_lock(path):
                with file_lock(path):
                    held.add(path)
                    try:
                        yield
                    finally:
                        held.remove(path)
            def reject(path):
                self.assertEqual(path, child)
                self.assertIn(coordinator, held)
                self.assertFalse(io_helpers.output_claim(child).exists())
                raise ValueError('new root rejected before reservation')
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator), \
                    mock.patch.object(io_helpers, 'file_lock', tracked_lock):
                with self.assertRaisesRegex(ValueError, 'before reservation'):
                    with io_helpers.output_lock(child, before_register=reject):
                        self.fail('invalid root registered')
            self.assertFalse(io_helpers.output_claim(child).exists())
            self.assertFalse(parent.exists())
            with redirect_stderr(io.StringIO()):
                builder.build(SOURCE, parent, root/'cache')
            self.assertTrue((parent/'build-manifest.json').is_file())
            def check_under_lock(*args):
                self.assertIn(io_helpers.output_claim(parent), held)
            with mock.patch.object(io_helpers, 'file_lock', tracked_lock), \
                    mock.patch.object(builder, 'check_missing_output_aliases', side_effect=check_under_lock) as check:
                builder.build(SOURCE, parent, root/'cache')
                check.assert_called_once()  # Registered roots validate under their output lock.

    def test_invalid_output_type_and_ownership_leave_no_registration(self):
        for kind in ['file', 'unowned-directory']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); parent = root/'public'; output = parent/'nested'
                parent.mkdir()
                if kind == 'file':
                    output.write_text('keep this file')
                else:
                    output.mkdir(); (output/'foreign').write_text('keep this directory')
                with self.assertRaisesRegex(ValueError, 'must be a directory|not owned'):
                    builder.build(SOURCE, output, root/'cache')
                self.assertFalse(io_helpers.output_claim(output).exists())
                self.assertTrue(parent.is_dir())
                if kind == 'file':
                    self.assertEqual(output.read_text(), 'keep this file')
                    output.unlink()
                else:
                    self.assertEqual((output/'foreign').read_text(), 'keep this directory')
                    shutil.rmtree(output)
                with redirect_stderr(io.StringIO()):
                    builder.build(SOURCE, parent, root/'cache')
                self.assertTrue((parent/'build-manifest.json').is_file())

    def test_registration_parent_cleanup_preserves_primary_error_and_existing_paths(self):
        for failure in ['validation', 'mkdir', 'cleanup', 'nonempty']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); existing = root/'public'; existing.mkdir()
                output = existing/'first/second/dist'
                mkdir, rmdir = Path.mkdir, Path.rmdir
                def create(path, *args, **kwargs):
                    if failure == 'mkdir' and path == output.parent:
                        raise PermissionError('primary mkdir failure')
                    return mkdir(path, *args, **kwargs)
                def remove(path):
                    if failure == 'cleanup' and path == output.parent:
                        raise PermissionError('cleanup denied')
                    return rmdir(path)
                def reject(path):
                    if failure == 'nonempty':
                        (path.parent/'preserve').write_text('content prevents cleanup')
                    raise ValueError('primary validation failure')
                expected = PermissionError if failure == 'mkdir' else ValueError
                messages = io.StringIO()
                with mock.patch.object(Path, 'mkdir', create), mock.patch.object(Path, 'rmdir', remove), \
                        redirect_stderr(messages), self.assertRaisesRegex(expected, 'primary'):
                    with io_helpers.output_lock(output, before_register=reject):
                        self.fail('failed registration accepted')
                self.assertTrue(existing.is_dir())
                self.assertFalse(io_helpers.output_claim(output).exists())
                if failure in ['validation', 'mkdir']:
                    self.assertEqual(list(existing.iterdir()), [])
                else:
                    self.assertTrue(output.parent.is_dir())
                    self.assertIn('Registration cleanup failed', messages.getvalue())
                if failure == 'nonempty':
                    self.assertEqual((output.parent/'preserve').read_text(), 'content prevents cleanup')

    def test_strict_ownership_metadata_errors_propagate_without_boolean_predicates(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve(); marker = directory/'build-manifest.json'
            marker.write_text(json.dumps({'format': builder.BUILD_FORMAT}))
            lstat = Path.lstat
            for denied in [directory, marker]:
                def deny(path):
                    if path == denied:
                        raise PermissionError('ownership metadata denied')
                    return lstat(path)
                with self.subTest(path=denied), mock.patch.object(Path, 'lstat', deny), \
                        mock.patch.object(Path, 'is_dir', return_value=False), \
                        mock.patch.object(Path, 'is_file', return_value=False):
                    with self.assertRaisesRegex(PermissionError, 'metadata denied'):
                        io_helpers.owned_generation(directory, require_readable=True)
                    self.assertFalse(io_helpers.owned_generation(directory))
            marker.unlink()
            self.assertFalse(io_helpers.owned_generation(directory, require_readable=True))
            target = directory/'valid-manifest.json'
            target.write_text(json.dumps({'format': builder.BUILD_FORMAT}))
            marker.symlink_to(target)
            self.assertFalse(io_helpers.owned_generation(directory, require_readable=True))

    def test_registration_accepts_concurrently_created_directory_without_owning_it(self):
        for reject in [False, True]:
            with self.subTest(reject=reject), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); shared = root/'shared'; output = shared/'level/dist'
                mkdir = Path.mkdir
                injected = False
                def create(path, *args, **kwargs):
                    nonlocal injected
                    if path == shared and not injected:
                        injected = True
                        mkdir(path)  # Another job's cache creation wins this mkdir.
                    return mkdir(path, *args, **kwargs)
                def validate(path):
                    if reject:
                        raise ValueError('registration rejected')
                with mock.patch.object(Path, 'mkdir', create):
                    if reject:
                        with self.assertRaisesRegex(ValueError, 'registration rejected'):
                            with io_helpers.output_lock(output, before_register=validate):
                                self.fail('rejected registration accepted')
                    else:
                        with io_helpers.output_lock(output, before_register=validate):
                            pass
                self.assertTrue(injected)
                self.assertTrue(shared.is_dir())
                if reject:
                    self.assertEqual(list(shared.iterdir()), [])
                else:
                    self.assertTrue(io_helpers.output_claim(output).is_file())

    def test_registration_rejects_concurrent_parent_files_and_symlinks(self):
        for kind in ['file', 'symlink']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); shared = root/'shared'; output = shared/'level/dist'
                target = root/'target'; target.mkdir()
                mkdir = Path.mkdir
                def create(path, *args, **kwargs):
                    if path == shared:
                        if kind == 'file':
                            path.write_text('preserve')
                        else:
                            path.symlink_to(target, target_is_directory=True)
                    return mkdir(path, *args, **kwargs)
                with mock.patch.object(Path, 'mkdir', create), self.assertRaises(FileExistsError):
                    with io_helpers.output_lock(output):
                        self.fail('non-directory concurrent parent accepted')
                self.assertEqual(list(target.iterdir()), [])
                if kind == 'file':
                    self.assertEqual(shared.read_text(), 'preserve')
                else:
                    self.assertTrue(shared.is_symlink())

    def test_initial_probe_metadata_failure_cleans_up_and_preserves_primary_error(self):
        for cleanup_fails in [False, True]:
            with self.subTest(cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); parent = root/'public'; output = parent/'level/dist'
                mkdir, stat, rmdir = Path.mkdir, Path.stat, Path.rmdir
                created = False
                def create(path, *args, **kwargs):
                    nonlocal created
                    result = mkdir(path, *args, **kwargs)
                    if path == output:
                        created = True
                    return result
                def metadata(path, *args, **kwargs):
                    if path == output and created:
                        raise PermissionError('primary probe metadata failure')
                    return stat(path, *args, **kwargs)
                def remove(path):
                    if path == output and cleanup_fails:
                        raise PermissionError('probe cleanup denied')
                    return rmdir(path)
                messages = io.StringIO()
                with mock.patch.object(Path, 'mkdir', create), mock.patch.object(Path, 'stat', metadata), \
                        mock.patch.object(Path, 'rmdir', remove), redirect_stderr(messages), \
                        self.assertRaisesRegex(PermissionError, 'primary probe metadata failure'):
                    builder.build(SOURCE, output, root/'cache')
                self.assertTrue(created)
                self.assertFalse(io_helpers.output_claim(output).exists())
                if cleanup_fails:
                    self.assertTrue(output.is_dir())
                    self.assertIn('probe cleanup denied', messages.getvalue())
                else:
                    self.assertFalse(parent.exists())
                    builder.build(SOURCE, parent, root/'cache')
                    self.assertTrue((parent/'build-manifest.json').is_file())

    def test_unreadable_nested_marker_stops_publication_without_losing_output(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); output, cache = root/'dist', root/'cache'
            builder.build(SOURCE, output, cache)
            before = (output/'data.json').read_bytes()
            nested = output/'nested'; nested.mkdir()
            marker = nested/'build-manifest.json'
            marker.write_text(json.dumps({'format': builder.BUILD_FORMAT}))
            read = io_helpers.read_json
            def deny(path):
                if path == marker:
                    raise PermissionError('nested ownership marker is unreadable')
                return read(path)
            with mock.patch.object(io_helpers, 'read_json', deny):
                with self.assertRaisesRegex(PermissionError, 'marker is unreadable'):
                    builder.build(SOURCE, output, cache)
                self.assertFalse(io_helpers.owned_generation(nested))
                with self.assertRaisesRegex(PermissionError, 'marker is unreadable'):
                    io_helpers.reject_overlapping_outputs(nested/'child')
            self.assertEqual((output/'data.json').read_bytes(), before)
            self.assertTrue(marker.is_file())
            marker.unlink()
            io_helpers.reject_overlapping_outputs(output)  # A missing marker is allowed.

    def test_output_cannot_replace_the_registration_lock_tree(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); coordinator = root/'coordination/registration.lock'
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator):
                for output in [root, coordinator.parent, coordinator, coordinator/'nested']:
                    with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'registration lock'):
                        with io_helpers.output_lock(output):
                            self.fail('coordination path exposed to replacement')
            self.assertFalse(coordinator.parent.exists())

    def test_coordinator_identity_rejects_case_aliases_without_rejecting_distinct_siblings(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); coordinator = root/'onionsurge/generation-registration.lock'
            probe = root/'probe'; probe.mkdir()
            insensitive = (root/'PROBE').exists()
            probe.rmdir()
            output = root/'ONIONSURGE'
            if not insensitive:
                output.mkdir()
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator):
                if insensitive:
                    with self.assertRaisesRegex(ValueError, 'registration lock'):
                        with io_helpers.output_lock(output):
                            self.fail('case alias accepted')
                    self.assertFalse(io_helpers.output_claim(output).exists())
                    alias = coordinator.parent/'GENERATION-REGISTRATION.LOCK'
                    with self.assertRaisesRegex(ValueError, 'registration lock'):
                        with io_helpers.output_lock(alias):
                            self.fail('file alias accepted')
                else:
                    with io_helpers.output_lock(output):
                        pass
                    self.assertFalse(output.samefile(coordinator.parent))
                    self.assertTrue(io_helpers.output_claim(output).is_file())

    def test_readonly_coordinator_directory_reports_fixed_provisioning_path(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); coordinator = root/'account-cache/registration.lock'
            mkdir = Path.mkdir
            def deny(path, *args, **kwargs):
                if path == coordinator.parent:
                    raise PermissionError('read-only account directory')
                return mkdir(path, *args, **kwargs)
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator), \
                    mock.patch.object(Path, 'mkdir', deny):
                with self.assertRaisesRegex(ValueError, 'Provision a writable') as raised:
                    with io_helpers.output_lock(root/'dist'):
                        self.fail('unusable coordinator accepted')
            self.assertIn(str(coordinator.parent), str(raised.exception))
            self.assertFalse(io_helpers.output_claim(root/'dist').exists())

    def test_unsearchable_coordinator_reports_provisioning_before_creation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); coordinator = root/'account-cache/registration.lock'
            stat = Path.stat
            def deny(path, *args, **kwargs):
                if path == coordinator:
                    raise PermissionError('account cache cannot be searched')
                return stat(path, *args, **kwargs)
            with mock.patch.object(io_helpers, 'registration_lock_path', return_value=coordinator), \
                    mock.patch.object(Path, 'stat', deny):
                with self.assertRaisesRegex(ValueError, 'Provision a writable') as raised:
                    with io_helpers.output_lock(root/'dist'):
                        self.fail('unsearchable coordinator accepted')
            self.assertIsInstance(raised.exception.__cause__, PermissionError)
            self.assertIn(str(coordinator.parent), str(raised.exception))
            self.assertFalse(coordinator.parent.exists())
            self.assertFalse(io_helpers.output_claim(root/'dist').exists())

    def test_reserved_claim_components_are_rejected_before_parent_creation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for component in ['.dist.build.lock', '.DIST.BUILD.LOCK']:
                output = root/component/'nested'
                with self.subTest(component=component), self.assertRaisesRegex(ValueError, 'reserved'):
                    builder.build(SOURCE, output, root/'cache')
                self.assertFalse((root/component).exists())
                with self.assertRaisesRegex(ValueError, 'reserved'):
                    with io_helpers.output_lock(output):
                        self.fail('reserved component accepted')

    def test_resolved_missing_reserved_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); reserved = root/'.dist.build.lock'
            alias = root/'alias'; alias.symlink_to(reserved, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'reserved'):
                with io_helpers.output_lock(alias/'nested'):
                    self.fail('resolved reserved component accepted')
            self.assertFalse(reserved.exists())

    def test_nested_marker_lookup_uses_actual_filesystem_case_rules(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); output = root/'public'; nested = output/'nested'
            nested.mkdir(parents=True)
            marker = nested/'BUILD-MANIFEST.JSON'
            marker.write_text(json.dumps({'format': builder.BUILD_FORMAT}))
            if (nested/'build-manifest.json').exists():
                with self.assertRaisesRegex(ValueError, 'contains a generation'):
                    with io_helpers.output_lock(output):
                        self.fail('nested generation accepted')
                self.assertFalse(io_helpers.output_claim(output).exists())
            else:
                with io_helpers.output_lock(output):
                    pass
            self.assertTrue(marker.is_file())

    def test_first_build_rejects_missing_cache_alias_only_on_insensitive_volume(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); output, cache = root/'public/level/DIST', root/'public/level/dist/reference'
            probe = root/'probe'; probe.mkdir()
            insensitive = (root/'PROBE').exists()
            probe.rmdir()
            self.assertFalse(output.exists())
            self.assertFalse(cache.exists())
            if insensitive:
                with mock.patch.object(builder, 'prepare') as prepare:
                    with self.assertRaisesRegex(ValueError, 'reference cache'):
                        builder.build(SOURCE, output, cache)
                    prepare.assert_not_called()
                self.assertFalse(output.exists())
                self.assertFalse(cache.parent.exists())
                self.assertFalse(io_helpers.output_claim(output).exists())
                self.assertFalse((root/'public').exists())
                with redirect_stderr(io.StringIO()):
                    builder.build(SOURCE, root/'public', root/'cache')
                self.assertTrue((root/'public/build-manifest.json').is_file())
            else:
                builder.build(SOURCE, output, cache)
                self.assertTrue((output/'build-manifest.json').is_file())
                self.assertTrue(cache.is_dir())
                self.assertFalse(output.samefile(cache.parent))

    def test_missing_output_probe_detects_observed_replacement_and_preserves_primary_error(self):
        for fail_check in [False, True]:
            with self.subTest(fail_check=fail_check), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); output = root/'dist'
                def replace_probe(*args):
                    output.rename(root/'original-probe')
                    output.mkdir()
                    (output/'foreign').write_text('preserve this replacement')
                    if fail_check:
                        raise ValueError('primary protection error')
                expected = ValueError if fail_check else OSError
                message = 'primary protection error' if fail_check else 'probe was replaced'
                with mock.patch.object(builder, 'protected_output_path', replace_probe), \
                        redirect_stderr(io.StringIO()), self.assertRaisesRegex(expected, message):
                    builder.check_missing_output_aliases(output, SOURCE, root/'cache')
                self.assertEqual((output/'foreign').read_text(), 'preserve this replacement')
                self.assertTrue((root/'original-probe').is_dir())

    def test_clean_checkout_builds_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)/'project'; project.mkdir()
            for name in ['scripts', 'site', 'inputs', 'catalogue']:
                shutil.copytree(ROOT/name, project/name, ignore=shutil.ignore_patterns('__pycache__'))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1'); env.pop('PYTHONPATH', None)
            process = subprocess.run([sys.executable, '-B', str(project/'scripts/build.py')],
                                     cwd=folder, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads((project/'dist/data.json').read_text())
            self.assertEqual(result['end'], self.input['end'])
            self.assertEqual(len(result['charts']), len(self.input['charts']))

    def test_subnormal_scaled_display_and_integer_window_normalization(self):
        subnormal = float.fromhex('0x0.0000000000001p-1022')
        self.assertEqual(builder.scaled_display(subnormal, 0.1), '<1')
        self.assertEqual(builder.scaled_display(subnormal, 0.1, relays=2), '<1')
        self.assertEqual(builder.scaled_display(0.0, 0.1), '0')
        snapshot = copy.deepcopy(self.input)
        snapshot['start'] = float(snapshot['start'])
        snapshot['end'] = float(snapshot['end'])
        snapshot['versions']['as_of'] = float(snapshot['versions']['as_of'])
        with tempfile.TemporaryDirectory() as folder:
            built, _ = builder.prepare(snapshot, 'a'*64, Path(folder)/'cache')
        self.assertIs(type(built['start']), int)
        self.assertIs(type(built['end']), int)
        self.assertIs(type(built['versions']['as_of']), int)

    def test_duplicate_export_paths_across_chart_and_suffix_hyphens_are_rejected(self):
        colliding_ranges = {
            'version': 1,
            'default_range': 'rate-7d',
            'legacy_aliases': {'24h': 'rate-7d'},
            'card_slots': {'fallback_dynamic': 'rate-7d', 'fixed': ['7d', 'all']},
            'periods': [
                {
                    'id': 'rate-7d', 'summary_key': 'rate_seven_days', 'export_suffix': 'rate-7d',
                    'label': 'Rate 7d', 'average_label': 'Last 7d rate average', 'plot_label': 'Last 7d',
                    'window': {'unit': 'days', 'count': 7, 'comparison_lag_days': 1},
                    'minimum_coverage': 0.9, 'minimum_comparison_windows': 6,
                },
                {
                    'id': '7d', 'summary_key': 'seven_days', 'export_suffix': '7d',
                    'label': '7 days', 'average_label': 'Last 7 days average', 'plot_label': 'Last 7 days',
                    'window': {'unit': 'days', 'count': 7, 'comparison_lag_days': 1},
                    'minimum_coverage': 0.9, 'minimum_comparison_windows': 6,
                },
                {
                    'id': 'all', 'summary_key': 'all_time', 'export_suffix': 'all-time',
                    'label': 'All time', 'average_label': 'All-time average', 'plot_label': 'All time',
                    'window': {'unit': 'all_time', 'count': None, 'comparison_lag_days': 0},
                    'minimum_coverage': 0, 'minimum_comparison_windows': 30,
                },
            ],
        }
        snapshot = copy.deepcopy(self.input)
        old_0, old_1 = snapshot['charts'][0]['id'], snapshot['charts'][1]['id']
        snapshot['charts'][0]['id'] = 'connection'
        snapshot['charts'][1]['id'] = 'connection-rate'
        for family in snapshot['families']:
            family['children'] = [
                'connection' if cid == old_0 else 'connection-rate' if cid == old_1 else cid
                for cid in family['children']
            ]
            if family['primary'] == old_0:
                family['primary'] = 'connection'
            elif family['primary'] == old_1:
                family['primary'] = 'connection-rate'

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tr_path = root / 'colliding_time_ranges.json'
            tr_path.write_text(json.dumps(colliding_ranges))
            snap_path = root / 'snapshot.json'
            snap_path.write_text(json.dumps(snapshot))
            with self.assertRaisesRegex(ValueError, 'Duplicate export path: exports/connection-rate-7d.csv'):
                builder.build(snap_path, root / 'dist', root / 'cache', time_ranges_path=tr_path)
            self.assertFalse((root / 'dist').exists())

            # Direct write_outputs calls must also fail before writing any CSV files.
            colliding_data = copy.deepcopy(self.data)
            colliding_data['charts'][0]['id'] = 'connection'
            colliding_data['charts'][1]['id'] = 'connection-rate'
            colliding_data['time_ranges'] = colliding_ranges
            stage = root / 'stage'
            stage.mkdir()
            ref_json = (self.output / self.data['reference_document']).read_text()
            with self.assertRaisesRegex(ValueError, 'Duplicate export path: exports/connection-rate-7d.csv'):
                builder.write_outputs(colliding_data, ref_json, stage)
            self.assertEqual(list(stage.iterdir()), [])

    def test_daily_v3_snapshot_builds_schema7_without_hourly_fields(self):
        from time_ranges import FORBIDDEN_HOURLY_KEYS, load_time_ranges
        from windows import DAY
        v3 = clone_snapshot(self.input)
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

        full_ranges = load_time_ranges()
        daily_periods = [p for p in full_ranges['periods'] if p['window']['unit'] != 'hours']
        daily_ranges = {
            'version': 1,
            'default_range': '1mo',
            'legacy_aliases': {'24h': '1mo', '30d': '1mo'},
            'card_slots': {'fallback_dynamic': '1mo', 'fixed': ['7d', 'all']},
            'periods': daily_periods,
        }

        def walk_keys(value):
            stack = [value]
            found = set()
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    found.update(cur)
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
            return found

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snap_path = root / 'v3.json'
            snap_path.write_text(json.dumps(v3))
            daily_tr_path = root / 'daily_time_ranges.json'
            daily_tr_path.write_text(json.dumps(daily_ranges))

            # Daily inputs cannot declare an 'hours' time-range period.
            with self.assertRaisesRegex(ValueError, 'Hours time-range period'):
                builder.prepare(clone_snapshot(v3), 'a' * 64, root / 'bad-cache')

            off_out, cache = root / 'dist-daily', root / 'cache'
            builder.build(snap_path, off_out, cache, time_ranges_path=daily_tr_path)
            off_data = json.loads((off_out / 'data.json').read_text())
            off_ref = json.loads((off_out / off_data['reference_document']).read_text())
            self.assertEqual(off_data['schema_version'], 7)
            self.assertEqual(off_data['window_seconds'], DAY)
            self.assertFalse(FORBIDDEN_HOURLY_KEYS & walk_keys(off_data))
            self.assertFalse(FORBIDDEN_HOURLY_KEYS & walk_keys(off_ref))
            self.assertFalse(list((off_out / 'exports').glob('*-24h.csv')))
            self.assertEqual(len(list((off_out / 'exports').glob('*.csv'))), len(off_data['charts']) * len(daily_periods))
            for chart in off_data['charts']:
                self.assertEqual(chart['builds']['scope'], 'Latest complete UTC day')
                for series in chart['series']:
                    self.assertEqual(set(series['current']), {'day'})
                    self.assertEqual(set(series['reference']), {'day'})
                    self.assertEqual(set(series['peaks']), {'day'})
                    self.assertNotIn('hour', series['relay_counts'])
                    self.assertEqual(set(off_ref['distributions'][chart['id']][series['key']]), {'day'})

            live_out = root / 'dist-daily-live'
            builder.build(snap_path, live_out, cache, live=True, time_ranges_path=daily_tr_path)
            live_data = json.loads((live_out / 'data.json').read_text())
            self.assertEqual(live_data, off_data)
            live_page = (live_out / 'index.html').read_text()
            self.assertIn('<meta name="robots" content="noindex,nofollow">', live_page)
            self.assertIn('Daily UTC measurements', live_page)
            manifest = json.loads((live_out / 'build-manifest.json').read_text())
            self.assertEqual(manifest['schema_version'], 7)
            self.assertFalse(any(p.endswith('-24h.csv') for p in manifest['release_objects']))


if __name__ == '__main__':
    unittest.main()
