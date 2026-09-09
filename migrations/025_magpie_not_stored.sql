-- A page we fetched and then could not keep needs its own reason.
--
-- Everything in this list so far describes a fetch that did not happen: a site
-- refused us, no route worked, the paid rung was capped. A blob store that
-- rejects the write is the opposite case, and it was landing as no reason at
-- all, because the exception escaped before anything settled the row: the
-- attempt read 'running' indefinitely, which is what a scrape still in flight
-- looks like.
--
-- Worth telling apart from the fetch failures rather than folding in, because
-- it points somewhere else entirely. 'all_strategies_failed' means go and look
-- at the site; this means go and look at us.
alter table magpie.attempt drop constraint if exists attempt_reason_check;

alter table magpie.attempt
    add constraint attempt_reason_check
    check (reason in ('robots', 'paywall', 'not_a_page', 'too_short',
                      'all_strategies_failed', 'unlocker_capped', 'restarted',
                      'not_stored'));
