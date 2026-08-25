from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalEvidenceIntegrityError,
    HistoricalPriceRepository,
)
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)


class FakeTicker:
    def __init__(self, close: float = 101.0) -> None:
        self.frame = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [102.0],
                "Low": [99.0],
                "Close": [close],
                "Adj Close": [100.5],
                "Volume": [1_000.0],
                "Dividends": [0.25],
                "Stock Splits": [0.0],
            },
            index=pd.DatetimeIndex(["2024-01-02"], tz="America/New_York"),
        )

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self.frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": "AAPL",
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


def _payload(
    close: float = 101.0,
    acquired_at: datetime = datetime(2026, 8, 11, tzinfo=timezone.utc),
):
    request = HistoricalEvidenceRequest(
        security_id="security-1",
        alias_revision="alias-v1",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        expected_sessions=(date(2024, 1, 2),),
        allowed_observed_symbols=("AAPL",),
    )
    return YFinanceHistoricalEvidenceAdapter(
        lambda _: FakeTicker(close), clock=lambda: acquired_at
    ).fetch(request)


def _repo(tmp_path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "historical-prices.db")
    )
    repo.ensure_schema()
    return repo


def test_commit_reuses_revision_without_duplicate_rows_and_audits_acquisition(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    first = _payload()
    later = replace(first, acquired_at="2026-08-12T00:00:00+00:00")

    assert repo.commit(first) == first.data_revision
    assert repo.commit(later) == first.data_revision
    stored = repo.get(first.data_revision)
    assert stored.data_revision == first.data_revision
    assert stored.rows == first.rows
    assert stored.actions == first.actions
    assert repo.acquisition_times(first.data_revision) == (
        "2026-08-11T00:00:00+00:00",
        "2026-08-12T00:00:00+00:00",
    )

    conn = repo._connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_price_observations"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_corporate_actions"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_changed_content_and_overlapping_interval_are_distinct_revisions(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    first = _payload()
    changed = _payload(close=101.5)
    repo.commit(first)
    repo.commit(changed)
    assert first.data_revision != changed.data_revision
    assert (
        repo.get_exact(
            security_id="security-1",
            start="2024-01-01",
            end="2024-02-01",
            data_revision=changed.data_revision,
        ).data_revision
        == changed.data_revision
    )
    with pytest.raises(EvidenceMissingError):
        repo.get_exact(
            security_id="security-1",
            start="2024-01-02",
            end="2024-02-01",
            data_revision=changed.data_revision,
        )


def test_find_cached_request_reuses_earliest_verified_revision(tmp_path) -> None:
    repo = _repo(tmp_path)
    first = _payload(close=101.0)
    later = _payload(
        close=101.5,
        acquired_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    repo.commit(first)
    repo.commit(later)

    cached = repo.find_request(
        security_id="security-1",
        requested_symbol="AAPL",
        alias_revision="alias-v1",
        start="2024-01-01",
        end="2024-02-01",
        request_contract_version=first.request_contract_version,
    )

    assert cached is not None
    assert cached.data_revision == first.data_revision


def test_find_compatible_request_can_cross_alias_revisions(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)

    cached = repo.find_compatible_request(
        security_id="security-1",
        requested_symbol="AAPL",
        start="2024-01-01",
        end="2024-02-01",
        request_contract_version=payload.request_contract_version,
    )

    assert cached is not None
    assert cached.alias_revision == "alias-v1"


def test_evidence_is_sql_immutable_and_foreign_keys_are_enforced(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)
    conn = repo._connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE historical_price_revisions SET observed_symbol='BAD'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO historical_evidence_references "
                "(consumer_type, consumer_id, data_revision, created_at) "
                "VALUES ('snapshot', 'missing', 'absent', '2026-08-11T00:00:00Z')"
            )
    finally:
        conn.close()


def test_pin_is_exact_and_integrity_verification_is_cache_only(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)
    repo.pin("snapshot", "profile:2024-01", payload.data_revision)
    assert repo.verify(payload.data_revision).data_revision == payload.data_revision

    conn = repo._connect()
    try:
        conn.execute("DROP TRIGGER historical_observation_immutable_delete")
        conn.execute(
            "DELETE FROM historical_price_observations WHERE data_revision=?",
            (payload.data_revision,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(HistoricalEvidenceIntegrityError):
        repo.verify(payload.data_revision)
    with pytest.raises(HistoricalEvidenceIntegrityError):
        repo.pin("backtest", "run-1", payload.data_revision)


def test_missing_revision_raises_stable_error(tmp_path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(EvidenceMissingError) as exc_info:
        repo.get("absent")
    assert exc_info.value.code == "evidence_missing"


def test_concurrent_identical_commit_has_one_revision_and_observation_set(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = tuple(pool.map(repo.commit, (payload, payload)))
    assert revisions == (payload.data_revision, payload.data_revision)
    conn = repo._connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_price_revisions"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_price_observations"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_constraint_failure_is_integrity_error_and_rolls_back(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    identity = json.loads(payload.canonical_manifest_json)
    identity["rows"] = [identity["rows"][0], identity["rows"][0]]
    broken = replace(
        payload,
        rows=(payload.rows[0], payload.rows[0]),
        data_revision=manifest_digest(identity),
        canonical_manifest_json=canonical_json(identity),
    )
    with pytest.raises(HistoricalEvidenceIntegrityError) as exc_info:
        repo.commit(broken)
    assert exc_info.value.code == "integrity_error"
    with pytest.raises(EvidenceMissingError):
        repo.get(broken.data_revision)
