"""Point-in-time leavers no provider can price are excluded (#82 C4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from app.repositories import backtest_repo as repo_module
from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest import historical_initialization_engine as engine_module
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
    EvidenceAdapter,
    InitializationMonthError,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidencePayload,
    HistoricalEvidenceRequest,
)
from app.services.backtest.snapshot_profile import (
    MonthlySnapshotCommitV1,
    NoProviderDataProofV1,
    SnapshotContractError,
)
from app.services.backtest.strategy_job import JobFailureCode
from tests.backtest.test_point_in_time_snapshots import (
    NOW,
    POLICY_V2,
    _processor,
    _profile,
)

#: STAY is a V2 member in every fixture month with no current-source
#: membership, so the roster treats it as having left the index.
DEAD = "STAY"


class _Failing:
    """Fail ``DEAD``'s fetch with ``failure``; answer the health probe.

    The probe (``SPY``) succeeds while ``healthy``, else the provider is
    "down"; every other symbol is delegated to the real adapter.
    """

    def __init__(self, inner: EvidenceAdapter, failure: ProviderFailure) -> None:
        self._inner = inner
        self._failure = failure
        self.healthy = True
        self.calls = 0
        self.probes = 0

    def fetch(self, definition: HistoricalEvidenceRequest) -> HistoricalEvidencePayload:
        if definition.symbol == engine_module.PROBE_SYMBOL:
            self.probes += 1
            if not self.healthy:
                raise ProviderFailure(FailureCode.PROVIDER_UNAVAILABLE, "down")
            return cast(HistoricalEvidencePayload, object())
        if definition.symbol != DEAD:
            return self._inner.fetch(definition)
        self.calls += 1
        raise self._failure


def _dead_processor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: ProviderFailure
) -> tuple[CanonicalSnapshotMonthProcessor, BacktestRepository, _Failing, str]:
    processor, repo, roster = _processor(tmp_path, monkeypatch)
    failing = _Failing(processor._evidence_adapter, failure)
    processor._evidence_adapter = failing  # type: ignore[assignment]
    return processor, repo, failing, roster.roster_digest


def _contract_error() -> ProviderFailure:
    return ProviderFailure(
        FailureCode.PROVIDER_CONTRACT_ERROR, "Historical source contract mismatch"
    )


def _dead_member(repo: BacktestRepository, digest: str, month: str):  # noqa: ANN202
    write_set = repo.snapshot_month_write_set(
        _profile(digest, POLICY_V2).profile_hash, month
    )
    assert write_set is not None
    members, records = write_set
    (dead,) = [m for m in members if m.observed_symbol == DEAD]
    return dead, members, records


@pytest.mark.parametrize(
    "code", [FailureCode.PROVIDER_CONTRACT_ERROR, FailureCode.REQUIRED_DATA_MISSING]
)
def test_dead_leaver_is_excluded_and_the_month_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: FailureCode
) -> None:
    processor, repo, _failing, digest = _dead_processor(
        tmp_path, monkeypatch, ProviderFailure(code, "no data")
    )

    outcome = processor("2005-06")

    dead, members, records = _dead_member(repo, digest, "2005-06")
    assert (dead.resolution, dead.exclusion_reason) == (
        "legitimate_exclusion",
        "no_provider_data",
    )
    proof = dead.exclusion_evidence
    assert isinstance(proof, NoProviderDataProofV1)
    assert (proof.provider, proof.requested_symbol, proof.failure_code) == (
        "yfinance",
        DEAD,
        code.value,
    )
    assert proof.request_contract_version == REQUEST_CONTRACT_VERSION
    assert proof.first_attempted_at == NOW
    assert dead.provider_data_revision == proof.content_digest
    # Counts are unchanged: the leaver is in the month's set, as excluded.
    assert (len(members), len(records)) == (2, 1)
    assert outcome.no_provider_data_securities == 1
    assert outcome.reused_securities + outcome.fetched_securities == 1
    manifest = repo.snapshot_month(_profile(digest, POLICY_V2).profile_hash, "2005-06")
    assert manifest is not None
    assert (manifest.expected_count, manifest.excluded_count) == (2, 1)


def test_failure_is_memoised_across_months_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, repo, failing, digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    processor("2005-06")
    processor("2005-07")
    assert (failing.calls, failing.probes) == (1, 1)

    # A later run (fresh processor, same stores and contract) asks no one.
    later = CanonicalSnapshotMonthProcessor(
        job_id="job",
        claim_token="claim",
        profile=processor._profile,
        roster=processor._roster,
        backtest_repository=repo,
        price_repository=processor._price_repository,
        evidence_adapter=failing,  # type: ignore[arg-type]
        evidence_adapters=processor._evidence_adapters,
        clock=lambda: NOW + timedelta(hours=1),
        project_root=processor._project_root,
    )
    later("2005-08")
    assert failing.calls == 1
    june, _members, _records = _dead_member(repo, digest, "2005-06")
    august, _members, _records = _dead_member(repo, digest, "2005-08")
    assert june.exclusion_evidence is not None
    assert august.exclusion_evidence == june.exclusion_evidence.model_copy(
        update={
            "snapshot_month": "2005-08",
            "target_session": august.as_of_session_date,
        }
    )


def test_failed_probe_keeps_contract_error_fatal_without_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _repo, failing, _digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    failing.healthy = False

    for month in ("2005-06", "2005-07"):
        with pytest.raises(InitializationMonthError) as caught:
            processor(month)
        assert caught.value.code is JobFailureCode.PROVIDER_CONTRACT_ERROR

    # One probe per run; nothing memoised, so each month asked again.
    assert (failing.probes, failing.calls) == (1, 2)
    assert _attempt(processor) is None


def test_required_data_missing_needs_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _repo, failing, _digest = _dead_processor(
        tmp_path,
        monkeypatch,
        ProviderFailure(FailureCode.REQUIRED_DATA_MISSING, "no rows"),
    )
    failing.healthy = False

    assert processor("2005-06").no_provider_data_securities == 1
    assert failing.probes == 0


def _dead_id(processor: CanonicalSnapshotMonthProcessor) -> str:
    return next(
        m.security_id for m in processor._roster.members if m.provider_symbol == DEAD
    )


def _attempt(processor: CanonicalSnapshotMonthProcessor):  # noqa: ANN202
    return processor._price_repository.get_unavailable_attempt(
        _dead_id(processor), contract_version=REQUEST_CONTRACT_VERSION
    )


@pytest.mark.parametrize(
    ("alias", "age_days", "asked"),
    [("own", 89, False), ("other", 1, True), ("own", 91, True), (None, 1, True)],
    ids=["fresh", "other-alias", "expired", "unparseable"],
)
def test_memo_needs_same_alias_and_is_not_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias: str | None,
    age_days: int,
    asked: bool,
) -> None:
    processor, _repo, failing, _digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    reason = (
        "legacy free-text reason"
        if alias is None
        else json.dumps(
            {
                "alias_revision": (
                    processor._alias_revision if alias == "own" else "f" * 64
                ),
                "detail": "x",
                "failure_code": "provider_contract_error",
                "first_attempted_at": (NOW - timedelta(days=age_days)).isoformat(),
            }
        )
    )
    processor._price_repository.record_unavailable_attempt(
        security_id=_dead_id(processor),
        requested_symbol=DEAD,
        reason=reason,
        contract_version=REQUEST_CONTRACT_VERSION,
    )

    assert processor("2005-06").no_provider_data_securities == 1
    assert failing.calls == int(asked)


def test_no_provider_data_escaping_maps_to_its_job_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _repo, _failing, _digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )

    def escape(*_args: object) -> None:
        raise engine_module.NoProviderData("required_data_missing", NOW, DEAD)

    monkeypatch.setattr(processor, "_resolve_fresh_members", escape)
    with pytest.raises(InitializationMonthError) as caught:
        processor("2005-06")

    assert caught.value.code is JobFailureCode.REQUIRED_DATA_MISSING


def test_adoption_path_excludes_a_dead_leaver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update mode: STAY was a valid scan, its evidence is now unreachable."""
    healthy, repo = _healthy_month(tmp_path, monkeypatch)
    previous = healthy._profile
    profile = previous.model_copy(update={"display_version": "v2-update"})
    prices = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "prices-update.db")
    )
    prices.ensure_schema()
    failing = _Failing(healthy._evidence_adapter, _contract_error())
    update = CanonicalSnapshotMonthProcessor(
        job_id="job",
        claim_token="claim",
        profile=profile,
        roster=healthy._roster,
        backtest_repository=repo,
        price_repository=prices,
        evidence_adapter=failing,  # type: ignore[arg-type]
        evidence_adapters=healthy._evidence_adapters,
        clock=lambda: NOW,
        project_root=healthy._project_root,
        mode="update",
    )
    monkeypatch.setattr(update, "_predecessor_profile", lambda: previous)
    adopt = update._adopt_month
    adopted: list[str | None] = []

    def tracked(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = adopt(*args, **kwargs)
        adopted.append(result[0])
        return result

    monkeypatch.setattr(update, "_adopt_month", tracked)

    outcome = update("2005-06")

    # Adoption completed (LEAV adopted) instead of falling back to a
    # from-scratch month on an escaped NoProviderData.
    assert adopted == [previous.profile_hash]
    assert outcome.no_provider_data_securities == 1
    write_set = repo.snapshot_month_write_set(profile.profile_hash, "2005-06")
    assert write_set is not None
    (dead,) = [m for m in write_set[0] if m.observed_symbol == DEAD]
    assert dead.exclusion_reason == "no_provider_data"


def _healthy_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CanonicalSnapshotMonthProcessor, BacktestRepository]:
    processor, repo, _roster = _processor(tmp_path, monkeypatch)
    processor("2005-06")
    return processor, repo


