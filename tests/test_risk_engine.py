"""Tests for the deterministic Portfolio Risk Coach engine (GH-16)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from app.schemas.portfolio_risk import RiskFindingV1, RiskReportV1
from app.schemas.trade import Position
from app.services.gbp_valuation_service import GbpValuationService
from app.services.risk_engine import evaluate

TODAY = date(2026, 9, 28)
FRESH = "2026-09-27 18:00 UTC"


def _pos(
    ticker: str,
    shares: float,
    price: float | None,
    *,
    stop: float | None = None,
    price_currency: str = "GBP",
    cost_currency: str = "GBP",
) -> Position:
    return Position(
        ticker=ticker,
        shares=shares,
        avg_cost=1.0,
        total_cost=shares,
        current_price=price,
        stop_loss=stop,
        price_currency=price_currency,
        cost_currency=cost_currency,
    )


def _run(
    positions: list[Position],
    *,
    sectors: dict[str, str | None] | None = None,
    scan_stops: dict[str, tuple[float, str]] | None = None,
    cash: float | None = 0.0,
    gbpusd: float | None = None,
    prices_as_of: str | None = FRESH,
) -> RiskReportV1:
    return evaluate(
        positions,
        sectors=sectors
        if sectors is not None
        else {p.ticker: "Tech" for p in positions},
        scan_stops=scan_stops or {},
        cash_gbp=cash,
        gbpusd=gbpusd,
        prices_as_of=prices_as_of,
        today=TODAY,
        # Only GBP/GBp/USD are exercised, which never touch this service.
        gbp_valuation=GbpValuationService(MagicMock(), ticker_factory=MagicMock()),
    )


def _kind(report: RiskReportV1, kind: str) -> list[RiskFindingV1]:
    return [f for f in report.findings if f.kind == kind]


def test_concentrated_position_names_it_with_inputs() -> None:
    report = _run([_pos("AAA", 3, 100.0, stop=99.0)], cash=700.0)
    [finding] = _kind(report, "position_concentration")
    assert finding.severity == "high"
    assert finding.tickers == ("AAA",)
    assert finding.inputs == (
        ("Position value", "£300.00"),
        ("Portfolio value", "£1,000.00"),
        ("Weight", "30.0%"),
        ("Limit", "20%"),
    )
    assert report.total_value_gbp == Decimal("1000.0")


def test_sector_breach_is_high_and_names_sector() -> None:
    positions = [_pos("AAA", 2, 100.0, stop=99.0), _pos("BBB", 2, 100.0, stop=99.0)]
    report = _run(
        positions, sectors={"AAA": "Technology", "BBB": "Technology"}, cash=600.0
    )
    [finding] = _kind(report, "sector_concentration")
    assert finding.severity == "high"
    assert finding.title == "Technology is 40.0% of the portfolio"
    assert finding.tickers == ("AAA", "BBB")
    assert not _kind(report, "position_concentration")  # 20% is at the limit


def test_unknown_sector_reported_once_and_never_ranked() -> None:
    positions = [_pos("AAA", 3, 100.0, stop=99.0), _pos("BBB", 3, 100.0, stop=99.0)]
    report = _run(positions, sectors={"AAA": None}, cash=400.0)
    [finding] = _kind(report, "unknown_sector")
    assert finding.severity == "info"
    assert finding.tickers == ("AAA", "BBB")
    assert not _kind(report, "sector_concentration")


def test_stop_headroom_aggregates_capital_at_risk() -> None:
    report = _run([_pos("AAA", 10, 100.0, stop=90.0)])
    [finding] = _kind(report, "capital_at_risk")
    assert finding.severity == "high"
    assert finding.inputs[0] == ("AAA (BUY trade stop 90.00 GBP)", "£100.00 (10.0%)")
    assert ("Total at risk", "£100.00") in finding.inputs
    assert ("Limit", "6%") in finding.inputs
    assert report.confidence == "complete"


def test_capital_at_risk_within_limit_is_info() -> None:
    report = _run([_pos("AAA", 10, 100.0, stop=99.0)])
    [finding] = _kind(report, "capital_at_risk")
    assert finding.severity == "info"
    assert finding.action == ""


def test_at_or_below_stop_is_high_and_contributes_zero() -> None:
    report = _run([_pos("AAA", 10, 90.0, stop=90.0)])
    [below] = _kind(report, "below_stop")
    assert below.severity == "high"
    [aggregate] = _kind(report, "capital_at_risk")
    assert ("Total at risk", "£0.00") in aggregate.inputs


def test_no_stop_is_medium_and_limits_confidence() -> None:
    report = _run([_pos("AAA", 10, 100.0)])
    [finding] = _kind(report, "no_stop")
    assert finding.severity == "medium"
    assert not _kind(report, "capital_at_risk")
    assert report.confidence == "limited"


def test_scan_stop_used_when_no_trade_stop_and_source_recorded() -> None:
    report = _run(
        [_pos("AAA", 10, 100.0), _pos("BBB", 10, 100.0, stop=95.0)],
        scan_stops={"AAA": (80.0, "GBP"), "BBB": (50.0, "GBP")},
    )
    [aggregate] = _kind(report, "capital_at_risk")
    labels = dict(aggregate.inputs)
    assert labels["AAA (scan analysis stop 80.00 GBP)"] == "£200.00 (10.0%)"
    # The BUY-trade stop wins over the scan stop.
    assert labels["BBB (BUY trade stop 95.00 GBP)"] == "£50.00 (2.5%)"


def test_unpriced_positions_are_excluded_and_limit_confidence() -> None:
    report = _run(
        [
            _pos("NOPRICE", 10, None, stop=5.0),
            _pos("USDNORATE", 10, 100.0, stop=90.0, price_currency="USD"),
            _pos("AAA", 1, 100.0, stop=99.0),
        ],
        cash=900.0,
    )
    unpriced = _kind(report, "unpriced")
    assert [f.tickers for f in unpriced] == [("NOPRICE",), ("USDNORATE",)]
    assert all(f.severity == "medium" for f in unpriced)
    assert report.total_value_gbp == Decimal("1000.0")
    assert report.confidence == "limited"


def test_cross_currency_stop_converted_before_comparing() -> None:
    # 1000 shares at 100p (= £1,000) with a £0.95 trade stop (= £950).
    report = _run([_pos("VOD", 1000, 100.0, stop=0.95, price_currency="GBp")])
    assert not _kind(report, "below_stop")
    [aggregate] = _kind(report, "capital_at_risk")
    assert ("Total at risk", "£50.00") in aggregate.inputs


def test_unconvertible_stop_keeps_value_but_counts_as_no_stop() -> None:
    report = _run([_pos("AZN", 10, 100.0, stop=90.0, cost_currency="USD")], gbpusd=None)
    assert not _kind(report, "unpriced")
    [finding] = _kind(report, "no_stop")
    assert "could not be converted to GBP" in finding.detail
    assert report.total_value_gbp == Decimal("1000.0")
    assert _kind(report, "position_concentration")  # still in concentration
    assert not _kind(report, "capital_at_risk")
    assert report.confidence == "limited"


def test_usd_position_valued_through_supplied_rate() -> None:
    report = _run(
        [
            _pos(
                "MSFT", 10, 125.0, stop=100.0, price_currency="USD", cost_currency="USD"
            )
        ],
        gbpusd=1.25,
    )
    assert report.total_value_gbp == Decimal("1000.0")
    [aggregate] = _kind(report, "capital_at_risk")
    assert ("Total at risk", "£200.00") in aggregate.inputs


def test_empty_portfolio_only_reports_cash() -> None:
    report = _run([], cash=500.0, prices_as_of=None)
    assert [f.kind for f in report.findings] == ["cash"]
    assert report.confidence == "complete"
    unknown_cash = _run([], cash=None)
    assert [f.kind for f in unknown_cash.findings] == ["cash"]
    assert unknown_cash.confidence == "limited"
    assert unknown_cash.findings[0].title == "Cash: unknown"


def test_stale_or_undated_prices_limit_confidence() -> None:
    positions = [_pos("AAA", 1, 100.0, stop=99.0)]
    stale = _run(positions, prices_as_of="2026-09-24 18:00 UTC")
    assert stale.confidence == "limited"
    assert any("older than 3 days" in text for text in stale.limitations)
    assert _run(positions, prices_as_of=None).confidence == "limited"
    assert _run(positions, prices_as_of="2026-09-25 09:00 UTC").confidence == (
        "complete"
    )


def test_findings_order_risk_first_then_magnitude_then_ticker() -> None:
    positions = [
        _pos("BIG", 5, 100.0),  # 50%, no stop
        _pos("MID", 3, 100.0),  # 30%, no stop
        _pos("LOW", 1, 100.0),  # 10%, no stop
        _pos("GONE", 1, None),
    ]
    report = _run(positions, cash=100.0)
    assert [(f.kind, f.tickers[:1]) for f in report.findings] == [
        ("sector_concentration", ("BIG",)),
        ("position_concentration", ("BIG",)),
        ("position_concentration", ("MID",)),
        ("no_stop", ("BIG",)),
        ("no_stop", ("MID",)),
        ("no_stop", ("LOW",)),
        ("unpriced", ("GONE",)),
        ("cash", ()),
    ]


def test_same_inputs_give_equal_reports() -> None:
    positions = [_pos("AAA", 10, 100.0, stop=90.0), _pos("BBB", 1, None)]
    assert _run(positions, cash=None) == _run(positions, cash=None)


def test_findings_never_carry_sizing_instructions() -> None:
    report = _run([_pos("AAA", 10, 100.0, stop=90.0)])
    for finding in report.findings:
        assert "shares" not in finding.action.lower()
        assert not finding.action or finding.action.startswith("Review")


def test_non_positive_shares_are_excluded_and_never_below_stop() -> None:
    report = _run(
        [
            _pos("OVER", -5, 100.0, stop=200.0),
            _pos("ZERO", 0, 100.0, stop=200.0),
            _pos("AAA", 1, 100.0, stop=99.0),
        ],
        cash=900.0,
    )
    unpriced = _kind(report, "unpriced")
    assert [f.tickers for f in unpriced] == [("OVER",), ("ZERO",)]
    assert all("non-positive share count" in f.detail for f in unpriced)
    assert not _kind(report, "below_stop")
    assert report.total_value_gbp == Decimal("1000.0")


def test_non_positive_total_is_a_limitation() -> None:
    report = _run([_pos("AAA", 1, 100.0, stop=90.0)], cash=-500.0)
    assert report.confidence == "limited"
    assert (
        "Portfolio value is not positive; concentration and capital-at-risk "
        "checks skipped."
    ) in report.limitations
    assert not _kind(report, "position_concentration")
    assert not _kind(report, "capital_at_risk")


def test_only_finite_positive_stops_count() -> None:
    report = _run(
        [
            _pos("ZERO", 1, 100.0, stop=0.0),
            _pos("NEG", 1, 100.0, stop=-5.0),
            _pos("NAN", 1, 100.0, stop=float("nan")),
            _pos("SCAN", 1, 100.0, stop=-1.0),
        ],
        scan_stops={"NAN": (float("nan"), "GBP"), "SCAN": (80.0, "GBP")},
        cash=9600.0,
    )
    assert sorted(f.tickers[0] for f in _kind(report, "no_stop")) == [
        "NAN",
        "NEG",
        "ZERO",
    ]
    assert not _kind(report, "below_stop")
    [aggregate] = _kind(report, "capital_at_risk")
    assert aggregate.tickers == ("SCAN",)  # invalid trade stop falls to scan


def test_limit_comparison_matches_displayed_percentage() -> None:
    # 20.04% displays as 20.0%, so it must not breach a 20% limit.
    at_limit = _run([_pos("AAA", 1, 2004.0, stop=2003.0)], cash=7996.0)
    assert not _kind(at_limit, "position_concentration")
    over = _run([_pos("AAA", 1, 2006.0, stop=2005.0)], cash=7994.0)
    [finding] = _kind(over, "position_concentration")
    assert ("Weight", "20.1%") in finding.inputs
    # 6.04% at risk displays as 6.0%: within the 6% limit.
    risk = _run([_pos("AAA", 1, 1000.0, stop=396.0)], cash=9000.0)
    [aggregate] = _kind(risk, "capital_at_risk")
    assert aggregate.title == "Capital at risk to stops is 6.0%"
    assert aggregate.severity == "info"


def test_capital_at_risk_detail_names_holdings_counted_as_zero() -> None:
    report = _run([_pos("AAA", 1, 90.0, stop=95.0), _pos("BBB", 10, 100.0, stop=90.0)])
    [aggregate] = _kind(report, "capital_at_risk")
    assert "1 holding(s) at or below their stop (AAA) count as £0.00" in (
        aggregate.detail
    )
    no_zero = _run([_pos("BBB", 10, 100.0, stop=90.0)])
    assert "at or below" not in _kind(no_zero, "capital_at_risk")[0].detail
