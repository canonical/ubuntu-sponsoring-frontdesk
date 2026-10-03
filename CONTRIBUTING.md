# Contributing

## Development environment

The bot writes to a public community tracker, so it's worth hacking on it
somewhere that isn't the machine running the real thing. The project ships a
[Workshop](https://ubuntu.com/workshop/docs/) definition for that.

### Workshop in one minute

Workshop builds a **container you keep**. The definition in
`.workshop/dev.yaml` describes how the container is *provisioned*; anything
you do inside it afterwards (authenticating tools, scratch files) simply
stays there. Think "a machine I set up once", not "a fresh sandbox per
command".

Two pieces make it up:

- **`.workshop/dev.yaml`** -- the workshop: which Ubuntu base, which SDKs,
  and the named `actions` you can run.
- **`.workshop/frontdesk-dev/`** -- an *in-project SDK*. SDKs are the unit
  of "how this environment gets built": only they have hooks, so anything
  installed lives here rather than in `dev.yaml`. Published SDKs can't know
  about our distro packages, hence one in the repo. `setup-base` runs as
  root at launch (apt packages, ruff), `setup-project` runs as your user
  afterwards, and `check-health` reports whether the result is usable via
  `workshopctl set-health`.

`dev.yaml` also composes Canonical's **`opencode` SDK**, so the LLM phases
have `opencode` without anyone installing it by hand.

The project directory is bind-mounted at `/project` inside the container, so
your editor stays on the host and the container only supplies the runtime.

### First run

```
snap install workshop --classic     # if needed
workshop launch dev                 # builds the container (a few minutes)
workshop run dev check              # ruff + the unit suite
```

Name the workshop when running an action. With a single bare argument
(`workshop run check`) Workshop can't tell an action name from a workshop
name and asks you to disambiguate; `workshop run -- check` also works.

Actions (`workshop actions` lists them):

| action | what it does |
|---|---|
| `check` | `make all`: `ruff check`, `ruff format --check`, then the unit suite |
| `stats` | the audit-trail retrospective (`stats.py`) |
| `triage` | `main.py` on one item; it defaults to `--dry-run`, so nothing is written unless you pass a write mode |

Arguments go after the action: `workshop run dev triage --url <url>`.

For anything narrower, use the tools directly:

```
workshop exec dev -- python3 -m pytest usf/tests/ -k rebuild
workshop shell dev                  # interactive shell inside
```

### Configuring credentials, once

`check` and `stats` need nothing. Everything that talks to Launchpad or the
LLM needs setting up by hand the first time, inside the container:

```
workshop shell dev
```

- **Launchpad**: run `python3 usf/main.py --url <bug-or-mp-url>` (dry-run by
  default). launchpadlib prints an authorisation URL on first use; approve
  it and the token is cached in
  `~/.cache/ubuntu-sponsoring-frontdesk/`.
- **opencode** (only needed to exercise the LLM phases): it's already
  installed by its SDK, so just `opencode auth login` for your Copilot
  subscription and pick the model you want. `setup-project` writes
  `~/.config/opencode/opencode.jsonc` with the tool-less
  `sponsoring-reviewer` agent the bot requires (see `doc/design_journal.md`
  #99) -- add a `"model"` line to that agent to pin one rather than use
  opencode's default. Your edits are kept: the hook only writes the file
  when it's missing.

Two things to know:

- **Do it once and it sticks.** `workshop refresh` re-provisions the
  container and clears its home directory, but both credential stores are
  declared as [mount interface
  plugs](https://ubuntu.com/workshop/docs/how-to/develop-sdks/configure-mount/)
  (`workshop-target`), which Workshop keeps outside the container:
  `~/.cache/ubuntu-sponsoring-frontdesk` by our SDK, and opencode's config
  and data by its own. Verified across a re-provisioning refresh. The
  backing directories live under `~/.local/share/workshop/id/<id>/dev/mount/`
  on the host -- `workshop info dev` prints them -- so they're outside the
  checkout and can't be committed by accident. Anything else you leave in
  the container's home does not survive.
- **The container is not a security boundary for credentials.** Authorising
  the real bot account inside it grants that account's write access to
  anything running there. `--dry-run` is what keeps a mistake harmless.

### Running on the host instead

The bot needs system Python (`apt_pkg` from `python3-apt` is a compiled
extension tied to it), so there's no virtualenv:

```
sudo apt install python3-apt python3-debian python3-launchpadlib python3-yaml \
                 python3-pytest distro-info
make all
```

See the README's deployment notes for credentials, the operator webhook and
the `opencode` agent the LLM phases need.

## Before proposing a change

- `make all` must pass: `ruff check`, `ruff format --check`, then the suite.
- Every behavioural change gets a numbered entry in
  `doc/design_journal.md` explaining *why*, plus a test. Live-found fixes
  should name the bug or MP that triggered them.
- A check that posts to Launchpad is reviewed in `--interactive` mode
  against a real item before it runs unattended; say so in the journal when
  something hasn't been live-verified yet.
