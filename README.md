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

## Usage

```
python3 main.py (--url <bug-or-MP-url> | --all | --sweep) [mode] [--force] [--verbose]
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

Items are re-triaged only when their contributor-controlled state
changes (see "facts" in the [glossary](doc/GLOSSARY.md)); runs are
idempotent and safe to repeat.

## Documentation

- [doc/STATUS.md](doc/STATUS.md) -- current state, run history, backlog.
- [doc/design_journal.md](doc/design_journal.md) -- every design
  decision, numbered, with rationale and live-verification notes.
- [doc/GLOSSARY.md](doc/GLOSSARY.md) -- sponsoring vocabulary and the
  bot's own concepts (facts, inconclusive, finding tiers, ...).
- Flow charts: [triage state machine](doc/flow.svg), [write
  gate](doc/write_gate.svg), [sync-request sub-flow](doc/sync_triage.svg).

## Deployment notes

Machine-local setup, none of it in the repository:

- **Launchpad credentials**: OAuth token cached under
  `~/.cache/ubuntu-sponsoring-bot/` (env-overridable, see
  `launchpad_client.py`); first run opens a browser to authorise.
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
  URL in `~/.config/ubuntu-sponsoring-bot/config.ini` (see `notify.py`);
  absent means notifications are simply disabled.

## Reporting issues

Repository, issue tracker, and contributions:
<https://github.com/canonical/ubuntu-sponsoring-frontdesk>

If the bot posted something wrong on your bug or merge proposal, please
file an issue with the Launchpad URL -- every action the bot takes is
recorded in its audit trail and can be traced. You can also simply reply
on the bug/MP: the bot backs off as soon as a human reviewer engages.