def test_current_member_failure_stays_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _repo, _failing, _digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    monkeypatch.setattr(engine_module, "is_current_source", lambda _sources: True)

    with pytest.raises(InitializationMonthError) as caught:
        processor("2005-06")

    assert caught.value.code is JobFailureCode.PROVIDER_CONTRACT_ERROR
    assert caught.value.detail.endswith(f"for {DEAD}")


def test_retryable_failure_keeps_todays_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "PROVIDER_RETRY_WAITS_SECONDS", ())
    failure = ProviderFailure(
        FailureCode.PROVIDER_UNAVAILABLE, "unavailable", retryable=True
    )
    processor, _repo, failing, _digest = _dead_processor(tmp_path, monkeypatch, failure)

    with pytest.raises(InitializationMonthError) as caught:
        processor("2005-06")

    assert caught.value.code is JobFailureCode.PROVIDER_UNAVAILABLE
    assert _attempt(processor) is None
    assert failing.calls == 1


def test_v1_processor_never_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, _repo, _failing, _digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    processor._point_in_time = False  # the V1 code path, same failing member

    with pytest.raises(InitializationMonthError) as caught:
        processor._prepare_member(
            next(m for m in processor._roster.members if m.provider_symbol == DEAD),
            "2005-06",
            processor._calendar.last_session_of_month("XNYS", "2005-06"),
            NOW,
        )

    assert caught.value.code is JobFailureCode.PROVIDER_CONTRACT_ERROR


