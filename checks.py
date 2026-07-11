import datetime
import logging
import re
from typing import NamedTuple

import archive_lookup
import attachments
import git_history
import llm_reviewer
import notify

logger = logging.getLogger(__name__)


class Finding(NamedTuple):
    """
    One reviewable point a check found (design_journal.md #31). Checks in the
    `incomplete`/`question` tiers no longer post their own comment; they
    return a Finding and main.py aggregates every fired Finding from the
    whole pass into one templated comment (see render_findings_comment).
    `closing`-tier outcomes (check_administrative_state, check_empty_diff,
    check_stale_version's "done") keep their own terse comment and
    short-circuit as before -- they mean "already resolved", not "feedback".

    tier: "incomplete" -- a hard requirement, the contributor must act
          before this can be sponsored; drives a Needs Fixing vote.
          "question" -- advisory, never blocks and never votes. Reserved
          for future soft findings; nothing produces it yet.

    kind: only meaningful within tier "question" (seb128, 2026-07-09):
          "advisory" -- genuinely optional, address whenever or never.
          "verify" -- if true this would actually be a problem, but the
          check isn't confident enough (an LLM judgment) to vote/block
          on it -- ask the sponsor/contributor to double-check instead.
          Ignored by "incomplete"/"closing" findings.
    """

    tier: str
    message: str
    kind: str = "advisory"


def render_findings_comment(findings):
    """
    Render the one aggregated comment for a pass's fired findings
    (design_journal.md #31): a single intro, a "needs fixing" section, an
    optional "nice to have" section, and a single closing line -- instead
    of one comment per check. Individual finding messages are bullets and
    deliberately carry no greeting/sign-off of their own.
    """
    incomplete = [f for f in findings if f.tier == "incomplete"]
    verify = [f for f in findings if f.tier == "question" and f.kind == "verify"]
    advisory = [f for f in findings if f.tier == "question" and f.kind == "advisory"]

    def bullets(items):
        # Continuation lines are indented so a multi-line message stays
        # visually attached to its bullet in Launchpad's plain-text renderer.
        return "\n".join("* " + f.message.replace("\n", "\n  ") for f in items)

    parts = [
        "Thanks for your contribution! The automated review spotted the "
        "following points:"
    ]
    if incomplete:
        parts.append(
            "Needs fixing before this can be sponsored:\n\n" + bullets(incomplete)
        )
    if verify:
        parts.append(
            "Please verify (not confirmed -- if any of these are real they'd "
            "need fixing, but the automated review isn't confident enough to "
            "block on them):\n\n" + bullets(verify)
        )
    if advisory:
        parts.append(
            "Nice to have (non-blocking -- none of these block the upload, "
            "but you may want to address them now, before a sponsor reviews "
            "this, or in a future contribution):\n\n" + bullets(advisory)
        )
    if incomplete:
        parts.append("Once the points above are addressed, please let us know!")
    return "\n\n".join(parts)


# Service accounts whose comments must not count as "a human reviewer is
# engaged" (design_journal.md #45 follow-up). Checked live (2026-07-06, all
# 52 tracked MPs): NO service account has ever commented on a sponsoring MP
# -- git-ubuntu closes MPs via a status change, not a comment -- so today
# this list is future-proofing, not a fix. ~ubuntu-sponsoring-bot is the
# entry that matters: once the bot moves off seb128's personal account, its
# pre-switch comments won't match lp.me anymore. ~janitor comments on BUGS
# ("This bug was fixed in the package ..."), so it becomes load-bearing when
# the bug side of #35 ships. Maintained by editing this constant.
SERVICE_ACCOUNTS = frozenset(
    {
        "~ubuntu-sponsoring-bot",
        "~git-ubuntu-bot",
        "~git-ubuntu-import",
        "~janitor",
    }
)


def check_human_engaged(lp_obj, lp_client):
    """
    True if a human reviewer is already engaged on this item, i.e. someone
    other than the submitter (and other than the bot itself) commented since
    the current diff was pushed (design_journal.md #35). main.py uses this to
    suppress the aggregated incomplete/question findings: the bot exists for
    early feedback *before* a sponsor spends time on an item, so once one is
    actively reviewing, a bot bounce is redundant at best and confusing at
    worst. Only suppresses findings-tier output -- closing-tier outcomes
    (already resolved) are decided before this is ever consulted.

    Deliberately counts a comment from ANY non-submitter account, not just
    ~ubuntu-dev members: non-core contributors leave real review feedback
    too. Known service accounts (SERVICE_ACCOUNTS above) are excluded so an
    automated comment can never silence the bot.

    A comment only counts if made after preview_diff.date_created (the same
    anchor as #30's grace period): a fresh push generates a new diff, so a
    stale comment on an old revision can't suppress the bot forever on a new,
    unreviewed push. If the diff carries no timestamp, any qualifying comment
    counts (erring toward staying quiet).

    MPs only for now: bugs have no diff/push anchor and the right "since
    current submission" frame is an open #35 question, so bugs always return
    False (never suppressed) until that's decided.

    Returns True/False, or None if the comment history couldn't be read
    (per the codebase-wide convention, #28: don't act either way on an
    infra failure; the caller posts nothing and retries next run).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type != "branch_merge_proposal":
        return False

    lp = getattr(lp_client, "lp", None)
    me = getattr(getattr(lp, "me", None), "self_link", None)
    try:
        submitter = lp_obj.registrant_link
        diff = lp_obj.preview_diff
        anchor = getattr(diff, "date_created", None) if diff is not None else None
        for c in lp_obj.all_comments:
            if c.author_link in (me, submitter):
                continue
            if c.author_link.rsplit("/", 1)[-1] in SERVICE_ACCOUNTS:
                logger.debug(
                    "  [engaged] ignoring comment by %s: known service account.",
                    c.author_link,
                )
                continue
            if anchor is not None and getattr(c, "date_created", None) is not None:
                if c.date_created <= anchor:
                    logger.debug(
                        "  [engaged] ignoring comment by %s: predates the "
                        "current diff (%s <= %s).",
                        c.author_link,
                        c.date_created,
                        anchor,
                    )
                    continue
            logger.info(
                "  [engaged] human reviewer already engaged: comment by %s "
                "since the current diff.",
                c.author_link,
            )
            return True
    except Exception as e:
        logger.warning(
            "  [engaged] could not read the comment history (%s); "
            "cannot tell whether a human is engaged.",
            e,
        )
        return None
    return False


# A task that has landed in Ubuntu.
DONE_STATUSES = ("Fix Released", "Fix Committed")
# Statuses that mean "this series no longer needs sponsor action": either it
# landed, or it was never targeted (untargeted series are set Won't Fix/Invalid).
CLOSED_STATUSES = DONE_STATUSES + ("Won't Fix", "Invalid")


def _relevant_ubuntu_tasks(bug, source_package):
    """
    Return the bug's Ubuntu tasks for the package this sponsorship is about.

    Upstream and Debian tasks never count -- an upstream 'Fix Released' says
    nothing about whether the Ubuntu upload happened. We match 'pkg (Ubuntu)'
    (devel) and 'pkg (Ubuntu <Series>)' (SRU). When the package is unknown
    (e.g. a single --url run with no queue context) we fall back to every
    '(Ubuntu' task on the bug.
    """
    tasks = []
    for task in bug.bug_tasks:
        name = task.bug_target_name or ""
        if source_package:
            if name.startswith(f"{source_package} (Ubuntu"):
                tasks.append(task)
        elif "(Ubuntu" in name:
            tasks.append(task)
    return tasks


def check_administrative_state(url, lp_obj, lp_client, source_package=None):
    """
    Check if the request is already fixed/uploaded for Ubuntu.
    Returns True if we handled it and unsubscribed, False otherwise.
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]

    if resource_type == "branch_merge_proposal":
        # MPs use queue_status; they drop off the sponsor queue automatically
        # when merged/rejected.
        logger.debug(
            "check_administrative_state: MP queue_status=%r", lp_obj.queue_status
        )
        if lp_obj.queue_status in ("Merged", "Rejected"):
            logger.info("[%s] is %s. No action needed.", url, lp_obj.queue_status)
            return True
        return False

    # Bug or bug task: evaluate across all relevant Ubuntu series tasks. The
    # request is complete only when none of them is still open -- one series
    # landing must not drop the whole bug off the queue.
    bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
    tasks = _relevant_ubuntu_tasks(bug, source_package)
    if not tasks:
        # Can't identify the Ubuntu task(s); leave it for a human.
        logger.debug(
            "check_administrative_state: no relevant Ubuntu task(s) for "
            "source_package=%r; skipping (left for a human).",
            source_package,
        )
        return False

    all_closed = all(task.status in CLOSED_STATUSES for task in tasks)
    any_landed = any(task.status in DONE_STATUSES for task in tasks)
    logger.debug(
        "check_administrative_state: tasks=%s all_closed=%s any_landed=%s",
        {t.bug_target_name: t.status for t in tasks},
        all_closed,
        any_landed,
    )
    if all_closed and any_landed:
        summary = ", ".join(
            sorted(f"{task.bug_target_name}: {task.status}" for task in tasks)
        )
        logger.info(
            "[%s] all relevant Ubuntu tasks resolved (%s). Unsubscribing ~ubuntu-sponsors.",
            url,
            summary,
        )
        lp_client.comment(
            bug,
            f"This request appears complete for Ubuntu ({summary}). "
            f"Cleaning up the queue by unsubscribing ~ubuntu-sponsors.",
        )
        lp_client.unsubscribe_sponsors(bug)
        return True

    return False


# Attachment filenames that read as a proposed fix even when the submitter
# forgot to tick Launchpad's "patch" flag. Lives in attachments.py (#62)
# so the content-fetching foundation and this metadata check agree.
_PATCH_FILENAME_RE = attachments.PATCH_FILENAME_RE

# Linked MPs in these states are no longer a review venue.
_INACTIVE_MP_STATUSES = ("Rejected", "Superseded")

# A plausible link to a proposed package build/source, for needs-packaging
# bugs (design_journal.md #50). A brand-new package has no existing branch
# to attach a patch/debdiff to, so pointing at a PPA (the build) and/or a
# git repo (the source) is the normal, expected shape of a contribution
# there -- unlike an ordinary bug, where the same link is unusual enough
# that guessing at it risks more false "yes, reviewable" positives than it's
# worth. Deliberately broad (github.com/salsa.debian.org too, not just
# Launchpad): the goal is "don't wrongly close", not "verify the link is
# good" -- a human still has to judge it.
_PROPOSED_SOURCE_LINK_RE = re.compile(
    r"launchpad\.net/~[\w.+-]+/\+archive"  # PPA
    r"|launchpad\.net/~[\w.+-]+/\+git"  # LP git repo, non-code. subdomain form
    r"|code\.launchpad\.net/~[\w.+-]+/\+git"  # LP git repo, code. subdomain form
    r"|github\.com/[\w.-]+/[\w.-]+"
    r"|salsa\.debian\.org/[\w.-]+/[\w.-]+",
    re.IGNORECASE,
)


def _is_needs_packaging(bug):
    """True if any of the bug's tasks targets plain 'ubuntu' (no package,
    no series) -- Launchpad's shape for 'this package doesn't exist in
    Ubuntu yet' requests (STATUS.md's known residual edge). There is, by
    definition, no existing packaging branch to diff against, so having no
    patch/debdiff attached is normal here, unlike an ordinary bug."""
    return any(
        (task.bug_target_name or "").strip().lower() == "ubuntu"
        for task in bug.bug_tasks
    )


def _has_proposed_source_link(bug):
    """Whether the bug's description or any comment mentions what looks
    like a PPA or git repository -- the usual way a needs-packaging
    contributor points at their proposed work when there's no packaging
    branch to attach a patch to. Best-effort text scan, not a fetch/build
    of anything: false negatives (an unusual hosting choice) just mean the
    bug is closed as before; false positives (a link that doesn't pan out)
    just mean a human looks and closes it anyway -- errs toward the
    cautious side per seb128's live examples (bug #2129955: PPA + git repo
    named in comment #1, wrongly auto-closed as 'no_patch'; #2142921
    similarly)."""
    texts = [getattr(bug, "description", "") or ""]
    try:
        for message in bug.messages:
            texts.append(getattr(message, "content", "") or "")
    except Exception as e:
        logger.debug(
            "_has_proposed_source_link: could not read bug comments (%s); "
            "scanning the description only.",
            e,
        )
    return any(_PROPOSED_SOURCE_LINK_RE.search(text) for text in texts)


