"""Point-in-time S&P 500 roster policy and source (#82 C1)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from app.repositories import db
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.backtest.point_in_time_roster import (
    PointInTimeRosterSourceAdapter,
    point_in_time_rows,
    spans_trade_after,
)
from app.services.backtest.reconstruction_roster import (
    CapturedRosterV1,
    MarketIdentityEvidence,
    PointInTimeRosterPolicyV2,
    ReconstructionRosterCaptureService,
    RosterCaptureError,
    RosterSource,
    RosterSourcePayloadV1,
    TerminalExitV1,
)
from app.services.backtest.security_identity import SecurityAliasManifestV1
from app.services.index_membership.sp500_import import INDEX_ID

NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
#: Roster digest of the V1 capture below on ``main`` before #82 C1.
V1_DIGEST = "1b3837ba8f34aca031aeab92f4398be9417c9e9e968f884a8da10f586659af3d"
UUIDS = (
    "7d16e313-2dd2-45a8-8a33-7b61b7df3fc8",
    "435d3ca4-cbbb-4da1-a486-292beb19125a",
    "5b3a7f0e-58a4-4e8e-9d55-0d8f4f2e6a11",
    "c0b8e7a2-1f3d-4b6e-8c9a-2d4e6f8a0b13",
    "9e1d2c3b-4a5f-4e6d-8c7b-1a2b3c4d5e6f",
    "0f9e8d7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f",
    "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
    "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f",
    "4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f8a",
)
WIKI_HEADER = (
    "ticker,date,open,high,low,close,volume,ex-dividend,split_ratio,"
    "adj_open,adj_high,adj_low,adj_close,adj_volume"
)
#: WIKI ticker -> last date with a close (each starts 1998-01-02).
WIKI_LAST = {
    "ENDS": "2009-06-16",
    "ACQ": "2010-02-26",
    "CAP": "2012-04-30",
    "YHOO": "2017-06-16",
    "AAL": "2005-01-03",
}
#: ticker -> [(start, end, event_type or None, terminal_price)]
HISTORY: dict[str, list[tuple[str, str | None, str | None, float | None]]] = {
    "AAPL": [("1982-11-30", None, None, None)],
    "ENDS": [("1996-01-02", "2009-06-17", "delisting", None)],
    "ACQ": [("2001-01-02", "2010-03-01", "acquisition", 45.5)],
    "CAP": [("2003-01-02", "2012-05-01", "still_trading", None)],
    "REJ": [
        ("2001-01-02", "2005-01-03", "still_trading", None),
        ("2008-01-02", None, None, None),
    ],
    "AAL": [
        ("1996-01-02", "1997-01-15", "acquisition", 20.0),
        ("2015-03-23", None, None, None),
    ],
    "LEHMQ": [("1996-01-02", "2008-10-01", "bankruptcy", None)],
    "AABA": [("1999-12-08", "2017-06-19", "acquisition", None)],
    "OLD": [("1990-01-02", "1995-06-01", "delisting", None)],
}


def _interval(ticker: str, start: str, end: str | None) -> MembershipInterval:
    return MembershipInterval(
        ticker=ticker, start_date=start, end_date=end, security_key=f"{ticker}@{start}"
    )


def _event(ticker: str, start: str, end: str, kind: str, price: float | None):
    return TerminalEvent(
        security_key=f"{ticker}@{start}",
        ticker=ticker,
        exit_date=end,
        event_type=kind,  # type: ignore[arg-type]
        terminal_price=price,
        evidence="wikipedia",
    )


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write tmp membership, WIKI and override files mirroring ``HISTORY``."""
    membership_db = tmp_path / "index_membership.db"
    repo = IndexMembershipRepository(db.make_connect(lambda: membership_db))
    repo.ensure_schema()
    spells = [(t, s, e, k, p) for t, rows in HISTORY.items() for s, e, k, p in rows]
    import_id, _ = repo.record_import(
        index_id=INDEX_ID,
        source="test",
        source_ref="test-ref@abc",
        source_digest="d" * 64,
        first_date="1982-01-01",
        last_date="2026-01-01",
        snapshot_count=1,
        low_confidence_before="1996-01-01",
        intervals=[_interval(t, s, e) for t, s, e, _k, _p in spells],
    )
    repo.replace_terminal_events(
        import_id,
        [_event(t, s, e, k, p) for t, s, e, k, p in spells if e and k],
    )
    wiki_db = tmp_path / "wiki_prices.db"
    csv_path = tmp_path / "WIKI.csv"
    rows = "".join(
        f"{t},{day},1,1,1,1,1,0,1,1,1,1,1,1\n"
        for t, last in WIKI_LAST.items()
        for day in ("1998-01-02", last)
    )
    csv_path.write_text(f"{WIKI_HEADER}\n{rows}")
    wiki = WikiPriceRepository(db.make_connect(lambda: wiki_db))
    wiki.ensure_schema()
    wiki.import_csv(csv_path)
    overrides = tmp_path / "overrides.csv"
    overrides.write_text(
        "sp_ticker,wiki_ticker,start_date,end_date\nAABA,YHOO,1999-12-08,2017-06-19\n"
    )
    return membership_db, wiki_db, overrides


