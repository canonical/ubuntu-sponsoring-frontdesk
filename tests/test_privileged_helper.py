"""Design #78: the ~ubuntu-sponsors unsubscribe is delegated to
privileged_helper.py (separate process, separate token from a team-member
account) so the bot account itself can leave the team -- a member's MP vote
claims the team review slot and permanently drops the MP from the
sponsoring report."""

import types

from audit import AuditLog
import launchpad_client
from launchpad_client import LPClient
import privileged_helper
from fakes import FakeBug, FakeRoot


def _client(tmp_path):
    audit = AuditLog(path=str(tmp_path / "audit.jsonl"))
    return LPClient(mode="yes", audit=audit, lp=FakeRoot())


class _CountingBug(FakeBug):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.unsubscribes = 0

    def unsubscribe(self, person=None):
        self.unsubscribes += 1


# --- delegation from LPClient.unsubscribe_sponsors ---------------------------


def test_configured_helper_is_invoked_instead_of_a_direct_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launchpad_client, "_helper_configured", lambda: True)
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="unsubscribed", stderr="")

    monkeypatch.setattr(launchpad_client.subprocess, "run", fake_run)
    bug = _CountingBug(id=2160299)
    client = _client(tmp_path)
    client.unsubscribe_sponsors(bug)

    assert bug.unsubscribes == 0  # not done through the bot's own session
    assert calls["cmd"][-2:] == ["unsubscribe-sponsors", "2160299"]
    assert launchpad_client._HELPER_SCRIPT in calls["cmd"]
    assert client.write_outcomes == ["performed"]


def test_helper_failure_records_an_ineffective_write(tmp_path, monkeypatch):
    monkeypatch.setattr(launchpad_client, "_helper_configured", lambda: True)
    monkeypatch.setattr(
        launchpad_client.subprocess,
        "run",
        lambda cmd, **kwargs: types.SimpleNamespace(
            returncode=1, stdout="", stderr="helper failed: no such token"
        ),
    )
    client = _client(tmp_path)
    client.unsubscribe_sponsors(_CountingBug(id=1))

    # error outcome -> all_writes_effective() False -> facts withheld (#36).
    assert client.write_outcomes == ["error"]
    assert client.all_writes_effective() is False


def test_unconfigured_helper_falls_back_to_the_direct_call(tmp_path, monkeypatch):
    # Transition behavior while the bot account is still a team member.
    monkeypatch.setattr(launchpad_client, "_helper_configured", lambda: False)
    bug = _CountingBug(id=1)
    client = _client(tmp_path)
    client.unsubscribe_sponsors(bug)
    assert bug.unsubscribes == 1
    assert client.write_outcomes == ["performed"]


# --- the helper script itself -------------------------------------------------


def _helper_lp(bug):
    people = {"ubuntu-sponsors": object()}
    return types.SimpleNamespace(
        bugs={2160299: bug},
        people=people,
        me=types.SimpleNamespace(name="helper-account"),
    )


def test_helper_unsubscribes_and_exits_zero(monkeypatch, capsys):
    bug = _CountingBug(id=2160299)
    monkeypatch.setattr(privileged_helper, "_login", lambda: _helper_lp(bug))
    assert privileged_helper.main(["unsubscribe-sponsors", "2160299"]) == 0
    assert bug.unsubscribes == 1
    assert "helper-account" in capsys.readouterr().out


def test_helper_failure_exits_nonzero(monkeypatch, capsys):
    def _boom():
        raise TimeoutError("launchpad timeout")

    monkeypatch.setattr(privileged_helper, "_login", _boom)
    assert privileged_helper.main(["unsubscribe-sponsors", "1"]) == 1
    assert "helper failed" in capsys.readouterr().err
