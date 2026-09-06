"""Resolve BAU authority and capture provider-native evidence before analysis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Literal, cast

import pandas as pd

from app.repositories.backtest_repo import BacktestRepository
from app.services.backtest.bau_run_envelope import (
    BauCaptureMemberV1,
    BauRawEvidenceV1,
    BauSnapshotCaptureV1,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.reconstruction_roster import CapturedRosterV1
from app.services.backtest.snapshot_profile import FULL_HISTORY_START
from app.services.backtest.source_manifest import (
    DetectorInputIdentityV1,
    ReconstructionInputManifestV1,
    detector_source_manifests,
    record_composition_source_manifest,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar


class BauCaptureUnavailable(RuntimeError):
    """Capture authority or exact run-owned evidence could not be established."""


class BauCaptureSession:
    """Run-scoped scanner fetch session over one resolved immutable roster."""

    def __init__(
        self,
        *,
        run_id: str,
        snapshot_month: str,
        profile,
        roster: CapturedRosterV1,
        roster_captured_at: datetime,
        alias_revision: str,
        sessions: dict[str, date],
        adapter: YFinanceHistoricalEvidenceAdapter,
        clock,
        project_root: Path,
        backtest_repository: BacktestRepository,
    ) -> None:
        self._run_id = run_id
        self._snapshot_month = snapshot_month
        self._profile = profile
        self._roster = roster
        self._roster_captured_at = roster_captured_at
        self._alias_revision = alias_revision
        self._sessions = sessions
        self._adapter = adapter
        self._clock = clock
        self._project_root = project_root
        self._backtest = backtest_repository
        self._captured: tuple[BauCaptureMemberV1, ...] | None = None
        self._frames: dict[str, pd.DataFrame] = {}
        self._consumed: set[str] = set()
        self._consumed_lock = Lock()

    def preload(self) -> None:
        """Fetch the complete roster before the scanner converts any ticker."""
        ordered = tuple(sorted(self._roster.members, key=lambda item: item.security_id))
        try:
            # This is an eligible scan's market-data boundary, not a background
            # initializer. Bound concurrency prevents a large profile roster from
            # serially delaying the ordinary scanner for hundreds of requests.
            with ThreadPoolExecutor(max_workers=min(8, len(ordered))) as pool:
                fetched = tuple(pool.map(self._capture_member, ordered))
        except Exception as exc:
            self._backtest.fail_bau_run_authority(
                run_id=self._run_id,
                completed_at=self._clock().astimezone(timezone.utc),
                reason="BAU roster evidence capture failed",
            )
            raise BauCaptureUnavailable("complete BAU roster capture failed") from exc
        # Publish to the session only after every member succeeded. A partial
        # provider response can therefore never leak into capture or scanning.
        self._captured = tuple(item[0] for item in fetched)
        self._frames = {item[1]: item[2] for item in fetched}

    def _capture_member(self, member) -> tuple[BauCaptureMemberV1, str, pd.DataFrame]:
        calendar = TradingCalendar()
        session = self._sessions[member.mic]
        end = _next_month(self._snapshot_month)
        request = HistoricalEvidenceRequest(
            security_id=member.security_id,
            alias_revision=self._alias_revision,
            symbol=member.provider_symbol,
            start=FULL_HISTORY_START,
            end=end,
            expected_currency=member.currency,
            expected_quote_unit=member.quote_unit,
            expected_timezone=BauCaptureCoordinator._TIMEZONES[member.mic],
            expected_sessions=calendar.sessions_in_range(
                member.mic, FULL_HISTORY_START, end
            ),
            allowed_observed_symbols=(member.provider_symbol,),
            allow_missing_prefix=True,
        )
        raw = BauRawEvidenceV1.from_historical_payload(self._adapter.fetch(request))
        manifest = _input_manifest(
            self._profile,
            member.security_id,
            self._alias_revision,
            self._snapshot_month,
            session,
            raw,
            self._project_root,
        )
        capture = BauCaptureMemberV1(
            security_id=member.security_id,
            mic=cast(Literal["BATS", "XNAS", "XNYS", "XLON"], member.mic),
            canonical_session=session,
            source_cutoff=session,
            alias_revision=self._alias_revision,
            input_manifest=manifest,
            raw_evidence=raw,
        )
        return capture, member.provider_symbol, _scanner_frame(raw)

    def roster_tickers(self) -> tuple[str, ...]:
        """Return the successfully captured roster to add to this scanner run."""
        if self._captured is None:
            return ()
        return tuple(self._frames)

    def frame_for(self, ticker: str) -> pd.DataFrame | None:
        """Return the exact capture response projected for live technicals."""
        frame = self._frames.get(ticker)
        if frame is not None:
            with self._consumed_lock:
                self._consumed.add(ticker)
        return frame

    def complete_capture(self) -> BauSnapshotCaptureV1 | None:
        if self._captured is None:
            return None
        expected = set(self._frames)
        with self._consumed_lock:
            consumed = set(self._consumed)
        if consumed != expected:
            raise BauCaptureUnavailable(
                "complete BAU roster did not participate in the scanner run"
            )
        return BauSnapshotCaptureV1(
            source_run_id=self._run_id,
            snapshot_month=self._snapshot_month,
            profile=self._profile,
            roster_digest=self._roster.roster_digest,
            roster_captured_at=self._roster_captured_at,
            captured_at=self._clock().astimezone(timezone.utc),
            members=self._captured,
        )


class BauCaptureCoordinator:
    """The scanner-run capture boundary; ordinary scanning is never blocked."""

    _TIMEZONES = {
        "XNAS": "America/New_York",
        "XNYS": "America/New_York",
        "XLON": "Europe/London",
    }

    def __init__(
        self,
        *,
        backtest_repository: BacktestRepository,
        envelope_directory: Path,
        adapter: YFinanceHistoricalEvidenceAdapter | None = None,
        clock=lambda: datetime.now(timezone.utc),
        project_root: Path | None = None,
    ) -> None:
        self._backtest = backtest_repository
        del envelope_directory  # The SQLite attempt journal is the capture gate.
        self._adapter = adapter or YFinanceHistoricalEvidenceAdapter()
        self._clock = clock
        self._project_root = project_root or Path(__file__).resolve().parents[3]

    def prepare_for_run(self, run_id: str) -> BauCaptureSession | None:
        """Resolve capture authority without performing any provider fetch."""
        now = self._clock().astimezone(timezone.utc)
        profile_ref = self._backtest.active_snapshot_profile()
        if profile_ref is None:
            raise BauCaptureUnavailable("no active snapshot profile")
        profile = self._backtest.snapshot_profile(profile_ref.profile_hash)
        if profile is None:
            raise BauCaptureUnavailable("active snapshot profile is unavailable")
        try:
            self._backtest.validate_bau_profile_authority(profile)
        except Exception as exc:
            raise BauCaptureUnavailable(
                "active snapshot profile is incompatible"
            ) from exc
        month = _previous_month(now.date())
        if self._backtest.snapshot_month(profile.profile_hash, month) is not None:
            return None
        roster_json = self._backtest.roster_manifest_json(profile.roster_digest)
        if roster_json is None:
            raise BauCaptureUnavailable("active reconstruction roster is unavailable")
        roster = CapturedRosterV1.from_json(profile.roster_digest, roster_json)
        try:
            roster_captured_at = datetime.fromisoformat(
                str(json.loads(roster.canonical_manifest_json)["captured_at"])
            ).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BauCaptureUnavailable("roster capture authority is invalid") from exc
        calendar = TradingCalendar()
        sessions = calendar.month_sessions(
            tuple(member.mic for member in roster.members), month, as_of=now.date()
        )
        first_eligible = _first_eligible_capture_date(calendar, sessions)
        if now.date() != first_eligible:
            return None
        if any(
            now <= calendar.session_close(mic, session).to_pydatetime()
            for mic, session in sessions.items()
        ):
            return None
        try:
            alias_revision = str(
                json.loads(roster.canonical_manifest_json)["alias_revision"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BauCaptureUnavailable("roster alias authority is invalid") from exc
        if not self._backtest.claim_bau_capture_attempt(
            run_id=run_id,
            profile_hash=profile.profile_hash,
            snapshot_month=month,
            attempted_at=now,
        ):
            return None
        return BauCaptureSession(
            run_id=run_id,
            snapshot_month=month,
            profile=profile,
            roster=roster,
            roster_captured_at=roster_captured_at,
            alias_revision=alias_revision,
            sessions=sessions,
            adapter=self._adapter,
            clock=self._clock,
            project_root=self._project_root,
            backtest_repository=self._backtest,
        )


def _input_manifest(
    profile, security_id, alias_revision, month, session, raw, project_root: Path
) -> ReconstructionInputManifestV1:
    manifests = detector_source_manifests(project_root)
    return ReconstructionInputManifestV1(
        schema_version="reconstruction_input_manifest.v1",
        security_id=security_id,
        snapshot_month=month,
        as_of_session_date=session,
        provider_data_revision=raw.data_revision,
        evidence_start=raw.start,
        evidence_end=raw.end,
        provider_request_contract_version=raw.request_contract_version,
        provider_evidence_manifest_digest=raw.data_revision,
        market_plane_policy_version=PRICE_VOLUME_PLANE_VERSION,
        alias_revision=alias_revision,
        roster_digest=profile.roster_digest,
        calendar_dataset_version=profile.calendar_dataset_version,
        calendar_dataset_digest=profile.calendar_dataset_digest,
        yfinance_ingestion_version=yfinance_ingestion_source_manifest(
            project_root
        ).digest,
        record_schema_version="historical_scan_record.v1",
        reconstructability_policy_version="reconstructability.v1",
        record_composition_version=record_composition_source_manifest(
            project_root
        ).digest,
        detectors=tuple(
            DetectorInputIdentityV1(
                detector_id=item.detector_id,
                detector_api_version=item.detector_api_version,
                detector_version=manifests[item.detector_id].digest,
                configuration=dict(item.configuration),
            )
            for item in DETECTOR_REGISTRY
        ),
    )


def _scanner_frame(raw: BauRawEvidenceV1) -> pd.DataFrame:
    """Project the exact provider-native response like yfinance auto-adjust."""
    rows: list[dict[str, float]] = []
    index: list[pd.Timestamp] = []
    for row in raw.rows:
        close = float.fromhex(str(row["close"]))
        adjusted = float.fromhex(str(row["adj_close"]))
        factor = adjusted / close
        rows.append(
            {
                "open": float.fromhex(str(row["open"])) * factor,
                "high": float.fromhex(str(row["high"])) * factor,
                "low": float.fromhex(str(row["low"])) * factor,
                "close": adjusted,
                "volume": float.fromhex(str(row["volume"])),
            }
        )
        index.append(pd.Timestamp(str(row["session"])))
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index))
    return frame.dropna(subset=["open", "high", "low", "close"])


def _previous_month(value: date) -> str:
    return (
        value.replace(day=1).fromordinal(value.replace(day=1).toordinal() - 1)
    ).strftime("%Y-%m")


def _next_month(month: str) -> date:
    year, value = (int(part) for part in month.split("-"))
    return date(year + (value == 12), value % 12 + 1, 1)


def _first_eligible_capture_date(
    calendar: TradingCalendar, sessions: dict[str, date]
) -> date:
    return max(
        tuple(
            stamp.date()
            for stamp in calendar._calendar(mic).sessions_window(session, 2)
        )[1]
        for mic, session in sessions.items()
    )


__all__ = ["BauCaptureCoordinator", "BauCaptureSession", "BauCaptureUnavailable"]
