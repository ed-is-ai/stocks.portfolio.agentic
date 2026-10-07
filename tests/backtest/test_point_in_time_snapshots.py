"""Point-in-time monthly snapshots (#82 C2)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestIntegrityError, BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
)
from app.services.backtest import historical_initialization_engine as engine_module
from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
    InitializationMonthError,
)
from app.services.backtest.historical_price_evidence import (
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.point_in_time_membership import month_members
from app.services.backtest.reconstruction_roster import (
    CapturedRosterMemberV1,
    CapturedRosterV1,
    PointInTimeRosterPolicyV2,
    RosterSource,
)
from app.services.backtest.security_identity import SecurityAliasManifestV1
from app.services.backtest.snapshot_profile import (
    ProfileDetectorV1,
    SnapshotProfileV1,
    provider_request_contract_version,
)
from app.services.backtest.source_manifest import (
    detector_source_manifests,
    yfinance_ingestion_source_manifest,
)
from app.services.backtest.trading_calendar import TradingCalendar
from app.services.backtest.wiki_historical_evidence import (
    WikiHistoricalEvidenceAdapter,
)
from tests.backtest.test_point_in_time_roster import CURRENT, _payload, _service

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "historical_scan_record_v1.json"
NOW = datetime(2005, 10, 14, 12, tzinfo=timezone.utc)
FIRST = date(2004, 1, 2)
#: symbol -> (provider, membership intervals, first priced session, last).
MEMBERS: dict[str, tuple[str, list[list[str | None]], date, date]] = {
    "STAY": ("yfinance", [["2004-01-02", None]], FIRST, date(2005, 9, 30)),
    # Leaves mid-July: in June, not in July (as-of 2005-07-29) or August.
    "LEAV": ("wiki", [["2004-01-02", "2005-07-15"]], FIRST, date(2005, 7, 14)),
    # Joins in August with a short WIKI history: an exclusion proof.
    "JOIN": ("wiki", [["2005-08-01", None]], date(2005, 8, 1), date(2005, 9, 30)),
}
MONTHS = ("2005-06", "2005-07", "2005-08")
POLICY_V2 = "PointInTimeRosterPolicyV2"


def _member(
    security_id: str, intervals: tuple[tuple[str, str | None], ...]
) -> CapturedRosterMemberV1:
    return CapturedRosterMemberV1(
        security_id=security_id,
        mic="XNYS",
        calendar="XNYS",
        provider_symbol=security_id.upper(),
        currency="USD",
        quote_unit="USD",
        source_memberships=(),
        identity_evidence=(),
        evidence_digest="a" * 64,
        provider="yfinance",
        membership_intervals=intervals,
    )


JUNE_2005_END = {"XNYS": date(2005, 6, 30)}


# The spec's I/O matrix; each session is the month's XNYS as-of session.
@pytest.mark.parametrize(
    ("intervals", "sessions", "included"),
    [
        ((("1996-01-02", "2009-12-31"),), JUNE_2005_END, True),
        ((("2008-01-02", None),), {"XNYS": date(2007, 6, 29)}, False),
        ((("1996-01-02", "2009-06-15"),), {"XNYS": date(2009, 7, 31)}, False),
        ((("1996-01-02", "2009-06-15"),), {"XNYS": date(2009, 6, 30)}, False),
        ((("2005-06-15", None),), JUNE_2005_END, True),
        ((("1996-01-02", "2001-01-02"), ("2005-01-03", None)), JUNE_2005_END, True),
        ((("1996-01-02", "2005-06-30"),), JUNE_2005_END, False),
        ((), JUNE_2005_END, False),
    ],
    ids=[
        "member",
        "before-joining",
        "after-leaving",
        "leaves-mid-month-before-as-of",
        "joins-mid-month-before-as-of",
        "rejoin",
        "leaves-on-as-of",
        "screen-only",
    ],
)
def test_month_members_follow_membership_intervals(
    intervals: tuple[tuple[str, str | None], ...],
    sessions: dict[str, date],
    included: bool,
) -> None:
    member = _member("x", intervals)
    assert month_members((member,), sessions, point_in_time=True) == (
        [member] if included else []
    )


def test_v1_month_members_are_the_whole_roster_in_id_order() -> None:
    members = (_member("b", ()), _member("a", ()))
    assert month_members(members, JUNE_2005_END, point_in_time=False) == [
        members[1],
        members[0],
    ]


def test_month_members_reject_a_mic_without_a_session() -> None:
    member = _member("x", (("1996-01-02", None),))
    with pytest.raises(ValueError, match="XNYS"):
        month_members((member,), {"XLON": date(2005, 6, 30)}, point_in_time=True)


def _profile(roster_digest: str, policy: str) -> SnapshotProfileV1:
    manifests = detector_source_manifests(PROJECT_ROOT)
    return SnapshotProfileV1(
        schema_version="snapshot_profile.v1",
        display_version="Scanner data v2",
        record_schema_version="historical_scan_record.v1",
        detectors=tuple(
            ProfileDetectorV1(
                detector_id=detector.detector_id,
                detector_api_version=detector.detector_api_version,
                detector_version=manifests[detector.detector_id].digest,
            )
            for detector in DETECTOR_REGISTRY
        ),
        roster_policy_version=policy,  # type: ignore[arg-type]
        roster_digest=roster_digest,
        identity_registry_version="SecurityIdentityRegistryV1",
        alias_policy_version="SecurityAliasManifestV1",
        source_policy_version="FreeHistoricalSourcePolicyV1",
        calendar_policy_version="PerExchangeMonthEndV1",
        calendar_dataset_version="exchange-calendars-v1",
        calendar_dataset_digest=TradingCalendar().session_table_digest(),
        yfinance_request_contract_version=REQUEST_CONTRACT_VERSION,
        yfinance_ingestion_version=yfinance_ingestion_source_manifest(
            PROJECT_ROOT
        ).digest,
        market_plane_policy_version="HistoricalMarketPlanesV1",
        reconstructability_policy_version="reconstructability.v1",
        provenance_vocabulary=("best_effort_reconstructed", "observed_bau"),
        cadence="per-exchange month_end",
    )


def test_wiki_contract_is_admitted_only_by_point_in_time_profiles() -> None:
    v1 = _profile("a" * 64, "ReconstructionRosterPolicyV1")
    v2 = _profile("a" * 64, "PointInTimeRosterPolicyV2")
    assert provider_request_contract_version(v1, "wiki") is None
    assert provider_request_contract_version(v2, "wiki") == "WikiArchiveDailyV1"
    assert provider_request_contract_version(v2, "yfinance") == (
        REQUEST_CONTRACT_VERSION
    )
    assert provider_request_contract_version(v2, "other") is None


def _fixture_record(**provenance: object) -> HistoricalScanRecordV1:
    record = HistoricalScanRecordV1.from_canonical_json(
        FIXTURE.read_bytes().rstrip(b"\n")
    )
    payload = record.model_dump(mode="python")
    payload["provenance"] = {**payload["provenance"], **provenance}
    return HistoricalScanRecordV1.model_validate(payload)


POINT_IN_TIME = {
    "price_provider": "wiki",
    "universe_basis": "point_in_time_index_membership",
    "point_in_time_universe": True,
    "survivorship_bias": "reduced",
    "renamed_or_delisted_may_be_absent": True,
}


def test_reconstructability_policy_accepts_point_in_time_provenance() -> None:
    record = _fixture_record(**POINT_IN_TIME)
    assert record.provenance.survivorship_bias == "reduced"
    v1 = _fixture_record()
    assert v1.provenance.universe_basis == "captured_configured_roster"


@pytest.mark.parametrize(
    "override",
    [
        {"point_in_time_universe": False},
        {"survivorship_bias": "known"},
        {"universe_basis": "captured_configured_roster"},
        {"renamed_or_delisted_may_be_absent": False},
    ],
)
def test_reconstructability_policy_rejects_mixed_provenance(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _fixture_record(**{**POINT_IN_TIME, **override})


def test_roster_identity_trigger_is_replaced_in_place(tmp_path: Path) -> None:
    path = tmp_path / "backtest.db"
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    conn = sqlite3.connect(path)
    (current,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name="
        "'snapshot_member_requires_roster_identity'"
    ).fetchone()
    legacy = current.replace(
        "alias.provider IN ('yfinance', 'wiki')", "alias.provider = 'yfinance'"
    )
    assert legacy != current
    conn.execute("DROP TRIGGER snapshot_member_requires_roster_identity")
    conn.execute(legacy)
    conn.commit()

    repo.ensure_schema()

    (migrated,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name="
        "'snapshot_member_requires_roster_identity'"
    ).fetchone()
    conn.close()
    assert migrated == current


# ---------------------------------------------------------------------------
# A V2 roster built and committed end to end over three months.
# ---------------------------------------------------------------------------


def _sessions(first: date, last: date) -> tuple[date, ...]:
    return TradingCalendar().sessions_in_range(
        "XNYS", first, date.fromordinal(last.toordinal() + 1)
    )


class _Ticker:
    def __init__(self, symbol: str) -> None:
        _provider, _intervals, first, last = MEMBERS[symbol]
        sessions = _sessions(first, last)
        closes = [50.0 + index * 0.05 for index in range(len(sessions))]
        self._frame = pd.DataFrame(
            {
                "Open": closes,
                "High": [close + 1 for close in closes],
                "Low": [close - 1 for close in closes],
                "Close": closes,
                "Adj Close": closes,
                "Volume": [1_000.0] * len(closes),
                "Dividends": [0.0] * len(closes),
                "Stock Splits": [0.0] * len(closes),
            },
            index=pd.DatetimeIndex(
                [session.isoformat() for session in sessions], tz="America/New_York"
            ),
        )
        self._symbol = symbol

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, str]:
        return {
            "symbol": self._symbol,
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
        }


def _wiki_db(tmp_path: Path) -> Path:
    lines = [
        f"{symbol},{session.isoformat()},{50 + i * 0.05},{51 + i * 0.05},"
        f"{49 + i * 0.05},{50 + i * 0.05},1000.0,0.0,1.0,1"
        for symbol, (provider, _intervals, first, last) in MEMBERS.items()
        if provider == "wiki"
        for i, session in enumerate(_sessions(first, last))
    ]
    csv_path = tmp_path / "WIKI.csv"
    csv_path.write_text(
        "ticker,date,open,high,low,close,volume,ex-dividend,split_ratio,adj_close\n"
        + "\n".join(lines)
        + "\n"
    )
    path = tmp_path / "wiki_prices.db"
    wiki = WikiPriceRepository(db.make_connect(lambda: path))
    wiki.ensure_schema()
    wiki.import_csv(csv_path)
    return path


def _capture(repo: BacktestRepository) -> CapturedRosterV1:
    rows = [
        {
            "symbol": symbol,
            "provider_symbol": symbol,
            "provider": provider,
            "membership_intervals": intervals,
            "terminal_exit": None,
        }
        for symbol, (provider, intervals, _first, _last) in MEMBERS.items()
    ]
    point_in_time = _payload(RosterSource.SP500_POINT_IN_TIME, rows)
    fetchers = (*((lambda p=p: p) for p in CURRENT), lambda: point_in_time)
    return _service(repo, fetchers, PointInTimeRosterPolicyV2()).capture(
        "v2", SecurityAliasManifestV1.build((), created_at=NOW)
    )


def _processor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str = POLICY_V2
) -> tuple[CanonicalSnapshotMonthProcessor, BacktestRepository, CapturedRosterV1]:
    """Capture a V2 roster and wire an engine month processor over it."""
    monkeypatch.setattr(CanonicalSnapshotMonthProcessor, "_DETECTOR_WORKERS", 1)
    repo = BacktestRepository(
        db.make_connect(lambda: tmp_path / "backtest.db"),
        clock=lambda: NOW.date(),
    )
    repo.ensure_schema()
    commit = repo.commit_snapshot_month

    def unclaimed_commit(snapshot, verifier, **kwargs):  # noqa: ANN001, ANN202
        kwargs.pop("job_claim")  # no strategy job backs this fixture run
        return commit(snapshot, verifier, **kwargs)

    monkeypatch.setattr(repo, "commit_snapshot_month", unclaimed_commit)
    prices = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "prices.db"))
    prices.ensure_schema()
    roster = _capture(repo)
    profile = _profile(roster.roster_digest, policy)
    processor = CanonicalSnapshotMonthProcessor(
        job_id="job",
        claim_token="claim",
        profile=profile,
        roster=roster,
        backtest_repository=repo,
        price_repository=prices,
        evidence_adapter=YFinanceHistoricalEvidenceAdapter(_Ticker, clock=lambda: NOW),
        evidence_adapters={
            "wiki": WikiHistoricalEvidenceAdapter(_wiki_db(tmp_path), clock=lambda: NOW)
        },
        clock=lambda: NOW,
        project_root=PROJECT_ROOT,
    )
    return processor, repo, roster


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, months: tuple[str, ...]):
    processor, repo, roster = _processor(tmp_path, monkeypatch)
    outcomes = [processor(month) for month in months]
    ids = {member.provider_symbol: member.security_id for member in roster.members}
    profile = _profile(roster.roster_digest, POLICY_V2)
    return repo, profile, ids, outcomes, tmp_path / "backtest.db"


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build and commit ``MONTHS`` of a V2 roster through the engine."""
    return _build(tmp_path, monkeypatch, MONTHS)


