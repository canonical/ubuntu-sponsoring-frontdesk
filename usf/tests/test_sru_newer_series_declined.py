"""#180: Check 7 answers per series, and names a series the contributor
explicitly declines to fix differently.

Trigger: appstream bug #2172814, an SRU to resolute. The text said the fix
"is in tenacious already" and, with a reason, "I am not submitting anything
for Stonking". The escape hatch asked whether ALL newer series were fixed,
so its single `not-stated` made the finding claim "no evidence of it being
resolved in stonking and tenacious" -- wrong about tenacious, and asking
the contributor to "check" a choice they had already explained.

The fixture here is test_sru_newer_series's: a noble SRU, with resolute
(stable) and stonking (devel) unhandled -- resolute plays stonking's part,
stonking tenacious's.
"""

from fakes import FakeLLM, FakeTask
from test_sru_newer_series import _LP, _bug, _sru_mp

import checks


def _run(fixed=(), declined=()):
    bug = _bug(
        [FakeTask("testpkg (Ubuntu)", "New"), FakeTask("testpkg (Ubuntu Noble)", "In Progress")]
    )
    llm = FakeLLM()
    llm.fixed_in_newer = {"fixed": set(fixed), "declined": set(declined)}
    return checks.check_sru_newer_series("url", _sru_mp([bug]), _LP(), llm)


DECLINED_RESOLUTE = (
    "The description explains why resolute won't get this fix. SRU policy "
    "normally requires newer supported releases to be fixed first, so the SRU "
    "team will need to agree to skipping it. See "
    "https://ubuntu.com/project/docs/SRU/reference/requirements"
)


def test_the_live_shape():
    finding = _run(fixed={"stonking"}, declined={"resolute"})
    assert finding.tier == "question" and finding.kind == "verify"
    assert finding.message == DECLINED_RESOLUTE  # the fixed series isn't named at all


def test_only_the_series_the_text_doesnt_cover_are_named():
    finding = _run(fixed={"stonking"})
    assert "no evidence of it being resolved in resolute." in finding.message
    assert "stonking" not in finding.message


def test_declined_and_not_stated_together():
    finding = _run(declined={"resolute"})
    assert "no evidence of it being resolved in stonking." in finding.message
    assert DECLINED_RESOLUTE in finding.message


def test_all_fixed_is_still_silent():
    assert _run(fixed={"resolute", "stonking"}) is False


def test_nothing_stated_is_unchanged():
    finding = _run()
    assert "no evidence of it being resolved in resolute and stonking" in finding.message
    assert "SRU team will need to agree" not in finding.message


def test_the_prompt_asks_per_series_with_a_declined_list(monkeypatch):
    from llm_reviewer import LLMReviewer

    prompts = []
    r = LLMReviewer()
    monkeypatch.setattr(r, "_query_llm", lambda p: prompts.append(p) or "```yaml\nfixed: []\n```")
    r.review_fixed_in_newer_series("text", ["resolute (Ubuntu 26.04)"])
    assert "list a series as declined when the text explicitly says it" in prompts[0]
    assert "declined: []" in prompts[0]
