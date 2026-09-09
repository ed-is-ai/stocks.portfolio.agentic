"""Direct unit tests for :class:`CashReconstruction` (#543).

The backfill tests exercise this through SQLite and a whole service; these
pin the arithmetic itself -- nearest-anchor selection, the roll-backward
branch, multi-currency isolation -- where a wrong answer is one assertion
away rather than buried in a written row.
"""

from decimal import Decimal

import pytest

from app.services.cash_reconstruction import CashReconstruction
from app.services.snapshot_repair import gbp_replay_rows


def _replay(*rows: tuple[str, str, float, float, str]) -> list[tuple[object, ...]]:
    """Build replay tuples ``(ticker, action, shares, price, date, None, None)``."""
    return [(*row, None, None) for row in rows]


def test_no_anchor_at_all_reconstructs_nothing() -> None:
    assert CashReconstruction([], [], []).balances_at("2024-01-01") is None


def test_a_sell_after_the_anchor_adds_its_proceeds() -> None:
    model = CashReconstruction(
        [("2024-01-01", "GBP", Decimal("1000"))],
        [],
        _replay(("AAPL", "SELL", 10.0, 5.0, "2024-01-02")),
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1050")}


def test_a_buy_after_the_anchor_subtracts_its_cost() -> None:
    model = CashReconstruction(
        [("2024-01-01", "GBP", Decimal("1000"))],
        [],
        _replay(("AAPL", "BUY", 10.0, 5.0, "2024-01-02")),
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("950")}


def test_the_anchor_day_itself_is_the_stated_balance() -> None:
    """The interval is half-open, so same-day trades are already stated."""
    model = CashReconstruction(
        [("2024-01-02", "GBP", Decimal("1000"))],
        [],
        _replay(("AAPL", "BUY", 10.0, 5.0, "2024-01-02")),
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1000")}


def test_days_before_the_only_anchor_roll_backward() -> None:
    """A buy on d3 means cash was *higher* before it, not unknown."""
    model = CashReconstruction(
        [("2024-01-05", "GBP", Decimal("1000"))],
        [],
        _replay(("AAPL", "BUY", 10.0, 5.0, "2024-01-03")),
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1050")}
    # Nothing moves between d3 and the anchor, so d4 is the anchor itself.
    assert model.balances_at("2024-01-04") == {"GBP": Decimal("1000")}


def test_the_nearest_anchor_wins_in_either_direction() -> None:
    model = CashReconstruction(
        [
            ("2024-01-01", "GBP", Decimal("1000")),
            ("2024-01-31", "GBP", Decimal("4000")),
        ],
        [("2024-01-30", "CONTRIBUTION", 500.0, "GBP")],
        [],
    )
    # d2 is nearest the January 1st anchor, which the contribution post-dates.
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1000")}
    # d30 is nearest the January 31st one, and unwinds the contribution off it.
    assert model.balances_at("2024-01-29") == {"GBP": Decimal("3500")}


def test_an_equidistant_day_rolls_forward_from_the_earlier_anchor() -> None:
    model = CashReconstruction(
        [
            ("2024-01-01", "GBP", Decimal("1000")),
            ("2024-01-03", "GBP", Decimal("9999")),
        ],
        [],
        _replay(("AAPL", "SELL", 10.0, 5.0, "2024-01-02")),
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1050")}


def test_flow_signs_follow_the_type_and_ambiguous_types_move_nothing() -> None:
    model = CashReconstruction(
        [("2024-01-01", "GBP", Decimal("1000"))],
        [
            ("2024-01-02", "DIVIDEND", 20.0, "GBP"),
            ("2024-01-03", "WITHDRAWAL", 50.0, "GBP"),
            ("2024-01-04", "OTHER", 999.0, "GBP"),
            ("2024-01-04", "TRANSFER", 999.0, "GBP"),
            ("2024-01-04", "OPENING", 999.0, "GBP"),
        ],
        [],
    )
    assert model.balances_at("2024-01-02") == {"GBP": Decimal("1020")}
    assert model.balances_at("2024-01-03") == {"GBP": Decimal("970")}
    assert model.balances_at("2024-01-04") == {"GBP": Decimal("970")}


def test_each_currency_uses_its_own_anchors_and_flows() -> None:
    model = CashReconstruction(
        [
            ("2024-01-01", "GBP", Decimal("1000")),
            ("2024-01-01", "USD", Decimal("400")),
        ],
        [("2024-01-02", "DIVIDEND", 25.0, "USD")],
        _replay(("AAPL", "SELL", 10.0, 5.0, "2024-01-02")),
    )
    # Trades book to GBP only; the USD dividend touches USD only.
    assert model.balances_at("2024-01-02") == {
        "GBP": Decimal("1050"),
        "USD": Decimal("425"),
    }


def test_a_currency_reconstructing_to_zero_is_omitted() -> None:
    """A recent foreign anchor must not assert a phantom historical balance.

    It would also blank the whole day's cash, because the caller returns
    None when any reported currency has no dated FX rate.
    """
    model = CashReconstruction(
        [
            ("2024-06-01", "GBP", Decimal("1000")),
            ("2024-06-01", "USD", Decimal("300")),
        ],
        [("2024-05-02", "CONTRIBUTION", 300.0, "USD")],
        [],
    )
    assert model.balances_at("2024-05-01") == {"GBP": Decimal("1000")}


def test_a_foreign_buy_lowers_cash_by_its_gbp_value() -> None:
    """The replay rows reaching here are already converted (#549), so an
    HKD purchase takes ``price / rate`` out of the sterling balance -- not
    the raw HKD figure, which moved cash by ~10x the real amount."""
    rows = gbp_replay_rows(
        [("9988", "BUY", 10.0, 80.0, "2024-01-02", None, None, "HKD")],
        lambda currency, as_of: 9.8,
    )
    model = CashReconstruction([("2024-01-01", "GBP", Decimal("1000"))], [], rows)
    balance = model.balances_at("2024-01-02")
    assert balance is not None
    assert float(balance["GBP"]) == pytest.approx(1000 - 800 / 9.8)


def test_an_unconvertible_trade_nulls_the_whole_days_cash() -> None:
    """No dated rate for the trade's day: the movement is unknown, so the
    day's cash is NULL rather than the anchor pretending nothing moved."""
    rows = gbp_replay_rows(
        [("9988", "BUY", 10.0, 80.0, "2024-01-02", None, None, "HKD")],
        lambda currency, as_of: None,
    )
    model = CashReconstruction([("2024-01-01", "GBP", Decimal("1000"))], [], rows)
    assert model.balances_at("2024-01-02") is None
    # A day before the trade is unaffected -- it is outside the interval.
    assert model.balances_at("2024-01-01") == {"GBP": Decimal("1000")}
