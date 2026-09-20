/* Which Rupert the diagrams describe.

   Mirrors `screener.rupert.version`, and `tests/test_rupert_docs.py` fails if
   the two disagree. It is duplicated rather than fetched because this page has
   to say which version it is describing even when the status service is down —
   a diagram with no version on it is one a reader has to take on trust. */

export const RUPERT_VERSION = {
  version: "v1",
  released: "2026-09-20",
  note:
    "First Rupert. A regex shortlists cashtags and bare uppercase tokens " +
    "against the universe and refuses to choose; Jev 1.13 answers one " +
    "`choice` for the company and one for the claim kind, plus three `noul` " +
    "gates; a choice below 0.9 confidence is kept as `unsure` rather than " +
    "linked; FinBERT reads tone on what resolved and nothing else does.",
};
