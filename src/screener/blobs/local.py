"""A directory. What the test suite uses, and the fallback if R2 ever goes."""

from pathlib import Path

from screener.blobs.errors import BlobWriteFailed
from screener.blobs.paths import check


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
