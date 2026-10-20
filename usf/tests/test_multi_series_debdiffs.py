"""#154: a bug with debdiffs for several series is reviewed per series.

Trigger: openblas bug #2169719 (resolute + stonking debdiffs) -- every
bug-side check read only the newest debdiff, so the resolute one, the actual
SRU, was never checked. Now Checks 5-16 run once per series, on that
series' newest debdiff, and the comment is grouped by series (seb128's
layout: "=== series (file) ===" blocks, findings about the bug as a whole
first).
"""

import pytest
from fakes import FakeAttachment, FakeBug, FakeLLM, FakeTask, FakeTriageClient

import archive_lookup
import attachments
import checks
import main
from state import StateManager

URL = "https://launchpad.net/bugs/2169719"
SERIES = [("noble", "24.04"), ("resolute", "26.04"), ("stonking", "26.10")]


def _debdiff(suite, version, note="Fix things."):
    return f"""\
diff -Nru foo-1.2/debian/changelog foo-1.2/debian/changelog
--- foo-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ foo-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,7 @@
+foo ({version}) {suite}; urgency=medium
+
+  * {note}
+
+ -- Dev <dev@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 foo (1.2-3) {suite}; urgency=medium
"""


_PLAIN_PATCH = """\
--- a/src/foo.c
+++ b/src/foo.c
@@ -1 +1 @@
-old
+new
"""


def _att(name, content):
    return FakeAttachment(name, type="Patch", content=content)


@pytest.fixture(autouse=True)
def _series(monkeypatch):
    monkeypatch.setattr(archive_lookup, "supported_series_ordered", lambda lp: SERIES)
    attachments.reset_cache()


# --- choosing the debdiffs ----------------------------------------------------


def test_debdiffs_for_two_series_are_both_reviewed_oldest_series_first():
    bug = FakeBug(
        attachments=[
            _att("devel.debdiff", _debdiff("stonking", "1.2-4ubuntu1")),
            _att("stable.debdiff", _debdiff("resolute-proposed", "1.2-3ubuntu0.1")),
        ]
    )

    targets = checks.bug_review_targets(bug)

    assert [(t.label, t.series, t.version) for t in targets] == [
        ("resolute (stable.debdiff)", "resolute", "1.2-3ubuntu0.1"),
        ("stonking (devel.debdiff)", "stonking", "1.2-4ubuntu1"),
    ]


def test_one_series_keeps_the_single_target_path():
    bug = FakeBug(
        attachments=[
            _att("v1.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1")),
            _att("v2.debdiff", _debdiff("resolute", "1.2-3ubuntu0.2")),
        ]
    )
    assert checks.bug_review_targets(bug) == []


def test_a_newer_iteration_replaces_the_older_one_for_its_series():
    bug = FakeBug(
        attachments=[
            _att("r1.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1")),
            _att("s1.debdiff", _debdiff("stonking", "1.2-4ubuntu1")),
            _att("r2.debdiff", _debdiff("resolute", "1.2-3ubuntu0.2")),
        ]
    )

    labels = [t.label for t in checks.bug_review_targets(bug)]

    assert labels == ["resolute (r2.debdiff)", "stonking (s1.debdiff)"]


def test_plain_patch_is_superseded_and_an_unreadable_suite_goes_by_filename():
    bug = FakeBug(
        attachments=[
            _att("upstream.patch", _PLAIN_PATCH),
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1")),
            _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1")),
            _att("wip.debdiff", _debdiff("UNRELEASED", "1.2-4ubuntu2")),
        ]
    )

    labels = [t.label for t in checks.bug_review_targets(bug)]

    assert labels == ["resolute (r.debdiff)", "stonking (s.debdiff)", "wip.debdiff"]


def test_an_unfetchable_attachment_is_inconclusive():
    bug = FakeBug(
        attachments=[
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1")),
            FakeAttachment("s.debdiff", type="Patch", fail_fetch=True),
        ]
    )
    assert checks.bug_review_targets(bug) is None


def test_focus_makes_review_target_answer_with_that_debdiff():
    older = _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1"))
    newer = _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1"))
    bug = FakeBug(attachments=[older, newer])

    with attachments.focused(bug, older, "focused text"):
        assert attachments.review_target(bug) == (older, "focused text")
    assert attachments.review_target(bug)[0] is newer


# --- the comment -------------------------------------------------------------


