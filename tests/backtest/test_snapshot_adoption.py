"""gh-468 adoption invariants: Update-mode months equal Rebuild-mode months.

The core guarantee of incremental snapshot initialization is that adopting
an unchanged member's carried detector payloads and rewriting its provenance
under the new data version produces byte-identical records, members, and
month manifests to a from-scratch reconstruction of the same inputs. These
tests pin that invariant at the record, validation, and month-commit level.
"""

from __future__ import annotations

import json
from datetime import date

from test_historical_scan_reconstruction import (  # noqa: I001
    PROJECT_ROOT,
    ROSTER_CAPTURED_AT,
    _evidence,
    _request,
    _roster,
)
from test_snapshot_coverage_repository import NOW, _profile, _snapshot

from app.services.backtest.canonical_manifest import canonical_json
from app.services.backtest.historical_data_qualification import REQUEST_CONTRACT_VERSION
from app.services.backtest.historical_scan_reconstruction import (
    HistoricalScanReconstructor,
)
from app.services.backtest.reconstruction_roster import CapturedRosterV1
from app.services.backtest.snapshot_profile import (
    SnapshotProfileV1,
    adoption_gate_failures,
)


def _shifted_roster(*, extra_member: bool = False) -> CapturedRosterV1:
    """Rebuild the fixture roster with a different capture time/membership.

    Any content change gives the roster a new digest -- the adoption case --
    while member identity stays stable for the unchanged member.
    """
    body = json.loads(_roster().canonical_manifest_json)
    body["captured_at"] = ROSTER_CAPTURED_AT.replace(
        minute=ROSTER_CAPTURED_AT.minute + 1
    ).isoformat()
    if extra_member:
        member = dict(body["members"][0])
        member["security_id"] = "sec-002"
        member["provider_symbol"] = "TEST2"
        body["members"].append(member)
        body["expected_count"] = len(body["members"])
    rendered = canonical_json(body)
    from app.services.backtest.canonical_manifest import manifest_digest

    return CapturedRosterV1.from_json(manifest_digest(body), rendered)


def _revised_profile(roster_digest: str) -> SnapshotProfileV1:
    """The fixture profile rebound to a new roster digest (roster churn).

    The calendar digest and ingestion version are rebond to the real
    reconstruction inputs so the profile matches what a genuine from-scratch
    reconstruction would validate against.
    """
    from app.services.backtest.historical_scan_reconstruction import (
        canonical_calendar_digest,
    )
    from app.services.backtest.source_manifest import (
        yfinance_ingestion_source_manifest,
    )

    return _profile().model_copy(
        update={
            "roster_digest": roster_digest,
            "calendar_dataset_digest": canonical_calendar_digest(),
            "yfinance_ingestion_version": yfinance_ingestion_source_manifest(
                PROJECT_ROOT
            ).digest,
            "yfinance_request_contract_version": REQUEST_CONTRACT_VERSION,
        }
    )


def test_roster_churn_alone_is_the_adoption_case() -> None:
    """A roster-only difference raises no adoption gate; policy changes do."""
    previous = _profile()
    shifted = _profile().model_copy(
        update={"roster_digest": _shifted_roster().roster_digest}
    )
    assert adoption_gate_failures(previous, shifted) == ()

    ingestion_bump = shifted.model_copy(
        update={"yfinance_ingestion_version": "ingestion-v2"}
    )
    assert any(
        "ingestion" in reason
        for reason in adoption_gate_failures(previous, ingestion_bump)
    )

    detectors = tuple(
        item.model_copy(update={"detector_version": "d" * 64})
        for item in previous.detectors
    )
    detector_bump = shifted.model_copy(update={"detectors": detectors})
    assert any(
        "detector" in reason
        for reason in adoption_gate_failures(previous, detector_bump)
    )

    calendar_bump = shifted.model_copy(update={"calendar_dataset_digest": "e" * 64})
    assert any(
        "calendar" in reason
        for reason in adoption_gate_failures(previous, calendar_bump)
    )


