"""tools/lp-credentials.py: the token must belong to the account asked for."""

import importlib.util
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "lp-credentials.py"


@pytest.fixture
def tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("lp_credentials", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logins = []

    def approve_as(name):
        def login_with(consumer, credentials_file, **kwargs):
            logins.append((consumer, credentials_file))
            Path(credentials_file).write_text("[1]\n")
            return SimpleNamespace(me=SimpleNamespace(name=name))

        monkeypatch.setattr(module.Launchpad, "login_with", staticmethod(login_with))

    module.approve_as = approve_as
    module.logins = logins
    return module


def test_token_for_the_named_account_is_kept(tool, tmp_path):
    tool.approve_as("ubuntu-sponsoring-bot")

    path = tool.create("ubuntu-sponsoring-bot", tmp_path)

    assert path == os.path.join(tmp_path, "ubuntu-sponsoring-bot.credentials")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert tool.logins == [("ubuntu-sponsoring-frontdesk", path)]


def test_token_approved_as_someone_else_is_discarded(tool, tmp_path):
    tool.approve_as("seb128")

    with pytest.raises(SystemExit, match="approved as ~seb128, not ~ubuntu-sponsoring-bot"):
        tool.create("ubuntu-sponsoring-bot", tmp_path)

    assert not list(tmp_path.iterdir())


def test_existing_file_is_never_reused(tool, tmp_path):
    (tmp_path / "ubuntu-sponsoring-bot.credentials").write_text("old token")
    tool.approve_as("ubuntu-sponsoring-bot")

    with pytest.raises(SystemExit, match="already exists"):
        tool.create("ubuntu-sponsoring-bot", tmp_path)

    assert tool.logins == []
    assert (tmp_path / "ubuntu-sponsoring-bot.credentials").read_text() == "old token"
