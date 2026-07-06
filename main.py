import argparse
import logging
import shutil
import sys
import time
import urllib.request
import json
from state import StateManager
from launchpad_client import LPClient
from audit import AuditLog
import checks
import facts
from llm_reviewer import LLMReviewer

logger = logging.getLogger(__name__)


def triage_url(url, state_manager, lp_client, llm_reviewer, force=False, item=None):
    # Verbose timing: log how long each step takes, and the total for the URL
    # regardless of which return path was taken, so a slow --all --dry-run
    # scan can be attributed to a specific check/lookup instead of guessed at.
    t_start = time.monotonic()
    try:
        return _triage_url(
            url, state_manager, lp_client, llm_reviewer, force, item, t_start
        )
    except Exception:
        # Several launchpadlib attribute reads in facts.build_facts and the
        # checks (queue_status, bug_tasks, target_git_path, ...) have no
        # try/except of their own -- unlike the lookups in archive_lookup.py,
        # which already fail safe internally. Without this, a single stalled
        # or failed Launchpad call (now bounded by LPClient's timeout, see
        # design_journal.md #33) would propagate out of process_queue's loop
        # and abort an --all run, silently skipping every later item. Nothing
        # gets persisted here, so this URL is retried from scratch next run,
        # same as an unhandled load_url failure already was.
        logger.exception("Unexpected error triaging %s; skipping for this run.", url)
        return None
    finally:
        logger.debug("[timing] TOTAL for %s: %.2fs", url, time.monotonic() - t_start)


