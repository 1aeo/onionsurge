"""Evaluate the generated queries with Prometheus's own synthetic test engine."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('population_catalogue', ROOT / 'scripts/build_catalogue.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PopulationCatalogueTests(unittest.TestCase):
    def test_generated_catalogue_emits_hour_and_day_population_routes(self):
        contract = json.loads((ROOT / 'catalogue/contract.json').read_text())
        generated = builder.build(contract)
        queries = generated['queries.json']
        routes = generated['routes.json']
        self.assertEqual(queries, json.loads((ROOT / 'catalogue/queries.json').read_text()))
        self.assertEqual(routes, json.loads((ROOT / 'catalogue/routes.json').read_text()))
        self.assertEqual(routes['directory_requests.relay_population.hour'], 'relay_population_directory_requests_hour')
        self.assertEqual(routes['directory_requests.relay_population.day'], 'relay_population_directory_requests_day')
        self.assertIn('[1h]', queries['relay_population_directory_requests_hour'])
        self.assertNotIn('hs_get|hs_post|no_request', queries['relay_population_directory_requests_hour'])
        self.assertIn('[1d]', queries['relay_population_directory_requests_day'])
        self.assertNotIn('hs_get|hs_post|no_request', queries['relay_population_directory_requests_day'])


@unittest.skipUnless(shutil.which('promtool'), 'promtool is needed for PromQL semantic tests')
class PopulationPromQLTests(unittest.TestCase):
    def evaluate(self, group, inputs, expected, window='1d', gate=360, interval='2m'):
        case = {'evaluation_interval': interval, 'tests': [{
            'interval': interval, 'input_series': inputs, 'promql_expr_test': [{
                'expr': builder.population_query(group, window, gate), 'eval_time': window,
                'exp_samples': [{'labels': f'{{action="{code}",type="{kind}"}}', 'value': count}
                                for kind, code, count in expected]}]}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'population.json'
            path.write_text(json.dumps(case))  # JSON is also valid YAML.
            result = subprocess.run(['promtool', 'test', 'rules', str(path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def source(instance, labels='', values='0+1x720'):
        return {'series': f'population_test{{job="tor_relays",instance="{instance}"{labels}}}', 'values': values}

    @staticmethod
    def flags(instance, values, points=720, omit=(), extra=''):
        return [{'series': f'tor_relay_flag{{job="tor_relays",instance="{instance}",type="{role}"{extra}}}',
                 'values': ' '.join(['NaN'] * (points + 1)) if value == 'NaN' else f'{value}x{points}'}
                for role, value in values.items() if role not in omit]

    def test_binary_agreeing_flags_missing_conflicting_and_last_within_window(self):
        middle = {'Running': 1, 'Guard': 0, 'Exit': 0, 'HSDir': 0, 'V2Dir': 0}
        guard = dict(middle, Guard=1, HSDir=1, V2Dir=1)
        inputs = []
        cases = [('guard', guard, ()), ('middle', middle, ()), ('exit', dict(middle, Exit=1, V2Dir=1), ()),
                 ('nonrunning', dict(guard, Running=0), ()), ('zero_flags', dict(middle, Running=0), ()), ('missing', guard, ('HSDir',)),
                 ('duplicate', guard, ()), ('agreeing_duplicate', guard, ()), ('nan_duplicate', guard, ()),
                 ('nonbinary', dict(guard, V2Dir=2), ()),
                 ('invalidduplicate', guard, ()), ('absent', {}, ()),
                 ('offline', dict(middle, Guard=1, V2Dir=1), ())]
        for name, flags, omit in cases:
            points = 717 if name == 'offline' else 720
            inputs.append(self.source(name, ',type="probe"', f'0+1x{points}'))
            inputs.extend(self.flags(name, flags, points, omit))
        inputs.extend(self.flags('duplicate', {'Guard': 0}, extra=',copy="synthetic"'))
        inputs.extend(self.flags('agreeing_duplicate', guard, extra=',copy="synthetic"'))
        inputs.extend(self.flags('nan_duplicate', {'Guard': 'NaN'}, extra=',copy="synthetic"'))
        inputs.extend(self.flags('invalidduplicate', {'Guard': 2}, extra=',copy="synthetic"'))
        inputs.append(self.source('low_samples', ',type="probe"', '0+1x350'))
        inputs.extend(self.flags('low_samples', guard))
        inputs.append(self.source('guard', ',type="other"'))
        self.evaluate({'selector': 'population_test{job="tor_relays"}', 'label': 'type'}, inputs,
                      [('probe', 0, 6), ('probe', 1, 1), ('probe', 27, 1), ('probe', 33, 1), ('probe', 51, 1), ('probe', 53, 1), ('probe', 59, 2), ('other', 59, 1)])

    def test_custom_day_requires_three_qualified_strata(self):
        inputs = []
        for name, strata in [('complete', 3), ('partial', 2), ('one_short', 3), ('unclassified', 3)]:
            for i in range(strata):
                points = 359 if name == 'one_short' and i == 2 else 360
                inputs.append(self.source(name, f',type="hs_get",prev_hop="stratum_{i}"', f'0+1x{points}'))
            if name != 'unclassified':
                inputs.extend(self.flags(name, {'Running': 1, 'Guard': 1, 'Exit': 0, 'HSDir': 1, 'V2Dir': 1}, 360))
        self.evaluate({'selector': 'population_test{job="tor_relays"}', 'label': 'type', 'strata': True}, inputs,
                      [('hs_get', 0, 1), ('hs_get', 59, 1)], '1d', 360, '4m')

    def test_action_class_is_preserved_before_histogram_label_and_scalar_sentinel(self):
        flags = {'Running': 1, 'Guard': 1, 'Exit': 0, 'HSDir': 1, 'V2Dir': 1}
        inputs = [self.source('classified', ',action="success"'), self.source('classified', ',action="not_found"'),
                  self.source('unknown', ',action="success"'), *self.flags('classified', flags)]
        self.evaluate({'selector': 'population_test{job="tor_relays"}', 'label': 'action'}, inputs,
                      [('success', 59, 1), ('success', 0, 1), ('not_found', 59, 1)])
        self.evaluate({'selector': 'population_test{job="tor_relays"}', 'label': ''},
                      [self.source('classified'), *self.flags('classified', flags)], [('scalar', 59, 1)])


if __name__ == '__main__':
    unittest.main()
