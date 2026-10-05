# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Unit tests for the workload, run against a temporary root directory."""

import json
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest
from charmlibs import apt

import frontdesk
from frontdesk import PACKAGES, Frontdesk, InstallError, Secrets

REPO = Path(__file__).resolve().parents[2]


class FakeSystemd:
    """Records systemctl calls; knows which units are running."""

    class SystemdError(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.running = set()
        self.enabled = set()

    def daemon_reload(self):
        self.calls.append(("daemon-reload",))

    def service_enable(self, *args):
        self.calls.append(("enable", *args))
        self.running.add(args[-1])
        self.enabled.add(args[-1])

    def service_disable(self, *args):
        self.calls.append(("disable", *args))
        self.running.discard(args[-1])
        self.enabled.discard(args[-1])

    def service_restart(self, name):
        self.calls.append(("restart", name))

    def service_start(self, *args):
        self.calls.append(("start", *args))

    def service_running(self, name):
        return name in self.running

    def service_failed(self, name):
        return False


@pytest.fixture
def systemd(monkeypatch):
    fake = FakeSystemd()
    monkeypatch.setattr(frontdesk, "systemd", fake)
    monkeypatch.setattr(frontdesk, "_unit_enabled", lambda name: name in fake.enabled)
    return fake


@pytest.fixture
def fd(tmp_path, monkeypatch, systemd):
    monkeypatch.setattr(Frontdesk, "_chown", lambda self, path, user="", group="": None)
    home = tmp_path / "home" / "ubuntu"
    home.mkdir(parents=True)
    w = Frontdesk(root=tmp_path, charm_dir=REPO, home=home, environ={})
    for d in (w.config_dir, w.state_dir, w.cache_dir, w.systemd_dir):
        d.mkdir(parents=True)
    return w


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# --- install -------------------------------------------------------------------


@pytest.fixture
def installers(monkeypatch):
    calls = []
    monkeypatch.setattr(frontdesk.apt, "update", lambda: calls.append("apt-update"))
    monkeypatch.setattr(
        frontdesk.apt,
        "add_package",
        lambda pkgs, update_cache=False: calls.append(("apt", pkgs)),
    )
    monkeypatch.setattr(
        frontdesk.snap,
        "ensure_installed",
        lambda name, classic=False: calls.append(("snap", name, classic)),
    )
    monkeypatch.setattr(
        frontdesk.subprocess,
        "run",
        lambda cmd, **kw: calls.append(("run", *cmd)) or subprocess.CompletedProcess(cmd, 0),
    )
    return calls


def test_install_sets_up_packages_code_and_wrapper(fd, installers):
    fd.install()

    assert installers == [
        "apt-update",
        ("apt", PACKAGES),
        ("snap", "opencode", True),
        ("run", "add-apt-repository", "-y", "ppa:enr0n/ubuntu-lint"),
        ("apt", "python3-ubuntu-lint"),
    ]
    assert (fd.code_dir / "main.py").exists()
    assert not (fd.code_dir / "tests").exists()
    assert not (fd.code_dir / "conftest.py").exists()
    assert not list(fd.code_dir.rglob("__pycache__"))
    assert _mode(fd.wrapper) == 0o755
    assert fd.wrapper.read_text() == (REPO / "src/script/frontdesk").read_text()
    assert fd.installed


def test_install_replaces_the_code_wholesale(fd, installers):
    fd.code_dir.mkdir(parents=True)
    (fd.code_dir / "removed_module.py").write_text("")

    fd.install()

    assert not (fd.code_dir / "removed_module.py").exists()
    assert not fd.code_dir.with_name("usf.old").exists()
    assert not fd.code_dir.with_name("usf.new").exists()


def test_install_failure_leaves_it_uninstalled(fd, installers, monkeypatch):
    fd.install()

    def broken(*args, **kwargs):
        raise apt.PackageError("no network")

    monkeypatch.setattr(frontdesk.apt, "update", broken)

    with pytest.raises(InstallError, match="no network"):
        fd.install()
    assert not fd.installed


def test_ubuntu_lint_failure_does_not_fail_the_install(fd, installers, monkeypatch):
    def no_ppa(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(frontdesk.subprocess, "run", no_ppa)

    fd.install()

    assert fd.installed


# --- configure -----------------------------------------------------------------

SECRETS = Secrets(lp_triager="triager-token")


def test_environment_relocates_every_path(fd):
    fd.environ = {"JUJU_CHARM_HTTPS_PROXY": "http://proxy:3128"}

    fd.configure("dry-run", 60, SECRETS)

    env = fd.env_file.read_text()
    root = fd.root
    for line in (
        f'SPONSORING_BOT_STATE="{root}/var/lib/frontdesk/state.db"',
        f'SPONSORING_BOT_AUDIT="{root}/var/lib/frontdesk/audit.jsonl"',
        f'SPONSORING_BOT_CONFIG="{root}/etc/frontdesk/config.ini"',
        f'SPONSORING_BOT_LP_CREDENTIALS="{root}/etc/frontdesk/triager.credentials"',
        f'SPONSORING_BOT_LP_CACHE="{root}/var/cache/frontdesk/triager-launchpadlib"',
        f'SPONSORING_BOT_HELPER_LP_CREDENTIALS="{root}/etc/frontdesk/sponsor.credentials"',
        f'SPONSORING_BOT_HELPER_LP_CACHE="{root}/var/cache/frontdesk/sponsor-launchpadlib"',
        'https_proxy="http://proxy:3128"',
        'HTTPS_PROXY="http://proxy:3128"',
    ):
        assert line in env.splitlines()
    assert "/snap/bin" in env


def test_every_env_override_the_bot_reads_is_set(fd):
    """If the bot grows a new ~/.cache or ~/.config path, the charm must
    relocate it too -- otherwise it silently lands in ubuntu's home."""
    read = set()
    for module in (REPO / "usf").glob("*.py"):
        read |= set(re.findall(r'"(SPONSORING_BOT_[A-Z_]+)"', module.read_text()))
    # Deliberately left at its default: it must match the token's account.
    read.discard("SPONSORING_BOT_LP_USERNAME")

    assert read == {v for v in fd.environment() if v.startswith("SPONSORING_BOT_")}


def test_environment_values_are_quoted():
    assert frontdesk._env_quote('a"b$c`d\\e') == '"a\\"b\\$c\\`d\\\\e"'
    with pytest.raises(ValueError):
        frontdesk._env_quote("a\nb")


def test_secret_files_are_written_and_removed(fd):
    fd.configure(
        "dry-run",
        60,
        Secrets(lp_triager="t", lp_sponsor="s", webhook_url=" https://chat/hooks/x\n"),
    )

    assert fd.triager_credentials.read_text() == "t"
    assert fd.sponsor_credentials.read_text() == "s"
    assert _mode(fd.triager_credentials) == 0o640
    assert fd.notify_config.read_text() == (
        "[notifications]\nwebhook_url = https://chat/hooks/x\n"
    )

    fd.configure("dry-run", 60, Secrets(lp_triager="t"))

    # privileged_helper.py keys on the sponsor file merely existing.
    assert not fd.sponsor_credentials.exists()
    assert not fd.notify_config.exists()


def _agent(fd):
    text = fd.opencode_config.read_text()
    config = json.loads("".join(line for line in text.splitlines() if not line.startswith("//")))
    return config["agent"]["sponsoring-reviewer"]


def test_opencode_agent_is_tool_less(fd):
    fd.configure("dry-run", 60, SECRETS, llm_model="github-copilot/some-model")

    agent = _agent(fd)
    assert agent["tools"] == {"*": False}
    assert agent["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    assert agent["model"] == "github-copilot/some-model"

    fd.configure("dry-run", 60, SECRETS)

    assert "model" not in _agent(fd)


def test_opencode_agent_name_matches_the_bot():
    text = (REPO / "usf" / "llm_reviewer.py").read_text()
    assert f'_OPENCODE_AGENT = "{frontdesk.OPENCODE_AGENT}"' in text


def test_opencode_auth_follows_the_secret_not_the_file(fd):
    fd.configure("dry-run", 60, Secrets(lp_triager="t", opencode_auth="v1"))
    assert fd.opencode_auth.read_text() == "v1"
    assert _mode(fd.opencode_auth) == 0o600

    # opencode refreshes its token in place; an unchanged secret keeps that.
    fd.opencode_auth.write_text("v1-refreshed")
    fd.configure("dry-run", 60, Secrets(lp_triager="t", opencode_auth="v1"))
    assert fd.opencode_auth.read_text() == "v1-refreshed"

    fd.configure("dry-run", 60, Secrets(lp_triager="t", opencode_auth="v2"))
    assert fd.opencode_auth.read_text() == "v2"

    fd.configure("dry-run", 60, Secrets(lp_triager="t"))
    assert not fd.opencode_auth.exists()


@pytest.mark.parametrize("mode, flag", [("dry-run", "--dry-run"), ("yes", "--yes")])
def test_units_carry_mode_and_interval(fd, mode, flag):
    fd.configure(mode, 45, SECRETS)

    service = (fd.systemd_dir / "frontdesk.service").read_text()
    timer = (fd.systemd_dir / "frontdesk.timer").read_text()
    assert f"ExecStart=/usr/local/bin/frontdesk --all {flag}\n" in service
    assert "OnUnitInactiveSec=45min\n" in timer
    assert "__" not in service + timer


def test_timer_enabled_only_with_a_mode_and_credentials(fd, systemd):
    fd.configure("dry-run", 60, SECRETS)
    assert ("enable", "--now", "frontdesk.timer") in systemd.calls

    systemd.calls.clear()
    fd.configure("off", 60, SECRETS)
    assert ("disable", "--now", "frontdesk.timer") in systemd.calls

    fd.configure("dry-run", 60, SECRETS)
    systemd.calls.clear()
    fd.configure("yes", 60, Secrets())
    assert ("disable", "--now", "frontdesk.timer") in systemd.calls
    # The service is written in the requested mode all the same.
    assert "--yes" in (fd.systemd_dir / "frontdesk.service").read_text()


@pytest.mark.parametrize("mode", ["dry-run", "off"])
def test_reconfigure_without_changes_touches_nothing(fd, systemd, mode):
    fd.configure(mode, 60, SECRETS)
    systemd.calls.clear()

    fd.configure(mode, 60, SECRETS)

    # Not even a no-op enable: it reloads the daemon, and this runs on every
    # update-status (#146).
    assert systemd.calls == []


def test_new_interval_only_reloads(fd, systemd):
    fd.configure("dry-run", 60, SECRETS)
    systemd.calls.clear()

    fd.configure("dry-run", 30, SECRETS)

    # A reload applies the new OnUnitInactiveSec; a restart would fire the
    # (past) OnBootSec, starting a pass nobody asked for.
    assert systemd.calls == [("daemon-reload",)]


def test_mode_change_only_reloads(fd, systemd):
    fd.configure("dry-run", 60, SECRETS)
    systemd.calls.clear()

    fd.configure("yes", 60, SECRETS)

    assert systemd.calls == [("daemon-reload",)]


def test_timer_is_not_rearmed_by_reloads():
    """OnActiveSec is re-armed by every daemon-reload -- seen live, it kept
    pushing the first pass back on each update-status (#146)."""
    timer = (REPO / "src/systemd/frontdesk.timer").read_text()
    settings = [line for line in timer.splitlines() if line and not line.startswith("#")]
    assert "OnBootSec=5min" in settings
    assert not [line for line in settings if line.startswith("OnActiveSec")]


def test_disabled_timer_left_alone(fd, systemd):
    fd.configure("off", 60, SECRETS)

    assert ("disable", "--now", "frontdesk.timer") not in systemd.calls


def test_stop_disables_only_a_live_timer(fd, systemd):
    fd.stop()
    assert systemd.calls == []

    fd.configure("dry-run", 60, SECRETS)
    systemd.calls.clear()
    fd.stop()
    assert systemd.calls == [("disable", "--now", "frontdesk.timer")]


def test_invalid_mode_is_refused(fd):
    with pytest.raises(ValueError):
        fd.configure("interactive", 60, SECRETS)


# --- runtime -------------------------------------------------------------------


def test_run_goes_through_the_wrapper_as_ubuntu(fd, monkeypatch):
    seen = {}

    def fake_run_group(cmd, timeout=None):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return subprocess.CompletedProcess(cmd, 0, "out", "")

    monkeypatch.setattr(frontdesk, "_run_group", fake_run_group)

    fd.run(["stats", "--queue"], timeout=60)

    assert seen["cmd"] == ["runuser", "-u", "ubuntu", "--", str(fd.wrapper), "stats", "--queue"]
    assert seen["timeout"] == 60


def test_run_group_returns_the_output():
    proc = frontdesk._run_group(["sh", "-c", "echo out; echo err >&2; exit 3"])

    assert (proc.returncode, proc.stdout, proc.stderr) == (3, "out\n", "err\n")


def test_run_group_timeout_kills_the_whole_tree(tmp_path):
    """A timeout used to kill only runuser: flock and the bot under it kept
    the lock and the output pipe, so the action didn't return (#146)."""
    pidfile = tmp_path / "child.pid"
    # The grandchild stands in for the bot under runuser/flock.
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    start = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        frontdesk._run_group(["sh", "-c", script], timeout=1)

    assert time.monotonic() - start < 10
    child = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


def _alive(pid):
    # The process can vanish mid-read: ENOENT or ESRCH both mean it's gone.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


@pytest.mark.parametrize(
    "state, running", [("activating", True), ("inactive", False), ("failed", False)]
)
def test_pass_running_means_the_oneshot_is_activating(fd, monkeypatch, state, running):
    """is-active is never true for a running oneshot (#146, seen live)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, state + "\n", "")

    monkeypatch.setattr(frontdesk.subprocess, "run", fake_run)

    assert fd.pass_running is running
    assert seen["cmd"][-1] == "frontdesk.service"


def test_start_pass_does_not_block(fd, systemd):
    fd.start_pass()

    assert systemd.calls == [("start", "--no-block", "frontdesk.service")]
