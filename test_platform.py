import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('selfhost_platform', Path(__file__).with_name('selfhost.py'))
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class InstallationTest(unittest.TestCase):
    def test_home_is_required_and_has_its_real_bff_and_root_frontend(self):
        self.assertIn('home', platform.REQUIRED)
        self.assertNotIn('home', platform.SERVICES, 'Home is FastAPI, not a Django migration target')
        services = platform.compose_config('https://platform.example.org', 8080, self.lock()['images'])['services']
        self.assertEqual(services['home']['command'], ['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8031'])
        self.assertNotIn('home-migrate', services)
        env = services['home']['environment']
        self.assertEqual(env['SHIZUHA_OAUTH_JWKS_URL'], 'http://id:8001/shizuha-id/.well-known/jwks.json')
        self.assertEqual(env['SHIZUHA_EXPECTED_ISSUER'], 'https://platform.example.org/id')
        self.assertEqual(env['SHIZUHA_REVOCATION_STATUS_URL'], 'http://id:8001/api/oauth/revocation-status')
        self.assertEqual(env['HOME_REDIS_URL'], 'redis://redis:6379/12')
        self.assertEqual(env['HIVE_API_URL'], 'http://hive:8030/api')
        self.assertEqual(env['HIVE_METERING_URL'], 'http://hive:8030')
        self.assertEqual(env['HOME_OPERATOR_USER_IDS'], '${HOME_OPERATOR_USER_IDS:-}')
        self.assertEqual(services['home-frontend']['build']['args'], {
            'VITE_HOME_OPERATOR_USER_IDS': '${HOME_OPERATOR_USER_IDS:-}',
            'VITE_HOME_OPERATOR_AGENT': '${HOME_OPERATOR_AGENT:-}'})
        self.assertEqual(set(env) & {'JWT_SECRET_KEY', 'POSTGRES_PASSWORD', 'FLEET_PROVISIONER_TOKEN', 'S3_SECRET_KEY'}, set())
        self.assertIn('home', services['gateway']['depends_on'])
        self.assertIn('home-frontend', services['gateway']['depends_on'])

    def test_home_root_and_api_routes_do_not_replace_other_service_routes(self):
        gateway = platform.nginx_config('http://localhost:8080')
        self.assertNotIn('location = / { return 302 /wiki/;', gateway)
        self.assertIn('location / { proxy_pass http://home_frontend/; }', gateway)
        self.assertIn('location /api/home/ { proxy_pass http://home_backend; }', gateway)
        self.assertIn('location /api/usage/ { proxy_pass http://home_backend; }', gateway)
        self.assertIn('location /api/ { return 404; }', gateway)
        for name in ('id', 'hive', 'pulse', 'connect', 'wiki'):
            self.assertIn(f'location /{name}/api/ {{ proxy_pass http://{name}_backend/api/; }}', gateway)

    def test_prepared_home_build_is_locked_isolated_and_uses_root_asset_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'install'
            platform.prepare(self.lock(), out, 'http://localhost:8080', 8080)
            backend = (out / 'Dockerfile.home').read_text()
            self.assertIn('COPY home/backend/', backend)
            self.assertIn('uv sync --frozen --no-dev --no-install-project', backend)
            self.assertIn('USER 10001', backend)
            frontend = (out / 'Dockerfile.home-frontend').read_text()
            self.assertIn('COPY home/frontend/', frontend)
            self.assertIn('VITE_BASE_PATH=/ VITE_API_BASE=/api', frontend)
            self.assertIn('npm ci --ignore-scripts', frontend)
            self.assertNotIn('npm install', frontend)
            self.assertIn('COPY packages/shizuha-ui /workspace/packages/shizuha-ui', frontend)
            self.assertIn('COPY packages/shizuha-chat /workspace/packages/shizuha-chat', frontend)
            self.assertNotIn('DB_PASSWORD_HOME', (out / 'secrets.env').read_text())

    def test_generated_home_copy_context_preserves_locked_local_package_targets(self):
        # Execute the generated COPY/WORKDIR geometry against an isolated source
        # context. A flattened /app consumer resolves ../../packages outside it.
        import shlex
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, stage = root / 'sources', root / 'stage'
            frontend = context / 'home/frontend'
            frontend.mkdir(parents=True)
            dependencies = {f'@shizuha/{name}': f'file:../../packages/shizuha-{name}'
                            for name in ('ui', 'chat')}
            (frontend / 'package.json').write_text(json.dumps({'dependencies': dependencies}))
            (context / 'home/nginx.prod.conf').write_text('# reviewed source guard fixture')
            for name in ('ui', 'chat'):
                package = context / f'packages/shizuha-{name}'
                (package / 'src').mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({'name': f'@shizuha/{name}'}))
                (package / 'package-lock.json').write_text('{"lockfileVersion": 3}')
            workdir = stage
            installs = []
            for line in platform.frontend_dockerfile('home').splitlines():
                fields = shlex.split(line)
                if not fields:
                    continue
                if fields[0] == 'FROM' and 'build' not in fields:
                    break
                if fields[0] == 'WORKDIR':
                    workdir = stage / fields[1].lstrip('/')
                    workdir.mkdir(parents=True, exist_ok=True)
                elif fields[0] == 'COPY':
                    source = context / fields[1]
                    target = stage / fields[2].lstrip('/') if fields[2].startswith('/') else workdir / fields[2]
                    if source.is_dir():
                        shutil.copytree(source, target, dirs_exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                elif fields[0] == 'RUN' and 'npm ci' in line:
                    installs.append(line)
                    if fields[1] == 'cd':
                        package = stage / fields[2].lstrip('/')
                        self.assertTrue((package / 'package-lock.json').is_file())
                    else:
                        actual = json.loads((workdir / 'package.json').read_text())
                        for name, reference in actual['dependencies'].items():
                            target = (workdir / reference.removeprefix('file:')).resolve()
                            self.assertTrue(target.is_relative_to(stage), name)
                            self.assertEqual(json.loads((target / 'package.json').read_text())['name'], name)
                            self.assertTrue((target / 'src').is_dir())
            self.assertEqual(len(installs), 3)
            self.assertIn('COPY --from=build /workspace/home/frontend/dist /usr/share/nginx/html', platform.frontend_dockerfile('home'))

    def test_ci_hostname_mapping_matches_ipv4_publish_when_dns_is_ipv6_only(self):
        import http.server
        import socket
        import threading
        import urllib.request
        spec = importlib.util.spec_from_file_location('installer_verify', Path(__file__).with_name('verify.py'))
        verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{}' if 'health' in self.path else b'<html>owned fixture</html>'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = root / 'hosts'
            hosts.write_text('127.0.0.1 localhost\n::1 localhost\n')
            binary = root / 'getent'
            binary.write_text('#!/bin/sh\nprintf "%s\\n" "::1 shizuha.localhost"\n')
            binary.chmod(0o700)
            script = Path(__file__).with_name('ci-validate.sh').read_text()
            start = script.index('export SHIZUHA_ACCEPTANCE_URL=')
            end = script.index('python3 "$script_dir/selfhost.py" prepare')
            # Only redirect the actual hosts-file write into this owned fixture.
            block = script[start:end].replace('/etc/hosts', str(hosts))
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']}
            subprocess.run(['bash', '-eu', '-c', block], env=env, check=True)
            first = hosts.read_bytes()
            subprocess.run(['bash', '-eu', '-c', block], env=env, check=True)
            self.assertEqual(hosts.read_bytes(), first, 'host registration must be idempotent')
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            resolve = socket.getaddrinfo
            def fixture_resolver(host, port, *args, **kwargs):
                if host == 'shizuha.localhost':
                    mapped = any(parts[0] == '127.0.0.1' and host in parts[1:]
                                 for line in hosts.read_text().splitlines()
                                 if (parts := line.split('#', 1)[0].split()))
                    host = '127.0.0.1' if mapped else '::1'
                return resolve(host, port, *args, **kwargs)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with patch.object(socket, 'getaddrinfo', side_effect=fixture_resolver), patch.object(verify, 'urlopen', opener.open):
                    checks = verify.check(f'http://shizuha.localhost:{server.server_port}')
                self.assertEqual(len(checks), 22)
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_installer_directory_does_not_shadow_uuid_platform_dependency(self):
        check = "import uuid, platform; assert uuid.uuid4().version == 4; assert platform.system()"
        subprocess.run([sys.executable, '-c', check], cwd=Path(__file__).parent, check=True)
        # Prove this check catches the historical entrypoint collision.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'platform.py').write_text('# installer collision\n')
            failed = subprocess.run([sys.executable, '-c', check], cwd=tmp, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(b'has no attribute', failed.stderr)

    def test_frontend_asset_prefix_is_stripped_before_shared_root_server(self):
        gateway = platform.nginx_config('http://localhost:8080')
        for name in platform.SERVICES:
            self.assertIn(f'location /{name}/ {{ proxy_pass http://{name}_frontend/; }}', gateway)
            dockerfile = platform.frontend_dockerfile(name)
            self.assertIn('root /usr/share/nginx/html', dockerfile)
            self.assertIn('location /assets/ { try_files $uri =404; }', dockerfile)
            self.assertNotIn('nginx.prod.conf', dockerfile)

    def test_gateway_tracks_changed_addresses_for_every_service(self):
        gateway = platform.nginx_config('http://localhost:8080')
        self.assertIn('resolver 127.0.0.11 valid=5s ipv6=off;', gateway)
        for name, (port, _, _) in platform.SERVICES.items():
            self.assertIn(f'upstream {name}_backend {{ zone {name}_backend 64k; server {name}:{port} resolve; }}', gateway)
            self.assertIn(f'upstream {name}_frontend {{ zone {name}_frontend 64k; server {name}-frontend:80 resolve; }}', gateway)
        self.assertIn('upstream atlas_backend { zone atlas_backend 64k; server atlas:8080 resolve; }', gateway)

    def lock(self):
        return {'schema': 1, 'images': {name: 'docker.io/library/' + name + '@sha256:' + 'a' * 64 for name in platform.IMAGES}, 'sources': {name: {'url': f'https://github.com/shizuha-labs/{name}.git', 'commit': 'a' * 40} for name in platform.REQUIRED}}

    def test_public_sources_reject_credentials_and_moving_refs(self):
        for field, bad in [('url', 'https://secret@github.com/shizuha-labs/wiki.git'), ('url', 'https://origin.example/wiki.git'), ('commit', 'publish')]:
            lock = self.lock()
            lock['sources']['wiki'][field] = bad
            with self.assertRaises(ValueError):
                platform.validate_lock(lock)
        platform.validate_lock(self.lock())

    def test_prepare_does_not_overwrite_secrets_and_orders_migrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'install'
            platform.prepare(self.lock(), out, 'http://localhost:8080', 8080)
            secret = (out / 'secrets.env').read_text()
            self.assertEqual((out / 'secrets.env').stat().st_mode & 0o777, 0o600)
            values = [line.split('=', 1)[1] for line in secret.splitlines()]
            self.assertEqual(len(values), len(set(values)))
            with self.assertRaises(FileExistsError):
                platform.prepare(self.lock(), out, 'http://localhost:8080', 8080)
            self.assertEqual((out / 'secrets.env').read_text(), secret)
            config = json.loads((out / 'compose.json').read_text())
            for name in platform.SERVICES:
                service = config['services'][name]
                self.assertEqual(service['environment']['SHIZUHA_REVOCATION_STATUS_URL'], 'http://id:8001/api/oauth/revocation-status')
                self.assertEqual(service['environment']['SHIZUHA_SERVICE_NAME'], name)
                self.assertEqual(service['environment']['SHIZUHA_OAUTH_JWKS_URL'], 'http://id:8001/shizuha-id/.well-known/jwks.json')
                self.assertEqual(service['environment']['SHIZUHA_EXPECTED_ISSUER'], 'http://localhost:8080/id')
                self.assertEqual(service['entrypoint'], [])
                self.assertEqual(service['depends_on'][name + '-migrate']['condition'], 'service_completed_successfully')
                self.assertNotIn('ports', service)
                self.assertIn('migrate', ' '.join(config['services'][name + '-migrate']['command']))
                self.assertEqual(config['services'][name + '-migrate']['restart'], 'no')
            self.assertEqual(config['services']['mail']['depends_on']['mail-server-init']['condition'], 'service_completed_successfully')
            self.assertEqual(config['services']['gateway']['ports'], ['127.0.0.1:8080:80'])
            self.assertIn('atlas', config['services']['gateway']['depends_on'])
            # Atlas selects legacy HS256 exclusively when this variable exists;
            # current ID emits RS256, so this install must use its JWKS path.
            self.assertNotIn('ATLAS_JWT_SECRET', config['services']['atlas']['environment'])
            self.assertIn('ATLAS_JWKS_URL', config['services']['atlas']['environment'])
            self.assertEqual(config['services']['notes']['environment']['AWS_S3_PUBLIC_ENDPOINT_URL'], 'http://localhost:8333')
            self.assertEqual(config['services']['notes']['environment']['AWS_S3_ENDPOINT_URL'], 'http://object-store:8333')
            self.assertNotIn('admin@example.com', ''.join(p.read_text() for p in out.iterdir() if p.is_file()))

    def test_privileged_credentials_reach_only_their_actual_consumers(self):
        services = platform.compose_config('http://localhost:8080', 8080, self.lock()['images'])['services']
        for name in platform.SERVICES:
            env = services[name]['environment']
            self.assertEqual('STALWART_ADMIN_PASSWORD' in env, name == 'mail')
            self.assertEqual('STALWART_PROVISIONING_PASSWORD' in env, name == 'mail')
            self.assertEqual('ID_TO_MAIL_LIFECYCLE_SECRET' in env, name in {'id', 'mail'})
            self.assertEqual('ID_TO_PULSE_LIFECYCLE_SECRET' in env, name in {'id', 'pulse'})
            self.assertEqual('SHIZUHA_MAIL_OAUTH_CLIENT_ID' in env, name in {'id', 'mail'})
            self.assertEqual('CONNECT_SECURITY_EVENT_ORIGIN_SECRET' in env, name in {'id', 'admin', 'wiki', 'connect'})
        self.assertNotIn('env_file', services['postgres'])
        self.assertEqual(set(services['postgres']['environment']),
                         {'POSTGRES_PASSWORD'} | {'DB_PASSWORD_' + name.upper() for name in platform.SERVICES})
        self.assertEqual(set(services['mail-server-init']['environment']), {
            'STALWART_ADMIN_PASSWORD', 'STALWART_ADMIN_URL', 'MAIL_DOMAIN',
            'SHIZUHA_OAUTH_ISSUER', 'SHIZUHA_MAIL_OAUTH_CLIENT_ID'})

    def test_anonymous_git_drops_ambient_auth_and_config(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'private', 'GIT_CONFIG_COUNT': '1'}):
            with patch.object(platform.subprocess, 'run') as run:
                platform.anonymous_git('ls-remote', 'https://github.com/shizuha-labs/example.git')
        env = run.call_args.kwargs['env']
        self.assertNotIn('GH_TOKEN', env)
        self.assertNotIn('GIT_CONFIG_COUNT', env)
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertIn('credential.helper=', run.call_args.args[0])

    def test_public_origin_rejects_path_and_credentials(self):
        for origin in ['https://user:password@example.com', 'https://example.com/app', 'file:///tmp/app']:
            with self.assertRaises(ValueError):
                platform.compose_config(origin, 8080, self.lock()['images'])


if __name__ == '__main__':
    unittest.main()
