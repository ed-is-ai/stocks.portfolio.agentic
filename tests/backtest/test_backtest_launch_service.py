"""Story 2.7 coverage: ``BacktestLaunchService.launch()``/``configuration()``.

Follows ``test_strategy_job_service.py``'s minimal-``FakeRepository``-per-
method style: each fake implements only the methods the module under test
actually calls, returning plain ``SimpleNamespace`` objects rather than
constructing the real (strict, frozen) pydantic models this module only
ever reads attributes off of.

``discover()`` is exercised against the real, already-established
``tests/fixtures/backtest-strategies/discovery`` fixture root (the same
root ``test_skill_discovery.py``/``test_run_input_manifest.py`` use) --
its only genuinely valid Strategy is ``valid-strategy`` (a required
``watch_security_id`` string parameter, an optional ``fixed_shares``
integer parameter bounded ``1..100``, default ``1``). Nothing here writes
a second Strategy fixture; ``configuration()``'s multi-Strategy state is
proven with two minimal ad-hoc Skill folders instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
)
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchCommandV1,
    BacktestLaunchService,
    BacktestLaunchValidationError,
)
from app.services.backtest.strategy_job import BacktestSubmissionV1
from app.services.backtest.strategy_job_service import StrategyJobService

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_ROOT = (
    PROJECT_ROOT / "tests" / "fixtures" / "backtest-strategies" / "discovery"
)
PROFILE_HASH: str = "a" * 64
ROSTER_DIGEST: str = "b" * 64
ALIAS_DIGEST: str = "c" * 64
ORDERED_MONTH_DIGEST: str = "d" * 64
FX_DIGEST: str = "e" * 64
PRICE_REVISION: str = "1" * 64


# ---------------------------------------------------------------------------
# Minimal fakes -- one method per call site, nothing more.
# ---------------------------------------------------------------------------


@dataclass
class FakeBacktestRepo:
    """Implements only the ``BacktestRepository`` methods
    ``BacktestLaunchService``/``build_run_input_manifest`` actually call."""

    coverage_intervals: tuple[tuple[str, str], ...] = (("2026-01", "2026-06"),)
    active_profile_hash: str | None = PROFILE_HASH
    member_revisions: tuple[tuple[str, str], ...] = (("sec-aapl", PRICE_REVISION),)
    member_revisions_error: BacktestIntegrityError | None = None
    coverage_error: BacktestIntegrityError | None = None
    ready: bool = True
    #: Coverage intervals returned only when ``snapshot_coverage`` is called
    #: with a profile hash other than ``active_profile_hash`` -- proves the
    #: caller always threads its own already-resolved profile hash through,
    #: never relying on this fake's "whatever is currently active" default.
    stale_profile_hash: str | None = None
    stale_coverage_intervals: tuple[tuple[str, str], ...] = ()
    seen_coverage_profile_hashes: list[str | None] = field(default_factory=list)

    def active_snapshot_profile(self):
        if self.active_profile_hash is None:
            return None
        return SimpleNamespace(profile_hash=self.active_profile_hash)

    def snapshot_coverage(self, profile_hash: str | None = None):
        self.seen_coverage_profile_hashes.append(profile_hash)
        if self.coverage_error is not None:
            raise self.coverage_error
        if profile_hash is None:
            raise AssertionError(
                "snapshot_coverage() must always be called with an explicit, "
                "already-resolved profile_hash -- never the current-active default"
            )
        intervals = (
            self.stale_coverage_intervals
            if profile_hash == self.stale_profile_hash
            else self.coverage_intervals
        )
        return SimpleNamespace(
            intervals=tuple(
                SimpleNamespace(start_month=start, end_month=end)
                for start, end in intervals
            )
        )

    def snapshot_profile(self, profile_hash: str):
        return SimpleNamespace(roster_digest=ROSTER_DIGEST)

    def snapshot_member_revisions(self, profile_hash: str, snapshot_month: str):
        if self.member_revisions_error is not None:
            raise self.member_revisions_error
        return self.member_revisions

    def interval_readiness(self, profile_hash: str, start_month: str, end_month: str):
        return SimpleNamespace(
            ready=self.ready,
            ordered_month_digest=ORDERED_MONTH_DIGEST if self.ready else None,
            missing_months=() if self.ready else (start_month,),
        )

    def roster_alias_revision(self, roster_digest: str):
        return ALIAS_DIGEST


@dataclass
class FakeHistoricalPriceRepo:
    """security_id -> currency for every revision this fake resolves."""

    evidence: dict[str, str] = field(default_factory=lambda: {PRICE_REVISION: "USD"})
    security_ids: dict[str, str] = field(
        default_factory=lambda: {PRICE_REVISION: "sec-aapl"}
    )

    def get(self, revision: str):
        if revision not in self.evidence:
            raise EvidenceMissingError(f"no evidence for {revision!r}")
        return SimpleNamespace(
            security_id=self.security_ids[revision], currency=self.evidence[revision]
        )


@dataclass
class FakeFxQuoteRepo:
    available: bool = True

    def get_for_pair_and_date(self, pair: str, as_of: str):
        if not self.available:
            return None
        return SimpleNamespace(digest=FX_DIGEST)

    def get_by_digest(self, digest: str):
        if not self.available or digest != FX_DIGEST:
            return None
        return SimpleNamespace(digest=digest)


class FakeJobs:
    def __init__(self) -> None:
        self.submissions: list[BacktestSubmissionV1] = []

    def enqueue_backtest(self, submission: BacktestSubmissionV1):
        self.submissions.append(submission)
        return SimpleNamespace(
            job=SimpleNamespace(id=f"job-{len(self.submissions)}"),
            backtest=SimpleNamespace(job_id=f"job-{len(self.submissions)}"),
        )


def _service(
    *,
    backtest_repo: FakeBacktestRepo | None = None,
    historical_price_repo: FakeHistoricalPriceRepo | None = None,
    fx_quote_repo: FakeFxQuoteRepo | None = None,
    jobs: FakeJobs | None = None,
    skills_root: Path = DISCOVERY_ROOT,
) -> tuple[BacktestLaunchService, FakeJobs]:
    jobs = jobs or FakeJobs()
    service = BacktestLaunchService(
        backtest_repo=cast(BacktestRepository, backtest_repo or FakeBacktestRepo()),
        historical_price_repo=cast(
            HistoricalPriceRepository,
            historical_price_repo or FakeHistoricalPriceRepo(),
        ),
        fx_quote_repo=cast(FxQuoteRepository, fx_quote_repo or FakeFxQuoteRepo()),
        jobs=cast(StrategyJobService, jobs),
        skills_root=skills_root,
        project_root=PROJECT_ROOT,
    )
    return service, jobs


def _command(**overrides: object) -> BacktestLaunchCommandV1:
    defaults: dict[str, object] = dict(
        strategy_id="valid-strategy",
        rendered_profile_hash=None,
        start_month="2026-02",
        end_month="2026-03",
        base_currency="GBP",
        starting_capital=Decimal("10000"),
        parameters={"watch_security_id": "sec-aapl", "fixed_shares": 5},
        idempotency_key=None,
    )
    defaults.update(overrides)
    return BacktestLaunchCommandV1(**defaults)  # type: ignore[arg-type]


def _field_errors(exc: BacktestLaunchValidationError) -> dict[str, str]:
    return {error.field: error.message for error in exc.errors}


# ---------------------------------------------------------------------------
# launch() -- happy path + idempotency-key passthrough
# ---------------------------------------------------------------------------


def test_launch_happy_path_enqueues_exactly_once() -> None:
    service, jobs = _service()

    result = service.launch(_command())

    assert len(jobs.submissions) == 1
    assert result.job.id == "job-1"
    submission = jobs.submissions[0]
    assert submission.strategy_id == "valid-strategy"
    assert submission.base_currency == "GBP"
    assert submission.starting_capital == Decimal("10000")
    # sec-aapl's evidence currency is USD, base_currency is GBP -- FX
    # evidence must have been pinned.
    assert submission.parameters["watch_security_id"] == "sec-aapl"
    assert submission.parameters["fixed_shares"] == 5


def test_launch_idempotency_key_passes_through_unchanged() -> None:
    service, jobs = _service()

    service.launch(_command(idempotency_key="submit-42"))

    assert jobs.submissions[0].idempotency_key == "submit-42"


def test_launch_same_currency_security_needs_no_fx_pin() -> None:
    """When ``base_currency`` already matches the security's own evidence
    currency, no FX pin is required -- ``_fx_pair`` must never be called
    with an unsupported pair for the common same-currency case."""
    price_repo = FakeHistoricalPriceRepo(evidence={PRICE_REVISION: "GBP"})
    fx_repo = FakeFxQuoteRepo(available=False)  # would fail if consulted
    service, jobs = _service(historical_price_repo=price_repo, fx_quote_repo=fx_repo)

    service.launch(_command(base_currency="GBP"))

    assert len(jobs.submissions) == 1


def test_launch_usd_base_currency_pins_fx_for_a_gbp_security() -> None:
    """The reverse direction of the same-currency test above: a USD-base
    Run against a GBP-denominated security must resolve and pin an FX
    quote exactly as the GBP-base/USD-security case does."""
    price_repo = FakeHistoricalPriceRepo(evidence={PRICE_REVISION: "GBP"})
    service, jobs = _service(historical_price_repo=price_repo)

    result = service.launch(_command(base_currency="USD"))

    assert len(jobs.submissions) == 1
    assert jobs.submissions[0].base_currency == "USD"
    assert result.job.id == "job-1"


# ---------------------------------------------------------------------------
# launch() -- unknown/stale Strategy
# ---------------------------------------------------------------------------


def test_launch_rejects_unknown_strategy_id() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(strategy_id="does-not-exist"))

    assert "strategy_id" in _field_errors(excinfo.value)
    assert jobs.submissions == []


# ---------------------------------------------------------------------------
# launch() -- active-profile mismatch
# ---------------------------------------------------------------------------


def test_launch_rejects_stale_rendered_profile_hash() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(rendered_profile_hash="f" * 64))

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_when_no_active_profile_is_set() -> None:
    repo = FakeBacktestRepo(active_profile_hash=None)
    service, jobs = _service(backtest_repo=repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command())

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


# ---------------------------------------------------------------------------
# launch() -- period: out of coverage / gapped interval / current-
# incomplete-month (all reduce to "not inside one contiguous Ready
# interval", the one authority ``_validate_period`` checks).
# ---------------------------------------------------------------------------


def test_launch_rejects_period_entirely_outside_coverage() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(start_month="2025-01", end_month="2025-02"))

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_period_crossing_a_coverage_gap() -> None:
    """Two separate Ready intervals with a gap between them -- a period
    whose months individually fall inside *some* coverage but spans both
    intervals must still be rejected (no single contiguous interval
    contains the whole requested range)."""
    repo = FakeBacktestRepo(
        coverage_intervals=(("2026-01", "2026-02"), ("2026-05", "2026-06"))
    )
    service, jobs = _service(backtest_repo=repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(start_month="2026-02", end_month="2026-05"))

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_validates_period_against_the_already_resolved_profile() -> None:
    """Regression: period validation must use the exact ``profile_hash``
    already resolved earlier in ``launch()``, never re-derive "whatever
    profile happens to be active" a second time. A profile switch between
    those two reads must not let a period be validated against the wrong
    profile's Ready intervals while evidence is pinned to the original one."""
    repo = FakeBacktestRepo(
        coverage_intervals=(("2026-01", "2026-06"),),
        stale_profile_hash="stale-profile-hash",
        stale_coverage_intervals=(("2020-01", "2020-06"),),
    )
    service, jobs = _service(backtest_repo=repo)

    result = service.launch(_command(start_month="2026-02", end_month="2026-03"))

    assert len(jobs.submissions) == 1
    # Every snapshot_coverage() call during this launch used the real,
    # already-resolved active profile hash -- never the stale one, and
    # never the "no hash given" default this fake would flag as a bug.
    assert repo.seen_coverage_profile_hashes
    assert all(h == PROFILE_HASH for h in repo.seen_coverage_profile_hashes)
    assert result.job.id == "job-1"


