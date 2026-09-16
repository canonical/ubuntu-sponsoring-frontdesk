"""Check 14: SRU version-precedence correctness (design_journal.md #110).

Two independently checked problems, both provable from real archive state
rather than a recommended string pattern (contrast
check_sru_version_suffix_convention, #109, which is advisory):
1. every series newer than the target must currently publish a HIGHER
   version than the one proposed;
2. the proposed version must never have been published in any OTHER
   series' history (Ubuntu's archive pool is shared across series).
"""

from fakes import FakeAttachment, FakeBug, FakeDiff, FakeMP

import archive_lookup
import checks

URL = "url"

_SERIES_ORDER = [
    ("jammy", "22.04"),
    ("noble", "24.04"),
    ("resolute", "25.10"),
    ("stonking", "26.10"),
]


class _LP:
    def __init__(self):
        self.lp = None


class _Pub:
    def __init__(self, series):
        self.series = series


def _patch_archive(
    monkeypatch,
    devel="stonking",
    series_order=_SERIES_ORDER,
    series_order_fails=False,
    versions=None,
    fail_series_lookup_for=(),
    publications=None,
    publications_fail=False,
):
    """versions: {series_name: version_string} for series that DO have a
    publication; anything else comes back as {} (nothing published,
    "skip" -- a real archive_lookup.ubuntu_versions() behaviour, not a
    failure). fail_series_lookup_for: series names whose lookup should
    return None (a genuine Launchpad failure)."""
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: devel)
    if series_order_fails:
        monkeypatch.setattr(archive_lookup, "supported_series_ordered", lambda lp: None)
    else:
        monkeypatch.setattr(archive_lookup, "supported_series_ordered", lambda lp: series_order)

    versions = versions or {}

    def fake_ubuntu_versions(lp, pkg, series_names=None):
        series = series_names[0]
        if series in fail_series_lookup_for:
            return None
        if series in versions:
            return {series: versions[series]}
        return {}

    monkeypatch.setattr(archive_lookup, "ubuntu_versions", fake_ubuntu_versions)

    if publications_fail:
        monkeypatch.setattr(
            archive_lookup, "any_series_publication", lambda lp, pkg, version, status=None: None
        )
    else:
        monkeypatch.setattr(
            archive_lookup,
            "any_series_publication",
            lambda lp, pkg, version, status=None: publications or [],
        )
    monkeypatch.setattr(archive_lookup, "publication_series_name", lambda pub: pub.series)


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