def _triage_url(url, state_manager, lp_client, llm_reviewer, force, item, t_start):
    logger.info("--- Starting triage for: %s ---", url)

    t_last = [t_start]

    def checkpoint(label):
        now = time.monotonic()
        logger.debug(
            "[timing] %s: %.2fs (total %.2fs)", label, now - t_last[0], now - t_start
        )
        t_last[0] = now

    # The queue entry tells us which package the request is about, which lets the
    # admin check look at the right Ubuntu series tasks. Absent for --url runs.
    source_package = (item or {}).get("source_package")

    try:
        lp_obj = lp_client.load_url(url)
    except Exception as e:
        logger.warning("Failed to load URL from Launchpad: %s", e)
        return
    checkpoint("load_url")

    # Fingerprint the contributor-controlled signals. We (re-)triage only when
    # these change; an unchanged snapshot means nothing has happened since we
    # last looked, so we stay quiet and avoid re-posting the same comment.
    new_facts = facts.build_facts(lp_obj, lp=getattr(lp_client, "lp", None))
    checkpoint("build_facts")
    if not force:
        stored_facts = state_manager.get_facts(url)
        checkpoint("get_facts")
        if stored_facts is not None and stored_facts == new_facts:
            logger.info("Facts unchanged since last triage. Skipping (nothing to do).")
            return

    # New, changed, or forced: run the full pipeline from scratch. Every
    # terminal path persists new_facts so the next run can detect "no change" --
    # EXCEPT when a check couldn't fully determine an answer (a lookup/fetch
    # failure, not a genuine "nothing to flag"). Checks signal that by
    # returning None instead of False; `inconclusive` tracks whether any did,
    # across all 6 checks. An inconclusive pass posts nothing and persists
    # nothing (design #31's addendum: the aggregated comment must not claim
    # completeness it doesn't have) -- persisting would make the top-level
    # facts-unchanged gate skip this URL forever, and whatever the check
    # couldn't determine this run would never get re-checked.
    inconclusive = False

    # The archive-version part of the fingerprint (design_journal.md #37)
    # follows the same None-means-lookup-failed convention as the checks: a
    # failed lookup must not be persisted (two consecutive failures would
    # silently compare equal at the gate), so the whole pass is inconclusive.
    if new_facts.get("archive_version", "") is None:
        inconclusive = True
        logger.info(
            "Archive version lookup failed while fingerprinting; "
            "treating this pass as inconclusive."
        )

    # Facts must also not be persisted when an intended write didn't actually
    # take effect (dry-run, declined at [y/N], no TTY, or an error). Otherwise
    # a --dry-run pass would cache every bounce-worthy item as handled, and a
    # later --interactive/--yes run would skip them all at the facts-unchanged
    # gate -- the writes would silently never happen. LPClient tracks each
    # write's outcome per item; a declined write is retried (re-prompted) on
    # the next run, same as a transient failure.
    lp_client.start_item()
    # Fresh item, fresh diff-content memo (design_journal.md #39) -- checks
    # 2/5/6 share one fetch of the same preview diff within this item.
    checks.reset_diff_lines_cache()

    def persistable_facts():
        if inconclusive or not lp_client.all_writes_effective():
            if not lp_client.all_writes_effective():
                logger.info(
                    "A write this run was not performed (dry-run/declined/"
                    "no TTY/error). Facts not persisted; this URL will be "
                    "re-triaged next run."
                )
            return None
        return new_facts

    # Check 1: Administrative
    fired = checks.check_administrative_state(
        url, lp_obj, lp_client, source_package=source_package
    )
    checkpoint("check_administrative_state")
    logger.debug("check_administrative_state -> %s", fired)
    if fired is None:
        inconclusive = True
    elif fired:
        state_manager.update_status(
            url,
            "DONE",
            "Unsubscribed as already fixed/merged.",
            facts=persistable_facts(),
        )
        return

    # Check 1b: anything to sponsor at all? (bugs only, closing tier) --
    # a bug whose fix is under review on a linked MP is a duplicate queue
    # entry; a bug with no patch and no MP has nothing to review yet.
    outcome = checks.check_nothing_to_sponsor(url, lp_obj, lp_client)
    checkpoint("check_nothing_to_sponsor")
    logger.debug("check_nothing_to_sponsor -> %s", outcome)
    if outcome is None:
        inconclusive = True
    elif outcome:
        detail = (
            "Unsubscribed: fix under review on the linked merge proposal."
            if outcome == "mp_review"
            else "Unsubscribed: no patch or merge proposal to sponsor yet."
        )
        state_manager.update_status(url, "DONE", detail, facts=persistable_facts())
        return

    # Checks 2-6 no longer stop the pipeline on first fire (design_journal.md
    # #31): incomplete-tier findings are collected across the whole pass and
    # posted as ONE aggregated comment at the end, so a contributor learns
    # about every simultaneous problem in the same round instead of one per
    # bot run. Closing-tier outcomes (check 1 above, check 4, check 6's
    # "done"/"pending") still short-circuit -- the item is already resolved,
    # so any findings collected so far are deliberately dropped: no point
    # nitpicking a change that already landed (seb128, 2026-07-06).
    findings = []

    # Check 2: Target Branch (incomplete tier)
    result = checks.check_target_branch(url, lp_obj, lp_client)
    checkpoint("check_target_branch")
    logger.debug("check_target_branch -> %s", result)
    if result is None:
        inconclusive = True
    elif result:
        findings.append(result)

    # Check 3: MP Conflicts (incomplete tier)
    result = checks.check_mp_conflicts(url, lp_obj, lp_client)
    checkpoint("check_mp_conflicts")
    logger.debug("check_mp_conflicts -> %s", result)
    if result is None:
        inconclusive = True
    elif result:
        findings.append(result)

    # Check 4: MP Empty Diff (closing tier -- short-circuits)
    fired = checks.check_empty_diff(url, lp_obj, lp_client)
    checkpoint("check_empty_diff")
    logger.debug("check_empty_diff -> %s", fired)
    if fired is None:
        inconclusive = True
    elif fired:
        state_manager.update_status(
            url,
            "DONE",
            "Closed: empty diff (already landed).",
            facts=persistable_facts(),
        )
        return

    # Check 5: changelog LP bug reference sanity (incomplete tier)
    result = checks.check_changelog_bug_reference(url, lp_obj, lp_client)
    checkpoint("check_changelog_bug_reference")
    logger.debug("check_changelog_bug_reference -> %s", result)
    if result is None:
        inconclusive = True
    elif result:
        findings.append(result)

    # Check 6: proposed version vs. archive (stale / already-uploaded).
    # Mixed tiers: returns a Finding (incomplete -- stale/duplicate version),
    # "done"/"pending" (closing -- already landed), False, or None.
    outcome = checks.check_stale_version(url, lp_obj, lp_client)
    checkpoint("check_stale_version")
    logger.debug("check_stale_version -> %s", outcome)
    if outcome is None:
        inconclusive = True
    elif outcome == "done":
        state_manager.update_status(
            url,
            "DONE",
            "Closed: proposed version already uploaded to the archive.",
            facts=persistable_facts(),
        )
        return
    elif outcome == "pending":
        # The change is (almost certainly) already in the archive, just too
        # recently for git-ubuntu's importer to have auto-closed the MP yet.
        # Everything stays quiet -- including any findings collected above:
        # bouncing a contributor over details of a change that already
        # landed is exactly the noise the closing tier exists to avoid.
        # Deliberately no facts= here: nothing about the MP itself changes
        # while we wait out the grace period, so persisting new_facts would
        # make the top-level facts-unchanged gate skip this URL forever and
        # the deferred comment would never get posted once the window
        # passes. Leaving the stored facts snapshot untouched (or unset)
        # means the next run re-triages this URL regardless.
        state_manager.update_status(
            url,
            "PENDING_ARCHIVE_IMPORT",
            "Version matches the archive but was published recently; "
            "deferring in case git-ubuntu's importer auto-closes this MP first.",
        )
        return
    elif outcome:
        findings.append(outcome)

    if inconclusive:
        # Design #31's addendum: the aggregated comment presents itself as
        # the complete list of what to fix this round, so posting it while
        # any check couldn't determine its result would claim a completeness
        # it doesn't have. Post nothing, persist nothing, retry next run.
        # (The LLM phase is skipped too -- its finding would be gated the
        # same way, so running it would only spend tokens on a pass that
        # can't act.)
        logger.info(
            "One or more checks couldn't be fully evaluated (a lookup/fetch "
            "failure). Skipping the LLM phase and posting nothing this run -- "
            "the aggregated review must not claim to be complete when it "
            "isn't. Facts won't be persisted, so this URL is retried next run."
        )
        return

    logger.info("Deterministic checks evaluated. Moving to LLM review...")

    # LLM Phase: folds into the same findings pool (design #31) -- SYNCED
    # stays closing-tier with its own terse path, INCOMPLETE becomes one
    # incomplete-tier finding in the aggregate.
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type in ("bug", "bug_task"):
        # Pass the bug object to the LLM reviewer
        if resource_type == "bug_task":
            lp_obj = lp_obj.bug

        new_status, comment = llm_reviewer.triage_bug(lp_obj)
    elif resource_type == "branch_merge_proposal":
        new_status, comment = llm_reviewer.triage_mp(lp_obj)
    else:
        new_status, comment = "READY_FOR_HUMAN", "Unknown resource type for LLM"
    checkpoint("llm_reviewer")

    llm_incomplete = new_status == "INCOMPLETE"
    if llm_incomplete:
        findings.append(checks.Finding("incomplete", comment))

    if new_status == "SYNCED":
        logger.info(
            "Archive check found this already synced. Commenting, closing, and unsubscribing."
        )
        lp_client.comment(lp_obj, comment)
        changed = lp_client.set_bug_tasks_fix_released(lp_obj)
        new_facts = facts.apply_task_status_changes(new_facts, changed)
        lp_client.unsubscribe_sponsors(lp_obj)
        state_manager.update_status(
            url,
            "DONE",
            "Closed: already synced (archive check).",
            facts=persistable_facts(),
        )
        return

    if findings:
        # Design #35: once a human reviewer is engaged (someone other than
        # the submitter commented since the current diff), suppress the
        # findings entirely -- the bot's job is early feedback BEFORE a
        # sponsor spends time here, not talking over an ongoing review.
        # Checked lazily, only when there is something to suppress: closing
        # tiers already returned above and are never suppressed. Suppression
        # silenced output but never skipped evaluation (the #35 amendment) --
        # all checks and the LLM already ran by this point.
        engaged = checks.check_human_engaged(lp_obj, lp_client)
        checkpoint("check_human_engaged")
        logger.debug("check_human_engaged -> %s", engaged)
        if engaged is None:
            logger.info(
                "Couldn't read the comment history to tell whether a human "
                "reviewer is engaged. Posting nothing this run; facts won't "
                "be persisted, so this URL is retried next run."
            )
            return
        if engaged:
            # A determined, stable state -- persist facts like a clean pass,
            # so the facts-unchanged gate skips this URL until a genuinely
            # new push (new diff) changes the fingerprint and real checking
            # resumes.
            logger.info(
                "A human reviewer already commented on the current revision; "
                "suppressing %d finding(s) and leaving the review to them.",
                len(findings),
            )
            state_manager.update_status(
                url,
                "READY_FOR_HUMAN",
                f"{len(findings)} finding(s) suppressed: "
                "a human reviewer is already engaged.",
                facts=persistable_facts(),
            )
            return
        aggregated = checks.render_findings_comment(findings)
        blocking = [f for f in findings if f.tier == "incomplete"]
        logger.info(
            "Posting the aggregated review comment (%d finding(s), %d blocking).",
            len(findings),
            len(blocking),
        )
        # A review vote only exists on MPs; bug findings (the LLM's) post as
        # a plain comment, as the INCOMPLETE path always did.
        vote = (
            "Needs Fixing"
            if blocking and resource_type == "branch_merge_proposal"
            else None
        )
        lp_client.comment(lp_obj, aggregated, vote=vote)
        if llm_incomplete:
            # Mark the bug Incomplete (the status for "waiting on the
            # submitter"). We deliberately keep ~ubuntu-sponsors subscribed
            # for visibility. Fold our own status writes into the persisted
            # facts so the bot's action is not mistaken for a contributor
            # change on the next run (Fix #2).
            changed = lp_client.set_bug_tasks_incomplete(lp_obj)
            new_facts = facts.apply_task_status_changes(new_facts, changed)
        if blocking:
            state_manager.update_status(
                url,
                "WAITING_ON_CONTRIBUTOR",
                f"Bounced: {len(blocking)} finding(s) need contributor action.",
                facts=persistable_facts(),
            )
            return
        # question-tier only: advisory, doesn't block a human review.

    logger.info("No blocking findings. Marking ready for human.")
    state_manager.update_status(
        url,
        "READY_FOR_HUMAN",
        "Ready for human review.",
        facts=persistable_facts(),
    )


