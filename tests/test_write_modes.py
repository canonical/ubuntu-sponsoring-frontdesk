"""Fix #3 + #4 + #6: write-mode/TTY gating, comment dedup, and audit outcomes."""

import json

from audit import AuditLog
from launchpad_client import LPClient, FOOTNOTE
from fakes import FakeRoot, FakeBug, FakeBugMessage, FakeMP, FakeMPComment, BOT, HUMAN


def _client(tmp_path, mode):
    audit = AuditLog(path=str(tmp_path / "audit.jsonl"))
    return LPClient(mode=mode, audit=audit, lp=FakeRoot())


def _entries(client):
    with open(client.audit.path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---- write-mode gating -------------------------------------------------


def test_dry_run_does_not_write_and_is_audited(tmp_path):
    c = _client(tmp_path, "dry-run")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.new_messages == []  # nothing written
    assert _entries(c)[-1]["outcome"] == "dry-run"


def test_yes_writes_and_is_audited(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.new_messages == [f"hello\n\n{FOOTNOTE}"]
    assert _entries(c)[-1]["outcome"] == "performed"


def test_interactive_without_tty_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    c = _client(tmp_path, "interactive")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.new_messages == []
    assert _entries(c)[-1]["outcome"] == "no-tty"


def test_interactive_with_tty_and_yes_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    c = _client(tmp_path, "interactive")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.new_messages == [f"hello\n\n{FOOTNOTE}"]


# ---- footnote (design #57) ---------------------------------------------


def test_every_comment_carries_the_footnote(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    c.comment(bug, "Some review outcome.")
    assert bug.new_messages[-1].endswith(FOOTNOTE)
    assert "automated" in FOOTNOTE
    assert "https://github.com/canonical/ubuntu-sponsoring-frontdesk/issues" in FOOTNOTE


# ---- dedup against Launchpad as source of truth ------------------------


def test_identical_bot_comment_is_skipped(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    bug.messages = [
        FakeBugMessage(BOT, f"Please retarget to debian/sid\n\n{FOOTNOTE}")
    ]
    c.comment(bug, "Please retarget to debian/sid")
    assert bug.new_messages == []  # suppressed
    assert _entries(c)[-1]["outcome"] == "skipped-duplicate"


def test_different_bot_comment_still_posts(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    bug.messages = [FakeBugMessage(BOT, "Please retarget to debian/sid")]
    c.comment(bug, "Now it has conflicts, please rebase")
    assert bug.new_messages == [f"Now it has conflicts, please rebase\n\n{FOOTNOTE}"]


def test_matching_text_from_human_is_not_treated_as_ours(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    bug.messages = [
        FakeBugMessage(HUMAN, f"Please retarget to debian/sid\n\n{FOOTNOTE}")
    ]
    c.comment(bug, "Please retarget to debian/sid")
    assert bug.new_messages == [f"Please retarget to debian/sid\n\n{FOOTNOTE}"]


def test_mp_identical_comment_is_skipped(tmp_path):
    c = _client(tmp_path, "yes")
    mp = FakeMP()
    mp.all_comments = [FakeMPComment(BOT, f"This MP has conflicts\n\n{FOOTNOTE}")]
    c.comment(mp, "This MP has conflicts")
    assert mp.created_comments == []


class _UnreadableHistory:
    """A comment-history read failing mid-iteration (network blip)."""

    def __iter__(self):
        raise TimeoutError("simulated Launchpad timeout")


def test_unreadable_history_skips_the_write_even_under_yes(tmp_path):
    # Never write on an incomplete picture (design_journal.md #28's
    # principle applied to dedup): if the history can't be read, skip in
    # every mode and retry next run rather than risk a double-post.
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    bug.messages = _UnreadableHistory()
    c.comment(bug, "hello")
    assert bug.new_messages == []
    assert _entries(c)[-1]["outcome"] == "skipped-dedup-unavailable"
    # not an effective outcome -> main won't persist facts -> retried
    assert not c.all_writes_effective()


# ---- MP review votes (git-ubuntu MPs don't accept direct status writes) ----


def test_mp_comment_with_vote_is_passed_to_createComment(tmp_path):
    c = _client(tmp_path, "yes")
    mp = FakeMP()
    c.comment(mp, "Please rebase and resolve conflicts.", vote="Needs Fixing")
    assert mp.created_comments == [
        f"Please rebase and resolve conflicts.\n\n{FOOTNOTE}"
    ]
    assert mp.created_votes == ["Needs Fixing"]


def test_mp_comment_without_vote_passes_none(tmp_path):
    c = _client(tmp_path, "yes")
    mp = FakeMP()
    c.comment(mp, "Just a heads-up, no vote attached.")
    assert mp.created_votes == [None]


# ---- bot self-subscribes when it comments on a bug (#88) -------------------
# seb128: "so I can see how sponsors/reporters react to the bot activity" --
# every bug comment, any tier, subscribes the bot's own account. MPs are
# unaffected: a review vote already adds a review slot/notification.


def test_bug_comment_subscribes_the_bot(tmp_path):
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.subscribed_by == [c.lp.me]


def test_mp_comment_does_not_touch_subscriptions(tmp_path):
    # FakeMP has no subscribe() at all -- if the comment path ever called
    # it for MPs, this would error rather than silently pass.
    c = _client(tmp_path, "yes")
    mp = FakeMP()
    c.comment(mp, "Please rebase.")
    assert mp.created_comments == [f"Please rebase.\n\n{FOOTNOTE}"]


def test_dry_run_comment_does_not_subscribe(tmp_path):
    c = _client(tmp_path, "dry-run")
    bug = FakeBug()
    c.comment(bug, "hello")
    assert bug.subscribed_by == []


def test_deduped_comment_does_not_re_subscribe(tmp_path):
    # The dedup short-circuit runs before the write/subscribe path -- a
    # second identical comment is a no-op, including for the subscribe.
    c = _client(tmp_path, "yes")
    bug = FakeBug()
    bug.messages = [FakeBugMessage(BOT, f"hello\n\n{FOOTNOTE}")]
    c.comment(bug, "hello")
    assert bug.subscribed_by == []


def test_subscribe_failure_does_not_break_the_comment_outcome(tmp_path):
    class _UnsubscribableBug(FakeBug):
        def subscribe(self, person=None):
            raise TimeoutError("simulated Launchpad timeout")

    c = _client(tmp_path, "yes")
    bug = _UnsubscribableBug()
    c.comment(bug, "hello")
    assert bug.new_messages == [f"hello\n\n{FOOTNOTE}"]
    assert _entries(c)[-1]["outcome"] == "performed"
    assert c.all_writes_effective()
