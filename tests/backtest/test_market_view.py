"""Story 2.3 coverage: the full I/O matrix for ``MarketView`` (AD-3/AD-18)
-- bound/no-look-ahead behavior, scan-eligibility timing, and unknown-
security handling."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, RosterCaptureCommit
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.market_view import (
    MarketView,
    MarketViewBoundError,
    PRICE_HISTORY_COLUMNS,
    UnselectedSecurityError,
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
    def __init__(self, frame: pd.DataFrame, symbol: str) -> None:
        self._frame = frame
        self._symbol = symbol

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
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
            [session.isoformat() for session in sessions], tz="America/New_York"
        ),
    )
    request = HistoricalEvidenceRequest(
        security_id=security_id,
        alias_revision=DIGEST_B,
        symbol=symbol,
        start=start,
        end=end,
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=sessions,
        allowed_observed_symbols=(symbol,),
    )
    payload = YFinanceHistoricalEvidenceAdapter(
        lambda _: _FakeTicker(frame, symbol), clock=lambda: NOW
    ).fetch(request)
    repo.commit(payload)
    return payload.data_revision


def _price_repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    repo.ensure_schema()
    return repo


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
