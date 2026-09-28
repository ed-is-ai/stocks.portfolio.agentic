"""Tests for the in-process recurring pipeline trigger (#2)."""

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core import config
from app.services import pipeline_scheduler


@pytest.fixture(autouse=True)
def stop_scheduler():
    yield
    pipeline_scheduler.stop_pipeline_scheduler()


def test_empty_cron_disables_the_schedule() -> None:
    assert pipeline_scheduler.start_pipeline_scheduler("", "UTC", lambda: None) is None
    assert pipeline_scheduler.next_scheduled_run() is None


def test_default_schedule_runs_after_the_us_close_on_weekdays(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_SCHEDULE_CRON")
    pipeline_scheduler.start_pipeline_scheduler(
        config.pipeline_schedule_cron(),
        config.PIPELINE_SCHEDULE_TIMEZONE,
        lambda: None,
    )

    next_run = pipeline_scheduler.next_scheduled_run()

    assert next_run is not None
    local = next_run.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (16, 30)
    assert local.weekday() < 5


def test_scheduled_job_calls_the_shared_run_path() -> None:
    calls: list[str] = []
    scheduler = pipeline_scheduler.start_pipeline_scheduler(
        "0 0 1 1 *", "UTC", lambda: calls.append("run")
    )
    assert scheduler is not None

    job = scheduler.get_job(pipeline_scheduler.JOB_ID)
    assert job is not None
    job.func()

    assert calls == ["run"]
    assert job.max_instances == 1
    assert job.coalesce is True


def test_scheduled_run_refreshes_institutional_sources(monkeypatch) -> None:
    calls: list[dict[str, bool]] = []

    class FakePipeline:
        def run_once(self, **kwargs: bool) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("app.api.app.get_pipeline_service", FakePipeline)
    from app.api.app import _run_scheduled_pipeline

    _run_scheduled_pipeline()

    assert calls == [{"extract": True}]


def test_lifespan_starts_and_stops_the_schedule(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_SCHEDULE_CRON", "0 0 1 1 *")

    with TestClient(create_app(strategy_jobs_enabled=False)) as client:
        assert pipeline_scheduler.next_scheduled_run() is not None
        page = client.get("/")
        assert "Next scheduled refresh" in page.text

    assert pipeline_scheduler.next_scheduled_run() is None


def test_menu_omits_next_run_when_unscheduled() -> None:
    with TestClient(create_app(strategy_jobs_enabled=False)) as client:
        page = client.get("/")

    assert "Next scheduled refresh" not in page.text