def test_clean_case_is_false(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


def test_newer_series_exactly_matching_is_not_leg_one(monkeypatch):
    # #116, live-found: equality is deliberately NOT leg 1's territory --
    # a newer series at exactly the proposed version means that version
    # is already published there, which is leg 2's "reused elsewhere"
    # case (more precise wording), not "fix not landed yet". With no
    # leg-2 publication configured, this is clean.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-3ubuntu0.1", "stonking": "1.3-0ubuntu1"},
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


def test_newer_series_exactly_matching_and_actually_published_is_leg_two(monkeypatch):
    # Same versions as above, but this time resolute's exact-match
    # version really is on record as a publication there -- now it's a
    # genuine "reused elsewhere" problem, reported by leg 2.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-3ubuntu0.1", "stonking": "1.3-0ubuntu1"},
        publications=[_Pub("resolute")],
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    finding = checks.check_sru_version_newer_series_precedence(URL, mp, _LP())
    assert finding.tier == "incomplete"
    assert "resolute" in finding.message
    assert "reused" in finding.message


def test_newer_series_lower_bounces(monkeypatch):
    # stonking hasn't diverged from the shared base at all -- strictly
    # lower than the proposed SRU version, the exact scenario that
    # motivated #110. #116: this is airtight proof the fix hasn't
    # landed in devel yet, so the message says so directly.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.2-3"},
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    finding = checks.check_sru_version_newer_series_precedence(URL, mp, _LP())
    assert finding.tier == "incomplete"
    assert "stonking" in finding.message
    assert "development release" in finding.message
    assert "hasn't been uploaded there yet" in finding.message


def test_intermediate_stable_series_lower_is_not_called_devel(monkeypatch):
    # #116: only the actual devel series (last in series_names) gets the
    # "(the development release)" label -- an intermediate stable series
    # being behind gets the same "hasn't landed" wording, but must not
    # be mislabeled as devel.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-3", "stonking": "1.3-0ubuntu1"},
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    finding = checks.check_sru_version_newer_series_precedence(URL, mp, _LP())
    assert finding.tier == "incomplete"
    assert "`resolute` is still at" in finding.message
    assert "development release" not in finding.message


def test_newer_series_with_nothing_published_is_skipped(monkeypatch):
    # Neither resolute nor stonking has ever carried this package
    # (removed/never synced) -- not a version-ordering problem.
    _patch_archive(monkeypatch, versions={})
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


def test_version_reused_in_another_series_bounces(monkeypatch):
    # The exact scenario from the conversation: stonking used this exact
    # version once, then moved past it via a Debian sync -- invisible to
    # the newer-series-ordering leg alone.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
        publications=[_Pub("stonking")],
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    finding = checks.check_sru_version_newer_series_precedence(URL, mp, _LP())
    assert finding.tier == "incomplete"
    assert "stonking" in finding.message
    assert "1.2-3ubuntu0.1" in finding.message


def test_version_match_in_target_series_itself_is_not_reported(monkeypatch):
    # A match in the TARGET series is check_stale_version's own territory
    # (already-uploaded / version-collision) -- not this check's to
    # duplicate.
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
        publications=[_Pub("noble")],
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


def test_target_series_not_supported_is_false(monkeypatch):
    _patch_archive(monkeypatch, series_order=[("jammy", "22.04")])
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


def test_series_order_lookup_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, series_order_fails=True)
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is None


def test_newer_series_lookup_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, fail_series_lookup_for={"resolute"})
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is None


def test_any_series_publication_failure_is_inconclusive(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
        publications_fail=True,
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is None


def test_publication_series_unresolvable_is_inconclusive(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
        publications=[_Pub(None)],
    )
    mp = _mp_with_diff(_diff_for("1.2-3ubuntu0.1"))
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is None


def test_devel_targeted_mp_still_checks_cross_series_reuse(monkeypatch):
    # No SRU-shape gate (checks.py's own docstring explains why): leg 1
    # is naturally a no-op for devel (nothing is newer), but leg 2 still
    # matters -- reusing a version from elsewhere in the archive is
    # always wrong, devel or not.
    _patch_archive(monkeypatch, publications=[_Pub("noble")])
    mp = _mp_with_diff(
        _diff_for("1.2-3ubuntu1", suite="stonking"), target="refs/heads/ubuntu/devel"
    )
    finding = checks.check_sru_version_newer_series_precedence(URL, mp, _LP())
    assert finding.tier == "incomplete"
    assert "noble" in finding.message


def test_unfetchable_diff_is_inconclusive():
    mp = FakeMP(diff=None, target="refs/heads/ubuntu/noble-devel")
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is None


def test_no_new_stanza_is_false():
    mp = _mp_with_diff("diff --git a/debian/control b/debian/control\n")
    assert checks.check_sru_version_newer_series_precedence(URL, mp, _LP()) is False


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


def test_bug_version_reused_in_another_series_bounces(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
        publications=[_Pub("stonking")],
    )
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", content=_DEBDIFF_CONVENTIONAL)],
    )
    finding = checks.check_sru_version_newer_series_precedence(URL, bug, _LP())
    assert finding.tier == "incomplete"
    assert "stonking" in finding.message


def test_bug_clean_case_is_false(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"resolute": "1.2-4ubuntu1", "stonking": "1.3-0ubuntu1"},
    )
    bug = FakeBug(
        title="fix",
        attachments=[FakeAttachment("fix.debdiff", content=_DEBDIFF_CONVENTIONAL)],
    )
    assert checks.check_sru_version_newer_series_precedence(URL, bug, _LP()) is False
