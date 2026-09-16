"""Fix #5: admin check looks at the relevant Ubuntu series tasks only."""

from fakes import FakeBug, FakeMP, FakeTask

import checks

_FIX_COMMITTED = [FakeTask("foo (Ubuntu)", "Fix Committed")]


class _LP:
    def __init__(self):
        self.comments = []
        self.unsubscribed = 0

    def comment(self, obj, message):
        self.comments.append(message)

    def unsubscribe_sponsors(self, obj):
        self.unsubscribed += 1


def _run(tasks, pkg, lp=None):
    lp = lp if lp is not None else _LP()
    fired = checks.check_administrative_state("url", FakeBug(tasks=tasks), lp, source_package=pkg)
    if not any(t.status == "Fix Committed" for t in tasks):
        # #75: without a Fix Committed task this check never writes --
        # Fix Released bugs drop off the next sponsoring-report build on
        # their own. Fix Committed stays listed, hence writes (#81).
        assert lp.comments == [] and lp.unsubscribed == 0
    return fired


def test_upstream_done_but_ubuntu_open_does_not_unsubscribe():
    # The original bug: an upstream 'Fix Released' must not drop the bug.
    tasks = [
        FakeTask("walinuxagent", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Jammy)", "In Progress"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_one_series_landed_another_open_is_not_done():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Noble)", "In Progress"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_targeted_landed_untargeted_wontfix_invalid_is_done():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Noble)", "Won't Fix"),
        FakeTask("walinuxagent (Ubuntu Oracular)", "Invalid"),
    ]
    assert _run(tasks, "walinuxagent") is True


def test_all_closed_but_nothing_landed_is_not_claimed_complete():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Invalid"),
        FakeTask("walinuxagent (Ubuntu Noble)", "Won't Fix"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_other_package_done_is_ignored():
    tasks = [
        FakeTask("otherpkg (Ubuntu)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu)", "New"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_simple_devel_release_is_done():
    assert _run([FakeTask("mosquitto (Ubuntu)", "Fix Released")], "mosquitto") is True


def test_unknown_package_falls_back_to_any_ubuntu_task():
    assert _run([FakeTask("mosquitto (Ubuntu)", "Fix Released")], None) is True


def test_merged_mp_is_handled():
    mp = FakeMP(queue_status="Merged")
    assert checks.check_administrative_state("u", mp, _LP()) is True


def test_fix_committed_unsubscribes_with_an_explanation():
    # #81 (bug #2159516): Fix Committed = uploaded, awaiting release -- the
    # sponsoring report keeps listing it (confirmed live), so unlike Fix
    # Released (#75) the bot must unsubscribe, and explain why (seb128
    # chose always-comment over engaged-aware silence for transparency).
    lp = _LP()
    tasks = [FakeTask("xdg-desktop-portal-wlr (Ubuntu)", "Fix Committed")]
    assert _run(tasks, "xdg-desktop-portal-wlr", lp=lp) is True
    assert "uploaded and is awaiting release" in lp.comments[0]
    # No task-status summary in the comment: the bug page already shows it
    # (seb128 wording review).
    assert "Fix Committed" not in lp.comments[0]
    assert lp.unsubscribed == 1


def test_mixed_released_and_committed_still_unsubscribes():
    # One series released, another still in -proposed: the Committed task
    # alone keeps the bug on the report.
    lp = _LP()
    tasks = [
        FakeTask("foo (Ubuntu Noble)", "Fix Released"),
        FakeTask("foo (Ubuntu Jammy)", "Fix Committed"),
    ]
    assert _run(tasks, "foo", lp=lp) is True
    assert lp.unsubscribed == 1


def test_closed_task_with_a_still_live_linked_mp_is_not_done():
    # #112, found live (backport-iwlwifi-dkms bug #2166733): the task went
    # Fix Committed when an EARLIER MP merged, but a follow-up MP on the
    # same bug was still Needs review -- a single task can't reflect that
    # there's a second, unrelated review still pending.
    lp = _LP()
    live_mp = FakeMP(queue_status="Needs review")
    bug = FakeBug(tasks=_FIX_COMMITTED, linked_merge_proposals=[live_mp])
    assert checks.check_administrative_state("url", bug, lp, source_package="foo") is False
    assert lp.comments == [] and lp.unsubscribed == 0


def test_closed_task_with_only_a_merged_linked_mp_still_unsubscribes():
    # The MP that actually closed the task shows up as Merged -- not a
    # live review, so it must not block the usual Fix-Committed handling.
    lp = _LP()
    merged_mp = FakeMP(queue_status="Merged")
    bug = FakeBug(tasks=_FIX_COMMITTED, linked_merge_proposals=[merged_mp])
    assert checks.check_administrative_state("url", bug, lp, source_package="foo") is True
    assert lp.unsubscribed == 1


def test_closed_task_with_only_rejected_or_superseded_mps_still_unsubscribes():
    lp = _LP()
    mps = [FakeMP(queue_status="Rejected"), FakeMP(queue_status="Superseded")]
    bug = FakeBug(tasks=_FIX_COMMITTED, linked_merge_proposals=mps)
    assert checks.check_administrative_state("url", bug, lp, source_package="foo") is True
    assert lp.unsubscribed == 1


def test_linked_mp_lookup_failure_is_inconclusive():
    lp = _LP()

    class _BrokenBug(FakeBug):
        @property
        def linked_merge_proposals(self):
            raise TimeoutError("simulated Launchpad timeout")

        @linked_merge_proposals.setter
        def linked_merge_proposals(self, value):
            pass

    bug = _BrokenBug(tasks=_FIX_COMMITTED)
    assert checks.check_administrative_state("url", bug, lp, source_package="foo") is None
    assert lp.comments == [] and lp.unsubscribed == 0
