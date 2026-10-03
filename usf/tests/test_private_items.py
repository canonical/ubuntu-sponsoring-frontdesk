"""Design #103: private bugs/MPs are never processed -- triage would ship
their content to a third-party LLM provider and into DEBUG logs, an
audience Launchpad's ACLs never granted. Moot while the bot's account
holds no privileges, but the guard survives a future privilege change.
Nothing is persisted, so the item triages normally once public."""

import datetime

from fakes import FakeBug, FakeLLM, FakeMP, FakeTask, FakeTriageClient

import main
import sweep
from state import StateManager

BUG_URL = "https://bugs.launchpad.net/ubuntu/+source/foo/+bug/99"
MP_URL = "https://code.launchpad.net/~x/ubuntu/+source/foo/+git/foo/+merge/99"

OLD_BOUNCE = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)


class _NeverCalledLLM(FakeLLM):
    def triage_bug(self, obj):
        raise AssertionError("the LLM must not see a private item")

    def triage_mp(self, obj, diff_text=None):
        raise AssertionError("the LLM must not see a private item")

    def review_bounce_response(self, bounce_reason, comments):
        raise AssertionError("the LLM must not see a private item")


def test_private_bug_is_skipped_entirely(tmp_path):
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    bug.private = True
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    lp = FakeTriageClient(objects={BUG_URL: bug})
    main.triage_url(BUG_URL, sm, lp, _NeverCalledLLM())
    assert lp.comments == []
    assert sm.get_status(BUG_URL) is None
    assert sm.get_facts(BUG_URL) is None  # re-examined every pass


def test_private_mp_is_skipped_entirely(tmp_path):
    mp = FakeMP()
    mp.private = True
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    lp = FakeTriageClient(objects={MP_URL: mp})
    main.triage_url(MP_URL, sm, lp, _NeverCalledLLM())
    assert lp.comments == []
    assert sm.get_status(MP_URL) is None


def test_public_bug_is_unaffected(tmp_path):
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "New")])
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    lp = FakeTriageClient(objects={BUG_URL: bug})
    main.triage_url(BUG_URL, sm, lp, FakeLLM())
    # The pipeline ran: some terminal state was recorded.
    assert sm.get_status(BUG_URL) is not None


def test_private_bug_is_not_swept(tmp_path):
    bug = FakeBug(tasks=[FakeTask("foo (Ubuntu)", "Incomplete", date_incomplete=OLD_BOUNCE)])
    bug.private = True
    bug.messages = []
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    sm.update_status(BUG_URL, "WAITING_ON_CONTRIBUTOR", "Bounced", bounce_reason="x")
    lp = FakeTriageClient(objects={BUG_URL: bug})
    sweep.sweep_bounced_bugs(sm, lp, _NeverCalledLLM())
    assert lp.comments == []
    assert sm.get_status(BUG_URL)[0] == "WAITING_ON_CONTRIBUTOR"
