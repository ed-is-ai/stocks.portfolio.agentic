"""Contract tests for durable, cross-process pipeline status."""

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path

import pytest

from app.repositories.pipeline_status_repo import (
    PipelineRunActiveError,
    PipelineStatusRepository,
)
from app.schemas.pipeline_status import PipelineStage, PipelineState, StageState


def _stalled_start(path: str, ready, release) -> None:
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


def _stalled_transition(path: str, ready, release) -> None:
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


def _start_replacement_run(path: str, attempting, refused) -> None:
    attempting.set()
    try:
        PipelineStatusRepository(Path(path)).start(run_id="run-b")
    except PipelineRunActiveError:
        refused.set()


def _finish_then_start_replacement(path: str, attempting) -> None:
    attempting.set()
    repo = PipelineStatusRepository(Path(path))
    repo.finish(PipelineState.FAILED, expected_run_id="run-a")
    repo.start(run_id="run-b")


def test_repository_round_trip_preserves_order_and_timing(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path)

    status = repo.start(run_id="run-123")
    repo.transition(
        PipelineStage.SOURCES, StageState.RUNNING, expected_run_id="run-123"
    )
    repo.transition(
        PipelineStage.SOURCES, StageState.COMPLETE, expected_run_id="run-123"
    )
    repo.transition(
        PipelineStage.MARKET_DATA,
        StageState.RUNNING,
        expected_run_id="run-123",
    )
    repo.update_counts(scanned=14, expected_run_id="run-123")
    loaded = repo.load()

    assert status.run_id == "run-123"
    assert loaded.state is PipelineState.RUNNING
    assert loaded.current_stage is PipelineStage.MARKET_DATA
    assert [stage.stage for stage in loaded.stages] == list(PipelineStage)
    assert loaded.stages[0].state is StageState.COMPLETE
    assert loaded.stages[0].duration_seconds is not None
    assert loaded.scanned == 14
    assert json.loads(path.read_text())["schema_version"] == 1


def test_repository_uses_replace_for_atomic_writes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "pipeline_status.json"
    replaced: list[tuple[object, object]] = []

    import app.repositories.pipeline_status_repo as repo_module

    real_replace = repo_module.os.replace

    def recording_replace(source, destination):
        replaced.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(repo_module.os, "replace", recording_replace)
    PipelineStatusRepository(path).start(run_id="atomic")

    assert len(replaced) == 1
    assert replaced[0][1] == path
    assert path.exists()


def test_missing_corrupt_and_unknown_schema_return_idle(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path)

    assert repo.load().state is PipelineState.IDLE
    path.write_text("not-json", encoding="utf-8")
    assert repo.load().state is PipelineState.IDLE
    path.write_text('{"schema_version": 999}', encoding="utf-8")
    assert repo.load().state is PipelineState.IDLE
    path.write_text("[]", encoding="utf-8")
    assert repo.load().state is PipelineState.IDLE


