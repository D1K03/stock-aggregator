"""Apply a reconciliation plan to Postgres.

The only writer, and it never opens a socket. Everything runs in one
transaction: a half-applied universe would leave the next scoring run computing
percentiles against a partly built peer group, which is silent corruption rather
than a visible failure.
"""

import logging
from datetime import date
from pathlib import Path

import psycopg

from screener.config import settings
from screener.universe.reconcile import (
    DEPARTURE_CEILING,
    ExistingSecurity,
    Plan,
    plan,
)
from screener.universe.rows import UniverseRow, read_rows, slugify

logger = logging.getLogger(__name__)

SCHEME = "yfinance"
MARKET_GROUP = "market"


class DepartureCeilingExceeded(RuntimeError):
    """The CSV would retire more of the universe than a real refresh ever does."""


def _returned_id(cur: psycopg.Cursor, what: str) -> int:
    """The id from an `insert ... returning id`.

    An insert with a `returning` clause always yields a row, so `None` here means
    the statement did not do what it says. Saying which one beats a `TypeError`
    on a subscript twenty frames down.
    """
    got = cur.fetchone()
    if got is None:
        raise RuntimeError(f"insert of {what} returned no id")
    return got[0]


def ensure_taxonomy(conn: psycopg.Connection, rows: list[UniverseRow]) -> None:
    """Upsert the scheme, both node levels, and peer groups for levels 0 and 1.

    `sector_node` gets industries as well as sectors because a security points at
    the most specific classification available. `peer_group` gets only what v1
    scores — industry groups would be rows nothing references.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values (%s, %s)"
            " on conflict (code) do update set name = excluded.name returning id",
            (SCHEME, "yfinance"),
        )
        scheme_id = _returned_id(cur, "sector_scheme")

        sectors = {r.yf_sector for r in rows if r.yf_sector}
        for sector in sorted(sectors):
            cur.execute(
                "insert into sector_node (scheme_id, level, code, name) values (%s, 1, %s, %s)"
                " on conflict (scheme_id, code) do update set name = excluded.name",
                (scheme_id, slugify(sector), sector),
            )
        for row in rows:
            if not (row.yf_industry and row.yf_sector):
                continue
            cur.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " select %s, parent.id, 2, %s, %s from sector_node parent"
                " where parent.scheme_id = %s and parent.code = %s"
                " on conflict (scheme_id, code) do update set name = excluded.name",
                (scheme_id, slugify(row.yf_industry), row.yf_industry, scheme_id, slugify(row.yf_sector)),
            )

        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, %s) on conflict (scheme_id, code) do nothing",
            (scheme_id, MARKET_GROUP),
        )
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " select %s, id, 1, code from sector_node"
            " where scheme_id = %s and level = 1"
            " on conflict (scheme_id, code) do nothing",
            (scheme_id, scheme_id),
        )


def current_state(conn: psycopg.Connection) -> list[ExistingSecurity]:
    """Every security, with the fields the reconciler matches and compares on."""
    with conn.cursor() as cur:
        cur.execute(
            "select s.id, coalesce(s.cik, ''), s.primary_symbol,"
            "       coalesce(n.code, ''), s.is_active"
            "  from security s"
            "  left join security_sector ss"
            "    on ss.security_id = s.id and ss.valid_to is null"
            "  left join sector_node n on n.id = ss.sector_node_id"
        )
        return [
            ExistingSecurity(id=r[0], cik=r[1], symbol=r[2], industry_code=r[3], is_active=r[4])
            for r in cur.fetchall()
        ]


def _node_id(cur: psycopg.Cursor, code: str) -> int | None:
    cur.execute(
        "select n.id from sector_node n join sector_scheme s on s.id = n.scheme_id"
        " where s.code = %s and n.code = %s",
        (SCHEME, code),
    )
    got = cur.fetchone()
    return got[0] if got else None


def _classification(row: UniverseRow) -> str:
    return slugify(row.yf_industry or row.yf_sector)


def apply(
    conn: psycopg.Connection,
    rows: list[UniverseRow],
    *,
    as_of: date,
    force: bool = False,
) -> Plan:
    """Reconcile `rows` into the database. All of it, or none of it.

    Everything is inside one transaction, the taxonomy upsert and the ceiling
    check included. On an autocommit connection an `ensure_taxonomy` outside it
    would commit new sector nodes even when the load was then refused — leaving
    the database changed by a run that reported doing nothing.
    """
    with conn.transaction(), conn.cursor() as cur:
        ensure_taxonomy(conn, rows)
        decided = plan(rows, current_state(conn))

        if not force and decided.departure_share > DEPARTURE_CEILING:
            raise DepartureCeilingExceeded(
                f"{len(decided.departed)} of {decided.active_before} active securities would be "
                f"retired ({decided.departure_share:.0%}, ceiling {DEPARTURE_CEILING:.0%}). "
                "Pass --force if this is genuinely intended."
            )

        for row in decided.new:
            cur.execute(
                "insert into security (name, mic, currency, country, cik, primary_symbol,"
                " is_active, first_seen) values (%s, %s, %s, 'US', %s, %s, true, %s) returning id",
                (row.name, row.mic, row.currency, row.cik or None, row.symbol, as_of),
            )
            security_id = _returned_id(cur, f"security {row.symbol}")
            cur.execute(
                "insert into security_symbol (security_id, symbol, mic, valid_from, source)"
                " values (%s, %s, %s, %s, %s)",
                (security_id, row.symbol, row.mic, as_of, SCHEME),
            )
            node = _node_id(cur, _classification(row))
            if node is not None:
                cur.execute(
                    "insert into security_sector (security_id, sector_node_id, valid_from, source)"
                    " values (%s, %s, %s, %s)",
                    (security_id, node, as_of, SCHEME),
                )

        for gone in decided.departed:
            cur.execute(
                "update security set is_active = false, last_seen = %s where id = %s",
                (as_of, gone.id),
            )

        for back, _row in decided.reentered:
            cur.execute(
                "update security set is_active = true, last_seen = null where id = %s",
                (back.id,),
            )

        for old, row in decided.renamed:
            cur.execute(
                "update security_symbol set valid_to = %s"
                " where security_id = %s and valid_to is null",
                (as_of, old.id),
            )
            cur.execute(
                "insert into security_symbol (security_id, symbol, mic, valid_from, source)"
                " values (%s, %s, %s, %s, %s)",
                (old.id, row.symbol, row.mic, as_of, SCHEME),
            )
            cur.execute(
                "update security set primary_symbol = %s, name = %s where id = %s",
                (row.symbol, row.name, old.id),
            )

        for old, row in decided.reclassified:
            node = _node_id(cur, _classification(row))
            if node is None:
                continue
            cur.execute(
                "update security_sector set valid_to = %s"
                " where security_id = %s and valid_to is null",
                (as_of, old.id),
            )
            cur.execute(
                "insert into security_sector (security_id, sector_node_id, valid_from, source)"
                " values (%s, %s, %s, %s)",
                (old.id, node, as_of, SCHEME),
            )

    logger.info("universe load: %s", decided.summary())
    return decided


def load(path: Path, *, as_of: date, dry_run: bool = False, force: bool = False) -> Plan:
    """Read the CSV and apply it, or describe what applying it would do.

    A dry run deliberately does not upsert the taxonomy. `plan()` compares the
    CSV's own slugs against the codes already stored, so it needs nothing new
    written — and writing on an autocommit connection would leave sector nodes
    behind from a command whose whole promise is that it changes nothing.
    """
    rows = read_rows(path)
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        if dry_run:
            decided = plan(rows, current_state(conn))
            logger.info("dry run: %s", decided.summary())
            return decided
        return apply(conn, rows, as_of=as_of, force=force)
