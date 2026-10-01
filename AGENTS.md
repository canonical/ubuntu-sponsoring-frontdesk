# Working on Frontdesk

Conventions for anyone -- human or agent -- changing this project. They
exist because this bot writes to a public community tracker: a bad comment
is seen by a contributor who did nothing wrong.

## Document what the bot says

`doc/CHECKS.md` is the contributor-facing reference *and* where the posted
wording is reviewed. Any change to a check's wording, tier, or resulting
action updates it in the same commit. A new check is added to its table with
its wording quoted.

`tests/test_checks_doc.py` enforces the mechanical half: every
finding-producing check must be named in the doc, and every quote in the doc
must still exist in `checks.py` or `llm_reviewer.py`. If it fails, the doc
is behind the code -- fix the doc, don't relax the test.

Check numbers are assigned in the `checks.py` docstring and referenced by
`main.py`'s comments and `doc/CHECKS.md`. Keep the three in agreement.

## Explain why, in the journal

Every behavioural change gets a numbered entry in `doc/design_journal.md`
saying what prompted it and why the chosen behaviour is right -- not what
the diff does. Live-found fixes name the bug or merge proposal that
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

- `make all` must pass: `ruff check`, `ruff format --check`, then the suite.
  Read the whole output, not the last line -- the format step passing says
  nothing about `ruff check`.
- New behaviour comes with a test.
- Commit messages wrap at 72 columns.
- A check that posts to Launchpad is exercised in `--interactive` mode
  against a real item before it runs unattended. Say so in the journal when
  something hasn't been live-verified yet.

`CONTRIBUTING.md` covers setting up an environment to do any of this in.