def _rebased_request(evidence, roster: CapturedRosterV1):
    """The fixture request rebuilt against ``roster`` (new roster digest).

    Targets the canonical 2026-07 month-end the commit validator requires;
    the 272-session evidence covers it.
    """
    from dataclasses import replace

    from test_historical_scan_reconstruction import _request

    request = _request(
        evidence, as_of_session_date=date(2026, 7, 31), snapshot_month="2026-07"
    )
    manifest = request.input_manifest.model_copy(
        update={"roster_digest": roster.roster_digest}
    )
    return replace(request, roster=roster, input_manifest=manifest)


def test_adopted_record_is_byte_identical_to_fresh_reconstruction() -> None:
    """Carrying detector payloads and rewriting provenance under the new
    roster reproduces a from-scratch reconstruction byte-for-byte."""
    old_roster = _roster()
    new_roster = _shifted_roster()
    # 273 XNAS sessions from 2025-07-01 end exactly on the canonical
    # 2026-07-31 month-end the month-commit validator requires.
    evidence = _evidence(count=273)
    reconstructor = HistoricalScanReconstructor(None)

    predecessor = reconstructor.reconstruct(_rebased_request(evidence, old_roster))
    fresh_request = _rebased_request(evidence, new_roster)
    fresh = reconstructor.reconstruct(fresh_request)
    adopted = reconstructor.adopted_record(
        fresh_request,
        technicals=predecessor.record.technicals,
        stage=predecessor.record.stage,
        vcp=predecessor.record.vcp,
    )

    assert adopted == fresh.record
    assert adopted.canonical_json_bytes() == fresh.record.canonical_json_bytes()
    # The rewrite is real: the predecessor record itself differs.
    assert adopted != predecessor.record
    assert adopted.provenance.roster_digest == new_roster.roster_digest


def test_adopted_month_is_byte_identical_to_rebuild_month() -> None:
    """A month committed from adopted records equals one committed from
    fresh reconstructions under the same new profile: same manifest, same
    digests, same stored content."""
    old_roster = _roster()
    new_roster = _shifted_roster(extra_member=True)
    new_profile = _revised_profile(new_roster.roster_digest)
    # 273 XNAS sessions from 2025-07-01 end exactly on the canonical
    # 2026-07-31 month-end the month-commit validator requires.
    evidence = _evidence(count=273)
    reconstructor = HistoricalScanReconstructor(None)

    predecessor_record = reconstructor.reconstruct(
        _rebased_request(evidence, old_roster)
    ).record
    fresh_request = _rebased_request(evidence, new_roster)
    fresh_record = reconstructor.reconstruct(fresh_request).record
    adopted_record = reconstructor.adopted_record(
        fresh_request,
        technicals=predecessor_record.technicals,
        stage=predecessor_record.stage,
        vcp=predecessor_record.vcp,
    )

    adopted_month = _snapshot(
        new_profile, month=adopted_record.snapshot_month, record=adopted_record
    )
    rebuilt_month = _snapshot(
        new_profile, month=fresh_record.snapshot_month, record=fresh_record
    )
    assert adopted_month.manifest == rebuilt_month.manifest
    assert adopted_month.members == rebuilt_month.members
    assert adopted_month.records == rebuilt_month.records
    # The unmodified validator accepts the adopted write set: provenance
    # vs profile checks are satisfied by the rewrite alone.
    assert adopted_month.manifest.semantic_content_digest == (
        rebuilt_month.manifest.semantic_content_digest
    )


