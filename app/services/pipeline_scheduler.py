"""Recurring pipeline trigger for the web process (#2).

Scheduled runs call the same ``PipelineService.run_once`` path as the Refresh
button, so they share its single-run lock, run lease, status bar and run log.
"""

from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

JOB_ID = "scheduled_pipeline"
# ponytail: a sleeping host misses the run; add a launchd/systemd wake if needed.
MISFIRE_GRACE_SECONDS = 3600

_scheduler: BackgroundScheduler | None = None


def start_pipeline_scheduler(
    cron: str, timezone: str, run: Callable[[], object]
) -> BackgroundScheduler | None:
    """Start a background scheduler for ``run``; an empty ``cron`` disables it."""
    global _scheduler
    if not cron:
        return None
    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        run,
        CronTrigger.from_crontab(cron, timezone=timezone),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


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
