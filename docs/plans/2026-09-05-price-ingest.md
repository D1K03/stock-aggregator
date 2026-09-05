# Price Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python -m screener.ingest prices` fills `price_daily`, `corporate_action` and the `ingest_observation` trail from Yahoo, backfilling to 2020 on first sight of a security and fetching only what is missing thereafter.

**Architecture:** A `screener.ingest` package shaped like `screener.universe` — argparse entry point, one subcommand per verb. Parsing is a pure function tested without a socket or a database; the loader is tested against a real Postgres; the network is `httpx.MockTransport` throughout. A new `screener.blobs` package provides the payload store the schema has always assumed, with a `local` implementation for tests and an R2 implementation for the box.

**Tech Stack:** Python 3.11+, httpx (via `screener.fetch.LanePool`), psycopg 3, pytest, Postgres 16. No new runtime dependencies.

**Spec:** `docs/specs/2026-09-05-price-ingest.md`

## Global Constraints

- **No new runtime dependencies.** `pyproject.toml` declares `httpx`, `psycopg[binary]` and `discord.py`. SigV4 is hand-rolled on `httpx`; `boto3` is not permitted (spec D10).
- **No test may reach the network.** Pass `transport=httpx.MockTransport(...)` everywhere, as `tests/test_fetch_lanes.py` and `tests/test_universe_yahoo.py` do.
- **`screener.blobs` uses the `local` implementation in every test.**
- Entry point mirrors `screener.boot` and `screener.universe`: `python -m screener.ingest <command>`, argparse with a positional `command` and a `choices` tuple.
- Database access uses `screener.config.settings().database_url` and psycopg 3. Credentials for anything else come from that subsystem's own config module, never `screener.config.Settings`.
- All SQL identifiers lowercase; `text` never `varchar(n)`; `timestamptz` never `timestamp`; `numeric` never float. Money and prices are `Decimal`, never `float`.
- **No migrations.** Every table this writes to already exists (migration 005).
- `pyright` must report zero errors. SQL assembled at runtime is rejected by design — build any dynamic SQL with `psycopg.sql` composition.
- Yahoo `/v8/finance/chart` needs **no crumb**. Do not import or extend `screener.universe.sources.yahoo` (spec §3).
- **Settling window is 7 calendar days.** Bars inside it may be upserted; bars outside it are insert-if-absent.
- **Backfill start is 2020-01-01.** Migration 008 pre-creates partitions from there.

---

## File structure

| File | Responsibility |
|---|---|
| `src/screener/blobs/__init__.py` | Public surface: `BlobStore`, `store()`, `BlobWriteFailed` |
| `src/screener/blobs/config.py` | `BlobConfig.from_env()` — which implementation, and its credentials |
| `src/screener/blobs/paths.py` | `blob_path()` and the safe-character assertion |
| `src/screener/blobs/local.py` | `LocalStore` — a directory. What tests use |
| `src/screener/blobs/sigv4.py` | Canonical request, string-to-sign, signing key, Authorization header |
| `src/screener/blobs/s3.py` | `S3Store` — PUT and GET over httpx against R2 |
| `src/screener/ingest/__init__.py` | Public surface: `Bar`, `Action`, `parse`, `windows`, `run_prices`, `run_sweep` |
| `src/screener/ingest/__main__.py` | `raise SystemExit(main())` |
| `src/screener/ingest/cli.py` | argparse `main(argv)`, dispatch to prices/sweep |
| `src/screener/ingest/chart.py` | `ChartClient` over a `LanePool`; returns raw bytes or None |
| `src/screener/ingest/parse.py` | pure `parse(payload) -> (bars, actions)` |
| `src/screener/ingest/window.py` | pure-ish `windows(conn, ids, today)` — per-security fetch start |
| `src/screener/ingest/load.py` | observation row, the three write paths, the one mutating function |
| `src/screener/ingest/run.py` | `ingest_run` lifecycle, the per-security loop |
| `src/screener/ingest/sweep.py` | detect-only comparison and its count-by-field summary |

---

### Task 1: `screener.blobs` — interface, paths, local store

**Files:**
- Create: `src/screener/blobs/__init__.py`, `config.py`, `paths.py`, `local.py`
- Test: `tests/test_blobs.py`

**Interfaces:**
- Consumes: `screener.config.env`
- Produces: `BlobStore` protocol with `put(path: str, data: bytes) -> None` and `get(path: str) -> bytes`; `BlobWriteFailed(RuntimeError)`; `blob_path(source: str, endpoint: str, day: date, security_id: int) -> str`; `LocalStore(root: Path)`; `store() -> BlobStore`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_blobs.py
from datetime import date
from pathlib import Path

import pytest

from screener.blobs import BlobWriteFailed, LocalStore, blob_path


def test_blob_path_is_the_shape_the_schema_spec_fixes():
    assert blob_path("yahoo", "chart", date(2026, 9, 5), 1234) == (
        "yahoo/chart/2026-09-05/1234.json.gz"
    )


def test_a_local_store_round_trips_bytes(tmp_path):
    store = LocalStore(tmp_path)
    store.put("yahoo/chart/2026-09-05/1.json.gz", b"payload")
    assert store.get("yahoo/chart/2026-09-05/1.json.gz") == b"payload"


def test_a_local_store_creates_intermediate_directories(tmp_path):
    LocalStore(tmp_path).put("a/b/c/d.json.gz", b"x")
    assert (tmp_path / "a" / "b" / "c" / "d.json.gz").read_bytes() == b"x"


def test_reading_something_that_was_never_written_raises(tmp_path):
    with pytest.raises(BlobWriteFailed):
        LocalStore(tmp_path).get("nothing/here.json.gz")


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.json.gz",            # traversal
        "/absolute.json.gz",            # absolute
        "with space.json.gz",           # would need percent-encoding
        "unicodé.json.gz",         # would need percent-encoding
        "back\\slash.json.gz",
    ],
)
def test_an_unsafe_path_is_refused_rather_than_encoded(tmp_path, bad):
    # SigV4 canonical-URI encoding is where hand-rolled signers break. Every
    # path this system produces is safe by construction, so assert that rather
    # than implement percent-encoding rules we cannot fully test.
    with pytest.raises(ValueError, match="unsafe blob path"):
        LocalStore(tmp_path).put(bad, b"x")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_blobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.blobs'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/blobs/paths.py
"""Where a payload goes, and the one rule about what a path may contain."""

import re
from datetime import date

# Deliberately narrow. `S3Store` signs the path into a SigV4 canonical URI, and
# canonical-URI percent-encoding is the classic way a hand-rolled signer breaks.
# Every path this project produces is alphanumerics, dots, dashes and slashes,
# so refusing anything else is cheaper and safer than encoding rules that would
# never be exercised.
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def check(path: str) -> str:
    if not SAFE.match(path) or ".." in path:
        raise ValueError(f"unsafe blob path: {path!r}")
    return path


def blob_path(source: str, endpoint: str, day: date, security_id: int) -> str:
    """The layout fixed by docs/specs/2026-09-04-database-schema-design.md."""
    return check(f"{source}/{endpoint}/{day.isoformat()}/{security_id}.json.gz")
```

```python
# src/screener/blobs/local.py
"""A directory. What the test suite uses, and the fallback if R2 ever goes."""

from pathlib import Path

from screener.blobs.paths import check


class BlobWriteFailed(RuntimeError):
    """A payload could not be written or read.

    Fatal for a run rather than for one security: `ingest_observation` asserts
    that `blob_path` exists, so a row must not be written when the object does
    not. A store that is failing is almost always failing systemically.
    """


class LocalStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def put(self, path: str, data: bytes) -> None:
        target = self._root / check(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, path: str) -> bytes:
        target = self._root / check(path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise BlobWriteFailed(f"cannot read {path}: {exc}") from exc
```

```python
# src/screener/blobs/config.py
"""Which store, and its credentials. Owned here, not in screener.config."""

from dataclasses import dataclass, field
from pathlib import Path

from screener.config import env


@dataclass(frozen=True)
class BlobConfig:
    backend: str = "local"
    local_dir: Path = Path("data/blobs")
    endpoint: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = field(default=None, repr=False)
    # R2 wants `auto` in the credential scope. It is not a real AWS region.
    region: str = "auto"

    @classmethod
    def from_env(cls) -> "BlobConfig":
        return cls(
            backend=env.text("BLOB_BACKEND", "local"),
            local_dir=Path(env.text("BLOB_LOCAL_DIR", "data/blobs")),
            endpoint=env.optional("BLOB_S3_ENDPOINT"),
            bucket=env.optional("BLOB_S3_BUCKET"),
            access_key=env.optional("BLOB_S3_ACCESS_KEY_ID"),
            secret_key=env.optional("BLOB_S3_SECRET_ACCESS_KEY"),
            region=env.text("BLOB_S3_REGION", "auto"),
        )
```

```python
# src/screener/blobs/__init__.py
"""The payload store.

`ingest_observation.blob_path` is `not null` and D1 of the schema spec claims
every score traces back to the stored response, so this is evidence rather than
cache. Nothing here prunes.
"""

from pathlib import Path
from typing import Protocol

from screener.blobs.config import BlobConfig
from screener.blobs.local import BlobWriteFailed, LocalStore
from screener.blobs.paths import blob_path, check


class BlobStore(Protocol):
    def put(self, path: str, data: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...


def store(config: BlobConfig | None = None) -> BlobStore:
    config = config or BlobConfig.from_env()
    if config.backend == "local":
        return LocalStore(Path(config.local_dir))
    if config.backend == "s3":
        from screener.blobs.s3 import S3Store

        return S3Store(config)
    raise RuntimeError(f"BLOB_BACKEND must be 'local' or 's3', got {config.backend!r}")


__all__ = [
    "BlobConfig",
    "BlobStore",
    "BlobWriteFailed",
    "LocalStore",
    "blob_path",
    "check",
    "store",
]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_blobs.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Typecheck**

Run: `pyright`
Expected: zero errors

- [ ] **Step 6: Commit**

```bash
git add src/screener/blobs tests/test_blobs.py
git commit -m "Add the payload store interface and its local implementation"
```

---

### Task 2: SigV4, against the published AWS vector

**Files:**
- Create: `src/screener/blobs/sigv4.py`
- Test: `tests/test_sigv4.py`

**Interfaces:**
- Consumes: nothing outside stdlib
- Produces: `authorization(method: str, url: str, headers: dict[str, str], payload_hash: str, *, access_key: str, secret_key: str, region: str, service: str, now: datetime) -> str`, and the intermediate helpers `canonical_request`, `string_to_sign`, `signing_key` (exported so each can be asserted separately)

- [ ] **Step 1: Write the failing test**

The inputs below are AWS's documented "GET Object" SigV4 example. **Verify the expected
signature against AWS's published Signature Version 4 examples page before trusting this
file — if the published value differs, the published value wins and this test changes.**

```python
# tests/test_sigv4.py
from datetime import datetime, timezone

from screener.blobs.sigv4 import (
    authorization,
    canonical_request,
    signing_key,
    string_to_sign,
)

# AWS's documented example. Stable, and the reason hand-rolling is defensible:
# this is implemented against a spec with a conformance vector, not a blog post.
ACCESS = "AKIAIOSFODNN7EXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
WHEN = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
HEADERS = {
    "host": "examplebucket.s3.amazonaws.com",
    "range": "bytes=0-9",
    "x-amz-content-sha256": EMPTY,
    "x-amz-date": "20130524T000000Z",
}


def test_the_canonical_request_matches_the_published_example():
    got = canonical_request(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt", HEADERS, EMPTY
    )
    assert got.splitlines()[0] == "GET"
    assert got.splitlines()[1] == "/test.txt"
    assert got.splitlines()[2] == ""          # no query string
    assert "host:examplebucket.s3.amazonaws.com" in got
    assert got.splitlines()[-2] == (
        "host;range;x-amz-content-sha256;x-amz-date"
    )
    assert got.splitlines()[-1] == EMPTY


def test_the_string_to_sign_carries_the_credential_scope():
    sts = string_to_sign(
        canonical_request(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt", HEADERS, EMPTY
        ),
        when=WHEN,
        region="us-east-1",
        service="s3",
    )
    assert sts.splitlines()[0] == "AWS4-HMAC-SHA256"
    assert sts.splitlines()[1] == "20130524T000000Z"
    assert sts.splitlines()[2] == "20130524/us-east-1/s3/aws4_request"


def test_the_signing_key_is_derived_by_date_region_service_then_request():
    key = signing_key(SECRET, when=WHEN, region="us-east-1", service="s3")
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_the_authorization_header_matches_the_published_signature():
    header = authorization(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        HEADERS,
        EMPTY,
        access_key=ACCESS,
        secret_key=SECRET,
        region="us-east-1",
        service="s3",
        now=WHEN,
    )
    assert header.startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in header
    assert header.endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_sigv4.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.blobs.sigv4'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/blobs/sigv4.py
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_sigv4.py -v`
Expected: PASS, 4 tests.
If `test_the_authorization_header_matches_the_published_signature` fails, **check the expected
hex against AWS's published example before changing any code** — a wrong constant in the test
is more likely than a wrong implementation this early.

- [ ] **Step 5: Commit**

```bash
git add src/screener/blobs/sigv4.py tests/test_sigv4.py
git commit -m "Sign requests with SigV4, verified against the published AWS vector"
```

---

### Task 3: The R2 store

**Files:**
- Create: `src/screener/blobs/s3.py`
- Modify: `tests/test_blobs.py` (append)

**Interfaces:**
- Consumes: `BlobConfig`, `sigv4.authorization`, `paths.check`
- Produces: `S3Store(config, *, transport=None, now=None)` satisfying `BlobStore`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_blobs.py  (append)
from datetime import datetime, timezone

import httpx
import pytest

from screener.blobs import BlobConfig, BlobWriteFailed
from screener.blobs.s3 import S3Store

WHEN = datetime(2026, 9, 5, 2, 0, 0, tzinfo=timezone.utc)


def s3_config():
    return BlobConfig(
        backend="s3",
        endpoint="https://acct.r2.cloudflarestorage.com",
        bucket="screener",
        access_key="key",
        secret_key="secret",
        region="auto",
    )


def recorder(status=200, body=b""):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler), seen


def test_a_put_signs_the_request_and_targets_bucket_and_key():
    transport, seen = recorder()
    S3Store(s3_config(), transport=transport, now=lambda: WHEN).put("a/b.json.gz", b"x")
    assert len(seen) == 1
    assert seen[0].method == "PUT"
    assert str(seen[0].url) == "https://acct.r2.cloudflarestorage.com/screener/a/b.json.gz"
    assert seen[0].headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=key/")
    assert "/auto/s3/aws4_request" in seen[0].headers["authorization"]


def test_a_put_sends_unsigned_payload_not_a_body_hash():
    # The gzipped blob, the D4 content hash and x-amz-content-sha256 are three
    # different things. Sending the wrong one is a signature error that reads as
    # a credentials problem.
    transport, seen = recorder()
    S3Store(s3_config(), transport=transport, now=lambda: WHEN).put("a/b.json.gz", b"x")
    assert seen[0].headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"


def test_a_non_2xx_put_raises_rather_than_returning():
    transport, _ = recorder(status=403)
    with pytest.raises(BlobWriteFailed, match="403"):
        S3Store(s3_config(), transport=transport, now=lambda: WHEN).put("a/b.json.gz", b"x")


def test_a_get_returns_the_body():
    transport, seen = recorder(body=b"payload")
    got = S3Store(s3_config(), transport=transport, now=lambda: WHEN).get("a/b.json.gz")
    assert got == b"payload"
    assert seen[0].method == "GET"


def test_an_unsafe_path_is_refused_before_anything_is_signed():
    transport, seen = recorder()
    with pytest.raises(ValueError, match="unsafe blob path"):
        S3Store(s3_config(), transport=transport, now=lambda: WHEN).put("../x", b"x")
    assert seen == []


def test_a_missing_credential_is_a_clear_error_not_a_403():
    config = BlobConfig(backend="s3", endpoint="https://e", bucket="b")
    with pytest.raises(RuntimeError, match="BLOB_S3_"):
        S3Store(config)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_blobs.py -v -k s3 or put or get`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.blobs.s3'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/blobs/s3.py
"""PUT and GET against an S3-compatible bucket. R2 in production.

Two verbs, one bucket, static credentials. Anything more than that belongs in a
library, and the moment this file needs multipart or presigning is the moment to
revisit D10 of the spec.
"""

from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from screener.blobs.config import BlobConfig
from screener.blobs.local import BlobWriteFailed
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
```

- [ ] **Step 4: Run the whole blobs suite**

Run: `pytest tests/test_blobs.py tests/test_sigv4.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/blobs/s3.py tests/test_blobs.py
git commit -m "Store payloads in R2 over two signed verbs"
```

---

### Task 4: The chart client

**Files:**
- Create: `src/screener/ingest/__init__.py`, `__main__.py`, `chart.py`
- Test: `tests/test_ingest_chart.py`

**Interfaces:**
- Consumes: `screener.fetch.LanePool`, `screener.fetch.Lane`
- Produces: `ChartClient(*, lanes=None, transport=None, sleep=None, backoff=1.0)` with `fetch(symbol: str, start: date, end: date) -> bytes | None`, and module constants `BASE`, `BROWSER`, `TIMEOUT`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_chart.py
from datetime import date

import httpx
import pytest

from screener.fetch import Lane, LanePool
from screener.ingest.chart import ChartClient


def responder(*responses):
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, text="{}")

    return httpx.MockTransport(handler), seen


