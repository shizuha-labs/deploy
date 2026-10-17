#!/usr/bin/env python3
"""Register a trusted operator agent in Hive using its own provider credential.

The credential enters a Kubernetes Secret through stdin, never Hive's env JSON,
argv, a ConfigMap, or a source file. This manages only the dedicated cluster.
"""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def compose_command(root, project):
    return ['docker', 'compose', '--project-name', project, '--env-file',
            str(root / 'secrets.env'), '-f', str(root / 'compose.json')]


def kube_json(compose, arguments, data=None):
    result = subprocess.run(compose + ['exec', '-T', 'runtime-cluster', 'kubectl'] + arguments,
                            input=None if data is None else json.dumps(data).encode(),
                            capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Dedicated runtime Kubernetes operation failed: ' + ' '.join(arguments[:3]))
    # kubectl prints one JSON document per created resource, including when
    # stdin is a Kubernetes List. Preserve single-object get/create results.
    remaining = result.stdout.decode().strip()
    documents = []
    decoder = json.JSONDecoder()
    while remaining:
        document, end = decoder.raw_decode(remaining)
        documents.append(document)
        remaining = remaining[end:].lstrip()
    if len(documents) > 1:
        return {'apiVersion': 'v1', 'kind': 'List', 'items': documents}
    return documents[0] if documents else None


def create_agent(root, project, api, token, username, model, base_url, provider_key):
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,39}', username):
        raise ValueError('Agent username must contain 3–40 lowercase letters, digits or hyphens')
    parsed = urlsplit(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider URL must be HTTP(S), without credentials, query or fragment')
    if not model.strip() or not provider_key.strip():
        raise ValueError('An explicit model and this agent’s provider credential are required')
    # Select the explicitly configured compatible endpoint even when the
    # vendor model name would otherwise choose a hosted provider by prefix.
    model = model.strip()
    if not model.startswith('cortex/'):
        model = 'cortex/' + model
    configuration = json.loads((root / 'runtime.json').read_text())
    compose = compose_command(root, project)
    node = kube_json(compose, ['get', 'node/selfhost-runtime', '-o', 'json'])
    arch = node['metadata']['labels']['kubernetes.io/arch']
    # No overwrite path: an existing username/Secret is an explicit conflict.
    kube_json(compose, ['create', '-f', '-', '-o', 'json'], {
        'apiVersion': 'v1', 'kind': 'Secret',
        'metadata': {'name': username + '-agent-creds', 'namespace': 'shizuha-fleet',
                     'labels': {'app.kubernetes.io/managed-by': 'shizuha-selfhost'}},
        'type': 'Opaque',
        'data': {'CORTEX_API_KEY': base64.b64encode(provider_key.encode()).decode()},
    })
    # This is Hive's documented custom-image path. It does not impersonate an
    # internally signed DesiredRuntimeRelease or alter native release gates.
    result = api('POST', '/hive/api/v1/fleet/agents/', token, {
        'agent_username': username, 'display_name': username,
        'image': configuration['agent_image'], 'image_pull_policy': 'Never',
        'model': model, 'arch': arch, 'requires_longhorn': False,
        # This installation has one dedicated node and local-path workspace
        # storage. Declare that locality through Hive's supported node pin;
        # otherwise the shared-fleet control-plane exclusion matches no node.
        'node_name': node['metadata']['name'],
        'storage_class': 'local-path', 'pvc_size': '1Gi',
        'sidecar_set': ['broker'], 'hostplane_ssh': False,
        'mcp_config': {'execution_method': 'shizuha'},
        'env': {'CORTEX_BASE_URL': base_url.rstrip('/'),
                'OPENAI_BASE_URL': base_url.rstrip('/'),
                'SHIZUHA_K8S_PRIMARY_METHOD': 'shizuha',
                'NPM_CONFIG_REGISTRY': 'https://registry.npmjs.org',
                'PIP_INDEX_URL': 'https://pypi.org/simple'},
        'resource_overrides': {'requests': {'cpu': '100m', 'memory': '256Mi'},
                               'limits': {'cpu': '2', 'memory': '2Gi'}},
        'platform_url': configuration['public_origin'],
    })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installation', type=Path)
    parser.add_argument('--project', default='shizuha')
    parser.add_argument('--username', required=True, help='new agent username')
    parser.add_argument('--operator', required=True, help='local ID staff username')
    parser.add_argument('--model', required=True, help='provider model, e.g. cortex/my-model')
    parser.add_argument('--provider-url', required=True, help='OpenAI-compatible API base URL reachable from agent pods')
    parser.add_argument('--provider-key-file', type=Path, required=True)
    args = parser.parse_args()
    root = args.installation.resolve()
    mode = args.provider_key_file.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise ValueError('Provider key must be in a regular file readable only by its owner (chmod 600)')
    origin = json.loads((root / 'runtime.json').read_text())['public_origin']

    def api(method, path, token=None, data=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(origin + path, method=method, headers=headers,
                          data=None if data is None else json.dumps(data).encode())
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f'{method} {path} returned HTTP {exc.code}; inspect Hive events locally') from None

    token = api('POST', '/id/api/auth/login/', data={
        'username': args.operator, 'password': getpass.getpass('Local ID staff password: '),
    })['tokens']['access']
    result = create_agent(root, args.project, api, token, args.username, args.model,
                          args.provider_url, args.provider_key_file.read_text().strip())
    print(json.dumps({'id': result['id'], 'username': args.username,
                      'status': result.get('status'), 'message': 'Inspect the agent in Hive for readiness and activity'}))


if __name__ == '__main__':
    main()
