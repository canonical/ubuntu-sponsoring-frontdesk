"""Fix #5: admin check looks at the relevant Ubuntu series tasks only."""

import checks
from fakes import FakeBug, FakeTask, FakeMP


class _LP:
    def __init__(self):
        self.acted = False

    def comment(self, obj, message):
        self.acted = True

    def unsubscribe_sponsors(self, obj):
        self.acted = True


def _run(tasks, pkg):
    return checks.check_administrative_state(
        "url", FakeBug(tasks=tasks), _LP(), source_package=pkg
    )


def test_upstream_done_but_ubuntu_open_does_not_unsubscribe():
    # The original bug: an upstream 'Fix Released' must not drop the bug.
    tasks = [
        FakeTask("walinuxagent", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Jammy)", "In Progress"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_one_series_landed_another_open_is_not_done():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Noble)", "In Progress"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_targeted_landed_untargeted_wontfix_invalid_is_done():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu Noble)", "Won't Fix"),
        FakeTask("walinuxagent (Ubuntu Oracular)", "Invalid"),
    ]
    assert _run(tasks, "walinuxagent") is True


def test_all_closed_but_nothing_landed_is_not_claimed_complete():
    tasks = [
        FakeTask("walinuxagent (Ubuntu Jammy)", "Invalid"),
        FakeTask("walinuxagent (Ubuntu Noble)", "Won't Fix"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_other_package_done_is_ignored():
    tasks = [
        FakeTask("otherpkg (Ubuntu)", "Fix Released"),
        FakeTask("walinuxagent (Ubuntu)", "New"),
    ]
    assert _run(tasks, "walinuxagent") is False


def test_simple_devel_release_is_done():
    assert _run([FakeTask("mosquitto (Ubuntu)", "Fix Released")], "mosquitto") is True


def test_unknown_package_falls_back_to_any_ubuntu_task():
    assert _run([FakeTask("mosquitto (Ubuntu)", "Fix Released")], None) is True


def test_merged_mp_is_handled():
    mp = FakeMP(queue_status="Merged")
    assert checks.check_administrative_state("u", mp, _LP()) is True
