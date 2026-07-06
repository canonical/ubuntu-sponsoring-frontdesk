"""SYNCED status: the archive check found the sync already landed. Sets the matching
Ubuntu task(s) to Fix Released, comments, unsubscribes sponsors, and folds
the write into facts so it doesn't re-trigger next run."""

import main
from launchpad_client import LPClient
from state import StateManager
from fakes import FakeBug, FakeTask, FakeRoot, FakeAudit, FakeTriageClient, FakeLLM

URL = "https://launchpad.net/bugs/99"


def _client(mode, devel_series_name="noble"):
    return LPClient(
        mode=mode, lp=FakeRoot(devel_series_name=devel_series_name), audit=FakeAudit()
    )


# --- LPClient.set_bug_tasks_fix_released -------------------------------------


def test_sets_untargeted_and_devel_tasks_only():
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "New"),  # -> Fix Released
            FakeTask("foo (Ubuntu Noble)", "Confirmed"),  # devel series -> Fix Released
            FakeTask("foo (Ubuntu Jammy)", "Confirmed"),  # SRU series -> left alone
            FakeTask("foo (Debian)", "New"),  # not Ubuntu -> skip
        ]
    )
    changed = _client("yes").set_bug_tasks_fix_released(bug)

    assert changed == {
        "foo (Ubuntu)": "Fix Released",
        "foo (Ubuntu Noble)": "Fix Released",
    }
    assert [t.status for t in bug.bug_tasks] == [
        "Fix Released",
        "Fix Released",
        "Confirmed",
        "New",
    ]


def test_skips_terminal_but_flips_incomplete():
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "Fix Committed"),  # terminal -> skip
            FakeTask("foo (Ubuntu Noble)", "Incomplete"),  # previously bounced -> flip
        ]
    )
    changed = _client("yes").set_bug_tasks_fix_released(bug)
    assert changed == {"foo (Ubuntu Noble)": "Fix Released"}


def test_dry_run_changes_nothing():
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    changed = _client("dry-run").set_bug_tasks_fix_released(bug)
    assert changed == {}
    assert bug.bug_tasks[0].status == "New"


def test_falls_back_to_untargeted_only_when_devel_series_unknown():
    lp = FakeRoot()
    del lp.distributions  # simulate lookup failure
    client = LPClient(mode="yes", lp=lp, audit=FakeAudit())
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "New"),
            FakeTask("foo (Ubuntu Noble)", "New"),
        ]
    )
    changed = client.set_bug_tasks_fix_released(bug)
    assert changed == {"foo (Ubuntu)": "Fix Released"}


# --- end-to-end via main.triage_url ------------------------------------------


def test_synced_bug_closed_end_to_end(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")],
        # Realistic sync-request title/body: sync requests carry no patch,
        # so they must match the sync detection to get past
        # check_nothing_to_sponsor.
        description="Please sync foo from Debian.",
        title="Sync foo 1.2-3 (main) from Debian unstable",
    )
    lp = FakeTriageClient(objects={URL: bug})
    llm = FakeLLM(bug_result=("SYNCED", "Already synced, closing."))

    main.triage_url(URL, sm, lp, llm)

    assert lp.comments == ["Already synced, closing."]
    assert bug.bug_tasks[0].status == "Fix Released"
    assert getattr(lp, "unsubscribed", 0) == 1
    assert sm.get_status(URL)[0] == "DONE"

    # Re-run: facts (task status + description) unchanged by a contributor ->
    # no re-triage, no duplicate comment.
    main.triage_url(URL, sm, lp, llm)
    assert lp.comments == ["Already synced, closing."]
