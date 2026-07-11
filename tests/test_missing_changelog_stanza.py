"""Check 9 (#61): a fix MP must add a debian/changelog entry. Only fires
when debian/changelog is completely absent from the diff (the nux MP
#508190 shape); a touched changelog -- even just appending to an existing
UNRELEASED entry -- is clean. The LP: #nnn suggestion appears only when a
bug is linked to the MP or referenced in its commit message/description."""

import types

import checks
from fakes import FakeDiff, FakeMP

URL = "url"
DOC = "commit-changes/#write-the-changelog-entry"

NO_CHANGELOG_DIFF = """\
diff --git a/debian/patches/fix.patch b/debian/patches/fix.patch
--- /dev/null
+++ b/debian/patches/fix.patch
@@ -0,0 +1 @@
+the patch content
"""

WITH_STANZA_DIFF = """\
diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,10 @@
+testpkg (1.2-3ubuntu2) stonking; urgency=medium
+
+  * Fix the resize crash (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3ubuntu1) stonking; urgency=medium
"""

# Appends a bullet to an existing (e.g. UNRELEASED) entry: changelog is
# touched but no header line is added. Deliberately clean.
UNRELEASED_APPEND_DIFF = """\
diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,4 +1,5 @@
 testpkg (1.2-3ubuntu2) UNRELEASED; urgency=medium

   * First fix.
+  * Second fix.

"""


def _mp(diff_text=NO_CHANGELOG_DIFF, source="refs/heads/fix-lp2000001", **kwargs):
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source=source,
        diff=FakeDiff("/d/1", 40, diff_text=diff_text),
        **kwargs,
    )


def _linked_bug(number):
    return types.SimpleNamespace(id=number, title="a bug")


def test_missing_changelog_fires_incomplete_without_bug():
    finding = checks.check_missing_changelog_stanza(URL, _mp(), None)
    assert finding.tier == "incomplete"
    assert "doesn't add a `debian/changelog` entry" in finding.message
    assert "incremented version number -- see" in finding.message
    assert DOC in finding.message
    assert "LP:" not in finding.message


def test_linked_bug_gets_the_lp_reference_suggestion():
    mp = _mp(bugs=[_linked_bug(2000001)])
    finding = checks.check_missing_changelog_stanza(URL, mp, None)
    assert "including an `LP: #2000001` reference" in finding.message
    assert "so the bug is closed when the package is published" in finding.message


def test_two_linked_bugs_get_plural_wording():
    mp = _mp(bugs=[_linked_bug(2000001), _linked_bug(2000002)])
    finding = checks.check_missing_changelog_stanza(URL, mp, None)
    assert "`LP: #2000001`, `LP: #2000002` references" in finding.message
    assert "so the bugs are closed" in finding.message


def test_lp_reference_in_commit_message_is_used_when_no_bug_is_linked():
    mp = _mp()
    mp.commit_message = "fix the crash (LP: #1999999)"
    finding = checks.check_missing_changelog_stanza(URL, mp, None)
    assert "including an `LP: #1999999` reference" in finding.message


def test_lp_reference_in_description_is_used_too():
    mp = _mp()
    mp.description = "See LP: #1888888 for details"
    finding = checks.check_missing_changelog_stanza(URL, mp, None)
    assert "`LP: #1888888`" in finding.message


def test_new_changelog_entry_is_clean():
    assert (
        checks.check_missing_changelog_stanza(URL, _mp(WITH_STANZA_DIFF), None)
        is False
    )


def test_appending_to_an_existing_entry_is_clean():
    assert (
        checks.check_missing_changelog_stanza(URL, _mp(UNRELEASED_APPEND_DIFF), None)
        is False
    )


def test_merge_mp_is_exempt():
    mp = _mp(source="refs/heads/merge-1.2-3-stonking")
    assert checks.check_missing_changelog_stanza(URL, mp, None) is False


def test_empty_diff_is_someone_elses_problem():
    assert checks.check_missing_changelog_stanza(URL, _mp(""), None) is False


def test_unreadable_diff_is_inconclusive():
    class _RaisingDiffText:
        self_link = "/d/raising"
        diff_lines_count = 40

        @property
        def diff_text(self):
            raise RuntimeError("network blip")

    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/fix-lp2000001",
        diff=_RaisingDiffText(),
    )
    assert checks.check_missing_changelog_stanza(URL, mp, None) is None


def test_linked_bugs_lookup_failure_is_inconclusive():
    class _RaisingBugs:
        def __iter__(self):
            raise TimeoutError("simulated Launchpad timeout")

    mp = _mp()
    mp.bugs = _RaisingBugs()
    assert checks.check_missing_changelog_stanza(URL, mp, None) is None


def test_bug_resource_is_skipped():
    bug = types.SimpleNamespace(
        resource_type_link="https://api.launchpad.net/devel/#bug"
    )
    assert checks.check_missing_changelog_stanza(URL, bug, None) is False
