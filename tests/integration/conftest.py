# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Fixtures for the integration tests (jubilant, against a real controller).

Real credentials are deliberately not needed: the tests use dummy secrets
and check the wiring (status, files, timer, actions), never a pass against
Launchpad -- that is a live check for a human, in dry-run.
"""

import os
import subprocess
from pathlib import Path

import jubilant
import pytest

APP = "frontdesk"
REPO = Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption("--charm-path", help="Use this packed charm instead of packing one")
    parser.addoption("--model", help="Use this existing model instead of a temporary one")


@pytest.fixture(scope="module")
def juju(request):
    model = request.config.getoption("--model")
    if model:
        yield jubilant.Juju(model=model)
        return
    with jubilant.temp_model() as juju:
        yield juju


@pytest.fixture(scope="module")
def charm_path(request):
    path = request.config.getoption("--charm-path") or os.environ.get("CHARM_PATH")
    if path:
        return Path(path).resolve()
    # Captured (a pipe, not pytest's capture file -- charmcraft fails
    # silently writing to that), and shown if packing fails.
    proc = subprocess.run(["charmcraft", "pack"], cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"charmcraft pack failed:\n{proc.stdout}\n{proc.stderr}")
    return next(REPO.glob("*.charm")).resolve()
