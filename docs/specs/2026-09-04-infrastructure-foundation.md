# Infrastructure foundation

Status: implemented. Written 2026-09-04.

The layer between the database and everything that has not been built yet: secrets, HTTP
fetching, an LLM client, alert delivery, a status endpoint, and a way to get all of it onto a
server. Standing decisions are summarised in `DESIGN.md`; this document holds the module set and
the reasoning specific to it.

Roots only. No source adapters, no daily job, no scoring, no alert content, no web UI. The point
is that ingest, scoring and alerting each inherit a working piece rather than inventing their
own, and that a credential or a proxy never has to be threaded through them by hand.

Most of this is ported from a sibling project (`scrape-terminal`, "Job Terminal") that has run
the same shapes on a VPS for a month. Where this deviates from that source, the deviation is
stated and justified.

---

## 1. Constraints this has to satisfy

From `DESIGN.md` and the schema spec, restated as requirements on this layer:

1. **APIs before scrapers.** The default request path is direct, with no proxy in it. A proxy is
   something a specific source opts into, never a default.
2. **Every score traces to visible raw inputs.** Which path served a request is part of that
   trace, so the fetch layer records which strategy won and which were tried first.
3. **Tunables live in the database.** Pillar weights, alert thresholds, cooldowns and coverage
   floors are rows. Configuration must not duplicate them.
4. **`scoring_run.git_sha` and `config_hash` are `not null`** and had no producer. Something has
   to supply both, from inside a container that has no git history.
5. **Cost ceiling ~£5–10/month**, of which the VPS is £4–6. Everything added here is £0 marginal.
6. **Boring, cheap and honest over impressive.** One new runtime dependency is the budget.
7. **Alerts say "score crossed threshold", never a recommendation.** The delivery layer carries
   no opinion about what it is delivering.

---

## 2. Decisions

**D1. Flat single-purpose packages, not one `infra` tree.** Each of `secrets`, `fetch`, `ai`,
`notify`, `health`, `provenance` and `boot` is a package beside the existing `config`, `migrate`
and `partitions`, exposing its surface through `__init__.py`. *Rejected:* a shared `infra`
namespace, which buys a level of nesting and no isolation, and a single module per concern, which
does not leave room for a credential object beside the code that uses it.

**D2. Credentials belong to the subsystem that uses them, not to `Settings`.** `screener.config`
holds `database_url` and nothing else; `fetch.config.ProxyConfig`, `ai.config.RouterConfig` and
`notify.config.DiscordConfig` each read their own environment. *Rejected:* one settings object
holding everything — it forces one policy on every field, so a required `DATABASE_URL` would make
a process that only wanted to post a Discord alert fail for want of a database it never touches.
This was found by writing the self-test: every check failed with "DATABASE_URL is not set",
including the three that had nothing to do with the database.

**D3. `settings()` stays uncached.** *Rejected:* `functools.lru_cache`. `secrets.load_into_environ()`
mutates `os.environ` during boot, so a cached object would freeze whatever the first caller saw —
and a stray import-time `settings()` anywhere in the tree would silently pin pre-Infisical values.
The symptom would be a wrong database, not an error.

**D4. Secrets over stdlib `urllib`, not the Infisical SDK or CLI.** Two HTTP calls do not justify
a dependency that has to exist in the image, or an apt repository in the Dockerfile. *Rejected:*
the official SDK; the CLI.

**D5. Missing secrets are a no-op; failed secrets are fatal.** No machine identity configured
means the environment is used as-is, which is how local development and CI run unstubbed. A
configured identity that fails to answer stops the process. *Rejected:* falling back on failure —
starting with half a configuration means failing later, somewhere less obvious, possibly against
the wrong database.

**D6. The fallback chain is the retry.** A strategy makes one attempt; failure moves to the next
strategy. *Rejected:* per-strategy retry with backoff. Re-issuing a request down the path that
just failed rarely helps, and the next scheduled run picks up anything transient. Rate limiting
is also out: Finnhub's 60/min is a property of a source, so it belongs with ingest.

