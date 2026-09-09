"""Evidence connections bound contention and retain safe durable diagnostics."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from time import monotonic

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest import worker
from app.services.backtest.strategy_job import BootstrapSubmissionV1, StrategyJobStatus


@pytest.mark.parametrize(
    "repository_type", [HistoricalPriceRepository, BacktestRepository]
)
def test_wal_reader_snapshot_and_bounded_writer_recovery(
    tmp_path, monkeypatch, repository_type
):
    path = tmp_path / "evidence.db"
    repo = repository_type(db.make_connect(lambda: path))
    repo.ensure_schema()
    with db.session(repo._connect) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
        conn.execute("CREATE TABLE contention_probe (value INTEGER)")
        conn.execute("INSERT INTO contention_probe VALUES (1)")

    def write(value):
        with db.session(repo._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO contention_probe VALUES (?)", (value,))

    with closing(repo._connect()) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM contention_probe").fetchall() == [(1,)]
        pool.submit(write, 2).result(timeout=3)
        assert reader.execute("SELECT value FROM contention_probe").fetchall() == [(1,)]
        reader.commit()
        assert reader.execute("SELECT value FROM contention_probe").fetchall() == [
            (1,),
            (2,),
        ]

        # Exercise the same SQLite busy handler with a short test-only budget.
        monkeypatch.setattr(db, "EVIDENCE_SQLITE_BUSY_TIMEOUT_MS", 75)
        reader.execute("BEGIN IMMEDIATE")
        started = monotonic()
        with pytest.raises(sqlite3.OperationalError) as failure:
            pool.submit(write, 3).result(timeout=3)
        assert 0.05 <= monotonic() - started < 3
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert failure.value.sqlite_errorname == "SQLITE_BUSY"
        assert "code=5; name=SQLITE_BUSY" in db.sqlite_failure_detail(
            failure.value, "evidence.write", "fallback"
        )
        reader.rollback()
        pool.submit(write, 4).result(timeout=3)
        assert reader.execute("SELECT value FROM contention_probe").fetchall() == [
            (1,),
            (2,),
            (4,),
        ]

    # A failed transaction rolls back its earlier statements and is not replayed.
    with pytest.raises(sqlite3.OperationalError):
        with db.session(repo._connect) as conn:
            conn.execute("INSERT INTO contention_probe VALUES (5)")
            conn.execute("INSERT INTO missing_table VALUES (1)")
    with closing(repo._connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM contention_probe").fetchone()[0] == 3


def _raise_sqlite_failure():
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute('SELECT * FROM "private-account-secret"')


@pytest.mark.parametrize("stage", ["construct", "execute"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_worker_persists_original_sqlite_identity_without_sensitive_text(
    tmp_path, monkeypatch, stage, wrapped
):
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "jobs.db"))
    repo.ensure_schema()
    repo.create_bootstrap_job(BootstrapSubmissionV1(idempotency_key="diagnostic"))
    claimed = repo.claim_next_strategy_job()
    assert claimed is not None

    def fail(*args, **kwargs):
        try:
            _raise_sqlite_failure()
        except sqlite3.Error as exc:
            if wrapped:
                raise RuntimeError("private-wrapper-secret") from exc
            raise

    class BrokenEngine:
        run = staticmethod(fail)

    monkeypatch.setattr(
        worker,
        "build_stage_walk_engine",
        fail if stage == "construct" else lambda *a, **k: BrokenEngine(),
    )
    assert (
        worker.main(
            ["--job-id", claimed.job.id, "--claim-token", claimed.claim_token],
            repository_factory=lambda: repo,
        )
        == 1
    )
    # Fetch again through the repository: this is durable job failure detail.
    failed = repo.strategy_job(claimed.job.id)
    assert failed.status is StrategyJobStatus.FAILED
    assert (
        failed.failure_detail
        == f"worker.{stage}: sqlite3.OperationalError; code=1; name=SQLITE_ERROR"
    )
    assert failed.failure_detail is not None
    assert "private" not in failed.failure_detail


def test_sqlite_diagnostic_handles_implicit_context_and_cycles():
    try:
        _raise_sqlite_failure()
    except sqlite3.Error:
        try:
            raise RuntimeError("private-wrapper-secret")
        except RuntimeError as exc:
            assert (
                "sqlite3.OperationalError; code=1; name=SQLITE_ERROR"
                in db.sqlite_failure_detail(exc, "evidence.read", "fallback")
            )
    cyclic = RuntimeError("private")
    cyclic.__cause__ = cyclic
    assert db.sqlite_failure_detail(cyclic, "evidence.read", "fallback") == "fallback"
