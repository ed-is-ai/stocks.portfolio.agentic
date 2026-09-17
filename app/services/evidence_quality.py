"""Compose the read-only Data Quality view (#639).

Pure composition on top of three already-built services: the evidence
census (#636), the run funnel (#635) and the shared-inputs report (#638).
Nothing here computes a fault, fetches, or writes -- every figure is read
from the published artifact, the run log, the persisted regime snapshot,
the FX revision metadata and the pinned trading calendar, each fail-soft.

``build_data_quality_view`` is pure so the composition is testable without
IO; ``load_data_quality_view`` is the thin gatherer the route calls.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
import logging

from pydantic import BaseModel, ConfigDict

from app.core.config import ANALYSIS_JSON, SKILLS_DIR
from app.core.ticker_identity import (
    AmbiguousTickerAliasError,
    canonical_ticker,
    load_aliases,
)
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    read_analysis_artifact,
)
from app.schemas.evidence_census import (
    FAULT_DROPPED,
    FAULT_GAPPED,
    FAULT_MISSING_FRAGMENT,
    FAULT_THIN,
    EvidenceCensusV1,
)
from app.schemas.market_regime import MarketRegimeSnapshotV1
from app.schemas.record import StockRecord
from app.services.backtest.historical_price_evidence import (
    FX_PAIR,
    FX_SERIES_SECURITY_ID,
)
from app.services.backtest.scan_view import (
    CurrentScanMarketView,
    PortfolioHistoryRead,
    build_scan_market_view,
    read_portfolio_history,
)
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.strategy_evidence import StrategyEvidenceRequirementsV1
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.evidence_census import build_evidence_census
from app.services.evidence_funnel import (
    EvidenceFunnelV1,
    build_evidence_funnel,
    find_run_log_row,
)
from app.services.evidence_shared_inputs import (
    SecurityCurrencyInputV1,
    SecurityFxImpactV1,
    SharedInputsReportV1,
    StrategyImpactInputV1,
    build_shared_inputs_report,
)
from app.services.portfolio_recommendation_service import (
    _declared_requirements,
    _load_strategy_instance,
)

logger = logging.getLogger(__name__)

#: Base currency of the ledger, as ``build_portfolio_view`` pins it.
BASE_CURRENCY = "GBP"

#: Cap on the portfolio-namespace session read: a trading year is the
#: widest window any declared Strategy minimum sits inside, so nothing on
#: this screen is truncated by it.
_PORTFOLIO_READ_SESSIONS = 260

#: MIC whose sessions stand in for "a full trailing year of trading".
CALENDAR_MIC = "XNYS"

#: Severity rank per fault, worst last. The census already orders each
#: row's ``faults`` by this same escalation, so the rank of a row is the
#: rank of its last fault -- nothing is reclassified here.
FAULT_SEVERITY_RANK: Mapping[str, int] = {
    FAULT_THIN: 1,
    FAULT_GAPPED: 2,
    FAULT_MISSING_FRAGMENT: 3,
    FAULT_DROPPED: 4,
}


class DataQualityViewV1(BaseModel):
    """Everything the Data Quality tab renders, already reconciled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool = False
    unavailable_reason: str = ""
    as_of_session: date | None = None
    census: EvidenceCensusV1 | None = None
    funnel: EvidenceFunnelV1 = EvidenceFunnelV1(available=False)
    shared_inputs: SharedInputsReportV1 | None = None
    #: The shared-inputs FX impact keyed by security id, so a census row
    #: can show its own FX ceiling without the template re-deriving one.
    fx_by_security: Mapping[str, SecurityFxImpactV1] = {}
    #: Discovered Strategies whose runtime or declaration could not be read.
    strategies_unreadable: int = 0
    #: Holdings the exit-side figures are scoped to -- the exit path is
    #: only ever evaluated for a security actually held.
    holdings_count: int = 0
    #: Strategy discovery itself failed, so the roster below is a degraded
    #: read rather than an empty one -- the two must never read alike.
    strategies_unavailable: bool = False


