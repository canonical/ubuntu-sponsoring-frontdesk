"""archive_lookup.is_native_source (#77): the published .dsc's Format:
field decides native vs non-native; Format 1.0 (ambiguous by design) and
missing/unreadable Format fall back to the source file names."""

import types

import archive_lookup


class _Pub:
    def __init__(self, files):
        self._files = files

    def sourceFileUrls(self):
        return [f"https://launchpadlibrarian.net/1/{name}" for name in self._files]


def _lp(pubs):
    archive = types.SimpleNamespace(getPublishedSources=lambda **kw: pubs)
    ubuntu = types.SimpleNamespace(
        main_archive=archive, getSeries=lambda name_or_version: name_or_version
    )
    return types.SimpleNamespace(distributions={"ubuntu": ubuntu})


def _serve_dsc(monkeypatch, body):
    class _Resp:
        def __init__(self):
            self._body = body.encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(archive_lookup.urllib.request, "urlopen", lambda url, timeout=15: _Resp())


SIGNED_NATIVE_DSC = """\
-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Format: 3.0 (native)
Source: unity
"""


def test_format_native_wins(monkeypatch):
    _serve_dsc(monkeypatch, SIGNED_NATIVE_DSC)
    # File list even looks non-native -- the Format field is authoritative.
    pub = _Pub(["unity_7.7.1.dsc", "unity_7.7.1.debian.tar.xz"])
    assert archive_lookup.is_native_source(_lp([pub]), "unity", "stonking") is True


def test_format_quilt_is_non_native(monkeypatch):
    _serve_dsc(monkeypatch, "Format: 3.0 (quilt)\nSource: gnutls28\n")
    pub = _Pub(["gnutls28_3.8.dsc", "gnutls28_3.8.orig.tar.xz"])
    assert archive_lookup.is_native_source(_lp([pub]), "gnutls28", "stonking") is False


def test_format_1_0_tiebreaks_on_diff_gz(monkeypatch):
    _serve_dsc(monkeypatch, "Format: 1.0\nSource: oldpkg\n")
    diffed = _Pub(["oldpkg_1.dsc", "oldpkg_1.orig.tar.gz", "oldpkg_1-2.diff.gz"])
    assert archive_lookup.is_native_source(_lp([diffed]), "oldpkg", "stonking") is False
    plain = _Pub(["oldpkg_1.dsc", "oldpkg_1.tar.gz"])
    assert archive_lookup.is_native_source(_lp([plain]), "oldpkg", "stonking") is True


def test_no_publication_is_inconclusive():
    assert archive_lookup.is_native_source(_lp([]), "newpkg", "stonking") is None


def test_dsc_fetch_failure_is_inconclusive(monkeypatch):
    def _boom(url, timeout=15):
        raise TimeoutError("librarian timeout")

    monkeypatch.setattr(archive_lookup.urllib.request, "urlopen", _boom)
    pub = _Pub(["pkg_1.dsc", "pkg_1.tar.gz"])
    assert archive_lookup.is_native_source(_lp([pub]), "pkg", "stonking") is None
