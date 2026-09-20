"""The diagrams in `docs/rupert.md` and the ones the dashboard draws.

Two copies of the same mermaid, in two places that serve two readers: the doc
renders on GitHub, `/rupert/how` renders in the dashboard beside the page it
explains. Neither can be dropped and neither is generated at runtime, so the
only thing standing between them and quietly disagreeing is this file.

Shaped after the grant tests in `tests/test_playground.py`: parse both sources
and assert they say the same thing, rather than hand-maintaining a copy of one
of them here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "rupert.md"
PAGE = ROOT / "web" / "lib" / "how-rupert.ts"


def doc_diagrams() -> list[str]:
    """Every mermaid block in the doc, in order."""
    return [
        block.strip()
        for block in re.findall(r"```mermaid\n(.*?)\n```", DOC.read_text(), re.S)
    ]


def page_diagrams() -> list[str]:
    """Every mermaid block the page holds, in order.

    The TS file stores each as a JSON string literal, so `json.loads` on the
    quoted span is the parser — deliberately not a regex over the escapes, which
    is how `\\n` ends up compared against a real newline and the test passes
    while the diagrams differ.
    """
    import json

    found: list[str] = []
    for line in PAGE.read_text().split("\n"):
        stripped = line.strip()
        if not stripped.startswith("diagram: "):
            continue
        value = stripped[len("diagram: ") :].rstrip(",")
        # Only a quoted string is data. The type declaration in the block above
        # is `diagram: string | null;`, which matches the prefix and is not one.
        if not value.startswith('"'):
            continue
        found.append(str(json.loads(value)).strip())
    return found


def test_the_page_draws_every_diagram_the_doc_has():
    # Count first, so "somebody added a diagram to one of them" fails with a
    # number rather than with a wall of flowchart source.
    assert len(page_diagrams()) == len(doc_diagrams())


def test_every_diagram_is_byte_for_byte_the_same_in_both():
    for n, (doc, page) in enumerate(zip(doc_diagrams(), page_diagrams(), strict=True)):
        assert doc == page, (
            f"diagram {n + 1} differs between docs/rupert.md and "
            f"web/lib/how-rupert.ts — change the doc and regenerate the page"
        )


def test_the_doc_still_has_the_diagrams_this_is_guarding():
    # So a doc rewritten without any mermaid cannot make both tests above pass
    # by comparing two empty lists.
    assert len(doc_diagrams()) >= 5


def test_the_page_explains_the_thing_it_is_linked_from():
    # The help link on /rupert points here, so the section that describes the
    # decision path has to survive an edit to either file.
    body = PAGE.read_text()
    assert "The decision path" in body
    assert "flowchart" in body


# -- the walks -------------------------------------------------------------

WALKS = ROOT / "web" / "lib" / "rupert-walk.ts"
VERSION_TS = ROOT / "web" / "lib" / "rupert-version.ts"
VERSION_PY = ROOT / "src" / "screener" / "rupert" / "version.py"


def walk_node_ids() -> set[str]:
    """Every mermaid node id the worked examples step through."""
    body = WALKS.read_text()
    paths = re.findall(r"path:\s*\[(.*?)\]", body, re.S)
    return {
        name.strip().strip('"')
        for path in paths
        for name in path.split(",")
        if name.strip()
    }


def decision_node_ids() -> set[str]:
    """Every node id declared in the decision flowchart.

    The character class matches a **whole** mermaid id, uppercase included. It
    was `[a-z][a-z0-9_]*` first, which silently truncated `unsureRENAMED` to
    `unsure` — so a renamed node still appeared to exist and the test below
    passed while the animation highlighted nothing. Caught by deliberately
    renaming a node and finding the test green.
    """
    diagram = doc_diagrams()[0]
    ident = r"[A-Za-z][A-Za-z0-9_]*"
    # `id["label"]` and `id{"label"}` declare a node...
    declared = set(re.findall(rf"^\s*({ident})\s*[\[{{(]", diagram, re.M))
    # ...and so does naming one as an arrow's target, with or without a label.
    declared |= set(re.findall(rf"-->\s*(?:\|[^|]*\|\s*)?({ident})", diagram))
    return declared


def test_every_step_of_every_walk_is_a_node_that_exists():
    # The walks highlight by id. A renamed node does not raise — it highlights
    # nothing, and the animation silently stops explaining anything. This is the
    # only thing that would notice.
    missing = walk_node_ids() - decision_node_ids()
    assert not missing, f"walk steps that are not nodes in the diagram: {sorted(missing)}"


def test_the_walks_cover_every_outcome_the_diagram_has():
    # Five ways for a text to end up, and a chip for each: the point of the
    # walks is that "about nothing" is the common answer, which you only see if
    # the examples are not all happy paths.
    ends = set(re.findall(r'ends:\s*"([a-z]+)"', WALKS.read_text()))
    assert ends == {"resolved", "none", "unsure", "crowded", "nothing"}


def test_the_page_and_the_code_agree_on_which_rupert_this_is():
    # The page states a version so a reader can tell whether the diagram is the
    # one running. Two sources of that string would drift the first time one was
    # bumped.
    in_code = re.search(r'^VERSION\s*=\s*"([^"]+)"', VERSION_PY.read_text(), re.M)
    on_page = re.search(r'version:\s*"([^"]+)"', VERSION_TS.read_text())
    assert in_code and on_page
    assert in_code.group(1) == on_page.group(1)


def test_the_running_version_has_a_changelog_entry():
    body = VERSION_PY.read_text()
    found = re.search(r'^VERSION\s*=\s*"([^"]+)"', body, re.M)
    assert found
    version = found.group(1)
    # A version nobody wrote down is one nobody can explain, and the dashboard
    # shows this text.
    assert f'"{version}",' in body or f'(\n        "{version}",' in body
