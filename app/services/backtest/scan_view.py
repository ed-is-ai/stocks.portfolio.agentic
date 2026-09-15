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

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence, cast

import pandas as pd

from app.core.ticker_identity import AmbiguousTickerAliasError, canonical_ticker
from app.schemas.record import StockRecord
from app.schemas.analysis_artifact import (
    CurrentAnalysisEvidenceV1,
    CurrentEvidenceGapV1,
    CurrentEvidenceSuccessV1,
)
from app.schemas.trade import Position
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalEvidenceIntegrityError,
    HistoricalPriceRepository,
)
from app.services.backtest.market_planes import (
    HistoricalMarketPlanes,
    MarketDataPolicyError,
)
from app.services.backtest.market_view import (
    PRICE_HISTORY_COLUMNS,
    _missing_calendar_sessions,
)
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    SecurityEvidenceCoverageV1,
)
from app.services.backtest.strategy_protocol import (
    PositionSummaryV1,
    PortfolioView,
)
from app.services.backtest.historical_scan_record import StageV1, TechnicalsV1, VcpV1
from app.services.backtest.trading_calendar import TradingCalendar

_CARRY_FORWARD_POLICY = "portfolio_history_carry_forward_v1"


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
class PortfolioHistoryRead:
    """One read-only, typed outcome for a held security's fallback history."""

    security_id: str
    display_ticker: str
    history: pd.DataFrame | None = None
    evidence_details: tuple[str, ...] = ()
    error_code: str | None = None

    @property
    def available(self) -> bool:
        return self.history is not None


_PORTFOLIO_RAW_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "stock_splits",
)


