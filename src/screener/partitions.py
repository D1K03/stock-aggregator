"""Pre-creation of yearly range partitions.

Granularity is yearly. Once the universe passes ~1,000 tickers, monthly
partitions can be created from the next year boundary onwards — a partitioned
parent holds mixed granularity, so no existing partition is ever rebuilt.
"""

import psycopg

PARTITIONED_TABLES: tuple[str, ...] = (
    "price_daily",
    "metric_daily",
    "pillar_score_daily",
    "snapshot_daily",
    "event_flag_daily",
)


def partition_name(table: str, year: int) -> str:
    return f"{table}_{year}"


def _exists(conn: psycopg.Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass(%s)", (f"public.{name}",))
        return cur.fetchone()[0] is not None


def ensure_partitions(
    conn: psycopg.Connection, through_year: int, from_year: int = 2026
) -> list[str]:
    """Create any missing yearly partition from `from_year` to `through_year`.

    Returns the names created, in order. Safe to call on every run.
    """
    created: list[str] = []
    for year in range(from_year, through_year + 1):
        for table in PARTITIONED_TABLES:
            name = partition_name(table, year)
            if _exists(conn, name):
                continue
            conn.execute(
                f"create table {name} partition of {table} "
                f"for values from ('{year}-01-01') to ('{year + 1}-01-01')"
            )
            created.append(name)
    return created
