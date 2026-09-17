"""Shared run-level inputs report (#638)."""

from __future__ import annotations

from pathlib import Path

from typing import cast

import pytest

from app.schemas.market_regime import MarketRegimeSnapshotV1
from app.services.backtest import regime_filter
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.historical_price_evidence import (
    FX_PAIR,
    FX_SERIES_SECURITY_ID,
)
from app.services.backtest.strategy_evidence import (
    EvidenceKind,
    EvidenceRequirementV1,
    StrategyEvidenceRequirementsV1,
)
from app.services import evidence_shared_inputs
from app.services.evidence_shared_inputs import (
    SecurityCurrencyInputV1,
    StrategyImpactInputV1,
    build_shared_inputs_report,
    strategy_input_from,
)


_NON_PRICE_KIND = EvidenceKind.SCAN_STAGE


def _security(
    security_id: str, currency: str, sessions: int
) -> SecurityCurrencyInputV1:
    return SecurityCurrencyInputV1(
        security_id=security_id, currency=currency, sessions=sessions
    )


def _strategy(
    strategy_id: str = "s1",
    *,
    enabled: bool = True,
    ma_length: int | None = 200,
    entry: int = 0,
    exit_: int = 0,
) -> StrategyImpactInputV1:
    return StrategyImpactInputV1(
        strategy_id=strategy_id,
        display_name=strategy_id.upper(),
        regime_filter_enabled=enabled,
        benchmark_security_id="SPY",
        ma_length=ma_length,
        entry_minimum_sessions=entry,
        exit_minimum_sessions=exit_,
    )


_BENCHMARK = SecurityCurrencyInputV1(security_id="SPY", currency="GBP", sessions=252)


def _report(**kwargs: object):
    defaults: dict[str, object] = {
        "base_currency": "GBP",
        "securities": (),
        "strategies": (),
    }
    defaults.update(kwargs)
    return build_shared_inputs_report(**defaults)  # type: ignore[arg-type]


def test_fx_amplification_counts_non_base_securities() -> None:
    report = _report(
        securities=(
            _security("A", "USD", 252),
            _security("B", "USD", 252),
            _security("C", "USD", 252),
            _security("D", "GBP", 252),
        ),
        fx_sessions=120,
    )
    assert report.fx.securities_affected == 3
    assert report.fx.usable_session_ceiling == 120
    assert report.fx.available is True
    assert report.fx.pair == FX_PAIR
    assert report.fx.security_id == FX_SERIES_SECURITY_ID


def test_ceiling_reported_beside_own_count() -> None:
    report = _report(securities=(_security("A", "USD", 252),), fx_sessions=120)
    row = report.securities[0]
    assert (row.sessions, row.fx_ceiling, row.usable_sessions) == (252, 120, 120)
    assert row.fx_required is True


def test_base_currency_security_needs_no_fx() -> None:
    report = _report(securities=(_security("A", "GBP", 252),), fx_sessions=120)
    row = report.securities[0]
    assert row.fx_required is False
    assert row.fx_ceiling is None
    assert row.usable_sessions == 252


def test_fx_unavailable_degrades_every_non_base_security() -> None:
    """Without the series nothing converts, so the outage caps them all at 0."""
    report = _report(
        securities=(_security("A", "USD", 252), _security("B", "USD", 40)),
        fx_sessions=None,
    )
    assert report.fx.available is False
    assert report.fx.usable_session_ceiling is None
    assert report.fx.securities_affected == 2
    assert report.fx.securities_capped == 2
    assert [row.sessions for row in report.securities] == [252, 40]
    assert [row.usable_sessions for row in report.securities] == [0, 0]
    assert [row.fx_ceiling for row in report.securities] == [0, 0]


def test_fx_outage_leaves_no_security_eligible() -> None:
    """The amplification must reach the per-Strategy headline, not stop at the row."""
    report = _report(
        securities=(_security("A", "USD", 252), _security("B", "USD", 252)),
        strategies=(_strategy(enabled=False, entry=200, exit_=200),),
        fx_sessions=None,
    )
    assert report.strategies[0].eligible_entry == 0
    assert report.strategies[0].eligible_exit == 0


def test_benchmark_verdict_is_split_per_strategy() -> None:
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    report = _report(
        securities=(_BENCHMARK,),
        strategies=(
            _strategy("a", ma_length=200),
            _strategy("b", ma_length=220),
        ),
        snapshot=snapshot,
    )
    assert [row.benchmark_satisfied for row in report.strategies] == [True, False]


def test_disabled_regime_gate_is_unaffected() -> None:
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=10
    )
    report = _report(strategies=(_strategy(enabled=False),), snapshot=snapshot)
    row = report.strategies[0]
    assert row.regime_filter_enabled is False
    assert row.benchmark_satisfied is None


def test_missing_snapshot_leaves_verdict_unaffected() -> None:
    report = _report(strategies=(_strategy(),), snapshot=None)
    assert report.benchmark.available is False
    assert report.benchmark.generated_at == ""
    assert report.benchmark.session_count == 0
    assert report.strategies[0].benchmark_satisfied is None


