"""Check 17 (#155): an already-uploaded changelog entry must not be edited.

Trigger: python-awscurl MP #511734 added 0.44-0ubuntu2 and, in the same
diff, reworded the 0.44-0ubuntu1 entry below it and added a bullet to it.
No whitespace exception (seb128): editor-normalized spacing or line endings
in old entries is a common bounce.
"""

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP

import attachments
import checks

URL = "url"

_NEW = """\
+python-awscurl (0.44-0ubuntu2) stonking; urgency=medium
+
+  * Make awscrt optional.
+
+ -- Dev <dev@example.com>  Mon, 21 Sep 2026 10:39:24 -0400
+
"""

# The trigger MP's changelog hunk, trimmed.
_TRIGGER = (
    """diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,6 +1,13 @@
"""
    + _NEW
    + """ python-awscurl (0.44-0ubuntu1) stonking; urgency=medium
\x20
-  * New upstream release 0.44
+  * New upstream release 0.44.
+  * d/gbp.conf: Add gbp.conf to match team packaging repos.
\x20
  -- Dev <dev@example.com>  Thu, 20 Aug 2026 18:26:21 -0400
\x20
"""
)

_CLEAN = (
    """diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,9 @@
"""
    + _NEW
    + """ python-awscurl (0.44-0ubuntu1) stonking; urgency=medium
\x20
   * New upstream release 0.44
"""
)


class _LP:
    lp = None


def _mp(diff_text, source="refs/heads/fix"):
    checks.reset_diff_lines_cache()
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source=source,
        diff=FakeDiff("/d/1", 20, diff_text=diff_text),
        package="python-awscurl",
    )


def _check(diff_text, **kw):
    return checks.check_released_changelog_edit(URL, _mp(diff_text, **kw), _LP())


def test_trigger_edit_fires_and_names_the_entry():
    finding = _check(_TRIGGER)

    assert finding.tier == "incomplete"
    assert finding.message == (
        "The diff also changes an existing `debian/changelog` entry "
        "(`python-awscurl 0.44-0ubuntu1`) that was already uploaded. Released entries "
        "are a record of past uploads and shouldn't be edited: please limit your "
        "changelog changes to the new entry at the top."
    )


def test_only_a_new_entry_is_clean():
    assert _check(_CLEAN) is False


def test_whitespace_only_edit_still_fires():
    edit = _CLEAN.replace(
        "   * New upstream release 0.44\n",
        "-  * New upstream release 0.44 \n+  * New upstream release 0.44\n",
    )
    assert _check(edit).tier == "incomplete"


def test_two_entries_edited_are_both_named():
    diff = _TRIGGER + (
        "@@ -8,4 +15,4 @@\n"
        " python-awscurl (0.43-1ubuntu1) stonking; urgency=medium\n"
        " \n"
        "-  * Old wording\n"
        "+  * New wording\n"
    )

    message = _check(diff).message

    assert message.startswith(
        "The diff also changes existing `debian/changelog` entries "
        "(`python-awscurl 0.44-0ubuntu1`, `python-awscurl 0.43-1ubuntu1`) that were"
    )


def test_edit_deep_in_history_without_its_header_still_fires():
    diff = _CLEAN + "@@ -40,3 +46,3 @@\n \n-  * typo\n+  * typo fixed\n \n"

    message = _check(diff).message

    assert message.startswith("The diff also changes an existing `debian/changelog` entry that")


def test_unreleased_entry_below_may_be_edited():
    diff = _TRIGGER.replace(
        " python-awscurl (0.44-0ubuntu1) stonking;", " python-awscurl (0.44-0ubuntu1) UNRELEASED;"
    )
    assert _check(diff) is False


def test_no_new_entry_is_left_to_check_9():
    in_place = """diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,4 @@
 python-awscurl (0.44-0ubuntu1) UNRELEASED; urgency=medium
\x20
   * New upstream release 0.44
+  * Another change.
"""
    assert _check(in_place) is False


def test_merge_mp_is_skipped():
    # A merge sits on the Debian revision (on an Ubuntu one it isn't a
    # merge, #104).
    on_debian = _TRIGGER.replace("(0.44-0ubuntu1)", "(0.44-1)")
    assert _check(on_debian, source="refs/heads/merge-0.44-1") is False


def test_unreadable_diff_is_inconclusive():
    class _RaisingDiffText:
        self_link = "/d/raising"
        diff_lines_count = 40

        @property
        def diff_text(self):
            raise RuntimeError("network blip")

    mp = _mp("")
    mp.preview_diff = _RaisingDiffText()
    assert checks.check_released_changelog_edit(URL, mp, _LP()) is None


# --- bug side: the debdiff under review --------------------------------------

_DEBDIFF = (
    "diff -Nru awscurl-0.44/debian/changelog awscurl-0.44/debian/changelog\n"
    "--- awscurl-0.44/debian/changelog\t2026-08-20 00:00:00.000000000 +0000\n"
    "+++ awscurl-0.44/debian/changelog\t2026-09-21 00:00:00.000000000 +0000\n"
    + _TRIGGER.split("+++ b/debian/changelog\n", 1)[1]
)


def _bug(content, title="awscrt optional"):
    attachments.reset_cache()
    return FakeBug(
        title=title, attachments=[FakeAttachment("fix.debdiff", type="Patch", content=content)]
    )


def test_bug_debdiff_editing_history_fires():
    finding = checks.check_released_changelog_edit(URL, _bug(_DEBDIFF), _LP())
    assert "(`python-awscurl 0.44-0ubuntu1`)" in finding.message


def test_merge_bug_is_skipped():
    bug = _bug(_DEBDIFF, title="Please merge python-awscurl 0.44-1 from Debian")
    assert checks.check_released_changelog_edit(URL, bug, _LP()) is False