def check_nothing_to_sponsor(url, lp_obj, lp_client):
    """
    Closing-tier check, bugs only: is there actually anything here for the
    sponsors team to review?

    Two cases (seb128, 2026-07-06, found live on bug #2139024):

    - "mp_review": the bug has a linked merge proposal that either names
      ~ubuntu-sponsors as a requested reviewer or has already received a
      review vote. The MP is the better review venue and (in the reviewer
      case) already its own sponsoring-queue entry -- the bug is a duplicate,
      so unsubscribe ~ubuntu-sponsors from it and let the review continue on
      the MP. A linked MP with neither signal proves nothing (it may not be
      in the queue at all), so it is left for a human.

    - "no_patch": no linked MP and no patch attached (Launchpad's patch flag,
      or a *.debdiff/*.diff/*.patch filename) -- nothing to sponsor yet, so
      say so, unsubscribe ~ubuntu-sponsors, and invite re-subscribing once a
      fix is proposed. Sync requests are exempt: they legitimately carry no
      patch (the LLM phase reviews those).

    Returns "mp_review"/"no_patch" (handled: commented + unsubscribed),
    False (a fix is present, or not a bug, or nothing conclusive), or None
    (a Launchpad lookup failed; retry next run).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type not in ("bug", "bug_task"):
        return False
    bug = lp_obj.bug if resource_type == "bug_task" else lp_obj

    try:
        active_mps = [
            mp
            for mp in bug.linked_merge_proposals
            if mp.queue_status not in _INACTIVE_MP_STATUSES
        ]
        for mp in active_mps:
            sponsors_requested = False
            reviewed = False
            for vote in mp.votes:
                if vote.reviewer_link.rsplit("/", 1)[-1] == "~ubuntu-sponsors":
                    sponsors_requested = True
                if vote.comment_link is not None:
                    reviewed = True
            logger.debug(
                "check_nothing_to_sponsor: linked MP %s sponsors_requested=%s "
                "reviewed=%s",
                mp.web_link,
                sponsors_requested,
                reviewed,
            )
            if sponsors_requested or reviewed:
                logger.info(
                    "[%s] fix is under review on linked MP %s. Unsubscribing "
                    "~ubuntu-sponsors from the bug.",
                    url,
                    mp.web_link,
                )
                lp_client.comment(
                    bug,
                    f"The fix proposed here is being reviewed on {mp.web_link}, "
                    "so there is no need for a separate sponsoring-queue entry "
                    "for this bug. Cleaning up the queue by unsubscribing "
                    "~ubuntu-sponsors; the review continues on the merge "
                    "proposal.",
                )
                lp_client.unsubscribe_sponsors(bug)
                return "mp_review"
        if active_mps:
            # An MP exists but shows no review signal yet; can't tell which
            # entry the queue should keep. Leave it for a human.
            return False

        # Sync requests legitimately have no patch to attach.
        if llm_reviewer._is_sync(
            getattr(bug, "title", ""), getattr(bug, "description", "")
        ):
            logger.debug("check_nothing_to_sponsor: sync request; no patch expected.")
            return False

        for attachment in bug.attachments:
            if attachment.type == "Patch" or _PATCH_FILENAME_RE.search(
                attachment.title or ""
            ):
                logger.debug(
                    "check_nothing_to_sponsor: found patch attachment %r.",
                    attachment.title,
                )
                return False

        # needs-packaging bugs have no existing branch to attach a
        # patch/debdiff to, so a PPA/git-repo link in the description or
        # comments is the normal shape of a contribution there (unlike an
        # ordinary bug). Cautious by design (#50): a link just means
        # "leave it for a human", not "treat it as a real patch" -- no
        # comment, no unsubscribe, no judgment on whether the link is any
        # good.
        if _is_needs_packaging(bug) and _has_proposed_source_link(bug):
            logger.info(
                "[%s] needs-packaging bug references a possible PPA/git "
                "repo; leaving for a human instead of closing as no_patch.",
                url,
            )
            return False
    except Exception as e:
        logger.warning(
            "check_nothing_to_sponsor: could not read the bug's linked MPs/"
            "attachments (%s); skipping, will retry next run.",
            e,
        )
        return None

    logger.info(
        "[%s] no patch attached and no linked merge proposal: nothing to "
        "sponsor yet. Unsubscribing ~ubuntu-sponsors.",
        url,
    )
    lp_client.comment(
        bug,
        "There doesn't seem to be a patch or merge proposal attached to this "
        "bug yet, so there is nothing for the sponsors team to review at this "
        "point. Cleaning up the queue by unsubscribing ~ubuntu-sponsors -- "
        "please subscribe them again once a proposed fix is available.",
    )
    lp_client.unsubscribe_sponsors(bug)
    return "no_patch"


_MISSING_DIFF_GRACE = datetime.timedelta(hours=1)


def _diff_missing_is_still_generating(lp_obj):
    """`lp_obj.preview_diff` was fetched cleanly and is None -- Launchpad
    hasn't (yet, or ever) computed a diff for this MP. To a human this shows
    as a yellow "Launchpad is generating the diff" box on the MP page, and it
    normally clears within minutes of the MP being opened (or a new commit
    being pushed). Distinguishes that normal, brief window from an MP that's
    been sitting without a diff far longer than diff generation should ever
    take -- which live data shows really happens (MP #503471, missing since
    creation ~3 months prior) and looks like a stuck/failed Launchpad job
    Launchpad itself never retried.

    Returns True (caller should return None -- retriable, still expected to
    resolve) when the MP's `date_created` is under `_MISSING_DIFF_GRACE` old,
    or when the age can't be determined at all (fails safe to the more
    conservative "keep waiting" assumption). Returns False once the grace
    period has elapsed -- callers should treat the missing diff as a stable
    "no diff data available" fact (as with an MP whose diff has no
    debian/changelog section) rather than retrying forever: without this,
    an MP stuck like #503471 would stay "inconclusive" on every single run
    indefinitely, never settling into the facts cache (see design_journal.md
    #28) and re-invoking the LLM phase pointlessly every time.

    NOTE: `date_created` is the MP's creation date, not "time of the most
    recent push" -- a diff that goes missing again after a later push (rare)
    could in theory hit the non-generating branch sooner than a fresh MP
    would. Good enough for now; revisit if that turns out to matter.

    The service-maintainer notification #30 asked for here (a human-
    actionable Launchpad-side problem, not something the contributor can
    fix) was built as design #48 -- but it fires from main.py's LLM phase
    (where `diff_text(lp_obj) is False` is observed exactly once per MP),
    not from this function, precisely because of the not-memoized note
    below.

    NOT memoized: up to four checks (conflicts, empty diff, changelog bug
    reference, stale version) can each hit this same missing-diff path for
    the same MP within one `triage_url` call, so the WARNING below prints up
    to four times per stuck MP per run (confirmed live, MP #503471) --
    `functools.lru_cache` was tried and reverted, since real launchpadlib
    `Entry` objects aren't hashable (`TypeError: unhashable type: 'Entry'`,
    caught live before this shipped) and an `id(lp_obj)`-keyed dict risks
    incorrect cache hits from id reuse after garbage collection across a
    long `--all` run. Repeated printing is harmless.
    """
    created = getattr(lp_obj, "date_created", None)
    if created is None:
        return True
    try:
        age = datetime.datetime.now(datetime.timezone.utc) - created
    except TypeError as e:
        logger.debug(
            "_diff_missing_is_still_generating: couldn't compute MP age (%s); "
            "assuming still generating.",
            e,
        )
        return True

    if age < _MISSING_DIFF_GRACE:
        logger.debug(
            "_diff_missing_is_still_generating: MP created %s ago (< %s); "
            "probably still generating.",
            age,
            _MISSING_DIFF_GRACE,
        )
        return True

    logger.warning(
        "preview_diff has been missing for %s (MP created %s) -- longer than "
        "Launchpad's diff generation should ever take. This likely means "
        "Launchpad hit a bug generating the diff and never retried. Treating "
        "as 'no diff data available' rather than retrying forever (a human "
        "should look at this MP directly).",
        age,
        created,
    )
    return False


def check_mp_conflicts(url, lp_obj, lp_client):
    """
    Check: Merge Conflicts
    Rejects MPs that have conflicts.

    `preview_diff` is fetched lazily by launchpadlib on first access -- a
    network/API failure there (or reading its `conflicts` attribute) would
    otherwise propagate straight out of this function and crash the whole
    triage run (a `--all` run would die mid-queue on a single transient
    failure). Returns None ("couldn't determine", see checks.py's general
    None/False convention -- e.g. _is_merge_proposal, check_stale_version)
    rather than letting that happen or guessing False ("no conflicts").

    A `preview_diff` that's cleanly None (no exception -- Launchpad simply
    hasn't produced one) is its own case, handled by
    `_diff_missing_is_still_generating`: None while within the grace period,
    False once it's clearly stuck (see that function's docstring).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type != "branch_merge_proposal":
        return False

    # Conflict info lives on the preview diff (the MP has no `has_conflicts`).
    # `conflicts` is a string listing conflicting files; empty means none.
    try:
        diff = getattr(lp_obj, "preview_diff", None)
        if diff is None:
            if _diff_missing_is_still_generating(lp_obj):
                return None
            return False
        conflicts = (getattr(diff, "conflicts", "") or "").strip()
    except Exception as e:
        logger.debug(
            "check_mp_conflicts: couldn't read preview_diff (%s); can't determine.",
            e,
        )
        return None
    logger.debug("check_mp_conflicts: conflicts=%r", conflicts)
    if conflicts:
        logger.info("[%s] has conflicts. Adding an incomplete finding.", url)
        return Finding(
            "incomplete",
            "This Merge Proposal has merge conflicts and cannot be cleanly "
            "merged. Please rebase your branch, resolve the conflicts, and "
            "push the updated branch.",
        )
    return False


_MERGE_BRANCH_RE = re.compile(r"merge", re.IGNORECASE)
_MERGE_BUG_TITLE_RE = re.compile(r"^(please\s+)?merge\b", re.IGNORECASE)


def _is_merge_proposal(lp_obj):
    """
    Best-effort, fail-safe-to-'not a merge' detection of a git-ubuntu "merge"
    (rebase onto a newer Debian revision) MP, as opposed to a plain fix or an
    SRU MP. Only merge MPs are required to target `debian/sid` or
    `debian/experimental` -- fix MPs and SRUs correctly target `ubuntu/devel`
    / `ubuntu/<series>`, and the old check bounced them unconditionally.

    Launchpad has no field that says "this MP is a merge", so this is a
    heuristic, validated against the live ~ubuntu-sponsors queue (2026-07-02,
    52 open MPs, seb128): every merge MP's source branch name contains
    "merge" (e.g. "merge/1.2-3", "merge-lp1234567-stonking"); every fix MP's
    doesn't -- zero mismatches. Checked first since `source_git_path` is
    already on the object (no extra API call).

    Falls back to linked-bug titles (git-ubuntu merge bugs are titled "Merge
    <pkg> from Debian ..." / "Please merge ...") for merge MPs that don't
    follow the branch-naming convention -- costs one extra API call
    (`lp_obj.bugs`), so only made when the cheap check didn't already decide.

    Returns True/False/None: None means the branch name wasn't decisive and
    the `lp_obj.bugs` fallback call itself failed (a genuine API/network
    error), as opposed to succeeding and finding no merge-shaped bug title
    (real False). Callers must treat None as "couldn't determine" -- not
    "confirmed not a merge" -- since a network blip is retriable and a
    caller that persists this as a definitive False would incorrectly cache
    "not a merge" until the contributor changes something.
    """
    source_branch_name = getattr(lp_obj, "source_git_path", "") or ""
    if _MERGE_BRANCH_RE.search(source_branch_name):
        logger.debug(
            "_is_merge_proposal: source branch %r looks like a merge branch.",
            source_branch_name,
        )
        return True

    try:
        bugs = list(lp_obj.bugs)
    except Exception as e:
        logger.debug(
            "_is_merge_proposal: source branch %r isn't merge-shaped and "
            "linked bugs couldn't be checked (%s); can't determine.",
            source_branch_name,
            e,
        )
        return None

    for bug in bugs:
        if _MERGE_BUG_TITLE_RE.match(bug.title or ""):
            logger.debug(
                "_is_merge_proposal: source branch %r isn't merge-shaped, "
                "but linked bug %r looks like a merge bug.",
                source_branch_name,
                bug.title,
            )
            return True

    logger.debug(
        "_is_merge_proposal: source branch %r and linked bug titles (%s) "
        "don't look like a merge; not a merge.",
        source_branch_name,
        [b.title for b in bugs],
    )
    return False


