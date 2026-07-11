"""Check 8 (#60): changes to upstream files must arrive as debian/patches
patches, not direct source edits. Merge MPs, new upstream versions, and
native packages are silently exempt; the trigger case (nux MP #508190) had
no changelog stanza at all, so nativeness falls back to the archive."""

import types

import archive_lookup
import checks
from fakes import FakeDiff, FakeMP

URL = "url"

# Direct edit: new stanza (same upstream as the entry below it) + a .cpp edit.
DIRECT_EDIT_DIFF = """\
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

   * Old entry.
diff --git a/src/framebuffer.cpp b/src/framebuffer.cpp
--- a/src/framebuffer.cpp
+++ b/src/framebuffer.cpp
@@ -1 +1 @@
-int old;
+int fixed;
"""

# Same shape but the version is native (no Debian revision).
NATIVE_DIFF = DIRECT_EDIT_DIFF.replace("1.2-3ubuntu2", "1.3").replace(
    "1.2-3ubuntu1", "1.2"
)

# New upstream version: upstream component changes 1.2 -> 1.3.
UPSTREAM_BUMP_DIFF = DIRECT_EDIT_DIFF.replace("1.2-3ubuntu2", "1.3-0ubuntu1")

# The nux #508190 shape: source edit, no changelog change at all.
NO_STANZA_DIFF = """\
diff --git a/src/framebuffer.cpp b/src/framebuffer.cpp
--- a/src/framebuffer.cpp
+++ b/src/framebuffer.cpp
@@ -1 +1 @@
-int old;
+int fixed;
"""

DEBIAN_ONLY_DIFF = """\
diff --git a/debian/patches/fix.patch b/debian/patches/fix.patch
--- /dev/null
+++ b/debian/patches/fix.patch
@@ -0,0 +1 @@
+the patch content
"""


def _mp(diff_text, source="refs/heads/fix-lp2000001", **kwargs):
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source=source,
        diff=FakeDiff("/d/1", 40, diff_text=diff_text),
        **kwargs,
    )


class _LP:
    lp = types.SimpleNamespace()


def setup_function(_fn):
    checks.reset_diff_lines_cache()


def test_direct_source_edit_fires_incomplete():
    finding = checks.check_direct_source_edit(URL, _mp(DIRECT_EDIT_DIFF), _LP())
    assert finding.tier == "incomplete"
    assert "`src/framebuffer.cpp`" in finding.message
    assert "debian/patches" in finding.message
    assert "apply-the-fix" in finding.message


def test_debian_only_changes_are_clean():
    assert checks.check_direct_source_edit(URL, _mp(DEBIAN_ONLY_DIFF), _LP()) is False


def test_merge_mp_is_exempt():
    mp = _mp(DIRECT_EDIT_DIFF, source="refs/heads/merge-1.2-3-stonking")
    assert checks.check_direct_source_edit(URL, mp, _LP()) is False


def test_new_upstream_version_is_exempt():
    assert (
        checks.check_direct_source_edit(URL, _mp(UPSTREAM_BUMP_DIFF), _LP()) is False
    )


def test_native_package_version_is_exempt():
    assert checks.check_direct_source_edit(URL, _mp(NATIVE_DIFF), _LP()) is False


def test_no_stanza_falls_back_to_archive_version(monkeypatch):
    # nux's shape: nativeness comes from the published version.
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, package, series_names=None: {"stonking": "1.2-3ubuntu1"},
    )
    finding = checks.check_direct_source_edit(URL, _mp(NO_STANZA_DIFF), _LP())
    assert finding.tier == "incomplete"


def test_no_stanza_native_archive_version_is_exempt(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, package, series_names=None: {"stonking": "1.2"},
    )
    assert checks.check_direct_source_edit(URL, _mp(NO_STANZA_DIFF), _LP()) is False


def test_no_stanza_archive_lookup_failure_is_inconclusive(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(
        archive_lookup, "ubuntu_versions", lambda lp, package, series_names=None: None
    )
    assert checks.check_direct_source_edit(URL, _mp(NO_STANZA_DIFF), _LP()) is None


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
    assert checks.check_direct_source_edit(URL, mp, _LP()) is None


def test_bug_without_attachments_is_clean():
    # The bug side is real since #62 (see test_attachments.py); a bug with
    # nothing attached has nothing to judge.
    from fakes import FakeBug

    assert checks.check_direct_source_edit(URL, FakeBug(), _LP()) is False


def test_many_files_are_capped_in_the_message():
    extra = "".join(
        f"diff --git a/src/f{i}.c b/src/f{i}.c\n"
        f"--- a/src/f{i}.c\n+++ b/src/f{i}.c\n@@ -1 +1 @@\n-a\n+b\n"
        for i in range(7)
    )
    finding = checks.check_direct_source_edit(URL, _mp(DIRECT_EDIT_DIFF + extra), _LP())
    assert "and 3 more" in finding.message
