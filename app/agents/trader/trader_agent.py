"""
Trader Agent — records buy/sell trades to SQLite and computes portfolio P&L.

Standalone agent; not part of the main scan pipeline.
Run as part of the web UI backend.
"""

import csv
import hashlib
import io
import logging
import re
import uuid
from datetime import date as _date
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import PrivateAttr

from app.agents.base import Agent
from app.core.config import ANALYSIS_JSON, PORTFOLIO_VALUE_CSV, TRADES_DB
from app.core.money import (
    Money,
    MoneyParseError,
    establish_row_decimal_separator,
    has_currency_marker,
    parse_money,
)
from app.core.quantity import QUANTITY_EPSILON, round_quantity
from app.core.ticker_identity import (
    AmbiguousTickerAliasError,
    canonical_ticker,
    canonicalize_or_fallback,
    load_aliases,
)
from app.schemas import CashFlow, Position, SippImportResult, Trade
from app.repositories import db
from app.repositories.account_repo import AccountStateRepository
from app.repositories.artifacts_repo import ArtifactsRepository
from app.repositories.cash_balances_repo import CashBalancesRepository
from app.repositories.cash_reconciliation_repo import CashReconciliationRepository
from app.repositories.cash_flows_repo import CashFlowsRepository
from app.repositories.db import Connect
from app.repositories.fx_rate_cache_repo import FxRateCacheRepository
from app.repositories.import_receipt_repo import (
    ImportReceiptRepository,
    ImportRowLineage,
    canonical_row_digest,
)
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.portfolios_repo import PortfoliosRepository
from app.repositories.price_cache_repo import PriceCacheRepository
from app.repositories.ticker_currency_cache_repo import TickerCurrencyCacheRepository
from app.repositories.trades_repo import TradesRepository
from app.schemas.trade import Portfolio
from app.services.portfolio_import.contract_registry import ContractRegistryError
from app.services.portfolio_import.contract_schema import (
    CanonicalField,
    contract_content_digest,
)
from app.services.portfolio_import.normalizer import (
    REJECTED_REASON_KEY,
    ContractNormalizer,
)
from app.services.portfolio_import.registry_loader import get_contract_registry

logger = logging.getLogger(__name__)

_DB_PATH = TRADES_DB

# Matches a leading run of BOM noise on a CSV header cell: either the real
# BOM character (U+FEFF, left behind when a provider export stacks more than
# one and ``utf-8-sig`` only strips the first) or the 3-character mojibake
# "ï»¿" produced when a BOM gets decoded as Latin-1 and re-saved as UTF-8
# (#171).
_HEADER_BOM_NOISE_RE = re.compile(r"^(?:﻿|ï»¿)+")


class SippImportError(ValueError):
    """Raised when a SIPP CSV cannot be imported (e.g. missing columns)."""


class OpeningLotDuplicateError(ValueError):
    """Raised when an Opening Lot entry duplicates an existing one (Story
    2.4, AC4) -- same canonicalized ticker, ``shares``, and ``date`` already
    recorded with ``source="opening_lot"`` for this portfolio."""


