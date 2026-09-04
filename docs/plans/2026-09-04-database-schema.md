# Database Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the Postgres schema for the equity screener as versioned SQL migrations, with a
test suite that proves each invariant the spec claims actually holds.

**Architecture:** Numbered plain-SQL migration files applied by a ~60-line Python runner that
records applied versions in a `schema_migration` table. Plain SQL rather than Alembic because
the schema leans on range partitioning, GiST exclusion constraints, partial indexes and array
columns — DDL that ORM migration tooling expresses awkwardly, and whose value here is exactness
rather than autogeneration. Tests run against a real Postgres; there is no meaningful way to
test an exclusion constraint against a mock.

**Tech Stack:** Python 3.11+, psycopg 3, pytest, Postgres 16.

**Spec:** `docs/specs/2026-09-04-database-schema-design.md`

## Global Constraints

- Postgres 16 (Azure Flexible Server target). Minimum 12 for FKs referencing partitioned tables.
- `btree_gist` extension required for exclusion constraints combining scalar equality with range
  overlap.
- All identifiers lowercase; `text` never `varchar(n)`; `timestamptz` never `timestamp`;
  `numeric` never float for any value used in arithmetic.
- Primary keys are `bigint generated always as identity`, or `smallint` for small reference
  tables. `metric_daily` deliberately has no surrogate key.
- Every foreign key column gets an explicit index.
- Enumerations are `text` with a `check` constraint, not native enum types.
- Migrations add constraints inside `do $$ ... $$` blocks — Postgres has no
  `add constraint if not exists`.
- **Invariant: nothing holds a foreign key into a partitioned table.** Tested in Task 6.
- Creation order: reference data → security and temporal tables → weights and scoring runs →
  ingest and facts → derived daily → alerting.

---

### Task 1: Project scaffolding, database harness, and migration runner

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/screener/__init__.py`
- Create: `src/screener/config.py`
- Create: `src/screener/migrate.py`
- Create: `migrations/.gitkeep`
- Create: `tests/conftest.py`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `screener.config.settings() -> Settings` with `.database_url: str`
  - `screener.migrate.applied_versions(conn) -> set[str]`
  - `screener.migrate.apply_migrations(conn, directory: Path) -> list[str]` returning the
    versions newly applied, in order.
  - pytest fixture `fresh_db` yielding a `psycopg.Connection` against an empty `public` schema
    with all migrations applied.
  - pytest fixture `empty_db` yielding a `psycopg.Connection` against an empty `public` schema
    with **no** migrations applied.

**A working Postgres is required before this task.** Two options in WSL:

```bash
# A virtualenv for the project
python3 -m venv .venv && source .venv/bin/activate

# Option A — local install (no Docker needed)
sudo apt update && sudo apt install -y postgresql
sudo service postgresql start
sudo -u postgres createuser -s "$USER"
createdb screener_test

# Option B — Docker, if Docker Desktop WSL integration is enabled
docker run --rm -d --name pg -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:16
```

Then export the URL the tests read:

```bash
export DATABASE_URL_TEST="postgresql:///screener_test"        # Option A
export DATABASE_URL_TEST="postgresql://postgres:x@localhost:5432/postgres"   # Option B
```

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "screener"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "psycopg[binary]>=3.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create `.env.example`**

```bash
# Production / development database
DATABASE_URL=postgresql://user:password@host:5432/screener

# Database used by the test suite. THIS SCHEMA IS DROPPED ON EVERY TEST.
# Point it at a throwaway database, never at DATABASE_URL.
DATABASE_URL_TEST=postgresql:///screener_test
```

- [ ] **Step 3: Create `src/screener/__init__.py`**

```python
"""Multi-signal equity screener."""
```

- [ ] **Step 4: Create `src/screener/config.py`**

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str


def settings() -> Settings:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return Settings(database_url=url)
```

- [ ] **Step 5: Write the failing tests for the migration runner**

Create `tests/test_migrate.py`:

```python
import pytest
import psycopg

from screener.migrate import applied_versions, apply_migrations


def test_apply_migrations_runs_files_in_order(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")
    (tmp_path / "002_second.sql").write_text("create table b (id int);")

    applied = apply_migrations(empty_db, tmp_path)

    assert applied == ["001_first", "002_second"]
    assert applied_versions(empty_db) == {"001_first", "002_second"}


def test_apply_migrations_is_idempotent(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")

    assert apply_migrations(empty_db, tmp_path) == ["001_first"]
    assert apply_migrations(empty_db, tmp_path) == []


def test_failing_migration_rolls_back_and_is_not_recorded(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")
    (tmp_path / "002_bad.sql").write_text("this is not valid sql;")

    with pytest.raises(psycopg.Error):
        apply_migrations(empty_db, tmp_path)

    assert applied_versions(empty_db) == {"001_first"}
    with empty_db.cursor() as cur:
        cur.execute("select to_regclass('public.a'), to_regclass('public.b')")
        assert cur.fetchone() == ("a", None)
```

- [ ] **Step 6: Write `tests/conftest.py`**

```python
import os
from pathlib import Path

import psycopg
import pytest

from screener.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST is not set; see docs/plans for setup")
    return url


@pytest.fixture
def empty_db(db_url):
    """A connection to an empty public schema. No migrations applied."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        conn.execute("create schema public")
        yield conn


@pytest.fixture
def fresh_db(empty_db):
    """A connection to an empty public schema with all migrations applied."""
    apply_migrations(empty_db, MIGRATIONS)
    return empty_db
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `pip install -e ".[dev]" && pytest tests/test_migrate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.migrate'`

- [ ] **Step 8: Write `src/screener/migrate.py`**

