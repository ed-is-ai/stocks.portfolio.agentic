"""Contract tests for the deterministic Weinstein backtest Strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd

from app.services.backtest.strategy_evidence import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceKind,
    StrategyEvidenceRequirementsV1,
)
from app.services.backtest.strategy_explanation import (
    ExplanationFactV1,
    SignalReasonV1,
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
SPEC = spec_from_file_location("weinstein_backtest_strategy_test", RUNTIME)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
WeinsteinStrategy = MODULE.WeinsteinStrategy

AS_OF = date(2026, 8, 20)
PARAMETERS = {
    "selected_securities": ["sec-aapl"],
    "breakout_lookback_sessions": 50,
    "minimum_relative_volume": 1.5,
    "maximum_loss_pct": 10.0,
}


def _history(
    *,
    current_close: str | None = None,
    current_high: str | None = None,
    current_volume: str = "150",
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=225)
    closes = [Decimal(100 + index) for index in range(225)]
    if current_close is not None:
        closes[-1] = Decimal(current_close)
    highs = list(closes)
    if current_high is not None:
        highs[-1] = Decimal(current_high)
    volumes = [Decimal("100")] * 224 + [Decimal(current_volume)]
    return pd.DataFrame(
        {"high": highs, "close": closes, "volume": volumes}, index=sessions
    )


def _scan(
    stage: str = "Stage 2", *, as_of: date = date(2026, 7, 31)
) -> SimpleNamespace:
    return SimpleNamespace(
        security_id="sec-aapl",
        as_of_session_date=as_of,
        stage=SimpleNamespace(value=stage),
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
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        history = self._history.copy()
        history = cast(
            pd.DataFrame,
            history.loc[
                [
                    (index.date() if hasattr(index, "date") else index)
                    <= self.as_of_session
                    for index in history.index
                ]
            ],
        )
        if limit is not None:
            history = cast(pd.DataFrame, history.iloc[-limit:, :])
        if columns is not None:
            history = cast(pd.DataFrame, history.loc[:, list(columns)])
        return cast(pd.DataFrame, history)

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


def test_stage2_entry_qualifies_at_inclusive_volume_boundary() -> None:
    strategy = WeinsteinStrategy()
    view = _View(_history(), _scan())

    first = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))
    second = validate_entry_signals(strategy.entry_signals(view, PARAMETERS))

    assert isinstance(strategy, StrategyProtocolV1)
    assert first == second
    assert [(item.side, item.rule_id) for item in first] == [
        (SignalSide.BUY, "weinstein_stage2_breakout_v1")
    ]


def test_entry_requires_strict_breakout_and_complete_current_history() -> None:
    strategy = WeinsteinStrategy()
    history = _history()
    equal_breakout = history.copy()
    equal_breakout.loc[equal_breakout.index[-1], "close"] = history["high"].iloc[-2]

    assert strategy.entry_signals(_View(equal_breakout, _scan()), PARAMETERS) == []
    assert strategy.entry_signals(
        _View(cast(pd.DataFrame, history.iloc[:-1, :]), _scan()), PARAMETERS
    ) == []
    assert strategy.entry_signals(
        _View(cast(pd.DataFrame, history.iloc[-203:, :]), _scan()), PARAMETERS
    ) == []


def test_entry_fails_closed_for_missing_future_or_invalid_volume_evidence() -> None:
    strategy = WeinsteinStrategy()

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


def test_non_stage2_exit_and_position_sizing_are_fail_closed() -> None:
    strategy = WeinsteinStrategy()
    view = _View(_history(), _scan("Stage 3"))
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
    strategy = WeinsteinStrategy()
    loss_view = _View(_history(current_close="89"), None)

    assert len(strategy.exit_signals(loss_view, _portfolio(), PARAMETERS)) == 1
    assert (
        strategy.exit_signals(
            _View(_history(), _scan("Stage 3")), _portfolio("0"), PARAMETERS
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
    strategy = WeinsteinStrategy()
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
    strategy = WeinsteinStrategy()

    signals = validate_entry_signals(
        strategy.entry_signals(
            _View(_history(), _scan()),
            {**PARAMETERS, "selected_securities": ["sec-msft", "sec-aapl"]},
        )
    )

    assert [signal.security_id for signal in signals] == ["sec-aapl"]


def test_multi_security_universe_exits_only_held_securities() -> None:
    strategy = WeinsteinStrategy()
    view = _UniverseView(_history(), _scan("Stage 3"))

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
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        history = self._histories.get(security_id, pd.DataFrame()).copy()
        if limit is not None:
            history = cast(pd.DataFrame, history.iloc[-limit:, :])
        if columns is not None:
            history = cast(pd.DataFrame, history.loc[:, list(columns)])
        return cast(pd.DataFrame, history)

    def scan_result(self, security_id: str) -> SimpleNamespace | None:
        scan = self._scans.get(security_id)
        if scan is None:
            return None
        return SimpleNamespace(**{**vars(scan), "security_id": security_id})


def _momentum_history(
    *,
    numerator: str = "110",
    denominator: str = "100",
    missing_endpoint: int | None = None,
    missing_reason: str = "fx_outside_coverage",
) -> pd.DataFrame:
    sessions = pd.bdate_range(end=AS_OF, periods=253)
    closes: list[Decimal | None] = [Decimal("100")] * 253
    reasons: list[str | None] = [None] * 253
    closes[-22] = Decimal(numerator)
    closes[-253] = Decimal(denominator)
    if missing_endpoint is not None:
        closes[missing_endpoint] = None
        reasons[missing_endpoint] = missing_reason
    return pd.DataFrame(
        {
            "close": closes,
            "reason": reasons,
            "source_currency": "GBP",
            "source_quote_unit": "GBP",
            "fx_rate": None,
            "fx_session": None,
            "fx_revision": None,
            "policy_version": "CurrencyConversionPolicyV1",
        },
        index=sessions,
    )


class _RankedView(_KeyedView):
    base_currency = "GBP"

    def __init__(
        self,
        histories: dict[str, pd.DataFrame],
        momentum: dict[str, pd.DataFrame],
    ) -> None:
        super().__init__(histories, {key: _scan() for key in histories})
        self._momentum = momentum

    def base_currency_close_history(
        self, security_id: str, *, limit: int
    ) -> pd.DataFrame:
        assert limit == 253
        return cast(
            pd.DataFrame, self._momentum[security_id].iloc[-limit:, :].copy()
        )


def _ranking_reason(signal: Signal) -> SignalReasonV1:
    assert signal.explanation is not None
    return next(
        reason
        for reason in signal.explanation.reasons
        if reason.code == "entry_ranking"
    )


def _fact(reason: SignalReasonV1, label: str) -> ExplanationFactV1:
    return next(fact for fact in reason.facts if fact.label == label)


def test_entry_ranking_prefers_momentum_over_identifier_and_explains_endpoints() -> None:
    low_id, high_id = "sec-a", "sec-z"
    view = _RankedView(
        {low_id: _history(), high_id: _history()},
        {
            low_id: _momentum_history(numerator="120"),
            high_id: _momentum_history(numerator="150"),
        },
    )

    signals = WeinsteinStrategy().entry_signals(
        view, {**PARAMETERS, "selected_securities": [high_id, low_id]}
    )
    by_id = {signal.security_id: signal for signal in signals}
    ranking = _ranking_reason(by_id[high_id])
    dates = pd.bdate_range(end=AS_OF, periods=253)

    assert by_id[high_id].priority == Decimal("2")
    assert by_id[low_id].priority == Decimal("1")
    assert _fact(ranking, "Momentum").observed == Decimal("0.5")
    assert (
        _fact(ranking, "Momentum numerator session").observed
        == pd.Timestamp(dates[-22]).date().isoformat()
    )
    assert (
        _fact(ranking, "Momentum denominator session").observed
        == pd.Timestamp(dates[-253]).date().isoformat()
    )
    assert _fact(ranking, "Score currency").observed == "GBP"


def test_relative_volume_breaks_momentum_ties_then_identifier_breaks_exact_ties() -> None:
    ids = ("sec-a", "sec-b", "sec-c")
    view = _RankedView(
        {
            "sec-a": _history(current_volume="200"),
            "sec-b": _history(current_volume="200"),
            "sec-c": _history(current_volume="300"),
        },
        {security_id: _momentum_history() for security_id in ids},
    )

    signals = WeinsteinStrategy().entry_signals(
        view, {**PARAMETERS, "selected_securities": list(reversed(ids))}
    )
    priorities = {signal.security_id: signal.priority for signal in signals}

    assert priorities == {
        "sec-c": Decimal("3"),
        "sec-a": Decimal("2"),
        "sec-b": Decimal("1"),
    }
    assert (
        _fact(_ranking_reason(signals[0]), "Relative volume").observed
        == Decimal("3")
    )


def test_valid_negative_momentum_ranks_ahead_of_fx_missing() -> None:
    missing_id, valid_id = "sec-a-missing", "sec-z-valid"
    view = _RankedView(
        {valid_id: _history(), missing_id: _history()},
        {
            missing_id: _momentum_history(
                missing_endpoint=-253, missing_reason="fx_outside_coverage"
            ),
            valid_id: _momentum_history(numerator="90"),
        },
    )

    signals = WeinsteinStrategy().entry_signals(
        view, {**PARAMETERS, "selected_securities": [missing_id, valid_id]}
    )
    by_id = {signal.security_id: signal for signal in signals}

    assert by_id[missing_id].priority == Decimal("1")
    assert by_id[valid_id].priority == Decimal("2")
    assert (
        _fact(_ranking_reason(by_id[valid_id]), "Momentum").observed
        == Decimal("-0.1")
    )
    assert (
        _fact(_ranking_reason(by_id[missing_id]), "Momentum unavailable reason").observed
        == "fx_outside_coverage"
    )


def test_relative_volume_breaks_ties_between_two_missing_momentum_values() -> None:
    ids = ("sec-a", "sec-z")
    view = _RankedView(
        {
            "sec-a": _history(current_volume="200"),
            "sec-z": _history(current_volume="300"),
        },
        {
            security_id: _momentum_history(
                missing_endpoint=-253, missing_reason="fx_outside_coverage"
            )
            for security_id in ids
        },
    )

    signals = WeinsteinStrategy().entry_signals(
        view, {**PARAMETERS, "selected_securities": list(ids)}
    )
    priorities = {signal.security_id: signal.priority for signal in signals}

    assert priorities == {"sec-z": Decimal("2"), "sec-a": Decimal("1")}


def test_momentum_uses_253_canonical_rows_and_excludes_t_through_t_minus_20() -> None:
    security_id = "sec-aapl"
    baseline = _momentum_history(numerator="120", denominator="100")
    changed_recent = baseline.copy()
    changed_recent.iloc[-21:, changed_recent.columns.get_loc("close")] = Decimal("900")
    view = _RankedView(
        {security_id: _history()},
        {security_id: baseline},
    )
    changed_view = _RankedView(
        {security_id: _history()},
        {security_id: changed_recent},
    )
    parameters = {**PARAMETERS, "selected_securities": [security_id]}

    original = WeinsteinStrategy().entry_signals(view, parameters)[0]
    changed = WeinsteinStrategy().entry_signals(changed_view, parameters)[0]

    assert _fact(_ranking_reason(original), "Momentum").observed == Decimal("0.2")
    assert _fact(_ranking_reason(changed), "Momentum").observed == Decimal("0.2")


def test_missing_optional_currency_history_preserves_entry_with_reason() -> None:
    signal = WeinsteinStrategy().entry_signals(
        _View(_history(), _scan()), PARAMETERS
    )[0]

    assert signal.priority == Decimal("1")
    assert _fact(_ranking_reason(signal), "Ranking policy").observed == (
        "weinstein_momentum_relative_volume_v1"
    )
    assert _fact(_ranking_reason(signal), "Candidate count").observed == Decimal("1")
    assert _fact(_ranking_reason(signal), "Ordinal rank").observed == Decimal("1")
    assert _fact(_ranking_reason(signal), "Encoded priority").observed == Decimal("1")
    assert (
        _fact(_ranking_reason(signal), "Momentum unavailable reason").observed
        == "base_currency_history_unavailable"
    )


def test_short_history_and_invalid_momentum_endpoints_keep_qualifying_entry() -> None:
    security_id = "sec-aapl"
    full = _momentum_history()
    short = cast(pd.DataFrame, full.iloc[1:, :].copy())
    invalid_numerator = full.copy()
    invalid_numerator.iloc[-22, invalid_numerator.columns.get_loc("close")] = (
        Decimal("NaN")
    )
    invalid_denominator = full.copy()
    invalid_denominator.iloc[-253, invalid_denominator.columns.get_loc("close")] = (
        Decimal("0")
    )

    cases = (
        (short, "insufficient_price_history"),
        (invalid_numerator, "invalid_momentum_endpoint"),
        (invalid_denominator, "invalid_momentum_endpoint"),
    )
    for history, expected_reason in cases:
        view = _RankedView(
            {security_id: _history()}, {security_id: history}
        )
        signals = WeinsteinStrategy().entry_signals(
            view, {**PARAMETERS, "selected_securities": [security_id]}
        )

        assert len(signals) == 1
        assert signals[0].priority == Decimal("1")
        assert (
            _fact(
                _ranking_reason(signals[0]), "Momentum unavailable reason"
            ).observed
            == expected_reason
        )


def _upgrade_parameters(**overrides: object) -> dict[str, object]:
    return {
        **PARAMETERS,
        "selected_securities": ["sec-aapl", "sec-msft"],
        "enable_position_upgrade": True,
        "upgrade_score_margin_pct": 10.0,
        **overrides,
    }


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
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view,
        _held_portfolio(cash="1"),
        {**PARAMETERS, "selected_securities": ["sec-aapl", "sec-msft"]},
    )

    assert exits == []


def test_upgrade_exit_sells_weakest_position_when_margin_cleared() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="0"), _upgrade_parameters())
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "weinstein_upgrade_exit_v1")
    ]


def test_upgrade_exit_keeps_a_cash_slot_for_engine_owned_buy_allocation() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="100000"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_does_not_fire_when_margin_not_cleared() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="325")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert exits == []


def test_upgrade_exit_never_duplicates_a_position_already_exiting_on_its_own_rule() -> (
    None
):
    """The held position is already exiting via its own Stage 3 rule --
    the upgrade check must not also emit a second SELL for the same
    security."""
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan("Stage 3"), "sec-msft": _scan()},
    )

    exits = strategy.exit_signals(
        view, _held_portfolio(cash="1"), _upgrade_parameters()
    )

    assert [(s.security_id, s.rule_id) for s in exits] == [
        ("sec-aapl", "weinstein_stage_exit_v1")
    ]


def test_empty_or_malformed_universe_emits_nothing() -> None:
    strategy = WeinsteinStrategy()
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

    def __init__(self, inner: _View, benchmark_closes: list[str]) -> None:
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
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        if security_id == _BENCHMARK_ID:
            history = self._benchmark.copy()
            if limit is not None:
                history = cast(pd.DataFrame, history.iloc[-limit:, :])
            if columns is not None:
                history = cast(pd.DataFrame, history.loc[:, list(columns)])
            return cast(pd.DataFrame, history)
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
    strategy = WeinsteinStrategy()

    absent = strategy.entry_signals(_View(_history(), _scan()), PARAMETERS)
    disabled = strategy.entry_signals(
        _View(_history(), _scan()),
        {**PARAMETERS, BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: False},
    )

    assert len(absent) == 1
    assert absent == disabled


def test_regime_filter_suppresses_entries_but_not_exits_when_risk_off() -> None:
    strategy = WeinsteinStrategy()
    params = _risk_off_params()
    gated = _RegimeView(_View(_history(), _scan("Stage 3")), ["10", "10", "4"])
    plain = _View(_history(), _scan("Stage 3"))

    assert strategy.entry_signals(gated, params) == []
    assert validate_exit_signals(
        strategy.exit_signals(gated, _portfolio(), params)
    ) == validate_exit_signals(strategy.exit_signals(plain, _portfolio(), params))


def test_regime_filter_fails_closed_when_benchmark_not_in_universe() -> None:
    strategy = WeinsteinStrategy()
    params = {
        **PARAMETERS,
        BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: _BENCHMARK_ID,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }

    assert strategy.entry_signals(_View(_history(), _scan()), params) == []


def test_regime_filter_enabled_risk_on_does_not_alter_entries() -> None:
    """Gate permits: enabled + risk-on entries match the disabled path."""
    strategy = WeinsteinStrategy()
    enabled = _risk_off_params()
    disabled = {**enabled, BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: False}

    assert strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), enabled
    ) == strategy.entry_signals(
        _RegimeView(_View(_history(), _scan()), ["1", "1", "100"]), disabled
    )


def test_evidence_requirements_declare_history_and_stage() -> None:
    """The declaration matches the rules' own guards (evidence contract v1)."""
    requirements = WeinsteinStrategy().evidence_requirements(PARAMETERS)

    assert isinstance(requirements, StrategyEvidenceRequirementsV1)
    assert requirements.contract_version == EVIDENCE_CONTRACT_VERSION
    entry = {item.kind: item for item in requirements.entry}
    exit_ = {item.kind: item for item in requirements.exit}
    assert set(entry) == {EvidenceKind.PRICE_HISTORY, EvidenceKind.SCAN_STAGE}
    assert set(exit_) == {EvidenceKind.PRICE_HISTORY, EvidenceKind.SCAN_STAGE}
    assert entry[EvidenceKind.PRICE_HISTORY].minimum_sessions == 220
    assert exit_[EvidenceKind.PRICE_HISTORY].minimum_sessions == 150
    deeper = WeinsteinStrategy().evidence_requirements(
        {**PARAMETERS, "breakout_lookback_sessions": 300}
    )
    assert deeper.entry[0].minimum_sessions == 301


