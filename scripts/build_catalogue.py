#!/usr/bin/env python3
"""Validate and deterministically export the reviewed public query contract."""
import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from io_helpers import atomic_text, json_text
import query_protocol as access
from windows import nonblank, require

CONTRACT_FIELDS = {'schema_version', 'project', 'usage', 'postprocessing', 'rules', 'queries', 'operations', 'relay_population_groups'}
OPERATION_KEY = re.compile(r'^[a-z0-9][a-z0-9._-]{0,95}\Z')
SLUG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,95}\Z')


def population_query(group, window, samples):
    """Count eligible sources and their last recorded, complete flag sets."""
    eligible = f'count_over_time({group["selector"]}[{window}]) >= {samples}'
    if group.get('strata'):
        eligible = f'count by(instance,type)({eligible}) == 3'
    eligible = f'(({eligible}) > bool 0)'
    if group['label'] == 'action':
        eligible = f'label_replace({eligible},"type","$1","action","(.+)")'
    elif not group['label']:
        eligible = f'label_replace({eligible},"type","scalar","",".*")'

    def flags(types, exact=False):
        operator = '=' if exact else '=~'
        return f'last_over_time(tor_relay_flag{{job="tor_relays",type{operator}"{types}"}}[{window}])'

    all_flags = flags('Running|Guard|Exit|HSDir|V2Dir')
    agree = f'min by(instance,type)({all_flags}) == max by(instance,type)({all_flags})'
    # min/max can ignore NaN in mixed inputs. Require every recorded value,
    # including duplicate streams, to pass the binary check as well.
    binary = (f'count by(instance,type)({all_flags}) == '
              f'count by(instance,type)({all_flags} == 0 or {all_flags} == 1)')
    classified = f'count by(instance)(({agree}) and on(instance,type) ({binary})) == 5'
    # Codes are exact small integers: zero is unclassified; otherwise bit 0
    # marks complete flag observations; bits 1..5 are G, E, HSDir, V2Dir, Running.
    weighted = ' or '.join(f'({bit} * min by(instance,type)({flags(role, True)}))'
                           for role, bit in [('Guard', 2), ('Exit', 4), ('HSDir', 8), ('V2Dir', 16), ('Running', 32)])
    codes = f'(1 + sum by(instance)({weighted})) and on(instance) ({classified})'
    observed = (f'(({eligible}) * on(instance) group_left() ({codes}))'
                f' or on(instance,type) ({eligible} * 0)')
    # Only aggregate code frequencies leave the gateway. Retaining an explicit
    # zero bin distinguishes unclassified contributors from missing telemetry.
    return f'sum by(type,action)(count_values("action",({observed})) by(type))'


def build(contract):
    require(isinstance(contract, dict) and set(contract) <= CONTRACT_FIELDS, 'Invalid contract structure')
    require(type(contract.get('schema_version')) is int and contract['schema_version'] == 1, 'Unsupported contract schema')
    require(isinstance(contract.get('queries'), dict) and isinstance(contract.get('operations'), dict),
            'Queries and operations must be objects')
    queries, routes = dict(contract['queries']), dict(contract['operations'])
    groups = contract.get('relay_population_groups', {})
    require(isinstance(groups, dict), 'Relay population groups must be an object')
    for name, group in groups.items():
        require(isinstance(name, str) and SLUG.fullmatch(name) and isinstance(group, dict), 'Invalid relay population group')
        require(nonblank(group.get('selector')), 'Invalid relay population selector')
        require(group.get('label') in ('', 'type', 'action'), 'Unsupported population grouping label')
        charts = group.get('charts')
        require(isinstance(charts, list) and charts and all(isinstance(c, str) and SLUG.fullmatch(c) for c in charts),
                'Invalid relay population chart list')
        key = f'relay_population_{name}_day'
        require(key not in queries, 'Duplicate generated query ID')
        queries[key] = population_query(group, '1d', 360)
        for chart in charts:
            operation = f'{chart}.relay_population.day'
            require(operation not in routes, 'Duplicate population operation')
            routes[operation] = key
    access.validate_catalogue(queries)
    require(routes and all(isinstance(k, str) and OPERATION_KEY.fullmatch(k) and isinstance(v, str) and v in queries
                           for k, v in routes.items()),
            'Operation references an unknown fixed query ID')
    require(set(queries) == set(routes.values()), 'Every query must have a declared operation')
    expected = {'max_range_seconds': access.MAX_RANGE, 'max_points': access.MAX_POINTS,
                'step_seconds': list(access.STEPS), 'query_timeout_seconds': 25, 'max_response_bytes': access.MAX_RESPONSE}
    require(contract.get('rules') == expected, 'Query contract and gateway limits differ')
    return {'queries.json': dict(sorted(queries.items())), 'routes.json': dict(sorted(routes.items()))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = build(access.load_json(ROOT / 'catalogue/contract.json'))
    umask = os.umask(0o077)
    os.umask(umask)
    for filename, content in result.items():
        path = ROOT / 'catalogue' / filename
        if args.check:
            if access.load_json(path) != content:
                raise ValueError(filename + ' differs from reviewed contract')
        else:
            atomic_text(path, json_text(content, pretty=True), mode=0o666 & ~umask)
    print(f"Verified {len(result['queries.json'])} fixed query IDs; {len(result['routes.json'])} operations.")


if __name__ == '__main__':
    main()
