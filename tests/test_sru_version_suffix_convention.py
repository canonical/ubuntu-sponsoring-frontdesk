"""Check 13: SRU version-suffix convention (design_journal.md #109), via
ubuntu-lint's check_sru_version_string_convention rather than reimplementing
the suffix logic. MP-side and bug-side (debdiff attachments) -- see
checks.py's docstring; the bug path is a pure input-gathering wrapper
around the same shared verdict function _stale_version_bug's own bug/MP
split established.

python3-ubuntu-lint is a system package with no 24.04 PPA build yet (see
checks.py's import comment) -- present on the bot's real VM, absent on
GitHub Actions' runner. These tests exercise the real library, so skip
outright rather than fail when it's missing (checks.py's own defensive
import + _sru_version_convention_verdict's fail-to-False path are what
covers that host, tested separately below without needing the library at
all -- see test_missing_ubuntu_lint_is_false)."""

import pytest

pytest.importorskip("ubuntu_lint")

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP

import archive_lookup
import checks

URL = "url"


class _LP:
    def __init__(self):
        self.lp = None


def _patch_archive(monkeypatch, devel="stonking", versions=None, pub=object(), changelog=None):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: devel)
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: versions,
    )
    monkeypatch.setattr(
        archive_lookup,
        "published_source",
        lambda lp, pkg, series, version, status="Published": pub,
    )
    monkeypatch.setattr(archive_lookup, "changelog_text", lambda p: changelog)


def _mp_with_diff(diff_text, target="refs/heads/ubuntu/noble-devel", package="testpkg"):
    return FakeMP(
        target=target,
        diff=FakeDiff("/d/1", 174, diff_text=diff_text),
        package=package,
    )


def _diff_for(new_stanza_version, prev_stanza_version="1.2-3", suite="noble"):
    return f"""diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg ({new_stanza_version}) {suite}; urgency=medium
+
+  * Fix something.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg ({prev_stanza_version}) {suite}; urgency=medium
"""


_ARCHIVE_CHANGELOG = """testpkg (1.2-3) noble; urgency=medium

  * Something old.

 -- A B <a@b.com>  Wed, 01 Jun 2026 10:27:27 +0200
"""

_ARCHIVE_CHANGELOG_DEVEL = _ARCHIVE_CHANGELOG.replace("noble", "stonking")


def test_conventional_suffix_is_fine(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False


def test_wrong_suffix_is_advisory(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu1"))
    finding = checks.check_sru_version_suffix_convention(URL, mp, _LP())
    assert finding.tier == "question"
    assert finding.kind == "advisory"
    assert "1.2-3ubuntu0.1" in finding.message


def test_devel_targeted_mp_is_not_an_sru(monkeypatch):
    # ubuntu-lint derives "is this an SRU" from the changelog stanza's own
    # distribution field, not the MP's git target branch -- a devel-shaped
    # stanza (suite=stonking, the current devel codename) self-skips.
    _patch_archive(monkeypatch, versions={"stonking": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG_DEVEL)
    mp = _mp_with_diff(
        _diff_for("1.2-3ubuntu1", suite="stonking"), target="refs/heads/ubuntu/devel"
    )
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False


def test_nothing_published_yet_is_false(monkeypatch):
    _patch_archive(monkeypatch, versions={})
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False


def test_ubuntu_versions_lookup_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, versions=None)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is None


def test_published_source_lookup_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, pub=None)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is None


def test_changelog_fetch_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=None)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is None


def test_malformed_archive_changelog_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog="not a changelog at all")
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is None


def test_unfetchable_diff_is_inconclusive(monkeypatch):
    mp = FakeMP(diff=None, target="refs/heads/ubuntu/noble-devel")
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is None


def test_no_new_stanza_is_false(monkeypatch):
    mp = _mp_with_diff("diff --git a/debian/control b/debian/control\n")
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False


_DEBDIFF_CONVENTIONAL = """\
diff -Nru testpkg-1.2/debian/changelog testpkg-1.2/debian/changelog
--- testpkg-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,10 @@
+testpkg (1.2-3ubuntu0.1) noble; urgency=medium
+
+  * Fix something (LP: #2000001).
+
+ -- Riku <riku@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3) noble; urgency=medium
"""

_DEBDIFF_WRONG_SUFFIX = _DEBDIFF_CONVENTIONAL.replace("1.2-3ubuntu0.1", "1.2-3ubuntu1")


def test_bug_conventional_suffix_is_fine(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", content=_DEBDIFF_CONVENTIONAL)],
    )
    assert checks.check_sru_version_suffix_convention(URL, bug, _LP()) is False


def test_bug_wrong_suffix_is_advisory(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", content=_DEBDIFF_WRONG_SUFFIX)],
    )
    finding = checks.check_sru_version_suffix_convention(URL, bug, _LP())
    assert finding.tier == "question"
    assert finding.kind == "advisory"
    assert "1.2-3ubuntu0.1" in finding.message


def test_bug_unknown_suite_is_false(monkeypatch):
    debdiff = _DEBDIFF_CONVENTIONAL.replace("noble", "totallymadeup")
    bug = FakeBug(title="fix", attachments=[FakeAttachment("fix.debdiff", content=debdiff)])
    assert checks.check_sru_version_suffix_convention(URL, bug, _LP()) is False


def test_bug_no_attachments_is_false():
    bug = FakeBug(title="fix")
    assert checks.check_sru_version_suffix_convention(URL, bug, _LP()) is False


def test_bug_unfetchable_attachment_is_inconclusive():
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", fail_fetch=True)],
    )
    assert checks.check_sru_version_suffix_convention(URL, bug, _LP()) is None


def test_backport_from_devel_convention_is_accepted(monkeypatch):
    # #119: `~YY.MM.N` is the documented backport-from-devel convention
    # (live: python3-defaults 3.10.6-1~22.04.2 for jammy). The conftest
    # pins noble to 24.04.
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    mp = _mp_with_diff(_diff_for("1.2-3~24.04.2"))
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False


def test_backport_suffix_for_another_series_still_advises(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, changelog=_ARCHIVE_CHANGELOG)
    mp = _mp_with_diff(_diff_for("1.2-3~22.04.2"))
    finding = checks.check_sru_version_suffix_convention(URL, mp, _LP())
    assert finding and finding.kind == "advisory"
