"""Story 2.3 coverage: the full I/O matrix for ``MarketView`` (AD-3/AD-18)
-- bound/no-look-ahead behavior, scan-eligibility timing, and unknown-
security handling."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, RosterCaptureCommit
from app.repositories.historical_price_repo import (
    HistoricalPriceRepository,
    StoredHistoricalEvidence,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
)
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.market_view import (
    MarketView,
    MarketViewBoundError,
    PRICE_HISTORY_COLUMNS,
    UnselectedSecurityError,
)
from app.services.backtest.market_planes import (
    HistoricalMarketPlanes,
    MarketDataPolicyError,
)
from app.services.backtest.currency import prepare_fx_closes
from app.services.backtest.strategy_evidence import (
    EvidenceCapableViewV1,
    EvidenceKind,
)
from app.services.backtest.run_universe import (
    RunUniverseError,
    RunUniverseErrorCode,
)
from app.services.backtest.snapshot_profile import (
    MonthlySnapshotCommitV1,
    ProfileDetectorV1,
    SnapshotMemberV1,
    SnapshotProfileV1,
)
from app.services.backtest.source_manifest import detector_source_manifests
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.strategy_job import RegimeBenchmarkPinV1
from app.services.backtest.trading_calendar import TradingCalendar

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECURITY_ID = "sec-001"


# ---------------------------------------------------------------------------
# Price evidence fixtures
# ---------------------------------------------------------------------------


class _FakeTicker:
    def __init__(
        self,
        frame: pd.DataFrame,
        symbol: str,
        *,
        currency: str = "USD",
        exchange_timezone: str = "America/New_York",
    ) -> None:
        self._frame = frame
        self._symbol = symbol
        self._currency = currency
        self._exchange_timezone = exchange_timezone

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": self._currency,
            "exchangeTimezoneName": self._exchange_timezone,
        }


def _commit_price_evidence(
    repo: HistoricalPriceRepository,
    *,
    security_id: str,
    symbol: str,
    start: date,
    end: date,
    sessions: tuple[date, ...],
    closes: tuple[float, ...],
    currency: str = "USD",
    quote_unit: str = "USD",
    exchange_timezone: str = "America/New_York",
) -> str:
    frame = pd.DataFrame(
        {
            "Open": [close - 1 for close in closes],
            "High": [close + 1 for close in closes],
            "Low": [close - 2 for close in closes],
            "Close": list(closes),
            "Adj Close": list(closes),
            "Volume": [1_000.0 for _ in closes],
            "Dividends": [0.0 for _ in closes],
            "Stock Splits": [0.0 for _ in closes],
        },
        index=pd.DatetimeIndex(
            [session.isoformat() for session in sessions], tz=exchange_timezone
        ),
    )
    request = HistoricalEvidenceRequest(
        security_id=security_id,
        alias_revision=DIGEST_B,
        symbol=symbol,
        start=start,
        end=end,
        expected_currency="GBP" if quote_unit == "GBp" else currency,
        expected_quote_unit=quote_unit,
        expected_timezone=exchange_timezone,
        expected_sessions=sessions,
        allowed_observed_symbols=(symbol,),
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: _FakeTicker(
            frame,
            symbol,
            currency=quote_unit,
            exchange_timezone=exchange_timezone,
        ),
        clock=lambda: NOW,
    ).fetch(request)
    repo.commit(payload)
    return payload.data_revision


def _price_repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    repo.ensure_schema()
    return repo


def _fx_evidence(
    closes: tuple[tuple[date, float], ...],
    *,
    start: date = date(2026, 6, 1),
    end: date = date(2026, 6, 10),
) -> StoredHistoricalEvidence:
    request_contract = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }
    return StoredHistoricalEvidence(
        data_revision=DIGEST_C,
        security_id="fx:GBPUSD=X",
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version=REQUEST_CONTRACT_VERSION,
        requested_symbol="GBPUSD=X",
        observed_symbol="GBPUSD=X",
        alias_revision=DIGEST_B,
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="Europe/London",
        start=start.isoformat(),
        end=end.isoformat(),
        request_contract=request_contract,
        response_metadata_digest=DIGEST_A,
        canonical_manifest_json="{}",
        rows=tuple(
            {
                "session": session.isoformat(),
                "open": close.hex(),
                "high": close.hex(),
                "low": close.hex(),
                "close": close.hex(),
                "adj_close": close.hex(),
                "volume": float(0).hex(),
                "dividends": float(0).hex(),
                "stock_splits": float(0).hex(),
            }
            for session, close in closes
        ),
        actions=(),
    )


# ---------------------------------------------------------------------------
# Scan-result (monthly snapshot) fixtures -- mirrors
# tests/backtest/test_snapshot_coverage_repository.py's established pattern.
# ---------------------------------------------------------------------------


def _authoritative_detectors() -> tuple[ProfileDetectorV1, ...]:
    manifests = detector_source_manifests(PROJECT_ROOT)
    return tuple(
        ProfileDetectorV1(
            detector_id=detector.detector_id,
            detector_api_version=detector.detector_api_version,
            detector_version=manifests[detector.detector_id].digest,
        )
        for detector in DETECTOR_REGISTRY
    )


def _profile() -> SnapshotProfileV1:
    return SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v1",
        record_schema_version="historical_scan_record.v1",
        detectors=_authoritative_detectors(),
        roster_policy_version="ReconstructionRosterPolicyV1",
        roster_digest=DIGEST_A,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        yfinance_request_contract_version="yfinance-daily-v1",
        yfinance_ingestion_version="ingestion-v1",
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )


#: Deterministic (content-derived, no randomness) -- computed once so every
#: test can pass the exact ``profile_hash`` the committed profile actually
#: has, rather than an unrelated placeholder digest.
PROFILE_HASH = _profile().profile_hash


def _record(month: str) -> HistoricalScanRecordV1:
    original = HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )
    payload = original.model_dump(mode="python")
    provenance = dict(payload["provenance"])
    provenance["calendar_dataset_digest"] = TradingCalendar().session_table_digest()
    provenance["detector_versions"] = {
        item.detector_id: item.detector_version for item in _authoritative_detectors()
    }
    payload["provenance"] = provenance
    if month != "2026-07":
        session = {"2026-06": "2026-06-30", "2026-05": "2026-05-29"}[month]
        payload["snapshot_month"] = month
        payload["as_of_session_date"] = session
    provisional = HistoricalScanRecordV1.model_validate(payload, strict=False)
    revision = _evidence_for_record(provisional).data_revision
    provenance = dict(provisional.provenance.model_dump(mode="python"))
    provenance["provider_data_revision"] = revision
    provenance["provider_evidence_manifest_digest"] = revision
    payload = provisional.model_dump(mode="python")
    payload["provenance"] = provenance
    return HistoricalScanRecordV1.model_validate(payload)


def _evidence_for_record(record: HistoricalScanRecordV1):
    from app.repositories.historical_price_repo import StoredHistoricalEvidence
    from app.services.backtest.canonical_manifest import canonical_json, manifest_digest

    session = record.as_of_session_date
    start = f"{session.year}-01-01"
    end = date.fromordinal(session.toordinal() + 1).isoformat()
    rows = (
        {
            "session": session.isoformat(),
            "open": float(100).hex(),
            "high": float(102).hex(),
            "low": float(99).hex(),
            "close": float(101).hex(),
            "adj_close": float(101).hex(),
            "volume": float(1000).hex(),
            "dividends": float(0).hex(),
            "stock_splits": float(0).hex(),
        },
    )
    manifest = {
        "canonicalizer_version": "HistoricalEvidenceCanonicalizerV1",
        "request_contract_version": record.provenance.provider_request_contract_version,
        "request": {
            "start": start,
            "end": end,
            "interval": "1d",
            "prepost": False,
            "auto_adjust": False,
            "back_adjust": False,
            "actions": True,
            "repair": False,
            "keepna": True,
            "rounding": False,
            "timeout": 15,
            "raise_errors": True,
        },
        "requested_symbol": record.observed_symbol,
        "observed_symbol": record.observed_symbol,
        "currency": record.currency,
        "quote_unit": record.quote_unit,
        "quote_unit_scale": "1",
        "exchange_timezone": "America/New_York",
        "rows": rows,
        "provider": "yfinance",
        "provider_version": "1.4.1",
        "security_id": record.security_id,
        "alias_revision": DIGEST_B,
        "actions": (),
    }
    rendered = canonical_json(manifest)
    return StoredHistoricalEvidence(
        data_revision=manifest_digest(manifest),
        security_id=record.security_id,
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version=record.provenance.provider_request_contract_version,
        requested_symbol=record.observed_symbol,
        observed_symbol=record.observed_symbol,
        alias_revision=DIGEST_B,
        currency=record.currency,
        quote_unit=record.quote_unit,
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start=start,
        end=end,
        request_contract=manifest["request"],
        response_metadata_digest=DIGEST_C,
        canonical_manifest_json=rendered,
        rows=rows,
        actions=(),
    )


class _Verifier:
    def __init__(self, snapshot: MonthlySnapshotCommitV1) -> None:
        self._evidence = {
            item.data_revision: item
            for item in (_evidence_for_record(record) for record in snapshot.records)
        }

    def verify(self, data_revision: str):  # noqa: ANN201
        return self._evidence[data_revision]


def _roster_commit() -> RosterCaptureCommit:
    return RosterCaptureCommit(
        lineage_id="lineage-1",
        roster_digest=DIGEST_A,
        roster_manifest_json='{"schema_version":"ReconstructionRosterManifestV1"}',
        policy_version="ReconstructionRosterPolicyV1",
        identity_registry_revision="d" * 64,
        identity_registry_json='{"identities":[]}',
        identity_evidence_digest="e" * 64,
        alias_revision=DIGEST_B,
        alias_manifest_json='{"entries":[]}',
        alias_evidence_digest="f" * 64,
        captured_at=NOW.isoformat(),
        identities=(("sec-001", "XNAS", "CAFÉ", "1" * 64),),
        aliases=(
            (
                "sec-001",
                "yfinance",
                "XNAS",
                "CAFÉ",
                None,
                None,
                "fixture",
                "4" * 64,
                "provider_evidence",
            ),
        ),
        sources=(("datahub_sp500", "2" * 64, "[]", NOW.isoformat()),),
        members=(
            (
                "sec-001",
                "XNAS",
                "CAFÉ",
                "USD",
                '["datahub_sp500"]',
                "[]",
                "3" * 64,
            ),
        ),
    )


def _backtest_repo(tmp_path: Path) -> BacktestRepository:
    repo = BacktestRepository(
        db.make_connect(lambda: tmp_path / "backtest.db"),
        clock=lambda: date(2026, 8, 11),
    )
    repo.ensure_schema()
    repo.commit_roster_capture(_roster_commit())
    return repo


def _commit_month(
    repo: BacktestRepository, profile: SnapshotProfileV1, month: str
) -> HistoricalScanRecordV1:
    record = _record(month)
    commit = MonthlySnapshotCommitV1.build(
        profile=profile,
        snapshot_month=month,
        provenance_quality="best_effort_reconstructed",
        members=(SnapshotMemberV1.valid_scan(record),),
        records=(record,),
        committed_at=NOW,
        as_of=date(2026, 8, 11),
    )
    repo.commit_snapshot_month(commit, _Verifier(commit))
    return record


# ---------------------------------------------------------------------------
# price_history -- bound / no-look-ahead / unknown-security
# ---------------------------------------------------------------------------


def test_price_history_returns_only_rows_on_or_before_the_bound(tmp_path) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
        date(2026, 6, 5),
    )
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0, 102.0, 103.0, 104.0),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    frame = view.price_history(SECURITY_ID)

    assert list(frame.index) == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    assert tuple(frame.columns) == PRICE_HISTORY_COLUMNS


@pytest.mark.parametrize(
    (
        "source_currency",
        "quote_unit",
        "base_currency",
        "native_close",
        "expected_close",
    ),
    [
        ("GBP", "GBP", "USD", 10.0, Decimal("12.50000000")),
        ("USD", "USD", "GBP", 12.5, Decimal("10.00000000")),
        ("GBP", "GBp", "USD", 1000.0, Decimal("12.50000000")),
    ],
)
def test_base_currency_history_uses_quote_units_and_bounded_fx(
    tmp_path,
    source_currency: str,
    quote_unit: str,
    base_currency: str,
    native_close: float,
    expected_close: Decimal,
) -> None:
    price_repo = _price_repo(tmp_path)
    session = date(2026, 6, 3)
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="TEST.L" if source_currency == "GBP" else "TEST",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=(session,),
        closes=(native_close,),
        currency=source_currency,
        quote_unit=quote_unit,
        exchange_timezone=(
            "Europe/London" if source_currency == "GBP" else "America/New_York"
        ),
    )
    fx = _fx_evidence(((session, 1.25),))
    view = MarketView(
        as_of_session=session,
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
        base_currency=base_currency,
        fx_evidence=fx,
        prepared_fx=prepare_fx_closes(fx),
    )

    rows = view.base_currency_close_history(SECURITY_ID, limit=1)

    assert rows.index.tolist() == [session]
    assert rows.iloc[0]["close"] == expected_close
    assert rows.iloc[0]["reason"] is None
    assert rows.iloc[0]["source_currency"] == source_currency
    assert rows.iloc[0]["source_quote_unit"] == quote_unit
    assert rows.iloc[0]["fx_rate"] == Decimal("1.25")
    assert rows.iloc[0]["fx_session"] == session
    assert rows.iloc[0]["fx_revision"] == fx.data_revision
    assert rows.iloc[0]["policy_version"] == "CurrencyConversionPolicyV1"


def test_base_currency_history_preserves_rows_outside_fx_revision_and_uses_no_future_fx(
    tmp_path,
) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = tuple(date(2026, 6, day) for day in (1, 2, 3, 4))
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="TEST.L",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(10.0, 10.0, 10.0, 10.0),
        currency="GBP",
        quote_unit="GBP",
        exchange_timezone="Europe/London",
    )
    fx = _fx_evidence(
        ((date(2026, 6, 3), 1.25), (date(2026, 6, 4), 1.5)),
        start=date(2026, 6, 3),
        end=date(2026, 6, 10),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 4),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
        base_currency="USD",
        fx_evidence=fx,
        prepared_fx=prepare_fx_closes(fx),
    )

    rows = view.base_currency_close_history(SECURITY_ID, limit=4)

    assert rows.index.tolist() == list(sessions)
    assert rows["reason"].iloc[:2].tolist() == [
        "fx_outside_coverage",
        "fx_outside_coverage",
    ]
    assert rows["reason"].iloc[2:].isna().all()
    assert rows["close"].iloc[:2].isna().all()
    assert rows["close"].iloc[2] == Decimal("12.50000000")
    assert rows["fx_session"].iloc[2] == sessions[2]
    assert rows["close"].iloc[3] == Decimal("15.00000000")
    assert rows["fx_session"].iloc[3] == sessions[3]


def test_base_currency_history_marks_excessive_fx_carry_unavailable(tmp_path) -> None:
    price_repo = _price_repo(tmp_path)
    session = date(2026, 6, 8)
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="TEST.L",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=(session,),
        closes=(10.0,),
        currency="GBP",
        quote_unit="GBP",
        exchange_timezone="Europe/London",
    )
    fx = _fx_evidence(
        ((date(2026, 6, 1), 1.25),),
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
    )
    view = MarketView(
        as_of_session=session,
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
        base_currency="USD",
        fx_evidence=fx,
        prepared_fx=prepare_fx_closes(fx),
    )

    row = view.base_currency_close_history(SECURITY_ID, limit=1).iloc[0]

    assert pd.isna(row["close"])
    assert row["reason"] == "fx_stale"
    assert row["fx_revision"] == fx.data_revision


def test_base_currency_history_keeps_market_view_selection_and_bound_errors(
    tmp_path,
) -> None:
    view = _universe_view(tmp_path, (SECURITY_ID,))
    with pytest.raises(UnselectedSecurityError):
        view.base_currency_close_history("sec-outside", limit=2)

    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=(date(2026, 6, 1), date(2026, 6, 2)),
        closes=(100.0, 101.0),
    )
    bound_view = MarketView(
        as_of_session=date(2026, 7, 1),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )
    with pytest.raises(MarketViewBoundError):
        bound_view.base_currency_close_history(SECURITY_ID, limit=2)


def test_reference_history_is_bounded_without_widening_trade_reads(tmp_path) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
    )
    revision = _commit_price_evidence(
        price_repo,
        security_id="reference-spy",
        symbol="SPY",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0, 102.0, 103.0),
    )
    pin = RegimeBenchmarkPinV1(
        security_id="reference-spy",
        identity_registry_revision=DIGEST_A,
        alias_revision=DIGEST_B,
        price_revision=revision,
        action_revision=revision,
        evidence_digest=revision,
        request_start=date(2026, 6, 1),
        request_end=date(2026, 6, 10),
        session_policy="canonical_exchange_sessions_v2",
        calendar_mic="XNYS",
        calendar_session_table_digest=DIGEST_C,
        price_plane_policy_version="HistoricalMarketPlanesV1",
    )
    access = price_repo.open_read(revision)
    try:
        view = MarketView(
            as_of_session=date(2026, 6, 3),
            profile_hash=PROFILE_HASH,
            security_price_revisions={},
            selected_universe=(SECURITY_ID,),
            backtest_repo=_backtest_repo(tmp_path),
            historical_price_repo=price_repo,
            regime_benchmark=pin,
            regime_benchmark_access=access,
        )

        frame = view.regime_benchmark_history(
            "reference-spy", limit=10, columns=("close",)
        )

        assert list(frame.index) == list(sessions[:3])
        with pytest.raises(UnselectedSecurityError):
            view.price_history("reference-spy")
        with pytest.raises(UnselectedSecurityError):
            view.regime_benchmark_history("other-reference")
    finally:
        access.close()


def test_price_history_returns_bounded_rows_and_requested_columns(tmp_path) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
    )
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0, 102.0, 103.0),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 4),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    frame = view.price_history(SECURITY_ID, limit=2, columns=("close", "volume"))

    assert list(frame.index) == [date(2026, 6, 3), date(2026, 6, 4)]
    assert tuple(frame.columns) == ("close", "volume")


@pytest.mark.parametrize(
    ("limit", "columns"),
    [(0, None), (-1, None), (True, None), (None, ()), (None, ("bad",))],
)
def test_price_history_rejects_invalid_bounds(tmp_path, limit, columns) -> None:
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=(date(2026, 6, 1),),
        closes=(100.0,),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 1),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    with pytest.raises(MarketDataPolicyError) as exc_info:
        view.price_history(SECURITY_ID, limit=limit, columns=columns)

    assert exc_info.value.code == "invalid_price_history_request"


def test_price_history_uses_a_prepared_plane_without_repository_io(
    tmp_path, monkeypatch
) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3))
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0, 102.0),
    )
    prepared = HistoricalMarketPlanes.from_evidence(price_repo.get(revision))

    def fail_get(_revision: str):
        raise AssertionError("prepared MarketView must not read the repository")

    monkeypatch.setattr(price_repo, "get", fail_get)
    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
        prepared_planes={SECURITY_ID: prepared},
    )

    frame = view.price_history(SECURITY_ID)

    assert list(frame.index) == list(sessions)


def test_price_history_uses_active_v2_bounded_access_without_complete_get(
    tmp_path, monkeypatch
) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (
        date(2024, 12, 30),
        date(2025, 1, 2),
        date(2025, 1, 3),
    )
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2024, 12, 1),
        end=date(2025, 2, 1),
        sessions=sessions,
        closes=(100.0, 101.0, 102.0),
    )
    price_repo.migrate_v1_to_v2()
    price_repo.activate_v2(review_reference="market-view-test")
    price_repo.reset_read_counters()
    access = price_repo.open_read(revision)

    def fail_get(_revision: str):
        raise AssertionError("active-v2 MarketView must not complete-read")

    monkeypatch.setattr(price_repo, "get", fail_get)
    view = MarketView(
        as_of_session=date(2025, 1, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
        price_accesses={SECURITY_ID: access},
    )

    frame = view.price_history(SECURITY_ID, limit=2, columns=("close",))

    assert list(frame.index) == [date(2025, 1, 2), date(2025, 1, 3)]
    assert list(frame["close"]) == [Decimal("101.0"), Decimal("102.0")]
    assert price_repo.read_counters.complete_revision_materializations == 0


def test_price_history_reuses_lazy_prepared_plane_across_views(tmp_path, monkeypatch):
    price_repo = _price_repo(tmp_path)
    sessions = (date(2025, 1, 2), date(2025, 1, 3))
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2025, 1, 1),
        end=date(2025, 2, 1),
        sessions=sessions,
        closes=(100.0, 101.0),
    )
    price_repo.migrate_v1_to_v2()
    price_repo.activate_v2(review_reference="market-view-plane-cache-test")
    access = price_repo.open_read(revision)
    original_bounded = access.bounded
    calls = 0

    def counted(*, through, limit=None, columns=None):
        nonlocal calls
        calls += 1
        return original_bounded(through=through, limit=limit, columns=columns)

    monkeypatch.setattr(access, "bounded", counted)
    cache = {}
    common = {
        "profile_hash": PROFILE_HASH,
        "security_price_revisions": {SECURITY_ID: revision},
        "selected_universe": (SECURITY_ID,),
        "backtest_repo": _backtest_repo(tmp_path),
        "historical_price_repo": price_repo,
        "price_accesses": {SECURITY_ID: access},
        "prepared_plane_cache": cache,
    }

    first = MarketView(as_of_session=date(2025, 1, 2), **common)
    second = MarketView(as_of_session=date(2025, 1, 3), **common)

    first.price_history(SECURITY_ID, limit=1, columns=("close",))
    second.price_history(SECURITY_ID, limit=1, columns=("close",))

    assert calls == 1
    assert set(cache) == {SECURITY_ID}
    access.close()


def test_price_history_out_of_bound_evidence_raises_stable_error(tmp_path) -> None:
    price_repo = _price_repo(tmp_path)
    sessions = (date(2026, 6, 1), date(2026, 6, 2))
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0),
    )
    beyond_bound = date(2026, 7, 1)
    view = MarketView(
        as_of_session=beyond_bound,
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    with pytest.raises(MarketViewBoundError) as exc_info:
        view.price_history(SECURITY_ID)

    assert exc_info.value.code == "bound_violation"
    assert exc_info.value.security_id == SECURITY_ID
    assert exc_info.value.as_of_session == beyond_bound


def test_price_history_as_of_session_exactly_at_exclusive_end_raises(
    tmp_path,
) -> None:
    """``plane.end`` is exclusive (yfinance-style half-open interval) --
    ``as_of_session == plane.end`` must raise, not silently succeed as if
    it were still in-bound."""
    price_repo = _price_repo(tmp_path)
    sessions = (date(2026, 6, 1), date(2026, 6, 2))
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=sessions,
        closes=(100.0, 101.0),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 10),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    with pytest.raises(MarketViewBoundError):
        view.price_history(SECURITY_ID)


def test_price_history_selected_security_without_evidence_returns_empty_frame(
    tmp_path,
) -> None:
    price_repo = _price_repo(tmp_path)
    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    frame = view.price_history(SECURITY_ID)

    assert frame.empty
    assert tuple(frame.columns) == PRICE_HISTORY_COLUMNS


def test_security_price_revisions_mapping_is_detached_from_caller_mutation(
    tmp_path,
) -> None:
    price_repo = _price_repo(tmp_path)
    revisions = {SECURITY_ID: "z" * 64}
    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions=revisions,
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )
    revisions["sec-injected"] = "y" * 64

    assert "sec-injected" not in view.security_price_revisions
    with pytest.raises(TypeError):
        view.security_price_revisions["sec-injected"] = "y" * 64  # type: ignore[index]


# ---------------------------------------------------------------------------
# scan_result -- committed-month visibility timing
# ---------------------------------------------------------------------------


def test_scan_result_without_any_committed_month_returns_none(tmp_path) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    view = MarketView(
        as_of_session=date(2026, 6, 30),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
    )

    assert view.scan_result(SECURITY_ID) is None


def test_scan_result_is_invisible_before_its_own_as_of_session(tmp_path) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    _commit_month(backtest_repo, profile, "2026-06")

    view = MarketView(
        as_of_session=date(2026, 6, 29),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
    )

    assert view.scan_result(SECURITY_ID) is None


def test_scan_result_returns_prior_committed_month_while_inside_the_next(
    tmp_path,
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    june_record = _commit_month(backtest_repo, profile, "2026-06")
    _commit_month(backtest_repo, profile, "2026-07")

    # D is inside July (month M+1) but before July's own as-of session
    # (2026-07-31) -- June's record must still answer, never July's.
    view = MarketView(
        as_of_session=date(2026, 7, 15),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
    )

    result = view.scan_result(SECURITY_ID)

    assert result is not None
    assert result.snapshot_month == june_record.snapshot_month
    assert result.digest() == june_record.digest()


def test_scan_result_switches_once_superseded_by_the_next_committed_month(
    tmp_path,
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    _commit_month(backtest_repo, profile, "2026-06")
    july_record = _commit_month(backtest_repo, profile, "2026-07")

    view = MarketView(
        as_of_session=date(2026, 7, 31),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
    )

    result = view.scan_result(SECURITY_ID)

    assert result is not None
    assert result.snapshot_month == july_record.snapshot_month
    assert result.digest() == july_record.digest()


def test_scan_result_reuses_one_current_month_cache_entry(
    tmp_path, monkeypatch
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    _commit_month(backtest_repo, profile, "2026-06")
    _commit_month(backtest_repo, profile, "2026-07")
    original = backtest_repo.latest_committed_scan_result
    calls = 0

    def counted(*, profile_hash, security_id, as_of_session):
        nonlocal calls
        calls += 1
        return original(
            profile_hash=profile_hash,
            security_id=security_id,
            as_of_session=as_of_session,
        )

    monkeypatch.setattr(backtest_repo, "latest_committed_scan_result", counted)
    cache: dict[tuple[str, str], HistoricalScanRecordV1 | None] = {}
    month: dict[str, str] = {}
    for session in (date(2026, 7, 15), date(2026, 7, 20)):
        view = MarketView(
            as_of_session=session,
            profile_hash=PROFILE_HASH,
            security_price_revisions={},
            selected_universe=(SECURITY_ID,),
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            scan_cache=cache,
            scan_cache_month=month,
        )
        assert view.scan_result(SECURITY_ID) is not None
    assert calls == 1
    assert len(cache) == 1


def test_scan_result_refreshes_at_month_end_visibility_boundary(
    tmp_path, monkeypatch
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    _commit_month(backtest_repo, profile, "2026-06")
    july_record = _commit_month(backtest_repo, profile, "2026-07")
    original = backtest_repo.latest_committed_scan_result
    calls = 0

    def counted(*, profile_hash, security_id, as_of_session):
        nonlocal calls
        calls += 1
        return original(
            profile_hash=profile_hash,
            security_id=security_id,
            as_of_session=as_of_session,
        )

    monkeypatch.setattr(backtest_repo, "latest_committed_scan_result", counted)
    cache: dict[tuple[str, str], HistoricalScanRecordV1 | None] = {}
    month: dict[str, str] = {}
    for session in (date(2026, 7, 15), date(2026, 7, 31)):
        view = MarketView(
            as_of_session=session,
            profile_hash=PROFILE_HASH,
            security_price_revisions={},
            selected_universe=(SECURITY_ID,),
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            scan_cache=cache,
            scan_cache_month=month,
        )
        result = view.scan_result(SECURITY_ID)
    assert calls == 2
    assert result is not None
    assert result.snapshot_month == july_record.snapshot_month


def test_scan_result_refreshes_on_last_weekday_when_calendar_month_end_is_weekend(
    tmp_path, monkeypatch
) -> None:
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    profile = _profile()
    _commit_month(backtest_repo, profile, "2026-05")
    may_record = _record("2026-05")
    original = backtest_repo.latest_committed_scan_result
    calls = 0

    def counted(*, profile_hash, security_id, as_of_session):
        nonlocal calls
        calls += 1
        return original(
            profile_hash=profile_hash,
            security_id=security_id,
            as_of_session=as_of_session,
        )

    monkeypatch.setattr(backtest_repo, "latest_committed_scan_result", counted)
    cache: dict[tuple[str, str], HistoricalScanRecordV1 | None] = {}
    month: dict[str, str] = {}
    for session in (date(2026, 5, 28), date(2026, 5, 29)):
        view = MarketView(
            as_of_session=session,
            profile_hash=PROFILE_HASH,
            security_price_revisions={},
            selected_universe=(SECURITY_ID,),
            backtest_repo=backtest_repo,
            historical_price_repo=price_repo,
            scan_cache=cache,
            scan_cache_month=month,
        )
        result = view.scan_result(SECURITY_ID)

    assert calls == 2
    assert result is not None
    assert result.snapshot_month == may_record.snapshot_month


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_market_view_satisfies_market_view_v1(tmp_path) -> None:
    from app.services.backtest.strategy_protocol import MarketViewV1

    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=_price_repo(tmp_path),
    )

    assert isinstance(view, MarketViewV1)


# ---------------------------------------------------------------------------
# Selected-universe scoping (Story 4.2)
# ---------------------------------------------------------------------------


def _universe_view(tmp_path, universe: tuple[str, ...]) -> MarketView:
    return MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=universe,
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=_price_repo(tmp_path),
    )


def test_selected_universe_is_canonicalized_on_construction(tmp_path) -> None:
    view = _universe_view(tmp_path, ("sec-msft", SECURITY_ID, "sec-msft"))

    assert view.selected_universe == (SECURITY_ID, "sec-msft")


def test_unselected_security_signal_is_rejected_not_silently_dropped(
    tmp_path,
) -> None:
    view = _universe_view(tmp_path, (SECURITY_ID,))

    with pytest.raises(UnselectedSecurityError) as exc_info:
        view.require_selected("sec-not-selected")

    assert exc_info.value.code == "unselected_security"
    assert exc_info.value.security_id == "sec-not-selected"
    assert exc_info.value.selected_universe == (SECURITY_ID,)


def test_unselected_security_reads_are_rejected(tmp_path) -> None:
    view = _universe_view(tmp_path, (SECURITY_ID,))

    with pytest.raises(UnselectedSecurityError):
        view.price_history("sec-not-selected")
    with pytest.raises(UnselectedSecurityError):
        view.scan_result("sec-not-selected")


def test_empty_selected_universe_is_rejected(tmp_path) -> None:
    with pytest.raises(RunUniverseError) as exc_info:
        _universe_view(tmp_path, ())

    assert exc_info.value.code is RunUniverseErrorCode.EMPTY_UNIVERSE


# ---------------------------------------------------------------------------
# Evidence capabilities / coverage (#471)
# ---------------------------------------------------------------------------


def test_evidence_capabilities_declare_every_kind(tmp_path) -> None:
    """A pinned Run evidences price *and* every scan detector fragment."""
    view = _universe_view(tmp_path, (SECURITY_ID,))

    assert view.evidence_capabilities == frozenset(EvidenceKind)
    assert isinstance(view, EvidenceCapableViewV1)


def test_evidence_coverage_reports_price_and_scan_evidence(tmp_path) -> None:
    """A security with pinned price and a visible scan reports both."""
    backtest_repo = _backtest_repo(tmp_path)
    price_repo = _price_repo(tmp_path)
    _commit_month(backtest_repo, _profile(), "2026-06")
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 7, 10),
        sessions=(date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)),
        closes=(100.0, 101.0, 102.0),
    )
    view = MarketView(
        as_of_session=date(2026, 6, 30),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=price_repo,
    )

    coverage = view.evidence_coverage(SECURITY_ID)

    assert coverage.security_id == SECURITY_ID
    assert coverage.sessions == 3
    assert coverage.columns == PRICE_HISTORY_COLUMNS
    assert coverage.kinds == frozenset(EvidenceKind)


def test_evidence_coverage_without_price_evidence_reports_scan_only(tmp_path) -> None:
    """Scan visibility is not gated on price evidence, and vice versa."""
    backtest_repo = _backtest_repo(tmp_path)
    _commit_month(backtest_repo, _profile(), "2026-06")
    view = MarketView(
        as_of_session=date(2026, 6, 30),
        profile_hash=PROFILE_HASH,
        security_price_revisions={},
        selected_universe=(SECURITY_ID,),
        backtest_repo=backtest_repo,
        historical_price_repo=_price_repo(tmp_path),
    )

    coverage = view.evidence_coverage(SECURITY_ID)

    assert coverage.sessions == 0
    assert coverage.columns == ()
    assert EvidenceKind.PRICE_HISTORY not in coverage.kinds
    assert EvidenceKind.SCAN_STAGE in coverage.kinds


def test_evidence_coverage_of_an_unselected_security_is_zero_not_an_error(
    tmp_path,
) -> None:
    """Preflight is a diagnostic: it answers, it never raises."""
    view = _universe_view(tmp_path, (SECURITY_ID,))

    with pytest.raises(UnselectedSecurityError):
        view.price_history("sec-outside")

    coverage = view.evidence_coverage("sec-outside")

    assert coverage.security_id == "sec-outside"
    assert coverage.sessions == 0
    assert coverage.kinds == frozenset()


def test_evidence_coverage_of_a_bound_violating_security_is_zero_not_an_error(
    tmp_path,
) -> None:
    price_repo = _price_repo(tmp_path)
    revision = _commit_price_evidence(
        price_repo,
        security_id=SECURITY_ID,
        symbol="AAPL",
        start=date(2026, 6, 1),
        end=date(2026, 6, 10),
        sessions=(date(2026, 6, 1), date(2026, 6, 2)),
        closes=(100.0, 101.0),
    )
    view = MarketView(
        as_of_session=date(2026, 7, 1),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: revision},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=price_repo,
    )

    with pytest.raises(MarketViewBoundError):
        view.price_history(SECURITY_ID)

    coverage = view.evidence_coverage(SECURITY_ID)

    assert coverage.sessions == 0
    assert coverage.kinds == frozenset()


def test_evidence_coverage_survives_a_repository_failure(tmp_path) -> None:
    """Any read failure degrades one security, never the whole evaluation."""

    class _ExplodingPriceRepo:
        def get(self, revision: str) -> object:
            raise RuntimeError("evidence store offline")

    view = MarketView(
        as_of_session=date(2026, 6, 3),
        profile_hash=PROFILE_HASH,
        security_price_revisions={SECURITY_ID: "d" * 64},
        selected_universe=(SECURITY_ID,),
        backtest_repo=_backtest_repo(tmp_path),
        historical_price_repo=cast(Any, _ExplodingPriceRepo()),
    )

    coverage = view.evidence_coverage(SECURITY_ID)

    assert coverage.sessions == 0
    assert coverage.kinds == frozenset()


def test_evidence_coverage_of_an_empty_security_id_answers_rather_than_raises(
    tmp_path,
) -> None:
    coverage = _universe_view(tmp_path, (SECURITY_ID,)).evidence_coverage("")

    assert coverage.sessions == 0
    assert coverage.kinds == frozenset()