def test_commit_checks_reject_v1_profiles_and_current_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, repo, _failing, digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    processor("2005-06")
    _dead, members, records = _dead_member(repo, digest, "2005-06")

    with pytest.raises(SnapshotContractError, match="point-in-time"):
        MonthlySnapshotCommitV1._validate_members_and_records(
            _profile(digest, "ReconstructionRosterPolicyV1"),
            "2005-06",
            "best_effort_reconstructed",
            members,
            records,
        )
    monkeypatch.setattr(repo_module, "is_current_source", lambda _sources: True)
    conn = sqlite3.connect(tmp_path / "backtest.db")
    try:
        with pytest.raises(BacktestIntegrityError, match="non-current"):
            BacktestRepository._validate_snapshot_members_against_roster(
                conn,
                _profile(digest, POLICY_V2),
                "2005-06",
                members,
                provenance_quality="best_effort_reconstructed",
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# In-place CHECK widening of an existing snapshot_members table.
# ---------------------------------------------------------------------------

_THREE_REASONS = (
    "'insufficient_detector_history',\n{indent}'incomplete_detector_history'"
)


def _table_sql(conn: sqlite3.Connection) -> str:
    (sql,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='snapshot_members'"
    ).fetchone()
    return str(sql)


def _rewrite(conn: sqlite3.Connection, sql: str) -> None:
    (version,) = conn.execute("PRAGMA schema_version").fetchone()
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='snapshot_members'",
        (sql,),
    )
    conn.execute(f"PRAGMA schema_version = {version + 1}")
    conn.execute("PRAGMA writable_schema = OFF")
    conn.commit()


