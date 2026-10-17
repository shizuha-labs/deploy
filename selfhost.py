#!/usr/bin/env python3
"""Prepare an isolated Compose installation from reviewed public source commits."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import urlsplit
import runtime_support

SERVICES = {
    'id': (8001, 'shizuha_id', 'gunicorn'),
    'hive': (8030, 'hive_project', 'daphne'),
    'pulse': (8002, 'taskmanager', 'gunicorn'),
    'connect': (8019, 'connectmanager', 'daphne'),
    'wiki': (8013, 'wikimanager', 'gunicorn'),
    'admin': (8003, 'shizuha_admin', 'gunicorn'),
    'notes': (8012, 'notesmanager', 'gunicorn'),
    'drive': (8018, 'drivemanager', 'gunicorn'),
    'mail': (8014, 'mailmanager', 'gunicorn'),
}
REQUIRED = set(SERVICES) | {'packages', 'atlas', 'runtime', 'home'}
IMAGES = {'postgres', 'redis', 'nginx', 'object_store', 'mail_server', 'k3s'}


def validate_lock(lock):
    if lock.get('schema') != 1 or set(lock.get('sources', {})) != REQUIRED:
        raise ValueError('schema 1 requires all platform service sources plus packages and runtime')
    for name, source in lock['sources'].items():
        if not re.fullmatch(r'https://github\.com/shizuha-labs/[A-Za-z0-9_.-]+\.git', source.get('url', '')):
            raise ValueError(f'{name}: expected a public shizuha-labs GitHub HTTPS repository')
        if not re.fullmatch(r'[a-f0-9]{40}', source.get('commit', '')):
            raise ValueError(f'{name}: exact reviewed 40-character publish commit required')
        if set(source) != {'url', 'commit'}:
            raise ValueError(f'{name}: unexpected source fields')
    if set(lock.get('images', {})) != IMAGES:
        raise ValueError('release lock requires postgres, redis, nginx, object_store, mail_server and k3s image digests')
    for name, reference in lock['images'].items():
        if not re.fullmatch(r'(?:docker\.io|quay\.io|ghcr\.io)/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', reference):
            raise ValueError(f'{name}: public registry reference with a sha256 digest required')
    return lock


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)


def anonymous_git(*args, cwd=None):
    # Do not inherit GitHub credentials, credential helpers or URL rewrites.
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'GH_', 'GITHUB_'))}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false')
    subprocess.run(['git', '-c', 'credential.helper=', *args], cwd=cwd, env=env, check=True)


def fetch(lock, root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, source in lock['sources'].items():
        target = root / name
        anonymous_git('init', str(target))
        anonymous_git('fetch', '--depth=1', source['url'], source['commit'], cwd=target)
        anonymous_git('checkout', '--detach', source['commit'], cwd=target)
        # Git metadata must never enter application image layers.
        (target / '.dockerignore').write_text('.git\n.env\n.env.*\nnode_modules\n.venv\n')


def backend_dockerfile(name):
    return f'''FROM docker.io/library/python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
COPY --from=ghcr.io/astral-sh/uv@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libmagic1 libjpeg-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY {name}/ /app/
COPY packages/ /packages/
RUN if test -f uv.lock; then uv sync --frozen --no-dev --no-install-project --no-editable; else uv venv && uv pip install -r requirements.txt; fi
RUN uv pip install /packages/shizuha-auth /packages/shizuha-storage
RUN mkdir -p /var/log/django /var/log/supervisor /app/media /app/staticfiles
'''


def frontend_dockerfile(name):
    base = '/' if name == 'home' else f'/{name}/'
    api = '/api' if name == 'home' else f'/{name}/api'
    workdir = '/workspace/home/frontend' if name == 'home' else '/app'
    build_inputs = ("""ENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
