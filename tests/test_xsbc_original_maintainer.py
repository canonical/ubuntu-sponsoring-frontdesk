"""Check 12: a package's first Ubuntu delta should add
XSBC-Original-Maintainer to debian/control (design_journal.md #93,
https://ubuntu.com/project/docs/contributors/updating/make-changes-to-a-
package/#updating-the-maintainer). Advisory only -- a sponsor can add it
at upload time."""

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP, FakeRoot, FakeTriageClient

import archive_lookup
import checks

URL = "url"


def _mp_with_diff(diff_text):
    # FakeMP's default source branch name looks merge-shaped
    # ('merge-1.2-3-stonking'); override it so _is_merge_proposal doesn't
    # exempt these fix-shaped fixtures.
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/fix-something",
        diff=FakeDiff("/d/1", 174, diff_text=diff_text),
        package="foo",
        bugs=[],
    )


_FIRST_DELTA_NO_XSBC = """diff --git a/debian/changelog b/debian/changelog
index 111..222 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+foo (1.2-3ubuntu1) stonking; urgency=medium
+
+  * Ubuntuify.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 foo (1.2-3) unstable; urgency=medium
diff --git a/debian/control b/debian/control
index 333..444 100644
--- a/debian/control
+++ b/debian/control
@@ -1,2 +1,2 @@
-Maintainer: Debian Foo Maintainers <foo@lists.debian.org>
+Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
"""

_FIRST_DELTA_WITH_XSBC = _FIRST_DELTA_NO_XSBC.replace(
    "-Maintainer: Debian Foo Maintainers <foo@lists.debian.org>\n"
    "+Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>\n",
    "-Maintainer: Debian Foo Maintainers <foo@lists.debian.org>\n"
    "+Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>\n"
    "+XSBC-Original-Maintainer: Debian Foo Maintainers <foo@lists.debian.org>\n",
)

_NOT_FIRST_DELTA = """diff --git a/debian/changelog b/debian/changelog
index 111..222 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+foo (1.2-3ubuntu2) stonking; urgency=medium
+
+  * Another delta.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 foo (1.2-3ubuntu1) stonking; urgency=medium
"""

_MERGE_NO_CONTEXT = """diff --git a/debian/changelog b/debian/changelog
index 111..222 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,4 @@
+foo (1.2-3) unstable; urgency=medium
+
+  * Merge.
+
"""


def test_mp_first_delta_missing_field_fires():
    mp = _mp_with_diff(_FIRST_DELTA_NO_XSBC)
    finding = checks.check_xsbc_original_maintainer(URL, mp, FakeTriageClient(objects={}))
    assert finding.tier == "question" and finding.kind == "advisory"
    assert "XSBC-Original-Maintainer" in finding.message


def test_mp_first_delta_with_field_is_clean():
    mp = _mp_with_diff(_FIRST_DELTA_WITH_XSBC)
    assert checks.check_xsbc_original_maintainer(URL, mp, FakeTriageClient(objects={})) is False


def test_mp_not_first_delta_is_false():
    mp = _mp_with_diff(_NOT_FIRST_DELTA)
    assert checks.check_xsbc_original_maintainer(URL, mp, FakeTriageClient(objects={})) is False


def test_mp_merge_is_exempt():
    mp = FakeMP(
        target="refs/heads/debian/sid",
        source="refs/heads/merge/1.2-3",
        diff=FakeDiff("/d/1", 174, diff_text=_FIRST_DELTA_NO_XSBC),
        package="foo",
        bugs=[],
    )
    assert checks.check_xsbc_original_maintainer(URL, mp, FakeTriageClient(objects={})) is False


def _lp():
    return FakeTriageClient(objects={}, lp=FakeRoot(devel_series_name="stonking"))


def test_mp_falls_back_to_archive_when_context_too_short(monkeypatch):
    monkeypatch.setattr(
        archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {"stonking": "1.2-3"}
    )
    mp = _mp_with_diff(_MERGE_NO_CONTEXT.replace("foo (1.2-3)", "foo (1.2-3ubuntu1)"))
    finding = checks.check_xsbc_original_maintainer(URL, mp, _lp())
    assert finding.tier == "question"


def test_mp_archive_lookup_failure_is_inconclusive(monkeypatch):
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: None)
    mp = _mp_with_diff(_MERGE_NO_CONTEXT.replace("foo (1.2-3)", "foo (1.2-3ubuntu1)"))
    assert checks.check_xsbc_original_maintainer(URL, mp, _lp()) is None


def test_mp_nothing_published_is_false(monkeypatch):
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    mp = _mp_with_diff(_MERGE_NO_CONTEXT.replace("foo (1.2-3)", "foo (1.2-3ubuntu1)"))
    assert checks.check_xsbc_original_maintainer(URL, mp, _lp()) is False


def test_mp_unfetchable_diff_is_inconclusive():
    mp = FakeMP(target="refs/heads/ubuntu/devel", source="refs/heads/fix-x", diff=None)
    assert checks.check_xsbc_original_maintainer(URL, mp, _lp()) is None


# --- bug-side (debdiff attachment) ---------------------------------------------

_BUG_FIRST_DELTA_NO_XSBC = """\
diff -Nru foo-1.2/debian/changelog foo-1.2/debian/changelog
--- foo-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ foo-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,10 @@
+foo (1.2-3ubuntu1) stonking; urgency=medium
+
+  * Ubuntuify (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 foo (1.2-3) unstable; urgency=medium
diff -Nru foo-1.2/debian/control foo-1.2/debian/control
--- foo-1.2/debian/control\t2026-06-01 10:00:00.000000000 +0200
+++ foo-1.2/debian/control\t2026-07-11 10:00:00.000000000 +0200
@@ -1,2 +1,2 @@
-Maintainer: Debian Foo Maintainers <foo@lists.debian.org>
+Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
"""


def test_bug_debdiff_first_delta_missing_field_fires():
    bug = FakeBug(
        title="fix", attachments=[FakeAttachment("fix.debdiff", content=_BUG_FIRST_DELTA_NO_XSBC)]
    )
    finding = checks.check_xsbc_original_maintainer(URL, bug, FakeTriageClient(objects={URL: bug}))
    assert finding.tier == "question" and finding.kind == "advisory"


def test_bug_sync_request_is_exempt():
    bug = FakeBug(
        title="Sync foo 1.2-3ubuntu1 (main) from Debian unstable",
        attachments=[FakeAttachment("fix.debdiff", content=_BUG_FIRST_DELTA_NO_XSBC)],
    )
    assert (
        checks.check_xsbc_original_maintainer(URL, bug, FakeTriageClient(objects={URL: bug}))
        is False
    )


def test_bug_no_attachments_is_false():
    bug = FakeBug(title="fix")
    assert (
        checks.check_xsbc_original_maintainer(URL, bug, FakeTriageClient(objects={URL: bug}))
        is False
    )
