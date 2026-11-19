#!/usr/bin/env python3
"""Embed three offline chart previews; downloads require the complete build."""
import argparse
import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from io_helpers import BUILD_FORMAT, atomic_text, file_sha256, json_text, normalized_output_path, output_lock, read_json, reject_working_directory_overlap, sha256_hex, staged_generation
from windows import is_sha256, require

ROOT = Path(__file__).resolve().parents[1]
CORE_ASSETS = ('index.html', 'style.css', 'data.js', 'app.js')
PREVIEW_NAMES = tuple(f'design-{design}.html' for design in range(1, 4))
_STYLE_END_RE = re.compile(r'</style', re.IGNORECASE)
_SCRIPT_END_RE = re.compile(r'</script|<!--', re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r'(<html\b[^>]*>)', re.IGNORECASE)
_ASSET_MARKER_RE = re.compile(r'<link rel="stylesheet" href="style\.css">|<script src="(?:data|app)\.js"></script>')


def replace_once(text, marker, replacement):
    require(text.count(marker) == 1, f'Expected one asset marker: {marker}')
    return text.replace(marker, replacement, 1)


def _scan_tree(directory, check_uid=None):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Preview source paths must be regular files/directories, not symlinks')
    if check_uid is not None and info.st_uid != check_uid:
        raise ValueError('Package as the same user that owns the build output')
    access = {Path('.'): (info.st_gid, stat.S_IMODE(info.st_mode))}
    regular_files = set()
    for path in directory.rglob('*'):
        pinfo = path.lstat()
        mode = pinfo.st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('Preview source paths must be regular files/directories, not symlinks')
        if check_uid is not None and pinfo.st_uid != check_uid:
            raise ValueError('Package as the same user that owns the build output')
        rel = path.relative_to(directory)
        access[rel] = pinfo.st_gid, stat.S_IMODE(mode)
        if stat.S_ISREG(mode):
            regular_files.add(rel.as_posix())
    return access, regular_files


def validate_source(directory):
    access, regular_files = _scan_tree(directory, check_uid=os.geteuid())
    for name in CORE_ASSETS:
        if Path(name) in access:
            require(name in regular_files, f'Required preview asset must be a regular file: {name}')
    if Path('index.html') not in access:
        (directory / 'index.html').lstat()
    require('data-mode="live"' not in (directory / 'index.html').read_text(encoding='utf-8'),
            'Live builds cannot be packaged as offline previews')
    for name in CORE_ASSETS[1:]:
        if Path(name) not in access:
            (directory / name).lstat()
    checked_manifest(directory, verify_hashes=False, existing_files=regular_files)
    return access


def package(directory):
    directory = normalized_output_path(directory)
    reject_working_directory_overlap(directory)
    with output_lock(directory, before_register=validate_source):
        access = validate_source(directory)
        directory = directory.resolve(strict=True)
        with staged_generation(directory) as stage:
            shutil.copytree(directory, stage, dirs_exist_ok=True, symlinks=True)
            stage_files = regular_tree(stage)
            previews, manifest = preview_bytes(stage, existing_files=stage_files)
            for name, encoded in previews.items():
                (stage / name).write_bytes(encoded)
            if manifest is not None:
                atomic_text(stage / 'build-manifest.json', manifest)
            # copytree preserves modes, not groups; atomic manifest replacement
            # also creates a new inode. Restore access before promotion.
            default_gid = access[Path('.')][0]
            for path in [stage, *stage.rglob('*')]:
                info = path.stat()
                gid, mode = access.get(path.relative_to(stage), (default_gid, stat.S_IMODE(info.st_mode)))
                if info.st_gid != gid:
                    os.chown(path, -1, gid)
                os.chmod(path, mode)


def regular_tree(directory):
    return _scan_tree(directory)[1]


