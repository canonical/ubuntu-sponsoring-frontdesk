# Charming Frontdesk: a starting point

Notes for the session that writes the charm. This is a **machine** charm
(unlike canonical/ubuntu-manpages-operator, which is k8s): the bot is a
Python process on an Ubuntu host that talks to Launchpad, not a container
workload.

Written before the charm (#143) as what the bot needs and the questions a
charm forces. The charm now exists (#146): the answers are at the end, under
"Decisions", and "Deploying" says how to use it.

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
| `~/.cache/ubuntu-sponsoring-frontdesk-helper/credentials` | the privileged helper's token (#78) | a `~ubuntu-sponsors` member account, used only to unsubscribe the team; the bot delegates to it when the file exists |
| `~/.cache/ubuntu-sponsoring-frontdesk/launchpadlib/` | launchpadlib's own cache | disposable |
| `~/.config/ubuntu-sponsoring-frontdesk/config.ini` | `[notifications] webhook_url` for Mattermost | secret; absent means notifications are simply skipped |

Every one of those is relocatable by environment variable, which is what
makes a charm layout straightforward:

```
SPONSORING_BOT_AUDIT                   SPONSORING_BOT_STATE
SPONSORING_BOT_CONFIG                  SPONSORING_BOT_LP_USERNAME
SPONSORING_BOT_LP_CREDENTIALS          SPONSORING_BOT_LP_CACHE
SPONSORING_BOT_HELPER_LP_CREDENTIALS   SPONSORING_BOT_HELPER_LP_CACHE
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

## Decisions (#146)

seb128's answers to the questions above; the reasoning is in the design
journal.

1. **Credentials: Juju secrets**, one config option each:
   `lp-triager-credentials` (the bot account, required),
   `lp-sponsor-credentials` (the #78 helper, optional), `opencode-auth`
   (opencode's `auth.json`), `mattermost-webhook`. The browser
   authorisation happens once, off the unit; the resulting file goes into
   the secret.
2. **A systemd timer**, `OnUnitInactiveSec=run-interval` (default 60
   minutes): counted from the end of the previous pass, so passes can't
   stack. A `flock` keeps a manual run and a timer pass apart.
3. **`mode` = `off` | `dry-run` | `yes`, default `off`.** `--interactive`
   needs a terminal, so it stays a manual run over `juju ssh` with the
   `frontdesk` wrapper.
4. **State on the rootfs** (`/var/lib/frontdesk`) for now; seb128 handles
   backups. Juju storage vs. an off-host copy is in the STATUS.md backlog.
5. **The charm adds `ppa:enr0n/ubuntu-lint`**, hard-coded and best effort,
   until the package is SRUed to 26.04 -- then the PPA goes.
6. **Actions:** `stats`, `run-now`, `triage url=` (always dry-run).

Not exposed, deliberately: `SPONSORING_BOT_LP_USERNAME` (it must match the
token's account, and the default does) and the LLM call budgets (fine
hard-coded).

## Deploying

Not in Charmhub yet: pack locally. `.github/workflows/promote.yml` is ready
for when it is (manual trigger only).

```
charmcraft pack
juju deploy ./ubuntu-sponsoring-frontdesk_amd64.charm frontdesk
```

It comes up Blocked on `lp-triager-credentials`. Each secret is created
from a file, granted to the application, and set on its option.

The Launchpad tokens come from `tools/lp-credentials.py <account>`, which
prints a URL to approve: open it in a browser logged in as **that account**
(a private window, not your own session) -- the script refuses a token
approved as anyone else, and won't overwrite an existing file.

The juju snap can't read files under a dot-directory (or `/tmp`), so work
from a plain directory, and delete the files once the secrets exist.
`juju add-secret` prints the new secret's ID, which each block keeps in
`$id` for its `juju config` line. Once per shell:

```
FRONTDESK=~/ubuntu-sponsoring-frontdesk     # your checkout of this repo
mkdir -p ~/frontdesk-secrets && cd ~/frontdesk-secrets
```

Then each block is self-contained:

**Bot account** (`lp-triager-credentials`, required):

```
"$FRONTDESK"/tools/lp-credentials.py ubuntu-sponsoring-bot
id=$(juju add-secret frontdesk-triager credentials#file=ubuntu-sponsoring-bot.credentials)
juju grant-secret frontdesk-triager frontdesk
juju config frontdesk lp-triager-credentials="$id"
```

**Privileged helper** (`lp-sponsor-credentials`, optional -- without it the
`~ubuntu-sponsors` unsubscribe fails, #78):

```
"$FRONTDESK"/tools/lp-credentials.py ubuntu-sponsoring-helper
id=$(juju add-secret frontdesk-sponsor credentials#file=ubuntu-sponsoring-helper.credentials)
juju grant-secret frontdesk-sponsor frontdesk
juju config frontdesk lp-sponsor-credentials="$id"
```

**opencode login** (`opencode-auth`, optional -- without it the LLM phases
fail safe). Copied from a machine already logged in; don't log in on the
unit, the charm deletes an `auth.json` that no secret backs:

```
cp ~/.local/share/opencode/auth.json opencode-auth.json
id=$(juju add-secret frontdesk-opencode auth-json#file=opencode-auth.json)
juju grant-secret frontdesk-opencode frontdesk
juju config frontdesk opencode-auth="$id"
```

**Mattermost webhook** (`mattermost-webhook`, optional -- without it
operator notifications are only logged):

```
echo -n 'https://chat.example.com/hooks/<key>' > mattermost-webhook.url
id=$(juju add-secret frontdesk-webhook webhook-url#file=mattermost-webhook.url)
juju grant-secret frontdesk-webhook frontdesk
juju config frontdesk mattermost-webhook="$id"
```

**LLM model** (`llm-model`, optional -- empty uses opencode's default):

```
juju config frontdesk llm-model=github-copilot/<model>
```

To replace a secret's content later (a new token), update it in place; the
charm picks the change up by itself:

```
juju update-secret frontdesk-triager credentials#file=ubuntu-sponsoring-bot.credentials
```

When everything is set:

```
cd ~ && rm -r ~/frontdesk-secrets
```

It is then Active with `mode=off`: nothing runs on its own. The status line
names what is missing (no LLM review, no helper, no ubuntu-lint).

```
juju run frontdesk/0 triage url=<bug-or-mp-url>   # one item, dry-run
juju config frontdesk mode=dry-run                # first pass at once, then every run-interval
juju run frontdesk/0 run-now                      # a pass now, in the configured mode
juju run frontdesk/0 stats [queue=true] [since=30d]
juju ssh frontdesk/0 -- sudo -u ubuntu frontdesk --url <url> --interactive
juju ssh frontdesk/0 -- journalctl -u frontdesk -f
```

On the unit:

| path | what |
|---|---|
| `/srv/frontdesk/usf/` | the bot, replaced wholesale on upgrade |
| `/usr/local/bin/frontdesk` | runs it with the timer's environment and lock; `frontdesk stats ...` runs stats.py |
| `/etc/frontdesk/` | `environment` and the secrets (root:ubuntu, read-only to the bot) |
| `/var/lib/frontdesk/` | `state.db`, `audit.jsonl`, `pass.lock` -- **what a backup must keep** |
| `/var/cache/frontdesk/` | launchpadlib caches, disposable |
| `~ubuntu/.config/opencode/opencode.jsonc` | the tool-less agent, rewritten by the charm |

Moving an existing deployment: copy `state.db` and `audit.jsonl` into
`/var/lib/frontdesk/` (owned by ubuntu) while `mode=off`, before the first
pass -- otherwise the whole queue is re-triaged once (#130).
