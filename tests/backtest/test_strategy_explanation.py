"""Validation and formatting tests for Strategy signal explanations (#472).

Covers the spec's I/O and edge-case matrix rows: malformed codes,
non-finite numbers, presentation markup, nondeterministic collections,
duplicate reasons, canonical ordering, unsupported contract versions,
future-dated evidence, and the shared host formatter's output.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.backtest.strategy_explanation import (
    EXPLANATION_CONTRACT_VERSION,
    ComparisonOperator,
    EvidenceUnit,
    ExplanationFactV1,
    SignalExplanationV1,
    SignalReasonV1,
    format_decimal,
    format_fact,
    format_reason,
    format_value,
)
from app.services.backtest.strategy_protocol import (
    Signal,
    SignalSide,
    StrategyProtocolError,
    StrategyProtocolErrorCode,
    validate_signal_explanations,
)

SESSION = date(2026, 8, 20)


def _reason(code: str = "close_below_sma150") -> SignalReasonV1:
    return SignalReasonV1(
        code=code,
        summary="Close fell below the 150-session moving average.",
        facts=(
            ExplanationFactV1(
                label="Close",
                observed=Decimal("92.10"),
                operator=ComparisonOperator.LT,
                threshold=Decimal("101.44"),
                unit=EvidenceUnit.PRICE,
            ),
        ),
    )


def _signal(explanation: SignalExplanationV1 | None) -> Signal:
    return Signal(
        security_id="sec-aapl",
        side=SignalSide.SELL,
        session=SESSION,
        rule_id="weinstein_stage_exit_v1",
        explanation=explanation,
    )


@pytest.mark.parametrize("code", ["Stage 2!", "", "ab", "Close", "close-below"])
def test_malformed_reason_code_is_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        SignalReasonV1(code=code, summary="Something happened.")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimal_evidence_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        ExplanationFactV1(label="Close", observed=Decimal(value))
    with pytest.raises(ValidationError):
        ExplanationFactV1(label="Close", threshold=Decimal(value))


@pytest.mark.parametrize("markup", ["<b>Close</b>", "Close &amp; volume", "&#9888; hi"])
def test_presentation_markup_is_rejected_in_label_and_summary(markup: str) -> None:
    with pytest.raises(ValidationError):
        ExplanationFactV1(label=markup)
    with pytest.raises(ValidationError):
        SignalReasonV1(code="stage_exit", summary=markup)
    with pytest.raises(ValidationError):
        ExplanationFactV1(label="Stage", observed=markup)


def test_control_characters_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalReasonV1(code="stage_exit", summary="line\nbreak")


@pytest.mark.parametrize(
    "collection",
    [
        {"close_below_sma150"},
        frozenset({"close_below_sma150"}),
        {"close_below_sma150": 1},
        iter(()),
    ],
)
def test_nondeterministic_collections_are_rejected(collection: object) -> None:
    with pytest.raises((ValidationError, TypeError)):
        SignalExplanationV1(reasons=collection)
    with pytest.raises((ValidationError, TypeError)):
        SignalReasonV1(code="stage_exit", summary="Stage failed.", facts=collection)


def test_duplicate_reason_codes_and_fact_labels_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalExplanationV1(reasons=(_reason(), _reason()))
    with pytest.raises(ValidationError):
        SignalReasonV1(
            code="stage_exit",
            summary="Stage failed.",
            facts=(
                ExplanationFactV1(label="Close", observed=Decimal(1)),
                ExplanationFactV1(label="Close", observed=Decimal(2)),
            ),
        )


def test_empty_reasons_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalExplanationV1(reasons=())


def test_reasons_are_canonically_sorted_by_code() -> None:
    explanation = SignalExplanationV1(
        reasons=[
            _reason("stage_exit"),
            _reason("maximum_loss_stop"),
            _reason("close_below_sma150"),
        ]
    )

    assert explanation.codes == (
        "close_below_sma150",
        "maximum_loss_stop",
        "stage_exit",
    )
    assert explanation.contract_version == EXPLANATION_CONTRACT_VERSION


def test_unsupported_contract_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalExplanationV1(contract_version=2, reasons=(_reason(),))


def test_future_dated_evidence_is_rejected_at_signal_construction() -> None:
    explanation = SignalExplanationV1(
        reasons=(
            SignalReasonV1(
                code="stage_exit",
                summary="Stage failed.",
                facts=(
                    ExplanationFactV1(
                        label="Weinstein stage",
                        observed="Stage 3",
                        as_of=date(2026, 8, 21),
                    ),
                ),
            ),
        )
    )

    with pytest.raises(StrategyProtocolError) as excinfo:
        _signal(explanation)

    assert excinfo.value.code is StrategyProtocolErrorCode.FUTURE_DATED_OBSERVATION


def test_same_session_evidence_is_accepted() -> None:
    explanation = SignalExplanationV1(
        reasons=(
            SignalReasonV1(
                code="stage_exit",
                summary="Stage failed.",
                facts=(
                    ExplanationFactV1(
                        label="Weinstein stage", observed="Stage 3", as_of=SESSION
                    ),
                ),
            ),
        )
    )

    assert _signal(explanation).explanation is explanation


def test_validate_signal_explanations_requires_every_signal_to_explain_itself() -> None:
    explained = _signal(SignalExplanationV1(reasons=(_reason(),)))

    assert validate_signal_explanations([explained], method_name="exit_signals") == (
        explained,
    )

    with pytest.raises(StrategyProtocolError) as excinfo:
        validate_signal_explanations([explained, _signal(None)], method_name="exits")

    assert excinfo.value.code is StrategyProtocolErrorCode.MISSING_SIGNAL_EXPLANATION


def test_signal_explanation_never_changes_ordering() -> None:
    """``sort_key`` must ignore the explanation entirely (AC: ordering)."""
    plain = _signal(None)
    explained = _signal(SignalExplanationV1(reasons=(_reason(),)))

    assert plain.sort_key == explained.sort_key


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("92.10"), "92.1"),
        (Decimal("101.4400"), "101.44"),
        (Decimal("1E+2"), "100"),
        (Decimal("0.000"), "0"),
        (Decimal("-3.50"), "-3.5"),
    ],
)
def test_format_decimal_is_plain_and_trimmed(value: Decimal, expected: str) -> None:
    assert format_decimal(value) == expected


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (EvidenceUnit.PERCENT, "12.5%"),
        (EvidenceUnit.RATIO, "12.5x"),
        (EvidenceUnit.SESSIONS, "12.5 sessions"),
        (EvidenceUnit.PRICE, "12.5"),
        (EvidenceUnit.COUNT, "12.5"),
        (EvidenceUnit.SCORE, "12.5"),
        (EvidenceUnit.NONE, "12.5"),
    ],
)
def test_format_value_renders_each_unit(unit: EvidenceUnit, expected: str) -> None:
    assert format_value(Decimal("12.50"), unit) == expected
    assert format_value(None, unit) == ""
    assert format_value("Stage 2", unit) == "Stage 2"


def test_format_fact_and_reason_render_plain_text() -> None:
    compared = ExplanationFactV1(
        label="Close",
        observed=Decimal("92.10"),
        operator=ComparisonOperator.LT,
        threshold=Decimal("101.44"),
        unit=EvidenceUnit.PRICE,
    )
    bare = ExplanationFactV1(
        label="Close", observed=Decimal("92.10"), unit=EvidenceUnit.PRICE
    )
    dated = ExplanationFactV1(
        label="Weinstein stage",
        observed="Stage 3",
        operator=ComparisonOperator.IS_NOT,
        threshold="Stage 2",
        as_of=SESSION,
    )

    assert format_fact(compared) == "Close 92.1 < 101.44"
    assert format_fact(bare) == "Close: 92.1"
    assert format_fact(dated) == (
        "Weinstein stage Stage 3 is not Stage 2 (as of 2026-08-20)"
    )
    summary, facts = format_reason(_reason())
    assert summary == "Close fell below the 150-session moving average."
    assert facts == ("Close 92.1 < 101.44",)


# ---------------------------------------------------------------------------
# Review-pass hardening: render-time crashes and half-stated comparisons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1E+30"), "1" + "0" * 30),
        (
            Decimal("12345678901234567890123456789012"),
            "12345678901234567890123456789012",
        ),
        (Decimal("-1500.500"), "-1500.5"),
        (Decimal("0.000"), "0"),
    ],
)
def test_format_decimal_never_raises_beyond_the_default_context(
    value: Decimal, expected: str
) -> None:
    """A magnitude past the default precision must render, not explode.

    ``quantize`` would raise ``InvalidOperation`` here — deferring a
    failure from construction time to render time, inside an email.
    """
    assert format_decimal(value) == expected
    assert format_fact(ExplanationFactV1(label="Big", observed=value)) == (
        f"Big: {expected}"
    )


def test_an_operator_requires_both_sides_of_the_comparison() -> None:
    """A half-stated comparison would render as evidence never observed."""
    with pytest.raises(ValidationError):
        ExplanationFactV1(
            label="Close", observed=Decimal("1"), operator=ComparisonOperator.GT
        )
    with pytest.raises(ValidationError):
        ExplanationFactV1(
            label="Close", operator=ComparisonOperator.GT, threshold=Decimal("2")
        )
    with pytest.raises(ValidationError):
        ExplanationFactV1(label="Close", threshold=Decimal("2"))


@pytest.mark.parametrize("character", [" ", " ", "\x85", "\x7f"])
def test_plain_text_rejects_line_breaking_and_c1_characters(character: str) -> None:
    """These are not C0 controls but still break a plain-text contract."""
    with pytest.raises(ValidationError):
        ExplanationFactV1(label=f"Close{character}")
    with pytest.raises(ValidationError):
        SignalReasonV1(code="stage_exit", summary=f"Stage failed{character}")