```python
from pathlib import Path

import psycopg

_SCHEMA_MIGRATION = """
create table if not exists schema_migration (
    version    text primary key,
    applied_at timestamptz not null default now()
)
"""


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA_MIGRATION)


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """Versions already applied to this database."""
    _ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("select version from schema_migration")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(conn: psycopg.Connection, directory: Path) -> list[str]:
    """Apply every .sql file in `directory` not yet recorded, in filename order.

    Each migration runs in its own transaction: a failure rolls that file back
    and leaves it unrecorded, so a fixed migration can simply be re-run.
    """
    done = applied_versions(conn)
    newly_applied: list[str] = []

    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text()
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "insert into schema_migration (version) values (%s)", (version,)
            )
        newly_applied.append(version)

    return newly_applied
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `pytest tests/test_migrate.py -v`
Expected: 3 passed

- [ ] **Step 10: Commit**

```bash
touch migrations/.gitkeep
git add pyproject.toml .env.example src tests migrations
git commit -m "feat: project scaffolding and SQL migration runner"
```

---

### Task 2: Extensions and reference tables

**Files:**
- Create: `migrations/001_extensions.sql`
- Create: `migrations/002_reference.sql`
- Test: `tests/test_reference.py`

**Interfaces:**
- Consumes: `fresh_db` fixture from Task 1.
- Produces: tables `pillar(id smallint, code text, name text)`,
  `metric(id smallint, code, name, pillar_id, unit, higher_is_better, cadence, is_active)`,
  `sector_scheme(id smallint, code, name)`,
  `sector_node(id bigint, scheme_id, parent_id, level, code, name)`,
  `data_source(id smallint, code, name)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reference.py`:

```python
import pytest
import psycopg


def test_btree_gist_extension_is_installed(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute("select 1 from pg_extension where extname = 'btree_gist'")
        assert cur.fetchone() is not None


def test_metric_cadence_rejects_unknown_value(fresh_db):
    fresh_db.execute(
        "insert into pillar (code, name) values ('quality', 'Quality')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', (select id from pillar where code = 'quality'),
                    'ratio', true, 'hourly')
            """
        )


def test_sector_node_level_is_constrained_to_sector_or_industry(fresh_db):
    fresh_db.execute("insert into sector_scheme (code, name) values ('yfinance', 'yfinance')")
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values ((select id from sector_scheme where code = 'yfinance'), 3, 'x', 'X')
            """
        )


def test_sector_node_code_is_unique_within_a_scheme(fresh_db):
    fresh_db.execute("insert into sector_scheme (code, name) values ('yfinance', 'yfinance')")
    fresh_db.execute(
        """
        insert into sector_node (scheme_id, level, code, name)
        values ((select id from sector_scheme where code = 'yfinance'), 1, 'tech', 'Technology')
        """
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values ((select id from sector_scheme where code = 'yfinance'), 1, 'tech', 'Dup')
            """
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_reference.py -v`
Expected: FAIL — `relation "pillar" does not exist`

- [ ] **Step 3: Write `migrations/001_extensions.sql`**

```sql
-- btree_gist lets an exclusion constraint combine scalar equality (security_id with =)
-- with range overlap (daterange with &&). Available on Azure Flexible Server.
create extension if not exists btree_gist;
```

- [ ] **Step 4: Write `migrations/002_reference.sql`**

```sql
create table pillar (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);

create table metric (
    id               smallint generated always as identity primary key,
    code             text not null unique,
    name             text not null,
    pillar_id        smallint not null references pillar(id),
    unit             text not null,
    higher_is_better boolean not null,
    cadence          text not null check (cadence in ('daily', 'quarterly', 'event')),
    is_active        boolean not null default true
);
create index metric_pillar_id_idx on metric (pillar_id);

create table sector_scheme (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);

create table sector_node (
    id        bigint generated always as identity primary key,
    scheme_id smallint not null references sector_scheme(id),
    parent_id bigint references sector_node(id),
    level     smallint not null check (level in (1, 2)),
    code      text not null,
    name      text not null,
    unique (scheme_id, code)
);
create index sector_node_scheme_id_idx on sector_node (scheme_id);
create index sector_node_parent_id_idx on sector_node (parent_id);

create table data_source (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_reference.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add migrations/001_extensions.sql migrations/002_reference.sql tests/test_reference.py
git commit -m "feat: extensions and reference tables"
```

---

### Task 3: Security identity and temporal history

**Files:**
- Create: `migrations/003_security.sql`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `sector_scheme`, `sector_node` from Task 2.
- Produces: `security`, `security_symbol`, `security_sector`, `peer_group`.

This task proves the spec's claim that a partial unique index cannot prevent overlapping
validity periods, and that the exclusion constraint can.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_identity.py`:

```python
import pytest
import psycopg


@pytest.fixture
def security_id(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01')
            returning id
            """
        )
        return cur.fetchone()[0]


def test_currency_must_be_three_characters(fresh_db):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Bad', 'XNAS', 'DOLLAR', 'US', 'BAD', '2020-01-01')
            """
        )


def test_two_securities_cannot_hold_the_same_current_symbol(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'AAPL', 'XNAS', '2020-01-01', 'yfinance')
        """,
        (security_id,),
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Impostor', 'XNAS', 'USD', 'US', 'AAPL', '2021-01-01')
            returning id
            """
        )
        other = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into security_symbol (security_id, symbol, mic, valid_from, source)
            values (%s, 'AAPL', 'XNAS', '2021-01-01', 'yfinance')
            """,
            (other,),
        )


def test_a_retired_symbol_can_be_reissued_to_another_security(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Unrelated Co', 'XNAS', 'USD', 'US', 'FB', '2023-01-01')
            returning id
            """
        )
        other = cur.fetchone()[0]

    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'FB', 'XNAS', '2023-01-01', 'yfinance')
        """,
        (other,),
    )


