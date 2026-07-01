"""
Append-only audit trail for every write the bot makes (or would make).

For a bot that mutates a public community tracker, ``print`` is not a record.
Each attempted write -- including what dry-run *would* have done and writes that
were skipped or declined -- is appended as one JSON object per line so the trail
is durable, greppable, and reviewable after the fact.
"""

import datetime
import json


def _summarize(text, limit=120):
    """First non-empty line of text, truncated -- enough to identify the action."""
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


class AuditLog:
    def __init__(self, path="audit.jsonl"):
        self.path = path

    def record(self, *, url, action, target, mode, outcome, detail=""):
        """
        Append one audit entry.

        outcome is one of:
          performed | dry-run | declined | no-tty | skipped-duplicate | error
        """
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "url": url,
            "action": action,
            "target": target,
            "mode": mode,
            "outcome": outcome,
            "detail": _summarize(detail),
        }
        with open(self.path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry
