"""Fix #2: facts-gated re-triage (the Marco lifecycle) and StateManager facts."""

import logging

from fakes import (
    BOT,
    CLEAN_DIFF_TEXT,
    HUMAN,
    FakeArchive,
    FakeAttachment,
    FakeBug,
    FakeBugMessage,
    FakeDiff,
    FakeLLM,
    FakeMP,
    FakePublication,
    FakeRoot,
    FakeTask,
    FakeTriageClient,
)

import facts
import main
from state import StateManager

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
    lp.objects[URL] = FakeMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/diff/901", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"

    # 10:15 / 10:30 cron re-runs, contributor idle (bot set Needs fixing) -> no new comment
    main.triage_url(URL, sm, lp, llm)
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1

    # 14:00 retarget + new push -> facts change -> re-triage to human review
    lp.objects[URL] = FakeMP(
        target=".../debian/sid",
        diff=FakeDiff("/diff/902", 50, diff_text=CLEAN_DIFF_TEXT),
    )
    main.triage_url(URL, sm, lp, llm)
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
    assert len(lp.comments) == 1


def test_inconclusive_check_does_not_persist_facts_so_next_run_retries(tmp_path, caplog):
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
    # Design #31's addendum: an inconclusive pass posts nothing, skips the
    # LLM phase, and records nothing -- the aggregated review must not claim
    # completeness it doesn't have.
    assert sm.get_status(URL) is None
    assert sm.get_facts(URL) is None  # inconclusive -> not persisted

    # A second run against the exact same, unchanged MP object must NOT be
    # skipped by the facts-unchanged gate -- there's no stored snapshot to
    # compare against, so the full pipeline runs again rather than being
    # cached as "nothing to do" forever.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        main.triage_url(URL, sm, lp, llm)
    assert "Skipping (nothing to do)" not in caplog.text
    assert "couldn't be fully evaluated" in caplog.text


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


def test_dry_run_bounce_does_not_persist_facts_so_a_real_run_still_writes(tmp_path):
    # A --dry-run pass must not cache a bounce-worthy item as handled:
    # persisting facts would make a later --interactive/--yes run skip it at
    # the facts-unchanged gate and the bounce would silently never be posted.
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
            )
        },
        write_outcome="dry-run",
    )
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1  # the intended (not performed) write
    assert sm.get_facts(URL) is None  # not cached as handled

    # The "real" run against the unchanged MP re-triages and writes for real.
    lp.write_outcome = "performed"
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 2
    assert sm.get_facts(URL) is not None  # now handled, cache it


def test_declined_write_is_retried_next_run(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
            )
        },
        write_outcome="declined",
    )
    main.triage_url(URL, sm, lp, FakeLLM())
    assert sm.get_facts(URL) is None  # declined != handled; re-prompt next run


def test_skipped_duplicate_counts_as_handled(tmp_path):
    # An identical bot comment already on Launchpad means the item IS handled
    # (e.g. state.db was lost); facts should persist so we stop re-triaging.
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
            )
        },
        write_outcome="skipped-duplicate",
    )
    main.triage_url(URL, sm, lp, FakeLLM())
    assert sm.get_facts(URL) is not None


def _root_with_archive(archive):
    root = FakeRoot()
    root.distributions["ubuntu"].main_archive = archive
    return root


def test_archive_version_is_part_of_the_fingerprint(tmp_path):
    # Design #37: an archive upload landing after an item's first triage must
    # re-trigger triage, even though nothing contributor-controlled changed.
    sm = _state(tmp_path)
    archive = FakeArchive(pubs=[FakePublication("1.0-1")])
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
            )
        },
        lp=_root_with_archive(archive),
    )
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1
    assert sm.get_facts(URL)["archive_version"] == "1.0-1"

    # unchanged archive + unchanged MP -> skipped
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1

    # a new archive upload changes the fingerprint -> full re-triage
    archive.pubs = [FakePublication("2.0-1")]
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 2


def test_archive_lookup_failure_is_inconclusive_not_cached(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT),
            )
        },
        lp=_root_with_archive(FakeArchive(fail=True)),
    )
    main.triage_url(URL, sm, lp, FakeLLM())
    # The pass is inconclusive, so the wrong-target-branch finding is NOT
    # posted (design #31's addendum: an aggregated review that can't be
    # complete stays silent and retries)...
    assert lp.comments == []
    # ...and a failed fingerprint lookup must never be persisted: two
    # consecutive failures would compare equal at the gate and freeze the item.
    assert sm.get_facts(URL) is None


