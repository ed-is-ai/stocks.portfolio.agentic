from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Callable, cast

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
        index=pd.DatetimeIndex(
            [session.isoformat() for session in sessions], tz="America/New_York"
        ),
    )


def _service(
    tmp_path,
    frame: pd.DataFrame,
    acquired_at: datetime = datetime(2026, 9, 22, tzinfo=timezone.utc),
) -> BenchmarkEvidenceService:
    backtest = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    backtest.ensure_schema()
    prices = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    prices.ensure_schema()
    adapter = YFinanceHistoricalEvidenceAdapter(
        lambda _symbol: _Ticker(frame),
        provider_version="test",
        clock=lambda: acquired_at,
    )
    return BenchmarkEvidenceService(
        backtest_repository=backtest,
        price_repository=prices,
        adapter=adapter,
    )


def _prices(tmp_path) -> HistoricalPriceRepository:
    return HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))


class _ScriptedPrices:
    """Real price repository with scripted candidates and ``verify`` results."""

    def __init__(
        self,
        real: HistoricalPriceRepository,
        *,
        revisions: tuple[str, ...] | None = None,
        verify: Callable[[str], Any] | None = None,
    ) -> None:
        self._real = real
        self._revisions = revisions
        self._verify = verify or real.verify
        self.verified: list[str] = []

    def exact_end_revisions(self, **kwargs: str) -> tuple[str, ...]:
        if self._revisions is not None:
            return self._revisions
        return self._real.exact_end_revisions(**kwargs)

    def verify(self, revision: str) -> Any:
        self.verified.append(revision)
        return self._verify(revision)


def _scripted(tmp_path, prices: _ScriptedPrices) -> BenchmarkEvidenceService:
    """A service over the same stores, reading prices through ``prices``."""
    return BenchmarkEvidenceService(
        backtest_repository=BacktestRepository(
            db.make_connect(lambda: tmp_path / "backtest.db")
        ),
        price_repository=cast(HistoricalPriceRepository, prices),
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


def _acquired(tmp_path, *, ma_length: int = 200):
    service = _service(tmp_path, _frame())
    result = service.acquire(
        _IDENTITY,
        _ALIAS,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
        ma_length=ma_length,
    )
    return service, result


def test_shorter_ma_reuses_longer_stored_evidence(tmp_path) -> None:
    """gh-127: MA 150 resolves against a revision starting before its warm-up."""
    service, result = _acquired(tmp_path)

    pin = service.resolve_existing(
        _IDENTITY.security_id,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
        ma_length=150,
    )

    evidence = _prices(tmp_path).verify(result.data_revision)
    assert pin == result.reference_pin()
    # The same equality the worker and preparation checks enforce.
    assert evidence.start == pin.request_start.isoformat()
    assert evidence.end == pin.request_end.isoformat()


def test_exact_warmup_revision_pins_evidence_window(tmp_path) -> None:
    service, result = _acquired(tmp_path)

    pin = service.resolve_existing(
        _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 2, 1)
    )

    evidence = _prices(tmp_path).verify(result.data_revision)
    assert pin.request_start == result.request.start
    assert evidence.start == pin.request_start.isoformat()
    assert evidence.end == pin.request_end.isoformat()


def test_revision_starting_after_warmup_is_rejected(tmp_path) -> None:
    _, result = _acquired(tmp_path, ma_length=100)
    prices = _ScriptedPrices(_prices(tmp_path), revisions=(result.data_revision,))

    with pytest.raises(BenchmarkEvidenceError, match="lacks SMA warm-up"):
        _scripted(tmp_path, prices).resolve_existing(
            _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 2, 1)
        )


