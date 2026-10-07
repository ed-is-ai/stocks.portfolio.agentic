"""Deterministic long-only Weinstein Stage 2 breakout backtest Strategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from app.services.backtest.regime_filter import entry_signals_permitted
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_explanation import (
    ComparisonOperator,
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
)
from app.services.backtest.strategy_protocol import (
    BaseCurrencyCloseHistoryViewV1,
    MarketViewV1,
    PortfolioView,
    Signal,
    SignalSide,
    StopLevelV1,
    StrategyParameters,
)

STRATEGY_ID = "rtly-backtest-weinstein"
STRATEGY_API_VERSION = 1
_ENTRY_RULE = "weinstein_stage2_breakout_v1"
_EXIT_RULE = "weinstein_stage_exit_v1"
_UPGRADE_EXIT_RULE = "weinstein_upgrade_exit_v1"


UNIVERSE_PARAMETER = "selected_securities"


def _universe(parameters: StrategyParameters) -> tuple[str, ...]:
    """Return the host-bound selected universe as a canonical ID tuple.

    The host injects an already sorted, deduplicated tuple; re-deriving it
    here keeps iteration deterministic for any caller and makes a
    malformed or empty universe fail closed with no signals.
    """
    raw = parameters.get(UNIVERSE_PARAMETER)
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _decimals(values: Any) -> list[Decimal] | None:
    result = [_decimal(value) for value in values]
    return None if any(value is None for value in result) else list(result)  # type: ignore[arg-type]


def _plain_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _session_date(value: Any) -> date | None:
    if type(value) is date:
        return value
    converter = getattr(value, "date", None)
    if callable(converter):
        converted = converter()
        return converted if isinstance(converted, date) else None
    return None


def _current_history(
    view: MarketViewV1,
    security_id: str,
    *,
    limit: int,
    columns: tuple[str, ...],
) -> Any | None:
    history = view.price_history(security_id, limit=limit, columns=columns)
    if history.empty or _session_date(history.index[-1]) != view.as_of_session:
        return None
    if not set(columns).issubset(history.columns):
        return None
    return history


def _visible_scan(view: MarketViewV1, security_id: str) -> Any | None:
    scan = view.scan_result(security_id)
    if scan is None or getattr(scan, "security_id", None) != security_id:
        return None
    as_of = getattr(scan, "as_of_session_date", None)
    if not isinstance(as_of, date) or as_of > view.as_of_session:
        return None
    return scan


def _position(portfolio: PortfolioView, security_id: str) -> Any | None:
    return next(
        (item for item in portfolio.positions if item.security_id == security_id),
        None,
    )


def _integral_quantity(portfolio: PortfolioView, security_id: str) -> Decimal:
    held = _position(portfolio, security_id)
    if held is None or held.quantity <= 0:
        return Decimal(0)
    return held.quantity


def _sma_slope(prices: list[Decimal], window: int, lookback: int = 4) -> Decimal | None:
    if len(prices) < window + lookback:
        return None
    current = sum(prices[-window:], Decimal(0)) / Decimal(window)
    past = sum(prices[-(window + lookback) : -lookback], Decimal(0)) / Decimal(window)
    return current - past


def _weekly_closes(history: Any) -> list[Decimal] | None:
    grouped: dict[tuple[int, int], Decimal] = {}
    try:
        sessions = history.index
        closes = history["close"]
    except (AttributeError, KeyError, TypeError):
        return None
    for session, raw_close in zip(sessions, closes, strict=True):
        session_date = _session_date(session)
        close = _decimal(raw_close)
        if session_date is None or close is None:
            return None
        iso = session_date.isocalendar()
        grouped[(iso.year, iso.week)] = close
    return [grouped[key] for key in sorted(grouped)][-52:]


def _classify_stage(
    *,
    price: Decimal,
    sma150: Decimal,
    sma200: Decimal,
    weekly: list[Decimal],
) -> str | None:
    slope150 = _sma_slope(weekly, window=30)
    slope200 = _sma_slope(weekly, window=40)
    if slope150 is None or slope200 is None:
        return None
    above_150 = price > sma150
    above_200 = price > sma200
    ma_bullish = sma150 > sma200
    if above_150 and ma_bullish and slope200 > 0:
        return "Stage 2"
    if not above_150 and not above_200 and slope150 < 0:
        return "Stage 4"
    if (not above_150 and above_200) or (above_150 and ma_bullish and slope200 <= 0):
        return "Stage 3"
    return "Stage 1"


class _EntryQualification(NamedTuple):
    """A qualifying entry's trend score plus the evidence behind it."""

    score: Decimal
    close: Decimal
    prior_high: Decimal
    lookback: int
    volume: Decimal
    prior_volume_mean: Decimal
    required_volume: Decimal
    volume_multiplier: Decimal
    scan_stage: str
    daily_stage: str


