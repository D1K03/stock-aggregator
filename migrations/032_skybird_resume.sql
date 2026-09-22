-- A capture survives the supervisor that was running it.
--
-- Until now a restart ended every live capture for good. The old supervisor's
-- shutdown and the new one's boot both settled 'starting' and 'running' to
-- `failed / supervisor_restart`, and every deploy recreates the skybird
-- container, so merging anything during a broadcast cost the rest of it. A
-- restart now puts the capture back in the queue, and the next supervisor
-- starts the stream as far back as the gap: YouTube keeps an hour of a live
-- broadcast in the playlist it hands us, and ffmpeg reads a backlog at about
-- ninety times real time before settling back at the live edge.
--
-- Existing rows are left alone, as 024 left magpie's. They are history, and
-- nothing restarted them.

-- The capture clock at the end of the last chunk accounted for: where the
-- stored audio stops, and so where a resumed capture has to pick up from.
--
-- `captured_seconds` is not enough on its own. It says how much audio there is,
-- not when the stream said it, and a rewind is measured against the wall clock:
-- how long ago the audio stopped is how far behind live to start.
--
-- Null when there is nothing to recover: a capture that has not heard anything
-- yet, or one somebody has resumed from a pause. A pause is a gap somebody
-- chose, and filling it in from the stream's own replay would put back exactly
-- what they asked not to have.
alter table skybird.stream_session
    add column captured_until timestamptz;

-- Restarts in a row with no chunk accounted for between them.
--
-- A resume re-fetches the audio it had not finished with, so a chunk that takes
-- the supervisor down with it would be fetched again and take it down again, for
-- ever, probing YouTube every few seconds as it went. A deploy never gets past
-- one of these before the next chunk lands and puts it back to zero; three is a
-- capture that is the problem, and it is failed rather than resumed.
alter table skybird.stream_session
    add column restarts smallint not null default 0 check (restarts >= 0);
