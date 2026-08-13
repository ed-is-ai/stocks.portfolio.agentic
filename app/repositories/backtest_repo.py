"""Persistence for immutable Strategy Manager evidence and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
from typing import Callable, Protocol
from uuid import uuid4

from app.repositories.db import Connect, session
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    HistoricalScanRecordV1,
    HistoricalScanContractError,
)
from app.services.backtest.canonical_manifest import manifest_digest
from app.services.backtest.snapshot_profile import (
    ActiveSnapshotProfileV1,
    CoverageIntervalV1,
    CoverageSummaryV1,
    HistoricalEvidenceV1,
    IntervalReadinessV1,
    MonthlySnapshotCommitV1,
    ProvenanceCoverageV1,
    SnapshotMemberV1,
    SnapshotMonthManifestV1,
    SnapshotProfileV1,
    SnapshotContractError,
    build_before_first_provider_observation,
    verified_evidence_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.strategy_job import (
    ClaimedStrategyJobV1,
    InitializationEnqueueResultV1,
    InitializationRunV1,
    JobFailureCode,
    StrategyJobConflict,
    StrategyJobNotFound,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
    requested_month_digest,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_QUALIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_source_qualifications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_digest      TEXT NOT NULL,
    source_versions_json TEXT NOT NULL,
    fixture_digest       TEXT NOT NULL,
    probe_definition_digest TEXT NOT NULL,
    probe_digest         TEXT NOT NULL,
    qualified_at         TEXT NOT NULL,
    passed               INTEGER NOT NULL CHECK(passed IN (0, 1)),
    failure_code         TEXT,
    failure_reason       TEXT,
    CHECK(
        (passed = 1 AND failure_code IS NULL AND failure_reason IS NULL)
        OR
        (passed = 0 AND failure_code IS NOT NULL AND failure_reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_source_qualification_contract
ON historical_source_qualifications(contract_digest, id DESC);
CREATE TRIGGER IF NOT EXISTS qualification_append_only_update
BEFORE UPDATE ON historical_source_qualifications
BEGIN SELECT RAISE(ABORT, 'qualification evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS qualification_append_only_delete
BEFORE DELETE ON historical_source_qualifications
BEGIN SELECT RAISE(ABORT, 'qualification evidence is append-only'); END;
"""