class _EntryRankEvidence(NamedTuple):
    momentum: Decimal | None
    numerator_session: date | None
    denominator_session: date | None
    score_currency: str | None
    relative_volume: Decimal
    missing_reason: str | None


def _ranking_evidence(
    view: MarketViewV1, security_id: str, qualification: _EntryQualification
) -> _EntryRankEvidence:
    """Read bounded base-currency endpoints without affecting eligibility."""
    if not isinstance(view, BaseCurrencyCloseHistoryViewV1):
        return _EntryRankEvidence(
            None,
            None,
            None,
            None,
            _relative_volume(qualification),
            "base_currency_history_unavailable",
        )
    history = view.base_currency_close_history(security_id, limit=253)
    currency = view.base_currency
    sessions = tuple(_session_date(session) for session in history.index)
    if any(session is None for session in sessions):
        raise ValueError("base-currency history has an invalid session index")
    dated_sessions = tuple(session for session in sessions if session is not None)
    if dated_sessions != tuple(sorted(set(dated_sessions))):
        raise ValueError("base-currency history sessions are not unique and ordered")
    if any(session > view.as_of_session for session in dated_sessions):
        raise ValueError("base-currency history contains a future session")
    if not dated_sessions or dated_sessions[-1] != view.as_of_session:
        return _EntryRankEvidence(
            None,
            None,
            None,
            currency,
            _relative_volume(qualification),
            "current_base_currency_close_unavailable",
        )
    if len(history.index) < 253:
        return _EntryRankEvidence(
            None,
            None,
            None,
            currency,
            _relative_volume(qualification),
            "insufficient_price_history",
        )

    numerator_session = dated_sessions[-22]
    denominator_session = dated_sessions[-253]
    numerator = _decimal(history["close"].iloc[-22])
    denominator = _decimal(history["close"].iloc[-253])
    numerator_reason = history["reason"].iloc[-22]
    denominator_reason = history["reason"].iloc[-253]
    missing: list[str] = []
    if numerator is None or numerator <= 0:
        missing.append(
            str(numerator_reason)
            if isinstance(numerator_reason, str) and numerator_reason
            else "invalid_momentum_endpoint"
        )
    if denominator is None or denominator <= 0:
        missing.append(
            str(denominator_reason)
            if isinstance(denominator_reason, str) and denominator_reason
            else "invalid_momentum_endpoint"
        )
    if missing:
        return _EntryRankEvidence(
            None,
            numerator_session,
            denominator_session,
            currency,
            _relative_volume(qualification),
            ",".join(dict.fromkeys(missing)),
        )
    assert numerator is not None and denominator is not None
    return _EntryRankEvidence(
        numerator / denominator - Decimal(1),
        numerator_session,
        denominator_session,
        currency,
        _relative_volume(qualification),
        None,
    )


def _relative_volume(qualification: _EntryQualification) -> Decimal:
    return qualification.volume / qualification.prior_volume_mean


