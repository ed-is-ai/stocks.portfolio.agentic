"""
Trader Agent — records buy/sell trades to SQLite and computes portfolio P&L.

Standalone agent; not part of the main scan pipeline.
Run as part of the web UI backend.
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import PrivateAttr

from app.agents.base import Agent
from app.core.config import ANALYSIS_JSON, PORTFOLIO_VALUE_CSV, TRADES_DB
from app.schemas import CashFlow, Position, SippImportResult, Trade
from app.repositories import db
from app.repositories.account_repo import AccountStateRepository
from app.repositories.artifacts_repo import ArtifactsRepository
from app.repositories.cash_flows_repo import CashFlowsRepository
from app.repositories.db import Connect
from app.repositories.portfolio_snapshots_repo import PortfolioSnapshotsRepository
from app.repositories.portfolios_repo import PortfoliosRepository
from app.repositories.price_cache_repo import PriceCacheRepository
from app.repositories.trades_repo import TradesRepository
from app.schemas.trade import Portfolio

logger = logging.getLogger(__name__)

_DB_PATH = TRADES_DB

# Columns a SIPP CSV must carry for the import to make sense (Sedol optional).
REQUIRED_SIPP_COLUMNS = (
    "Date",
    "Symbol",
    "Quantity",
    "Price",
    "Description",
    "Reference",
    "Debit",
    "Credit",
    "Running Balance",
)


class SippImportError(ValueError):
    """Raised when a SIPP CSV cannot be imported (e.g. missing columns)."""


def _to_iso_date(value: str) -> str:
    """Return ``value`` as ISO ``YYYY-MM-DD``.

    Accepts the SIPP CSV's ``DD/MM/YYYY`` and already-ISO ``YYYY-MM-DD``.
    Unrecognized formats are logged and returned unchanged so a single odd
    row never aborts an import.
    """
    value = value.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.warning("unrecognized trade date format: %r (stored unchanged)", value)
    return value


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
    _price_cache: PriceCacheRepository = PrivateAttr()
    _account: AccountStateRepository = PrivateAttr()
    _artifacts: ArtifactsRepository = PrivateAttr()
    _portfolios: PortfoliosRepository = PrivateAttr()
    _snapshots: PortfolioSnapshotsRepository = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        connect: Connect = db.make_connect(lambda: self.db_path)
        self._trades = TradesRepository(connect)
        self._cash_flows = CashFlowsRepository(connect)
        self._price_cache = PriceCacheRepository(connect)
        self._account = AccountStateRepository(connect)
        self._artifacts = ArtifactsRepository()
        self._portfolios = PortfoliosRepository(connect)
        self._snapshots = PortfolioSnapshotsRepository(connect)
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
    ) -> Trade:
        """Record a buy transaction and return the saved Trade."""
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
        )

    def record_sell(
        self,
        ticker: str,
        shares: float,
        price: float,
        date: str | None = None,
        notes: str = "",
        portfolio_id: int | None = None,
    ) -> Trade:
        """Record a sell transaction and return the saved Trade."""
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
        touches the same ticker held in another.
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
        )

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a trade by ID. Returns True if a row was deleted."""
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
        state = self._replay_trades(rows)
        return [
            self._build_position(ticker, s, current_prices, display_info)
            for ticker, s in state.items()
            if s["shares"] > 0
        ]

    @staticmethod
    def _replay_trades(
        rows: list[tuple[Any, ...]],
    ) -> dict[str, dict[str, Any]]:
        """Replay trade rows to derive per-ticker running state."""
        state: dict[str, dict[str, Any]] = {}
        for ticker, action, shares, price, date, stop_loss, entry_price in rows:
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
                new_total = s["shares"] + shares
                s["avg_cost"] = (
                    s["avg_cost"] * s["shares"] + price * shares
                ) / new_total
                s["shares"] = new_total
                if s["entry_date"] is None:
                    s["entry_date"] = date
                if stop_loss is not None:
                    s["stop_loss"] = stop_loss
                if entry_price is not None:
                    s["entry_price"] = entry_price
            else:  # SELL
                if shares > s["shares"]:
                    logging.getLogger(__name__).warning(
                        "Oversell for %s: sold %s but only %s held; clamping to 0",
                        ticker,
                        shares,
                        s["shares"],
                    )
                s["shares"] = max(0.0, s["shares"] - shares)
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
        """
        avg_cost = s["avg_cost"]
        remaining = s["shares"]
        total_cost = round(avg_cost * remaining, 2)

        disp = display_info.get(ticker) if display_info else None
        currency = disp[1] if disp else "GBP"
        is_usd = currency == "USD"

        if is_usd and disp is not None:
            # Use original USD price so cost (USD) and value (USD) are comparable
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
            shares=round(remaining, 4),
            avg_cost=round(avg_cost, 4),
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
        self, csv_path: str | Path, portfolio_id: int | None = None
    ) -> SippImportResult:
        """Import a SIPP CSV into ``portfolio_id``; return counts and problems.

        Validates the header row first (missing required columns abort the
        import with ``SippImportError`` before any DB write). Unparseable
        numeric values and skipped trade rows are collected and reported rather
        than silently swallowed (#152). Idempotency is keyed per-portfolio, so
        the same CSV imported into two portfolios does not collide (#147).
        """
        csv_path = Path(csv_path)
        parse_errors: list[str] = []
        skipped_rows: list[str] = []

        def clean_amount(s: str | None, field: str = "", ref: str = "") -> float:
            if not s or s.strip() in ("", "n/a"):
                return 0.0
            cleaned = (
                s.replace("﻿", "")
                .replace("£", "")
                .replace(",", "")
                .replace('"', "")
                .strip()
            )
            try:
                return float(cleaned)
            except ValueError:
                parse_errors.append(
                    f"row {ref or '?'}: unparseable {field or 'amount'} {s.strip()!r}"
                )
                return 0.0

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

        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        present = {(h or "").replace("﻿", "").strip() for h in fieldnames}
        missing = [c for c in REQUIRED_SIPP_COLUMNS if c not in present]
        if missing:
            raise SippImportError(
                "CSV is missing required columns: " + ", ".join(missing)
            )

        conn = self._conn()
        buy_count, sell_count, cash_count, cash_balance = 0, 0, 0, 0.0
        # The authoritative cash balance is the running balance of the
        # latest-dated row, chosen independently of the CSV's row order so a
        # newest-first export yields the same balance as an oldest-first one
        # (#158). Rank = (has-ISO-date, iso-date, file-index): an ISO-dated row
        # always beats an unparseable-date row, later dates win, and ties fall
        # to the last such row in the file. With no parseable dates this
        # degrades to the previous last-row-in-file behaviour.
        cash_balance_rank: tuple[int, str, int] | None = None

        try:
            for idx, row in enumerate(rows):
                qty = row.get("Quantity", "").strip()
                symbol = row.get("Symbol", "").strip()
                reference = row.get("Reference", "").strip()
                debit = clean_amount(row.get("Debit", ""), "Debit", reference)
                credit = clean_amount(row.get("Credit", ""), "Credit", reference)
                price = clean_amount(row.get("Price", ""), "Price", reference)
                date = _to_iso_date(row.get("Date", "").strip())
                description = row.get("Description", "").strip()

                if qty and qty != "n/a":
                    # Trade if Symbol is valid or if HSBC GLOB
                    is_trade = symbol and symbol != "n/a"
                    is_hsbc_glob = "HSBC GLOB" in description.upper()

                    if is_trade or is_hsbc_glob:
                        try:
                            shares = float(qty.replace("£", "").replace(",", ""))
                        except ValueError:
                            skipped_rows.append(
                                f"row {reference or '?'}: unparseable quantity {qty!r}"
                            )
                            logger.warning(
                                "skipping trade row with unparseable quantity %r (ref %s)",
                                qty,
                                reference or "",
                            )
                            shares = None

                        if shares is not None:
                            if shares <= 0 or price <= 0:
                                skipped_rows.append(
                                    f"row {reference or '?'}: non-positive "
                                    f"shares/price ({shares}, {price})"
                                )
                            else:
                                action = (
                                    "BUY"
                                    if debit > 0
                                    else "SELL"
                                    if credit > 0
                                    else None
                                )
                                if action:
                                    ticker = symbol.upper() if is_trade else "HSFWA"
                                    self._trades.insert_ignore(
                                        conn,
                                        ticker,
                                        action,
                                        shares,
                                        price,
                                        date,
                                        "",
                                        reference or None,
                                        portfolio_id,
                                    )
                                    if action == "BUY":
                                        buy_count += 1
                                    else:
                                        sell_count += 1
                                else:
                                    skipped_rows.append(
                                        f"row {reference or '?'}: trade with no "
                                        "debit/credit amount"
                                    )
                    else:
                        amount = credit if credit > 0 else debit
                        if amount > 0:
                            flow_type = classify_flow_type(description)
                            self._cash_flows.insert_ignore(
                                conn,
                                date,
                                flow_type,
                                None,
                                amount,
                                description,
                                reference,
                                portfolio_id,
                            )
                            cash_count += 1
                else:
                    if symbol and symbol != "n/a":
                        continue
                    amount = credit if credit > 0 else debit
                    if amount > 0:
                        flow_type = classify_flow_type(description)
                        self._cash_flows.insert_ignore(
                            conn,
                            date,
                            flow_type,
                            None,
                            amount,
                            description,
                            reference,
                            portfolio_id,
                        )
                        cash_count += 1

                running_balance = row.get("Running Balance", "").strip()
                if running_balance and running_balance != "n/a":
                    rb = clean_amount(running_balance, "Running Balance", reference)
                    if rb > 0:
                        is_iso = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date))
                        rank = (1 if is_iso else 0, date if is_iso else "", idx)
                        if cash_balance_rank is None or rank > cash_balance_rank:
                            cash_balance_rank = rank
                            cash_balance = rb

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if cash_balance > 0:
            # The winning row's date (its ISO date, or None if unparseable).
            cash_asof = (
                cash_balance_rank[1]
                if cash_balance_rank is not None and cash_balance_rank[0] == 1
                else None
            )
            self._apply_import_cash_balance(portfolio_id, cash_balance, cash_asof)
        self.update_portfolio_snapshot(cash_balance, portfolio_id)

        print(
            f"SIPP import complete: {buy_count} BUY, {sell_count} SELL, "
            f"{cash_count} cash flows, {len(skipped_rows)} skipped, "
            f"{len(parse_errors)} parse errors. "
            f"Cash balance: GBP {cash_balance:,.2f}"
        )
        return SippImportResult(
            cash_balance=cash_balance,
            buy_count=buy_count,
            sell_count=sell_count,
            cash_flow_count=cash_count,
            skipped_rows=skipped_rows,
            parse_errors=parse_errors,
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

    def _apply_import_cash_balance(
        self, portfolio_id: int | None, amount: float, as_of: str | None
    ) -> None:
        """Store an imported cash balance only if it's not older than the one
        already stored (#160).

        The provider Running Balance is a snapshot, so importing an *older*
        SIPP file after a newer one must not regress the balance to a stale
        date. We persist the balance's as-of date alongside it and only replace
        when the incoming date is newer-or-equal (ties → the newer import wins).
        An incoming balance with no parseable date can't be compared, so it
        overwrites — matching the pre-#160 behaviour for undated files.
        """
        stored_date = self._account.get(self._cash_date_key(portfolio_id))
        if as_of is None or stored_date is None or as_of >= stored_date:
            self.set_cash_balance(amount, portfolio_id)
            if as_of is not None:
                self._account.set(self._cash_date_key(portfolio_id), as_of)

    def snapshot_history(
        self, portfolio_id: int, limit: int = 180
    ) -> list[tuple[Any, ...]]:
        """Return a portfolio's value-history snapshots, oldest-first."""
        return self._snapshots.history(portfolio_id, limit)

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

        missing_tickers = [
            ticker
            for ticker, s in state.items()
            if s["shares"] > 0 and ticker not in current_prices
        ]
        if missing_tickers:
            logger.warning(f"Could not fetch prices for: {missing_tickers}")

        for ticker, s in state.items():
            if s["shares"] > 0:
                pos = self._build_position(ticker, s, current_prices, display_info)
                positions.append(pos)

        logger.info(f"Portfolio refresh complete: {len(positions)} positions updated")
        return positions

    def run(self, payload: Any = None) -> list[Position]:
        """Standalone agent interface — return current portfolio."""
        return self.get_portfolio()
