"""Prove a signed object URL does not imply anonymous bucket access."""
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def require_private_object(signed_url):
    parsed = urlsplit(signed_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Object acceptance requires an HTTP(S) URL without embedded credentials')
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    signatures = [value for key, value in pairs if key == 'X-Amz-Signature']
    if len(signatures) != 1 or not re.fullmatch(r'[0-9a-f]{64}', signatures[0]):
        raise ValueError('Expected one S3 v4 signature')
    altered = ('0' if signatures[0][0] != '0' else '1') + signatures[0][1:]
    tampered = [(key, altered if key == 'X-Amz-Signature' else value) for key, value in pairs]
    for label, query in [('unsigned', ''), ('altered-signature', urlencode(tampered))]:
        target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ''))
        # No platform token, org header or cookie jar belongs on object storage.
        try:
            with urlopen(Request(target), timeout=30) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
            error.close()
        if status != 403:
            raise RuntimeError(f'{label} object request must return 403, got {status}')


def require_private_notes_attachment(compose, attachment_id, note_id, organization_id, expected_bytes):
    """Check this CI attachment's backing object without exposing its signed URL."""
    import json
    import subprocess
    import uuid
    attachment_id, note_id = str(uuid.UUID(str(attachment_id))), str(uuid.UUID(str(note_id)))
    if isinstance(organization_id, bool) or not str(organization_id).isdigit() or int(organization_id) <= 0:
        raise ValueError('Notes object proof requires an organization identity')
    organization_id = int(organization_id)
    code = '''import os, sys, json
os.environ['DJANGO_SETTINGS_MODULE'] = 'notesmanager.settings'
import django
django.setup()
from notes.models import Attachment
attachment = Attachment.objects.get(pk=sys.argv[1], note_id=sys.argv[2], note__organization_id=int(sys.argv[3]))
print(json.dumps({'attachment_id': str(attachment.pk), 'note_id': str(attachment.note_id), 'url': attachment.file.storage.url(attachment.file.name)}))
'''
    result = subprocess.run(list(compose) + ['exec', '-T', 'notes', 'python', '-c', code,
                            attachment_id, note_id, str(organization_id)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    try:
        if result.returncode or len(result.stdout) > 8192:
            raise ValueError('unavailable object proof')
        value = json.loads(result.stdout)
        if (set(value) != {'attachment_id', 'note_id', 'url'}
                or value['attachment_id'] != attachment_id or value['note_id'] != note_id
                or not isinstance(value['url'], str)):
            raise ValueError('object identity mismatch')
        require_private_object(value['url'])
        # No platform credentials, cookies, or organization headers go to S3.
        with urlopen(Request(value['url']), timeout=30) as response:
            if response.read(len(expected_bytes) + 1) != expected_bytes:
                raise ValueError('object bytes mismatch')
    except Exception:
        # Neither subprocess output nor signed URLs belong in CI diagnostics.
        raise RuntimeError('Notes backing object privacy proof failed') from None
