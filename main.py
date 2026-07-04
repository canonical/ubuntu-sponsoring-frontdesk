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
    # across all 6 checks, so the LLM-phase terminal branches below know not
    # to persist facts in that case -- persisting would make the top-level
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

    # Check 2: Target Branch
    fired = checks.check_target_branch(url, lp_obj, lp_client)
    checkpoint("check_target_branch")
    logger.debug("check_target_branch -> %s", fired)
    if fired is None:
        inconclusive = True
    elif fired:
        state_manager.update_status(
            url,
            "WAITING_ON_CONTRIBUTOR",
            "Bounced: wrong target branch.",
            facts=persistable_facts(),
        )
        return

    # Check 3: MP Conflicts
    fired = checks.check_mp_conflicts(url, lp_obj, lp_client)
    checkpoint("check_mp_conflicts")
    logger.debug("check_mp_conflicts -> %s", fired)
    if fired is None:
        inconclusive = True
    elif fired:
        state_manager.update_status(
            url,
            "WAITING_ON_CONTRIBUTOR",
            "Bounced: merge conflicts.",
            facts=persistable_facts(),
        )
        return

    # Check 4: MP Empty Diff
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

    # Check 5: changelog LP bug reference sanity
    fired = checks.check_changelog_bug_reference(url, lp_obj, lp_client)
    checkpoint("check_changelog_bug_reference")
    logger.debug("check_changelog_bug_reference -> %s", fired)
    if fired is None:
        inconclusive = True
    elif fired:
        state_manager.update_status(
            url,
            "WAITING_ON_CONTRIBUTOR",
            "Bounced: changelog cites a bug not reported against this package.",
            facts=persistable_facts(),
        )
        return

    # Check 6: proposed version vs. archive (stale / already-uploaded).
    # Unlike the other checks, a fired result here maps to three different
    # outcomes, so it returns "needs_fixing"/"done"/"pending" rather than a bool.
    outcome = checks.check_stale_version(url, lp_obj, lp_client)
    checkpoint("check_stale_version")
    logger.debug("check_stale_version -> %s", outcome)
    if outcome is None:
        inconclusive = True
    elif outcome == "needs_fixing":
        state_manager.update_status(
            url,
            "WAITING_ON_CONTRIBUTOR",
            "Bounced: proposed version is stale or a duplicate of an existing upload.",
            facts=persistable_facts(),
        )
        return
    elif outcome == "done":
        state_manager.update_status(
            url,
            "DONE",
            "Closed: proposed version already uploaded to the archive.",
            facts=persistable_facts(),
        )
        return
    elif outcome == "pending":
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

    if inconclusive:
        logger.info(
            "One or more checks couldn't be fully evaluated (a lookup/fetch "
            "failure). Facts won't be persisted, so this URL is retried next run."
        )

    logger.info("Passed deterministic MVP checks. Moving to LLM review...")

    # LLM Phase
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
    elif new_status == "INCOMPLETE":
        logger.info(
            "LLM determined request is INCOMPLETE. Commenting and setting Incomplete."
        )
        lp_client.comment(lp_obj, comment)
        # Mark the bug Incomplete (the status for "waiting on the submitter").
        # We deliberately keep ~ubuntu-sponsors subscribed for visibility. Fold
        # our own status writes into the persisted facts so the bot's action is
        # not mistaken for a contributor change on the next run (Fix #2).
        changed = lp_client.set_bug_tasks_incomplete(lp_obj)
        new_facts = facts.apply_task_status_changes(new_facts, changed)
        state_manager.update_status(
            url,
            "WAITING_ON_CONTRIBUTOR",
            "Bounced: LLM rejected (INCOMPLETE); set Incomplete.",
            facts=persistable_facts(),
        )
    elif new_status == "READY_FOR_HUMAN":
        logger.info("LLM checks passed (or none applicable). Marking ready for human.")
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
