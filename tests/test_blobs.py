from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from screener.blobs import BlobConfig, BlobWriteFailed, LocalStore, blob_path
from screener.blobs.s3 import S3Store


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
