"""Compact provider-log attestation from the already verified review artifact.

Contains public source identities and verdicts only, never application tokens,
logs, screenshots, or installation secrets. This is evidence, not gate policy.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

PREFIX = 'SHIZUHA_DELIVERY_PROOF='
LIMIT = 6 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def validate_public_inputs(inputs):
    lock = inputs['source_lock']
    if set(lock) != {'schema', 'sources', 'images'} or lock['schema'] != 1:
        raise ValueError('Unexpected source lock fields')
    for name, source in lock['sources'].items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]*', name)
                or set(source) != {'url', 'commit'}
                or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', source['url'])
                or not re.fullmatch(r'[a-f0-9]{40}', source['commit'])):
            raise ValueError('Invalid public source identity')
    for name, image in lock['images'].items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]*', name)
                or not re.fullmatch(r'(?:docker\.io|quay\.io|ghcr\.io)/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image)):
            raise ValueError('Invalid public image identity')
    installer = inputs['installer']
    if inputs['mode'] == 'private':
        if installer is not None:
            raise ValueError('Private proof must not claim a public installer')
    elif (not isinstance(installer, dict)
            or set(installer) != {'schema', 'url', 'commit', 'tree'} or installer['schema'] != 1
            or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', installer['url'])
            or not all(re.fullmatch(r'[a-f0-9]{40}', installer[key]) for key in ('commit', 'tree'))):
        raise ValueError('Invalid public installer identity')


def capture_inputs(lock_path, output, private):
    lock_path, output = Path(lock_path), Path(output)
    config = lock_path.parent / 'public-installer.json'
    inputs = {
        'mode': 'private' if private else 'public',
        'source_lock': json.loads(lock_path.read_text()),
        'installer': None if private else json.loads(config.read_text()),
    }
    validate_public_inputs(inputs)
    # The published installer identity and lock are verified by selfhost_bundle
    # before invoking ci-validate; retaining the actual bytes binds that proof.
    with (output / 'delivery-input.json').open('x') as stream:
        json.dump(inputs, stream, sort_keys=True)


def from_archive(path, run_id, candidate_version, candidate_sha256):
    if not re.fullmatch(r'[1-9][0-9]*', str(run_id)):
        raise ValueError('Invalid provider run identity')
    if not re.fullmatch(r'[a-f0-9]{64}', candidate_sha256):
        raise ValueError('Invalid candidate digest')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', candidate_version):
        raise ValueError('Invalid candidate version')
    content = Path(path).read_bytes()
    if not 0 < len(content) <= LIMIT:
        raise ValueError('Review archive exceeds bound')
    wanted = {'receipt.json', 'gates.json', 'delivery-input.json'}
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
        for member in archive:
            if member.name not in wanted:
                continue
            if member.name in selected or not member.isfile() or not 0 < member.size <= 256 * 1024:
                raise ValueError('Unsafe or duplicate delivery artifact')
            selected[member.name] = archive.extractfile(member).read()
    if set(selected) != wanted:
        raise ValueError('Incomplete delivery evidence')
    receipt = json.loads(selected['receipt.json'])
    for name in wanted - {'receipt.json'}:
        expected = receipt['files'][name]
        if expected != {'bytes': len(selected[name]), 'sha256': hashlib.sha256(selected[name]).hexdigest()}:
            raise ValueError('Delivery artifact does not match receipt')
    inputs = json.loads(selected['delivery-input.json'])
    if set(inputs) != {'mode', 'source_lock', 'installer'} or inputs['mode'] not in {'public', 'private'}:
        raise ValueError('Invalid installation input contract')
    validate_public_inputs(inputs)
    gates = json.loads(selected['gates.json'])
    if not isinstance(gates, dict) or any(
        not re.fullmatch(r'[a-z][a-z0-9-]*', name)
        or not isinstance(verdict, dict)
        or verdict.get('status') not in {'passed', 'failed', 'blocked'}
        for name, verdict in gates.items()
    ):
        raise ValueError('Invalid gate verdicts')
    # Only the status is needed for attestation; arbitrary error strings and
    # diagnostics must never be copied into durable provenance.
    gates = {name: {'status': verdict['status']} for name, verdict in gates.items()}
    # Select every public field explicitly; never copy an arbitrary receipt
    # payload into durable provider logs.
    return {
        'schema': 1, 'run_id': int(run_id), 'candidate_version': candidate_version,
        'candidate_sha256': candidate_sha256, 'mode': inputs['mode'],
        'exit_code': receipt['exit_code'], 'installer': inputs['installer'],
        'source_lock': inputs['source_lock'],
        'source_lock_sha256': hashlib.sha256(canonical(inputs['source_lock'])).hexdigest(),
        'archive_sha256': hashlib.sha256(content).hexdigest(),
        'gates': gates,
    }
