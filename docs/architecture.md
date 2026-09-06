# The shape of it

Nine areas of the system, drawn. `docs/infrastructure.md` says what each piece
is for and when to reach for it, `DESIGN.md` says why it was chosen, and
`deploy/README.md` says how to operate it. This is the one that says where
things sit and what touches what.

Everything here depicts code that runs today, with one exception: most of the v1
pipeline is unbuilt, and the parts that do not exist yet are drawn with a dashed
border and labelled `«not built»`. `PLAN.md` owns the roadmap — this file only
draws it so the tables and packages below have somewhere to land.

---

## What runs, and what talks to what

Nine containers in one compose project, sharing a VPS with four other stacks.
Four facts do most of the work in this picture:

- **Nothing publishes a host port in production.** Caddy reaches the services
  over the compose network and the tunnel reaches Caddy, so there is no port to
  collide with the neighbouring stacks and the database has no public surface.
- **`cloudflared` dials outward.** The Cloudflare edge never dials in. That is
  the whole ingress story, and it is why there is no firewall rule to maintain.
- **`api`, `bot` and `reddit` are the same image** with different commands. One
  dependency set, one build; the cost is that the api image carries `discord.py`
  without importing it.
- **`transcribe` and `skybird` are not**, and that is the rule the pair above is
  the exception to. A separate image is what a separate dependency set earns:
  ctranslate2, onnxruntime and PyAV for one, ffmpeg and yt-dlp for the other,
  and neither the status service nor the bot has any use for either.

```mermaid
flowchart LR
    internet(["The internet"])
    claude(["claude.ai<br/>custom connector"])
    edge["Cloudflare edge<br/>TLS + Access policy"]

    subgraph vps["VPS — compose project stock-aggregator"]
        cfd["cloudflared"]
        app["app<br/>caddy:2-alpine<br/>expose 8080"]
        api["api<br/>ghcr.io/d1k03/stock-aggregator<br/>python -m screener.boot"]
        web["web<br/>ghcr.io/d1k03/stock-aggregator-web<br/>node server.js"]
        bot["bot<br/>same image as api<br/>python -m screener.bot"]
        tr["transcribe<br/>ghcr.io/d1k03/stock-aggregator-transcribe<br/>faster-whisper, expose 8081"]
        sky["skybird<br/>ghcr.io/d1k03/stock-aggregator-skybird<br/>yt-dlp + ffmpeg, no port"]
        rdt["reddit<br/>same image as api<br/>python -m screener.reddit"]
        pg[("postgres:16<br/>named volume pg_data")]
    end

    infisical["Infisical"]
    ghoauth["GitHub OAuth"]
    router["OpenRouter"]
    dgw["Discord gateway"]
    drest["Discord REST v10"]
    streams["YouTube / Twitch"]
    arctic["Arctic Shift"]

    internet --> edge
    claude -->|"MCP over HTTPS, bearer token"| edge
    cfd -->|"dials outward, never in"| edge
    cfd --> app
    app -->|"/auth/* /health /ready /status /api/*"| api
    app -->|"/mcp /oauth/* /.well-known/*"| api
    app -->|"everything else"| web
    bot -->|"POST /api/render"| web
    api -->|"POST /transcribe"| tr
    bot -->|"POST /transcribe"| tr
    sky -->|"POST /transcribe"| tr
    sky -->|"audio only, via yt-dlp"| streams
    rdt -->|"posts + comments"| arctic
    api --> pg
    bot --> pg
    sky --> pg
    rdt --> pg
    api --> infisical
    api --> ghoauth
    api --> router
    api --> drest
    bot --> infisical
    bot --> router
    bot --> dgw
    rdt --> infisical
```

Two edges are the ones people get wrong.

**`/api/*` belongs to the Python service, not to Next.js.** Caddy routes it to
`api:8080` ahead of the catch-all, so the dashboard's `/api/ask`, `/api/handoff`
and `/api/audit` are all answered by `screener.health`. Next.js owns exactly one
route of its own, `/api/render`, and Caddy never sends anything there — it is
unreachable from the internet by construction rather than by a check. **Do not**
add a Next.js API route expecting it to be routable.

**`bot → web` is a real dependency.** The bot posts a chart's SVG to
`web:3000/api/render` and gets a PNG back, because `web/lib/chart-svg.ts` is the
only place a chart is drawn and the Discord image must not drift from the one on
the dashboard.

