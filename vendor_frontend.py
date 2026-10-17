#!/usr/bin/env python3
"""Vendor reviewed npm pack artifacts and update a frontend's public lockfile.

Run in the release preparation workspace before scanning and committing the
publish snapshot, never at user install time. npm lock generation preserves
existing public package pins and integrity records.
"""
import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit, urlunsplit


def compatible_version(version, requested):
    # Release manifests use exact or caret ranges. Refuse unknown syntax rather
    # than silently replacing a dependency with an incompatible local package.
    if requested.startswith('file:vendor/'):
        return True
    match = re.fullmatch(r'(\^?)(\d+)\.(\d+)\.(\d+)', requested)
    actual = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or not actual:
        return False
    minimum = tuple(map(int, match.groups()[1:]))
    current = tuple(map(int, actual.groups()))
    if not match.group(1):
        return current == minimum
    major, minor, patch = minimum
    maximum = (major + 1, 0, 0) if major else ((0, minor + 1, 0) if minor else (0, 0, patch + 1))
    return minimum <= current < maximum


def vendor(frontend, artifacts):
    package_file = frontend / 'package.json'
    lock_file = frontend / 'package-lock.json'
    package = json.loads(package_file.read_text())
    lock = json.loads(lock_file.read_text())
    available = {}
    for artifact in sorted(artifacts.glob('*.tgz')):
        with tarfile.open(artifact, 'r:gz') as archive:
            member = archive.getmember('package/package.json')
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError(f'invalid npm metadata: {artifact.name}')
            metadata = json.load(archive.extractfile(member))
        name = metadata['name']
        if not re.fullmatch(r'@shizuha/[a-z0-9][a-z0-9._-]*', name) or name in available:
            raise ValueError(f'invalid or duplicate private package: {name}')
        available[name] = (artifact, metadata)
    required = {name for section in ('dependencies', 'devDependencies', 'optionalDependencies')
                for name in package.get(section, {}) if name.startswith('@shizuha/')}
    missing = required - available.keys()
    if missing:
        raise ValueError('missing reviewed package tarballs: ' + ', '.join(sorted(missing)))
    # Refuse artifacts whose transitive private graph is not bundled. The current
    # UI/editor/Atlas libraries have no private npm dependency edges.
    for name in required:
        metadata = available[name][1]
        for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
            requested = package.get(section, {}).get(name)
            # Only the named platform monorepo edge may be replaced. This
            # does not authorize arbitrary file/path dependencies: the reviewed
            # tarball name, semantic version and digest become the public pin.
            monorepo = requested == 'file:../../packages/shizuha-' + name.split('/')[1]
            if monorepo and not compatible_version(metadata['version'], metadata['version']):
                raise ValueError(f'{name}: reviewed artifact requires a semantic version')
            if requested and not monorepo and not compatible_version(metadata['version'], requested):
                raise ValueError(f'{name}: artifact version does not satisfy {requested}')
        for section in ('dependencies', 'optionalDependencies'):
            if any(dep.startswith('@shizuha/') for dep in metadata.get(section, {})):
                raise ValueError(f'{name}: transitive private packages require a reviewed bundling strategy')
    dest = frontend / 'vendor'
    dest.mkdir(exist_ok=True)
    manifest = {}
    resolve_graph = False
    for name in sorted(required):
        artifact, metadata = available[name]
        filename = name.split('/')[1] + '.tgz'
        shutil.copyfile(artifact, dest / filename)
        for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
            if name in package.get(section, {}):
                package[section][name] = 'file:vendor/' + filename
        manifest[name] = {'version': metadata['version'], 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
        key = 'node_modules/' + name
        existing = lock.get('packages', {}).get(key, {})
        graph_fields = ('version', 'dependencies', 'optionalDependencies',
                        'peerDependencies', 'peerDependenciesMeta', 'engines', 'bin')
        if existing and all(existing.get(field) == metadata.get(field) for field in graph_fields):
            # License-only repacks do not change the dependency graph. Preserve
            # its verified nested peer layout, updating only the artifact pin.
            existing['resolved'] = 'file:vendor/' + filename
            existing['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(artifact.read_bytes()).digest()).decode()
        else:
            resolve_graph = True
            for old_key in list(lock.get('packages', {})):
                if old_key == key or old_key.startswith(key + '/node_modules/'):
                    del lock['packages'][old_key]
        for section in ('dependencies', 'devDependencies', 'optionalDependencies'):
            root_section = lock.get('packages', {}).get('', {}).get(section, {})
            if name in root_section:
                root_section[name] = 'file:vendor/' + filename
    for entry in lock.get('packages', {}).values():
        resolved = entry.get('resolved', '')
        parsed = urlsplit(resolved)
        if parsed.netloc and parsed.hostname != 'registry.npmjs.org':
            if parsed.hostname != 'npm-cache.registry.svc.cluster.local':
                raise ValueError('unreviewed dependency host: ' + str(parsed.hostname))
            if '/@shizuha/' in parsed.path or parsed.path.startswith('/@shizuha%2f'):
                raise ValueError('private transitive dependency remains in lockfile')
            entry['resolved'] = urlunsplit(('https', 'registry.npmjs.org', parsed.path, '', ''))
    package_file.write_text(json.dumps(package, indent=2) + '\n')
    lock_file.write_text(json.dumps(lock, indent=2) + '\n')
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    # Existing node_modules metadata must not influence the publish lock. In
    # particular, replacing a local tarball a second time can otherwise retain
    # incomplete nested peer dependency entries that fail a clean npm ci.
    with tempfile.TemporaryDirectory(prefix='public-npm-lock-') as scratch:
        clean = Path(scratch)
        shutil.copyfile(package_file, clean / 'package.json')
        shutil.copyfile(lock_file, clean / 'package-lock.json')
        shutil.copytree(dest, clean / 'vendor')
        if resolve_graph:
            subprocess.run(['npm', 'install', '--package-lock-only', '--ignore-scripts', '--no-audit', '--no-fund',
                            '--registry=https://registry.npmjs.org/'], cwd=clean, check=True)
        final = json.loads((clean / 'package-lock.json').read_text())
        generated_lock = (clean / 'package-lock.json').read_bytes()
    for entry in final.get('packages', {}).values():
        resolved = entry.get('resolved', '')
        if resolved and not (resolved.startswith('https://registry.npmjs.org/') or resolved.startswith('file:vendor/')):
            raise ValueError('generated lockfile contains a non-public dependency URL')
    lock_file.write_bytes(generated_lock)
    print('Vendored ' + ', '.join(sorted(required)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    args = parser.parse_args()
    vendor(args.frontend, args.artifacts)
