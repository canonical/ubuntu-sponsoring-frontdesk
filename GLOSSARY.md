# Glossary

Terms and concepts used across this codebase and its docs (`design_journal.md`,
`STATUS.md`). Two kinds: general Ubuntu/Launchpad sponsoring vocabulary, and
terms specific to this bot's own design. Entries point at `design_journal.md`
`#N` where the reasoning lives — this file defines, it doesn't re-argue.

## Ubuntu / Launchpad sponsoring vocabulary

- **Sponsoring queue** — the set of bugs and merge proposals (MPs) waiting for
  an Ubuntu archive uploader (a "sponsor") to review and land them on behalf of
  a contributor who can't upload directly. Sourced from
  `sponsoring-reports.ubuntu.com/jsons/sponsoring.json` (#5).
- **MP (Merge Proposal)** — a Launchpad git merge proposal
  (`branch_merge_proposal`). This bot only handles git MPs; Bazaar support was
  dropped (#7).
- **Merge (contribution type)** — an MP that rebases Ubuntu's packaging onto a
  newer Debian revision ("sync the delta forward"). Distinguished from a plain
  **fix** (an Ubuntu-specific change) and an **SRU** upload. The
  merge/fix/SRU distinction drives which checks apply and what "correct
  target branch" even means — see `_is_merge_proposal`, #20.
- **SRU (Stable Release Update)** — a fix backported to an already-released
  Ubuntu series (targets `ubuntu/<series>`, e.g. `ubuntu/jammy`), governed by
  its own process (SRU template, regression testing) mostly out of scope for
  this bot so far (#20).
- **Sync request** — a bug asking for a Debian package to be copied into
  Ubuntu as-is (no code review needed, since nothing Ubuntu-specific changed).
  Always a bug, never an MP. See `_is_sync`, #18.
- **Delta** — the set of Ubuntu-specific changes on top of the Debian
  packaging, detectable from a `ubuntuN` suffix in the version's revision
  (`buildN`, a no-change rebuild, doesn't count). "Has a delta" vs. "no delta"
  is the first fork in sync-request triage (#18).
- **`devel` / devel-proposed`** — the current Ubuntu development series
  (`archive_lookup.devel_codename`) and its `-proposed` pocket, where new
  uploads land before hitting the release pocket. Checks compare against
  `<devel>-proposed` first, falling back to `<devel>` (#27).
- **Pocket** — an archive subdivision per series (`release`, `proposed`,
  `updates`, `security`, ...). This bot mostly cares about `-proposed` vs. the
  plain release pocket for `devel`.
- **FFe (Feature Freeze Exception)** — required after a cycle's Feature
  Freeze date for a sync/upload that introduces a new feature, not just a bug
  fix. Backlog, not wired in yet (#19); the freeze date itself lives in
  `release_schedule.py`.
- **git-ubuntu** — the tool/workflow Ubuntu packaging MPs are built on
  (`source_git_path` naming conventions, its own importer bot). Two
  git-ubuntu-specific quirks this bot works around: it **rejects direct
  `queue_status` writes** (#25 — use a review vote instead) and its importer
  **auto-closes an MP once it notices the change has landed**, which can lag
  (#27's grace period exists because of this).
- **Review vote** — Launchpad's actual mechanism for flagging an MP
  (`Approve`, `Needs Fixing`, `Needs Information`, `Abstain`, `Disapprove`,
  `Needs Resubmitting`), attached to a comment via
  `BranchMergeProposal.createComment(vote=...)`. Replaces the direct status
  write this bot used to (wrongly) attempt (#25).
- **Changelog stanza / entry** — one version's block in `debian/changelog`
  (header line + bullet points). "The new/top stanza" = the entry the MP or
  patch is proposing, always the first one in the diff.
- **`LP: #NNNNNN`** — the changelog convention for citing a Launchpad bug
  fixed by that entry; format varies in the wild (`LP: #123`, `LP #123`,
  `LP#123`). Checked for accuracy by `check_changelog_bug_reference` (#26).
- **madison** — the archive-query format/tool (`rmadison`, or its web
  equivalents) that answers "what versions of package X exist, in which
  suites." This bot queries live madison-format HTTP endpoints instead of
  shelling out to the CLI (#21, #23).

## Bot-specific architecture and terms

- **Check** — one deterministic Python function in `checks.py`
  (`check_administrative_state`, `check_nothing_to_sponsor`,
  `check_target_branch`, `check_mp_conflicts`, `check_empty_diff`,
  `check_changelog_bug_reference`, `check_stale_version`), run in a fixed
  order before the LLM phase. See `flow.dot`/`flow.svg`.
- **Fires** — a check "fires" when it finds something to flag (returns
  truthy). Since #31/#44, only `closing`-tier outcomes short-circuit
  `triage_url` (the item is already resolved); `incomplete`-tier outcomes
  return a Finding and the pipeline keeps evaluating so every simultaneous
  problem surfaces in the same pass.
- **The `None` / `False` / truthy return contract** — every check's return
  value means: `False` = definitively checked, nothing to flag (stable, safe
  to cache); truthy = fired; `None` = an external lookup/fetch failed,
  genuinely undetermined (retriable, must NOT be cached as clean). See
  [[check-lookup-failure-vs-nothing-to-flag]], #28.
- **Inconclusive** — `main.py`'s `triage_url` flag, set when any check
  returns `None`. Gates whether the eventual terminal `update_status` call
  persists `facts` (see next entry) or omits it so the URL is retried next
  run regardless of whether the contributor changed anything (#28).
- **Facts (facts-based re-triage)** — a snapshot of contributor-controlled
  signals (`facts.build_facts`) stored per URL in `state.db`. Re-triage only
  happens when the stored snapshot differs from a freshly built one, or
  `--force`/`inconclusive` bypasses the gate. Deliberately excludes
  bot-mutated fields (e.g. `queue_status`) so the bot never mistakes its own
  write for a contributor change (#10). Equivalent to pmtriage's
  `facts.yaml`.
- **Status** (`state.db` column, informational only) — one of `DONE`,
  `WAITING_ON_CONTRIBUTOR`, `INCOMPLETE`→folded into `WAITING_ON_CONTRIBUTOR`,
  `READY_FOR_HUMAN`, `SYNCED`→`DONE`, `PENDING_ARCHIVE_IMPORT`. Not a gate by
  itself — see facts, above (#10 replaced the old "status is terminal" model).
- **`PENDING_ARCHIVE_IMPORT`** — status used only by `check_stale_version`'s
  grace-period deferral (the `"pending"` outcome, #27): a matching upload
  exists in the archive but is <24h old, so git-ubuntu's importer might
  auto-close the MP on its own; the bot waits rather than racing it.
- **Write modes** — `--dry-run` (default, logs intended writes, does
  nothing), `--interactive` (`[y/N]` prompt, refuses without a TTY), `--yes`
  (unattended/cron). Enforced by `LPClient._decide` (#11). Every attempt,
  including skips, is appended to `audit.jsonl` (#14).
- **Fail-safe** — this bot's standing bias: when a signal is ambiguous or a
  lookup fails, prefer no action / route to a human over guessing and
  auto-rejecting a contributor. Applied throughout (#9, #20 §"Fail-safe
  stance", #26, #28).
- **LLM phase** — runs after the deterministic checks unless a closing-tier
  outcome resolved the item or the pass went inconclusive (#44 skips the LLM
  then, since its finding couldn't be posted anyway); invokes the `opencode`
  CLI (#4) for qualitative judgment (bugs: SRU template completeness, sync
  delta justification; MPs: the #47 content review, see "MP content review")
  and never holds write credentials itself — it returns a recommendation,
  `main.py` performs the write (#9).
- **MP content review** — `triage_mp` (#47): one LLM call over the new
  changelog stanza + the `debian/` diff (capped; upstream files listed by
  path only) judging stanza quality, changelog-vs-diff consistency, and a
  Feature Freeze classification (feature past FF → "will need an FFe"
  bullet). All findings advisory (`question` tier, `ADVISORY` status →
  `Finding("question", ...)` per bullet); malformed LLM output means
  silence. Skips quietly when the diff is empty or adds no complete new
  changelog stanza.
- **Verdict block** — the fenced `yaml` block (`verdict: pass|fail`,
  `reason:`) the LLM is asked to end its reply with; parsed by
  `_extract_verdict`, fails safe to `READY_FOR_HUMAN` on anything missing or
  malformed (#9).
- **`archive_lookup.py`** — module for archive-state queries: Ubuntu versions
  and devel series via the Launchpad API, Debian versions via the live
  ftp-master `madison` endpoint (not a mirror, no publish-run lag, #23),
  version comparison via `apt_pkg.version_compare` (#18, #21).
- **`--verbose`** — logs every check's decision step (fired and skipped, not
  just fired) via the standard `logging` module, for reconstructing why a
  check did or didn't trigger without re-deriving the logic by hand (#24).
  Also logs a `[timing]` line per step (`load_url`, `build_facts`, each
  check, the LLM phase) plus a `TOTAL` per URL (#32), for seeing where a
  run's time actually goes. All bot output (not just verbose logging) goes
  through `logging` rather than `print` as of #32, so output is a single
  correctly-ordered stream even when redirected/piped.
- **Smoke test** — a read-only, no-write check of real Launchpad object
  attributes (`make smoke URL=...`), used to validate an assumption (e.g. an
  attribute's actual name) against production before trusting it in a check.
  Caught the `has_conflicts` vs. `preview_diff.conflicts` bug (#16).
- **Live-validated** — a design or fix that was run against a real Launchpad
  URL (always `--dry-run`) and its output manually inspected, not just
  covered by unit tests against fakes. Called out explicitly throughout
  `design_journal.md` because fakes have repeatedly encoded wrong assumptions
  that only production data caught (#16, #20, #26, #27, #28).
- **Finding** — one check's (or the LLM phase's) individual observation:
  `checks.Finding(tier, message)`. Implemented in #44 (designed as #31):
  `main.py` collects every fired Finding across the whole pass and posts
  them as ONE aggregated comment (`render_findings_comment`), replacing the
  old first-fire-wins one-comment-per-check model.
- **`closing` / `incomplete` / `question`** — the three finding severity
  tiers (#31/#44), in priority order for picking a run's one status/vote:
  `closing` (already resolved, short-circuits with its own terse comment,
  and *drops* any findings collected earlier — no nitpicking a change that
  already landed) > `incomplete` (hard requirement, aggregates into "needs
  fixing", drives the one `Needs Fixing` vote) > `question` (non-blocking
  advisory, never changes status, aggregates into "nice to have"; produced by
  the #47 MP content review). Named to avoid colliding with the pre-existing,
  unrelated **inconclusive** (above) — a deliberate naming choice made
  during design, not an accident.
- **Aggregated comment** — the single templated review comment per pass
  (#44): one intro, a "needs fixing" bullet section, an optional "nice to
  have" section, one closing line. Individual finding messages carry no
  greeting/sign-off of their own (the LLM's SRU/sync messages included,
  #46). An inconclusive pass posts no aggregate at all and skips the LLM —
  the comment must not claim to be the complete list when it isn't.
- **Human-engaged suppression** — #45 (designed as #35): if anyone other
  than the MP's submitter, the bot itself, and known `SERVICE_ACCOUNTS`
  commented since the current `preview_diff` was created, the whole
  aggregate is silenced (facts persist; quiet until a new push) — a human
  review is in progress and the bot must not talk over it. Only ever
  suppresses findings-tier output; closing-tier outcomes are decided before
  it is consulted. MPs only so far; the bug-side anchor is an open question.
- **`SERVICE_ACCOUNTS`** — frozenset in `checks.py` of Launchpad usernames
  whose comments never count as human engagement (`~ubuntu-sponsoring-bot`,
  `~git-ubuntu-bot`, `~git-ubuntu-import`, `~janitor`). Empirically
  future-proofing: no service account had ever commented on a tracked MP
  (git-ubuntu closes via status change; the janitor posts on bugs).
- **Nothing to sponsor** — `check_nothing_to_sponsor` (#46), closing tier,
  bugs only: unsubscribes ~ubuntu-sponsors when the bug's fix is under
  review on a linked MP (sponsors-as-reviewer or an actual review vote —
  the bug is a duplicate queue entry) or when there is no patch and no
  linked MP at all (sync requests exempt — they legitimately carry no
  patch). Runs before the LLM, so a patch-less bug is never LLM-reviewed.