**D7. A 2xx with an empty body is a failure unless the caller says otherwise.** More than one
provider answers a rate-limited request that way, and it is indistinguishable from "nothing to
report". *Rejected:* trusting the status code. The fact layer is append-only, so a bogus empty
observation is not something a later run corrects.

**D8. `config_hash` takes the caller's mapping and knows nothing about `Settings`.** Per the
schema spec this hash covers the *scoring* parameters a run's output depends on — `cutoff_offset`,
the minimum peer count. *Rejected:* deriving it from process configuration, which would make it
change when the OpenRouter model or the health port changed. That is the same unrelated-churn
failure that got `git_sha` rejected as a comparability key in the schema spec's D8, and it would
also mean a credential could be hashed into an append-only table by accident.

**D9. Two names for the build SHA.** `git_sha()` returns `"unknown"` and never raises, for
`/status`. `require_git_sha()` raises, for anything writing `scoring_run.git_sha`. *Rejected:* one
function with a `strict=` flag — a boolean that switches between raising and not raising reads as
an accident at the call site. A row stamped `"unknown"` is a permanent, unrepairable lie about a
run in an append-only table.

**D10. The status service is stdlib `http.server`.** *Rejected:* FastAPI. Three read-only JSON
GETs with no request bodies, no validation and no auth — Cloudflare Access owns that — do not
justify starlette, pydantic and an async runtime in a repo defined by having almost no
dependencies. Replacing this one module when the deferred UI arrives changes nothing around it.

**D11. Migrations run in the container entrypoint, under a Postgres advisory lock.**
`apply_migrations` reads the applied set and then runs DDL with nothing in between; two containers
starting at once would both see version N unapplied and both run its `CREATE TABLE`, and the loser
dies on `DuplicateTable` — which reads like a broken migration rather than a race, and sends
whoever is on the deploy looking at the wrong file. *Rejected:* migrating as a separate CD step,
which would leave a manual `docker compose up` on the box doing something different from a
pipeline run.

**D12. Sync, not async.** The sibling is async throughout. This repo is entirely synchronous
psycopg, and porting the async colouring would introduce an event loop for no benefit.

**D13. Discord is a webhook behind a channel protocol; no bot.** `DESIGN.md` says "a single HTTP
POST to a webhook. No OAuth, no bot hosting." A gateway bot means an always-connected process and
a token with real scopes, for a payload that is one message a day. *Deferred, not rejected:* a bot
arrives later as another `NotificationChannel` and nothing above it changes.

**D14. Spend limits are set at the provider.** A credit cap on the OpenRouter key, a spend cap on
the Bright Data zone. *Rejected:* a budget/accounting module — it would need its own table, and it
can be defeated by a bug in itself, whereas a provider-side cap cannot.

**D15. Deploy is called from CI, not triggered by `push`.** A push-triggered deploy races the CI
run on the same commit and can win; and branch protection checks the pull request head, which is
not the merge commit that lands on `main`. Running `deploy.yml` as a reusable workflow from
`ci.yml` after `ci-ok` guarantees the thing deployed is the thing that went green.

---

## 3. Conventions

- Every package exposes its surface through `__init__.py`. Nothing outside imports a submodule.
- Credentials are `repr=False` dataclass fields. `test_config` builds each config object with a
  sentinel value and asserts it does not appear in the repr — tracebacks reach log aggregators.
- Absence and emptiness are the same thing. `${VAR:-}` from an unset compose interpolation exports
  an empty string, and treating that as a configured empty value turns a missing secret into a
  confusing failure much further downstream.
- HTTP-touching modules take an optional `transport`, so tests pass `httpx.MockTransport` and
  exercise the real client code with no network, no credential, and no mocking library.
