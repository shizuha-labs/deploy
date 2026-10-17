"""Bounded review artifact transport over existing Kubernetes pod logs."""
import base64
import hashlib
from pathlib import Path
import re
import sys

LIMIT = 6 * 1024 * 1024
PREFIX = 'SHIZUHA_REVIEW_'


def emit(path, run_id, stream=sys.stdout):
    if not re.fullmatch(r'[0-9]+', run_id):
        raise ValueError('invalid review run ID')
    content = Path(path).read_bytes()
    if not content or len(content) > LIMIT:
        raise ValueError('review archive must contain 1 byte to 6 MiB')
    digest = hashlib.sha256(content).hexdigest()
    print(f'{PREFIX}BEGIN {run_id} {len(content)} {digest}', file=stream)
    encoded = base64.b64encode(content).decode('ascii')
    for offset in range(0, len(encoded), 4096):
        print(PREFIX + 'DATA ' + encoded[offset:offset + 4096], file=stream)
    print(PREFIX + 'END ' + run_id, file=stream, flush=True)


def receive(log, run_id, destination):
    state, chunks, ordinary, encoded_size = 'before', [], [], 0
    size = digest = None
    for line in log.splitlines():
        if not line.startswith(PREFIX):
            ordinary.append(line)
            continue
        if line.startswith(PREFIX + 'BEGIN '):
            if state != 'before':
                raise ValueError('duplicate review artifact frame')
            fields = line.split()
            if len(fields) != 4 or fields[1] != run_id or not fields[2].isdigit() or not re.fullmatch(r'[a-f0-9]{64}', fields[3]):
                raise ValueError('invalid review artifact header')
            size, digest = int(fields[2]), fields[3]
            if not 0 < size <= LIMIT:
                raise ValueError('review archive exceeds 6 MiB bound')
            state = 'data'
        elif line.startswith(PREFIX + 'DATA '):
            if state != 'data':
                raise ValueError('review data outside artifact frame')
            chunk = line[len(PREFIX + 'DATA '):]
            encoded_size += len(chunk)
            if encoded_size > 4 * ((LIMIT + 2) // 3):
                raise ValueError('encoded review archive exceeds 8 MiB bound')
            chunks.append(chunk)
        elif line == PREFIX + 'END ' + run_id and state == 'data':
            state = 'after'
        else:
            raise ValueError('invalid review artifact frame')
    if state != 'after':
        raise ValueError('review artifact is missing or truncated')
    content = base64.b64decode(''.join(chunks), validate=True)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('review archive size or SHA256 mismatch')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite an earlier receipt from this CI run.
    with destination.open('xb') as output:
        destination.chmod(0o600)
        output.write(content)
    return '\n'.join(ordinary) + '\n'


if __name__ == '__main__':
    emit(sys.argv[1], sys.argv[2])
