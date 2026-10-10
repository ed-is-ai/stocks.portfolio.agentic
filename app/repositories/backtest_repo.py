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
import re
import sqlite3
from threading import RLock
from typing import (
    Any,
    Callable,
    Literal,
    Mapping,
    NamedTuple,
    Protocol,
    TYPE_CHECKING,
    cast,
    overload,
)
from collections.abc import Iterable
from uuid import uuid4
import zlib

from pydantic import ValidationError

from app.repositories.db import Connect, evidence_connect, session
from app.services.backtest.historical_scan_record import (
    DetectorFragmentEnvelopeV1,
    HistoricalScanRecordV1,
    HistoricalScanContractError,
)
from app.services.backtest.canonical_manifest import (
    canonical_json as render_canonical_json,
    manifest_digest,
)
from app.services.backtest.security_identity import (
    AliasEntryV1,
    SecurityAliasManifestV1,
    SecurityIdentityRegistryV1,
    SecurityIdentityV1,
)
from app.services.backtest.snapshot_profile import (
    ActiveSnapshotProfileV1,
    CoverageIntervalV1,
    CoverageSummaryV1,
    HistoricalEvidenceV1,
    IntervalReadinessV1,
    MonthlySnapshotCommitV1,
    NoProviderDataProofV1,
    ProvenanceCoverageV1,
    SnapshotMemberV1,
    SnapshotMonthManifestV1,
    SnapshotProfileV1,
    SnapshotContractError,
    build_before_first_provider_observation,
    build_incomplete_detector_history,
    build_insufficient_detector_history,
    provider_request_contract_version,
    verified_evidence_manifest,
)
from app.services.backtest.point_in_time_membership import (
    POINT_IN_TIME_POLICY_VERSION,
    MembershipIntervals,
    month_members,
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
    InitializationProgressV1,
    InitializationRunV1,
    JobFailureCode,
    PreparationRunV1,
    PreparationSubmissionV1,
    PreparationEnqueueResultV1,
    RegimeBenchmarkPinV1,
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
        CandidateAuditV1,
        EquityCurvePointV1,
        TradeLogEvent,
    )
    from app.services.backtest.metrics import BacktestMetricsV1, MetricAvailabilityV1

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
_db_session = session


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

