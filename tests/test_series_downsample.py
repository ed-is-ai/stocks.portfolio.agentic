"""Unit tests for the pure chart downsampler (#421)."""

from datetime import datetime, timedelta

import pytest

from app.services.series_downsample import downsample_last_per_bucket


def _rows(n: int, *, step_hours: float = 12.0) -> list[tuple[str, float]]:
    start = datetime(2020, 1, 1)
    return [
        ((start + timedelta(hours=step_hours * i)).isoformat(), float(i))
        for i in range(n)
    ]


def test_empty_input_returned_unchanged() -> None:
    assert downsample_last_per_bucket([], 250) == []


def test_single_row_returned_unchanged() -> None:
    rows = _rows(1)
    assert downsample_last_per_bucket(rows, 250) == rows


def test_already_small_series_untouched() -> None:
    rows = _rows(200)
    assert downsample_last_per_bucket(rows, 250) == rows


def test_long_span_capped_to_max_points() -> None:
    # ~5 years at two snapshots/day -> ~3650 rows.
    rows = _rows(3650)
    out = downsample_last_per_bucket(rows, 250)
    assert len(out) <= 250
    assert len(out) > 1


def test_endpoints_are_retained() -> None:
    rows = _rows(3650)
    out = downsample_last_per_bucket(rows, 250)
    assert out[0] == rows[0]
    assert out[-1] == rows[-1]


def test_chronological_order_preserved() -> None:
    rows = _rows(3650)
    out = downsample_last_per_bucket(rows, 250)
    stamps = [r[0] for r in out]
    assert stamps == sorted(stamps)


def test_last_row_of_each_bucket_is_kept() -> None:
    # 40 rows over 40 days, max_points 10 -> 4-day buckets, last per bucket.
    rows = [
        ((datetime(2021, 1, 1) + timedelta(days=i)).isoformat(), float(i))
        for i in range(40)
    ]
    out = downsample_last_per_bucket(rows, 10)
    assert len(out) <= 10
    assert len(out) < len(rows)
    assert out[0] == rows[0]
    assert out[-1] == rows[-1]
    # Values increase with the day index, so a last-per-bucket pick skips the
    # intermediate rows while preserving order.
    assert [r[1] for r in out] == sorted(r[1] for r in out)
    assert out[1][1] == 7.0  # last row of the second 4-day bucket (days 4..7)


def test_single_calendar_day_window_returns_exactly_two() -> None:
    # Many rows all on one calendar day -> span_days == 0; must not collapse
    # to a single point (#421).
    start = datetime(2022, 3, 1)
    rows = [((start + timedelta(minutes=i)).isoformat(), float(i)) for i in range(500)]
    out = downsample_last_per_bucket(rows, 250)
    assert out == [rows[0], rows[-1]]


def test_span_equal_to_max_points_stays_within_cap() -> None:
    # span_days == max_points is the boundary that could yield max_points + 1
    # buckets (#421).
    rows = [
        ((datetime(2020, 1, 1) + timedelta(days=i)).isoformat(), float(i))
        for i in range(11)
    ]
    out = downsample_last_per_bucket(rows, 10)
    assert len(out) <= 10
    assert out[0] == rows[0]
    assert out[-1] == rows[-1]


def test_max_points_below_one_rejected() -> None:
    with pytest.raises(ValueError):
        downsample_last_per_bucket(_rows(5), 0)
