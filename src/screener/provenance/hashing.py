"""A stable hash of the configuration a scoring run depended on."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _refuse(value: Any) -> str:
    """Reject anything json cannot render deterministically.

    `Decimal`, `timedelta` and friends land here on purpose. Their `repr` is
    not a stable contract across Python versions, so silently accepting one
    would produce a hash that changes on an interpreter upgrade and quietly
    breaks comparability between runs. The caller has to pick the canonical
    rendering, because that choice is the thing being promised.
    """
    raise TypeError(
        f"config_hash cannot serialise {type(value).__name__}; "
        "convert it to a string, number or bool first"
    )


def _canonical(config: Mapping[str, Any]) -> bytes:
    # sort_keys so insertion order cannot change the digest, and explicit
    # separators so whitespace cannot.
    return json.dumps(
        config, sort_keys=True, separators=(",", ":"), default=_refuse
    ).encode()


def config_hash(config: Mapping[str, Any]) -> bytes:
    """SHA-256 over a canonical rendering of `config`, as raw digest bytes.

    Bytes rather than hex because `scoring_run.config_hash` is `bytea`.

    The mapping is supplied by the caller rather than derived from `Settings`,
    and that is the whole point. Per the schema spec this hash covers the
    *scoring* parameters a run's output depends on — `cutoff_offset`, the
    minimum peer count — so that two runs sharing a logic version and a hash
    are reproducible. Deriving it from process configuration instead would make
    it change when the OpenRouter model or the health port changed: the same
    unrelated-churn problem that got `git_sha` rejected as a comparability key.

    A consequence worth stating: credentials must never appear in `config`.
    Nothing about a run's output depends on them, so anything that would put
    one here is a mistake about what this hash is for.
    """
    return hashlib.sha256(_canonical(config)).digest()
