"""PUT and GET against an S3-compatible bucket. R2 in production.

Two verbs, one bucket, static credentials. Anything more than that belongs in a
library, and the moment this file needs multipart or presigning is the moment to
revisit D10 of the spec.
"""

import re
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from screener.blobs.config import BlobConfig
from screener.blobs.errors import BlobWriteFailed
from screener.blobs.paths import check
from screener.blobs.sigv4 import UNSIGNED, authorization

TIMEOUT = 30.0

# Lowercase letters, digits, dots and hyphens, starting and ending alphanumeric.
# Dots are allowed because this endpoint may be S3 or MinIO rather than R2;
# underscores and capitals are rejected by all three.
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _why(response: httpx.Response) -> str:
    """The store's own words for a refusal, which the status code alone withholds.

    S3 and R2 both answer with an XML `Code`. Without it a misspelt bucket and a
    malformed request are both "returned 400", and the difference is the whole
    diagnosis.
    """
    body = response.text
    if "<Code>" in body:
        return f"{response.status_code} {body.split('<Code>')[1].split('</Code>')[0]}"
    return f"{response.status_code} {body[:200].strip()}".rstrip()


class S3Store:
    def __init__(
        self,
        config: BlobConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("BLOB_S3_ENDPOINT", config.endpoint),
                ("BLOB_S3_BUCKET", config.bucket),
                ("BLOB_S3_ACCESS_KEY_ID", config.access_key),
                ("BLOB_S3_SECRET_ACCESS_KEY", config.secret_key),
            )
            if not value
        ]
        if missing:
            # A 403 from a missing key is indistinguishable from a 403 from clock
            # skew, so refuse to start rather than fail at the first payload.
            raise RuntimeError(f"BLOB_BACKEND=s3 needs {', '.join(missing)}")
        if not BUCKET.match(config.bucket or ""):
            # Knowable here, and otherwise only learned when the first payload is
            # rejected, by which time whatever produced it has usually moved on.
            raise RuntimeError(
                f"BLOB_S3_BUCKET={config.bucket!r} is not a valid bucket name: "
                "lowercase letters, digits, dots and hyphens only"
            )
        self._config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport)

    def _url(self, path: str) -> str:
        return f"{self._config.endpoint}/{self._config.bucket}/{path}"

    def _signed(self, method: str, path: str) -> tuple[str, dict[str, str]]:
        url = self._url(check(path))
        when = self._now()
        host = httpx.URL(url).host
        headers = {
            "host": host,
            "x-amz-content-sha256": UNSIGNED,
            "x-amz-date": when.strftime("%Y%m%dT%H%M%SZ"),
        }
        headers["authorization"] = authorization(
            method,
            url,
            headers,
            UNSIGNED,
            access_key=self._config.access_key or "",
            secret_key=self._config.secret_key or "",
            region=self._config.region,
            service="s3",
            now=when,
        )
        return url, headers

    def put(self, path: str, data: bytes) -> None:
        url, headers = self._signed("PUT", path)
        try:
            response = self._client.put(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise BlobWriteFailed(f"PUT {path} failed: {exc}") from exc
        if response.status_code >= 300:
            raise BlobWriteFailed(f"PUT {path} returned {_why(response)}")

    def get(self, path: str) -> bytes:
        url, headers = self._signed("GET", path)
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise BlobWriteFailed(f"GET {path} failed: {exc}") from exc
        if response.status_code >= 300:
            raise BlobWriteFailed(f"GET {path} returned {_why(response)}")
        return response.content
