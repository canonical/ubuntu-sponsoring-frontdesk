"""One parser for every LLM reply (design_journal.md #168).

Each review prompt ends "End your reply with a fenced yaml block, and write
nothing after it". Five review methods used to each re-implement the same
steps: regex for the *first* fenced block, yaml.safe_load, then ad-hoc field
checks. Two problems with that:

- the first block can be one the model quoted from the untrusted bug or
  diff text, so a contributor's own ```yaml block could be read as the
  verdict;
- a reply that couldn't be read looked exactly like a real verdict, only
  logged.

`parse_reply` takes the LAST fenced block, requires nothing but whitespace
after it, and validates it with a per-check function. It returns Parsed or
Unparseable; each caller maps Unparseable to its existing fail-safe default,
and the reason ends up in the audit trail.

Validators take the loaded YAML and return the cleaned data, or raise
ValueError with a short reason. They are deliberately no stricter than the
code they replace about anything that decides an outcome: a reply each old
parser accepted gives the same result here. What's new is that a reply the
old code quietly defaulted on is now *named* unparseable.
"""

import collections
import logging
import re

import yaml

logger = logging.getLogger(__name__)

Parsed = collections.namedtuple("Parsed", "data summary")
Unparseable = collections.namedtuple("Unparseable", "reason")

# An opening fence on its own line: ``` optionally tagged yaml/yml/json.
_OPEN_FENCE_RE = re.compile(r"^\s*```\s*(?:yaml|yml|json)?\s*$", re.IGNORECASE)
_CLOSE_FENCE_RE = re.compile(r"^\s*```\s*$")


def last_block(text):
    """The body of the reply's final fenced block. Raises ValueError when
    the reply doesn't end with one."""
    lines = (text or "").rstrip().splitlines()
    if not lines or not _CLOSE_FENCE_RE.match(lines[-1]):
        if any(_CLOSE_FENCE_RE.match(line) for line in lines):
            raise ValueError("text after the last fenced block")
        raise ValueError("no fenced block")
    for index in range(len(lines) - 2, -1, -1):
        if _OPEN_FENCE_RE.match(lines[index]):
            return "\n".join(lines[index + 1 : -1])
    raise ValueError("no opening fence for the final block")


def parse_reply(text, validate, fallback=None):
    """Parsed(data, summary) or Unparseable(reason), never an exception.
    `validate(data)` returns (cleaned, summary) or raises ValueError.

    `fallback(body)`, when given, is tried only if the block isn't valid
    YAML: it returns a mapping read some stricter, narrower way, or raises
    ValueError -- in which case the YAML error is the one reported. A reply
    read that way has "+lines" on its summary, so the audit trail shows how
    often it's needed (#171). Defaults to `validate.fallback` if the
    validator names one."""
    if fallback is None:
        fallback = getattr(validate, "fallback", None)
    try:
        body = last_block(text)
        via = ""
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            error = f"malformed YAML ({_first_line(exc)})"
            if fallback is None:
                raise ValueError(error) from None
            try:
                data = fallback(body)
            except ValueError:
                raise ValueError(error) from None
            via = "+lines"
        if not isinstance(data, dict):
            raise ValueError("block is not a mapping")
        cleaned, summary = validate(data)
    except ValueError as exc:
        return Unparseable(str(exc))
    return Parsed(cleaned, summary + via)


def _first_line(exc):
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


def _choice(data, field, allowed):
    value = data.get(field)
    if value is None:
        raise ValueError(f"no {field}")
    value = str(value).strip().lower()
    if value not in allowed:
        raise ValueError(f"unknown {field} {value!r}")
    return value


