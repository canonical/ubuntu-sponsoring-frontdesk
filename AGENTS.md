# Working on Frontdesk

Conventions for anyone -- human or agent -- changing this project. They
exist because this bot writes to a public community tracker: a bad comment
is seen by a contributor who did nothing wrong.

## Document what the bot says

`doc/CHECKS.md` is the contributor-facing reference *and* where the posted
wording is reviewed. Any change to a check's wording, tier, or resulting
action updates it in the same commit. A new check is added to its table with
its wording quoted.

`usf/tests/test_checks_doc.py` enforces the mechanical half: every
finding-producing check must be named in the doc, and every quote in the doc
must still exist in `usf/checks.py` or `usf/llm_reviewer.py`. If it fails, the doc
is behind the code -- fix the doc, don't relax the test.

Check numbers are assigned in the `usf/checks.py` docstring and referenced
by `usf/main.py`'s comments and `doc/CHECKS.md`. Keep the three in agreement.

## Keep the flow chart current

`doc/flow.dot` is the developer view of `usf/main.py`'s `triage_url`: the gates, the check
order, what short-circuits what, and the terminal states. A change to any of
those updates the chart in the same commit, and the committed renders are
regenerated from it:

    dot -Tsvg doc/flow.dot -o doc/flow.svg
    dot -Tpng doc/flow.dot -o doc/flow.png

(`doc/write_gate.dot` and `doc/sync_triage.dot` cover the write gate and the
sync sub-flow, same rule.) No test can catch a stale diagram, which is
exactly how it previously drifted: it stopped at Check 12, missing three
checks and a terminal state, while nothing failed. So the rule is the only
guard -- adding a check means adding its node.

## Explain why, in the journal

Every behavioural change gets a numbered entry in `doc/design_journal.md`
saying what prompted it and why the chosen behaviour is right -- not what
the diff does. Entries are numbered sequentially across the whole project,
charm included: continue from the last one in the file rather than starting
a new scheme. Live-found fixes name the bug or merge proposal that
triggered them, so the case can be re-checked later.

Changes to tiers or wording are design decisions: propose the wording for
review before committing it.

## Fail safe

A check returns `False` when it definitively found nothing (that result is
cached), a finding when it fired, and `None` when a lookup failed. `None`
means the item is retried rather than acted on: never guess from missing
data, and never post on a lookup that didn't complete.

Prompts embed contributor-written text, so LLM calls run under the tool-less
`sponsoring-reviewer` agent and any reply containing tool use is discarded
(design journal #99). Don't loosen that.

## Before committing

- `make all` must pass: `ruff check`, `ruff format --check`, then the suite,
  for both halves -- `usf-check` (the bot, system python3) and
  `charm-check` (the charm, uv). Read the whole output, not the last line --
  the format step passing says nothing about `ruff check`, and the bot's
  suite passing says nothing about the charm's.
- A change to the charm (`src/`) should be checked on a real model, not
  only unit-tested -- the charm's journal entry (#146) lists four bugs that
  only showed up that way. But `make charm-integration` packs the charm and
  deploys to a juju controller: slow, and a change to the contributor's
  system they may not have or want. So it is never run unprompted: when a
  change touches the charm, *suggest* it, say what it would do and roughly
  how long it takes (~15 minutes), and run it only once the person you're
  working with agrees. If they decline, say in the journal entry that the
  change wasn't deployed.
- New behaviour comes with a test.
- Commit messages wrap at 72 columns.
- A check that posts to Launchpad is exercised in `--interactive` mode
  against a real item before it runs unattended. Say so in the journal when
  something hasn't been live-verified yet.

`CONTRIBUTING.md` covers setting up an environment to do any of this in.
