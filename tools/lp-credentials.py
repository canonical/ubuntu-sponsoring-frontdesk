#!/usr/bin/python3
# Copyright 2026 Canonical Ltd.
# See COPYING for licensing details.

"""Create a Launchpad credentials file for one of the bot's accounts.

    tools/lp-credentials.py <launchpad-account>

writes <launchpad-account>.credentials in the current directory, ready for
`juju add-secret ... credentials#file=<launchpad-account>.credentials`
(doc/CHARM.md). The two accounts the charm takes:

    ubuntu-sponsoring-bot     -> lp-triager-credentials
    ubuntu-sponsoring-helper  -> lp-sponsor-credentials (the #78 helper,
                                 a ~ubuntu-sponsors member)

It prints a URL to approve. The token belongs to whoever is logged in to
Launchpad in the browser that approves it -- so open the URL in a session
logged in as THAT account (a private window), not as yourself. The result is
checked: a token for any other account is deleted and the script fails,
since the bot would read its own comments as a human reviewer's (#146).

Needs python3-launchpadlib. Runs headless: nothing opens a browser here.
"""

import argparse
import os
import sys

from launchpadlib.credentials import AuthorizeRequestTokenWithURL
from launchpadlib.launchpad import Launchpad

# What Launchpad's "Authorized applications" page lists the token as: a name
# of its own, so it can be revoked without touching another service's token.
CONSUMER = "ubuntu-sponsoring-frontdesk"


def create(account, directory="."):
    """Authorise a token for ``account``; return the file's path.

    Raises SystemExit with a message on any refusal, leaving no file behind.
    """
    path = os.path.join(directory, f"{account}.credentials")
    # login_with() only asks for authorisation when the file is missing:
    # with one already there it would silently reuse that token instead.
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists; move it away to create a new token.")

    lp = Launchpad.login_with(
        CONSUMER,
        service_root="production",
        version="devel",
        credentials_file=path,
        authorization_engine=AuthorizeRequestTokenWithURL("production", consumer_name=CONSUMER),
    )
    # login_with() never validates the token (#147): this is the first call
    # that needs it, and it says whose it is.
    try:
        name = lp.me.name
    except Exception as e:
        _discard(path)
        raise SystemExit(f"Launchpad refused the new token: {e}") from e
    if name != account:
        _discard(path)
        raise SystemExit(
            f"The token was approved as ~{name}, not ~{account}: discarded. "
            f"Approve the URL in a browser logged in as ~{account}."
        )
    os.chmod(path, 0o600)
    return path


def _discard(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("account", help="Launchpad account name, without the ~")
    args = parser.parse_args(argv)
    path = create(args.account.lstrip("~"))
    print(f"Credentials for ~{args.account.lstrip('~')} saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