def test_overlapping_symbol_periods_for_one_security_are_rejected(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    with pytest.raises(psycopg.errors.ExclusionViolation):
        fresh_db.execute(
            """
            insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
            values (%s, 'META', 'XNAS', '2022-01-01', '2023-01-01', 'yfinance')
            """,
            (security_id,),
        )


def test_adjacent_symbol_periods_are_allowed(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'META', 'XNAS', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )


def test_overlapping_sector_periods_are_rejected(fresh_db, security_id):
    with fresh_db.cursor() as cur:
        cur.execute("insert into sector_scheme (code, name) values ('yf', 'yf') returning id")
        scheme = cur.fetchone()[0]
        cur.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values (%s, 1, 'tech', 'Technology') returning id
            """,
            (scheme,),
        )
        tech = cur.fetchone()[0]
        cur.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values (%s, 1, 'comms', 'Communications') returning id
            """,
            (scheme,),
        )
        comms = cur.fetchone()[0]

    fresh_db.execute(
        """
        insert into security_sector (security_id, sector_node_id, valid_from, valid_to, source)
        values (%s, %s, '2020-01-01', '2024-01-01', 'yfinance')
        """,
        (security_id, tech),
    )
    with pytest.raises(psycopg.errors.ExclusionViolation):
        fresh_db.execute(
            """
            insert into security_sector (security_id, sector_node_id, valid_from, source)
            values (%s, %s, '2023-01-01', 'yfinance')
            """,
            (security_id, comms),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL — `relation "security" does not exist`

- [ ] **Step 3: Write `migrations/003_security.sql`**

```sql
create table security (
    id             bigint generated always as identity primary key,
    name           text not null,
    mic            text not null,
    currency       text not null check (length(currency) = 3),
    country        text not null check (length(country) = 2),
    cik            text,
    figi           text,
    primary_symbol text not null,
    is_active      boolean not null default true,
    first_seen     date not null,
    last_seen      date,
    created_at     timestamptz not null default now()
);

create table security_symbol (
    id          bigint generated always as identity primary key,
    security_id bigint not null references security(id),
    symbol      text not null,
    mic         text not null,
    valid_from  date not null,
    valid_to    date,
    source      text not null,
    constraint security_symbol_no_overlap
        exclude using gist (
            security_id with =,
            daterange(valid_from, valid_to, '[)') with &&
        )
);
create index security_symbol_security_id_idx on security_symbol (security_id);
create unique index security_symbol_current_uq
    on security_symbol (symbol, mic) where valid_to is null;

create table security_sector (
    id             bigint generated always as identity primary key,
    security_id    bigint not null references security(id),
    sector_node_id bigint not null references sector_node(id),
    valid_from     date not null,
    valid_to       date,
    source         text not null,
    constraint security_sector_no_overlap
        exclude using gist (
            security_id with =,
            daterange(valid_from, valid_to, '[)') with &&
        )
);
create index security_sector_security_id_idx on security_sector (security_id);
create index security_sector_node_idx on security_sector (sector_node_id);
create unique index security_sector_current_uq
    on security_sector (security_id) where valid_to is null;

create table peer_group (
    id             bigint generated always as identity primary key,
    scheme_id      smallint not null references sector_scheme(id),
    sector_node_id bigint references sector_node(id),
    level          smallint not null check (level in (0, 1, 2)),
    code           text not null,
    unique (scheme_id, code)
);
create index peer_group_sector_node_idx on peer_group (sector_node_id);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_identity.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add migrations/003_security.sql tests/test_identity.py
git commit -m "feat: security identity with temporal symbol and sector history"
```

---

### Task 4: Weights and scoring runs

**Files:**
- Create: `migrations/004_scoring.sql`
- Test: `tests/test_scoring_runs.py`

**Interfaces:**
- Consumes: `pillar` from Task 2.
- Produces: `weight_version`, `pillar_weight`, `scoring_logic_version`, `scoring_run`.
  `scoring_run` carries `as_of_range daterange`, `cutoff_offset interval`, `status`,
  `emits_alerts`, `git_sha`, `config_hash`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scoring_runs.py`:

```python
import pytest
import psycopg


@pytest.fixture
def versions(fresh_db):
    """A weight version and a logic version to hang runs off."""
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into weight_version (code) values ('v1') returning id"
        )
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('initial') returning id"
        )
        logic = cur.fetchone()[0]
    return weight, logic


def _insert_run(conn, versions, start, end, status, emits_alerts=False):
    weight, logic = versions
    conn.execute(
        """
        insert into scoring_run (
            as_of_range, cutoff_offset, logic_version_id, weight_version_id,
            status, emits_alerts, git_sha, config_hash, started_at, outcome
        )
        values (daterange(%s, %s, '[)'), '1 day 6 hours', %s, %s,
                %s, %s, 'abc123', '\\x00'::bytea, now(), 'ok')
        """,
        (start, end, logic, weight, status, emits_alerts),
    )


