# Status & Handoff

_Last updated: 2026-07-10_

Snapshot of where the bot stands, how to run it, and what's next. Architectural
rationale lives in `design_journal.md`.

## What the bot does

Triages the Ubuntu sponsoring queue (bugs + merge proposals): deterministic
Python checks first, then an LLM phase (via `opencode`) for qualitative judgment
(SRU template completeness, sync delta explanation, MP changelog/diff content
review, SRU newer-series coverage). The LLM only ever produces
a *recommendation*; the orchestrator performs writes, behind a confirmation gate.
Everything the pass found is posted as ONE aggregated review comment (#44), and
that comment is suppressed entirely once a human reviewer is already engaged on
the item (#45). Current pipeline: `flow.svg`.

## How to run

```bash
make test                       # unit suite (needs pytest, launchpadlib, pyyaml, python3-apt)
make smoke URL=<lp-url>          # read-only attribute check against real Launchpad
python3 main.py --url <url> [--dry-run|--interactive|--yes] [--verbose]
python3 main.py --all  [--dry-run|--interactive|--yes] [--force] [--verbose]
```

Write modes: **`--dry-run`** (default; logs intended writes, does nothing),
`--interactive` (`[y/N]` per write; refuses if no TTY), `--yes` (unattended/cron).
Every write attempt is recorded in `audit.jsonl`. `--verbose` (design_journal.md
#24, #32) logs every decision step each check considered (not just the ones
that fired) plus a `[timing]` line per step and a `TOTAL` line per URL --
useful both for working out why a check did or didn't trigger and for seeing
where a slow run's time actually goes. All output goes through `logging`
(design_journal.md #32), not `print`, so it's a single correctly-ordered
stream even when redirected/piped.

Auth: first run opens a browser to authorize; the token persists to
`~/.cache/ubuntu-sponsoring-bot/credentials`, so later runs are non-interactive.

## Done (2026-06-29 hardening pass + validation)

Fixes 1–6 + auth + a real bug found in validation. See `design_journal.md` #9–#16.

- Structured YAML LLM verdicts, fail-safe to human (never auto-reject on doubt)
- Facts-based re-triage (replaces terminal `DONE`); excludes bot-mutated fields
- Explicit write modes (fixes cron-vs-TTY crash)
- Comment dedup via Launchpad identity (`self.lp.me`), exact-content match
- Admin check scoped to the relevant Ubuntu series tasks for `source_package`
- pytest suite + `audit.jsonl`
- Auth via persisted `credentials_file` (headless/cron-safe)
- **Validated against real Launchpad:** opencode path, auth persistence, and
  all bug+MP attributes. The MP smoke test caught that conflicts live on
  `preview_diff.conflicts`, not `has_conflicts` (now fixed).
- **Bounce = Incomplete** (journal #17): an `INCOMPLETE` bug now has all open
  Ubuntu tasks set to `Incomplete`; `~ubuntu-sponsors` stays subscribed. The
  bot's own status write is folded into the facts snapshot so it doesn't
  re-review itself next run.

## Next (resume here)

1. **Rule B — stale-bounce sweep (DESIGNED, not built).** For any bug with an
   Ubuntu task in `Incomplete`, check activity since `bug_task.date_incomplete`:

   - **New attachment** → flip tasks back to New immediately (attachment content
     not yet readable via lpcli; canonical/lpcli#23 filed; revisit once fixed).
   - **New comment(s)** → pass comment text to LLM (read via launchpadlib,
     injected into prompt) + the stored bounce reason; LLM judges whether the
     response addresses the bounce. Yes → flip to New. No + >30d → sweep.
     No + <30d → leave (give them more time).
   - **No activity + >30d** → sweep: post final comment ("no reply in a month,
     unsubscribing ~ubuntu-sponsors; re-subscribe if you update the bug") +
     unsubscribe sponsors.
   - **No activity + <30d** → nothing yet.

   Requires: store `bounce_reason` in `state.db` when setting Incomplete.
   Before implementing: smoke-test `bug_task.date_incomplete` and
   `bug.attachments[].date_created` exist in the real LP API
   (canonical/lpcli#24 also filed re: attachment visibility in lpcli).
2. **Bot account (DONE, 2026-07-07).** The cached OAuth token was swapped;
   the bot now authenticates as `~ubuntu-sponsoring-bot` (verified live:
   `lp.me` resolves to it). The pieces that keyed on identity were already
   prepared: `SERVICE_ACCOUNTS` contains `~ubuntu-sponsoring-bot`, so the
   bot's own comments can't trigger the #45 human-engaged suppression; and
   pre-switch `~seb128` bot comments now *count* as human engagement, which
   is benign-to-correct (already-bounced items stay quiet until a new push
   resets the diff anchor). Residual switch-day caveats, accepted: (a)
   `~seb128`-posted comments are invisible to the new account's exact-match
   dedup, so a re-triaged item could get a one-time duplicate comment; (b)
   whether the bot account can unsubscribe `~ubuntu-sponsors` from bugs
   (team unsubscribe may need membership) and whether it holds the
   git-ubuntu `queue_status` grant (#25) are unverified until the first
   real writes — watch the first `--interactive` run for "Could not
   unsubscribe" warnings.
3. **First real `--interactive` run (DONE, 2026-07-04).** MP #507575
   (grub2, `~sharkcnnnnnn`): `check_stale_version` correctly found the
   proposed version already published with different content, prompted
   `[y/N]`, seb128 approved, comment + `vote="Needs Fixing"` posted for
   real -- confirmed both in `audit.jsonl` (`outcome: "performed"`) and
   directly on the MP on Launchpad. First real write this bot has ever
   made outside a smoke test.
4. **MP LLM content review (DONE, 2026-07-07).** See design_journal.md #47.
   `triage_mp` (formerly a stub) now sends the new changelog stanza + the
   `debian/` diff (capped, upstream files listed by path only) to the LLM
   for three judgments: stanza quality, changelog-vs-diff consistency, and
   a Feature Freeze classification (a feature past FF gets a "will need an
   FFe" bullet — the foundation for the #19 FFe check). Everything is
   `question`-tier (advisory: "nice to have" section, no vote, no bounce)
   until live accuracy is proven — first real producer of that tier. Any
   parsing doubt means silence, not noise. Reuses the per-item diff memo
   (now caching the full diff text) so the LLM phase costs no extra
   librarian fetch. Remaining follow-ups: promote clearly-content-free
   stanzas to `incomplete` later; DEP-3 headers / patch review still out
   of scope.
5. **Tool-using investigation triage** — pull packages, check Debian/upstream
   trackers, git status (the richer roadmap; opencode agent with tools).
6. **Hygiene (DONE).** `make lint`/`make fmt` targets + `.github/workflows/ci.yml`
   running ruff + pytest; whole tree is `ruff check`/`ruff format` clean. The
   brittle SRU/sync detection in `llm_reviewer.triage_bug` is replaced by
   `_is_sru`/`_is_sync` (case- and whitespace-tolerant bracketed-header and
   title matching), covered by `tests/test_request_detection.py`.
7. **Sync request triage (DONE).** See design_journal.md #18 and
   `sync_triage.dot`/`.svg`/`.png`. Splits sync-titled bugs on whether the
   currently published Ubuntu version has a delta (`ubuntuN` in the revision),
   then checks archive state via `archive_lookup.py` (already-synced → close
   as `Fix Released` via the `SYNCED` status/`set_bug_tasks_fix_released`; not
   in Debian → LLM checks for a justification before bouncing) or reuses the
   existing `review_sync_request` LLM check for the has-delta path (now
   correctly gated on delta actually being present). `archive_lookup.py`
   (design_journal.md #21) gets Ubuntu versions from the Launchpad API (no
   local tool needed) and Debian versions from
   `api.ftp-master.debian.org/madison` over HTTP (design_journal.md #23) — the
   live `dak` database, not a mirror-index-backed endpoint, so there's no
   ~6h publish-run lag to work around (no `rmadison`/`devscripts` dependency
   either way); `LLMReviewer` now takes an `lp` object so it can reuse the
   bot's authenticated session. The target-branch check design (item 8 below)
   should reuse it.
   2.b (verifying "already in Debian" delta claims) is still backlog.
   **Also backlog:** Feature Freeze Exception handling for sync requests —
   see design_journal.md #19.
8. **`check_target_branch` merge/fix/SRU detection + sid/experimental (DONE).**
   See design_journal.md #20. Caught live: MP #507686 (a plain fix) was being
   bounced for targeting `ubuntu/devel`, which is only wrong for **merge**
   MPs (rebase onto a newer Debian revision). `checks._is_merge_proposal` now
   classifies an MP before applying the debian/* rule: primarily via
   `source_git_path` containing "merge" (validated 100% accurate against the
   live queue), falling back to linked-bug titles ("Merge \<pkg\> from
   Debian...") when that's inconclusive. Fix and SRU MPs (and merges already
   correctly targeting `debian/*`) are left alone. For a merge MP that does
   need bouncing, `checks._debian_target_suite` now also names the specific
   branch (`debian/sid` vs `debian/experimental`) in the comment: primary
   signal is the changelog-entry header on the diff-context line right below
   the new entry (read from `preview_diff.diff_text`, validated live against
   real merge diffs), falling back to `archive_lookup.debian_versions` when
   the diff doesn't resolve it; falls back further to the old generic
   "either one" phrasing when neither resolves — never guesses.
9. **MP review votes replace status writes; changelog bug reference check
   (DONE).** See design_journal.md #25, #26. Found while testing: git-ubuntu
   MPs don't accept direct `queue_status` writes (`setStatus` — a git-ubuntu
   limitation, not a bot bug), so every MP bounce (`check_mp_conflicts`,
   `check_target_branch`) had been silently ineffective. Fixed by attaching a
   Launchpad review vote to the bounce comment instead
   (`lp_client.comment(mp, message, vote="Needs Fixing")`); `set_status` is
   deleted (nothing else called it). `check_empty_diff` now comments with no
   vote (there's no "Merged" vote option — a human still closes it out).
   New **Check 5**: `check_changelog_bug_reference` warns (comment +
   `vote="Needs Fixing"`) when an LP bug cited in the new changelog entry
   (`LP: #123456`) isn't reported against this source package — catches
   copy-paste/typo'd bug numbers before they ship into the released
   changelog permanently. Validated live: correctly silent on a real merge's
   4 genuinely-matching bug references, and correctly flagged a real
   mismatch on another MP (bug cited was against a sibling package, not this
   one). Had to fix a real false-positive during validation first: git-ubuntu
   merge diffs can show most of `debian/changelog` as "added" (not a clean
   top-of-file insert), which was sweeping in bug numbers from old, unrelated
   entries — fixed by bounding the scan to the first changelog stanza only.
10. **Proposed version vs. archive sanity check (DONE, MPs only).** See
    design_journal.md #27. New **Check 6**: `checks.check_stale_version`
    compares the MP's proposed (top changelog entry) version against what's
    published in `<devel>-proposed` / `<devel>` (whichever is ahead). Older
    than the archive → bounce ("needs to be rebased", `vote="Needs
    Fixing"`). Same version → fetches the archive publication's changelog
    (`archive_lookup.published_source` + `changelog_text`, both new) and
    compares content: matching → "already uploaded, can be closed" (comment
    only, like the empty-diff case) **unless the publication is under 24h
    old, in which case it defers instead of commenting** (`"pending"`) —
    git-ubuntu's own importer may auto-close the MP itself once it catches
    up, and commenting immediately risks a race with that; different
    content → "duplicate version, needs a rebase" (`vote="Needs Fixing"`,
    never deferred). Newer than the archive → nothing to do. A fired result
    maps to several different outcomes, so this one returns
    `"needs_fixing"`/`"done"`/`"pending"`/`False`/`None` instead of a plain
    bool — `main.py` branches on the value (see item 11 for the `None`
    case).
    **Backlog:** the same check for a bug's attached patch (needs to read
    the proposed version out of the patch instead of an MP diff — a
    different extraction problem, deliberately deferred to keep this change
    scoped to MPs first). Also backlog: setting `queue_status` to `Merged`
    in the "already uploaded" case instead of just commenting — needs
    investigating whether the bot's account actually has that permission
    for git-ubuntu MPs (see the `TODO` in the code).
    **Live-validated 2026-07-03**, twice, against real MP #506927 (svxlink,
    `--dry-run --verbose --force`): all 6 checks ran correctly end to end
    both times. First run: `changelogUrl()` over plain `urllib` fetched
    fine (no auth issue) and `check_stale_version` correctly resolved case
    3a (matching content). Second run (same MP, after the grace-period fix
    below): correctly deferred instead of commenting, since the archive
    publication was only ~1h old — exactly the scenario the grace period
    exists for.
11. **Lookup failures must not be cached as "nothing to flag" (DONE).** See
    design_journal.md #28. Found live: a real network timeout fetching the
    svxlink archive changelog (item 10) was silently swallowed to a plain
    `False`, which let the pipeline fall through to `READY_FOR_HUMAN` and
    persist facts — permanently caching a transient failure as "checked,
    nothing wrong" until the contributor changed something. Every check
    with an external-lookup fail-safe path (`check_target_branch` via
    `_is_merge_proposal`, `check_changelog_bug_reference`,
    `check_stale_version`) now returns `None` specifically for "a lookup
    failed, couldn't determine" — distinct from `False` ("definitively
    checked, nothing to flag", safe to cache). `main.py` tracks an
    `inconclusive` flag across all 6 checks and skips persisting `facts` on
    every LLM-phase terminal branch when set, so the next run re-triages
    regardless of whether the contributor changed anything.
    **Also fixed the same day (was briefly a backlog item):**
    `check_mp_conflicts` and `check_empty_diff` accessed `preview_diff`
    attributes with no `try/except` at all; a lazily-fetched attribute
    raising on a network failure there would have propagated all the way
    out of `triage_url` uncaught, likely killing an entire `--all` run
    mid-queue — a different problem (crash-safety) from the rest of this
    item (cache-correctness). Both now wrap the `preview_diff` fetch in
    `try/except` and return `None` on failure, same convention as every
    other check. `main.py` needed no changes (the `None`-handling dispatch
    was already uniform across all 6 checks). `check_empty_diff` also
    picked up its first dedicated unit tests (previously untested) while
    this was being fixed.
12. **Upfront warning when `opencode` is missing (DONE).** See
    design_journal.md #29. Found while preparing the first `--interactive`
    run in a fresh container: `opencode` wasn't installed there, and the
    only signal was a per-item warning buried in `--all` output once an
    SRU/sync item happened to reach the LLM phase. `main()` now checks
    `shutil.which("opencode")` once at startup and prints an unmissable
    warning that LLM review is disabled and the bot is running in degraded
    (checks-only) mode — deterministic checks 1–6 are unaffected either way.
13. **Permanently-missing `preview_diff` no longer loops forever (DONE).**
    See design_journal.md #30. Found live: MP #503471 (plymouth) has had no
    `preview_diff` since it was created ~3 months ago — a stuck Launchpad
    job, not a transient failure. New `checks._diff_missing_is_still_generating`
    checks `date_created` against a 1h grace period: within it, still
    `None` (retriable); past it, `False` (stable, cacheable — safe because
    `facts.build_facts` already fingerprints diff identity, so a
    later-generated real diff still triggers re-triage). Live-validated
    against the real MP. The "notify service maintainer" path for exactly
    this kind of Launchpad-side problem was built 2026-07-07 as #48 (see
    item 28): a stuck diff now pings the operator Mattermost channel.
14. **Aggregated findings model: `closing`/`incomplete`/`question` (DONE,
    2026-07-06).** Designed in design_journal.md #31, implemented as #44. Today's dispatcher stops at the
    first check that fires, so an item with two simultaneous problems only
    ever surfaces one per bot run — slow when the next look (bot or human
    sponsor) can be a week away. Design: every check outcome gets a severity
    tier (`closing` short-circuits as today; `incomplete` now keeps
    evaluating so all simultaneous hard-requirement findings are collected;
    `question` is a new non-blocking, advisory tier that never changes
    status). One templated comment replaces per-check comments and the
    LLM phase's separate write — intro line once, then "Needs fixing before
    this can be sponsored" / "Nice to have (non-blocking)" sections, each
    omitted if empty; nothing posted at all if a run has zero findings.
    `llm_reviewer.triage_bug`/`triage_mp` will need to contribute findings
    into the same pool instead of writing their own comment. Interactive-mode
    UX (one prompt for the aggregate vs. per-finding) explicitly left open.
    **Addendum:** a pass with any inconclusive (`None`) result must not post
    the aggregated comment at all -- same "don't cache, retry next run"
    treatment `inconclusive` already gets for facts persistence, extended to
    gate the write itself, so a partial finding set is never posted as if it
    were a complete review.
    **Implemented 2026-07-06 (design_journal.md #44):** checks return
    `checks.Finding` objects, `main.py` collects them across the whole pass
    (LLM `INCOMPLETE` folds in), `checks.render_findings_comment` builds the
    one comment, one `Needs Fixing` vote (MPs only). Closing-tier and
    `pending` outcomes drop findings collected earlier (no nitpicking an
    already-landed change); an inconclusive pass posts nothing and skips
    the LLM phase. Interactive UX: one `[y/N]` for the whole aggregate.
    Live-validated on firmware-sof MP #504187 — a real two-finding case
    (conflicts + bad bug reference) that first-fire-wins had been surfacing
    one bot run at a time. Still open at the time, both since built:
    LLM-emitted `question`-tier findings (#47, item 4) and the
    already-uploaded comment → admin-notification switch (#48, item 28).
    This also unblocks item 17 (#35), whose prerequisite was this tier
    model.
15. **Per-check timing + `print()` → `logging` throughout (DONE).** See
    design_journal.md #32. `--verbose` now logs a `[timing]` line after each
    step in `triage_url` (`load_url`, `build_facts`, each of the 6 checks,
    the LLM phase) plus a `TOTAL` per URL, regardless of which return path
    fired. Live-validated against MP #507686 (xmltooling): 7.5s total,
    dominated by `check_stale_version` (3.67s) and
    `check_changelog_bug_reference` (1.41s) -- both do live external
    lookups; nothing pathological, just serialized network calls across 6
    checks. Also fixed, found while reading that capture: `print()` (stdout)
    and the `[verbose]` `logging` output (stderr) interleaved out of true
    execution order in piped/captured output. Every `print()` across
    `main.py`/`checks.py`/`llm_reviewer.py`/`launchpad_client.py`/
    `archive_lookup.py` (59 sites) now goes through `logging`
    (`logger.info`/`logger.warning`); default level is `INFO` (was
    verbose-only `WARNING`) so today's always-visible narration stays
    visible without `--verbose`. `--log`/`--logdir` considered and
    deliberately not added yet -- no concrete need until `--all` runs
    unattended via cron.
16. **Launchpad session timeout + fail-safe error handling (DONE).** See
    design_journal.md #33. Found live: a `--all --dry-run --verbose` run sat
    stuck for 1h33m (log silent the last 27+ min) on one item -- `LPClient`
    had no `timeout=` on `Launchpad.login_with(...)`, so a single stalled
    API call blocked the whole run forever. Fixed with `_LP_TIMEOUT_SECONDS
    = 30`; traced (not guessed) that this becomes a raw socket timeout with
    no retry logic anywhere in httplib2/lazr.restfulclient/launchpadlib, and
    confirmed live that it's a per-syscall bound, not a per-request
    deadline -- a slow trickle or a redirect hop can still add up to
    several multiples of 30s before failing (confirmed: 102s and 165s on
    two real items in the same clean run). Also added: a per-item catch-all
    in `main.triage_url` (several launchpadlib attribute reads have no
    try/except of their own, unlike `archive_lookup.py`'s lookups) so one
    item's failure logs and skips rather than aborting the rest of an
    `--all` run; and a separate try/except around `LPClient(...)`
    construction in `main()`, found necessary live (a forced-timeout test
    crashed there with a 60-line raw traceback, outside the per-item
    catch-all's coverage) -- now a clean one-line `logger.error` +
    `sys.exit(1)`. **Live-validated end to end:** a full clean `--all
    --dry-run --verbose` run against the real 90-item queue completed with
    exit code 0, zero unexpected errors, ~23.1 min total (median item
    8.84s; `check_stale_version` + `check_changelog_bug_reference` alone
    account for ~60% of total time). Backlog, deliberately not built:
    in-process retry (rejected -- today's failures are slow degrading
    struggles, not clean fast timeouts, so retrying immediately risks
    making a bad item worse; the next cron run is already a correct,
    better-spaced retry) and per-item memoization of the repeated diff-content
    fetch across checks 5/6 (real signature-change scope, deferred).
    **Outcome breakdown from that same clean 90-item run** (what the triage
    actually found, not just timing): 28 `check_stale_version` fires (23
    stale, 4 already-uploaded, 1 duplicate-version), 4 real merge conflicts
    (ROCm/HIP cluster), 2 admin-resolved bugs, zero wrong-target-branch and
    zero bad-changelog-reference bounces (good signal on #20/#25's
    precision, though one clean run isn't a trend). 56 items reached the LLM
    phase: 53 `READY_FOR_HUMAN`, 3 `INCOMPLETE`. One of the 2 `inconclusive`
    items (`google-osconfig-agent` MP #504883) is a live example of the
    #31-addendum scenario (an inconclusive check co-occurring with a fired
    one) -- harmless today, but concrete confirmation the addendum matters
    once #31 ships. Full breakdown in design_journal.md #33.

17. **Suppress bounces once a human is already engaged (DONE, 2026-07-06 --
    MP side; bug-side anchor still open).** Implemented per design #35 on
    top of #31's tier machinery, see design_journal.md #45.
    `checks.check_human_engaged` (MPs only): a comment by anyone other
    than the submitter and the bot itself, made since
    `preview_diff.date_created`, means a human review is in progress --
    `main.py` then silences the whole aggregated findings comment (no
    vote, no bounce; facts persist, status `READY_FOR_HUMAN`, quiet until
    a new push changes the fingerprint). Checked lazily, only when
    findings are about to post -- closing tiers returned earlier and are
    never suppressed; per the #35 amendment, suppression never skips
    evaluation. Unreadable comment history -> None -> post nothing,
    persist nothing, retry. Bugs always return False until the bug-side
    "since current submission" anchor question is decided.
    Live-validated: firmware-sof MP #504187 (the #31 two-finding
    showcase) is correctly suppressed -- juliank commented since the
    current diff; hiprand #507756 (no reviewer) still bounces.
    Original design notes below. Found by manually checking real items from a
    dry run: the bot has no concept today of "a human sponsor already
    commented here," so it can post a redundant/confusing bounce on an item
    someone's actively reviewing -- against the whole point of giving
    *early* feedback. Rule: skip posting a finding if anyone other than the
    submitter (and not the bot itself) has commented since the item's
    current diff was created -- not restricted to `~ubuntu-dev`, any human
    engagement counts. Critically, this only ever suppresses `incomplete`/
    `question`-tier output (per #31's taxonomy); `closing`-tier outcomes
    (`check_administrative_state`, `check_empty_diff`,
    `check_stale_version`'s `done`) always still fire regardless -- a
    sponsor commenting "looks good, uploading" must not permanently block
    the bot from later recognizing the archive actually got the matching
    upload. MP-side "since current diff" anchor
    (`preview_diff.date_created`) is decided; bug-side anchor is an open
    question (no diff/attachment-date equivalent readily available, see
    #30's lpcli attachment-visibility gap). Also surfaced a related backlog
    item: if a version+content match on an `ubuntu/*` MP doesn't get
    auto-closed by git-ubuntu within the existing 24h grace period, that's
    worth an admin notification — built 2026-07-07 as #48 (see item 28).

18. **Facts persist only when writes actually took effect (DONE, 2026-07-04).**
    See design_journal.md #36. Found in a design review: a `--dry-run` pass
    persisted facts for every fired check, so a later `--interactive`/`--yes`
    run skipped those items at the facts-unchanged gate and the bounces would
    never be posted. Confirmed live: ~86 items in `state.db` were cached that
    way from prior dry runs (repaired -- facts cleared for everything without
    a `performed` write in `audit.jsonl`). `LPClient` now tracks per-item
    write outcomes; `main` persists facts only when all attempted writes
    ended `performed`/`skipped-duplicate` (and the pass wasn't inconclusive,
    now enforced on the deterministic branches too, not just the LLM ones).
    A declined `[y/N]` re-prompts next run rather than dropping the item.

19. **Archive version in the facts fingerprint (DONE, 2026-07-04).** See
    design_journal.md #37. check_stale_version only ever worked on an item's
    first triage: archive movement (someone else's upload, or this change
    landing) never re-triggered the pipeline because facts were purely
    contributor-controlled. `build_facts` now records the package's current
    devel(-proposed) version for MPs (one getPublishedSources call per MP
    per run); an archive upload changes the fingerprint and re-triages
    immediately. Lookup failure -> `None` -> whole pass inconclusive (never
    persisted). Old snapshots migrate themselves via a one-time re-triage
    (dedup keeps it silent). Live-validated on hiprand #507756.

20. **Unreadable comment history skips the write, every mode (DONE,
    2026-07-04).** See design_journal.md #38. Dedup's history-read failing
    used to fall through to posting -- a transient glitch under --yes could
    double-post publicly. Now `_already_posted` returns None on failure and
    `comment()` skips with a `skipped-dedup-unavailable` audit outcome
    (non-effective per #36, so the item retries next run). seb128's call:
    infra glitch -> skip and retry, never prompt or guess.

21. **Design #35 amended: suppressed findings do not short-circuit.** The
    original write-up had suppressed incomplete-tier findings stopping the
    pipeline, which could skip the closing-capable checks (4/6) and miss an
    archive-matches-MP close on a human-engaged item -- the exact case the
    closing-tier exemption exists for. Now: suppression silences output,
    never skips evaluation. Implement #35 on top of #31's tiered dispatch,
    not before it.

22. **Per-item diff-content memoization (DONE, 2026-07-04).** See
    design_journal.md #39 (the #33 backlog item). Checks 2/5/6 now share one
    fetch of the preview diff content per item (one-entry cache keyed on the
    diff's `self_link`, failures cached too, reset per item and per test).
    Live-validated on grub2 #507575, which had been stuck inconclusive on
    the redundant second fetch timing out -- it now completes and persisted
    facts for the first time.

23. **"Already uploaded" comment links to the publication (DONE, 2026-07-05).**
    See design_journal.md #40. seb128 asked for the "already uploaded to the
    archive as `pkg version`" bounce (check 6, case 3a) to link to the
    Launchpad page for that publication. Turned out `SourcePackagePublishingHistory`
    has no `web_link` at all (confirmed live) -- fixed by constructing
    `archive_lookup.published_source_url(package, version)`
    (`https://launchpad.net/ubuntu/+source/<pkg>/<version>`), appended as a
    bare URL (Launchpad auto-linkifies these; Markdown link syntax would not
    render). Live-verified: 200 OK against the real ipu6-drivers publication.

24. **`check_stale_version` compares against the targeted series, not always
    devel (DONE, 2026-07-05).** See design_journal.md #41. Found live: a
    real SRU (MP #504085, gce-compute-image-packages, targeting noble) was
    wrongly bounced as stale because the check always compared against the
    current devel series (`stonking`) regardless of what the MP actually
    targets -- a known gap flagged in #27, hit live for the first time.
    New `checks._target_ubuntu_series` reads the series from
    `target_git_path` (`ubuntu/<series>-devel` for an SRU, falling back to
    devel for `ubuntu/devel`/Debian-merge targets); `_max_published_version`
    now checks every pocket (release/updates/security/proposed), not just
    a hardcoded devel/devel-proposed pair. `facts._archive_version` (#37)
    got the same fix for the same reason. Live-validated: the motivating MP
    now correctly resolves to `done` (already in `noble-proposed`) instead
    of a false "needs rebase" bounce.

25. **`--verbose` logs the LLM prompt/reply/token usage (DONE, 2026-07-05).**
    See design_journal.md #42. `_query_llm` now invokes `opencode run
    --format json` (an NDJSON event stream) instead of the default
    pretty-printed output -- the only format exposing per-call token
    usage/cost. `--verbose` now logs the full prompt sent, the raw reply,
    and a one-line token/cost summary (total/input/output/reasoning/
    cache read+write/cost). Fails safe to raw stdout if no usable event is
    found, same fail-safe convention as everywhere else in this bot.
    Live-validated against real `opencode`.

26. **`check_stale_version` checks history before bouncing an older
    proposal (DONE, 2026-07-05).** See design_journal.md #43. Found live:
    MP #505086 proposed a version older than the current archive max, but
    that exact version had itself been published and later superseded --
    this MP's change already landed, so "please rebase" was the wrong
    message. `archive_lookup.published_source` gained a `status=None` mode
    (search every status, not just Published); the cmp<0 branch now checks
    for a historical publication of the exact proposed version before
    bouncing, running the same content-comparison logic as the cmp==0 case
    (matching -> `done`, different -> `needs_fixing` as a version
    collision, not found -> unchanged "needs rebase"). Live-validated on
    the motivating MP: now correctly resolves to `done`.

27. **Nothing-to-sponsor check + LLM bullet wording (DONE, 2026-07-06).**
    See design_journal.md #46. Found by seb128 on the first interactive run
    of the new pipeline (bug #2139024, bounced over its SRU template
    despite having no patch and a linked MP already reviewed):
    `check_nothing_to_sponsor` (closing tier, bugs only, before the LLM)
    unsubscribes ~ubuntu-sponsors when the bug's fix is under review on a
    linked MP (sponsors-as-reviewer or an actual review vote -- the bug is
    a duplicate queue entry) or when there is no patch and no MP at all
    (sync requests exempt). Bug facts now fingerprint attachments and
    linked-MP review state so a closed no-patch bug re-triages once a fix
    appears. The SRU/sync LLM INCOMPLETE messages also lost their own
    greeting/sign-off (the aggregated template carries them once).

28. **Operator notifications via Mattermost webhook (DONE, 2026-07-07).**
    See design_journal.md #48 — the "notify a service maintainer" hook that
    items 13/14/17 (#30/#25/#43) each asked for. New `notify.py` posts to a
    Mattermost incoming webhook whose URL lives OUTSIDE the VCS in
    `~/.config/ubuntu-sponsoring-bot/config.ini` (`[notifications]
    webhook_url`; `SPONSORING_BOT_CONFIG` overrides the path); missing
    config = notifications disabled, everything else unaffected. Strictly
    an operator/admin channel for anomalies the bot can't act on — never a
    mirror of queue statuses. Round-one triggers: (a) preview diff missing
    past the generation grace period (fired once per MP from main's LLM
    phase, not from each of the 4 checks that notice it); (b)
    `check_stale_version`'s "done" case — git-ubuntu's importer failed to
    auto-close an already-uploaded MP; when a webhook is configured this
    replaces the "can be closed" MP comment entirely (#43's agreed switch),
    otherwise the comment is kept. Dry-run logs "would notify"; posting is
    best-effort (a failed POST is logged and lost, deliberately not wired
    into the #36 write-effectiveness retry). Dedup across runs comes free
    from the facts-unchanged gate. Live-validated 2026-07-07: the stuck-diff
    trigger on rust-cargo-c MP #507285 posted exactly one message to the
    real channel (trigger (b) unit-tested only, no live instance yet).

29. **Rich-history diagnosis for uploaded-not-autoclosed MPs (#49; A/B1/B2
    DONE 2026-07-08, round two parked).** When `check_stale_version` hits
    "already uploaded but the MP didn't autoclose", diagnose WHY the MP's
    git history was dropped and tell the right audience (MP comment =
    sponsor/uploader, channel = git-ubuntu maintainers; this deliberately
    amends #48's MP silence — that was right only while there was nothing
    actionable to say). Case A: the `.changes` (via `changesFileUrl()`)
    carries no Vcs-Git keys → sponsor-tooling comment + "rich history
    dropped" ping. Case B (keys present): hash equality vs the MP tip is
    the wrong test (sponsors legitimately stack a fixup and upload from
    their own repo) — `git_history.commit_contains()` checks *ancestry*
    via a sandboxed scratch fetch. B1 ancestor → pure importer bug: MP
    comment ("admins have been notified" only when a webhook exists) +
    ping. B2 not-an-ancestor → history diverged: comment (teach "base the
    upload on the contributor's commits") + ping. B3/unfetchable → the
    plain #48 ping. Round two (target-branch classification + a truncated
    diff comment) is PARKED, not backlog — build only if live data shows
    the finer split is needed (see design_journal.md #49's note).
    **First live signal, 2026-07-08 full-queue dry-run:** fsverity-utils
    MP #506172 hit the done-case; the `.changes` fetch genuinely timed out
    (`archive_lookup.changes_file_vcs_keys` → None), and it correctly fell
    through to the plain #48 ping rather than misdiagnosing. Still no live
    case with a successfully-fetched `.changes` (cases A/B1/B2 unexercised
    live).
30. **needs-packaging bugs: a PPA/git link is not "nothing to sponsor"
    (#50, DONE 2026-07-08).** Found in the same dry-run: bugs #2129955 and
    #2142921 were wrongly auto-closed by `check_nothing_to_sponsor`'s
    no_patch case despite a PPA/git-repo link named in a comment — normal
    for needs-packaging (task target exactly `ubuntu`, no package/series),
    since there's no existing branch to attach a patch/debdiff to.
    `_is_needs_packaging` + a regex scan (`_has_proposed_source_link`,
    description + all comments, best-effort — Launchpad PPA/git shapes
    plus github.com/salsa.debian.org) now skip the close (leave for a
    human) instead; scoped to needs-packaging only, so an ordinary bug
    with the same link still closes as before. Deliberately minimal first
    pass: no comment, no unsubscribe, no judgment on the link's quality —
    richer handling (a question-tier note, or LLM judgment of the link)
    is a natural round two once live data shows this doesn't over-trigger.
    **Re-verified live 2026-07-09:** re-ran both #2129955 and #2142921
    individually against the fixed code; both now log the skip and land
    READY_FOR_HUMAN. Confirmed fixed.
31. **Log readability: URL at conclusion + blank-line separator (#51,
    DONE 2026-07-09).** `triage_url`'s `finally` block now also logs
    `"--- Finished triage for: <url> ---"` plus a trailing blank line, so
    a long `--all` log doesn't require scrolling back up to find which
    URL a block of output belongs to, and items are visually separated.
32. **Skip the Feature-Freeze classification question pre-freeze (#52,
    DONE 2026-07-09).** `triage_mp` used to always ask the LLM to
    classify for FF purposes even though the resulting bullet only ever
    surfaces `is_after_feature_freeze()`-gated — wasted tokens/latency on
    every MP pre-freeze. The FF paragraph + `feature:` yaml field are now
    built empty when not past freeze; the diff/consistency review (needed
    regardless of freeze) is unaffected.
33. **MP review: "verify" vs "advisory" question-tier split + YAML-quoting
    fix (#53, DONE 2026-07-09).** Found live on rust-sudo-rs MP #508055:
    an unquoted observation containing "LP: #2156983" got misparsed by
    YAML (colon-space starts a mapping, `#` starts a comment), posting a
    garbage bullet (`{'The stanza claims LP': None}`). Fixed by having
    the model double-quote bullets and dropping (not stringifying) any
    non-string list item. Separately, `checks.Finding` gained a `kind`
    field (`advisory`/`verify`): "advisory" is for findings we're
    confident really don't block (vague stanza bullets); "verify" is for
    findings that WOULD block if true but the LLM's confidence isn't
    enough to vote on automatically (stanza/diff mismatches, the FFe
    bullet) — these now render in their own "Please verify" comment
    section instead of "Nice to have (non-blocking)". Live-verified same
    day against the trigger MP: two correctly-quoted mismatches, no
    garbage. 282 tests, lint clean.
34. **MP review question 2 scoped to "what changed", not "how it works"
    (#54, DONE 2026-07-09).** Found live on qca2 MP #507745: the LLM
    flagged a "mismatch" because a stanza naming a patch and its purpose
    didn't also describe an internal SSL v3 guard inside that same patch.
    Per seb128, a changelog describes the visible effect or the bug fixed,
    not every mechanism inside a patch. Question 2's prompt now only flags
    contradicted claims or whole unmentioned changes (a separate
    file/fix/patch); implementation completeness of an
    already-named-and-attributed patch is explicitly out of scope.
    Live-verified twice on the trigger MP: `verdict: pass`.
35. **SRU already in the upload queue → skip review, defer (#55, DONE
    2026-07-10).** Found live on libp11 MP #507660: an SRU already
    uploaded and waiting in noble's Unapproved queue (invisible to all
    publication-based lookups) got a full LLM review despite there being
    nothing left to sponsor. New `archive_lookup.upload_in_queue()`
    (Unapproved/New/Accepted, tri-state); `check_stale_version`'s
    previously-unconditional "clean" branch now returns `"queued"` →
    `PENDING_UPLOAD_QUEUE`: silent, no LLM, no facts persisted, re-triaged
    each run until publication flips it to the existing "done" close-out.
    284 tests. Backlog: SRU bug-template check, version-collision vs later
    series, skip the FF question for SRU targets.
36. **Queue hit compares changelog content before deferring (#56, DONE
    2026-07-10).** seb128's review of #55: SRU version increments are
    convention-fixed, so a same-version queue hit could be someone else's
    racing SRU — "rebase and bump", not "nothing to do". The queued
    upload's `.changes` `Changes:` field (publicly fetchable via
    `changes_file_url`, even Unapproved) is decoded and compared against
    the MP's stanza (trailer-insensitive, existing normalization): match →
    defer as before; differ → incomplete finding (rebase with a new
    version); unreadable → inconclusive/retry. 290 tests.
37. **Automated-review footnote on every comment (#57, DONE 2026-07-10).**
    `LPClient.comment()` appends a `FOOTNOTE` constant (signature
    separator + "This is an automated initial review of sponsoring
    requests. If this review seems wrong, please report it
    at https://bugs.launchpad.net/ubuntu-sponsoring") before the dedup
    check, so `_already_posted` compares the posted text. Pre-#57
    comments won't exact-match their footnoted regeneration — accepted
    one-time double-post risk; the facts gate remains the primary guard.
    291 tests. Not yet seen on a live write.

38. **SRU "fix newer series first" check (#58, DONE 2026-07-10).** New
    Check 7 `check_sru_newer_series`: for an SRU (MP targeting a stable
    series, or bug with an open series task), every newer supported
    series (devel included) must show the fix handled -- task Fix
    Released/Committed, a linked MP per series, or a series-named patch.
    Mechanically unhandled series get one LLM question ("does the bug
    text say it's fixed there?", series labeled with release versions
    since codenames postdate LLM training); yes -> soft "update the bug
    tasks" advisory, no -> full advisory citing the SRU requirements.
    Never blocks/votes; runs after the inconclusive gate to avoid
    wasted tokens. Live-probed on trigger MP #507660 (libp11/noble):
    mechanical layer right first try, prompt iterated twice for the
    codename-ordering gap. 307 tests.

39. **SRU checks backlog (discussed 2026-07-10, not built).** From the
    libp11 deep-dive (journal #55/#58): (a) SRU bug-template check --
    [Impact]/[Test Plan]/[Where problems could occur] present and
    substantive in the linked bug (a real "needs fixing", regex presence
    + possibly LLM substance -- note `review_sru_template` exists but is
    bug-side only); (b) version-collision check vs later series
    (`ubuntu1` where the SRU convention wants `ubuntu0.1`); (c) ~~skip
    the Feature-Freeze classification for SRU-targeted MPs~~ DONE, see
    item 40.

40. **FF classification skipped for SRU-targeted MPs (#59, DONE
    2026-07-11).** `triage_mp`'s `check_feature` gate is now
    `after_freeze AND not _targets_stable_series(mp)` -- FF is a
    devel-series concept, so an SRU MP no longer gets the classification
    question (or an FFe bullet) regardless of date. Fails toward
    skipping (advisory-only cost) when the devel codename can't be
    looked up. Not observable live until Feature Freeze; covered by
    tests. 309 tests.

41. **Direct source edits must be debian/patches patches (#60, DONE
    2026-07-11).** New deterministic Check 8 `check_direct_source_edit`
    (incomplete tier): files changed outside debian/ on a fix MP get a
    "provide this as a patch under debian/patches" bounce, citing
    https://ubuntu.com/project/docs/contributors/bug-fix/apply-the-fix/.
    Exempt (silently): merge MPs, new upstream versions (upstream
    component changed vs the changelog entry below), native packages
    (version without a Debian revision -- from the stanza, else from
    the archive when the MP has no stanza, the trigger nux #508190's
    shape). Proper nativeness classification (debian/source/format) is
    backlog. Live-verified on the trigger MP. 320 tests.

42. **Backlog: missing debian/changelog stanza on a fix MP.** The nux
    trigger MP changed no debian/ file at all -- no changelog entry, so
    the LLM content review skips and nothing flags the absence itself.
    A proper contribution needs a changelog entry; design as its own
    small check (seb128, 2026-07-11: "that's for tomorrow").

43. **Backlog: Check 8 for debdiff attachments on bugs (seb128,
    2026-07-11).** The direct-source-edit rule applies equally to a
    debdiff attached to a bug, but `check_direct_source_edit` is
    MP-only and nothing reads attachment *content* today (bug-side
    checks only look at titles/flags; launchpadlib can fetch the bytes
    via `attachment.data`, unlike lpcli -- see item 1's canonical/lpcli#23
    note). Needs: fetch the debdiff, parse changed paths, reuse the
    same exemptions (native, new upstream version).

## Live dry-run, 2026-07-08 (full queue, ~87 items, `--all --dry-run --verbose`)

No crashes, no unhandled exceptions (one grep false-positive: an LLM-quoted
kernel debugging session inside an MP diff contained the string
"Traceback"). 27 aggregated comments proposed, 0 votes cast (dry-run).
Notable:
- **#47's advisory tier fired live for the first time on two MPs**
  (rocFFT #507900, rocr-runtime #507908, both ROCm SRU backports with
  unresolved git conflict markers) — worth watching: in both cases the
  LLM's "nice to have" bullet mostly restates the same fact the
  deterministic conflict check already flagged as blocking. Not wrong,
  just redundant; if this pattern repeats, consider suppressing an
  advisory observation that only re-describes an already-collected
  `incomplete` finding.
- **#48/#49's first live signal** — see item 29 (fsverity-utils #506172).
- **#50's two motivating bugs** (#2129955, #2142921) both still hit the
  old wrong-close behavior in this run, since the fix landed mid-run —
  expected, and good corroboration the bug was real. Re-running just
  those two URLs (or the full queue again) would confirm the fix; not yet
  done.
- #50 re-verified live 2026-07-09 (see item 30). `--interactive` run
  started 2026-07-09 and is in progress/resuming.

## First `--interactive` run, 2026-07-09 (in progress)

- **Bot account permission gap found:** `~ubuntu-sponsoring-bot` posted a
  comment fine but got HTTP 401 "does not have permission to unsubscribe
  Ubuntu Sponsors" on bug #2143088 (the linked-MP-under-review path of
  `check_nothing_to_sponsor`). seb128 added the bot account to the
  `~ubuntu-sponsors` team as a fix; not yet re-verified live (watch the
  next unsubscribe attempt). See [[bot-account-switch]].
- Five live findings fed straight into fixes: log readability (#51),
  FF-classification token waste (#52), the YAML-quoting/verify-vs-advisory
  split (#53), the over-scoped stanza/diff consistency question (#54), and
  the SRU-already-in-upload-queue gap (#55) — see items 31–35 above.
- Next step: resume the `--interactive` run, watch for the unsubscribe
  permission fix taking effect, and watch for any further `question`-tier
  MP review output now that it's split into "Please verify"/"Nice to
  have".

## Known residual edges (documented in code)

- LLM-authored comments could be reworded on a from-scratch re-run and slip past
  exact-match dedup (deterministic bounce comments are safe).
- A `--url` run with no queue context lacks `source_package`, so the admin check
  falls back to "any Ubuntu task".
- `needs-packaging` bugs have task target `ubuntu` (not `pkg (Ubuntu)`); they
  fall through to a human, which is correct.
- LLM call latency ~12s; fine given most items are filtered out before the LLM.
