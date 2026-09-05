"""In-process progress for the portfolio snapshot backfill (#508).

The #502 backfill runs as a FastAPI ``BackgroundTasks`` job *inside the web
process*, so its progress can live in memory here -- no file/DB persistence
like ``PipelineStatusRepository`` (which exists because the pipeline shells
out to a subprocess). ``GET /api/portfolio/backfill-status`` reads this to
render a status bar and keep the Refresh Prices spinner going until the fill
actually finishes, not just until the price fetch returns.

Single-process only: with ``uvicorn --workers >1`` each worker holds its own
tracker and a poll may hit a worker that never ran the backfill. The app is
single-process today; move this to a file-backed store (mirroring
``PipelineStatusRepository``) if that ever changes.

Progress is lost on a web-server restart mid-run -- the bar simply stops
updating and the next poll clears it. The backfill itself is idempotent and
resumable, so no data is affected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: A terminal (done/failed) entry is served for this long so the bar can show
#: its result, then evicted on the next read so an idle page stays clean.
_TERMINAL_TTL = timedelta(seconds=90)

BackfillPhase = Literal["evidence", "valuing", "done", "failed"]


class BackfillProgress(BaseModel):
    """An immutable snapshot of one portfolio's backfill run.

    ``days_total`` counts every calendar day in the fill window; ``rows_written``
    only the days that produced a snapshot, so the two advance at different
    rates (weekends/holidays/evidence gaps produce no row) -- the status bar
    shows both so a slow stretch never looks stuck.
    """

    model_config = ConfigDict(frozen=True)

    portfolio_id: int
    phase: BackfillPhase
    tickers_done: int = 0
    tickers_total: int = 0
    days_done: int = 0
    days_total: int = 0
    rows_written: int = 0
    started_at: datetime
    finished_at: datetime | None = None
    first_day: str | None = None
    last_day: str | None = None
    fetch_failures: tuple[str, ...] = ()
    newly_unavailable: tuple[str, ...] = ()
    error: str | None = None

    @property
    def running(self) -> bool:
        """True while the run is still doing work."""
        return self.phase in ("evidence", "valuing")

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock seconds from start to finish (or to now while running)."""
        end = self.finished_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def has_gaps(self) -> bool:
        """True if some ticker's evidence could not be acquired."""
        return bool(self.fetch_failures or self.newly_unavailable)


class BackfillStatusTracker:
    """Thread-safe in-memory map of ``portfolio_id -> BackfillProgress``.

    ``SnapshotBackfillService`` publishes into it as it works; the status
    route reads it. Every mutator replaces the stored model wholesale under
    the lock, so a reader always sees a consistent snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, BackfillProgress] = {}

    def begin(
        self, portfolio_id: int, *, days_total: int, first_day: str, last_day: str
    ) -> None:
        """Record that a run has started for ``portfolio_id`` (evidence phase)."""
        with self._lock:
            self._entries[portfolio_id] = BackfillProgress(
                portfolio_id=portfolio_id,
                phase="evidence",
                days_total=days_total,
                first_day=first_day,
                last_day=last_day,
                started_at=datetime.now(timezone.utc),
            )

    def set_evidence(self, portfolio_id: int, done: int, total: int) -> None:
        """Update the "fetching price history (done/total tickers)" counter."""
        self._update(portfolio_id, tickers_done=done, tickers_total=total)

    def enter_valuing(self, portfolio_id: int) -> None:
        """Mark the transition from evidence acquisition to day valuation."""
        self._update(portfolio_id, phase="valuing")

    def advance(self, portfolio_id: int, *, days_done: int, rows_written: int) -> None:
        """Update the live day counter and rows-written total."""
        self._update(portfolio_id, days_done=days_done, rows_written=rows_written)

    def complete(
        self,
        portfolio_id: int,
        *,
        rows_written: int,
        days_done: int,
        fetch_failures: tuple[str, ...] = (),
        newly_unavailable: tuple[str, ...] = (),
    ) -> None:
        """Record a successful finish; the entry lingers for ``_TERMINAL_TTL``."""
        self._update(
            portfolio_id,
            phase="done",
            finished_at=datetime.now(timezone.utc),
            rows_written=rows_written,
            days_done=days_done,
            fetch_failures=fetch_failures,
            newly_unavailable=newly_unavailable,
        )

    def fail(self, portfolio_id: int, error: str) -> None:
        """Record a failed finish; the entry lingers for ``_TERMINAL_TTL``."""
        self._update(
            portfolio_id,
            phase="failed",
            finished_at=datetime.now(timezone.utc),
            error=error,
        )

    def is_running(self, portfolio_id: int) -> bool:
        """True if a run for ``portfolio_id`` is currently in progress."""
        with self._lock:
            entry = self._entries.get(portfolio_id)
            return entry is not None and entry.running

    def get(self, portfolio_id: int) -> BackfillProgress | None:
        """Return the current progress, evicting a stale terminal entry first."""
        with self._lock:
            entry = self._entries.get(portfolio_id)
            if entry is None:
                return None
            if (
                entry.finished_at is not None
                and datetime.now(timezone.utc) - entry.finished_at > _TERMINAL_TTL
            ):
                del self._entries[portfolio_id]
                return None
            return entry

    def _update(self, portfolio_id: int, **fields: object) -> None:
        with self._lock:
            entry = self._entries.get(portfolio_id)
            if entry is None:
                # A late callback after the entry was evicted -- ignore it
                # rather than resurrect a partial record.
                return
            self._entries[portfolio_id] = entry.model_copy(update=fields)


#: Process-wide singleton shared by the service and the status route.
tracker = BackfillStatusTracker()
