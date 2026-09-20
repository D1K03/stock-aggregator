-- Which Rupert decided a row.
--
-- `rupert.mention.model` already records which build of Jev answered. This
-- records which *pipeline* asked — the shortlist rules, the question set, the
-- gates and the thresholds — and the two move independently: a provider ships a
-- new build under an alias we pinned against, and we rewrite every question
-- without the model changing at all.
--
-- The same argument `scoring_run.logic_version_id` makes. A decision taken under
-- a different set of questions is not comparable with one taken under these, and
-- the only honest way to compare two nights is to know whether the rules moved
-- between them. Without this column that question is answerable only by reading
-- the git log and guessing at deploy times.
--
-- Defaulted to 'v1' rather than left null: every row already in the table was
-- decided by the first Rupert, which is a fact we know rather than one we have
-- to admit ignorance of. New rows get it from `screener.rupert.VERSION`, and the
-- default stays as a floor for anything that forgets.
alter table rupert.mention
    add column if not exists rupert_version text not null default 'v1';

create index if not exists mention_version_idx
    on rupert.mention (rupert_version, observed_at desc);
