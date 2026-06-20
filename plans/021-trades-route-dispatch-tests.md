# Plan 021: Characterize the `POST /trades` action-dispatch route

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 7bfeee7..HEAD -- app/api/routes/trades.py`
> If `trades.py` changed since this plan was written, compare the "Current
> state" excerpt against the live code before proceeding; on a mismatch, treat
> it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `7bfeee7`, 2026-06-20

## Why this matters

`POST /trades` is a money-mutating endpoint that dispatches the submitted
`action` form field to one of three `TraderService` writes — `record_buy`,
`record_sell`, `correct_trade` — and **silently returns HTTP 200 with no write**
for any unrecognized action (it only logs a warning). Today only the auth guard
on this route is covered (`tests/test_web_auth.py` tests `DELETE /trades` and
`POST /refresh-data`). The dispatch wiring itself — which action calls which
method, with which arguments, and the silent-no-op fall-through — has zero
tests. A wrong branch (e.g. `SELL` calling `record_buy`) or an argument-order
slip would mis-record real trades and nothing would catch it. This plan pins the
dispatch behavior with `TestClient` tests.

## Current state

File: `app/api/routes/trades.py`. The route under test (reproduced):

```python
@router.post("/trades", dependencies=[Depends(require_local_or_token)])
async def record_trade(
    request: Request,
    trader: TraderDep,
    portfolio: PortfolioDep,
    ticker: Annotated[str, Form()],
    action: Annotated[str, Form()],
    shares: Annotated[float, Form()],
    price: Annotated[float, Form()],
    date: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    stop_loss: Annotated[float | None, Form()] = None,
    entry_price: Annotated[float | None, Form()] = None,
) -> HTMLResponse:
    """Record a BUY, SELL, or CORRECT action and return the updated portfolio."""
    if action == "BUY":
        trader.record_buy(ticker, shares, price, date, notes, stop_loss, entry_price)
    elif action == "SELL":
        trader.record_sell(ticker, shares, price, date, notes)
    elif action == "CORRECT":
        trader.correct_trade(ticker, shares, price, date, notes, stop_loss, entry_price)
    else:
        logger.warning("unsupported trade action: %s", action)
    context = portfolio.default_portfolio_context()
    return templates.TemplateResponse(request, "_portfolio.html", context=context)
```

Facts the tests rely on:
- The route depends on `require_local_or_token` (`app/core/security.py`), so a
  `TestClient` request (host `"testclient"`, non-loopback) is **403 unless** an
  `APP_AUTH_TOKEN` env var is set and a matching `X-Auth-Token` header is sent.
  This is exactly the pattern in `tests/test_web_auth.py:20-33`
  (`test_delete_trade_allowed_with_matching_token`).
- Services are injected via FastAPI dependency overrides. Override
  `get_trader_service` and `get_portfolio_service` (imported from
  `app.api.dependencies`) with `MagicMock`s, set on `app.dependency_overrides`,
  and cleared in a `finally`. See `tests/test_web_auth.py` for the override +
  cleanup pattern.
- Argument order passed to the trader (assert exactly this):
  - `BUY`   → `trader.record_buy(ticker, shares, price, date, notes, stop_loss, entry_price)`
  - `SELL`  → `trader.record_sell(ticker, shares, price, date, notes)`
  - `CORRECT` → `trader.correct_trade(ticker, shares, price, date, notes, stop_loss, entry_price)`
  - any other action → **none of the three is called**; response is still 200.
- `shares` and `price` are coerced to `float` by FastAPI, so a form value of
  `"10"` arrives as `10.0`. `notes` defaults to `""`, `stop_loss`/`entry_price`
  default to `None` when the form omits them.
- **Avoid rendering the Jinja template.** The handler ends with
  `templates.TemplateResponse(request, "_portfolio.html", context=...)`. With a
  mocked `portfolio` service the context is not a real dict and the template
  would fail to render. Replace the route module's `TemplateResponse` with a
  stub (see Step 2) so the test exercises the dispatch, not the HTML:
  `monkeypatch.setattr("app.api.routes.trades.templates.TemplateResponse", lambda *a, **k: HTMLResponse("ok"))`.

### Test conventions in this repo (match these)

- `tests/test_<module>.py`, plain pytest. The web-layer pattern is
  `tests/test_web_auth.py`: `TestClient(app)`, `app.dependency_overrides[...] =
  lambda: mock`, wrapped in `try/finally: app.dependency_overrides.clear()`, and
  `patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"})` (or the `monkeypatch`
  fixture) to set the token.
