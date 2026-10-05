"""#147: main() checks the Launchpad token before starting a pass.

login_with() never validates a stored token, so a revoked or bogus one used
to "log in" fine and then fail every item on its own 401 while the pass
exited 0 -- under the charm, an Active unit doing nothing useful.
"""

import pytest
from fakes import FakeRoot

import main


class _RefusedMe(FakeRoot):
    @property
    def me(self):
        raise Exception("HTTP Error 401: Unauthorized\nResponse headers:\n---\nserver: gunicorn")

    @me.setter
    def me(self, value):
        pass


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    monkeypatch.setenv("SPONSORING_BOT_STATE", str(tmp_path / "state.db"))
    monkeypatch.setenv("SPONSORING_BOT_AUDIT", str(tmp_path / "audit.jsonl"))
    passes = []
    monkeypatch.setattr(main, "process_queue", lambda *a, **kw: passes.append("queue"))
    monkeypatch.setattr(main.sweep, "sweep_bounced_bugs", lambda *a, **kw: passes.append("sweep"))

    def run(lp):
        monkeypatch.setattr(main, "LPClient", lambda mode, audit: _Client(lp, mode, audit))
        monkeypatch.setattr("sys.argv", ["main.py", "--all"])
        main.main()
        return passes

    return run


class _Client:
    def __init__(self, lp, mode, audit):
        self.lp = lp
        self.mode = mode
        self.audit = audit


def test_refused_token_exits_before_the_pass(run_main, caplog):
    with pytest.raises(SystemExit) as e:
        run_main(_RefusedMe())

    assert e.value.code == 1
    # One line naming the refusal, not launchpadlib's header dump.
    assert "Failed to authenticate to Launchpad: HTTP Error 401: Unauthorized" in caplog.text
    assert "gunicorn" not in caplog.text


def test_accepted_token_runs_the_pass(run_main, caplog):
    caplog.set_level("INFO")

    passes = run_main(FakeRoot())

    assert passes == ["queue", "sweep"]
    assert "Authenticated to Launchpad as ~" in caplog.text
