"""Route and rendered-accessibility tests for Strategy Manager Story 1.9."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_backtest_repository, get_strategy_job_service
from app.repositories.backtest_repo import BacktestIntegrityError
from app.services.backtest.strategy_job import StrategyJobStatus, StrategyJobType

client = TestClient(app)


@dataclass
class FakeProfile:
    profile_hash: str = "a" * 64
    calendar_dataset_version: str = "exchange-calendars-v1"


class FakeRepo:
    def __init__(self, *, qualified: bool = True, coverage_error: str | None = None):
        self.qualified = qualified
        self.coverage_error = coverage_error
        self.profile = FakeProfile()
        self.activity = None

    def snapshot_coverage(self):
        if self.coverage_error:
            raise BacktestIntegrityError(self.coverage_error)
        return SimpleNamespace(
            display_version="Scanner v1",
            earliest_month="2024-01",
            latest_month="2024-03",
            snapshot_count=3,
            intervals=(SimpleNamespace(start_month="2024-01", end_month="2024-03"),),
            provenance=(
                SimpleNamespace(
                    provenance_quality="best_effort_reconstructed",
                    snapshot_count=3,
                    intervals=(
                        SimpleNamespace(start_month="2024-01", end_month="2024-03"),
                    ),
                ),
            ),
        )

    def active_snapshot_profile(self):
        return SimpleNamespace(profile_hash=self.profile.profile_hash)

    def snapshot_profile(self, _hash):
        return self.profile

    def current_qualification_contract_digest(self):
        return "b" * 64 if self.qualified else None

    def list_strategy_jobs(self):
        return () if self.activity is None else (self.activity,)

    def interval_readiness(self, *_args):
        return SimpleNamespace(no_op=False)

    def strategy_job(self, job_id):
        if self.activity is None or job_id != self.activity.id:
            from app.services.backtest.strategy_job import StrategyJobNotFound

            raise StrategyJobNotFound("missing")
        return self.activity

    def initialization_run(self, _job_id):
        return SimpleNamespace(requested_start="2024-01", requested_end="2024-03")

    def strategy_run(self, _job_id):
        return SimpleNamespace(
            strategy_id="momentum_v1", start_month="2024-01", end_month="2024-03"
        )


class FakeJobs:
    def __init__(self):
        self.submissions = []

    def enqueue_initialization(self, submission):
        self.submissions.append(submission)
        return SimpleNamespace(no_op=False, job=SimpleNamespace(id="job-1"))

    def legal_actions(self, job_id):
        return SimpleNamespace(job_id=job_id, legal_actions=("cancel",))

    def request_cancellation(self, request):
        self.cancel_request = request

    def restart_backtest(self, request):
        self.restart_request = request
        return SimpleNamespace(job=SimpleNamespace(id="job-2"))


@pytest.fixture
def services(monkeypatch):
    repo, jobs = FakeRepo(), FakeJobs()
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_job_service] = lambda: jobs
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repo, jobs
    finally:
        app.dependency_overrides.clear()


def test_main_renders_coverage_and_canonical_reconstruction_warning(services):
    response = client.get("/partials/strategy-manager")
    assert response.status_code == 200
    assert "Scanner v1" in response.text
    assert "Best-effort yfinance" in response.text
    assert (
        "Survivorship-biased reconstruction; not a point-in-time market universe."
        in response.text
    )
    assert "source-gap" not in response.text


def test_initialization_is_disabled_when_not_qualified(services):
    repo, _ = services
    repo.qualified = False
    response = client.get("/strategy-manager/initialization")
    assert response.status_code == 200
    assert "providers have not passed certification" in response.text
    assert "disabled" in response.text


def test_invalid_range_has_linked_errors_and_no_aria_invalid_when_clean(services):
    response = client.post(
        "/strategy-manager/initialization",
        data={"start_month": "2026-02", "end_month": "2026-01"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert 'id="initialization-errors"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert 'aria-describedby="end-month-help end-month-error"' in response.text
    assert 'aria-describedby="start-month-help"' in response.text


def test_valid_submission_enqueues_once_and_redirects(services):
    _, jobs = services
    response = client.post(
        "/strategy-manager/initialization",
        data={"start_month": "2024-01", "end_month": "2024-02"},
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-1"
    assert len(jobs.submissions) == 1


def test_initialization_mutation_requires_guard(services):
    response = client.post(
        "/strategy-manager/initialization",
        data={"start_month": "2024-01", "end_month": "2024-02"},
    )
    assert response.status_code == 403


def test_tab_is_registered_in_the_application_shell():
    response = client.get("/")
    assert 'id="tab-strategy-manager"' in response.text


def test_activity_shows_only_authoritative_running_month_and_one_live_region(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.INITIALIZATION,
        status=StrategyJobStatus.RUNNING,
        status_version=4,
        current_month="2024-02",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get("/strategy-manager/activities/job-1")
    assert response.status_code == 200
    assert "Current month:" in response.text
    assert "2024-02" in response.text
    assert response.text.count('role="status"') == 1
    assert (
        'hx-get="/strategy-manager/activities/job-1/status?last_seen_version=4"'
        in response.text
    )


def test_activity_poll_drops_same_or_older_version(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.INITIALIZATION,
        status=StrategyJobStatus.QUEUED,
        status_version=4,
        current_month=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get(
        "/strategy-manager/activities/job-1/status?last_seen_version=4"
    )
    assert response.status_code == 204
    assert response.text == ""


def test_cancel_is_guarded_and_uses_live_action_version(services):
    repo, jobs = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.INITIALIZATION,
        status=StrategyJobStatus.RUNNING,
        status_version=4,
        current_month=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    denied = client.post(
        "/strategy-manager/activities/job-1/cancel", data={"expected_version": "4"}
    )
    assert denied.status_code == 403
    accepted = client.post(
        "/strategy-manager/activities/job-1/cancel",
        data={"expected_version": "4"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert accepted.status_code == 200
    assert jobs.cancel_request.expected_version == 4
    assert "Cancel initialization?" in accepted.text


def test_backtest_activity_renders_a_minimal_status_shell_instead_of_404(services):
    """Story 2.6 AC 9: the Activity route used to 404 for a backtest job
    (``_activity_context`` raised unconditionally for any non-
    initialization job type); it now renders a correct, minimal status
    shell using the Strategy Run's own fields, never initialization-only
    ones."""
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.RUNNING,
        status_version=4,
        current_month="2024-02",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get("/strategy-manager/activities/job-1")
    assert response.status_code == 200
    assert "Backtest activity" in response.text
    assert "momentum_v1" in response.text
    assert "2024-01 to 2024-03" in response.text
    assert "Historical initialization" not in response.text


def test_backtest_activity_status_poll_uses_the_backtest_template(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.RUNNING,
        status_version=5,
        current_month="2024-02",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get(
        "/strategy-manager/activities/job-1/status?last_seen_version=4"
    )
    assert response.status_code == 200
    assert "Backtest activity" in response.text


def test_backtest_cancel_reuses_the_generic_lifecycle_command(services):
    repo, jobs = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.RUNNING,
        status_version=4,
        current_month=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    accepted = client.post(
        "/strategy-manager/activities/job-1/cancel",
        data={"expected_version": "4"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert accepted.status_code == 200
    assert jobs.cancel_request.expected_version == 4
    assert "Cancel backtest?" in accepted.text


def test_backtest_restart_dispatches_to_restart_backtest(services):
    repo, jobs = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.FAILED,
        status_version=4,
        current_month=None,
        cancel_requested_at=None,
        failed_month="2024-02",
        failure_detail="Required historical data is unavailable",
    )
    response = client.post(
        "/strategy-manager/activities/job-1/restart",
        data={"expected_version": "4"},
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-2"
    assert jobs.restart_request.source_job_id == "job-1"
