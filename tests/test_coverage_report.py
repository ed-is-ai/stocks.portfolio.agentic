"""Per-month price coverage of the point-in-time S&P 500 (#74)."""

import csv
import json
import sqlite3
import zlib
from datetime import date
from pathlib import Path

import pytest

from app.cli import coverage_report as cli
from app.repositories import db
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
)
from app.services.index_membership import coverage

# Minimal columns of the real price-cache tables (historical_price_repo.py).
_PRICE_SCHEMA = """
CREATE TABLE historical_price_revisions (data_revision TEXT PRIMARY KEY,
    provider TEXT, requested_symbol TEXT, start_date TEXT, end_date TEXT,
    first_acquired_at TEXT);
CREATE TABLE historical_price_v2_revisions (revision_id INTEGER PRIMARY KEY,
    data_revision TEXT UNIQUE);
CREATE TABLE historical_price_v2_revision_chunks (revision_id INTEGER,
    chunk_kind TEXT, chunk_year INTEGER, chunk_digest TEXT);
CREATE TABLE historical_price_v2_chunks (chunk_digest TEXT PRIMARY KEY,
    compressed_payload BLOB);
"""

# ticker -> (start, end) intervals; AAL is reused, everything else has one.
# LATE is still a member (not cached before its history); LEHMQ and ENDS left.
INTERVALS = {
    "AAPL": [("1996-01-02", None)],
    "AAL": [("1996-01-02", "1997-01-15"), ("2000-01-03", None)],
    "LATE": [("1996-01-02", None)],
    "LEHMQ": [("1996-01-02", "2008-10-01")],
    "ENDS": [("1996-01-02", "2010-01-04")],
    "BRK.B": [("1996-01-02", None)],
}

Rows = dict[int, list[tuple[str, float | None]]]


def _add_revision(
    conn: sqlite3.Connection,
    symbol: str,
    revision: str,
    rows: Rows,
    end_date: str = "2026-08-01",
    provider: str = "yfinance",
    start_date: str = "1970-01-01",
) -> None:
    conn.execute(
        "INSERT INTO historical_price_revisions VALUES (?, ?, ?, ?, ?, ?)",
        (revision, provider, symbol, start_date, end_date, "2026-08-01T00:00:00"),
    )
    revision_id = conn.execute(
        "INSERT INTO historical_price_v2_revisions (data_revision) VALUES (?)",
        (revision,),
    ).lastrowid
    for year, items in rows.items():
        payload = {
            "kind": "rows",
            "year": year,
            "items": [
                {"session": s, "close": None if c is None else c.hex()}
                for s, c in items
            ],
        }
        digest = f"{revision}-{year}"
        conn.execute(
            "INSERT INTO historical_price_v2_chunks VALUES (?, ?)",
            (digest, zlib.compress(json.dumps(payload).encode())),
        )
        conn.execute(
            "INSERT INTO historical_price_v2_revision_chunks VALUES (?, 'rows', ?, ?)",
            (revision_id, year, digest),
        )


def _span(first: str, last: str) -> Rows:
    return {int(first[:4]): [(first, 1.0)], int(last[:4]): [(last, 2.0)]}


