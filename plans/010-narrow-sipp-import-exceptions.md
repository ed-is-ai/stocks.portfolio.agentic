# Plan 010: Stop silently swallowing errors inside `import_sipp`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dbf0d18..HEAD -- app/agents/trader/trader_agent.py tests/test_trader_agent.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none (independent; can land before or after 008/009)
- **Category**: bug
- **Planned at**: commit `6429330`, 2026-06-19
- **Reconciled at**: commit `dbf0d18` (post-009), 2026-06-19 — line numbers refreshed after plan 009 landed in main; logic unchanged

## Why this matters

`import_sipp` is wrapped in one transaction that rolls back on error
(`trader_agent.py:504–509`, added by an earlier money-path-safety plan). But two
inner blocks defeat that safety by swallowing **every** exception per row:

```python
except Exception:
    pass
```

at `trader_agent.py:476` and `:495` (the two cash-flow insert sites), plus a
silent `except ValueError: pass` at `:459` on the trade branch. The consequence:
a genuine database error on a cash-flow row is caught and discarded, the loop
continues, and `conn.commit()` still runs — so a partial import is silently
committed instead of rolling back, and a malformed trade row vanishes with no log
entry. Either way the resulting cash balance / positions can be wrong with **no
diagnostic**. The prior trader-exception cleanup explicitly left these
`import_sipp` catches alone (see `plans/006`), so they are still here.

This plan makes per-row database errors propagate to the existing rollback
handler, and makes malformed-data skips visible in the log.

## Current state

`app/agents/trader/trader_agent.py`, inside `import_sipp` (the row loop runs
inside `try: ... except Exception: conn.rollback(); raise`).

Trade branch (lines 433–460):
```python
try:
    shares = float(qty.replace("£", "").replace(",", ""))
    if shares > 0 and price > 0:
        action = ("BUY" if debit > 0 else "SELL" if credit > 0 else None)
        if action:
            ticker = symbol.upper() if is_trade else "HSFWA"
            self._trades.insert_ignore(conn, ticker, action, shares, price, date, "", reference or None)
            if action == "BUY":
                buy_count += 1
            else:
                sell_count += 1
except ValueError:
    pass
```

First cash-flow site (lines 462–477):
```python
amount = credit if credit > 0 else debit
if amount > 0:
    flow_type = classify_flow_type(description)
    try:
        self._cash_flows.insert_ignore(conn, date, flow_type, None, amount, description, reference)
        cash_count += 1
    except Exception:
        pass
```

Second cash-flow site (lines 481–496) is identical in body to the first.

The whole loop is already protected by (lines 504–509):
```python
    conn.commit()
except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
```

`insert_ignore` uses `INSERT OR IGNORE`, so a duplicate `reference` does **not**
raise — idempotent re-imports stay safe after this change. The errors that would
now propagate are real DB faults (locked db, constraint violation), which *should*
abort and roll back the import.

### Conventions to follow

- Module logger already exists: `logger = logging.getLogger(__name__)`
  (trader_agent.py:27). Use `logger.warning(...)` with `%`-style args, matching
  the existing `_replay_trades` oversell log (trader_agent.py:240–245).

## Commands you will need

| Purpose   | Command                                          | Expected on success |
|-----------|--------------------------------------------------|---------------------|
| Tests     | `uv run pytest tests/test_trader_agent.py -v`    | all pass            |
| Full suite | `uv run pytest`                                  | all pass            |
| Lint      | `uv run ruff check .`                             | All checks passed!  |
| Format    | `uv run ruff format app/ tests/`                  | reformatted/unchanged |

## Scope

**In scope** (the only files you should modify):
- `app/agents/trader/trader_agent.py` (the three inner catch blocks in `import_sipp`)
- `tests/test_trader_agent.py` (add one test)

**Out of scope** (do NOT touch):
- The outer transaction handler (lines 504–509) — it already does the right thing.
- `record_buy` / `record_sell` / any non-import method.
- The repositories.

## Git workflow

- Branch: `advisor/010-narrow-sipp-import-exceptions`
- Commit message style: conventional commits, e.g.
  `fix(trader): stop swallowing per-row errors in SIPP import`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Let cash-flow DB errors propagate to the rollback handler

At **both** cash-flow insert sites, remove the inner `try/except Exception:
pass`, leaving the insert and counter increment so any real DB error propagates
to the existing outer handler (which rolls back and re-raises). Each site becomes:

