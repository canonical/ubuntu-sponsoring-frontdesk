"""#193: coloured console output, and the item URL with every write prompt."""

import io
import logging

import pytest
from fakes import FakeAudit, FakeRoot

import logcolor
from launchpad_client import LPClient


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "choice, stream, environ, expected",
    [
        ("auto", _Tty(), {}, True),
        ("auto", io.StringIO(), {}, False),  # piped, e.g. through tee
        ("auto", _Tty(), {"NO_COLOR": "1"}, False),
        ("always", io.StringIO(), {"NO_COLOR": "1"}, True),
        ("never", _Tty(), {}, False),
    ],
)
def test_when_colour_is_used(choice, stream, environ, expected):
    assert logcolor.wanted(choice, stream=stream, environ=environ) is expected


def _fmt(level, msg):
    record = logging.LogRecord("x", level, __file__, 1, msg, None, None)
    return logcolor.ColorFormatter("%(message)s").format(record)


def test_levels_and_actions():
    assert _fmt(logging.WARNING, "w") == f"{logcolor.YELLOW}w{logcolor.RESET}"
    assert _fmt(logging.ERROR, "e") == f"{logcolor.RED}e{logcolor.RESET}"
    assert _fmt(logging.INFO, "plain") == "plain"
    action = _fmt(logging.INFO, "\n[ACTION] Post this comment:\n---")
    assert action == (
        f"\n{logcolor.BOLD_CYAN}[ACTION] Post this comment:{logcolor.RESET}"
        f"\n{logcolor.BOLD_CYAN}---{logcolor.RESET}"
    )


def test_every_write_prompt_names_the_item(caplog):
    client = LPClient(mode="dry-run", lp=FakeRoot(), audit=FakeAudit())
    client.start_item("https://launchpad.net/bugs/1")
    with caplog.at_level(logging.INFO):
        client._decide("Post this comment")
    assert "  Item: https://launchpad.net/bugs/1" in caplog.text


def _record(msg, **extra):
    record = logging.LogRecord("__main__", logging.INFO, __file__, 1, msg, None, None)
    record.__dict__.update(extra)
    return record


@pytest.mark.parametrize("formatter", [logcolor.Formatter, logcolor.ColorFormatter])
def test_the_separator_has_no_prefix_and_no_colour(formatter):
    # #194: a blank line and the rule, without "[verbose] __main__:".
    import main

    fmt = formatter("[verbose] %(name)s: %(message)s")
    assert fmt.format(_record(f"\n{main.ITEM_RULE}", bare=True)) == f"\n{main.ITEM_RULE}"


def test_only_the_starting_line_is_coloured():
    fmt = logcolor.ColorFormatter("[verbose] %(name)s: %(message)s")
    start = fmt.format(_record("=== Starting triage for: u ==="))
    assert start == (
        f"{logcolor.BOLD_MAGENTA}[verbose] __main__: === Starting triage for: u ==="
        f"{logcolor.RESET}"
    )
    assert fmt.format(_record("=== Finished triage for: u ===")) == (
        "[verbose] __main__: === Finished triage for: u ==="
    )
