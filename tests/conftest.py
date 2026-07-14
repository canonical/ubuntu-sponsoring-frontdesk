"""Shared test fixtures.

The per-item memo caches are keyed on Launchpad self_links, and the fakes
reuse a handful of shared links (FakeBug is always .../bug/1), so a cached
verdict from one test would leak into the next -- across modules, in
whatever order pytest runs them. Reset every per-item cache before each
test, so no test module has to remember to do it itself.
"""

import pytest

import archive_lookup
import attachments
import checks
import launchpad_client


@pytest.fixture(autouse=True)
def _reset_per_item_caches():
    checks.reset_diff_lines_cache()
    checks.reset_human_engaged_cache()
    attachments.reset_cache()


@pytest.fixture(autouse=True)
def _no_privileged_helper(monkeypatch):
    """Tests must never depend on (or spawn) the real privileged helper
    (#78): _helper_configured() looks for a credentials file on the host,
    so once real helper credentials exist, un-pinned tests would start
    subprocesses. Default to unconfigured; delegation tests override."""
    monkeypatch.setattr(launchpad_client, "_helper_configured", lambda: False)


@pytest.fixture(autouse=True)
def _pinned_distro_info(monkeypatch):
    """supported_series_ordered shells out to ubuntu-distro-info (#83);
    tests must not depend on the host's distro-info-data. Pin the series
    table to the same set FakeRoot's fixtures use (jammy..stonking,
    stonking = devel); the function's own unit tests override."""
    outputs = {
        ("--supported",): "jammy\nnoble\nresolute\nstonking\n",
        ("--supported", "--release"): "22.04 LTS\n24.04 LTS\n26.04 LTS\n26.10\n",
    }
    monkeypatch.setattr(
        archive_lookup, "_distro_info", lambda *args: outputs[args]
    )
