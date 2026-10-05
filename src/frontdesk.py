# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""The Frontdesk workload: everything the charm does on the machine.

Deliberately free of ops, so it can be read (and tested) as "what ends up on
the unit": packages, the bot's code, the files that point it at its state and
credentials, and the systemd timer that runs it.

Layout on the unit (#146):

    /srv/frontdesk/usf/        the bot, copied from the charm's usf/
    /usr/local/bin/frontdesk   wrapper: the timer's and a human's entry point
    /etc/frontdesk/            environment + secrets (root:ubuntu, read-only to the bot)
    /var/lib/frontdesk/        state.db, audit.jsonl -- what a backup must keep
    /var/cache/frontdesk/      launchpadlib caches -- disposable
    ~ubuntu/.config/opencode/  the tool-less sponsoring-reviewer agent (#99)
    ~ubuntu/.local/share/opencode/auth.json   opencode's login

Secrets and state are kept in separate trees on purpose: a backup of
/var/lib/frontdesk carries the bot's history without its tokens.
"""

import hashlib
import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from charmlibs import apt, snap, systemd

logger = logging.getLogger(__name__)

USER = "ubuntu"

# Runtime dependencies of the bot. System Python, no virtualenv: apt_pkg
# (python3-apt) is a compiled extension tied to the system interpreter.
# git is for git_history.py's sandboxed ancestry check (#49).
PACKAGES = [
    "distro-info",
    "git",
    "python3-apt",
    "python3-debian",
    "python3-launchpadlib",
    "python3-yaml",
]

# Check 13 delegates to ubuntu-lint, not yet in the 26.04 archive. Optional:
# without it the check degrades to silence. Drop the PPA once the package is
# SRUed to 26.04 (doc/STATUS.md).
UBUNTU_LINT_PPA = "ppa:enr0n/ubuntu-lint"
UBUNTU_LINT_PACKAGE = "python3-ubuntu-lint"

OPENCODE_SNAP = "opencode"
# The agent llm_reviewer.py runs every LLM call under. It is a security
# control, not a preference: prompts embed contributor-written text, so the
# model must not be able to act (design_journal.md #99). The charm owns the
# file and rewrites it, so a hand edit can't quietly re-enable tools.
OPENCODE_AGENT = "sponsoring-reviewer"

MODES = ("off", "dry-run", "yes")

SERVICE = "frontdesk.service"
TIMER = "frontdesk.timer"

_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"


def _unit_enabled(name):
    """`systemctl is-enabled`, which charmlibs.systemd doesn't wrap."""
    return subprocess.run(["systemctl", "is-enabled", "--quiet", name]).returncode == 0


def _active_state(name):
    """systemd's ActiveState for a unit ("active", "activating", ...)."""
    proc = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", name],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _run_group(cmd, timeout=None):
    """subprocess.run(capture_output=True), but a timeout kills the whole
    process tree, not just ``cmd``.

    subprocess.run's timeout kills only the process it started -- here
    runuser -- while flock and the bot under it keep running, holding the
    pass lock and the output pipe, so the call doesn't return until the
    bot finishes on its own (#146 follow-up). Started in its own session,
    the tree shares one process group that can be killed at once.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


class InstallError(Exception):
    """Setting up the machine failed; the charm retries on the next event."""


@dataclass(frozen=True)
class Secrets:
    """Secret material from the charm's secret config options; None = unset."""

    lp_triager: str | None = None
    lp_sponsor: str | None = None
    opencode_auth: str | None = None
    webhook_url: str | None = None


def _env_quote(value):
    """Double-quote a value for a file read by both sh and systemd."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"newline in environment value {value!r}")
    for ch in ("\\", '"', "$", "`"):
        value = value.replace(ch, "\\" + ch)
    return f'"{value}"'


class Frontdesk:
    """The machine side of the charm.

    ``root`` prefixes every absolute path, so tests can run it against a
    temporary directory; on a unit it is ``/``.
    """

    def __init__(self, root=Path("/"), charm_dir=None, home=None, environ=None):
        self.root = Path(root)
        self.charm_dir = Path(charm_dir) if charm_dir else Path(__file__).resolve().parent.parent
        self._home = Path(home) if home else None
        self.environ = os.environ if environ is None else environ

        self.code_dir = self._p("/srv/frontdesk/usf")
        self.installed_marker = self._p("/srv/frontdesk/.installed")
        self.wrapper = self._p("/usr/local/bin/frontdesk")
        self.config_dir = self._p("/etc/frontdesk")
        self.state_dir = self._p("/var/lib/frontdesk")
        self.cache_dir = self._p("/var/cache/frontdesk")
        self.systemd_dir = self._p("/etc/systemd/system")

        self.env_file = self.config_dir / "environment"
        self.triager_credentials = self.config_dir / "triager.credentials"
        self.sponsor_credentials = self.config_dir / "sponsor.credentials"
        self.notify_config = self.config_dir / "config.ini"
        self.opencode_auth_marker = self.config_dir / "opencode-auth.sha256"

    def _p(self, path):
        return self.root / Path(path).relative_to("/")

    @property
    def home(self):
        if self._home is None:
            self._home = Path(pwd.getpwnam(USER).pw_dir)
        return self._home

    @property
    def opencode_config(self):
        return self.home / ".config" / "opencode" / "opencode.jsonc"

    @property
    def opencode_auth(self):
        return self.home / ".local" / "share" / "opencode" / "auth.json"

    # --- ownership helpers (stubbed in tests: they can't chown) -------------

    def _chown(self, path, user=USER, group=USER):
        shutil.chown(path, user, group)

    def _mkdir(self, path, mode=0o755, user=None, group=None):
        """Create ``path`` and any missing parents, each owned as asked."""
        missing = []
        p = Path(path)
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            d.mkdir(mode=mode)
            if user:
                self._chown(d, user, group or user)
        os.chmod(path, mode)
        if user:
            self._chown(path, user, group or user)

    def _write(self, path, content, mode=0o644, user=None, group=None):
        """Write ``content`` atomically if it differs; return whether it did."""
        path = Path(path)
        try:
            if path.read_text() == content:
                os.chmod(path, mode)
                if user:
                    self._chown(path, user, group or user)
                return False
        except FileNotFoundError:
            pass
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        if user:
            self._chown(tmp, user, group or user)
        os.replace(tmp, path)
        return True

    @staticmethod
    def _remove(path):
        """Remove ``path`` if present; return whether it was."""
        try:
            Path(path).unlink()
            return True
        except FileNotFoundError:
            return False

    # --- install -------------------------------------------------------------

    @property
    def installed(self):
        return self.installed_marker.exists()

    def install(self):
        """Packages, the bot's code and the wrapper. Safe to re-run (upgrade).

        Raises InstallError; the marker is only written once everything
        succeeded, so a failed install is retried by the next reconcile.
        """
        self._remove(self.installed_marker)
        try:
            self._install_packages()
            self._install_opencode()
            self._setup_directories()
            self._install_code()
            self._install_wrapper()
        except (
            apt.PackageError,
            apt.PackageNotFoundError,
            snap.Error,
            subprocess.CalledProcessError,
            OSError,
        ) as e:
            raise InstallError(str(e)) from e
        # Best effort, after the essentials: Check 13 degrades to silence
        # without it, so this must not fail the install.
        self._install_ubuntu_lint()
        self._write(self.installed_marker, self._charm_version() + "\n")

    def _charm_version(self):
        try:
            return (self.charm_dir / "version").read_text().strip()
        except OSError:
            return "unknown"

    def _install_packages(self):
        apt.update()
        apt.add_package(PACKAGES)

    def _install_opencode(self):
        snap.ensure_installed(OPENCODE_SNAP, classic=True)

    def _install_ubuntu_lint(self):
        try:
            subprocess.run(
                ["add-apt-repository", "-y", UBUNTU_LINT_PPA],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            apt.add_package(UBUNTU_LINT_PACKAGE, update_cache=True)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            apt.PackageError,
            apt.PackageNotFoundError,
            OSError,
        ) as e:
            logger.warning("ubuntu-lint unavailable, Check 13 will stay silent: %s", e)

    @property
    def ubuntu_lint_installed(self):
        return self._p("/usr/lib/python3/dist-packages/ubuntu_lint").is_dir()

    def _setup_directories(self):
        self._mkdir(self.code_dir.parent, 0o755)
        # The bot reads its environment and secrets but cannot change them.
        self._mkdir(self.config_dir, 0o750, "root", USER)
        self._mkdir(self.state_dir, 0o750, USER)
        self._mkdir(self.cache_dir, 0o750, USER)

    def _install_code(self):
        """Replace /srv/frontdesk/usf wholesale, so no stale module survives.

        Built beside the live copy and swapped in, so a pass that is running
        during an upgrade loses at most its view of files it hadn't opened.
        """
        new = self.code_dir.with_name("usf.new")
        old = self.code_dir.with_name("usf.old")
        shutil.rmtree(new, ignore_errors=True)
        shutil.copytree(
            self.charm_dir / "usf",
            new,
            ignore=shutil.ignore_patterns("__pycache__", "tests", "conftest.py"),
        )
        if self.code_dir.exists():
            shutil.rmtree(old, ignore_errors=True)
            self.code_dir.rename(old)
        new.rename(self.code_dir)
        shutil.rmtree(old, ignore_errors=True)

    def _install_wrapper(self):
        content = (self.charm_dir / "src" / "script" / "frontdesk").read_text()
        self._mkdir(self.wrapper.parent, 0o755)
        self._write(self.wrapper, content, 0o755)

    # --- configure -----------------------------------------------------------

    def configure(self, mode, interval, secrets, llm_model=""):
        """Make the machine match the charm's config.

        Runs on every hook, update-status included, so when nothing changed
        it must change nothing: files are only rewritten when their content
        differs, and systemd is only touched when a unit file or the timer's
        enabled state has to change (even a no-op `systemctl enable` reloads
        the daemon)."""
        if mode not in MODES:
            raise ValueError(f"invalid mode {mode!r}")
        self._write_secrets(secrets)
        self._write_opencode_config(llm_model)
        self._write_environment()
        enabled = mode != "off" and secrets.lp_triager is not None
        self._apply_units(mode, interval, enabled)

    def _write_secrets(self, secrets):
        # Owned by root, group-readable by the bot: it reads them, nothing
        # it runs can rewrite them. Unset means removed -- the charm's config
        # is the only source of truth, and privileged_helper.py keys on the
        # sponsor file merely existing.
        for path, content in (
            (self.triager_credentials, secrets.lp_triager),
            (self.sponsor_credentials, secrets.lp_sponsor),
        ):
            if content is None:
                self._remove(path)
            else:
                self._write(path, content, 0o640, "root", USER)

        if secrets.webhook_url is None:
            self._remove(self.notify_config)
        else:
            url = secrets.webhook_url.strip()
            if "\n" in url:
                raise ValueError("webhook URL spans several lines")
            # notify.py reads [notifications] webhook_url (#48).
            ini = f"[notifications]\nwebhook_url = {url}\n"
            self._write(self.notify_config, ini, 0o640, "root", USER)

        self._write_opencode_auth(secrets.opencode_auth)

    def _write_opencode_auth(self, content):
        """opencode refreshes short-lived tokens in auth.json itself, so the
        file legitimately drifts from the secret. Write it only when the
        SECRET changed (tracked by hash), not whenever the file differs --
        otherwise every update-status would roll back a refreshed token."""
        if content is None:
            self._remove(self.opencode_auth)
            self._remove(self.opencode_auth_marker)
            return
        digest = hashlib.sha256(content.encode()).hexdigest()
        try:
            unchanged = self.opencode_auth_marker.read_text().strip() == digest
        except FileNotFoundError:
            unchanged = False
        if unchanged and self.opencode_auth.exists():
            return
        self._mkdir(self.opencode_auth.parent, 0o700, USER)
        self._write(self.opencode_auth, content, 0o600, USER)
        self._write(self.opencode_auth_marker, digest + "\n", 0o600, "root", "root")

    def opencode_config_text(self, llm_model=""):
        agent = {
            "description": "Tool-less text reviewer for the Ubuntu sponsoring bot",
            "mode": "primary",
            "tools": {"*": False},
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        }
        if llm_model:
            agent["model"] = llm_model
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agent": {OPENCODE_AGENT: agent},
        }
        header = (
            "// Written by the ubuntu-sponsoring-frontdesk charm; edits are overwritten.\n"
            "// The tool-less agent is a security control (design_journal.md #99):\n"
            "// set the model with `juju config <app> llm-model=...`.\n"
        )
        return header + json.dumps(config, indent=2) + "\n"

    def _write_opencode_config(self, llm_model):
        self._mkdir(self.opencode_config.parent, 0o755, USER)
        self._write(self.opencode_config, self.opencode_config_text(llm_model), 0o644, USER)

    def environment(self):
        """The variables the wrapper exports: every path the bot would
        otherwise default under ~/.cache or ~/.config, plus Juju's proxy."""
        env = {
            "PATH": _SYSTEM_PATH,
            "PYTHONUNBUFFERED": "1",
            # /srv/frontdesk/usf is root-owned; don't try to write .pyc there.
            "PYTHONDONTWRITEBYTECODE": "1",
            "SPONSORING_BOT_STATE": str(self.state_dir / "state.db"),
            "SPONSORING_BOT_AUDIT": str(self.state_dir / "audit.jsonl"),
            "SPONSORING_BOT_CONFIG": str(self.notify_config),
            "SPONSORING_BOT_LP_CREDENTIALS": str(self.triager_credentials),
            "SPONSORING_BOT_LP_CACHE": str(self.cache_dir / "triager-launchpadlib"),
            "SPONSORING_BOT_HELPER_LP_CREDENTIALS": str(self.sponsor_credentials),
            "SPONSORING_BOT_HELPER_LP_CACHE": str(self.cache_dir / "sponsor-launchpadlib"),
        }
        for juju_var, names in (
            ("JUJU_CHARM_HTTP_PROXY", ("http_proxy", "HTTP_PROXY")),
            ("JUJU_CHARM_HTTPS_PROXY", ("https_proxy", "HTTPS_PROXY")),
            ("JUJU_CHARM_NO_PROXY", ("no_proxy", "NO_PROXY")),
        ):
            value = self.environ.get(juju_var)
            if value:
                env.update(dict.fromkeys(names, value))
        return env

    def _write_environment(self):
        lines = ["# Written by the ubuntu-sponsoring-frontdesk charm; edits are overwritten."]
        lines += [f"{k}={_env_quote(v)}" for k, v in self.environment().items()]
        self._write(self.env_file, "\n".join(lines) + "\n", 0o644)

    def _unit_text(self, name, mode, interval):
        text = (self.charm_dir / "src" / "systemd" / name).read_text()
        # "off" never runs from the timer; a manual `systemctl start` then
        # gets the harmless mode rather than an error.
        flag = "--yes" if mode == "yes" else "--dry-run"
        return text.replace("__MODE_FLAG__", flag).replace("__INTERVAL__", str(int(interval)))

    def _apply_units(self, mode, interval, enabled):
        service_changed = self._write(
            self.systemd_dir / SERVICE, self._unit_text(SERVICE, mode, interval)
        )
        timer_changed = self._write(
            self.systemd_dir / TIMER, self._unit_text(TIMER, mode, interval)
        )
        if service_changed or timer_changed:
            # Enough on its own for a new interval to apply to the running
            # timer -- no restart, which would also fire OnBootSec again
            # (it is in the past), i.e. start an unasked-for pass.
            systemd.daemon_reload()
        active = systemd.service_running(TIMER)
        if enabled and not (active and _unit_enabled(TIMER)):
            systemd.service_enable("--now", TIMER)
        elif not enabled and (active or _unit_enabled(TIMER)):
            systemd.service_disable("--now", TIMER)

    # --- runtime -------------------------------------------------------------

    @property
    def timer_active(self):
        return systemd.service_running(TIMER)

    @property
    def pass_running(self):
        # A oneshot service is "activating" for as long as it runs, never
        # "active" -- so not systemd.service_running(), which is is-active.
        return _active_state(SERVICE) == "activating"

    @property
    def last_pass_failed(self):
        return systemd.service_failed(SERVICE)

    def start_pass(self):
        """Start a queue pass in the background (the run-now action)."""
        systemd.service_start("--no-block", SERVICE)

    def run(self, args, timeout=None):
        """Run the wrapper as the bot's user; returns the CompletedProcess.

        Actions run as root, and a root-run bot would leave a root-owned
        state.db behind that the timer can no longer write.
        """
        return _run_group(["runuser", "-u", USER, "--", str(self.wrapper), *args], timeout)

    def stop(self):
        """Stop scheduling passes (bad config, unit removal). A running pass
        finishes."""
        if systemd.service_running(TIMER) or _unit_enabled(TIMER):
            systemd.service_disable("--now", TIMER)
