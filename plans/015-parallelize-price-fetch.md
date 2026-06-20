# Plan 015: Parallelize per-ticker price fetching

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dbf0d18..HEAD -- app/services/portfolio_service.py`
> If the file changed since this plan was written, compare the "Current state"
> excerpt against the live code before proceeding; on a mismatch, treat it as a
> STOP condition.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: MED
- **Depends on**: 013 (its valuation tests guard the rest of this file). 011 for a
  clean suite.
- **Category**: performance
- **Planned at**: commit `dbf0d18`, 2026-06-19

## Why this matters

On every portfolio price refresh, `PortfolioService.fetch_all_prices` fetches
each holding's price **sequentially**, and each holding triggers multiple
yfinance network round-trips (`yf.download` plus a `fast_info.currency` lookup,
plus a `.L` fallback fetch when the first attempt misses). For a portfolio of N
holdings that is ~2–4N sequential network calls — the refresh wall-clock grows
linearly with the portfolio and is dominated by network latency, not CPU. These
calls are independent and I/O-bound, so running them concurrently with a bounded
thread pool cuts the wall-clock to roughly the slowest single ticker without
changing any result. This is a pure latency win; the computed prices are
identical.

## Current state

`app/services/portfolio_service.py:132-152` — the sequential loop:

```python
def fetch_all_prices(
    self, tickers: list[str], aliases: dict[str, str], gbpusd: float
) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
    """Fetch GBP-normalised prices for all portfolio tickers."""
    gbp_prices: dict[str, float] = {}
    display_info: dict[str, tuple[float, str]] = {}
    for t in tickers:
        yf_sym = aliases.get(t, t)
        result = self._fetch_price_gbp(yf_sym, gbpusd)
        if (result is None or result[0] < 0.01) and t not in aliases:
            result = self._fetch_price_gbp(f"{t}.L", gbpusd)
        if result is not None and result[0] >= 0.01:
            gbp_price, orig_price, currency = result
            gbp_prices[t] = gbp_price
            display_info[t] = (orig_price, currency)
    return gbp_prices, display_info
```

`_fetch_price_gbp(self, yf_sym, gbpusd)` (lines 95-130) returns
`(gbp_price, orig_price, currency)` or `None`, and does the actual network I/O.
It is the unit of work to parallelize — **do not change its body**.

Result order does not matter: the outputs are dicts keyed by ticker.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest tests/test_portfolio_service.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |

## Scope

**In scope** (the only files you should modify):
- `app/services/portfolio_service.py` (rewrite the `fetch_all_prices` loop body
  to use a bounded thread pool; extract the per-ticker resolution into a local
  helper)
- `tests/test_portfolio_service.py` (add tests — this file is created by plan 013;
  if it does not exist yet, create it)

**Out of scope** (do NOT touch):
- `_fetch_price_gbp` — its per-ticker logic (the `.L` semantics, currency
  handling, the `>= 0.01` threshold) must be preserved exactly.
- Any change to the returned data shape or the alias/retry semantics.

## Git workflow

- Branch: `advisor/015-parallelize-price-fetch`
- Commit message: `perf(portfolio): fetch holding prices concurrently`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Replace the sequential loop with a bounded thread pool

In `app/services/portfolio_service.py`, add at the top of the module:
```python
from concurrent.futures import ThreadPoolExecutor
```

Rewrite `fetch_all_prices` so the **per-ticker resolution is unchanged** but runs
concurrently. Extract the existing per-ticker logic into a closure that returns
`(ticker, gbp_price, orig_price, currency)` or `None`, then map it over the
tickers with a bounded pool and assemble the dicts from the results:

```python
def fetch_all_prices(
    self, tickers: list[str], aliases: dict[str, str], gbpusd: float
) -> tuple[dict[str, float], dict[str, tuple[float, str]]]:
    """Fetch GBP-normalised prices for all portfolio tickers (concurrently)."""

    def _resolve(t: str) -> tuple[str, float, float, str] | None:
        yf_sym = aliases.get(t, t)
        result = self._fetch_price_gbp(yf_sym, gbpusd)
        if (result is None or result[0] < 0.01) and t not in aliases:
            result = self._fetch_price_gbp(f"{t}.L", gbpusd)
        if result is not None and result[0] >= 0.01:
            gbp_price, orig_price, currency = result
            return t, gbp_price, orig_price, currency
        return None

    gbp_prices: dict[str, float] = {}
    display_info: dict[str, tuple[float, str]] = {}
    if not tickers:
        return gbp_prices, display_info
    with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
        for res in pool.map(_resolve, tickers):
            if res is not None:
                t, gbp_price, orig_price, currency = res
                gbp_prices[t] = gbp_price
                display_info[t] = (orig_price, currency)
    return gbp_prices, display_info