- `from app.api.app import app` and `from app.api.dependencies import
  get_trader_service, get_portfolio_service`.

## Commands you will need

| Purpose   | Command                                            | Expected on success |
|-----------|----------------------------------------------------|---------------------|
| Run new tests | `uv run pytest tests/test_trades_routes.py -q` | all pass            |
| Full suite | `uv run pytest -q`                                | all pass (no regressions) |
| Typecheck | `uv run pyrefly check`                              | no NEW errors in `tests/test_trades_routes.py` (large pre-existing baseline exists) |
| Lint      | `uv run ruff check tests/test_trades_routes.py`    | exit 0              |
| Format    | `uv run ruff format tests/test_trades_routes.py`   | reformats, exit 0   |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_trades_routes.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/api/routes/trades.py` — characterization only; do not change the route.
  If a test reveals a likely bug (e.g. the silent 200 on an unknown action looks
  wrong), assert the **actual current** behavior, add a `# NOTE:` comment, and
  report it. Do not add validation here.
- `tests/test_web_auth.py` — leave it as is.
- Any other `app/` or `tests/` file.

## Git workflow

- Branch: `advisor/021-trades-route-dispatch-tests`
- Conventional-commit style, e.g.
  `test(api): characterize POST /trades action dispatch`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file scaffold

Create `tests/test_trades_routes.py`:

```python
"""Characterization tests for the POST /trades action-dispatch route."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_portfolio_service, get_trader_service

client = TestClient(app)

_FORM = {
    "ticker": "AAPL",
    "shares": "10",
    "price": "100",
    "date": "2024-01-01",
}
```

**Verify**: `uv run pytest tests/test_trades_routes.py -q`
→ collects 0 tests, exits 0.

### Step 2: Add a fixture that wires mocks, auth, and a template stub

Add a fixture that, for each test: sets `APP_AUTH_TOKEN`, overrides both service
providers with `MagicMock`s, stubs the route's `TemplateResponse`, yields the
two mocks, then tears everything down. Example shape:

```python
@pytest.fixture
def mocked_trades(monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    monkeypatch.setattr(
        "app.api.routes.trades.templates.TemplateResponse",
        lambda *a, **k: HTMLResponse("ok"),
    )
    mock_trader = MagicMock()
    mock_portfolio = MagicMock()
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    app.dependency_overrides[get_portfolio_service] = lambda: mock_portfolio
    try:
        yield mock_trader, mock_portfolio
    finally:
        app.dependency_overrides.clear()


def _post(action: str, **extra):
    return client.post(
        "/trades",
        data={**_FORM, "action": action, **extra},
        headers={"X-Auth-Token": "s3cret"},
    )
```

**Verify**: `uv run pytest tests/test_trades_routes.py -q` → still collects 0
tests (no test functions yet), exits 0.

### Step 3: Test each dispatch branch

Using the `mocked_trades` fixture and `_post`:

- **BUY dispatches `record_buy`**: `_post("BUY")` → `resp.status_code == 200`;
  `mock_trader.record_buy.assert_called_once_with("AAPL", 10.0, 100.0,
  "2024-01-01", "", None, None)`; `mock_trader.record_sell.assert_not_called()`;
  `mock_trader.correct_trade.assert_not_called()`.
- **SELL dispatches `record_sell`**: `_post("SELL")` → 200;
  `mock_trader.record_sell.assert_called_once_with("AAPL", 10.0, 100.0,
  "2024-01-01", "")`; the other two not called.
