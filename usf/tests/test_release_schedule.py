"""release_schedule.is_after_feature_freeze: fails safe (False) when the
manually-maintained FEATURE_FREEZE constants aren't set for the cycle, and
(#178) when the date belongs to a series that is no longer the devel one."""

import datetime

import release_schedule


def _set(monkeypatch, series="stonking", date=datetime.date(2026, 6, 1)):
    monkeypatch.setattr(release_schedule, "FEATURE_FREEZE_SERIES", series)
    monkeypatch.setattr(release_schedule, "FEATURE_FREEZE", date)


def test_unset_freeze_date_is_never_after(monkeypatch):
    _set(monkeypatch, date=None)
    assert release_schedule.is_after_feature_freeze("stonking") is False


def test_unset_freeze_series_is_never_after(monkeypatch):
    _set(monkeypatch, series=None)
    assert release_schedule.is_after_feature_freeze("stonking") is False


def test_before_and_after_freeze_date(monkeypatch):
    _set(monkeypatch)
    after = release_schedule.is_after_feature_freeze
    assert after("stonking", datetime.date(2026, 5, 1)) is False
    assert after("stonking", datetime.date(2026, 7, 1)) is True
    assert after("stonking", datetime.date(2026, 6, 1)) is False


def test_a_date_for_the_previous_devel_series_no_longer_applies(monkeypatch):
    """#178 (live, alsa-ucm-conf MP #514634): stonking's Feature Freeze
    kept applying after tenacious opened."""
    _set(monkeypatch)
    assert (
        release_schedule.is_after_feature_freeze("tenacious", datetime.date(2026, 11, 12)) is False
    )


def test_an_unknown_devel_series_fails_safe(monkeypatch):
    _set(monkeypatch)
    assert release_schedule.is_after_feature_freeze(None, datetime.date(2026, 11, 12)) is False