def process_queue(state_manager, lp_client, llm_reviewer, force=False):
    logger.info("Fetching sponsoring queue JSON...")
    url = "https://sponsoring-reports.ubuntu.com/jsons/sponsoring.json"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())

    logger.info("Found %d items in the queue.", len(data))
    for item in data:
        link = item.get("link")
        if link:
            triage_url(
                link, state_manager, lp_client, llm_reviewer, force=force, item=item
            )


def main():
    parser = argparse.ArgumentParser(description="Ubuntu Sponsoring Bot (Local MVP)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="The Launchpad Bug or MP URL to triage")
    group.add_argument(
        "--all", action="store_true", help="Process the entire sponsoring queue"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-triage even if facts are unchanged",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every decision step considered by each check, not just the ones that fired",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended writes without performing them (default)",
    )
    mode_group.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt [y/N] before each write (requires a TTY)",
    )
    mode_group.add_argument(
        "--yes",
        action="store_true",
        help="Perform writes without prompting (for unattended/cron use)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s" if not args.verbose else "[verbose] %(name)s: %(message)s",
    )

    if args.yes:
        mode = "yes"
    elif args.interactive:
        mode = "interactive"
    else:
        mode = "dry-run"

    if shutil.which("opencode") is None:
        logger.warning(
            "'opencode' CLI not found on PATH. LLM review is DISABLED -- "
            "every item that reaches the LLM phase (SRU template checks, sync "
            "justification checks) will fail safe to READY_FOR_HUMAN instead of "
            "getting a real qualitative review. Install/configure opencode to "
            "restore it; until then the bot is running in degraded (checks-only) mode."
        )

    state_manager = StateManager()
    audit = AuditLog()

    logger.info(
        "Authenticating to Launchpad... (write mode: %s, audit: %s)", mode, audit.path
    )
    try:
        lp_client = LPClient(mode=mode, audit=audit)
    except Exception as e:
        # Unlike a per-item failure inside triage_url (caught there, see
        # design_journal.md #33), there is no session to fall back to here --
        # nothing this run can do without one. Log clearly and exit non-zero
        # rather than letting a raw traceback fall out (confirmed live: a
        # forced 1s timeout crashed here with a 60-line TimeoutError
        # traceback, not the clean [timing]/logging output the rest of a
        # run now produces).
        logger.error("Failed to authenticate to Launchpad: %s", e)
        sys.exit(1)
    llm_reviewer = LLMReviewer(lp=lp_client.lp)

    if args.url:
        triage_url(args.url, state_manager, lp_client, llm_reviewer, force=args.force)
    elif args.all:
        process_queue(state_manager, lp_client, llm_reviewer, force=args.force)


if __name__ == "__main__":
    main()
