import os
import sys

from launchpadlib.launchpad import Launchpad

from audit import AuditLog


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


class LPClient:
    def __init__(
        self, app_name="ubuntu-sponsoring-bot", mode="dry-run", audit=None, lp=None
    ):
        self.mode = mode
        self.audit = audit if audit is not None else AuditLog()
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
            version="devel",
        )

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
        print(f"\n[ACTION] {description}")

        if self.mode == "dry-run":
            print(
                "  [dry-run] not performing this write. Re-run with --interactive or --yes to act."
            )
            return "dry-run"

        if self.mode == "yes":
            print("  [--yes] performing write.")
            return "perform"

        # interactive
        if not sys.stdin.isatty():
            print(
                "  [interactive] no TTY available; refusing to write. "
                "Use --yes for unattended runs or --dry-run to preview."
            )
            return "no-tty"
        ans = input("  Proceed? [y/N]: ")
        if ans.strip().lower() == "y":
            return "perform"
        print("  Skipped (declined).")
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
        True if the bot account has already posted an identical comment here.

        Launchpad is the source of truth: we compare the comment author to the
        bot's own account (self.lp.me) rather than relying on a text marker or on
        local state, so this still works after state.db is lost or when running
        from another machine. Only an EXACT content match is suppressed -- a
        genuinely different message (a new bounce reason, a reopen after
        back-and-forth) still goes through.
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
            # If we can't read history, don't crash and don't silently skip --
            # fall through to the normal mode gate (dry-run/[y/N]/--yes).
            print(
                f"  [dedup] could not read existing comments ({e}); proceeding to confirm."
            )
        return False

    def unsubscribe_sponsors(self, lp_obj):
        """
        Unsubscribes ubuntu-sponsors from a bug or MP.
        """
        sponsors_team = self.lp.people["ubuntu-sponsors"]

        resource_type = lp_obj.resource_type_link.split("#")[-1]

        if resource_type == "bug_task":
            lp_obj = lp_obj.bug
            resource_type = "bug"

        target = _target(lp_obj)
        decision = self._decide(
            f"Unsubscribe ~ubuntu-sponsors from this {resource_type}."
        )
        if decision != "perform":
            self.audit.record(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome=decision,
            )
            return

        try:
            if resource_type == "bug":
                lp_obj.unsubscribe(person=sponsors_team)
            elif resource_type == "branch_merge_proposal":
                # For MPs, we can remove the reviewer
                try:
                    lp_obj.unsubscribe(person=sponsors_team)
                except AttributeError:
                    print("Note: Direct unsubscribe method not found on MP object.")
            self.audit.record(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome="performed",
            )
        except Exception as e:
            self.audit.record(
                url=target,
                action="unsubscribe",
                target=target,
                mode=self.mode,
                outcome="error",
                detail=str(e),
            )
            print(f"Could not unsubscribe: {e}")

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
                self.audit.record(
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
                self.audit.record(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="performed",
                    detail="Incomplete",
                )
            except Exception as e:
                self.audit.record(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="error",
                    detail=f"Incomplete: {e}",
                )
                print(f"Could not set '{name}' to Incomplete: {e}")

        return changed

    def _devel_series_display_name(self):
        """The current Ubuntu devel series' display name (e.g. 'Noble'), as it
        appears in a bug task's target ('pkg (Ubuntu Noble)'). None if it
        can't be determined -- callers should fail safe rather than guess."""
        try:
            return self.lp.distributions["ubuntu"].current_series.name.capitalize()
        except Exception as e:
            print(f"WARNING: could not determine the Ubuntu devel series: {e}")
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
            print(
                "WARNING: only matching the untargeted Ubuntu task "
                "(devel series unknown)."
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
                self.audit.record(
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
                self.audit.record(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="performed",
                    detail="Fix Released",
                )
            except Exception as e:
                self.audit.record(
                    url=target,
                    action="set_status",
                    target=name,
                    mode=self.mode,
                    outcome="error",
                    detail=f"Fix Released: {e}",
                )
                print(f"Could not set '{name}' to Fix Released: {e}")

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
        """
        resource_type = lp_obj.resource_type_link.split("#")[-1]

        if resource_type == "bug_task":
            lp_obj = lp_obj.bug
            resource_type = "bug"

        target = _target(lp_obj)
        detail = f"[vote={vote}] {message}" if vote else message

        if self._already_posted(lp_obj, message, resource_type):
            print(
                "  [dedup] identical bot comment already present on Launchpad; skipping."
            )
            self.audit.record(
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
            self.audit.record(
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
            self.audit.record(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="performed",
                detail=detail,
            )
        except Exception as e:
            self.audit.record(
                url=target,
                action="comment",
                target=target,
                mode=self.mode,
                outcome="error",
                detail=str(e),
            )
            print(f"Could not post comment: {e}")
