"""Check 7 (#58): an SRU must show the fix landed in newer series first --
devel plus every supported stable series newer than the target -- via the
bug's task table, a linked MP per series, or a series-named patch. When the
metadata shows nothing, the LLM checks whether the bug text says it's fixed."""

import types

from fakes import (
    FakeAttachment,
    FakeBug,
    FakeDistribution,
    FakeLLM,
    FakeMP,
    FakeSeries,
    FakeTask,
)

import archive_lookup
import checks

# jammy < noble < resolute (stable) < stonking (devel); focal EOL.
_SERIES_TABLE = [
    FakeSeries("stonking", "26.10", "Active Development"),
    FakeSeries("resolute", "26.04", "Current Stable Release"),
    FakeSeries("noble", "24.04", "Supported"),
    FakeSeries("jammy", "22.04", "Supported"),
    FakeSeries("focal", "20.04", "Obsolete"),
]


class _LP:
    def __init__(self):
        self.lp = types.SimpleNamespace(
            distributions={
                "ubuntu": FakeDistribution(devel_series_name="stonking", series=_SERIES_TABLE)
            }
        )


def _sru_mp(bugs, target="refs/heads/ubuntu/noble-devel"):
    return FakeMP(target=target, bugs=bugs)


def _bug(tasks, **kwargs):
    return FakeBug(tasks=tasks, **kwargs)


# --- archive_lookup.supported_series_ordered ---------------------------------


def test_supported_series_ordered_oldest_first_devel_last():
    # #83: sourced from ubuntu-distro-info (real EOL dates), not Launchpad
    # series statuses (which lag EOL -- questing -- and count ESM). The
    # conftest fixture pins the command output; `lp` is unused.
    assert archive_lookup.supported_series_ordered(None) == [
        ("jammy", "22.04"),
        ("noble", "24.04"),
        ("resolute", "26.04"),
        ("stonking", "26.10"),
    ]


def test_supported_series_ordered_strips_the_lts_qualifier():
    assert ("jammy", "22.04") in archive_lookup.supported_series_ordered(None)


def test_supported_series_ordered_none_on_failure(monkeypatch):
    def _boom(*args):
        raise FileNotFoundError("ubuntu-distro-info not installed")

    monkeypatch.setattr(archive_lookup, "_distro_info", _boom)
    assert archive_lookup.supported_series_ordered(None) is None


def test_supported_series_ordered_none_on_misaligned_output(monkeypatch):
    monkeypatch.setattr(
        archive_lookup,
        "_distro_info",
        lambda *args: "jammy\nnoble\n" if args == ("--supported",) else "22.04 LTS\n",
    )
    assert archive_lookup.supported_series_ordered(None) is None


# --- not-an-SRU exits (no Launchpad lookups needed) ---------------------------


def test_mp_targeting_a_pocket_branch_is_an_sru(monkeypatch):
    # #84: ubuntu/jammy-updates names jammy just as ubuntu/jammy-devel does.
    bug = _bug([FakeTask("testpkg (Ubuntu Jammy)", "In Progress")])
    mp = _sru_mp([bug], target="refs/heads/ubuntu/jammy-updates")
    finding = checks.check_sru_newer_series("url", mp, _LP(), FakeLLM())
    assert finding and "noble, resolute, stonking" in finding.message


def test_mp_targeting_devel_is_not_an_sru():
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    # lp_client deliberately without a usable .lp: proves no lookup happens.
    lp = types.SimpleNamespace(lp=None)
    assert checks.check_sru_newer_series("url", mp, lp, FakeLLM()) is False


def test_bug_without_series_task_is_not_an_sru():
    bug = _bug([FakeTask("testpkg (Ubuntu)", "New")])
    lp = types.SimpleNamespace(lp=None)
    assert checks.check_sru_newer_series("url", bug, lp, FakeLLM()) is False


def test_mp_targeting_the_devel_series_by_name_is_not_an_sru():
    mp = _sru_mp([], target="refs/heads/ubuntu/stonking-devel")
    assert checks.check_sru_newer_series("url", mp, _LP(), FakeLLM()) is False


# --- mechanically handled newer series (no LLM call) --------------------------


def test_all_newer_series_fix_released_is_clean():
    bug = _bug(
        [
            FakeTask("testpkg (Ubuntu)", "Fix Released"),
            FakeTask("testpkg (Ubuntu Resolute)", "Fix Committed"),
            FakeTask("testpkg (Ubuntu Noble)", "In Progress"),
        ]
    )
    llm = FakeLLM()
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), llm) is False
    assert getattr(llm, "newer_series_queries", []) == []


def test_linked_mps_per_series_count_as_handled():
    # The 'one bug, three MPs' shape: devel + resolute covered by sibling MPs.
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Noble)", "In Progress")],
        linked_merge_proposals=[
            FakeMP(target="refs/heads/ubuntu/devel"),
            FakeMP(target="refs/heads/ubuntu/resolute-devel"),
        ],
    )
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), FakeLLM()) is False


