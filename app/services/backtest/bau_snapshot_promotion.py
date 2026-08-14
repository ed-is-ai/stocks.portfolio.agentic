"""Promotion and replay of published scanner-owned BAU envelopes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.bau_run_envelope import BauRunEnvelopeStore
from app.services.backtest.historical_price_evidence import HistoricalEvidencePayload
from app.services.backtest.observed_bau_record_builder import ObservedBauRecordBuilder
from app.services.backtest.snapshot_profile import (
    MonthlySnapshotCommitV1,
    SnapshotMemberV1,
)


class BauPromotionError(RuntimeError):
    """A published BAU envelope could not be safely promoted."""


class BauSnapshotPromotionService:
    """Reload an envelope, commit its evidence, then pin only the winner."""

    def __init__(
        self,
        *,
        backtest_repository: BacktestRepository,
        price_repository: HistoricalPriceRepository,
        envelope_directory: Path,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._backtest = backtest_repository
        self._prices = price_repository
        self._store = BauRunEnvelopeStore(envelope_directory)
        self._clock = clock

    def promote_run(self, run_id: str) -> bool:
        """Promote a reloaded completed envelope; return false when ineligible."""
        envelope = self._store.load(run_id)
        capture = envelope.capture
        if capture is None:
            return False
        decision = self._backtest.is_promotable_bau(
            capture.profile, envelope, envelope_store=self._store
        )
        if not decision.eligible:
            raise BauPromotionError(decision.reason or "BAU envelope is ineligible")

        builder = ObservedBauRecordBuilder()
        records = tuple(
            builder.build(member, roster_captured_at=capture.roster_captured_at).record
            for member in capture.members
        )
        # Persist exact run-owned responses before commit so the existing
        # immutable snapshot verifier can validate them.  These are evidence
        # revisions, not pins; failed/conflicting commits leave no retention ref.
        for member in capture.members:
            raw = member.raw_evidence
            self._prices.commit(
                HistoricalEvidencePayload(
                    security_id=raw.security_id,
                    alias_revision=raw.alias_revision,
                    provider=raw.provider,
                    provider_version=raw.provider_version,
                    request_contract_version=raw.request_contract_version,
                    requested_symbol=raw.requested_symbol,
                    observed_symbol=raw.observed_symbol,
                    currency=raw.currency,
                    quote_unit=raw.quote_unit,
                    quote_unit_scale=raw.quote_unit_scale,
                    exchange_timezone=raw.exchange_timezone,
                    start=raw.start.isoformat(),
                    end=raw.end.isoformat(),
                    request_contract=raw.request_contract,
                    rows=raw.rows,
                    actions=raw.actions,
                    response_metadata_digest=raw.response_metadata_digest,
                    data_revision=raw.data_revision,
                    canonical_manifest_json=raw.canonical_manifest_json,
                    acquired_at=raw.acquired_at.isoformat(),
                )
            )
        now = self._clock().astimezone(timezone.utc)
        commit = MonthlySnapshotCommitV1.build(
            profile=capture.profile,
            snapshot_month=capture.snapshot_month,
            provenance_quality="observed_bau",
            members=tuple(SnapshotMemberV1.valid_scan(record) for record in records),
            records=records,
            committed_at=now,
            as_of=now.date(),
            source_run_id=envelope.run_id,
            observed_at=capture.captured_at,
        )
        try:
            self._backtest.commit_snapshot_month(
                commit, self._prices, require_active_profile=True
            )
        except BacktestIntegrityError as exc:
            raise BauPromotionError("BAU snapshot commit failed") from exc
        self.reconcile_pins(capture.profile.profile_hash, capture.snapshot_month)
        return True

    def replay_completed_envelopes(self) -> tuple[str, ...]:
        """Recover completed-but-uncommitted captures on each later BAU run."""
        replayed: list[str] = []
        failures: list[str] = []
        for run_id in self._store.completed_capture_run_ids():
            try:
                # Always pass through the immutable commit predicate. An existing
                # month is a no-op only when its semantic content is identical;
                # a conflicting envelope remains an integrity failure.
                if self.promote_run(run_id):
                    replayed.append(run_id)
            except Exception as exc:
                failures.append(f"{run_id}: {exc}")
        if failures:
            raise BauPromotionError(
                "BAU replay failures after processing all envelopes: "
                + "; ".join(failures)
            )
        return tuple(replayed)

    def reconcile_pins(self, profile_hash: str, snapshot_month: str) -> None:
        """Idempotently pin evidence references from the immutable winner only."""
        for security_id, revision in self._backtest.snapshot_member_revisions(
            profile_hash, snapshot_month
        ):
            self._prices.pin(
                "snapshot",
                f"{profile_hash}:{snapshot_month}:{security_id}",
                revision,
            )


__all__ = ["BauPromotionError", "BauSnapshotPromotionService"]