def _entry_explanation(
    qualification: _EntryQualification,
    ranking: _EntryRankEvidence,
    *,
    session: date,
    rank: int,
    candidate_count: int,
    priority: Decimal,
) -> SignalExplanationV1:
    """Explain one Stage 2 breakout entry in provider-neutral terms."""
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="stage2_confirmed",
                summary=(
                    "The monthly scan and today's own price structure both "
                    "read a Stage 2 advance."
                ),
                facts=[
                    ExplanationFactV1(
                        label="Scan stage",
                        observed=qualification.scan_stage,
                        operator=ComparisonOperator.IS,
                        threshold="Stage 2",
                    ),
                    ExplanationFactV1(
                        label="Daily stage",
                        observed=qualification.daily_stage,
                        operator=ComparisonOperator.IS,
                        threshold="Stage 2",
                        as_of=session,
                    ),
                ],
            ),
            SignalReasonV1(
                code="breakout_above_prior_high",
                summary="Close broke above its prior breakout-window high.",
                facts=[
                    ExplanationFactV1(
                        label="Close",
                        observed=qualification.close,
                        operator=ComparisonOperator.GT,
                        threshold=qualification.prior_high,
                        unit=EvidenceUnit.PRICE,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Breakout lookback",
                        observed=Decimal(qualification.lookback),
                        unit=EvidenceUnit.SESSIONS,
                    ),
                ],
            ),
            SignalReasonV1(
                code="volume_expansion",
                summary="Breakout volume expanded above its 50-session average.",
                facts=[
                    ExplanationFactV1(
                        label="Volume",
                        observed=qualification.volume,
                        operator=ComparisonOperator.GTE,
                        threshold=qualification.required_volume,
                        unit=EvidenceUnit.COUNT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Required multiple of average volume",
                        observed=qualification.volume_multiplier,
                        unit=EvidenceUnit.RATIO,
                    ),
                ],
            ),
            SignalReasonV1(
                code="entry_ranking",
                summary=(
                    "Qualifying entries rank by 252-session Run-currency price "
                    "momentum, then relative volume, then security ID."
                ),
                facts=[
                    ExplanationFactV1(
                        label="Ranking policy",
                        observed="weinstein_momentum_relative_volume_v1",
                    ),
                    ExplanationFactV1(
                        label="Momentum",
                        observed=ranking.momentum,
                        unit=EvidenceUnit.RATIO,
                        as_of=ranking.numerator_session,
                    ),
                    ExplanationFactV1(
                        label="Momentum numerator session",
                        observed=(
                            ranking.numerator_session.isoformat()
                            if ranking.numerator_session is not None
                            else "unavailable"
                        ),
                    ),
                    ExplanationFactV1(
                        label="Momentum denominator session",
                        observed=(
                            ranking.denominator_session.isoformat()
                            if ranking.denominator_session is not None
                            else "unavailable"
                        ),
                    ),
                    ExplanationFactV1(
                        label="Score currency",
                        observed=ranking.score_currency or "unavailable",
                    ),
                    ExplanationFactV1(
                        label="Relative volume",
                        observed=ranking.relative_volume,
                        unit=EvidenceUnit.RATIO,
                    ),
                    ExplanationFactV1(
                        label="Candidate count", observed=Decimal(candidate_count)
                    ),
                    ExplanationFactV1(label="Ordinal rank", observed=Decimal(rank)),
                    ExplanationFactV1(label="Encoded priority", observed=priority),
                    ExplanationFactV1(
                        label="Momentum unavailable reason",
                        observed=ranking.missing_reason,
                    ),
                ],
            ),
        ]
    )