COPY packages/shizuha-ui /workspace/packages/shizuha-ui
COPY packages/shizuha-chat /workspace/packages/shizuha-chat
ENV SHIZUHA_PACKAGES_DIR=/workspace/packages
RUN cd /workspace/packages/shizuha-ui && npm ci --ignore-scripts --omit=dev
RUN cd /workspace/packages/shizuha-chat && npm ci --ignore-scripts --omit=dev
RUN rm -rf /workspace/packages/shizuha-ui/node_modules/react /workspace/packages/shizuha-ui/node_modules/react-dom /workspace/packages/shizuha-chat/node_modules/react /workspace/packages/shizuha-chat/node_modules/react-dom
COPY home/nginx.prod.conf /workspace/home/nginx.prod.conf
ARG VITE_HOME_OPERATOR_USER_IDS=
ARG VITE_HOME_OPERATOR_AGENT=
ENV VITE_HOME_OPERATOR_USER_IDS=$VITE_HOME_OPERATOR_USER_IDS VITE_HOME_OPERATOR_AGENT=$VITE_HOME_OPERATOR_AGENT
""" if name == 'home' else '')
    build_context = (build_inputs + f'COPY {name}/frontend/ ./\n' if name == 'home' else
                     f'COPY {name}/frontend/ ./\nENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/\n')
    return f'''FROM docker.io/library/node@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR {workdir}
{build_context}RUN npm ci --ignore-scripts
ENV VITE_BASE_PATH={base} VITE_API_BASE={api} VITE_LOGIN_URL=/id/login NODE_ENV=production
RUN npm run build
FROM docker.io/library/nginx@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
COPY --from=build {workdir}/dist /usr/share/nginx/html
RUN printf '%s\\n' 'server {{ listen 80; root /usr/share/nginx/html; index index.html; location /health {{ return 200 "OK\\n"; }} location /assets/ {{ try_files $uri =404; }} location / {{ try_files $uri $uri/ /index.html; }} }}' > /etc/nginx/conf.d/default.conf
'''


def home_dockerfile():
    return """FROM docker.io/library/python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
