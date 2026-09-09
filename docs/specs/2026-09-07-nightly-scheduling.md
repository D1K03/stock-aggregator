# Nightly scheduling

Status: agreed, not implemented. Written 2026-09-07.

A container that runs the pipeline once a night: prices, then fundamentals, then scoring, at
23:00 UTC, recovering from a restart and saying so in Discord when a night is lost. Standing
decisions live in `DESIGN.md`; the commands it runs are specified in
`docs/specs/2026-09-05-price-ingest.md`, `docs/specs/2026-09-06-fundamentals-ingest.md` and
`docs/specs/2026-09-05-scoring.md`.

Scope: **when the existing commands run, and what happens when one does not.** No command
changes. Nothing new is ingested, nothing new is scored, and no alerting rule is evaluated.

---

## 1. What this has to satisfy

1. **The forward log has to start.** `DESIGN.md` says fundamentals are restated rather than
   point-in-time, so history cannot be reconstructed and the forward log of daily snapshots is
   the only credible basis for backtesting weights. Scoring D2 makes that concrete: runs are
   forward-only and `--as-of` refuses a past date, so **a night not run is a night that can
   never be recovered**. Every day without a scheduler is a permanent hole.
2. **A night must survive a deploy.** `deploy.yml` runs `compose up -d`, which recreates changed
   containers — including, sooner or later, this one, mid-run.
3. **A lost night must be noticed.** Nothing watches the logs, and the alerting cycle does not
   exist.
4. **A night must not be noisy.** One security fails every night for reasons already understood;
   a scheduler that reports that is one nobody reads.
5. **No new runtime dependencies**, and no scheduling configuration outside this repository.

---

## 2. Findings

**F1 — the run the schema spec describes is one the CLI refuses.** The schema spec sizes
`cutoff_offset` for "a live run scoring D at 02:00 the next morning". But
`screener.scoring.cli` refuses an `--as-of` in the past, and at 02:00 on D+1 the date D is in
the past. A 02:00 run can therefore only score *today* — a day whose market has not opened, so
the snapshot would carry the previous session's close under tomorrow's date. The trigger hour is
what resolves this, not a code change (D2).

**F2 — a daytime price pass can only add an in-progress bar.** Everything earlier is already
stored by the previous night, so the entire new content of a midday fetch is today's unfinished
session. `price_daily` holds daily bars and has no way to mark a row partial, and it is granted
to `playground`, `playground_bot` and `playground_mcp` — so the console, Steven's `sql` tool and
the claude.ai connector would each read it as a close. The clock leaves no safe window: US
markets run 13:30–20:00 UTC, so a pass before the open adds nothing at all, a pass during the
session adds exactly the partial bar, and a pass after 20:00 is the nightly run. Hence D10.

**F3 — nothing schedules anything today.** No cron, no systemd timer, no `schedule:` workflow,
nothing in `compose.prod.yaml`. `ingest prices`, `ingest fundamentals` and `scoring run` happen
only when a human types them, which is what makes requirement 1 urgent rather than tidy.

---

## 3. Decisions

**D1 — A container with a wake loop, following `screener.reddit`.** `python -m screener.nightly`
as its own compose service, PID 1, waiting on a `threading.Event` rather than `time.sleep` — for
the reason reddit's own docstring gives: a signal handler cannot interrupt a long sleep, so
SIGTERM would be answered whenever the sleep happened to end and every deploy would sit through
the full SIGKILL timeout.

`NIGHTLY_ENABLED` is the off switch, defaulting to true, and it is an explicit boolean rather
than something inferred. Reddit switches off by holding an empty subreddit list, which works
because its work is described by data; a night has no such list, so an unset
`NIGHTLY_TRIGGER_HOUR` must mean the default hour and never silence. The switch exists so a night
can be stopped during an incident without editing compose.

Host cron and a scheduled GitHub Actions workflow were both considered. Cron would put the
schedule outside this repository, and `CLAUDE.md` already names two such things — the Caddy
handles and Cloudflare's bot rule — as configuration that "will fail invisibly"; a third is not
worth the simplicity. Actions would keep the schedule in the repository but makes the nightly
pipeline depend on GitHub being up, and its cron is best-effort, routinely firing more than ten
minutes late.

**D2 — 23:00 UTC on D, scoring D.** Per F1, this is the hour that makes the run legal and the
snapshot honest: each snapshot carries the date of the close it reflects, and `--as-of` never
sees a past date. US markets close at 20:00 UTC in summer and 21:00 in winter, so this leaves
two to three hours for Yahoo to settle — and the seven-day settling window exists precisely
because Yahoo revises recent sessions, so exactness is not required.

**UTC, not local time.** `visibility_cutoff` builds from UTC midnight, so a local-time trigger
would move twice a year relative to arithmetic defined in UTC — for no gain, since no human is
awake for either.

`cutoff_offset` is unchanged at `'1 day 6 hours'`. At 23:00 the cutoff of D+1 06:00 is simply in
the future, so everything fetched is visible, and a backfill run still evaluates the identical
expression — which is the property that makes the offset an offset.

