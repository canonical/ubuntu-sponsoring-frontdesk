"""MP deterministic checks: conflicts (via preview_diff) and target branch."""

import datetime
import types

from fakes import FakeBug, FakeBugRef, FakeDiff, FakeMP, FakeTask

import archive_lookup
import checks
import facts


class _LP:
    def __init__(self, bugs=None):
        self.comments = []
        self.votes = []
        self.lp = types.SimpleNamespace(bugs=bugs or {})

    def comment(self, obj, message, vote=None):
        self.comments.append(message)
        self.votes.append(vote)


def test_conflicts_detected_from_preview_diff():
    mp = FakeMP(diff=FakeDiff("/d/1", 100, conflicts="foo.c\nbar.c"))
    lp = _LP()
    finding = checks.check_mp_conflicts("url", mp, lp)
    assert finding.tier == "incomplete"
    assert "merge conflicts" in finding.message
    assert lp.comments == []  # findings are aggregated by main.py, not posted here


def test_no_conflicts_when_diff_conflicts_empty():
    mp = FakeMP(diff=FakeDiff("/d/1", 100, conflicts=""))
    assert checks.check_mp_conflicts("url", mp, _LP()) is False


class _MPWithRaisingPreviewDiff:
    """A launchpadlib `preview_diff` fetch can fail on a network/API error
    (it's lazily fetched on first access) -- distinct from the attribute
    genuinely not existing, which `getattr(..., default)` already handles.
    `getattr` only swallows AttributeError, so this must propagate to a
    try/except inside the check, not the caller."""

    resource_type_link = FakeMP.resource_type_link

    @property
    def preview_diff(self):
        raise RuntimeError("network blip fetching preview_diff")


def test_mp_conflicts_returns_none_when_preview_diff_unreadable():
    lp = _LP()
    assert checks.check_mp_conflicts("url", _MPWithRaisingPreviewDiff(), lp) is None
    assert lp.comments == []


def test_empty_diff_fires_on_zero_lines():
    mp = FakeMP(diff=FakeDiff("/d/1", 0))
    lp = _LP()
    assert checks.check_empty_diff("url", mp, lp) is True
    assert lp.votes == [None]


def test_empty_diff_false_when_diff_has_content():
    mp = FakeMP(diff=FakeDiff("/d/1", 100))
    assert checks.check_empty_diff("url", mp, _LP()) is False


def test_empty_diff_returns_none_when_preview_diff_unreadable():
    lp = _LP()
    assert checks.check_empty_diff("url", _MPWithRaisingPreviewDiff(), lp) is None
    assert lp.comments == []


# --- missing (not-yet-generated / stuck) preview_diff -----------------------
# preview_diff can be cleanly None (no exception) when Launchpad hasn't
# produced a diff -- normal for a few minutes after an MP is opened or a new
# commit is pushed, but live data (MP #503471) showed this can also mean a
# stuck Launchpad job that never gets retried. checks._diff_missing_is_still_
# generating distinguishes the two via the MP's date_created.


def test_mp_conflicts_returns_none_when_diff_missing_and_mp_is_fresh():
    mp = FakeMP(no_diff=True, date_created=datetime.datetime.now(datetime.timezone.utc))
    assert checks.check_mp_conflicts("url", mp, _LP()) is None


def test_mp_conflicts_returns_false_when_diff_missing_past_grace_period():
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    mp = FakeMP(no_diff=True, date_created=old)
    assert checks.check_mp_conflicts("url", mp, _LP()) is False


def test_empty_diff_returns_none_when_diff_missing_and_mp_is_fresh():
    mp = FakeMP(no_diff=True, date_created=datetime.datetime.now(datetime.timezone.utc))
    assert checks.check_empty_diff("url", mp, _LP()) is None


def test_empty_diff_returns_false_when_diff_missing_past_grace_period():
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    mp = FakeMP(no_diff=True, date_created=old)
    assert checks.check_empty_diff("url", mp, _LP()) is False


def test_diff_missing_without_date_created_fails_safe_to_still_generating():
    mp = FakeMP(no_diff=True)  # date_created unset -> can't tell the age
    assert checks.check_mp_conflicts("url", mp, _LP()) is None
    assert checks.check_empty_diff("url", mp, _LP()) is None