def _adapter(paths: tuple[Path, Path, Path]) -> PointInTimeRosterSourceAdapter:
    return PointInTimeRosterSourceAdapter(*paths, clock=lambda: NOW)


def test_adapter_rows_cover_every_matrix_case(sources) -> None:
    payload = _adapter(sources)()
    rows = {row["symbol"]: cast(dict[str, Any], row) for row in payload.rows}

    assert payload.source is RosterSource.SP500_POINT_IN_TIME
    assert payload.source_version == "test-ref@abc"
    config = json.loads(payload.config_version)
    assert config["membership_source_digest"] == "d" * 64
    assert set(config) >= {"terminal_events_digest", "wiki_import_digest"}
    assert "OLD" not in rows  # ended before 2000
    assert rows["AAPL"]["provider"] == "yfinance"
    assert rows["AAPL"]["membership_intervals"] == [["1982-11-30", None]]
    assert rows["AAPL"]["terminal_exit"] is None
    ends = rows["ENDS"]
    assert (ends["provider"], ends["provider_symbol"]) == ("wiki", "ENDS")
    assert ends["terminal_exit"]["event_type"] == "delisting"
    assert ends["terminal_exit"]["terminal_price"] is None
    assert rows["ACQ"]["provider"] == "wiki"
    assert rows["ACQ"]["terminal_exit"]["terminal_price"] == 45.5
    assert (rows["CAP"]["provider"], rows["CAP"]["terminal_exit"]) == (
        "yfinance",
        None,
    )
    assert len(rows["REJ"]["membership_intervals"]) == 2
    assert rows["AAL"]["membership_intervals"] == [["2015-03-23", None]]
    assert rows["AAL"]["provider"] == "yfinance"
    lehm = rows["LEHMQ"]
    assert (lehm["provider"], lehm["provider_symbol"]) == ("yfinance", "LEHMQ")
    assert lehm["terminal_exit"]["event_type"] == "bankruptcy"
    assert (rows["AABA"]["provider"], rows["AABA"]["provider_symbol"]) == (
        "wiki",
        "YHOO",
    )


def test_exit_drops_earlier_intervals_of_a_reused_ticker() -> None:
    spells = [
        _interval("AAL", "1996-01-02", "2001-01-15"),
        _interval("AAL", "2015-03-23", None),
        _interval("BRK.B", "1996-01-02", None),
    ]
    events = {
        "AAL@1996-01-02": _event("AAL", "1996-01-02", "2001-01-15", "acquisition", 1.0)
    }

    rows, kept = point_in_time_rows(spells, events, {}, [])

    assert rows[0]["membership_intervals"] == [["2015-03-23", None]]
    assert rows[0]["terminal_exit"] is None
    assert rows[1]["provider_symbol"] == "BRK-B"
    assert kept == []


def test_exit_traded_through_keeps_both_intervals() -> None:
    spells = [
        _interval("PCG", "1996-01-02", "2019-01-18"),
        _interval("PCG", "2022-10-03", None),
    ]
    events = {
        "PCG@1996-01-02": _event("PCG", "1996-01-02", "2019-01-18", "bankruptcy", None)
    }
    yf_spans = {"PCG": ("1970-01-02", "2026-09-30")}

    rows, kept = point_in_time_rows(spells, events, {}, [], yf_spans)

    assert rows[0]["membership_intervals"] == [
        ["1996-01-02", "2019-01-18"],
        ["2022-10-03", None],
    ]
    assert kept == [("PCG", "2019-01-18")]


