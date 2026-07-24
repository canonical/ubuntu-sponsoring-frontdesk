"""
Build a fingerprint of the *contributor-controlled* signals our triage keys on.

This is the sponsoring-bot equivalent of pmtriage's ``facts.yaml``: we snapshot
the inputs to our decisions and re-run triage only when they change. It
deliberately EXCLUDES any field the bot itself mutates (notably a merge
proposal's ``queue_status``, which we set to 'Needs fixing'/'Merged'). If we
fingerprinted those, the bot would see its own action on the next run, think the
contributor changed something, and re-trigger forever -- nagging the submitter
on every cron tick. Fingerprint what the contributor controls; never what the
bot writes.

One deliberate exception to "contributor-controlled" (design_journal.md #37):
for MPs the fingerprint also includes the package's current archive version.
It's third-party state, not contributor state, but check_stale_version's
verdict depends on it -- without it in the fingerprint, an archive upload
landing after an item's first triage (someone else's newer version, or this
very change getting sponsored) would never re-trigger triage, freezing the
staleness check at its first-look result forever. The original exclusion
rationale was about fields the BOT mutates (feedback loops), which archive
state is not.
"""

import hashlib
import logging

logger = logging.getLogger(__name__)


def _digest(text):
    """Short stable digest of a text field. The fingerprint needs to know
    *whether* content changed, not what it is -- storing full comment
    threads or linked-bug descriptions in every state.db snapshot would
    bloat it for zero extra sensitivity."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _comments_digest(bug):
    """Digest of the bug's non-service-account comment texts, or None when
    the read failed (retriable -- main treats it as inconclusive).

    Comments feed real decisions (`_has_proposed_source_link`'s PPA/git
    link exemption #50, sync detail in follow-ups, the sweep's response
    judgment), so a new or edited comment must re-trigger triage.
    Service-account comments (the bot's own included) are excluded,
    mirroring how the checks themselves ignore them -- the bot commenting
    must not look like a contributor change on the next run (the same
    feedback-loop rule the module docstring states for queue_status).
    """
    from checks import SERVICE_ACCOUNTS

    try:
        texts = []
        for message in bug.messages:
            owner = getattr(message, "owner_link", "") or ""
            if owner.rsplit("/", 1)[-1] in SERVICE_ACCOUNTS:
                continue
            texts.append(getattr(message, "content", "") or "")
    except Exception as e:
        logger.warning("Could not read bug comments for the fingerprint: %s", e)
        return None
    return f"{len(texts)}:{_digest(chr(0).join(texts))}"


def _linked_bug_signals(lp_obj):
    """Per-linked-bug fingerprint entries for an MP, or None when the read
    failed (retriable). Covers exactly what the MP-side checks consume
    from linked bugs: title (merge-bug detection), description (the SRU
    template review in triage_mp), task statuses and attachment links
    (check_sru_newer_series' series evidence). Fixing a linked bug's SRU
    template -- or attaching a debdiff there -- must re-trigger the MP's
    triage even when the MP itself is untouched."""
    try:
        entries = []
        for bug in lp_obj.bugs:
            statuses = ",".join(sorted(f"{t.bug_target_name}:{t.status}" for t in bug.bug_tasks))
            attachment_links = ",".join(
                sorted(getattr(a, "self_link", "") or "" for a in getattr(bug, "attachments", []))
            )
            entries.append(
                "{}|{}|{}|{}".format(
                    getattr(bug, "self_link", "") or "",
                    _digest(
                        (getattr(bug, "title", "") or "")
                        + "\0"
                        + (getattr(bug, "description", "") or "")
                    ),
                    statuses,
                    attachment_links,
                )
            )
    except Exception as e:
        logger.warning("Could not read the MP's linked bugs for the fingerprint: %s", e)
        return None
    return sorted(entries)


def _archive_version(lp, lp_obj):
    """The package's currently published version in the series this MP
    actually targets (see checks._target_ubuntu_series -- the current devel
    series for an 'ubuntu/devel' target, or the specific stable series for
    an SRU targeting 'ubuntu/<series>-devel'), highest across every pocket
    published for it (design_journal.md #41 -- mirrors check_stale_version
    exactly, so the fingerprint tracks the same archive state the check
    itself reacts to). '' if nothing is published (a stable fact); None if
    a lookup failed (retriable -- main treats it as inconclusive, so it is
    never persisted and never silently equals a stored value)."""
    # Imported here, not at module top: checks imports nothing from facts
    # today, but keeping this one-way keeps any future cycle impossible.
    import archive_lookup
    from checks import (
        _max_published_version,
        _source_package_from_mp,
        _target_ubuntu_series,
    )

    package = _source_package_from_mp(lp_obj)
    if not package:
        # Structural, not a lookup failure: no package in the URL means
        # check_stale_version can't run either -- a stable fact.
        return ""
    target_series = _target_ubuntu_series(lp_obj, lp)
    if not target_series:
        return None
    versions = archive_lookup.ubuntu_versions(lp, package, series_names=[target_series])
    if versions is None:
        return None
    return _max_published_version(versions) or ""


def build_facts(lp_obj, lp=None):
    """Return a dict snapshot of the signals our triage keys on for lp_obj.

    ``lp`` (the launchpadlib root) enables the archive-version part of the
    fingerprint for MPs; without it that field is omitted entirely (some
    tests exercise other fields in isolation).
    """
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    facts = {"resource_type": resource_type}

    if resource_type == "branch_merge_proposal":
        facts["target_git_path"] = getattr(lp_obj, "target_git_path", "") or ""
        # The source branch name drives merge-vs-fix classification
        # (checks._is_merge_proposal); renaming/repointing it must
        # re-trigger triage.
        facts["source_git_path"] = getattr(lp_obj, "source_git_path", "") or ""
        # Linked-bug content the MP checks consume (SRU template text,
        # task statuses, attachments) -- see _linked_bug_signals.
        facts["linked_bugs"] = _linked_bug_signals(lp_obj)
        if lp is not None:
            facts["archive_version"] = _archive_version(lp, lp_obj)
        # A fresh push generates a new preview_diff with a new self_link, so the
        # link doubles as "did the contributor push new code?". Conflict state
        # lives on the diff (`conflicts` string), not on the MP itself.
        diff = getattr(lp_obj, "preview_diff", None)
        if diff is not None:
            facts["has_conflicts"] = bool((getattr(diff, "conflicts", "") or "").strip())
            facts["diff_id"] = getattr(diff, "self_link", None)
            facts["diff_lines_count"] = getattr(diff, "diff_lines_count", None)
        else:
            facts["has_conflicts"] = False
            facts["diff_id"] = None
            facts["diff_lines_count"] = None

    elif resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        # The title drives sync/merge/needs-packaging detection (_is_sync,
        # _parse_sync_title, the merge-bug title check); retitling must
        # re-trigger triage.
        facts["title"] = getattr(bug, "title", "") or ""
        # The description is what the LLM review reads; if the submitter edits the
        # SRU/sync template, this changes and we re-review.
        facts["description"] = getattr(bug, "description", "") or ""
        # Non-service-account comment content (count + digest); a PPA link
        # or new detail added in a comment must re-trigger triage.
        facts["comments_digest"] = _comments_digest(bug)
        facts["tags"] = sorted(getattr(bug, "tags", []) or [])
        # Task statuses are set by humans (Fix Released/Committed), not the bot,
        # so they are a legitimate change signal for the administrative check.
        facts["task_statuses"] = sorted(f"{t.bug_target_name}:{t.status}" for t in bug.bug_tasks)
        # What there is to sponsor (check_nothing_to_sponsor) changes when a
        # patch gets attached or an MP gets linked/reviewed -- without these
        # in the fingerprint, a bug closed as "nothing to sponsor" would stay
        # skipped at the facts-unchanged gate even after the contributor
        # attaches a fix and re-subscribes ~ubuntu-sponsors.
        facts["attachments"] = sorted(
            getattr(a, "self_link", "") or "" for a in getattr(bug, "attachments", [])
        )
        facts["linked_mps"] = sorted(
            "{}|{}|{}".format(
                mp.self_link,
                mp.queue_status,
                ",".join(
                    sorted(
                        f"{v.reviewer_link.rsplit('/', 1)[-1]}:{v.comment_link is not None}"
                        for v in mp.votes
                    )
                ),
            )
            for mp in getattr(bug, "linked_merge_proposals", [])
        )

    return facts


def apply_task_status_changes(facts_snapshot, changes):
    """
    Return a copy of a bug facts snapshot with task status changes applied
    (``{bug_target_name: new_status}``).

    Used after the bot transitions tasks itself (e.g. to Incomplete): we persist
    the post-write state so the bot's own action is not mistaken for a
    contributor change on the next run. A no-op when there is nothing to change.
    """
    if not changes or "task_statuses" not in facts_snapshot:
        return facts_snapshot
    updated = []
    for entry in facts_snapshot["task_statuses"]:
        target, sep, status = entry.partition(":")
        if target in changes:
            status = changes[target]
        updated.append(f"{target}{sep or ':'}{status}")
    snapshot = dict(facts_snapshot)
    snapshot["task_statuses"] = sorted(updated)
    return snapshot
