"""
Cycle-specific release-schedule dates the bot needs for sync-request triage.

BACKLOG: source FEATURE_FREEZE from an online release schedule (e.g. the
Ubuntu release schedule page/ICS, or Launchpad milestone dates) instead of
hand-editing it. For now it's a manually maintained constant -- update it at
the start of every cycle. See https://ubuntu.com/project/docs/release-team/freezes/
for what Feature Freeze means; that page does not itself carry dates.
"""

import datetime

# Update every cycle. None until set, so callers fail safe (never claim to be
# past a freeze we don't actually know the date of).
FEATURE_FREEZE = datetime.date(2026, 8, 20)  # confirmed by seb128, 2026-07-01


def is_after_feature_freeze(today=None):
    """True if `today` (default: real today) is past FEATURE_FREEZE.

    False -- not just "unknown" -- when FEATURE_FREEZE hasn't been set, so
    callers that gate on this fail safe to "no freeze in effect" rather than
    silently misbehaving.
    """
    if FEATURE_FREEZE is None:
        return False
    return (today or datetime.date.today()) > FEATURE_FREEZE
