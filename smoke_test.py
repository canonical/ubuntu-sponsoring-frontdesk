"""
Read-only smoke test against real Launchpad.

Loads one bug or merge-proposal URL and verifies that every attribute the bot
relies on actually exists in production, then prints what build_facts() and the
dedup read-path return. It makes NO writes: it never calls the checks (which
post comments) and the client is in dry-run mode, so even the write methods
would refuse. Run this once before any unattended use to confirm the
launchpadlib field names match what the code assumes.

Usage:
    python3 smoke_test.py <launchpad-bug-or-mp-url>
"""

import sys

import facts
from launchpad_client import LPClient

# Attributes read off the top-level object per resource type.
EXPECTED = {
    # MP conflict state lives on preview_diff.conflicts, not on the MP itself.
    "branch_merge_proposal": [
        "target_git_path",
        "preview_diff",
        "queue_status",
        "self_link",
        "all_comments",
    ],
    "bug": ["description", "tags", "bug_tasks", "messages", "self_link"],
}

_SENTINEL = object()


def check(obj, name):
    val = getattr(obj, name, _SENTINEL)
    present = val is not _SENTINEL
    shown = "<MISSING>" if not present else repr(val)[:80]
    print(f"  [{'ok  ' if present else 'MISS'}] {name}: {shown}")
    return present


def main():
    if len(sys.argv) != 2:
        print("usage: python3 smoke_test.py <launchpad-bug-or-mp-url>")
        sys.exit(2)
    url = sys.argv[1]

    # dry-run client: cannot write even if asked.
    lp_client = LPClient(mode="dry-run")
    print(f"bot identity (lp.me.self_link): {lp_client.lp.me.self_link}")

    obj = lp_client.load_url(url)
    rtype = obj.resource_type_link.split("#")[-1]
    print(f"\nLoaded {url}\n  resource_type = {rtype}")

    if rtype == "bug_task":
        obj = obj.bug
        rtype = "bug"

    print("\nTop-level attributes the bot reads:")
    all_ok = all(check(obj, n) for n in EXPECTED.get(rtype, []))

    if rtype == "branch_merge_proposal":
        diff = getattr(obj, "preview_diff", None)
        if diff is not None:
            print("\n  preview_diff sub-attributes:")
            all_ok &= check(diff, "self_link")
            all_ok &= check(diff, "diff_lines_count")
            all_ok &= check(diff, "conflicts")
        else:
            print("\n  preview_diff is None (no diff yet)")
        print("\n  sample all_comments[].author_link / message_body:")
        for c in list(getattr(obj, "all_comments", []))[:3]:
            check(c, "author_link")
            check(c, "message_body")

    elif rtype == "bug":
        print("\n  sample bug_tasks[].bug_target_name / status:")
        for t in list(getattr(obj, "bug_tasks", []))[:8]:
            check(t, "bug_target_name")
            check(t, "status")
        print("\n  sample messages[].owner_link / content:")
        for m in list(getattr(obj, "messages", []))[:3]:
            check(m, "owner_link")
            check(m, "content")

    print("\nbuild_facts() output:")
    print("  ", facts.build_facts(obj))

    # Exercises the dedup read path against real data (read-only, no write).
    print("\nDedup read-path (looks for an identical existing bot comment):")
    dummy = "<<smoke-test marker that should match nothing>>"
    print("  _already_posted(dummy) =", lp_client._already_posted(obj, dummy, rtype))

    print()
    print(
        "ALL EXPECTED ATTRIBUTES PRESENT"
        if all_ok
        else "SOME ATTRIBUTES MISSING -- see [MISS] lines above"
    )


if __name__ == "__main__":
    main()
