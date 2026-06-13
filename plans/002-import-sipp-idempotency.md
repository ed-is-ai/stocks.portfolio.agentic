# Plan 002: Make `import_sipp` idempotent (no duplicate trades on re-import)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat f823039..HEAD -- agents/trader/trader_agent.py tests/test_trader_agent.py`
> If either file changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (touches the money path — guarded by a new regression test + a schema migration)
- **Depends on**: plans/001-restore-green-test-suite.md (need a green suite to verify against)
- **Category**: bug
- **Planned at**: commit `f823039`, 2026-06-13

## Why this matters

`TraderAgent.import_sipp()` inserts every CSV trade row with a plain
`INSERT INTO trades (...)` and never clears or de-duplicates first. Re-running an
import — which the CLAUDE.md quarterly-update workflow explicitly invites ("Run
the import ... quarterly") — **doubles every position**, corrupting share counts,
average cost basis, total cost, and the portfolio-value chart. The cash-flow side
of the same function is already protected (`cash_flows.reference` is `UNIQUE` and
inserts use `INSERT OR IGNORE`), so this is an inconsistency, not a hard design
problem. This plan brings trades to parity: a stable per-row key + idempotent
insert, so re-importing the same statement is a no-op and importing an updated
statement only adds the genuinely new rows — **without disturbing trades entered
manually through the web UI**.

## Current state

`agents/trader/trader_agent.py`:

The `trades` table schema (lines 19-30) has **no** uniqueness/reference column:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK(action IN ('BUY', 'SELL')),
    shares      REAL NOT NULL CHECK(shares > 0),
    price       REAL NOT NULL CHECK(price > 0),
    date        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    stop_loss   REAL,
    entry_price REAL
);
CREATE TABLE IF NOT EXISTS cash_flows (
    ...
    reference   TEXT UNIQUE           -- <-- cash_flows already has this
);
```

The DB is migrated additively at startup via `ALTER TABLE ... ADD COLUMN` wrapped
in `try/except` (lines 89-98) — follow that exact pattern for the new column.

The import loop reads a `Reference` column from the CSV but only uses it for
cash flows, never for trades:

```python
# agents/trader/trader_agent.py — inside import_sipp(), the TRADE branch (lines ~493-510)
if is_trade or is_hsbc_glob:
    try:
        shares = float(qty.replace("£", "").replace(",", ""))
        if shares > 0 and price > 0:
            action = ("BUY" if debit > 0 else "SELL" if credit > 0 else None)
            if action:
                ticker = symbol.upper() if is_trade else "HSFWA"
                conn.execute(
                    "INSERT INTO trades (ticker, action, shares, price, date, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ticker, action, shares, price, date, ""),
                )
```

`reference` is already parsed earlier in the loop (line ~486):
`reference = row.get("Reference", "").strip()`.

Manual trades created via the web UI go through `record_buy` / `record_sell`
(lines 128, 167) — these do **not** set a reference, so the new column must be
nullable and the uniqueness must apply only to non-null references (so manual
trades are never deduped or blocked).

### Design (chosen — match the existing `cash_flows` approach)

1. Add a nullable `reference TEXT` column to `trades`.
2. Add a **partial unique index** so uniqueness applies only to SIPP rows:
   `CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_reference ON trades(reference) WHERE reference IS NOT NULL;`
   (SQLite supports partial indexes. A plain `UNIQUE` column would forbid more
   than one manual NULL-reference trade — which we must allow — so it must be a
   partial index.)
3. In `import_sipp`, insert trades with `INSERT OR IGNORE` and pass the parsed
   `reference`. Re-imports collide on the index and are ignored.

> Why not "DELETE FROM trades before import"? Because the trades table also holds
> manual web-UI trades with no SIPP reference; deleting all rows would wipe them.
> The partial-index approach is idempotent for SIPP rows and leaves manual trades
> untouched.

### Repo conventions
- Migrations: additive `ALTER TABLE ... ADD COLUMN` inside `try/except Exception:
  pass` in `_init_db` (lines 86-98). Indexes: add to `_SCHEMA` (runs via
  `executescript`, which tolerates `IF NOT EXISTS`).
- Type hints required; line length ≤ 88; f-strings; snake_case.
- Tests: `pytest` + `tmp_path` for an isolated DB — see `tests/test_trader_agent.py`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Run trader tests | `python -m pytest tests/test_trader_agent.py -o addopts="" -q` | all pass |
| Run full root suite | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | all pass, < 30s |
| Inspect a built schema (ad hoc) | `python -c "import sqlite3,tempfile; ..."` | see Step 4 |

## Scope

**In scope** (the only files you may modify):
- `agents/trader/trader_agent.py` (schema, `_init_db` migration, `import_sipp`)
- `tests/test_trader_agent.py` (add regression test)

**Out of scope** (do NOT touch):
- `record_buy` / `record_sell` / `correct_trade` signatures — manual trades stay
  reference-less by design.
- `cash_flows` handling — already idempotent; leave it.
- The web layer (`web/app.py`) — no caller change is needed; `import_sipp` keeps
  the same signature and return type.
- Any existing production `trades.db` file — this plan does not migrate user data;
  the additive column + partial index apply on next `_init_db`. (See Maintenance.)

## Git workflow

- Branch: `advisor/002-import-sipp-idempotency`
- Commit per logical unit; conventional commits, e.g.
  `fix(trader): make SIPP import idempotent via trade reference key`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the `reference` column + partial unique index to the schema

In `_SCHEMA` (lines 19-52), add `reference TEXT` as the last column of the
`trades` table, and append a partial unique index statement after the
`CREATE TABLE` blocks:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_reference
    ON trades(reference) WHERE reference IS NOT NULL;
```

