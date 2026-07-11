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
    FakeBugMessage,
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


# The default FakeMP targets debian/sid (a merge, landing via devel), so
# qualifying bugs need an open devel ask: the plain '(Ubuntu)' task.
def _devel_ask_bug(**kwargs):
    return FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")], **kwargs)


def test_linked_mp_with_sponsors_reviewer_fires_mp_review():
    mp = FakeMP(votes=[FakeVote("~ubuntu-sponsors")])
    bug = _devel_ask_bug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "mp_review"
    assert len(lp.comments) == 1
    assert mp.web_link in lp.comments[0]
    assert lp.unsubscribed == 1


def test_linked_mp_already_reviewed_fires_mp_review():
    mp = FakeMP(votes=[FakeVote("~rr", comment_link="/comments/1")])
    bug = _devel_ask_bug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "mp_review"


def test_linked_mp_without_review_signal_is_left_for_a_human():
    # An MP exists but has no sponsors reviewer and no review yet: can't tell
    # which queue entry to keep, so don't touch anything.
    mp = FakeMP(votes=[FakeVote("~rr")])
    bug = _devel_ask_bug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


# --- the MP must be a sponsoring venue (gnocchi bug #2148798) ------------------


def test_team_fork_mp_is_not_a_review_venue():
    # ~ubuntu-openstack-dev-style fork branches (master, stable/*) are a
    # team's internal workflow, never sponsoring-queue entries -- however
    # reviewed. The MP still shields the no_patch close (left for a human).
    mp = FakeMP(
        target="refs/heads/master", votes=[FakeVote("~rr", comment_link="/c/1")]
    )
    bug = _devel_ask_bug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


def test_merged_mp_is_not_a_review_venue():
    # A Merged MP's review is over; it can't be where the review continues.
    mp = FakeMP(
        queue_status="Merged", votes=[FakeVote("~rr", comment_link="/c/1")]
    )
    bug = _devel_ask_bug(linked_merge_proposals=[mp])
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []


def test_mp_for_a_series_the_bug_does_not_ask_about_is_ignored():
    # The MP targets jammy but the bug only has an open noble task.
    mp = FakeMP(
        target="refs/heads/ubuntu/jammy-devel",
        votes=[FakeVote("~rr", comment_link="/c/1")],
    )
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Noble)", "In Progress")],
        linked_merge_proposals=[mp],
    )
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False


def test_mp_matching_an_open_series_task_fires_mp_review():
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        votes=[FakeVote("~rr", comment_link="/c/1")],
    )
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Noble)", "In Progress")],
        linked_merge_proposals=[mp],
    )
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "mp_review"


def test_closed_series_task_does_not_qualify_the_mp():
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        votes=[FakeVote("~rr", comment_link="/c/1")],
    )
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Noble)", "Fix Released")],
        linked_merge_proposals=[mp],
    )
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False


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
    # contributor re-subscribes ~ubuntu-sponsors. Content must be
    # debdiff-shaped (touch debian/) or Check 10 (#64) would bounce it.
    bug.attachments.append(
        FakeAttachment(
            "fix.debdiff",
            type="Patch",
            content=(
                "--- foo-1.0/debian/rules\n+++ foo-1.1/debian/rules\n"
                "@@ -1 +1 @@\n-a\n+b\n"
            ),
        )
    )
    main.triage_url(URL, sm, lp, FakeLLM())
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


# --- needs-packaging: PPA/git links skip the no_patch close (design #50) ----
# Found live by seb128: bug #2129955 (comment names a PPA + git repo) and
# #2142921 (similar) were both wrongly auto-closed as "no_patch".


def _needs_packaging_bug(**kwargs):
    kwargs.setdefault("tasks", [FakeTask("ubuntu", "New")])
    return FakeBug(**kwargs)


def test_needs_packaging_with_ppa_link_in_description_is_left_for_a_human():
    bug = _needs_packaging_bug(
        description="I have it building in a PPA at "
        "https://launchpad.net/~aglinserer/+archive/ubuntu/ub-packaging"
    )
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []
    assert getattr(lp, "unsubscribed", 0) == 0


def test_needs_packaging_with_git_link_in_a_comment_is_left_for_a_human():
    bug = _needs_packaging_bug(description="new package request")
    bug.messages = [
        FakeBugMessage(
            "~contrib",
            "Sources are at https://code.launchpad.net/~aglinserer/+git/vulkan-profiles",
        )
    ]
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False


def test_needs_packaging_without_any_link_still_closes():
    # No PPA/git mention at all: nothing changes from the pre-#50 behavior.
    bug = _needs_packaging_bug(description="please package this")
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"


def test_ordinary_bug_with_a_ppa_link_still_closes():
    # The exemption is scoped to needs-packaging: an ordinary bug pointing
    # at a PPA in a comment is unusual enough that guessing "reviewable"
    # would risk more false positives than it prevents (seb128).
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")],
        description="see https://launchpad.net/~someone/+archive/ubuntu/ppa",
    )
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"


def test_needs_packaging_comment_read_failure_does_not_crash():
    bug = _needs_packaging_bug(description="new package")

    class BoomMessages:
        def __iter__(self):
            raise RuntimeError("timeout")

    bug.messages = BoomMessages()
    lp = FakeTriageClient(objects={URL: bug})
    # Falls back to scanning the description alone; no link there -> closes
    # as before, doesn't propagate the comment-read failure as inconclusive.
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"
