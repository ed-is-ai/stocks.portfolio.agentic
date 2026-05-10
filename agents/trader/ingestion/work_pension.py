"""Work Pension ingestor — simple date,value CSV (lump-sum, no holdings).

Expected CSV format (any two-column file with date + value):
    date,value
    2025-01-31,42500.00
    2025-02-28,43100.00

The first column is treated as the date (DD/MM/YYYY or YYYY-MM-DD).
The second column is the total portfolio value in GBP.
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agents.trader.ingestion.base import IngestResult, IngestorBase

if TYPE_CHECKING:
    from agents.trader.trader_agent import TraderAgent

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")


def _parse_date(raw: str) -> str | None:
    """Return ISO date string or None if unparseable."""
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


class WorkPensionIngestor(IngestorBase):
    """Ingest Work Pension statement CSVs from data/staging/WorkPension/."""

    portfolio = "WorkPension"

    def ingest_file(self, path: Path, trader: "TraderAgent") -> IngestResult:
        errors: list[str] = []
        balance_rows: list[dict[str, str]] = []

        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                content = fh.read()
        except Exception as exc:
            return IngestResult(path.name, self.portfolio, 0, 0, [str(exc)])

        reader = csv.reader(content.splitlines())
        for line_num, row in enumerate(reader, 1):
            if not row or len(row) < 2:
                continue
            date_str = _parse_date(row[0])
            if date_str is None:
                if line_num == 1:
                    continue  # header row
                errors.append(f"Line {line_num}: unparseable date '{row[0]}'")
                continue
            val_str = row[1].replace("£", "").replace(",", "").strip()
            try:
                value = float(val_str)
            except ValueError:
                errors.append(f"Line {line_num}: unparseable value '{row[1]}'")
                continue
            balance_rows.append({
                "timestamp": f"{date_str}T00:00",
                "portfolio": self.portfolio,
                "total_value": str(round(value, 2)),
                "total_cost": "0",
            })

        value_rows_added = self._append_value_rows(balance_rows)
        return IngestResult(
            file=path.name,
            portfolio=self.portfolio,
            trades_imported=0,
            value_rows_imported=value_rows_added,
            errors=errors,
        )
