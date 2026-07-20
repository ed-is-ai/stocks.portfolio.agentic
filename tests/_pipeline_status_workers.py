"""Multiprocessing worker targets for pipeline status lock tests.

Kept in their own importable module (rather than defined inline in the test
module) because ``multiprocessing.get_context("spawn")`` re-imports the
target function's module in the child process; a function defined directly
in a pytest test module is not reliably importable that way.
"""

from pathlib import Path

from app.repositories.pipeline_status_repo import (
    PipelineRunActiveError,
    PipelineStatusRepository,
)
from app.schemas.pipeline_status import PipelineStage, PipelineState, StageState


def stalled_start(path: str, ready, release) -> None:
    """Pause process A after reading idle but before committing its lease."""
    repo = PipelineStatusRepository(Path(path))
    write = repo._write

    def wait_then_write(status) -> None:
        ready.set()
        if not release.wait(timeout=5):
            raise TimeoutError("race test was not released")
        write(status)

    repo._write = wait_then_write  # type: ignore[method-assign]
    repo.start(run_id="run-a")


def stalled_transition(path: str, ready, release) -> None:
    """Pause run A's mutation after ownership check but before commit."""
    repo = PipelineStatusRepository(Path(path))
    write = repo._write

    def wait_then_write(status) -> None:
        ready.set()
        if not release.wait(timeout=5):
            raise TimeoutError("race test was not released")
        write(status)

    repo._write = wait_then_write  # type: ignore[method-assign]
    repo.transition(
        PipelineStage.SOURCES,
        StageState.RUNNING,
        expected_run_id="run-a",
    )


def start_replacement_run(path: str, attempting, refused) -> None:
    attempting.set()
    try:
        PipelineStatusRepository(Path(path)).start(run_id="run-b")
    except PipelineRunActiveError:
        refused.set()


def finish_then_start_replacement(path: str, attempting) -> None:
    attempting.set()
    repo = PipelineStatusRepository(Path(path))
    repo.finish(PipelineState.FAILED, expected_run_id="run-a")
    repo.start(run_id="run-b")
