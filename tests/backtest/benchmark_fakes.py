"""Shared fakes for SPY benchmark tests: stored evidence and a price reader."""

from __future__ import annotations

from datetime import date

from app.integrations.fx_history import BOE_FX_SERIES_REQUEST_CONTRACT_VERSION
from app.repositories.historical_price_repo import StoredHistoricalEvidence

SPY_ID = "spy-security"


def spy_evidence(
    closes: list[tuple[date, float, float]],
    *,
    security_id: str = SPY_ID,
    currency: str = "USD",
    end: str = "2025-02-01",
    **overrides: object,
) -> StoredHistoricalEvidence:
    """Return SPY evidence with ``(session, close, dividend)`` rows."""
    fields: dict[str, object] = {
        "data_revision": "rev",
        "security_id": security_id,
        "provider": "yfinance",
        "provider_version": "1",
        "request_contract_version": "1",
        "requested_symbol": "SPY",
        "observed_symbol": "SPY",
        "alias_revision": None,
        "currency": currency,
        "quote_unit": currency,
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "start": "2025-01-01",
        "end": end,
        "request_contract": {},
        "response_metadata_digest": "d",
        "canonical_manifest_json": "{}",
        "rows": tuple(
            {
                "session": session.isoformat(),
                "close": float(close).hex(),
                "dividends": float(dividend).hex(),
            }
            for session, close, dividend in closes
        ),
        "actions": (),
    }
    fields.update(overrides)
    return StoredHistoricalEvidence(**fields)  # type: ignore[arg-type]


def fx_evidence(
    rows: list[tuple[date, float]], *, end: str = "2025-03-31"
) -> StoredHistoricalEvidence:
    """Return Bank of England GBP/USD evidence with ``(session, rate)`` rows."""
    return spy_evidence(
        [(session, rate, 0.0) for session, rate in rows],
        security_id="fx",
        end=end,
        provider="bank_of_england",
        request_contract_version=BOE_FX_SERIES_REQUEST_CONTRACT_VERSION,
        requested_symbol="GBPUSD=X",
        observed_symbol="GBPUSD=X",
        exchange_timezone="Europe/London",
    )


class FakePrices:
    """A price reader over named revisions that records every ``get``."""

    def __init__(self, **evidence: StoredHistoricalEvidence) -> None:
        self.evidence = evidence
        self.calls: list[str] = []

    def get(self, data_revision: str) -> StoredHistoricalEvidence:
        """Return the stored evidence or raise ``LookupError``."""
        self.calls.append(data_revision)
        return self.evidence[data_revision]
