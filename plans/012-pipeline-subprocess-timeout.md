# Plan 012: Add a timeout to the pipeline subprocess

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat dbf0d18..HEAD -- app/services/pipeline_service.py`
> If the file changed since this plan was written, compare the "Current state"
> excerpt against the live code before proceeding; on a mismatch, treat it as a
> STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 011 (so `uv run pytest` runs clean without `--ignore`)
- **Category**: bug
- **Planned at**: commit `dbf0d18`, 2026-06-19

## Why this matters

`PipelineService.run_once` shells out to the orchestrator (a full scan → analyse
→ alert run that does unbounded network I/O over ~100 tickers and external
screeners) with **no timeout**. It is invoked from two places:

- the web `POST /refresh-data` endpoint, via `await asyncio.to_thread(pipeline.run_once)`
  (`app/api/routes/pipeline.py:39`), and
- the background scheduler.

If the subprocess hangs (a wedged network call, an unresponsive screener), the
call blocks **forever**: the web request never returns and the worker thread is
leaked; a scheduled run never completes and can pile up. Every other subprocess
in the codebase already sets a timeout — `scanner_agent.py:127` uses
`timeout=300`. This plan brings `run_once` in line: bound the run and convert a
timeout into a clean failure result.

## Current state

`app/services/pipeline_service.py` (whole file is short):

```python
import subprocess
import sys

from pydantic import BaseModel

from app.core.config import ROOT_DIR


class PipelineRunResult(BaseModel):
    """Outcome of a single pipeline run."""

    success: bool
    details: str


class PipelineService:
    """Runs the momentum pipeline once and reports the outcome."""

    def run_once(self) -> PipelineRunResult:
        """Run the orchestrator pipeline once and capture its output."""
        result = subprocess.run(
            [sys.executable, "-m", "app.orchestration.orchestrator", "--once"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
        )
        details = (result.stdout or result.stderr).strip()
        return PipelineRunResult(success=result.returncode == 0, details=details)
```

Convention to match: `scanner_agent.py:110-128` calls `subprocess.run([...],
capture_output=True, text=True, timeout=300)`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest tests/test_pipeline_service.py -v` | all pass |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |

## Scope

**In scope** (the only files you should modify):
- `app/services/pipeline_service.py` (add timeout + TimeoutExpired handling)
- `tests/test_pipeline_service.py` (create)

**Out of scope** (do NOT touch):
- `app/api/routes/pipeline.py` — the route already offloads to a thread; do not
  change it.
- `app/orchestration/orchestrator.py` — the pipeline body itself is unchanged.
- The scheduler wiring.

## Git workflow

- Branch: `advisor/012-pipeline-subprocess-timeout`
- Commit message: `fix(pipeline): bound run_once subprocess with a timeout`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add a bounded timeout and convert a timeout into a failure result

In `app/services/pipeline_service.py`:

1. Add a module-level constant for the cap (a full pipeline legitimately takes a
   few minutes; 30 minutes is a generous safety ceiling, not a normal duration):
   ```python
   _RUN_TIMEOUT_SECONDS = 1800  # 30 min safety cap; a normal run is far shorter
   ```
2. Pass `timeout=_RUN_TIMEOUT_SECONDS` to `subprocess.run`, and wrap the call so
   a `subprocess.TimeoutExpired` becomes a clean failed `PipelineRunResult`
   instead of propagating:
   ```python
   def run_once(self) -> PipelineRunResult:
       """Run the orchestrator pipeline once and capture its output."""
       try:
           result = subprocess.run(
               [sys.executable, "-m", "app.orchestration.orchestrator", "--once"],
               cwd=str(ROOT_DIR),
               capture_output=True,
               text=True,
               timeout=_RUN_TIMEOUT_SECONDS,
           )
       except subprocess.TimeoutExpired:
           return PipelineRunResult(
               success=False,
               details=f"Pipeline timed out after {_RUN_TIMEOUT_SECONDS}s",
           )
       details = (result.stdout or result.stderr).strip()
       return PipelineRunResult(success=result.returncode == 0, details=details)
   ```

**Verify**: `grep -n "timeout" app/services/pipeline_service.py` → shows the
constant, the `timeout=` argument, and the `TimeoutExpired` handler.

### Step 2: Add tests (this module has none today)

Create `tests/test_pipeline_service.py`. Use `monkeypatch` to replace
`subprocess.run` so no real subprocess runs:

```python
import subprocess

from app.services.pipeline_service import PipelineService


def test_run_once_success(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout")  # timeout must be passed
        return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is True
    assert result.details == "ok"


def test_run_once_failure_uses_stderr(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert result.details == "boom"


def test_run_once_timeout_returns_failure(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = PipelineService().run_once()
    assert result.success is False
    assert "timed out" in result.details.lower()
```

**Verify**: `uv run pytest tests/test_pipeline_service.py -v` → 3 passed.

### Step 3: Full suite, lint, format

**Verify**:
- `uv run pytest` → all pass (the 3 new tests included).
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformatted; re-stage only the
  2 in-scope files).

## Test plan

Three new tests in `tests/test_pipeline_service.py`, all monkeypatching
`subprocess.run` (no real subprocess, no network):
- success path (returncode 0 → `success=True`, stdout in details),
- failure path (returncode 1 → `success=False`, stderr in details),
- timeout path (`TimeoutExpired` → `success=False`, "timed out" in details).

These also pin that a `timeout` kwarg is actually passed (the success test
asserts it). Verification: `uv run pytest tests/test_pipeline_service.py -v`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -n "timeout=_RUN_TIMEOUT_SECONDS" app/services/pipeline_service.py` returns a match
- [ ] `grep -n "except subprocess.TimeoutExpired" app/services/pipeline_service.py` returns a match
- [ ] `uv run pytest tests/test_pipeline_service.py -v` → 3 passed
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `git status` shows only the 2 in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows `pipeline_service.py` changed since `dbf0d18` and the
  excerpt no longer matches.
- `subprocess.run` cannot be monkeypatched as shown (e.g. the module imports it
  differently) — re-locate the call and report.

## Maintenance notes

- 30 minutes is a deliberately generous ceiling. If real pipeline runs ever
  approach it, raise the constant rather than removing the timeout — an unbounded
  external run is the failure mode this plan exists to prevent.
- The subprocess is killed by `subprocess.run` on timeout (it sends SIGKILL after
  the timeout and re-raises). No orphan process remains. A reviewer should confirm
  the `TimeoutExpired` branch returns a result rather than re-raising, so the web
  `/refresh-data` thread always completes.
