"""The connector: the protocol, the OAuth flow, and what the role may read.

The security tests here are not decoration. An MCP server is a database on the
public internet with an OAuth server in front of it, and most of what follows
exists because a specific attack works without it — each of those says which.
"""

import base64
import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from screener import auth, playground
from screener.mcp import config as mcp_config
from screener.mcp import oauth, protocol, store, tools

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "018_mcp.sql"
PASSWORD = "throwaway-for-this-test"
SECRET = "mcp-tests-session-secret"
BASE = "http://127.0.0.1:9"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"

# Denied to the connector, and the reason. `test_every_table_is_granted_or_denied`
# reads the grant list out of the migration, so this file cannot be the stale
# half of the pair — what it holds is the argument, not the inventory.
DENIED = {
    "auth.app_user": "who may sign in",
    "auth.session": "token_hash is authentication material",
    "audit.event": "identities, conversation transcripts and spend",
    "mcp.client": "who registered, and where a code may be sent",
    "mcp.authorization": "one-time codes and their PKCE challenges",
    "mcp.token": "the connector's own access and refresh material",
}


def granted_in_migration() -> set[str]:
    text = MIGRATION.read_text()
    public = text.split("grant select on", 1)[1].split("to playground_mcp", 1)[0]
    names = {
        n
        for n in re.findall(r"[a-z_][a-z0-9_]*", re.sub(r"--[^\n]*", "", public))
        if n not in {"grant", "select", "on", "to"}
    }
    skybird = set(re.findall(r"skybird\.([a-z_]+)", text))
    return {f"public.{n}" for n in names} | {f"skybird.{s}" for s in skybird}


@pytest.fixture
def connector(fresh_db, db_url, monkeypatch):
    """The connector's role, provisioned the way a deployment provisions it."""
    monkeypatch.setenv("PLAYGROUND_MCP_DB_PASSWORD", PASSWORD)
    playground.ensure_password(fresh_db)
    url = make_conninfo(db_url, user="playground_mcp", password=PASSWORD)
    # Both, because the api container really does hold both: the console reads
    # the first and the connector the second, in one process.
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", url)
    monkeypatch.setenv("PLAYGROUND_MCP_DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ellis")
    monkeypatch.setenv("APP_BASE_URL", BASE)
    return url


@pytest.fixture
def token(connector, fresh_db):
    """A live access token for `ellis`, made the way the token endpoint makes one."""
    client_id = store.register(fresh_db, client_name="Claude", redirect_uris=(CLAUDE,))
    access = "access-token-for-tests"
    store.save_token(
        fresh_db,
        family_id=store.new_family_id(),
        token_hash=auth.token_hash(access, SECRET, purpose=oauth.ACCESS),
        refresh_hash=auth.token_hash("refresh-for-tests", SECRET, purpose=oauth.REFRESH),
        client_id=client_id,
        login="ellis",
        resource=f"{BASE}/mcp",
        scope=mcp_config.SCOPE,
        access_ttl=3600,
        refresh_ttl=99999,
    )
    return access


def rpc(body: bytes, actor: str = "ellis") -> tuple[int, dict | None]:
    status, answer = protocol.handle(body, actor=actor)
    return status, (json.loads(answer) if answer else None)


def ask(method: str, params=None, request_id=1, actor="ellis") -> tuple[int, dict]:
    """A request, which by definition gets an answer. Notifications use `rpc`."""
    message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    status, answer = rpc(json.dumps(message).encode(), actor)
    assert answer is not None
    return status, answer


# -- the protocol ------------------------------------------------------------


