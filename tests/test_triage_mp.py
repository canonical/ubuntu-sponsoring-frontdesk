"""Design #47: LLM content review for merge proposals -- changelog stanza
quality, changelog-vs-diff consistency, and a Feature Freeze classification.
All findings are advisory (question tier): they never block, never vote, and
any parsing doubt means silence, not noise."""

import datetime

import llm_reviewer
import main
import release_schedule
from state import StateManager
from fakes import CLEAN_DIFF_TEXT, FakeDiff, FakeLLM, FakeMP, FakeTriageClient

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


def _reply(verdict="pass", feature="no", observations=None):
    obs = "".join(f"  - {o}\n" for o in (observations or []))
    return (
        "Some analysis prose.\n\n```yaml\n"
        f"verdict: {verdict}\nfeature: {feature}\nobservations:\n{obs}```\n"
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
    obs, feature = ScriptedReviewer("")._extract_mp_review(
        _reply(verdict="fail", feature="yes", observations=["First.", "Second."])
    )
    assert obs == ["First.", "Second."]
    assert feature is True


def test_parse_fail_without_observations_is_silent():
    obs, _ = ScriptedReviewer("")._extract_mp_review(_reply(verdict="fail"))
    assert obs == []


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
    assert payload == ["Vague bullet."]


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
    assert "Feature Freeze Exception" in payload[0]


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
    llm = FakeLLM(mp_result=("ADVISORY", ["Soft observation one.", "Two."]))

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert "Nice to have" in lp.comments[0]
    assert "* Soft observation one." in lp.comments[0]
    assert "* Two." in lp.comments[0]
    assert "Needs fixing" not in lp.comments[0]
    assert lp.votes == [None]
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_advisory_plus_blocking_finding_split_into_sections(tmp_path):
    sm = _state(tmp_path)
    # Merge conflicts (incomplete) + an LLM advisory: one comment, both
    # sections, and the vote/status driven by the blocking one.
    mp = FakeMP(diff=FakeDiff("/d/1", 50, conflicts="foo.c", diff_text=CLEAN_DIFF_TEXT))
    lp = FakeTriageClient(objects={URL: mp})
    llm = FakeLLM(mp_result=("ADVISORY", ["Consider clarifying the stanza."]))

    main.triage_url(URL, sm, lp, llm)

    assert len(lp.comments) == 1
    assert "Needs fixing" in lp.comments[0]
    assert "Nice to have" in lp.comments[0]
    assert lp.votes == ["Needs Fixing"]
    assert sm.get_status(URL)[0] == "WAITING_ON_CONTRIBUTOR"


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
