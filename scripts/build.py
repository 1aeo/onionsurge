#!/usr/bin/env python3
"""Build OnionSurge from one validated aggregate snapshot; no network access."""
import argparse
from bisect import bisect_left, bisect_right
import copy
import csv
from decimal import Decimal
from fractions import Fraction
import shutil
import stat
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

import display_numbers
from historical_statistics import historical_averages_and_windows, historical_comparison
from population_statistics import relay_counts
from io_helpers import BUILD_FORMAT, cleanup_warning, file_sha256, filesystem_contains, javascript_data, json_text, normalized_output_path, output_lock, owned_generation, parse_json, reject_working_directory_overlap, sha256_hex, staged_generation
from reference_statistics import describe_reference, frozen_reference, retained_peak
from time_ranges import DEFAULT_TIME_RANGES_PATH, load_time_ranges, resolve_period_bounds, validate_time_ranges
from validation import PUBLIC_VERSION_FIELDS, validate_snapshot
from windows import DAY, MINIMUM_POPULATION_COVERAGE, finite_number, require, usable_measurement, valid_rows

ROOT = Path(__file__).resolve().parents[1]


def scaled_reference_mean(mean, relays, scale):
    if mean is None or not relays:
        return None
    fleet_mean = mean * relays
    # Keep ordinary rounding unchanged; reorder only an overflowing intermediate.
    return fleet_mean * scale if finite_number(fleet_mean) else mean * (relays * scale)


def scaled_display(value, scale, relays=1):
    if value is None or not relays:
        return display_numbers.number(None)
    product = scaled_reference_mean(value, relays, scale) if relays != 1 else value * scale
    if not finite_number(product):
        return display_numbers.number(float('inf'))
    if product == 0 and value > 0 and relays > 0 and scale > 0:
        return display_numbers.number(Decimal(str(value)) * Decimal(int(relays)) * Decimal(str(scale)))
    return display_numbers.number(product)


def _validated_export_periods(charts, period_bounds):
    """Compute CSV window bounds and reject colliding exports/<chart_id>-<suffix>.csv paths."""
    period_exports = [
        (p['export_suffix'], p['window']['unit'] == 'all_time', None if p['window']['unit'] == 'all_time' else period_start)
        for p, period_start, _ in period_bounds
    ]
    seen = set()
    for chart in charts:
        for suffix, _, _ in period_exports:
            rel_path = f'exports/{chart["id"]}-{suffix}.csv'
            require(rel_path not in seen, f'Duplicate export path: {rel_path}')
            seen.add(rel_path)
    return period_exports