def test_initialize_echoes_a_version_we_speak(connector):
    _, answer = ask("initialize", {"protocolVersion": "2025-06-18"})
    assert answer["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in answer["result"]["capabilities"]


def test_initialize_offers_our_own_version_when_the_client_asks_for_one_we_do_not(
    connector,
):
    # The spec's rule: answer with the latest we support and let the client
    # decide, rather than refusing and leaving it with nothing to try.
    _, answer = ask("initialize", {"protocolVersion": "1999-01-01"})
    assert answer["result"]["protocolVersion"] == mcp_config.DEFAULT_VERSION


def test_a_notification_gets_an_empty_202_and_no_body(connector):
    status, answer = rpc(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    )
    # Answering a notification is itself a protocol error, so the body must be
    # absent rather than merely empty-ish.
    assert (status, answer) == (202, None)


def test_tools_list_names_every_tool_with_a_schema(connector):
    _, answer = ask("tools/list")
    listed = answer["result"]["tools"]
    assert {t["name"] for t in listed} == set(tools.BY_NAME)
    assert all(t["inputSchema"]["type"] == "object" for t in listed)
    assert all(t["description"] for t in listed)


def test_an_unknown_method_is_a_json_rpc_error_not_an_http_one(connector):
    status, answer = ask("tools/destroy")
    # 200: the transport worked perfectly and the call did not. A client that
    # sees 500 here decides the network is broken and retries.
    assert status == 200
    assert answer["error"]["code"] == protocol.METHOD_NOT_FOUND


def test_a_batch_is_refused_rather_than_half_supported(connector):
    status, answer = rpc(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]')
    assert status == 400 and answer is not None
    assert answer["error"]["code"] == protocol.INVALID_REQUEST


def test_malformed_json_comes_back_in_a_json_rpc_envelope(connector):
    status, answer = rpc(b"{not json")
    assert status == 400 and answer is not None
    assert answer["error"]["code"] == protocol.PARSE_ERROR
    assert answer["id"] is None


def test_an_unknown_tool_names_itself_rather_than_failing_silently(connector):
    _, answer = ask("tools/call", {"name": "rm", "arguments": {}})
    assert answer["error"]["code"] == protocol.INVALID_PARAMS


# -- the tools ---------------------------------------------------------------


def test_query_returns_columns_and_rows(connector):
    _, answer = ask(
        "tools/call", {"name": "query", "arguments": {"sql": "select 1 as n"}}
    )
    body = json.loads(answer["result"]["content"][0]["text"])
    assert body["columns"] == ["n"] and body["rows"] == [[1]]
    assert answer["result"]["isError"] is False


def test_a_bad_table_name_comes_back_with_a_pointer_to_list_tables(connector):
    _, answer = ask(
        "tools/call", {"name": "query", "arguments": {"sql": "select * from nope"}}
    )
    body = json.loads(answer["result"]["content"][0]["text"])
    assert "list_tables" in body["error"]


def test_a_write_through_the_query_tool_is_refused(connector):
    _, answer = ask(
        "tools/call",
        {"name": "query", "arguments": {"sql": "insert into security default values"}},
    )
    body = json.loads(answer["result"]["content"][0]["text"])
    assert "error" in body


def test_list_tables_shows_the_grant_and_nothing_else(connector):
    _, answer = ask("tools/call", {"name": "list_tables", "arguments": {}})
    body = json.loads(answer["result"]["content"][0]["text"])
    named = {t["name"] for t in body["tables"]}
    assert not named & {d.split(".")[-1] for d in DENIED}
    assert "price_daily" in named


def test_the_shaped_tools_bind_their_arguments_rather_than_building_sql(connector):
    # The argument that would end the run if it were concatenated. It comes back
    # as no rows, because it is a search term and was always only ever a search
    # term.
    _, answer = ask(
        "tools/call",
        {
            "name": "reddit_chatter",
            "arguments": {"term": "'; drop table security; --"},
        },
    )
    body = json.loads(answer["result"]["content"][0]["text"])
    assert body["row_count"] == 0
    # And the table it named is still there.
    assert playground.run("select count(*) from security").row_count == 1


def test_a_percent_in_a_literal_is_a_query_error_not_a_dead_database(connector):
    # psycopg scans a parameterised statement for placeholders and raises before
    # Postgres is reached, with no SQLSTATE. The old rule read "no SQLSTATE" as
    # "the server is unreachable", so a typo in this repo's own SQL would have
    # told the caller Postgres was down.
    with pytest.raises(playground.QueryError):
        playground.select("select 'a%' as bad, %s as n", [1])


def test_price_history_says_so_rather_than_returning_nothing(connector):
    _, answer = ask(
        "tools/call", {"name": "price_history", "arguments": {"symbol": "NOSUCH"}}
    )
    body = json.loads(answer["result"]["content"][0]["text"])
    assert body["rows"] == [] and "ingest" in body["note"]


def test_every_tool_call_lands_in_the_audit_trail(connector, fresh_db):
    ask("tools/call", {"name": "query", "arguments": {"sql": "select 1"}}, actor="ellis")
    row = fresh_db.execute(
        "select actor, operation, detail from audit.event "
        "where operation = 'mcp.query' order by id desc limit 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "ellis"
    # The query itself, not merely that one happened. "Which query did it run"
    # is the first question anybody asks about a connector on a database.
    assert row[2]["arguments"]["sql"] == "select 1"


# -- the role ----------------------------------------------------------------


def refused(statement: str) -> str:
    """The message the connector's role produced, through the engine it uses."""
    with pytest.raises(playground.QueryError) as caught:
        playground.run(statement)
    return caught.value.message


def test_the_connector_cannot_read_sign_in_or_the_audit_trail(connector):
    for table in DENIED:
        assert "permission denied" in refused(f"select * from {table}")


def test_the_connector_cannot_write(connector):
    # Two answers, and both matter. Through the engine an INSERT never reaches
    # the question of permission: `DECLARE ... CURSOR FOR` accepts a SELECT or a
    # VALUES and nothing else, so it dies as a syntax error first.
    assert 'syntax error at or near "insert"' in refused(
        "insert into security default values"
    )

    # And underneath that, on a plain connection as the role, it is refused
    # again — which is the one that would still hold if the cursor ever went.
    with psycopg.connect(connector) as conn:
        with pytest.raises(psycopg.Error) as caught:
            conn.execute("insert into security default values")
    assert "read-only" in str(caught.value)


def test_the_connector_is_not_a_superuser(connector):
    # The application's own connection *is* one, and `pg_read_file` returns on
    # it. That is the entire reason this role exists.
    assert "permission denied" in refused("select pg_read_file('/etc/hostname')")
    with psycopg.connect(connector) as conn:
        row = conn.execute(
            "select usesuper from pg_user where usename = current_user"
        ).fetchone()
    assert row is not None and row[0] is False


def test_a_second_statement_is_refused_and_the_table_survives(connector):
    with pytest.raises(playground.QueryError):
        playground.run("select 1; drop table security")
    assert playground.run("select to_regclass('security')").rows[0][0] is not None


def test_every_table_is_granted_or_deliberately_denied(connector, fresh_db):
    rows = fresh_db.execute(
        """
        select n.nspname || '.' || c.relname
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r', 'p') and not c.relispartition
          and n.nspname not in ('pg_catalog', 'information_schema')
          and n.nspname not like 'pg\\_%'
        """
    ).fetchall()
    assert {r[0] for r in rows} == granted_in_migration() | set(DENIED)


def test_the_connector_can_read_transcripts_and_that_is_deliberate(connector):
    # Steven cannot, by migration 017, because he works a capture's controls and
    # should not read one back. This caller is the reason the schema is granted
    # at all, and it is the line to revoke if that stops being wanted.
    assert playground.run("select count(*) from skybird.transcript_segment").row_count == 1


# -- OAuth: the parts an attacker would try -----------------------------------


def test_registration_refuses_an_attackers_callback(connector):
    # The whole authorization endpoint rests on this. Registration is
    # unauthenticated by necessity, so without it an attacker registers a client
    # called "Claude" pointing at their own server, sends Ellis a link, and the
    # SameSite=Lax cookie rides a top-level navigation into a consent screen he
    # has every reason to accept. PKCE does not help: the attacker chose the
    # challenge.
    response = oauth.register(json.dumps({"redirect_uris": ["https://evil.test/cb"]}).encode())
    assert response.status == 400
    assert json.loads(response.body)["error"] == oauth.INVALID_REQUEST


def test_registration_accepts_claude_and_a_loopback(connector):
    for uri in (CLAUDE, "http://127.0.0.1:3118/callback"):
        response = oauth.register(json.dumps({"redirect_uris": [uri]}).encode())
        assert response.status == 201, uri
        assert "client_secret" not in json.loads(response.body)


def test_authorize_refuses_a_redirect_the_client_never_registered(connector, fresh_db):
    client_id = store.register(fresh_db, client_name="Claude", redirect_uris=(CLAUDE,))
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://evil.test/cb",
            "code_challenge": "x",
            "code_challenge_method": "S256",
        }
    )
    response = oauth.authorize(query, "ellis", True, secret=SECRET)
    assert response.status == 400
    # Rendered, never redirected: bouncing to an unverified address is how an
    # open redirect becomes a stolen code.
    assert response.headers == ()


