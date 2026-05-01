"""Portfolio signal evaluator — EXIT when stop is hit, ADD on a new pivot breakout."""

from models import ExitSignal, Position, StockRecord


class ExitEvaluator:
    """Evaluate the signal for a held position."""

    def evaluate(self, position: Position, stock: StockRecord | None) -> ExitSignal | None:
        """Return EXIT, ADD, or None (blank)."""
        price = position.current_price
        stop = position.stop_loss

        if price is not None and stop is not None and price <= stop:
            return ExitSignal(
                action="EXIT",
                reason=f"Stop loss hit — price ${price:.2f} at or below stop ${stop:.2f}",
            )

        analysis = stock.analysis if stock else None
        if analysis and analysis.entry_zone == "broken_out":
            ep = analysis.entry_price
            return ExitSignal(
                action="ADD",
                reason=f"New pivot breakout — entry ${ep:.2f}" if ep else "New pivot breakout",
            )

        return None
