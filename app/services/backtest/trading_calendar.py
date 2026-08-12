"""Canonical exchange-session authority for Strategy Manager."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import lru_cache
import re

import exchange_calendars as xcals
import pandas as pd

_MIC_TO_CALENDAR = {"XNAS": "XNYS", "XNYS": "XNYS", "XLON": "XLON"}
_DIGEST_START = "1970-01-01"
_DIGEST_END = "2100-12-31"
_MONTH_RE = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")


@lru_cache(maxsize=1)
def _canonical_session_table_bytes() -> bytes:
    records: list[dict[str, str]] = []
    for name in ("XNYS", "XLON"):
        schedule = xcals.get_calendar(
            name, start=_DIGEST_START, end=_DIGEST_END
        ).schedule
        for session, row in schedule.iterrows():
            records.append(
                {
                    "calendar": name,
                    "session": pd.Timestamp(str(session)).date().isoformat(),
                    "open": row["open"].isoformat(),
                    "close": row["close"].isoformat(),
                }
            )
    return json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class CalendarContractError(ValueError):
    """Stable server-side rejection for invalid snapshot calendar input."""

    code = "calendar_error"


def _month_parts(month: str) -> tuple[int, int]:
    match = _MONTH_RE.fullmatch(month)
    if match is None:
        raise CalendarContractError(f"Malformed snapshot month: {month}")
    year, number = int(match.group(1)), int(match.group(2))
    if year < 1970 or year > 2100:
        raise CalendarContractError(
            f"Snapshot month is outside calendar authority: {month}"
        )
    return year, number


def _next_month(month: str) -> str:
    year, number = _month_parts(month)
    if number == 12:
        year += 1
        number = 1
    else:
        number += 1
    if year > 2100:
        raise CalendarContractError("Snapshot month exceeds calendar authority")
    return f"{year:04d}-{number:02d}"


class TradingCalendar:
    """Expose the closed MIC mapping and canonical XNYS/XLON sessions."""

    @staticmethod
    def calendar_name(mic: str) -> str:
        try:
            return _MIC_TO_CALENDAR[mic]
        except KeyError as exc:
            raise ValueError(f"Unsupported MIC: {mic}") from exc

    def _calendar(self, mic: str):
        # exchange_calendars otherwise builds a rolling default window around
        # today, making the supposedly canonical digest change at midnight and
        # excluding older/future backtest sessions from the authority table.
        return xcals.get_calendar(
            self.calendar_name(mic), start=_DIGEST_START, end=_DIGEST_END
        )

    def is_session(self, mic: str, session: str | date) -> bool:
        return bool(self._calendar(mic).is_session(pd.Timestamp(session)))

    def session_open(self, mic: str, session: str | date) -> pd.Timestamp:
        return self._calendar(mic).session_open(pd.Timestamp(session))

    def session_close(self, mic: str, session: str | date) -> pd.Timestamp:
        return self._calendar(mic).session_close(pd.Timestamp(session))

    def last_session_of_month(self, mic: str, month: str) -> date:
        _month_parts(month)
        period = pd.Period(month, freq="M")
        sessions = self._calendar(mic).sessions_in_range(
            period.start_time.normalize(), period.end_time.normalize()
        )
        if len(sessions) == 0:
            raise ValueError(f"No sessions for {mic} in {month}")
        return sessions[-1].date()

    @staticmethod
    def closed_month(month: str, *, as_of: date) -> str:
        """Validate one fully closed historical calendar month."""
        year, number = _month_parts(month)
        if (year, number) >= (as_of.year, as_of.month):
            raise CalendarContractError(
                f"Snapshot month must be fully closed before {as_of.isoformat()}"
            )
        return month

    def month_sessions(
        self, mics: tuple[str, ...], month: str, *, as_of: date
    ) -> dict[str, date]:
        """Resolve stable per-MIC month-end sessions for a closed month."""
        normalized = self.closed_month(month, as_of=as_of)
        resolved: dict[str, date] = {}
        for mic in sorted(set(mics)):
            try:
                resolved[mic] = self.last_session_of_month(mic, normalized)
            except ValueError as exc:
                raise CalendarContractError(str(exc)) from exc
        return resolved

    @staticmethod
    def months_inclusive(start_month: str, end_month: str) -> tuple[str, ...]:
        _month_parts(start_month)
        _month_parts(end_month)
        if start_month > end_month:
            raise CalendarContractError("Snapshot start month follows end month")
        months: list[str] = []
        current = start_month
        while True:
            months.append(current)
            if current == end_month:
                return tuple(months)
            current = _next_month(current)

    @staticmethod
    def contiguous_month_intervals(
        months: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        ordered = tuple(sorted(set(months)))
        for month in ordered:
            _month_parts(month)
        if not ordered:
            return ()
        intervals: list[tuple[str, str]] = []
        start = previous = ordered[0]
        for month in ordered[1:]:
            if month != _next_month(previous):
                intervals.append((start, previous))
                start = month
            previous = month
        intervals.append((start, previous))
        return tuple(intervals)

    def session_table_digest(self) -> str:
        """Return SHA-256 for the canonical 1970–2100 XNYS/XLON table."""
        return hashlib.sha256(_canonical_session_table_bytes()).hexdigest()
