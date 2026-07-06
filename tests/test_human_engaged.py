"""Design #35: once a human reviewer commented on the current revision,
suppress the incomplete/question-tier findings (the bot is for early feedback,
not for talking over an ongoing review). Closing-tier outcomes are never
suppressed, and suppression never skips evaluation -- it only silences the
aggregated comment at the very end of the pass."""

import datetime

import checks
import main
from state import StateManager
from fakes import (
    BOT,
    CLEAN_DIFF_TEXT,
    HUMAN,
    FakeDiff,
    FakeLLM,
    FakeMP,
    FakeMPComment,
    FakeRoot,
    FakeTriageClient,
)

URL = "https://code.launchpad.net/~marco/+merge/12345"
REVIEWER = "https://api.launchpad.net/devel/~a-reviewer"

DIFF_DATE = datetime.datetime(2026, 7, 1, 12, 0)
BEFORE_DIFF = datetime.datetime(2026, 6, 30, 12, 0)
AFTER_DIFF = datetime.datetime(2026, 7, 2, 12, 0)


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


def _client(objects):
    # FakeRoot supplies lp.me so the bot's own comments are recognized.
    return FakeTriageClient(objects=objects, lp=FakeRoot())


def _mp(comments, **kwargs):
    mp = FakeMP(**kwargs)
    mp.all_comments = comments
    return mp


# --- check_human_engaged (unit) -----------------------------------------------


def test_reviewer_comment_after_current_diff_counts():
    mp = _mp(
        [FakeMPComment(REVIEWER, "looks off, please fix X", AFTER_DIFF)],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is True


def test_submitter_and_bot_comments_do_not_count():
    mp = _mp(
        [
            FakeMPComment(HUMAN, "updated, please review", AFTER_DIFF),
            FakeMPComment(BOT, "Thanks for your contribution! ...", AFTER_DIFF),
        ],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is False


def test_reviewer_comment_on_an_older_push_does_not_count():
    # A fresh push generates a new diff with a new timestamp; a stale comment
    # on the previous revision must not suppress the bot forever.
    mp = _mp(
        [FakeMPComment(REVIEWER, "old feedback", BEFORE_DIFF)],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is False


def test_missing_diff_timestamp_still_counts_reviewer_comment():
    # No anchor to scope by: err toward staying quiet.
    mp = _mp(
        [FakeMPComment(REVIEWER, "feedback", AFTER_DIFF)],
        diff=FakeDiff("/d/1", 50),  # date_created=None
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is True


def test_unreadable_comment_history_is_inconclusive():
    class _BrokenCommentsMP(FakeMP):
        @property
        def all_comments(self):
            raise TimeoutError("simulated Launchpad timeout")

        @all_comments.setter
        def all_comments(self, value):
            pass

    mp = _BrokenCommentsMP(diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE))
    assert checks.check_human_engaged(mp, _client({URL: mp})) is None


def test_bugs_are_never_suppressed_for_now():
    # Bug-side "since current submission" anchor is an open #35 question;
    # until decided, bugs always report not-engaged.
    from fakes import FakeBug

    bug = FakeBug()
    assert checks.check_human_engaged(bug, _client({URL: bug})) is False


# --- end-to-end ---------------------------------------------------------------


def test_engaged_reviewer_suppresses_the_aggregate(tmp_path):
    # Two findings fire (wrong target branch + conflicts), but a reviewer
    # already commented on the current revision: nothing posts, no vote, and
    # facts persist so the item is skipped until a new push changes the diff.
    sm = _state(tmp_path)
    mp = _mp(
        [FakeMPComment(REVIEWER, "fix the conflict and I'll sponsor", AFTER_DIFF)],
        target=".../ubuntu/devel",
        diff=FakeDiff(
            "/d/1",
            50,
            conflicts="foo.c",
            diff_text=CLEAN_DIFF_TEXT,
            date_created=DIFF_DATE,
        ),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert lp.comments == []
    assert lp.votes == []
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"
    assert "suppressed" in sm.get_status(URL)[1]
    assert sm.get_facts(URL) is not None


def test_closing_outcome_still_fires_despite_engaged_reviewer(tmp_path):
    # The #35 exemption: "looks good, uploading" is the common path to an MP
    # being resolved -- closing-tier outcomes (here: empty diff, the change
    # already landed) must always act regardless of who commented.
    sm = _state(tmp_path)
    mp = _mp(
        [FakeMPComment(REVIEWER, "looks good, uploading", AFTER_DIFF)],
        diff=FakeDiff("/d/1", 0, diff_text=CLEAN_DIFF_TEXT, date_created=DIFF_DATE),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "can be closed" in lp.comments[0]
    assert sm.get_status(URL)[0] == "DONE"


def test_unreadable_history_posts_nothing_and_persists_nothing(tmp_path):
    # Engagement unknown (comment fetch failed): per the #28/#31 conventions,
    # don't act either way -- no aggregate, no facts, retry next run.
    sm = _state(tmp_path)

    class _BrokenCommentsMP(FakeMP):
        @property
        def all_comments(self):
            raise TimeoutError("simulated Launchpad timeout")

        @all_comments.setter
        def all_comments(self, value):
            pass

    mp = _BrokenCommentsMP(
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT, date_created=DIFF_DATE),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert lp.comments == []
    assert sm.get_status(URL) is None
    assert sm.get_facts(URL) is None


def test_no_reviewer_comment_still_bounces_normally(tmp_path):
    # Control: submitter-only thread doesn't suppress anything.
    sm = _state(tmp_path)
    mp = _mp(
        [FakeMPComment(HUMAN, "please review this", AFTER_DIFF)],
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT, date_created=DIFF_DATE),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "should target" in lp.comments[0]
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"