_CHANGELOG_HEADER_RE = re.compile(
    r"^(?P<pkg>\S+)\s+\((?P<version>[^)]+)\)\s+(?P<suite>[a-zA-Z][\w.-]*);\s*urgency="
)
_UBUNTU_SUFFIX_RE = re.compile(r"ubuntu\d+$", re.IGNORECASE)
_SOURCE_PACKAGE_FROM_URL_RE = re.compile(r"/\+source/(?P<pkg>[^/]+)/\+git/")


def _strip_ubuntu_suffix(version):
    """'1.2-3ubuntu1' -> '1.2-3' (keeps any epoch). None-safe."""
    if not version:
        return None
    return _UBUNTU_SUFFIX_RE.sub("", version)


def _source_package_from_mp(lp_obj):
    """Best-effort source package name from the MP's own URL
    ('.../+source/<pkg>/+git/...'). None if it can't be found."""
    for attr in ("web_link", "self_link"):
        url = getattr(lp_obj, attr, "") or ""
        match = _SOURCE_PACKAGE_FROM_URL_RE.search(url)
        if match:
            return match.group("pkg")
    return None


# One-entry memo for diff_text, (diff_self_link, result) or None.
# Checks 2/5/6 -- and, since #47, the LLM MP review -- each need the same
# diff content within a single item's triage; without this they re-fetched
# it independently -- doubling both the time (the two slowest checks in
# every timing capture, design_journal.md #33) and the exposure to librarian
# slow-trickle timeouts (seen live on grub2 #507575: check 5's fetch
# succeeded, check 6's identical re-fetch timed out seconds later,
# design_journal.md #37). Keyed on the preview diff's self_link -- a stable,
# hashable string that changes when the contributor pushes (new diff, new
# link), unlike the launchpadlib Entry itself (unhashable, see #30's
# reverted lru_cache) or id(lp_obj) (GC reuse risk). One entry only: checks
# for the same item run consecutively, so memory stays bounded and
# cross-item reuse is structurally impossible. Failures (None) are cached
# too, deliberately: the second caller re-attempting a fetch that just
# failed is exactly the compounding this exists to remove -- the item is
# inconclusive either way and retries next run (#28). The memo holds the
# FULL diff text (not the extracted changelog section, as pre-#47) so the
# LLM phase can reuse the same fetch.
_diff_text_cache = None


def reset_diff_lines_cache():
    """Drop the per-item diff-content memo. Called by main at the start of
    each item (hygiene; distinct real MPs can't share a diff self_link) and
    by the test suite between tests (fakes CAN reuse links like '/d/1')."""
    global _diff_text_cache
    _diff_text_cache = None


def diff_text(lp_obj):
    """The full preview-diff text for this MP, memoized per preview diff
    per item. Costs one extra API call (fetching the diff content itself,
    beyond the metadata check_mp_conflicts/check_empty_diff already use).

    Returns a str, or one of two different falsy values that callers must
    NOT treat interchangeably:
    - None: the diff itself couldn't be fetched (an exception reading
      diff_text -- typically a network/API failure -- or a missing
      preview_diff that's still within its generation grace period, see
      `_diff_missing_is_still_generating`). This is retriable -- a caller
      must propagate it as "couldn't determine", not as a confirmed negative.
    - False: preview_diff has been missing far longer than diff generation
      should ever take -- no diff data exists. A stable fact that won't
      change on retry (barring the contributor pushing new commits or
      Launchpad belatedly generating the diff -- both already caught by the
      facts-change gate, since a new/real diff changes the fingerprinted
      diff_id/diff_lines_count in facts.build_facts).
    """
    global _diff_text_cache
    try:
        diff = getattr(lp_obj, "preview_diff", None)
        key = getattr(diff, "self_link", None) if diff is not None else None
    except Exception:
        key = None
    if key is not None and _diff_text_cache and _diff_text_cache[0] == key:
        logger.debug("diff_text: reusing already-fetched diff content")
        return _diff_text_cache[1]
    result = _diff_text_fetch(lp_obj)
    if key is not None:
        _diff_text_cache = (key, result)
    return result


def _diff_text_fetch(lp_obj):
    diff = getattr(lp_obj, "preview_diff", None)
    if diff is None:
        if _diff_missing_is_still_generating(lp_obj):
            return None
        return False
    try:
        return diff.diff_text.open().read().decode(errors="replace")
    except Exception as e:
        logger.debug("diff_text: could not fetch diff text: %s", e)
        return None


def _changelog_diff_lines(lp_obj):
    """The unified-diff lines for the debian/changelog hunk in this MP's
    preview diff. Same None/False contract as `diff_text` (which this
    extracts from), with one addition: False also means the diff was
    fetched fine but has no debian/changelog section at all."""
    text = diff_text(lp_obj)
    if not isinstance(text, str):
        return text

    for section in re.split(r"^diff --git a/", text, flags=re.MULTILINE):
        if section.startswith("debian/changelog "):
            return section.splitlines()
    logger.debug("_changelog_diff_lines: no debian/changelog section in diff.")
    return False


def _debian_target_suite(lp_obj):
    """
    The Debian suite (in branch-name form: 'sid' or 'experimental') a merge
    MP should target, or None if it can't be determined -- callers fail safe
    to a generic message rather than guessing which of the two to name.

    Primary: diff-context. A merge prepends the new Ubuntu changelog entry to
    the top of debian/changelog, so the first unchanged (context) line below
    it is the header of the Debian entry the merge is based on -- same
    version modulo the trailing "ubuntuN", and its suite (unstable/
    experimental) is exactly the Debian suite to target (Debian has no
    experimental->sid auto-migration, so the suite a version was uploaded to
    is where it stays). No extra API call beyond fetching the diff text.

    Fallback: archive_lookup, only tried when diff-context didn't resolve
    it. Derives the Debian version from the proposed Ubuntu version (read
    from the diff's own added changelog header) and checks which suite(s)
    hold exactly that version.
    """
    lines = _changelog_diff_lines(lp_obj)
    new_version = None
    if lines:
        saw_added_entry = False
        for line in lines:
            if (
                line.startswith("+++")
                or line.startswith("---")
                or line.startswith("@@")
            ):
                continue
            if line.startswith("+"):
                saw_added_entry = True
                if new_version is None:
                    match = _CHANGELOG_HEADER_RE.match(line[1:])
                    if match:
                        new_version = match.group("version")
                continue
            if saw_added_entry and line.startswith(" "):
                match = _CHANGELOG_HEADER_RE.match(line[1:])
                if match:
                    suite = match.group("suite").lower()
                    logger.debug("_debian_target_suite: diff-context suite=%r", suite)
                    return "sid" if suite == "unstable" else suite
                break  # first context line wasn't a changelog header; give up

    package = _source_package_from_mp(lp_obj)
    debian_base_version = _strip_ubuntu_suffix(new_version)
    if not package or not debian_base_version:
        logger.debug(
            "_debian_target_suite: diff-context inconclusive and archive_lookup "
            "fallback can't run (package=%r, debian_base_version=%r).",
            package,
            debian_base_version,
        )
        return None

    versions = archive_lookup.debian_versions(package)
    if not versions:
        logger.debug(
            "_debian_target_suite: archive_lookup found nothing for %r.", package
        )
        return None

    matches = {
        suite
        for suite in ("unstable", "experimental")
        if versions.get(suite) == debian_base_version
    }
    logger.debug(
        "_debian_target_suite: archive_lookup fallback, debian_base_version=%r "
        "-> matching suites=%s",
        debian_base_version,
        matches,
    )
    if len(matches) == 1:
        suite = matches.pop()
        return "sid" if suite == "unstable" else suite
    return None


def check_target_branch(url, lp_obj, lp_client):
    """
    Check 2: Target Branch
    Rejects merge MPs (rebase onto a newer Debian revision) that target
    `ubuntu/devel` directly instead of `debian/sid`/`debian/experimental`.
    Plain fix MPs and SRUs legitimately target `ubuntu/devel` /
    `ubuntu/<series>` and are left alone -- see _is_merge_proposal.

    Returns True/False/None like every check, but None specifically means
    _is_merge_proposal's `lp_obj.bugs` fallback call failed and we
    genuinely can't tell if this is a merge MP -- see _is_merge_proposal's
    docstring for why that must not be cached as "not a merge".
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type != "branch_merge_proposal":
        return False

    is_merge = _is_merge_proposal(lp_obj)
    if is_merge is None:
        logger.debug(
            "check_target_branch: couldn't determine whether this is a "
            "merge MP; leaving for retry rather than guessing."
        )
        return None
    if not is_merge:
        logger.debug(
            "check_target_branch: not a merge MP; ubuntu/devel (or "
            "ubuntu/<series> for an SRU) is a legitimate target. Skipping."
        )
        return False

    target_branch_name = getattr(lp_obj, "target_git_path", "") or ""
    logger.debug(
        "check_target_branch: merge MP, target_git_path=%r", target_branch_name
    )

    if "debian/" not in target_branch_name:
        suite = _debian_target_suite(lp_obj)
        logger.debug("check_target_branch: suggested target suite=%r", suite)
        target_phrase = (
            f"`debian/{suite}`"
            if suite
            else "`debian/sid` (or `debian/experimental`, matching the Debian suite you uploaded to)"
        )
        logger.info(
            "[%s] is a merge MP incorrectly targeting %r. Adding an "
            "incomplete finding.",
            url,
            target_branch_name,
        )
        return Finding(
            "incomplete",
            "This Merge Proposal is a merge (rebase onto a newer Debian "
            f"revision) but targets `ubuntu/devel`. According to our workflow, "
            f"merge MPs should target {target_phrase} instead (workaround for "
            "LP: #1976112). Please update the target branch. See "
            "https://ubuntu.com/project/docs/contributors/merging/git-ubuntu-merge-proposal/#merge-git-ubuntu-merge-proposal",
        )

    return False


def check_empty_diff(url, lp_obj, lp_client):
    """
    Check 4: Empty Diff
    Rejects MPs where the diff is empty (already landed).

    Same rationale as check_mp_conflicts for the try/except: `preview_diff`
    is fetched lazily, so a network/API failure reading it must return None
    ("couldn't determine"), not crash the run or guess False. A cleanly-None
    `preview_diff` (no exception -- see `_diff_missing_is_still_generating`)
    is None while within the grace period, False once it's clearly stuck.
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type != "branch_merge_proposal":
        return False

    try:
        diff = getattr(lp_obj, "preview_diff", None)
        if diff is None:
            if _diff_missing_is_still_generating(lp_obj):
                return None
            return False
        diff_lines_count = getattr(diff, "diff_lines_count", -1)
    except Exception as e:
        logger.debug(
            "check_empty_diff: couldn't read preview_diff (%s); can't determine.",
            e,
        )
        return None

    logger.debug("check_empty_diff: diff_lines_count=%s", diff_lines_count)
    if diff_lines_count == 0:
        comment = (
            "Thanks for your contribution! The proposed change seems to have landed in the target Vcs, "
            "so the merge request can be closed."
        )
        logger.info(
            "[%s] has an empty diff. Commenting (no status write -- git-ubuntu "
            "MPs don't accept it; a human closes this out).",
            url,
        )
        lp_client.comment(lp_obj, comment)

        return True

    return False


_LP_BUG_BLOCK_RE = re.compile(r"LP:?\s*(#\d+(?:\s*,\s*#\d+)*)", re.IGNORECASE)
_BUG_NUM_RE = re.compile(r"#(\d+)")


def _new_changelog_stanza_lines(lp_obj):
    """Content lines (leading '+' stripped) of the *new* debian/changelog
    stanza this MP adds.

    Bounded to the *first* changelog stanza only (stops at the second entry
    header found among the added lines). This matters more than it sounds:
    a git-ubuntu merge's diff can show far more than just the new entry as
    "+" -- confirmed live against a real merge MP where 1472 of 1697
    debian/changelog diff lines were "+", i.e. most of the file's history,
    not a clean top-of-file insert (git couldn't produce a smaller diff
    against the diverged history). Without this bound we'd sweep in
    long-since dead, unrelated content from entries several merges back.

    Propagates _changelog_diff_lines's None/False distinction (see its
    docstring): None means the diff itself couldn't be fetched (retriable,
    "couldn't determine"); an empty list means the diff was read fine but
    has no debian/changelog section, or has one with nothing added to it
    (stable fact, safe to treat as "nothing to check")."""
    lines = _changelog_diff_lines(lp_obj)
    if lines is None:
        return None
    if lines is False:
        return []
    added = [
        line[1:]
        for line in lines
        if line.startswith("+") and not line.startswith("+++")
    ]
    entry_lines = []
    headers_seen = 0
    for line in added:
        if _CHANGELOG_HEADER_RE.match(line):
            headers_seen += 1
            if headers_seen > 1:
                break
        entry_lines.append(line)
    return entry_lines