def build_data_quality_view(
    view: CurrentScanMarketView,
    *,
    current_evidence: CurrentAnalysisEvidenceV1 | None = None,
    unresolved: Iterable[str] = (),
    holdings: Iterable[str] = (),
    aliases: Mapping[str, str] | None = None,
    run_log_row: Mapping[str, str] | None = None,
    currencies: Mapping[str, str] | None = None,
    strategy_requirements: Mapping[str, StrategyEvidenceRequirementsV1] | None = None,
    strategy_inputs: Iterable[StrategyImpactInputV1] = (),
    fx_sessions: int | None = None,
    calendar_digest: str = "",
    calendar_expected_sessions: int | None = None,
    snapshot: MarketRegimeSnapshotV1 | None = None,
    strategies_unreadable: int = 0,
    strategies_unavailable: bool = False,
    portfolio_reads: Iterable[PortfolioHistoryRead] = (),
) -> DataQualityViewV1:
    """Compose the census, funnel and shared-inputs report for one scan.

    Pure: every input is a value already read elsewhere. The shared-inputs
    report is handed the run's whole selected universe as its ``securities``
    argument, because it derives the regime benchmark's in-universe check
    from exactly that set; ``holdings`` narrows the exit-side figures only.
    """
    alias_map = dict(aliases or {})
    holding_ids = frozenset(
        _canonical(ticker, alias_map) for ticker in holdings if ticker
    )
    currency_map = dict(currencies or {})
    # ``portfolio_reads`` are bounded by the holdings (single digits), and
    # each is a read of already-persisted ``portfolio:<symbol>`` evidence --
    # no provider call and no write.
    census = build_evidence_census(
        view,
        current_evidence=current_evidence,
        unresolved=unresolved,
        holdings=holding_ids,
        portfolio_reads=portfolio_reads,
        strategies=dict(strategy_requirements or {}),
        aliases=aliases,
        currencies=currency_map,
    )
    funnel = build_evidence_funnel(
        current_evidence, run_log_row, len(view.selected_universe)
    )
    shared_inputs = build_shared_inputs_report(
        base_currency=BASE_CURRENCY,
        securities=tuple(
            SecurityCurrencyInputV1(
                security_id=security_id,
                currency=currency_map.get(security_id) or "USD",
                sessions=view.evidence_coverage(security_id).sessions,
            )
            for security_id in view.selected_universe
        ),
        strategies=strategy_inputs,
        fx_sessions=fx_sessions,
        calendar_digest=calendar_digest,
        calendar_expected_sessions=calendar_expected_sessions,
        snapshot=snapshot,
        holding_ids=holding_ids,
    )
    return DataQualityViewV1(
        available=True,
        as_of_session=census.as_of_session,
        census=census,
        funnel=funnel,
        shared_inputs=shared_inputs,
        fx_by_security={row.security_id: row for row in shared_inputs.securities},
        holdings_count=len(holding_ids),
        strategies_unreadable=strategies_unreadable,
        strategies_unavailable=strategies_unavailable,
    )


def load_data_quality_view(portfolio_id: int | None = None) -> DataQualityViewV1:
    """Gather every persisted input fail-soft and compose the view.

    A missing artifact, alias file, run log row, regime snapshot, FX
    revision, calendar or Strategy runtime degrades only its own section;
    the view itself is returned unavailable only when there is no published
    scan to account for at all.

    ``portfolio_id`` scopes the holdings the census must account for
    (``None`` aggregates every portfolio, as the rest of the app does).
    """
    artifact = read_analysis_artifact(ANALYSIS_JSON)
    records = _records(artifact.records if artifact is not None else None)
    if not records:
        return DataQualityViewV1(
            unavailable_reason="No published scan artifact to account for."
        )
    current_evidence = artifact.current_evidence if artifact is not None else None
    aliases = _aliases()
    try:
        view, unresolved = build_scan_market_view(
            records,
            aliases,
            as_of_session=(
                current_evidence.as_of_session if current_evidence else None
            ),
            current_evidence=current_evidence,
        )
    except Exception:
        logger.exception("Data Quality could not build a scan view")
        return DataQualityViewV1(
            unavailable_reason="Scan artifact carries no usable price evidence."
        )
    if not view.selected_universe:
        return DataQualityViewV1(
            unavailable_reason="Scan artifact carries no usable price evidence."
        )
    requirements, inputs, unreadable, discovered = _strategies(view)
    holdings = _holdings(portfolio_id)
    return build_data_quality_view(
        view,
        current_evidence=current_evidence,
        unresolved=unresolved,
        holdings=holdings,
        portfolio_reads=_portfolio_reads(holdings, aliases, view.as_of_session),
        aliases=aliases,
        run_log_row=(
            find_run_log_row(current_evidence.run_id) if current_evidence else None
        ),
        currencies=_currencies(records, aliases),
        strategy_requirements=requirements,
        strategy_inputs=inputs,
        fx_sessions=_fx_sessions(view.as_of_session),
        calendar_digest=_calendar_digest(),
        calendar_expected_sessions=_expected_sessions(view.as_of_session),
        snapshot=_snapshot(),
        strategies_unreadable=unreadable,
        strategies_unavailable=not discovered,
    )


