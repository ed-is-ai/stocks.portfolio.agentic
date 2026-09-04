"""Persistence for immutable Strategy Manager evidence and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
import html
import json
import logging
import sqlite3
from threading import RLock
from typing import (
    Any,
    Callable,
    Literal,
    Mapping,
    Protocol,
    TYPE_CHECKING,
    cast,
    overload,
)
from uuid import uuid4

from pydantic import ValidationError

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
    build_incomplete_detector_history,
    build_insufficient_detector_history,
    verified_evidence_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.strategy_job import (
    BacktestEnqueueResultV1,
    BacktestRunV1,
    BacktestSubmissionV1,
    BootstrapEnqueueResultV1,
    BootstrapSubmissionV1,
    BootstrapRunV1,
    ClaimedStrategyJobV1,
    InitializationEnqueueResultV1,
    InitializationRunV1,
    JobFailureCode,
    PreparationRunV1,
    PreparationSubmissionV1,
    PreparationEnqueueResultV1,
    RunUniverseSelectionV1,
    RecoveryAction,
    RecentJobFailureV1,
    STAGE_SEQUENCES,
    StrategyJobConflict,
    StrategyJobNotFound,
    StrategyJobStatus,
    StrategyJobType,
    StrategyJobV1,
    WorkerLeaseFenceV1,
    WorkerLeaseV1,
    requested_month_digest,
)
from app.services.backtest.strategy_protocol import (
    EntrySelectionDecisionV1,
    EntrySelectionState,
    InitialEntrySelectionV1,
    Signal,
    SignalSide,
    StrategyProtocolError,
    validate_initial_entry_selection,
)


if TYPE_CHECKING:
    # Deferred to break the real import cycle: ``backtest_engine.py`` and
    # ``metrics.py`` both import ``run_input_manifest.py``, which imports
    # ``BacktestRepository`` from this module. Every runtime use below
    # imports these names locally inside the method that needs them
    # (matching this file's existing lazy-import convention, e.g.
    # ``_qualification_is_current``); only static type-checking sees this
    # block, guarded by ``from __future__ import annotations`` deferring
    # every annotation in this file to a string.
    from app.services.backtest.backtest_engine import (
        EquityCurvePointV1,
        TradeLogEvent,
    )
    from app.services.backtest.metrics import BacktestMetricsV1, MetricAvailabilityV1

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)

# Backtest work is served by both the web process and the orchestrator.  Keep
# its SQLite tuning local to this repository: the trading and alert databases
# have different contention profiles and must not inherit it accidentally.
BACKTEST_SQLITE_BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class BauPromotionDecision:
    """Repository-owned result for a durable BAU envelope eligibility check."""

    eligible: bool
    reason: str | None = None


@dataclass(frozen=True)
class BauRunAuthority:
    """Durable scanner-run authority independent of presentation artifacts."""

    run_id: str
    profile_hash: str
    snapshot_month: str
    state: str
    attempted_at: datetime
    analysis_payload_digest: str | None
    capture_digest: str | None
    prepared_envelope_digest: str | None
    completed_envelope_digest: str | None
    completed_at: datetime | None
    failure_reason: str | None


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
    mic TEXT NOT NULL CHECK(mic IN ('BATS', 'XNAS', 'XNYS', 'XLON')),
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
    mic TEXT NOT NULL CHECK(mic IN ('BATS', 'XNAS', 'XNYS', 'XLON')),
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
-- Additive activation audit (gh-468): every activation writes one row in the
-- same transaction that overwrites ``active_snapshot_profile``, so the
-- predecessor of an active profile stays discoverable. Profiles activated
-- before this table existed fall back to the newest-committed-months
-- heuristic in ``previous_snapshot_profile``.
CREATE TABLE IF NOT EXISTS snapshot_profile_activation_history (
    profile_hash TEXT NOT NULL REFERENCES snapshot_profiles(profile_hash),
    activation_seq INTEGER NOT NULL CHECK(activation_seq > 0),
    activated_at TEXT NOT NULL,
    PRIMARY KEY(profile_hash, activation_seq)
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
    adopted_from_profile_hash TEXT CHECK(
        adopted_from_profile_hash IS NULL
        OR length(adopted_from_profile_hash) = 64
    ),
    PRIMARY KEY(profile_hash, snapshot_month),
    CHECK(expected_count = valid_count + excluded_count)
);
CREATE TABLE IF NOT EXISTS snapshot_members (
    profile_hash TEXT NOT NULL,
    snapshot_month TEXT NOT NULL,
    security_id TEXT NOT NULL,
    canonical_member_json TEXT NOT NULL,
    observed_symbol TEXT NOT NULL,
    mic TEXT NOT NULL CHECK(mic IN ('BATS', 'XNAS', 'XNYS', 'XLON')),
    as_of_session_date TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK(resolution IN ('valid_scan', 'legitimate_exclusion')),
    source_cutoff TEXT NOT NULL,
    source_payload_digest TEXT NOT NULL CHECK(length(source_payload_digest) = 64),
    input_revision TEXT NOT NULL CHECK(length(input_revision) = 64),
    provider_data_revision TEXT NOT NULL CHECK(length(provider_data_revision) = 64),
    provider_evidence_manifest_digest TEXT NOT NULL CHECK(length(provider_evidence_manifest_digest) = 64),
    alias_revision TEXT NOT NULL CHECK(length(alias_revision) = 64),
    record_digest TEXT CHECK(record_digest IS NULL OR length(record_digest) = 64),
    exclusion_reason TEXT CHECK(exclusion_reason IS NULL OR exclusion_reason IN (
        'before_first_provider_observation',
        'insufficient_detector_history',
        'incomplete_detector_history'
    )),
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
         AND exclusion_reason IN (
             'before_first_provider_observation',
             'insufficient_detector_history',
             'incomplete_detector_history'
         )
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

_BAU_RUN_AUTHORITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS bau_run_authority (
    run_id TEXT PRIMARY KEY,
    profile_hash TEXT NOT NULL CHECK(length(profile_hash) = 64),
    snapshot_month TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('attempted', 'prepared', 'completed', 'failed')),
    attempted_at TEXT NOT NULL,
    analysis_payload_digest TEXT CHECK(
        analysis_payload_digest IS NULL OR length(analysis_payload_digest) = 64
    ),
    capture_digest TEXT CHECK(capture_digest IS NULL OR length(capture_digest) = 64),
    prepared_envelope_digest TEXT CHECK(
        prepared_envelope_digest IS NULL OR length(prepared_envelope_digest) = 64
    ),
    completed_envelope_digest TEXT CHECK(
        completed_envelope_digest IS NULL OR length(completed_envelope_digest) = 64
    ),
    completed_at TEXT,
    failure_reason TEXT,
    UNIQUE(profile_hash, snapshot_month),
    CHECK(
        (state = 'attempted'
         AND analysis_payload_digest IS NULL
         AND capture_digest IS NULL
         AND prepared_envelope_digest IS NULL
         AND completed_envelope_digest IS NULL
         AND completed_at IS NULL)
        OR
        (state = 'prepared'
         AND analysis_payload_digest IS NOT NULL
         AND capture_digest IS NOT NULL
         AND prepared_envelope_digest IS NOT NULL
         AND completed_envelope_digest IS NULL
         AND completed_at IS NULL)
        OR
        (state = 'completed'
         AND analysis_payload_digest IS NOT NULL
         AND capture_digest IS NOT NULL
         AND prepared_envelope_digest IS NOT NULL
         AND completed_envelope_digest IS NOT NULL
         AND completed_at IS NOT NULL
         AND failure_reason IS NULL)
        OR
        (state = 'failed' AND completed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_bau_run_authority_state
ON bau_run_authority(state, attempted_at);

CREATE TRIGGER IF NOT EXISTS bau_run_authority_identity_immutable
BEFORE UPDATE ON bau_run_authority
WHEN NEW.run_id != OLD.run_id
  OR NEW.profile_hash != OLD.profile_hash
  OR NEW.snapshot_month != OLD.snapshot_month
  OR NEW.attempted_at != OLD.attempted_at
BEGIN SELECT RAISE(ABORT, 'BAU run authority identity is immutable'); END;

CREATE TRIGGER IF NOT EXISTS bau_run_authority_legal_transition
BEFORE UPDATE ON bau_run_authority
WHEN NOT (
    (OLD.state = 'attempted' AND NEW.state IN ('prepared', 'failed'))
    OR (OLD.state = 'prepared' AND NEW.state IN ('completed', 'failed'))
)
BEGIN SELECT RAISE(ABORT, 'illegal BAU run authority transition'); END;

CREATE TRIGGER IF NOT EXISTS bau_run_authority_immutable_delete
BEFORE DELETE ON bau_run_authority
BEGIN SELECT RAISE(ABORT, 'BAU run authority is immutable'); END;
"""

_STRATEGY_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_worker_lease (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    instance_id TEXT NOT NULL CHECK(length(instance_id) > 0),
    generation INTEGER NOT NULL CHECK(generation > 0),
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK(expires_at > heartbeat_at)
);

CREATE TRIGGER IF NOT EXISTS strategy_worker_lease_generation_monotonic
BEFORE UPDATE ON strategy_worker_lease
WHEN NEW.generation < OLD.generation
   OR (NEW.instance_id != OLD.instance_id
       AND NEW.generation != OLD.generation + 1)
BEGIN SELECT RAISE(ABORT, 'worker lease generation is not monotonic'); END;

CREATE TRIGGER IF NOT EXISTS strategy_worker_lease_immutable_delete
BEFORE DELETE ON strategy_worker_lease
BEGIN SELECT RAISE(ABORT, 'worker lease is immutable'); END;

CREATE TABLE IF NOT EXISTS strategy_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL CHECK(job_type IN (
        'bootstrap', 'initialization', 'preparation', 'backtest'
    )),
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'complete', 'failed', 'cancelled'
    )),
    parent_job_id TEXT REFERENCES strategy_jobs(id),
    enqueue_seq INTEGER NOT NULL UNIQUE CHECK(enqueue_seq > 0),
    claim_token TEXT,
    current_month TEXT,
    current_stage TEXT CHECK(current_stage IS NULL OR current_stage IN (
        'qualification', 'roster_capture', 'profile_activation',
        'evidence_selection', 'fx_pinning', 'manifest_sealing'
    )),
    owner_instance_id TEXT,
    lease_generation INTEGER CHECK(lease_generation IS NULL OR lease_generation > 0),
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
    CHECK(status != 'queued' OR (
        claim_token IS NULL AND current_month IS NULL AND current_stage IS NULL
        AND owner_instance_id IS NULL AND lease_generation IS NULL
    )),
    CHECK(status != 'running' OR claim_token IS NOT NULL),
    CHECK((owner_instance_id IS NULL) = (lease_generation IS NULL)),
    CHECK(status NOT IN ('complete', 'failed', 'cancelled') OR (
        current_month IS NULL AND current_stage IS NULL
        AND owner_instance_id IS NULL AND lease_generation IS NULL
    )),
    CHECK(job_type IN ('bootstrap', 'preparation') OR current_stage IS NULL),
    CHECK(job_type IN ('initialization', 'backtest') OR current_month IS NULL),
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
    mode TEXT NOT NULL DEFAULT 'rebuild' CHECK(mode IN ('update', 'rebuild')),
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
    OR NEW.current_stage IS NOT OLD.current_stage
    OR NEW.owner_instance_id IS NOT OLD.owner_instance_id
    OR NEW.lease_generation IS NOT OLD.lease_generation
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

DROP TRIGGER IF EXISTS strategy_job_version_requires_mutation;
CREATE TRIGGER strategy_job_version_requires_mutation
BEFORE UPDATE ON strategy_jobs
WHEN NEW.status_version != OLD.status_version
 AND NEW.status IS OLD.status
 AND NEW.claim_token IS OLD.claim_token
 AND NEW.current_month IS OLD.current_month
 AND NEW.current_stage IS OLD.current_stage
 AND NEW.owner_instance_id IS OLD.owner_instance_id
 AND NEW.lease_generation IS OLD.lease_generation
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
  OR NEW.mode != OLD.mode
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

CREATE TABLE IF NOT EXISTS bootstrap_runs (
    job_id TEXT PRIMARY KEY REFERENCES strategy_jobs(id)
);

CREATE TRIGGER IF NOT EXISTS bootstrap_subtype_matches_job
BEFORE INSERT ON bootstrap_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = NEW.job_id AND job.job_type = 'bootstrap'
 )
BEGIN SELECT RAISE(ABORT, 'bootstrap subtype does not match job'); END;

CREATE TRIGGER IF NOT EXISTS bootstrap_job_requires_subtype_before_running
BEFORE UPDATE OF status ON strategy_jobs
WHEN NEW.job_type = 'bootstrap'
 AND NEW.status = 'running'
 AND NOT EXISTS (
    SELECT 1 FROM bootstrap_runs run WHERE run.job_id = NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'bootstrap subtype is missing'); END;

CREATE TRIGGER IF NOT EXISTS bootstrap_run_immutable
BEFORE UPDATE ON bootstrap_runs
BEGIN SELECT RAISE(ABORT, 'bootstrap run is immutable'); END;

DROP TRIGGER IF EXISTS bootstrap_run_immutable_delete;
CREATE TRIGGER bootstrap_run_immutable_delete
BEFORE DELETE ON bootstrap_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = OLD.job_id AND job.deleted_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'bootstrap run is immutable'); END;

CREATE TABLE IF NOT EXISTS preparation_runs (
    job_id TEXT PRIMARY KEY REFERENCES strategy_jobs(id)
);
CREATE TABLE IF NOT EXISTS preparation_enqueue_actions(idempotency_key TEXT PRIMARY KEY,submission_digest TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE REFERENCES preparation_runs(job_id),created_at TEXT NOT NULL);

CREATE TRIGGER IF NOT EXISTS preparation_subtype_matches_job
BEFORE INSERT ON preparation_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = NEW.job_id AND job.job_type = 'preparation'
 )
BEGIN SELECT RAISE(ABORT, 'preparation subtype does not match job'); END;

CREATE TRIGGER IF NOT EXISTS preparation_job_requires_subtype_before_running
BEFORE UPDATE OF status ON strategy_jobs
WHEN NEW.job_type = 'preparation'
 AND NEW.status = 'running'
 AND NOT EXISTS (
    SELECT 1 FROM preparation_runs run WHERE run.job_id = NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'preparation subtype is missing'); END;

CREATE TRIGGER IF NOT EXISTS preparation_run_immutable
BEFORE UPDATE ON preparation_runs
BEGIN SELECT RAISE(ABORT, 'preparation run is immutable'); END;

DROP TRIGGER IF EXISTS preparation_run_immutable_delete;
CREATE TRIGGER preparation_run_immutable_delete
BEFORE DELETE ON preparation_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = OLD.job_id AND job.deleted_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'preparation run is immutable'); END;

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

#: Story 2.5 (AD-9): Strategy Run identity/pin, attempt-owned staging, and
#: the immutable Result/Trade Log/Equity Curve a completed attempt
#: promotes. Story 2.6 owns enqueue/claim/cancel/restart/delete -- it
#: creates ``strategy_runs``/``run_input_manifests`` rows before a real
#: backtest job may transition to ``running``. This schema deliberately
#: does not add a trigger enforcing that on ``strategy_jobs`` itself:
#: Story 2.2/2.3 already established a lightweight ``job_type='backtest'``
#: placeholder (no subtype row) sharing the FIFO with initialization jobs
#: (``test_initialization_and_backtest_placeholders_share_one_fifo``), and
#: this story must not narrow that existing contract. ``write_backtest_
#: staging``/``complete_claimed_backtest_job`` enforce the real
#: prerequisite (a ``strategy_runs``/staging row must exist) themselves.
_BACKTEST_RESULT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_input_manifests (
    digest TEXT PRIMARY KEY CHECK(length(digest) = 64),
    execution_contract_digest TEXT NOT NULL CHECK(
        length(execution_contract_digest) = 64
    ),
    canonical_manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_input_manifests_execution_contract
ON run_input_manifests(execution_contract_digest);

CREATE TRIGGER IF NOT EXISTS run_input_manifest_immutable_update
BEFORE UPDATE ON run_input_manifests
BEGIN SELECT RAISE(ABORT, 'run input manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS run_input_manifest_immutable_delete
BEFORE DELETE ON run_input_manifests
BEGIN SELECT RAISE(ABORT, 'run input manifest is immutable'); END;

CREATE TABLE IF NOT EXISTS strategy_runs (
    id TEXT PRIMARY KEY REFERENCES strategy_jobs(id),
    strategy_id TEXT NOT NULL,
    strategy_api_version INTEGER NOT NULL CHECK(strategy_api_version > 0),
    strategy_source_digest TEXT NOT NULL CHECK(length(strategy_source_digest) = 64),
    parameters_json TEXT NOT NULL,
    profile_hash TEXT NOT NULL REFERENCES snapshot_profiles(profile_hash),
    start_month TEXT NOT NULL,
    end_month TEXT NOT NULL,
    ordered_month_digest TEXT NOT NULL CHECK(length(ordered_month_digest) = 64),
    base_currency TEXT NOT NULL CHECK(base_currency IN ('GBP', 'USD')),
    starting_capital TEXT NOT NULL,
    run_input_manifest_digest TEXT NOT NULL REFERENCES run_input_manifests(digest),
    execution_contract_digest TEXT NOT NULL CHECK(
        length(execution_contract_digest) = 64
    ),
    created_at TEXT NOT NULL,
    CHECK(start_month <= end_month)
);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_comparison_dimensions
ON strategy_runs(
    start_month, end_month, profile_hash, ordered_month_digest,
    base_currency, execution_contract_digest
);

CREATE TRIGGER IF NOT EXISTS strategy_run_subtype_matches_job
BEFORE INSERT ON strategy_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    WHERE job.id = NEW.id AND job.job_type = 'backtest'
)
BEGIN SELECT RAISE(ABORT, 'strategy run subtype does not match job'); END;

CREATE TRIGGER IF NOT EXISTS strategy_run_immutable_update
BEFORE UPDATE ON strategy_runs
BEGIN SELECT RAISE(ABORT, 'strategy run configuration is immutable'); END;

CREATE TRIGGER IF NOT EXISTS strategy_run_immutable_delete
BEFORE DELETE ON strategy_runs
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job WHERE job.id = OLD.id AND job.deleted_at IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'strategy run is immutable'); END;

