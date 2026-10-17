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