def _lp_bug_numbers_from_new_changelog_entry(lp_obj):
    """LP bug numbers cited in the *new* changelog entry (e.g. '(LP:
    #1234567)', also matches the colon-less 'LP #1234567' / 'LP#1234567'
    forms seen in "Remaining changes" bullets, possibly several
    comma-separated), or None if the diff itself couldn't be fetched
    (retriable -- see _changelog_diff_lines). An empty set (not None)
    means the diff was read fine -- whether or not it touches
    debian/changelog at all -- and the new entry (if any) cites no bug."""
    entry_lines = _new_changelog_stanza_lines(lp_obj)
    if entry_lines is None:
        return None
    return _lp_bug_numbers_from_text("\n".join(entry_lines))


def _lp_bug_numbers_from_text(text):
    """LP bug numbers cited in a changelog-entry text -- the extraction
    core shared by the MP path above and the attachment path (#63)."""
    numbers = set()
    for block in _LP_BUG_BLOCK_RE.findall(text):
        numbers.update(int(n) for n in _BUG_NUM_RE.findall(block))
    return numbers


def _proposed_changelog_entry(lp_obj):
    """(version, full_entry_text) for the new (top) debian/changelog stanza
    this MP adds, as read from the diff -- see _new_changelog_stanza_lines
    for the first-stanza bound.

    version is one of three kinds of value, which callers must not treat
    interchangeably:
    - a version string: found it.
    - None: the diff itself couldn't be fetched -- retriable, "couldn't
      determine" (see _changelog_diff_lines).
    - False: the diff was read fine but has no debian/changelog section, or
      its first added block isn't a recognizable changelog header -- a
      stable fact about this MP's diff content.
    entry_text is the full stanza text when version is a string, else None.
    """
    entry_lines = _new_changelog_stanza_lines(lp_obj)
    if entry_lines is None:
        return None, None
    if not entry_lines:
        return False, None
    match = _CHANGELOG_HEADER_RE.match(entry_lines[0])
    if not match:
        return False, None
    return match.group("version"), "\n".join(entry_lines)


def _first_changelog_stanza_text(text):
    """The first stanza (topmost entry) of a plain-text debian/changelog
    file, e.g. as fetched from archive_lookup.changelog_text(). Stops at
    the second header line found, same bounding rule as
    _new_changelog_stanza_lines (a published changelog is the whole file's
    history, not just the latest entry)."""
    headers_seen = 0
    stanza = []
    for line in text.splitlines():
        if _CHANGELOG_HEADER_RE.match(line):
            headers_seen += 1
            if headers_seen > 1:
                break
        stanza.append(line)
    return "\n".join(stanza)


def _normalize_changelog_entry(text):
    """Whitespace-tolerant form of a changelog stanza for equality
    comparison (trailing whitespace per line, and leading/trailing blank
    lines, shouldn't count as a content difference)."""
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines())


def _strip_changelog_trailer(text):
    """Drop a stanza's ' -- maintainer <email>  date' trailer line(s). A
    queued upload's .changes Changes field carries the stanza WITHOUT the
    trailer, so comparing it against a debian/changelog stanza must ignore
    the trailer on both sides (design_journal.md #56)."""
    return "\n".join(
        line for line in text.splitlines() if not line.startswith(" -- ")
    )


def _bug_targets_package(bug, package):
    """True if any task on `bug` is against `package` -- a bug page can
    legitimately list several packages/projects, so any single match is
    enough (matches 'pkg (Ubuntu)', 'pkg (Debian)', 'pkg (Ubuntu <Series>)',
    or a bare project name equal to `package`)."""
    for task in bug.bug_tasks:
        name = task.bug_target_name or ""
        if name == package or name.startswith(f"{package} ("):
            return True
    return False


