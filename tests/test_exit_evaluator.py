"""Unit tests for ExitEvaluator."""

from agents.analyst.exit_evaluator import ExitEvaluator
from models import Position, StockAnalysis, StockRecord, StockScan


def _make_position(**overrides) -> Position:
    defaults = dict(
        ticker="TEST",
        shares=100.0,
        avg_cost=50.0,
        total_cost=5000.0,
        entry_price=50.0,
        stop_loss=46.0,
    )
    defaults.update(overrides)
    return Position(**defaults)


def _make_stock(price: float = 50.0, **overrides) -> StockRecord:
    defaults = dict(
        ticker="TEST",
        as_of="2024-01-01",
        price=price,
        volume=1_000_000,
        rel_volume=1.0,
        high_52w=120.0,
        low_52w=40.0,
        pct_from_52w_high=-10.0,
        pct_change_week=0.0,
    )
    defaults.update(overrides)
    return StockRecord.model_validate(StockScan(**defaults).model_dump())


class TestExitEvaluator:
    def setup_method(self) -> None:
        self.ev = ExitEvaluator()

    def test_no_signal_when_holding_normally(self) -> None:
        pos = _make_position(current_price=55.0, stop_loss=46.0)
        sig = self.ev.evaluate(pos, None)
        assert sig is None

    def test_no_price_returns_none(self) -> None:
        pos = _make_position(current_price=None, stop_loss=46.0)
        sig = self.ev.evaluate(pos, None)
        assert sig is None

    def test_stop_hit_returns_exit(self) -> None:
        pos = _make_position(current_price=45.0, stop_loss=46.0)
        sig = self.ev.evaluate(pos, None)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "stop" in sig.reason.lower()

    def test_stop_at_exact_level_returns_exit(self) -> None:
        pos = _make_position(current_price=46.0, stop_loss=46.0)
        sig = self.ev.evaluate(pos, None)
        assert sig is not None
        assert sig.action == "EXIT"

    def test_above_stop_no_signal(self) -> None:
        pos = _make_position(current_price=47.0, stop_loss=46.0)
        sig = self.ev.evaluate(pos, None)
        assert sig is None

    def test_add_signal_on_breakout(self) -> None:
        pos = _make_position(current_price=55.0, stop_loss=46.0)
        stock = _make_stock(price=55.0)
        stock.analysis = StockAnalysis(
            score=8,
            stage="Stage 2",
            entry_zone="broken_out",
            entry_price=54.0,
            summary="",
        )
        sig = self.ev.evaluate(pos, stock)
        assert sig is not None
        assert sig.action == "ADD"
        assert "54" in sig.reason

    def test_no_add_when_approaching(self) -> None:
        pos = _make_position(current_price=52.0, stop_loss=46.0)
        stock = _make_stock(price=52.0)
        stock.analysis = StockAnalysis(
            score=7,
            stage="Stage 2",
            entry_zone="approaching",
            summary="",
        )
        sig = self.ev.evaluate(pos, stock)
        assert sig is None

    def test_exit_takes_priority_over_add(self) -> None:
        """Stop hit wins even if entry_zone is broken_out."""
        pos = _make_position(current_price=45.0, stop_loss=46.0)
        stock = _make_stock(price=45.0)
        stock.analysis = StockAnalysis(
            score=8,
            stage="Stage 2",
            entry_zone="broken_out",
            summary="",
        )
        sig = self.ev.evaluate(pos, stock)
        assert sig is not None
        assert sig.action == "EXIT"

    def test_no_stop_no_signal(self) -> None:
        pos = _make_position(current_price=30.0, stop_loss=None)
        sig = self.ev.evaluate(pos, None)
        assert sig is None
