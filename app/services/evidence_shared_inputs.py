"""Report the run-level inputs no single security owns (#638).

FX, the pinned trading calendar and the regime benchmark are shared inputs:
when one of them is short, every affected security degrades at once, yet the
per-security evidence census reads each shortfall as that security's own
Strategy result. This module takes values that have already been read --
per-security session counts and currencies, the FX series session count, the
pinned calendar digest, the persisted regime snapshot and one input row per
discovered Strategy -- and returns a typed report naming the shared ceiling
and how far it reaches.

Everything here is a comparison of values handed in: no provider fetch, no
database write, no gate evaluation. In particular ``entry_signals_permitted``
is never called -- the benchmark verdict is ``snapshot.session_count >=
ma_length``, reported per Strategy because each Strategy declares its own MA
length, and left ``None`` (unaffected) rather than ``False`` whenever the
gate does not apply or no snapshot exists. Where the real gate fails closed --
an unusable MA length, a missing benchmark, a benchmark outside the run, or a
degraded reading -- the verdict is ``False``, never ``True``.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.market_regime import MarketRegimeSnapshotV1
from app.services.backtest.historical_price_evidence import (
    FX_PAIR,
    FX_SERIES_SECURITY_ID,
)
from app.services.backtest.regime_filter import (
    MIN_MA_LENGTH,
    REGIME_FILTER_BENCHMARK_PARAM,
    BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM,
    REGIME_FILTER_MA_LENGTH_PARAM,
)
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)


class _FrozenInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecurityCurrencyInputV1(_FrozenInputModel):
    """One security's already-read evidence currency and session count."""

    security_id: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    sessions: int = Field(default=0, ge=0)


class StrategyImpactInputV1(_FrozenInputModel):
    """One discovered Strategy's declared regime and evidence minimums."""

    strategy_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    regime_filter_enabled: bool = False
    benchmark_security_id: str | None = None
    ma_length: int | None = None
    entry_minimum_sessions: int = Field(default=0, ge=0)
    exit_minimum_sessions: int = Field(default=0, ge=0)
    #: Evidence kinds each path declares, verbatim from the requirements.
    entry_kinds: tuple[str, ...] = ()
    exit_kinds: tuple[str, ...] = ()


class FxSharedInputV1(_FrozenInputModel):
    """The FX series as one ceiling shared by every non-base security."""

    pair: str = FX_PAIR
    security_id: str = FX_SERIES_SECURITY_ID
    available: bool = False
    usable_session_ceiling: int | None = None
    #: Securities that need FX at all.
    securities_affected: int = Field(default=0, ge=0)
    #: Securities whose usable history is actually reduced by this ceiling --
    #: always <= ``securities_affected``, and the figure that separates
    #: "depends on FX" from "degraded by FX".
    securities_capped: int = Field(default=0, ge=0)


class SecurityFxImpactV1(_FrozenInputModel):
    """One security's own session count set beside the shared FX ceiling."""

    security_id: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    fx_required: bool
    sessions: int = Field(ge=0)
    fx_ceiling: int | None = None
    usable_sessions: int = Field(ge=0)


class CalendarSharedInputV1(_FrozenInputModel):
    """The pinned trading calendar's identity, carried verbatim."""

    digest: str = ""
    expected_sessions: int | None = None
    available: bool = False
    #: Securities whose usable history falls short of ``expected_sessions``.
    securities_affected: int = Field(default=0, ge=0)


class BenchmarkSharedInputV1(_FrozenInputModel):
    """The persisted regime snapshot's benchmark facts."""

    available: bool = False
    session_count: int = Field(default=0, ge=0)
    generated_at: str = ""
    #: Carried from the snapshot: a degraded reading cannot satisfy any gate.
    is_degraded: bool = False
    #: Strategies whose entry gate depends on this benchmark.
    strategies_affected: int = Field(default=0, ge=0)


class StrategyImpactV1(_FrozenInputModel):
    """One Strategy's benchmark verdict and eligible security counts."""

    strategy_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    regime_filter_enabled: bool = False
    benchmark_security_id: str | None = None
    ma_length: int | None = None
    benchmark_satisfied: bool | None = None
    entry_minimum_sessions: int = Field(default=0, ge=0)
    exit_minimum_sessions: int = Field(default=0, ge=0)
    entry_kinds: tuple[str, ...] = ()
    exit_kinds: tuple[str, ...] = ()
    eligible_entry: int = Field(default=0, ge=0)
    eligible_exit: int = Field(default=0, ge=0)