def test_two_overlapping_live_runs_are_rejected(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")


def test_a_backfill_may_overlap_a_live_run(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "backfill")


def test_two_experiments_may_overlap_each_other(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-02-01", "experiment")
    _insert_run(fresh_db, versions, "2026-01-01", "2026-02-01", "experiment")


def test_non_overlapping_live_runs_are_allowed(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    _insert_run(fresh_db, versions, "2026-01-02", "2026-01-03", "live")


def test_status_rejects_unknown_value(fresh_db, versions):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "provisional")


def test_pillar_weights_reject_negative_values(fresh_db, versions):
    weight, _ = versions
    fresh_db.execute("insert into pillar (code, name) values ('quality', 'Quality')")
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into pillar_weight (weight_version_id, pillar_id, weight)
            values (%s, (select id from pillar where code = 'quality'), -0.5)
            """,
            (weight,),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_scoring_runs.py -v`
Expected: FAIL — `relation "weight_version" does not exist`

- [ ] **Step 3: Write `migrations/004_scoring.sql`**

```sql
create table weight_version (
    id         bigint generated always as identity primary key,
    code       text not null unique,
    note       text,
    created_at timestamptz not null default now()
);

-- Weights are stored raw and normalised at read time by dividing by the version's
-- sum. "Weights sum to 1" cannot be expressed cleanly as a table constraint and
-- would eventually be violated; normalising on use makes it impossible to lie.
create table pillar_weight (
    weight_version_id bigint not null references weight_version(id),
    pillar_id         smallint not null references pillar(id),
    weight            numeric not null check (weight >= 0),
    primary key (weight_version_id, pillar_id)
);
create index pillar_weight_pillar_idx on pillar_weight (pillar_id);

create table scoring_logic_version (
    id            smallint generated always as identity primary key,
    description   text not null,
    introduced_at timestamptz not null default now()
);

create table scoring_run (
    id                bigint generated always as identity primary key,
    as_of_range       daterange not null,
    -- Visible facts when scoring date D are those with
    -- observed_at <= D + cutoff_offset. An offset rather than a timestamp so
    -- live and backfill runs evaluate the identical expression.
    cutoff_offset     interval not null,
    logic_version_id  smallint not null references scoring_logic_version(id),
    weight_version_id bigint not null references weight_version(id),
    status            text not null check (status in ('live', 'backfill', 'experiment')),
    emits_alerts      boolean not null,
    git_sha           text not null,
    config_hash       bytea not null,
    started_at        timestamptz not null,
    finished_at       timestamptz,
    outcome           text not null check (outcome in ('running', 'ok', 'failed')),
    supersedes_run_id bigint references scoring_run(id),
    note              text,
    constraint scoring_run_one_live_per_date
        exclude using gist (as_of_range with &&) where (status = 'live')
);
create index scoring_run_logic_idx on scoring_run (logic_version_id);
create index scoring_run_weight_idx on scoring_run (weight_version_id);
create index scoring_run_supersedes_idx on scoring_run (supersedes_run_id);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_scoring_runs.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add migrations/004_scoring.sql tests/test_scoring_runs.py
git commit -m "feat: weight versions and scoring runs with one-live-run-per-date constraint"
```

---

### Task 5: Ingest and the bitemporal fact layer

**Files:**
- Create: `migrations/005_facts.sql`
- Test: `tests/test_point_in_time.py`

**Interfaces:**
- Consumes: `security` (Task 3), `metric`, `data_source` (Task 2).
- Produces: `ingest_run`, `ingest_observation`, `fundamental_fact`, `price_daily`
  (yearly partitions), `corporate_action`.

This task settles the spec's claim about date-cast semantics empirically. The test asserting
that a bare date excludes same-day observations is the one that matters — if it fails, the
spec's reasoning is wrong and the cutoff design needs revisiting before anything is built on it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_point_in_time.py`:

```python
import pytest
import psycopg

PIT_QUERY = """
select distinct on (security_id, metric_id, period_end) value
from fundamental_fact
where security_id = %s
  and observed_at <= (%s::date + %s::interval)
order by security_id, metric_id, period_end, observed_at desc
"""


@pytest.fixture
def fact_setup(fresh_db):
    """One security, one metric, one ingest observation to hang facts off."""
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into pillar (code, name) values ('quality', 'Q') returning id")
        pillar = cur.fetchone()[0]
        cur.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', %s, 'ratio', true, 'quarterly') returning id
            """,
            (pillar,),
        )
        metric = cur.fetchone()[0]
        cur.execute("insert into data_source (code, name) values ('yf', 'yfinance') returning id")
        source = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_run (source_id, endpoint, started_at, status)
            values (%s, 'fundamentals', now(), 'ok') returning id
            """,
            (source,),
        )
        run = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_observation
                (ingest_run_id, security_id, fetched_at, content_hash, blob_path, is_new_payload)
            values (%s, %s, now(), '\\x00'::bytea, 'yf/fundamentals/2026-01-01/1.json.gz', true)
            returning id
            """,
            (run, security),
        )
        observation = cur.fetchone()[0]
    return {"security": security, "metric": metric, "observation": observation}


def _insert_fact(conn, setup, value, observed_at, period_end="2025-12-31"):
    conn.execute(
        """
        insert into fundamental_fact
            (security_id, metric_id, period_end, period_type, value,
             observed_at, ingest_observation_id)
        values (%s, %s, %s, 'Q', %s, %s, %s)
        """,
        (setup["security"], setup["metric"], period_end, value,
         observed_at, setup["observation"]),
    )


def test_a_bare_date_cutoff_excludes_same_day_observations(fresh_db, fact_setup):
    """The defect the spec's cutoff_offset exists to prevent.

    Comparing observed_at against a bare date casts it to midnight at the START
    of that day, so a fact learned at 02:00 on D is invisible when scoring D.
    """
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute(
            "select count(*) from fundamental_fact where observed_at <= %s::date",
            ("2026-02-10",),
        )
        assert cur.fetchone()[0] == 0


