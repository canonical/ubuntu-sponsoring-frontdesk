"""
Tightened SRU/sync detection in llm_reviewer (replaces brittle exact-substring
matching). The detectors decide which review a bug is routed to, so they must
tolerate the casing/whitespace submitters actually use without misrouting
unrelated bugs.
"""

from llm_reviewer import _is_sru, _is_sync

# --- SRU detection ----------------------------------------------------------


def test_sru_canonical_header():
    assert _is_sru([], "[Impact]\nThe bug crashes foo.") is True


def test_sru_header_case_and_whitespace_insensitive():
    # The old `"[Impact]" in description` missed all of these.
    assert _is_sru([], "[ impact ]\n...") is True
    assert _is_sru([], "[IMPACT]\n...") is True


def test_sru_current_and_legacy_section_names():
    assert _is_sru([], "[Where problems could occur]\n...") is True
    assert _is_sru([], "[Test Plan]\n...") is True
    assert _is_sru([], "[Test Case]\n...") is True
    assert _is_sru([], "[Regression Potential]\n...") is True


def test_sru_tag_case_insensitive():
    assert _is_sru(["SRU"], "nothing templated here") is True


def test_non_sru_plain_bug_not_matched():
    assert _is_sru(["bitesize"], "Please fix the typo in the about dialog.") is False


def test_sru_unbracketed_mention_not_matched():
    # Prose mentioning impact must not trip the SRU router; only the bracketed
    # template header counts.
    assert _is_sru([], "This has a big impact on users.") is False


# --- Sync detection ---------------------------------------------------------


def test_sync_title():
    title = "Sync foo 1.2-3 (main) from Debian unstable"
    assert _is_sync(title, "") is True


def test_sync_title_case_insensitive():
    assert _is_sync("sync bar 2.0 from debian experimental", "") is True


def test_sync_body_markers():
    assert _is_sync("", "Please sync this package.") is True
    assert _is_sync("", "This is a sync request for bar.") is True


def test_sync_body_case_insensitive():
    assert _is_sync("", "please SYNC bar from somewhere") is True


def test_non_sync_bug_not_matched():
    assert _is_sync("Crash on startup", "The app segfaults immediately.") is False


# --- #125: an FFe for devel is not an SRU -------------------------------------


class _Task:
    def __init__(self, name, status="In Progress"):
        self.bug_target_name = name
        self.status = status


class _MP:
    def __init__(self, target):
        self.target_git_path = target
        self.queue_status = "Needs review"


class _Bug:
    resource_type_link = "https://api.launchpad.net/devel/#bug"

    def __init__(self, tasks, mps=(), description="", tags=()):
        self.bug_tasks = tasks
        self.linked_merge_proposals = list(mps)
        self.description = description
        self.title = "backport-iwlwifi-dkms FFe"
        self.tags = list(tags)


_FFE_TEXT = "## FFE ##\n\n[Rationale]\n\n* x\n\n[ Regression Potential ]\n\n* y\n"


def test_ffe_text_alone_still_looks_sru_shaped():
    # The regex can't tell them apart -- FFe templates carry the same
    # "[ Regression Potential ]" section (live: bug #2159856).
    assert _is_sru([], _FFE_TEXT) is True


def test_devel_only_bug_is_not_an_sru():
    from llm_reviewer import _targets_only_devel

    bug = _Bug(
        [
            _Task("backport-iwlwifi-dkms (Ubuntu)"),
            _Task("backport-iwlwifi-dkms (Ubuntu Stonking)"),
        ],
        mps=[_MP("refs/heads/ubuntu/devel")],
        description=_FFE_TEXT,
    )
    assert _targets_only_devel(bug, "stonking") is True


def test_stable_series_task_makes_it_an_sru():
    from llm_reviewer import _targets_only_devel

    bug = _Bug([_Task("foo (Ubuntu)"), _Task("foo (Ubuntu Noble)")])
    assert _targets_only_devel(bug, "stonking") is False


