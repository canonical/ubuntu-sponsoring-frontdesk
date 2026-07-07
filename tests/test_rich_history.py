"""Design #49 round one: rich-history diagnosis for uploaded-but-not-
autoclosed MPs. When the uploaded .changes carries no git-ubuntu Vcs keys,
the MP's git history was dropped -- worth an MP comment (teach the tooling)
plus an operator ping; anything else falls through to the undiagnosed #48
behavior."""

import io

import pytest

import archive_lookup
import checks
import notify
import test_mp_checks


@pytest.fixture(autouse=True)
def _notify_reset():
    yield
    notify.setup(False)


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda url, text: sent.append(text))
    return sent


def _configure_webhook(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text("[notifications]\nwebhook_url = https://chat.example.com/h\n")
    monkeypatch.setenv("SPONSORING_BOT_CONFIG", str(path))


# --- changes_file_vcs_keys parsing -------------------------------------------


class _ChangesPub:
    def __init__(self, url="https://launchpad.net/.../x.changes"):
        self._url = url

    def changesFileUrl(self):
        return self._url


def _serve(monkeypatch, body):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        archive_lookup.urllib.request,
        "urlopen",
        lambda url, timeout=None: _Resp(body.encode()),
    )


def test_vcs_keys_parsed_from_changes(monkeypatch):
    _serve(
        monkeypatch,
        "Format: 1.8\n"
        "Source: testpkg\n"
        "Vcs-Git: https://git.launchpad.net/ubuntu/+source/testpkg\n"
        "Vcs-Git-Commit: 0123abcd\n"
        "Vcs-Git-Ref: refs/heads/upload\n",
    )
    keys = archive_lookup.changes_file_vcs_keys(_ChangesPub())
    assert keys == {
        "Vcs-Git": "https://git.launchpad.net/ubuntu/+source/testpkg",
        "Vcs-Git-Commit": "0123abcd",
        "Vcs-Git-Ref": "refs/heads/upload",
    }


def test_vcs_keys_absent_returns_empty_dict(monkeypatch):
    _serve(monkeypatch, "Format: 1.8\nSource: testpkg\n")
    assert archive_lookup.changes_file_vcs_keys(_ChangesPub()) == {}


def test_vcs_keys_fetch_failure_returns_none(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("librarian down")

    monkeypatch.setattr(archive_lookup.urllib.request, "urlopen", boom)
    assert archive_lookup.changes_file_vcs_keys(_ChangesPub()) is None


def test_vcs_keys_no_changes_url_returns_none():
    class NoUrl:
        def changesFileUrl(self):
            return None

    assert archive_lookup.changes_file_vcs_keys(NoUrl()) is None
    # A publication object without the method at all (older fakes, odd
    # records) must also degrade to "can't diagnose".
    assert archive_lookup.changes_file_vcs_keys(object()) is None


# --- the done-branch diagnosis (#49 case A) ----------------------------------


def _already_uploaded_mp(monkeypatch, vcs_keys):
    test_mp_checks._patch_archive(
        monkeypatch,
        versions={"noble": "1.2-4"},
        changelog=test_mp_checks._ARCHIVE_CHANGELOG_MATCHING,
    )
    monkeypatch.setattr(archive_lookup, "changes_file_vcs_keys", lambda pub: vcs_keys)
    return test_mp_checks._merge_mp_with_diff(test_mp_checks._CHANGELOG_DIFF_V124)


def test_missing_vcs_keys_comments_and_notifies(tmp_path, monkeypatch, posts):
    _configure_webhook(tmp_path, monkeypatch)
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch, vcs_keys={})
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    # MP comment: closure info + the sponsor-facing Vcs-headers note.
    assert len(lp.comments) == 1
    assert "can be closed" in lp.comments[0]
    assert "Vcs headers" in lp.comments[0]
    assert "handle-git-ubuntu-uploads" in lp.comments[0]
    # Channel: the terse central-tally ping, not the generic #48 one.
    assert len(posts) == 1
    assert "rich history dropped" in posts[0]
    assert "testpkg 1.2-4" in posts[0]


def test_missing_vcs_keys_comments_even_without_webhook(monkeypatch, posts):
    # The MP comment carries the teaching value on its own; no webhook
    # means the ping is just dropped.
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch, vcs_keys={})
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert "Vcs headers" in lp.comments[0]
    assert posts == []


def test_present_vcs_keys_fall_through_to_undiagnosed_ping(
    tmp_path, monkeypatch, posts
):
    # Keys present (case B -- commit match/mismatch -- is design-pending):
    # behave exactly like #48, operator ping instead of an MP comment.
    _configure_webhook(tmp_path, monkeypatch)
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch, vcs_keys={"Vcs-Git-Commit": "0123abcd"})
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.comments == []
    assert len(posts) == 1
    assert "did not auto-close" in posts[0]


def test_unfetchable_changes_falls_through_to_undiagnosed_ping(
    tmp_path, monkeypatch, posts
):
    _configure_webhook(tmp_path, monkeypatch)
    notify.setup(True)
    mp = _already_uploaded_mp(monkeypatch, vcs_keys=None)
    lp = test_mp_checks._LP()
    assert checks.check_stale_version("url", mp, lp) == "done"
    assert lp.comments == []
    assert len(posts) == 1
    assert "did not auto-close" in posts[0]
