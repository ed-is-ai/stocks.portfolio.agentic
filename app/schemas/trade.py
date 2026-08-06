"""Portfolio and trading schemas."""

from __future__ import annotations

from pydantic import BaseModel


class SippImportResult(BaseModel):
    """Outcome of a SIPP CSV import, including anything skipped or unparseable.

    ``cash_balance`` is the authoritative closing balance (final Running
    Balance). The count/list fields make otherwise-silent problems visible to
    the caller (#152) so the UI can report them instead of a false success.
    """

    cash_balance: float
    buy_count: int = 0
    sell_count: int = 0
    cash_flow_count: int = 0
    skipped_rows: list[str] = []
    parse_errors: list[str] = []


class Portfolio(BaseModel):
    """A named account (e.g. SIPP, ISA) that scopes its own holdings, cash,
    and value history (#147). Names may duplicate; ``id`` is the identity."""

    id: int
    name: str
    created_at: str
    trade_count: int | None = None
    cash_flow_count: int | None = None


class CashFlow(BaseModel):
    """A non-trade cash movement recorded in the ledger (#161).

    Dividends, contributions, interest, tax relief, transfers, withdrawals, and
    the opening balance. Stored per-portfolio; surfaced as a read-only activity
    view (the balance itself still comes from the provider Running Balance).
    """

    id: int | None = None
    date: str
    flow_type: str
    ticker: str | None = None
    amount: float
    description: str | None = None
    reference: str | None = None
    portfolio_id: int | None = None


class ExitSignal(BaseModel):
    """Exit or add signal for a held position."""

    action: str  # "EXIT" | "ADD"
    reason: str


class EmailConfig(BaseModel):
    host: str
    port: int
    user: str
    password: str
    recipient: str


class Trade(BaseModel):
    """A single recorded buy or sell transaction."""

    id: int | None = None
    ticker: str
    action: str  # "BUY" or "SELL"
    shares: float
    price: float
    date: str
    notes: str = ""
    stop_loss: float | None = None  # initial stop from analysis at time of entry
    entry_price: float | None = None  # analyst pivot entry price
    portfolio_id: int | None = None  # owning account (#147)


class Position(BaseModel):
    """An open portfolio position with live P&L and Minervini exit metrics."""

    ticker: str
    shares: float
    avg_cost: float
    current_price: float | None = None
    total_cost: float
    current_value: float | None = None
    unrealised_pnl: float | None = None
    unrealised_pnl_pct: float | None = None
    entry_price: float | None = None  # pivot entry from first BUY
    entry_date: str | None = None  # date of first BUY
    stop_loss: float | None = None  # initial stop from first BUY
    profit_target_20: float | None = None  # entry_price * 1.20
    profit_target_25: float | None = None  # entry_price * 1.25
    next_pivot: float | None = (
        None  # current analyst pivot entry (populated in web layer)
    )
    exit_signal: ExitSignal | None = None  # populated by ExitEvaluator in web layer
    price_currency: str = "GBP"  # ISO currency code for current_price/current_value/pnl
