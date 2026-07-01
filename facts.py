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
"""


def build_facts(lp_obj):
    """Return a dict snapshot of the contributor-controlled signals for lp_obj."""
    resource_type = lp_obj.resource_type_link.split("#")[-1]
    facts = {"resource_type": resource_type}

    if resource_type == "branch_merge_proposal":
        facts["target_git_path"] = getattr(lp_obj, "target_git_path", "") or ""
        # A fresh push generates a new preview_diff with a new self_link, so the
        # link doubles as "did the contributor push new code?". Conflict state
        # lives on the diff (`conflicts` string), not on the MP itself.
        diff = getattr(lp_obj, "preview_diff", None)
        if diff is not None:
            facts["has_conflicts"] = bool(
                (getattr(diff, "conflicts", "") or "").strip()
            )
            facts["diff_id"] = getattr(diff, "self_link", None)
            facts["diff_lines_count"] = getattr(diff, "diff_lines_count", None)
        else:
            facts["has_conflicts"] = False
            facts["diff_id"] = None
            facts["diff_lines_count"] = None

    elif resource_type in ("bug", "bug_task"):
        bug = lp_obj.bug if resource_type == "bug_task" else lp_obj
        # The description is what the LLM review reads; if the submitter edits the
        # SRU/sync template, this changes and we re-review.
        facts["description"] = getattr(bug, "description", "") or ""
        facts["tags"] = sorted(getattr(bug, "tags", []) or [])
        # Task statuses are set by humans (Fix Released/Committed), not the bot,
        # so they are a legitimate change signal for the administrative check.
        facts["task_statuses"] = sorted(
            f"{t.bug_target_name}:{t.status}" for t in bug.bug_tasks
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