def test_comment_is_grouped_by_series():
    findings = [
        checks.Finding("incomplete", "Bug-wide problem."),
        checks.Finding("incomplete", "Resolute problem.", group="resolute (r.debdiff)"),
        checks.Finding("question", "Resolute nicety.", group="resolute (r.debdiff)"),
    ]
    groups = [
        ("noble (n.debdiff)", "Already uploaded as `foo 1.2-2ubuntu0.1`, nothing left to do."),
        ("resolute (r.debdiff)", None),
        ("stonking (s.debdiff)", None),
    ]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert out == (
        "Thanks for your contribution! The automated review spotted the following points:\n\n"
        "Needs fixing before this can be sponsored:\n\n* Bug-wide problem.\n\n"
        "=== noble (n.debdiff) ===\n\n"
        "Already uploaded as `foo 1.2-2ubuntu0.1`, nothing left to do.\n\n"
        "=== resolute (r.debdiff) ===\n\n"
        "Needs fixing before this can be sponsored:\n\n* Resolute problem.\n\n"
        "Nice to have (non-blocking -- none of these block the upload, but you may want "
        "to address them now, before a sponsor reviews this, or in a future "
        "contribution):\n\n* Resolute nicety.\n\n"
        "The bug status is being set to Incomplete while waiting. Once the points above "
        "are addressed, please set it back to New so the request re-enters the review queue!"
    )


# --- #164: an identical finding on several series isn't repeated verbatim ---


def test_an_identical_finding_on_a_later_series_is_shortened():
    """tigervnc bug #2161717: the XSBC-Original-Maintainer advisory fired
    identically on the noble and resolute debdiffs."""
    msg = "Missing XSBC-Original-Maintainer."
    findings = [
        checks.Finding("question", msg, kind="advisory", group="noble (n.debdiff)"),
        checks.Finding("question", msg, kind="advisory", group="resolute (r.debdiff)"),
    ]
    groups = [("noble (n.debdiff)", None), ("resolute (r.debdiff)", None)]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert (
        "=== noble (n.debdiff) ===\n\n"
        + (
            "Nice to have (non-blocking -- none of these block the upload, but you may want "
            "to address them now, before a sponsor reviews this, or in a future "
            f"contribution):\n\n* {msg}"
        )
        in out
    )
    assert (
        "=== resolute (r.debdiff) ===\n\n"
        + (
            "Nice to have (non-blocking -- none of these block the upload, but you may want "
            "to address them now, before a sponsor reviews this, or in a future "
            "contribution):\n\n* Same as noble."
        )
        in out
    )
    assert out.count(msg) == 1


def test_a_third_series_still_points_at_the_first_not_the_second():
    msg = "Missing XSBC-Original-Maintainer."
    findings = [
        checks.Finding("question", msg, kind="advisory", group="noble (n.debdiff)"),
        checks.Finding("question", msg, kind="advisory", group="resolute (r.debdiff)"),
        checks.Finding("question", msg, kind="advisory", group="stonking (s.debdiff)"),
    ]
    groups = [
        ("noble (n.debdiff)", None),
        ("resolute (r.debdiff)", None),
        ("stonking (s.debdiff)", None),
    ]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert out.count("Same as noble.") == 2
    assert "Same as resolute" not in out


def test_findings_with_different_text_are_not_merged():
    findings = [
        checks.Finding("question", "Noble-specific point.", kind="advisory", group="noble (n.d)"),
        checks.Finding(
            "question", "Resolute-specific point.", kind="advisory", group="resolute (r.d)"
        ),
    ]
    groups = [("noble (n.d)", None), ("resolute (r.d)", None)]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert "Same as" not in out
    assert "Noble-specific point." in out and "Resolute-specific point." in out


def test_a_filename_only_label_is_referenced_by_its_filename():
    """A series-less group (an unreadable suite or UNRELEASED, #154) has no
    "series (" prefix to strip -- the whole label is the reference."""
    msg = "Same text, no series known for either."
    findings = [
        checks.Finding("question", msg, kind="advisory", group="first.debdiff"),
        checks.Finding("question", msg, kind="advisory", group="second.debdiff"),
    ]
    groups = [("first.debdiff", None), ("second.debdiff", None)]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert "Same as first.debdiff." in out


def test_only_bug_level_findings_render_without_series_blocks():
    """#156, systemd bug #2170538: one bug-level bullet over two clean
    series used to come with two "Nothing to fix." blocks."""
    findings = [checks.Finding("question", "Newer series first.", kind="verify")]
    groups = [("jammy (j.debdiff)", None), ("noble (n.debdiff)", None)]

    out = checks.render_findings_comment(findings, for_bug=True, groups=groups)

    assert "===" not in out
    assert out == checks.render_findings_comment(findings, for_bug=True)


def test_a_single_group_renders_as_before():
    findings = [checks.Finding("incomplete", "Problem.")]
    assert checks.render_findings_comment(
        findings, groups=[("resolute (r.debdiff)", None)]
    ) == checks.render_findings_comment(findings)


