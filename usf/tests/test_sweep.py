"""Rule B, the stale-bounce sweep (#66): bugs the bot bounced (state.db
WAITING_ON_CONTRIBUTOR) get revisited -- a new usable diff attachment or a
response the LLM judges as addressing the bounce flips the tasks back to
New; silence for 30+ days gets the final comment + unsubscribe."""

import datetime

from fakes import (
    BOT,
    HUMAN,
    FakeAttachment,
    FakeBug,
    FakeBugMessage,
    FakeLLM,
    FakeTask,
    FakeTriageClient,
)

import sweep
from state import StateManager

URL = "https://bugs.launchpad.net/ubuntu/+source/foo/+bug/123"

NOW = datetime.datetime.now(datetime.timezone.utc)
RECENT_BOUNCE = NOW - datetime.timedelta(days=2)
OLD_BOUNCE = NOW - datetime.timedelta(days=40)
AFTER_BOUNCE = NOW - datetime.timedelta(days=1)

DEBDIFF = """\
--- foo-1.0/debian/rules
+++ foo-1.1/debian/rules
@@ -1 +1 @@
-a
+b
"""


def _state(tmp_path, bounce_reason="Please add a changelog entry."):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    sm.update_status(URL, "WAITING_ON_CONTRIBUTOR", "Bounced", bounce_reason=bounce_reason)
    return sm


def _bounced_bug(incomplete_since=RECENT_BOUNCE, attachments=None, messages=None):
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "Incomplete", date_incomplete=incomplete_since)],
        attachments=attachments or [],
    )
    bug.messages = messages or []
    return bug


def _run(tmp_path, bug, llm=None, bounce_reason="Please add a changelog entry."):
    sm = _state(tmp_path, bounce_reason=bounce_reason)
    lp = FakeTriageClient(objects={URL: bug})
    sweep.sweep_bounced_bugs(sm, lp, llm or FakeLLM())
    return sm, lp


