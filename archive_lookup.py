"""
Archive-lookup helpers used by the sync-request triage in llm_reviewer.py to
answer "what's actually in the archive" without shelling out to `rmadison`
(which needs devscripts installed locally) or `distro-info`.

Ubuntu state comes straight from the Launchpad API -- the canonical source,
and one we're already authenticated against for everything else the bot
does. Debian state comes from the FTP-master team's live madison endpoint
(https://api.ftp-master.debian.org/madison), which reads straight from dak's
own database (projectb) -- updated the moment an upload is accepted, not
after the next dinstall/publisher run. That matters here: qa.debian.org's
madison.php (and the default `rmadison` backend) only reflect the archive
*after* the publisher has run, which can lag a fresh upload by hours; a sync
request filed right after uploading to Debian is normal, not suspicious, and
using the stale mirror-index view would make it look like the version
doesn't exist yet.

Every public lookup function returns ``None`` (not ``{}``) when the lookup
could not be completed at all (network error, Launchpad error) -- callers
use that to distinguish "genuinely no results" from "we couldn't check" and
fail safe rather than act on a guess.
"""

import logging
import re
import urllib.error
import urllib.parse
import urllib.request

import apt_pkg

logger = logging.getLogger(__name__)

# apt_pkg.version_compare() raises until the system is initialized; do it
# once, lazily, rather than paying the config-file read on import.
_apt_pkg_initialized = False


def _ensure_apt_pkg():
    global _apt_pkg_initialized
    if not _apt_pkg_initialized:
        apt_pkg.init_system()
        _apt_pkg_initialized = True


# --- Debian: api.ftp-master.debian.org/madison (live dak database) -----------

_MADISON_URL = "https://api.ftp-master.debian.org/madison"

# The plain-text output is the same format rmadison/madison.php print:
#   pkgname | 1.2-3 | unstable | source
#   pkgname | 1.2-3 | noble-proposed | source, amd64, arm64
_LINE_RE = re.compile(r"^\s*\S+\s*\|\s*(?P<version>\S+)\s*\|\s*(?P<suites>[^|]+?)\s*\|")


def _parse_madison(output):
    """Parse madison-format text into {suite: version}. A suite field can
    list several pockets for the same version ('noble-updates,
    noble-security'); each gets its own entry. A suite can also appear on
    more than one line during a partial rollout (e.g. one arch still
    building the previous version) -- the last line wins, which is a
    reasonable approximation and matches the pre-existing behaviour."""
    versions = {}
    for line in output.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        version = match.group("version").strip()
        for suite in match.group("suites").split(","):
            versions[suite.strip()] = version
    return versions


def debian_versions(package, suites=None):
    """{suite: version} for `package` in Debian, optionally restricted to
    `suites`. None if the query could not be completed at all."""
    params = {"text": "on", "package": package}
    if suites:
        params["s"] = ",".join(suites)
    url = f"{_MADISON_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            output = resp.read().decode()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.warning("could not query %s: %s", url, e)
        return None
    return _parse_madison(output)


# --- Ubuntu: Launchpad API ----------------------------------------------------

# Publication pockets other than Release get a suite-style suffix, mirroring
# how they'd read in rmadison/madison output ('noble', 'noble-proposed', ...).
_POCKET_SUFFIX = {"Release": ""}


def devel_codename(lp):
    """The current Ubuntu development series codename (e.g. 'plucky'), or
    None if it couldn't be determined."""
    try:
        return lp.distributions["ubuntu"].current_series.name
    except Exception as e:
        logger.warning("could not determine the Ubuntu devel series: %s", e)
        return None


def supported_series_ordered(lp):
    """The Ubuntu series a fix can still be expected to land in -- status
    Supported, Current Stable Release, or Active Development -- as a list
    of (codename, version) pairs ordered oldest release first (so the
    current devel series is last). Versions ride along because an LLM
    can't be assumed to know recent codenames' ordering (design #58's
    live probe: the model had no idea 'resolute' is 26.04, so 'fixed in
    plucky 25.04+' didn't read as covering it). Includes ESM-only series
    (they report 'Supported' too), which is harmless: callers only look
    at series NEWER than an SRU's target. None on lookup failure
    (tri-state; a failure must not read as 'no newer series exist')."""
    try:
        entries = []
        for series in lp.distributions["ubuntu"].series:
            if series.status in (
                "Supported",
                "Current Stable Release",
                "Active Development",
            ):
                # '24.04' -> (24, 4): the release version orders series
                # chronologically; codenames don't.
                key = tuple(int(part) for part in series.version.split("."))
                entries.append((key, series.name, series.version))
        return [(name, version) for _key, name, version in sorted(entries)]
    except Exception as e:
        logger.warning("Launchpad lookup failed (supported Ubuntu series): %s", e)
        return None


