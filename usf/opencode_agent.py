"""The tool-less opencode agent every LLM call runs under (#99, #166).

The prompts embed contributor-written text, so the agent must have no tools.
This module is the single definition of that agent, used by both halves:

- the bot passes it to every `opencode` call in OPENCODE_CONFIG_CONTENT,
  opencode's highest-precedence config source, so no home or project
  config can loosen it (a project `opencode.json` does override
  OPENCODE_CONFIG, checked on 1.19.5 -- hence the inline variable);
- the charm writes the same definition, plus the configured model, to the
  unit's ~/.config/opencode/opencode.jsonc (defence in depth, and where
  the model comes from -- opencode merges the two).

`check()` is the pre-flight: it asks opencode for the agent as it will
actually run it (`opencode debug agent`, #167) and refuses one that could
use any tool. Standard library only: the charm loads this file by path,
outside the bot's environment.
"""

import collections
import json
import os
import subprocess

AGENT_NAME = "sponsoring-reviewer"

_INSPECT_TIMEOUT_SECONDS = 60

# The one permission allowed to stay non-deny after the catch-all: opencode
# appends `external_directory <its tool-output dir> allow` to every agent.
# It is a path guard consulted *by* tools, not a tool, so with every tool
# denied it grants nothing.
_PATH_GUARD_PERMISSIONS = ("external_directory",)

# What the pre-flight found: `problem` is None when the agent is safe to
# use, otherwise why not; `model` is the resolved "provider/model" or None
# (recorded with each LLM call, #168 -- opencode's reply stream doesn't
# say which model answered).
Inspection = collections.namedtuple("Inspection", "problem model")


def agent_definition(model=""):
    """The agent's opencode config: no tools, and the riskiest permissions
    denied by name as a second layer."""
    agent = {
        "description": "Tool-less text reviewer for the Ubuntu sponsoring bot",
        "mode": "primary",
        "tools": {"*": False},
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
    }
    if model:
        agent["model"] = model
    return agent


def config(model=""):
    return {
        "$schema": "https://opencode.ai/config.json",
        "agent": {AGENT_NAME: agent_definition(model)},
    }


def environment(base=None):
    """`base` (default: this process's environment) with the agent pinned
    through OPENCODE_CONFIG_CONTENT. No model: that merges in from the
    home config (the charm's `llm-model`, or a developer's own)."""
    env = dict(os.environ if base is None else base)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config())
    return env


def unsafe_rules_reason(rules):
    """Why these effective permission rules could let the agent use a
    tool, or None.

    opencode evaluates rules last-match-wins, and drops a tool from the
    agent only when the last rule matching its name is a `*`-pattern deny
    (both read from the 1.19.5 source). So the agent is tool-less when a
    catch-all `* * deny` exists and nothing after it allows or asks --
    except the path guard above. Deliberately stricter than opencode's own
    logic: an unknown tool name is covered, and anything unexpected fails.
    """
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        return "its permission rules aren't a list of rules"
    catch_all = None
    for index, rule in enumerate(rules):
        if (rule.get("permission"), rule.get("pattern"), rule.get("action")) == ("*", "*", "deny"):
            catch_all = index
    if catch_all is None:
        return "no catch-all deny rule"
    for rule in rules[catch_all + 1 :]:
        if rule.get("action") == "deny" or rule.get("permission") in _PATH_GUARD_PERMISSIONS:
            continue
        return (
            f"rule {rule.get('permission')!r} {rule.get('pattern')!r} -> "
            f"{rule.get('action')!r} comes after the catch-all deny"
        )
    return None


def unsafe_tools_reason(tools):
    """Why opencode's resolved tool map offers the agent a tool, or None.
    Must be a non-empty map with every value exactly False."""
    if not isinstance(tools, dict) or not tools:
        return "its resolved tool map is missing"
    enabled = sorted(name for name, on in tools.items() if on is not False)
    if enabled:
        return f"tools enabled: {', '.join(enabled)}"
    return None


def _model_name(model):
    if isinstance(model, dict) and model.get("providerID") and model.get("modelID"):
        return f"{model['providerID']}/{model['modelID']}"
    return None


def inspect(info):
    """Judge `opencode debug agent`'s parsed JSON for the agent."""
    if not isinstance(info, dict):
        return Inspection("the agent description isn't a JSON object", None)
    model = _model_name(info.get("model"))
    for reason in (
        unsafe_tools_reason(info.get("tools")),
        unsafe_rules_reason(info.get("permission")),
    ):
        if reason:
            return Inspection(f"agent {AGENT_NAME!r} isn't tool-less: {reason}", model)
    return Inspection(None, model)


def check(env=None, run=None):
    """Ask opencode how it resolves AGENT_NAME under `env`.

    `opencode debug agent <name>` (opencode 1.19.5) prints the agent as
    opencode will run it: JSON with the resolved `tools` map, the effective
    `permission` rules and the `model`, and exits non-zero naming the agent
    when it isn't defined. Both the tool map and the rules must say
    tool-less (defence in depth -- neither format is documented). Anything
    that can't be asked or read fails closed: the returned problem is set.
    """
    run = run or subprocess.run
    try:
        proc = run(
            ["opencode", "debug", "agent", AGENT_NAME],
            text=True,
            capture_output=True,
            check=False,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        return Inspection("opencode is not installed", None)
    except subprocess.TimeoutExpired:
        return Inspection("`opencode debug agent` timed out", None)
    except OSError as e:
        return Inspection(f"`opencode debug agent` couldn't run ({e})", None)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        if "not found" in detail:
            return Inspection(f"agent {AGENT_NAME!r} is not defined", None)
        return Inspection(f"`opencode debug agent` exited {proc.returncode}: {detail}", None)
    try:
        info = json.loads(proc.stdout)
    except ValueError:
        return Inspection(f"couldn't read agent {AGENT_NAME!r}'s description", None)
    return inspect(info)