COPY --from=ghcr.io/astral-sh/uv@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY home/backend/pyproject.toml home/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY home/backend/app ./app
RUN useradd --uid 10001 --home-dir /home/home --create-home home && mkdir /data && chown home:home /data
USER 10001
EXPOSE 8031
"""


def compose_config(public_url, port, images, storage_url="http://localhost:8333", runtime_commit=runtime_support.RUNTIME_SOURCE):
    url = urlsplit(public_url)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.path not in {'', '/'} or url.query or url.fragment or url.username or url.password:
        raise ValueError('public URL must be an HTTP(S) origin without credentials or a path')
    storage = urlsplit(storage_url)
    if storage.scheme not in {'http', 'https'} or not storage.hostname or storage.path not in {'', '/'} or storage.username or storage.password:
        raise ValueError('storage URL must be an HTTP(S) origin')
    origin = public_url.rstrip('/')
    services = {
        'postgres': {
            'image': images['postgres'],
            'environment': {'POSTGRES_PASSWORD': '${POSTGRES_ADMIN_PASSWORD:?run prepare}',
                            **{'DB_PASSWORD_' + name.upper(): '${DB_PASSWORD_' + name.upper() + ':?run prepare}' for name in SERVICES}},
            'volumes': ['postgres:/var/lib/postgresql/data', './init-db.sh:/docker-entrypoint-initdb.d/init-db.sh:ro'],
            'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U postgres'], 'interval': '5s', 'timeout': '5s', 'retries': 30},
        },
        'redis': {'image': images['redis'], 'command': ['redis-server', '--appendonly', 'yes'],
                  'volumes': ['redis:/data'], 'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '5s', 'timeout': '3s', 'retries': 30}},
    }
    services['object-store'] = {
        'image': images['object_store'], 'command': ['mini', '-dir=/data'],
        'ports': ['127.0.0.1:8333:8333'],
        'environment': {'AWS_ACCESS_KEY_ID': '${S3_ACCESS_KEY:?run prepare}',
                        'AWS_SECRET_ACCESS_KEY': '${S3_SECRET_KEY:?run prepare}'},
        'volumes': ['object-store:/data'],
    }
    services['object-store-init'] = {
        'build': {'context': './sources', 'dockerfile': '../Dockerfile.wiki'},
        'entrypoint': [], 'command': ['python', '/s3-bootstrap.py'],
        'volumes': ['./s3-bootstrap.py:/s3-bootstrap.py:ro'],
        'environment': services['object-store']['environment'],
        'depends_on': {'object-store': {'condition': 'service_started'}},
        'restart': 'no',
    }
    services['mail-server'] = {
        'image': images['mail_server'], 'command': ['--config', '/etc/stalwart/config.json'],
        'environment': {'STALWART_RECOVERY_ADMIN': 'admin:${STALWART_ADMIN_PASSWORD:?run prepare}'},
        'volumes': ['./stalwart.json:/etc/stalwart/config.json:ro', 'mail-server:/var/lib/stalwart'],
    }
    volumes = {'postgres': {}, 'redis': {}, 'oauth-keys': {}, 'object-store': {}, 'mail-server': {}}
    for index, (name, (svc_port, module, server)) in enumerate(SERVICES.items(), 1):
        env = {
            'DEBUG': 'false', 'JWT_SECRET_KEY': '${JWT_SECRET_KEY:?run prepare}',
            'SECRET_KEY': '${JWT_SECRET_KEY:?run prepare}',
            'ALLOWED_HOSTS': f'{url.hostname},localhost,127.0.0.1,{name},shizuha-{name}',
            'CSRF_TRUSTED_ORIGINS': origin, 'CORS_ALLOWED_ORIGINS': origin,
            'CORS_ALLOW_ALL_ORIGINS': 'false',
            'POSTGRES_HOST': 'postgres', 'POSTGRES_PORT': '5432', 'POSTGRES_DB': f'shizuha_{name}',
            'POSTGRES_USER': name, 'POSTGRES_PASSWORD': '${DB_PASSWORD_' + name.upper() + ':?run prepare}',
            'DB_HOST': 'postgres', 'DB_PORT': '5432', 'DB_NAME': f'shizuha_{name}',
            'DB_USER': name, 'DB_PASSWORD': '${DB_PASSWORD_' + name.upper() + ':?run prepare}',
            'REDIS_HOST': 'redis', 'REDIS_URL': f'redis://redis:6379/{index}',
            'REDIS_CHANNEL_URL': f'redis://redis:6379/{index}',
            'CELERY_BROKER_URL': f'redis://redis:6379/{index}', 'CELERY_RESULT_BACKEND': f'redis://redis:6379/{index}',
            'SHIZUHA_PUBLIC_HOST': url.netloc, 'SHIZUHA_PUBLIC_SCHEME': url.scheme,
            'SHIZUHA_ID_API_URL': 'http://id:8001/api', 'SHIZUHA_ID_INTERNAL_URL': 'http://id:8001',
            'SHIZUHA_ID_FRONTEND_URL': f'{origin}/id', 'SHIZUHA_OAUTH_ISSUER': f'{origin}/id',
            'OAUTH_ISSUER_URL': f'{origin}/id', 'SHIZUHA_EXPECTED_ISSUER': f'{origin}/id',
            'SHIZUHA_REVOCATION_STATUS_URL': 'http://id:8001/api/oauth/revocation-status',
            'SHIZUHA_OAUTH_KEY_DIR': '/var/shizuha/oauth-keys',
            'HIVE_PUBLIC_PLATFORM_URL': origin,
            'JWT_AUTH_COOKIE_DOMAIN': '',
            'MAIL_DOMAIN': '${MAIL_DOMAIN:?run prepare}',
            'SHIZUHA_MAIL_DOMAIN': '${MAIL_DOMAIN:?run prepare}',
            'SHIZUHA_MAIL_OAUTH_CLIENT_ID': '${SHIZUHA_MAIL_OAUTH_CLIENT_ID:?run prepare}',
            'SHIZUHA_OAUTH_TOKEN_URL': 'http://id:8001/api/oauth/token',
            'STALWART_JMAP_URL': 'http://mail-server:8080',
            'STALWART_ADMIN_URL': 'http://mail-server:8080',
            'STALWART_PROVISIONING_URL': 'http://mail-server:8080',
            'STALWART_PROVISIONING_USER': 'admin',
            'STALWART_PROVISIONING_PASSWORD': '${STALWART_ADMIN_PASSWORD:?run prepare}',
            'STALWART_ADMIN_PASSWORD': '${STALWART_ADMIN_PASSWORD:?run prepare}',
            'UNIFIED_MAIL_JMAP_URL': 'http://mail-server:8080',
            'UNIFIED_MAIL_SMTP_HOST': 'mail-server', 'UNIFIED_MAIL_SMTP_PORT': '587',
            'ID_TO_MAIL_LIFECYCLE_SECRET': '${ID_TO_MAIL_LIFECYCLE_SECRET:?run prepare}',
            'MAIL_USER_LIFECYCLE_CONSUMER_URL': 'http://mail:8014/api/internal/user-lifecycle-events/',
            'SHIZUHA_WIKI_PUBLIC_URL': f'{origin}/wiki',
            'FORCE_SCRIPT_NAME': f'/{name}', 'PUBLIC_API_PREFIX': f'/{name}',
            'MINIO_ENDPOINT': 'object-store:8333' if name == 'pulse' else 'http://object-store:8333',
            'MINIO_PUBLIC_ENDPOINT': storage.netloc,
            'AWS_S3_PUBLIC_ENDPOINT_URL': storage_url,
            'MINIO_PUBLIC_SECURE': 'true' if storage.scheme == 'https' else 'false',
            'MINIO_ACCESS_KEY': '${S3_ACCESS_KEY:?run prepare}',
            'MINIO_SECRET_KEY': '${S3_SECRET_KEY:?run prepare}',
            'MINIO_PULSE_BUCKET': 'pulse-attachments',
            'MINIO_BUCKET': f'shizuha_{name}'.replace('_', '-'),
            'PULSE_EXPECTED_MINIO_ENDPOINT': 'object-store:8333',
            'SHIZUHA_SERVICE_NAME': name,
            'SHIZUHA_OAUTH_JWKS_URL': 'http://id:8001/shizuha-id/.well-known/jwks.json',
            'PULSE_EXPECTED_MINIO_BUCKET': 'pulse-attachments',
            'PULSE_EXPECTED_MINIO_REGION': 'us-east-1',
            'USE_S3_STORAGE': '1' if name == 'drive' else '0',
            'AWS_ACCESS_KEY_ID': '${S3_ACCESS_KEY:?run prepare}',
            'AWS_SECRET_ACCESS_KEY': '${S3_SECRET_KEY:?run prepare}',
            'AWS_S3_ENDPOINT_URL': 'http://object-store:8333',
            'AWS_STORAGE_BUCKET_NAME': f'shizuha-{name}',
            'WIKI_REQUIRE_SHARED_MEDIA_STORAGE': '1' if name == 'wiki' else '0',
            'CONNECT_SECURITY_EVENT_ORIGIN_SECRET': '${CONNECT_SECURITY_EVENT_ORIGIN_SECRET:?run prepare}',
            'SHIZUHA_CONNECT_SECURITY_EVENT_URL': 'http://connect:8019/api/internal/security-events/',
            'ID_TO_PULSE_LIFECYCLE_SECRET': '${ID_TO_PULSE_LIFECYCLE_SECRET:?run prepare}',
            'PULSE_USER_LIFECYCLE_CONSUMER_URL': 'http://pulse:8002/api/internal/user-lifecycle-events/',
        }
        # Credential consumers are explicit: unrelated application containers
        # must not receive mailbox administration or lifecycle-signing powers.
        consumers = {
            'SHIZUHA_MAIL_OAUTH_CLIENT_ID': {'id', 'mail'},
            'SHIZUHA_MAIL_DOMAIN': {'id', 'mail'},
            'ID_TO_MAIL_LIFECYCLE_SECRET': {'id', 'mail'},
            'MAIL_USER_LIFECYCLE_CONSUMER_URL': {'id'},
            'ID_TO_PULSE_LIFECYCLE_SECRET': {'id', 'pulse'},
            'PULSE_USER_LIFECYCLE_CONSUMER_URL': {'id'},
            'CONNECT_SECURITY_EVENT_ORIGIN_SECRET': {'id', 'admin', 'wiki', 'connect'},
        }
        for key in tuple(env):
            if key.startswith(('STALWART_', 'UNIFIED_MAIL_')) or key == 'MAIL_DOMAIN':
                consumers[key] = {'mail'}
        env = {key: value for key, value in env.items()
               if key not in consumers or name in consumers[key]}
        for peer, (peer_port, _, _) in SERVICES.items():
            env[f'SHIZUHA_{peer.upper()}_API_URL'] = f'http://{peer}:{peer_port}/api'
            env[f'SHIZUHA_{peer.upper()}_URL'] = f'http://{peer}:{peer_port}'
        if name == 'pulse':
            # Privileged assignments require signed Hive capability evidence.
            # Ordinary task CRUD remains available without that integration.
            env['PULSE_SUBSTRATE_CAP_MODE'] = 'enforce'
        if name == 'id':
            env['SHIZUHA_MAIL_API_URL'] = 'http://mail:8014'
        build = {'context': './sources', 'dockerfile': f'../Dockerfile.{name}'}
        migration = f'{name}-migrate'
        services[migration] = {'build': build, 'entrypoint': [], 'command': ['python', 'manage.py', 'migrate', '--noinput'], 'environment': env,
                              'depends_on': {'postgres': {'condition': 'service_healthy'}, 'redis': {'condition': 'service_healthy'}}, 'restart': 'no'}
        cmd = (['daphne', '-b', '0.0.0.0', '-p', str(svc_port), f'{module}.asgi:application'] if server == 'daphne'
               else ['gunicorn', '--bind', f'0.0.0.0:{svc_port}', '--workers', '2', f'{module}.wsgi:application'])
        volumes[f'{name}-media'] = {}
        services[name] = {'build': build, 'entrypoint': [], 'command': cmd, 'environment': env,
                          'depends_on': {migration: {'condition': 'service_completed_successfully'}, 'object-store-init': {'condition': 'service_completed_successfully'}},
                          'volumes': [f'{name}-media:/app/media'],
                          'networks': {'default': {'aliases': [f'shizuha-{name}']}},
                          'healthcheck': {'test': ['CMD', 'python', '-c', f'import urllib.request; urllib.request.urlopen("http://localhost:{svc_port}/api/health/", timeout=5)'], 'interval': '10s', 'timeout': '6s', 'retries': 30, 'start_period': '30s'}}
        if name == 'id':
            services[name]['volumes'].append('oauth-keys:/var/shizuha/oauth-keys')
            # Legacy data migration promotes an existing configured client. On a
            # fresh database, provision it explicitly after schema migrations.
            services[migration]['environment'] = {**env, 'SHIZUHA_MAIL_OAUTH_CLIENT_ID': ''}
            services[migration]['volumes'] = ['./identity-bootstrap.py:/identity-bootstrap.py:ro']
            services[migration]['command'] = ['sh', '-ec', 'python manage.py migrate --noinput && python manage.py shell < /identity-bootstrap.py']
            services[migration]['environment']['SELFHOST_MAIL_CLIENT_ID'] = '${SHIZUHA_MAIL_OAUTH_CLIENT_ID:?run prepare}'
        if name in {'pulse', 'connect', 'wiki', 'notes', 'drive', 'mail'}:
            for role, command in {'worker': ['celery', '-A', module, 'worker', '-l', 'info', '-Q', f'shizuha_{name}', '--concurrency=2'],
                                  'beat': ['celery', '-A', module, 'beat', '-l', 'info']}.items():
                services[f'{name}-{role}'] = {'build': build, 'entrypoint': [], 'command': command, 'environment': env,
                                             'depends_on': {migration: {'condition': 'service_completed_successfully'}}}
        services[f'{name}-frontend'] = {'build': {'context': './sources', 'dockerfile': f'../Dockerfile.{name}-frontend'}}
    for name, suffix, command in (
        ('id', 'lifecycle', ['python', 'manage.py', 'dispatch_user_lifecycle_events', '--loop']),
        ('pulse', 'scheduler', ['python', 'manage.py', 'run_scheduler']),
    ):
        source = services[name]
        services[f'{name}-{suffix}'] = {
            'build': source['build'], 'entrypoint': [], 'command': command,
            'environment': source['environment'], 'depends_on': source['depends_on'],
            'restart': 'unless-stopped',
        }
    volumes['atlas-data'] = {}
    services['atlas'] = {
        'build': {'context': './sources', 'dockerfile': '../Dockerfile.atlas'},
        'environment': {'ATLAS_DB': '/data/atlas.db',
                        'ATLAS_JWKS_URL': 'http://id:8001/shizuha-id/.well-known/jwks.json',
                        'SHIZUHA_REVOCATION_STATUS_URL': 'http://id:8001/api/oauth/revocation-status'},
        'volumes': ['atlas-data:/data'],
        'healthcheck': {'test': ['CMD', 'python', '-c',
                               'import urllib.request; urllib.request.urlopen("http://localhost:8080/health", timeout=5)'],
                        'interval': '10s', 'timeout': '6s', 'retries': 30},
    }
    services['mail-server-init'] = {
        'build': services['mail']['build'], 'entrypoint': [], 'command': ['python', '/mail_bootstrap.py'],
        'volumes': ['./mail_bootstrap.py:/mail_bootstrap.py:ro'],
        'environment': {key: services['mail']['environment'][key] for key in (
            'STALWART_ADMIN_PASSWORD', 'STALWART_ADMIN_URL', 'MAIL_DOMAIN',
            'SHIZUHA_OAUTH_ISSUER', 'SHIZUHA_MAIL_OAUTH_CLIENT_ID')},
        'depends_on': {'mail-server': {'condition': 'service_started'}}, 'restart': 'no',
    }
    services['mail']['depends_on']['mail-server-init'] = {'condition': 'service_completed_successfully'}
    services['home'] = {
        'build': {'context': './sources', 'dockerfile': '../Dockerfile.home'},
        'entrypoint': [],
        'command': ['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8031'],
        'environment': {
            'SHIZUHA_ID_URL': 'http://id:8001',
            'SHIZUHA_OAUTH_JWKS_URL': 'http://id:8001/shizuha-id/.well-known/jwks.json',
            'SHIZUHA_EXPECTED_ISSUER': origin + '/id',
            'SHIZUHA_REVOCATION_STATUS_URL': 'http://id:8001/api/oauth/revocation-status',
            'PULSE_API_URL': 'http://pulse:8002', 'ADMIN_API_URL': 'http://admin:8003/api',
            'HIVE_API_URL': 'http://hive:8030/api', 'HIVE_METERING_URL': 'http://hive:8030',
            'CONNECT_API_URL': 'http://connect:8019/api',
            'HOME_REDIS_URL': 'redis://redis:6379/12',
            'HOME_OPERATOR_USER_IDS': '${HOME_OPERATOR_USER_IDS:-}',
            'HOME_AUDIT_LEAD_STORE_PATH': '/data/audit-leads.jsonl',
        },
        'depends_on': {name: {'condition': 'service_healthy'} for name in ('id', 'admin', 'pulse', 'hive', 'connect', 'redis')},
        'volumes': ['home-data:/data'],
        'healthcheck': {'test': ['CMD', 'python', '-c',
                               'import urllib.request; urllib.request.urlopen("http://localhost:8031/api/home/health", timeout=5)'],
                        'interval': '10s', 'timeout': '6s', 'retries': 30},
    }
    volumes['home-data'] = {}
    services['home-frontend'] = {'build': {'context': './sources', 'dockerfile': '../Dockerfile.home-frontend',
        'args': {'VITE_HOME_OPERATOR_USER_IDS': '${HOME_OPERATOR_USER_IDS:-}',
                 'VITE_HOME_OPERATOR_AGENT': '${HOME_OPERATOR_AGENT:-}'}}}
    services['gateway'] = {'networks': {'default': {'aliases': [url.hostname] if url.scheme == 'http' else []}}, 'image': images['nginx'], 'ports': [f'127.0.0.1:{port}:80'],
                           'volumes': ['./nginx.conf:/etc/nginx/conf.d/default.conf:ro'],
                           'depends_on': {name: {'condition': 'service_healthy'} for name in SERVICES}}
    services['gateway']['depends_on']['home'] = {'condition': 'service_healthy'}
    services['gateway']['depends_on']['home-frontend'] = {'condition': 'service_started'}
    services['gateway']['depends_on']['atlas'] = {'condition': 'service_healthy'}
    for name in SERVICES:
        services['gateway']['depends_on'][f'{name}-frontend'] = {'condition': 'service_started'}
    runtime = runtime_support.extend(services, volumes, images, origin, runtime_commit)
    for service in services.values():
        service['logging'] = {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
        if not service.get('command', [None])[0] == 'python':
            service.setdefault('restart', 'unless-stopped')
    return {'name': 'shizuha-selfhost', 'services': services, 'volumes': volumes, 'x-shizuha-runtime': runtime}


def nginx_config(public_url):
    text = "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
    # Docker can reassign addresses on restart. Resolve service names throughout
    # the gateway lifetime; resolving them only at nginx startup pins stale IPs.
    text += 'resolver 127.0.0.11 valid=5s ipv6=off;\n'
    for name, (port, _, _) in SERVICES.items():
        text += f'upstream {name}_backend {{ zone {name}_backend 64k; server {name}:{port} resolve; }}\n'
        text += f'upstream {name}_frontend {{ zone {name}_frontend 64k; server {name}-frontend:80 resolve; }}\n'
    text += 'upstream home_backend { zone home_backend 64k; server home:8031 resolve; }\n'
    text += 'upstream home_frontend { zone home_frontend 64k; server home-frontend:80 resolve; }\n'
    text += 'upstream atlas_backend { zone atlas_backend 64k; server atlas:8080 resolve; }\n'
    text += '''server {
 listen 80;
 server_name _;
 client_max_body_size 50m;
 proxy_set_header Host $http_host;
 proxy_set_header X-Forwarded-Host $http_host;
 proxy_set_header X-Forwarded-Proto $scheme;
 proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
 proxy_http_version 1.1;
 proxy_set_header Upgrade $http_upgrade;
 proxy_set_header Connection $connection_upgrade;
 location /api/home/ { proxy_pass http://home_backend; }
 location /api/usage/ { proxy_pass http://home_backend; }
 location /api/ { return 404; }
 location / { proxy_pass http://home_frontend/; }
'''
    for name, (port, _, _) in SERVICES.items():
        text += f' location = /{name} {{ return 302 /{name}/; }}\n'
        text += f' location /{name}/api/ {{ proxy_pass http://{name}_backend/api/; }}\n'
        text += f' location /{name}/ws/ {{ proxy_pass http://{name}_backend/ws/; }}\n'
        text += f' location /{name}/ {{ proxy_pass http://{name}_frontend/; }}\n'
    text += ' location /id/.well-known/ { proxy_pass http://id_backend/shizuha-id/.well-known/; }\n location = /atlas/health { proxy_pass http://atlas_backend/health; }\n location = /atlas { return 302 /atlas/; }\n location /atlas/ { proxy_pass http://atlas_backend; }\n}\n'
    # Canonical OIDC endpoints must also be reachable at the same port inside
    # the Compose network (the public hostname is a gateway network alias).
    public_port = urlsplit(public_url).port or (443 if urlsplit(public_url).scheme == 'https' else 80)
    if public_port != 80 and urlsplit(public_url).scheme == 'http':
        text = text.replace(' listen 80;', f' listen 80;\n listen {public_port};')
    return text.replace('X-Forwarded-Proto $scheme', 'X-Forwarded-Proto ' + urlsplit(public_url).scheme)


def prepare(lock, output, public_url, port, storage_url="http://localhost:8333"):
    config = compose_config(public_url, port, lock['images'], storage_url, lock['sources']['runtime']['commit'])
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_write(output / 'source-lock.json', json.dumps(lock, indent=2) + '\n')
    keys = ['POSTGRES_ADMIN_PASSWORD', 'JWT_SECRET_KEY', 'CONNECT_SECURITY_EVENT_ORIGIN_SECRET', 'ID_TO_PULSE_LIFECYCLE_SECRET']
    keys += ['S3_ACCESS_KEY', 'S3_SECRET_KEY', 'STALWART_ADMIN_PASSWORD', 'SHIZUHA_MAIL_OAUTH_CLIENT_ID', 'ID_TO_MAIL_LIFECYCLE_SECRET']
    keys += ['K3S_TOKEN', 'FLEET_PROVISIONER_TOKEN']
    keys += ['DB_PASSWORD_' + name.upper() for name in SERVICES]
    values = {key: secrets.token_hex(10 if key == 'S3_ACCESS_KEY' else (20 if key == 'S3_SECRET_KEY' else 32)) for key in keys}
    private_write(output / 'secrets.env', ''.join(f'{key}={value}\n' for key, value in values.items()) + 'MAIL_DOMAIN=example.org\n')
    private_write(output / 'runtime.json', json.dumps(config.pop('x-shizuha-runtime'), indent=2) + '\n')
    runtime_support.write_files(output)
    private_write(output / 'compose.json', json.dumps(config, indent=2) + '\n')
    private_write(output / 'nginx.conf', nginx_config(public_url))
    init = '#!/bin/sh\nset -eu\n'
    for name in SERVICES:
        init += f'''psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v app_password="$DB_PASSWORD_{name.upper()}" <<'SQL'
CREATE USER {name} WITH PASSWORD :'app_password';
CREATE DATABASE shizuha_{name} OWNER {name};
SQL
'''
    private_write(output / 'init-db.sh', init)
    os.chmod(output / 'init-db.sh', 0o755)
    for name in SERVICES:
        private_write(output / f'Dockerfile.{name}', backend_dockerfile(name))
        private_write(output / f'Dockerfile.{name}-frontend', frontend_dockerfile(name))
    private_write(output / 'Dockerfile.home', home_dockerfile())
    private_write(output / 'Dockerfile.home-frontend', frontend_dockerfile('home'))
    private_write(output / 'Dockerfile.atlas', '''FROM docker.io/library/node@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS ui
WORKDIR /src
COPY atlas/frontend/ ./
ENV NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
RUN npm ci --ignore-scripts && npm run build
FROM docker.io/library/python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
WORKDIR /app
COPY atlas/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY atlas/app.py atlas/auth.py atlas/store.py atlas/token_revocation.py ./
COPY atlas/catalog ./catalog
COPY atlas/scripts ./scripts
COPY --from=ui /src/dist ./static
RUN useradd --uid 10001 --home-dir /home/atlas --create-home atlas && mkdir /data && chown atlas:atlas /data
USER 10001
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
''')
    private_write(output / 's3-bootstrap.py', '''import os, time
import boto3
from botocore.config import Config
client = boto3.client('s3', endpoint_url='http://object-store:8333', region_name='us-east-1', config=Config(signature_version='s3v4', connect_timeout=3, read_timeout=3, retries={'max_attempts': 1}))
for attempt in range(60):
    try:
        client.list_buckets()
        break
    except Exception:
        if attempt == 59:
            raise
        time.sleep(2)
for name in ['pulse-attachments', 'shizuha-wiki', 'shizuha-connect', 'shizuha-notes', 'shizuha-drive', 'shizuha-mail']:
    existing = {bucket['Name'] for bucket in client.list_buckets()['Buckets']}
    if name not in existing:
        client.create_bucket(Bucket=name)
print('Object-storage buckets ready')
''')
    private_write(output / 'stalwart.json', json.dumps({'@type': 'RocksDb', 'path': '/var/lib/stalwart'}))
    # Stalwart runs as uid2000 and must read the non-secret datastore pointer.
    os.chmod(output / 'stalwart.json', 0o644)
    private_write(output / 'mail_bootstrap.py', Path(__file__).with_name('mail_bootstrap.py').read_text())
    private_write(output / 'bootstrap_admin.py', Path(__file__).with_name('bootstrap_admin.py').read_text())
    private_write(output / 'identity-bootstrap.py', '''import os
from identity.models import OAuthClient
client, created = OAuthClient.objects.get_or_create(client_id=os.environ['SELFHOST_MAIL_CLIENT_ID'], defaults={'client_name': 'shizuha-mail', 'is_system': True, 'scope': 'openid email', 'grant_types': ['authorization_code', 'refresh_token'], 'redirect_uris': []})
if not client.is_system or client.client_name != 'shizuha-mail':
    raise RuntimeError('Configured mail client conflicts with an existing client')
print('Mail OAuth client ready')
''')
    private_write(output / '.gitignore', '*\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'fetch', 'validate'])
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('installation'))
    parser.add_argument('--public-url', default='http://shizuha.localhost:8080')
    parser.add_argument('--storage-url', default='http://localhost:8333')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    lock = validate_lock(json.loads(args.lock.read_text()))
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.command == 'prepare':
        prepare(lock, args.output, args.public_url, args.port, args.storage_url)
    elif args.command == 'fetch':
        if not args.output.is_dir():
            parser.error('run prepare first')
        recorded = json.loads((args.output / 'source-lock.json').read_text())
        if recorded != lock:
            parser.error('source lock differs from the prepared installation')
        fetch(lock, args.output / 'sources')
        private_write(args.output / 'sources' / '.dockerignore', '**/.git\n**/.env\n**/.env.*\n**/node_modules\n**/.venv\n**/__pycache__\n')
    print(f'{args.command}: complete')


if __name__ == '__main__':
    main()