def test_a_login_off_the_allow_list_never_reaches_consent(connector):
    response = oauth.authorize("", "stranger", False, secret=SECRET)
    assert response.status == 403


def test_the_local_development_login_cannot_mint_a_connector_token(connector):
    # `/auth/local` issues a real session without proving anything, which is
    # right for a laptop. It must never be a way to reach a database from the
    # internet, so it is refused by name rather than left to the allow-list.
    assert oauth.authorize("", oauth.LOCAL_LOGIN, True, secret=SECRET).status == 403
    assert oauth.approve({}, oauth.LOCAL_LOGIN, True).status == 403


def test_a_signed_out_visitor_is_sent_to_sign_in_and_back(connector):
    response = oauth.authorize("client_id=x", None, False, secret=SECRET)
    assert response.status == 302
    location = dict(response.headers)["Location"]
    assert location.startswith("/auth/login?next=")
    assert urllib.parse.unquote(location).endswith("/oauth/authorize?client_id=x")


# -- OAuth: the full flow -----------------------------------------------------


def _verifier_and_challenge():
    verifier = "a" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authorize(fresh_db, challenge: str, login: str = "ellis") -> str:
    """Walk register → consent → code, and return the code."""
    registered = json.loads(
        oauth.register(json.dumps({"redirect_uris": [CLAUDE]}).encode()).body
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": registered["client_id"],
            "redirect_uri": CLAUDE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque",
            "resource": f"{BASE}/mcp",
        }
    )
    granted = oauth.approve({"payload": [query]}, login, True)
    assert granted.status == 302
    location = dict(granted.headers)["Location"]
    assert "state=opaque" in location
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]


