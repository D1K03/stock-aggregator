-- Pausing a capture, and the timeline that has to survive it.
--
-- 014 is already applied wherever skybird has run, and the runner never
-- re-applies a recorded migration, so this lands as a new file rather than an
-- edit to that one. 009 exists for the same reason.
--
-- A paused capture holds no process and is not costing the transcriber
-- anything, but it is still somebody's: it keeps its slot in the partial unique
-- index, so nobody can start a second capture of a stream you have paused, and
-- it survives a supervisor restart untouched -- `reconcile` only settles the
-- states that imply a process, and this is not one of them.
alter table skybird.stream_session
    drop constraint stream_session_state_check;
alter table skybird.stream_session
    add constraint stream_session_state_check
        check (state in ('requested', 'starting', 'running', 'paused',
                         'stopping', 'stopped', 'failed'));

-- Seconds of audio captured so far, across every reconnect and every pause.
--
-- This is what `offset_seconds` on the transcript counts, and until now it
-- lived only in the supervisor's memory -- which was fine while the only way to
-- stop was to stop for good. A pause has to come back and carry on counting, so
-- the number has to outlive the process. Written in the same statement that
-- moves the chunk counters, so it costs nothing extra.
--
-- Deliberately not "seconds since started_at": a paused stream keeps running
-- without us, and the audio we hold is what the offsets describe.
alter table skybird.stream_session
    add column captured_seconds numeric not null default 0
        check (captured_seconds >= 0);

-- The supervisor's poll and the one-live-capture-per-stream rule both key on
-- the live states, and 'paused' is now one of them. Neither index can be
-- altered in place, so both are replaced.
drop index skybird.stream_session_live_idx;
create index stream_session_live_idx on skybird.stream_session (state)
    where state in ('requested', 'starting', 'running', 'paused', 'stopping');

drop index skybird.stream_session_live_uq;
create unique index stream_session_live_uq
    on skybird.stream_session (platform, external_id)
    where state in ('requested', 'starting', 'running', 'paused', 'stopping');
