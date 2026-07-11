"""The bug-attachment content foundation (#62): fetching, gunzipping,
classifying and selecting patch/debdiff attachments, plus Check 8's
bug-side path built on top of it."""

import gzip

import attachments
import checks
from fakes import FakeAttachment, FakeBug

URL = "url"

# A real-world-shaped debdiff (diff -Nru style: version-dir prefixes, no
# git headers): new changelog entry + a direct upstream source edit.
DEBDIFF = """\
diff -Nru testpkg-1.2/debian/changelog testpkg-1.2/debian/changelog
--- testpkg-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,10 @@
+testpkg (1.2-3ubuntu2) stonking; urgency=medium
+
+  * Fix the resize crash (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3ubuntu1) stonking; urgency=medium

   * Old entry.
diff -Nru testpkg-1.2/src/framebuffer.cpp testpkg-1.2/src/framebuffer.cpp
--- testpkg-1.2/src/framebuffer.cpp\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/src/framebuffer.cpp\t2026-07-11 10:00:00.000000000 +0200
@@ -1 +1 @@
-int old;
+int fixed;
"""

DEBDIFF_PATCHES_ONLY = """\
diff -Nru testpkg-1.2/debian/changelog testpkg-1.2/debian/changelog
--- testpkg-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,10 @@
+testpkg (1.2-3ubuntu2) stonking; urgency=medium
+
+  * Fix the resize crash (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3ubuntu1) stonking; urgency=medium
diff -Nru testpkg-1.2/debian/patches/fix.patch testpkg-1.2/debian/patches/fix.patch
--- testpkg-1.2/debian/patches/fix.patch\t1970-01-01 01:00:00.000000000 +0100
+++ testpkg-1.2/debian/patches/fix.patch\t2026-07-11 10:00:00.000000000 +0200
@@ -0,0 +1 @@
+the patch content
"""

# A plain upstream patch: no debian/ file at all -- a normal shape.
PLAIN_PATCH = """\
--- a/src/framebuffer.cpp
+++ b/src/framebuffer.cpp
@@ -1 +1 @@
-int old;
+int fixed;
"""


def setup_function(_fn):
    attachments.reset_cache()


def _bug(atts, title="crash on resize"):
    return FakeBug(title=title, attachments=atts)


def test_classify_debdiff_paths_strip_the_version_dir():
    info = attachments.classify_diff(DEBDIFF)
    assert info["debian_paths"] == ["debian/changelog"]
    assert info["other_paths"] == ["src/framebuffer.cpp"]
    assert any("testpkg (1.2-3ubuntu2)" in l for l in info["changelog_lines"])


