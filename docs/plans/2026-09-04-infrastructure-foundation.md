# Infrastructure Foundation Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Every step must leave
> `pytest` and `pyright` green before the next one starts.

**Goal:** Give the screener the layer between its database and everything unbuilt — secrets, HTTP
fetching, an LLM client, alert delivery, a status service, provenance stamps, and a deployment
that puts all of it on a VPS. Roots only: no source adapters, no daily job, no alert content.

**Architecture:** Flat single-purpose packages beside the existing `config`, `migrate` and
`partitions`, each exposing its surface through `__init__.py`. Credentials belong to the subsystem
that uses them rather than to a shared settings object, so a process that only posts an alert does
not require a database URL. One new runtime dependency (`httpx`); secrets and the status service
are stdlib, which is what keeps the dependency budget at two. Deployment is a container image
built by CI, published to GHCR and pulled by the VPS, with migrations running in the entrypoint
under a Postgres advisory lock.

**Tech Stack:** Python 3.11+, httpx, psycopg 3, pytest, Docker, GitHub Actions.

**Spec:** `docs/specs/2026-09-04-infrastructure-foundation.md`

## Global Constraints

- Python 3.11 floor. Pyright `standard`, zero errors, and it is a CI gate.
- Two runtime dependencies total. Anything else must earn its place in the spec first.
- No test may require a network, a credential, or a live tunnel. HTTP is exercised through
  `httpx.MockTransport`; there is no mocking library.
- Database-touching tests use the existing `fresh_db` fixture and inherit `conftest.py`'s
  skip-locally-fail-in-CI rule.
- The repo-root `compose.yaml` is test-only and is not to be modified.
- `ci-ok` is the branch-protection check. A new CI job that is not in its `needs:` gates nothing.

---

## Task 1 — Dependency and configuration seam

- [x] Add `httpx>=0.27` to `[project] dependencies` **before** any module imports it, or pyright
      reports `reportMissingImports` and the tree is red mid-task.
- [x] `screener/config/`: `env.py` with `optional`, `required`, `text`, `integer`;
      `settings.py` with a one-field `Settings` and an uncached `settings()`.
- [x] Keep the failure message byte-identical to `"<NAME> is not set"` — it is the string in
      `README.md` and `CLAUDE.md` and the one people search for.
- [x] `tests/test_config.py`.

## Task 2 — Provenance

- [x] `screener/provenance/`: `git.py` (`git_sha`, `require_git_sha`) and `hashing.py`
      (`config_hash` over a caller-supplied mapping, returning 32 raw bytes).
- [x] `config_hash` refuses anything json cannot render deterministically, `Decimal` included.
- [x] `tests/test_provenance.py`, including a `fresh_db` test that round-trips a digest through
      the real `bytea` column rather than asserting that it would work.

## Task 3 — Secrets

- [x] `screener/secrets/infisical.py`: Universal Auth login, raw secrets read, written into
      `os.environ` without overwriting anything already set.
- [x] No credentials configured returns 0; a failed fetch raises `SecretsError`.
- [x] Key names are logged, values never.
- [x] `tests/test_secrets.py`, stubbing `urllib.request.urlopen`.

## Task 4 — Fetch

- [x] `screener/fetch/`: `config.py` (`ProxyConfig`), `result.py`, `strategies.py`, `chain.py`.
- [x] `DEFAULT_STRATEGIES = ("direct",)`. Proxy strategies raise `StrategyUnavailable` at call
      time when unconfigured, so they can sit in the registry without being reachable.
- [x] Empty 2xx rejected unless `allow_empty=True`; `validate` callback on top.
- [x] `FetchResult.attempts` records the chain, not just the winner.
- [x] Redact query strings from logs **and from exception text** — httpx puts the full URL into
      `raise_for_status()`'s message, which a test caught.
- [x] `tests/test_fetch.py`, including that Bright Data is unreachable under the default list
      *while credentials are present*.

## Task 5 — AI and notifications

- [x] `screener/ai/`: `models.py` (allow-list, so a typo cannot bill oddly), `config.py`,
      `openrouter.py` with `"usage": {"include": true}` so the cost comes from the response.
- [x] `screener/notify/`: `base.py` (`Alert`, `AlertField`, `NotificationChannel`,
      `ChannelError`), `config.py`, `discord.py` with one `Retry-After` retry.
