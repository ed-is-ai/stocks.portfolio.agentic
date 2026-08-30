"""Pure server-side downsampling for the portfolio value-history chart (#421).

The chart can span up to five years of snapshots written on every value
refresh and every SIPP import. Rendering thousands of points is wasteful and
unreadable, so the selected window is reduced to at most ``max_points``
samples — the last row of each fixed-width day bucket — before it reaches the
template.
"""

from collections.abc import Sequence
from datetime import datetime
from math import ceil
from typing import Any, TypeVar

Row = TypeVar("Row", bound=Sequence[Any])


def _parse_day(timestamp: Any) -> datetime | None:
    """Parse the ``YYYY-MM-DD`` prefix of an ISO timestamp, or ``None``."""
    try:
        return datetime.strptime(str(timestamp)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def downsample_last_per_bucket(rows: list[Row], max_points: int) -> list[Row]:
    """Reduce ``rows`` to ``<= max_points`` samples, last-per-time-bucket.

    ``rows`` are assumed chronological and each row's first element is an ISO
    timestamp string. Buckets are ``max(1, ceil(span_days / max_points))``-day
    windows measured from the first row; the last row seen in each bucket is
    kept and the final row is always kept. Chronological order is preserved.
    Inputs of 0, 1, or already ``<= max_points`` rows are returned unchanged.
    """
    if max_points < 1:
        raise ValueError("max_points must be >= 1")
    if len(rows) <= max_points:
        return list(rows)

    first_day = _parse_day(rows[0][0])
    last_day = _parse_day(rows[-1][0])
    if first_day is None or last_day is None:
        return list(rows)
    span_days = max((last_day - first_day).days, 0)
    bucket_days = max(1, ceil(span_days / max_points))

    kept: list[Row] = []
    last_bucket: int | None = None
    for row in rows:
        day = _parse_day(row[0])
        bucket = (
            (day - first_day).days // bucket_days if day is not None else last_bucket
        )
        if kept and bucket == last_bucket:
            kept[-1] = row
        else:
            kept.append(row)
            last_bucket = bucket
    # A window whose rows all fall in one bucket (e.g. a single calendar day)
    # leaves one survivor; the two endpoint writes below would then collapse
    # to a single row. Return the true first and last instead.
    if len(kept) < 2:
        return [rows[0], rows[-1]]
    # Both endpoints are always the real first/last rows, even when the first
    # row was not the last sample in its bucket.
    kept[0] = rows[0]
    kept[-1] = rows[-1]
    # Bucket indices run ``0..span_days // bucket_days`` inclusive, which is
    # ``max_points + 1`` slots when ``span_days == max_points``. Drop the
    # penultimate sample(s) to honour the ``<= max_points`` guarantee.
    if len(kept) > max_points:
        kept = kept[: max_points - 1] + [kept[-1]]
    return kept
