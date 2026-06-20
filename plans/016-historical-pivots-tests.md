# Plan 016: Cover the historical-pivots detection math with unit tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 13074c8..HEAD -- app/agents/analyst/historical_pivots.py`
> If `historical_pivots.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `13074c8`, 2026-06-20

## Why this matters

`app/agents/analyst/historical_pivots.py` is ~460 lines of pure, deterministic
algorithm — peak detection, base construction, breakout confirmation, Stage-2
filtering, dedup, and open-base detection. It produces the historical
base/pivot levels the analyst agent surfaces (`analyst_agent.py:903` calls
`find_historical_pivots`). It has **zero test coverage today**. Every helper is
a pure function over a pandas DataFrame, so a bug (off-by-one window, wrong
threshold comparison, dedup keeping the wrong pivot) would change trading
signals silently and nothing would catch it. This plan pins the current
behavior with fast, network-free unit tests, creating the safety net any future
tuning of these thresholds will need.

## Current state

File: `app/agents/analyst/historical_pivots.py`. All functions operate on a
weekly OHLCV DataFrame with **lowercase** columns (`high`, `low`, `close`) and a
**DatetimeIndex**. Module-level constants (do not change them; tests rely on
these exact values):

```python
_HALF_WINDOW = 10          # weeks each side to qualify a local high as a peak
_MIN_PROMINENCE = 0.08     # peak must be >= 8% above the window's lowest low
_MIN_BASE_WEEKS = 6        # consolidation must last at least 6 weeks
_MAX_BASE_DEPTH = 35.0     # base cannot drop more than 35% from pivot (%)
_MIN_ADVANCE_PCT = 8.0     # advance after breakout must be >= 8% to confirm
_BREAKOUT_SEARCH_WEEKS = 26
_ADVANCE_WINDOW_WEEKS = 52
_DEDUP_BAND = 0.05         # merge pivots within 5% of each other
_BREAKOUT_THRESHOLD = 1.005  # close must be > pivot * this to count as breakout
_MIN_HISTORY_BARS = 60
```

The functions under test (signatures exactly as they exist today):

- `fetch_weekly_ohlcv(ticker, period="10y") -> pd.DataFrame` — the **only**
  network function (calls `yf.download`). Do NOT call it in tests; monkeypatch
  it (see Step 6).
- `_find_peak_candidates(weekly, half_window=_HALF_WINDOW) -> list[int]` — row
  indices where `high[i]` is the max of its `±half_window` window AND prominence
  `(high[i] - window_lows.min()) / high[i] >= _MIN_PROMINENCE`. Skips a bar when
  `high[i] == high[i-1]` (flat-top dedup). Loop range is
  `range(half_window, n - half_window)`, so the first/last `half_window` bars
  can never be peaks.
- `_build_base(weekly, peak_idx, max_depth_pct=35.0, min_base_weeks=6) -> dict | None`
  — walks back from `peak_idx` while `low[i] >= pivot*(1 - max_depth_pct/100)`;
  returns `None` if `base_weeks < min_base_weeks`. On success returns a dict with
  keys `base_start_idx, base_end_idx, base_start, base_end, base_weeks,
  max_depth_pct`.
- `_find_breakout(weekly, peak_idx, pivot_price, ...) -> dict` — always returns a
  dict. Searches forward up to `_BREAKOUT_SEARCH_WEEKS` for a close
  `>= pivot * _BREAKOUT_THRESHOLD`. If none found → `status="resistance"`,
  `breakout_date=None`. If found → `advance_pct` over the next
  `_ADVANCE_WINDOW_WEEKS`; `status="confirmed"` when `advance_pct >= 8.0`, else
  `"failed"`.
- `_is_stage2_at_pivot(weekly, peak_idx) -> bool` — `False` when `peak_idx < 39`
  (insufficient SMA40 history); otherwise `close[peak_idx] > mean(close[peak_idx-39 : peak_idx+1])`.
- `_deduplicate_pivots(pivots, band_pct=0.05) -> list[dict]` — `[]` for empty
  input. Sorts by `pivot_price`; merges entries within `band_pct`, keeping the
  one with the higher `advance_pct` (treating `None` advance as `0.0`); returns
  sorted by `base_start`. Each input dict must have `pivot_price`, `base_start`,
  and optionally `advance_pct`.
- `_detect_open_base(weekly, confirmed_pivots, ...) -> dict | None` — finds the
  highest high in the trailing ≤52 weeks; returns `None` if price already broke
  out above it, if the base is shorter than `min_base_weeks`, or if it duplicates
  a confirmed pivot within `_DEDUP_BAND`. On success returns a dict with
  `status="open"` and `breakout_date=None`.
- `find_historical_pivots(ticker, period="10y", ..., require_stage2=True) -> list[dict]`
  — orchestrates all of the above; calls `fetch_weekly_ohlcv` first. Each result
  dict has keys: `pivot_price, base_start, base_end, base_weeks, max_depth_pct,
  breakout_date, breakout_close, advance_pct, stage2_at_pivot, status`.

### Test conventions in this repo (match these)

- Tests live in `tests/`, named `test_<module>.py`, using **plain pytest**
  functions or simple classes — no unittest. See `tests/test_exit_evaluator.py`
  for the style (module-level `_make_*` helper builders, one assert-focused test
  per behavior).
- Synthetic pandas data uses a `pd.date_range(...)` DatetimeIndex — see the
  `sample_stock_data` fixture in `tests/conftest.py:11-24` (note it uses
  **lowercase** column names and `freq="W"`, exactly what these functions need).
- Monkeypatching yfinance is done with `monkeypatch.setattr` — see the
  `mock_yfinance` fixture in `tests/conftest.py:64-82`.

## Commands you will need

| Purpose   | Command                                            | Expected on success      |
|-----------|----------------------------------------------------|--------------------------|
| Run new tests | `uv run pytest tests/test_historical_pivots.py -q` | all pass             |
| Full suite | `uv run pytest -q`                                 | all pass (was 98)        |
| Typecheck | `uv run pyrefly check`                              | exit 0 (pre-existing warnings OK if check passes) |
| Lint      | `uv run ruff check tests/test_historical_pivots.py` | exit 0                  |
| Format    | `uv run ruff format tests/test_historical_pivots.py` | reformats, exit 0      |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_historical_pivots.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/agents/analyst/historical_pivots.py` — this is a *characterization* plan.
  You are pinning current behavior, not changing it. If a test reveals what looks
  like a bug, write the test to assert the **actual current** behavior and note
  it in a `# NOTE:` comment — do not "fix" the source here.
