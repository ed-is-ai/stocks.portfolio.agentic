# Plan 009: Store all trade dates as ISO `YYYY-MM-DD` and sort by them directly

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 6429330..HEAD -- app/agents/trader/trader_agent.py app/repositories/trades_repo.py app/repositories/db.py tests/test_trader_agent.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/008-sipp-replay-characterization-tests.md (must be DONE first)
- **Category**: bug
- **Planned at**: commit `6429330`, 2026-06-19

## Why this matters

The `trades` table stores dates in **two incompatible formats** depending on how
a trade was created, and the chronological sort only understands one of them:

- `import_sipp` stores the raw SIPP CSV date, `DD/MM/YYYY`
  (`trader_agent.py:406` reads it, `:434` inserts it unmodified).
- `record_buy` / `record_sell` / `correct_trade` default the date to
  `datetime.today().strftime("%Y-%m-%d")` — ISO `YYYY-MM-DD`
  (`trader_agent.py:91`, `:123`, `:153`).
- `open_rows()` orders trades for replay with `_DATE_SORT`, which rebuilds a
  sort key from **substring positions hardcoded for `DD/MM/YYYY`**
  (`trades_repo.py:9–11`).

For an ISO string like `2026-04-30`, those substrings (`substr(date,7,4)` etc.)
produce a garbage key. So when the table holds **both** formats — which it does
the moment a user records a manual trade alongside imported SIPP trades — the
"chronological" replay runs in a meaningless order. Because SELLs are clamped to
available shares and BUY ordering sets the entry date, a wrong order produces a
**wrong average cost basis, wrong entry date, and wrong P&L**, silently.

The fix: canonicalize every stored trade date to ISO `YYYY-MM-DD` (which sorts
chronologically as a plain string), sort by the column directly, and migrate the
existing rows that are still in `DD/MM/YYYY`.

## Current state

- `app/agents/trader/trader_agent.py`
  - `import_sipp` reads the date at line 406 and passes it to
    `self._trades.insert_ignore(...)` at line 428–437 and to
    `self._cash_flows.insert_ignore(...)` at lines 449–457 / 468–476:
    ```python
    date = row.get("Date", "").strip()        # line 406  (DD/MM/YYYY)
    ...
    self._trades.insert_ignore(conn, ticker, action, shares, price, date, "", reference or None)
    ...
    self._cash_flows.insert_ignore(conn, date, flow_type, None, amount, description, reference)
    ```
  - `record_buy` / `record_sell` / `correct_trade` already produce ISO via
    `datetime.today().strftime("%Y-%m-%d")` (lines 91, 123, 153). They are
    **not** changed by this plan, but verify they remain ISO.
- `app/repositories/trades_repo.py` lines 8–12:
  ```python
  # Replays sort DD/MM/YYYY dates by reconstructing YYYY/MM/DD, then id.
  _DATE_SORT = (
      "substr(date, 7, 4) || '/' || substr(date, 4, 2) || '/' || substr(date, 1, 2)"
  )
  _REPLAY_COLUMNS = "ticker, action, shares, price, date, stop_loss, entry_price"
  ```
  `_DATE_SORT` is used in `history()` (line 94) and `open_rows()` (line 115).
- `app/repositories/db.py` — `init_trades_db(conn)` (lines 84–104) is the single
  owner of schema + additive migrations; it runs on every `TraderAgent` startup
  via `_init_db`. This is where the one-time data migration belongs.

### Conventions to follow

- Migrations in `init_trades_db` are wrapped in `try/except
  sqlite3.OperationalError` with `logger.debug(...)` and end with `conn.commit()`
  — match that style (db.py:87–104).
- Logging: module-level `logger = logging.getLogger(__name__)` already exists in
  both `trader_agent.py` (line 27) and `db.py` (line 15).

## Commands you will need

| Purpose   | Command                                          | Expected on success |
|-----------|--------------------------------------------------|---------------------|
| Tests     | `uv run pytest tests/test_trader_agent.py -v`    | all pass            |
| Full suite | `uv run pytest`                                  | all pass            |
| Lint      | `uv run ruff check .`                             | All checks passed!  |
| Format    | `uv run ruff format app/ tests/`                  | reformatted/unchanged |
| Typecheck | `uv run pyrefly check app/agents/trader/trader_agent.py app/repositories/trades_repo.py app/repositories/db.py` | no NEW errors in these files |

