-- Rupert: which security a text is about, and what kind of thing it says.
--
-- The join `screener.reddit` and `screener.magpie` both stopped short of.
-- Migration 012 said it out loud -- "connecting an item to the tickers it
-- mentions is its own piece of work" -- and 022 said it again and gave the
-- reason a nullable `security_id` was the wrong answer: "a nullable column here
-- would invite a half-done join and quietly start being read as a fact".
--
-- This is that piece of work, and it is a table rather than a column for
-- exactly the reason 022 gave. A resolution is a decision somebody made with a
-- model, at a moment, with a confidence and a set of alternatives it was chosen
-- between. All of that is evidence and all of it is thrown away by a column.
--
-- **Measured on the live corpus, 2026-09-20, and the numbers decided the
-- design.** Over 22 days and 424,178 items:
--
--   * `$TICKER` appears in **0.6% of comments**. A cashtag-only resolver sees
--     essentially nothing, so bare uppercase tokens have to be candidates too.
--   * Bare-token matching against the 1,504-symbol universe reaches **5.6% of
--     items**, and roughly **a quarter of the top matches are ordinary
--     English**: over three days the top 25 included YOU (113), ON (104),
--     IT (82), ARE (63), ALL (47), AM (45), NOW (44) and PM (43), beside real
--     traffic in MU (346), SNDK (214), AMD (157) and NVDA (125).
--   * A blacklist is **lossy in both directions**: dropping ALL, IT and ON
--     discards Allstate, Gartner and ON Semiconductor for good. A word list
--     cannot tell "I put it ALL on calls" from "ALL reported a combined ratio
--     of 91", because the difference is the sentence around it.
--
-- So the deterministic half generates candidates and refuses to choose, and a
-- model that has the sentence in front of it does the choosing. What is stored
-- is the choice **and the distribution it came from**, on `screener.sentiment`'s
-- terms: three probabilities travel, never one number, because a reading always
-- has to carry its inputs.
--
-- Its own schema, on the grounds `auth`, `audit`, `skybird`, `magpie` and `mcp`
-- have one -- and unlike 027, which argued itself into `public` because an
-- insider transaction is a fact about a security. A mention is a fact about a
-- *text*, and the two tables here reference `social_item` and `magpie.document`
-- far more than they reference `security`.
--
-- Ingest only, on 027's terms and 012's before it. Nothing here becomes a
-- number: `rupert.reduce` is pure and unconsumed, and wiring it into the
-- Sentiment pillar needs a weight-version bump and a backfill with alerting
-- disabled, per DESIGN.md -- not a table.

-- `if not exists` on everything below, which the `public` tables in this project
-- do not need and a schema-owning migration always does. A failed migration is
-- re-run, and the only reset anything here has is `drop schema public cascade` --
-- which by construction does not reach a schema of our own, so the tables
-- outlive it and the re-run finds them still standing. 022 made the same call
-- for `magpie`, for the same reason.
create schema if not exists rupert;

insert into data_source (code, name)
values ('rupert', 'Rupert (text to security resolution)')
on conflict (code) do nothing;