def _to_iso_date(value: str) -> str:
    """Return ``value`` as ISO ``YYYY-MM-DD``.

    Accepts the SIPP CSV's ``DD/MM/YYYY``, already-ISO ``YYYY-MM-DD``, and
    (Story 3.4, for IG's export) 2-digit-year ``DD/MM/YY`` -- tried last,
    after both 4-digit formats, so a genuinely 4-digit-year value is never
    misread as a 2-digit one. BOM characters (which some provider exports
    embed even mid-value, e.g. ``12/10/2﻿﻿020``) are stripped first
    so a polluted date still parses (#166).
    """
    value = value.replace("﻿", "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.warning("unrecognized trade date format: %r (stored unchanged)", value)
    return value


def _idempotency_key(
    date: str, symbol: str, sedol: str, quantity: str, description: str
) -> str:
    """Return the deterministic dedupe key for one SIPP row.

    Derived from the row's own content rather than its provider Reference,
    which real exports commonly leave blank or ``"n/a"`` on non-trade rows.
    The same row re-uploaded in an overlapping CSV therefore produces the
    same key and is suppressed by the ``(portfolio_id, idempotency_key)``
    unique index.
    """
    raw = "|".join((date, symbol, sedol, quantity, description))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


#: Story 3.3, AC4: a rejected plan's preview table is capped at this many
#: rows, and each cell is length-capped below -- real user financial data,
#: so redaction is intentionally conservative rather than configurable.
_PREVIEW_ROW_CAP = 20
_PREVIEW_FREE_TEXT_MAX_CHARS = 40
_PREVIEW_CELL_MAX_CHARS = 60
#: Free-text fields are truncated harder than numeric/date fields -- they're
#: the most likely to carry incidentally-sensitive prose.
_PREVIEW_FREE_TEXT_FIELDS = frozenset(
    {CanonicalField.DESCRIPTION.value, CanonicalField.REFERENCE.value}
)


def _truncate_preview_cell(value: str, field: str) -> str:
    """Truncate one preview cell, harder for free-text fields than others."""
    limit = (
        _PREVIEW_FREE_TEXT_MAX_CHARS
        if field in _PREVIEW_FREE_TEXT_FIELDS
        else _PREVIEW_CELL_MAX_CHARS
    )
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _build_preview_rows(
    normalized_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build a bounded, redacted preview of parsed rows for a rejected plan.

    Capped at the first ``_PREVIEW_ROW_CAP`` rows. Every emitted row shares
    exactly one fixed, ordered key list -- every value of ``CanonicalField``,
    in enum declaration order -- with a missing value rendered as ``""``, so
    the rendered preview table's column headers can never misalign against a
    row that happened to parse a different subset of fields than another
    (Story 3.3).
    """
    keys = [field.value for field in CanonicalField]
    return [
        {key: _truncate_preview_cell(row.get(key, ""), key) for key in keys}
        for row in normalized_rows[:_PREVIEW_ROW_CAP]
    ]


def _detect_reconciliation_issues(
    rows: list[dict[str, str]],
) -> list[tuple[str, str, float, float, float, float, str]]:
    """Detect statement-balance discontinuities, independently per currency.

    Returns tuples of ``(currency, date, prior_balance, expected_balance,
    actual_balance, difference, row_ref)`` -- a pure function with no DB
    side effects; the caller persists whatever it returns.

    Walks ``rows`` in true chronological order (the SIPP export is
    reverse-chronological -- newest first -- so oldest-to-newest traversal
    is last-row-to-first-row; see Gate 3's identical assumption). For each
    currency independently, tracks a running checkpoint balance (the last
    *observed* Running Balance) and the cumulative credit-minus-debit
    movement since that checkpoint -- a EUR checkpoint must never
    reconcile against USD/GBP movements now that Story 1.4 makes balances
    currency-scoped. Uses its own local ``Money`` parsing, entirely
    independent of the main row loop's ``parse_errors``/``failed_rows`` --
    a cell already flagged there must not be flagged here a second time.
    A row already rejected by the debit+credit contradiction check (AC4)
    contributes zero net movement here -- its true movement is unknown,
    not zero; a deliberate, documented limitation, not a bug.
    """

    def parse(raw: str) -> Money | None:
        if not raw or raw.strip() in ("", "n/a"):
            return None
        try:
            return parse_money(raw, default_currency="GBP")
        except MoneyParseError:
            return None

    issues: list[tuple[str, str, float, float, float, float, str]] = []
    checkpoint: dict[str, Decimal] = {}
    movement: dict[str, Decimal] = {}

    for idx in range(len(rows) - 1, -1, -1):
        row = rows[idx]
        reference_raw = row.get("Reference", "").strip()
        reference = (
            reference_raw if reference_raw and reference_raw.lower() != "n/a" else None
        )
        row_label = reference if reference else f"CSV row {idx + 2}"
        date = _to_iso_date(row.get("Date", "").strip())

        debit_raw = row.get("Debit", "").strip()
        credit_raw = row.get("Credit", "").strip()
        contradictory = debit_raw not in ("", "n/a") and credit_raw not in ("", "n/a")

        debit_money = None if contradictory else parse(debit_raw)
        credit_money = None if contradictory else parse(credit_raw)
        rb_money = parse(row.get("Running Balance", ""))

        move_source = credit_money or debit_money
        if move_source is not None:
            move_currency = move_source.currency
            delta = (credit_money.amount if credit_money else Decimal("0")) - (
                debit_money.amount if debit_money else Decimal("0")
            )
            movement[move_currency] = movement.get(move_currency, Decimal("0")) + delta

        if rb_money is not None:
            currency = rb_money.currency
            actual = rb_money.amount
            prior = checkpoint.get(currency)
            if prior is not None:
                expected = prior + movement.get(currency, Decimal("0"))
                if abs(actual - expected) > Decimal("0.005"):
                    issues.append(
                        (
                            currency,
                            date,
                            float(prior),
                            float(expected),
                            float(actual),
                            float(actual - expected),
                            row_label,
                        )
                    )
            checkpoint[currency] = actual
            movement[currency] = Decimal("0")

    return issues


class TraderAgent(Agent):
    """Records trades and computes portfolio P&L using average cost basis.

    Supports importing SIPP transactions from CSV, separating stock trades
    from non-trade cash flows (contributions, dividends, tax relief, interest).
    Maintains separate tables: trades (stock buy/sell) and cash_flows (non-trades).
    """

    name: str = "TraderAgent"
    db_path: Path = _DB_PATH

    _trades: TradesRepository = PrivateAttr()
    _cash_flows: CashFlowsRepository = PrivateAttr()
    _cash_balances: CashBalancesRepository = PrivateAttr()
    _cash_reconciliation: CashReconciliationRepository = PrivateAttr()
    _import_receipts: ImportReceiptRepository = PrivateAttr()
    _price_cache: PriceCacheRepository = PrivateAttr()
    _account: AccountStateRepository = PrivateAttr()
    _artifacts: ArtifactsRepository = PrivateAttr()
    _portfolios: PortfoliosRepository = PrivateAttr()
    _snapshots: PortfolioSnapshotsRepository = PrivateAttr()
    _fx_rates: FxRateCacheRepository = PrivateAttr()
    _ticker_currency_cache: TickerCurrencyCacheRepository = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        connect: Connect = db.make_connect(lambda: self.db_path)
        self._trades = TradesRepository(connect)
        self._cash_flows = CashFlowsRepository(connect)
        self._cash_balances = CashBalancesRepository(connect)
        self._cash_reconciliation = CashReconciliationRepository(connect)
        self._import_receipts = ImportReceiptRepository(connect)
        self._price_cache = PriceCacheRepository(connect)
        self._account = AccountStateRepository(connect)
        self._artifacts = ArtifactsRepository()
        self._portfolios = PortfoliosRepository(connect)
        self._snapshots = PortfolioSnapshotsRepository(connect)
        self._fx_rates = FxRateCacheRepository(connect)
        self._ticker_currency_cache = TickerCurrencyCacheRepository(connect)
        self._init_db()

    # --- portfolio CRUD (#147) -------------------------------------------

    @staticmethod
    def _cash_key(portfolio_id: int | None) -> str:
        """Return the account-state key holding a portfolio's cash balance.

        ``None`` maps to the legacy single-portfolio ``cash_balance`` key so
        pre-multi-portfolio code paths and tests keep working unchanged.
        """
        return (
            f"cash_balance:{portfolio_id}"
            if portfolio_id is not None
            else ("cash_balance")
        )

    def list_portfolios(self) -> list[Portfolio]:
        """Return every portfolio with its trade/cash-flow counts."""
        result: list[Portfolio] = []
        for pf in self._portfolios.list_all():
            trades, flows = self._portfolios.counts(pf.id)
            result.append(
                pf.model_copy(
                    update={
                        "trade_count": trades,
                        "cash_flow_count": flows,
                    }
                )
            )
        return result

    def get_portfolio_meta(self, portfolio_id: int) -> Portfolio | None:
        """Return one portfolio's metadata (with counts), or None."""
        pf = self._portfolios.get(portfolio_id)
        if pf is None:
            return None
        trades, flows = self._portfolios.counts(pf.id)
        return pf.model_copy(
            update={
                "trade_count": trades,
                "cash_flow_count": flows,
            }
        )

    def portfolio_exists(self, portfolio_id: int) -> bool:
        """Return True if a portfolio with ``portfolio_id`` exists."""
        return self._portfolios.exists(portfolio_id)

    def create_portfolio(self, name: str, opening_cash: float = 0.0) -> Portfolio:
        """Create a portfolio, optionally seeding an opening cash balance.

        Opening cash is stored as the portfolio's balance and recorded as an
        auditable ``OPENING`` cash-flow row so it shows in trade history.
        """
        pf = self._portfolios.create(name.strip() or "Untitled")
        if opening_cash and opening_cash > 0:
            with self._conn() as conn:
                self._cash_flows.insert_ignore(
                    conn,
                    datetime.today().strftime("%Y-%m-%d"),
                    "OPENING",
                    None,
                    opening_cash,
                    "Opening balance",
                    f"opening:{pf.id}",
                    pf.id,
                )
                conn.commit()
            self.set_cash_balance(opening_cash, pf.id)
        return self.get_portfolio_meta(pf.id) or pf

    def set_unmatched_sell_ack(self, trade_id: int, acknowledged: bool) -> None:
        """Set or clear an unmatched sell's realised-P&L acknowledgment (AD-6).

        The sole writer of ``trades.realised_pnl_ack_at``. Called only via
        ``TraderService``'s passthrough, whose only caller is
        ``RealisedPnlService`` (AD-6).
        """
        from datetime import timezone

        acknowledged_at = (
            datetime.now(timezone.utc).isoformat() if acknowledged else None
        )
        with self._conn() as conn:
            self._trades.set_ack(conn, trade_id, acknowledged_at)
            conn.commit()

    def rename_portfolio(self, portfolio_id: int, name: str) -> bool:
        """Rename a portfolio. Returns True if it existed."""
        return self._portfolios.rename(portfolio_id, name.strip() or "Untitled")

    def delete_portfolio(self, portfolio_id: int) -> bool:
        """Hard-delete a portfolio and all its data. Returns True if it
        existed."""
        return self._portfolios.delete(portfolio_id)

    def _conn(self):
        """Open a connection to the configured database (for batch imports)."""
        return db.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            db.init_trades_db(conn)
        # Seed cash_balance from portfolio_value.csv if not yet stored
        if not self._account.exists("cash_balance"):
            self._seed_cash_from_csv()

    def _seed_cash_from_csv(self) -> None:
        """Seed cash_balance from portfolio_value.csv on first startup."""
        try:
            rows = self._artifacts.read_csv_dicts(PORTFOLIO_VALUE_CSV)
            if rows and rows[-1].get("cash_balance"):
                amount = float(rows[-1]["cash_balance"])
                if amount > 0:
                    self._account.set("cash_balance", str(amount))
        except (OSError, ValueError, KeyError, csv.Error) as exc:
            logger.debug("cash seed from csv skipped: %s", exc)

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
        """Record a buy transaction and return the saved Trade.

        ``source`` tags this row's provenance (Story 2.4) -- defaults to
        ``"manual"`` (the generic ``/trades`` BUY form); callers like
        Quick-Add and the Opening Lot entry path pass their own value.
        """
        trade_date = (
            _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
        )
        trade_id = self._trades.insert(
            ticker.upper(),
            "BUY",
            shares,
            price,
            trade_date,
            notes,
            stop_loss,
            entry_price,
            portfolio_id,
            source=source,
        )
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            stop_loss=stop_loss,
            entry_price=entry_price,
            portfolio_id=portfolio_id,
            source=source,
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
        """Record a sell transaction and return the saved Trade.

        ``source`` tags this row's provenance (Story 2.4) -- defaults to
        ``"manual"`` (the generic ``/trades`` SELL form).
        """
        trade_date = (
            _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
        )
        trade_id = self._trades.insert(
            ticker.upper(),
            "SELL",
            shares,
            price,
            trade_date,
            notes,
            portfolio_id=portfolio_id,
            source=source,
        )
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="SELL",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            portfolio_id=portfolio_id,
            source=source,
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
        """Overwrite the position for a ticker: delete all trades and insert one BUY.

        Scoped to ``portfolio_id`` so correcting a ticker in one account never
        touches the same ticker held in another. Always tags the written row
        ``source="correction"`` (Story 2.4), regardless of what the
        replaced trades' provenance was.
        """
        trade_date = (
            _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
        )
        self._trades.delete_by_ticker(ticker.upper(), portfolio_id)
        trade_id = self._trades.insert(
            ticker.upper(),
            "BUY",
            shares,
            price,
            trade_date,
            notes,
            stop_loss,
            entry_price,
            portfolio_id,
            source="correction",
        )
        return Trade(
            id=trade_id,
            ticker=ticker.upper(),
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            stop_loss=stop_loss,
            entry_price=entry_price,
            portfolio_id=portfolio_id,
            source="correction",
        )

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a trade by ID. Returns True if a row was deleted."""
        return self._trades.delete_by_id(trade_id)

    def _opening_lot_duplicate_check(
        self,
        canonical_ticker_value: str,
        shares: float,
        trade_date: str,
        portfolio_id: int | None,
        exclude_trade_id: int | None = None,
    ) -> None:
        """Raise ``OpeningLotDuplicateError`` if an Opening Lot with the same
        canonicalized ticker, ``shares``, and ``date`` already exists for
        this portfolio (Story 2.4, AC4).

        Shared by ``record_opening_lot`` and ``update_opening_lot``;
        ``exclude_trade_id`` lets an edit collide-check against every
        *other* Opening Lot without always rejecting against itself.
        Distinct from SIPP import's idempotency-key dedup, which has no
        equivalent content to key on for a manual entry.
        """
        for existing in self._trades.history(canonical_ticker_value, portfolio_id):
            if (
                existing.id != exclude_trade_id
                and existing.source == "opening_lot"
                and existing.action == "BUY"
                and existing.date == trade_date
                and abs(existing.shares - shares) <= QUANTITY_EPSILON
            ):
                raise OpeningLotDuplicateError(
                    f"An Opening Lot for {canonical_ticker_value} on "
                    f"{trade_date} with {shares:g} shares already exists."
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
        """Record a manual Opening Lot (Story 2.4).

        A BUY trade tagged ``source="opening_lot"`` -- reuses the exact
        insert path an imported Buy uses, so it participates in FIFO and
        average-cost replay identically (AC3), including in the Match
        Trace. Rejects (``OpeningLotDuplicateError``) a duplicate entry --
        same canonicalized ticker, ``shares``, and ``date`` already
        recorded as an Opening Lot for this portfolio (AC4) -- before any
        write.
        """
        canonical = canonicalize_or_fallback(
            ticker.upper(), load_aliases(), logger=logger, context="record_opening_lot"
        )
        trade_date = (
            _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
        )
        self._opening_lot_duplicate_check(canonical, shares, trade_date, portfolio_id)
        trade_id = self._trades.insert(
            canonical,
            "BUY",
            shares,
            price,
            trade_date,
            notes,
            portfolio_id=portfolio_id,
            source="opening_lot",
        )
        return Trade(
            id=trade_id,
            ticker=canonical,
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            portfolio_id=portfolio_id,
            source="opening_lot",
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

        Re-runs the same duplicate check as ``record_opening_lot``,
        excluding the row being edited itself, so an edit that collides
        with a *different* Opening Lot is still rejected (AC4). The
        caller (the route) is responsible for the consumed/unconsumed gate
        (AC7/AC8, via ``RealisedPnlService.opening_lot_status``) before
        calling this -- this performs the write unconditionally once
        called. Raises ``ValueError`` if no Opening Lot with ``trade_id``
        exists for this portfolio.
        """
        canonical = canonicalize_or_fallback(
            ticker.upper(), load_aliases(), logger=logger, context="update_opening_lot"
        )
        trade_date = (
            _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
        )
        self._opening_lot_duplicate_check(
            canonical, shares, trade_date, portfolio_id, exclude_trade_id=trade_id
        )
        updated = self._trades.update_opening_lot(
            trade_id, canonical, shares, price, trade_date, notes, portfolio_id
        )
        if not updated:
            raise ValueError(
                f"No Opening Lot with id={trade_id} exists for this portfolio."
            )
        return Trade(
            id=trade_id,
            ticker=canonical,
            action="BUY",
            shares=shares,
            price=price,
            date=trade_date,
            notes=notes,
            portfolio_id=portfolio_id,
            source="opening_lot",
        )

    def delete_opening_lot(
        self, trade_id: int, portfolio_id: int | None = None
    ) -> bool:
        """Delete an Opening Lot by id, scoped to ``portfolio_id``.

        Only deletes a row that is in fact ``source="opening_lot"``
        (defense in depth so this Opening-Lot-specific path can never
        delete an unrelated trade). The caller (the route) is responsible
        for the consumed/unconsumed gate (AC7/AC8) before calling this.
        Returns True if a row was deleted.
        """
        history = self._trades.history(portfolio_id=portfolio_id)
        target = next(
            (t for t in history if t.id == trade_id and t.source == "opening_lot"),
            None,
        )
        if target is None:
            return False
        return self._trades.delete_by_id(trade_id)

    def get_trade_history(
        self, ticker: str | None = None, portfolio_id: int | None = None
    ) -> list[Trade]:
        """Return trades newest first. Optionally filter by ticker/portfolio."""
        return self._trades.history(ticker.upper() if ticker else None, portfolio_id)

    def held_tickers(self) -> set[str]:
        """Return tickers held (net-positive) in any portfolio (#147)."""
        return self._trades.held_tickers()

    def get_cash_flows(
        self, portfolio_id: int | None = None, limit: int = 200
    ) -> list[CashFlow]:
        """Return the cash-flow ledger newest-first, scoped to a portfolio."""
        return self._cash_flows.history(portfolio_id, limit)

    def get_latest_trade(self, ticker: str) -> Trade | None:
        """Return the most recent trade for a ticker, or None if none exist."""
        history = self.get_trade_history(ticker)
        return history[0] if history else None

    def get_portfolio(
        self,
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
        portfolio_id: int | None = None,
    ) -> list[Position]:
        """Compute open positions using average cost basis.

        Replays trades chronologically to derive shares and avg cost per ticker.
        Unrealised P&L requires current_prices dict; omit for cost-only view.
        Only includes valid-ticker trades (excludes Symbol='n/a'). Scoped to
        ``portfolio_id`` when given (``None`` aggregates every portfolio).
        display_info: optional {ticker: (original_price, currency)} for template display.
        """
        rows = self._trades.open_rows(portfolio_id)
        return self._compute_positions(rows, current_prices, display_info)

    def get_portfolio_from_trades(
        self,
        trades: list[Trade],
        current_prices: dict[str, float] | None = None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> list[Position]:
        """Compute positions from one already-read trade snapshot.

        ``get_trade_history()`` is intentionally presentation-ordered, while
        position replay must retain ``TradesRepository.open_rows()`` ordering.
        Reconstruct that ordered, valid-ticker projection here so a request
        can share one trade read between holdings, chart markers, and opening
        lot indicators without changing average-cost semantics.
        """
        ordered = sorted(
            (trade for trade in trades if trade.ticker not in {"", "n/a", "N/A"}),
            key=lambda trade: (
                trade.date,
                -(trade.source_row_index if trade.source_row_index is not None else -1),
                trade.idempotency_key is not None,
                trade.idempotency_key or "",
                trade.id is not None,
                trade.id or 0,
            ),
        )
        rows = [
            (
                trade.ticker,
                trade.action,
                trade.shares,
                trade.price,
                trade.date,
                trade.stop_loss,
                trade.entry_price,
            )
            for trade in ordered
        ]
        return self._compute_positions(rows, current_prices, display_info)

    @staticmethod
    def _compute_positions(
        rows: list[tuple[Any, ...]],
        current_prices: dict[str, float] | None,
        display_info: dict[str, tuple[float, str]] | None,
    ) -> list[Position]:
        """Replay trade rows into open Positions with live P&L.

        Shared by ``get_portfolio`` (reading committed rows via
        ``open_rows``) and ``import_sipp``'s in-transaction snapshot calc
        (reading via ``open_rows_on_connection``) so both use one
        implementation.

        A fully-closed position (``shares`` within ``QUANTITY_EPSILON`` of
        0) is excluded -- there is nothing open to show, and a bare
        ``!= 0`` would let residual float dust from a long replay surface
        as a phantom near-zero row. A genuinely negative ``shares`` (an
        oversell whose shortfall was never clamped away, Story 2.3) is
        deliberately included: it must stay visible and trackable on the
        Portfolio tab, not vanish the way a `> 0` filter would make it.
        """
        state = TraderAgent._replay_trades(rows)
        return [
            TraderAgent._build_position(ticker, s, current_prices, display_info)
            for ticker, s in state.items()
            if abs(s["shares"]) > QUANTITY_EPSILON
        ]

    @staticmethod
    def _replay_trades(
        rows: list[tuple[Any, ...]],
    ) -> dict[str, dict[str, Any]]:
        """Replay trade rows to derive per-ticker running state.

        Each row's ticker is canonicalized via ``canonicalize_or_fallback``
        before being used as the ``state`` dict key, so
        cross-spelling trades for one security (e.g. ``ABC.L`` and ``ABC``
        under a configured alias) fold into one running position instead of
        fragmenting -- agreeing with FIFO replay's and ``held_tickers()``'s
        identity. A cycle or malformed alias file degrades to the raw
        ticker with a logged warning rather than crashing this replay.

        A row with an unparseable (non-ISO) ``date`` is skipped (logged,
        not raised) -- Story 2.2, mirroring ``RealisedPnlService.
        _sorted_valid_trades``'s existing FIFO skip so average-cost replay
        never silently includes a row FIFO excludes.
        """
        aliases = load_aliases()
        state: dict[str, dict[str, Any]] = {}
        for (
            raw_ticker,
            action,
            shares,
            price,
            trade_date,
            stop_loss,
            entry_price,
        ) in rows:
            try:
                _date.fromisoformat(trade_date)
            except ValueError:
                logger.warning(
                    "_replay_trades: skipping row for %s with unparseable date %r",
                    raw_ticker,
                    trade_date,
                )
                continue
            ticker = canonicalize_or_fallback(
                raw_ticker, aliases, logger=logger, context="_replay_trades"
            )
            if ticker not in state:
                state[ticker] = {
                    "shares": 0.0,
                    "avg_cost": 0.0,
                    "entry_date": None,
                    "entry_price": None,
                    "stop_loss": None,
                }
            s = state[ticker]
            if action == "BUY":
                # Story 2.3: `s["shares"]` is kept rounded to the shared 8dp
                # policy after every update (below), so accumulated float
                # dust from prior BUY/SELL arithmetic never carries forward
                # -- this is what makes the zero-crossing check below
                # reliable, rather than comparing a possibly-dust-laden raw
                # float against `QUANTITY_EPSILON`.
                new_total = round_quantity(s["shares"] + shares)
                # Removing the oversell clamp lets `s["shares"]` be negative
                # going in, so a BUY can now land `new_total` on exactly 0
                # -- a position that was oversold and is now exactly closed
                # out. There is no such thing as a weighted-average cost of
                # zero shares, so this guard exists purely to avoid a
                # ZeroDivisionError; it is not the "reset avg_cost on
                # crossing zero" behavior the spec's Design Notes leave out
                # of scope, since a flat (0-share) position is filtered out
                # of every display path regardless of what its avg_cost
                # holds.
                if abs(new_total) < QUANTITY_EPSILON:
                    s["avg_cost"] = 0.0
                else:
                    s["avg_cost"] = (
                        s["avg_cost"] * s["shares"] + price * shares
                    ) / new_total
                s["shares"] = new_total
                if s["entry_date"] is None:
                    s["entry_date"] = trade_date
                if stop_loss is not None:
                    s["stop_loss"] = stop_loss
                if entry_price is not None:
                    s["entry_price"] = entry_price
            else:  # SELL
                # Epsilon-tolerant, not a bare `>` -- a SELL that (within
                # float precision) exactly matches the held quantity must
                # never register as an oversell purely from float-arithmetic
                # dust accumulated across a long replay (mirrors FIFO's own
                # `remaining_to_sell > QUANTITY_EPSILON` idiom).
                if shares - s["shares"] > QUANTITY_EPSILON:
                    # Story 2.3: no longer clamped to 0 -- the shortfall is
                    # preserved as a negative share count, mirroring FIFO's
                    # existing UnmatchedSell convention of reporting the
                    # unconsumed remainder instead of discarding it.
                    logging.getLogger(__name__).warning(
                        "Oversell for %s: sold %s but only %s held; "
                        "recording a negative remainder of %s",
                        ticker,
                        shares,
                        s["shares"],
                        shares - s["shares"],
                    )
                s["shares"] = round_quantity(s["shares"] - shares)
        return state

    @staticmethod
    def _build_position(
        ticker: str,
        s: dict[str, Any],
        current_prices: dict[str, float] | None,
        display_info: dict[str, tuple[float, str]] | None = None,
    ) -> Position:
        """Build a Position model from replay state and live prices.

        For USD stocks, all monetary fields (current_price, current_value,
        unrealised_pnl) are kept in USD so that P&L is self-consistent.
        The web layer converts to GBP for aggregate totals.

        ``shares``/``avg_cost`` are rounded via the shared
        ``round_quantity`` (8dp round-half-even, Story 2.3) rather than
        ``round(..., 4)``, unifying average-cost's precision policy with
        FIFO's. Monetary fields below (``total_cost``, ``current_value``,
        ``unrealised_pnl``, ``current_price``, profit targets) stay on the
        pre-existing 2dp ``round()`` -- a distinct typed fact (AD-24), out
        of this story's scope.
        """
        avg_cost = s["avg_cost"]
        remaining = s["shares"]
        total_cost = round(avg_cost * remaining, 2)

        disp = display_info.get(ticker) if display_info else None
        currency = disp[1] if disp else "GBP"
        # Display metadata is the native quote unit.  Every non-GBP unit
        # must use its native close so current value/P&L never combines a
        # GBP-normalised close with a native-currency cost basis.
        is_native_quote = currency != "GBP"

        if is_native_quote and disp is not None:
            # Use original native price so cost and value are comparable.
            cp: float | None = disp[0]
        else:
            cp = current_prices.get(ticker) if current_prices else None

        current_value = round(cp * remaining, 2) if cp is not None else None
        upnl = (
            round(current_value - total_cost, 2) if current_value is not None else None
        )
        upnl_pct = (
            round(upnl / total_cost * 100, 2)
            if upnl is not None and total_cost > 0
            else None
        )
        entry_price: float | None = s["entry_price"]
        pt20 = round(entry_price * 1.20, 2) if entry_price else None
        pt25 = round(entry_price * 1.25, 2) if entry_price else None
        return Position(
            ticker=ticker,
            shares=round_quantity(remaining),
            avg_cost=round_quantity(avg_cost),
            current_price=round(cp, 2) if cp is not None else None,
            total_cost=total_cost,
            current_value=current_value,
            unrealised_pnl=upnl,
            unrealised_pnl_pct=upnl_pct,
            entry_price=entry_price,
            entry_date=s["entry_date"],
            stop_loss=s["stop_loss"],
            profit_target_20=pt20,
            profit_target_25=pt25,
            price_currency=currency,
        )

    def update_portfolio_snapshot(
        self, cash_balance: float | None = None, portfolio_id: int | None = None
    ) -> None:
        """Append a value snapshot for the given portfolio (or the legacy CSV).

        When ``portfolio_id`` is given the snapshot is written to the
        per-portfolio ``portfolio_snapshots`` table (#147). With no id the
        legacy single-portfolio ``portfolio_value.csv`` is appended, preserving
        pre-multi-portfolio behaviour.
        """
        from datetime import datetime, timezone

        # Load current prices from analysis results
        prices = {}
        data = self._artifacts.read_json(ANALYSIS_JSON, default=None)
        if data is not None:
            try:
                prices = {r["ticker"]: r["price"] for r in data}
            except (TypeError, KeyError) as exc:
                logger.warning("price/value computation failed: %s", exc)

        positions = self.get_portfolio(
            prices if prices else None, portfolio_id=portfolio_id
        )
        total_cost = sum(p.total_cost for p in positions if p.total_cost is not None)
        total_value = sum(
            p.current_value for p in positions if p.current_value is not None
        )
        if total_value is None:
            total_value = total_cost

        if portfolio_id is not None:
            if cash_balance is None:
                cash_balance = self.get_cash_balance(portfolio_id)
            timestamp = datetime.now(timezone.utc).isoformat()
            self._snapshots.append(
                portfolio_id,
                timestamp,
                round(total_value, 2),
                round(total_cost, 2),
                round(cash_balance, 2) if cash_balance is not None else None,
            )
            return

        if cash_balance is None:
            rows = self._artifacts.read_csv_dicts(PORTFOLIO_VALUE_CSV)
            try:
                cash_balance = float(rows[-1].get("cash_balance", "0")) if rows else 0.0
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "could not parse cash balance, defaulting to 0.0: %s", exc
                )
                cash_balance = 0.0

        timestamp = datetime.now(timezone.utc).isoformat()
        investments_value = total_cost
        fieldnames = [
            "timestamp",
            "total_value",
            "total_cost",
            "cash_balance",
            "investments_value",
        ]
        self._artifacts.append_csv_row(
            PORTFOLIO_VALUE_CSV,
            fieldnames,
            {
                "timestamp": timestamp,
                "total_value": f"{total_value:.2f}",
                "total_cost": f"{total_cost:.2f}",
                "cash_balance": f"{cash_balance:.2f}",
                "investments_value": f"{investments_value:.2f}",
            },
        )

    def import_sipp(
        self,
        csv_content: bytes,
        portfolio_id: int | None = None,
        provider_id: str | None = None,
        account_type_id: str | None = None,
    ) -> SippImportResult:
        """Import a SIPP CSV into ``portfolio_id``; return counts and problems.

        ``provider_id``/``account_type_id`` (Story 3.3) are the caller's
        explicit selection, validated here against the CSV's
        auto-detected ``contract`` -- never trusted as-is. Enforcing that
        ``provider_id`` itself is present at all is the HTTP route's job
        (``POST /import-sipp`` 400s before ever reaching this method); this
        method only checks it *when given*: a non-blank ``provider_id``
        that doesn't match the detected contract's own ``provider_id``
        raises ``SippImportError`` (tampering/mismatch) -- ``None``/blank
        is accepted here for callers other than that route (e.g. tests,
        scripts) that don't go through its presence check.
        Account-type validation is symmetric with provider validation, not
        skippable by omission: if the detected contract declares any
        ``account_type_ids``, ``account_type_id`` is required -- missing,
        blank, or not one of the contract's declared values all raise
        ``SippImportError`` naming the valid options. If the contract
        declares no account types, any non-blank ``account_type_id`` itself
        raises ``SippImportError`` (there is nothing to validate it
        against). The value echoed back on ``SippImportResult`` is always
        the one that passed this validation, never the raw parameter.

        The returned cash balance is the effective committed portfolio balance
        after stale-date protection, not necessarily this file's candidate.

        Parses each request's own uploaded bytes exactly once — never a
        shared filesystem path — so concurrent uploads can never read or
        overwrite one another's *input CSV* on disk (#210; this does not by
        itself guarantee DB-level isolation for two concurrent imports into
        the *same* ``portfolio_id``, which is separate, later-story
        territory). Validates the header row first
        (missing required columns abort the import with ``SippImportError``
        before any DB write). Every monetary cell is parsed into a
        currency-preserving ``Money`` (Story 1.4); ambiguous, unsupported,
        malformed-locale, or contradictory currency evidence rejects its
        row via ``failed_rows`` rather than silently defaulting to GBP or
        ``0.0`` (AC3) — ``parse_errors`` is kept only for API compatibility
        and stays empty for money fields now that every such failure is a
        ``failed`` outcome. Idempotency is keyed on ``(portfolio_id,
        idempotency_key)`` — a hash of the row's own content, not its
        provider Reference — so overlapping quarterly exports dedupe
        deterministically and the same CSV imported into two portfolios
        does not collide (#147).

        Every row is first evaluated into an in-memory plan with no database
        writes. Only if every row is safe to write (none resolve to
        ``failed``) does the method open one write transaction and commit
        trades, cash flows, the per-currency cash balance, and the portfolio
        snapshot together. If any row is ``failed``, the whole plan is
        rejected and nothing is persisted — see ``failed_rows`` and
        ``status="rejected"`` on the returned result (#210's atomic-commit
        follow-up).

        Every row resolves to exactly one of ``inserted``/``duplicate``/
        ``skipped``/``failed``, and the returned counts say which. For a
        committed plan those come from the write itself, so a row suppressed
        by the unique index is reported as a duplicate rather than counted
        as a fresh buy/sell/cash success; for a rejected plan they come from
        a read-only pre-check and describe no persisted state at all.
        """
        parse_errors: list[str] = []
        skipped_rows: list[str] = []
        failed_rows: list[str] = []
        # Staged writes -- the exact positional args ``insert_ignore`` needs,
        # minus the connection, which is supplied only once the plan is known
        # to be fully valid (Gate 3: no DB connection is opened for writes
        # until every row has been evaluated).
        planned_trades: list[tuple[Any, ...]] = []
        planned_cash_flows: list[tuple[Any, ...]] = []
        # GH-311: row-lineage bookkeeping, parallel to the two planned-write
        # lists above. ``planned_trades`` already embeds its own row's
        # ``idx`` (as ``source_row_index``, tuple position 10) so no
        # parallel list is needed for it; ``planned_cash_flows`` does not,
        # hence ``cash_flow_row_idxs``. A benign-empty row never joins
        # either planned list, so its ``idx`` is recorded directly here.
        cash_flow_row_idxs: list[int] = []
        skipped_row_idxs: list[int] = []
        # Story 2.4: one fresh id per import call, stamped on every trade
        # this call writes -- traces a sell (or any trade) back to the
        # specific import that produced it.
        import_batch_id = uuid.uuid4().hex
        # Rows with genuinely no signal (no quantity, no debit/credit, no
        # parse error) -- counted toward the row-count reconciliation below
        # so a benign informational row never falsely flags status="error",
        # but not surfaced to the user as a "skipped due to issues" row
        # since nothing actually went wrong (#187).
        benign_empty_count = 0

        def parse_field_money(
            s: str | None,
            field: str = "",
            ref: str = "",
            *,
            decimal_separator_hint: str | None = None,
        ) -> tuple[Money | None, bool]:
            """Parse one CSV cell into ``(Money, marker_present)``.

            Returns ``(None, False)`` for a blank/``"n/a"`` cell (a no-op,
            matching ``clean_amount``'s existing behaviour exactly) or for a
            cell that failed to parse -- in the failure case, a row-cannot-
            proceed entry is appended to ``failed_rows`` here rather than
            falling back to a silently-guessed ``0.0`` (AC3).

            ``decimal_separator_hint`` is only ever passed for the Price
            field (see call site) -- Debit, Credit, and Running Balance
            keep strict ambiguity rejection, since they're the row's own
            source of that evidence and can't borrow it from themselves.
            """
            if not s or s.strip() in ("", "n/a"):
                return None, False
            try:
                # No ``ref=`` passed: ``parse_money``'s own error messages
                # should show the actual malformed cell text, not the row
                # label -- the row label is prepended separately below.
                money = parse_money(
                    s,
                    default_currency="GBP",
                    decimal_separator_hint=decimal_separator_hint,
                )
            except MoneyParseError as exc:
                failed_rows.append(
                    f"row {ref or '?'}: {field or 'amount'} {exc.code}: {exc.detail}"
                )
                return None, False
            return money, has_currency_marker(s)

        def classify_flow_type(description: str) -> str:
            d = description.lower()
            if "contribution" in d:
                return "CONTRIBUTION"
            elif "div" in d:
                return "DIVIDEND"
            elif "interest" in d:
                return "INTEREST"
            elif "tax relief" in d:
                return "TAX_RELIEF"
            elif "trf" in d:
                return "TRANSFER"
            elif "debit" in d:
                return "WITHDRAWAL"
            return "OTHER"

        # The single in-memory transformation this method performs: decode
        # the caller-owned bytes once, then parse via a StringIO-backed
        # DictReader. No file path is ever opened, read, or re-read (#210).
        # ``newline=None`` reproduces the universal-newline translation that
        # ``open(..., encoding="utf-8-sig")`` (the previous implementation)
        # applied by default -- without it, a bare ``\r``-terminated CSV
        # (classic Mac-style line endings, occasionally seen in older
        # broker exports) raises ``_csv.Error`` instead of parsing cleanly.
        # GH-311: a content digest of the raw uploaded bytes, computed once
        # here (before decode) and reused for the receipt row -- never
        # recomputed from the decoded text, which could differ byte-for-
        # byte from the original upload.
        source_digest = hashlib.sha256(csv_content).hexdigest()
        text = csv_content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=None))
        # Some provider exports prepend a run of BOM noise to the very
        # first header cell (e.g. "﻿...﻿Date" or "ï»¿...ï»¿Date").
        # ``utf-8-sig`` strips only one real BOM, so the first column key
        # stays polluted and every ``row.get("Date")`` misses it —
        # silently blanking the trade date (#166, #171). Normalise the
        # header names before reading rows so the value lookups use the
        # clean column names.
        reader.fieldnames = [
            _HEADER_BOM_NOISE_RE.sub("", h or "").strip()
            for h in (reader.fieldnames or [])
        ]
        fieldnames = reader.fieldnames
        rows = list(reader)

        # Contract detection is header-based and deterministic (GH-310):
        # it replaces the previous hardcoded "every REQUIRED_SIPP_COLUMNS
        # cell present" check with a registry lookup over versioned,
        # data-driven contracts. Zero or ambiguous matches fail closed
        # before any DB write, preserving the route's existing
        # ``except SippImportError`` 400 contract.
        try:
            contract = get_contract_registry().detect(fieldnames or [])
        except ContractRegistryError as exc:
            raise SippImportError(str(exc)) from exc

        # Story 3.3: validate the caller's explicit selection against the
        # auto-detected contract before doing anything else -- a rejected
        # selection must abort before any row is even parsed further.
        if provider_id and provider_id != contract.provider_id:
            raise SippImportError(
                "selected provider does not match this CSV's detected "
                f"provider (expected {contract.provider_id!r}, got "
                f"{provider_id!r})"
            )

        # Symmetric with the provider check above, not skippable by
        # omission: a contract that declares account types requires one: a
        # contract that declares none forbids one.
        if contract.account_type_ids:
            if not account_type_id or not account_type_id.strip():
                raise SippImportError(
                    "select an account type: " + ", ".join(contract.account_type_ids)
                )
            if account_type_id not in contract.account_type_ids:
                raise SippImportError(
                    f"unknown account type {account_type_id!r}; valid "
                    "options: " + ", ".join(contract.account_type_ids)
                )
            validated_account_type_id = account_type_id
        else:
            if account_type_id and account_type_id.strip():
                raise SippImportError(
                    f"this provider has no account types; got {account_type_id!r}"
                )
            validated_account_type_id = ""

        # Every downstream phase reads canonical field keys, never raw
        # provider column names -- ``_detect_reconciliation_issues`` is the
        # one exception: it's independently unit-tested against raw
        # column-name dicts, so it keeps reading ``rows`` (the raw rows)
        # directly rather than the normalized view below.
        normalized_rows = [
            ContractNormalizer.normalize_row(contract, row) for row in rows
        ]
        # GH-311: one canonical-row digest per source row, computed once up
        # front from the normalized (never raw) view -- reused below to
        # build each row's lineage entry without persisting any raw
        # financial CSV content.
        row_digests = [canonical_row_digest(row) for row in normalized_rows]

        # Detection has no DB side effects of its own (Story 1.5, AC5) --
        # run it once the rows are available, independent of the main
        # insert loop below. Persisted only if the plan actually commits.
        reconciliation_issues = _detect_reconciliation_issues(rows)

        buy_count, sell_count, cash_count = 0, 0, 0
        inserted_count = duplicate_count = 0
        # A rejected plan never opens a write connection, so its per-row
        # inserted/duplicate outcomes can only come from a read-only
        # pre-check. One query per table per import (not per row): the keys
        # already persisted for this portfolio. Newly planned keys are added
        # as they're classified so a row repeated *within* one CSV reads as a
        # duplicate too, exactly as INSERT OR IGNORE would treat it.
        existing_trade_keys = self._trades.idempotency_keys_for_portfolio(portfolio_id)
        existing_cash_flow_keys = self._cash_flows.idempotency_keys_for_portfolio(
            portfolio_id
        )
        provisional_inserted_count = provisional_duplicate_count = 0

        def classify(key: str, seen: set[str]) -> None:
            """Count ``key`` as provisionally inserted or duplicate."""
            nonlocal provisional_inserted_count, provisional_duplicate_count
            if key in seen:
                provisional_duplicate_count += 1
            else:
                provisional_inserted_count += 1
                seen.add(key)

        # The authoritative cash balance is the running balance of the
        # latest-dated row, chosen independently of the CSV's row order so a
        # newest-first export yields the same balance as an oldest-first one
        # (#158), tracked per currency (Story 1.4) so a EUR Running Balance
        # row and a GBP Running Balance row never compete against each
        # other's rank. Rank = (has-ISO-date, iso-date, file-index): an
        # ISO-dated row always beats an unparseable-date row, later dates
        # win, and ties fall to the last such row in the file. With no
        # parseable dates this degrades to the previous last-row-in-file
        # behaviour.
        cash_balance_rank: dict[str, tuple[int, str, int]] = {}
        cash_balance: dict[str, Decimal] = {}

        # Story 3.4: a contract with no ``running_balance`` mapping at all
        # (e.g. IG, which never states an absolute balance) uses the
        # additive delta-sum fallback below instead of the snapshot-style
        # Running Balance handling -- keyed only on the contract's own
        # capability, never on ``contract.provider_id`` (AC4).
        has_running_balance_mapping = (
            CanonicalField.RUNNING_BALANCE in contract.field_mapping.values()
        )
        # Parallel to ``planned_trades``/``planned_cash_flows`` (by row
        # ``idx``): each row's net cash contribution (credit - debit),
        # already confirmed GBP -- populated only when
        # ``has_running_balance_mapping`` is False, since it's the only
        # path that ever reads it. Summed at commit time, filtered to rows
        # whose *actual* write outcome was not "duplicate", so a repeated
        # import never double-counts (mirrors the II Running Balance
        # path's own idempotency, which is guaranteed by #160's stale-date
        # guard on an absolute snapshot instead).
        fallback_delta_rows: list[tuple[int, Decimal]] = []

        # Loaded once per import (not per row) -- the alias map is a small,
        # rarely-changing local config file (no per-row/caching concern).
        aliases = load_aliases()

        for idx, row in enumerate(normalized_rows):
            qty = row.get("quantity", "").strip()
            symbol = row.get("security_symbol", "").strip()
            reference_raw = row.get("reference", "").strip()
            # Blank/"n/a" means "this provider didn't give one" — treat it
            # as no reference (None), not a literal value. Every insert is
            # deduped per-portfolio on (portfolio_id, reference) via a
            # partial unique index that exempts NULL references (#147);
            # passing the literal string "n/a" through would make every
            # such row collide with the first one and get silently
            # dropped by INSERT OR IGNORE (#186) — real SIPP exports
            # commonly leave Reference "n/a" on every interest/dividend
            # row, so this wasn't a hypothetical edge case.
            reference = (
                reference_raw
                if reference_raw and reference_raw.lower() != "n/a"
                else None
            )
            # Many providers leave Reference blank/"n/a" on non-trade rows
            # (interest, dividends) — fall back to a 1-based CSV row number
            # (accounting for the header row) so issue detail can still
            # point at a specific row instead of a useless "row n/a" for
            # every such row (#185).
            row_label = reference if reference else f"CSV row {idx + 2}"

            # Story 3.4: a row ``ContractNormalizer.normalize_row`` already
            # marked for whole-plan rejection (``reject_unless_column_equals``
            # not satisfied, or ``text_extraction``'s ``required_marker``
            # matched but the full pattern still failed) -- reuse the exact
            # same ``failed_rows``/whole-plan-rejection mechanism every
            # other row-level failure in this method uses, rather than a
            # second, parallel rejection channel.
            rejected_reason = row.get(REJECTED_REASON_KEY)
            if rejected_reason:
                failed_rows.append(f"row {row_label}: {rejected_reason}")
                continue

            # Story 1.5, AC4: both Debit and Credit populated on one row is
            # contradictory monetary evidence -- reject it outright rather
            # than silently preferring Debit (today's "BUY" if debit > 0
            # else "SELL" if credit > 0" always resolves the contradiction
            # toward Debit). Checked on the raw cell strings, not the parsed
            # amounts: a parse failure and a blank cell both resolve to
            # None/0, which would make "populated" indistinguishable from
            # "genuinely absent".
            debit_raw = row.get("debit", "").strip()
            credit_raw = row.get("credit", "").strip()
            if debit_raw not in ("", "n/a") and credit_raw not in ("", "n/a"):
                failed_rows.append(
                    f"row {row_label}: contradictory row has both Debit "
                    f"{debit_raw!r} and Credit {credit_raw!r} populated"
                )
                continue

            debit_money, debit_marker = parse_field_money(
                row.get("debit", ""), "Debit", row_label
            )
            credit_money, credit_marker = parse_field_money(
                row.get("credit", ""), "Credit", row_label
            )
            rb_raw = row.get("running_balance", "").strip()
            rb_money, rb_marker = parse_field_money(
                row.get("running_balance", ""), "Running Balance", row_label
            )
            # A trade row's Price can be a genuinely ambiguous 3dp amount
            # (e.g. "£2.351") that's indistinguishable from a grouped
            # whole number on its own -- but Debit/Credit/Running Balance
            # on the *same* row, if any of them mixes both separators
            # (e.g. "£1,501.99"), unambiguously establish this row's
            # decimal separator, which resolves the Price reading too.
            row_decimal_hint = establish_row_decimal_separator(
                debit_raw, credit_raw, rb_raw
            )
            debit = debit_money.amount if debit_money else Decimal("0")
            credit = credit_money.amount if credit_money else Decimal("0")
            date = _to_iso_date(row.get("date", "").strip())
            sedol = row.get("security_identifier", "").strip()
            description = row.get("description", "").strip()
            idempotency_key = _idempotency_key(date, symbol, sedol, qty, description)

            # Story 3.4: the cash-balance fallback (no ``running_balance``
            # mapping on this contract) writes to the legacy single-value,
            # GBP-only ``cash_balance:{portfolio_id}`` slot -- so a
            # contributing row resolving to any other currency must reject
            # the whole plan rather than be silently excluded from the sum
            # (money must not silently vanish from a reported balance).
            # Checked here, before any write, exactly like every other
            # ``failed_rows`` gate in this loop.
            if not has_running_balance_mapping and (debit > 0 or credit > 0):
                flow_currency = (
                    credit_money.currency
                    if credit_money is not None and credit > 0
                    else debit_money.currency
                    if debit_money is not None and debit > 0
                    else "GBP"
                )
                if flow_currency != "GBP":
                    failed_rows.append(
                        f"row {row_label}: cash-balance fallback only "
                        f"supports GBP, got {flow_currency!r}"
                    )
                else:
                    fallback_delta_rows.append((idx, credit - debit))

            # Price is only monetary evidence on a trade row -- a non-trade
            # row's Price cell can carry an unrelated numeric shape (e.g. a
            # 5dp GBP/USD FX rate on a housekeeping row) that this method
            # never reads, so it must not be run through strict Money
            # parsing at all (Gate 4; grounded in real production data).
            price_money: Money | None = None
            price_marker = False

            if qty and qty != "n/a":
                raw_identity = next(
                    (
                        value.upper()
                        for value in (symbol, sedol)
                        if value and value.lower() != "n/a"
                    ),
                    None,
                )

                if raw_identity is not None:
                    try:
                        shares = float(qty.replace("£", "").replace(",", ""))
                    except ValueError:
                        # A row that cannot be safely written must not be
                        # written at all -- and per AC #2, one such row
                        # rejects the *entire* plan, not just itself.
                        failed_rows.append(
                            f"row {row_label}: unparseable quantity {qty!r}"
                        )
                        logger.warning(
                            "rejecting import: unparseable quantity %r (ref %s)",
                            qty,
                            row_label,
                        )
                        shares = None

                    if shares is not None:
                        price_money, price_marker = parse_field_money(
                            row.get("price", ""),
                            "Price",
                            row_label,
                            decimal_separator_hint=row_decimal_hint,
                        )
                        price = price_money.amount if price_money else Decimal("0")
                        if shares <= 0 or price <= 0:
                            failed_rows.append(
                                f"row {row_label}: non-positive "
                                f"shares/price ({shares}, {price})"
                            )
                        else:
                            action = (
                                "BUY" if debit > 0 else "SELL" if credit > 0 else None
                            )
                            if action:
                                # The chosen Symbol/SEDOL identity is resolved
                                # via canonical_ticker directly (not
                                # canonicalize_or_fallback) so this method
                                # can catch AmbiguousTickerAliasError itself
                                # and reject the row via failed_rows instead
                                # of raising or silently falling back --
                                # import is the one call site with a
                                # per-row rejection channel. This rejection
                                # branch does not call classify() -- matching every
                                # sibling row-rejection branch in this
                                # function.
                                ticker: str | None
                                try:
                                    ticker = canonical_ticker(raw_identity, aliases)
                                except AmbiguousTickerAliasError as exc:
                                    failed_rows.append(
                                        f"row {row_label}: ambiguous ticker alias "
                                        f"for {raw_identity!r}: {exc}"
                                    )
                                    ticker = None

                                if ticker is not None:
                                    planned_trades.append(
                                        (
                                            ticker,
                                            action,
                                            shares,
                                            float(price),
                                            date,
                                            "",
                                            reference or None,
                                            portfolio_id,
                                            idempotency_key,
                                            price_money.currency
                                            if price_money
                                            else "GBP",
                                            idx,
                                            "sipp_import",
                                            import_batch_id,
                                        )
                                    )
                                    classify(idempotency_key, existing_trade_keys)
                            else:
                                failed_rows.append(
                                    f"row {row_label}: trade with no "
                                    "debit/credit amount"
                                )
                else:
                    amount_money = credit_money if credit > 0 else debit_money
                    amount = credit if credit > 0 else debit
                    if amount > 0:
                        flow_type = classify_flow_type(description)
                        planned_cash_flows.append(
                            (
                                date,
                                flow_type,
                                None,
                                float(amount),
                                description,
                                reference,
                                portfolio_id,
                                idempotency_key,
                                amount_money.currency if amount_money else "GBP",
                            )
                        )
                        cash_flow_row_idxs.append(idx)
                        classify(idempotency_key, existing_cash_flow_keys)
                    else:
                        skipped_row_idxs.append(idx)
                        benign_empty_count += 1
            else:
                # No Quantity means this can't be a share trade regardless
                # of whether Symbol/Sedol is populated — some providers
                # tag a cash dividend with the paying company's symbol for
                # reference (e.g. "Div 60 SIGNIFY NV") even though no
                # shares changed hands. Previously any populated Symbol
                # here caused a silent `continue` that dropped the row
                # with zero visibility — not even counted as skipped
                # (#186). Fall through to the same cash-flow handling as
                # a blank-Symbol row; a genuine no-amount/no-signal row
                # still does nothing below.
                amount_money = credit_money if credit > 0 else debit_money
                amount = credit if credit > 0 else debit
                if amount > 0:
                    flow_type = classify_flow_type(description)
                    planned_cash_flows.append(
                        (
                            date,
                            flow_type,
                            None,
                            float(amount),
                            description,
                            reference,
                            portfolio_id,
                            idempotency_key,
                            amount_money.currency if amount_money else "GBP",
                        )
                    )
                    cash_flow_row_idxs.append(idx)
                    classify(idempotency_key, existing_cash_flow_keys)
                else:
                    skipped_row_idxs.append(idx)
                    benign_empty_count += 1

            # Row-level contradictory-currency cross-check (AC3): among the
            # fields that resolved from an *explicit* marker (a marker-less
            # field that silently defaulted never contradicts anything),
            # more than one distinct currency means this row's monetary
            # evidence disagrees with itself.
            marker_currencies = {
                money.currency
                for money, marker in (
                    (debit_money, debit_marker),
                    (credit_money, credit_marker),
                    (rb_money, rb_marker),
                    (price_money, price_marker),
                )
                if money is not None and marker
            }
            if len(marker_currencies) > 1:
                failed_rows.append(
                    f"row {row_label}: contradictory currency evidence "
                    f"({sorted(marker_currencies)})"
                )

            # Story 1.5, AC1/AC2: a genuine zero/negative closing balance
            # must win the rank exactly like a positive one -- gate on
            # whether the cell parsed successfully (rb_money is not None),
            # never on its sign. A cell that fails to parse already added
            # its own failed_rows entry in parse_field_money above and
            # never reaches here as a fabricated 0.0.
            if rb_money is not None:
                rb_currency = rb_money.currency
                is_iso = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date))
                # Reverse-chronological same-day tie-break (PRD §10 / FR-9):
                # the first-listed row for a given Date is the most recent
                # transaction of that day. Negating idx makes the smaller
                # original index (the first-listed row) produce the larger
                # rank value and win the tie -- the date components still
                # dominate the comparison, so cross-date ordering is
                # unaffected; only the within-same-date direction changes.
                rank = (1 if is_iso else 0, date if is_iso else "", -idx)
                existing_rank = cash_balance_rank.get(rb_currency)
                if existing_rank is None or rank > existing_rank:
                    cash_balance_rank[rb_currency] = rank
                    cash_balance[rb_currency] = rb_money.amount

        total_rows = len(rows)

        if failed_rows:
            # AC #2/#4: the plan is rejected in full -- nothing planned above
            # was ever written (no connection was opened for writes), and
            # every failing row/reason is reported so the CSV can be
            # corrected and re-uploaded. The inserted/duplicate counts are
            # the read-only provisional ones -- "what would have happened" --
            # since no write was ever attempted. buy/sell/cash stay at 0:
            # those three describe committed state only.
            return SippImportResult(
                cash_balance=0.0,
                buy_count=0,
                sell_count=0,
                cash_flow_count=0,
                skipped_rows=[],
                inserted_count=provisional_inserted_count,
                duplicate_count=provisional_duplicate_count,
                skipped_count=benign_empty_count,
                parse_errors=parse_errors,
                failed_rows=failed_rows,
                total_rows=total_rows,
                status="rejected",
                provider_id=contract.provider_id,
                provider_name=contract.provider_name,
                contract_version=contract.version,
                account_type_id=validated_account_type_id,
                preview_rows=_build_preview_rows(normalized_rows),
            )

        # Commit phase: every row was safe to write. Persist trades, cash
        # flows, the per-currency cash balance, and the portfolio snapshot
        # together in one transaction (AC #1) so a failure partway through
        # leaves no partial state (AC #3) -- SQLite discards an uncommitted
        # transaction when its connection closes, which is the rollback
        # mechanism relied on below; no explicit ``conn.rollback()`` call is
        # required for it to work, though one is included for clarity.
        # Load current prices once, before opening the write transaction --
        # this is a filesystem read unrelated to trades.db, so doing it while
        # holding BEGIN IMMEDIATE's write lock would needlessly widen the
        # lock window for concurrent imports without any correctness benefit.
        prices: dict[str, float] = {}
        if portfolio_id is not None:
            data = self._artifacts.read_json(ANALYSIS_JSON, default=None)
            if data is not None:
                try:
                    prices = {r["ticker"]: r["price"] for r in data}
                except (TypeError, KeyError) as exc:
                    logger.warning("price/value computation failed: %s", exc)

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # GH-311: one row-lineage entry per source row that reached the
            # commit phase (a benign-empty row included) -- built here, at
            # write time, so its outcome/link reflects what actually
            # happened on this connection, never the earlier provisional
            # read-only classification.
            row_lineage: list[ImportRowLineage] = []
            # Story 3.4: the fallback delta's per-row write outcome,
            # keyed by ``idx`` -- populated in both insert loops below,
            # read afterwards so the delta sum only counts a row actually
            # inserted this transaction, never one suppressed as a
            # duplicate (idempotent re-import, mirroring the II Running
            # Balance path's own dedup guarantee).
            outcome_by_idx: dict[int, str] = {}
            # Write-time outcomes are authoritative: a row suppressed by the
            # unique index is a duplicate, never a buy/sell/cash "success".
            for trade_args in planned_trades:
                idx = trade_args[10]
                outcome = self._trades.insert_ignore(conn, *trade_args)
                outcome_by_idx[idx] = outcome
                trade_id: int | None = None
                if outcome == "duplicate":
                    duplicate_count += 1
                else:
                    inserted_count += 1
                    if trade_args[1] == "BUY":
                        buy_count += 1
                    else:
                        sell_count += 1
                    # ``cursor.rowcount`` (checked inside ``insert_ignore``)
                    # already distinguished "actually inserted" from
                    # "ignored as duplicate" -- ``last_insert_rowid()`` on
                    # this same connection, read immediately after and
                    # before any other write, is that row's id.
                    trade_id = int(
                        conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    )
                row_lineage.append(
                    ImportRowLineage(
                        physical_row_number=idx,
                        canonical_row_digest=row_digests[idx],
                        outcome=outcome,
                        trade_id=trade_id,
                    )
                )
            # ``strict=True`` (review finding): these two lists are
            # maintained in parallel by construction (every append site
            # above appends to both together) -- fail loudly if a future
            # edit ever breaks that invariant, instead of ``zip`` silently
            # truncating and misattributing row-lineage to the wrong row.
            for cf_idx, flow_args in zip(
                cash_flow_row_idxs, planned_cash_flows, strict=True
            ):
                outcome = self._cash_flows.insert_ignore(conn, *flow_args)
                outcome_by_idx[cf_idx] = outcome
                cash_flow_id: int | None = None
                if outcome == "duplicate":
                    duplicate_count += 1
                else:
                    inserted_count += 1
                    cash_count += 1
                    cash_flow_id = int(
                        conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    )
                row_lineage.append(
                    ImportRowLineage(
                        physical_row_number=cf_idx,
                        canonical_row_digest=row_digests[cf_idx],
                        outcome=outcome,
                        cash_flow_id=cash_flow_id,
                    )
                )
            for skip_idx in skipped_row_idxs:
                row_lineage.append(
                    ImportRowLineage(
                        physical_row_number=skip_idx,
                        canonical_row_digest=row_digests[skip_idx],
                        outcome="skipped",
                        reason="benign empty row: no quantity/amount to import",
                    )
                )

            for (
                currency,
                date,
                prior,
                expected,
                actual,
                difference,
                row_ref,
            ) in reconciliation_issues:
                self._cash_reconciliation.insert_issue_on_connection(
                    conn,
                    portfolio_id,
                    date,
                    prior,
                    expected,
                    actual,
                    difference,
                    row_ref,
                    currency,
                )

            # Every currency with a winning Running Balance row joins the
            # new per-currency cash_balances table (AD-24), each guarded by
            # the #160 stale-date check independently.
            self._apply_import_cash_balances(
                conn, portfolio_id, cash_balance, cash_balance_rank
            )

            # The legacy account_state cash_balance:{pid} mechanism (and
            # SippImportResult.cash_balance) stays scoped to GBP only,
            # exactly matching today's single-value behaviour when no
            # non-GBP markers are present in a file -- reads elsewhere are
            # not migrated onto cash_balances by this story.
            gbp_balance = cash_balance.get("GBP")
            gbp_candidate = float(gbp_balance) if gbp_balance is not None else 0.0
            if has_running_balance_mapping:
                # Story 1.5, AC1/AC2: test whether a winner was found at all
                # (gbp_balance is not None), never its sign -- a winning
                # 0.0 or negative balance must still be applied.
                if gbp_balance is not None:
                    gbp_rank = cash_balance_rank.get("GBP")
                    cash_asof = (
                        gbp_rank[1]
                        if gbp_rank is not None and gbp_rank[0] == 1
                        else None
                    )
                    self._apply_import_cash_balance(
                        conn, portfolio_id, gbp_candidate, cash_asof
                    )
            else:
                # Story 3.4 (review pass 2 correction): a contract with no
                # ``running_balance`` mapping (e.g. IG) has no absolute
                # snapshot to compare by date at all -- apply this
                # import's own net delta unconditionally, via a dedicated
                # method that never consults the #160 stale-date guard
                # (see ``_apply_import_cash_balance_delta``'s docstring for
                # why routing a delta through that guard is exactly review
                # pass 1's bug). Counts only rows whose actual write
                # outcome (``outcome_by_idx``) was not "duplicate", so a
                # repeated import never double-counts.
                fallback_delta = sum(
                    (
                        contribution
                        for row_idx, contribution in fallback_delta_rows
                        if outcome_by_idx.get(row_idx) != "duplicate"
                    ),
                    start=Decimal("0"),
                )
                self._apply_import_cash_balance_delta(
                    conn, portfolio_id, float(fallback_delta)
                )

            # Read the balance that is actually committed -- never this
            # file's own candidate, which _apply_import_cash_balance may
            # have just rejected as stale. Read on the open transaction
            # connection so it sees this import's own write, and reuse the
            # value below rather than re-reading on a second connection.
            effective_cash_balance = self._effective_cash_balance(
                conn, portfolio_id, gbp_candidate
            )
            if portfolio_id is not None:
                self._write_import_snapshot(
                    conn, portfolio_id, effective_cash_balance, prices
                )

            # Every data row must land in exactly one bucket -- a mismatch
            # means some row was silently unaccounted for (a bug), not just
            # a data-quality issue, so it's flagged distinctly from an
            # ordinary skipped-row warning (#187). Reconciled on the four
            # FR-13 outcomes rather than on buy/sell/cash, which no longer
            # cover every written row now that a duplicate is counted as a
            # duplicate instead of a success. Computed here (moved up from
            # after the transaction, GH-311 review) so the receipt's
            # ``status`` and the returned ``SippImportResult.status`` are
            # the exact same value, never two independently-derived
            # formulas that could silently diverge.
            total_actions = (
                inserted_count
                + duplicate_count
                + len(skipped_rows)
                + benign_empty_count
            )
            status = "ok" if total_actions == total_rows else "error"

            # GH-311: the durable receipt + row-lineage write, on the same
            # open connection and inside this same transaction, immediately
            # before commit -- every count here is already final (the
            # trade/cash-flow loops above are the last thing that can
            # change inserted/duplicate/skipped counts). A committed plan
            # never has a failed row (the ``failed_rows`` early return
            # above rejects the whole plan first), so ``failed_count`` is
            # always 0 here.
            # Story 3.3: the receipt now records exactly the caller's
            # validated per-request selection, never the contract's full
            # supported set (deferred-work.md item from Story 3.2's review).
            receipt_id = self._import_receipts.insert_receipt_on_connection(
                conn,
                import_batch_id=import_batch_id,
                portfolio_id=portfolio_id,
                provider_id=contract.provider_id,
                account_type_id=validated_account_type_id or None,
                contract_id=contract.contract_id,
                contract_version=contract.version,
                contract_content_digest=contract_content_digest(contract),
                source_digest=source_digest,
                status=status,
                inserted_count=inserted_count,
                duplicate_count=duplicate_count,
                skipped_count=benign_empty_count,
                failed_count=0,
            )
            self._import_receipts.insert_rows_on_connection(
                conn, receipt_id, row_lineage
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if portfolio_id is None:
            # The legacy single-portfolio path appends to
            # ``portfolio_value.csv``, a different artifact outside
            # ``trades.db``'s one-transaction contract -- best-effort, and
            # only run after the transaction above has successfully
            # committed (never for a rejected plan, never before the cash
            # balance it reads has landed).
            self.update_portfolio_snapshot(effective_cash_balance, portfolio_id)

        print(
            f"SIPP import complete: {buy_count} BUY, {sell_count} SELL, "
            f"{cash_count} cash flows, {duplicate_count} duplicates, "
            f"{benign_empty_count} skipped, "
            f"{len(parse_errors)} parse errors, status={status}. "
            # The effective balance, matching what the result and the page
            # report -- printing this file's own candidate here would show a
            # number that was rejected as stale and appears nowhere else.
            f"Cash balance: GBP {effective_cash_balance:,.2f} "
            f"(this file's GBP candidate: GBP {gbp_candidate:,.2f})"
        )
        return SippImportResult(
            cash_balance=effective_cash_balance,
            buy_count=buy_count,
            sell_count=sell_count,
            cash_flow_count=cash_count,
            skipped_rows=skipped_rows,
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            skipped_count=benign_empty_count,
            parse_errors=parse_errors,
            failed_rows=[],
            total_rows=total_rows,
            status=status,
            reconciliation_issue_count=len(reconciliation_issues),
            receipt_id=receipt_id,
            provider_id=contract.provider_id,
            provider_name=contract.provider_name,
            contract_version=contract.version,
            account_type_id=validated_account_type_id,
        )

    def save_price_cache(
        self,
        prices: dict[str, float],
        currencies: dict[str, tuple[float, str]] | None = None,
    ) -> None:
        """Persist fetched prices to the DB with a UTC timestamp.

        currencies: optional {ticker: (original_price, currency_code)} for display.
        """
        self._price_cache.upsert_many(prices, currencies)

    def load_price_cache(
        self,
    ) -> tuple[dict[str, float], str | None, dict[str, tuple[float, str]]]:
        """Return (prices, fetched_at, display_info) from the DB.

        display_info: {ticker: (original_price, currency_code)}
        """
        rows = self._price_cache.load_all()
        if not rows:
            return {}, None, {}
        prices = {r[0]: r[1] for r in rows}
        fetched_at = rows[0][2]
        display_info: dict[str, tuple[float, str]] = {
            r[0]: (r[4], r[3] or "GBP") for r in rows if r[4] is not None
        }
        return prices, fetched_at, display_info

    def get_cached_fx_rates(
        self, dates: list[str], pair: str = "GBPUSD=X"
    ) -> dict[str, float]:
        """Return cached ``{date: rate}`` rows for one FX pair."""
        return self._fx_rates.get_many(dates, pair)

    def save_fx_rates(self, rates: dict[str, float], pair: str = "GBPUSD=X") -> None:
        """Persist resolved ``{date: rate}`` rows for one FX pair."""
        self._fx_rates.upsert_many(rates, pair)

    def get_cached_ticker_currencies(self, tickers: list[str]) -> dict[str, str]:
        """Return persisted ``{ticker: currency}`` rows for tickers with no
        live price-cache entry (delisted/no-longer-scanned)."""
        return self._ticker_currency_cache.get_many(tickers)

    def save_ticker_currencies(self, currencies: dict[str, str]) -> None:
        """Persist resolved ``{ticker: currency}`` rows so a delisted or
        no-longer-scanned ticker's currency lookup survives restarts."""
        self._ticker_currency_cache.upsert_many(currencies)

    def set_cash_balance(self, amount: float, portfolio_id: int | None = None) -> None:
        """Persist a portfolio's cash balance (Running Balance) to account_state."""
        self._account.set(self._cash_key(portfolio_id), str(amount))

    def get_cash_balance(self, portfolio_id: int | None = None) -> float | None:
        """Return a portfolio's stored cash balance, or None if not yet set."""
        value = self._account.get(self._cash_key(portfolio_id))
        return float(value) if value is not None else None

    @staticmethod
    def _cash_date_key(portfolio_id: int | None) -> str:
        """account_state key holding the as-of date of the stored cash balance."""
        return (
            f"cash_balance_date:{portfolio_id}"
            if portfolio_id is not None
            else "cash_balance_date"
        )

    def _effective_cash_balance(
        self, conn: Any, portfolio_id: int | None, fallback: float
    ) -> float:
        """Return the cash balance actually committed for ``portfolio_id``.

        Read on the caller's open connection so it reflects writes made
        earlier in the same transaction. A stored value that cannot be
        parsed as a number falls back to ``fallback`` rather than raising:
        this runs inside the import's write transaction, so one malformed
        legacy value must not roll back an otherwise valid import.
        """
        stored = self._account.get_on_connection(conn, self._cash_key(portfolio_id))
        if stored is None:
            return 0.0
        try:
            return float(stored)
        except (TypeError, ValueError):
            logger.warning(
                "unparseable stored cash balance %r for portfolio %s; "
                "falling back to this import's own balance",
                stored,
                portfolio_id,
            )
            return fallback

    def _apply_import_cash_balance(
        self, conn: Any, portfolio_id: int | None, amount: float, as_of: str | None
    ) -> None:
        """Store an imported cash balance only if it's not older than the one
        already stored (#160), writing on the caller's open connection so
        the balance update joins the same transaction as the import's
        trade/cash-flow writes (AC #1). Does not commit.

        The provider Running Balance is a snapshot, so importing an *older*
        SIPP file after a newer one must not regress the balance to a stale
        date. We persist the balance's as-of date alongside it and only replace
        when the incoming date is newer-or-equal (ties → the newer import wins).
        An incoming balance with no parseable date overwrites only when
        nothing dated is already stored (Story 1.5, AC3) — it can never be
        compared against a stored date, so it must not blindly win over one.
        """
        stored_date = self._account.get_on_connection(
            conn, self._cash_date_key(portfolio_id)
        )
        if stored_date is None or (as_of is not None and as_of >= stored_date):
            self._account.set_on_connection(
                conn, self._cash_key(portfolio_id), str(amount)
            )
            if as_of is not None:
                self._account.set_on_connection(
                    conn, self._cash_date_key(portfolio_id), as_of
                )

    def _apply_import_cash_balance_delta(
        self, conn: Any, portfolio_id: int | None, delta: float
    ) -> None:
        """Add ``delta`` to the currently-stored GBP cash balance for
        ``portfolio_id`` -- unconditionally, on the caller's open ``conn``.
        Does not commit.

        Story 3.4 (review pass 2 correction, see the spec's Spec Change
        Log): used by the cash-balance fallback for a contract with no
        ``running_balance`` mapping (e.g. IG, whose export never states an
        absolute balance, only per-row deltas). This method deliberately
        **never** consults ``_cash_date_key``/the ``#160`` stale-date
        guard at all -- that guard exists to compare two *absolute
        snapshot candidates* by date and keep the newer one; it has no
        meaning for a *delta*, which is always correct to add regardless
        of what date (if any) the currently-stored value carries. Routing
        the delta through ``_apply_import_cash_balance``'s date comparison
        (review pass 1's approach, with ``as_of=None``) meant that once
        any dated running-balance import ever set a stored date for a
        portfolio, every subsequent undated delta silently failed that
        guard and was dropped forever, with no error -- a real,
        user-invisible financial-data bug. An additive delta is
        unconditionally valid to apply; gating it through a date
        comparison was the bug, not a safety net.

        Reads the existing value via ``get_on_connection`` on this same
        open ``conn`` (never a separate connection, unlike
        ``get_cash_balance()``), so it observes this same transaction's
        own prior writes and this import's read never races a concurrent
        one. A stored value that fails to parse as a number is treated as
        ``0.0`` rather than raising -- this runs inside the import's write
        transaction, so one malformed legacy value must not roll back an
        otherwise-valid import (mirrors ``_effective_cash_balance``'s own
        fallback behaviour).

        Known, bounded limitation (see ``deferred-work.md``): a single
        portfolio realistically maps to one broker account, so mixing a
        running-balance provider (II) and a non-running-balance provider
        (IG) into the *same* ``portfolio_id`` is an unusual setup this
        legacy single-value ``cash_key`` slot was never designed to
        reconcile between two independent sources of truth. This method
        guarantees the additive delta is never silently dropped -- it does
        not make the two mechanisms produce a jointly-coherent number when
        combined in one portfolio.
        """
        stored = self._account.get_on_connection(conn, self._cash_key(portfolio_id))
        try:
            current = float(stored) if stored is not None else 0.0
        except (TypeError, ValueError):
            logger.warning(
                "unparseable stored cash balance %r for portfolio %s; "
                "treating as 0.0 before applying this import's delta",
                stored,
                portfolio_id,
            )
            current = 0.0
        self._account.set_on_connection(
            conn, self._cash_key(portfolio_id), str(current + delta)
        )

    def _apply_import_cash_balances(
        self,
        conn: Any,
        portfolio_id: int | None,
        cash_balance: dict[str, Decimal],
        cash_balance_rank: dict[str, tuple[int, str, int]],
    ) -> None:
        """Persist each currency's winning Running Balance to
        ``cash_balances``, applying the #160 stale-date guard independently
        per currency (AD-24: "per-currency balance state" joins the same
        one-transaction commit as the import's trade/cash-flow writes). An
        undated incoming balance overwrites only when nothing dated is
        already stored for that currency (Story 1.5, AC3). Writes on the
        caller's open connection; does not commit.
        """
        for currency, amount in cash_balance.items():
            rank = cash_balance_rank[currency]
            as_of = rank[1] if rank[0] == 1 else None
            stored = self._cash_balances.get_on_connection(conn, portfolio_id, currency)
            stored_date = stored[1] if stored else None
            if stored_date is None or (as_of is not None and as_of >= stored_date):
                self._cash_balances.upsert_on_connection(
                    conn, portfolio_id, currency, amount, as_of
                )

    def _write_import_snapshot(
        self,
        conn: Any,
        portfolio_id: int,
        cash_balance: float,
        prices: dict[str, float],
    ) -> None:
        """Append this import's portfolio snapshot on the same open connection.

        ``prices`` is read by the caller *before* the write transaction
        opens (a filesystem read, unrelated to ``trades.db``, that should
        not extend the ``BEGIN IMMEDIATE`` lock window). Reads trades through
        ``open_rows_on_connection`` (not ``get_portfolio``/``open_rows``,
        which open their own connection) so the snapshot's
        total_cost/total_value reflect this transaction's own
        not-yet-committed trade inserts — a separate connection would only
        see the database's last *committed* state and silently miss them,
        producing a snapshot that is atomically committed but numerically
        wrong (the "read-your-own-writes hazard"). Does not commit.
        """
        rows = self._trades.open_rows_on_connection(conn, portfolio_id)
        positions = self._compute_positions(rows, prices if prices else None, None)
        total_cost = sum(p.total_cost for p in positions if p.total_cost is not None)
        total_value = sum(
            p.current_value for p in positions if p.current_value is not None
        )
        if total_value is None:
            total_value = total_cost

        timestamp = datetime.now(timezone.utc).isoformat()
        self._snapshots.append_on_connection(
            conn,
            portfolio_id,
            timestamp,
            round(total_value, 2),
            round(total_cost, 2),
            round(cash_balance, 2) if cash_balance is not None else None,
        )

    def snapshot_history(
        self, portfolio_id: int, limit: int = 180
    ) -> list[tuple[Any, ...]]:
        """Return a portfolio's value-history snapshots, oldest-first."""
        return self._snapshots.history(portfolio_id, limit)

    def list_reconciliation_issues(
        self, portfolio_id: int | None, limit: int = 200
    ) -> list[tuple[Any, ...]]:
        """Return a portfolio's recorded cash-reconciliation issues,
        newest-first (Story 1.5, AC5)."""
        return self._cash_reconciliation.list_issues(portfolio_id, limit)

    def list_cash_balances(
        self, portfolio_id: int | None
    ) -> list[tuple[str, Decimal, str | None]]:
        """Return every ``(currency, amount, as_of)`` balance for a
        portfolio (Story 1.6, Gate 3)."""
        return self._cash_balances.list_all(portfolio_id)

    def refresh_portfolio_prices(
        self,
        current_prices: dict[str, float],
        display_info: dict[str, tuple[float, str]] | None = None,
        portfolio_id: int | None = None,
    ) -> list[Position]:
        """Fetch live prices and recalculate portfolio metrics.

        Takes a dict of current prices {ticker: gbp_price} and returns updated
        Position objects with recalculated market value, unrealised P&L,
        and profit targets. Handles missing prices gracefully. Scoped to
        ``portfolio_id`` when given.
        display_info: optional {ticker: (original_price, currency)} for template display.
        """
        import logging

        logger = logging.getLogger(__name__)
        logger.info("Starting portfolio price refresh")

        rows = self._trades.open_rows(portfolio_id)
        state = self._replay_trades(rows)
        positions = []

        # Story 2.3: epsilon-tolerant (not `> 0` or a bare `!= 0`) so an
        # oversold, negative-share ticker stays visible on refresh too,
        # matching `_compute_positions`'s identical fix -- otherwise this
        # sibling replay path would still silently drop the ticker the
        # clamp-removal was meant to surface, and a bare `!= 0` would let
        # residual float dust from a long replay surface as a phantom row.
        missing_tickers = [
            ticker
            for ticker, s in state.items()
            if abs(s["shares"]) > QUANTITY_EPSILON and ticker not in current_prices
        ]
        if missing_tickers:
            logger.warning(f"Could not fetch prices for: {missing_tickers}")

        for ticker, s in state.items():
            if abs(s["shares"]) > QUANTITY_EPSILON:
                pos = self._build_position(ticker, s, current_prices, display_info)
                positions.append(pos)

        logger.info(f"Portfolio refresh complete: {len(positions)} positions updated")
        return positions

    def run(self, payload: Any = None) -> list[Position]:
        """Standalone agent interface — return current portfolio."""
        return self.get_portfolio()
