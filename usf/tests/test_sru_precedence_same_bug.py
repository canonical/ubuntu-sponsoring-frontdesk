"""#175: Check 14 reads the other series' debdiffs on the same bug.

Trigger: tp-smapi bug #2173763, debdiffs for resolute and stonking
(`0.45-1ubuntu2` each) and tenacious (`0.45-1.1ubuntu1`), all three series
publishing `0.45-1ubuntu1`. Check 14 said the fix hadn't landed in the
newer series, though tenacious's own debdiff was right there with a
version sorting above -- a linked MP already counted (#124), a debdiff
didn't. And it missed the real problem: resolute and stonking proposed the
same version, which can only be uploaded once.
"""

import pytest
from fakes import FakeAttachment, FakeBug
from test_sru_version_precedence import _LP, _patch_archive

import attachments
import checks

URL = "https://launchpad.net/bugs/2173763"
SERIES = [("resolute", "26.04"), ("stonking", "26.10"), ("tenacious", "27.04")]
PUBLISHED = {
    "resolute": "0.45-1ubuntu1",
    "stonking": "0.45-1ubuntu1",
    "tenacious": "0.45-1ubuntu1",
}


def _debdiff(series, version):
    return f"""\
diff -Nru tp-smapi-0.45/debian/changelog tp-smapi-0.45/debian/changelog
--- tp-smapi-0.45/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ tp-smapi-0.45/debian/changelog\t2026-11-10 10:00:00.000000000 +0200
@@ -1,3 +1,9 @@
+tp-smapi ({version}) {series}; urgency=medium
+
+  * Build against kernels 7.2+ (LP: #2173763)
+
+ -- A B <a@b.com>  Tue, 10 Nov 2026 10:00:00 +0200
+
 tp-smapi ({PUBLISHED[series]}) {series}; urgency=medium
"""


@pytest.fixture(autouse=True)
def _archive(monkeypatch):
    attachments.reset_cache()
    _patch_archive(monkeypatch, devel="tenacious", series_order=SERIES, versions=PUBLISHED)


def _bug(versions, unreadable=()):
    atts = [
        FakeAttachment(
            f"{series}.patch",
            type="Patch",
            content=_debdiff(series, version),
            fail_fetch=series in unreadable,
        )
        for series, version in versions.items()
    ]
    return FakeBug(attachments=atts), atts


def _check14(bug, atts, series):
    att = next(a for a in atts if a.title == f"{series}.patch")
    with attachments.focused(bug, att, attachments.attachment_text(att)):
        return checks.check_sru_version_newer_series_precedence(URL, bug, _LP())


LIVE = {"resolute": "0.45-1ubuntu2", "stonking": "0.45-1ubuntu2", "tenacious": "0.45-1.1ubuntu1"}


def test_the_live_shape_reports_the_shared_version_not_an_unlanded_fix():
    bug, atts = _bug(LIVE)
    finding = _check14(bug, atts, "resolute")
    assert finding.tier == "incomplete"
    assert finding.message == (
        "The proposed version can't be used:\n"
        "- `0.45-1ubuntu2` is also proposed for `stonking` on this bug -- a version can "
        "only be uploaded once across all series, so each series' upload needs its own\n"
        "Please pick a different version."
    )
    stonking = _check14(bug, atts, "stonking")
    assert "is also proposed for `resolute` on this bug" in stonking.message
    assert "hasn't landed" not in stonking.message


def test_newer_debdiffs_sorting_above_count_as_in_flight():
    # Versions that don't extend the published base (#124's own skip), so
    # only the same-bug debdiffs can cover the newer series.
    bug, atts = _bug(
        {"resolute": "0.45-1ubuntu2", "stonking": "0.45-1ubuntu3", "tenacious": "0.45-1.1ubuntu1"}
    )
    assert _check14(bug, atts, "resolute") is False
    assert _check14(bug, atts, "stonking") is False


def test_a_devel_debdiff_sorting_below_still_leaves_it_unlanded():
    # 0.45-1ubuntu1.1 < 0.45-1ubuntu2: uploading both would still break
    # the upgrade from stonking to tenacious.
    bug, atts = _bug({"stonking": "0.45-1ubuntu2", "tenacious": "0.45-1ubuntu1.1"})
    finding = _check14(bug, atts, "stonking")
    assert "`tenacious` (the development release) is still at `0.45-1ubuntu1`" in finding.message


def test_without_a_devel_debdiff_it_is_unlanded_as_before():
    bug, atts = _bug({"resolute": "0.45-1ubuntu2"})
    finding = _check14(bug, atts, "resolute")
    assert "`stonking` is still at `0.45-1ubuntu1`" in finding.message
    assert "`tenacious` (the development release) is still at" in finding.message


def test_an_unreadable_other_debdiff_is_inconclusive():
    bug, atts = _bug(LIVE, unreadable=("tenacious",))
    assert _check14(bug, atts, "resolute") is None
