from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app


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
