#!/usr/bin/env python3
"""Install a pinned query gateway under an existing SSH account's home only."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types

SOURCES = {'query_protocol.py': 'scripts/query_protocol.py', 'query_gateway.py': 'scripts/query_gateway.py',
           'queries.json': 'catalogue/queries.json'}
CODE = Path('.local/lib/onionsurge-source/releases')
CONFIG = Path('.config/onionsurge-source/generations')
STATE = Path('.local/state/onionsurge-source')
MAX_BYTES = 8 * 1024**2
_COMMIT_RE = re.compile(r'[a-f0-9]{40}')
_MAX_JSON_DEPTH = 64


class InstallError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise InstallError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _check_json_depth(text, max_depth=_MAX_JSON_DEPTH):
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


def parse_json(raw):
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        raw_bytes = bytes(raw)
        try:
            text = raw_bytes.decode(json.detect_encoding(raw_bytes), 'surrogatepass')
        except UnicodeDecodeError as error:
            raise InstallError('invalid_json') from error
    else:
        raise InstallError('invalid_json')

    if not _check_json_depth(text):
        raise InstallError('json_nesting_limit')

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(InstallError('nonfinite_json')))
    except InstallError:
        raise
    except RecursionError as error:
        raise InstallError('json_nesting_limit') from error
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as error:
        raise InstallError('invalid_json') from error


def public_key(text):
    require(isinstance(text, str), 'ed25519_public_key_required')
    lines = text.strip().splitlines()
    require(len(lines) == 1, 'one_public_key_required')
    fields = lines[0].split()
    require(len(fields) >= 2 and fields[0] == 'ssh-ed25519', 'ed25519_public_key_required')
    try:
        wire = base64.b64decode(fields[1], validate=True)
    except ValueError as error:
        raise InstallError('invalid_public_key') from error
    require(wire[:15] == b'\0\0\0\x0bssh-ed25519' and wire[15:19] == b'\0\0\0\x20'
            and len(wire) == 51, 'invalid_public_key')
    return fields[1]


def address(value):
    try:
        require(isinstance(value, str) and '%' not in value, 'literal_source_address_required')
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise InstallError('literal_source_address_required') from error
    return str(getattr(parsed, 'ipv4_mapped', None) or parsed)


def _check_owned(info, kind_fn, private, exact_mode=None, kind_err='owned_regular_file_required', perm_err='unsafe_file_permissions'):
    require(kind_fn(info.st_mode) and info.st_uid == os.geteuid(), kind_err)
    require(not info.st_mode & (0o077 if private else 0o022), perm_err)
    if exact_mode is not None:
        require(stat.S_IMODE(info.st_mode) == exact_mode, 'unexpected_file_mode')


def _open_checked_stream(path, flags, stream_mode, *, create_mode=0o600, private=False, exact_mode=None,
                         kind_err='owned_regular_file_required', perm_err='unsafe_file_permissions'):
    fd = os.open(path, flags, create_mode)
    try:
        _check_owned(os.fstat(fd), stat.S_ISREG, private, exact_mode, kind_err=kind_err, perm_err=perm_err)
        stream = os.fdopen(fd, stream_mode)
        fd = None
        return stream
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def read_regular(path, private=False, exact_mode=None):
    with _open_checked_stream(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 'rb',
                              private=private, exact_mode=exact_mode) as stream:
        value = stream.read(MAX_BYTES + 1)
    require(len(value) <= MAX_BYTES, 'file_too_large')
    return value


def directory(path, private=False, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    _check_owned(info, stat.S_ISDIR, private, kind_err='unsafe_owned_directory', perm_err='unsafe_owned_directory')
    return info


def tree(home, relative):
    path = home
    for part in relative.parts:
        require(part not in ('', '.', '..'), 'invalid_managed_path')
        path /= part
        directory(path, private=path == home / relative, create=True)
    return path


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def replace_private(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.onionsurge-', delete=False) as stream:
            temporary = Path(stream.name); stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path); temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def immutable_generation(parent, name, files):
    destination = parent / name
    manifest = json.dumps({'files': {key: sha(value) for key, value in sorted(files.items())}},
                          sort_keys=True, separators=(',', ':')).encode() + b'\n'
    expected = dict(files, **{'install-manifest.json': manifest})
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        directory(destination, private=True)
        require({p.name for p in destination.iterdir()} == set(expected), 'existing_generation_differs')
        require(all(read_regular(destination / key, private=True) == value for key, value in expected.items()),
                'existing_generation_differs')
        return destination, False
    stage = Path(tempfile.mkdtemp(prefix='.install-', dir=parent))
    try:
        for key, value in expected.items():
            write_new(stage / key, value)
        # Installers share the account's installation lock. Other writers must
        # not edit these private trees or authorized_keys during installation.
        os.rename(stage, destination)
        return destination, True
    finally:
        if stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError:
                pass


def key_fields(line):
    """Read only key-prefix fields; arbitrary comment bytes stay untouched."""
    fields = []; index = 0
    while index < len(line) and len(fields) < 3:
        while index < len(line) and line[index] in b' \t\r\n':
            index += 1
        if index == len(line) or (not fields and line[index] == ord('#')):
            break
        start = index; quoted = escaped = False
        while index < len(line):
            char = line[index]
            if escaped:
                escaped = False
            elif char == ord('\\'):
                escaped = True
            elif char == ord('"'):
                quoted = not quoted
            elif char in b' \t\r\n' and not quoted:
                break
            index += 1
        if quoted:
            break
        fields.append(line[start:index])
    return fields


def replacement_keys(original, blob, line):
    result = []; inserted = False
    for existing in original.splitlines(keepends=True):
        fields = key_fields(existing)
        offset = 0 if fields and fields[0] == b'ssh-ed25519' else 1
        matches = len(fields) > offset + 1 and fields[offset] == b'ssh-ed25519' and fields[offset + 1] == blob.encode()
        if matches:
            if not inserted:
                result.append(line); inserted = True
        else:
            result.append(existing)
    updated = b''.join(result)
    if not inserted:
        updated += (b'\n' if updated and not updated.endswith(b'\n') else b'') + line
    return updated


def forced_command(script, config, state):
    arguments = ['/usr/bin/python3', '-I', '-B', str(script), '--config-dir', str(config), '--state-dir', str(state)]
    return 'exec env -i PATH=/usr/bin:/bin SSH_ORIGINAL_COMMAND="$SSH_ORIGINAL_COMMAND" ' + ' '.join(shlex.quote(arg) for arg in arguments)


def validate_code(files, evidence):
    previous = sys.modules.get('query_protocol'); old_path = list(sys.path)
    try:
        helper = types.ModuleType('query_protocol'); helper.__file__ = '/pinned/query_protocol.py'
        sys.modules['query_protocol'] = helper; exec(files['query_protocol.py'], helper.__dict__)
        gateway = types.ModuleType('_pinned_query_gateway'); gateway.__file__ = '/pinned/query_gateway.py'
        exec(files['query_gateway.py'], gateway.__dict__)
        queries = helper.validate_catalogue(parse_json(files['queries.json']))
        require(all(queries.get(key) == value for key, value in gateway.VIRTUAL_QUERIES.items()), 'pinned_catalogue_mismatch')
        gateway.validate_attestations(parse_json(evidence), time.time())
        return queries
    finally:
        sys.path[:] = old_path
        if previous is None:
            sys.modules.pop('query_protocol', None)
        else:
            sys.modules['query_protocol'] = previous


def verify_gateway(script, config, state, expected):
    result = subprocess.run(['/usr/bin/python3', '-I', '-B', str(script), '--config-dir', str(config), '--state-dir', str(state)],
                            env={'PATH': '/usr/bin:/bin', 'SSH_ORIGINAL_COMMAND': 'queries'},
                            capture_output=True, timeout=45)
    require(result.returncode == 0 and json.loads(result.stdout) == expected, 'installed_gateway_check_failed')


def install(payload, home):
    require(sys.version_info >= (3, 11), 'python_3_11_required')
    require(isinstance(payload, dict) and set(payload) == {'commit', 'files', 'file_sha256', 'public_key', 'allowed_address', 'attestations', 'prometheus_port'}, 'invalid_install_payload')
    commit = payload['commit']
    require(isinstance(commit, str) and _COMMIT_RE.fullmatch(commit), 'invalid_commit_pin')
    require(isinstance(payload['files'], dict) and isinstance(payload['file_sha256'], dict)
            and set(payload['files']) == set(SOURCES) and set(payload['file_sha256']) == set(SOURCES)
            and all(isinstance(v, str) for v in payload['files'].values())
            and all(isinstance(v, str) for v in payload['file_sha256'].values()),
            'invalid_pinned_files')
    files = {key: value.encode('utf-8') for key, value in payload['files'].items()}
    require(all(sha(value) == payload['file_sha256'][key] for key, value in files.items()), 'pinned_file_hash_mismatch')
    blob, allowed = public_key(payload['public_key']), address(payload['allowed_address'])
    require(type(payload['prometheus_port']) is int and 1 <= payload['prometheus_port'] <= 65535, 'invalid_local_port')
    require(isinstance(payload['attestations'], str), 'invalid_attestations')
    evidence = payload['attestations'].encode('utf-8')
    queries = validate_code(files, evidence)
    require(isinstance(home, (str, os.PathLike)), 'invalid_account_home')
    home_raw = os.fspath(home)
    require('\\' not in home_raw and not any(ord(c) < 32 or ord(c) == 127 for c in home_raw), 'invalid_account_home')
    home = Path(home_raw); require(home.is_absolute(), 'absolute_account_home_required'); directory(home)
    require(stat.S_IMODE(directory(home / '.ssh', private=True).st_mode) == 0o700, 'ssh_directory_mode_required')
    authorized = home / '.ssh/authorized_keys'
    read_regular(authorized, private=True, exact_mode=0o600)
    state = tree(home, STATE)
    with _open_checked_stream(state / 'install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 'a+b',
                              create_mode=0o600, private=True,
                              kind_err='invalid_install_lock', perm_err='invalid_install_lock') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Re-read after waiting so a completed cooperating installation is the
        # baseline, rather than overwriting it with the pre-lock bytes.
        original = read_regular(authorized, private=True, exact_mode=0o600)
        code, code_created = immutable_generation(tree(home, CODE), commit, files)
        identity = json.dumps({'home': str(home), 'commit': commit, 'port': payload['prometheus_port'],
                               'queries': sha(files['queries.json']), 'attestations': sha(evidence)}, sort_keys=True).encode()
        generation = sha(identity)
        config_path = home / CONFIG / generation
        config_files = {'queries.json': files['queries.json'], 'attestations.json': evidence,
                        'gateway.json': (json.dumps({'prometheus_port': payload['prometheus_port'],
                                                    'attestations_file': str(config_path / 'attestations.json')}, sort_keys=True) + '\n').encode()}
        config, config_created = immutable_generation(tree(home, CONFIG), generation, config_files)
        verify_gateway(code / 'query_gateway.py', config, state, queries)
        command = forced_command(code / 'query_gateway.py', config, state)
        escaped = command.replace('\\', '\\\\').replace('"', '\\"')
        line = f'restrict,from="{allowed}",command="{escaped}" ssh-ed25519 {blob} onionsurge-read\n'.encode()
        updated = replacement_keys(original, blob, line)
        changed = updated != original
        if changed:
            backups = tree(home, STATE / 'authorized-keys-backups')
            backup = backups / (sha(original) + '.bak')
            if backup.exists():
                require(read_regular(backup, private=True) == original, 'authorization_backup_differs')
            else:
                write_new(backup, original)
            require(read_regular(authorized, private=True, exact_mode=0o600) == original, 'authorized_keys_changed')
            replace_private(authorized, updated)
        return {'format': 'onionsurge-source-install-v1', 'commit': commit, 'config_id': generation,
                'key_changed': changed, 'code_created': code_created, 'config_created': config_created,
                'forced_command_sha256': sha(command.encode()),
                'relative_script': str(CODE / commit / 'query_gateway.py'),
                'relative_config': str(CONFIG / generation), 'relative_state': str(STATE)}


def pinned(repository, commit, name):
    return subprocess.run(['git', '-C', str(repository), 'show', f'{commit}:{name}'],
                          check=True, capture_output=True).stdout


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--apply-stdin']:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1); require(len(raw) <= MAX_BYTES, 'install_payload_too_large')
        result = install(parse_json(raw), Path(pwd.getpwuid(os.geteuid()).pw_dir))
        print(json.dumps(result, sort_keys=True)); return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admin-target', required=True)
    parser.add_argument('--publisher-public-key', type=Path, required=True)
    parser.add_argument('--allowed-address', required=True)
    parser.add_argument('--attestations', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--prometheus-port', type=int, default=9090)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    require(_COMMIT_RE.fullmatch(args.commit), 'full_commit_pin_required')
    require(type(args.prometheus_port) is int and 1 <= args.prometheus_port <= 65535, 'invalid_local_port')
    if args.receipt:
        args.receipt = args.receipt.absolute()
        directory(args.receipt.parent, private=True)
        require(os.access(args.receipt.parent, os.W_OK), 'receipt_directory_not_writable')
        try:
            prior = parse_json(read_regular(args.receipt, private=True))
        except FileNotFoundError:
            pass
        else:
            require(isinstance(prior, dict) and prior.get('format') == 'onionsurge-source-install-v1', 'unrelated_receipt_file')
    repository = Path(__file__).resolve().parents[1]
    kind = subprocess.run(['git', '-C', str(repository), 'cat-file', '-t', args.commit],
                          check=True, capture_output=True).stdout.strip()
    require(kind == b'commit', 'commit_object_required')
    blobs = {key: pinned(repository, args.commit, path) for key, path in SOURCES.items()}
    installer = pinned(repository, args.commit, 'scripts/install_source.py').decode('utf-8')
    key = read_regular(args.publisher_public_key).decode('utf-8'); public_key(key)
    evidence_bytes = read_regular(args.attestations, private=True); validate_code(blobs, evidence_bytes)
    evidence = evidence_bytes.decode('utf-8')
    payload = {'commit': args.commit, 'files': {key: value.decode('utf-8') for key, value in blobs.items()},
               'file_sha256': {key: sha(value) for key, value in blobs.items()}, 'public_key': key,
               'allowed_address': address(args.allowed_address), 'attestations': evidence, 'prometheus_port': args.prometheus_port}
    command = '/usr/bin/python3 -I -B -c ' + shlex.quote(installer) + ' --apply-stdin'
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                             '-o', 'UpdateHostKeys=no', '-o', 'ForwardAgent=no', '--', args.admin_target, command],
                            input=json.dumps(payload).encode(), capture_output=True, timeout=90)
    require(result.returncode == 0 and len(result.stdout) <= 65536, 'source_install_failed')
    receipt = parse_json(result.stdout)
    require(isinstance(receipt, dict) and receipt.get('commit') == args.commit
            and type(receipt.get('key_changed')) is bool, 'source_receipt_mismatch')
    if args.receipt:
        replace_private(args.receipt, (json.dumps(receipt, indent=2) + '\n').encode())
    print(json.dumps({'status': 'installed', 'commit': args.commit, 'key_changed': receipt['key_changed']}))


if __name__ == '__main__':
    try:
        main()
    except InstallError as error:
        print('Source installation failed: ' + str(error), file=sys.stderr); raise SystemExit(1)
    except Exception as error:
        print('Source installation failed: ' + type(error).__name__, file=sys.stderr); raise SystemExit(1)