```

`max_workers=min(8, len(tickers))` caps concurrency so a large watchlist does not
open an unbounded number of network connections.

**Verify**: `grep -n "ThreadPoolExecutor" app/services/portfolio_service.py` →
shows the import and the use in `fetch_all_prices`.

### Step 2: Add tests that mock the network and pin behavior

In `tests/test_portfolio_service.py` (created by plan 013; if absent, create it
with the same imports). These tests monkeypatch `_fetch_price_gbp` so **no
network** happens, and assert the assembly, the `.L` retry, and the threshold
filtering are preserved:

```python
def test_fetch_all_prices_assembles_results(monkeypatch) -> None:
    svc = PortfolioService(_StubTrader(), _StubEvaluator())

    def fake_fetch(yf_sym, gbpusd):
        # GBP prices keyed off the symbol; ignore gbpusd here
        table = {"AAA": (10.0, 10.0, "GBP"), "BBB": (20.0, 20.0, "GBP")}
        return table.get(yf_sym)

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["AAA", "BBB"], {}, 1.35)
    assert prices == {"AAA": 10.0, "BBB": 20.0}
    assert display == {"AAA": (10.0, "GBP"), "BBB": (20.0, "GBP")}


def test_fetch_all_prices_retries_with_london_suffix(monkeypatch) -> None:
    svc = PortfolioService(_StubTrader(), _StubEvaluator())
    calls: list[str] = []

    def fake_fetch(yf_sym, gbpusd):
        calls.append(yf_sym)
        if yf_sym == "VOD":
            return None  # primary miss
        if yf_sym == "VOD.L":
            return (1.23, 123.0, "GBP")  # .L succeeds
        return None

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    prices, display = svc.fetch_all_prices(["VOD"], {}, 1.35)
    assert prices == {"VOD": 1.23}
    assert "VOD.L" in calls  # the .L fallback ran


def test_fetch_all_prices_drops_below_threshold(monkeypatch) -> None:
    svc = PortfolioService(_StubTrader(), _StubEvaluator())

    def fake_fetch(yf_sym, gbpusd):
        return (0.0, 0.0, "GBP")  # below the 0.01 threshold

    monkeypatch.setattr(svc, "_fetch_price_gbp", fake_fetch)
    # alias present so the .L retry is skipped (matches `t not in aliases`)
    prices, display = svc.fetch_all_prices(["ZZZ"], {"ZZZ": "ZZZ"}, 1.35)
    assert prices == {}
    assert display == {}
```

`_StubTrader` and `_StubEvaluator` are defined in `tests/test_portfolio_service.py`
by plan 013. If you are creating the file fresh, add minimal versions:
```python
class _StubTrader:
    def get_trade_history(self, ticker=None):
        return []

class _StubEvaluator:
    def evaluate(self, position, stock):
        return None
```

**Verify**: `uv run pytest tests/test_portfolio_service.py -v` → all pass
(plan-013 tests, if present, plus these 3).

### Step 3: Full suite, lint, format

**Verify**:
- `uv run pytest` → all pass.
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage only
  in-scope files).

## Test plan

Three new tests in `tests/test_portfolio_service.py`, all monkeypatching
`_fetch_price_gbp` (no network):
- assembles multiple tickers into the price + display dicts,
- the `.L` fallback still runs when the primary symbol misses and there is no alias,
- a price below the `0.01` threshold is dropped.

These pin that the parallel version preserves the exact alias/retry/threshold
semantics of the sequential one. Verification:
`uv run pytest tests/test_portfolio_service.py -v`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -n "ThreadPoolExecutor" app/services/portfolio_service.py` returns the import + use
- [ ] `uv run pytest tests/test_portfolio_service.py -v` → all pass (the 3 new included)
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `git status` shows only the 2 in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `portfolio_service.py` changed since `dbf0d18` and the
  `fetch_all_prices` excerpt no longer matches.
- A test reveals the parallel version produces a different result than the
  documented sequential semantics (e.g. the `.L` retry no longer fires) — report
  it; do not paper over it.
- `pool.map` surfaces an exception from `_fetch_price_gbp` that the sequential
  version would have swallowed — note it; the per-ticker function already returns
  `None` on miss, so a raised exception is a real change worth reporting.

## Maintenance notes

- This keeps `_fetch_price_gbp` as the single per-ticker unit of work; any future
  change to retry/currency logic stays there and is automatically parallelized.
- yfinance calls are I/O-bound; `ThreadPoolExecutor` (not processes) is correct.
  `max_workers` is capped at 8 — raise it only if profiling shows the network can
  take more concurrency without rate-limiting.
- A bigger win (a single batched `yf.download` for all symbols) was considered but
  deliberately deferred: it complicates the per-symbol currency lookup and the
  `.L` fallback, raising risk for a portfolio whose N is small. Revisit only if N
  grows large enough that even 8-way concurrency is too slow.
