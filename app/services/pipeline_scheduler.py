"""Recurring pipeline trigger for the web process (#2).

Scheduled runs call the same ``PipelineService.run_once`` path as the Refresh
button, so they share its single-run lock, run lease, status bar and run log.
"""

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

JOB_ID = "scheduled_pipeline"
RESYNC_JOB_ID = "wall_clock_resync"
# A run that fires late after the host wakes still counts. Two hours covers a
# wake scheduled in local time during the weeks UK and US clocks change on
# different dates, and stays on the same UTC date as the default 16:30 run.
MISFIRE_GRACE_SECONDS = 7200
# APScheduler waits on the monotonic clock, which stops while macOS sleeps, so
# a wait started before sleep would end hours late. A short no-op tick makes
# the scheduler re-read the wall clock soon after the host wakes.
RESYNC_SECONDS = 30

_scheduler: BackgroundScheduler | None = None


def start_pipeline_scheduler(
    cron: str, timezone: str, run: Callable[[], object]
) -> BackgroundScheduler | None:
    """Start a background scheduler for ``run``; an empty ``cron`` disables it."""
    global _scheduler
    if not cron:
        return None
    # The resync tick would log two INFO lines every 30s; runs are recorded
    # in the pipeline run log instead.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        _stay_awake(run),
        CronTrigger.from_crontab(cron, timezone=timezone),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        _resync,
        IntervalTrigger(seconds=RESYNC_SECONDS),
        id=RESYNC_JOB_ID,
        coalesce=True,
        misfire_grace_time=None,
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def _resync() -> None:
    """Do nothing; running at all makes the scheduler re-read the clock."""


def _stay_awake(run: Callable[[], object]) -> Callable[[], None]:
    """Wrap ``run`` so macOS does not idle-sleep until it finishes.

    ``caffeinate -w`` also releases the assertion if this process dies. On
    hosts without ``caffeinate`` the run is left as it is.
    """

    def job() -> None:
        caffeinate = shutil.which("caffeinate")
        guard = (
            subprocess.Popen([caffeinate, "-i", "-w", str(os.getpid())])
            if caffeinate
            else None
        )
        try:
            run()
        finally:
            if guard is not None:
                guard.terminate()

    return job


def stop_pipeline_scheduler() -> None:
    """Stop the running scheduler without waiting for an in-flight run."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_scheduled_run() -> datetime | None:
    """Return when the next scheduled run fires, or ``None`` when unscheduled."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job is not None else None