- Any other `app/` or `tests/` file.

## Git workflow

- Branch: `advisor/016-historical-pivots-tests`
- Conventional-commit style (matches `git log`), e.g.
  `test(analyst): characterize historical-pivots detection math`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file with a synthetic-frame builder

Create `tests/test_historical_pivots.py`. Add a module docstring and a helper
that builds a weekly DataFrame with the exact shape these functions expect:

```python
"""Unit tests for historical base-pivot detection (pure DataFrame math)."""

from __future__ import annotations

import pandas as pd

from app.agents.analyst import historical_pivots as hp


def _weekly(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    start: str = "2020-01-06",
) -> pd.DataFrame:
    """Build a weekly OHLCV frame with lowercase cols and a DatetimeIndex."""
    n = len(highs)
    assert len(lows) == n and len(closes) == n
    idx = pd.date_range(start=start, periods=n, freq="W")
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes}, index=idx
    )
```

**Verify**: `uv run pytest tests/test_historical_pivots.py -q`
→ collects 0 tests, exits 0 (no errors). (No tests yet — this just confirms the
imports and helper parse.)

### Step 2: Test `_find_peak_candidates`

Add tests covering:
- **A single clear peak**: build ≥21 bars (so the loop range is non-empty) that
  rise to one high in the middle then fall, with the window low ≥8% below the
  peak. Assert the returned list contains the peak's index and that every
  returned index is in `range(_HALF_WINDOW, n - _HALF_WINDOW)`.