-- One row per item that produced at least one candidate.
--
-- **Not one row per item.** 94.4% of the corpus mentions nothing that looks
-- like a ticker, and a row for each would be 7M a year saying "no". What tracks
-- those is `rupert.progress` below, for the same reason `screener.edgar` keeps
-- its frontier in `ingest_run` rather than deriving it from stored rows.
--
-- **Not one row per (item x security) either**, which is the tempting shape and
-- the wrong one. `choice` picks one option, and that is the honest primitive
-- here: a comment is *about* a company, and one comparing two is either about
-- the comparison -- which is not a claim about either -- or about the one it is
-- really arguing over. A row per candidate would turn one opinion into three
-- and let a list post outvote a thesis, which is the same failure 027 avoided
-- by refusing a row per reporting owner.
create table if not exists rupert.mention (
    id        bigint generated always as identity primary key,
    source_id smallint not null references data_source(id),

    -- Which corpus the text came from. Two nullable references and a check,
    -- rather than a `(kind, id)` pair: a polymorphic key cannot be a foreign
    -- key, and this table's whole purpose is to be a join nobody has to trust.
    -- `on delete cascade` because a mention of a deleted document is a claim
    -- about nothing.
    social_item_id bigint references social_item (id) on delete cascade,
    document_id    bigint references magpie.document (id) on delete cascade,
    check (num_nonnulls(social_item_id, document_id) = 1),

    -- Null unless `state = 'resolved'`. Every other state is a decision *not*
    -- to link, and each one is a different reason -- which is `magpie.attempt`'s
    -- shape, where a refusal is a state and a reason rather than an absent row.
    security_id bigint references security (id),
    state       text not null check (
        state in ('resolved', 'none', 'unsure', 'crowded', 'failed')
    ),

    -- What it was choosing between, as the deterministic half found them. Kept
    -- because the alternatives are what make the choice reviewable: "chose NVDA
    -- from {NVDA}" and "chose NVDA from {NVDA, AMD, INTC}" are different
    -- evidence, and only one of them is interesting.
    candidates text[] not null,
    chosen     text,

    -- The distribution, not just the winner. `confidence` is the model's own
    -- collapse of it -- (n x peak - 1) / (n - 1) -- and is stored beside the
    -- probabilities rather than instead of them, because an independent
    -- calibration test measured this model **overconfident on `choice` out of
    -- distribution** (refit temperature 3.29). Keeping the distribution is what
    -- lets a threshold be re-cut later without re-running a night's decisions.
    confidence    numeric check (confidence between 0 and 1),
    probabilities jsonb,

    -- Three yes/no probabilities from the same request, free by fan-out: all
    -- questions in one call are evaluated in parallel, so asking more of them
    -- costs tokens and not a round trip.
    --
    -- `own_business` separates a claim about the company from one about the
    -- market. `position_talk` separates "I bought calls" from "their guidance
    -- was cut" -- a trade announcement is sentiment about the poster, not about
    -- the company. `injection` is the guard: a Reddit corpus is an adversarial
    -- corpus, and the same model's published cookbook caught an injected post
    -- that similarity search had ranked *first*.
    --
    -- `noul` was measured **under**confident out of distribution (refit 0.66),
    -- which is why these are the gates and the `choice` above is not.
    own_business  numeric check (own_business between 0 and 1),
    position_talk numeric check (position_talk between 0 and 1),
    injection     numeric check (injection between 0 and 1),

    -- What kind of thing the text says. The narrative layer: this is what a
    -- flag is built from, and deliberately not what a score is built from.
    -- No check constraint on the vocabulary, on 027's grounds for
    -- `transaction_code` -- except inverted. There the vocabulary belongs to
    -- SEC; here it belongs to a prompt, and a prompt changes more often than a
    -- federal form. A migration to add a claim kind would be a migration to
    -- edit a question.
    claim_kind       text,
    claim_confidence numeric check (claim_confidence between 0 and 1),

    -- The meter, and the model that has to be able to disagree with itself
    -- across versions. `cost_usd` is the second per-request price in this
    -- project after `magpie.attempt.cost_usd`, and it is here for the same
    -- reason: the thing that spends money keeps its own count, because one
    -- counter shared with the assistant's cap would let a busy night silence
    -- Steven. Reported by OpenRouter, never computed from a local price list.
    model        text not null,
    input_tokens int not null default 0,
    cost_usd     numeric(10, 6) not null default 0,

    -- When we decided, which is what a point-in-time read filters on. Named to
    -- match `fundamental_fact.observed_at` rather than `decided_at`, because
    -- the scoring run's `cutoff_offset` is the thing that will read it and the
    -- two clocks have to mean the same thing.
    observed_at timestamptz not null default now(),

    unique (source_id, social_item_id),
    unique (source_id, document_id)
);