def test_cutoff_offset_includes_the_overnight_fetch(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-02-10", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [0.25]


def test_point_in_time_read_returns_the_latest_observation_at_the_cutoff(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    _insert_fact(fresh_db, fact_setup, 0.31, "2026-03-15 02:00+00")  # restatement

    with fresh_db.cursor() as cur:
        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-02-10", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [0.25]

        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-03-20", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [0.31]


def test_a_restatement_does_not_overwrite_the_original(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    _insert_fact(fresh_db, fact_setup, 0.31, "2026-03-15 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from fundamental_fact")
        assert cur.fetchone()[0] == 2


def test_the_same_fact_cannot_be_recorded_twice_at_one_instant(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")


def test_price_rows_route_to_the_correct_yearly_partition(fresh_db, fact_setup):
    fresh_db.execute(
        """
        insert into price_daily
            (security_id, trade_date, open, high, low, close, volume,
             observed_at, ingest_observation_id)
        values (%s, '2026-02-10', 1, 2, 0.5, 1.5, 1000, now(), %s)
        """,
        (fact_setup["security"], fact_setup["observation"]),
    )
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from price_daily_2026")
        assert cur.fetchone()[0] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_point_in_time.py -v`
Expected: FAIL — `relation "ingest_run" does not exist`

- [ ] **Step 3: Write `migrations/005_facts.sql`**

```sql
create table ingest_run (
    id                   bigint generated always as identity primary key,
    source_id            smallint not null references data_source(id),
    endpoint             text not null,
    started_at           timestamptz not null,
    finished_at          timestamptz,
    status               text not null
                           check (status in ('running', 'ok', 'partial', 'failed')),
    securities_requested int,
    securities_ok        int,
    error                text
);
create index ingest_run_source_id_idx on ingest_run (source_id);

-- Deliberately NOT partitioned. A partitioned table's unique constraint must
-- include the partition key, which would force a composite primary key and
-- break the simple foreign keys the traceability chain depends on.
create table ingest_observation (
    id             bigint generated always as identity primary key,
    ingest_run_id  bigint not null references ingest_run(id),
    security_id    bigint not null references security(id),
    fetched_at     timestamptz not null,
    content_hash   bytea not null,
    blob_path      text not null,
    is_new_payload boolean not null,
    payload_bytes  int
);
create index ingest_observation_run_idx on ingest_observation (ingest_run_id);
create index ingest_observation_security_idx
    on ingest_observation (security_id, fetched_at desc);

create table fundamental_fact (
    id                    bigint generated always as identity primary key,
    security_id           bigint not null references security(id),
    metric_id             smallint not null references metric(id),
    period_end            date not null,
    period_type           text not null check (period_type in ('Q', 'A', 'TTM')),
    value                 numeric not null,
    currency              text check (length(currency) = 3),
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id),
    restates_id           bigint references fundamental_fact(id),
    unique (security_id, metric_id, period_end, period_type, observed_at)
);
create index fundamental_fact_pit_idx
    on fundamental_fact (security_id, metric_id, period_end, observed_at desc);
create index fundamental_fact_obs_idx on fundamental_fact (ingest_observation_id);
create index fundamental_fact_restates_idx on fundamental_fact (restates_id);

create table price_daily (
    security_id           bigint not null references security(id),
    trade_date            date not null,
    open                  numeric not null,
    high                  numeric not null,
    low                   numeric not null,
    close                 numeric not null,
    volume                bigint not null,
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id),
    primary key (security_id, trade_date)
) partition by range (trade_date);
create index price_daily_obs_idx on price_daily (ingest_observation_id);

create table price_daily_2026 partition of price_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table corporate_action (
    id                    bigint generated always as identity primary key,
    security_id           bigint not null references security(id),
    effective_date        date not null,
    action_type           text not null
                            check (action_type in ('split', 'dividend', 'spinoff')),
    ratio                 numeric,
    amount                numeric,
    currency              text check (length(currency) = 3),
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id)
);
create index corporate_action_security_idx on corporate_action (security_id, effective_date);
create index corporate_action_obs_idx on corporate_action (ingest_observation_id);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_point_in_time.py -v`
Expected: 6 passed

If `test_a_bare_date_cutoff_excludes_same_day_observations` FAILS, stop and report it. The
spec's cutoff reasoning rests on that semantics; a different result means the design needs
revisiting rather than patching.

- [ ] **Step 5: Commit**

```bash
git add migrations/005_facts.sql tests/test_point_in_time.py
git commit -m "feat: bitemporal fact layer with interval-offset point-in-time reads"
```

---

### Task 6: Derived daily layer and the partitioned-FK invariant

**Files:**
- Create: `migrations/006_derived.sql`
- Test: `tests/test_derived.py`

**Interfaces:**
- Consumes: `security`, `metric`, `peer_group`, `pillar`, `scoring_run`, `fundamental_fact`.
- Produces: `metric_daily` (yearly partitions), `pillar_score_daily`, `snapshot_daily`,
  `event_flag_daily`, `peer_group_stat`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_derived.py`:

```python
import pytest
import psycopg


def test_metric_daily_is_range_partitioned_on_as_of(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select partstrat
            from pg_partitioned_table
            where partrelid = 'metric_daily'::regclass
            """
        )
        assert cur.fetchone()[0] == "r"


def test_yearly_and_monthly_partitions_coexist_on_one_parent(fresh_db):
    """The spec defers monthly partitioning by mixing granularity, never converting."""
    fresh_db.execute(
        """
        create table metric_daily_2027_01 partition of metric_daily
            for values from ('2027-01-01') to ('2027-02-01')
        """
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select count(*) from pg_inherits where inhparent = 'metric_daily'::regclass
            """
        )
        assert cur.fetchone()[0] >= 2


def test_a_single_column_foreign_key_into_snapshot_daily_is_rejected(fresh_db):
    """Records the invariant: nothing holds an FK into a partitioned table.

    Postgres 12+ permits an FK referencing a partitioned table, but only against a
    unique constraint containing the partition key — and snapshot_daily has no
    surrogate id to point at.
    """
    with pytest.raises(psycopg.errors.InvalidForeignKey):
        fresh_db.execute(
            """
            create table would_break (
                id bigint generated always as identity primary key,
                snapshot_security_id bigint references snapshot_daily(security_id)
            )
            """
        )


@pytest.fixture
def derived_setup(fresh_db):
    """Real rows for every foreign key the derived tables require.

    Without these the inserts below would trip a foreign-key violation, and the
    test would pass for the wrong reason.
    """
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into pillar (code, name) values ('quality', 'Q') returning id")
        pillar = cur.fetchone()[0]
        cur.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', %s, 'ratio', true, 'quarterly') returning id
            """,
            (pillar,),
        )
        metric = cur.fetchone()[0]
        cur.execute("insert into sector_scheme (code, name) values ('yf', 'yf') returning id")
        scheme = cur.fetchone()[0]
        cur.execute(
            """
            insert into peer_group (scheme_id, level, code)
            values (%s, 0, 'market') returning id
            """,
            (scheme,),
        )
        peer_group = cur.fetchone()[0]
        cur.execute("insert into weight_version (code) values ('v1') returning id")
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('initial') returning id"
        )
        logic = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            )
            values (daterange('2026-02-10', '2026-02-11', '[)'), '1 day 6 hours', %s, %s,
                    'live', true, 'abc123', '\\x00'::bytea, now(), 'ok')
            returning id
            """,
            (logic, weight),
        )
        run = cur.fetchone()[0]
    return {
        "security": security,
        "metric": metric,
        "peer_group": peer_group,
        "run": run,
    }


def test_peer_group_stat_requires_eleven_deciles(fresh_db, derived_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into peer_group_stat
                (as_of, scoring_run_id, peer_group_id, metric_id, member_count, deciles)
            values ('2026-02-10', %s, %s, %s, 30, array[1, 2, 3]::numeric[])
            """,
            (derived_setup["run"], derived_setup["peer_group"], derived_setup["metric"]),
        )


def test_eleven_deciles_are_accepted(fresh_db, derived_setup):
    fresh_db.execute(
        """
        insert into peer_group_stat
            (as_of, scoring_run_id, peer_group_id, metric_id, member_count, deciles)
        values ('2026-02-10', %s, %s, %s, 30,
                array[0,1,2,3,4,5,6,7,8,9,10]::numeric[])
        """,
        (derived_setup["run"], derived_setup["peer_group"], derived_setup["metric"]),
    )


def test_percentile_outside_zero_to_one_hundred_is_rejected(fresh_db, derived_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into metric_daily
                (as_of, scoring_run_id, security_id, metric_id, raw_value,
                 percentile, peer_group_id, peer_count, fallback_level)
            values ('2026-02-10', %s, %s, %s, 0.25, 101, %s, 30, 2)
            """,
            (derived_setup["run"], derived_setup["security"],
             derived_setup["metric"], derived_setup["peer_group"]),
        )


def test_snapshot_daily_has_no_weight_version_column(fresh_db):
    """Redundant with scoring_run; a second copy would eventually disagree."""
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select count(*) from information_schema.columns
            where table_name = 'snapshot_daily' and column_name = 'weight_version_id'
            """
        )
        assert cur.fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_derived.py -v`
Expected: FAIL — `relation "metric_daily" does not exist`

- [ ] **Step 3: Write `migrations/006_derived.sql`**

```sql
-- Yearly partitions, not monthly. Partitioning at all is non-negotiable
-- (retrofitting is a full rewrite), but at the starting universe size monthly
-- would mean ~100 near-empty partitions. Granularity is never switched: once the
-- universe passes ~1,000 tickers, start creating monthly partitions from the next
-- year boundary and leave earlier years coarse. Boundaries align, so nothing is
-- rebuilt. There is deliberately no default partition — attaching a new partition
-- alongside one requires a full scan.
create table metric_daily (
    as_of               date not null,
    scoring_run_id      bigint not null references scoring_run(id),
    security_id         bigint not null references security(id),
    metric_id           smallint not null references metric(id),
    raw_value           numeric not null,
    percentile          numeric not null check (percentile between 0 and 100),
    peer_group_id       bigint not null references peer_group(id),
    peer_count          int not null,
    fallback_level      smallint not null check (fallback_level in (0, 1, 2)),
    fundamental_fact_id bigint references fundamental_fact(id),
    period_end          date,
    primary key (as_of, scoring_run_id, security_id, metric_id)
) partition by range (as_of);

create index metric_daily_history_idx on metric_daily (security_id, metric_id, as_of desc);
create index metric_daily_run_idx on metric_daily (scoring_run_id);
create index metric_daily_fact_idx on metric_daily (fundamental_fact_id);
create index metric_daily_peer_group_idx on metric_daily (peer_group_id);
create index metric_daily_metric_idx on metric_daily (metric_id);

create table metric_daily_2026 partition of metric_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table pillar_score_daily (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    security_id    bigint not null references security(id),
    pillar_id      smallint not null references pillar(id),
    score          numeric not null,
    metric_count   smallint not null,
    coverage       numeric not null check (coverage between 0 and 1),
    primary key (as_of, scoring_run_id, security_id, pillar_id)
) partition by range (as_of);
create index pillar_score_daily_run_idx on pillar_score_daily (scoring_run_id);
create index pillar_score_daily_security_idx on pillar_score_daily (security_id, as_of desc);
create index pillar_score_daily_pillar_idx on pillar_score_daily (pillar_id);

create table pillar_score_daily_2026 partition of pillar_score_daily
    for values from ('2026-01-01') to ('2027-01-01');

-- blended_score is a materialised derivation, not ground truth: stored so the
-- nightly crossing diff need not aggregate pillar rows. The run carries the
-- weight version, so there is deliberately no weight_version_id column here.
create table snapshot_daily (
    as_of                date not null,
    scoring_run_id       bigint not null references scoring_run(id),
    security_id          bigint not null references security(id),
    blended_score        numeric not null,
    pillar_agreement     smallint not null,
    min_coverage         numeric not null,
    worst_fallback_level smallint not null,
    primary key (as_of, scoring_run_id, security_id)
) partition by range (as_of);
create index snapshot_daily_run_idx on snapshot_daily (scoring_run_id);
create index snapshot_daily_security_idx on snapshot_daily (security_id, as_of desc);

create table snapshot_daily_2026 partition of snapshot_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table event_flag_daily (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    security_id    bigint not null references security(id),
    flag_code      text not null,
    severity       smallint not null,
    detail         jsonb,
    primary key (as_of, scoring_run_id, security_id, flag_code)
) partition by range (as_of);
create index event_flag_daily_run_idx on event_flag_daily (scoring_run_id);
create index event_flag_daily_security_idx on event_flag_daily (security_id, as_of desc);
create index event_flag_daily_code_idx on event_flag_daily (flag_code, as_of desc);

create table event_flag_daily_2026 partition of event_flag_daily
    for values from ('2026-01-01') to ('2027-01-01');

-- A cache, not a source of truth: the exact distribution is recoverable from
-- metric_daily. deciles[1] and deciles[11] are the min and max.
create table peer_group_stat (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    peer_group_id  bigint not null references peer_group(id),
    metric_id      smallint not null references metric(id),
    member_count   int not null,
    deciles        numeric[] not null check (array_length(deciles, 1) = 11),
    primary key (as_of, scoring_run_id, peer_group_id, metric_id)
);
create index peer_group_stat_run_idx on peer_group_stat (scoring_run_id);
create index peer_group_stat_group_idx on peer_group_stat (peer_group_id);
create index peer_group_stat_metric_idx on peer_group_stat (metric_id);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_derived.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add migrations/006_derived.sql tests/test_derived.py
git commit -m "feat: derived daily layer with yearly partitions"
```

---

### Task 7: Alerting

**Files:**
- Create: `migrations/007_alerting.sql`
- Test: `tests/test_alerting.py`

**Interfaces:**
- Consumes: `security`, `scoring_run`.
- Produces: `alert_rule`, `alert_event`, `alert_state`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alerting.py`:

```python
import pytest
import psycopg


@pytest.fixture
def alert_setup(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into weight_version (code) values ('v1') returning id")
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('initial') returning id"
        )
        logic = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            )
            values (daterange('2026-02-10', '2026-02-11', '[)'), '1 day 6 hours', %s, %s,
                    'live', true, 'abc123', '\\x00'::bytea, now(), 'ok')
            returning id
            """,
            (logic, weight),
        )
        run = cur.fetchone()[0]
        cur.execute(
            """
            insert into alert_rule (code, name, condition_type, params, cooldown_days, min_coverage)
            values ('cross80', 'Score crosses 80', 'score_crossing',
                    '{"threshold": 80}'::jsonb, 5, 0.6)
            returning id
            """
        )
        rule = cur.fetchone()[0]
    return {"security": security, "run": run, "rule": rule}


def _fire(conn, setup, as_of="2026-02-10"):
    conn.execute(
        """
        insert into alert_event
            (alert_rule_id, security_id, as_of, scoring_run_id, fired_at,
             blended_score, previous_blended_score, pillar_scores, raw_inputs,
             driver, delivery_status)
        values (%s, %s, %s, %s, now(), 82, 68,
                '{"quality": 91}'::jsonb, '{"roic": 0.25}'::jsonb,
                'three upward revisions in 5 days', 'pending')
        """,
        (setup["rule"], setup["security"], as_of, setup["run"]),
    )


def test_the_same_alert_cannot_fire_twice_for_one_date(fresh_db, alert_setup):
    """Makes a re-run of the daily job idempotent rather than double-posting."""
    _fire(fresh_db, alert_setup)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _fire(fresh_db, alert_setup)


def test_previous_blended_score_may_be_null_for_non_crossing_rules(fresh_db, alert_setup):
    fresh_db.execute(
        """
        insert into alert_event
            (alert_rule_id, security_id, as_of, scoring_run_id, fired_at,
             blended_score, pillar_scores, raw_inputs, driver, delivery_status)
        values (%s, %s, '2026-02-10', %s, now(), 82,
                '{"insider": 77}'::jsonb, '{"net_buying": 1200000}'::jsonb,
                'insider buying appeared', 'pending')
        """,
        (alert_setup["rule"], alert_setup["security"], alert_setup["run"]),
    )


def test_undelivered_alerts_are_findable(fresh_db, alert_setup):
    _fire(fresh_db, alert_setup)
    with fresh_db.cursor() as cur:
        cur.execute(
            "select count(*) from alert_event where delivery_status <> 'sent'"
        )
        assert cur.fetchone()[0] == 1


def test_alert_state_direction_is_constrained(fresh_db, alert_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into alert_state
                (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
                 cooldown_until, last_direction)
            values (%s, %s, now(), '2026-02-10', '2026-02-15', 0)
            """,
            (alert_setup["rule"], alert_setup["security"]),
        )


