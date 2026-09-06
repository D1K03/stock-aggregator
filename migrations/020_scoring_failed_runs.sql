-- A failed run stops holding its date.
--
-- 004 gave `scoring_run` an exclusion constraint keying on `status`, so that
-- two live runs cannot both claim to be the score for one day. That rule is
-- right and stays. What it did not account for is how a run records dying:
-- through `outcome`, not `status`. So the row a failed night deliberately
-- leaves behind -- `status = 'live'`, the evidence that the night happened --
-- also blocked every later attempt to score that date, permanently, and the
-- only way out was an operator deleting the row by hand.
--
-- The fix is to say what was always meant: one live run per date among the
-- runs that are still standing. `outcome` already carries 'failed' as a legal
-- value, so nothing here invents a state -- 004's predicate was simply
-- narrower than the sentence it was written for.
--
-- `status` is not widened to hold 'failed' instead. The two columns answer
-- different questions -- `status` is what kind of run this is (live, backfill,
-- experiment), `outcome` is how it went -- and collapsing them would make
-- "was this a live run?" unanswerable for every run that ever failed.

alter table scoring_run
    drop constraint scoring_run_one_live_per_date;

alter table scoring_run
    add constraint scoring_run_one_live_per_date
        exclude using gist (as_of_range with &&)
        where (status = 'live' and outcome <> 'failed');
