"""Pure presentation helpers for the backtest results list.

Formatting is deliberately locale- and libc-independent: the day is rendered
with ``str(n)`` and the month from a fixed English abbreviation list, so the
helpers never raise on any platform for a valid :class:`~datetime.datetime`.
"""

from datetime import datetime, timezone

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC, treating a naive datetime as already UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def relative_time(value: datetime, *, now: datetime | None = None) -> str:
    """Render how long ago ``value`` was, relative to ``now`` (default: UTC now).

    ``< 60s`` (including clock skew where ``value`` is ahead of ``now``) renders
    ``"just now"``; minutes as ``"5m ago"``; hours as ``"2h ago"``; a same-year
    date as ``"12 Aug"``; an older date as ``"12 Aug 2024"``.
    """
    ref = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    moment = _as_utc(value)
    secs = (ref - moment).total_seconds()
    if secs < _MINUTE:
        return "just now"
    if secs < _HOUR:
        return f"{int(secs // _MINUTE)}m ago"
    if secs < _DAY:
        return f"{int(secs // _HOUR)}h ago"
    day = str(moment.day)
    mon = _MONTHS[moment.month - 1]
    if moment.year == ref.year:
        return f"{day} {mon}"
    return f"{day} {mon} {moment.year}"


def absolute_time(value: datetime) -> str:
    """Render the absolute instant as ``"2026-08-12 09:05 UTC"`` (UTC-normalised)."""
    return f"{_as_utc(value):%Y-%m-%d %H:%M} UTC"