def ubuntu_versions(lp, package, series_names=None):
    """{suite: version} for `package` currently published in Ubuntu's
    primary archive, restricted to `series_names` (e.g. ['noble']; defaults
    to the current devel series). Suite names in the result combine the
    series with its pocket ('noble', 'noble-proposed'). None if the lookup
    could not be completed at all."""
    try:
        ubuntu = lp.distributions["ubuntu"]
        archive = ubuntu.main_archive
        names = series_names or [ubuntu.current_series.name]
        versions = {}
        for name in names:
            series = ubuntu.getSeries(name_or_version=name)
            pubs = archive.getPublishedSources(
                source_name=package,
                exact_match=True,
                distro_series=series,
                status="Published",
            )
            for pub in pubs:
                suffix = _POCKET_SUFFIX.get(pub.pocket, f"-{pub.pocket.lower()}")
                versions[f"{name}{suffix}"] = pub.source_package_version
        return versions
    except Exception as e:
        logger.warning(
            "Launchpad lookup failed (ubuntu versions for %s): %s", package, e
        )
        return None


def published_source_url(package, version):
    """The human-facing Launchpad page for a specific published source
    version (e.g. 'https://launchpad.net/ubuntu/+source/foo/1.2-1'). Built
    directly rather than read off the publication object: a
    SourcePackagePublishingHistory has no `web_link` -- confirmed live, the
    real launchpadlib object simply doesn't expose one, since this kind of
    record has no canonical page of its own in Launchpad's object model
    (the page instead belongs to the package+version combination). `quote`
    keeps Debian-version characters valid in a URL path without mangling
    version comparisons in the comment text elsewhere ('~', '+', ':' for an
    epoch, all common and all safe to leave unescaped in a URL path)."""
    return (
        f"https://launchpad.net/ubuntu/+source/{package}/"
        f"{urllib.parse.quote(version, safe='~+:')}"
    )


def published_source(lp, package, series_name, version, status="Published"):
    """The SourcePackagePublishingHistory for `package` == `version` in
    `series_name` (any pocket; first match), or None if there's no such
    publication or the lookup fails.

    ``status`` defaults to 'Published' (today's active archive state).
    Pass ``status=None`` to search every publication status at once
    (Published, Superseded, Deleted, ...) -- used by check_stale_version
    (design_journal.md #43) to find a publication of an exact version that
    has since been superseded by something newer, rather than assuming a
    version older than the current archive max was simply never uploaded.
    When several statuses match, a still-registered publication (not
    'Deleted') is preferred -- a deleted record's changelog may not be
    readable."""
    try:
        ubuntu = lp.distributions["ubuntu"]
        archive = ubuntu.main_archive
        series = ubuntu.getSeries(name_or_version=series_name)
        kwargs = dict(
            source_name=package,
            exact_match=True,
            distro_series=series,
            version=version,
        )
        if status is not None:
            kwargs["status"] = status
        matches = list(archive.getPublishedSources(**kwargs))
        if not matches:
            return None
        matches.sort(key=lambda p: getattr(p, "status", "") == "Deleted")
        return matches[0]
    except Exception as e:
        logger.warning(
            "Launchpad lookup failed (published source for %s %s in %s): %s",
            package,
            version,
            series_name,
            e,
        )
        return None


