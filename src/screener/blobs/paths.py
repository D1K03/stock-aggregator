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
