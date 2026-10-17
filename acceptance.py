#!/usr/bin/env python3
"""Authenticated acceptance against an isolated installation created by CI."""
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

BASE = os.environ.get('SHIZUHA_ACCEPTANCE_URL', 'http://shizuha.localhost:8080').rstrip('/')
INSTALL = Path(os.environ['SHIZUHA_INSTALLATION_DIR'])
COMPOSE = ['docker', 'compose', '--project-name', os.environ['SHIZUHA_COMPOSE_PROJECT'],
           '--env-file', str(INSTALL / 'secrets.env'), '-f', str(INSTALL / 'compose.json')]


def api(method, path, token=None, data=None, org=None, expected=(200, 201)):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if org:
        headers['X-Organization-ID'] = str(org)
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = Request(BASE + path, data=None if data is None else json.dumps(data).encode(), headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    if status not in expected:
        raise RuntimeError(f'{method} {path}: expected {expected}, got {status}: {body[:500].decode(errors="replace")}')
    if not body:
        return None
    return json.loads(body)


def rows(data):
    return data if isinstance(data, list) else data.get('results', data.get('data', []))


def upload(path, token, content, org, fields=None):
    boundary = 'shizuha-' + secrets.token_hex(16)
    prefix = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n' for key, value in (fields or {}).items()).encode()
    body = prefix + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="acceptance.txt"\r\n'
            'Content-Type: text/plain\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    request = Request(BASE + path, data=body, headers={
        'Authorization': 'Bearer ' + token, 'X-Organization-ID': str(org),
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
    })
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('download requires an HTTP(S) URL without embedded credentials')
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


class DownloadRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target_origin = origin(new_url)
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and target_origin != origin(BASE):
            # urllib copies request headers across redirects. A platform route
            # may legitimately redirect to S3; that host only needs its signed
            # URL, never the user's platform bearer or organization context.
            redirected.remove_header('Authorization')
            redirected.remove_header('X-organization-id')
        return redirected


def download(path, token, org):
    target = urljoin(BASE + '/', path)
    target_origin = origin(target)
    headers = {}
    if target_origin == origin(BASE):
        headers = {'Authorization': 'Bearer ' + token, 'X-Organization-ID': str(org)}
    request = Request(target, headers=headers)
    with build_opener(DownloadRedirectHandler()).open(request, timeout=30) as response:
        return response.read()


