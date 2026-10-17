"""Real Hive provisioning, broker authentication and agent/tool acceptance."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from runtime_agent import create_agent, kube_json


def verify(root, compose, api, staff_token, outsider_token):
    if os.environ.get('SHIZUHA_SELFHOST_CI') != '1':
        raise RuntimeError('Scripted model fixture is restricted to the isolated CI installation')
    project = os.environ['SHIZUHA_COMPOSE_PROJECT']
    config = json.loads((root / 'runtime.json').read_text())
    key, marker = secrets.token_urlsafe(32), secrets.token_hex(24)
    namespace, name = 'shizuha-fleet', 'acceptance-provider'
    labels = {'app': name}
    objects = [
        {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace},
         'data': {'key': base64.b64encode(key.encode()).decode()}},
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': namespace},
         'data': {'server.js': Path(__file__).with_name('runtime_fixture.js').read_text()}},
        {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': namespace, 'labels': labels},
         'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                  'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000},
                  'containers': [{'name': 'provider', 'image': config['agent_image'], 'imagePullPolicy': 'Never',
                      'command': ['node', '/fixture/server.js'],
                      'env': [{'name': 'FIXTURE_KEY', 'valueFrom': {'secretKeyRef': {'name': name, 'key': 'key'}}},
                              {'name': 'FIXTURE_MARKER', 'value': marker}],
                      'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                      'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '500m', 'memory': '128Mi'}},
                      'readinessProbe': {'tcpSocket': {'port': 8099}, 'periodSeconds': 2},
                      'volumeMounts': [{'name': 'fixture', 'mountPath': '/fixture', 'readOnly': True}]}],
                  'volumes': [{'name': 'fixture', 'configMap': {'name': name}}]}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': namespace},
         'spec': {'selector': labels, 'ports': [{'port': 8099, 'targetPort': 8099}]}},
    ]
    kube_json(compose, ['create', '-f', '-', '-o', 'json'], {'apiVersion': 'v1', 'kind': 'List', 'items': objects})
    kube = compose + ['exec', '-T', 'runtime-cluster', 'kubectl', '-n', namespace]
    subprocess.run(kube + ['wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s'], check=True)
    agent = create_agent(root, project, api, staff_token, 'oss-runtime-test', 'cortex/selfhost-acceptance',
                         f'http://{name}.{namespace}.svc.cluster.local:8099', key)
    agent_id = agent['id']
    api('POST', '/hive/api/v1/fleet/agents/', outsider_token, {
        'agent_username': 'forbidden-runtime', 'model': 'cortex/selfhost-acceptance',
    }, expected=(403,))
    subprocess.run(kube + ['rollout', 'status', 'deployment/agent-oss-runtime-test', '--timeout=240s'], check=True)
    pods = kube_json(compose, ['get', 'pods', '-n', namespace, '-l', 'shizuha.io/agent=oss-runtime-test', '-o', 'json'])['items']
    if len(pods) != 1:
        raise RuntimeError('Expected exactly one accepted agent pod')
    pod = pods[0]
    assert pod['spec'].get('automountServiceAccountToken') is False
    assert not any('hostPath' in volume for volume in pod['spec'].get('volumes', []))
    assert {'agent', 'broker'} <= {container['name'] for container in pod['spec']['containers']}
    for container in pod['spec']['containers']:
        assert container['securityContext']['runAsNonRoot'] is True
        assert container['securityContext']['allowPrivilegeEscalation'] is False
    execute = kube + ['exec', '-i', pod['metadata']['name'], '-c', 'agent', '--', 'node', '-']
    # The request goes through the actual gateway SSE and provider SSE paths.
    # Reading the file independently proves the tool ran, not merely a claim.
    script = Path(__file__).with_name('runtime-broker-acceptance.cjs').read_text() + '''
const fs = require('node:fs');
(async () => {
  let listening = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      const health = await fetch('http://127.0.0.1:8080/health', {signal:AbortSignal.timeout(2000)});
      if (health.ok) { listening = true; break; }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (!listening) throw new Error('Running pod did not start the gateway HTTP listener');
  const response = await fetch('http://127.0.0.1:8080/v1/query/stream', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({prompt: 'Write acceptance-proof.txt containing MARKER', permissionMode: 'autonomous'}),
    signal: AbortSignal.timeout(180000),
  });
  if (!response.ok) throw new Error('Gateway query HTTP ' + response.status);
  const events = await response.text();
  if (!events.includes('tool_complete') || !events.includes('MARKER')) throw new Error('Missing successful tool/terminal SSE');
  if (fs.readFileSync('/home/agent/.shizuha/acceptance-proof.txt', 'utf8') !== 'MARKER\\n') throw new Error('Tool disk side effect differs');
  // Broker authenticates this agent through local ID, using the protected UDS.
  await verifyBrokerIdentity(process.env.MCP_AUTH_PROXY_SOCKET, PUBLIC_ORIGIN, 'oss-runtime-test');
  console.log('PASS real gateway SSE, provider SSE, executed write tool and local ID broker authentication');
})().catch(error => { console.error(error.message); process.exitCode=1; });
'''.replace('MARKER', marker).replace('PUBLIC_ORIGIN', json.dumps(config['public_origin']))
    subprocess.run(execute, input=script, text=True, check=True, timeout=270)
    proof_script = "fetch('http://127.0.0.1:8099/evidence',{headers:{Authorization:'Bearer '+process.env.FIXTURE_KEY}}).then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))"
    output = subprocess.check_output(kube + ['exec', name, '--', 'node', '-e', proof_script], text=True, timeout=30)
    proof = json.loads(output)
    assert proof == {'requests': 2, 'toolObserved': True}, proof
    # Hive must also report the actual running pod on its operator API.
    status = api('GET', f'/hive/api/v1/fleet/agents/{agent_id}/status/', staff_token)
    if status.get('pod_status') != 'Running' or status.get('ready_containers', 0) < 2 or status.get('pod_name') != pod['metadata']['name']:
        raise RuntimeError('Hive operator status did not confirm the same ready agent and broker pod')
    print('PASS Hive staff-only provisioning, public-source agent, two model calls, real tool bytes and broker identity', flush=True)
