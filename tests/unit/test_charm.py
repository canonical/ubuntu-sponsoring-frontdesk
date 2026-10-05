# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Unit tests for the ops layer: config -> workload calls, and status."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from charmlibs.systemd import SystemdError
from ops.testing import (
    ActionFailed,
    ActiveStatus,
    BlockedStatus,
    Context,
    MaintenanceStatus,
    Secret,
    State,
)

from charm import FrontdeskCharm
from frontdesk import Frontdesk, InstallError, Secrets


@pytest.fixture
def workload():
    w = MagicMock(spec=Frontdesk)
    w.installed = True
    w.pass_running = False
    w.last_pass_failed = False
    w.ubuntu_lint_installed = True
    with patch("charm.Frontdesk", return_value=w):
        yield w


@pytest.fixture
def ctx():
    return Context(FrontdeskCharm)


def _secret(key, value="content"):
    return Secret(tracked_content={key: value})


def _state(config=None, secrets=()):
    return State(leader=True, config=config or {}, secrets=list(secrets))


def _configured(mode="dry-run", **extra):
    """A State with the required triager secret plus whatever else."""
    triager = _secret("credentials", "triager-token")
    secrets = [triager]
    config = {"mode": mode, "lp-triager-credentials": triager.id}
    for option, (key, value) in extra.items():
        s = _secret(key, value)
        secrets.append(s)
        config[option.replace("_", "-")] = s.id
    return _state(config, secrets)


# --- install / reconcile -------------------------------------------------------


def test_install_without_triager_credentials_blocks(ctx, workload):
    out = ctx.run(ctx.on.install(), _state())

    workload.install.assert_called_once_with()
    workload.configure.assert_called_once_with("off", 60, Secrets(), "")
    assert out.unit_status == BlockedStatus("lp-triager-credentials is not set")


def test_install_failure_blocks_and_skips_configure(ctx, workload):
    workload.install.side_effect = InstallError("apt broke")
    workload.installed = False

    out = ctx.run(ctx.on.install(), _state())

    workload.configure.assert_not_called()
    assert out.unit_status == BlockedStatus("installation failed, see juju debug-log (retrying)")


def test_reconcile_retries_a_failed_install(ctx, workload):
    workload.installed = False

    def install():
        workload.installed = True

    workload.install.side_effect = install

    ctx.run(ctx.on.update_status(), _configured())

    workload.install.assert_called_once_with()
    workload.configure.assert_called_once()


def test_upgrade_reinstalls(ctx, workload):
    ctx.run(ctx.on.upgrade_charm(), _configured())

    workload.install.assert_called_once_with()


def test_configured_dry_run_is_active_and_names_what_is_missing(ctx, workload):
    out = ctx.run(ctx.on.config_changed(), _configured("dry-run"))

    workload.configure.assert_called_once_with(
        "dry-run", 60, Secrets(lp_triager="triager-token"), ""
    )
    assert out.unit_status == ActiveStatus(
        "dry-run every 60m; LLM review off (no opencode-auth); no lp-sponsor-credentials"
    )


def test_all_secrets_reach_the_workload(ctx, workload):
    state = _configured(
        "yes",
        lp_sponsor_credentials=("credentials", "sponsor-token"),
        opencode_auth=("auth-json", "{}"),
        mattermost_webhook=("webhook-url", "https://chat/hooks/x"),
    )
    state = State(
        leader=True,
        config={**state.config, "run-interval": 30, "llm-model": " a/b "},
        secrets=state.secrets,
    )

    out = ctx.run(ctx.on.config_changed(), state)

    workload.configure.assert_called_once_with(
        "yes",
        30,
        Secrets(
            lp_triager="triager-token",
            lp_sponsor="sponsor-token",
            opencode_auth="{}",
            webhook_url="https://chat/hooks/x",
        ),
        "a/b",
    )
    assert out.unit_status == ActiveStatus("yes every 30m")


def test_mode_off_says_so(ctx, workload):
    out = ctx.run(ctx.on.config_changed(), _configured("off"))

    assert out.unit_status.message.startswith("mode=off, no scheduled passes")


def test_secret_changed_reconciles(ctx, workload):
    state = _configured()
    (triager,) = state.secrets

    ctx.run(ctx.on.secret_changed(triager), state)

    workload.configure.assert_called_once()


@pytest.mark.parametrize(
    "config, message",
    [
        ({"mode": "interactive"}, "invalid mode 'interactive': use one of off, dry-run, yes"),
        ({"run-interval": 0}, "run-interval must be at least 1 (minutes)"),
    ],
)
def test_invalid_config_blocks_and_stops_the_timer(ctx, workload, config, message):
    base = _configured()
    state = State(leader=True, config={**base.config, **config}, secrets=base.secrets)

    out = ctx.run(ctx.on.config_changed(), state)

    workload.configure.assert_not_called()
    workload.stop.assert_called_once_with()
    assert out.unit_status == BlockedStatus(message)


def test_secret_without_the_expected_key_blocks(ctx, workload):
    triager = _secret("oauth", "x")
    state = _state({"lp-triager-credentials": triager.id}, [triager])

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == BlockedStatus(
        "lp-triager-credentials: secret has no 'credentials' key"
    )


