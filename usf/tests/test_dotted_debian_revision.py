"""Dotted revisions reach ubuntu-lint (#162, #195).

#129 skipped Check 13 when the previous Debian revision had a dot (an NMU),
because ubuntu-lint 0.2.x misread it; #162 narrowed that to the Debian part
(tigervnc bug #2161717). ubuntu-lint 0.3 has the fix, and #195 removed the
skip: every shape is now handed to ubuntu-lint. Uses a fake ubuntu_lint, like
test_sru_version_suffix_convention_missing_lint.py, so it runs without the
real package installed.
"""

import types

from debian import changelog as debian_changelog
from fakes import FakeDiff, FakeMP

import archive_lookup
import checks

URL = "url"


class _LP:
    def __init__(self):
        self.lp = None


def _fake_lint(monkeypatch, calls):
    def check(context):
        calls.append(context)

    monkeypatch.setattr(
        checks,
        "ubuntu_lint",
        types.SimpleNamespace(
            Context=lambda **kw: kw,
            check_sru_version_string_convention=check,
            LintException=type("LintException", (Exception,), {}),
        ),
    )
    monkeypatch.setattr(checks, "debian_changelog", debian_changelog)


def _mp(archive_version, proposed_version, suite="jammy", package="tigervnc"):
    diff = f"""diff --git a/debian/changelog b/debian/changelog
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+{package} ({proposed_version}) {suite}; urgency=medium
+
+  * Fix something.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 {package} ({archive_version}) {suite}; urgency=medium
"""
    return FakeMP(
        target=f"refs/heads/ubuntu/{suite}-devel",
        diff=FakeDiff("/d/1", 174, diff_text=diff),
        package=package,
    )


def _patch_archive(monkeypatch, archive_version, suite="jammy"):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {suite: archive_version},
    )
    monkeypatch.setattr(archive_lookup, "published_source", lambda *a, **k: object())
    monkeypatch.setattr(
        archive_lookup,
        "changelog_text",
        lambda pub: (
            f"tigervnc ({archive_version}) {suite}; urgency=medium\n\n"
            "  * Old.\n\n -- A B <a@b.com>  Wed, 01 Jun 2026 10:27:27 +0200\n"
        ),
    )


def test_an_ordinary_re_sru_revision_is_not_skipped(monkeypatch):
    """The live trigger: dots only in the Ubuntu part of the previous
    revision (`4ubuntu0.22.04.1`, a prior SRU) -- not #129's case."""
    calls = []
    _fake_lint(monkeypatch, calls)
    _patch_archive(monkeypatch, "1.12.0+dfsg-4ubuntu0.22.04.1")
    mp = _mp("1.12.0+dfsg-4ubuntu0.22.04.1", "1.12.0+dfsg-4ubuntu0.22.04.2")

    result = checks.check_sru_version_suffix_convention(URL, mp, _LP())

    assert len(calls) == 1
    assert result is False


def test_a_dotted_debian_revision_reaches_ubuntu_lint(monkeypatch):
    """#129's case, skipped until #195: the DEBIAN part itself has a dot."""
    calls = []
    _fake_lint(monkeypatch, calls)
    _patch_archive(monkeypatch, "0.10.1-4.1")
    mp = _mp("0.10.1-4.1", "0.10.1-4.1ubuntu0.1")

    checks.check_sru_version_suffix_convention(URL, mp, _LP())

    assert len(calls) == 1


def test_ubuntu_lint_gets_our_launchpad_connection(monkeypatch):
    # #195: no anonymous login per call.
    calls = []
    _fake_lint(monkeypatch, calls)
    _patch_archive(monkeypatch, "0.10.1-4.1")
    lp = _LP()
    lp.lp = object()

    checks.check_sru_version_suffix_convention(URL, _mp("0.10.1-4.1", "0.10.1-4.1ubuntu0.1"), lp)

    assert calls[0]["launchpad_handle"] is lp.lp


def test_an_ubuntu_lint_older_than_0_3_is_silent_and_warned_once(monkeypatch, caplog):
    calls = []
    _fake_lint(monkeypatch, calls)

    def old_context(debian_changelog):  # 0.2.x: no launchpad_handle
        raise AssertionError("not reached")

    checks.ubuntu_lint.Context = old_context
    monkeypatch.setattr(checks, "_LINT_TOO_OLD_WARNED", False)
    _patch_archive(monkeypatch, "0.10.1-4")
    mp = _mp("0.10.1-4", "0.10.1-4ubuntu0.1")

    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False
    assert checks.check_sru_version_suffix_convention(URL, mp, _LP()) is False
    assert caplog.text.count("older than 0.3") == 1
    assert calls == []
