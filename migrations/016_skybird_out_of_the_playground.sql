-- Take the skybird schema back off the read-only role.
--
-- `013_playground.sql` grants it, conditionally, and the comment there says why:
-- the schema was "created outside this repository", present on a development
-- machine and not on the VPS. That was true when it was written. It is not any
-- more — skybird is in this repository, in migrations 014 and 015, and the
-- grant was made without the one fact that decides the question.
--
-- Steven's `sql` tool runs as this role. He is deliberately built to start,
-- pause and stop captures and to be unable to read one back: `bot/tools/skybird`
-- has `watch`, `captures` and `hold` and nothing that returns a line of
-- transcript. A grant here hands him the whole thing through the side door,
-- which makes the missing tool decoration rather than a decision.
--
-- The same argument `audit.event` loses on, for that matter. A captured
-- transcript is somebody's words and `requested_by` is a GitHub login, and the
-- deny list in 013 already turns both of those down one schema over.
--
-- Written as a revoke rather than an edit to 013 because 013 is applied
-- wherever the playground has run, and the runner never re-applies a recorded
-- migration. It is also why this is not merely a tidy-up: whether that
-- conditional fired at all depends on whether skybird's schema existed when 013
-- ran, so a database restored from a dump and one migrated in place could
-- disagree. After this they cannot.
--
-- `revoke` on something that was never granted is not an error, so this is
-- correct on both.
revoke select on all tables in schema skybird from playground;
revoke usage on schema skybird from playground;

-- Tables 014 and 015 have not created yet cannot inherit a grant either. There
-- is no `alter default privileges` for this role by design — 013 says exposure
-- should cost a line in a migration and a line in a test — but the conditional
-- grant above ran `grant select on all tables`, which is a snapshot rather than
-- a rule, and a future skybird table would have missed it in any case. This
-- keeps the answer the same for the ones that exist and the ones that do not.
revoke all on schema skybird from playground;
