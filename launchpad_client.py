import logging
import os
import subprocess
import sys

from launchpadlib.launchpad import Launchpad

from audit import AuditLog

logger = logging.getLogger(__name__)

# Appended to every comment the bot posts (design #57): disclose that it's
# automated and give readers somewhere to report a bad review. "-- " is the
# conventional plain-text signature separator; Launchpad auto-links the
# bare URL (it does NOT render Markdown links).
FOOTNOTE = (
    "-- \n"
    "This is an automated initial review of sponsoring requests. If this "
    "review seems wrong, please report it at "
    "https://bugs.launchpad.net/ubuntu-sponsoring"
)


def _target(lp_obj):
    """A stable identifier for the object being written to, for the audit trail."""
    return (
        getattr(lp_obj, "self_link", None)
        or getattr(lp_obj, "web_link", None)
        or str(lp_obj)
    )


# Bug task statuses that already mean "no submitter action is awaited"; we never
# flip these to Incomplete.
_RESOLVED_STATUSES = (
    "Fix Released",
    "Fix Committed",
    "Won't Fix",
    "Invalid",
    "Incomplete",
)

# Truly conclusive statuses we never overwrite even when the archive shows a
# sync landed -- unlike _RESOLVED_STATUSES, this does NOT include Incomplete:
# a bug we previously bounced can still turn out to be already-synced (someone
# else uploaded it in the meantime), and should still be closed out.
_CONCLUSIVE_STATUSES = ("Fix Released", "Fix Committed", "Won't Fix", "Invalid")

# launchpadlib passes this straight through to httplib2 as a socket timeout.
# Left unset, a single stalled Launchpad API call blocks forever -- confirmed
# live (design_journal.md #33): a --all --dry-run --verbose run sat for over
# an hour with one idle ESTABLISHED connection to the API, no timeout to
# recover from it.
_LP_TIMEOUT_SECONDS = 30

# Privileged-helper delegation (#78): unsubscribing ~ubuntu-sponsors from a
# bug requires team membership, but the bot account must NOT be a member (a
# member's MP vote claims the team review slot and permanently drops the MP
# from the sponsoring report). When helper credentials are configured, the
# unsubscribe is delegated to privileged_helper.py, a subprocess with its own
# token from a member account; otherwise the bot acts directly (the
# pre-#78 behavior, kept for the transition while the bot is still a member).
_HELPER_SCRIPT = os.path.join(os.path.dirname(__file__), "privileged_helper.py")


def _helper_credentials():
    return os.environ.get(
        "SPONSORING_BOT_HELPER_LP_CREDENTIALS",
        os.path.join(
            os.path.expanduser("~"),
            ".cache",
            "ubuntu-sponsoring-bot-helper",
            "credentials",
        ),
    )


def _helper_configured():
    return os.path.exists(_helper_credentials())


# Write outcomes after which the item can be considered handled: the write
# either happened, or an identical bot comment already existed on Launchpad.
# Everything else (dry-run, declined at [y/N], no TTY, error) means the
# intended write did NOT take effect, so the item must be re-triaged next run
# rather than have its facts cached as if it had been dealt with.
_EFFECTIVE_WRITE_OUTCOMES = ("performed", "skipped-duplicate")