def _visible(repo, profile, security_id: str, session: date) -> str | None:  # noqa: ANN001
    record = repo.latest_committed_scan_result(
        profile_hash=profile.profile_hash,
        security_id=security_id,
        as_of_session=session,
    )
    return None if record is None else record.snapshot_month


def test_v2_months_hold_exactly_their_point_in_time_members(built) -> None:
    repo, profile, ids, outcomes, _path = built
    names = {security_id: symbol for symbol, security_id in ids.items()}
    resolved = {}
    for month in MONTHS:
        write_set = repo.snapshot_month_write_set(profile.profile_hash, month)
        assert write_set is not None
        members, _records = write_set
        resolved[month] = {
            names[member.security_id]: member.resolution for member in members
        }

    assert resolved == {
        "2005-06": {"STAY": "valid_scan", "LEAV": "valid_scan"},
        "2005-07": {"STAY": "valid_scan"},
        "2005-08": {"STAY": "valid_scan", "JOIN": "legitimate_exclusion"},
    }
    assert [o.reused_securities + o.fetched_securities for o in outcomes] == [2, 1, 2]


def test_v2_records_carry_point_in_time_provenance(built) -> None:
    repo, profile, ids, _outcomes, _path = built
    write_set = repo.snapshot_month_write_set(profile.profile_hash, "2005-06")
    assert write_set is not None
    providers = {record.security_id: record.provenance for record in write_set[1]}
    wiki, yahoo = providers[ids["LEAV"]], providers[ids["STAY"]]
    assert (wiki.price_provider, wiki.provider_request_contract_version) == (
        "wiki",
        "WikiArchiveDailyV1",
    )
    assert yahoo.price_provider == "yfinance"
    for provenance in (wiki, yahoo):
        assert provenance.universe_basis == "point_in_time_index_membership"
        assert provenance.point_in_time_universe is True
        assert provenance.survivorship_bias == "reduced"
        assert provenance.renamed_or_delisted_may_be_absent is True
    august = repo.snapshot_month_write_set(profile.profile_hash, "2005-08")
    assert august is not None
    proof = {m.security_id: m for m in august[0]}[ids["JOIN"]].exclusion_evidence
    assert proof is not None
    assert (proof.provider, proof.request_contract_version) == (
        "wiki",
        "WikiArchiveDailyV1",
    )


