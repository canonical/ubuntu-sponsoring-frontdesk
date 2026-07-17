"""Design #42: opencode is queried via --format json so --verbose can log
the prompt, the raw reply, and token usage/cost. _parse_ndjson_reply is
the pure parsing half; _query_llm is exercised end-to-end with subprocess
mocked so no real opencode call happens.

Design #99: the prompts embed attacker-controlled bug/MP text, so the call
runs under a dedicated tool-less opencode agent, without
--dangerously-skip-permissions, with a timeout, and with two runtime
tripwires (agent-not-found fallback on stderr, tool_use events in the
reply stream) that both discard the reply and fail safe."""

import json
import subprocess

import llm_reviewer
from llm_reviewer import LLMReviewer


def _ndjson(*events):
    return "\n".join(json.dumps(e) for e in events)


def _text_event(text):
    return {"type": "text", "part": {"type": "text", "text": text}}


def _tool_use_event(tool="bash"):
    return {"type": "tool_use", "part": {"type": "tool", "tool": tool}}


def _step_finish_event(
    total=100, input_=10, output=20, reasoning=0, cache_read=0, cache_write=0, cost=0.01
):
    return {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "tokens": {
                "total": total,
                "input": input_,
                "output": output,
                "reasoning": reasoning,
                "cache": {"read": cache_read, "write": cache_write},
            },
            "cost": cost,
        },
    }


def test_parse_ndjson_reply_extracts_text_and_usage():
    stdout = _ndjson(
        {"type": "step_start", "part": {}},
        _text_event("Looks good.\n```yaml\nverdict: pass\nreason:\n```"),
        _step_finish_event(
            total=8000, input_=3, output=37, cache_read=100, cache_write=200, cost=0.03
        ),
    )
    text, usage, used_tools = LLMReviewer._parse_ndjson_reply(stdout)
    assert text == "Looks good.\n```yaml\nverdict: pass\nreason:\n```"
    assert used_tools is False
    assert usage == {
        "total": 8000,
        "input": 3,
        "output": 37,
        "reasoning": 0,
        "cache_read": 100,
        "cache_write": 200,
        "cost": 0.03,
    }


def test_parse_ndjson_reply_concatenates_multiple_text_events():
    stdout = _ndjson(
        _text_event("Part one. "), _text_event("Part two."), _step_finish_event()
    )
    text, usage, _ = LLMReviewer._parse_ndjson_reply(stdout)
    assert text == "Part one. Part two."
    assert usage is not None


def test_parse_ndjson_reply_sums_multiple_step_finish_events():
    stdout = _ndjson(
        _text_event("hi"),
        _step_finish_event(total=100, cost=0.01),
        _step_finish_event(total=50, cost=0.005),
    )
    _, usage, _ = LLMReviewer._parse_ndjson_reply(stdout)
    assert usage["total"] == 150
    assert usage["cost"] == 0.015


def test_parse_ndjson_reply_skips_unparseable_lines():
    stdout = "not json at all\n" + _ndjson(_text_event("hi"), _step_finish_event())
    text, usage, _ = LLMReviewer._parse_ndjson_reply(stdout)
    assert text == "hi"
    assert usage is not None


def test_parse_ndjson_reply_no_text_event_falls_back_to_raw_stdout():
    # An unexpected/older opencode output shape -- no 'text' event to find.
    # Never guess; return the raw stream so diagnostic content isn't lost.
    stdout = _ndjson({"type": "step_start", "part": {}})
    text, usage, _ = LLMReviewer._parse_ndjson_reply(stdout)
    assert text == stdout.strip()
    assert usage is None


def test_parse_ndjson_reply_flags_tool_use_events():
    # Live-observed shape on opencode 1.18.3: a bash invocation appears as
    # a distinct tool_use event in the stream (design_journal.md #99).
    stdout = _ndjson(
        _tool_use_event(), _text_event("the date is..."), _step_finish_event()
    )
    text, _, used_tools = LLMReviewer._parse_ndjson_reply(stdout)
    assert used_tools is True
    assert text == "the date is..."


