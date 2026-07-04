# Status & Handoff

_Last updated: 2026-07-04_

Snapshot of where the bot stands, how to run it, and what's next. Architectural
rationale lives in `design_journal.md`.

## What the bot does

Triages the Ubuntu sponsoring queue (bugs + merge proposals): deterministic
Python checks first, then an LLM phase (via `opencode`) for qualitative judgment
(SRU template completeness, sync delta explanation). The LLM only ever produces
a *recommendation*; the orchestrator performs writes, behind a confirmation gate.

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
2. **Bot account.** Currently authenticates as `~seb128`. Before go-live,
   authorize as `~ubuntu-sponsoring-bot` (confirm the account exists and has
   queue access) — the self-comment dedup keys on `self.lp.me`.
   **Switch-day caveat:** comments posted while running as `~seb128` become
   invisible to the new account's dedup, so previously-bounced items could be
   re-commented under the new identity. Cheap mitigation at switch time:
   clear `state.db` facts (forces re-triage, which is correct anyway) and
   accept the one-time duplicates, or manually skim the handful of items with
   `performed` comment writes in `audit.jsonl` first.
3. **First real `--interactive` run (DONE, 2026-07-04).** MP #507575
   (grub2, `~sharkcnnnnnn`): `check_stale_version` correctly found the
   proposed version already published with different content, prompted
   `[y/N]`, seb128 approved, comment + `vote="Needs Fixing"` posted for
   real -- confirmed both in `audit.jsonl` (`outcome: "performed"`) and
   directly on the MP on Launchpad. First real write this bot has ever
   made outside a smoke test.
4. **`triage_mp` is a stub** — implement MP LLM triage (e.g. DEP-3 headers).
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
    against the real MP. Backlog: a "notify service maintainer"
    (Matrix/Mattermost) path for exactly this kind of Launchpad-side
    problem — deliberately not built (needs a new notification backend),
    but this is the spot it would hook into.
14. **Aggregated findings model: `closing`/`incomplete`/`question` (DESIGNED,
    not built).** See design_journal.md #31. Today's dispatcher stops at the
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
    **Not implemented yet** — this is a refactor of every check's return
    contract and the `main.py` dispatcher; recorded as a design first.
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

17. **Suppress bounces once a human is already engaged (DESIGNED, not built).**
    See design_journal.md #35. Found by manually checking real items from a
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
    worth an admin Matrix/Mattermost notification (same not-yet-built hook
    as #30/#25).

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

## Known residual edges (documented in code)

- LLM-authored comments could be reworded on a from-scratch re-run and slip past
  exact-match dedup (deterministic bounce comments are safe).
- A `--url` run with no queue context lacks `source_package`, so the admin check
  falls back to "any Ubuntu task".
- `needs-packaging` bugs have task target `ubuntu` (not `pkg (Ubuntu)`); they
  fall through to a human, which is correct.
- LLM call latency ~12s; fine given most items are filtered out before the LLM.