def test_launch_rejects_current_incomplete_month_not_yet_in_coverage() -> None:
    """A month simply never committed into any Ready interval (e.g. the
    current, still-incomplete month) is out-of-coverage exactly like any
    other unready period -- no separate "future month" rule exists."""
    repo = FakeBacktestRepo(coverage_intervals=(("2026-01", "2026-02"),))
    service, jobs = _service(backtest_repo=repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(start_month="2026-08", end_month="2026-08"))

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_malformed_month_strings() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(start_month="not-a-month", end_month="2026-03"))

    assert "start_month" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_end_month_before_start_month() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(start_month="2026-03", end_month="2026-02"))

    assert "end_month" in _field_errors(excinfo.value)
    assert jobs.submissions == []


# ---------------------------------------------------------------------------
# launch() -- invalid parameters
# ---------------------------------------------------------------------------


def test_launch_rejects_out_of_range_parameter() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(parameters={"fixed_shares": 999}))

    assert "param__fixed_shares" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_wrong_typed_parameter() -> None:
    """A ``bool`` must never satisfy an ``integer`` field."""
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(parameters={"fixed_shares": True}))

    assert "param__fixed_shares" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_unknown_parameter_name() -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError):
        service.launch(_command(parameters={"not_a_real_parameter": 1}))

    assert jobs.submissions == []


