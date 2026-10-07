"""Contract tests for the deterministic Minervini backtest Strategy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.services.backtest.strategy_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceKind,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_protocol import (
    PortfolioView,
    PositionSummaryV1,
    Signal,
    SignalSide,
    StopLevelV1,
    StrategyProtocolV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
)

RUNTIME = Path(__file__).resolve().parents[1] / "strategy.py"
SPEC = spec_from_file_location("minervini_backtest_strategy_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MinerviniStrategy = MODULE.MinerviniStrategy

AS_OF = date(2026, 8, 20)
PARAMETERS = {
    "selected_securities": ["sec-aapl"],
    "minimum_vcp_score": 70,
    "minimum_trend_score": 85.0,
    "minimum_relative_volume": 1.5,
    "maximum_pivot_extension_pct": 3.0,
    "maximum_loss_pct": 8.0,
}


def _history(
    *, current_close: str = "103", current_volume: str = "150"
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=51)
    closes = [Decimal("100")] * 50 + [Decimal(current_close)]
    volumes = [Decimal("100")] * 50 + [Decimal(current_volume)]
    return pd.DataFrame({"close": closes, "volume": volumes}, index=sessions)


def _momentum_history(
    numerator: str | None, *, rows: int = 253, reason: str | None = None
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=rows)
    closes: list[Decimal | None] = [Decimal("100")] * rows
    reasons: list[str | None] = [None] * rows
    if rows >= 22:
        closes[-22] = None if numerator is None else Decimal(numerator)
        reasons[-22] = reason
    return pd.DataFrame({"close": closes, "reason": reasons}, index=sessions)


def _scan(
    *,
    stage: str = "Stage 2",
    state: str = "Breakout",
    as_of: date = date(2026, 7, 31),
    score: int = 70,
    security_id: str = "sec-aapl",
    breakout_volume: bool = True,
    valid_vcp: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id=security_id,
        as_of_session_date=as_of,
        stage=SimpleNamespace(value=stage),
        vcp=SimpleNamespace(
            valid_vcp=valid_vcp,
            score=score,
            trend_template_score=Decimal("85"),
            trend_template_passed=True,
            breakout_volume_detected=breakout_volume,
            pivot_price=Decimal("100"),
            execution_state=state,
        ),
    )


class _View:
    def __init__(self, history: pd.DataFrame, scan: SimpleNamespace | None) -> None:
        self.as_of_session = AS_OF
        self._history = history
        self._scan = scan

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: object | None = None,
    ) -> pd.DataFrame:
        history = self._history.copy()
        history = history.loc[
            [
                (index.date() if hasattr(index, "date") else index)
                <= self.as_of_session
                for index in history.index
            ]
        ]
        if limit is not None:
            history = history.iloc[-limit:]
        if columns is not None:
            history = history.loc[:, list(columns)]
        return history

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        return self._scan


def _portfolio(quantity: str = "10", average_cost: str = "100") -> PortfolioView:
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal("1000"),
        positions=(
            PositionSummaryV1(
                security_id="sec-aapl",
                quantity=Decimal(quantity),
                average_cost=Decimal(average_cost),
            ),
        ),
        volatility_observations=(),
    )


def test_entry_qualifies_at_inclusive_score_volume_and_extension_bounds() -> None:
    strategy = MinerviniStrategy()
    view = _View(_history(), _scan())

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert first == second
    assert [(item.side, item.rule_id) for item in first] == [
        (SignalSide.BUY, "minervini_vcp_breakout_v1")
    ]


def test_unvalidated_vcp_still_enters_and_priority_is_ordinal() -> None:
    """A validated VCP is optional; raw VCP evidence remains available."""
    strategy = MinerviniStrategy()
    view = _View(_history(), _scan(valid_vcp=False, score=42))

    signals = validate_entry_signals(
        strategy.entry_signals(view, {**PARAMETERS, "minimum_vcp_score": 0})
    )

    assert [(item.side, item.priority) for item in signals] == [
        (SignalSide.BUY, Decimal("1"))
    ]
    assert signals[0].explanation is not None
    ranking = next(
        reason
        for reason in signals[0].explanation.reasons
        if reason.code == "entry_ranking"
    )
    assert next(
        fact.observed for fact in ranking.facts if fact.label == "Raw VCP score"
    ) == Decimal("42")
    assert (
        next(
            fact.observed
            for fact in ranking.facts
            if fact.label == "Momentum unavailable reason"
        )
        == "base_currency_history_unavailable"
    )


def test_zero_vcp_score_remains_an_eligible_entry() -> None:
    signal = MinerviniStrategy().entry_signals(
        _View(_history(), _scan(valid_vcp=False, score=0)),
        {**PARAMETERS, "minimum_vcp_score": 0},
    )[0]

    assert signal.side is SignalSide.BUY
    assert signal.priority == Decimal("1")
    assert signal.explanation is not None
    ranking = next(
        reason
        for reason in signal.explanation.reasons
        if reason.code == "entry_ranking"
    )
    assert next(
        fact.observed for fact in ranking.facts if fact.label == "Raw VCP score"
    ) == Decimal("0")


def test_scan_without_a_vcp_score_never_enters() -> None:
    strategy = MinerviniStrategy()
    scan = _scan()
    scan.vcp.score = None

    signals = strategy.entry_signals(
        _View(_history(), scan), {**PARAMETERS, "minimum_vcp_score": 0}
    )

    assert signals == []


def test_entry_fails_closed_for_short_stale_or_overextended_evidence() -> None:
    strategy = MinerviniStrategy()
    short = _history().iloc[-50:]
    stale = _history().iloc[:-1]

    assert strategy.entry_signals(_View(short, _scan()), PARAMETERS) == []
    assert strategy.entry_signals(_View(stale, _scan()), PARAMETERS) == []
    assert (
        strategy.entry_signals(
            _View(_history(current_close="103.01"), _scan()), PARAMETERS
        )
        == []
    )


def test_entry_fails_closed_for_missing_future_or_invalid_volume_evidence() -> None:
    strategy = MinerviniStrategy()

    assert strategy.entry_signals(_View(_history(), None), PARAMETERS) == []
    assert (
        strategy.entry_signals(
            _View(_history(), _scan(as_of=date(2026, 8, 21))), PARAMETERS
        )
        == []
    )
    assert (
        strategy.entry_signals(
            _View(_history(current_volume="NaN"), _scan()), PARAMETERS
        )
        == []
    )
    assert (
        strategy.entry_signals(
            _View(_history(current_volume="149.99"), _scan()), PARAMETERS
        )
        == []
    )
    zero_volume = _history(current_volume="0")
    zero_volume.loc[:, "volume"] = Decimal("0")
    assert strategy.entry_signals(_View(zero_volume, _scan()), PARAMETERS) == []


def test_daily_breakout_enters_from_an_intact_monthly_base() -> None:
    """A month-end scan read before the breakout (#31) must not block entry.

    ``Breakout`` in a scan describes only its snapshot session, so the daily
    close/volume gates trigger the entry while the scan vouches for the base.
    """
    strategy = MinerviniStrategy()

    for state in ("Pre-breakout", "Breakout", "Early-post-breakout"):
        scan = _scan(state=state, breakout_volume=False)
        signals = strategy.entry_signals(_View(_history(), scan), PARAMETERS)
        assert [item.rule_id for item in signals] == ["minervini_vcp_breakout_v1"]


def test_monthly_scan_state_still_rejects_extended_or_broken_bases() -> None:
    strategy = MinerviniStrategy()

    for state in ("Extended", "Overextended", "Damaged", "Invalid"):
        view = _View(_history(), _scan(state=state))
        assert strategy.entry_signals(view, PARAMETERS) == []


def test_daily_gates_still_decide_the_breakout_from_an_intact_base() -> None:
    strategy = MinerviniStrategy()
    base = _scan(state="Pre-breakout", breakout_volume=False)

    below_pivot = _history(current_close="99.99")
    assert strategy.entry_signals(_View(below_pivot, base), PARAMETERS) == []
    thin_volume = _history(current_volume="149.99")
    assert strategy.entry_signals(_View(thin_volume, base), PARAMETERS) == []


def test_exit_and_position_sizing_use_full_held_quantity() -> None:
    strategy = MinerviniStrategy()
    view = _View(_history(), _scan(state="Damaged"))
    portfolio = _portfolio()

    exits = validate_exit_signals(strategy.exit_signals(view, portfolio, PARAMETERS))

    assert len(exits) == 1
    assert (
        validate_position_size(
            strategy.position_size(exits[0], view, portfolio, PARAMETERS)
        )
        == 10
    )
    assert strategy.position_size(
        exits[0], view, _portfolio("10.5"), PARAMETERS
    ) == Decimal("10.5")
    buy = Signal(
        security_id="sec-aapl",
        side=SignalSide.BUY,
        session=AS_OF,
        rule_id="test_buy",
    )
    assert strategy.position_size(buy, view, portfolio, PARAMETERS) == 0


def test_price_risk_exit_does_not_require_scan_and_zero_quantity_does_not_sell() -> (
    None
):
    strategy = MinerviniStrategy()
    loss_view = _View(_history(current_close="91"), None)

    assert len(strategy.exit_signals(loss_view, _portfolio(), PARAMETERS)) == 1
    assert (
        strategy.exit_signals(
            _View(_history(), _scan(state="Damaged")), _portfolio("0"), PARAMETERS
        )
        == []
    )


class _UniverseView(_View):
    """Serves the same evidence for every selected security."""

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        if self._scan is None:
            return None
        return SimpleNamespace(**{**vars(self._scan), "security_id": security_id})


def test_multi_security_universe_enters_each_qualifying_security() -> None:
    strategy = MinerviniStrategy()
    view = _UniverseView(_history(), _scan())

    signals = validate_entry_signals(
        strategy.entry_signals(
            view,
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl", "sec-msft"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl", "sec-msft"]


def test_multi_security_universe_skips_securities_without_matching_scan() -> None:
    """``_View`` only ever serves ``sec-aapl``'s scan, so a second selected
    security has no visible evidence of its own and must not enter."""
    strategy = MinerviniStrategy()

    signals = validate_entry_signals(
        strategy.entry_signals(
            _View(_history(), _scan()),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl"]


def test_multi_security_universe_exits_only_held_securities() -> None:
    strategy = MinerviniStrategy()
    view = _UniverseView(_history(), _scan(state="Damaged"))

    exits = validate_exit_signals(
        strategy.exit_signals(
            view,
            _portfolio(),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in exits] == ["sec-aapl"]


class _KeyedView:
    """Serves distinct history/scan evidence per security_id, for
    upgrade-exit tests that need a weak held position and a stronger
    unheld candidate to differ."""

    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        scans: dict[str, SimpleNamespace | None],
    ) -> None:
        self.as_of_session = AS_OF
        self._histories = histories
        self._scans = scans

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: object | None = None,
    ) -> pd.DataFrame:
        history = self._histories.get(security_id, pd.DataFrame()).copy()
        if limit is not None:
            history = history.iloc[-limit:]
        if columns is not None:
            history = history.loc[:, list(columns)]
        return history

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        scan = self._scans.get(security_id)
        if scan is None:
            return None
        return SimpleNamespace(**{**vars(scan), "security_id": security_id})


class _RankedKeyedView(_KeyedView):
    base_currency = "GBP"

    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        scans: dict[str, SimpleNamespace | None],
        ranking_histories: dict[str, pd.DataFrame],
    ) -> None:
        super().__init__(histories, scans)
        self._ranking_histories = ranking_histories

    def base_currency_close_history(
        self, security_id: str, *, limit: int
    ) -> pd.DataFrame:
        return self._ranking_histories[security_id].iloc[-limit:]


def _upgrade_parameters(**overrides: object) -> dict[str, object]:
    return {
        **PARAMETERS,
        "selected_securities": ["sec-aapl", "sec-msft"],
        "enable_position_upgrade": True,
        "upgrade_score_margin": 15,
        **overrides,
    }


def test_entry_ranking_is_vcp_first_then_momentum_then_security_id() -> None:
    strategy = MinerviniStrategy()
    ids = ("sec-a", "sec-b", "sec-c", "sec-d")
    view = _RankedKeyedView(
        {security_id: _history() for security_id in ids},
        {
            "sec-a": _scan(score=80, security_id="sec-a"),
            "sec-b": _scan(score=80, security_id="sec-b"),
            "sec-c": _scan(score=79, security_id="sec-c"),
            "sec-d": _scan(score=80, security_id="sec-d"),
        },
        {
            "sec-a": _momentum_history("90"),
            "sec-b": _momentum_history(None, reason="fx_stale"),
            "sec-c": _momentum_history("140"),
            "sec-d": _momentum_history("90"),
        },
    )

    signals = strategy.entry_signals(
        view,
        {
            **PARAMETERS,
            "selected_securities": list(reversed(ids)),
            "minimum_vcp_score": 0,
        },
    )

    assert {signal.security_id: signal.priority for signal in signals} == {
        "sec-a": Decimal("4"),
        "sec-d": Decimal("3"),
        "sec-b": Decimal("2"),
        "sec-c": Decimal("1"),
    }
    assert len(signals) == 4
    missing = next(signal for signal in signals if signal.security_id == "sec-b")
    assert missing.explanation is not None
    ranking = next(
        reason
        for reason in missing.explanation.reasons
        if reason.code == "entry_ranking"
    )
    assert (
        next(
            fact.observed
            for fact in ranking.facts
            if fact.label == "Momentum unavailable reason"
        )
        == "fx_stale"
    )
    strongest_momentum = next(
        signal for signal in signals if signal.security_id == "sec-c"
    )
    assert strongest_momentum.explanation is not None
    momentum_reason = next(
        reason
        for reason in strongest_momentum.explanation.reasons
        if reason.code == "entry_ranking"
    )
    assert next(
        fact.observed for fact in momentum_reason.facts if fact.label == "Momentum"
    ) == Decimal("40.0")


def _held_portfolio(cash: str, security_id: str = "sec-aapl") -> PortfolioView:
    return PortfolioView(
        as_of_session=AS_OF,
        base_currency="GBP",
        cash=Decimal(cash),
        positions=(
            PositionSummaryV1(
                security_id=security_id,
                quantity=Decimal("10"),
                average_cost=Decimal("100"),
            ),
        ),
        volatility_observations=(),
    )


def test_upgrade_exit_disabled_by_default_even_with_a_stronger_starved_candidate() -> (
    None
):
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view,
        _held_portfolio(cash="1"),
        {**PARAMETERS, "selected_securities": ["sec-aapl", "sec-msft"]},
    )

    assert exits == []


def test_upgrade_exit_sells_weakest_position_when_margin_cleared() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="0"), _upgrade_parameters())
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "minervini_upgrade_exit_v1")
    ]


def test_upgrade_exit_keeps_a_cash_slot_for_engine_owned_buy_allocation() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="10000"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_does_not_fire_when_margin_not_cleared() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=80)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_never_duplicates_a_position_already_exiting_on_its_own_rule() -> (
    None
):
    """The held position is already exiting via its own risk rule
    (Damaged VCP) -- the upgrade check must not also emit a second SELL
    for the same security."""
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70, state="Damaged"), "sec-msft": _scan(score=95)},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "minervini_risk_exit_v1")
    ]


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = MinerviniStrategy()
    view = _UniverseView(_history(), _scan())
    without_universe = {
        name: value
        for name, value in PARAMETERS.items()
        if name != "selected_securities"
    }

    assert strategy.entry_signals(view, {**PARAMETERS, "selected_securities": []}) == []
    assert (
        strategy.entry_signals(view, {**PARAMETERS, "selected_securities": "sec-aapl"})
        == []
    )
    assert strategy.entry_signals(view, without_universe) == []


# ---------------------------------------------------------------------------
# #388 -- opt-in market-regime entry filter
# ---------------------------------------------------------------------------

from app.services.backtest.regime_filter import (  # noqa: E402
    REGIME_FILTER_BENCHMARK_PARAM,
    BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM,
    REGIME_FILTER_MA_LENGTH_PARAM,
)

_BENCHMARK_ID = "sec-spy"


class _RegimeView:
    """Wrap a contract ``_View`` and serve a crafted benchmark frame."""

    def __init__(self, inner: object, benchmark_closes: list[str]) -> None:
        self._inner = inner
        self.as_of_session = inner.as_of_session
        closes = [Decimal(value) for value in benchmark_closes]
        self._benchmark = pd.DataFrame(
            {
                "open": closes,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": closes,
            }
        )

    def price_history(
        self,
        security_id: str,
        *,
        limit: int | None = None,
        columns: object | None = None,
    ) -> pd.DataFrame:
        if security_id == _BENCHMARK_ID:
            history = self._benchmark.copy()
            if limit is not None:
                history = history.iloc[-limit:]
            if columns is not None:
                history = history.loc[:, list(columns)]
            return history
        return self._inner.price_history(security_id, limit=limit, columns=columns)

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        return self._inner.scan_result(security_id)


def _risk_off_params() -> dict[str, object]:
    return {
        **PARAMETERS,
        "selected_securities": ["sec-aapl", _BENCHMARK_ID],
        BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }


def test_regime_filter_absent_matches_explicitly_disabled() -> None:
    strategy = MinerviniStrategy()

    absent = strategy.entry_signals(_View(_history(), _scan()), PARAMETERS)
    disabled = strategy.entry_signals(
        _View(_history(), _scan()),
        {**PARAMETERS, BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: False},
    )

    assert len(absent) == 1
    assert absent == disabled


def test_regime_filter_suppresses_entries_but_not_exits_when_risk_off() -> None:
    strategy = MinerviniStrategy()
    params = _risk_off_params()
    gated = _RegimeView(_View(_history(), _scan(state="Damaged")), ["10", "10", "4"])
    plain = _View(_history(), _scan(state="Damaged"))

    assert strategy.entry_signals(gated, params) == []
    assert validate_exit_signals(
        strategy.exit_signals(gated, _portfolio(), params)
    ) == validate_exit_signals(strategy.exit_signals(plain, _portfolio(), params))


def test_regime_filter_fails_closed_when_benchmark_not_in_universe() -> None:
    strategy = MinerviniStrategy()
    params = {
        **PARAMETERS,
        BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }

    assert strategy.entry_signals(_View(_history(), _scan()), params) == []


def test_regime_filter_enabled_risk_on_does_not_alter_entries() -> None:
    """Gate permits: enabled + risk-on entries match the disabled path."""
    strategy = MinerviniStrategy()
    enabled = _risk_off_params()
    disabled = {**enabled, BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: False}

    assert strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), enabled
    ) == strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), disabled
    )


def test_evidence_requirements_declare_history_stage_and_vcp() -> None:
    """The declaration matches the rules' own guards (evidence contract v1)."""
    requirements = MinerviniStrategy().evidence_requirements(PARAMETERS)

    assert isinstance(requirements, StrategyEvidenceRequirementsV1)
    assert requirements.contract_version == EVIDENCE_CONTRACT_VERSION
    entry = {item.kind: item for item in requirements.entry}
    exit_ = {item.kind: item for item in requirements.exit}
    expected = {
        EvidenceKind.PRICE_HISTORY,
        EvidenceKind.SCAN_STAGE,
        EvidenceKind.SCAN_VCP,
    }
    assert set(entry) == expected
    assert set(exit_) == expected
    assert entry[EvidenceKind.PRICE_HISTORY].minimum_sessions == 51
    assert exit_[EvidenceKind.PRICE_HISTORY].minimum_sessions == 50


def test_absent_scan_evidence_never_creates_an_exit() -> None:
    """A scan record carrying no stage/VCP is missing evidence, not a failure."""
    empty = SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=date(2026, 7, 31),
        stage=None,
        vcp=None,
    )
    strategy = MinerviniStrategy()

    assert (
        strategy.exit_signals(_View(_history(), empty), _portfolio(), PARAMETERS) == []
    )