- Anything that can leak a URL redacts its query string first, including exception text — httpx
  puts the full URL into `raise_for_status()`'s message.

---

## 4. Modules

| Module | Public surface | Notes |
|---|---|---|
| `screener.config` | `Settings`, `settings()`, `env` | `database_url` only |
| `screener.secrets` | `load_into_environ()`, `fetch()`, `SecretsError` | stdlib urllib |
| `screener.fetch` | `fetch()`, `FetchResult`, `ProxyConfig`, errors | default `("direct",)` |
| `screener.ai` | `complete()`, `Completion`, `MODELS`, `resolve_model()` | cost read from the response |
| `screener.notify` | `Alert`, `NotificationChannel`, `DiscordWebhook` | protocol first |
| `screener.health` | `serve()`, `build_server()` | `/health`, `/ready`, `/status` |
| `screener.provenance` | `git_sha()`, `require_git_sha()`, `config_hash()` | fills two not-null columns |
| `screener.boot` | `main()`, `prepare_database()` | the container's CMD |

Endpoint split: `/health` touches nothing and is what the container healthcheck hits — a readiness
probe there would restart a healthy container every time the database blipped. `/ready` opens a
short-lived connection per probe, never a cached one, because the question is whether a connection
can be made *now*. `/status` runs no queries at all, so the one endpoint that can say which build
is running does not go dark exactly when something is wrong.

---

## 5. Deployment

Second compose stack on the VPS that already runs the sibling project, isolated by project name.
Two services, no published host ports: the tunnel dials outward, so nothing new listens on the
host and there is no port to collide with. Postgres runs in the stack with a named volume and is
not published to the host either, so the database has no public attack surface and needs no
firewall rule of its own.

Full runbook in `deploy/README.md`, including the first-time setup that cannot be done from CI —
the Tailscale ACL, the Infisical project and the Cloudflare hostname.

Two traps recorded because both fail silently:

- **The smoke test must not go through the tunnel.** Cloudflare Access answers an unauthenticated
  request with a 302 to its login page, and `curl -f` does not treat a redirect as a failure — a
  public probe would go green against a completely dead application. It probes from inside the
  container.
- **The Tailscale credential expires.** An OAuth client would not, but Tailscale exposes no API
  for creating one, so the provisioned credential is a tagged, reusable, ephemeral auth key. An
  expired key fails as a connection timeout that reads like a network fault — the sibling project
  lost time to exactly this — so the workflow checks the expiry date first and fails with a dated,
  actionable message. Swapping in an OAuth client later removes the check along with the problem.

---

## 6. Open items for implementation

Named so they are recorded rather than forgotten:

- **Rate limiting** belongs with ingest, where a source's published limit is known.
- **Source adapters** — nothing implements `fetch()` against a real source yet.
- **The daily job.** `boot` pre-creates partitions, but nothing ingests, scores or diffs.
- **Persisted LLM spend.** `Completion` carries the cost; nothing writes it down. Needs a table.
- **Alert content** — crossing detection, the pillar breakdown, dedup and the cooldown.
- **Where raw payloads go.** `DESIGN.md` now records this as open — Azure Blob was the answer
  while the database was Azure. A volume on the VPS with a pruning job, or an object store billed
  per GB. The ingest spec settles it, since ingest writes the first one.
- **The dated schema documents still describe Azure.** `docs/specs/2026-09-04-database-schema-design.md`
  and its plan were accurate when written and are point-in-time records, so they are left alone;
  `DESIGN.md` is the current decision and it is the one that was updated.
- **Backups.** No `pg_dump` schedule exists, and now that the database is a volume on the VPS
  rather than a managed service, nothing is taking one on our behalf. This is the most important
  item on this list.
- **Ingress rules are not version controlled**, being dashboard-managed. Revisit when the deferred
  UI adds routes.
- **Rollback across a migration boundary is unsupported.** There are no down migrations, and an
  older image starts happily against a newer schema.
