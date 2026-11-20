"""#191: a sponsor's small changelog edit before upload is still the same
upload (live: libvirt MP #513129, `d/p/u/` fixed to `d/p/u-aa/`)."""

import checks

MP = """\
libvirt (10.0.0-2ubuntu8.20) noble; urgency=medium

  * apparmor: allow QEMU reads to /proc/sys/vm/max_map_count (LP: #2170732)
    - d/p/u/lp2170732-apparmor-qemu-read-max-map-count.patch

 -- Alex Ramírez <alex.ramirez@canonical.com>  Wed, 14 Oct 2026 14:52:49 +0000"""
ARCHIVE = MP.replace("d/p/u/", "d/p/u-aa/")
RACED = """\
libvirt (10.0.0-2ubuntu8.20) noble; urgency=medium

  * Fix a crash in the domain event loop (LP: #2170999)
    - d/p/u/lp2170999-fix-event-loop.patch

 -- Someone Else <x@example.com>  Thu, 15 Oct 2026 10:00:00 +0000"""


def test_the_live_sponsor_edit_is_the_same_upload():
    assert checks._same_upload(MP, ARCHIVE)


def test_the_authors_own_upload_is_the_same_whatever_the_edit():
    assert checks._same_upload(MP, RACED, creator="~a", author="~a")


def test_a_different_change_is_not():
    assert not checks._same_upload(MP, RACED)
    assert not checks._same_upload(MP, RACED, creator="~b", author="~a")


def test_a_different_bug_reference_is_not():
    assert not checks._same_upload(MP, ARCHIVE.replace("2170732)", "2170733)"))


def test_an_added_bullet_is_not():
    extra = ARCHIVE.replace("patch\n", "patch\n  * Also something else.\n", 1)
    assert not checks._same_upload(MP, extra)


def test_a_different_header_is_not():
    assert not checks._same_upload(MP, ARCHIVE.replace("noble;", "noble-security;"))
