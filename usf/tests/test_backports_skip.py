"""#172: uploads targeting a -backports pocket are out of scope, for now.

Trigger: git-diff-html bug #2173887, no-change backports of a tenacious
package to noble/resolute/stonking, suites `<series>-backports`, versions
`~YY.MM.1`. Check 15 bounced all three as botched no-change rebuilds, the
bug was treated as an SRU (template review, Check 7). The backports
process has its own rules the checks don't model; until it's documented
well enough to check, the bot stays out -- nothing posted, nothing saved.
"""

import pytest
from fakes import (
    CLEAN_DIFF_TEXT,
    FakeAttachment,
    FakeBug,
    FakeDiff,
    FakeLLM,
    FakeMP,
    FakeTask,
    FakeTriageClient,
)

import attachments
import checks
import main
from state import StateManager

URL = "https://launchpad.net/bugs/2173887"


def _debdiff(suite, version):
    return f"""\
diff -Nru git-diff-html-1.2.1/debian/changelog git-diff-html-1.2.1/debian/changelog
--- git-diff-html-1.2.1/debian/changelog\t2026-10-29 19:34:49.000000000 +0800
+++ git-diff-html-1.2.1/debian/changelog\t2026-11-11 02:05:36.000000000 +0800
@@ -1,3 +1,9 @@
+git-diff-html ({version}) {suite}; urgency=medium
+
+  * No-change backport.
+
+ -- Lu Haoran <luhaoran1981@icloud.com>  Wed, 11 Nov 2026 02:05:36 +0800
+
 git-diff-html (1.2.1-1) unstable; urgency=medium
"""


def _att(name, content):
    return FakeAttachment(name, type="Patch", content=content)


def _bug(*atts):
    return FakeBug(
        attachments=list(atts),
        tasks=[FakeTask("git-diff-html (Ubuntu Noble)", "New")],
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    attachments.reset_cache()


# --- detection ------------------------------------------------------------------


def test_the_live_shape_is_a_backport():
    bug = _bug(
        _att("noble", _debdiff("noble-backports", "1.2.1-1~24.04.1")),
        _att("resolute", _debdiff("resolute-backports", "1.2.1-1~26.04.1")),
        _att("stonking", _debdiff("stonking-backports", "1.2.1-1~26.10.1")),
    )
    assert checks.is_backports_upload(bug) is True


@pytest.mark.parametrize("suite", ["noble", "noble-proposed", "noble-updates", "tenacious"])
def test_other_pockets_are_not(suite):
    bug = _bug(_att("x", _debdiff(suite, "1.2.1-1ubuntu0.1")))
    assert checks.is_backports_upload(bug) is False


def test_one_backports_debdiff_among_others_puts_the_item_out():
    bug = _bug(
        _att("sru", _debdiff("noble", "1.2.1-1ubuntu0.1")),
        _att("bpo", _debdiff("resolute-backports", "1.2.1-1~26.04.1")),
    )
    assert checks.is_backports_upload(bug) is True


def test_a_replaced_backports_iteration_does_not_count():
    # The contributor first targeted noble-backports, then attached a
    # regular SRU debdiff for noble: only the newest per series counts.
    bug = _bug(
        _att("first", _debdiff("noble-backports", "1.2.1-1~24.04.1")),
        _att("second", _debdiff("noble", "1.2.1-1ubuntu0.1")),
    )
    assert checks.is_backports_upload(bug) is False


def test_an_unreadable_attachment_does_not_count():
    # The checks that follow read the same attachment and go inconclusive.
    bug = _bug(FakeAttachment("x", type="Patch", fail_fetch=True))
    assert checks.is_backports_upload(bug) is False


def _mp_diff(suite):
    # An MP's preview diff is a git diff; the new entry is found the same way.
    return _debdiff(suite, "1.2.1-1~24.04.1").replace(
        "diff -Nru git-diff-html-1.2.1/debian/changelog git-diff-html-1.2.1/debian/changelog",
        "diff --git a/debian/changelog b/debian/changelog",
    )


@pytest.mark.parametrize("suite, expected", [("noble-backports", True), ("noble", False)])
def test_a_merge_proposal_reads_its_new_entry(suite, expected):
    mp = FakeMP(
        target=".../ubuntu/noble-devel", diff=FakeDiff("/d/1", 50, diff_text=_mp_diff(suite))
    )
    assert checks.is_backports_upload(mp) is expected


def test_a_merge_proposal_without_a_new_entry_is_not():
    mp = FakeMP(diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT))
    assert checks.is_backports_upload(mp) is False


# --- main.py ----------------------------------------------------------------------


def test_a_backport_is_skipped_without_posting_or_saving(tmp_path):
    bug = _bug(
        _att("noble", _debdiff("noble-backports", "1.2.1-1~24.04.1")),
        _att("stonking", _debdiff("stonking-backports", "1.2.1-1~26.10.1")),
    )
    lp = FakeTriageClient(objects={URL: bug})
    sm = StateManager(db_path=str(tmp_path / "s.db"))
    llm = FakeLLM()

    main.triage_url(URL, sm, lp, llm)

    assert lp.comments == [] and lp.votes == []
    assert sm.get_status(URL) is None  # nothing saved: re-examined next pass
    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["outcome"] == "skipped-backports"
    assert row["extra"]["findings"] == []
    assert not getattr(llm, "newer_series_queries", [])  # no Check 7 LLM call