_ROSTER_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS security_identity_registry_revisions (
    revision_digest TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_identities (
    security_id TEXT PRIMARY KEY,
    mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
    provider_symbol TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    identity_registry_revision TEXT NOT NULL REFERENCES security_identity_registry_revisions(revision_digest),
    created_at TEXT NOT NULL,
    UNIQUE(mic, provider_symbol)
);
CREATE TABLE IF NOT EXISTS security_alias_manifests (
    alias_revision TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_alias_entries (
    alias_revision TEXT NOT NULL REFERENCES security_alias_manifests(alias_revision),
    security_id TEXT NOT NULL REFERENCES security_identities(security_id),
    provider TEXT NOT NULL,
    mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
    observed_symbol TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    evidence_source TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('provider_evidence', 'manual_override')),
    PRIMARY KEY(alias_revision, provider, mic, observed_symbol, effective_from, effective_to, security_id),
    CHECK(effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to)
);
CREATE TABLE IF NOT EXISTS reconstruction_rosters (
    roster_digest TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    canonical_manifest_json TEXT NOT NULL,
    identity_registry_revision TEXT NOT NULL REFERENCES security_identity_registry_revisions(revision_digest),
    alias_revision TEXT NOT NULL REFERENCES security_alias_manifests(alias_revision),
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_sources (
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    source_name TEXT NOT NULL CHECK(source_name IN ('datahub_sp500', 'tradingview_us', 'tradingview_uk')),
    payload_digest TEXT NOT NULL,
    original_payload_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    PRIMARY KEY(roster_digest, source_name),
    UNIQUE(roster_digest, source_order)
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_members (
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    security_id TEXT NOT NULL REFERENCES security_identities(security_id),
    mic TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    currency TEXT NOT NULL,
    source_memberships_json TEXT NOT NULL,
    identity_evidence_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    PRIMARY KEY(roster_digest, security_id),
    UNIQUE(roster_digest, mic, provider_symbol)
);
CREATE TABLE IF NOT EXISTS reconstruction_roster_lineages (
    lineage_id TEXT PRIMARY KEY,
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    bound_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS alias_no_overlap
BEFORE INSERT ON security_alias_entries
WHEN EXISTS (
    SELECT 1 FROM security_alias_entries existing
    WHERE existing.provider = NEW.provider
      AND existing.alias_revision = NEW.alias_revision
      AND existing.mic = NEW.mic
      AND existing.observed_symbol = NEW.observed_symbol
      AND COALESCE(existing.effective_from, '0001-01-01') < COALESCE(NEW.effective_to, '9999-12-31')
      AND COALESCE(NEW.effective_from, '0001-01-01') < COALESCE(existing.effective_to, '9999-12-31')
)
BEGIN SELECT RAISE(ABORT, 'alias intervals overlap'); END;

CREATE TRIGGER IF NOT EXISTS identity_registry_immutable_update BEFORE UPDATE ON security_identity_registry_revisions BEGIN SELECT RAISE(ABORT, 'identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS identity_registry_immutable_delete BEFORE DELETE ON security_identity_registry_revisions BEGIN SELECT RAISE(ABORT, 'identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS security_identity_immutable_update BEFORE UPDATE ON security_identities BEGIN SELECT RAISE(ABORT, 'security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS security_identity_immutable_delete BEFORE DELETE ON security_identities BEGIN SELECT RAISE(ABORT, 'security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_manifest_immutable_update BEFORE UPDATE ON security_alias_manifests BEGIN SELECT RAISE(ABORT, 'alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_manifest_immutable_delete BEFORE DELETE ON security_alias_manifests BEGIN SELECT RAISE(ABORT, 'alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_entry_immutable_update BEFORE UPDATE ON security_alias_entries BEGIN SELECT RAISE(ABORT, 'alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS alias_entry_immutable_delete BEFORE DELETE ON security_alias_entries BEGIN SELECT RAISE(ABORT, 'alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_immutable_update BEFORE UPDATE ON reconstruction_rosters BEGIN SELECT RAISE(ABORT, 'reconstruction roster is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_immutable_delete BEFORE DELETE ON reconstruction_rosters BEGIN SELECT RAISE(ABORT, 'reconstruction roster is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_source_immutable_update BEFORE UPDATE ON reconstruction_roster_sources BEGIN SELECT RAISE(ABORT, 'roster source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_source_immutable_delete BEFORE DELETE ON reconstruction_roster_sources BEGIN SELECT RAISE(ABORT, 'roster source is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_member_immutable_update BEFORE UPDATE ON reconstruction_roster_members BEGIN SELECT RAISE(ABORT, 'roster member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_member_immutable_delete BEFORE DELETE ON reconstruction_roster_members BEGIN SELECT RAISE(ABORT, 'roster member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_lineage_immutable_update BEFORE UPDATE ON reconstruction_roster_lineages BEGIN SELECT RAISE(ABORT, 'roster lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS roster_lineage_immutable_delete BEFORE DELETE ON reconstruction_roster_lineages BEGIN SELECT RAISE(ABORT, 'roster lineage is immutable'); END;
"""

_SCAN_RECONSTRUCTION_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_reconstruction_cache (
    security_id       TEXT NOT NULL,
    date              TEXT NOT NULL,
    detector          TEXT NOT NULL CHECK(detector IN (
        'technical_indicators_v1', 'weinstein_stage_v1', 'vcp_v1'
    )),
    detector_version  TEXT NOT NULL CHECK(length(detector_version) = 64),
    input_revision    TEXT NOT NULL CHECK(length(input_revision) = 64),
    scan_result_json  TEXT NOT NULL,
    scan_result_digest TEXT NOT NULL CHECK(length(scan_result_digest) = 64),
    PRIMARY KEY (security_id, date, detector, detector_version, input_revision)
);
CREATE TRIGGER IF NOT EXISTS scan_reconstruction_cache_immutable_update
BEFORE UPDATE ON scan_reconstruction_cache
BEGIN SELECT RAISE(ABORT, 'scan reconstruction cache is immutable'); END;
CREATE TRIGGER IF NOT EXISTS scan_reconstruction_cache_immutable_delete
BEFORE DELETE ON scan_reconstruction_cache
BEGIN SELECT RAISE(ABORT, 'scan reconstruction cache is immutable'); END;
"""

_SNAPSHOT_COVERAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_profiles (
    profile_hash TEXT PRIMARY KEY CHECK(length(profile_hash) = 64),
    canonical_profile_json TEXT NOT NULL,
    display_version TEXT NOT NULL,
    roster_digest TEXT NOT NULL REFERENCES reconstruction_rosters(roster_digest),
    scanner_schema_version TEXT NOT NULL,
    calendar_dataset_version TEXT NOT NULL,
    calendar_dataset_digest TEXT NOT NULL CHECK(length(calendar_dataset_digest) = 64),
    cadence TEXT NOT NULL CHECK(cadence = 'per-exchange month_end')
);
CREATE TABLE IF NOT EXISTS active_snapshot_profile (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    profile_hash TEXT NOT NULL REFERENCES snapshot_profiles(profile_hash),
    activation_seq INTEGER NOT NULL CHECK(activation_seq > 0),
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_months (
    profile_hash TEXT NOT NULL REFERENCES snapshot_profiles(profile_hash),
    snapshot_month TEXT NOT NULL,
    canonical_manifest_json TEXT NOT NULL,
    provenance_quality TEXT NOT NULL CHECK(provenance_quality IN (
        'best_effort_reconstructed', 'observed_bau'
    )),
    processing_complete INTEGER NOT NULL CHECK(processing_complete = 1),
    market_complete TEXT NOT NULL CHECK(market_complete = 'unknown'),
    roster_digest TEXT NOT NULL,
    expected_digest TEXT NOT NULL CHECK(length(expected_digest) = 64),
    input_revision_digest TEXT NOT NULL CHECK(length(input_revision_digest) = 64),
    result_digest TEXT NOT NULL CHECK(length(result_digest) = 64),
    expected_count INTEGER NOT NULL CHECK(expected_count >= 0),
    valid_count INTEGER NOT NULL CHECK(valid_count >= 0),
    excluded_count INTEGER NOT NULL CHECK(excluded_count >= 0),
    content_digest TEXT NOT NULL CHECK(length(content_digest) = 64),
    source_run_id TEXT,
    observed_at TEXT,
    committed_at TEXT NOT NULL,
    PRIMARY KEY(profile_hash, snapshot_month),
    CHECK(expected_count = valid_count + excluded_count)
);
CREATE TABLE IF NOT EXISTS snapshot_members (
    profile_hash TEXT NOT NULL,
    snapshot_month TEXT NOT NULL,
    security_id TEXT NOT NULL,
    canonical_member_json TEXT NOT NULL,
    observed_symbol TEXT NOT NULL,
    mic TEXT NOT NULL CHECK(mic IN ('XNAS', 'XNYS', 'XLON')),
    as_of_session_date TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK(resolution IN ('valid_scan', 'legitimate_exclusion')),
    source_cutoff TEXT NOT NULL,
    source_payload_digest TEXT NOT NULL CHECK(length(source_payload_digest) = 64),
    input_revision TEXT NOT NULL CHECK(length(input_revision) = 64),
    provider_data_revision TEXT NOT NULL CHECK(length(provider_data_revision) = 64),
    provider_evidence_manifest_digest TEXT NOT NULL CHECK(length(provider_evidence_manifest_digest) = 64),
    alias_revision TEXT NOT NULL CHECK(length(alias_revision) = 64),
    record_digest TEXT CHECK(record_digest IS NULL OR length(record_digest) = 64),
    exclusion_reason TEXT CHECK(exclusion_reason IS NULL OR exclusion_reason = 'before_first_provider_observation'),
    exclusion_evidence_json TEXT,
    provenance_digest TEXT NOT NULL CHECK(length(provenance_digest) = 64),
    PRIMARY KEY(profile_hash, snapshot_month, security_id),
    FOREIGN KEY(profile_hash, snapshot_month)
        REFERENCES snapshot_months(profile_hash, snapshot_month)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (resolution = 'valid_scan' AND record_digest IS NOT NULL
         AND exclusion_reason IS NULL AND exclusion_evidence_json IS NULL)
        OR
        (resolution = 'legitimate_exclusion' AND record_digest IS NULL
         AND exclusion_reason = 'before_first_provider_observation'
         AND exclusion_evidence_json IS NOT NULL)
    )
);
CREATE TABLE IF NOT EXISTS monthly_scan_results (
    profile_hash TEXT NOT NULL,
    snapshot_month TEXT NOT NULL,
    security_id TEXT NOT NULL,
    historical_scan_record_json TEXT NOT NULL,
    record_digest TEXT NOT NULL CHECK(length(record_digest) = 64),
    PRIMARY KEY(profile_hash, snapshot_month, security_id),
    FOREIGN KEY(profile_hash, snapshot_month, security_id)
        REFERENCES snapshot_members(profile_hash, snapshot_month, security_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TRIGGER IF NOT EXISTS snapshot_profile_immutable_update BEFORE UPDATE ON snapshot_profiles BEGIN SELECT RAISE(ABORT, 'snapshot profile is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_profile_immutable_delete BEFORE DELETE ON snapshot_profiles BEGIN SELECT RAISE(ABORT, 'snapshot profile is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_month_immutable_update BEFORE UPDATE ON snapshot_months BEGIN SELECT RAISE(ABORT, 'snapshot month is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_month_immutable_delete BEFORE DELETE ON snapshot_months BEGIN SELECT RAISE(ABORT, 'snapshot month is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_member_immutable_update BEFORE UPDATE ON snapshot_members BEGIN SELECT RAISE(ABORT, 'snapshot member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_member_immutable_delete BEFORE DELETE ON snapshot_members BEGIN SELECT RAISE(ABORT, 'snapshot member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS monthly_scan_result_immutable_update BEFORE UPDATE ON monthly_scan_results BEGIN SELECT RAISE(ABORT, 'monthly scan result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS monthly_scan_result_immutable_delete BEFORE DELETE ON monthly_scan_results BEGIN SELECT RAISE(ABORT, 'monthly scan result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_member_requires_roster_identity
BEFORE INSERT ON snapshot_members
WHEN NOT EXISTS (
    SELECT 1
    FROM snapshot_profiles profile
    JOIN reconstruction_roster_members roster
      ON roster.roster_digest = profile.roster_digest
    JOIN reconstruction_rosters roster_manifest
      ON roster_manifest.roster_digest = profile.roster_digest
    JOIN security_alias_entries alias
      ON alias.alias_revision = roster_manifest.alias_revision
     AND alias.security_id = roster.security_id
     AND alias.provider = 'yfinance'
     AND alias.mic = roster.mic
     AND alias.observed_symbol = NEW.observed_symbol
     AND (alias.effective_from IS NULL OR alias.effective_from <= NEW.as_of_session_date)
     AND (alias.effective_to IS NULL OR NEW.as_of_session_date < alias.effective_to)
    WHERE profile.profile_hash = NEW.profile_hash
      AND roster.security_id = NEW.security_id
      AND roster.mic = NEW.mic
      AND NEW.alias_revision = roster_manifest.alias_revision
)
BEGIN SELECT RAISE(ABORT, 'snapshot member is outside profile roster'); END;
CREATE TRIGGER IF NOT EXISTS monthly_scan_result_requires_valid_member
BEFORE INSERT ON monthly_scan_results
WHEN NOT EXISTS (
    SELECT 1 FROM snapshot_members member
    WHERE member.profile_hash = NEW.profile_hash
      AND member.snapshot_month = NEW.snapshot_month
      AND member.security_id = NEW.security_id
      AND member.resolution = 'valid_scan'
      AND member.record_digest = NEW.record_digest
)
BEGIN SELECT RAISE(ABORT, 'monthly scan result requires matching valid member'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_month_requires_complete_write_set
BEFORE INSERT ON snapshot_months
WHEN
    (SELECT COUNT(*) FROM snapshot_members member
     WHERE member.profile_hash = NEW.profile_hash
       AND member.snapshot_month = NEW.snapshot_month) != NEW.expected_count
    OR
    (SELECT COUNT(*) FROM snapshot_members member
     WHERE member.profile_hash = NEW.profile_hash
       AND member.snapshot_month = NEW.snapshot_month
       AND member.resolution = 'valid_scan') != NEW.valid_count
    OR
    (SELECT COUNT(*) FROM snapshot_members member
     WHERE member.profile_hash = NEW.profile_hash
       AND member.snapshot_month = NEW.snapshot_month
       AND member.resolution = 'legitimate_exclusion') != NEW.excluded_count
    OR
    (SELECT COUNT(*) FROM monthly_scan_results result
     WHERE result.profile_hash = NEW.profile_hash
       AND result.snapshot_month = NEW.snapshot_month) != NEW.valid_count
BEGIN SELECT RAISE(ABORT, 'snapshot month write set is incomplete'); END;
CREATE TRIGGER IF NOT EXISTS active_snapshot_profile_monotonic_update
BEFORE UPDATE ON active_snapshot_profile
WHEN NEW.activation_seq != OLD.activation_seq + 1
  OR NEW.profile_hash = OLD.profile_hash
BEGIN SELECT RAISE(ABORT, 'active snapshot profile transition is not monotonic'); END;
CREATE TRIGGER IF NOT EXISTS active_snapshot_profile_initial_sequence
BEFORE INSERT ON active_snapshot_profile
WHEN NEW.activation_seq != 1
BEGIN SELECT RAISE(ABORT, 'active snapshot profile must start at sequence 1'); END;
CREATE TRIGGER IF NOT EXISTS active_snapshot_profile_immutable_delete
BEFORE DELETE ON active_snapshot_profile
BEGIN SELECT RAISE(ABORT, 'active snapshot profile cannot be deleted'); END;
"""

_STRATEGY_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL CHECK(job_type IN ('initialization', 'backtest')),
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'complete', 'failed', 'cancelled'
    )),
    parent_job_id TEXT REFERENCES strategy_jobs(id),
    enqueue_seq INTEGER NOT NULL UNIQUE CHECK(enqueue_seq > 0),
    claim_token TEXT,
    current_month TEXT,
    status_version INTEGER NOT NULL CHECK(status_version > 0),
    cancel_requested_at TEXT,
    failure_code TEXT CHECK(failure_code IS NULL OR failure_code IN (
        'provider_unavailable', 'provider_throttled', 'provider_contract_error',
        'required_data_missing', 'identity_ambiguous', 'calendar_error',
        'integrity_error', 'worker_interrupted'
    )),
    failed_month TEXT,
    failure_detail TEXT,
    deleted_at TEXT,
    audit_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(status != 'queued' OR (claim_token IS NULL AND current_month IS NULL)),
    CHECK(status != 'running' OR claim_token IS NOT NULL),
    CHECK(status NOT IN ('complete', 'failed', 'cancelled') OR current_month IS NULL),
    CHECK(
        (status = 'failed' AND failure_code IS NOT NULL AND failure_detail IS NOT NULL)
        OR
        (status != 'failed' AND failure_code IS NULL AND failed_month IS NULL
         AND failure_detail IS NULL)
    ),
    CHECK(status != 'cancelled' OR cancel_requested_at IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_strategy_job
ON strategy_jobs(status) WHERE status = 'running' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS strategy_job_fifo
ON strategy_jobs(status, enqueue_seq);

CREATE TABLE IF NOT EXISTS initialization_runs (
    job_id TEXT PRIMARY KEY REFERENCES strategy_jobs(id),
    profile_hash TEXT NOT NULL REFERENCES snapshot_profiles(profile_hash),
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    requested_months_json TEXT NOT NULL,
    requested_month_digest TEXT NOT NULL CHECK(length(requested_month_digest) = 64),
    calendar_dataset_version TEXT NOT NULL,
    qualification_contract_digest TEXT NOT NULL CHECK(
        length(qualification_contract_digest) = 64
    ),
    ordered_month_digest TEXT CHECK(
        ordered_month_digest IS NULL OR length(ordered_month_digest) = 64
    ),
    CHECK(requested_start <= requested_end)
);

CREATE TRIGGER IF NOT EXISTS strategy_job_identity_immutable
BEFORE UPDATE ON strategy_jobs
WHEN NEW.id != OLD.id
  OR NEW.job_type != OLD.job_type
  OR NEW.parent_job_id IS NOT OLD.parent_job_id
  OR NEW.enqueue_seq != OLD.enqueue_seq
  OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'strategy job identity is immutable'); END;

DROP TRIGGER IF EXISTS strategy_job_terminal_immutable;
CREATE TRIGGER strategy_job_terminal_immutable
BEFORE UPDATE ON strategy_jobs
WHEN OLD.status IN ('complete', 'failed', 'cancelled')
 AND (
    NEW.status != OLD.status
    OR NEW.claim_token IS NOT OLD.claim_token
    OR NEW.current_month IS NOT OLD.current_month
    OR NEW.cancel_requested_at IS NOT OLD.cancel_requested_at
    OR NEW.failure_code IS NOT OLD.failure_code
    OR NEW.failed_month IS NOT OLD.failed_month
    OR NEW.failure_detail IS NOT OLD.failure_detail
 )
BEGIN SELECT RAISE(ABORT, 'terminal strategy job is immutable'); END;

CREATE TRIGGER IF NOT EXISTS strategy_job_legal_transition
BEFORE UPDATE ON strategy_jobs
WHEN NEW.status != OLD.status
 AND NOT (
    (OLD.status = 'queued' AND NEW.status IN ('running', 'cancelled', 'failed'))
    OR
    (OLD.status = 'running' AND NEW.status IN ('complete', 'failed', 'cancelled'))
 )
BEGIN SELECT RAISE(ABORT, 'illegal strategy job transition'); END;

CREATE TRIGGER IF NOT EXISTS strategy_job_version_monotonic
BEFORE UPDATE ON strategy_jobs
WHEN NEW.status_version != OLD.status_version + 1
BEGIN SELECT RAISE(ABORT, 'strategy job version is not monotonic'); END;

CREATE TRIGGER IF NOT EXISTS strategy_job_version_requires_mutation
BEFORE UPDATE ON strategy_jobs
WHEN NEW.status_version != OLD.status_version
 AND NEW.status IS OLD.status
 AND NEW.claim_token IS OLD.claim_token
 AND NEW.current_month IS OLD.current_month
 AND NEW.cancel_requested_at IS OLD.cancel_requested_at
 AND NEW.failure_code IS OLD.failure_code
 AND NEW.failed_month IS OLD.failed_month
 AND NEW.failure_detail IS OLD.failure_detail
 AND NEW.deleted_at IS OLD.deleted_at
 AND NEW.audit_summary IS OLD.audit_summary
BEGIN SELECT RAISE(ABORT, 'strategy job version requires a lifecycle mutation'); END;

CREATE TRIGGER IF NOT EXISTS initialization_job_requires_subtype_before_running
BEFORE UPDATE OF status ON strategy_jobs
WHEN NEW.job_type = 'initialization'
 AND NEW.status = 'running'
 AND NOT EXISTS (
    SELECT 1 FROM initialization_runs run WHERE run.job_id = NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'initialization subtype is missing'); END;

CREATE TRIGGER IF NOT EXISTS initialization_job_requires_digest_before_complete
BEFORE UPDATE OF status ON strategy_jobs
WHEN NEW.job_type = 'initialization'
 AND NEW.status = 'complete'
 AND NOT EXISTS (
    SELECT 1 FROM initialization_runs run
    WHERE run.job_id = NEW.id AND run.ordered_month_digest IS NOT NULL
 )
BEGIN SELECT RAISE(ABORT, 'initialization completion digest is missing'); END;

CREATE TRIGGER IF NOT EXISTS initialization_subtype_matches_job
BEFORE INSERT ON initialization_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = NEW.job_id AND job.job_type = 'initialization'
 )
BEGIN SELECT RAISE(ABORT, 'initialization subtype does not match job'); END;

CREATE TRIGGER IF NOT EXISTS initialization_run_immutable
BEFORE UPDATE ON initialization_runs
WHEN NEW.job_id != OLD.job_id
  OR NEW.profile_hash != OLD.profile_hash
  OR NEW.requested_start != OLD.requested_start
  OR NEW.requested_end != OLD.requested_end
  OR NEW.requested_months_json != OLD.requested_months_json
  OR NEW.requested_month_digest != OLD.requested_month_digest
  OR NEW.calendar_dataset_version != OLD.calendar_dataset_version
  OR NEW.qualification_contract_digest != OLD.qualification_contract_digest
  OR (OLD.ordered_month_digest IS NOT NULL AND NEW.ordered_month_digest IS NOT OLD.ordered_month_digest)
  OR (OLD.ordered_month_digest IS NULL AND NEW.ordered_month_digest IS NULL)
BEGIN SELECT RAISE(ABORT, 'initialization configuration is immutable'); END;

CREATE TRIGGER IF NOT EXISTS initialization_digest_requires_running_job
BEFORE UPDATE OF ordered_month_digest ON initialization_runs
WHEN OLD.ordered_month_digest IS NULL
 AND NEW.ordered_month_digest IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = NEW.job_id AND job.status = 'running'
 )
BEGIN SELECT RAISE(ABORT, 'initialization digest requires running job'); END;

DROP TRIGGER IF EXISTS initialization_run_immutable_delete;
CREATE TRIGGER initialization_run_immutable_delete
BEFORE DELETE ON initialization_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = OLD.job_id AND job.deleted_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'initialization run is immutable'); END;

CREATE TABLE IF NOT EXISTS notification_outbox (
    job_id TEXT PRIMARY KEY REFERENCES strategy_jobs(id),
    job_status_version INTEGER NOT NULL CHECK(job_status_version > 0),
    payload_json TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 1 CHECK(pending IN (0, 1)),
    projected_status_version INTEGER CHECK(
        projected_status_version IS NULL OR projected_status_version >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_job_restart_actions (
    source_job_id TEXT NOT NULL REFERENCES strategy_jobs(id),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) > 0),
    child_job_id TEXT NOT NULL UNIQUE REFERENCES strategy_jobs(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_job_id, idempotency_key)
);
"""


@dataclass(frozen=True)
class QualificationResult:
    contract_digest: str
    source_versions_json: str
    fixture_digest: str
    probe_definition_digest: str
    probe_digest: str
    qualified_at: str
    passed: bool
    failure_code: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class RosterCaptureCommit:
    """Complete atomic write-set for one immutable roster-lineage binding."""

    lineage_id: str
    roster_digest: str
    roster_manifest_json: str
    policy_version: str
    identity_registry_revision: str
    identity_registry_json: str
    identity_evidence_digest: str
    alias_revision: str
    alias_manifest_json: str
    alias_evidence_digest: str
    captured_at: str
    identities: tuple[tuple[str, str, str, str], ...]
    aliases: tuple[
        tuple[
            str,
            str,
            str,
            str,
            str | None,
            str | None,
            str,
            str,
            str,
        ],
        ...,
    ]
    sources: tuple[tuple[str, str, str, str], ...]
    members: tuple[tuple[str, str, str, str, str, str, str], ...]


class BacktestIntegrityError(RuntimeError):
    def __init__(self, message: str, *, code: str = "integrity_error") -> None:
        self.code = code
        super().__init__(message)


SnapshotEvidenceV1 = HistoricalEvidenceV1


class HistoricalEvidenceVerifier(Protocol):
    def verify(self, data_revision: str) -> HistoricalEvidenceV1: ...


@dataclass(frozen=True)
class DetectorCacheKey:
    security_id: str
    date: date
    detector: str
    detector_version: str
    input_revision: str

    def sql_values(self) -> tuple[str, str, str, str, str]:
        return (
            self.security_id,
            self.date.isoformat(),
            self.detector,
            self.detector_version,
            self.input_revision,
        )


def _row_to_result(row: tuple[object, ...]) -> QualificationResult:
    return QualificationResult(
        contract_digest=str(row[0]),
        source_versions_json=str(row[1]),
        fixture_digest=str(row[2]),
        probe_definition_digest=str(row[3]),
        probe_digest=str(row[4]),
        qualified_at=str(row[5]),
        passed=bool(row[6]),
        failure_code=None if row[7] is None else str(row[7]),
        failure_reason=None if row[8] is None else str(row[8]),
    )


_JOB_COLUMNS = (
    "id",
    "job_type",
    "status",
    "parent_job_id",
    "enqueue_seq",
    "claim_token",
    "current_month",
    "status_version",
    "cancel_requested_at",
    "failure_code",
    "failed_month",
    "failure_detail",
    "deleted_at",
    "audit_summary",
    "created_at",
    "updated_at",
)


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _row_to_strategy_job(row: sqlite3.Row | tuple[object, ...]) -> StrategyJobV1:
    return StrategyJobV1(
        id=str(row[0]),
        job_type=StrategyJobType(str(row[1])),
        status=StrategyJobStatus(str(row[2])),
        parent_job_id=None if row[3] is None else str(row[3]),
        enqueue_seq=int(str(row[4])),
        claim_token=None if row[5] is None else str(row[5]),
        current_month=None if row[6] is None else str(row[6]),
        status_version=int(str(row[7])),
        cancel_requested_at=_optional_instant(row[8]),
        failure_code=(None if row[9] is None else JobFailureCode(str(row[9]))),
        failed_month=None if row[10] is None else str(row[10]),
        failure_detail=None if row[11] is None else str(row[11]),
        deleted_at=_optional_instant(row[12]),
        audit_summary=None if row[13] is None else str(row[13]),
        created_at=datetime.fromisoformat(str(row[14])),
        updated_at=datetime.fromisoformat(str(row[15])),
    )


def _row_to_initialization(
    row: sqlite3.Row | tuple[object, ...],
) -> InitializationRunV1:
    months = json.loads(str(row[4]))
    if not isinstance(months, list) or not all(
        isinstance(item, str) for item in months
    ):
        raise BacktestIntegrityError("stored initialization month sequence is invalid")
    return InitializationRunV1(
        job_id=str(row[0]),
        profile_hash=str(row[1]),
        requested_start=str(row[2]),
        requested_end=str(row[3]),
        requested_months=tuple(months),
        requested_month_digest=str(row[5]),
        calendar_dataset_version=str(row[6]),
        qualification_contract_digest=str(row[7]),
        ordered_month_digest=None if row[8] is None else str(row[8]),
    )


class BacktestRepository:
    """Repository seed that later stories extend with jobs and results."""

    def __init__(
        self,
        connect: Connect,
        *,
        clock: Callable[[], date] = lambda: datetime.now(timezone.utc).date(),
        instant_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_generator: Callable[[], str] = lambda: str(uuid4()),
        token_generator: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._instant_clock = instant_clock
        self._id_generator = id_generator
        self._token_generator = token_generator

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            conn.executescript(
                _QUALIFICATION_SCHEMA
                + _ROSTER_SCHEMA
                + _SCAN_RECONSTRUCTION_CACHE_SCHEMA
                + _SNAPSHOT_COVERAGE_SCHEMA
                + _STRATEGY_JOB_SCHEMA
            )
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(historical_source_qualifications)"
                ).fetchall()
            }
            if "probe_definition_digest" not in columns:
                try:
                    conn.execute(
                        "ALTER TABLE historical_source_qualifications "
                        "ADD COLUMN probe_definition_digest TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    raise

    def record_qualification(self, result: QualificationResult) -> int:
        with session(self._connect) as conn:
            cursor = conn.execute(
                """INSERT INTO historical_source_qualifications (
                    contract_digest, source_versions_json, fixture_digest,
                    probe_definition_digest, probe_digest, qualified_at, passed,
                    failure_code, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.contract_digest,
                    result.source_versions_json,
                    result.fixture_digest,
                    result.probe_definition_digest,
                    result.probe_digest,
                    result.qualified_at,
                    int(result.passed),
                    result.failure_code,
                    result.failure_reason,
                ),
            )
            return int(cursor.lastrowid or 0)

    def qualification_history(self, contract_digest: str) -> list[QualificationResult]:
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT contract_digest, source_versions_json, fixture_digest,
                          probe_definition_digest, probe_digest, qualified_at, passed, failure_code,
                          failure_reason
                   FROM historical_source_qualifications
                   WHERE contract_digest = ? ORDER BY id ASC""",
                (contract_digest,),
            ).fetchall()
        return [_row_to_result(row) for row in rows]

    def latest_qualification(self, contract_digest: str) -> QualificationResult | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT contract_digest, source_versions_json, fixture_digest,
                          probe_definition_digest, probe_digest, qualified_at, passed, failure_code,
                          failure_reason
                   FROM historical_source_qualifications
                   WHERE contract_digest = ?
                   ORDER BY id DESC LIMIT 1""",
                (contract_digest,),
            ).fetchone()
        return None if row is None else _row_to_result(row)

    def latest_recorded_qualification(self) -> QualificationResult | None:
        """Return the latest immutable qualification result for worker revalidation."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT contract_digest, source_versions_json, fixture_digest,
                          probe_definition_digest, probe_digest, qualified_at, passed,
                          failure_code, failure_reason
                   FROM historical_source_qualifications
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        return None if row is None else _row_to_result(row)

    @staticmethod
    def _qualification_is_current(result: QualificationResult) -> bool:
        from app.services.backtest.historical_data_qualification import (
            FIXTURE_CONTRACT_VERSION,
            REQUEST_CONTRACT_VERSION,
            current_source_versions_json,
        )

        source_versions_json = current_source_versions_json()
        if not result.passed or result.source_versions_json != source_versions_json:
            return False
        try:
            sources = json.loads(source_versions_json)
        except json.JSONDecodeError:
            return False
        expected = manifest_digest(
            {
                "sources": sources,
                "calendar_digest": TradingCalendar().session_table_digest(),
                "request_contract": REQUEST_CONTRACT_VERSION,
                "fixture_contract": FIXTURE_CONTRACT_VERSION,
                "fixture_digest": result.fixture_digest,
                "probe_definition_digest": result.probe_definition_digest,
            }
        )
        return result.contract_digest == expected

    def current_qualification_contract_digest(self) -> str | None:
        result = self.latest_recorded_qualification()
        if result is None or not self._qualification_is_current(result):
            return None
        return result.contract_digest

    @classmethod
    def _require_qualification_on_connection(
        cls, conn: sqlite3.Connection, expected_digest: str
    ) -> None:
        row = conn.execute(
            """SELECT contract_digest, source_versions_json, fixture_digest,
                      probe_definition_digest, probe_digest, qualified_at, passed,
                      failure_code, failure_reason
               FROM historical_source_qualifications ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            raise StrategyJobConflict("historical data contract is not qualified")
        result = _row_to_result(row)
        if (
            result.contract_digest != expected_digest
            or not cls._qualification_is_current(result)
        ):
            raise StrategyJobConflict("historical data contract is not qualified")

    def roster_digest_for_lineage(self, lineage_id: str) -> str | None:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT roster_digest FROM reconstruction_roster_lineages WHERE lineage_id=?",
                (lineage_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def roster_manifest_json(self, roster_digest: str) -> str | None:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT canonical_manifest_json FROM reconstruction_rosters WHERE roster_digest=?",
                (roster_digest,),
            ).fetchone()
        return None if row is None else str(row[0])

    def identity_rows(self) -> list[tuple[str, str, str, str]]:
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT security_id, mic, provider_symbol, evidence_digest
                   FROM security_identities ORDER BY security_id"""
            ).fetchall()
        return [tuple(str(value) for value in row) for row in rows]  # type: ignore[return-value]

    def effective_alias_bounds(
        self,
        *,
        alias_revision: str,
        security_id: str,
        mic: str,
        observed_symbol: str,
        session_date: date,
    ) -> tuple[date | None, date | None]:
        """Return the one effective immutable yfinance alias interval."""
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT effective_from, effective_to
                   FROM security_alias_entries
                   WHERE alias_revision=? AND security_id=? AND provider='yfinance'
                     AND mic=? AND observed_symbol=?
                     AND (effective_from IS NULL OR effective_from<=?)
                     AND (effective_to IS NULL OR ?<effective_to)""",
                (
                    alias_revision,
                    security_id,
                    mic,
                    observed_symbol,
                    session_date.isoformat(),
                    session_date.isoformat(),
                ),
            ).fetchall()
        if len(rows) != 1:
            code = "identity_ambiguous" if len(rows) > 1 else "required_data_missing"
            raise BacktestIntegrityError(
                "effective alias evidence is unavailable", code=code
            )
        return (
            None if rows[0][0] is None else date.fromisoformat(str(rows[0][0])),
            None if rows[0][1] is None else date.fromisoformat(str(rows[0][1])),
        )

    def create_initialization_job(
        self,
        *,
        profile_hash: str,
        requested_start: str,
        requested_end: str,
        calendar_dataset_version: str,
        qualification_contract_digest: str,
        parent_job_id: str | None = None,
    ) -> InitializationEnqueueResultV1:
        """Atomically enqueue one initialization, or return a verified no-op."""
        months = TradingCalendar.months_inclusive(requested_start, requested_end)
        for month in months:
            TradingCalendar.closed_month(month, as_of=self._clock())
        now = self._job_now()
        rendered_months = json.dumps(list(months), separators=(",", ":"))
        month_digest = requested_month_digest(
            profile_hash, months, calendar_dataset_version
        )
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._require_qualification_on_connection(
                    conn, qualification_contract_digest
                )
                profile = conn.execute(
                    """SELECT calendar_dataset_version
                       FROM snapshot_profiles WHERE profile_hash=?""",
                    (profile_hash,),
                ).fetchone()
                if profile is None:
                    raise StrategyJobConflict("snapshot profile does not exist")
                if str(profile[0]) != calendar_dataset_version:
                    raise StrategyJobConflict(
                        "snapshot profile calendar version is incompatible"
                    )
                active = conn.execute(
                    "SELECT profile_hash FROM active_snapshot_profile "
                    "WHERE singleton_id=1"
                ).fetchone()
                if active is None or str(active[0]) != profile_hash:
                    raise StrategyJobConflict("snapshot profile is not active")
                if self._interval_is_ready_for_job(
                    conn, profile_hash, requested_start, requested_end
                ):
                    return InitializationEnqueueResultV1(no_op=True)
                if (
                    parent_job_id is not None
                    and conn.execute(
                        "SELECT 1 FROM strategy_jobs WHERE id=?", (parent_job_id,)
                    ).fetchone()
                    is None
                ):
                    raise StrategyJobConflict("parent strategy job does not exist")
                sequence_row = conn.execute(
                    "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM strategy_jobs"
                ).fetchone()
                enqueue_seq = int(sequence_row[0]) if sequence_row else 1
                job_id = self._id_generator()
                conn.execute(
                    """INSERT INTO strategy_jobs (
                           id, job_type, status, parent_job_id, enqueue_seq,
                           claim_token, current_month, status_version,
                           cancel_requested_at, failure_code, failed_month,
                           failure_detail, deleted_at, audit_summary,
                           created_at, updated_at
                       ) VALUES (?, 'initialization', 'queued', ?, ?, NULL, NULL, 1,
                                 NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                    (job_id, parent_job_id, enqueue_seq, now, now),
                )
                conn.execute(
                    """INSERT INTO initialization_runs (
                           job_id, profile_hash, requested_start, requested_end,
                           requested_months_json, requested_month_digest,
                           calendar_dataset_version, qualification_contract_digest,
                           ordered_month_digest
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        job_id,
                        profile_hash,
                        requested_start,
                        requested_end,
                        rendered_months,
                        month_digest,
                        calendar_dataset_version,
                        qualification_contract_digest,
                    ),
                )
                job = self._load_strategy_job(conn, job_id)
                initialization = self._load_initialization(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
            return InitializationEnqueueResultV1(
                no_op=False, job=job, initialization=initialization
            )
        except (StrategyJobConflict, StrategyJobNotFound):
            raise
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict("initialization job creation conflicted") from exc

    def strategy_job(self, job_id: str) -> StrategyJobV1:
        with session(self._connect) as conn:
            return self._load_strategy_job(conn, job_id)

    def initialization_run(self, job_id: str) -> InitializationRunV1:
        with session(self._connect) as conn:
            return self._load_initialization(conn, job_id)

    def list_strategy_jobs(self) -> tuple[StrategyJobV1, ...]:
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM strategy_jobs "
                "ORDER BY enqueue_seq"
            ).fetchall()
        return tuple(_row_to_strategy_job(row) for row in rows)

    def claim_next_strategy_job(self) -> ClaimedStrategyJobV1 | None:
        """Claim the smallest queued sequence while enforcing one running job."""
        token = self._token_generator()
        now = self._job_now()
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if (
                    conn.execute(
                        "SELECT 1 FROM strategy_jobs WHERE status='running' "
                        "AND deleted_at IS NULL LIMIT 1"
                    ).fetchone()
                    is not None
                ):
                    return None
                row = conn.execute(
                    """SELECT id, status_version FROM strategy_jobs
                       WHERE status='queued' AND deleted_at IS NULL
                       ORDER BY enqueue_seq LIMIT 1"""
                ).fetchone()
                if row is None:
                    return None
                cursor = conn.execute(
                    """UPDATE strategy_jobs
                       SET status='running', claim_token=?, current_month=NULL,
                           status_version=status_version+1, updated_at=?
                       WHERE id=? AND status='queued' AND status_version=?""",
                    (token, now, str(row[0]), int(row[1])),
                )
                if cursor.rowcount != 1:
                    raise StrategyJobConflict("queued job changed before claim")
                job = self._load_strategy_job(conn, str(row[0]))
                initialization = (
                    self._load_initialization(conn, job.id)
                    if job.job_type is StrategyJobType.INITIALIZATION
                    else None
                )
                self._upsert_notification_outbox_on_connection(conn, job)
                return ClaimedStrategyJobV1(
                    job=job, initialization=initialization, claim_token=token
                )
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict("strategy job claim conflicted") from exc

    def set_strategy_job_current_month(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        month: str,
    ) -> StrategyJobV1:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            initialization = self._load_initialization(conn, job_id)
            if month not in initialization.requested_months:
                raise StrategyJobConflict("progress month is outside requested range")
            cursor = conn.execute(
                """UPDATE strategy_jobs
                   SET current_month=?, status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL""",
                (
                    month,
                    self._job_now(),
                    job_id,
                    claim_token,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker progress ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def request_strategy_job_cancellation(
        self, job_id: str, *, expected_version: int
    ) -> StrategyJobV1:
        now = self._job_now()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, job_id)
            if job.status_version != expected_version:
                raise StrategyJobConflict("cancellation request is stale")
            if job.status.terminal or job.cancel_requested_at is not None:
                return job
            if job.status is StrategyJobStatus.QUEUED:
                cursor = conn.execute(
                    """UPDATE strategy_jobs
                       SET status='cancelled', cancel_requested_at=?, current_month=NULL,
                           status_version=status_version+1, updated_at=?
                       WHERE id=? AND status='queued' AND status_version=?""",
                    (now, now, job_id, expected_version),
                )
            else:
                cursor = conn.execute(
                    """UPDATE strategy_jobs
                       SET cancel_requested_at=?, status_version=status_version+1,
                           updated_at=?
                       WHERE id=? AND status='running' AND status_version=?
                         AND cancel_requested_at IS NULL""",
                    (now, now, job_id, expected_version),
                )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("cancellation request conflicted")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def cancel_claimed_strategy_job(
        self, job_id: str, claim_token: str, *, expected_version: int
    ) -> StrategyJobV1:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE strategy_jobs
                   SET status='cancelled', claim_token=NULL, current_month=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NOT NULL""",
                (self._job_now(), job_id, claim_token, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker cancellation ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def fail_claimed_strategy_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        failure_code: JobFailureCode,
        failed_month: str | None,
        detail: str,
    ) -> StrategyJobV1:
        safe_detail = detail.strip()
        if not safe_detail or len(safe_detail) > 500:
            raise ValueError("failure detail must contain 1-500 characters")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if failed_month is not None:
                initialization = self._load_initialization(conn, job_id)
                if failed_month not in initialization.requested_months:
                    raise StrategyJobConflict("failed month is outside requested range")
            cursor = conn.execute(
                """UPDATE strategy_jobs
                   SET status='failed', claim_token=NULL, current_month=NULL,
                       failure_code=?, failed_month=?, failure_detail=?,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL""",
                (
                    failure_code.value,
                    failed_month,
                    safe_detail,
                    self._job_now(),
                    job_id,
                    claim_token,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker failure ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def complete_claimed_initialization_job(
        self, job_id: str, claim_token: str, *, expected_version: int
    ) -> StrategyJobV1:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, job_id)
            if (
                job.status is not StrategyJobStatus.RUNNING
                or job.claim_token != claim_token
                or job.status_version != expected_version
                or job.cancel_requested_at is not None
            ):
                raise StrategyJobConflict("worker completion ownership is stale")
            initialization = self._load_initialization(conn, job_id)
            readiness = self._interval_readiness_on_connection(
                conn,
                initialization.profile_hash,
                initialization.requested_start,
                initialization.requested_end,
            )
            if not readiness.ready or readiness.ordered_month_digest is None:
                raise StrategyJobConflict("initialization interval is not Ready")
            conn.execute(
                """UPDATE initialization_runs SET ordered_month_digest=?
                   WHERE job_id=? AND ordered_month_digest IS NULL""",
                (readiness.ordered_month_digest, job_id),
            )
            cursor = conn.execute(
                """UPDATE strategy_jobs
                   SET status='complete', claim_token=NULL, current_month=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL""",
                (self._job_now(), job_id, claim_token, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker completion ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def reconcile_interrupted_strategy_jobs(self) -> tuple[StrategyJobV1, ...]:
        """Fail running claims left behind by a previous application process."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, status_version FROM strategy_jobs "
                "WHERE status='running' ORDER BY enqueue_seq"
            ).fetchall()
            reconciled: list[StrategyJobV1] = []
            for row in rows:
                cursor = conn.execute(
                    """UPDATE strategy_jobs
                       SET status='failed', claim_token=NULL, current_month=NULL,
                           failure_code='worker_interrupted', failed_month=NULL,
                           failure_detail='Worker interrupted before completion',
                           status_version=status_version+1, updated_at=?
                       WHERE id=? AND status='running' AND status_version=?""",
                    (self._job_now(), str(row[0]), int(row[1])),
                )
                if cursor.rowcount == 1:
                    job = self._load_strategy_job(conn, str(row[0]))
                    self._upsert_notification_outbox_on_connection(conn, job)
                    reconciled.append(job)
            return tuple(reconciled)

    def legal_strategy_job_actions(self, job_id: str) -> tuple[str, ...]:
        with session(self._connect) as conn:
            job = self._load_strategy_job(conn, job_id)
            if job.deleted_at is not None:
                return ()
            if job.status in {StrategyJobStatus.QUEUED, StrategyJobStatus.RUNNING}:
                return ("cancel",)
            if job.status in {StrategyJobStatus.FAILED, StrategyJobStatus.CANCELLED}:
                return ("restart", "delete")
            return ()

    def can_delete_strategy_job(self, job_id: str) -> bool:
        return "delete" in self.legal_strategy_job_actions(job_id)

    def restart_initialization_job(
        self,
        source_job_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> InitializationEnqueueResultV1:
        """Create one replay-from-beginning child for an eligible terminal source."""
        if not idempotency_key.strip():
            raise ValueError("restart idempotency key must not be blank")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._load_strategy_job(conn, source_job_id)
            existing = conn.execute(
                """SELECT child_job_id FROM strategy_job_restart_actions
                   WHERE source_job_id=? AND idempotency_key=?""",
                (source_job_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                child_id = str(existing[0])
                return InitializationEnqueueResultV1(
                    no_op=False,
                    job=self._load_strategy_job(conn, child_id),
                    initialization=self._load_initialization(conn, child_id),
                )
            if source.status_version != expected_version:
                raise StrategyJobConflict("restart request is stale")
            if source.status not in {
                StrategyJobStatus.FAILED,
                StrategyJobStatus.CANCELLED,
            }:
                raise StrategyJobConflict("strategy job cannot be restarted")
            if source.deleted_at is not None:
                raise StrategyJobConflict("deleted strategy job cannot be restarted")
            prior_child = conn.execute(
                "SELECT 1 FROM strategy_job_restart_actions WHERE source_job_id=?",
                (source_job_id,),
            ).fetchone()
            if prior_child is not None:
                raise StrategyJobConflict("strategy job already has a restart child")
            initialization = self._load_initialization(conn, source_job_id)
            now = self._job_now()
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM strategy_jobs"
                ).fetchone()[0]
            )
            child_id = self._id_generator()
            conn.execute(
                """INSERT INTO strategy_jobs (
                     id, job_type, status, parent_job_id, enqueue_seq,
                     claim_token, current_month, status_version, cancel_requested_at,
                     failure_code, failed_month, failure_detail, deleted_at,
                     audit_summary, created_at, updated_at
                   ) VALUES (?, 'initialization', 'queued', ?, ?, NULL, NULL, 1,
                              NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                (child_id, source_job_id, sequence, now, now),
            )
            conn.execute(
                """INSERT INTO initialization_runs (
                     job_id, profile_hash, requested_start, requested_end,
                     requested_months_json, requested_month_digest,
                     calendar_dataset_version, qualification_contract_digest,
                     ordered_month_digest
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    child_id,
                    initialization.profile_hash,
                    initialization.requested_start,
                    initialization.requested_end,
                    json.dumps(
                        list(initialization.requested_months), separators=(",", ":")
                    ),
                    initialization.requested_month_digest,
                    initialization.calendar_dataset_version,
                    initialization.qualification_contract_digest,
                ),
            )
            conn.execute(
                """INSERT INTO strategy_job_restart_actions
                   (source_job_id, idempotency_key, child_job_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (source_job_id, idempotency_key, child_id, now),
            )
            child = self._load_strategy_job(conn, child_id)
            self._upsert_notification_outbox_on_connection(conn, child)
            return InitializationEnqueueResultV1(
                no_op=False,
                job=child,
                initialization=self._load_initialization(conn, child_id),
            )

    def delete_strategy_job(
        self, job_id: str, *, expected_version: int
    ) -> StrategyJobV1:
        """Tombstone a failed/cancelled attempt while retaining lineage and audit."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, job_id)
            if job.deleted_at is not None:
                return job
            if job.status_version != expected_version:
                raise StrategyJobConflict("delete request is stale")
            if job.status not in {
                StrategyJobStatus.FAILED,
                StrategyJobStatus.CANCELLED,
            }:
                raise StrategyJobConflict("strategy job cannot be deleted")
            initialization = self._load_initialization(conn, job_id)
            summary = (
                f"{job.job_type.value} {job.status.value}: "
                f"{initialization.requested_start} to {initialization.requested_end}"
            )
            now = self._job_now()
            cursor = conn.execute(
                """UPDATE strategy_jobs
                   SET deleted_at=?, audit_summary=?, status_version=status_version+1,
                       updated_at=?
                   WHERE id=? AND status_version=? AND deleted_at IS NULL""",
                (now, summary, now, job_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("delete request conflicted")
            tombstone = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, tombstone)
            conn.execute("DELETE FROM initialization_runs WHERE job_id=?", (job_id,))
            return tombstone

    def _job_now(self) -> str:
        value = self._instant_clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job clock must return a timezone-aware instant")
        return value.astimezone(timezone.utc).isoformat()

    def _upsert_notification_outbox_on_connection(
        self, conn: sqlite3.Connection, job: StrategyJobV1
    ) -> None:
        """Persist the authoritative lifecycle projection in the same transaction."""
        initialization = None
        if job.job_type is StrategyJobType.INITIALIZATION:
            try:
                initialization = self._load_initialization(conn, job.id).model_dump(
                    mode="json"
                )
            except StrategyJobNotFound:
                initialization = None
        payload = {
            "schema_version": "strategy_job_notification.v1",
            "job": job.model_dump(mode="json"),
            "initialization": initialization,
            "tombstoned": job.deleted_at is not None,
        }
        now = self._job_now()
        conn.execute(
            """INSERT INTO notification_outbox (
                   job_id, job_status_version, payload_json, pending,
                   projected_status_version, created_at, updated_at
               ) VALUES (?, ?, ?, 1, NULL, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   job_status_version=excluded.job_status_version,
                   payload_json=excluded.payload_json,
                   pending=1,
                   updated_at=excluded.updated_at
               WHERE excluded.job_status_version > notification_outbox.job_status_version""",
            (
                job.id,
                job.status_version,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                now,
                now,
            ),
        )

    def pending_notification_outbox(self) -> tuple[dict[str, object], ...]:
        """Return pending lifecycle projections for the repairable projector."""
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT job_id, job_status_version, payload_json
                   FROM notification_outbox
                   WHERE pending=1 OR projected_status_version IS NULL
                      OR projected_status_version < job_status_version
                   ORDER BY updated_at, job_id"""
            ).fetchall()
        return tuple(
            {
                "job_id": str(row[0]),
                "job_status_version": int(row[1]),
                "payload": json.loads(str(row[2])),
            }
            for row in rows
        )

    def acknowledge_notification_outbox(
        self, job_id: str, job_status_version: int
    ) -> bool:
        """Acknowledge only the exact version that was projected."""
        with session(self._connect) as conn:
            cursor = conn.execute(
                """UPDATE notification_outbox
                   SET pending=CASE WHEN job_status_version=? THEN 0 ELSE 1 END,
                       projected_status_version=?, updated_at=?
                   WHERE job_id=? AND job_status_version=?""",
                (
                    job_status_version,
                    job_status_version,
                    self._job_now(),
                    job_id,
                    job_status_version,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _load_strategy_job(conn: sqlite3.Connection, job_id: str) -> StrategyJobV1:
        row = conn.execute(
            f"SELECT {', '.join(_JOB_COLUMNS)} FROM strategy_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"strategy job not found: {job_id}")
        try:
            return _row_to_strategy_job(row)
        except Exception as exc:
            raise BacktestIntegrityError("stored strategy job is invalid") from exc

    @staticmethod
    def _load_initialization(
        conn: sqlite3.Connection, job_id: str
    ) -> InitializationRunV1:
        row = conn.execute(
            """SELECT job_id, profile_hash, requested_start, requested_end,
                      requested_months_json, requested_month_digest,
                      calendar_dataset_version, qualification_contract_digest,
                      ordered_month_digest
               FROM initialization_runs WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"initialization run not found: {job_id}")
        try:
            return _row_to_initialization(row)
        except BacktestIntegrityError:
            raise
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored initialization run is invalid"
            ) from exc

    def _interval_is_ready_for_job(
        self,
        conn: sqlite3.Connection,
        profile_hash: str,
        requested_start: str,
        requested_end: str,
    ) -> bool:
        return self._interval_readiness_on_connection(
            conn, profile_hash, requested_start, requested_end
        ).ready

    def compare_and_insert_detector_fragment(
        self, key: DetectorCacheKey, canonical_json: str | bytes
    ) -> DetectorFragmentEnvelopeV1:
        raw = (
            canonical_json.encode("utf-8")
            if isinstance(canonical_json, str)
            else bytes(canonical_json)
        )
        try:
            envelope = DetectorFragmentEnvelopeV1.from_canonical_json(raw)
        except HistoricalScanContractError as exc:
            raise BacktestIntegrityError("detector fragment is not canonical") from exc
        self._verify_fragment_key(key, envelope)
        rendered = raw.decode("utf-8")
        digest = sha256(raw).hexdigest()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO scan_reconstruction_cache (
                       security_id, date, detector, detector_version, input_revision,
                       scan_result_json, scan_result_digest
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*key.sql_values(), rendered, digest),
            )
            row = conn.execute(
                """SELECT scan_result_json, scan_result_digest
                   FROM scan_reconstruction_cache
                   WHERE security_id=? AND date=? AND detector=?
                     AND detector_version=? AND input_revision=?""",
                key.sql_values(),
            ).fetchone()
            if row is None:
                raise BacktestIntegrityError("detector cache write was not visible")
            stored = self._validated_stored_fragment(key, str(row[0]), str(row[1]))
            if str(row[0]) != rendered or str(row[1]) != digest:
                raise BacktestIntegrityError(
                    "immutable detector cache key has conflicting content"
                )
            return stored

    def detector_fragment(
        self, key: DetectorCacheKey
    ) -> DetectorFragmentEnvelopeV1 | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT scan_result_json, scan_result_digest
                   FROM scan_reconstruction_cache
                   WHERE security_id=? AND date=? AND detector=?
                     AND detector_version=? AND input_revision=?""",
                key.sql_values(),
            ).fetchone()
        if row is None:
            return None
        return self._validated_stored_fragment(key, str(row[0]), str(row[1]))

    def detector_cache_count(self) -> int:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM scan_reconstruction_cache"
            ).fetchone()
        return 0 if row is None else int(row[0])

    def compare_and_insert_snapshot_profile(
        self, profile: SnapshotProfileV1
    ) -> SnapshotProfileV1:
        """Persist one immutable policy profile or verify its existing winner."""
        try:
            canonical = SnapshotProfileV1.from_canonical_json(
                profile.canonical_json_bytes()
            )
            self._validate_profile_authority(canonical)
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._insert_profile_on_connection(conn, canonical)
            return canonical
        except BacktestIntegrityError:
            raise
        except Exception as exc:
            raise BacktestIntegrityError("snapshot profile commit failed") from exc

    def snapshot_profile(self, profile_hash: str) -> SnapshotProfileV1 | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT canonical_profile_json, display_version, roster_digest,
                          scanner_schema_version, calendar_dataset_version,
                          calendar_dataset_digest, cadence
                   FROM snapshot_profiles WHERE profile_hash=?""",
                (profile_hash,),
            ).fetchone()
        if row is None:
            return None
        profile = self._validated_profile_row(profile_hash, row)
        self._validate_profile_authority(profile)
        return profile

    @classmethod
    def _validated_profile_row(
        cls, profile_hash: str, row: sqlite3.Row | tuple[object, ...]
    ) -> SnapshotProfileV1:
        try:
            profile = SnapshotProfileV1.from_canonical_json(str(row[0]))
        except Exception as exc:
            raise BacktestIntegrityError("stored snapshot profile is invalid") from exc
        if profile.profile_hash != profile_hash or tuple(
            str(item) for item in row[1:]
        ) != (
            profile.display_version,
            profile.roster_digest,
            profile.record_schema_version,
            profile.calendar_dataset_version,
            profile.calendar_dataset_digest,
            profile.cadence,
        ):
            raise BacktestIntegrityError("stored snapshot profile hash is invalid")
        return profile

    @staticmethod
    def _insert_profile_on_connection(
        conn: sqlite3.Connection, profile: SnapshotProfileV1
    ) -> None:
        profile_hash = profile.profile_hash
        rendered = profile.canonical_json()
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_profiles (
                   profile_hash, canonical_profile_json, display_version,
                   roster_digest, scanner_schema_version,
                   calendar_dataset_version, calendar_dataset_digest, cadence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_hash,
                rendered,
                profile.display_version,
                profile.roster_digest,
                profile.record_schema_version,
                profile.calendar_dataset_version,
                profile.calendar_dataset_digest,
                profile.cadence,
            ),
        )
        row = conn.execute(
            "SELECT canonical_profile_json FROM snapshot_profiles WHERE profile_hash=?",
            (profile_hash,),
        ).fetchone()
        if row is None or str(row[0]) != rendered:
            raise BacktestIntegrityError(
                "immutable snapshot profile has conflicting content"
            )

    @staticmethod
    def _validate_profile_authority(profile: SnapshotProfileV1) -> None:
        # Lazy imports preserve the qualification/evidence repository import graph.
        from app.services.backtest.detectors import DETECTOR_REGISTRY
        from app.services.backtest.source_manifest import detector_source_manifests

        calendar = TradingCalendar()
        if (
            profile.calendar_dataset_version != "exchange-calendars-v1"
            or profile.calendar_dataset_digest != calendar.session_table_digest()
        ):
            raise BacktestIntegrityError(
                "snapshot profile calendar does not match the canonical authority",
                code="calendar_error",
            )
        manifests = detector_source_manifests(_PROJECT_ROOT)
        detector_apis = {
            detector.detector_id: detector.detector_api_version
            for detector in DETECTOR_REGISTRY
        }
        if any(
            detector.detector_api_version != detector_apis[detector.detector_id]
            or detector.detector_version != manifests[detector.detector_id].digest
            for detector in profile.detectors
        ):
            raise BacktestIntegrityError(
                "snapshot profile detector manifests do not match the runtime authority"
            )

    def commit_snapshot_month(
        self,
        commit: MonthlySnapshotCommitV1,
        evidence_verifier: HistoricalEvidenceVerifier,
        *,
        job_claim: tuple[str, str] | None = None,
    ) -> SnapshotMonthManifestV1:
        """Atomically compare-and-insert one complete Ready snapshot month."""
        try:
            canonical = MonthlySnapshotCommitV1.from_canonical_json(
                commit.canonical_json_bytes()
            )
            self._validate_profile_authority(canonical.profile)
            try:
                TradingCalendar.closed_month(
                    canonical.manifest.snapshot_month, as_of=self._clock()
                )
            except ValueError as exc:
                raise BacktestIntegrityError(
                    "snapshot month is not fully closed", code="calendar_error"
                ) from exc
            self._verify_snapshot_input_evidence(canonical, evidence_verifier)
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if job_claim is not None:
                    owned = conn.execute(
                        """SELECT 1 FROM strategy_jobs
                           WHERE id=? AND status='running' AND claim_token=?""",
                        job_claim,
                    ).fetchone()
                    if owned is None:
                        raise StrategyJobConflict(
                            "snapshot publisher no longer owns the job"
                        )
                self._validate_snapshot_roster(conn, canonical)
                self._insert_profile_on_connection(conn, canonical.profile)
                existing = conn.execute(
                    """SELECT canonical_manifest_json FROM snapshot_months
                       WHERE profile_hash=? AND snapshot_month=?""",
                    (canonical.profile_hash, canonical.manifest.snapshot_month),
                ).fetchone()
                if existing is not None:
                    try:
                        existing_manifest = SnapshotMonthManifestV1.from_canonical_json(
                            str(existing[0])
                        )
                    except Exception as exc:
                        raise BacktestIntegrityError(
                            "stored snapshot month manifest is invalid"
                        ) from exc
                    if (
                        existing_manifest.content_digest
                        != canonical.manifest.content_digest
                    ):
                        raise BacktestIntegrityError(
                            "immutable snapshot month has conflicting content"
                        )
                    self._verify_snapshot_rows(
                        conn, canonical, allow_audit_metadata_difference=True
                    )
                    return existing_manifest

                for member in canonical.members:
                    conn.execute(
                        """INSERT INTO snapshot_members (
                               profile_hash, snapshot_month, security_id,
                               canonical_member_json, observed_symbol, mic,
                               as_of_session_date, resolution, source_cutoff,
                               source_payload_digest, input_revision,
                               provider_data_revision,
                               provider_evidence_manifest_digest, alias_revision,
                               record_digest,
                               exclusion_reason, exclusion_evidence_json,
                               provenance_digest
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            canonical.profile_hash,
                            canonical.manifest.snapshot_month,
                            member.security_id,
                            member.canonical_json(),
                            member.observed_symbol,
                            member.mic,
                            member.as_of_session_date.isoformat(),
                            member.resolution,
                            member.source_cutoff.isoformat(),
                            member.source_payload_digest,
                            member.input_revision,
                            member.provider_data_revision,
                            member.provider_evidence_manifest_digest,
                            member.alias_revision,
                            member.record_digest,
                            member.exclusion_reason,
                            (
                                None
                                if member.exclusion_evidence is None
                                else member.exclusion_evidence.canonical_json()
                            ),
                            member.provenance_digest,
                        ),
                    )
                for record in canonical.records:
                    conn.execute(
                        """INSERT INTO monthly_scan_results (
                               profile_hash, snapshot_month, security_id,
                               historical_scan_record_json, record_digest
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            canonical.profile_hash,
                            canonical.manifest.snapshot_month,
                            record.security_id,
                            record.canonical_json(),
                            record.digest(),
                        ),
                    )
                manifest = canonical.manifest
                manifest_json = manifest.model_dump(mode="json")
                conn.execute(
                    """INSERT INTO snapshot_months (
                           profile_hash, snapshot_month, canonical_manifest_json,
                           provenance_quality, processing_complete, market_complete,
                           roster_digest, expected_digest, input_revision_digest,
                           result_digest, expected_count, valid_count, excluded_count,
                           content_digest, source_run_id, observed_at, committed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        canonical.profile_hash,
                        manifest.snapshot_month,
                        manifest.canonical_json(),
                        manifest.provenance_quality,
                        1,
                        "unknown",
                        manifest.roster_digest,
                        manifest.expected_digest,
                        manifest.input_revision_digest,
                        manifest.result_digest,
                        manifest.expected_count,
                        manifest.valid_count,
                        manifest.excluded_count,
                        manifest.content_digest,
                        manifest.source_run_id,
                        manifest_json["observed_at"],
                        manifest_json["committed_at"],
                    ),
                )
                self._verify_snapshot_rows(conn, canonical)
                return manifest
        except (BacktestIntegrityError, StrategyJobConflict):
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "integrity_error"))
            if code in {"evidence_missing", "not_found"}:
                code = "required_data_missing"
            raise BacktestIntegrityError(
                "snapshot month transaction failed", code=code
            ) from exc

    @staticmethod
    def _verify_snapshot_input_evidence(
        commit: MonthlySnapshotCommitV1,
        evidence_verifier: HistoricalEvidenceVerifier,
    ) -> None:
        records = {item.security_id: item for item in commit.records}
        for member in commit.members:
            evidence = evidence_verifier.verify(member.provider_data_revision)
            try:
                verified_evidence_manifest(evidence)
            except SnapshotContractError as exc:
                raise BacktestIntegrityError(
                    "snapshot provider evidence is invalid", code=exc.code
                ) from exc
            if (
                evidence.data_revision != member.provider_data_revision
                or member.provider_evidence_manifest_digest != evidence.data_revision
                or evidence.security_id != member.security_id
                or evidence.observed_symbol != member.observed_symbol
                or evidence.request_contract_version
                != commit.profile.yfinance_request_contract_version
            ):
                raise BacktestIntegrityError(
                    "snapshot provider evidence does not match member"
                )
            if member.resolution == "valid_scan":
                record = records[member.security_id]
                if (
                    evidence.alias_revision != record.provenance.alias_revision
                    or evidence.currency != record.currency
                    or evidence.quote_unit != record.quote_unit
                ):
                    raise BacktestIntegrityError(
                        "snapshot provider evidence does not match record"
                    )
            else:
                proof = member.exclusion_evidence
                assert proof is not None
                if (
                    evidence.alias_revision != proof.alias_revision
                    or evidence.currency != proof.currency
                    or evidence.quote_unit != proof.quote_unit
                ):
                    raise BacktestIntegrityError(
                        "snapshot provider evidence does not match exclusion proof"
                    )
                rebuilt = build_before_first_provider_observation(
                    evidence=evidence,
                    snapshot_month=proof.snapshot_month,
                    target_session=proof.target_session,
                    mic=proof.mic,
                    alias_revision=proof.alias_revision,
                    alias_effective_from=proof.alias_effective_from,
                    alias_effective_to=proof.alias_effective_to,
                    calendar_dataset_version=proof.calendar_dataset_version,
                    calendar_dataset_digest=proof.calendar_dataset_digest,
                    acquired_at=proof.acquired_at,
                )
                if rebuilt.content_identity() != proof.content_identity():
                    raise BacktestIntegrityError(
                        "snapshot exclusion proof is not derived from provider evidence"
                    )

    @staticmethod
    def _validate_snapshot_roster(
        conn: sqlite3.Connection, commit: MonthlySnapshotCommitV1
    ) -> None:
        BacktestRepository._validate_snapshot_members_against_roster(
            conn, commit.profile, commit.members
        )

    @staticmethod
    def _validate_snapshot_members_against_roster(
        conn: sqlite3.Connection,
        profile: SnapshotProfileV1,
        members: tuple[SnapshotMemberV1, ...],
    ) -> None:
        rows = conn.execute(
            """SELECT member.security_id, member.mic, roster.alias_revision
               FROM reconstruction_roster_members member
               JOIN reconstruction_rosters roster
                 ON roster.roster_digest = member.roster_digest
               WHERE member.roster_digest=? ORDER BY member.security_id""",
            (profile.roster_digest,),
        ).fetchall()
        expected = tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)
        actual = tuple(
            (item.security_id, item.mic, item.alias_revision) for item in members
        )
        if not expected or actual != expected:
            raise BacktestIntegrityError(
                "snapshot members do not match the immutable reconstruction roster"
            )
        for member in members:
            alias = conn.execute(
                """SELECT 1 FROM security_alias_entries
                   WHERE alias_revision=? AND security_id=? AND provider='yfinance'
                     AND mic=? AND observed_symbol=?
                     AND (effective_from IS NULL OR effective_from<=?)
                     AND (effective_to IS NULL OR ?<effective_to)""",
                (
                    member.alias_revision,
                    member.security_id,
                    member.mic,
                    member.observed_symbol,
                    member.as_of_session_date.isoformat(),
                    member.as_of_session_date.isoformat(),
                ),
            ).fetchone()
            if alias is None:
                raise BacktestIntegrityError(
                    "snapshot member alias is not effective for the target session",
                    code="identity_ambiguous",
                )

    @staticmethod
    def _verify_snapshot_rows(
        conn: sqlite3.Connection,
        commit: MonthlySnapshotCommitV1,
        *,
        allow_audit_metadata_difference: bool = False,
    ) -> None:
        key = (commit.profile_hash, commit.manifest.snapshot_month)
        month = conn.execute(
            """SELECT canonical_manifest_json FROM snapshot_months
               WHERE profile_hash=? AND snapshot_month=?""",
            key,
        ).fetchone()
        if month is None:
            raise BacktestIntegrityError("stored snapshot month manifest is invalid")
        try:
            stored_manifest = SnapshotMonthManifestV1.from_canonical_json(str(month[0]))
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored snapshot month manifest is invalid"
            ) from exc
        if allow_audit_metadata_difference:
            if stored_manifest.content_digest != commit.manifest.content_digest:
                raise BacktestIntegrityError("stored snapshot month content is invalid")
        elif stored_manifest != commit.manifest:
            raise BacktestIntegrityError("stored snapshot month manifest is invalid")
        members = conn.execute(
            """SELECT canonical_member_json FROM snapshot_members
               WHERE profile_hash=? AND snapshot_month=? ORDER BY security_id""",
            key,
        ).fetchall()
        if allow_audit_metadata_difference:
            try:
                stored_members = tuple(
                    SnapshotMemberV1.from_canonical_json(str(row[0])) for row in members
                )
            except Exception as exc:
                raise BacktestIntegrityError(
                    "stored snapshot member evidence is invalid"
                ) from exc
            if tuple(item.content_identity() for item in stored_members) != tuple(
                item.content_identity() for item in commit.members
            ):
                raise BacktestIntegrityError(
                    "stored snapshot member evidence is invalid"
                )
        elif tuple(str(row[0]) for row in members) != tuple(
            item.canonical_json() for item in commit.members
        ):
            raise BacktestIntegrityError("stored snapshot member evidence is invalid")
        results = conn.execute(
            """SELECT historical_scan_record_json, record_digest
               FROM monthly_scan_results
               WHERE profile_hash=? AND snapshot_month=? ORDER BY security_id""",
            key,
        ).fetchall()
        expected_results = tuple(
            (item.canonical_json(), item.digest()) for item in commit.records
        )
        if tuple((str(row[0]), str(row[1])) for row in results) != expected_results:
            raise BacktestIntegrityError("stored monthly scan results are invalid")

    def snapshot_month(
        self, profile_hash: str, snapshot_month: str
    ) -> SnapshotMonthManifestV1 | None:
        with session(self._connect) as conn:
            return self._load_verified_snapshot_month(
                conn, profile_hash, snapshot_month
            )

    def _load_verified_snapshot_month(
        self,
        conn: sqlite3.Connection,
        profile_hash: str,
        snapshot_month: str,
    ) -> SnapshotMonthManifestV1 | None:
        profile_row = conn.execute(
            """SELECT canonical_profile_json, display_version, roster_digest,
                      scanner_schema_version, calendar_dataset_version,
                      calendar_dataset_digest, cadence
               FROM snapshot_profiles WHERE profile_hash=?""",
            (profile_hash,),
        ).fetchone()
        if profile_row is None:
            raise BacktestIntegrityError("snapshot profile does not exist")
        profile = self._validated_profile_row(profile_hash, profile_row)
        self._validate_profile_authority(profile)
        row = conn.execute(
            """SELECT canonical_manifest_json, provenance_quality,
                      processing_complete, market_complete, roster_digest,
                      expected_digest, input_revision_digest, result_digest,
                      expected_count, valid_count, excluded_count, content_digest,
                      source_run_id, observed_at, committed_at
               FROM snapshot_months
               WHERE profile_hash=? AND snapshot_month=?""",
            (profile_hash, snapshot_month),
        ).fetchone()
        if row is None:
            return None
        try:
            manifest = SnapshotMonthManifestV1.from_canonical_json(str(row[0]))
        except Exception as exc:
            raise BacktestIntegrityError("stored snapshot month is invalid") from exc
        manifest_json = manifest.model_dump(mode="json")
        if (
            manifest.profile_hash != profile_hash
            or manifest.snapshot_month != snapshot_month
            or (
                str(row[1]),
                int(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                int(row[8]),
                int(row[9]),
                int(row[10]),
                str(row[11]),
                None if row[12] is None else str(row[12]),
                None if row[13] is None else str(row[13]),
                str(row[14]),
            )
            != (
                manifest.provenance_quality,
                1,
                "unknown",
                manifest.roster_digest,
                manifest.expected_digest,
                manifest.input_revision_digest,
                manifest.result_digest,
                manifest.expected_count,
                manifest.valid_count,
                manifest.excluded_count,
                manifest.content_digest,
                manifest.source_run_id,
                manifest_json["observed_at"],
                manifest_json["committed_at"],
            )
        ):
            raise BacktestIntegrityError("stored snapshot month key is invalid")

        member_rows = conn.execute(
            """SELECT canonical_member_json, observed_symbol, mic,
                      as_of_session_date, resolution, source_cutoff,
                      source_payload_digest, input_revision, provider_data_revision,
                      provider_evidence_manifest_digest, alias_revision, record_digest,
                      exclusion_reason, exclusion_evidence_json, provenance_digest
               FROM snapshot_members
               WHERE profile_hash=? AND snapshot_month=? ORDER BY security_id""",
            (profile_hash, snapshot_month),
        ).fetchall()
        members: list[SnapshotMemberV1] = []
        try:
            for stored in member_rows:
                member = SnapshotMemberV1.from_canonical_json(str(stored[0]))
                expected_columns = (
                    member.observed_symbol,
                    member.mic,
                    member.as_of_session_date.isoformat(),
                    member.resolution,
                    member.source_cutoff.isoformat(),
                    member.source_payload_digest,
                    member.input_revision,
                    member.provider_data_revision,
                    member.provider_evidence_manifest_digest,
                    member.alias_revision,
                    member.record_digest,
                    member.exclusion_reason,
                    None
                    if member.exclusion_evidence is None
                    else member.exclusion_evidence.canonical_json(),
                    member.provenance_digest,
                )
                actual_columns = tuple(
                    None if item is None else str(item) for item in stored[1:]
                )
                if actual_columns != expected_columns:
                    raise ValueError("stored member columns differ from canonical JSON")
                members.append(member)
        except Exception as exc:
            raise BacktestIntegrityError("stored snapshot members are invalid") from exc

        result_rows = conn.execute(
            """SELECT security_id, historical_scan_record_json, record_digest
               FROM monthly_scan_results
               WHERE profile_hash=? AND snapshot_month=? ORDER BY security_id""",
            (profile_hash, snapshot_month),
        ).fetchall()
        records: list[HistoricalScanRecordV1] = []
        try:
            for stored in result_rows:
                record = HistoricalScanRecordV1.from_canonical_json(str(stored[1]))
                if (
                    str(stored[0]) != record.security_id
                    or str(stored[2]) != record.digest()
                ):
                    raise ValueError("stored result columns differ from canonical JSON")
                records.append(record)
            member_tuple = tuple(members)
            record_tuple = tuple(records)
            self._validate_snapshot_members_against_roster(conn, profile, member_tuple)
            MonthlySnapshotCommitV1._validate_members_and_records(
                profile,
                snapshot_month,
                manifest.provenance_quality,
                member_tuple,
                record_tuple,
            )
            expected_manifest = MonthlySnapshotCommitV1._manifest(
                profile=profile,
                snapshot_month=snapshot_month,
                provenance_quality=manifest.provenance_quality,
                members=member_tuple,
                records=record_tuple,
                committed_at=manifest.committed_at,
                source_run_id=manifest.source_run_id,
                observed_at=manifest.observed_at,
            )
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored snapshot write set is invalid"
            ) from exc
        if expected_manifest != manifest:
            raise BacktestIntegrityError("stored snapshot manifest digests are invalid")
        return manifest

    def activate_snapshot_profile(
        self, profile_hash: str, activated_at: datetime
    ) -> ActiveSnapshotProfileV1:
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                profile = conn.execute(
                    "SELECT canonical_profile_json FROM snapshot_profiles WHERE profile_hash=?",
                    (profile_hash,),
                ).fetchone()
                if profile is None:
                    raise BacktestIntegrityError("snapshot profile does not exist")
                parsed = SnapshotProfileV1.from_canonical_json(str(profile[0]))
                if parsed.profile_hash != profile_hash:
                    raise BacktestIntegrityError("snapshot profile identity is invalid")
                current = conn.execute(
                    """SELECT profile_hash, activation_seq, activated_at
                       FROM active_snapshot_profile WHERE singleton_id=1"""
                ).fetchone()
                if current is not None and str(current[0]) == profile_hash:
                    return ActiveSnapshotProfileV1(
                        profile_hash=profile_hash,
                        activation_seq=int(current[1]),
                        activated_at=datetime.fromisoformat(str(current[2])),
                    )
                next_seq = 1 if current is None else int(current[1]) + 1
                active = ActiveSnapshotProfileV1(
                    profile_hash=profile_hash,
                    activation_seq=next_seq,
                    activated_at=activated_at,
                )
                timestamp = active.model_dump(mode="json")["activated_at"]
                if current is None:
                    conn.execute(
                        """INSERT INTO active_snapshot_profile
                           (singleton_id, profile_hash, activation_seq, activated_at)
                           VALUES (1, ?, ?, ?)""",
                        (profile_hash, next_seq, timestamp),
                    )
                else:
                    cursor = conn.execute(
                        """UPDATE active_snapshot_profile
                           SET profile_hash=?, activation_seq=?, activated_at=?
                           WHERE singleton_id=1 AND activation_seq=?""",
                        (profile_hash, next_seq, timestamp, int(current[1])),
                    )
                    if cursor.rowcount != 1:
                        raise BacktestIntegrityError(
                            "active snapshot profile changed concurrently"
                        )
                return active
        except BacktestIntegrityError:
            raise
        except Exception as exc:
            raise BacktestIntegrityError("snapshot profile activation failed") from exc

    def active_snapshot_profile(self) -> ActiveSnapshotProfileV1 | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT profile_hash, activation_seq, activated_at
                   FROM active_snapshot_profile WHERE singleton_id=1"""
            ).fetchone()
        if row is None:
            return None
        try:
            return ActiveSnapshotProfileV1(
                profile_hash=str(row[0]),
                activation_seq=int(row[1]),
                activated_at=datetime.fromisoformat(str(row[2])),
            )
        except Exception as exc:
            raise BacktestIntegrityError("active snapshot profile is invalid") from exc

    def snapshot_coverage(self, profile_hash: str | None = None) -> CoverageSummaryV1:
        selected_hash = profile_hash
        if selected_hash is None:
            active = self.active_snapshot_profile()
            if active is None:
                raise BacktestIntegrityError("no active snapshot profile")
            selected_hash = active.profile_hash
        profile = self.snapshot_profile(selected_hash)
        if profile is None:
            raise BacktestIntegrityError("snapshot profile does not exist")
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT snapshot_month FROM snapshot_months
                   WHERE profile_hash=? AND processing_complete=1
                     AND market_complete='unknown'
                   ORDER BY snapshot_month""",
                (selected_hash,),
            ).fetchall()
            manifests = tuple(
                self._load_verified_snapshot_month(conn, selected_hash, str(row[0]))
                for row in rows
            )
        if any(item is None for item in manifests):
            raise BacktestIntegrityError("snapshot coverage evidence is invalid")
        manifests = tuple(item for item in manifests if item is not None)
        months = tuple(item.snapshot_month for item in manifests)
        intervals = self._coverage_intervals(months)
        provenance: list[ProvenanceCoverageV1] = []
        for quality in ("best_effort_reconstructed", "observed_bau"):
            quality_months = tuple(
                item.snapshot_month
                for item in manifests
                if item.provenance_quality == quality
            )
            if quality_months:
                provenance.append(
                    ProvenanceCoverageV1(
                        provenance_quality=quality,
                        snapshot_count=len(quality_months),
                        intervals=self._coverage_intervals(quality_months),
                    )
                )
        return CoverageSummaryV1(
            profile_hash=selected_hash,
            display_version=profile.display_version,
            earliest_month=None if not months else months[0],
            latest_month=None if not months else months[-1],
            snapshot_count=len(months),
            intervals=intervals,
            provenance=tuple(provenance),
        )

    @staticmethod
    def _coverage_intervals(months: tuple[str, ...]) -> tuple[CoverageIntervalV1, ...]:
        return tuple(
            CoverageIntervalV1(start_month=start, end_month=end)
            for start, end in TradingCalendar.contiguous_month_intervals(months)
        )

    def interval_readiness(
        self, profile_hash: str, start_month: str, end_month: str
    ) -> IntervalReadinessV1:
        if self.snapshot_profile(profile_hash) is None:
            raise BacktestIntegrityError("snapshot profile does not exist")
        with session(self._connect) as conn:
            return self._interval_readiness_on_connection(
                conn, profile_hash, start_month, end_month
            )

    def _interval_readiness_on_connection(
        self,
        conn: sqlite3.Connection,
        profile_hash: str,
        start_month: str,
        end_month: str,
    ) -> IntervalReadinessV1:
        requested = TradingCalendar.months_inclusive(start_month, end_month)
        rows = conn.execute(
            """SELECT snapshot_month FROM snapshot_months
               WHERE profile_hash=? AND snapshot_month>=? AND snapshot_month<=?
                 AND processing_complete=1 AND market_complete='unknown'
               ORDER BY snapshot_month""",
            (profile_hash, start_month, end_month),
        ).fetchall()
        manifests = tuple(
            self._load_verified_snapshot_month(conn, profile_hash, str(row[0]))
            for row in rows
        )
        if any(item is None for item in manifests):
            raise BacktestIntegrityError("snapshot interval evidence is invalid")
        manifests = tuple(item for item in manifests if item is not None)
        by_month = {item.snapshot_month: item for item in manifests}
        missing = tuple(month for month in requested if month not in by_month)
        if missing:
            return IntervalReadinessV1(
                profile_hash=profile_hash,
                start_month=start_month,
                end_month=end_month,
                ready=False,
                no_op=False,
                missing_months=missing,
                ordered_month_digest=None,
            )
        ordered_month_digest = manifest_digest(
            {
                "schema_version": "ordered_snapshot_months.v1",
                "profile_hash": profile_hash,
                "months": [
                    {
                        "snapshot_month": item.snapshot_month,
                        "roster_digest": item.roster_digest,
                        "expected_digest": item.expected_digest,
                        "input_revision_digest": item.input_revision_digest,
                        "provenance_quality": item.provenance_quality,
                        "content_digest": item.content_digest,
                    }
                    for item in (by_month[month] for month in requested)
                ],
            }
        )
        return IntervalReadinessV1(
            profile_hash=profile_hash,
            start_month=start_month,
            end_month=end_month,
            ready=True,
            no_op=True,
            missing_months=(),
            ordered_month_digest=ordered_month_digest,
        )

    @staticmethod
    def _verify_fragment_key(
        key: DetectorCacheKey, envelope: DetectorFragmentEnvelopeV1
    ) -> None:
        if (
            envelope.security_id,
            envelope.date,
            envelope.detector,
            envelope.detector_version,
            envelope.input_revision,
        ) != (
            key.security_id,
            key.date,
            key.detector,
            key.detector_version,
            key.input_revision,
        ):
            raise BacktestIntegrityError(
                "detector fragment envelope does not match cache key"
            )

    @classmethod
    def _validated_stored_fragment(
        cls, key: DetectorCacheKey, rendered: str, digest: str
    ) -> DetectorFragmentEnvelopeV1:
        raw = rendered.encode("utf-8")
        if sha256(raw).hexdigest() != digest:
            raise BacktestIntegrityError("detector cache digest is invalid")
        try:
            envelope = DetectorFragmentEnvelopeV1.from_canonical_json(raw)
        except HistoricalScanContractError as exc:
            raise BacktestIntegrityError("stored detector fragment is invalid") from exc
        cls._verify_fragment_key(key, envelope)
        return envelope

    def commit_roster_capture(self, commit: RosterCaptureCommit) -> str:
        """Atomically compare-and-insert a complete roster capture."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT roster_digest FROM reconstruction_roster_lineages WHERE lineage_id=?",
                (commit.lineage_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) == commit.roster_digest:
                    return commit.roster_digest
                raise sqlite3.IntegrityError(
                    "lineage is already bound to a different roster"
                )

            roster_preexisting = (
                conn.execute(
                    "SELECT 1 FROM reconstruction_rosters WHERE roster_digest=?",
                    (commit.roster_digest,),
                ).fetchone()
                is not None
            )
            alias_preexisting = (
                conn.execute(
                    "SELECT 1 FROM security_alias_manifests WHERE alias_revision=?",
                    (commit.alias_revision,),
                ).fetchone()
                is not None
            )
            self._insert_or_verify(
                conn,
                "security_identity_registry_revisions",
                "revision_digest",
                commit.identity_registry_revision,
                "canonical_manifest_json",
                commit.identity_registry_json,
                """INSERT INTO security_identity_registry_revisions
                   (revision_digest, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    commit.identity_registry_revision,
                    commit.identity_registry_json,
                    commit.identity_evidence_digest,
                    commit.captured_at,
                ),
            )
            for security_id, mic, symbol, evidence_digest in commit.identities:
                conn.execute(
                    """INSERT OR IGNORE INTO security_identities
                       (security_id, mic, provider_symbol, evidence_digest,
                        identity_registry_revision, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        security_id,
                        mic,
                        symbol,
                        evidence_digest,
                        commit.identity_registry_revision,
                        commit.captured_at,
                    ),
                )
                row = conn.execute(
                    """SELECT security_id, evidence_digest FROM security_identities
                       WHERE mic=? AND provider_symbol=?""",
                    (mic, symbol),
                ).fetchone()
                if row != (security_id, evidence_digest):
                    raise sqlite3.IntegrityError(
                        "canonical identity conflicts with existing security"
                    )

            self._insert_or_verify(
                conn,
                "security_alias_manifests",
                "alias_revision",
                commit.alias_revision,
                "canonical_manifest_json",
                commit.alias_manifest_json,
                """INSERT INTO security_alias_manifests
                   (alias_revision, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    commit.alias_revision,
                    commit.alias_manifest_json,
                    commit.alias_evidence_digest,
                    commit.captured_at,
                ),
            )
            if not alias_preexisting:
                for alias in commit.aliases:
                    conn.execute(
                        """INSERT INTO security_alias_entries
                           (alias_revision, security_id, provider, mic, observed_symbol,
                            effective_from, effective_to, evidence_source, evidence_digest,
                            provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (commit.alias_revision, *alias),
                    )

            self._insert_or_verify(
                conn,
                "reconstruction_rosters",
                "roster_digest",
                commit.roster_digest,
                "canonical_manifest_json",
                commit.roster_manifest_json,
                """INSERT INTO reconstruction_rosters
                   (roster_digest, policy_version, canonical_manifest_json,
                    identity_registry_revision, alias_revision, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    commit.roster_digest,
                    commit.policy_version,
                    commit.roster_manifest_json,
                    commit.identity_registry_revision,
                    commit.alias_revision,
                    commit.captured_at,
                ),
            )
            if not roster_preexisting:
                for source_order, source in enumerate(commit.sources):
                    conn.execute(
                        """INSERT INTO reconstruction_roster_sources
                           (roster_digest, source_name, payload_digest,
                            original_payload_json, retrieved_at, source_order)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (commit.roster_digest, *source, source_order),
                    )
                for member in commit.members:
                    conn.execute(
                        """INSERT INTO reconstruction_roster_members
                           (roster_digest, security_id, mic, provider_symbol, currency,
                            source_memberships_json, identity_evidence_json,
                            evidence_digest)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (commit.roster_digest, *member),
                    )
            conn.execute(
                """INSERT INTO reconstruction_roster_lineages
                   (lineage_id, roster_digest, bound_at) VALUES (?, ?, ?)""",
                (commit.lineage_id, commit.roster_digest, commit.captured_at),
            )
        return commit.roster_digest

    @staticmethod
    def _insert_or_verify(
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key: str,
        content_column: str,
        content: str,
        insert_sql: str,
        insert_values: tuple[object, ...],
    ) -> None:
        row = conn.execute(
            f"SELECT {content_column} FROM {table} WHERE {key_column}=?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(insert_sql, insert_values)
        elif str(row[0]) != content:
            raise sqlite3.IntegrityError(f"{table} digest collision")