def _upgrade_explanation(
    *,
    candidate_id: str,
    candidate_score: Decimal,
    held_score: Decimal,
    margin: Decimal,
    session: date,
) -> SignalExplanationV1:
    """Explain rotating out of the weakest holding into stronger leadership."""
    return SignalExplanationV1(
        reasons=[
            SignalReasonV1(
                code="portfolio_upgrade",
                summary=(
                    "A stronger Stage 2 candidate outranks this holding by "
                    "more than the required margin, so capital rotates to it."
                ),
                facts=[
                    # The candidate's identity is a fact *value*, never part
                    # of the label: a long security id must not be able to
                    # overflow the label bound and cost the Sell signal.
                    ExplanationFactV1(label="Upgrade candidate", observed=candidate_id),
                    ExplanationFactV1(
                        label="Candidate trend score",
                        observed=candidate_score,
                        operator=ComparisonOperator.GTE,
                        threshold=held_score + margin,
                        unit=EvidenceUnit.PERCENT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Held trend score",
                        observed=held_score,
                        unit=EvidenceUnit.PERCENT,
                        as_of=session,
                    ),
                    ExplanationFactV1(
                        label="Required upgrade margin",
                        observed=margin,
                        unit=EvidenceUnit.PERCENT,
                    ),
                ],
            ),
        ]
    )


