-- What the corpus was saying about one security, in English.
--
-- The first thing in this project to put an LLM's prose in front of a number,
-- and it is allowed for exactly the reason `screener.ai` exists: **narrative
-- extraction, never a score.** DESIGN.md draws that line twice and neither side
-- of it moves here. FinBERT still supplies every tone, `reduce` still does the
-- arithmetic, and this reads the same sentences a person would read on
-- `/rupert` and says what they were about.
--
-- The distinction is worth being precise about, because it is the one that
-- would rot first. A narrative may say "most of the traffic was about the
-- memory pricing cycle, with several posts citing a supply cut". It may not say
-- "sentiment is 0.37", may not rank, and may not advise -- the tone beside it on
-- the page is FinBERT's and the model is never shown it, so it cannot launder a
-- number it was handed back into prose that sounds like a finding.
--
-- **One row per security per day per model.** A narrative is a reading of a
-- corpus that changes daily, so regenerating it within a day would spend money
-- to paraphrase itself; `unique (security_id, as_of, model)` is what makes the
-- second click on the same day free. Keyed on the model too, on
-- `rupert.reading`'s terms: a summary written by a different model is a
-- different reading and an append rather than an edit.
--
-- `mentions_used` and `window_days` are stored because they are the difference
-- between a narrative worth trusting and one worth ignoring. Eleven comments
-- and four hundred produce the same confident paragraph, and only the count
-- says which you are looking at -- the same argument `reduce.Mood` makes for
-- keeping the item count beside the tone.

create table if not exists rupert.narrative (
    id          bigint generated always as identity primary key,
    security_id bigint not null references security (id) on delete cascade,

    -- The day the corpus was read for, not the moment the request was made.
    -- Two people clicking the same security an hour apart are asking the same
    -- question and should get the same answer without paying twice.
    as_of date not null,

    -- The prose. No length constraint: the prompt bounds it far below anything
    -- a column check could usefully catch, and a truncated narrative is worse
    -- than a long one.
    text text not null,

    -- How much was read to write it. See the header: this is what tells a
    -- narrative from three comments apart from one from three hundred, and both
    -- read equally fluently.
    mentions_used int not null check (mentions_used >= 0),
    window_days   int not null check (window_days > 0),

    -- The meter, matching `rupert.mention.cost_usd` and `magpie.attempt`'s
    -- before it. Reported by OpenRouter, never computed from a local price
    -- list, because a second price table here is wrong the first time a
    -- provider changes a rate and silently wrong afterwards.
    model      text not null,
    cost_usd   numeric(10, 6) not null default 0,
    created_at timestamptz not null default now(),

    unique (security_id, as_of, model)
);

create index if not exists narrative_security_idx
    on rupert.narrative (security_id, as_of desc);

-- On 013's terms, and the same sentence 028 wrote: exposure costs a line here
-- and a line in the two grant tests. What leaves the box is a paragraph about
-- public Reddit comments that `social_item` already sends.
grant select on rupert.narrative to playground, playground_bot, playground_mcp;
