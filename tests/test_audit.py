"""Fix #6: the audit log captures a durable, structured record."""

import json

from audit import AuditLog, default_path


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


# --- #130: default location is the cache dir, not the checkout -----------------


def test_default_path_is_the_cache_dir(monkeypatch):
    monkeypatch.delenv("SPONSORING_BOT_AUDIT", raising=False)
    monkeypatch.setenv("HOME", "/home/someone")
    assert default_path() == ("/home/someone/.cache/ubuntu-sponsoring-frontdesk/audit.jsonl")


def test_default_path_is_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("SPONSORING_BOT_AUDIT", str(tmp_path / "elsewhere.jsonl"))
    assert default_path() == str(tmp_path / "elsewhere.jsonl")


def test_audit_log_creates_its_directory(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    log = AuditLog(path=str(path))
    log.record(url="u", action="comment", target="t", mode="yes", outcome="performed")
    assert path.exists()
