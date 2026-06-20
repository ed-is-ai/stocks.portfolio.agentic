# Plan 020: Unit-test the `require_local_or_token` security guard directly

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat 7bfeee7..HEAD -- app/core/security.py app/core/config.py`
> If either file changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch, treat
> it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tests
- **Planned at**: commit `7bfeee7`, 2026-06-20

## Why this matters

`require_local_or_token` is the only authorization guard on the money-mutating
web endpoints (`POST /trades`, `DELETE /trades/{id}`, `POST /refresh-data`).
The existing tests in `tests/test_web_auth.py` exercise it **only through
FastAPI's `TestClient`**, whose client host is the string `"testclient"` — never
a loopback address. As a result the guard's three most important branches are
completely untested: the **loopback-allow** path (`127.0.0.1` / `::1` /
`localhost`), the **token-set-but-still-loopback** path, and the
**`request.client is None` → 403** edge. A one-character typo in `"127.0.0.1"`
or a dropped `"::1"` would either silently lock every localhost developer out of
their own portfolio or weaken the guard, and CI would stay green. This plan pins
every branch with a fast, dependency-free unit test that calls the function
directly with a synthetic request.

## Current state

File: `app/core/security.py` (reproduced in full — it is short):

```python
from fastapi import HTTPException, Request

from app.core import config


def require_local_or_token(request: Request) -> None:
    """Allow loopback clients, or any client with a valid shared secret. ..."""
    token = config.APP_AUTH_TOKEN()
    if token and request.headers.get("X-Auth-Token") == token:
        return
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise HTTPException(status_code=403, detail="Forbidden")
```

Facts the tests rely on:
- The token comes from `config.APP_AUTH_TOKEN()` (`app/core/config.py:48-50`),
  which is a **function** that returns `os.getenv("APP_AUTH_TOKEN")` *at call
  time*. Control it in tests with the `monkeypatch` fixture:
  `monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")` or
  `monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)`. Do **not** patch
  `config.APP_AUTH_TOKEN` itself — set the env var.
- Allow order: a matching `X-Auth-Token` short-circuits first (works from any
  host). Otherwise the host must be one of `{"127.0.0.1", "::1", "localhost"}`.
  Anything else → `HTTPException(status_code=403)`.
- `request.client` may be `None`; then `client_host` is `None`, which is not in
  the loopback set → 403.
- The function takes a `fastapi.Request`, but it only touches `request.headers`
  (a `.get(...)` call) and `request.client` (which has a `.host` attribute, or
  is `None`). You do **not** need a real `Request` — build a lightweight stub
  (see Step 1). A `types.SimpleNamespace` is enough: `headers` can be a plain
  dict (it has `.get`), and `client` is either a `SimpleNamespace(host=...)` or
  `None`.

### Test conventions in this repo (match these)

- `tests/test_<module>.py`, plain pytest functions — see
  `tests/test_exit_evaluator.py` for the helper-builder + focused-assert style,
  and `tests/test_web_auth.py` for how the existing auth tests are written.
- Use the built-in `monkeypatch` fixture for env vars. Assert raised exceptions
  with `pytest.raises(HTTPException)` and check `exc.value.status_code == 403`.

## Commands you will need

| Purpose   | Command                                        | Expected on success |
|-----------|------------------------------------------------|---------------------|
| Run new tests | `uv run pytest tests/test_security.py -q`  | all pass            |
| Full suite | `uv run pytest -q`                            | all pass (no regressions) |
| Typecheck | `uv run pyrefly check`                          | no NEW errors in `tests/test_security.py` (a large pre-existing error baseline exists; that is expected — only your file must add none) |
| Lint      | `uv run ruff check tests/test_security.py`     | exit 0              |
| Format    | `uv run ruff format tests/test_security.py`    | reformats, exit 0   |

## Scope

**In scope** (the only files you should create/modify):
- `tests/test_security.py` (create)
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch):
- `app/core/security.py` — this is a *characterization* plan; pin current
  behavior, do not change the guard. If a test reveals a likely bug, assert the
  **actual current** behavior, add a `# NOTE:` comment, and report it.
- `tests/test_web_auth.py` — leave the existing integration tests as they are.
- Any other `app/` or `tests/` file.

## Git workflow

- Branch: `advisor/020-security-guard-unit-tests`
- Conventional-commit style, e.g.
  `test(core): characterize require_local_or_token guard branches`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Create the test file with a request stub

Create `tests/test_security.py`:

