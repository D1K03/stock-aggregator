"""What the deployed stack hands each container.

One test, and it exists because of a bug it would have caught. The bot ran in
production for its whole life without `DATABASE_URL`, and nothing said so: the
spend cap fails open when the trail cannot be read, memory comes back empty when
there is nothing to read it from, and an audit trail with no Discord in it looks
exactly like a quiet day. The first thing to fail out loud was a skybird tool,
months of design later.

So the list below is the argument, written down. A service that opens a database
connection has to be in it, and a service that does not has to be kept out of it
on purpose -- the same shape as the playground's deny list in
`tests/test_playground.py`, and for the same reason: a future service should
have to be argued about before it is silently either.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "compose.prod.yaml"

# Every service that runs our code and reads the database.
NEEDS_DATABASE = {
    "api": "migrations, sign-in, the audit trail and every dashboard read",
    "bot": "the spend cap, Steven's memory, the audit trail, the skybird tools",
    "reddit": "social_item, and the ingest_run rows beside it",
    "skybird": "the control plane -- it polls for a row and writes transcripts",
}

# Every service that must NOT be handed one, each for a stated reason.
NO_DATABASE = {
    "app": "caddy, and it proxies bytes",
    "web": "holds no credentials and never opens a database connection",
    "transcribe": "audio in, text out; it calls nothing outside its own process",
    "postgres": "is the database",
}


def services() -> dict[str, str]:
    """Each top-level service name mapped to its block of the file.

    A two-space key under `services:` starts a block and the next one ends it,
    which is all the structure this needs. Deliberately not PyYAML: it is not a
    dependency of this project and adding one to assert a fact about a text file
    would be the wrong trade.
    """
    text = COMPOSE.read_text()
    body = text.split("\nservices:\n", 1)[1]
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([a-z][a-z0-9-]*):$", body, re.M)]
    found = {}
    for i, (at, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        found[name] = body[at:end]
    return found


def test_every_service_is_either_given_a_database_or_deliberately_not():
    # So a service added later fails this suite until someone has decided which
    # side of the line it is on, rather than inheriting whichever the template
    # it was copied from happened to have.
    assert set(services()) == set(NEEDS_DATABASE) | set(NO_DATABASE)


def test_the_services_that_read_the_database_are_told_where_it_is():
    # The bug itself. `bot` was absent from here for its whole life, and every
    # symptom of that was something quietly not happening.
    missing = [
        name for name in NEEDS_DATABASE if "DATABASE_URL:" not in services()[name]
    ]
    assert not missing, f"{missing} read the database and are not told where it is"


def test_nothing_else_is_handed_a_database_url():
    # The other half, and the one that keeps the list honest: a credential
    # handed to a container that has no use for it is a credential in one more
    # place than it needs to be.
    extra = [name for name in NO_DATABASE if "DATABASE_URL:" in services()[name]]
    assert not extra, f"{extra} are given a database they do not open"


# -- the two read-only roles ------------------------------------------------
#
# `screener.playground` is one engine with two callers, and after migration 017
# they connect as two roles: the dashboard's console as `playground`, which can
# read skybird, and Steven's `sql` tool as `playground_bot`, which cannot.
#
# Neither is chosen in Python. Both read `PLAYGROUND_DATABASE_URL` and compose
# hands them different ones, so the whole split rests on each container holding
# only its own credential — which is what these assert.


def test_the_console_and_the_bot_connect_as_different_roles():
    api, bot = services()["api"], services()["bot"]
    assert "postgresql://playground:" in api
    assert "postgresql://playground_bot:" in bot


def test_neither_container_can_authenticate_as_the_other_role():
    # The one that matters. A shared password would make the two roles a
    # formality: whoever holds it can connect as either, and the enforcement
    # would be back to which URL the Python picked.
    api, bot = services()["api"], services()["bot"]
    assert "PLAYGROUND_BOT_DB_PASSWORD}@" not in api, "api can become the bot role"
    assert "PLAYGROUND_DB_PASSWORD}@" not in bot, "the bot can become the console role"


def test_the_bot_is_never_handed_the_consoles_password():
    # Subtler than it looks, and the reason it is asserted rather than assumed.
    # `PLAYGROUND_DB_PASSWORD` lives in `.env` so compose can interpolate the
    # api's URL from it — and the bot has `env_file: .env`, which would then
    # inject that same password into the one container that must not hold it.
    # An `environment:` entry wins over `env_file:`, so the empty override is
    # what actually closes it. Deleting that line silently re-opens the door.
    assert 'PLAYGROUND_DB_PASSWORD: ""' in services()["bot"]

    # Nor the connector's, which is a third role again.
    assert 'PLAYGROUND_MCP_DB_PASSWORD: ""' in services()["bot"]

    # The api holds all three, and only because provisioning runs there — beside
    # the migrations, under the same advisory lock. It never builds a URL from
    # the bot's password.
    assert "PLAYGROUND_BOT_DB_PASSWORD:" in services()["api"]
    assert "postgresql://playground_bot:" not in services()["api"]


def test_the_connector_reaches_the_api_rather_than_the_dashboard():
    # Every one of these is fixed by a spec or by what gets typed into Claude,
    # so none of them can live under /api/ where the existing handle would catch
    # it. Without a line each, Caddy's catch-all sends them to Next.js, which
    # answers 307 to /login — and Claude drops the Authorization header across a
    # redirect, so the whole flow fails as an authorization error with nothing
    # here to explain it.
    caddyfile = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text()
    for path in ("/mcp", "/mcp/*", "/oauth/*", "/.well-known/*"):
        assert f"handle {path} {{" in caddyfile, path

    # And ahead of the catch-all, which is the whole point of listing them.
    assert caddyfile.index("handle /mcp {") < caddyfile.index("handle {")


def test_the_connector_runs_as_its_own_role():
    # The grant list in `018_mcp.sql` is what decides what claude.ai can read,
    # and it only decides anything if the process actually connects as that
    # role rather than reusing the console's URL.
    api = services()["api"]
    assert "postgresql://playground_mcp:" in api
    assert "PLAYGROUND_MCP_DATABASE_URL:" in api
