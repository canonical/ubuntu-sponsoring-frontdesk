"""Operator notifications via a Mattermost incoming webhook (design #48).

This is an *operator/admin* channel, not a sponsor-facing one: it carries
anomalies the bot detected but cannot act on and the contributor cannot fix
(stuck Launchpad diff generation, git-ubuntu importer failures) -- things
that previously only appeared in the verbose log. It must never mirror
ordinary queue states (READY_FOR_HUMAN etc.) into chat; sponsors already
have the queue page for that.

The webhook URL is a secret and lives outside the VCS, in
~/.config/ubuntu-sponsoring-frontdesk/config.ini::

    [notifications]
    webhook_url = https://chat.example.com/hooks/...

A missing file or key simply disables notifications -- fresh checkouts and
the test suite need zero setup. `SPONSORING_BOT_CONFIG` overrides the config
path (same style as the credentials-path override in launchpad_client.py).

Posting is best-effort: a failed POST logs a warning and the alert is lost.
Deliberately NOT wired into LPClient's write-effectiveness tracking (#36) --
blocking facts persistence to retry a ping would re-run the whole pipeline
(LLM included) over an anomaly that stays visible on the MP itself.
"""

import configparser
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds; a chat ping must never stall a triage run for long

# Posting is opt-in per process: main() enables it for non-dry-run modes.
# The import-time default (disabled) fails safe for tests and library use --
# anything that doesn't call setup() can at most log "would notify".
_enabled = False


def setup(enabled):
    """Called once by main(): enabled=(mode != "dry-run"). No [y/N] gate --
    this is the operator talking to themselves, not a contributor-visible
    write, so interactive mode posts without prompting."""
    global _enabled
    _enabled = bool(enabled)


def _config_path():
    return os.environ.get(
        "SPONSORING_BOT_CONFIG",
        os.path.join(
            os.path.expanduser("~"),
            ".config",
            "ubuntu-sponsoring-frontdesk",
            "config.ini",
        ),
    )


def webhook_url():
    """The configured webhook URL, or None (notifications disabled).

    Re-read on every call rather than memoized: the file is tiny, and a
    cache would need test-only reset plumbing for no measurable gain.
    """
    parser = configparser.ConfigParser()
    path = _config_path()
    try:
        found = parser.read(path)
    except configparser.Error as e:
        logger.warning("notify: could not parse %s (%s); notifications disabled.", path, e)
        return None
    if not found:
        return None
    return parser.get("notifications", "webhook_url", fallback=None)


def is_configured():
    """Whether a webhook URL is configured at all (regardless of mode).

    Callers that *replace* another signal with a notification (the #43
    switch: importer-failure MP comment -> operator ping) branch on this,
    not on notify()'s return value -- in dry-run the notification isn't
    posted either, but the replaced write would have been a dry-run no-op
    too, so the branch must be the same in both modes.
    """
    return webhook_url() is not None


def _post(url, text):
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        response.read()


def notify(text):
    """Best-effort operator ping. Returns True only if actually posted."""
    url = webhook_url()
    if url is None:
        logger.debug("notify: no webhook configured; dropping: %s", text)
        return False
    if not _enabled:
        logger.info("[dry-run] Would notify operators: %s", text)
        return False
    try:
        _post(url, text)
    except Exception as e:
        logger.warning("notify: webhook POST failed (%s); this alert is lost: %s", e, text)
        return False
    logger.info("Notified operators: %s", text)
    return True
