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
- **`PENDING_UPLOAD_QUEUE`** — status for `check_stale_version`'s `"queued"`
  outcome (#55): the proposed version is already uploaded and sitting in the
  target series' upload queue (Unapproved/New/Accepted — invisible to
  publication lookups; typical for an SRU awaiting the SRU team). Nothing to
  sponsor while it waits: silent, LLM phase skipped, no facts persisted, so
  each run re-triages it until publication flips it to the "done" close-out
  (or a queue rejection makes it a live sponsoring item again). Only fires
  when the queued upload's `.changes` `Changes:` field matches the MP's
  stanza (#56 — SRU version increments are convention-fixed, so a
  same-version queue hit can be an independent racing SRU; different
  content bounces as "rebase with a new version" instead).
- **SRU "fix newer series first" check** — Check 7, `check_sru_newer_series`
  (#58): SRU policy requires the fix to land in the development release
  (and, by extension, every supported series newer than the SRU's target)
  first. Handled = task Fix Released/Committed, a linked MP targeting that
  series, or a series-named patch attachment; otherwise one LLM question
  asks whether the bug text says it's already fixed there (task tables are
  often stale — updating them needs privileges submitters usually lack).
  Always advisory (question tier), never a reject reason.
- **Direct source-edit check** — Check 8, `check_direct_source_edit`
  (#60): a fix MP whose diff touches files outside `debian/` gets an
  incomplete-tier bounce — changes to upstream code must be provided as
  patches under `debian/patches`
  (https://ubuntu.com/project/docs/contributors/bug-fix/apply-the-fix/).
  Silently exempt: merge MPs, new upstream versions, and native packages
  (no Debian revision in the version — read from the new stanza, or from
  the archive when the MP carries no stanza at all). MP-only today; the
  same rule for debdiff attachments on bugs is backlog (STATUS item 43).
- **Missing changelog-entry check** — Check 9,
  `check_missing_changelog_stanza` (#61): a fix MP whose diff doesn't
  touch `debian/changelog` at all gets an incomplete-tier bounce asking
  for a new changelog entry with an incremented version number
  (https://ubuntu.com/project/docs/contributors/updating/commit-changes/#write-the-changelog-entry).
  The `LP: #nnn` suggestion appears only when a bug is linked to the MP
  or referenced in its commit message/description. A touched changelog
  without a new header (appending to an UNRELEASED entry) is clean;
  merge MPs are exempt.
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
  path only) judging stanza quality (`observations`, `kind="advisory"`),
  changelog-vs-diff consistency (`mismatches`, `kind="verify"`, #53), and a
  Feature Freeze classification (feature past FF → "will need an FFe"
  bullet, `kind="verify"`; the classification question itself is skipped
  in the prompt entirely pre-freeze, #52, since the bullet can never fire
  before then, and for SRU-targeted MPs regardless of date, #59, since FF
  is a devel-series concept). All findings advisory (`question` tier, `ADVISORY` status
  → `[(kind, bullet), ...]` → one `Finding("question", bullet, kind=kind)`
  per pair); malformed LLM output means silence. Skips quietly when the
  diff is empty or adds no complete new changelog stanza. Bullets must be
  double-quoted in the YAML the model returns — an unquoted bullet
  containing ": #" (a bug reference like "LP: #123") gets misparsed as a
  YAML mapping-key-plus-comment, which is dropped defensively rather than
  posted as garbage (#53). The consistency question is scoped to *what
  changed* (contradicted claims, whole unmentioned files/fixes/patches),
  not *how it works*: a stanza that correctly names and attributes a patch
  is never flagged for omitting that patch's internal implementation
  details (#54).
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
  `checks.Finding(tier, message, kind="advisory")`. Implemented in #44
  (designed as #31); `kind` added in #53. `main.py` collects every fired
  Finding across the whole pass and posts them as ONE aggregated comment
  (`render_findings_comment`), replacing the old first-fire-wins
  one-comment-per-check model.
- **`closing` / `incomplete` / `question`** — the three finding severity
  tiers (#31/#44), in priority order for picking a run's one status/vote:
  `closing` (already resolved, short-circuits with its own terse comment,
  and *drops* any findings collected earlier — no nitpicking a change that
  already landed) > `incomplete` (hard requirement, aggregates into "needs
  fixing", drives the one `Needs Fixing` vote) > `question` (non-blocking
  advisory, never changes status, aggregates into "please verify" or "nice
  to have" depending on `kind`; produced by the #47 MP content review).
  Named to avoid colliding with the pre-existing, unrelated
  **inconclusive** (above) — a deliberate naming choice made during
  design, not an accident.
- **`kind: "advisory" | "verify"`** — sub-classification within the
  `question` tier only (#53). `"advisory"` is for findings the check is
  confident really don't matter (e.g. a terse-but-adequate changelog
  bullet) — renders in "Nice to have (non-blocking)". `"verify"` is for
  findings that WOULD block if true, but the check (typically an LLM
  judgment) isn't confident enough to vote/bounce on automatically (a
  stanza/diff mismatch, or the Feature-Freeze-Exception classification) —
  renders in its own "Please verify (not confirmed...)" section instead,
  read as an assertion to double-check rather than an optional nicety.
- **Aggregated comment** — the single templated review comment per pass
  (#44, `kind`-aware sectioning added #53): one intro, a "needs fixing"
  bullet section, an optional "please verify" section, an optional "nice
  to have" section, one closing line. Individual finding messages carry no
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

- **Operator notifications** — `notify.py` (#48): best-effort pings to a
  Mattermost incoming webhook, strictly for anomalies the bot detected but
  can't act on and the contributor can't fix (stuck Launchpad diff
  generation; git-ubuntu's importer failing to auto-close an
  already-uploaded MP — the latter replaces the "can be closed" MP comment
  when configured, per #43's switch). Never a mirror of queue statuses.
  The webhook URL lives outside the VCS in
  `~/.config/ubuntu-sponsoring-bot/config.ini` (`[notifications]
  webhook_url`); unconfigured means disabled. Dry-run logs "would notify".

- **Rich history** — the contributor's actual git commits, preserved into
  the official git-ubuntu packaging branches when an upload's `.changes`
  carries the `Vcs-Git` / `Vcs-Git-Commit` / `Vcs-Git-Ref` keys pointing at
  a repo containing them. Without that, the importer synthesizes an
  "Imported using git-ubuntu import." commit and the MP's history is lost
  (work that will likely need redoing at the next merge).

- **Rich-history diagnosis** — #49, runs in `check_stale_version`'s "done"
  case (change already uploaded, MP not auto-closed): reads the uploaded
  `.changes` and, when the Vcs keys are present, checks *ancestry* (is the
  MP's proposed commit contained in the uploaded history? --
  `git_history.commit_contains`, sandboxed scratch fetch; hash equality
  would false-positive on a sponsor legitimately stacking a fixup commit).
  Outcomes: missing keys (A) or diverged history (B2) → informational MP
  comment for the sponsor/uploader + channel ping for the git-ubuntu
  maintainers; contained (B1) → pure importer/autoclose bug, comment +
  ping; undeterminable → the plain #48 ping.

- **needs-packaging bug** — a request for a package that doesn't exist in
  Ubuntu yet; Launchpad shape is a task targeting plain `ubuntu` (no
  package, no series). Has no existing branch to attach a patch/debdiff
  to, so `check_nothing_to_sponsor` (#50) treats a PPA/git-repo link in
  the description or comments as the normal shape of a proposed
  contribution there and leaves it for a human instead of auto-closing.
