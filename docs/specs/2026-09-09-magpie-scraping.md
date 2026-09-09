# Magpie: a document scraping engine

*2026-09-09*

## The problem

Hand it a link and it should get the article: cheapest route first, escalating
when that fails, extracted and stored. Steven should be able to be told "scrape
this" and have it kept. Everything gathered should be queryable in
`/playground`. Later — not here — it should crawl outward from a source to the
links around it.

**Most of that is already built and has never run.** `screener.fetch` is an
ordered chain of named strategies: `direct`, `isp_proxy`, `unlocker`. Any
exception or an empty 2xx escalates to the next; a `validate` callback lets a
caller reject a body and force escalation; `FetchResult.strategy` records which
rung answered. `STRATEGIES` is a registry, so a new rung is one function and one
dictionary entry.

No production code has ever named `unlocker`. `boot/selftest.py` is the only
caller in the tree that names any proxy strategy, and only to prove the exit
address differs from the box's own.

So this cycle is not a fetcher. It is the four things missing around one:

1. **Extraction.** Nothing here parses HTML except a hand-rolled `HTMLParser` in
   `universe/sources/wikipedia.py`.
2. **Somewhere to put a document.** `ingest_observation` cannot hold one: its
   `security_id` is `not null` and an article mentions zero, one or many
   tickers. Making that column nullable would weaken traceability for every
   source that does have exactly one security, to accommodate one that never
   does — the argument `012_social.sql` already made for `social_item`.
3. **robots.txt.** `CLAUDE.md` requires respecting it and there is no
   implementation. Today it is a human policy applied when choosing a source,
   which is why `screener.reddit` reads a mirror instead of reddit.com.
4. **A control surface.** Somewhere to add a source and see what came back.

## Decisions

**D1. The ladder is not rebuilt.** Magpie names the strategies it is willing to
use, in order, and passes a `validate` callback. `screener.fetch` keeps owning
how bytes arrive; magpie owns which document is worth acquiring and at what
price. *Rejected:* a magpie-local fetcher, which would be a second HTTP path
with its own redaction, its own timeouts and its own bugs.

**D2. Extraction is trafilatura, and never its downloader.** `bare_extraction`
takes an HTML string and does no network I/O at all; it returns title, author,
date, language, canonical URL and cleaned text. The library also ships
`fetch_url`, which would bypass the proxy ladder *and* the spend cap without
anything looking wrong. A test asserts the extraction half opens no socket,
because that is the failure that would not otherwise be noticed.

**D3. The content hash is over the extracted text, not the response.**
`screener.reddit` recorded this lesson from Yahoo: hashing a whole response
meant the hash changed nightly and dedup never fired once. An article page
carries ad slots, nonces, view counters and a rendering timestamp, all of which
move on every fetch. The headline and the body do not, so those are what is
hashed.

**D4. robots.txt is mechanical, and a refusal is terminal.** stdlib
`urllib.robotparser`, one fetch per host, cached, honouring `can_fetch` and
`crawl_delay`. A disallowed URL is refused and **does not escalate** — escalating
past a `Disallow` would turn "respect robots.txt" into "route around
robots.txt", which is the opposite of the constraint. *Rejected for now:*
`protego`, which is more faithful to RFC 9309 on wildcards. That matters for a
broad crawl and not for a URL a person pasted; it is the documented upgrade when
crawling lands.

**D5. Its own container, with a thin client.** The extraction half holds
trafilatura and lxml; the client half is `httpx` and nothing more, and is what
the status service and the bot import. Exactly the split `screener.transcribe`
draws, for the same reason: the three runtime dependencies stay three, and the
two processes that want a document do not pay for lxml to ask for one.

**D6. Its own `magpie` schema, readable by Steven.** The three grounds `auth`,
`audit` and `skybird` each give: nothing here is a fact or a score, no scoring
query joins to it, and a document outlives the page it was taken from.

Granting `playground_bot` is a **deliberate departure from skybird**, where
Steven drives captures and cannot read a transcript back. That asymmetry existed
because the transcript was not the point of giving him the controls. Here
reading *is* the point — "look into this article" is the feature — so the text
is granted, and the departure is recorded rather than left to look like an
oversight.

**D7. The tool stores; it does not summarise.** A tool result is capped at 400
characters and is re-sent as context on every following round, so an article
cannot come back through one. `scrape` returns what the document *is* — title,
host, date, length, id, and which rung served it — and the text goes to the
store. Summarising at scrape time is a real option and it has a real per-document
price; it is a later decision, not a default.

**D8. A paid fetch says what it cost, on the row and in the trail.**
`budget.spent_24h()` already sums `cost_usd` across every audit event rather than
only model calls, so recording a real cost on a scrape's audit row means the
existing `DAILY_SPEND_CAP_USD` meters proxy spend with no new mechanism, folding
Discord onto GitHub the same way it does for replies. This makes `scrape` the
first tool in the project that spends money.

The strategy is stored on the document row too, not only in the trail: a
document that cost money should say so where it is read.

**D9. Politeness lives here, not in `screener.fetch`.** D6 of the infrastructure
spec keeps throttling out of the fetch layer — the pool never sleeps, never
retries, never throttles, and a source's rate limit is the source's business.
`crawl_delay` and the floor between two requests to one host are magpie's.

**D10. The ladder stays extensible.** The order is configuration, not a
constant, and `STRATEGIES` is a registry. A Wayback rung before the paid one, or
a headless rung after it, is one function and one entry. None are added here.

## What this deliberately does not do

- **No crawling.** One URL in, one document out. A frontier, a link graph and a
  politeness budget are a cycle of their own, and building the queue before the
  fetcher has been proven is how it gets built twice.
- **No accounts, no logins, no paywalls.** Staying logged out is what keeps this
  on the right side of the line, and it is exactly what changes when an agent
  starts holding credentials. That is a direction worth taking and it needs its
  own spec.
- **No summarisation.** D7.
- **No new fetch strategies.** D10 says the ladder is extensible; this cycle
  extends it by zero rungs.
- **No JavaScript rendering** beyond whatever the Web Unlocker does server-side.

## Terms of service

`DESIGN.md` says APIs before scrapers, and respect robots.txt and ToS. This is
the first code in the project that enforces the first half rather than applying
it by hand when choosing a source, and D4 is why a refusal cannot be escalated
past.

The second half is recorded here as a known position rather than left for
somebody to discover. Full article text in a private store is a reproduction:
this is private research, it is never redistributed, nothing is served back out,
and the raw payload is kept as evidence of what was read on a date rather than
as a library. Nothing here reads a page behind a login or a paywall, and the
engine has no credentials with which to try.

## Cost

One new image — trafilatura and lxml, roughly 15 MB over the base — and a fifth
CI build. No new paid service.

The rungs are not evenly priced and the design leans on that. `direct` is free.
`isp_proxy` is four exit addresses on a flat monthly plan a sibling project
already pays for, so its marginal cost per request is zero. The Web Unlocker is
roughly $0.002 per **successful** request, billed only on success, and is
reached only when both free rungs have failed. The £5–10/month target is
unaffected unless the paid rung is being reached routinely — which is the thing
D8's cost recording exists to make visible before the invoice does.
