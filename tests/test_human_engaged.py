"""Design #35: once a human reviewer commented on the current revision,
suppress the question-tier findings (the bot is for early feedback, not for
talking over an ongoing review). Closing-tier outcomes are never suppressed,
and suppression never skips evaluation -- it only silences the aggregated
comment at the very end of the pass. #94: blocking (tier="incomplete")
findings are also never suppressed -- they're facts about archive/technical
state (e.g. a version collision), not a review opinion."""

import datetime

import checks
import main
from state import StateManager
from fakes import (
    BOT,
    CLEAN_DIFF_TEXT,
    HUMAN,
    FakeAttachment,
    FakeBug,
    FakeBugMessage,
    FakeDiff,
    FakeLLM,
    FakeMP,
    FakeMPComment,
    FakeRoot,
    FakeTask,
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


def test_service_account_comments_do_not_count():
    # A known automated account (checks.SERVICE_ACCOUNTS) must never silence
    # the bot -- notably ~ubuntu-sponsoring-bot itself, whose pre-account-
    # switch comments won't match lp.me once the bot moves off seb128's
    # personal account.
    comments = [
        FakeMPComment(
            f"https://api.launchpad.net/devel/{acct}", "automated noise", AFTER_DIFF
        )
        for acct in checks.SERVICE_ACCOUNTS
    ]
    mp = _mp(comments, diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE))
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


