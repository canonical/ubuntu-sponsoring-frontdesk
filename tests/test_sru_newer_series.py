"""Check 7 (#58): an SRU must show the fix landed in newer series first --
devel plus every supported stable series newer than the target -- via the
bug's task table, a linked MP per series, or a series-named patch. When the
metadata shows nothing, the LLM checks whether the bug text says it's fixed."""

import types

import archive_lookup
import checks
from fakes import (
    FakeAttachment,
    FakeBug,
    FakeDistribution,
    FakeLLM,
    FakeMP,
    FakeSeries,
    FakeTask,
)


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
                "ubuntu": FakeDistribution(
                    devel_series_name="stonking", series=_SERIES_TABLE
                )
            }
        )


def _sru_mp(bugs, target="refs/heads/ubuntu/noble-devel"):
    return FakeMP(target=target, bugs=bugs)


def _bug(tasks, **kwargs):
    return FakeBug(tasks=tasks, **kwargs)


# --- archive_lookup.supported_series_ordered ---------------------------------


def test_supported_series_ordered_oldest_first_devel_last():
    lp = _LP().lp
    assert archive_lookup.supported_series_ordered(lp) == [
        ("jammy", "22.04"),
        ("noble", "24.04"),
        ("resolute", "26.04"),
        ("stonking", "26.10"),
    ]


def test_supported_series_ordered_none_on_failure():
    lp = types.SimpleNamespace(distributions={})
    assert archive_lookup.supported_series_ordered(lp) is None


# --- not-an-SRU exits (no Launchpad lookups needed) ---------------------------


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
    assert (
        checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), FakeLLM()) is False
    )


def test_series_named_patch_attachment_counts_as_handled():
    bug = _bug(
        [FakeTask("testpkg (Ubuntu Noble)", "In Progress")],
        attachments=[
            FakeAttachment("fix-resolute.debdiff", type="Patch"),
            FakeAttachment("fix-stonking.debdiff", type="Patch"),
        ],
    )
    assert (
        checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), FakeLLM()) is False
    )


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
    assert finding.tier == "question" and finding.kind == "advisory"
    assert "resolute, stonking" in finding.message
    assert "SRU requirements" in finding.message
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


def test_series_table_lookup_failure_is_inconclusive():
    bug = _bug([FakeTask("testpkg (Ubuntu Noble)", "In Progress")])
    # series=None: iterating the series table raises inside archive_lookup.
    broken = types.SimpleNamespace(
        lp=types.SimpleNamespace(
            distributions={
                "ubuntu": types.SimpleNamespace(
                    current_series=FakeSeries("stonking"), series=None
                )
            }
        )
    )
    assert (
        checks.check_sru_newer_series("url", _sru_mp([bug]), broken, FakeLLM()) is None
    )


def test_unreadable_linked_bugs_is_inconclusive():
    class _RaisingBugs:
        resource_type_link = FakeMP.resource_type_link
        target_git_path = "refs/heads/ubuntu/noble-devel"
        self_link = FakeMP(target="x").self_link
        web_link = FakeMP(target="x").web_link

        @property
        def bugs(self):
            raise RuntimeError("network blip")

    assert (
        checks.check_sru_newer_series("url", _RaisingBugs(), _LP(), FakeLLM()) is None
    )


def test_sru_to_eol_series_is_skipped():
    bug = _bug([FakeTask("testpkg (Ubuntu Focal)", "In Progress")])
    mp = _sru_mp([bug], target="refs/heads/ubuntu/focal-devel")
    assert checks.check_sru_newer_series("url", mp, _LP(), FakeLLM()) is False