def prepare(snapshot, source_sha, cache, refresh=False, time_ranges_path=None):
    validate_snapshot(snapshot)
    tr_path = DEFAULT_TIME_RANGES_PATH if time_ranges_path is None else Path(time_ranges_path)
    tr_bytes = tr_path.read_bytes()
    time_ranges = validate_time_ranges(parse_json(tr_bytes))
    time_ranges_sha = sha256_hex(tr_bytes)
    data = copy.deepcopy(snapshot)
    data['start'] = int(data['start'])
    data['end'] = int(data['end'])
    day_end, cutoff, period_bounds = resolve_period_bounds(data['end'], time_ranges)
    avg_bounds = (day_end, period_bounds)
    _validated_export_periods(data['charts'], period_bounds)
    # Raw version-collector wrappers and optional audit notes are not website inputs.
    data['versions'] = {key: value for key, value in data['versions'].items() if key in PUBLIC_VERSION_FIELDS}
    data['versions']['as_of'] = int(data['versions']['as_of'])
    reference, encoded, ref_id, ref_sha = frozen_reference(
        data['charts'], data['end'], cache, source_sha, refresh=refresh,
        qualification={k: data.get('history_methods', {}).get(k) for k in
                       ['row_fields', 'counter', 'gauge', 'coverage', 'custom_directory', 'sampling', 'coarsening']})
    ref_end = reference['window']['end']
    labels = data['versions']['cohort_labels']
    cohorts = data['versions']['metric_cohorts']
    builds_meta = {
        key: ('Latest complete UTC day', 'Earlier running versions are not recorded.',
              '; '.join(f'{label}: {int(cohort[k])} relays' for k, label in labels.items() if cohort.get(k)))
        for key, cohort in cohorts.items()
    }
    for chart in data['charts']:
        ckey = chart['cohort_key']
        scope, hist_scope, display = builds_meta[ckey]
        chart['builds'] = {'display': display, 'cohort': dict(cohorts[ckey]),
                           'scope': scope, 'historical_scope': hist_scope}
        scale = chart['display_scale']
        for series in chart['series']:
            current = series['current']['day']
            expected = current.setdefault('expected_relays', max(8, chart['relays']))
            value, n = current.get('value'), current.get('relays')
            coverage = current.setdefault('coverage', n / expected if n and expected else None)
            current.setdefault('valid', usable_measurement(value, n)
                               and coverage is not None and MINIMUM_POPULATION_COVERAGE <= coverage <= 1)
            raw_daily = series['history']['daily']
            daily_rows = valid_rows(raw_daily, alignment=DAY)
            daily_ts = [row[0] for row in daily_rows]
            cutoff_end = bisect_right(daily_ts, cutoff)
            cutoff_rows = daily_rows[:cutoff_end]
            ratios = {row[0]: Fraction(row[1]) / int(row[2]) for row in daily_rows}
            series['reference'] = {
                'day': describe_reference(reference['distributions'][chart['id']][series['key']]['day'], current)
            }
            series['peaks'] = {
                'day': retained_peak(raw_daily, current, data['end'], valid_daily=daily_rows, ratios=ratios)
            }
            series['historical_averages'], population_windows = historical_averages_and_windows(
                raw_daily, data['end'], time_ranges=time_ranges, daily_rows=daily_rows, bounds=avg_bounds)
            series['relay_counts'] = relay_counts(series, data['end'], series['historical_averages'], population_windows)
            for period_def, _, comp_start in period_bounds:
                period = period_def['summary_key']
                summary = series['historical_averages'][period]
                summary['display'] = scaled_display(summary['value'], scale)
                comp_rows = (cutoff_rows if comp_start is None
                             else daily_rows[bisect_left(daily_ts, comp_start + DAY, 0, cutoff_end):cutoff_end])
                summary['comparison'] = historical_comparison(comp_rows, current, ref_end,
                                                              summary['status'] == 'ready', ratios=ratios)
            current['display'] = scaled_display(value if current['valid'] else None, scale)
            ref = series['reference']['day']
            current['reference_display'] = scaled_display(ref['mean_per_relay'], scale, n)
            peak = series['peaks']['day']
            current['peak_display'] = scaled_display(peak['fleet_peak'] if peak else None, scale)
            rank = ref['current_percentile']
            ref['display_rank'] = display_numbers.percentage(rank, suffix=False, boundaries=(90, 95, 99, 100)) if rank is not None else None
    data['schema_version'] = 6
    data['time_ranges'] = time_ranges
    data.update(reference_cutoff=ref_end, reference_window=reference['window'],
                reference_id=ref_id, reference_sha256=ref_sha, reference_document='reference/' + ref_id + '.json')
    data['provenance'] = {'aggregate_input_sha256': source_sha,
                          'transform_sha256': file_sha256(__file__),
                          'display_helper_sha256': file_sha256(display_numbers.__file__),
                          'reference_method_sha256': reference['method_sha256'],
                          'historical_averages_method_sha256': file_sha256(ROOT / 'scripts/historical_statistics.py'),
                          'relay_population_method_sha256': file_sha256(ROOT / 'scripts/population_statistics.py'),
                          'coarsening_method_sha256': file_sha256(ROOT / 'scripts/coarsening.py'),
                          'time_ranges_sha256': time_ranges_sha,
                          'time_ranges_method_sha256': file_sha256(ROOT / 'scripts/time_ranges.py'),
                          'query_contract_sha256': file_sha256(ROOT / 'catalogue/contract.json'),
                          'input_schema_version': snapshot['input_schema_version']}
    return data, encoded