def test_query_llm_invokes_opencode_with_json_format(monkeypatch):
    captured = {}

    def fake_run(cmd, input, text, capture_output, check, timeout):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["timeout"] = timeout
        stdout = _ndjson(
            _text_event("```yaml\nverdict: pass\nreason:\n```"), _step_finish_event()
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    output = r._query_llm("some prompt")
    assert "verdict: pass" in output
    assert "--format" in captured["cmd"]
    assert "json" in captured["cmd"]
    # The prompt travels over stdin, never argv: a prompt embedding a huge
    # MP diff would otherwise exceed the kernel's argument-size limit.
    assert captured["input"] == "some prompt"
    assert "some prompt" not in captured["cmd"]
    # #99: the tool-less agent, no permission bypass, and a timeout.
    assert "--agent" in captured["cmd"]
    assert llm_reviewer._OPENCODE_AGENT in captured["cmd"]
    assert "--dangerously-skip-permissions" not in captured["cmd"]
    assert captured["timeout"] == llm_reviewer._LLM_TIMEOUT_SECONDS


def test_query_llm_logs_prompt_reply_and_usage(monkeypatch, caplog):
    import logging

    def fake_run(cmd, input, text, capture_output, check, timeout):
        stdout = _ndjson(
            _text_event("the reply"),
            _step_finish_event(total=42, cost=0.007),
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    with caplog.at_level(logging.DEBUG):
        r._query_llm("THE-PROMPT-MARKER")
    assert "THE-PROMPT-MARKER" in caplog.text
    assert "the reply" in caplog.text
    assert "total=42" in caplog.text
    assert "cost=$0.0070" in caplog.text


def test_query_llm_nonzero_exit_still_fails_safe(monkeypatch):
    def fake_run(cmd, input, text, capture_output, check, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    assert r._query_llm("prompt") == "FAIL: LLM invocation failed internally."


def test_query_llm_opencode_missing(monkeypatch):
    def fake_run(cmd, input, text, capture_output, check, timeout):
        raise FileNotFoundError("opencode not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    assert "not installed" in r._query_llm("prompt")


# --- #99: security tripwires -------------------------------------------------


def test_query_llm_missing_agent_fallback_discards_the_reply(monkeypatch, caplog):
    # Verified live on opencode 1.18.3: an undefined agent produces a
    # stderr warning and exit 0, silently falling back to the default
    # (tool-capable) agent. That run must never be trusted.
    def fake_run(cmd, input, text, capture_output, check, timeout):
        stdout = _ndjson(_text_event("verdict: pass"), _step_finish_event())
        stderr = (
            f'! agent "{llm_reviewer._OPENCODE_AGENT}" not found. '
            "Falling back to default agent"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    with caplog.at_level("WARNING"):
        result = r._query_llm("prompt")
    assert result == "FAIL: LLM agent misconfigured."
    assert "opencode.jsonc" in caplog.text


def test_query_llm_tool_use_in_reply_discards_it(monkeypatch, caplog):
    def fake_run(cmd, input, text, capture_output, check, timeout):
        stdout = _ndjson(
            _tool_use_event(), _text_event("verdict: pass"), _step_finish_event()
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    with caplog.at_level("WARNING"):
        result = r._query_llm("prompt")
    assert result == "FAIL: LLM reply used tools."


def test_query_llm_timeout_fails_safe(monkeypatch, caplog):
    def fake_run(cmd, input, text, capture_output, check, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = LLMReviewer()
    with caplog.at_level("WARNING"):
        result = r._query_llm("prompt")
    assert result == "FAIL: LLM invocation timed out."


# --- #100: call budgets, input caps, usage accounting -------------------------


def _ok_run(monkeypatch, calls=None):
    def fake_run(cmd, input, text, capture_output, check, timeout):
        if calls is not None:
            calls.append(input)
        stdout = _ndjson(_text_event("reply"), _step_finish_event(total=10, cost=0.001))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_per_item_budget_defers_after_the_cap(monkeypatch):
    calls = []
    _ok_run(monkeypatch, calls)
    r = LLMReviewer()
    r.start_item("https://bugs.launchpad.net/bugs/1")
    for _ in range(llm_reviewer._ITEM_LLM_CALL_BUDGET):
        assert r._query_llm("p") == "reply"
    assert r._query_llm("p") == "FAIL: LLM per-item call budget exhausted."
    assert len(calls) == llm_reviewer._ITEM_LLM_CALL_BUDGET
    # A new item gets a fresh budget.
    r.start_item("https://bugs.launchpad.net/bugs/2")
    assert r._query_llm("p") == "reply"


def test_run_budget_defers_and_notifies_once(monkeypatch, caplog):
    _ok_run(monkeypatch)
    notifications = []
    monkeypatch.setattr(llm_reviewer.notify, "notify", notifications.append)
    r = LLMReviewer()
    monkeypatch.setattr(llm_reviewer, "_ITEM_LLM_CALL_BUDGET", 10**6)
    monkeypatch.setattr(llm_reviewer, "_RUN_LLM_CALL_BUDGET", 3)
    for _ in range(3):
        assert r._query_llm("p") == "reply"
    with caplog.at_level("WARNING"):
        assert r._query_llm("p") == "FAIL: LLM run call budget exhausted."
        assert r._query_llm("p") == "FAIL: LLM run call budget exhausted."
    assert len(notifications) == 1  # operator pinged exactly once
    # start_item does NOT reset the run budget.
    r.start_item("u")
    assert r._query_llm("p") == "FAIL: LLM run call budget exhausted."


def test_usage_is_recorded_in_the_audit_trail(monkeypatch):
    _ok_run(monkeypatch)

    class _Audit:
        records = []

        def record(self, **kw):
            self.records.append(kw)

    audit = _Audit()
    r = LLMReviewer(audit=audit)
    r.start_item("https://bugs.launchpad.net/bugs/7")
    r._query_llm("p")
    assert len(audit.records) == 1
    entry = audit.records[0]
    assert entry["action"] == "llm_call"
    assert entry["url"] == "https://bugs.launchpad.net/bugs/7"
    assert "tokens=10" in entry["detail"]
    assert "cost=$0.0010" in entry["detail"]


def test_run_summary_totals_usage(monkeypatch, caplog):
    _ok_run(monkeypatch)
    r = LLMReviewer()
    r.start_item("u")
    r._query_llm("p")
    r._query_llm("p")
    with caplog.at_level("INFO"):
        r.log_run_summary()
    assert "2 call(s)" in caplog.text
    assert "20 token(s)" in caplog.text


def test_cap_text_marks_truncation():
    capped = llm_reviewer._cap_text("x" * 25_000)
    assert len(capped) < 25_000
    assert "truncated by the bot" in capped
    assert llm_reviewer._cap_text("short") == "short"
    assert llm_reviewer._cap_text(None) == ""


def test_bounce_response_prompt_caps_comments(monkeypatch):
    calls = []
    _ok_run(monkeypatch, calls)
    r = LLMReviewer()
    r.start_item("u")
    # 30 comments, one of them huge: prompt keeps the newest 20, each capped.
    comments = [f"comment {i}" for i in range(29)] + ["y" * 50_000]
    r.review_bounce_response("z" * 50_000, comments)
    prompt = calls[0]
    assert "comment 0" not in prompt  # oldest dropped
    assert "comment 28" in prompt  # newest kept
    assert prompt.count("truncated by the bot") == 2  # feedback + huge comment
    assert "y" * 4_001 not in prompt
    assert "z" * 10_001 not in prompt


def test_sru_template_prompt_caps_the_description(monkeypatch):
    calls = []
    _ok_run(monkeypatch, calls)
    r = LLMReviewer()
    r.start_item("u")
    r.review_sru_template("d" * 50_000)
    assert "d" * 20_001 not in calls[0]
    assert "truncated by the bot" in calls[0]
