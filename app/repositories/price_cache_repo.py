"""Repository for the ``price_cache`` table in ``trades.db``."""

from datetime import datetime, timezone
from typing import Any

from app.repositories.db import Connect, session


class PriceCacheRepository:
    """Typed access to the cached price table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def upsert_many(
        self,
        prices: dict[str, float],
        currencies: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        """Persist fetched prices with a UTC timestamp.

        ``currencies`` is an optional ``{ticker: (original_price, currency)}``
        map for display in the native currency.
        """
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with session(self._connect) as conn:
            for ticker, price in prices.items():
                orig = currencies.get(ticker) if currencies else None
                orig_price = orig[0] if orig else None
                currency = orig[1] if orig else "GBP"
                conn.execute(
                    "INSERT INTO price_cache"
                    " (ticker, price, fetched_at, currency, original_price)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(ticker) DO UPDATE SET"
                    "  price=excluded.price, fetched_at=excluded.fetched_at,"
                    "  currency=excluded.currency,"
                    "  original_price=excluded.original_price",
                    (ticker, price, fetched_at, currency, orig_price),
                )
            conn.commit()

    def load_all(self) -> list[tuple[Any, ...]]:
        """Return all cached rows: (ticker, price, fetched_at, currency, original_price)."""
        with session(self._connect) as conn:
            return conn.execute(
                "SELECT ticker, price, fetched_at, currency, original_price"
                " FROM price_cache"
            ).fetchall()
