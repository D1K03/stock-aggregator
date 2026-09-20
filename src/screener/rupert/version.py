"""Which Rupert decided a row.

**Not the model's version, and that is the point.** `rupert.mention.model`
already records which build of Jev answered; this records which *pipeline* asked
— the shortlist rules, the question set, the gates and the thresholds. The two
move independently: a provider can ship a new build under an alias we pinned
against, and we can rewrite every question without the model changing at all.

Stamped on every mention for the reason `scoring_run` carries a
`scoring_logic_version`: a decision made under a different set of questions is
not comparable with one made under these, and the only honest way to compare two
nights is to know whether the rules moved between them.

**Bump this when the decision changes, not when the code does.** A refactor that
produces identical decisions is not a version. What is:

- a question added, removed or reworded in `rupert.questions`
- a change to the confidence floor or the injection ceiling
- a change to what `rupert.candidates` will shortlist
- a change to which model is pinned in `rupert.decide`

`CHANGELOG` below is what the dashboard shows, so a version with no entry is a
version nobody can explain.
"""

VERSION = "v1"

# Newest first. Each entry is what a reader needs to know to tell whether two
# decisions are comparable — not a commit log.
CHANGELOG: list[tuple[str, str, str]] = [
    (
        "v1",
        "2026-09-20",
        "First Rupert. A regex shortlists cashtags and bare uppercase tokens "
        "against the universe and refuses to choose; Jev 1.13 answers one "
        "`choice` for the company and one for the claim kind, plus three `noul` "
        "gates; a choice below 0.9 confidence is kept as `unsure` rather than "
        "linked; FinBERT reads tone on what resolved and nothing else does.",
    ),
]


def described(version: str = VERSION) -> str:
    """What changed in one version, or a placeholder if nobody wrote it down."""
    for code, _when, note in CHANGELOG:
        if code == version:
            return note
    return "No changelog entry for this version."


def released(version: str = VERSION) -> str:
    """When a version was first used."""
    for code, when, _note in CHANGELOG:
        if code == version:
            return when
    return ""