def test_alert_state_holds_one_row_per_rule_and_security(fresh_db, alert_setup):
    fresh_db.execute(
        """
        insert into alert_state
            (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
             cooldown_until, last_direction)
        values (%s, %s, now(), '2026-02-10', '2026-02-15', 1)
        """,
        (alert_setup["rule"], alert_setup["security"]),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into alert_state
                (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
                 cooldown_until, last_direction)
            values (%s, %s, now(), '2026-02-11', '2026-02-16', -1)
            """,
            (alert_setup["rule"], alert_setup["security"]),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_alerting.py -v`
Expected: FAIL — `relation "alert_rule" does not exist`

- [ ] **Step 3: Write `migrations/007_alerting.sql`**

```sql
create table alert_rule (
    id             bigint generated always as identity primary key,
    code           text not null unique,
    name           text not null,
    enabled        boolean not null default true,
    condition_type text not null
                     check (condition_type in ('score_crossing', 'pillar_flip',
                                               'revision_cluster', 'insider_buying',
                                               'valuation_band')),
    params         jsonb not null,
    cooldown_days  smallint not null,
    min_coverage   numeric not null
);

-- The frozen record of what fired and why. JSONB is right here and nowhere else
-- in the schema: an immutable document, never queried across rows, answering
-- "why did this alert?" in a single row read.
--
-- The unique constraint makes the daily job idempotent. It also means a backfill
-- run would collide, which is the desired outcome — but it is a backstop, not the
-- mechanism: the alerting step must be skipped entirely when the run has
-- emits_alerts = false, not attempted and left to fail. Postgres cannot express
-- that as a cross-table check; the code owns it.
create table alert_event (
    id                     bigint generated always as identity primary key,
    alert_rule_id          bigint not null references alert_rule(id),
    security_id            bigint not null references security(id),
    as_of                  date not null,
    scoring_run_id         bigint not null references scoring_run(id),
    fired_at               timestamptz not null,
    blended_score          numeric not null,
    previous_blended_score numeric,
    pillar_scores          jsonb not null,
    raw_inputs             jsonb not null,
    driver                 text not null,
    event_flags            jsonb,
    delivery_status        text not null
                             check (delivery_status in ('pending', 'sent', 'failed')),
    delivery_attempts      smallint not null default 0,
    delivered_at           timestamptz,
    delivery_error         text,
    unique (alert_rule_id, security_id, as_of)
);
create index alert_event_security_idx on alert_event (security_id, as_of desc);
create index alert_event_rule_idx on alert_event (alert_rule_id);
create index alert_event_run_idx on alert_event (scoring_run_id);
create index alert_event_pending_idx
    on alert_event (fired_at) where delivery_status <> 'sent';

-- last_direction exists because a cooldown that suppresses the OPPOSITE crossing
-- is wrong: a score crossing up, then genuinely collapsing back, is exactly the
-- event worth hearing about. Cooldown suppresses repetition, not reversal.
create table alert_state (
    alert_rule_id    bigint not null references alert_rule(id),
    security_id      bigint not null references security(id),
    last_fired_at    timestamptz not null,
    last_fired_as_of date not null,
    cooldown_until   date not null,
    last_direction   smallint not null check (last_direction in (-1, 1)),
    primary key (alert_rule_id, security_id)
);
create index alert_state_security_idx on alert_state (security_id);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_alerting.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add migrations/007_alerting.sql tests/test_alerting.py
git commit -m "feat: alerting tables with idempotent firing and directional cooldown"
```

---

### Task 8: Partition pre-creation

**Files:**
- Create: `src/screener/partitions.py`
- Test: `tests/test_partitions.py`

**Interfaces:**
- Consumes: all partitioned tables from Tasks 5 and 6.
- Produces:
  - `screener.partitions.PARTITIONED_TABLES: tuple[str, ...]`
  - `screener.partitions.partition_name(table: str, year: int) -> str`
  - `screener.partitions.ensure_partitions(conn, through_year: int) -> list[str]` returning the
    partitions newly created, in order.

The daily job calls `ensure_partitions(conn, date.today().year + 1)` so next year's partitions
exist well before they are needed. Fifteen lines of SQL beats a `pg_partman` dependency, and it
fails loudly at a time someone is watching.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_partitions.py`:

```python
from screener.partitions import ensure_partitions, partition_name


def test_partition_name_is_table_and_year():
    assert partition_name("metric_daily", 2027) == "metric_daily_2027"


def test_ensure_partitions_creates_missing_years(fresh_db):
    created = ensure_partitions(fresh_db, through_year=2027)

    assert "metric_daily_2027" in created
    assert "snapshot_daily_2027" in created
    assert "price_daily_2027" in created
    assert "metric_daily_2026" not in created  # created by the migration


def test_ensure_partitions_is_idempotent(fresh_db):
    ensure_partitions(fresh_db, through_year=2027)
    assert ensure_partitions(fresh_db, through_year=2027) == []


def test_rows_route_into_a_newly_created_partition(fresh_db):
    ensure_partitions(fresh_db, through_year=2027)
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into weight_version (code) values ('v1') returning id")
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('x') returning id"
        )
        logic = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            )
            values (daterange('2027-03-01', '2027-03-02', '[)'), '1 day 6 hours', %s, %s,
                    'live', true, 'abc', '\\x00'::bytea, now(), 'ok')
            returning id
            """,
            (logic, weight),
        )
        run = cur.fetchone()[0]
        cur.execute(
            """
            insert into snapshot_daily
                (as_of, scoring_run_id, security_id, blended_score,
                 pillar_agreement, min_coverage, worst_fallback_level)
            values ('2027-03-01', %s, %s, 82, 3, 0.8, 2)
            """,
            (run, security),
        )
        cur.execute("select count(*) from snapshot_daily_2027")
        assert cur.fetchone()[0] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_partitions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener.partitions'`

