"""Dependency-neutral detector inputs and validation."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.backtest.historical_scan_record import DetectorId, TechnicalsV1
from app.services.backtest.market_planes import SplitContinuousRow


REQUIRED_HISTORY_SESSIONS = 252


class DetectorExecutionError(ValueError):
    def __init__(self, code: str, detector: str, detail: str) -> None:
        self.code = code
        self.detector = detector
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class DetectorContext:
    rows: tuple[SplitContinuousRow, ...]
    technicals: TechnicalsV1 | None = None

    def validate(self, detector: DetectorId) -> None:
        if len(self.rows) != REQUIRED_HISTORY_SESSIONS:
            raise DetectorExecutionError(
                "required_data_missing", detector, "252 completed sessions are required"
            )
        sessions = tuple(row.session for row in self.rows)
        if sessions != tuple(sorted(sessions)) or len(set(sessions)) != len(sessions):
            raise DetectorExecutionError(
                "integrity_error", detector, "detector sessions are invalid"
            )
        for row in self.rows:
            if row.volume is None:
                raise DetectorExecutionError(
                    "required_data_missing", detector, "detector volume is missing"
                )
            if not all(
                value.is_finite()
                for value in (row.open, row.high, row.low, row.close, row.volume)
            ):
                raise DetectorExecutionError(
                    "integrity_error", detector, "detector input is not finite"
                )