def test_changelog_bug_reference_returns_false_when_diff_missing_past_grace_period():
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    mp = FakeMP(no_diff=True, date_created=old)
    assert checks.check_changelog_bug_reference("url", mp, _LP()) is False


def test_facts_reflect_conflict_state():
    assert facts.build_facts(FakeMP(conflicts="x.c"))["has_conflicts"] is True
    assert facts.build_facts(FakeMP(conflicts=""))["has_conflicts"] is False


def test_target_branch_matches_real_ref_format():
    # Real Launchpad returns e.g. 'refs/heads/ubuntu/devel'. Default FakeMP
    # source branch is merge-shaped, so this is a merge MP mistargeted.
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    lp = _LP()
    finding = checks.check_target_branch("url", mp, lp)
    assert finding.tier == "incomplete"
    assert lp.comments == []

    ok = FakeMP(target="refs/heads/debian/sid")
    assert checks.check_target_branch("url", ok, _LP()) is False


def test_fix_mp_targeting_devel_is_not_bounced():
    # Regression test: a plain fix MP (not a Debian rebase) legitimately
    # targets ubuntu/devel and must not be treated as a mistargeted merge.
    mp = FakeMP(target="refs/heads/ubuntu/devel", source="refs/heads/fix-lp2155031")
    assert checks.check_target_branch("url", mp, _LP()) is False


def test_sru_mp_targeting_series_is_not_bounced():
    mp = FakeMP(target="refs/heads/ubuntu/jammy", source="refs/heads/fix-lp2155031-jammy")
    assert checks.check_target_branch("url", mp, _LP()) is False


def test_merge_detected_via_linked_bug_title_when_branch_isnt_merge_shaped():
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/some-branch",
        bugs=[FakeBugRef("Merge foo from Debian for stonking cycle")],
    )
    lp = _LP()
    finding = checks.check_target_branch("url", mp, lp)
    assert finding.tier == "incomplete"
    assert lp.comments == []


def test_unrelated_linked_bug_title_does_not_trigger_bounce():
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/fix-branch",
        bugs=[FakeBugRef("foo: crashes on startup")],
    )
    assert checks.check_target_branch("url", mp, _LP()) is False


def test_merge_proposal_detection_returns_none_when_bugs_unreadable():
    # A branch name that isn't merge-shaped plus an unreadable bugs fallback
    # means "couldn't determine" (None), not a confirmed "not a merge"
    # (False) -- the caller must retry, not cache a guess.
    class _NoBugs:
        source_git_path = "refs/heads/some-branch"

        @property
        def bugs(self):
            raise RuntimeError("boom")

    assert checks._is_merge_proposal(_NoBugs()) is None


_UBUNTU_BASED_DIFF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,11 @@
+testpkg (1.4.0-0ubuntu1) stonking; urgency=medium
+
+  * New upstream release.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.3.1-10ubuntu1) stonking; urgency=medium

   * Something
"""


def test_merge_shaped_branch_but_based_on_ubuntu_upload_is_not_a_merge():
    # Trigger: rust-sequoia-sq MP #508836 -- branch/bug title say "merge",
    # but the changelog base version already carries an ubuntuN suffix, so
    # it's a version bump done in Ubuntu, not a rebase onto Debian.
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/merge-lp2161399-stonking",
        diff=FakeDiff("/d/1", 20, diff_text=_UBUNTU_BASED_DIFF),
    )
    assert checks._is_merge_proposal(mp) is False
    assert checks.check_target_branch("url", mp, _LP()) is False


def test_merge_shaped_bug_title_but_based_on_ubuntu_upload_is_not_a_merge():
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/some-branch",
        bugs=[FakeBugRef("Please merge foo into Stonking")],
        diff=FakeDiff("/d/1", 20, diff_text=_UBUNTU_BASED_DIFF),
    )
    assert checks._is_merge_proposal(mp) is False
    assert checks.check_target_branch("url", mp, _LP()) is False


def test_merge_shaped_branch_based_on_debian_version_is_still_a_merge():
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/merge-1.2-3-stonking",
        diff=FakeDiff("/d/1", 20, diff_text=_CHANGELOG_DIFF.format(debian_suite="unstable")),
    )
    assert checks._is_merge_proposal(mp) is True
    finding = checks.check_target_branch("url", mp, _LP())
    assert finding.tier == "incomplete"


def test_check_target_branch_returns_none_when_merge_status_undeterminable():
    mp = FakeMP(
        target="refs/heads/ubuntu/devel",
        source="refs/heads/some-branch",
    )

    class _RaisingBugs:
        def __iter__(self):
            raise RuntimeError("boom")

    mp.bugs = _RaisingBugs()
    assert checks.check_target_branch("url", mp, _LP()) is None


# --- sid vs experimental: diff-context (primary) and archive_lookup (fallback) ---

_CHANGELOG_DIFF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,11 @@
+testpkg (1.2-3ubuntu1) stonking; urgency=medium
+
+  * Merge with Debian {debian_suite} (LP: #1234567). Remaining changes:
+    - some change
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3) {debian_suite}; urgency=medium

   * Something
diff --git a/debian/control b/debian/control
index 1..2 100644
--- a/debian/control
+++ b/debian/control
@@ -1,1 +1,1 @@
-Foo
+Bar
"""