def pool(transport, names=("one",)):
    return LanePool([Lane(name, transport=transport) for name in names])


def test_a_fetch_asks_for_the_computed_window_and_daily_bars():
    transport, seen = responder(httpx.Response(200, text='{"chart":{}}'))
    client = ChartClient(lanes=pool(transport))
    client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5))
    url = str(seen[0].url)
    assert "/v8/finance/chart/AAPL" in url
    assert "interval=1d" in url
    assert "events=div%2Csplit" in url or "events=div,split" in url
    # period1/period2 rather than range=: the window is computed per security.
    assert "period1=" in url and "period2=" in url


def test_a_fetch_returns_the_raw_bytes_because_they_get_hashed_and_stored():
    body = '{"chart":{"result":[]}}'
    transport, _ = responder(httpx.Response(200, text=body))
    got = ChartClient(lanes=pool(transport)).fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5))
    assert got == body.encode()


def test_a_404_returns_none_rather_than_raising():
    # CWEN-A 404s on both endpoints. One bad symbol must not end a night.
    transport, _ = responder(httpx.Response(404, text="not found"))
    assert ChartClient(lanes=pool(transport)).fetch("CWEN-A", date(2026, 9, 1), date(2026, 9, 5)) is None


def test_a_429_parks_the_lane_and_the_retry_leaves_by_a_different_exit():
    transport, seen = responder(
        httpx.Response(429, text="slow down"),
        httpx.Response(200, text='{"chart":{}}'),
    )
    lanes = pool(transport, names=("one", "two"))
    client = ChartClient(lanes=lanes, sleep=lambda _s: None)
    assert client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5)) is not None
    assert len(seen) == 2


def test_a_transport_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = ChartClient(lanes=pool(httpx.MockTransport(handler)))
    assert client.fetch("AAPL", date(2026, 9, 1), date(2026, 9, 5)) is None


def test_an_unconfigured_environment_gives_one_direct_lane(monkeypatch):
    monkeypatch.delenv("BRIGHTDATA_PROXY_IPS", raising=False)
    transport, _ = responder(httpx.Response(200, text="{}"))
    with ChartClient(transport=transport) as client:
        assert len(client.lanes) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_chart.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/chart.py
"""Daily bars from Yahoo, over a lane.

`/v8/finance/chart` needs no crumb — only a User-Agent — so this shares nothing
with `screener.universe.sources.yahoo` but `LanePool`. That is deliberate:
promoting the universe client would share the cookie and crumb machinery this
does not use. When fundamentals land and need the crumbed path, that is the
moment the promotion earns itself.
"""

import time
from collections.abc import Callable
from datetime import date, datetime, time as clock, timezone

import httpx

from screener.fetch import Lane, LanePool

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
TIMEOUT = 25.0
_SPARE_ATTEMPTS = 3

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _epoch(day: date) -> int:
    return int(datetime.combine(day, clock.min, tzinfo=timezone.utc).timestamp())


class ChartClient:
    def __init__(
        self,
        *,
        lanes: LanePool | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._owned = lanes is None
        self.lanes = lanes or LanePool.from_env(
            headers=BROWSER,
            timeout=TIMEOUT,
            transport=transport,
            fallback_to_direct=True,
        )
        self._sleep = sleep or time.sleep
        self._backoff = backoff

    def close(self) -> None:
        if self._owned:
            self.lanes.close()

    def __enter__(self) -> "ChartClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, lane: Lane, url: str) -> httpx.Response | None:
        try:
            return lane.get(url)
        except httpx.HTTPError:
            return None

    def fetch(self, symbol: str, start: date, end: date) -> bytes | None:
        """Raw response bytes for one symbol's window, or None.

        Bytes rather than parsed JSON because the caller hashes and stores them:
        parsing first and re-serialising would hash our reshaping of the response
        rather than the response, which is the mistake that rules out yfinance.

        None means "this security failed tonight" and never ends the run — D4 of
        the spec widens its window tomorrow.
        """
        url = (
            f"{BASE}/{symbol}?period1={_epoch(start)}&period2={_epoch(end)}"
            "&interval=1d&events=div,split"
        )
        backoff = self._backoff
        lane = self.lanes.acquire()
        for _ in range(len(self.lanes) + _SPARE_ATTEMPTS):
            if lane.parked_for:
                # Every lane is cooling down. The wait lives here, not in the
                # pool: how long to give a source is the source's business.
                self._sleep(backoff)
                backoff *= 2
            response = self._request(lane, url)
            if response is None:
                return None
            if response.status_code == 429:
                lane.park(backoff)
                lane = self.lanes.acquire()
                continue
            if response.status_code != 200:
                return None
            return response.content
        return None
```

```python
# src/screener/ingest/__main__.py
from screener.ingest.cli import main

raise SystemExit(main())
```

```python
# src/screener/ingest/__init__.py
"""Daily ingest. Prices in this cycle; fundamentals in the next."""

from screener.ingest.chart import ChartClient

__all__ = ["ChartClient"]
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_chart.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/ingest tests/test_ingest_chart.py
git commit -m "Fetch daily bars from Yahoo over a lane"
```

---

### Task 5: Parsing, as a pure function

**Files:**
- Create: `src/screener/ingest/parse.py`
- Test: `tests/test_ingest_parse.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Bar(trade_date, open, high, low, close, volume)` and `Action(effective_date, action_type, ratio, amount)`, both frozen dataclasses with `Decimal` money fields; `parse(payload: bytes) -> tuple[list[Bar], list[Action]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_parse.py
import json
from datetime import date
from decimal import Decimal

from screener.ingest.parse import Action, Bar, parse


def chart(timestamps, quote, events=None):
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD", "symbol": "AAPL"},
                    "timestamp": timestamps,
                    "indicators": {"quote": [quote]},
                }
            ],
            "error": None,
        }
    }
    if events is not None:
        body["chart"]["result"][0]["events"] = events
    return json.dumps(body).encode()


def test_bars_come_back_as_decimals_and_a_date():
    payload = chart(
        [1758585600],  # 2025-09-23T00:00:00Z
        {
            "open": [100.5],
            "high": [101.0],
            "low": [99.5],
            "close": [100.0],
            "volume": [1234567],
        },
    )
    bars, _ = parse(payload)
    assert bars == [
        Bar(
            trade_date=date(2025, 9, 23),
            open=Decimal("100.5"),
            high=Decimal("101.0"),
            low=Decimal("99.5"),
            close=Decimal("100.0"),
            volume=1234567,
        )
    ]


def test_a_bar_with_a_null_field_is_dropped_not_zero_filled():
    # Yahoo pads its quote arrays with nulls. price_daily's columns are not
    # null, and a zero-filled bar is a fabricated -100% return.
    payload = chart(
        [1758585600, 1758672000],
        {
            "open": [100.0, None],
            "high": [101.0, None],
            "low": [99.0, None],
            "close": [100.0, None],
            "volume": [10, None],
        },
    )
    bars, _ = parse(payload)
    assert len(bars) == 1
    assert bars[0].trade_date == date(2025, 9, 23)


def test_splits_and_dividends_are_both_read():
    payload = chart(
        [1758585600],
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        events={
            "splits": {
                "1758585600": {
                    "date": 1758585600,
                    "numerator": 2,
                    "denominator": 1,
                    "splitRatio": "2:1",
                }
            },
            "dividends": {"1758585600": {"date": 1758585600, "amount": 0.24}},
        },
    )
    _, actions = parse(payload)
    assert Action(date(2025, 9, 23), "split", Decimal("2"), None) in actions
    assert Action(date(2025, 9, 23), "dividend", None, Decimal("0.24")) in actions


def test_no_events_block_means_no_actions_not_an_error():
    payload = chart(
        [1758585600],
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
    )
    bars, actions = parse(payload)
    assert len(bars) == 1
    assert actions == []


