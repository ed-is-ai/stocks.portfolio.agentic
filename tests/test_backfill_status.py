"""Tests for the in-process snapshot-backfill progress tracker (#508)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.api.routes.portfolio as portfolio_routes
from app.api.app import app
from app.services.backfill_status import (
    _TERMINAL_TTL,
    BackfillStatusTracker,
)


def _begun(pid: int = 7, days_total: int = 100) -> BackfillStatusTracker:
    tracker = BackfillStatusTracker()
    tracker.begin(
        pid, days_total=days_total, first_day="2020-09-24", last_day="2026-09-04"
    )
    return tracker


def test_begin_starts_in_the_evidence_phase_and_reports_running() -> None:
    tracker = _begun()

    progress = tracker.get(7)

    assert progress is not None
    assert progress.phase == "evidence"
    assert progress.running is True
    assert progress.days_total == 100
    assert progress.first_day == "2020-09-24"
    assert tracker.is_running(7) is True


def test_phase_and_counters_advance_through_a_run() -> None:
    tracker = _begun()

    tracker.set_evidence(7, 3, 12)
    tracker.enter_valuing(7)
    tracker.advance(7, days_done=50, rows_written=31)
    progress = tracker.get(7)

    assert progress is not None
    assert (progress.tickers_done, progress.tickers_total) == (3, 12)
    assert progress.phase == "valuing"
    assert (progress.days_done, progress.rows_written) == (50, 31)
    assert progress.running is True


def test_complete_marks_terminal_and_keeps_the_counts() -> None:
    tracker = _begun()
    tracker.enter_valuing(7)

    tracker.complete(7, rows_written=373, days_done=2172, newly_unavailable=("TR28",))
    progress = tracker.get(7)

    assert progress is not None
    assert progress.phase == "done"
    assert progress.running is False
    assert progress.rows_written == 373
    assert progress.newly_unavailable == ("TR28",)
    assert progress.has_gaps is True
    assert tracker.is_running(7) is False


def test_fail_records_the_error_and_stops_running() -> None:
    tracker = _begun()

    tracker.fail(7, "database is locked")
    progress = tracker.get(7)

    assert progress is not None
    assert progress.phase == "failed"
    assert progress.error == "database is locked"
    assert tracker.is_running(7) is False


def test_a_terminal_entry_is_evicted_once_it_ages_out() -> None:
    tracker = _begun()
    tracker.complete(7, rows_written=1, days_done=1)
    entry = tracker.get(7)
    assert entry is not None

    # Backdate the finish so the next read is past the TTL.
    stale = entry.model_copy(
        update={
            "finished_at": datetime.now(timezone.utc)
            - _TERMINAL_TTL
            - timedelta(seconds=1)
        }
    )
    tracker._entries[7] = stale  # noqa: SLF001 -- exercising the eviction path

    assert tracker.get(7) is None


def test_unknown_portfolio_is_not_running_and_has_no_progress() -> None:
    tracker = BackfillStatusTracker()

    assert tracker.get(99) is None
    assert tracker.is_running(99) is False


def test_a_late_callback_after_eviction_does_not_resurrect_the_entry() -> None:
    tracker = BackfillStatusTracker()

    tracker.advance(7, days_done=10, rows_written=2)

    assert tracker.get(7) is None


def test_elapsed_seconds_is_frozen_once_finished() -> None:
    tracker = _begun()
    tracker.complete(7, rows_written=0, days_done=0)

    progress = tracker.get(7)
    assert progress is not None
    first = progress.elapsed_seconds
    second = tracker.get(7).elapsed_seconds  # type: ignore[union-attr]

    assert first == second  # a finished run's clock does not keep ticking


# --- #132: the poll answers straight from the in-memory tracker -------------


class _OnlyGet:
    """A tracker stand-in that exposes ``get`` alone and counts its reads."""

    def __init__(self, tracker: BackfillStatusTracker) -> None:
        self._tracker = tracker
        self.reads = 0

    def get(self, portfolio_id: int):
        self.reads += 1
        return self._tracker.get(portfolio_id)


def _poll(monkeypatch, tracker: BackfillStatusTracker) -> tuple[_OnlyGet, list[str]]:
    fake = _OnlyGet(tracker)
    monkeypatch.setattr(portfolio_routes, "backfill_tracker", fake)
    client = TestClient(app)
    bodies = [
        client.get("/api/portfolio/backfill-status?portfolio_id=7").text
        for _ in range(2)
    ]
    return fake, bodies


def test_a_repeat_backfill_poll_reads_only_the_tracker(monkeypatch) -> None:
    fake, bodies = _poll(monkeypatch, _begun())

    assert fake.reads == 2
    assert bodies[0] == bodies[1]
    assert 'hx-trigger="every 2s"' in bodies[0]


def test_a_terminal_backfill_poll_is_a_one_shot_reload(monkeypatch) -> None:
    tracker = _begun()
    tracker.complete(7, days_done=100, rows_written=31)

    fake, bodies = _poll(monkeypatch, tracker)

    assert fake.reads == 2
    assert 'hx-trigger="load delay:8s"' in bodies[0]
    assert "every 2s" not in bodies[0]