_CHANGELOG_DIFF_NO_HEADER_CONTEXT = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg (1.2-3ubuntu1) stonking; urgency=medium
+
+  * Merge with Debian unstable (LP: #1234567).
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 not a changelog header, just some old context line
"""


def _merge_mp_with_diff(diff_text, package="testpkg"):
    return FakeMP(
        target="refs/heads/ubuntu/devel",
        diff=FakeDiff("/d/1", 174, diff_text=diff_text),
        package=package,
    )


def test_debian_target_suite_from_diff_context_unstable():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    assert checks._debian_target_suite(mp) == "sid"


def test_debian_target_suite_from_diff_context_experimental():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="experimental"))
    assert checks._debian_target_suite(mp) == "experimental"


def test_debian_target_suite_falls_back_to_archive_lookup(monkeypatch):
    # Diff context doesn't have a parseable header on the context line, but the
    # new entry's own version (1.2-3ubuntu1 -> 1.2-3) is readable, so we fall
    # back to checking which Debian suite holds exactly that version.
    monkeypatch.setattr(
        archive_lookup,
        "debian_versions",
        lambda pkg: {"unstable": "1.2-3", "experimental": "1.1-1"} if pkg == "testpkg" else None,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_NO_HEADER_CONTEXT)
    assert checks._debian_target_suite(mp) == "sid"


def test_debian_target_suite_none_when_archive_lookup_ambiguous(monkeypatch):
    monkeypatch.setattr(
        archive_lookup,
        "debian_versions",
        lambda pkg: {"unstable": "1.2-3", "experimental": "1.2-3"},
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_NO_HEADER_CONTEXT)
    assert checks._debian_target_suite(mp) is None


def test_debian_target_suite_none_when_nothing_resolvable():
    # No diff at all, no bugs, default FakeMP diff has no diff_text.
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    assert checks._debian_target_suite(mp) is None


def test_check_target_branch_names_the_specific_suite_when_resolvable():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="experimental"))
    lp = _LP()
    finding = checks.check_target_branch("url", mp, lp)
    assert "`debian/experimental`" in finding.message
    assert "or `debian/experimental`, matching" not in finding.message


def test_check_target_branch_falls_back_to_generic_message_when_unresolvable():
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    lp = _LP()
    finding = checks.check_target_branch("url", mp, lp)
    assert "`debian/sid` (or `debian/experimental`, matching" in finding.message


# --- changelog LP bug reference sanity -----------------------------------

_CHANGELOG_DIFF_NO_BUG_REF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg (1.2-4) stonking; urgency=medium
+
+  * Fix something, no bug reference here.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3) unstable; urgency=medium
"""

_CHANGELOG_DIFF_TWO_BUGS = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,8 @@
+testpkg (1.2-4) stonking; urgency=medium
+
+  * Fix something (LP: #1234567, #7654321).
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3) unstable; urgency=medium
"""

# Regression fixture: a real git-ubuntu merge diff can show far more than the
# new entry as "+" (confirmed live: 1472 of 1697 debian/changelog diff lines
# were "+" for one real merge MP, not a clean top-of-file insert). Here the
# *whole* hunk -- including an unrelated older stanza citing a different bug
# -- is "+"-prefixed; only #1234567 (the first/new stanza) should be picked up.
_CHANGELOG_DIFF_WHOLESALE_REPLACE = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,10 +1,15 @@
+testpkg (1.2-4ubuntu1) stonking; urgency=medium
+
+  * Merge with Debian unstable (LP: #1234567). Remaining changes:
+    - some carried-forward change
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
+testpkg (1.2-3ubuntu2) resolute; urgency=medium
+
+  * Some old unrelated fix (LP: #9999999)
+
+ -- C D <c@d.com>  Wed, 01 Jan 2025 10:27:27 +0200
+
 testpkg (1.2-3) unstable; urgency=medium
"""


def test_changelog_bug_reference_matches_package_no_warning():
    bug = FakeBug(tasks=[FakeTask("testpkg (Ubuntu)", "New")])
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    lp = _LP(bugs={1234567: bug})
    assert checks.check_changelog_bug_reference("url", mp, lp) is False


def test_changelog_bug_reference_matches_debian_task():
    # Any task naming the package counts, not just an Ubuntu one -- a bug
    # page can legitimately list several packages.
    bug = FakeBug(tasks=[FakeTask("testpkg (Debian)", "New")])
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    lp = _LP(bugs={1234567: bug})
    assert checks.check_changelog_bug_reference("url", mp, lp) is False


def test_changelog_bug_reference_mismatched_package_warns():
    bug = FakeBug(tasks=[FakeTask("otherpkg (Ubuntu)", "New")])
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    lp = _LP(bugs={1234567: bug})
    finding = checks.check_changelog_bug_reference("url", mp, lp)
    assert finding.tier == "incomplete"
    assert "testpkg" in finding.message
    assert "#1234567" in finding.message
    assert lp.comments == []


def test_changelog_bug_reference_confirmed_mismatch_fires_even_if_other_lookup_fails():
    # #1234567's lookup fails (missing from the fake bugs map) but #7654321
    # is a confirmed mismatch -- we already have a definite problem to
    # report, so this still fires rather than returning None.
    mismatched_bug = FakeBug(tasks=[FakeTask("otherpkg (Ubuntu)", "New")])
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_TWO_BUGS)
    lp = _LP(bugs={7654321: mismatched_bug})
    finding = checks.check_changelog_bug_reference("url", mp, lp)
    assert "#7654321" in finding.message