def test_an_error_response_yields_nothing_rather_than_raising():
    payload = json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}).encode()
    assert parse(payload) == ([], [])


def test_malformed_json_yields_nothing_rather_than_raising():
    assert parse(b"<html>bad gateway</html>") == ([], [])


def test_bars_come_back_in_date_order():
    payload = chart(
        [1758672000, 1758585600],
        {
            "open": [2.0, 1.0],
            "high": [2.0, 1.0],
            "low": [2.0, 1.0],
            "close": [2.0, 1.0],
            "volume": [2, 1],
        },
    )
    bars, _ = parse(payload)
    assert [b.trade_date for b in bars] == sorted(b.trade_date for b in bars)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.parse'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/parse.py
"""Chart JSON to bars and corporate actions. No I/O, no database, no clock.

Kept pure so the awkward cases — nulls in the quote arrays, an error response,
an events block that is simply absent — are tested without a socket.

`Decimal` throughout rather than float. Prices are money, and a float close of
99.99999999999999 stored in a `numeric` column is a value nobody typed.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Action:
    effective_date: date
    action_type: str
    ratio: Decimal | None
    amount: Decimal | None


def _day(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    # str() first: Decimal(float) preserves the binary error rather than the
    # number the provider meant.
    return Decimal(str(value))


def parse(payload: bytes) -> tuple[list[Bar], list[Action]]:
    """Bars and actions, or two empty lists.

    Never raises. A malformed or error response is one security failing, which
    the caller counts and the next night's wider window repairs.
    """
    try:
        result = json.loads(payload)["chart"]["result"]
    except (KeyError, TypeError, ValueError):
        return [], []
    if not result:
        return [], []
    block = result[0]

    stamps = block.get("timestamp") or []
    quotes = (block.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0] if quotes else {}

    bars: list[Bar] = []
    for index, stamp in enumerate(stamps):
        fields = {
            name: _decimal(_at(quote, name, index))
            for name in ("open", "high", "low", "close")
        }
        volume = _at(quote, "volume", index)
        # A null anywhere drops the bar. The columns are `not null`, and a
        # zero-filled bar reads downstream as a real -100% move.
        if any(value is None for value in fields.values()) or volume is None:
            continue
        bars.append(
            Bar(
                trade_date=_day(stamp),
                open=fields["open"],
                high=fields["high"],
                low=fields["low"],
                close=fields["close"],
                volume=int(volume),
            )
        )
    bars.sort(key=lambda bar: bar.trade_date)

    events = block.get("events") or {}
    actions: list[Action] = []
    for raw in (events.get("splits") or {}).values():
        numerator = _decimal(raw.get("numerator"))
        denominator = _decimal(raw.get("denominator")) or Decimal(1)
        actions.append(
            Action(
                effective_date=_day(raw["date"]),
                action_type="split",
                ratio=(numerator / denominator) if numerator else None,
                amount=None,
            )
        )
    for raw in (events.get("dividends") or {}).values():
        actions.append(
            Action(
                effective_date=_day(raw["date"]),
                action_type="dividend",
                ratio=None,
                amount=_decimal(raw.get("amount")),
            )
        )
    actions.sort(key=lambda action: (action.effective_date, action.action_type))
    return bars, actions


def _at(quote: dict[str, list[object]], name: str, index: int) -> object:
    values = quote.get(name) or []
    return values[index] if index < len(values) else None
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_parse.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/ingest/parse.py tests/test_ingest_parse.py
git commit -m "Parse chart responses into bars and corporate actions"
```

---

### Task 6: Per-security window derivation

**Files:**
- Create: `src/screener/ingest/window.py`
- Test: `tests/test_ingest_window.py`

**Interfaces:**
- Consumes: `psycopg.Connection`
- Produces: `BACKFILL_START: date`, `SETTLING_DAYS: int`, `windows(conn, security_ids: list[int], *, today: date) -> dict[int, date]`, `settling_cutoff(today: date) -> date`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_window.py
from datetime import date
from decimal import Decimal

from screener.ingest.window import BACKFILL_START, settling_cutoff, windows

TODAY = date(2026, 9, 5)


def a_security(conn, symbol="AAPL"):
    # `security` has six not-null columns beyond the identity key. Inserting
    # fewer fails on the first test, so the fixture carries them all.
    return conn.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
        (symbol, symbol),
    ).fetchone()[0]


def a_bar(conn, security_id, day, close="100"):
    conn.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, %s, %s, %s, %s, %s, now(), %s)""",
        (security_id, day, close, close, close, close, 1,
         an_observation(conn, security_id)),
    )


def test_a_security_with_no_rows_backfills_from_2020(fresh_db):
    sid = a_security(fresh_db)
    assert windows(fresh_db, [sid], today=TODAY) == {sid: BACKFILL_START}


def test_a_security_held_to_yesterday_fetches_only_the_settling_window(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 9, 4))
    # Held to yesterday, so held+1 is today; the settling cutoff is earlier and
    # wins, because in-window bars must be re-fetched to be re-settled.
    assert windows(fresh_db, [sid], today=TODAY) == {sid: settling_cutoff(TODAY)}


def test_a_security_three_days_stale_fetches_the_gap_plus_the_window(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 9, 1))
    assert windows(fresh_db, [sid], today=TODAY) == {sid: settling_cutoff(TODAY)}


def test_a_security_stale_by_a_month_fetches_back_to_where_it_stopped(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 8, 1))
    # The fetch window widens to close the gap. The settling window does not.
    assert windows(fresh_db, [sid], today=TODAY) == {sid: date(2026, 8, 2)}


def test_every_requested_security_gets_an_answer(fresh_db):
    held = a_security(fresh_db, "AAPL")
    fresh = a_security(fresh_db, "MSFT")
    a_bar(fresh_db, held, date(2026, 8, 1))
    got = windows(fresh_db, [held, fresh], today=TODAY)
    assert set(got) == {held, fresh}
    assert got[fresh] == BACKFILL_START


def test_the_settling_cutoff_is_seven_calendar_days():
    assert settling_cutoff(date(2026, 9, 5)) == date(2026, 8, 29)
```

Add to `tests/conftest.py`. Tasks 7, 8 and 9 all use these, so they live here once:

```python
def an_observation(conn, security_id):
    """An ingest_observation to hang facts off.

    `price_daily.ingest_observation_id` and `corporate_action.ingest_observation_id`
    are both not-null foreign keys, so nothing can be inserted without one.
    """
    source_id = conn.execute(
        "insert into data_source (code, name) values ('yahoo', 'Yahoo') "
        "on conflict (code) do update set name = excluded.name returning id"
    ).fetchone()[0]
    run_id = conn.execute(
        "insert into ingest_run (source_id, endpoint, started_at, status) "
        "values (%s, 'chart', now(), 'running') returning id",
        (source_id,),
    ).fetchone()[0]
    return conn.execute(
        """insert into ingest_observation
           (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
            is_new_payload, payload_bytes)
           values (%s, %s, now(), %s, 'yahoo/chart/2026-09-05/1.json.gz', true, 1)
           returning id""",
        (run_id, security_id, b"\x00" * 32),
    ).fetchone()[0]


@pytest.fixture
def ingest_ctx(fresh_db):
    """One security and an observation, as (security_id, observation_id)."""
    security_id = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Test', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01') returning id"""
    ).fetchone()[0]
    return security_id, an_observation(fresh_db, security_id)


@pytest.fixture
def two_securities(fresh_db):
    """Two securities as (id, symbol) pairs."""
    out = []
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        out.append(
            (
                fresh_db.execute(
                    """insert into security
                       (name, mic, currency, country, primary_symbol, first_seen)
                       values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
                    (name, symbol),
                ).fetchone()[0],
                symbol,
            )
        )
    return out[0], out[1]


def chart_bytes(day, close="100", volume=10, split=None):
    """A minimal chart response. Used by the run and sweep tests."""
    import json
    from datetime import datetime, timezone

    stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD"},
                    "timestamp": [stamp],
                    "indicators": {
                        "quote": [
                            {
                                "open": [float(close)],
                                "high": [float(close)],
                                "low": [float(close)],
                                "close": [float(close)],
                                "volume": [volume],
                            }
                        ]
                    },
                }
            ]
        }
    }
    if split:
        body["chart"]["result"][0]["events"] = {
            "splits": {str(stamp): {"date": stamp, "numerator": 2, "denominator": 1}}
        }
    return json.dumps(body).encode()


