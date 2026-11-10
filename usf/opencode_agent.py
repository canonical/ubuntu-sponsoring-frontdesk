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

`check()` is the pre-flight: it asks opencode for the agent's *effective*
permission rules and refuses an agent that could use any tool. Standard
library only: the charm loads this file by path, outside the bot's
environment.
"""

import json
import os
import re
import subprocess

AGENT_NAME = "sponsoring-reviewer"

_AGENT_LIST_TIMEOUT_SECONDS = 60

# `opencode agent list` prints each agent as a "name (mode)" line at
# column 0 followed by its effective rules as an indented JSON array
# (opencode 1.19.5). Not a documented format: anything else fails closed.
_AGENT_HEADER_RE = re.compile(r"^(?P<name>\S+) \((?P<mode>[^)]*)\)\s*$")

# The one permission allowed to stay non-deny after the catch-all: opencode
# appends `external_directory <its tool-output dir> allow` to every agent.
# It is a path guard consulted *by* tools, not a tool, so with every tool
# denied it grants nothing.
_PATH_GUARD_PERMISSIONS = ("external_directory",)


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


def parse_agent_list(text):
    """{agent name: raw text of its rules block} from `opencode agent list`."""
    blocks = {}
    name = None
    for line in (text or "").splitlines():
        header = _AGENT_HEADER_RE.match(line)
        if header:
            name = header.group("name")
            blocks[name] = []
        elif name is not None:
            blocks[name].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def unsafe_reason(rules):
    """Why these effective rules could let the agent use a tool, or None.

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


def check(env=None, run=None):
    """None when opencode resolves AGENT_NAME to a tool-less agent under
    `env`, otherwise why not -- including when opencode can't be asked or
    its answer can't be read (fail closed)."""
    run = run or subprocess.run
    try:
        proc = run(
            ["opencode", "agent", "list"],
            text=True,
            capture_output=True,
            check=False,
            timeout=_AGENT_LIST_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        return "opencode is not installed"
    except subprocess.TimeoutExpired:
        return "`opencode agent list` timed out"
    except OSError as e:
        return f"`opencode agent list` couldn't run ({e})"
    if proc.returncode != 0:
        return f"`opencode agent list` exited {proc.returncode}: {(proc.stderr or '').strip()}"
    block = parse_agent_list(proc.stdout).get(AGENT_NAME)
    if block is None:
        return f"agent {AGENT_NAME!r} is not defined"
    try:
        rules = json.loads(block)
    except ValueError:
        return f"couldn't read agent {AGENT_NAME!r}'s permission rules"
    reason = unsafe_reason(rules)
    if reason:
        return f"agent {AGENT_NAME!r} isn't tool-less: {reason}"
    return None
