"""Atomic JSON persistence for cross-process pipeline progress.

The application is supported on Windows, macOS, and Linux. Each platform
serializes complete read-check-write transactions with its native file lock
(``msvcrt.locking`` on Windows, ``flock(2)`` elsewhere). The POSIX lock is
advisory and the Windows lock is mandatory; either way, all status writers
must go through this repository.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import IO, Iterator
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.pipeline_status import (
    PipelineStage,
    PipelineState,
    PipelineStatus,
    StageState,
)
from app.schemas.source_health import SourceHealth, SourceName

_LOCK_TIMEOUT_SECONDS = 30.0

if sys.platform == "win32":
    import msvcrt

    def _acquire_lock(stream: IO[bytes]) -> None:
        """Block until the mandatory byte-range lock is acquired."""
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _release_lock(stream: IO[bytes]) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire_lock(stream: IO[bytes]) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)

    def _release_lock(stream: IO[bytes]) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PipelineRunActiveError(RuntimeError):
    """Raised when a new caller cannot acquire the active-run lease."""

    def __init__(self, status: PipelineStatus) -> None:
        self.status = status
        super().__init__(f"Pipeline run {status.run_id} is already running.")


class PipelineRunInactiveError(RuntimeError):
    """Raised when a child can no longer attach to its expected run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Pipeline run {run_id} is no longer active.")


