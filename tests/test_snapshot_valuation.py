"""Tests for the shared GBP snapshot valuation rule (#466).

Covers every row of the issue's I/O matrix that concerns *valuing* a
snapshot: mixed currencies, the analysis-artifact envelope, no prices,
partial prices, a missing FX rate, and a cash-only portfolio.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.core.money import Money
from app.schemas.trade import Position
from app.services.gbp_valuation_service import (
    GbpValuationProjection,
    GbpValuationService,
)
from app.services.snapshot_valuation import (
    amount_in_gbp,
    valid_rate_or_none,
    value_positions_gbp,
)


class _StubGbpValuation:
    """Values non-GBP ``Money`` from a fixed ``{currency: rate}`` table.

    Rates quote units of the currency per 1 GBP, matching
    ``GbpValuationService``. A currency absent from the table is
    ``valuation_unavailable`` -- the real service's honest no-quote state.
    """

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self._rates = rates or {}

    def value_in_gbp(self, money: Money) -> GbpValuationProjection:
        rate = self._rates.get(money.currency)
        if rate is None:
            return GbpValuationProjection(
                money=money, status="valuation_unavailable", reason="fetch_failed"
            )
        return GbpValuationProjection(
            money=money,
            status="valued",
            gbp_amount=(money.amount / Decimal(str(rate))),
        )


def _valuation(rates: dict[str, float] | None = None) -> GbpValuationService:
    return cast(GbpValuationService, _StubGbpValuation(rates))


def _position(
    ticker: str,
    *,
    cost: float,
    value: float | None,
    currency: str = "GBP",
) -> Position:
    return Position(
        ticker=ticker,
        shares=10.0,
        avg_cost=cost / 10.0,
        total_cost=cost,
        current_value=value,
        price_currency=currency,
    )


def _agent(tmp_path: Path) -> TraderAgent:
    agent = TraderAgent(name="TraderAgent")
    agent.db_path = tmp_path / "trades.db"
    agent._init_db()
    agent._gbp_valuation = _valuation()  # type: ignore[assignment]
    return agent


# --- amount_in_gbp ---------------------------------------------------------


def test_amount_in_gbp_converts_usd_pence_and_passes_gbp_through() -> None:
    service = _valuation()
    assert amount_in_gbp(200.0, "USD", 2.0, service) == 100.0
    assert amount_in_gbp(250.0, "GBp", 2.0, service) == 2.5
    assert amount_in_gbp(100.0, "GBP", 2.0, service) == 100.0


def test_amount_in_gbp_uses_valuation_service_for_hkd() -> None:
    service = _valuation({"HKD": 10.0})
    assert amount_in_gbp(500.0, "HKD", 2.0, service) == 50.0
    assert amount_in_gbp(500.0, "HKD", 2.0, _valuation()) is None


@pytest.mark.parametrize("rate", [None, 0.0, -1.0, float("nan"), float("inf"), "x"])
def test_valid_rate_or_none_rejects_unusable_rates(rate: Any) -> None:
    assert valid_rate_or_none(rate) is None


# --- value_positions_gbp ---------------------------------------------------


def test_mixed_currency_positions_sum_in_gbp() -> None:
    positions = [
        _position("AAPL", cost=100.0, value=200.0, currency="USD"),
        _position("VOD.L", cost=50.0, value=80.0, currency="GBP"),
    ]
    result = value_positions_gbp(positions, 2.0, _valuation())
    assert result.status == "valued"
    assert result.market_value_gbp == pytest.approx(180.0)
    assert result.cost_gbp == pytest.approx(100.0)
    assert (result.valued_positions, result.unvalued_positions) == (2, 0)


def test_no_prices_available_is_unavailable_never_zero() -> None:
    positions = [
        _position("AAPL", cost=100.0, value=None, currency="USD"),
        _position("VOD.L", cost=50.0, value=None),
    ]
    result = value_positions_gbp(positions, 2.0, _valuation())
    assert result.status == "unavailable"
    assert result.market_value_gbp is None
    assert (result.valued_positions, result.unvalued_positions) == (0, 2)


def test_partial_prices_report_incomplete_and_no_partial_total() -> None:
    positions = [
        _position("AAPL", cost=100.0, value=200.0, currency="USD"),
        _position("VOD.L", cost=50.0, value=80.0),
        _position("BP.L", cost=25.0, value=None),
    ]
    result = value_positions_gbp(positions, 2.0, _valuation())
    assert result.status == "incomplete"
    assert result.market_value_gbp is None
    assert (result.valued_positions, result.unvalued_positions) == (2, 1)


def test_missing_fx_leaves_usd_holding_unvalued() -> None:
    positions = [
        _position("AAPL", cost=100.0, value=200.0, currency="USD"),
        _position("VOD.L", cost=50.0, value=80.0),
    ]
    result = value_positions_gbp(positions, None, _valuation())
    assert result.status == "incomplete"
    assert result.market_value_gbp is None
    # The unconvertible cost makes the aggregate cost unavailable too --
    # never a partial figure presented as a total.
    assert result.cost_gbp is None


def test_cash_only_portfolio_is_a_genuine_zero() -> None:
    result = value_positions_gbp([], 2.0, _valuation())
    assert result.status == "empty"
    assert result.market_value_gbp == 0.0
    assert result.cost_gbp == 0.0


# --- TraderAgent.update_portfolio_snapshot ---------------------------------


def test_snapshot_reads_prices_from_the_analysis_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``{"meta": ..., "records": [...]}`` envelope must resolve prices.

    Before #466 this parsed the envelope as a bare list, resolved nothing,
    and persisted a bogus ``0.00`` (regression test for the reported bug).
    """
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)
    artifact = tmp_path / "analysis_results.json"
    artifact.write_text(
        json.dumps(
            {
                "meta": {"run_id": "r1", "generated_at": "2024-01-02T00:00:00+00:00"},
                "records": [{"ticker": "AAPL", "price": 20.0}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.ANALYSIS_JSON", artifact, raising=False
    )

    result = agent.update_portfolio_snapshot(1000.0, pf.id)
    assert result.status == "valued"
    assert result.market_value_gbp == pytest.approx(200.0)
    rows = agent.snapshot_history(pf.id)
    assert rows[-1][1] == pytest.approx(200.0)


def test_snapshot_with_no_resolvable_prices_persists_null(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01", portfolio_id=pf.id)

    result = agent.update_portfolio_snapshot(1000.0, pf.id, positions=None, gbpusd=None)
    assert result.status == "unavailable"
    rows = agent.snapshot_history(pf.id)
    assert rows[-1][1] is None


def test_snapshot_of_cash_only_portfolio_stores_zero(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")

    result = agent.update_portfolio_snapshot(1000.0, pf.id)
    assert result.status == "empty"
    rows = agent.snapshot_history(pf.id)
    assert rows[-1][1] == 0.0
    assert rows[-1][2] == 0.0


def test_supplied_positions_and_rate_are_used_verbatim(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    pf = agent.create_portfolio("SIPP")
    positions = [
        _position("AAPL", cost=100.0, value=200.0, currency="USD"),
        _position("VOD.L", cost=50.0, value=80.0),
    ]

    result = agent.update_portfolio_snapshot(
        500.0, pf.id, positions=positions, gbpusd=2.0
    )
    assert result.market_value_gbp == pytest.approx(180.0)
    rows = agent.snapshot_history(pf.id)
    assert rows[-1][1] == pytest.approx(180.0)
    assert rows[-1][3] == pytest.approx(500.0)


def test_legacy_csv_path_writes_blank_cell_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-``portfolio_id`` CSV branch keeps working and stays honest."""
    agent = _agent(tmp_path)
    agent.record_buy("AAPL", 10, 5.0, "2024-01-01")
    csv_path = tmp_path / "portfolio_value.csv"
    monkeypatch.setattr(
        "app.agents.trader.trader_agent.PORTFOLIO_VALUE_CSV", csv_path, raising=False
    )

    result = agent.update_portfolio_snapshot(250.0)
    assert result.status == "unavailable"
    body = csv_path.read_text(encoding="utf-8")
    assert "cash_balance" in body
    last = body.strip().splitlines()[-1].split(",")
    assert last[1] == ""  # total_value column left blank, never "0.00"
    assert last[3] == "250.00"