def _aliases() -> dict[str, str]:
    """Return the alias map, empty when the alias file is unreadable.

    ``load_aliases`` raises by design on a present-but-corrupt file with no
    cached last-good map. This screen exists to report broken data, so a
    broken alias file degrades identity resolution here, never the page.
    """
    try:
        return dict(load_aliases())
    except Exception:
        logger.exception("Alias map unreadable for Data Quality")
        return {}


def _holdings(portfolio_id: int | None) -> tuple[str, ...]:
    """Return the open holdings' tickers, empty when unreadable.

    Bare tickers only: ``build_evidence_census`` canonicalises them itself
    and performs no repository read for them.
    """
    from app.api.dependencies import get_trader_service

    try:
        return tuple(
            position.ticker
            for position in get_trader_service().get_portfolio(
                portfolio_id=portfolio_id
            )
            if position.shares > 0
        )
    except Exception:
        logger.exception("Holdings unreadable for Data Quality")
        return ()


def _portfolio_reads(
    holdings: Iterable[str],
    aliases: Mapping[str, str],
    as_of_session: date,
) -> tuple[PortfolioHistoryRead, ...]:
    """Read each holding's persisted ``portfolio:<symbol>`` history.

    Bounded by the holdings, which are single digits, and read-only: every
    call goes through the repository's covering-revision/bounded-read pair,
    never a provider and never a repair. An unreadable repository degrades
    to no reads at all, so the portfolio-namespace column reads "not read"
    rather than a false zero.
    """
    from app.api.dependencies import get_read_only_historical_price_repository

    tickers = tuple(dict.fromkeys(ticker for ticker in holdings if ticker))
    if not tickers:
        return ()
    try:
        repo = get_read_only_historical_price_repository()
    except Exception:
        logger.exception("Portfolio evidence unreadable for Data Quality")
        return ()
    reads: list[PortfolioHistoryRead] = []
    for ticker in tickers:
        try:
            reads.append(
                read_portfolio_history(
                    repo,
                    ticker,
                    dict(aliases),
                    through=as_of_session,
                    # The longest window any row reports; the read is
                    # bounded by what exists, so this only sets the cap.
                    minimum_sessions=_PORTFOLIO_READ_SESSIONS,
                )
            )
        except Exception:
            logger.exception("Portfolio evidence unreadable for %s", ticker)
    return tuple(reads)


def _records(rows: list[dict] | None) -> list[StockRecord]:
    """Validate the artifact's records, skipping any malformed row."""
    records: list[StockRecord] = []
    for row in rows or []:
        try:
            records.append(StockRecord.model_validate(row))
        except Exception:
            continue
    return records


def _currencies(
    records: Iterable[StockRecord], aliases: Mapping[str, str]
) -> dict[str, str]:
    """Map canonical security id to the record's own evidence currency."""
    currencies: dict[str, str] = {}
    for record in records:
        try:
            security_id = canonical_ticker(record.ticker, dict(aliases))
        except (AmbiguousTickerAliasError, TypeError, ValueError):
            continue
        currencies[security_id] = record.currency or "USD"
    return currencies


