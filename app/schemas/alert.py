"""Alert-stage schema."""

from pydantic import BaseModel, Field


class AlertSummary(BaseModel):
    """Result of the alert stage — replaces ``AlertAgent.run``'s bare ``int``.

    ``buy_count`` is the number of buy/breakout alerts queued this run; ``tickers``
    lists the tickers that triggered them.
    """

    buy_count: int
    tickers: list[str] = Field(default_factory=list)
