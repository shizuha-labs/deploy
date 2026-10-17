"""Public-source Kubernetes runtime for a standalone Hive installation.

This dedicated cluster is separate from the host's Docker/Kubernetes control
plane. Only Hive receives its kubeconfig; agent pods never mount host sockets.
"""
from pathlib import Path
from urllib.parse import urlsplit

RUNTIME_SOURCE = '4df33ff9d58fa36280634811c013caa63bb6b18a'
K3S_IMAGE = 'docker.io/rancher/k3s@sha256:71abd3a56f57884c62732e0e0d87606052cb5f8555b7db7e8e33c04570b8175c'
NODE_IMAGE = 'docker.io/library/node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
GO_IMAGE = 'docker.io/library/golang@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c'
BROKER_BASE_IMAGE = 'gcr.io/distroless/static-debian12@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab'


def extend(services, volumes, images, origin, runtime_commit):
    """Extend a generated Compose topology. Call after regular services exist."""
    tag = runtime_commit[:12]
    runtime_image = 'shizuha-selfhost-agent:public-' + tag
    broker_image = 'shizuha-selfhost-broker:public-' + tag
    volumes['runtime-cluster'] = {}
    services['runtime-cluster'] = {
        'image': images['k3s'], 'privileged': True,
        'command': ['server', '--disable=traefik', '--disable=servicelb',
                    '--disable=metrics-server', '--tls-san=runtime-cluster',
                    '--cluster-cidr=10.244.0.0/16', '--service-cidr=10.245.0.0/16',
                    '--cluster-dns=10.245.0.10', '--snapshotter=native',
                    '--node-name=selfhost-runtime', '--write-kubeconfig-mode=600'],
        'environment': {'K3S_TOKEN': '${K3S_TOKEN:?run prepare}'},
        'volumes': ['runtime-cluster:/var/lib/rancher/k3s'],
        'tmpfs': ['/run', '/var/run'],
        'healthcheck': {'test': ['CMD', 'kubectl', 'get', '--raw=/readyz'],
                        'interval': '5s', 'timeout': '5s', 'retries': 60},
    }
    services['runtime-image'] = {
        'build': {'context': './sources/runtime', 'dockerfile': '../../Dockerfile.runtime'},
        'image': runtime_image, 'entrypoint': ['/bin/true'], 'restart': 'no',
        'profiles': ['runtime-build'],
    }
    services['runtime-broker-image'] = {
        'build': {'context': './sources/runtime/mcp-auth-proxy', 'dockerfile': '../../../Dockerfile.broker'},
        'image': broker_image, 'entrypoint': ['/mcp-auth-proxy'],
        'command': ['--help'], 'restart': 'no', 'profiles': ['runtime-build'],
    }
    hive = services['hive']
    services['runtime-keys'] = {
        'build': dict(hive['build']), 'entrypoint': [],
        'command': ['python', '/bootstrap/runtime_keys.py'],
        'volumes': ['./runtime:/runtime', './runtime_keys.py:/bootstrap/runtime_keys.py:ro'],
        'restart': 'no',
    }
    hive.setdefault('depends_on', {})['runtime-keys'] = {'condition': 'service_completed_successfully'}
    hive['volumes'] = hive.get('volumes', []) + [
        './runtime:/runtime:ro', './runtime_hive_start.py:/bootstrap/runtime_hive_start.py:ro']
    hive['command'] = ['python', '/bootstrap/runtime_hive_start.py', *hive['command']]
    hive['environment'].update({
        'KUBECONFIG': '/runtime/kubeconfig', 'SHIZUHA_PLATFORM_URL': origin,
        'SHIZUHA_ID_INTERNAL_URL': 'http://id:8001',
        'SHIZUHA_CONNECT_INTERNAL_URL': 'http://connect:8019',
        'HIVE_ADMIN_API_URL': 'http://admin:8003/api',
        'HIVE_PUBLIC_PLATFORM_URL': origin, 'HIVE_ID_FLEET_PROVISIONER_TOKEN': '${FLEET_PROVISIONER_TOKEN:?run prepare}',
        'HIVE_NATIVE_RUNTIME_RELEASE_ENABLED': 'false',
        'RUNTIME_FLEET_API_URL': '', 'RUNTIME_FLEET_CONTROL_URL': '',
        'RUNTIME_STORAGE_CLASS': 'local-path', 'FLEET_AGENT_IMAGE': runtime_image,
        'FLEET_BROKER_IMAGE': broker_image, 'FLEET_DIND_REGISTRY_MIRROR': '',
        'HIVE_CAPABILITY_EVIDENCE_KID': 'selfhost-v1',
        # Hive fleet v1 and shared OAuth middleware consume different setting
        # names; both must use this installation's actual ID discovery route.
        'SHIZUHA_JWKS_URL': 'http://id:8001/shizuha-id/.well-known/jwks.json',
        'SHIZUHA_OAUTH_JWKS_URL': 'http://id:8001/shizuha-id/.well-known/jwks.json',
    })
    services['id']['environment']['FLEET_PROVISIONER_TOKEN'] = '${FLEET_PROVISIONER_TOKEN:?run prepare}'
    return {'agent_image': runtime_image, 'broker_image': broker_image, 'public_origin': origin,
            'public_hostname': urlsplit(origin).hostname, 'source_commit': runtime_commit}


