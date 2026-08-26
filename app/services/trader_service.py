"""Trader service — the API/orchestration boundary over ``TraderAgent``.

Routes and other services depend on this instead of instantiating
``TraderAgent`` (and its repositories) directly. It is a thin delegation layer:
the trade/portfolio business logic still lives in the agent and repositories,
but the rest of the app talks to persistence only through here.
"""

from typing import Any

from app.agents.trader.trader_agent import TraderAgent
from app.schemas.trade import (
    CashFlow,
    Portfolio,
    Position,
    SippImportResult,
    Trade,
)


class TraderService:
    """Wraps ``TraderAgent`` + repositories behind a stable service API."""

    def __init__(self, agent: TraderAgent | None = None) -> None:
        self._agent = agent or TraderAgent(name="TraderAgent")

    # --- portfolio CRUD (#147) --------------------------------------------

    def list_portfolios(self) -> list[Portfolio]:
        """Return every portfolio with trade/cash-flow counts."""
        return self._agent.list_portfolios()

    def get_portfolio_meta(self, portfolio_id: int) -> Portfolio | None:
        """Return one portfolio's metadata, or None if it doesn't exist."""
        return self._agent.get_portfolio_meta(portfolio_id)

    def portfolio_exists(self, portfolio_id: int) -> bool:
        """Return True if a portfolio with ``portfolio_id`` exists."""
        return self._agent.portfolio_exists(portfolio_id)

    def create_portfolio(self, name: str, opening_cash: float = 0.0) -> Portfolio:
        """Create a portfolio, optionally seeding an opening cash balance."""
        return self._agent.create_portfolio(name, opening_cash)

    def rename_portfolio(self, portfolio_id: int, name: str) -> bool:
        """Rename a portfolio. Returns True if it existed."""
        return self._agent.rename_portfolio(portfolio_id, name)

    def delete_portfolio(self, portfolio_id: int) -> bool:
        """Hard-delete a portfolio and all its data. Returns True if existed."""
        return self._agent.delete_portfolio(portfolio_id)

    def snapshot_history(self, portfolio_id: int, limit: int = 180) -> list[Any]:
        """Return a portfolio's value-history snapshots, oldest-first."""
        return self._agent.snapshot_history(portfolio_id, limit)

    def list_reconciliation_issues(
        self, portfolio_id: int | None, limit: int = 200
    ) -> list[Any]:
        """Return a portfolio's recorded cash-reconciliation issues,
        newest-first (Story 1.5, AC5)."""
        return self._agent.list_reconciliation_issues(portfolio_id, limit)

    def list_cash_balances(self, portfolio_id: int | None) -> list[Any]:
        """Return every ``(currency, amount, as_of)`` balance for a
        portfolio (Story 1.6, Gate 3)."""
        return self._agent.list_cash_balances(portfolio_id)

    # --- reads ------------------------------------------------------------

    def get_portfolio(
        self,
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
        portfolio_id: int | None = None,
    ) -> list[Position]:
        """Return open positions, optionally valued with live prices."""
        return self._agent.get_portfolio(current_prices, display_info, portfolio_id)

    def get_portfolio_from_trades(
        self,
        trades: list[Trade],
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Return positions from an already-read request trade snapshot."""
        return self._agent.get_portfolio_from_trades(
            trades, current_prices, display_info
        )

    def get_trade_history(
        self, ticker: str | None = None, portfolio_id: int | None = None
    ) -> list[Trade]:
        """Return trades, newest first; optionally filter by ticker/portfolio."""
        return self._agent.get_trade_history(ticker, portfolio_id)

    def held_tickers(self) -> set[str]:
        """Return tickers held (net-positive) in any portfolio."""
        return self._agent.held_tickers()

    def get_cash_flows(
        self, portfolio_id: int | None = None, limit: int = 200
    ) -> list[CashFlow]:
        """Return the cash-flow ledger newest-first, scoped to a portfolio."""
        return self._agent.get_cash_flows(portfolio_id, limit)

    def get_cash_balance(self, portfolio_id: int | None = None) -> float | None:
        """Return a portfolio's stored cash balance, or None if unset."""
        return self._agent.get_cash_balance(portfolio_id)

    def load_price_cache(
        self,
    ) -> tuple[dict[str, float], str | None, dict[str, tuple[float, str]]]:
        """Return (prices, fetched_at, display_info) from the price cache."""
        return self._agent.load_price_cache()

    def get_cached_fx_rates(
        self, dates: list[str], pair: str = "GBPUSD=X"
    ) -> dict[str, float]:
        """Return cached ``{date: rate}`` rows for one FX pair."""
        return self._agent.get_cached_fx_rates(dates, pair)

    def get_cached_ticker_currencies(self, tickers: list[str]) -> dict[str, str]:
        """Return persisted ``{ticker: currency}`` rows for tickers with no
        live price-cache entry (delisted/no-longer-scanned)."""
        return self._agent.get_cached_ticker_currencies(tickers)

    # --- writes -----------------------------------------------------------

    def save_price_cache(
        self,
        prices: dict[str, float],
        currencies: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        """Persist fetched prices (and optional display info) to the cache."""
        self._agent.save_price_cache(prices, currencies)

    def save_fx_rates(self, rates: dict[str, float], pair: str = "GBPUSD=X") -> None:
        """Persist resolved ``{date: rate}`` rows for one FX pair."""
        self._agent.save_fx_rates(rates, pair)

    def save_ticker_currencies(self, currencies: dict[str, str]) -> None:
        """Persist resolved ``{ticker: currency}`` rows so a delisted or
        no-longer-scanned ticker's currency lookup survives restarts."""
        self._agent.save_ticker_currencies(currencies)

    def refresh_portfolio_prices(
        self,
        current_prices: dict[str, float],
        display_info: dict[str, tuple[float, str]] | None = None,
        portfolio_id: int | None = None,
    ) -> list[Position]:
        """Recalculate portfolio metrics from supplied live prices."""
        return self._agent.refresh_portfolio_prices(
            current_prices, display_info, portfolio_id
        )

    def update_portfolio_snapshot(
        self, cash_balance: float | None = None, portfolio_id: int | None = None
    ) -> None:
        """Append a portfolio value snapshot to the value log."""
        self._agent.update_portfolio_snapshot(cash_balance, portfolio_id)

    def record_buy(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
        portfolio_id: int | None = None,
        source: str = "manual",
    ) -> Trade:
        """Record a BUY transaction. ``source`` tags its provenance (#Story 2.4)."""
        return self._agent.record_buy(
            ticker,
            shares,
            price,
            date,
            notes,
            stop_loss,
            entry_price,
            portfolio_id,
            source,
        )

    def record_sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        portfolio_id: int | None = None,
        source: str = "manual",
    ) -> Trade:
        """Record a SELL transaction. ``source`` tags its provenance (#Story 2.4)."""
        return self._agent.record_sell(
            ticker, shares, price, date, notes, portfolio_id, source
        )

    def correct_trade(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
        portfolio_id: int | None = None,
    ) -> Trade:
        """Overwrite a ticker's position with a single corrected BUY.

        Always tagged ``source="correction"`` by ``TraderAgent.correct_trade``.
        """
        return self._agent.correct_trade(
            ticker, shares, price, date, notes, stop_loss, entry_price, portfolio_id
        )

    def record_opening_lot(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        portfolio_id: int | None = None,
    ) -> Trade:
        """Record a manual Opening Lot (Story 2.4). Raises
        ``OpeningLotDuplicateError`` on a same ticker/shares/date duplicate."""
        return self._agent.record_opening_lot(
            ticker, shares, price, date, notes, portfolio_id
        )

    def update_opening_lot(
        self,
        trade_id: int,
        ticker: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        portfolio_id: int | None = None,
    ) -> Trade:
        """Edit an existing Opening Lot's fields in place (Story 2.4).

        The caller must have already confirmed the lot is unconsumed
        (AC7/AC8) -- this performs the write unconditionally.
        """
        return self._agent.update_opening_lot(
            trade_id, ticker, shares, price, date, notes, portfolio_id
        )

    def delete_opening_lot(
        self, trade_id: int, portfolio_id: int | None = None
    ) -> bool:
        """Delete an Opening Lot by id (Story 2.4).

        The caller must have already confirmed the lot is unconsumed
        (AC7/AC8) -- this performs the delete unconditionally. Returns
        True if a row was deleted.
        """
        return self._agent.delete_opening_lot(trade_id, portfolio_id)

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a trade by ID. Returns True if a row was deleted."""
        return self._agent.delete_trade(trade_id)

    def set_unmatched_sell_ack(self, trade_id: int, acknowledged: bool) -> None:
        """Set or clear an unmatched sell's acknowledgment (AD-6 passthrough)."""
        self._agent.set_unmatched_sell_ack(trade_id, acknowledged)

    def import_sipp(
        self, csv_content: bytes, portfolio_id: int | None = None
    ) -> SippImportResult:
        """Import a SIPP CSV; return the outcome (cash balance + counts)."""
        return self._agent.import_sipp(csv_content, portfolio_id)