class SharedInputsReportV1(_FrozenInputModel):
    """Run-level shared inputs and the per-Strategy impact of each."""

    base_currency: str = Field(min_length=1)
    fx: FxSharedInputV1
    calendar: CalendarSharedInputV1
    benchmark: BenchmarkSharedInputV1
    securities: tuple[SecurityFxImpactV1, ...] = ()
    strategies: tuple[StrategyImpactV1, ...] = ()


def build_shared_inputs_report(
    *,
    base_currency: str,
    securities: Iterable[SecurityCurrencyInputV1],
    strategies: Iterable[StrategyImpactInputV1],
    fx_sessions: int | None = None,
    calendar_digest: str = "",
    calendar_expected_sessions: int | None = None,
    snapshot: MarketRegimeSnapshotV1 | None = None,
    holding_ids: frozenset[str] | None = None,
) -> SharedInputsReportV1:
    """Report the shared FX, calendar and benchmark inputs for one run.

    Every figure is a comparison of the values handed in. ``fx_sessions`` is
    the FX series' own session count (``None`` when the series is
    unavailable, in which case every non-base security reports a ceiling of
    zero: without the series nothing converts).
    ``snapshot`` is the persisted regime snapshot; its ``session_count`` is
    judged separately against each Strategy's declared MA length.

    ``holding_ids`` restricts ``eligible_exit`` alone: an exit path is only
    ever evaluated for a security actually held, so counting it over the
    whole scan universe would claim hundreds of securities are "eligible for
    exit". It does not narrow ``securities`` -- the benchmark's in-universe
    check derives from that set and must keep seeing the whole run. ``None``
    leaves ``eligible_exit`` scoped to ``securities``, as before.

    Rows are sorted by ``security_id`` and ``strategy_id`` so the same inputs
    supplied in any iteration order produce an equal report.
    """
    impacts = tuple(
        sorted(
            (_fx_impact(row, base_currency, fx_sessions) for row in securities),
            key=lambda row: row.security_id,
        )
    )
    strategy_rows = tuple(strategies)
    security_ids = frozenset(row.security_id for row in impacts)
    fx = FxSharedInputV1(
        available=fx_sessions is not None,
        usable_session_ceiling=fx_sessions,
        securities_affected=sum(1 for row in impacts if row.fx_required),
        securities_capped=sum(
            1 for row in impacts if row.usable_sessions < row.sessions
        ),
    )
    benchmark = BenchmarkSharedInputV1(
        available=snapshot is not None,
        session_count=0 if snapshot is None else snapshot.session_count,
        generated_at="" if snapshot is None else snapshot.generated_at,
        is_degraded=snapshot is not None and snapshot.is_degraded,
        strategies_affected=sum(
            1 for row in strategy_rows if row.regime_filter_enabled
        ),
    )
    usable = tuple(row.usable_sessions for row in impacts)
    exit_usable = (
        usable
        if holding_ids is None
        else tuple(
            row.usable_sessions for row in impacts if row.security_id in holding_ids
        )
    )
    return SharedInputsReportV1(
        base_currency=base_currency,
        fx=fx,
        calendar=CalendarSharedInputV1(
            digest=calendar_digest,
            expected_sessions=calendar_expected_sessions,
            available=bool(calendar_digest),
            securities_affected=(
                0
                if calendar_expected_sessions is None
                else sum(
                    1
                    for row in impacts
                    if row.usable_sessions < calendar_expected_sessions
                )
            ),
        ),
        benchmark=benchmark,
        securities=impacts,
        strategies=tuple(
            sorted(
                (
                    _strategy_impact(row, snapshot, usable, exit_usable, security_ids)
                    for row in strategy_rows
                ),
                key=lambda row: row.strategy_id,
            )
        ),
    )