def upload_in_queue(lp, package, series_name, version=None):
    """Whether `package` == `version` is sitting in `series_name`'s upload
    queue awaiting archive review -- uploaded, but not yet published, so
    invisible to getPublishedSources/madison. Typical for an SRU waiting on
    the SRU team in the Unapproved queue (design_journal.md #55).
    ``version=None`` matches ANY upload of the source (#79: a brand-new
    package waiting in the NEW queue, where the version isn't known from
    the bug).

    Tri-state: the PackageUpload entry (truthy -- in queue; pass it to
    queue_changes_text() to see WHOSE upload it is, a same-version race is
    possible since SRU version increments are convention-fixed), False (not
    in queue), None (lookup failed -- the caller must not treat a failed
    lookup as "not queued")."""
    try:
        series = lp.distributions["ubuntu"].getSeries(name_or_version=series_name)
        kwargs = dict(name=package, exact_match=True)
        if version is not None:
            kwargs["version"] = version
        # Unapproved first: it's where SRUs (and freeze-time devel uploads)
        # wait, so the common hit short-circuits the other two lookups.
        # Accepted is included to cover the window between queue acceptance
        # and actual publication.
        for status in ("Unapproved", "New", "Accepted"):
            uploads = series.getPackageUploads(status=status, **kwargs)
            for upload in uploads:
                logger.debug(
                    "upload_in_queue: %s %s found in %s queue %r (pocket=%s)",
                    package,
                    version,
                    series_name,
                    status,
                    getattr(upload, "pocket", "?"),
                )
                return upload
        return False
    except Exception as e:
        logger.warning(
            "Launchpad lookup failed (upload queue for %s %s in %s): %s",
            package,
            version,
            series_name,
            e,
        )
        return None


# The `Format:` declaration in a .dsc (dpkg-source(1)). Signed .dsc files
# wrap the fields in a PGP clearsign envelope; a plain line scan still finds
# the field, no signature handling needed.
_DSC_FORMAT_RE = re.compile(r"^Format:\s*(?P<format>.+?)\s*$", re.MULTILINE)

# Source files that only a non-native package ships: the Debian packaging
# delta relative to a separate orig tarball.
_NON_NATIVE_FILE_MARKERS = (".diff.gz", ".debian.tar.")


def is_native_source(lp, package, series_name):
    """
    Whether the currently published `package` in `series_name` is a native
    source package (design_journal.md #77): native packages carry their
    packaging in the upstream tree, so direct source edits are legitimate
    (dpkg-source(1)).

    The authoritative signal is the published .dsc's `Format:` field
    (seb128's suggestion -- it's what dpkg-source itself obeys):
    `3.0 (native)` -> native, `3.0 (quilt)` -> non-native. `Format: 1.0`
    is ambiguous by design (1.0 is native iff no .diff.gz accompanies the
    tarball), so 1.0 -- or a missing/unrecognized Format -- falls back to
    the source file names from the same sourceFileUrls() listing.

    Tri-state: True (native), False (non-native), None (no publication
    found or a lookup/fetch failed -- the caller must not guess).
    """
    try:
        ubuntu = lp.distributions["ubuntu"]
        archive = ubuntu.main_archive
        series = ubuntu.getSeries(name_or_version=series_name)
        pubs = list(
            archive.getPublishedSources(
                source_name=package,
                exact_match=True,
                distro_series=series,
                status="Published",
            )
        )
        if not pubs:
            logger.debug(
                "is_native_source: no current publication of %s in %s.",
                package,
                series_name,
            )
            return None
        file_urls = list(pubs[0].sourceFileUrls())
    except Exception as e:
        logger.warning(
            "Launchpad lookup failed (source files for %s in %s): %s",
            package,
            series_name,
            e,
        )
        return None

    dsc_url = next((u for u in file_urls if u.endswith(".dsc")), None)
    fmt = None
    if dsc_url:
        try:
            with urllib.request.urlopen(dsc_url, timeout=15) as resp:
                match = _DSC_FORMAT_RE.search(resp.read().decode(errors="replace"))
                fmt = match.group("format").strip() if match else None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            logger.warning("could not fetch .dsc at %s: %s", dsc_url, e)
            return None
    logger.debug(
        "is_native_source: %s in %s: Format=%r files=%s",
        package,
        series_name,
        fmt,
        [u.rsplit("/", 1)[-1] for u in file_urls],
    )
    if fmt == "3.0 (native)":
        return True
    if fmt == "3.0 (quilt)":
        return False
    # Format 1.0 (or no readable Format): the file listing decides.
    return not any(
        marker in url.rsplit("/", 1)[-1]
        for url in file_urls
        for marker in _NON_NATIVE_FILE_MARKERS
    )


