"""Sync-request decision tree (llm_reviewer._triage_sync / _parse_sync_title).

archive_lookup itself is mocked at the function level (its own HTTP/Launchpad
behaviour is covered by test_archive_lookup.py); here we're only testing the
branching logic. _query_llm is mocked so no real opencode call happens. `lp`
is never touched by these tests since devel_codename/ubuntu_versions are
monkeypatched -- a sentinel stands in for it.
"""

import archive_lookup
from llm_reviewer import LLMReviewer, _parse_sync_title

_LP = object()  # never dereferenced; archive_lookup calls are monkeypatched


# --- _parse_sync_title ---------------------------------------------------


def test_parse_title_with_component_and_suite():
    assert _parse_sync_title("Sync foo 1.2-3 (main) from Debian unstable") == (
        "foo",
        "1.2-3",
        "unstable",
    )


def test_parse_title_lowercase_no_component():
    assert _parse_sync_title("sync bar 2.0 from debian experimental") == (
        "bar",
        "2.0",
        "experimental",
    )


def test_parse_title_no_suite_named_returns_none_suite():
    assert _parse_sync_title("Sync baz 3.1-1 (universe) from Debian") == (
        "baz",
        "3.1-1",
        None,
    )


def test_parse_title_unparsable_returns_none():
    assert _parse_sync_title("Please sync my package") is None
    assert _parse_sync_title("") is None


# --- _triage_sync ---------------------------------------------------------


def _reviewer(monkeypatch, yaml_response=None):
    r = LLMReviewer(lp=_LP)
    if yaml_response is not None:
        monkeypatch.setattr(r, "_query_llm", lambda *a, **k: yaml_response)
    return r


TITLE = "Sync foo 1.2-3 (main) from Debian unstable"


def test_has_delta_routes_to_delta_explanation(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {"noble": "1.2-2ubuntu1"},
    )
    r = _reviewer(monkeypatch, "```yaml\nverdict: pass\nreason:\n```")
    status, comment = r._triage_sync(TITLE, "no delta, explained")
    assert status == "READY_FOR_HUMAN"
    assert "delta" in comment.lower()


def test_no_delta_already_synced_closes(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {"noble": "1.2-3"},
    )
    r = _reviewer(monkeypatch)
    status, comment = r._triage_sync(TITLE, "please sync")
    assert status == "SYNCED"
    assert "foo" in comment and "1.2-3" in comment


def test_no_delta_newer_already_published_closes(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {"noble": "1.2-4"},
    )
    r = _reviewer(monkeypatch)
    status, _ = r._triage_sync(TITLE, "please sync")
    assert status == "SYNCED"


def test_no_delta_not_synced_found_in_debian_ready_for_human(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    monkeypatch.setattr(
        archive_lookup,
        "debian_versions",
        lambda pkg, suites=None: {"sid": "1.2-3"},  # 'unstable' keyed as 'sid'
    )
    r = _reviewer(monkeypatch)
    status, comment = r._triage_sync(TITLE, "please sync")
    assert status == "READY_FOR_HUMAN"
    assert "unstable" in comment


def test_not_in_debian_llm_justified_ready_for_human(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    monkeypatch.setattr(archive_lookup, "debian_versions", lambda pkg, suites=None: {})
    r = _reviewer(monkeypatch, "```yaml\nverdict: pass\nreason:\n```")
    status, comment = r._triage_sync(TITLE, "special case: NEW queue backlog")
    assert status == "READY_FOR_HUMAN"


def test_not_in_debian_llm_unjustified_incomplete(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    monkeypatch.setattr(archive_lookup, "debian_versions", lambda pkg, suites=None: {})
    r = _reviewer(
        monkeypatch,
        "```yaml\nverdict: fail\nreason: Version not found in Debian unstable.\n```",
    )
    status, comment = r._triage_sync(TITLE, "no explanation given")
    assert status == "INCOMPLETE"
    assert "Version not found" in comment


def test_experimental_suite_hint_is_used(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    captured = {}

    def fake_debian_versions(pkg, suites=None):
        captured["suites"] = suites
        return {"experimental": "2.0"}

    monkeypatch.setattr(archive_lookup, "debian_versions", fake_debian_versions)
    r = _reviewer(monkeypatch)
    title = "sync bar 2.0 from debian experimental"
    status, _ = r._triage_sync(title, "please sync")
    assert status == "READY_FOR_HUMAN"
    assert captured["suites"] == ["experimental"]


def test_unparsable_title_falls_back_to_delta_review(monkeypatch):
    r = _reviewer(monkeypatch, "```yaml\nverdict: pass\nreason:\n```")
    status, comment = r._triage_sync("Please sync my package", "no delta here")
    assert status == "READY_FOR_HUMAN"
    assert "delta" in comment.lower()


def test_devel_codename_unknown_falls_back_to_delta_review(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: None)
    r = _reviewer(monkeypatch, "```yaml\nverdict: pass\nreason:\n```")
    status, comment = r._triage_sync(TITLE, "no delta here")
    assert status == "READY_FOR_HUMAN"
    assert "delta" in comment.lower()


def test_ubuntu_lookup_failure_falls_back_to_delta_review(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: None)
    r = _reviewer(monkeypatch, "```yaml\nverdict: pass\nreason:\n```")
    status, comment = r._triage_sync(TITLE, "no delta here")
    assert status == "READY_FOR_HUMAN"
    assert "delta" in comment.lower()


def test_debian_lookup_failure_routes_to_human_without_llm(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(archive_lookup, "ubuntu_versions", lambda lp, pkg, series_names=None: {})
    monkeypatch.setattr(archive_lookup, "debian_versions", lambda pkg, suites=None: None)
    r = LLMReviewer(lp=_LP)

    def fail_if_called(*a, **k):
        raise AssertionError("LLM should not be called when the archive lookup itself failed")

    monkeypatch.setattr(r, "_query_llm", fail_if_called)
    status, comment = r._triage_sync(TITLE, "please sync")
    assert status == "READY_FOR_HUMAN"
    assert "could not verify" in comment.lower()


# --- triage_bug dispatch --------------------------------------------------


class _Bug:
    def __init__(self, title, description, tags=None):
        self.title = title
        self.description = description
        self.tags = tags or []


def test_triage_bug_dispatches_sync_titled_bugs_to_triage_sync(monkeypatch):
    monkeypatch.setattr(archive_lookup, "devel_codename", lambda lp: "noble")
    monkeypatch.setattr(
        archive_lookup,
        "ubuntu_versions",
        lambda lp, pkg, series_names=None: {"noble": "1.2-3"},
    )
    r = LLMReviewer(lp=_LP)
    bug = _Bug(TITLE, "please sync")
    status, comment = r.triage_bug(bug)
    assert status == "SYNCED"
