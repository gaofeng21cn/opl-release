"""Unified version metadata file reader and writer across ecosystems."""
from __future__ import annotations

import json
import plistlib
import re
import tomllib
from pathlib import Path

from .common import ReleaseError, read_json, safe_path, version_tuple, write_json


def read_declared_version(root: Path, version_files: list[dict]) -> str:
    if not version_files:
        raise ReleaseError('No version_files declared')
    item = version_files[0]
    path = safe_path(root, item['path'])
    kind = item['kind']
    if kind == 'plist_bundle':
        version = plistlib.loads(path.read_bytes())['CFBundleShortVersionString']
    elif kind == 'toml_project':
        version = tomllib.loads(path.read_text(encoding='utf-8'))['project']['version']
    elif kind == 'python_version':
        match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']', path.read_text(encoding='utf-8'))
        if not match:
            raise ReleaseError(f'Missing __version__ in {item["path"]}')
        version = match[1]
    elif kind == 'xml_project':
        match = re.search(r'<Version>([^<]+)</Version>', path.read_text(encoding='utf-8'))
        if not match:
            raise ReleaseError(f'Missing <Version> in {item["path"]}')
        version = match[1]
    else:
        data = read_json(path)
        field = item.get('field', 'version')
        version = data['package']['version'] if field == 'package.version' else data['version']
    version_tuple(version)
    return version


def update_version_files(root: Path, refs: list[dict], version: str) -> set[str]:
    version_tuple(version)
    changed: set[str] = set()
    for item in refs:
        p = safe_path(root, item['path'])
        kind = item['kind']
        if kind == 'json':
            d = read_json(p)
            field = item.get('field', 'version')
            if field == 'package.version' and isinstance(d.get('package'), dict) and 'version' in d['package']:
                d['package']['version'] = version
            elif field == 'version':
                d['version'] = version
                if isinstance(d.get('packages', {}).get(''), dict):
                    d['packages']['']['version'] = version
            else:
                raise ReleaseError(f'Cannot update declared JSON field {item["path"]}#{field}')
            write_json(p, d)
        elif kind == 'toml_project':
            text = p.read_text(encoding='utf-8')
            project_name = tomllib.loads(text)['project']['name']
            text, count = re.subn(
                r'(?ms)(^\[project\]\s*\n(?:(?!^\[).)*?^version\s*=\s*)["\'][^"\']+["\']',
                lambda m: m[1] + json.dumps(version),
                text,
                count=1,
            )
            if count != 1:
                raise ReleaseError(f'Cannot update version in {item["path"]}')
            p.write_text(text, encoding='utf-8')
            lock_path = p.with_name('uv.lock')
            if lock_path.is_file():
                lock = lock_path.read_text(encoding='utf-8')
                replacements = 0

                def align_local(match):
                    nonlocal replacements
                    block = match[0]
                    pkg = tomllib.loads(block)['package'][0]
                    src = pkg.get('source', {})
                    if pkg['name'] == project_name and (src.get('editable') == '.' or src.get('virtual') == '.'):
                        replacements += 1
                        return re.sub(r'(?m)^version\s*=.*$', 'version = ' + json.dumps(version), block, count=1)
                    return block

                lock = re.sub(r'(?ms)^\[\[package\]\]\n.*?(?=^\[\[package\]\]|\Z)', align_local, lock)
                if replacements != 1:
                    raise ReleaseError('Cannot align local project version in uv.lock')
                lock_path.write_text(lock, encoding='utf-8')
                changed.add(str(lock_path.relative_to(root)))
        elif kind == 'python_version':
            text, count = re.subn(r'(?m)^__version__\s*=.*$', '__version__ = ' + json.dumps(version), p.read_text(encoding='utf-8'))
            if count != 1:
                raise ReleaseError(f'Cannot update __version__ in {item["path"]}')
            p.write_text(text, encoding='utf-8')
        elif kind == 'plist_bundle':
            original = p.read_bytes()
            d = plistlib.loads(original)
            if d.get('CFBundleShortVersionString') != version:
                build = str(d.get('CFBundleVersion', '0'))
                d['CFBundleVersion'] = str(int(build) + 1) if build.isdigit() else version
            d['CFBundleShortVersionString'] = version
            p.write_bytes(plistlib.dumps(d, fmt=plistlib.FMT_BINARY if original.startswith(b'bplist') else plistlib.FMT_XML, sort_keys=False))
        elif kind == 'xml_project':
            text, count = re.subn(r'(<Version>)[^<]+(</Version>)', lambda m: m[1] + version + m[2], p.read_text(encoding='utf-8'))
            if count != 1:
                raise ReleaseError(f'Cannot update XML <Version> in {item["path"]}')
            p.write_text(text, encoding='utf-8')
        else:
            raise ReleaseError(f'Unsupported version file kind: {kind!r}')
        changed.add(item['path'])
    return changed
