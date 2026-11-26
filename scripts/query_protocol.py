"""Fixed-query PromQL protocol, strict JSON decoding, and HTTP validation helpers."""
import json
import math
import os
import re
import shlex
import stat
import urllib.parse
import urllib.request

MAX_RESPONSE = 8 * 1024**2
MAX_POINTS = 169
MAX_RANGE = 7 * 86400
HOUR = 3600
SAMPLED_HOUR_STEP = 21600
DAY = 86400
STEPS = (HOUR, SAMPLED_HOUR_STEP, DAY)
LABELS = {'type', 'action', 'build_id', 'sha256', 'dircache', 'dircache_source', 'tor_version'}
NUMERIC_VALUE = re.compile(r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
_TOR_VERSION_RE = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,3}){3}\Z')
LABEL_REPLACE_DEST = re.compile(r'\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,')
LABEL_REPLACE_SRC = re.compile(r'\s*"(|[A-Za-z_][A-Za-z0-9_]*)"\s*,')
MAX_JSON_DEPTH = 64
_INT10_RE = re.compile(r'[0-9]{1,10}')
_LABEL_REPLACE_CALL_RE = re.compile(r'(?<![A-Za-z0-9_])label_replace\s*\(')
_LABEL_JOIN_CALL_RE = re.compile(r'(?<![A-Za-z0-9_])label_join\s*\(')
_GROUPED_AGG_RE = re.compile(r'^(?:sum|count)\s+by\(([^)]+)\)\(')
_SIMPLE_AGG_RE = re.compile(r'^(?:sum|count)\(')
_CAT_KEY_RE = re.compile(r'[a-z0-9_]{1,128}')
_AGG_CODES = ('query_failed', 'query_warnings', 'invalid_series_count', 'invalid_series',
              'invalid_point_count', 'invalid_point', 'invalid_timestamp',
              'off_grid_timestamp', 'unordered_points', 'invalid_value')


class AccessError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_json_depth(text, max_depth=MAX_JSON_DEPTH):
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in ('[', '{'):
            depth += 1
            if depth > max_depth:
                return False
        elif ch in (']', '}'):
            depth -= 1
    return True


def strict_json(raw, *, error_messages=None):
    def err(code, detail=''):
        if error_messages is None:
            return AccessError('invalid_json' if code == 'invalid_encoding' else code)
        return ValueError(error_messages[code] + detail)

    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
        try:
            text = raw_bytes.decode(json.detect_encoding(raw_bytes), 'surrogatepass')
        except UnicodeDecodeError as exc:
            raise err('invalid_encoding') from exc
    elif error_messages is not None:
        raise TypeError('the JSON object must be str, bytes or bytearray')
    else:
        raise AccessError('invalid_json')

    if not _check_json_depth(text):
        raise err('json_nesting_limit')

    def reject_constant(value):
        raise err('nonfinite_json', value)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise err('duplicate_json_key', key)
            result[key] = value
        return result

    try:
        return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        if error_messages is not None:
            raise
        raise AccessError('invalid_json') from exc
    except ValueError as exc:
        if isinstance(exc, AccessError) or error_messages is not None:
            raise
        raise AccessError('invalid_json') from exc
    except RecursionError as exc:
        raise err('json_nesting_limit') from exc


def regular_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AccessError('not_regular_file')
        stream = os.fdopen(fd, 'rb')
        fd = None
        return stream
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def load_json(path, maximum=MAX_RESPONSE):
    with regular_read(path) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise AccessError('json_too_large')
    return strict_json(raw)


