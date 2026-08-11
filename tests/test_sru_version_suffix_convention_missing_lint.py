"""check_sru_version_suffix_convention's fallback when ubuntu_lint isn't
installed on this host (design_journal.md #109) -- separate from
test_sru_version_suffix_convention.py so it runs even where the real
package is absent (it doesn't need it: checks.ubuntu_lint is monkeypatched
to None directly, exercising the same fallback that fires for real on a
host without python3-ubuntu-lint)."""

from fakes import FakeDiff, FakeMP

import archive_lookup
import checks

URL = "url"


class _LP:
    def __init__(self):
        self.lp = None


def test_missing_ubuntu_lint_is_false_not_none(monkeypatch):
    monkeypatch.setattr(checks, "ubuntu_lint", None)
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {"noble": "1.2-3"},
    )
    diff = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg (1.2-3ubuntu1) noble; urgency=medium
+
+  * Fix something.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3) noble; urgency=medium
"""
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        diff=FakeDiff("/d/1", 174, diff_text=diff),
        package="testpkg",
    )
    # False, not None: a missing system dependency must not mark every
    # item on this host inconclusive forever (main.py's whole-item gate).
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False