## Scope

**In scope** (the only files you should modify):
- `app/agents/trader/trader_agent.py` (add a date helper; normalize in `import_sipp`)
- `app/repositories/trades_repo.py` (`_DATE_SORT` → sort by column)
- `app/repositories/db.py` (one-time DD/MM/YYYY → ISO migration in `init_trades_db`)
- `tests/test_trader_agent.py` (update one assertion from plan 008; add regression test)

**Out of scope** (do NOT touch):
- `record_buy` / `record_sell` / `correct_trade` signatures or defaults — they
  already emit ISO; do not change them.
- `app/repositories/cash_flows_repo.py` — cash-flow dates are normalized at the
  `import_sipp` call site (Step 2) and migrated in Step 4; the repo itself is
  unchanged.
- The web/service layer (`app/services/`, `app/api/`).

## Git workflow

- Branch: `advisor/009-canonicalize-trade-dates-iso`
- Commit message style: conventional commits, e.g.
  `fix(trader): store trade dates as ISO and sort chronologically`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add an ISO date helper in `trader_agent.py`

Add a module-level function near the top of
`app/agents/trader/trader_agent.py` (after the imports / `logger`, before the
class). It converts `DD/MM/YYYY` to `YYYY-MM-DD`, passes a value that is already
ISO straight through, and on any unrecognized format logs a warning and returns
the input unchanged (never raise — an import must not crash on one odd row, but
the warning surfaces it):

```python
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
```

`datetime` is already imported (`from datetime import datetime`, line 10).

**Verify**: `uv run python -c "from app.agents.trader.trader_agent import _to_iso_date; print(_to_iso_date('01/02/2024'), _to_iso_date('2024-02-01'))"`
→ prints `2024-02-01 2024-02-01`.

### Step 2: Normalize the date in `import_sipp`

In `app/agents/trader/trader_agent.py`, change the date read at line 406 so the
normalized ISO date is used for **both** the trade insert and the cash-flow
inserts:

```python
date = _to_iso_date(row.get("Date", "").strip())
```

Leave the rest of the loop unchanged — both `insert_ignore` calls already use the
`date` variable.

**Verify**: `uv run pytest tests/test_trader_agent.py -v` → the plan-008 tests
that assert `shares`/`flow_type`/cash still pass (the `entry_date` assertion is
updated in Step 5).

### Step 3: Sort by the date column directly in `trades_repo.py`

Now that all newly written dates are ISO (Step 2) and existing rows get migrated
(Step 4), ISO strings sort chronologically as plain text. Replace the
`_DATE_SORT` definition in `app/repositories/trades_repo.py` (lines 8–11):

```python
# Dates are stored ISO (YYYY-MM-DD), which sorts chronologically as text.
_DATE_SORT = "date"
```

Do not change `history()` or `open_rows()` — they interpolate `_DATE_SORT` and
keep working (`ORDER BY date DESC, id DESC` and `ORDER BY date, id`).

**Verify**: `uv run pytest tests/test_trader_agent.py -v` → ordering test still
passes.

### Step 4: Migrate existing `DD/MM/YYYY` rows to ISO in `init_trades_db`

In `app/repositories/db.py`, inside `init_trades_db`, **before** the final
`conn.commit()` (line 104), add an idempotent data migration that rewrites any
remaining `DD/MM/YYYY` dates to ISO in both date-bearing tables. The
`date LIKE '__/__/____'` predicate matches only slash-format rows, so re-running
is a no-op:

```python
    for table in ("trades", "cash_flows"):
        try:
            conn.execute(
                f"UPDATE {table} SET date = "
                "substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' "
                "|| substr(date, 1, 2) "
                "WHERE date LIKE '__/__/____'"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("date migration step skipped: %s", exc)
```

**Verify**: add and run this throwaway check (then delete the temp file):
```bash
uv run python - <<'PY'
import sqlite3, tempfile, os
from app.repositories import db
p = os.path.join(tempfile.mkdtemp(), "t.db")
c = db.connect(p); db.init_trades_db(c)
c.execute("INSERT INTO trades (ticker,action,shares,price,date) VALUES ('AAPL','BUY',1,1,'15/03/2024')")
c.commit(); c.close()
c = db.connect(p); db.init_trades_db(c)  # migration runs here
print(c.execute("SELECT date FROM trades").fetchone()[0])  # expect 2024-03-15
PY
```
→ prints `2024-03-15`.

