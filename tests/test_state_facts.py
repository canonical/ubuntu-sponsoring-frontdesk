"""Fix #2: facts-gated re-triage (the Marco lifecycle) and StateManager facts."""

import logging

import main
from state import StateManager
from fakes import FakeMP, FakeDiff, FakeTriageClient, FakeLLM

URL = "https://code.launchpad.net/~marco/+merge/12345"


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


def test_facts_roundtrip_and_change_detection(tmp_path):
    sm = _state(tmp_path)
    assert sm.get_facts(URL) is None
    sm.update_status(URL, "WAITING_ON_CONTRIBUTOR", "x", facts={"a": 1})
    assert sm.get_facts(URL) == {"a": 1}
    # update without facts must not wipe the stored snapshot
    sm.update_status(URL, "READY_FOR_HUMAN", "y")
    assert sm.get_facts(URL) == {"a": 1}


def test_marco_lifecycle(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(objects={})
    llm = FakeLLM()

    # 10:00 wrong branch -> one bounce comment
    lp.objects[URL] = FakeMP(target=".../ubuntu/devel", diff=FakeDiff("/diff/901", 50))
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"

    # 10:15 / 10:30 cron re-runs, contributor idle (bot set Needs fixing) -> no new comment
    main.triage_url(URL, sm, lp, llm)
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1

    # 14:00 retarget + new push -> facts change -> re-triage to human review
    lp.objects[URL] = FakeMP(target=".../debian/sid", diff=FakeDiff("/diff/902", 50))
    main.triage_url(URL, sm, lp, llm)
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
    assert len(lp.comments) == 1


def test_inconclusive_check_does_not_persist_facts_so_next_run_retries(
    tmp_path, caplog
):
    # A merge MP correctly targeting debian/sid, with no diff_text on its
    # preview diff -- check_changelog_bug_reference and check_stale_version
    # both can't read the diff, so neither fires but the run is
    # inconclusive (not a confirmed "nothing to flag").
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={URL: FakeMP(target="refs/heads/debian/sid", diff=FakeDiff("/d/1", 50))}
    )
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
    assert sm.get_facts(URL) is None  # inconclusive -> not persisted

    # A second run against the exact same, unchanged MP object must NOT be
    # skipped by the facts-unchanged gate -- there's no stored snapshot to
    # compare against, so the full pipeline runs again rather than being
    # cached as "nothing to do" forever.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        main.triage_url(URL, sm, lp, llm)
    assert "Skipping (nothing to do)" not in caplog.text
    assert "Moving to LLM review" in caplog.text


class _RaisesOnQueueStatus:
    """Simulates an unguarded launchpadlib attribute read (e.g.
    lp_obj.queue_status in checks.check_administrative_state) hitting the
    LPClient timeout (design_journal.md #33) -- no try/except of its own,
    unlike the lookups in archive_lookup.py."""

    resource_type_link = "https://api.launchpad.net/devel/#branch_merge_proposal"
    target_git_path = "refs/heads/debian/sid"
    source_git_path = "refs/heads/merge-1.2-3"
    preview_diff = None
    date_created = None
    self_link = "https://api.launchpad.net/devel/~human/+merge/999"

    @property
    def queue_status(self):
        raise TimeoutError("simulated Launchpad API timeout")


def test_unexpected_exception_does_not_crash_the_run(tmp_path, caplog):
    # A single stalled/failed launchpadlib call anywhere in the pipeline must
    # not propagate out of triage_url -- that would abort an --all run and
    # silently skip every later queue item (design_journal.md #33).
    sm = _state(tmp_path)
    lp = FakeTriageClient(objects={URL: _RaisesOnQueueStatus()})
    llm = FakeLLM()

    with caplog.at_level(logging.WARNING):
        result = main.triage_url(URL, sm, lp, llm)

    assert result is None
    assert "Unexpected error triaging" in caplog.text
    # Nothing persisted -- next run retries from scratch rather than caching
    # a failure as a clean pass.
    assert sm.get_facts(URL) is None
    assert sm.get_status(URL) is None


def test_force_bypasses_facts_gate(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={URL: FakeMP(target=".../ubuntu/devel", diff=FakeDiff("/d/1", 5))}
    )
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1
    # unchanged facts but --force -> deterministic check runs again, comments again
    main.triage_url(URL, sm, lp, llm, force=True)
    assert len(lp.comments) == 2
