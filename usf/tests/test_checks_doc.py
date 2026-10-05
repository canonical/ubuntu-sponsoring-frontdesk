"""#137: doc/CHECKS.md is the reviewed wording, so it must match the code.

Two guards, both working on source text rather than on the checks
themselves: a check builds its message from live data, so there is no
template to introspect without reshaping every check first.
"""

import pathlib
import re

# usf/tests/ -> usf/ (the modules) -> the repo root (doc/, shared with the
# charm since the #144 move).
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOC = _ROOT.parent / "doc" / "CHECKS.md"

# Checks that produce neither a finding nor a comment: they only gate the
# pass, and doc/CHECKS.md explains them in prose ("Why the bot sometimes
# says nothing") rather than as rows in the table.
_GATES = {
    "check_human_engaged",
    "check_sponsoring_team_subscribed",
}


def _normalize(text):
    """Collapse whitespace so reflowing a source string doesn't fail the test."""
    return re.sub(r"\s+", " ", text).strip()


def _source_text():
    """Both modules' source, whitespace-collapsed.

    String literals are concatenated across lines in the source (implicit
    concatenation plus f-string prefixes), so the quotes, prefixes and the
    newlines between them are stripped: what's left is comparable with the
    prose in the doc.
    """
    raw = "\n".join((_ROOT / name).read_text() for name in ("checks.py", "llm_reviewer.py"))
    # Join implicitly-concatenated literals: `"a "\n    "b"` -> `a b`.
    joined = re.sub(r'"\s*\n\s*f?"', "", raw)
    joined = joined.replace('f"', '"')
    # Escaped newlines inside a literal are line breaks in the posted
    # comment, so they read as whitespace for comparison purposes.
    joined = joined.replace("\\n", " ")
    return _normalize(joined)


def test_every_finding_check_is_documented():
    names = re.findall(r"^def (check_\w+)", (_ROOT / "checks.py").read_text(), re.M)
    assert names, "no check functions found -- did checks.py move?"
    doc = _DOC.read_text()
    undocumented = [n for n in names if n not in _GATES and n not in doc]
    assert not undocumented, (
        "these checks aren't in doc/CHECKS.md: "
        + ", ".join(undocumented)
        + " -- document the check (and its wording) in the same commit"
    )


def test_quoted_wording_matches_the_code():
    source = _source_text()
    # Blockquote paragraphs: consecutive "> " lines form one quote.
    quotes = []
    current = []
    for line in _DOC.read_text().splitlines():
        if line.startswith(">"):
            current.append(line.lstrip(">").strip())
        elif current:
            quotes.append(" ".join(current))
            current = []
    if current:
        quotes.append(" ".join(current))
    assert quotes, "no quoted wording found in doc/CHECKS.md"

    missing = []
    for quote in quotes:
        if quote.strip() in {"...", ""}:
            continue
        # A quote may be an excerpt and carries {placeholders} filled in at
        # runtime, so match it as a regex with those spans as wildcards.
        # Leading/trailing ones add nothing to a search, and an unbounded
        # leading one made a mismatch quadratic over the whole source --
        # the test hung instead of failing (#149). A placeholder stands for
        # one expression, so its wildcard is bounded too.
        parts = [p for p in re.split(r"(\{[^}]*\})", _normalize(quote)) if p]
        while parts and parts[0].startswith("{"):
            parts.pop(0)
        while parts and parts[-1].startswith("{"):
            parts.pop()
        pattern = "".join(
            ".{0,300}?" if part.startswith("{") else re.escape(part) for part in parts
        )
        if not re.search(pattern, source):
            missing.append(quote[:70] + ("..." if len(quote) > 70 else ""))
    assert not missing, (
        "doc/CHECKS.md quotes wording that isn't in checks.py or "
        "llm_reviewer.py:\n  " + "\n  ".join(missing)
    )
