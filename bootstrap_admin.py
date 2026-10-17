#!/usr/bin/env python3
"""Create the first local administrator through ID's normal activation path."""
import argparse
import getpass
from pathlib import Path
import subprocess


def create(installation, project, username, email, password):
    installation = Path(installation).resolve()
    if not (installation / 'compose.json').is_file():
        raise ValueError('installation/compose.json is required')
    # Send credentials through stdin, never command arguments, environment,
    # files, or output. ID applies its configured password validators.
    script = f'''
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.validators import validate_email
from django.db import transaction
from identity.lifecycle import transition_user
User = get_user_model()
username, email, password = {username!r}, {email!r}, {password!r}
validate_email(email)
user = User(username=username, email=email)
user.full_clean(exclude=['password'])
validate_password(password, user)
with transaction.atomic():
    if User.objects.filter(is_superuser=True).exists():
        raise RuntimeError('A local administrator already exists; use ID account management')
    user = User.objects.create_superuser(username=username, email=email, password=password, is_active=False)
    transition_user(user, 'active', reason='Local installation administrator bootstrap')
print('Local administrator created and activated')
'''
    subprocess.run(['docker', 'compose', '--project-name', project,
                    '--env-file', str(installation / 'secrets.env'),
                    '-f', str(installation / 'compose.json'), 'exec', '-T',
                    'id', 'python', 'manage.py', 'shell'],
                   input=script, text=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installation', type=Path)
    parser.add_argument('--project', default='shizuha-selfhost')
    args = parser.parse_args()
    username = input('Administrator username: ').strip()
    email = input('Administrator email: ').strip()
    password = getpass.getpass('Password: ')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords did not match; no account was created')
    create(args.installation, args.project, username, email, password)


if __name__ == '__main__':
    main()