def test_snapshot_generated_at_is_carried_verbatim() -> None:
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True,
        return_52w_pct=1.0,
        session_count=210,
        generated_at="2026-01-02T03:04:05+00:00",
    )
    report = _report(snapshot=snapshot)
    assert report.benchmark.generated_at == "2026-01-02T03:04:05+00:00"
    assert report.benchmark.available is True


def test_eligible_counts_use_declared_minimums() -> None:
    report = _report(
        securities=(
            _security("A", "GBP", 252),
            _security("B", "GBP", 120),
            _security("C", "GBP", 40),
        ),
        strategies=(_strategy(entry=220, exit_=51),),
    )
    row = report.strategies[0]
    assert row.eligible_entry == 1
    assert row.eligible_exit == 2


def test_unusable_ma_length_fails_closed_like_the_gate() -> None:
    """``entry_signals_permitted`` returns False here, so must the report."""
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    report = _report(strategies=(_strategy(ma_length=None),), snapshot=snapshot)
    assert report.strategies[0].benchmark_satisfied is False


def test_missing_benchmark_fails_closed() -> None:
    """A gate-enabled Strategy with no benchmark blocks every entry in a real run."""
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    row = StrategyImpactInputV1(
        strategy_id="s1",
        display_name="S1",
        regime_filter_enabled=True,
        benchmark_security_id=None,
        ma_length=200,
    )
    assert (
        _report(strategies=(row,), snapshot=snapshot).strategies[0].benchmark_satisfied
        is False
    )


def test_benchmark_outside_the_run_fails_closed() -> None:
    """The real gate rejects a benchmark absent from the universe."""
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    report = build_shared_inputs_report(
        base_currency="GBP",
        securities=(_security("A", "GBP", 252),),
        strategies=(_strategy(),),
        snapshot=snapshot,
    )
    assert report.strategies[0].benchmark_satisfied is False


def test_degraded_snapshot_cannot_satisfy_any_ma_length() -> None:
    """A degraded reading is surfaced and never reads as a passing benchmark."""
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=5000, is_degraded=True
    )
    report = _report(
        securities=(_BENCHMARK,),
        strategies=(_strategy(ma_length=200),),
        snapshot=snapshot,
    )
    assert report.benchmark.is_degraded is True
    assert report.strategies[0].benchmark_satisfied is False


def test_empty_roster_and_universe() -> None:
    report = _report()
    assert report.securities == ()
    assert report.strategies == ()
    assert report.fx.securities_affected == 0
    assert report.calendar.available is False


def test_calendar_identity_is_carried() -> None:
    report = _report(calendar_digest="abc123", calendar_expected_sessions=252)
    assert report.calendar.digest == "abc123"
    assert report.calendar.expected_sessions == 252
    assert report.calendar.available is True


def test_report_is_order_independent() -> None:
    securities = (
        _security("A", "USD", 252),
        _security("B", "GBP", 120),
        _security("C", "USD", 40),
    )
    strategies = (_strategy("a", entry=100), _strategy("b", entry=200))
    first = _report(securities=securities, strategies=strategies, fx_sessions=200)
    second = _report(
        securities=tuple(reversed(securities)),
        strategies=tuple(reversed(strategies)),
        fx_sessions=200,
    )
    assert first == second


def test_the_gate_is_never_evaluated() -> None:
    """AC: no gate evaluation. Monkeypatching cannot show this -- the module
    never references the function -- so assert on the source instead."""
    source = Path(evidence_shared_inputs.__file__).read_text()

    assert "entry_signals_permitted(" not in source
    assert "import entry_signals_permitted" not in source
    assert not hasattr(evidence_shared_inputs, "entry_signals_permitted")


def test_benchmark_verdict_holds_when_everything_is_usable() -> None:
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    report = _report(
        securities=(_security("A", "USD", 252), _BENCHMARK),
        strategies=(_strategy(),),
        fx_sessions=120,
        snapshot=snapshot,
    )
    assert report.strategies[0].benchmark_satisfied is True


class _Descriptor:
    """Minimal stand-in carrying only the fields the reader touches."""

    def __init__(self, parameters: dict[str, object]) -> None:
        self.strategy_id = "s1"
        self.display_name = "S1"
        self.default_parameters = parameters


def test_strategy_input_from_reads_parameters_and_minimums() -> None:
    descriptor = _Descriptor(
        {
            regime_filter.BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: True,
            regime_filter.REGIME_FILTER_BENCHMARK_PARAM: "SPY",
            regime_filter.REGIME_FILTER_MA_LENGTH_PARAM: 200,
        }
    )
    requirements = StrategyEvidenceRequirementsV1(
        entry=(
            EvidenceRequirementV1(
                kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=220
            ),
            EvidenceRequirementV1(kind=_NON_PRICE_KIND),
        ),
        exit=(
            EvidenceRequirementV1(kind=EvidenceKind.PRICE_HISTORY, minimum_sessions=51),
        ),
    )
    row = strategy_input_from(cast(StrategyDescriptorV1, descriptor), requirements)
    assert row.regime_filter_enabled is True
    assert row.benchmark_security_id == "SPY"
    assert row.ma_length == 200
    assert row.entry_minimum_sessions == 220
    assert row.exit_minimum_sessions == 51


