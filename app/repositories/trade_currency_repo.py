"""Repository for evidence-resolved *trade* currencies in ``trades.db`` (#553).

Deliberately a separate table from ``ticker_currency_cache``: that one holds
the currency a ticker is *quoted* in (the live price feed's display unit),
which is a different fact from the currency its trades were priced in. HSFWA
and SGLN quote in USD but their SIPP trades are in pounds, so writing a trade
verdict into the quote cache would both corrupt the quote unit used to value
``current_value`` and fire that table's realised-P&L revision triggers.

A row is written only when :class:`app.services.trade_currency_resolver.
TradeCurrencyResolver` reaches a verdict from dated evidence, so the vote is
paid once per ticker rather than on every replay. ``evidence`` records why,
in one short line, because this is a money-affecting inference.
"""

from datetime import datetime, timezone

from app.repositories.db import Connect, session


class TradeCurrencyRepository:
    """Typed access to the ``trade_currency_resolutions`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get_all(self) -> dict[str, str]:
        """Return every stored ``{ticker: trade currency}`` verdict.

        The table holds at most one row per traded ticker, so reading it
        whole is cheaper than a per-ticker round trip during a replay.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT ticker, currency FROM trade_currency_resolutions"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def upsert(self, ticker: str, currency: str, evidence: str) -> None:
        """Persist one ticker's verdict, overwriting any earlier one."""
        with session(self._connect) as conn:
            conn.execute(
                "INSERT INTO trade_currency_resolutions"
                " (ticker, currency, resolved_at, evidence) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET"
                " currency = excluded.currency,"
                " resolved_at = excluded.resolved_at,"
                " evidence = excluded.evidence",
                (
                    ticker,
                    currency,
                    datetime.now(timezone.utc).isoformat(),
                    evidence,
                ),
            )
