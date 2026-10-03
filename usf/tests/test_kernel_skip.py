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


# --- #139: upstream-project merge proposals aren't ours -----------------------


def test_git_ubuntu_mp_is_a_packaging_mp():
    assert checks.is_packaging_mp(FakeMP(package="tmpreaper")) is True


def test_upstream_project_mp_is_skipped():
    # https://code.launchpad.net/~florian-rathgeber/ufc/python-setup/+merge/126259
    # targets ~fenics-core/ufc/main -- an upstream project branch with no
    # debian/ directory, which reached the queue via the report's uploader
    # loop (design_journal.md #139).
    mp = FakeMP(package="ufc")
    mp.self_link = (
        "https://api.launchpad.net/devel/~florian-rathgeber/ufc/python-setup/+merge/126259"
    )
    mp.web_link = "https://code.launchpad.net/~florian-rathgeber/ufc/python-setup/+merge/126259"
    mp.target_git_path = ""
    mp.target_branch_link = "https://api.launchpad.net/devel/~fenics-core/ufc/main"
    assert checks.is_packaging_mp(mp) is False


def test_bzr_packaging_branch_is_kept():
    mp = FakeMP(package="hello")
    mp.self_link = "https://api.launchpad.net/devel/~someone/hello/fix/+merge/42"
    mp.web_link = "https://code.launchpad.net/~someone/hello/fix/+merge/42"
    mp.target_git_path = ""
    mp.target_branch_link = "https://api.launchpad.net/devel/~someone/ubuntu/noble/hello/fix"
    assert checks.is_packaging_mp(mp) is True


def test_team_fork_targeting_a_git_ubuntu_branch_is_kept():
    mp = FakeMP(package="gnocchi", target="refs/heads/ubuntu/noble-devel")
    mp.self_link = (
        "https://api.launchpad.net/devel/~ubuntu-openstack-dev/gnocchi/+git/gnocchi/+merge/7"
    )
    mp.web_link = "https://code.launchpad.net/~ubuntu-openstack-dev/gnocchi/+git/gnocchi/+merge/7"
    assert checks.is_packaging_mp(mp) is True


def test_bugs_are_never_skipped_as_non_packaging():
    assert checks.is_packaging_mp(FakeBug()) is True
