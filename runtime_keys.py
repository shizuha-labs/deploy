"""Create the installation's capability signing key once, never rotate on boot."""
import base64
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def initialize(root):
    key_path = root / 'capability-key.pem'
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError('Existing runtime capability key has an unexpected type')
    else:
        key = Ed25519PrivateKey.generate()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keyring = {'schema_version': 1, 'keys': [{'kid': 'selfhost-v1', 'current': True,
        'public_key': base64.urlsafe_b64encode(public).decode().rstrip('=')}]}
    fd = os.open(root / 'keyring.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(keyring) + '\n')


if __name__ == '__main__':
    initialize(Path('/runtime'))
    print('Installation capability key initialized; private bytes retained only in runtime volume')
