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

### When one session has to outlive the request

`fetch()` builds a client per call, so anything whose authorisation lives in a
cookie cannot use it: the jar is gone before the next request needs it. That is
what `LanePool` is for.

```python
from screener.fetch import LanePool

with LanePool.from_env(headers=BROWSER, timeout=25.0) as pool:
    lane = pool.acquire()          # rotates on every call
    response = lane.get(url)       # never raises on status
```

A **lane** is one client, one cookie jar, one exit address, held for the length
of a run. A pool hands them out round-robin, so a long job leaves by every
address the zone holds instead of piling onto one. `park(seconds)` takes a lane
out of rotation after a 429 and `acquire()` skips it until it frees.

Two things it deliberately is not. It is **not a strategy** — it does not appear
in the table above and `fetch()` cannot reach it. And it is **not a rate
limiter**: the pool never sleeps and never retries, so the waiting and the
numbers stay with the caller, which is where D6 puts them.

`pool.across(items, work)` is the one place concurrency lives, and it runs
**one worker per lane and has no argument to run more**. The claim it rests on
is not "concurrency is fine" but the narrower "one request in flight per exit
address": four workers over four addresses is not four over one. Measured before
it was allowed — the whole S&P 500, 1,006 requests, 44s against 138s sequential,
every one a 200. `acquire()` is still the sequential path and still does not
change how many requests are in flight.

Yahoo is the only caller today, through `screener.universe.sources.yahoo`.

### Bright Data specifically

An ISP proxy zone with **four UK exit IPs**, on a flat monthly plan that a
sibling project already pays for. That is the whole reason it is available here:
the marginal cost of *having* it is zero.

There are two ways to reach them, and they are not interchangeable. `isp_proxy`
draws a **fresh random session per request**, which is right for a one-off
request that got blocked — but it is a draw, not a rotation: twelve draws
against the live zone came back 5/3/2/2 across the four addresses. `LanePool`
**pins** one lane per address with an `-ip-` flag, which is the only way to get
four addresses used evenly, and the only form that can hold a cookie. Set
`BRIGHTDATA_PROXY_IPS` to the addresses; leave it unset and there are no lanes.

The self-test checks both: that a proxied exit differs from the box's own, and
that the configured lanes differ **from each other** — four lanes quietly
sharing one address is billed, looks healthy, and spreads nothing.

**Do not** reach for it because a source is slow, or intermittently 500s, or
because you are not sure. Reach for it when you have seen a block — with one
exception, written down because it is an exception. Yahoo starts on the lanes
rather than falling back to them, since a night's ingest is ~3,000 requests off
a single VPS address and a pinned lane measured 1.07x direct latency, so
spreading them costs about twenty seconds. Clearing `BRIGHTDATA_PROXY_IPS` is
how that is switched off.

The Web Unlocker is a separate product billed per successful request and should
be treated as spending money every time it runs. It cannot be a lane: it POSTs
each URL as an independent call, so there is no jar to keep.

---

## Speech to text

`screener.transcribe` — faster-whisper on CPU, in a container of its own,
reached over the compose network the way the chart renderer is.

```python
from screener.transcribe import transcribe

spoken = transcribe(audio_bytes)     # Transcript | None, never raises
```

A Discord voice message in a DM is transcribed and answered as though it had
been typed, with what was heard quoted above the reply. The dashboard's mic
button puts the transcript in the composer instead, so a misheard ticker is
corrected before a model call is paid for.

| | |
|---|---|
| Cost | CPU only. No per-minute billing, and no audio leaves the box. |
| Model | `base.en`, int8, baked into the image. `initial_prompt` biases it toward tickers. |
| Cap | two minutes, enforced by the callers before anything is downloaded |

**It is the first service here with a resource limit**, which contradicts the
absence of one everywhere else: two cores and a gigabyte. Everything else in
this stack is idle until asked a question, and this saturates a core for as long
as the clip is, on a box shared with four other compose projects. The limit and
`WHISPER_THREADS` have to agree, or ctranslate2 spawns one thread per core it can
see and spends its time being descheduled inside the quota.

**Do not** reach for a bigger model first when it mishears a ticker. The
`initial_prompt` in `transcribe/server.py` is free and `small.en` costs roughly
three times the CPU.

**The audio is never written anywhere**, on disk or otherwise: it is held for one
request and dropped, and the transcription's own audit row records how long the
clip was rather than what was in it. The question does reach the trail on the
reply row, exactly as a typed question does, because that is where Steven's
memory lives, and a spoken question is a question. The `voice` flag on that row
is what tells a transcription error from a typo when reading it back.

---

## Social ingest

`screener.reddit` — posts and comments from **Arctic Shift**, a public Reddit
mirror, on a six-hourly loop in a container of its own.

**Reddit's own API is not used, and that was checked rather than assumed.**
`reddit.com/r/stocks/new.json` answers 403 with an HTML body whatever
User-Agent is sent, and `robots.txt` is `Disallow: /` for every agent — so
scraping it is ruled out by this project's own rule. The official OAuth route
needs a manually approved client and caps listings at about a thousand items,
which does not reach a week of r/wallstreetbets in any case.

Measured, per week: 788 posts and 132,052 comments on r/wallstreetbets, 270 and
14,944 on r/stocks. Comments are 99% of it. Reddit's envelope is dropped and the
fields that carry meaning are kept, which is roughly a third of the size.

| | |
|---|---|
| Cost | bandwidth only. No key, no per-request price, ~2.7 GB/year of rows |
| Cadence | `REDDIT_REFRESH_HOURS`, backfilling `REDDIT_BACKFILL_DAYS` the first time it sees a subreddit |
| Switch | an empty `REDDIT_SUBREDDITS`; the container logs it and exits cleanly |

**Do not** point the Bright Data lanes at it to go faster. The mirror does
refuse about one page in six on the busiest subreddit, and the retry recovers
every time — so there is no block that needs routing around, and Arctic Shift is
run by volunteers, which makes rotating four exit addresses at a service whose
error message asks for less traffic a different act from spreading load across a
commercial API. `REDDIT_DELAY_MS` is the knob if they ever ask for less.

**Do not** add per-item audit rows. One `ingest_run` per subreddit and kind, and
one `audit.event` for the pass: `record()` opens a connection per call and the
same table backs Steven's memory.

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
fetch, a proxied fetch **and whether its exit IP actually differs**, the
configured lanes **and whether they differ from each other**, OpenRouter, the
Discord webhook, the bot token, and the social mirror **and how far behind it
is** — freshness rather than reachability, because a mirror that has quietly
stopped keeping up still answers. Anything unconfigured reports `SKIP`,
because switched-off is the expected state for most of it.

It posts nothing. Nothing in this project has a consumer yet, so this command is
the only thing that would notice a piece of it going quietly broken.