def test_changelog_bug_reference_returns_none_when_diff_unreadable():
    mp = FakeMP(target="refs/heads/ubuntu/devel")  # default diff has no diff_text
    assert checks.check_changelog_bug_reference("url", mp, _LP()) is None


def test_changelog_bug_reference_no_bug_cited_skips():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_NO_BUG_REF)
    assert checks.check_changelog_bug_reference("url", mp, _LP()) is False


def test_lp_bug_numbers_ignore_older_entries_swept_up_by_a_wholesale_diff():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_WHOLESALE_REPLACE)
    numbers = checks._lp_bug_numbers_from_new_changelog_entry(mp)
    assert numbers == {1234567}


def test_changelog_bug_reference_lookup_failure_returns_none():
    # Bug #1234567 isn't in the fake `lp.bugs` map -> lookup fails. We
    # neither treat "couldn't check" as a confirmed mismatch, nor as a
    # confirmed clean pass (False) -- it's genuinely undetermined (None),
    # so a network blip here isn't cached as "no problem" forever.
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    lp = _LP(bugs={})
    assert checks.check_changelog_bug_reference("url", mp, lp) is None
    assert lp.comments == []


def test_changelog_bug_reference_partial_mismatch_lists_only_mismatched():
    matching_bug = FakeBug(tasks=[FakeTask("testpkg (Ubuntu)", "New")])
    mismatched_bug = FakeBug(tasks=[FakeTask("otherpkg (Ubuntu)", "New")])
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_TWO_BUGS)
    lp = _LP(bugs={1234567: matching_bug, 7654321: mismatched_bug})
    finding = checks.check_changelog_bug_reference("url", mp, lp)
    assert "#7654321" in finding.message
    assert "#1234567" not in finding.message


def test_changelog_bug_reference_skips_when_package_undeterminable():
    class _NoPackageMP:
        resource_type_link = FakeMP.resource_type_link
        self_link = "https://api.launchpad.net/devel/mp/1"
        preview_diff = None

    assert checks.check_changelog_bug_reference("url", _NoPackageMP(), _LP()) is False


# --- proposed version vs. archive ------------------------------------------