class FakeClient:
    """A ChartClient-shaped stub, keyed by symbol so one can fail."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def fetch(self, symbol, start, end):
        self.asked.append((symbol, start, end))
        return self.bodies.get(symbol)
```

- [ ] **Step 2: Run to verify they fail**

Run: `DATABASE_URL_TEST=postgresql://postgres:screener@localhost:5432/screener_test pytest tests/test_ingest_window.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.window'`

If every test **skips**, `DATABASE_URL_TEST` is unset. Start Postgres first:
`docker compose -p screener-test up -d postgres`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/window.py
"""How far back to ask, per security.

Two windows, and they must not be one number:

- the **fetch** window is how much to request, and widens to close a gap after
  an outage;
- the **settling** window is how far back an upsert is permitted, and never
  widens.

Sharing a number would let a catch-up run silently rewrite bars that had already
settled.

Derived per security from `price_daily` rather than from the last successful
`ingest_run`. Failures are per security but a run's status is per run, so a
run-level window never goes back for what a `partial` night missed. Per security
it self-heals, and a security added by the quarterly universe refresh has no rows
at all, so it backfills with no special case.
"""

from datetime import date, timedelta

import psycopg

# Migration 008 pre-creates yearly partitions from here, for exactly this.
BACKFILL_START = date(2020, 1, 1)

# Calendar days, not trading days, so no market calendar is needed. Seven covers
# a normal trading week across a weekend. Yahoo appears to revise only the most
# recent session, so this is generous on purpose; the upsert log is what would
# justify shortening it.
SETTLING_DAYS = 7


def settling_cutoff(today: date) -> date:
    return today - timedelta(days=SETTLING_DAYS)


def windows(
    conn: psycopg.Connection, security_ids: list[int], *, today: date
) -> dict[int, date]:
    """The first date to request, per security. Every id gets an answer."""
    if not security_ids:
        return {}
    cutoff = settling_cutoff(today)
    held: dict[int, date] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select security_id, max(trade_date)
                 from price_daily
                where security_id = any(%s)
             group by security_id""",
            (security_ids,),
        )
        for security_id, latest in cur.fetchall():
            held[security_id] = latest
    return {
        security_id: (
            min(held[security_id] + timedelta(days=1), cutoff)
            if security_id in held
            else BACKFILL_START
        )
        for security_id in security_ids
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_window.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/ingest/window.py tests/test_ingest_window.py
git commit -m "Derive the fetch window per security from the data"
```

---

### Task 7: The loader — one transaction, one mutating function

**Files:**
- Create: `src/screener/ingest/load.py`
- Test: `tests/test_ingest_load.py`

**Interfaces:**
- Consumes: `Bar`, `Action` from `parse`; `settling_cutoff` from `window`
- Produces: `Change(security_id, trade_date, field, old, new)`; `record_observation(cur, *, ingest_run_id, security_id, content_hash, blob_path, is_new_payload, payload_bytes) -> int`; `previous_hash(cur, security_id) -> bytes | None`; `insert_settled_bars(cur, security_id, observation_id, bars, cutoff) -> int`; `upsert_unsettled_bars(cur, security_id, observation_id, bars, cutoff) -> list[Change]`; `insert_actions(cur, security_id, observation_id, actions) -> list[Change]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_load.py
from datetime import date
from decimal import Decimal

import pytest

from screener.ingest.load import (
    insert_actions,
    insert_settled_bars,
    record_observation,
    upsert_unsettled_bars,
)
from screener.ingest.parse import Action, Bar
from screener.ingest.window import settling_cutoff

TODAY = date(2026, 9, 5)
CUTOFF = settling_cutoff(TODAY)


def bar(day, close="100", volume=10):
    return Bar(day, Decimal(close), Decimal(close), Decimal(close), Decimal(close), volume)


def test_a_settled_bar_that_already_exists_is_not_modified(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    old = date(2026, 1, 5)
    insert_settled_bars(fresh_db.cursor(), sid, obs, [bar(old, "100")], CUTOFF)
    insert_settled_bars(fresh_db.cursor(), sid, obs, [bar(old, "999")], CUTOFF)
    got = fresh_db.execute(
        "select close from price_daily where security_id=%s and trade_date=%s", (sid, old)
    ).fetchone()[0]
    assert got == Decimal("100")


def test_an_unsettled_bar_is_upserted_and_the_change_is_reported(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    recent = date(2026, 9, 4)
    upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent, "100", 10)], CUTOFF)
    changes = upsert_unsettled_bars(
        fresh_db.cursor(), sid, obs, [bar(recent, "100", 99)], CUTOFF
    )
    assert [(c.field, c.old, c.new) for c in changes] == [("volume", 10, 99)]
    got = fresh_db.execute(
        "select volume from price_daily where security_id=%s and trade_date=%s", (sid, recent)
    ).fetchone()[0]
    assert got == 99


def test_an_unchanged_unsettled_bar_reports_no_change(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    recent = date(2026, 9, 4)
    upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent)], CUTOFF)
    assert upsert_unsettled_bars(fresh_db.cursor(), sid, obs, [bar(recent)], CUTOFF) == []


