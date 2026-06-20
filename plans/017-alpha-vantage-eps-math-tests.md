# Plan 017: Pin the Alpha Vantage EPS-growth math with unit tests

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 13074c8..HEAD -- app/integrations/alpha_vantage.py`
> If `alpha_vantage.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `13074c8`, 2026-06-20

## Why this matters

`AlphaVantageClient.get_quarterly_eps_growth` and `get_annual_eps_cagr` compute
the EPS-growth fundamentals the scanner uses as a fallback when yfinance is
missing data. They are real financial calculations with subtle correctness
guards — fewer than 5 quarters → `None`, non-positive base-year EPS → `None`,
unparseable strings → `None`, and a CAGR formula with an `n-1` exponent that is
easy to get wrong. None of this is tested. A regression here would feed wrong
growth numbers into stock scoring silently. These methods are pure given the
JSON dict from `get_earnings`, so they test cleanly by stubbing that one method —
no network, no API key.

## Current state

File: `app/integrations/alpha_vantage.py`. The two methods under test
(`alpha_vantage.py:126-170`), reproduced exactly:

```python
def get_quarterly_eps_growth(self, symbol: str) -> float | None:
    """Return MRQ YoY EPS growth from EARNINGS endpoint. ..."""
    earnings = self.get_earnings(symbol)
    if not earnings:
        return None
    quarters: list[dict[str, str]] = earnings.get("quarterlyEarnings", [])
    if len(quarters) < 5:
        return None
    try:
        recent = float(quarters[0].get("reportedEPS", "None"))
        year_ago = float(quarters[4].get("reportedEPS", "None"))
    except (ValueError, TypeError):
        return None
    if year_ago <= 0:
        return None
    return round((recent - year_ago) / year_ago, 4)

def get_annual_eps_cagr(self, symbol: str) -> float | None:
    """Return 3-year annual EPS CAGR from EARNINGS endpoint. ..."""
    earnings = self.get_earnings(symbol)
    if not earnings:
        return None
    annual: list[dict[str, str]] = earnings.get("annualEarnings", [])
    n = min(len(annual), 4)
    if n < 3:
        return None
    try:
        newest = float(annual[0].get("reportedEPS", "None"))
        oldest = float(annual[n - 1].get("reportedEPS", "None"))
    except (ValueError, TypeError):
        return None
    if oldest <= 0 or newest <= 0:
        return None
    cagr = (newest / oldest) ** (1 / (n - 1)) - 1
    return round(cagr, 4)
```

Key facts the tests rely on:
- Quarterly growth uses index `0` (most recent) vs index `4` (year-ago);
  formula `(recent - year_ago) / year_ago`, rounded to 4 dp.
- Annual CAGR uses `n = min(len(annual), 4)`, base at index `n-1`, newest at
  index `0`, exponent `1/(n-1)`, rounded to 4 dp.
- Both call `self.get_earnings(symbol)` first — **stub that method** in tests so
  no network/API key is needed. `get_earnings` returns the parsed JSON dict (or
  `None`).
- The constructor reads `ALPHA_VANTAGE_API_KEY` from env; the `enabled` property
  is `bool(self.api_key)`. Construct with an explicit key, e.g.
  `AlphaVantageClient(api_key="test")`, to avoid env dependence (the methods
  under test don't check `enabled` themselves — `get_earnings` does, but you're
  stubbing it).

### Test conventions in this repo (match these)

- `tests/test_<module>.py`, plain pytest. See `tests/test_exit_evaluator.py` for
  the helper-builder + focused-assert style.
- Stubbing a method: use `monkeypatch.setattr(client, "get_earnings", lambda symbol: {...})`
  or assign `client.get_earnings = lambda symbol: {...}`. Prefer `monkeypatch`.
- Float comparisons: use `pytest.approx` or compare the already-rounded value
  exactly (these methods round to 4 dp, so exact compare on the rounded result
  is fine, e.g. `assert client.get_quarterly_eps_growth("X") == 0.5`).

## Commands you will need

| Purpose   | Command                                          | Expected on success |
|-----------|--------------------------------------------------|---------------------|
| Run new tests | `uv run pytest tests/test_alpha_vantage.py -q` | all pass          |
| Full suite | `uv run pytest -q`                              | all pass (was 98)   |
| Typecheck | `uv run pyrefly check`                            | exit 0              |
| Lint      | `uv run ruff check tests/test_alpha_vantage.py`  | exit 0              |
| Format    | `uv run ruff format tests/test_alpha_vantage.py` | reformats, exit 0   |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_alpha_vantage.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/integrations/alpha_vantage.py` — characterization only; do not change the
  source. If a test reveals a likely bug, assert the **actual current** behavior,
  add a `# NOTE:` comment, and report it.
- `_get`, `get_overview`, `get_fundamentals` and the network path — not in scope.
  Only the two EPS-math methods (via a stubbed `get_earnings`).

## Git workflow

- Branch: `advisor/017-alpha-vantage-eps-math-tests`
- Conventional-commit style, e.g.
  `test(integrations): characterize Alpha Vantage EPS growth math`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file and a quarter/annual builder