CREATE TABLE IF NOT EXISTS backtest_staging (
    run_id TEXT PRIMARY KEY REFERENCES strategy_runs(id),
    state_schema_version TEXT NOT NULL,
    state_json TEXT NOT NULL,
    events_json TEXT NOT NULL,
    equity_curve_json TEXT NOT NULL,
    final_cash_base TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_staging_entry_selection (
    run_id TEXT PRIMARY KEY REFERENCES backtest_staging(run_id) ON DELETE CASCADE,
    session TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    rule_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_staging_entry_selection_decisions (
    run_id TEXT NOT NULL REFERENCES backtest_staging_entry_selection(run_id)
        ON DELETE CASCADE,
    security_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank > 0),
    state TEXT NOT NULL CHECK(state IN (
        'selected', 'eligible_not_selected', 'excluded'
    )),
    score TEXT,
    reason_code TEXT,
    PRIMARY KEY(run_id, security_id),
    UNIQUE(run_id, rank)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    run_id TEXT PRIMARY KEY REFERENCES strategy_runs(id),
    result_schema_version TEXT NOT NULL DEFAULT 'backtest_result.v1'
        CHECK(result_schema_version IN ('backtest_result.v1', 'backtest_result.v2')),
    metrics_json TEXT NOT NULL,
    final_cash_base TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK(length(result_digest) = 64),
    note TEXT,
    note_version INTEGER NOT NULL CHECK(note_version > 0),
    completed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_result_entry_selection (
    run_id TEXT PRIMARY KEY REFERENCES backtest_results(run_id),
    session TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    rule_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_result_entry_selection_decisions (
    run_id TEXT NOT NULL REFERENCES backtest_result_entry_selection(run_id),
    security_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank > 0),
    state TEXT NOT NULL CHECK(state IN (
        'selected', 'eligible_not_selected', 'excluded'
    )),
    score TEXT,
    reason_code TEXT,
    PRIMARY KEY(run_id, security_id),
    UNIQUE(run_id, rank)
);

CREATE TRIGGER IF NOT EXISTS backtest_result_entry_selection_immutable_update
BEFORE UPDATE ON backtest_result_entry_selection
BEGIN SELECT RAISE(ABORT, 'backtest result entry selection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_entry_selection_immutable_delete
BEFORE DELETE ON backtest_result_entry_selection
BEGIN SELECT RAISE(ABORT, 'backtest result entry selection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_entry_selection_decision_immutable_update
BEFORE UPDATE ON backtest_result_entry_selection_decisions
BEGIN SELECT RAISE(ABORT, 'backtest result entry selection decision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_entry_selection_decision_immutable_delete
BEFORE DELETE ON backtest_result_entry_selection_decisions
BEGIN SELECT RAISE(ABORT, 'backtest result entry selection decision is immutable'); END;

CREATE TRIGGER IF NOT EXISTS backtest_result_evidence_immutable
BEFORE UPDATE ON backtest_results
WHEN NEW.run_id != OLD.run_id
  OR NEW.result_schema_version != OLD.result_schema_version
  OR NEW.metrics_json != OLD.metrics_json
  OR NEW.final_cash_base != OLD.final_cash_base
  OR NEW.result_digest != OLD.result_digest
  OR NEW.completed_at != OLD.completed_at
BEGIN SELECT RAISE(ABORT, 'backtest result evidence is immutable'); END;

CREATE TRIGGER IF NOT EXISTS backtest_result_note_version_monotonic
BEFORE UPDATE ON backtest_results
WHEN NEW.note_version != OLD.note_version + 1
BEGIN SELECT RAISE(ABORT, 'backtest result note version is not monotonic'); END;

CREATE TRIGGER IF NOT EXISTS backtest_result_immutable_delete
BEFORE DELETE ON backtest_results
BEGIN SELECT RAISE(ABORT, 'backtest result is immutable'); END;

CREATE TABLE IF NOT EXISTS trade_log (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_results(run_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    kind TEXT NOT NULL CHECK(kind IN (
        'entry_fill', 'exit_fill', 'skipped_signal', 'split_applied',
        'dividend_applied', 'open_position_mark'
    )),
    security_id TEXT NOT NULL,
    event_json TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_trade_log_run_sequence ON trade_log(run_id, sequence);

CREATE TRIGGER IF NOT EXISTS trade_log_immutable_update
BEFORE UPDATE ON trade_log
BEGIN SELECT RAISE(ABORT, 'trade log is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trade_log_immutable_delete
BEFORE DELETE ON trade_log
BEGIN SELECT RAISE(ABORT, 'trade log is immutable'); END;

CREATE TABLE IF NOT EXISTS equity_curve (
    run_id TEXT NOT NULL REFERENCES backtest_results(run_id),
    date TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    cash_base TEXT NOT NULL,
    positions_value_base TEXT NOT NULL,
    total_equity_base TEXT NOT NULL,
    PRIMARY KEY(run_id, date),
    UNIQUE(run_id, sequence)
);

CREATE TRIGGER IF NOT EXISTS equity_curve_immutable_update
BEFORE UPDATE ON equity_curve
BEGIN SELECT RAISE(ABORT, 'equity curve is immutable'); END;
CREATE TRIGGER IF NOT EXISTS equity_curve_immutable_delete
BEFORE DELETE ON equity_curve
BEGIN SELECT RAISE(ABORT, 'equity curve is immutable'); END;

-- Story 2.6: enqueue-time action idempotency for ``create_backtest_job``,
-- mirroring ``strategy_job_restart_actions``' idempotency-key shape but
-- keyed on the key alone (no source job exists yet at initial enqueue).
-- A caller-supplied key retrying the identical submission returns the
-- same attempt; an enqueue with no key (NULL) never dedupes -- every
-- distinct intentional submission with no key creates a distinct attempt.
-- ``submission_digest`` pins the exact submission content a key was first
-- committed with (Story 2.6 review), so a key replayed against a
-- divergent submission is rejected rather than silently returning a
-- stale attempt for the wrong content.
CREATE TABLE IF NOT EXISTS backtest_enqueue_actions (
    idempotency_key TEXT PRIMARY KEY CHECK(length(idempotency_key) > 0),
    job_id TEXT NOT NULL UNIQUE REFERENCES strategy_jobs(id),
    submission_digest TEXT NOT NULL CHECK(length(submission_digest) = 64),
    created_at TEXT NOT NULL
);

-- Story 4.6.2: durable Bootstrap submission identity.  This binds a caller
-- key to its canonical request without introducing another job lifecycle.
CREATE TABLE IF NOT EXISTS bootstrap_enqueue_actions (
    idempotency_key TEXT PRIMARY KEY CHECK(
        length(idempotency_key) BETWEEN 1 AND 200
        AND length(trim(idempotency_key)) > 0
    ),
    job_id TEXT NOT NULL UNIQUE REFERENCES strategy_jobs(id),
    submission_digest TEXT NOT NULL CHECK(length(submission_digest) = 64),
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS bootstrap_enqueue_action_requires_bootstrap_job
BEFORE INSERT ON bootstrap_enqueue_actions
WHEN NOT EXISTS (
    SELECT 1 FROM strategy_jobs job
    JOIN bootstrap_runs run ON run.job_id = job.id
    WHERE job.id = NEW.job_id AND job.job_type = 'bootstrap'
)
BEGIN SELECT RAISE(ABORT, 'bootstrap enqueue action requires bootstrap job'); END;

CREATE TRIGGER IF NOT EXISTS bootstrap_enqueue_action_immutable_update
BEFORE UPDATE ON bootstrap_enqueue_actions
BEGIN SELECT RAISE(ABORT, 'bootstrap enqueue action is immutable'); END;

CREATE TRIGGER IF NOT EXISTS bootstrap_enqueue_action_immutable_delete
BEFORE DELETE ON bootstrap_enqueue_actions
BEGIN SELECT RAISE(ABORT, 'bootstrap enqueue action is immutable'); END;
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


#: Unicode code-point cap on a Backtest Result note's escaped, persisted
#: text (AC 5) -- checked after ``html.escape`` (whose entity expansion can
#: grow the text) and before persistence, never truncated silently.
_NOTE_MAX_CODE_POINTS = 10_000


@dataclass(frozen=True)
class _StrategyRunRow:
    """One pinned Backtest Run identity, as ``strategy_runs`` stores it
    (AD-9) -- created by Story 2.6's enqueue, read-only here."""

    id: str
    strategy_id: str
    strategy_api_version: int
    strategy_source_digest: str
    parameters: dict[str, object]
    profile_hash: str
    start_month: str
    end_month: str
    ordered_month_digest: str
    base_currency: str
    starting_capital: Decimal
    run_input_manifest_digest: str
    execution_contract_digest: str
    manifest_version: str
    universe_selection: RunUniverseSelectionV1 | None
    source_preparation_job_id: str | None


@dataclass(frozen=True)
class BacktestStagingV1:
    """One attempt-owned staging row's canonical content (AC 1, 6) --
    versioned portfolio state, ordered Trade Log events, and the ordered
    Equity Curve a running attempt has produced so far."""

    run_id: str
    state_schema_version: str
    portfolio_state: dict[str, object]
    events: tuple[TradeLogEvent, ...]
    equity_curve: tuple[EquityCurvePointV1, ...]
    final_cash_base: Decimal
    updated_at: str
    initial_entry_selection: InitialEntrySelectionV1 | None = None


@dataclass(frozen=True)
class BacktestResultV1:
    """One completed Backtest Result's full typed retrieval projection
    (AC 5): Strategy ID/version, exact parameters, normalized period,
    profile/ordered evidence, capital/base currency, full replay/
    execution-contract digests, the four Metrics plus typed availability
    reasons, the complete ordered Trade Log, the Equity Curve,
    provenance, and optional note state."""

    run_id: str
    strategy_id: str
    strategy_api_version: int
    strategy_source_digest: str
    parameters: dict[str, object]
    profile_hash: str
    start_month: str
    end_month: str
    ordered_month_digest: str
    base_currency: str
    starting_capital: Decimal
    run_input_manifest_digest: str
    execution_contract_digest: str
    metrics: BacktestMetricsV1
    metric_availability: MetricAvailabilityV1
    events: tuple[TradeLogEvent, ...]
    equity_curve: tuple[EquityCurvePointV1, ...]
    final_cash_base: Decimal
    completed_at: datetime
    note: str | None
    note_version: int
    manifest_version: str = "run_input_manifest.v1"
    universe_selection: RunUniverseSelectionV1 | None = None
    source_preparation_job_id: str | None = None
    initial_entry_selection: InitialEntrySelectionV1 | None = None


@dataclass(frozen=True)
class BacktestActivitySummaryV1:
    """One row of the Backtest activity/results list (Story 2.8 AC 1) --
    persisted Strategy/version, a deterministic parameter summary built
    from persisted typed parameters (independent of whether the Skill
    still exists on disk), the normalized period, and Metrics only from a
    verified complete Result -- ``None`` for every non-complete job,
    never a zero-filled stand-in.

    gh-434 adds the display-only universe context: the run's pinned
    ``profile_hash``, the canonical security IDs parsed from the
    persisted ``selection_json`` (``None`` for legacy runs without one or
    whose stored JSON no longer validates), and the tuning-parameters
    dict with the universe-selection keys removed."""

    job: StrategyJobV1
    strategy_id: str
    strategy_api_version: int
    parameter_summary: str
    start_month: str
    end_month: str
    metrics: "BacktestMetricsV1 | None"
    metric_availability: "MetricAvailabilityV1 | None"
    profile_hash: str | None = None
    universe_security_ids: tuple[str, ...] | None = None
    tuning_parameters: dict[str, object] | None = None


class ComparisonIneligibleReason(StrEnum):
    """Stable, machine-readable reasons two Backtest Results are not
    eligible for comparison (AD-19, Story 3.1) -- one code per rejection
    dimension, mirroring ``MetricUnavailableReason``/``SkipReasonCode``'s
    established enum-plus-frozen-dataclass idiom."""

    NOT_FOUND = "not_found"
    SELF_COMPARISON = "self_comparison"
    TOMBSTONED = "tombstoned"
    NOT_COMPLETE = "not_complete"
    PERIOD_MISMATCH = "period_mismatch"
    PROFILE_MISMATCH = "profile_mismatch"
    EVIDENCE_DIGEST_MISMATCH = "evidence_digest_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    EXECUTION_CONTRACT_MISMATCH = "execution_contract_mismatch"
    MANIFEST_VERSION_MISMATCH = "manifest_version_mismatch"


@dataclass(frozen=True)
class ComparisonEligibilityV1:
    """The typed, exhaustive return of ``is_comparable`` (AD-19, Story
    3.1) -- either eligible with no reason, or ineligible with a stable
    machine-readable reason plus a human-readable ``detail`` naming what
    differed. ``detail`` is a diagnostic string for logs/debugging, not
    pre-approved UI copy -- callers building user-facing messages should
    key off ``reason`` instead."""

    eligible: bool
    reason: ComparisonIneligibleReason | None
    detail: str


@dataclass(frozen=True)
class ComparisonCandidateV1:
    """One other Backtest Result eligible for comparison against an
    anchor Result (Story 3.1 AC 3) -- exactly the fields the picker needs
    to display: Strategy identity, parameter summary, normalized period,
    base currency, and data-version context."""

    run_id: str
    strategy_id: str
    strategy_api_version: int
    parameter_summary: str
    start_month: str
    end_month: str
    base_currency: str
    profile_hash: str


SnapshotEvidenceV1 = HistoricalEvidenceV1


@dataclass(frozen=True)
class ProfileMemberDeltaV1:
    """Roster delta between two snapshot profiles (gh-468).

    Members are ``(security_id, provider_symbol, mic, currency)`` tuples in
    ``roster_member_identities`` order. A member whose identity tuple differs
    between the two profiles counts as both removed and added: its carried
    evidence identity is not stable, so it must resolve fresh.
    """

    previous_profile_hash: str
    next_profile_hash: str
    added: tuple[tuple[str, str, str, str], ...]
    removed: tuple[tuple[str, str, str, str], ...]
    unchanged: tuple[tuple[str, str, str, str], ...]


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
    "current_stage",
    "owner_instance_id",
    "lease_generation",
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


#: Appended to every job-mutating CAS predicate. A database with no
#: persisted lease row has no worker-ownership concept at all, so the
#: fence is vacuously satisfied; once a lease exists, only a writer
#: presenting that exact ``(instance_id, generation)`` pair may mutate a
#: job, and a writer whose generation has been superseded by a takeover
#: matches no row and leaves the job untouched.
_LEASE_FENCE_SQL = """
                     AND (
                        NOT EXISTS (
                            SELECT 1 FROM strategy_worker_lease WHERE singleton_id=1
                        )
                        OR EXISTS (
                            SELECT 1 FROM strategy_worker_lease
                            WHERE singleton_id=1 AND instance_id=? AND generation=?
                        )
                     )"""


#: The one ``(table, job-id column)`` each job type's identity row lives
#: in -- every ``strategy_jobs`` row has exactly one row in exactly one of
#: these. ``strategy_runs`` predates the four-type schema and keys its own
#: job id as ``id`` rather than ``job_id``.
_SUBTYPE_TABLES: dict[StrategyJobType, tuple[str, str]] = {
    StrategyJobType.BOOTSTRAP: ("bootstrap_runs", "job_id"),
    StrategyJobType.INITIALIZATION: ("initialization_runs", "job_id"),
    StrategyJobType.PREPARATION: ("preparation_runs", "job_id"),
    StrategyJobType.BACKTEST: ("strategy_runs", "id"),
}


def _lease_fence_params(
    lease: "WorkerLeaseFenceV1 | None",
) -> tuple[str | None, int | None]:
    """Return the ``(instance_id, generation)`` bindings ``_LEASE_FENCE_SQL``
    expects -- ``(None, None)`` never matches a persisted lease row, so an
    unfenced write is rejected the moment any lease is held."""
    return (None, None) if lease is None else (lease.instance_id, lease.generation)


def _optional_instant(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _parameter_summary(parameters: dict[str, object]) -> str:
    """Deterministic, concise ``key=value`` summary in sorted key order --
    independent of Skill discovery/schema order (Story 2.8 AC 1), since a
    persisted attempt's identity must survive the Skill being removed."""
    if not parameters:
        return "(defaults)"
    return ", ".join(f"{key}={value!r}" for key, value in sorted(parameters.items()))


#: The default parameter key carrying a run's security universe, plus the
#: pre-gh-434 alias -- both are universe selection, never a tuning knob.
UNIVERSE_PARAMETER_KEYS = ("security_ids", "selected_securities")

#: ``RunUniverseSelectionV1.universe_parameter``'s schema default, used
#: when a run has no persisted selection to name its own key.
DEFAULT_UNIVERSE_PARAMETER = "security_ids"


def tuning_parameters(
    parameters: dict[str, object], universe_parameter: str | None = None
) -> dict[str, object] | None:
    """Return ``parameters`` minus the universe-selection keys (gh-434) --
    the display-only tuning-knob view for the results list and Result
    page. ``universe_parameter`` names the run's own universe key when a
    persisted selection exists; :data:`UNIVERSE_PARAMETER_KEYS` (the
    default key and the legacy alias) are always excluded. ``None`` means
    the run had no parameters at all (renders as "(defaults)"); an empty
    dict means every parameter was a universe key (renders as
    "(universe selection only)"). ``_parameter_summary``'s output
    contract is untouched."""
    if not parameters:
        return None
    excluded = set(UNIVERSE_PARAMETER_KEYS)
    if universe_parameter:
        excluded.add(universe_parameter)
    return {key: value for key, value in parameters.items() if key not in excluded}


def _parse_universe_selection(
    selection_json: object,
) -> tuple[tuple[str, ...] | None, str]:
    """Parse a persisted ``strategy_runs.selection_json`` value into
    ``(canonical_security_ids, universe_parameter)``.

    A legacy NULL -- or stored JSON that no longer validates against
    :class:`RunUniverseSelectionV1` -- degrades to ``(None,
    :data:`DEFAULT_UNIVERSE_PARAMETER`)`` so the display layer renders a
    placeholder instead of raising; nothing is ever rewritten."""
    if selection_json is None:
        return None, DEFAULT_UNIVERSE_PARAMETER
    try:
        selection = RunUniverseSelectionV1.model_validate_json(str(selection_json))
    except ValidationError:
        return None, DEFAULT_UNIVERSE_PARAMETER
    return selection.canonical_security_ids, selection.universe_parameter


def _row_to_strategy_job(row: sqlite3.Row | tuple[object, ...]) -> StrategyJobV1:
    return StrategyJobV1(
        id=str(row[0]),
        job_type=StrategyJobType(str(row[1])),
        status=StrategyJobStatus(str(row[2])),
        parent_job_id=None if row[3] is None else str(row[3]),
        enqueue_seq=int(str(row[4])),
        claim_token=None if row[5] is None else str(row[5]),
        current_month=None if row[6] is None else str(row[6]),
        current_stage=None if row[7] is None else str(row[7]),
        owner_instance_id=None if row[8] is None else str(row[8]),
        lease_generation=None if row[9] is None else int(str(row[9])),
        status_version=int(str(row[10])),
        cancel_requested_at=_optional_instant(row[11]),
        failure_code=(None if row[12] is None else JobFailureCode(str(row[12]))),
        failed_month=None if row[13] is None else str(row[13]),
        failure_detail=None if row[14] is None else str(row[14]),
        deleted_at=_optional_instant(row[15]),
        audit_summary=None if row[16] is None else str(row[16]),
        created_at=datetime.fromisoformat(str(row[17])),
        updated_at=datetime.fromisoformat(str(row[18])),
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
        mode="update" if len(row) > 9 and str(row[9]) == "update" else "rebuild",
    )


def _migrate_bats_mic_constraints(conn: sqlite3.Connection) -> None:
    """Expand legacy closed-MIC CHECK constraints without losing evidence."""
    legacy_constraint = "('XNAS', 'XNYS', 'XLON')"
    expanded_constraint = "('BATS', 'XNAS', 'XNYS', 'XLON')"
    tables = (
        "snapshot_members",
        "security_alias_entries",
        "security_identities",
    )
    pending: list[tuple[str, str]] = []
    stale_replacements: list[str] = []
    for table in tables:
        replacement = f"{table}__bats_migration"
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (replacement,),
            ).fetchone()
            is not None
        ):
            stale_replacements.append(replacement)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is not None and legacy_constraint in str(row[0]):
            pending.append((table, str(row[0])))
    if not pending and not stale_replacements:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for replacement in stale_replacements:
            conn.execute(f'DROP TABLE "{replacement}"')
        for table, table_sql in pending:
            replacement = f"{table}__bats_migration"
            triggers = tuple(
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type='trigger' AND sql IS NOT NULL
                         AND (tbl_name=? OR instr(sql, ?) > 0)""",
                    (table, table),
                ).fetchall()
            )
            columns = tuple(
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            if f"CREATE TABLE IF NOT EXISTS {table}" in table_sql:
                create_sql = table_sql.replace(
                    f"CREATE TABLE IF NOT EXISTS {table}",
                    f"CREATE TABLE {replacement}",
                    1,
                )
            else:
                create_sql = table_sql.replace(
                    f"CREATE TABLE {table}", f"CREATE TABLE {replacement}", 1
                )
            create_sql = create_sql.replace(legacy_constraint, expanded_constraint)
            conn.execute(create_sql)
            rendered_columns = ", ".join(f'"{column}"' for column in columns)
            conn.execute(
                f'INSERT INTO "{replacement}" ({rendered_columns}) '
                f'SELECT {rendered_columns} FROM "{table}"'
            )
            for trigger_name, _trigger_sql in triggers:
                conn.execute(f'DROP TRIGGER "{trigger_name}"')
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
            for _trigger_name, trigger_sql in triggers:
                conn.execute(trigger_sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError("BATS MIC migration violated foreign keys")


def _migrate_snapshot_exclusion_constraints(conn: sqlite3.Connection) -> None:
    """Expand the closed legitimate-exclusion vocabulary without data loss."""
    table = "snapshot_members"
    replacement = f"{table}__exclusion_migration"
    legacy_column = (
        "exclusion_reason TEXT CHECK(exclusion_reason IS NULL OR "
        "exclusion_reason = 'before_first_provider_observation')"
    )
    expanded_values = (
        "'before_first_provider_observation', "
        "'insufficient_detector_history', "
        "'incomplete_detector_history'"
    )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    stale = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (replacement,)
    ).fetchone()
    if (row is None or legacy_column not in str(row[0])) and stale is None:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if stale is not None:
            conn.execute(f'DROP TABLE "{replacement}"')
        if row is not None and legacy_column in str(row[0]):
            table_sql = str(row[0])
            triggers = tuple(
                (str(item[0]), str(item[1]))
                for item in conn.execute(
                    """SELECT name, sql FROM sqlite_master
                       WHERE type='trigger' AND sql IS NOT NULL
                         AND (tbl_name=? OR instr(sql, ?) > 0)""",
                    (table, table),
                ).fetchall()
            )
            columns = tuple(
                str(item[1])
                for item in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            create_sql = table_sql.replace(
                f'CREATE TABLE "{table}"', f'CREATE TABLE "{replacement}"', 1
            ).replace(f"CREATE TABLE {table}", f"CREATE TABLE {replacement}", 1)
            create_sql = create_sql.replace(
                legacy_column,
                "exclusion_reason TEXT CHECK(exclusion_reason IS NULL OR "
                f"exclusion_reason IN ({expanded_values}))",
            ).replace(
                "exclusion_reason = 'before_first_provider_observation'",
                f"exclusion_reason IN ({expanded_values})",
            )
            conn.execute(create_sql)
            rendered = ", ".join(f'"{column}"' for column in columns)
            conn.execute(
                f'INSERT INTO "{replacement}" ({rendered}) '
                f'SELECT {rendered} FROM "{table}"'
            )
            for trigger_name, _trigger_sql in triggers:
                conn.execute(f'DROP TRIGGER "{trigger_name}"')
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
            for _trigger_name, trigger_sql in triggers:
                conn.execute(trigger_sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError(
            "snapshot exclusion migration violated foreign keys"
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
        self._connect = self._backtest_connection(connect)
        self._clock = clock
        self._instant_clock = instant_clock
        self._id_generator = id_generator
        self._token_generator = token_generator
        # Coverage summaries are process-local projections of immutable evidence.
        # The lock also makes miss/verify/publish one operation for callers that
        # share a repository instance.
        self._snapshot_coverage_lock = RLock()
        self._snapshot_coverage_cache: dict[str, tuple[str, CoverageSummaryV1]] = {}
        self._snapshot_coverage_cache_limit = 16

    @staticmethod
    def _backtest_connection(connect: Connect) -> Connect:
        """Return a fresh backtest connection with bounded lock waiting.

        ``Connect`` deliberately returns a new connection for each repository
        operation, so the pragma must be applied here rather than only during
        schema setup.  SQLite scopes ``busy_timeout`` to the connection.
        """

        def open_connection() -> sqlite3.Connection:
            conn = connect()
            conn.execute(f"PRAGMA busy_timeout = {BACKTEST_SQLITE_BUSY_TIMEOUT_MS}")
            return conn

        return open_connection

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            # WAL lets Strategy Manager's read-heavy tab rendering proceed
            # alongside the orchestrator's short lease/write transactions.
            # The mode is durable database state, but issuing it here also
            # upgrades existing rollback-journal databases at startup.
            conn.execute("PRAGMA journal_mode = WAL")
            # ``executescript`` otherwise commits before running and permits
            # two startup processes to interleave a trigger DROP/CREATE pair.
            # Keep each schema phase under SQLite's cross-process write lock.
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + _QUALIFICATION_SCHEMA
                + _ROSTER_SCHEMA
                + _SCAN_RECONSTRUCTION_CACHE_SCHEMA
                + _SNAPSHOT_COVERAGE_SCHEMA
                + _BAU_RUN_AUTHORITY_SCHEMA
                + _STRATEGY_JOB_SCHEMA
                + _BACKTEST_RESULT_SCHEMA
                + "\nCOMMIT;"
            )
            _migrate_bats_mic_constraints(conn)
            _migrate_snapshot_exclusion_constraints(conn)
            conn.execute("BEGIN IMMEDIATE")
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
            prep_cols = {
                str(x[1]) for x in conn.execute("PRAGMA table_info(preparation_runs)")
            }
            for definition in (
                "selection_json TEXT",
                "strategy_id TEXT",
                "strategy_api_version INTEGER",
                "strategy_source_digest TEXT",
                "parameters_json TEXT",
                "start_month TEXT",
                "end_month TEXT",
                "base_currency TEXT",
                "starting_capital TEXT",
            ):
                if definition.split()[0] not in prep_cols:
                    conn.execute(
                        f"ALTER TABLE preparation_runs ADD COLUMN {definition}"
                    )
            manifest_cols = {
                str(x[1])
                for x in conn.execute("PRAGMA table_info(run_input_manifests)")
            }
            if "manifest_version" not in manifest_cols:
                conn.execute(
                    "ALTER TABLE run_input_manifests ADD COLUMN manifest_version TEXT NOT NULL DEFAULT 'run_input_manifest.v1'"
                )
            run_cols = {
                str(x[1]) for x in conn.execute("PRAGMA table_info(strategy_runs)")
            }
            for definition in (
                "manifest_version TEXT NOT NULL DEFAULT 'run_input_manifest.v1'",
                "run_universe_digest TEXT",
                "source_preparation_job_id TEXT",
                "selection_json TEXT",
            ):
                if definition.split()[0] not in run_cols:
                    conn.execute(f"ALTER TABLE strategy_runs ADD COLUMN {definition}")
            result_cols = {
                str(x[1]) for x in conn.execute("PRAGMA table_info(backtest_results)")
            }
            if "result_schema_version" not in result_cols:
                conn.execute(
                    "ALTER TABLE backtest_results ADD COLUMN result_schema_version "
                    "TEXT NOT NULL DEFAULT 'backtest_result.v1'"
                )
            # gh-468: additive adoption provenance on committed months and the
            # Update/Rebuild choice on initialization runs. Both are nullable
            # / defaulted so pre-existing databases migrate in place.
            month_cols = {
                str(x[1]) for x in conn.execute("PRAGMA table_info(snapshot_months)")
            }
            if "adopted_from_profile_hash" not in month_cols:
                conn.execute(
                    "ALTER TABLE snapshot_months ADD COLUMN adopted_from_profile_hash TEXT"
                )
            init_cols = {
                str(x[1])
                for x in conn.execute("PRAGMA table_info(initialization_runs)")
            }
            if "mode" not in init_cols:
                conn.execute(
                    "ALTER TABLE initialization_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'rebuild'"
                )
            # ``CREATE TRIGGER IF NOT EXISTS`` in the schema script does not
            # replace a pre-selection trigger on an existing database. Rebuild
            # this one after its additive column migration so legacy and fresh
            # stores enforce the same immutable evidence contract.
            conn.execute("DROP TRIGGER IF EXISTS backtest_result_evidence_immutable")
            conn.execute(
                """CREATE TRIGGER backtest_result_evidence_immutable
                   BEFORE UPDATE ON backtest_results
                   WHEN NEW.run_id != OLD.run_id
                     OR NEW.result_schema_version != OLD.result_schema_version
                     OR NEW.metrics_json != OLD.metrics_json
                     OR NEW.final_cash_base != OLD.final_cash_base
                     OR NEW.result_digest != OLD.result_digest
                     OR NEW.completed_at != OLD.completed_at
                   BEGIN SELECT RAISE(ABORT, 'backtest result evidence is immutable'); END"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_strategy_runs_source_preparation "
                "ON strategy_runs(source_preparation_job_id) "
                "WHERE source_preparation_job_id IS NOT NULL"
            )
            conn.execute("DROP TRIGGER IF EXISTS strategy_run_v2_contract_insert")
            conn.execute(
                """CREATE TRIGGER strategy_run_v2_contract_insert
                   BEFORE INSERT ON strategy_runs
                   WHEN NOT EXISTS(
                       SELECT 1 FROM run_input_manifests m
                       WHERE m.digest=NEW.run_input_manifest_digest
                         AND m.manifest_version=NEW.manifest_version
                   ) OR NOT (
                       (NEW.manifest_version='run_input_manifest.v1'
                        AND NEW.run_universe_digest IS NULL
                        AND NEW.source_preparation_job_id IS NULL
                        AND NEW.selection_json IS NULL)
                       OR
                       (NEW.manifest_version='run_input_manifest.v2'
                        AND length(NEW.run_universe_digest)=64
                        AND NEW.selection_json IS NOT NULL
                        AND EXISTS(
                            SELECT 1 FROM strategy_jobs j
                            WHERE j.id=NEW.id AND (
                                (NEW.source_preparation_job_id IS NOT NULL
                                 AND j.parent_job_id IS NULL)
                                OR
                                (NEW.source_preparation_job_id IS NULL
                                 AND j.parent_job_id IS NOT NULL)
                            )
                        ))
                   )
                   BEGIN
                       SELECT RAISE(
                           ABORT, 'strategy run version provenance mismatch'
                       );
                   END"""
            )
            existing = conn.execute(
                """SELECT id FROM strategy_jobs
                   WHERE id NOT IN (SELECT job_id FROM notification_outbox)
                   ORDER BY enqueue_seq"""
            ).fetchall()
            for row in existing:
                self._upsert_notification_outbox_on_connection(
                    conn, self._load_strategy_job(conn, str(row[0]))
                )

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

    def roster_alias_revision(self, roster_digest: str) -> str | None:
        """Return the immutable alias revision one captured roster pins.

        Read-only lookup ``RunInputManifestV1`` (Story 2.3) needs to pin
        the single alias revision a Run's whole security universe was
        resolved under -- distinct from each security's own price/action
        evidence revision.
        """
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT alias_revision FROM reconstruction_rosters WHERE roster_digest=?",
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

    def roster_member_identities(
        self, profile_hash: str
    ) -> list[tuple[str, str, str, str]]:
        """Return roster member identities for one profile's universe.

        Joins ``snapshot_profiles`` → ``reconstruction_roster_members``
        → ``security_identities`` to return
        ``(security_id, provider_symbol, mic, quote_currency)`` tuples
        sorted by ``(provider_symbol, mic)`` for deterministic display.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT member.security_id, member.provider_symbol,
                          member.mic, member.currency
                   FROM reconstruction_roster_members member
                   JOIN snapshot_profiles profile
                     ON profile.roster_digest = member.roster_digest
                  WHERE profile.profile_hash = ?
                  ORDER BY member.provider_symbol, member.mic""",
                (profile_hash,),
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    def recent_job_failures(self, limit: int = 5) -> tuple[RecentJobFailureV1, ...]:
        """Return bounded recent failed/cancelled jobs for diagnostics.

        Queries ``strategy_jobs`` for ``status IN ('failed', 'cancelled')``
        ordered by ``updated_at DESC``, limited to ``limit`` entries.
        Maps each to :class:`RecentJobFailureV1` with a recovery action
        based on job type.
        """
        _RECOVERY_BY_TYPE: dict[StrategyJobType, RecoveryAction] = {
            StrategyJobType.BOOTSTRAP: RecoveryAction.SET_UP,
            StrategyJobType.INITIALIZATION: RecoveryAction.INITIALIZE,
            StrategyJobType.PREPARATION: RecoveryAction.CONFIGURE,
            StrategyJobType.BACKTEST: RecoveryAction.RETRY,
        }
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT id, job_type, status, current_stage,
                           current_month, failure_code, updated_at
                    FROM strategy_jobs
                    WHERE status IN ('failed', 'cancelled')
                      AND deleted_at IS NULL
                    ORDER BY updated_at DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        results: list[RecentJobFailureV1] = []
        for row in rows:
            job_type = StrategyJobType(str(row[1]))
            status = StrategyJobStatus(str(row[2]))
            stage_or_month = (
                str(row[3])
                if row[3] is not None
                else (str(row[4]) if row[4] is not None else None)
            )
            if status is StrategyJobStatus.FAILED:
                try:
                    failure_code = JobFailureCode(str(row[5]))
                except ValueError:
                    failure_code = JobFailureCode.INTEGRITY_ERROR
                recovery = _RECOVERY_BY_TYPE.get(job_type, RecoveryAction.RETRY)
            else:
                failure_code = JobFailureCode.WORKER_INTERRUPTED
                recovery = RecoveryAction.RECONCILE_WORKER
            results.append(
                RecentJobFailureV1(
                    job_id=str(row[0]),
                    job_type=job_type,
                    failure_code=failure_code,
                    stage_or_month=stage_or_month,
                    failed_at=datetime.fromisoformat(str(row[6])),
                    recovery_action=recovery,
                )
            )
        return tuple(results)

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
        mode: str = "rebuild",
    ) -> InitializationEnqueueResultV1:
        """Atomically enqueue one initialization, or return a verified no-op.

        ``mode`` (gh-468) selects Update (adopt unchanged members from the
        predecessor data version) or Rebuild; it is part of the run's
        requested-month digest so restart/replay is deterministic.
        """
        if mode not in {"update", "rebuild"}:
            raise StrategyJobConflict("initialization mode is invalid")
        months = TradingCalendar.months_inclusive(requested_start, requested_end)
        for month in months:
            TradingCalendar.closed_month(month, as_of=self._clock())
        now = self._job_now()
        rendered_months = json.dumps(list(months), separators=(",", ":"))
        month_digest = requested_month_digest(
            profile_hash, months, calendar_dataset_version, mode=mode
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
                           ordered_month_digest, mode
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        job_id,
                        profile_hash,
                        requested_start,
                        requested_end,
                        rendered_months,
                        month_digest,
                        calendar_dataset_version,
                        qualification_contract_digest,
                        mode,
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

    def create_bootstrap_job(
        self,
        submission: BootstrapSubmissionV1,
        *,
        allow_active_refresh: bool = False,
    ) -> BootstrapEnqueueResultV1:
        """Atomically no-op, create, or durably replay one Bootstrap activity.

        Replay lookup, active-profile no-op, competing-request policy, and the
        job/subtype/action/outbox writes share one immediate transaction.
        """
        now = self._job_now()
        content_digest = submission.canonical_content_digest()
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """SELECT job_id, submission_digest
                       FROM bootstrap_enqueue_actions WHERE idempotency_key=?""",
                    (submission.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if str(existing[1]) != content_digest:
                        raise StrategyJobConflict(
                            "idempotency key was already used for a different bootstrap submission"
                        )
                    job = self._load_bootstrap_submission_job(conn, str(existing[0]))
                    return BootstrapEnqueueResultV1(
                        no_op=False,
                        job=job,
                        bootstrap=self._load_bootstrap(conn, job.id),
                    )
                active = conn.execute(
                    """SELECT 1 FROM active_snapshot_profile
                       WHERE singleton_id=1 AND profile_hash IS NOT NULL"""
                ).fetchone()
                if active is not None and not allow_active_refresh:
                    return BootstrapEnqueueResultV1(no_op=True)
                competing = conn.execute(
                    """SELECT 1 FROM strategy_jobs
                       WHERE job_type='bootstrap' AND status IN ('queued', 'running')
                         AND deleted_at IS NULL"""
                ).fetchone()
                if competing is not None:
                    raise StrategyJobConflict(
                        "a bootstrap job is already queued or running"
                    )
                if (
                    submission.parent_job_id is not None
                    and conn.execute(
                        "SELECT 1 FROM strategy_jobs WHERE id=?",
                        (submission.parent_job_id,),
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
                           claim_token, current_month, current_stage,
                           owner_instance_id, lease_generation, status_version,
                           cancel_requested_at, failure_code, failed_month,
                           failure_detail, deleted_at, audit_summary,
                           created_at, updated_at
                       ) VALUES (?, 'bootstrap', 'queued', ?, ?, NULL, NULL, NULL,
                                 NULL, NULL, 1, NULL, NULL, NULL, NULL, NULL, NULL,
                                 ?, ?)""",
                    (job_id, submission.parent_job_id, enqueue_seq, now, now),
                )
                conn.execute(
                    "INSERT INTO bootstrap_runs (job_id) VALUES (?)", (job_id,)
                )
                conn.execute(
                    """INSERT INTO bootstrap_enqueue_actions
                       (idempotency_key, job_id, submission_digest, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (submission.idempotency_key, job_id, content_digest, now),
                )
                job = self._load_strategy_job(conn, job_id)
                bootstrap = self._load_bootstrap(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
            return BootstrapEnqueueResultV1(no_op=False, job=job, bootstrap=bootstrap)
        except (StrategyJobConflict, StrategyJobNotFound):
            raise
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict("bootstrap job creation conflicted") from exc

    def _load_bootstrap_submission_job(
        self, conn: sqlite3.Connection, job_id: str
    ) -> StrategyJobV1:
        try:
            job = self._load_strategy_job(conn, job_id)
            if job.deleted_at is not None:
                raise StrategyJobConflict(
                    "the original bootstrap activity is no longer available"
                )
            if job.job_type is not StrategyJobType.BOOTSTRAP:
                raise BacktestIntegrityError(
                    "bootstrap submission references a non-bootstrap job"
                )
            self._load_bootstrap(conn, job_id)
            return job
        except StrategyJobNotFound as exc:
            raise BacktestIntegrityError(
                "stored bootstrap submission is unavailable"
            ) from exc

    @overload
    def create_preparation_job(
        self, submission: None = None, *, parent_job_id: str | None = None
    ) -> StrategyJobV1: ...
    @overload
    def create_preparation_job(
        self, submission: PreparationSubmissionV1, *, parent_job_id: str | None = None
    ) -> PreparationEnqueueResultV1: ...
    def create_preparation_job(
        self,
        submission: PreparationSubmissionV1 | None = None,
        *,
        parent_job_id: str | None = None,
    ) -> StrategyJobV1 | PreparationEnqueueResultV1:
        if submission is None:
            return self._create_stage_job(StrategyJobType.PREPARATION, parent_job_id)
        if parent_job_id is not None and parent_job_id != submission.parent_job_id:
            raise StrategyJobConflict("preparation parent lineage mismatch")
        parent_job_id = submission.parent_job_id
        now = self._job_now()
        digest = submission.content_digest()
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                replay = conn.execute(
                    "SELECT submission_digest,job_id FROM preparation_enqueue_actions WHERE idempotency_key=?",
                    (submission.idempotency_key,),
                ).fetchone()
                if replay:
                    if str(replay[0]) != digest:
                        raise StrategyJobConflict(
                            "idempotency key was used for a different preparation"
                        )
                    job = self._load_strategy_job(conn, str(replay[1]))
                    if job.deleted_at is not None:
                        raise StrategyJobConflict(
                            "preparation replay target is unavailable"
                        )
                    return PreparationEnqueueResultV1(
                        job=job, preparation=self._load_preparation(conn, job.id)
                    )
                active = conn.execute(
                    "SELECT profile_hash,activation_seq FROM active_snapshot_profile WHERE singleton_id=1"
                ).fetchone()
                if active is None or (str(active[0]), int(active[1])) != (
                    submission.selection.profile_hash,
                    submission.selection.activation_seq,
                ):
                    raise StrategyJobConflict("selected universe is stale")
                roster = {
                    str(x[0])
                    for x in conn.execute(
                        """SELECT m.security_id FROM snapshot_profiles p JOIN reconstruction_roster_members m ON m.roster_digest=p.roster_digest WHERE p.profile_hash=?""",
                        (submission.selection.profile_hash,),
                    )
                }
                if not set(submission.selection.canonical_security_ids) <= roster:
                    raise StrategyJobConflict(
                        "selected universe is not in active roster"
                    )
                seq = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM strategy_jobs"
                    ).fetchone()[0]
                )
                job_id = self._id_generator()
                conn.execute(
                    """INSERT INTO strategy_jobs(id,job_type,status,parent_job_id,enqueue_seq,claim_token,current_month,current_stage,owner_instance_id,lease_generation,status_version,cancel_requested_at,failure_code,failed_month,failure_detail,deleted_at,audit_summary,created_at,updated_at) VALUES(?,'preparation','queued',?,?,NULL,NULL,NULL,NULL,NULL,1,NULL,NULL,NULL,NULL,NULL,NULL,?,?)""",
                    (job_id, parent_job_id, seq, now, now),
                )
                conn.execute(
                    """INSERT INTO preparation_runs(job_id,selection_json,strategy_id,strategy_api_version,strategy_source_digest,parameters_json,start_month,end_month,base_currency,starting_capital) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        submission.selection.model_dump_json(),
                        submission.strategy_id,
                        submission.strategy_api_version,
                        submission.strategy_source_digest,
                        json.dumps(
                            dict(submission.parameters),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        submission.start_month,
                        submission.end_month,
                        submission.base_currency,
                        str(submission.starting_capital),
                    ),
                )
                conn.execute(
                    "INSERT INTO preparation_enqueue_actions VALUES(?,?,?,?)",
                    (submission.idempotency_key, digest, job_id, now),
                )
                job = self._load_strategy_job(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
                return PreparationEnqueueResultV1(
                    job=job, preparation=self._load_preparation(conn, job_id)
                )
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict("preparation job creation conflicted") from exc

    def _create_stage_job(
        self, job_type: StrategyJobType, parent_job_id: str | None
    ) -> StrategyJobV1:
        now = self._job_now()
        table, _ = _SUBTYPE_TABLES[job_type]
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
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
                           claim_token, current_month, current_stage,
                           owner_instance_id, lease_generation, status_version,
                           cancel_requested_at, failure_code, failed_month,
                           failure_detail, deleted_at, audit_summary,
                           created_at, updated_at
                       ) VALUES (?, ?, 'queued', ?, ?, NULL, NULL, NULL, NULL, NULL,
                                 1, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                    (job_id, job_type.value, parent_job_id, enqueue_seq, now, now),
                )
                conn.execute(f"INSERT INTO {table} (job_id) VALUES (?)", (job_id,))
                job = self._load_strategy_job(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
            return job
        except (StrategyJobConflict, StrategyJobNotFound):
            raise
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict(
                f"{job_type.value} job creation conflicted"
            ) from exc

    # -- Story 2.6: Backtest atomic enqueue -------------------------------

    @staticmethod
    def _backtest_submission_content_digest(submission: BacktestSubmissionV1) -> str:
        """Canonical content hash of one submission (minus its own
        ``idempotency_key``), used to detect a replayed key whose
        submission has actually diverged (Story 2.6 review) -- an
        idempotency-key replay must return the exact already-committed
        attempt, never silently paper over a different submission."""
        payload = submission.model_dump(mode="python", exclude={"idempotency_key"})
        payload["starting_capital"] = str(submission.starting_capital)
        return manifest_digest(payload)

    def create_backtest_job(
        self, submission: BacktestSubmissionV1
    ) -> BacktestEnqueueResultV1:
        """Atomically enqueue one Backtest attempt (AC 1).

        One ``BEGIN IMMEDIATE`` transaction revalidates the active
        snapshot profile and the exact contiguous normalized month
        sequence, recomputing ``ordered_month_digest`` fresh from live
        coverage rather than trusting any caller-supplied value, then
        persists exactly one queued ``backtest`` ``strategy_jobs`` row,
        its immutable ``strategy_runs`` identity, and a content-addressed
        ``run_input_manifests`` binding -- reused (never duplicated) when
        an identical digest already exists. An explicit
        ``idempotency_key`` retrying the identical submission returns the
        already-committed attempt unchanged and creates nothing; omitting
        it, or supplying a distinct key, always creates a distinct
        attempt, even for otherwise-identical parameters. A key replayed
        against a submission whose content has diverged from the one it
        was first committed with is rejected rather than silently
        returning the stale attempt.
        """
        from app.services.backtest.run_input_manifest import (
            RunInputManifestV1,
            read_run_input_manifest,
        )

        if submission.manifest_version != "run_input_manifest.v1":
            raise StrategyJobConflict("V2 backtests require preparation seal")
        if submission.canonical_manifest_json != "{}":
            try:
                parsed = read_run_input_manifest(submission.canonical_manifest_json)
            except Exception as exc:
                raise StrategyJobConflict("run input manifest is invalid") from exc
            if (
                not isinstance(parsed, RunInputManifestV1)
                or parsed.schema_version != "run_input_manifest.v1"
                or parsed.digest() != submission.run_input_manifest_digest
            ):
                raise StrategyJobConflict("run input manifest version is invalid")
        now = self._job_now()
        content_digest = self._backtest_submission_content_digest(submission)
        try:
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if submission.idempotency_key is not None:
                    existing = conn.execute(
                        """SELECT job_id, submission_digest FROM backtest_enqueue_actions
                           WHERE idempotency_key=?""",
                        (submission.idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        if str(existing[1]) != content_digest:
                            raise StrategyJobConflict(
                                "idempotency key was already used for a "
                                "different backtest submission"
                            )
                        existing_id = str(existing[0])
                        return BacktestEnqueueResultV1(
                            job=self._load_strategy_job(conn, existing_id),
                            backtest=self._load_strategy_run(conn, existing_id),
                        )
                profile = conn.execute(
                    "SELECT 1 FROM snapshot_profiles WHERE profile_hash=?",
                    (submission.profile_hash,),
                ).fetchone()
                if profile is None:
                    raise StrategyJobConflict("snapshot profile does not exist")
                active = conn.execute(
                    "SELECT profile_hash FROM active_snapshot_profile "
                    "WHERE singleton_id=1"
                ).fetchone()
                if active is None or str(active[0]) != submission.profile_hash:
                    raise StrategyJobConflict("snapshot profile is not active")
                if submission.parent_job_id is not None:
                    try:
                        parent = self._load_strategy_job(conn, submission.parent_job_id)
                    except StrategyJobNotFound:
                        raise StrategyJobConflict(
                            "parent strategy job does not exist"
                        ) from None
                    if parent.job_type is not StrategyJobType.BACKTEST:
                        raise StrategyJobConflict(
                            "parent strategy job must be a backtest job"
                        )
                    if parent.status not in {
                        StrategyJobStatus.FAILED,
                        StrategyJobStatus.CANCELLED,
                    }:
                        raise StrategyJobConflict(
                            "parent strategy job must be terminal"
                        )
                    if parent.deleted_at is not None:
                        raise StrategyJobConflict(
                            "deleted strategy job cannot be a parent"
                        )
                readiness = self._interval_readiness_on_connection(
                    conn,
                    submission.profile_hash,
                    submission.start_month,
                    submission.end_month,
                )
                if not readiness.ready or readiness.ordered_month_digest is None:
                    raise StrategyJobConflict("snapshot coverage is not Ready")
                if (
                    manifest_digest(json.loads(submission.canonical_manifest_json))
                    != submission.run_input_manifest_digest
                ):
                    raise StrategyJobConflict(
                        "run input manifest digest does not match its "
                        "canonical manifest"
                    )
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
                       ) VALUES (?, 'backtest', 'queued', ?, ?, NULL, NULL, 1,
                                 NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                    (job_id, submission.parent_job_id, enqueue_seq, now, now),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO run_input_manifests (
                           digest, execution_contract_digest,
                           canonical_manifest_json, created_at, manifest_version
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        submission.run_input_manifest_digest,
                        submission.execution_contract_digest,
                        submission.canonical_manifest_json,
                        now,
                        submission.manifest_version,
                    ),
                )
                conn.execute(
                    """INSERT INTO strategy_runs (
                           id, strategy_id, strategy_api_version,
                           strategy_source_digest, parameters_json, profile_hash,
                           start_month, end_month, ordered_month_digest,
                           base_currency, starting_capital,
                           run_input_manifest_digest, execution_contract_digest,
                           manifest_version,run_universe_digest,source_preparation_job_id,selection_json,created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        submission.strategy_id,
                        submission.strategy_api_version,
                        submission.strategy_source_digest,
                        json.dumps(
                            dict(submission.parameters),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        submission.profile_hash,
                        submission.start_month,
                        submission.end_month,
                        readiness.ordered_month_digest,
                        submission.base_currency,
                        str(submission.starting_capital),
                        submission.run_input_manifest_digest,
                        submission.execution_contract_digest,
                        submission.manifest_version,
                        None,
                        None,
                        None,
                        now,
                    ),
                )
                if submission.idempotency_key is not None:
                    conn.execute(
                        """INSERT INTO backtest_enqueue_actions
                           (idempotency_key, job_id, submission_digest, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (submission.idempotency_key, job_id, content_digest, now),
                    )
                job = self._load_strategy_job(conn, job_id)
                backtest = self._load_strategy_run(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
            return BacktestEnqueueResultV1(job=job, backtest=backtest)
        except (StrategyJobConflict, StrategyJobNotFound):
            raise
        except sqlite3.IntegrityError as exc:
            raise StrategyJobConflict("backtest job creation conflicted") from exc

    def seal_preparation_and_create_backtest(
        self,
        prep_id: str,
        token: str,
        *,
        expected_version: int,
        submission: BacktestSubmissionV1,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> BacktestEnqueueResultV1:
        from app.services.backtest.run_input_manifest import (
            RunInputManifestV2,
            read_run_input_manifest,
        )

        if (
            submission.manifest_version != "run_input_manifest.v2"
            or submission.source_preparation_job_id != prep_id
        ):
            raise StrategyJobConflict("invalid V2 seal")
        now = self._job_now()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, prep_id)
            prep = self._load_preparation(conn, prep_id)
            existing = conn.execute(
                "SELECT id FROM strategy_runs WHERE source_preparation_job_id=?",
                (prep_id,),
            ).fetchone()
            if existing and job.status is StrategyJobStatus.COMPLETE:
                cid = str(existing[0])
                return BacktestEnqueueResultV1(
                    job=self._load_strategy_job(conn, cid),
                    backtest=self._load_strategy_run(conn, cid),
                )
            if (
                job.status is not StrategyJobStatus.RUNNING
                or job.claim_token != token
                or job.status_version != expected_version
                or job.cancel_requested_at
                or prep.selection is None
            ):
                raise StrategyJobConflict("preparation ownership is stale")
            try:
                manifest = read_run_input_manifest(submission.canonical_manifest_json)
            except Exception as exc:
                raise StrategyJobConflict("sealed manifest is invalid") from exc
            s = prep.selection
            ok = (
                isinstance(manifest, RunInputManifestV2)
                and submission.strategy_id == prep.strategy_id == manifest.strategy_id
                and submission.strategy_api_version
                == prep.strategy_api_version
                == manifest.strategy_api_version
                and submission.strategy_source_digest
                == prep.strategy_source_digest
                == manifest.strategy_source_digest
                and dict(submission.parameters)
                == dict(prep.parameters)
                == dict(manifest.parameters)
                and submission.profile_hash == s.profile_hash == manifest.profile_hash
                and submission.start_month == prep.start_month == manifest.start_month
                and submission.end_month == prep.end_month == manifest.end_month
                and submission.base_currency
                == prep.base_currency
                == manifest.base_currency
                and submission.starting_capital
                == prep.starting_capital
                == manifest.starting_capital
                and submission.universe_selection == s == manifest.universe_selection
                and manifest.source_preparation_job_id == prep_id
                and manifest.digest() == submission.run_input_manifest_digest
                and submission.execution_contract_digest
                == manifest.execution_contract_digest()
            )
            if not ok:
                raise StrategyJobConflict("preparation seal identity mismatch")
            active = conn.execute(
                "SELECT profile_hash,activation_seq FROM active_snapshot_profile WHERE singleton_id=1"
            ).fetchone()
            roster = {
                str(x[0])
                for x in conn.execute(
                    "SELECT m.security_id FROM snapshot_profiles p JOIN reconstruction_roster_members m ON m.roster_digest=p.roster_digest WHERE p.profile_hash=?",
                    (s.profile_hash,),
                )
            }
            if (
                active is None
                or (str(active[0]), int(active[1]))
                != (s.profile_hash, s.activation_seq)
                or not set(s.canonical_security_ids) <= roster
                or tuple(sorted(x.security_id for x in manifest.securities))
                != s.canonical_security_ids
            ):
                raise StrategyJobConflict("selected universe is stale")
            ready = self._interval_readiness_on_connection(
                conn, s.profile_hash, manifest.start_month, manifest.end_month
            )
            if (
                not ready.ready
                or ready.ordered_month_digest != manifest.ordered_month_digest
            ):
                raise StrategyJobConflict("selected evidence is stale")
            cid = self._id_generator()
            seq = int(
                conn.execute(
                    "SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM strategy_jobs"
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO strategy_jobs(id,job_type,status,parent_job_id,enqueue_seq,claim_token,current_month,current_stage,owner_instance_id,lease_generation,status_version,cancel_requested_at,failure_code,failed_month,failure_detail,deleted_at,audit_summary,created_at,updated_at) VALUES(?,'backtest','queued',NULL,?,NULL,NULL,NULL,NULL,NULL,1,NULL,NULL,NULL,NULL,NULL,NULL,?,?)",
                (cid, seq, now, now),
            )
            conn.execute(
                "INSERT INTO run_input_manifests(digest,execution_contract_digest,canonical_manifest_json,created_at,manifest_version) VALUES(?,?,?,?,?)",
                (
                    manifest.digest(),
                    manifest.execution_contract_digest(),
                    manifest.canonical_json(),
                    now,
                    "run_input_manifest.v2",
                ),
            )
            conn.execute(
                "INSERT INTO strategy_runs(id,strategy_id,strategy_api_version,strategy_source_digest,parameters_json,profile_hash,start_month,end_month,ordered_month_digest,base_currency,starting_capital,run_input_manifest_digest,execution_contract_digest,manifest_version,run_universe_digest,source_preparation_job_id,selection_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    manifest.strategy_id,
                    manifest.strategy_api_version,
                    manifest.strategy_source_digest,
                    json.dumps(
                        dict(manifest.parameters), sort_keys=True, separators=(",", ":")
                    ),
                    manifest.profile_hash,
                    manifest.start_month,
                    manifest.end_month,
                    manifest.ordered_month_digest,
                    manifest.base_currency,
                    str(manifest.starting_capital),
                    manifest.digest(),
                    manifest.execution_contract_digest(),
                    "run_input_manifest.v2",
                    s.run_universe_digest,
                    prep_id,
                    s.model_dump_json(),
                    now,
                ),
            )
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"UPDATE strategy_jobs SET status='complete',claim_token=NULL,current_stage=NULL,owner_instance_id=NULL,lease_generation=NULL,status_version=status_version+1,updated_at=? WHERE id=? AND claim_token=? AND status_version=? {_LEASE_FENCE_SQL}",
                (now, prep_id, token, expected_version, *fence),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("preparation ownership is stale")
            done = self._load_strategy_job(conn, prep_id)
            child = self._load_strategy_job(conn, cid)
            self._upsert_notification_outbox_on_connection(conn, done)
            self._upsert_notification_outbox_on_connection(conn, child)
            return BacktestEnqueueResultV1(
                job=child, backtest=self._load_strategy_run(conn, cid)
            )

    def strategy_job(self, job_id: str) -> StrategyJobV1:
        with session(self._connect) as conn:
            return self._load_strategy_job(conn, job_id)

    def initialization_run(self, job_id: str) -> InitializationRunV1:
        with session(self._connect) as conn:
            return self._load_initialization(conn, job_id)

    def bootstrap_run(self, job_id: str) -> BootstrapRunV1:
        """Return one ``bootstrap`` job's subtype identity row."""
        with session(self._connect) as conn:
            job = self._load_strategy_job(conn, job_id)
            self._require_own_subtype(conn, job, StrategyJobType.BOOTSTRAP)
            return self._load_bootstrap(conn, job_id)

    def preparation_run(self, job_id: str) -> PreparationRunV1:
        """Return one ``preparation`` job's subtype identity row."""
        with session(self._connect) as conn:
            job = self._load_strategy_job(conn, job_id)
            self._require_own_subtype(conn, job, StrategyJobType.PREPARATION)
            return self._load_preparation(conn, job_id)

    def preparation_child_backtest_id(self, job_id: str) -> str | None:
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT id FROM strategy_runs WHERE source_preparation_job_id=?",
                (job_id,),
            ).fetchone()
            return None if row is None else str(row[0])

    def strategy_run(self, job_id: str) -> BacktestRunV1:
        with session(self._connect) as conn:
            return self._load_strategy_run(conn, job_id)

    def run_input_manifest_json(self, digest: str) -> str | None:
        """Return the stored content-addressed canonical manifest JSON for
        ``digest``, or ``None`` if no such manifest has ever been bound --
        the worker's read path for the manifest ``create_backtest_job``
        pinned at enqueue time (never rebuilt or re-derived)."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT canonical_manifest_json FROM run_input_manifests WHERE digest=?",
                (digest,),
            ).fetchone()
        return None if row is None else str(row[0])

    def list_strategy_jobs(self) -> tuple[StrategyJobV1, ...]:
        with session(self._connect) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM strategy_jobs "
                "ORDER BY enqueue_seq"
            ).fetchall()
        return tuple(_row_to_strategy_job(row) for row in rows)

    def list_backtest_activities(self) -> tuple[BacktestActivitySummaryV1, ...]:
        """Return every non-tombstoned Backtest job, newest first (AC 1).

        Filters strictly to ``job_type='backtest' AND deleted_at IS NULL``,
        ordered by ``enqueue_seq DESC`` -- ``updated_at`` is never ordering
        authority (Story 2.8 Dev Notes). The parameter summary is built
        from each job's own persisted ``strategy_runs.parameters_json``
        (sorted by key, independent of whether the current Skill still
        discovers that Strategy at all) and Metrics are attached only via
        :meth:`backtest_result`'s verified-complete projection. A
        ``complete`` job whose Result is missing/malformed, or a
        non-complete job that unexpectedly has one, raises
        :class:`BacktestIntegrityError` rather than silently returning a
        partial or zero-filled row.
        """
        with session(self._connect) as conn:
            job_rows = conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM strategy_jobs "
                "WHERE job_type='backtest' AND deleted_at IS NULL "
                "ORDER BY enqueue_seq DESC"
            ).fetchall()
            jobs = tuple(_row_to_strategy_job(row) for row in job_rows)
            run_by_id: dict[str, tuple[object, ...]] = {
                str(row[0]): row
                for row in conn.execute(
                    "SELECT id, strategy_id, strategy_api_version, "
                    "parameters_json, start_month, end_month, "
                    "profile_hash, selection_json FROM strategy_runs"
                ).fetchall()
            }
            result_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT run_id FROM backtest_results"
                ).fetchall()
            }

        summaries: list[BacktestActivitySummaryV1] = []
        for job in jobs:
            run_row = run_by_id.get(job.id)
            if run_row is None:
                raise BacktestIntegrityError(
                    f"backtest job {job.id!r} has no pinned strategy run"
                )
            try:
                parameters = json.loads(str(run_row[3]))
                if not isinstance(parameters, dict):
                    raise ValueError("stored strategy run parameters are not an object")
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise BacktestIntegrityError(
                    f"backtest job {job.id!r} has invalid stored parameters"
                ) from exc
            has_result = job.id in result_ids
            is_complete = job.status is StrategyJobStatus.COMPLETE
            if is_complete != has_result:
                raise BacktestIntegrityError(
                    f"backtest job {job.id!r} Result cardinality does not "
                    "match its status"
                )
            metrics = None
            availability = None
            if is_complete:
                result = self.backtest_result(job.id)
                metrics = result.metrics
                availability = result.metric_availability
            universe_ids, universe_parameter = _parse_universe_selection(run_row[7])
            summaries.append(
                BacktestActivitySummaryV1(
                    job=job,
                    strategy_id=str(run_row[1]),
                    strategy_api_version=int(str(run_row[2])),
                    parameter_summary=_parameter_summary(parameters),
                    start_month=str(run_row[4]),
                    end_month=str(run_row[5]),
                    metrics=metrics,
                    metric_availability=availability,
                    profile_hash=str(run_row[6]),
                    universe_security_ids=universe_ids,
                    tuning_parameters=tuning_parameters(parameters, universe_parameter),
                )
            )
        return tuple(summaries)

    def latest_completed_backtest_result(self) -> BacktestResultV1 | None:
        """Return the newest durable, validated completed Backtest Result.

        Result completion time, with the immutable run ID as a deterministic
        tie-breaker, is the ordering authority.  Activity recency is not:
        queued, running, failed, cancelled, and tombstoned jobs are excluded
        before their Result is read.  Each candidate is reconstructed through
        :meth:`backtest_result`, so malformed immutable evidence is never
        returned as recall input.
        """
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT result.run_id
                   FROM backtest_results AS result
                   JOIN strategy_jobs AS job ON job.id = result.run_id
                   WHERE job.job_type='backtest'
                     AND job.status='complete'
                     AND job.deleted_at IS NULL
                   ORDER BY result.completed_at DESC, result.run_id DESC"""
            ).fetchall()
        for row in rows:
            try:
                return self.backtest_result(str(row[0]))
            except (BacktestIntegrityError, StrategyJobNotFound):
                # A damaged historical Result is not safe recall input. A
                # prior valid immutable Result may still be usable.
                continue
        return None

    def is_comparable(self, left: str, right: str) -> ComparisonEligibilityV1:
        """Return AD-19's one canonical comparison-eligibility verdict for
        two persisted Backtest Result IDs (Story 3.1 AC 1, 2, 5).

        Self-comparison is rejected before any lookup. Each side is then
        checked via the same ``strategy_jobs`` row :meth:`strategy_job`
        already reads: a missing row, a tombstoned job
        (``deleted_at IS NOT NULL``), or a non-complete Backtest job
        (queued/running/failed/cancelled, or a non-Backtest job type) is
        reported as an ordinary ``eligible=False`` outcome, never an
        error. Once both jobs are confirmed complete, their Results are
        loaded via :meth:`backtest_result` -- never re-parsed here -- and
        any :class:`BacktestIntegrityError`/:class:`StrategyJobNotFound`
        it raises for a complete job whose Result has vanished or been
        tampered with propagates uncaught, mirroring
        :meth:`list_backtest_activities`'s existing integrity boundary
        rather than swallowing it into a false ineligibility reason.
        Eligible Results are then compared on exactly the six AD-19
        dimensions (``start_month``, ``end_month``, ``profile_hash``,
        ``ordered_month_digest``, ``base_currency``,
        ``execution_contract_digest``); ``strategy_id``, ``parameters``,
        and ``starting_capital`` are never compared.

        Only the first-encountered ineligibility reason or integrity
        error is reported when both ``left`` and ``right`` are broken --
        ``left`` is always checked first. Fixing it and calling again is
        required to discover a second, independent problem on ``right``.
        """
        if left == right:
            return ComparisonEligibilityV1(
                eligible=False,
                reason=ComparisonIneligibleReason.SELF_COMPARISON,
                detail=f"{left!r} cannot be compared to itself",
            )

        for run_id in (left, right):
            reason = self._comparison_job_reason(run_id)
            if reason is not None:
                return ComparisonEligibilityV1(
                    eligible=False,
                    reason=reason,
                    detail=f"{run_id!r} is not eligible for comparison "
                    f"({reason.value})",
                )

        left_result = self.backtest_result(left)
        right_result = self.backtest_result(right)
        if left_result.manifest_version != right_result.manifest_version:
            return ComparisonEligibilityV1(
                False,
                ComparisonIneligibleReason.MANIFEST_VERSION_MISMATCH,
                "Backtests use different manifest versions",
            )
        left_selection = left_result.universe_selection
        right_selection = right_result.universe_selection
        if (
            left_result.manifest_version == "run_input_manifest.v2"
            and left_selection is not None
            and right_selection is not None
            and left_selection.run_universe_digest
            != right_selection.run_universe_digest
        ):
            return ComparisonEligibilityV1(
                False,
                ComparisonIneligibleReason.EVIDENCE_DIGEST_MISMATCH,
                "Backtests use different selected universes",
            )  # type: ignore[union-attr]
        return self._compare_eligible_results(left_result, right_result)

    def _comparison_job_reason(self, run_id: str) -> ComparisonIneligibleReason | None:
        """Return the reason ``run_id`` is not a comparable job, or
        ``None`` if it is a non-tombstoned, complete Backtest job."""
        try:
            job = self.strategy_job(run_id)
        except StrategyJobNotFound:
            return ComparisonIneligibleReason.NOT_FOUND
        if job.deleted_at is not None:
            return ComparisonIneligibleReason.TOMBSTONED
        if (
            job.job_type is not StrategyJobType.BACKTEST
            or job.status is not StrategyJobStatus.COMPLETE
        ):
            return ComparisonIneligibleReason.NOT_COMPLETE
        return None

    @staticmethod
    def _compare_eligible_results(
        left: BacktestResultV1, right: BacktestResultV1
    ) -> ComparisonEligibilityV1:
        """Compare two already-loaded complete Results on exactly AD-19's
        six dimensions, returning the first mismatch's specific reason."""
        dimensions: tuple[
            tuple[ComparisonIneligibleReason, str, object, object], ...
        ] = (
            (
                ComparisonIneligibleReason.PERIOD_MISMATCH,
                "start_month",
                left.start_month,
                right.start_month,
            ),
            (
                ComparisonIneligibleReason.PERIOD_MISMATCH,
                "end_month",
                left.end_month,
                right.end_month,
            ),
            (
                ComparisonIneligibleReason.PROFILE_MISMATCH,
                "profile_hash",
                left.profile_hash,
                right.profile_hash,
            ),
            (
                ComparisonIneligibleReason.EVIDENCE_DIGEST_MISMATCH,
                "ordered_month_digest",
                left.ordered_month_digest,
                right.ordered_month_digest,
            ),
            (
                ComparisonIneligibleReason.CURRENCY_MISMATCH,
                "base_currency",
                left.base_currency,
                right.base_currency,
            ),
            (
                ComparisonIneligibleReason.EXECUTION_CONTRACT_MISMATCH,
                "execution_contract_digest",
                left.execution_contract_digest,
                right.execution_contract_digest,
            ),
        )
        for reason, field, left_value, right_value in dimensions:
            if left_value != right_value:
                return ComparisonEligibilityV1(
                    eligible=False,
                    reason=reason,
                    detail=f"{field} differs: {left_value!r} vs {right_value!r}",
                )
        return ComparisonEligibilityV1(eligible=True, reason=None, detail="")

    def comparison_candidates(self, run_id: str) -> tuple[ComparisonCandidateV1, ...]:
        """Return every other eligible Backtest Result for ``run_id``
        (Story 3.1 AC 3), newest first.

        Loads the anchor via :meth:`backtest_result` first, propagating
        :class:`StrategyJobNotFound`/:class:`BacktestIntegrityError`
        unchanged for a missing/malformed anchor -- an ineligible
        (e.g. tombstoned) but still-loadable anchor is not itself an
        error here; it simply yields no candidates, since every pairing
        against it would fail the same job-level check below. The anchor
        is loaded exactly once and reused for every candidate comparison
        (never re-verified per candidate) via the same
        :meth:`_comparison_job_reason`/:meth:`_compare_eligible_results`
        helpers :meth:`is_comparable` itself calls -- the identical
        exhaustive predicate used at submission, never a second/
        duplicated comparison. Only eligible peers are kept, ordered
        ``enqueue_seq DESC``. No candidate is ever preselected.
        """
        anchor_result = self.backtest_result(run_id)
        anchor_reason = self._comparison_job_reason(run_id)
        if anchor_reason is not None:
            return ()

        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT job.id FROM strategy_jobs AS job
                   JOIN strategy_runs AS run ON run.id = job.id
                   WHERE job.job_type='backtest' AND job.status='complete'
                     AND job.deleted_at IS NULL AND job.id != ?
                     AND run.start_month = ? AND run.end_month = ?
                     AND run.profile_hash = ? AND run.ordered_month_digest = ?
                     AND run.base_currency = ?
                     AND run.execution_contract_digest = ?
                   ORDER BY job.enqueue_seq DESC""",
                (
                    run_id,
                    anchor_result.start_month,
                    anchor_result.end_month,
                    anchor_result.profile_hash,
                    anchor_result.ordered_month_digest,
                    anchor_result.base_currency,
                    anchor_result.execution_contract_digest,
                ),
            ).fetchall()

        candidates: list[ComparisonCandidateV1] = []
        for row in rows:
            candidate_id = str(row[0])
            if self._comparison_job_reason(candidate_id) is not None:
                continue
            candidate_result = self.backtest_result(candidate_id)
            eligibility = self._compare_eligible_results(
                anchor_result, candidate_result
            )
            if not eligibility.eligible:
                continue
            candidates.append(
                ComparisonCandidateV1(
                    run_id=candidate_result.run_id,
                    strategy_id=candidate_result.strategy_id,
                    strategy_api_version=candidate_result.strategy_api_version,
                    parameter_summary=_parameter_summary(candidate_result.parameters),
                    start_month=candidate_result.start_month,
                    end_month=candidate_result.end_month,
                    base_currency=candidate_result.base_currency,
                    profile_hash=candidate_result.profile_hash,
                )
            )
        return tuple(candidates)

    # -- Story 4.1: singleton worker lease --------------------------------

    def acquire_or_renew_worker_lease(
        self, instance_id: str, *, ttl_seconds: float
    ) -> WorkerLeaseV1:
        """Acquire, renew, or take over the singleton worker lease.

        A first acquisition starts at generation 1. The current owner
        renewing keeps its generation (a heartbeat never fences its own
        in-flight writes out). Any other instance may only take over once
        the persisted ``expires_at`` has passed, and does so at
        ``generation + 1`` -- the monotonic value every job mutation is
        compare-and-swapped against.

        Raises :class:`StrategyJobConflict` when a different instance
        still holds an unexpired lease.
        """
        if not instance_id.strip():
            raise ValueError("worker lease instance id must not be blank")
        if ttl_seconds <= 0:
            raise ValueError("worker lease ttl must be positive")
        now = self._instant_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_worker_lease(conn)
            if current is None:
                generation = 1
            elif current.instance_id == instance_id:
                generation = current.generation
            elif current.expires_at > now:
                raise StrategyJobConflict(
                    "worker lease is held by another live instance"
                )
            else:
                generation = current.generation + 1
            conn.execute(
                """INSERT INTO strategy_worker_lease (
                       singleton_id, instance_id, generation, heartbeat_at, expires_at
                   ) VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                       instance_id=excluded.instance_id,
                       generation=excluded.generation,
                       heartbeat_at=excluded.heartbeat_at,
                       expires_at=excluded.expires_at""",
                (
                    instance_id,
                    generation,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            lease = self._load_worker_lease(conn)
        if lease is None:
            raise BacktestIntegrityError("worker lease vanished after its own write")
        return lease

    def read_worker_lease(self) -> WorkerLeaseV1 | None:
        """Return the persisted lease without ever mutating it.

        Read-only by contract: inspecting readiness never renews the
        lease, extends its expiry, or bumps its generation.
        """
        with session(self._connect) as conn:
            return self._load_worker_lease(conn)

    @staticmethod
    def _load_worker_lease(conn: sqlite3.Connection) -> WorkerLeaseV1 | None:
        row = conn.execute(
            """SELECT instance_id, generation, heartbeat_at, expires_at
               FROM strategy_worker_lease WHERE singleton_id=1"""
        ).fetchone()
        if row is None:
            return None
        try:
            return WorkerLeaseV1(
                instance_id=str(row[0]),
                generation=int(str(row[1])),
                heartbeat_at=datetime.fromisoformat(str(row[2])),
                expires_at=datetime.fromisoformat(str(row[3])),
            )
        except Exception as exc:
            raise BacktestIntegrityError("stored worker lease is invalid") from exc

    def _instant_now(self) -> datetime:
        value = self._instant_clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job clock must return a timezone-aware instant")
        return value.astimezone(timezone.utc)

    def claim_next_strategy_job(
        self, *, lease: WorkerLeaseFenceV1 | None = None
    ) -> ClaimedStrategyJobV1 | None:
        """Claim the smallest queued sequence while enforcing one running job.

        Considers all four job types -- the FIFO is keyed on
        ``enqueue_seq`` alone, never on type -- and records ``lease``'s
        owner/generation on the claimed row so a later takeover can tell
        an abandoned claim from one the current healthy lease still owns.
        """
        token = self._token_generator()
        now = self._job_now()
        fence = _lease_fence_params(lease)
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
                    f"""UPDATE strategy_jobs
                       SET status='running', claim_token=?, current_month=NULL,
                           current_stage=NULL, owner_instance_id=?,
                           lease_generation=?,
                           status_version=status_version+1, updated_at=?
                       WHERE id=? AND status='queued' AND status_version=?
                         {_LEASE_FENCE_SQL}""",
                    (token, *fence, now, str(row[0]), int(row[1]), *fence),
                )
                if cursor.rowcount != 1:
                    raise StrategyJobConflict("queued job changed before claim")
                job = self._load_strategy_job(conn, str(row[0]))
                self._require_exclusive_subtype(conn, job)
                self._upsert_notification_outbox_on_connection(conn, job)
                job_type = job.job_type
                return ClaimedStrategyJobV1(
                    job=job,
                    bootstrap=(
                        self._load_bootstrap(conn, job.id)
                        if job_type is StrategyJobType.BOOTSTRAP
                        else None
                    ),
                    initialization=(
                        self._load_initialization(conn, job.id)
                        if job_type is StrategyJobType.INITIALIZATION
                        else None
                    ),
                    preparation=(
                        self._load_preparation(conn, job.id)
                        if job_type is StrategyJobType.PREPARATION
                        else None
                    ),
                    backtest=(
                        self._load_strategy_run(conn, job.id)
                        if job_type is StrategyJobType.BACKTEST
                        else None
                    ),
                    claim_token=token,
                    lease_generation=job.lease_generation,
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
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_type = self._require_job_type(conn, job_id)
            if job_type is StrategyJobType.INITIALIZATION:
                initialization = self._load_initialization(conn, job_id)
                if month not in initialization.requested_months:
                    raise StrategyJobConflict(
                        "progress month is outside requested range"
                    )
            elif job_type is StrategyJobType.BACKTEST:
                backtest = self._load_strategy_run(conn, job_id)
                if not (backtest.start_month <= month <= backtest.end_month):
                    raise StrategyJobConflict(
                        "progress month is outside requested range"
                    )
            else:
                raise StrategyJobConflict(
                    f"{job_type.value} jobs report stages, not months"
                )
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET current_month=?, status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (
                    month,
                    self._job_now(),
                    job_id,
                    claim_token,
                    expected_version,
                    *fence,
                ),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker progress ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def set_strategy_job_current_stage(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        stage: str,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        """Record one stage-walking activity's next declared safe step.

        The stage-typed mirror of :meth:`set_strategy_job_current_month`:
        ``bootstrap``/``preparation`` progress is one closed
        ``current_stage`` value, never a month.
        """
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_type = self._require_job_type(conn, job_id)
            sequence = STAGE_SEQUENCES.get(job_type)
            if sequence is None:
                raise StrategyJobConflict(
                    f"{job_type.value} jobs report months, not stages"
                )
            if stage not in sequence:
                raise StrategyJobConflict(f"{stage!r} is not a {job_type.value} stage")
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET current_stage=?, status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (
                    stage,
                    self._job_now(),
                    job_id,
                    claim_token,
                    expected_version,
                    *fence,
                ),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker progress ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def complete_claimed_stage_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        """Mark one claimed ``bootstrap``/``preparation`` activity complete."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_type = self._require_job_type(conn, job_id)
            if job_type not in STAGE_SEQUENCES:
                raise StrategyJobConflict(
                    f"{job_type.value} jobs do not complete through a stage walk"
                )
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET status='complete', claim_token=NULL, current_stage=NULL,
                       owner_instance_id=NULL, lease_generation=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (self._job_now(), job_id, claim_token, expected_version, *fence),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker completion ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def activate_bootstrap_profile_and_complete(
        self,
        profile: SnapshotProfileV1,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        qualification_contract_digest: str,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        """Atomically seal Bootstrap's profile activation and terminal job state."""
        try:
            canonical = SnapshotProfileV1.from_canonical_json(
                profile.canonical_json_bytes()
            )
            self._validate_profile_authority(canonical)
            with session(self._connect) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if (
                    self._require_job_type(conn, job_id)
                    is not StrategyJobType.BOOTSTRAP
                ):
                    raise StrategyJobConflict("only bootstrap jobs activate profiles")
                fence = _lease_fence_params(lease)
                owned = conn.execute(
                    f"""SELECT 1 FROM strategy_jobs WHERE id=? AND status='running'
                        AND claim_token=? AND status_version=? AND cancel_requested_at IS NULL
                        AND current_stage='profile_activation'
                        {_LEASE_FENCE_SQL}""",
                    (job_id, claim_token, expected_version, *fence),
                ).fetchone()
                if owned is None:
                    raise StrategyJobConflict("worker completion ownership is stale")
                self._require_qualification_on_connection(
                    conn, qualification_contract_digest
                )
                lineage = conn.execute(
                    "SELECT roster_digest FROM reconstruction_roster_lineages WHERE lineage_id=?",
                    (job_id,),
                ).fetchone()
                if lineage is None or str(lineage[0]) != canonical.roster_digest:
                    raise StrategyJobConflict(
                        "bootstrap roster evidence does not match the claimed job"
                    )
                self._insert_profile_on_connection(conn, canonical)
                current = conn.execute(
                    "SELECT profile_hash, activation_seq FROM active_snapshot_profile WHERE singleton_id=1"
                ).fetchone()
                if current is None:
                    activation_seq = 1
                    conn.execute(
                        "INSERT INTO active_snapshot_profile (singleton_id, profile_hash, activation_seq, activated_at) VALUES (1, ?, ?, ?)",
                        (canonical.profile_hash, activation_seq, self._job_now()),
                    )
                elif str(current[0]) != canonical.profile_hash:
                    activation_seq = int(current[1]) + 1
                    conn.execute(
                        "UPDATE active_snapshot_profile SET profile_hash=?, activation_seq=?, activated_at=? WHERE singleton_id=1 AND activation_seq=?",
                        (
                            canonical.profile_hash,
                            activation_seq,
                            self._job_now(),
                            int(current[1]),
                        ),
                    )
                else:
                    activation_seq = int(current[1])
                self._record_activation_history_on_connection(
                    conn, canonical.profile_hash, activation_seq
                )
                cursor = conn.execute(
                    f"""UPDATE strategy_jobs SET status='complete', claim_token=NULL,
                        current_stage=NULL, owner_instance_id=NULL, lease_generation=NULL,
                        status_version=status_version+1, updated_at=?
                        WHERE id=? AND status='running' AND claim_token=? AND status_version=?
                          AND cancel_requested_at IS NULL {_LEASE_FENCE_SQL}""",
                    (self._job_now(), job_id, claim_token, expected_version, *fence),
                )
                if cursor.rowcount != 1:
                    raise StrategyJobConflict("worker completion ownership is stale")
                job = self._load_strategy_job(conn, job_id)
                self._upsert_notification_outbox_on_connection(conn, job)
                return job
        except (BacktestIntegrityError, StrategyJobConflict):
            raise
        except Exception as exc:
            raise BacktestIntegrityError("bootstrap profile activation failed") from exc

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
            if (
                job.job_type is StrategyJobType.BOOTSTRAP
                and job.status is StrategyJobStatus.RUNNING
                and job.current_stage == "profile_activation"
            ) or (
                job.job_type is StrategyJobType.PREPARATION
                and job.status is StrategyJobStatus.RUNNING
                and job.current_stage == "manifest_sealing"
            ):
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
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_type = self._require_job_type(conn, job_id)
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET status='cancelled', claim_token=NULL, current_month=NULL,
                       current_stage=NULL, owner_instance_id=NULL,
                       lease_generation=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NOT NULL
                     {_LEASE_FENCE_SQL}""",
                (self._job_now(), job_id, claim_token, expected_version, *fence),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker cancellation ownership is stale")
            if job_type is StrategyJobType.BACKTEST:
                # AC 5: running cancellation atomically discards every
                # attempt-owned staging row in the exact same commit that
                # finalizes ``cancelled`` -- never a separate write, and
                # shared content-addressed evidence (profile/manifest) is
                # untouched.
                conn.execute("DELETE FROM backtest_staging WHERE run_id=?", (job_id,))
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
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        safe_detail = detail.strip()
        if not safe_detail or len(safe_detail) > 500:
            raise ValueError("failure detail must contain 1-500 characters")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if failed_month is not None:
                job_type = self._require_job_type(conn, job_id)
                if job_type is StrategyJobType.INITIALIZATION:
                    initialization = self._load_initialization(conn, job_id)
                    if failed_month not in initialization.requested_months:
                        raise StrategyJobConflict(
                            "failed month is outside requested range"
                        )
                elif job_type is StrategyJobType.BACKTEST:
                    backtest = self._load_strategy_run(conn, job_id)
                    if not (backtest.start_month <= failed_month <= backtest.end_month):
                        raise StrategyJobConflict(
                            "failed month is outside requested range"
                        )
                else:
                    raise StrategyJobConflict(
                        f"{job_type.value} jobs cannot carry a failed month"
                    )
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET status='failed', claim_token=NULL, current_month=NULL,
                       current_stage=NULL, owner_instance_id=NULL,
                       lease_generation=NULL,
                       failure_code=?, failed_month=?, failure_detail=?,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (
                    failure_code.value,
                    failed_month,
                    safe_detail,
                    self._job_now(),
                    job_id,
                    claim_token,
                    expected_version,
                    *fence,
                ),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker failure ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def complete_claimed_initialization_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
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
            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET status='complete', claim_token=NULL, current_month=NULL,
                       owner_instance_id=NULL, lease_generation=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (self._job_now(), job_id, claim_token, expected_version, *fence),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker completion ownership is stale")
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    # -- Story 2.5: Backtest staging, completion, note and retrieval -----

    def write_backtest_staging(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_version: int,
        state_schema_version: str,
        portfolio_state: Mapping[str, object],
        events: tuple[TradeLogEvent, ...],
        equity_curve: tuple[EquityCurvePointV1, ...],
        final_cash_base: Decimal,
        initial_entry_selection: InitialEntrySelectionV1 | None = None,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> None:
        """Attempt-owned compare-and-swap staging write (AC 1, 6).

        Requires ``claim_token``/``expected_version`` to match the run's
        *current* owning running job -- the identical ownership predicate
        ``set_strategy_job_current_month`` enforces -- and atomically
        replaces the whole canonical staging payload (versioned portfolio
        state, ordered Trade Log events, ordered Equity Curve). Rejects a
        stale, non-running, cancelled, or deleted owner with
        ``StrategyJobConflict``; never partially writes, and staging stays
        invisible to completed-Result queries (``backtest_results`` is a
        separate table). This is the primitive a future ``SessionBatchSink``
        adapter (Story 2.6) calls once per session -- it does not itself
        claim, enqueue, schedule, or run anything.
        """
        if events:
            sequences = [event.sequence for event in events]
            if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
                raise ValueError("staging events must be strictly ordered by sequence")
        if equity_curve:
            sessions = [point.session for point in equity_curve]
            if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
                raise ValueError(
                    "staging equity curve must be strictly ordered by session"
                )
            sequences = [point.sequence for point in equity_curve]
            if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
                raise ValueError(
                    "staging equity curve must be strictly ordered by sequence"
                )
        if not final_cash_base.is_finite():
            raise ValueError("staging final_cash_base must be finite")
        if initial_entry_selection is not None:
            if not equity_curve:
                raise BacktestIntegrityError(
                    "initial entry selection requires a first equity-curve session"
                )
        now = self._job_now()
        try:
            state_json = json.dumps(
                dict(portfolio_state), sort_keys=True, separators=(",", ":")
            )
        except TypeError as exc:
            raise ValueError(
                "staging portfolio_state must be JSON-serializable"
            ) from exc
        events_json = json.dumps(
            [event.model_dump(mode="json") for event in events],
            sort_keys=True,
            separators=(",", ":"),
        )
        curve_json = json.dumps(
            [point.model_dump(mode="json") for point in equity_curve],
            sort_keys=True,
            separators=(",", ":"),
        )
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, run_id)
            fence = _lease_fence_params(lease)
            lease_matches = conn.execute(
                f"SELECT 1 WHERE 1=1 {_LEASE_FENCE_SQL}", fence
            ).fetchone()
            if (
                job.status is not StrategyJobStatus.RUNNING
                or job.claim_token != claim_token
                or job.status_version != expected_version
                or job.cancel_requested_at is not None
                or lease_matches is None
            ):
                raise StrategyJobConflict("staging write ownership is stale")
            if initial_entry_selection is not None:
                strategy_run = self._load_strategy_run_row(conn, run_id)
                selection = strategy_run.universe_selection
                if selection is None:
                    raise BacktestIntegrityError(
                        "initial entry selection requires a pinned universe"
                    )
                try:
                    initial_entry_selection = validate_initial_entry_selection(
                        initial_entry_selection,
                        pinned_security_ids=selection.canonical_security_ids,
                        expected_session=equity_curve[0].session,
                    )
                except StrategyProtocolError as exc:
                    raise BacktestIntegrityError(
                        "staged initial entry selection is invalid",
                        code=exc.code.value,
                    ) from exc
            conn.execute(
                """INSERT INTO backtest_staging (
                       run_id, state_schema_version, state_json, events_json,
                       equity_curve_json, final_cash_base, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       state_schema_version=excluded.state_schema_version,
                       state_json=excluded.state_json,
                       events_json=excluded.events_json,
                       equity_curve_json=excluded.equity_curve_json,
                       final_cash_base=excluded.final_cash_base,
                       updated_at=excluded.updated_at""",
                (
                    run_id,
                    state_schema_version,
                    state_json,
                    events_json,
                    curve_json,
                    str(final_cash_base),
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM backtest_staging_entry_selection_decisions WHERE run_id=?",
                (run_id,),
            )
            conn.execute(
                "DELETE FROM backtest_staging_entry_selection WHERE run_id=?",
                (run_id,),
            )
            if initial_entry_selection is not None:
                self._insert_entry_selection(
                    conn,
                    "backtest_staging_entry_selection",
                    "backtest_staging_entry_selection_decisions",
                    run_id,
                    initial_entry_selection,
                )

    def complete_claimed_backtest_job(
        self,
        job_id: str,
        claim_token: str,
        *,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> StrategyJobV1:
        """Atomically promote one claimed running Backtest attempt's
        staging into an immutable Result + Trade Log + Equity Curve, in
        the exact ``complete_claimed_initialization_job`` shape (AC 4, 6):
        reload/validate the running job's ownership, load staging + the
        pinned ``strategy_runs`` identity, compute Metrics via
        ``metrics.py`` (the sole authority), insert Result/trade_log/
        equity_curve, delete the winning staging row, transition the job
        ``running -> complete``, and upsert the notification outbox -- all
        in one ``BEGIN IMMEDIATE`` transaction.

        Repeated completion once the job has already reached ``complete``
        is an idempotent no-op (returns the already-committed job
        unchanged, no duplicate rows). A proposed Result whose canonical
        content diverges from an already-stored Result for the same
        ``run_id`` raises :class:`BacktestIntegrityError` and leaves the
        stored Result untouched; under this method's own atomic write
        shape that can only occur via directly tampered state, since a
        normal write always inserts the Result and transitions the job
        together.
        """
        from app.services.backtest.backtest_engine import ExitFillEventV1
        from app.services.backtest.metrics import MetricsError, calculate_metrics

        now = self._job_now()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, job_id)
            existing_digest = self._existing_result_digest(conn, job_id)

            if (
                job.status is not StrategyJobStatus.RUNNING
                or job.claim_token != claim_token
                or job.status_version != expected_version
                or job.cancel_requested_at is not None
            ):
                if (
                    job.status is StrategyJobStatus.COMPLETE
                    and existing_digest is not None
                ):
                    return job  # idempotent no-op: already completed
                raise StrategyJobConflict("worker completion ownership is stale")

            strategy_run = self._load_strategy_run_row(conn, job_id)
            staging = self._load_backtest_staging_row(conn, job_id)
            if staging is None:
                raise StrategyJobConflict("no staging exists for this run")

            closed_trades = tuple(
                event for event in staging.events if isinstance(event, ExitFillEventV1)
            )
            try:
                metrics = calculate_metrics(
                    starting_capital=strategy_run.starting_capital,
                    equity_curve=staging.equity_curve,
                    closed_trades=closed_trades,
                )
            except MetricsError as exc:
                raise BacktestIntegrityError(str(exc)) from exc
            payload = self._canonical_result_payload(
                metrics=metrics,
                events=staging.events,
                equity_curve=staging.equity_curve,
                final_cash_base=staging.final_cash_base,
                completed_at=now,
                initial_entry_selection=staging.initial_entry_selection,
            )
            proposed_digest = manifest_digest(payload)

            if existing_digest is not None:
                if existing_digest != proposed_digest:
                    raise BacktestIntegrityError(
                        "conflicting repeat completion for run_id"
                    )
            else:
                self._insert_backtest_result(
                    conn,
                    job_id,
                    metrics=metrics,
                    events=staging.events,
                    equity_curve=staging.equity_curve,
                    final_cash_base=staging.final_cash_base,
                    result_digest=proposed_digest,
                    completed_at=now,
                    initial_entry_selection=staging.initial_entry_selection,
                )

            fence = _lease_fence_params(lease)
            cursor = conn.execute(
                f"""UPDATE strategy_jobs
                   SET status='complete', claim_token=NULL, current_month=NULL,
                       owner_instance_id=NULL, lease_generation=NULL,
                       status_version=status_version+1, updated_at=?
                   WHERE id=? AND status='running' AND claim_token=?
                     AND status_version=? AND cancel_requested_at IS NULL
                     {_LEASE_FENCE_SQL}""",
                (now, job_id, claim_token, expected_version, *fence),
            )
            if cursor.rowcount != 1:
                raise StrategyJobConflict("worker completion ownership is stale")
            conn.execute("DELETE FROM backtest_staging WHERE run_id=?", (job_id,))
            job = self._load_strategy_job(conn, job_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            return job

    def update_backtest_result_note(
        self, run_id: str, *, expected_note_version: int, note: str | None
    ) -> BacktestResultV1:
        """Compare-and-swap note update (AC 5) -- the one repository
        method permitted to touch a completed Result's note. Changes only
        ``note``/``note_version``/``updated_at``; every other Result field
        (digests, Metrics, Trade Log, Equity Curve) stays immutable,
        independently enforced by the ``backtest_result_evidence_
        immutable`` trigger. ``note`` is escaped plain text; the escaped
        result is capped at 10,000 Unicode code points; empty or
        whitespace-only input normalizes to ``None``.
        """
        normalized = self._normalize_note(note)
        now = self._job_now()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE backtest_results
                   SET note=?, note_version=note_version+1, updated_at=?
                   WHERE run_id=? AND note_version=?""",
                (normalized, now, run_id, expected_note_version),
            )
            if cursor.rowcount != 1:
                exists = conn.execute(
                    "SELECT 1 FROM backtest_results WHERE run_id=?", (run_id,)
                ).fetchone()
                if exists is None:
                    raise StrategyJobNotFound(f"backtest result not found: {run_id}")
                raise StrategyJobConflict("note update version is stale")
        return self.backtest_result(run_id)

    def backtest_result(self, run_id: str) -> BacktestResultV1:
        """Return one completed Backtest's full typed retrieval projection
        (AC 5): Strategy ID/version, exact parameters, normalized period,
        profile/ordered evidence, capital/base currency, full replay/
        execution-contract digests, the four Metrics plus typed
        availability reasons (recomputed via ``metrics.py``, the sole
        authority -- never a second implementation), the complete ordered
        Trade Log, the Equity Curve, provenance, and optional note state.

        Raises :class:`StrategyJobNotFound` when no completed Result
        exists for ``run_id``, and :class:`BacktestIntegrityError` if the
        stored evidence no longer reconstructs to its own recorded digest
        (tamper detection, mirroring ``activate_snapshot_profile``'s
        rebuild-and-compare convention).
        """
        from app.services.backtest.backtest_engine import ExitFillEventV1
        from app.services.backtest.metrics import (
            BacktestMetricsV1,
            MetricsError,
            metric_availability,
        )

        with session(self._connect) as conn:
            strategy_run = self._load_strategy_run_row(conn, run_id)
            row = conn.execute(
                """SELECT result_schema_version, metrics_json, final_cash_base,
                          result_digest, note,
                          note_version, completed_at
                   FROM backtest_results WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StrategyJobNotFound(f"backtest result not found: {run_id}")
            event_rows = conn.execute(
                "SELECT event_json FROM trade_log WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            curve_rows = conn.execute(
                """SELECT date, sequence, cash_base, positions_value_base,
                          total_equity_base
                   FROM equity_curve WHERE run_id=? ORDER BY date""",
                (run_id,),
            ).fetchall()
            initial_entry_selection = self._load_entry_selection(
                conn,
                "backtest_result_entry_selection",
                "backtest_result_entry_selection_decisions",
                run_id,
            )

        result_schema_version = str(row[0])
        if result_schema_version == "backtest_result.v1":
            if initial_entry_selection is not None:
                raise BacktestIntegrityError(
                    "legacy backtest result contains unexpected selection evidence"
                )
        elif result_schema_version == "backtest_result.v2":
            if initial_entry_selection is None:
                raise BacktestIntegrityError(
                    "selection-bearing backtest result is missing selection evidence"
                )
        else:
            raise BacktestIntegrityError("stored backtest result schema is invalid")

        try:
            metrics = BacktestMetricsV1.model_validate(json.loads(str(row[1])))
            final_cash_base = Decimal(str(row[2]))
            completed_at_raw = str(row[6])
            completed_at = datetime.fromisoformat(completed_at_raw)
            events = tuple(
                self._parse_trade_log_event(json.loads(str(item[0])))
                for item in event_rows
            )
            equity_curve = tuple(
                self._parse_equity_curve_point(
                    {
                        "session": str(item[0]),
                        "sequence": int(item[1]),
                        "cash_base": str(item[2]),
                        "positions_value_base": str(item[3]),
                        "total_equity_base": str(item[4]),
                    }
                )
                for item in curve_rows
            )
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            InvalidOperation,
        ) as exc:
            raise BacktestIntegrityError("stored backtest result is invalid") from exc

        payload = self._canonical_result_payload(
            metrics=metrics,
            events=events,
            equity_curve=equity_curve,
            final_cash_base=final_cash_base,
            completed_at=completed_at_raw,
            initial_entry_selection=initial_entry_selection,
        )
        if manifest_digest(payload) != str(row[3]):
            raise BacktestIntegrityError("stored backtest result digest is invalid")

        closed_trades = tuple(
            event for event in events if isinstance(event, ExitFillEventV1)
        )
        try:
            availability = metric_availability(
                equity_curve=equity_curve, closed_trades=closed_trades
            )
        except MetricsError as exc:
            raise BacktestIntegrityError(str(exc)) from exc

        return BacktestResultV1(
            run_id=run_id,
            strategy_id=strategy_run.strategy_id,
            strategy_api_version=strategy_run.strategy_api_version,
            strategy_source_digest=strategy_run.strategy_source_digest,
            parameters=strategy_run.parameters,
            profile_hash=strategy_run.profile_hash,
            start_month=strategy_run.start_month,
            end_month=strategy_run.end_month,
            ordered_month_digest=strategy_run.ordered_month_digest,
            base_currency=strategy_run.base_currency,
            starting_capital=strategy_run.starting_capital,
            run_input_manifest_digest=strategy_run.run_input_manifest_digest,
            execution_contract_digest=strategy_run.execution_contract_digest,
            metrics=metrics,
            metric_availability=availability,
            events=events,
            equity_curve=equity_curve,
            final_cash_base=final_cash_base,
            completed_at=completed_at,
            note=None if row[4] is None else str(row[4]),
            note_version=int(row[5]),
            manifest_version=strategy_run.manifest_version,
            universe_selection=strategy_run.universe_selection,
            source_preparation_job_id=strategy_run.source_preparation_job_id,
            initial_entry_selection=initial_entry_selection,
        )

    @staticmethod
    def _normalize_note(note: str | None) -> str | None:
        if note is None:
            return None
        stripped = note.strip()
        if not stripped:
            return None
        escaped = html.escape(stripped, quote=True)
        if len(escaped) > _NOTE_MAX_CODE_POINTS:
            raise ValueError(
                f"note text exceeds {_NOTE_MAX_CODE_POINTS} Unicode code points"
            )
        return escaped

    @staticmethod
    def _existing_result_digest(conn: sqlite3.Connection, run_id: str) -> str | None:
        row = conn.execute(
            "SELECT result_digest FROM backtest_results WHERE run_id=?", (run_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _load_strategy_run_row(
        conn: sqlite3.Connection, run_id: str
    ) -> _StrategyRunRow:
        row = conn.execute(
            """SELECT id, strategy_id, strategy_api_version, strategy_source_digest,
                      parameters_json, profile_hash, start_month, end_month,
                      ordered_month_digest, base_currency, starting_capital,
                      run_input_manifest_digest, execution_contract_digest
                      ,manifest_version,selection_json,source_preparation_job_id
               FROM strategy_runs WHERE id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"strategy run not found: {run_id}")
        try:
            parameters = json.loads(str(row[4]))
            if not isinstance(parameters, dict):
                raise ValueError("stored strategy run parameters are not an object")
            starting_capital = Decimal(str(row[10]))
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            InvalidOperation,
        ) as exc:
            raise BacktestIntegrityError("stored strategy run is invalid") from exc
        try:
            selection = (
                None
                if row[14] is None
                else RunUniverseSelectionV1.model_validate_json(str(row[14]))
            )
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored strategy run provenance is invalid"
            ) from exc
        manifest_row = conn.execute(
            "SELECT manifest_version,canonical_manifest_json FROM run_input_manifests WHERE digest=?",
            (str(row[11]),),
        ).fetchone()
        if manifest_row is None or str(manifest_row[0]) != str(row[13]):
            raise BacktestIntegrityError("manifest and run versions disagree")
        if str(manifest_row[1]) == "{}" and str(row[13]) != "run_input_manifest.v1":
            raise BacktestIntegrityError("stored run input manifest is invalid")
        if str(manifest_row[1]) != "{}":
            try:
                from app.services.backtest.run_input_manifest import (
                    read_run_input_manifest,
                )

                parsed = read_run_input_manifest(str(manifest_row[1]))
                if parsed.schema_version != str(row[13]) or parsed.digest() != str(
                    row[11]
                ):
                    raise ValueError
                if (
                    str(row[13]) == "run_input_manifest.v2"
                    and getattr(parsed, "universe_selection", None) != selection
                ):
                    raise ValueError
            except Exception as exc:
                raise BacktestIntegrityError(
                    "stored run input manifest is invalid"
                ) from exc
        return _StrategyRunRow(
            id=str(row[0]),
            strategy_id=str(row[1]),
            strategy_api_version=int(row[2]),
            strategy_source_digest=str(row[3]),
            parameters=parameters,
            profile_hash=str(row[5]),
            start_month=str(row[6]),
            end_month=str(row[7]),
            ordered_month_digest=str(row[8]),
            base_currency=str(row[9]),
            starting_capital=starting_capital,
            run_input_manifest_digest=str(row[11]),
            execution_contract_digest=str(row[12]),
            manifest_version=str(row[13]),
            universe_selection=selection,
            source_preparation_job_id=None if row[15] is None else str(row[15]),
        )

    @staticmethod
    def _load_strategy_run(conn: sqlite3.Connection, job_id: str) -> BacktestRunV1:
        """Return job ``job_id``'s pinned ``strategy_runs`` identity as
        the typed :class:`BacktestRunV1` subtype (Story 2.6) -- the
        backtest-side mirror of ``_load_initialization``, reusing
        ``_load_strategy_run_row``'s existing parse/tamper handling."""
        row = BacktestRepository._load_strategy_run_row(conn, job_id)
        return BacktestRunV1(
            job_id=row.id,
            strategy_id=row.strategy_id,
            strategy_api_version=row.strategy_api_version,
            strategy_source_digest=row.strategy_source_digest,
            parameters=row.parameters,
            profile_hash=row.profile_hash,
            start_month=row.start_month,
            end_month=row.end_month,
            ordered_month_digest=row.ordered_month_digest,
            base_currency=row.base_currency,  # type: ignore[arg-type]
            starting_capital=row.starting_capital,
            run_input_manifest_digest=row.run_input_manifest_digest,
            execution_contract_digest=row.execution_contract_digest,
            manifest_version=cast(
                Literal["run_input_manifest.v1", "run_input_manifest.v2"],
                row.manifest_version,
            ),
            universe_selection=row.universe_selection,
            source_preparation_job_id=row.source_preparation_job_id,
        )

    @classmethod
    def _require_own_subtype(
        cls, conn: sqlite3.Connection, job: StrategyJobV1, wanted: StrategyJobType
    ) -> None:
        """Raise unless ``job`` is a ``wanted`` job with only its own subtype."""
        if job.job_type is not wanted:
            raise StrategyJobNotFound(f"{wanted.value} run not found: {job.id}")
        cls._require_exclusive_subtype(conn, job)

    @staticmethod
    def _require_exclusive_subtype(
        conn: sqlite3.Connection, job: StrategyJobV1
    ) -> None:
        """Reject a job carrying a subtype row that is not its own.

        Every ``strategy_jobs`` row has exactly one matching subtype row.
        A *missing* matching row surfaces from the subtype loader itself
        as :class:`StrategyJobNotFound`; this guards the other half of the
        invariant -- a row in some other type's subtype table, which no
        legitimate write path can produce and which would otherwise let a
        claimed job run against the wrong identity.
        """
        for job_type, (table, column) in _SUBTYPE_TABLES.items():
            if job_type is job.job_type:
                continue
            if (
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column}=?", (job.id,)
                ).fetchone()
                is not None
            ):
                raise BacktestIntegrityError(
                    f"{job.job_type.value} job {job.id} also has a "
                    f"{job_type.value} subtype row"
                )

    @staticmethod
    def _require_stage_subtype_row(
        conn: sqlite3.Connection, job_id: str, job_type: StrategyJobType
    ) -> None:
        """Raise unless ``job_id`` has its stage-typed subtype identity row."""
        table, column = _SUBTYPE_TABLES[job_type]
        if (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE {column}=?", (job_id,)
            ).fetchone()
            is None
        ):
            raise StrategyJobNotFound(f"{job_type.value} run not found: {job_id}")

    def _load_bootstrap(self, conn: sqlite3.Connection, job_id: str) -> BootstrapRunV1:
        self._require_stage_subtype_row(conn, job_id, StrategyJobType.BOOTSTRAP)
        return BootstrapRunV1(job_id=job_id)

    def _load_preparation(
        self, conn: sqlite3.Connection, job_id: str
    ) -> PreparationRunV1:
        row = conn.execute(
            "SELECT selection_json,strategy_id,strategy_api_version,strategy_source_digest,parameters_json,start_month,end_month,base_currency,starting_capital FROM preparation_runs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"preparation run not found: {job_id}")
        if row[0] is None:
            return PreparationRunV1(job_id=job_id)
        try:
            return PreparationRunV1(
                job_id=job_id,
                selection=RunUniverseSelectionV1.model_validate_json(str(row[0])),
                strategy_id=str(row[1]),
                strategy_api_version=int(row[2]),
                strategy_source_digest=str(row[3]),
                parameters=json.loads(str(row[4])),
                start_month=str(row[5]),
                end_month=str(row[6]),
                base_currency=cast(Literal["GBP", "USD"], str(row[7])),
                starting_capital=Decimal(str(row[8])),
            )
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored preparation identity is invalid"
            ) from exc

    @staticmethod
    def _require_job_type(conn: sqlite3.Connection, job_id: str) -> StrategyJobType:
        """Return ``job_id``'s ``job_type`` without loading the full row --
        the minimal lookup type-aware lifecycle methods (progress,
        failure, cancellation, deletion) need before deciding which
        subtype table to consult."""
        row = conn.execute(
            "SELECT job_type FROM strategy_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"strategy job not found: {job_id}")
        return StrategyJobType(str(row[0]))

    def _load_backtest_staging_row(
        self, conn: sqlite3.Connection, run_id: str
    ) -> BacktestStagingV1 | None:
        row = conn.execute(
            """SELECT run_id, state_schema_version, state_json, events_json,
                      equity_curve_json, final_cash_base, updated_at
               FROM backtest_staging WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        initial_entry_selection = self._load_entry_selection(
            conn,
            "backtest_staging_entry_selection",
            "backtest_staging_entry_selection_decisions",
            run_id,
        )
        try:
            state = json.loads(str(row[2]))
            if not isinstance(state, dict):
                raise ValueError("stored staging state is not an object")
            events = tuple(
                self._parse_trade_log_event(item) for item in json.loads(str(row[3]))
            )
            equity_curve = tuple(
                self._parse_equity_curve_point(item) for item in json.loads(str(row[4]))
            )
            final_cash_base = Decimal(str(row[5]))
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            InvalidOperation,
        ) as exc:
            raise BacktestIntegrityError("stored backtest staging is invalid") from exc
        return BacktestStagingV1(
            run_id=str(row[0]),
            state_schema_version=str(row[1]),
            portfolio_state=state,
            events=events,
            equity_curve=equity_curve,
            final_cash_base=final_cash_base,
            updated_at=str(row[6]),
            initial_entry_selection=initial_entry_selection,
        )

    def _insert_backtest_result(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        metrics: BacktestMetricsV1,
        events: tuple[TradeLogEvent, ...],
        equity_curve: tuple[EquityCurvePointV1, ...],
        final_cash_base: Decimal,
        result_digest: str,
        completed_at: str,
        initial_entry_selection: InitialEntrySelectionV1 | None,
    ) -> None:
        metrics_json = json.dumps(
            metrics.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        conn.execute(
            """INSERT INTO backtest_results (
                   run_id, result_schema_version, metrics_json, final_cash_base,
                   result_digest,
                   note, note_version, completed_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)""",
            (
                run_id,
                (
                    "backtest_result.v2"
                    if initial_entry_selection is not None
                    else "backtest_result.v1"
                ),
                metrics_json,
                str(final_cash_base),
                result_digest,
                completed_at,
                completed_at,
            ),
        )
        if initial_entry_selection is not None:
            self._insert_entry_selection(
                conn,
                "backtest_result_entry_selection",
                "backtest_result_entry_selection_decisions",
                run_id,
                initial_entry_selection,
            )
        for event in events:
            conn.execute(
                """INSERT INTO trade_log (
                       id, run_id, sequence, kind, security_id, event_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._id_generator(),
                    run_id,
                    event.sequence,
                    event.kind,
                    event.security_id,
                    json.dumps(
                        event.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        for point in equity_curve:
            conn.execute(
                """INSERT INTO equity_curve (
                       run_id, date, sequence, cash_base, positions_value_base,
                       total_equity_base
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    point.session.isoformat(),
                    point.sequence,
                    str(point.cash_base),
                    str(point.positions_value_base),
                    str(point.total_equity_base),
                ),
            )

    @staticmethod
    def _canonical_result_payload(
        *,
        metrics: BacktestMetricsV1,
        events: tuple[TradeLogEvent, ...],
        equity_curve: tuple[EquityCurvePointV1, ...],
        final_cash_base: Decimal,
        completed_at: str,
        initial_entry_selection: InitialEntrySelectionV1 | None = None,
    ) -> dict[str, object]:
        """The one canonical shape both digest computation (on write) and
        tamper verification (on read) hash -- pre-stringifies every
        Decimal via pydantic's own ``mode="json"`` dump before this ever
        reaches ``canonical_manifest.jsonable`` (which has no native
        ``Decimal`` case). ``completed_at`` is the exact stored ISO string
        (the same value passed to ``_insert_backtest_result`` on write, or
        read back verbatim from the ``backtest_results`` row) so a
        tampered ``completed_at`` fails the digest rebuild-and-compare
        just like every other evidence field."""
        payload: dict[str, object] = {
            "schema_version": (
                "backtest_result.v2"
                if initial_entry_selection is not None
                else "backtest_result.v1"
            ),
            "metrics": metrics.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "equity_curve": [point.model_dump(mode="json") for point in equity_curve],
            "final_cash_base": str(final_cash_base),
            "completed_at": completed_at,
        }
        if initial_entry_selection is not None:
            payload["initial_entry_selection"] = initial_entry_selection.model_dump(
                mode="json"
            )
        return payload

    @staticmethod
    def _insert_entry_selection(
        conn: sqlite3.Connection,
        header_table: str,
        decision_table: str,
        run_id: str,
        selection: InitialEntrySelectionV1,
    ) -> None:
        conn.execute(
            f"INSERT INTO {header_table} "
            "(run_id, session, metric_id, metric_version, rule_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                selection.session.isoformat(),
                selection.metric_id,
                selection.metric_version,
                selection.rule_id,
            ),
        )
        for decision in selection.decisions:
            conn.execute(
                f"INSERT INTO {decision_table} "
                "(run_id, security_id, rank, state, score, reason_code) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    decision.security_id,
                    decision.rank,
                    decision.state.value,
                    None if decision.score is None else str(decision.score),
                    decision.reason_code,
                ),
            )

    @staticmethod
    def _load_entry_selection(
        conn: sqlite3.Connection,
        header_table: str,
        decision_table: str,
        run_id: str,
    ) -> InitialEntrySelectionV1 | None:
        header = conn.execute(
            f"SELECT session, metric_id, metric_version, rule_id "
            f"FROM {header_table} WHERE run_id=?",
            (run_id,),
        ).fetchone()
        decision_rows = conn.execute(
            f"SELECT security_id, rank, state, score, reason_code "
            f"FROM {decision_table} WHERE run_id=? ORDER BY rank",
            (run_id,),
        ).fetchall()
        if header is None:
            if decision_rows:
                raise BacktestIntegrityError(
                    "entry selection decisions exist without a header"
                )
            return None
        try:
            decisions = tuple(
                EntrySelectionDecisionV1(
                    security_id=str(row[0]),
                    rank=int(row[1]),
                    state=EntrySelectionState(str(row[2])),
                    score=None if row[3] is None else Decimal(str(row[3])),
                    reason_code=None if row[4] is None else str(row[4]),
                )
                for row in decision_rows
            )
            selection_session = date.fromisoformat(str(header[0]))
            rule_id = str(header[3])
            signals = tuple(
                Signal(
                    security_id=decision.security_id,
                    side=SignalSide.BUY,
                    session=selection_session,
                    rule_id=rule_id,
                )
                for decision in decisions
                if decision.state.value == "selected"
            )
            return InitialEntrySelectionV1(
                session=selection_session,
                metric_id=str(header[1]),
                metric_version=str(header[2]),
                rule_id=rule_id,
                decisions=decisions,
                signals=signals,
            )
        except (ValueError, TypeError) as exc:
            raise BacktestIntegrityError("stored entry selection is invalid") from exc

    @staticmethod
    def _parse_trade_log_event(payload: object) -> TradeLogEvent:
        from app.services.backtest.backtest_engine import (
            DividendAppliedEventV1,
            EntryFillEventV1,
            ExitFillEventV1,
            OpenPositionMarkEventV1,
            SkippedSignalEventV1,
            SplitAppliedEventV1,
        )

        if not isinstance(payload, dict):
            raise ValueError("trade log event payload is not an object")
        models: dict[str, type] = {
            "entry_fill": EntryFillEventV1,
            "exit_fill": ExitFillEventV1,
            "skipped_signal": SkippedSignalEventV1,
            "split_applied": SplitAppliedEventV1,
            "dividend_applied": DividendAppliedEventV1,
            "open_position_mark": OpenPositionMarkEventV1,
        }
        kind = payload.get("kind")
        model = models.get(str(kind))
        if model is None:
            raise ValueError(f"unknown trade log event kind: {kind!r}")
        # ``strict=False``: this payload round-tripped through this
        # method's own ``model_dump(mode="json")`` writer, so ``date``/
        # ``Decimal`` fields are JSON strings here -- coercing them back
        # is exact and lossless, unlike relaxing validation of untrusted
        # input. The model's own field constraints (patterns, ``gt``,
        # ``ge``, ``allow_inf_nan=False``) still apply either way.
        return model.model_validate(payload, strict=False)

    @staticmethod
    def _parse_equity_curve_point(payload: object) -> EquityCurvePointV1:
        from app.services.backtest.backtest_engine import EquityCurvePointV1

        if not isinstance(payload, dict):
            raise ValueError("equity curve point payload is not an object")
        return EquityCurvePointV1.model_validate(payload, strict=False)

    def reconcile_interrupted_strategy_jobs(
        self, *, lease: WorkerLeaseFenceV1 | None = None
    ) -> tuple[StrategyJobV1, ...]:
        """Fail running claims left behind by a previous application process.

        With ``lease`` supplied (startup or a takeover), a ``running`` row
        the current healthy lease still owns is left completely untouched
        and only abandoned claims -- those owned by a stale generation, or
        by no lease at all -- become ``worker_interrupted``.
        """
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if lease is None:
                rows = conn.execute(
                    "SELECT id, status_version FROM strategy_jobs "
                    "WHERE status='running' ORDER BY enqueue_seq"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, status_version FROM strategy_jobs
                       WHERE status='running'
                         AND NOT (owner_instance_id IS ? AND lease_generation IS ?)
                       ORDER BY enqueue_seq""",
                    (lease.instance_id, lease.generation),
                ).fetchall()
            reconciled: list[StrategyJobV1] = []
            for row in rows:
                cursor = conn.execute(
                    """UPDATE strategy_jobs
                       SET status='failed', claim_token=NULL, current_month=NULL,
                           current_stage=NULL, owner_instance_id=NULL,
                           lease_generation=NULL,
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
                if (
                    job.job_type is StrategyJobType.BOOTSTRAP
                    and job.status is StrategyJobStatus.RUNNING
                    and job.current_stage == "profile_activation"
                ) or (
                    job.job_type is StrategyJobType.PREPARATION
                    and job.status is StrategyJobStatus.RUNNING
                    and job.current_stage == "manifest_sealing"
                ):
                    return ()
                return ("cancel",)
            if job.status in {StrategyJobStatus.FAILED, StrategyJobStatus.CANCELLED}:
                if job.job_type in STAGE_SEQUENCES:
                    # Bootstrap/Preparation have no replay-from-beginning
                    # restart path: their real domain logic (and therefore
                    # what a restart would even replay) is Story 4.3/4.6.
                    return ("delete",)
                child = conn.execute(
                    """SELECT 1 FROM strategy_job_restart_actions
                       WHERE source_job_id=? LIMIT 1""",
                    (job_id,),
                ).fetchone()
                return ("delete",) if child is not None else ("restart", "delete")
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
                     ordered_month_digest, mode
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
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
                    initialization.mode,
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

    def restart_backtest_job(
        self,
        source_job_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BacktestEnqueueResultV1:
        """Create one replay-from-beginning Backtest child for an eligible
        terminal source (AC 7) -- mirrors ``restart_initialization_job``'s
        idempotency-key/parent-child shape exactly, copying the source's
        immutable Strategy/version/parameters/profile/range/capital/
        currency identity and reusing (never duplicating) its existing
        content-addressed ``run_input_manifests`` binding. Never copies
        staging -- the child always starts from ``queued``/no progress.
        """
        if not idempotency_key.strip():
            raise ValueError("restart idempotency key must not be blank")
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._load_strategy_job(conn, source_job_id)
            if source.job_type is not StrategyJobType.BACKTEST:
                raise StrategyJobConflict(
                    "restart_backtest_job requires a backtest job"
                )
            existing = conn.execute(
                """SELECT child_job_id FROM strategy_job_restart_actions
                   WHERE source_job_id=? AND idempotency_key=?""",
                (source_job_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                child_id = str(existing[0])
                return BacktestEnqueueResultV1(
                    job=self._load_strategy_job(conn, child_id),
                    backtest=self._load_strategy_run(conn, child_id),
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
            backtest = self._load_strategy_run(conn, source_job_id)
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
                   ) VALUES (?, 'backtest', 'queued', ?, ?, NULL, NULL, 1,
                              NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                (child_id, source_job_id, sequence, now, now),
            )
            conn.execute(
                """INSERT INTO strategy_runs (
                     id, strategy_id, strategy_api_version, strategy_source_digest,
                     parameters_json, profile_hash, start_month, end_month,
                     ordered_month_digest, base_currency, starting_capital,
                     run_input_manifest_digest, execution_contract_digest,
                     manifest_version,run_universe_digest,source_preparation_job_id,selection_json,created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_id,
                    backtest.strategy_id,
                    backtest.strategy_api_version,
                    backtest.strategy_source_digest,
                    json.dumps(
                        dict(backtest.parameters),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    backtest.profile_hash,
                    backtest.start_month,
                    backtest.end_month,
                    backtest.ordered_month_digest,
                    backtest.base_currency,
                    str(backtest.starting_capital),
                    backtest.run_input_manifest_digest,
                    backtest.execution_contract_digest,
                    backtest.manifest_version,
                    None
                    if backtest.universe_selection is None
                    else backtest.universe_selection.run_universe_digest,
                    None,
                    None
                    if backtest.universe_selection is None
                    else backtest.universe_selection.model_dump_json(),
                    now,
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
            return BacktestEnqueueResultV1(
                job=child, backtest=self._load_strategy_run(conn, child_id)
            )

    def delete_strategy_job(
        self, job_id: str, *, expected_version: int
    ) -> StrategyJobV1:
        """Tombstone a failed/cancelled attempt while retaining lineage and audit."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._load_strategy_job(conn, job_id)
            if job.deleted_at is not None:
                if job.status_version != expected_version:
                    raise StrategyJobConflict("delete request is stale")
                return job
            if job.status_version != expected_version:
                raise StrategyJobConflict("delete request is stale")
            if job.status not in {
                StrategyJobStatus.FAILED,
                StrategyJobStatus.CANCELLED,
            }:
                raise StrategyJobConflict("strategy job cannot be deleted")
            if job.job_type is StrategyJobType.INITIALIZATION:
                initialization = self._load_initialization(conn, job_id)
                summary = (
                    f"{job.job_type.value} {job.status.value}: "
                    f"{initialization.requested_start} to "
                    f"{initialization.requested_end}"
                )
            elif job.job_type is StrategyJobType.BACKTEST:
                backtest = self._load_strategy_run(conn, job_id)
                summary = (
                    f"{job.job_type.value} {job.status.value}: "
                    f"{backtest.start_month} to {backtest.end_month}"
                )
            else:
                stages = STAGE_SEQUENCES[job.job_type]
                summary = (
                    f"{job.job_type.value} {job.status.value}: {len(stages)} stages"
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
            if job.job_type in STAGE_SEQUENCES:
                table, _ = _SUBTYPE_TABLES[job.job_type]
                if job.job_type is StrategyJobType.PREPARATION:
                    conn.execute(
                        "DELETE FROM preparation_enqueue_actions WHERE job_id=?",
                        (job_id,),
                    )
                conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            elif job.job_type is StrategyJobType.INITIALIZATION:
                conn.execute(
                    "DELETE FROM initialization_runs WHERE job_id=?", (job_id,)
                )
            else:
                # AC 8: delete only this attempt's Strategy Run binding and
                # any remaining staging -- never the shared content-
                # addressed manifest, never a descendant, and never a
                # completed Result (Story 2.5's schema makes
                # ``backtest_results``/``trade_log``/``equity_curve``
                # unconditionally immutable-delete, and a failed/cancelled
                # attempt never has one).
                conn.execute("DELETE FROM backtest_staging WHERE run_id=?", (job_id,))
                conn.execute("DELETE FROM strategy_runs WHERE id=?", (job_id,))
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
        backtest = None
        if job.job_type is StrategyJobType.BACKTEST:
            try:
                backtest = self._load_strategy_run(conn, job.id).model_dump(mode="json")
            except StrategyJobNotFound:
                backtest = None
        payload = {
            "schema_version": "strategy_job_notification.v1",
            "job": job.model_dump(mode="json"),
            "initialization": initialization,
            "backtest": backtest,
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
        pending: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[2]))
            except json.JSONDecodeError:
                logger.exception(
                    "Invalid Strategy Manager notification payload for job %s",
                    row[0],
                )
                continue
            pending.append(
                {
                    "job_id": str(row[0]),
                    "job_status_version": int(row[1]),
                    "payload": payload,
                }
            )
        return tuple(pending)

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
                      ordered_month_digest, mode
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
        profile = self.stored_snapshot_profile(profile_hash)
        if profile is not None:
            self._validate_profile_authority(profile)
        return profile

    def stored_snapshot_profile(self, profile_hash: str) -> SnapshotProfileV1 | None:
        """Load persisted profile identity without applying runtime authority."""
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
        return self._validated_profile_row(profile_hash, row)

    def claim_bau_capture_attempt(
        self,
        *,
        run_id: str,
        profile_hash: str,
        snapshot_month: str,
        attempted_at: datetime,
    ) -> bool:
        """Claim the one permitted live capture attempt for profile/month."""
        stamp = attempted_at.astimezone(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO bau_run_authority (
                       run_id, profile_hash, snapshot_month, state, attempted_at
                   ) VALUES (?, ?, ?, 'attempted', ?)""",
                (run_id, profile_hash, snapshot_month, stamp),
            )
            return cursor.rowcount == 1

    def prepare_bau_run_authority(
        self,
        *,
        run_id: str,
        analysis_payload_digest: str,
        capture_digest: str,
        prepared_envelope_digest: str,
    ) -> BauRunAuthority:
        """Bind a published prepared envelope to its previously claimed run."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_bau_run_authority(conn, run_id)
            if current.state == "prepared":
                if (
                    current.analysis_payload_digest,
                    current.capture_digest,
                    current.prepared_envelope_digest,
                ) != (
                    analysis_payload_digest,
                    capture_digest,
                    prepared_envelope_digest,
                ):
                    raise BacktestIntegrityError(
                        "prepared BAU run authority has conflicting content"
                    )
                return current
            if current.state != "attempted":
                raise BacktestIntegrityError("BAU run cannot be prepared")
            conn.execute(
                """UPDATE bau_run_authority
                   SET state='prepared', analysis_payload_digest=?,
                       capture_digest=?, prepared_envelope_digest=?
                   WHERE run_id=? AND state='attempted'""",
                (
                    analysis_payload_digest,
                    capture_digest,
                    prepared_envelope_digest,
                    run_id,
                ),
            )
            return self._load_bau_run_authority(conn, run_id)

    def complete_bau_run_authority(
        self,
        *,
        run_id: str,
        completed_envelope_digest: str,
        completed_at: datetime,
    ) -> BauRunAuthority:
        """Record terminal scanner success after the pipeline status commits."""
        stamp = completed_at.astimezone(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_bau_run_authority(conn, run_id)
            if current.state == "completed":
                if current.completed_envelope_digest != completed_envelope_digest:
                    raise BacktestIntegrityError(
                        "completed BAU run authority has conflicting content"
                    )
                return current
            if current.state != "prepared":
                raise BacktestIntegrityError("BAU run cannot be completed")
            conn.execute(
                """UPDATE bau_run_authority
                   SET state='completed', completed_envelope_digest=?, completed_at=?
                   WHERE run_id=? AND state='prepared'""",
                (completed_envelope_digest, stamp, run_id),
            )
            return self._load_bau_run_authority(conn, run_id)

    def fail_bau_run_authority(
        self, *, run_id: str, completed_at: datetime, reason: str
    ) -> BauRunAuthority:
        """Close an attempted/prepared run without promotable authority."""
        stamp = completed_at.astimezone(timezone.utc).isoformat()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_bau_run_authority(conn, run_id)
            if current.state in {"completed", "failed"}:
                return current
            conn.execute(
                """UPDATE bau_run_authority
                   SET state='failed', completed_at=?, failure_reason=?
                   WHERE run_id=? AND state IN ('attempted', 'prepared')""",
                (stamp, reason[:500], run_id),
            )
            return self._load_bau_run_authority(conn, run_id)

    def bau_run_authority(self, run_id: str) -> BauRunAuthority | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT run_id, profile_hash, snapshot_month, state, attempted_at,
                          analysis_payload_digest, capture_digest,
                          prepared_envelope_digest, completed_envelope_digest,
                          completed_at, failure_reason
                   FROM bau_run_authority WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        return None if row is None else self._bau_run_authority_from_row(row)

    def unfinished_bau_run_authorities(self) -> tuple[BauRunAuthority, ...]:
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT run_id, profile_hash, snapshot_month, state, attempted_at,
                          analysis_payload_digest, capture_digest,
                          prepared_envelope_digest, completed_envelope_digest,
                          completed_at, failure_reason
                   FROM bau_run_authority
                   WHERE state IN ('attempted', 'prepared')
                   ORDER BY attempted_at"""
            ).fetchall()
        return tuple(self._bau_run_authority_from_row(row) for row in rows)

    @classmethod
    def _load_bau_run_authority(
        cls, conn: sqlite3.Connection, run_id: str
    ) -> BauRunAuthority:
        row = conn.execute(
            """SELECT run_id, profile_hash, snapshot_month, state, attempted_at,
                      analysis_payload_digest, capture_digest,
                      prepared_envelope_digest, completed_envelope_digest,
                      completed_at, failure_reason
               FROM bau_run_authority WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise BacktestIntegrityError("BAU run authority does not exist")
        return cls._bau_run_authority_from_row(row)

    @staticmethod
    def _bau_run_authority_from_row(row) -> BauRunAuthority:
        return BauRunAuthority(
            run_id=str(row[0]),
            profile_hash=str(row[1]),
            snapshot_month=str(row[2]),
            state=str(row[3]),
            attempted_at=datetime.fromisoformat(str(row[4])).astimezone(timezone.utc),
            analysis_payload_digest=None if row[5] is None else str(row[5]),
            capture_digest=None if row[6] is None else str(row[6]),
            prepared_envelope_digest=None if row[7] is None else str(row[7]),
            completed_envelope_digest=None if row[8] is None else str(row[8]),
            completed_at=(
                None
                if row[9] is None
                else datetime.fromisoformat(str(row[9])).astimezone(timezone.utc)
            ),
            failure_reason=None if row[10] is None else str(row[10]),
        )

    def is_promotable_bau(
        self, profile: SnapshotProfileV1, envelope: object, *, envelope_store=None
    ) -> BauPromotionDecision:
        """Validate a completed scanner-owned envelope before immutable commit.

        This intentionally accepts the envelope rather than presentation output.
        It is read-only: the active-profile check is repeated inside the commit
        transaction by ``commit_bau_snapshot`` below.
        """
        from app.services.backtest.bau_run_envelope import BauRunEnvelopeV1
        from app.services.backtest.reconstruction_roster import CapturedRosterV1

        if not isinstance(envelope, BauRunEnvelopeV1):
            return BauPromotionDecision(False, "BAU envelope has the wrong type")
        if envelope_store is None:
            return BauPromotionDecision(False, "BAU envelope authority is unavailable")
        try:
            if envelope_store.load(envelope.run_id) != envelope:
                return BauPromotionDecision(False, "BAU envelope is not durably owned")
        except Exception:
            return BauPromotionDecision(False, "BAU envelope is not durably owned")
        capture = envelope.capture
        if (
            envelope.outcome != "successful"
            or envelope.completion_state != "completed"
            or capture is None
            or envelope.capture_digest != capture.capture_digest
        ):
            return BauPromotionDecision(False, "BAU envelope is not completed")
        if capture.profile != profile:
            return BauPromotionDecision(False, "BAU envelope profile is incompatible")
        try:
            authority = self.bau_run_authority(envelope.run_id)
            if (
                authority is None
                or authority.state != "completed"
                or authority.profile_hash != profile.profile_hash
                or authority.snapshot_month != capture.snapshot_month
                or authority.analysis_payload_digest != envelope.analysis_payload_digest
                or authority.capture_digest != envelope.capture_digest
                or authority.completed_envelope_digest != envelope.digest()
            ):
                return BauPromotionDecision(
                    False, "BAU run is not durably authoritative"
                )
            self.validate_bau_profile_authority(profile)
            active = self.active_snapshot_profile()
            if active is None or active.profile_hash != profile.profile_hash:
                return BauPromotionDecision(False, "snapshot profile is not active")
            roster_json = self.roster_manifest_json(profile.roster_digest)
            if roster_json is None:
                return BauPromotionDecision(False, "snapshot roster is unavailable")
            roster = CapturedRosterV1.from_json(profile.roster_digest, roster_json)
            expected = tuple((item.security_id, item.mic) for item in roster.members)
            actual = tuple((item.security_id, item.mic) for item in capture.members)
            if actual != expected:
                return BauPromotionDecision(False, "BAU capture roster is incomplete")
            roster_by_id = {item.security_id: item for item in roster.members}
            calendar = TradingCalendar()
            roster_payload = json.loads(roster.canonical_manifest_json)
            alias_revision = str(roster_payload["alias_revision"])
            sessions = {
                mic: calendar.last_session_of_month(mic, capture.snapshot_month)
                for mic in {member.mic for member in capture.members}
            }
            first_eligible = max(
                tuple(
                    stamp.date()
                    for stamp in calendar._calendar(mic).sessions_window(session, 2)
                )[1]
                for mic, session in sessions.items()
            )
            if (
                capture.roster_captured_at > capture.captured_at
                or capture.captured_at.date() != first_eligible
            ):
                return BauPromotionDecision(False, "BAU capture window is incompatible")
            from importlib.metadata import version

            from app.services.backtest.detectors import DETECTOR_REGISTRY
            from app.services.backtest.historical_price_evidence import (
                HistoricalEvidenceRequest,
                request_contract,
            )
            from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
            from app.services.backtest.snapshot_profile import FULL_HISTORY_START
            from app.services.backtest.source_manifest import detector_source_manifests

            runtime_manifests = detector_source_manifests(_PROJECT_ROOT)
            runtime_detectors = {
                item.detector_id: (
                    item.detector_api_version,
                    runtime_manifests[item.detector_id].digest,
                    dict(item.configuration),
                )
                for item in DETECTOR_REGISTRY
            }
            end_year, end_month = (
                int(part) for part in capture.snapshot_month.split("-")
            )
            expected_end = date(end_year + (end_month == 12), end_month % 12 + 1, 1)
            for member in capture.members:
                manifest = member.input_manifest
                raw = member.raw_evidence
                roster_member = roster_by_id[member.security_id]
                expected_session = calendar.last_session_of_month(
                    member.mic, capture.snapshot_month
                )
                evidence_sessions = tuple(
                    date.fromisoformat(str(row["session"])) for row in raw.rows
                )
                expected_timezone = (
                    "Europe/London" if member.mic == "XLON" else "America/New_York"
                )
                expected_scale = "0.01" if roster_member.quote_unit == "GBp" else "1"
                expected_request = request_contract(
                    HistoricalEvidenceRequest(
                        security_id=member.security_id,
                        alias_revision=alias_revision,
                        symbol=roster_member.provider_symbol,
                        start=FULL_HISTORY_START,
                        end=expected_end,
                        expected_currency=roster_member.currency,
                        expected_quote_unit=roster_member.quote_unit,
                        expected_timezone=expected_timezone,
                        expected_sessions=(),
                        allowed_observed_symbols=(roster_member.provider_symbol,),
                        allow_missing_prefix=True,
                    )
                )
                actual_detectors = {
                    item.detector_id: (
                        item.detector_api_version,
                        item.detector_version,
                        dict(item.configuration),
                    )
                    for item in manifest.detectors
                }
                if (
                    member.canonical_session != expected_session
                    or member.source_cutoff != expected_session
                    or not evidence_sessions
                    or evidence_sessions[-1] != expected_session
                    or any(item > member.source_cutoff for item in evidence_sessions)
                    or raw.requested_symbol != roster_member.provider_symbol
                    or raw.observed_symbol != roster_member.provider_symbol
                    or raw.alias_revision != member.alias_revision
                    or member.alias_revision != alias_revision
                    or raw.provider != "yfinance"
                    or raw.provider_version != version("yfinance")
                    or raw.currency != roster_member.currency
                    or raw.quote_unit != roster_member.quote_unit
                    or raw.quote_unit_scale != expected_scale
                    or raw.exchange_timezone != expected_timezone
                    or raw.start != FULL_HISTORY_START
                    or raw.end != expected_end
                    or dict(raw.request_contract) != expected_request
                    or raw.request_contract_version
                    != profile.yfinance_request_contract_version
                    or raw.acquired_at
                    <= calendar.session_close(
                        member.mic, expected_session
                    ).to_pydatetime()
                    or raw.acquired_at.date() != first_eligible
                    or raw.acquired_at > capture.captured_at
                    or manifest.roster_digest != profile.roster_digest
                    or manifest.snapshot_month != capture.snapshot_month
                    or manifest.as_of_session_date != member.canonical_session
                    or manifest.calendar_dataset_version
                    != profile.calendar_dataset_version
                    or manifest.calendar_dataset_digest
                    != profile.calendar_dataset_digest
                    or manifest.yfinance_ingestion_version
                    != profile.yfinance_ingestion_version
                    or manifest.provider_request_contract_version
                    != profile.yfinance_request_contract_version
                    or manifest.provider_data_revision != raw.data_revision
                    or manifest.provider_evidence_manifest_digest != raw.data_revision
                    or manifest.evidence_start != raw.start
                    or manifest.evidence_end != raw.end
                    or manifest.market_plane_policy_version
                    != PRICE_VOLUME_PLANE_VERSION
                    or manifest.market_plane_policy_version
                    != profile.market_plane_policy_version
                    or manifest.record_schema_version != profile.record_schema_version
                    or manifest.reconstructability_policy_version
                    != profile.reconstructability_policy_version
                    or manifest.detector_versions != profile.detector_versions
                    or actual_detectors != runtime_detectors
                ):
                    return BauPromotionDecision(
                        False, "BAU capture authority facts are incompatible"
                    )
        except Exception:
            return BauPromotionDecision(False, "BAU capture authority is invalid")
        return BauPromotionDecision(True)

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

    @classmethod
    def validate_bau_profile_authority(cls, profile: SnapshotProfileV1) -> None:
        """Require the active profile to match every BAU capture runtime policy."""
        from app.services.backtest.historical_data_qualification import (
            REQUEST_CONTRACT_VERSION,
        )
        from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
        from app.services.backtest.source_manifest import (
            yfinance_ingestion_source_manifest,
        )

        cls._validate_profile_authority(profile)
        if (
            profile.yfinance_request_contract_version != REQUEST_CONTRACT_VERSION
            or profile.yfinance_ingestion_version
            != yfinance_ingestion_source_manifest(_PROJECT_ROOT).digest
            or profile.market_plane_policy_version != PRICE_VOLUME_PLANE_VERSION
            or profile.record_schema_version != "historical_scan_record.v1"
            or profile.reconstructability_policy_version != "reconstructability.v1"
        ):
            raise BacktestIntegrityError(
                "snapshot profile source policies do not match BAU runtime authority"
            )

    def commit_snapshot_month(
        self,
        commit: MonthlySnapshotCommitV1,
        evidence_verifier: HistoricalEvidenceVerifier,
        *,
        job_claim: tuple[str, str] | None = None,
        require_active_profile: bool = False,
        lease: WorkerLeaseFenceV1 | None = None,
        adopted_from_profile_hash: str | None = None,
    ) -> SnapshotMonthManifestV1:
        """Atomically compare-and-insert one complete Ready snapshot month.

        ``adopted_from_profile_hash`` records, for Update-mode initialization
        (gh-468), the predecessor data version whose committed month the
        unchanged members were adopted from. It is provenance-only: the
        stored write set is byte-identical to a from-scratch month.
        """
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
                    fence = _lease_fence_params(lease)
                    owned = conn.execute(
                        f"""SELECT 1 FROM strategy_jobs
                           WHERE id=? AND status='running' AND claim_token=?
                           {_LEASE_FENCE_SQL}""",
                        (*job_claim, *fence),
                    ).fetchone()
                    if owned is None:
                        raise StrategyJobConflict(
                            "snapshot publisher no longer owns the job"
                        )
                if require_active_profile:
                    active = conn.execute(
                        "SELECT profile_hash FROM active_snapshot_profile "
                        "WHERE singleton_id=1"
                    ).fetchone()
                    if active is None or str(active[0]) != canonical.profile_hash:
                        raise BacktestIntegrityError("snapshot profile is not active")
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
                        existing_manifest.semantic_content_digest
                        != canonical.manifest.semantic_content_digest
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
                           content_digest, source_run_id, observed_at, committed_at,
                           adopted_from_profile_hash
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        adopted_from_profile_hash,
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
                proof_builder = {
                    "before_first_provider_observation": (
                        build_before_first_provider_observation
                    ),
                    "insufficient_detector_history": (
                        build_insufficient_detector_history
                    ),
                    "incomplete_detector_history": build_incomplete_detector_history,
                }[proof.exclusion_reason]
                rebuilt = proof_builder(
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
            if (
                stored_manifest.semantic_content_digest
                != commit.manifest.semantic_content_digest
            ):
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

    def snapshot_member_revisions(
        self, profile_hash: str, snapshot_month: str
    ) -> tuple[tuple[str, str], ...]:
        """Return immutable winner evidence IDs only after full month validation."""
        with session(self._connect) as conn:
            if (
                self._load_verified_snapshot_month(conn, profile_hash, snapshot_month)
                is None
            ):
                raise BacktestIntegrityError("snapshot month does not exist")
            rows = conn.execute(
                """SELECT security_id, provider_data_revision FROM snapshot_members
                   WHERE profile_hash=? AND snapshot_month=?
                     AND resolution='valid_scan'
                   ORDER BY security_id""",
                (profile_hash, snapshot_month),
            ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def snapshot_month_write_set(
        self, profile_hash: str, snapshot_month: str
    ) -> tuple[tuple[SnapshotMemberV1, ...], tuple[HistoricalScanRecordV1, ...]] | None:
        """Return one committed month's members + records, read-only.

        The adoption seam for Update-mode initialization (gh-468): the
        predecessor month's stored write set, exactly as committed. Returns
        ``None`` when the month is not committed for that profile.
        """
        with session(self._connect) as conn:
            member_rows = conn.execute(
                """SELECT canonical_member_json FROM snapshot_members
                    WHERE profile_hash=? AND snapshot_month=?
                    ORDER BY security_id""",
                (profile_hash, snapshot_month),
            ).fetchall()
            if not member_rows:
                committed = conn.execute(
                    """SELECT 1 FROM snapshot_months
                        WHERE profile_hash=? AND snapshot_month=?""",
                    (profile_hash, snapshot_month),
                ).fetchone()
                if committed is None:
                    return None
            result_rows = conn.execute(
                """SELECT historical_scan_record_json FROM monthly_scan_results
                    WHERE profile_hash=? AND snapshot_month=?
                    ORDER BY security_id""",
                (profile_hash, snapshot_month),
            ).fetchall()
        try:
            members = tuple(
                SnapshotMemberV1.from_canonical_json(str(row[0])) for row in member_rows
            )
            records = tuple(
                HistoricalScanRecordV1.from_canonical_json(str(row[0]))
                for row in result_rows
            )
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored predecessor month write set is invalid"
            ) from exc
        return members, records

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
                self._record_activation_history_on_connection(
                    conn, profile_hash, next_seq
                )
                return active
        except BacktestIntegrityError:
            raise
        except Exception as exc:
            raise BacktestIntegrityError("snapshot profile activation failed") from exc

    @staticmethod
    def _record_activation_history_on_connection(
        conn: sqlite3.Connection, profile_hash: str, activation_seq: int
    ) -> None:
        """Append one activation-audit row beside ``active_snapshot_profile``.

        Same-transaction append keeps the predecessor of an active profile
        discoverable (gh-468). Idempotent for a re-read of an unchanged
        pointer, which must not create a duplicate history row.
        """
        conn.execute(
            """INSERT INTO snapshot_profile_activation_history
                   (profile_hash, activation_seq, activated_at)
               SELECT ?, ?, activated_at FROM active_snapshot_profile
                WHERE singleton_id=1 AND profile_hash=? AND activation_seq=?
               ON CONFLICT(profile_hash, activation_seq) DO NOTHING""",
            (profile_hash, activation_seq, profile_hash, activation_seq),
        )

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

    def previous_snapshot_profile(self, profile_hash: str) -> SnapshotProfileV1 | None:
        """Return the profile activated immediately before ``profile_hash``.

        Resolution order (gh-468): activation history walking backwards to
        the nearest predecessor that owns committed months; for profiles
        activated before that table existed (or when history yields no
        candidate with months), the non-active profile with the most
        recently committed ``snapshot_months``. Returns ``None`` when no
        predecessor can be resolved.
        """
        with session(self._connect) as conn:
            current_seq = conn.execute(
                """SELECT MAX(activation_seq) FROM (
                       SELECT activation_seq
                         FROM snapshot_profile_activation_history
                        WHERE profile_hash=?
                       UNION ALL
                       SELECT activation_seq FROM active_snapshot_profile
                        WHERE singleton_id=1 AND profile_hash=?
                   )""",
                (profile_hash, profile_hash),
            ).fetchone()
            candidates: list[Any] = []
            if current_seq is not None and current_seq[0] is not None:
                candidates = conn.execute(
                    """SELECT profile.canonical_profile_json
                         FROM snapshot_profile_activation_history history
                         JOIN snapshot_profiles profile
                           ON profile.profile_hash = history.profile_hash
                        WHERE history.activation_seq < ?
                          AND history.profile_hash != ?
                        ORDER BY history.activation_seq DESC""",
                    (int(current_seq[0]), profile_hash),
                ).fetchall()
            if not candidates:
                # Pre-history fallback: the non-active profiles with the most
                # recently committed snapshot months, newest first (gh-468).
                candidates = conn.execute(
                    """SELECT profile.canonical_profile_json
                         FROM snapshot_months month
                         JOIN snapshot_profiles profile
                           ON profile.profile_hash = month.profile_hash
                        WHERE month.profile_hash != ?
                          AND month.profile_hash != (
                              SELECT profile_hash FROM active_snapshot_profile
                               WHERE singleton_id=1
                          )
                        GROUP BY month.profile_hash
                        ORDER BY MAX(month.committed_at) DESC""",
                    (profile_hash,),
                ).fetchall()
        for row in candidates:
            try:
                candidate = SnapshotProfileV1.from_canonical_json(str(row[0]))
            except Exception as exc:
                raise BacktestIntegrityError(
                    "stored predecessor snapshot profile is invalid"
                ) from exc
            # Walk back to the nearest predecessor that actually owns
            # committed months; intermediates initialized nothing (gh-468).
            if self.profile_has_committed_months(candidate.profile_hash):
                return candidate
        return None

    def profile_has_committed_months(self, profile_hash: str) -> bool:
        """Return whether one profile owns at least one committed month."""
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT 1 FROM snapshot_months WHERE profile_hash=? LIMIT 1",
                (profile_hash,),
            ).fetchone()
        return row is not None

    def profile_member_delta(
        self, previous_profile_hash: str, next_profile_hash: str
    ) -> ProfileMemberDeltaV1 | None:
        """Return the roster delta between two profiles (gh-468).

        Read-only projection over ``roster_member_identities``; ``None`` when
        either profile does not exist.
        """
        try:
            previous = self.roster_member_identities(previous_profile_hash)
            nxt = self.roster_member_identities(next_profile_hash)
        except BacktestIntegrityError:
            return None
        previous_by_id = {item[0]: item for item in previous}
        next_by_id = {item[0]: item for item in nxt}
        added = tuple(
            item
            for security_id, item in sorted(next_by_id.items())
            if security_id not in previous_by_id or previous_by_id[security_id] != item
        )
        removed = tuple(
            item
            for security_id, item in sorted(previous_by_id.items())
            if security_id not in next_by_id or next_by_id[security_id] != item
        )
        unchanged = tuple(
            item
            for security_id, item in sorted(next_by_id.items())
            if security_id in previous_by_id and previous_by_id[security_id] == item
        )
        return ProfileMemberDeltaV1(
            previous_profile_hash=previous_profile_hash,
            next_profile_hash=next_profile_hash,
            added=added,
            removed=removed,
            unchanged=unchanged,
        )

    def snapshot_coverage(self, profile_hash: str | None = None) -> CoverageSummaryV1:
        with self._snapshot_coverage_lock:
            with session(self._connect) as conn:
                conn.execute("BEGIN")
                selected_hash = profile_hash
                if selected_hash is None:
                    active_row = conn.execute(
                        "SELECT profile_hash FROM active_snapshot_profile "
                        "WHERE singleton_id=1"
                    ).fetchone()
                    if active_row is None:
                        raise BacktestIntegrityError("no active snapshot profile")
                    selected_hash = str(active_row[0])
                revision = self._snapshot_coverage_revision(conn, selected_hash)
                cached = self._snapshot_coverage_cache.get(selected_hash)
                if cached is not None and cached[0] == revision:
                    # Re-run the profile authority check on every hit. The
                    # revision detects database changes; this preserves the
                    # existing runtime-authority failure semantics as well.
                    profile = self._load_snapshot_profile_on_connection(
                        conn, selected_hash
                    )
                    self._validate_profile_authority(profile)
                    return cached[1]

                # A failed integrity check must not leave an older value that
                # could be returned by a later lookup.
                self._snapshot_coverage_cache.pop(selected_hash, None)
                profile = self._load_snapshot_profile_on_connection(conn, selected_hash)
                self._validate_profile_authority(profile)
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
                    raise BacktestIntegrityError(
                        "snapshot coverage evidence is invalid"
                    )
                manifests = tuple(item for item in manifests if item is not None)
                months = tuple(item.snapshot_month for item in manifests)
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
                summary = CoverageSummaryV1(
                    profile_hash=selected_hash,
                    display_version=profile.display_version,
                    earliest_month=None if not months else months[0],
                    latest_month=None if not months else months[-1],
                    snapshot_count=len(months),
                    intervals=self._coverage_intervals(months),
                    provenance=tuple(provenance),
                )
                # Recompute against the same explicit SQLite read snapshot. A
                # writer racing this read cannot cause a partial projection to
                # be published, and its committed revision will miss next time.
                verified_revision = self._snapshot_coverage_revision(
                    conn, selected_hash
                )
                if (
                    selected_hash not in self._snapshot_coverage_cache
                    and len(self._snapshot_coverage_cache)
                    >= self._snapshot_coverage_cache_limit
                ):
                    self._snapshot_coverage_cache.pop(
                        next(iter(self._snapshot_coverage_cache))
                    )
                self._snapshot_coverage_cache[selected_hash] = (
                    verified_revision,
                    summary,
                )
                return summary

    @staticmethod
    def _load_snapshot_profile_on_connection(
        conn: sqlite3.Connection, profile_hash: str
    ) -> SnapshotProfileV1:
        row = conn.execute(
            """SELECT canonical_profile_json, display_version, roster_digest,
                      scanner_schema_version, calendar_dataset_version,
                      calendar_dataset_digest, cadence
               FROM snapshot_profiles WHERE profile_hash=?""",
            (profile_hash,),
        ).fetchone()
        if row is None:
            raise BacktestIntegrityError("snapshot profile does not exist")
        return BacktestRepository._validated_profile_row(profile_hash, row)

    @staticmethod
    def _snapshot_coverage_revision(conn: sqlite3.Connection, profile_hash: str) -> str:
        """Return a cheap identity for all evidence used by coverage reads.

        This intentionally hashes stored bytes and denormalized columns rather
        than parsing them. Thus cache hits avoid month verification, while any
        committed profile, roster, alias, month, member, or result mutation
        changes the identity and forces the normal fail-closed verifier.
        """
        profile_row = conn.execute(
            "SELECT * FROM snapshot_profiles WHERE profile_hash=?", (profile_hash,)
        ).fetchone()
        if profile_row is None:
            raise BacktestIntegrityError("snapshot profile does not exist")
        roster_digest = str(profile_row[3])
        parts: list[str] = []
        for table, query, params in (
            (
                "profile",
                "SELECT * FROM snapshot_profiles WHERE profile_hash=?",
                (profile_hash,),
            ),
            ("active", "SELECT * FROM active_snapshot_profile", ()),
            (
                "months",
                "SELECT * FROM snapshot_months WHERE profile_hash=? ORDER BY snapshot_month",
                (profile_hash,),
            ),
            (
                "members",
                "SELECT * FROM snapshot_members WHERE profile_hash=? ORDER BY snapshot_month, security_id",
                (profile_hash,),
            ),
            (
                "results",
                "SELECT * FROM monthly_scan_results WHERE profile_hash=? ORDER BY snapshot_month, security_id",
                (profile_hash,),
            ),
            (
                "roster",
                "SELECT * FROM reconstruction_rosters WHERE roster_digest=?",
                (roster_digest,),
            ),
            (
                "roster_members",
                "SELECT * FROM reconstruction_roster_members WHERE roster_digest=? ORDER BY security_id",
                (roster_digest,),
            ),
        ):
            parts.append(table)
            parts.extend(repr(tuple(row)) for row in conn.execute(query, params))
        alias_revision = conn.execute(
            "SELECT alias_revision FROM reconstruction_rosters WHERE roster_digest=?",
            (roster_digest,),
        ).fetchone()
        if alias_revision is not None:
            parts.extend(
                [
                    "aliases",
                    *(
                        repr(tuple(row))
                        for row in conn.execute(
                            "SELECT * FROM security_alias_manifests WHERE alias_revision=?",
                            (str(alias_revision[0]),),
                        )
                    ),
                ]
            )
            parts.extend(
                repr(tuple(row))
                for row in conn.execute(
                    "SELECT * FROM security_alias_entries WHERE alias_revision=? ORDER BY provider, mic, observed_symbol",
                    (str(alias_revision[0]),),
                )
            )
        return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

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

    def latest_committed_scan_result(
        self, *, profile_hash: str, security_id: str, as_of_session: date
    ) -> HistoricalScanRecordV1 | None:
        """Return the latest committed monthly scan record visible at a session.

        ``MarketView.scan_result`` (Story 2.3) is the one caller: a
        monthly scan candidate enters visibility only from its own
        recorded month-end ``as_of_session_date`` onward and remains the
        answer until superseded by ``security_id``'s next committed
        month, so this picks the newest committed ``valid_scan`` member
        with ``as_of_session_date <= as_of_session`` -- never a record
        from a month that has not itself been fully committed
        (``snapshot_months`` is the append-only commit ledger; a month
        absent from it, or still mid-write, is invisible here). Returns
        ``None`` when no such record exists yet -- "not visible yet" is
        not a bound violation.
        """
        with session(self._connect) as conn:
            row = conn.execute(
                """
                SELECT r.snapshot_month, r.historical_scan_record_json, r.record_digest
                FROM monthly_scan_results r
                JOIN snapshot_months m
                  ON m.profile_hash = r.profile_hash
                 AND m.snapshot_month = r.snapshot_month
                JOIN snapshot_members mem
                  ON mem.profile_hash = r.profile_hash
                 AND mem.snapshot_month = r.snapshot_month
                 AND mem.security_id = r.security_id
                WHERE r.profile_hash = ?
                  AND r.security_id = ?
                  AND mem.resolution = 'valid_scan'
                  AND mem.as_of_session_date <= ?
                  AND m.processing_complete = 1
                  AND m.market_complete = 'unknown'
                ORDER BY mem.as_of_session_date DESC, r.snapshot_month DESC
                LIMIT 1
                """,
                (profile_hash, security_id, as_of_session.isoformat()),
            ).fetchone()
        if row is None:
            return None
        try:
            record = HistoricalScanRecordV1.from_canonical_json(str(row[1]))
        except Exception as exc:
            raise BacktestIntegrityError(
                "stored monthly scan result is invalid"
            ) from exc
        if (
            record.security_id != security_id
            or record.snapshot_month != str(row[0])
            or record.digest() != str(row[2])
        ):
            raise BacktestIntegrityError("stored monthly scan result is invalid")
        return record

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

    def commit_roster_capture(
        self,
        commit: RosterCaptureCommit,
        *,
        job_claim: tuple[str, str, int] | None = None,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> str:
        """Atomically compare-and-insert a complete roster capture."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if job_claim is not None:
                job_id, claim_token, expected_version = job_claim
                fence = _lease_fence_params(lease)
                owned = conn.execute(
                    f"""SELECT 1 FROM strategy_jobs
                        WHERE id=? AND job_type='bootstrap' AND status='running'
                          AND claim_token=? AND status_version=?
                          AND current_stage='roster_capture'
                          AND cancel_requested_at IS NULL {_LEASE_FENCE_SQL}""",
                    (job_id, claim_token, expected_version, *fence),
                ).fetchone()
                if owned is None or commit.lineage_id != job_id:
                    raise StrategyJobConflict(
                        "bootstrap roster capture ownership is stale"
                    )
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
                row = conn.execute(
                    """SELECT security_id, evidence_digest FROM security_identities
                       WHERE mic=? AND provider_symbol=?""",
                    (mic, symbol),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO security_identities
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
                    row = (security_id, evidence_digest)
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
