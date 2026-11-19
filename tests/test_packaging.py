import importlib.util
import hashlib
import json
import os
import stat
import contextlib
import io
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import io_helpers
spec = importlib.util.spec_from_file_location('previews', ROOT / 'scripts/package_previews.py')
previews = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previews)


class PackagingChecks(unittest.TestCase):
    def fixture(self, root):
        (root / 'index.html').write_text('<link rel="stylesheet" href="style.css"><script src="data.js"></script><script src="app.js"></script>')
        (root / 'style.css').write_text('body{color:green}')
        (root / 'app.js').write_text('void window.ONIONSURGE_DATA;')
        (root / 'data.js').write_text('window.ONIONSURGE_DATA={"value":"</script><script>alert(1)</script>"};')
        self.update_manifest(root)

    def update_manifest(self, root):
        files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file() and p.name != 'build-manifest.json'}
        (root/'build-manifest.json').write_text(json.dumps({'format': previews.BUILD_FORMAT, 'files': files})+'\n')

    def test_live_preview_is_rejected_before_a_new_reservation(self):
        for manifest_mode in [False, True]:
            with self.subTest(manifest_mode=manifest_mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / 'live'; root.mkdir(); self.fixture(root)
                if manifest_mode:
                    manifest = json.loads((root / 'build-manifest.json').read_text())
                    manifest['mode'] = 'live'
                    (root / 'build-manifest.json').write_text(json.dumps(manifest))
                else:
                    page = root / 'index.html'
                    page.write_text('<html data-mode="live">' + page.read_text())
                    (root / 'data.js').unlink()  # A production frontend need not carry sample data.
                    self.update_manifest(root)
                before = {file.name: file.read_bytes() for file in root.iterdir()}
                with self.assertRaisesRegex(ValueError, 'Live builds cannot'):
                    previews.package(root)
                self.assertFalse(io_helpers.output_claim(root).exists())
                self.assertEqual({file.name: file.read_bytes() for file in root.iterdir()}, before)

    def test_data_cannot_terminate_the_inline_script(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir()
            self.fixture(root)
            previews.package(root)
            manifest = json.loads((root / 'build-manifest.json').read_text())
            for design in range(1, 4):
                page = (root / f'design-{design}.html').read_text()
                self.assertEqual(page.count('</script>'), 2)
                self.assertIn('<meta http-equiv="Content-Security-Policy"', page)
                self.assertIn('\\u003c/script>', page)
                self.assertIn(f'window.DEFAULT_DESIGN="{design}"', page)
                self.assertEqual(manifest['files'][f'design-{design}.html'], hashlib.sha256(page.encode('utf-8')).hexdigest())

    def test_asset_hashes_are_read_only_from_the_stage(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()/'dist'; root.mkdir(); self.fixture(root)
            read_bytes = Path.read_bytes
            asset_reads = []
            def read(path):
                if path.name in ('index.html', 'style.css', 'data.js', 'app.js'):
                    asset_reads.append(path)
                return read_bytes(path)
            with mock.patch.object(Path, 'read_bytes', read):
                previews.package(root)
            self.assertEqual(sorted(path.name for path in asset_reads),
                             ['app.js', 'data.js', 'index.html', 'style.css'])
            self.assertTrue(all(path.parent != root for path in asset_reads))

    def test_invalid_manifest_changes_no_previews(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
            (root / 'design-1.html').write_text('previous preview')
            for invalid in [None, [], {}, {'files': []}]:
                manifest = json.dumps(invalid)
                (root / 'build-manifest.json').write_text(manifest)
                with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, 'files map'):
                    previews.package(root)
                self.assertEqual((root / 'design-1.html').read_text(), 'previous preview')
                self.assertEqual((root / 'build-manifest.json').read_text(), manifest)
                self.assertFalse((root / 'design-2.html').exists())
            (root / 'build-manifest.json').unlink()
            with contextlib.redirect_stderr(io.StringIO()) as messages:
                previews.package(root)
            self.assertIn('Unverified previous generation preserved', messages.getvalue())
            self.assertTrue((root / 'design-3.html').is_file())

    def test_build_lock_covers_every_read_and_write(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
            held = False
            @contextlib.contextmanager
            def lock(directory, **kwargs):
                nonlocal held
                self.assertEqual(directory, root.resolve())
                held = True
                try:
                    yield
                finally:
                    held = False
            read_text, write_bytes = Path.read_text, Path.write_bytes
            read_json, atomic_text = previews.read_json, previews.atomic_text
            def read(path, *args, **kwargs):
                self.assertTrue(held)
                return read_text(path, *args, **kwargs)
            def write(path, data):
                self.assertTrue(held)
                self.assertIsInstance(data, bytes)
                self.assertTrue(data.endswith(b'\n') if path.name == 'build-manifest.json' else True)
                return write_bytes(path, data)
            def read_manifest(path):
                self.assertTrue(held)
                return read_json(path)
            def replace_manifest(path, text):
                self.assertTrue(held)
                return atomic_text(path, text)
            with mock.patch.object(previews, 'output_lock', lock), mock.patch.object(Path, 'read_text', read), mock.patch.object(Path, 'write_bytes', write), mock.patch.object(previews, 'read_json', read_manifest), mock.patch.object(previews, 'atomic_text', replace_manifest):
                previews.package(root)
            self.assertFalse(held)

    def test_missing_generation_can_appear_while_waiting_for_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve() / 'dist'
            claim = io_helpers.output_claim(root); claim.touch()
            file_lock = io_helpers.file_lock
            checked = previews.validate_source
            held = False
            @contextlib.contextmanager
            def promote_before_lock_returns(path):
                nonlocal held
                with file_lock(path):
                    if path == claim:
                        root.mkdir(); self.fixture(root)
                        held = True
                    try:
                        yield
                    finally:
                        held = False
            def check_under_output_lock(path):
                self.assertTrue(held)
                return checked(path)
            with mock.patch.object(io_helpers, 'file_lock', promote_before_lock_returns), \
                    mock.patch.object(previews, 'validate_source', side_effect=check_under_output_lock) as check:
                previews.package(root)
                check.assert_called_once()
            self.assertTrue((root / 'design-1.html').is_file())
            with self.assertRaises(FileNotFoundError):
                previews.package(root.parent / 'absent')

    def test_invalid_new_preview_source_leaves_no_claim_or_created_parents(self):
        for kind in ['missing', 'file', 'empty', 'junk', 'missing-core', 'core-directory', 'symlink', 'manifest']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as name:
                parent = Path(name).resolve()/'public'; source = parent/'level/typo'
                if kind == 'file':
                    source.parent.mkdir(parents=True); source.write_text('keep this file')
                elif kind != 'missing':
                    source.mkdir(parents=True)
                    if kind == 'junk':
                        (source/'unrelated').write_text('preserve')
                    elif kind not in ['empty', 'junk']:
                        self.fixture(source)
                        if kind == 'missing-core':
                            (source/'app.js').unlink()
                        elif kind == 'core-directory':
                            (source/'app.js').unlink(); (source/'app.js').mkdir()
                        elif kind == 'symlink':
                            (source/'app.js').unlink(); (source/'app.js').symlink_to(source/'style.css')
                        else:
                            (source/'build-manifest.json').write_text('{"files": []}')
                expected = FileNotFoundError if kind in ['missing', 'empty', 'junk', 'missing-core'] else ValueError
                with self.assertRaises(expected):
                    previews.package(source)
                self.assertFalse(io_helpers.output_claim(source).exists())
                if kind == 'missing':
                    self.assertFalse(parent.exists())
                    parent.mkdir()
                else:
                    if kind == 'file':
                        self.assertEqual(source.read_text(), 'keep this file')
                        source.unlink()
                    else:
                        self.assertTrue(source.is_dir())
                        previews.shutil.rmtree(source)
                    source.parent.rmdir()
                self.fixture(parent)
                previews.package(parent)
                self.assertTrue((parent/'design-1.html').is_file())

    def test_stale_digest_retains_reservation_until_explicit_retirement(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name).resolve()/'public'; source = parent/'nested'
            source.mkdir(parents=True); self.fixture(source)
            reviewed_app = (source/'app.js').read_bytes()
            (source/'app.js').write_bytes(b'void "unreviewed edit";\n')
            before = {p.name: p.read_bytes() for p in source.iterdir()}
            claim = io_helpers.output_claim(source)
            self.assertFalse(claim.exists())
            with self.assertRaisesRegex(ValueError, 'recorded hash'):
                previews.package(source)
            self.assertEqual({p.name: p.read_bytes() for p in source.iterdir()}, before)
            self.assertTrue(claim.is_file())
            identity = claim.stat()
            with self.assertRaisesRegex(ValueError, 'reserved descendant'):
                previews.package(parent)
            self.assertFalse(io_helpers.output_claim(parent).exists())

            (source/'app.js').write_bytes(reviewed_app)
            previews.package(source)
            self.assertTrue((source/'design-1.html').is_file())
            current = claim.stat()
            self.assertEqual((current.st_dev, current.st_ino), (identity.st_dev, identity.st_ino))

            # All writers have exited. Deleting the generation alone is not
            # retirement; remove its persistent claim deliberately as well.
            previews.shutil.rmtree(source)
            with self.assertRaisesRegex(ValueError, 'reserved descendant'):
                previews.package(parent)
            claim.unlink()
            self.fixture(parent)
            previews.package(parent)
            self.assertTrue((parent/'design-1.html').is_file())

    def test_manifest_publish_failure_preserves_readable_previous_marker(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
            previous = (root/'build-manifest.json').read_bytes()
            with mock.patch('io_helpers.os.replace', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    previews.package(root)
            self.assertEqual((root / 'build-manifest.json').read_bytes(), previous)
            self.assertEqual(json.loads(previous)['format'], previews.BUILD_FORMAT)
            previews.package(root)
            self.assertTrue({f'design-{n}.html' for n in range(1, 4)} <= set(json.loads((root / 'build-manifest.json').read_bytes())['files']))

    def test_changed_previews_and_manifest_publish_as_one_generation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'public'; root.mkdir(); self.fixture(root)
            previews.package(root)
            old_preview = (root/'design-1.html').read_bytes()
            (root/'app.js').write_bytes(b'void "changed preview output";\n')
            (root/'exports').mkdir()
            (root/'exports/example-1mo.csv').write_bytes(b'utc,value\n0,1\n')
            manifest = json.loads((root/'build-manifest.json').read_bytes())
            for asset in ['index.html', 'style.css', 'data.js', 'app.js', 'exports/example-1mo.csv']:
                manifest['files'][asset] = hashlib.sha256((root/asset).read_bytes()).hexdigest()
            (root/'build-manifest.json').write_text(json.dumps(manifest)+'\n')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            candidate = []
            def fail_manifest(path, text):
                candidate.append((path.parent/'design-1.html').read_bytes())
                raise OSError('manifest publication failed')
            with mock.patch.object(previews, 'atomic_text', fail_manifest):
                with self.assertRaisesRegex(OSError, 'manifest publication failed'):
                    previews.package(root)
            self.assertEqual(len(candidate), 1)
            self.assertNotEqual(candidate[0], old_preview, 'the attempted previews must actually differ')
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(after, before)
            for asset, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((root/asset).read_bytes()).hexdigest(), expected)
            previews.package(root)
            self.assertNotEqual((root/'design-1.html').read_bytes(), old_preview)
            latest = json.loads((root/'build-manifest.json').read_bytes())
            for asset, expected in latest['files'].items():
                self.assertEqual(hashlib.sha256((root/asset).read_bytes()).hexdigest(), expected)
            for asset in ['index.html', 'style.css', 'data.js', 'app.js', 'exports/example-1mo.csv']:
                self.assertEqual((root/asset).read_bytes(), before[asset])

    def test_partial_preview_write_failure_leaves_previous_generation_unchanged(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'public'; root.mkdir(); self.fixture(root)
            previews.package(root)
            old_preview = (root/'design-1.html').read_bytes()
            (root/'app.js').write_bytes(b'void \"changed before failed second write\";\n')
            manifest = json.loads((root/'build-manifest.json').read_text())
            manifest['files']['app.js'] = hashlib.sha256((root/'app.js').read_bytes()).hexdigest()
            (root/'build-manifest.json').write_text(json.dumps(manifest))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            attempted = []
            write = Path.write_bytes
            def disk_full(path, data):
                if path.name == 'design-1.html':
                    attempted.append(data)
                if path.name == 'design-2.html':
                    raise OSError('disk full after first preview')
                return write(path, data)
            with mock.patch.object(Path, 'write_bytes', disk_full):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    previews.package(root)
            self.assertEqual(len(attempted), 1)
            self.assertNotEqual(attempted[0], old_preview)
            self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)

    def test_symlinked_source_assets_are_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name); root = parent/'public'; root.mkdir(); self.fixture(root)
            external = parent/'external.js'; external.write_text('private source')
            (root/'app.js').unlink(); (root/'app.js').symlink_to(external)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                previews.package(root)
            self.assertTrue((root/'app.js').is_symlink())
            self.assertFalse((root/'design-1.html').exists())

    def test_editor_save_with_stale_manifest_is_rejected_not_blessed(self):
        for update_manifest in [False, True]:
            with self.subTest(reviewed=update_manifest), tempfile.TemporaryDirectory() as name:
                root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
                before = (root/'build-manifest.json').read_bytes()
                copytree = previews.shutil.copytree
                def editor_saves_before_copy(source, stage, **kwargs):
                    (source/'app.js').write_bytes(b'void "editor-saved-version";\n')
                    if update_manifest:
                        self.update_manifest(source)
                    return copytree(source, stage, **kwargs)
                with mock.patch.object(previews.shutil, 'copytree', editor_saves_before_copy):
                    if update_manifest:
                        previews.package(root)
                    else:
                        with self.assertRaisesRegex(ValueError, 'recorded hash'):
                            previews.package(root)
                if update_manifest:
                    self.assertIn((root/'app.js').read_text(), (root/'design-1.html').read_text())
                else:
                    self.assertFalse((root/'design-1.html').exists())
                    self.assertEqual((root/'build-manifest.json').read_bytes(), before)

    def test_packaging_rejects_working_directory_inside_the_replaced_tree(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root); (root/'nested').mkdir()
            original = Path.cwd()
            try:
                for cwd in [root, root/'nested']:
                    os.chdir(cwd)
                    with self.assertRaisesRegex(ValueError, 'outside directory'):
                        previews.package(root)
                    self.assertTrue(Path.cwd().is_dir())
                    self.assertFalse((root/'design-1.html').exists())
            finally:
                os.chdir(original)

    @unittest.skipUnless(os.name == 'posix', 'POSIX group access')
    def test_packaging_restores_source_groups_and_modes(self):
        groups = [gid for gid in os.getgroups() if gid != os.getegid()]
        if not groups:
            self.skipTest('No supplementary group available')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
            gid = groups[0]
            try:
                os.chown(root, -1, gid)
                os.chown(root/'data.js', -1, gid)
            except PermissionError:
                self.skipTest('Filesystem does not permit supplementary-group assignment')
            root.chmod(0o750)
            (root/'data.js').chmod(0o640)
            original = {p.relative_to(root): (p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in [root, *root.rglob('*')]}
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            copytree, chown = previews.shutil.copytree, os.chown
            def force_mismatch(source, stage, **kwargs):
                result = copytree(source, stage, **kwargs)
                chown(stage/'data.js', -1, os.getegid())
                return result
            with mock.patch.object(previews.shutil, 'copytree', force_mismatch), \
                    mock.patch.object(previews.os, 'chown', side_effect=PermissionError('cannot restore group')):
                with self.assertRaisesRegex(PermissionError, 'restore group'):
                    previews.package(root)
            self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
            previews.package(root)
            for relative, expected in original.items():
                info = (root/relative).stat()
                self.assertEqual((info.st_gid, stat.S_IMODE(info.st_mode)), expected)
            self.assertEqual((root/'design-1.html').stat().st_gid, gid)

    def test_terminal_parent_path_is_rejected_before_packaging_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root); (root/'exports').mkdir()
            with mock.patch.object(previews, 'output_lock') as lock:
                with self.assertRaisesRegex(ValueError, 'cannot end with'):
                    previews.package(root/'exports'/'..')
                lock.assert_not_called()
            alias = root.parent/(root.name + '-alias')
            try:
                alias.symlink_to(root, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    previews.package(alias)
            finally:
                alias.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission modes')
    def test_atomic_manifest_update_preserves_public_and_private_modes(self):
        from io_helpers import atomic_text
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root)
            manifest = root/'build-manifest.json'
            for mode in [0o644, 0o600]:
                self.update_manifest(root)
                manifest.chmod(mode)
                previews.package(root)
                self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), mode)
                self.assertTrue({f'design-{n}.html' for n in range(1, 4)} <= set(json.loads(manifest.read_text())['files']))
            new_cache = root/'new-cache.json'
            atomic_text(new_cache, '{}\n')
            self.assertEqual(stat.S_IMODE(new_cache.stat().st_mode), 0o600)

    def test_shared_normalization_keeps_equivalent_lock_paths_together(self):
        from io_helpers import normalized_output_path, output_lock
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); (root/'intermediate').mkdir()
            self.assertEqual(normalized_output_path(root/'dist'), normalized_output_path(root/'intermediate'/'..'/'dist'))
            with self.assertRaisesRegex(ValueError, 'cannot end with'):
                with output_lock(root/'dist'/'..'):
                    self.fail('ambiguous lock path accepted')

    def test_manifest_requires_canonical_posix_paths(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root); previews.package(root)
            original = json.loads((root/'build-manifest.json').read_text())
            old_preview = (root/'design-1.html').read_bytes()
            for alias in ['./design-1.html', 'design-1.html/', 'exports/../design-1.html', 'exports//value.csv', 'exports\\value.csv']:
                manifest = json.loads(json.dumps(original))
                manifest['files'][alias] = original['files']['design-1.html']
                (root/'build-manifest.json').write_text(json.dumps(manifest))
                with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, 'Invalid staged manifest'):
                    previews.package(root)
                self.assertEqual((root/'design-1.html').read_bytes(), old_preview)

    def test_manifest_rejects_physical_preview_aliases_before_copying(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)/'dist'; root.mkdir(); self.fixture(root); previews.package(root)
            original = json.loads((root/'build-manifest.json').read_text())
            alias = root/'preview-alias.html'
            os.link(root/'design-1.html', alias)
            manifest = json.loads(json.dumps(original))
            manifest['files'][alias.name] = original['files']['design-1.html']
            (root/'build-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'aliases a regenerated'):
                previews.package(root)
            alias.unlink()
            upper = root/'DESIGN-1.HTML'
            manifest = json.loads(json.dumps(original))
            if upper.exists() and upper.samefile(root/'design-1.html'):
                manifest['files'][upper.name] = original['files']['design-1.html']
                (root/'build-manifest.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'aliases a regenerated'):
                    previews.package(root)
            else:
                upper.write_text('distinct case-sensitive asset')
                manifest['files'][upper.name] = hashlib.sha256(upper.read_bytes()).hexdigest()
                (root/'build-manifest.json').write_text(json.dumps(manifest))
                previews.package(root)
                self.assertEqual(upper.read_text(), 'distinct case-sensitive asset')

    def test_missing_or_duplicate_asset_markers_fail(self):
        for text in ('', 'marker marker'):
            with self.assertRaises(ValueError):
                previews.replace_once(text, 'marker', 'replacement')

    def test_packaging_rejects_undeclared_files_rawtext_breakouts_and_emits_csp(self):
        import base64, re
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'dist'; root.mkdir()
            (root / 'index.html').write_text(
                '<!doctype html><html><head><title>Test</title>'
                '<link rel="stylesheet" href="style.css"></head>'
                '<body><script src="data.js"></script><script src="app.js"></script></body></html>')
            (root / 'style.css').write_text('body{color:green}')
            (root / 'app.js').write_text('void window.ONIONSURGE_DATA;')
            (root / 'data.js').write_text('window.ONIONSURGE_DATA={};')
            self.update_manifest(root)
            previews.package(root)
            for design in range(1, 4):
                html = (root / f'design-{design}.html').read_text()
                policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html).group(1)
                style_body = re.search(r'<style>([\s\S]*?)</style>', html).group(1)
                scripts = re.findall(r'<script>([\s\S]*?)</script>', html)
                self.assertEqual(len(scripts), 2)
                style_hash = 'sha256-' + base64.b64encode(hashlib.sha256(style_body.encode()).digest()).decode()
                self.assertIn(style_hash, policy)
                for script in scripts:
                    script_hash = 'sha256-' + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                    self.assertIn(script_hash, policy)
            (root / 'undeclared.txt').write_text('leak')
            with self.assertRaisesRegex(ValueError, 'Undeclared staged asset'):
                previews.package(root)
            (root / 'undeclared.txt').unlink()
            (root / 'style.css').write_text('body{/* </style> */}')
            self.update_manifest(root)
            with self.assertRaisesRegex(ValueError, 'Stylesheet'):
                previews.package(root)
            (root / 'style.css').write_text('body{color:green}')
            (root / 'app.js').write_text('/* <!-- </script> */')
            self.update_manifest(root)
            with self.assertRaisesRegex(ValueError, 'Application script'):
                previews.package(root)

    def test_single_pass_inline_and_regular_manifest_and_index_checks(self):
        import re
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'dist'; root.mkdir()
            self.fixture(root)
            # CSS comments containing another asset marker must not hijack single-pass splicing
            (root / 'style.css').write_text('body{color:green}/* <script src="app.js"></script> */')
            (root / 'data.js').write_text('window.ONIONSURGE_DATA={"x":"<link rel=\\"stylesheet\\" href=\\"style.css\\">"};')
            self.update_manifest(root)
            previews.package(root)
            for design in range(1, 4):
                html = (root / f'design-{design}.html').read_text()
                self.assertEqual(len(re.findall(r'<script>', html)), 2)
                self.assertIn('/* <script src="app.js"></script> */', html)

            # Dangling or valid symlink at build-manifest.json is rejected rather than ignored
            (root / 'build-manifest.json').unlink()
            (root / 'build-manifest.json').symlink_to(root / 'missing-manifest.json')
            with self.assertRaisesRegex(ValueError, 'regular file'):
                previews.package(root)

            # Directory index.html is rejected before reading
            (root / 'build-manifest.json').unlink()
            (root / 'index.html').unlink()
            (root / 'index.html').mkdir()
            with self.assertRaisesRegex(ValueError, 'regular file: index.html'):
                previews.package(root)

    def test_hard_linked_preview_names_sharing_inode_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'dist'
            root.mkdir()
            self.fixture(root)
            previews.package(root)
            manifest = json.loads((root / 'build-manifest.json').read_text())
            (root / 'design-1.html').unlink()
            os.link(root / 'design-2.html', root / 'design-1.html')
            del manifest['files']['design-1.html']
            (root / 'build-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'aliases a regenerated preview'):
                previews.package(root)
