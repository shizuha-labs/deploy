#!/usr/bin/env python3
"""Idempotently provision the installation's Stalwart domain and ID directory."""
import base64
import json
import os
import time
from urllib.request import Request, urlopen


def call(method, arguments):
    auth = base64.b64encode(('admin:' + os.environ['STALWART_ADMIN_PASSWORD']).encode()).decode()
    request = Request(os.environ.get('STALWART_ADMIN_URL', 'http://mail-server:8080') + '/jmap',
                      data=json.dumps({'using': ['urn:ietf:params:jmap:core', 'urn:stalwart:jmap'],
                                       'methodCalls': [[method, arguments, 'install']]}).encode(),
                      headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)['methodResponses'][0]
    if result[0] != method or any(result[1].get(key) for key in ('notCreated', 'notUpdated', 'notDestroyed')):
        raise RuntimeError('Stalwart configuration failed: ' + json.dumps(result))
    return result[1]


def upsert(kind, natural_key, value):
    objects = call('x:' + kind + '/get', {}).get('list', [])
    ids = [obj['id'] for obj in objects if obj.get(natural_key) == value[natural_key]]
    if len(ids) > 1:
        raise RuntimeError('Ambiguous Stalwart object: ' + kind)
    if ids:
        current = call('x:' + kind + '/get', {'ids': ids})['list'][0]
        update = {key: val for key, val in value.items() if key != natural_key and current.get(key) != val}
        if update:
            call('x:' + kind + '/set', {'update': {ids[0]: update}})
        return ids[0]
    created = call('x:' + kind + '/set', {'create': {'installation': value}})
    return created['created']['installation']['id']


def main():
    for attempt in range(60):
        try:
            call('x:Domain/query', {})
            break
        except Exception:
            if attempt == 59:
                raise
            time.sleep(2)
    domain = upsert('Domain', 'name', {'name': os.environ['MAIL_DOMAIN'], 'description': 'Self-hosted platform mail', 'isEnabled': True})
    call('x:SystemSettings/set', {'update': {'singleton': {'defaultHostname': 'mail.' + os.environ['MAIL_DOMAIN'], 'defaultDomainId': domain}}})
    directory = upsert('Directory', 'description', {
        '@type': 'Oidc', 'description': 'Platform identity',
        'issuerUrl': os.environ['SHIZUHA_OAUTH_ISSUER'],
        'requireAudience': os.environ['SHIZUHA_MAIL_OAUTH_CLIENT_ID'],
        'requireScopes': {'openid': True, 'email': True},
        'claimUsername': 'email', 'claimName': 'name', 'claimGroups': 'groups',
    })
    call('x:Authentication/set', {'update': {'singleton': {'directoryId': directory}}})
    print('Mail domain and ID directory provisioned; restart mail-server to apply initial configuration')


if __name__ == '__main__':
    main()
