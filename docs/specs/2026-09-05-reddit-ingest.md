# Reddit ingest

*Status: implemented. Written 2026-09-05.*

Posts and comments from r/wallstreetbets, r/stocks and any other subreddit
named in configuration. A week backfilled once, then kept up every six hours.
Ingest only: nothing here connects an item to a security or scores it.

## The route, and why it is not Reddit

**Reddit's own API is unavailable, and this was tested rather than assumed.**
`https://www.reddit.com/r/stocks/new.json` answers **403 with an HTML body**
whatever User-Agent is sent, and `https://www.reddit.com/robots.txt` is:

```
User-agent: *
Disallow: /
```

`CLAUDE.md` says "APIs before scrapers… respect robots.txt and ToS", so
scraping reddit.com is ruled out by this project's own constraint, and this
does not do it.

The official OAuth route, which `DESIGN.md:136` names, has two problems beyond
availability. A new client needs manual approval under the Responsible Builder
Policy — weeks, with a real chance of silent refusal. And listings are capped at
about 1,000 items with no historical access, which does not reach a week of
r/wallstreetbets at 788 posts and 132,052 comments.

**Arctic Shift** is a public mirror with date-range search over both posts and
comments. It needs no key, is current to within minutes, and has the historical
reach the official listings do not. It is also one volunteer-run service and a
single point of failure, and it serves data Reddit's Public Content Policy
restricts. It is an API rather than a scraper, which is why it clears the
robots.txt rule, but it is a grey area and choosing it is a choice.

## Measured volume

| | per week | raw JSON |
|---|---|---|
| r/wallstreetbets posts | 788 | 3 MB |
| r/wallstreetbets comments | 132,052 | 260 MB |
| r/stocks posts | 270 | 1 MB |
| r/stocks comments | 14,944 | 29 MB |

Comments are 99% of it. Keeping Reddit's envelope whole is ~290 MB/week; keeping
the fields that carry meaning is roughly a third of that, so ~2.7 GB/year.

## Decisions

**D1 — Extract, and drop the envelope.** Kept: id, subreddit, kind, parent,
author, created, score, title, body, permalink. Dropped: flair, awards, and the
sixty-odd null fields Reddit sends.

This sits against "every score traces back to visible raw inputs", so the
reading it rests on should be arguable rather than assumed: **the body text is
the raw input** for anything sentiment ever does with it, and what is discarded
is not evidence. A future score traces to the exact words that produced it.

**D2 — A new table, not `ingest_observation`.** That table's `security_id` is
`not null` and references `security(id)`. A Reddit post mentions zero, one or
many tickers. Making the column nullable would weaken the traceability chain for
every source that does have exactly one security, to accommodate one that never
does. `social_item` is the answer; it holds no reference to a security at all,
because deciding which tickers an item mentions is its own piece of work.

**D3 — Hash per item, not per response.** `DESIGN.md` records that content-hash
dedup never fires for Yahoo: one bundled `quoteSummary` carries eight fields
that move on a trading day, so the hash changes nightly and the write is never
skipped. The proposed remedy there was to hash the unit that is stable. Here
that unit is obvious — a comment body almost never changes — so the hash covers
id, title, body and author, and deliberately **not** `score`, which moves every
time anyone votes. Verified: a second pass over the same window inserts nothing
and rewrites nothing.

**D4 — The walk goes backwards.** Arctic Shift answers newest-first. Measured,
not assumed, and getting it wrong is quiet: a forward walk takes the newest
hundred, jumps its cursor to the end of the window and stops, so a week's
backfill returns one page and every count still looks plausible. That happened
during implementation and is why it is written down here.

**D5 — One `ingest_run` per subreddit and kind, one `audit.event` per pass.**
`ingest_run` is what that table is for; `securities_requested` and
`securities_ok` stay null because they do not apply to a source that is not
per-security. `record()` opens a connection per call and the same table backs
Steven's memory, so per-item rows are out.

**D6 — The `bot` container shape, not the `transcribe` one.** This needs httpx
and psycopg and nothing else, so it is the api image with a different command:
no second Dockerfile, no fourth CI build, no extra image to pull. No healthcheck,
for the reason the bot has none — a check cannot tell "sleeping until the next
pass" from "wedged".

The wait is a `threading.Event`, not `time.sleep`: a signal handler cannot
interrupt a six-hour sleep, so SIGTERM would be answered whenever it happened to
finish and every deploy would sit through the full SIGKILL timeout.

## On throttling, and on the proxy

Arctic Shift signals "slow down" with **HTTP 422** carrying
`{"error": "Timeout. Maybe slow down a bit"}`, not 429. Treating that as fatal
ends a backfill early with most of the window unread.

**Measured on the VPS**, walking a day of r/wallstreetbets comments: **20
refusals across 127 pages**, about one in six, every one recovered by the retry.
A shallower run of sixty pages saw none at all, which is recorded here only
because it is the wrong conclusion — the refusals arrive as the walk goes
deeper, so a short test says the opposite of the truth. That mistake was made
during implementation and is what the two-span resume in D7 exists for.

**The Bright Data lanes were considered and are not used.** Retries recover, so
there is no block that needs routing around, and Arctic Shift is run by
volunteers — rotating four exit addresses at a service whose error message asks
for less traffic is a different act from spreading load across a commercial API.
`REDDIT_DELAY_MS` is the knob if they ever ask. The day-wide window chunking is
politeness rather than a fix for a measured problem, and the code says so.

## D7 — Two spans per pass, so an interrupted backfill finishes

The walk runs backwards from now. An interruption therefore leaves the *newest*
slice stored and the older end missing, and resuming from `max(created_utc)`
alone never comes back for it: the hole is permanent and silent, and at one
refusal in six on a 889-page backfill it is the ordinary case rather than the
unlucky one.

So each pass runs two spans: catch up from the newest item to now, and — when
the oldest item stored has not reached the backfill target — go back for what
the last pass did not reach. Newest span first, so a fresh comment is never
delayed behind a long catch-up. Successive passes close the window.

## What this does not settle

**Where raw payloads land** is still open, and this does not close it: nothing
here writes a blob, because D1 keeps only extracted fields.

**Six-hourly does not fit the daily snapshot model.** `metric_daily`,
`pillar_score_daily` and `snapshot_daily` are keyed on `as_of date` and
`scoring_run.cutoff_offset` is per-run rather than per-source. Which six-hour
window feeds a score is a decision for whoever writes scoring.

**Turning this into a metric is a `scoring_logic_version` bump.** A new metric
entering an existing pillar moves that pillar for every ticker on the night it
lands, and the diff step reads that as a universe-wide set of crossings.

**~2.7 GB/year, and there are no backups.** This is the first thing that will
make the database genuinely expensive to lose. A retention policy is not
enforced and should be.

**Deleted content.** A deleted comment stops appearing and nothing prunes what
was already stored.
