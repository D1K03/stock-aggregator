"""What the playground may read, asked of the database rather than listed here."""

from dataclasses import dataclass

from screener.playground.engine import Unavailable, connect

# Relations worth browsing: ordinary tables, partitioned parents, views and
# materialised views. Indexes and sequences are not data.
_KINDS = ("r", "p", "v", "m")

_RELATIONS = """
select c.oid, n.nspname, c.relname, c.relkind
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where c.relkind = any(%s)
  and not c.relispartition
  and n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname not like 'pg\\_%%'
  and has_table_privilege(c.oid, 'select')
order by n.nspname, c.relname
"""

_COLUMNS = """
select a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
from pg_attribute a
where a.attrelid = any(%s) and a.attnum > 0 and not a.attisdropped
order by a.attrelid, a.attnum
"""


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class Table:
    schema: str
    name: str
    kind: str
    columns: tuple[Field, ...]


def catalog() -> tuple[Table, ...]:
    """Every relation this connection may select from, with its columns.

    Derived from `has_table_privilege` on the playground's own connection rather
    than from a list in Python, so the tree in the browser is a rendering of the
    grant and the two cannot disagree. A table added to migration 013 appears
    here on the next deploy with nothing else to change; one left out is
    invisible as well as unreadable, which is the failure worth having.

    Partitions are excluded twice over: `not c.relispartition` in the query, and
    the grant itself, since a partition inherits no ACL. So `price_daily`
    appears once rather than once per year, and naming a partition directly is
    a permission error — honest, and what the tree already implies.

    Two round trips regardless of how many tables there are.
    """
    with connect() as conn:
        relations = conn.execute(_RELATIONS, [list(_KINDS)]).fetchall()
        oids = [r[0] for r in relations]
        columns = conn.execute(_COLUMNS, [oids]).fetchall() if oids else []

    by_oid: dict[int, list[Field]] = {}
    for oid, name, kind, notnull in columns:
        by_oid.setdefault(oid, []).append(Field(str(name), str(kind), not notnull))

    return tuple(
        Table(
            schema=str(schema),
            name=str(name),
            kind={"r": "table", "p": "table", "v": "view", "m": "view"}[kind],
            columns=tuple(by_oid.get(oid, ())),
        )
        for oid, schema, name, kind in relations
    )


__all__ = ["Field", "Table", "Unavailable", "catalog"]
