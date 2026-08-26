"""Unit tests for ``ImportReceiptRepository`` (GH-311)."""

import pytest

from app.repositories import db
from app.repositories.import_receipt_repo import (
    ImportReceiptRepository,
    ImportRowLineage,
    canonical_row_digest,
)
from app.repositories.trades_repo import TradesRepository


@pytest.fixture
def trades_connect(tmp_path):
    """A Connect factory over an initialised temp trades.db."""
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return connect


def _insert_receipt(
    repo: ImportReceiptRepository,
    conn,
    *,
    status: str = "ok",
    inserted_count: int = 1,
    duplicate_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> int:
    return repo.insert_receipt_on_connection(
        conn,
        import_batch_id="batch-1",
        portfolio_id=1,
        provider_id="interactive_investor",
        account_type_id="SIPP",
        contract_id="ii_sipp",
        contract_version="1",
        contract_content_digest="digest-contract",
        source_digest="digest-source",
        status=status,
        inserted_count=inserted_count,
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
    )


def test_insert_receipt_and_rows_round_trip(trades_connect):
    """A receipt and its row-lineage entries round-trip through raw SQL --
    the repository's job is only to write on the caller's connection."""
    repo = ImportReceiptRepository(trades_connect)
    trades = TradesRepository(trades_connect)
    trade_id = trades.insert("AAPL", "BUY", 10, 100.0, "2024-01-01")
    with db.session(trades_connect) as conn:
        receipt_id = _insert_receipt(repo, conn)
        assert isinstance(receipt_id, int) and receipt_id > 0

        rows = [
            ImportRowLineage(
                physical_row_number=0,
                canonical_row_digest="rowdigest-0",
                outcome="inserted",
                trade_id=trade_id,
            ),
            ImportRowLineage(
                physical_row_number=1,
                canonical_row_digest="rowdigest-1",
                outcome="duplicate",
            ),
        ]
        repo.insert_rows_on_connection(conn, receipt_id, rows)

    conn = trades_connect()
    try:
        receipt_row = conn.execute(
            "SELECT import_batch_id, portfolio_id, provider_id, account_type_id, "
            "contract_id, contract_version, contract_content_digest, "
            "source_digest, status, inserted_count, duplicate_count, "
            "skipped_count, failed_count FROM portfolio_import_receipts "
            "WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        assert receipt_row == (
            "batch-1",
            1,
            "interactive_investor",
            "SIPP",
            "ii_sipp",
            "1",
            "digest-contract",
            "digest-source",
            "ok",
            1,
            0,
            0,
            0,
        )

        row_lineage = conn.execute(
            "SELECT physical_row_number, canonical_row_digest, outcome, "
            "trade_id, cash_flow_id FROM portfolio_import_rows "
            "WHERE receipt_id = ? ORDER BY physical_row_number",
            (receipt_id,),
        ).fetchall()
        assert row_lineage == [
            (0, "rowdigest-0", "inserted", trade_id, None),
            (1, "rowdigest-1", "duplicate", None, None),
        ]
    finally:
        conn.close()


def test_insert_receipt_on_connection_never_commits(trades_connect):
    """Writing a receipt on an open connection must not itself commit --
    the caller's rollback must be able to undo it (mirrors
    ``CashReconciliationRepository.insert_issue_on_connection``'s
    contract)."""
    repo = ImportReceiptRepository(trades_connect)
    conn = trades_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _insert_receipt(repo, conn)
        conn.rollback()
    finally:
        conn.close()

    conn = trades_connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_import_receipts"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_insert_rows_on_connection_never_commits(trades_connect):
    """Writing row-lineage entries on an open connection must not itself
    commit."""
    repo = ImportReceiptRepository(trades_connect)
    conn = trades_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        receipt_id = _insert_receipt(repo, conn)
        repo.insert_rows_on_connection(
            conn,
            receipt_id,
            [
                ImportRowLineage(
                    physical_row_number=0,
                    canonical_row_digest="rowdigest-0",
                    outcome="inserted",
                )
            ],
        )
        conn.rollback()
    finally:
        conn.close()

    conn = trades_connect()
    try:
        receipts = conn.execute(
            "SELECT COUNT(*) FROM portfolio_import_receipts"
        ).fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM portfolio_import_rows").fetchone()[0]
    finally:
        conn.close()
    assert receipts == 0
    assert rows == 0


def test_canonical_row_digest_is_deterministic_and_order_independent():
    """The digest is over sorted canonical field values -- key insertion
    order in the source dict must not change the result."""
    row_a = {"date": "2024-01-01", "quantity": "10"}
    row_b = {"quantity": "10", "date": "2024-01-01"}
    assert canonical_row_digest(row_a) == canonical_row_digest(row_b)


def test_canonical_row_digest_differs_for_different_content():
    row_a = {"date": "2024-01-01", "quantity": "10"}
    row_b = {"date": "2024-01-01", "quantity": "20"}
    assert canonical_row_digest(row_a) != canonical_row_digest(row_b)