def live_index(page):
    replacements = {
        'data-mode="offline"': 'data-mode="live"',
        '<meta name="robots" content="noindex,nofollow">': '',
        '<script src="data.js"></script>': '',
        '<script src="app.js"></script>': '<script src="app.js"></script><script src="live.js"></script>',
        'Local preview · saved measurements': 'Daily UTC measurements',
        '<a href="data.json" download>download the chart data and methods (JSON)</a>':
            '<a href="data/manifest.json" download>download the release manifest (JSON)</a>',
    }
    for marker, replacement in replacements.items():
        if page.count(marker) != 1:
            raise ValueError(f'Expected one live HTML marker: {marker}')
        page = page.replace(marker, replacement)
    return page


def write_outputs(data, reference_json, stage, *, live=False):
    time_ranges = validate_time_ranges(data['time_ranges']) if 'time_ranges' in data else load_time_ranges()
    data_end = data['end']
    _, _, period_bounds = resolve_period_bounds(data_end, time_ranges)
    period_exports = _validated_export_periods(data['charts'], period_bounds)
    site = ROOT / 'site'
    if any(path.is_symlink() for path in site.rglob('*')):
        raise ValueError('Site assets must be regular files, not symlinks')
    shutil.copytree(site, stage, dirs_exist_ok=True)
    if live:
        index = stage / 'index.html'
        index.write_text(live_index(index.read_text(encoding='utf-8')), encoding='utf-8')
    (stage / 'reference').mkdir(exist_ok=True)
    (stage / data['reference_document']).write_bytes(reference_json.encode('utf-8'))
    compact_json = json_text(data, pretty=False)
    (stage / 'data.json').write_text(compact_json if live else json_text(data, pretty=True), encoding='utf-8')
    (stage / 'data.js').write_text(javascript_data(data, compact_json=compact_json), encoding='utf-8')
    fields = ['id', 'title', 'family_id', 'definition', 'metrics', 'metric_origin', 'kind', 'unit',
              'display_unit', 'headline_unit', 'display_scale', 'query', 'quality_query', 'history_queries', 'builds']
    catalogue = {'families': data['families'], 'charts': [{k: c[k] for k in fields} for c in data['charts']],
                 'methods': data.get('history_methods')}
    (stage / 'catalogue.json').write_text(json_text(catalogue, pretty=True), encoding='utf-8')
    (stage / 'exports').mkdir(exist_ok=True)
    timestamp_labels = {}
    for chart in data['charts']:
        all_series_rows = [
            (series['key'], {row[0]: row[1] if row[4] else None for row in series['history']['daily'] if row[0] <= data_end})
            for series in chart['series']
        ]
        all_endpoints = sorted({t for _, mapping in all_series_rows for t in mapping})
        unit = chart['unit']
        for t in all_endpoints:
            if t not in timestamp_labels:
                timestamp_labels[t] = datetime.fromtimestamp(t, timezone.utc).isoformat()
        for suffix, is_all_time, period_start in period_exports:
            idx = 0 if is_all_time else bisect_right(all_endpoints, period_start)
            with (stage / 'exports' / f'{chart["id"]}-{suffix}.csv').open('w', encoding='utf-8', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['interval_end_utc', 'series', 'value', 'unit'])
                writer.writerows(
                    (timestamp_labels[t], series_key, mapping.get(t), unit)
                    for t in islice(all_endpoints, idx, None)
                    for series_key, mapping in all_series_rows
                )
    manifest = {'format': BUILD_FORMAT, 'mode': 'live' if live else 'offline', 'snapshot_end': data_end,
                'input_sha256': data['provenance']['aggregate_input_sha256'],
                'query_contract_sha256': data['provenance']['query_contract_sha256'],
                'files': {str(path.relative_to(stage)): file_sha256(path) for path in sorted(stage.rglob('*')) if path.is_file()}}
    (stage / 'build-manifest.json').write_text(json_text(manifest, pretty=True), encoding='utf-8')