def strategy_input_from(
    descriptor: StrategyDescriptorV1,
    requirements: StrategyEvidenceRequirementsV1,
) -> StrategyImpactInputV1:
    """Read one Strategy's declared regime parameters and session minimums.

    The regime parameters come from ``descriptor.default_parameters`` and the
    per-path minimums are the largest ``PRICE_HISTORY`` ``minimum_sessions``
    the Strategy declares, so Strategy loading and discovery stay with the
    caller and this module stays a pure comparison.
    """
    parameters = descriptor.default_parameters
    benchmark = parameters.get(REGIME_FILTER_BENCHMARK_PARAM)
    ma_length = parameters.get(REGIME_FILTER_MA_LENGTH_PARAM)
    return StrategyImpactInputV1(
        strategy_id=descriptor.strategy_id,
        display_name=descriptor.display_name,
        regime_filter_enabled=parameters.get(BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM)
        is True,
        benchmark_security_id=benchmark if isinstance(benchmark, str) else None,
        ma_length=_usable_ma_length(ma_length),
        entry_minimum_sessions=_minimum_sessions(requirements.entry),
        exit_minimum_sessions=_minimum_sessions(requirements.exit),
        entry_kinds=_kinds(requirements.entry),
        exit_kinds=_kinds(requirements.exit),
    )


def _fx_impact(
    row: SecurityCurrencyInputV1,
    base_currency: str,
    fx_sessions: int | None,
) -> SecurityFxImpactV1:
    """Set one security's own session count beside the shared FX ceiling."""
    required = _normalise_currency(row.currency) != _normalise_currency(base_currency)
    # Without the series nothing converts -- ``convert_to_base`` raises
    # ``fx_missing`` -- so an FX outage degrades every non-base security at
    # once rather than crediting it with history it cannot use.
    ceiling = (0 if fx_sessions is None else fx_sessions) if required else None
    usable = row.sessions if ceiling is None else min(row.sessions, ceiling)
    return SecurityFxImpactV1(
        security_id=row.security_id,
        currency=row.currency,
        fx_required=required,
        sessions=row.sessions,
        fx_ceiling=ceiling,
        usable_sessions=usable,
    )


def _strategy_impact(
    row: StrategyImpactInputV1,
    snapshot: MarketRegimeSnapshotV1 | None,
    usable: tuple[int, ...],
    exit_usable: tuple[int, ...],
    security_ids: frozenset[str],
) -> StrategyImpactV1:
    """Judge the benchmark for one Strategy and count its eligible securities.

    ``usable`` scopes the entry count, ``exit_usable`` the exit count; a held
    security the scan carries no evidence for has no row in either and so is
    never counted eligible.
    """
    satisfied: bool | None = None
    if row.regime_filter_enabled and snapshot is not None:
        # ``entry_signals_permitted`` fails closed on an unusable MA length, a
        # missing benchmark, a benchmark outside the run's securities, and a
        # degraded reading. Reporting those as satisfied would invert the very
        # outcome this section exists to explain.
        if (
            row.ma_length is None
            or not row.benchmark_security_id
            or row.benchmark_security_id not in security_ids
            or snapshot.is_degraded
        ):
            satisfied = False
        else:
            satisfied = snapshot.session_count >= row.ma_length
    return StrategyImpactV1(
        strategy_id=row.strategy_id,
        display_name=row.display_name,
        regime_filter_enabled=row.regime_filter_enabled,
        benchmark_security_id=row.benchmark_security_id,
        ma_length=row.ma_length,
        benchmark_satisfied=satisfied,
        entry_minimum_sessions=row.entry_minimum_sessions,
        exit_minimum_sessions=row.exit_minimum_sessions,
        entry_kinds=row.entry_kinds,
        exit_kinds=row.exit_kinds,
        eligible_entry=sum(
            1 for n in usable if n > 0 and n >= row.entry_minimum_sessions
        ),
        eligible_exit=sum(
            1 for n in exit_usable if n > 0 and n >= row.exit_minimum_sessions
        ),
    )


def _minimum_sessions(requirements: tuple[EvidenceRequirementV1, ...]) -> int:
    """Largest ``PRICE_HISTORY`` minimum one declared path asks for."""
    return max(
        (
            item.minimum_sessions
            for item in requirements
            if item.kind is EvidenceKind.PRICE_HISTORY
        ),
        default=0,
    )


def _kinds(requirements: tuple[EvidenceRequirementV1, ...]) -> tuple[str, ...]:
    """Evidence kind names one declared path asks for, ordered and unique."""
    return tuple(sorted({item.kind.value for item in requirements}))


def _normalise_currency(value: str) -> str:
    """Compare currencies the way ``currency.py`` does: trimmed and upper-cased."""
    return value.strip().upper()


def _usable_ma_length(value: object) -> int | None:
    """Return the declared MA length, or ``None`` when it is unusable."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= MIN_MA_LENGTH else None
