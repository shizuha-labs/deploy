import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_agent
import runtime_setup
import runtime_support
import runtime_keys


class RuntimeContractTest(unittest.TestCase):
    def test_kubectl_create_list_returns_multiple_json_documents(self):
        from subprocess import CompletedProcess
        objects = [{'kind': kind, 'metadata': {'name': 'acceptance-provider'}}
                   for kind in ('Secret', 'ConfigMap', 'Pod', 'Service')]
        wire = '\n'.join(json.dumps(item, indent=2) for item in objects).encode()
        with patch.object(runtime_agent.subprocess, 'run', return_value=CompletedProcess([], 0, wire)):
            result = runtime_agent.kube_json(['docker', 'compose'], ['create', '-f', '-', '-o', 'json'],
                                            {'apiVersion': 'v1', 'kind': 'List', 'items': objects})
        self.assertEqual(result['items'], objects)
        for wire, expected in ((json.dumps(objects[0]).encode(), objects[0]), (b'', None)):
            with patch.object(runtime_agent.subprocess, 'run', return_value=CompletedProcess([], 0, wire)):
                self.assertEqual(runtime_agent.kube_json([], ['get', '-o', 'json']), expected)
        with patch.object(runtime_agent.subprocess, 'run', return_value=CompletedProcess([], 0, b'{} invalid')):
            with self.assertRaises(json.JSONDecodeError):
                runtime_agent.kube_json([], ['get', '-o', 'json'])

    def test_signing_key_is_private_stable_and_matches_runtime_keyring_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_keys.initialize(root)
            original = (root / 'capability-key.pem').read_bytes()
            runtime_keys.initialize(root)
            self.assertEqual((root / 'capability-key.pem').read_bytes(), original)
            self.assertEqual((root / 'capability-key.pem').stat().st_mode & 0o777, 0o600)
            public = json.loads((root / 'keyring.json').read_text())
            self.assertEqual(set(public), {'schema_version', 'keys'})
            self.assertEqual(public['schema_version'], 1)
            self.assertEqual(set(public['keys'][0]), {'current', 'kid', 'public_key'})
            self.assertEqual(len(public['keys'][0]['public_key']), 43)

    def test_agent_credential_is_never_part_of_hive_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime.json').write_text(json.dumps({
                'agent_image': 'public-runtime:tested', 'public_origin': 'http://shizuha.localhost:8080',
            }))
            requests = []
            def api(method, path, token, data):
                requests.append(data)
                return {'id': 'agent-id'}
            with patch.object(runtime_agent, 'kube_json', side_effect=[
                {'metadata': {'name': 'selfhost-runtime', 'labels': {'kubernetes.io/arch': 'amd64'}}}, {},
            ]) as kube:
                result = runtime_agent.create_agent(root, 'isolated', api, 'operator-test-token',
                    'example-agent', 'gpt-example', 'https://provider.example/v1', 'generated-agent-key')
            self.assertEqual(result['id'], 'agent-id')
            self.assertNotIn('generated-agent-key', json.dumps(requests))
            self.assertEqual(kube.call_args.args[1][:2], ['create', '-f'])
            self.assertIn('CORTEX_API_KEY', kube.call_args.args[2]['data'])
            self.assertEqual(requests[0]['image_pull_policy'], 'Never')
            self.assertEqual(requests[0]['node_name'], 'selfhost-runtime')
            self.assertEqual(requests[0]['storage_class'], 'local-path')
            self.assertEqual(requests[0]['resource_overrides']['limits'], {'cpu': '2', 'memory': '2Gi'})
            self.assertFalse(requests[0]['hostplane_ssh'])
            self.assertEqual(requests[0]['model'], 'cortex/gpt-example')
            self.assertEqual(requests[0]['env']['OPENAI_BASE_URL'], 'https://provider.example/v1')

    def test_malformed_provider_url_cannot_create_secret(self):
        with patch.object(runtime_agent, 'kube_json') as kube:
            for url in ('file:///tmp/key', 'https://secret@provider.example', 'https://provider.example?key=value'):
                with self.assertRaises(ValueError):
                    runtime_agent.create_agent(Path('/unused'), 'isolated', None, '', 'example-agent', 'model', url, 'key')
            kube.assert_not_called()

    def test_runtime_control_plane_is_not_host_mounted_or_exposed(self):
        services = {'id': {'environment': {}}, 'hive': {
            'build': {'context': './sources'}, 'environment': {}, 'command': ['daphne', 'hive_project.asgi:application'],
        }}
        volumes = {}
        config = runtime_support.extend(services, volumes, {'k3s': runtime_support.K3S_IMAGE},
                                         'http://shizuha.localhost:8080', runtime_support.RUNTIME_SOURCE)
        # Fleet v1 consumes SHIZUHA_JWKS_URL while shared middleware consumes
        # SHIZUHA_OAUTH_JWKS_URL; the hosted fallback path is not the installer.
        for setting in ('SHIZUHA_JWKS_URL', 'SHIZUHA_OAUTH_JWKS_URL'):
            self.assertEqual(services['hive']['environment'][setting],
                             'http://id:8001/shizuha-id/.well-known/jwks.json')
        self.assertEqual(services['hive']['environment']['SHIZUHA_CONNECT_INTERNAL_URL'], 'http://connect:8019')
        cluster = services['runtime-cluster']
        self.assertNotIn('ports', cluster)
        self.assertNotIn('/var/run/docker.sock', json.dumps(services))
        self.assertEqual(cluster['volumes'], ['runtime-cluster:/var/lib/rancher/k3s'])
        self.assertEqual(config['public_hostname'], 'shizuha.localhost')
        self.assertIn('--snapshotter=native', cluster['command'])
        self.assertIn('runtime-build', services['runtime-broker-image']['profiles'])
        self.assertIn('runtime-build', services['runtime-image']['profiles'])

    def test_composed_services_have_their_real_ports(self):
        self.assertEqual(runtime_setup.PORTS['drive'], 8018)
        self.assertEqual(runtime_setup.PORTS['atlas'], 8080)
        service, endpoint = runtime_setup.endpoint_objects('shizuha', 'shizuha-drive', '172.20.0.2', 8018)
        self.assertEqual(service['spec']['ports'][0]['port'], endpoint['ports'][0]['port'])
        with self.assertRaises(ValueError):
            runtime_setup.endpoint_objects('shizuha', 'shizuha-drive', 'operator.private.example', 8018)