# ---------------------------------------------------------------------------
# #472 -- Strategy-owned structured explanations
# ---------------------------------------------------------------------------


def _codes(signal: Signal) -> tuple[str, ...]:
    assert signal.explanation is not None
    return signal.explanation.codes


def test_entry_explains_stage_pivot_volume_and_scores() -> None:
    strategy = MinerviniStrategy()

    entries = validate_entry_signals(
        strategy.entry_signals(_View(_history(), _scan()), PARAMETERS)
    )

    assert _codes(entries[0]) == (
        "entry_ranking",
        "stage2_confirmed",
        "trend_template",
        "vcp_breakout",
        "vcp_score",
        "volume_expansion",
    )


def test_damaged_vcp_exit_is_distinguishable_from_price_risk_exits() -> None:
    strategy = MinerviniStrategy()

    exits = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(), _scan(state="Damaged")), _portfolio(), PARAMETERS
        )
    )

    assert _codes(exits[0]) == ("vcp_state_invalidated",)


def test_stage_and_stop_loss_exits_carry_their_own_codes() -> None:
    strategy = MinerviniStrategy()

    stage_only = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(), _scan(stage="Stage 3")), _portfolio(), PARAMETERS
        )
    )
    stop_and_sma = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(current_close="80"), None), _portfolio(), PARAMETERS
        )
    )

    assert _codes(stage_only[0]) == ("stage_exit",)
    assert _codes(stop_and_sma[0]) == ("close_below_sma50", "maximum_loss_stop")