def test_predecessor_record_is_rejected_under_new_profile() -> None:
    """The reason adoption must rewrite: the predecessor record's provenance
    names the old roster, so it cannot commit under the new profile."""
    import pytest

    from app.services.backtest.snapshot_profile import SnapshotContractError

    old_roster = _roster()
    new_roster = _shifted_roster()
    new_profile = _revised_profile(new_roster.roster_digest)
    predecessor_record = (
        HistoricalScanReconstructor(None)
        .reconstruct(_request(_evidence(), roster=old_roster))
        .record
    )
    with pytest.raises(SnapshotContractError):
        _snapshot(new_profile, record=predecessor_record)
    assert date(2026, 7, 31) == date(2026, 7, 31)  # sanity: closed month fixture


# ---------------------------------------------------------------------------
# gh-468: predecessor resolution (activation history + walk-back + fallback)
# ---------------------------------------------------------------------------


def _adoption_repo(tmp_path):
    import sqlite3

    from test_snapshot_coverage_repository import _repo

    def _seed_profile(profile) -> None:
        """Insert a profile row without committing any months for it."""
        with sqlite3.connect(tmp_path / "backtest.db") as conn:
            conn.execute(
                """INSERT INTO snapshot_profiles
                   (profile_hash, canonical_profile_json, display_version,
                    roster_digest, scanner_schema_version,
                    calendar_dataset_version, calendar_dataset_digest, cadence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.profile_hash,
                    profile.canonical_json_bytes().decode(),
                    profile.display_version,
                    profile.roster_digest,
                    profile.record_schema_version,
                    profile.calendar_dataset_version,
                    profile.calendar_dataset_digest,
                    profile.cadence,
                ),
            )

    return NOW, _repo(tmp_path / "backtest.db"), _seed_profile


def test_previous_profile_uses_activation_history(tmp_path) -> None:
    from test_snapshot_coverage_repository import _commit, _profile, _snapshot


    NOW, repo, seed = _adoption_repo(tmp_path)
    first = _profile()
    _commit(repo, _snapshot(first))
    repo.activate_snapshot_profile(first.profile_hash, NOW)
    second = _profile(display="Scanner data v2")
    seed(second)
    repo.activate_snapshot_profile(second.profile_hash, NOW)

    previous = repo.previous_snapshot_profile(second.profile_hash)

    assert previous is not None
    assert previous.profile_hash == first.profile_hash


def test_previous_profile_walks_back_to_months_owning_predecessor(tmp_path) -> None:
    """A predecessor that initialized nothing is skipped, not fatal."""
    from test_snapshot_coverage_repository import _commit, _profile, _snapshot

    NOW, repo, seed = _adoption_repo(tmp_path)
    first = _profile()
    _commit(repo, _snapshot(first))
    repo.activate_snapshot_profile(first.profile_hash, NOW)
    middle = _profile(display="Scanner data v2")
    seed(middle)
    repo.activate_snapshot_profile(middle.profile_hash, NOW)
    latest = _profile(display="Scanner data v3")
    seed(latest)
    repo.activate_snapshot_profile(latest.profile_hash, NOW)

    previous = repo.previous_snapshot_profile(latest.profile_hash)

    assert previous is not None
    assert previous.profile_hash == first.profile_hash


def test_previous_profile_falls_back_without_activation_history(tmp_path) -> None:
    """Profiles activated before the history table existed resolve via the
    newest-committed-months heuristic (the live pre-gh-468 databases)."""
    import sqlite3

    from test_snapshot_coverage_repository import _commit, _profile, _snapshot

    NOW, repo, seed = _adoption_repo(tmp_path)
    first = _profile()
    _commit(repo, _snapshot(first))
    repo.activate_snapshot_profile(first.profile_hash, NOW)
    latest = _profile(display="Scanner data v9")
    seed(latest)
    repo.activate_snapshot_profile(latest.profile_hash, NOW)
    with sqlite3.connect(tmp_path / "backtest.db") as conn:
        conn.execute("DELETE FROM snapshot_profile_activation_history")

    previous = repo.previous_snapshot_profile(latest.profile_hash)

    assert previous is not None
    assert previous.profile_hash == first.profile_hash
