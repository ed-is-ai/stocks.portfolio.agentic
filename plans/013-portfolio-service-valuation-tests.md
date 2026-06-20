# Plan 013: Characterize the portfolio valuation / GBP-conversion math with tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dbf0d18..HEAD -- app/services/portfolio_service.py app/schemas/trade.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: 011 (so the full suite runs clean)
- **Category**: tests
- **Planned at**: commit `dbf0d18`, 2026-06-19

## Why this matters

`app/services/portfolio_service.py` (309 lines) owns the money math the user
actually sees on the portfolio page: GBP/USD conversion and the aggregate
"total cost", "total value", and "P&L" summary cards. **No test imports
`app.services.*`** today — this logic is entirely unverified. A regression in
the FX conversion or the total/P&L arithmetic would silently show wrong money.
This plan pins the current correct behavior so future changes (including plan
015, which refactors the same file) have a safety net. It adds **tests only** —
no production change.

## Current state

- `PortfolioService.__init__(self, trader, evaluator=None)` — takes a
  `TraderService` and an optional `ExitEvaluator`
  (`app/services/portfolio_service.py:33-39`).
- Pure FX helper (`portfolio_service.py:88-91`):
  ```python
  @staticmethod
  def _to_gbp(amount: float, currency: str, gbpusd: float) -> float:
      """Convert amount to GBP using current rate if currency is USD."""
      return amount / gbpusd if currency == "USD" else amount
  ```
- Static price extractor (`portfolio_service.py:51-54`):
  ```python
  @staticmethod
  def current_prices(records: list[StockRecord]) -> dict[str, float]:
      return {r.ticker: r.price for r in records}
  ```
- The aggregate math lives in `portfolio_partial_context`
  (`portfolio_service.py:228-291`). Given `positions`, `gbpusd_rate`, and
  `cash_balance`, it computes:
  - `total_cost_gbp` = Σ `_to_gbp(p.total_cost, p.price_currency, fx)` over **all**
    positions, **plus** `cash_balance`,
  - `total_value_gbp` = Σ `_to_gbp(p.current_value, …, fx)` over positions that
    **have** a `current_value`, **plus** `cash_balance`,
  - `total_cost_gbp_valued` = same cost sum but only over valued positions (no cash),
  - `total_pnl_gbp` = `total_value_gbp - total_cost_gbp_valued - (cash_balance or 0)`.
  - `fx = gbpusd_rate or _DEFAULT_GBPUSD` (`_DEFAULT_GBPUSD = 1.35`).
- The method also reads files / trader state via `self.load_analysis()`,
  `self._load_portfolio_history()`, and `self._trade_markers()` — these must be
  stubbed in tests so the valuation math is isolated (see Step 2).

The `Position` schema (`app/schemas/trade.py:37-57`) requires `ticker`, `shares`,
`avg_cost`, `total_cost`; everything else has a default. Relevant optionals:
`current_value: float | None = None`, `price_currency: str = "GBP"`.

### Test conventions to follow

Tests live in `tests/`, use `pytest`, plain functions with `tmp_path` /
`monkeypatch` fixtures (see `tests/test_trader_agent.py` for the house style).
For pure assertions on floats, the values in this plan are chosen to be exact
(no rounding needed).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest tests/test_portfolio_service.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |

## Scope

**In scope** (the only file you should modify):
- `tests/test_portfolio_service.py` (create)

**Out of scope** (do NOT touch):
- `app/services/portfolio_service.py` — characterize current behavior; do not
  "fix" anything. If a value looks wrong, STOP and report (see STOP conditions).
- Any other source file.

## Git workflow

- Branch: `advisor/013-portfolio-service-valuation-tests`
- Commit message: `test(services): characterize portfolio GBP valuation math`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Test the pure helpers

Create `tests/test_portfolio_service.py`:

```python
from app.schemas.record import StockRecord
from app.schemas.trade import Position
from app.services.portfolio_service import PortfolioService


def test_to_gbp_converts_usd_and_passes_through_gbp() -> None:
    assert PortfolioService._to_gbp(200.0, "USD", 2.0) == 100.0
    assert PortfolioService._to_gbp(100.0, "GBP", 2.0) == 100.0


def test_current_prices_maps_ticker_to_price() -> None:
    records = [
        StockRecord(ticker="AAA", price=10.0),
        StockRecord(ticker="BBB", price=20.0),
    ]
    assert PortfolioService.current_prices(records) == {"AAA": 10.0, "BBB": 20.0}
```

Before writing the `current_prices` test, confirm `StockRecord`'s required
fields: run `uv run python -c "from app.schemas.record import StockRecord; import inspect; print(StockRecord.model_fields.keys())"`.
If `StockRecord(ticker=..., price=...)` fails validation because other fields are
required, construct it with the minimal required set (the executor adds the
missing required fields with simple placeholder values) — the assertion only
cares about `ticker` and `price`.

