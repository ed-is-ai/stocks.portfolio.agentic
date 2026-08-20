"""Route and rendered-accessibility tests for Strategy Manager Story 1.9."""

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_backtest_launch_service,
    get_backtest_repository,
    get_strategy_job_service,
)
from app.repositories.backtest_repo import (
    BacktestActivitySummaryV1,
    BacktestIntegrityError,
)
from app.services.backtest.backtest_launch_service import (
    BacktestConfigurationViewV1,
    BacktestLaunchValidationError,
    LaunchFieldError,
)
from app.services.backtest.metrics import BacktestMetricsV1, MetricAvailabilityV1
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.snapshot_profile import CoverageSummaryV1, SnapshotProfileV1
from app.services.backtest.strategy_job import (
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)
from app.services.backtest.strategy_protocol import StrategyParameterV1

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
        self.backtest_activities: tuple[object, ...] = ()
        self.backtest_activities_error: BacktestIntegrityError | None = None

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

    def list_backtest_activities(self):
        if self.backtest_activities_error is not None:
            raise self.backtest_activities_error
        return self.backtest_activities


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


def test_backtest_activity_review_url_only_on_complete(services):
    """Story 2.8 AC3: a completed Backtest exposes a named review link;
    any non-complete state must not render one."""
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
    running = client.get("/strategy-manager/activities/job-1")
    assert "Review this Backtest" not in running.text

    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.COMPLETE,
        status_version=5,
        current_month=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    completed = client.get("/strategy-manager/activities/job-1")
    assert "Review this Backtest" in completed.text
    assert 'href="/strategy-manager/activities/job-1"' in completed.text


# ---------------------------------------------------------------------------
# Story 2.7: configuration/launch routes
# ---------------------------------------------------------------------------


def _param(**overrides: object) -> StrategyParameterV1:
    defaults: dict[str, object] = dict(
        name="p",
        type="string",
        default="x",
        description="d",
        required=False,
        minimum=None,
        maximum=None,
        enum_values=None,
    )
    defaults.update(overrides)
    return StrategyParameterV1(**defaults)  # type: ignore[arg-type]


ALPHA_PARAMETERS = (
    _param(
        name="lookback",
        type="integer",
        default=20,
        description="Lookback window",
        required=True,
        minimum=1,
        maximum=100,
    ),
    _param(
        name="threshold",
        type="number",
        default=1.5,
        description="Signal threshold",
        minimum=0.0,
        maximum=10.0,
    ),
    _param(
        name="enabled",
        type="boolean",
        default=True,
        description="Toggle",
    ),
    _param(
        name="label",
        type="string",
        default="x",
        description="A label",
    ),
    _param(
        name="mode",
        type="enum",
        default="a",
        description="Mode",
        enum_values=("a", "b", "c"),
    ),
)

STRATEGY_ALPHA = StrategyDescriptorV1(
    strategy_id="alpha",
    source_manifest_version="strategy_source_manifest.v1",
    source_digest="a" * 64,
    display_name="Alpha",
    description="Alpha strategy",
    api_version=1,
    parameters=ALPHA_PARAMETERS,
    default_parameters={
        "lookback": 20,
        "threshold": 1.5,
        "enabled": True,
        "label": "x",
        "mode": "a",
    },
    runtime_path="alpha/scripts/strategy.py",
)


class FakeLaunchService:
    """Minimal fake mirroring ``BacktestLaunchService``'s public surface."""

    def __init__(
        self,
        *,
        strategies: tuple[StrategyDescriptorV1, ...] = (STRATEGY_ALPHA,),
        coverage: object | None = None,
        coverage_error: str | None = None,
        launch_error: BacktestLaunchValidationError | None = None,
    ) -> None:
        self.strategies = strategies
        self.coverage = coverage or SimpleNamespace(
            display_version="Scanner v1",
            intervals=(SimpleNamespace(start_month="2024-01", end_month="2024-03"),),
        )
        self.coverage_error = coverage_error
        self.launch_error = launch_error
        self.launch_calls: list[object] = []
        self.launch_result = SimpleNamespace(job=SimpleNamespace(id="job-1"))

    def discover(self):
        return SimpleNamespace(strategies=self.strategies, warnings=())

    def configuration(self) -> BacktestConfigurationViewV1:
        return BacktestConfigurationViewV1(
            strategies=self.strategies,
            warnings=(),
            coverage=cast(CoverageSummaryV1, self.coverage),
            coverage_error=self.coverage_error,
            profile=cast(SnapshotProfileV1, SimpleNamespace(profile_hash="a" * 64)),
        )

    def launch(self, command):
        self.launch_calls.append(command)
        if self.launch_error is not None:
            raise self.launch_error
        return self.launch_result


@pytest.fixture
def launch(monkeypatch):
    fake = FakeLaunchService()
    app.dependency_overrides[get_backtest_launch_service] = lambda: fake
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_backtest_launch_service, None)


def test_configuration_zero_strategies_explains_state(launch):
    launch.strategies = ()
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    assert "No valid Strategy Skills are currently discoverable" in response.text
    # No form/Run Backtest button renders at all -- not just "not disabled".
    assert "Run Backtest" not in response.text
    assert "disabled" not in response.text


def test_configuration_switching_strategy_preserves_period_capital_currency(launch):
    """Switching the selected Strategy via the fields partial must not
    discard Period/Capital/Currency values the user already entered --
    they're unrelated to which Strategy is chosen."""
    response = client.get(
        "/strategy-manager/configuration/fields",
        params={
            "strategy_id": "alpha",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "USD",
            "starting_capital": "5000",
        },
    )
    assert response.status_code == 200
    assert 'value="2024-01" selected' in response.text
    assert 'value="2024-02" selected' in response.text
    assert '<option value="USD" selected>' in response.text
    assert 'value="5000"' in response.text