class LPClient:
    def __init__(
        self, app_name="ubuntu-sponsoring-bot", mode="dry-run", audit=None, lp=None
    ):
        self.mode = mode
        self.audit = audit if audit is not None else AuditLog()
        # Per-item write-outcome tracking, reset by start_item(); see
        # all_writes_effective() for why main.py cares.
        self.write_outcomes = []
        # ``lp`` can be injected for testing; otherwise authenticate for real.
        if lp is not None:
            self.lp = lp
            return

        # Persist the OAuth token in a credentials file (the team convention, as
        # in pmtriage / ubuntu-sponsoring). First use opens a browser to
        # authorise; subsequent runs are non-interactive -- which is what makes
        # unattended/cron operation possible without a desktop keyring. Paths are
        # env-overridable.
        creds_file = os.environ.get(
            "SPONSORING_BOT_LP_CREDENTIALS",
            os.path.join(os.path.expanduser("~"), ".cache", app_name, "credentials"),
        )
        cache_dir = os.environ.get(
            "SPONSORING_BOT_LP_CACHE",
            os.path.join(os.path.expanduser("~"), ".cache", app_name, "launchpadlib"),
        )
        os.makedirs(os.path.dirname(creds_file), exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)
        self.lp = Launchpad.login_with(
            application_name=app_name,
            service_root="production",
            credentials_file=creds_file,
            launchpadlib_dir=cache_dir,
            timeout=_LP_TIMEOUT_SECONDS,
            version="devel",
        )

    def start_item(self):
        """Reset per-item write tracking. Called by main at the start of each URL."""
        self.write_outcomes = []

    def all_writes_effective(self):
        """
        True when every write attempted since start_item() actually took effect
        (performed, or an identical comment already existed). main.py persists
        an item's facts snapshot only when this holds: a dry-run/declined/
        no-TTY/errored write means the item was NOT handled, and persisting
        facts would make the facts-unchanged gate skip it on the next (real)
        run -- the write would then never happen at all.
        """
        return all(o in _EFFECTIVE_WRITE_OUTCOMES for o in self.write_outcomes)

    def _record_write(self, **kwargs):
        self.write_outcomes.append(kwargs["outcome"])
        self.audit.record(**kwargs)

    def _decide(self, description):
        """
        Decide whether to perform a write, according to self.mode. Returns one of
        'perform', 'dry-run', 'declined', 'no-tty' -- the caller writes only on
        'perform', and the return value doubles as the audit outcome.

        - 'dry-run'     : never write; just log what would happen (default).
        - 'yes'         : write without prompting (unattended/cron use).
        - 'interactive' : prompt [y/N]; with no TTY (e.g. under cron) refuse to
                          write rather than crash on input()'s EOF.
        """
        logger.info("\n[ACTION] %s", description)

        if self.mode == "dry-run":
            logger.info(
                "  [dry-run] not performing this write. Re-run with --interactive or --yes to act."
            )
            return "dry-run"

        if self.mode == "yes":
            logger.info("  [--yes] performing write.")
            return "perform"

        # interactive
        if not sys.stdin.isatty():
            logger.info(
                "  [interactive] no TTY available; refusing to write. "
                "Use --yes for unattended runs or --dry-run to preview."
            )
            return "no-tty"
        ans = input("  Proceed? [y/N]: ")
        if ans.strip().lower() == "y":
            return "perform"
        logger.info("  Skipped (declined).")
        return "declined"

    def load_url(self, url):
        """Loads a Launchpad object (Bug, BugTask, or Merge Proposal) from a URL."""
        import re

        # Extract Bug ID
        match = re.search(r"(?:/bugs?/|/\+bug/)(\d+)", url)
        if match:
            bug_id = int(match.group(1))
            return self.lp.bugs[bug_id]

        # Transform MP URL to API URL
        if "/+merge/" in url:
            api_url = url.replace(
                "https://code.launchpad.net/", "https://api.launchpad.net/devel/"
            )
            return self.lp.load(api_url)

        return self.lp.load(url)

    def _already_posted(self, lp_obj, message, resource_type):
        """
        True if the bot account has already posted an identical comment here,
        False if it definitively hasn't, None if the history couldn't be read.

        Launchpad is the source of truth: we compare the comment author to the
        bot's own account (self.lp.me) rather than relying on a text marker or on
        local state, so this still works after state.db is lost or when running
        from another machine. Only an EXACT content match is suppressed -- a
        genuinely different message (a new bounce reason, a reopen after
        back-and-forth) still goes through.

        None follows the codebase-wide convention (see design_journal.md #28):
        an infra failure is not a basis for acting either way -- the caller
        skips the write and the item is retried next run (facts stay
        unpersisted per #36), rather than risking a double-post.
        """
        me = self.lp.me.self_link
        target = message.strip()
        try:
            if resource_type == "bug":
                for msg in lp_obj.messages:
                    if msg.owner_link == me and (msg.content or "").strip() == target:
                        return True
            elif resource_type == "branch_merge_proposal":
                for c in lp_obj.all_comments:
                    if c.author_link == me and (c.message_body or "").strip() == target:
                        return True
        except Exception as e:
            logger.warning(
                "  [dedup] could not read existing comments (%s); "
                "skipping this write, will retry next run.",
                e,
            )
            return None
        return False

    def unsubscribe_sponsors(self, lp_obj):
        """
        Unsubscribes ubuntu-sponsors from a bug.

        Bugs only, by design (design_journal.md #8): MPs drop off the sponsor
        queue via their status/vote transitions, and Launchpad offers no
        direct API to unsubscribe a review team from an MP anyway. Both call
        sites (check_administrative_state, the SYNCED path) are bug-only.

        The action needs ~ubuntu-sponsors membership, which the bot account
        must not have (#78: a member's MP vote claims the team review slot
        and permanently drops the MP from the report), so with helper
        credentials configured it is delegated to privileged_helper.py.
        """
        sponsors_team = self.lp.people["ubuntu-sponsors"]

        resource_type = lp_obj.resource_type_link.split("#")[-1]

        if resource_type == "bug_task":
            lp_obj = lp_obj.bug
            resource_type = "bug"
        if resource_type != "bug":
            logger.warning(
                "unsubscribe_sponsors called for a %s; only bugs are "
                "supported (design #8). Ignoring.",
                resource_type,
            )
            return

        target = _target(lp_obj)

        # Mid-run race belt (#76): the triage-level queue-membership guard
        # already filtered non-queue bugs, but a human may have unsubscribed
        # the team while we were triaging. Nothing to do then -- skip the
        # prompt entirely. On a lookup failure fall through to the normal
        # flow (worst case: a prompt/attempt for a no-op).
        try:
            if not any(
                s.person_link.rsplit("/", 1)[-1] == "~ubuntu-sponsors"
                for s in lp_obj.subscriptions
            ):
                logger.info(
                    "~ubuntu-sponsors is not subscribed to %s; nothing to "
                    "unsubscribe.",
                    target,
                )
                return
        except Exception:
            pass

        decision = self._decide(
            f"Unsubscribe ~ubuntu-sponsors from this {resource_type}."
        )
        if decision != "perform":
            self._record_write(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome=decision,
            )
            return

        try:
            if _helper_configured():
                # Delegated to the privileged helper (#78): a separate
                # process, separate token, separate account -- see the
                # module docstring in privileged_helper.py.
                result = subprocess.run(
                    [sys.executable, _HELPER_SCRIPT, "unsubscribe-sponsors",
                     str(lp_obj.id)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"privileged helper exited {result.returncode}: "
                        f"{result.stderr.strip()}"
                    )
                logger.info("privileged helper: %s", result.stdout.strip())
            else:
                lp_obj.unsubscribe(person=sponsors_team)
            self._record_write(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome="performed",
            )
            # Security updates queue via ~ubuntu-security-sponsors, which
            # the bot cannot unsubscribe yet (not a team member; backlog:
            # get the bot added once it has proven itself). Surface that
            # the bug therefore stays in the security queue -- best-effort,
            # purely informational.
            try:
                if any(
                    s.person_link.rsplit("/", 1)[-1] == "~ubuntu-security-sponsors"
                    for s in lp_obj.subscriptions
                ):
                    logger.info(
                        "~ubuntu-security-sponsors is also subscribed to %s; "
                        "the bot cannot unsubscribe that team yet, so the "
                        "bug stays in the security sponsoring queue.",
                        target,
                    )
            except Exception:
                pass
        except Exception as e:
            self._record_write(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome="error",
                detail=str(e),
            )
            logger.warning("Could not unsubscribe: %s", e)

    def set_bug_tasks_incomplete(self, lp_obj):
        """
        Set every open Ubuntu task on the bug to Incomplete -- the standard status
        for "waiting on the submitter". Tasks already resolved or already
        Incomplete are left untouched. Each transition is gated and audited.

        Returns ``{bug_target_name: 'Incomplete'}`` for the tasks actually
        changed, so the caller can fold those into its facts snapshot: the bot
        mutating status must not look like a contributor change next run (the
        same invariant that keeps an MP's queue_status out of facts -- see
        facts.py).
        """
        resource_type = lp_obj.resource_type_link.split("#")[-1]
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        target = _target(bug)
        changed = {}

        for task in bug.bug_tasks:
            name = task.bug_target_name or ""
            if "(Ubuntu" not in name:
                continue
            if task.status in _CONCLUSIVE_STATUSES:
                continue
            decision = self._decide(
                f"Set '{name}' status to Incomplete (was {task.status})."
            )
            if decision != "perform":
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome=decision,
                    detail="Incomplete",
                )
                continue
            try:
                task.transitionToStatus(status="Incomplete")
                changed[name] = "Incomplete"
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="performed",
                    detail="Incomplete",
                )
            except Exception as e:
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="error",
                    detail=f"Incomplete: {e}",
                )
                logger.warning("Could not set '%s' to Incomplete: %s", name, e)

        return changed

    def set_bug_tasks_new(self, lp_obj):
        """
        Flip the bug's Incomplete Ubuntu tasks back to New -- Rule B's
        "the contributor responded to the bounce" transition (design
        journal #66). Only tasks currently Incomplete are touched: the
        bot only undoes the state it (or a reviewer) set while waiting,
        never any other triage. Each transition is gated and audited.

        Returns ``{bug_target_name: 'New'}`` for the tasks actually
        changed (for folding into a facts snapshot, same invariant as
        set_bug_tasks_incomplete).
        """
        resource_type = lp_obj.resource_type_link.split("#")[-1]
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        target = _target(bug)
        changed = {}

        for task in bug.bug_tasks:
            name = task.bug_target_name or ""
            if "(Ubuntu" not in name:
                continue
            if task.status != "Incomplete":
                continue
            decision = self._decide(f"Set '{name}' status back to New.")
            if decision != "perform":
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome=decision,
                    detail="New",
                )
                continue
            try:
                task.transitionToStatus(status="New")
                changed[name] = "New"
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="performed",
                    detail="New",
                )
            except Exception as e:
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="error",
                    detail=f"New: {e}",
                )
                logger.warning("Could not set '%s' back to New: %s", name, e)

        return changed

    def _devel_series_display_name(self):
        """The current Ubuntu devel series' display name (e.g. 'Noble'), as it
        appears in a bug task's target ('pkg (Ubuntu Noble)'). None if it
        can't be determined -- callers should fail safe rather than guess."""
        try:
            return self.lp.distributions["ubuntu"].current_series.name.capitalize()
        except Exception as e:
            logger.warning("could not determine the Ubuntu devel series: %s", e)
            return None

    def set_bug_tasks_fix_released(self, lp_obj):
        """
        Set the Ubuntu task(s) matching a sync-request bug to Fix Released --
        used when the archive check shows the requested version already landed.

        Syncs only ever target $devel, so "matching" is the untargeted task
        ('pkg (Ubuntu)') or one a human nominated to the current devel series
        ('pkg (Ubuntu <Devel>)'); SRU series tasks are left alone. Conclusive
        statuses are skipped, but -- unlike set_bug_tasks_incomplete --
        Incomplete is NOT skipped: a previously-bounced bug can still turn out
        to already be synced by someone else.

        Returns ``{bug_target_name: 'Fix Released'}`` for tasks actually
        changed, for folding into the facts snapshot (see facts.py).
        """
        resource_type = lp_obj.resource_type_link.split("#")[-1]
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        target = _target(bug)
        changed = {}

        devel_series = self._devel_series_display_name()
        matching_suffixes = {"(Ubuntu)"}
        if devel_series:
            matching_suffixes.add(f"(Ubuntu {devel_series})")
        else:
            logger.warning(
                "only matching the untargeted Ubuntu task (devel series unknown)."
            )

        for task in bug.bug_tasks:
            name = task.bug_target_name or ""
            if not any(name.endswith(suffix) for suffix in matching_suffixes):
                continue
            if task.status in _CONCLUSIVE_STATUSES:
                continue
            decision = self._decide(
                f"Set '{name}' status to Fix Released (was {task.status})."
            )
            if decision != "perform":
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome=decision,
                    detail="Fix Released",
                )
                continue
            try:
                task.transitionToStatus(status="Fix Released")
                changed[name] = "Fix Released"
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="performed",
                    detail="Fix Released",
                )
            except Exception as e:
                self._record_write(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="error",
                    detail=f"Fix Released: {e}",
                )
                logger.warning("Could not set '%s' to Fix Released: %s", name, e)

        return changed

    def comment(self, lp_obj, message, vote=None):
        """
        Adds a comment to a bug or MP.

        ``vote`` (MPs only) attaches a code-review vote to the comment --
        one of Launchpad's fixed set: 'Approve', 'Needs Fixing', 'Needs
        Information', 'Abstain', 'Disapprove', 'Needs Resubmitting'. This is
        how the bot flags an MP for attention now: git-ubuntu MPs don't
        accept direct ``queue_status`` writes (setStatus fails/has no effect
        -- a known git-ubuntu limitation, confirmed by seb128 2026-07-03), so
        a vote-carrying comment is the only way left to signal "needs
        fixing" to a human reviewer. Ignored for bugs (no vote concept
        there).

        Every comment gets FOOTNOTE appended (design #57): readers learn
        it's automated and where to complain. Appended here, in the one
        place all comments flow through, so the dedup check compares the
        same final text that gets posted.
        """
        message = f"{message}\n\n{FOOTNOTE}"
        resource_type = lp_obj.resource_type_link.split("#")[-1]

        if resource_type == "bug_task":
            lp_obj = lp_obj.bug
            resource_type = "bug"

        target = _target(lp_obj)
        detail = f"[vote={vote}] {message}" if vote else message

        already = self._already_posted(lp_obj, message, resource_type)
        if already is None:
            # Comment history unreadable (infra glitch): never write on an
            # incomplete picture. Not an effective outcome, so facts stay
            # unpersisted and the write is retried next run.
            self._record_write(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="skipped-dedup-unavailable",
                detail=detail,
            )
            return
        if already:
            logger.info(
                "  [dedup] identical bot comment already present on Launchpad; skipping."
            )
            self._record_write(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="skipped-duplicate",
                detail=detail,
            )
            return

        vote_suffix = f" (vote={vote})" if vote else ""
        decision = self._decide(f"Post this comment{vote_suffix}:\n---\n{message}\n---")
        if decision != "perform":
            self._record_write(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome=decision,
                detail=detail,
            )
            return

        try:
            if resource_type == "bug":
                lp_obj.newMessage(content=message)
            elif resource_type == "branch_merge_proposal":
                if vote:
                    lp_obj.createComment(content=message, vote=vote)
                else:
                    lp_obj.createComment(content=message)
            self._record_write(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="performed",
                detail=detail,
            )
        except Exception as e:
            self._record_write(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="error",
                detail=str(e),
            )
            logger.warning("Could not post comment: %s", e)
