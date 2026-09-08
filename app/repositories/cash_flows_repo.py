"""Repository for the ``cash_flows`` table in ``trades.db``."""

from typing import Any

from app.repositories.db import Connect, session
from app.schemas.trade import CashFlow, SippImportRowOutcome


def _row_to_cash_flow(row: tuple[Any, ...]) -> CashFlow:
    return CashFlow(
        id=row[0],
        date=row[1],
        flow_type=row[2],
        ticker=row[3],
        amount=row[4],
        description=row[5],
        reference=row[6],
        portfolio_id=row[7],
        currency=row[8] if len(row) > 8 and row[8] is not None else "GBP",
    )


class CashFlowsRepository:
    """Typed access to the ``cash_flows`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def history(
        self, portfolio_id: int | None = None, limit: int = 200
    ) -> list[CashFlow]:
        """Return cash flows newest-first, optionally scoped to a portfolio."""
        base = (
            "SELECT id, date, flow_type, ticker, amount, description, reference,"
            " portfolio_id, currency FROM cash_flows"
        )
        params: tuple[Any, ...] = ()
        if portfolio_id is not None:
            base += " WHERE portfolio_id = ?"
            params = (portfolio_id,)
        base += " ORDER BY date DESC, id DESC LIMIT ?"
        params = (*params, limit)
        with session(self._connect) as conn:
            rows = conn.execute(base, params).fetchall()
        return [_row_to_cash_flow(r) for r in rows]

    def dated_flows(self, portfolio_id: int) -> list[tuple[str, str, float, str]]:
        """Return every ``(date, flow_type, amount, currency)`` oldest-first (#543).

        Unlike :meth:`history` this is neither limited nor newest-first and
        returns plain tuples rather than models: the cash reconstruction
        loads a portfolio's whole flow ledger once and then answers a
        balance for every backfilled day from it, so a 200-row cap or a
        per-day query would both be wrong.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT date, flow_type, amount, currency FROM cash_flows "
                "WHERE portfolio_id = ? ORDER BY date ASC, id ASC",
                (portfolio_id,),
            ).fetchall()
        return [
            (str(row[0]), str(row[1]), float(row[2]), str(row[3] or "GBP"))
            for row in rows
        ]

    def insert_ignore(
        self,
        conn: Any,
        date: str,
        flow_type: str,
        ticker: str | None,
        amount: float,
        description: str | None,
        reference: str | None,
        portfolio_id: int | None = None,
        idempotency_key: str | None = None,
        currency: str = "GBP",
    ) -> SippImportRowOutcome:
        """Insert a cash flow with ``INSERT OR IGNORE`` on the given connection.

        The ``(portfolio_id, idempotency_key)`` pair makes the SIPP import
        idempotent per-portfolio. Inserts share the import's connection so the
        whole import is one transaction.

        Returns the row's actual outcome: ``"inserted"`` when the row was
        written, ``"duplicate"`` when the unique index silently suppressed
        it. Never returns ``"skipped"``/``"failed"`` — those are decided at
        plan-build time and such a row never reaches this call.
        """
        cur = conn.execute(
            "INSERT OR IGNORE INTO cash_flows "
            "(date, flow_type, ticker, amount, description, reference, portfolio_id, idempotency_key, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date,
                flow_type,
                ticker,
                amount,
                description,
                reference,
                portfolio_id,
                idempotency_key,
                currency,
            ),
        )
        return "inserted" if cur.rowcount else "duplicate"

    def idempotency_keys_for_portfolio(self, portfolio_id: int | None) -> set[str]:
        """Return every cash-flow idempotency key already stored for a portfolio.

        Read-only: lets the SIPP import classify a row as inserted or
        duplicate before deciding whether to write anything at all.
        """
        bucket = -1 if portfolio_id is None else portfolio_id
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT idempotency_key FROM cash_flows "
                "WHERE ifnull(portfolio_id, -1) = ? AND idempotency_key IS NOT NULL",
                (bucket,),
            ).fetchall()
        return {str(row[0]) for row in rows}
