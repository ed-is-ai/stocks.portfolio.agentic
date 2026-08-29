"""Unit tests for the pure backtest activity presenter helpers."""

from datetime import datetime, timedelta, timezone

from app.services.backtest.activity_presenter import absolute_time, relative_time

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_recent_run_shows_hours_ago() -> None:
    assert relative_time(NOW - timedelta(hours=2), now=NOW) == "2h ago"


def test_sub_minute_run_shows_just_now() -> None:
    assert relative_time(NOW - timedelta(seconds=30), now=NOW) == "just now"


def test_this_year_run_shows_day_and_month() -> None:
    assert relative_time(datetime(2026, 8, 12, 9, 5, tzinfo=timezone.utc), now=NOW) == (
        "12 Aug"
    )


def test_prior_year_run_shows_year() -> None:
    assert relative_time(datetime(2024, 8, 12, 9, 5, tzinfo=timezone.utc), now=NOW) == (
        "12 Aug 2024"
    )


def test_naive_timestamp_treated_as_utc() -> None:
    assert relative_time(datetime(2026, 8, 29, 10, 0), now=NOW) == "2h ago"


def test_future_timestamp_clamped_to_just_now() -> None:
    assert relative_time(NOW + timedelta(minutes=5), now=NOW) == "just now"


def test_year_boundary_prior_year_shows_year() -> None:
    ref = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)
    moment = datetime(2024, 12, 31, 12, 0, tzinfo=timezone.utc)
    assert relative_time(moment, now=ref) == "31 Dec 2024"


def test_minute_boundary_60s() -> None:
    assert relative_time(NOW - timedelta(seconds=60), now=NOW) == "1m ago"


def test_hour_boundary_3600s() -> None:
    assert relative_time(NOW - timedelta(seconds=3600), now=NOW) == "1h ago"


def test_day_boundary_86400s() -> None:
    assert relative_time(NOW - timedelta(seconds=86400), now=NOW) == "28 Aug"


def test_just_under_minute() -> None:
    assert relative_time(NOW - timedelta(seconds=59), now=NOW) == "just now"


def test_minutes_ago() -> None:
    assert relative_time(NOW - timedelta(minutes=5), now=NOW) == "5m ago"


def test_default_now_does_not_raise() -> None:
    assert relative_time(datetime.now(timezone.utc)) == "just now"


def test_absolute_time_fixed_format() -> None:
    assert absolute_time(datetime(2026, 8, 12, 9, 5, tzinfo=timezone.utc)) == (
        "2026-08-12 09:05 UTC"
    )


def test_absolute_time_naive_treated_as_utc() -> None:
    assert absolute_time(datetime(2026, 8, 12, 9, 5)) == "2026-08-12 09:05 UTC"


def test_absolute_time_converts_offset_to_utc() -> None:
    tz = timezone(timedelta(hours=2))
    assert absolute_time(datetime(2026, 8, 12, 11, 5, tzinfo=tz)) == (
        "2026-08-12 09:05 UTC"
    )
