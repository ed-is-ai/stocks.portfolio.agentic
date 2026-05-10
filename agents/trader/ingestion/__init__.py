"""Staging-folder ingestion system for portfolio transaction CSVs."""

from agents.trader.ingestion.base import IngestResult, IngestorBase
from agents.trader.ingestion.generic import GenericIngestor
from agents.trader.ingestion.sipp import SIPPIngestor
from agents.trader.ingestion.work_pension import WorkPensionIngestor

__all__ = [
    "IngestorBase",
    "IngestResult",
    "SIPPIngestor",
    "WorkPensionIngestor",
    "GenericIngestor",
]