### Step 5: Update plan-008 ordering assertion and add the mixed-format regression test

In `tests/test_trader_agent.py`:

(a) In `test_replay_orders_by_trade_date_not_file_order`, the stored date is now
ISO, so change the entry-date assertion:
```python
    assert pos.entry_date == "2024-02-01"   # was "01/02/2024"
```
The `shares == 6.0` assertion must remain and keep passing.

(b) Add a regression test for the actual bug — a manual ISO trade and an imported
`DD/MM/YYYY` trade for the same ticker must replay in correct chronological
order:
```python
def test_replay_correct_with_mixed_date_formats(tmp_path: Path) -> None:
    # Manual BUY (ISO date) is earlier; imported SELL (DD/MM/YYYY) is later.
    # Correct chronological replay = BUY 10 then SELL 4 => 6 shares.
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 10.0, 100.0, "2024-01-01")

    csv_text = (
        "Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,"
        "Running Balance\n"
        "01/02/2024,AAPL,B1,4,110.00,Sell AAPL,REF-S1,,440.00,4560.00\n"
    )
    csv_path = tmp_path / "sipp.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    agent.import_sipp(csv_path)

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].shares == 6.0
    assert portfolio[0].entry_date == "2024-01-01"
```

**Verify**: `uv run pytest tests/test_trader_agent.py -v` → all pass, including
the new `test_replay_correct_with_mixed_date_formats`.

### Step 6: Full suite, lint, format, typecheck

**Verify**:
- `uv run pytest` → all pass.
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage).
- `uv run pyrefly check app/agents/trader/trader_agent.py app/repositories/trades_repo.py app/repositories/db.py` → no new errors attributable to your changes.

## Test plan

- Update `test_replay_orders_by_trade_date_not_file_order` entry-date assertion
  to ISO (behavior intentionally changed by this plan).
- New `test_replay_correct_with_mixed_date_formats`: ISO manual trade + imported
  `DD/MM/YYYY` trade, same ticker — asserts `shares == 6.0` and ISO entry date.
  This test **fails before** Steps 1–4 and **passes after** (it is the proof the
  bug is fixed).
- Migration smoke: the inline `python - <<PY` check in Step 4.
- Verification: `uv run pytest` → all pass.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest` exits 0; `test_replay_correct_with_mixed_date_formats` exists and passes
- [ ] `grep -n "substr(date" app/repositories/trades_repo.py` returns **no** matches (the substr sort is gone)
- [ ] `grep -n "_to_iso_date" app/agents/trader/trader_agent.py` returns the helper definition and its use in `import_sipp`
- [ ] `grep -n "date LIKE '__/__/____'" app/repositories/db.py` returns the migration
- [ ] `uv run ruff check .` → `All checks passed!`
- [ ] `git status` shows only the 4 in-scope files modified
- [ ] `plans/README.md` status row for 009 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- Plan 008 is not yet DONE (its characterization tests are the safety net for
  this change). Check `plans/README.md`.
- The drift check shows any in-scope file changed since `6429330` and the
  "Current state" excerpts no longer match.
- After Step 4, any existing trade in a test fixture sorts differently than
  expected, or a plan-008 test other than the intended Step-5 assertion changes
  result — that means the migration or sort change has a wider effect than
  intended.
- `_DATE_SORT` turns out to be referenced anywhere outside `trades_repo.py`
  (`grep -rn "_DATE_SORT" app/`) — if so, stop and report.

## Maintenance notes

- **Live data**: the migration in Step 4 rewrites dates in the real
  `app/agents/trader/trader_agent`'s database the next time the app starts. Before
  deploying, back up the live `trades.db` (the repo already keeps a
  `trades.db.backup` alongside it). Tests are unaffected — they use `tmp_path`
  databases.
- After this lands, **all** trade dates are ISO. Any new code that writes trade
  dates must use ISO (`%Y-%m-%d`); `_to_iso_date` is the helper to route any
  externally-formatted date through.
- A reviewer should confirm: no remaining `DD/MM/YYYY` literals are written to
  the `date` column, and `_DATE_SORT = "date"` did not change the newest-first
  ordering in `history()` (used by the web trade-history view).
- Cash-flow date *sorting* is not currently used anywhere; this plan still
  migrates `cash_flows.date` for consistency. If a future feature sorts cash
  flows by date, it can rely on ISO ordering.
