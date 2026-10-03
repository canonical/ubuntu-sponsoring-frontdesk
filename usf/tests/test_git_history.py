"""git_history.commit_contains against real (local, throwaway) git repos --
the ancestry primitive behind design #49 case B. Local `file` transports
refuse bare-sha fetch targets by default, which conveniently also exercises
the sha -> ref fetch fallback."""

import subprocess

import pytest

import git_history

MISSING_SHA = "0" * 40


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo with two commits on refs/heads/main; returns (path, c1, c2)."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main", ".")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f").write_text("one\n")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "c1")
    c1 = _git(path, "rev-parse", "HEAD")
    (path / "f").write_text("two\n")
    _git(path, "commit", "-q", "-am", "c2")
    c2 = _git(path, "rev-parse", "HEAD")
    return str(path), c1, c2


def test_ancestor_is_true(repo):
    path, c1, c2 = repo
    assert git_history.commit_contains(path, c2, c1, ref="refs/heads/main") is True


def test_same_commit_is_true(repo):
    path, _, c2 = repo
    assert git_history.commit_contains(path, c2, c2, ref="refs/heads/main") is True


def test_descendant_is_false(repo):
    # The proposed commit is NEWER than what was uploaded: not contained.
    path, c1, c2 = repo
    assert git_history.commit_contains(path, c1, c2, ref="refs/heads/main") is False


def test_unknown_candidate_is_false(repo):
    path, _, c2 = repo
    assert git_history.commit_contains(path, c2, MISSING_SHA, ref="refs/heads/main") is False


def test_unfetchable_repo_is_none(tmp_path):
    assert (
        git_history.commit_contains(
            str(tmp_path / "nope"), "a" * 40, "b" * 40, ref="refs/heads/main"
        )
        is None
    )


def test_tip_not_in_repo_is_none(repo):
    # The .changes recorded a commit the ref no longer reaches (and the
    # server won't serve the bare sha): ancestry is undeterminable.
    path, _, _ = repo
    assert git_history.commit_contains(path, MISSING_SHA, "b" * 40, ref="refs/heads/main") is None


def test_missing_inputs_are_none(repo):
    path, c1, c2 = repo
    assert git_history.commit_contains(None, c2, c1) is None
    assert git_history.commit_contains(path, None, c1) is None
    assert git_history.commit_contains(path, c2, None) is None