def _downgrade(conn: sqlite3.Connection) -> str:
    """Strip ``no_provider_data`` from both CHECKs, as a pre-C4 database."""
    current = _table_sql(conn)
    old = current
    for indent in (" " * 8, " " * 13):
        old = old.replace(
            _THREE_REASONS.format(indent=indent) + f",\n{indent}'no_provider_data'",
            _THREE_REASONS.format(indent=indent),
        )
    assert old.count("no_provider_data") == 0
    _rewrite(conn, old)
    return current


def test_old_check_is_widened_in_place_with_rows_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, repo, _failing, digest = _processor_with_month(tmp_path, monkeypatch)
    path = tmp_path / "backtest.db"
    conn = sqlite3.connect(path)
    current = _downgrade(conn)
    rows = conn.execute("SELECT COUNT(*) FROM snapshot_members").fetchone()

    repo.ensure_schema()
    (version,) = conn.execute("PRAGMA schema_version").fetchone()
    repo.ensure_schema()  # idempotent: no further rewrite

    assert _table_sql(conn) == current
    assert conn.execute("PRAGMA schema_version").fetchone() == (version,)
    assert conn.execute("SELECT COUNT(*) FROM snapshot_members").fetchone() == rows
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    assert repo.snapshot_month(_profile(digest, POLICY_V2).profile_hash, "2005-06")
    processor("2005-07")  # a dead leaver still commits after the widening


def test_single_line_legacy_check_is_widened(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    conn = sqlite3.connect(path)
    current = _table_sql(conn)
    legacy = (
        "'before_first_provider_observation', 'insufficient_detector_history', "
        "'incomplete_detector_history'"
    )
    single = current
    for indent in (" " * 8, " " * 13):
        single = single.replace(
            f"\n{indent}'before_first_provider_observation',\n{indent}"
            + _THREE_REASONS.format(indent=indent)
            + f",\n{indent}'no_provider_data'",
            legacy,
        )
    assert single.count(legacy) == 2
    _rewrite(conn, single)

    repo.ensure_schema()

    widened = _table_sql(conn)
    conn.close()
    assert widened.count(f"{legacy}, 'no_provider_data'") == 2


class _FailOnBump:
    """Connection proxy that fails the ``schema_version`` bump."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: tuple[str, ...] = ()) -> sqlite3.Cursor:
        if sql.startswith("PRAGMA schema_version ="):
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, parameters)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def test_failed_widening_leaves_the_schema_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    BacktestRepository(db.make_connect(lambda: path)).ensure_schema()
    conn = sqlite3.connect(path)
    _downgrade(conn)
    old = _table_sql(conn)
    (version,) = conn.execute("PRAGMA schema_version").fetchone()

    with pytest.raises(sqlite3.OperationalError):
        repo_module._migrate_snapshot_no_provider_data_check(
            cast(sqlite3.Connection, _FailOnBump(conn))
        )
    conn.close()

    fresh = sqlite3.connect(path)
    assert _table_sql(fresh) == old
    assert fresh.execute("PRAGMA schema_version").fetchone() == (version,)
    assert fresh.execute("PRAGMA writable_schema").fetchone() == (0,)
    assert fresh.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    fresh.close()


def test_unrecognised_check_raises(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    conn = sqlite3.connect(path)
    _downgrade(conn)
    _rewrite(conn, _table_sql(conn).replace("'incomplete_detector_history'", "'x'", 1))
    conn.close()

    with pytest.raises(BacktestIntegrityError, match="unrecognised"):
        repo.ensure_schema()


def _processor_with_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CanonicalSnapshotMonthProcessor, BacktestRepository, _Failing, str]:
    processor, repo, failing, digest = _dead_processor(
        tmp_path, monkeypatch, _contract_error()
    )
    processor("2005-06")
    return processor, repo, failing, digest
