# Yahoo exit lanes

*Status: implemented. Written 2026-09-05.*

Bright Data exit lanes for the direct Yahoo client: a long-lived session pinned
to one exit address, and a pool that rotates over several of them.

## The problem

`screener.fetch.fetch()` is stateless by construction — a fresh `httpx.Client`
per call, and the chain is the retry. Yahoo's `quoteSummary` needs the opposite:
a crumb is only valid alongside the cookie issued with it, so the jar has to
outlive the call. `screener.universe.sources.yahoo` already solves that with one
client held for the run, and documents why it is the one module that does not go
through `fetch()`.

The consequence nobody had noticed: **that client took no proxy argument, so
there was no way to put Yahoo traffic on Bright Data at all.** Nightly ingest is
~3,000 requests (fundamentals and prices for every ticker) leaving one VPS
address, which is exactly the gap `DESIGN.md` names as untested — the
1,506-request measurement ran from a development machine, and per-IP limits
attach to that IP.

## Decisions

**D1 — A lane is the unit of rotation.** One client, one jar, one exit, for the
length of a run. `screener.fetch.lanes` provides `Lane` and `LanePool`;
`acquire()` rotates round-robin, `park(seconds)` takes a lane out after a 429.

Rotation happens on **every** request, not only after a failure. A pool that
stayed on one lane until it broke would put a whole run through one address and
leave the rest idle, which is the rotation not happening.

**D2 — Pin by address, not by session token.** Bright Data picks an exit from
flags on the proxy username. `-session-x` returns the same address for the same
token, but a *new* token is a fresh random draw from the zone rather than a
rotation. Measured against the live zone: twelve random draws over four
addresses came back **5/3/2/2**. `-ip-A.B.C.D` pins one specific allocated
address, verified holding on all four with no 502. So `BRIGHTDATA_PROXY_IPS`
takes the addresses and each becomes one lane.

`isp_proxy` keeps its per-request session draw. It is for a one-off request that
got blocked, where a random address is the whole point.

**D3 — A jar per lane, for a reason that is not the obvious one.** The obvious
argument — a crumb is bound to the cookie, and the cookie to the address that
got it — is **false, and was tested rather than assumed**: a crumb and jar issued
through `88.223.247.250` and replayed through `85.28.44.208` returns 200. Yahoo
binds a crumb to the jar and not to the exit.

Lanes still keep their own jars, on grounds that survive that result: four
addresses sharing one cookie is the pattern bot detection is built to notice,
Yahoo's crumb scheme is undocumented and has changed before, and an
`httpx.Client` owns its jar anyway — sharing one would mean copying cookies by
hand. Recorded because the false version is the one that sounds right.

**D4 — The pool never sleeps, never retries, never throttles.** D6 of the
infrastructure spec keeps rate limiting out of the fetch layer and puts it with
ingest, and this does not reverse it. `park()` records a caller's number;
`acquire()` reads it when choosing and hands back the soonest-free lane when
every lane is parked, with `parked_for` still set. Every `time.sleep` on the
path stays in `yahoo.py`.

The call site reads `parked_for` as a boolean rather than a duration, so the
existing single-lane backoff test still asserts `slept == [0.5]` exactly.

**D5 — Yahoo starts on the lanes rather than falling back to them.** This is an
exception to `docs/infrastructure.md`'s "reach for it when you have seen a
block", and it is an exception on measured grounds rather than caution: Yahoo is
**not** currently blocking the VPS (10/10 direct calls succeeded), and a pinned
lane costs **1.07x** direct latency — 0.145s against 0.136s — so spreading a
3,000-request night over four addresses costs about twenty-four seconds.

`fetch()` is untouched: `DEFAULT_STRATEGIES` is still `("direct",)` and the test
enforcing it still passes. Yahoo is a caller that names the proxy, which is what
the default was always guarding.

**D6 — Unconfigured is one direct lane, and that is the off switch.** With no
`BRIGHTDATA_PROXY_IPS` the Yahoo client is byte-for-byte what it was before this
change. `LanePool.from_env()` otherwise *raises* when nothing is configured —
only Yahoo passes `fallback_to_direct=True` — because a caller that asked for
proxied lanes and silently got its own address is the failure this exists to
make visible. `--no-proxy` on `universe refresh` forces the direct pool.

**D7 — A crumb failure stays fatal for the whole run, even with lanes.** A
handshake failing on a paid exit almost certainly means Yahoo is blocking that
address, which is the thing worth noticing; continuing at three-quarter
throughput would hide it.

*Open item:* park a lane after N consecutive 401s, for the case where an address
is reallocated mid-run. It needs a failure-counting rule, and ingest is better
placed to choose one.

## Measurements (2026-09-05, from the VPS)

| | |
|---|---|
| Box's own exit | `45.13.238.205` |
| Zone exits, 12 random session draws | 4 distinct, hit 5/3/2/2 |
| `-ip-` pinning | holds on all four, no 502 |
| Yahoo over a pinned lane | cookie `A3`, 11-char crumb, `quoteSummary/AAPL` 200 |
| Crumb replayed via another exit | **200** — not exit-bound |
| Latency, 10 sequential `quoteSummary` | direct 0.136s, pinned lane 0.145s (1.07x) |