@pytest.mark.parametrize("value", [True, "200", 1, None])
def test_strategy_input_from_rejects_unusable_ma_length(value: object) -> None:
    descriptor = _Descriptor(
        {
            regime_filter.BLOCK_BUY_ON_DOWNTREND_ENABLED_PARAM: True,
            regime_filter.REGIME_FILTER_MA_LENGTH_PARAM: value,
        }
    )
    row = strategy_input_from(
        cast(StrategyDescriptorV1, descriptor), StrategyEvidenceRequirementsV1()
    )
    assert row.ma_length is None
    assert row.benchmark_security_id is None


def test_calendar_states_how_many_securities_fall_short() -> None:
    """Each shared input must report the number of securities it affects."""
    report = _report(
        securities=(
            _security("A", "GBP", 252),
            _security("B", "GBP", 120),
            _security("C", "GBP", 40),
        ),
        calendar_digest="abc123",
        calendar_expected_sessions=252,
    )
    assert report.calendar.securities_affected == 2


def test_benchmark_states_how_many_strategies_depend_on_it() -> None:
    snapshot = MarketRegimeSnapshotV1(
        spy_uptrend=True, return_52w_pct=1.0, session_count=210
    )
    report = _report(
        strategies=(_strategy("a"), _strategy("b"), _strategy("c", enabled=False)),
        snapshot=snapshot,
    )
    assert report.benchmark.strategies_affected == 2


def test_capped_is_distinct_from_affected() -> None:
    """Depending on FX is not the same as being degraded by it."""
    report = _report(
        securities=(_security("A", "USD", 100), _security("B", "USD", 400)),
        fx_sessions=252,
    )
    assert report.fx.securities_affected == 2
    assert report.fx.securities_capped == 1


def test_zero_session_security_is_never_eligible() -> None:
    """A minimum of 0 must not make a security with no evidence eligible."""
    report = _report(
        securities=(_security("A", "GBP", 0), _security("B", "GBP", 10)),
        strategies=(_strategy(enabled=False, entry=0, exit_=0),),
    )
    assert report.strategies[0].eligible_entry == 1
    assert report.strategies[0].eligible_exit == 1


def test_currency_comparison_is_normalised() -> None:
    """``currency.py`` normalises before deciding; so must ``fx_required``."""
    report = _report(securities=(_security("A", " gbp ", 252),), base_currency="GBP")

    assert report.securities[0].fx_required is False
    assert report.securities[0].usable_sessions == 252


def test_minimum_ma_length_is_accepted_at_the_boundary() -> None:
    """The one-off separating unusable from usable, asserted in both directions."""
    from app.services.backtest.regime_filter import MIN_MA_LENGTH
    from app.services.evidence_shared_inputs import _usable_ma_length

    assert _usable_ma_length(MIN_MA_LENGTH) == MIN_MA_LENGTH
    assert _usable_ma_length(MIN_MA_LENGTH - 1) is None


# --- exit eligibility scope (#639 review P2) --------------------------


def test_exit_eligibility_is_restricted_to_holdings() -> None:
    """``eligible_exit`` counts holdings; ``eligible_entry`` the universe."""
    report = _report(
        securities=(
            _security("A", "GBP", 252),
            _security("B", "GBP", 252),
            _security("C", "GBP", 252),
        ),
        strategies=(_strategy(enabled=False, entry=100, exit_=100),),
        holding_ids=frozenset({"A"}),
    )
    strategy = report.strategies[0]
    assert strategy.eligible_entry == 3
    assert strategy.eligible_exit == 1


def test_holding_ids_none_keeps_the_universe_wide_exit_count() -> None:
    """Existing callers that pass no holdings are unaffected."""
    securities = (_security("A", "GBP", 252), _security("B", "GBP", 252))
    strategies = (_strategy(enabled=False, entry=100, exit_=100),)
    assert (
        _report(securities=securities, strategies=strategies)
        .strategies[0]
        .eligible_exit
        == 2
    )


def test_empty_holdings_yield_no_exit_eligibility() -> None:
    """A portfolio with nothing held has nothing eligible to exit."""
    report = _report(
        securities=(_security("A", "GBP", 252),),
        strategies=(_strategy(enabled=False, entry=1, exit_=1),),
        holding_ids=frozenset(),
    )
    assert report.strategies[0].eligible_entry == 1
    assert report.strategies[0].eligible_exit == 0


def test_holding_ids_do_not_narrow_the_benchmark_universe_check() -> None:
    """The benchmark's in-universe check still sees the whole run."""
    report = _report(
        securities=(_BENCHMARK, _security("A", "GBP", 252)),
        strategies=(_strategy(ma_length=200),),
        snapshot=MarketRegimeSnapshotV1(
            spy_uptrend=True, return_52w_pct=1.0, session_count=250
        ),
        holding_ids=frozenset({"A"}),
    )
    assert report.strategies[0].benchmark_satisfied is True