- **Flat data → no peaks**: all highs equal, all lows equal → prominence is 0,
  expect `[]`.
- **Insufficient prominence**: a local max whose window low is only ~2% below it
  (< `_MIN_PROMINENCE`) → that index is not returned.

Construction hint: with `_HALF_WINDOW = 10`, you need at least 21 bars and the
peak must sit at index ≥10 and ≤ n-11. A simple recipe: 11 rising bars, a peak,
then 11 falling bars (n = 23, peak at index 11), with lows set ~15% below the
peak high inside the window.

**Verify**: `uv run pytest tests/test_historical_pivots.py -q -k peak`
→ all pass.

### Step 3: Test `_build_base`

- **Valid base** (≥6 weeks where lows stay above the depth floor): assert the
  returned dict's `base_weeks == peak_idx - base_start_idx`, `base_start_idx <
  peak_idx`, and that `base_start`/`base_end` are ISO date strings
  (`len(...) == 10`, contains `-`).
- **Too-short base → None**: a peak with a deep low (below
  `pivot*(1-0.35)`) immediately before it, so the walk-back stops after <6 weeks
  → expect `None`.

**Verify**: `uv run pytest tests/test_historical_pivots.py -q -k base`
→ all pass.

### Step 4: Test `_find_breakout`, `_is_stage2_at_pivot`, `_deduplicate_pivots`

`_find_breakout` (call with an explicit `pivot_price`):
- **Resistance**: closes after the peak never reach `pivot * 1.005` → dict has
  `status == "resistance"`, `breakout_date is None`, `advance_pct is None`.
- **Confirmed**: a close clears the threshold and a later close is ≥8% above the
  pivot → `status == "confirmed"`, `breakout_date` is a 10-char ISO string,
  `advance_pct >= 8.0`.
- **Failed**: a close just clears the threshold (e.g. pivot*1.006) but never
  advances 8% → `status == "failed"`.

`_is_stage2_at_pivot`:
- `peak_idx < 39` (e.g. 10) → `False` regardless of data.
- With ≥40 bars and `close[peak_idx]` above the trailing 40-bar mean → `True`;
  below it → `False`.

`_deduplicate_pivots`:
- Empty list → `[]`.
- Two pivots within 5% (`pivot_price` 100 and 103) where the second has the
  higher `advance_pct` → result length 1 and keeps the higher-advance pivot.
- Two pivots >5% apart (100 and 120) → both kept (length 2), sorted by
  `base_start`.

Each pivot dict you pass to `_deduplicate_pivots` needs at least
`{"pivot_price": ..., "base_start": "<iso>", "advance_pct": ...}`.

**Verify**: `uv run pytest tests/test_historical_pivots.py -q -k "breakout or stage2 or dedup"`
→ all pass.

### Step 5: Test `_detect_open_base`

- **Open base**: a frame whose trailing ≤52 weeks form a base of ≥6 weeks that
  has NOT broken out (no later close reaches `recent_high * 1.005`) → returns a
  dict with `status == "open"`, `breakout_date is None`, and a `pivot_price`.
- **Already broken out → None**: trailing data where a close after the recent
  high clears `recent_high * 1.005` → returns `None`.

**Verify**: `uv run pytest tests/test_historical_pivots.py -q -k open`
→ all pass.

### Step 6: Test `find_historical_pivots` end-to-end with a monkeypatched fetch

Do NOT hit the network. Monkeypatch the module's `fetch_weekly_ohlcv` so the
orchestration runs on a synthetic frame:

```python
def test_find_historical_pivots_runs_offline(monkeypatch):
    frame = _weekly(highs, lows, closes)  # craft ≥60 bars with one clear base
    monkeypatch.setattr(hp, "fetch_weekly_ohlcv", lambda ticker, period: frame)

    result = hp.find_historical_pivots("TEST", require_stage2=False)

    assert isinstance(result, list)
    for p in result:
        assert set(p) >= {
            "pivot_price", "base_start", "base_end", "base_weeks",
            "max_depth_pct", "breakout_date", "breakout_close",
            "advance_pct", "stage2_at_pivot", "status",
        }
