# Skybird: live stream capture

*Status: implemented. Written 2026-09-05.*

Paste a YouTube or Twitch live stream URL and its audio is pulled continuously,
transcribed in near-real time and stored with the second each phrase was said
at. Watch the stream on the dashboard while it runs. Stop it when you like;
delete it when you like.

## The problem

`screener.transcribe` transcribes a *clip*: at most 4 MB of audio, one POST, one
string back. A Discord voice note and the dashboard's mic button are both that
shape — somebody speaks for twenty seconds and stops.

A live stream is the other shape. It has no end, no content length and no single
buffer, and the thing worth having from it is not one string but a timeline. The
transcription service cannot be extended into that: its wire protocol requires
`Content-Length` and rejects chunked encoding on purpose, and its decoder does
`io.BytesIO(audio)` on a fully materialised buffer.

The second problem is that this is not a screener feature. Nothing about it is a
fact or a score, nothing joins to it, and its lifetime is its own. It needed a
place to live that was not `public` and not the ingest pipeline.

## Decisions

**D1 — The database is the control plane.** The status service writes a row in
`skybird.stream_session` with `state = 'requested'`; the supervisor, in its own
container, polls for it every two seconds.

The alternative was an HTTP endpoint on the capture container, which is what the
transcriber and the chart renderer have. It was rejected on three counts, of
which only the third is decisive. It would need a second internal API to define;
it would need authentication of its own, or the honest admission that it has
none; and it would put the state of a running capture inside a process that can
die holding it. With the row as the record, a capture left `running` by a
container that was killed is still visible on the next boot, and `reconcile`
settles it to `failed` rather than leaving the dashboard reporting a capture
that is not happening.

The cost is latency — up to one poll on a start or a stop, which is nothing
against a thing measured in hours — and a query every two seconds, which is one
indexed read of a partial index over four live rows.

**D2 — yt-dlp is the platform layer for capture; the adapters are identity and
embedding only.** A module in `skybird/platforms` does two things: recognise a
URL, and build a player URL. It never opens a socket.

This is the decision that makes "multi-platform" cheap rather than aspirational.
Writing YouTube and Twitch stream extraction by hand means player-response
parsing, a GQL access token, and a repair every time either platform changes
something — twice, and then a third time for the next site. yt-dlp already does
that for forty of them, so adding Kick or Rumble is one module, one entry in
`PLATFORMS`, and one row in `skybird.platform`. There is no capture code to
write at all.

`DESIGN.md`'s standing constraint — take the better tool for a specific job —
is what admits the dependency. It is pure Python with nothing compiled under it,
and it carries **no upper bound**, unlike `discord.py` and `faster-whisper`: we
sit on `extract_info` and a dict rather than a wide API surface, and being
*stale* is the failure mode that actually bites, because a pin would hold the
version that stopped working the week a platform changed something.

**D3 — ffmpeg comes from apt, in that image and no other.** This contradicts the
note in `pyproject.toml` that PyAV's wheel makes a system ffmpeg unnecessary,
and both claims are true. PyAV decodes one finished buffer, which is what the
transcriber does. This cuts a live HLS stream into fixed-length files while
reconnecting through the gaps, which is the binary's own segment muxer and has
no wheel.

The command is worth reading once:

```
ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -i <manifest>
       -vn -ac 1 -ar 16000 -c:a pcm_s16le
       -f segment -segment_time 15 -reset_timestamps 1
       -segment_list pipe:1 -segment_list_type flat  <tmpfs>/chunk%06d.wav
```

16 kHz mono s16le is exactly what Whisper wants, so nothing downstream
resamples, and a 15-second chunk is 480 KB against a 4 MB cap.
`-segment_list pipe:1` is the load-bearing flag: ffmpeg names each segment on
stdout the moment it closes it, so nothing has to guess from a modification time
whether a file is still being written to.

**D4 — Chunks are 15 seconds, and the transcript runs about twenty seconds
behind live.** Longer chunks decode more accurately and cost less; shorter ones
read as live. Fifteen was chosen for the second reason, with the first accepted:
`condition_on_previous_text=False` in the transcriber means no context crosses a
boundary, so the occasional word is clipped. `vad_filter=True` already tends to
cut at silence, which mitigates it, and per-utterance timings make the seam
visible rather than hidden.

Carrying the previous chunk's tail as an `initial_prompt` would fix it properly.
It is not in this change because it needs a request-shape change to a service
whose design note is "raw bytes, `Content-Length`, no chunked" — worth doing,
not worth doing first.

**D5 — The transcriber now returns per-utterance timings, and this is the only
change to code that already worked.** faster-whisper produces `segment.start`
and `segment.end`; `server.py` was joining them into one string and throwing the
boundaries away.