def test_configuration_selecting_strategy_renders_its_parameters(launch):
    response = client.get("/strategy-manager/configuration?strategy_id=alpha")
    assert response.status_code == 200
    assert "Parameters for Alpha" in response.text
    assert 'name="param__lookback"' in response.text
    assert 'name="param__threshold"' in response.text
    assert 'name="param__enabled"' in response.text
    assert 'name="param__label"' in response.text
    assert 'name="param__mode"' in response.text


def _base_form(**overrides: str) -> dict[str, str]:
    form = {
        "strategy_id": "alpha",
        "profile_hash": "a" * 64,
        "start_month": "2024-01",
        "end_month": "2024-02",
        "base_currency": "GBP",
        "starting_capital": "10000",
        "idempotency_key": "idem-1",
        "param__lookback": "20",
        "param__threshold": "1.5",
        "param__enabled": "true",
        "param__label": "hello",
        "param__mode": "1",  # index token -> "b"
    }
    form.update(overrides)
    return form


def test_every_scalar_type_encodes_to_its_exact_json_type(launch):
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(),
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(launch.launch_calls) == 1
    command = launch.launch_calls[0]
    assert command.parameters["lookback"] == 20
    assert isinstance(command.parameters["lookback"], int)
    assert command.parameters["threshold"] == 1.5
    assert isinstance(command.parameters["threshold"], float)
    assert command.parameters["enabled"] is True
    assert command.parameters["label"] == "hello"
    assert command.parameters["mode"] == "b"  # index 1 -> enum_values[1]
    assert command.starting_capital == Decimal("10000")
    assert command.idempotency_key == "idem-1"


@pytest.mark.parametrize("raw", ["true", "1.0", '"1"'])
def test_enum_index_token_rejects_lookalike_raw_values(launch, raw):
    """Story 2.7 AC6: the enum field decodes its raw form string only as
    an opaque option-index token -- "true"/"1.0"/'"1"' must never collide
    with (or be silently reinterpreted as) a valid index."""
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(**{"param__mode": raw}),
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert launch.launch_calls == []
    assert "Choose one of the listed options." in response.text


def test_unknown_form_field_is_rejected(launch):
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(**{"param__does_not_exist": "1"}),
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert launch.launch_calls == []


def test_duplicate_form_field_is_rejected(launch):
    duplicate_fields = cast(
        dict[str, str],
        [
            ("strategy_id", "alpha"),
            ("strategy_id", "alpha"),
            ("start_month", "2024-01"),
            ("end_month", "2024-02"),
            ("base_currency", "GBP"),
            ("starting_capital", "10000"),
        ],
    )
    response = client.post(
        "/strategy-manager/configuration",
        data=duplicate_fields,
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert launch.launch_calls == []


def test_validation_failure_returns_422_preserves_values_and_selection(launch):
    launch.launch_error = BacktestLaunchValidationError(
        (LaunchFieldError("starting_capital", "Enter a positive amount."),)
    )
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(starting_capital="10000"),
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert 'id="configuration-errors"' in response.text
    assert "Enter a positive amount." in response.text
    # Submitted values and Strategy selection survive the failed round trip.
    assert "checked" in response.text.split('id="strategy_id__alpha"')[1][:120]
    assert 'value="hello"' in response.text
    assert len(launch.launch_calls) == 1  # attempted, but no job resulted


def test_valid_submission_enqueues_once_and_redirects_to_activity(launch):
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(),
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-1"
    assert len(launch.launch_calls) == 1


def test_configuration_post_requires_auth_guard(launch):
    response = client.post("/strategy-manager/configuration", data=_base_form())
    assert response.status_code == 403
    assert launch.launch_calls == []


# ---------------------------------------------------------------------------
# Story 2.8: Backtest results list route
# ---------------------------------------------------------------------------


def test_backtests_list_renders_rows(services):
    repo, _ = services
    repo.backtest_activities = (
        BacktestActivitySummaryV1(
            job=cast(
                StrategyJobV1,
                SimpleNamespace(
                    id="job-9",
                    enqueue_seq=3,
                    status=StrategyJobStatus.COMPLETE,
                    cancel_requested_at=None,
                ),
            ),
            strategy_id="momentum_v1",
            strategy_api_version=1,
            parameter_summary="lookback=20",
            start_month="2024-01",
            end_month="2024-02",
            metrics=cast(
                BacktestMetricsV1,
                SimpleNamespace(total_return=0.125, win_rate=0.5),
            ),
            metric_availability=cast(MetricAvailabilityV1, SimpleNamespace()),
        ),
    )
    response = client.get("/strategy-manager/backtests")
    assert response.status_code == 200
    assert "momentum_v1 attempt 3" in response.text
    assert "lookback=20" in response.text
    assert "12.50%" in response.text
    assert "50.0%" in response.text


def test_backtests_list_empty_state(services):
    response = client.get("/strategy-manager/backtests")
    assert response.status_code == 200
    assert "No backtests yet." in response.text
    assert "Configure a Backtest" in response.text


def test_backtests_list_integrity_error_alert(services):
    repo, _ = services
    repo.backtest_activities_error = BacktestIntegrityError("corrupt backtest row")
    response = client.get("/strategy-manager/backtests")
    assert response.status_code == 200
    assert "corrupt backtest row" in response.text
    assert "Reload" in response.text
