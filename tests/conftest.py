"""Shared test fixtures.

The per-item memo caches are keyed on Launchpad self_links, and the fakes
reuse a handful of shared links (FakeBug is always .../bug/1), so a cached
verdict from one test would leak into the next -- across modules, in
whatever order pytest runs them. Reset every per-item cache before each
test, so no test module has to remember to do it itself.
"""

import pytest

import attachments
import checks


@pytest.fixture(autouse=True)
def _reset_per_item_caches():
    checks.reset_diff_lines_cache()
    checks.reset_human_engaged_cache()
    attachments.reset_cache()