def test_launch_applies_declared_defaults_for_omitted_parameters() -> None:
    """Defaults form a valid runnable configuration (AC 2) -- omitting an
    optional parameter must never itself be a validation error."""
    service, jobs = _service()

    service.launch(_command(parameters={"watch_security_id": "sec-aapl"}))

    assert jobs.submissions[0].parameters["fixed_shares"] == 1


# ---------------------------------------------------------------------------
# launch() -- non-finite / non-positive capital
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capital", [Decimal("0"), Decimal("-1"), Decimal("Infinity"), Decimal("NaN")]
)
def test_launch_rejects_non_positive_or_non_finite_capital(capital: Decimal) -> None:
    service, jobs = _service()

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(starting_capital=capital))

    assert "starting_capital" in _field_errors(excinfo.value)
    assert jobs.submissions == []


# ---------------------------------------------------------------------------
# launch() -- missing roster / FX evidence
# ---------------------------------------------------------------------------


def test_launch_rejects_when_roster_has_no_members() -> None:
    repo = FakeBacktestRepo(member_revisions=())
    service, jobs = _service(backtest_repo=repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command())

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_missing_roster_evidence_integrity_error() -> None:
    repo = FakeBacktestRepo(
        member_revisions_error=BacktestIntegrityError("snapshot month does not exist")
    )
    service, jobs = _service(backtest_repo=repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command())

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_missing_price_evidence_for_a_roster_member() -> None:
    price_repo = FakeHistoricalPriceRepo(evidence={}, security_ids={})
    service, jobs = _service(historical_price_repo=price_repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command())

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