_CHANGELOG_DIFF_V124 = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg (1.2-4) stonking; urgency=medium
+
+  * Fix something.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3) unstable; urgency=medium
"""

_ARCHIVE_CHANGELOG_MATCHING = """testpkg (1.2-4) stonking; urgency=medium

  * Fix something.

 -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200

testpkg (1.2-3) unstable; urgency=medium

  * Something old.
"""

_ARCHIVE_CHANGELOG_DIFFERENT = """testpkg (1.2-4) stonking; urgency=medium

  * A completely different fix uploaded by someone else.

 -- C D <c@d.com>  Thu, 02 Jul 2026 10:27:27 +0200

testpkg (1.2-3) unstable; urgency=medium

  * Something old.
"""


def _patch_archive(
    monkeypatch,
    devel="noble",
    versions=None,
    pub=object(),
    changelog=None,
    historical_pub=None,
    queued=False,
    queue_changes=None,
):
    """historical_pub is what an any-status published_source lookup
    (status=None) returns -- design_journal.md #43's cmp<0 check for a
    since-superseded publication of the exact proposed version. Defaults
    to None (nothing found), matching every pre-#43 test's expectation
    that an older-than-archive proposal with no history just bounces."""
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: devel)
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: versions,
    )
    monkeypatch.setattr(
        archive_lookup,
        "published_source",
        lambda lp, pkg, series, version, status="Published": (
            pub if status == "Published" else historical_pub
        ),
    )
    monkeypatch.setattr(archive_lookup, "changelog_text", lambda p: changelog)
    monkeypatch.setattr(
        archive_lookup,
        "upload_in_queue",
        lambda lp, pkg, series, version: queued,
    )
    monkeypatch.setattr(archive_lookup, "queue_changes_text", lambda upload: queue_changes)


def test_stale_version_newer_than_archive_is_fine(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is False


# The queue .changes' Changes field: same stanza as _CHANGELOG_DIFF_V124's
# new entry, but WITHOUT the ' -- maintainer' trailer (a .changes Changes
# field never carries one -- design #56's trailer-insensitive comparison).
_QUEUE_CHANGES_MATCHING = """testpkg (1.2-4) stonking; urgency=medium

  * Fix something."""

_QUEUE_CHANGES_DIFFERENT = """testpkg (1.2-4) stonking; urgency=medium

  * A completely different SRU that raced this MP to the same version."""


def test_stale_version_newer_but_sitting_in_upload_queue_is_queued(monkeypatch):
    # Design #55, found live: libp11 MP #507660 -- the SRU was already
    # uploaded and waiting in noble's Unapproved queue, so there's nothing
    # left to sponsor; defer silently instead of running the LLM review.
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-3"},
        queued=object(),
        queue_changes=_QUEUE_CHANGES_MATCHING,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "queued"
    assert lp.comments == []


def test_stale_version_queued_with_different_content_bounces(monkeypatch):
    # Design #56 (seb128): SRU version increments are convention-fixed, so
    # an independent SRU racing this MP picks the SAME version number --
    # a queue hit only means "nothing to do" if the changelog content
    # matches; otherwise the contributor needs to rebase and bump.
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-3"},
        queued=object(),
        queue_changes=_QUEUE_CHANGES_DIFFERENT,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    finding = checks.check_stale_version("url", mp, _LP())
    assert finding.tier == "incomplete"
    assert "1.2-4" in finding.message
    assert "upload queue" in finding.message


def test_stale_version_queued_but_changes_unreadable_is_inconclusive(monkeypatch):
    # Can't tell WHOSE upload is in the queue -> can't determine, retry.
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-3"},
        queued=object(),
        queue_changes=None,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_queue_lookup_failure_is_inconclusive(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-3"}, queued=None)
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_older_than_archive_bounces(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert finding.tier == "incomplete"
    assert "1.2-4" in finding.message
    assert "1.2-5" in finding.message
    assert lp.comments == []


def test_stale_version_prefers_proposed_pocket_over_release(monkeypatch):
    # -proposed (1.2-5) is ahead of the release pocket (1.2-3); the older
    # comparison should use -proposed, not the release version.
    _patch_archive(monkeypatch, versions={"noble": "1.2-3", "noble-proposed": "1.2-5"})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert "1.2-5" in finding.message


# --- older than archive, but the proposed version was itself once published
# and later superseded by unrelated newer work (design_journal.md #43) -------


def test_stale_version_older_but_was_itself_published_and_superseded_is_done(
    monkeypatch,
):
    # Found live: MP #505086 proposed 1.2-4, archive is ahead at 1.2-5, but
    # 1.2-4 itself was published (now Superseded) with matching content --
    # this MP's own change already landed; "please rebase" is the wrong
    # message, "already uploaded, can be closed" is.
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-5"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
        historical_pub=_FakePublishedSource(),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.votes == [None]
    assert "1.2-4" in lp.comments[0]


def test_stale_version_older_but_was_itself_published_with_different_content(
    monkeypatch,
):
    # The exact proposed version was published (now superseded) but with
    # DIFFERENT content -- a genuine version collision, still needs a
    # rebase, just phrased as a duplicate-version conflict rather than "the
    # archive has moved on".
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-5"},
        changelog=_ARCHIVE_CHANGELOG_DIFFERENT,
        historical_pub=_FakePublishedSource(),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert finding.tier == "incomplete"
    assert "different content" in finding.message


def test_stale_version_older_with_no_history_bounces_as_before(monkeypatch):
    # The exact proposed version genuinely was never published (no history
    # at all, any status) -- falls through to the original "needs a
    # rebase" bounce. historical_pub defaults to None in _patch_archive.
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert "older than the one already in the archive" in finding.message


def test_stale_version_older_history_lookup_changelog_fetch_fails_is_none(
    monkeypatch,
):
    # A historical publication of the exact proposed version was found, but
    # its changelog couldn't be fetched -- genuinely can't determine
    # whether this is "already landed" or a rebase; must not guess either
    # way (changelog=None is _patch_archive's default: lookup "succeeded"
    # in finding a record, but changelog_text itself fails).
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-5"},
        historical_pub=_FakePublishedSource(),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_same_version_matching_content_is_done(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.votes == [None]
    assert "1.2-4" in lp.comments[0]


def test_stale_version_matching_content_links_to_the_publication(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert "https://launchpad.net/ubuntu/+source/testpkg/1.2-4" in lp.comments[0]


def test_stale_version_same_version_different_content_bounces(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_DIFFERENT,
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert finding.tier == "incomplete"
    assert lp.comments == []


def test_stale_version_returns_none_when_devel_series_unknown(monkeypatch):
    # A lookup failure -- must not be cached as "nothing to flag".
    _patch_archive(monkeypatch, devel=None, versions={"noble": "1.2-5"})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_structural_false_when_archive_has_nothing_published(
    monkeypatch,
):
    # versions={} (not None): the lookup succeeded, this package just isn't
    # published in this series yet -- a stable fact, not a lookup failure.
    _patch_archive(monkeypatch, versions={})
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is False


def test_stale_version_returns_none_when_ubuntu_versions_lookup_fails(monkeypatch):
    _patch_archive(monkeypatch, versions=None)
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_returns_none_when_publication_record_unavailable(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-4"}, pub=None)
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_returns_none_when_archive_changelog_unfetchable(monkeypatch):
    _patch_archive(monkeypatch, versions={"noble": "1.2-4"}, changelog=None)
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_returns_none_when_diff_unreadable(monkeypatch):
    # Default FakeDiff has no diff_text -- reading it raises, same as a
    # real network failure fetching the diff. Must not be cached as clean.
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    assert checks.check_stale_version("url", mp, _LP()) is None


def test_stale_version_structural_false_when_diff_missing_past_grace_period(
    monkeypatch,
):
    # preview_diff genuinely None (not an exception) and older than the
    # generation grace period -- treated as "no diff data available", same
    # bucket as no debian/changelog section, not retried forever.
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    mp = FakeMP(target="refs/heads/ubuntu/devel", no_diff=True, date_created=old)
    assert checks.check_stale_version("url", mp, _LP()) is False


def test_stale_version_structural_false_when_diff_has_no_changelog_section(
    monkeypatch,
):
    # Diff fetched fine, but touches only debian/control -- a stable fact
    # about this MP's content, distinct from an unreadable diff.
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    diff_text = (
        "diff --git a/debian/control b/debian/control\n"
        "index 1..2 100644\n"
        "--- a/debian/control\n"
        "+++ b/debian/control\n"
        "@@ -1,1 +1,1 @@\n"
        "-Foo\n"
        "+Bar\n"
    )
    mp = _merge_mp_with_diff(diff_text)
    assert checks.check_stale_version("url", mp, _LP()) is False


def test_stale_version_on_a_bug_without_attachments_is_clean(monkeypatch):
    # The bug side is real since #65 (see test_attachments.py); with no
    # usable attachment there is no proposed version to compare.
    _patch_archive(monkeypatch, versions={"noble": "1.2-5"})
    bug = FakeBug()
    assert checks.check_stale_version("url", bug, _LP()) is False


# --- 3a deferral: git-ubuntu's importer may auto-close the MP itself -------


class _FakePublishedSource:
    def __init__(self, date_published=None):
        self.date_published = date_published


def test_stale_version_recent_upload_defers_close_comment(monkeypatch):
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
        pub=_FakePublishedSource(date_published=recent),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "pending"
    assert lp.comments == []
    assert lp.votes == []


def test_stale_version_old_upload_posts_the_close_comment(monkeypatch):
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
        pub=_FakePublishedSource(date_published=old),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.comments


def test_stale_version_recent_upload_with_different_content_still_bounces(
    monkeypatch,
):
    # The grace period only applies to 3a (matching content) -- a genuine
    # duplicate-version upload with different content needs a rebase
    # regardless of how recently it landed.
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_DIFFERENT,
        pub=_FakePublishedSource(date_published=recent),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp).tier == "incomplete"


def test_stale_version_missing_date_published_does_not_defer(monkeypatch):
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
        pub=_FakePublishedSource(date_published=None),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"


def test_stale_version_unparseable_date_published_fails_safe_to_no_defer(
    monkeypatch,
):
    _patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=_ARCHIVE_CHANGELOG_MATCHING,
        pub=_FakePublishedSource(date_published="not-a-datetime"),
    )
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF_V124)
    lp = _LP()
    assert checks.check_stale_version("url", mp, lp) == "done"


# --- per-item diff-content memoization (design_journal.md #39) ---


def test_diff_content_fetched_once_across_checks():
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    checks._changelog_diff_lines(mp)
    checks._changelog_diff_lines(mp)
    assert mp.preview_diff.diff_text.opens == 1


def test_diff_memo_invalidated_by_a_new_diff():
    # A fresh push = a new preview diff with a new self_link -> refetch.
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))
    first = checks._changelog_diff_lines(mp)
    mp.preview_diff = FakeDiff(
        "/d/2", 20, diff_text=_CHANGELOG_DIFF.format(debian_suite="experimental")
    )
    second = checks._changelog_diff_lines(mp)
    assert mp.preview_diff.diff_text.opens == 1
    assert first != second


def test_diff_memo_caches_failures_too():
    # The whole point (design #33/#39): a failed fetch must NOT be re-attempted
    # by the next check in the same item -- that compounds slow failures.
    mp = _merge_mp_with_diff(_CHANGELOG_DIFF.format(debian_suite="unstable"))

    class _FailingFile:
        opens = 0

        def open(self):
            _FailingFile.opens += 1
            raise TimeoutError("simulated librarian timeout")

    mp.preview_diff.diff_text = _FailingFile()
    assert checks._changelog_diff_lines(mp) is None
    assert checks._changelog_diff_lines(mp) is None
    assert _FailingFile.opens == 1

    # reset (what main does per item) -> genuinely retried
    checks.reset_diff_lines_cache()
    assert checks._changelog_diff_lines(mp) is None
    assert _FailingFile.opens == 2


# --- target series resolution: SRUs target their own series, not devel -----
# (design_journal.md #41: check_stale_version used to always compare against
# the current devel series regardless of what the MP targets, wrongly
# flagging a real SRU as stale against an unrelated, unreleased series.)


def test_target_ubuntu_series_sru_branch():
    mp = FakeMP(target="refs/heads/ubuntu/noble-devel")
    assert checks._target_ubuntu_series(mp, lp=None) == "noble"


def test_target_ubuntu_series_pocket_branches_name_their_series():
    # #84 (nano MP #508284): a jammy SRU targeting the git-ubuntu pocket
    # branch ubuntu/jammy-updates was compared against devel because the
    # target didn't parse. Whatever the branch-choice merits (backlog 5e),
    # the series it names is unambiguous.
    for pocket in ("updates", "security", "proposed", "backports"):
        mp = FakeMP(target=f"refs/heads/ubuntu/jammy-{pocket}")
        assert checks._target_ubuntu_series(mp, lp=None) == "jammy"


def test_target_ubuntu_series_bare_devel_resolves_via_devel_codename(monkeypatch):
    monkeypatch.setattr(checks.archive_lookup, "devel_codename", lambda lp: "stonking")
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    assert checks._target_ubuntu_series(mp, lp=object()) == "stonking"


def test_target_ubuntu_series_debian_target_falls_back_to_devel(monkeypatch):
    # A merge MP not yet retargeted to debian/* -- still lands via devel (#27).
    monkeypatch.setattr(checks.archive_lookup, "devel_codename", lambda lp: "stonking")
    mp = FakeMP(target="refs/heads/debian/sid")
    assert checks._target_ubuntu_series(mp, lp=object()) == "stonking"


def test_target_ubuntu_series_none_when_devel_codename_fails(monkeypatch):
    monkeypatch.setattr(checks.archive_lookup, "devel_codename", lambda lp: None)
    mp = FakeMP(target="refs/heads/ubuntu/devel")
    assert checks._target_ubuntu_series(mp, lp=object()) is None


def test_max_published_version_picks_highest_across_pockets():
    versions = {"noble": "1.2-3", "noble-updates": "1.2-6", "noble-proposed": "1.2-5"}
    assert checks._max_published_version(versions) == "1.2-6"


def test_max_published_version_empty_is_none():
    assert checks._max_published_version({}) is None


def _sru_mp_with_diff(diff_text, series="noble", package="testpkg"):
    return FakeMP(
        target=f"refs/heads/ubuntu/{series}-devel",
        diff=FakeDiff("/d/1", 40, diff_text=diff_text),
        package=package,
    )


_SRU_CHANGELOG_DIFF = """diff --git a/debian/changelog b/debian/changelog
index e84b35c..8f19411 100644
--- a/debian/changelog
+++ b/debian/changelog
@@ -1,3 +1,7 @@
+testpkg (1.2-3ubuntu1~24.04.0) noble; urgency=medium
+
+  * Backport to noble.
+
+ -- A B <a@b.com>  Wed, 01 Jul 2026 10:27:27 +0200
+
 testpkg (1.2-3ubuntu1) stonking; urgency=medium

   * Something