def test_series_named_patch_attachment_counts_as_handled():
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Noble)", "In Progress")],
        attachments=[
            FakeAttachment("fix-resolute.debdiff", type="Patch"),
            FakeAttachment("fix-stonking.debdiff", type="Patch"),
        ],
    )
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), FakeLLM()) is False


_STONKING_DEBDIFF = """\
diff -Nru testpkg-1.2/debian/changelog testpkg-1.2/debian/changelog
--- testpkg-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,7 @@
+testpkg (1.2-4ubuntu2) stonking; urgency=medium
+
+  * Fix things.
+
+ -- Dev <dev@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-4ubuntu1) resolute; urgency=medium
"""


def test_series_attachment_without_a_series_named_filename_still_counts():
    # Live regression (#97, neutron bug #2150285): the debdiff's filename is
    # named after the bug number and version, not the series ('lp2150285_
    # 28.0.0-0ubuntu2.debdiff') -- the changelog stanza's own suite field
    # ('stonking') is what actually tells us where it targets.
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Resolute)", "In Progress")],
        attachments=[
            FakeAttachment(
                "lp2150285_1.2-4ubuntu2.debdiff",
                type="Patch",
                content=_STONKING_DEBDIFF,
            ),
        ],
    )
    assert checks.check_sru_newer_series("url", bug, _LP(), FakeLLM()) is False


def test_series_attachment_content_does_not_match_the_wrong_series():
    # Control: the stonking debdiff above doesn't also cover a still-open
    # noble ask that nothing addresses.
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Noble)", "In Progress")],
        attachments=[
            FakeAttachment(
                "lp2150285_1.2-4ubuntu2.debdiff",
                type="Patch",
                content=_STONKING_DEBDIFF,
            ),
        ],
    )
    finding = checks.check_sru_newer_series("url", bug, _LP(), FakeLLM())
    assert finding and "resolute" in finding.message
    assert "stonking" not in finding.message


_RESOLUTE_DEBDIFF = """\
diff -Nru testpkg-1.2/debian/changelog testpkg-1.2/debian/changelog
--- testpkg-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ testpkg-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,7 @@
+testpkg (1.2-4ubuntu1) resolute; urgency=medium
+
+  * Fix things.
+
+ -- Dev <dev@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3) resolute; urgency=medium
"""


def test_no_series_task_but_sru_shaped_attachment_is_still_evaluated():
    # #115, found live (v4l2-relayd bug #2166611): the bug's only task was
    # the plain 'testpkg (Ubuntu)' -- no series-specific task at all -- but
    # the attached debdiff's changelog stanza named a stable series
    # (resolute) directly. Must still ask whether stonking (newer) got the
    # fix first, not silently pass as "not an SRU" the way the bare task
    # alone would suggest.
    bug = _bug(
        [FakeTask("testpkg (Ubuntu)", "Confirmed")],
        attachments=[FakeAttachment("fix.debdiff", type="Patch", content=_RESOLUTE_DEBDIFF)],
    )
    finding = checks.check_sru_newer_series("url", bug, _LP(), FakeLLM())
    assert finding and "stonking" in finding.message


def test_no_series_task_and_no_sru_shaped_attachment_is_still_not_an_sru():
    bug = _bug(
        [FakeTask("testpkg (Ubuntu)", "Confirmed")],
        attachments=[FakeAttachment("notes.txt", type="Unspecified", content="just some notes")],
    )
    assert checks.check_sru_newer_series("url", bug, _LP(), FakeLLM()) is False


def test_no_series_task_attachment_fetch_failure_is_inconclusive():
    bug = _bug(
        [FakeTask("testpkg (Ubuntu)", "Confirmed")],
        attachments=[FakeAttachment("fix.debdiff", type="Patch", fail_fetch=True)],
    )
    assert checks.check_sru_newer_series("url", bug, _LP(), FakeLLM()) is None


# --- the removed-package exemption (#69) --------------------------------------


class _SeriesArchive:
    """getPublishedSources honoring distro_series: the package is published
    only in the given series names."""

    def __init__(self, present):
        self.present = present

    def getPublishedSources(self, source_name, exact_match, distro_series, status):
        from fakes import FakePublication

        if distro_series.name in self.present:
            return [FakePublication("1.0-1")]
        return []


def _lp_with_archive(present):
    lp = _LP()
    lp.lp.distributions["ubuntu"].main_archive = _SeriesArchive(present)
    return lp


def _open_sru_bug():
    return _bug([FakeTask("testpkg (Ubuntu Noble)", "In Progress")])


def test_package_removed_from_all_newer_series_skips_silently():
    # u-boot-nezha (bug #2148507): removed after noble, so no fix can or
    # need land in resolute/stonking. Deterministic -- no LLM question.
    llm = FakeLLM()
    lp = _lp_with_archive({"noble"})
    assert checks.check_sru_newer_series("url", _sru_mp([_open_sru_bug()]), lp, llm) is False
    assert getattr(llm, "newer_series_queries", []) == []


