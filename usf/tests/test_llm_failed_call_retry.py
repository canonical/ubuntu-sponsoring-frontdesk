"""#169: a failed LLM call makes the item inconclusive -- retried next run.

Before, only Check 7 did this. The SRU template, sync and MP reviews read a
failed call as their fail-safe default ("ready for a human", or silence),
and that was saved like a real result: the facts gate then skipped the item
until its contributor changed something, so the review never happened.
That covers an LLM outage, a timeout, the run budget running out and #166's
refused agent -- under mode=yes, every SRU item in such a pass.
"""

from fakes import CLEAN_DIFF_TEXT, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

import llm_reviewer
import main
from llm_reviewer import LLMReviewer
from state import StateManager

URL = "https://code.launchpad.net/~x/ubuntu/+source/foo/+git/foo/+merge/1"


def _mp():
    return FakeMP(target=".../ubuntu/devel", diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT))


def _triage(tmp_path, llm):
    lp = FakeTriageClient(objects={URL: _mp()})
    sm = StateManager(db_path=str(tmp_path / "s.db"))
    main.triage_url(URL, sm, lp, llm)
    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    return lp, sm, row


# --- main.py ------------------------------------------------------------------


def test_a_failed_llm_review_posts_nothing_and_is_retried(tmp_path):
    llm = FakeLLM(mp_result=("ADVISORY", [("advisory", "A nit.")]))
    llm.failed = ["mp_review"]

    lp, sm, row = _triage(tmp_path, llm)

    assert lp.comments == [] and lp.votes == []
    assert sm.get_facts(URL) is None  # not saved: the next run retries it
    assert row["outcome"] == "inconclusive"
    assert row["extra"]["inconclusive"] == ["llm-failed:mp_review"]


def test_a_completed_llm_review_still_posts_and_is_saved(tmp_path):
    llm = FakeLLM(mp_result=("ADVISORY", [("advisory", "A nit.")]))

    lp, sm, row = _triage(tmp_path, llm)

    assert len(lp.comments) == 1 and "A nit." in lp.comments[0]
    assert sm.get_facts(URL) is not None
    assert row["extra"]["inconclusive"] == []


# --- LLMReviewer: which failures are worth retrying -----------------------------


def _reviewer(monkeypatch, response):
    r = LLMReviewer()
    monkeypatch.setattr(r, "_query_llm", lambda prompt: response)
    r.start_item("u")
    return r


def test_transient_failures_are_retried(monkeypatch):
    for response in (
        "FAIL: LLM invocation timed out.",
        "FAIL: LLM invocation failed internally.",
        "FAIL: LLM run call budget exhausted.",
        "FAIL: LLM agent misconfigured.",
        "FAIL: LLM reply used tools.",
        "FAIL: something new and unknown.",  # unknown -> retried, the safe side
    ):
        r = _reviewer(monkeypatch, response)
        assert r.review_sru_template("d") == (True, "")  # still its fail-safe
        assert r.failed_checks() == ["sru_template"], response


def test_the_per_item_budget_is_not_retried(monkeypatch):
    # It would trip again on the same item every run, forever.
    r = _reviewer(monkeypatch, llm_reviewer._FAIL_ITEM_BUDGET)
    r.review_sru_template("d")
    assert r.failed_checks() == []


def test_missing_opencode_is_not_retried(monkeypatch):
    # The documented checks-only mode: deterministic findings still post.
    r = _reviewer(monkeypatch, llm_reviewer._FAIL_NO_OPENCODE)
    r.review_sru_template("d")
    assert r.failed_checks() == []


def test_an_unparseable_reply_is_not_a_failed_call(monkeypatch):
    # #168: visibility only, decided separately (handover 2C).
    r = _reviewer(monkeypatch, "I think it's fine.")
    r.review_sru_template("d")
    assert r.failed_checks() == []


def test_failures_reset_per_item_and_are_listed_once(monkeypatch):
    r = _reviewer(monkeypatch, "FAIL: LLM invocation timed out.")
    r.review_sru_template("a")
    r.review_sru_template("b")  # an SRU MP asks once per linked bug
    r.review_sync_request("c")
    assert r.failed_checks() == ["sru_template", "sync_delta"]
    r.start_item("v")
    assert r.failed_checks() == []
