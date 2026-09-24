"""Bouncing a bug as INCOMPLETE sets all open Ubuntu tasks to Incomplete,
keeps ~ubuntu-sponsors subscribed, and does not re-triage its own status write."""

from fakes import (
    FakeAttachment,
    FakeAudit,
    FakeBug,
    FakeLLM,
    FakeRoot,
    FakeTask,
    FakeTriageClient,
)

import facts
import main
from launchpad_client import LPClient
from state import StateManager

URL = "https://launchpad.net/bugs/42"


# --- facts.apply_task_status_changes (pure) ---------------------------------


def test_apply_status_changes_updates_only_named_targets():
    snap = {"task_statuses": ["foo (Ubuntu):New", "foo (Ubuntu Jammy):Confirmed"]}
    out = facts.apply_task_status_changes(snap, {"foo (Ubuntu)": "Incomplete"})
    assert out["task_statuses"] == [
        "foo (Ubuntu Jammy):Confirmed",
        "foo (Ubuntu):Incomplete",
    ]
    # original snapshot is not mutated
    assert snap["task_statuses"][0] == "foo (Ubuntu):New"


def test_apply_status_changes_noop_without_changes():
    snap = {"task_statuses": ["x (Ubuntu):New"]}
    assert facts.apply_task_status_changes(snap, {}) is snap


# --- LPClient.set_bug_tasks_incomplete --------------------------------------


def _client(mode):
    return LPClient(mode=mode, lp=FakeRoot(), audit=FakeAudit())


def test_sets_open_ubuntu_tasks_and_skips_others():
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "New"),  # -> Incomplete
            FakeTask("foo (Ubuntu Jammy)", "Confirmed"),  # -> Incomplete
            FakeTask("foo (Debian)", "New"),  # not Ubuntu -> skip
            FakeTask("foo (Ubuntu Focal)", "Fix Released"),  # resolved -> skip
        ]
    )
    changed = _client("yes").set_bug_tasks_incomplete(bug)

    assert changed == {"foo (Ubuntu)": "Incomplete", "foo (Ubuntu Jammy)": "Incomplete"}
    assert [t.status for t in bug.bug_tasks] == [
        "Incomplete",
        "Incomplete",
        "New",
        "Fix Released",
    ]
    # #92: the real write is `task.status = X; task.lp_save()`, not a
    # transitionToStatus() named operation (which was never actually
    # available on a bug task -- found live, alsa-lib bug #2159614).
    assert [t.saved for t in bug.bug_tasks] == [1, 1, 0, 0]


def test_dry_run_changes_nothing():
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    changed = _client("dry-run").set_bug_tasks_incomplete(bug)
    assert changed == {}
    assert bug.bug_tasks[0].status == "New"


# --- end-to-end via main.triage_url -----------------------------------------


def test_bounce_sets_incomplete_keeps_sponsors_and_is_quiet_next_run(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    # A patch must be attached: a bug with nothing to sponsor is closed by
    # check_nothing_to_sponsor before the LLM ever runs.
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")],
        description="sync me",
        attachments=[FakeAttachment("fix.debdiff", type="Patch")],
    )
    lp = FakeTriageClient(objects={URL: bug})
    llm = FakeLLM(bug_result=("INCOMPLETE", "Please explain the Ubuntu delta."))

    main.triage_url(URL, sm, lp, llm)

    # The LLM's INCOMPLETE verdict is one finding in the aggregated review
    # comment (design #31), not a raw standalone comment anymore.
    assert len(lp.comments) == 1
    assert "Please explain the Ubuntu delta." in lp.comments[0]
    assert "Needs fixing before this can be sponsored:" in lp.comments[0]
    assert lp.votes == [None]  # votes exist on MPs only, not bugs
    assert bug.bug_tasks[0].status == "Incomplete"  # set Incomplete
    assert getattr(lp, "unsubscribed", 0) == 0  # sponsors kept
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"

    # Re-run: the only change is the bot's own status write -> facts match ->
    # no re-triage, no second comment.
    main.triage_url(URL, sm, lp, llm)
    assert len(lp.comments) == 1


def test_deterministic_bug_bounce_also_sets_incomplete(tmp_path):
    # Found live on bug #2145103 (#65's same-version-collision bounce): only
    # the LLM's INCOMPLETE verdict used to set the task status; a blocking
    # finding from a deterministic check posted the comment but left the
    # tasks open. Any blocking finding on a bug must set Incomplete -- Rule
    # B's sweep clock (date_incomplete) depends on it too.
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    # The default FakeAttachment content is a plain code patch: Check 10
    # bounces it (incomplete tier) with the LLM staying at READY_FOR_HUMAN.
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")],
        description="fix attached",
        attachments=[FakeAttachment("fix.patch", type="Patch")],
    )
    lp = FakeTriageClient(objects={URL: bug})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "Needs fixing before this can be sponsored:" in lp.comments[0]
    # The bug variant of the closing line: says the status is being set to
    # Incomplete and how to re-enter the queue (set it back to New).
    assert "set it back to New" in lp.comments[0]
    assert bug.bug_tasks[0].status == "Incomplete"
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


# --- #127: scope the bounce to the reviewed debdiff's series -------------------


def _debdiff(suite):
    return f"""\
diff -Nru foo-1.2/debian/changelog foo-1.2/debian/changelog
--- foo-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ foo-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,7 @@
+foo (1.2-3ubuntu0.1) {suite}; urgency=medium
+
+  * Fix things.
+
+ -- Dev <dev@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 foo (1.2-3) {suite}; urgency=medium
"""


class _LPDevel:
    def __init__(self, devel="stonking"):
        import types

        self.lp = types.SimpleNamespace(
            distributions={
                "ubuntu": types.SimpleNamespace(current_series=types.SimpleNamespace(name=devel))
            }
        )


def _scoping_bug(suite):
    from fakes import FakeAttachment, FakeBug, FakeTask

    import attachments

    attachments.reset_cache()
    return FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "In Progress"),
            FakeTask("foo (Ubuntu Resolute)", "In Progress"),
        ],
        attachments=[FakeAttachment("foo.debdiff", type="Patch", content=_debdiff(suite))],
    )


def test_devel_debdiff_scopes_the_bounce_to_the_plain_task():
    # Live (swift bug #2156199): a stonking version conflict must not
    # bounce the resolute task.
    import checks

    bug = _scoping_bug("stonking")
    assert checks.bounce_task_targets(bug, _LPDevel()) == {"foo (Ubuntu)"}


def test_series_debdiff_scopes_the_bounce_to_that_series_task():
    import checks

    bug = _scoping_bug("resolute")
    assert checks.bounce_task_targets(bug, _LPDevel()) == {"foo (Ubuntu Resolute)"}


def test_no_usable_attachment_bounces_every_task_as_before():
    from fakes import FakeBug, FakeTask

    import checks

    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "In Progress")])
    assert checks.bounce_task_targets(bug, _LPDevel()) is None


def test_unmatched_suite_bounces_every_task_as_before():
    # A debdiff for a series with no task of its own: scoping would skip
    # the bounce entirely, so fall back to all open tasks.
    import checks

    bug = _scoping_bug("jammy")
    assert checks.bounce_task_targets(bug, _LPDevel()) is None
