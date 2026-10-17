#!/usr/bin/env python3
"""Package explicit review artifacts; never include credentials or browser traces."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile


def collect(root):
    root = Path(root).resolve()
    paths = [root / name for name in ('receipt.json', 'delivery-input.json', 'health.txt', 'images.json', 'gates.json', 'storage-budget.json')]
    browser = root / 'browser-evidence'
    if browser.is_dir() and not browser.is_symlink():
        paths += sorted(browser.glob('*.png'))
        paths += [browser / 'applications.json']
    selected = []
    total = 0
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        total += path.stat().st_size
        if total > 20 * 1024 * 1024:
            raise ValueError('Review evidence exceeds 20 MiB')
        selected.append(path)
    destination = root / 'review-evidence.tar.gz'
    with tarfile.open(destination, 'w:gz') as archive:
        for path in selected:
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    destination.chmod(0o600)
    manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in selected}
    (root / 'review-evidence-ready').write_text(json.dumps(manifest, sort_keys=True))
    return destination


if __name__ == '__main__':
    collect(sys.argv[1])