Create `tests/test_alpha_vantage.py`:

```python
"""Unit tests for AlphaVantageClient EPS-growth math (no network)."""

from __future__ import annotations

from app.integrations.alpha_vantage import AlphaVantageClient


def _client_with_earnings(earnings, monkeypatch) -> AlphaVantageClient:
    client = AlphaVantageClient(api_key="test")
    monkeypatch.setattr(client, "get_earnings", lambda symbol: earnings)
    return client


def _quarters(*eps_values: str) -> dict:
    return {"quarterlyEarnings": [{"reportedEPS": v} for v in eps_values]}


def _annual(*eps_values: str) -> dict:
    return {"annualEarnings": [{"reportedEPS": v} for v in eps_values]}
```

**Verify**: `uv run pytest tests/test_alpha_vantage.py -q`
→ collects 0 tests, exits 0.

### Step 2: Test `get_quarterly_eps_growth`

Cover:
- **Happy path**: quarters `["1.50", "x", "x", "x", "1.00"]` (index 0 = 1.50,
  index 4 = 1.00) → `(1.50 - 1.00)/1.00 = 0.5`. Fill the in-between values with
  any parseable string; only indices 0 and 4 matter. Assert `== 0.5`.
- **Fewer than 5 quarters → None**: pass 4 quarters.
- **Year-ago EPS ≤ 0 → None**: index 4 = `"0"` (or negative).
- **Unparseable EPS → None**: index 0 = `"None"` or `""`.
- **`get_earnings` returns None → None**: stub it to return `None`.
- **Negative growth**: index 0 = `"0.50"`, index 4 = `"1.00"` → `-0.5`.

**Verify**: `uv run pytest tests/test_alpha_vantage.py -q -k quarterly`
→ all pass.

### Step 3: Test `get_annual_eps_cagr`

Cover:
- **Happy path, 4 years**: annual `["8.00", "x", "x", "1.00"]` → `n=4`,
  `(8.00/1.00) ** (1/3) - 1 = 1.0` exactly → assert `== 1.0`.
- **Exactly 3 years**: annual `["4.00", "x", "1.00"]` → `n=3`,
  `(4.00/1.00) ** (1/2) - 1 = 1.0` → assert `== 1.0`.
- **Fewer than 3 → None**: 2 annual entries.
- **Base-year EPS ≤ 0 → None**: oldest (index `n-1`) = `"0"`.
- **Newest EPS ≤ 0 → None**: index 0 = `"-1.0"`.
- **Unparseable → None**: index 0 = `"n/a"`.
- **More than 4 years uses only the first 4**: pass 6 annual entries where index
  3 is the base used (`n` caps at 4); confirm the result matches the 4-year
  calculation, not a 6-year one. (Set index 3 to a known base and index 5 to a
  wildly different value; the result must ignore index 5.)

**Verify**: `uv run pytest tests/test_alpha_vantage.py -q -k cagr`
→ all pass.

### Step 4: Format, lint, typecheck, full suite

- `uv run ruff format tests/test_alpha_vantage.py`
- `uv run ruff check tests/test_alpha_vantage.py` → exit 0
- `uv run pyrefly check` → exit 0
- `uv run pytest -q` → full suite green

Then set this plan's row in `plans/README.md` to DONE.

## Test plan

- New file `tests/test_alpha_vantage.py` covering: quarterly happy/negative/
  too-few/zero-base/unparseable/no-earnings; annual 4yr/3yr/too-few/zero-base/
  negative-newest/unparseable/caps-at-4.
- Structural pattern: `tests/test_exit_evaluator.py` (builders + focused asserts).
- Verification: `uv run pytest tests/test_alpha_vantage.py -q` → all pass;
  `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_alpha_vantage.py -q` passes with ≥12 new tests
- [ ] `uv run pytest -q` exits 0 (no regression in the existing 98)
- [ ] `uv run pyrefly check` exits 0
- [ ] `uv run ruff check tests/test_alpha_vantage.py` exits 0
- [ ] `git status` shows only `tests/test_alpha_vantage.py` and
      `plans/README.md` changed
- [ ] `plans/README.md` status row for plan 017 updated to DONE

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `alpha_vantage.py` changed since `13074c8` and the two
  method bodies no longer match the "Current state" excerpts.
- A characterization test fails in a way that looks like a real source bug —
  leave the test asserting the actual behavior with a `# NOTE:` and report it.
- Any test would require a live API key or network call — it must not; you are
  stubbing `get_earnings`.

## Maintenance notes

- These pin the current formulas. If the EPS-growth definition changes (e.g.
  switching the year-ago quarter index, or the CAGR period), update the matching
  test deliberately and flag it in review.
- A reviewer should confirm every test stubs `get_earnings` (no real HTTP) and
  that expected values were computed by hand from the stubbed input.
- Deferred: `_get`, `get_overview`, `get_fundamentals` (network/parse glue) — low
  value, left out on purpose.