def test_upgrade_exit_explains_the_rotation() -> None:
    strategy = MinerviniStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history()},
        {"sec-aapl": _scan(score=70), "sec-msft": _scan(score=95)},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="0"), _upgrade_parameters())
    )

    assert _codes(exits[0]) == ("portfolio_upgrade",)


# --- GH-57: the Strategy's own stop level ------------------------------------

NEXT = date(2026, 8, 21)
EPSILON = Decimal("0.01")


def _closes(closes: list[Decimal], end: date = AS_OF) -> pd.DataFrame:
    sessions = pd.bdate_range(end=end, periods=len(closes))
    volumes = [Decimal("100")] * len(closes)
    return pd.DataFrame({"close": closes, "volume": volumes}, index=sessions)


def _stop(
    closes: list[Decimal], parameters: Mapping[str, object] = PARAMETERS
) -> StopLevelV1:
    view = _View(_closes(closes), _scan())
    stop = MinerviniStrategy().stop_level(view, _portfolio(), parameters, "sec-aapl")
    assert stop is not None
    return stop


def _exits_next_session(closes: list[Decimal], next_close: Decimal) -> bool:
    view = _View(_closes([*closes, next_close], NEXT), _scan())
    view.as_of_session = NEXT
    return bool(MinerviniStrategy().exit_signals(view, _portfolio(), PARAMETERS))


