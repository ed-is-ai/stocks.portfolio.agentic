"""Portfolio and trading schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

#: The four possible outcomes of one SIPP import row (FR-13/AD-25). Story
#: 1.2 uses this vocabulary to gate the all-or-nothing commit ("did any row
#: resolve to failed?"); as of Story 1.8 the repository insert methods also
#: return ``inserted``/``duplicate`` so the two are no longer conflated.
SippImportRowOutcome = Literal["inserted", "duplicate", "skipped", "failed"]


class SippImportResult(BaseModel):
    """Outcome of a SIPP CSV import, including anything skipped or unparseable.

    ``cash_balance`` is the actually-effective, already-committed portfolio
    balance after stale-date guards have run; it is not necessarily this
    file's closing Running Balance, which may have been rejected as stale.
    The one exception is ``status="rejected"``, where it is ``0.0``: that
    plan committed nothing, so it has no effective balance to report.
    The count/list fields make otherwise-silent problems visible to the
    caller (#152) so the UI can report them instead of a false success.

    ``status`` is one of:

    - ``"ok"``: the plan committed and every row landed in a bucket that
      reconciles cleanly against ``total_rows``.
    - ``"error"``: the pre-existing row-accounting-mismatch safety net
      (#187) — ``buy_count + sell_count + cash_flow_count +
      len(skipped_rows) != total_rows``. Orthogonal to ``"rejected"``.
    - ``"rejected"``: the plan contained at least one ``failed`` row (see
      ``failed_rows``), so — per the all-or-nothing commit rule — nothing
      from this plan was persisted: no trades, cash flows, cash-balance
      update, or portfolio snapshot (#210).

    Every row resolves to exactly one of the four FR-13 outcomes, so
    ``inserted_count + duplicate_count + skipped_count + len(failed_rows)``
    always equals ``total_rows``. For ``status="ok"`` those counts describe
    what was actually persisted; for ``status="rejected"`` they are
    provisional ("what would have happened") and describe no committed
    state at all.
    """

    cash_balance: float
    buy_count: int = 0
    sell_count: int = 0
    cash_flow_count: int = 0
    # Vestigial as of Story 1.2: every row-level problem ``TraderAgent.
    # import_sipp`` used to record here now goes to ``failed_rows`` instead
    # (an all-or-nothing plan has no partial "skip and keep going" outcome).
    # Kept (not removed) because route/UI code and tests still construct/
    # handle it, and a future story (e.g. a genuine reconciliation "skip"
    # outcome) may repopulate it -- see Story 1.2's Dev Notes.
    skipped_rows: list[str] = []
    inserted_count: int = 0
    duplicate_count: int = 0
    skipped_count: int = 0
    parse_errors: list[str] = []
    failed_rows: list[str] = []
    total_rows: int = 0
    status: Literal["ok", "rejected", "error"] = "ok"
    # Statement-balance discontinuities detected against intervening cash
    # movements (Story 1.5, AC5) -- a count only, surfaced immediately
    # without a second round-trip; the detail lives in the dedicated
    # reconciliation view (GET /portfolio/{id}/reconciliation).
    reconciliation_issue_count: int = 0
    # The durable provenance record for this committed import (GH-311) --
    # ``None`` for a rejected plan, which writes no receipt. Lets a future
    # caller (Story 3.3) reference the receipt without a second query.
    receipt_id: int | None = None
    # Story 3.3: the auto-detected contract identity, echoed back so the
    # page can confirm what was actually used -- never user-supplied, always
    # derived from ``ContractRegistry.detect``.
    provider_id: str = ""
    provider_name: str = ""
    contract_version: str = ""
    # The server-validated account type actually used for this import --
    # never the caller's raw, unvalidated form field. Empty string means
    # either the contract declares no account types, or (for a rejected
    # plan reached before validation could run) no value was ever chosen.
    account_type_id: str = ""
    # Story 3.3, AC4: a bounded, redacted preview of parsed rows for a
    # rejected plan only -- empty for every other status.
    preview_rows: list[dict[str, str]] = []


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
    currency: str = "GBP"  # source currency recorded on the SIPP row (#210)


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
    realised_pnl_ack_at: str | None = None  # unmatched-sell ack timestamp (#170)
    currency: str = "GBP"  # source currency recorded on the SIPP row (#210)
    source_row_index: int | None = None  # 0-based position in its source CSV
    idempotency_key: str | None = None  # content-derived dedupe/tiebreak key
    # Provenance (#Story 2.4): which write path produced this row --
    # "sipp_import" | "manual" | "quick_add" | "correction" | "opening_lot".
    # NULL for every row written before this story shipped (never backfilled).
    source: str | None = None
    # One id per ``import_sipp()`` call, stamped on every trade it writes.
    # NULL for every non-SIPP-import write path.
    import_batch_id: str | None = None


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
    # The portfolio's own raw import spelling for this holding (GH-473),
    # carried alongside the canonical ``ticker`` so presenters can show the
    # symbol the user actually imported (e.g. ``HSFWA``) instead of the
    # opaque canonical id it aliases to (``0P00013P6I.L``). Optional and
    # ``None`` by default so every existing construction site keeps
    # ``display_symbol == ticker``; never persisted, always derived on
    # replay. Presentation-only: matching, dedup, and Strategy input keep
    # using ``ticker``.
    display_ticker: str | None = None

    @property
    def display_symbol(self) -> str:
        """Return the user-facing spelling for this holding.

        The single place every presenter (screen, email, recommendation
        rows) reads the display identity from, so an unaliased holding —
        which has no distinct raw spelling — transparently falls back to
        the canonical ``ticker``. A blank or whitespace-only
        ``display_ticker`` falls back the same way, so a malformed import
        row can never render an empty symbol cell. Always read this
        property, never the raw ``display_ticker`` field.
        """
        return (self.display_ticker or "").strip() or self.ticker