def check_changelog_bug_reference(url, lp_obj, lp_client):
    """
    Check: changelog LP bug reference sanity.

    A git-ubuntu changelog entry commonly cites an LP bug, e.g.
    '(LP: #1234567)'. If that bug isn't reported against this source
    package at all, it's very likely a copy-paste/typo'd bug number that
    would otherwise ship into the released changelog permanently -- warn so
    a human can double-check before sponsoring.

    Fail-safe: an unidentifiable source package or no bug reference cited
    (diff read fine, nothing to check) -> skip, definitively nothing wrong.
    An unreadable diff, or every cited bug's lookup failing -> return None
    ("couldn't determine", retriable) rather than a clean False, so a
    network blip doesn't get cached as "no problem" forever -- see
    _changelog_diff_lines and _is_merge_proposal's docstrings for the same
    None/False distinction elsewhere in this module. A confirmed mismatch
    always fires (True) even if some *other* cited bug's lookup failed --
    we already have a definite problem to report.

    Bug-side parity (#63): the same rule applies to the new changelog
    entry inside a debdiff attached to a bug -- see
    _changelog_bug_reference_bug.
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        return _changelog_bug_reference_bug(url, bug, lp_client)
    if resource_type != "branch_merge_proposal":
        return False

    package = _source_package_from_mp(lp_obj)
    if not package:
        logger.debug(
            "check_changelog_bug_reference: could not determine source "
            "package; skipping."
        )
        return False

    bug_numbers = _lp_bug_numbers_from_new_changelog_entry(lp_obj)
    if bug_numbers is None:
        logger.debug("check_changelog_bug_reference: diff unreadable; can't determine.")
        return None
    if not bug_numbers:
        logger.debug(
            "check_changelog_bug_reference: no LP bug reference in the new "
            "changelog entry; skipping."
        )
        return False

    return _bug_reference_verdict(
        url, "the changelog", bug_numbers, package, lp_client
    )


def _bug_reference_verdict(url, where, bug_numbers, package, lp_client, host_bug=None):
    """The shared verification tail of the changelog bug-reference check
    (#63): load each cited bug, compare against `package`, and turn the
    result into a Finding/False/None with the docstring's fail-safe
    semantics. `host_bug`, when given (the attachment path), short-cuts
    a citation of the very bug under triage -- we already hold it, no
    extra API call."""
    mismatched = []
    lookup_failed = False
    host_id = getattr(host_bug, "id", None) if host_bug is not None else None
    for number in sorted(bug_numbers):
        if number == host_id:
            bug = host_bug
        else:
            try:
                bug = lp_client.lp.bugs[number]
            except Exception as e:
                logger.debug(
                    "check_changelog_bug_reference: could not load bug #%s "
                    "(%s); skipping it rather than guessing.",
                    number,
                    e,
                )
                lookup_failed = True
                continue
        if not _bug_targets_package(bug, package):
            mismatched.append(number)

    logger.debug(
        "check_changelog_bug_reference: package=%r cited=%s mismatched=%s "
        "lookup_failed=%s",
        package,
        sorted(bug_numbers),
        mismatched,
        lookup_failed,
    )
    if not mismatched:
        if lookup_failed:
            logger.debug(
                "check_changelog_bug_reference: couldn't verify every cited "
                "bug; can't determine."
            )
            return None
        return False

    bug_list = ", ".join(f"LP: #{n}" for n in mismatched)
    logger.info(
        "[%s] changelog cites %s, not reported against %r. Adding an "
        "incomplete finding.",
        url,
        bug_list,
        package,
    )
    return Finding(
        "incomplete",
        f"The bug reference(s) in {where} ({bug_list}) don't appear to "
        f"be reported against `{package}`. Please double-check the bug "
        "number(s) are correct.",
    )


def _changelog_bug_reference_bug(url, bug, lp_client):
    """
    The bug-side path of the changelog bug-reference check (#63): reads
    the new changelog entry from the newest usable patch/debdiff
    attachment (attachments.review_target -- memoized, free when Check 8
    already fetched it) and verifies its LP: #nnn citations the same way
    the MP path does.

    The package comes from the entry's own header (that's what would be
    uploaded). A plain patch has no changelog entry, so it -- like a
    debdiff citing no bug -- skips naturally; no debdiff/patch
    classification needed. Citing the host bug itself (the common case)
    is checked against the bug object we already hold.

    Returns an incomplete Finding, False, or None (attachment fetch or a
    cited bug's lookup failed with nothing confirmed -- retriable).
    """
    target = attachments.review_target(bug)
    if target is None:
        return None
    if target is False:
        return False
    _attachment, text = target

    stanza = llm_reviewer._new_changelog_stanza(text)
    if not stanza:
        logger.debug(
            "_changelog_bug_reference_bug: no new changelog entry in the "
            "attachment; skipping."
        )
        return False
    header = _CHANGELOG_HEADER_RE.match(stanza.splitlines()[0])
    if not header:
        logger.debug(
            "_changelog_bug_reference_bug: unparseable entry header; skipping."
        )
        return False
    package = header.group("pkg")

    bug_numbers = _lp_bug_numbers_from_text(stanza)
    if not bug_numbers:
        logger.debug(
            "_changelog_bug_reference_bug: entry cites no bug; skipping."
        )
        return False

    return _bug_reference_verdict(
        url,
        "the attached debdiff's changelog",
        bug_numbers,
        package,
        lp_client,
        host_bug=bug,
    )


_RECENT_UPLOAD_GRACE = datetime.timedelta(hours=24)

# git-ubuntu's per-series "tip" branches are named 'ubuntu/<series>-devel'
# (e.g. 'ubuntu/noble-devel' for an SRU targeting noble), distinct from the
# single 'ubuntu/devel' branch that tracks the archive's actual development
# series. Matches either shape; non-greedy so '-devel' is stripped when
# present rather than swallowed into the series name.
_TARGET_SERIES_RE = re.compile(r"ubuntu/(?P<series>[a-z0-9.]+?)(?:-devel)?$")


def _target_ubuntu_series(lp_obj, lp):
    """The Ubuntu series this MP's changes actually target: 'noble' for
    'refs/heads/ubuntu/noble-devel' (an SRU), the current devel codename
    (e.g. 'stonking') for 'refs/heads/ubuntu/devel' or any target that
    doesn't parse as 'ubuntu/<series>' at all (a Debian merge MP not yet
    retargeted to debian/*, which still lands via devel -- see #27).

    Found live (design_journal.md #41): check_stale_version previously
    always compared against the current devel series regardless of what
    the MP actually targets, which wrongly flagged a real SRU (MP #504085,
    targeting noble) as 'older than devel' when devel (an unrelated,
    unreleased series) was simply never the right comparison at all.

    None only when devel_codename() itself fails -- retriable, not a guess.
    """
    target = getattr(lp_obj, "target_git_path", "") or ""
    match = _TARGET_SERIES_RE.search(target)
    series = match.group("series") if match else None
    if series and series != "devel":
        return series
    return archive_lookup.devel_codename(lp)


def _max_published_version(versions):
    """The highest version among a {suite: version} dict (see
    archive_lookup.ubuntu_versions) -- checks every pocket present for the
    series (release, updates, security, proposed), not just release/
    proposed, since for a stable series any of updates/security/proposed
    can be the one currently ahead. None if nothing is published (a stable,
    structural fact -- distinct from the lookup itself failing, which
    ubuntu_versions already signals by returning None for the whole dict,
    checked by the caller before this is ever called)."""
    highest = None
    for version in versions.values():
        if highest is None or archive_lookup.version_compare(version, highest) > 0:
            highest = version
    return highest


def check_stale_version(url, lp_obj, lp_client):
    """
    Check: proposed version vs. archive.

    Compares the version in the MP's new (top) debian/changelog entry
    against what's currently published for this package in the series the
    MP actually targets -- the current devel series for an
    'ubuntu/devel'-targeting MP (including a Debian merge MP still
    targeting debian/*, which lands via devel per #27), or the specific
    stable series for an SRU targeting 'ubuntu/<series>-devel' (see
    _target_ubuntu_series). Checks every pocket published for that series
    (release, updates, security, proposed) and takes the highest version,
    not just release-or-proposed -- whichever pocket is actually ahead is
    what a new upload would land behind (design_journal.md #41: comparing
    an SRU against the unrelated devel series produced a false "stale"
    bounce live).

    1. proposed > archive: nothing to do, this is the normal case.
    2. proposed < archive: normally means someone else's upload already
       landed with a higher version while this MP sat in the queue --
       needs a rebase. But first (design_journal.md #43, found live: MP
       #505086, backport-iwlwifi-dkms): check whether the exact PROPOSED
       version was ever published at all (any status -- Published or
       since Superseded/Deleted). If so, this MP's own change already
       landed and was later superseded by unrelated, newer work -- "please
       rebase" would be the wrong message; instead this runs the same
       content comparison as case 3 against that historical publication
       (3a/3b below apply identically), since the version being older than
       the current archive max doesn't mean it was never uploaded. Only
       when no publication of the exact proposed version exists at all
       does this fall through to the original "needs a rebase" bounce.
    3. proposed == archive: a publication with this exact version already
       exists. Fetch its changelog (SourcePackagePublishingHistory.
       changelogUrl()) and compare content against the proposed entry:
       3a. same content -> this MP's change was already uploaded. If the
           publication is younger than _RECENT_UPLOAD_GRACE, defer instead
           of commenting: git-ubuntu's own importer will notice the merge
           landed and auto-close the MP itself once it catches up, which
           can take a while -- commenting "this can be closed" immediately
           risks doing so right as (or just before) Launchpad closes it on
           its own. Once the publication is old enough that auto-close
           would have already happened if it were going to, comment as
           normal.
       3b. different content -> a *different* upload reused the same
           version number; needs a rebase (new version). Not deferred --
           there's nothing for git-ubuntu's importer to reconcile here, the
           content genuinely differs regardless of how long ago the archive
           upload landed.

    Bug-side parity (#65): the same comparison runs for a debdiff
    attached to a bug -- see _stale_version_bug. The verdict core
    (_stale_version_verdict) is shared; only the input gathering and the
    already-landed handling (_classify_against_publication vs its _bug
    sibling) differ.

    Fail-safe throughout, but distinguishes two kinds of "nothing to
    report" (see _changelog_diff_lines / _is_merge_proposal's docstrings
    for the same distinction elsewhere in this module):
    - a stable, structural fact (e.g. this MP's diff genuinely has no
      parseable changelog version, or genuinely nothing is published for
      this package yet) -> False, safe to cache.
    - an actual lookup/fetch failure (diff unreadable, devel series
      lookup failed, archive lookup failed, publication record or its
      changelog couldn't be fetched) -> None, "couldn't determine" --
      retriable, must NOT be cached as a clean pass. This is what a
      transient network blip on any of these calls looks like; treating
      it as a plain False would silently and permanently suppress this
      check for that MP the moment main.py persists facts on whatever
      conclusive state the pipeline falls through to.

    Returns a Finding (2, 3b -- an incomplete-tier "needs rebasing" point
    for the aggregated comment, design_journal.md #31), "done" (3a, old
    enough -- closing tier, posts its own terse comment), "pending" (3a,
    too recent -- deliberately deferred), "queued" (already uploaded and
    waiting in the target series' upload queue for archive review, #55 --
    deferred silently until it publishes and becomes "done"), False (1, or
    a structural non-applicability), or None (a lookup/fetch failure) --
    unlike every other check in this module, a fired result here can mean
    several different outcomes, so the caller (main.py) needs to know which.
    "pending", "queued" and None are all outcomes the caller must NOT persist a
    facts snapshot for: nothing about the MP itself changes while we wait
    (out the grace period, or for the next retry), so persisting facts
    here would make the top-level "facts unchanged -> skip" gate
    permanently skip re-checking this URL.
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        return _stale_version_bug(url, bug, lp_client)
    if resource_type != "branch_merge_proposal":
        return False

    package = _source_package_from_mp(lp_obj)
    proposed_version, proposed_entry = _proposed_changelog_entry(lp_obj)
    logger.debug(
        "check_stale_version: package=%r proposed_version=%r",
        package,
        proposed_version,
    )
    if not package:
        return False
    if proposed_version is None:
        logger.debug("check_stale_version: diff unreadable; can't determine.")
        return None
    if not proposed_version:
        return False

    target_series = _target_ubuntu_series(lp_obj, lp_client.lp)
    if not target_series:
        logger.debug(
            "check_stale_version: couldn't determine the target series; can't determine."
        )
        return None

    def classify(version, pub):
        return _classify_against_publication(
            url, lp_obj, lp_client, package, version, proposed_entry, pub
        )

    return _stale_version_verdict(
        url, lp_client, package, proposed_version, proposed_entry,
        target_series, classify,
    )


def _stale_version_verdict(
    url, lp_client, package, proposed_version, proposed_entry, target_series,
    classify,
):
    """The input-source-independent core of the stale-version check (#65):
    archive comparison, the upload-queue race (#55/#56), the historical-
    publication rescue (#43). `classify(version, pub)` handles the
    "exact version already published" outcome -- _classify_against_
    publication for MPs (grace defer, rich-history diagnosis), its _bug
    sibling for debdiffs on bugs. Return-value contract is
    check_stale_version's."""
    versions = archive_lookup.ubuntu_versions(
        lp_client.lp, package, series_names=[target_series]
    )
    if versions is None:
        logger.debug("check_stale_version: archive lookup failed; can't determine.")
        return None
    archive_version = _max_published_version(versions)
    logger.debug(
        "check_stale_version: target_series=%s versions=%s -> using %r",
        target_series,
        versions,
        archive_version,
    )
    if not archive_version:
        return False

    cmp = archive_lookup.version_compare(proposed_version, archive_version)
    logger.debug(
        "check_stale_version: proposed=%r archive=%r cmp=%d",
        proposed_version,
        archive_version,
        cmp,
    )
    if cmp > 0:
        # Newer than anything published -- but it may already have been
        # uploaded and be sitting in the series' upload queue (Unapproved /
        # New / Accepted), where it's invisible to the publication lookups
        # above. Typical for an SRU awaiting SRU-team review (design #55,
        # found live: libp11 MP #507660 sat in noble's Unapproved queue
        # while the bot spent an LLM review concluding "ready for a
        # sponsor"). At that point sponsoring is done -- queue review isn't
        # the sponsors' job -- so nothing should be posted and no LLM
        # tokens spent; once the queue accepts and publishes it, the
        # cmp == 0 path below closes the MP out as usual.
        queued = archive_lookup.upload_in_queue(
            lp_client.lp, package, target_series, proposed_version
        )
        if queued is None:
            logger.debug(
                "check_stale_version: upload-queue lookup failed; can't determine."
            )
            return None
        if queued:
            # Same version doesn't prove same upload: SRU version increments
            # are convention-fixed, so an independent SRU racing this MP
            # would pick the very same version number (seb128, design #56).
            # Tell them apart by changelog content, same idea as
            # _classify_against_publication -- the queue .changes' Changes
            # field carries the new stanza (sans the ' -- ' trailer, so the
            # trailer is ignored on both sides).
            queue_entry = archive_lookup.queue_changes_text(queued)
            if queue_entry is None:
                logger.debug(
                    "check_stale_version: couldn't read the queued upload's "
                    "Changes field; can't determine whose upload it is."
                )
                return None
            same_content = _normalize_changelog_entry(
                _strip_changelog_trailer(proposed_entry)
            ) == _normalize_changelog_entry(_strip_changelog_trailer(queue_entry))
            if not same_content:
                logger.info(
                    "[%s] version %r is waiting in the %s upload queue with "
                    "DIFFERENT content -- a same-version race. Adding an "
                    "incomplete finding.",
                    url,
                    proposed_version,
                    target_series,
                )
                return Finding(
                    "incomplete",
                    f"An upload with the same version (`{proposed_version}`) "
                    "but different content is already waiting in the "
                    f"{target_series} upload queue. Your change needs to be "
                    "rebased (with a new version number) and resubmitted.",
                )
            logger.info(
                "[%s] version %r is already waiting in the %s upload queue "
                "with matching content; nothing left to sponsor while it "
                "awaits archive review.",
                url,
                proposed_version,
                target_series,
            )
            return "queued"
        return False

    if cmp < 0:
        # Before assuming "someone else's newer upload landed, please
        # rebase": check whether the exact PROPOSED version was ever
        # published for this package (any status -- Published or since
        # Superseded/Deleted). If it was, this MP's own change already
        # landed at some point and was later superseded by unrelated,
        # newer work -- "please rebase" is the wrong message; "already
        # uploaded, this MP can be closed" is (design_journal.md #43,
        # found live: MP #505086, backport-iwlwifi-dkms, where the
        # currently-Superseded archive record for the exact proposed
        # version matched its content).
        historical_pub = archive_lookup.published_source(
            lp_client.lp, package, target_series, proposed_version, status=None
        )
        if historical_pub is not None:
            outcome = classify(proposed_version, historical_pub)
            if outcome is not None:
                return outcome
            # Publication exists but its changelog couldn't be fetched --
            # genuinely can't determine whether this is a stale rebase or
            # an already-landed change; don't guess either way.
            return None

        logger.info(
            "[%s] proposes %r, older than the archive's %r. Adding an "
            "incomplete finding.",
            url,
            proposed_version,
            archive_version,
        )
        return Finding(
            "incomplete",
            f"The proposed version (`{proposed_version}`) is older than the "
            f"one already in the archive (`{archive_version}` in "
            f"{target_series}). Please rebase on top of the current archive "
            "version.",
        )

    # cmp == 0: same version already published. Tell "this MP's own change,
    # already uploaded" apart from "an unrelated upload reused the version
    # number" by comparing changelog content.
    pub = archive_lookup.published_source(
        lp_client.lp, package, target_series, archive_version
    )
    if pub is None:
        # ubuntu_versions() just confirmed a publication with this exact
        # version exists, so this is almost certainly a lookup hiccup, not a
        # genuine "no such publication" -- can't determine, don't cache.
        logger.debug(
            "check_stale_version: version %r already published but couldn't "
            "load the publication record; can't determine.",
            archive_version,
        )
        return None

    outcome = classify(archive_version, pub)
    # None here means the changelog fetch failed -- correctly propagates as
    # "couldn't determine" (see this function's own docstring/return-value
    # contract), same as every other lookup-failure path in this module.
    return outcome


def _classify_against_publication(
    url, lp_obj, lp_client, package, version, proposed_entry, pub
):
    """
    Given an existing publication `pub` of exactly `version`, compare its
    changelog content against `proposed_entry` and post the appropriate
    comment. Shared by check_stale_version's cmp==0 path (the current
    archive version matches the proposal) and its cmp<0 path (the proposal
    is older than the archive, but was itself published and later
    superseded -- design_journal.md #43).

    Returns "done" (matching content -- this MP's change already landed;
    posts its own closing comment), a Finding (different content -- a
    genuine version collision, needs a rebase with a new version number;
    contributed to the aggregated comment per design_journal.md #31),
    "pending" (matching content but the publication is too recent -- see
    _RECENT_UPLOAD_GRACE), or None if the changelog itself couldn't be
    fetched (retriable).
    """
    archive_text = archive_lookup.changelog_text(pub)
    if archive_text is None:
        logger.debug(
            "check_stale_version: couldn't fetch the archive changelog; "
            "can't determine."
        )
        return None

    archive_entry = _first_changelog_stanza_text(archive_text)
    same_content = _normalize_changelog_entry(
        proposed_entry
    ) == _normalize_changelog_entry(archive_entry)
    logger.debug(
        "check_stale_version: version %r already published (status=%s); "
        "content matches=%s",
        version,
        getattr(pub, "status", "?"),
        same_content,
    )

    if same_content:
        date_published = getattr(pub, "date_published", None)
        if date_published is not None:
            try:
                age = datetime.datetime.now(datetime.timezone.utc) - date_published
            except TypeError as e:
                logger.debug(
                    "check_stale_version: couldn't compute upload age (%s); "
                    "proceeding without deferring.",
                    e,
                )
                age = None
            if age is not None and age < _RECENT_UPLOAD_GRACE:
                logger.debug(
                    "check_stale_version: version %r published %s ago (< %s); "
                    "deferring -- git-ubuntu's importer may auto-close this MP "
                    "first once it catches up.",
                    version,
                    age,
                    _RECENT_UPLOAD_GRACE,
                )
                logger.info(
                    "[%s] version %r was published %s ago (< %s); deferring the "
                    "close comment in case git-ubuntu's importer auto-closes "
                    "this MP first.",
                    url,
                    version,
                    age,
                    _RECENT_UPLOAD_GRACE,
                )
                return "pending"

        comment = (
            "Thanks for your contribution! It seems that this change was already "
            f"uploaded to the archive as `{package} {version}`, so this "
            "Merge Proposal can be closed."
        )
        # Launchpad's comment renderer doesn't support Markdown link syntax (a
        # `[text](url)` would show up literally), but it does auto-linkify
        # bare URLs -- so the publication's page is appended as plain text.
        # A SourcePackagePublishingHistory has no web_link (confirmed live:
        # the real launchpadlib object simply lacks the attribute -- this
        # kind of record has no canonical page of its own in Launchpad's
        # object model), so the URL is constructed instead, same shape as
        # any '+source/<pkg>/<version>' release page. Live-verified against
        # a real MP (ipu6-drivers #503576): 200 OK, resolves to the intended
        # publication.
        pub_url = archive_lookup.published_source_url(package, version)
        comment += f"\n\n{pub_url}"

        # Rich-history diagnosis (#49, round one): the importer not closing
        # this MP usually correlates with it not grafting the MP's real git
        # commits either. Cheapest cause first: did the upload's .changes
        # carry the git-ubuntu Vcs keys at all?
        vcs_keys = archive_lookup.changes_file_vcs_keys(pub)
        if vcs_keys is not None and not vcs_keys.get("Vcs-Git-Commit"):
            # Upload carried no rich-history metadata: the MP's git history
            # was dropped and will need redoing next merge. Unlike #48's
            # bare closure housekeeping, this is worth saying ON the MP
            # (the sponsor learns the tooling, the contributor learns their
            # history didn't land) -- plus a terser operator ping so the
            # channel keeps the central tally of how often this happens.
            # Wording agreed with seb128 (design #49).
            logger.info(
                "[%s] version %r was uploaded without Vcs-Git-* headers; "
                "the MP's git history was dropped. Commenting and "
                "notifying (#49).",
                url,
                version,
            )
            lp_client.comment(
                lp_obj,
                "Thanks for your contribution! This change was already "
                f"uploaded to the archive as `{package} {version}` "
                f"({pub_url}), so this merge proposal can be closed.\n\n"
                "Note for sponsors: the .changes uploaded didn't include "
                "the needed Vcs headers for the git commits from this "
                "merge proposal to be imported in the official packaging "
                "history (https://ubuntu.com/project/docs/contributors/"
                "advanced/handle-git-ubuntu-uploads/"
                "#build-a-git-ubuntu-source-package-branch-for-uploading)",
            )
            notify.notify(
                f":warning: rich history dropped: {package} {version} was "
                f"uploaded without Vcs-Git-* headers -- {url} did not "
                "autoclose and the MP's git history was not imported."
            )
            return "done"
        if vcs_keys is not None and vcs_keys.get("Vcs-Git-Commit"):
            # Case B (#49): the upload DOES carry rich-history metadata.
            # Hash equality against the MP's tip proves nothing (a sponsor
            # legitimately stacks a fixup commit and uploads from their own
            # ~sponsor repo); the discriminating question is ancestry --
            # does the uploaded history CONTAIN the proposed commit?
            proposed_sha = getattr(
                getattr(lp_obj, "preview_diff", None), "source_revision_id", None
            )
            contained = None
            if proposed_sha:
                contained = git_history.commit_contains(
                    vcs_keys.get("Vcs-Git"),
                    vcs_keys["Vcs-Git-Commit"],
                    proposed_sha,
                    ref=vcs_keys.get("Vcs-Git-Ref"),
                )
            if contained is True:
                # B1: the contributor's history is preserved inside the
                # upload and the MP STILL didn't autoclose -- nobody did
                # anything wrong; this isolates an importer/autoclose bug.
                # Tell the contributor why their merged work shows an open
                # MP (only claiming the admins were notified when a webhook
                # actually exists to notify them through), and give the
                # git-ubuntu maintainers the diagnosis on the channel.
                # Wording agreed with seb128 (design #49).
                logger.info(
                    "[%s] upload's rich history contains the proposed "
                    "commit %r but the MP didn't autoclose; likely importer "
                    "bug. Commenting and notifying (#49 B1).",
                    url,
                    proposed_sha,
                )
                note = (
                    "Note: the upload correctly included the commits "
                    "proposed here, but git-ubuntu didn't autoclose this "
                    "merge proposal -- likely an import problem"
                )
                if notify.is_configured():
                    note += "; the git-ubuntu admins have been notified of the issue."
                else:
                    note += "."
                lp_client.comment(
                    lp_obj,
                    "Thanks for your contribution! This change was already "
                    f"uploaded to the archive as `{package} {version}` "
                    f"({pub_url}), so this merge proposal can be closed."
                    f"\n\n{note}",
                )
                notify.notify(
                    f":warning: importer issue: {package} {version} was "
                    "uploaded with rich history containing the commit "
                    f"proposed on {url} (Vcs-Git-Commit "
                    f"`{vcs_keys['Vcs-Git-Commit']}`), but the MP did not "
                    "autoclose -- likely a git-ubuntu import problem."
                )
                return "done"
            if contained is False:
                # B2: the upload carries valid rich history that does NOT
                # build on the contributor's commits -- their git history
                # was dropped even though the sponsor used the tooling.
                # Attribution deliberately neutral ("carried its own git
                # history"): we can see THAT the histories diverged, not
                # why. Wording agreed with seb128 (design #49).
                logger.info(
                    "[%s] upload's rich history (%r) does not contain the "
                    "proposed commit %r; the MP's git history was dropped. "
                    "Commenting and notifying (#49 B2).",
                    url,
                    vcs_keys["Vcs-Git-Commit"],
                    proposed_sha,
                )
                lp_client.comment(
                    lp_obj,
                    "Thanks for your contribution! This change was already "
                    f"uploaded to the archive as `{package} {version}` "
                    f"({pub_url}), so this merge proposal can be closed.\n\n"
                    "Note: the upload carried its own git history "
                    f"(Vcs-Git-Commit `{vcs_keys['Vcs-Git-Commit']}`), "
                    "which doesn't include the commit proposed here "
                    f"(`{proposed_sha}`). The git commits from this merge "
                    "proposal were therefore not imported into the official "
                    "packaging history. For sponsors: basing the upload "
                    "branch on the contributor's proposed commits (rather "
                    "than recreating the changes) preserves their git "
                    "history and lets the merge proposal autoclose.",
                )
                notify.notify(
                    f":warning: rich history diverged: {package} {version}'s "
                    "uploaded history (Vcs-Git-Commit "
                    f"`{vcs_keys['Vcs-Git-Commit']}`) does not contain the "
                    f"commit proposed on {url} (`{proposed_sha}`) -- the "
                    "MP's git history was not imported."
                )
                return "done"
            # contained is None (B3: unfetchable repo / undeterminable
            # ancestry): fall through to the undiagnosed #48 behavior.
        # vcs_keys is None (.changes unfetchable -- can't diagnose):
        # fall through to the undiagnosed #48 behavior below.
        # TODO: closing out stays comment/notification-only until the
        # underlying permission gap is resolved. Confirmed live 2026-07-05
        # (ipu6-drivers #503576): seb128's own account (and the bot's)
        # cannot set queue_status on a git-ubuntu MP, but a role account
        # with dedicated git-ubuntu access CAN, via the web UI -- so this
        # isn't a hard git-ubuntu API rejection
        # ([[git-ubuntu-mp-status-writes]] / design_journal.md #25), it's a
        # permission this bot's (and seb128's normal) account simply doesn't
        # hold, and it's being worked on at the Launchpad level. Once the
        # bot authenticates as an account with that access (see STATUS.md
        # item 2, the ~ubuntu-sponsoring-bot switch), revisit setting
        # queue_status="Merged" here directly.
        if notify.is_configured():
            # Design #43's agreed switch, built as #48: reaching this point
            # means git-ubuntu's importer failed to auto-close an MP whose
            # change is already in the archive -- an admin problem, not
            # contributor feedback. Stay fully silent on the MP and tell the
            # operators instead.
            logger.info(
                "[%s] version %r already published with matching content. "
                "Notifying operators (importer failed to auto-close; no MP "
                "comment -- design #48).",
                url,
                version,
            )
            notify.notify(
                f":warning: git-ubuntu's importer did not auto-close {url} "
                f"even though `{package} {version}` is already published "
                f"with matching content ({pub_url}). The MP needs to be "
                "closed manually."
            )
        else:
            logger.info(
                "[%s] version %r already published with matching content. "
                "Commenting (no status write -- see the code note above).",
                url,
                version,
            )
            lp_client.comment(lp_obj, comment)
        return "done"

    logger.info(
        "[%s] version %r already published with different content. Adding "
        "an incomplete finding.",
        url,
        version,
    )
    return Finding(
        "incomplete",
        f"An upload with the same version (`{version}`) but different "
        "content already exists in the archive. Your change needs to be "
        "rebased (with a new version number) and resubmitted.",
    )


# Pocket suffixes a changelog suite may carry: 'noble-proposed' etc.
_POCKET_SUFFIX_RE = re.compile(r"-(proposed|updates|security|backports)$")


def _stale_version_bug(url, bug, lp_client):
    """
    check_stale_version's bug-side path (#65): the proposed version /
    entry / package come from the newest usable attachment's new
    changelog stanza (attachments.review_target -- memoized, free after
    checks 8/10), and the target series from the stanza's own suite
    field ('pkg (1.2-3ubuntu1) noble; urgency=...') -- that's where an
    upload would actually go, so it's the authoritative signal; pocket
    suffixes are stripped. A suite that isn't a currently-known series
    (UNRELEASED, a typo, an EOL series) -> skip, don't guess.

    The verdict core is shared with the MP path (_stale_version_verdict);
    only the already-landed handling differs
    (_classify_against_publication_bug: no grace defer, no rich-history
    diagnosis).
    """
    target = attachments.review_target(bug)
    if target is None:
        return None
    if target is False:
        return False
    _attachment, text = target

    stanza = llm_reviewer._new_changelog_stanza(text)
    if not stanza:
        logger.debug("_stale_version_bug: no new changelog entry; skipping.")
        return False
    header = _CHANGELOG_HEADER_RE.match(stanza.splitlines()[0])
    if not header:
        logger.debug("_stale_version_bug: unparseable entry header; skipping.")
        return False
    package = header.group("pkg")
    proposed_version = header.group("version")
    suite = _POCKET_SUFFIX_RE.sub("", header.group("suite").lower())

    series_pairs = archive_lookup.supported_series_ordered(lp_client.lp)
    if series_pairs is None:
        logger.debug("_stale_version_bug: series lookup failed; can't determine.")
        return None
    if suite not in (name for name, _version in series_pairs):
        logger.debug(
            "_stale_version_bug: suite %r isn't a currently-known series; "
            "skipping.",
            suite,
        )
        return False

    def classify(version, pub):
        return _classify_against_publication_bug(
            url, bug, lp_client, package, version, stanza, pub
        )

    return _stale_version_verdict(
        url, lp_client, package, proposed_version, stanza, suite, classify
    )


def _classify_against_publication_bug(
    url, bug, lp_client, package, version, proposed_entry, pub
):
    """
    The bug-side sibling of _classify_against_publication: an existing
    publication of exactly `version` compared by changelog content.

    Matching content -> the attached debdiff's change already landed:
    comment + unsubscribe ~ubuntu-sponsors immediately and return "done".
    No grace defer, unlike the MP side (seb128, #65): the automation
    that would resolve the bug on its own -- Launchpad closing the task
    over the changelog's `LP: #nnn` -- only runs when the upload reaches
    the release pocket, which for an SRU can take weeks; nothing is
    racing us here, so unsubscribe directly. No rich-history diagnosis
    either (#49 is about git-ubuntu MP imports; a debdiff carries no git
    history to preserve).

    Different content -> the same-version-collision Finding. None -> the
    publication's changelog couldn't be fetched (retriable).
    """
    archive_text = archive_lookup.changelog_text(pub)
    if archive_text is None:
        logger.debug(
            "check_stale_version: couldn't fetch the archive changelog; "
            "can't determine."
        )
        return None

    archive_entry = _first_changelog_stanza_text(archive_text)
    same_content = _normalize_changelog_entry(
        proposed_entry
    ) == _normalize_changelog_entry(archive_entry)
    logger.debug(
        "check_stale_version: version %r already published (status=%s); "
        "content matches=%s",
        version,
        getattr(pub, "status", "?"),
        same_content,
    )

    if not same_content:
        logger.info(
            "[%s] version %r already published with different content. "
            "Adding an incomplete finding.",
            url,
            version,
        )
        return Finding(
            "incomplete",
            f"An upload with the same version (`{version}`) but different "
            "content already exists in the archive. Your change needs to be "
            "rebased (with a new version number) and resubmitted.",
        )

    pub_url = archive_lookup.published_source_url(package, version)
    logger.info(
        "[%s] version %r already published with matching content. "
        "Commenting and unsubscribing ~ubuntu-sponsors.",
        url,
        version,
    )
    lp_client.comment(
        bug,
        "Thanks for your contribution! It seems that this change was "
        f"already uploaded to the archive as `{package} {version}`, so "
        "there is nothing left to sponsor here. Cleaning up the queue by "
        f"unsubscribing ~ubuntu-sponsors.\n\n{pub_url}",
    )
    lp_client.unsubscribe_sponsors(bug)
    return "done"


# Task shape of a series-specific Ubuntu bug task: 'pkg (Ubuntu Noble)'.
_SERIES_TASK_RE = re.compile(r"^(?P<pkg>\S+) \(Ubuntu (?P<series>[A-Za-z]+)\)$")


def _series_evidence(bug, package, series_name, is_devel, devel_name):
    """
    Whether `bug` shows the fix for `package` is handled in `series_name`:
    its bug task is Fix Released/Committed, a linked (active) MP targets
    that series, or a patch attachment names it. Purely mechanical -- the
    'bug text says it's fixed there' case is the LLM's question, not ours.
    Raises on Launchpad read failures (callers map that to inconclusive).
    """
    task_name = f"{package} (Ubuntu)" if is_devel else f"{package} (Ubuntu {series_name.title()})"
    for task in bug.bug_tasks:
        if (task.bug_target_name or "").lower() == task_name.lower():
            if task.status in DONE_STATUSES:
                return True
    for mp in bug.linked_merge_proposals:
        if mp.queue_status in _INACTIVE_MP_STATUSES:
            continue
        target = getattr(mp, "target_git_path", "") or ""
        match = _TARGET_SERIES_RE.search(target)
        if not match:
            continue
        mp_series = match.group("series")
        if mp_series == series_name or (is_devel and mp_series in ("devel", devel_name)):
            return True
    for attachment in bug.attachments:
        title = (attachment.title or "").lower()
        if series_name.lower() in title:
            return True
    return False


def check_sru_newer_series(url, lp_obj, lp_client, llm):
    """
    Check 7: SRU 'fix newer series first' (design_journal.md #58).

    SRU policy (https://ubuntu.com/project/docs/SRU/reference/requirements)
    requires the development release to be fixed before a stable series
    gets the SRU -- and by extension every supported series newer than the
    target. We can't verify the fix actually landed in those series (it may
    have arrived via a refactoring or a newer upstream release), but the
    bug's own metadata is checkable: for each newer supported series the
    fix counts as handled when its bug task is Fix Released/Committed, a
    linked MP targets it, or a patch attachment names it (the common
    'one bug, three MPs' shape).

    When some newer series looks unhandled mechanically, the LLM gets one
    focused question -- does the bug text state the issue is already fixed
    there? (Task tables are often stale: updating them needs privileges
    most submitters don't have, while the description frequently documents
    'newer series ship version X which has the fix'.) Yes -> a soft
    'please update the bug tasks' note; no -> the full advisory. Both are
    question-tier/advisory: per seb128 this is never a reject reason, the
    ask is that the bug reflects reality and the fix lands newest-first.

    Returns a Finding ("question"/advisory), False (not an SRU, or all
    newer series handled), or None (a Launchpad/LLM lookup failed --
    inconclusive, retry next run; main.py persists no facts).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]

    # Decide "not an SRU" from the item itself before any Launchpad lookup:
    # the vast majority of items exit here for free.
    if resource_type == "branch_merge_proposal":
        package = _source_package_from_mp(lp_obj)
        if not package:
            logger.debug(
                "check_sru_newer_series: could not determine source package; "
                "skipping."
            )
            return False
        target = getattr(lp_obj, "target_git_path", "") or ""
        match = _TARGET_SERIES_RE.search(target)
        target_series = match.group("series") if match else None
        if not target_series or target_series == "devel":
            # Lands via the devel series (see _target_ubuntu_series).
            logger.debug("check_sru_newer_series: targets devel; not an SRU.")
            return False
        devel_name = archive_lookup.devel_codename(lp_client.lp)
        if devel_name is None:
            return None
        if target_series == devel_name:
            logger.debug("check_sru_newer_series: targets devel; not an SRU.")
            return False
        try:
            bugs = [bug for bug in lp_obj.bugs if _bug_targets_package(bug, package)]
        except Exception as e:
            logger.warning(
                "check_sru_newer_series: couldn't read the MP's linked bugs "
                "(%s); can't determine.",
                e,
            )
            return None
        if not bugs:
            # An SRU MP without a linked bug has bigger problems (the LLM
            # review flags that); nothing to evaluate here.
            logger.debug(
                "check_sru_newer_series: no linked bug targeting %r; skipping.",
                package,
            )
            return False
    elif resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        bugs = [bug]
        # SRU shape on the bug side: an open series-specific task. The
        # OLDEST open stable series is the deepest SRU target -- everything
        # newer than it must be covered. (A devel-series task nominated by
        # name rather than the plain 'pkg (Ubuntu)' would land in this list
        # too, harmlessly: nothing is newer than it, so `newer` comes out
        # empty below.)
        open_series = []
        package = None
        try:
            for task in bug.bug_tasks:
                match = _SERIES_TASK_RE.match(task.bug_target_name or "")
                if not match:
                    continue
                if task.status in CLOSED_STATUSES:
                    continue
                open_series.append(match.group("series").lower())
                package = package or match.group("pkg")
        except Exception as e:
            logger.warning(
                "check_sru_newer_series: couldn't read the bug's tasks (%s); "
                "can't determine.",
                e,
            )
            return None
        if not open_series:
            logger.debug(
                "check_sru_newer_series: no open series-specific task; not "
                "an SRU (or nothing left to do)."
            )
            return False
        devel_name = archive_lookup.devel_codename(lp_client.lp)
        if devel_name is None:
            return None
        target_series = None  # resolved against the ordered list below
    else:
        return False

    series_order = archive_lookup.supported_series_ordered(lp_client.lp)
    if series_order is None:
        return None
    series_names = [name for name, _version in series_order]
    series_versions = dict(series_order)
    if resource_type in ("bug", "bug_task"):
        candidates = [name for name in series_names if name in open_series]
        if not candidates:
            # The open series task(s) are for EOL series; a human call.
            logger.debug(
                "check_sru_newer_series: open series tasks (%s) aren't in "
                "the supported set; skipping.",
                open_series,
            )
            return False
        target_series = candidates[0]
    if target_series not in series_names:
        # SRU to an EOL/unknown series -- out of scope for this check.
        logger.debug(
            "check_sru_newer_series: target series %r not in the supported "
            "set; skipping.",
            target_series,
        )
        return False

    newer = series_names[series_names.index(target_series) + 1 :]
    unhandled = []
    try:
        for series_name in newer:
            is_devel = series_name == devel_name
            if not any(
                _series_evidence(bug, package, series_name, is_devel, devel_name)
                for bug in bugs
            ):
                unhandled.append(series_name)
    except Exception as e:
        logger.warning(
            "check_sru_newer_series: couldn't read the bug's tasks/MPs/"
            "attachments (%s); can't determine.",
            e,
        )
        return None

    logger.debug(
        "check_sru_newer_series: target=%r newer=%s unhandled=%s",
        target_series,
        newer,
        unhandled,
    )
    if not unhandled:
        return False

    # Escape hatch: the bug text often documents that newer series already
    # ship the fix even when nobody with the privileges updated the tasks.
    bug_text = "\n\n".join(
        f"{getattr(bug, 'title', '') or ''}\n{getattr(bug, 'description', '') or ''}"
        for bug in bugs
    )
    # Label each series with its release version: recent codenames postdate
    # any LLM's training data, so 'fixed in plucky 25.04+' is only readable
    # as covering resolute if the prompt says resolute is Ubuntu 26.04
    # (found on design #58's live probe).
    labeled = [
        f"{name} (Ubuntu {series_versions[name]})" for name in unhandled
    ]
    stated_fixed = llm.review_fixed_in_newer_series(bug_text, labeled)
    if stated_fixed is None:
        logger.debug(
            "check_sru_newer_series: LLM invocation failed; can't determine."
        )
        return None

    series_list = ", ".join(unhandled)
    if stated_fixed:
        # The bug text already documents the newer series as fixed/not
        # affected -- that's all a reviewer needs. Updating the task table
        # would require nominating series tasks, a restricted action most
        # contributors can't perform, so there is nothing useful to ask of
        # the submitter (seb128, live on bug #2148507).
        logger.info(
            "[%s] SRU to %s: bug text says %s already fixed; tasks don't "
            "reflect it but fixing that needs nomination rights. Skipping.",
            url,
            target_series,
            series_list,
        )
        return False

    logger.info(
        "[%s] SRU to %s with no sign the fix landed in %s first. Adding an "
        "advisory finding.",
        url,
        target_series,
        series_list,
    )
    return Finding(
        "question",
        f"This looks like an SRU targeting {target_series}, but there is no "
        f"indication that the issue is fixed in the newer Ubuntu series "
        f"({series_list}). Per the SRU requirements "
        "(https://ubuntu.com/project/docs/SRU/reference/requirements) the "
        "fix should land in the development release first, and ideally in "
        "the newer stable series too. If it is already fixed there, please "
        "update the bug tasks to reflect that; otherwise the fix should be "
        "uploaded to the newer series before this update.",
        kind="advisory",
    )


def _upstream_component(version):
    """The upstream part of a Debian version ('1:1.2-3ubuntu1' -> '1.2').
    None for a native version (no Debian revision separator) -- which is
    exactly the 'native package' exception to the patches-only rule
    (design #60; proper classification via debian/source/format is
    backlog)."""
    v = version.split(":", 1)[-1]
    if "-" not in v:
        return None
    return v.rsplit("-", 1)[0]


def _old_changelog_version_from_lines(lines):
    """The first changelog header visible in a changelog hunk's context/
    removed lines -- the entry the new stanza sits on top of. None when
    it isn't visible (short context)."""
    for line in lines:
        if not line or line[0] not in " -":
            continue
        match = _CHANGELOG_HEADER_RE.match(line[1:])
        if match:
            return match.group("version")
    return None


def _old_changelog_version(lp_obj):
    """The version of the changelog entry the MP's new stanza sits on top
    of, read from the debian/changelog hunk's context/removed lines. None
    when it isn't visible in the diff (short context) or there is no
    changelog hunk at all."""
    lines = _changelog_diff_lines(lp_obj)
    if not isinstance(lines, list):
        return None
    return _old_changelog_version_from_lines(lines)


def check_direct_source_edit(url, lp_obj, lp_client):
    """
    Check 8: upstream source files edited directly (design_journal.md #60).

    Ubuntu packaging policy requires changes to upstream files to be
    provided as patches under debian/patches, not as direct edits of the
    source tree (https://ubuntu.com/project/docs/contributors/bug-fix/
    apply-the-fix/) -- a common feedback case (trigger: nux MP #508190,
    which edited a .cpp directly and carried no changelog stanza at all).

    Detection: the preview diff touches files outside debian/. Silent
    skips, each erring toward not bouncing:
    - merge MPs (_is_merge_proposal): merges import upstream changes
      wholesale, that's their job;
    - a new upstream version (the stanza's upstream component differs
      from the entry below it in the diff): the diff naturally carries
      upstream changes;
    - native packages (no Debian revision in the version, from the new
      stanza when present, else from the version currently published in
      the target series): upstream and packaging aren't separate there.
      Proper nativeness classification (debian/source/format) is backlog.

    Bug-side parity (#62): the same rule applies to a debdiff attached to
    a bug -- see _direct_source_edit_bug. A plain patch (touching no
    debian/ file) is a normal contribution shape there and never bounced.

    Returns an incomplete Finding, False (clean/skipped), or None (diff,
    merge-detection, or archive lookup failed -- retriable).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        return _direct_source_edit_bug(url, bug)
    if resource_type != "branch_merge_proposal":
        return False

    text = diff_text(lp_obj)
    if text is None:
        logger.debug("check_direct_source_edit: diff unreadable; can't determine.")
        return None
    if text is False or not text.strip():
        # Missing/empty diff is check 4's (and #48's) problem, not ours.
        return False

    _debian_part, other_files = llm_reviewer._split_debian_diff(text)
    if not other_files:
        logger.debug("check_direct_source_edit: all changes under debian/; clean.")
        return False

    merge = _is_merge_proposal(lp_obj)
    if merge is None:
        return None
    if merge:
        logger.debug(
            "check_direct_source_edit: merge MP; upstream changes expected. Skipping."
        )
        return False

    stanza = llm_reviewer._new_changelog_stanza(text)
    proposed_version = None
    if stanza:
        header = _CHANGELOG_HEADER_RE.match(stanza.splitlines()[0])
        proposed_version = header.group("version") if header else None
    if proposed_version is None:
        # No (parseable) new changelog stanza -- the trigger MP's shape.
        # Nativeness has to come from what the archive currently publishes.
        package = _source_package_from_mp(lp_obj)
        if not package:
            logger.debug(
                "check_direct_source_edit: no version and no source package; "
                "skipping."
            )
            return False
        target_series = _target_ubuntu_series(lp_obj, lp_client.lp)
        if target_series is None:
            return None
        versions = archive_lookup.ubuntu_versions(
            lp_client.lp, package, series_names=[target_series]
        )
        if versions is None:
            return None
        proposed_version = _max_published_version(versions)
        if proposed_version is None:
            logger.debug(
                "check_direct_source_edit: nothing published in %s; can't "
                "classify native vs non-native. Skipping.",
                target_series,
            )
            return False

    upstream = _upstream_component(proposed_version)
    if upstream is None:
        logger.debug(
            "check_direct_source_edit: %r looks like a native package "
            "version; direct source edits are legitimate there. Skipping.",
            proposed_version,
        )
        return False

    old_version = _old_changelog_version(lp_obj)
    if (
        stanza
        and old_version is not None
        and _upstream_component(old_version) != upstream
    ):
        logger.debug(
            "check_direct_source_edit: upstream version changed (%r -> %r); "
            "the diff naturally carries upstream changes. Skipping.",
            old_version,
            proposed_version,
        )
        return False

    return _direct_edit_finding(url, "The changes edit", other_files)


def _direct_edit_finding(url, intro, other_files):
    """The Check 8 bounce, shared by the MP and bug paths (#60/#62).
    `intro` opens the sentence ('The changes edit' / 'The attached
    debdiff edits')."""
    shown = ", ".join(f"`{p}`" for p in other_files[:5])
    more = f" (and {len(other_files) - 5} more)" if len(other_files) > 5 else ""
    logger.info(
        "[%s] upstream files edited directly (%s%s). Adding an incomplete "
        "finding.",
        url,
        shown,
        more,
    )
    return Finding(
        "incomplete",
        f"{intro} upstream source files directly ({shown}{more}). "
        "Changes to upstream code must be provided as patches under "
        "`debian/patches` instead, so they stay visible and survive new "
        "upstream versions -- see "
        "https://ubuntu.com/project/docs/contributors/bug-fix/apply-the-fix/",
    )


def _direct_source_edit_bug(url, bug):
    """
    Check 8's bug-side path (#62): the direct-source-edit rule applied to
    the newest usable patch/debdiff attachment (attachments.review_target).

    Only *debdiffs* are judged -- a diff that touches debian/ files is a
    package-level change and must carry upstream edits as debian/patches
    patches. A plain patch (no debian/ file at all) is a normal
    contribution shape -- it's expected to BECOME a debian/patches patch
    at upload time -- and is never bounced.

    Exemptions mirror the MP path where they apply: merge bugs (title,
    same signal as _is_merge_proposal's fallback), new upstream versions,
    native packages. Unlike the MP path there is no archive fallback for
    nativeness: a debdiff without a parseable new changelog stanza is a
    shape we haven't seen (debdiffs diff two source packages, the stanza
    is inherently there) -- skip rather than guess.

    Returns an incomplete Finding, False, or None (attachment listing/
    fetch failed -- retriable).
    """
    if _MERGE_BUG_TITLE_RE.match(getattr(bug, "title", "") or ""):
        logger.debug("_direct_source_edit_bug: merge bug; skipping.")
        return False

    target = attachments.review_target(bug)
    if target is None:
        return None
    if target is False:
        return False
    attachment, text = target

    info = attachments.classify_diff(text)
    if not info["debian_paths"]:
        logger.debug(
            "_direct_source_edit_bug: %r touches no debian/ file; a plain "
            "patch is a normal contribution shape. Skipping.",
            getattr(attachment, "title", "?"),
        )
        return False
    if not info["other_paths"]:
        logger.debug("_direct_source_edit_bug: all changes under debian/; clean.")
        return False

    stanza = llm_reviewer._new_changelog_stanza(text)
    proposed_version = None
    if stanza:
        header = _CHANGELOG_HEADER_RE.match(stanza.splitlines()[0])
        proposed_version = header.group("version") if header else None
    if proposed_version is None:
        logger.debug(
            "_direct_source_edit_bug: debdiff has no parseable new "
            "changelog stanza; unexpected shape, skipping."
        )
        return False

    upstream = _upstream_component(proposed_version)
    if upstream is None:
        logger.debug(
            "_direct_source_edit_bug: %r looks native; skipping.",
            proposed_version,
        )
        return False

    old_version = None
    if info["changelog_lines"]:
        old_version = _old_changelog_version_from_lines(info["changelog_lines"])
    if old_version is not None and _upstream_component(old_version) != upstream:
        logger.debug(
            "_direct_source_edit_bug: upstream version changed (%r -> %r); "
            "skipping.",
            old_version,
            proposed_version,
        )
        return False

    return _direct_edit_finding(
        url, "The attached debdiff edits", info["other_paths"]
    )


_LP_BUG_REF_RE = re.compile(r"LP:\s*#(\d+)", re.IGNORECASE)


def _mp_bug_numbers(lp_obj):
    """
    Bug numbers to suggest in a changelog `LP: #nnn` closer: the MP's
    linked bugs first, else `LP: #nnn` references found in the MP's
    commit message / description. Empty list when neither has any --
    changes don't always come with a bug and that's fine (seb128,
    design_journal.md #61). None when the linked-bugs API call itself
    failed (retriable, per the checks.py None/False convention).
    """
    try:
        numbers = [str(bug.id) for bug in lp_obj.bugs if getattr(bug, "id", None)]
    except Exception as e:
        logger.debug("_mp_bug_numbers: couldn't read linked bugs (%s).", e)
        return None
    if numbers:
        return numbers
    text = "\n".join(
        getattr(lp_obj, field, "") or ""
        for field in ("commit_message", "description")
    )
    return list(dict.fromkeys(_LP_BUG_REF_RE.findall(text)))


def check_missing_changelog_stanza(url, lp_obj, lp_client):
    """
    Check 9: the MP doesn't add a debian/changelog entry (design_journal.md
    #61). Every upload needs a new changelog entry with an incremented
    version; the trigger shape (nux MP #508190) touched only source files
    and would otherwise get feedback about everything except the missing
    changelog.

    Only fires when debian/changelog is completely absent from the diff.
    A touched changelog with no *added* header line (e.g. appending bullet
    points to an existing UNRELEASED entry) is deliberately treated as
    clean -- erring toward not bouncing.

    Silent skips: missing/empty diff (check 4's problem), merge MPs
    (reviewed on their own terms and always end with a reconstruct-
    changelog step). Returns an incomplete Finding, False, or None (diff
    fetch, merge detection, or linked-bugs lookup failed -- retriable).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type != "branch_merge_proposal":
        return False

    text = diff_text(lp_obj)
    if text is None:
        logger.debug("check_missing_changelog_stanza: diff unreadable.")
        return None
    if text is False or not text.strip():
        return False

    lines = _changelog_diff_lines(lp_obj)
    if lines is not False:
        logger.debug("check_missing_changelog_stanza: changelog touched; clean.")
        return False

    merge = _is_merge_proposal(lp_obj)
    if merge is None:
        return None
    if merge:
        logger.debug("check_missing_changelog_stanza: merge MP; skipping.")
        return False

    bug_numbers = _mp_bug_numbers(lp_obj)
    if bug_numbers is None:
        return None
    if bug_numbers:
        refs = ", ".join(f"`LP: #{n}`" for n in bug_numbers)
        plural = len(bug_numbers) > 1
        lp_clause = (
            f", including {'' if plural else 'an '}{refs} "
            f"reference{'s' if plural else ''} so the "
            f"bug{'s are' if plural else ' is'} closed when the package "
            "is published"
        )
    else:
        lp_clause = ""
    logger.info(
        "[%s] no debian/changelog entry in the diff. Adding an incomplete "
        "finding.",
        url,
    )
    return Finding(
        "incomplete",
        "The merge proposal doesn't add a `debian/changelog` entry "
        "describing the changes. Please add a new changelog entry with an "
        f"incremented version number{lp_clause} -- see "
        "https://ubuntu.com/project/docs/contributors/updating/"
        "commit-changes/#write-the-changelog-entry",
    )


def check_patch_not_debdiff(url, lp_obj, lp_client):
    """
    Check 10: a plain code patch attached to a bug isn't sponsorable as-is
    (design_journal.md #64). A patch against the upstream source (including
    git format-patch output) is a nice step, but sponsors won't turn it
    into a package update themselves -- that means writing the changelog,
    doing the packaging change, and verifying the patch applies (seb128).
    Ask for a debdiff instead.

    Detection: the newest usable diff attachment (attachments.review_target,
    memoized -- free after Check 8) touches no debian/ file at all. A diff
    that does touch debian/ is already debdiff-shaped and is judged by
    Check 8 / the changelog bug-reference check on its merits.

    Silent skips, each erring toward not bouncing: merge bugs, sync
    requests (no package update expected from the submitter), needs-
    packaging bugs (no existing package to debdiff against, #50
    territory), and bugs with an active linked MP (the review lives
    there; the attachment is supplementary). Returns an incomplete
    Finding, False, or None (attachment fetch or MP listing failed --
    retriable).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    if resource_type not in ("bug", "bug_task"):
        return False
    bug = lp_obj.bug if resource_type == "bug_task" else lp_obj

    if _MERGE_BUG_TITLE_RE.match(getattr(bug, "title", "") or ""):
        logger.debug("check_patch_not_debdiff: merge bug; skipping.")
        return False
    if llm_reviewer._is_sync(
        getattr(bug, "title", ""), getattr(bug, "description", "")
    ):
        logger.debug("check_patch_not_debdiff: sync request; skipping.")
        return False
    if _is_needs_packaging(bug):
        logger.debug("check_patch_not_debdiff: needs-packaging bug; skipping.")
        return False
    try:
        has_active_mp = any(
            mp.queue_status not in _INACTIVE_MP_STATUSES
            for mp in bug.linked_merge_proposals
        )
    except Exception as e:
        logger.debug(
            "check_patch_not_debdiff: couldn't read linked MPs (%s); "
            "can't determine.",
            e,
        )
        return None
    if has_active_mp:
        logger.debug(
            "check_patch_not_debdiff: active linked MP; the review lives "
            "there. Skipping."
        )
        return False

    target = attachments.review_target(bug)
    if target is None:
        return None
    if target is False:
        return False
    attachment, text = target

    info = attachments.classify_diff(text)
    if info["debian_paths"]:
        logger.debug(
            "check_patch_not_debdiff: %r touches debian/; debdiff-shaped, "
            "judged elsewhere.",
            getattr(attachment, "title", "?"),
        )
        return False
    if not info["other_paths"]:
        logger.debug(
            "check_patch_not_debdiff: no file paths recognized in %r; "
            "skipping.",
            getattr(attachment, "title", "?"),
        )
        return False

    logger.info(
        "[%s] attachment %r is a plain code patch, not a debdiff. Adding "
        "an incomplete finding.",
        url,
        getattr(attachment, "title", "?"),
    )
    return Finding(
        "incomplete",
        "The attachment is a plain code patch. Thank you for working on a "
        "fix! To be ready for sponsoring it needs to be turned into a "
        "source package update (debdiff): include the patch under "
        "`debian/patches` and add a new `debian/changelog` entry with an "
        "incremented version number describing the change -- see "
        "https://ubuntu.com/project/docs/contributors/updating/"
        "work-with-debian-patches/",
    )
