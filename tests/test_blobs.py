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