def test_launch_rejects_missing_fx_evidence_when_currencies_differ() -> None:
    fx_repo = FakeFxQuoteRepo(available=False)
    service, jobs = _service(fx_quote_repo=fx_repo)

    with pytest.raises(BacktestLaunchValidationError) as excinfo:
        service.launch(_command(base_currency="GBP"))

    assert "form" in _field_errors(excinfo.value)
    assert jobs.submissions == []


# ---------------------------------------------------------------------------
# configuration() -- zero/one/multiple-Strategy states, coverage errors
# ---------------------------------------------------------------------------


def test_configuration_zero_valid_strategies(tmp_path: Path) -> None:
    empty_root = tmp_path / "skills"
    empty_root.mkdir()
    service, _ = _service(skills_root=empty_root)

    view = service.configuration()

    assert view.strategies == ()


def test_configuration_one_valid_strategy() -> None:
    service, _ = _service()

    view = service.configuration()

    assert [s.strategy_id for s in view.strategies] == ["valid-strategy"]


def _write_minimal_strategy_skill(root: Path, folder: str, strategy_id: str) -> None:
    skill_dir = root / folder
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "strategy.py").write_text(
        "raise RuntimeError('never imported by discovery')\n", encoding="utf-8"
    )
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "kind: backtest-strategy\n"
        f"name: {strategy_id}\n"
        f"display_name: {strategy_id}\n"
        "description: A minimal ad-hoc fixture Strategy.\n"
        "api_version: 1\n"
        "runtime_files:\n"
        "  - scripts/strategy.py\n"
        "strategy_universe:\n"
        "  schema_version: strategy_universe.v1\n"
        "  mode: selected-securities\n"
        "  parameter: selected_securities\n"
        "parameters: []\n"
        "---\n\n# fixture\n",
        encoding="utf-8",
    )


def test_configuration_multiple_valid_strategies(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_minimal_strategy_skill(root, "alpha", "alpha")
    _write_minimal_strategy_skill(root, "beta", "beta")
    service, _ = _service(skills_root=root)

    view = service.configuration()

    assert {s.strategy_id for s in view.strategies} == {"alpha", "beta"}


def test_configuration_reports_coverage_integrity_error() -> None:
    repo = FakeBacktestRepo(coverage_error=BacktestIntegrityError("corrupt coverage"))
    service, _ = _service(backtest_repo=repo)

    view = service.configuration()

    assert view.coverage is None
    assert view.coverage_error == "corrupt coverage"


def test_configuration_no_active_profile_has_no_coverage() -> None:
    repo = FakeBacktestRepo(active_profile_hash=None)
    service, _ = _service(backtest_repo=repo)

    view = service.configuration()

    assert view.coverage is None
    assert view.coverage_error is None
    assert view.profile is None
