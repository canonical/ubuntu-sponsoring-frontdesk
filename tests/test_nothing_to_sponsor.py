"""check_nothing_to_sponsor (closing tier, bugs only): a bug whose fix is
under review on a linked MP is a duplicate sponsoring-queue entry, and a bug
with no patch and no MP has nothing to review yet -- both get
~ubuntu-sponsors unsubscribed. Found live on bug #2139024 (no patch, linked
MP already reviewed)."""

import checks
import main
from state import StateManager
from fakes import (
    FakeAttachment,
    FakeBug,
    FakeLLM,
    FakeMP,
    FakeTask,
    FakeTriageClient,
    FakeVote,
)

URL = "https://bugs.launchpad.net/ubuntu/+source/foo/+bug/123"


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


# --- unit ---------------------------------------------------------------------


def test_no_patch_no_mp_fires():
    bug = FakeBug(description="please fix this")
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"
    assert len(lp.comments) == 1
    assert "nothing for the sponsors team to review" in lp.comments[0]
    assert lp.unsubscribed == 1


def test_flagged_patch_attachment_means_something_to_sponsor():
    bug = FakeBug(attachments=[FakeAttachment("fix", type="Patch")])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


def test_patchy_filename_counts_even_without_the_patch_flag():
    bug = FakeBug(attachments=[FakeAttachment("foo_1.2-3.debdiff")])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False


def test_log_attachment_alone_is_not_a_patch():
    bug = FakeBug(attachments=[FakeAttachment("dmesg.log")])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"


def test_sync_request_is_exempt_despite_no_patch():
    bug = FakeBug(title="Sync foo 1.2-3 (main) from Debian unstable")
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


def test_linked_mp_with_sponsors_reviewer_fires_mp_review():
    mp = FakeMP(votes=[FakeVote("~ubuntu-sponsors")])
    bug = FakeBug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "mp_review"
    assert len(lp.comments) == 1
    assert mp.web_link in lp.comments[0]
    assert lp.unsubscribed == 1


def test_linked_mp_already_reviewed_fires_mp_review():
    mp = FakeMP(votes=[FakeVote("~rr", comment_link="/comments/1")])
    bug = FakeBug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "mp_review"


def test_linked_mp_without_review_signal_is_left_for_a_human():
    # An MP exists but has no sponsors reviewer and no review yet: can't tell
    # which queue entry to keep, so don't touch anything.
    mp = FakeMP(votes=[FakeVote("~rr")])
    bug = FakeBug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


def test_rejected_mp_is_ignored_and_no_patch_fires():
    mp = FakeMP(queue_status="Rejected", votes=[FakeVote("~rr", "/comments/1")])
    bug = FakeBug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"


def test_lookup_failure_is_inconclusive():
    class _BrokenBug(FakeBug):
        @property
        def linked_merge_proposals(self):
            raise TimeoutError("simulated Launchpad timeout")

        @linked_merge_proposals.setter
        def linked_merge_proposals(self, value):
            pass

    bug = _BrokenBug()
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is None
    assert lp.comments == []


def test_mps_are_not_this_checks_business():
    mp = FakeMP()
    lp = FakeTriageClient(objects={URL: mp})
    assert checks.check_nothing_to_sponsor(URL, mp, lp) is False


# --- end-to-end ---------------------------------------------------------------


def test_bug_with_reviewed_mp_closed_before_llm(tmp_path):
    # The live #2139024 case: SRU-ish bug, no patch, linked MP already
    # reviewed -- the old pipeline bounced it over the SRU template; now it
    # is closed as a duplicate entry before the LLM ever runs.
    sm = _state(tmp_path)
    llm_calls = []

    class _CountingLLM(FakeLLM):
        def triage_bug(self, obj):
            llm_calls.append(obj)
            return super().triage_bug(obj)

    mp = FakeMP(votes=[FakeVote("~rr", comment_link="/comments/1")])
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "In Progress")],
        description="[Impact] none given",
        linked_merge_proposals=[mp],
    )
    lp = FakeTriageClient(objects={URL: bug})

    main.triage_url(URL, sm, lp, _CountingLLM())

    assert llm_calls == []
    assert len(lp.comments) == 1
    assert "review continues on the merge proposal" in lp.comments[0]
    assert sm.get_status(URL)[0] == "DONE"
    assert "linked merge proposal" in sm.get_status(URL)[1]


def test_bug_without_patch_closed_end_to_end(tmp_path):
    sm = _state(tmp_path)
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")], description="broken")
    lp = FakeTriageClient(objects={URL: bug})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "nothing for the sponsors team to review" in lp.comments[0]
    assert sm.get_status(URL)[0] == "DONE"

    # Attaching a patch changes the facts fingerprint, so the bug is
    # re-triaged (not skipped at the facts-unchanged gate) once the
    # contributor re-subscribes ~ubuntu-sponsors.
    bug.attachments.append(FakeAttachment("fix.debdiff", type="Patch"))
    main.triage_url(URL, sm, lp, FakeLLM())
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