def changelog_text(pub):
    """Plain-text contents of a SourcePackagePublishingHistory's changelog
    file (`pub.changelogUrl()`), or None if it can't be resolved or fetched.

    Deliberately a plain, unauthenticated fetch (not routed through the
    bot's launchpadlib session): changelogUrl() points at a publicly
    readable librarian file (the standard way other tooling reads a
    published changelog, confirmed by seb128 -- not something specific to
    this bot's credentials). If that assumption turns out to be wrong for
    some publication, urlopen fails and this returns None, which makes the
    caller skip the content comparison rather than misfire -- but this
    specific path hasn't been exercised against a real MP yet, so treat
    the "already uploaded with matching content" case in
    checks.check_stale_version as unverified until a live smoke test.
    """
    try:
        url = pub.changelogUrl()
    except Exception as e:
        logger.warning("could not resolve changelogUrl(): %s", e)
        return None
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.warning("could not fetch changelog at %s: %s", url, e)
        return None


def changes_file_vcs_keys(pub):
    """The git-ubuntu rich-history keys (`Vcs-Git`, `Vcs-Git-Commit`,
    `Vcs-Git-Ref`) from a SourcePackagePublishingHistory's uploaded
    `.changes` file, as a dict of the keys present. An upload made with
    git-ubuntu-aware tooling carries all three; without them the importer
    cannot graft the MP's real commits and synthesizes an import commit
    instead (design_journal.md #49).

    Returns:
    - dict (possibly EMPTY -- .changes fetched fine, keys simply absent:
      the upload genuinely carried no rich-history metadata), or
    - None: the .changes couldn't be resolved/fetched -- can't diagnose;
      callers must fall back to their undiagnosed behavior, not treat
      this as "keys missing".

    Same plain unauthenticated librarian fetch as changelog_text() above,
    and the same caveat: not yet exercised against a live publication.
    A signed .changes wraps the fields in a PGP clearsign envelope, which
    doesn't matter for a line-wise scan.
    """
    try:
        url = pub.changesFileUrl()
    except Exception as e:
        logger.warning("could not resolve changesFileUrl(): %s", e)
        return None
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.warning("could not fetch .changes at %s: %s", url, e)
        return None
    keys = {}
    for line in text.splitlines():
        for key in ("Vcs-Git", "Vcs-Git-Commit", "Vcs-Git-Ref"):
            if line.startswith(key + ":"):
                keys[key] = line[len(key) + 1 :].strip()
    return keys


def queue_changes_text(upload):
    """The `Changes:` field of a queued PackageUpload's .changes file,
    decoded back into changelog-stanza form, or None if it can't be
    fetched/parsed.

    A PackageUpload exposes `changes_file_url` (an attribute, unlike a
    publication's changesFileUrl() method), publicly fetchable even for
    the Unapproved queue (verified live, libp11 noble upload 38582499).
    The Changes field carries the new changelog stanza(s) in RFC822
    continuation encoding: every line prefixed with one space, blank
    lines encoded as ' .'. It has NO ' -- maintainer' trailer line --
    callers comparing against a debian/changelog stanza must ignore the
    trailer on their side (design_journal.md #56)."""
    url = getattr(upload, "changes_file_url", None)
    if not url:
        logger.warning("queued upload has no changes_file_url")
        return None
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode(errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.warning("could not fetch queue .changes at %s: %s", url, e)
        return None
    lines = None
    for line in text.splitlines():
        if lines is None:
            if line.startswith("Changes:"):
                lines = []
            continue
        if not line.startswith(" "):
            break  # end of the folded field
        line = line[1:]
        lines.append("" if line == "." else line)
    if not lines:
        logger.warning("no Changes field found in queue .changes at %s", url)
        return None
    return "\n".join(lines)


# --- Version comparison (apt_pkg) ---------------------------------------------


def has_ubuntu_delta(version):
    """True if this version string carries Ubuntu-specific changes.

    A plain no-change rebuild uses a 'buildN' suffix; only a 'ubuntuN'
    suffix means the packaging actually diverges from Debian.
    """
    return "ubuntu" in (version or "").lower()


def version_compare(a, b):
    """-1/0/1 per Debian version ordering (wraps apt_pkg.version_compare)."""
    _ensure_apt_pkg()
    return apt_pkg.version_compare(a, b)


def is_at_least(candidate, minimum):
    """True if `candidate` is >= `minimum` (Debian version ordering)."""
    return version_compare(candidate, minimum) >= 0
