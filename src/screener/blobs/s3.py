"""PUT and GET against an S3-compatible bucket. R2 in production.

Two verbs, one bucket, static credentials. Anything more than that belongs in a
library, and the moment this file needs multipart or presigning is the moment to
revisit D10 of the spec.
"""

from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from screener.blobs.config import BlobConfig
from screener.blobs.errors import BlobWriteFailed
from screener.blobs.paths import check
from screener.blobs.sigv4 import UNSIGNED, authorization

TIMEOUT = 30.0


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
            raise BlobWriteFailed(f"PUT {path} returned {response.status_code}")

    def get(self, path: str) -> bytes:
        url, headers = self._signed("GET", path)
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise BlobWriteFailed(f"GET {path} failed: {exc}") from exc
        if response.status_code >= 300:
            raise BlobWriteFailed(f"GET {path} returned {response.status_code}")
        return response.content