**D3 — Clock-aligned, with a catch-up check on boot.** The loop computes the wait to the next
23:00 UTC rather than sleeping a fixed 24 hours: a pure interval drifts by the length of each
pass, about ten minutes a day and five hours a month, and D2's whole argument is about the hour.

On startup it asks the database whether tonight is already done — a `live` `scoring_run` covering
today with `outcome = 'ok'` — and if it is not, **and the trigger hour has already passed**, it
runs immediately. Both halves of that condition matter: without the first, a restart would score
a night twice; without the second, a container starting at 10:00 would run the night thirteen
hours early, against a market still open.

This is what makes requirement 2 hold. A deploy at 23:03 leaves a half-finished night; the new
container boots, finds today unscored, and finishes it. Under a bare clock-aligned wait that date
would be lost, permanently, per requirement 1.

Waking every few minutes and asking the same question continuously was considered and rejected:
it recovers just as well, but a night that fails for a real reason would retry every tick — 288
attempts a day, each around 3,000 Yahoo requests.

The pieces this leans on are already built and tested: scoring takes an advisory lock so a second
process refuses rather than double-scoring, and `reconcile` settles a run left behind by a
process that died.

**D4 — One sequential pass.** Prices, then fundamentals, then scoring, on one autocommit
connection — autocommit because both ingest halves commit per security and `run_scoring` commits
its run row before the writes it wraps.

Ordering is by construction rather than by clock arithmetic. Three separately-triggered jobs
would encode "scoring runs after ingest" as an assumption about gaps between trigger times, which
breaks the first evening ingest runs long.

`night.py` calls `run_prices`, `run_fundamentals` and `run_scoring` directly rather than shelling
out to the CLI: exceptions rather than exit codes, and testable without a subprocess.

**D5 — Scoring is gated on prices having wholly failed, and on nothing else.**
`IngestReport.status` is already `'failed'` exactly when `ok == 0 and requested > 0`, so "wholly
failed" needs no new threshold — the price cycle defined it.

The gate exists because `cutoff_offset` filters on `observed_at`, so yesterday's bars remain
visible: scoring after a dead ingest would produce a complete-looking snapshot from stale data,
and the crossing diff would later read it as "nothing moved". A silent-wrong shape, of the kind
this pipeline keeps having to design against.

**Prices only, deliberately.** Scoring reads bars and nothing else this cycle — no ratio consumes
a fundamental fact yet — so a fundamentals failure must not block a scoring run that does not
depend on it. **When the ratios cycle lands, this gate widens to fundamentals**, and that is
recorded here because it is exactly the kind of thing a later cycle misses.

**D6 — Three attempts, then the night is given up.** Backing off five minutes, then fifteen.
Deliberately unlike reddit's retry-every-five-minutes-forever: reddit's pass is cheap and
idempotent, while a night is around 3,000 requests, and hammering a source having a bad evening
is how a rate limit nobody has hit becomes one that has been.

Two failures are not retried at all. `ScoringInProgress` means another process holds the lock, so
this one steps aside rather than arguing with a run that is working. `NoBarsVisible` is not
transient, and spending three attempts on it delays nothing but the log entry.

**D7 — A partial ingest is a success.** `CWEN-A` fails every single night: measured as the only
failure in 3,012 requests across a full night, a symbol-formatting problem with nothing
authentication- or limit-shaped behind it. A scheduler that treated any per-security failure as a
night failure would therefore notify every night, for ever, and requirement 4 would be violated
on the first evening.

A night fails when the whole night failed: prices wholly failed, scoring was gated, or something
raised.

**D8 — Discord on a given-up night, silence on a good one, and it is not an alert.** The
`NotificationChannel` protocol and the Discord webhook exist from the infrastructure cycle and
have had no consumer. An operational message touches no crossing, no cooldown and no
`emits_alerts` flag, so it neither pre-empts the alerting cycle nor breaks the scoring cycle's
claim that its runs stay silent.

The message carries the date, the step that failed, the counts, and the thing that is actually
urgent: **that snapshot cannot be backfilled.** Scoring D2 is forward-only, so a given-up night
is a permanent hole in the forward log rather than something tomorrow repairs. A message that
said only "ingest failed" would understate what was lost.

Nothing is sent on success. A nightly "all good" trains its reader to ignore the channel, which
costs exactly the message that matters.

**D9 — No market calendar.** Inherited from scoring D12: a weekend run produces a snapshot
identical to Friday's, which is honest, because the score genuinely did not move. Introducing a
trading calendar to suppress those rows would add a dependency and a source of drift to save a
few thousand rows a year — and the scheduler is the wrong place to hold one.

**D10 — One trigger. No midday pass.** Per F2, a daytime price fetch's only new content is a bar
the schema cannot mark as partial and three read-only roles can read as a close. Genuine intraday
freshness is a different feature — a quote stored somewhere that says it is a quote — and it
belongs in its own cycle rather than being smuggled in through the scheduler. It also has no
consumer today: the dashboard draws invented data until the ratios and swap cycles land.

