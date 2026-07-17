"""
Rule B: the stale-bounce sweep (design_journal.md #66, STATUS.md item 1).

When the bot bounces a bug (aggregated feedback + tasks set Incomplete),
the ball is in the contributor's court. This pass revisits every bug the
bot is still waiting on (state.db status WAITING_ON_CONTRIBUTOR) and,
based on activity since the tasks went Incomplete:

- a new *usable diff* attachment -> the contributor responded with a new
  patch/debdiff: flip the Incomplete Ubuntu tasks back to New so the bug
  re-enters the sponsoring workflow (the next queue pass re-triages the
  new attachment on its merits -- checks 8/10/#63/#65 -- so no content
  judgment is duplicated here). Non-diff attachments (logs, screenshots)
  don't count as addressing a patch bounce and fall through to the
  comment logic.
- new non-bot comment(s) -> ask the LLM whether the response addresses
  the stored bounce feedback: yes -> flip to New; no -> wait for the
  timer; LLM failure or no stored bounce_reason (bugs bounced before the
  column existed) -> timer only.
- nothing for STALE_BOUNCE_AGE -> sweep: final comment + unsubscribe
  ~ubuntu-sponsors, state -> DONE.

The reference time is Launchpad's own `bug_task.date_incomplete` --
authoritative, and it survives a lost/rebuilt state.db.
"""

import datetime
import logging

import attachments
import checks

logger = logging.getLogger(__name__)

STALE_BOUNCE_AGE = datetime.timedelta(days=30)

SWEEP_COMMENT = (
    "There hasn't been any update following the review feedback in over a "
    "month, so we are cleaning up the sponsoring queue by unsubscribing "
    "~ubuntu-sponsors. If you get back to working on the fix, please "
    "address the feedback and subscribe ~ubuntu-sponsors again to get "
    "back in the review queue."
)


def sweep_bounced_bugs(state_manager, lp_client, llm_reviewer):
    """Run the sweep over every bounced bug. Per-bug failures are logged
    and skipped -- one broken bug must not kill the whole pass (same
    stance as main's per-item catch-all)."""
    bounced = state_manager.bounced_bugs()
    logger.info("Sweep: %d bounced bug(s) to revisit.", len(bounced))
    for url, bounce_reason in bounced:
        try:
            _sweep_one(url, bounce_reason, state_manager, lp_client, llm_reviewer)
        except Exception:
            logger.exception("Sweep: unexpected error on %s; skipping.", url)


def _advance_state_if_writes_landed(url, state_manager, lp_client, status, details):
    """Advance a swept bug's stored state only when every write this item
    actually took effect (external review finding, 2026-07-17; the #36
    pattern applied to the sweep). In dry-run, on a declined [y/N], or on
    an errored write, nothing happened on Launchpad -- advancing the state
    anyway would drop the bug from bounced_bugs() forever and the sweep
    action would silently never happen. Leaving WAITING_ON_CONTRIBUTOR
    retries next run; comment dedup (#38) keeps the retry from
    double-posting."""
    if lp_client.all_writes_effective():
        state_manager.update_status(url, status, details)
        return
    logger.info(
        "[%s] a sweep write was not performed (dry-run/declined/no TTY/"
        "error). Leaving WAITING_ON_CONTRIBUTOR; retrying next sweep.",
        url,
    )


