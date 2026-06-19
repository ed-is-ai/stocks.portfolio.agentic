# Plan 005: Protect money-mutating web endpoints (enforce localhost / shared-secret)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat ce96c93..HEAD -- web/app.py`
> If `web/app.py` changed since this plan was written, compare the "Current
> state" excerpts below against the live code before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW (additive dependency guard; no change to existing happy-path behavior when run locally without a token)
- **Depends on**: 001 (need a green, fast test suite to add endpoint tests against)
- **Category**: security
- **Planned at**: commit `ce96c93`, 2026-06-18

## Why this matters

The web app exposes three endpoints that mutate money state or run a
subprocess — `POST /trades` (records buys/sells), `DELETE /trades/{trade_id}`
(deletes a trade), and `POST /refresh-data` (spawns `orchestrator.py`) — with
**no authentication and no CSRF protection**. Today this is acceptable *only*
because the app is documented to run locally via `uvicorn web.app:app`, which
binds to loopback (`127.0.0.1`) by default. The danger is silent: the moment
someone runs it with `--host 0.0.0.0` (to reach it from a phone, a LAN, a
container), every endpoint becomes world-writable with zero friction, and a
malicious web page the user visits could `fetch('http://localhost:8000/trades',
{method:'POST', ...})` against the local instance (CSRF). This plan makes the
localhost-only assumption **enforced rather than assumed**: mutating endpoints
are allowed for loopback clients, or for any client presenting a shared secret
when one is configured, and rejected otherwise. It does not change behavior for
the current local-only workflow (no token set + loopback client ⇒ allowed).

## Current state

- `web/app.py` — FastAPI app; mutating routes have no guard.

App construction (no auth middleware, no CSRF):

```python
# web/app.py:16-36
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
...
app = FastAPI(title="Stock Trader")
templates = Jinja2Templates(directory=str(_ROOT / "web" / "templates"))
trader = TraderAgent(name="TraderAgent")
```

The three unprotected mutating endpoints:

```python
# web/app.py:204
@app.post("/refresh-data", response_class=HTMLResponse)
async def refresh_data(request: Request) -> HTMLResponse:
    ...
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(_ROOT / "orchestrator.py"), "--once"],
        ...
    )

# web/app.py:411
@app.post("/trades")
async def record_trade(request: Request, ticker: ..., action: ..., ...):
    if action == "BUY":
        trader.record_buy(...)
    elif action == "SELL":
        trader.record_sell(...)
    ...

# web/app.py:435
@app.delete("/trades/{trade_id}")
async def delete_trade(trade_id: int) -> RedirectResponse:
    trader.delete_trade(trade_id)
    return RedirectResponse("/partials/history", status_code=303)
```

Note: the subprocess args in `/refresh-data` are a fixed list (no shell, no
user input interpolated) — there is **no command-injection hole**; the only gap
is "anyone who can reach the port can trigger it".

### How the app is run (documented, loopback by default)
`web/app.py:4-5` docstring: `python -m uvicorn web.app:app --reload`. Uvicorn
binds `127.0.0.1` unless `--host` is passed. So the default deployment is
already loopback-only; this plan protects against the non-default case.

### Repo conventions to match
- Type hints required; line length ≤ 88; snake_case functions; f-strings;
  docstrings on public callables (see existing helpers like `_get_gbpusd_rate`
  in `web/app.py`).
- Imports that must live below the `sys.path` shim carry `# noqa: E402`
  (see `web/app.py:25-27`). Your new imports are stdlib `os` / FastAPI symbols
  and go in the **top** import block (lines 8-18), not below the shim.
- Tests use `pytest` + `unittest.mock`; FastAPI endpoints are tested with
  `fastapi.testclient.TestClient`. There is no existing `web/` test file —
  create `tests/test_web_auth.py`.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Run the new test file | `python -m pytest tests/test_web_auth.py -o addopts="" -q -p no:cacheprovider` | all pass |
| Run full root suite | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | 0 failed, < 30s |
| Confirm guard wired | `grep -n "Depends(require_local_or_token)" web/app.py` | 3 matches |

> `-o addopts=""` overrides `pytest.ini`'s `--json-report` (plugin may be
> absent). Always pass it.

## Scope

**In scope** (the only files you may modify/create):
- `web/app.py` (add the guard dependency + apply it to the 3 mutating routes)
- `tests/test_web_auth.py` (create)
- `run.md` (add one short note documenting `APP_AUTH_TOKEN` and the loopback
  default)

**Out of scope** (do NOT touch):
- Any `GET` / `@app.get` route — read-only, no guard needed.
- The trade logic in `TraderAgent` — untouched.
- The subprocess call body in `/refresh-data` — it is already injection-safe;
  only add the guard dependency to the route.
- The HTML templates / front-end JS — no CSRF token plumbing into forms in this
  plan (the loopback+token guard is the agreed scope; see Maintenance notes).

## Git workflow