def test_archive_version_prefers_proposed_pocket():
    root = _root_with_archive(
        FakeArchive(pubs=[FakePublication("1.0-1"), FakePublication("1.1-1", "Proposed")])
    )
    assert facts._archive_version(root, FakeMP()) == "1.1-1"


def test_archive_version_nothing_published_is_a_stable_empty_string():
    root = _root_with_archive(FakeArchive(pubs=[]))
    assert facts._archive_version(root, FakeMP()) == ""


def test_archive_version_lookup_failure_is_none():
    root = _root_with_archive(FakeArchive(fail=True))
    assert facts._archive_version(root, FakeMP()) is None


def test_force_bypasses_facts_gate(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={
            URL: FakeMP(
                target=".../ubuntu/devel",
                diff=FakeDiff("/d/1", 5, diff_text=CLEAN_DIFF_TEXT),
            )
        }
    )
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1
    # unchanged facts but --force -> deterministic check runs again, comments again
    main.triage_url(URL, sm, lp, llm, force=True)
    assert len(lp.comments) == 2


# --- #102: fingerprint completeness (external review finding) ----------------
# Every field a check consumes must change the fingerprint when edited;
# otherwise the facts-unchanged gate skips the item forever after an edit.


def _bug_facts(**kwargs):
    return facts.build_facts(FakeBug(**kwargs))


def test_bug_title_is_part_of_the_fingerprint():
    a = _bug_facts(title="Sync foo 1.0-1 from Debian unstable")
    b = _bug_facts(title="Please merge foo 1.0-1 from Debian unstable")
    assert a != b


def test_bug_comment_content_is_part_of_the_fingerprint():
    bug = FakeBug()
    bug.messages = [FakeBugMessage(HUMAN, "initial report")]
    a = facts.build_facts(bug)
    bug.messages = [
        FakeBugMessage(HUMAN, "initial report"),
        FakeBugMessage(HUMAN, "packaging at ppa:marco/foo now"),
    ]
    b = facts.build_facts(bug)
    assert a != b


def test_service_account_comments_do_not_change_the_fingerprint():
    bug = FakeBug()
    bug.messages = [FakeBugMessage(HUMAN, "initial report")]
    a = facts.build_facts(bug)
    bug.messages = [
        FakeBugMessage(HUMAN, "initial report"),
        FakeBugMessage(BOT, "the bot's own bounce comment"),
    ]
    b = facts.build_facts(bug)
    # The bot commenting must not look like a contributor change.
    assert a == b


def test_unreadable_comments_are_none_not_a_stable_value():
    bug = FakeBug()

    class _Boom:
        def __iter__(self):
            raise TimeoutError("lp timeout")

    bug.messages = _Boom()
    snapshot = facts.build_facts(bug)
    assert snapshot["comments_digest"] is None


def test_mp_source_branch_is_part_of_the_fingerprint():
    a = facts.build_facts(FakeMP(source="refs/heads/merge-1.2-3"))
    b = facts.build_facts(FakeMP(source="refs/heads/fix-crash"))
    assert a != b


def test_mp_linked_bug_description_is_part_of_the_fingerprint():
    bug = FakeBug(description="[Impact]\nTBD")
    a = facts.build_facts(FakeMP(bugs=[bug]))
    bug.description = "[Impact]\nUsers crash on boot.\n[Test Plan]\n..."
    b = facts.build_facts(FakeMP(bugs=[bug]))
    assert a != b


def test_mp_linked_bug_attachment_is_part_of_the_fingerprint():
    bug = FakeBug()
    a = facts.build_facts(FakeMP(bugs=[bug]))
    bug.attachments = [FakeAttachment("fix.debdiff", content="x")]
    b = facts.build_facts(FakeMP(bugs=[bug]))
    assert a != b


def test_mp_unreadable_linked_bugs_are_none(tmp_path):
    class _BrokenBugs:
        def __iter__(self):
            raise TimeoutError("lp timeout")

    mp = FakeMP()
    mp.bugs = _BrokenBugs()
    snapshot = facts.build_facts(mp)
    assert snapshot["linked_bugs"] is None


def test_comment_digest_lookup_failure_is_inconclusive_not_cached(tmp_path):
    # main must treat a None lookup field like archive_version's (#37):
    # inconclusive, nothing persisted, retried next run.
    sm = _state(tmp_path)
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])

    class _Boom:
        def __iter__(self):
            raise TimeoutError("lp timeout")

    bug.messages = _Boom()
    bug_url = "https://bugs.launchpad.net/ubuntu/+source/foo/+bug/42"
    lp = FakeTriageClient(objects={bug_url: bug})
    main.triage_url(bug_url, sm, lp, FakeLLM())
    assert sm.get_facts(bug_url) is None