def _token(form: dict) -> tuple[int, dict]:
    response = oauth.token(urllib.parse.urlencode(form).encode())
    return response.status, json.loads(response.body)


def test_the_whole_flow_ends_in_a_working_token(connector, fresh_db):
    verifier, challenge = _verifier_and_challenge()
    code = _authorize(fresh_db, challenge)
    status, issued = _token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE,
            "code_verifier": verifier,
        }
    )
    assert status == 200 and issued["token_type"] == "Bearer"
    who = oauth.bearer(f"Bearer {issued['access_token']}")
    assert who.login == "ellis"


def test_the_wrong_pkce_verifier_is_refused(connector, fresh_db):
    _, challenge = _verifier_and_challenge()
    code = _authorize(fresh_db, challenge)
    status, body = _token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE,
            "code_verifier": "b" * 64,
        }
    )
    assert status == 400 and body["error"] == oauth.INVALID_GRANT


def test_a_code_cannot_be_redeemed_twice(connector, fresh_db):
    verifier, challenge = _verifier_and_challenge()
    code = _authorize(fresh_db, challenge)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE,
        "code_verifier": verifier,
    }
    assert _token(form)[0] == 200
    status, body = _token(form)
    assert status == 400 and body["error"] == oauth.INVALID_GRANT


def test_a_replayed_refresh_token_revokes_the_whole_family(connector, fresh_db):
    # Rotation on its own detects nothing: if a refresh token is stolen and the
    # thief spends it first, the real client's next attempt just fails and looks
    # like a bad network. Recognising the *second* use is what turns a silent
    # theft into a visible disconnection.
    verifier, challenge = _verifier_and_challenge()
    code = _authorize(fresh_db, challenge)
    _, first = _token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE,
            "code_verifier": verifier,
        }
    )
    _, second = _token(
        {"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}
    )
    assert second["access_token"] != first["access_token"]

    # The thief replays the one they took.
    status, body = _token(
        {"grant_type": "refresh_token", "refresh_token": first["refresh_token"]}
    )
    assert status == 400 and body["error"] == oauth.INVALID_GRANT

    # And the token the legitimate client is holding stops working too, which is
    # the point: a compromised chain is compromised at both ends.
    with pytest.raises(oauth.Unauthorized):
        oauth.bearer(f"Bearer {second['access_token']}")


def test_a_token_for_another_audience_is_refused(connector, fresh_db):
    # RFC 8707. Without this the server accepts any token it ever issued for any
    # resource, which is the token passthrough the MCP spec forbids by name.
    client_id = store.register(fresh_db, client_name="c", redirect_uris=(CLAUDE,))
    store.save_token(
        fresh_db,
        family_id=store.new_family_id(),
        token_hash=auth.token_hash("elsewhere", SECRET, purpose=oauth.ACCESS),
        refresh_hash=auth.token_hash("elsewhere-r", SECRET, purpose=oauth.REFRESH),
        client_id=client_id,
        login="ellis",
        resource="https://someone-else.example/mcp",
        scope=mcp_config.SCOPE,
        access_ttl=3600,
        refresh_ttl=3600,
    )
    with pytest.raises(oauth.Unauthorized):
        oauth.bearer("Bearer elsewhere")


def test_a_login_removed_from_the_allow_list_loses_its_connector(token, monkeypatch):
    assert oauth.bearer(f"Bearer {token}").login == "ellis"
    # A token lives thirty days of refreshes and a session lives one. Without
    # re-checking, taking somebody off the allow-list would close the dashboard
    # and leave the database open.
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "someone-else")
    with pytest.raises(oauth.Unauthorized):
        oauth.bearer(f"Bearer {token}")


