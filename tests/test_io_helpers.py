from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import io_helpers


class AtomicWriteTests(unittest.TestCase):
    def test_working_directory_access_errors_are_not_treated_as_absence(self):
        with mock.patch.object(Path, 'cwd', side_effect=PermissionError('cwd denied')):
            with self.assertRaisesRegex(PermissionError, 'cwd denied'):
                io_helpers.reject_working_directory_overlap(Path('/output'))

    def test_cleanup_failure_preserves_primary_error_and_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'value.json'
            target.write_text('previous')
            with mock.patch.object(io_helpers.os, 'replace', side_effect=OSError('replace failed')), \
                    mock.patch.object(Path, 'unlink', side_effect=PermissionError('cleanup denied')), \
                    redirect_stderr(io.StringIO()) as warnings:
                with self.assertRaisesRegex(OSError, '^replace failed$'):
                    io_helpers.atomic_text(target, 'replacement')
            self.assertEqual(target.read_text(), 'previous')
            remaining = list(Path(folder).glob('.value.json.*.tmp'))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].read_text(), 'replacement')
            self.assertIn(str(remaining[0]), warnings.getvalue())

    def test_success_does_not_clean_a_path_already_renamed(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'value.json'
            with mock.patch.object(Path, 'unlink', side_effect=AssertionError('unexpected cleanup')):
                io_helpers.atomic_text(target, 'complete\n')
            self.assertEqual(target.read_bytes(), b'complete\n')
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_json_depth_handles_utf16_strings_and_unterminated_escapes(self):
        import json as _json
        safe_utf16 = _json.dumps('"' + '[' * 70).encode('utf-16-le')
        self.assertEqual(io_helpers.parse_json(safe_utf16), '"' + '[' * 70)
        deep_utf16 = ('[' + _json.dumps('"') + ',' + '[' * 65 + '"x"' + ']' * 65 + ']').encode('utf-16-le')
        with self.assertRaisesRegex(ValueError, 'nesting exceeds limit'):
            io_helpers.parse_json(deep_utf16)
        for bad_raw in (b'"' + b'\\"' * 50_000, b'"' + b'\\"' * 500_000 + b'\\'):
            with self.assertRaises(ValueError):
                io_helpers.parse_json(bad_raw)
        with self.assertRaises(TypeError):
            io_helpers.parse_json(123)
        import json as _json
        for raw in (
            '{"a": "' + '[' * 70 + '"}',
            '[1, "' + '{' * 70 + '"]',
            ('[1, "' + '{' * 70 + '"]').encode('utf-16-le'),
        ):
            self.assertEqual(io_helpers.parse_json(raw), _json.loads(raw))
        deep_masked = '{"a": "' + ']' * 100 + '", "b": ' + '[' * 120 + '1' + ']' * 120 + '}'
        with self.assertRaisesRegex(ValueError, 'JSON nesting exceeds limit'):
            io_helpers.parse_json(deep_masked)

    def test_atomic_text_guards_input_type_and_symlink_modes(self):
        import stat
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            uncreated = root / 'missing_parent' / 'file.txt'
            for bad_text in (b'bytes', 123, None):
                with self.subTest(bad_text=type(bad_text).__name__), self.assertRaises(TypeError):
                    io_helpers.atomic_text(uncreated, bad_text)
            self.assertFalse(uncreated.parent.exists())

            secret = root / 'secret.txt'
            secret.write_text('secret')
            secret.chmod(0o600)
            link = root / 'link.txt'
            link.symlink_to(secret)
            io_helpers.atomic_text(link, 'replaced\n', mode=0o644)
            self.assertFalse(link.is_symlink())
            self.assertEqual(stat.S_IMODE(link.lstat().st_mode), 0o644)
            self.assertEqual(secret.read_text(), 'secret')

            regular = root / 'regular.txt'
            regular.write_text('old')
            regular.chmod(0o600)
            io_helpers.atomic_text(regular, 'new\n', mode=0o644)
            self.assertEqual(stat.S_IMODE(regular.lstat().st_mode), 0o600)

            public = root / 'public.txt'
            public.write_text('old')
            public.chmod(0o644)
            io_helpers.atomic_text(public, 'private\n', mode=0o600, preserve_mode=False)
            self.assertEqual(stat.S_IMODE(public.lstat().st_mode), 0o600)

    def test_read_regular_bytes_leaks_zero_fds_on_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            world_file = root / 'world.txt'
            world_file.write_text('hello')
            world_file.chmod(0o644)
            big_file = root / 'big.txt'
            big_file.write_bytes(b'x' * 20)
            probe_file = root / 'probe.txt'
            probe_file.write_text('probe')
            cases = [
                (root, None, False),
                (fifo, None, False),
                (world_file, None, True),
                (big_file, 10, False),
            ]
            with probe_file.open('rb') as owned:
                for target, limit, private in cases:
                    probe_before = os.dup(owned.fileno())
                    os.close(probe_before)
                    with self.subTest(target=target.name), self.assertRaises(ValueError):
                        io_helpers.read_regular_bytes(target, limit, private=private)
                    probe_after = os.dup(owned.fileno())
                    os.close(probe_after)
                    self.assertEqual(probe_after, probe_before)


if __name__ == '__main__':
    unittest.main()
