#!/usr/bin/env python3
"""Smoke a running clean installation; exit nonzero on missing UI or API."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import urlopen


def check(base):
    results = []
    for name in ('id', 'hive', 'pulse', 'connect', 'wiki', 'atlas', 'admin', 'notes', 'drive', 'mail'):
        for kind, suffix in [('UI', f'/{name}/'), ('API', '/atlas/health' if name == 'atlas' else f'/{name}/api/health/')]:
            with urlopen(base.rstrip('/') + suffix, timeout=20) as response:
                body = response.read()
                if response.status != 200:
                    raise RuntimeError(f'{name} {kind}: HTTP {response.status}')
                if kind == 'UI' and b'<html' not in body.lower():
                    raise RuntimeError(f'{name}: missing HTML UI')
                if kind == 'API':
                    json.loads(body)
            results.append(f'{name} {kind}: passed')
    for kind, suffix in [('UI', '/'), ('API', '/api/home/health')]:
        with urlopen(base.rstrip('/') + suffix, timeout=20) as response:
            body = response.read()
            if response.status != 200 or (kind == 'UI' and b'<html' not in body.lower()):
                raise RuntimeError('Home ' + kind + ' is unavailable')
            if kind == 'API':
                json.loads(body)
        results.append('home ' + kind + ': passed')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:8080')
    args = parser.parse_args()
    print('\n'.join(check(args.url)))
