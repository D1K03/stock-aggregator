"""Pre-creation of yearly range partitions.

Granularity is yearly. Once the universe passes ~1,000 tickers, monthly
partitions can be created from the next year boundary onwards — a partitioned
parent holds mixed granularity, so no existing partition is ever rebuilt.

Existence is decided by partition-bound COVERAGE, not by name: once a table
has monthly partitions for a year (e.g. `metric_daily_2028_01` ...
`_12`), a yearly partition of the same name does not exist, but the year IS
already covered — creating a same-named yearly partition would overlap and
error. `ensure_partitions` therefore inspects each parent's actual attached
partitions (via `pg_inherits` / `pg_get_expr(relpartbound, ...)`) and skips a
year if any existing partition already overlaps it.
"""

import re

import psycopg
from psycopg import sql

_BOUND_DATE_RE = re.compile(r"'(\d{4}-\d{2}-\d{2})'")

# Per-table start year for partition pre-creation. price_daily holds raw
# market history that predates the first scoring date (momentum needs 12+
# trailing months), while the derived tables cannot contain rows earlier
# than the first scoring run.
PARTITION_START_YEAR: dict[str, int] = {
    "price_daily": 2020,
    "metric_daily": 2026,
    "pillar_score_daily": 2026,
    "snapshot_daily": 2026,
    "event_flag_daily": 2026,
}

PARTITIONED_TABLES: tuple[str, ...] = tuple(PARTITION_START_YEAR.keys())


def partition_name(table: str, year: int) -> str:
    return f"{table}_{year}"


def _parent_schema(conn: psycopg.Connection, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            select n.nspname
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where c.oid = %s::regclass
            """,
            (table,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"partitioned table {table!r} not found")
        return row[0]


def _year_covered(conn: psycopg.Connection, table: str, year: int) -> bool:
    """True if any partition already attached to `table` overlaps `year`.

    Bounds are read from `pg_get_expr(relpartbound, oid)`, which renders as
    e.g. "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')" — the two date
    literals are pulled out in order rather than re-parsed by the database,
    since the wording is stable but not worth depending on syntactically.
    """
    import datetime

    with conn.cursor() as cur:
        cur.execute(
            """
            select pg_get_expr(c.relpartbound, c.oid)
            from pg_inherits i
            join pg_class c on c.oid = i.inhrelid
            where i.inhparent = %s::regclass
            """,
            (table,),
        )
        year_start = datetime.date(year, 1, 1)
        year_end = datetime.date(year + 1, 1, 1)
        for (bound_expr,) in cur.fetchall():
            dates = _BOUND_DATE_RE.findall(bound_expr or "")
            if len(dates) != 2:
                continue
            lower = datetime.date.fromisoformat(dates[0])
            upper = datetime.date.fromisoformat(dates[1])
            if lower < year_end and upper > year_start:
                return True
        return False


def ensure_partitions(
    conn: psycopg.Connection, through_year: int, from_year: int | None = None
) -> list[str]:
    """Create any missing yearly partition up to `through_year`.

    When `from_year` is omitted, each table starts from its own entry in
    `PARTITION_START_YEAR`; an explicit `from_year` overrides that for every
    table. A year is skipped, not recreated, whenever an existing partition
    (yearly or monthly) already covers it. Safe to call on every run.
    """
    created: list[str] = []
    for table in PARTITIONED_TABLES:
        start = from_year if from_year is not None else PARTITION_START_YEAR[table]
        schema = _parent_schema(conn, table)
        for year in range(start, through_year + 1):
            if _year_covered(conn, table, year):
                continue
            name = partition_name(table, year)
            # Composed rather than an f-string: psycopg types the query as
            # LiteralString to catch SQL assembled at runtime, and composition
            # is the sanctioned way to build DDL around identifiers. It also
            # quotes them properly instead of trusting the caller.
            conn.execute(
                sql.SQL(
                    "create table {child} partition of {parent} "
                    "for values from ({start}) to ({end})"
                ).format(
                    child=sql.Identifier(schema, name),
                    parent=sql.Identifier(table),
                    start=sql.Literal(f"{year}-01-01"),
                    end=sql.Literal(f"{year + 1}-01-01"),
                )
            )
            created.append(name)
    return created