```python
amount = credit if credit > 0 else debit
if amount > 0:
    flow_type = classify_flow_type(description)
    self._cash_flows.insert_ignore(
        conn, date, flow_type, None, amount, description, reference
    )
    cash_count += 1
```

Apply this to the site at lines 462–477 **and** the identical one at 481–496.

**Verify**: `grep -n "except Exception" app/agents/trader/trader_agent.py` →
returns **only** the outer handler line (the one immediately followed by
`conn.rollback()`); the two cash-flow `except Exception: pass` blocks are gone.

### Step 2: Log (don't silently drop) malformed trade rows

Replace the trade branch's `except ValueError: pass` (line 459) with a logged
skip, so a row whose `Quantity` is non-numeric is visible:

```python
except ValueError:
    logger.warning(
        "skipping trade row with unparseable quantity %r (ref %s)",
        qty,
        reference or "",
    )
```

Keep this as a non-fatal skip: malformed *source data* should not abort an
otherwise-valid import, but it must not be invisible. (DB errors are a different
class and are handled by Step 1's propagation.)

**Verify**: `grep -n "except ValueError" app/agents/trader/trader_agent.py` →
the match is now followed by a `logger.warning`, not `pass`.

### Step 3: Add a test for the malformed-row log

In `tests/test_trader_agent.py`:

```python
def test_sipp_logs_and_skips_malformed_quantity(tmp_path: Path, caplog) -> None:
    import logging

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,notanumber,100.00,Buy AAPL,REF-BAD,1000.00,,5000.00\n"
        "02/02/2024,MSFT,B2,5,200.00,Buy MSFT,REF-OK,1000.00,,4000.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()

    with caplog.at_level(logging.WARNING):
        agent.import_sipp(csv_path)

    # The malformed AAPL row was skipped (and logged); the valid MSFT row imported.
    portfolio = agent.get_portfolio()
    assert {p.ticker for p in portfolio} == {"MSFT"}
    assert any("unparseable quantity" in r.message for r in caplog.records)
```

**Verify**: `uv run pytest tests/test_trader_agent.py::test_sipp_logs_and_skips_malformed_quantity -v` → passes.

### Step 4: Full suite, lint, format

**Verify**:
- `uv run pytest` → all pass (the existing `test_import_sipp_is_idempotent` and
  `test_import_sipp_rolls_back_on_error` must still pass — they prove
  idempotency and the rollback boundary are intact).
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage).

## Test plan

- New `test_sipp_logs_and_skips_malformed_quantity`: a non-numeric `Quantity`
  row is logged and skipped while a valid row in the same import still lands.
- Regression guard: the existing `test_import_sipp_is_idempotent` confirms
  duplicate references still don't raise after removing the inner catches (the
  `INSERT OR IGNORE` path). Do not modify it.
- Verification: `uv run pytest tests/test_trader_agent.py -v` → all pass.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -c "except Exception" app/agents/trader/trader_agent.py` returns `1`
      (only the outer rollback handler remains)
- [ ] `grep -n "pass" app/agents/trader/trader_agent.py` shows no `except ...: pass`
      inside `import_sipp` (lines ~459–496)
- [ ] `uv run pytest` exits 0; the new test exists and passes; idempotency and
      rollback tests still pass
- [ ] `uv run ruff check .` → `All checks passed!`
- [ ] `git status` shows only the 2 in-scope files modified
- [ ] `plans/README.md` status row for 010 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `import_sipp` changed since `6429330` and the line
  numbers / excerpts no longer match (e.g. plan 009 reshaped the loop). If 009
  landed first, the two cash-flow sites and the `except ValueError` may have
  moved — re-locate them by content, but if the structure differs materially,
  stop and report.
- Removing the inner catches makes `test_import_sipp_is_idempotent` fail (that
  would mean `INSERT OR IGNORE` is *not* actually suppressing duplicate-reference
  errors and the assumption behind this plan is wrong).

## Maintenance notes

- After this change, a real database error during SIPP import aborts and rolls
  back the whole import (correct, money-safe behavior) instead of silently
  committing a partial result. Operators should expect an exception + traceback
  on a genuinely broken import rather than a quietly wrong balance.
- If a future requirement wants "import the good rows, report the bad ones"
  (partial import), that is a deliberate design change — it should collect
  per-row errors into a report and surface them, not reinstate `except: pass`.
- This plan and plan 009 both edit the `import_sipp` row loop. If executing both,
  prefer landing 009 first, then re-locate these catch blocks by content (their
  line numbers will have shifted).
