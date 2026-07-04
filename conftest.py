# Root conftest: ensures the project modules (checks, main, ...) and the test
# helpers in tests/ are importable when running `pytest` from the project root.

import pytest

import checks


@pytest.fixture(autouse=True)
def _reset_diff_lines_cache():
    # Real MPs never share a preview-diff self_link, but the fakes reuse
    # short ones ('/d/1') across tests -- without this, one test's cached
    # diff content (or cached failure) would leak into the next.
    checks.reset_diff_lines_cache()
    yield
