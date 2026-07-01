"""
Tightened SRU/sync detection in llm_reviewer (replaces brittle exact-substring
matching). The detectors decide which review a bug is routed to, so they must
tolerate the casing/whitespace submitters actually use without misrouting
unrelated bugs.
"""

from llm_reviewer import _is_sru, _is_sync


# --- SRU detection ----------------------------------------------------------


def test_sru_canonical_header():
    assert _is_sru([], "[Impact]\nThe bug crashes foo.") is True


def test_sru_header_case_and_whitespace_insensitive():
    # The old `"[Impact]" in description` missed all of these.
    assert _is_sru([], "[ impact ]\n...") is True
    assert _is_sru([], "[IMPACT]\n...") is True


def test_sru_current_and_legacy_section_names():
    assert _is_sru([], "[Where problems could occur]\n...") is True
    assert _is_sru([], "[Test Plan]\n...") is True
    assert _is_sru([], "[Test Case]\n...") is True
    assert _is_sru([], "[Regression Potential]\n...") is True


def test_sru_tag_case_insensitive():
    assert _is_sru(["SRU"], "nothing templated here") is True


def test_non_sru_plain_bug_not_matched():
    assert _is_sru(["bitesize"], "Please fix the typo in the about dialog.") is False


def test_sru_unbracketed_mention_not_matched():
    # Prose mentioning impact must not trip the SRU router; only the bracketed
    # template header counts.
    assert _is_sru([], "This has a big impact on users.") is False


# --- Sync detection ---------------------------------------------------------


def test_sync_title():
    title = "Sync foo 1.2-3 (main) from Debian unstable"
    assert _is_sync(title, "") is True


def test_sync_title_case_insensitive():
    assert _is_sync("sync bar 2.0 from debian experimental", "") is True


def test_sync_body_markers():
    assert _is_sync("", "Please sync this package.") is True
    assert _is_sync("", "This is a sync request for bar.") is True


def test_sync_body_case_insensitive():
    assert _is_sync("", "please SYNC bar from somewhere") is True


def test_non_sync_bug_not_matched():
    assert _is_sync("Crash on startup", "The app segfaults immediately.") is False
