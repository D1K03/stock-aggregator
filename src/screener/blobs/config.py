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
