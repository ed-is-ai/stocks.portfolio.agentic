"""Trader service — the API/orchestration boundary over ``TraderAgent``.

Routes and other services depend on this instead of instantiating
``TraderAgent`` (and its repositories) directly. It is a thin delegation layer:
the trade/portfolio business logic still lives in the agent and repositories,
but the rest of the app talks to persistence only through here.
"""

from pathlib import Path

from app.agents.trader.trader_agent import TraderAgent
from app.schemas.trade import Position, Trade


class TraderService:
    """Wraps ``TraderAgent`` + repositories behind a stable service API."""

    def __init__(self, agent: TraderAgent | None = None) -> None:
        self._agent = agent or TraderAgent(name="TraderAgent")

    # --- reads ------------------------------------------------------------

    def get_portfolio(
        self,
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Return open positions, optionally valued with live prices."""
        return self._agent.get_portfolio(current_prices, display_info)

    def get_trade_history(self, ticker: str | None = None) -> list[Trade]:
        """Return all trades, newest first; optionally filter by ticker."""
        return self._agent.get_trade_history(ticker)

    def get_cash_balance(self) -> float | None:
        """Return the stored SIPP cash balance, or None if not imported."""
        return self._agent.get_cash_balance()

    def load_price_cache(
        self,
    ) -> tuple[dict[str, float], str | None, dict[str, tuple[float, str]]]:
        """Return (prices, fetched_at, display_info) from the price cache."""
        return self._agent.load_price_cache()

    # --- writes -----------------------------------------------------------

    def save_price_cache(
        self,
        prices: dict[str, float],
        currencies: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        """Persist fetched prices (and optional display info) to the cache."""
        self._agent.save_price_cache(prices, currencies)

    def refresh_portfolio_prices(
        self,
        current_prices: dict[str, float],
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Recalculate portfolio metrics from supplied live prices."""
        return self._agent.refresh_portfolio_prices(current_prices, display_info)

    def update_portfolio_snapshot(self, cash_balance: float | None = None) -> None:
        """Append a portfolio value snapshot to the value log."""
        self._agent.update_portfolio_snapshot(cash_balance)

    def record_buy(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
    ) -> Trade:
        """Record a BUY transaction."""
        return self._agent.record_buy(
            ticker, shares, price, date, notes, stop_loss, entry_price
        )

    def record_sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
    ) -> Trade:
        """Record a SELL transaction."""
        return self._agent.record_sell(ticker, shares, price, date, notes)

    def correct_trade(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
    ) -> Trade:
        """Overwrite a ticker's position with a single corrected BUY."""
        return self._agent.correct_trade(
            ticker, shares, price, date, notes, stop_loss, entry_price
        )

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a trade by ID. Returns True if a row was deleted."""
        return self._agent.delete_trade(trade_id)

    def import_sipp(self, csv_path: str | Path) -> float:
        """Import a SIPP CSV; return the resulting cash balance."""
        return self._agent.import_sipp(csv_path)
