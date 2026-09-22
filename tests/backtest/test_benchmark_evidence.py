from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.benchmark_evidence import (
    BenchmarkEvidenceError,
    BenchmarkEvidenceService,
)
from app.services.backtest.historical_price_evidence import (
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.security_identity import AliasEntryV1, SecurityIdentityV1
from app.services.backtest.trading_calendar import TradingCalendar


_IDENTITY = SecurityIdentityV1(
    security_id="6f8d45e7-50a2-4b79-9a6c-7f9c3f0c1c62",
    mic="ARCX",
    provider_symbol="SPY",
    evidence_digest="s" * 64,
)
_ALIAS = AliasEntryV1(
    security_id=_IDENTITY.security_id,
    provider="yfinance",
    mic="ARCX",
    observed_symbol="SPY",
    effective_from=date(1993, 1, 22),
    effective_to=None,
    evidence_source="historical-listing-fixture",
    evidence_digest="a" * 64,
    provenance="manual_override",
)


class _Ticker:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, Any]:
        assert repair is False
        return {
            "symbol": "SPY",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


def _frame(*, drop: date | None = None) -> pd.DataFrame:
    sessions = TradingCalendar().sessions_in_range(
        "XNYS", date(1999, 1, 1), date(2000, 2, 1)
    )
    if drop is not None:
        sessions = tuple(session for session in sessions if session != drop)
    values = [float(index + 100) for index in range(len(sessions))]
    return pd.DataFrame(
        {
            "Open": values,
            "High": [value + 1 for value in values],
            "Low": [value - 1 for value in values],
            "Close": values,
            "Adj Close": values,
            "Volume": [1_000.0] * len(values),
            "Dividends": [0.0] * len(values),
            "Stock Splits": [0.0] * len(values),
        },
        index=pd.DatetimeIndex([session.isoformat() for session in sessions], tz="America/New_York"),
    )


def _service(tmp_path, frame: pd.DataFrame) -> BenchmarkEvidenceService:
    backtest = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    backtest.ensure_schema()
    prices = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    prices.ensure_schema()
    adapter = YFinanceHistoricalEvidenceAdapter(
        lambda _symbol: _Ticker(frame),
        provider_version="test",
        clock=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    return BenchmarkEvidenceService(
        backtest_repository=backtest,
        price_repository=prices,
        adapter=adapter,
    )


def test_spy_reference_acquisition_includes_200_session_warmup(tmp_path) -> None:
    result = _service(tmp_path, _frame()).acquire(
        _IDENTITY,
        _ALIAS,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
    )

    assert len(result.warmup_sessions) == 200
    assert result.first_decision_session == date(2000, 1, 3)
    assert result.request.start == result.warmup_sessions[0]
    assert result.request.alias_revision == result.registration.alias_revision
    assert len(result.data_revision) == 64
    assert result.price_revision == result.action_revision == result.data_revision
    assert result.session_policy == "canonical_exchange_sessions_v2"
    assert result.price_plane_policy_version == "HistoricalMarketPlanesV1"
    assert result.reference_pin().security_id == _IDENTITY.security_id
    assert result.reference_pin().alias_revision == result.registration.alias_revision


def test_existing_reference_resolves_to_same_offline_pin(tmp_path) -> None:
    service = _service(tmp_path, _frame())
    result = service.acquire(
        _IDENTITY,
        _ALIAS,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
    )

    resolved = service.resolve_existing(
        _IDENTITY.security_id,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
    )

    assert resolved == result.reference_pin()


def test_reference_acquisition_rejects_missing_canonical_session(tmp_path) -> None:
    with pytest.raises(BenchmarkEvidenceError, match="missing"):
        _service(tmp_path, _frame(drop=date(1999, 3, 22))).acquire(
            _IDENTITY,
            _ALIAS,
            start=date(2000, 1, 1),
            end=date(2000, 2, 1),
        )


def test_current_only_alias_cannot_be_used_as_historical_continuity(tmp_path) -> None:
    current_only = AliasEntryV1(
        **{
            **_ALIAS.__dict__,
            "effective_from": date(2026, 1, 1),
        }
    )
    with pytest.raises(BenchmarkEvidenceError, match="continuity"):
        _service(tmp_path, _frame()).acquire(
            _IDENTITY,
            current_only,
            start=date(2000, 1, 1),
            end=date(2000, 2, 1),
        )


def test_invalid_ma_length_and_calendar_bounds_fail_closed(tmp_path) -> None:
    service = _service(tmp_path, _frame())
    with pytest.raises(BenchmarkEvidenceError, match="length"):
        service.acquire(
            _IDENTITY,
            _ALIAS,
            start=date(2000, 1, 1),
            end=date(2000, 2, 1),
            ma_length="200",  # type: ignore[arg-type]
        )
    with pytest.raises(BenchmarkEvidenceError, match="calendar authority"):
        service.acquire(
            _IDENTITY,
            _ALIAS,
            start=date(2000, 1, 1),
            end=date(2101, 1, 1),
        )
