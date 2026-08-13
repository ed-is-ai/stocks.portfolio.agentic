"""Repository for the ``cash_balances`` table in ``trades.db``."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.repositories.db import Connect, session


def _row_to_balance(row: tuple[Any, ...]) -> tuple[Decimal, str | None]:
    return Decimal(row[0]), row[1]


class CashBalancesRepository:
    """Typed access to the ``cash_balances`` table (portfolio_id, currency)."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get(
        self, portfolio_id: int | None, currency: str
    ) -> tuple[Decimal, str | None] | None:
        """Return ``(amount, as_of)`` for a portfolio's currency balance, or
        None if no balance has been recorded for it yet."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT amount, as_of FROM cash_balances"
                " WHERE ifnull(portfolio_id, -1) = ? AND currency = ?",
                (-1 if portfolio_id is None else portfolio_id, currency),
            ).fetchone()
        return _row_to_balance(row) if row else None

    def list_all(
        self, portfolio_id: int | None
    ) -> list[tuple[str, Decimal, str | None]]:
        """Return every ``(currency, amount, as_of)`` balance for a
        portfolio, ordered by currency. ``portfolio_id=None`` returns no
        rows -- enumeration is only meaningful for a real portfolio,
        matching ``CashReconciliationRepository.list_issues``'s convention.
        """
        if portfolio_id is None:
            return []
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT currency, amount, as_of FROM cash_balances"
                " WHERE portfolio_id = ? ORDER BY currency",
                (portfolio_id,),
            ).fetchall()
        return [(row[0], Decimal(row[1]), row[2]) for row in rows]

    def get_on_connection(
        self, conn: Any, portfolio_id: int | None, currency: str
    ) -> tuple[Decimal, str | None] | None:
        """Return ``(amount, as_of)`` on the caller's open connection.

        Takes the caller's open connection and does not commit — lets the
        SIPP import's stale-date guard read this transaction's own
        not-yet-committed writes, joining the same transaction as its
        trade/cash-flow inserts.
        """
        row = conn.execute(
            "SELECT amount, as_of FROM cash_balances"
            " WHERE ifnull(portfolio_id, -1) = ? AND currency = ?",
            (-1 if portfolio_id is None else portfolio_id, currency),
        ).fetchone()
        return _row_to_balance(row) if row else None

    def upsert_on_connection(
        self,
        conn: Any,
        portfolio_id: int | None,
        currency: str,
        amount: Decimal,
        as_of: str | None,
    ) -> None:
        """Upsert a portfolio's currency balance on the caller's open
        connection; does not commit.

        A genuine upsert (not ``INSERT OR IGNORE``) — a later, newer-dated
        balance for the same ``(portfolio_id, currency)`` replaces the
        stored one. Mirrors ``insert_ignore``'s batch-friendly convention so
        the caller (``TraderAgent.import_sipp``) controls the transaction
        boundary and this write joins its one-transaction commit.
        """
        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        conn.execute(
            "INSERT INTO cash_balances (portfolio_id, currency, amount, as_of, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, currency) DO UPDATE SET "
            " amount=excluded.amount, as_of=excluded.as_of, updated_at=excluded.updated_at",
            (portfolio_id, currency, str(amount), as_of, updated_at),
        )