-- Reference-only instruments have a separate closed MIC contract.  Keeping
-- this storage additive preserves the immutable tradable identity/alias
-- tables and, especially, the snapshot-member MIC constraint.
CREATE TABLE IF NOT EXISTS reference_identity_registry_revisions (
    revision_digest TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_security_identities (
    security_id TEXT PRIMARY KEY,
    mic TEXT NOT NULL CHECK(mic = 'ARCX'),
    provider_symbol TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    identity_registry_revision TEXT NOT NULL
        REFERENCES reference_identity_registry_revisions(revision_digest),
    created_at TEXT NOT NULL,
    UNIQUE(mic, provider_symbol)
);
CREATE TABLE IF NOT EXISTS reference_alias_manifests (
    alias_revision TEXT PRIMARY KEY,
    canonical_manifest_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_alias_entries (
    alias_revision TEXT NOT NULL REFERENCES reference_alias_manifests(alias_revision),
    security_id TEXT NOT NULL REFERENCES reference_security_identities(security_id),
    provider TEXT NOT NULL,
    mic TEXT NOT NULL CHECK(mic = 'ARCX'),
    observed_symbol TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    evidence_source TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('provider_evidence', 'manual_override')),
    PRIMARY KEY(alias_revision, provider, mic, observed_symbol, effective_from, effective_to, security_id),
    CHECK(effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to)
);
CREATE TRIGGER IF NOT EXISTS reference_identity_registry_immutable_update
BEFORE UPDATE ON reference_identity_registry_revisions
BEGIN SELECT RAISE(ABORT, 'reference identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_identity_registry_immutable_delete
BEFORE DELETE ON reference_identity_registry_revisions
BEGIN SELECT RAISE(ABORT, 'reference identity registry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_security_identity_immutable_update
BEFORE UPDATE ON reference_security_identities
BEGIN SELECT RAISE(ABORT, 'reference security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_security_identity_immutable_delete
BEFORE DELETE ON reference_security_identities
BEGIN SELECT RAISE(ABORT, 'reference security identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_alias_manifest_immutable_update
BEFORE UPDATE ON reference_alias_manifests
BEGIN SELECT RAISE(ABORT, 'reference alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_alias_manifest_immutable_delete
BEFORE DELETE ON reference_alias_manifests
BEGIN SELECT RAISE(ABORT, 'reference alias manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_alias_entry_immutable_update
BEFORE UPDATE ON reference_alias_entries
BEGIN SELECT RAISE(ABORT, 'reference alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_alias_entry_immutable_delete
BEFORE DELETE ON reference_alias_entries
BEGIN SELECT RAISE(ABORT, 'reference alias entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reference_alias_no_overlap
BEFORE INSERT ON reference_alias_entries
WHEN EXISTS (
    SELECT 1 FROM reference_alias_entries existing
    WHERE existing.provider = NEW.provider
      AND existing.alias_revision = NEW.alias_revision
      AND existing.mic = NEW.mic
      AND existing.observed_symbol = NEW.observed_symbol
      AND COALESCE(existing.effective_from, '0001-01-01') < COALESCE(NEW.effective_to, '9999-12-31')
      AND COALESCE(NEW.effective_from, '0001-01-01') < COALESCE(existing.effective_to, '9999-12-31')
)
BEGIN SELECT RAISE(ABORT, 'reference alias intervals overlap'); END;

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
    source_name TEXT NOT NULL CHECK(source_name IN ('datahub_sp500', 'tradingview_us', 'tradingview_uk', 'sp500_point_in_time')),
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
        'incomplete_detector_history',
        'no_provider_data'
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
             'incomplete_detector_history',
             'no_provider_data'
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
-- latest_committed_scan_result() filters (profile_hash, security_id) and
-- joins snapshot_month rather than binding it as a literal, so the PK's
-- leftmost prefix stops at profile_hash -- without this, every lookup
-- scans all months for the profile instead of seeking straight to the
-- security's rows.
CREATE INDEX IF NOT EXISTS idx_monthly_scan_results_profile_security
ON monthly_scan_results(profile_hash, security_id, snapshot_month DESC);

CREATE TRIGGER IF NOT EXISTS snapshot_profile_immutable_update BEFORE UPDATE ON snapshot_profiles BEGIN SELECT RAISE(ABORT, 'snapshot profile is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_profile_immutable_delete BEFORE DELETE ON snapshot_profiles BEGIN SELECT RAISE(ABORT, 'snapshot profile is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_month_immutable_update BEFORE UPDATE ON snapshot_months BEGIN SELECT RAISE(ABORT, 'snapshot month is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_month_immutable_delete BEFORE DELETE ON snapshot_months BEGIN SELECT RAISE(ABORT, 'snapshot month is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_member_immutable_update BEFORE UPDATE ON snapshot_members BEGIN SELECT RAISE(ABORT, 'snapshot member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_member_immutable_delete BEFORE DELETE ON snapshot_members BEGIN SELECT RAISE(ABORT, 'snapshot member is immutable'); END;
CREATE TRIGGER IF NOT EXISTS monthly_scan_result_immutable_update BEFORE UPDATE ON monthly_scan_results BEGIN SELECT RAISE(ABORT, 'monthly scan result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS monthly_scan_result_immutable_delete BEFORE DELETE ON monthly_scan_results BEGIN SELECT RAISE(ABORT, 'monthly scan result is immutable'); END;
DROP TRIGGER IF EXISTS snapshot_member_requires_roster_identity;
CREATE TRIGGER snapshot_member_requires_roster_identity
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
     AND alias.provider IN ('yfinance', 'wiki')
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

CREATE TABLE IF NOT EXISTS initialization_progress (
    job_id TEXT PRIMARY KEY REFERENCES initialization_runs(job_id),
    committed_months INTEGER NOT NULL CHECK(committed_months >= 0),
    reused_months INTEGER NOT NULL CHECK(reused_months >= 0),
    fetched_months INTEGER NOT NULL CHECK(fetched_months >= 0),
    partial_months INTEGER NOT NULL CHECK(partial_months >= 0),
    reused_securities INTEGER NOT NULL CHECK(reused_securities >= 0),
    fetched_securities INTEGER NOT NULL CHECK(fetched_securities >= 0),
    fresh_elapsed_seconds REAL NOT NULL CHECK(fresh_elapsed_seconds >= 0),
    fresh_months INTEGER NOT NULL CHECK(fresh_months >= 0),
    last_committed_month TEXT NOT NULL,
    last_committed_at TEXT NOT NULL,
    CHECK(committed_months = reused_months + fetched_months + partial_months)
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
    job_id TEXT PRIMARY KEY REFERENCES strategy_jobs(id),
    regime_benchmark_json TEXT
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
    updated_at TEXT NOT NULL,
    last_batch_sequence INTEGER NOT NULL DEFAULT 0
        CHECK(last_batch_sequence >= 0),
    last_session TEXT,
    last_event_sequence INTEGER NOT NULL DEFAULT 0
        CHECK(last_event_sequence >= 0),
    last_equity_sequence INTEGER NOT NULL DEFAULT 0
        CHECK(last_equity_sequence >= 0)
);

-- GH-616: one bounded, attempt-owned payload per published session. The
-- checkpoint above remains the latest portfolio state; it never accumulates
-- events/equity on the append path.
CREATE TABLE IF NOT EXISTS backtest_staging_batches (
    run_id TEXT NOT NULL REFERENCES backtest_staging(run_id) ON DELETE CASCADE,
    batch_sequence INTEGER NOT NULL CHECK(batch_sequence > 0),
    session TEXT NOT NULL CHECK(length(session) = 10),
    payload_encoding TEXT NOT NULL CHECK(payload_encoding = 'json+zlib.v1'),
    payload_blob BLOB NOT NULL CHECK(length(payload_blob) > 0),
    uncompressed_bytes INTEGER NOT NULL CHECK(uncompressed_bytes > 0),
    payload_digest TEXT NOT NULL CHECK(
        length(payload_digest) = 64
        AND payload_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, batch_sequence),
    UNIQUE(run_id, session)
);

CREATE TABLE IF NOT EXISTS backtest_staging_audit_batches (
    run_id TEXT NOT NULL,
    batch_sequence INTEGER NOT NULL,
    session TEXT NOT NULL CHECK(length(session) = 10),
    audit_contract_version TEXT NOT NULL
        CHECK(audit_contract_version = 'candidate_allocation_audit.v1'),
    candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
    audit_digest TEXT NOT NULL CHECK(length(audit_digest) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, batch_sequence),
    FOREIGN KEY(run_id, batch_sequence)
        REFERENCES backtest_staging_batches(run_id, batch_sequence) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS backtest_staging_audit_contracts (
    run_id TEXT PRIMARY KEY,
    audit_contract_version TEXT NOT NULL
        CHECK(audit_contract_version IN ('none', 'candidate_allocation_audit.v1'))
);
CREATE TRIGGER IF NOT EXISTS backtest_staging_delete_audit_contract
AFTER DELETE ON backtest_staging
BEGIN
    DELETE FROM backtest_staging_audit_contracts WHERE run_id=OLD.run_id;
END;
CREATE TABLE IF NOT EXISTS backtest_staging_candidate_audits (
    run_id TEXT NOT NULL,
    batch_sequence INTEGER NOT NULL,
    candidate_sequence INTEGER NOT NULL CHECK(candidate_sequence > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    PRIMARY KEY(run_id, candidate_sequence),
    FOREIGN KEY(run_id, batch_sequence)
        REFERENCES backtest_staging_audit_batches(run_id, batch_sequence) ON DELETE CASCADE
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
    audit_contract_version TEXT NOT NULL DEFAULT 'none'
        CHECK(audit_contract_version IN ('none', 'candidate_allocation_audit.v1')),
    metrics_json TEXT NOT NULL,
    final_cash_base TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK(length(result_digest) = 64),
    note TEXT,
    note_version INTEGER NOT NULL CHECK(note_version > 0),
    completed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_result_audit_manifests (
    run_id TEXT PRIMARY KEY REFERENCES backtest_results(run_id),
    audit_contract_version TEXT NOT NULL
        CHECK(audit_contract_version = 'candidate_allocation_audit.v1'),
    candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
    summary_json TEXT NOT NULL,
    audit_digest TEXT NOT NULL CHECK(length(audit_digest) = 64),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_result_candidate_audits (
    run_id TEXT NOT NULL REFERENCES backtest_result_audit_manifests(run_id),
    candidate_sequence INTEGER NOT NULL CHECK(candidate_sequence > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK(length(payload_digest) = 64),
    PRIMARY KEY(run_id, candidate_sequence)
);
CREATE TRIGGER IF NOT EXISTS backtest_result_audit_manifest_immutable_update
BEFORE UPDATE ON backtest_result_audit_manifests
BEGIN SELECT RAISE(ABORT, 'backtest result audit manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_audit_manifest_immutable_delete
BEFORE DELETE ON backtest_result_audit_manifests
BEGIN SELECT RAISE(ABORT, 'backtest result audit manifest is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_candidate_audit_immutable_update
BEFORE UPDATE ON backtest_result_candidate_audits
BEGIN SELECT RAISE(ABORT, 'backtest result candidate audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS backtest_result_candidate_audit_immutable_delete
BEFORE DELETE ON backtest_result_candidate_audits
BEGIN SELECT RAISE(ABORT, 'backtest result candidate audit is immutable'); END;

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
  OR NEW.audit_contract_version != OLD.audit_contract_version
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
        'dividend_applied', 'open_position_mark', 'terminal_settlement'
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


_STRATEGY_EXPERIMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_experiments (
    id TEXT PRIMARY KEY,
    baseline_run_id TEXT NOT NULL REFERENCES strategy_jobs(id),
    draft_digest TEXT NOT NULL UNIQUE CHECK(length(draft_digest) = 64),
    draft_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'approved', 'discarded', 'complete', 'inconclusive'
    )),
    candidate_run_id TEXT UNIQUE REFERENCES strategy_jobs(id),
    approval_json TEXT,
    comparison_json TEXT,
    conclusion_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((status = 'draft' AND candidate_run_id IS NULL AND approval_json IS NULL)
       OR (status = 'discarded' AND candidate_run_id IS NULL AND approval_json IS NULL)
       OR (status IN ('approved', 'complete', 'inconclusive')
           AND candidate_run_id IS NOT NULL AND approval_json IS NOT NULL)),
    CHECK((status IN ('complete', 'inconclusive')) = (conclusion_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS strategy_experiment_baseline
ON strategy_experiments(baseline_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS strategy_experiment_status
ON strategy_experiments(status, created_at DESC);
CREATE TABLE IF NOT EXISTS strategy_experiment_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT REFERENCES strategy_experiments(id),
    baseline_run_id TEXT,
    candidate_run_id TEXT,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_experiment_audit_experiment
ON strategy_experiment_audit(experiment_id, sequence);
CREATE TRIGGER IF NOT EXISTS strategy_experiment_draft_immutable
BEFORE UPDATE ON strategy_experiments
WHEN NEW.id != OLD.id
  OR NEW.baseline_run_id != OLD.baseline_run_id
  OR NEW.draft_digest != OLD.draft_digest
  OR NEW.draft_json != OLD.draft_json
  OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'strategy experiment draft is immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_legal_transition
BEFORE UPDATE ON strategy_experiments
WHEN NOT (
    (OLD.status = 'draft' AND NEW.status IN ('approved', 'discarded'))
    OR (OLD.status = 'approved' AND NEW.status IN ('complete', 'inconclusive'))
)
BEGIN SELECT RAISE(ABORT, 'illegal strategy experiment transition'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_immutable_delete
BEFORE DELETE ON strategy_experiments
BEGIN SELECT RAISE(ABORT, 'strategy experiment is immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_audit_immutable_update
BEFORE UPDATE ON strategy_experiment_audit
BEGIN SELECT RAISE(ABORT, 'strategy experiment audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS strategy_experiment_audit_immutable_delete
BEFORE DELETE ON strategy_experiment_audit
BEGIN SELECT RAISE(ABORT, 'strategy experiment audit is append-only'); END;
"""


_STRATEGY_MANAGER_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_manager_agent_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL CHECK(task IN ('insights', 'question')),
    request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
    summary_digest TEXT NOT NULL CHECK(length(summary_digest) = 64),
    prompt_version TEXT NOT NULL CHECK(length(prompt_version) BETWEEN 1 AND 80),
    schema_version TEXT NOT NULL CHECK(length(schema_version) BETWEEN 1 AND 80),
    user_question TEXT CHECK(user_question IS NULL OR length(user_question) <= 500),
    attempts_json TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('accepted', 'unavailable')),
    accepted_citations_json TEXT NOT NULL,
    output_json TEXT,
    occurred_at TEXT NOT NULL,
    CHECK((task = 'question') = (user_question IS NOT NULL)),
    CHECK((outcome = 'accepted') = (output_json IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS strategy_manager_agent_cache
ON strategy_manager_agent_audit(task, request_digest, outcome, sequence DESC);
CREATE TRIGGER IF NOT EXISTS strategy_manager_agent_audit_immutable_update
BEFORE UPDATE ON strategy_manager_agent_audit
BEGIN SELECT RAISE(ABORT, 'strategy manager agent audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS strategy_manager_agent_audit_immutable_delete
BEFORE DELETE ON strategy_manager_agent_audit
BEGIN SELECT RAISE(ABORT, 'strategy manager agent audit is append-only'); END;
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


@dataclass(frozen=True)
class ReferenceIdentityRegistrationV1:
    """The immutable revisions created for one reference-only instrument."""

    identity: SecurityIdentityV1
    alias: AliasEntryV1
    identity_registry_revision: str
    alias_revision: str


class BacktestIntegrityError(RuntimeError):
    def __init__(self, message: str, *, code: str = "integrity_error") -> None:
        self.code = code
        super().__init__(message)


#: Unicode code-point cap on a Backtest Result note's escaped, persisted
#: text (AC 5) -- checked after ``html.escape`` (whose entity expansion can
#: grow the text) and before persistence, never truncated silently.
_NOTE_MAX_CODE_POINTS = 10_000
_BACKTEST_STAGING_BATCH_ENCODING = "json+zlib.v1"
_BACKTEST_CANDIDATE_AUDIT_CONTRACT = "candidate_allocation_audit.v1"


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
    regime_benchmark: "RegimeBenchmarkPinV1 | None" = None


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
class BacktestStagingBatchV1:
    """One decoded, ordered session delta from an attempt-owned batch."""

    run_id: str
    batch_sequence: int
    session: date
    events: tuple[TradeLogEvent, ...]
    equity_point: EquityCurvePointV1
    payload_encoding: str
    uncompressed_bytes: int
    payload_digest: str
    created_at: str


@dataclass(frozen=True)
class BacktestStagingCheckpointV1:
    """The latest portfolio checkpoint paired with published batches."""

    run_id: str
    state_schema_version: str
    portfolio_state: dict[str, object]
    final_cash_base: Decimal
    last_batch_sequence: int
    last_session: date | None
    last_event_sequence: int
    last_equity_sequence: int
    updated_at: str
    initial_entry_selection: InitialEntrySelectionV1 | None = None


@dataclass(frozen=True)
class BacktestCandidateAuditSummaryV1:
    """Integrity-bound coverage and allocation counts for one Result."""

    recorded: bool
    contract_version: str
    candidate_count: int = 0
    priority_recorded: int = 0
    priority_missing: int = 0
    explanation_recorded: int = 0
    explanation_missing: int = 0
    preflight_rejected: int = 0
    full_book_rejected: int = 0
    competition_rejected: int = 0
    filled: int = 0
    fill_rejected: int = 0
    audit_digest: str | None = None


@dataclass(frozen=True)
class BacktestCandidateAuditPromotionV1:
    """Validated audit manifest data ready to promote with a Result."""

    summary: dict[str, int]
    audit_digest: str


@dataclass(frozen=True)
class BacktestCandidateAuditPageV1:
    summary: BacktestCandidateAuditSummaryV1
    page: int
    page_size: int
    total_pages: int
    records: tuple["CandidateAuditV1", ...]


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
    regime_benchmark: "RegimeBenchmarkPinV1 | None" = None
    initial_entry_selection: InitialEntrySelectionV1 | None = None
    candidate_audit_summary: BacktestCandidateAuditSummaryV1 | None = None


@dataclass(frozen=True)
class RecentBacktestResultsV1:
    """A bounded set of verified Result candidates and exclusion counts."""

    results: tuple[BacktestResultV1, ...]
    inspected_count: int
    integrity_excluded_count: int
    missing_result_count: int
    job_exclusion_counts: tuple[tuple[str, int], ...]


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
    #: Why a complete job's stored Result failed verification (no Metrics).
    result_error: str | None = None


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


def _row_to_initialization_progress(
    row: sqlite3.Row | tuple[object, ...],
) -> InitializationProgressV1:
    return InitializationProgressV1(
        job_id=str(row[0]),
        committed_months=int(str(row[1])),
        reused_months=int(str(row[2])),
        fetched_months=int(str(row[3])),
        partial_months=int(str(row[4])),
        reused_securities=int(str(row[5])),
        fetched_securities=int(str(row[6])),
        fresh_elapsed_seconds=float(str(row[7])),
        fresh_months=int(str(row[8])),
        last_committed_month=str(row[9]),
        last_committed_at=datetime.fromisoformat(str(row[10])),
    )


#: Roster sources screened live; a V2 BAU month observes only their members.
CURRENT_ROSTER_SOURCES = frozenset(
    {"datahub_sp500", "tradingview_us", "tradingview_uk"}
)


def is_current_source(source_memberships: Iterable[str]) -> bool:
    """Whether a roster member comes from a live (current) roster source."""
    return not CURRENT_ROSTER_SOURCES.isdisjoint(source_memberships)


class _PointInTimeRosterMember(NamedTuple):
    security_id: str
    mic: str
    provider: str
    membership_intervals: MembershipIntervals
    source_memberships: tuple[str, ...]


def _source_memberships(item: Mapping[str, object]) -> tuple[str, ...]:
    """Return a manifest member's source names; reject anything else."""
    sources = item.get("source_memberships")
    if not isinstance(sources, list) or not all(isinstance(x, str) for x in sources):
        raise ValueError("roster member source memberships are malformed")
    return tuple(sources)


def _utc_month(instant: str) -> str:
    """Return the ``YYYY-MM`` UTC month of an ISO-8601 instant."""
    return datetime.fromisoformat(instant).astimezone(timezone.utc).strftime("%Y-%m")


# ponytail: unbounded, but rosters are immutable and few per process.
_POINT_IN_TIME_ROSTERS: dict[str, tuple[_PointInTimeRosterMember, ...]] = {}


def _point_in_time_roster_members(
    conn: sqlite3.Connection, roster_digest: str
) -> tuple[_PointInTimeRosterMember, ...]:
    """Return a point-in-time roster's members, parsed once per digest (#82)."""
    cached = _POINT_IN_TIME_ROSTERS.get(roster_digest)
    if cached is not None:
        return cached
    row = conn.execute(
        """SELECT canonical_manifest_json FROM reconstruction_rosters
           WHERE roster_digest=?""",
        (roster_digest,),
    ).fetchone()
    try:
        manifest = json.loads(str(row[0]))
        if manifest["policy_version"] != POINT_IN_TIME_POLICY_VERSION:
            raise ValueError("roster manifest policy differs from its row")
        members = tuple(
            _PointInTimeRosterMember(
                security_id=str(item["security_id"]),
                mic=str(item["mic"]),
                provider=str(item.get("provider") or ""),
                membership_intervals=tuple(
                    (str(start), None if end is None else str(end))
                    for start, end in item.get("membership_intervals") or ()
                ),
                source_memberships=_source_memberships(item),
            )
            for item in manifest["members"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BacktestIntegrityError(
            "point-in-time reconstruction roster is invalid"
        ) from exc
    _POINT_IN_TIME_ROSTERS[roster_digest] = members
    return members


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


#: The three-reason exclusion vocabulary that predates ``no_provider_data``.
_PRE_NO_PROVIDER_DATA_REASONS = re.compile(
    r"'before_first_provider_observation',(\s*)'insufficient_detector_history',"
    r"\s*'incomplete_detector_history'"
)


def _migrate_snapshot_no_provider_data_check(conn: sqlite3.Connection) -> None:
    """Admit ``no_provider_data`` in both ``snapshot_members`` CHECKs (#82 C4).

    Widening a CHECK changes no stored row, so the DDL text is rewritten in
    place through ``writable_schema`` and ``schema_version`` is bumped, as
    ``HistoricalPriceRepository._migrate_provider_check`` does: the real
    table is far too large to copy. Idempotent; unrecognised DDL raises.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshot_members'"
    ).fetchone()
    if row is None or str(row[0]).count("'no_provider_data'") == 2:
        return
    widened, count = _PRE_NO_PROVIDER_DATA_REASONS.subn(
        lambda match: f"{match.group(0)},{match.group(1)}'no_provider_data'",
        str(row[0]),
    )
    if count != 2 or "'no_provider_data'" in str(row[0]):
        raise BacktestIntegrityError(
            "snapshot_members has an unrecognised exclusion CHECK"
        )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA writable_schema = ON")
    try:
        # Re-read under the write lock: another process may have migrated.
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='snapshot_members'"
        ).fetchone()
        if sql == str(row[0]):
            (version,) = conn.execute("PRAGMA schema_version").fetchone()
            conn.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type = 'table' "
                "AND name = 'snapshot_members'",
                (widened,),
            )
            conn.execute(f"PRAGMA schema_version = {int(version) + 1}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA writable_schema = OFF")


def _migrate_trade_log_kind_constraint(conn: sqlite3.Connection) -> None:
    """Admit ``terminal_settlement`` trade log rows (#82) without data loss."""
    table, replacement = "trade_log", "trade_log__kind_migration"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    stale = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (replacement,)
    ).fetchone()
    legacy = row is not None and "'terminal_settlement'" not in str(row[0])
    if not legacy and stale is None:
        return

    before = len(conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchall())
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if stale is not None:
            conn.execute(f'DROP TABLE "{replacement}"')
        if legacy and row is not None:
            dependents = conn.execute(
                """SELECT type, name, sql FROM sqlite_master
                   WHERE type IN ('trigger', 'index') AND sql IS NOT NULL
                     AND (tbl_name=? OR instr(sql, ?) > 0)""",
                (table, table),
            ).fetchall()
            columns = ", ".join(
                f'"{item[1]}"'
                for item in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            create_sql = (
                str(row[0])
                .replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{replacement}"', 1)
                .replace(f"CREATE TABLE {table}", f"CREATE TABLE {replacement}", 1)
                .replace(
                    "'open_position_mark'",
                    "'open_position_mark', 'terminal_settlement'",
                    1,
                )
            )
            rewritten = replacement in create_sql
            if not rewritten or "'terminal_settlement'" not in create_sql:
                raise sqlite3.DatabaseError("unrecognised trade_log schema")
            conn.execute(create_sql)
            conn.execute(
                f'INSERT INTO "{replacement}" ({columns}) '
                f'SELECT {columns} FROM "{table}"'
            )
            for kind, name, _sql in dependents:
                conn.execute(f'DROP {str(kind).upper()} "{name}"')
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
            for _kind, _name, sql in dependents:
                conn.execute(str(sql))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # Only violations the rebuild introduced fail it (rowids are renumbered,
    # so compare counts); older orphans stay as they were.
    after = len(conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchall())
    if after > before:
        raise sqlite3.IntegrityError("trade log kind migration violated foreign keys")


def _migrate_roster_source_constraint(conn: sqlite3.Connection) -> None:
    """Admit the ``sp500_point_in_time`` roster source (#82) without data loss."""
    table = "reconstruction_roster_sources"
    replacement = f"{table}__pit_migration"
    legacy = "'tradingview_uk')"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    stale = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (replacement,)
    ).fetchone()
    sql = "" if row is None else str(row[0])
    pending = legacy in sql
    if sql and not pending and "'sp500_point_in_time'" not in sql:
        raise sqlite3.DatabaseError("unrecognised roster source schema")
    if not pending and stale is None:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if stale is not None:
            conn.execute(f'DROP TABLE "{replacement}"')
        if pending and row is not None:
            triggers = conn.execute(
                """SELECT name, sql FROM sqlite_master
                   WHERE type='trigger' AND sql IS NOT NULL
                     AND (tbl_name=? OR instr(sql, ?) > 0)""",
                (table, table),
            ).fetchall()
            columns = ", ".join(
                f'"{item[1]}"'
                for item in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            create_sql = (
                str(row[0])
                .replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{replacement}"', 1)
                .replace(f"CREATE TABLE {table}", f"CREATE TABLE {replacement}", 1)
                .replace(legacy, "'tradingview_uk', 'sp500_point_in_time')", 1)
            )
            if replacement not in create_sql:
                raise sqlite3.DatabaseError("unrecognised roster source schema")
            conn.execute(create_sql)
            conn.execute(
                f'INSERT INTO "{replacement}" ({columns}) '
                f'SELECT {columns} FROM "{table}"'
            )
            for name, _sql in triggers:
                conn.execute(f'DROP TRIGGER "{name}"')
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
            for _name, sql in triggers:
                conn.execute(str(sql))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    if conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchall():
        raise sqlite3.IntegrityError("roster source migration violated foreign keys")


def _ensure_trigger(conn: sqlite3.Connection, definition: str) -> None:
    """Replace a migrated trigger only when its stored definition differs."""
    definition = definition.strip().rstrip(";")
    name = definition.split()[2]
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    if existing is not None and str(existing[0]).strip().rstrip(";") == definition:
        return
    conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute(definition)


def _execute_schema(conn: sqlite3.Connection, script: str) -> None:
    """Execute schema in the caller's transaction without redundant trigger DDL."""
    statement = ""
    pending_drop = None
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        if pending_drop is not None:
            name = pending_drop.split()[4].rstrip(";")
            if sql.split()[:3] != ["CREATE", "TRIGGER", name]:
                conn.execute(pending_drop)
            pending_drop = None
        if sql.startswith("DROP TRIGGER IF EXISTS "):
            # Skip only paired replacement drops, never intentional removals.
            pending_drop = sql
            continue
        if sql.startswith("CREATE TRIGGER ") and not sql.startswith(
            "CREATE TRIGGER IF NOT EXISTS "
        ):
            _ensure_trigger(conn, sql)
        else:
            conn.execute(sql)
    if statement.strip():
        raise ValueError("incomplete repository schema statement")
    if pending_drop is not None:
        conn.execute(pending_drop)


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
        self._connect = evidence_connect(connect)
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

    def ensure_schema(self) -> None:
        with session(self._connect) as conn:
            # WAL lets Strategy Manager's read-heavy tab rendering proceed
            # alongside the orchestrator's short lease/write transactions.
            # The mode is durable database state, but issuing it here also
            # upgrades existing rollback-journal databases at startup.
            conn.execute("PRAGMA journal_mode = WAL")
            # Preserve schema_version across unchanged startups so durable
            # verification remains reusable. Changed triggers still migrate
            # under SQLite's cross-process write lock.
            conn.execute("BEGIN IMMEDIATE")
            _execute_schema(
                conn,
                _QUALIFICATION_SCHEMA
                + _ROSTER_SCHEMA
                + _SCAN_RECONSTRUCTION_CACHE_SCHEMA
                + _SNAPSHOT_COVERAGE_SCHEMA
                + _BAU_RUN_AUTHORITY_SCHEMA
                + _STRATEGY_JOB_SCHEMA
                + _BACKTEST_RESULT_SCHEMA
                + _STRATEGY_EXPERIMENT_SCHEMA
                + _STRATEGY_MANAGER_AGENT_SCHEMA,
            )
            conn.commit()
            _migrate_bats_mic_constraints(conn)
            _migrate_snapshot_exclusion_constraints(conn)
            _migrate_snapshot_no_provider_data_check(conn)
            _migrate_trade_log_kind_constraint(conn)
            _migrate_roster_source_constraint(conn)
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
                "regime_benchmark_json TEXT",
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
                "experiment_id TEXT",
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
            if "audit_contract_version" not in result_cols:
                conn.execute(
                    "ALTER TABLE backtest_results ADD COLUMN audit_contract_version "
                    "TEXT NOT NULL DEFAULT 'none'"
                )
            staging_cols = {
                str(x[1]) for x in conn.execute("PRAGMA table_info(backtest_staging)")
            }
            for definition in (
                "last_batch_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_batch_sequence >= 0)",
                "last_session TEXT",
                "last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_event_sequence >= 0)",
                "last_equity_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_equity_sequence >= 0)",
            ):
                if definition.split()[0] not in staging_cols:
                    conn.execute(
                        f"ALTER TABLE backtest_staging ADD COLUMN {definition}"
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
            _ensure_trigger(
                conn,
                """CREATE TRIGGER backtest_result_evidence_immutable
                   BEFORE UPDATE ON backtest_results
                   WHEN NEW.run_id != OLD.run_id
                     OR NEW.result_schema_version != OLD.result_schema_version
                     OR NEW.audit_contract_version != OLD.audit_contract_version
                     OR NEW.metrics_json != OLD.metrics_json
                     OR NEW.final_cash_base != OLD.final_cash_base
                     OR NEW.result_digest != OLD.result_digest
                     OR NEW.completed_at != OLD.completed_at
                   BEGIN SELECT RAISE(ABORT, 'backtest result evidence is immutable'); END""",
            )
            preparation_index_sql = (
                "CREATE UNIQUE INDEX idx_strategy_runs_source_preparation "
                "ON strategy_runs(source_preparation_job_id) "
                "WHERE source_preparation_job_id IS NOT NULL AND experiment_id IS NULL"
            )
            preparation_index_row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_strategy_runs_source_preparation'"
            ).fetchone()
            existing_preparation_index_sql = (
                " ".join(str(preparation_index_row[0]).split())
                if preparation_index_row is not None
                else None
            )
            if existing_preparation_index_sql != preparation_index_sql:
                conn.execute(
                    "DROP INDEX IF EXISTS idx_strategy_runs_source_preparation"
                )
                conn.execute(preparation_index_sql)
            _ensure_trigger(
                conn,
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
                                (NEW.experiment_id IS NULL
                                 AND NEW.source_preparation_job_id IS NOT NULL
                                 AND j.parent_job_id IS NULL)
                                OR (NEW.experiment_id IS NULL
                                 AND NEW.source_preparation_job_id IS NULL
                                 AND j.parent_job_id IS NOT NULL)
                                OR (NEW.experiment_id IS NOT NULL
                                 AND NEW.source_preparation_job_id IS NOT NULL
                                 AND j.parent_job_id IS NOT NULL
                                 AND EXISTS(
                                     SELECT 1 FROM strategy_experiments e
                                     WHERE e.id=NEW.experiment_id
                                       AND e.candidate_run_id=NEW.id
                                       AND e.baseline_run_id=j.parent_job_id
                                       AND e.status='approved'
                                 ))
                            )
                        ))
                       OR
                       (NEW.manifest_version='run_input_manifest.v3'
                        AND length(NEW.run_universe_digest)=64
                        AND NEW.selection_json IS NOT NULL
                        AND json_valid(NEW.selection_json)
                        AND EXISTS(
                            SELECT 1 FROM run_input_manifests m
                            WHERE m.digest=NEW.run_input_manifest_digest
                              AND m.manifest_version='run_input_manifest.v3'
                              AND json_valid(m.canonical_manifest_json)
                              AND json_type(
                                  json_extract(m.canonical_manifest_json, '$.regime_benchmark')
                              )='object'
                        )
                        AND EXISTS(
                            SELECT 1 FROM strategy_jobs j
                            WHERE j.id=NEW.id AND (
                                (NEW.experiment_id IS NULL
                                 AND NEW.source_preparation_job_id IS NOT NULL
                                 AND j.parent_job_id IS NULL)
                                OR (NEW.experiment_id IS NULL
                                 AND NEW.source_preparation_job_id IS NULL
                                 AND j.parent_job_id IS NOT NULL)
                                OR (NEW.experiment_id IS NOT NULL
                                 AND NEW.source_preparation_job_id IS NOT NULL
                                 AND j.parent_job_id IS NOT NULL
                                 AND EXISTS(
                                     SELECT 1 FROM strategy_experiments e
                                     WHERE e.id=NEW.experiment_id
                                       AND e.candidate_run_id=NEW.id
                                       AND e.baseline_run_id=j.parent_job_id
                                       AND e.status='approved'
                                 ))
                            )
                        ))
                   )
                   BEGIN
                       SELECT RAISE(
                           ABORT, 'strategy run version provenance mismatch'
                       );
                   END""",
            )
            _ensure_trigger(
                conn,
                """CREATE TRIGGER strategy_run_experiment_candidate_insert
                   BEFORE INSERT ON strategy_runs
                   WHEN NEW.experiment_id IS NOT NULL
                    AND NOT EXISTS(
                        SELECT 1 FROM strategy_experiments e
                        JOIN strategy_jobs j ON j.id=NEW.id
                        WHERE e.id=NEW.experiment_id
                          AND e.candidate_run_id=NEW.id
                          AND e.baseline_run_id=j.parent_job_id
                          AND e.status='approved'
                    )
                   BEGIN
                       SELECT RAISE(ABORT, 'strategy run experiment provenance mismatch');
                   END""",
            )
            self._ensure_snapshot_coverage_revisions(conn)
            existing = conn.execute(
                """SELECT id FROM strategy_jobs
                   WHERE id NOT IN (SELECT job_id FROM notification_outbox)
                   ORDER BY enqueue_seq"""
            ).fetchall()
            for row in existing:
                self._upsert_notification_outbox_on_connection(
                    conn, self._load_strategy_job(conn, str(row[0]))
                )

    @staticmethod
    def _ensure_snapshot_coverage_revisions(conn: sqlite3.Connection) -> None:
        """Account for source writes in their transaction, including external writers."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshot_coverage_revision_state (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                epoch TEXT NOT NULL,
                generation INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO snapshot_coverage_revision_state
               VALUES (1, lower(hex(randomblob(16))), 0)"""
        )
        # No foreign key: deleted/moved profiles must retain their generation.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshot_coverage_profile_revisions (
                profile_hash TEXT PRIMARY KEY,
                generation INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshot_coverage_summaries (
                profile_hash TEXT PRIMARY KEY,
                source_revision TEXT NOT NULL,
                verifier_version INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                summary_digest TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshot_member_revision_summaries (
                profile_hash TEXT NOT NULL,
                snapshot_month TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                verifier_version INTEGER NOT NULL,
                members_json TEXT NOT NULL,
                members_digest TEXT NOT NULL,
                PRIMARY KEY(profile_hash, snapshot_month)
            )"""
        )
        profile_tables = (
            "snapshot_profiles",
            "snapshot_months",
            "snapshot_members",
            "monthly_scan_results",
        )
        shared_tables = (
            "active_snapshot_profile",
            "reconstruction_rosters",
            "reconstruction_roster_members",
            "security_alias_manifests",
            "security_alias_entries",
        )
        for table in (*profile_tables, *shared_tables):
            for event in ("INSERT", "UPDATE", "DELETE"):
                if table in profile_tables:
                    identities = (
                        ("OLD", "NEW")
                        if event == "UPDATE"
                        else ("OLD",)
                        if event == "DELETE"
                        else ("NEW",)
                    )
                    body = "".join(
                        "INSERT INTO snapshot_coverage_profile_revisions "
                        f"VALUES ({identity}.profile_hash, 1) "
                        "ON CONFLICT(profile_hash) DO UPDATE "
                        "SET generation=generation+1;"
                        for identity in identities
                    )
                else:
                    # Shared roster/alias changes are infrequent; conservatively
                    # invalidate every profile instead of indexing dependencies.
                    body = (
                        "UPDATE snapshot_coverage_revision_state "
                        "SET generation=generation+1 WHERE singleton_id=1;"
                    )
                conn.execute(
                    f"CREATE TRIGGER IF NOT EXISTS coverage_revision_{table}_{event.lower()} "
                    f"AFTER {event} ON {table} BEGIN {body} END"
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

    def register_reference_identity(
        self,
        identity: SecurityIdentityV1,
        alias: AliasEntryV1,
        *,
        created_at: datetime,
    ) -> ReferenceIdentityRegistrationV1:
        """Atomically register one non-tradable reference identity and alias.

        Reference storage is intentionally separate from the roster identity
        tables.  That keeps the recovered trading universe and snapshot-member
        MIC CHECK immutable while retaining the same content-addressed,
        append-only registration semantics.
        """
        if identity.mic != "ARCX" or alias.mic != "ARCX":
            raise ValueError("reference identities currently require ARCX")
        if alias.security_id != identity.security_id:
            raise ValueError("reference alias does not match identity")
        if alias.observed_symbol != identity.provider_symbol:
            raise ValueError("reference alias does not match provider symbol")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        registry = SecurityIdentityRegistryV1.build(
            (identity,), created_at=created_at, allow_reference_mics=True
        )
        aliases = SecurityAliasManifestV1.build(
            (alias,), created_at=created_at, allow_reference_mics=True
        )
        registry_json = render_canonical_json(
            {
                "schema_version": registry.schema_version,
                "revision": registry.revision,
                "evidence_digest": registry.evidence_digest,
                "identities": registry.identities,
            }
        )
        aliases_json = render_canonical_json(
            {
                "schema_version": aliases.schema_version,
                "revision": aliases.revision,
                "evidence_digest": aliases.evidence_digest,
                "entries": aliases.entries,
            }
        )
        captured_at = created_at.astimezone(timezone.utc).isoformat()
        alias_values = (
            alias.security_id,
            alias.provider,
            alias.mic,
            alias.observed_symbol,
            None if alias.effective_from is None else alias.effective_from.isoformat(),
            None if alias.effective_to is None else alias.effective_to.isoformat(),
            alias.evidence_source,
            alias.evidence_digest,
            alias.provenance,
        )
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT security_id, evidence_digest
                   FROM reference_security_identities
                   WHERE mic=? AND provider_symbol=?""",
                (identity.mic, identity.provider_symbol),
            ).fetchone()
            by_id = conn.execute(
                """SELECT mic, provider_symbol, evidence_digest
                   FROM reference_security_identities WHERE security_id=?""",
                (identity.security_id,),
            ).fetchone()
            expected_identity = (
                identity.security_id,
                identity.evidence_digest,
            )
            if (
                existing is not None
                and tuple(str(value) for value in existing) != expected_identity
            ):
                raise sqlite3.IntegrityError(
                    "reference identity conflicts with existing security"
                )
            if by_id is not None and tuple(str(value) for value in by_id) != (
                identity.mic,
                identity.provider_symbol,
                identity.evidence_digest,
            ):
                raise sqlite3.IntegrityError(
                    "reference security id conflicts with existing identity"
                )
            self._insert_or_verify(
                conn,
                "reference_identity_registry_revisions",
                "revision_digest",
                registry.revision,
                "canonical_manifest_json",
                registry_json,
                """INSERT INTO reference_identity_registry_revisions
                   (revision_digest, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    registry.revision,
                    registry_json,
                    registry.evidence_digest,
                    captured_at,
                ),
            )
            if existing is None and by_id is None:
                conn.execute(
                    """INSERT INTO reference_security_identities
                       (security_id, mic, provider_symbol, evidence_digest,
                        identity_registry_revision, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        identity.security_id,
                        identity.mic,
                        identity.provider_symbol,
                        identity.evidence_digest,
                        registry.revision,
                        captured_at,
                    ),
                )
            alias_existing = conn.execute(
                "SELECT canonical_manifest_json FROM reference_alias_manifests "
                "WHERE alias_revision=?",
                (aliases.revision,),
            ).fetchone()
            if alias_existing is not None and str(alias_existing[0]) != aliases_json:
                raise sqlite3.IntegrityError(
                    "reference alias manifest digest collision"
                )
            self._insert_or_verify(
                conn,
                "reference_alias_manifests",
                "alias_revision",
                aliases.revision,
                "canonical_manifest_json",
                aliases_json,
                """INSERT INTO reference_alias_manifests
                   (alias_revision, canonical_manifest_json, evidence_digest, created_at)
                   VALUES (?, ?, ?, ?)""",
                (aliases.revision, aliases_json, aliases.evidence_digest, captured_at),
            )
            alias_row = conn.execute(
                """SELECT security_id, provider, mic, observed_symbol,
                          effective_from, effective_to, evidence_source,
                          evidence_digest, provenance
                   FROM reference_alias_entries
                   WHERE alias_revision=?""",
                (aliases.revision,),
            ).fetchone()
            if alias_row is None:
                conn.execute(
                    """INSERT INTO reference_alias_entries
                       (alias_revision, security_id, provider, mic, observed_symbol,
                        effective_from, effective_to, evidence_source, evidence_digest,
                        provenance)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (aliases.revision, *alias_values),
                )
            elif (
                tuple(str(value) if value is not None else None for value in alias_row)
                != alias_values
            ):
                raise sqlite3.IntegrityError(
                    "reference alias conflicts with existing entry"
                )
        return ReferenceIdentityRegistrationV1(
            identity=identity,
            alias=alias,
            identity_registry_revision=registry.revision,
            alias_revision=aliases.revision,
        )

    def reference_identity_rows(self) -> list[tuple[str, str, str, str]]:
        """Return reference identities without mixing them into the roster."""
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT security_id, mic, provider_symbol, evidence_digest
                   FROM reference_security_identities ORDER BY security_id"""
            ).fetchall()
        return [tuple(str(value) for value in row) for row in rows]  # type: ignore[return-value]

    def reference_alias_entry(self, alias_revision: str) -> AliasEntryV1:
        """Resolve one immutable reference alias for later run pinning."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT security_id, provider, mic, observed_symbol,
                          effective_from, effective_to, evidence_source,
                          evidence_digest, provenance
                   FROM reference_alias_entries
                  WHERE alias_revision=?""",
                (alias_revision,),
            ).fetchone()
        if row is None:
            raise BacktestIntegrityError(
                "reference alias revision is unavailable",
                code="reference_alias_missing",
            )
        return AliasEntryV1(
            security_id=str(row[0]),
            provider=str(row[1]),
            mic=str(row[2]),
            observed_symbol=str(row[3]),
            effective_from=None if row[4] is None else date.fromisoformat(str(row[4])),
            effective_to=None if row[5] is None else date.fromisoformat(str(row[5])),
            evidence_source=str(row[6]),
            evidence_digest=str(row[7]),
            provenance=cast(
                Literal["provider_evidence", "manual_override"], str(row[8])
            ),
        )

    def reference_identity_details(self, security_id: str) -> tuple[str, str, str, str]:
        """Resolve one immutable reference identity and its registry revision."""
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT mic, provider_symbol, evidence_digest,
                          identity_registry_revision
                     FROM reference_security_identities
                    WHERE security_id=?""",
                (security_id,),
            ).fetchone()
        if row is None:
            raise BacktestIntegrityError(
                "reference identity is unavailable",
                code="reference_identity_missing",
            )
        return tuple(str(value) for value in row)  # type: ignore[return-value]

    def reference_alias_revision(self, security_id: str) -> str:
        """Resolve the one registered yfinance alias for a reference security."""
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT alias_revision
                     FROM reference_alias_entries
                    WHERE security_id=? AND provider='yfinance'
                      AND mic='ARCX' AND observed_symbol='SPY'""",
                (security_id,),
            ).fetchall()
        if len(rows) != 1:
            raise BacktestIntegrityError(
                "reference alias is unavailable or ambiguous",
                code="reference_alias_missing",
            )
        return str(rows[0][0])

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
        provider: str = "yfinance",
    ) -> tuple[date | None, date | None]:
        """Return the one effective immutable alias interval for ``provider``."""
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT effective_from, effective_to
                   FROM security_alias_entries
                   WHERE alias_revision=? AND security_id=? AND provider=?
                     AND mic=? AND observed_symbol=?
                     AND (effective_from IS NULL OR effective_from<=?)
                     AND (effective_to IS NULL OR ?<effective_to)""",
                (
                    alias_revision,
                    security_id,
                    provider,
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
                    """INSERT INTO preparation_runs(job_id,selection_json,strategy_id,strategy_api_version,strategy_source_digest,parameters_json,start_month,end_month,base_currency,starting_capital,regime_benchmark_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
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
                        None
                        if submission.regime_benchmark is None
                        else submission.regime_benchmark.model_dump_json(),
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

    @staticmethod
    def _experiment_json(value: object) -> str:
        from pydantic import BaseModel

        if not isinstance(value, BaseModel):
            raise TypeError("experiment payload must be a Pydantic model")
        return json.dumps(
            value.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @staticmethod
    def _experiment_from_row(row: tuple[object, ...]):
        from app.schemas.strategy_experiment import StrategyExperimentV1

        payload = {
            "id": row[0],
            "draft_digest": row[2],
            "draft": json.loads(str(row[3])),
            "status": row[4],
            "candidate_run_id": row[5],
            "approval": json.loads(str(row[6])) if row[6] is not None else None,
            "comparison": json.loads(str(row[7])) if row[7] is not None else None,
            "conclusion": json.loads(str(row[8])) if row[8] is not None else None,
            "created_at": row[9],
            "updated_at": row[10],
        }
        return StrategyExperimentV1.model_validate_json(json.dumps(payload))

    @staticmethod
    def _append_experiment_event(
        conn: sqlite3.Connection,
        *,
        experiment_id: str | None,
        baseline_run_id: str | None,
        candidate_run_id: str | None,
        event_type: str,
        details: Mapping[str, object],
        occurred_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO strategy_experiment_audit
               (experiment_id, baseline_run_id, candidate_run_id,
                event_type, details_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                experiment_id,
                baseline_run_id,
                candidate_run_id,
                event_type,
                json.dumps(
                    details,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                occurred_at,
            ),
        )

    def append_strategy_experiment_attempt(
        self,
        *,
        baseline_run_id: str | None,
        event_type: str,
        details: Mapping[str, object],
    ) -> None:
        """Record a rejected or unavailable draft attempt without a draft row."""
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._append_experiment_event(
                conn,
                experiment_id=None,
                baseline_run_id=baseline_run_id,
                candidate_run_id=None,
                event_type=event_type,
                details=details,
                occurred_at=self._job_now(),
            )

    def create_strategy_experiment_draft(self, draft: object, draft_digest: str):
        """Persist a draft only when its baseline is still verified and live."""
        from app.schemas.strategy_experiment import StrategyExperimentDraftV1
        from app.services.backtest.run_input_manifest import read_run_input_manifest

        if not isinstance(draft, StrategyExperimentDraftV1):
            raise TypeError("draft must be a StrategyExperimentDraftV1")
        try:
            baseline_job = self.strategy_job(draft.baseline_run_id)
            baseline_result = self.backtest_result(draft.baseline_run_id)
            stored_manifest = self.run_input_manifest_json(
                baseline_result.run_input_manifest_digest
            )
            if (
                baseline_job.job_type is not StrategyJobType.BACKTEST
                or baseline_job.status is not StrategyJobStatus.COMPLETE
                or baseline_job.deleted_at is not None
                or stored_manifest is None
                or stored_manifest != draft.baseline_manifest_json
            ):
                raise StrategyJobConflict("baseline is not a verified live backtest")
            manifest = read_run_input_manifest(stored_manifest)
            if (
                not manifest.accepts_stored_digest(
                    baseline_result.run_input_manifest_digest
                )
                or manifest.digest() != draft.baseline_manifest_digest
                or baseline_result.strategy_id != draft.strategy_id
                or baseline_result.strategy_api_version != draft.strategy_api_version
                or baseline_result.strategy_source_digest
                != draft.strategy_source_digest
            ):
                raise StrategyJobConflict("baseline manifest identity is inconsistent")
        except (BacktestIntegrityError, StrategyJobNotFound) as exc:
            raise StrategyJobConflict(
                "baseline result is unavailable or corrupt"
            ) from exc

        now = self._job_now()
        experiment_id = self._id_generator()
        draft_json = self._experiment_json(draft)
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            baseline = self._load_strategy_job(conn, draft.baseline_run_id)
            if (
                baseline.status is not StrategyJobStatus.COMPLETE
                or baseline.deleted_at is not None
                or baseline.job_type is not StrategyJobType.BACKTEST
            ):
                raise StrategyJobConflict("baseline is no longer eligible")
            conn.execute(
                """INSERT INTO strategy_experiments
                   (id, baseline_run_id, draft_digest, draft_json, status,
                    candidate_run_id, approval_json, comparison_json,
                    conclusion_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'draft', NULL, NULL, NULL, NULL, ?, ?)""",
                (
                    experiment_id,
                    draft.baseline_run_id,
                    draft_digest,
                    draft_json,
                    now,
                    now,
                ),
            )
            self._append_experiment_event(
                conn,
                experiment_id=experiment_id,
                baseline_run_id=draft.baseline_run_id,
                candidate_run_id=None,
                event_type="draft_created",
                details={
                    "draft_digest": draft_digest,
                    "model_provider": draft.model_provider,
                    "model_id": draft.model_id,
                    "model_attempts": [
                        attempt.model_dump(mode="json")
                        for attempt in draft.model_attempts
                    ],
                },
                occurred_at=now,
            )
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise BacktestIntegrityError("stored strategy experiment disappeared")
        return self._experiment_from_row(row)

    def strategy_experiment(self, experiment_id: str):
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise StrategyJobNotFound(f"strategy experiment not found: {experiment_id}")
        return self._experiment_from_row(row)

    def strategy_experiment_for_candidate(self, candidate_run_id: str):
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE candidate_run_id=?""",
                (candidate_run_id,),
            ).fetchone()
        return None if row is None else self._experiment_from_row(row)

    def pending_strategy_experiment_reconciliations(
        self, *, limit: int = 20
    ) -> tuple[str, ...]:
        """Return terminal candidate jobs whose durable experiment is unsettled."""
        if not 1 <= limit <= 100:
            raise ValueError(
                "experiment reconciliation limit must be between 1 and 100"
            )
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT e.candidate_run_id
                   FROM strategy_experiments e
                   JOIN strategy_jobs j ON j.id=e.candidate_run_id
                   WHERE e.status='approved'
                     AND j.status IN ('complete', 'failed', 'cancelled')
                   ORDER BY e.updated_at, j.updated_at, e.id
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_strategy_experiments(self):
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments ORDER BY created_at DESC, id DESC"""
            ).fetchall()
        return tuple(self._experiment_from_row(row) for row in rows)

    def pending_strategy_experiments(self, *, limit: int = 5):
        """Return a bounded newest-first view of actionable GH-15 drafts."""
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("pending experiment limit must be between 1 and 25")
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments
                   WHERE status='draft'
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(self._experiment_from_row(row) for row in rows)

    def strategy_experiment_audit(self, experiment_id: str):
        from app.schemas.strategy_experiment import StrategyExperimentAuditEventV1

        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT sequence, experiment_id, baseline_run_id,
                          candidate_run_id, event_type, occurred_at, details_json
                   FROM strategy_experiment_audit
                   WHERE experiment_id=? ORDER BY sequence""",
                (experiment_id,),
            ).fetchall()
        return tuple(
            StrategyExperimentAuditEventV1.model_validate_json(
                json.dumps(
                    {
                        "sequence": row[0],
                        "experiment_id": row[1],
                        "baseline_run_id": row[2],
                        "candidate_run_id": row[3],
                        "event_type": row[4],
                        "occurred_at": row[5],
                        "details": json.loads(str(row[6])),
                    }
                )
            )
            for row in rows
        )

    def strategy_experiment_attempt_audit(self, *, limit: int = 20):
        """Return recent rejected/unavailable draft attempts with no experiment row."""
        from app.schemas.strategy_experiment import StrategyExperimentAuditEventV1

        if not 1 <= limit <= 100:
            raise ValueError("experiment audit limit must be between 1 and 100")
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT sequence, experiment_id, baseline_run_id,
                          candidate_run_id, event_type, occurred_at, details_json
                   FROM strategy_experiment_audit
                   WHERE experiment_id IS NULL
                   ORDER BY sequence DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(
            StrategyExperimentAuditEventV1.model_validate_json(
                json.dumps(
                    {
                        "sequence": row[0],
                        "experiment_id": row[1],
                        "baseline_run_id": row[2],
                        "candidate_run_id": row[3],
                        "event_type": row[4],
                        "occurred_at": row[5],
                        "details": json.loads(str(row[6])),
                    }
                )
            )
            for row in rows
        )

    def strategy_manager_agent_cache(
        self, task: Literal["insights", "question"], request_digest: str
    ) -> str | None:
        """Return the newest locally accepted output for an exact request digest."""
        if task not in {"insights", "question"} or not re.fullmatch(
            r"[0-9a-f]{64}", request_digest
        ):
            raise ValueError("invalid Strategy Manager agent cache key")
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT output_json FROM strategy_manager_agent_audit
                   WHERE task=? AND request_digest=? AND outcome='accepted'
                   ORDER BY sequence DESC LIMIT 1""",
                (task, request_digest),
            ).fetchone()
        return None if row is None else str(row[0])

    def record_strategy_manager_agent_call(
        self,
        *,
        task: Literal["insights", "question"],
        request_digest: str,
        summary_digest: str,
        prompt_version: str,
        schema_version: str,
        attempts: tuple[Mapping[str, object], ...],
        outcome: Literal["accepted", "unavailable"],
        accepted_citations: tuple[str, ...],
        output: Mapping[str, object] | None,
        user_question: str | None = None,
    ) -> int:
        """Append provider attempts and any validated response without raw evidence."""
        if task not in {"insights", "question"}:
            raise ValueError("invalid Strategy Manager agent task")
        for digest in (request_digest, summary_digest):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid Strategy Manager agent digest")
        if (
            not prompt_version
            or len(prompt_version) > 80
            or not schema_version
            or len(schema_version) > 80
        ):
            raise ValueError("invalid Strategy Manager agent prompt/schema version")
        if (task == "question") != (user_question is not None):
            raise ValueError("question audit text does not match task")
        if user_question is not None and (
            not user_question or len(user_question) > 500
        ):
            raise ValueError("question audit text is outside its bound")
        if (outcome == "accepted") != (output is not None):
            raise ValueError("accepted outcome must match validated output")
        encoded_attempts = json.dumps(
            [dict(item) for item in attempts],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        encoded_citations = json.dumps(
            list(accepted_citations), separators=(",", ":"), ensure_ascii=False
        )
        encoded_output = (
            None
            if output is None
            else json.dumps(
                dict(output),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        with session(self._connect) as conn:
            cursor = conn.execute(
                """INSERT INTO strategy_manager_agent_audit
                   (task, request_digest, summary_digest, prompt_version, schema_version,
                    user_question, attempts_json, outcome, accepted_citations_json,
                    output_json, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task,
                    request_digest,
                    summary_digest,
                    prompt_version,
                    schema_version,
                    user_question,
                    encoded_attempts,
                    outcome,
                    encoded_citations,
                    encoded_output,
                    self._job_now(),
                ),
            )
            if cursor.lastrowid is None:
                raise BacktestIntegrityError(
                    "Strategy Manager agent audit sequence was not assigned"
                )
            return cursor.lastrowid

    def strategy_experiment_baselines(self, *, limit: int = 25):
        """Return recent verified completed Backtests, skipping damaged rows."""
        from app.schemas.strategy_experiment import StrategyExperimentBaselineOptionV1

        if not 1 <= limit <= 100:
            raise ValueError("experiment baseline limit must be between 1 and 100")
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT j.id, r.strategy_id, r.start_month, r.end_month
                   FROM strategy_jobs j
                   JOIN strategy_runs r ON r.id=j.id
                   WHERE j.job_type='backtest' AND j.status='complete'
                     AND j.deleted_at IS NULL
                   ORDER BY j.enqueue_seq DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        options = []
        for row in rows:
            run_id = str(row[0])
            try:
                result = self.backtest_result(run_id)
            except (BacktestIntegrityError, StrategyJobNotFound, ValueError):
                continue
            if result.strategy_id != str(row[1]):
                continue
            options.append(
                StrategyExperimentBaselineOptionV1(
                    id=run_id,
                    strategy_id=str(row[1]),
                    start_month=str(row[2]),
                    end_month=str(row[3]),
                )
            )
            if len(options) >= limit:
                break
        return tuple(options)

    def discard_strategy_experiment(self, experiment_id: str, draft_digest: str):
        from app.schemas.strategy_experiment import ExperimentStatus

        now = self._job_now()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise StrategyJobNotFound(
                    f"strategy experiment not found: {experiment_id}"
                )
            experiment = self._experiment_from_row(row)
            if experiment.draft_digest != draft_digest:
                raise StrategyJobConflict("strategy experiment draft digest is stale")
            if experiment.status is ExperimentStatus.DISCARDED:
                return experiment
            if experiment.status is not ExperimentStatus.DRAFT:
                raise StrategyJobConflict("only an unapproved draft can be discarded")
            conn.execute(
                "UPDATE strategy_experiments SET status='discarded', updated_at=? WHERE id=?",
                (now, experiment_id),
            )
            self._append_experiment_event(
                conn,
                experiment_id=experiment_id,
                baseline_run_id=experiment.draft.baseline_run_id,
                candidate_run_id=None,
                event_type="draft_discarded",
                details={"draft_digest": draft_digest},
                occurred_at=now,
            )
            updated = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        if updated is None:
            raise BacktestIntegrityError("stored strategy experiment disappeared")
        return self._experiment_from_row(updated)

    def approve_strategy_experiment_candidate(
        self,
        experiment_id: str,
        draft_digest: str,
        candidate_manifest_json: str,
        approval: object,
    ):
        """Atomically bind approval and enqueue one exact-manifest candidate."""
        from app.schemas.strategy_experiment import (
            ExperimentStatus,
            StrategyExperimentApprovalV1,
        )
        from app.services.backtest.run_input_manifest import read_run_input_manifest

        if not isinstance(approval, StrategyExperimentApprovalV1):
            raise TypeError("approval must be a StrategyExperimentApprovalV1")
        if approval.draft_digest != draft_digest:
            raise StrategyJobConflict("approval is not bound to the stored draft")
        experiment = self.strategy_experiment(experiment_id)
        if experiment.draft_digest != draft_digest:
            raise StrategyJobConflict("strategy experiment draft digest is stale")
        if experiment.candidate_run_id is not None:
            return experiment, self.strategy_job(experiment.candidate_run_id)

        try:
            baseline_job = self.strategy_job(experiment.draft.baseline_run_id)
            baseline_result = self.backtest_result(experiment.draft.baseline_run_id)
            baseline_raw = self.run_input_manifest_json(
                baseline_result.run_input_manifest_digest
            )
            if (
                baseline_job.job_type is not StrategyJobType.BACKTEST
                or baseline_job.status is not StrategyJobStatus.COMPLETE
                or baseline_job.deleted_at is not None
                or baseline_raw is None
                or baseline_raw != experiment.draft.baseline_manifest_json
            ):
                raise StrategyJobConflict("baseline is no longer eligible")
            baseline_manifest = read_run_input_manifest(baseline_raw)
            candidate_manifest = read_run_input_manifest(candidate_manifest_json)
        except (BacktestIntegrityError, StrategyJobNotFound) as exc:
            raise StrategyJobConflict("verified baseline is unavailable") from exc

        if (
            not baseline_manifest.accepts_stored_digest(
                baseline_result.run_input_manifest_digest
            )
            or baseline_manifest.digest() != experiment.draft.baseline_manifest_digest
            or candidate_manifest.canonical_json() != candidate_manifest_json
            or candidate_manifest.schema_version != baseline_manifest.schema_version
            or candidate_manifest.execution_contract_digest()
            != baseline_manifest.execution_contract_digest()
        ):
            raise StrategyJobConflict("candidate manifest is invalid or incompatible")
        baseline_payload = baseline_manifest.canonical_payload()
        candidate_payload = candidate_manifest.canonical_payload()
        baseline_parameters = dict(baseline_manifest.parameters)
        candidate_parameters = dict(candidate_manifest.parameters)
        before = baseline_parameters.get(experiment.draft.parameter_name, object())
        after = candidate_parameters.get(experiment.draft.parameter_name, object())
        changed = {
            key
            for key in baseline_parameters.keys() | candidate_parameters.keys()
            if json.dumps(
                baseline_parameters.get(key), sort_keys=True, separators=(",", ":")
            )
            != json.dumps(
                candidate_parameters.get(key), sort_keys=True, separators=(",", ":")
            )
        }
        baseline_payload.pop("parameters", None)
        candidate_payload.pop("parameters", None)
        if (
            baseline_payload != candidate_payload
            or changed != {experiment.draft.parameter_name}
            or before != experiment.draft.baseline_value
            or after != experiment.draft.proposed_value
        ):
            raise StrategyJobConflict(
                "candidate changes more than the approved parameter"
            )

        now = self._job_now()
        candidate_digest = candidate_manifest.digest()
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise StrategyJobNotFound(
                    f"strategy experiment not found: {experiment_id}"
                )
            current = self._experiment_from_row(row)
            if current.draft_digest != draft_digest:
                raise StrategyJobConflict("strategy experiment draft digest is stale")
            if current.candidate_run_id is not None:
                return current, self._load_strategy_job(conn, current.candidate_run_id)
            if current.status is not ExperimentStatus.DRAFT:
                raise StrategyJobConflict("only an unapproved draft can be approved")
            baseline = self._load_strategy_job(conn, current.draft.baseline_run_id)
            if (
                baseline.job_type is not StrategyJobType.BACKTEST
                or baseline.status is not StrategyJobStatus.COMPLETE
                or baseline.deleted_at is not None
            ):
                raise StrategyJobConflict("baseline is no longer eligible")
            baseline_run = conn.execute(
                """SELECT id, strategy_id, strategy_api_version,
                          strategy_source_digest, parameters_json, profile_hash,
                          start_month, end_month, ordered_month_digest,
                          base_currency, starting_capital,
                          run_input_manifest_digest, execution_contract_digest,
                          manifest_version, run_universe_digest,
                          source_preparation_job_id, selection_json, created_at
                   FROM strategy_runs WHERE id=?""",
                (current.draft.baseline_run_id,),
            ).fetchone()
            if baseline_run is None:
                raise StrategyJobConflict("baseline run identity is unavailable")
            if str(baseline_run[11]) != baseline_result.run_input_manifest_digest:
                raise StrategyJobConflict("baseline run identity is inconsistent")
            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM strategy_jobs"
            ).fetchone()
            enqueue_seq = int(sequence_row[0]) if sequence_row else 1
            candidate_id = self._id_generator()
            candidate_parent_id = current.draft.baseline_run_id
            conn.execute(
                """INSERT INTO strategy_jobs (
                       id, job_type, status, parent_job_id, enqueue_seq,
                       claim_token, current_month, status_version,
                       cancel_requested_at, failure_code, failed_month,
                       failure_detail, deleted_at, audit_summary,
                       created_at, updated_at
                   ) VALUES (?, 'backtest', 'queued', ?, ?, NULL, NULL, 1,
                             NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                (candidate_id, candidate_parent_id, enqueue_seq, now, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO run_input_manifests
                   (digest, execution_contract_digest, canonical_manifest_json,
                    created_at, manifest_version)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    candidate_digest,
                    candidate_manifest.execution_contract_digest(),
                    candidate_manifest_json,
                    now,
                    candidate_manifest.schema_version,
                ),
            )
            conn.execute(
                """UPDATE strategy_experiments
                   SET status='approved', candidate_run_id=?, approval_json=?, updated_at=?
                   WHERE id=?""",
                (
                    candidate_id,
                    self._experiment_json(approval),
                    now,
                    experiment_id,
                ),
            )
            run_values = list(baseline_run)
            run_values[0] = candidate_id
            run_values[4] = json.dumps(
                candidate_parameters, sort_keys=True, separators=(",", ":")
            )
            run_values[11] = candidate_digest
            run_values[17] = now
            run_values.append(experiment_id)
            conn.execute(
                """INSERT INTO strategy_runs (
                       id, strategy_id, strategy_api_version,
                       strategy_source_digest, parameters_json, profile_hash,
                       start_month, end_month, ordered_month_digest,
                       base_currency, starting_capital,
                       run_input_manifest_digest, execution_contract_digest,
                       manifest_version, run_universe_digest,
                       source_preparation_job_id, selection_json, created_at,
                       experiment_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                run_values,
            )
            self._append_experiment_event(
                conn,
                experiment_id=experiment_id,
                baseline_run_id=current.draft.baseline_run_id,
                candidate_run_id=candidate_id,
                event_type="candidate_approved_and_enqueued",
                details={
                    "draft_digest": draft_digest,
                    "candidate_manifest_digest": candidate_digest,
                    "approval": json.loads(self._experiment_json(approval)),
                },
                occurred_at=now,
            )
            job = self._load_strategy_job(conn, candidate_id)
            self._upsert_notification_outbox_on_connection(conn, job)
            updated_row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        if updated_row is None:
            raise BacktestIntegrityError("stored strategy experiment disappeared")
        return self._experiment_from_row(updated_row), job

    def finalize_strategy_experiment(
        self,
        experiment_id: str,
        candidate_run_id: str,
        comparison: object,
        conclusion: object,
    ):
        from app.schemas.strategy_experiment import (
            ExperimentStatus,
            ExperimentVerdict,
            StrategyExperimentComparisonV1,
            StrategyExperimentConclusionV1,
        )

        if not isinstance(comparison, StrategyExperimentComparisonV1) or not isinstance(
            conclusion, StrategyExperimentConclusionV1
        ):
            raise TypeError("comparison and conclusion must use experiment models")
        now = self._job_now()
        status = (
            ExperimentStatus.INCONCLUSIVE
            if conclusion.verdict is ExperimentVerdict.INCONCLUSIVE
            else ExperimentStatus.COMPLETE
        )
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise StrategyJobNotFound(
                    f"strategy experiment not found: {experiment_id}"
                )
            current = self._experiment_from_row(row)
            if current.candidate_run_id != candidate_run_id:
                raise StrategyJobConflict("candidate run does not match experiment")
            if current.status in {
                ExperimentStatus.COMPLETE,
                ExperimentStatus.INCONCLUSIVE,
            }:
                return current
            if current.status is not ExperimentStatus.APPROVED:
                raise StrategyJobConflict("only an approved experiment can conclude")
            conn.execute(
                """UPDATE strategy_experiments
                   SET status=?, comparison_json=?, conclusion_json=?, updated_at=?
                   WHERE id=?""",
                (
                    status.value,
                    self._experiment_json(comparison),
                    self._experiment_json(conclusion),
                    now,
                    experiment_id,
                ),
            )
            self._append_experiment_event(
                conn,
                experiment_id=experiment_id,
                baseline_run_id=current.draft.baseline_run_id,
                candidate_run_id=candidate_run_id,
                event_type="experiment_concluded",
                details={
                    "verdict": conclusion.verdict.value,
                    "comparison": json.loads(self._experiment_json(comparison)),
                    "limitations": list(comparison.limitations),
                },
                occurred_at=now,
            )
            updated_row = conn.execute(
                """SELECT id, baseline_run_id, draft_digest, draft_json, status,
                          candidate_run_id, approval_json, comparison_json,
                          conclusion_json, created_at, updated_at
                   FROM strategy_experiments WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        if updated_row is None:
            raise BacktestIntegrityError("stored strategy experiment disappeared")
        return self._experiment_from_row(updated_row)

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
                submitted_payload = json.loads(submission.canonical_manifest_json)
                # gh-641: ``python_runtime`` is recorded in the stored
                # rendering but never hashed -- mirror
                # ``RunInputManifestV1.digest``.
                submitted_payload.pop("python_runtime", None)
                if (
                    manifest_digest(submitted_payload)
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
        historical_price_repository: Any | None = None,
    ) -> BacktestEnqueueResultV1:
        from app.services.backtest.run_input_manifest import (
            RunInputManifestV2,
            RunInputManifestV3,
            read_run_input_manifest,
        )

        if (
            submission.manifest_version
            not in {"run_input_manifest.v2", "run_input_manifest.v3"}
            or submission.source_preparation_job_id != prep_id
        ):
            raise StrategyJobConflict("invalid preparation seal")
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
            if isinstance(manifest, RunInputManifestV3):
                pin = manifest.regime_benchmark
                if historical_price_repository is None:
                    raise StrategyJobConflict(
                        "V3 preparation requires a historical price repository"
                    )
                identity_row = conn.execute(
                    """SELECT mic, provider_symbol, identity_registry_revision
                         FROM reference_security_identities
                        WHERE security_id=?""",
                    (pin.security_id,),
                ).fetchone()
                alias_row = conn.execute(
                    """SELECT security_id, provider, mic, observed_symbol
                         FROM reference_alias_entries
                        WHERE alias_revision=?""",
                    (pin.alias_revision,),
                ).fetchone()
                if (
                    identity_row is None
                    or tuple(str(value) for value in identity_row)
                    != ("ARCX", "SPY", pin.identity_registry_revision)
                    or alias_row is None
                    or tuple(str(value) for value in alias_row)
                    != (pin.security_id, "yfinance", "ARCX", "SPY")
                ):
                    raise StrategyJobConflict(
                        "regime benchmark identity is unavailable"
                    )
                try:
                    price_evidence = historical_price_repository.verify(
                        pin.price_revision
                    )
                    action_evidence = (
                        price_evidence
                        if pin.action_revision == pin.price_revision
                        else historical_price_repository.verify(pin.action_revision)
                    )
                except Exception as exc:
                    raise StrategyJobConflict(
                        "regime benchmark evidence is unavailable"
                    ) from exc
                for evidence in (price_evidence, action_evidence):
                    if (
                        evidence.data_revision != pin.evidence_digest
                        or evidence.security_id != pin.security_id
                        or evidence.alias_revision != pin.alias_revision
                        or evidence.provider != "yfinance"
                        or evidence.currency != "USD"
                        or evidence.quote_unit != "USD"
                        or evidence.exchange_timezone != "America/New_York"
                        or evidence.start != pin.request_start.isoformat()
                        or evidence.end != pin.request_end.isoformat()
                    ):
                        raise StrategyJobConflict(
                            "regime benchmark evidence identity mismatch"
                        )
            s = prep.selection
            ok = (
                isinstance(manifest, (RunInputManifestV2, RunInputManifestV3))
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
                and submission.regime_benchmark == prep.regime_benchmark
                and getattr(manifest, "regime_benchmark", None) == prep.regime_benchmark
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
                    manifest.schema_version,
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
                    manifest.schema_version,
                    s.run_universe_digest,
                    prep_id,
                    s.model_dump_json(),
                    now,
                ),
            )
            if isinstance(manifest, RunInputManifestV3):
                assert historical_price_repository is not None
                for revision in {
                    manifest.regime_benchmark.price_revision,
                    manifest.regime_benchmark.action_revision,
                }:
                    historical_price_repository.pin("backtest", cid, revision)
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

    def initialization_progress(self, job_id: str) -> InitializationProgressV1 | None:
        with session(self._connect) as conn:
            row = conn.execute(
                """SELECT job_id, committed_months, reused_months, fetched_months,
                          partial_months, reused_securities, fetched_securities,
                          fresh_elapsed_seconds, fresh_months, last_committed_month,
                          last_committed_at FROM initialization_progress WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            return None if row is None else _row_to_initialization_progress(row)

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
        ``complete`` job whose Result is missing, or a non-complete job
        that unexpectedly has one, raises :class:`BacktestIntegrityError`
        rather than silently returning a partial or zero-filled row. A
        present Result that fails verification is listed with no Metrics
        and its ``result_error``, so one damaged row is flagged rather than
        hiding every other Backtest.
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
            result_error = None
            if is_complete:
                try:
                    result = self.backtest_result(job.id)
                except BacktestIntegrityError as exc:
                    result_error = str(exc)
                else:
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
                    result_error=result_error,
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

    def recent_verified_backtest_results(
        self, *, limit: int = 25
    ) -> RecentBacktestResultsV1:
        """Read at most ``limit`` recent complete, live Result candidates.

        Candidate rows are ordered by their persisted completion time and
        stable run ID. Each candidate is returned only after the ordinary
        Result digest and metric reconstruction checks pass. Failed,
        cancelled, queued, running, and tombstoned jobs are counted through
        a metadata-only aggregate and never cause Result/event/curve reads.
        The candidate cap applies before Result verification, so a damaged
        row cannot make the landing scan arbitrarily far back through history.
        """
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("recent Backtest Result limit must be between 1 and 25")
        with session(self._connect) as conn:
            rows = conn.execute(
                """SELECT job.id, result.run_id
                   FROM strategy_jobs AS job
                   LEFT JOIN backtest_results AS result ON result.run_id=job.id
                   WHERE job.job_type='backtest' AND job.status='complete'
                     AND job.deleted_at IS NULL
                   ORDER BY result.completed_at DESC, job.id ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            exclusion_rows = conn.execute(
                """SELECT CASE WHEN deleted_at IS NOT NULL THEN 'deleted'
                               ELSE status END, COUNT(*)
                   FROM strategy_jobs
                   WHERE job_type='backtest'
                     AND (status != 'complete' OR deleted_at IS NOT NULL)
                   GROUP BY CASE WHEN deleted_at IS NOT NULL THEN 'deleted'
                                 ELSE status END"""
            ).fetchall()

        results: list[BacktestResultV1] = []
        corrupt_count = 0
        missing_count = 0
        for row in rows:
            run_id = str(row[0])
            if row[1] is None:
                missing_count += 1
                continue
            try:
                results.append(self.backtest_result(run_id))
            except (BacktestIntegrityError, StrategyJobNotFound, ValueError):
                corrupt_count += 1
        return RecentBacktestResultsV1(
            results=tuple(results),
            inspected_count=len(rows),
            integrity_excluded_count=corrupt_count,
            missing_result_count=missing_count,
            job_exclusion_counts=tuple(
                sorted((str(row[0]), int(row[1])) for row in exclusion_rows)
            ),
        )

    def is_comparable(
        self,
        left: str,
        right: str,
        *,
        left_result: BacktestResultV1 | None = None,
        right_result: BacktestResultV1 | None = None,
    ) -> ComparisonEligibilityV1:
        """Return AD-19's one canonical comparison-eligibility verdict for
        two persisted Backtest Result IDs (Story 3.1 AC 1, 2, 5).

        Self-comparison is rejected before any lookup. Each side is then
        checked via the same ``strategy_jobs`` row :meth:`strategy_job`
        already reads: a missing row, a tombstoned job
        (``deleted_at IS NOT NULL``), or a non-complete Backtest job
        (queued/running/failed/cancelled, or a non-Backtest job type) is
        reported as an ordinary ``eligible=False`` outcome, never an
        error. Once both jobs are confirmed complete, their Results are
        loaded via :meth:`backtest_result` -- or supplied as a Result that
        the caller freshly verified in this request -- never re-parsed here -- and
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

        ``left_result`` and ``right_result`` are intentionally narrow
        request-local reuse inputs for callers that already performed the
        full retrieval verification.  They are never retained by this
        repository; ordinary calls continue to verify both persisted Results.
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

        left_result = self._comparison_result_for_request(left, left_result)
        right_result = self._comparison_result_for_request(right, right_result)
        return self._compare_verified_results(left_result, right_result)

    def comparison_results_if_eligible(
        self, left: str, right: str
    ) -> tuple[
        ComparisonEligibilityV1, BacktestResultV1 | None, BacktestResultV1 | None
    ]:
        """Revalidate eligibility and return its verified Result inputs once.

        This is intentionally request-scoped: callers present the returned
        objects immediately and never retain them as a repository cache.
        """
        if left == right:
            return (
                ComparisonEligibilityV1(
                    eligible=False,
                    reason=ComparisonIneligibleReason.SELF_COMPARISON,
                    detail=f"{left!r} cannot be compared to itself",
                ),
                None,
                None,
            )
        for run_id in (left, right):
            reason = self._comparison_job_reason(run_id)
            if reason is not None:
                return (
                    ComparisonEligibilityV1(
                        eligible=False,
                        reason=reason,
                        detail=f"{run_id!r} is not eligible for comparison "
                        f"({reason.value})",
                    ),
                    None,
                    None,
                )
        left_result = self.backtest_result(left)
        right_result = self.backtest_result(right)
        return (
            self.is_comparable(
                left, right, left_result=left_result, right_result=right_result
            ),
            left_result,
            right_result,
        )

    def _comparison_result_for_request(
        self, run_id: str, result: BacktestResultV1 | None
    ) -> BacktestResultV1:
        """Return a Result freshly verified by this request, if supplied.

        The optional object is deliberately request-scoped caller state, not
        a repository cache.  Its ID must still name the requested row; the
        caller is responsible for obtaining it through :meth:`backtest_result`
        in the same request.  Calls without it retain the public full-read
        verification boundary.
        """
        if result is None:
            return self.backtest_result(run_id)
        if result.run_id != run_id:
            raise ValueError("reused backtest result does not match run_id")
        return result

    def _compare_verified_results(
        self, left_result: BacktestResultV1, right_result: BacktestResultV1
    ) -> ComparisonEligibilityV1:
        """Apply the complete comparison predicate to verified Results."""
        if left_result.manifest_version != right_result.manifest_version:
            return ComparisonEligibilityV1(
                False,
                ComparisonIneligibleReason.MANIFEST_VERSION_MISMATCH,
                "Backtests use different manifest versions",
            )
        left_selection = left_result.universe_selection
        right_selection = right_result.universe_selection
        if (
            left_result.manifest_version
            in {"run_input_manifest.v2", "run_input_manifest.v3"}
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
        if left_result.manifest_version == "run_input_manifest.v3":
            if (
                left_result.regime_benchmark is None
                or right_result.regime_benchmark is None
                or left_result.regime_benchmark.digest()
                != right_result.regime_benchmark.digest()
            ):
                return ComparisonEligibilityV1(
                    False,
                    ComparisonIneligibleReason.EVIDENCE_DIGEST_MISMATCH,
                    "Backtests use different regime benchmark evidence",
                )
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

    def comparison_candidates(
        self, run_id: str, *, anchor_result: BacktestResultV1 | None = None
    ) -> tuple[ComparisonCandidateV1, ...]:
        """Return every other eligible Backtest Result for ``run_id``
        (Story 3.1 AC 3), newest first.

        Loads the anchor via :meth:`backtest_result` first (unless a caller
        supplies its freshly verified request-local ``anchor_result``), propagating
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
        anchor_result = self._comparison_result_for_request(run_id, anchor_result)
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

    def record_initialization_month_commit(
        self,
        job_id: str,
        claim_token: str,
        *,
        month: str,
        reused_securities: int,
        fetched_securities: int,
        fresh_elapsed_seconds: float,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> InitializationProgressV1:
        if min(reused_securities, fetched_securities, fresh_elapsed_seconds) < 0:
            raise ValueError("initialization progress counts must be non-negative")
        outcome = (
            "partial_months"
            if reused_securities and fetched_securities
            else "fetched_months"
            if fetched_securities
            else "reused_months"
        )
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            initialization = self._load_initialization(conn, job_id)
            if month not in initialization.requested_months:
                raise StrategyJobConflict("progress month is outside requested range")
            fence = _lease_fence_params(lease)
            owned = conn.execute(
                f"SELECT 1 FROM strategy_jobs WHERE id=? AND status='running' AND claim_token=? {_LEASE_FENCE_SQL}",
                (job_id, claim_token, *fence),
            ).fetchone()
            if owned is None:
                raise StrategyJobConflict("worker progress ownership is stale")
            now = self._job_now()
            conn.execute(
                f"""INSERT INTO initialization_progress (
                       job_id, committed_months, reused_months, fetched_months,
                       partial_months, reused_securities, fetched_securities,
                       fresh_elapsed_seconds, fresh_months, last_committed_month,
                       last_committed_at
                   ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       committed_months=committed_months+1,
                       {outcome}={outcome}+1,
                       reused_securities=reused_securities+excluded.reused_securities,
                       fetched_securities=fetched_securities+excluded.fetched_securities,
                       fresh_elapsed_seconds=fresh_elapsed_seconds+excluded.fresh_elapsed_seconds,
                       fresh_months=fresh_months+excluded.fresh_months,
                       last_committed_month=excluded.last_committed_month,
                       last_committed_at=excluded.last_committed_at""",
                (
                    job_id,
                    int(outcome == "reused_months"),
                    int(outcome == "fetched_months"),
                    int(outcome == "partial_months"),
                    reused_securities,
                    fetched_securities,
                    fresh_elapsed_seconds,
                    int(fetched_securities > 0),
                    month,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT job_id, committed_months, reused_months, fetched_months, partial_months, reused_securities, fetched_securities, fresh_elapsed_seconds, fresh_months, last_committed_month, last_committed_at FROM initialization_progress WHERE job_id=?",
                (job_id,),
            ).fetchone()
            assert row is not None
            return _row_to_initialization_progress(row)

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
            job_type = self._require_job_type(conn, job_id)
            if failed_month is not None:
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
            if job_type is StrategyJobType.BACKTEST:
                conn.execute("DELETE FROM backtest_staging WHERE run_id=?", (job_id,))
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

    def append_backtest_staging_batch(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_version: int,
        batch_sequence: int,
        session: date,
        state_schema_version: str,
        portfolio_state: Mapping[str, object],
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
        candidate_audits: tuple[CandidateAuditV1, ...] | None = None,
        final_cash_base: Decimal | None = None,
        initial_entry_selection: InitialEntrySelectionV1 | None = None,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> None:
        """Append one session delta and its matching portfolio checkpoint.

        Serialization happens before the SQLite write lock. The append,
        audit rows, checkpoint update, and first entry-selection write share one
        ``BEGIN IMMEDIATE`` transaction and the existing worker fence.
        """
        if type(batch_sequence) is not int or batch_sequence <= 0:
            raise BacktestIntegrityError("staging batch sequence is invalid")
        if not isinstance(session, date):
            raise BacktestIntegrityError("staging batch session is invalid")
        if not state_schema_version:
            raise BacktestIntegrityError("staging state schema version is invalid")
        try:
            if final_cash_base is None:
                final_cash_base = equity_point.cash_base
            if not final_cash_base.is_finite():
                raise ValueError("final cash is not finite")
            if equity_point.session != session:
                raise ValueError("equity point session does not match batch session")
            event_sequences = [event.sequence for event in events]
            if event_sequences != sorted(event_sequences) or len(
                set(event_sequences)
            ) != len(event_sequences):
                raise ValueError("staging batch events are not strictly ordered")
            if final_cash_base != equity_point.cash_base:
                raise ValueError("staging final cash does not match equity point")
            state_json = json.dumps(
                dict(portfolio_state),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payload, compressed, payload_digest = self._encode_backtest_staging_batch(
                session, events, equity_point
            )
            audit_payloads = None
            audit_digest = None
            if candidate_audits is not None:
                audit_payloads, audit_digest = self._encode_candidate_audit_batch(
                    session, candidate_audits
                )
                self._validate_candidate_audit_event_links(
                    session, candidate_audits, events
                )
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise BacktestIntegrityError("backtest staging batch is invalid") from exc

        now = self._job_now()
        with _db_session(self._connect) as conn:
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
                raise StrategyJobConflict("staging batch append ownership is stale")

            run_range = conn.execute(
                "SELECT start_month, end_month FROM strategy_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            session_month = session.strftime("%Y-%m")
            if run_range is None or not (
                str(run_range[0]) <= session_month <= str(run_range[1])
            ):
                raise BacktestIntegrityError(
                    "staging batch session is outside the pinned run range"
                )

            checkpoint = conn.execute(
                """SELECT state_json, events_json, equity_curve_json,
                          final_cash_base, last_batch_sequence, last_session,
                          last_event_sequence, last_equity_sequence
                   FROM backtest_staging WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if checkpoint is None:
                last_batch_sequence = 0
                last_session: date | None = None
                last_event_sequence = 0
                last_equity_sequence = 0
            else:
                if (
                    str(checkpoint[1]) != "[]"
                    or str(checkpoint[2]) != "[]"
                    or str(checkpoint[0]) == ""
                ):
                    raise BacktestIntegrityError(
                        "legacy cumulative staging must be discarded before append"
                    )
                try:
                    last_batch_sequence = int(checkpoint[4])
                    last_session = (
                        None
                        if checkpoint[5] is None
                        else date.fromisoformat(str(checkpoint[5]))
                    )
                    last_event_sequence = int(checkpoint[6])
                    last_equity_sequence = int(checkpoint[7])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise BacktestIntegrityError(
                        "backtest staging checkpoint is invalid"
                    ) from exc

            audit_contract = conn.execute(
                """SELECT audit_contract_version
                   FROM backtest_staging_audit_contracts WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if audit_contract is None:
                staged_audit_batch_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM backtest_staging_audit_batches WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
                staged_audit_row_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM backtest_staging_candidate_audits WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
                if last_batch_sequence == 0:
                    existing_batch_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM backtest_staging_batches WHERE run_id=?",
                            (run_id,),
                        ).fetchone()[0]
                    )
                    if existing_batch_count:
                        raise BacktestIntegrityError(
                            "staging batches exist without a checkpoint"
                        )
                    audit_contract_version = (
                        _BACKTEST_CANDIDATE_AUDIT_CONTRACT
                        if candidate_audits is not None
                        else "none"
                    )
                elif staged_audit_batch_count == 0 and staged_audit_row_count == 0:
                    # Staging batches created before the audit contract are
                    # immutable economic input. A resumed run remains legacy,
                    # because reconstructing its missing candidate evidence
                    # would invent history.
                    audit_contract_version = "none"
                elif staged_audit_batch_count == last_batch_sequence:
                    audit_contract_version = _BACKTEST_CANDIDATE_AUDIT_CONTRACT
                else:
                    raise BacktestIntegrityError(
                        "candidate audit batch coverage is incomplete"
                    )
                conn.execute(
                    """INSERT INTO backtest_staging_audit_contracts (
                           run_id, audit_contract_version
                       ) VALUES (?, ?)""",
                    (run_id, audit_contract_version),
                )
            else:
                audit_contract_version = str(audit_contract[0])
                if audit_contract_version not in {
                    "none",
                    _BACKTEST_CANDIDATE_AUDIT_CONTRACT,
                }:
                    raise BacktestIntegrityError(
                        "candidate audit staging contract is invalid"
                    )

            if audit_contract_version == "none":
                if (
                    conn.execute(
                        """SELECT 1 FROM backtest_staging_audit_batches
                       WHERE run_id=? LIMIT 1""",
                        (run_id,),
                    ).fetchone()
                    or conn.execute(
                        """SELECT 1 FROM backtest_staging_candidate_audits
                       WHERE run_id=? LIMIT 1""",
                        (run_id,),
                    ).fetchone()
                ):
                    raise BacktestIntegrityError(
                        "legacy staging unexpectedly contains candidate audit evidence"
                    )
                candidate_audits = None
                audit_payloads = None
                audit_digest = None
            elif candidate_audits is None:
                raise BacktestIntegrityError(
                    "audited staging batch is missing candidate audit evidence"
                )

            existing = conn.execute(
                """SELECT run_id, batch_sequence, session, payload_encoding,
                          payload_blob, uncompressed_bytes, payload_digest, created_at
                   FROM backtest_staging_batches
                   WHERE run_id=? AND (batch_sequence=? OR session=?)""",
                (run_id, batch_sequence, session.isoformat()),
            ).fetchone()
            if existing is not None:
                same_key = (
                    int(existing[1]) == batch_sequence
                    and str(existing[2]) == session.isoformat()
                )
                same_payload = (
                    str(existing[3]) == _BACKTEST_STAGING_BATCH_ENCODING
                    and int(existing[5]) == len(payload)
                    and str(existing[6]) == payload_digest
                )
                stored_audit = conn.execute(
                    """SELECT audit_contract_version, session, candidate_count,
                              audit_digest
                       FROM backtest_staging_audit_batches
                       WHERE run_id=? AND batch_sequence=?""",
                    (run_id, batch_sequence),
                ).fetchone()
                if candidate_audits is None:
                    same_audit = stored_audit is None
                else:
                    stored_rows = conn.execute(
                        """SELECT candidate_sequence, payload_json, payload_digest
                           FROM backtest_staging_candidate_audits
                           WHERE run_id=? AND batch_sequence=?
                           ORDER BY candidate_sequence""",
                        (run_id, batch_sequence),
                    ).fetchall()
                    same_audit = (
                        stored_audit is not None
                        and str(stored_audit[0]) == _BACKTEST_CANDIDATE_AUDIT_CONTRACT
                        and str(stored_audit[1]) == session.isoformat()
                        and int(stored_audit[2]) == len(audit_payloads or ())
                        and str(stored_audit[3]) == audit_digest
                        and tuple(
                            (int(row[0]), str(row[1]), str(row[2]))
                            for row in stored_rows
                        )
                        == (audit_payloads or ())
                    )
                same_payload = same_payload and same_audit
                if same_key and same_payload:
                    self._decode_backtest_staging_batch(existing)
                    stored_selection = self._load_entry_selection(
                        conn,
                        "backtest_staging_entry_selection",
                        "backtest_staging_entry_selection_decisions",
                        run_id,
                    )
                    requested_selection = (
                        None
                        if initial_entry_selection is None
                        else initial_entry_selection.model_dump(mode="json")
                    )
                    stored_selection_payload = (
                        None
                        if stored_selection is None
                        else stored_selection.model_dump(mode="json")
                    )
                    if requested_selection != stored_selection_payload:
                        raise BacktestIntegrityError(
                            "staging retry has a different initial selection"
                        )
                    if last_batch_sequence < batch_sequence:
                        raise BacktestIntegrityError(
                            "staging checkpoint is behind an existing batch"
                        )
                    if last_batch_sequence == batch_sequence and (
                        str(checkpoint[0]) != state_json
                        or Decimal(str(checkpoint[3])) != final_cash_base
                    ):
                        raise BacktestIntegrityError(
                            "staging retry has a different checkpoint"
                        )
                    return
                raise BacktestIntegrityError(
                    "staging batch key has a different payload"
                )

            if batch_sequence != last_batch_sequence + 1:
                raise BacktestIntegrityError(
                    "staging batch sequence is not the next batch"
                )
            if last_session is not None and session <= last_session:
                raise BacktestIntegrityError(
                    "staging batch sessions are not increasing"
                )
            if event_sequences and event_sequences[0] <= last_event_sequence:
                raise BacktestIntegrityError("staging event sequence is not increasing")
            if equity_point.sequence <= last_equity_sequence:
                raise BacktestIntegrityError(
                    "staging equity sequence is not increasing"
                )

            if initial_entry_selection is not None:
                if batch_sequence != 1:
                    raise BacktestIntegrityError(
                        "initial entry selection must be published with the first batch"
                    )
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
                        expected_session=session,
                    )
                except StrategyProtocolError as exc:
                    raise BacktestIntegrityError(
                        "staged initial entry selection is invalid",
                        code=exc.code.value,
                    ) from exc
                if (
                    conn.execute(
                        "SELECT 1 FROM backtest_staging_entry_selection WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    is not None
                ):
                    raise BacktestIntegrityError(
                        "initial entry selection was already published"
                    )

            last_event_sequence = max(
                last_event_sequence, max(event_sequences, default=0)
            )
            last_equity_sequence = equity_point.sequence
            last_session_text = session.isoformat()
            conn.execute(
                """INSERT INTO backtest_staging (
                       run_id, state_schema_version, state_json, events_json,
                       equity_curve_json, final_cash_base, updated_at,
                       last_batch_sequence, last_session, last_event_sequence,
                       last_equity_sequence
                   ) VALUES (?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       state_schema_version=excluded.state_schema_version,
                       state_json=excluded.state_json,
                       events_json='[]', equity_curve_json='[]',
                       final_cash_base=excluded.final_cash_base,
                       updated_at=excluded.updated_at,
                       last_batch_sequence=excluded.last_batch_sequence,
                       last_session=excluded.last_session,
                       last_event_sequence=excluded.last_event_sequence,
                       last_equity_sequence=excluded.last_equity_sequence""",
                (
                    run_id,
                    state_schema_version,
                    state_json,
                    str(final_cash_base),
                    now,
                    batch_sequence,
                    last_session_text,
                    last_event_sequence,
                    last_equity_sequence,
                ),
            )
            try:
                conn.execute(
                    """INSERT INTO backtest_staging_batches (
                           run_id, batch_sequence, session, payload_encoding,
                           payload_blob, uncompressed_bytes, payload_digest, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        batch_sequence,
                        last_session_text,
                        _BACKTEST_STAGING_BATCH_ENCODING,
                        compressed,
                        len(payload),
                        payload_digest,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BacktestIntegrityError(
                    "backtest staging batch insert failed"
                ) from exc
            if candidate_audits is not None:
                conn.execute(
                    """INSERT INTO backtest_staging_audit_batches (
                           run_id, batch_sequence, session, audit_contract_version,
                           candidate_count, audit_digest, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        batch_sequence,
                        last_session_text,
                        _BACKTEST_CANDIDATE_AUDIT_CONTRACT,
                        len(audit_payloads or ()),
                        audit_digest,
                        now,
                    ),
                )
                conn.executemany(
                    """INSERT INTO backtest_staging_candidate_audits (
                           run_id, batch_sequence, candidate_sequence,
                           payload_json, payload_digest
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        (run_id, batch_sequence, *audit_payload)
                        for audit_payload in (audit_payloads or ())
                    ),
                )
            if initial_entry_selection is not None:
                self._insert_entry_selection(
                    conn,
                    "backtest_staging_entry_selection",
                    "backtest_staging_entry_selection_decisions",
                    run_id,
                    initial_entry_selection,
                )

    def delete_backtest_staging(
        self,
        run_id: str,
        *,
        claim_token: str,
        expected_version: int,
        lease: WorkerLeaseFenceV1 | None = None,
    ) -> None:
        """Fence and atomically remove one running attempt's staging."""
        with _db_session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._load_strategy_job(conn, run_id)
            fence = _lease_fence_params(lease)
            owned = conn.execute(
                f"""SELECT 1 FROM strategy_jobs
                    WHERE id=? AND status='running' AND claim_token=?
                      AND status_version=? {_LEASE_FENCE_SQL}""",
                (run_id, claim_token, expected_version, *fence),
            ).fetchone()
            if owned is None:
                raise StrategyJobConflict("staging cleanup ownership is stale")
            conn.execute("DELETE FROM backtest_staging WHERE run_id=?", (run_id,))

    def read_backtest_staging_batches(
        self, run_id: str
    ) -> tuple[BacktestStagingBatchV1, ...]:
        """Read and strictly validate an attempt's batches in sequence order."""
        with _db_session(self._connect) as conn:
            return self._load_backtest_staging_batches_on_connection(conn, run_id)

    def read_backtest_staging_candidate_audits(
        self, run_id: str
    ) -> tuple[CandidateAuditV1, ...] | None:
        """Read staged candidate audits, or ``None`` for legacy batches."""
        with _db_session(self._connect) as conn:
            batches = self._load_backtest_staging_batches_on_connection(conn, run_id)
            return self._load_backtest_staging_candidate_audits_on_connection(
                conn, run_id, batches
            )

    def read_backtest_staging_checkpoint(
        self, run_id: str
    ) -> BacktestStagingCheckpointV1 | None:
        """Read the latest portfolio checkpoint without cumulative history."""
        with _db_session(self._connect) as conn:
            return self._load_backtest_staging_checkpoint_on_connection(conn, run_id)

    @classmethod
    def _load_backtest_staging_batches_on_connection(
        cls, conn: sqlite3.Connection, run_id: str
    ) -> tuple[BacktestStagingBatchV1, ...]:
        rows = conn.execute(
            """SELECT run_id, batch_sequence, session, payload_encoding,
                      payload_blob, uncompressed_bytes, payload_digest, created_at
               FROM backtest_staging_batches
               WHERE run_id=? ORDER BY batch_sequence""",
            (run_id,),
        ).fetchall()
        batches = tuple(cls._decode_backtest_staging_batch(row) for row in rows)
        cls._validate_backtest_staging_batch_order(conn, run_id, batches)
        return batches

    def _load_backtest_staging_checkpoint_on_connection(
        self, conn: sqlite3.Connection, run_id: str
    ) -> BacktestStagingCheckpointV1 | None:
        row = conn.execute(
            """SELECT run_id, state_schema_version, state_json,
                          final_cash_base, last_batch_sequence, last_session,
                          last_event_sequence, last_equity_sequence, updated_at
                   FROM backtest_staging WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(str(row[2]))
            if not isinstance(state, dict):
                raise ValueError("checkpoint state is not an object")
            final_cash = Decimal(str(row[3]))
            if not final_cash.is_finite():
                raise ValueError("checkpoint cash is not finite")
            last_session = None if row[5] is None else date.fromisoformat(str(row[5]))
            values = tuple(int(row[index]) for index in (4, 6, 7))
            if any(value < 0 for value in values):
                raise ValueError("checkpoint high-water mark is negative")
        except (
            json.JSONDecodeError,
            InvalidOperation,
            TypeError,
            ValueError,
        ) as exc:
            raise BacktestIntegrityError(
                "backtest staging checkpoint is invalid"
            ) from exc
        return BacktestStagingCheckpointV1(
            run_id=str(row[0]),
            state_schema_version=str(row[1]),
            portfolio_state=state,
            final_cash_base=final_cash,
            last_batch_sequence=values[0],
            last_session=last_session,
            last_event_sequence=values[1],
            last_equity_sequence=values[2],
            updated_at=str(row[8]),
            initial_entry_selection=self._load_entry_selection(
                conn,
                "backtest_staging_entry_selection",
                "backtest_staging_entry_selection_decisions",
                run_id,
            ),
        )

    @classmethod
    def _encode_backtest_staging_batch(
        cls,
        batch_session: date,
        events: tuple[TradeLogEvent, ...],
        equity_point: EquityCurvePointV1,
    ) -> tuple[bytes, bytes, str]:
        payload = {
            "events": [event.model_dump(mode="json") for event in events],
            "equity_curve": [equity_point.model_dump(mode="json")],
            "session": batch_session.isoformat(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return raw, zlib.compress(raw), sha256(raw).hexdigest()

    @staticmethod
    def _encode_candidate_audit_batch(
        batch_session: date, records: tuple[CandidateAuditV1, ...]
    ) -> tuple[tuple[tuple[int, str, str], ...], str]:
        from app.services.backtest.backtest_engine import CandidateAuditV1

        ordered = tuple(sorted(records, key=lambda item: item.candidate_sequence))
        if len({item.candidate_sequence for item in ordered}) != len(ordered):
            raise ValueError("candidate audit batch repeats a candidate sequence")
        payloads: list[tuple[int, str, str]] = []
        for record in ordered:
            if not isinstance(record, CandidateAuditV1):
                raise ValueError("candidate audit row has an unsupported type")
            if record.outcome_session != batch_session:
                raise ValueError("candidate audit outcome session differs from batch")
            payload = json.dumps(
                record.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payloads.append(
                (
                    record.candidate_sequence,
                    payload,
                    sha256(payload.encode()).hexdigest(),
                )
            )
        digest = manifest_digest(
            {
                "contract_version": _BACKTEST_CANDIDATE_AUDIT_CONTRACT,
                "session": batch_session.isoformat(),
                "rows": [item[2] for item in payloads],
            }
        )
        return tuple(payloads), digest

    @staticmethod
    def _validate_candidate_audit_event_links(
        outcome_session: date,
        records: tuple[CandidateAuditV1, ...],
        events: tuple[TradeLogEvent, ...],
    ) -> None:
        from app.services.backtest.backtest_engine import (
            CandidateAuditDisposition,
            CandidateAuditV1,
            EntryFillEventV1,
            SignalSide,
            SkippedSignalEventV1,
            SkipReasonCode,
        )

        event_by_sequence = {event.sequence: event for event in events}
        linked: set[int] = set()
        for record in records:
            if not isinstance(record, CandidateAuditV1):
                raise BacktestIntegrityError("candidate audit row has an invalid type")
            event = event_by_sequence.get(record.event_sequence)
            if event is None or record.event_sequence in linked:
                raise BacktestIntegrityError("candidate audit event link is missing")
            linked.add(record.event_sequence)
            common = (
                record.outcome_session == outcome_session
                and record.side is SignalSide.BUY
                and event.security_id == record.security_id
                and getattr(event, "side", SignalSide.BUY) is SignalSide.BUY
                and getattr(event, "signal_session", None) == record.signal_session
                and getattr(event, "rule_id", None) == record.rule_id
            )
            if isinstance(event, EntryFillEventV1):
                valid = (
                    common
                    and record.disposition is CandidateAuditDisposition.FILLED
                    and record.reason_code is None
                    and event.fill_session == record.outcome_session
                    and event.fill_session == record.intended_fill_session
                )
            elif isinstance(event, SkippedSignalEventV1):
                valid_disposition = record.disposition in {
                    CandidateAuditDisposition.PREFLIGHT_REJECTED,
                    CandidateAuditDisposition.FULL_BOOK_REJECTED,
                    CandidateAuditDisposition.COMPETITION_REJECTED,
                    CandidateAuditDisposition.FILL_REJECTED,
                }
                valid = (
                    common
                    and valid_disposition
                    and record.reason_code == event.reason.value
                )
                if record.disposition in {
                    CandidateAuditDisposition.FULL_BOOK_REJECTED,
                    CandidateAuditDisposition.COMPETITION_REJECTED,
                }:
                    valid = (
                        valid
                        and event.reason is SkipReasonCode.MAX_CONCURRENT_POSITIONS
                    )
                if record.disposition is CandidateAuditDisposition.FULL_BOOK_REJECTED:
                    valid = valid and record.available_slots_before_cohort == 0
                elif (
                    record.disposition is CandidateAuditDisposition.COMPETITION_REJECTED
                ):
                    valid = (
                        valid
                        and record.available_slots_before_cohort is not None
                        and record.available_slots_before_cohort > 0
                        and record.available_slots_before_candidate == 0
                    )
                elif record.disposition is CandidateAuditDisposition.PREFLIGHT_REJECTED:
                    valid = (
                        valid
                        and record.host_cohort_position is None
                        and record.intended_fill_session is None
                    )
                elif record.disposition is CandidateAuditDisposition.FILL_REJECTED:
                    valid = valid and record.intended_fill_session == outcome_session
            else:
                valid = False
            if not valid:
                raise BacktestIntegrityError("candidate audit event link is invalid")

    @classmethod
    def _validate_backtest_staging_candidate_audits_on_connection(
        cls,
        conn: sqlite3.Connection,
        run_id: str,
        batches: tuple[BacktestStagingBatchV1, ...],
    ) -> BacktestCandidateAuditPromotionV1 | None:
        """Validate staged audit one session at a time and retain only counts."""
        from app.services.backtest.backtest_engine import CandidateAuditV1

        audit_batch_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM backtest_staging_audit_batches WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        audit_row_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM backtest_staging_candidate_audits WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        audit_contract = conn.execute(
            """SELECT audit_contract_version
               FROM backtest_staging_audit_contracts WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if audit_contract is None:
            if audit_batch_count or audit_row_count:
                raise BacktestIntegrityError(
                    "candidate audit staging contract is missing"
                )
            return None
        contract_version = str(audit_contract[0])
        if contract_version == "none":
            if audit_batch_count or audit_row_count:
                raise BacktestIntegrityError(
                    "legacy staging unexpectedly contains candidate audit evidence"
                )
            return None
        if contract_version != _BACKTEST_CANDIDATE_AUDIT_CONTRACT:
            raise BacktestIntegrityError("candidate audit staging contract is invalid")
        if audit_batch_count != len(batches):
            raise BacktestIntegrityError("candidate audit batch coverage is incomplete")

        summary = {
            "candidate_count": 0,
            "priority_recorded": 0,
            "priority_missing": 0,
            "explanation_recorded": 0,
            "explanation_missing": 0,
            "preflight_rejected": 0,
            "full_book_rejected": 0,
            "competition_rejected": 0,
            "filled": 0,
            "fill_rejected": 0,
        }
        for batch in batches:
            header = conn.execute(
                """SELECT session, audit_contract_version, candidate_count,
                          audit_digest
                   FROM backtest_staging_audit_batches
                   WHERE run_id=? AND batch_sequence=?""",
                (run_id, batch.batch_sequence),
            ).fetchone()
            if (
                header is None
                or str(header[0]) != batch.session.isoformat()
                or str(header[1]) != _BACKTEST_CANDIDATE_AUDIT_CONTRACT
            ):
                raise BacktestIntegrityError("candidate audit batch header is invalid")
            rows = conn.execute(
                """SELECT candidate_sequence, payload_json, payload_digest
                   FROM backtest_staging_candidate_audits
                   WHERE run_id=? AND batch_sequence=? ORDER BY candidate_sequence""",
                (run_id, batch.batch_sequence),
            ).fetchall()
            parsed: list[CandidateAuditV1] = []
            payloads: list[tuple[int, str, str]] = []
            for row in rows:
                sequence, payload_json, payload_digest = (
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                )
                if sha256(payload_json.encode()).hexdigest() != payload_digest:
                    raise BacktestIntegrityError(
                        "candidate audit row digest is invalid"
                    )
                try:
                    record = CandidateAuditV1.model_validate(
                        json.loads(payload_json), strict=False
                    )
                    canonical = json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    ValidationError,
                ) as exc:
                    raise BacktestIntegrityError(
                        "candidate audit row is invalid"
                    ) from exc
                if record.candidate_sequence != sequence or canonical != payload_json:
                    raise BacktestIntegrityError("candidate audit row is not canonical")
                parsed.append(record)
                payloads.append((sequence, payload_json, payload_digest))

            if int(header[2]) != len(parsed):
                raise BacktestIntegrityError(
                    "candidate audit count does not match staged rows"
                )
            encoded, digest = cls._encode_candidate_audit_batch(
                batch.session, tuple(parsed)
            )
            if str(header[3]) != digest or tuple(payloads) != encoded:
                raise BacktestIntegrityError("candidate audit batch digest is invalid")
            cls._validate_candidate_audit_event_links(
                batch.session, tuple(parsed), batch.events
            )
            all_buy_events = {
                event.sequence
                for event in batch.events
                if event.kind == "entry_fill"
                or (event.kind == "skipped_signal" and event.side.value == "BUY")
            }
            if all_buy_events != {record.event_sequence for record in parsed}:
                raise BacktestIntegrityError("candidate audit is missing a BUY outcome")
            for key, value in cls._candidate_audit_summary_payload(
                tuple(parsed)
            ).items():
                summary[key] += value

        last_candidate_sequence = 0

        def validated_candidate_digests() -> Iterable[str]:
            nonlocal last_candidate_sequence
            digest_rows = conn.execute(
                """SELECT candidate_sequence, payload_digest
                   FROM backtest_staging_candidate_audits
               WHERE run_id=? ORDER BY candidate_sequence""",
                (run_id,),
            )
            for row in digest_rows:
                sequence = int(row[0])
                if sequence != last_candidate_sequence + 1:
                    raise BacktestIntegrityError(
                        "candidate audit sequence coverage is invalid"
                    )
                last_candidate_sequence = sequence
                yield str(row[1])

        audit_digest = cls._candidate_audit_digest_from_row_digests(
            summary, validated_candidate_digests()
        )
        if last_candidate_sequence != summary["candidate_count"]:
            raise BacktestIntegrityError("candidate audit sequence coverage is invalid")
        return BacktestCandidateAuditPromotionV1(
            summary=summary, audit_digest=audit_digest
        )

    @classmethod
    def _load_backtest_staging_candidate_audits_on_connection(
        cls,
        conn: sqlite3.Connection,
        run_id: str,
        batches: tuple[BacktestStagingBatchV1, ...],
    ) -> tuple[CandidateAuditV1, ...] | None:
        from app.services.backtest.backtest_engine import CandidateAuditV1

        audit_batch_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM backtest_staging_audit_batches WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        audit_row_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM backtest_staging_candidate_audits WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        audit_contract = conn.execute(
            """SELECT audit_contract_version
               FROM backtest_staging_audit_contracts WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if audit_contract is None:
            if audit_batch_count or audit_row_count:
                raise BacktestIntegrityError(
                    "candidate audit staging contract is missing"
                )
            return None
        if str(audit_contract[0]) == "none":
            if audit_batch_count or audit_row_count:
                raise BacktestIntegrityError(
                    "legacy staging unexpectedly contains candidate audit evidence"
                )
            return None
        if str(audit_contract[0]) != _BACKTEST_CANDIDATE_AUDIT_CONTRACT:
            raise BacktestIntegrityError("candidate audit staging contract is invalid")
        if audit_batch_count != len(batches):
            raise BacktestIntegrityError("candidate audit batch coverage is incomplete")

        records: list[CandidateAuditV1] = []
        for batch in batches:
            header = conn.execute(
                """SELECT session, audit_contract_version, candidate_count,
                          audit_digest
                   FROM backtest_staging_audit_batches
                   WHERE run_id=? AND batch_sequence=?""",
                (run_id, batch.batch_sequence),
            ).fetchone()
            if (
                header is None
                or str(header[0]) != batch.session.isoformat()
                or str(header[1]) != _BACKTEST_CANDIDATE_AUDIT_CONTRACT
            ):
                raise BacktestIntegrityError("candidate audit batch header is invalid")
            rows = conn.execute(
                """SELECT candidate_sequence, payload_json, payload_digest
                   FROM backtest_staging_candidate_audits
                   WHERE run_id=? AND batch_sequence=? ORDER BY candidate_sequence""",
                (run_id, batch.batch_sequence),
            ).fetchall()
            parsed: list[CandidateAuditV1] = []
            payloads: list[tuple[int, str, str]] = []
            for row in rows:
                sequence, payload_json, payload_digest = (
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                )
                if sha256(payload_json.encode()).hexdigest() != payload_digest:
                    raise BacktestIntegrityError(
                        "candidate audit row digest is invalid"
                    )
                try:
                    record = CandidateAuditV1.model_validate(
                        json.loads(payload_json), strict=False
                    )
                    canonical = json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    ValidationError,
                ) as exc:
                    raise BacktestIntegrityError(
                        "candidate audit row is invalid"
                    ) from exc
                if record.candidate_sequence != sequence or canonical != payload_json:
                    raise BacktestIntegrityError("candidate audit row is not canonical")
                parsed.append(record)
                payloads.append((sequence, payload_json, payload_digest))
            encoded, digest = cls._encode_candidate_audit_batch(
                batch.session, tuple(parsed)
            )
            if int(header[2]) != len(parsed):
                raise BacktestIntegrityError(
                    "candidate audit count does not match staged rows"
                )
            if str(header[3]) != digest or tuple(payloads) != encoded:
                raise BacktestIntegrityError("candidate audit batch digest is invalid")
            cls._validate_candidate_audit_event_links(
                batch.session, tuple(parsed), batch.events
            )
            all_buy_events = {
                event.sequence
                for event in batch.events
                if event.kind == "entry_fill"
                or (event.kind == "skipped_signal" and event.side.value == "BUY")
            }
            if all_buy_events != {record.event_sequence for record in parsed}:
                raise BacktestIntegrityError("candidate audit is missing a BUY outcome")
            records.extend(parsed)
        if sorted(item.candidate_sequence for item in records) != list(
            range(1, len(records) + 1)
        ):
            raise BacktestIntegrityError("candidate audit sequence coverage is invalid")
        return tuple(sorted(records, key=lambda item: item.candidate_sequence))

    @classmethod
    def _decode_backtest_staging_batch(
        cls, row: sqlite3.Row | tuple[object, ...]
    ) -> BacktestStagingBatchV1:
        try:
            run_id = str(row[0])
            batch_sequence = int(cast(Any, row[1]))
            stored_session = str(row[2])
            encoding = str(row[3])
            compressed = row[4]
            claimed_size = int(cast(Any, row[5]))
            stored_digest = str(row[6])
            created_at = str(row[7])
            if (
                batch_sequence <= 0
                or encoding != _BACKTEST_STAGING_BATCH_ENCODING
                or not isinstance(compressed, (bytes, bytearray, memoryview))
                or claimed_size <= 0
                or len(stored_digest) != 64
            ):
                raise ValueError("invalid batch metadata")
            batch_session = date.fromisoformat(stored_session)
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(bytes(compressed), claimed_size + 1)
            if (
                decompressor.unconsumed_tail
                or decompressor.unused_data
                or not decompressor.eof
                or len(raw) != claimed_size
                or sha256(raw).hexdigest() != stored_digest
            ):
                raise ValueError("invalid batch compression or digest")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "events",
                "equity_curve",
                "session",
            }:
                raise ValueError("invalid batch payload shape")
            if payload["session"] != stored_session:
                raise ValueError("batch payload session mismatch")
            event_payloads = payload["events"]
            curve_payloads = payload["equity_curve"]
            if not isinstance(event_payloads, list) or not isinstance(
                curve_payloads, list
            ):
                raise ValueError("batch arrays are invalid")
            if len(curve_payloads) != 1:
                raise ValueError("batch must contain one equity point")
            events = tuple(cls._parse_trade_log_event(item) for item in event_payloads)
            equity_point = cls._parse_equity_curve_point(curve_payloads[0])
            event_sequences = [event.sequence for event in events]
            if event_sequences != sorted(event_sequences) or len(
                set(event_sequences)
            ) != len(event_sequences):
                raise ValueError("batch events are unordered")
            if equity_point.session != batch_session:
                raise ValueError("batch equity session mismatch")
            canonical, _compressed, _digest = cls._encode_backtest_staging_batch(
                batch_session, events, equity_point
            )
            if canonical != raw:
                raise ValueError("batch payload is not canonical")
        except (
            IndexError,
            InvalidOperation,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            ValidationError,
            json.JSONDecodeError,
            zlib.error,
        ) as exc:
            raise BacktestIntegrityError("invalid backtest staging batch") from exc
        return BacktestStagingBatchV1(
            run_id=run_id,
            batch_sequence=batch_sequence,
            session=batch_session,
            events=events,
            equity_point=equity_point,
            payload_encoding=encoding,
            uncompressed_bytes=claimed_size,
            payload_digest=stored_digest,
            created_at=created_at,
        )

    @staticmethod
    def _validate_backtest_staging_batch_order(
        conn: sqlite3.Connection,
        run_id: str,
        batches: tuple[BacktestStagingBatchV1, ...],
    ) -> None:
        run_range = conn.execute(
            "SELECT start_month, end_month FROM strategy_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_range is None and batches:
            raise BacktestIntegrityError("backtest staging run is missing")
        last_session: date | None = None
        last_event_sequence = 0
        last_equity_sequence = 0
        for expected_sequence, batch in enumerate(batches, start=1):
            batch_month = batch.session.strftime("%Y-%m")
            if run_range is not None and not (
                str(run_range[0]) <= batch_month <= str(run_range[1])
            ):
                raise BacktestIntegrityError(
                    "backtest staging batch session is outside the pinned run range"
                )
            if batch.batch_sequence != expected_sequence:
                raise BacktestIntegrityError(
                    "backtest staging batches are not contiguous"
                )
            if last_session is not None and batch.session <= last_session:
                raise BacktestIntegrityError(
                    "backtest staging sessions are not increasing"
                )
            event_sequences = [event.sequence for event in batch.events]
            if event_sequences and event_sequences[0] <= last_event_sequence:
                raise BacktestIntegrityError(
                    "backtest staging events are not increasing"
                )
            if batch.equity_point.sequence <= last_equity_sequence:
                raise BacktestIntegrityError(
                    "backtest staging equity is not increasing"
                )
            last_session = batch.session
            last_event_sequence = max(
                last_event_sequence, max(event_sequences, default=0)
            )
            last_equity_sequence = batch.equity_point.sequence

        checkpoint = conn.execute(
            """SELECT last_batch_sequence, last_session, last_event_sequence,
                      last_equity_sequence, final_cash_base
               FROM backtest_staging WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if checkpoint is None:
            if batches:
                raise BacktestIntegrityError("backtest staging checkpoint is missing")
            return
        try:
            checkpoint_values = (
                int(checkpoint[0]),
                None if checkpoint[1] is None else str(checkpoint[1]),
                int(checkpoint[2]),
                int(checkpoint[3]),
                Decimal(str(checkpoint[4])),
            )
        except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
            raise BacktestIntegrityError(
                "backtest staging checkpoint is invalid"
            ) from exc
        if checkpoint_values[0] != len(batches):
            raise BacktestIntegrityError(
                "backtest staging checkpoint does not match batches"
            )
        if (
            checkpoint_values[1]
            != (None if last_session is None else last_session.isoformat())
            or checkpoint_values[2] != last_event_sequence
            or checkpoint_values[3] != last_equity_sequence
        ):
            raise BacktestIntegrityError(
                "backtest staging checkpoint high-water mark is invalid"
            )
        if batches and checkpoint_values[4] != batches[-1].equity_point.cash_base:
            raise BacktestIntegrityError(
                "backtest staging checkpoint cash does not match latest batch"
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
        from app.services.backtest.metrics import (
            ClosedTrade,
            MetricsError,
            calculate_metrics,
        )

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
            checkpoint = self._load_backtest_staging_checkpoint_on_connection(
                conn, job_id
            )
            if staging is None or checkpoint is None:
                raise StrategyJobConflict("no staging exists for this run")

            candidate_audit_promotion: BacktestCandidateAuditPromotionV1 | None = None
            batch_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM backtest_staging_batches WHERE run_id=?",
                    (job_id,),
                ).fetchone()[0]
            )
            if checkpoint.last_batch_sequence and not batch_count:
                raise BacktestIntegrityError("backtest staging batches are missing")
            if batch_count:
                if staging.events or staging.equity_curve:
                    raise BacktestIntegrityError(
                        "backtest staging mixes legacy and batch payloads"
                    )
                batches = self._load_backtest_staging_batches_on_connection(
                    conn, job_id
                )
                if checkpoint.initial_entry_selection is not None and (
                    not batches
                    or checkpoint.initial_entry_selection.session != batches[0].session
                ):
                    raise BacktestIntegrityError(
                        "initial entry selection does not match first batch"
                    )
                staging = BacktestStagingV1(
                    run_id=checkpoint.run_id,
                    state_schema_version=checkpoint.state_schema_version,
                    portfolio_state=checkpoint.portfolio_state,
                    events=tuple(event for batch in batches for event in batch.events),
                    equity_curve=tuple(batch.equity_point for batch in batches),
                    final_cash_base=checkpoint.final_cash_base,
                    updated_at=checkpoint.updated_at,
                    initial_entry_selection=checkpoint.initial_entry_selection,
                )
                candidate_audit_promotion = (
                    self._validate_backtest_staging_candidate_audits_on_connection(
                        conn, job_id, batches
                    )
                )

            closed_trades = tuple(
                event for event in staging.events if isinstance(event, ClosedTrade)
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
                expected_audit_version = (
                    "none"
                    if candidate_audit_promotion is None
                    else _BACKTEST_CANDIDATE_AUDIT_CONTRACT
                )
                stored_result = conn.execute(
                    "SELECT audit_contract_version FROM backtest_results WHERE run_id=?",
                    (job_id,),
                ).fetchone()
                if (
                    stored_result is None
                    or str(stored_result[0]) != expected_audit_version
                ):
                    raise BacktestIntegrityError(
                        "conflicting repeat completion audit contract"
                    )
                if candidate_audit_promotion is not None:
                    stored_summary = (
                        self._load_backtest_candidate_audit_summary_on_connection(
                            conn, job_id, expected_audit_version
                        )
                    )
                    if (
                        stored_summary.audit_digest
                        != candidate_audit_promotion.audit_digest
                    ):
                        raise BacktestIntegrityError(
                            "conflicting repeat completion candidate audit"
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
                    candidate_audit_promotion=candidate_audit_promotion,
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

    def backtest_result(
        self, run_id: str, *, include_candidate_audit_summary: bool = True
    ) -> BacktestResultV1:
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

        ``include_candidate_audit_summary=False`` lets the Result page load
        the independently paginated companion audit so its integrity errors
        stay local to that section and each request reads only one audit page.
        """
        from app.services.backtest.metrics import (
            BacktestMetricsV1,
            ClosedTrade,
            MetricsError,
            metric_availability,
        )

        with session(self._connect) as conn:
            strategy_run = self._load_strategy_run_row(conn, run_id)
            row = conn.execute(
                """SELECT result_schema_version, metrics_json, final_cash_base,
                          result_digest, note,
                          note_version, completed_at, audit_contract_version
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
            candidate_audit_summary = (
                self._load_backtest_candidate_audit_summary_on_connection(
                    conn, run_id, str(row[7])
                )
                if include_candidate_audit_summary
                else None
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
            event for event in events if isinstance(event, ClosedTrade)
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
            regime_benchmark=strategy_run.regime_benchmark,
            initial_entry_selection=initial_entry_selection,
            candidate_audit_summary=candidate_audit_summary,
        )

    def backtest_result_candidate_audit_page(
        self, run_id: str, *, page: int = 1, page_size: int = 25
    ) -> BacktestCandidateAuditPageV1:
        """Read one bounded, integrity-checked page of persisted candidates."""
        from app.services.backtest.backtest_engine import CandidateAuditV1

        if type(page) is not int or page < 1:
            raise ValueError("candidate audit page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("candidate audit page size must be between 1 and 100")
        with session(self._connect) as conn:
            result_row = conn.execute(
                "SELECT audit_contract_version FROM backtest_results WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if result_row is None:
                raise StrategyJobNotFound(f"backtest result not found: {run_id}")
            summary = self._load_backtest_candidate_audit_summary_on_connection(
                conn, run_id, str(result_row[0])
            )
            total_pages = max(1, (summary.candidate_count + page_size - 1) // page_size)
            if page > total_pages:
                raise ValueError("candidate audit page is outside the Result")
            if not summary.recorded:
                return BacktestCandidateAuditPageV1(
                    summary=summary,
                    page=page,
                    page_size=page_size,
                    total_pages=total_pages,
                    records=(),
                )
            rows = conn.execute(
                """SELECT candidate_sequence, payload_json, payload_digest
                   FROM backtest_result_candidate_audits
                   WHERE run_id=? ORDER BY candidate_sequence LIMIT ? OFFSET ?""",
                (run_id, page_size, (page - 1) * page_size),
            ).fetchall()
            row_offset = (page - 1) * page_size
            expected_page_count = min(
                page_size, max(0, summary.candidate_count - row_offset)
            )
            if len(rows) != expected_page_count:
                raise BacktestIntegrityError("candidate audit row count is invalid")
            records: list[CandidateAuditV1] = []
            for page_offset, row in enumerate(rows):
                sequence, payload_json, payload_digest = (
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                )
                if sequence != row_offset + page_offset + 1:
                    raise BacktestIntegrityError(
                        "candidate audit sequence coverage is invalid"
                    )
                if sha256(payload_json.encode()).hexdigest() != payload_digest:
                    raise BacktestIntegrityError(
                        "candidate audit row digest is invalid"
                    )
                try:
                    record = CandidateAuditV1.model_validate(
                        json.loads(payload_json), strict=False
                    )
                    canonical = json.dumps(
                        record.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    ValidationError,
                ) as exc:
                    raise BacktestIntegrityError(
                        "candidate audit row is invalid"
                    ) from exc
                if record.candidate_sequence != sequence or canonical != payload_json:
                    raise BacktestIntegrityError("candidate audit row is not canonical")
                records.append(record)
            if records:
                event_sequences = tuple(record.event_sequence for record in records)
                placeholders = ",".join("?" for _ in event_sequences)
                event_rows = conn.execute(
                    """SELECT sequence, event_json FROM trade_log
                       WHERE run_id=? AND sequence IN ("""
                    + placeholders
                    + ")",
                    (run_id, *event_sequences),
                ).fetchall()
                events_by_sequence: dict[int, TradeLogEvent] = {}
                for event_row in event_rows:
                    try:
                        event = self._parse_trade_log_event(
                            json.loads(str(event_row[1]))
                        )
                    except (
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                        ValidationError,
                    ) as exc:
                        raise BacktestIntegrityError(
                            "candidate audit event is invalid"
                        ) from exc
                    events_by_sequence[int(event_row[0])] = event
                for record in records:
                    event = events_by_sequence.get(record.event_sequence)
                    if event is None:
                        raise BacktestIntegrityError(
                            "candidate audit event link is missing"
                        )
                    self._validate_candidate_audit_event_links(
                        record.outcome_session, (record,), (event,)
                    )
        return BacktestCandidateAuditPageV1(
            summary=summary,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            records=tuple(records),
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
        parsed = None
        if str(manifest_row[1]) != "{}":
            try:
                from app.services.backtest.run_input_manifest import (
                    read_run_input_manifest,
                )

                parsed = read_run_input_manifest(str(manifest_row[1]))
                if parsed.schema_version != str(
                    row[13]
                ) or not parsed.accepts_stored_digest(str(row[11])):
                    raise ValueError
                if (
                    str(row[13]) in {"run_input_manifest.v2", "run_input_manifest.v3"}
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
            regime_benchmark=getattr(parsed, "regime_benchmark", None)
            if str(row[13]) == "run_input_manifest.v3"
            else None,
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
                Literal[
                    "run_input_manifest.v1",
                    "run_input_manifest.v2",
                    "run_input_manifest.v3",
                ],
                row.manifest_version,
            ),
            universe_selection=row.universe_selection,
            source_preparation_job_id=row.source_preparation_job_id,
            regime_benchmark=row.regime_benchmark,
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
        from app.services.backtest.strategy_job import RegimeBenchmarkPinV1

        row = conn.execute(
            "SELECT selection_json,strategy_id,strategy_api_version,strategy_source_digest,parameters_json,start_month,end_month,base_currency,starting_capital,regime_benchmark_json FROM preparation_runs WHERE job_id=?",
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
                regime_benchmark=(
                    None
                    if row[9] is None
                    else RegimeBenchmarkPinV1.model_validate_json(str(row[9]))
                ),
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
        candidate_audit_promotion: BacktestCandidateAuditPromotionV1 | None,
    ) -> None:
        metrics_json = json.dumps(
            metrics.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        conn.execute(
            """INSERT INTO backtest_results (
                   run_id, result_schema_version, audit_contract_version,
                   metrics_json, final_cash_base, result_digest,
                   note, note_version, completed_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)""",
            (
                run_id,
                (
                    "backtest_result.v2"
                    if initial_entry_selection is not None
                    else "backtest_result.v1"
                ),
                (
                    "none"
                    if candidate_audit_promotion is None
                    else _BACKTEST_CANDIDATE_AUDIT_CONTRACT
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
        if candidate_audit_promotion is not None:
            summary = candidate_audit_promotion.summary
            conn.execute(
                """INSERT INTO backtest_result_audit_manifests (
                       run_id, audit_contract_version, candidate_count,
                       summary_json, audit_digest, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    _BACKTEST_CANDIDATE_AUDIT_CONTRACT,
                    summary["candidate_count"],
                    json.dumps(summary, sort_keys=True, separators=(",", ":")),
                    candidate_audit_promotion.audit_digest,
                    completed_at,
                ),
            )
            conn.execute(
                """INSERT INTO backtest_result_candidate_audits (
                       run_id, candidate_sequence, payload_json, payload_digest
                   ) SELECT ?, candidate_sequence, payload_json, payload_digest
                     FROM backtest_staging_candidate_audits
                    WHERE run_id=? ORDER BY candidate_sequence""",
                (run_id, run_id),
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
    def _candidate_audit_summary_payload(
        records: tuple[CandidateAuditV1, ...],
    ) -> dict[str, int]:
        from app.services.backtest.backtest_engine import CandidateAuditDisposition

        counts = {
            "candidate_count": len(records),
            "priority_recorded": sum(record.priority is not None for record in records),
            "priority_missing": sum(record.priority is None for record in records),
            "explanation_recorded": sum(
                record.explanation is not None for record in records
            ),
            "explanation_missing": sum(
                record.explanation is None for record in records
            ),
            "preflight_rejected": 0,
            "full_book_rejected": 0,
            "competition_rejected": 0,
            "filled": 0,
            "fill_rejected": 0,
        }
        for record in records:
            key = {
                CandidateAuditDisposition.PREFLIGHT_REJECTED: "preflight_rejected",
                CandidateAuditDisposition.FULL_BOOK_REJECTED: "full_book_rejected",
                CandidateAuditDisposition.COMPETITION_REJECTED: "competition_rejected",
                CandidateAuditDisposition.FILLED: "filled",
                CandidateAuditDisposition.FILL_REJECTED: "fill_rejected",
            }[record.disposition]
            counts[key] += 1
        return counts

    @staticmethod
    def _candidate_audit_digest_from_row_digests(
        summary: Mapping[str, int], row_digests: Iterable[str]
    ) -> str:
        candidate_count = summary["candidate_count"]
        digest = sha256()
        digest.update(
            (
                f'{{"candidate_count":{candidate_count},'
                f'"contract_version":{json.dumps(_BACKTEST_CANDIDATE_AUDIT_CONTRACT)},'
                '"row_digests":['
            ).encode()
        )
        seen = 0
        for seen, row_digest in enumerate(row_digests, start=1):
            if seen > 1:
                digest.update(b",")
            digest.update(json.dumps(row_digest).encode())
        if seen != candidate_count:
            raise BacktestIntegrityError("candidate audit row count is invalid")
        digest.update(b'],"summary":')
        digest.update(
            json.dumps(dict(summary), sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"}")
        return digest.hexdigest()

    @classmethod
    def _load_backtest_candidate_audit_summary_on_connection(
        cls,
        conn: sqlite3.Connection,
        run_id: str,
        contract_version: str,
    ) -> BacktestCandidateAuditSummaryV1:
        """Validate the persisted audit summary without scanning all rows.

        Promotion validates the complete staged contract. Result reads check
        the manifest and row count here, then validate payloads and event links
        only for the requested page.
        """
        manifest = conn.execute(
            """SELECT audit_contract_version, candidate_count, summary_json,
                      audit_digest
               FROM backtest_result_audit_manifests WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if contract_version == "none":
            has_audit_rows = conn.execute(
                """SELECT 1 FROM backtest_result_candidate_audits
                   WHERE run_id=? LIMIT 1""",
                (run_id,),
            ).fetchone()
            if manifest is not None or has_audit_rows:
                raise BacktestIntegrityError(
                    "legacy Result unexpectedly contains candidate audit evidence"
                )
            return BacktestCandidateAuditSummaryV1(
                recorded=False, contract_version="not_recorded"
            )
        if contract_version != _BACKTEST_CANDIDATE_AUDIT_CONTRACT:
            raise BacktestIntegrityError("stored candidate audit contract is invalid")
        if manifest is None:
            raise BacktestIntegrityError("candidate audit manifest is missing")
        if str(manifest[0]) != _BACKTEST_CANDIDATE_AUDIT_CONTRACT:
            raise BacktestIntegrityError("candidate audit manifest version is invalid")
        expected_count = int(manifest[1])
        if expected_count == 0:
            has_audit_rows = conn.execute(
                """SELECT 1 FROM backtest_result_candidate_audits
                   WHERE run_id=? LIMIT 1""",
                (run_id,),
            ).fetchone()
            if has_audit_rows:
                raise BacktestIntegrityError("candidate audit row count is invalid")
        else:
            first_row = conn.execute(
                """SELECT candidate_sequence FROM backtest_result_candidate_audits
                   WHERE run_id=? ORDER BY candidate_sequence LIMIT 1""",
                (run_id,),
            ).fetchone()
            last_row = conn.execute(
                """SELECT candidate_sequence FROM backtest_result_candidate_audits
                   WHERE run_id=? ORDER BY candidate_sequence DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            if (
                first_row is None
                or last_row is None
                or int(first_row[0]) != 1
                or int(last_row[0]) != expected_count
            ):
                raise BacktestIntegrityError("candidate audit row count is invalid")
        try:
            summary = json.loads(str(manifest[2]))
            required = {
                "candidate_count",
                "priority_recorded",
                "priority_missing",
                "explanation_recorded",
                "explanation_missing",
                "preflight_rejected",
                "full_book_rejected",
                "competition_rejected",
                "filled",
                "fill_rejected",
            }
            if not isinstance(summary, dict) or set(summary) != required:
                raise ValueError("candidate audit summary has an invalid shape")
            if any(type(value) is not int or value < 0 for value in summary.values()):
                raise ValueError("candidate audit summary contains an invalid count")
            if summary["candidate_count"] != expected_count:
                raise ValueError("candidate audit summary count is inconsistent")
            if (
                summary["priority_recorded"] + summary["priority_missing"]
                != expected_count
                or summary["explanation_recorded"] + summary["explanation_missing"]
                != expected_count
            ):
                raise ValueError("candidate audit coverage counts are inconsistent")
            outcome_keys = {
                "preflight_rejected",
                "full_book_rejected",
                "competition_rejected",
                "filled",
                "fill_rejected",
            }
            if sum(summary[key] for key in outcome_keys) != expected_count:
                raise ValueError("candidate audit outcome counts are inconsistent")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BacktestIntegrityError("candidate audit summary is invalid") from exc
        canonical_summary = json.dumps(summary, sort_keys=True, separators=(",", ":"))
        if canonical_summary != str(manifest[2]):
            raise BacktestIntegrityError("candidate audit summary is not canonical")
        audit_digest = str(manifest[3])
        if len(audit_digest) != 64 or any(
            character not in "0123456789abcdef" for character in audit_digest
        ):
            raise BacktestIntegrityError("candidate audit manifest digest is invalid")
        return BacktestCandidateAuditSummaryV1(
            recorded=True,
            contract_version=_BACKTEST_CANDIDATE_AUDIT_CONTRACT,
            **summary,
            audit_digest=audit_digest,
        )

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
            TerminalSettlementEventV1,
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
            "terminal_settlement": TerminalSettlementEventV1,
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
                    if (
                        self._require_job_type(conn, str(row[0]))
                        is StrategyJobType.BACKTEST
                    ):
                        conn.execute(
                            "DELETE FROM backtest_staging WHERE run_id=?",
                            (str(row[0]),),
                        )
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
            conn.execute(
                "DELETE FROM backtest_staging WHERE run_id=?", (source_job_id,)
            )
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
        return self.compare_and_insert_detector_fragments(((key, canonical_json),))[key]

    def detector_fragment(
        self, key: DetectorCacheKey
    ) -> DetectorFragmentEnvelopeV1 | None:
        return self.detector_fragments((key,)).get(key)

    def detector_fragments(
        self, keys: Iterable[DetectorCacheKey]
    ) -> dict[DetectorCacheKey, DetectorFragmentEnvelopeV1]:
        """Return all validated immutable cache hits in one set-based query."""
        unique = tuple(dict.fromkeys(keys))
        if not unique:
            return {}
        with session(self._connect) as conn:
            self._populate_detector_cache_keys(conn, unique)
            rows = conn.execute(
                """SELECT c.security_id, c.date, c.detector, c.detector_version,
                          c.input_revision, c.scan_result_json, c.scan_result_digest
                   FROM scan_reconstruction_cache AS c
                   JOIN temp.detector_cache_keys AS k
                     ON (k.security_id, k.date, k.detector, k.detector_version,
                         k.input_revision) = (c.security_id, c.date, c.detector,
                         c.detector_version, c.input_revision)"""
            ).fetchall()
        by_values = {key.sql_values(): key for key in unique}
        return {
            key: self._validated_stored_fragment(key, str(row[5]), str(row[6]))
            for row in rows
            if (
                key := by_values[
                    (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
                ]
            )
        }

    def compare_and_insert_detector_fragments(
        self, items: Iterable[tuple[DetectorCacheKey, str | bytes]]
    ) -> dict[DetectorCacheKey, DetectorFragmentEnvelopeV1]:
        """Atomically insert or verify a batch of immutable detector fragments."""
        candidates: dict[DetectorCacheKey, tuple[str, str]] = {}
        for key, canonical_json in items:
            raw = (
                canonical_json.encode("utf-8")
                if isinstance(canonical_json, str)
                else bytes(canonical_json)
            )
            try:
                envelope = DetectorFragmentEnvelopeV1.from_canonical_json(raw)
            except HistoricalScanContractError as exc:
                raise BacktestIntegrityError(
                    "detector fragment is not canonical"
                ) from exc
            self._verify_fragment_key(key, envelope)
            candidate = (raw.decode("utf-8"), sha256(raw).hexdigest())
            prior = candidates.setdefault(key, candidate)
            if prior != candidate:
                raise BacktestIntegrityError(
                    "duplicate detector cache key has conflicting content"
                )
        if not candidates:
            return {}
        with session(self._connect) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                """INSERT OR IGNORE INTO scan_reconstruction_cache (
                       security_id, date, detector, detector_version, input_revision,
                       scan_result_json, scan_result_digest
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (*key.sql_values(), rendered, digest)
                    for key, (rendered, digest) in candidates.items()
                ],
            )
            self._populate_detector_cache_keys(conn, tuple(candidates))
            rows = conn.execute(
                """SELECT c.security_id, c.date, c.detector, c.detector_version,
                          c.input_revision, c.scan_result_json, c.scan_result_digest
                   FROM scan_reconstruction_cache AS c
                   JOIN temp.detector_cache_keys AS k
                     ON (k.security_id, k.date, k.detector, k.detector_version,
                         k.input_revision) = (c.security_id, c.date, c.detector,
                         c.detector_version, c.input_revision)"""
            ).fetchall()
            winners: dict[DetectorCacheKey, DetectorFragmentEnvelopeV1] = {}
            by_values = {key.sql_values(): key for key in candidates}
            for row in rows:
                key = by_values[
                    (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
                ]
                rendered, digest = candidates[key]
                stored = self._validated_stored_fragment(key, str(row[5]), str(row[6]))
                if str(row[5]) != rendered or str(row[6]) != digest:
                    raise BacktestIntegrityError(
                        "immutable detector cache key has conflicting content"
                    )
                winners[key] = stored
            if len(winners) != len(candidates):
                raise BacktestIntegrityError("detector cache write was not visible")
            return winners

    @staticmethod
    def _populate_detector_cache_keys(
        conn: sqlite3.Connection, keys: tuple[DetectorCacheKey, ...]
    ) -> None:
        conn.execute("DROP TABLE IF EXISTS temp.detector_cache_keys")
        conn.execute(
            """CREATE TEMP TABLE detector_cache_keys (
                   security_id TEXT, date TEXT, detector TEXT, detector_version TEXT,
                   input_revision TEXT,
                   PRIMARY KEY (security_id, date, detector, detector_version, input_revision)
               ) WITHOUT ROWID"""
        )
        conn.executemany(
            "INSERT INTO temp.detector_cache_keys VALUES (?, ?, ?, ?, ?)",
            (key.sql_values() for key in keys),
        )

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
            point_in_time = (
                profile.roster_policy_version == POINT_IN_TIME_POLICY_VERSION
            )
            expected = tuple(
                (item.security_id, item.mic)
                for item in roster.members
                if not point_in_time or is_current_source(item.source_memberships)
            )
            actual = tuple((item.security_id, item.mic) for item in capture.members)
            if not expected:
                return BauPromotionDecision(False, "BAU capture roster is empty")
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
                        # gh-641 shim: a capture sealed under the retired
                        # runtime-hashed scheme still names this identity.
                        runtime_manifests[item.detector_id].digest
                        if runtime_manifests[item.detector_id].accepts_stored_digest(
                            item.detector_version
                        )
                        else item.detector_version,
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
            or not manifests[detector.detector_id].accepts_stored_digest(
                detector.detector_version
            )
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
            or not yfinance_ingestion_source_manifest(
                _PROJECT_ROOT
            ).accepts_stored_digest(profile.yfinance_ingestion_version)
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
            if isinstance(member.exclusion_evidence, NoProviderDataProofV1):
                continue  # #82 C4: the proof records a failure, not evidence
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
                != provider_request_contract_version(commit.profile, evidence.provider)
            ):
                raise BacktestIntegrityError(
                    "snapshot provider evidence does not match member"
                )
            if member.resolution == "valid_scan":
                record = records[member.security_id]
                if (
                    evidence.alias_revision != record.provenance.alias_revision
                    or evidence.provider != record.provenance.price_provider
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
            conn,
            commit.profile,
            commit.manifest.snapshot_month,
            commit.members,
            provenance_quality=commit.manifest.provenance_quality,
        )

    @staticmethod
    def _validate_snapshot_members_against_roster(
        conn: sqlite3.Connection,
        profile: SnapshotProfileV1,
        snapshot_month: str,
        members: tuple[SnapshotMemberV1, ...],
        *,
        provenance_quality: str,
    ) -> None:
        if provenance_quality not in ("best_effort_reconstructed", "observed_bau"):
            raise BacktestIntegrityError("snapshot provenance quality is unknown")
        roster = conn.execute(
            """SELECT policy_version, captured_at FROM reconstruction_rosters
               WHERE roster_digest=?""",
            (profile.roster_digest,),
        ).fetchone()
        if roster is not None and str(roster[0]) != profile.roster_policy_version:
            raise BacktestIntegrityError(
                "snapshot profile and reconstruction roster policy differ"
            )
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
        providers: dict[str, str] = {}
        if profile.roster_policy_version == POINT_IN_TIME_POLICY_VERSION:
            # #82: a point-in-time month holds exactly that month's members.
            roster_members = _point_in_time_roster_members(conn, profile.roster_digest)
            calendar = TradingCalendar()
            sessions = {
                mic: calendar.last_session_of_month(mic, snapshot_month)
                for mic in {member.mic for member in roster_members}
            }
            if provenance_quality == "observed_bau" and (
                roster is None or snapshot_month < _utc_month(str(roster[1]))
            ):
                raise BacktestIntegrityError(
                    "observed BAU month precedes its roster capture"
                )
            # C3b: a BAU month observes the live screens, not the intervals.
            month = (
                [m for m in roster_members if is_current_source(m.source_memberships)]
                if provenance_quality == "observed_bau"
                else month_members(roster_members, sessions, point_in_time=True)
            )
            in_month = {member.security_id for member in month}
            providers = {
                member.security_id: member.provider or "yfinance"
                for member in roster_members
            }
            matches = (
                set(actual) <= set(expected)
                and actual == tuple(sorted(actual))
                and {item[0] for item in actual} == in_month
            )
            current = {
                m.security_id
                for m in roster_members
                if is_current_source(m.source_memberships)
            }
            for item in members:
                proof = item.exclusion_evidence
                if isinstance(proof, NoProviderDataProofV1) and (
                    item.security_id in current
                    or proof.provider != providers.get(item.security_id)
                ):
                    raise BacktestIntegrityError(
                        "no-provider-data exclusion requires a non-current member "
                        "of its provider"
                    )
        else:
            matches = actual == expected
        if not expected or not matches:
            raise BacktestIntegrityError(
                "snapshot members do not match the immutable reconstruction roster"
            )
        for member in members:
            alias = conn.execute(
                """SELECT 1 FROM security_alias_entries
                   WHERE alias_revision=? AND security_id=? AND provider=?
                     AND mic=? AND observed_symbol=?
                     AND (effective_from IS NULL OR effective_from<=?)
                     AND (effective_to IS NULL OR ?<effective_to)""",
                (
                    member.alias_revision,
                    member.security_id,
                    providers.get(member.security_id, "yfinance"),
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
            conn.execute("BEGIN")
            profile = self._load_snapshot_profile_on_connection(conn, profile_hash)
            self._validate_profile_authority(profile)
            revision = self._snapshot_coverage_revision(conn, profile_hash)
            persisted = self._load_persisted_snapshot_member_revisions(
                conn, profile_hash, snapshot_month, revision
            )
            if persisted is not None:
                return persisted
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
            revisions = tuple((str(row[0]), str(row[1])) for row in rows)
        self._publish_snapshot_member_revisions(
            profile_hash, snapshot_month, revisions, revision
        )
        return revisions

    def selected_member_revisions(
        self,
        profile_hash: str,
        snapshot_month: str,
        selected_security_ids: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        """Resolve selected roster members from verified profile snapshots.

        A selected universe may span securities whose first valid scan was
        committed after the run's start month.  Prefer the requested month,
        then use each security's latest committed ``valid_scan`` month.  The
        existing full-month validator remains the authority for every
        returned revision; this method only chooses among immutable rows.
        """
        selected = tuple(dict.fromkeys(selected_security_ids))
        if not selected:
            return ()
        preferred = dict(self.snapshot_member_revisions(profile_hash, snapshot_month))
        resolved = {
            security_id: preferred[security_id]
            for security_id in selected
            if security_id in preferred
        }
        missing = tuple(
            security_id for security_id in selected if security_id not in resolved
        )
        if not missing:
            return tuple(
                (security_id, resolved[security_id]) for security_id in selected
            )

        placeholders = ",".join("?" for _ in missing)
        with session(self._connect) as conn:
            rows = conn.execute(
                f"""SELECT security_id, MAX(snapshot_month)
                       FROM snapshot_members
                      WHERE profile_hash=? AND resolution='valid_scan'
                        AND security_id IN ({placeholders})
                      GROUP BY security_id""",
                (profile_hash, *missing),
            ).fetchall()
        for row in rows:
            security_id, month = str(row[0]), str(row[1])
            try:
                month_revisions = dict(
                    self.snapshot_member_revisions(profile_hash, month)
                )
            except BacktestIntegrityError:
                continue
            revision = month_revisions.get(security_id)
            if revision is not None:
                resolved[security_id] = revision
        return tuple(
            (security_id, resolved[security_id])
            for security_id in selected
            if security_id in resolved
        )

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
            self._validate_snapshot_members_against_roster(
                conn,
                profile,
                snapshot_month,
                member_tuple,
                provenance_quality=manifest.provenance_quality,
            )
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
                # Runtime authority can change independently of database writes.
                # Reject it before reading bulk evidence, including on cache hits.
                try:
                    profile = self._load_snapshot_profile_on_connection(
                        conn, selected_hash
                    )
                    self._validate_profile_authority(profile)
                    revision = self._snapshot_coverage_revision(conn, selected_hash)
                except Exception:
                    self._snapshot_coverage_cache.pop(selected_hash, None)
                    raise
                cached = self._snapshot_coverage_cache.get(selected_hash)
                if cached is not None and cached[0] == revision:
                    return cached[1]

                # Failed verification must not leave an older reusable summary.
                self._snapshot_coverage_cache.pop(selected_hash, None)
                persisted = self._load_persisted_snapshot_coverage(
                    conn, profile, revision
                )
                if persisted is not None:
                    self._cache_snapshot_coverage(selected_hash, revision, persisted)
                    return persisted
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
            # End the verification read transaction before acquiring a write
            # transaction. Never upgrade a stale WAL read snapshot to a writer.
            self._publish_snapshot_coverage(summary, revision)
            self._cache_snapshot_coverage(selected_hash, revision, summary)
            return summary

    # Bump when full coverage verification or projection semantics change.
    _coverage_verifier_version = 1
    _coverage_summary_max_bytes = 1_048_576
    _member_revision_verifier_version = 1
    _member_revision_summary_max_bytes = 1_048_576

    def _cache_snapshot_coverage(
        self, profile_hash: str, revision: str, summary: CoverageSummaryV1
    ) -> None:
        if (
            profile_hash not in self._snapshot_coverage_cache
            and len(self._snapshot_coverage_cache)
            >= self._snapshot_coverage_cache_limit
        ):
            self._snapshot_coverage_cache.pop(next(iter(self._snapshot_coverage_cache)))
        self._snapshot_coverage_cache[profile_hash] = (revision, summary)

    def _load_persisted_snapshot_coverage(
        self, conn: sqlite3.Connection, profile: SnapshotProfileV1, revision: str
    ) -> CoverageSummaryV1 | None:
        row = conn.execute(
            """SELECT summary_json, summary_digest
               FROM snapshot_coverage_summaries
               WHERE profile_hash=? AND source_revision=? AND verifier_version=?
                 AND typeof(summary_json)='text'
                 AND typeof(summary_digest)='text' AND length(summary_digest)=64
                 AND length(CAST(summary_json AS BLOB))<=?""",
            (
                profile.profile_hash,
                revision,
                self._coverage_verifier_version,
                self._coverage_summary_max_bytes,
            ),
        ).fetchone()
        if row is None:
            return None
        payload, digest = row
        if sha256(payload.encode("utf-8")).hexdigest() != digest:
            return None
        try:
            summary = CoverageSummaryV1.from_canonical_json(payload)
        except (ValueError, RecursionError):
            return None
        if (
            summary.profile_hash != profile.profile_hash
            or summary.display_version != profile.display_version
        ):
            return None
        return summary

    def _publish_snapshot_coverage(
        self, summary: CoverageSummaryV1, revision: str
    ) -> None:
        payload = summary.canonical_json()
        if len(payload.encode("utf-8")) > self._coverage_summary_max_bytes:
            return
        try:
            with session(self._connect) as conn:
                if conn.execute("PRAGMA query_only").fetchone()[0]:
                    # Read-only diagnostics may verify without publishing.
                    # Strict preparation still requires a durable projection.
                    return
                conn.execute("BEGIN IMMEDIATE")
                if (
                    self._snapshot_coverage_revision(conn, summary.profile_hash)
                    != revision
                ):
                    return
                conn.execute(
                    """INSERT INTO snapshot_coverage_summaries
                       (profile_hash, source_revision, verifier_version,
                        summary_json, summary_digest) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(profile_hash) DO UPDATE SET
                         source_revision=excluded.source_revision,
                         verifier_version=excluded.verifier_version,
                         summary_json=excluded.summary_json,
                         summary_digest=excluded.summary_digest""",
                    (
                        summary.profile_hash,
                        revision,
                        self._coverage_verifier_version,
                        payload,
                        sha256(payload.encode("utf-8")).hexdigest(),
                    ),
                )
        except sqlite3.OperationalError as exc:
            # Persistence is optional only for bounded lock contention.
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise

    def _load_persisted_snapshot_member_revisions(
        self,
        conn: sqlite3.Connection,
        profile_hash: str,
        snapshot_month: str,
        revision: str,
    ) -> tuple[tuple[str, str], ...] | None:
        row = conn.execute(
            """SELECT members_json, members_digest
               FROM snapshot_member_revision_summaries
               WHERE profile_hash=? AND snapshot_month=? AND source_revision=?
                 AND verifier_version=? AND typeof(members_json)='text'
                 AND typeof(members_digest)='text' AND length(members_digest)=64
                 AND length(CAST(members_json AS BLOB))<=?""",
            (
                profile_hash,
                snapshot_month,
                revision,
                self._member_revision_verifier_version,
                self._member_revision_summary_max_bytes,
            ),
        ).fetchone()
        if row is None:
            return None
        payload, digest = str(row[0]), str(row[1])
        if sha256(payload.encode("utf-8")).hexdigest() != digest:
            return None
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(decoded, list):
            return None
        revisions: list[tuple[str, str]] = []
        for item in decoded:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
            ):
                return None
            revisions.append((item[0], item[1]))
        if any(left[0] >= right[0] for left, right in zip(revisions, revisions[1:])):
            return None
        return tuple(revisions)

    def _publish_snapshot_member_revisions(
        self,
        profile_hash: str,
        snapshot_month: str,
        revisions: tuple[tuple[str, str], ...],
        revision: str,
    ) -> None:
        payload = json.dumps(revisions, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > self._member_revision_summary_max_bytes:
            return
        try:
            with session(self._connect) as conn:
                if conn.execute("PRAGMA query_only").fetchone()[0]:
                    return
                conn.execute("BEGIN IMMEDIATE")
                if self._snapshot_coverage_revision(conn, profile_hash) != revision:
                    return
                conn.execute(
                    """INSERT INTO snapshot_member_revision_summaries
                       (profile_hash, snapshot_month, source_revision,
                        verifier_version, members_json, members_digest)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(profile_hash, snapshot_month) DO UPDATE SET
                         source_revision=excluded.source_revision,
                         verifier_version=excluded.verifier_version,
                         members_json=excluded.members_json,
                         members_digest=excluded.members_digest""",
                    (
                        profile_hash,
                        snapshot_month,
                        revision,
                        self._member_revision_verifier_version,
                        payload,
                        sha256(payload.encode("utf-8")).hexdigest(),
                    ),
                )
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                raise

    def prepare_snapshot_member_revisions(
        self, profile_hash: str, snapshot_month: str
    ) -> tuple[tuple[str, str], ...]:
        """Verify one Result month and require its durable projection."""
        revisions = self.snapshot_member_revisions(profile_hash, snapshot_month)
        with session(self._connect) as conn:
            conn.execute("BEGIN")
            profile = self._load_snapshot_profile_on_connection(conn, profile_hash)
            self._validate_profile_authority(profile)
            revision = self._snapshot_coverage_revision(conn, profile_hash)
            if (
                self._load_persisted_snapshot_member_revisions(
                    conn, profile_hash, snapshot_month, revision
                )
                != revisions
            ):
                raise BacktestIntegrityError(
                    "snapshot member revision preparation did not persist current evidence; retry"
                )
        return revisions

    def prepare_snapshot_coverage(
        self, profile_hash: str | None = None
    ) -> CoverageSummaryV1:
        """Verify coverage and require a current durable projection for startup."""
        with self._snapshot_coverage_lock:
            # A previous best-effort publication may have encountered a lock.
            self._snapshot_coverage_cache.clear()
            summary = self.snapshot_coverage(profile_hash)
        with session(self._connect) as conn:
            conn.execute("BEGIN")
            profile = self._load_snapshot_profile_on_connection(
                conn, summary.profile_hash
            )
            self._validate_profile_authority(profile)
            revision = self._snapshot_coverage_revision(conn, summary.profile_hash)
            if (
                self._load_persisted_snapshot_coverage(conn, profile, revision)
                != summary
            ):
                raise BacktestIntegrityError(
                    "snapshot coverage preparation did not persist current evidence; retry"
                )
            months = tuple(
                str(row[0])
                for row in conn.execute(
                    """SELECT DISTINCT run.start_month
                       FROM backtest_results AS result
                       JOIN strategy_runs AS run ON run.id=result.run_id
                       JOIN strategy_jobs AS job ON job.id=run.id
                       WHERE run.profile_hash=? AND job.job_type='backtest'
                         AND job.status='complete' AND job.deleted_at IS NULL
                       ORDER BY run.start_month""",
                    (summary.profile_hash,),
                ).fetchall()
            )
        for month in months:
            self.prepare_snapshot_member_revisions(summary.profile_hash, month)
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
        """Read constant-size transactional generations from this read snapshot."""
        row = conn.execute(
            """SELECT state.epoch, state.generation, COALESCE(profile.generation, 0)
               FROM snapshot_coverage_revision_state AS state
               LEFT JOIN snapshot_coverage_profile_revisions AS profile
                 ON profile.profile_hash=?
               WHERE state.singleton_id=1""",
            (profile_hash,),
        ).fetchone()
        if row is None:
            raise BacktestIntegrityError("snapshot coverage revision state is missing")
        # DDL (including dropped integrity triggers) and database replacement
        # must not accidentally reuse an earlier process-local cache identity.
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        return repr((*row, schema_version))

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
            if not self._member_of_latest_month(
                conn, profile_hash, record, as_of_session
            ):
                return None
        return record

    @staticmethod
    def _member_of_latest_month(
        conn: sqlite3.Connection,
        profile_hash: str,
        record: HistoricalScanRecordV1,
        as_of_session: date,
    ) -> bool:
        """Whether ``record``'s security is in the latest month in effect (#82).

        The latest fully committed month in effect at ``as_of_session`` must
        hold a member row for the security, so a point-in-time leaver's scans
        stop being visible. Every month before the session's month is in
        effect; the session's own month only from its stored as-of session on
        the record's calendar. Every V1 month holds every roster member, so
        V1 visibility is unchanged.
        """
        session_month = as_of_session.strftime("%Y-%m")
        months = conn.execute(
            """SELECT snapshot_month FROM snapshot_months
               WHERE profile_hash=? AND snapshot_month>? AND snapshot_month<=?
                 AND processing_complete = 1 AND market_complete = 'unknown'
               ORDER BY snapshot_month DESC LIMIT 2""",
            (profile_hash, record.snapshot_month, session_month),
        ).fetchall()
        calendar = TradingCalendar.calendar_name(record.mic)
        mics = tuple(
            mic
            for mic in ("BATS", "XNAS", "XNYS", "XLON")
            if TradingCalendar.calendar_name(mic) == calendar
        )
        for (month,) in months:
            if month == session_month:
                (month_as_of,) = conn.execute(
                    f"""SELECT MAX(as_of_session_date) FROM snapshot_members
                        WHERE profile_hash=? AND snapshot_month=?
                          AND mic IN ({", ".join("?" for _ in mics)})""",
                    (profile_hash, month, *mics),
                ).fetchone()
                if month_as_of is None or str(month_as_of) > as_of_session.isoformat():
                    continue
            return (
                conn.execute(
                    """SELECT 1 FROM snapshot_members
                       WHERE profile_hash=? AND snapshot_month=? AND security_id=?""",
                    (profile_hash, month, record.security_id),
                ).fetchone()
                is not None
            )
        return True

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
