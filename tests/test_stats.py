"""stats.py: the read-only retrospective over the audit trail (#131).

It must never fail on a log it doesn't fully understand -- a truncated last
line, rows from before the per-item record existed, a missing `extra` --
since its whole job is to be run casually against whatever the VM has.
"""

import json

import stats


def _write(tmp_path, rows):
    path = tmp_path / "audit.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _item(url, outcome="WAITING_ON_CONTRIBUTOR", findings=(), suppressed=(), llm=None):
    return {
        "ts": "2026-09-25T12:00:00+00:00",
        "url": url,
        "action": "triage",
        "target": "bug",
        "mode": "yes",
        "outcome": outcome,
        "detail": "",
        "extra": {
            "findings": list(findings),
            "suppressed": list(suppressed),
            "closing": None,
            "engaged": False,
            "llm": llm or {},
            "elapsed_s": 1.0,
        },
    }


def _finding(check, tier="incomplete", kind=None):
    return {"check": check, "tier": tier, "kind": kind}


def test_counts_findings_by_check_and_tier(tmp_path):
    path = _write(
        tmp_path,
        [
            _item("u1", findings=[_finding("check_stale_version")]),
            _item("u2", findings=[_finding("check_stale_version")]),
            _item(
                "u3",
                findings=[_finding("check_xsbc_original_maintainer", "question", "advisory")],
            ),
            _item("u4", outcome="READY_FOR_HUMAN"),
        ],
    )
    data = stats.summarize(stats.load(path))
    assert data["items"] == 4
    assert data["clean_items"] == 1
    assert data["findings_by_check"]["check_stale_version"] == 2
    assert data["findings_by_tier"]["incomplete"] == 2
    assert data["findings_by_tier"]["question/advisory"] == 1
    assert data["outcomes"]["READY_FOR_HUMAN"] == 1


def test_declined_writes_are_attributed_to_the_items_checks(tmp_path):
    # A declined comment is the closest thing to "the bot was wrong here".
    path = _write(
        tmp_path,
        [
            _item("u1", findings=[_finding("check_target_branch")]),
            {
                "ts": "2026-09-25T12:00:01+00:00",
                "url": "u1",
                "action": "comment",
                "target": "u1",
                "mode": "interactive",
                "outcome": "declined",
                "detail": "Thanks!",
            },
        ],
    )
    data = stats.summarize(stats.load(path))
    assert data["declined_by_check"]["check_target_branch"] == 1
    assert data["write_outcomes"][("comment", "declined")] == 1


def test_suppressed_findings_are_counted_separately(tmp_path):
    path = _write(
        tmp_path,
        [_item("u1", findings=[_finding("a")], suppressed=[_finding("check_sru_newer_series")])],
    )
    data = stats.summarize(stats.load(path))
    assert data["suppressed_by_check"]["check_sru_newer_series"] == 1


def test_llm_cost_is_summed_per_item(tmp_path):
    path = _write(
        tmp_path,
        [
            _item("u1", llm={"calls": 2, "tokens": 100, "cost_usd": 0.01}),
            _item("u2", llm={"calls": 1, "tokens": 50, "cost_usd": 0.02}),
            _item("u3"),
        ],
    )
    data = stats.summarize(stats.load(path))
    assert data["llm"]["tokens"] == 150
    assert data["llm"]["cost_usd"] == 0.03
    assert data["llm"]["items_reaching_llm"] == 2
    assert data["llm"]["top_cost"][0] == (0.02, "u2")


def test_old_rows_without_a_triage_record_still_report(tmp_path):
    # The whole 721-row history predates #131: writes and errors must still
    # be summarized, with zero items.
    path = _write(
        tmp_path,
        [
            {
                "ts": "2026-07-08T09:07:27+00:00",
                "url": "u1",
                "action": "unsubscribe",
                "target": "u1",
                "mode": "yes",
                "outcome": "error",
                "detail": "HTTP Error 401",
            }
        ],
    )
    data = stats.summarize(stats.load(path))
    assert data["items"] == 0
    assert len(data["errors"]) == 1
    assert stats.report(data)  # must not raise on empty sections


def test_truncated_last_line_is_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(_item("u1")) + '\n{"ts": "2026')
    assert len(stats.load(str(path))) == 1


def test_since_filters_by_timestamp(tmp_path):
    # Timestamps relative to now: a fixed "recent" date silently ages out
    # and the test starts failing a day later.
    import datetime

    old = _item("old")
    old["ts"] = "2020-01-01T00:00:00+00:00"
    recent = _item("new")
    recent["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = _write(tmp_path, [old, recent])
    rows = stats.load(path, stats._parse_since("1d"))
    assert [r["url"] for r in rows] == ["new"]