- [x] `tests/test_ai.py`, `tests/test_notify.py`.

## Task 6 — Status service and boot

- [x] `screener/health/`: `checks.py` and `server.py`. HTTP/1.1 with an accurate `Content-Length`,
      `ThreadingHTTPServer`, silenced access log, SIGTERM handled from a separate thread because
      `shutdown()` blocks until `serve_forever()` returns.
- [x] `screener/boot/`: `startup.py` (secrets → advisory lock → migrate → `ensure_partitions` →
      serve) and `selftest.py`. `__main__.py` so `python -m screener.boot` works.
- [x] `tests/test_health.py`, binding port 0 in a fixture and hitting it with `urllib.request`.

## Task 7 — Deployment

- [x] `deploy/Dockerfile` (single stage, non-root, `ARG GIT_SHA` → `ENV SCREENER_GIT_SHA`),
      `.dockerignore`.
- [x] `deploy/compose.prod.yaml` and `deploy/compose.tunnel.yaml`. No published ports, healthcheck
      on `/health`, image pinned by `SCREENER_IMAGE_TAG`.
- [x] `.github/workflows/deploy.yml` as a reusable workflow; `build` job added to `ci.yml` and to
      `ci-ok`'s `needs`; `deploy` job calling it after `ci-ok` on `main`.
- [x] `deploy/README.md` — the first-time setup that cannot be done from CI.

## Task 8 — Documentation

- [x] Replace the Bright Data entry in `DESIGN.md` (replace, not append — its own header says so)
      and extend `## Infrastructure`.
- [x] Fix `CLAUDE.md`'s Status block, which has been wrong in every clause since PR #1.
- [x] `PLAN.md` Done entry; `README.md` status row, commands and deployment pointer.
- [x] `.env.example` gains every new variable with a note that production reads Infisical.

---

## Verification

Each claim in the spec, and the test that demonstrates it rather than asserting it.

| Claim | Demonstrated by |
|---|---|
| Bright Data is off by default | `test_bright_data_is_never_reached_under_the_default_strategy_list` — credentials are fully configured and the proxy strategies are replaced with functions that raise if called |
| The chain escalates and records what it tried | `test_the_chain_falls_through_a_failure_and_records_what_it_tried` |
| An empty 2xx does not become a real observation | `test_an_empty_two_hundred_is_rejected_and_escalates` |
| A caller can demote a successful response | `test_a_validate_callback_can_demote_a_successful_response` |
| Credentials do not leak through error text | `test_a_query_string_is_not_echoed_into_the_error` |
| Credentials do not leak through a repr | `test_no_config_object_prints_its_credentials` |
| Secret values are never logged | `test_secret_values_are_never_logged` |
| A debugging override survives Infisical | `test_an_existing_environment_variable_is_not_overwritten` |
| Half a configuration does not start | `test_a_failed_fetch_is_fatal_rather_than_falling_back` |
| `config_hash` is stable and order-independent | `test_key_order_does_not_change_the_hash` |
| `config_hash` fits the `bytea` column | `test_a_config_hash_round_trips_through_the_bytea_column` |
| An unstamped build cannot reach `scoring_run` | `test_an_unknown_build_is_reported_by_git_sha_but_refused_by_require` |
| `/health` never touches the database | `test_health_answers_without_a_database` |
| `/status` survives a database outage | `test_status_answers_when_the_database_is_unreachable` |
| A model typo cannot bill oddly | `test_an_unknown_model_id_falls_back_to_the_cheap_default` |
| An alert is never dropped silently | `test_a_rejected_payload_raises_rather_than_dropping_the_alert` |

Verified by hand, because no test can:

- `docker build -f deploy/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) .` succeeds.
- The image against a real Postgres logs `migrations: already current`, creates the next year's
  partitions, and serves. `/status` reports the baked SHA; `/ready` reports 9 migrations.
- `docker stop` completes in about a second, not ten — SIGTERM is handled rather than ignored.

## Out of scope

Source adapters, rate limiting, the daily job, persisted LLM spend, alert content, raw payload
storage, backups, and metrics. Each is listed in the spec's open items — backups especially, now
that the database is a volume on the VPS rather than a managed service.