DOCKERFILE = '''FROM {node} AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ git ca-certificates && rm -rf /var/lib/apt/lists/*
# Official Node supplies matching headers. Native fallback builds must use
# those bytes rather than download a second copy from nodejs.org.
ENV npm_config_nodedir=/usr/local
WORKDIR /opt/shizuha
COPY package.json package-lock.json ./
RUN npm ci --registry=https://registry.npmjs.org
COPY . .
RUN npm run build:node

FROM {node}
RUN apt-get update && apt-get install -y --no-install-recommends tini bash curl git python3 ca-certificates && rm -rf /var/lib/apt/lists/* && usermod -l agent node && usermod -d /home/agent -m agent
WORKDIR /opt/shizuha
COPY --from=build /opt/shizuha/dist ./dist
COPY --from=build /opt/shizuha/node_modules ./node_modules
COPY --from=build /opt/shizuha/package.json ./package.json
COPY --from=build /opt/shizuha/agent-runtime-entrypoint.sh /usr/local/bin/agent-runtime-entrypoint.sh
RUN chmod 755 /usr/local/bin/agent-runtime-entrypoint.sh && mkdir -p /home/agent/.shizuha && chown -R 1000:1000 /home/agent
ENV HOME=/home/agent SHIZUHA_DIST=/opt/shizuha/dist NPM_CONFIG_REGISTRY=https://registry.npmjs.org PIP_INDEX_URL=https://pypi.org/simple NODE_OPTIONS=--max-old-space-size=1536
USER 1000:1000
WORKDIR /home/agent/.shizuha
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-runtime-entrypoint.sh"]
'''

BROKER_DOCKERFILE = '''FROM {go} AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/mcp-auth-proxy ./cmd/mcp-auth-proxy
FROM {runtime}
COPY --from=build /out/mcp-auth-proxy /mcp-auth-proxy
USER 1000:1000
ENTRYPOINT ["/mcp-auth-proxy"]
'''


def write_files(output):
    output = Path(output)
    (output / 'runtime').mkdir(mode=0o700)
    (output / 'Dockerfile.runtime').write_text(DOCKERFILE.format(node=NODE_IMAGE))
    (output / 'Dockerfile.broker').write_text(BROKER_DOCKERFILE.format(go=GO_IMAGE, runtime=BROKER_BASE_IMAGE))
    for name in ('runtime_keys.py', 'runtime_hive_start.py', 'runtime_setup.py', 'runtime_agent.py'):
        (output / name).write_bytes(Path(__file__).with_name(name).read_bytes())
