"""Check 16 (#151): a newly added debian/patches patch needs a DEP-3 header.

Trigger: golang-github-a-h-templ MP #512397, whose new patch started
straight at "Index:". Narrow on purpose (seb128): only a NEW patch with no
Description:/Subject: fires; header quality and modified patches are left
to humans.
"""

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP

import attachments
import checks

URL = "url"


class _LP:
    lp = None


_CHANGELOG = """diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+templ (0.3-2ubuntu1) stonking; urgency=medium
+
+  * d/p/0007-fix.patch: Fix the armhf alignment panic. (LP: #2169121)
+
+ -- Dev <dev@example.com>  Thu, 01 Oct 2026 15:38:28 +0530
+
 templ (0.3-2) unstable; urgency=medium
"""

# The trigger MP's patch, as git shows a new file: no header at all.
_HEADERLESS = """diff --git a/debian/patches/0007-fix.patch b/debian/patches/0007-fix.patch
new file mode 100644
--- /dev/null
+++ b/debian/patches/0007-fix.patch
@@ -0,0 +1,9 @@
+Index: templ/server.go
+===================================================================
+--- templ.orig/server.go
++++ templ/server.go
+@@ -17,3 +17,3 @@
+ type Handler struct {
+-	m        *sync.Mutex
+ 	counter  int64
++	m        *sync.Mutex
"""

_SERIES = """diff --git a/debian/patches/series b/debian/patches/series
--- a/debian/patches/series
+++ b/debian/patches/series
@@ -6,0 +7 @@
+0007-fix.patch
"""


def _with_header(first_line):
    return _HEADERLESS.replace("@@ -0,0 +1,9 @@\n", f"@@ -0,0 +1,11 @@\n+{first_line}\n+\n")


def _mp(diff_text, source="refs/heads/fix-armhf"):
    checks.reset_diff_lines_cache()
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source=source,
        diff=FakeDiff("/d/1", 20, diff_text=diff_text),
        package="templ",
    )


def test_new_patch_without_header_fires():
    finding = checks.check_dep3_patch_header(URL, _mp(_CHANGELOG + _HEADERLESS + _SERIES), _LP())

    assert finding.tier == "incomplete"
    assert finding.message == (
        "The new patch `debian/patches/0007-fix.patch` has no DEP-3 header. Please "
        "add one describing the change: at least `Description:`, plus `Origin:` "
        "(or `Author:`), `Bug-Ubuntu:` and `Forwarded:` where they apply. See "
        "https://ubuntu.com/project/docs/how-ubuntu-is-made/concepts/patches/"
    )


def test_description_or_subject_is_enough():
    for first in ("Description: Fix armhf atomic alignment", "Subject: [PATCH] fix alignment"):
        mp = _mp(_CHANGELOG + _with_header(first) + _SERIES)
        assert checks.check_dep3_patch_header(URL, mp, _LP()) is False, first


def test_description_inside_the_patch_body_does_not_count():
    # A "Description:" in the patched code is not a header.
    body = _HEADERLESS.replace(
        "+ type Handler struct {", "+ type Handler struct {\n+-Description: x"
    ).replace("+1,9 @@", "+1,10 @@")
    assert checks.check_dep3_patch_header(URL, _mp(_CHANGELOG + body), _LP()).tier == "incomplete"


def test_several_patches_are_listed_together():
    second = _HEADERLESS.replace("0007-fix", "0008-more")

    finding = checks.check_dep3_patch_header(URL, _mp(_CHANGELOG + _HEADERLESS + second), _LP())

    assert finding.message.startswith(
        "The new patches `debian/patches/0007-fix.patch`, `debian/patches/0008-more.patch` have"
    )


def test_modified_existing_patch_is_not_judged():
    modified = """diff --git a/debian/patches/0001-old.patch b/debian/patches/0001-old.patch
--- a/debian/patches/0001-old.patch
+++ b/debian/patches/0001-old.patch
@@ -3,3 +3,3 @@
 --- a/foo.c
 +++ b/foo.c
-+old
++new
"""
    assert checks.check_dep3_patch_header(URL, _mp(_CHANGELOG + modified), _LP()) is False


def test_new_non_patch_files_are_ignored():
    readme = _HEADERLESS.replace("debian/patches/0007-fix.patch", "debian/patches/README")
    other = _HEADERLESS.replace("debian/patches/0007-fix.patch", "debian/tests/control")
    assert checks.check_dep3_patch_header(URL, _mp(_CHANGELOG + readme + other), _LP()) is False


def test_merge_mp_is_skipped():
    mp = _mp(_CHANGELOG + _HEADERLESS, source="refs/heads/merge-0.3-2")
    assert checks.check_dep3_patch_header(URL, mp, _LP()) is False


def test_unreadable_diff_is_inconclusive():
    class _RaisingDiffText:
        self_link = "/d/raising"
        diff_lines_count = 40

        @property
        def diff_text(self):
            raise RuntimeError("network blip")

    mp = _mp("")
    mp.preview_diff = _RaisingDiffText()
    assert checks.check_dep3_patch_header(URL, mp, _LP()) is None


# --- bug side: the same rule on an attached debdiff -------------------------

_DEBDIFF = """diff -Nru templ-0.3/debian/changelog templ-0.3/debian/changelog
--- templ-0.3/debian/changelog	2026-09-01 00:00:00.000000000 +0000
+++ templ-0.3/debian/changelog	2026-10-01 00:00:00.000000000 +0000
@@ -1,3 +1,7 @@
+templ (0.3-2ubuntu1) stonking; urgency=medium
+
+  * Fix the armhf alignment panic. (LP: #2169121)
+
+ -- Dev <dev@example.com>  Thu, 01 Oct 2026 15:38:28 +0530
+
 templ (0.3-2) unstable; urgency=medium
diff -Nru templ-0.3/debian/patches/0007-fix.patch templ-0.3/debian/patches/0007-fix.patch
--- templ-0.3/debian/patches/0007-fix.patch	1970-01-01 00:00:00.000000000 +0000
+++ templ-0.3/debian/patches/0007-fix.patch	2026-10-01 00:00:00.000000000 +0000
@@ -0,0 +1,4 @@
+--- a/server.go
++++ b/server.go
+@@ -1 +1 @@
+-x
"""


def _bug(content, title="crash on armhf"):
    attachments.reset_cache()
    return FakeBug(
        title=title,
        attachments=[FakeAttachment("fix.debdiff", type="Patch", content=content)],
    )


def test_bug_debdiff_new_patch_without_header_fires():
    finding = checks.check_dep3_patch_header(URL, _bug(_DEBDIFF), _LP())
    assert "`debian/patches/0007-fix.patch` has no DEP-3 header" in finding.message


def test_bug_debdiff_with_header_is_clean():
    with_header = _DEBDIFF.replace("@@ -0,0 +1,4 @@\n", "@@ -0,0 +1,6 @@\n+Description: fix\n+\n")
    assert checks.check_dep3_patch_header(URL, _bug(with_header), _LP()) is False


def test_merge_bug_is_skipped():
    bug = _bug(_DEBDIFF, title="Please merge templ 0.3-2 from Debian unstable")
    assert checks.check_dep3_patch_header(URL, bug, _LP()) is False