create index if not exists mention_security_idx
    on rupert.mention (security_id, observed_at desc)
    where state = 'resolved';
create index if not exists mention_state_idx on rupert.mention (state);
create index if not exists mention_observed_idx on rupert.mention (observed_at desc);
-- The meter's index: the daily spend is a sum over one day of one column.
create index if not exists mention_cost_idx on rupert.mention (observed_at desc)
    where cost_usd > 0;


-- How the text reads, from FinBERT and from nothing else.
--
-- A separate table from the decision above because they come from different
-- models with different failure modes, and the states between them are real: a
-- mention with no reading is one that resolved while the sentiment service was
-- down, and it should be visibly that rather than silently toneless. Same split
-- `screener.sentiment` already draws -- it scores text and does not know where
-- the text came from.
--
-- **Only resolved mentions get one.** Scoring the 94.4% that resolve to nothing
-- would be ~28 minutes of the box a night to produce numbers about no security.
create table if not exists rupert.reading (
    id         bigint generated always as identity primary key,
    mention_id bigint not null references rupert.mention (id) on delete cascade,

    -- Three probabilities, never one number. `positive - negative` is derived
    -- in `screener.sentiment`'s client and deliberately not stored: "confidently
    -- neutral" and "torn between positive and negative" both land near zero and
    -- are not the same reading, and a stored scalar would let the two disagree
    -- with the inputs it came from.
    positive numeric not null check (positive between 0 and 1),
    negative numeric not null check (negative between 0 and 1),
    neutral  numeric not null check (neutral between 0 and 1),

    -- The model is part of the key. A reading is only comparable with another
    -- from the same weights, and re-reading a corpus under a new checkpoint is
    -- a second row rather than an edit -- which is `fundamental_fact`'s rule
    -- that a fact is appended and never rewritten.
    model       text not null,
    observed_at timestamptz not null default now(),

    unique (mention_id, model)
);

create index if not exists reading_mention_idx on rupert.reading (mention_id);
create index if not exists reading_observed_idx on rupert.reading (observed_at desc);


-- How far through each corpus the resolver has read.
--
-- **`max(observed_at)` over `rupert.mention` cannot serve**, and this is the
-- same argument `screener.edgar.store.walked` makes about `max(filed_date)`.
-- 94.4% of items produce no mention row at all, so an hour in which nobody
-- named a ticker is indistinguishable from an hour nobody looked at. "Read it,
-- found nothing" and "never read it" must not be the same state.
--
-- Unlike edgar this is not `ingest_run`: edgar's unit of work is a named day
-- and `endpoint` holds the name, so the frontier is a set. Here the unit is a
-- position on a continuous timeline and the frontier is a scalar, which no
-- amount of `ingest_run` rows expresses without scanning them all.
--
-- One row per corpus. `social_item.created_utc` is the clock for Reddit and
-- `magpie.document.first_seen_at` for articles, so the column is named for what
-- it is rather than for either.
create table if not exists rupert.progress (
    corpus          text primary key check (corpus in ('social', 'document')),
    read_through    timestamptz not null,
    items_read      bigint not null default 0,
    updated_at      timestamptz not null default now()
);


-- On 013's terms: table by table, never `all tables in schema`, so that
-- exposure costs a line here and a line in tests/test_playground.py.
--
-- All three roles, including `playground_mcp`, which means these rows leave the
-- box to claude.ai -- said plainly here the way 018 said it about transcripts
-- and 022 about articles. What leaves is a ticker, a confidence and three
-- probabilities about a public Reddit comment that is already granted through
-- `social_item`; this table says which company it was about.
grant usage on schema rupert to playground, playground_bot, playground_mcp;
grant select on rupert.mention, rupert.reading, rupert.progress
    to playground, playground_bot, playground_mcp;