def test_the_two_paths_split_on_the_cutoff(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    bars = [bar(date(2026, 1, 5)), bar(date(2026, 9, 4))]
    assert insert_settled_bars(fresh_db.cursor(), sid, obs, bars, CUTOFF) == 1
    assert len(upsert_unsettled_bars(fresh_db.cursor(), sid, obs, bars, CUTOFF)) == 0
    count = fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 2


def test_a_corporate_action_already_held_is_not_duplicated(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    action = Action(date(2026, 9, 3), "split", Decimal("2"), None)
    insert_actions(fresh_db.cursor(), sid, obs, [action])
    insert_actions(fresh_db.cursor(), sid, obs, [action])
    count = fresh_db.execute(
        "select count(*) from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 1


def test_a_differing_corporate_action_is_reported_and_not_written(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    insert_actions(
        fresh_db.cursor(), sid, obs, [Action(date(2026, 9, 3), "dividend", None, Decimal("0.24"))]
    )
    changes = insert_actions(
        fresh_db.cursor(), sid, obs, [Action(date(2026, 9, 3), "dividend", None, Decimal("0.25"))]
    )
    assert [(c.field, c.old, c.new) for c in changes] == [
        ("amount", Decimal("0.24"), Decimal("0.25"))
    ]
    held = fresh_db.execute(
        "select amount from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert held == Decimal("0.24")


def test_a_split_and_a_dividend_on_one_day_are_both_kept(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    day = date(2026, 9, 3)
    insert_actions(
        fresh_db.cursor(),
        sid,
        obs,
        [
            Action(day, "split", Decimal("2"), None),
            Action(day, "dividend", None, Decimal("0.24")),
        ],
    )
    count = fresh_db.execute(
        "select count(*) from corporate_action where security_id=%s", (sid,)
    ).fetchone()[0]
    assert count == 2
```

The `ingest_ctx` fixture was added to `tests/conftest.py` in Task 6.


- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_load.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.load'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/load.py
"""Writing bars, actions and the observation that vouches for them.

`upsert_unsettled_bars` is the only function in the whole ingest path that
mutates an existing row. It is a named function rather than an `on conflict`
clause folded into a bulk insert precisely so that it can be found, read and
tested — and so that every value it changes is reported.

That report is the only witness these changes have. The sweep compares Yahoo
against what is already stored, so by the time it runs an in-window change has
already been absorbed and leaves no mismatch to find.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from screener.ingest.parse import Action, Bar

logger = logging.getLogger(__name__)

BAR_FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Change:
    security_id: int
    on: date
    field: str
    old: Any
    new: Any


def previous_hash(cur: psycopg.Cursor, security_id: int) -> bytes | None:
    """The last content hash seen for this security on the chart endpoint.

    `endpoint` lives on `ingest_run`, not on the observation, so this joins.
    """
    cur.execute(
        """select o.content_hash
             from ingest_observation o
             join ingest_run r on r.id = o.ingest_run_id
            where o.security_id = %s and r.endpoint = 'chart'
         order by o.fetched_at desc
            limit 1""",
        (security_id,),
    )
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def record_observation(
    cur: psycopg.Cursor,
    *,
    ingest_run_id: int,
    security_id: int,
    content_hash: bytes,
    blob_path: str,
    is_new_payload: bool,
    payload_bytes: int,
) -> int:
    """Always written, even when the payload was unchanged (schema D4).

    Dropping it when nothing changed would lose the record of what was known on
    a date, which is the whole point of the trail.
    """
    cur.execute(
        """insert into ingest_observation
           (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
            is_new_payload, payload_bytes)
           values (%s, %s, now(), %s, %s, %s, %s)
           returning id""",
        (
            ingest_run_id,
            security_id,
            content_hash,
            blob_path,
            is_new_payload,
            payload_bytes,
        ),
    )
    return cur.fetchone()[0]


def insert_settled_bars(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    bars: list[Bar],
    cutoff: date,
) -> int:
    """Bars older than the settling window. Insert-if-absent, never modify.

    `on conflict do nothing` is not a mutation: it is how append-only is spelled
    when a re-run legitimately sees rows it already wrote.
    """
    settled = [bar for bar in bars if bar.trade_date < cutoff]
    if not settled:
        return 0
    cur.executemany(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, %s, %s, %s, %s, %s, now(), %s)
           on conflict (security_id, trade_date) do nothing""",
        [
            (
                security_id,
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                observation_id,
            )
            for bar in settled
        ],
    )
    return len(settled)


def upsert_unsettled_bars(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    bars: list[Bar],
    cutoff: date,
) -> list[Change]:
    """Bars inside the settling window. **The one mutating path.**

    Yahoo revises the most recent session — volume in particular, as
    consolidated tape arrives — so a bar is not final the moment it appears.
    Every changed value is returned and logged, because nothing else can see it.
    """
    unsettled = [bar for bar in bars if bar.trade_date >= cutoff]
    changes: list[Change] = []
    for bar in unsettled:
        cur.execute(
            """select open, high, low, close, volume
                 from price_daily
                where security_id = %s and trade_date = %s""",
            (security_id, bar.trade_date),
        )
        existing = cur.fetchone()
        if existing is not None:
            for field, old in zip(BAR_FIELDS, existing):
                new = getattr(bar, field)
                if old != new:
                    change = Change(security_id, bar.trade_date, field, old, new)
                    changes.append(change)
                    logger.info(
                        "settling-window change: security=%s %s %s %s -> %s",
                        security_id,
                        bar.trade_date,
                        field,
                        old,
                        new,
                    )
        cur.execute(
            """insert into price_daily
               (security_id, trade_date, open, high, low, close, volume,
                observed_at, ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, %s, now(), %s)
               on conflict (security_id, trade_date) do update set
                 open = excluded.open,
                 high = excluded.high,
                 low = excluded.low,
                 close = excluded.close,
                 volume = excluded.volume,
                 observed_at = excluded.observed_at,
                 ingest_observation_id = excluded.ingest_observation_id""",
            (
                security_id,
                bar.trade_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                observation_id,
            ),
        )
    return changes


def insert_actions(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    actions: list[Action],
) -> list[Change]:
    """Insert-if-absent on (security, date, type); log a difference, never write it.

    `corporate_action` has no unique constraint, and the events block returns
    everything inside the requested window — so without this a dividend is
    re-inserted on each of the next several nights, and duplicated dividends
    corrupt exactly the adjustment schema D6 computes at scoring time.

    A hard constraint would also stop the duplicates, but it would permanently
    block a provider revising an amount. Detect, log, decide with evidence.
    """
    changes: list[Change] = []
    for action in actions:
        cur.execute(
            """select ratio, amount
                 from corporate_action
                where security_id = %s and effective_date = %s and action_type = %s""",
            (security_id, action.effective_date, action.action_type),
        )
        existing = cur.fetchone()
        if existing is not None:
            for field, old, new in (
                ("ratio", existing[0], action.ratio),
                ("amount", existing[1], action.amount),
            ):
                if old != new:
                    change = Change(security_id, action.effective_date, field, old, new)
                    changes.append(change)
                    logger.warning(
                        "corporate action differs, not written: security=%s %s %s %s -> %s",
                        security_id,
                        action.effective_date,
                        field,
                        old,
                        new,
                    )
            continue
        cur.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, amount,
                currency, observed_at, ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, now(), %s)""",
            (
                security_id,
                action.effective_date,
                action.action_type,
                action.ratio,
                action.amount,
                "USD",
                observation_id,
            ),
        )
    return changes
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_load.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/ingest/load.py tests/test_ingest_load.py tests/conftest.py
git commit -m "Write bars and corporate actions, mutating in exactly one place"
```

---

### Task 8: The run — atomicity, failure counting, `ingest_run`

**Files:**
- Create: `src/screener/ingest/run.py`
- Test: `tests/test_ingest_run.py`

**Interfaces:**
- Consumes: everything above
- Produces: `IngestReport(requested, ok, failed, changes)`; `run_prices(conn, *, client, blobs, today, securities, run_id=None, delay=0.0) -> IngestReport`; `active_securities(conn) -> list[tuple[int, str]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_run.py
import gzip
from datetime import date

import pytest

from screener.blobs import LocalStore
from screener.ingest.run import run_prices

TODAY = date(2026, 9, 5)


# `FakeClient` and `chart_bytes` come from conftest (Task 6).


def test_a_run_writes_an_observation_a_blob_and_the_bars(fresh_db, two_securities, tmp_path):
    (sid, symbol), _ = two_securities
    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    blobs = LocalStore(tmp_path)
    report = run_prices(
        fresh_db, client=client, blobs=blobs, today=TODAY, securities=[(sid, symbol)]
    )
    assert report.ok == 1 and report.failed == 0
    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0] == 1
    path = fresh_db.execute(
        "select blob_path from ingest_observation where security_id=%s", (sid,)
    ).fetchone()[0]
    assert gzip.decompress(blobs.get(path))


def test_the_observation_and_both_fact_writes_are_one_transaction(
    fresh_db, two_securities, tmp_path, monkeypatch
):
    # THE -50% MOMENTUM TEST. A run that inserts bars, dies, and leaves the
    # split un-inserted produces an unadjusted series across a 2-for-1 split:
    # a -50% twelve-month return that looks like real data rather than failure.
    (sid, symbol), _ = two_securities
    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4), split=True)})

    import screener.ingest.run as run_module

    def explode(*args, **kwargs):
        raise RuntimeError("died between the bars and the split")

    monkeypatch.setattr(run_module, "insert_actions", explode)

    with pytest.raises(RuntimeError, match="died between"):
        run_prices(
            fresh_db, client=client, blobs=LocalStore(tmp_path), today=TODAY,
            securities=[(sid, symbol)],
        )

    assert fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0] == 0
    assert fresh_db.execute(
        "select count(*) from ingest_observation where security_id=%s", (sid,)
    ).fetchone()[0] == 0


def test_one_securitys_failure_does_not_end_the_run(fresh_db, two_securities, tmp_path):
    (good, good_symbol), (bad, bad_symbol) = two_securities
    client = FakeClient({good_symbol: chart_bytes(date(2026, 9, 4))})  # bad returns None
    report = run_prices(
        fresh_db,
        client=client,
        blobs=LocalStore(tmp_path),
        today=TODAY,
        securities=[(good, good_symbol), (bad, bad_symbol)],
    )
    assert report.ok == 1 and report.failed == 1
    assert report.requested == 2


def test_a_blob_failure_aborts_the_run_and_writes_no_observation(
    fresh_db, two_securities, tmp_path
):
    from screener.blobs import BlobWriteFailed

    (sid, symbol), _ = two_securities

    class Broken:
        def put(self, path, data):
            raise BlobWriteFailed("R2 is down")

        def get(self, path):
            raise BlobWriteFailed("R2 is down")

    client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    with pytest.raises(BlobWriteFailed):
        run_prices(
            fresh_db, client=client, blobs=Broken(), today=TODAY, securities=[(sid, symbol)]
        )
    assert fresh_db.execute("select count(*) from ingest_observation").fetchone()[0] == 0


def test_re_running_the_same_night_is_idempotent(fresh_db, two_securities, tmp_path):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    for _ in range(2):
        client = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
        run_prices(
            fresh_db, client=client, blobs=blobs, today=TODAY, securities=[(sid, symbol)]
        )
    rows = fresh_db.execute(
        "select count(*) from price_daily where security_id=%s", (sid,)
    ).fetchone()[0]
    assert rows == 1


def test_a_second_run_asks_from_a_narrower_window(fresh_db, two_securities, tmp_path):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    first = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    run_prices(fresh_db, client=first, blobs=blobs, today=TODAY, securities=[(sid, symbol)])
    assert first.asked[0][1] == date(2020, 1, 1)

    second = FakeClient({symbol: chart_bytes(date(2026, 9, 4))})
    run_prices(fresh_db, client=second, blobs=blobs, today=TODAY, securities=[(sid, symbol)])
    assert second.asked[0][1] == date(2026, 8, 29)


def test_an_unchanged_payload_still_writes_an_observation(fresh_db, two_securities, tmp_path):
    (sid, symbol), _ = two_securities
    blobs = LocalStore(tmp_path)
    body = chart_bytes(date(2026, 9, 4))
    for _ in range(2):
        run_prices(
            fresh_db, client=FakeClient({symbol: body}), blobs=blobs, today=TODAY,
            securities=[(sid, symbol)],
        )
    observations = fresh_db.execute(
        "select count(*), count(*) filter (where is_new_payload) from ingest_observation"
    ).fetchone()
    assert observations[0] == 2      # always recorded
    assert observations[1] == 1      # blob written once
```

The `two_securities`, `chart_bytes` and `FakeClient` helpers were added to
`tests/conftest.py` in Task 6.


- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_run.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.run'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/run.py
"""One night of price ingest.

Backfill is not a mode. A security with no rows gets 2020-01-01 from
`windows()`, so cron and a human run the same code and the awkward case — an
outage — exercises the same path as an ordinary night rather than a branch that
only runs once something has already gone wrong.
"""

import gzip
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import psycopg

from screener.blobs import BlobStore, blob_path
from screener.ingest.load import (
    Change,
    insert_actions,
    insert_settled_bars,
    previous_hash,
    record_observation,
    upsert_unsettled_bars,
)
from screener.ingest.parse import parse
from screener.ingest.window import settling_cutoff, windows

logger = logging.getLogger(__name__)

SOURCE = "yahoo"
ENDPOINT = "chart"


@dataclass
class IngestReport:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    changes: list[Change] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.ok == 0 and self.requested:
            return "failed"
        return "ok" if self.failed == 0 else "partial"


def source_id(conn: psycopg.Connection) -> int:
    """The `yahoo` data_source row, created on first use."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into data_source (code, name) values (%s, 'Yahoo Finance') "
            "on conflict (code) do update set name = excluded.name returning id",
            (SOURCE,),
        )
        return cur.fetchone()[0]


def active_securities(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """Active securities and their current symbol."""
    with conn.cursor() as cur:
        cur.execute(
            """select s.id, ss.symbol
                 from security s
                 join security_symbol ss on ss.security_id = s.id
                where s.is_active and ss.valid_to is null
             order by ss.symbol"""
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def open_run(conn: psycopg.Connection, requested: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """insert into ingest_run
               (source_id, endpoint, started_at, status, securities_requested)
               values (%s, %s, now(), 'running', %s) returning id""",
            (source_id(conn), ENDPOINT, requested),
        )
        return cur.fetchone()[0]


def close_run(conn: psycopg.Connection, run_id: int, report: IngestReport) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """update ingest_run
                  set finished_at = now(), status = %s, securities_ok = %s
                where id = %s""",
            (report.status, report.ok, run_id),
        )


def run_prices(
    conn: psycopg.Connection,
    *,
    client,
    blobs: BlobStore,
    today: date,
    securities: list[tuple[int, str]],
    run_id: int | None = None,
    delay: float = 0.0,
) -> IngestReport:
    report = IngestReport(requested=len(securities))
    owned_run = run_id is None
    run_id = run_id if run_id is not None else open_run(conn, len(securities))
    starts = windows(conn, [sid for sid, _ in securities], today=today)
    cutoff = settling_cutoff(today)

    for security_id, symbol in securities:
        if delay:
            time.sleep(delay)
        payload = client.fetch(symbol, starts[security_id], today)
        if payload is None:
            report.failed += 1
            logger.warning("no chart for %s; next run widens its window", symbol)
            continue

        content_hash = hashlib.sha256(payload).digest()
        path = blob_path(SOURCE, ENDPOINT, today, security_id)

        with conn.cursor() as cur:
            is_new = previous_hash(cur, security_id) != content_hash
        if is_new:
            # Raises on failure, which ends the run: `ingest_observation`
            # asserts blob_path exists, so the row must not be written when the
            # object does not. An R2 error is systemic, not per-object.
            blobs.put(path, gzip.compress(payload))

        bars, actions = parse(payload)
        # One transaction: observation first because it is the FK target, then
        # both fact writes. A run that inserts bars, dies, and leaves the split
        # un-inserted gives a -50% return that looks like real data.
        with conn.transaction():
            with conn.cursor() as cur:
                observation_id = record_observation(
                    cur,
                    ingest_run_id=run_id,
                    security_id=security_id,
                    content_hash=content_hash,
                    blob_path=path,
                    is_new_payload=is_new,
                    payload_bytes=len(payload),
                )
                insert_settled_bars(cur, security_id, observation_id, bars, cutoff)
                report.changes.extend(
                    upsert_unsettled_bars(cur, security_id, observation_id, bars, cutoff)
                )
                report.changes.extend(
                    insert_actions(cur, security_id, observation_id, actions)
                )
        report.ok += 1

    if owned_run:
        close_run(conn, run_id, report)
    return report
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_run.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Run the whole suite**

Run: `pytest`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add src/screener/ingest/run.py tests/test_ingest_run.py tests/conftest.py
git commit -m "Run a night of price ingest, atomically per security"
```

---

### Task 9: The detect-only sweep

**Files:**
- Create: `src/screener/ingest/sweep.py`
- Test: `tests/test_ingest_sweep.py`

**Interfaces:**
- Consumes: `parse`, `Change`
- Produces: `SweepReport(compared, mismatches, by_field)`; `run_sweep(conn, *, client, today, securities) -> SweepReport`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_sweep.py
from datetime import date

from screener.ingest.sweep import run_sweep

# `FakeClient` and `chart_bytes` come from conftest (Task 6): tests/ is not a
# package, so cross-importing between test modules is not reliable.

TODAY = date(2026, 9, 5)


def test_the_sweep_reports_a_mismatch_by_field(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    day = date(2026, 5, 1)
    fresh_db.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, 100, 100, 100, 100, 10, now(), %s)""",
        (sid, day, obs),
    )
    client = FakeClient({"AAA": chart_bytes(day, close="100", volume=99)})
    report = run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    assert report.by_field == {"volume": 1}
    assert report.compared == 1


def test_the_sweep_writes_nothing_at_all(fresh_db, ingest_ctx):
    sid, obs = ingest_ctx
    day = date(2026, 5, 1)
    fresh_db.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, 100, 100, 100, 100, 10, now(), %s)""",
        (sid, day, obs),
    )
    before = (
        fresh_db.execute("select count(*) from price_daily").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_observation").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_run").fetchone()[0],
        fresh_db.execute("select close, volume from price_daily").fetchone(),
    )
    client = FakeClient({"AAA": chart_bytes(day, close="777", volume=999)})
    run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    after = (
        fresh_db.execute("select count(*) from price_daily").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_observation").fetchone()[0],
        fresh_db.execute("select count(*) from ingest_run").fetchone()[0],
        fresh_db.execute("select close, volume from price_daily").fetchone(),
    )
    assert before == after


def test_a_bar_yahoo_returns_that_we_do_not_hold_is_not_a_mismatch(fresh_db, ingest_ctx):
    sid, _ = ingest_ctx
    client = FakeClient({"AAA": chart_bytes(date(2026, 5, 1))})
    report = run_sweep(fresh_db, client=client, today=TODAY, securities=[(sid, "AAA")])
    assert report.by_field == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_sweep.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.sweep'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/sweep.py
"""Does Yahoo actually correct settled bars, and how often?

Detect-only. Writes nothing: no rows, no blobs, no `ingest_observation`. It is a
diagnostic, not an observation of record, so it must not enter the trail.

The **field** matters more than the count. "Volume was revised" and "close was
revised" have completely different implications for a momentum score: if the
answer is a handful a year and all volume, `price_daily` keeps its snapshot key.
If closes are corrected regularly it needs `observed_at` in that key and a
point-in-time read, and that has to be known before any backtest means anything.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import psycopg

from screener.ingest.load import BAR_FIELDS, Change
from screener.ingest.parse import parse
from screener.ingest.window import BACKFILL_START

logger = logging.getLogger(__name__)


@dataclass
class SweepReport:
    compared: int = 0
    mismatches: list[Change] = field(default_factory=list)
    by_field: dict[str, int] = field(default_factory=dict)


def run_sweep(
    conn: psycopg.Connection,
    *,
    client,
    today: date,
    securities: list[tuple[int, str]],
) -> SweepReport:
    report = SweepReport()
    counts: Counter[str] = Counter()

    for security_id, symbol in securities:
        payload = client.fetch(symbol, BACKFILL_START, today)
        if payload is None:
            logger.warning("sweep: no chart for %s", symbol)
            continue
        bars, _ = parse(payload)

        with conn.cursor() as cur:
            cur.execute(
                """select trade_date, open, high, low, close, volume
                     from price_daily where security_id = %s""",
                (security_id,),
            )
            held = {row[0]: row[1:] for row in cur.fetchall()}

        for bar in bars:
            existing = held.get(bar.trade_date)
            if existing is None:
                # We simply do not hold it. That is a gap, not a correction.
                continue
            report.compared += 1
            for name, old in zip(BAR_FIELDS, existing):
                new = getattr(bar, name)
                if old != new:
                    counts[name] += 1
                    report.mismatches.append(
                        Change(security_id, bar.trade_date, name, old, new)
                    )
                    logger.info(
                        "sweep mismatch: security=%s %s %s %s -> %s",
                        security_id, bar.trade_date, name, old, new,
                    )

    report.by_field = dict(counts)
    logger.info(
        "sweep: compared %d bars, %d mismatches, by field: %s",
        report.compared, len(report.mismatches), report.by_field or "none",
    )
    return report
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_sweep.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add src/screener/ingest/sweep.py tests/test_ingest_sweep.py
git commit -m "Add a detect-only sweep for corrections to settled bars"
```

---

### Task 10: Wire the CLI and document the commands

**Files:**
- Create: `src/screener/ingest/cli.py`
- Modify: `src/screener/ingest/__init__.py`, `.env.example`, `docs/infrastructure.md`, `CLAUDE.md`, `PLAN.md`
- Test: `tests/test_ingest_cli.py`

**Interfaces:**
- Consumes: `run_prices`, `run_sweep`, `active_securities`, `ChartClient`, `screener.blobs.store`
- Produces: `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_cli.py
import pytest

from screener.ingest.cli import main


def test_an_unknown_command_exits_rather_than_guessing():
    with pytest.raises(SystemExit):
        main(["nonsense"])


def test_the_commands_are_exactly_prices_and_sweep(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "prices" in out and "sweep" in out


def test_delay_defaults_to_zero_because_nothing_has_hit_a_limit():
    import argparse

    from screener.ingest.cli import build_parser

    parser: argparse.ArgumentParser = build_parser()
    assert parser.parse_args(["prices"]).delay == 0.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_ingest_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.ingest.cli'`

- [ ] **Step 3: Write the implementation**

```python
# src/screener/ingest/cli.py
"""Entry point, shaped like `screener.universe` so the two read the same.

`prices` is the nightly path and works out what it needs per security. `sweep`
is a hand-run diagnostic that writes nothing.
"""

import argparse
import logging
from datetime import date

import psycopg

from screener.blobs import store
from screener.config import settings
from screener.ingest.chart import ChartClient
from screener.ingest.run import active_securities, run_prices
from screener.ingest.sweep import run_sweep

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screener.ingest", description=__doc__)
    parser.add_argument(
        "command",
        choices=("prices", "sweep"),
        help=(
            "prices: fetch missing daily bars per security, backfilling to 2020 "
            "on first sight. sweep: compare six years against what is stored "
            "and report differences, writing nothing."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=(
            "seconds between securities. Zero by default: 3,012 sequential "
            "requests produced no 429, and lanes already park an exit that "
            "rate-limits. This is the safeguard, not a known requirement."
        ),
    )
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="override the run date (testing and backfills)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N securities, for a smoke run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    today = args.today or date.today()

    with psycopg.connect(settings().database_url) as conn:
        securities = active_securities(conn)
        if args.limit:
            securities = securities[: args.limit]
        if not securities:
            logger.error("no active securities; run `python -m screener.universe load` first")
            return 1

        with ChartClient() as client:
            if args.command == "prices":
                report = run_prices(
                    conn,
                    client=client,
                    blobs=store(),
                    today=today,
                    securities=securities,
                    delay=args.delay,
                )
                logger.info(
                    "prices: %d requested, %d ok, %d failed, %d settling-window changes",
                    report.requested, report.ok, report.failed, len(report.changes),
                )
                return 0 if report.ok else 1

            report = run_sweep(conn, client=client, today=today, securities=securities)
            logger.info(
                "sweep: %d compared, %d mismatches, by field %s",
                report.compared, len(report.mismatches), report.by_field or "none",
            )
            return 0
```

Update `src/screener/ingest/__init__.py`:

```python
"""Daily ingest. Prices in this cycle; fundamentals in the next."""

from screener.ingest.chart import ChartClient
from screener.ingest.parse import Action, Bar, parse
from screener.ingest.run import IngestReport, active_securities, run_prices
from screener.ingest.sweep import SweepReport, run_sweep
from screener.ingest.window import BACKFILL_START, SETTLING_DAYS, windows

__all__ = [
    "BACKFILL_START",
    "SETTLING_DAYS",
    "Action",
    "Bar",
    "ChartClient",
    "IngestReport",
    "SweepReport",
    "active_securities",
    "parse",
    "run_prices",
    "run_sweep",
    "windows",
]
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_ingest_cli.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Update the docs**

Append to `.env.example`:

```bash
# --- Payload store -----------------------------------------------------------
# Where raw API responses land. `local` is a directory (tests, and the fallback
# if R2 ever becomes a problem); `s3` is Cloudflare R2. blob_path is not null and
# every score traces back to a stored response, so nothing here prunes.
BLOB_BACKEND=local
BLOB_LOCAL_DIR=data/blobs
# R2 wants `auto` as the region. Endpoint is https://<account>.r2.cloudflarestorage.com
BLOB_S3_ENDPOINT=
BLOB_S3_BUCKET=
BLOB_S3_ACCESS_KEY_ID=
BLOB_S3_SECRET_ACCESS_KEY=
BLOB_S3_REGION=auto
```

Add to `CLAUDE.md` under Commands:

```markdown
- Ingest prices: `python -m screener.ingest prices` — fetches missing daily bars per
  security, backfilling to 2020 on first sight. `sweep` is a hand-run diagnostic that
  compares six years against what is stored and writes nothing.
```

Add to `CLAUDE.md` under Infrastructure layout:

```markdown
- `screener.blobs` — the payload store. `local` for tests, Cloudflare R2 in production,
  SigV4 hand-rolled because two verbs against one bucket with static credentials is the
  narrow case where that is tractable. Nothing prunes: `ingest_observation.blob_path` is
  `not null` and every score traces back to a stored response.
- `screener.ingest` — daily bars and corporate actions from Yahoo. Backfill is not a mode;
  a security with no rows gets 2020. Two windows: the fetch window widens to close a gap,
  the settling window (7 days) never does, and inside it is the one place the ingest path
  mutates an existing row.
```

Update `PLAN.md` item 2 to point at the spec and plan, and move the price half to a
"Done" entry once merged.

- [ ] **Step 6: Run everything**

Run: `pytest && pyright`
Expected: full suite passes, zero type errors

- [ ] **Step 7: Commit**

```bash
git add src/screener/ingest/cli.py src/screener/ingest/__init__.py tests/test_ingest_cli.py \
        .env.example CLAUDE.md docs/infrastructure.md PLAN.md
git commit -m "Wire the ingest commands and document them"
```

---

## Verification

After Task 10, with Postgres up and the universe loaded:

```bash
# 1. Everything green, no network touched
pytest && pyright

# 2. A five-security smoke run against real Yahoo, writing blobs to disk
BLOB_BACKEND=local python -m screener.ingest prices --limit 5

# 3. Confirm the shape of what landed
psql "$DATABASE_URL" -c "
  select count(*) bars, min(trade_date), max(trade_date) from price_daily;
  select count(*) from ingest_observation;
  select status, securities_requested, securities_ok from ingest_run order by id desc limit 1;
"

# 4. Re-run it: bar count must not change, and a second observation row must appear
python -m screener.ingest prices --limit 5

# 5. The sweep must write nothing
python -m screener.ingest sweep --limit 5
```

Expected after step 2: roughly 1,600 bars per security (six years of trading days),
one `ingest_observation` per security, `ingest_run.status = 'ok'`.

Expected after step 4: identical bar count; `ingest_observation` count doubled;
`is_new_payload` false for the second set unless a bar genuinely moved.

---

## Out of scope

- Fundamentals, `fundamental_fact`, and the crumbed `quoteSummary` path — cycle two.
- Scoring: `metric_daily`, `pillar_score_daily`, `snapshot_daily`.
- Replacing `screener.concept` in the dashboard and the bot's chart tool.
- The nightly `pg_dump` to R2. This plan creates the bucket and credential it needs.
- Scheduling. Nothing runs this on a timer yet.
