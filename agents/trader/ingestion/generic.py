"""Generic configurable ingestor for portfolios with non-standard CSV formats (e.g. LISA).

Driven by a config.json in the staging folder.  If config.json is absent,
ingest_file returns an error rather than silently failing.

config.json schema:
{
  "date_col":    "Settlement Date",   // column name for trade date
  "action_col":  "Transaction Type",  // column name for BUY/SELL indicator
  "ticker_col":  "Symbol",            // column name for ticker symbol
  "shares_col":  "Quantity",          // column name for number of shares
  "price_col":   "Price",             // column name for price per share
  "buy_value":   "Buy",               // value in action_col that means BUY
  "sell_value":  "Sell",              // value in action_col that means SELL
  "currency":    "GBP"                // native currency of prices
}
"""

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agents.trader.ingestion.base import IngestResult, IngestorBase

if TYPE_CHECKING:
    from agents.trader.trader_agent import TraderAgent

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y")
_REQUIRED_KEYS = {
    "date_col", "action_col", "ticker_col",
    "shares_col", "price_col", "buy_value", "sell_value",
}


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _strip_currency(v: str) -> float | None:
    if not v or v.strip().lower() in ("n/a", ""):
        return None
    cleaned = v.replace("£", "").replace("$", "").replace(",", "").replace("\xa3", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


class GenericIngestor(IngestorBase):
    """Configurable ingestor for portfolios with custom CSV formats."""

    def __init__(self, portfolio: str) -> None:
        self.portfolio = portfolio

    def _load_config(self) -> dict[str, str] | None:
        cfg_path = self.staging_dir / "config.json"
        if not cfg_path.exists():
            return None
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def ingest_file(self, path: Path, trader: "TraderAgent") -> IngestResult:
        cfg = self._load_config()
        if cfg is None:
            return IngestResult(
                path.name, self.portfolio, 0, 0,
                errors=[
                    f"config.json not found in {self.staging_dir}. "
                    "Create it with column mappings before ingesting."
                ],
            )
        missing = _REQUIRED_KEYS - set(cfg)
        if missing:
            return IngestResult(
                path.name, self.portfolio, 0, 0,
                errors=[f"config.json missing required keys: {sorted(missing)}"],
            )

        errors: list[str] = []
        trades_imported = 0

        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(raw))
            rows = list(reader)
        except Exception as exc:
            return IngestResult(path.name, self.portfolio, 0, 0, [str(exc)])

        for line_num, row in enumerate(rows, 2):
            date_str = _parse_date(row.get(cfg["date_col"], ""))
            if date_str is None:
                errors.append(f"Line {line_num}: unparseable date '{row.get(cfg['date_col'])}'")
                continue

            action_raw = row.get(cfg["action_col"], "").strip()
            if action_raw == cfg["buy_value"]:
                action = "BUY"
            elif action_raw == cfg["sell_value"]:
                action = "SELL"
            else:
                continue  # skip non-trade rows (dividends, fees, etc.)

            ticker = row.get(cfg["ticker_col"], "").strip().upper()
            if not ticker:
                errors.append(f"Line {line_num}: missing ticker")
                continue

            shares = _strip_currency(row.get(cfg["shares_col"], ""))
            price = _strip_currency(row.get(cfg["price_col"], ""))
            if shares is None or price is None:
                errors.append(f"Line {line_num}: bad shares/price for {ticker}")
                continue

            if action == "BUY":
                trader.record_buy(ticker, shares, price, date_str, portfolio=self.portfolio)
            else:
                trader.record_sell(ticker, shares, price, date_str, portfolio=self.portfolio)
            trades_imported += 1

        return IngestResult(
            file=path.name,
            portfolio=self.portfolio,
            trades_imported=trades_imported,
            value_rows_imported=0,
            errors=errors,
        )
