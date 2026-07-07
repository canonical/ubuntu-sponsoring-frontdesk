"""Git ancestry checks against remote (Launchpad-hosted) repositories --
design #49 case B.

The rich-history question for an uploaded-but-not-autoclosed MP is not
"does the upload's Vcs-Git-Commit equal the MP's tip?" -- a sponsor may
legitimately stack a fixup commit on the contributor's branch and upload
from their own ~sponsor Launchpad repo, so the hashes differ while the
contributor's history is fully preserved. The real question is ancestry:
is the MP's tip commit contained in the history the upload points at?

This costs a git fetch of a stranger's repository, so everything here is
sandboxed: a throwaway scratch directory (removed in `finally`), a bounded
subprocess timeout per git call, and no configuration or credentials from
the operator's environment beyond plain anonymous https access. A partial
fetch (`--filter=tree:0`, commits only, no file content) is tried first --
ancestry needs only the commit graph -- with a plain full fetch as the
fallback for servers that don't honor filters.
"""

import logging
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 120  # seconds per git subprocess


def commit_contains(repo_url, tip, candidate, ref=None):
    """Whether `candidate` is an ancestor of (or equal to) commit `tip` in
    the repository at `repo_url`.

    `ref` (e.g. the upload's Vcs-Git-Ref) is used as a fallback fetch target
    when the server refuses to serve the bare `tip` sha directly.

    Returns True / False / None (couldn't determine: clone/fetch failure,
    timeout, or `tip` itself not found -- per the codebase-wide convention,
    None must make the caller fall back to undiagnosed behavior, not be
    read as False).
    """
    if not repo_url or not tip or not candidate:
        return None
    scratch = tempfile.mkdtemp(prefix="sponsoring-bot-git-")
    try:

        def git(*args):
            return subprocess.run(
                ["git", *args],
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )

        if git("init", "-q", ".").returncode != 0:
            logger.warning("git init failed in scratch dir; can't check ancestry.")
            return None

        # Fetch targets in preference order: the exact commit (works when
        # the server allows arbitrary-sha wants), then the named ref. Both
        # tried with a commits-only filter first, then unfiltered.
        fetched = False
        for target in [t for t in (tip, ref) if t]:
            for extra in (("--filter=tree:0",), ()):
                result = git("fetch", "-q", *extra, repo_url, target)
                if result.returncode == 0:
                    fetched = True
                    break
                logger.debug(
                    "git fetch %s %s (%s) failed: %s",
                    repo_url,
                    target,
                    extra or "no filter",
                    result.stderr.strip(),
                )
            if fetched:
                break
        if not fetched:
            logger.warning(
                "Could not fetch %s (tried %r and ref %r); can't check ancestry.",
                repo_url,
                tip,
                ref,
            )
            return None

        # The fetch may have landed on a ref whose tip moved past the
        # .changes' recorded commit; ancestry is only checkable if the
        # recorded tip itself is present.
        if git("cat-file", "-e", f"{tip}^{{commit}}").returncode != 0:
            logger.warning(
                "Commit %r not found in %s after fetch; can't check ancestry.",
                tip,
                repo_url,
            )
            return None

        # A full (non-shallow) fetch brings tip's whole commit chain, so a
        # missing candidate genuinely means "not in this history".
        if git("cat-file", "-e", f"{candidate}^{{commit}}").returncode != 0:
            return False

        result = git("merge-base", "--is-ancestor", candidate, tip)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        logger.warning(
            "git merge-base failed (%s); can't check ancestry.",
            result.stderr.strip(),
        )
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("git ancestry check against %s failed: %s", repo_url, e)
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