def test_spans_trade_after_needs_a_series_spanning_the_exit() -> None:
    trades_after = spans_trade_after(
        (
            {
                "SNDK": ("1995-11-01", "2016-05-12"),
                "BRK_B": ("1996-05-09", "2018-03-27"),
            },
            {
                "SNDK": ("2025-02-24", "2026-09-30"),
                "LATE": ("2010-01-14", "2026-09-30"),
            },
        )
    )
    assert not trades_after("SNDK", "2016-05-12")  # ends at exit, returns 2025
    assert not trades_after("LATE", "2010-01-04")  # starts 10 days after
    assert trades_after("BRK.B", "2010-01-04")  # WIKI spelling found
    assert not trades_after("NONE", "2010-01-04")


def test_terminal_exit_digest_pins_the_stored_event() -> None:
    event = _event("ACQ", "2001-01-02", "2010-03-01", "acquisition", 45.5)
    spells = [_interval("ACQ", "2001-01-02", "2010-03-01")]
    first, _ = point_in_time_rows(spells, {event.security_key: event}, {}, [])
    moved = event.model_copy(update={"terminal_price": 46.0})
    second, _ = point_in_time_rows(spells, {event.security_key: moved}, {}, [])
    assert (
        first[0]["terminal_exit"]["event_digest"]
        != (second[0]["terminal_exit"]["event_digest"])
    )


def test_missing_membership_db_is_provider_unavailable(sources, tmp_path) -> None:
    _membership, wiki_db, overrides = sources
    adapter = PointInTimeRosterSourceAdapter(
        tmp_path / "absent.db", wiki_db, overrides, clock=lambda: NOW
    )
    with pytest.raises(RosterCaptureError) as caught:
        adapter()
    assert caught.value.code == "provider_unavailable"
    assert not (tmp_path / "absent.db").exists()


def _payload(
    source: RosterSource, rows: Sequence[Mapping[str, object]]
) -> RosterSourcePayloadV1:
    return RosterSourcePayloadV1.build(
        source=source,
        rows=rows,
        retrieved_at=NOW,
        source_version=f"{source.value}-v1",
        package_version="test",
        config_version="ReconstructionRosterPolicyV1",
    )


CURRENT = (
    _payload(RosterSource.DATAHUB_SP500, [{"symbol": "AAPL", "name": "Apple"}]),
    _payload(
        RosterSource.TRADINGVIEW_US,
        [{"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"}],
    ),
    _payload(
        RosterSource.TRADINGVIEW_UK,
        [{"symbol": "LSE:ULVR", "exchange": "LSE", "currency": "GBp"}],
    ),
)


def _service(
    repo: BacktestRepository,
    fetchers: Sequence[Callable[[], RosterSourcePayloadV1]],
    policy: PointInTimeRosterPolicyV2 | None = None,
) -> ReconstructionRosterCaptureService:
    ids = iter(UUIDS)
    return ReconstructionRosterCaptureService(
        repo,
        fetchers,
        lambda _s, _r: MarketIdentityEvidence(
            "XNAS", "USD", "USD", "yfinance_metadata", "i" * 64
        ),
        id_generator=lambda: next(ids),
        clock=lambda: NOW,
        policy=policy,
    )


def _repo(path: Path) -> BacktestRepository:
    repo = BacktestRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    return repo


def _v1_capture(repo: BacktestRepository) -> CapturedRosterV1:
    fetchers = tuple((lambda p=p: p) for p in CURRENT)
    aliases = SecurityAliasManifestV1.build((), created_at=NOW)
    return _service(repo, fetchers).capture("v1", aliases)


def test_v1_capture_digest_is_unchanged_from_main(tmp_path) -> None:
    captured = _v1_capture(_repo(tmp_path / "backtest.db"))
    assert captured.roster_digest == V1_DIGEST
    member = json.loads(captured.canonical_manifest_json)["members"][0]
    assert "provider" not in member
    parsed = CapturedRosterV1.from_json(
        captured.roster_digest, captured.canonical_manifest_json
    )
    assert [
        (m.provider, m.membership_intervals, m.terminal_exit) for m in parsed.members
    ] == [("", (), None)] * 2


def _v2_capture(path: Path, sources) -> CapturedRosterV1:
    fetchers = (*((lambda p=p: p) for p in CURRENT), _adapter(sources))
    aliases = SecurityAliasManifestV1.build((), created_at=NOW)
    return _service(_repo(path), fetchers, PointInTimeRosterPolicyV2()).capture(
        "v2", aliases
    )


