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
    rebind_historical_evidence_alias,
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


def test_v2_commit_reconstructs_canonical_evidence_and_reuses_chunks(tmp_path) -> None:
    repo = _repo(tmp_path)
    first = _payload()
    rebound = rebind_historical_evidence_alias(
        first, alias_revision="alias-v2", acquired_at="2026-08-12T00:00:00+00:00"
    )

    assert repo.commit_v2(first) == first.data_revision
    assert repo.commit_v2(rebound) == rebound.data_revision
    with db.session(repo._connect) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_price_v2_revisions"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM historical_price_v2_chunks"
        ).fetchone() == (2,)
    stored = repo.get_v2(first.data_revision)
    assert stored.canonical_manifest_json == first.canonical_manifest_json
    assert stored.rows == first.rows
    assert stored.actions == first.actions
    with pytest.raises(EvidenceMissingError):
        repo.get(first.data_revision)


def test_active_v2_bounded_read_crosses_year_and_reports_chunk_counters(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    first = _payload()
    rows = (
        {**first.rows[0], "session": "2024-12-30"},
        {**first.rows[0], "session": "2025-01-02", "close": float(102).hex()},
    )
    actions = ({**first.actions[0], "session": "2024-12-30"},)
    identity = json.loads(first.canonical_manifest_json)
    identity["request"]["end"] = "2025-02-01"
    identity["rows"] = list(rows)
    identity["actions"] = list(actions)
    payload = replace(
        first,
        end="2025-02-01",
        request_contract={**first.request_contract, "end": "2025-02-01"},
        rows=rows,
        actions=actions,
        data_revision=manifest_digest(identity),
        canonical_manifest_json=canonical_json(identity),
    )
    repo.commit(payload)
    repo.migrate_v1_to_v2()
    repo.activate_v2(review_reference="bounded-read-test")

    repo.reset_read_counters()
    bounded = repo.open_read(payload.data_revision).bounded(
        through=date(2025, 1, 2), limit=2
    )

    assert [row["session"] for row in bounded.rows] == [
        "2024-12-30",
        "2025-01-02",
    ]
    assert bounded.selected_price_chunk_years == (2024, 2025)
    assert bounded.data_revision == payload.data_revision
    assert repo.read_counters.complete_revision_materializations == 0
    assert repo.read_counters.chunks_decompressed == 3
    assert repo.read_counters.rows_retained == 2


def test_active_format_never_falls_back_to_other_revision_or_format(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit_v2(payload)

    with pytest.raises(EvidenceMissingError):
        repo.open_read(payload.data_revision)


def test_active_v2_missing_chunk_fails_closed(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)
    repo.migrate_v1_to_v2()
    repo.activate_v2(review_reference="bounded-read-test")
    with db.session(repo._connect) as conn:
        conn.execute("DROP TRIGGER historical_v2_mapping_immutable_delete")
        conn.execute("DELETE FROM historical_price_v2_revision_chunks")

    with pytest.raises((EvidenceMissingError, HistoricalEvidenceIntegrityError)):
        repo.open_read(payload.data_revision).bounded(through=date(2024, 1, 2), limit=1)


def test_v2_reader_rejects_corrupt_chunk(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit_v2(payload)
    with db.session(repo._connect) as conn:
        conn.execute("DROP TRIGGER historical_v2_chunk_immutable_update")
        conn.execute("UPDATE historical_price_v2_chunks SET compressed_payload=x'00'")
    with pytest.raises(HistoricalEvidenceIntegrityError, match="invalid v2 chunk"):
        repo.get_v2(payload.data_revision)


def test_v2_reader_rejects_compressed_trailing_bytes(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit_v2(payload)
    with db.session(repo._connect) as conn:
        conn.execute("DROP TRIGGER historical_v2_chunk_immutable_update")
        compressed = conn.execute(
            "SELECT compressed_payload FROM historical_price_v2_chunks LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "UPDATE historical_price_v2_chunks SET compressed_payload=?",
            (bytes(compressed) + b"trailing-bytes",),
        )
    with pytest.raises(HistoricalEvidenceIntegrityError, match="invalid v2 chunk"):
        repo.get_v2(payload.data_revision)


def test_v2_chunk_cache_rechecks_mapping_identity(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)
    repo.migrate_v1_to_v2()
    repo.activate_v2(review_reference="cache-identity-test")
    handle = repo.open_read(payload.data_revision)
    try:
        with db.session(repo._connect) as conn:
            revision_id = int(
                conn.execute(
                    "SELECT revision_id FROM historical_price_v2_revisions "
                    "WHERE data_revision=?",
                    (payload.data_revision,),
                ).fetchone()[0]
            )
            mapping = next(
                item
                for item in repo._v2_mappings(conn, revision_id)
                if item[0] == "rows"
            )
        repo._decode_v2_chunk(mapping, handle._chunk_cache)
        tampered = ("actions", *mapping[1:])
        with pytest.raises(HistoricalEvidenceIntegrityError, match="invalid v2 chunk"):
            repo._decode_v2_chunk(tampered, handle._chunk_cache)
    finally:
        handle.close()


def test_v2_reader_rejects_oversized_chunk_claim(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit_v2(payload)
    with db.session(repo._connect) as conn:
        conn.execute("DROP TRIGGER historical_v2_chunk_immutable_update")
        conn.execute(
            "UPDATE historical_price_v2_chunks SET uncompressed_bytes=?",
            (repo._v2_chunk_max_bytes + 1,),
        )
    with pytest.raises(HistoricalEvidenceIntegrityError, match="invalid v2 chunk"):
        repo.get_v2(payload.data_revision)


def test_v2_revision_and_chunk_mappings_are_sql_immutable(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit_v2(payload)
    conn = repo._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE historical_price_v2_revisions SET metadata_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM historical_price_v2_revision_chunks")
    finally:
        conn.close()


def test_v2_splits_rows_into_calendar_year_chunks(tmp_path) -> None:
    repo = _repo(tmp_path)
    first = _payload()
    second_year_row = {**first.rows[0], "session": "2025-01-02"}
    identity = json.loads(first.canonical_manifest_json)
    identity["request"]["end"] = "2025-02-01"
    identity["rows"] = [*first.rows, second_year_row]
    payload = replace(
        first,
        end="2025-02-01",
        request_contract={**first.request_contract, "end": "2025-02-01"},
        rows=(*first.rows, second_year_row),
        data_revision=manifest_digest(identity),
        canonical_manifest_json=canonical_json(identity),
    )

    repo.commit_v2(payload)
    with db.session(repo._connect) as conn:
        mappings = conn.execute(
            "SELECT chunk_kind, chunk_year FROM historical_price_v2_revision_chunks "
            "ORDER BY chunk_order"
        ).fetchall()
    assert mappings == [("actions", 2024), ("rows", 2024), ("rows", 2025)]
    assert repo.get_v2(payload.data_revision).canonical_manifest_json == (
        payload.canonical_manifest_json
    )


def test_v2_migration_resumes_then_activates_and_rolls_back(tmp_path) -> None:
    repo = _repo(tmp_path)
    first, second = _payload(), _payload(close=102.0)
    repo.commit(first)
    repo.commit(second)

    partial = repo.migrate_v1_to_v2(max_revisions=1)
    assert (
        partial.source_revision_count,
        partial.migrated_revision_count,
        partial.completed,
    ) == (
        2,
        1,
        False,
    )
    with pytest.raises(HistoricalEvidenceIntegrityError, match="incomplete"):
        repo.activate_v2(review_reference="test evidence")
    complete = _repo(tmp_path).migrate_v1_to_v2()
    assert (complete.migrated_revision_count, complete.completed) == (2, True)
    repo.activate_v2(review_reference="test evidence")
    assert repo.get(first.data_revision).canonical_manifest_json == (
        first.canonical_manifest_json
    )
    post_activation = _payload(close=103.0)
    repo.commit(post_activation)
    assert repo.get(post_activation.data_revision).canonical_manifest_json == (
        post_activation.canonical_manifest_json
    )
    repo.rollback_v2_activation()
    assert repo.get(second.data_revision).canonical_manifest_json == (
        second.canonical_manifest_json
    )


def test_v2_activation_requires_a_review_reference(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.commit(_payload())
    repo.migrate_v1_to_v2()
    with pytest.raises(HistoricalEvidenceIntegrityError, match="review is required"):
        repo.activate_v2(review_reference=" ")


def test_v2_retention_plan_reports_references_and_grace_exclusions(tmp_path) -> None:
    repo = _repo(tmp_path)
    pinned, eligible = _payload(), _payload(close=102.0)
    recent = _payload(
        close=103.0, acquired_at=datetime(2026, 9, 9, tzinfo=timezone.utc)
    )
    for payload in (pinned, eligible, recent):
        repo.commit(payload)
    repo.migrate_v1_to_v2()
    repo.pin("snapshot", "profile:2024-01", pinned.data_revision)

    plan = repo.plan_v2_retention(grace_before="2026-09-01T00:00:00+00:00")

    assert plan.candidates == (eligible.data_revision,)
    assert dict(plan.exclusions) == {
        pinned.data_revision: "authoritative_reference",
        recent.data_revision: "within_grace_period",
    }
    repo.activate_v2(review_reference="test")
    assert repo.execute_v2_retention(plan, review_reference="test") == (1, 1)
    with pytest.raises(EvidenceMissingError):
        repo.get_v2(eligible.data_revision)
    assert repo.get_v2(pinned.data_revision).data_revision == pinned.data_revision


def test_v2_retention_refuses_a_plan_after_references_change(tmp_path) -> None:
    repo = _repo(tmp_path)
    payload = _payload()
    repo.commit(payload)
    repo.migrate_v1_to_v2()
    repo.activate_v2(review_reference="test")
    plan = repo.plan_v2_retention(grace_before="2026-09-01T00:00:00+00:00")
    repo.pin("snapshot", "profile:2024-01", payload.data_revision)

    with pytest.raises(HistoricalEvidenceIntegrityError, match="references changed"):
        repo.execute_v2_retention(plan, review_reference="test")


def test_v2_migration_refuses_insufficient_capacity_and_source_drift(tmp_path) -> None:
    repo = _repo(tmp_path)
    first, second = _payload(), _payload(close=102.0)
    repo.commit(first)
    repo.commit(second)
    with pytest.raises(HistoricalEvidenceIntegrityError, match="insufficient disk"):
        repo.migrate_v1_to_v2(available_bytes=0)
    assert repo.migrate_v1_to_v2(max_revisions=1).migrated_revision_count == 1
    repo.commit(_payload(close=103.0))
    with pytest.raises(HistoricalEvidenceIntegrityError, match="source changed"):
        repo.migrate_v1_to_v2()


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
