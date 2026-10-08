"""#82 (C3a): backtest runs over point-in-time universes with pinned exits.

One test per I/O matrix row of ``spec-gh-82c3a-pit-runs.md``, reusing the
in-memory fixtures of the engine, manifest and launch-service tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.repositories.backtest_repo import BacktestIntegrityError
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.repositories import db
from app.services.backtest.backtest_engine import (
    OpenPositionMarkEventV1,
    SecurityMarketDataV1,
    SkipReasonCode,
    SkippedSignalEventV1,
    TerminalSettlementEventV1,
    run_simulation,
)
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchValidationError,
    pinned_terminal_exits,
)
from app.services.backtest.point_in_time_membership import (
    POINT_IN_TIME_POLICY_VERSION,
)
from app.services.backtest.run_input_manifest import (
    TerminalExitV1,
    build_run_input_manifest_v2,
    read_run_input_manifest,
)
from app.services.backtest.run_universe import run_universe_digest
from app.services.backtest.strategy_job import RunUniverseSelectionV1
from app.services.backtest.strategy_protocol import Signal, SignalSide
from tests.backtest.test_backtest_engine import (
    DIGEST_A,
    _ScriptedStrategy,
    _build_security,
    _manifest as _engine_manifest,
    _market_view_factory,
    _sessions,
)
from tests.backtest.test_backtest_launch_service import (
    OTHER_REVISION,
    PRICE_REVISION,
    FakeBacktestRepo,
    FakeHistoricalPriceRepo,
    _command,
    _service,
)
from tests.backtest.test_run_input_manifest import _manifest, _securities

EVENT_DIGEST = "e" * 64
THIRD_REVISION = "3" * 64

# ---------------------------------------------------------------------------
# Engine: joiner, leaver with a pinned exit, evidence ending early
# ---------------------------------------------------------------------------

SESSIONS = _sessions("XNYS", date(2008, 5, 1), date(2008, 8, 1))
D0, D1 = SESSIONS[0], SESSIONS[1]
LEAVER_END = date(2008, 5, 30)  # last observation before the June exit
EARLY_END = date(2008, 6, 13)  # held, no exit, evidence just stops
JOINER_START = date(2008, 6, 2)


def _buy(security_id: str, session: date) -> Signal:
    return Signal(
        security_id=security_id, side=SignalSide.BUY, session=session, rule_id="b"
    )


def _sell(security_id: str, session: date) -> Signal:
    return Signal(
        security_id=security_id, side=SignalSide.SELL, session=session, rule_id="s"
    )


def _security(security_id: str, revision: str, sessions: tuple[date, ...], **kw):
    return _build_security(security_id, "XNYS", sessions, revision=revision, **kw)


def _pit_run(strategy: _ScriptedStrategy):
    leaver = _security("sec-a", DIGEST_A, tuple(s for s in SESSIONS if s <= LEAVER_END))
    early = _security(
        "sec-b",
        "b" * 64,
        tuple(s for s in SESSIONS if s <= EARLY_END),
        price_overrides={EARLY_END: (100.0, 77.0)},
    )
    joiner = _security(
        "sec-c", "c" * 64, tuple(s for s in SESSIONS if s >= JOINER_START)
    )
    exit_item = TerminalExitV1(
        security_id="sec-a",
        exit_session=date(2008, 6, 2),
        exit_type="acquisition",
        terminal_price_native=Decimal("45.5"),
        source_digest=EVENT_DIGEST,
    )
    return run_simulation(
        manifest=_engine_manifest(
            securities=(leaver[1], early[1], joiner[1]),
            start_month="2008-05",
            end_month="2008-07",
        ),
        strategy=strategy,
        market_view_factory=_market_view_factory(),
        security_market_data=(leaver[0], early[0], joiner[0]),
        terminal_exits=(exit_item,),
    )


def test_pit_run_settles_leaver_marks_early_end_and_trades_joiner() -> None:
    output = _pit_run(
        _ScriptedStrategy(
            entries={
                D0: [_buy("sec-a", D0), _buy("sec-b", D0)],
                date(2008, 6, 10): [_buy("sec-c", date(2008, 6, 10))],
            },
            default_size=10,
        )
    )

    (settlement,) = [
        e for e in output.events if isinstance(e, TerminalSettlementEventV1)
    ]
    assert (settlement.security_id, settlement.session) == ("sec-a", date(2008, 6, 2))
    assert settlement.settlement_price_native == Decimal("45.5")
    marks = {mark.security_id: mark for mark in output.final_open_positions}
    assert set(marks) == {"sec-b", "sec-c"}
    assert marks["sec-b"].mark_price_native == Decimal("77")
    assert all(isinstance(m, OpenPositionMarkEventV1) for m in marks.values())
    assert output.equity_curve[-1].session == SESSIONS[-1]


def test_fill_after_evidence_end_is_skipped_never_fatal() -> None:
    late = date(2008, 6, 20)
    output = _pit_run(
        _ScriptedStrategy(
            entries={D0: [_buy("sec-b", D0)], late: [_buy("sec-a", late)]},
            exits={late: [_sell("sec-b", late)]},
            size_by_rule={"s": -1},
            default_size=10,
        )
    )

    skipped = {
        e.security_id: e.reason
        for e in output.events
        if isinstance(e, SkippedSignalEventV1)
    }
    assert skipped["sec-b"] is SkipReasonCode.NO_PRICE_AFTER_EVIDENCE_END
    # sec-a exited on 2008-06-02: the exit outranks the missing price.
    assert skipped["sec-a"] is SkipReasonCode.SECURITY_EXITED
    assert [m.security_id for m in output.final_open_positions] == ["sec-b"]


def test_buy_signal_for_security_whose_evidence_ended_is_skipped() -> None:
    late = date(2008, 6, 20)
    output = _pit_run(
        _ScriptedStrategy(entries={late: [_buy("sec-b", late)]}, default_size=10)
    )

    (skip,) = [e for e in output.events if isinstance(e, SkippedSignalEventV1)]
    assert skip.reason is SkipReasonCode.NO_PRICE_AFTER_EVIDENCE_END
    assert output.final_open_positions == ()


# ---------------------------------------------------------------------------
# Run manifest: optional terminal_exits, V1/V2 digests unchanged
# ---------------------------------------------------------------------------

#: Digests of the default test manifests computed on ``main`` (d2e7100)
#: before ``terminal_exits`` existed -- an empty field must not move them.
V1_GOLDEN = "a19247f42a68c7791b3c8bb2e5605695c4105b2a2b7eee1eb367a5fccd6852db"
V2_GOLDEN = "0409a6239b7fc1246ebbaf4d8aa73345b52f0bc028e749781cebb51eb16e5cc3"


def _exit(security_id: str = "sec-000") -> TerminalExitV1:
    return TerminalExitV1(
        security_id=security_id,
        exit_session=date(2026, 6, 15),
        exit_type="acquisition",
        terminal_price_native=Decimal("45.5"),
        source_digest=EVENT_DIGEST,
    )


SELECTION = RunUniverseSelectionV1(
    profile_hash=DIGEST_A,
    activation_seq=1,
    universe_parameter="symbols",
    canonical_security_ids=("sec-000",),
    run_universe_digest=run_universe_digest(
        ["sec-000"], parameter="symbols", profile_hash=DIGEST_A
    ),
)


def test_empty_terminal_exits_keep_v1_and_v2_digests() -> None:
    v1 = _manifest(engine_version="backtest_engine.v9")
    v2 = build_run_input_manifest_v2(
        _manifest(
            engine_version="backtest_engine.v9", parameters={"symbols": ["sec-000"]}
        ),
        selection=SELECTION,
        source_preparation_job_id="prep",
    )

    assert "terminal_exits" not in json.loads(v1.canonical_json())
    assert v1.digest() == V1_GOLDEN
    assert v2.digest() == V2_GOLDEN


def test_manifest_with_exits_round_trips_to_the_same_digest() -> None:
    v1 = _manifest(terminal_exits=(_exit(),))
    v2 = build_run_input_manifest_v2(
        _manifest(parameters={"symbols": ["sec-000"]}, terminal_exits=(_exit(),)),
        selection=SELECTION,
        source_preparation_job_id="prep",
    )

    for manifest in (v1, v2):
        restored = read_run_input_manifest(manifest.canonical_json())
        assert type(restored) is type(manifest)
        assert restored.terminal_exits == (_exit(),)
        assert restored.digest() == manifest.digest()
    assert v1.digest() != _manifest().digest()


@pytest.mark.parametrize(
    "exits", [(_exit("sec-999"),), (_exit(), _exit())], ids=["unpinned", "repeated"]
)
def test_manifest_rejects_unpinned_or_repeated_exits(exits) -> None:
    with pytest.raises(ValueError, match="terminal_exits"):
        _manifest(securities=_securities(), terminal_exits=exits)


# ---------------------------------------------------------------------------
# Launch: V2 union universe and pinned exits; V1 unchanged
# ---------------------------------------------------------------------------


def _member(
    security_id: str,
    exit_date: str | None,
    *,
    price: str = (45.5).hex(),
    digest: str = EVENT_DIGEST,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "mic": "XNAS",
        "calendar": "XNAS",
        "provider_symbol": security_id.upper(),
        "currency": "USD",
        "quote_unit": "USD",
        "source_memberships": ["sp500_point_in_time"],
        "identity_evidence": [],
        "evidence_digest": DIGEST_A,
        "provider": "yfinance",
        "membership_intervals": [["2000-01-01", None]],
        "terminal_exit": None
        if exit_date is None
        else {
            "exit_date": exit_date,
            "event_type": "acquisition",
            "terminal_price": price,
            "event_digest": digest,
        },
    }


MONTHS: dict[str, tuple[tuple[str, str], ...]] = {
    "2026-02": (("sec-aapl", PRICE_REVISION), ("sec-ibm", THIRD_REVISION)),
    "2026-03": (("sec-ibm", THIRD_REVISION), ("sec-msft", OTHER_REVISION)),
}


@dataclass
class PointInTimeRepo(FakeBacktestRepo):
    """Feb holds the leaver ``sec-aapl``; Mar adds the joiner ``sec-msft``."""

    policy: str = POINT_IN_TIME_POLICY_VERSION
    months: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=lambda: dict(MONTHS)
    )
    roster_reads: list[str] = field(default_factory=list)
    leaver_exit: dict[str, str] = field(default_factory=dict)

    def snapshot_profile(self, profile_hash: str):
        return SimpleNamespace(
            roster_digest="b" * 64, roster_policy_version=self.policy
        )

    def snapshot_member_revisions(self, profile_hash: str, snapshot_month: str):
        if snapshot_month not in self.months:  # as the real repository does
            raise BacktestIntegrityError("snapshot month does not exist")
        return self.months[snapshot_month]

    def selected_member_revisions(self, profile_hash, snapshot_month, selected):
        found = dict(pair for month in self.months.values() for pair in month)
        return tuple((item, found[item]) for item in selected if item in found)

    def roster_manifest_json(self, roster_digest: str) -> str:
        self.roster_reads.append(roster_digest)
        members = [
            # inside the window: pinned
            _member("sec-aapl", "2026-03-02", **self.leaver_exit),
            _member("sec-ibm", "2026-01-15"),  # before start_month: not pinned
            _member("sec-msft", "2026-04-01"),  # after end_month: not pinned
        ]
        return json.dumps({"members": members})


def _price_repo() -> FakeHistoricalPriceRepo:
    revisions = {
        PRICE_REVISION: "sec-aapl",
        OTHER_REVISION: "sec-msft",
        THIRD_REVISION: "sec-ibm",
    }
    return FakeHistoricalPriceRepo(
        evidence={revision: "USD" for revision in revisions}, security_ids=revisions
    )


def _launched(repo: FakeBacktestRepo):
    service, jobs = _service(backtest_repo=repo, historical_price_repo=_price_repo())
    service.launch(_command(base_currency="USD"))
    (submission,) = jobs.submissions
    return read_run_input_manifest(submission.canonical_manifest_json)


def test_v2_launch_pins_union_of_joiners_and_leavers_with_window_exits() -> None:
    manifest = _launched(PointInTimeRepo())

    assert [(s.security_id, s.price_revision) for s in manifest.securities] == [
        ("sec-aapl", PRICE_REVISION),
        ("sec-ibm", THIRD_REVISION),
        ("sec-msft", OTHER_REVISION),
    ]
    assert manifest.terminal_exits == (
        TerminalExitV1(
            security_id="sec-aapl",
            exit_session=date(2026, 3, 2),
            exit_type="acquisition",
            terminal_price_native=Decimal("45.5"),
            source_digest=EVENT_DIGEST,
        ),
    )


def test_v1_launch_keeps_start_month_universe_and_reads_no_roster() -> None:
    repo = PointInTimeRepo(policy="ReconstructionRosterPolicyV1")

    manifest = _launched(repo)

    assert [s.security_id for s in manifest.securities] == ["sec-aapl", "sec-ibm"]
    assert manifest.terminal_exits == ()
    assert repo.roster_reads == []


@pytest.mark.parametrize(
    "leaver_exit",
    [{"price": float("nan").hex()}, {"price": float("inf").hex()}, {"digest": "zz"}],
    ids=["nan-price", "inf-price", "bad-digest"],
)
def test_v2_launch_rejects_a_malformed_pinned_exit(leaver_exit) -> None:
    service, jobs = _service(
        backtest_repo=PointInTimeRepo(leaver_exit=leaver_exit),
        historical_price_repo=_price_repo(),
    )

    with pytest.raises(BacktestLaunchValidationError, match="sec-aapl"):
        service.launch(_command(base_currency="USD"))
    assert jobs.submissions == []


def test_v2_launch_fails_when_a_run_month_has_no_snapshot() -> None:
    service, jobs = _service(
        backtest_repo=PointInTimeRepo(), historical_price_repo=_price_repo()
    )

    with pytest.raises(BacktestLaunchValidationError, match="does not exist"):
        service.launch(_command(base_currency="USD", end_month="2026-04"))
    assert jobs.submissions == []


def test_only_settling_exit_types_are_pinned() -> None:
    exit_item = SimpleNamespace(
        exit_date="2026-03-02",
        event_type="rename",
        terminal_price=None,
        event_digest=EVENT_DIGEST,
    )
    roster = SimpleNamespace(
        members=(SimpleNamespace(security_id="sec-aapl", terminal_exit=exit_item),)
    )

    assert pinned_terminal_exits(roster, ("sec-aapl",), "2026-02", "2026-03") == ()  # type: ignore[arg-type]


def test_real_repository_union_pins_a_joiner_valid_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.backtest import test_point_in_time_snapshots as pit

    # A joiner with enough history for a valid scan in its first month.
    joiner = ("wiki", [["2005-08-01", None]], pit.FIRST, date(2005, 9, 30))
    monkeypatch.setitem(pit.MEMBERS, "JOIN", joiner)
    repo, profile, ids, _outcomes, _path = pit._build(
        tmp_path, monkeypatch, ("2005-07", "2005-08")
    )
    service, _jobs = _service(
        backtest_repo=repo,  # type: ignore[arg-type]
        historical_price_repo=HistoricalPriceRepository(  # type: ignore[arg-type]
            db.make_connect(lambda: tmp_path / "prices.db")
        ),
    )

    union = service._union_member_ids(profile.profile_hash, "2005-07", "2005-08")
    evidence = service._resolve_roster_evidence(
        profile_hash=profile.profile_hash,
        snapshot_month="2005-07",
        base_currency="USD",
        start_month="2005-07",
        end_month="2005-08",
        selected_security_ids=union,
        pin_fx=False,
    )

    assert set(union) == {ids["JOIN"], ids["STAY"]}
    august = dict(repo.snapshot_member_revisions(profile.profile_hash, "2005-08"))
    assert {item.security_id: item.price_revision for item in evidence} == {
        security_id: august[security_id] for security_id in union
    }


@dataclass
class _EmptyAccess:
    """A run-owned price handle whose evidence holds no rows."""

    start: date
    end: date
    security_id: str = "sec-a"
    data_revision: str = DIGEST_A
    currency: str = "USD"
    quote_unit: str = "USD"
    exchange_timezone: str = "America/New_York"

    def as_traded_row_on_or_before(self, session: date) -> None:
        return None

    def as_traded_row(self, session: date) -> None:
        return None

    def actions_on(self, session: date) -> tuple[()]:
        return ()

    def close(self) -> None:
        return None


def test_engine_treats_evidence_without_rows_as_uncovered() -> None:
    sessions = _sessions("XNYS", date(2008, 5, 1), date(2008, 6, 1))
    _market_data, pinned = _security("sec-a", DIGEST_A, sessions)
    access = _EmptyAccess(start=date(2008, 4, 30), end=date(2008, 6, 1))

    output = run_simulation(
        manifest=_engine_manifest(
            securities=(pinned,), start_month="2008-05", end_month="2008-05"
        ),
        strategy=_ScriptedStrategy(entries={sessions[0]: [_buy("sec-a", sessions[0])]}),
        market_view_factory=_market_view_factory(),
        security_market_data=(
            SecurityMarketDataV1(security_id="sec-a", price_access=access),
        ),
    )

    (skip,) = [e for e in output.events if isinstance(e, SkippedSignalEventV1)]
    assert skip.reason is SkipReasonCode.NO_PRICE_AFTER_EVIDENCE_END
    assert output.final_open_positions == ()
