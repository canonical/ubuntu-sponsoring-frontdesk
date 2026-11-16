"""#179 / Check 18: a fix pasted into a comment, with nothing attached.

Trigger: gnome-remote-desktop bug #2174636. The submitter pasted the
upstream patch into comment #2 (indented like a Markdown code block), with
no attachment and no merge proposal. Check 1b closed the bug as "no patch
or merge proposal attached" and unsubscribed ~ubuntu-sponsors -- dropping a
real contribution from the queue. Now Check 1b leaves it, and Check 18 asks
for a debdiff or a merge proposal, like Check 10 does for a plain patch.
"""

from fakes import HUMAN, FakeAttachment, FakeBug, FakeBugMessage, FakeLLM, FakeMP, FakeTriageClient

import checks
import main
from state import StateManager

URL = "https://launchpad.net/bugs/2174636"

# Shaped like the live comment: indented four spaces.
PASTED = """\
## The patch (upstream commit c82b4ed4, to cherry-pick onto 50.2)

    --- a/src/grd-daemon-system.c
    +++ b/src/grd-daemon-system.c
    @@ -323,8 +323,6 @@ on_handle_start_handover (GrdDBusRemoteDesktopRdpHandover *interface,
       g_autofree char *key = NULL;
    -  g_autoptr (GVariant) redirect_variant = NULL;
"""

WORDING = (
    "The proposed fix appears to be pasted into a comment. Thank you for working "
    "on it! To be ready for sponsoring it needs to be attached as a source package "
    "update (debdiff) -- include the patch under `debian/patches` and add a new "
    "`debian/changelog` entry with an incremented version number describing the "
    "change -- or proposed as a merge proposal. See "
    "https://ubuntu.com/project/docs/contributors/updating/work-with-debian-patches/"
)


def _bug(comment=PASTED, author=HUMAN, **kw):
    bug = FakeBug(description="[ Impact ]\n\nA use-after-free.", **kw)
    bug.messages = [FakeBugMessage(author, comment)]
    return bug


# --- detection -------------------------------------------------------------------


def test_the_live_shape_is_detected():
    assert checks._comment_with_pasted_patch(_bug()) is True


def test_a_patch_in_the_description_counts_too():
    bug = FakeBug(description="Fix:\n" + PASTED)
    assert checks._comment_with_pasted_patch(bug) is True


def test_prose_with_dashes_is_not_a_patch():
    prose = "--- Steps ---\n+++ important +++\nRun it @@ twice @@."
    assert checks._comment_with_pasted_patch(_bug(prose)) is False


def test_a_hunk_header_is_required():
    no_hunk = "--- a/x.c\n+++ b/x.c\nsome text\n"
    assert checks._comment_with_pasted_patch(_bug(no_hunk)) is False


# --- Check 1b no longer closes it ---------------------------------------------------


def test_check_1b_leaves_a_pasted_patch_alone():
    bug = _bug()
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) is False
    assert lp.comments == []
    assert getattr(lp, "unsubscribed", 0) == 0


def test_check_1b_still_closes_a_bug_with_no_patch_anywhere():
    bug = _bug("Any news on this?")
    lp = FakeTriageClient(objects={URL: bug})
    assert checks.check_nothing_to_sponsor(URL, bug, lp) == "no_patch"


# --- Check 18 ------------------------------------------------------------------


def test_check_18_asks_for_a_debdiff():
    bug = _bug()
    finding = checks.check_patch_in_comment(URL, bug, FakeTriageClient(objects={URL: bug}))
    assert finding.tier == "incomplete"
    assert finding.message == WORDING


def test_check_18_skips_when_a_patch_is_attached():
    bug = _bug(attachments=[FakeAttachment("fix.debdiff", type="Patch")])
    assert checks.check_patch_in_comment(URL, bug, FakeTriageClient(objects={URL: bug})) is False


def test_check_18_skips_when_a_live_mp_is_linked():
    bug = _bug(linked_merge_proposals=[FakeMP()])
    assert checks.check_patch_in_comment(URL, bug, FakeTriageClient(objects={URL: bug})) is False


def test_check_18_leaves_an_engaged_reviewer_alone():
    # A sponsor pasting a suggested fix is part of a conversation.
    bug = _bug(author="https://api.launchpad.net/devel/~a-reviewer")
    assert checks.check_patch_in_comment(URL, bug, FakeTriageClient(objects={URL: bug})) is False


def test_check_18_ignores_merge_proposals():
    assert checks.check_patch_in_comment(URL, FakeMP(), None) is False


def test_end_to_end_the_bug_is_bounced_not_closed(tmp_path):
    bug = _bug()
    lp = FakeTriageClient(objects={URL: bug})
    sm = StateManager(db_path=str(tmp_path / "s.db"))

    main.triage_url(URL, sm, lp, FakeLLM())

    assert getattr(lp, "unsubscribed", 0) == 0
    assert len(lp.comments) == 1
    assert WORDING in lp.comments[0]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"
