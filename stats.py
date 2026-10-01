"""
Read-only retrospective over the audit trail (design_journal.md #131).

Answers the questions the audit log alone couldn't: what does the bot
actually catch, how often is it wrong, what does it cost. Reads
``audit.jsonl`` (see audit.default_path) and writes nothing.

Usage:
    python3 stats.py [--since 30d] [--json] [--audit PATH]
"""

import argparse
import collections
import datetime
import json
import sys

import audit


def _parse_since(value):
    """'30d' / '12h' / an ISO date -- the cutoff to report from."""
    if not value:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if value[-1] in "dh" and value[:-1].isdigit():
        amount = int(value[:-1])
        delta = (
            datetime.timedelta(days=amount)
            if value[-1] == "d"
            else datetime.timedelta(hours=amount)
        )
        return now - delta
    return datetime.datetime.fromisoformat(value).astimezone(datetime.timezone.utc)


def load(path, since=None):
    """Audit rows, oldest first, optionally only those at/after `since`.
    Malformed lines are skipped -- a half-written last line must not make
    the whole retrospective fail."""
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    ts = datetime.datetime.fromisoformat(row["ts"])
                except (KeyError, ValueError):
                    continue
                if ts < since:
                    continue
            rows.append(row)
    return rows


def summarize(rows):
    """Everything the report prints, as plain data (so --json is the same
    numbers, not a second implementation)."""
    items = [r for r in rows if r.get("action") == "triage"]
    writes = [r for r in rows if r.get("action") in ("comment", "set_status", "unsubscribe")]
    llm_calls = [r for r in rows if r.get("action") == "llm_call"]

    findings = collections.Counter()
    suppressed = collections.Counter()
    tiers = collections.Counter()
    clean = 0
    # #138: why a pass couldn't act. An inconclusive pass posts nothing and
    # persists nothing, so a check whose lookup keeps failing is invisible in
    # every other section -- it never produces a finding to count.
    inconclusive = collections.Counter()
    declined_checks = collections.Counter()
    declined_urls = {r["url"] for r in writes if r.get("outcome") == "declined"}

    for item in items:
        extra = item.get("extra") or {}
        fired = extra.get("findings") or []
        if not fired:
            clean += 1
        for finding in fired:
            findings[finding.get("check") or "?"] += 1
            tiers[_tier_label(finding)] += 1
            if item["url"] in declined_urls:
                declined_checks[finding.get("check") or "?"] += 1
        for finding in extra.get("suppressed") or []:
            suppressed[finding.get("check") or "?"] += 1
        for reason in extra.get("inconclusive") or []:
            inconclusive[reason] += 1

    tokens = sum((r.get("extra") or {}).get("llm", {}).get("tokens", 0) for r in items)
    cost = sum((r.get("extra") or {}).get("llm", {}).get("cost_usd", 0.0) for r in items)
    per_item_cost = sorted(
        (((r.get("extra") or {}).get("llm", {}).get("cost_usd", 0.0), r["url"]) for r in items),
        reverse=True,
    )

    return {
        "rows": len(rows),
        "items": len(items),
        "clean_items": clean,
        "outcomes": collections.Counter(r.get("outcome") for r in items),
        "findings_by_check": findings,
        "findings_by_tier": tiers,
        "suppressed_by_check": suppressed,
        "inconclusive_by_reason": inconclusive,
        "declined_by_check": declined_checks,
        "write_outcomes": collections.Counter((r.get("action"), r.get("outcome")) for r in writes),
        "llm": {
            "calls": len(llm_calls),
            "items_reaching_llm": sum(
                1 for r in items if (r.get("extra") or {}).get("llm", {}).get("calls")
            ),
            "tokens": tokens,
            "cost_usd": round(cost, 4),
            "top_cost": per_item_cost[:5],
        },
        "errors": [
            {
                "ts": r["ts"],
                "action": r.get("action"),
                "url": r.get("url"),
                "detail": r.get("detail"),
            }
            for r in rows
            if r.get("outcome") == "error"
        ],
    }


def _tier_label(finding):
    tier = finding.get("tier") or "?"
    kind = finding.get("kind")
    return f"{tier}/{kind}" if tier == "question" and kind else tier


def _table(counter, total=None, indent="  "):
    lines = []
    width = max((len(str(k)) for k in counter), default=0)
    for key, count in counter.most_common():
        share = f"  {count / total:5.1%}" if total else ""
        lines.append(f"{indent}{str(key):<{width}}  {count:>4}{share}")
    return "\n".join(lines) or f"{indent}(none)"


def report(data):
    items = data["items"]
    out = [
        f"Audit rows: {data['rows']}   items triaged: {items}",
        "",
        "Outcomes",
        _table(data["outcomes"], items),
        "",
        f"Findings by check  ({items - data['clean_items']} item(s) with findings, "
        f"{data['clean_items']} clean)",
        _table(data["findings_by_check"], items),
        "",
        "Findings by tier",
        _table(data["findings_by_tier"]),
        "",
        "Suppressed by engagement, per check",
        _table(data["suppressed_by_check"]),
        "",
        "Inconclusive passes, per failing lookup (#138)",
        _table(data["inconclusive_by_reason"], items),
        "",
        "Findings on items whose write didn't go through",
        _table(data["declined_by_check"]),
        "  (a decline can simply mean the account lacks the permission --",
        "   check the Writes section before reading this as a bad finding)",
        "",
        "Writes",
        _table(
            collections.Counter({f"{a} {o}": c for (a, o), c in data["write_outcomes"].items()})
        ),
        "",
        "LLM",
        f"  calls {data['llm']['calls']}   items reaching the LLM "
        f"{data['llm']['items_reaching_llm']}/{items}",
        f"  tokens {data['llm']['tokens']}   cost ${data['llm']['cost_usd']}",
    ]
    for cost, url in data["llm"]["top_cost"]:
        if cost:
            out.append(f"    ${cost:.4f}  {url}")
    if data["errors"]:
        out += ["", f"Errors ({len(data['errors'])})"]
        out += [f"  {e['ts']}  {e['action']}  {e['detail']}" for e in data["errors"][-10:]]
    return "\n".join(out)


def _jsonable(data):
    """JSON-safe copy: Counters become dicts, and the tuple keys of
    write_outcomes become "action outcome" strings."""
    out = {}
    for key, value in data.items():
        if key == "write_outcomes":
            out[key] = {f"{action} {outcome}": count for (action, outcome), count in value.items()}
        elif isinstance(value, collections.Counter):
            out[key] = dict(value)
        elif key == "llm":
            out[key] = dict(value, top_cost=[[c, u] for c, u in value["top_cost"]])
        else:
            out[key] = value
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="e.g. 30d, 12h, or an ISO date")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--audit", default=audit.default_path(), help="audit trail path")
    args = parser.parse_args()

    try:
        rows = load(args.audit, _parse_since(args.since))
    except FileNotFoundError:
        print(f"No audit trail at {args.audit}", file=sys.stderr)
        return 1

    data = summarize(rows)
    if args.json:
        print(json.dumps(_jsonable(data), indent=2))
    else:
        print(report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
