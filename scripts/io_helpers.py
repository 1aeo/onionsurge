"""Safe deterministic JSON and local build coordination."""
import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import query_protocol as access

BUILD_FORMAT = 'onionsurge-build-v1'
_JSON_ERRORS = {
    'invalid_encoding': 'Invalid JSON byte encoding',
    'json_nesting_limit': 'JSON nesting exceeds limit',
    'nonfinite_json': 'Non-finite JSON number: ',
    'duplicate_json_key': 'Duplicate JSON object key: ',
}


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    return sha256_hex(Path(path).read_bytes())


def read_regular_bytes(path, max_bytes=None, *, private=False):
    """Read bounded regular-file bytes without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Path must be a regular file')
        if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError('Private file must be owned by the current user with mode 0600')
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValueError('File exceeds size limit')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            data = stream.read(max_bytes + 1) if max_bytes is not None else stream.read()
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError('File exceeds size limit')
    return data


def supported_writes():
    if os.name != 'posix' or not (sys.platform.startswith('linux') or sys.platform == 'darwin'):
        raise ValueError('Filesystem-writing tools require Linux or macOS')


def owned_generation(directory, *, require_readable=False):
    marker = directory / 'build-manifest.json'
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode) or not stat.S_ISREG(marker.lstat().st_mode):
            return False
        value = read_json(marker)
        return isinstance(value, dict) and value.get('format') == BUILD_FORMAT
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return False
    except OSError:
        if require_readable:
            raise
        return False


def cleanup_warning(message):
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def json_text(value, pretty=False):
    return json.dumps(value, allow_nan=False, sort_keys=True,
                      indent=2 if pretty else None,
                      separators=None if pretty else (',', ':')) + '\n'


def javascript_data(value, *, compact_json=None):
    serialized = json_text(value) if compact_json is None else compact_json
    encoded = serialized.rstrip('\n').replace('<', '\\u003c')
    return 'window.ONIONSURGE_DATA = ' + encoded + ';\n'


def parse_json(encoded):
    return access.strict_json(encoded, error_messages=_JSON_ERRORS)


def read_json(path):
    return parse_json(Path(path).read_bytes())


def atomic_text(path, text, *, mode=None, preserve_mode=True, dir_mode=0o777):
    supported_writes()
    if not isinstance(text, str):
        raise TypeError('atomic_text requires str')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    try:
        info = path.lstat()
    except FileNotFoundError:
        existing_mode = None
    else:
        existing_mode = stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else None
    temporary = None
    pending_error = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, prefix='.' + path.name + '.',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            target_mode = existing_mode if (preserve_mode and existing_mode is not None) else mode
            if target_mode is not None:
                os.chmod(temporary, target_mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except BaseException as error:
        pending_error = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                if pending_error is None:
                    raise
                cleanup_warning(f'Atomic-write cleanup failed for {temporary}: {error}')


def normalized_output_path(directory):
    """Canonicalize the parent while preserving the final symlink safeguard."""
    supported_writes()
    directory = Path(directory).absolute()
    if any(is_claim_name(part) for part in directory.parts):
        raise ValueError('Output path uses a reserved .name.build.lock component')
    if directory.name == '..':
        raise ValueError('Output directory cannot end with ..')
    resolved = directory.parent.resolve() / directory.name
    if any(is_claim_name(part) for part in resolved.parts):
        raise ValueError('Output path uses a reserved .name.build.lock component')
    return resolved


def filesystem_contains(parent, child):
    """Include existing filesystem aliases without folding case-sensitive names."""
    if parent == child or parent in child.parents:
        return True
    try:
        parent_info = parent.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    identity = parent_info.st_dev, parent_info.st_ino
    for path in [child, *child.parents]:
        try:
            info = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if (info.st_dev, info.st_ino) == identity:
            return True
    return False


def reject_working_directory_overlap(directory):
    try:
        current = Path.cwd()
    except FileNotFoundError:
        return  # Absolute-path callers can still work from an already removed cwd.
    if filesystem_contains(directory, current.resolve()):
        raise ValueError("Run generation tools from the output tree's parent or another outside directory")


def reject_coordinator_overlap(directory, coordinator):
    if filesystem_contains(directory, coordinator) or filesystem_contains(coordinator, directory):
        raise ValueError('Output overlaps the per-user registration lock')


def is_claim_name(name):
    return name.startswith('.') and name.casefold().endswith('.build.lock')


@contextlib.contextmanager
def output_lock(directory, *, before_register=None):
    """Coordinate cooperating writers; validate new roots before reserving them."""
    directory = normalized_output_path(directory)
    if directory.is_symlink():
        raise ValueError('Output directory cannot be a symlink')
    coordinator = registration_lock_path()
    lock = output_claim(directory)
    # Register a root atomically with the overlap check. Never wait for a busy
    # output while holding this short per-user coordination lock.
    with contextlib.ExitStack() as registration:
        try:
            reject_coordinator_overlap(directory, coordinator)
            coordinator.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            registration.enter_context(file_lock(coordinator))
            reject_coordinator_overlap(directory, coordinator)
        except OSError as error:
            raise ValueError(f'Provision a writable coordinator directory for the build owner: '
                             f'{coordinator.parent} (lock: {coordinator})') from error
        if not lock.exists():
            reject_overlapping_outputs(directory)
            missing, created = [], []
            parent = lock.parent
            while True:
                try:
                    parent.lstat()
                    break
                except FileNotFoundError:
                    missing.append(parent)
                    parent = parent.parent
            try:
                for parent in reversed(missing):
                    try:
                        parent.mkdir()
                    except FileExistsError:
                        if not stat.S_ISDIR(parent.lstat().st_mode):
                            raise
                    else:
                        created.append(parent)
                if before_register is not None:
                    before_register(directory)
                with lock.open('xb'):
                    pass
            except BaseException:
                for parent in reversed(created):
                    try:
                        parent.rmdir()
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        cleanup_warning(f'Registration cleanup failed for {parent}: {error}')
                raise
    with file_lock(lock):
        reject_overlapping_outputs(directory)
        yield


def registration_lock_path():
    supported_writes()
    import pwd
    # Use the account's home, not per-job HOME/TMPDIR overrides.
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as error:
        raise ValueError('Build owner account must exist in the system account database') from error
    if not home.is_absolute():
        raise ValueError('Build owner account must have an absolute home directory')
    return home / '.cache/onionsurge/generation-registration.lock'


def output_claim(directory):
    return directory.parent / ('.' + directory.name + '.build.lock')


def reject_overlapping_outputs(directory):
    """Persistent claims reserve disjoint roots; siblings remain independent."""
    if directory.is_symlink():
        raise ValueError('Output directory cannot be a symlink')
    for parent in directory.parents:
        claim = output_claim(parent)
        if claim.exists() or claim.is_symlink() or owned_generation(parent, require_readable=True):
            raise ValueError(f'Output overlaps a reserved ancestor: {parent}')
    if directory.is_dir():
        def raise_scan_error(error):
            raise error
        for root, _, files in os.walk(directory, followlinks=False, onerror=raise_scan_error):
            root = Path(root)
            if root != directory and owned_generation(root, require_readable=True):
                raise ValueError(f'Output contains a generation: {root}')
            if any(is_claim_name(name) for name in files):
                raise ValueError(f'Output contains a reserved descendant: {root}')


@contextlib.contextmanager
def staged_generation(output):
    """Prepare a full generation, then publish with recoverable replacement.

    All writers honor output_lock; the caller holds it and has checked the
    destination. Preparation failures leave the previous generation intact;
    failed restores retain it.
    """
    supported_writes()
    output = Path(output)
    stage = Path(tempfile.mkdtemp(prefix='.' + output.name + '.staging-', dir=output.parent))
    backup = None
    promoted = False
    pending_error = None
    try:
        yield stage
        reject_overlapping_outputs(output)
        if output.exists():
            candidate = Path(tempfile.mkdtemp(prefix='.' + output.name + '.previous-', dir=output.parent))
            candidate.rmdir()
            os.replace(output, candidate)
            backup = candidate
        try:
            os.replace(stage, output)
            promoted = True
        except BaseException as promotion_error:
            if backup is not None:
                try:
                    os.replace(backup, output)
                except BaseException as restore_error:
                    raise OSError(f'Generation promotion failed ({promotion_error}); restore failed. '
                                  f'Previous output preserved at {backup}') from restore_error
                backup = None
            raise
    except BaseException as error:
        pending_error = error
        raise
    finally:
        cleanup = [stage]
        if promoted and backup is not None:
            if owned_generation(backup):
                cleanup.append(backup)
            else:
                cleanup_warning(f'Unverified previous generation preserved at {backup}')
        for path in cleanup:
            try:
                if path.exists():
                    shutil.rmtree(path)
            except OSError as cleanup_error:
                if pending_error is None and not promoted:
                    raise
                cleanup_warning(f'Generation cleanup failed for {path}: {cleanup_error}')


@contextlib.contextmanager
def file_lock(path, *, private_dir=False):
    """Serialize builders without a third-party locking dependency."""
    supported_writes()
    import fcntl
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700 if private_dir else 0o777)
    if private_dir:
        dir_info = directory.lstat()
        if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.geteuid() or dir_info.st_mode & 0o077:
            raise ValueError('Lock directory must be private owned')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('Lock files must be regular and owned by the build user')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
