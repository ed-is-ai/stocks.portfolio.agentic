"""Unit tests for Trade Log security-label resolution (gh-367).

Covers every I/O & Edge-Case Matrix row for
``resolve_security_label`` plus one ``trade_log_view`` row per event
kind, checking each row carries ``security_label`` while keeping
``security_id``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.backtest.backtest_engine import (
    DividendAppliedEventV1,
    EntryFillEventV1,
    ExitFillEventV1,
    OpenPositionMarkEventV1,
    SignalSide,
    SkipReasonCode,
    SkippedSignalEventV1,
    SplitAppliedEventV1,
)
from app.services.backtest.result_presenter import (
    UNRESOLVED_SECURITY_LABEL,
    resolve_security_label,
    trade_log_view,
)

_SID = "SEC1"


def _skipped(seq: int = 1) -> SkippedSignalEventV1:
    return SkippedSignalEventV1(
        security_id=_SID,
        side=SignalSide.BUY,
        signal_session=date(2024, 1, 2),
        rule_id="rule-1",
        reason=SkipReasonCode.INSUFFICIENT_CASH,
        detail="not enough cash",
        sequence=seq,
    )


def _entry(seq: int = 2) -> EntryFillEventV1:
    return EntryFillEventV1(
        security_id=_SID,
        signal_session=date(2024, 1, 2),
        fill_session=date(2024, 1, 3),
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("100.00"),
        fill_currency="USD",
        fill_quote_unit="1",
        cost_base=Decimal("1000.00"),
        sequence=seq,
    )


def _exit(seq: int = 3) -> ExitFillEventV1:
    return ExitFillEventV1(
        security_id=_SID,
        signal_session=date(2024, 1, 20),
        fill_session=date(2024, 1, 21),
        rule_id="rule-1",
        shares=10,
        fill_price_native=Decimal("105.00"),
        fill_currency="USD",
        fill_quote_unit="1",
        proceeds_base=Decimal("1050.00"),
        cost_basis_base=Decimal("1000.00"),
        realized_pnl_base=Decimal("50.00"),
        sequence=seq,
    )


def _split(seq: int = 4) -> SplitAppliedEventV1:
    return SplitAppliedEventV1(
        security_id=_SID,
        session=date(2024, 1, 10),
        ratio=Decimal("2"),
        shares_before=Decimal("10"),
        shares_after=Decimal("20"),
        evidence_revision="f" * 64,
        policy_version="v1",
        sequence=seq,
    )


def _dividend(seq: int = 5) -> DividendAppliedEventV1:
    return DividendAppliedEventV1(
        security_id=_SID,
        session=date(2024, 1, 15),
        per_share_amount=Decimal("0.50"),
        shares_carried=Decimal("10"),
        cash_credit_native=Decimal("5.00"),
        cash_credit_base=Decimal("5.00"),
        currency="USD",
        quote_unit="1",
        evidence_revision="g" * 64,
        policy_version="v1",
        sequence=seq,
    )


def _open_mark(seq: int = 6) -> OpenPositionMarkEventV1:
    return OpenPositionMarkEventV1(
        security_id=_SID,
        session=date(2024, 1, 31),
        shares=10,
        mark_price_native=Decimal("110.00"),
        market_value_base=Decimal("1100.00"),
        cost_basis_base=Decimal("1000.00"),
        unrealized_pnl_base=Decimal("100.00"),
        sequence=seq,
    )


def _result(events: tuple[object, ...]) -> object:
    return SimpleNamespace(events=events, base_currency="GBP")


def test_resolved_member_uses_symbol_and_mic() -> None:
    assert resolve_security_label(_SID, {_SID: ("AAPL", "XNAS")}) == "AAPL (XNAS)"


def test_symbol_without_mic_uses_bare_symbol() -> None:
    assert resolve_security_label(_SID, {_SID: ("AAPL", "")}) == "AAPL"


def test_unresolved_id_returns_fallback_constant() -> None:
    assert resolve_security_label(_SID, {"other": ("AAPL", "XNAS")}) == (
        UNRESOLVED_SECURITY_LABEL
    )
    assert UNRESOLVED_SECURITY_LABEL == "Unknown security"


def test_empty_map_returns_fallback_for_every_row() -> None:
    view = trade_log_view(_result((_entry(), _exit())), {})
    assert [row.security_label for row in view.rows] == [
        UNRESOLVED_SECURITY_LABEL,
        UNRESOLVED_SECURITY_LABEL,
    ]
    assert all(row.security_id == _SID for row in view.rows)


def test_blank_or_whitespace_symbol_falls_back() -> None:
    assert (
        resolve_security_label(_SID, {_SID: ("", "XNAS")}) == UNRESOLVED_SECURITY_LABEL
    )
    assert (
        resolve_security_label(_SID, {_SID: ("   ", "")}) == UNRESOLVED_SECURITY_LABEL
    )


def test_symbol_with_surrounding_whitespace_is_trimmed() -> None:
    assert resolve_security_label(_SID, {_SID: (" AAPL ", " XNAS ")}) == "AAPL (XNAS)"


def test_duplicate_symbols_render_same_label_distinct_ids() -> None:
    identities = {"a": ("AAPL", "XNAS"), "b": ("AAPL", "XNAS")}
    assert resolve_security_label("a", identities) == "AAPL (XNAS)"
    assert resolve_security_label("b", identities) == "AAPL (XNAS)"


def test_every_event_kind_carries_label_and_keeps_id() -> None:
    events = (_skipped(), _entry(), _exit(), _split(), _dividend(), _open_mark())
    view = trade_log_view(_result(events), {_SID: ("MSFT", "XNAS")})
    assert len(view.rows) == 6
    for row in view.rows:
        assert row.security_label == "MSFT (XNAS)"
        assert row.security_id == _SID