def test_closed_stable_task_does_not_count():
    # A released noble task isn't an open SRU ask.
    from llm_reviewer import _targets_only_devel

    bug = _Bug([_Task("foo (Ubuntu Noble)", status="Fix Released")])
    assert _targets_only_devel(bug, "stonking") is True


def test_stable_targeted_mp_makes_it_an_sru():
    from llm_reviewer import _targets_only_devel

    bug = _Bug([_Task("foo (Ubuntu)")], mps=[_MP("refs/heads/ubuntu/noble-devel")])
    assert _targets_only_devel(bug, "stonking") is False


def test_unknown_devel_does_not_suppress():
    from llm_reviewer import _targets_only_devel

    assert _targets_only_devel(_Bug([_Task("foo (Ubuntu)")]), None) is False


def test_triage_bug_skips_the_sru_template_review_for_a_devel_ffe():
    import types

    import llm_reviewer

    r = llm_reviewer.LLMReviewer()
    r.lp = types.SimpleNamespace(
        distributions={
            "ubuntu": types.SimpleNamespace(current_series=types.SimpleNamespace(name="stonking"))
        }
    )

    def _boom(description):
        raise AssertionError("SRU review must not run")

    r.review_sru_template = _boom
    bug = _Bug(
        [_Task("foo (Ubuntu)"), _Task("foo (Ubuntu Stonking)")],
        mps=[_MP("refs/heads/ubuntu/devel")],
        description=_FFE_TEXT,
    )
    status, _ = r.triage_bug(bug)
    assert status == "READY_FOR_HUMAN"


def test_sru_tag_wins_over_devel_only_metadata(monkeypatch):
    import types

    import llm_reviewer

    r = llm_reviewer.LLMReviewer()
    r.lp = types.SimpleNamespace(
        distributions={
            "ubuntu": types.SimpleNamespace(current_series=types.SimpleNamespace(name="stonking"))
        }
    )
    called = []
    r.review_sru_template = lambda description: (called.append(1), (True, ""))[1]
    bug = _Bug([_Task("foo (Ubuntu)")], description=_FFE_TEXT, tags=["sru"])
    r.triage_bug(bug)
    assert called == [1]


# --- #125 follow-up: debdiff suite and inactive MPs ---------------------------

_NOBLE_DEBDIFF = """\
diff -Nru foo-1.2/debian/changelog foo-1.2/debian/changelog
--- foo-1.2/debian/changelog\t2026-06-01 10:00:00.000000000 +0200
+++ foo-1.2/debian/changelog\t2026-07-11 10:00:00.000000000 +0200
@@ -1,3 +1,7 @@
+foo (1.2-3ubuntu0.1) noble; urgency=medium
+
+  * Fix things.
+
+ -- Dev <dev@example.com>  Fri, 10 Jul 2026 10:00:00 +0200
+
 foo (1.2-3) noble; urgency=medium
"""


def test_debdiff_targeting_a_stable_series_makes_it_an_sru():
    from fakes import FakeAttachment, FakeBug

    import attachments
    from llm_reviewer import _targets_only_devel

    attachments.reset_cache()
    bug = FakeBug(
        description=_FFE_TEXT,
        attachments=[FakeAttachment("fix.debdiff", type="Patch", content=_NOBLE_DEBDIFF)],
    )
    assert _targets_only_devel(bug, "stonking") is False


def test_debdiff_targeting_devel_stays_devel_only():
    from fakes import FakeAttachment, FakeBug

    import attachments
    from llm_reviewer import _targets_only_devel

    attachments.reset_cache()
    bug = FakeBug(
        description=_FFE_TEXT,
        attachments=[
            FakeAttachment(
                "fix.debdiff",
                type="Patch",
                content=_NOBLE_DEBDIFF.replace("noble", "stonking"),
            )
        ],
    )
    assert _targets_only_devel(bug, "stonking") is True


def test_rejected_stable_mp_does_not_count():
    from llm_reviewer import _targets_only_devel

    mp = _MP("refs/heads/ubuntu/noble-devel")
    mp.queue_status = "Rejected"
    bug = _Bug([_Task("foo (Ubuntu)")], mps=[mp])
    assert _targets_only_devel(bug, "stonking") is True
