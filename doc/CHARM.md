# Charming Frontdesk: a starting point

Notes for the session that writes the charm. This is a **machine** charm
(unlike canonical/ubuntu-manpages-operator, which is k8s): the bot is a
Python process on an Ubuntu host that talks to Launchpad, not a container
workload.

Nothing here is a design decision -- it is what the bot needs today, and the
questions a charm forces that only seb128 can answer.

## Repository layout

Decided before the charm work started (#144), so the charm session doesn't
have to restructure and design at the same time:

```
usf/            the bot ("ubuntu-sponsoring-frontdesk"), tests in usf/tests/
src/            the charm -- charm.py, plus the service/timer units
doc/            shared: design journal, checks reference, these notes
```

One repo, deliberately: the bot exists only to be deployed on our
infrastructure, so a separate charm repo would just add clone/pull logic to
the charm for no gain (seb128). The house example to follow is
`canonical/ubuntu-autosync-operator`, which ships its workload inside the
charm (`src/script/`, `src/systemd/`) and copies it into place from
`install`.

Two things to get right when packing:

- `usf/` is **not** included automatically. Charmcraft's charm part primes
  `src/` and `lib/`; the workload directory needs an explicit entry.
- The bot's modules import each other flat (`import checks`), so keep the
  directory intact and run from it -- give the systemd unit a
  `WorkingDirectory` of wherever `usf/` is installed. No import refactor is
  needed, and a flattening one would be a mistake.

## What the workload actually is

One Python program, run as a batch job rather than a daemon:

```
python3 usf/main.py --all --dry-run|--interactive|--yes   # the queue pass
python3 usf/main.py --sweep                               # the Rule B sweep alone
python3 usf/main.py --url <bug-or-mp-url>                 # one item
```

`--all` ends by running the sweep itself, so a deployment normally needs
only the first form on a timer. Each pass fetches
`https://sponsoring-reports.ubuntu.com/jsons/sponsoring.json` and triages
every item whose facts changed since last time.

There is no HTTP service, no listening socket and nothing to scale: a second
concurrent pass would just duplicate work and double the Launchpad calls.

## Runtime requirements

- **System Python, no virtualenv.** `python3-apt` provides `apt_pkg`, a
  compiled extension tied to the system interpreter; the version comparison
  in every check depends on it. Packages: `python3-apt`, `python3-debian`,
  `python3-launchpadlib`, `python3-yaml`, `distro-info`.
- **`python3-ubuntu-lint`** (Check 13) from `ppa:enr0n/ubuntu-lint`. Absent,
  the check degrades to silence rather than failing -- so it is optional,
  but the check is dead weight without it. See `.workshop/frontdesk-dev/`
  for the exact install sequence, which is known to work on 26.04.
- **`opencode`** for the LLM phases, plus the tool-less `sponsoring-reviewer`
  agent in `~/.config/opencode/opencode.jsonc`. Without it the LLM phases
  fail safe (the item is left for a human) and the deterministic checks
  still run. The agent config is a security control, not a preference:
  prompts embed contributor-written text (design journal #99).

## State and configuration the charm must provide

| path | what | notes |
|---|---|---|
| `~/.cache/ubuntu-sponsoring-frontdesk/audit.jsonl` | audit trail | append-only, grows ~100KB/month; `stats.py` reads it |
| `~/.cache/ubuntu-sponsoring-frontdesk/state.db` | sqlite: per-item status, facts fingerprint, bounce reason | **losing it re-triages the whole queue once**, re-posting nothing but re-spending LLM calls |
| `~/.cache/ubuntu-sponsoring-frontdesk/credentials` | Launchpad OAuth token | created by an interactive browser authorisation on first use |
| `~/.cache/ubuntu-sponsoring-frontdesk/launchpadlib/` | launchpadlib's own cache | disposable |
| `~/.config/ubuntu-sponsoring-frontdesk/config.ini` | `[notify] webhook_url` for Mattermost | secret; absent means notifications are simply skipped |

Every one of those is relocatable by environment variable, which is what
makes a charm layout straightforward:

```
SPONSORING_BOT_AUDIT       SPONSORING_BOT_STATE      SPONSORING_BOT_CONFIG
SPONSORING_BOT_LP_CACHE    SPONSORING_BOT_LP_USERNAME
```

`SPONSORING_BOT_LP_USERNAME` defaults to `ubuntu-sponsoring-bot` and is used
to recognise the bot's own comments; it must match the account the token
belongs to, or the bot will treat its own comments as a human reviewer's.

## How it runs today, and why that matters

It runs **`--interactive`, with a human approving each write** -- not
unattended. That isn't caution about the checks; it is that the current
account cannot unsubscribe `~ubuntu-sponsors`, so those writes are declined
and done by hand. A dedicated account that is a member of the team is
needed before `--yes` makes sense (see `STATUS.md`).

A charm that deploys straight to `--yes` would therefore be deploying a
configuration nobody has run yet. Worth making the write mode a config
option that defaults to `dry-run`.

Budgets already in the code, which a charm should probably expose:
`_ITEM_LLM_CALL_BUDGET = 6` and `_RUN_LLM_CALL_BUDGET = 100`
(`llm_reviewer.py`). A full pass currently costs a few cents and ~15
minutes for ~55 items.

## Questions the charm forces

1. **Credentials.** The Launchpad token needs a browser authorisation once.
   Options: juju secret holding a pre-authorised `credentials` file; a
   manual first-run `juju exec`; or an action that performs the
   authorisation. Same question for opencode's login.
2. **Timer or service.** No daemon exists, so something must schedule the
   pass -- a systemd timer in the charm, or a cron-style config option. How
   often? Passes are cheap when nothing changed (78% of items skip).
3. **Write mode as config**, defaulting to `dry-run`; promoting to `--yes`
   should be a deliberate act, and is blocked on the account question above.
4. **Where state lives.** A charm storage mount keeps `state.db` across
   unit replacement, which matters more than it looks: losing it costs a
   full re-triage, which is LLM spend rather than wrong behaviour.
5. **Does the charm own `ubuntu-lint`'s PPA?** It is currently a manual
   `add-apt-repository`; a charm config for the source is tidier, and the
   dotted-revision fix (0.2.4) is awaited there anyway.
6. **Observability.** `stats.py` and `stats.py --queue` are the current
   interface. A charm action wrapping them is the obvious first step; the
   Mattermost webhook already covers operator alerts.

## Where to read next

- `AGENTS.md` -- conventions this repo expects from any change.
- `CONTRIBUTING.md` -- the Workshop dev environment, which already
  provisions everything in "Runtime requirements" and is the closest thing
  to a working deployment recipe.
- `doc/design_journal.md` #133 (Workshop) and #130 (why state moved to the
  cache dir) are the entries most relevant to packaging this up.
- `doc/STATUS.md` -- current backlog, including the account question.