def test_v2_capture_merges_point_in_time_members_end_to_end(sources, tmp_path) -> None:
    captured = _v2_capture(tmp_path / "a.db", sources)
    again = _v2_capture(tmp_path / "b.db", sources)
    members = {m.provider_symbol: m for m in captured.members}

    assert captured.roster_digest == again.roster_digest
    aapl = members["AAPL"]
    assert aapl.mic == "XNAS"
    assert aapl.source_memberships == (
        "datahub_sp500",
        "tradingview_us",
        "sp500_point_in_time",
    )
    assert aapl.membership_intervals == (("1982-11-30", None),)
    assert (aapl.provider, aapl.terminal_exit) == ("yfinance", None)
    assert members["ULVR.L"].provider == "yfinance"
    ends = members["ENDS"]
    assert (ends.mic, ends.calendar, ends.provider) == ("XNYS", "XNYS", "wiki")
    assert ends.identity_evidence[0].evidence_source.endswith("mic_assumed")
    assert isinstance(members["YHOO"].terminal_exit, TerminalExitV1)
    assert members["ACQ"].terminal_exit is not None
    assert members["ACQ"].terminal_exit.terminal_price == 45.5
    assert members["LEHMQ"].terminal_exit is not None
    assert "AAL" in members and "OLD" not in members

    parsed = CapturedRosterV1.from_json(
        captured.roster_digest, captured.canonical_manifest_json
    )
    assert parsed.members == captured.members
    manifest = json.loads(captured.canonical_manifest_json)
    assert manifest["policy_version"] == "PointInTimeRosterPolicyV2"
    with sqlite3.connect(tmp_path / "a.db") as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT source_name FROM reconstruction_roster_sources "
                "ORDER BY source_order"
            )
        ]
    assert names[-1] == "sp500_point_in_time"


def test_v2_capture_commits_nothing_without_membership_db(tmp_path, sources) -> None:
    _membership, wiki_db, overrides = sources
    missing = PointInTimeRosterSourceAdapter(
        tmp_path / "absent.db", wiki_db, overrides, clock=lambda: NOW
    )
    repo = _repo(tmp_path / "backtest.db")
    fetchers = (*((lambda p=p: p) for p in CURRENT), missing)
    service = _service(repo, fetchers, PointInTimeRosterPolicyV2())
    with pytest.raises(RosterCaptureError, match="unavailable"):
        service.capture("v2", SecurityAliasManifestV1.build((), created_at=NOW))
    assert repo.roster_digest_for_lineage("v2") is None


def test_schema_upgrade_admits_point_in_time_source_rows(tmp_path) -> None:
    path = tmp_path / "backtest.db"
    _v1_capture(_repo(path))
    with sqlite3.connect(path) as conn:
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='reconstruction_roster_sources'"
        ).fetchone()
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='reconstruction_roster_sources'",
            (sql.replace(", 'sp500_point_in_time'", ""),),
        )
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM reconstruction_roster_sources").fetchall()

    _repo(path)

    with sqlite3.connect(path) as conn:
        after = conn.execute("SELECT * FROM reconstruction_roster_sources").fetchall()
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='reconstruction_roster_sources'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM reconstruction_roster_sources")
    assert len(before) == 3 and after == before
    assert "'sp500_point_in_time'" in sql


def test_since_filter_is_injectable(sources) -> None:
    membership, wiki_db, overrides = sources
    payload = PointInTimeRosterSourceAdapter(
        membership, wiki_db, overrides, since=date(1990, 1, 1), clock=lambda: NOW
    )()
    assert "OLD" in {row["symbol"] for row in payload.rows}


def test_config_records_price_inputs_and_missing_price_db_fails(
    sources, tmp_path
) -> None:
    from tests.test_coverage_report import _PRICE_SCHEMA, _add_revision, _span

    config = json.loads(_adapter(sources)().config_version)
    assert (config["price_db_used"], config["kept_by_continuity"]) == (False, [])

    price_db = tmp_path / "prices.db"
    with sqlite3.connect(price_db) as conn:
        conn.executescript(_PRICE_SCHEMA)
        _add_revision(conn, "AAL", "aal", _span("1990-01-02", "2026-07-31"))
    payload = PointInTimeRosterSourceAdapter(
        *sources, price_db=price_db, since=date(1990, 1, 1), clock=lambda: NOW
    )()
    config = json.loads(payload.config_version)
    assert config["price_db_used"] is True
    assert config["kept_by_continuity"] == [["AAL", "1997-01-15"]]

    missing = PointInTimeRosterSourceAdapter(
        *sources, price_db=tmp_path / "absent.db", clock=lambda: NOW
    )
    with pytest.raises(RosterCaptureError) as caught:
        missing()
    assert caught.value.code == "provider_unavailable"