This is not designed out. `NIGHTLY_TRIGGER_HOUR` becoming a list, with scoring on the last pass
before midnight, is the shape if it is ever wanted.

---

## 4. Conventions this follows

- Each package has a small public surface through `__init__.py`; nothing outside imports a
  submodule directly.
- Pure computation separated from database work: the trigger arithmetic and the gate take plain
  values and return plain values.
- All SQL identifiers lowercase; `timestamptz` never `timestamp`.
- Comments explain *why*, not *what*.

---

## 5. Layout

```
screener.nightly
  __init__.py   the public surface
  __main__.py   PID 1: signals, the wait, the catch-up check
  config.py     NightlyConfig.from_env -- trigger hour, retries, the off switch
  night.py      one night: the three steps in order, and the gate
```

`night.py` opens no socket of its own beyond the clients the ingest halves already own, and
`__main__.py` is the only module that reads a clock it did not receive.

A compose service in `deploy/compose.prod.yaml`, on `api` rather than `postgres` — the same
`depends_on` reddit uses, and for the same reason: a healthy database is not a migrated one, and
this needs `metric`, `security` and the seeded reference rows to exist.

---

## 6. A night

```
23:00 UTC  (or on boot, if today is unscored and 23:00 has passed)
   |
   +- active_securities(conn)
   +- run_prices        ~4.7 min   -> IngestReport
   +- run_fundamentals  ~4.2 min   -> FundamentalsReport
   +- gate: prices wholly failed?  -> skip scoring, the night failed
   +- run_scoring       -> ScoringReport
   |
   +- ok      -> log, wait for tomorrow, say nothing
   +- failed  -> back off, retry (up to 3), then Discord and wait for tomorrow
```

---

## 7. Failure behaviour

| failure | behaviour |
|---|---|
| some securities fail | the night succeeds (D7); counted and logged, nothing sent |
| prices wholly failed | scoring is skipped (D5); the night failed, retried, then reported |
| fundamentals failed | logged; scoring still runs, because it reads no fact this cycle |
| `BlobWriteFailed` | the night failed — an observation must never name an object that was not written |
| `ScoringInProgress` | step aside without retrying (D6) |
| `NoBarsVisible` | the night failed, without consuming three attempts |
| the container is killed mid-night | the next boot's catch-up check finishes the night (D3); a run left `running` is settled by `reconcile` |
| three attempts exhausted | Discord, then wait for tomorrow. That date's snapshot is gone for good |

---

## 8. Testing

The trigger arithmetic and the gate are pure; the catch-up check needs Postgres 16; the loop
takes an injected clock and event, as `TimeseriesClient` takes an injected `now`.

- **Prices wholly failed and scoring is never called** — the gate, and the reason it exists.
- **A partial ingest still scores.** The `CWEN-A` case, which is every real night.
- **A fundamentals failure does not gate scoring**, this cycle.
- **A night that succeeds sends nothing**, and **a night given up sends one message, not three.**
- **Booting before the trigger hour waits rather than running the night early** — the catch-up
  check must not fire merely because today is unscored at 10:00.
- Booting after the trigger hour with today unscored runs immediately; booting with today already
  scored waits for tomorrow.
- `ScoringInProgress` steps aside without consuming an attempt.
- Trigger arithmetic across 22:59, 23:01 and 00:01, with no drift after a long pass.
- SIGTERM during the wait returns promptly rather than at the end of the sleep.

---

## 9. Out of scope

- **Every command's behaviour.** Nothing in `screener.ingest` or `screener.scoring` changes.
- **Intraday freshness**, per D10. A quote that says it is a quote is its own cycle.
- **Alerting.** No crossing is computed, no `alert_rule` is read, and `emits_alerts` stays false
  on every run this schedules.
- **Backfilling a missed night.** Scoring D2 forbids it; `status = 'backfill'` remains a run type
  to add later.
- **The quarterly `universe refresh`.** It goes out to Wikipedia and SEC, writes a CSV that is
  meant to be reviewed in a diff before it moves a score, and so is deliberately a human action.
- **A market calendar**, per D9.

---

## 10. Open parameters

- **23:00 UTC** is chosen for two to three hours of settling after the US close. If Yahoo turns
  out to revise the same-day bar later than that, the settling window absorbs it — but the hour
  is the cheapest thing to move.
- **Three attempts, five and fifteen minutes.** Untested against a real outage; the shape matters
  more than the numbers.
- **Whether a *silent* night should eventually be reported.** Today nothing is sent on success,
  so a container that never starts is indistinguishable from one that succeeds. `/status`
  surfacing the age of the last successful night is the cheap answer, and belongs with whatever
  cycle next touches that endpoint.

---

## 11. Errata this cycle owes another document

- **The schema spec** describes `cutoff_offset` as sized for "a live run scoring D at 02:00 the
  next morning". Per F1 that run is one `screener.scoring.cli` refuses, because at 02:00 on D+1
  the date D is in the past. The offset's value is unaffected and its reasoning stands; what is
  wrong is the worked example of when the run happens.
