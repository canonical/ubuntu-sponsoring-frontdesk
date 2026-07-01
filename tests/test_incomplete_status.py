"""Bouncing a bug as INCOMPLETE sets all open Ubuntu tasks to Incomplete,
keeps ~ubuntu-sponsors subscribed, and does not re-triage its own status write."""

import facts
import main
from launchpad_client import LPClient
from state import StateManager
from fakes import FakeBug, FakeTask, FakeRoot, FakeAudit, FakeTriageClient, FakeLLM

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


def test_dry_run_changes_nothing():
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    changed = _client("dry-run").set_bug_tasks_incomplete(bug)
    assert changed == {}
    assert bug.bug_tasks[0].status == "New"


# --- end-to-end via main.triage_url -----------------------------------------


def test_bounce_sets_incomplete_keeps_sponsors_and_is_quiet_next_run(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")], description="sync me")
    lp = FakeTriageClient(objects={URL: bug})
    llm = FakeLLM(bug_result=("INCOMPLETE", "Please explain the Ubuntu delta."))

    main.triage_url(URL, sm, lp, llm)

    assert lp.comments == ["Please explain the Ubuntu delta."]
    assert bug.bug_tasks[0].status == "Incomplete"  # set Incomplete
    assert getattr(lp, "unsubscribed", 0) == 0  # sponsors kept
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"

    # Re-run: the only change is the bot's own status write -> facts match ->
    # no re-triage, no second comment.
    main.triage_url(URL, sm, lp, llm)
    assert lp.comments == ["Please explain the Ubuntu delta."]
