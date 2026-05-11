"""Backfill portfolio_value.csv with weekly equity + cash history.

Replays all trades to compute holdings at each Sunday, fetches weekly
closing prices from yfinance, converts USD/GBp to GBP, and prepends
rows to portfolio_value.csv without duplicating existing dates.

Cash is set to the current stored balance as a flat line — no historical
cash data is available.

Run from project root:
    python backfill_portfolio_weekly.py
"""

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path("agents/trader/trades.db")
CSV_PATH = Path("portfolio_value.csv")
ALIASES_PATH = Path("data/ticker_aliases.json")
FIELDNAMES = ["timestamp", "total_value", "total_cost", "cash_balance", "investments_value"]

# SIPP CSV for historical cash balances — set to None to use stored balance only
SIPP_CSV = next(Path("data/processed/SIPP").glob("*.csv"), None) if Path("data/processed/SIPP").exists() else None

# ---------------------------------------------------------------------------
# Load trades
# ---------------------------------------------------------------------------

conn = sqlite3.connect(DB_PATH)
raw_trades = conn.execute(
    "SELECT date, ticker, action, shares, price FROM trades ORDER BY date, rowid"
).fetchall()
cash_row = conn.execute(
    "SELECT value FROM account_state WHERE key='cash_balance'"
).fetchone()
conn.close()

CASH_BALANCE = float(cash_row[0]) if cash_row else 0.0

# ---------------------------------------------------------------------------
# Load historical cash balances from SIPP CSV (Running Balance column)
# ---------------------------------------------------------------------------

def _clean_amount(s: str) -> float | None:
    if not s or s.strip() in ("", "n/a"):
        return None
    return float(s.replace("£", "").replace(",", "").replace("​", "").strip())

# cash_by_date: maps YYYY-MM-DD -> cash balance on that date (from Running Balance)
cash_by_date: dict[str, float] = {}

if SIPP_CSV:
    print(f"Loading cash history from {SIPP_CSV.name}...")
    with open(SIPP_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("Date", "").strip()
            balance_str = row.get("Running Balance", "").strip()
            if not date_str or not balance_str:
                continue
            try:
                d = datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
                val = _clean_amount(balance_str)
                if val is not None:
                    cash_by_date[d] = val
            except ValueError:
                pass
    print(f"  Loaded {len(cash_by_date)} cash balance dates")

def get_cash_for_week(week_str: str) -> float:
    """Return the most recent known cash balance on or before week_str."""
    if not cash_by_date:
        return CASH_BALANCE
    best = CASH_BALANCE
    for d_str, balance in sorted(cash_by_date.items()):
        if d_str <= week_str:
            best = balance
        else:
            break
    return best

# Parse DD/MM/YYYY dates
def parse_date(s: str) -> date:
    d, m, y = s.split("/")
    return date(int(y), int(m), int(d))

trades: list[tuple[date, str, str, float, float]] = []
for raw_date, ticker, action, shares, price in raw_trades:
    try:
        trades.append((parse_date(raw_date), ticker, action, float(shares), float(price)))
    except ValueError:
        print(f"Skipping unparseable date: {raw_date}")

if not trades:
    print("No trades found.")
    exit(0)

first_trade_date = trades[0][0]
today = date.today()

# ---------------------------------------------------------------------------
# Build weekly date range (Mondays)
# ---------------------------------------------------------------------------

def next_monday(d: date) -> date:
    days_ahead = 7 - d.weekday() if d.weekday() != 0 else 7
    return d + timedelta(days=days_ahead)

weeks: list[date] = []
w = first_trade_date + timedelta(days=(7 - first_trade_date.weekday()) % 7)  # first Monday after first trade
while w <= today:
    weeks.append(w)
    w += timedelta(days=7)
if not weeks or weeks[-1] < today:
    weeks.append(today)

print(f"Date range: {weeks[0]} to {weeks[-1]} ({len(weeks)} weeks)")

# ---------------------------------------------------------------------------
# Load existing CSV dates to avoid duplicates
# ---------------------------------------------------------------------------

existing_dates: set[str] = set()
existing_rows: list[dict] = []
if CSV_PATH.exists():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing_rows.append(row)
            ts = row.get("timestamp", "")[:10]
            existing_dates.add(ts)

print(f"Existing rows: {len(existing_rows)}, existing dates: {len(existing_dates)}")

# ---------------------------------------------------------------------------
# Determine which tickers need prices
# ---------------------------------------------------------------------------

aliases: dict[str, str] = {}
if ALIASES_PATH.exists():
    aliases = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))

