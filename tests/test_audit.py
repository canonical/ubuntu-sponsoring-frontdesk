"""Fix #6: the audit log captures a durable, structured record."""

import json

from audit import AuditLog


def test_record_appends_json_lines_with_expected_fields(tmp_path):
    log = AuditLog(path=str(tmp_path / "audit.jsonl"))
    log.record(
        url="u1",
        action="comment",
        target="t1",
        mode="yes",
        outcome="performed",
        detail="Hello!\nsecond line",
    )
    log.record(
        url="u2",
        action="set_status",
        target="t2",
        mode="dry-run",
        outcome="dry-run",
        detail="Needs fixing",
    )

    with open(log.path) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]

    assert len(rows) == 2
    first = rows[0]
    assert set(first) == {"ts", "url", "action", "target", "mode", "outcome", "detail"}
    assert first["outcome"] == "performed"
    # detail is summarized to the first line
    assert first["detail"] == "Hello!"
    assert rows[1]["action"] == "set_status"
