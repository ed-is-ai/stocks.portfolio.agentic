"""Repository for persisted ticker currency resolutions in ``trades.db``.

A ticker no longer present in the live price-scan cache (delisted, or sold
long enough ago to have dropped off the watchlist) has no fast currency
source, so ``PortfolioService.ticker_currency`` falls back to a live
``yfinance`` lookup. That lookup is cached in-process (``@lru_cache``), but
the cache resets on every server restart -- for a delisted ticker the lookup
also fails and falls back to a guessed default, so a restart re-pays that
network cost (and the same guess) every time. This table makes the
resolution durable across restarts, mirroring ``fx_rate_cache``'s pattern.
"""

from datetime import datetime, timezone

from app.repositories.db import Connect, session


class TickerCurrencyCacheRepository:
    """Typed access to the ``ticker_currency_cache`` table.

    A dumb store: it returns/persists whatever currency it's given,
    including a guessed default from a failed lookup -- the same value
    ``PortfolioService.ticker_currency``'s in-process cache would already
    hold for the life of the running server. This only extends that same
    result's lifetime across restarts; it introduces no new risk.
    """

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get_many(self, tickers: list[str]) -> dict[str, str]:
        """Return stored ``{ticker: currency}`` rows.

        A ticker with no stored row is simply absent from the returned dict.
        """
        if not tickers:
            return {}
        with session(self._connect) as conn:
            placeholders = ",".join("?" for _ in tickers)
            rows = conn.execute(
                f"SELECT ticker, currency FROM ticker_currency_cache"
                f" WHERE ticker IN ({placeholders})",
                tickers,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def upsert_many(self, currencies: dict[str, str]) -> None:
        """Persist ``{ticker: currency}``, overwriting existing rows."""
        if not currencies:
            return
        resolved_at = datetime.now(timezone.utc).isoformat()
        with session(self._connect) as conn:
            for ticker, currency in currencies.items():
                conn.execute(
                    "INSERT INTO ticker_currency_cache"
                    " (ticker, currency, resolved_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(ticker) DO UPDATE SET"
                    " currency = excluded.currency,"
                    " resolved_at = excluded.resolved_at",
                    (ticker, currency, resolved_at),
                )
