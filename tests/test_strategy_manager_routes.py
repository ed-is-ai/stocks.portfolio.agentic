"""Route and rendered-accessibility tests for Strategy Manager Story 1.9."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import html
from pathlib import Path
import re
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import (
    get_backtest_launch_service,
    get_backtest_repository,
    get_bootstrap_service,
    get_readiness_service,
    get_strategy_job_service,
)
from app.repositories.backtest_repo import (
    BacktestActivitySummaryV1,
    BacktestIntegrityError,
    BacktestResultV1,
    ComparisonCandidateV1,
    ComparisonEligibilityV1,
    ComparisonIneligibleReason,
)
from app.services.backtest.backtest_engine import (
    DividendAppliedEventV1,
    EntryFillEventV1,
    EquityCurvePointV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
)
from app.services.backtest.backtest_launch_service import (
    BacktestConfigurationViewV1,
    BacktestLaunchValidationError,
    LaunchFieldError,
)
from app.services.backtest.metrics import (
    BacktestMetricsV1,
    MetricAvailabilityV1,
    MetricUnavailableReason,
)
from app.services.backtest.result_presenter import comparison_equity_payload
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyUniverseContractV1,
)
from app.services.backtest.snapshot_profile import (
    CoverageIntervalV1,
    CoverageSummaryV1,
    ProvenanceCoverageV1,
    SnapshotProfileV1,
)
from app.services.backtest.strategy_job import (
    StrategyJobConflict,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
)
from app.api.routes.strategy_manager import _bootstrap_stage_progress
from app.services.backtest.strategy_bootstrap_service import (
    StrategyBootstrapService,
)
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
    StrategyParameterV1,
)
from app.services.backtest.strategy_readiness_service import (
    StrategyReadinessService,
)

client = TestClient(app)


@dataclass
class FakeProfile:
    profile_hash: str = "a" * 64
    calendar_dataset_version: str = "exchange-calendars-v1"
    roster_digest: str = "r" * 64


class FakeActiveProfile:
    profile_hash: str = "a" * 64
    activation_seq: int = 1
    activated_at: datetime = datetime(2026, 8, 21, tzinfo=timezone.utc)


class FakeRepo:
    def __init__(
        self,
        *,
        qualified: bool = True,
        coverage_error: str | None = None,
        has_active: bool = True,
        snapshot_count: int = 3,
        stale_profile: bool = False,
        readiness_error: Exception | None = None,
    ):
        self.qualified = qualified
        self.coverage_error = coverage_error
        # gh-396: knobs so landing-CTA/readiness tests can drive each
        # pipeline stage (no active profile -> setup; zero snapshots ->
        # initialize; all ready -> configure; stale provider map -> setup;
        # readiness read raising -> degraded-but-rendered landing).
        self.has_active = has_active
        self.snapshot_count = snapshot_count
        self.stale_profile = stale_profile
        self.readiness_error = readiness_error
        self.profile = FakeProfile()
        self.activity = None
        self.backtest_activities: tuple[object, ...] = ()
        self.backtest_activities_error: BacktestIntegrityError | None = None
        # Story 2.9: Result page + note CAS fakes.
        self.result: BacktestResultV1 | None = None
        self.result_error: Exception | None = None
        self.result_coverage: CoverageSummaryV1 | None = None
        # Story 3.3: a second, independently-settable Result/error so
        # Comparison tests can express "side A loads fine, side B is
        # corrupt/vanished" (and vice versa) -- `self.result`/
        # `self.result_error` above always own side A's outcome; this
        # pair owns side B's.
        self.result_b: BacktestResultV1 | None = None
        self.result_b_error: Exception | None = None
        self.note_conflict = False
        self.note_value_error: str | None = None
        self.last_note_call: tuple[str, int, str] | None = None
        # Story 3.2: Compare picker fakes.
        self.candidates: tuple[ComparisonCandidateV1, ...] = ()
        self.candidates_error: Exception | None = None
        self.eligibility: ComparisonEligibilityV1 | None = None
        self.eligibility_error: Exception | None = None
        self.last_is_comparable_call: tuple[str, str] | None = None
        self.strategy_job_error: Exception | None = None
        self.bootstrap = SimpleNamespace(job_id="job-1")
        self.bootstrap_run_calls = 0
        self.active_profile_calls = 0
        # Story gh-367: pinned roster identities for Trade Log label
        # resolution. Includes the event fixtures' ``SEC1`` so rendered
        # Security cells show a readable ticker/exchange label.
        self.roster_identities: list[tuple[str, str, str, str]] = [
            ("sid_001", "AAPL", "XNYS", "USD"),
            ("SEC1", "MSFT", "XNAS", "USD"),
        ]

    def roster_member_identities(self, profile_hash):
        return self.roster_identities

    def roster_manifest_json(self, roster_digest):
        # gh-396: readiness `roster` prerequisite is READY when the active
        # profile's roster manifest is present.
        return "{}"

    def stored_snapshot_profile(self, profile_hash):
        # gh-396: bootstrap `_active_profile_needs_refresh` reads this. A
        # stored profile whose hash cannot match the freshly recomputed
        # one models a stale provider map -> setup is required again.
        if self.stale_profile:
            return SimpleNamespace(
                roster_digest="a" * 64, profile_hash="stale-provider-map"
            )
        return None

    def recent_job_failures(self, limit: int = 5):
        if self.readiness_error is not None:
            raise self.readiness_error
        return ()

    def identity_rows(self):
        return [("sid_001", "XNYS", "TEST", "g" * 64)]

    def read_worker_lease(self):
        return None

    def snapshot_coverage(self, profile_hash=None):
        if self.coverage_error:
            raise BacktestIntegrityError(self.coverage_error)
        if profile_hash is not None and self.result_coverage is not None:
            return self.result_coverage
        return SimpleNamespace(
            display_version="Scanner v1",
            earliest_month="2024-01",
            latest_month="2024-03",
            snapshot_count=self.snapshot_count,
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

    def backtest_result(self, run_id):
        from app.services.backtest.strategy_job import StrategyJobNotFound

        if (
            self.result_b is not None
            and self.result is not None
            and self.result_b.run_id == self.result.run_id
        ):
            raise AssertionError(
                "FakeRepo misconfigured: result and result_b share a run_id "
                "-- give each side a distinct run_id"
            )
        if self.result_b is not None and run_id == self.result_b.run_id:
            if self.result_b_error is not None:
                raise self.result_b_error
            return self.result_b
        if self.result_error is not None:
            raise self.result_error
        if self.result is None or self.result.run_id != run_id:
            raise StrategyJobNotFound(f"missing result: {run_id}")
        return self.result

    def update_backtest_result_note(self, run_id, *, expected_note_version, note):
        from app.services.backtest.strategy_job import StrategyJobNotFound

        self.last_note_call = (run_id, expected_note_version, note)
        if self.result is None or self.result.run_id != run_id:
            raise StrategyJobNotFound(f"missing result: {run_id}")
        stripped = note.strip()
        if stripped:
            escaped = html.escape(stripped, quote=True)
            if len(escaped) > 10_000:
                raise ValueError("note text exceeds 10,000 Unicode code points")
        else:
            escaped = None
        if self.note_value_error:
            raise ValueError(self.note_value_error)
        if self.note_conflict:
            raise StrategyJobConflict("note update version is stale")
        if expected_note_version != self.result.note_version:
            raise StrategyJobConflict("note update version is stale")
        self.result = replace(
            self.result, note=escaped, note_version=self.result.note_version + 1
        )
        return self.result

    def active_snapshot_profile(self):
        self.active_profile_calls += 1
        return FakeActiveProfile() if self.has_active else None

    def snapshot_profile(self, _hash):
        return self.profile

    def current_qualification_contract_digest(self):
        return "b" * 64 if self.qualified else None

    def list_strategy_jobs(self):
        return () if self.activity is None else (self.activity,)

    def interval_readiness(self, *_args):
        return SimpleNamespace(no_op=False)

    def strategy_job(self, job_id):
        if self.strategy_job_error is not None:
            raise self.strategy_job_error
        if self.activity is None or job_id != self.activity.id:
            from app.services.backtest.strategy_job import StrategyJobNotFound

            raise StrategyJobNotFound("missing")
        return self.activity

    def initialization_run(self, _job_id):
        return SimpleNamespace(requested_start="2024-01", requested_end="2024-03")

    def bootstrap_run(self, _job_id):
        self.bootstrap_run_calls += 1
        return self.bootstrap

    def strategy_run(self, _job_id):
        return SimpleNamespace(
            strategy_id="momentum_v1", start_month="2024-01", end_month="2024-03"
        )

    def list_backtest_activities(self):
        if self.backtest_activities_error is not None:
            raise self.backtest_activities_error
        return self.backtest_activities

    def comparison_candidates(self, run_id):
        if self.candidates_error is not None:
            raise self.candidates_error
        return self.candidates

    def is_comparable(self, left, right):
        self.last_is_comparable_call = (left, right)
        if self.eligibility_error is not None:
            raise self.eligibility_error
        if self.eligibility is not None:
            return self.eligibility
        return ComparisonEligibilityV1(
            eligible=False,
            reason=ComparisonIneligibleReason.NOT_FOUND,
            detail=f"{right!r} is not eligible for comparison (not_found)",
        )


class FakeJobs:
    def __init__(self):
        self.submissions = []
        self.actions = ("cancel",)

    def enqueue_initialization(self, submission):
        self.submissions.append(submission)
        return SimpleNamespace(no_op=False, job=SimpleNamespace(id="job-1"))

    def legal_actions(self, job_id):
        return SimpleNamespace(job_id=job_id, legal_actions=self.actions)

    def request_cancellation(self, request):
        self.cancel_request = request

    def delete_job(self, request):
        self.delete_request = request

    def restart_backtest(self, request):
        self.restart_request = request
        return SimpleNamespace(job=SimpleNamespace(id="job-2"))


@pytest.fixture
def services(monkeypatch):
    repo, jobs = FakeRepo(), FakeJobs()
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_strategy_job_service] = lambda: jobs
    # gh-396: the landing now also reads readiness + bootstrap; the real
    # providers are @lru_cache'd against the real repo, so override both
    # with fakes built on this test's FakeRepo.
    app.dependency_overrides[get_readiness_service] = lambda: StrategyReadinessService(
        repo
    )
    app.dependency_overrides[get_bootstrap_service] = lambda: StrategyBootstrapService(
        repo, jobs
    )
    # gh-396: keep the `discovery` prerequisite deterministic -- the real
    # `discover_strategies(SKILLS_DIR)` walks the repo filesystem.
    monkeypatch.setattr(
        "app.services.backtest.strategy_readiness_service.discover_strategies",
        lambda _root: SimpleNamespace(strategies=(object(),), warnings=()),
    )
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


def test_main_logs_render_timing(services, caplog):
    repo, _ = services

    with caplog.at_level("INFO"):
        response = client.get("/partials/strategy-manager")

    assert response.status_code == 200
    # gh-396: landing now composes readiness + bootstrap state; each reads
    # the active profile. 6 calls = 1 (_strategy_manager_context) + 3
    # (readiness roster/active_profile/coverage) + 2 (bootstrap
    # is_setup_required + _active_profile_needs_refresh).
    assert repo.active_profile_calls == 6
    assert "Strategy Manager tab rendered in" in caplog.text


# ---------------------------------------------------------------------------
# gh-396: adaptive guided landing (one primary CTA + progress line)
# ---------------------------------------------------------------------------


def test_landing_cta_is_setup_when_no_active_profile(services):
    repo, _ = services
    repo.has_active = False
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Set up Strategy Manager" in response.text
    assert 'hx-get="/strategy-manager/setup"' in response.text
    # qualification + discovery are READY without an active profile.
    assert "2 of 5 prerequisites ready." in response.text


def test_landing_cta_is_prepare_data_when_no_coverage(services):
    repo, _ = services
    repo.snapshot_count = 0
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Prepare historical data" in response.text
    assert 'hx-get="/strategy-manager/initialization"' in response.text
    assert "4 of 5 prerequisites ready." in response.text


def test_landing_cta_is_configure_when_all_ready(services):
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Configure a Backtest" in response.text
    assert 'hx-get="/strategy-manager/configuration"' in response.text
    assert "5 of 5 prerequisites ready." in response.text


def test_landing_cta_is_review_readiness_at_four_of_five(services):
    # gh-396 patch A: coverage is ready but another prerequisite is not,
    # so the CTA must not send the user to Configure (the removed
    # dead-end) -- it routes to Readiness instead.
    repo, _ = services
    repo.qualified = False
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "4 of 5 prerequisites ready." in response.text
    assert "Review readiness</button>" in response.text
    # The primary CTA is not the Configure button (the removed dead-end);
    # "Configure a Backtest" still appears as an <a> in the results list.
    assert "Configure a Backtest</button>" not in response.text


def test_landing_cta_is_setup_when_active_profile_is_stale(services):
    # gh-396 patch H: a stale provider map means setup is required again
    # even with an active profile present.
    repo, _ = services
    repo.stale_profile = True
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Set up Strategy Manager" in response.text
    assert 'hx-get="/strategy-manager/setup"' in response.text


def test_landing_renders_when_readiness_state_unavailable(services):
    # gh-396 patch B: an integrity error in readiness/bootstrap must not
    # 500 the landing -- it degrades to no progress + the setup gate.
    repo, _ = services
    repo.readiness_error = BacktestIntegrityError("worker ledger corrupt")
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "0 of 5 prerequisites ready." in response.text
    assert "Prepare historical data" in response.text


def test_landing_has_no_top_level_diagnostics_control(services):
    response = client.get("/strategy-manager")
    assert response.status_code == 200
    assert "Diagnostics" not in response.text
    assert "/strategy-manager/diagnostics" not in response.text


def test_landing_setup_already_notice_banner(services):
    response = client.get("/strategy-manager?setup=already")
    assert response.status_code == 200
    assert "Strategy Manager is already set up." in response.text
    assert "Verified at 2026-08-21 00:00 UTC." in response.text


def test_landing_setup_already_notice_suppressed_when_setup_required(services):
    # gh-396 patch C: reconcile the notice with live state -- never show
    # the "already set up" banner above the "setup required" warning.
    repo, _ = services
    repo.has_active = False
    response = client.get("/strategy-manager?setup=already")
    assert response.status_code == 200
    assert "already set up" not in response.text.lower()
    assert "Set up Strategy Manager" in response.text


def test_landing_ignores_unknown_setup_query(services):
    response = client.get("/strategy-manager?setup=bogus")
    assert response.status_code == 200
    assert "already set up" not in response.text


def test_setup_get_redirects_to_landing_banner_when_already_set_up(services):
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager?setup=already"
    followed = client.get("/strategy-manager/setup")
    assert "Strategy Manager is already set up." in followed.text


def test_setup_get_renders_form_when_setup_required(services):
    repo, _ = services
    repo.has_active = False
    response = client.get("/strategy-manager/setup")
    assert response.status_code == 200
    assert 'action="/strategy-manager/setup"' in response.text
    assert 'name="idempotency_key"' in response.text


def test_diagnostics_route_redirects_and_never_404s(services):
    response = client.get("/strategy-manager/diagnostics", follow_redirects=False)
    assert response.status_code == 303
    assert (
        response.headers["location"] == "/strategy-manager/readiness?section=advanced"
    )


def test_diagnostics_deep_link_opens_advanced_disclosure(services):
    # gh-396 patch D: following the diagnostics redirect lands on Readiness
    # with the Advanced disclosure already expanded.
    followed = client.get("/strategy-manager/diagnostics")
    assert followed.status_code == 200
    assert '<details id="advanced" class="mt-4" open>' in followed.text


def test_readiness_page_exposes_advanced_disclosure(services):
    response = client.get("/strategy-manager/readiness")
    assert response.status_code == 200
    # Collapsed by default (no `section=advanced`).
    assert '<details id="advanced" class="mt-4">' in response.text
    assert "Advanced / troubleshooting" in response.text
    assert "No recent failures." in response.text


def test_readiness_and_landing_use_plain_language(services):
    # gh-398: enum values render through plain-language label maps and each
    # readiness prerequisite name carries a one-line `title` tooltip.
    readiness = client.get("/strategy-manager/readiness")
    assert readiness.status_code == 200
    # No raw snake_case enum token leaks as visible copy...
    assert "stale_incompatible" not in readiness.text
    assert "integrity_error" not in readiness.text
    # ...and the plain-language labels are what render instead.
    assert "Historical data check" in readiness.text
    # Every prerequisite name carries a one-line explanation tooltip.
    assert (
        'title="Confirms the market-data providers are certified '
        'for backtesting."' in readiness.text
    )
    assert 'aria-label="Historical data check.' in readiness.text

    landing = client.get("/strategy-manager")
    assert landing.status_code == 200
    assert "Backtestable periods" in landing.text
    assert "Data version" in landing.text
    assert "Scanner-data version" not in landing.text


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


def test_invalid_initialization_htmx_submission_swaps_linked_errors(services):
    response = client.post(
        "/strategy-manager/initialization",
        data={"start_month": "2026-02", "end_month": "28"},
        headers={"X-Auth-Token": "s3cret", "HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'id="initialization-errors"' in response.text
    assert "Use a fully closed month in YYYY-MM format." in response.text
    assert 'value="28"' in response.text


def test_strategy_manager_js_swaps_expected_form_error_responses() -> None:
    root = Path(__file__).resolve().parents[1]
    javascript = (
        root / "app" / "api" / "static" / "js" / "strategy-manager.js"
    ).read_text(encoding="utf-8")

    assert "status === 409 || status === 422" in javascript
    assert "target.id === 'tab-content'" in javascript
    assert "detail.shouldSwap = true" in javascript
    assert "detail.isError = false" in javascript


def test_strategy_manager_script_url_is_cache_versioned(services) -> None:
    response = client.get("/")

    assert 'src="/static/js/strategy-manager.js?v=20260824-1"' in response.text


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


@pytest.mark.parametrize(
    ("status", "current_stage", "failure_detail", "actions", "polls"),
    [
        (StrategyJobStatus.QUEUED, None, None, ("cancel",), True),
        (StrategyJobStatus.RUNNING, "roster_capture", None, ("cancel",), True),
        (StrategyJobStatus.COMPLETE, None, None, (), False),
        (StrategyJobStatus.CANCELLED, None, None, (), False),
    ],
)
def test_bootstrap_activity_renders_status_stage_and_terminal_polling(
    services, status, current_stage, failure_detail, actions, polls
):
    repo, jobs = services
    jobs.actions = actions
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=status,
        status_version=7,
        current_month=None,
        current_stage=current_stage,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=failure_detail,
    )
    response = client.get("/strategy-manager/activities/job-1")
    assert response.status_code == 200
    assert "Strategy Manager setup activity" in response.text
    assert repo.bootstrap_run_calls == 1
    if current_stage:
        assert "Roster Capture" in response.text
    assert ("hx-trigger=" in response.text) is polls
    polling = "/status?last_seen_version" in response.text
    assert polling is polls
    if not polls:
        assert "hx-trigger=" not in response.text


def test_failed_bootstrap_activity_shows_failure_and_legal_action(services):
    repo, jobs = services
    jobs.actions = ("delete",)
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=StrategyJobStatus.FAILED,
        status_version=8,
        current_month=None,
        current_stage=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail="Historical provider certification failed",
    )
    response = client.get("/strategy-manager/activities/job-1")
    assert response.status_code == 200
    assert "Historical provider certification failed" in response.text
    assert "Try setup again" in response.text
    assert 'action="/strategy-manager/setup"' in response.text
    assert 'name="idempotency_key"' in response.text
    assert "Delete setup attempt" in response.text
    assert 'data-bs-target="#delete-confirmation-job-1"' in response.text
    assert "hx-trigger=" not in response.text
    assert "/status?last_seen_version" not in response.text


def test_bootstrap_activity_cancel_uses_the_guarded_lifecycle_command(services):
    repo, jobs = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=StrategyJobStatus.RUNNING,
        status_version=7,
        current_month=None,
        current_stage="qualification",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.post(
        "/strategy-manager/activities/job-1/cancel",
        data={"expected_version": "7"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 200
    assert jobs.cancel_request.expected_version == 7
    assert "Cancel setup?" in response.text


def test_bootstrap_activity_delete_returns_setup_recovery(services):
    repo, jobs = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=StrategyJobStatus.FAILED,
        status_version=8,
        current_month=None,
        current_stage=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail="Bootstrap failed",
    )
    response = client.post(
        "/strategy-manager/activities/job-1/delete",
        data={"expected_version": "8"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 200
    assert jobs.delete_request.expected_version == 8
    assert "Setup history" in response.text
    assert 'href="/strategy-manager/setup"' in response.text


def test_bootstrap_activity_poll_returns_empty_for_same_or_newer_version(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=StrategyJobStatus.RUNNING,
        status_version=7,
        current_month=None,
        current_stage="qualification",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    for version in (7, 8):
        response = client.get(
            f"/strategy-manager/activities/job-1/status?last_seen_version={version}"
        )
        assert response.status_code == 204
        assert response.text == ""


def test_bootstrap_activity_poll_renders_newer_version(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=StrategyJobStatus.RUNNING,
        status_version=8,
        current_month=None,
        current_stage="profile_activation",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get(
        "/strategy-manager/activities/job-1/status?last_seen_version=7"
    )
    assert response.status_code == 200
    assert "Profile Activation" in response.text
    assert 'data-status-version="8"' in response.text


def _bootstrap_job(
    *,
    status,
    current_stage=None,
    status_version=7,
    enqueue_seq=1,
    deleted_at=None,
):
    return SimpleNamespace(
        id="job-1",
        job_type=StrategyJobType.BOOTSTRAP,
        status=status,
        status_version=status_version,
        enqueue_seq=enqueue_seq,
        current_month=None,
        current_stage=current_stage,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail="Roster capture failed"
        if status is StrategyJobStatus.FAILED
        else None,
        deleted_at=deleted_at,
        created_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "status",
    [StrategyJobStatus.QUEUED, StrategyJobStatus.RUNNING],
)
def test_setup_get_redirects_to_activity_for_live_bootstrap_job(services, status):
    repo, _ = services
    repo.activity = _bootstrap_job(status=status)
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-1"


@pytest.mark.parametrize(
    "status",
    [None, StrategyJobStatus.CANCELLED, StrategyJobStatus.FAILED],
)
def test_setup_get_renders_form_when_no_live_bootstrap_job(services, status):
    repo, _ = services
    repo.has_active = False
    repo.activity = None if status is None else _bootstrap_job(status=status)
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/strategy-manager/setup"' in response.text


def test_setup_get_completed_job_but_setup_required_again_renders_form(services):
    repo, _ = services
    repo.has_active = False
    repo.activity = _bootstrap_job(status=StrategyJobStatus.COMPLETE)
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/strategy-manager/setup"' in response.text


def test_setup_get_completed_job_when_set_up_redirects_to_already_banner(services):
    repo, _ = services
    repo.activity = _bootstrap_job(status=StrategyJobStatus.COMPLETE)
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager?setup=already"


def test_setup_get_ignores_soft_deleted_newer_bootstrap_job(services):
    repo, _ = services
    repo.has_active = False

    live = _bootstrap_job(status=StrategyJobStatus.RUNNING, enqueue_seq=5)
    live.id = "job-live"
    deleted = _bootstrap_job(status=StrategyJobStatus.RUNNING, enqueue_seq=9)
    deleted.id = "job-deleted"
    deleted.deleted_at = datetime(2026, 8, 29, tzinfo=timezone.utc)
    repo.list_strategy_jobs = lambda: (live, deleted)

    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-live"


def test_setup_get_uses_newest_bootstrap_job_for_redirect(services):
    repo, _ = services

    older = _bootstrap_job(status=StrategyJobStatus.CANCELLED, enqueue_seq=1)
    older.id = "job-old"
    newer = _bootstrap_job(status=StrategyJobStatus.RUNNING, enqueue_seq=7)
    newer.id = "job-new"
    repo.list_strategy_jobs = lambda: (older, newer)

    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/job-new"


def test_setup_get_survives_integrity_error_from_job_scan(services):
    repo, _ = services
    repo.has_active = False

    def _raise():
        raise BacktestIntegrityError("ledger unavailable")

    repo.list_strategy_jobs = _raise
    response = client.get("/strategy-manager/setup", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/strategy-manager/setup"' in response.text


def test_bootstrap_activity_shows_ordered_stage_progress_mid_run(services):
    repo, jobs = services
    jobs.actions = ("cancel",)
    repo.activity = _bootstrap_job(
        status=StrategyJobStatus.RUNNING, current_stage="roster_capture"
    )
    text = client.get("/strategy-manager/activities/job-1").text
    assert "Verifying historical data" in text
    assert "Capturing securities" in text
    assert "Activating setup" in text
    assert "sm-stage-complete" in text
    assert "sm-stage-current" in text
    assert "sm-stage-pending" in text
    assert 'aria-current="step"' in text


def test_bootstrap_activity_marks_failed_stage_and_recovery_link(services):
    repo, jobs = services
    jobs.actions = ("delete",)
    repo.activity = _bootstrap_job(
        status=StrategyJobStatus.FAILED,
        current_stage="roster_capture",
        status_version=8,
    )
    text = client.get("/strategy-manager/activities/job-1").text
    assert "sm-stage-failed" in text
    assert "Roster capture failed" in text
    assert 'href="/strategy-manager/setup"' in text
    assert "Try setup again" in text


def test_bootstrap_activity_cancelled_shows_stopped_stage_and_recovery_link(services):
    repo, jobs = services
    jobs.actions = ("delete",)
    repo.activity = _bootstrap_job(
        status=StrategyJobStatus.CANCELLED,
        current_stage="roster_capture",
        status_version=8,
    )
    text = client.get("/strategy-manager/activities/job-1").text
    assert "sm-stage-stopped" in text
    assert "Stopped" in text
    assert "sm-stage-failed" not in text
    assert 'href="/strategy-manager/setup"' in text


def test_bootstrap_activity_complete_shows_confirmation_and_stops_polling(services):
    repo, jobs = services
    jobs.actions = ()
    repo.activity = _bootstrap_job(status=StrategyJobStatus.COMPLETE, status_version=9)
    text = client.get("/strategy-manager/activities/job-1").text
    assert "Strategy Manager is set up." in text
    assert 'href="/strategy-manager"' in text
    assert "hx-trigger=" not in text
    assert text.count("sm-stage-complete") == 3


@pytest.mark.parametrize(
    ("status", "current_stage", "expected"),
    [
        (StrategyJobStatus.QUEUED, None, ["pending", "pending", "pending"]),
        (StrategyJobStatus.RUNNING, None, ["current", "pending", "pending"]),
        (
            StrategyJobStatus.RUNNING,
            "roster_capture",
            ["complete", "current", "pending"],
        ),
        (
            StrategyJobStatus.RUNNING,
            "profile_activation",
            ["complete", "complete", "current"],
        ),
        (
            StrategyJobStatus.FAILED,
            "roster_capture",
            ["complete", "failed", "pending"],
        ),
        (StrategyJobStatus.FAILED, None, ["failed", "pending", "pending"]),
        (
            StrategyJobStatus.CANCELLED,
            "roster_capture",
            ["complete", "stopped", "pending"],
        ),
        (StrategyJobStatus.CANCELLED, None, ["stopped", "pending", "pending"]),
        (
            StrategyJobStatus.COMPLETE,
            "profile_activation",
            ["complete", "complete", "complete"],
        ),
        (StrategyJobStatus.COMPLETE, None, ["complete", "complete", "complete"]),
    ],
)
def test_bootstrap_stage_progress_matrix(status, current_stage, expected):
    job = _bootstrap_job(status=status, current_stage=current_stage)
    progress = _bootstrap_stage_progress(cast(StrategyJobV1, job))
    assert [stage["key"] for stage in progress] == [
        "qualification",
        "roster_capture",
        "profile_activation",
    ]
    assert [stage["label"] for stage in progress] == [
        "Verifying historical data",
        "Capturing securities",
        "Activating setup",
    ]
    assert [stage["state"] for stage in progress] == expected


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
    assert 'href="/strategy-manager/results/job-1"' in completed.text


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
    runtime_files=("alpha/scripts/strategy.py",),
    universe=StrategyUniverseContractV1(
        schema_version="strategy_universe.v1",
        mode="selected-securities",
        parameter="selected_securities",
    ),
)

STRATEGY_BUY_AND_HOLD = StrategyDescriptorV1(
    strategy_id="rtly-backtest-buy-and-hold",
    source_manifest_version="strategy_source_manifest.v1",
    source_digest="h" * 64,
    display_name="Buy and Hold Backtest",
    description="A one-time ranked passive basket.",
    api_version=1,
    parameters=(
        _param(
            name="top_x",
            type="integer",
            default=10,
            minimum=1,
            description="Number of strongest eligible securities to buy once at the first Run session.",
        ),
    ),
    default_parameters={"top_x": 10},
    runtime_path="rtly-backtest-buy-and-hold/scripts/strategy.py",
    runtime_files=("rtly-backtest-buy-and-hold/scripts/strategy.py",),
    universe=StrategyUniverseContractV1(
        schema_version="strategy_universe.v1",
        mode="selected-securities",
        parameter="selected_securities",
    ),
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
    repo = FakeRepo()
    app.dependency_overrides[get_backtest_launch_service] = lambda: fake
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_backtest_launch_service, None)
        app.dependency_overrides.pop(get_backtest_repository, None)


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


def test_buy_and_hold_top_x_uses_shared_metadata_driven_integer_field(launch):
    launch.strategies = (STRATEGY_ALPHA, STRATEGY_BUY_AND_HOLD)
    response = client.get(
        "/strategy-manager/configuration?strategy_id=rtly-backtest-buy-and-hold"
    )
    assert response.status_code == 200
    text = response.text
    assert 'id="param__top_x"' in text
    assert 'name="param__top_x"' in text
    assert 'value="10"' in text
    assert 'min="1"' in text
    assert 'step="1"' in text
    assert "Number of strongest eligible securities" in text


def test_buy_and_hold_top_x_validation_error_stays_on_its_field(launch):
    """``top_x`` uses the shared decoder, including retry-value retention."""
    launch.strategies = (STRATEGY_BUY_AND_HOLD,)
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "rtly-backtest-buy-and-hold",
            "profile_hash": "a" * 64,
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "buy-and-hold-invalid-top-x",
            "security_ids": "sid_001",
            "param__top_x": "1.5",
        },
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    text = response.text
    assert 'id="param__top_x-error"' in text
    assert 'aria-invalid="true"' in text
    assert 'value="1.5"' in text
    assert launch.launch_calls == []


def test_strategy_radio_label_shows_count_not_default_dump(launch):
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    text = response.text
    # No per-parameter default dump in the choosing list.
    assert "Parameters: lookback" not in text
    assert "(default 20)" not in text
    # Just a plain count (alpha declares 5 parameters).
    assert ">5 parameters</div>" in text


def test_configuration_disclosure_splits_required_and_optional(launch):
    response = client.get("/strategy-manager/configuration?strategy_id=alpha")
    assert response.status_code == 200
    text = response.text
    assert '<details class="sm-param-disclosure"' in text
    assert '<span id="param-summary" aria-live="polite">5 parameters</span>' in text
    # lookback is required -> rendered before (outside) the disclosure;
    # the disclosure closes before the Period step fieldset.
    before_details, _, rest = text.partition('<details class="sm-param-disclosure"')
    disclosed, _, after_details = rest.partition("</details>")
    assert 'name="param__lookback"' in before_details
    assert 'name="param__threshold"' in disclosed
    assert 'name="param__mode"' in disclosed
    assert 'name="param__lookback"' not in disclosed
    assert "Period" in after_details and 'data-wizard-step="2"' in after_details
    # Collapsed by default (no error), and the dead data-param-type attr is gone.
    assert '<details class="sm-param-disclosure">' in text
    assert "data-param-type" not in text
    assert 'data-param-default="20"' in before_details  # lookback default, outside


def test_configuration_422_on_disclosed_param_opens_disclosure(launch):
    launch.launch_error = BacktestLaunchValidationError(
        (LaunchFieldError("param__threshold", "Outside the allowed range."),)
    )
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(),
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    text = response.text
    assert '<details class="sm-param-disclosure" open>' in text
    assert "Outside the allowed range." in text


def _base_form(**overrides: str) -> dict[str, str]:
    form = {
        "strategy_id": "alpha",
        "profile_hash": "a" * 64,
        "activation_seq": "1",
        "start_month": "2024-01",
        "end_month": "2024-02",
        "base_currency": "GBP",
        "starting_capital": "10000",
        "idempotency_key": "idem-1",
        "security_ids": "sid_001",
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
# B1: staged wizard for backtest configuration (gh-399) -- Strategy, then
# Run setup (Universe/Period/Capital), then Review & Run (3 steps).
# ---------------------------------------------------------------------------


def test_configuration_renders_wizard_shell(launch):
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    text = response.text
    assert 'id="wizard-step-indicator"' in text
    assert "Step 1 of 3" in text
    assert 'aria-live="polite"' in text
    assert 'id="wizard-back"' in text
    assert 'id="wizard-next"' in text
    assert 'id="wizard-summary"' in text
    assert 'data-wizard-step="1"' in text
    assert 'data-wizard-step="2"' in text
    assert 'data-wizard-step="3"' in text


def test_configuration_all_steps_visible_without_js(launch):
    """Progressive enhancement: the server never marks a step ``hidden`` --
    step visibility is applied by the wizard script only, so every step and
    the submit render with JavaScript disabled."""
    response = client.get("/strategy-manager/configuration?strategy_id=alpha")
    assert response.status_code == 200
    text = response.text
    # No step container is server-rendered hidden (attribute in any spot).
    for opening in re.findall(r"<[^>]*data-wizard-step=\"\d\"[^>]*>", text):
        assert "hidden" not in opening
    assert text.count('data-wizard-step="1"') >= 2  # Strategy + Parameters
    assert text.count('data-wizard-step="2"') >= 2  # Universe + Period/Capital
    assert 'data-wizard-step="3"' in text
    assert "Run Backtest" in text


def test_configuration_fields_partial_carries_wizard_steps(launch):
    response = client.get(
        "/strategy-manager/configuration/fields",
        params={"strategy_id": "alpha"},
    )
    assert response.status_code == 200
    assert 'data-wizard-step="1"' in response.text  # Parameters fieldset
    assert 'data-wizard-step="2"' in response.text  # Period + Capital fieldsets


def test_configuration_wizard_script_has_error_detection_selectors(launch):
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    text = response.text
    assert ".is-invalid" in text
    assert 'aria-invalid="true"' in text
    assert "sm-alert-danger" in text  # universe errors route by alert, not field
    assert "configuration-errors" in text
    assert "htmx:afterSettle" in text
    # afterSettle is bound once for the lifetime of the shell, not per swap.
    assert "smWizardBound" in text


def test_configuration_universe_error_is_inside_the_run_setup_step(launch):
    """A universe/security validation error must render within the step-2
    container so the wizard opens the step that actually holds the fault."""
    response = client.post(
        "/strategy-manager/configuration",
        data=_base_form(security_ids="not_in_roster"),
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    universe_fieldset = response.text.split('<legend class="h5">Universe</legend>')[1]
    universe_fieldset = universe_fieldset.split("</fieldset>")[0]
    assert "Unknown securities: not_in_roster" in universe_fieldset


def test_configuration_422_fragment_keeps_wizard_markup(launch):
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
    strategy_fieldset = response.text.split('<legend class="h5">Strategy</legend>')[0]
    assert 'data-wizard-step="1"' in strategy_fieldset


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


# ---------------------------------------------------------------------------
# Story 2.9: standalone completed-Backtest Result review + note CAS
# ---------------------------------------------------------------------------

RESULT_RUN_ID = "run-1"
RESULT_PROFILE_HASH = "a" * 64


def _equity_point(day: int, equity: str, seq: int) -> EquityCurvePointV1:
    return EquityCurvePointV1(
        session=date(2024, 1, day),
        cash_base=Decimal(equity),
        positions_value_base=Decimal("0"),
        total_equity_base=Decimal(equity),
        sequence=seq,
    )


def _metrics(**overrides: object) -> BacktestMetricsV1:
    defaults: dict[str, object] = dict(
        total_return=0.10, sharpe_ratio=1.23, win_rate=0.5, max_drawdown=-0.05
    )
    defaults.update(overrides)
    return BacktestMetricsV1(**defaults)  # type: ignore[arg-type]


def _availability(**overrides: object) -> MetricAvailabilityV1:
    defaults: dict[str, object] = dict(
        win_rate_unavailable=None, sharpe_unavailable=None
    )
    defaults.update(overrides)
    return MetricAvailabilityV1(**defaults)  # type: ignore[arg-type]


def _skipped_event(seq: int = 1) -> SkippedSignalEventV1:
    return SkippedSignalEventV1(
        security_id="SEC1",
        side=SignalSide.BUY,
        signal_session=date(2024, 1, 2),
        rule_id="rule-1",
        reason=SkipReasonCode.INSUFFICIENT_CASH,
        detail="not enough cash on hand",
        sequence=seq,
    )


def _entry_event(seq: int = 1) -> EntryFillEventV1:
    return EntryFillEventV1(
        security_id="SEC1",
        signal_session=date(2024, 1, 2),
        fill_session=date(2024, 1, 3),
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("100.00"),
        fill_currency="USD",
        fill_quote_unit="1",
        cost_base=Decimal("1000.00"),
        sequence=seq,
    )


def _exit_event(seq: int = 2, pnl: str = "50.00") -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id="SEC1",
        signal_session=date(2024, 1, 20),
        fill_session=date(2024, 1, 21),
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("105.00"),
        fill_currency="USD",
        fill_quote_unit="1",
        proceeds_base=Decimal("1050.00"),
        cost_basis_base=Decimal("1000.00"),
        realized_pnl_base=Decimal(pnl),
        sequence=seq,
    )


def _split_event(seq: int = 1) -> SplitAppliedEventV1:
    return SplitAppliedEventV1(
        security_id="SEC1",
        session=date(2024, 1, 10),
        ratio=Decimal("2"),
        shares_before=Decimal("10"),
        shares_after=Decimal("20"),
        evidence_revision="f" * 64,
        policy_version="v1",
        sequence=seq,
    )


def _open_mark_event(seq: int = 1) -> OpenPositionMarkEventV1:
    return OpenPositionMarkEventV1(
        security_id="SEC1",
        session=date(2024, 1, 31),
        shares=10,
        mark_price_native=Decimal("110.00"),
        market_value_base=Decimal("1100.00"),
        cost_basis_base=Decimal("1000.00"),
        unrealized_pnl_base=Decimal("100.00"),
        sequence=seq,
    )


def _dividend_event(seq: int = 1) -> DividendAppliedEventV1:
    return DividendAppliedEventV1(
        security_id="SEC1",
        session=date(2024, 1, 15),
        per_share_amount=Decimal("0.50"),
        shares_carried=Decimal("10"),
        cash_credit_native=Decimal("5.00"),
        cash_credit_base=Decimal("5.00"),
        currency="USD",
        quote_unit="1",
        evidence_revision="g" * 64,
        policy_version="v1",
        sequence=seq,
    )


def _result(
    *,
    run_id: str = RESULT_RUN_ID,
    events: tuple = (),
    equity_curve: tuple | None = None,
    metrics: BacktestMetricsV1 | None = None,
    availability: MetricAvailabilityV1 | None = None,
    note: str | None = None,
    note_version: int = 1,
    start_month: str = "2024-01",
    end_month: str = "2024-01",
    profile_hash: str = RESULT_PROFILE_HASH,
    initial_entry_selection: InitialEntrySelectionV1 | None = None,
) -> BacktestResultV1:
    curve = (
        equity_curve
        if equity_curve is not None
        else (_equity_point(1, "10000.00", 1), _equity_point(31, "11000.00", 2))
    )
    return BacktestResultV1(
        run_id=run_id,
        strategy_id="momentum_v1",
        strategy_api_version=1,
        strategy_source_digest="b" * 64,
        parameters={"lookback": 20},
        profile_hash=profile_hash,
        start_month=start_month,
        end_month=end_month,
        ordered_month_digest="c" * 64,
        base_currency="GBP",
        starting_capital=Decimal("10000.00"),
        run_input_manifest_digest="d" * 64,
        execution_contract_digest="e" * 64,
        metrics=metrics or _metrics(),
        metric_availability=availability or _availability(),
        events=events,
        equity_curve=curve,
        final_cash_base=Decimal("1000.00"),
        completed_at=datetime(2024, 2, 1, 12, 0, tzinfo=timezone.utc),
        note=note,
        note_version=note_version,
        initial_entry_selection=initial_entry_selection,
    )


def _complete_backtest_activity(run_id: str = RESULT_RUN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.COMPLETE,
        status_version=1,
        current_month=None,
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )


def test_result_page_renders_sections_in_order(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(events=(_entry_event(1), _exit_event(2)))
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    for label in (
        "Run identity",
        "Metrics",
        "Equity Curve",
        "Trade Log",
        "Provenance",
        "Decision note",
    ):
        assert label in text
    assert text.index("Run identity") < text.index("Metrics")
    assert text.index("Metrics") < text.index("Equity Curve")
    assert text.index("Equity Curve") < text.index("Trade Log")
    assert text.index("Trade Log") < text.index("Provenance")
    assert text.index("Provenance") < text.index("Decision note")
    assert "momentum_v1 v1" in text
    assert "2024-01 to 2024-01" in text
    assert "No live-portfolio" not in text  # sanity: no live-import copy leaks in


def test_result_initial_basket_is_visible_and_explains_persisted_evidence(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    selection_session = date(2024, 1, 1)
    repo.result = _result(
        initial_entry_selection=InitialEntrySelectionV1(
            session=selection_session,
            metric_id="split_adjusted_close_return_252_sessions",
            metric_version="v1",
            rule_id="buy_and_hold_top_x_entry_v1",
            decisions=(
                EntrySelectionDecisionV1(
                    security_id="sid_001",
                    rank=1,
                    state=EntrySelectionState.SELECTED,
                    score=Decimal("0.1236"),
                ),
                EntrySelectionDecisionV1(
                    security_id="missing-id",
                    rank=2,
                    state=EntrySelectionState.EXCLUDED,
                    reason_code="insufficient_history",
                ),
            ),
            signals=(
                Signal(
                    security_id="sid_001",
                    side=SignalSide.BUY,
                    session=selection_session,
                    rule_id="buy_and_hold_top_x_entry_v1",
                ),
            ),
        )
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "Initial basket" in text
    assert "Trailing return (252 sessions)" in text
    assert "split_adjusted_close_return_252_sessions v1" in text
    assert "split_adjusted_close_return_252_sessions vv1" not in text
    assert "Calculated from the 252 prior trading sessions" in text
    assert "12.4%" in text
    assert "AAPL (XNYS)" in text
    assert "Unknown security" in text
    assert "missing-id" in text
    assert "Insufficient price history for the 252-session return." in text
    assert "insufficient_history" not in text


def test_result_metrics_format_signed_and_neutral(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        metrics=_metrics(
            total_return=0.125, sharpe_ratio=-1.5, win_rate=0.625, max_drawdown=-0.083
        )
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "+12.5%" in response.text
    assert "-1.50" in response.text
    assert "62.5%" in response.text
    assert "-8.3%" in response.text


def test_result_metrics_neutral_zero_never_shows_a_sign(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(metrics=_metrics(total_return=0.0, max_drawdown=0.0))
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "+0.0%" not in response.text
    assert "-0.0%" not in response.text
    assert "0.0%" in response.text


def test_result_no_closed_trades_shows_no_applicable_text(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        metrics=_metrics(sharpe_ratio=None, win_rate=None),
        availability=_availability(
            win_rate_unavailable=MetricUnavailableReason.NO_CLOSED_TRADES,
            sharpe_unavailable=MetricUnavailableReason.INSUFFICIENT_DAILY_RETURNS,
        ),
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "Not applicable — no closed trades" in response.text
    assert "Not applicable — insufficient daily return variation" in response.text


def test_result_sharpe_zero_variance_shows_insufficient_variation_text(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        metrics=_metrics(sharpe_ratio=None),
        availability=_availability(
            sharpe_unavailable=MetricUnavailableReason.ZERO_VARIANCE
        ),
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "Not applicable — insufficient daily return variation" in response.text


def test_result_no_events_states_no_simulated_trades(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(events=())
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "No simulated trades." in response.text


def test_result_skipped_only_events_state_and_all_kinds_render(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        events=(
            _skipped_event(1),
            _split_event(2),
            _open_mark_event(3),
            _dividend_event(4),
        )
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "No executed simulated trades" in response.text
    assert "Skipped" in response.text
    assert "Split" in response.text
    assert "Open mark" in response.text
    assert "Dividend" in response.text
    assert "shares credited" in response.text
    assert "not enough cash on hand" in response.text


def test_result_all_event_kinds_render_without_forcing_buy_sell_shape(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        events=(
            _skipped_event(1),
            _entry_event(2),
            _exit_event(3),
            _split_event(4),
            _open_mark_event(5),
            _dividend_event(6),
        )
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "No executed simulated trades" not in text
    for label in ("Skipped", "Buy", "Sell", "Split", "Open mark", "Dividend"):
        assert label in text
    # gh-367: the pinned roster resolves SEC1 to a readable label; the
    # bare GUID survives only inside the row's audit-detail disclosure.
    assert "MSFT (XNAS)" in text
    assert "Security ID: SEC1" in text
    assert "<td>SEC1</td>" not in text


def test_result_trade_log_unresolved_security_falls_back(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.roster_identities = []
    repo.result = _result(events=(_entry_event(1), _exit_event(2)))
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "Unknown security" in text
    assert "Security ID: SEC1" in text


def test_result_chart_and_table_share_the_same_ordered_payload(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(
        equity_curve=(_equity_point(1, "10000.00", 1), _equity_point(2, "10250.50", 2))
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    # The tojson payload feeding the chart and the table's rendered cells
    # both derive from the exact same presenter output.
    assert '"date": "2024-01-01"' in text
    assert '"equity": 10000.0' in text
    assert '"equity": 10250.5' in text
    assert "10,000.00" in text
    assert "10,250.50" in text


def test_result_canvas_has_role_img_accessible_name_and_destroy_guard(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert 'role="img"' in text
    assert "aria-label=" in text
    assert "Chart.getChart(canvas)" in text
    assert "existing.destroy()" in text
    assert "View equity data table" in text


def test_result_provenance_shows_reconstructed_and_observed_separately(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(start_month="2024-01", end_month="2024-03")
    repo.result_coverage = CoverageSummaryV1(
        profile_hash=RESULT_PROFILE_HASH,
        display_version="Scanner v1",
        earliest_month="2024-01",
        latest_month="2024-06",
        snapshot_count=6,
        intervals=(CoverageIntervalV1(start_month="2024-01", end_month="2024-06"),),
        provenance=(
            ProvenanceCoverageV1(
                provenance_quality="best_effort_reconstructed",
                snapshot_count=2,
                intervals=(
                    CoverageIntervalV1(start_month="2024-01", end_month="2024-02"),
                ),
            ),
            ProvenanceCoverageV1(
                provenance_quality="observed_bau",
                snapshot_count=4,
                intervals=(
                    CoverageIntervalV1(start_month="2024-03", end_month="2024-06"),
                ),
            ),
        ),
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "Best Effort Reconstructed" in text
    assert "Observed Bau" in text
    assert "Best-effort yfinance." in text
    # Clipped to the Result's own [2024-01, 2024-03] window, never the
    # whole profile's [2024-01, 2024-06] coverage.
    assert "2024-04" not in text
    assert "2024-05" not in text
    assert "2024-06" not in text


def test_result_redirects_non_complete_job_to_activity_shell(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id=RESULT_RUN_ID,
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.RUNNING,
        status_version=1,
        current_month="2024-01",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get(
        f"/strategy-manager/results/{RESULT_RUN_ID}", follow_redirects=False
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == f"/strategy-manager/activities/{RESULT_RUN_ID}"
    )


def test_result_unknown_run_id_is_404(services):
    response = client.get("/strategy-manager/results/does-not-exist")
    assert response.status_code == 404


def test_result_integrity_error_renders_no_partial_data(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result_error = BacktestIntegrityError(
        "stored backtest result digest is invalid"
    )
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "stored backtest result digest is invalid" in response.text
    assert "Metrics" not in response.text
    assert "Trade Log" not in response.text


# --- Note CAS -----------------------------------------------------------


def test_result_note_read_state_shows_no_note_by_default(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note=None)
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "No note." in response.text


def test_result_note_read_state_shows_saved_text_escaped_exactly_once(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    stored_escaped = html.escape("Tom & Jerry <script>", quote=True)
    repo.result = _result(note=stored_escaped)
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "Tom &amp; Jerry &lt;script&gt;" in response.text
    assert "&amp;amp;" not in response.text  # never double-escaped
    # The user's hostile text must never appear unescaped/executable --
    # distinct from this page's own legitimate inline chart <script> tag.
    assert "Tom & Jerry <script>" not in response.text


def test_result_note_save_success(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note=None, note_version=1)
    response = client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": "Looks promising.", "expected_note_version": "1"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 200
    assert "Saved." in response.text
    assert "Looks promising." in response.text
    assert repo.last_note_call == (RESULT_RUN_ID, 1, "Looks promising.")
    assert repo.result is not None
    assert repo.result.note_version == 2


def test_result_note_save_empty_normalizes_to_no_note(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note="existing", note_version=1)
    response = client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": "   ", "expected_note_version": "1"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 200
    assert "No note." in response.text
    assert repo.result is not None
    assert repo.result.note is None


def test_result_note_save_stale_version_conflict(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note=None, note_version=3)
    response = client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": "my new note", "expected_note_version": "1"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 409
    assert "changed since you loaded it" in response.text
    assert "my new note" in response.text
    assert "Retry" in response.text
    assert "Saved." not in response.text
    # The stale save never touched the persisted Result.
    assert repo.result.note_version == 3


def test_result_note_save_over_length_returns_422(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note=None, note_version=1)
    too_long = "x" * 10_001
    response = client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": too_long, "expected_note_version": "1"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert "exceeds" in response.text
    assert "Saved." not in response.text
    assert repo.result.note_version == 1


def test_result_note_post_requires_auth_guard(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result(note=None, note_version=1)
    response = client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": "hello", "expected_note_version": "1"},
    )
    assert response.status_code == 403
    assert repo.last_note_call is None


def test_result_note_save_only_changes_note_fields(services):
    """AC 7: the only mutation this view permits is note/note_version --
    every other Result field (Metrics, events, curve, digests) survives a
    note save unchanged."""
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    before = _result(
        events=(_entry_event(1), _exit_event(2)), note=None, note_version=1
    )
    repo.result = before
    client.post(
        f"/strategy-manager/results/{RESULT_RUN_ID}/note",
        data={"note": "a decision note", "expected_note_version": "1"},
        headers={"X-Auth-Token": "s3cret"},
    )
    after = repo.result
    assert after is not None
    assert after.note_version == before.note_version + 1
    assert after.note != before.note
    for field in (
        "run_id",
        "strategy_id",
        "strategy_api_version",
        "strategy_source_digest",
        "parameters",
        "profile_hash",
        "start_month",
        "end_month",
        "ordered_month_digest",
        "base_currency",
        "starting_capital",
        "run_input_manifest_digest",
        "execution_contract_digest",
        "metrics",
        "metric_availability",
        "events",
        "equity_curve",
        "final_cash_base",
        "completed_at",
    ):
        assert getattr(after, field) == getattr(before, field)


def test_backtests_list_links_complete_row_to_result_url(services):
    repo, _ = services
    repo.backtest_activities = (
        BacktestActivitySummaryV1(
            job=cast(
                StrategyJobV1,
                SimpleNamespace(
                    id="job-complete",
                    enqueue_seq=1,
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
                BacktestMetricsV1, SimpleNamespace(total_return=0.1, win_rate=0.5)
            ),
            metric_availability=cast(MetricAvailabilityV1, SimpleNamespace()),
        ),
        BacktestActivitySummaryV1(
            job=cast(
                StrategyJobV1,
                SimpleNamespace(
                    id="job-running",
                    enqueue_seq=2,
                    status=StrategyJobStatus.RUNNING,
                    cancel_requested_at=None,
                ),
            ),
            strategy_id="momentum_v1",
            strategy_api_version=1,
            parameter_summary="lookback=20",
            start_month="2024-01",
            end_month="2024-02",
            metrics=None,
            metric_availability=None,
        ),
    )
    response = client.get("/strategy-manager/backtests")
    assert response.status_code == 200
    assert 'href="/strategy-manager/results/job-complete"' in response.text
    assert 'href="/strategy-manager/activities/job-running"' in response.text


# ---------------------------------------------------------------------------
# Story 3.2: Compare picker -- choose an eligible Result to compare against
# ---------------------------------------------------------------------------


def _candidate(**overrides: object) -> ComparisonCandidateV1:
    defaults: dict[str, object] = dict(
        run_id="run-2",
        strategy_id="mean_reversion_v1",
        strategy_api_version=2,
        parameter_summary="window=10",
        start_month="2024-01",
        end_month="2024-01",
        base_currency="GBP",
        profile_hash=RESULT_PROFILE_HASH,
    )
    defaults.update(overrides)
    return ComparisonCandidateV1(**defaults)  # type: ignore[arg-type]


def test_result_page_compare_link_is_active_not_disabled(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    response = client.get(f"/strategy-manager/results/{RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "Compare (coming soon)" not in text
    match = re.search(r"<a [^>]*>Compare</a>", text)
    assert match is not None, "Compare link not found"
    compare_link = match.group(0)
    assert "disabled" not in compare_link
    assert "aria-disabled" not in compare_link
    assert f'hx-get="/strategy-manager/compare?run_id={RESULT_RUN_ID}"' in compare_link
    assert 'hx-target="#tab-content" hx-swap="innerHTML"' in compare_link


def test_compare_picker_lists_eligible_candidates_none_preselected(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    response = client.get(f"/strategy-manager/compare?run_id={RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert "momentum_v1 v1" in text  # anchor identity named
    assert RESULT_RUN_ID in text
    assert "mean_reversion_v1 v2" in text
    assert "window=10" in text
    assert "2024-01 to 2024-01" in text
    assert "GBP" in text
    assert "run-2" in text
    # No candidate option carries `selected` -- only the disabled placeholder does.
    assert '<option value="run-2">' in text
    assert '<option value="" selected disabled>' in text


def test_compare_picker_empty_state_has_no_submit_control(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = ()
    response = client.get(f"/strategy-manager/compare?run_id={RESULT_RUN_ID}")
    assert response.status_code == 200
    text = response.text
    assert (
        "Another completed result using the same period, scanner evidence, "
        "base currency, and execution rules is required." in text
    )
    assert "<select" not in text
    assert "<form" not in text
    assert "Compare</button>" not in text


def test_compare_picker_redirects_missing_anchor_to_activity_shell(services):
    response = client.get(
        "/strategy-manager/compare?run_id=does-not-exist", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/strategy-manager/activities/does-not-exist"


def test_compare_picker_redirects_non_complete_anchor_to_activity_shell(services):
    repo, _ = services
    repo.activity = SimpleNamespace(
        id=RESULT_RUN_ID,
        job_type=StrategyJobType.BACKTEST,
        status=StrategyJobStatus.RUNNING,
        status_version=1,
        current_month="2024-01",
        cancel_requested_at=None,
        failed_month=None,
        failure_detail=None,
    )
    response = client.get(
        f"/strategy-manager/compare?run_id={RESULT_RUN_ID}", follow_redirects=False
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == f"/strategy-manager/activities/{RESULT_RUN_ID}"
    )


def test_compare_picker_corrupt_job_row_renders_integrity_error(services):
    repo, _ = services
    repo.strategy_job_error = BacktestIntegrityError("stored strategy job is invalid")
    response = client.get(
        f"/strategy-manager/compare?run_id={RESULT_RUN_ID}", follow_redirects=False
    )
    assert response.status_code == 200
    assert "stored strategy job is invalid" in response.text
    assert "Compare against" not in response.text


def test_compare_picker_integrity_error_renders_no_partial_data(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result_error = BacktestIntegrityError(
        "stored backtest result digest is invalid"
    )
    response = client.get(f"/strategy-manager/compare?run_id={RESULT_RUN_ID}")
    assert response.status_code == 200
    assert "stored backtest result digest is invalid" in response.text
    assert "Compare against" not in response.text


def test_compare_submit_success_redirects_and_mutates_nothing(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.post(
        "/strategy-manager/compare",
        data={"run_id": RESULT_RUN_ID, "candidate_run_id": "run-2"},
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/strategy-manager/comparisons/{RESULT_RUN_ID}/run-2"
    )
    assert repo.last_is_comparable_call == (RESULT_RUN_ID, "run-2")
    assert repo.last_note_call is None
    assert repo.result is not None
    assert repo.result.note_version == 1  # unchanged -- no mutation occurred


def test_compare_submit_stale_ineligible_returns_422_with_reason(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    repo.eligibility = ComparisonEligibilityV1(
        eligible=False,
        reason=ComparisonIneligibleReason.PERIOD_MISMATCH,
        detail="start_month differs: '2024-01' vs '2024-02'",
    )
    response = client.post(
        "/strategy-manager/compare",
        data={"run_id": RESULT_RUN_ID, "candidate_run_id": "run-2"},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    text = response.text
    assert 'id="compare-picker-errors"' in text
    assert 'role="alert"' in text
    assert 'tabindex="-1"' in text
    assert "start_month differs" in text
    # A fresh candidate list is re-fetched, and nothing is preselected.
    assert "mean_reversion_v1 v2" in text
    assert '<option value="" selected disabled>' in text


def test_compare_submit_missing_candidate_id_treated_as_ineligible(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    response = client.post(
        "/strategy-manager/compare",
        data={"run_id": RESULT_RUN_ID},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert repo.last_is_comparable_call == (RESULT_RUN_ID, "")


def test_compare_submit_anchor_integrity_error_branch_no_redirect(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.eligibility_error = BacktestIntegrityError(
        "stored backtest result digest is invalid"
    )
    response = client.post(
        "/strategy-manager/compare",
        data={"run_id": RESULT_RUN_ID, "candidate_run_id": "run-2"},
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "stored backtest result digest is invalid" in response.text


def test_compare_submit_requires_auth_guard(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    response = client.post(
        "/strategy-manager/compare",
        data={"run_id": RESULT_RUN_ID, "candidate_run_id": "run-2"},
    )
    assert response.status_code == 403
    assert repo.last_is_comparable_call is None


def test_compare_picker_reason_query_param_populates_picker_error(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    response = client.get(
        "/strategy-manager/compare",
        params={"run_id": RESULT_RUN_ID, "reason": "some reason text"},
    )
    assert response.status_code == 200
    text = response.text
    assert 'id="compare-picker-errors"' in text
    assert 'role="alert"' in text
    assert 'tabindex="-1"' in text
    assert "some reason text" in text


def test_compare_picker_without_reason_shows_no_picker_error(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    response = client.get(f"/strategy-manager/compare?run_id={RESULT_RUN_ID}")
    assert response.status_code == 200
    assert 'id="compare-picker-errors"' not in response.text


def test_compare_picker_empty_reason_shows_no_picker_error(services):
    repo, _ = services
    repo.activity = _complete_backtest_activity()
    repo.result = _result()
    repo.candidates = (_candidate(),)
    response = client.get(
        "/strategy-manager/compare", params={"run_id": RESULT_RUN_ID, "reason": ""}
    )
    assert response.status_code == 200
    assert 'id="compare-picker-errors"' not in response.text


# ---------------------------------------------------------------------------
# Story 3.3: Comparison -- review two eligible Results side by side
# ---------------------------------------------------------------------------


def test_comparison_equity_payload_zips_matching_date_sequences():
    left = _result(
        run_id="run-1",
        equity_curve=(_equity_point(1, "10000.00", 1), _equity_point(2, "10450.00", 2)),
    )
    right = _result(
        run_id="run-2",
        equity_curve=(_equity_point(1, "9500.00", 1), _equity_point(2, "9820.50", 2)),
    )
    payload = comparison_equity_payload(left, right)
    assert payload == (
        {
            "date": "2024-01-01",
            "equity_a": 10000.0,
            "equity_a_display": "10,000.00",
            "equity_b": 9500.0,
            "equity_b_display": "9,500.00",
        },
        {
            "date": "2024-01-02",
            "equity_a": 10450.0,
            "equity_a_display": "10,450.00",
            "equity_b": 9820.5,
            "equity_b_display": "9,820.50",
        },
    )


def test_comparison_equity_payload_raises_on_divergent_dates():
    left = _result(run_id="run-1", equity_curve=(_equity_point(1, "10000.00", 1),))
    right = _result(run_id="run-2", equity_curve=(_equity_point(2, "9500.00", 1),))
    with pytest.raises(BacktestIntegrityError):
        comparison_equity_payload(left, right)


def test_comparison_happy_path_renders_both_sides(services):
    repo, _ = services
    repo.result = _result(run_id="run-1")
    repo.result_b = _result(run_id="run-2")
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.get("/strategy-manager/comparisons/run-1/run-2")
    assert response.status_code == 200
    text = response.text
    assert repo.last_is_comparable_call == ("run-1", "run-2")
    assert "Result A" in text
    assert "Result B" in text
    assert "run-1" in text
    assert "run-2" in text
    assert "Metrics" in text
    assert "Trade Log" in text
    assert "Provenance" in text
    assert "comparison-equity-chart" in text
    assert "View equity data table" in text
    # Notes are a standalone-Result concern -- never rendered here.
    assert "Decision note" not in text


def test_comparison_trade_log_resolves_security_labels_on_both_sides(services):
    # gh-367: the comparison view uses the same pinned-roster label
    # resolution as the single-result view, on both sides.
    repo, _ = services
    repo.result = _result(run_id="run-1", events=(_entry_event(1), _exit_event(2)))
    repo.result_b = _result(run_id="run-2", events=(_entry_event(1),))
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.get("/strategy-manager/comparisons/run-1/run-2")
    assert response.status_code == 200
    text = response.text
    assert text.count("MSFT (XNAS)") >= 3
    assert text.count("Security ID: SEC1") >= 3
    assert "<td>SEC1</td>" not in text


def test_comparison_ineligible_redirects_to_picker_with_reason(services):
    repo, _ = services
    repo.eligibility = ComparisonEligibilityV1(
        eligible=False,
        reason=ComparisonIneligibleReason.TOMBSTONED,
        detail="'run-2' is not eligible for comparison (tombstoned)",
    )
    response = client.get(
        "/strategy-manager/comparisons/run-1/run-2", follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/strategy-manager/compare?run_id=run-1&reason=")
    assert repo.last_is_comparable_call == ("run-1", "run-2")

    # Following the redirect re-renders the picker with the reason surfaced
    # as `picker_error` -- never a stale/trusted eligibility state.
    repo.activity = _complete_backtest_activity(run_id="run-1")
    repo.result = _result(run_id="run-1")
    follow = client.get(location)
    assert follow.status_code == 200
    assert "is not eligible for comparison (tombstoned)" in follow.text
    assert 'id="compare-picker-errors"' in follow.text


def test_comparison_self_comparison_redirects_same_as_ineligible(services):
    repo, _ = services
    repo.eligibility = ComparisonEligibilityV1(
        eligible=False,
        reason=ComparisonIneligibleReason.SELF_COMPARISON,
        detail="'run-1' cannot be compared to itself",
    )
    response = client.get(
        "/strategy-manager/comparisons/run-1/run-1", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/strategy-manager/compare?run_id=run-1&reason="
    )


def test_comparison_ineligible_redirect_urlencodes_run_id_a(services):
    repo, _ = services
    repo.eligibility = ComparisonEligibilityV1(
        eligible=False,
        reason=ComparisonIneligibleReason.NOT_FOUND,
        detail="'run&1' is not eligible for comparison (not found)",
    )
    response = client.get(
        "/strategy-manager/comparisons/run%261/run-2", follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    # A raw `&` in run_id_a must not be reflected unescaped into the
    # redirect's query string -- that would inject a second query param
    # (e.g. a bare `1&reason=...`) ahead of the real `reason`.
    assert location.startswith("/strategy-manager/compare?run_id=run%261&reason=")
    assert location.count("&") == 1


def test_comparison_vanished_side_a_result_renders_integrity_error(services):
    repo, _ = services
    repo.result_b = _result(run_id="run-2")
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.get("/strategy-manager/comparisons/run-1/run-2")
    assert response.status_code == 200
    text = response.text
    assert "backtest result evidence is missing for a complete job" in text
    assert "Metrics" not in text
    assert "Trade Log" not in text
    assert "Reload comparison" in text
    assert "View Result A" in text
    assert "View Result B" in text


def test_comparison_corrupt_side_b_result_renders_integrity_error(services):
    repo, _ = services
    repo.result = _result(run_id="run-1")
    repo.result_b = _result(run_id="run-2")
    repo.result_b_error = BacktestIntegrityError(
        "stored backtest result digest is invalid"
    )
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.get("/strategy-manager/comparisons/run-1/run-2")
    assert response.status_code == 200
    text = response.text
    assert "stored backtest result digest is invalid" in text
    assert "Metrics" not in text
    assert "Trade Log" not in text


def test_comparison_equity_date_mismatch_renders_integrity_error(services):
    repo, _ = services
    repo.result = _result(run_id="run-1")
    repo.result_b = _result(
        run_id="run-2",
        equity_curve=(_equity_point(2, "9500.00", 1), _equity_point(30, "9800.00", 2)),
    )
    repo.eligibility = ComparisonEligibilityV1(eligible=True, reason=None, detail="")
    response = client.get("/strategy-manager/comparisons/run-1/run-2")
    assert response.status_code == 200
    text = response.text
    assert "comparison equity curves diverge" in text
    assert "Metrics" not in text
    assert "Trade Log" not in text
