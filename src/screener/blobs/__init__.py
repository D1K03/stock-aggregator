"""The payload store.

`ingest_observation.blob_path` is `not null` and D1 of the schema spec claims
every score traces back to the stored response, so this is evidence rather than
cache. Nothing here prunes.
"""

from pathlib import Path
from typing import Protocol

from screener.blobs.config import BlobConfig
from screener.blobs.errors import BlobWriteFailed
from screener.blobs.local import LocalStore
from screener.blobs.paths import blob_path, check


class BlobStore(Protocol):
    def put(self, path: str, data: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...


def store(config: BlobConfig | None = None) -> BlobStore:
    config = config or BlobConfig.from_env()
    if config.backend == "local":
        return LocalStore(Path(config.local_dir))
    if config.backend == "s3":
        raise RuntimeError("BLOB_BACKEND=s3 arrives in Task 3")
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
