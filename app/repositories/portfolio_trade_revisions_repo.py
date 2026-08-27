"""Durable per-portfolio revisions for derived trade-ledger views."""

from collections.abc import Iterable

from app.repositories.db import Connect, session


class PortfolioTradeRevisionsRepository:
    """Read the durable revisions for derived realised-P&L views."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get(self, portfolio_id: int) -> int:
        """Return a portfolio's revision, using zero as the legacy baseline."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT revision FROM portfolio_trade_revisions WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def get_many(self, portfolio_ids: Iterable[int]) -> dict[int, int]:
        """Return revisions for all requested IDs, including zero baselines."""
        ids = list(dict.fromkeys(portfolio_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT portfolio_id, revision FROM portfolio_trade_revisions "
                f"WHERE portfolio_id IN ({placeholders})",
                ids,
            ).fetchall()
        revisions = {int(portfolio_id): int(revision) for portfolio_id, revision in rows}
        return {portfolio_id: revisions.get(portfolio_id, 0) for portfolio_id in ids}

    def get_pnl_input_revision(self) -> int:
        """Return the shared FX/currency revision, with zero as baseline."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT revision FROM realised_pnl_input_revision WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row is not None else 0