(`executescript` in `_init_db` runs the whole `_SCHEMA`; `IF NOT EXISTS` keeps it
safe on existing DBs.)

### Step 2: Migrate existing DBs additively

In `_init_db` (lines 86-98), extend the existing additive-migration loop so the
`reference` column is added to pre-existing `trades` tables. Mirror the current
pattern exactly:

```python
for col_def in ("stop_loss REAL", "entry_price REAL", "reference TEXT"):
    try:
        conn.execute(f"ALTER TABLE trades ADD COLUMN {col_def}")
    except Exception:
        pass
# Create the partial index after the column exists (idempotent):
try:
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_reference "
        "ON trades(reference) WHERE reference IS NOT NULL"
    )
except Exception:
    pass
```

### Step 3: Use the reference + `INSERT OR IGNORE` in `import_sipp`

In the trade-insert branch of `import_sipp` (lines ~501-506), change the insert to
include `reference` and use `INSERT OR IGNORE`:

```python
ticker = symbol.upper() if is_trade else "HSFWA"
conn.execute(
    "INSERT OR IGNORE INTO trades "
    "(ticker, action, shares, price, date, notes, reference) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    (ticker, action, shares, price, date, "", reference or None),
)
```

Pass `reference or None` so empty-string references become NULL (and are NOT
deduped — but SIPP rows always carry a unique reference, so this only affects
oddly-formed rows). Do not change the `buy_count`/`sell_count` accounting lines.

### Step 4: Verify idempotency manually before writing the test

Run this ad-hoc check (it uses a throwaway DB; adjust the import path if needed):

```bash
python - <<'PY'
import tempfile, os
from pathlib import Path
from agents.trader.trader_agent import TraderAgent
csv = """Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance
01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00
"""
d = tempfile.mkdtemp()
p = Path(d) / "in.csv"; p.write_text(csv, encoding="utf-8")
a = TraderAgent(name="T"); a.db_path = Path(d) / "trades.db"; a._init_db()
a.import_sipp(p); a.import_sipp(p)   # import twice
pos = a.get_portfolio()
print("positions:", [(x.ticker, x.shares) for x in pos])
assert len(pos) == 1 and pos[0].shares == 10, "DUPLICATED on re-import!"
print("OK: idempotent")
PY
```

**Expected**: `OK: idempotent` (shares == 10, not 20).

### Step 5: Add a regression test

Add to `tests/test_trader_agent.py`, modeled on `test_record_multiple_buys`
(uses `tmp_path`):

```python
def test_import_sipp_is_idempotent(tmp_path: Path) -> None:
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    agent.import_sipp(csv_path)
    agent.import_sipp(csv_path)  # re-import must not duplicate

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].ticker == "AAPL"
    assert portfolio[0].shares == 10.0  # not 20.0
```

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all
pass, including the new test.

### Step 6: Confirm the whole suite still passes

**Verify**: `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider`
→ exits 0, < 30s.

## Test plan

- New test: `test_import_sipp_is_idempotent` — imports the same CSV twice and
  asserts a single position with un-doubled shares (the exact regression).
- Pattern: model after `tests/test_trader_agent.py::test_record_multiple_buys`.
- (Optional, if quick) a second case: import CSV A, then a superset CSV A+B with a
  new reference, and assert both positions exist — proving new rows still import.
- Verification: `python -m pytest tests/test_trader_agent.py -o addopts="" -q`
  → all pass including the new test(s).

## Done criteria

ALL must hold:

- [ ] The Step-4 manual check prints `OK: idempotent`.
- [ ] `python -m pytest tests/test_trader_agent.py -o addopts="" -q` exits 0 with
      the new regression test present and passing.
- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0, < 30s.
- [ ] `grep -n "INSERT OR IGNORE INTO trades" agents/trader/trader_agent.py`
      returns one match (the import path).
- [ ] No files outside the in-scope list modified (`git status`).
- [ ] `plans/README.md` status row for 002 updated.

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code (drift since `f823039`).
- The partial-index `CREATE` fails on the installed SQLite (very old SQLite lacks
  partial indexes). If so, report the SQLite version
  (`python -c "import sqlite3; print(sqlite3.sqlite_version)"`) — do NOT fall back
  to deleting all trades.
- After Step 3, a real SIPP fixture you have access to shows trades being **dropped**
  on first import (would imply duplicate references within a single statement) —
  report it; the dedup key may need to combine reference with row identity.
- Any change here would require touching `web/app.py` or the agent's public method
  signatures.

## Maintenance notes

- **Existing production `trades.db`**: this change is forward-looking. The column
  + index are created on next `_init_db`, but rows already duplicated by past
  re-imports are NOT auto-cleaned. If the live DB is already doubled, that is a
  one-off data-repair task (dedupe by ticker/date/price or re-import from scratch)
  — explicitly deferred out of this plan. Flag it to the owner.
- If a future SIPP provider CSV lacks a stable `Reference` column, idempotency
  silently weakens (references become NULL → not deduped). Watch for that when
  onboarding a new broker export format.
- Reviewer should scrutinize: the index is **partial** (`WHERE reference IS NOT
  NULL`) so manual reference-less trades aren't blocked; and `reference or None`
  converts empty strings to NULL.
