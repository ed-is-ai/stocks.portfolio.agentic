"""Story 2.1 coverage: every I/O-matrix row for the Strategy protocol
boundary, plus determinism and no-persistence-leakage checks.

Also covers Story 2.2's :func:`validate_strategy_parameters` -- the single
shared authority Skill discovery, a future engine launch, and a future UI
all reuse verbatim.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import math

import pytest
from pydantic import ValidationError

from app.services.backtest.strategy_protocol import (
    MarketViewV1,
    ParameterFieldErrorV1,
    ParameterValidationErrorCode,
    PortfolioView,
    PositionSummaryV1,
    Signal,
    SignalSide,
    StrategyParameterV1,
    StrategyProtocolError,
    StrategyProtocolErrorCode,
    StrategyProtocolV1,
    VolatilityObservationV1,
    validate_entry_signals,
    validate_exit_signals,
    validate_position_size,
    validate_strategy_parameters,
)


def _signal(
    *,
    security_id: str = "sec-aapl",
    side: SignalSide = SignalSide.BUY,
    session: date = date(2026, 6, 1),
    rule_id: str = "rule-1",
) -> Signal:
    return Signal(security_id=security_id, side=side, session=session, rule_id=rule_id)


def _portfolio(
    *,
    as_of_session: date = date(2026, 6, 1),
    cash: Decimal = Decimal("1000"),
    positions: tuple[PositionSummaryV1, ...] = (),
    volatility_observations: tuple[VolatilityObservationV1, ...] = (),
) -> PortfolioView:
    return PortfolioView(
        as_of_session=as_of_session,
        base_currency="GBP",
        cash=cash,
        positions=positions,
        volatility_observations=volatility_observations,
    )


# ---------------------------------------------------------------------------
# Signal / sort key
# ---------------------------------------------------------------------------


def test_signal_sort_key_orders_by_session_security_sell_before_buy_rule() -> None:
    sell = _signal(security_id="sec-aapl", side=SignalSide.SELL, rule_id="r")
    buy = _signal(security_id="sec-aapl", side=SignalSide.BUY, rule_id="r")
    earlier_session = _signal(session=date(2026, 5, 1))

    assert earlier_session.sort_key < sell.sort_key
    assert sell.sort_key < buy.sort_key


def test_signal_sort_key_is_pure_and_reproducible() -> None:
    first = _signal()
    second = _signal()

    assert first.sort_key == second.sort_key


def test_signal_rejects_blank_security_id_and_rule_id() -> None:
    with pytest.raises(ValidationError):
        _signal(security_id="")
    with pytest.raises(ValidationError):
        _signal(rule_id="")


# ---------------------------------------------------------------------------
# Valid signal batch -> deterministic order across repeated / reordered calls
# ---------------------------------------------------------------------------


def test_validate_entry_signals_accepts_well_formed_batch_in_deterministic_order() -> (
    None
):
    a = _signal(security_id="sec-a", side=SignalSide.BUY, rule_id="ra")
    b = _signal(security_id="sec-b", side=SignalSide.SELL, rule_id="rb")
    c = _signal(security_id="sec-a", side=SignalSide.SELL, rule_id="rc")

    first_order = validate_entry_signals([a, b, c])
    second_order = validate_entry_signals([c, a, b])
    third_order = validate_entry_signals([b, c, a])

    assert first_order == second_order == third_order
    assert [s.rule_id for s in first_order] == ["rc", "ra", "rb"]


def test_validate_exit_signals_accepts_well_formed_batch() -> None:
    signals = [_signal(security_id="sec-a"), _signal(security_id="sec-b")]

    result = validate_exit_signals(signals)

    assert len(result) == 2
    assert all(isinstance(item, Signal) for item in result)


# ---------------------------------------------------------------------------
# Malformed signal container / element
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("validator", [validate_entry_signals, validate_exit_signals])
@pytest.mark.parametrize("container", [(), {"a": 1}, "not-a-list", None, 5])
def test_validate_signal_rejects_non_list_container(validator, container) -> None:
    with pytest.raises(StrategyProtocolError) as excinfo:
        validator(container)

    assert excinfo.value.code == StrategyProtocolErrorCode.INVALID_SIGNAL_CONTAINER


@pytest.mark.parametrize("validator", [validate_entry_signals, validate_exit_signals])
def test_validate_signal_rejects_non_signal_element(validator) -> None:
    with pytest.raises(StrategyProtocolError) as excinfo:
        validator([_signal(), {"security_id": "sec-x"}])

    assert excinfo.value.code == StrategyProtocolErrorCode.INVALID_SIGNAL_ELEMENT


def test_validate_signal_batch_rejects_before_mutation_hook_semantics() -> None:
    """The validator must fail *before* returning anything a caller could
    act on -- there is no partial/valid-prefix result on rejection."""
    with pytest.raises(StrategyProtocolError):
        validate_entry_signals([_signal(), 42])


# ---------------------------------------------------------------------------
# PortfolioView: future-dated volatility observation
# ---------------------------------------------------------------------------


def test_portfolio_view_rejects_future_dated_volatility_observation() -> None:
    as_of = date(2026, 6, 1)
    future_observation = VolatilityObservationV1(
        security_id="sec-aapl", session=date(2026, 6, 2), value=Decimal("0.2")
    )

    with pytest.raises(StrategyProtocolError) as excinfo:
        _portfolio(as_of_session=as_of, volatility_observations=(future_observation,))

    assert excinfo.value.code == StrategyProtocolErrorCode.FUTURE_DATED_OBSERVATION


def test_portfolio_view_accepts_observation_dated_on_the_bound() -> None:
    as_of = date(2026, 6, 1)
    on_bound = VolatilityObservationV1(
        security_id="sec-aapl", session=as_of, value=Decimal("0.2")
    )

    view = _portfolio(as_of_session=as_of, volatility_observations=(on_bound,))

    assert view.volatility_observations == (on_bound,)


# ---------------------------------------------------------------------------
# PortfolioView: duplicate entries
# ---------------------------------------------------------------------------


def test_portfolio_view_rejects_duplicate_position_security_id() -> None:
    duplicate = tuple(
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("1"), average_cost=Decimal("10")
        )
        for _ in range(2)
    )

    with pytest.raises(StrategyProtocolError) as excinfo:
        _portfolio(positions=duplicate)

    assert excinfo.value.code == StrategyProtocolErrorCode.DUPLICATE_POSITION


def test_portfolio_view_rejects_duplicate_volatility_observation() -> None:
    as_of = date(2026, 6, 1)
    duplicate = tuple(
        VolatilityObservationV1(
            security_id="sec-aapl", session=as_of, value=Decimal("0.2")
        )
        for _ in range(2)
    )

    with pytest.raises(StrategyProtocolError) as excinfo:
        _portfolio(as_of_session=as_of, volatility_observations=duplicate)

    assert (
        excinfo.value.code == StrategyProtocolErrorCode.DUPLICATE_VOLATILITY_OBSERVATION
    )


# ---------------------------------------------------------------------------
# PortfolioView / PositionSummaryV1: non-negative domain
# ---------------------------------------------------------------------------


def test_portfolio_view_rejects_negative_cash() -> None:
    with pytest.raises(ValidationError):
        _portfolio(cash=Decimal("-1"))


def test_position_summary_rejects_negative_quantity() -> None:
    with pytest.raises(ValidationError):
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("-1"), average_cost=Decimal("10")
        )


def test_position_summary_rejects_non_positive_average_cost() -> None:
    with pytest.raises(ValidationError):
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("1"), average_cost=Decimal("0")
        )


# ---------------------------------------------------------------------------
# PortfolioView: non-finite values
#
# ``_StrategyModel``'s ``allow_inf_nan=False`` config rejects NaN/Infinity
# at pydantic-core's own type-validation layer, before any field validator
# runs, so these raise the standard ``pydantic.ValidationError`` rather
# than a ``StrategyProtocolError`` -- there is no dedicated I/O-matrix row
# requiring a bespoke code for this case (unlike future-dated observations).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity")])
def test_portfolio_view_rejects_non_finite_cash(bad_value: Decimal) -> None:
    with pytest.raises(ValidationError):
        _portfolio(cash=bad_value)


@pytest.mark.parametrize("bad_value", [Decimal("NaN"), Decimal("Infinity")])
def test_portfolio_view_rejects_non_finite_position_quantity(
    bad_value: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        PositionSummaryV1(
            security_id="sec-aapl", quantity=bad_value, average_cost=Decimal("10")
        )


def test_volatility_observation_rejects_non_finite_value() -> None:
    with pytest.raises(ValidationError):
        VolatilityObservationV1(
            security_id="sec-aapl", session=date(2026, 6, 1), value=Decimal("NaN")
        )


# ---------------------------------------------------------------------------
# Boolean / negative / non-integral position size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_size", [True, False])
def test_validate_position_size_rejects_bool(bad_size: bool) -> None:
    with pytest.raises(StrategyProtocolError) as excinfo:
        validate_position_size(bad_size)

    assert excinfo.value.code == StrategyProtocolErrorCode.INVALID_POSITION_SIZE_TYPE


def test_validate_position_size_rejects_negative() -> None:
    with pytest.raises(StrategyProtocolError) as excinfo:
        validate_position_size(-5)

    assert excinfo.value.code == StrategyProtocolErrorCode.NEGATIVE_POSITION_SIZE


@pytest.mark.parametrize("bad_size", [5.5, "5", None, Decimal("5")])
def test_validate_position_size_rejects_non_integral(bad_size: object) -> None:
    with pytest.raises(StrategyProtocolError) as excinfo:
        validate_position_size(bad_size)

    assert excinfo.value.code == StrategyProtocolErrorCode.INVALID_POSITION_SIZE_TYPE


def test_validate_position_size_accepts_valid_int() -> None:
    assert validate_position_size(10) == 10
    assert validate_position_size(0) == 0


# ---------------------------------------------------------------------------
# Caller mutation after construction cannot affect the view
# ---------------------------------------------------------------------------


def test_portfolio_view_detaches_caller_supplied_position_list() -> None:
    positions = [
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("10"), average_cost=Decimal("100")
        )
    ]

    view = _portfolio(positions=tuple(positions))
    positions.append(
        PositionSummaryV1(
            security_id="sec-msft", quantity=Decimal("5"), average_cost=Decimal("50")
        )
    )
    positions.clear()

    assert len(view.positions) == 1
    assert view.positions[0].security_id == "sec-aapl"


def test_portfolio_view_accepts_and_detaches_a_plain_list_input() -> None:
    raw_positions = [
        PositionSummaryV1(
            security_id="sec-aapl", quantity=Decimal("10"), average_cost=Decimal("100")
        )
    ]

    view = PortfolioView(
        as_of_session=date(2026, 6, 1),
        base_currency="GBP",
        cash=Decimal("100"),
        positions=raw_positions,
        volatility_observations=[],
    )
    raw_positions.clear()

    assert len(view.positions) == 1


def test_signal_and_position_summary_are_frozen() -> None:
    signal = _signal()
    with pytest.raises(ValidationError):
        signal.security_id = "other"  # type: ignore[misc]

    position = PositionSummaryV1(
        security_id="sec-aapl", quantity=Decimal("10"), average_cost=Decimal("100")
    )
    with pytest.raises(ValidationError):
        position.quantity = Decimal("20")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# No persistence leakage / no live identifiers
# ---------------------------------------------------------------------------


def test_portfolio_view_rejects_unexpected_fields_like_a_live_account_id() -> None:
    with pytest.raises(ValidationError):
        PortfolioView(
            as_of_session=date(2026, 6, 1),
            base_currency="GBP",
            cash=Decimal("100"),
            positions=(),
            volatility_observations=(),
            sipp_account_id="live-account-123",  # type: ignore[call-arg]
        )


def test_signal_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        Signal(
            security_id="sec-aapl",
            side=SignalSide.BUY,
            session=date(2026, 6, 1),
            rule_id="r",
            trade_id="live-trade-1",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class _ConformingStrategy:
    def entry_signals(self, view, parameters):  # noqa: ANN001, ANN201
        return []

    def exit_signals(self, view, portfolio, parameters):  # noqa: ANN001, ANN201
        return []

    def position_size(self, signal, view, portfolio, parameters):  # noqa: ANN001, ANN201
        return 1


class _NonConformingStrategy:
    def entry_signals(self, view, parameters):  # noqa: ANN001, ANN201
        return []


def test_strategy_protocol_v1_runtime_checkable_conformance() -> None:
    assert isinstance(_ConformingStrategy(), StrategyProtocolV1)
    assert not isinstance(_NonConformingStrategy(), StrategyProtocolV1)


def test_portfolio_view_satisfies_market_view_v1_structurally() -> None:
    view = _portfolio()
    assert isinstance(view, MarketViewV1)


# ---------------------------------------------------------------------------
# StrategyParameterV1 -- schema-authoring invariants (Story 2.2)
# ---------------------------------------------------------------------------


def _param(**overrides: object) -> StrategyParameterV1:
    defaults: dict[str, object] = dict(
        name="n",
        type="integer",
        default=1,
        description="d",
        required=True,
    )
    defaults.update(overrides)
    return StrategyParameterV1(**defaults)  # type: ignore[arg-type]


def test_strategy_parameter_is_frozen() -> None:
    parameter = _param()
    with pytest.raises(ValidationError):
        parameter.default = 2  # type: ignore[misc]


def test_strategy_parameter_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        _param(min_length=1)  # type: ignore[call-arg]


def test_strategy_parameter_rejects_minimum_on_non_numeric_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="string", default="x", minimum=1)


def test_strategy_parameter_rejects_maximum_on_non_numeric_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="boolean", default=True, maximum=1)


def test_strategy_parameter_rejects_minimum_greater_than_maximum() -> None:
    with pytest.raises(ValidationError):
        _param(minimum=10, maximum=1)


def test_strategy_parameter_allows_minimum_equal_maximum() -> None:
    parameter = _param(minimum=5, maximum=5, default=5)
    assert parameter.minimum == parameter.maximum == 5


def test_strategy_parameter_rejects_bool_minimum() -> None:
    with pytest.raises(ValidationError):
        _param(minimum=True)


def test_strategy_parameter_rejects_non_finite_minimum() -> None:
    with pytest.raises(ValidationError):
        _param(type="number", default=1.0, minimum=math.nan)


def test_strategy_parameter_rejects_enum_values_on_non_enum_type() -> None:
    with pytest.raises(ValidationError):
        _param(enum_values=("a", "b"))


def test_strategy_parameter_requires_non_empty_enum_values() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default="a", enum_values=())


def test_strategy_parameter_rejects_null_enum_value() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default="a", enum_values=("a", None))


def test_strategy_parameter_rejects_heterogeneous_enum_values() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default=1, enum_values=(1, "a"))


def test_strategy_parameter_rejects_bool_int_enum_leakage() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default=1, enum_values=(1, True))


def test_strategy_parameter_rejects_duplicate_enum_values() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default="a", enum_values=("a", "a"))


def test_strategy_parameter_rejects_default_not_in_enum_values() -> None:
    with pytest.raises(ValidationError):
        _param(type="enum", default="c", enum_values=("a", "b"))


def test_strategy_parameter_enum_default_must_type_match_not_just_equal() -> None:
    # ``True`` must never be accepted as a default for an enum whose values
    # are ints -- mirrors the same bool-vs-int care required everywhere else.
    with pytest.raises(ValidationError):
        _param(type="enum", default=True, enum_values=(0, 1))


def test_strategy_parameter_rejects_bool_default_for_integer_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="integer", default=True)


def test_strategy_parameter_rejects_bool_default_for_number_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="number", default=True)


def test_strategy_parameter_rejects_int_default_for_string_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="string", default=1)


def test_strategy_parameter_rejects_string_default_for_boolean_type() -> None:
    with pytest.raises(ValidationError):
        _param(type="boolean", default="true")


def test_strategy_parameter_number_type_accepts_int_default() -> None:
    parameter = _param(type="number", default=5)
    assert parameter.default == 5


def test_strategy_parameter_rejects_non_finite_default() -> None:
    with pytest.raises(ValidationError):
        _param(type="number", default=math.inf)


def test_strategy_parameter_allows_out_of_range_default() -> None:
    # Deliberately NOT a construction-time error -- whether a default falls
    # within minimum/maximum is validate_strategy_parameters's job (so Skill
    # discovery can isolate it as `invalid_defaults` instead), not this
    # model's.
    parameter = _param(default=999, minimum=1, maximum=10)
    assert parameter.default == 999


# ---------------------------------------------------------------------------
# validate_strategy_parameters -- the shared authority (Story 2.2)
# ---------------------------------------------------------------------------


def test_validate_strategy_parameters_valid_submission_returns_mapping() -> None:
    schema = [_param(name="n", type="integer", default=1, required=True)]
    result = validate_strategy_parameters(schema, {"n": 5}, apply_defaults=False)
    assert result == {"n": 5}


def test_validate_strategy_parameters_missing_required_apply_defaults_false() -> None:
    schema = [_param(name="n", type="integer", default=1, required=True)]
    result = validate_strategy_parameters(schema, {}, apply_defaults=False)
    assert result == (
        ParameterFieldErrorV1(
            parameter_name="n",
            code=ParameterValidationErrorCode.MISSING_REQUIRED,
            message="'n' is required",
        ),
    )


def test_validate_strategy_parameters_missing_optional_apply_defaults_false() -> None:
    schema = [_param(name="n", type="integer", default=1, required=False)]
    result = validate_strategy_parameters(schema, {}, apply_defaults=False)
    assert result == {}


def test_validate_strategy_parameters_apply_defaults_true_fills_every_field() -> None:
    schema = [
        _param(name="required_field", type="integer", default=7, required=True),
        _param(name="optional_field", type="string", default="x", required=False),
    ]
    result = validate_strategy_parameters(schema, {}, apply_defaults=True)
    assert result == {"required_field": 7, "optional_field": "x"}


def test_validate_strategy_parameters_apply_defaults_true_rejects_out_of_range() -> (
    None
):
    schema = [_param(name="n", type="integer", default=999, minimum=1, maximum=10)]
    result = validate_strategy_parameters(schema, {}, apply_defaults=True)
    assert result == (
        ParameterFieldErrorV1(
            parameter_name="n",
            code=ParameterValidationErrorCode.ABOVE_MAXIMUM,
            message="'n' must be <= 10",
        ),
    )


def test_validate_strategy_parameters_rejects_bool_for_integer_field() -> None:
    schema = [_param(name="n", type="integer", default=1, required=False)]
    result = validate_strategy_parameters(schema, {"n": True}, apply_defaults=False)
    assert result == (
        ParameterFieldErrorV1(
            parameter_name="n",
            code=ParameterValidationErrorCode.INVALID_TYPE,
            message="'n' must be a plain int",
        ),
    )


def test_validate_strategy_parameters_rejects_bool_for_number_field() -> None:
    schema = [_param(name="n", type="number", default=1.0, required=False)]
    result = validate_strategy_parameters(schema, {"n": False}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.INVALID_TYPE


def test_validate_strategy_parameters_rejects_unknown_field() -> None:
    schema = [_param(name="n", type="integer", default=1, required=False)]
    result = validate_strategy_parameters(schema, {"unknown": 1}, apply_defaults=False)
    assert result == (
        ParameterFieldErrorV1(
            parameter_name="unknown",
            code=ParameterValidationErrorCode.UNKNOWN_FIELD,
            message="unknown parameter 'unknown'",
        ),
    )


def test_validate_strategy_parameters_below_minimum() -> None:
    schema = [_param(name="n", type="integer", default=5, minimum=10)]
    result = validate_strategy_parameters(schema, {"n": 1}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.BELOW_MINIMUM


def test_validate_strategy_parameters_above_maximum() -> None:
    schema = [_param(name="n", type="integer", default=5, maximum=10)]
    result = validate_strategy_parameters(schema, {"n": 11}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.ABOVE_MAXIMUM


def test_validate_strategy_parameters_within_bounds_is_accepted() -> None:
    schema = [_param(name="n", type="integer", default=5, minimum=1, maximum=10)]
    result = validate_strategy_parameters(schema, {"n": 10}, apply_defaults=False)
    assert result == {"n": 10}


def test_validate_strategy_parameters_enum_exact_type_match_true_rejects_one() -> None:
    schema = [
        _param(name="n", type="enum", default=0, enum_values=(0, 1), required=False)
    ]
    result = validate_strategy_parameters(schema, {"n": True}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.NOT_IN_ENUM


def test_validate_strategy_parameters_enum_accepts_valid_member() -> None:
    schema = [
        _param(
            name="n",
            type="enum",
            default="a",
            enum_values=("a", "b"),
            required=False,
        )
    ]
    result = validate_strategy_parameters(schema, {"n": "b"}, apply_defaults=False)
    assert result == {"n": "b"}


def test_validate_strategy_parameters_rejects_nan() -> None:
    schema = [_param(name="n", type="number", default=1.0, required=False)]
    result = validate_strategy_parameters(schema, {"n": math.nan}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.NON_FINITE_VALUE


def test_validate_strategy_parameters_rejects_infinity() -> None:
    schema = [_param(name="n", type="number", default=1.0, required=False)]
    result = validate_strategy_parameters(schema, {"n": math.inf}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.NON_FINITE_VALUE


def test_validate_strategy_parameters_string_type_rejects_non_string() -> None:
    schema = [_param(name="n", type="string", default="x", required=False)]
    result = validate_strategy_parameters(schema, {"n": 1}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.INVALID_TYPE


def test_validate_strategy_parameters_boolean_type_rejects_non_bool() -> None:
    schema = [_param(name="n", type="boolean", default=True, required=False)]
    result = validate_strategy_parameters(schema, {"n": 1}, apply_defaults=False)
    assert isinstance(result, tuple)
    assert result[0].code == ParameterValidationErrorCode.INVALID_TYPE


def test_validate_strategy_parameters_error_order_is_schema_then_lexical_unknown() -> (
    None
):
    schema = [
        _param(name="second", type="integer", default=1, required=True),
        _param(name="first", type="integer", default=1, required=True),
    ]
    result = validate_strategy_parameters(
        schema, {"zeta": 1, "alpha": 1}, apply_defaults=False
    )
    assert isinstance(result, tuple)
    parameter_names = [error.parameter_name for error in result]
    # Schema declaration order first ("second" before "first" -- the order
    # given, not alphabetical), then unknown submitted fields lexically.
    assert parameter_names == ["second", "first", "alpha", "zeta"]


def test_validate_strategy_parameters_is_deterministic_across_calls() -> None:
    schema = [
        _param(name="b", type="integer", default=1, required=True),
        _param(name="a", type="integer", default=1, required=True),
    ]
    first = validate_strategy_parameters(schema, {"x": 1}, apply_defaults=False)
    second = validate_strategy_parameters(schema, {"x": 1}, apply_defaults=False)
    assert first == second


def test_validate_strategy_parameters_rejects_duplicate_schema_declarations() -> None:
    schema = [
        _param(name="dup", type="integer", default=1, required=True),
        _param(name="dup", type="integer", default=2, required=False),
    ]
    with pytest.raises(StrategyProtocolError) as excinfo:
        validate_strategy_parameters(schema, {}, apply_defaults=False)
    assert (
        excinfo.value.code == StrategyProtocolErrorCode.DUPLICATE_PARAMETER_DECLARATION
    )


def test_validate_strategy_parameters_pure_no_mutation_of_inputs() -> None:
    schema = [_param(name="n", type="integer", default=1, required=False)]
    submitted = {"n": 5}
    validate_strategy_parameters(schema, submitted, apply_defaults=False)
    assert submitted == {"n": 5}


def test_validate_strategy_parameters_empty_schema_empty_submission() -> None:
    result = validate_strategy_parameters([], {}, apply_defaults=True)
    assert result == {}
