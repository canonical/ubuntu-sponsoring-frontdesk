"""
Bug-attachment content foundation (design_journal.md #62).

Bug-side checks historically looked only at attachment *metadata* (the
Patch flag, the filename); this module is the shared primitive that
fetches and classifies attachment *content*, so the MP path's diff
helpers (llm_reviewer._split_debian_diff, _new_changelog_stanza, ...)
can run over patches and debdiffs attached to bugs.

Contracts mirror checks.diff_text's tri-state convention:
- str / (attachment, str): usable content.
- None: a fetch failed -- retriable, callers must treat it as "couldn't
  determine", never as a confirmed negative.
- False: no usable diff attachment -- a stable fact (nothing attached,
  nothing that parses as a diff, or over the size cap).

Deterministic code talks to Launchpad through launchpadlib, where
`attachment.data.open().read()` just works (the old "lpcli can't fetch
attachment bytes" note only ever mattered for the LLM's tooling, and
canonical/lpcli#23 was resolved 2026-07-10 anyway).
"""

import contextlib
import gzip
import io
import logging
import re

logger = logging.getLogger(__name__)

# Filenames that look like a contribution. Shared with
# checks.check_nothing_to_sponsor (this module is a leaf import for
# checks.py, so the regex lives here).
PATCH_FILENAME_RE = re.compile(r"\.(debdiff|diff|patch)(\.gz)?$", re.IGNORECASE)

# seb128 (2026-07-11): real debdiffs are small -- people use an MP or a
# PPA/tarball for new upstream versions -- so just skip anything bigger;
# "we will probably not miss much in practice". Applied to the
# *decompressed* size, so it doubles as the gzip-bomb guard.
MAX_ATTACHMENT_BYTES = 1024 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_DIFF_MARKER_RE = re.compile(r"^(diff --git |--- |Index: )", re.MULTILINE)

# Per-bug memo, mirroring checks' diff_text cache: several checks read
# the same attachment within one item's pass.
_review_target_cache = None

# #154: while a multi-series bug's per-series debdiffs are reviewed one at
# a time, review_target answers with the debdiff in focus instead of the
# newest one -- so every bug-side check reviews that series without each
# needing to know about series. (bug self_link, (attachment, text)).
_focus = None


def reset_cache():
    """Called by main at the start of each item and by tests."""
    global _review_target_cache, _focus
    _review_target_cache = None
    _focus = None


@contextlib.contextmanager
def focused(bug, attachment, text):
    """Make review_target(bug) return (attachment, text) inside the block."""
    global _focus
    previous = _focus
    _focus = (getattr(bug, "self_link", None), (attachment, text))
    try:
        yield
    finally:
        _focus = previous


def patch_attachments(bug):
    """The bug's attachments that look like contributions (Patch flag set,
    or a patch-shaped filename), in Launchpad's order (oldest first).
    Propagates exceptions -- callers decide their own None handling."""
    result = []
    for attachment in bug.attachments:
        if attachment.type == "Patch" or PATCH_FILENAME_RE.search(attachment.title or ""):
            result.append(attachment)
    return result


def attachment_text(attachment):
    """The attachment's content as text: str, None (fetch failed --
    retriable), or False (unusable: over MAX_ATTACHMENT_BYTES once
    decompressed, or not diff-shaped -- a stable fact)."""
    # #150: an attachment can be an external link rather than an uploaded
    # file (Launchpad sets `url` only then). There is nothing to read: its
    # `data` 404s, which looked like a failed fetch and kept the item
    # inconclusive on every pass (bug #2156932, a Gerrit review linked and
    # flagged as a patch). It isn't a diff we can review -- a stable fact.
    # Same for the UCT vulnerability-patches attachments Launchpad is adding
    # (a list of named patch URLs, not a file); the attribute doesn't exist
    # on production yet, hence getattr.
    link = getattr(attachment, "url", None)
    vulnerability_patches = getattr(attachment, "vulnerability_patches", None)
    if link or vulnerability_patches:
        logger.debug(
            "attachment_text: %r is a %s, not an uploaded file; skipping.",
            getattr(attachment, "title", "?"),
            f"link ({link})" if link else "vulnerability-patches list",
        )
        return False
    try:
        # cap + 1, not the whole file (#145): the size check below only needs
        # to know whether the cap was exceeded, and a 400MB upload with a
        # patch-shaped name would otherwise be fetched in full just to be
        # rejected -- slow enough to time out, which fails the item instead
        # of skipping the attachment. The gzip path below is already bounded
        # the same way, on its decompressed output (#62).
        raw = attachment.data.open().read(MAX_ATTACHMENT_BYTES + 1)
    except Exception as e:
        logger.debug(
            "attachment_text: could not fetch %r (%s).",
            getattr(attachment, "title", "?"),
            e,
        )
        return None
    if raw[:2] == _GZIP_MAGIC:
        try:
            # Decompress with an explicit ceiling on the *output* so a
            # decompression bomb can't eat the bot's memory; one extra
            # byte tells us "over the cap".
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                raw = f.read(MAX_ATTACHMENT_BYTES + 1)
        except Exception as e:
            logger.debug(
                "attachment_text: %r is gzip-shaped but wouldn't decompress (%s); unusable.",
                getattr(attachment, "title", "?"),
                e,
            )
            return False
    if len(raw) > MAX_ATTACHMENT_BYTES:
        logger.debug(
            "attachment_text: %r is %d bytes (cap %d); skipping.",
            getattr(attachment, "title", "?"),
            len(raw),
            MAX_ATTACHMENT_BYTES,
        )
        return False
    text = raw.decode(errors="replace")
    if not _DIFF_MARKER_RE.search(text):
        logger.debug(
            "attachment_text: %r doesn't look like a diff; skipping.",
            getattr(attachment, "title", "?"),
        )
        return False
    return text