"""


def test_stale_version_checks_the_targeted_series_not_devel(monkeypatch):
    # The bug this reproduces: an SRU proposing 1.2-3ubuntu1~24.04.0 for
    # noble must be compared against noble's own archive state, not
    # whatever unrelated version happens to be in the (still unreleased)
    # devel series.
    calls = []

    def fake_ubuntu_versions(lp, pkg, series_names=None):
        calls.append(series_names)
        if series_names == ["noble"]:
            return {"noble": "1.2-2ubuntu1~24.04.0"}  # older -> proposed wins
        return {"stonking": "5.0-1"}  # devel: unrelated, would wrongly "win"

    monkeypatch.setattr(checks.archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(checks.archive_lookup, "ubuntu_versions", fake_ubuntu_versions)
    monkeypatch.setattr(
        checks.archive_lookup,
        "upload_in_queue",
        lambda lp, pkg, series, version: False,
    )

    mp = _sru_mp_with_diff(_SRU_CHANGELOG_DIFF)
    assert checks.check_stale_version("url", mp, _LP()) is False
    assert calls == [["noble"]]


def test_stale_version_sru_older_than_noble_needs_fixing(monkeypatch):
    def fake_ubuntu_versions(lp, pkg, series_names=None):
        if series_names == ["noble"]:
            return {"noble-updates": "1.2-4ubuntu1~24.04.0"}
        return {"stonking": "0.1-1"}

    monkeypatch.setattr(checks.archive_lookup, "devel_codename", lambda lp: "stonking")
    monkeypatch.setattr(checks.archive_lookup, "ubuntu_versions", fake_ubuntu_versions)

    mp = _sru_mp_with_diff(_SRU_CHANGELOG_DIFF)
    lp = _LP()
    finding = checks.check_stale_version("url", mp, lp)
    assert finding.tier == "incomplete"
    assert "noble" in finding.message
    assert "stonking" not in finding.message
