"""Check 11: the proposed version carries a ~ppaN suffix (design_journal.md
#90). Found live on ipmiutil MP #508280 (3.2.2-1ubuntu1~ppa2) -- a PPA-build
version string has no business in an archive upload."""

import checks
from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP, FakeTriageClient

URL = "url"

_PPA_MP_DIFF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+ipmiutil (3.2.2-1ubuntu1~ppa2) stonking; urgency=medium
+
+  * PPA test build.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 ipmiutil (3.2.2-1ubuntu1) stonking; urgency=medium
"""

_CLEAN_MP_DIFF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+ipmiutil (3.2.2-1ubuntu2) stonking; urgency=medium
+
+  * Fix something.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 ipmiutil (3.2.2-1ubuntu1) stonking; urgency=medium
"""


def _mp_with_diff(diff_text):
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        diff=FakeDiff("/d/1", 174, diff_text=diff_text),
        package="ipmiutil",
    )


def test_mp_ppa_suffix_fires():
    mp = _mp_with_diff(_PPA_MP_DIFF)
    finding = checks.check_ppa_version_suffix(URL, mp, FakeTriageClient(objects={}))
    assert finding.tier == "incomplete"
    assert "3.2.2-1ubuntu1~ppa2" in finding.message
    assert "~ppaN" in finding.message


def test_mp_clean_version_is_false():
    mp = _mp_with_diff(_CLEAN_MP_DIFF)
    assert (
        checks.check_ppa_version_suffix(URL, mp, FakeTriageClient(objects={}))
        is False
    )


def test_mp_uppercase_ppa_still_fires():
    diff = _PPA_MP_DIFF.replace("~ppa2", "~PPA2")
    mp = _mp_with_diff(diff)
    finding = checks.check_ppa_version_suffix(URL, mp, FakeTriageClient(objects={}))
    assert finding is not False and finding is not None


def test_mp_unfetchable_diff_is_inconclusive():
    mp = FakeMP(diff=None)
    assert (
        checks.check_ppa_version_suffix(URL, mp, FakeTriageClient(objects={})) is None
    )


def test_mp_no_new_stanza_is_false():
    mp = _mp_with_diff("diff --git a/debian/control b/debian/control\n")
    assert (
        checks.check_ppa_version_suffix(URL, mp, FakeTriageClient(objects={}))
        is False
    )


def test_bug_not_a_bug_or_mp_is_false():
    class _Other:
        resource_type_link = "https://api.launchpad.net/devel/#distribution"

    assert (
        checks.check_ppa_version_suffix(URL, _Other(), FakeTriageClient(objects={}))
        is False
    )


_PPA_DEBDIFF = """\
diff -Nru ipmiutil-3.2.2/debian/changelog ipmiutil-3.2.2/debian/changelog
--- ipmiutil-3.2.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ ipmiutil-3.2.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,10 @@
+ipmiutil (3.2.2-1ubuntu1~ppa2) stonking; urgency=medium
+
+  * PPA test build (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 ipmiutil (3.2.2-1ubuntu1) stonking; urgency=medium
"""


def test_bug_debdiff_ppa_suffix_fires():
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", content=_PPA_DEBDIFF)],
    )
    finding = checks.check_ppa_version_suffix(
        URL, bug, FakeTriageClient(objects={URL: bug})
    )
    assert finding.tier == "incomplete"
    assert "3.2.2-1ubuntu1~ppa2" in finding.message


def test_bug_no_attachments_is_false():
    bug = FakeBug(title="fix")
    assert (
        checks.check_ppa_version_suffix(
            URL, bug, FakeTriageClient(objects={URL: bug})
        )
        is False
    )


def test_bug_unfetchable_attachment_is_inconclusive():
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", fail_fetch=True)],
    )
    assert (
        checks.check_ppa_version_suffix(
            URL, bug, FakeTriageClient(objects={URL: bug})
        )
        is None
    )