class WeinsteinStrategy:
    """Apply Stage 2 breakout and Stage/risk exit rules."""

    def __init__(self) -> None:
        self._qualification_view: MarketViewV1 | None = None
        self._qualification_cache: dict[
            tuple[str, str], _EntryQualification | None
        ] = {}

    def _cached_entry_qualification(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> _EntryQualification | None:
        if self._qualification_view is not view:
            self._qualification_view = view
            self._qualification_cache.clear()
        key = (security_id, repr(sorted(parameters.items(), key=lambda item: item[0])))
        if key not in self._qualification_cache:
            self._qualification_cache[key] = self._entry_qualification(
                view, parameters, security_id
            )
        return self._qualification_cache[key]

    def evidence_requirements(
        self, parameters: StrategyParameters
    ) -> StrategyEvidenceRequirementsV1:
        """Declare the trend window *and* the Weinstein stage evidence.

        The stage classification is not derivable from OHLCV alone: the
        entry rule and the stage-failure exit both read the committed
        monthly scan's ``stage``. Declaring it means a view that cannot
        evidence a stage is reported incompatible rather than silently
        read as "not Stage 2" (#471).
        """
        lookback = _plain_int(parameters.get("breakout_lookback_sessions")) or 50
        stage = EvidenceRequirementV1(kind=EvidenceKind.SCAN_STAGE)
        return StrategyEvidenceRequirementsV1(
            entry=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=max(220, lookback + 1, 51),
                    columns=("high", "close", "volume"),
                ),
                stage,
            ),
            exit=(
                EvidenceRequirementV1(
                    kind=EvidenceKind.PRICE_HISTORY,
                    minimum_sessions=150,
                    columns=("close",),
                ),
                stage,
            ),
        )

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        universe = _universe(parameters)
        if not entry_signals_permitted(view, parameters, universe):
            return []
        candidates: list[tuple[str, _EntryQualification, _EntryRankEvidence]] = []
        for security_id in universe:
            qualification = self._cached_entry_qualification(
                view, parameters, security_id
            )
            if qualification is not None:
                candidates.append(
                    (
                        security_id,
                        qualification,
                        _ranking_evidence(view, security_id, qualification),
                    )
                )
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                candidate[2].momentum is None,
                -(candidate[2].momentum or Decimal(0)),
                -candidate[2].relative_volume,
                candidate[0],
            ),
        )
        count = len(ordered)
        return [
            self._entry_signal(
                view,
                security_id,
                qualification,
                ranking,
                rank=rank,
                candidate_count=count,
            )
            for rank, (security_id, qualification, ranking) in enumerate(
                ordered, start=1
            )
        ]

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        signals = [
            self._exit_signal(view, portfolio, parameters, security_id)
            for security_id in _universe(parameters)
        ]
        signals = [signal for signal in signals if signal is not None]
        upgrade = self._upgrade_exit_signal(
            view,
            portfolio,
            parameters,
            frozenset(signal.security_id for signal in signals),
        )
        if upgrade is not None:
            signals.append(upgrade)
        return signals

    def _entry_qualification(
        self, view: MarketViewV1, parameters: StrategyParameters, security_id: str
    ) -> _EntryQualification | None:
        """Return this security's qualification -- its trend-strength score
        (percent close is above its 150-session SMA) plus the observations
        behind it -- if it qualifies for entry today, else ``None``.
        Factored out of :meth:`_entry_signal` so the upgrade-exit ranking
        (below) can score a would-be candidate using the exact same
        qualification rules, without duplicating them, and so the emitted
        Signal can explain itself (#472) from the very same numbers."""
        scan = _visible_scan(view, security_id)
        if scan is None:
            return None
        lookback = _plain_int(parameters["breakout_lookback_sessions"])
        if lookback is None:
            return None
        required = max(220, lookback + 1, 51)
        history = _current_history(
            view,
            security_id,
            limit=required,
            columns=("high", "close", "volume"),
        )
        if history is None or len(history) < required:
            return None

        closes = _decimals(history["close"])
        highs = _decimals(history["high"])
        volumes = _decimals(history["volume"])
        weekly = _weekly_closes(history)
        if closes is None or highs is None or volumes is None or weekly is None:
            return None
        close = closes[-1]
        sma150 = sum(closes[-150:], Decimal(0)) / Decimal(150)
        sma200 = sum(closes[-200:], Decimal(0)) / Decimal(200)
        daily_stage = _classify_stage(
            price=close,
            sma150=sma150,
            sma200=sma200,
            weekly=weekly,
        )
        prior_high = max(highs[-(lookback + 1) : -1])
        prior_volume_mean = sum(volumes[-51:-1], Decimal(0)) / Decimal(50)
        minimum_volume = _decimal(parameters["minimum_relative_volume"])
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        if (
            minimum_volume is None
            or prior_volume_mean <= 0
            or volumes[-1] < 0
            or scan_stage != "Stage 2"
            or daily_stage != "Stage 2"
            or close <= prior_high
            or volumes[-1] < prior_volume_mean * minimum_volume
            or sma150 <= 0
        ):
            return None
        return _EntryQualification(
            score=(close - sma150) / sma150 * Decimal(100),
            close=close,
            prior_high=prior_high,
            lookback=lookback,
            volume=volumes[-1],
            prior_volume_mean=prior_volume_mean,
            required_volume=prior_volume_mean * minimum_volume,
            volume_multiplier=minimum_volume,
            scan_stage=scan_stage,
            daily_stage=daily_stage,
        )

    def _entry_signal(
        self,
        view: MarketViewV1,
        security_id: str,
        qualification: _EntryQualification,
        ranking: _EntryRankEvidence,
        *,
        rank: int,
        candidate_count: int,
    ) -> Signal:
        priority = Decimal(candidate_count - rank + 1)
        return Signal(
            security_id=security_id,
            side=SignalSide.BUY,
            session=view.as_of_session,
            rule_id=_ENTRY_RULE,
            priority=priority,
            explanation=_entry_explanation(
                qualification,
                ranking,
                session=view.as_of_session,
                rank=rank,
                candidate_count=candidate_count,
                priority=priority,
            ),
        )

    def _held_trend_strength(
        self, view: MarketViewV1, security_id: str
    ) -> Decimal | None:
        """Return a held position's current percent-above-150-session-SMA
        for upgrade ranking, or ``None`` if there isn't enough bounded
        history today -- a position with no computable score is never
        treated as the weakest holding."""
        history = _current_history(view, security_id, limit=150, columns=("close",))
        if history is None or len(history) < 150:
            return None
        closes = _decimals(history["close"].iloc[-150:])
        if closes is None:
            return None
        close = closes[-1]
        sma150 = sum(closes, Decimal(0)) / Decimal(150)
        if sma150 <= 0:
            return None
        return (close - sma150) / sma150 * Decimal(100)

    def _upgrade_exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        already_exiting: frozenset[str],
    ) -> Signal | None:
        """Portfolio upgrading: rotate capital toward the strongest Stage 2
        leadership when a slot isn't otherwise free.

        When a stronger unheld candidate's percent-above-150-session-SMA
        clears the weakest held position's own current reading by at least
        ``upgrade_score_margin_pct`` points, sell the weakest holding to
        free cash for the stronger setup -- mirroring Weinstein's own
        practice of rotating out of laggards into leadership during a Stage
        2 advance. This never overrides the mechanical stop/SMA/stage
        exits above and never buys anything itself -- the freed cash is
        picked up by the ordinary entry path on a later qualifying
        session.
        """
        if parameters.get("enable_position_upgrade") is not True:
            return None
        margin = _decimal(parameters["upgrade_score_margin_pct"])
        if margin is None:
            return None
        # The shared allocator owns BUY affordability.  Do not liquidate a
        # holding while a cash slot remains for its next cohort.
        if portfolio.cash > 0:
            return None

        held_ids = {
            position.security_id
            for position in portfolio.positions
            if position.quantity > 0 and position.security_id not in already_exiting
        }
        if not held_ids:
            return None

        candidates: list[tuple[Decimal, str]] = []
        for security_id in _universe(parameters):
            if security_id in held_ids:
                continue
            qualification = self._cached_entry_qualification(
                view, parameters, security_id
            )
            if qualification is not None:
                candidates.append((qualification.score, security_id))
        if not candidates:
            return None
        best_score, best_security_id = max(candidates, key=lambda item: item)

        held_scored = [
            (score, security_id)
            for security_id in held_ids
            if (score := self._held_trend_strength(view, security_id)) is not None
        ]
        if not held_scored:
            return None
        weakest_score, weakest_security_id = min(held_scored, key=lambda item: item)

        if best_score - weakest_score < margin:
            return None
        return Signal(
            security_id=weakest_security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_UPGRADE_EXIT_RULE,
            explanation=_upgrade_explanation(
                candidate_id=best_security_id,
                candidate_score=best_score,
                held_score=weakest_score,
                margin=margin,
                session=view.as_of_session,
            ),
        )

    def _exit_signal(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> Signal | None:
        held = _position(portfolio, security_id)
        if held is None or held.quantity <= 0:
            return None
        scan = _visible_scan(view, security_id)
        history = _current_history(view, security_id, limit=150, columns=("close",))
        if history is None or len(history) < 150:
            return None
        closes = _decimals(history["close"].iloc[-150:])
        maximum_loss = _decimal(parameters["maximum_loss_pct"])
        if closes is None or maximum_loss is None:
            return None
        close = closes[-1]
        sma150 = sum(closes, Decimal(0)) / Decimal(150)
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        scan_stage = getattr(getattr(scan, "stage", None), "value", None)
        # The reason list below *is* the exit decision (#472): each rule
        # appends itself, and the Sell fires exactly when at least one did.
        # Deriving the decision from the reasons rather than restating both
        # makes an explained condition and a firing condition impossible to
        # diverge.
        reasons: list[SignalReasonV1] = []
        if close <= stop:
            reasons.append(
                SignalReasonV1(
                    code="maximum_loss_stop",
                    summary="Close hit the maximum-loss stop for this position.",
                    facts=[
                        ExplanationFactV1(
                            label="Close",
                            observed=close,
                            operator=ComparisonOperator.LTE,
                            threshold=stop,
                            unit=EvidenceUnit.PRICE,
                            as_of=view.as_of_session,
                        ),
                        ExplanationFactV1(
                            label="Maximum loss",
                            observed=maximum_loss,
                            unit=EvidenceUnit.PERCENT,
                        ),
                    ],
                )
            )
        if close < sma150:
            reasons.append(
                SignalReasonV1(
                    code="close_below_sma150",
                    summary="Close fell below the 150-session moving average.",
                    facts=[
                        ExplanationFactV1(
                            label="Close",
                            observed=close,
                            operator=ComparisonOperator.LT,
                            threshold=sma150,
                            unit=EvidenceUnit.PRICE,
                            as_of=view.as_of_session,
                        ),
                    ],
                )
            )
        # Only an *evidenced* stage can fail: a view carrying no stage
        # evidence at all must never be read as "not Stage 2", which
        # would manufacture a Sell out of missing evidence (#471).
        if scan_stage is not None and scan_stage != "Stage 2":
            reasons.append(
                SignalReasonV1(
                    code="stage_exit",
                    summary="The security is no longer in a Stage 2 advance.",
                    facts=[
                        ExplanationFactV1(
                            label="Weinstein stage",
                            observed=scan_stage,
                            operator=ComparisonOperator.IS_NOT,
                            threshold="Stage 2",
                        ),
                    ],
                )
            )
        if not reasons:
            return None
        return Signal(
            security_id=security_id,
            side=SignalSide.SELL,
            session=view.as_of_session,
            rule_id=_EXIT_RULE,
            explanation=SignalExplanationV1(reasons=reasons),
        )

    def stop_level(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
        security_id: str,
    ) -> StopLevelV1 | None:
        """Return the close at which :meth:`_exit_signal` fires next session.

        The exit sells on ``close <= stop`` or on a close below the next
        session's 150-session SMA, which is exactly a close below the mean
        of today's latest 149 closes; the higher of the two binds.
        ``maximum_loss_pct`` is read as the exit reads it, with no default.
        """
        held = _position(portfolio, security_id)
        if held is None or held.quantity <= 0:
            return None
        maximum_loss = _decimal(parameters.get("maximum_loss_pct"))
        if maximum_loss is None or not 0 <= maximum_loss < 100:
            return StopLevelV1(
                rule_code="invalid_setting",
                summary="Strategy setting maximum_loss_pct is missing or unusable.",
            )
        history = _current_history(view, security_id, limit=149, columns=("close",))
        closes = (
            None
            if history is None or len(history) < 149
            else _decimals(history["close"])
        )
        if closes is None:
            return StopLevelV1(
                rule_code="insufficient_history",
                summary="Needs 149 sessions of current closes.",
            )
        stop = held.average_cost * (Decimal(1) - maximum_loss / Decimal(100))
        sma_break = sum(closes, Decimal(0)) / Decimal(149)
        facts = [
            ExplanationFactV1(
                label="Maximum-loss stop", observed=stop, unit=EvidenceUnit.PRICE
            ),
            ExplanationFactV1(
                label="Maximum loss", observed=maximum_loss, unit=EvidenceUnit.PERCENT
            ),
            ExplanationFactV1(
                label="Close that breaks the 150-session SMA",
                observed=sma_break,
                unit=EvidenceUnit.PRICE,
                as_of=view.as_of_session,
            ),
        ]
        if sma_break > stop:
            return StopLevelV1(
                level=sma_break,
                rule_code="close_below_sma150",
                basis="mixed",
                trigger="close_lt",
                summary="Close below the 150-day average",
                facts=facts,
            )
        if stop <= 0:
            return StopLevelV1(
                rule_code="no_level", summary="No positive stop level.", facts=facts
            )
        return StopLevelV1(
            level=stop,
            rule_code="maximum_loss_stop",
            summary=f"Max loss {maximum_loss.normalize():f}% from average cost",
            facts=facts,
            basis="mixed",
            trigger="close_lte",
        )

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int | Decimal:
        if signal.side == SignalSide.SELL:
            return _integral_quantity(portfolio, signal.security_id)
        # The engine reserves equal capital and determines whole shares.
        return 0