```

Note: `find_historical_pivots` requires `len(frame) >= _MIN_HISTORY_BARS` is NOT
checked here (that guard lives in `fetch_weekly_ohlcv`, which you bypassed), but
`_is_stage2_at_pivot` needs `peak_idx >= 39` to ever return True — so build a
frame of **at least ~70 weekly bars** with the base/peak past index 40 if you
want a non-empty result with `require_stage2=False`. An empty `result` is also a
valid assertion target for a degenerate frame; prefer at least one test where
`result` is non-empty so the key-set assertion actually runs.

Also add: a flat/degenerate frame (no qualifying peaks) → `result == []`.

**Verify**: `uv run pytest tests/test_historical_pivots.py -q`
→ all pass.

### Step 7: Format, lint, typecheck, full suite

Run, in order:
- `uv run ruff format tests/test_historical_pivots.py`
- `uv run ruff check tests/test_historical_pivots.py` → exit 0
- `uv run pyrefly check` → exit 0
- `uv run pytest -q` → full suite green (98 prior + your new tests)

Then update this plan's row in `plans/README.md` to DONE.

## Test plan

- New file `tests/test_historical_pivots.py` with at least these cases:
  peak: single-peak/flat/low-prominence; base: valid/too-short; breakout:
  resistance/confirmed/failed; stage2: insufficient-history/above/below;
  dedup: empty/merge-within-band/keep-apart; open-base: open/already-broken-out;
  end-to-end: non-empty offline run (key-set check) + degenerate → `[]`.
- Structural pattern to follow: `tests/test_exit_evaluator.py` (helper builders +
  focused asserts) and the synthetic-frame style of `tests/conftest.py:11-24`.
- Verification: `uv run pytest tests/test_historical_pivots.py -q` → all pass;
  `uv run pytest -q` → full suite still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_historical_pivots.py -q` passes with ≥12 new tests
- [ ] `uv run pytest -q` exits 0 (no regression in the existing 98)
- [ ] `uv run pyrefly check` exits 0
- [ ] `uv run ruff check tests/test_historical_pivots.py` exits 0
- [ ] `git status` shows only `tests/test_historical_pivots.py` and
      `plans/README.md` modified/created
- [ ] `plans/README.md` status row for plan 016 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `historical_pivots.py` changed since `13074c8` and the
  function signatures or constants no longer match the "Current state" excerpts.
- You cannot construct synthetic data that makes a helper return its documented
  result after two reasonable attempts — report which helper and what you tried
  (the algorithm may behave differently than this plan describes; that itself is
  worth surfacing).
- A test you wrote to assert current behavior fails in a way that looks like a
  real source bug — capture the input and the actual-vs-expected, leave the test
  asserting the **actual** behavior with a `# NOTE:` comment, and report it.
- Any test requires network access (yfinance) — it must not; revisit the
  monkeypatch.

## Maintenance notes

- These are characterization tests: they encode *current* behavior, including any
  quirks. If a future change intentionally retunes a threshold
  (`_MIN_PROMINENCE`, `_MAX_BASE_DEPTH`, `_BREAKOUT_THRESHOLD`, etc.), the
  matching test must be updated deliberately and the change called out in review.
- A reviewer should check that no test reaches the network and that asserted
  values were derived from the synthetic input, not copy-pasted from a flaky run.
- Deferred: testing `_print_pivots`/`main` (CLI/stdout formatting) — low value,
  left out on purpose.