def test_gappy_revision_is_rejected(tmp_path) -> None:
    _acquired(tmp_path)
    real = _prices(tmp_path)

    def gappy(revision: str) -> Any:
        evidence = real.verify(revision)
        return replace(evidence, rows=evidence.rows[:10] + evidence.rows[11:])

    with pytest.raises(BenchmarkEvidenceError, match="incomplete"):
        _scripted(tmp_path, _ScriptedPrices(real, verify=gappy)).resolve_existing(
            _IDENTITY.security_id,
            start=date(2000, 1, 1),
            end=date(2000, 2, 1),
            ma_length=150,
        )


def test_too_few_sessions_for_the_worker_minimum_is_rejected(tmp_path) -> None:
    """MA 100 still needs the worker's 200-session minimum."""
    service, _result = _acquired(tmp_path, ma_length=150)

    with pytest.raises(BenchmarkEvidenceError, match="incomplete"):
        service.resolve_existing(
            _IDENTITY.security_id,
            start=date(2000, 1, 1),
            end=date(2000, 2, 1),
            ma_length=100,
        )


def test_first_revision_passing_every_check_is_pinned(tmp_path) -> None:
    """A too-short latest-start revision falls through to a longer one."""
    service, longer = _acquired(tmp_path)
    service.acquire(
        _IDENTITY,
        _ALIAS,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
        ma_length=150,
    )

    pin = service.resolve_existing(
        _IDENTITY.security_id,
        start=date(2000, 1, 1),
        end=date(2000, 2, 1),
        ma_length=100,
    )

    assert pin == longer.reference_pin()


def test_wider_newer_revision_does_not_shadow_exact_end(tmp_path) -> None:
    exact = _service(tmp_path, _frame()).acquire(
        _IDENTITY, _ALIAS, start=date(2000, 1, 1), end=date(2000, 1, 15)
    )
    later = datetime(2026, 9, 23, tzinfo=timezone.utc)
    service = _service(tmp_path, _frame(), acquired_at=later)
    service.acquire(_IDENTITY, _ALIAS, start=date(2000, 1, 1), end=date(2000, 2, 1))

    pin = service.resolve_existing(
        _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 1, 15)
    )

    assert pin == exact.reference_pin()


def test_unknown_reference_id_is_a_benchmark_evidence_error(tmp_path) -> None:
    service, _result = _acquired(tmp_path)

    with pytest.raises(BenchmarkEvidenceError):
        service.resolve_existing(
            "not-a-reference", start=date(2000, 1, 1), end=date(2000, 2, 1)
        )


MISSING_REVISION = "0" * 64


def test_unreadable_candidate_falls_through_to_the_next(tmp_path) -> None:
    _, result = _acquired(tmp_path)
    prices = _ScriptedPrices(
        _prices(tmp_path), revisions=(MISSING_REVISION, result.data_revision)
    )

    pin = _scripted(tmp_path, prices).resolve_existing(
        _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 2, 1)
    )

    assert pin == result.reference_pin()
    assert prices.verified == [MISSING_REVISION, result.data_revision]


def test_all_failing_candidates_join_distinct_reasons_in_order(tmp_path) -> None:
    _, late = _acquired(tmp_path, ma_length=100)
    prices = _ScriptedPrices(
        _prices(tmp_path),
        revisions=(MISSING_REVISION, late.data_revision, MISSING_REVISION),
    )

    with pytest.raises(BenchmarkEvidenceError) as exc_info:
        _scripted(tmp_path, prices).resolve_existing(
            _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 2, 1)
        )

    assert str(exc_info.value) == (
        "registered benchmark evidence is unreadable; "
        "registered benchmark evidence lacks SMA warm-up"
    )


def test_candidates_are_capped_at_ten(tmp_path) -> None:
    _acquired(tmp_path)
    prices = _ScriptedPrices(_prices(tmp_path), revisions=(MISSING_REVISION,) * 11)

    with pytest.raises(BenchmarkEvidenceError, match="unreadable"):
        _scripted(tmp_path, prices).resolve_existing(
            _IDENTITY.security_id, start=date(2000, 1, 1), end=date(2000, 2, 1)
        )

    assert len(prices.verified) == 10