- **CORRECT dispatches `correct_trade`**: `_post("CORRECT")` → 200;
  `mock_trader.correct_trade.assert_called_once_with("AAPL", 10.0, 100.0,
  "2024-01-01", "", None, None)`; the other two not called.
- **Optional fields forwarded on BUY**: `_post("BUY", notes="hi",
  stop_loss="90", entry_price="95")` →
  `mock_trader.record_buy.assert_called_once_with("AAPL", 10.0, 100.0,
  "2024-01-01", "hi", 90.0, 95.0)`.
- **Unknown action is a silent 200 no-op** (current behavior — add a `# NOTE:`):
  `_post("FROBNICATE")` → `resp.status_code == 200`; none of `record_buy`,
  `record_sell`, `correct_trade` called.

**Verify**: `uv run pytest tests/test_trades_routes.py -q`
→ all pass.

### Step 4: Test the auth guard still protects this route

Add one test confirming the guard applies to `POST /trades` (not just the routes
`test_web_auth.py` covers). With **no** `APP_AUTH_TOKEN` and **no** token header,
a `TestClient` POST is non-loopback → 403:

```python
def test_post_trades_forbidden_without_token(monkeypatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    resp = client.post("/trades", data={**_FORM, "action": "BUY"})
    assert resp.status_code == 403
```

(Do not use the `mocked_trades` fixture here — you want the guard to fire before
any service is reached.)

**Verify**: `uv run pytest tests/test_trades_routes.py -q -k forbidden`
→ passes.

### Step 5: Format, lint, typecheck, full suite

- `uv run ruff format tests/test_trades_routes.py`
- `uv run ruff check tests/test_trades_routes.py` → exit 0
- `uv run pyrefly check` → no new errors referencing your file
- `uv run pytest -q` → full suite green

Then update this plan's row in `plans/README.md` to DONE (unless a reviewer
maintains the index).

## Test plan

- New file `tests/test_trades_routes.py` with **≥6 tests**: BUY/SELL/CORRECT
  dispatch (correct method + exact args + others-not-called), optional-fields
  forwarding on BUY, unknown-action silent 200, and the auth-guard 403.
- Structural pattern: `tests/test_web_auth.py` (TestClient + dependency
  overrides + token header). Template rendering is stubbed per Step 2.
- Verification: `uv run pytest tests/test_trades_routes.py -q` → all pass;
  `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_trades_routes.py -q` passes with ≥6 new tests
- [ ] `uv run pytest -q` exits 0 (no regression)
- [ ] `uv run ruff check tests/test_trades_routes.py` exits 0
- [ ] `uv run pyrefly check` introduces no new errors in `tests/test_trades_routes.py`
- [ ] `git status` shows only `tests/test_trades_routes.py` created (and
      `plans/README.md` if you updated the index)

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `trades.py` changed since `7bfeee7` and the dispatch
  branches or the argument order no longer match the excerpt.
- The `TemplateResponse` monkeypatch does not prevent a template-render error
  (the import path may differ) — report the actual error; do not check a real
  `_portfolio.html` render into scope.
- A dispatch test fails because the route calls a *different* method or argument
  order than documented — that is a real finding: leave the test asserting the
  **actual** behavior with a `# NOTE:` and report it.
- The auth-guard test returns something other than 403 with no token — report
  it (the guard may have changed); do not weaken the test to make it pass.

## Maintenance notes

- If the silent-200-on-unknown-action behavior is later changed to return a 4xx
  (a reasonable fix), the unknown-action test must be updated deliberately and
  the change called out in review — this test currently *pins* the silent no-op,
  it does not endorse it.
- A reviewer should confirm the dispatch tests assert *both* the called method's
  exact arguments and that the other two writes were not called (a branch that
  falls through to two methods would otherwise slip past).
- Deferred: rendering the real `_portfolio.html` and asserting on its content —
  out of scope; the portfolio context is built and tested in
  `tests/test_portfolio_service.py`.