def test_absent_stage_evidence_never_creates_an_exit() -> None:
    """A scan record carrying no stage is missing evidence, not a failure."""
    stageless = SimpleNamespace(
        security_id="sec-aapl", as_of_session_date=date(2026, 7, 31), stage=None
    )
    strategy = WeinsteinStrategy()

    assert (
        strategy.exit_signals(_View(_history(), stageless), _portfolio(), PARAMETERS)
        == []
    )


# ---------------------------------------------------------------------------
# #472 -- Strategy-owned structured explanations
# ---------------------------------------------------------------------------


def _codes(signal: Signal) -> tuple[str, ...]:
    assert signal.explanation is not None
    return signal.explanation.codes


def test_entry_explains_stage_breakout_and_volume() -> None:
    strategy = WeinsteinStrategy()

    entries = validate_entry_signals(
        strategy.entry_signals(_View(_history(), _scan()), PARAMETERS)
    )

    assert _codes(entries[0]) == (
        "breakout_above_prior_high",
        "entry_ranking",
        "stage2_confirmed",
        "volume_expansion",
    )


def test_stage_exit_and_stop_loss_exit_are_distinguishable() -> None:
    strategy = WeinsteinStrategy()

    stage_only = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(), _scan("Stage 3")), _portfolio(), PARAMETERS
        )
    )
    stop_only = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(current_close="89"), None), _portfolio(), PARAMETERS
        )
    )

    assert "stage_exit" in _codes(stage_only[0])
    assert "maximum_loss_stop" not in _codes(stage_only[0])
    assert "maximum_loss_stop" in _codes(stop_only[0])
    assert "stage_exit" not in _codes(stop_only[0])


