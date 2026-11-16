"""#182 / Check 19: a development-style version for a released series.

Trigger: network-manager bug #2174507. A debdiff titled "debdiff for devel"
bumped `1.58.1-1ubuntu3` to `1.58.1-1ubuntu4` -- a development-release
upload -- but its changelog said `stonking`, the series `dch` copied from the
previous entry. The bot reviewed it as a stonking SRU: Check 13 (SRU version
convention), Check 14 (not landed in tenacious) and Check 7 (no evidence in
tenacious) each restated the one mistake from a different angle.
"""

import pytest
from fakes import FakeAttachment, FakeBug, FakeLLM, FakeRoot, FakeTask, FakeTriageClient

import archive_lookup
import attachments
import checks
import main
from state import StateManager

URL = "https://launchpad.net/bugs/2174507"
SERIES = [("noble", "24.04"), ("resolute", "26.04"), ("stonking", "26.10"), ("tenacious", "27.04")]

WORDING = (
    "The changelog entry targets `stonking`, a released series, but "
    "`1.58.1-1ubuntu4` is numbered like a development-release upload. If this is "
    "meant for the development release, the changelog should target `tenacious` "
    "instead (`dch` reuses the previous entry's series, an easy mistake to make). "
    "If it's a stable release update for `stonking`, it needs an SRU version and "
    "the SRU process. See "
    "https://ubuntu.com/project/docs/how-ubuntu-is-made/concepts/version-strings/"
)


def _debdiff(suite, version):
    return f"""\
diff -Nru network-manager-1.58.1/debian/changelog network-manager-1.58.1/debian/changelog
--- network-manager-1.58.1/debian/changelog\t2026-11-01 10:00:00.000000000 +0000
+++ network-manager-1.58.1/debian/changelog\t2026-11-15 10:00:00.000000000 +0000
@@ -1,3 +1,9 @@
+network-manager ({version}) {suite}; urgency=medium
+
+  * Install the wifi powersave default under /usr/lib (LP: #2174507)
+
+ -- A B <a@b.com>  Sun, 15 Nov 2026 10:00:00 +0000
+
 network-manager (1.58.1-1ubuntu3) stonking; urgency=medium
diff -Nru network-manager-1.58.1/debian/rules network-manager-1.58.1/debian/rules
--- network-manager-1.58.1/debian/rules	2026-11-01 10:00:00.000000000 +0000
+++ network-manager-1.58.1/debian/rules	2026-11-15 10:00:00.000000000 +0000
@@ -1 +1 @@
-old
+new
"""


@pytest.fixture(autouse=True)
def _archive(monkeypatch):
    attachments.reset_cache()
    monkeypatch.setattr(archive_lookup, "supported_series_ordered", lambda lp: SERIES)
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "tenacious")


def _bug(suite, version):
    return FakeBug(
        tasks=[FakeTask("network-manager (Ubuntu)", "New")],
        attachments=[
            FakeAttachment("debdiff for devel", type="Patch", content=_debdiff(suite, version))
        ],
    )


def _check(suite, version):
    bug = _bug(suite, version)
    return checks.check_devel_version_on_released_series(
        URL, bug, FakeTriageClient(objects={URL: bug}, lp=FakeRoot())
    )


def test_the_live_shape():
    finding = _check("stonking", "1.58.1-1ubuntu4")
    assert finding.tier == "incomplete"
    assert finding.message == WORDING


@pytest.mark.parametrize(
    "suite, version",
    [
        ("tenacious", "1.58.1-1ubuntu4"),  # the development release itself
        ("stonking", "1.58.1-1ubuntu3.1"),  # a regular SRU version
        ("stonking", "1.58.1-1ubuntu0.1"),
        ("stonking", "1.58.1-1ubuntu3.26.10.1"),
        ("stonking", "1.58.1-1build1"),  # not an ubuntuN revision
        ("unreleased", "1.58.1-1ubuntu4"),  # not a known series
    ],
)
def test_other_shapes_do_not_fire(suite, version):
    assert _check(suite, version) is False


def test_a_pocket_suffix_is_read_as_its_series():
    assert _check("stonking-proposed", "1.58.1-1ubuntu4").tier == "incomplete"


def test_end_to_end_it_replaces_the_sru_findings(tmp_path, monkeypatch):
    bug = _bug("stonking", "1.58.1-1ubuntu4")
    lp = FakeTriageClient(objects={URL: bug}, lp=FakeRoot())
    sm = StateManager(db_path=str(tmp_path / "s.db"))
    llm = FakeLLM()
    # Stub the archive-backed checks that would otherwise need real lookups.
    monkeypatch.setattr(checks, "check_stale_version", lambda *a, **k: False)
    monkeypatch.setattr(checks, "check_changelog_bug_reference", lambda *a, **k: False)
    monkeypatch.setattr(checks, "check_sru_version_suffix_convention", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(checks, "check_sru_version_newer_series_precedence", lambda *a, **k: 1 / 0)

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert WORDING in lp.comments[0]
    assert "SRU policy requires" not in lp.comments[0]  # no Check 7/14 restatement
    assert not getattr(llm, "newer_series_queries", [])  # Check 7 skipped
