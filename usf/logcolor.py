"""Coloured console logging for the operator (#193).

Display only: warnings in yellow, errors in red, the `[ACTION]` write
prompts in bold cyan, item headers in bold magenta (#194), everything
else plain. Used only when the output is a terminal (or forced with
--color=always) and NO_COLOR isn't set, so cron, the charm and piped runs
stay plain.
"""

import logging
import os
import sys

RESET = "\033[0m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD_CYAN = "\033[1;36m"
BOLD_MAGENTA = "\033[1;35m"


def wanted(choice, stream=None, environ=None):
    """Whether to colour: `choice` is the --color value."""
    environ = os.environ if environ is None else environ
    if choice == "never":
        return False
    if choice == "always":
        return True
    if environ.get("NO_COLOR"):
        return False
    stream = sys.stderr if stream is None else stream
    return bool(getattr(stream, "isatty", lambda: False)())


class Formatter(logging.Formatter):
    """The console formatter, coloured or not: a record logged with
    extra={"bare": True} is printed as its message alone, without the
    `[verbose] name:` prefix (#194's separator)."""

    def format(self, record):
        if getattr(record, "bare", False):
            return record.getMessage()
        return super().format(record)


class ColorFormatter(Formatter):
    def format(self, record):
        text = super().format(record)
        if getattr(record, "bare", False):
            return text
        if record.levelno >= logging.ERROR:
            color = RED
        elif record.levelno >= logging.WARNING:
            color = YELLOW
        elif record.getMessage().lstrip("\n").startswith("[ACTION]"):
            color = BOLD_CYAN
        elif record.getMessage().startswith("=== Starting triage"):
            # #194: the item header.
            color = BOLD_MAGENTA
        else:
            return text
        # Colour each line, so a multi-line comment preview stays coloured
        # in pagers that reset at line ends.
        return "\n".join(f"{color}{line}{RESET}" if line else line for line in text.split("\n"))