`web` itself is static and unwired: its only environment variable is `NODE_ENV`,
it holds no credentials, and it never opens a database connection. `bot` has no
port, no Caddy route and no healthcheck — discord.py reconnects with its own
backoff, and a check that cannot tell "reconnecting" from "wedged" would restart
a bot that was about to recover.

**`skybird` has no port either, and no edge pointing at it.** It is the only
service nothing calls: it reads what to do from Postgres, pulls audio, posts it
to the transcriber and writes lines back. That is what "the database is the
control plane" means in this picture — the arrow from the dashboard to a running
capture goes through `pg`, not across the network. It has no healthcheck for the
same reason the bot has none, and it holds a Postgres advisory lock so a second
copy stands by rather than capturing the same stream twice.

**`api` answers claude.ai as well as the browser.** The connector is not a
container of its own, and could not be: its OAuth consent screen needs the
session cookie the status service issues, so the two share a process. That
process therefore holds three read-only database passwords and picks the role
per call, which is the one place in this system where the separation between
roles is a branch rather than a credential. `docs/infrastructure.md` says so
where it describes the connector.

Its three paths need `handle` blocks of their own because none of them can live
under `/api/*`: `/.well-known/*` is fixed by RFC 9728 and RFC 8414, and `/mcp`
is what somebody types into Claude. Without them the catch-all sends all three
to Next.js, which redirects, and an `Authorization` header does not survive a
redirect.

**`transcribe` has no Caddy route on purpose.** `/transcribe` matches the
catch-all, which goes to Next.js, so nothing on the internet resolves to it. The
browser reaches it through `api`, which is what holds the session.

---

## Fetching, and where Bright Data sits

`screener.fetch` is one function over an ordered list of strategies, first
success wins. **The order is whatever the caller passes** — there is no fixed
chain, and the only default is `DEFAULT_STRATEGIES = ("direct",)` in
`fetch/chain.py`, with a test enforcing it. Every proxy path has to be asked for
by name, so depending on Bright Data is a visible decision in a diff rather than
something a default turned on quietly.

```mermaid
flowchart TD
    entry["fetch(url, strategies)<br/>strategies defaults to ('direct',)"]
    next{"Next name in the caller's list"}
    known{"Registered in STRATEGIES?"}
    direct["direct<br/>plain httpx + BROWSER_HEADERS<br/>free"]
    isp["isp_proxy — Bright Data ISP proxy<br/>brd.superproxy.io:44445, verify=False<br/>fresh -session- suffix draws a new exit IP<br/>bandwidth off a flat monthly plan"]
    unl["unlocker — Bright Data Web Unlocker<br/>POST api.brightdata.com/request<br/>Bearer auth, zone + format raw<br/>priced per successful request"]
    ok{"2xx with a non-empty body,<br/>and validate() happy?"}
    win["FetchResult<br/>attempts = every name tried"]
    err["errors.append — the failure is kept, not raised"]
    more{"Any strategy left?"}
    fail["FetchError<br/>all strategies failed for this url"]

    entry --> next
    next --> known
    known -->|"no — unknown strategy, not counted as an attempt"| err
    known -->|direct| direct
    known -->|isp_proxy| isp
    known -->|unlocker| unl
    direct --> ok
    isp --> ok
    unl --> ok
    ok -->|yes| win
    ok -->|"no — EmptyResponse, StrategyUnavailable, or any exception at all"| err
    err --> more
    more -->|yes| next
    more -->|no| fail
```

Three behaviours the diagram encodes and prose would bury:

- **A 2xx with an empty body is a failure** and demotes to the next strategy,
  unless the caller passes `allow_empty=True`. More than one provider answers a
  rate-limited request that way, and the fact layer is append-only, so an empty
  payload written once is there for good.
- **An unconfigured strategy raises `StrategyUnavailable` from inside itself**
  rather than being silently skipped, so "no Bright Data credentials set" lands
  in the error list beside genuine failures instead of vanishing.
- **There is no retry and no backoff. The chain is the retry.** `except
  Exception` in the loop is deliberately broad — the next strategy is the
  handler.

`unlocker` is registered and reachable but has no production caller today, and
**do not** put it first in a chain: it is the only strategy with a per-call
price. `universe/sources/yahoo.py` is the one module that deliberately bypasses
`fetch()` altogether, because a Yahoo crumb is only valid alongside the cookie
issued with it and `fetch()` builds a fresh client per call, which would discard
the jar.

---

## The packages