def main():
    if os.environ.get('SHIZUHA_SELFHOST_CI') != '1':
        raise RuntimeError('refusing to seed acceptance users outside isolated CI')
    password = secrets.token_urlsafe(24)
    import bootstrap_admin
    bootstrap_admin.create(INSTALL, os.environ['SHIZUHA_COMPOSE_PROJECT'],
                           'ossadmin', 'ossadmin@example.org', password)
    bootstrap = f'''
from django.contrib.auth import get_user_model
from identity.models import UserProfile
from identity.lifecycle import transition_user
User = get_user_model()
for name, password in [('ossowner', {password!r}), ('ossoutsider', {password!r}), ('testuser', {password!r})]:
    if User.objects.filter(username=name).exists():
        raise RuntimeError('acceptance requires an empty installation')
    user = User.objects.create_user(username=name, email=name + '@example.org', password=password, is_active=True)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.lifecycle_state != 'active':
        transition_user(user, 'active', reason='Isolated open-source installation acceptance')
print('Created isolated acceptance users')
'''
    subprocess.run(COMPOSE + ['exec', '-T', 'id', 'python', 'manage.py', 'shell'], input=bootstrap, text=True, check=True)
    def login(name, secret=password):
        return api('POST', '/id/api/auth/login/', data={'username': name, 'password': secret})['tokens']['access']
    administrator = login('ossadmin')
    owner = login('ossowner')
    outsider = login('ossoutsider')
    # Ordinary accounts start with least-privilege service access. Exercise the
    # same audited administrator grant endpoint used by real installations,
    # while keeping all tenant-isolation principals non-staff.
    required_services = {'hive', 'pulse', 'connect', 'wiki', 'atlas', 'notes', 'drive', 'mail'}
    for name, token in [('ossowner', owner), ('ossoutsider', outsider), ('testuser', login('testuser'))]:
        services = required_services | ({'admin'} if name == 'ossowner' else set())
        user = api('GET', '/id/api/auth/user/', token)
        granted = api('POST', f'/id/api/auth/admin/users/{user["id"]}/services/',
                      administrator, {'service_slugs': sorted(services),
                                      'action': 'grant', 'reason': 'Isolated installation acceptance'})
        assert services <= set(granted['enabled_services'])
    owner, outsider = login('ossowner'), login('ossoutsider')
    print('PASS administrator bootstrap and audited non-staff service grants', flush=True)
    api('POST', '/admin/api/organizations/', owner, {'name': 'Acceptance Organization', 'slug': 'oss-acceptance'})
    org = api('GET', '/admin/api/organizations/oss-acceptance/', owner)['id']
    api('POST', '/admin/api/organizations/', outsider, {'name': 'Other Organization', 'slug': 'oss-other'})
    other_org = api('GET', '/admin/api/organizations/oss-other/', outsider)['id']
    assert org != other_org
    api('GET', '/admin/api/me/', owner)
    api('GET', '/admin/api/me/', outsider, expected=(403,))
    api('GET', '/admin/api/organizations/', outsider)
    owner, outsider = login('ossowner'), login('ossoutsider')
    print('PASS ID login and independent Admin organizations', flush=True)
    space = api('POST', '/wiki/api/spaces/', owner, {'key': 'OSS', 'name': 'Acceptance', 'space_type': 'team', 'visibility': 'private'}, org)
    page = api('POST', '/wiki/api/pages/', owner, {'space': space['id'], 'title': 'Persistent acceptance page', 'content_text': 'fresh installation'}, org)
    page_path = f'/wiki/api/pages/{page["id"]}/'
    assert api('GET', page_path, owner, org=org)['title'] == 'Persistent acceptance page'
    api('GET', page_path, outsider, org=other_org, expected=(403, 404))
    api('GET', page_path, expected=(401, 403, 404))
    print('PASS Wiki create/read and tenant/anonymous isolation', flush=True)
    atlas = api('POST', '/atlas/api/maps', owner, {'organization_id': org, 'title': 'Acceptance map'})
    atlas_id = atlas.get('id', atlas.get('map', {}).get('id'))
    if not atlas_id:
        raise RuntimeError('Atlas creation did not return its map id')
    atlas_path = f'/atlas/api/maps/{atlas_id}'
    api('GET', atlas_path, owner)
    api('GET', atlas_path, outsider, expected=(403, 404))
    print('PASS Atlas create/read and tenant isolation', flush=True)
    api('POST', '/pulse/api/projects/', owner, {'organization': org, 'name': 'Acceptance project', 'key': 'OSS'}, org)
    projects = rows(api('GET', f'/pulse/api/projects/?organization_id={org}', owner, org=org))
    project = next(value for value in projects if value['key'] == 'OSS')
    task = api('POST', '/pulse/api/tasks/', owner, {'title': 'Acceptance task', 'project': project['id'], 'organization': org}, org)
    task_path = f'/pulse/api/tasks/{task["id"]}/'
    api('GET', task_path, owner, org=org)
    api('GET', task_path, outsider, org=other_org, expected=(403, 404))
    attachment_bytes = b'Pulse object storage acceptance\n'
    attachment = upload('/pulse/api/attachments/', owner, attachment_bytes, org, {'item': task['id']})
    attachment_url = attachment.get('download_url') or attachment.get('attachment', {}).get('download_url')
    if not attachment_url:
        raise RuntimeError('Pulse attachment creation did not expose its safe download URL')
    assert download(attachment_url, owner, org) == attachment_bytes
    print('PASS Pulse create/read, attachment bytes and tenant isolation', flush=True)
    notebook = api('POST', '/notes/api/notebooks/', owner, {'name': 'Acceptance notebook'}, org)
    note = api('POST', '/notes/api/notes/', owner, {'notebook': notebook['id'], 'title': 'Acceptance note', 'content_text': 'persistent note'}, org)
    note_path = f'/notes/api/notes/{note["id"]}/'
    api('GET', note_path, owner, org=org)
    api('GET', note_path, outsider, org=other_org, expected=(403, 404))
    note_bytes = b'Notes externally signed attachment acceptance\n'
    note_attachment = upload('/notes/api/attachments/', owner, note_bytes, org, {'note': note['id']})
    assert download(note_attachment['file_url'], owner, org) == note_bytes
    from object_storage_acceptance import require_private_object
    require_private_object(note_attachment['file_url'])
    attachment_path = f'/notes/api/attachments/{note_attachment["id"]}/file/'
    assert download(attachment_path, owner, org) == note_bytes
    api('GET', attachment_path, outsider, org=other_org, expected=(403, 404))
    print('PASS Notes create/read, external signed bytes and attachment tenant isolation', flush=True)
    conversation = api('POST', '/connect/api/conversations/', owner, {
        'conversation_type': 'group', 'name': 'Acceptance conversation', 'participant_ids': [],
    }, org)
    messages_path = '/connect/api/conversations/' + conversation['id'] + '/messages/'
    message = api('POST', messages_path, owner, {'message_type': 'text', 'content': 'Persistent acceptance message'}, org)
    assert any(value['content'] == 'Persistent acceptance message' for value in api('GET', messages_path, owner, org=org)['messages'])
    api('GET', messages_path, outsider, org=other_org, expected=(403, 404))
    print('PASS Connect message create/read and participant isolation', flush=True)
    file_bytes = b'Fresh source installation attachment acceptance\n'
    uploaded = upload('/drive/api/files/', owner, file_bytes, org)
    file_path = '/drive/api/files/' + str(uploaded['id']) + '/download/'
    assert download(file_path, owner, org) == file_bytes
    api('GET', file_path, outsider, org=other_org, expected=(403, 404))
    print('PASS Drive upload/download byte equality and account isolation', flush=True)
    from acceptance_gates import Gates
    gates = Gates(INSTALL / 'gates.json')
    import home_acceptance
    gates.run('home', lambda: home_acceptance.verify(api, owner, outsider, org, other_org))
    def restart_persistence():
        # Restart only this CI project's applications; named volumes must preserve
        # the created data. This is never called against the production daemon.
        subprocess.run(COMPOSE + ['restart', 'wiki', 'atlas', 'pulse', 'notes', 'drive', 'connect'], check=True)
        for attempt in range(60):
            try:
                for path in (page_path, atlas_path, task_path, note_path):
                    api('GET', path, owner, org=org)
                assert download(file_path, owner, org) == file_bytes
                assert any(value['content'] == 'Persistent acceptance message' for value in api('GET', messages_path, owner, org=org)['messages'])
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(2)
        print('PASS created objects survive application restart', flush=True)
    gates.run('restart-persistence', restart_persistence)
    # The existing Notes suite drives the real login page and all note flows.
    browser_token = login('testuser')
    api('POST', '/admin/api/organizations/', browser_token,
        {'name': 'Browser Acceptance', 'slug': 'oss-browser'})
    browser_org = api('GET', '/admin/api/organizations/oss-browser/', browser_token)['id']
    frontend = INSTALL / 'sources' / 'notes' / 'frontend'
    env = os.environ.copy()
    env.update(BASE_URL=BASE, API_URL=BASE + '/notes/api/', ID_API_URL=BASE + '/id/api/',
               TEST_ORGANIZATION_ID=str(browser_org), TEST_USER_PASSWORD=password, CI='true',
               SHIZUHA_BROWSER_EVIDENCE_DIR=str(INSTALL / 'browser-evidence'))
    dependencies_ready = gates.run('browser-dependencies', lambda: subprocess.run(
        ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=frontend, env=env, check=True))
    wiki_env = dict(env, SHIZUHA_BROWSER_PASSWORD=password,
                    SHIZUHA_WIKI_PAGE_SLUG=page['slug'], SHIZUHA_WIKI_ORG=str(org))
    if dependencies_ready:
        gates.run('application-browsers', lambda: subprocess.run(
            ['node', str(Path(__file__).with_name('browser-acceptance.mjs'))], env=wiki_env, check=True))
        gates.run('atlas-editor', lambda: subprocess.run(
            ['node', str(Path(__file__).with_name('atlas-browser-acceptance.mjs'))], env=wiki_env, check=True))
        def notes_browser():
            subprocess.run(['npm', 'test'], cwd=frontend, env=env, check=True)
            subprocess.run(['node', str(Path(__file__).with_name('notes-browser-acceptance.mjs'))],
                           env=env, check=True)
        gates.run('notes-browser', notes_browser)
    else:
        for name in ('application-browsers', 'atlas-editor', 'notes-browser'):
            gates.blocked(name, 'browser-dependencies')
    def mail_journey():
        # Check authenticated discovery before the Mail and runtime journeys.
        for service, path in [('connect', '/connect/api/conversations/'), ('drive', '/drive/api/files/'), ('mail', '/mail/api/accounts/'), ('hive', '/hive/api/v1/fleet/agent-cards/?owner_only=true')]:
            api('GET', path, owner, org=org)
            print(f'PASS {service} authenticated API', flush=True)
        # Activation delivery provisions a mailbox through the real signed ID
        # lifecycle path. Never insert a MailAccount directly in this test.
        def mailbox(token):
            for attempt in range(60):
                accounts = rows(api('GET', '/mail/api/accounts/', token))
                managed = [value for value in accounts if value.get('settings', {}).get('managed_identity')]
                if managed:
                    return managed[0]
                time.sleep(2)
            raise RuntimeError('ID activation did not provision a managed mailbox')
        sender, recipient = mailbox(owner), mailbox(outsider)
        api('POST', '/mail/api/accounts/sync/', outsider, {'account_id': recipient['id']})
        subject = 'Installation delivery ' + secrets.token_hex(8)
        sent = api('POST', '/mail/api/send/', owner, {
            'account_id': sender['id'], 'to_addresses': [recipient['email_address']],
            'subject': subject, 'body_text': 'Real local delivery through managed JMAP',
            'client_message_id': __import__('uuid').uuid4().hex,
        }, expected=(200, 201, 202))
        for attempt in range(60):
            api('POST', '/mail/api/accounts/sync/', outsider, {'account_id': recipient['id']})
            messages = rows(api('GET', '/mail/api/messages/?folder_role=inbox&account_id=' + recipient['id'], outsider))
            delivered = next((item for item in messages if item.get('subject') == subject), None)
            if delivered:
                break
            time.sleep(2)
        else:
            raise RuntimeError('Managed Mail send accepted but recipient did not receive the message')
        api('GET', '/mail/api/messages/' + str(delivered['id']) + '/?account_id=' + recipient['id'], owner, expected=(403, 404))
        print('PASS Mail managed provisioning, delegated auth, send/receive and account isolation', flush=True)
    gates.run('mail-delivery', mail_journey)
    import revocation_acceptance
    gates.run('session-revocation', lambda: revocation_acceptance.verify(
        api, BASE, 'testuser', password, browser_org))
    def runtime_journey():
        # Ordinary application journeys do not depend on the dedicated agent
        # cluster. Validate them first, then require real runtime setup and launch.
        with (INSTALL / 'runtime-setup.log').open('w') as runtime_log:
            subprocess.run(['python3', str(INSTALL / 'runtime_setup.py'), str(INSTALL),
                            '--project', os.environ['SHIZUHA_COMPOSE_PROJECT']],
                           stdout=runtime_log, stderr=subprocess.STDOUT, check=True)
        # Escalate only this isolated fixture after all tenant-isolation checks.
        subprocess.run(COMPOSE + ['exec', '-T', 'id', 'python', 'manage.py', 'shell', '-c',
            "from django.contrib.auth import get_user_model; get_user_model().objects.filter(username='ossowner').update(is_staff=True)"], check=True)
        import runtime_acceptance
        runtime_acceptance.verify(INSTALL, COMPOSE, api, login('ossowner'), outsider)

    gates.run('runtime-agent', runtime_journey)
    gates.finish()


if __name__ == '__main__':
    main()