The response gains a `segments` array beside the existing `text`, and
`Transcript` gains a `segments` tuple that defaults to empty. Every existing
caller reads `.text` and is untouched. The seam on `build_server` still accepts
the plain `(text, seconds)` pair, so a test fake stays two lines of Python and
only the real model has to carry timings.

Without this a transcript is one row per fifteen seconds, which is a bucket
rather than a timestamp. With it, a mention keeps the second it was said at,
which is the difference between a stored transcript and a useful one.

**D6 — Audio is never written to a disk, and never kept.** Chunks land in a
tmpfs, are POSTed once, and are unlinked. `screener.transcribe` holds itself to
the same rule and this does not weaken it.

It is also the answer to the storage question that `DESIGN.md` leaves open for
payloads. A night of audio is gigabytes; a night of text is megabytes; only the
text is worth anything, and keeping the audio would mean a retention policy, a
volume and a bill for something nothing reads.

The backlog is bounded for the same reason. At most `MAX_PENDING_CHUNKS` may
queue; past that the oldest is dropped and counted on the session. Growing
without limit would fill memory to hide a transcriber that is behind, and the
gap is visible in the offsets anyway.

**D7 — Two streams at once, because the transcriber is one.**
`screener.transcribe` holds a `BoundedSemaphore(1)` on a two-core container, and
a 15-second chunk of `base.en` int8 is a couple of seconds of it. Two streams is
somewhere around a fifth to two-fifths of its time; a third would spend more of
its life queued than decoding, and would push the dashboard's mic button toward
its thirty-second busy wait before a `503`.

**That figure is an estimate, not a measurement**, and should not be quoted as
though it were. Measuring a chunk on the VPS is the first thing to do after this
lands, and `SKYBIRD_MAX_SESSIONS` should be set from the result. If it is worse
than estimated the escape hatch costs no code: `TRANSCRIBER_URL` is already an
environment variable, so pointing skybird at a second transcribe container is
configuration.

A shared transcriber rather than one of its own, for the reason the chart
renderer is shared: one transcriber cannot drift from itself.

**D8 — Watching goes through the platform's own player.** An `<iframe>` of a
YouTube or Twitch embed, never a restream through the box. It costs no
bandwidth, needs no code, and is the sanctioned way to watch.

The embed URL is built **in Python and returned by the API**, not assembled in
the browser. Twitch refuses to play unless `parent` names the host serving the
page, which is configuration — and the `web` container deliberately holds none,
its only environment variable being `NODE_ENV`. A wrong `parent` is a black
frame rather than an error, which is exactly the sort of failure that should not
depend on a value the front end guessed.

`StreamRef.embed_url` is nullable because a YouTube handle URL names no
broadcast until YouTube is asked which one is live. The supervisor fills it in
after the probe, through `Platform.embed_video`.

**D9 — Its own `skybird` schema**, on the three grounds `010_auth.sql` and
`011_audit.sql` state: nothing here is a fact or a score, no scoring query joins
to it, and it keeps the test suite's `drop schema public cascade` meaning what
it says.

Two departures from the schema conventions, both deliberate. `platform` is a
**table rather than `text` + `check`**, because the whole point of the list is
that it grows and a check constraint would make each addition a migration;
`state` is a check constraint, because that *is* a closed set this code owns.
And `transcript_segment` is **not partitioned**, unlike every other table that
grows daily: retention here is "delete the session", which cascades, and the
only read that matters is one session in sequence order — which partitioning by
time would scatter across partitions rather than keep adjacent.

**D10 — The three mutations are POSTs, and the rest of the file prefers GET.**
`_ask` explains why the status service avoids bodies: a body means
`Content-Length`, a read, and keep-alive bookkeeping, which is not worth it for
a short string. `_handoff` follows that even though it sends a Discord message.

Skybird's `start`, `stop` and `delete` do not, and the exception is narrow
enough to name: a GET that deletes a transcript is one prefetch or one followed
link away from deleting it by accident. `do_POST` and `_read_body` already
exist, so the cost is a branch.

**D11 — A session is required on every route, unconditionally, and there is
still no decorator.** `_require_login` collapses the repeated error bodies, and
the call site keeps the check visible — `login = self._require_login(config)` is
two lines that cannot be left out by accident, which is the property the
"no decorator" note is actually protecting. All five routes are also in the
parametrised list in `tests/test_auth.py`, which is what would catch a sixth
added without one.

**No budget check**, unlike `/api/transcribe`. Skybird spends nothing; the
resource it consumes is CPU on a capped container, and the session cap is what
bounds that. Refusing to capture a stream because someone had spent their $0.10
of model budget would be a cap enforcing the wrong thing.

