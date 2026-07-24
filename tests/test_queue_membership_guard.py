"""Design #76: bugs reach the sponsoring queue only via a direct
~ubuntu-sponsors / ~ubuntu-security-sponsors subscription. A bug without one
(typically a mistaken --url run on the bug an MP links to) is not a
sponsoring request, and the bot must not write to it. Dry-run may proceed --
it performs no writes -- so it stays usable as a "what would the bot do
here" probe."""

from fakes import (
    FakeBug,
    FakeLLM,
    FakeMP,
    FakeRoot,
    FakeSubscription,
    FakeTask,
    FakeTriageClient,
)

import checks
import main
from audit import AuditLog
from launchpad_client import LPClient
from state import StateManager

BUG_URL = "https://bugs.launchpad.net/ubuntu/+source/unity/+bug/2160299"


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


def _non_queue_bug():
    # Open task, no patch, no MP: without the guard this takes the no_patch
    # close (comment + unsubscribe) -- the live #2160299 shape.
    return FakeBug(tasks=[FakeTask("unity (Ubuntu)", "New")], subscriptions=[])


# --- check_sponsoring_team_subscribed (unit) ---------------------------------


def test_sponsors_subscription_passes():
    assert checks.check_sponsoring_team_subscribed("u", FakeBug()) is True


def test_security_sponsors_subscription_passes():
    bug = FakeBug(subscriptions=[FakeSubscription("~ubuntu-security-sponsors")])
    assert checks.check_sponsoring_team_subscribed("u", bug) is True


def test_no_sponsoring_team_fails():
    bug = FakeBug(subscriptions=[FakeSubscription("~some-human"), FakeSubscription("~a-team")])
    assert checks.check_sponsoring_team_subscribed("u", bug) is False


def test_mp_always_passes():
    assert checks.check_sponsoring_team_subscribed("u", FakeMP()) is True


def test_unreadable_subscriptions_are_inconclusive():
    class _BrokenSubsBug(FakeBug):
        @property
        def subscriptions(self):
            raise TimeoutError("simulated Launchpad timeout")

        @subscriptions.setter
        def subscriptions(self, value):
            pass

    assert checks.check_sponsoring_team_subscribed("u", _BrokenSubsBug()) is None


# --- the triage-level guard (end-to-end) --------------------------------------


def test_write_mode_skips_a_non_queue_bug(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(objects={BUG_URL: _non_queue_bug()}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments == []
    assert getattr(lp, "unsubscribed", 0) == 0
    assert sm.get_facts(BUG_URL) is None  # nothing persisted, retried next run


def test_dry_run_proceeds_on_a_non_queue_bug(tmp_path):
    # Dry-run performs no writes, so the guard lets the pipeline run: this is
    # the supported way to probe what the bot would do on an arbitrary bug.
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={BUG_URL: _non_queue_bug()},
        lp=FakeRoot(),
        mode="dry-run",
        write_outcome="dry-run",
    )

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    # The pipeline ran to its normal conclusion (no_patch close attempted).
    assert any("no need" in c or "nothing" in c.lower() for c in lp.comments)
    assert sm.get_facts(BUG_URL) is None  # dry-run never persists facts


def test_queued_bug_is_triaged_normally(tmp_path):
    # Control: the default FakeBug carries the ~ubuntu-sponsors subscription.
    sm = _state(tmp_path)
    bug = FakeBug(tasks=[FakeTask("unity (Ubuntu)", "New")])
    lp = FakeTriageClient(objects={BUG_URL: bug}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments  # the no_patch close fired as before


# --- unsubscribe_sponsors race belt -------------------------------------------


class _CountingBug(FakeBug):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.unsubscribes = 0

    def unsubscribe(self, person=None):
        self.unsubscribes += 1


def _client(tmp_path, mode="yes"):
    audit = AuditLog(path=str(tmp_path / "audit.jsonl"))
    return LPClient(mode=mode, audit=audit, lp=FakeRoot())


def test_unsubscribe_is_a_silent_noop_when_not_subscribed(tmp_path):
    bug = _CountingBug(subscriptions=[])
    _client(tmp_path).unsubscribe_sponsors(bug)
    assert bug.unsubscribes == 0


def test_unsubscribe_still_performs_when_subscribed(tmp_path):
    bug = _CountingBug()
    _client(tmp_path).unsubscribe_sponsors(bug)
    assert bug.unsubscribes == 1
