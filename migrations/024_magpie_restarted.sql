-- An attempt the scraper never got to try needs its own reason.
--
-- `reconcile` settles rows left behind by a process that died mid-fetch, and it
-- was writing `all_strategies_failed` for them. The interface renders that as
-- "every route to it failed", which is not true and is misleading exactly when
-- somebody is working out what went wrong: a deploy replaced the container, no
-- route was tried, and the row said the web had refused us.
--
-- Existing rows are left alone. They are history, and rewriting them would
-- claim to know which of them were restarts rather than genuine failures.
alter table magpie.attempt drop constraint if exists attempt_reason_check;

alter table magpie.attempt
    add constraint attempt_reason_check
    check (reason in ('robots', 'paywall', 'not_a_page', 'too_short',
                      'all_strategies_failed', 'unlocker_capped', 'restarted'));
