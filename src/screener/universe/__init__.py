"""Choosing, refreshing and loading the ticker universe.

Two commands with a hard split: `refresh` talks to the network and never opens a
database connection; `load` talks to the database and never opens a socket. The
committed CSV between them is what makes a reclassification reviewable before it
can move a score.

The two entry-point functions are deliberately *not* re-exported here. They share
their names with the modules holding them, so importing the function would rebind
`screener.universe.refresh` from the module to the callable and leave anything
addressing the module by path — a `monkeypatch.setattr` string, a type checker —
looking at the wrong object. Call them as `refresh.refresh()` and `load.load()`,
or through the CLI, which is how anything outside actually reaches them.
"""

from screener.universe.load import DepartureCeilingExceeded, apply
from screener.universe.reconcile import (
    DEPARTURE_CEILING,
    AmbiguousIdentity,
    ExistingSecurity,
    Plan,
    plan,
)
from screener.universe.refresh import RefreshReport
from screener.universe.rows import (
    FIELDNAMES,
    UniverseRow,
    normalise_symbol,
    read_rows,
    slugify,
    write_rows,
)

__all__ = [
    "DEPARTURE_CEILING",
    "FIELDNAMES",
    "AmbiguousIdentity",
    "DepartureCeilingExceeded",
    "ExistingSecurity",
    "Plan",
    "RefreshReport",
    "UniverseRow",
    "apply",
    "normalise_symbol",
    "plan",
    "read_rows",
    "slugify",
    "write_rows",
]