class PipelineStatusRepository:
    """Read and atomically replace the status artifact.

    Missing, corrupt, and unsupported artifacts are deliberately treated as
    idle so status rendering cannot take down the web application.
    """

    def __init__(self, path: Path, *, stale_after_seconds: float = 1860) -> None:
        self.path = Path(path)
        self.history_path = self.path.with_name(
            f"{self.path.stem}_history{self.path.suffix}"
        )
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Hold the in-process and interprocess file locks until commit."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.lock_path, "a+b") as lock_stream:
            _acquire_lock(lock_stream)
            try:
                yield
            finally:
                _release_lock(lock_stream)

    def load(self) -> PipelineStatus:
        with self._transaction():
            status = self._read()
            if status.state is PipelineState.RUNNING:
                now = self._now()
                if self._is_stale(status, now):
                    status = self._recover_interrupted(status)
                    self._write(status)
                else:
                    status.elapsed_seconds = self._elapsed(status, now)
            return status

    def _read(self) -> PipelineStatus:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return PipelineStatus.idle()
            if payload.get("schema_version") != 1:
                return PipelineStatus.idle()
            return PipelineStatus.model_validate(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValidationError):
            return PipelineStatus.idle()

    def _write(self, status: PipelineStatus) -> None:
        self._write_payload(self.path, status.model_dump(mode="json"))

    def _write_payload(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _read_history(self) -> list[PipelineStatus]:
        """Return archived terminal runs, skipping any unreadable entries."""
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                return []
            history: list[PipelineStatus] = []
            for item in payload:
                try:
                    history.append(PipelineStatus.model_validate(item))
                except (TypeError, ValidationError):
                    continue
            return history
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _archive(self, status: PipelineStatus) -> None:
        """Append a terminal run to bounded history, keeping the newest 100."""
        history = self._read_history()
        history = [item for item in history if item.run_id != status.run_id]
        history.append(status.model_copy(deep=True))
        self._write_payload(
            self.history_path,
            [item.model_dump(mode="json") for item in history[-100:]],
        )

    def find_run(self, run_id: str) -> PipelineStatus | None:
        """Return the current or archived status for *run_id*, if any.

        Used to enrich the freshness display with a run's source-health
        coverage. Freshness *timing* itself must come from the analysis
        artifact's own embedded ownership metadata (see
        ``app.schemas.analysis_artifact``), not from this lookup — see that
        module's docstring for why a second, separately-written fact about
        artifact ownership is unsafe to rely on across a crash.
        """
        with self._transaction():
            current = self._read()
            if current.run_id == run_id:
                return current
            for item in self._read_history():
                if item.run_id == run_id:
                    return item
            return None

    def update_source_health(
        self,
        source_health: dict[SourceName | str, SourceHealth],
        *,
        expected_run_id: str,
    ) -> PipelineStatus:
        """Persist per-source coverage for the active run, if it still owns it."""
        with self._transaction():
            status = self._read()
            if (
                status.run_id != expected_run_id
                or status.state is not PipelineState.RUNNING
            ):
                return status
            status.source_health = {
                SourceName(source): health for source, health in source_health.items()
            }
            now = self._now()
            status.updated_at = now
            status.elapsed_seconds = self._elapsed(status, now)
            self._write(status)
            return status

    def start(
        self,
        *,
        run_id: str | None = None,
        expected_run_id: str | None = None,
    ) -> PipelineStatus:
        with self._transaction():
            current = self._read()
            if current.state is PipelineState.RUNNING and self._is_stale(
                current, self._now()
            ):
                current = self._recover_interrupted(current)
                self._write(current)

            if expected_run_id is not None:
                if (
                    current.state is PipelineState.RUNNING
                    and current.run_id == expected_run_id
                ):
                    return current
                if current.state is PipelineState.RUNNING:
                    raise PipelineRunActiveError(current)
                if current.run_id is not None:
                    raise PipelineRunInactiveError(expected_run_id)
            elif current.state is PipelineState.RUNNING:
                raise PipelineRunActiveError(current)

            now = self._now()
            status = PipelineStatus(
                run_id=expected_run_id or run_id or str(uuid4()),
                state=PipelineState.RUNNING,
                started_at=now,
                updated_at=now,
            )
            self._write(status)
        return status

    def _is_stale(self, status: PipelineStatus, now: datetime) -> bool:
        return (now - status.updated_at).total_seconds() > self.stale_after_seconds

    def transition(
        self,
        stage: PipelineStage,
        state: StageState,
        *,
        expected_run_id: str,
        current: int | None = None,
        total: int | None = None,
        substage: str | None = None,
    ) -> PipelineStatus:
        with self._transaction():
            status = self._read()
            if (
                status.run_id != expected_run_id
                or status.state is not PipelineState.RUNNING
            ):
                return status
            now = self._now()
            item = status.stage(stage)
            if state is StageState.RUNNING:
                item.started_at = item.started_at or now
                status.current_stage = stage
            if state in {StageState.COMPLETE, StageState.SKIPPED, StageState.FAILED}:
                item.started_at = item.started_at or now
                item.completed_at = now
                item.duration_seconds = round(
                    (now - item.started_at).total_seconds(), 3
                )
            item.state = state
            if current is not None:
                item.current = current
            if total is not None:
                item.total = total
            if substage is not None:
                item.substage = substage
            status.updated_at = now
            status.elapsed_seconds = self._elapsed(status, now)
            self._write(status)
            return status

    def update_counts(
        self,
        *,
        expected_run_id: str,
        scanned: int | None = None,
        analysed: int | None = None,
        actionable: int | None = None,
    ) -> PipelineStatus:
        with self._transaction():
            status = self._read()
            if (
                status.run_id != expected_run_id
                or status.state is not PipelineState.RUNNING
            ):
                return status
            if scanned is not None:
                status.scanned = scanned
            if analysed is not None:
                status.analysed = analysed
            if actionable is not None:
                status.actionable = actionable
            now = self._now()
            status.updated_at = now
            status.elapsed_seconds = self._elapsed(status, now)
            self._write(status)
            return status

    def finish(
        self,
        state: PipelineState,
        *,
        expected_run_id: str,
        scanned: int | None = None,
        analysed: int | None = None,
        actionable: int | None = None,
        error_summary: str | None = None,
        artifact_produced: bool = False,
    ) -> PipelineStatus:
        """Record the terminal outcome for *expected_run_id* and archive it.

        ``artifact_produced`` is informational only (surfaced in run history
        for diagnostics); it is set synchronously by the caller in the same
        breath as computing ``state``, so there is no separate write for it
        to race against. It must never be used as the sole signal for "is
        this run's analysis artifact the one currently on disk" — that
        question is answered by reading the artifact's own embedded
        ownership metadata (see ``app.schemas.analysis_artifact``).
        """
        if state not in {
            PipelineState.COMPLETE,
            PipelineState.PARTIAL,
            PipelineState.FAILED,
            PipelineState.SKIPPED,
        }:
            raise ValueError(f"{state} is not a terminal pipeline state")
        with self._transaction():
            status = self._read()
            if status.run_id != expected_run_id:
                return status
            now = self._now()
            had_running_stage = any(
                item.state is StageState.RUNNING for item in status.stages
            )
            for item in status.stages:
                if item.state is StageState.RUNNING:
                    item.state = (
                        StageState.FAILED
                        if state is PipelineState.FAILED
                        else StageState.COMPLETE
                    )
                    item.completed_at = now
                    item.duration_seconds = round(
                        (now - (item.started_at or now)).total_seconds(), 3
                    )
                elif item.state is StageState.PENDING:
                    item.state = StageState.SKIPPED
            if (
                state is PipelineState.FAILED
                and not had_running_stage
                and status.current_stage is not None
            ):
                # A failure can occur between a substage's completion and the
                # next transition. Attribute it to the latest active stage.
                status.stage(status.current_stage).state = StageState.FAILED
            status.state = state
            status.completed_at = now
            status.updated_at = now
            status.elapsed_seconds = self._elapsed(status, now)
            status.error_summary = self._safe_error(error_summary)
            status.analysis_artifact_produced = artifact_produced
            if scanned is not None:
                status.scanned = scanned
            if analysed is not None:
                status.analysed = analysed
            if actionable is not None:
                status.actionable = actionable
            self._write(status)
            self._archive(status)
            return status

    def _recover_interrupted(self, status: PipelineStatus) -> PipelineStatus:
        now = self._now()
        if status.current_stage is not None:
            item = status.stage(status.current_stage)
            item.state = StageState.FAILED
            item.completed_at = now
            item.duration_seconds = round(
                (now - (item.started_at or status.started_at or now)).total_seconds(), 3
            )
        status.state = PipelineState.FAILED
        status.completed_at = now
        status.updated_at = now
        status.elapsed_seconds = self._elapsed(status, now)
        status.error_summary = "Pipeline interrupted before completion."
        return status

    @staticmethod
    def _elapsed(status: PipelineStatus, now: datetime) -> float:
        if status.started_at is None:
            return 0.0
        return round(max(0.0, (now - status.started_at).total_seconds()), 1)

    @staticmethod
    def _safe_error(error: str | None) -> str | None:
        if not error:
            return None
        return " ".join(error.split())[:500]
