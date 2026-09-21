"""What the dashboard's dependency tree is allowed to resolve to.

`web/package.json` carries an `overrides` block, and an override is a claim
that outlives the reason for it. This file is the reason, written down.

The case in hand is lodash-es. mermaid reaches it twice -- through `dagre-d3-es`
on a range, and through `chevrotain`, which pins it at *exactly* `4.17.23`. An
exact pin is the thing Dependabot cannot route around, so both advisories below
sat open against a lockfile the bot had already given up on. The override is how
the pin is overruled, and it is one line in a JSON file with nothing else
holding it there.

**Deleting it has no symptom.** The functions 4.18.0 changes -- `_.template`,
the internals behind `_.unset` and `_.omit`, `_.fromPairs` and `_.random` --
are none of the ones chevrotain and dagre-d3-es reach, so the change is tree
shaken out: building the dashboard on each version in turn produced 111 client
chunks and all 111 were byte-identical. Nothing renders differently, nothing
fails, and `npm install` would quietly put the vulnerable copy back with every
check in CI still green. That is what this file is for, and it is the same
argument as `tests/test_compose.py`, where the bug was a container running for
months without a database and nothing saying so.

Read-only and offline: it parses two files that are committed, and runs the
package manager not at all.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "web" / "package.json"
LOCKFILE = ROOT / "web" / "package-lock.json"

# Every package the dashboard overrides, the version it must not resolve below,
# and why. A new override belongs here with its reason the way a new service
# belongs in `tests/test_compose.py`'s two lists -- so that it has to be argued
# for once rather than inherited from whatever was copied.
FLOORS: dict[str, tuple[tuple[int, int, int], str]] = {
    "lodash-es": (
        (4, 18, 0),
        "GHSA-r5fr-rjxr-66jc / CVE-2026-4800, code injection through "
        "`_.template` imports key names, and GHSA-f23m-r3pf-42rh / "
        "CVE-2026-2950, prototype pollution through an array path in "
        "`_.unset` and `_.omit`. Both are fixed in 4.18.0 and neither is "
        "reachable from a version chevrotain's exact pin would allow.",
    ),
}


def parsed(version: str) -> tuple[int, ...]:
    """`^4.18.1` and `4.18.1` alike as a comparable tuple.

    The range prefixes npm allows for a floor and nothing cleverer: a real
    semver range parser is not a dependency this project has, and an override
    written as something this cannot read should fail loudly here rather than
    be silently treated as satisfying anything.
    """
    for prefix in ("^", "~", ">=", ">", "="):
        if version.startswith(prefix):
            version = version[len(prefix) :]
            break
    parts = version.split("-", 1)[0].split(".")
    assert all(part.isdigit() for part in parts), f"cannot read a floor out of {version!r}"
    return tuple(int(part) for part in parts)


def overrides() -> dict[str, str]:
    return json.loads(MANIFEST.read_text()).get("overrides", {})


def resolved(package: str) -> dict[str, str]:
    """Every copy of `package` the lockfile installs, by its path in the tree.

    Keyed by path rather than collapsed to a set, because npm is free to place
    a second copy under a dependency that wanted a different version -- and a
    nested one at 4.17.23 is exactly the regression this is looking for.
    """
    entries = json.loads(LOCKFILE.read_text())["packages"]
    suffix = f"node_modules/{package}"
    return {
        path: entry["version"]
        for path, entry in entries.items()
        if path == suffix or path.endswith(f"/{suffix}")
    }


def test_every_override_is_argued_for():
    # Both directions. An override with no entry here is one nobody can
    # explain; an entry here with no override is a floor nothing enforces.
    assert set(overrides()) == set(FLOORS)


def test_each_override_asks_for_at_least_its_floor():
    for package, (floor, why) in FLOORS.items():
        assert parsed(overrides()[package]) >= floor, f"{package}: {why}"


def test_the_lockfile_resolves_every_copy_above_its_floor():
    # The one that catches it. `npm ci` installs the lockfile and reads the
    # override only to check it, so this -- not the manifest -- is what says
    # which code ships.
    for package, (floor, why) in FLOORS.items():
        installed = resolved(package)
        assert installed, f"{package} is overridden but absent from the lockfile"
        low = {path: at for path, at in installed.items() if parsed(at) < floor}
        assert not low, f"{package} resolves below {'.'.join(map(str, floor))} at {low}: {why}"
