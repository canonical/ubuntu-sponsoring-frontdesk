"""#173: a multi-release SRU using ubuntu0.YY.MM.N everywhere isn't flagged.

Trigger: lua-bitop bug #2173901, a security update with debdiffs for jammy
(`1.0.2-5`, also published in bionic and focal) and noble (`1.0.2-7`, only
in noble), both `ubuntu0.YY.MM.1`. The version-strings doc requires the
per-release form only when the same base version is in several releases,
so ubuntu-lint expected `1.0.2-7ubuntu0.1` for noble and Check 13 flagged
it. One consistent scheme across a multi-release update is harmless -- the
version sorts ahead and can't collide -- so Check 13 stays silent when
every other series' debdiff uses the per-release form and ubuntu-lint
itself expects it for at least one of them.

Uses a fake ubuntu_lint that applies the documented rule from a `shared`
set (series whose base version is published in another release too).
"""

import types

import pytest
from debian import changelog as debian_changelog
from fakes import FakeAttachment, FakeBug

import archive_lookup
import attachments
import checks

URL = "https://launchpad.net/bugs/2173901"
SERIES = [
    ("jammy", "22.04"),
    ("noble", "24.04"),
    ("resolute", "26.04"),
    ("stonking", "26.10"),
    ("tenacious", "27.04"),
]
RELEASE = dict(SERIES)
ARCHIVE = {"jammy": "1.0.2-5", "noble": "1.0.2-7"}


class _Client:
    lp = None


class _LintError(Exception):
    def __init__(self, result, reason):
        super().__init__(reason)
        self.result = result
        self.reason = reason


def _fake_lint(monkeypatch, shared, calls=None):
    """The documented rule: `<base>ubuntu0.YY.MM.1` when the base version
    is in several releases (`shared`), `<base>ubuntu0.1` otherwise."""

    def check(context):
        entries = context["debian_changelog"]
        proposed = str(entries[0].version)
        series = entries[0].distributions.split("-")[0]  # jammy-security -> jammy
        base = str(entries[1].version)
        if calls is not None:
            calls.append(series)
        expected = f"{base}ubuntu0.{RELEASE[series]}.1" if series in shared else f"{base}ubuntu0.1"
        if proposed != expected:
            raise _LintError("fail", f"{proposed} does not match expected version {expected}")

    monkeypatch.setattr(
        checks,
        "ubuntu_lint",
        types.SimpleNamespace(
            Context=lambda **kw: kw,
            check_sru_version_string_convention=check,
            LintException=_LintError,
            LintResult=types.SimpleNamespace(FAIL="fail", SKIP="skip"),
        ),
    )
    monkeypatch.setattr(checks, "debian_changelog", debian_changelog)
    monkeypatch.setattr(checks, "lint_handles_dotted_revisions", lambda: False)


@pytest.fixture(autouse=True)
def _archive(monkeypatch):
    attachments.reset_cache()
    monkeypatch.setattr(archive_lookup, "supported_series_ordered", lambda lp: SERIES)
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "tenacious")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {s: ARCHIVE[s] for s in series_names or []},
    )
    monkeypatch.setattr(archive_lookup, "published_source", lambda lp, pkg, series, v: series)
    monkeypatch.setattr(
        archive_lookup,
        "changelog_text",
        lambda series: (
            f"lua-bitop ({ARCHIVE[series]}) {series}; urgency=medium\n\n"
            "  * Old.\n\n -- A B <a@b.com>  Wed, 01 Jun 2026 10:27:27 +0200\n"
        ),
    )


def _debdiff(series, version):
    return f"""\
diff -Nru lua-bitop-1.0.2/debian/changelog lua-bitop-1.0.2/debian/changelog
--- lua-bitop-1.0.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ lua-bitop-1.0.2/debian/changelog\t2026-11-10 10:00:00.000000000 +0200
@@ -1,3 +1,9 @@
+lua-bitop ({version}) {series}-security; urgency=medium
+
+  * SECURITY UPDATE: stack buffer over-read (LP: #2173901)
+
+ -- A B <a@b.com>  Tue, 10 Nov 2026 10:00:00 +0200
+
 lua-bitop ({ARCHIVE[series]}) {series}; urgency=medium
"""


def _bug(versions):
    atts = [
        FakeAttachment(f"{series}.debdiff", type="Patch", content=_debdiff(series, version))
        for series, version in versions.items()
    ]
    return FakeBug(attachments=atts), atts


def _check13(bug, atts, series):
    att = next(a for a in atts if a.title == f"{series}.debdiff")
    with attachments.focused(bug, att, attachments.attachment_text(att)):
        return checks.check_sru_version_suffix_convention(URL, bug, _Client())


LIVE = {"jammy": "1.0.2-5ubuntu0.22.04.1", "noble": "1.0.2-7ubuntu0.24.04.1"}


def test_the_live_shape_is_not_flagged(monkeypatch):
    _fake_lint(monkeypatch, shared={"jammy"})
    bug, atts = _bug(LIVE)
    assert _check13(bug, atts, "noble") is False
    assert _check13(bug, atts, "jammy") is False  # required there, as before


def test_without_a_series_that_needs_it_noble_is_still_flagged(monkeypatch):
    # Neither base version is shared: the per-release form is needed nowhere.
    _fake_lint(monkeypatch, shared=set())
    bug, atts = _bug(LIVE)
    assert isinstance(_check13(bug, atts, "noble"), checks.Finding)


def test_an_inconsistent_scheme_is_still_flagged(monkeypatch):
    _fake_lint(monkeypatch, shared={"jammy"})
    bug, atts = _bug({"jammy": "1.0.2-5ubuntu0.1", "noble": "1.0.2-7ubuntu0.24.04.1"})
    assert isinstance(_check13(bug, atts, "noble"), checks.Finding)


def test_a_single_series_bug_is_unchanged(monkeypatch):
    _fake_lint(monkeypatch, shared={"jammy"})
    bug, atts = _bug({"noble": "1.0.2-7ubuntu0.24.04.1"})
    assert isinstance(_check13(bug, atts, "noble"), checks.Finding)


def test_a_failed_lookup_for_the_other_series_is_inconclusive(monkeypatch):
    _fake_lint(monkeypatch, shared={"jammy"})
    bug, atts = _bug(LIVE)
    real = archive_lookup.changelog_text
    monkeypatch.setattr(
        archive_lookup, "changelog_text", lambda s: None if s == "jammy" else real(s)
    )
    assert _check13(bug, atts, "noble") is None
