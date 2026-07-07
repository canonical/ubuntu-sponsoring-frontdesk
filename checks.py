import datetime
import logging
import re
from typing import NamedTuple

import archive_lookup
import llm_reviewer

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
    """

    tier: str
    message: str


def render_findings_comment(findings):
    """
    Render the one aggregated comment for a pass's fired findings
    (design_journal.md #31): a single intro, a "needs fixing" section, an
    optional "nice to have" section, and a single closing line -- instead
    of one comment per check. Individual finding messages are bullets and
    deliberately carry no greeting/sign-off of their own.
    """
    incomplete = [f for f in findings if f.tier == "incomplete"]
    question = [f for f in findings if f.tier == "question"]

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
    if question:
        parts.append(
            "Nice to have (non-blocking -- none of these block the upload, "
            "but you may want to address them now, before a sponsor reviews "
            "this, or in a future contribution):\n\n" + bullets(question)
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
# forgot to tick Launchpad's "patch" flag.
_PATCH_FILENAME_RE = re.compile(r"\.(debdiff|diff|patch)(\.gz)?$", re.IGNORECASE)

# Linked MPs in these states are no longer a review venue.
_INACTIVE_MP_STATUSES = ("Rejected", "Superseded")


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

    TODO(design_journal.md #30, backlog): once the grace period elapses this
    is exactly the kind of thing a service-maintainer notification (Matrix/
    Mattermost) should fire for -- a human-actionable Launchpad-side problem,
    not something the contributor can fix. Not built yet (needs a new
    notification backend/API token); for now this only logs.

    NOT memoized: up to four checks (conflicts, empty diff, changelog bug
    reference, stale version) can each hit this same missing-diff path for
    the same MP within one `triage_url` call, so the WARNING below prints up
    to four times per stuck MP per run (confirmed live, MP #503471) --
    `functools.lru_cache` was tried and reverted, since real launchpadlib
    `Entry` objects aren't hashable (`TypeError: unhashable type: 'Entry'`,
    caught live before this shipped) and an `id(lp_obj)`-keyed dict risks
    incorrect cache hits from id reuse after garbage collection across a
    long `--all` run. Repeated printing is harmless today; revisit if/when
    the maintainer-notification TODO above is built (that one genuinely
    should fire once per MP, not once per check) -- e.g. by threading an
    explicit per-triage_url cache down from main.py instead.
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
    numbers = set()
    for block in _LP_BUG_BLOCK_RE.findall("\n".join(entry_lines)):
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
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
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

    mismatched = []
    lookup_failed = False
    for number in sorted(bug_numbers):
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
        f"The bug reference(s) in the changelog ({bug_list}) don't appear to "
        f"be reported against `{package}`. Please double-check the bug "
        "number(s) are correct.",
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

    Applies to merge_proposals only for now -- the same comparison is
    useful for a bug's attached patch too, but needs adapting to read the
    proposed version out of the patch instead of an MP diff (see
    STATUS.md backlog).

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
    too recent -- deliberately deferred), False (1, or a structural
    non-applicability), or None (a lookup/fetch failure) -- unlike every
    other check in this module, a fired result here can mean several
    different outcomes, so the caller (main.py) needs to know which.
    "pending" and None are both outcomes the caller must NOT persist a
    facts snapshot for: nothing about the MP itself changes while we wait
    (out the grace period, or for the next retry), so persisting facts
    here would make the top-level "facts unchanged -> skip" gate
    permanently skip re-checking this URL.
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
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
            outcome = _classify_against_publication(
                url,
                lp_obj,
                lp_client,
                package,
                proposed_version,
                proposed_entry,
                historical_pub,
            )
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

    outcome = _classify_against_publication(
        url, lp_obj, lp_client, package, archive_version, proposed_entry, pub
    )
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
        logger.info(
            "[%s] version %r already published with matching content. "
            "Commenting (no status write -- see the code note below).",
            url,
            version,
        )
        # TODO: this stays comment-only until the underlying permission gap
        # is resolved. Confirmed live 2026-07-05 (ipu6-drivers #503576):
        # seb128's own account (and the bot's) cannot set queue_status on a
        # git-ubuntu MP, but a role account with dedicated git-ubuntu access
        # CAN, via the web UI -- so this isn't a hard git-ubuntu API
        # rejection ([[git-ubuntu-mp-status-writes]] / design_journal.md
        # #25), it's a permission this bot's (and seb128's normal) account
        # simply doesn't hold, and it's being worked on at the Launchpad
        # level. Once the bot authenticates as an account with that access
        # (see STATUS.md item 2, the ~ubuntu-sponsoring-bot switch), revisit
        # setting queue_status="Merged" here directly.
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
