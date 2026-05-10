"""SIPP ingestor — parses AJ Bell brokerage CSV transaction files.

CSV columns: Date, Settlement Date, Symbol, Sedol, Quantity, Price, Description,
             Reference, Debit, Credit, Running Balance

Debit populated  → BUY  (cash leaves, shares arrive)
Credit populated → SELL (cash arrives, shares leave)
Rows where Quantity or Price == 'n/a' are non-trade events (dividends, cash, fees).
"""

import csv
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agents.trader.ingestion.base import IngestResult, IngestorBase

if TYPE_CHECKING:
    from agents.trader.trader_agent import TraderAgent

# Pre-CSV positions held before the brokerage CSV begins (May 2024).
# Format: (ticker, shares, avg_price_gbp, date_str)
_PRE_CSV_HOLDINGS: list[tuple[str, float, float, str]] = [
    ("RIO",  41.0,  45.00, "2022-01-01"),
    ("SMT",  554.0, 10.00, "2022-01-01"),
    ("INTC", 168.0, 32.00, "2022-02-07"),
    ("NWG",  2085.0, 5.00, "2022-01-01"),
    ("AZN",   25.0, 100.00, "2022-01-01"),
    ("ASHM", 648.0,  2.00, "2022-01-01"),
    ("DPZ",   10.0, 350.00, "2022-01-01"),
    ("GOOGL", 30.0, 150.00, "2022-01-01"),
    ("ONON",  75.0,  40.00, "2022-01-01"),
    ("META",  24.0, 500.00, "2022-01-01"),
    ("AMZN",  20.0, 180.00, "2022-01-01"),
    ("DIS",   25.0,  90.00, "2022-01-01"),
    ("VEIL", 1337.0, 6.00, "2022-01-01"),
    ("PYPL",  30.0,  70.00, "2022-01-01"),
    ("LULU",  10.0, 200.00, "2022-01-01"),
]