- [ ] **Step 3: Write `src/screener/partitions.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_partitions.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the whole suite**

Run: `pytest -v`
Expected: 42 passed

- [ ] **Step 6: Record the tooling in CLAUDE.md**

Replace the "When adding tooling" section of `CLAUDE.md` with:

```markdown
## Commands

- Install: `pip install -e ".[dev]"`
- Tests: `pytest` — needs `DATABASE_URL_TEST` pointing at a throwaway Postgres 16
  (the suite drops and recreates the `public` schema on every test).
- Single test: `pytest tests/test_identity.py::test_overlapping_symbol_periods_for_one_security_are_rejected -v`
- Apply migrations: `python -c "import psycopg, pathlib; from screener.migrate import apply_migrations; from screener.config import settings; conn = psycopg.connect(settings().database_url, autocommit=True); print(apply_migrations(conn, pathlib.Path('migrations')))"`

Migrations are plain numbered SQL in `migrations/`, applied in filename order and
recorded in `schema_migration`. Each runs in its own transaction, so a failure leaves
it unrecorded and the fixed file can be re-run.
```

- [ ] **Step 7: Commit**

```bash
git add src/screener/partitions.py tests/test_partitions.py CLAUDE.md
git commit -m "feat: yearly partition pre-creation"
```

---

## Verification

After Task 8, the whole suite passing means these spec claims are demonstrated rather than
asserted:

| Claim | Test |
|---|---|
| A bare-date cutoff excludes same-day observations | `test_a_bare_date_cutoff_excludes_same_day_observations` |
| The interval offset includes the overnight fetch | `test_cutoff_offset_includes_the_overnight_fetch` |
| Restatements do not overwrite history | `test_a_restatement_does_not_overwrite_the_original` |
| Two live runs cannot overlap | `test_two_overlapping_live_runs_are_rejected` |
| A partial index cannot stop overlapping validity; exclusion can | `test_overlapping_symbol_periods_for_one_security_are_rejected` |
| Retired symbols can be reissued | `test_a_retired_symbol_can_be_reissued_to_another_security` |
| Yearly and monthly partitions coexist on one parent | `test_yearly_and_monthly_partitions_coexist_on_one_parent` |
| Nothing can hold a single-column FK into a partitioned table | `test_a_single_column_foreign_key_into_snapshot_daily_is_rejected` |
| A re-run cannot double-fire an alert | `test_the_same_alert_cannot_fire_twice_for_one_date` |

## Out of scope

Ingest clients, scoring computation, the peer-group fallback walk, Discord delivery, and Blob
payload writing. Each needs its own spec before a plan. This plan delivers the schema and the
evidence that its constraints behave as designed.