def parse(command, queries, now):
    if len(command) > 1024:
        raise AccessError('command_too_long')
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise AccessError('invalid_command_syntax') from exc
    if args == ['queries']:
        return ('queries',)
    if len(args) == 5 and args[0] == 'prom':
        _, key, begin, end, step = args
        if key not in queries or not all(_INT10_RE.fullmatch(x) for x in (begin, end, step)):
            raise AccessError('invalid_query_request')
        begin, end, step = map(int, (begin, end, step))
        if not 0 <= begin < end <= (int(now) // HOUR) * HOUR:
            raise AccessError('invalid_time_range')
        if step not in STEPS or begin % step or end % step:
            raise AccessError('unaligned_range')
        if end - begin > MAX_RANGE or (end - begin) // step + 1 > MAX_POINTS:
            raise AccessError('range_limit')
        return ('prom', key, begin, end, step)
    raise AccessError('command_not_allowed')


def _safe_label_replace_calls(expression, unquoted):
    for match in _LABEL_REPLACE_CALL_RE.finditer(expression):
        if not unquoted[match.start()]:
            continue
        depth, comma, dest, checked = 1, 0, None, False
        pos = match.end()
        while pos < len(expression) and depth > 0:
            if not unquoted[pos]:
                pos += 1
                continue
            ch = expression[pos]
            if ch in ('(', '[', '{'):
                depth += 1
            elif ch in (')', ']', '}'):
                depth -= 1
            elif ch == ',' and depth == 1:
                comma += 1
                if comma == 1:
                    dest_match = LABEL_REPLACE_DEST.match(expression, pos + 1)
                    if not dest_match:
                        return False
                    dest = dest_match.group(1)
                    if dest.startswith('__'):
                        return False
                elif comma == 3:
                    src_match = LABEL_REPLACE_SRC.match(expression, pos + 1)
                    if not src_match:
                        return False
                    src = src_match.group(1)
                    if dest in LABELS and src not in (LABELS | {''}):
                        return False
                    checked = True
                    break
            pos += 1
        if not checked:
            return False
    return True


def safe_aggregate(expression):
    """Recognize the reviewed outer aggregate grammar, including its full extent.

    This is not a general PromQL parser. Unsupported forms need explicit review.
    Runtime response validation separately prevents per-target label disclosure.
    """
    if not isinstance(expression, str) or len(expression) > 10000:
        return False
    # Reject single quotes, backticks, comments and control chars so the
    # lexical scanner matches PromQL string/comment boundaries.
    if any(ord(c) < 32 or ord(c) == 127 or c in ("'", "`", "#") for c in expression):
        return False
    grouped = _GROUPED_AGG_RE.match(expression)
    if grouped:
        parts = [x.strip() for x in grouped.group(1).split(',')]
        if any(not x for x in parts) or len(parts) != len(set(parts)) or not set(parts) <= LABELS:
            return False
        start = grouped.end() - 1
    else:
        prefix = _SIMPLE_AGG_RE.match(expression)
        if prefix:
            start = prefix.end() - 1
        elif expression.startswith('count_values("dircache",'):
            start = len('count_values')
        else:
            return False
    depth, quoted, escaped = 0, False, False
    unquoted = [False] * len(expression)
    for i, char in enumerate(expression):
        unquoted[i] = not quoted
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif i >= start:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return (i == len(expression) - 1
                            and not any(unquoted[m.start()] for m in _LABEL_JOIN_CALL_RE.finditer(expression))
                            and _safe_label_replace_calls(expression, unquoted))
                if depth < 0:
                    return False
    return False


def validate_catalogue(queries):
    if not isinstance(queries, dict) or not 1 <= len(queries) <= 256:
        raise AccessError('invalid_catalogue')
    for key, expression in queries.items():
        if not isinstance(key, str) or not _CAT_KEY_RE.fullmatch(key) or not safe_aggregate(expression):
            raise AccessError('invalid_catalogue_entry')
    return queries


def validate_matrix(payload, begin, end, step, *, max_series=512, max_points=MAX_POINTS,
                    check_labels=None, codes=_AGG_CODES, return_envelope=True):
    err_failed, err_warn, err_scount, err_series, err_pcount, err_pt, err_ts, err_grid, err_order, err_val = codes
    if type(step) is not int or step <= 0:
        raise AccessError('invalid_step')
    if (not isinstance(payload, dict) or not isinstance(payload.get('data'), dict)
            or payload.get('status') != 'success' or payload['data'].get('resultType') != 'matrix'):
        raise AccessError(err_failed)
    warnings = payload.get('warnings', [])
    if not isinstance(warnings, list) or warnings:
        raise AccessError(err_warn)
    rows = payload['data'].get('result')
    if not isinstance(rows, list) or len(rows) > max_series:
        raise AccessError(err_scount)
    clean, seen_labels = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise AccessError(err_series)
        labels, values = row.get('metric'), row.get('values')
        if check_labels is not None:
            check_labels(labels)
        else:
            if not isinstance(labels, dict) or not set(labels) <= LABELS:
                raise AccessError('unexpected_output_labels')
            if any(not isinstance(v, str) or len(v) > 256 for v in labels.values()):
                raise AccessError('invalid_label_value')
            if 'tor_version' in labels and not _TOR_VERSION_RE.fullmatch(labels['tor_version']):
                raise AccessError('invalid_label_value')
            canonical = tuple(sorted(labels.items()))
            if canonical in seen_labels:
                raise AccessError('duplicate_series')
            seen_labels.add(canonical)
        if not isinstance(values, list) or len(values) > max_points:
            raise AccessError(err_pcount)
        previous = None
        for pair in values:
            if not isinstance(pair, list) or len(pair) != 2:
                raise AccessError(err_pt)
            ts, value = pair
            if type(ts) not in (int, float):
                raise AccessError(err_ts)
            try:
                if not math.isfinite(ts):
                    raise AccessError(err_ts)
            except OverflowError as exc:
                raise AccessError(err_ts) from exc
            if not begin <= ts <= end:
                raise AccessError(err_ts)
            if (ts - begin) % step != 0:
                raise AccessError(err_grid)
            if previous is not None and ts <= previous:
                raise AccessError(err_order)
            previous = ts
            if not isinstance(value, str) or len(value) > 64 or not NUMERIC_VALUE.fullmatch(value) or not math.isfinite(float(value)):
                raise AccessError(err_val)
        if return_envelope:
            clean.append({'metric': labels, 'values': values})
    return {'status': 'success', 'data': {'resultType': 'matrix', 'result': clean}} if return_envelope else rows


def aggregate_response(payload, begin, end, step):
    return validate_matrix(payload, begin, end, step)


def fetch_prometheus(expression, begin, end, step, port=9090, opener=None, validate=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise AccessError('invalid_local_port')
    params = urllib.parse.urlencode({'query': expression, 'start': begin, 'end': end,
                                     'step': step, 'timeout': '25s'})
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f'http://127.0.0.1:{port}/api/v1/query_range?' + params, timeout=30) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise AccessError('response_limit')
    payload = strict_json(raw)
    return payload if validate is None else validate(payload, begin, end, step)


def query_prometheus(expression, begin, end, step, port=9090, opener=None):
    return fetch_prometheus(expression, begin, end, step, port=port, opener=opener, validate=aggregate_response)