def test_already_uploaded_comment_names_every_upload():
    one = checks.already_uploaded_comment([("foo", "1.2-3ubuntu0.1")])
    several = checks.already_uploaded_comment([("foo", "1.2-3ubuntu0.1"), ("foo", "1.2-4ubuntu1")])

    assert "this change was already uploaded to the archive as `foo 1.2-3ubuntu0.1`, so" in one
    assert (
        "these changes were already uploaded to the archive as `foo 1.2-3ubuntu0.1` "
        "and `foo 1.2-4ubuntu1`, so there is nothing left to sponsor here." in several
    )
    assert several.endswith(
        "https://launchpad.net/ubuntu/+source/foo/1.2-3ubuntu0.1\n"
        "https://launchpad.net/ubuntu/+source/foo/1.2-4ubuntu1"
    )


# --- end to end: main.triage_url with the checks stubbed --------------------
#
# Every check is stubbed to "nothing found"; two react to the debdiff in
# focus: Check 16 flags one whose changelog says BAD, Check 6 reports one
# saying UPLOADED as already in the archive. That exercises main's per-series
# loop, focus, grouping, the closing rules and the Incomplete scoping.

_QUIET = [
    "check_administrative_state",
    "check_nothing_to_sponsor",
    "check_target_branch",
    "check_mp_conflicts",
    "check_empty_diff",
    "check_changelog_bug_reference",
    "check_direct_source_edit",
    "check_missing_changelog_stanza",
    "check_patch_not_debdiff",
    "check_ppa_version_suffix",
    "check_sru_version_suffix_convention",
    "check_sru_version_newer_series_precedence",
    "check_no_change_rebuild_version",
    "check_xsbc_original_maintainer",
    "check_released_changelog_edit",
    "check_sru_newer_series",
    "check_human_engaged",
]


class _LPDevel:
    def __init__(self):
        import types

        self.distributions = {
            "ubuntu": types.SimpleNamespace(current_series=types.SimpleNamespace(name="stonking"))
        }


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    for name in _QUIET:
        monkeypatch.setattr(checks, name, lambda *a, **k: False)
    reviewed = []

    def focused_text(lp_obj):
        target = attachments.review_target(lp_obj)
        reviewed.append(target[0].title)
        return target[1]

    def dep3(url, lp_obj, lp_client):
        if "BAD" in focused_text(lp_obj):
            return checks.Finding("incomplete", "Add a DEP-3 header.")
        return False

    def stale(url, lp_obj, lp_client):
        return "done" if "UPLOADED" in focused_text(lp_obj) else False

    monkeypatch.setattr(checks, "check_dep3_patch_header", dep3)
    monkeypatch.setattr(checks, "check_stale_version", stale)

    def run(bug, llm=None):
        sm = StateManager(db_path=str(tmp_path / "state.db"))
        lp = FakeTriageClient(objects={URL: bug}, lp=_LPDevel())
        main.triage_url(URL, sm, lp, llm or FakeLLM())
        return lp, sm.get_status(URL)[0]

    run.reviewed = reviewed
    return run


def _bug(resolute_note, stonking_note):
    return FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "New"),
            FakeTask("foo (Ubuntu Resolute)", "New"),
        ],
        description="SRU",
        attachments=[
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1", resolute_note)),
            _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1", stonking_note)),
        ],
    )


def _statuses(bug):
    return {t.bug_target_name: t.status for t in bug.bug_tasks}


def test_both_series_reviewed_and_only_the_blocked_one_bounced(pipeline):
    bug = _bug("BAD", "fine")

    lp, status = pipeline(bug)

    assert pipeline.reviewed.count("r.debdiff") >= 1  # the older, stable debdiff too
    assert len(lp.comments) == 1
    comment = lp.comments[0]
    assert "=== resolute (r.debdiff) ===\n\nNeeds fixing before this can be sponsored:" in comment
    assert "* Add a DEP-3 header." in comment
    # #156: the clean series gets no block.
    assert "stonking" not in comment
    assert "point above is addressed" in comment
    assert _statuses(bug) == {"foo (Ubuntu)": "New", "foo (Ubuntu Resolute)": "Incomplete"}
    assert status == "WAITING_ON_CONTRIBUTOR"


def test_an_uploaded_series_drops_out_and_the_rest_is_reviewed(pipeline):
    bug = _bug("UPLOADED", "BAD")

    lp, status = pipeline(bug)

    comment = lp.comments[0]
    assert (
        "=== resolute (r.debdiff) ===\n\nAlready uploaded as `foo 1.2-3ubuntu0.1`, "
        "nothing left to do for this series." in comment
    )
    assert "=== stonking (s.debdiff) ===\n\nNeeds fixing" in comment
    assert getattr(lp, "unsubscribed", 0) == 0
    assert _statuses(bug) == {"foo (Ubuntu)": "Incomplete", "foo (Ubuntu Resolute)": "New"}
    assert status == "WAITING_ON_CONTRIBUTOR"