def test_capture_registers_aliases_under_the_member_provider(built) -> None:
    _repo, _profile_, _ids, _outcomes, path = built
    conn = sqlite3.connect(path)
    rows = dict(
        conn.execute(
            "SELECT observed_symbol, provider FROM security_alias_entries"
        ).fetchall()
    )
    conn.close()
    assert rows["LEAV"] == rows["JOIN"] == "wiki"
    assert rows["STAY"] == rows["AAPL"] == "yfinance"


def test_leaver_scans_stop_being_visible(built) -> None:
    repo, profile, ids, _outcomes, _path = built

    def visible(symbol: str, session: date) -> str | None:
        return _visible(repo, profile, ids[symbol], session)

    # Inside July, before its month end, June is still the latest month.
    assert visible("LEAV", date(2005, 7, 15)) == "2005-06"
    assert visible("LEAV", date(2005, 7, 29)) is None
    # A partial current month (August not yet at its as-of) still hides him.
    assert visible("LEAV", date(2005, 8, 15)) is None
    assert visible("LEAV", date(2005, 8, 31)) is None
    assert visible("STAY", date(2005, 8, 15)) == "2005-07"
    assert visible("STAY", date(2005, 8, 31)) == "2005-08"
    # An excluded member stays a member: no valid scan, but no failure.
    assert visible("JOIN", date(2005, 8, 31)) is None