def protected_output_path(output, source, cache, time_ranges_path=None):
    """Check stable path relationships without inspecting an output generation."""
    require(not filesystem_contains(output, ROOT), 'Output may not replace the repository or an ancestor')
    protected_paths = [ROOT / 'site', ROOT / 'scripts', ROOT / 'inputs', ROOT / 'catalogue', source, cache]
    if time_ranges_path is not None:
        protected_paths.append(Path(time_ranges_path))
    for protected in protected_paths:
        protected = protected.resolve()
        require(not (filesystem_contains(output, protected) or filesystem_contains(protected, output)),
                'Output overlaps source, input or reference cache')


def safe_output(output, source, cache, time_ranges_path=None):
    output = normalized_output_path(output)
    try:
        info = output.lstat()
    except FileNotFoundError:
        info = None
    require(info is None or not stat.S_ISLNK(info.st_mode), 'Output may not be a symlink')
    output = output.resolve()
    protected_output_path(output, source, cache, time_ranges_path)
    if info is not None:
        require(stat.S_ISDIR(info.st_mode), 'Output must be a directory')
        require(not any(output.iterdir()) or owned_generation(output),
                'Refusing to replace a nonempty directory not owned by this builder')
    return output


def check_missing_output_aliases(output, source, cache, time_ranges_path=None):
    """Probe naming rules while holding the registration or output lock."""
    if output.exists():
        return
    output.mkdir(mode=0o700)
    identity = None
    failure = None
    try:
        identity = output.stat()
        protected_output_path(output, source, cache, time_ranges_path)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            # Best effort detection; uncoordinated path replacement is outside
            # the cooperative-lock contract, including between stat and rmdir.
            if identity is not None:
                current = output.lstat()
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise OSError('Output probe was replaced; preserving the replacement')
            output.rmdir()
        except OSError as error:
            if failure is None:
                raise
            cleanup_warning(f'Output probe cleanup failed for {output}: {error}')


def build(source, output, cache, refresh=False, *, live=False, time_ranges_path=None):
    source, cache = Path(source).absolute(), Path(cache).resolve()
    tr_path = DEFAULT_TIME_RANGES_PATH if time_ranges_path is None else Path(time_ranges_path).absolute()
    output = normalized_output_path(output)
    protected_output_path(output, source, cache, tr_path)
    reject_working_directory_overlap(output)
    def validate_output(path):
        path = safe_output(path, source, cache, tr_path)
        check_missing_output_aliases(path, source, cache, tr_path)
        return path
    with output_lock(output, before_register=validate_output):
        # Ownership/type checks wait for any in-progress generation swap.
        output = validate_output(output)
        source_bytes = source.read_bytes()
        snapshot = parse_json(source_bytes)
        source_sha = sha256_hex(source_bytes)
        data, reference_json = prepare(snapshot, source_sha, cache, refresh, time_ranges_path=tr_path)
        with staged_generation(output) as stage:
            if live:
                write_outputs(data, reference_json, stage, live=True)
            else:
                write_outputs(data, reference_json, stage)
    return {'output': str(output), 'charts': len(data['charts']), 'families': len(data['families']),
            'snapshot_end': data['end'], 'reference_id': data['reference_id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'inputs/snapshot.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--cache', type=Path, default=ROOT / '.cache/reference')
    parser.add_argument('--time-ranges', type=Path, default=DEFAULT_TIME_RANGES_PATH)
    parser.add_argument('--refresh-reference', action='store_true')
    parser.add_argument('--live', action='store_true', help='Build the verified-release frontend; retain data artifacts for publication')
    args = parser.parse_args()
    try:
        print(json_text(build(args.input, args.output, args.cache, args.refresh_reference,
                              live=args.live, time_ranges_path=args.time_ranges)).strip())
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
        parser.exit(1, f'Build failed: {error}\n')


if __name__ == '__main__':
    main()
