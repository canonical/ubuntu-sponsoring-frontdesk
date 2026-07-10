"""archive_lookup.py: archive lookups the sync-request triage relies on.
Debian lookups are tested by monkeypatching urllib.request.urlopen; Ubuntu
lookups by a small fake Launchpad object -- no real network/Launchpad call is
made. Parsing is pure and tested directly."""

import io

import apt_pkg

import archive_lookup


class _Response(io.BytesIO):
    """Minimal stand-in for the object urllib.request.urlopen's context
    manager yields."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- parsing (pure) ----------------------------------------------------------


def test_parse_single_suite_per_line():
    out = "foo | 1.2-3 | unstable | source\nfoo | 1.2-2 | bookworm | source\n"
    assert archive_lookup._parse_madison(out) == {
        "unstable": "1.2-3",
        "bookworm": "1.2-2",
    }


def test_parse_multiple_pockets_on_one_line():
    out = "foo | 1.2-3ubuntu1 | noble-updates, noble-security | source, amd64\n"
    assert archive_lookup._parse_madison(out) == {
        "noble-updates": "1.2-3ubuntu1",
        "noble-security": "1.2-3ubuntu1",
    }


def test_parse_ignores_unmatched_lines():
    out = "Not a data line\nfoo | 1.0 | unstable | source\n"
    assert archive_lookup._parse_madison(out) == {"unstable": "1.0"}


def test_parse_empty_output():
    assert archive_lookup._parse_madison("") == {}


# --- has_ubuntu_delta ---------------------------------------------------------


def test_ubuntu_suffix_is_a_delta():
    assert archive_lookup.has_ubuntu_delta("1.2-3ubuntu1") is True


def test_build_suffix_is_not_a_delta():
    assert archive_lookup.has_ubuntu_delta("1.2-3build1") is False


def test_plain_debian_version_is_not_a_delta():
    assert archive_lookup.has_ubuntu_delta("1.2-3") is False


def test_empty_version_is_not_a_delta():
    assert archive_lookup.has_ubuntu_delta("") is False
    assert archive_lookup.has_ubuntu_delta(None) is False


# --- debian_versions (api.ftp-master.debian.org/madison) ----------------------


def test_debian_versions_returns_none_on_network_error(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_error(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", raise_error)
    assert archive_lookup.debian_versions("foo") is None


def test_debian_versions_returns_empty_dict_for_unknown_package(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(b""))
    assert archive_lookup.debian_versions("doesnotexist") == {}


def test_debian_versions_passes_package_and_suite_filter(monkeypatch):
    import urllib.request

    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _Response(b" foo | 1.0 | sid | source\n")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = archive_lookup.debian_versions("foo", suites=["unstable"])
    assert "package=foo" in captured["url"]
    assert "s=unstable" in captured["url"]
    assert "text=on" in captured["url"]
    assert result == {"sid": "1.0"}


# --- devel_codename / ubuntu_versions (Launchpad API) -------------------------


class _FakeSeries:
    def __init__(self, name):
        self.name = name


class _FakePublication:
    def __init__(self, version, pocket="Release", status="Published"):
        self.source_package_version = version
        self.pocket = pocket
        self.status = status


class _FakeArchive:
    def __init__(self, publications):
        self._publications = publications

    def getPublishedSources(
        self, source_name, exact_match, distro_series, version=None, status=None
    ):
        # Mirrors the real Launchpad API (confirmed live, design_journal.md
        # #43): omitting `status` entirely returns every status, not just
        # Published -- so the fake's default must be None too, not
        # "Published", or it wouldn't reproduce what published_source's
        # status=None path actually does against the real API.
        pubs = self._publications.get((source_name, distro_series.name), [])
        if version is not None:
            pubs = [p for p in pubs if p.source_package_version == version]
        if status is not None:
            pubs = [p for p in pubs if p.status == status]
        return pubs


class _FakeDistribution:
    def __init__(self, devel_name, publications=None):
        self.current_series = _FakeSeries(devel_name)
        self.main_archive = _FakeArchive(publications or {})
        self._devel_name = devel_name

    def getSeries(self, name_or_version):
        return _FakeSeries(name_or_version)


class _FakeLP:
    def __init__(self, devel_name="noble", publications=None):
        self.distributions = {"ubuntu": _FakeDistribution(devel_name, publications)}


def test_devel_codename_reads_current_series():
    assert archive_lookup.devel_codename(_FakeLP(devel_name="plucky")) == "plucky"


def test_devel_codename_none_on_lookup_failure():
    lp = _FakeLP()
    del lp.distributions
    assert archive_lookup.devel_codename(lp) is None


def test_ubuntu_versions_returns_none_on_lookup_failure():
    lp = _FakeLP()
    del lp.distributions
    assert archive_lookup.ubuntu_versions(lp, "foo") is None


def test_ubuntu_versions_empty_for_unpublished_package():
    lp = _FakeLP(devel_name="noble")
    assert archive_lookup.ubuntu_versions(lp, "doesnotexist") == {}


def test_ubuntu_versions_combines_pockets():
    lp = _FakeLP(
        devel_name="noble",
        publications={
            ("foo", "noble"): [
                _FakePublication("1.0-1", pocket="Release"),
                _FakePublication("1.0-2", pocket="Proposed"),
            ]
        },
    )
    assert archive_lookup.ubuntu_versions(lp, "foo", series_names=["noble"]) == {
        "noble": "1.0-1",
        "noble-proposed": "1.0-2",
    }


# --- is_at_least ---------------------------------------------------------------


def test_is_at_least_uses_apt_pkg_version_compare(monkeypatch):
    captured = {}

    def fake_compare(v1, v2):
        captured["args"] = (v1, v2)
        return 1

    monkeypatch.setattr(apt_pkg, "version_compare", fake_compare)
    assert archive_lookup.is_at_least("1.2-3", "1.2-2") is True
    assert captured["args"] == ("1.2-3", "1.2-2")


def test_is_at_least_true_when_equal(monkeypatch):
    monkeypatch.setattr(apt_pkg, "version_compare", lambda v1, v2: 0)
    assert archive_lookup.is_at_least("1.2-3", "1.2-3") is True


def test_is_at_least_false_when_older(monkeypatch):
    monkeypatch.setattr(apt_pkg, "version_compare", lambda v1, v2: -1)
    assert archive_lookup.is_at_least("1.0", "2.0") is False


# --- version_compare -----------------------------------------------------


def test_version_compare_wraps_apt_pkg(monkeypatch):
    captured = {}

    def fake_compare(v1, v2):
        captured["args"] = (v1, v2)
        return -1

    monkeypatch.setattr(apt_pkg, "version_compare", fake_compare)
    assert archive_lookup.version_compare("1.0", "2.0") == -1
    assert captured["args"] == ("1.0", "2.0")


# --- published_source ------------------------------------------------------


def test_published_source_matches_exact_version():
    lp = _FakeLP(
        devel_name="noble",
        publications={("foo", "noble"): [_FakePublication("1.0-1", pocket="Release")]},
    )
    pub = archive_lookup.published_source(lp, "foo", "noble", "1.0-1")
    assert pub is not None
    assert pub.source_package_version == "1.0-1"


def test_published_source_none_when_version_not_found():
    lp = _FakeLP(
        devel_name="noble",
        publications={("foo", "noble"): [_FakePublication("1.0-1", pocket="Release")]},
    )
    assert archive_lookup.published_source(lp, "foo", "noble", "9.9-9") is None


def test_published_source_none_on_lookup_failure():
    lp = _FakeLP()
    del lp.distributions
    assert archive_lookup.published_source(lp, "foo", "noble", "1.0-1") is None


def test_published_source_status_none_finds_superseded(monkeypatch):
    # design_journal.md #43: check_stale_version looks up a version that
    # isn't currently Published (it's been superseded by something newer)
    # to tell "this MP's change already landed" apart from "never uploaded".
    lp = _FakeLP(
        devel_name="noble",
        publications={
            ("foo", "noble"): [_FakePublication("1.0-1", status="Superseded")]
        },
    )
    pub = archive_lookup.published_source(lp, "foo", "noble", "1.0-1", status=None)
    assert pub is not None
    assert pub.status == "Superseded"


def test_published_source_status_none_prefers_non_deleted():
    # A Deleted record's changelog may not be readable -- prefer whichever
    # match is still registered when several statuses exist for one version.
    lp = _FakeLP(
        devel_name="noble",
        publications={
            ("foo", "noble"): [
                _FakePublication("1.0-1", status="Deleted"),
                _FakePublication("1.0-1", status="Superseded"),
            ]
        },
    )
    pub = archive_lookup.published_source(lp, "foo", "noble", "1.0-1", status=None)
    assert pub.status == "Superseded"


# --- changelog_text ----------------------------------------------------------


class _FakePub:
    def __init__(self, url=None, raises=None):
        self._url = url
        self._raises = raises

    def changelogUrl(self):
        if self._raises:
            raise self._raises
        return self._url


def test_changelog_text_fetches_the_resolved_url(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=None: _Response(b"changelog text"),
    )
    pub = _FakePub(url="https://launchpadlibrarian.net/1/foo_1.0-1.changelog")
    assert archive_lookup.changelog_text(pub) == "changelog text"


def test_changelog_text_none_when_url_unresolvable():
    pub = _FakePub(raises=RuntimeError("no permission"))
    assert archive_lookup.changelog_text(pub) is None


def test_changelog_text_none_on_fetch_failure(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_error(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", raise_error)
    pub = _FakePub(url="https://launchpadlibrarian.net/1/foo_1.0-1.changelog")
    assert archive_lookup.changelog_text(pub) is None


def test_published_source_url_basic():
    assert (
        archive_lookup.published_source_url("foo", "1.2-1")
        == "https://launchpad.net/ubuntu/+source/foo/1.2-1"
    )


def test_published_source_url_keeps_debian_version_characters_readable():
    # ~, +, and the epoch ':' are common in Debian versions and valid
    # unescaped in a URL path -- only genuinely unsafe characters should
    # ever get percent-encoded, so the link stays human-readable.
    url = archive_lookup.published_source_url("foo", "1:2.0~exp1+dfsg-1")
    assert url == "https://launchpad.net/ubuntu/+source/foo/1:2.0~exp1+dfsg-1"


# --- queue_changes_text (design #56) ------------------------------------------

_CHANGES_FILE = b"""Format: 1.8
Date: Wed, 07 Jul 2026 22:21:21 +0530
Source: testpkg
Version: 1.2-4
Changes:
 testpkg (1.2-4) stonking; urgency=medium
 .
   * Fix something.
Checksums-Sha1:
 deadbeef 1234 testpkg_1.2-4.dsc
"""


class _Upload:
    changes_file_url = "https://launchpad.net/ubuntu/noble/+upload/1/+files/x.changes"


def test_queue_changes_text_decodes_the_changes_field(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Response(_CHANGES_FILE)
    )
    text = archive_lookup.queue_changes_text(_Upload())
    # One-space continuation prefix stripped, ' .' decoded to a blank line,
    # field ends at the next non-continuation header.
    assert text == "testpkg (1.2-4) stonking; urgency=medium\n\n  * Fix something."


def test_queue_changes_text_none_on_fetch_failure(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_error(*a, **k):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", raise_error)
    assert archive_lookup.queue_changes_text(_Upload()) is None


def test_queue_changes_text_none_without_changes_field(monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Response(b"Format: 1.8\n")
    )
    assert archive_lookup.queue_changes_text(_Upload()) is None


def test_queue_changes_text_none_without_url():
    class NoUrl:
        changes_file_url = None

    assert archive_lookup.queue_changes_text(NoUrl()) is None