def test_ungranted_optional_secret_blocks(ctx, workload):
    base = _configured()
    state = State(
        leader=True,
        # A well-formed ID for a secret the model never granted us.
        config={**base.config, "opencode-auth": _secret("auth-json").id},
        secrets=base.secrets,
    )

    out = ctx.run(ctx.on.config_changed(), state)

    assert out.unit_status == BlockedStatus("opencode-auth: secret not found or not granted")


def test_configure_failure_blocks(ctx, workload):
    workload.configure.side_effect = SystemdError("daemon-reload failed")

    out = ctx.run(ctx.on.config_changed(), _configured())

    assert out.unit_status == BlockedStatus("configuration failed, see juju debug-log")


def test_failed_last_pass_blocks(ctx, workload):
    workload.last_pass_failed = True

    out = ctx.run(ctx.on.update_status(), _configured())

    assert out.unit_status == BlockedStatus("last pass failed, see journalctl -u frontdesk")


def test_failed_last_pass_is_not_reported_with_mode_off(ctx, workload):
    workload.last_pass_failed = True

    out = ctx.run(ctx.on.update_status(), _configured("off"))

    assert isinstance(out.unit_status, ActiveStatus)


def test_running_pass_and_missing_ubuntu_lint_are_reported(ctx, workload):
    workload.pass_running = True
    workload.ubuntu_lint_installed = False
    state = _configured(
        lp_sponsor_credentials=("credentials", "s"), opencode_auth=("auth-json", "{}")
    )

    out = ctx.run(ctx.on.update_status(), state)

    assert out.unit_status == ActiveStatus(
        "dry-run every 60m; pass running; no ubuntu-lint (Check 13 silent)"
    )


def test_status_while_installing(ctx, workload):
    workload.installed = False

    out = ctx.run(ctx.on.install(), _state())

    assert out.unit_status == MaintenanceStatus("installing")


def test_remove_stops_the_timer(ctx, workload):
    ctx.run(ctx.on.remove(), _configured())

    workload.stop.assert_called_once_with()


# --- actions -------------------------------------------------------------------


def test_run_now_starts_a_pass(ctx, workload):
    ctx.run(ctx.on.action("run-now"), _configured("dry-run"))

    workload.start_pass.assert_called_once_with()
    assert "dry-run pass" in ctx.action_results["message"]


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda w: None, "mode is 'off'"),
        (lambda w: setattr(w, "pass_running", True), "already running"),
    ],
)
def test_run_now_refuses(ctx, workload, setup, reason):
    setup(workload)
    mode = "off" if reason.startswith("mode") else "dry-run"

    with pytest.raises(ActionFailed, match=reason):
        ctx.run(ctx.on.action("run-now"), _configured(mode))

    workload.start_pass.assert_not_called()


def test_run_now_refuses_without_credentials(ctx, workload):
    with pytest.raises(ActionFailed, match="lp-triager-credentials is not set"):
        ctx.run(ctx.on.action("run-now"), _state({"mode": "dry-run"}))

    workload.start_pass.assert_not_called()


def test_triage_is_always_a_forced_dry_run(ctx, workload):
    workload.run.return_value = subprocess.CompletedProcess([], 0, "", "log line\n")
    url = "https://bugs.launchpad.net/ubuntu/+source/hello/+bug/1"

    ctx.run(ctx.on.action("triage", params={"url": url, "verbose": True}), _configured("yes"))

    workload.run.assert_called_once_with(
        ["--url", url, "--dry-run", "--force", "--verbose"], timeout=1800
    )
    assert ctx.action_results == {"output": "log line\n", "exit-code": 0}


def test_triage_rejects_a_non_launchpad_url(ctx, workload):
    with pytest.raises(ActionFailed, match="Not a Launchpad URL"):
        ctx.run(ctx.on.action("triage", params={"url": "http://example.com"}), _configured())

    workload.run.assert_not_called()


def test_triage_failure_fails_the_action(ctx, workload):
    workload.run.return_value = subprocess.CompletedProcess([], 1, "", "auth failed\n")
    url = "https://code.launchpad.net/~a/ubuntu/+source/x/+git/x/+merge/1"

    with pytest.raises(ActionFailed, match="status 1"):
        ctx.run(ctx.on.action("triage", params={"url": url}), _configured())

    assert ctx.action_results["output"] == "auth failed\n"


def test_triage_timeout_fails_the_action(ctx, workload):
    workload.run.side_effect = subprocess.TimeoutExpired("frontdesk", 1800)
    url = "https://bugs.launchpad.net/bugs/1"

    with pytest.raises(ActionFailed, match="Timed out"):
        ctx.run(ctx.on.action("triage", params={"url": url}), _configured())


def test_stats_passes_its_flags(ctx, workload):
    workload.run.return_value = subprocess.CompletedProcess([], 0, "report\n", "")

    ctx.run(
        ctx.on.action("stats", params={"since": "30d", "queue": True, "json": True}),
        _state(),
    )

    workload.run.assert_called_once_with(
        ["stats", "--since", "30d", "--queue", "--json"], timeout=None
    )
    assert ctx.action_results["output"] == "report\n"


def test_stats_works_without_credentials(ctx, workload):
    workload.run.return_value = subprocess.CompletedProcess([], 0, "report\n", "")

    ctx.run(ctx.on.action("stats"), _state())

    workload.run.assert_called_once_with(["stats"], timeout=None)
