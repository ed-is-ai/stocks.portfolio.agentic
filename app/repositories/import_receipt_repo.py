"""Repository for the ``portfolio_import_receipts``/``portfolio_import_rows``
tables in ``trades.db`` (GH-311).

A receipt is the durable, per-import audit record of which provider
contract interpreted a committed SIPP import and how each source row
resolved. Both tables are written on the caller's own open connection,
inside the same atomic transaction ``TraderAgent.import_sipp`` already
opens for trades/cash_flows/reconciliation/balances/snapshot -- mirrors
``CashReconciliationRepository.insert_issue_on_connection``'s pattern
exactly: takes ``conn`` explicitly, never calls ``conn.commit()`` or
``conn.rollback()`` itself.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.repositories.db import Connect


class ImportRowLineage(BaseModel):
    """One source CSV row's outcome, ready to persist as a lineage row.

    ``canonical_row_digest`` is a one-way SHA-256 over the row's
    *normalized canonical* field values (never raw CSV cell content --
    see :func:`canonical_row_digest`). ``trade_id``/``cash_flow_id`` are
    ``None`` when nothing was created for this row (a duplicate, a
    skipped/benign-empty row).
    """

    model_config = ConfigDict(frozen=True)

    physical_row_number: int
    canonical_row_digest: str
    outcome: str
    reason: str | None = None
    trade_id: int | None = None
    cash_flow_id: int | None = None


def canonical_row_digest(normalized_row: dict[str, str]) -> str:
    """Stable content digest over a row's normalized canonical values.

    Hashes the same dict ``ContractNormalizer.normalize_row`` already
    produces, never the raw CSV cell text -- keeps the digest
    provider-neutral (same shape regardless of which contract produced
    the row) and one-way/non-reversible, so no raw financial content is
    ever persisted (GH-311). Mirrors ``gbp_valuation_service._quote_
    digest``'s small self-contained SHA-256 style, but serializes via
    ``json.dumps`` (not a naive ``"|"``-joined string) so a field value
    that happens to contain the separator character (e.g. a Description
    cell with a literal ``|``) can never collide with a differently-shaped
    row (review finding).
    """
    raw = json.dumps(sorted(normalized_row.items()), separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ImportReceiptRepository:
    """Typed access to the ``portfolio_import_receipts``/``portfolio_import_
    rows`` tables."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def insert_receipt_on_connection(
        self,
        conn: Any,
        *,
        import_batch_id: str,
        portfolio_id: int | None,
        provider_id: str,
        account_type_id: str | None,
        contract_id: str,
        contract_version: str,
        contract_content_digest: str,
        source_digest: str,
        status: str,
        inserted_count: int,
        duplicate_count: int,
        skipped_count: int,
        failed_count: int,
    ) -> int:
        """Insert one import receipt row and return its new id.

        Takes the caller's open connection and does not commit -- lets
        the SIPP import's receipt write join the same transaction as its
        trade/cash-flow/reconciliation/balance/snapshot writes.
        """
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        cur = conn.execute(
            "INSERT INTO portfolio_import_receipts "
            "(import_batch_id, portfolio_id, provider_id, account_type_id, "
            "contract_id, contract_version, contract_content_digest, "
            "source_digest, created_at, status, inserted_count, "
            "duplicate_count, skipped_count, failed_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_batch_id,
                portfolio_id,
                provider_id,
                account_type_id,
                contract_id,
                contract_version,
                contract_content_digest,
                source_digest,
                created_at,
                status,
                inserted_count,
                duplicate_count,
                skipped_count,
                failed_count,
            ),
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def insert_rows_on_connection(
        self, conn: Any, receipt_id: int, rows: list[ImportRowLineage]
    ) -> None:
        """Insert one row-lineage entry per source data row.

        Takes the caller's open connection and does not commit -- see
        ``insert_receipt_on_connection``.
        """
        conn.executemany(
            "INSERT INTO portfolio_import_rows "
            "(receipt_id, physical_row_number, canonical_row_digest, "
            "outcome, reason, trade_id, cash_flow_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    receipt_id,
                    row.physical_row_number,
                    row.canonical_row_digest,
                    row.outcome,
                    row.reason,
                    row.trade_id,
                    row.cash_flow_id,
                )
                for row in rows
            ],
        )


__all__ = ["ImportReceiptRepository", "ImportRowLineage", "canonical_row_digest"]
