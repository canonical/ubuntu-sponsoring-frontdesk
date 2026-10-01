"""Design #31: incomplete-tier findings are collected across the whole pass
and posted as ONE aggregated comment; closing-tier outcomes still
short-circuit and drop any findings collected so far (no nitpicking a change
that already landed)."""

from fakes import CLEAN_DIFF_TEXT, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

import checks
import main
from state import StateManager

URL = "https://code.launchpad.net/~marco/+merge/12345"


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


# --- render_findings_comment (pure) ------------------------------------------


def test_render_incomplete_findings_one_comment_with_bullets():
    out = checks.render_findings_comment(
        [
            checks.Finding("incomplete", "First problem."),
            checks.Finding("incomplete", "Second problem."),
        ]
    )
    assert out.startswith("Thanks for your contribution!")
    assert "Needs fixing before this can be sponsored:" in out
    assert "* First problem." in out
    assert "* Second problem." in out
    assert "please let us know!" in out
    assert "Nice to have" not in out


def test_render_for_bug_closes_with_the_back_to_new_hint():
    # A bug bounce also sets the tasks Incomplete (#70), so the closing
    # line explains the way back into the queue instead of "let us know".
    out = checks.render_findings_comment(
        [checks.Finding("incomplete", "First problem.")], for_bug=True
    )
    assert "status is being set to Incomplete" in out
    assert "set it back to New" in out
    assert "let us know" not in out


def test_render_question_only_has_no_blocking_section_or_closing_line():
    out = checks.render_findings_comment([checks.Finding("question", "A soft suggestion.")])
    assert "Nice to have" in out
    assert "* A soft suggestion." in out
    assert "Needs fixing" not in out
    assert "please let us know" not in out


def test_render_multiline_message_stays_attached_to_its_bullet():
    out = checks.render_findings_comment([checks.Finding("incomplete", "Line one.\nLine two.")])
    assert "* Line one.\n  Line two." in out


# --- end-to-end: several simultaneous problems -> one comment ----------------