def _sweep_one(url, bounce_reason, state_manager, lp_client, llm_reviewer):
    # Fresh per-item write-outcome tracking: without this the sweep would
    # inherit the last queue item's outcomes -- a stale failure could
    # wrongly block a state advance here, and (worse, pre-fix) a clean
    # slate was never guaranteed for the gating below.
    lp_client.start_item()
    # #100: each swept bug is its own item for the LLM call budget.
    llm_reviewer.start_item(url)
    lp_obj = lp_client.load_url(url)
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    bug = lp_obj.bug if resource_type == "bug_task" else lp_obj

    # Private items are never processed (#103) -- the response judgment
    # would ship the bug's comments to the LLM provider. Same guard as
    # main's; the bug stays in bounced_bugs() and sweeps normally if it
    # ever becomes public again.
    if getattr(bug, "private", False):
        logger.info(
            "Sweep [%s]: bug is private -- leaving for a human, content "
            "never sent to the LLM.",
            url,
        )
        return

    incomplete_since = _incomplete_since(bug)
    if incomplete_since is None:
        # No Ubuntu task is Incomplete anymore -- someone else moved the
        # bug on. Nothing for the sweep to do; normal queue triage owns it
        # again (and will overwrite this state entry on its next pass).
        logger.debug("Sweep [%s]: no longer Incomplete; skipping.", url)
        return

    attachments.reset_cache()
    if _new_usable_diff_since(bug, incomplete_since):
        logger.info(
            "[%s] new patch/debdiff attached since the bounce. Setting the "
            "tasks back to New for re-review.",
            url,
        )
        lp_client.set_bug_tasks_new(bug)
        _advance_state_if_writes_landed(
            url,
            state_manager,
            lp_client,
            "READY_FOR_HUMAN",
            "New attachment since the bounce; tasks set back to New.",
        )
        return

    comments = _comments_since(bug, incomplete_since)
    if comments:
        if bounce_reason:
            addressed = llm_reviewer.review_bounce_response(bounce_reason, comments)
            if addressed is None:
                logger.info(
                    "[%s] couldn't judge the contributor's response (LLM "
                    "failure); leaving as-is, retrying next sweep.",
                    url,
                )
                return
            if addressed:
                logger.info(
                    "[%s] the response addresses the bounce feedback. "
                    "Setting the tasks back to New for re-review.",
                    url,
                )
                lp_client.set_bug_tasks_new(bug)
                _advance_state_if_writes_landed(
                    url,
                    state_manager,
                    lp_client,
                    "READY_FOR_HUMAN",
                    "Response addresses the bounce; tasks set back to New.",
                )
                return
            logger.debug(
                "Sweep [%s]: response doesn't address the feedback; timer "
                "logic applies.",
                url,
            )
        else:
            logger.debug(
                "Sweep [%s]: comments since the bounce but no stored "
                "bounce_reason (pre-#66 bounce); timer logic applies.",
                url,
            )

    age = datetime.datetime.now(datetime.timezone.utc) - incomplete_since
    if age < STALE_BOUNCE_AGE:
        logger.debug(
            "Sweep [%s]: bounced %s ago (< %s); leaving for now.",
            url,
            age,
            STALE_BOUNCE_AGE,
        )
        return

    logger.info(
        "[%s] no response to the bounce in %s. Sweeping: final comment + "
        "unsubscribing ~ubuntu-sponsors.",
        url,
        age,
    )
    lp_client.comment(bug, SWEEP_COMMENT)
    lp_client.unsubscribe_sponsors(bug)
    _advance_state_if_writes_landed(
        url,
        state_manager,
        lp_client,
        "DONE",
        "Swept: no response to the review feedback in over a month; "
        "~ubuntu-sponsors unsubscribed.",
    )


def _incomplete_since(bug):
    """The newest `date_incomplete` across the bug's currently-Incomplete
    Ubuntu tasks, or None when no Ubuntu task is Incomplete (nothing to
    sweep). A task Incomplete but missing its date (shouldn't happen --
    Launchpad maintains the field) is ignored rather than guessed at."""
    dates = [
        task.date_incomplete
        for task in bug.bug_tasks
        if "(Ubuntu" in (task.bug_target_name or "")
        and task.status == "Incomplete"
        and task.date_incomplete is not None
    ]
    return max(dates) if dates else None


def _new_usable_diff_since(bug, since):
    """True if a patch-shaped attachment newer than `since` has usable
    diff content. Attachments carry no date of their own; their upload
    message's date_created is the timestamp (verified live, #66)."""
    for attachment in reversed(attachments.patch_attachments(bug)):
        date = getattr(getattr(attachment, "message", None), "date_created", None)
        if date is None or date <= since:
            continue
        text = attachments.attachment_text(attachment)
        if isinstance(text, str):
            return True
    return False


def _comments_since(bug, since):
    """Non-service-account comment texts newer than `since`, oldest first."""
    texts = []
    for message in bug.messages:
        date = getattr(message, "date_created", None)
        if date is None or date <= since:
            continue
        owner = getattr(message, "owner_link", "") or ""
        if owner.rsplit("/", 1)[-1] in checks.SERVICE_ACCOUNTS:
            continue
        content = getattr(message, "content", "") or ""
        if content.strip():
            texts.append(content)
    return texts