all_tickers = sorted({t for _, t, _, _, _ in trades})
print(f"Tickers to resolve: {len(all_tickers)}")

def yf_symbol(ticker: str) -> str:
    if ticker in aliases:
        return aliases[ticker]
    # UK stocks that need .L suffix heuristic: short codes, not standard US tickers
    return ticker

# ---------------------------------------------------------------------------
# Fetch historical weekly closes from yfinance (batch)
# ---------------------------------------------------------------------------

start_str = weeks[0].strftime("%Y-%m-%d")
end_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")

print(f"Fetching historical prices from {start_str} to {end_str}...")

# Try US symbols first as a batch
us_symbols = [yf_symbol(t) for t in all_tickers]
raw_prices = yf.download(
    us_symbols,
    start=start_str,
    end=end_str,
    interval="1wk",
    progress=True,
    auto_adjust=True,
)

# Also fetch GBPUSD history
print("Fetching GBPUSD history...")
fx_data = yf.download("GBPUSD=X", start=start_str, end=end_str, interval="1wk", progress=False, auto_adjust=True)
gbpusd_history: dict[str, float] = {}
if not fx_data.empty:
    close = fx_data["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    for dt, val in close.items():
        if val and val == val:  # not NaN
            gbpusd_history[str(dt.date())] = float(val)
print(f"  GBPUSD history: {len(gbpusd_history)} weeks")

def get_gbpusd(week_str: str) -> float:
    """Return closest available GBPUSD rate on or before week_str."""
    best = 1.25
    for d_str, rate in sorted(gbpusd_history.items()):
        if d_str <= week_str:
            best = rate
        else:
            break
    return best

# Parse raw_prices into per-ticker weekly close dict
# raw_prices["Close"] is a DataFrame with tickers as columns (multi-index download)
ticker_weekly: dict[str, dict[str, float]] = defaultdict(dict)

if not raw_prices.empty:
    close_df = raw_prices["Close"] if "Close" in raw_prices.columns else raw_prices
    if hasattr(close_df, "columns"):
        for col in close_df.columns:
            series = close_df[col].dropna()
            for dt, val in series.items():
                if val and val == val:
                    ticker_weekly[str(col)][str(dt.date())] = float(val)

print(f"  Price data fetched for {len(ticker_weekly)} symbols")

# For tickers that failed, retry with .L suffix
failed = [t for t in all_tickers if yf_symbol(t) not in ticker_weekly or not ticker_weekly[yf_symbol(t)]]
if failed:
    print(f"  Retrying {len(failed)} tickers with .L suffix...")
    retry_syms = [f"{t}.L" for t in failed]
    retry_data = yf.download(retry_syms, start=start_str, end=end_str, interval="1wk", progress=False, auto_adjust=True)
    if not retry_data.empty:
        close_df2 = retry_data["Close"] if "Close" in retry_data.columns else retry_data
        if hasattr(close_df2, "columns"):
            for col in close_df2.columns:
                series = close_df2[col].dropna()
                base = col.replace(".L", "")
                for dt, val in series.items():
                    if val and val == val:
                        ticker_weekly[col][str(dt.date())] = float(val)

def get_price(ticker: str, week_str: str) -> tuple[float | None, str]:
    """Return (price, currency) for ticker at or before week_str."""
    sym = yf_symbol(ticker)
    for sym_try in [sym, f"{ticker}.L"]:
        data = ticker_weekly.get(sym_try, {})
        if not data:
            continue
        best_date, best_price = None, None
        for d_str, price in sorted(data.items()):
            if d_str <= week_str:
                best_date, best_price = d_str, price
            else:
                break
        if best_price is not None:
            # Detect currency from symbol suffix
            if sym_try.endswith(".L"):
                return best_price, "GBp"
            return best_price, "USD"
    return None, "USD"

# ---------------------------------------------------------------------------
# Replay trades weekly
# ---------------------------------------------------------------------------

print("Replaying trades and computing weekly snapshots...")

# holdings[ticker] = {"shares": float, "total_cost_usd": float}
holdings: dict[str, dict] = defaultdict(lambda: {"shares": 0.0, "total_cost": 0.0})
trade_idx = 0
new_rows: list[dict] = []

for week in weeks:
    week_str = week.strftime("%Y-%m-%d")

    # Apply all trades up to and including this week
    while trade_idx < len(trades) and trades[trade_idx][0] <= week:
        _, ticker, action, shares, price = trades[trade_idx]
        if action == "BUY":
            holdings[ticker]["shares"] += shares
            holdings[ticker]["total_cost"] += price * shares
        else:
            if holdings[ticker]["shares"] > 0:
                avg = holdings[ticker]["total_cost"] / holdings[ticker]["shares"]
                holdings[ticker]["shares"] = max(0.0, holdings[ticker]["shares"] - shares)
                holdings[ticker]["total_cost"] = avg * holdings[ticker]["shares"]
        trade_idx += 1

    if week_str in existing_dates:
        continue  # skip dates already in CSV

    gbpusd = get_gbpusd(week_str)
    total_value_gbp = 0.0
    total_cost_gbp = 0.0

    for ticker, state in holdings.items():
        if state["shares"] <= 0:
            continue
        price, currency = get_price(ticker, week_str)
        if price is None:
            # Fall back to cost basis
            cost_per_share = state["total_cost"] / state["shares"] if state["shares"] > 0 else 0
            price, currency = cost_per_share, "USD"

        # Convert to GBP
        if currency == "GBp":
            gbp_price = price / 100
            cost_per_share = state["total_cost"] / state["shares"]
            gbp_cost = (cost_per_share / 100) * state["shares"]
        elif currency == "USD":
            gbp_price = price / gbpusd
            cost_per_share = state["total_cost"] / state["shares"]
            gbp_cost = (cost_per_share / gbpusd) * state["shares"]
        else:
            gbp_price = price
            cost_per_share = state["total_cost"] / state["shares"]
            gbp_cost = cost_per_share * state["shares"]

        total_value_gbp += gbp_price * state["shares"]
        total_cost_gbp += gbp_cost

    if total_cost_gbp > 0:
        cash = get_cash_for_week(week_str)
        new_rows.append({
            "timestamp": f"{week_str}T00:00:00+00:00",
            "total_value": f"{total_value_gbp + cash:.2f}",
            "total_cost": f"{total_cost_gbp:.2f}",
            "cash_balance": f"{cash:.2f}",
            "investments_value": f"{total_value_gbp:.2f}",
        })

print(f"Generated {len(new_rows)} new historical rows")

if not new_rows:
    print("Nothing new to write.")
    exit(0)

# ---------------------------------------------------------------------------
# Merge and write
# ---------------------------------------------------------------------------

all_rows = new_rows + existing_rows
all_rows.sort(key=lambda r: r["timestamp"])

with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(all_rows)

print(f"Wrote {len(all_rows)} total rows to {CSV_PATH}")
print(f"  Historical: {len(new_rows)} rows from {new_rows[0]['timestamp'][:10]} to {new_rows[-1]['timestamp'][:10]}")