@pytest.fixture
def price_db(tmp_path: Path) -> Path:
    path = tmp_path / "prices.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRICE_SCHEMA)
    # Older AAPL revision (shorter span) is neither earliest nor latest.
    _add_revision(
        conn,
        "AAPL",
        "aapl-old",
        _span("2010-01-04", "2012-12-31"),
        "2013-01-01",
        "2010-01-01",
    )
    # GOOGL: the latest revision is a short window; an older one starts 1970.
    _add_revision(
        conn, "GOOGL", "g-long", _span("2004-08-19", "2026-06-30"), "2026-07-01"
    )
    _add_revision(
        conn,
        "GOOGL",
        "g-short",
        _span("2024-03-04", "2026-07-31"),
        start_date="2024-03-04",
    )
    # Latest AAPL: first chunk has no close, last chunk is all null: move inward.
    _add_revision(
        conn,
        "AAPL",
        "aapl-new",
        {
            1979: [("1979-12-31", None)],
            1980: [("1980-12-15", 1.0), ("1980-12-12", 1.0)],
            2026: [("2026-07-30", 1.0), ("2026-07-31", 2.0), ("2026-08-03", None)],
            2027: [("2027-01-04", None)],
        },
    )
    _add_revision(conn, "AAL", "aal", _span("1990-01-02", "2026-07-31"))
    _add_revision(conn, "LATE", "late", _span("2005-09-27", "2026-07-31"))
    _add_revision(conn, "ENDS", "ends", _span("1990-01-02", "2008-06-30"))
    _add_revision(conn, "BRK-B", "brk", _span("1996-05-09", "2026-07-31"))
    _add_revision(
        conn, "LEHMQ", "boe", _span("1990-01-02", "2026-07-31"), provider="other"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def membership_db(tmp_path: Path) -> Path:
    path = tmp_path / "m.db"
    repo = IndexMembershipRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    intervals = [
        MembershipInterval(ticker=t, start_date=s, end_date=e, security_key=f"{t}@{s}")
        for t, spells in INTERVALS.items()
        for s, e in spells
    ]
    repo.record_import(
        index_id="sp500",
        source="test",
        source_ref="sha",
        source_digest="d",
        first_date="1996-01-02",
        last_date="2026-07-31",
        snapshot_count=3,
        low_confidence_before="2001-01-16",
        intervals=intervals,
    )
    return path


def _report(membership_db: Path, price_db: Path, start: str, end: str):
    repo = IndexMembershipRepository(lambda: coverage.read_only(membership_db))
    return coverage.coverage_report(repo, coverage.price_spans(price_db), start, end)


def test_price_spans_edge_revisions_and_inward_chunks(price_db: Path) -> None:
    spans = coverage.price_spans(price_db)
    assert spans["AAPL"] == ("1980-12-12", "2026-07-31")
    assert spans["GOOGL"] == ("2004-08-19", "2026-07-31")
    assert spans["BRK-B"] == ("1996-05-09", "2026-07-31")
    assert "LEHMQ" not in spans  # not a yfinance revision


def test_month_as_of_is_last_calendar_day() -> None:
    assert coverage.month_as_of("2000-02") == "2000-02-29"
    assert coverage.month_as_of("2008-09") == "2008-09-30"
    assert coverage.months_between("1999-11", "2000-02") == [
        "1999-11",
        "1999-12",
        "2000-01",
        "2000-02",
    ]


def test_io_matrix(membership_db: Path, price_db: Path) -> None:
    rows = {r.month: r for r in _report(membership_db, price_db, "2000-01", "2026-10")}
    for row in rows.values():
        assert row.priced + row.suspect + row.not_cached + row.missing == row.members
    jan = rows["2000-01"]
    assert jan.as_of == "2000-01-31"
    assert jan.suspect_tickers == ["AAL"]  # reused ticker with spanning history
    assert jan.missing_tickers == ["LEHMQ"]  # left the index, no data
    assert jan.not_cached_tickers == ["LATE"]  # current member, history too short
    assert jan.priced == 3  # AAPL (long history), ENDS, BRK.B via BRK-B
    assert (jan.confidence, jan.stale) == ("low", False)
    assert rows["2001-03"].missing_tickers == ["LEHMQ"]
    assert rows["2001-03"].confidence == "normal"
    assert set(rows["2008-09"].missing_tickers) == {"ENDS", "LEHMQ"}
    assert set(rows["2008-06"].missing_tickers) == {"LEHMQ"}
    assert rows["2026-10"].stale


def test_cli_table_and_yearly_totals(
    membership_db: Path, price_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(_args(membership_db, price_db, "--from", "2000-01", "--to", "2001-12"))
    out = capsys.readouterr().out
    assert "2000-01 as_of=2000-01-31 members=6 priced=3 (50.0%)" in out
    assert "suspect=1 not_cached=1 missing=1 low" in out
    assert "2000 members=72 priced=36 (50.0%) suspect=12 not_cached=12" in out
    assert "2001 members=72" in out
    assert cli.LIMIT_NOTE in out


def test_cli_month_detail_and_csv(
    membership_db: Path,
    price_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_csv = tmp_path / "out.csv"
    cli.main(_args(membership_db, price_db, "--month", "2008-09", "--csv", out_csv))
    out = capsys.readouterr().out
    assert "missing: ENDS LEHMQ" in out
    assert "not cached: \n" in out
    assert "suspect: AAL" in out
    with out_csv.open() as handle:
        records = list(csv.DictReader(handle))
    assert len(records) == 1
    assert records[0]["month"] == "2008-09"
    assert records[0]["missing_tickers"] == "ENDS LEHMQ"


def test_cli_rejects_bad_month(membership_db: Path, price_db: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(_args(membership_db, price_db, "--month", "2008-9"))
    assert exc.value.code == 2


@pytest.mark.parametrize("which", ["membership", "price"])
def test_cli_missing_db_is_not_created(
    membership_db: Path, price_db: Path, tmp_path: Path, which: str
) -> None:
    absent = tmp_path / "absent.db"
    paths = (absent, price_db) if which == "membership" else (membership_db, absent)
    with pytest.raises(SystemExit, match="absent.db"):
        cli.main(_args(*paths))
    assert not absent.exists()


def test_cli_exits_without_membership_import(tmp_path: Path, price_db: Path) -> None:
    empty = tmp_path / "empty.db"
    IndexMembershipRepository(db.make_connect(lambda: empty)).ensure_schema()
    with pytest.raises(SystemExit, match="no sp500 membership"):
        cli.main(_args(empty, price_db))


def test_cli_does_not_modify_databases(membership_db: Path, price_db: Path) -> None:
    before = [p.stat().st_mtime_ns for p in (membership_db, price_db)]
    cli.main(_args(membership_db, price_db, "--from", "2008-01", "--to", "2008-12"))
    assert [p.stat().st_mtime_ns for p in (membership_db, price_db)] == before


def _args(membership_db: Path, price_db: Path, *extra: object) -> list[str]:
    return [
        "sp500",
        "--membership-db",
        str(membership_db),
        "--price-db",
        str(price_db),
        *map(str, extra),
    ]


def test_default_end_is_last_complete_month(
    membership_db: Path, price_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    today = date(2026, 10, 7)
    assert coverage.last_complete_month({"A": ("2000", "2026-10-06")}, today) == (
        "2026-09"
    )
    assert coverage.last_complete_month({"A": ("2000", "2026-07-31")}, today) == (
        "2026-07"
    )
    assert coverage.last_complete_month({}, today) == "2026-09"
    # Tue 28 July: 29-31 July are still missing, so July is not complete.
    assert coverage.last_complete_month({"A": ("2000", "2026-07-28")}, today) == (
        "2026-06"
    )
    cli.main(_args(membership_db, price_db, "--from", "2026-06"))
    out = capsys.readouterr().out
    assert "2026-07 as_of" in out and "2026-08 as_of" not in out


def test_edge_revision_without_closes_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRICE_SCHEMA)
    empty: Rows = {2000: [("2000-01-03", None)]}
    _add_revision(conn, "X", "x-empty", empty, "2030-01-01", start_date="1960-01-01")
    _add_revision(conn, "X", "x-good", _span("1999-01-04", "2026-07-31"))
    # a mapped chunk whose payload row is gone is skipped, not fatal
    conn.execute(
        "INSERT INTO historical_price_v2_revision_chunks VALUES (2, 'rows', 1990, 'gone')"
    )
    conn.commit()
    conn.close()
    assert coverage.price_spans(path)["X"] == ("1999-01-04", "2026-07-31")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--from", "2010-01", "--to", "2009-12"), "after the end month"),
        (("--month", "2008-09", "--from", "2008-01"), None),
        (("--csv", "/no/such/dir/out.csv"), "csv directory not found"),
    ],
)
def test_cli_rejects_bad_ranges(
    membership_db: Path, price_db: Path, extra: tuple[str, ...], message: str | None
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(_args(membership_db, price_db, *extra))
    if message is None:
        assert exc.value.code == 2  # argparse usage error
    else:
        assert message in str(exc.value)


def test_cli_reports_unreadable_price_cache(
    membership_db: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()  # exists, but no price tables
    with pytest.raises(SystemExit, match="cannot read price cache"):
        cli.main(_args(membership_db, other))
