# Root conftest: ensures the project modules (checks, main, ...) and the test
# helpers in tests/ are importable when running `pytest` from the project root.

import pytest

import archive_lookup
import checks


@pytest.fixture(autouse=True)
def _reset_diff_lines_cache():
    # Real MPs never share a preview-diff self_link, but the fakes reuse
    # short ones ('/d/1') across tests -- without this, one test's cached
    # diff content (or cached failure) would leak into the next.
    checks.reset_diff_lines_cache()
    archive_lookup.reset_cache()
    yield


@pytest.fixture(autouse=True)
def _no_real_notify_config(monkeypatch, tmp_path):
    # The developer machine may have a real webhook configured in
    # ~/.config/ubuntu-sponsoring-frontdesk/config.ini; tests must never see it
    # (notify.is_configured() changes behavior -- the #43 importer-failure
    # switch -- and an enabled notify() would post to the real channel).
    monkeypatch.setenv("SPONSORING_BOT_CONFIG", str(tmp_path / "no-config.ini"))
    yield


@pytest.fixture(autouse=True)
def _agent_preflight_passes(monkeypatch):
    # #166: the agent pre-flight runs the real `opencode debug agent`; tests
    # that exercise _query_llm fake subprocess.run for the call itself and
    # must not hit it. Tests of the pre-flight override this or call
    # opencode_agent directly.
    import llm_reviewer
    import opencode_agent

    monkeypatch.setattr(
        llm_reviewer, "_check_agent", lambda env: opencode_agent.Inspection(None, None)
    )
    yield
