#!/usr/bin/env python3
"""Measure isolated Docker storage before importing a second copy into K3s."""
import json
import os
from pathlib import Path
import subprocess
import sys

GIB = 1024 ** 3

def budget(total, available, image_sizes):
    if total <= 0 or available < 0 or available > total or not image_sizes or any(size <= 0 for size in image_sizes):
        raise ValueError('Invalid Docker filesystem or runtime image measurements')
    # Native containerd stores imported content and unpacked snapshots. Retain
    # room for both, two runtime writable layers, and normal kubelet image-GC
    # headroom. This is a preflight; it never changes Kubernetes thresholds.
    copies = 2 * sum(image_sizes)
    writable = 2 * GIB
    reserve = (total * 15 + 99) // 100
    required = copies + writable + reserve
    return {'total_bytes': total, 'available_bytes': available,
            'runtime_image_bytes': sum(image_sizes), 'import_copy_budget_bytes': copies,
            'writable_budget_bytes': writable, 'filesystem_reserve_bytes': reserve,
            'required_available_bytes': required, 'passed': available >= required}

def measure(root):
    config = json.loads((root / 'compose.json').read_text())
    runtime = json.loads((root / 'runtime.json').read_text())
    images = [runtime['agent_image'], runtime['broker_image']]
    sizes = [int(subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Size}}', image], text=True)) for image in images]
    # df observes the daemon's actual backing filesystem via a disposable
    # container, even when the Docker daemon is remote from this CI process.
    result = subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'sh',
        config['services']['postgres']['image'], '-c', 'LC_ALL=C df -Pk /'], text=True)
    columns = result.strip().splitlines()[-1].split()
    receipt = budget(int(columns[1]) * 1024, int(columns[3]) * 1024, sizes)
    receipt['images'] = dict(zip(images, sizes))
    (root / 'storage-budget.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, sort_keys=True), flush=True)
    if not receipt['passed']:
        raise RuntimeError('Insufficient Docker backing storage for runtime import and writable layers; increase isolated storage capacity')
    return receipt

if __name__ == '__main__':
    if os.environ.get('SHIZUHA_SELFHOST_CI') != '1':
        raise SystemExit('Storage preflight requires the isolated acceptance Docker daemon')
    measure(Path(sys.argv[1]))