def test_mps_are_not_in_the_sweep_population(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    sm.update_status(
        "https://code.launchpad.net/~x/ubuntu/+source/foo/+git/foo/+merge/1",
        "WAITING_ON_CONTRIBUTOR",
        "Bounced",
    )
    sm.update_status(URL, "WAITING_ON_CONTRIBUTOR", "Bounced")
    assert [u for u, _r in sm.bounced_bugs()] == [URL]


def test_no_longer_incomplete_is_left_alone(tmp_path):
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    sm, lp = _run(tmp_path, bug)
    assert lp.comments == []
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_new_diff_attachment_flips_tasks_back_to_new(tmp_path):
    bug = _bounced_bug(
        attachments=[FakeAttachment("v2.debdiff", content=DEBDIFF, date_created=AFTER_BOUNCE)]
    )
    sm, lp = _run(tmp_path, bug)
    assert bug.bug_tasks[0].status == "New"
    assert lp.comments == []  # the flip is silent; re-triage does the talking
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_attachment_from_before_the_bounce_does_not_count(tmp_path):
    bug = _bounced_bug(
        incomplete_since=OLD_BOUNCE,
        attachments=[
            FakeAttachment(
                "v1.debdiff",
                content=DEBDIFF,
                date_created=OLD_BOUNCE - datetime.timedelta(days=1),
            )
        ],
    )
    sm, lp = _run(tmp_path, bug)
    # Old bounce, no new activity: swept.
    assert bug.bug_tasks[0].status == "Incomplete"
    assert sm.get_status(URL)[0] == "DONE"


def test_non_diff_attachment_does_not_flip(tmp_path):
    bug = _bounced_bug(
        attachments=[
            FakeAttachment("crash.log.patch", content="not a diff", date_created=AFTER_BOUNCE)
        ]
    )
    sm, lp = _run(tmp_path, bug)
    assert bug.bug_tasks[0].status == "Incomplete"
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_response_judged_addressed_flips_tasks(tmp_path):
    llm = FakeLLM()
    llm.bounce_addressed = True
    bug = _bounced_bug(messages=[FakeBugMessage(HUMAN, "Done, see the PPA", AFTER_BOUNCE)])
    sm, lp = _run(tmp_path, bug, llm=llm)
    assert bug.bug_tasks[0].status == "New"
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
    assert llm.bounce_queries[0][0] == "Please add a changelog entry."
    assert llm.bounce_queries[0][1] == ["Done, see the PPA"]


def test_response_not_addressed_and_recent_waits(tmp_path):
    bug = _bounced_bug(messages=[FakeBugMessage(HUMAN, "I'll look next week", AFTER_BOUNCE)])
    sm, lp = _run(tmp_path, bug)
    assert bug.bug_tasks[0].status == "Incomplete"
    assert lp.comments == []
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_response_not_addressed_and_old_is_swept(tmp_path):
    bug = _bounced_bug(
        incomplete_since=OLD_BOUNCE,
        messages=[
            FakeBugMessage(HUMAN, "I'll look next week", OLD_BOUNCE + datetime.timedelta(days=1))
        ],
    )
    sm, lp = _run(tmp_path, bug)
    assert len(lp.comments) == 1
    assert "over a month" in lp.comments[0]
    assert "subscribe ~ubuntu-sponsors again" in lp.comments[0]
    assert lp.unsubscribed == 1
    assert sm.get_status(URL)[0] == "DONE"


def test_llm_failure_defers_to_next_run(tmp_path):
    llm = FakeLLM()
    llm.bounce_addressed = None
    bug = _bounced_bug(
        incomplete_since=OLD_BOUNCE,
        messages=[FakeBugMessage(HUMAN, "some reply", AFTER_BOUNCE)],
    )
    sm, lp = _run(tmp_path, bug, llm=llm)
    # Even though the bounce is old, an unjudgeable response must not be
    # swept over -- retry next run.
    assert lp.comments == []
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_bot_comments_do_not_count_as_a_response(tmp_path):
    bug = _bounced_bug(
        incomplete_since=OLD_BOUNCE,
        messages=[FakeBugMessage(BOT, "the bounce comment itself", AFTER_BOUNCE)],
    )
    llm = FakeLLM()
    llm.bounce_addressed = True  # must not even be consulted
    sm, lp = _run(tmp_path, bug, llm=llm)
    assert not hasattr(llm, "bounce_queries")
    assert sm.get_status(URL)[0] == "DONE"


def test_no_activity_and_recent_bounce_waits(tmp_path):
    bug = _bounced_bug(incomplete_since=RECENT_BOUNCE)
    sm, lp = _run(tmp_path, bug)
    assert lp.comments == []
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_no_activity_and_old_bounce_is_swept(tmp_path):
    bug = _bounced_bug(incomplete_since=OLD_BOUNCE)
    sm, lp = _run(tmp_path, bug)
    assert len(lp.comments) == 1
    assert lp.unsubscribed == 1
    assert sm.get_status(URL)[0] == "DONE"


def test_no_stored_bounce_reason_falls_back_to_the_timer(tmp_path):
    llm = FakeLLM()
    llm.bounce_addressed = True  # must not be consulted without a reason
    bug = _bounced_bug(
        incomplete_since=OLD_BOUNCE,
        messages=[FakeBugMessage(HUMAN, "a reply", AFTER_BOUNCE)],
    )
    sm, lp = _run(tmp_path, bug, llm=llm, bounce_reason=None)
    assert not hasattr(llm, "bounce_queries")
    assert sm.get_status(URL)[0] == "DONE"


def test_one_broken_bug_does_not_kill_the_pass(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    broken_url = "https://bugs.launchpad.net/ubuntu/+source/bar/+bug/666"
    sm.update_status(broken_url, "WAITING_ON_CONTRIBUTOR", "Bounced")
    sm.update_status(URL, "WAITING_ON_CONTRIBUTOR", "Bounced")
    bug = _bounced_bug(incomplete_since=OLD_BOUNCE)
    lp = FakeTriageClient(objects={URL: bug})  # broken_url raises KeyError
    sweep.sweep_bounced_bugs(sm, lp, FakeLLM())
    assert sm.get_status(URL)[0] == "DONE"


# --- write-effectiveness gating (external review finding, 2026-07-17) --------
# A sweep write that didn't actually happen (dry-run, declined, error) must
# not advance the stored state -- otherwise the bug drops out of
# bounced_bugs() forever and the action silently never happens.


def _run_with_outcome(tmp_path, bug, outcome, llm=None):
    sm = _state(tmp_path)
    lp = FakeTriageClient(objects={URL: bug}, write_outcome=outcome)
    sweep.sweep_bounced_bugs(sm, lp, llm or FakeLLM())
    return sm, lp


def test_dry_run_diff_flip_does_not_advance_state(tmp_path):
    bug = _bounced_bug(
        attachments=[FakeAttachment("v2.debdiff", content=DEBDIFF, date_created=AFTER_BOUNCE)]
    )
    sm, lp = _run_with_outcome(tmp_path, bug, "dry-run")
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"
    assert [u for u, _r in sm.bounced_bugs()] == [URL]  # still swept next run


def test_declined_response_flip_does_not_advance_state(tmp_path):
    llm = FakeLLM()
    llm.bounce_addressed = True
    bug = _bounced_bug(messages=[FakeBugMessage(HUMAN, "Done, see the PPA", AFTER_BOUNCE)])
    sm, lp = _run_with_outcome(tmp_path, bug, "declined", llm=llm)
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_errored_sweep_close_does_not_advance_state(tmp_path):
    bug = _bounced_bug(incomplete_since=OLD_BOUNCE)
    sm, lp = _run_with_outcome(tmp_path, bug, "error")
    assert len(lp.comments) == 1  # the attempt happened...
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"  # ...but state waits


def test_effective_writes_still_advance_state(tmp_path):
    bug = _bounced_bug(incomplete_since=OLD_BOUNCE)
    sm, lp = _run_with_outcome(tmp_path, bug, "performed")
    assert sm.get_status(URL)[0] == "DONE"


def test_stale_outcome_from_a_previous_item_does_not_block_the_sweep(tmp_path):
    # start_item() must reset write tracking per swept bug: a failed write
    # left over from the preceding queue item is not this bug's problem.
    sm = _state(tmp_path)
    bug = _bounced_bug(incomplete_since=OLD_BOUNCE)
    lp = FakeTriageClient(objects={URL: bug})
    lp.write_outcomes = ["error"]  # residue from an earlier item
    sweep.sweep_bounced_bugs(sm, lp, FakeLLM())
    assert sm.get_status(URL)[0] == "DONE"
