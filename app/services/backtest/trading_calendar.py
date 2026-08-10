"""Canonical exchange-session authority for Strategy Manager."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import cached_property

import exchange_calendars as xcals
import pandas as pd

_MIC_TO_CALENDAR = {"XNAS": "XNYS", "XNYS": "XNYS", "XLON": "XLON"}
_DIGEST_START = "1970-01-01"
_DIGEST_END = "2100-12-31"


class TradingCalendar:
    """Expose the closed MIC mapping and canonical XNYS/XLON sessions."""

    @staticmethod
    def calendar_name(mic: str) -> str:
        try:
            return _MIC_TO_CALENDAR[mic]
        except KeyError as exc:
            raise ValueError(f"Unsupported MIC: {mic}") from exc

    def _calendar(self, mic: str):
        return xcals.get_calendar(self.calendar_name(mic))

    def is_session(self, mic: str, session: str | date) -> bool:
        return bool(self._calendar(mic).is_session(pd.Timestamp(session)))

    def session_open(self, mic: str, session: str | date) -> pd.Timestamp:
        return self._calendar(mic).session_open(pd.Timestamp(session))

    def session_close(self, mic: str, session: str | date) -> pd.Timestamp:
        return self._calendar(mic).session_close(pd.Timestamp(session))

    def last_session_of_month(self, mic: str, month: str) -> date:
        period = pd.Period(month, freq="M")
        sessions = self._calendar(mic).sessions_in_range(
            period.start_time.normalize(), period.end_time.normalize()
        )
        if len(sessions) == 0:
            raise ValueError(f"No sessions for {mic} in {month}")
        return sessions[-1].date()

    @cached_property
    def _session_table_bytes(self) -> bytes:
        records: list[dict[str, str]] = []
        for name in ("XNYS", "XLON"):
            schedule = xcals.get_calendar(name).schedule.loc[_DIGEST_START:_DIGEST_END]
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

    def session_table_digest(self) -> str:
        """Return SHA-256 for the canonical 1970–2100 XNYS/XLON table."""
        return hashlib.sha256(self._session_table_bytes).hexdigest()
