from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
import sqlite3

import pytest

from app.repositories import db
from app.repositories.backtest_repo import (
    BacktestIntegrityError,
    BacktestRepository,
    DetectorCacheKey,
)
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    TechnicalResultV1,
    TechnicalsV1,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def _technicals(price: str = "100") -> TechnicalsV1:
    return TechnicalsV1(
        price=Decimal(price),
        sma10=Decimal("99"),
        sma30=Decimal("98"),
        sma50=Decimal("97"),
        sma150=Decimal("90"),
        sma200=Decimal("80"),
        rsi14=Decimal("60"),
        atr14=Decimal("2"),
        volume=Decimal("1000.125"),
        vol_ma50=Decimal("900.5"),
        rel_volume=Decimal("1.11"),
        high_52w=Decimal("110"),
        low_52w=Decimal("70"),
        high_base=Decimal("105"),
        handle_low=Decimal("95"),
        pct_from_52w_high=Decimal("-9.09"),
        pct_change_week=Decimal("2.5"),
    )


def _fragment(
    *,
    price: str = "100",
    detector_version: str = DIGEST_A,
    input_revision: str = DIGEST_B,
) -> DetectorFragmentEnvelopeV1:
    return DetectorFragmentEnvelopeV1(
        schema_version="scan_detector_fragment.v1",
        security_id="sec-001",
        date=date(2026, 7, 31),
        detector="technical_indicators_v1",
        detector_version=detector_version,
        detector_api_version="1",
        input_revision=input_revision,
        result=TechnicalResultV1(technicals=_technicals(price)),
    )


def _key(fragment: DetectorFragmentEnvelopeV1) -> DetectorCacheKey:
    return DetectorCacheKey(
        security_id=fragment.security_id,
        date=fragment.date,
        detector=fragment.detector,
        detector_version=fragment.detector_version,
        input_revision=fragment.input_revision,
    )


def _repo(path) -> BacktestRepository:
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    return repo


def test_cache_primary_key_is_exact_and_identical_content_reopens(tmp_path) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    fragment = _fragment()

    first = repo.compare_and_insert_detector_fragment(
        _key(fragment), fragment.canonical_json_bytes()
    )
    second = repo.compare_and_insert_detector_fragment(
        _key(fragment), fragment.canonical_json_bytes()
    )
    reopened = _repo(path).detector_fragment(_key(fragment))

    assert first == second == reopened == fragment
    conn = repo._connect()
    try:
        pk = [
            str(row[1])
            for row in sorted(
                conn.execute("PRAGMA table_info(scan_reconstruction_cache)"),
                key=lambda row: int(row[5]) if row[5] else 99,
            )
            if row[5]
        ]
        assert pk == [
            "security_id",
            "date",
            "detector",
            "detector_version",
            "input_revision",
        ]
    finally:
        conn.close()


def test_version_or_input_change_is_a_cache_miss(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    fragments = (
        _fragment(),
        _fragment(detector_version=DIGEST_C),
        _fragment(input_revision=DIGEST_C),
    )
    for fragment in fragments:
        repo.compare_and_insert_detector_fragment(
            _key(fragment), fragment.canonical_json_bytes()
        )
    assert repo.detector_cache_count() == 3


def test_conflicting_content_or_key_is_integrity_error_and_rolls_back(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    first = _fragment(price="100")
    conflict = _fragment(price="101")
    repo.compare_and_insert_detector_fragment(_key(first), first.canonical_json_bytes())

    with pytest.raises(BacktestIntegrityError) as content_error:
        repo.compare_and_insert_detector_fragment(
            _key(first), conflict.canonical_json_bytes()
        )
    assert content_error.value.code == "integrity_error"

    mismatched_key = replace(_key(first), security_id="other")
    with pytest.raises(BacktestIntegrityError) as key_error:
        repo.compare_and_insert_detector_fragment(
            mismatched_key, first.canonical_json_bytes()
        )
    assert key_error.value.code == "integrity_error"
    assert repo.detector_cache_count() == 1
    assert repo.detector_fragment(_key(first)) == first


def test_cache_rows_are_sql_immutable_and_digest_is_repository_owned(tmp_path) -> None:
    repo = _repo(tmp_path / "backtest.db")
    fragment = _fragment()
    repo.compare_and_insert_detector_fragment(
        _key(fragment), fragment.canonical_json_bytes()
    )
    conn = repo._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE scan_reconstruction_cache SET scan_result_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM scan_reconstruction_cache")
    finally:
        conn.close()

    conn = repo._connect()
    try:
        conn.execute("DROP TRIGGER scan_reconstruction_cache_immutable_update")
        conn.execute(
            "UPDATE scan_reconstruction_cache SET scan_result_digest=?",
            ("0" * 64,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(BacktestIntegrityError, match="digest"):
        repo.detector_fragment(_key(fragment))


def test_concurrent_identical_writers_use_independent_connections_and_converge(
    tmp_path,
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    fragment = _fragment()

    def write(_index: int) -> DetectorFragmentEnvelopeV1:
        independent = BacktestRepository(db.make_connect(lambda: path))
        return independent.compare_and_insert_detector_fragment(
            _key(fragment), fragment.canonical_json_bytes()
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(write, range(8)))

    assert results == (fragment,) * 8
    assert repo.detector_cache_count() == 1
