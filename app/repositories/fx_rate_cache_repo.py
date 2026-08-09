"""Repository for the ``fx_rate_cache`` table in ``trades.db``.

Caches historical GBP/USD rates keyed by calendar date, so
``PortfolioService.historical_gbpusd_rates`` never re-fetches the same
date's rate from ``yfinance`` on a later view load (Epic 1, Story 1.2).
"""

from app.repositories.db import Connect, session


class FxRateCacheRepository:
    """Typed access to the cached historical GBP/USD rate table.

    A dumb store: it returns/persists whatever values it's given, including
    an invalid (``0``/negative/``NULL``) stored rate — filtering a rate's
    validity is ``PortfolioService``'s responsibility, not this repository's.
    """

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get_many(self, dates: list[str]) -> dict[str, float]:
        """Return every stored ``{date: gbpusd_rate}`` row for ``dates``.

        A date with no stored row is simply absent from the returned dict.
        """
        if not dates:
            return {}
        with session(self._connect) as conn:
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"SELECT date, gbpusd_rate FROM fx_rate_cache "
                f"WHERE date IN ({placeholders})",
                dates,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def upsert_many(self, rates: dict[str, float]) -> None:
        """Persist ``{date: gbpusd_rate}``, overwriting any existing row."""
        with session(self._connect) as conn:
            for rate_date, rate in rates.items():
                conn.execute(
                    "INSERT INTO fx_rate_cache (date, gbpusd_rate) VALUES (?, ?)"
                    " ON CONFLICT(date) DO UPDATE SET"
                    "  gbpusd_rate = excluded.gbpusd_rate",
                    (rate_date, rate),
                )