def test_submitter_reply_after_reviewer_resumes_review():
    # #96: the reviewer asked for something, the submitter answered on the
    # same diff -- the ball is back with the queue, so the bot should look
    # again rather than stay suppressed forever.
    mp = _mp(
        [
            FakeMPComment(REVIEWER, "please fix X", AFTER_DIFF),
            FakeMPComment(
                HUMAN, "done, PTAL", AFTER_DIFF + datetime.timedelta(hours=1)
            ),
        ],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is False


def test_reviewer_reply_after_submitter_stays_engaged():
    # The submitter answered, but the reviewer came back afterward -- still
    # their conversation, still engaged.
    mp = _mp(
        [
            FakeMPComment(REVIEWER, "please fix X", AFTER_DIFF),
            FakeMPComment(
                HUMAN, "done, PTAL", AFTER_DIFF + datetime.timedelta(hours=1)
            ),
            FakeMPComment(
                REVIEWER, "still not right", AFTER_DIFF + datetime.timedelta(hours=2)
            ),
        ],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
    )
    assert checks.check_human_engaged(mp, _client({URL: mp})) is True


def test_undated_comment_falls_back_to_any_qualifying_comment_counts():
    # A comment without a timestamp can't be ranked against the rest --
    # conservatively treat that as still engaged, same as the pre-#96 rule.
    mp = _mp(
        [
            FakeMPComment(
                HUMAN, "done, PTAL", AFTER_DIFF + datetime.timedelta(hours=1)
            ),
            FakeMPComment(REVIEWER, "please fix X", None),
        ],
        diff=FakeDiff("/d/1", 50, date_created=DIFF_DATE),
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


# --- check_human_engaged on bugs (#72) ------------------------------------------


ATTACH_DATE = DIFF_DATE  # the newest usable diff attachment is the anchor
DEBDIFF = "--- foo-1.0/debian/rules\n+++ foo-1.1/debian/rules\n@@ -1 +1 @@\n-a\n+b\n"


def _bug(messages, bug_attachments=None):
    bug = FakeBug(attachments=bug_attachments or [])
    bug.messages = messages
    return bug


def test_bug_reviewer_comment_counts():
    # The concrete trigger (security bug #2069291): a sponsor working out
    # the fix in the comments -- no attachment, so any qualifying comment.
    bug = _bug([FakeBugMessage(REVIEWER, "here are the patches to backport")])
    assert checks.check_human_engaged(bug, _client({URL: bug})) is True


def test_bug_reporter_and_service_comments_do_not_count():
    bug = _bug(
        [
            FakeBugMessage(HUMAN, "I updated my PPA"),  # the reporter
            FakeBugMessage(BOT, "Thanks for your contribution! ..."),
        ]
    )
    assert checks.check_human_engaged(bug, _client({URL: bug})) is False


def test_bug_comment_predating_the_current_attachment_does_not_count():
    # A new debdiff resets the conversation, like a new push on an MP.
    bug = _bug(
        [FakeBugMessage(REVIEWER, "old feedback", BEFORE_DIFF)],
        bug_attachments=[
            FakeAttachment("v2.debdiff", content=DEBDIFF, date_created=ATTACH_DATE)
        ],
    )
    assert checks.check_human_engaged(bug, _client({URL: bug})) is False


def test_bug_comment_after_the_current_attachment_counts():
    bug = _bug(
        [FakeBugMessage(REVIEWER, "reviewed the debdiff, one issue", AFTER_DIFF)],
        bug_attachments=[
            FakeAttachment("v2.debdiff", content=DEBDIFF, date_created=ATTACH_DATE)
        ],
    )
    assert checks.check_human_engaged(bug, _client({URL: bug})) is True


def test_bug_reporter_reply_after_reviewer_resumes_review():
    # #96, the live trigger (ghostty bug #2155110): a reviewer asked for an
    # SRU template, the reporter added one afterward -- resume, don't stay
    # suppressed.
    bug = _bug(
        [
            FakeBugMessage(REVIEWER, "needs a SRU template", AFTER_DIFF),
            FakeBugMessage(
                HUMAN, "added the template", AFTER_DIFF + datetime.timedelta(hours=1)
            ),
        ]
    )
    assert checks.check_human_engaged(bug, _client({URL: bug})) is False


# --- end-to-end ---------------------------------------------------------------


def test_engaged_reviewer_does_not_suppress_a_blocking_finding(tmp_path):
    # Conflicts (tier="incomplete") is a fact about the branch's mergeable
    # state, not a review opinion -- a reviewer already commenting doesn't
    # make it go away, so it still posts and votes despite engagement (#94).
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

    assert len(lp.comments) == 1
    assert "conflict" in lp.comments[0].lower()
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_engaged_reviewer_does_not_suppress_a_blocking_bug_bounce(tmp_path):
    # Bug-side #72/#94: a plain code patch bounces (Check 10, tier=
    # "incomplete") even though a reviewer commented after the attachment --
    # a sponsor still can't turn a plain patch into an upload themselves.
    sm = _state(tmp_path)
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "New")],
        description="fix attached",
        attachments=[
            FakeAttachment("fix.patch", type="Patch", date_created=ATTACH_DATE)
        ],
    )
    bug.messages = [
        FakeBugMessage(REVIEWER, "patch looks right, needs a debdiff", AFTER_DIFF)
    ]
    bug_url = "https://launchpad.net/bugs/42"
    lp = _client({bug_url: bug})

    main.triage_url(bug_url, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert bug.bug_tasks[0].status == "Incomplete"
    assert sm.get_status(bug_url)[0] == "WAITING_ON_CONTRIBUTOR"


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


# --- #94: blocking findings survive engagement --------------------------------


def test_blocking_finding_still_bounces_despite_engaged_reviewer(monkeypatch, tmp_path):
    # A version collision (check_stale_version, tier="incomplete") is a fact
    # about archive state, not a review opinion -- a reviewer's comment
    # doesn't resolve it, so it must still post and vote Needs Fixing.
    monkeypatch.setattr(
        checks,
        "check_stale_version",
        lambda url, lp_obj, lp_client: checks.Finding(
            "incomplete",
            "An upload with the same version but different content already "
            "exists in the archive. Your change needs to be rebased.",
            kind="advisory",
        ),
    )
    sm = _state(tmp_path)
    mp = _mp(
        [FakeMPComment(REVIEWER, "fix the conflict and I'll sponsor", AFTER_DIFF)],
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT, date_created=DIFF_DATE),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "rebased" in lp.comments[0]
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


def test_mixed_findings_keep_blocking_and_drop_question_when_engaged(
    monkeypatch, tmp_path
):
    # Both a blocking and a non-blocking finding fire; engagement should
    # only silence the non-blocking one.
    monkeypatch.setattr(
        checks,
        "check_stale_version",
        lambda url, lp_obj, lp_client: checks.Finding(
            "incomplete", "needs a rebase", kind="advisory"
        ),
    )
    monkeypatch.setattr(
        checks,
        "check_ppa_version_suffix",
        lambda url, lp_obj, lp_client: checks.Finding(
            "question", "the version contains a stray ~ppa suffix", kind="advisory"
        ),
    )
    sm = _state(tmp_path)
    mp = _mp(
        [FakeMPComment(REVIEWER, "looking at this", AFTER_DIFF)],
        target=".../ubuntu/devel",
        diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT, date_created=DIFF_DATE),
    )
    lp = _client({URL: mp})

    main.triage_url(URL, sm, lp, FakeLLM())

    assert len(lp.comments) == 1
    assert "rebase" in lp.comments[0]
    assert "ppa" not in lp.comments[0].lower()
    assert lp.votes == ["Needs Fixing"]