class RuntimeDNSActivationTest(unittest.TestCase):
    """Exercise the real setup entrypoint across existing subprocess boundaries."""
    def invoke(self, answers, rollout_elapsed=0, query_timeout=False):
        import contextlib
        import io
        import subprocess
        from types import SimpleNamespace
        commands = []
        applied = []
        clock = [0.0]
        responses = iter(answers)
        last = [answers[-1]]
        class Export:
            def __enter__(self):
                self.stdout = io.BytesIO(b'synthetic image')
                return self
            def __exit__(self, *args):
                pass
            def wait(self, timeout=None):
                return 0
        def run(command, **kwargs):
            commands.append(command)
            out = b''
            rc = 0
            if command[-2:] == ['-f', '-']:
                applied.extend(json.loads(kwargs['input'])['items'])
            if command[-1] == '/etc/rancher/k3s/k3s.yaml':
                out = b'server: https://127.0.0.1:6443\n'
            if command[-3:-1] == ['python', '-c']:
                out = json.dumps({n: '172.22.0.' + str(i + 2)
                    for i, n in enumerate(list(runtime_setup.PORTS) + ['gateway'])}).encode()
            if 'get' in command and 'service/kube-dns' in command:
                out = json.dumps({'metadata': {'name': 'kube-dns', 'namespace': 'kube-system'},
                                  'spec': {'clusterIP': '10.43.0.10'}}).encode()
            if 'rollout' in command:
                clock[0] += rollout_elapsed
            if 'nslookup' in command:
                self.assertLessEqual(kwargs['timeout'], 180 - clock[0])
                if query_timeout:
                    clock[0] += kwargs['timeout']
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                self.assertEqual(command[-4:], ['nslookup', '-type=A', 'synthetic.invalid', '10.43.0.10'])
                self.assertTrue(any(x['kind'] == 'ConfigMap' and
                    x['metadata']['name'] == 'coredns-custom' for x in applied))
                last[0] = next(responses, last[0])
                rc, out = last[0]
            return subprocess.CompletedProcess(command, rc, out, b'synthetic diagnostic')
        def sleep(seconds):
            clock[0] += seconds
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime').mkdir()
            (root / 'runtime/keyring.json').write_text('{}')
            (root / 'runtime.json').write_text(json.dumps({'agent_image': 'synthetic-agent',
                'broker_image': 'synthetic-broker', 'public_hostname': 'synthetic.invalid'}))
            with patch('sys.argv', ['runtime_setup.py', str(root), '--project', 'synthetic']), \
                 patch.object(runtime_setup.subprocess, 'run', side_effect=run), \
                 patch.object(runtime_setup.subprocess, 'Popen', side_effect=lambda *a, **kw: Export()), \
                 patch.object(runtime_setup, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep), create=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                runtime_setup.main()
        return commands

    @staticmethod
    def answer(address, host='synthetic.invalid'):
        return ('Server: 10.43.0.10\nAddress: 10.43.0.10:53\n\n'
                'Name: ' + host + '\nAddress: ' + address + '\n').encode()

    def test_main_waits_for_installed_zone_after_ready_deployment(self):
        commands = self.invoke([(1, b'NXDOMAIN'), (0, self.answer('172.22.0.12'))])
        self.assertEqual(sum('nslookup' in c for c in commands), 2)

    def test_main_refuses_unactivated_zone_with_existing_ready_deployment(self):
        with self.assertRaisesRegex(RuntimeError, 'DNS.*expected gateway'):
            self.invoke([(1, b'NXDOMAIN')])

    def test_main_exhausted_rollout_budget_cannot_start_fresh_dns_budget(self):
        with self.assertRaisesRegex(RuntimeError, 'DNS.*expected gateway'):
            self.invoke([(0, self.answer('172.22.0.12'))], rollout_elapsed=180)

    def test_main_dns_timeout_stays_in_existing_total_budget(self):
        with self.assertRaisesRegex(RuntimeError, 'DNS.*expected gateway'):
            self.invoke([(0, self.answer('172.22.0.12'))], rollout_elapsed=175, query_timeout=True)

    def test_main_refuses_wrong_a_and_dns_server_address_false_positive(self):
        for answer in (self.answer('192.0.2.99'),
                       b'Server: 172.22.0.12\nAddress: 172.22.0.12:53\n',
                       self.answer('172.22.0.12', 'other.invalid'),
                       self.answer('172.22.0.12') + b'Address: 192.0.2.99\n'):
            with self.subTest(answer=answer), self.assertRaisesRegex(RuntimeError, 'DNS.*expected gateway'):
                self.invoke([(0, answer)])


if __name__ == '__main__':
    unittest.main()
