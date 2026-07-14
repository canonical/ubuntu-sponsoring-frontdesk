"""Design #91: a bug filed less than _NEW_BUG_GRACE ago is skipped -- the
Launchpad 'new bug' form can't set everything a report needs (targeted
series, linked MPs, etc), so submitters commonly finish the report in an
edit or follow-up comment right after filing. Bugs only: an MP's diff/
branch is already complete when it's created. Dry-run bypasses (no writes,
useful to preview); write modes skip and retry next run."""

import datetime

import main
from fakes import FakeBug, FakeLLM, FakeRoot, FakeTask, FakeTriageClient, OLD_ENOUGH

BUG_URL = "https://bugs.launchpad.net/ubuntu/+source/foo/+bug/1"


def _state(tmp_path):
    from state import StateManager

    return StateManager(db_path=str(tmp_path / "state.db"))


def _fresh_bug(minutes_ago=1):
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes_ago
    )
    return FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")], date_created=created
    )


def test_write_mode_skips_a_fresh_bug(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(objects={BUG_URL: _fresh_bug()}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments == []
    assert sm.get_facts(BUG_URL) is None  # nothing persisted, retried next run


def test_dry_run_proceeds_on_a_fresh_bug(tmp_path):
    sm = _state(tmp_path)
    lp = FakeTriageClient(
        objects={BUG_URL: _fresh_bug()},
        lp=FakeRoot(),
        mode="dry-run",
        write_outcome="dry-run",
    )

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    # The pipeline ran to its normal conclusion (no_patch close attempted).
    assert lp.comments
    assert sm.get_facts(BUG_URL) is None  # dry-run never persists facts


def test_old_enough_bug_is_triaged_normally(tmp_path):
    sm = _state(tmp_path)
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")], date_created=OLD_ENOUGH)
    lp = FakeTriageClient(objects={BUG_URL: bug}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments


def test_bug_just_past_the_grace_period_is_triaged(tmp_path):
    sm = _state(tmp_path)
    bug = _fresh_bug(minutes_ago=11)
    lp = FakeTriageClient(objects={BUG_URL: bug}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments


def test_unreadable_creation_date_is_inconclusive(tmp_path):
    class _BrokenBug(FakeBug):
        @property
        def date_created(self):
            raise TimeoutError("simulated Launchpad timeout")

        @date_created.setter
        def date_created(self, value):
            pass

    sm = _state(tmp_path)
    bug = _BrokenBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    lp = FakeTriageClient(objects={BUG_URL: bug}, lp=FakeRoot())

    main.triage_url(BUG_URL, sm, lp, FakeLLM(), force=True)

    assert lp.comments == []
    assert sm.get_facts(BUG_URL) is None


def test_mps_are_not_subject_to_the_grace_period(tmp_path):
    from fakes import FakeMP

    sm = _state(tmp_path)
    mp = FakeMP(
        target="refs/heads/ubuntu/devel"
    )  # FakeMP has no date_created concept at all
    url = "https://code.launchpad.net/~human/+merge/1"
    lp = FakeTriageClient(objects={url: mp}, lp=FakeRoot())

    # Should not raise, and should not be gated by any bug-only grace logic.
    main.triage_url(url, sm, lp, FakeLLM(), force=True)
