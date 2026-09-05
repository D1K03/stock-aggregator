"""AWS Signature Version 4, for exactly two verbs against one bucket.

Hand-rolled rather than pulling boto3, which would add five packages and a
third HTTP stack. The narrow case is what makes that defensible: static
credentials, no session token, no assume-role, no presigning, no multipart, and
an official conformance vector to implement against.

Three ways this goes wrong, all cheap to avoid and expensive to debug:

- **Three different hashes are in play.** `content_hash` in the ingest trail is
  the sha256 of the *uncompressed* response (schema D4). The blob is the
  *gzipped* bytes. `x-amz-content-sha256` is a third thing, and `S3Store` sends
  `UNSIGNED-PAYLOAD` for it. Passing the D4 hash there is a signature error that
  presents as a credentials problem.
- **Canonical URI encoding.** Not implemented. `screener.blobs.paths.check`
  guarantees the path needs none, which is a smaller thing to get right.
- **Clock skew returns 403, not a clear error.** On a VPS with drifting NTP this
  is the failure that reads as bad credentials. If signing suddenly fails
  everywhere, check the clock before the keys.
"""

import hashlib
import hmac
import urllib.parse
from datetime import datetime

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED = "UNSIGNED-PAYLOAD"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def canonical_request(
    method: str, url: str, headers: dict[str, str], payload_hash: str
) -> str:
    """Method, path, query, canonical headers, signed headers, payload hash."""
    parts = urllib.parse.urlsplit(url)
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed = ";".join(names)
    # The path is used verbatim: `paths.check` has already guaranteed it
    # contains nothing that would need percent-encoding.
    return "\n".join(
        (
            method,
            parts.path or "/",
            parts.query,
            canonical_headers,
            signed,
            payload_hash,
        )
    )


def _scope(when: datetime, region: str, service: str) -> str:
    return f"{when.strftime('%Y%m%d')}/{region}/{service}/aws4_request"


def string_to_sign(
    request: str, *, when: datetime, region: str, service: str
) -> str:
    return "\n".join(
        (
            ALGORITHM,
            when.strftime("%Y%m%dT%H%M%SZ"),
            _scope(when, region, service),
            _sha256(request.encode()),
        )
    )


def signing_key(
    secret_key: str, *, when: datetime, region: str, service: str
) -> bytes:
    key = _hmac(f"AWS4{secret_key}".encode(), when.strftime("%Y%m%d"))
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def authorization(
    method: str,
    url: str,
    headers: dict[str, str],
    payload_hash: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime,
) -> str:
    request = canonical_request(method, url, headers, payload_hash)
    to_sign = string_to_sign(request, when=now, region=region, service=service)
    key = signing_key(secret_key, when=now, region=region, service=service)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed = ";".join(sorted(name.lower() for name in headers))
    return (
        f"{ALGORITHM} "
        f"Credential={access_key}/{_scope(now, region, service)}, "
        f"SignedHeaders={signed}, "
        f"Signature={signature}"
    )
