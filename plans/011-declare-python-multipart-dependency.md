# Plan 011: Declare `python-multipart` as an explicit dependency

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dbf0d18..HEAD -- pyproject.toml uv.lock tests/test_web_auth.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dependencies
- **Planned at**: commit `dbf0d18`, 2026-06-19

## Why this matters

`tests/test_web_auth.py` exercises FastAPI endpoints that read form data
(`Form(...)`). FastAPI requires the `python-multipart` package to parse form
bodies — but that package is declared in **neither** `pyproject.toml` nor
`uv.lock`. It happens to be present in the current dev virtualenv, so
`uv run pytest` passes locally. On any clean environment — a fresh clone, CI, or
a new `uv sync` — pytest **fails at collection**:

```
RuntimeError: Form data requires "python-multipart" to be installed.
```

This makes the full test suite un-runnable on a clean checkout (every recent
contributor has had to work around it with `--ignore=tests/test_web_auth.py`).
Declaring the dependency restores a green `uv run pytest` everywhere.

## Current state

- `pyproject.toml` declares FastAPI, uvicorn, jinja2 — but not `python-multipart`:
  ```toml
  dependencies = [
      "pydantic>=2.0.0",
      ...
      "fastapi>=0.110.0",
      "uvicorn>=0.29.0",
      "jinja2>=3.1.0",
  ]
  ```
- `tests/test_web_auth.py` posts to money-mutating endpoints that use
  `Annotated[str, Form()]` (see `app/api/routes/trades.py`), which triggers the
  form parser and thus the `python-multipart` requirement.
- Package manager is **uv** (see `.claude/CLAUDE.md`: "ONLY use uv, NEVER pip";
  add with `uv add <package>`). `uv.lock` is committed.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Add dependency | `uv add python-multipart` | resolves, updates pyproject.toml + uv.lock |
| Sync (clean check) | `uv sync` | exit 0 |
| Full suite | `uv run pytest` | all pass (incl. tests/test_web_auth.py, NO `--ignore`) |
| Lint | `uv run ruff check .` | unchanged from baseline |

## Scope

**In scope** (modified by `uv add`, do not hand-edit beyond that):
- `pyproject.toml` (dependency list)
- `uv.lock`

**Out of scope** (do NOT touch):
- Any source file, any test file. This is a pure dependency declaration.
- Do NOT pin an exact version unless `uv add` does so; let uv choose the
  constraint, matching the existing `>=`-style entries.

## Git workflow

- Branch: `advisor/011-declare-python-multipart-dependency`
- Commit message: `chore(deps): declare python-multipart (required by FastAPI form routes)`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the dependency

Run `uv add python-multipart`. This updates the `dependencies` array in
`pyproject.toml` and regenerates `uv.lock`.

**Verify**: `grep -n "multipart" pyproject.toml` → shows a `python-multipart`
entry in the `[project] dependencies` array (not dev-dependencies — it is a
runtime requirement of the web app's form routes).

### Step 2: Confirm the suite is green WITHOUT the ignore workaround

**Verify**:
- `uv run pytest` → all pass, and the output shows `tests/test_web_auth.py`
  tests running (e.g. `test_delete_trade_forbidden_for_non_loopback_without_token`)
  rather than a collection error.
- Specifically: `uv run pytest tests/test_web_auth.py -v` → all pass (previously
  errored at collection).

### Step 3: Lint unaffected

**Verify**: `uv run ruff check .` → no new errors introduced by this change
(there may be pre-existing errors in `scripts/` and `skills/`; those are not
yours — confirm the count did not increase).

## Test plan

No new tests. The existing `tests/test_web_auth.py` is the proof: it must now
collect and pass under a plain `uv run pytest`. There is nothing to add — this
plan makes already-written tests runnable on a clean environment.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -c "python-multipart" pyproject.toml` returns ≥ 1
- [ ] `python-multipart` appears in `uv.lock` (`grep -c "python-multipart" uv.lock` ≥ 1)
- [ ] `uv run pytest` exits 0 with NO `--ignore` flag, and `test_web_auth.py` tests are collected and pass
- [ ] `git status` shows only `pyproject.toml` and `uv.lock` modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `uv add python-multipart` fails to resolve (network/registry issue) — report
  the error; do not fall back to `pip`.
- After adding it, `uv run pytest` still errors at collection on
  `test_web_auth.py` — that means the missing package was not the cause; report
  the new error.
- `uv add` wants to make unrelated major-version bumps to other packages — stop
  and report the proposed lock diff rather than accepting a broad upgrade.

## Maintenance notes

- `python-multipart` is a transitive-feeling but actually *direct* requirement:
  FastAPI only needs it when an app uses form/multipart parsing, so it is not in
  FastAPI's own hard deps. Any route using `Form(...)`/`File(...)` needs it
  declared here.
- A reviewer should confirm it landed in runtime `dependencies`, not
  `dependency-groups.dev` — the web app needs it at runtime, not just for tests.
- Once this lands, drop the `--ignore=tests/test_web_auth.py` workaround from any
  CI config or contributor notes that still carry it.
