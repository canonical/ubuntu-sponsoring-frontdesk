"""Check 15 (#132): a no-change rebuild takes a buildN revision.

Triggered by the diff carrying no functional change -- not by the changelog
wording, since "rebuild for the <x> transition" is as common as "no-change
rebuild" and neither phrase proves a delta is absent (seb128).
"""

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP

import attachments
import checks

URL = "url"


class _LP:
    lp = None


def _changelog_hunk(new_version, old_version):
    return f"""diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+gfarm2fs ({new_version}) stonking; urgency=medium
+
+  * Rebuild for the fuse3 transition
+
+ -- Dev <dev@example.com>  Thu, 24 Sep 2026 12:02:49 -0700
+
 gfarm2fs ({old_version}) resolute; urgency=medium
"""


_MAINTAINER_HUNK = """diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -2,7 +2,8 @@ Source: gfarm2fs
-Maintainer: Dmitry Smirnov <onlyjob@debian.org>
+Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
+XSBC-Original-Maintainer: Dmitry Smirnov <onlyjob@debian.org>
"""

_BUILD_DEPENDS_HUNK = """diff --git a/debian/control b/debian/control
--- a/debian/control
+++ b/debian/control
@@ -5,7 +5,7 @@ Standards-Version: 4.6.0
-Build-Depends: debhelper-compat (= 12), libfuse-dev
+Build-Depends: debhelper-compat (= 12), libfuse3-dev
"""


def _mp(diff_text):
    checks.reset_diff_lines_cache()
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/gfarm2fs-fuse-transition",
        diff=FakeDiff("/d/1", 20, diff_text=diff_text),
        package="gfarm2fs",
    )


def test_changelog_only_rebuild_with_ubuntu_revision_bounces():
    finding = checks.check_no_change_rebuild_version(
        URL, _mp(_changelog_hunk("1.2.16-1.1ubuntu1", "1.2.16-1.1")), _LP()
    )
    assert finding.tier == "incomplete"
    assert "`1.2.16-1.1build1`" in finding.message
    assert "`1.2.16-1.1ubuntu1`" in finding.message


def test_existing_build_revision_is_incremented():
    # Live shape (gfarm2fs fuse-transition branch): on top of build1 the
    # next rebuild is build2, not build1 and not ubuntu1.
    finding = checks.check_no_change_rebuild_version(
        URL, _mp(_changelog_hunk("1.2.16-1.1ubuntu1", "1.2.16-1.1build1")), _LP()
    )
    assert "`1.2.16-1.1build2`" in finding.message


def test_update_maintainer_change_is_tolerated():
    diff = _changelog_hunk("1.2.16-1.1ubuntu1", "1.2.16-1.1build1") + _MAINTAINER_HUNK
    finding = checks.check_no_change_rebuild_version(URL, _mp(diff), _LP())
    assert finding.tier == "incomplete"


def test_a_real_delta_is_not_a_rebuild():
    # A Build-Depends change IS a delta to keep, so ubuntuN is correct.
    diff = _changelog_hunk("1.2.16-1.1ubuntu1", "1.2.16-1.1build1") + _BUILD_DEPENDS_HUNK
    assert checks.check_no_change_rebuild_version(URL, _mp(diff), _LP()) is False


def test_correct_build_revision_is_quiet():
    assert (
        checks.check_no_change_rebuild_version(
            URL, _mp(_changelog_hunk("1.2.16-1.1build2", "1.2.16-1.1build1")), _LP()
        )
        is False
    )


def test_invisible_previous_version_is_not_guessed():
    diff = """diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+gfarm2fs (1.2.16-1.1ubuntu1) stonking; urgency=medium
+
+  * Rebuild for the fuse3 transition
+
+ -- Dev <dev@example.com>  Thu, 24 Sep 2026 12:02:49 -0700
+
"""
    assert checks.check_no_change_rebuild_version(URL, _mp(diff), _LP()) is False


def test_unfetchable_diff_is_inconclusive():
    checks.reset_diff_lines_cache()
    mp = FakeMP(target="refs/heads/ubuntu/devel", diff=None)
    assert checks.check_no_change_rebuild_version(URL, mp, _LP()) is None


_DEBDIFF = """\
diff -Nru gfarm2fs-1.2.16/debian/changelog gfarm2fs-1.2.16/debian/changelog
--- gfarm2fs-1.2.16/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ gfarm2fs-1.2.16/debian/changelog\t2026-09-24 12:02:49.000000000 +0200
@@ -1,3 +1,7 @@
+gfarm2fs (1.2.16-1.1ubuntu1) stonking; urgency=medium
+
+  * Rebuild for the fuse3 transition
+
+ -- Dev <dev@example.com>  Thu, 24 Sep 2026 12:02:49 -0700
+
 gfarm2fs (1.2.16-1.1build1) resolute; urgency=medium
"""


def test_bug_debdiff_is_covered():
    attachments.reset_cache()
    bug = FakeBug(attachments=[FakeAttachment("r.debdiff", type="Patch", content=_DEBDIFF)])
    finding = checks.check_no_change_rebuild_version(URL, bug, _LP())
    assert finding.tier == "incomplete"
    assert "`1.2.16-1.1build2`" in finding.message
