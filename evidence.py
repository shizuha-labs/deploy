#!/usr/bin/env python3
"""Emit bounded, credential-redacted private CI evidence before Job cleanup."""
import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
import sys

root, code = Path(sys.argv[1]), int(sys.argv[2])
secrets = []
for line in (root / 'secrets.env').read_text().splitlines():
    if '=' in line:
        value = line.split('=', 1)[1].strip().strip('"\'')
        if len(value) >= 8:
            secrets.append(value)
receipt = {'schema': 1, 'exit_code': code, 'files': {}}
for name in ('pull.log', 'build.log', 'build-cache-cleanup.log', 'storage-budget.log', 'storage-budget.json', 'start.log',
             'mail-restart.log', 'runtime-setup.log', 'runtime-diagnostics.log', 'membership-diagnostics.log', 'health.txt', 'acceptance.log',
             'images.json', 'gates.json', 'delivery-input.json', 'service-status.txt', 'services.log', 'cleanup.log'):
    path = root / name
    if not path.exists():
        continue
    content = path.read_bytes()
    receipt['files'][name] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    body = content.decode(errors='replace')
    for value in secrets:
        body = body.replace(value, '[REDACTED]')
    if name == 'services.log':
        # Compose interleaves every container. A global tail can discard the
        # failing application's evidence behind a noisy healthy sidecar.
        groups = defaultdict(lambda: deque(maxlen=40))
        for line in body.splitlines():
            groups[line.split('|', 1)[0].strip()].append(line)
        print(f'--- {name} (final 40 lines per service) ---')
        for service in sorted(groups):
            print('\n'.join(groups[service]))
    else:
        print(f'--- {name} (final 120 lines) ---')
        print('\n'.join(body.splitlines()[-120:]))
print('INSTALLATION_RECEIPT=' + json.dumps(receipt, sort_keys=True))
(root / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