## What was not built

No concurrency across lanes — `2026-09-05-universe-and-identity.md` says "No
concurrency", and four lanes are not four workers. No rate limiter, retry or
adaptive pacing in `fetch`. No per-lane health scores or automatic retirement;
nothing has throttled us, so a fixed cooldown the caller chose is enough. No
persisted lane state. No spend counter in code — caps live at the provider. No
auto-discovery of the zone's addresses through Bright Data's API, which would
buy a startup dependency on their control plane to avoid pasting four addresses
into Infisical once.

## Verifying on the VPS, after this merges

Merging is not enough on its own, and that is deliberate. `ci.yml` calls
`deploy.yml` once `ci-ok` passes on main, so the image rolls out unattended, but
`BRIGHTDATA_PROXY_IPS` does not exist in Infisical yet. Until it does the
deployed code is a single direct lane and `fetch lanes` reports SKIP, which is
the safe state rather than a broken one. It also means the deploy proves nothing
about lanes by itself.

The order that actually tests it:

1. Merge, and let the deploy finish.
2. Run the self-test before changing anything. `fetch lanes` must report **SKIP**,
   and `universe refresh` must behave as it always has. This is the half worth
   checking first, because it is the half that runs if the addresses are ever
   cleared.
3. Add `BRIGHTDATA_PROXY_IPS` to Infisical `prod`:
   `88.223.247.250,85.28.44.208,158.46.157.31,158.46.203.229`
4. Restart `api` so `load_into_environ()` picks it up.
5. Run the self-test again. `fetch lanes` must name four **distinct** exits, none
   of them the box's own `45.13.238.205`.
6. Run `python -m screener.universe refresh` in the container and check the run
   log: requests should spread across all four addresses, `crumb_fetches` should
   be 4 rather than one per symbol, and the CSV diff should hold only genuine
   sector and industry changes. Re-run with `--no-proxy` and the CSV should be
   identical, because the proxy changes which address a request leaves by and
   nothing about what comes back.

Steps 1 to 6 were already exercised against the live zone before this landed, by
mounting the changed files over the deployed image in a throwaway container:
four lanes on four distinct exits, twelve of twelve symbols resolved, and
`crumb_fetches` of exactly 4. What that rehearsal could not cover is the crumb
refresh path, because the run was too short to expire one. Expect the first real
overnight job to be the first time that code executes.

---

## Refined: one request in flight per exit

*Added 2026-09-05, after the lanes were deployed and measured under real load.*

The plan above says "rotation is not concurrency" and that the pool must never
grow a `map()`. It now has one, called `across`, and this is the record of why.

**The decision it reverses rested on a withdrawn figure.**
`2026-09-05-universe-and-identity.md` says "No concurrency", and the evidence
given is that eight yfinance workers lost 43% of 1,506 requests. `DESIGN.md`
withdrew that figure for varying concurrency and request count at the same time.
So the recorded reason for the rule no longer stands on anything, which is not
the same as the rule being wrong, but does mean it has to be re-argued rather
than inherited.

**What was measured**, on the VPS, against the live Yahoo endpoints:

| | requests | wall clock | per request | outcome |
|---|---|---|---|---|
| Sequential, 4 lanes round-robin | 1,006 | 138s | 136ms | 1,006 × 200 |
| One worker per lane, prototype | 1,006 | 35.2s | 35ms | 1,006 × 200 |
| One worker per lane, `across` as shipped | 1,006 | **43.8s** | 44ms | 1,006 × 200 |

The shipped figure is the one to quote. It is slower than the prototype that
argued for this, and the difference is not explained: it may be the cold
container it ran in, or run-to-run variance against a third party, and one run
each is not enough to tell those apart. **3.2x, not 3.9x**, is what has actually
been demonstrated.

Median per-request latency was 127ms under load against 136ms sequential, so
nothing degraded. A separate isolating run showed where the ceiling is not:

| | per request | gain |
|---|---|---|
| direct, 1 worker | 111ms | — |
| direct, 4 workers, one address | 30ms | 3.7x |
| lanes, 4 workers, four addresses | 36ms | 3.1x |

Neither Yahoo nor Bright Data was the bottleneck. Doing one request at a time
was. The lanes are marginally slower than direct, which is the same ~7% proxy
overhead the original measurement found.

**The claim being made is narrower than "concurrency is fine".** It is *one
request in flight per exit address*. Four workers over four addresses is not
four workers over one, and that distinction is the entire argument. So `across`
takes no worker count: concurrency is `len(pool)` and there is no way to ask for
more. A knob would let the distinction be lost by someone in a hurry, and the
property is worth more as a fact about the type than as a note in a docstring.

**What this does not establish.** It was one 35-second burst, not a nightly job
run for a month, and Yahoo's tolerance is undocumented and can change without
telling anyone. The first sign of it changing will be a 429, which parks a lane
and is already handled. If they arrive in numbers, the fix is to go back to
`acquire` and lose 100 seconds a night, which is a price a job with no deadline
can pay.
