# Frontdesk

A triage bot for the [Ubuntu sponsoring
queue](https://ubuntu-sponsoring.ubuntu.com/): it reviews the bugs and
merge proposals waiting for a sponsor, gives contributors early,
actionable feedback (stale versions, missing changelog entries, patches
that should be debdiffs, SRU-policy gaps, ...), and cleans out requests
with nothing left to sponsor -- so human sponsors spend their time on
items that are actually ready.

Deterministic checks run first; an LLM reviews the qualitative parts
(SRU template quality, whether a diff matches its changelog) last, and
only when the deterministic phase didn't already decide. Every write is
gated, deduplicated, and recorded in an audit trail, and the bot backs
off whenever a human reviewer is already engaged.

## Layout

```
usf/          the bot itself, with its tests in usf/tests/
src/          the machine charm that deploys it, with its tests in tests/
tools/        operator helpers, e.g. creating the Launchpad tokens
doc/          design journal, checks reference, flow charts, charm notes
.workshop/    the Workshop dev environment (see CONTRIBUTING.md)
```

`usf/` is short for ubuntu-sponsoring-frontdesk. It sits beside the charm
that deploys it (`src/`) rather than under it: the bot is what this repo is
for, and the charm is how it gets run. Its modules
import each other flat -- `import checks` -- so they stay in one directory
and are run from it.

## Usage

```
python3 usf/main.py (--url <bug-or-MP-url> | --all | --sweep) [mode] [--force] [--verbose]
```

- `--url` triages one Launchpad bug or merge proposal; `--all` processes
  the whole sponsoring queue and then sweeps previously bounced bugs;
  `--sweep` runs only that sweep.
- Write modes: `--dry-run` (default -- log intended writes without
  performing them), `--interactive` (prompt `[y/N]` before each write),
  `--yes` (unattended/cron).
- `--force` bypasses the "nothing changed since last look" gate;
  `--verbose` logs every decision step, including LLM prompts/replies
  and token usage.

Retrospective over the audit trail (read-only, writes nothing):

```
python3 usf/stats.py [--since 30d] [--json]
```

It reports what the checks actually caught (per check and tier), what a
human's engagement suppressed, which lookups left a pass inconclusive, which
findings sat on items whose write didn't go through, LLM tokens/cost per
item, and errors.

For where things stand *now* rather than what happened, `--queue` reads
`state.db` instead: counts by status, what is waiting on a contributor and
what is ready for a human, oldest first.

```
python3 usf/stats.py --queue [--json]
```

Items are re-triaged only when their contributor-controlled state
changes (see "facts" in the [glossary](doc/GLOSSARY.md)); runs are
idempotent and safe to repeat.

## Hacking

`.workshop/dev.yaml` defines a [Workshop](https://ubuntu.com/workshop/docs/)
so lint, the unit suite and dry-run triages can be run in a throwaway
container instead of on the machine that runs the real bot:

```
workshop launch dev
workshop run dev check  # the bot's lint + unit suite, as CI runs them
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the other actions and for what is
deliberately left out of the sandbox (Launchpad credentials, `opencode`).

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) -- development environment (Workshop),
  and what a change is expected to come with.
- [doc/CHECKS.md](doc/CHECKS.md) -- the complete, categorized list of
  every check currently implemented.
- [doc/STATUS.md](doc/STATUS.md) -- current state, run history, backlog.
- [doc/design_journal.md](doc/design_journal.md) -- every design
  decision, numbered, with rationale and live-verification notes.
- [doc/GLOSSARY.md](doc/GLOSSARY.md) -- sponsoring vocabulary and the
  bot's own concepts (facts, inconclusive, finding tiers, ...).
- Flow charts: [triage state machine](doc/flow.svg), [write
  gate](doc/write_gate.svg), [sync-request sub-flow](doc/sync_triage.svg).

## Deployment notes

Production runs from the charm: `doc/CHARM.md` covers deploying it, its
config, actions and where everything lives on the unit. The notes below are
what the bot expects when run by hand, and what the charm provides.

Machine-local setup, none of it in the repository:

- **Launchpad credentials**: OAuth token cached under
  `~/.cache/ubuntu-sponsoring-frontdesk/` (env-overridable, see
  `launchpad_client.py`); first run opens a browser to authorise.
- **Runtime state**: the audit trail (`audit.jsonl`) and the state DB
  (`state.db`) live under `~/.cache/ubuntu-sponsoring-frontdesk/` too,
  overridable via `SPONSORING_BOT_AUDIT` / `SPONSORING_BOT_STATE`. Never in
  the checkout: they are machine-local records, not source.
- **Bot's Launchpad username**: defaults to `ubuntu-sponsoring-bot`
  (`checks.BOT_USERNAME`), overridable via `SPONSORING_BOT_LP_USERNAME` if
  the account is ever renamed -- used to recognise the bot's own past
  comments where a live session isn't available (facts fingerprinting, the
  bounce-response sweep).
- **Privileged helper**: the `~ubuntu-sponsors` unsubscribe runs through
  `privileged_helper.py` with a separate team-member token
  (`python3 privileged_helper.py login`); the bot account itself must
  NOT be a member of `~ubuntu-sponsors`.
- **LLM access**: the bot invokes the `opencode` CLI under a dedicated
  **tool-less agent** named `sponsoring-reviewer`, which must be defined
  in `~/.config/opencode/opencode.jsonc`:

  ```jsonc
  {
    "agent": {
      "sponsoring-reviewer": {
        "description": "Tool-less text reviewer for the Ubuntu sponsoring bot",
        "mode": "primary",
        "tools": { "*": false },
        "permission": { "edit": "deny", "bash": "deny", "webfetch": "deny" }
      }
    }
  }
  ```

  This is a security boundary, not a preference: prompts embed
  contributor-controlled text, so the LLM must not be able to act (see
  design_journal.md #99). If the agent is missing the bot fails safe --
  every LLM-phase item defers to a human and a warning names this file.
- **Operator notifications** (optional): a Mattermost incoming-webhook
  URL in `~/.config/ubuntu-sponsoring-frontdesk/config.ini` (see `notify.py`);
  absent means notifications are simply disabled.

## Reporting issues

Repository, issue tracker, and contributions:
<https://github.com/canonical/ubuntu-sponsoring-frontdesk>

If the bot posted something wrong on your bug or merge proposal, please
file an issue with the Launchpad URL -- every action the bot takes is
recorded in its audit trail and can be traced. You can also simply reply
on the bug/MP: the bot backs off as soon as a human reviewer engages.

## License

Copyright (C) 2026 Canonical Ltd.

GPL-3.0-only; see [COPYING](COPYING) for the full text.