def test_simultaneously_true_exit_conditions_all_appear_once() -> None:
    strategy = WeinsteinStrategy()

    exits = validate_exit_signals(
        strategy.exit_signals(
            _View(_history(current_close="89"), _scan("Stage 3")),
            _portfolio(),
            PARAMETERS,
        )
    )

    assert _codes(exits[0]) == (
        "close_below_sma150",
        "maximum_loss_stop",
        "stage_exit",
    )


def test_upgrade_exit_explains_the_rotation() -> None:
    strategy = WeinsteinStrategy()
    view = _KeyedView(
        {"sec-aapl": _history(), "sec-msft": _history(current_close="450")},
        {"sec-aapl": _scan(), "sec-msft": _scan()},
    )

    exits = validate_exit_signals(
        strategy.exit_signals(view, _held_portfolio(cash="0"), _upgrade_parameters())
    )

    assert _codes(exits[0]) == ("portfolio_upgrade",)
    assert exits[0].explanation is not None
    assert len(exits[0].explanation.reasons[0].facts) == 4


# --- GH-57: the Strategy's own stop level ------------------------------------

NEXT = date(2026, 8, 21)
EPSILON = Decimal("0.01")


def _closes(closes: list[Decimal], end: date = AS_OF) -> pd.DataFrame:
    sessions = pd.bdate_range(end=end, periods=len(closes))
    return pd.DataFrame({"close": closes}, index=sessions)