def test_stop_level_is_the_close_that_breaks_the_next_sessions_sma50() -> None:
    closes = [Decimal(100 + index) for index in range(60)]
    stop = _stop(closes)
    level = stop.level
    assert level is not None

    # The mean of the latest 49 closes, not today's SMA50 (134.5).
    assert stop.level == Decimal(135)
    assert stop.rule_code == "close_below_sma50"
    assert (stop.basis, stop.trigger) == ("mixed", "close_lt")
    assert _exits_next_session(closes, level - EPSILON)
    assert not _exits_next_session(closes, level + EPSILON)


def test_stop_level_is_the_maximum_loss_stop_when_it_binds() -> None:
    closes = [Decimal(90)] * 60
    stop = _stop(closes)
    level = stop.level
    assert level is not None

    assert stop.level == Decimal("92.00")
    assert stop.rule_code == "maximum_loss_stop"
    assert (stop.basis, stop.trigger) == ("mixed", "close_lte")
    assert stop.summary == "Max loss 8% from average cost"
    assert _exits_next_session(closes, level - EPSILON)
    assert not _exits_next_session(closes, level + EPSILON)


UNUSABLE_MAXIMUM_LOSS = (None, "x", float("nan"), -1, 100)


def test_stop_level_declares_an_unusable_maximum_loss() -> None:
    """Read as the exit reads it, with no default; checked before history."""
    missing = {k: v for k, v in PARAMETERS.items() if k != "maximum_loss_pct"}
    unusable = [
        PARAMETERS | {"maximum_loss_pct": value} for value in UNUSABLE_MAXIMUM_LOSS
    ]

    for parameters in [missing, *unusable]:
        for closes in ([Decimal(90)] * 60, [Decimal(90)] * 48):
            stop = _stop(closes, parameters)
            assert stop.level is None
            assert stop.summary == (
                "Strategy setting maximum_loss_pct is missing or unusable."
            )


def test_stop_level_declares_short_history_and_ignores_unheld() -> None:
    short = _stop([Decimal(90)] * 48)
    view = _View(_closes([Decimal(90)] * 60), _scan())

    assert short.level is None and short.rule_code == "insufficient_history"
    assert (
        MinerviniStrategy().stop_level(view, _portfolio("0"), PARAMETERS, "sec-aapl")
        is None
    )
