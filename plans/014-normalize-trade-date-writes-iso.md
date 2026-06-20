# Plan 014: Normalize all trade-date writes to ISO (close the 009 invariant)

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
- **Depends on**: 011 (clean suite). Builds on the date work already merged (the
  `_to_iso_date` helper exists).
- **Category**: bug
- **Planned at**: commit `dbf0d18`, 2026-06-19

## Why this matters

A prior change established the invariant that **all trade dates are stored ISO
`YYYY-MM-DD`** so the replay sort (`_DATE_SORT = "date"`, lexical = chronological)
is correct. `import_sipp` normalizes its dates through a helper `_to_iso_date`.
But `record_buy` / `record_sell` / `correct_trade` — the path used by the web
trade form and any direct API call — pass a caller-supplied `date` string
**straight to the database without normalizing it**. They only produce ISO when
`date` is omitted (the `datetime.today()` default).

In practice the web form uses `<input type="date">`, which submits ISO, so the
bug is latent today. But the invariant is one non-ISO write away from breaking:
a direct `POST /trades` with `date=15/03/2024`, or any future client, would store
`15/03/2024`, which `_DATE_SORT = "date"` sorts as the string `"15/03/2024"` —
re-introducing the silent wrong-cost-basis bug that the date work fixed. This
plan routes every provided date through the same `_to_iso_date` helper, making the
ISO invariant total at the write boundary.

## Current state

`app/agents/trader/trader_agent.py`:

- The helper already exists (module level, line 32):
  ```python
  def _to_iso_date(value: str) -> str:
      """Return ``value`` as ISO ``YYYY-MM-DD``. Accepts DD/MM/YYYY and ISO;
      logs + returns unchanged on unrecognized formats."""
      ...
  ```
- Three methods compute `trade_date` identically, taking the caller date as-is:
  - `record_buy` — line 108: `trade_date = date or datetime.today().strftime("%Y-%m-%d")`
  - `record_sell` — line 140: `trade_date = date or datetime.today().strftime("%Y-%m-%d")`
  - `correct_trade` — line 170: `trade_date = date or datetime.today().strftime("%Y-%m-%d")`

  Each then inserts `trade_date` via `self._trades.insert(...)`.
- `import_sipp` already normalizes (`date = _to_iso_date(...)`), so it is **not**
  changed here.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest tests/test_trader_agent.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |

## Scope

**In scope** (the only files you should modify):
- `app/agents/trader/trader_agent.py` (the three `trade_date = ...` lines)
- `tests/test_trader_agent.py` (add tests)

**Out of scope** (do NOT touch):
- `import_sipp` — already normalizes.
- `app/repositories/*`, the web routes, the services.
- The `_to_iso_date` helper itself.

## Git workflow

- Branch: `advisor/014-normalize-trade-date-writes-iso`
- Commit message: `fix(trader): normalize record_buy/sell/correct dates to ISO`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Route the provided date through `_to_iso_date` in all three methods

In `app/agents/trader/trader_agent.py`, change each of the three identical lines
(108, 140, 170):

```python
trade_date = date or datetime.today().strftime("%Y-%m-%d")
```

to:

```python
trade_date = _to_iso_date(date) if date else datetime.today().strftime("%Y-%m-%d")
```

The `datetime.today()` default already produces ISO; only the caller-supplied
branch needed normalizing. `_to_iso_date` is in the same module — no import
change.

**Verify**: `grep -n "_to_iso_date(date) if date" app/agents/trader/trader_agent.py`
→ returns **three** matches (record_buy, record_sell, correct_trade).

### Step 2: Add tests

In `tests/test_trader_agent.py`:

```python
def test_record_buy_normalizes_ddmmyyyy_to_iso(tmp_path: Path) -> None:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 5.0, 100.0, "15/03/2024")

    latest = agent.get_latest_trade("AAPL")
    assert latest is not None
    assert latest.date == "2024-03-15"  # stored ISO, not "15/03/2024"


def test_replay_correct_when_record_buy_uses_ddmmyyyy(tmp_path: Path) -> None:
    # A DD/MM/YYYY manual BUY (earlier) and an ISO manual SELL (later) for the
    # same ticker must replay chronologically: BUY 10 then SELL 4 => 6 shares.
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent.record_buy("AAPL", 10.0, 100.0, "01/02/2024")   # -> 2024-02-01
    agent.record_sell("AAPL", 4.0, 110.0, "2024-03-15")   # later

    portfolio = agent.get_portfolio()
    assert len(portfolio) == 1
    assert portfolio[0].shares == 6.0
    assert portfolio[0].entry_date == "2024-02-01"
```

**Verify**: `uv run pytest tests/test_trader_agent.py::test_record_buy_normalizes_ddmmyyyy_to_iso tests/test_trader_agent.py::test_replay_correct_when_record_buy_uses_ddmmyyyy -v`
→ 2 passed.

### Step 3: Full suite, lint, format

**Verify**:
- `uv run pytest` → all pass (the existing trader tests must still pass — the ISO
  default path is unchanged for callers that already pass ISO).
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage the 2
  in-scope files only).

## Test plan

Two new tests in `tests/test_trader_agent.py`:
- `record_buy` with a `DD/MM/YYYY` date stores ISO (the direct unit assertion).
- a `DD/MM/YYYY` manual BUY + an ISO manual SELL replay in correct chronological
  order (the end-to-end guard that the invariant holds across the manual path).

Both **fail before** Step 1 (the date is stored unmodified) and **pass after**.
Verification: `uv run pytest tests/test_trader_agent.py -v` → all pass.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -c "_to_iso_date(date) if date" app/agents/trader/trader_agent.py` returns `3`
- [ ] `uv run pytest tests/test_trader_agent.py -v` → all pass, including the 2 new tests
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `git status` shows only the 2 in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `trader_agent.py` changed since `dbf0d18` and the three
  `trade_date = date or ...` lines are no longer at 108/140/170 or differ in form
  — re-locate them by content; if `_to_iso_date` no longer exists, STOP (a
  prerequisite is missing).
- `test_record_buy_normalizes_ddmmyyyy_to_iso` passes BEFORE Step 1 — that would
  mean dates are already normalized somewhere and this plan is redundant; report
  it.

## Maintenance notes

- After this, the write boundary for trade dates is uniformly ISO regardless of
  caller (web form, direct API, import). `_to_iso_date` is the single helper any
  new trade-write path should route through.
- A reviewer should confirm the `datetime.today()` default branch is untouched
  (it already emits ISO) and that `import_sipp` was not modified (it normalizes
  independently).
- `_to_iso_date` logs a warning and returns the input unchanged on an
  unrecognized format — so a truly malformed date still stores as-given but is
  visible in logs, rather than silently corrupting the sort. That is the intended
  trade-off.
