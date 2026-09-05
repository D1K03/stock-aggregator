# The toolbox

What exists, when to reach for it, and when not to.

`DESIGN.md` says why each of these was chosen and `deploy/README.md` says how to
operate them. This is the middle document: you are about to write some code and
want to know which of these it should use.

The recurring theme is that most of these cost nothing until used and quite a
lot once used carelessly, so nearly every entry has a "reach for it when" and a
"do not" that matters more.

---

## Fetching over HTTP

`screener.fetch` — one function, an ordered list of strategies, first success
wins.

```python
from screener.fetch import fetch

result = fetch(url)                              # direct only
result = fetch(url, ("direct", "isp_proxy"))     # fall back if direct fails
```

| Strategy | Cost | Reach for it when |
|---|---|---|
| `direct` | free | Always. This is the default and should stay the default. |
| `isp_proxy` | bandwidth off a flat monthly plan | A source blocks the VPS, or rate-limits by IP and you need a different one. |
| `unlocker` | **per successful request** | A source defeats the proxy too. Last resort, and never first in a chain. |

**The default is `("direct",)` and a test enforces it.** Bright Data is
reachable only when a caller names it, so adding a proxy to a request is a
visible decision in a diff rather than something a default did quietly.

Two behaviours worth knowing before you write a source:

- **A 2xx with an empty body is treated as a failure** and escalates to the next
  strategy. More than one provider answers a rate-limited request that way, and
  it is indistinguishable from "nothing to report". Pass `allow_empty=True` if
  a source genuinely returns nothing sometimes.
- **There is no retry inside a strategy.** The chain is the retry. Re-issuing
  down the path that just failed rarely helps, and the next scheduled run picks
  up anything transient.

`FetchResult.strategy` records which path served the request and
`.attempts` records what was tried first — worth logging, because a run that
quietly fell back to a proxy is a fact about the source.

### Bright Data specifically

An ISP proxy zone with **four UK exit IPs**, on a flat monthly plan that a
sibling project already pays for. That is the whole reason it is available here:
the marginal cost of *having* it is zero.

Each request draws a fresh session, which is how a different exit IP is
selected. With four addresses that rotation is real; the self-test asserts the
proxied exit IP differs from the box's own, and fails if it does not.

**Do not** reach for it because a source is slow, or intermittently 500s, or
because you are not sure. Reach for it when you have seen a block. The Web
Unlocker is a separate product billed per successful request and should be
treated as spending money every time it runs.

---

## Ingress: the Cloudflare Tunnel

`cloudflared` runs in the stack and **dials outward**. Nothing listens on the
VPS's public interface, there is no inbound firewall rule, and there is no
certificate to renew.

- Hostname: `screener.edenmatrix.xyz`
- Tunnel id: `d627c412-a8ef-48ba-953f-b878835c3c82`
- Routing lives in the Cloudflare Zero Trust dashboard, **not in this repo**

Reach for it when something needs to be reachable from outside. Adding a second
hostname is a dashboard action plus a `handle` block in the Caddyfile.

**Do not** publish a container port on the host to expose something. That is
the thing the tunnel exists to avoid, and the box is shared with four other
stacks.

The tradeoff worth remembering: because the hostname mapping is in the
dashboard, it is not in version control and not in code review. There is one
rule today, so this is cheap; revisit if that changes.

### Caddy, and why a service is called `app`

Caddy fronts the stack and routes by path: `/auth`, `/health`, `/ready` and
`/status` to the status service, everything else to the dashboard. One origin,
so the session cookie is same-site and there is no CORS surface.

**The Caddy service is named `app`.** The tunnel's public hostname points at
`app:8080`, and that mapping is in the dashboard, so renaming the service means
editing the tunnel by hand to match. A stale route fails as a 502 with nothing
in the repo to explain it.

---

## Access to the box

The VPS is `v69720`: 4 cores, 15 GB RAM, 99 GB disk. It is **shared** — five
compose projects run on it, of which `stock-aggregator` is one.

- Deploys reach it over **Tailscale**, as an ephemeral `tag:ci` node
- The CI credential is an auth key that **expires 2026-12-03**
- Port 22 is currently also open publicly; closing it is a hardening step