def test_no_token_carries_the_pointer_a_client_needs_to_find_the_flow(connector):
    with pytest.raises(oauth.Unauthorized) as caught:
        oauth.bearer(None)
    header = caught.value.header()
    assert 'resource_metadata="' in header
    assert header.startswith("Bearer ")


def test_an_mcp_token_is_not_also_a_browser_session(connector, fresh_db, token):
    # Domain separation. Without a purpose label the two hashes are identical,
    # so a row copied from `mcp.token` into `auth.session` would be a working
    # cookie — and this application connects as the cluster superuser.
    assert auth.resolve_session(fresh_db, token, SECRET) is None
    assert auth.token_hash(token, SECRET) != auth.token_hash(
        token, SECRET, purpose=oauth.ACCESS
    )


# -- discovery ---------------------------------------------------------------


def test_the_protected_resource_document_names_this_server_exactly(connector):
    body = json.loads(oauth.protected_resource_document().body)
    # Must equal the URL as typed into Claude, because that string is what comes
    # back as `resource` and what every token is checked against.
    assert body["resource"] == f"{BASE}/mcp" == mcp_config.resource()
    assert body["authorization_servers"] == [BASE]


def test_the_authorization_server_document_advertises_what_claude_requires(connector):
    body = json.loads(oauth.authorization_server_document().body)
    # Claude checks for S256 before starting, and registers as a public client
    # with no secret to authenticate with.
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "none" in body["token_endpoint_auth_methods_supported"]
    assert body["registration_endpoint"].endswith("/oauth/register")
    assert body["issuer"] == BASE


