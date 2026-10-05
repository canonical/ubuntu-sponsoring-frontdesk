# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Deploy the charm and check the wiring end to end, with dummy secrets.

The tests run in order and build on each other (one deployment per module).
"""

import jubilant
import pytest

from .conftest import APP

UNIT = f"{APP}/0"
# launchpadlib's credentials-file shape, with a token Launchpad will refuse.
DUMMY_LP_CREDENTIALS = (
    "[1]\nconsumer_key = ubuntu-sponsoring-frontdesk\nconsumer_secret = \n"
    "access_token = dummy\naccess_secret = dummy\n"
)


def _status_message(juju):
    return juju.status().apps[APP].units[UNIT].workload_status.message


def _sh(juju, command):
    return juju.exec(command, unit=UNIT).stdout.strip()


def test_deploy_blocks_without_credentials(juju: jubilant.Juju, charm_path):
    juju.deploy(charm_path, APP)

    juju.wait(lambda s: jubilant.all_blocked(s, APP), error=jubilant.any_error, timeout=1800)
    assert _status_message(juju) == "lp-triager-credentials is not set"


def test_credentials_unblock_with_the_timer_off(juju: jubilant.Juju):
    uri = juju.add_secret("frontdesk-triager", {"credentials": DUMMY_LP_CREDENTIALS})
    juju.grant_secret(uri, APP)

    juju.config(APP, {"lp-triager-credentials": uri})

    juju.wait(lambda s: jubilant.all_active(s, APP), error=jubilant.any_error, timeout=600)
    assert _status_message(juju).startswith("mode=off, no scheduled passes")
    assert _sh(juju, "systemctl is-enabled frontdesk.timer || true") == "disabled"


def test_files_on_the_unit(juju: jubilant.Juju):
    assert _sh(juju, "stat -c '%U:%G %a' /etc/frontdesk/triager.credentials") == "root:ubuntu 640"
    assert _sh(juju, "stat -c '%U:%G %a' /var/lib/frontdesk") == "ubuntu:ubuntu 750"
    env = _sh(juju, "cat /etc/frontdesk/environment")
    assert 'SPONSORING_BOT_STATE="/var/lib/frontdesk/state.db"' in env
    agent = _sh(juju, "cat /home/ubuntu/.config/opencode/opencode.jsonc")
    assert '"*": false' in agent
    assert _sh(juju, "test -e /srv/frontdesk/usf/main.py && echo ok") == "ok"
    assert _sh(juju, "test -e /srv/frontdesk/usf/tests || echo absent") == "absent"
    # The bot's runtime imports resolve on the unit's system Python.
    imports = "import apt_pkg, launchpadlib, yaml, debian.changelog; print('ok')"
    assert _sh(juju, f'python3 -c "{imports}"') == "ok"
    assert _sh(juju, "sudo -u ubuntu -H opencode --version >/dev/null && echo ok") == "ok"


def test_wrapper_refuses_root(juju: jubilant.Juju):
    task = juju.exec("frontdesk --help; echo rc=$?", unit=UNIT)
    assert "rc=1" in task.stdout
    assert "run this as the ubuntu user" in task.stderr + task.stdout


def test_stats_runs_as_the_bot(juju: jubilant.Juju):
    with pytest.raises(jubilant.TaskError) as e:
        juju.run(UNIT, "stats")
    # No pass has run, so there is no audit trail yet -- but stats.py ran,
    # with the charm's paths.
    assert "No audit trail at /var/lib/frontdesk/audit.jsonl" in e.value.task.results["output"]


def test_run_now_refused_while_off(juju: jubilant.Juju):
    with pytest.raises(jubilant.TaskError) as e:
        juju.run(UNIT, "run-now")
    assert "mode is 'off'" in e.value.task.message


def test_dry_run_mode_enables_the_timer(juju: jubilant.Juju):
    juju.config(APP, {"mode": "dry-run", "run-interval": 120})

    juju.wait(
        lambda s: jubilant.all_active(s, APP) and _status_message(juju).startswith("dry-run"),
        timeout=600,
    )
    assert _status_message(juju).startswith("dry-run every 120m")
    assert _sh(juju, "systemctl is-active frontdesk.timer") == "active"
    service = _sh(juju, "systemctl cat frontdesk.service")
    assert "ExecStart=/usr/local/bin/frontdesk --all --dry-run" in service
    assert "OnUnitInactiveSec=120min" in _sh(juju, "systemctl cat frontdesk.timer")


def test_a_manual_run_and_a_pass_never_overlap(juju: jubilant.Juju):
    script = (
        "sudo -u ubuntu flock /var/lib/frontdesk/pass.lock sleep 60 & sleep 2; "
        "sudo -u ubuntu frontdesk --url https://bugs.launchpad.net/bugs/1; echo manual=$?; "
        "systemctl start frontdesk.service; echo service=$?; "
        "systemctl is-failed frontdesk.service; kill %1"
    )
    task = juju.exec(script, unit=UNIT)
    # Held by someone else: the manual run says so and stops; the timer's
    # pass is skipped without counting as a failure.
    assert "manual=75" in task.stdout
    assert "a pass is already running" in task.stderr
    assert "service=0" in task.stdout
    assert "\ninactive" in task.stdout


def test_triage_stops_at_launchpad_refusing_the_dummy_token(juju: jubilant.Juju):
    url = "https://bugs.launchpad.net/ubuntu/+bug/1"
    with pytest.raises(jubilant.TaskError) as e:
        juju.run(UNIT, "triage", {"url": url})
    # The whole chain ran -- wrapper, environment, the bot's imports -- up to
    # the identity check (#147) refusing the dummy token, once, before any
    # item is loaded.
    output = e.value.task.results["output"]
    assert "write mode: dry-run, audit: /var/lib/frontdesk/audit.jsonl" in output
    assert "Failed to authenticate to Launchpad: HTTP Error 401: Unauthorized" in output
    assert "Starting triage" not in output


def test_a_refused_token_fails_the_pass_visibly(juju: jubilant.Juju):
    juju.exec("systemctl start frontdesk.service || true", unit=UNIT)

    juju.wait(lambda s: jubilant.all_blocked(s, APP), timeout=600)
    assert _status_message(juju) == "last pass failed, see journalctl -u frontdesk"


def test_mode_off_disables_the_timer_again(juju: jubilant.Juju):
    juju.config(APP, {"mode": "off"})

    juju.wait(
        lambda s: jubilant.all_active(s, APP) and _status_message(juju).startswith("mode=off"),
        timeout=600,
    )
    assert _sh(juju, "systemctl is-enabled frontdesk.timer || true") == "disabled"
