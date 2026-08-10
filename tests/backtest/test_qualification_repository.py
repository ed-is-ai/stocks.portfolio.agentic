from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository, QualificationResult
from app.services.backtest.historical_data_qualification import (
    EvidenceCheck,
    MANDATORY_FIXTURE_IDS,
    MANDATORY_PROBE_IDS,
    QualificationAvailabilityService,
    QualificationContract,
    QualificationRecorder,
)


def _contract(name: str = "v1") -> QualificationContract:
    return QualificationContract(name, '{"pandas":"3"}', "f" * 64, "d" * 64)


def _result(
    contract: QualificationContract, *, passed: bool = True
) -> QualificationResult:
    return QualificationResult(
        contract_digest=contract.contract_digest,
        source_versions_json=contract.source_versions_json,
        fixture_digest=contract.fixture_digest,
        probe_definition_digest=contract.probe_definition_digest,
        probe_digest="p" * 64,
        qualified_at=datetime(2026, 8, 10, tzinfo=timezone.utc).isoformat(),
        passed=passed,
        failure_code=None if passed else "provider_unavailable",
        failure_reason=None if passed else "Historical source unavailable",
    )


def _evidence(names: tuple[str, ...]) -> dict[str, EvidenceCheck]:
    return {name: EvidenceCheck(True, name * 8) for name in names}


def test_repository_is_append_only_and_restart_safe(tmp_path) -> None:
    connect = db.make_connect(lambda: tmp_path / "backtest.db")
    repo = BacktestRepository(connect)
    repo.ensure_schema()
    row_id = repo.record_qualification(_result(_contract()))
    reopened = BacktestRepository(connect)
    reopened.ensure_schema()
    assert reopened.latest_qualification("v1") is not None
    conn = connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE historical_source_qualifications SET passed=0 WHERE id=?",
                (row_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM historical_source_qualifications WHERE id=?", (row_id,)
            )
    finally:
        conn.close()


def test_latest_failure_revokes_prior_pass_and_contract_drift_fails(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    contract = _contract()
    service = QualificationAvailabilityService(repo)
    repo.record_qualification(_result(contract))
    assert service.availability(contract).available
    repo.record_qualification(_result(contract, passed=False))
    assert not service.availability(contract).available
    assert not service.availability(
        QualificationContract("v1", "changed", "f" * 64, "d" * 64)
    ).available


def test_recorder_rejects_empty_incomplete_or_extra_evidence(tmp_path) -> None:
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    repo.ensure_schema()
    instant = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    recorder = QualificationRecorder(repo, clock=lambda: instant)
    recorder.record(_contract(), {}, {})
    assert not repo.latest_qualification("v1").passed  # type: ignore[union-attr]
    incomplete = _evidence(MANDATORY_FIXTURE_IDS[:-1])
    probes = _evidence(MANDATORY_PROBE_IDS)
    recorder.record(_contract(), incomplete, probes)
    assert not repo.latest_qualification("v1").passed  # type: ignore[union-attr]
    complete = _evidence(MANDATORY_FIXTURE_IDS)
    recorder.record(_contract(), complete, probes)
    latest = repo.latest_qualification("v1")
    assert latest is not None and latest.passed
    assert latest.qualified_at == "2026-08-10T12:00:00+00:00"


def test_backtest_package_has_no_live_trading_or_fallback_imports() -> None:
    root = (
        __import__("pathlib").Path(__file__).parents[2]
        / "app"
        / "services"
        / "backtest"
    )
    source = "\n".join(path.read_text() for path in root.glob("*.py"))
    for forbidden in ("TraderAgent", "alpha_vantage", "stooq", "mcp"):
        assert forbidden not in source
