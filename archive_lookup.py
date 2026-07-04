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


def published_source(lp, package, series_name, version):
    """The SourcePackagePublishingHistory for `package` == `version` in
    `series_name` (any pocket; first match), or None if there's no such
    publication or the lookup fails."""
    try:
        ubuntu = lp.distributions["ubuntu"]
        archive = ubuntu.main_archive
        series = ubuntu.getSeries(name_or_version=series_name)
        pubs = archive.getPublishedSources(
            source_name=package,
            exact_match=True,
            distro_series=series,
            version=version,
            status="Published",
        )
        for pub in pubs:
            return pub
        return None
    except Exception as e:
        logger.warning(
            "Launchpad lookup failed (published source for %s %s in %s): %s",
            package,
            version,
            series_name,
            e,
        )
        return None


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