def test_partial_removal_only_asks_about_remaining_series():
    # Present in noble and stonking, removed from resolute: only stonking
    # is still unhandled, and only it reaches the LLM and the advisory.
    llm = FakeLLM()
    lp = _lp_with_archive({"noble", "stonking"})
    finding = checks.check_sru_newer_series("url", _sru_mp([_open_sru_bug()]), lp, llm)
    assert "stonking" in finding.message
    assert "resolute" not in finding.message
    assert llm.newer_series_queries[0][1] == ["stonking (Ubuntu 26.10)"]


def test_absence_from_target_series_disables_the_exemption():
    # Package published nowhere (an introduction, or a lookup blind spot):
    # absence can't be read as removal, fall through to the LLM path.
    llm = FakeLLM()
    lp = _lp_with_archive(set())
    finding = checks.check_sru_newer_series("url", _sru_mp([_open_sru_bug()]), lp, llm)
    assert "resolute, stonking" in finding.message


# --- unhandled newer series: the LLM escape hatch ------------------------------


def test_unhandled_newer_series_fires_the_advisory():
    bug = _bug(
        [
            FakeTask("testpkg (Ubuntu)", "New"),
            FakeTask("testpkg (Ubuntu Noble)", "In Progress"),
        ]
    )
    llm = FakeLLM()
    finding = checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), llm)
    # #85: kind="verify" (renders under "Please verify", not "Nice to
    # have" -- the wording is assertive that this must be checked, which
    # the advisory bucket's "non-blocking... nice to have" framing
    # contradicted).
    assert finding.tier == "question" and finding.kind == "verify"
    assert "resolute, stonking" in finding.message
    assert "SRU policy" in finding.message
    # The LLM was asked exactly about the unhandled series, labeled with
    # their release versions (codenames postdate its training data).
    assert llm.newer_series_queries[0][1] == [
        "resolute (Ubuntu 26.04)",
        "stonking (Ubuntu 26.10)",
    ]


def test_bug_text_saying_fixed_skips_silently():
    # The bug text already tells reviewers the newer series are covered;
    # fixing the task table needs series-nomination rights most
    # contributors don't have, so there is nothing to ask of the
    # submitter (seb128, live on bug #2148507).
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Noble)", "In Progress")],
        description="0.4.13 in resolute and later carries the workaround.",
    )
    llm = FakeLLM()
    llm.fixed_in_newer = True
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), llm) is False


def test_llm_failure_is_inconclusive():
    bug = _bug([FakeTask("testpkg (Ubuntu Noble)", "In Progress")])
    llm = FakeLLM()
    llm.fixed_in_newer = None
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), llm) is None


# --- bug-side items -----------------------------------------------------------


def test_bug_side_sru_uses_oldest_open_series_as_target():
    # jammy is the deepest target; noble is covered by an open task's MP,
    # resolute/stonking by nothing -> advisory names only those two.
    bug = _bug(
        [
            FakeTask("testpkg (Ubuntu Jammy)", "In Progress"),
            FakeTask("testpkg (Ubuntu Noble)", "Fix Committed"),
        ]
    )
    llm = FakeLLM()
    finding = checks.check_sru_newer_series("url", bug, _LP(), llm)
    assert finding.tier == "question"
    assert llm.newer_series_queries[0][1] == [
        "resolute (Ubuntu 26.04)",
        "stonking (Ubuntu 26.10)",
    ]


def test_bug_side_all_newer_closed_is_clean():
    bug = _bug(
        [
            FakeTask("testpkg (Ubuntu)", "Fix Released"),
            FakeTask("testpkg (Ubuntu Resolute)", "Fix Released"),
            FakeTask("testpkg (Ubuntu Noble)", "Confirmed"),
        ]
    )
    assert checks.check_sru_newer_series("url", bug, _LP(), FakeLLM()) is False


# --- lookup failures are inconclusive -----------------------------------------


def test_series_table_lookup_failure_is_inconclusive(monkeypatch):
    bug = _bug([FakeTask("testpkg (Ubuntu Noble)", "In Progress")])

    # #83: the series table comes from ubuntu-distro-info; a failing
    # command must read as "can't determine", not "no newer series".
    def _boom(*args):
        raise FileNotFoundError("ubuntu-distro-info not installed")

    monkeypatch.setattr(archive_lookup, "_distro_info", _boom)
    assert checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), FakeLLM()) is None


def test_unreadable_linked_bugs_is_inconclusive():
    class _RaisingBugs:
        resource_type_link = FakeMP.resource_type_link
        target_git_path = "refs/heads/ubuntu/noble-devel"
        self_link = FakeMP(target="x").self_link
        web_link = FakeMP(target="x").web_link

        @property
        def bugs(self):
            raise RuntimeError("network blip")

    assert checks.check_sru_newer_series("url", _RaisingBugs(), _LP(), FakeLLM()) is None


def test_sru_to_eol_series_is_skipped():
    bug = _bug([FakeTask("testpkg (Ubuntu Focal)", "In Progress")])
    mp = _sru_mp([bug], target="refs/heads/ubuntu/focal-devel")
    assert checks.check_sru_newer_series("url", mp, _LP(), FakeLLM()) is False