_VERDICT_LINE_RE = re.compile(r"^verdict:[ \t]*(?P<value>pass|fail)[ \t]*(?:#.*)?$", re.IGNORECASE)
_REASON_LINE_RE = re.compile(r"^reason:[ \t]*(?P<value>.*?)[ \t]*$")
_PROBLEM_LINE_RE = re.compile(r"^problem:[ \t]*(?P<value>\w+)[ \t]*(?:#.*)?$", re.IGNORECASE)
# #185: the SRU template review's per-section points.
SRU_SECTIONS = (
    ("impact", "[Impact]"),
    ("test_plan", "[Test Plan]"),
    ("where_problems_could_occur", "[Where problems could occur]"),
)
_SECTION_LINE_RE = re.compile(
    r"^(?P<key>" + "|".join(k for k, _ in SRU_SECTIONS) + r"):[ \t]*(?P<value>.*?)[ \t]*$"
)


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def verdict_lines(body):
    """#171: the verdict block read as two plain lines, for when it isn't
    valid YAML. Seen live (git-diff-html bug #2173887): the SRU-template
    prompt asks the model to name sections like [Test Plan], so a reason
    starting with one -- `reason: [Test Plan] and ... are missing` -- is a
    YAML flow sequence that never closes. Since #9 that parse failure
    quietly became "pass", dropping a real template bounce.

    Deliberately narrow, nothing inferred: exactly one top-level line
    `verdict: pass|fail` (an optional trailing comment is fine), at most
    one `reason:` line whose value is the rest of that line, and no other
    content -- an indented continuation, another key, a multi-line value
    all raise ValueError. One pair of enclosing quotes is removed."""
    verdict = reason = problem = None
    sections = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        match = _VERDICT_LINE_RE.match(line)
        if match and verdict is None:
            verdict = match.group("value")
            continue
        match = _REASON_LINE_RE.match(line)
        if match and reason is None:
            reason = match.group("value")
            continue
        match = _PROBLEM_LINE_RE.match(line)
        if match and problem is None:
            problem = match.group("value").lower()
            continue
        match = _SECTION_LINE_RE.match(line)
        if match and match.group("key") not in sections:
            sections[match.group("key")] = _unquote(match.group("value"))
            continue
        raise ValueError(f"unexpected line {line.strip()[:40]!r}")
    if verdict is None:
        raise ValueError("no verdict line")
    result = {"verdict": verdict, "reason": _unquote(reason or ""), **sections}
    if problem is not None:
        result["problem"] = problem
    return result


# --- per-check validators -----------------------------------------------------


def verdict(data):
    """SRU template and sync reviews: `verdict: pass|fail`, and a `fail`
    needs a reason (the reason is what the contributor is told)."""
    value = _choice(data, "verdict", ("pass", "fail"))
    reason = str(data.get("reason") or "").strip()
    if value == "fail" and not reason:
        raise ValueError("fail without a reason")
    return {"verdict": value, "reason": reason}, value


# #171: the verdict reviews' reason is free text that may start with "[".
verdict.fallback = verdict_lines


def sru_template_verdict(data):
    """The SRU template review: `verdict: pass|fail`; on a fail, `problem:
    missing|thin` (#184: a section absent, or all present but some too
    thin -- anything else reads as `missing`, the long-standing wording)
    and one field per template section saying what's wrong with it, empty
    when it's fine (#185). The bot writes the section labels itself; a
    fail naming no section is unparseable (routed to a human)."""
    value = _choice(data, "verdict", ("pass", "fail"))
    points = []
    for key, label in SRU_SECTIONS:
        text = str(data.get(key) or "").strip()
        if text:
            points.append((label, " ".join(text.split())))
    if value == "fail" and not points:
        raise ValueError("fail naming no section")
    problem = str(data.get("problem") or "").strip().lower()
    result = {
        "verdict": value,
        "problem": "thin" if value == "fail" and problem == "thin" else "missing",
        "points": points if value == "fail" else [],
    }
    return result, value


sru_template_verdict.fallback = verdict_lines


