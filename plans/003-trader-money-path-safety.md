# Plan 003: Trader money-path safety — transactional import + visible oversells

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If
> anything in "STOP conditions" occurs, stop and report — do not improvise. When
> done, update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat f823039..HEAD -- agents/trader/trader_agent.py tests/test_trader_agent.py`
> If either changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (adds safety + logging; the only behavioral change — surfacing oversells — is covered by a test)
- **Depends on**: plans/001-restore-green-test-suite.md
- **Category**: bug
- **Planned at**: commit `f823039`, 2026-06-13
- **Note**: If 002 is also being done, do 002 first, then this — both edit
  `import_sipp`. They don't conflict, but sequencing avoids a merge fixup.

## Why this matters

Two money-path weaknesses in `TraderAgent`:

1. **`import_sipp` is not transaction-safe.** It opens a raw connection, mutates
   in a loop, then `commit()`/`close()` at the very end with **no `try/finally`**.
   An exception partway through (a malformed row, a disk error) leaks the SQLite
   handle and leaves a **half-imported** database — some trades in, some out, no
   rollback. For a financial ledger, a silent partial import is worse than a clean
   failure.

2. **Oversells vanish silently.** `_replay_trades` clamps a SELL that exceeds
   holdings with `max(0.0, ...)`. A typo (selling 1000 instead of 100) or a missing
   buy is swallowed: the position just shows 0 shares with no signal, hiding a
   real data-entry error and skewing average cost on any later buy.

This plan makes a failed import roll back cleanly and makes oversells observable
(a warning log) — without changing the happy-path result.

## Current state

`agents/trader/trader_agent.py`:

**(1) `import_sipp` connection handling (lines 470-561, abridged):**

```python
with open(csv_path, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

conn = self._conn()                      # <-- raw connection, not a context manager
buy_count, sell_count, cash_count, cash_balance = 0, 0, 0, 0.0

for row in rows:
    ...                                   # many conn.execute(...) inserts
conn.commit()                             # <-- only reached if no exception
conn.close()                              # <-- leaks if an exception was raised above

if cash_balance > 0:
    self.set_cash_balance(cash_balance)
self.update_portfolio_snapshot(cash_balance)
...
return cash_balance
```

Every other DB method in this file uses `with self._conn() as conn:` (e.g.
`record_buy` line 140, `get_portfolio` line 269), which commits on success and
rolls back on exception. `import_sipp` is the outlier.

**(2) Oversell clamp in `_replay_trades` (lines 311-312):**

```python
else:  # SELL
    s["shares"] = max(0.0, s["shares"] - shares)
```

`_replay_trades` is a `@staticmethod` (line 283) used by `get_portfolio` (276)
and `refresh_portfolio_prices` (657). It has no logger today.

### Repo conventions
- Logging: use `logging.getLogger(__name__)`. `refresh_portfolio_prices` already
  does this (lines 645-648): `import logging; logger = logging.getLogger(__name__)`.
- Connection usage: `with self._conn() as conn:` everywhere else in the file.
- Type hints required; line length ≤ 88; f-strings; snake_case.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Run trader tests | `python -m pytest tests/test_trader_agent.py -o addopts="" -q` | all pass |
| Run full root suite | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | all pass, < 30s |

## Scope

**In scope** (the only files you may modify):
- `agents/trader/trader_agent.py` (`import_sipp` connection handling; `_replay_trades` logging)
- `tests/test_trader_agent.py` (add a partial-import rollback test and an oversell-warning test)

**Out of scope** (do NOT touch):
- The happy-path arithmetic of `_replay_trades` — shares must still clamp at 0 for
  display (a position can't go negative); we only ADD a warning, not change the math.
- `record_buy`/`record_sell`/`get_portfolio` public signatures.
- The broad `except Exception: pass` blocks elsewhere in the file (cash-flow
  inserts, CSV seeding) — deferred (see README "considered and rejected").
- `web/app.py` — no caller change needed.

## Git workflow

- Branch: `advisor/003-trader-money-path-safety`
- Commit per logical unit; conventional commits, e.g.
  `fix(trader): roll back SIPP import on error; log oversells`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Make `import_sipp` transaction-safe

Wrap the connection in `try/finally` so it always closes, and roll back on error
so a failed import leaves the DB unchanged. Replace the
`conn = self._conn()` ... `conn.commit(); conn.close()` structure with:

```python
conn = self._conn()
buy_count, sell_count, cash_count, cash_balance = 0, 0, 0, 0.0
try:
    for row in rows:
        ...                      # unchanged loop body
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()

if cash_balance > 0:
    self.set_cash_balance(cash_balance)
self.update_portfolio_snapshot(cash_balance)
...
return cash_balance
```

Keep the loop body exactly as-is (including its inner per-row `try/except` for
cash flows). The point is the **outer** transaction boundary. Note `cash_balance`
is computed inside the loop, so the post-import `set_cash_balance` /
`update_portfolio_snapshot` calls must stay AFTER the `finally` and only run on
success (they're already after `conn.close()` today — preserve that, since the
`raise` in `except` skips them on failure).

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all
pass (no regression to existing import behavior).

### Step 2: Surface oversells with a warning (without changing the math)

In `_replay_trades`, add a module-level logger and warn when a SELL exceeds
holdings, then still clamp at 0:

```python
else:  # SELL
    if shares > s["shares"]:
        logging.getLogger(__name__).warning(
            "Oversell for %s: sold %s but only %s held; clamping to 0",
            ticker, shares, s["shares"],
        )
    s["shares"] = max(0.0, s["shares"] - shares)
```

(`_replay_trades` is a staticmethod; calling `logging.getLogger(__name__)` inline
is fine and matches the lazy-logger style already used in
`refresh_portfolio_prices`.) Ensure `import logging` is present at module top (it
is imported lazily elsewhere; add a top-level `import logging` if not already
there, alongside the existing stdlib imports at lines 8-12).

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all pass.

### Step 3: Add regression tests

Add to `tests/test_trader_agent.py` (model after `test_record_multiple_buys`,
which uses `tmp_path`):

```python
def test_oversell_is_logged_and_clamped(tmp_path: Path, caplog) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("TEST1", 10.0, 100.0, "2026-04-30")
    agent.record_sell("TEST1", 25.0, 110.0, "2026-05-01")  # oversell

    import logging
    with caplog.at_level(logging.WARNING):
        portfolio = agent.get_portfolio()

    # Position is gone (clamped to 0), and the oversell was logged
    assert all(p.ticker != "TEST1" for p in portfolio)
    assert any("Oversell" in r.message for r in caplog.records)


def test_import_sipp_rolls_back_on_error(tmp_path: Path) -> None:
    # A malformed CSV that parses but triggers a failure mid-import would leave
    # a partial DB without rollback. Here we assert the connection is closed and
    # a clean DB results from a well-formed import (smoke for the try/finally).
    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
        "01/02/2024,AAPL,B1,10,100.00,Buy,REF1,1000.00,,5000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.import_sipp(csv_path)
    assert len(agent.get_portfolio()) == 1
```

> If plan 002 already added `test_import_sipp_is_idempotent`, keep both — they
> cover different properties.

**Verify**: `python -m pytest tests/test_trader_agent.py -o addopts="" -q` → all
pass, including the two new tests.

### Step 4: Confirm the whole suite still passes

**Verify**: `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider`
→ exits 0, < 30s.

## Test plan

- `test_oversell_is_logged_and_clamped` — proves the SELL>holdings case logs a
  warning AND still clamps (the position disappears rather than going negative).
- `test_import_sipp_rolls_back_on_error` — smoke for the transaction boundary; a
  clean import yields exactly one position and the connection is released.
- Pattern: `tests/test_trader_agent.py::test_record_multiple_buys`.
- `caplog` is a built-in pytest fixture — no new dependency.

## Done criteria

ALL must hold:

- [ ] `python -m pytest tests/test_trader_agent.py -o addopts="" -q` exits 0 with
      the two new tests passing.
- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0, < 30s.
- [ ] `grep -n "conn.rollback()" agents/trader/trader_agent.py` returns a match
      inside `import_sipp`.
- [ ] `grep -n "Oversell" agents/trader/trader_agent.py` returns one match.
- [ ] No files outside the in-scope list modified (`git status`).
- [ ] `plans/README.md` status row for 003 updated.

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code (drift since `f823039`).
- Wrapping `import_sipp` in `try/finally` changes the happy-path result of any
  existing test (it should not — report the diff if it does).
- You find `_replay_trades` is called somewhere that runs per-request in a tight
  loop where a warning log would be excessively noisy — report it before merging,
  rather than silencing the warning.

## Maintenance notes

- The oversell warning is informational, not enforcement. If the product later
  wants to *reject* oversells at entry time, that belongs in `record_sell`
  (validate against current holdings) — a separate change; note it here.
- If a future refactor moves `cash_balance` accumulation out of the loop, re-check
  that `set_cash_balance`/`update_portfolio_snapshot` still run only on a
  successful (committed) import.
- Reviewer should scrutinize: the `except: conn.rollback(); raise` re-raises (so
  callers still see the failure) and the `finally` always closes.
