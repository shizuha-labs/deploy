"""Prove a real ID logout invalidates its session in every JWT consumer."""
import base64
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PATHS = {
    'home': '/api/home/summary',
    'wiki': '/wiki/api/pages/',
    'atlas': '/atlas/api/maps',
    'mail': '/mail/api/accounts/',
    'admin': '/admin/api/organizations/',
    'notes': '/notes/api/notebooks/',
    'drive': '/drive/api/files/',
    'connect': '/connect/api/conversations/',
    'pulse': '/pulse/api/items/?limit=1',
    'hive': '/hive/api/v1/fleet/agent-cards/?owner_only=true',
}

def wait_for_denial(base, path, token, org, timeout=40):
    deadline = time.monotonic() + timeout
    while True:
        request = Request(base + path, headers={'Authorization': 'Bearer ' + token,
                          'X-Organization-ID': str(org), 'Accept': 'application/json'})
        try:
            with urlopen(request, timeout=10) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
        if status in (401, 403):
            return
        if status != 200:
            raise RuntimeError(f'{path}: HTTP {status} is not proof of session revocation')
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{path}: logged-out session remained authorized past revocation-cache TTL')
        time.sleep(1)

def verify(api, base, username, password, org):
    credentials = {'username': username, 'password': password}
    pair = api('POST', '/id/api/auth/login/', data=credentials)['tokens']
    raw = pair['access'].split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    if claims.get('client_id') or not claims.get('sid'):
        raise RuntimeError('Expected the real first-party session token contract')
    for path in PATHS.values():
        api('GET', path, pair['access'], org=org, expected=(200,))
    api('POST', '/id/api/auth/logout/', pair['access'], {'refresh_token': pair['refresh']}, expected=(200,))
    # Allow the documented 30s active-verdict cache to expire, but require an
    # actual authentication denial; server/route/network failures never count.
    for path in PATHS.values():
        wait_for_denial(base, path, pair['access'], org)
    fresh = api('POST', '/id/api/auth/login/', data=credentials)['tokens']['access']
    for path in PATHS.values():
        api('GET', path, fresh, org=org, expected=(200,))
    print('PASS real no-client-id session login, logout denial, and fresh login across all platform services including Home', flush=True)
