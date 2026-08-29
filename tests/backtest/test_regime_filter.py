"""Unit coverage for the shared market-regime entry filter (#388).

One test per row of the spec's I/O & edge-case matrix, plus the
below-to-above SMA transition test.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from app.services.backtest.regime_filter import (
    MIN_MA_LENGTH,
    REGIME_FILTER_BENCHMARK_PARAM,
    REGIME_FILTER_ENABLED_PARAM,
    REGIME_FILTER_MA_LENGTH_PARAM,
    entry_signals_permitted,
)

BENCHMARK = "sec-spy"
UNIVERSE = ("sec-aapl", "sec-spy")
AS_OF = date(2026, 1, 30)


class _View:
    """Minimal ``MarketViewV1`` stand-in exposing only ``price_history``."""

    def __init__(self, closes: list[object] | None, *, raises: bool = False) -> None:
        self._raises = raises
        if closes is None:
            self._history = pd.DataFrame({"open": []})
        else:
            start = AS_OF - timedelta(days=len(closes) - 1)
            index = [start + timedelta(days=offset) for offset in range(len(closes))]
            self._history = pd.DataFrame({"close": closes}, index=index)
        self.as_of_session = AS_OF

    def price_history(self, security_id: str) -> pd.DataFrame:
        if self._raises:
            raise RuntimeError("bound violation")
        return self._history.copy()


def _params(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        REGIME_FILTER_ENABLED_PARAM: True,
        REGIME_FILTER_BENCHMARK_PARAM: BENCHMARK,
        REGIME_FILTER_MA_LENGTH_PARAM: 3,
    }
    base.update(overrides)
    return base


def test_disabled_when_param_absent_permits() -> None:
    assert entry_signals_permitted(_View([1.0]), {}, UNIVERSE) is True


def test_disabled_when_flag_not_exactly_true_permits() -> None:
    view = _View([1.0, 2.0, 3.0])
    assert (
        entry_signals_permitted(
            view, _params(**{REGIME_FILTER_ENABLED_PARAM: 1}), UNIVERSE
        )
        is True
    )


def test_risk_on_when_latest_close_above_sma() -> None:
    view = _View([10.0, 10.0, 40.0])  # sma = 20, latest 40 > 20
    assert entry_signals_permitted(view, _params(), UNIVERSE) is True


def test_risk_off_when_latest_close_at_or_below_sma() -> None:
    view = _View([40.0, 40.0, 40.0])  # sma = 40, latest 40 not > 40
    assert entry_signals_permitted(view, _params(), UNIVERSE) is False


def test_risk_off_at_exact_sma_with_non_trivial_series() -> None:
    view = _View([10.0, 30.0, 20.0])  # sma = 20, latest 20, not > 20
    assert entry_signals_permitted(view, _params(), UNIVERSE) is False


def test_non_container_universe_fails_closed() -> None:
    view = _View([10.0, 10.0, 40.0])
    assert entry_signals_permitted(view, _params(), None) is False  # type: ignore[arg-type]


def test_one_dirty_cell_does_not_discard_the_whole_series() -> None:
    view = _View([10.0, "bad", 10.0, 40.0])  # one junk cell dropped -> 3 finite
    assert entry_signals_permitted(view, _params(), UNIVERSE) is True


def test_benchmark_not_in_universe_fails_closed() -> None:
    view = _View([10.0, 10.0, 40.0])
    assert entry_signals_permitted(view, _params(), ("sec-aapl",)) is False


def test_benchmark_empty_or_non_str_fails_closed() -> None:
    view = _View([10.0, 10.0, 40.0])
    assert (
        entry_signals_permitted(
            view, _params(**{REGIME_FILTER_BENCHMARK_PARAM: ""}), UNIVERSE
        )
        is False
    )
    assert (
        entry_signals_permitted(
            view, _params(**{REGIME_FILTER_BENCHMARK_PARAM: 123}), UNIVERSE
        )
        is False
    )


def test_bad_ma_length_fails_closed() -> None:
    view = _View([10.0, 10.0, 40.0])
    for bad in (True, 1, 0, -5, 2.5, "3", None):
        assert (
            entry_signals_permitted(
                view, _params(**{REGIME_FILTER_MA_LENGTH_PARAM: bad}), UNIVERSE
            )
            is False
        )


def test_insufficient_history_fails_closed() -> None:
    view = _View([10.0, 40.0])  # only 2 finite closes, ma_length 3
    assert entry_signals_permitted(view, _params(), UNIVERSE) is False


def test_price_history_raise_is_caught_and_fails_closed() -> None:
    view = _View([10.0, 10.0, 40.0], raises=True)
    assert entry_signals_permitted(view, _params(), UNIVERSE) is False


def test_missing_close_column_fails_closed() -> None:
    assert entry_signals_permitted(_View(None), _params(), UNIVERSE) is False


def test_non_finite_closes_are_dropped_before_count_and_sma() -> None:
    # 4 raw values, one NaN dropped -> 3 finite closes for ma_length 3.
    view = _View([10.0, math.nan, 10.0, 40.0])
    assert entry_signals_permitted(view, _params(), UNIVERSE) is True

    # Dropping leaves fewer than ma_length -> fail closed.
    short = _View([math.inf, 10.0, 40.0])
    assert entry_signals_permitted(short, _params(), UNIVERSE) is False


def test_boundary_exactly_ma_length_finite_closes_is_evaluated() -> None:
    view = _View([10.0, 10.0, 40.0])  # exactly 3
    assert entry_signals_permitted(view, _params(), UNIVERSE) is True


def test_sma_cross_from_below_to_above_flips_result() -> None:
    ma_length = MIN_MA_LENGTH + 1  # 3
    params = _params(**{REGIME_FILTER_MA_LENGTH_PARAM: ma_length})
    series = [20.0, 20.0, 20.0, 5.0, 60.0]

    earlier = _View(series[:-1])  # last 3 = [20,20,5], sma 15, latest 5 -> off
    later = _View(series)  # last 3 = [20,5,60], sma ~28.3, latest 60 -> on

    assert entry_signals_permitted(earlier, params, UNIVERSE) is False
    assert entry_signals_permitted(later, params, UNIVERSE) is True