def _canonical(ticker: str, aliases: Mapping[str, str]) -> str:
    """Canonicalise one ticker the way the census does, raw id on failure."""
    try:
        return canonical_ticker(ticker, dict(aliases))
    except (AmbiguousTickerAliasError, TypeError, ValueError):
        return ticker


def _strategies(
    view: CurrentScanMarketView,
) -> tuple[
    dict[str, StrategyEvidenceRequirementsV1],
    tuple[StrategyImpactInputV1, ...],
    int,
    bool,
]:
    """Read each discovered Strategy's declared requirements, fail-soft.

    A Strategy whose runtime will not load or whose declaration is missing
    or malformed is skipped and counted, never raised: the roster is a
    report, not a gate. The trailing flag says whether discovery itself
    succeeded -- "nothing discovered" and "discovery failed" are different
    claims and must not render alike.
    """
    from app.services.evidence_shared_inputs import strategy_input_from

    try:
        descriptors = _descriptors()
    except Exception:
        logger.exception("Strategy discovery failed for Data Quality")
        return {}, (), 0, False
    requirements: dict[str, StrategyEvidenceRequirementsV1] = {}
    inputs: list[StrategyImpactInputV1] = []
    unreadable = 0
    for descriptor in descriptors:
        try:
            strategy = _load_strategy_instance(SKILLS_DIR / descriptor.runtime_path)
            parameters = dict(descriptor.default_parameters) | dict(
                descriptor.bind_universe(view.selected_universe)
            )
            declared = _declared_requirements(strategy, parameters)
            if not isinstance(declared, StrategyEvidenceRequirementsV1):
                raise ValueError("Strategy declares no evidence requirements")
        except Exception:
            logger.exception(
                "Strategy %s unreadable for Data Quality", descriptor.strategy_id
            )
            unreadable += 1
            continue
        requirements[descriptor.strategy_id] = declared
        inputs.append(strategy_input_from(descriptor, declared))
    return requirements, tuple(inputs), unreadable, True


def _descriptors() -> tuple[StrategyDescriptorV1, ...]:
    """Return every discoverable Strategy descriptor."""
    from app.api.dependencies import get_strategy_assignment_service

    return get_strategy_assignment_service().list_choices()


def _snapshot() -> MarketRegimeSnapshotV1 | None:
    """Return the persisted regime snapshot, or None if unreadable."""
    from app.agents.scanner.market_regime_snapshot import load_market_regime

    try:
        return load_market_regime()
    except Exception:
        logger.exception("Regime snapshot unreadable for Data Quality")
        return None


def _fx_sessions(as_of_session: date) -> int | None:
    """Return the FX series' stored observation count, or None.

    Reads the revision's metadata only -- the observation count is already
    stored there, so no price rows are materialized for a figure that is
    one integer.
    """
    from app.api.dependencies import get_read_only_historical_price_repository

    handle = None
    try:
        repo = get_read_only_historical_price_repository()
        revision = repo.covering_revision(
            security_id=FX_SERIES_SECURITY_ID,
            requested_symbol=FX_PAIR,
            start=as_of_session.isoformat(),
            end=as_of_session.isoformat(),
        )
        if revision is None:
            return None
        handle = repo.open_read(revision)
        return int(handle.metadata.observation_count)
    except Exception:
        logger.exception("FX session count unreadable for Data Quality")
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                logger.exception("FX evidence handle would not close")


def _calendar_digest() -> str:
    """Return the pinned session-table digest, empty when unreadable."""
    try:
        return TradingCalendar().session_table_digest()
    except Exception:
        logger.exception("Calendar digest unreadable for Data Quality")
        return ""


def _expected_sessions(as_of_session: date) -> int | None:
    """Return the trailing-year XNYS session count ending at the census date.

    A trailing year on ``XNYS`` is the yardstick because it is the widest
    window any discovered Strategy's declared minimum sits inside, and the
    scan's own evidence windows are bounded the same way; it is a reference
    length, not a per-security expectation.
    """
    try:
        return len(
            TradingCalendar().sessions_in_range(
                CALENDAR_MIC,
                as_of_session - timedelta(days=365),
                as_of_session + timedelta(days=1),
            )
        )
    except Exception:
        logger.exception("Expected session count unreadable for Data Quality")
        return None