- Branch: `advisor/005-web-money-endpoint-protection`
- Commit per logical unit; conventional-commit style (matches repo `git log`,
  e.g. `feat(web): guard money-mutating endpoints behind localhost/token`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the guard dependency

In `web/app.py`, add `import os` to the top stdlib import block (near
`import csv`, line 8). Then, after the `app = FastAPI(...)` line (~line 33), add
this dependency:

```python
def require_local_or_token(request: Request) -> None:
    """Allow loopback clients, or any client with a valid shared secret.

    Money-mutating endpoints use this. When ``APP_AUTH_TOKEN`` is unset (the
    default local workflow), only loopback clients (127.0.0.1 / ::1) are
    allowed. When it is set, a matching ``X-Auth-Token`` header is also
    accepted from any host. Anything else gets HTTP 403.
    """
    token = os.getenv("APP_AUTH_TOKEN")
    if token and request.headers.get("X-Auth-Token") == token:
        return
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise HTTPException(status_code=403, detail="Forbidden")
```

Add `HTTPException` and `Depends` to the FastAPI import:
`from fastapi import Depends, FastAPI, Form, HTTPException, Request`.

**Verify**: `python -c "import ast,sys; ast.parse(open('web/app.py').read()); print('ok')"` → `ok`

### Step 2: Apply the guard to the three mutating endpoints

Add `_: Annotated[None, Depends(require_local_or_token)] = None` is **not** the
idiom here — use a route-level dependency so it runs before the body. Change
each decorator to include `dependencies=[Depends(require_local_or_token)]`:

```python
@app.post("/refresh-data", response_class=HTMLResponse,
          dependencies=[Depends(require_local_or_token)])
...
@app.post("/trades", dependencies=[Depends(require_local_or_token)])
...
@app.delete("/trades/{trade_id}",
            dependencies=[Depends(require_local_or_token)])
```

Do not change the function signatures or bodies.

**Verify**: `grep -n "Depends(require_local_or_token)" web/app.py` → 3 matches.

### Step 3: Write endpoint tests

Create `tests/test_web_auth.py`. Use FastAPI's `TestClient`. `TestClient`
issues requests with client host `testclient`, which is **not** loopback — so
by default (no token env) the guard returns 403, and with a token + header it
returns non-403. This is exactly what we want to assert.

```python
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)


def test_delete_trade_forbidden_for_non_loopback_without_token() -> None:
    # TestClient's client host is "testclient", not loopback, and no token set.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("APP_AUTH_TOKEN", None)
        resp = client.delete("/trades/1")
    assert resp.status_code == 403


def test_delete_trade_allowed_with_matching_token() -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        with patch("web.app.trader") as mock_trader:
            resp = client.delete(
                "/trades/1", headers={"X-Auth-Token": "s3cret"},
                follow_redirects=False,
            )
            assert resp.status_code != 403
            mock_trader.delete_trade.assert_called_once_with(1)


def test_refresh_data_forbidden_without_token() -> None:
    os.environ.pop("APP_AUTH_TOKEN", None)
    resp = client.post("/refresh-data")
    assert resp.status_code == 403
```

If patching `web.app.trader` is awkward for the `/trades` POST (it needs form
fields), it is enough to assert the 403 path for the unauthorized cases plus the
non-403 status for the `DELETE` happy path shown above — do not over-build.

**Verify**: `python -m pytest tests/test_web_auth.py -o addopts="" -q` → all pass.

### Step 4: Document the env var

In `run.md`, under the run instructions, add a short note:

```markdown
### Security note

The app binds to `127.0.0.1` by default (loopback only). Money-mutating
endpoints (`POST /trades`, `DELETE /trades/{id}`, `POST /refresh-data`) reject
non-loopback clients. To reach the app from another host, set `APP_AUTH_TOKEN`
and send it as the `X-Auth-Token` header on those requests.
```

**Verify**: `grep -n "APP_AUTH_TOKEN" run.md` → at least 1 match.

### Step 5: Confirm the whole suite is still green

**Verify**: `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider`
→ `0 failed`, completes in < 30s.

## Test plan

- New file `tests/test_web_auth.py` covering: (a) non-loopback client without
  token ⇒ 403 on a mutating route; (b) matching token ⇒ guard passes; (c) a
  second mutating route also 403s without auth.
- No existing web test to model after (none exists); the structural pattern is
  standard FastAPI `TestClient` usage shown above.
- Verification: `python -m pytest tests/test_web_auth.py -o addopts="" -q`.

## Done criteria

ALL must hold:

- [ ] `grep -n "Depends(require_local_or_token)" web/app.py` returns 3 matches.
- [ ] `grep -n "def require_local_or_token" web/app.py` returns 1 match.
- [ ] `python -m pytest tests/test_web_auth.py -o addopts="" -q` passes (≥ 3 tests).
- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0, < 30s.
- [ ] No files outside the in-scope list are modified (`git status`).
- [ ] `plans/README.md` status row for 005 updated to DONE.

## STOP conditions

Stop and report back (do not improvise) if:

- `web/app.py` no longer matches the "Current state" excerpts (drift).
- Applying the route-level dependency breaks an existing test in
  `tests/` that exercises these routes (means another test assumed open
  access — report it; do not weaken the guard to make it pass).
- `request.client` is `None` in the test environment for the loopback case you
  need to assert — report the observed behavior rather than special-casing it.

## Maintenance notes

- This guard protects against open-port and basic CSRF-from-another-origin by
  refusing non-loopback unauthenticated requests. It does **not** add per-form
  CSRF tokens; a malicious page on `localhost` itself is still out of scope.
  If the app is ever served to real users over a network, add proper session
  auth + CSRF tokens in the HTML forms — this plan is the floor, not the ceiling.
- If new money-mutating endpoints are added, they must also carry
  `dependencies=[Depends(require_local_or_token)]`. A reviewer should check this
  on any new `@app.post`/`@app.delete`/`@app.put` route.
- Deferred out of this plan: rate limiting, audit logging of mutations.
