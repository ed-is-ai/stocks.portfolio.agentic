"""Momentum pipeline wiring.

Thin Step adapters declare the typed stage boundaries and delegate to the agents'
``run`` methods, so the agents stay loose and pipeline-ignorant while the contract
lives here. ``extraction`` is kept as an explicit pre-step (it is conditional and
side-effecting — it refreshes the watchlist file — so it is not part of the
linear chain).
"""

from __future__ import annotations

from agents.alert.alert_agent import AlertAgent
from agents.analyst.analyst_agent import AnalystAgent
from agents.extraction.extraction_agent import ExtractionAgent
from agents.scanner.scanner_agent import ScannerAgent
from app.schemas.alert import AlertSummary
from app.schemas.record import StockRecord
from app.workflows.pipeline import Pipeline


class ScanStep:
    """Scan watchlist tickers into scored stock records."""

    name = "scan"

    def __init__(self, agent: ScannerAgent | None = None) -> None:
        self._agent = agent or ScannerAgent(name="ScannerAgent")

    def run(self, payload: list[str]) -> list[StockRecord]:
        return self._agent.run(payload)


class AnalyseStep:
    """Analyse scanned records, attaching scoring/analysis."""

    name = "analyse"

    def __init__(self, agent: AnalystAgent | None = None) -> None:
        self._agent = agent or AnalystAgent()

    def run(self, payload: list[StockRecord]) -> list[StockRecord]:
        return self._agent.run(payload)


class AlertStep:
    """Fire alerts for actionable setups and summarise the result."""

    name = "alert"

    def __init__(self, agent: AlertAgent | None = None) -> None:
        self._agent = agent or AlertAgent(name="AlertAgent")

    def run(self, payload: list[StockRecord]) -> AlertSummary:
        return self._agent.run(payload)


def build_momentum_pipeline(
    scanner: ScannerAgent | None = None,
    analyst: AnalystAgent | None = None,
    alerter: AlertAgent | None = None,
) -> Pipeline[list[str], AlertSummary]:
    """Build the scan → analyse → alert pipeline.

    Agents may be injected so a caller (e.g. the orchestrator) can keep a
    reference to the same ``alerter`` for post-run portfolio stop checks and the
    summary email; otherwise fresh agents are created.
    """
    return (
        Pipeline.start(ScanStep(scanner))
        .then(AnalyseStep(analyst))
        .then(AlertStep(alerter))
    )


def run_extraction() -> list[str]:
    """Run the extraction pre-step (refreshes the watchlist); returns tickers."""
    return ExtractionAgent(name="ExtractionAgent").run()