**Verify**: `uv run pytest tests/test_portfolio_service.py -v` → 2 passed.

### Step 2: Characterize the aggregate GBP totals

Add stubs and a test that isolates the valuation math from file/trader I/O:

```python
class _StubTrader:
    def get_trade_history(self, ticker=None):
        return []


class _StubEvaluator:
    def evaluate(self, position, stock):
        return None


def _make_service(monkeypatch) -> PortfolioService:
    svc = PortfolioService(_StubTrader(), _StubEvaluator())
    # isolate from disk / analysis state
    monkeypatch.setattr(svc, "load_analysis", lambda: [])
    monkeypatch.setattr(
        svc,
        "_load_portfolio_history",
        lambda: {"labels": [], "values": [], "costs": [], "cash_values": []},
    )
    return svc


def test_portfolio_totals_convert_usd_and_include_cash(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    positions = [
        Position(
            ticker="GBPCO", shares=1, avg_cost=100, total_cost=100,
            current_value=150, price_currency="GBP",
        ),
        Position(
            ticker="USDCO", shares=1, avg_cost=200, total_cost=200,
            current_value=270, price_currency="USD",
        ),
    ]
    ctx = svc.portfolio_partial_context(
        positions, gbpusd_rate=2.0, cash_balance=1000.0
    )
    # GBP cost 100; USD cost 200/2=100; + cash 1000 => 1200
    assert ctx["total_cost_gbp"] == 1200.0
    # GBP value 150; USD value 270/2=135; + cash 1000 => 1285
    assert ctx["total_value_gbp"] == 1285.0
    # valued cost (no cash): 100 + 100 = 200
    assert ctx["total_cost_gbp_valued"] == 200.0
    # pnl: 1285 - 200 - 1000 = 85  (GBP pos +50, USD pos +35)
    assert ctx["total_pnl_gbp"] == 85.0
    assert ctx["cash_balance"] == 1000.0


def test_position_without_current_value_excluded_from_value_totals(monkeypatch) -> None:
    svc = _make_service(monkeypatch)
    positions = [
        Position(
            ticker="VALUED", shares=1, avg_cost=100, total_cost=100,
            current_value=120, price_currency="GBP",
        ),
        Position(
            ticker="NOPRICE", shares=1, avg_cost=50, total_cost=50,
            current_value=None, price_currency="GBP",
        ),
    ]
    ctx = svc.portfolio_partial_context(positions, gbpusd_rate=2.0, cash_balance=0.0)
    # cost includes BOTH positions: 100 + 50 = 150
    assert ctx["total_cost_gbp"] == 150.0
    # value includes only the valued one: 120
    assert ctx["total_value_gbp"] == 120.0
    # valued cost only the valued one: 100; pnl = 120 - 100 - 0 = 20
    assert ctx["total_pnl_gbp"] == 20.0
```

**Verify**: `uv run pytest tests/test_portfolio_service.py -v` → 4 passed total.

### Step 3: Full suite, lint, format

**Verify**:
- `uv run pytest` → all pass (4 new included).
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage the test
  file only).

## Test plan

`tests/test_portfolio_service.py` with 4 tests:
- `_to_gbp` USD conversion + GBP pass-through (pure),
- `current_prices` mapping (pure),
- aggregate totals with a USD position + cash (the core money math),
- a position lacking `current_value` is counted in cost but excluded from value/P&L.

Verification: `uv run pytest tests/test_portfolio_service.py -v` → 4 passed.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_portfolio_service.py -v` → 4 passed
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `git status` shows only `tests/test_portfolio_service.py` added
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Any of the four assertions FAILS on current code. The expected values were
  derived by hand from the formulas in "Current state"; a mismatch means either
  the math differs from what this plan documented or a real bug exists — report
  the actual values, do NOT change production code or bend the assertion to pass.
- `Position(...)` or `StockRecord(...)` raises a validation error for missing
  required fields — add the minimal required fields and note what they were.
- `portfolio_partial_context` calls additional un-stubbed methods that touch the
  network or filesystem (the run hangs or hits real I/O) — report which method.

## Maintenance notes

- These tests stub `load_analysis` and `_load_portfolio_history`; if those method
  names change, the stubs must follow. They deliberately do not cover the chart
  serialization or trade-marker placement — those are presentation details, lower
  value, and can be a follow-up.
- Plan 015 refactors `fetch_all_prices` in this same file. These tests do not
  cover `fetch_all_prices` (it is network I/O) but they guarantee the valuation
  math around it is unchanged — a useful guard while 015 lands.
