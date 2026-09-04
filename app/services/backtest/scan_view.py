"""Current-scan ``MarketViewV1``/``PortfolioView`` adapter (#441).

Bridges the published analysis artifact's per-ticker scan records and the
portfolio's current holdings onto the same read seam historical backtests
hand a Strategy (``MarketViewV1``/``PortfolioView``), so the assigned
Strategy's own runtime code can evaluate *this* account without the host
re-implementing a single rule.

Honesty is the design constraint: only what the current artifact evidences
is exposed. ``price_history`` mirrors ``market_view.MarketView``'s
convention exactly (``PRICE_HISTORY_COLUMNS``, ``Decimal`` object-dtype
values, a ``date``-typed object index named ``session``, oldest-first, last
row == ``as_of_session``); ``scan_result`` projects only the security id
and the single evidenced market session — ``stage``/``vcp``/``technicals``
are deliberately absent, never fabricated. Tickers resolve through
``canonical_ticker``; anything unresolvable is surfaced to the caller,
never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Mapping, cast

import pandas as pd

from app.core.ticker_identity import AmbiguousTickerAliasError, canonical_ticker
from app.schemas.record import StockRecord
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceSuccessV1,
)
from app.schemas.trade import Position
from app.services.backtest.market_view import PRICE_HISTORY_COLUMNS
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    SecurityEvidenceCoverageV1,
)
from app.services.backtest.strategy_protocol import (
    PositionSummaryV1,
    PortfolioView,
)
from app.services.backtest.historical_scan_record import StageV1, TechnicalsV1, VcpV1


@dataclass(frozen=True)
class CurrentScanRecordView:
    """Honest projection of one current scan record for ``scan_result``.

    Only what the current artifact evidences is exposed: the security id
    and the single market session the record was produced for. ``stage``,
    ``vcp``, and ``technicals`` are deliberately absent (class-level
    ``None`` sentinels, not fields) — the current artifact does not
    evidence them, and fabricating them would invent historical
    provenance. Runtimes read them defensively via ``getattr``.
    """

    security_id: str
    as_of_session_date: date
    technicals: TechnicalsV1 | None = None
    stage: StageV1 | None = None
    vcp: VcpV1 | None = None


@dataclass(frozen=True)
class CurrentScanMarketView:
    """``MarketViewV1`` over one published scan artifact's evidence.

    Unlike a backtest Run's universe, an empty ``selected_universe`` is a
    meaningful 'no usable evidence' state here (the caller fails safe), so
    canonicalization sorts/deduplicates without rejecting empty input.
    """

    as_of_session: date
    selected_universe: tuple[str, ...]
    _histories: Mapping[str, pd.DataFrame]
    _scan_results: Mapping[str, CurrentScanRecordView] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Detach from caller-supplied collections so later mutation can
        # never reach this view, matching MarketView/PortfolioView.
        object.__setattr__(
            self, "selected_universe", tuple(sorted(set(self.selected_universe)))
        )
        object.__setattr__(self, "_histories", MappingProxyType(dict(self._histories)))
        object.__setattr__(
            self,
            "_scan_results",
            MappingProxyType(dict(self._scan_results or {})),
        )

    def price_history(self, security_id: str) -> pd.DataFrame:
        """Return OHLCV history through ``as_of_session``, oldest first.

        Columns are exactly ``PRICE_HISTORY_COLUMNS`` with ``Decimal``
        object-dtype values, indexed by plain ``date`` objects named
        ``session``; the last row is ``as_of_session``. Any security
        without evidenced history — unknown, or a held position absent
        from the scan — answers with an empty frame of the right shape,
        per the ``MarketViewV1`` contract ("unknown security → empty
        DataFrame, not error"): the fail-safe Hold rule depends on a
        runtime being able to query a held position the scan never saw.
        """
        frame = self._histories.get(security_id)
        if frame is None:
            return _empty_price_history()
        return frame

    def scan_result(self, security_id: str) -> CurrentScanRecordView | None:
        """Return the honest scan projection, or ``None`` outside the universe."""
        if security_id not in self.selected_universe:
            return None
        return self._scan_results.get(
            security_id,
            CurrentScanRecordView(
                security_id=security_id, as_of_session_date=self.as_of_session
            ),
        )

    @property
    def evidence_capabilities(self) -> frozenset[EvidenceKind]:
        """Declare OHLCV only — this view evidences no scan fragments.

        ``scan_result`` deliberately projects no ``stage``/``vcp``/
        ``technicals`` (see :class:`CurrentScanRecordView`), so declaring
        those kinds would claim evidence the published artifact does not
        carry. A Strategy requiring them is reported incompatible here
        rather than silently evaluated against ``None``.
        """
        return frozenset(EvidenceKind)

    def evidence_coverage(self, security_id: str) -> SecurityEvidenceCoverageV1:
        """Return ``security_id``'s real bounded-history coverage.

        Never raises: a security with no evidenced history (unknown, or a
        holding the scan never saw) answers with zero sessions and no
        kinds, which the generic preflight reads as degraded rather than
        as a failure. An empty id answers the same way rather than
        tripping the model's own non-empty-id validator.
        """
        if not security_id:
            return _NO_COVERAGE
        frame = self._histories.get(security_id)
        if frame is None or frame.empty:
            return SecurityEvidenceCoverageV1(security_id=security_id)
        kinds = {EvidenceKind.PRICE_HISTORY}
        scan = self._scan_results.get(security_id)
        if scan is not None:
            if scan.technicals is not None:
                kinds.add(EvidenceKind.SCAN_TECHNICALS)
            if scan.stage is not None:
                kinds.add(EvidenceKind.SCAN_STAGE)
            if scan.vcp is not None:
                kinds.add(EvidenceKind.SCAN_VCP)
        return SecurityEvidenceCoverageV1(
            security_id=security_id,
            kinds=frozenset(kinds),
            sessions=int(len(frame.index)),
            columns=tuple(str(column) for column in frame.columns),
        )


#: The zero-coverage answer for an unusable security id — evidence
#: coverage is a diagnostic, so it always answers with a value.
_NO_COVERAGE = SecurityEvidenceCoverageV1(security_id="unknown")


def _empty_price_history() -> pd.DataFrame:
    """Return the empty frame shape every ``price_history`` call guarantees."""
    return pd.DataFrame(
        columns=list(PRICE_HISTORY_COLUMNS),
        index=pd.Index([], dtype=object, name="session"),
    )


def _history_frame(record: StockRecord) -> pd.DataFrame:
    """Build one security's oldest-first ``Decimal`` OHLCV frame.

    The artifact stores daily bars newest-first (``StockScan.ohlcv_history``);
    they are reversed here so the view's convention matches the historical
    ``MarketView`` exactly. Malformed rows — missing keys, non-numeric or
    non-finite values, out-of-order or duplicate sessions — raise
    ``ValueError`` so the caller's fail-safe path renders an alert instead
    of silently inverting signal comparisons.
    """
    rows = list(record.ohlcv_history)
    rows.reverse()
    if not rows:
        return _empty_price_history()
    sessions: list[date] = []
    columns: dict[str, list[Decimal]] = {name: [] for name in PRICE_HISTORY_COLUMNS}
    for row in rows:
        if "date" not in row or any(name not in row for name in PRICE_HISTORY_COLUMNS):
            raise ValueError(
                f"scan record {record.ticker!r} has an OHLCV bar missing "
                "date or price columns"
            )
        try:
            session = date.fromisoformat(str(row["date"]))
            values = {name: Decimal(str(row[name])) for name in PRICE_HISTORY_COLUMNS}
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError(
                f"scan record {record.ticker!r} has a malformed OHLCV bar: {exc}"
            ) from exc
        if any(not value.is_finite() for value in values.values()):
            raise ValueError(
                f"scan record {record.ticker!r} has a non-finite OHLCV value"
            )
        if sessions and session <= sessions[-1]:
            raise ValueError(
                f"scan record {record.ticker!r} has out-of-order or duplicate "
                "OHLCV sessions"
            )
        sessions.append(session)
        for name in PRICE_HISTORY_COLUMNS:
            columns[name].append(values[name])
    return pd.DataFrame(
        columns,
        index=pd.Index(sessions, dtype=object, name="session"),
        columns=list(PRICE_HISTORY_COLUMNS),
    )


def build_scan_market_view(
    records: list[StockRecord],
    aliases: dict[str, str],
    as_of_session: date | None = None,
    current_evidence: CurrentAnalysisEvidenceV1 | None = None,
) -> tuple[CurrentScanMarketView, tuple[str, ...]]:
    """Build the current-scan market view from published scan records.

    Returns ``(view, unresolved)``. Each record's ticker resolves through
    ``canonical_ticker``; an ambiguous alias is surfaced in ``unresolved``
    rather than dropped. ``as_of_session`` is the single evidenced market
    session: the explicit argument when given, otherwise the latest session
    across the records. A security whose latest session differs from it
    carries stale evidence — it is excluded from the universe and surfaced
    in ``unresolved`` so the caller's fail-safe Hold rule handles it, never
    silently mixing sessions into one view.
    """
    resolved: dict[str, pd.DataFrame] = {}
    resolved_ticker: dict[str, str] = {}
    quarantined: set[str] = set()
    unresolved: list[str] = []
    for record in records:
        try:
            security_id = canonical_ticker(record.ticker, aliases)
        except AmbiguousTickerAliasError:
            unresolved.append(record.ticker)
            continue
        if security_id in resolved:
            # Two records canonicalizing to one id: quarantine both, and
            # surface both original spellings -- the first ticker's history
            # is discarded here too, so its raw string must not silently
            # disappear from the caller's diagnostics.
            unresolved.append(resolved_ticker.pop(security_id))
            unresolved.append(record.ticker)
            resolved.pop(security_id, None)
            quarantined.add(security_id)
            continue
        if security_id in quarantined:
            unresolved.append(record.ticker)
            continue
        resolved[security_id] = _history_frame(record)
        resolved_ticker[security_id] = record.ticker

    session = as_of_session
    if session is None:
        latest = [
            cast(date, frame.index[-1])
            for frame in resolved.values()
            if not frame.empty
        ]
        if not latest:
            raise ValueError(
                "no scan record carries OHLCV history; cannot derive as_of_session"
            )
        session = max(latest)

    universe: list[str] = []
    histories: dict[str, pd.DataFrame] = {}
    for security_id, frame in resolved.items():
        latest_session = None if frame.empty else cast(date, frame.index[-1])
        if latest_session is None or latest_session == session:
            universe.append(security_id)
            histories[security_id] = frame
        else:
            unresolved.append(security_id)
    scan_results: dict[str, CurrentScanRecordView] = {}
    evidence_quarantined: set[str] = set()
    if current_evidence is not None and current_evidence.as_of_session == session:
        for item in current_evidence.entries:
            if not isinstance(item, CurrentEvidenceSuccessV1):
                continue
            try:
                security_id = canonical_ticker(item.security_id, aliases)
            except AmbiguousTickerAliasError:
                unresolved.append(item.security_id)
                continue
            if (
                security_id not in histories
                or security_id in scan_results
                or security_id in evidence_quarantined
            ):
                unresolved.append(item.security_id)
                scan_results.pop(security_id, None)
                evidence_quarantined.add(security_id)
                continue
            results = {
                fragment.detector: fragment.result for fragment in item.fragments
            }
            scan_results[security_id] = CurrentScanRecordView(
                security_id=security_id,
                as_of_session_date=session,
                technicals=results["technical_indicators_v1"].technicals,  # type: ignore[union-attr]
                stage=results["weinstein_stage_v1"].stage,  # type: ignore[union-attr]
                vcp=results["vcp_v1"].vcp,  # type: ignore[union-attr]
            )
    view = CurrentScanMarketView(
        as_of_session=session,
        selected_universe=tuple(universe),
        _histories=histories,
        _scan_results=scan_results,
    )
    return view, tuple(unresolved)


def build_portfolio_view(
    positions: Iterable[Position],
    cash_balance: float | None,
    as_of_session: date,
) -> PortfolioView:
    """Build a ``PortfolioView`` from the portfolio's current holdings.

    ``positions`` tickers must already be canonical security ids (the
    caller canonicalizes against the same alias map the scan view used).
    Positions with non-positive shares or cost are skipped — the protocol
    requires ``quantity >= 0`` and ``average_cost > 0`` — and cash of
    ``None`` reads as zero. Base currency is GBP (the ledger's currency)
    and no volatility observations are fabricated.
    """
    summaries = [
        PositionSummaryV1(
            security_id=position.ticker,
            quantity=Decimal(str(position.shares)),
            average_cost=Decimal(str(position.avg_cost)),
        )
        for position in positions
        if position.shares > 0 and position.avg_cost > 0
    ]
    # ``PortfolioView.cash`` requires >= 0; an overdrawn ledger reads as zero
    # available cash for sizing purposes rather than failing the evaluation.
    cash = Decimal(str(cash_balance or 0))
    if not cash.is_finite() or cash < 0:
        cash = Decimal(0)
    return PortfolioView(
        as_of_session=as_of_session,
        base_currency="GBP",
        cash=cash,
        positions=summaries,
        volatility_observations=(),
    )
