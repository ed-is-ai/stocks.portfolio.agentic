"""Tests for Strategy Manager setup routes (Story 4.3).

Tests setup confirmation, idempotency, unauthorized rejection,
fixture label, and activity redirect.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

import pytest

from app.api.app import app
from app.api.dependencies import (
    get_backtest_repository,
    get_bootstrap_service,
    get_readiness_service,
    get_strategy_job_service,
)
from app.services.backtest.strategy_bootstrap_service import (
    StrategyBootstrapService,
)
from app.services.backtest.strategy_job import (
    BootstrapEnqueueResultV1,
    BootstrapRunV1,
    StrategyJobStatus,
    StrategyJobType,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.strategy_readiness_service import (
    StrategyReadinessService,
)
from fastapi.testclient import TestClient
import tests.test_strategy_manager_routes as routes_helpers

client = TestClient(app)

NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)


class SetupFakeRepo(routes_helpers.FakeRepo):
    """FakeRepo that can simulate no active profile."""

    def __init__(self, *, has_profile: bool = True) -> None:
        super().__init__()
        self._has_profile = has_profile
        self._bootstrap_jobs: list[object] = []
        self._bootstrap_by_key: dict[str, object] = {}

    def active_snapshot_profile(self):
        if not self._has_profile:
            return None
        return routes_helpers.FakeActiveProfile()

    def stored_snapshot_profile(self, profile_hash):
        return None

    def create_bootstrap_job(self, submission):
        from app.services.backtest.strategy_job import StrategyJobV1

        existing = self._bootstrap_by_key.get(submission.idempotency_key)
        if existing is not None:
            return BootstrapEnqueueResultV1(
                no_op=False,
                job=existing,
                bootstrap=BootstrapRunV1(job_id=existing.id),
            )
        if self._has_profile:
            return BootstrapEnqueueResultV1(no_op=True)
        job = StrategyJobV1(
            id=str(uuid.uuid4()),
            job_type=StrategyJobType.BOOTSTRAP,
            status=StrategyJobStatus.QUEUED,
            parent_job_id=submission.parent_job_id,
            enqueue_seq=1,
            status_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self._bootstrap_jobs.append(job)
        self._bootstrap_by_key[submission.idempotency_key] = job
        return BootstrapEnqueueResultV1(
            no_op=False,
            job=job,
            bootstrap=BootstrapRunV1(job_id=job.id),
        )


@pytest.fixture
def setup_env_no_profile(monkeypatch):
    repo = SetupFakeRepo(has_profile=False)
    jobs = StrategyJobService(repo)
    bootstrap = StrategyBootstrapService(repo, jobs=jobs)
    readiness = StrategyReadinessService(repo, clock=NOW)
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_job_service] = lambda: jobs
    app.dependency_overrides[get_bootstrap_service] = lambda: bootstrap
    app.dependency_overrides[get_readiness_service] = lambda: readiness
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repo, jobs, bootstrap
    finally:
        app.dependency_overrides.pop(get_backtest_repository, None)
        app.dependency_overrides.pop(get_strategy_job_service, None)
        app.dependency_overrides.pop(get_bootstrap_service, None)
        app.dependency_overrides.pop(get_readiness_service, None)


@pytest.fixture
def setup_env_with_profile(monkeypatch):
    repo = SetupFakeRepo(has_profile=True)
    jobs = StrategyJobService(repo)
    bootstrap = StrategyBootstrapService(repo, jobs=jobs)
    readiness = StrategyReadinessService(repo, clock=NOW)
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_job_service] = lambda: jobs
    app.dependency_overrides[get_bootstrap_service] = lambda: bootstrap
    app.dependency_overrides[get_readiness_service] = lambda: readiness
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repo, jobs, bootstrap
    finally:
        app.dependency_overrides.pop(get_backtest_repository, None)
        app.dependency_overrides.pop(get_strategy_job_service, None)
        app.dependency_overrides.pop(get_bootstrap_service, None)
        app.dependency_overrides.pop(get_readiness_service, None)


# ---------------------------------------------------------------------------
# Setup page
# ---------------------------------------------------------------------------


def test_setup_page_shows_setup_required(setup_env_no_profile) -> None:
    repo, _jobs, _bootstrap = setup_env_no_profile
    response = client.get("/strategy-manager/setup")
    assert response.status_code == 200
    assert "Set up Strategy Manager" in response.text
    match = re.search(
        r'<input type="hidden" name="idempotency_key" value="([^"]+)">',
        response.text,
    )
    assert match is not None and 1 <= len(match.group(1)) <= 200
    assert repo._bootstrap_jobs == []


def test_setup_page_shows_already_set_up(setup_env_with_profile) -> None:
    response = client.get("/strategy-manager/setup")
    assert response.status_code == 200
    assert "already set up" in response.text.lower()


# ---------------------------------------------------------------------------
# Setup submit
# ---------------------------------------------------------------------------


def test_setup_submit_creates_bootstrap_job(
    setup_env_no_profile,
) -> None:
    page = client.get("/strategy-manager/setup")
    key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text)
    assert key is not None
    response = client.post(
        "/strategy-manager/setup",
        headers={"X-Auth-Token": "s3cret"},
        data={"idempotency_key": key.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/strategy-manager/activities/" in response.headers.get("location", "")


def test_setup_submit_replays_the_same_form_to_the_same_activity(
    setup_env_no_profile,
) -> None:
    repo, _jobs, _bootstrap = setup_env_no_profile
    page = client.get("/strategy-manager/setup")
    key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text)
    assert key is not None
    request = {
        "headers": {"X-Auth-Token": "s3cret", "HX-Request": "true"},
        "data": {"idempotency_key": key.group(1)},
        "follow_redirects": False,
    }
    first = client.post("/strategy-manager/setup", **request)
    second = client.post("/strategy-manager/setup", **request)

    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert len(repo._bootstrap_jobs) == 1


def test_setup_submit_preserves_key_on_correctable_conflict(
    setup_env_no_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, _jobs, bootstrap = setup_env_no_profile

    def conflict(_submission):
        from app.services.backtest.strategy_job import StrategyJobConflict

        raise StrategyJobConflict("competing setup")

    monkeypatch.setattr(bootstrap, "start_setup", conflict)
    response = client.post(
        "/strategy-manager/setup",
        headers={"X-Auth-Token": "s3cret"},
        data={"idempotency_key": "retry-key"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert 'name="idempotency_key" value="retry-key"' in response.text


def test_setup_submit_already_set_up_returns_redirect(
    setup_env_with_profile,
) -> None:
    response = client.post(
        "/strategy-manager/setup",
        headers={"X-Auth-Token": "s3cret"},
        data={"idempotency_key": "active-profile"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/strategy-manager/setup" in response.headers.get("location", "")


# ---------------------------------------------------------------------------
# Unauthorized
# ---------------------------------------------------------------------------


def test_setup_submit_unauthorized_rejected(
    setup_env_no_profile,
) -> None:
    repo, _jobs, _bootstrap = setup_env_no_profile
    response = client.post(
        "/strategy-manager/setup",
        data={"idempotency_key": "unauthorized-submit"},
        follow_redirects=False,
    )
    assert response.status_code in (401, 403)
    assert repo._bootstrap_jobs == []


@pytest.mark.parametrize("idempotency_key", [None, "", "x" * 201])
def test_setup_submit_rejects_invalid_key_without_queuing(
    setup_env_no_profile, idempotency_key: str | None
) -> None:
    repo, _jobs, _bootstrap = setup_env_no_profile
    response = client.post(
        "/strategy-manager/setup",
        headers={"X-Auth-Token": "s3cret"},
        data={} if idempotency_key is None else {"idempotency_key": idempotency_key},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert repo._bootstrap_jobs == []
    if idempotency_key:
        assert idempotency_key not in response.text


# ---------------------------------------------------------------------------
# Readiness page
# ---------------------------------------------------------------------------


def test_readiness_page_returns_200(setup_env_no_profile) -> None:
    response = client.get("/strategy-manager/readiness")
    assert response.status_code == 200
    assert "Qualification" in response.text or "qualification" in (
        response.text.lower()
    )


def test_readiness_page_shows_missing_prerequisites(
    setup_env_no_profile,
) -> None:
    response = client.get("/strategy-manager/readiness")
    assert response.status_code == 200
    assert "missing" in response.text.lower()


def test_readiness_coverage_initialize_is_an_action_link(
    setup_env_with_profile,
) -> None:
    repo, _jobs, _bootstrap = setup_env_with_profile
    repo.result_coverage = type(
        "EmptyCoverage", (), {"snapshot_count": 0}
    )()

    response = client.get("/strategy-manager/readiness")

    assert response.status_code == 200
    assert 'href="/strategy-manager/initialization"' in response.text
    assert 'hx-get="/strategy-manager/initialization"' in response.text
    assert "Initialize</a>" in response.text


# ---------------------------------------------------------------------------
# Diagnostics page
# ---------------------------------------------------------------------------


def test_diagnostics_page_returns_200(setup_env_no_profile) -> None:
    response = client.get("/strategy-manager/diagnostics")
    assert response.status_code == 200


def test_diagnostics_page_shows_no_failures(
    setup_env_no_profile,
) -> None:
    response = client.get("/strategy-manager/diagnostics")
    assert response.status_code == 200
    # No recent failures in a fresh repo
    assert "recent_failures" in response.text or "No recent" in (response.text)


# ---------------------------------------------------------------------------
# Strategy manager main page shows setup banner
# ---------------------------------------------------------------------------


def test_strategy_manager_shows_setup_banner(
    setup_env_no_profile,
) -> None:
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Set up Strategy Manager" in response.text


def test_strategy_manager_no_setup_banner_when_active(
    setup_env_with_profile,
) -> None:
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Set up Strategy Manager" not in response.text
