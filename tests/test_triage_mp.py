"""Design #47: LLM content review for merge proposals -- changelog stanza
quality, changelog-vs-diff consistency, and a Feature Freeze classification.
All findings are advisory (question tier): they never block, never vote, and
any parsing doubt means silence, not noise."""

import datetime

import llm_reviewer
import main
import release_schedule
from state import StateManager
from fakes import CLEAN_DIFF_TEXT, FakeBug, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

URL = "https://code.launchpad.net/~marco/+merge/12345"

# A realistic small merge diff: new changelog stanza, a control change the
# stanza mentions, and one upstream file (whose content must NOT reach the
# LLM -- only its path).
MERGE_DIFF = """\
diff --git a/debian/changelog b/debian/changelog
index 111..222 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,10 @@
+testpkg (1.2-3ubuntu1) stonking; urgency=medium
+
+  * Merge with Debian unstable (LP: #2000001). Remaining changes:
+    - Set Ubuntu maintainer.
+
+ -- Marco <marco@example.com>  Mon, 06 Jul 2026 10:00:00 +0200
+
 testpkg (1.2-3) unstable; urgency=medium

   * Upstream fix.
diff --git a/debian/control b/debian/control
index 333..444 100644
--- a/debian/control
+++ b/debian/control
@@ -1,2 +1,2 @@
-Maintainer: Debian Team <d@example.org>
+Maintainer: Ubuntu Developers <u@example.com>
diff --git a/src/secretcode.c b/src/secretcode.c
index 555..666 100644
--- a/src/secretcode.c
+++ b/src/secretcode.c
@@ -1 +1 @@
-int old;
+int upstream_secret_content;
"""

EXPECTED_STANZA = """\
testpkg (1.2-3ubuntu1) stonking; urgency=medium

  * Merge with Debian unstable (LP: #2000001). Remaining changes:
    - Set Ubuntu maintainer.

 -- Marco <marco@example.com>  Mon, 06 Jul 2026 10:00:00 +0200"""


def _reply(verdict="pass", feature="no", observations=None, mismatches=None):
    obs = "".join(f"  - {o}\n" for o in (observations or []))
    mis = "".join(f"  - {m}\n" for m in (mismatches or []))
    return (
        "Some analysis prose.\n\n```yaml\n"
        f"verdict: {verdict}\nfeature: {feature}\n"
        f"observations:\n{obs}mismatches:\n{mis}```\n"
    )


