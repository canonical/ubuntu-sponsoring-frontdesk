"""#185: the duplicate-comment check ignores each "- [Section]:" point's
LLM-written detail (live: ezurio-qcacld-2.0-dkms bug #2173882 got the same
SRU-template bounce twice, reworded)."""

from fakes import BOT, FakeAudit, FakeBug, FakeBugMessage, FakeRoot

import checks
import llm_reviewer
from launchpad_client import FOOTNOTE, LPClient


def _posted(points, problem="thin"):
    bullet = llm_reviewer.sru_template_bullet(llm_reviewer.SruTemplateReason(points, problem))
    body = checks._bullet_text(bullet)
    return f"Thanks!\n\n{body}\n\n{FOOTNOTE}"


FIRST = _posted(
    [
        ("[Impact]", "does not say what concretely breaks for users"),
        ("[Test Plan]", "only a build/load check with no steps"),
    ]
)
REWORDED = _posted(
    [
        ("[Impact]", "lacks the user-visible failure and affected scope"),
        ("[Test Plan]", "no concrete steps exercising remain_on_channel/mgmt_tx"),
    ]
)


def _already(existing, new, owner=BOT):
    client = LPClient(mode="yes", lp=FakeRoot(), audit=FakeAudit())
    bug = FakeBug()
    bug.messages = [FakeBugMessage(owner, existing)]
    return client._already_posted(bug, new, "bug")


def test_the_rendered_points_are_indented_under_the_bullet():
    assert "\n\n  - [Impact] should explain" in FIRST
    assert "Here it does not say" in FIRST
    assert "\n  \n" not in FIRST  # blank lines stay empty (#197)


def test_reworded_points_for_the_same_sections_are_a_duplicate():
    assert _already(FIRST, REWORDED) is True


def test_a_different_set_of_sections_is_posted():
    other = _posted([("[Impact]", "x")])
    assert _already(FIRST, other) is False


def test_thin_and_missing_are_different_comments():
    missing = _posted(
        [("[Impact]", "a"), ("[Test Plan]", "b")],
        problem="missing",
    )
    assert _already(FIRST, missing) is False


def test_someone_elses_comment_is_not_ours():
    assert _already(FIRST, REWORDED, owner="https://x/~someone") is False
