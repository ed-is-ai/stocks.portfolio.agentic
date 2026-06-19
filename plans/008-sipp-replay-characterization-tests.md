# Plan 008: Pin SIPP import & replay behavior with characterization tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 6429330..HEAD -- app/agents/trader/trader_agent.py app/repositories/trades_repo.py tests/test_trader_agent.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `6429330`, 2026-06-19

## Why this matters

`import_sipp` is the money-critical path: it builds the trade ledger that the
portfolio's average cost basis, entry dates, and P&L are computed from. Today
the only tests are a single-row idempotency check and a single-row smoke test
([tests/test_trader_agent.py](../tests/test_trader_agent.py)). The behaviors
that actually matter — that trades replay in **chronological** order regardless
of CSV row order, that non-trade rows are classified into the right cash-flow
type, and that the returned cash balance comes from the running balance — have
**zero coverage**. Plan 009 will refactor the date handling in this path; without
characterization tests first, that refactor is unverifiable. This plan adds the
safety net. It changes **no production code**.

## Current state

- `app/agents/trader/trader_agent.py` — `import_sipp` (lines 357–502) parses the
  CSV, inserts trades and cash flows in one transaction, and returns the cash
  balance taken from the final non-empty `Running Balance` cell.
- `app/agents/trader/trader_agent.py` — `get_portfolio` (line 190) calls
  `_trades.open_rows()` then `_replay_trades` (line 211) to derive per-ticker
  shares / avg cost / entry date.
- `app/repositories/trades_repo.py` — `open_rows()` (line 105) returns trades
  **sorted chronologically** via `_DATE_SORT`, which reconstructs a `YYYY/MM/DD`
  key from a `DD/MM/YYYY` date string (lines 9–11):
  ```python
  _DATE_SORT = (
      "substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2)"
  )
  ```
- Replay is order-sensitive for SELLs: a SELL is clamped to available shares
  (`max(0.0, shares - sold)`), so processing a SELL before its preceding BUY
  yields a different result than the correct chronological order
  (trader_agent.py:238–246).
- The SIPP CSV header (see existing tests) is:
  `Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance`
  Dates in the SIPP CSV are `DD/MM/YYYY`. A row is a **trade** when `Quantity`
  and `Symbol` are present and not `n/a`; `Debit>0` ⇒ BUY, `Credit>0` ⇒ SELL.
  A row with `Symbol=n/a` (or no quantity) is a **cash flow**, classified by
  `Description` into `CONTRIBUTION` / `DIVIDEND` / `INTEREST` / `TAX_RELIEF` /
  `TRANSFER` / `WITHDRAWAL` / `OTHER`.

### Test conventions to follow

Model new tests on the existing ones in
[tests/test_trader_agent.py](../tests/test_trader_agent.py): each test builds a
`TraderAgent`, points `agent.db_path` at a `tmp_path` SQLite file, and calls
`agent._init_db()` before use. Example:

```python
def test_import_sipp_is_idempotent(tmp_path: Path) -> None:
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B123,10,100.00,Buy AAPL,REF-AAPL-1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path)
```

Reading cash flows: there is no public accessor, so query SQLite directly with
the stdlib `sqlite3` module against `agent.db_path`.

## Commands you will need

| Purpose   | Command                                             | Expected on success |
|-----------|-----------------------------------------------------|---------------------|
| Run these tests | `uv run pytest tests/test_trader_agent.py -v`  | all pass            |
| Full suite | `uv run pytest`                                    | all pass (88+)      |
| Lint      | `uv run ruff check tests/test_trader_agent.py`      | All checks passed!  |
| Format    | `uv run ruff format tests/test_trader_agent.py`     | reformatted/unchanged |

## Scope

**In scope** (the only file you should modify):
- `tests/test_trader_agent.py` (add tests; do not change existing ones)

**Out of scope** (do NOT touch):
- `app/agents/trader/trader_agent.py` — this plan only characterizes current
  behavior; do not "fix" anything you find. Bugs are handled by plans 009/010.
- `app/repositories/*.py`

## Git workflow

- Branch: `advisor/008-sipp-replay-characterization-tests`
- Commit message style: conventional commits, e.g.
  `test(trader): characterize SIPP replay ordering, classification, cash balance`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Characterize chronological replay ordering

Add a test proving replay sorts by trade date, not CSV row order. Use a CSV
where the SELL row (later date) appears **before** the BUY row (earlier date):

