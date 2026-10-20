"""#162: only the DEBIAN part of the previous revision is tested for a dot.

#129's skip (ubuntu-lint misreads a dotted Debian/NMU revision) tested the
whole revision string, Ubuntu suffix included -- `4ubuntu0.22.04.1`, an
ordinary re-SRU revision, has dots only in that Ubuntu part, so Check 13
was skipped on most re-SRUs of an already-SRUed package (tigervnc bug
#2161717's jammy debdiff). Uses a fake ubuntu_lint, like
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
    monkeypatch.setattr(checks, "lint_handles_dotted_revisions", lambda: False)


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


def test_a_genuine_dotted_debian_revision_is_still_skipped(monkeypatch):
    """#129's actual case: the DEBIAN part itself has a dot (an NMU)."""
    calls = []
    _fake_lint(monkeypatch, calls)
    _patch_archive(monkeypatch, "0.10.1-4.1")
    mp = _mp("0.10.1-4.1", "0.10.1-4.1ubuntu0.1")

    result = checks.check_sru_version_suffix_convention(URL, mp, _LP())

    assert calls == []
    assert result is False


def test_a_dotted_debian_revision_with_no_ubuntu_suffix_is_still_skipped(monkeypatch):
    """A first-ever Ubuntu delta on top of a dotted Debian NMU: no `ubuntu`
    in the revision at all, so splitting on it is a no-op -- the whole
    (dotted) string is the Debian part."""
    calls = []
    _fake_lint(monkeypatch, calls)
    _patch_archive(monkeypatch, "0.10.1-4.1")
    mp = _mp("0.10.1-4.1", "0.10.1-4.1build1")

    result = checks.check_sru_version_suffix_convention(URL, mp, _LP())

    assert calls == []
    assert result is False