def _norm_path(path):
    """A diff header path with its first component stripped: 'a/debian/
    changelog' (git) and 'pkg-1.2ubuntu1/debian/changelog' (debdiff) both
    -> 'debian/changelog'. None for /dev/null (added/deleted file side).
    The trailing debdiff timestamp is separated by a tab per POSIX, but
    spaces happen in the wild (seen live on bug #2158304), so cut at the
    first whitespace -- source-package paths don't contain spaces."""
    path = path.split()[0] if path.split() else ""
    if path == "/dev/null":
        return None
    parts = path.split("/", 1)
    return parts[1] if len(parts) == 2 else path


def classify_diff(text):
    """Split a unified diff -- git-style or debdiff-style -- into
    {'debian_paths': [...], 'other_paths': [...], 'changelog_lines':
    [...] or None}: the touched files under debian/, everything else,
    and the raw lines of the debian/changelog section when there is one.
    File boundaries are the '--- old' / '+++ new' header pairs, so this
    works whether or not the diff carries 'diff ...' command lines
    (checks._split-style git parsing doesn't, which is why the bug side
    can't reuse llm_reviewer._split_debian_diff directly)."""
    debian_paths = []
    other_paths = []
    changelog_lines = None
    lines = text.splitlines()
    section_start = None
    section_path = None

    def close_section(end):
        nonlocal changelog_lines
        if section_path == "debian/changelog":
            changelog_lines = lines[section_start:end]

    for i, line in enumerate(lines):
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            close_section(i)
            path = _norm_path(lines[i + 1][4:]) or _norm_path(line[4:])
            section_start = i
            section_path = path
            if path is None:
                continue
            if path == "debian/changelog" or path.startswith("debian/"):
                debian_paths.append(path)
            else:
                other_paths.append(path)
    close_section(len(lines))
    return {
        "debian_paths": debian_paths,
        "other_paths": other_paths,
        "changelog_lines": changelog_lines,
    }


def review_target(bug):
    """The attachment a sponsor would review: the NEWEST patch-shaped
    attachment whose content is a usable diff (older ones are superseded
    iterations). SRU bugs with one debdiff per series may eventually need
    an all-candidates variant (seb128, #62) -- newest-only for now.

    Returns (attachment, text), None (a fetch failed before a usable
    candidate was found -- retriable), or False (no usable candidate --
    stable). Memoized per bug per run. Inside `focused(...)`, the debdiff
    in focus instead (#154)."""
    global _review_target_cache
    key = getattr(bug, "self_link", None)
    if _focus is not None and _focus[0] == key:
        return _focus[1]
    if key is not None and _review_target_cache and _review_target_cache[0] == key:
        logger.debug("review_target: reusing already-fetched attachment")
        return _review_target_cache[1]
    result = _review_target_fetch(bug)
    if key is not None:
        _review_target_cache = (key, result)
    return result


def _review_target_fetch(bug):
    try:
        candidates = patch_attachments(bug)
    except Exception as e:
        logger.debug("review_target: couldn't list attachments (%s).", e)
        return None
    for attachment in reversed(candidates):
        text = attachment_text(attachment)
        if text is None:
            return None
        if text is not False:
            logger.debug("review_target: using %r.", getattr(attachment, "title", "?"))
            return attachment, text
    return False