The workflow checks that expiry date before joining and fails with a dated
message, because an expired key otherwise presents as a connection timeout that
reads like a network fault. Replacing it with an OAuth client removes both the
expiry and the check.

**Do not** assume the box is yours. Anything that eats CPU or disk affects four
other projects, and there is no resource limit configured on any of them.

---

## Secrets

Everything lives in **Infisical**, project `stock-aggregator`, environment
`prod`. `screener.secrets.load_into_environ()` pulls them into `os.environ` at
startup, before anything reads configuration.

The only credentials stored on the box are the three that authenticate that
exchange. Everything else is fetched with them and never touches disk.

To add a secret: put it in Infisical, read it through
`screener.config.env` in the config object of whichever subsystem owns it. Do
not add it to `screener.config.Settings` — that holds the database URL and
nothing else, deliberately, so a process that only posts an alert does not need
a database URL it never touches.

Behaviour worth knowing:

- **No credentials configured is a silent no-op**, which is how local runs and
  CI work with no stubbing.
- **A failed fetch is fatal.** Half a configuration fails later and somewhere
  less obvious.
- **Existing environment variables win**, so a `docker compose run -e …`
  override while debugging is not silently replaced.

---

## Models

`screener.ai` over OpenRouter. Narrative extraction, and the bot's replies.
**Never a score and never a sentiment number** — that is FinBERT's job, and a
model asked for either produces a confident answer with nothing behind it.

| Model | $/M in | $/M out | Context | Reach for it when |
|---|---|---|---|---|
| `upstage/solar-pro4` | 0.030 | 0.120 | 524k | Default for conversation. Cheapest here. |
| `deepseek/deepseek-v4-flash` | 0.086 | 0.171 | 1M | Extraction from one document. |
| `deepseek/deepseek-v4-pro` | 0.921 | 1.842 | 1M | A whole transcript, where the cheap model visibly struggles. |

Prices are indicative and for humans. **The real charge comes back on the
response** (`Completion.cost_usd`), because a local price table is wrong the
first time a provider changes a rate and silently wrong after that. The table
above was already stale once.

The model id is an allow-list: a typo falls back to the default rather than
matching some other provider's model and billing at a rate nobody chose.

**Spend control is a credit limit on the OpenRouter key**, not code. A cap
enforced by the provider cannot be defeated by a bug in our accounting.

---

## Shipping code

Merge to `main` and CI deploys it: build both images, push to GHCR tagged with
the commit SHA and `latest`, join the tailnet, copy the compose files, pull and
restart, then smoke-test from inside the container.

- Images: `ghcr.io/d1k03/stock-aggregator` and `…-web`
- The bot runs from the **same image** as the status service, different command

**Rolling back** is the Deploy workflow run manually with `image_tag` set to an
older SHA. No rebuild; it points the box at an image that already exists.

**Rollback across a migration is not supported.** There are no down migrations,
and an older image will start happily against a newer schema.

The smoke test probes from **inside** the container, never through the tunnel.
Cloudflare Access answers an unauthenticated request with a 302 to its login
page, and `curl -f` does not treat a redirect as a failure — a public probe
would go green against a completely dead application.

---

## The database

Postgres 16, in the stack, on the `pg_data` named volume. Not published to the
host, so it is reachable only from inside the compose network.

Migrations are plain numbered SQL applied by `screener.boot` under a **Postgres
advisory lock**, because the runner reads the applied set before running DDL and
two containers starting together would otherwise both run the same
`CREATE TABLE`.

**Never edit an applied migration**, and never rename one: the ledger keys on
the filename, so a rename makes it run again.

> **There are no backups.** The database is a volume on a VPS and nothing
> snapshots it. `deploy/README.md` has a manual `pg_dump`. This is the largest
> outstanding gap in the infrastructure and it grows every day there is data.

---

## Knowing whether any of it works

```bash
docker compose --env-file .env -f deploy/compose.prod.yaml \
  exec -T api python -m screener.boot selftest
```

One line per integration: database and migration count, build SHA, a direct
fetch, a proxied fetch **and whether its exit IP actually differs**, OpenRouter,
the Discord webhook, and the bot token. Anything unconfigured reports `SKIP`,
because switched-off is the expected state for most of it.

It posts nothing. Nothing in this project has a consumer yet, so this command is
the only thing that would notice a piece of it going quietly broken.
