"""Tests for the price-evidence fetch-on-miss backfill service (#490)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.snapshot_price_backfill import (
    PriceEvidenceBackfillService,
    PriceEvidenceUnavailable,
)


class FakeTicker:
    """A yfinance-shaped ticker with one session's OHLC row, or none at all."""

    def __init__(self, has_rows: bool = True) -> None:
        self._has_rows = has_rows

    def history(self, **_kwargs: object) -> pd.DataFrame:
        if not self._has_rows:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [102.0],
                "Low": [99.0],
                "Close": [101.0],
                "Adj Close": [100.5],
                "Volume": [1_000.0],
                "Dividends": [0.0],
                "Stock Splits": [0.0],
            },
            index=pd.DatetimeIndex(["2024-01-02"], tz="America/New_York"),
        )

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": "AAPL",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


class _RaisingTicker:
    """Raises a network fault, classified transient (``PROVIDER_UNAVAILABLE``,
    retryable) -- distinct from a delisted ticker's non-retryable failure."""

    def history(self, **_kwargs: object) -> pd.DataFrame:
        raise ConnectionError("boom")

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {}


class _DelistedTicker:
    """Raises the way yfinance actually does for a delisted/never-existed
    symbol -- not an empty frame, an exception before any frame is parsed
    (real-world: ``YFTzMissingError: $WCOG: possibly delisted; no timezone
    found``). Classified ``PROVIDER_CONTRACT_ERROR``, not
    ``REQUIRED_DATA_MISSING`` (GH-490)."""

    def history(self, **_kwargs: object) -> pd.DataFrame:
        raise RuntimeError("$WCOG: possibly delisted; no timezone found")

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {}


def _service(
    tmp_path, ticker_factory=None, aliases: dict[str, str] | None = None
) -> tuple[PriceEvidenceBackfillService, HistoricalPriceRepository]:
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "historical-prices.db")
    )
    repo.ensure_schema()
    adapter = YFinanceHistoricalEvidenceAdapter(
        ticker_factory or (lambda _: FakeTicker()),
        sleeper=lambda _: None,
    )
    return (
        PriceEvidenceBackfillService(repo, adapter, aliases=aliases or {}),
        repo,
    )


def test_no_coverage_yet_fetches_and_commits(tmp_path) -> None:
    service, repo = _service(tmp_path)

    fetched = service.ensure_coverage("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert fetched is True
    assert (
        repo.covering_revision(
            security_id="portfolio:AAPL",
            requested_symbol="AAPL",
            start="2024-01-01",
            end="2024-01-03",
        )
        is not None
    )


def test_already_covered_skips_the_fetch(tmp_path) -> None:
    calls: list[str] = []

    def factory(symbol: str) -> FakeTicker:
        calls.append(symbol)
        return FakeTicker()

    service, _repo = _service(tmp_path, ticker_factory=factory)
    service.ensure_coverage("AAPL", date(2024, 1, 1), date(2024, 1, 3))
    assert calls == ["AAPL"]

    fetched = service.ensure_coverage("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert fetched is False
    assert calls == ["AAPL"]  # no second fetch


def test_same_day_round_trip_never_requests_an_empty_range(tmp_path) -> None:
    service, repo = _service(tmp_path)

    fetched = service.ensure_coverage("AAPL", date(2024, 1, 2), date(2024, 1, 3))

    assert fetched is True
    assert (
        repo.covering_revision(
            security_id="portfolio:AAPL",
            requested_symbol="AAPL",
            start="2024-01-02",
            end="2024-01-03",
        )
        is not None
    )


def test_definitive_failure_is_recorded_unavailable_and_raised(tmp_path) -> None:
    service, repo = _service(
        tmp_path, ticker_factory=lambda _: FakeTicker(has_rows=False)
    )

    with pytest.raises(PriceEvidenceUnavailable):
        service.ensure_coverage("HSFWA", date(2024, 1, 1), date(2024, 1, 3))

    assert repo.get_unavailable_attempt("portfolio:HSFWA") is not None


def test_unavailable_ticker_is_never_retried(tmp_path) -> None:
    calls: list[str] = []

    def factory(symbol: str) -> FakeTicker:
        calls.append(symbol)
        return FakeTicker(has_rows=False)

    service, _repo = _service(tmp_path, ticker_factory=factory)
    with pytest.raises(PriceEvidenceUnavailable):
        service.ensure_coverage("HSFWA", date(2024, 1, 1), date(2024, 1, 3))
    assert len(calls) == 1

    # A second run for the same ticker must not fetch again, and must not
    # raise -- it's an already-known gap, not a fresh failure this run.
    fetched = service.ensure_coverage("HSFWA", date(2024, 1, 1), date(2024, 1, 3))

    assert fetched is False
    assert len(calls) == 1


def test_transient_failure_is_not_recorded_and_propagates(tmp_path) -> None:
    service, repo = _service(tmp_path, ticker_factory=lambda _: _RaisingTicker())

    with pytest.raises(ProviderFailure) as excinfo:
        service.ensure_coverage("AAPL", date(2024, 1, 1), date(2024, 1, 3))

    assert excinfo.value.code is FailureCode.PROVIDER_UNAVAILABLE
    assert repo.get_unavailable_attempt("portfolio:AAPL") is None


def test_delisted_ticker_is_recorded_unavailable_not_retried_forever(tmp_path) -> None:
    """A raised (not empty-frame) provider error classifies as
    ``PROVIDER_CONTRACT_ERROR``, which must be treated as definitive just
    like ``REQUIRED_DATA_MISSING`` -- otherwise a genuinely delisted ticker
    is retried on every single pipeline run, forever (GH-490 production
    finding: WCOG)."""
    calls: list[str] = []

    def factory(symbol: str) -> _DelistedTicker:
        calls.append(symbol)
        return _DelistedTicker()

    service, repo = _service(tmp_path, ticker_factory=factory)

    with pytest.raises(PriceEvidenceUnavailable):
        service.ensure_coverage("WCOG", date(2024, 1, 1), date(2024, 1, 3))

    attempt = repo.get_unavailable_attempt("portfolio:WCOG")
    assert attempt is not None
    # The adapter's own bounded retry loop treats PROVIDER_CONTRACT_ERROR as
    # retryable within one `ensure_coverage` call (3 attempts) -- what must
    # never happen is a *second* `ensure_coverage` call (i.e. the next
    # pipeline run) attempting again at all.
    first_run_calls = len(calls)
    assert first_run_calls > 0

    fetched = service.ensure_coverage("WCOG", date(2024, 1, 1), date(2024, 1, 3))

    assert fetched is False
    assert len(calls) == first_run_calls  # no fetch attempted on the next run


def test_alias_resolution_maps_to_the_canonical_symbol(tmp_path) -> None:
    service, repo = _service(
        tmp_path,
        ticker_factory=lambda _: FakeTicker(),
        aliases={"AAPL.OLD": "AAPL"},
    )

    fetched = service.ensure_coverage("AAPL.OLD", date(2024, 1, 1), date(2024, 1, 3))

    assert fetched is True
    assert (
        repo.covering_revision(
            security_id="portfolio:AAPL",
            requested_symbol="AAPL",
            start="2024-01-01",
            end="2024-01-03",
        )
        is not None
    )
