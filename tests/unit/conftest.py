# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Shared fixtures for the charm's unit tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_subprocess():
    """Fail loudly if a test reaches a real subprocess call.

    The charm's workload installs packages and drives systemd; an unmocked
    call would do that to whoever runs the tests. Tests that exercise
    subprocess deliberately patch it themselves, on top of this.
    """
    with patch(
        "subprocess.run",
        side_effect=AssertionError("unit test attempted to run a real subprocess; mock it"),
    ):
        yield
