from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from app.repositories.backtest_repo import BacktestResultV1
from app.services.backtest.benchmark_series import (
    BAD_EVIDENCE_NOTE,
    NO_COVERAGE_NOTE,
    NO_FX_NOTE,
    NO_PIN_NOTE,
    NOT_SPY_NOTE,
    STALE_FX_NOTE,
    BenchmarkSeries,
    BenchmarkUnavailable,
    spy_total_return_series,
)
from tests.backtest.benchmark_fakes import (
    SPY_ID,
    FakePrices,
    fx_evidence,
    spy_evidence,
)

D1, D2, D3 = (date(2025, 1, day) for day in (2, 3, 6))


def _result(base_currency: str = "USD", *, pinned: bool = True) -> BacktestResultV1:
    pin = SimpleNamespace(security_id=SPY_ID, price_revision="spy")
    return cast(
        BacktestResultV1,
        SimpleNamespace(
            regime_benchmark=pin if pinned else None, base_currency=base_currency
        ),
    )


def _values(outcome: object) -> tuple[Decimal, ...]:
    assert isinstance(outcome, BenchmarkSeries), outcome
    return outcome.values


def test_usd_price_series_needs_no_fx() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0), (D2, 110, 0)]))

    outcome = spy_total_return_series(_result(), (D1, D2), prices, "ignored")

    assert _values(outcome) == (Decimal(100), Decimal(110))
    assert prices.calls == ["spy"]


def test_dividend_reinvested_on_ex_date_beats_price_return() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0), (D2, 100, 2), (D3, 110, 0)]))

    values = _values(spy_total_return_series(_result(), (D1, D2, D3), prices, None))

    assert values[1] == Decimal("102")
    assert values[2] == Decimal("112.2")
    assert values[2] / values[0] > Decimal("1.1")


def test_carries_spy_forward_on_xlon_only_sessions() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0), (D3, 120, 0)]))

    values = _values(spy_total_return_series(_result(), (D1, D2, D3), prices, None))

    assert values == (Decimal(100), Decimal(100), Decimal(120))


def test_gbp_divides_by_latest_gbpusd_close() -> None:
    prices = FakePrices(
        spy=spy_evidence([(D1, 125, 0), (D2, 150, 0)]),
        fx=fx_evidence([(D1, 1.25)]),
    )

    values = _values(spy_total_return_series(_result("GBP"), (D1, D2), prices, "fx"))

    assert values == (Decimal(100), Decimal(120))


def test_second_call_is_served_from_cache() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0)]), fx=fx_evidence([(D1, 1)]))

    first = spy_total_return_series(_result("GBP"), (D1,), prices, "fx")
    second = spy_total_return_series(_result("GBP"), (D1,), prices, "fx")

    assert first == second
    assert prices.calls == ["spy", "fx"]


def test_read_errors_are_not_cached() -> None:
    prices = FakePrices()
    spy_total_return_series(_result(), (D1,), prices, None)
    prices.evidence["spy"] = spy_evidence([(D1, 100, 0)])

    outcome = spy_total_return_series(_result(), (D1,), prices, None)

    assert _values(outcome) == (Decimal(100),)


@pytest.mark.parametrize(
    ("base_currency", "pinned", "fx_revision", "note"),
    [
        ("USD", False, None, NO_PIN_NOTE),
        ("GBP", True, None, NO_FX_NOTE),
        ("EUR", True, None, "No SPY benchmark for base currency EUR."),
    ],
    ids=["unpinned", "gbp-without-fx", "unsupported-currency"],
)
def test_missing_inputs_are_unavailable(
    base_currency: str, pinned: bool, fx_revision: str | None, note: str
) -> None:
    result = _result(base_currency, pinned=pinned)

    outcome = spy_total_return_series(result, (D1,), FakePrices(), fx_revision)

    assert outcome == BenchmarkUnavailable(note)


def test_no_spy_row_before_first_session_is_never_backfilled() -> None:
    prices = FakePrices(spy=spy_evidence([(D2, 100, 0)]))

    outcome = spy_total_return_series(_result(), (D1, D2), prices, None)

    assert outcome == BenchmarkUnavailable(NO_COVERAGE_NOTE)


def test_run_beyond_evidence_end_is_unavailable() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0)], end="2025-01-03"))

    outcome = spy_total_return_series(_result(), (D1, D2), prices, None)

    assert outcome == BenchmarkUnavailable(NO_COVERAGE_NOTE)


def test_gbp_session_before_first_fx_close_is_unavailable() -> None:
    prices = FakePrices(spy=spy_evidence([(D1, 100, 0)]), fx=fx_evidence([(D2, 1)]))

    outcome = spy_total_return_series(_result("GBP"), (D1,), prices, "fx")

    assert outcome == BenchmarkUnavailable(NO_FX_NOTE)


def test_fx_rate_is_never_carried_past_its_evidence_end() -> None:
    prices = FakePrices(
        spy=spy_evidence([(D1, 100, 0), (D2, 100, 0)]),
        fx=fx_evidence([(D1, 1.25)], end="2025-01-03"),
    )

    outcome = spy_total_return_series(_result("GBP"), (D1, D2), prices, "fx")

    assert outcome == BenchmarkUnavailable(STALE_FX_NOTE)


def test_non_spy_pin_is_not_labelled_spy() -> None:
    prices = FakePrices(
        spy=spy_evidence([(D1, 100, 0)], requested_symbol="QQQ", observed_symbol="QQQ")
    )

    outcome = spy_total_return_series(_result(), (D1,), prices, None)

    assert outcome == BenchmarkUnavailable(NOT_SPY_NOTE)


@pytest.mark.parametrize(
    "make_prices",
    [
        lambda: FakePrices(),
        lambda: FakePrices(spy=spy_evidence([(D1, 100, 0)], security_id="other")),
        lambda: FakePrices(spy=spy_evidence([], rows=({"session": "bad"},))),
        lambda: FakePrices(spy=spy_evidence([(D1, 100, 1), (D1, 100, 1)])),
        lambda: FakePrices(spy=spy_evidence([(date(2024, 12, 31), 100, 0)])),
        lambda: FakePrices(spy=spy_evidence([], rows=({"session": 1},))),
    ],
    ids=[
        "unreadable",
        "security-mismatch",
        "malformed-row",
        "duplicate-session",
        "row-outside-interval",
        "missing-close",
    ],
)
def test_bad_evidence_is_unavailable(make_prices: Callable[[], FakePrices]) -> None:
    outcome = spy_total_return_series(_result(), (D1,), make_prices(), None)

    assert outcome == BenchmarkUnavailable(BAD_EVIDENCE_NOTE)
