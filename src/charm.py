#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Charm for Frontdesk, the Ubuntu sponsoring queue triage bot.

A thin ops layer over frontdesk.Frontdesk: every event reconciles the machine
against the config, and the status is computed afresh at the end of each hook
(collect-status) rather than set along the way, so one handler can't
overwrite another's Blocked.
"""

import logging
import subprocess
from dataclasses import dataclass, field

import ops
from charmlibs.systemd import SystemdError

from frontdesk import MODES, Frontdesk, InstallError, Secrets

logger = logging.getLogger(__name__)

# Secret config option -> the key its content lives under.
SECRET_KEYS = {
    "lp-triager-credentials": "credentials",
    "lp-sponsor-credentials": "credentials",
    "opencode-auth": "auth-json",
    "mattermost-webhook": "webhook-url",
}

# Action output is returned through Juju; keep it to something a terminal can
# show. The tail is what matters (the outcome is logged last).
_MAX_OUTPUT = 60_000
_TRIAGE_TIMEOUT = 30 * 60


@dataclass
class Settings:
    """The charm config, read and checked once per hook."""

    mode: str = "off"
    interval: int = 60
    llm_model: str = ""
    secrets: Secrets = field(default_factory=Secrets)
    # Each one is a reason the config can't be applied as given.
    problems: list[str] = field(default_factory=list)


def _tail(text):
    if len(text) <= _MAX_OUTPUT:
        return text
    return "[... output truncated ...]\n" + text[-_MAX_OUTPUT:]


class FrontdeskCharm(ops.CharmBase):
    """Run Frontdesk's queue pass on a systemd timer."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.workload = Frontdesk()
        self._settings_cache = None
        self._install_error = None
        self._configure_error = None

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        for event in (
            self.on.start,
            self.on.config_changed,
            self.on.secret_changed,
            self.on.update_status,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.remove, self._on_remove)
        framework.observe(self.on.collect_unit_status, self._on_collect_status)

        framework.observe(self.on.run_now_action, self._on_run_now)
        framework.observe(self.on.triage_action, self._on_triage)
        framework.observe(self.on.stats_action, self._on_stats)

    # --- config ----------------------------------------------------------------

    def _read_secret(self, option, problems):
        secret_id = self.config.get(option)
        if not secret_id:
            return None
        key = SECRET_KEYS[option]
        try:
            content = self.model.get_secret(id=str(secret_id)).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            logger.warning("Cannot read secret %s for %s: %s", secret_id, option, e)
            problems.append(f"{option}: secret not found or not granted")
            return None
        if not content.get(key):
            problems.append(f"{option}: secret has no '{key}' key")
            return None
        return content[key]

    @property
    def settings(self):
        if self._settings_cache is not None:
            return self._settings_cache
        problems = []
        mode = str(self.config.get("mode", "off")).strip()
        if mode not in MODES:
            problems.append(f"invalid mode {mode!r}: use one of {', '.join(MODES)}")
        interval = int(self.config.get("run-interval", 60))
        if interval < 1:
            problems.append("run-interval must be at least 1 (minutes)")

        secrets = Secrets(
            **{
                attr: self._read_secret(option, problems)
                for attr, option in (
                    ("lp_triager", "lp-triager-credentials"),
                    ("lp_sponsor", "lp-sponsor-credentials"),
                    ("opencode_auth", "opencode-auth"),
                    ("webhook_url", "mattermost-webhook"),
                )
            }
        )
        if not self.config.get("lp-triager-credentials"):
            problems.append("lp-triager-credentials is not set")

        self._settings_cache = Settings(
            mode=mode,
            interval=interval,
            llm_model=str(self.config.get("llm-model", "")).strip(),
            secrets=secrets,
            problems=problems,
        )
        return self._settings_cache

    @property
    def _config_valid(self):
        """Mode and interval are usable (secrets are judged separately)."""
        s = self.settings
        return s.mode in MODES and s.interval >= 1

    # --- events ----------------------------------------------------------------

    def _install(self):
        self.unit.status = ops.MaintenanceStatus("installing")
        try:
            self.workload.install()
            self._install_error = None
        except InstallError as e:
            logger.error("Install failed (retried on the next event): %s", e)
            self._install_error = str(e)

    def _on_install(self, event):
        self._install()
        self._reconcile(event)

    def _reconcile(self, _event):
        if not self.workload.installed:
            self._install()
            if not self.workload.installed:
                return
        s = self.settings
        try:
            if not self._config_valid:
                # Don't guess what a malformed config meant: stop scheduling
                # passes until it is fixed. A running pass finishes.
                self.workload.stop()
                return
            self.workload.configure(s.mode, s.interval, s.secrets, s.llm_model)
            self._configure_error = None
        except (ValueError, OSError, SystemdError) as e:
            logger.error("Configuring the workload failed: %s", e)
            self._configure_error = str(e)

    def _on_remove(self, _event):
        try:
            self.workload.stop()
        except SystemdError as e:
            logger.warning("Could not stop the timer: %s", e)

    def _on_collect_status(self, event: ops.CollectStatusEvent):
        if not self.workload.installed:
            if self._install_error is not None:
                event.add_status(
                    ops.BlockedStatus("installation failed, see juju debug-log (retrying)")
                )
            else:
                event.add_status(ops.MaintenanceStatus("installing"))
            return

        s = self.settings
        for problem in s.problems:
            event.add_status(ops.BlockedStatus(problem))
        if self._configure_error:
            event.add_status(ops.BlockedStatus("configuration failed, see juju debug-log"))
        try:
            if self.workload.last_pass_failed:
                event.add_status(
                    ops.BlockedStatus("last pass failed, see journalctl -u frontdesk")
                )
            running = self.workload.pass_running
        except SystemdError:
            running = False
        event.add_status(ops.ActiveStatus(self._active_message(s, running)))

    def _active_message(self, s, running):
        if s.mode == "off":
            parts = ["mode=off, no scheduled passes"]
        else:
            parts = [f"{s.mode} every {s.interval}m"]
        if running:
            parts.append("pass running")
        if s.secrets.opencode_auth is None:
            parts.append("LLM review off (no opencode-auth)")
        if s.secrets.lp_sponsor is None:
            parts.append("no lp-sponsor-credentials")
        if not self.workload.ubuntu_lint_installed:
            parts.append("no ubuntu-lint (Check 13 silent)")
        return "; ".join(parts)

    # --- actions ---------------------------------------------------------------

    def _ready_for_action(self, event):
        if not self.workload.installed:
            event.fail("The charm is not installed yet.")
            return False
        if self.settings.problems:
            event.fail("Fix the configuration first: " + "; ".join(self.settings.problems))
            return False
        return True

    def _on_run_now(self, event: ops.ActionEvent):
        if not self._ready_for_action(event):
            return
        if self.settings.mode == "off":
            event.fail("mode is 'off': set mode to dry-run or yes to run passes.")
            return
        if self.workload.pass_running:
            event.fail("A pass is already running (journalctl -u frontdesk -f).")
            return
        self.workload.start_pass()
        mode = self.settings.mode
        message = f"Started a {mode} pass; follow it with: journalctl -u frontdesk -f"
        event.set_results({"message": message})

    def _on_triage(self, event: ops.ActionEvent):
        url = str(event.params["url"]).strip()
        if not url.startswith("https://") or "launchpad.net/" not in url:
            event.fail(f"Not a Launchpad URL: {url!r}")
            return
        if not self._ready_for_action(event):
            return
        # Always --dry-run: writes are a human's call, made over `juju ssh`
        # with --interactive. --force: --url honours the facts-unchanged gate,
        # so an item the timer already saw would otherwise just be skipped.
        args = ["--url", url, "--dry-run", "--force"]
        if event.params.get("verbose"):
            args.append("--verbose")
        event.log(f"Triaging {url} (dry-run)")
        self._run_and_report(event, args, timeout=_TRIAGE_TIMEOUT)

    def _on_stats(self, event: ops.ActionEvent):
        if not self.workload.installed:
            event.fail("The charm is not installed yet.")
            return
        args = ["stats"]
        if event.params.get("since"):
            args += ["--since", str(event.params["since"])]
        if event.params.get("queue"):
            args.append("--queue")
        if event.params.get("json"):
            args.append("--json")
        self._run_and_report(event, args)

    def _run_and_report(self, event, args, timeout=None):
        try:
            proc = self.workload.run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            event.fail(f"Timed out after {timeout} seconds.")
            return
        # main.py logs to stderr, stats.py prints to stdout: return both.
        output = (proc.stdout or "") + (proc.stderr or "")
        event.set_results({"output": _tail(output), "exit-code": proc.returncode})
        if proc.returncode != 0:
            event.fail(f"Exited with status {proc.returncode}; see the output.")


if __name__ == "__main__":  # pragma: nocover
    ops.main(FrontdeskCharm)