def fixed_in_newer_series(series_names):
    """Check 7's escape hatch (#180): `fixed: [...]` and `declined: [...]`,
    each a subset of `series_names` (codenames). An entry may carry the
    prompt's "(Ubuntu YY.MM)" label; anything that isn't one of the series
    asked about is dropped. Both lists empty is a normal "not stated". A
    series in both counts as fixed."""
    asked = {name.lower() for name in series_names}

    def names(data, field):
        value = data.get(field)
        if value is None:
            return set()
        if not isinstance(value, list):
            raise ValueError(f"{field} is not a list")
        picked = set()
        for item in value:
            name = str(item).split("(")[0].strip().lower() if item is not None else ""
            if name in asked:
                picked.add(name)
            else:
                logger.warning("fixed_in_newer_series: ignoring %s entry %r.", field, item)
        return picked

    def validate(data):
        if "fixed" not in data and "declined" not in data:
            raise ValueError("no fixed/declined lists")
        fixed = names(data, "fixed")
        declined = names(data, "declined") - fixed
        summary = f"fixed:{len(fixed)},declined:{len(declined)}"
        return {"fixed": fixed, "declined": declined}, summary

    return validate


def bounce_response(data):
    """Rule B: `addressed: yes|no`, which YAML loads as a boolean. The old
    parser only accepted a real `True`; a quoted "yes" was "not addressed",
    and still is -- it's just named unparseable now."""
    value = data.get("addressed")
    if not isinstance(value, bool):
        raise ValueError(f"addressed is not yes/no ({value!r})")
    return {"addressed": value}, "addressed" if value else "not-addressed"


def findings_covered(count):
    """#106: `covered: [n, ...]`, finding numbers 1..count. An empty value
    is a normal "none". Entries that aren't one of the numbers are dropped,
    as before -- the rest of the answer is still usable."""

    def validate(data):
        value = data.get("covered")
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValueError("covered is not a list")
        covered = set()
        for n in value:
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= count:
                covered.add(n)
            else:
                logger.warning("findings_covered: ignoring entry %r (not 1..%d).", n, count)
        return {"covered": covered}, f"covered:{len(covered)}"

    return validate


def clean_bullet_list(raw, label):
    """A YAML list field as list[str], dropping anything that isn't a plain
    string.

    An unquoted bullet containing ": " followed by "#" (e.g. a bug
    reference like "LP: #123") gets misparsed by YAML as a mapping key with
    a trailing comment, silently truncating the rest of the text --
    yaml.safe_load returns a dict, not a string, for that item. str()-ing
    it would post garbage like "{'The stanza claims LP': None}", so drop it
    instead; the prompt tells the model to quote bullets to avoid this in
    the first place."""
    cleaned = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                cleaned.append(item.strip())
            elif item not in (None, ""):
                logger.warning(
                    "MP review: %s item wasn't a plain string (likely unquoted "
                    "': #' confused YAML); dropping it: %r",
                    label,
                    item,
                )
    elif isinstance(raw, str) and raw.strip():
        cleaned = [raw.strip()]
    return cleaned


_FEATURE = {"yes": True, "true": True, "no": False, "false": False}


def mp_review(data):
    """#47 MP review: `verdict: pass|fail`, optional `feature`, and on
    `fail` observations and/or mismatches. `feature` stays lenient (an
    unreadable value means "not classified", as before) so a stray value
    there can't throw away the bullets. A `fail` with no usable bullets is
    readable, just empty ("fail:0"), as before: the Feature Freeze
    classification beside it still counts."""
    value = _choice(data, "verdict", ("pass", "fail"))
    feature = _FEATURE.get(str(data.get("feature", "")).strip().lower())
    bullets = []
    if value == "fail":
        observations = clean_bullet_list(data.get("observations"), "observations")
        mismatches = clean_bullet_list(data.get("mismatches"), "mismatches")
        bullets = [("advisory", o) for o in observations] + [("verify", m) for m in mismatches]
        if not bullets:
            logger.warning(
                "MP review: 'fail' verdict without observations/mismatches; staying silent."
            )
    summary = f"fail:{len(bullets)}" if value == "fail" else "pass"
    return {"bullets": bullets, "feature": feature}, summary
