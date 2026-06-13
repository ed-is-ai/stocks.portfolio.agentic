"""Patch portfolio_value.csv rows with historical cash balances from SIPP CSV.

For each row in portfolio_value.csv, looks up the most recent Running Balance
on or before that date and updates cash_balance and total_value accordingly.

Run from project root:
    python patch_cash_history.py
"""

import csv
import io
from datetime import datetime
from pathlib import Path

CSV_PATH = Path("portfolio_value.csv")
SIPP_CSV = Path("data/processed/SIPP/7f2f2de8-52c5-4348-bc27-4e92f61bce1e.csv")
FIELDNAMES = ["timestamp", "total_value", "total_cost", "cash_balance", "investments_value"]

# ---------------------------------------------------------------------------
# Load cash history from SIPP CSV
# ---------------------------------------------------------------------------

def load_cash_history(sipp_path: Path) -> dict[str, float]:
    with open(sipp_path, encoding="utf-8-sig") as f:
        content = f.read()
    clean = content[content.index("Date,"):]
    reader = csv.DictReader(io.StringIO(clean))
    cash_by_date: dict[str, float] = {}
    for row in reader:
        date_str = row.get("Date", "").strip()
        balance_str = row.get("Running Balance", "").strip()
        if not date_str or not balance_str or balance_str == "n/a":
            continue
        try:
            d = datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
            val = float(balance_str.replace("£", "").replace(",", "").strip())
            cash_by_date[d] = val
        except ValueError:
            pass
    return cash_by_date


def get_cash(cash_by_date: dict[str, float], week_str: str, fallback: float) -> float:
    """Most recent cash balance on or before week_str."""
    best = fallback
    for d_str, balance in sorted(cash_by_date.items()):
        if d_str <= week_str:
            best = balance
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Patch rows
# ---------------------------------------------------------------------------

cash_by_date = load_cash_history(SIPP_CSV)
print(f"Loaded {len(cash_by_date)} cash dates from {SIPP_CSV.name}")
print(f"  Range: {min(cash_by_date)} to {max(cash_by_date)}")

with open(CSV_PATH, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

print(f"Patching {len(rows)} rows in {CSV_PATH}...")

patched = 0
for row in rows:
    ts = row["timestamp"][:10]
    investments_value = float(row.get("investments_value") or row.get("total_value") or 0)
    current_cash = float(row.get("cash_balance") or 0)
    new_cash = get_cash(cash_by_date, ts, current_cash)
    if abs(new_cash - current_cash) > 0.01:
        row["cash_balance"] = f"{new_cash:.2f}"
        row["total_value"] = f"{investments_value + new_cash:.2f}"
        patched += 1

print(f"Updated {patched} rows with new cash values")

with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

print("Done.")