def read_portfolio_history(
    prices: HistoricalPriceRepository,
    ticker: str,
    aliases: Mapping[str, str],
    *,
    through: date,
    minimum_sessions: int,
    carry_forward_sessions: int = 0,
    repair_outcome: str | None = None,
) -> PortfolioHistoryRead:
    """Read existing ``portfolio:<canonical>`` evidence through ``through``.

    This boundary is deliberately read-only: it only uses the repository's
    covering-revision and active-format bounded-read APIs. Invalid or absent
    evidence becomes a stable typed outcome for recommendation preflight.
    """
    display_ticker = ticker

    def failure_details(reason: str) -> tuple[str, ...]:
        details = [f"portfolio_history: status=unavailable; reason={reason}"]
        if repair_outcome is not None:
            details.append(f"portfolio_history: repair_outcome={repair_outcome}")
        return tuple(details)

    try:
        canonical = canonical_ticker(ticker, dict(aliases))
    except (AmbiguousTickerAliasError, TypeError, ValueError):
        return PortfolioHistoryRead(
            ticker,
            display_ticker,
            error_code="identity_mismatch",
            evidence_details=failure_details("identity_mismatch"),
        )
    if (
        isinstance(minimum_sessions, bool)
        or not isinstance(minimum_sessions, int)
        or minimum_sessions < 0
    ):
        return PortfolioHistoryRead(
            canonical,
            display_ticker,
            error_code="invalid_price_history_request",
            evidence_details=failure_details("invalid_price_history_request"),
        )
    read_limit = max(1, minimum_sessions)

    security_id = f"portfolio:{canonical}"
    handle = None
    try:
        # Revision selection only needs the current bound; ``limit`` supplies
        # the strategy window after a revision is found. Requiring the
        # calendar-day lookback here would reject a valid revision whose first
        # exchange session is later than that synthetic date.
        start = through
        end = through + timedelta(days=1)
        revision = prices.covering_revision(
            security_id=security_id,
            requested_symbol=canonical,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if revision is None:
            raise EvidenceMissingError("historical evidence is missing")
        handle = prices.open_read(revision)
        metadata = handle.metadata
        if (
            metadata.security_id != security_id
            or metadata.requested_symbol != canonical
            or metadata.observed_symbol != canonical
        ):
            raise HistoricalEvidenceIntegrityError(
                "portfolio evidence identity does not match the canonical holding"
            )
        bounded = handle.bounded(
            through=through,
            limit=read_limit,
            columns=_PORTFOLIO_RAW_COLUMNS,
        )
        plane = HistoricalMarketPlanes.from_bounded_evidence(bounded)
        rows = plane.split_continuous_window_as_of(through, limit=read_limit)
        if not rows:
            raise HistoricalEvidenceIntegrityError(
                "portfolio history has no usable observations"
            )
        scale = plane.quote_unit_scale
        mic = "XLON" if canonical.upper().endswith(".L") else "XNYS"
        try:
            expected = TradingCalendar().sessions_in_range(
                mic, rows[0].session, through + timedelta(days=1)
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise MarketDataPolicyError(
                "integrity_error", "portfolio history session calendar is invalid"
            ) from exc
        if through not in expected:
            raise MarketDataPolicyError(
                "bound_violation", "portfolio history as-of is not an exchange session"
            )
        observed = {row.session for row in rows}
        if any(session not in expected for session in observed):
            raise MarketDataPolicyError(
                "integrity_error", "portfolio history contains a non-session row"
            )
        missing = tuple(session for session in expected if session not in observed)
        trailing = tuple(session for session in expected if session > rows[-1].session)
        carry_limit = min(5, max(0, carry_forward_sessions))
        if repair_outcome in {"identity_mismatch", "integrity_error"}:
            carry_limit = 0
        if missing:
            if missing == trailing and len(missing) <= carry_limit and rows:
                carried_sessions = missing
            elif rows[-1].session == through and missing != trailing:
                # Preserve an internal gap for generic preflight diagnostics;
                # a later observed row proves it is not a carryable suffix.
                carried_sessions = ()
            else:
                raise MarketDataPolicyError(
                    "missing_sessions", "portfolio history has an unsafe session gap"
                )
        else:
            carried_sessions = ()
        projected: list[tuple[date, Decimal, Decimal, Decimal, Decimal, Decimal | None]] = []
        for row in rows:
            projected.append(
                (
                    row.session,
                    row.open * scale,
                    row.high * scale,
                    row.low * scale,
                    row.close * scale,
                    row.volume,
                )
            )
        for session in carried_sessions:
            _, open_value, high, low, close, _ = projected[-1]
            projected.append((session, open_value, high, low, close, None))
        frame = pd.DataFrame(
            {
                "open": [row[1] for row in projected],
                "high": [row[2] for row in projected],
                "low": [row[3] for row in projected],
                "close": [row[4] for row in projected],
                "volume": [row[5] for row in projected],
            },
            index=pd.Index(
                [row[0] for row in projected], dtype=object, name="session"
            ),
            columns=PRICE_HISTORY_COLUMNS,
        )
        details = (
            "portfolio_history: "
            f"revision={metadata.data_revision}; security_id={metadata.security_id}; "
            f"requested_symbol={metadata.requested_symbol}; "
            f"observed_symbol={metadata.observed_symbol}; "
            f"bounds={metadata.start}..{metadata.end}; "
            f"provider={metadata.provider}; quote_unit={metadata.quote_unit}; "
            f"quote_unit_scale={metadata.quote_unit_scale}; "
            f"alias_revision={metadata.alias_revision}; "
            f"provider_version={metadata.provider_version}; "
            f"request_contract_version={metadata.request_contract_version}; "
            f"response_metadata_digest={metadata.response_metadata_digest}",
        )
        if repair_outcome is not None:
            details += (f"portfolio_history: repair_outcome={repair_outcome}",)
        if carried_sessions:
            details += (
                "portfolio_history: "
                f"carry_forward_policy={_CARRY_FORWARD_POLICY}; "
                f"source_revision={metadata.data_revision}; "
                f"last_good_session={rows[-1].session.isoformat()}; "
                "carried_sessions="
                f"{','.join(session.isoformat() for session in carried_sessions)}",
            )
        return PortfolioHistoryRead(
            canonical, display_ticker, frame, details
        )
    except EvidenceMissingError:
        return PortfolioHistoryRead(
            canonical,
            display_ticker,
            error_code="evidence_missing",
            evidence_details=failure_details("evidence_missing"),
        )
    except HistoricalEvidenceIntegrityError:
        return PortfolioHistoryRead(
            canonical,
            display_ticker,
            error_code="integrity_error",
            evidence_details=failure_details("integrity_error"),
        )
    except MarketDataPolicyError as exc:
        return PortfolioHistoryRead(
            canonical,
            display_ticker,
            error_code=exc.code,
            evidence_details=failure_details(exc.code),
        )
    except Exception:
        return PortfolioHistoryRead(
            canonical,
            display_ticker,
            error_code="integrity_error",
            evidence_details=failure_details("integrity_error"),
        )
    finally:
        if handle is not None:
            handle.close()


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
    _display_tickers: Mapping[str, str] = field(default_factory=dict)
    _evidence_details: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    _portfolio_history_ids: frozenset[str] = frozenset()
    _portfolio_history_attempted: frozenset[str] = frozenset()

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
        object.__setattr__(
            self,
            "_display_tickers",
            MappingProxyType(dict(self._display_tickers or {})),
        )
        object.__setattr__(
            self,
            "_evidence_details",
            MappingProxyType(
                {
                    security_id: tuple(details)
                    for security_id, details in (self._evidence_details or {}).items()
                }
            ),
        )
        object.__setattr__(self, "_portfolio_history_ids", frozenset(self._portfolio_history_ids))
        object.__setattr__(
            self,
            "_portfolio_history_attempted",
            frozenset(self._portfolio_history_attempted),
        )

    def with_display_tickers(
        self, display_tickers: Mapping[str, str]
    ) -> "CurrentScanMarketView":
        """Return this view with additional portfolio display identities."""
        return replace(
            self,
            _display_tickers=dict(self._display_tickers) | dict(display_tickers),
        )

    def with_portfolio_history(
        self, outcomes: Iterable[PortfolioHistoryRead]
    ) -> "CurrentScanMarketView":
        """Attach fallback price history without changing scan authority."""
        histories = dict(self._histories)
        details = dict(self._evidence_details)
        display_tickers = dict(self._display_tickers)
        valid: set[str] = set(self._portfolio_history_ids)
        attempted = set(self._portfolio_history_attempted)
        for outcome in outcomes:
            if outcome.security_id in self.selected_universe:
                continue
            attempted.add(outcome.security_id)
            if outcome.display_ticker:
                display_tickers.setdefault(outcome.security_id, outcome.display_ticker)
            if outcome.history is not None:
                histories[outcome.security_id] = outcome.history
                valid.add(outcome.security_id)
            if outcome.evidence_details:
                details[outcome.security_id] = tuple(outcome.evidence_details)
        return replace(
            self,
            _histories=histories,
            _evidence_details=details,
            _display_tickers=display_tickers,
            _portfolio_history_ids=frozenset(valid),
            _portfolio_history_attempted=frozenset(attempted),
        )

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
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
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise MarketDataPolicyError(
                "invalid_price_history_request", "limit must be a positive integer."
            )
        try:
            requested_columns = (
                PRICE_HISTORY_COLUMNS if columns is None else tuple(columns)
            )
            valid_column_set = len(set(requested_columns)) == len(requested_columns)
        except (TypeError, ValueError):
            requested_columns = ()
            valid_column_set = False
        if (
            not requested_columns
            or not valid_column_set
            or any(column not in PRICE_HISTORY_COLUMNS for column in requested_columns)
        ):
            raise MarketDataPolicyError(
                "invalid_price_history_request",
                "columns must be a non-empty subset of the canonical price columns.",
            )
        requested_columns = tuple(
            column for column in PRICE_HISTORY_COLUMNS if column in requested_columns
        )
        frame = self._histories.get(security_id)
        if frame is None:
            return pd.DataFrame(
                columns=requested_columns,
                index=pd.Index([], dtype=object, name="session"),
            )
        if limit is not None:
            frame = frame.iloc[-limit:]
        return frame.loc[:, requested_columns]

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
        display_ticker = self._display_tickers.get(security_id, security_id)
        frame = self._histories.get(security_id)
        if frame is None or frame.empty:
            return SecurityEvidenceCoverageV1(
                security_id=security_id,
                display_ticker=display_ticker,
                evidence_details=self._evidence_details.get(security_id, ()),
            )
        session_dates = tuple(_session_date(session) for session in frame.index)
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
            display_ticker=display_ticker,
            kinds=frozenset(kinds),
            sessions=int(len(frame.index)),
            columns=tuple(
                str(column)
                for column in frame.columns
                if bool(frame[column].notna().all())
            ),
            session_dates=session_dates,
            missing_sessions=_missing_calendar_sessions(
                security_id,
                session_dates,
                self.as_of_session,
                display_ticker,
            ),
            evidence_details=self._evidence_details.get(security_id, ()),
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


def _session_date(value: object) -> date:
    """Normalize pandas/date index values without changing the evidence."""
    if isinstance(value, date) and not hasattr(value, "date"):
        return value
    if isinstance(value, date):
        return value.date()  # type: ignore[union-attr]
    return pd.Timestamp(value).date()


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
    evidence_details: dict[str, tuple[str, ...]] = {}
    evidence_quarantined: set[str] = set()
    evidence_seen: set[str] = set()
    if current_evidence is not None and current_evidence.as_of_session == session:
        for item in current_evidence.entries:
            if not isinstance(item, (CurrentEvidenceGapV1, CurrentEvidenceSuccessV1)):
                continue
            try:
                security_id = canonical_ticker(item.security_id, aliases)
            except AmbiguousTickerAliasError:
                unresolved.append(item.security_id)
                continue
            if security_id in evidence_quarantined:
                unresolved.append(item.security_id)
                continue
            if security_id in evidence_seen:
                unresolved.append(item.security_id)
                evidence_quarantined.add(security_id)
                scan_results.pop(security_id, None)
                evidence_details.pop(security_id, None)
                continue
            evidence_seen.add(security_id)
            if isinstance(item, CurrentEvidenceGapV1):
                evidence_details[security_id] = (f"{item.reason}: {item.detail}",)
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
        _display_tickers={
            security_id: ticker
            for security_id, ticker in resolved_ticker.items()
            if security_id in histories
        },
        _evidence_details=evidence_details,
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
