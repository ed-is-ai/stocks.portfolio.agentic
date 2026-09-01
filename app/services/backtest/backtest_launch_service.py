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
evidence is two-stage (#459): the exact-date rate at the chosen period's
first calendar day must still resolve through ``FxQuoteRepository``
(never ``app.services.gbp_valuation_service``, which is Story 1.6's
live-valuation-only tool and explicitly forbidden from any cross-epic
import), and the daily ``GBPUSD=X`` rate series spanning the run window
is ingested into ``HistoricalPriceRepository`` under the
``fx:GBPUSD=X`` pseudo-security -- the series' content-addressed
``data_revision`` is what the manifest pins, because the engine replays
FX through the historical price cache, where a single-day
``fx_quotes`` digest never resolves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, cast

from app.core.config import ROOT_DIR, SKILLS_DIR
from app.integrations.fx_history import (
    ChainedFxQuoteFetcher,
    FxProviderUnavailable,
    FxUnsupportedPair,
)
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.fx_quote_repo import (
    FxQuote,
    FxQuoteRepository,
    FxUnavailableAttempt,
)
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
)
from app.services.backtest.historical_price_evidence import (
    FX_PAIR,
    FxSeriesFetcher,
    ProviderFailure,
    YFinanceFxSeriesFetcher,
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

logger = logging.getLogger(__name__)

#: Provider key under which a definitive all-providers-miss is negatively
#: cached in ``fx_unavailable_attempts`` -- one key per chain, so a repeat
#: preparation after a definitive miss never re-walks the providers.
_FX_BACKFILL_CHAIN_PROVIDER = "backfill_chain"

#: Log/summary caps so a huge roster can neither produce an unbounded log
#: line nor overflow the worker's 500-char failure_detail budget.
_MAX_LOGGED_PROBLEMS = 100
_MAX_SUMMARIES = 3

#: Calendar-day buffer prepended to the ingested FX series window so the
#: first run session always has an on-or-before close even when the
#: provider's first row lands mid-week (#459).
_FX_SERIES_WINDOW_BUFFER_DAYS = 7


def _month_start(month: str) -> date:
    """Return the first calendar day of a ``YYYY-MM`` month string."""
    return date.fromisoformat(f"{month}-01")


def _month_after(month: str) -> date:
    """Return the first calendar day of the month after ``YYYY-MM``."""
    year, value = (int(part) for part in month.split("-"))
    return date(year + (value == 12), value % 12 + 1, 1)


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

    #: The message is composed entirely by this module (never raw
    #: exception text), so the worker may surface it verbatim.
    user_safe_message = True


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
        fx_fetcher: ChainedFxQuoteFetcher | None = None,
        fx_series_fetcher: FxSeriesFetcher | None = None,
    ) -> None:
        self._backtest_repo = backtest_repo
        self._historical_price_repo = historical_price_repo
        self._fx_quote_repo = fx_quote_repo
        self._jobs = jobs
        self._skills_root = skills_root
        self._project_root = project_root
        self._fx_fetcher = fx_fetcher or ChainedFxQuoteFetcher()
        self._fx_series_fetcher = fx_series_fetcher or YFinanceFxSeriesFetcher()

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

        # ``strategy.universe.parameter`` is a host-owned runtime input, not
        # a Strategy-authored tuning parameter.  The Strategy Manager binds
        # it only after validating the roster selection, so do not hand it
        # to the strict user-parameter validator (which correctly rejects
        # every name absent from ``strategy.parameters``).
        universe_parameter = strategy.universe.parameter
        submitted_parameters = dict(command.parameters)
        bound_universe = submitted_parameters.pop(universe_parameter, None)
        if bound_universe is not None and command.universe_selection is None:
            errors.append(
                LaunchFieldError(
                    f"param__{universe_parameter}",
                    f"unknown parameter {universe_parameter!r}",
                )
            )
        validated_parameters = validate_strategy_parameters(
            strategy.parameters, submitted_parameters, apply_defaults=True
        )
        if isinstance(validated_parameters, tuple):
            errors.extend(
                LaunchFieldError(f"param__{error.parameter_name}", error.message)
                for error in validated_parameters
            )
        elif command.universe_selection is not None:
            expected_universe = list(command.universe_selection.canonical_security_ids)
            if bound_universe != expected_universe:
                errors.append(
                    LaunchFieldError(
                        f"param__{universe_parameter}",
                        "The selected securities do not match the prepared universe.",
                    )
                )
            else:
                validated_parameters = {
                    **validated_parameters,
                    universe_parameter: bound_universe,
                }

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
                start_month=command.start_month,
                end_month=command.end_month,
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
        start_month: str,
        end_month: str,
        selected_security_ids: tuple[str, ...] | None = None,
        pin_fx: bool = True,
    ) -> tuple[PinnedSecurityEvidenceV1, ...]:
        """Pin every active-profile roster member's price/action/FX
        evidence for ``snapshot_month`` -- the chosen period's start month,
        a deterministic (if necessarily approximate, per Design Notes)
        choice of which month's committed roster represents "the full
        active-profile roster" for the whole Run.

        ``start_month``/``end_month`` bound the Run window the ingested FX
        series must span (#459). Whenever any security needs FX, the daily
        ``GBPUSD=X`` series over that window is ingested into the
        historical price cache and its content-addressed revision is
        pinned as every FX-needing security's ``fx_revision`` -- the
        engine replays FX through the historical price cache, where a
        single-day ``fx_quotes`` digest never resolves. Re-ingesting the
        same window converges to the same ``data_revision`` (content
        addressing), so repeated preparations stay consistent.
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
        revision_by_security = dict(members)
        # Evidence is emitted in the roster's own order regardless of
        # which securities needed a backfill -- a cold cache must produce
        # the same manifest identity as a warm one.
        evidence_by_security: dict[str, PinnedSecurityEvidenceV1] = {}
        # Roster-order ids of every security whose evidence currency
        # differs from the base -- they all share the one ingested daily
        # series revision pinned below (#459).
        fx_security_ids: list[str] = []
        # Collect every problem across the whole roster (never just the
        # first) -- matching BacktestLaunchValidationError's own documented
        # "carries every field-level error at once" contract, so an
        # operator sees every missing security's evidence gap in one pass
        # instead of fixing and resubmitting one security at a time.
        problems: list[str] = []
        # Unique (pair, as_of) cache misses -> the securities waiting on
        # each. Backfilled once per miss (never per security); the fetched
        # quote proves the exact-date rate exists, while the pinned
        # revision comes from the ingested daily series below.
        fx_misses: dict[tuple[str, str], list[str]] = {}
        for security_id, revision in members:
            try:
                evidence = self._historical_price_repo.get(revision)
            except EvidenceMissingError:
                problems.append(
                    f"Pinned historical evidence is missing for {security_id!r}."
                )
                continue
            if pin_fx and evidence.currency != base_currency:
                pair = _fx_pair(base_currency, evidence.currency)
                if pair is None:
                    problems.append(
                        f"No supported FX pair for {security_id!r}'s currency "
                        f"({evidence.currency}) against {base_currency}."
                    )
                    continue
                quote = self._fx_quote_repo.get_for_pair_and_date(pair, fx_as_of)
                if quote is None:
                    fx_misses.setdefault((pair, fx_as_of), []).append(security_id)
                    continue
                fx_security_ids.append(security_id)
            evidence_by_security[security_id] = PinnedSecurityEvidenceV1(
                security_id=security_id,
                price_revision=revision,
                action_revision=revision,
                fx_revision=None,
            )
        if fx_misses:
            backfilled_ids, fx_problems, fx_summaries = self._resolve_fx_misses(
                fx_misses
            )
            fx_security_ids.extend(backfilled_ids)
            # The full per-security list is for the log only; the raised
            # error carries one actionable summary per distinct miss.
            if fx_problems:
                if len(fx_problems) > _MAX_LOGGED_PROBLEMS:
                    fx_problems = fx_problems[:_MAX_LOGGED_PROBLEMS] + [
                        f"... and {len(fx_problems) - _MAX_LOGGED_PROBLEMS} more."
                    ]
                logger.warning(
                    "FX evidence resolution problems: %s", " ".join(fx_problems)
                )
            problems.extend(fx_summaries)
        if fx_security_ids and not problems:
            series_revision = self._ingest_fx_series_revision(start_month, end_month)
            for security_id in fx_security_ids:
                revision = revision_by_security[security_id]
                evidence_by_security[security_id] = PinnedSecurityEvidenceV1(
                    security_id=security_id,
                    price_revision=revision,
                    action_revision=revision,
                    fx_revision=series_revision,
                )
        if problems:
            raise _RosterEvidenceError(" ".join(problems))
        return tuple(evidence_by_security[security_id] for security_id, _ in members)

    def _ingest_fx_series_revision(self, start_month: str, end_month: str) -> str:
        """Ingest the daily ``GBPUSD=X`` series over the Run window (#459).

        Fetches the series spanning ``start_month``'s first day (minus a
        small buffer) through ``end_month``'s boundary via the injectable
        series fetcher and commits it into the historical price cache
        under the ``fx:GBPUSD=X`` pseudo-security. ``commit`` is
        content-addressed, so re-preparing the same window converges to
        the identical revision. A provider failure degrades to a
        preparation failure whose message names the pair and window;
        anything else (e.g. a repository integrity error) propagates so
        the worker classifies it on its own merits -- never a silent
        misclassification.
        """
        window_start = _month_start(start_month) - timedelta(
            days=_FX_SERIES_WINDOW_BUFFER_DAYS
        )
        window_end = _month_after(end_month)
        try:
            payload = self._fx_series_fetcher.fetch(start=window_start, end=window_end)
            return self._historical_price_repo.commit(payload)
        except ProviderFailure as exc:
            logger.warning(
                "FX series ingestion failed for %s (%s..%s): %s",
                FX_PAIR,
                window_start.isoformat(),
                window_end.isoformat(),
                exc,
            )
            raise _RosterEvidenceError(
                f"Historical FX rate series for {FX_PAIR} covering "
                f"{start_month} to {end_month} could not be ingested -- "
                f"retry preparation, or choose a later start month if the "
                f"pair is unavailable for this window."
            ) from exc

    def _resolve_fx_misses(
        self,
        fx_misses: Mapping[tuple[str, str], list[str]],
    ) -> tuple[list[str], list[str], list[str]]:
        """Backfill each unique FX cache miss through the provider chain.

        Returns the successfully backfilled security ids (the caller pins
        the ingested daily series revision for them, #459 -- the fetched
        quote itself only proves the exact-date rate exists),
        per-security problem strings (log only), and one summary message
        per distinct miss for the raised error -- naming the date, pair,
        affected-security count, and the mode-specific remedy: (a)
        fetchable but the fetch failed ("retry preparation"), or (b) no
        provider has the rate ("choose a later start month").
        """
        backfilled: list[str] = []
        per_security: list[str] = []
        summaries: list[str] = []
        for (pair, as_of), security_ids in fx_misses.items():
            outcome, quote = self._fetch_or_classify(pair, as_of)
            if outcome == "fetched" and quote is not None:
                # The quote is persisted and its exact-date rate proven;
                # the pinned revision still comes from the ingested daily
                # series, never from the quote's own digest (#459).
                backfilled.extend(security_ids)
                continue
            for security_id in security_ids:
                per_security.append(
                    f"Pinned historical FX evidence is unavailable for "
                    f"{security_id!r} as of {as_of}."
                )
            count = len(security_ids)
            if outcome == "transient":
                summaries.append(
                    f"Historical FX evidence for {as_of} ({pair}) could not be "
                    f"fetched for {count} securities — retry preparation."
                )
            elif outcome == "unsupported":
                summaries.append(
                    f"Currency pair {pair} is not supported by any configured "
                    f"FX provider — remove affected securities or extend the "
                    f"provider mapping."
                )
            else:
                summaries.append(
                    f"No FX rate is available for {as_of} ({pair}) for {count} "
                    f"securities — choose a later start month."
                )
        # The worker truncates failure_detail at 500 chars -- keep the
        # remedy-bearing summaries inside that budget.
        if len(summaries) > _MAX_SUMMARIES:
            summaries = summaries[:_MAX_SUMMARIES] + [
                f"... and {len(summaries) - _MAX_SUMMARIES} more distinct FX gaps."
            ]
        return backfilled, per_security, summaries

    def _fetch_or_classify(self, pair: str, as_of: str) -> tuple[str, FxQuote | None]:
        """Fetch one missed (pair, as_of) and classify the outcome.

        Returns ``(outcome, quote)`` where outcome is ``"fetched"`` (the
        persisted quote is returned -- it proves the exact-date rate
        exists, while the pinned revision comes from the ingested daily
        series, #459), ``"transient"``
        (the chain raised -- never negatively cached), ``"unsupported"`
        (no provider series for the pair), or ``"definitive"`` (no
        provider has the rate; negatively cached under the
        ``backfill_chain`` provider key so a repeat preparation fails
        immediately without any network fetch).
        """
        attempt = self._fx_quote_repo.get_unavailable_attempt(
            _FX_BACKFILL_CHAIN_PROVIDER, pair, as_of
        )
        if attempt is not None and attempt.reason == "no_rate":
            return "definitive", None
        try:
            quote = self._fx_fetcher.fetch(pair, as_of)
        except FxUnsupportedPair as exc:
            logger.warning("FX pair unsupported: %s", exc)
            return "unsupported", None
        except FxProviderUnavailable as exc:
            logger.warning(
                "FX backfill chain failed for %s as of %s: %s", pair, as_of, exc
            )
            return "transient", None
        except Exception as exc:  # never let a fetch bug crash the worker
            logger.warning(
                "FX backfill chain raised unexpectedly for %s as of %s: %s",
                pair,
                as_of,
                exc,
            )
            return "transient", None
        if quote is None:
            self._fx_quote_repo.record_unavailable_attempt(
                FxUnavailableAttempt(
                    provider=_FX_BACKFILL_CHAIN_PROVIDER,
                    pair=pair,
                    requested_date=as_of,
                    reason="no_rate",
                )
            )
            return "definitive", None
        self._fx_quote_repo.insert_or_get(quote)
        return "fetched", quote


__all__ = [
    "BacktestConfigurationViewV1",
    "BacktestLaunchCommandV1",
    "BacktestLaunchService",
    "BacktestLaunchValidationError",
    "LaunchFieldError",
]