def test_two_simultaneous_findings_surface_in_one_pass(tmp_path):
    # Merge conflicts (check 3) AND a missing changelog stanza (check 9):
    # before #31 the contributor learned about these one bot run at a time;
    # now both are bullets in the same single comment. A wrong-target-branch
    # finding (check 2) is deliberately NOT one of the two here -- since
    # #104 that suppresses check 3's finding for the pass (conflicts are a
    # downstream symptom of the wrong target, not a separate problem), so a
    # plain fix MP is used instead to keep this test about aggregation
    # itself, not that interaction (covered in test_mp_checks.py).
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        source="refs/heads/fix-lp2000001",
        # debian/-only diff: keeps check_direct_source_edit's nativeness
        # lookup (only triggered by non-debian edits) out of this test.
        diff=FakeDiff(
            "/d/1",
            50,
            conflicts="foo.c",
            diff_text="diff --git a/debian/control b/debian/control\n@@ -1 +1 @@\n-Foo\n+Bar\n",
        ),
    )
    lp = FakeTriageClient(objects={URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "merge conflicts" in lp.comments[0]
    assert "changelog" in lp.comments[0]  # the missing-stanza bullet
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_closing_outcome_drops_findings_collected_earlier(tmp_path):
    # Check 2 collects a wrong-target-branch finding, then check 4 (empty
    # diff -- the change already landed) fires: the item is resolved, so the
    # finding is dropped and only the terse closing comment posts. No point
    # nitpicking a change that was already uploaded.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 0, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "can be closed" in lp.comments[0]
    assert "should target" not in lp.comments[0]
    assert sm.get_status(URL)[0] == "DONE"


def test_pending_outcome_stays_fully_quiet_even_with_findings(tmp_path, monkeypatch):
    # check_stale_version says "matches the archive, published <24h ago":
    # the change already landed, git-ubuntu's importer will likely auto-close
    # the MP. Everything stays quiet -- including findings collected earlier
    # in the same pass -- and nothing is persisted, so the next run
    # re-evaluates from scratch.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})
    monkeypatch.setattr(checks, "check_stale_version", lambda *a, **k: "pending")

    main.triage_url(URL, sm, lp, FakeLLM())

    assert lp.comments == []
    assert sm.get_status(URL)[0] == "PENDING_ARCHIVE_IMPORT"
    assert sm.get_facts(URL) is None


def test_inconclusive_pass_posts_no_aggregate_and_skips_the_llm(tmp_path, monkeypatch):
    # #31's addendum: the aggregated comment claims to be the complete list
    # of what to fix this round; if any check couldn't determine its result,
    # posting it would claim a completeness it doesn't have. Stay silent,
    # skip the LLM (its finding would be gated anyway), retry next run.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})
    monkeypatch.setattr(checks, "check_stale_version", lambda *a, **k: None)

    llm_calls = []

    class _CountingLLM(FakeLLM):
        def triage_mp(self, obj):
            llm_calls.append(obj)
            return super().triage_mp(obj)

    main.triage_url(URL, sm, lp, _CountingLLM())

    assert lp.comments == []
    assert llm_calls == []
    assert sm.get_status(URL) is None
    assert sm.get_facts(URL) is None


def test_inconclusive_pass_records_which_lookup_failed(tmp_path, monkeypatch):
    # #138: an inconclusive pass posts nothing and persists nothing, so the
    # failing check appears in no other part of the audit -- it never
    # produces a finding to count. Without the reason, stats.py could say
    # only that ~70% of a live pass does nothing.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})
    monkeypatch.setattr(checks, "check_stale_version", lambda *a, **k: None)

    main.triage_url(URL, sm, lp, FakeLLM())

    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["outcome"] == "inconclusive"
    assert row["extra"]["inconclusive"] == ["check_stale_version"]


def test_inconclusive_reasons_are_deduplicated_and_ordered(tmp_path, monkeypatch):
    # Two checks failing in one pass are both named, once each, in the
    # order they were reached.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})
    monkeypatch.setattr(checks, "check_changelog_bug_reference", lambda *a, **k: None)
    monkeypatch.setattr(checks, "check_stale_version", lambda *a, **k: None)

    main.triage_url(URL, sm, lp, FakeLLM())

    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["extra"]["inconclusive"] == [
        "check_changelog_bug_reference",
        "check_stale_version",
    ]


def test_unchanged_pass_is_recorded_as_skipped_not_inconclusive(tmp_path):
    # #138: a pass that stops at the facts-unchanged gate is the bot working
    # correctly. Recording it as "inconclusive" (the old default for any path
    # that didn't call update_status) made ~70% of a live queue pass look like
    # failed lookups.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())  # first pass: persists facts
    lp.audit.records.clear()
    main.triage_url(URL, sm, lp, FakeLLM())  # second pass: nothing changed

    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["outcome"] == "skipped-unchanged"
    assert row["extra"]["inconclusive"] == []


def test_conclusive_pass_records_no_inconclusive_reason(tmp_path):
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["extra"]["inconclusive"] == []


# --- #131: the per-item audit record ------------------------------------------


def test_triage_writes_one_item_record_with_check_names(tmp_path):
    from fakes import FakeDiff, FakeLLM, FakeMP, FakeTriageClient

    import checks
    import main
    from state import StateManager

    url = "https://code.launchpad.net/~x/+merge/1"
    mp = FakeMP(diff=FakeDiff("/d/1", 50, conflicts="foo.c"))
    lp = FakeTriageClient(objects={url: mp})
    sm = StateManager(db_path=str(tmp_path / "s.db"))

    main.triage_url(url, sm, lp, FakeLLM())

    rows = [r for r in lp.audit.records if r["action"] == "triage"]
    assert len(rows) == 1
    row = rows[0]
    assert row["url"] == url
    assert row["target"] == "branch_merge_proposal"
    # This fixture has no usable `lp`, so a version check comes back None
    # and the pass is inconclusive -- which the record must say, since
    # "found nothing" and "couldn't tell" are different answers.
    assert row["outcome"] == "inconclusive"
    checks_fired = {f["check"] for f in row["extra"]["findings"]}
    assert "check_mp_conflicts" in checks_fired
    assert all(f["tier"] for f in row["extra"]["findings"])
    assert "elapsed_s" in row["extra"]
    assert isinstance(checks.Finding("incomplete", "x").tier, str)


def test_clean_item_records_no_findings(tmp_path):
    from fakes import CLEAN_DIFF_TEXT, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

    import main
    from state import StateManager

    url = "https://code.launchpad.net/~x/+merge/2"
    mp = FakeMP(diff=FakeDiff("/d/2", 50, diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={url: mp})
    sm = StateManager(db_path=str(tmp_path / "s.db"))

    main.triage_url(url, sm, lp, FakeLLM())

    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["extra"]["findings"] == []
