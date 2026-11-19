import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_source', ROOT / 'scripts/install_source.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


def public_key(byte=1):
    wire = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + bytes([byte]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(wire).decode()


def payload():
    files = {key: (ROOT / path).read_text() for key, path in installer.SOURCES.items()}
    evidence = {'selectors': {key: 'synthetic_' + key for key in ['pid', 'start', 'health', 'source_time', 'info']},
                'groups': {}, 'records': []}
    return {'commit': 'a' * 40, 'files': files, 'file_sha256': {key: installer.sha(value.encode()) for key, value in files.items()},
            'public_key': public_key(), 'allowed_address': '127.0.0.1', 'attestations': json.dumps(evidence), 'prometheus_port': 9090}


def home_fixture(folder, contents=None):
    home = Path(folder).resolve() / 'account home'; home.mkdir(mode=0o700)
    (home / '.ssh').mkdir(mode=0o700)
    keyfile = home / '.ssh/authorized_keys'; keyfile.write_bytes(contents if contents is not None else (public_key(2) + ' existing\n').encode())
    keyfile.chmod(0o600)
    return home, keyfile


class SourceInstallTests(unittest.TestCase):
    def test_install_is_pinned_private_idempotent_and_preserves_other_lines(self):
        other = (b'# private existing comment\r\n' + (public_key(2) + ' unrelated').encode() + b'\n\n')
        original = other + (public_key() + ' old unrestricted comment "\n').encode()
        original += (f'from="127.0.0.2",command="echo old option" {public_key()} duplicate\n').encode()
        with tempfile.TemporaryDirectory() as folder:
            home, keys = home_fixture(folder, original)
            expected = json.loads(payload()['files']['queries.json'])
            def postflight(script, config, state, queries):
                self.assertEqual(queries, expected)
                self.assertEqual(keys.read_bytes(), original)
                self.assertEqual((config / 'queries.json').read_bytes(), payload()['files']['queries.json'].encode())
            with mock.patch.object(installer, 'verify_gateway', side_effect=postflight):
                result = installer.install(payload(), home)
            updated = keys.read_bytes()
            self.assertTrue(updated.startswith(other))
            self.assertEqual(updated.count(public_key().encode()), 1)
            self.assertIn(b'restrict,from="127.0.0.1",command=', updated)
            self.assertIn(b'exec env -i PATH=/usr/bin:/bin SSH_ORIGINAL_COMMAND=', updated)
            self.assertIn(b'/usr/bin/python3 -I -B ', updated)
            self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o600)
            for field in ['relative_config', 'relative_state']:
                self.assertEqual(stat.S_IMODE((home / result[field]).stat().st_mode), 0o700)
            self.assertEqual((home / installer.STATE / 'authorized-keys-backups' / (installer.sha(original) + '.bak')).read_bytes(), original)
            before = keys.stat().st_ino
            with mock.patch.object(installer, 'verify_gateway'):
                second = installer.install(payload(), home)
            self.assertFalse(second['key_changed']); self.assertFalse(second['code_created']); self.assertFalse(second['config_created'])
            self.assertEqual(keys.stat().st_ino, before)
            self.assertNotIn(str(home), json.dumps(result))

    def test_only_dedicated_key_is_replaced_even_with_nonutf8_or_quoted_comments(self):
        own = public_key().encode(); other = public_key(2).encode()
        raw = other + b' unclosed " comment \xff\n' + own + b' comment \xff "\n' + b'# ' + own + b' not a key\n'
        replacement = b'restrict ' + own + b' onionsurge-read\n'
        updated = installer.replacement_keys(raw, public_key().split()[1], replacement)
        self.assertEqual(updated, other + b' unclosed " comment \xff\n' + replacement + b'# ' + own + b' not a key\n')

    def test_foreign_changed_or_symlinked_paths_never_get_replaced(self):
        for kind in ['ssh-symlink', 'key-symlink', 'key-mode', 'code-changed', 'concurrent-key-edit']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                home, keys = home_fixture(folder); original = keys.read_bytes()
                if kind == 'ssh-symlink':
                    (home / '.ssh').rename(home / 'other'); (home / '.ssh').symlink_to(home / 'other', target_is_directory=True)
                elif kind == 'key-symlink':
                    keys.rename(home / 'other'); keys.symlink_to(home / 'other')
                elif kind == 'key-mode':
                    keys.chmod(0o644)
                elif kind == 'code-changed':
                    with mock.patch.object(installer, 'verify_gateway'):
                        result = installer.install(payload(), home)
                    original = keys.read_bytes(); (home / result['relative_script']).write_text('unreviewed')
                def postflight(*args):
                    if kind == 'concurrent-key-edit':
                        keys.write_bytes(original + b'# concurrent preserved\n')
                with mock.patch.object(installer, 'verify_gateway', side_effect=postflight), self.assertRaises((ValueError, OSError)):
                    installer.install(payload(), home)
                self.assertEqual(keys.read_bytes(), original + (b'# concurrent preserved\n' if kind == 'concurrent-key-edit' else b''))

    def test_gateway_or_authorization_write_failure_preserves_current_access(self):
        for phase in ['gateway', 'replace']:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                home, keys = home_fixture(folder); before = keys.read_bytes()
                with mock.patch.object(installer, 'verify_gateway', side_effect=ValueError('synthetic check failure') if phase == 'gateway' else None), \
                        mock.patch.object(installer.os, 'replace', side_effect=OSError('synthetic write failure')), \
                        self.assertRaises((ValueError, OSError)):
                    installer.install(payload(), home)
                self.assertEqual(keys.read_bytes(), before)

    def test_env_boundary_preserves_original_command_as_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); script = root / "reader ' name.py"; result = root / 'received.json'
            script.write_text('import json,os,sys\nopen(sys.argv[2],"w").write(json.dumps(dict(os.environ)))\n')
            command = installer.forced_command(script, result, root / 'state')
            original = 'prom query 1 2 86400; $(touch ' + str(root / 'not-created') + ') "quoted"'
            process = subprocess.run(['/bin/sh', '-c', command], env={'PATH': '/usr/bin:/bin', 'SSH_ORIGINAL_COMMAND': original, 'PRIVATE_TEST_SECRET': 'not-forwarded'}, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            received = json.loads(result.read_text())
            self.assertEqual(received['SSH_ORIGINAL_COMMAND'], original)
            self.assertEqual(received['PATH'], '/usr/bin:/bin')
            self.assertNotIn('PRIVATE_TEST_SECRET', received)
            self.assertFalse((root / 'not-created').exists())

    def test_invalid_key_address_pin_and_evidence_fail_before_managed_writes(self):
        cases = [('public_key', 'not a public key'), ('allowed_address', '*'), ('allowed_address', '127.0.0.0/8'),
                 ('commit', 'main'), ('prometheus_port', True), ('attestations', '{"x":NaN}')]
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as folder:
                home, keys = home_fixture(folder); data = payload(); data[key] = value
                with self.assertRaises((ValueError, TypeError)):
                    installer.install(data, home)
                self.assertFalse((home / '.local').exists())
                self.assertEqual(keys.read_bytes(), (public_key(2) + ' existing\n').encode())
        for bad_home in ('/tmp/bad\\home', '/tmp/bad\nhome', '/tmp/bad\x7fhome'):
            with self.subTest(bad_home=bad_home):
                with self.assertRaisesRegex(ValueError, 'invalid_account_home'):
                    installer.install(payload(), bad_home)

    def test_postflight_uses_only_the_two_fixed_environment_variables(self):
        with mock.patch.object(installer.subprocess, 'run', return_value=type('Result', (), {'returncode': 0, 'stdout': b'{}'})()) as run:
            installer.verify_gateway(Path('/synthetic/reader.py'), Path('/synthetic/config'), Path('/synthetic/state'), {})
        args = run.call_args
        self.assertEqual(args.kwargs['env'], {'PATH': '/usr/bin:/bin', 'SSH_ORIGINAL_COMMAND': 'queries'})
        self.assertEqual(args.args[0][:3], ['/usr/bin/python3', '-I', '-B'])

    def test_isolated_stdin_cli_works_without_a_script_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            home, keys = home_fixture(folder)
            bootstrap = ('import pwd,types\n'
                         'pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir=' + repr(str(home)) + ')\n'
                         'namespace={"__name__":"isolated_install_test"}\n'
                         'exec(' + repr((ROOT / 'scripts/install_source.py').read_text()) + ',namespace)\n'
                         'namespace["verify_gateway"]=lambda *args:None\n'
                         'namespace["main"](["--apply-stdin"])\n')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap], input=json.dumps(payload()).encode(), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt['commit'], 'a' * 40)
            self.assertTrue(receipt['key_changed'])
            self.assertNotIn(str(home).encode(), result.stdout)
            self.assertNotIn(b'127.0.0.1', result.stdout)
            self.assertIn(b'restrict,from=', keys.read_bytes())

    def test_admin_cli_reads_only_pinned_blobs_and_uses_strict_mock_ssh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); home, _ = home_fixture(folder)
            keyfile = root / 'publisher.pub'; keyfile.write_text(public_key() + ' stripped-comment\n')
            evidence = root / 'evidence.json'; evidence.write_text(payload()['attestations']); evidence.chmod(0o600)
            receipt = root / 'receipt.json'; calls = []
            def run(arguments, **kwargs):
                calls.append(arguments)
                if arguments[0] == 'git':
                    if arguments[3] == 'cat-file':
                        output = b'commit\n'
                    else:
                        revision, path = arguments[-1].split(':', 1)
                        self.assertEqual(revision, 'a' * 40)
                        output = (ROOT / path).read_bytes()
                    return type('Result', (), {'returncode': 0, 'stdout': output})()
                self.assertEqual(arguments[0], 'ssh')
                self.assertIn('StrictHostKeyChecking=yes', arguments)
                self.assertIn('ForwardAgent=no', arguments)
                self.assertTrue(arguments[-1].startswith('/usr/bin/python3 -I -B -c '))
                self.assertNotIn('127.0.0.1', arguments[-1])
                transferred = json.loads(kwargs['input'])
                with mock.patch.object(installer, 'verify_gateway'):
                    result = installer.install(transferred, home)
                return type('Result', (), {'returncode': 0, 'stdout': json.dumps(result).encode()})()
            argv = ['--admin-target', 'SYNTHETIC_ADMIN', '--publisher-public-key', str(keyfile),
                    '--allowed-address', '127.0.0.1', '--attestations', str(evidence),
                    '--commit', 'a' * 40, '--receipt', str(receipt)]
            output = io.StringIO()
            with mock.patch.object(installer.subprocess, 'run', side_effect=run), redirect_stdout(output):
                installer.main(argv); installer.main(argv)
            self.assertEqual(sum(call[0] == 'ssh' for call in calls), 2)
            self.assertEqual([json.loads(line)['key_changed'] for line in output.getvalue().splitlines()], [True, False])
            self.assertNotIn('127.0.0.1', output.getvalue())
            self.assertNotIn(str(home), output.getvalue())
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)

    def test_hardening_port_payload_types_and_strict_ssh_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            home, _ = home_fixture(folder)
            for field, value in (
                ('file_sha256', list(installer.SOURCES)),
                ('files', dict(payload()['files'], **{'queries.json': 123})),
                ('public_key', 123),
                ('attestations', 123),
            ):
                bad = payload()
                bad[field] = value
                with self.subTest(field=field), self.assertRaises(installer.InstallError):
                    installer.install(bad, home)

            root = Path(folder).resolve()
            keyfile = root / 'publisher.pub'; keyfile.write_text(public_key() + '\n')
            evidence = root / 'evidence.json'; evidence.write_text(payload()['attestations']); evidence.chmod(0o600)
            base_argv = ['--admin-target', 'SYNTHETIC_ADMIN', '--publisher-public-key', str(keyfile),
                         '--allowed-address', '127.0.0.1', '--attestations', str(evidence),
                         '--commit', 'a' * 40]
            with mock.patch.object(installer.subprocess, 'run') as run:
                for bad_port in ('0', '65536'):
                    with self.assertRaisesRegex(installer.InstallError, 'invalid_local_port'):
                        installer.main([*base_argv, '--prometheus-port', bad_port])
                run.assert_not_called()

            for bad_receipt in (
                b'{"commit":"' + b'a' * 40 + b'","commit":"' + b'a' * 40 + b'","key_changed":true}',
                b'{"commit":"' + b'a' * 40 + b'","key_changed":NaN}',
                b'[1, 2, 3]',
                b'{bad json',
            ):
                def run_receipt(arguments, _receipt=bad_receipt, **kwargs):
                    if arguments[0] == 'git':
                        if arguments[3] == 'cat-file':
                            return type('Result', (), {'returncode': 0, 'stdout': b'commit\n'})()
                        return type('Result', (), {'returncode': 0, 'stdout': (ROOT / arguments[-1].split(':', 1)[1]).read_bytes()})()
                    return type('Result', (), {'returncode': 0, 'stdout': _receipt})()
                with self.subTest(bad_receipt=bad_receipt), \
                        mock.patch.object(installer.subprocess, 'run', side_effect=run_receipt), \
                        self.assertRaises(installer.InstallError):
                    installer.main(base_argv)

    def test_parse_json_rejects_oversized_int_deep_nesting_and_bad_types(self):
        previous_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(4300)
            with self.assertRaisesRegex(installer.InstallError, 'invalid_json'):
                installer.parse_json('1' * 5000)
        finally:
            sys.set_int_max_str_digits(previous_limit)
        for bad_raw in (123, b'\xff'):
            with self.subTest(bad_raw=type(bad_raw).__name__), \
                    self.assertRaisesRegex(installer.InstallError, 'invalid_json'):
                installer.parse_json(bad_raw)
        with self.assertRaisesRegex(installer.InstallError, 'json_nesting_limit'):
            installer.parse_json('[' * 50000 + '1' + ']' * 50000)
        deep_utf16 = ('[' * 70 + '1' + ']' * 70).encode('utf-16-le')
        with self.assertRaisesRegex(installer.InstallError, 'json_nesting_limit'):
            installer.parse_json(deep_utf16)
        for raw in (
            '{"a": "' + '[' * 70 + '"}',
            '[1, "' + '{' * 70 + '"]',
            ('[1, "' + '{' * 70 + '"]').encode('utf-16-le'),
        ):
            self.assertEqual(installer.parse_json(raw), json.loads(raw))
        deep_masked = '{"a": "' + ']' * 100 + '", "b": ' + '[' * 120 + '1' + ']' * 120 + '}'
        with self.assertRaisesRegex(installer.InstallError, 'json_nesting_limit'):
            installer.parse_json(deep_masked)

    def test_read_regular_validates_fstat_before_fdopen_without_leaking_fds(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subdir = root / 'subdir'
            subdir.mkdir()
            world = root / 'world.txt'
            world.write_text('data')
            world.chmod(0o644)
            probe = root / 'probe.txt'
            probe.write_text('ok')
            with probe.open('rb') as owned:
                for target, private in ((subdir, False), (world, True)):
                    before = os.dup(owned.fileno())
                    os.close(before)
                    with self.subTest(target=target.name), self.assertRaises(installer.InstallError):
                        installer.read_regular(target, private=private)
                    after = os.dup(owned.fileno())
                    os.close(after)
                    self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
