"""#136: kernel-team packages are out of scope entirely.

The kernel team runs its own SRU/upload workflow, so the bot never triages
`linux` or anything derived from it -- no checks, no LLM, no writes.
"""

from fakes import FakeBug, FakeDiff, FakeLLM, FakeMP, FakeTask, FakeTriageClient

import checks
import main
from state import StateManager

URL = "https://code.launchpad.net/~x/ubuntu/+source/linux/+git/linux/+merge/1"


def _mp(package):
    return FakeMP(
        target="refs/heads/ubuntu/noble-devel",
        diff=FakeDiff("/d/1", 50),
        package=package,
    )


def _bug(task_name):
    return FakeBug(tasks=[FakeTask(task_name, "New")])


def test_linux_mp_is_kernel():
    assert checks.is_kernel_item(_mp("linux")) is True


def test_derived_kernel_packages_are_kernel():
    for package in (
        "linux-signed",
        "linux-meta-hwe-6.14",
        "linux-hwe-6.14",
        "linux-oem-6.17",
        "linux-aws",
        "linux-riscv",
        "linux-firmware",
    ):
        assert checks.is_kernel_item(_mp(package)) is True, package


def test_unrelated_packages_are_not_kernel():
    for package in ("linuxlogo", "util-linux", "backport-iwlwifi-dkms", "wireplumber"):
        assert checks.is_kernel_item(_mp(package)) is False, package


def test_bug_task_names_are_checked():
    assert checks.is_kernel_item(_bug("linux (Ubuntu Noble)")) is True
    assert checks.is_kernel_item(_bug("wireplumber (Ubuntu)")) is False


def test_queue_source_package_is_checked():
    # The queue JSON's own name counts even when the MP URL says otherwise
    # (a kernel MP proposed from an oddly-named repo).
    assert checks.is_kernel_item(_mp("somethingelse"), source_package="linux-aws") is True


def test_unreadable_bug_tasks_do_not_claim_kernel():
    class _Broken(FakeBug):
        @property
        def bug_tasks(self):
            raise TimeoutError("simulated Launchpad timeout")

        @bug_tasks.setter
        def bug_tasks(self, value):
            pass

    assert checks.is_kernel_item(_Broken()) is False


def test_triage_skips_a_kernel_item_without_writing(tmp_path):
    mp = _mp("linux")
    lp = FakeTriageClient(objects={URL: mp})
    sm = StateManager(db_path=str(tmp_path / "s.db"))

    main.triage_url(URL, sm, lp, FakeLLM())

    assert lp.comments == [] and lp.votes == []
    assert sm.get_status(URL) is None  # nothing persisted, no facts stored
    row = [r for r in lp.audit.records if r["action"] == "triage"][0]
    assert row["outcome"] == "skipped-kernel"
    assert row["extra"]["findings"] == []
