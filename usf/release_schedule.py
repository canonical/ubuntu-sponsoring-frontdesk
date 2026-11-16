"""
Cycle-specific release-schedule dates the bot needs for sync-request triage.

BACKLOG: source FEATURE_FREEZE from an online release schedule (e.g. the
Ubuntu release schedule page/ICS, or Launchpad milestone dates) instead of
hand-editing it. For now it's a manually maintained constant -- update it at
the start of every cycle. See https://ubuntu.com/project/docs/release-team/freezes/
for what Feature Freeze means; that page does not itself carry dates.
"""

import datetime

# Update every cycle: the development series and its Feature Freeze date.
# None until set, so callers fail safe (never claim to be past a freeze we
# don't actually know the date of).
#
# #178 (live, alsa-ucm-conf MP #514634): the date used to stand alone, so
# once a new series opened, the previous cycle's (already past) date kept
# claiming a freeze for the whole new cycle until someone edited it. Tied
# to its series, a stale entry now stops applying the day the next series
# becomes devel -- a forgotten update means no FF notes, not wrong ones.
FEATURE_FREEZE_SERIES = "stonking"
FEATURE_FREEZE = datetime.date(2026, 8, 20)  # confirmed by seb128, 2026-07-01


def is_after_feature_freeze(devel_series, today=None):
    """True if `devel_series` is the series FEATURE_FREEZE belongs to and
    `today` (default: real today) is past it.

    False -- not just "unknown" -- when either constant isn't set, when the
    devel series couldn't be looked up (None), or when it's another series
    (#178: the entry is stale), so callers that gate on this fail safe to
    "no freeze in effect" rather than silently misbehaving.
    """
    if FEATURE_FREEZE is None or not FEATURE_FREEZE_SERIES:
        return False
    if devel_series != FEATURE_FREEZE_SERIES:
        return False
    return (today or datetime.date.today()) > FEATURE_FREEZE