class ScriptedReviewer(llm_reviewer.LLMReviewer):
    """LLMReviewer with a canned _query_llm reply; records prompts sent."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply
        self.prompts = []

    def _query_llm(self, prompt, model="high-complexity"):
        self.prompts.append(prompt)
        return self.reply


# --- diff preparation helpers (pure) ------------------------------------------


def test_new_changelog_stanza_extracted_without_diff_prefixes():
    assert llm_reviewer._new_changelog_stanza(MERGE_DIFF) == EXPECTED_STANZA


def test_no_added_stanza_means_none():
    assert llm_reviewer._new_changelog_stanza(CLEAN_DIFF_TEXT) is None


def test_edit_to_existing_stanza_is_not_a_new_stanza():
    # An added header immediately followed by context lines: the diff edits
    # an existing entry rather than adding a complete new stanza.
    diff = (
        "diff --git a/debian/changelog b/debian/changelog\n"
        "@@ -1,3 +1,3 @@\n"
        "+testpkg (1.2-3ubuntu1) stonking; urgency=medium\n"
        " \n"
        "   * Existing bullet.\n"
    )
    assert llm_reviewer._new_changelog_stanza(diff) is None


def test_split_debian_diff_keeps_debian_lists_the_rest():
    debian_part, other = llm_reviewer._split_debian_diff(MERGE_DIFF)
    assert "debian/changelog" in debian_part
    assert "debian/control" in debian_part
    assert "upstream_secret_content" not in debian_part
    assert other == ["src/secretcode.c"]


# --- verdict-block parsing (fail-safe = silence) -------------------------------


def test_parse_pass_yields_no_observations():
    r = ScriptedReviewer("")
    assert r._extract_mp_review(_reply()) == ([], False)


def test_parse_fail_yields_observations_and_feature():
    bullets, feature = ScriptedReviewer("")._extract_mp_review(
        _reply(verdict="fail", feature="yes", observations=["First.", "Second."])
    )
    assert bullets == [("advisory", "First."), ("advisory", "Second.")]
    assert feature is True


def test_parse_fail_yields_mismatches_as_verify_kind():
    bullets, _ = ScriptedReviewer("")._extract_mp_review(
        _reply(verdict="fail", mismatches=["Diff doesn't match stanza."])
    )
    assert bullets == [("verify", "Diff doesn't match stanza.")]


def test_parse_fail_without_observations_is_silent():
    bullets, _ = ScriptedReviewer("")._extract_mp_review(_reply(verdict="fail"))
    assert bullets == []


def test_parse_fail_drops_unquoted_colon_hash_observation():
    # An unquoted bullet like "The stanza claims LP: #123 removes ..." gets
    # misparsed by YAML: "LP:" becomes a mapping key and "#123..." becomes a
    # comment, so the list item comes back as a dict, not a string. That
    # must be dropped, not stringified into a garbage bullet.
    text = (
        "```yaml\nverdict: fail\nobservations:\n"
        "  - The stanza claims LP: #123 removes something.\n"
        '  - "This one is fine."\n```\n'
    )
    bullets, _ = ScriptedReviewer("")._extract_mp_review(text)
    assert bullets == [("advisory", "This one is fine.")]


def test_parse_missing_or_malformed_block_is_silent():
    r = ScriptedReviewer("")
    assert r._extract_mp_review("no block at all") == ([], None)
    assert r._extract_mp_review("```yaml\n[not, a, mapping]\n```") == ([], None)


# --- triage_mp ----------------------------------------------------------------


def test_prompt_carries_stanza_and_debian_diff_not_upstream_content():
    r = ScriptedReviewer(_reply())
    status, _ = r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"
    prompt = r.prompts[0]
    assert EXPECTED_STANZA in prompt
    assert "Maintainer: Ubuntu Developers" in prompt
    assert "upstream_secret_content" not in prompt  # path only, not content
    assert "src/secretcode.c" in prompt
    assert "TRUNCATED" not in prompt


def test_advisory_returns_observation_list():
    r = ScriptedReviewer(_reply(verdict="fail", observations=["Vague bullet."]))
    status, payload = r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    assert status == "ADVISORY"
    assert payload == [("advisory", "Vague bullet.")]


def test_advisory_returns_mismatch_as_verify_kind():
    r = ScriptedReviewer(
        _reply(verdict="fail", mismatches=["Stanza claims X but diff shows Y."])
    )
    status, payload = r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    assert status == "ADVISORY"
    assert payload == [("verify", "Stanza claims X but diff shows Y.")]


def test_empty_or_unfetchable_diff_skips_without_llm_call():
    r = ScriptedReviewer(_reply())
    for diff_text in (None, False, ""):
        status, _ = r.triage_mp(FakeMP(), diff_text=diff_text)
        assert status == "READY_FOR_HUMAN"
    assert r.prompts == []


def test_no_new_stanza_skips_without_llm_call():
    r = ScriptedReviewer(_reply())
    status, _ = r.triage_mp(FakeMP(), diff_text=CLEAN_DIFF_TEXT)
    assert status == "READY_FOR_HUMAN"
    assert r.prompts == []


def test_oversized_debian_diff_is_truncated_with_note(monkeypatch):
    monkeypatch.setattr(llm_reviewer, "_MP_DIFF_CAP", 200)
    r = ScriptedReviewer(_reply())
    r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    prompt = r.prompts[0]
    assert "TRUNCATED" in prompt
    # The stanza itself is never truncated, even when the diff is.
    assert EXPECTED_STANZA in prompt


def _diff_with_many_upstream_files(paths):
    sections = "".join(
        f"diff --git a/{p} b/{p}\nindex 1..2 100644\n--- a/{p}\n+++ b/{p}\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        for p in paths
    )
    return MERGE_DIFF + sections


def test_oversized_path_list_collapses_vendored_files_first(monkeypatch):
    # rust-sequoia-sqv +merge/502068: 4600+ vendored paths blew the prompt
    # up for zero review signal. Vendored paths are collapsed to a count so
    # the interesting stray file still fits under the cap.
    monkeypatch.setattr(llm_reviewer, "_MP_OTHER_FILES_CAP", 5)
    paths = [f"rust-vendor/crate{i}/lib.rs" for i in range(20)] + ["src/real_edit.c"]
    r = ScriptedReviewer(_reply())
    r.triage_mp(FakeMP(), diff_text=_diff_with_many_upstream_files(paths))
    prompt = r.prompts[0]
    assert "src/real_edit.c" in prompt
    assert "rust-vendor/crate1/lib.rs" not in prompt
    assert "20 files under vendored directories" in prompt
    assert "Do not flag changes as missing or unmentioned" in prompt


def test_oversized_path_list_without_vendor_is_capped_with_count(monkeypatch):
    monkeypatch.setattr(llm_reviewer, "_MP_OTHER_FILES_CAP", 5)
    paths = [f"src/file{i:02d}.c" for i in range(9)]
    r = ScriptedReviewer(_reply())
    r.triage_mp(FakeMP(), diff_text=_diff_with_many_upstream_files(paths))
    prompt = r.prompts[0]
    assert "src/file00.c" in prompt
    # MERGE_DIFF's own src/secretcode.c is also in the list, so 10 paths
    # against a cap of 5 leaves 5 unlisted.
    assert "... and 5 more files (not listed)." in prompt
    assert "Do not flag changes as missing or unmentioned" in prompt


def test_small_path_list_is_sent_whole_without_notes():
    r = ScriptedReviewer(_reply())
    r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    prompt = r.prompts[0]
    assert "src/secretcode.c" in prompt
    assert "more files" not in prompt
    assert "vendored" not in prompt


def test_feature_after_freeze_adds_ffe_bullet(monkeypatch):
    monkeypatch.setattr(
        release_schedule,
        "FEATURE_FREEZE",
        datetime.date.today() - datetime.timedelta(days=1),
    )
    r = ScriptedReviewer(_reply(feature="yes"))
    status, payload = r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    assert status == "ADVISORY"
    assert len(payload) == 1
    kind, message = payload[0]
    assert kind == "verify"
    assert "Feature Freeze Exception" in message


def test_sru_targeted_mp_skips_the_ff_question_even_after_freeze(monkeypatch):
    # FF is a devel-series concept (#59): an SRU targeting a stable series
    # must not get the classification question -- or an FFe bullet -- no
    # matter the date. The model here even answers feature=yes; without a
    # `feature:` field requested, _extract_mp_review must not act on it.
    monkeypatch.setattr(
        release_schedule,
        "FEATURE_FREEZE",
        datetime.date.today() - datetime.timedelta(days=1),
    )
    r = ScriptedReviewer(_reply(feature="yes"))
    status, _ = r.triage_mp(
        FakeMP(target="refs/heads/ubuntu/noble-devel"), diff_text=MERGE_DIFF
    )
    assert status == "READY_FOR_HUMAN"
    assert "Feature Freeze" not in r.prompts[0]


def test_devel_series_named_by_codename_still_gets_the_ff_question(monkeypatch):
    # 'ubuntu/stonking-devel' names a series explicitly, but it IS the
    # current devel series -- not an SRU, the FF question still applies.
    import types

    monkeypatch.setattr(
        release_schedule,
        "FEATURE_FREEZE",
        datetime.date.today() - datetime.timedelta(days=1),
    )
    r = ScriptedReviewer(_reply(feature="yes"))
    r.lp = types.SimpleNamespace(
        distributions={
            "ubuntu": types.SimpleNamespace(
                current_series=types.SimpleNamespace(name="stonking")
            )
        }
    )
    status, payload = r.triage_mp(
        FakeMP(target="refs/heads/ubuntu/stonking-devel"), diff_text=MERGE_DIFF
    )
    assert status == "ADVISORY"
    assert "Feature Freeze Exception" in payload[0][1]


def test_feature_before_freeze_stays_quiet(monkeypatch):
    monkeypatch.setattr(
        release_schedule,
        "FEATURE_FREEZE",
        datetime.date.today() + datetime.timedelta(days=30),
    )
    r = ScriptedReviewer(_reply(feature="yes"))
    status, _ = r.triage_mp(FakeMP(), diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"


# --- end-to-end: ADVISORY -> question-tier findings ----------------------------


def _state(tmp_path):
    return StateManager(db_path=str(tmp_path / "state.db"))


def test_advisory_posts_nice_to_have_comment_no_vote(tmp_path):
    sm = _state(tmp_path)
    mp = FakeMP(diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={URL: mp})
    llm = FakeLLM(
        mp_result=(
            "ADVISORY",
            [("advisory", "Soft observation one."), ("advisory", "Two.")],
        )
    )

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert "Nice to have" in lp.comments[0]
    assert "* Soft observation one." in lp.comments[0]
    assert "* Two." in lp.comments[0]
    assert "Needs fixing" not in lp.comments[0]
    assert "Please verify" not in lp.comments[0]
    assert lp.votes == [None]
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_advisory_verify_kind_posts_please_verify_comment_no_vote(tmp_path):
    sm = _state(tmp_path)
    mp = FakeMP(diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={URL: mp})
    llm = FakeLLM(
        mp_result=("ADVISORY", [("verify", "Stanza claims X but diff shows Y.")])
    )

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert "Please verify" in lp.comments[0]
    assert "* Stanza claims X but diff shows Y." in lp.comments[0]
    assert "Nice to have" not in lp.comments[0]
    assert "Needs fixing" not in lp.comments[0]
    assert lp.votes == [None]
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_advisory_plus_blocking_finding_split_into_sections(tmp_path):
    sm = _state(tmp_path)
    # Merge conflicts (incomplete) + an LLM advisory: one comment, both
    # sections, and the vote/status driven by the blocking one.
    mp = FakeMP(diff=FakeDiff("/d/1", 50, conflicts="foo.c", diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={URL: mp})
    llm = FakeLLM(
        mp_result=("ADVISORY", [("advisory", "Consider clarifying the stanza.")])
    )

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert "Needs fixing" in lp.comments[0]
    assert "Nice to have" in lp.comments[0]
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


# --- SRU template check on the linked bug (#86) --------------------------------
# An SRU-shaped MP (target names a stable series, not devel) is bound by the
# same SRU template requirement as a bug-side SRU request -- but until #86,
# triage_mp never checked it: only triage_bug did, and that only runs when
# the BUG itself is the queue entry.


def _sru_template_reply(verdict="pass", reason=""):
    return f"```yaml\nverdict: {verdict}\nreason: {reason!r}\n```\n"


class QueuedReviewer(llm_reviewer.LLMReviewer):
    """LLMReviewer replaying a queue of canned _query_llm replies in order,
    one per call -- needed here because a single triage_mp invocation can
    make multiple LLM calls (the SRU template check(s), then the changelog
    review) that must answer differently."""

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.prompts = []

    def _query_llm(self, prompt, model="high-complexity"):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_non_sru_mp_skips_the_template_check_entirely():
    # ubuntu/devel target: not SRU-shaped, so no linked-bug lookup, no
    # extra LLM call -- only the changelog-review reply is consumed.
    r = ScriptedReviewer(_reply())
    mp = FakeMP(target="refs/heads/ubuntu/devel", bugs=[FakeBug(id=1)])
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"
    assert len(r.prompts) == 1


def test_sru_mp_without_a_linked_bug_skips_the_template_check():
    r = ScriptedReviewer(_reply())
    mp = FakeMP(target="refs/heads/ubuntu/noble-devel", bugs=[])
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"


def test_sru_mp_single_bug_failing_template_blocks_before_the_diff_review():
    r = QueuedReviewer([_sru_template_reply("fail", "Missing [Test Plan].")])
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel", bugs=[FakeBug(id=42, description="x")]
    )
    status, comment = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "INCOMPLETE"
    assert "Missing [Test Plan]" in comment
    assert "bug-template/#reference-sru-bug-template" in comment
    # Only the template-check call happened -- the changelog review (which
    # would need a second queued reply) never ran.
    assert len(r.prompts) == 1


def test_sru_mp_single_bug_passing_template_falls_through_to_the_diff_review():
    r = QueuedReviewer([_sru_template_reply("pass"), _reply()])
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel", bugs=[FakeBug(id=42, description="x")]
    )
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"
    assert len(r.prompts) == 2


def test_sru_mp_multiple_bugs_all_must_pass_failures_listed():
    # seb128: every linked bug's template must pass; list the ones that don't.
    r = QueuedReviewer(
        [
            _sru_template_reply("pass"),
            _sru_template_reply("fail", "Missing [Impact]."),
        ]
    )
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        bugs=[FakeBug(id=10, description="ok"), FakeBug(id=20, description="bad")],
    )
    status, comment = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "INCOMPLETE"
    assert "Bug #20" in comment
    assert "Missing [Impact]" in comment
    assert "Bug #10" not in comment


def test_sru_mp_multiple_bugs_all_passing_falls_through():
    r = QueuedReviewer(
        [_sru_template_reply("pass"), _sru_template_reply("pass"), _reply()]
    )
    mp = FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        bugs=[FakeBug(id=10, description="ok"), FakeBug(id=20, description="ok")],
    )
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"


def test_sru_mp_unreadable_linked_bugs_skips_rather_than_blocks():
    class _BrokenMP(FakeMP):
        @property
        def bugs(self):
            raise TimeoutError("simulated Launchpad timeout")

        @bugs.setter
        def bugs(self, value):
            pass

    r = ScriptedReviewer(_reply())
    mp = _BrokenMP(target="refs/heads/ubuntu/noble-devel")
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "READY_FOR_HUMAN"


def test_sru_mp_pocket_branch_target_is_still_sru_shaped():
    # #84's fix applied to the MP-side duplicate regex too.
    r = QueuedReviewer([_sru_template_reply("fail", "Missing sections.")])
    mp = FakeMP(
        target="refs/heads/ubuntu/jammy-updates",
        bugs=[FakeBug(id=1, description="x")],
    )
    status, _ = r.triage_mp(mp, diff_text=MERGE_DIFF)
    assert status == "INCOMPLETE"


def test_main_passes_memoized_diff_text_to_triage_mp(tmp_path):
    # main.py hands the LLM phase the same diff text checks 2/5/6 already
    # fetched (via checks.diff_text's per-item memo). CLEAN_DIFF_TEXT keeps
    # those checks conclusively False so the pass actually reaches the LLM
    # (an unresolvable diff would go inconclusive and skip it -- by design).
    sm = _state(tmp_path)
    mp = FakeMP(diff=FakeDiff("/d/1", 50, diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={URL: mp})

    seen = {}

    class RecordingLLM(FakeLLM):
        def triage_mp(self, obj, diff_text=None):
            seen["diff_text"] = diff_text
            return ("READY_FOR_HUMAN", "")

    main.triage_url(URL, sm, lp, RecordingLLM())
    assert seen["diff_text"] == CLEAN_DIFF_TEXT
