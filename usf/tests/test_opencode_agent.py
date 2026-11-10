"""#166/#167: the tool-less agent is pinned on every call and verified
before the first one.

Design #99 relied on opencode finding `sponsoring-reviewer` in the home
config and on matching opencode's stderr when it didn't. opencode 1.19.5's
default `build` agent allows every permission, so the missing
--dangerously-skip-permissions is no backstop if that fallback happens.
The agent is now passed inline (OPENCODE_CONFIG_CONTENT, which beats home
and project configs) and `opencode debug agent` is read once per run
(#167): an agent whose resolved tools or effective rules could reach a
tool turns the LLM phases off.
"""

import json
import subprocess

import pytest

import llm_reviewer
import opencode_agent
from llm_reviewer import LLMReviewer


def _rules(*extra):
    """The effective rules opencode 1.19.5 reports for the pinned agent
    (trimmed: the defaults before the catch-all are the same shape)."""
    return [
        {"permission": "*", "action": "allow", "pattern": "*"},
        {"permission": "doom_loop", "action": "ask", "pattern": "*"},
        {"permission": "external_directory", "pattern": "*", "action": "ask"},
        {"permission": "read", "pattern": "*", "action": "allow"},
        {"permission": "*", "action": "deny", "pattern": "*"},
        {"permission": "edit", "action": "deny", "pattern": "*"},
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "webfetch", "action": "deny", "pattern": "*"},
        {
            "permission": "external_directory",
            "pattern": "/home/ubuntu/.local/share/opencode/tool-output/*",
            "action": "allow",
        },
        *extra,
    ]


# The resolved tool map opencode 1.19.5 reports for the pinned agent.
_NO_TOOLS = dict.fromkeys(
    "invalid question bash read glob grep edit write task webfetch todowrite skill".split(),
    False,
)


def _agent(rules=None, tools=None, model=None):
    """`opencode debug agent sponsoring-reviewer` output, as JSON text."""
    info = {
        "name": "sponsoring-reviewer",
        "mode": "primary",
        "permission": _rules() if rules is None else rules,
        "tools": dict(_NO_TOOLS) if tools is None else tools,
        "options": {},
        "native": False,
    }
    if model:
        info["model"] = model
    return json.dumps(info, indent=2)


def _runner(stdout, returncode=0, stderr="", calls=None):
    def run(cmd, text, capture_output, check, timeout, env):
        if calls is not None:
            calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run


def _problem(stdout, **kwargs):
    return opencode_agent.check(run=_runner(stdout, **kwargs)).problem


# --- opencode_agent.check: the shapes the handover named ----------------------


def test_a_present_tool_less_agent_passes_and_reports_its_model():
    model = {"providerID": "openrouter", "modelID": "anthropic/claude-sonnet-5.5"}
    inspection = opencode_agent.check(run=_runner(_agent(model=model)))
    assert inspection.problem is None
    assert inspection.model == "openrouter/anthropic/claude-sonnet-5.5"


def test_no_model_is_reported_as_none():
    assert opencode_agent.check(run=_runner(_agent())).model is None


def test_a_missing_agent_fails():
    # opencode 1.19.5's exact output for an undefined agent.
    stderr = (
        "Agent sponsoring-reviewer not found, run 'opencode agent list' to get an agent list\n"
    )
    assert "not defined" in _problem("", returncode=1, stderr=stderr)


def test_an_enabled_tool_fails():
    tools = dict(_NO_TOOLS, bash=True)
    problem = _problem(_agent(tools=tools))
    assert "isn't tool-less" in problem and "bash" in problem


def test_an_allow_rule_after_the_catch_all_fails():
    rules = _rules({"permission": "bash", "pattern": "*", "action": "allow"})
    problem = _problem(_agent(rules=rules))
    assert "isn't tool-less" in problem and "'bash'" in problem


def test_an_ask_rule_after_the_catch_all_fails():
    # "ask" with no one to answer still means the tool is offered to the model.
    rules = _rules({"permission": "task", "pattern": "*", "action": "ask"})
    assert "isn't tool-less" in _problem(_agent(rules=rules))


def test_no_catch_all_deny_fails():
    # An agent defined without `tools: {"*": false}`: opencode's defaults only.
    rules = [{"permission": "*", "action": "allow", "pattern": "*"}]
    assert "no catch-all deny" in _problem(_agent(rules=rules))


