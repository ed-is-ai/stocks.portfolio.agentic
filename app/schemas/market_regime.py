"""The persisted market-regime snapshot rendered as the scanner-screen banner.

Mirrors ``app/schemas/market_narrative.py``: a small pydantic model written
once per scan run (see ``app.agents.scanner.market_regime_snapshot``) and read
back at render time so the banner never triggers a second SPY download.

``sma_200`` / ``latest_close`` / ``session_count`` are carried verbatim from
``MarketRegimeReadingV1`` (its docstring reserves them for #387/#388); they are
persisted but not rendered by the current banner.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.market_regime import MarketRegimeReadingV1


class MarketRegimeSnapshotV1(BaseModel):
    """A single scan run's broad-market regime reading, persisted for the UI."""

    spy_uptrend: bool
    return_52w_pct: float
    sma_200: float | None = None
    latest_close: float | None = None
    session_count: int = 0
    is_degraded: bool = False
    generated_at: str = ""  # ISO timestamp; set by the builder

    @classmethod
    def from_reading(
        cls, reading: MarketRegimeReadingV1, generated_at: str
    ) -> "MarketRegimeSnapshotV1":
        """Build a snapshot from the scan's own regime reading.

        ``+ 0.0`` normalises a ``-0.0`` return (a tiny negative move that
        rounds to zero) so the banner never renders ``-0.0%``.
        """
        return cls(
            spy_uptrend=reading.spy_uptrend,
            return_52w_pct=reading.return_52w_pct + 0.0,
            sma_200=reading.sma_200,
            latest_close=reading.latest_close,
            session_count=reading.session_count,
            is_degraded=reading.is_degraded,
            generated_at=generated_at,
        )