```python
def test_replay_orders_by_trade_date_not_file_order(tmp_path: Path) -> None:
    # SELL row (15/03) listed BEFORE the BUY row (01/02). Correct chronological
    # replay = BUY 10 then SELL 4 => 6 shares. If rows were replayed in file
    # order, the SELL would hit 0 shares (clamped) and leave 10.
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "15/03/2024,AAPL,B1,4,110.00,Sell AAPL,REF-S1,,440.00,5440.00\n"
        "01/02/2024,AAPL,B1,10,100.00,Buy AAPL,REF-B1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path)

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    pos = portfolio[0]
    assert pos.ticker == "AAPL"
    assert pos.shares == 6.0          # chronological replay, not 10.0
    assert pos.entry_date == "01/02/2024"  # earliest BUY sets entry date
```

**Verify**: `uv run pytest tests/test_trader_agent.py::test_replay_orders_by_trade_date_not_file_order -v` → passes.

> Note: `entry_date` is asserted as the raw `DD/MM/YYYY` string because that is
> what the current code stores. Plan 009 will change stored dates to ISO and will
> update this assertion as part of its own changes — that is expected and
> accounted for there.

### Step 2: Characterize cash-flow classification

```python
def test_sipp_classifies_cash_flows(tmp_path: Path) -> None:
    import sqlite3

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Monthly contribution,REF-C1,,500.00,500.00\n"
        "02/01/2024,n/a,,,,Tax relief,REF-T1,,125.00,625.00\n"
        "03/01/2024,n/a,,,,AAPL dividend,REF-D1,,12.50,637.50\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path)

    conn = sqlite3.connect(agent.db_path)
    rows = conn.execute(
        "SELECT flow_type, amount FROM cash_flows ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows == [
        ("CONTRIBUTION", 500.0),
        ("TAX_RELIEF", 125.0),
        ("DIVIDEND", 12.5),
    ]
    # None of these created phantom trade positions
    assert agent.get_portfolio() == []
```

**Verify**: `uv run pytest tests/test_trader_agent.py::test_sipp_classifies_cash_flows -v` → passes.

### Step 3: Characterize cash balance from running balance

```python
def test_sipp_cash_balance_is_final_running_balance(tmp_path: Path) -> None:
    # Rows in chronological (oldest-first) order, as the documented import
    # expects. The returned cash balance is the last running balance.
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/01/2024,n/a,,,,Contribution,REF-C1,,1000.00,1000.00\n"
        "01/02/2024,AAPL,B1,5,100.00,Buy AAPL,REF-B1,500.00,,500.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    cash = agent.import_sipp(csv_path)
    assert cash == 500.0
```

**Verify**: `uv run pytest tests/test_trader_agent.py::test_sipp_cash_balance_is_final_running_balance -v` → passes.

### Step 4: Run the full suite, lint, and format

**Verify**:
- `uv run pytest` → all pass (the original 85 plus 3 new = 88).
- `uv run ruff check tests/test_trader_agent.py` → `All checks passed!`
- `uv run ruff format tests/test_trader_agent.py` → `1 file ... unchanged` (or reformatted; re-stage if so).

## Test plan

Three new tests in `tests/test_trader_agent.py`, modeled on the existing
`test_import_sipp_is_idempotent`:
- ordering: SELL-before-BUY in file, correct chronological result (`shares == 6.0`).
- classification: contribution / tax relief / dividend → correct `flow_type`.
- cash balance: returned value equals the final running balance.

Verification: `uv run pytest tests/test_trader_agent.py -v` → all pass, 3 new.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest` exits 0; the 3 new tests above exist and pass
- [ ] `uv run ruff check tests/test_trader_agent.py` → `All checks passed!`
- [ ] `git status` shows only `tests/test_trader_agent.py` modified (plus this
      `plans/README.md` row)
- [ ] `plans/README.md` status row for 008 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- Any of the three new tests **fails** on current code. That means the behavior
  you are characterizing is not what this plan assumed — report the actual
  result; do not change production code to make the test pass.
- The drift check shows `import_sipp` or `trades_repo.py` changed since commit
  `6429330` and the "Current state" excerpts no longer match.
- The existing tests in the file start failing.

## Maintenance notes

- These tests deliberately pin **current** behavior, including the raw
  `DD/MM/YYYY` `entry_date` string (Step 1) and the last-row cash-balance
  semantics (Step 3). Plans 009 (date canonicalization) and any future plan for
  the cash-balance-ordering finding will update the specific assertions they
  change — that is expected, not a regression.
- When plan 009 lands, the Step 1 `entry_date` assertion changes from
  `"01/02/2024"` to `"2024-02-01"`. The `shares == 6.0` assertion must keep
  passing — if it doesn't, 009 broke chronological ordering.
