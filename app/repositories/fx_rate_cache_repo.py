"""Repository for pair-aware historical FX rates in ``trades.db``."""

from app.repositories.db import Connect, session


class FxRateCacheRepository:
    """Typed access to the cached historical FX rate table.

    A dumb store: it returns/persists whatever values it's given, including
    an invalid (``0``/negative/``NULL``) stored rate — filtering a rate's
    validity is ``PortfolioService``'s responsibility, not this repository's.
    """

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get_many(self, dates: list[str], pair: str = "GBPUSD=X") -> dict[str, float]:
        """Return stored ``{date: rate}`` rows for one FX ``pair``.

        A date with no stored row is simply absent from the returned dict.
        """
        if not dates:
            return {}
        with session(self._connect) as conn:
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"SELECT date, rate FROM fx_rate_cache "
                f"WHERE pair = ? AND date IN ({placeholders})",
                [pair, *dates],
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def upsert_many(self, rates: dict[str, float], pair: str = "GBPUSD=X") -> None:
        """Persist ``{date: rate}`` for ``pair``, overwriting pair/date rows."""
        with session(self._connect) as conn:
            for rate_date, rate in rates.items():
                conn.execute(
                    "INSERT INTO fx_rate_cache (pair, date, rate) VALUES (?, ?, ?)"
                    " ON CONFLICT(pair, date) DO UPDATE SET rate = excluded.rate",
                    (pair, rate_date, rate),
                )
