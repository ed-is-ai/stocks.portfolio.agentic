from __future__ import annotations

import threading
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_backtest_repository


class FakeStrategyJobs:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconcile_startup(self):
        self.calls.append("reconcile")
        return ()

    def start_dispatcher(self):
        self.calls.append("start")

    def shutdown(self):
        self.calls.append("shutdown")


def test_lifespan_reconciles_before_dispatch_and_shuts_owned_worker() -> None:
    service = FakeStrategyJobs()
    app = create_app(
        strategy_job_service=service,
        strategy_jobs_enabled=True,
    )

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert service.calls == ["reconcile", "start"]

    assert service.calls == ["reconcile", "start", "shutdown"]


def test_lifespan_can_disable_real_workers_for_tests() -> None:
    service = FakeStrategyJobs()
    app = create_app(
        strategy_job_service=service,
        strategy_jobs_enabled=False,
    )

    with TestClient(app):
        pass

    assert service.calls == []


def _wait_for_preparation(client: TestClient, expected: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if client.get("/startup-status").json() == {"status": expected}:
            return
        time.sleep(0.01)
    raise AssertionError(f"Startup preparation never became {expected}")


def test_boot_prepares_with_workers_disabled_and_serves_while_pending() -> None:
    started, release = threading.Event(), threading.Event()

    def prepare() -> None:
        started.set()
        assert release.wait(3)

    app = create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=prepare)
    with TestClient(app) as client:
        try:
            assert started.wait(1)
            assert client.get("/startup-status").json() == {"status": "pending"}
            assert client.get("/").status_code == 200
        finally:
            release.set()
        _wait_for_preparation(client, "ready")


def test_boot_failure_is_safe_and_does_not_prevent_serving() -> None:
    def prepare() -> None:
        raise ValueError("private database path and evidence")

    app = create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=prepare)
    with TestClient(app) as client:
        _wait_for_preparation(client, "failed")
        assert "private" not in client.get("/startup-status").text
        assert client.get("/").status_code == 200


def test_boot_without_active_profile_skips_preparation(monkeypatch) -> None:
    class EmptyRepository:
        def active_snapshot_profile(self):
            return None

        def prepare_snapshot_coverage(self):
            raise AssertionError("No active profile should skip preparation")

    monkeypatch.setattr(
        "app.api.app.get_backtest_repository", lambda: EmptyRepository()
    )
    with TestClient(create_app(strategy_jobs_enabled=False)) as client:
        _wait_for_preparation(client, "ready")


def test_shutdown_joins_startup_preparation() -> None:
    started, release = threading.Event(), threading.Event()
    exiting, finished = threading.Event(), threading.Event()

    def prepare():
        started.set()
        assert release.wait(3)

    def serve():
        with TestClient(
            create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=prepare)
        ):
            assert started.wait(1)
            exiting.set()
        finished.set()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert exiting.wait(2)
        assert not finished.wait(0.05)
    finally:
        release.set()
        thread.join(timeout=3)
    assert finished.is_set()


def test_strategy_request_waits_without_blocking_startup_status():
    repository = get_backtest_repository()
    started, release = threading.Event(), threading.Event()

    def prepare():
        with repository._snapshot_coverage_lock:
            started.set()
            assert release.wait(3)

    with (
        TestClient(
            create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=prepare)
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert started.wait(1)
        response = pool.submit(client.get, "/strategy-manager")
        try:
            time.sleep(0.05)
            assert not response.done()
            assert client.get("/startup-status").json() == {"status": "pending"}
        finally:
            release.set()
        assert response.result(timeout=3).status_code == 200


@pytest.mark.asyncio
async def test_cancelled_lifespan_joins_preparation_before_worker_shutdown():
    release = threading.Event()
    entered = asyncio.Event()
    service = FakeStrategyJobs()
    app = create_app(
        strategy_job_service=service,
        strategy_jobs_enabled=True,
        prepare_strategy_coverage=lambda: release.wait(3),
    )

    async def serve():
        async with app.router.lifespan_context(app):
            entered.set()

    task = asyncio.create_task(serve())
    await entered.wait()
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert "shutdown" not in service.calls
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.calls[-1] == "shutdown"


def test_splash_waits_for_preparation_and_has_recovery_message() -> None:
    with TestClient(
        create_app(strategy_jobs_enabled=False, prepare_strategy_coverage=lambda: None)
    ) as client:
        html = client.get("/").text
        assert "fetch('/startup-status'" in html
        assert (
            "preparationReady && ((pageLoaded && tabSwapped) || hydrationExpired)"
            in html
        )
        assert "controller.abort()" in html
        assert (
            "Check Strategy Manager readiness for details; other screens remain available."
            in html
        )
        css = client.get("/static/css/splash.css").text
        assert '.boot-splash[data-preparation="pending"]' in css