def test_allows_before_the_catch_all_are_overridden():
    # Last match wins: a home/project config's `read: allow` lands before
    # the catch-all deny (observed on 1.19.5), so it grants nothing.
    rules = _rules()
    rules.insert(4, {"permission": "task", "pattern": "*", "action": "allow"})
    assert _problem(_agent(rules=rules)) is None


# --- ... and failing closed on anything unexpected ---------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        "not json at all",
        "[]",
        json.dumps({"permission": _rules()}),  # no tool map
        json.dumps({"tools": _NO_TOOLS}),  # no rules
        json.dumps({"tools": {}, "permission": _rules()}),
        "",
    ],
)
def test_an_unreadable_description_fails_closed(stdout):
    assert _problem(stdout) is not None


def test_a_failing_inspection_fails_closed():
    assert "exited 2" in _problem("", returncode=2, stderr="boom")


@pytest.mark.parametrize(
    "error, expected",
    [
        (FileNotFoundError(), "not installed"),
        (subprocess.TimeoutExpired(["opencode"], 60), "timed out"),
    ],
)
def test_opencode_unavailable_fails_closed(error, expected):
    def run(*args, **kwargs):
        raise error

    assert expected in opencode_agent.check(run=run).problem


def test_the_check_asks_under_the_pinned_environment():
    calls = []
    env = opencode_agent.environment({"HOME": "/home/x"})
    opencode_agent.check(env=env, run=_runner(_agent(), calls=calls))
    assert calls == [(["opencode", "debug", "agent", "sponsoring-reviewer"], env)]


def test_the_pinned_config_is_the_tool_less_agent():
    env = opencode_agent.environment({"HOME": "/home/x", "OPENCODE_CONFIG_CONTENT": "{}"})
    assert env["HOME"] == "/home/x"
    agent = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["sponsoring-reviewer"]
    assert agent["tools"] == {"*": False}
    # No model: it merges in from the home config (the charm's llm-model).
    assert "model" not in agent


# --- wiring in LLMReviewer._query_llm -----------------------------------------


def _reply_run(calls):
    def run(cmd, input, text, capture_output, check, timeout, env=None):
        calls.append((cmd, env))
        stdout = json.dumps({"type": "text", "part": {"type": "text", "text": "reply"}})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run


def test_every_call_carries_the_pinned_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _reply_run(calls))

    LLMReviewer()._query_llm("prompt")

    ((cmd, env),) = calls
    assert cmd[:2] == ["opencode", "run"]
    pinned = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert pinned == opencode_agent.config()


def test_a_refused_agent_disables_the_llm_for_the_run_and_notifies_once(monkeypatch, caplog):
    checked, calls, notifications = [], [], []
    monkeypatch.setattr(
        llm_reviewer,
        "_check_agent",
        lambda env: checked.append(env) or opencode_agent.Inspection("agent missing", None),
    )
    monkeypatch.setattr(subprocess, "run", _reply_run(calls))
    monkeypatch.setattr(llm_reviewer.notify, "notify", notifications.append)

    r = LLMReviewer()
    with caplog.at_level("WARNING"):
        results = [r._query_llm("p1"), r._query_llm("p2")]
        r.start_item("https://launchpad.net/bugs/2")
        results.append(r._query_llm("p3"))

    # Every call takes the existing misconfigured-agent path (FAIL ->
    # each check's inconclusive/fail-safe default), opencode run is never
    # invoked, nothing is spent against the budgets.
    assert results == ["FAIL: LLM agent misconfigured."] * 3
    assert calls == []
    assert r._run_calls == 0
    # Checked once for the run, under the pinned environment; notified once.
    assert len(checked) == 1
    assert "OPENCODE_CONFIG_CONTENT" in checked[0]
    assert len(notifications) == 1 and "agent missing" in notifications[0]
    # #169: true now -- every failed review makes its item inconclusive.
    assert "retried next run" in notifications[0]


def test_a_passing_preflight_is_checked_once_and_calls_proceed(monkeypatch):
    checked, calls = [], []
    monkeypatch.setattr(
        llm_reviewer,
        "_check_agent",
        lambda env: checked.append(env) or opencode_agent.Inspection(None, "openrouter/m"),
    )
    monkeypatch.setattr(subprocess, "run", _reply_run(calls))

    r = LLMReviewer()
    r._query_llm("p1")
    r._query_llm("p2")

    assert len(checked) == 1
    assert len(calls) == 2
    # #167: the same lookup tells which model the agent resolves to.
    assert r._agent_model == "openrouter/m"
