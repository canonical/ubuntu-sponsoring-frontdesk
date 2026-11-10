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


def parse_reply(text, validate):
    """Parsed(data, summary) or Unparseable(reason), never an exception.
    `validate(data)` returns (cleaned, summary) or raises ValueError."""
    try:
        body = last_block(text)
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise ValueError(f"malformed YAML ({_first_line(exc)})") from None
        if not isinstance(data, dict):
            raise ValueError("block is not a mapping")
        cleaned, summary = validate(data)
    except ValueError as exc:
        return Unparseable(str(exc))
    return Parsed(cleaned, summary)


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


# --- per-check validators -----------------------------------------------------


def verdict(data):
    """SRU template and sync reviews: `verdict: pass|fail`, and a `fail`
    needs a reason (the reason is what the contributor is told)."""
    value = _choice(data, "verdict", ("pass", "fail"))
    reason = str(data.get("reason") or "").strip()
    if value == "fail" and not reason:
        raise ValueError("fail without a reason")
    return {"verdict": value, "reason": reason}, value


def fixed_in_newer_series(data):
    """Check 7's escape hatch: `verdict: fixed|not-stated`."""
    value = _choice(data, "verdict", ("fixed", "not-stated"))
    return {"fixed": value == "fixed"}, value


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
