"""Design #48: operator notifications via a Mattermost incoming webhook --
config handling, dry-run suppression, best-effort failure tolerance, and the
two round-one triggers (stuck diff generation, importer failed to auto-close).
"""

import datetime

import pytest

import checks
import main
import notify
import test_mp_checks
from state import StateManager
from fakes import FakeLLM, FakeMP, FakeTriageClient

URL = "https://code.launchpad.net/~marco/+merge/12345"


@pytest.fixture(autouse=True)
def _disabled_by_default():
    # notify._enabled is process-global; tests that enable it must not leak
    # into their neighbors.
    yield
    notify.setup(False)


@pytest.fixture
def posts(monkeypatch):
    """Replace the webhook POST with a recorder. Returns the list of
    (url, text) tuples actually 'sent'."""
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, text: sent.append((url, text)))
    return sent


def _write_config(tmp_path, monkeypatch, url="https://chat.example.com/hooks/abc"):
    path = tmp_path / "config.ini"
    path.write_text(f"[notifications]\nwebhook_url = {url}\n")
    monkeypatch.setenv("SPONSORING_BOT_CONFIG", str(path))
    return url


# --- config handling --------------------------------------------------------


def test_missing_config_means_not_configured(posts):
    # conftest points SPONSORING_BOT_CONFIG at a nonexistent file.
    assert notify.webhook_url() is None
    assert notify.is_configured() is False
    notify.setup(True)
    assert notify.notify("hello") is False
    assert posts == []


def test_config_file_provides_webhook_url(tmp_path, monkeypatch):
    url = _write_config(tmp_path, monkeypatch)
    assert notify.webhook_url() == url
    assert notify.is_configured() is True


def test_config_without_the_key_means_not_configured(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text("[notifications]\nother = 1\n")
    monkeypatch.setenv("SPONSORING_BOT_CONFIG", str(path))
    assert notify.webhook_url() is None


def test_malformed_config_disables_quietly(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text("not an ini file [at all")
    monkeypatch.setenv("SPONSORING_BOT_CONFIG", str(path))
    assert notify.webhook_url() is None


# --- posting semantics ------------------------------------------------------


def test_disabled_mode_logs_but_does_not_post(tmp_path, monkeypatch, posts):
    # Default state (never setup, or dry-run): configured but not enabled.
    _write_config(tmp_path, monkeypatch)
    assert notify.notify("hello") is False
    assert posts == []


def test_enabled_and_configured_posts(tmp_path, monkeypatch, posts):
    url = _write_config(tmp_path, monkeypatch)
    notify.setup(True)
    assert notify.notify("hello operators") is True
    assert posts == [(url, "hello operators")]


def test_webhook_failure_is_swallowed(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    notify.setup(True)

    def boom(url, text):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "_post", boom)
    # Best-effort by design (#48): the alert is lost, the run continues.
    assert notify.notify("hello") is False


# --- trigger 1: stuck diff generation (#30 -> #48) --------------------------


def _stuck_diff_mp():
    # preview_diff genuinely missing, MP created long past the generation
    # grace period -- checks treat this as a stable "no diff data" fact and
    # the item sails through to READY_FOR_HUMAN.
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=12)
    return FakeMP(no_diff=True, date_created=created)


def _run(tmp_path, mp):
    sm = StateManager(db_path=str(tmp_path / "state.db"))
    lp = FakeTriageClient(objects={URL: mp})
    main.triage_url(URL, sm, lp, FakeLLM())
    return sm, lp


def test_stuck_diff_notifies_operators_once(tmp_path, monkeypatch, posts):
    _write_config(tmp_path, monkeypatch)
    notify.setup(True)
    sm, lp = _run(tmp_path, _stuck_diff_mp())
    assert len(posts) == 1
    assert URL in posts[0][1]
    assert "preview diff" in posts[0][1]
    # The anomaly is operator-facing only: nothing lands on the MP itself.
    assert lp.comments == []
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_stuck_diff_dry_run_does_not_post(tmp_path, monkeypatch, posts):
    _write_config(tmp_path, monkeypatch)
    notify.setup(False)  # dry-run
    _run(tmp_path, _stuck_diff_mp())
    assert posts == []


def test_stuck_diff_unconfigured_stays_quiet(tmp_path, posts):
    notify.setup(True)
    sm, lp = _run(tmp_path, _stuck_diff_mp())
    assert posts == []
    assert sm.get_status(URL)[0] == "READY_FOR_HUMAN"


def test_healthy_mp_does_not_notify(tmp_path, monkeypatch, posts):
    _write_config(tmp_path, monkeypatch)
    notify.setup(True)
    _run(tmp_path, FakeMP())
    assert posts == []


# --- trigger 2: importer failed to auto-close (#43's switch, built as #48) --


def _already_uploaded_mp(monkeypatch):
    test_mp_checks._patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=test_mp_checks._ARCHIVE_CHANGELOG_MATCHING,
    )
    return test_mp_checks._merge_mp_with_diff(test_mp_checks._CHANGELOG_DIFF_V124)


def test_importer_failure_notifies_instead_of_commenting(tmp_path, monkeypatch, posts):
    _write_config(tmp_path, monkeypatch)
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch)
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    # Design #43's switch: fully silent on the MP, operators get the signal.
    assert lp.comments == []
    assert len(posts) == 1
    assert "did not auto-close" in posts[0][1]
    assert "testpkg 1.2-4" in posts[0][1]


def test_importer_failure_dry_run_still_skips_the_comment(tmp_path, monkeypatch, posts):
    # is_configured (not the posting outcome) drives the comment-vs-notify
    # branch, so dry-run behaves like the real run minus the actual POST.
    _write_config(tmp_path, monkeypatch)
    notify.setup(False)
    mp = _already_uploaded_mp(monkeypatch)
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.comments == []
    assert posts == []


def test_importer_failure_unconfigured_keeps_the_mp_comment(monkeypatch, posts):
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch)
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    # No webhook anywhere to send the signal -- current behavior is kept.
    assert "1.2-4" in lp.comments[0]
    assert posts == []