```python
"""Unit tests for the require_local_or_token money-endpoint guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.security import require_local_or_token


def _request(host: str | None, token_header: str | None = None) -> SimpleNamespace:
    """Build a minimal stand-in for fastapi.Request.

    The guard only reads request.headers.get("X-Auth-Token") and
    request.client (.host, or None). A SimpleNamespace satisfies both.
    """
    headers = {"X-Auth-Token": token_header} if token_header is not None else {}
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(headers=headers, client=client)
```

**Verify**: `uv run pytest tests/test_security.py -q`
→ collects 0 tests, exits 0 (imports + stub parse).

### Step 2: Test the no-token (default local) workflow

With `APP_AUTH_TOKEN` unset (`monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)`):

- **Loopback `127.0.0.1` allowed**: `require_local_or_token(_request("127.0.0.1"))`
  returns `None` (does not raise).
- **Loopback `::1` allowed**: same with host `"::1"`.
- **`localhost` allowed**: same with host `"localhost"`.
- **Non-loopback rejected**: `_request("10.0.0.5")` →
  `pytest.raises(HTTPException)` and `exc.value.status_code == 403`.
- **`client is None` rejected**: `_request(None)` → raises `HTTPException`,
  `status_code == 403`.

To assert "does not raise", just call it — if it raised, the test fails. (You
may add `assert require_local_or_token(...) is None` to be explicit.)

**Verify**: `uv run pytest tests/test_security.py -q -k "loopback or localhost or none or non_loopback"`
→ all pass.

### Step 3: Test the token-configured workflow

With `monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")`:

- **Matching token from a non-loopback host allowed**:
  `_request("10.0.0.5", token_header="s3cret")` → returns `None`.
- **Wrong token from a non-loopback host rejected**:
  `_request("10.0.0.5", token_header="nope")` → `HTTPException`, 403.
- **Missing token header from a non-loopback host rejected**:
  `_request("10.0.0.5")` → `HTTPException`, 403.
- **Loopback still allowed even with a token configured and no header**:
  `_request("127.0.0.1")` → returns `None` (the loopback branch still applies
  when the token check falls through).

**Verify**: `uv run pytest tests/test_security.py -q -k token`
→ all pass.

### Step 4: Format, lint, typecheck, full suite

Run, in order:
- `uv run ruff format tests/test_security.py`
- `uv run ruff check tests/test_security.py` → exit 0
- `uv run pyrefly check` → no new errors referencing `tests/test_security.py`
- `uv run pytest -q` → full suite green (no regressions)

Then update this plan's row in `plans/README.md` to DONE (unless a reviewer
told you they maintain the index).

## Test plan

- New file `tests/test_security.py` with **≥9 tests**: three loopback-allow
  hosts, non-loopback reject, client-None reject (no-token mode); matching-token
  allow, wrong-token reject, missing-token reject, loopback-allow-with-token
  (token mode).
- Structural pattern: `tests/test_exit_evaluator.py` (focused asserts) +
  `tests/test_web_auth.py` (auth-token handling). Use `monkeypatch` for the env
  var and `pytest.raises(HTTPException)` for the 403 cases.
- Verification: `uv run pytest tests/test_security.py -q` → all pass;
  `uv run pytest -q` → still green.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uv run pytest tests/test_security.py -q` passes with ≥9 new tests
- [ ] `uv run pytest -q` exits 0 (no regression)
- [ ] `uv run ruff check tests/test_security.py` exits 0
- [ ] `uv run pyrefly check` introduces no new errors in `tests/test_security.py`
- [ ] `git status` shows only `tests/test_security.py` created (and
      `plans/README.md` if you updated the index)

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `security.py` changed since `7bfeee7` and the function
  body or the loopback set `{"127.0.0.1", "::1", "localhost"}` no longer matches
  the excerpt.
- A test you wrote to assert current behavior fails in a way that looks like a
  real guard bug (e.g. a loopback host is rejected, or a non-loopback host
  without a token is allowed) — leave the test asserting the **actual** observed
  behavior with a `# NOTE:` comment and report it; do not change the source.
- The `SimpleNamespace` stub fails because the guard reads an attribute the stub
  doesn't provide — report which attribute (the "Current state" notes may be
  incomplete); do not switch to spinning up a real server.

## Maintenance notes

- If the guard ever grows to inspect more of the request (e.g. `X-Forwarded-For`
  for proxy deployments), the `_request` stub must grow the matching attribute,
  and a proxy-spoofing test should be added — a forwarded header must not be
  trusted to grant the loopback allowance.
- A reviewer should confirm no test starts a real server or uses `TestClient`
  (the whole point is to reach the branches `TestClient` cannot), and that the
  403 assertions check `status_code`, not just that *something* raised.
- Deferred: end-to-end coverage of the guarded routes already exists in
  `tests/test_web_auth.py`; this plan complements it at the unit level.