Each piece is its own package with a small public surface through `__init__.py`,
and nothing outside imports a submodule directly. Solid edges are eager imports;
**dashed edges are deferred imports inside a function**, which is the mechanism
that keeps `discord.py` out of the status service's import graph.

```mermaid
flowchart TD
    env["config.env<br/>optional / required / text / integer"]
    settings["config.settings"]
    fetch["fetch"]
    ai["ai"]
    notify["notify"]
    auth["auth"]
    botcfg["bot.config"]
    audit["audit"]

    env --> settings
    env --> fetch
    env --> ai
    env --> notify
    env --> auth
    env --> botcfg

    secrets["secrets<br/>stdlib urllib only"]
    prov["provenance"]
    concept["concept<br/>delete when ingest lands"]
    migrate["migrate + partitions"]
    checks["health.checks"]

    boot["boot.startup"]
    selftest["boot.selftest"]
    health["health.server"]
    bot["bot.agent + bot.client"]
    tools["bot.tools"]
    universe["universe"]
    blobs["blobs<br/>local + s3, hand-rolled SigV4"]
    ingest["ingest<br/>prices + sweep"]
    skybird["skybird<br/>store + platforms only"]

    boot --> settings
    boot --> secrets
    boot --> migrate
    boot --> health
    boot -.->|lazy| selftest

    selftest --> ai
    selftest --> fetch
    selftest --> notify
    selftest --> botcfg
    selftest --> checks
    selftest --> prov

    health --> settings
    health --> audit
    health --> auth
    health --> checks
    health --> prov
    health -.->|"lazy, inside a request handler"| bot

    bot --> ai
    bot --> audit
    bot --> prov
    bot --> botcfg
    bot --> tools

    tools --> concept
    tools --> skybird
    tools --> audit
    tools --> checks
    tools --> prov

    universe --> settings
    universe --> fetch

    env --> blobs
    ingest --> settings
    ingest --> secrets
    ingest --> fetch
    ingest --> blobs

    audit --> settings
    health -.->|"lazy, inside a request handler"| skybird
    skybird --> settings
```

Two asymmetries are worth reading off it.

**`screener.notify` has exactly one consumer**, `boot/selftest.py`, and even
that only reads its configuration. Nothing sends an alert, because the alerting
code is not written yet — the package is a delivery mechanism waiting for
something to deliver.

**`bot` and `health` depend on each other**, but on different leaves and in
different ways. `health.server` reaches `bot.agent`, `bot.handoff` and
`bot.budget` only from inside request handlers; `bot/tools/deployment.py`
imports `health.checks` eagerly. Neither direction pulls the other's heavy
dependency.

---

## The database

Roughly twenty tables across three schemas. `public` holds the screener's own
data model; `auth` and `audit` are separate because no scoring query joins to
them, and because the test suite's `drop schema public cascade` should keep
meaning exactly what it says. The design and its reasoning are in
`docs/specs/2026-09-04-database-schema-design.md`.

### Reference and identity

```mermaid
erDiagram
    sector_scheme ||--o{ sector_node : scheme_id
    sector_node ||--o{ sector_node : "parent_id, level 1 to level 2"
    sector_scheme ||--o{ peer_group : scheme_id
    sector_node |o--o{ peer_group : "sector_node_id, null = market-wide"
    sector_node ||--o{ security_sector : sector_node_id
    security ||--o{ security_symbol : security_id
    security ||--o{ security_sector : security_id
    pillar ||--o{ metric : pillar_id

    security {
        bigint id PK
        text primary_symbol "denormalised convenience only"
        text cik "nullable, and deliberately not unique"
        boolean is_active
        date first_seen
        date last_seen "null while a member"
    }
    security_symbol {
        bigint id PK
        bigint security_id FK
        text symbol
        text mic
        date valid_from
        date valid_to "null = current"
    }
    security_sector {
        bigint id PK
        bigint security_id FK
        bigint sector_node_id FK
        date valid_from
        date valid_to "null = current"
    }
    sector_node {
        bigint id PK
        smallint scheme_id FK
        bigint parent_id FK "self, nullable"
        smallint level "1 = sector, 2 = industry"
        text code
    }
    peer_group {
        bigint id PK
        smallint scheme_id FK
        bigint sector_node_id FK "null = the level 0 market group"
        smallint level "0, 1 or 2"
    }
    metric {
        smallint id PK
        smallint pillar_id FK
        boolean higher_is_better
        text cadence "daily, quarterly or event"
    }
```

