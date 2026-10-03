"""
Read-only retrospective over the audit trail (design_journal.md #131).

Answers the questions the audit log alone couldn't: what does the bot
actually catch, how often is it wrong, what does it cost. Reads
``audit.jsonl`` (see audit.default_path) and writes nothing.

The audit trail says what the bot DID; `--queue` says where every item it
tracks stands right now, read from state.db (#142).

Usage:
    python3 stats.py [--since 30d] [--json] [--audit PATH]
    python3 stats.py --queue [--json] [--state PATH]
"""

import argparse
import collections
import datetime
import json
import sqlite3
import sys

import audit
import state
import sweep


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


def queue(db_path):
    """Where every tracked item stands now, from state.db.

    The audit trail is a history: it says what happened to an item on the
    pass that touched it, not what is outstanding today. This reads the
    state the bot acts on -- the same rows the facts-unchanged gate and the
    sweep consult -- so "what is the queue waiting on" doesn't have to be
    reconstructed from months of rows.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT url, status, last_checked, details FROM requests").fetchall()

    now = datetime.datetime.now(datetime.timezone.utc)
    by_status = collections.Counter()
    waiting, humans = [], []
    for url, status, last_checked, details in rows:
        by_status[status or "?"] += 1
        age = _age(last_checked, now)
        if status == "WAITING_ON_CONTRIBUTOR":
            waiting.append((age, url, details or ""))
        elif status == "READY_FOR_HUMAN":
            humans.append((age, url, details or ""))
    waiting.sort(key=lambda item: (item[0] is None, -(item[0] or 0)))
    humans.sort(key=lambda item: (item[0] is None, -(item[0] or 0)))
    sweep_days = sweep.STALE_BOUNCE_AGE.days
    return {
        "tracked": len(rows),
        "by_status": by_status,
        "waiting_on_contributor": waiting,
        "ready_for_human": humans,
        "sweep_days": sweep_days,
        # Bugs only, because the sweep deliberately leaves MPs alone (#66).
        # NOT the sweep's own verdict: the sweep measures Launchpad's
        # bug_task.date_incomplete, while this is the age of OUR last write.
        # The two differ whenever a human changed the status in between, so
        # this is a prompt to look, not a list of things the sweep will act
        # on.
        "quiet_bounced_bugs": [
            (age, url)
            for age, url, _ in waiting
            if age is not None and age >= sweep_days and "+merge/" not in url
        ],
    }


def _age(last_checked, now):
    """Whole days since `last_checked`, or None if it can't be read."""
    if not last_checked:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(last_checked))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (now - when).days


def queue_report(data):
    out = [
        f"Tracked items: {data['tracked']}",
        "",
        "Status",
        _table(data["by_status"], data["tracked"]),
    ]
    for key, title in (
        ("waiting_on_contributor", "Waiting on the contributor (oldest first)"),
        ("ready_for_human", "Ready for a human sponsor (oldest first)"),
    ):
        out += ["", title]
        if not data[key]:
            out.append("  (none)")
        for age, url, details in data[key][:15]:
            age_text = f"{age:>4}d" if age is not None else "   ?d"
            out.append(f"  {age_text}  {url}")
            if details:
                out.append(f"         {details}")
        if len(data[key]) > 15:
            out.append(f"  ... and {len(data[key]) - 15} more")
    quiet = data["quiet_bounced_bugs"]
    out += [
        "",
        f"Bounced bugs untouched by the bot for {data['sweep_days']}+ days (bugs only)",
        "  (indicative: the sweep's own clock is Launchpad's date_incomplete,",
        "   not our last write, so it may already have handled these)",
    ]
    out += [f"  {age:>4}d  {url}" for age, url in quiet[:10]] or ["  (none)"]
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="e.g. 30d, 12h, or an ISO date")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--audit", default=audit.default_path(), help="audit trail path")
    parser.add_argument(
        "--queue",
        action="store_true",
        help="where tracked items stand now, from state.db, instead of the audit retrospective",
    )
    parser.add_argument("--state", default=state.default_db_path(), help="state.db path")
    args = parser.parse_args()

    if args.queue:
        try:
            data = queue(args.state)
        except sqlite3.OperationalError:
            print(f"No state database at {args.state}", file=sys.stderr)
            return 1
        print(json.dumps(_jsonable(data), indent=2) if args.json else queue_report(data))
        return 0

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