def _stop(
    closes: list[Decimal], parameters: Mapping[str, object] = PARAMETERS
) -> StopLevelV1:
    view = _View(_closes(closes), _scan())
    stop = WeinsteinStrategy().stop_level(view, _portfolio(), parameters, "sec-aapl")
    assert stop is not None
    return stop


def _exits_next_session(closes: list[Decimal], next_close: Decimal) -> bool:
    view = _View(_closes([*closes, next_close], NEXT), _scan())
    view.as_of_session = NEXT
    return bool(WeinsteinStrategy().exit_signals(view, _portfolio(), PARAMETERS))


def test_stop_level_is_the_close_that_breaks_the_next_sessions_sma150() -> None:
    closes = [Decimal(100 + index) for index in range(160)]
    stop = _stop(closes)
    level = stop.level
    assert level is not None

    # The mean of the latest 149 closes, not today's SMA150 (184.5).
    assert stop.level == Decimal(185)
    assert stop.rule_code == "close_below_sma150"
    assert (stop.basis, stop.trigger) == ("mixed", "close_lt")
    assert _exits_next_session(closes, level - EPSILON)
    assert not _exits_next_session(closes, level + EPSILON)


def test_stop_level_is_the_maximum_loss_stop_when_it_binds() -> None:
    closes = [Decimal(85)] * 160
    stop = _stop(closes)
    level = stop.level
    assert level is not None

    assert stop.level == Decimal("90.00")
    assert stop.rule_code == "maximum_loss_stop"
    assert (stop.basis, stop.trigger) == ("mixed", "close_lte")
    assert stop.summary == "Max loss 10% from average cost"
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
        for closes in ([Decimal(85)] * 160, [Decimal(85)] * 148):
            stop = _stop(closes, parameters)
            assert stop.level is None
            assert stop.summary == (
                "Strategy setting maximum_loss_pct is missing or unusable."
            )


def test_stop_level_declares_short_history_and_ignores_unheld() -> None:
    short = _stop([Decimal(85)] * 148)
    view = _View(_closes([Decimal(85)] * 160), _scan())

    assert short.level is None and short.rule_code == "insufficient_history"
    assert (
        WeinsteinStrategy().stop_level(view, _portfolio("0"), PARAMETERS, "sec-aapl")
        is None
    )
