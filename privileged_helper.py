"""Privileged helper: team actions delegated to a separate Launchpad account
(design_journal.md #78).

The main bot account must NOT be a member of ~ubuntu-sponsors: a member's
vote on a merge proposal claims the team's review slot, which permanently
drops the MP from the sponsoring report -- the exact opposite of the bot's
"early feedback, then a human sponsors" purpose. But unsubscribing the team
from a bug REQUIRES membership. So that action runs here, in a separate
process authenticated with its own token from an account that is a member
(interim: a personal token; eventually a dedicated helper account -- the
same account is the natural future member of ~ubuntu-security-sponsors for
backlog 5b).

Deliberately a subprocess rather than a second in-process launchpadlib
session: seb128 has been bitten by launchpadlib state confusion between
sessions before, and a process boundary gives the credentials/cache
isolation by construction. Objects don't cross the boundary either way (a
bug loaded in one session can't act through another), so the helper
re-loads the bug itself; the caller just passes the bug id.

Contract: exit 0 = action performed, anything else = failed (stderr says
why); the caller records the write outcome accordingly. All mode gating
(dry-run / interactive / --yes) happens in the CALLER -- when this script
runs, the decision to write has already been made.

Note: the action is not anonymous on Launchpad -- the bug's activity log
records "removed subscriber ..." attributed to the helper account.
"""

import argparse
import logging
import os
import sys

from launchpadlib.launchpad import Launchpad

logger = logging.getLogger(__name__)

_LP_TIMEOUT_SECONDS = 30
_APP_NAME = "ubuntu-sponsoring-bot-helper"


def _login():
    creds_file = os.environ.get(
        "SPONSORING_BOT_HELPER_LP_CREDENTIALS",
        os.path.join(os.path.expanduser("~"), ".cache", _APP_NAME, "credentials"),
    )
    cache_dir = os.environ.get(
        "SPONSORING_BOT_HELPER_LP_CACHE",
        os.path.join(os.path.expanduser("~"), ".cache", _APP_NAME, "launchpadlib"),
    )
    os.makedirs(os.path.dirname(creds_file), exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    return Launchpad.login_with(
        application_name=_APP_NAME,
        service_root="production",
        credentials_file=creds_file,
        launchpadlib_dir=cache_dir,
        timeout=_LP_TIMEOUT_SECONDS,
        version="devel",
    )


def unsubscribe_team(bug_id, team):
    lp = _login()
    bug = lp.bugs[int(bug_id)]
    bug.unsubscribe(person=lp.people[team])
    print(f"unsubscribed ~{team} from bug #{bug_id} as {lp.me.name}")


def login():
    """One-time setup: run the OAuth flow and store the token. Needed
    because the caller only delegates to this helper once the credentials
    file EXISTS -- the first-use OAuth can never be triggered by the bot
    itself (found live: the unsubscribe fell back to the bot's own token
    and got a 401)."""
    lp = _login()
    print(f"logged in as {lp.me.name}; token stored, delegation is now active")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "login", help="Authorize the helper token (one-time setup, no writes)"
    )
    unsub = sub.add_parser(
        "unsubscribe-sponsors", help="Unsubscribe a sponsoring team from a bug"
    )
    unsub.add_argument("bug_id", help="Launchpad bug number")
    unsub.add_argument(
        "--team",
        default="ubuntu-sponsors",
        help="Team to unsubscribe (default: ubuntu-sponsors)",
    )
    args = parser.parse_args(argv)

    try:
        if args.action == "login":
            login()
        else:
            unsubscribe_team(args.bug_id, args.team)
    except Exception as e:
        print(f"helper failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