Both temporal tables carry **two overlapping guarantees, and they are not the
same guarantee**. A partial unique index on `where valid_to is null` gives "at
most one *current* row". A GiST exclusion constraint over `daterange(valid_from,
valid_to, '[)')` gives "no two validity periods overlap" — which a partial index
cannot express at all. Closing at `valid_to = as_of` and opening at `valid_from
= as_of` on the same day do not overlap, because the range is half-open.

**`security` and `peer_group` are not linked directly.** The join runs `security
→ security_sector → sector_node → peer_group`, and the group actually used on a
given day is recorded per ticker on `metric_daily.peer_group_id` — so a score
can be traced to the peers it was measured against, even after a
reclassification moves the ticker somewhere else.

### Ingest and the bitemporal fact layer

```mermaid
erDiagram
    data_source ||--o{ ingest_run : source_id
    ingest_run ||--o{ ingest_observation : ingest_run_id
    security ||--o{ ingest_observation : security_id
    ingest_observation ||--o{ fundamental_fact : ingest_observation_id
    ingest_observation ||--o{ price_daily : ingest_observation_id
    ingest_observation ||--o{ corporate_action : ingest_observation_id
    security ||--o{ fundamental_fact : security_id
    metric ||--o{ fundamental_fact : metric_id
    fundamental_fact |o--o{ fundamental_fact : "restates_id, append-only"

    ingest_observation {
        bigint id PK
        timestamptz fetched_at "when we looked"
        bytea content_hash "dedup"
        text blob_path
        boolean is_new_payload
    }
    fundamental_fact {
        bigint id PK
        date period_end "what it describes"
        text period_type "Q, A or TTM"
        timestamptz observed_at "when it became known"
        bigint restates_id FK "the row this supersedes"
    }
    price_daily {
        bigint security_id PK
        date trade_date PK
        timestamptz observed_at
    }
    corporate_action {
        bigint id PK
        date effective_date
        text action_type "split, dividend or spinoff"
        timestamptz observed_at
    }
```

There is **no column named `known_at`, `valid_time` or `system_time`** anywhere.
The two axes are named: what a fact describes is `period_end` / `trade_date` /
`effective_date`, and when it became known is `observed_at` — `fetched_at` on
`ingest_observation`. A restatement **inserts a new row pointing at the old
one** through `restates_id`; nothing is ever overwritten.

`ingest_observation` is deliberately not partitioned. A partitioned table's
unique constraint must include the partition key, which would force a composite
primary key and break the simple foreign keys the traceability chain depends on.

### Scoring runs, the derived daily layer and alerting

```mermaid
erDiagram
    scoring_logic_version ||--o{ scoring_run : logic_version_id
    weight_version ||--o{ scoring_run : weight_version_id
    weight_version ||--o{ pillar_weight : weight_version_id
    pillar ||--o{ pillar_weight : pillar_id
    scoring_run |o--o{ scoring_run : supersedes_run_id
    scoring_run ||--o{ metric_daily : scoring_run_id
    scoring_run ||--o{ pillar_score_daily : scoring_run_id
    scoring_run ||--o{ snapshot_daily : scoring_run_id
    scoring_run ||--o{ event_flag_daily : scoring_run_id
    scoring_run ||--o{ peer_group_stat : scoring_run_id
    scoring_run ||--o{ alert_event : scoring_run_id
    peer_group ||--o{ metric_daily : peer_group_id
    fundamental_fact |o--o{ metric_daily : "fundamental_fact_id, null for price metrics"
    alert_rule ||--o{ alert_event : alert_rule_id
    alert_rule ||--o{ alert_state : alert_rule_id
    security ||--o{ alert_event : security_id
    security ||--o{ alert_state : security_id

    scoring_run {
        bigint id PK
        daterange as_of_range "one live run per date, by exclusion constraint"
        interval cutoff_offset "visible facts are observed_at <= D + offset"
        boolean emits_alerts "false means skip alerting entirely"
        text status "live, backfill or experiment"
        text git_sha "reproducibility, never comparability"
        bytea config_hash "the scoring parameters, not the process config"
    }
    metric_daily {
        date as_of PK
        numeric percentile "0 to 100, within the peer group"
        int peer_count
        smallint fallback_level "2 industry, 1 sector, 0 market"
    }
    pillar_score_daily {
        date as_of PK
        numeric score
        smallint metric_count
        numeric coverage "0 to 1"
    }
    snapshot_daily {
        date as_of PK
        numeric blended_score "derivable, not ground truth"
        smallint pillar_agreement
        numeric min_coverage "worst pillar coverage that day"
        smallint worst_fallback_level
    }
    alert_rule {
        bigint id PK
        text condition_type
        smallint cooldown_days
        numeric min_coverage "the data-quality gate, per rule"
    }
    alert_event {
        bigint id PK
        date as_of "unique with rule and security — idempotency"
        numeric previous_blended_score
        text driver
        text delivery_status "pending, sent or failed"
    }
    alert_state {
        bigint alert_rule_id PK
        bigint security_id PK
        date cooldown_until
        smallint last_direction "-1 or 1"
    }
```

**No arrow points *into* a partitioned table, and none may.** Nothing in the
schema holds a foreign key to `price_daily`, `metric_daily`,
`pillar_score_daily`, `snapshot_daily` or `event_flag_daily` — that constraint
is what keeps a year's partition detachable.

`snapshot_daily` has **no `weight_version_id`** on purpose: the run carries it,
and the blended score is derivable from pillar scores plus that stamp.
`alert_state` tracks `last_direction` because a cooldown that suppresses the
*opposite* crossing is wrong — a score crossing up and then genuinely collapsing
back is exactly the event worth hearing about. And there is deliberately **no
`crossing` table**; crossings are derived from consecutive comparable
`snapshot_daily` rows.

### Sessions, the audit trail, and the migration ledger

```mermaid
erDiagram
    app_user ||--o{ session : "user_id, on delete cascade"

    app_user {
        bigint id PK
        bigint github_id UK "the numeric id, never the mutable login"
        text login "display and allow-list only"
    }
    session {
        bigint id PK
        bigint user_id FK
        bytea token_hash UK "HMAC of the cookie, never the token"
        timestamptz expires_at
    }
    event {
        bigint id PK
        timestamptz occurred_at
        text kind "agent, command, tool or system"
        text operation
        text actor "a Discord id, a GitHub login, or system"
        text actor_kind "discord, github or system"
        numeric cost_usd "what OpenRouter actually billed"
        jsonb detail "deliberately unstructured"
    }
    schema_migration {
        text version PK "the filename stem, not a content hash"
        timestamptz applied_at
    }
```

`auth.app_user → auth.session` carries the **only `on delete cascade` in the
whole schema**. `audit.event` has **no foreign keys at all**: `actor` is free
text because Discord ids and GitHub logins do not share a type, and joining them
to one table would invent a relationship that does not exist.

`public.schema_migration` is created by `src/screener/migrate.py`, not by any
migration file. It keys on the filename stem rather than a content hash, so
**renaming a shipped migration re-runs it**.

### Live stream capture

```mermaid
erDiagram
    platform ||--o{ stream_session : "platform"
    stream_session ||--o{ transcript_segment : "session_id, on delete cascade"

    platform {
        text code PK "youtube, twitch"
        text display_name
    }
    stream_session {
        bigint id PK
        text platform FK
        text external_id "video id, or channel where the URL names none"
        text state "requested, starting, running, stopping, stopped, failed"
        text embed_url "built server-side; null until a probe names the video"
        smallint chunk_seconds
        integer chunks_ok "counted, so a failing stream and a quiet one differ"
    }
    transcript_segment {
        bigint session_id FK
        integer seq PK "what the dashboard polls after"
        timestamptz captured_at "wall clock, re-anchored on reconnect"
        numeric offset_seconds "from the start of the session"
        text text
    }
```

Three things here go against the grain of the rest of the schema, each on
purpose.

**`platform` is a table rather than `text` + `check`.** The schema conventions
say enumerations are check constraints, and `state` follows that — it is a
closed set this code owns. The platform list is the opposite: the whole point is
that it grows, and a growing list behind a check constraint is a migration every
time somebody writes an adapter.

**`transcript_segment` is not partitioned**, unlike every other table that grows
by the day. Retention here is "delete the session", which cascades, rather than
a date-range drop; and the only read that matters is one session in sequence
order, which partitioning by time would scatter across partitions instead of
keeping adjacent.

**`stream_session` is a queue as well as a record.** A partial unique index on
`(platform, external_id)` over the four live states is what stops one stream
being captured twice, and a partial index on `state` is what makes the
supervisor's two-second poll cheap. Nothing else in this schema is read by a
process asking what to do next.

### Partitions

Five parents, all range-partitioned, all yearly, and no default partition
anywhere — attaching a new partition alongside a default requires a full scan.

| Parent | Key | First year |
|---|---|---|
| `price_daily` | `trade_date` | 2020 |
| `metric_daily` | `as_of` | 2026 |
| `pillar_score_daily` | `as_of` | 2026 |
| `snapshot_daily` | `as_of` | 2026 |
| `event_flag_daily` | `as_of` | 2026 |

The asymmetry is deliberate: momentum needs twelve months of trailing prices, so
a 2026 ingest writes 2025 rows, while a derived row cannot predate the run that
produced it. `ensure_partitions` decides existence by **partition-bound coverage
rather than by name**, reading `pg_get_expr(relpartbound)` and testing for
overlap — which is what will let monthly partitions be introduced at a future
year boundary without colliding with a same-named yearly one.

---

## The v1 pipeline

The spine, and the only diagram here that draws things which do not exist.
Universe, identity and daily **price** ingest are built; fundamentals ingest is
cycle two, and scoring, the snapshot diff and alerting are all unwritten.

```mermaid
flowchart LR
    classDef unbuilt stroke-dasharray: 5 5

    csv["data/universe.csv"]
    ident["universe load<br/>security, security_symbol,<br/>security_sector, peer_group"]
    ing["ingest prices<br/>Yahoo /v8/finance/chart, raw bars only<br/>content-hash dedup, payload to R2"]
    ingf["«not built» ingest fundamentals<br/>the crumbed quoteSummary path<br/>cycle two"]
    facts[("ingest_observation<br/>price_daily<br/>«not built» fundamental_fact")]
    sc["«not built» scoring<br/>percentile within sector peer group,<br/>then average within pillar"]
    derived[("metric_daily<br/>pillar_score_daily<br/>snapshot_daily<br/>event_flag_daily")]
    diff["«not built» diff<br/>today vs the last comparable snapshot"]
    gate{"«not built»<br/>scoring_run.emits_alerts"}
    skip["skip alerting entirely"]
    cool{"«not built»<br/>cooldown and dedup"}
    post["«not built» one POST<br/>to a Discord webhook"]

    csv --> ident
    ident --> ing
    ident --> ingf
    ing --> facts
    ingf --> facts
    facts --> sc
    sc --> derived
    derived --> diff
    diff --> gate
    gate -->|false| skip
    gate -->|true| cool
    cool --> post

    class ingf,sc,derived,diff,gate,skip,cool,post unbuilt
```

The `emits_alerts` gate is drawn because it is the invariant most likely to be
forgotten. Postgres cannot express it as a cross-table check, so **the alerting
code owns it**: a run with `emits_alerts = false` must be skipped before the
alerting step, not attempted and left to collide. The unique constraint on
`(alert_rule_id, security_id, as_of)` would catch a backfill, but it is a
backstop, not the mechanism — without the skip, a backfill run eventually fires
history into the channel.

Alerts fire on the **crossing**, not the state, and carry their reasoning: the
score delta, the pillar scores, the driver and any event-risk flags. They say
"score crossed threshold", never "STRONG BUY".

---

## From push to production

```mermaid
flowchart TD
    push["push or pull_request"]
    tc["typecheck — pyright, zero errors"]
    test["test — 3.11 and 3.12<br/>against a postgres:16 service container"]
    bldci["build — both images, push: false"]
    ok["ci-ok — if: always<br/>the single stable branch-protection check"]
    gate{"push to main?"}
    stop["stop — the PR is green, nothing ships"]

    bld["build and push<br/>api and web to GHCR<br/>tagged :sha and :latest"]
    dispatch["workflow_dispatch with image_tag<br/>rollback: build skipped entirely"]
    ts["join the tailnet<br/>ephemeral tag:ci node"]
    scp["scp the compose files only<br/>the box never holds a checkout"]
    up["ssh: docker compose pull && up -d"]
    reload["caddy reload<br/>a bind-mount change does not recreate the container"]
    smoke["smoke test from inside the container<br/>exec -T api against /ready, up to 30 tries"]
    botcheck["assert the bot service is running<br/>it has no port and no healthcheck"]
    boom["dump logs for api, app and web, then exit 1"]
    done["deployed"]

    push --> tc
    push --> test
    push --> bldci
    tc --> ok
    test --> ok
    bldci --> ok
    ok --> gate
    gate -->|no| stop
    gate -->|yes| bld
    dispatch -.->|"rollback path"| ts
    bld --> ts
    ts --> scp
    scp --> up
    up --> reload
    reload --> smoke
    smoke -->|ok| botcheck
    smoke -->|"never went ready"| boom
    botcheck --> done
```

Three of these steps exist because something went wrong once.

**The explicit `caddy reload`** is there because changing a bind-mounted file
does not recreate the container. Without it a Caddyfile change ships but never
takes effect — that is how `/api/*` kept going to Next.js for a whole release.

**The smoke test runs inside the container**, never through the tunnel, because
Cloudflare Access answers an unauthenticated request with a 302 and `curl -f`
treats a redirect as success. A smoke test that passes on a redirect is worse
than no smoke test.

**The bot is checked separately**, by asserting the service is still running
fifteen seconds later, because it has no port to probe and no healthcheck to
read.

Rollback is the same workflow dispatched by hand with `image_tag` set to an
older SHA; the build job is skipped and the box repoints. **Do not** roll back
across a migration boundary — migrations are applied forward at startup and
nothing undoes them.

---

## Starting up

`python -m screener.boot` takes `serve`, `migrate` or `selftest`, and does the
same first two things in every case.

```mermaid
sequenceDiagram
    autonumber
    participant C as container CMD
    participant B as boot.startup.main
    participant I as Infisical
    participant P as Postgres
    participant S as health.serve

    C->>B: python -m screener.boot, default serve
    B->>I: load_into_environ
    I-->>B: secrets into os.environ, existing values win
    Note over B,I: A SecretsError is fatal. Starting without<br/>credentials would fail later and less clearly.

    alt command is selftest
        B->>B: run the seven checks and exit
        Note over B: Deliberately before the database step, so a<br/>broken schema does not stop the report on it.
    end

    B->>P: select pg_advisory_lock 8119002
    B->>P: apply_migrations, each file in its own transaction
    P-->>B: newly applied versions
    B->>P: ensure_partitions through this year + 1
    P-->>B: partitions created
    B->>P: select pg_advisory_unlock 8119002

    alt command is migrate
        B->>C: exit 0
    else command is serve
        B->>S: serve
        Note over B,S: Called, not exec'd, so serve's own<br/>SIGTERM handler is the one that runs.
    end
```

The advisory lock exists because `apply_migrations` reads the applied set and
then runs DDL with nothing in between. Two containers starting at once — a
rolling deploy, or a restart racing a manual `compose up` — would both see
version N unapplied, both run its `create table`, and the loser would die on
`DuplicateTable`, which reads as a broken migration rather than as a race. The
lock is session-scoped, so a container killed mid-migration releases it when its
socket closes.

`selftest` is the only thing that exercises the infrastructure end to end today,
which is why it is worth running after a deploy. It checks the database, the
build SHA, a direct fetch, a proxied fetch, OpenRouter, the Discord webhook's
configuration and the bot's token. Two of those are pointed:

- **The `isp_proxy` check fails when the proxied exit IP matches the direct
  one.** A proxy that is configured, billed and doing nothing should not report
  OK.
- **The Discord check never sends.** It confirms the webhook is configured and
  stops there — a self-test must not make an outward-facing post.

Unconfigured is `SKIP`, not `FAIL`, and every check is wrapped so one crash
cannot stop the rest.

---

## Steven

The Discord bot is a command surface, not a delivery channel, and it runs as its
own process for the same reason it has its own container: a gateway connection
is a long-lived thing that should not share a lifecycle with a web server.

```mermaid
sequenceDiagram
    autonumber
    participant D as Discord
    participant CL as bot.client
    participant A as bot.agent
    participant BG as bot.budget
    participant DB as audit.event
    participant M as OpenRouter
    participant T as bot.tools
    participant W as web /api/render

    D->>CL: mention in a guild, or anything in a DM
    CL->>CL: wants_reply — say it in a channel,<br/>no need to in a DM
    opt DM
        CL->>DB: last_handoff_context, 30-minute window
    end
    CL->>A: asyncio.to_thread respond
    Note over CL,A: Every layer below is synchronous. Blocking the<br/>loop stalls the heartbeat, not just one command.
    A->>BG: check, before the model is called
    alt over the daily cap
        BG-->>A: refused
        A-->>D: names the figures, records outcome refused
    else allowed, or Postgres unreachable
        Note over BG: Fails open. Refusing everyone because<br/>Postgres blinked is the worse failure.
        opt not a fresh conversation
            A->>DB: recent_turns, for this person's folded identities
            DB-->>A: the last two exchanges, oldest first
        end
        A->>M: converse, up to six tool rounds
        M-->>A: tool call: chart
        A->>T: dispatch, inside collecting
        T-->>A: one sentence back to the model
        Note over T,A: The 60-point series never enters the message<br/>list. The chart rides beside the reply.
        A->>M: tool result
        M-->>A: final text
        A->>W: POST the SVG spec, up to three charts
        W-->>A: PNG
        A-->>D: reply plus attachments
    end
    A->>DB: record — tokens, cost, outcome, and<br/>the turn itself, truncated
```

`collecting()` is a `ContextVar` rather than a module global because each
request runs on its own worker thread and `asyncio.to_thread` copies the
context, so two people asking at once cannot be handed each other's charts. The
model names *what* to mark — `peak`, `low`, `surge`, `drop`, `crossing`,
`latest` — and the index is computed from the data. A model supplying
coordinates would be inventing where the marker goes, which is the same failure
as inventing a number and worse for being drawn precisely.

Steven remembers the last two exchanges, and reads them back from the audit
trail rather than holding them in a variable, because the bot and the status
service are separate processes and a variable in one is invisible to the other.
Identities are folded exactly as the spend cap folds them, so a thread started
on the dashboard is the one that carries on in a DM rather than a second
stranger. The memory is bounded on every axis at once — two exchanges, 300
characters each, final text only, half an hour, and nothing at all when the
dashboard sends `fresh=1` for a new chat — because a remembered turn is re-sent
on every round of every message after it, so the cost of remembering is
multiplied rather than paid once.

The handoff from the dashboard crosses the same gap and uses the same channel:
two processes that share no memory, and a trail that both can read.

```mermaid
sequenceDiagram
    autonumber
    participant U as browser
    participant API as api — health.server
    participant DB as audit.event
    participant DR as Discord REST v10
    participant BOT as bot process

    U->>API: GET /api/handoff, session required
    API->>DR: open a DM channel, then post to it
    Note over API,DR: REST, not the gateway. A second gateway session<br/>on one token would have both processes<br/>answering every command.
    DR-->>U: "Carrying on from the dashboard."
    API->>DB: record steven.handoff with the context
    U->>BOT: DMs the bot
    BOT->>DB: last_handoff_context for this Discord id
    DB-->>BOT: what they were looking at, if within 30 minutes
    BOT->>BOT: inject as a second system turn
    Note over BOT: Separate from the user turn, so the model can<br/>tell what was typed from what is on screen.
```

The daily spend cap is per person and checked before the model call. It folds
Discord onto GitHub through `DISCORD_USER_MAP` **in both directions**, so it
cannot be doubled by asking on the other surface, and `0` means nobody may spend
anything — the permissive reading of zero is explicitly rejected.

---

## The universe

Two commands that share nothing but a file. `refresh` is network-only and never
opens a database connection; `load` is database-only and never opens a socket.

```mermaid
flowchart LR
    wiki["Wikipedia<br/>S&P 500, 400, 600"]
    sec["SEC company_tickers.json<br/>CIK per symbol"]
    yahoo["Yahoo profile per symbol<br/>bypasses screener.fetch"]
    refresh["universe refresh<br/>no database connection"]
    csv["data/universe.csv"]
    unres["data/universe-unresolved.csv"]
    review["committed, reviewed in a diff"]
    load["universe load<br/>no network"]
    plan["plan — CIK, then current symbol,<br/>then treat as new"]
    ceiling{"more than 10% of the<br/>active universe departing?"}
    refuse["refuse, unless --force"]
    apply["apply — one transaction<br/>close then open validity rows"]
    tables[("security, security_symbol,<br/>security_sector, sector_node,<br/>peer_group")]

    wiki --> refresh
    sec --> refresh
    yahoo --> refresh
    refresh --> csv
    refresh --> unres
    csv --> review
    review --> load
    load --> plan
    plan --> ceiling
    ceiling -->|yes| refuse
    ceiling -->|no| apply
    apply --> tables
```

**The committed CSV is the review surface.** A sector reclassification moves a
ticker between peer groups, and a peer group change moves a score — so it has to
show up in a diff before it can move anything. That only works because the
file's ordering is deterministic and its field order never drifts.

**Identity is matched on CIK first, then on the current symbol, then treated as
new.** A symbol is a mutable attribute: matching on it turns a rename into a
departure plus an unrelated arrival, orphaning the company's history. Two rows
resolving to one security raises rather than merging.

The departure ceiling check sits **inside** the transaction, not before it. On
an autocommit connection, the taxonomy written outside it would survive a
refusal — so the guard would leave behind exactly the half-applied state it
exists to prevent.