def test_wiki_provider_needs_a_series_reaching_the_exit() -> None:
    spells = [
        _interval("OLDW", "2001-01-02", "2010-03-01"),
        _interval("REN", "2001-01-02", "2010-03-01"),
    ]
    events = {
        "OLDW@2001-01-02": _event("OLDW", "2001-01-02", "2010-03-01", "unknown", None),
        "REN@2001-01-02": _event("REN", "2001-01-02", "2010-03-01", "rename", None),
    }
    wiki = {"OLDW": ("1998-01-02", "2010-02-15"), "REN": ("1998-01-02", "2010-02-19")}

    rows, _ = point_in_time_rows(spells, events, wiki, [])

    assert rows[0]["provider"] == "yfinance"  # WIKI stops 14 days early
    assert rows[1]["provider"] == "wiki"  # rename falls back to WIKI


def test_prices_ending_at_the_exit_pin_a_synthetic_delisting() -> None:
    spells = [
        _interval("GONE", "2001-01-02", "2010-03-01"),
        _interval("LEFT", "2001-01-02", "2010-03-01"),
        _interval("YFG", "2001-01-02", "2015-03-02"),
    ]
    events = {
        f"{t}@2001-01-02": _event(t, "2001-01-02", end, kind, None)
        for t, end, kind in (
            ("GONE", "2010-03-01", "unknown"),
            ("LEFT", "2010-03-01", "still_trading"),
            ("YFG", "2015-03-02", "unknown"),
        )
    }
    wiki = {"GONE": ("1998-01-02", "2010-02-26")}
    yf = {"LEFT": ("1990-01-02", "2026-07-31"), "YFG": ("1990-01-02", "2015-02-27")}

    rows, _ = point_in_time_rows(spells, events, wiki, [], yf)
    by_symbol = {row["symbol"]: row for row in rows}

    gone = by_symbol["GONE"]["terminal_exit"]
    assert (gone["exit_date"], gone["event_type"], gone["terminal_price"]) == (
        "2010-03-01",
        "delisting",
        None,
    )
    assert by_symbol["LEFT"]["terminal_exit"] is None  # still trades
    assert by_symbol["YFG"]["provider"] == "yfinance"
    assert by_symbol["YFG"]["terminal_exit"]["event_type"] == "delisting"


def _pit_row(symbol: str, end: str | None, exit_: bool = False) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "provider": "yfinance",
        "provider_symbol": symbol,
        "membership_intervals": [["2001-01-02", end]],
        "terminal_exit": {
            "exit_date": end,
            "event_type": "acquisition",
            "terminal_price": None,
            "event_digest": "e" * 64,
        }
        if exit_
        else None,
    }


def _resolver(_s: str, _r: dict[str, object]) -> MarketIdentityEvidence:
    return MarketIdentityEvidence("XNAS", "USD", "USD", "test", "i" * 64)


def test_former_member_does_not_join_a_non_sp500_current_member() -> None:
    current = (
        CURRENT[0],
        _payload(
            RosterSource.TRADINGVIEW_US,
            [
                {"symbol": "NASDAQ:AAPL", "exchange": "NASDAQ", "currency": "USD"},
                {"symbol": "NYSE:TWX", "exchange": "NYSE", "currency": "USD"},
                {"symbol": "NYSE:CAP", "exchange": "NYSE", "currency": "USD"},
            ],
        ),
        CURRENT[2],
    )
    pit = _payload(
        RosterSource.SP500_POINT_IN_TIME,
        [
            _pit_row("TWX", "2018-06-15", exit_=True),
            _pit_row("CAP", "2012-05-01"),  # left for market cap: same company
            _pit_row("AAPL", None),  # later in canonical row order: skipped
            _pit_row("AAPL", "2010-01-04"),
        ],
    )

    members, skipped = PointInTimeRosterPolicyV2().normalize_with_skips(
        (*current, pit), _resolver
    )

    cap = next(m for m in members if m.provider_symbol == "CAP")
    assert cap.source_memberships == ("tradingview_us", "sp500_point_in_time")
    twx = next(m for m in members if m.provider_symbol == "TWX")
    assert twx.source_memberships == ("tradingview_us",)
    aapl = next(m for m in members if m.provider_symbol == "AAPL")
    assert aapl.membership_intervals == (("2001-01-02", "2010-01-04"),)
    assert [(s["provider_symbol"], s["reason"]) for s in skipped] == [
        ("TWX", "symbol_held_by_non_sp500_current_member"),
        ("AAPL", "duplicate_provider_symbol"),
    ]


