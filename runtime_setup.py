#!/usr/bin/env python3
"""Initialize only the installation's dedicated runtime cluster after Compose up.

No host kubeconfig, Docker socket mount, registry login, or Shizuha account is
used. Built images are transferred directly into the dedicated containerd.
"""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import subprocess

PORTS = {'id': 8001, 'admin': 8003, 'hive': 8030, 'pulse': 8002,
         'connect': 8019, 'wiki': 8013, 'atlas': 8080, 'notes': 8012,
         'drive': 8018, 'mail': 8014}


def endpoint_objects(namespace, name, address, port):
    ipaddress.ip_address(address)
    return [
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': namespace},
         'spec': {'ports': [{'name': 'http', 'port': port, 'targetPort': port}]}},
        {'apiVersion': 'discovery.k8s.io/v1', 'kind': 'EndpointSlice',
         'metadata': {'name': name + '-compose', 'namespace': namespace,
                      'labels': {'kubernetes.io/service-name': name, 'endpointslice.kubernetes.io/managed-by': 'shizuha-selfhost'}},
         'addressType': 'IPv4', 'ports': [{'name': 'http', 'port': port, 'protocol': 'TCP'}],
         'endpoints': [{'addresses': [address], 'conditions': {'ready': True}}]},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installation', type=Path)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    root = args.installation.resolve()
    configuration = json.loads((root / 'runtime.json').read_text())
    compose = ['docker', 'compose', '--project-name', args.project, '--env-file', str(root / 'secrets.env'), '-f', str(root / 'compose.json')]

    def run(command, data=None, timeout=180, readiness=False):
        proc = subprocess.run(command, input=data, capture_output=True, timeout=timeout)
        if proc.returncode:
            if readiness:
                # Only this fixed read-only node query may expose stderr. Never
                # echo commands carrying kubeconfig or Secret material.
                detail = proc.stderr.decode(errors='replace')[-2000:]
                for line in (root / 'secrets.env').read_text().splitlines():
                    value = line.partition('=')[2]
                    if len(value) >= 8:
                        detail = detail.replace(value, '[redacted]')
                raise RuntimeError('Dedicated runtime node is not Ready: ' + detail
                    + '\nInspect runtime-cluster containerd logs for the failed startup component.')
            # kubeconfig/Secret payloads may be reflected in error text.
            raise RuntimeError('Runtime initialization command failed: ' + ' '.join(command[:4]))
        return proc.stdout

    kube = compose + ['exec', '-T', 'runtime-cluster', 'kubectl']
    run(kube + ['wait', '--for=condition=Ready', 'node/selfhost-runtime', '--timeout=180s'],
        timeout=195, readiness=True)
    for image in (configuration['agent_image'], configuration['broker_image']):
        run(['docker', 'image', 'inspect', image])
        with subprocess.Popen(['docker', 'save', image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as export:
            result = subprocess.run(compose + ['exec', '-T', 'runtime-cluster', 'ctr', 'images', 'import', '-'],
                                    stdin=export.stdout, capture_output=True, timeout=600)
            export.stdout.close()
            if export.wait(timeout=30) or result.returncode:
                raise RuntimeError('Failed to transfer reviewed runtime image into the dedicated cluster')
    raw = run(compose + ['exec', '-T', 'runtime-cluster', 'cat', '/etc/rancher/k3s/k3s.yaml']).decode()
    if raw.count('server: https://127.0.0.1:6443') != 1:
        raise RuntimeError('Unexpected dedicated-cluster kubeconfig endpoint')
    raw = raw.replace('server: https://127.0.0.1:6443', 'server: https://runtime-cluster:6443')
    target = root / 'runtime/kubeconfig'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(raw)
    # Resolve inside this Compose network, never from the operator's private DNS.
    names = list(PORTS) + ['gateway']
    resolve = 'import socket,json;print(json.dumps({n:socket.gethostbyname(n) for n in ' + repr(names) + '}))'
    addresses = json.loads(run(compose + ['exec', '-T', 'hive', 'python', '-c', resolve]))
    namespaces = ['shizuha', 'shizuha-hive', 'shizuha-fleet']
    run(kube + ['apply', '-f', '-'], json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name}} for name in namespaces]}).encode())
    objects = []
    for name, port in PORTS.items():
        objects += endpoint_objects('shizuha', 'shizuha-' + name, addresses[name], port)
    objects += endpoint_objects('shizuha-hive', 'hive-direct', addresses['hive'], PORTS['hive'])
    objects += endpoint_objects('shizuha-hive', 'shizuha-hive', addresses['hive'], PORTS['hive'])
    public_host = configuration['public_hostname']
    if not public_host or any(c.isspace() or c in '{};' for c in public_host):
        raise ValueError('Unsafe public hostname for runtime DNS')
    gateway = str(ipaddress.ip_address(addresses['gateway']))
    objects += [
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'coredns-custom', 'namespace': 'kube-system'},
         'data': {'platform.server': f'{public_host}:53 {{\n hosts {{\n {gateway} {public_host}\n }}\n cache 30\n}}\n'}},
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'hive-capability-evidence-signer', 'namespace': 'shizuha-hive'},
         'type': 'Opaque', 'data': {'keyring.json': base64.b64encode((root / 'runtime/keyring.json').read_bytes()).decode()}},
    ]
    run(kube + ['apply', '-f', '-'], json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': objects}).encode())
    run(kube + ['label', 'node/selfhost-runtime', 'shizuha.io/platform=true', '--overwrite'])
    run(kube + ['rollout', 'status', 'deployment/coredns', '-n', 'kube-system', '--timeout=180s'])
    print('Dedicated Hive runtime initialized from the locked public source; agent-launch acceptance still required')


if __name__ == '__main__':
    main()
