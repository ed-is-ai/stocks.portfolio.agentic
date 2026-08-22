"""Backtest launch orchestration boundary (Story 2.7).

``BacktestLaunchService`` is the one place a web submission is turned into
a durably enqueued Backtest attempt. It never recalculates Story 2.1-2.6
logic -- it rediscovers the Strategy (``discover_strategies``), revalidates
parameters (``validate_strategy_parameters``), resolves the full
active-profile roster's pinned evidence, builds/verifies the Story 2.3
manifest (``build_run_input_manifest``), and calls Story 2.6's atomic
``StrategyJobService.enqueue_backtest`` -- never persisting anything before
every validation step has passed.

``securities`` for the manifest is resolved as every member of the active
profile's roster captured for the chosen ``start_month`` (v1 has no
Strategy-declared watchlist parameter type, so the full roster is the only
mechanically supportable universe -- Design Notes, spec-2-7-2-8). FX
evidence is resolved through ``FxQuoteRepository`` (never
``HistoricalPriceRepository``, and never ``app.services.gbp_valuation_service``,
which is Story 1.6's live-valuation-only tool and explicitly forbidden
from any cross-epic import), pinned at the chosen period's first calendar
day -- a deterministic, documented choice, not a per-day rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, cast

from app.core.config import ROOT_DIR, SKILLS_DIR
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
)
from app.services.backtest.run_input_manifest import (
    PinnedSecurityEvidenceV1,
    RunInputManifestError,
    build_run_input_manifest,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyDiscoveryResultV1,
    discover_strategies,
)
from app.services.backtest.snapshot_profile import CoverageSummaryV1, SnapshotProfileV1
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    BacktestSubmissionV1,
    PreparationSubmissionV1,
    PreparationEnqueueResultV1,
    RunUniverseSelectionV1,
    StrategyJobConflict,
)
from app.services.backtest.strategy_job_service import StrategyJobService
from app.services.backtest.strategy_protocol import (
    JsonValue,
    validate_strategy_parameters,
)
from app.services.backtest.trading_calendar import (
    CalendarContractError,
    TradingCalendar,
)

#: The only currency pair v1 needs to resolve -- ``base_currency`` and every
#: evidence ``currency`` are both closed to ``{"GBP", "USD"}`` (the same
#: ``Literal`` ``BacktestSubmissionV1``/``LegitimateExclusionProofV1`` already
#: enforce), so exactly one cross-currency pair can ever exist.
_CURRENCY_PAIR: dict[frozenset[str], str] = {frozenset({"GBP", "USD"}): "GBPUSD=X"}


@dataclass(frozen=True)
class LaunchFieldError:
    """One structured, per-field launch validation failure.

    ``field`` is either a stable form field id (``strategy_id``,
    ``start_month``, ``end_month``, ``base_currency``, ``starting_capital``,
    or a ``param__<name>`` dynamic parameter id) or ``"form"`` for a
    whole-submission failure with no single associated control.
    """

    field: str
    message: str


class BacktestLaunchValidationError(ValueError):
    """Raised for any correctable launch failure -- no job is ever created.

    Carries every field-level error at once (never just the first) so the
    route can render one linked, focused error summary.
    """

    def __init__(self, errors: tuple[LaunchFieldError, ...]) -> None:
        if not errors:
            raise ValueError("a validation error requires at least one field error")
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))


class _RosterEvidenceError(ValueError):
    """Internal: a roster-evidence resolution problem, always converted to
    a :class:`BacktestLaunchValidationError` before leaving this module."""

    code = "required_data_missing"


@dataclass(frozen=True)
class BacktestConfigurationViewV1:
    """Everything the configuration GET view renders (Story 2.7 AC 1, 3, 4).

    ``coverage``/``coverage_error`` and ``profile`` mirror
    ``strategy_manager.py``'s existing ``_coverage_context``/
    ``_profile_context`` shape -- built fresh on every render, never cached.
    """

    strategies: tuple[StrategyDescriptorV1, ...]
    warnings: tuple[object, ...]
    coverage: CoverageSummaryV1 | None
    coverage_error: str | None
    profile: SnapshotProfileV1 | None


@dataclass(frozen=True)
class BacktestLaunchCommandV1:
    """One form-neutral launch request -- already decoded to exact JSON
    scalar types by the route's codec, but re-verified in full by
    :meth:`BacktestLaunchService.launch` before anything is persisted."""

    strategy_id: str
    rendered_profile_hash: str | None
    start_month: str
    end_month: str
    base_currency: Literal["GBP", "USD"]
    starting_capital: Decimal
    parameters: Mapping[str, JsonValue]
    idempotency_key: str | None = None
    universe_selection: RunUniverseSelectionV1 | None = None


def _fx_pair(base_currency: str, security_currency: str) -> str | None:
    if base_currency == security_currency:
        return None
    return _CURRENCY_PAIR.get(frozenset({base_currency, security_currency}))


class BacktestLaunchService:
    """Orchestrates one Backtest launch end to end (Story 2.7 AC 4-8)."""

    def __init__(
        self,
        *,
        backtest_repo: BacktestRepository,
        historical_price_repo: HistoricalPriceRepository,
        fx_quote_repo: FxQuoteRepository,
        jobs: StrategyJobService,
        skills_root: Path = SKILLS_DIR,
        project_root: Path = ROOT_DIR,
    ) -> None:
        self._backtest_repo = backtest_repo
        self._historical_price_repo = historical_price_repo
        self._fx_quote_repo = fx_quote_repo
        self._jobs = jobs
        self._skills_root = skills_root
        self._project_root = project_root

    def discover(self) -> StrategyDiscoveryResultV1:
        """Return one fresh Story 2.2 discovery result -- never cached."""
        return discover_strategies(self._skills_root)

    def configuration(self) -> BacktestConfigurationViewV1:
        """Return everything the configuration GET view needs to render."""
        discovery = self.discover()
        coverage: CoverageSummaryV1 | None = None
        coverage_error: str | None = None
        profile: SnapshotProfileV1 | None = None
        try:
            active = self._backtest_repo.active_snapshot_profile()
            if active is not None:
                coverage = self._backtest_repo.snapshot_coverage(active.profile_hash)
                profile = self._backtest_repo.snapshot_profile(active.profile_hash)
        except BacktestIntegrityError as exc:
            # A later failure (e.g. snapshot_profile()) must not leave an
            # earlier successful read (coverage) visible alongside the
            # error -- callers treat coverage_error as fully replacing
            # coverage, never as an annotation on top of stale data.
            coverage = None
            profile = None
            coverage_error = str(exc)
        return BacktestConfigurationViewV1(
            strategies=discovery.strategies,
            warnings=discovery.warnings,
            coverage=coverage,
            coverage_error=coverage_error,
            profile=profile,
        )

    def launch(
        self, command: BacktestLaunchCommandV1
    ) -> BacktestEnqueueResultV1 | PreparationEnqueueResultV1:
        """Validate ``command`` end to end and enqueue exactly one attempt.

        Raises :class:`BacktestLaunchValidationError` for any correctable
        problem (unknown/stale Strategy, active-profile change, an
        out-of-coverage period, invalid capital/currency, invalid
        parameters, or missing roster/FX evidence) -- no job is ever
        created on that path. Re-raises :class:`StrategyJobConflict`
        verbatim for a race Story 2.6's own atomic transaction catches
        (e.g. the profile deactivated between this method's read and the
        write), wrapped as a single ``"form"`` field error.
        """
        errors: list[LaunchFieldError] = []

        strategy = self._resolve_strategy(command.strategy_id)
        if strategy is None:
            raise BacktestLaunchValidationError(
                (
                    LaunchFieldError(
                        "strategy_id", "Choose a currently available Strategy."
                    ),
                )
            )

        active = self._backtest_repo.active_snapshot_profile()
        if active is None:
            raise BacktestLaunchValidationError(
                (LaunchFieldError("form", "No active scanner-data version is set."),)
            )
        if (
            command.rendered_profile_hash is not None
            and command.rendered_profile_hash != active.profile_hash
        ):
            raise BacktestLaunchValidationError(
                (
                    LaunchFieldError(
                        "form",
                        "The active scanner-data version changed since this "
                        "form was loaded. Choose a period again.",
                    ),
                )
            )
        profile_hash = active.profile_hash

        errors.extend(
            self._validate_period(profile_hash, command.start_month, command.end_month)
        )

        if not command.starting_capital.is_finite() or command.starting_capital <= 0:
            errors.append(
                LaunchFieldError(
                    "starting_capital", "Enter a positive, finite starting capital."
                )
            )

        validated_parameters = validate_strategy_parameters(
            strategy.parameters, command.parameters, apply_defaults=True
        )
        if isinstance(validated_parameters, tuple):
            errors.extend(
                LaunchFieldError(f"param__{error.parameter_name}", error.message)
                for error in validated_parameters
            )

        if errors:
            raise BacktestLaunchValidationError(tuple(errors))

        if command.universe_selection is not None:
            if command.idempotency_key is None:
                raise BacktestLaunchValidationError(
                    (
                        LaunchFieldError(
                            "form", "Preparation request identity is missing."
                        ),
                    )
                )
            try:
                return self._jobs.enqueue_preparation(
                    PreparationSubmissionV1(
                        selection=command.universe_selection,
                        strategy_id=strategy.strategy_id,
                        strategy_api_version=strategy.api_version,
                        strategy_source_digest=strategy.source_digest,
                        parameters=dict(command.parameters),
                        start_month=command.start_month,
                        end_month=command.end_month,
                        base_currency=command.base_currency,
                        starting_capital=command.starting_capital,
                        idempotency_key=command.idempotency_key,
                    )
                )
            except (StrategyJobConflict, ValueError) as exc:
                raise BacktestLaunchValidationError(
                    (LaunchFieldError("form", str(exc)),)
                ) from exc

        try:
            securities = self._resolve_roster_evidence(
                profile_hash=profile_hash,
                snapshot_month=command.start_month,
                base_currency=command.base_currency,
            )
        except _RosterEvidenceError as exc:
            raise BacktestLaunchValidationError(
                (LaunchFieldError("form", str(exc)),)
            ) from exc

        try:
            manifest = build_run_input_manifest(
                project_root=self._project_root,
                backtest_repo=self._backtest_repo,
                historical_price_repo=self._historical_price_repo,
                fx_quote_repo=self._fx_quote_repo,
                strategy=strategy,
                submitted_parameters=cast(
                    Mapping[str, JsonValue], validated_parameters
                ),
                profile_hash=profile_hash,
                start_month=command.start_month,
                end_month=command.end_month,
                base_currency=command.base_currency,
                starting_capital=command.starting_capital,
                securities=securities,
            )
        except (RunInputManifestError, EvidenceMissingError) as exc:
            raise BacktestLaunchValidationError(
                (LaunchFieldError("form", str(exc)),)
            ) from exc

        submission = BacktestSubmissionV1(
            strategy_id=strategy.strategy_id,
            strategy_api_version=strategy.api_version,
            strategy_source_digest=strategy.source_digest,
            parameters=dict(manifest.parameters),
            profile_hash=profile_hash,
            start_month=command.start_month,
            end_month=command.end_month,
            base_currency=command.base_currency,
            starting_capital=command.starting_capital,
            run_input_manifest_digest=manifest.digest(),
            execution_contract_digest=manifest.execution_contract_digest(),
            canonical_manifest_json=manifest.canonical_json(),
            idempotency_key=command.idempotency_key,
        )
        try:
            return self._jobs.enqueue_backtest(submission)
        except StrategyJobConflict as exc:
            raise BacktestLaunchValidationError(
                (LaunchFieldError("form", str(exc)),)
            ) from exc

    def _resolve_strategy(self, strategy_id: str) -> StrategyDescriptorV1 | None:
        for strategy in self.discover().strategies:
            if strategy.strategy_id == strategy_id:
                return strategy
        return None

    def _validate_period(
        self, profile_hash: str, start_month: str, end_month: str
    ) -> tuple[LaunchFieldError, ...]:
        errors: list[LaunchFieldError] = []
        for field, value in (("start_month", start_month), ("end_month", end_month)):
            try:
                TradingCalendar.months_inclusive(value, value)
            except CalendarContractError:
                errors.append(
                    LaunchFieldError(field, "Use a valid calendar month in YYYY-MM.")
                )
        if errors:
            return tuple(errors)
        if start_month > end_month:
            return (
                LaunchFieldError(
                    "end_month", "End month must be on or after start month."
                ),
            )
        try:
            coverage = self._backtest_repo.snapshot_coverage(profile_hash)
        except BacktestIntegrityError as exc:
            return (LaunchFieldError("form", str(exc)),)
        in_one_interval = any(
            interval.start_month <= start_month and end_month <= interval.end_month
            for interval in coverage.intervals
        )
        if not in_one_interval:
            return (
                LaunchFieldError(
                    "form",
                    "Choose a period inside one contiguous Ready interval.",
                ),
            )
        return ()

    def _resolve_roster_evidence(
        self,
        *,
        profile_hash: str,
        snapshot_month: str,
        base_currency: Literal["GBP", "USD"],
        selected_security_ids: tuple[str, ...] | None = None,
    ) -> tuple[PinnedSecurityEvidenceV1, ...]:
        """Pin every active-profile roster member's price/action/FX
        evidence for ``snapshot_month`` -- the chosen period's start month,
        a deterministic (if necessarily approximate, per Design Notes)
        choice of which month's committed roster represents "the full
        active-profile roster" for the whole Run.
        """
        try:
            members = self._backtest_repo.snapshot_member_revisions(
                profile_hash, snapshot_month
            )
        except BacktestIntegrityError as exc:
            raise _RosterEvidenceError(str(exc)) from exc
        if not members:
            raise _RosterEvidenceError(
                "The active profile's roster has no members for the chosen period."
            )
        if selected_security_ids is not None:
            selected = set(selected_security_ids)
            members = tuple(item for item in members if item[0] in selected)
            if {item[0] for item in members} != selected:
                raise _RosterEvidenceError("Selected evidence is unavailable.")
        fx_as_of = f"{snapshot_month}-01"
        securities: list[PinnedSecurityEvidenceV1] = []
        # Collect every problem across the whole roster (never just the
        # first) -- matching BacktestLaunchValidationError's own documented
        # "carries every field-level error at once" contract, so an
        # operator sees every missing security's evidence gap in one pass
        # instead of fixing and resubmitting one security at a time.
        problems: list[str] = []
        for security_id, revision in members:
            try:
                evidence = self._historical_price_repo.get(revision)
            except EvidenceMissingError:
                problems.append(
                    f"Pinned historical evidence is missing for {security_id!r}."
                )
                continue
            fx_revision: str | None = None
            if evidence.currency != base_currency:
                pair = _fx_pair(base_currency, evidence.currency)
                if pair is None:
                    problems.append(
                        f"No supported FX pair for {security_id!r}'s currency "
                        f"({evidence.currency}) against {base_currency}."
                    )
                    continue
                quote = self._fx_quote_repo.get_for_pair_and_date(pair, fx_as_of)
                if quote is None:
                    problems.append(
                        f"Pinned historical FX evidence is unavailable for "
                        f"{security_id!r} as of {fx_as_of}."
                    )
                    continue
                fx_revision = quote.digest
            securities.append(
                PinnedSecurityEvidenceV1(
                    security_id=security_id,
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=fx_revision,
                )
            )
        if problems:
            raise _RosterEvidenceError(" ".join(problems))
        return tuple(securities)


__all__ = [
    "BacktestConfigurationViewV1",
    "BacktestLaunchCommandV1",
    "BacktestLaunchService",
    "BacktestLaunchValidationError",
    "LaunchFieldError",
]
