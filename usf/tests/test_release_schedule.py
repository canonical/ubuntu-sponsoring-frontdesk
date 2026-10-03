"""release_schedule.is_after_feature_freeze: fails safe (False) when the
manually-maintained FEATURE_FREEZE constant hasn't been set for the cycle."""

import datetime

import release_schedule


def test_unset_freeze_date_is_never_after(monkeypatch):
    monkeypatch.setattr(release_schedule, "FEATURE_FREEZE", None)
    assert release_schedule.is_after_feature_freeze() is False


def test_before_and_after_freeze_date(monkeypatch):
    monkeypatch.setattr(release_schedule, "FEATURE_FREEZE", datetime.date(2026, 6, 1))
    assert release_schedule.is_after_feature_freeze(datetime.date(2026, 5, 1)) is False
    assert release_schedule.is_after_feature_freeze(datetime.date(2026, 7, 1)) is True
    assert release_schedule.is_after_feature_freeze(datetime.date(2026, 6, 1)) is False