def test_structurally_incomplete_stage_list_returns_full_idle_model(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    payload = PipelineStatusRepository(path).start(run_id="incomplete").model_dump(
        mode="json"
    )
    payload["stages"] = payload["stages"][:-1]
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = PipelineStatusRepository(path).load()

    assert loaded.state is PipelineState.IDLE
    assert loaded.run_id is None
    assert [stage.stage for stage in loaded.stages] == list(PipelineStage)


def test_older_writer_cannot_mutate_or_finish_newer_run(tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    repo.start(run_id="run-a")
    repo.transition(
        PipelineStage.SOURCES,
        StageState.RUNNING,
        expected_run_id="run-a",
    )
    repo.finish(PipelineState.COMPLETE, expected_run_id="run-a")
    repo.start(run_id="run-b")

    with pytest.raises(PipelineRunActiveError):
        repo.start(run_id="run-a", expected_run_id="run-a")
    repo.transition(
        PipelineStage.SOURCES,
        StageState.COMPLETE,
        expected_run_id="run-a",
    )
    repo.update_counts(scanned=99, expected_run_id="run-a")
    repo.finish(PipelineState.FAILED, expected_run_id="run-a")

    retained = repo.load()
    assert retained.run_id == "run-b"
    assert retained.state is PipelineState.RUNNING
    assert retained.scanned == 0
    assert retained.stage(PipelineStage.SOURCES).state is StageState.PENDING

    repo.transition(
        PipelineStage.SOURCES,
        StageState.RUNNING,
        expected_run_id="run-b",
    )
    assert repo.load().stage(PipelineStage.SOURCES).state is StageState.RUNNING


def test_start_refuses_to_replace_a_live_run_but_allows_exact_attachment(
    tmp_path,
) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    original = repo.start(run_id="run-a")

    attached = repo.start(run_id="run-a", expected_run_id="run-a")

    assert attached.run_id == original.run_id
    assert attached.started_at == original.started_at
    with pytest.raises(RuntimeError, match="already running"):
        repo.start(run_id="run-b")
    with pytest.raises(RuntimeError, match="already running"):
        repo.start(run_id="run-b", expected_run_id="run-b")
    assert repo.load().run_id == "run-a"


def test_naive_persisted_timestamp_falls_back_to_idle(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path)
    payload = repo.start(run_id="naive").model_dump(mode="json")
    payload["updated_at"] = "2026-07-19T20:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = repo.load()

    assert loaded.state is PipelineState.IDLE
    assert loaded.run_id is None


def test_only_one_concurrent_process_acquires_the_run_lease(tmp_path) -> None:
    """Concurrent idle readers cannot both believe they acquired the lease."""
    path = tmp_path / "pipeline_status.json"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    replacement_attempting = context.Event()
    replacement_refused = context.Event()
    stale_writer = context.Process(
        target=_stalled_start,
        args=(str(path), ready, release),
    )
    replacement = context.Process(
        target=_start_replacement_run,
        args=(str(path), replacement_attempting, replacement_refused),
    )

    stale_writer.start()
    try:
        assert ready.wait(timeout=5)
        replacement.start()
        assert replacement_attempting.wait(timeout=5)
        replacement.join(timeout=0.5)
    finally:
        release.set()
        stale_writer.join(timeout=5)
        if replacement.pid is not None:
            replacement.join(timeout=5)

    assert stale_writer.exitcode == 0
    assert replacement.exitcode == 0
    assert replacement_refused.is_set()
    retained = PipelineStatusRepository(path).load()
    assert retained.run_id == "run-a"
    assert retained.stage(PipelineStage.SOURCES).state is StageState.PENDING


def test_stale_transition_cannot_overwrite_a_successor_run(tmp_path) -> None:
    """A's delayed mutation cannot land after A is finished and B starts."""
    path = tmp_path / "pipeline_status.json"
    PipelineStatusRepository(path).start(run_id="run-a")
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    replacement_attempting = context.Event()
    stale_writer = context.Process(
        target=_stalled_transition,
        args=(str(path), ready, release),
    )
    replacement = context.Process(
        target=_finish_then_start_replacement,
        args=(str(path), replacement_attempting),
    )

    stale_writer.start()
    try:
        assert ready.wait(timeout=5)
        replacement.start()
        assert replacement_attempting.wait(timeout=5)
        replacement.join(timeout=0.5)
    finally:
        release.set()
        stale_writer.join(timeout=5)
        if replacement.pid is not None:
            replacement.join(timeout=5)

    assert stale_writer.exitcode == 0
    assert replacement.exitcode == 0
    retained = PipelineStatusRepository(path).load()
    assert retained.run_id == "run-b"
    assert retained.state is PipelineState.RUNNING
    assert retained.stage(PipelineStage.SOURCES).state is StageState.PENDING


def test_stale_running_status_is_recovered_as_failed(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path, stale_after_seconds=60)
    repo.start(run_id="abandoned")
    repo.transition(
        PipelineStage.SOURCES, StageState.RUNNING, expected_run_id="abandoned"
    )
    payload = json.loads(path.read_text())
    old = datetime.now(timezone.utc) - timedelta(seconds=61)
    payload["updated_at"] = old.isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = repo.load()

    assert recovered.state is PipelineState.FAILED
    assert recovered.current_stage is PipelineStage.SOURCES
    assert recovered.stages[0].state is StageState.FAILED
    assert "interrupted" in (recovered.error_summary or "").lower()


def test_new_run_can_acquire_after_stale_running_status(tmp_path) -> None:
    path = tmp_path / "pipeline_status.json"
    repo = PipelineStatusRepository(path, stale_after_seconds=60)
    repo.start(run_id="abandoned")
    payload = json.loads(path.read_text())
    payload["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=61)
    ).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")

    acquired = repo.start(run_id="replacement")

    assert acquired.run_id == "replacement"
    assert acquired.state is PipelineState.RUNNING


def test_finish_retains_stage_durations_and_terminal_counts(tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    repo.start(run_id="done")
    repo.transition(
        PipelineStage.SOURCES, StageState.RUNNING, expected_run_id="done"
    )
    repo.transition(
        PipelineStage.SOURCES, StageState.COMPLETE, expected_run_id="done"
    )

    finished = repo.finish(
        PipelineState.COMPLETE,
        expected_run_id="done",
        scanned=10,
        analysed=8,
        actionable=2,
    )

    assert finished.completed_at is not None
    assert finished.elapsed_seconds >= 0
    assert finished.scanned == 10
    assert finished.analysed == 8
    assert finished.actionable == 2
    assert all(stage.state is not StageState.PENDING for stage in finished.stages)


def test_failure_between_transitions_is_attributed_to_latest_stage(tmp_path) -> None:
    repo = PipelineStatusRepository(tmp_path / "pipeline_status.json")
    repo.start(run_id="failed-between-stages")
    repo.transition(
        PipelineStage.SOURCES,
        StageState.RUNNING,
        expected_run_id="failed-between-stages",
    )
    repo.transition(
        PipelineStage.SOURCES,
        StageState.COMPLETE,
        expected_run_id="failed-between-stages",
    )

    failed = repo.finish(
        PipelineState.FAILED,
        expected_run_id="failed-between-stages",
        error_summary="assembly failed",
    )

    assert failed.stage(PipelineStage.SOURCES).state is StageState.FAILED