def test_uncommitted_later_month_does_not_hide_a_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, profile, ids, _outcomes, _path = _build(
        tmp_path, monkeypatch, ("2005-06", "2005-08")
    )
    leaver = ids["LEAV"]
    assert _visible(repo, profile, leaver, date(2005, 7, 29)) == "2005-06"
    assert _visible(repo, profile, leaver, date(2005, 8, 15)) == "2005-06"
    assert _visible(repo, profile, leaver, date(2005, 8, 31)) is None


def test_repo_rejects_a_month_missing_a_point_in_time_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processor, repo, roster = _processor(tmp_path, monkeypatch)
    leaver = {m.provider_symbol: m.security_id for m in roster.members}["LEAV"]

    def without_leaver(members, sessions, *, point_in_time):  # noqa: ANN001, ANN202
        return [
            member
            for member in month_members(members, sessions, point_in_time=point_in_time)
            if member.security_id != leaver
        ]

    monkeypatch.setattr(engine_module, "month_members", without_leaver)
    with pytest.raises(InitializationMonthError) as caught:
        processor("2005-06")

    assert isinstance(caught.value.__cause__, BacktestIntegrityError)
    assert "roster" in str(caught.value.__cause__)
    profile = _profile(roster.roster_digest, POLICY_V2)
    assert repo.snapshot_month_write_set(profile.profile_hash, "2005-06") is None


def test_roster_and_profile_policies_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="policy"):
        _processor(tmp_path, monkeypatch, "ReconstructionRosterPolicyV1")
    repo = BacktestRepository(db.make_connect(lambda: tmp_path / "backtest.db"))
    digest = repo.roster_digest_for_lineage("v2")
    assert digest is not None
    conn = sqlite3.connect(tmp_path / "backtest.db")
    try:
        with pytest.raises(BacktestIntegrityError, match="policy"):
            BacktestRepository._validate_snapshot_members_against_roster(
                conn, _profile(digest, "ReconstructionRosterPolicyV1"), "2005-06", ()
            )
    finally:
        conn.close()