def test_token_responses_are_never_cached(connector, fresh_db):
    verifier, challenge = _verifier_and_challenge()
    code = _authorize(fresh_db, challenge)
    response = oauth.token(
        urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE,
                "code_verifier": verifier,
            }
        ).encode()
    )
    assert ("Cache-Control", "no-store") in response.headers


# -- over HTTP ---------------------------------------------------------------


@pytest.fixture
def server(connector, monkeypatch):
    from screener.health import build_server

    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    running = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{running.server_address[1]}"
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=5)


def call(url, method="GET", data=None, headers=None):
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_a_call_without_a_token_is_401_and_says_where_to_authorize(server):
    status, headers, _ = call(
        f"{server}/mcp", "POST", b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    )
    assert status == 401
    # Claude reads this header to discover the authorization server, and ignores
    # it on any other status. Without it the connection dies at "couldn't reach".
    assert "resource_metadata=" in headers["WWW-Authenticate"]


def test_the_endpoint_answers_json_and_never_a_stream(server, token):
    status, headers, body = call(
        f"{server}/mcp",
        "POST",
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert status == 200
    # The whole reason this works behind a Cloudflare tunnel: cloudflared
    # buffers server-sent events, and a transport that never opens a stream
    # cannot be buffered.
    assert headers["Content-Type"] == "application/json"
    assert "event-stream" not in headers["Content-Type"]
    assert len(json.loads(body)["result"]["tools"]) == len(tools.TOOLS)


def test_a_get_is_405_rather_than_a_stream_nobody_offers(server):
    status, headers, _ = call(f"{server}/mcp")
    assert status == 405 and headers["Allow"] == "POST"


def test_delete_is_405_rather_than_the_stdlib_501(server):
    # Without `do_DELETE` the stdlib answers 501 with an HTML body and closes
    # the connection, which is a different and less true statement than "this
    # server is stateless".
    assert call(f"{server}/mcp", "DELETE")[0] == 405


def test_head_works_so_a_redirect_check_means_something(server):
    # `curl -I` is what Claude's own troubleshooting recommends for checking the
    # MCP URL does not redirect. Without `do_HEAD` every path answered 501 and
    # the check said nothing about the server.
    status, _, body = call(f"{server}/health", "HEAD")
    assert status == 200 and body == b""


def test_the_discovery_documents_are_reachable_without_a_session(server):
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
    ):
        status, _, body = call(f"{server}{path}")
        assert status == 200, path
        assert json.loads(body), path


def test_an_unsupported_protocol_version_is_refused_in_the_right_envelope(server, token):
    status, _, body = call(
        f"{server}/mcp",
        "POST",
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        {"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "1999-01-01"},
    )
    assert status == 400
    assert json.loads(body)["jsonrpc"] == "2.0"


def test_an_oversized_body_is_refused_as_json_rpc_not_as_this_services_own_shape(
    server, token
):
    status, _, body = call(
        f"{server}/mcp",
        "POST",
        b"x" * (mcp_config.MAX_BODY + 1),
        {"Authorization": f"Bearer {token}"},
    )
    assert status == 413
    # A JSON-RPC client reads `error.code`; `{"error": "over N bytes"}` would
    # read to it as a broken server rather than as a refusal.
    assert json.loads(body)["error"]["code"] == protocol.INVALID_REQUEST


def test_the_preflight_an_inspector_sends_is_answered(server):
    status, headers, _ = call(f"{server}/mcp", "OPTIONS")
    assert status == 204
    assert "WWW-Authenticate" in headers["Access-Control-Expose-Headers"]


def test_the_connector_reports_itself_off_without_a_role(server, monkeypatch):
    monkeypatch.delenv("PLAYGROUND_MCP_DATABASE_URL")
    status, _, body = call(
        f"{server}/mcp", "POST", b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    )
    assert status == 503
    assert "role" in json.loads(body)["error"]["message"]