def test_v2_manifest_records_skipped_rows(sources, tmp_path) -> None:
    manifest = json.loads(
        _v2_capture(tmp_path / "a.db", sources).canonical_manifest_json
    )
    assert manifest["skipped_point_in_time_rows"] == []


def test_assumed_mic_member_reuses_the_one_stored_us_identity(
    sources, tmp_path
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    ends = _v2_capture(path, sources).members
    stored = next(m for m in ends if m.provider_symbol == "ENDS")
    nasdaq_id = "8f0e1d2c-3b4a-4f5e-9d8c-7b6a5f4e3d2c"
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER security_identity_immutable_update")
        conn.execute(
            "UPDATE security_identities SET mic='XNAS', security_id=? "
            "WHERE provider_symbol='ENDS'",
            (nasdaq_id,),
        )
    fetchers = (*((lambda p=p: p) for p in CURRENT), _adapter(sources))
    captured = _service(repo, fetchers, PointInTimeRosterPolicyV2()).capture(
        "again", SecurityAliasManifestV1.build((), created_at=NOW)
    )
    again = next(m for m in captured.members if m.provider_symbol == "ENDS")
    assert stored.mic == "XNYS"
    assert (again.security_id, again.mic) == (nasdaq_id, "XNAS")


def test_capture_rejects_other_policy_lineage_and_fetcher_count(
    sources, tmp_path
) -> None:
    path = tmp_path / "backtest.db"
    repo = _repo(path)
    _v1_capture(repo)
    fetchers = (*((lambda p=p: p) for p in CURRENT), _adapter(sources))
    service = _service(repo, fetchers, PointInTimeRosterPolicyV2())
    with pytest.raises(RosterCaptureError) as caught:
        service.capture("v1", SecurityAliasManifestV1.build((), created_at=NOW))
    assert caught.value.code == "integrity_error"
    with pytest.raises(ValueError, match="per policy source"):
        _service(repo, fetchers[:3], PointInTimeRosterPolicyV2())


GOOD_EXIT = {
    "exit_date": "2010-03-01",
    "event_type": "acquisition",
    "terminal_price": None,
    "event_digest": "e" * 64,
}


@pytest.mark.parametrize(
    ("intervals", "exit_"),
    [
        ("2001-01-02", None),
        ([["2001-01-02"]], None),
        ([["2001-13-02", None]], None),
        ([["2010-01-02", "2001-01-02"]], None),
        ([["2001-01-02", None], ["2005-01-02", None]], None),
        ([["2001-01-02", "2010-03-01"]], "exit"),
        ([["2001-01-02", "2010-03-01"]], {"exit_date": "2010-03-01"}),
        ([["2001-01-02", "2010-03-01"]], {**GOOD_EXIT, "terminal_price": "x"}),
        ([["2001-01-02", "2010-03-01"]], {**GOOD_EXIT, "event_type": "rename"}),
    ],
)
def test_malformed_point_in_time_fields_are_integrity_errors(
    intervals: object, exit_: object
) -> None:
    member = {
        "security_id": "s",
        "mic": "XNYS",
        "calendar": "XNYS",
        "provider_symbol": "X",
        "currency": "USD",
        "quote_unit": "USD",
        "source_memberships": ["sp500_point_in_time"],
        "identity_evidence": [],
        "evidence_digest": "d",
        "provider": "yfinance",
        "membership_intervals": intervals,
        "terminal_exit": exit_,
    }
    with pytest.raises(RosterCaptureError) as caught:
        CapturedRosterV1.from_json("r", json.dumps({"members": [member]}))
    assert caught.value.code == "integrity_error"
    row = {**_pit_row("X", None), "membership_intervals": intervals}
    row["terminal_exit"] = exit_
    pit = _payload(RosterSource.SP500_POINT_IN_TIME, [row])
    with pytest.raises(RosterCaptureError) as caught:
        PointInTimeRosterPolicyV2().normalize((*CURRENT, pit), _resolver)
    assert caught.value.code == "integrity_error"


def test_unrecognised_roster_source_schema_fails_migration(tmp_path) -> None:
    path = tmp_path / "backtest.db"
    _repo(path)
    with sqlite3.connect(path) as conn:
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='reconstruction_roster_sources'"
        ).fetchone()
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='reconstruction_roster_sources'",
            (sql.replace("'sp500_point_in_time'", "'other_source'"),),
        )
    with pytest.raises(sqlite3.DatabaseError, match="unrecognised"):
        _repo(path)