def test_classify_git_style_diff_too():
    info = attachments.classify_diff(
        "diff --git a/debian/rules b/debian/rules\n"
        "--- a/debian/rules\n+++ b/debian/rules\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert info["debian_paths"] == ["debian/rules"]
    assert info["other_paths"] == []


def test_attachment_text_gunzips():
    att = FakeAttachment(
        "fix.debdiff.gz", type="Patch", content=gzip.compress(DEBDIFF.encode())
    )
    assert attachments.attachment_text(att) == DEBDIFF


def test_attachment_text_over_the_cap_is_unusable():
    big = "--- a/x\n+++ b/x\n" + "+x\n" * (attachments.MAX_ATTACHMENT_BYTES // 3 + 1)
    att = FakeAttachment("big.debdiff", content=big)
    assert attachments.attachment_text(att) is False


def test_gzip_bomb_is_capped_not_inflated():
    bomb = gzip.compress(b"--- a/x\n+++ b/x\n" + b"0" * (64 * 1024 * 1024))
    att = FakeAttachment("bomb.diff.gz", content=bomb)
    assert attachments.attachment_text(att) is False


def test_non_diff_content_is_unusable():
    att = FakeAttachment("screenshot.patch", content="not a diff at all")
    assert attachments.attachment_text(att) is False


def test_fetch_failure_is_none():
    att = FakeAttachment("fix.debdiff", fail_fetch=True)
    assert attachments.attachment_text(att) is None


def test_review_target_picks_the_newest_usable():
    old = FakeAttachment("v1.debdiff", content=PLAIN_PATCH)
    new = FakeAttachment("v2.debdiff", content=DEBDIFF)
    picked, text = attachments.review_target(_bug([old, new]))
    assert picked is new
    assert text == DEBDIFF


def test_review_target_skips_unusable_and_falls_back():
    tarball = FakeAttachment("src.patch", content="binary junk")
    real = FakeAttachment("fix.debdiff", content=DEBDIFF)
    picked, _text = attachments.review_target(_bug([real, tarball]))
    assert picked is real


def test_review_target_no_candidates_is_false():
    assert attachments.review_target(_bug([FakeAttachment("log.txt")])) is False


def test_review_target_fetch_failure_is_none():
    atts = [FakeAttachment("fix.debdiff", fail_fetch=True)]
    assert attachments.review_target(_bug(atts)) is None


def test_check8_bug_side_fires_on_a_direct_edit_debdiff():
    bug = _bug([FakeAttachment("fix.debdiff", type="Patch", content=DEBDIFF)])
    finding = checks.check_direct_source_edit(URL, bug, None)
    assert finding.tier == "incomplete"
    assert "The attached debdiff edits" in finding.message
    assert "`src/framebuffer.cpp`" in finding.message
    assert "debian/patches" in finding.message


def test_check8_bug_side_debdiff_with_proper_patches_is_clean():
    bug = _bug([FakeAttachment("fix.debdiff", content=DEBDIFF_PATCHES_ONLY)])
    assert checks.check_direct_source_edit(URL, bug, None) is False


def test_check8_bug_side_plain_patch_is_a_normal_shape():
    bug = _bug([FakeAttachment("fix.patch", type="Patch", content=PLAIN_PATCH)])
    assert checks.check_direct_source_edit(URL, bug, None) is False


def test_check8_bug_side_native_version_is_exempt():
    native = DEBDIFF.replace("1.2-3ubuntu2", "1.3").replace("1.2-3ubuntu1", "1.2")
    bug = _bug([FakeAttachment("fix.debdiff", content=native)])
    assert checks.check_direct_source_edit(URL, bug, None) is False


def test_check8_bug_side_new_upstream_version_is_exempt():
    bump = DEBDIFF.replace("1.2-3ubuntu2", "1.3-0ubuntu1")
    bug = _bug([FakeAttachment("fix.debdiff", content=bump)])
    assert checks.check_direct_source_edit(URL, bug, None) is False


def test_check8_bug_side_merge_bug_is_exempt():
    bug = _bug(
        [FakeAttachment("fix.debdiff", content=DEBDIFF)],
        title="Please merge testpkg 1.3-1 from Debian unstable",
    )
    assert checks.check_direct_source_edit(URL, bug, None) is False


def test_check8_bug_side_fetch_failure_is_inconclusive():
    bug = _bug([FakeAttachment("fix.debdiff", fail_fetch=True)])
    assert checks.check_direct_source_edit(URL, bug, None) is None


def test_check8_bug_side_no_attachments_is_clean():
    assert checks.check_direct_source_edit(URL, _bug([]), None) is False


# --- changelog bug-reference parity (#63) -----------------------------------
# DEBDIFF's new entry cites LP: #2000001 against package "testpkg".

import types

from fakes import FakeTask


class _LP:
    def __init__(self, bugs=None):
        self.lp = types.SimpleNamespace(bugs=bugs or {})


def test_bugref_self_reference_is_clean_without_a_lookup():
    # The host bug is #2000001 itself and targets testpkg: verified against
    # the object we already hold -- the empty lp.bugs map proves no load.
    bug = FakeBug(
        id=2000001,
        tasks=[FakeTask("testpkg (Ubuntu)", "New")],
        attachments=[FakeAttachment("fix.debdiff", type="Patch", content=DEBDIFF)],
    )
    assert checks.check_changelog_bug_reference(URL, bug, _LP()) is False


def test_bugref_mismatched_citation_fires():
    cited = FakeBug(tasks=[FakeTask("otherpkg (Ubuntu)", "New")])
    bug = FakeBug(
        id=999,
        attachments=[FakeAttachment("fix.debdiff", content=DEBDIFF)],
    )
    finding = checks.check_changelog_bug_reference(URL, bug, _LP({2000001: cited}))
    assert finding.tier == "incomplete"
    assert "attached debdiff's changelog" in finding.message
    assert "#2000001" in finding.message
    assert "`testpkg`" in finding.message


def test_bugref_citation_targeting_the_package_is_clean():
    cited = FakeBug(tasks=[FakeTask("testpkg (Ubuntu Noble)", "New")])
    bug = FakeBug(
        id=999,
        attachments=[FakeAttachment("fix.debdiff", content=DEBDIFF)],
    )
    assert (
        checks.check_changelog_bug_reference(URL, bug, _LP({2000001: cited}))
        is False
    )


def test_bugref_plain_patch_has_no_entry_and_skips():
    bug = FakeBug(attachments=[FakeAttachment("fix.patch", content=PLAIN_PATCH)])
    assert checks.check_changelog_bug_reference(URL, bug, _LP()) is False


def test_bugref_no_attachments_is_clean():
    assert checks.check_changelog_bug_reference(URL, FakeBug(), _LP()) is False


def test_bugref_fetch_failure_is_inconclusive():
    bug = FakeBug(attachments=[FakeAttachment("fix.debdiff", fail_fetch=True)])
    assert checks.check_changelog_bug_reference(URL, bug, _LP()) is None


def test_bugref_lookup_failure_with_nothing_confirmed_is_inconclusive():
    # Cited bug #2000001 can't be loaded (not in the map) and the host bug
    # is a different number: nothing confirmed -> retry, not a clean False.
    bug = FakeBug(
        id=999,
        attachments=[FakeAttachment("fix.debdiff", content=DEBDIFF)],
    )
    assert checks.check_changelog_bug_reference(URL, bug, _LP()) is None


# --- plain code patch needs to become a debdiff (#64) ------------------------

FORMAT_PATCH = """\
From 1234abcd Mon Sep 17 00:00:00 2001
From: Riku <riku@example.com>
Date: Fri, 10 Jul 2026 10:00:00 +0200
Subject: [PATCH] Fix the resize crash

---
 src/framebuffer.cpp | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

diff --git a/src/framebuffer.cpp b/src/framebuffer.cpp
--- a/src/framebuffer.cpp
+++ b/src/framebuffer.cpp
@@ -1 +1 @@
-int old;
+int fixed;
"""


def test_plain_patch_bounces_asking_for_a_debdiff():
    bug = _bug([FakeAttachment("fix.patch", type="Patch", content=PLAIN_PATCH)])
    finding = checks.check_patch_not_debdiff(URL, bug, None)
    assert finding.tier == "incomplete"
    assert "plain code patch" in finding.message
    assert "source package update (debdiff)" in finding.message
    assert "`debian/patches`" in finding.message
    assert "`debian/changelog` entry with an incremented version" in finding.message
    assert "work-with-debian-patches" in finding.message


def test_git_format_patch_bounces_the_same_way():
    bug = _bug([FakeAttachment("0001-fix.patch", content=FORMAT_PATCH)])
    finding = checks.check_patch_not_debdiff(URL, bug, None)
    assert finding.tier == "incomplete"


def test_debdiff_shaped_attachment_is_not_this_checks_business():
    bug = _bug([FakeAttachment("fix.debdiff", content=DEBDIFF)])
    assert checks.check_patch_not_debdiff(URL, bug, None) is False


def test_merge_bug_is_exempt():
    bug = _bug(
        [FakeAttachment("fix.patch", content=PLAIN_PATCH)],
        title="Please merge testpkg 1.3-1 from Debian unstable",
    )
    assert checks.check_patch_not_debdiff(URL, bug, None) is False


def test_sync_request_is_exempt():
    bug = _bug(
        [FakeAttachment("fix.patch", content=PLAIN_PATCH)],
        title="Sync testpkg 1.3-1 (universe) from Debian unstable (main)",
    )
    assert checks.check_patch_not_debdiff(URL, bug, None) is False


def test_needs_packaging_bug_is_exempt():
    bug = FakeBug(
        tasks=[FakeTask("ubuntu", "New")],
        attachments=[FakeAttachment("fix.patch", content=PLAIN_PATCH)],
    )
    assert checks.check_patch_not_debdiff(URL, bug, None) is False


def test_active_linked_mp_means_the_review_lives_there():
    from fakes import FakeMP

    bug = _bug([FakeAttachment("fix.patch", content=PLAIN_PATCH)])
    bug.linked_merge_proposals = [FakeMP(queue_status="Needs review")]
    assert checks.check_patch_not_debdiff(URL, bug, None) is False


def test_inactive_linked_mp_does_not_shield_the_patch():
    from fakes import FakeMP

    bug = _bug([FakeAttachment("fix.patch", content=PLAIN_PATCH)])
    bug.linked_merge_proposals = [FakeMP(queue_status="Rejected")]
    assert checks.check_patch_not_debdiff(URL, bug, None).tier == "incomplete"


def test_patch_fetch_failure_is_inconclusive():
    bug = _bug([FakeAttachment("fix.patch", fail_fetch=True)])
    assert checks.check_patch_not_debdiff(URL, bug, None) is None


def test_no_usable_attachment_is_clean():
    assert checks.check_patch_not_debdiff(URL, _bug([]), None) is False


def test_mp_resource_is_skipped():
    mp = types.SimpleNamespace(
        resource_type_link="https://api.launchpad.net/devel/#branch_merge_proposal"
    )
    assert checks.check_patch_not_debdiff(URL, mp, None) is False
