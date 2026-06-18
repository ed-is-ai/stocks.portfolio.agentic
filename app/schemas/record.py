"""The composed scan + analysis record."""

from __future__ import annotations

from app.schemas.scan import StockAnalysis, StockScan


class StockRecord(StockScan):
    analysis: StockAnalysis | None = None
