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
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
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