def checked_manifest(directory, verify_hashes=True, *, existing_files=None):
    manifest_path = directory / 'build-manifest.json'
    try:
        manifest_info = manifest_path.lstat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(manifest_info.st_mode), 'Preview manifest must be a regular file')
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ValueError('Preview manifest must contain a files map')
    if manifest.get('mode', 'offline') != 'offline':
        raise ValueError('Live builds cannot be packaged as offline previews')
    if manifest.get('format') != BUILD_FORMAT:
        raise ValueError('Preview manifest format is unsupported')
    preview_ids = {}
    for p in PREVIEW_NAMES:
        dp = directory / p
        if dp.is_file():
            pinfo = dp.stat()
            preview_ids.setdefault((pinfo.st_dev, pinfo.st_ino), set()).add(p)
    for name, expected in manifest['files'].items():
        if not isinstance(name, str) or '\\' in name or '\0' in name or not is_sha256(expected):
            raise ValueError('Invalid staged manifest entry')
        relative = PurePosixPath(name)
        require(relative.parts and not relative.is_absolute() and '..' not in relative.parts
                and name == relative.as_posix(), 'Invalid staged manifest entry')
        path = directory / relative
        try:
            info = path.stat()
        except OSError as error:
            raise ValueError(f'Staged asset differs from its recorded hash: {name}') from error
        require(stat.S_ISREG(info.st_mode) and (not verify_hashes or file_sha256(path) == expected),
                f'Staged asset differs from its recorded hash: {name}')
        aliases = preview_ids.get((info.st_dev, info.st_ino))
        require(not aliases or aliases == {name}, 'Manifest path aliases a regenerated preview')
    declared = set(manifest['files'])
    for rel in (regular_tree(directory) if existing_files is None else existing_files):
        require(rel == 'build-manifest.json' or rel in PREVIEW_NAMES or rel in declared,
                f'Undeclared staged asset: {rel}')
    return manifest


def csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii') + "'"


def preview_bytes(directory, *, existing_files=None):
    page = (directory / CORE_ASSETS[0]).read_text(encoding='utf-8')
    assets = {name: (directory / name).read_text(encoding='utf-8') for name in CORE_ASSETS[1:]}
    require(not _STYLE_END_RE.search(assets['style.css']), 'Stylesheet cannot close inline <style>')
    require(not _SCRIPT_END_RE.search(assets['app.js']), 'Application script cannot close or comment out inline <script>')
    manifest = checked_manifest(directory, existing_files=existing_files)
    # Check all three asset markers on the original page before inline splicing.
    for marker in ('<link rel="stylesheet" href="style.css">', '<script src="app.js"></script>', '<script src="data.js"></script>'):
        require(page.count(marker) == 1, f'Expected one asset marker: {marker}')
    require('<head>' not in page or page.count('<head>') == 1, 'Expected at most one <head> element')
    # The JSON assignment may contain HTML strings from an input bundle.
    data = assets['data.js'].replace('<', '\\u003c')
    style_hash, app_hash = csp_hash(assets['style.css']), csp_hash(assets['app.js'])
    previews = {}
    for design in range(1, 4):
        data_script = f'window.DEFAULT_DESIGN="{design}";' + data
        output = page
        policy = (f"default-src 'none'; script-src {csp_hash(data_script)} {app_hash}; "
                  f"style-src 'unsafe-inline'; style-src-elem {style_hash}; style-src-attr 'unsafe-inline'; "
                  "img-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
        meta = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
        if '<head>' in output:
            output = replace_once(output, '<head>', f'<head>{meta}')
        elif _HTML_OPEN_RE.search(output):
            output = _HTML_OPEN_RE.sub(r'\1' + f'<head>{meta}</head>', output, count=1)
        else:
            output = f'<head>{meta}</head>' + output
        replacements = {
            '<script src="data.js"></script>': f'<script>{data_script}</script>',
            '<link rel="stylesheet" href="style.css">': f'<style>{assets["style.css"]}</style>',
            '<script src="app.js"></script>': f'<script>{assets["app.js"]}</script>',
        }
        output = _ASSET_MARKER_RE.sub(lambda match: replacements[match.group(0)], output)
        name = f'design-{design}.html'
        encoded = output.encode('utf-8')
        previews[name] = encoded
        if manifest is not None:
            manifest['files'][name] = sha256_hex(encoded)
    return previews, json_text(manifest, pretty=True) if manifest is not None else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    try:
        package(args.directory)
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(1, f'Packaging failed: {error}\n')
    print(f'Created three offline chart previews in {args.directory}; keep the full build for CSV/JSON downloads.')


if __name__ == '__main__':
    main()