def _strip_currency(v: str) -> float | None:
    if not v or v.strip().lower() == "n/a":
        return None
    cleaned = v.replace("£", "").replace(",", "").replace("\xa3", "").replace("​", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_raw(path: Path) -> list[dict[str, str]]:
    """Read and normalise an AJ Bell CSV file; returns rows in chronological order."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("Â£", "£").replace("﻿", "")
    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    rows.reverse()  # file is newest-first; flip to chronological
    return rows


def _is_trade(row: dict[str, str]) -> bool:
    qty = row.get("Quantity", "n/a").strip().lower()
    price = row.get("Price", "n/a").strip().lower()
    return qty != "n/a" and price != "n/a"


def _cash_event_amount(row: dict[str, str]) -> float | None:
    desc = row.get("Description", "").strip().upper()
    credit = _strip_currency(row.get("Credit", "n/a"))
    if credit is None:
        return None
    if (
        "INTEREST" in desc
        or "CONTRIBUTION" in desc
        or "TAX RELIEF" in desc
        or "RELIEF" in desc
    ):
        return credit
    return None


class SIPPIngestor(IngestorBase):
    """Ingest AJ Bell SIPP brokerage CSV files from data/staging/SIPP/."""

    portfolio = "SIPP"

    def ingest_file(self, path: Path, trader: "TraderAgent") -> IngestResult:
        errors: list[str] = []
        try:
            rows = _parse_raw(path)
        except Exception as exc:
            return IngestResult(path.name, self.portfolio, 0, 0, [str(exc)])

        # --- Extract trade rows and running balance ---
        csv_trades: list[tuple[str, str, float, float, str]] = []  # (ticker, action, qty, price, date)
        balance_rows: list[dict[str, str]] = []
        csv_tickers: set[str] = set()

        for row in rows:
            # Running balance for portfolio_value.csv
            bal = _strip_currency(row.get("Running Balance", "n/a"))
            raw_date = row.get("Date", "").strip()
            if bal is not None and raw_date:
                try:
                    iso_date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                    balance_rows.append({
                        "timestamp": f"{iso_date}T00:00",
                        "portfolio": self.portfolio,
                        "total_value": str(round(bal, 2)),
                        "total_cost": "0",
                    })
                except ValueError:
                    pass

            if not _is_trade(row):
                cash_amount = _cash_event_amount(row)
                if cash_amount is not None:
                    try:
                        date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                        csv_trades.append(("CASH", "BUY", cash_amount, 1.0, date))
                        csv_tickers.add("CASH")
                    except ValueError:
                        errors.append(f"Bad date '{raw_date}'")
                continue

            sym = row.get("Symbol", "n/a").strip()
            if not sym or sym.lower() == "n/a":
                continue

            qty_str = row.get("Quantity", "").strip()
            price_str = row.get("Price", "").strip()
            try:
                qty = float(qty_str)
            except ValueError:
                errors.append(f"Bad quantity '{qty_str}' for {sym}")
                continue

            price = _strip_currency(price_str)
            if price is None:
                errors.append(f"Bad price '{price_str}' for {sym}")
                continue

            debit = _strip_currency(row.get("Debit", "n/a"))
            credit = _strip_currency(row.get("Credit", "n/a"))

            if debit is not None and credit is None:
                action = "BUY"
            elif credit is not None and debit is None:
                action = "SELL"
            elif debit is not None and credit is not None:
                if debit == 0.0 and credit > 0.0:
                    action = "SELL"
                elif credit == 0.0 and debit > 0.0:
                    action = "BUY"
                else:
                    continue  # ambiguous row with both sides populated
            else:
                continue  # ambiguous row

            try:
                date = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                errors.append(f"Bad date '{raw_date}'")
                continue

            csv_trades.append((sym.upper(), action, qty, price, date))
            csv_tickers.add(sym.upper())
            if action == "SELL":
                csv_tickers.add("CASH")

        # --- Determine which tickers appear in the CSV ---
        csv_tickers |= {t for t, *_ in csv_trades}

        # --- For each CSV ticker: wipe SIPP trades and re-import ---
        # This makes the ingestor idempotent (safe to run again with same file).
        trades_imported = 0
        for ticker in csv_tickers:
            # Delete existing SIPP trades for this ticker
            with trader._conn() as conn:
                conn.execute(
                    "DELETE FROM trades WHERE ticker = ? AND portfolio = ?",
                    (ticker, self.portfolio),
                )
                conn.commit()

            # Re-insert pre-CSV holding if applicable
            pre = {t: (s, p, d) for t, s, p, d in _PRE_CSV_HOLDINGS}
            if ticker in pre:
                shares, price_val, date_val = pre[ticker]
                trader.record_buy(
                    ticker, shares, price_val, date_val,
                    notes="pre-CSV holding",
                    portfolio=self.portfolio,
                )
                trades_imported += 1

        # Insert all CSV transactions in chronological order
        for ticker, action, qty, price_val, date in csv_trades:
            if action == "BUY":
                trader.record_buy(ticker, qty, price_val, date, portfolio=self.portfolio)
            else:
                trader.record_sell(ticker, qty, price_val, date, portfolio=self.portfolio)
            trades_imported += 1

        # --- Set cash from most recent AJ Bell running balance ---
        # The Running Balance column is the brokerage cash balance (not total portfolio).
        # It is the authoritative source; overwrite any previously stored CASH position.
        if balance_rows:
            latest_cash = float(balance_rows[-1]["total_value"])
            trader.set_cash(latest_cash, self.portfolio)

        # --- Append running balance rows to portfolio_value.csv ---
        value_rows_added = self._append_value_rows(balance_rows)

        return IngestResult(
            file=path.name,
            portfolio=self.portfolio,
            trades_imported=trades_imported,
            value_rows_imported=value_rows_added,
            errors=errors,
        )


if __name__ == "__main__":
    # Allow running standalone for testing:
    # python -m agents.trader.ingestion.sipp <csv_file>
    _root = Path(__file__).parent.parent.parent.parent
    sys.path.insert(0, str(_root))
    from agents.trader.trader_agent import TraderAgent as _TA

    _path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if _path:
        result = SIPPIngestor().ingest_file(_path, _TA(name="TraderAgent"))
        print(result)
