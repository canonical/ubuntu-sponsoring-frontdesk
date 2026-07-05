"""Design #31: incomplete-tier findings are collected across the whole pass
and posted as ONE aggregated comment; closing-tier outcomes still
short-circuit and drop any findings collected so far (no nitpicking a change
that already landed)."""

import checks
import main
from state import StateManager
from fakes import CLEAN_DIFF_TEXT, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

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


def test_render_question_only_has_no_blocking_section_or_closing_line():
    out = checks.render_findings_comment(
        [checks.Finding("question", "A soft suggestion.")]
    )
    assert "Nice to have" in out
    assert "* A soft suggestion." in out
    assert "Needs fixing" not in out
    assert "please let us know" not in out


def test_render_multiline_message_stays_attached_to_its_bullet():
    out = checks.render_findings_comment(
        [checks.Finding("incomplete", "Line one.\nLine two.")]
    )
    assert "* Line one.\n  Line two." in out


# --- end-to-end: several simultaneous problems -> one comment ----------------


def test_two_simultaneous_findings_surface_in_one_pass(tmp_path):
    # Wrong target branch (merge MP targeting ubuntu/devel) AND merge
    # conflicts: before #31 the contributor learned about these one bot run
    # at a time; now both are bullets in the same single comment.
    sm = _state(tmp_path)
    mp = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, conflicts="foo.c", diff_text=CLEAN_DIFF_TEXT),
    )
    lp = FakeTriageClient(objects={URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "merge conflicts" in lp.comments[0]
    assert "should target" in lp.comments[0]  # the target-branch bullet
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