**D12 — Start and delete are audited; stop is not.** `skybird.start` and
`skybird.delete` write an `audit.event`. Stop does not, because the session row
already carries `stopped_at` and `stop_reason`, and a second trail that could
disagree with the first is worse than one.

The delete row records a **count of segments, not their text** — the same rule
the transcription row follows, and for the same reason: the trail records that
something happened, not what was in it.

**D13 — Pause is a state, not a stop and a restart.** A held capture keeps its
row, its stream and its transcript. It holds its slot in the partial unique
index, so nobody can start a second capture of a stream you have paused; it
survives a supervisor restart untouched, because `reconcile` settles only the
states that imply a process and this is not one of them; and it stops counting
against the session cap, which is the whole point — pause is how you put
something else on without losing the first one.

That last part is why there are two state lists rather than one. `LIVE_STATES`
is what the unique index, the supervisor's poll and the interface's ordering all
key on, and `paused` is in it. `ACTIVE_STATES` is what the cap counts, and it is
not. Everywhere else the two are the same list, and collapsing them would mean
either a paused capture blocking a slot it is not using, or a paused stream
being captured twice.

**Resuming goes back to 'requested'**, not straight to 'running'. The manifest it
held has expired, the cap has to apply again, and a resumed capture wants
exactly the path a new one takes. It carries on rather than starting again
because both halves of its position are in the database: the sequence numbers,
which were always there, and `captured_seconds`, which was not.

That column is the change pause forced. The capture clock lived in the
supervisor's memory, which was fine while the only way to stop was to stop for
good. Every chunk moves it — including one that failed to transcribe and one
that was dropped, because the audio happened either way and an offset that
skipped it would put every line after the gap at the wrong second. It is written
in the same statement as the chunk counters, so it costs nothing.

**D14 — Steven can work the controls, and cannot read the transcript.** Three
tools: `watch` a link, `captures` to list, `hold` to pause, resume or stop by id.
There is deliberately no tool that returns transcript text. Reading a capture
back is a feature somebody will build; this one is about putting a stream on by
asking for it.

Two things fall out of that.

**A capture Steven starts records who asked**, not "steven" — `requested_by` is
shown in the dashboard beside the row, and a column that said the same thing for
every row would tell nobody anything. The model must not be the one to supply
the name, because a name in its arguments is a name it could invent, so
`tools.acting()` carries it in from the caller. It is the mirror of
`collecting()`, which carries a chart out, and it also lets the tool audit rows
say who they were for — which they previously could not.

**The limit reaches the model through a tool, not through the prompt.**
`captures` answers `used/limit` on every call, even with nothing running, and
`watch` names the limit in its refusal rather than only refusing. The prompt says
a limit exists and that `captures` reports it, and deliberately does not say what
it is: `SKYBIRD_MAX_SESSIONS` is configuration, and `SYSTEM_PROMPT` is built once
at import, before secrets are loaded — a number written into it would be the
default, frozen in, and wrong the first time anyone changed the setting.

The cost is real and is recorded in `tests/test_bot.py`: three tools and a line
of prompt are about 850 characters of fixed overhead on every message of every
conversation, which is the largest single rise that budget has taken. Raising it
was the decision; the test is where it is written down.

## What this deliberately does not do

- **Nothing reads the transcript.** No search, no ticker extraction, and no tool
  that hands one to Steven — he works the controls and that is all. The store
  and the API are shaped so a reading tool could be added beside the three
  control ones without touching either.
- **No speaker labels and no word-level timings.** WhisperX was rejected for
  needing torch and that has not changed.
- **English only.** The model is `base.en` and `language="en"` is hard-coded.
  Another language produces nonsense rather than an error.
- **No retention policy.** Transcripts stay until a person deletes one. That is
  the answer to "delete the data", and it means a capture left running all week
  is a decision somebody has to make rather than one a sweep makes for them.

## Terms of service

`DESIGN.md` says APIs before scrapers, and respect robots.txt and ToS. Pulling
audio out of a live stream is not something either platform's terms invite, even
for private use. Recorded here as a known position rather than left for somebody
to discover: the transcript is private and never redistributed, capture stops
when it is stopped, the audio is never retained, and *watching* deliberately
goes through the platform's own player, which is the sanctioned route and the
one that carries their advertising.

## Cost

No new paid service. Audio-only is roughly 50–60 MB an hour of bandwidth per
stream, and the CPU is the transcriber that already exists. The £5–10/month
target is unaffected. The image adds ffmpeg, which is the largest single thing
in it and lands in that image only.