def test_every_series_uploaded_closes_the_bug_once(pipeline):
    bug = _bug("UPLOADED", "UPLOADED")

    lp, status = pipeline(bug)

    assert len(lp.comments) == 1
    assert "`foo 1.2-3ubuntu0.1` and `foo 1.2-4ubuntu1`" in lp.comments[0]
    assert lp.unsubscribed == 1
    assert status == "DONE"


def test_every_series_clean_posts_nothing(pipeline):
    lp, status = pipeline(_bug("fine", "fine"))

    assert lp.comments == []
    assert status == "READY_FOR_HUMAN"


def test_a_bug_wide_blocker_bounces_every_series_under_review(pipeline):
    bug = _bug("fine", "fine")
    llm = FakeLLM(bug_result=("INCOMPLETE", "The SRU template is incomplete."))

    lp, _status = pipeline(bug, llm)

    comment = lp.comments[0]
    assert "* The SRU template is incomplete." in comment
    assert "===" not in comment  # both series clean: no blocks (#156)
    assert _statuses(bug) == {"foo (Ubuntu)": "Incomplete", "foo (Ubuntu Resolute)": "Incomplete"}


def test_a_single_series_bug_is_unchanged(pipeline):
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Resolute)", "New")],
        attachments=[
            _att("old.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1", "fine")),
            _att("new.debdiff", _debdiff("resolute", "1.2-3ubuntu0.2", "BAD")),
        ],
    )

    lp, _status = pipeline(bug)

    assert "===" not in lp.comments[0]
    assert "* Add a DEP-3 header." in lp.comments[0]
    assert "old.debdiff" not in pipeline.reviewed


# --- #157: debdiffs for closed or unsupported series aren't reviewed ---------


def test_debdiff_for_a_fix_released_series_is_dropped():
    """gnutls28 bug #2150202: bounced over the stonking debdiff's outdated
    version while the stonking task was Fix Released."""
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu)", "Fix Released"),
            FakeTask("foo (Ubuntu Noble)", "In Progress"),
            FakeTask("foo (Ubuntu Resolute)", "In Progress"),
            FakeTask("foo (Ubuntu Stonking)", "Fix Released"),
        ],
        attachments=[
            _att("n.debdiff", _debdiff("noble", "1.2-3ubuntu0.1")),
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.2")),
            _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1")),
        ],
    )

    labels = [t.label for t in checks.bug_review_targets(bug)]

    assert labels == ["noble (n.debdiff)", "resolute (r.debdiff)"]


def test_plain_ubuntu_task_stands_for_devel():
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu)", "Fix Released"), FakeTask("foo (Ubuntu Noble)", "New")],
        attachments=[
            _att("n.debdiff", _debdiff("noble", "1.2-3ubuntu0.1")),
            _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1")),
        ],
    )

    # One series left: still the per-target path, so the dropped stonking
    # debdiff (the newest) isn't what gets reviewed.
    assert [t.label for t in checks.bug_review_targets(bug)] == ["noble (n.debdiff)"]


def test_debdiff_for_an_unsupported_series_is_dropped():
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Noble)", "New"), FakeTask("foo (Ubuntu Resolute)", "New")],
        attachments=[
            _att("q.debdiff", _debdiff("questing", "1.2-3ubuntu0.5")),
            _att("n.debdiff", _debdiff("noble", "1.2-3ubuntu0.1")),
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.2")),
        ],
    )

    labels = [t.label for t in checks.bug_review_targets(bug)]

    assert labels == ["noble (n.debdiff)", "resolute (r.debdiff)"]


def test_every_debdiff_dropped_means_nothing_to_review():
    bug = FakeBug(
        tasks=[FakeTask("foo (Ubuntu Stonking)", "Fix Released")],
        attachments=[_att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1"))],
    )
    assert checks.bug_review_targets(bug) is False


def test_closed_series_debdiff_is_not_reviewed_end_to_end(pipeline):
    bug = FakeBug(
        tasks=[
            FakeTask("foo (Ubuntu Resolute)", "New"),
            FakeTask("foo (Ubuntu Stonking)", "Fix Released"),
        ],
        attachments=[
            _att("r.debdiff", _debdiff("resolute", "1.2-3ubuntu0.1", "fine")),
            _att("s.debdiff", _debdiff("stonking", "1.2-4ubuntu1", "BAD")),
        ],
    )

    lp, status = pipeline(bug)

    assert "s.debdiff" not in pipeline.reviewed
    assert lp.comments == []
    assert status == "READY_FOR_HUMAN"
