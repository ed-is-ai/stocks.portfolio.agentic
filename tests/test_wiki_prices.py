"""The Quandl WIKI price archive as a separate price source (#70)."""

import sqlite3
from pathlib import Path

import pytest

from app.cli import coverage_report as coverage_cli
from app.cli import wiki_prices as cli
from app.repositories import db
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership import coverage, wiki_link
from tests.test_coverage_report import _PRICE_SCHEMA, Rows, _add_revision

HEADER = (
    "ticker,date,open,high,low,close,volume,ex-dividend,split_ratio,"
    "adj_open,adj_high,adj_low,adj_close,adj_volume"
)
# ENDS: delisted, WIKI to 2009-06 (last row has a blank close); BRK_B: class
# share with a 2-for-1 split on 2017-01-04 and another since (yfinance is
# split-adjusted); YHOO: AABA's old ticker (override); RE: a reused S&P ticker;
# BAD: prices that disagree with yfinance.
WIKI_ROWS = """\
BRK_B,1996-05-09,1.0,1.0,1.0,1.0,100.0,0.0,1.0,1,1,1,1,100
BRK_B,2017-01-03,200.0,201.0,199.0,200.0,1000.0,0.0,1.0,1,1,1,1,1000
BRK_B,2017-01-04,100.0,101.0,99.0,100.0,1000.0,0.5,2.0,1,1,1,1,1000
BAD,2017-01-03,10.0,10.0,10.0,10.0,1.0,0.0,1.0,1,1,1,1,1
BAD,2017-01-04,10.0,10.0,10.0,10.0,1.0,0.0,1.0,1,1,1,1,1
BAD,2017-01-05,10.0,10.0,10.0,10.0,1.0,0.0,1.0,1,1,1,1,1
ENDS,2008-06-30,10.0,10.0,10.0,10.0,5.0,0.0,1.0,1,1,1,1,5
ENDS,2009-06-15,11.0,11.0,11.0,11.0,5.0,0.0,1.0,1,1,1,1,5
ENDS,2009-06-16,11.0,11.0,11.0,,5.0,0.0,1.0,1,1,1,1,5
RE,1990-01-02,5.0,5.0,5.0,5.0,1.0,0.0,1.0,1,1,1,1,1
RE,2018-03-27,6.0,6.0,6.0,6.0,1.0,0.0,1.0,1,1,1,1,1
YHOO,1996-04-12,25.25,43.0,24.5,33.0,17030000.0,0.0,1.0,1,1,1,1,1
YHOO,2017-06-16,52.79,53.3,51.9,52.5892,251032146.0,0.0,1.0,1,1,1,1,1
"""

INTERVALS = {
    "BRK.B": [("1996-01-02", None)],
    "ENDS": [("1996-01-02", "2010-01-04")],
    "AABA": [("1999-12-08", "2017-06-19")],
    "LEHMQ": [("1996-01-02", "2008-10-01")],
    "RE": [("1996-01-02", "1997-01-15"), ("2000-01-03", None)],
}
OVERRIDES = (
    "sp_ticker,wiki_ticker,start_date,end_date\nAABA,YHOO,1999-12-08,2017-06-19\n"
)


@pytest.fixture
def wiki_csv(tmp_path: Path) -> Path:
    path = tmp_path / "WIKI_PRICES.csv"
    path.write_text(f"{HEADER}\n{WIKI_ROWS}")
    return path


@pytest.fixture
def wiki_db(tmp_path: Path, wiki_csv: Path) -> Path:
    path = tmp_path / "wiki.db"
    cli.main(["import", "--csv", str(wiki_csv), "--wiki-db", str(path)])
    return path


@pytest.fixture
def overrides(tmp_path: Path) -> Path:
    path = tmp_path / "overrides.csv"
    path.write_text(OVERRIDES)
    return path


@pytest.fixture
def price_db(tmp_path: Path) -> Path:
    path = tmp_path / "prices.db"
    conn = sqlite3.connect(path)
    conn.executescript(_PRICE_SCHEMA)
    # Older BRK-B revision would agree in 2017 but is not the latest-ending.
    _add_revision(conn, "BRK-B", "brk-old", {2017: [("2017-01-04", 200.0)]}, "2018")
    brk: Rows = {
        1996: [("1996-05-09", 1.0)],
        2017: [("2017-01-03", 50.0), ("2017-01-04", 50.25)],
        2026: [("2026-07-31", 2.0)],
    }
    _add_revision(conn, "BRK-B", "brk", brk)
    _add_revision(conn, "YHOO", "yhoo", {2017: [("2017-06-16", 52.5892)]}, "2017-07")
    bad: Rows = {
        2017: [("2017-01-03", 10.0), ("2017-01-04", 20.0), ("2017-01-05", 30.0)]
    }
    _add_revision(conn, "BAD", "bad", bad)
    _add_revision(
        conn, "ENDS", "ends", {1990: [("1990-01-02", 1.0)], 2008: [("2008-06-30", 1.0)]}
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def membership_db(tmp_path: Path) -> Path:
    path = tmp_path / "m.db"
    repo = IndexMembershipRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    repo.record_import(
        index_id="sp500",
        source="test",
        source_ref="sha",
        source_digest="d",
        first_date="1996-01-02",
        last_date="2026-07-31",
        snapshot_count=3,
        low_confidence_before="1996-01-02",
        intervals=[
            MembershipInterval(
                ticker=t, start_date=s, end_date=e, security_key=f"{t}@{s}"
            )
            for t, spells in INTERVALS.items()
            for s, e in spells
        ],
    )
    return path


def _repo(path: Path) -> WikiPriceRepository:
    return WikiPriceRepository(lambda: coverage.read_only(path))


def test_import_records_prices_and_spans(wiki_db: Path) -> None:
    repo = _repo(wiki_db)
    record = repo.latest_import()
    assert record is not None
    assert (record.row_count, record.ticker_count) == (13, 5)
    assert (record.first_date, record.last_date) == ("1990-01-02", "2018-03-27")
    assert record.source_file == "WIKI_PRICES.csv"
    assert len(record.source_digest) == 64
    spans = repo.ticker_spans()
    assert spans["ENDS"] == ("2008-06-30", "2009-06-15")  # blank close excluded
    assert repo.closes("BRK_B", 2017) == {
        "2017-01-03": (200.0, 1.0),
        "2017-01-04": (100.0, 2.0),
    }
    conn = coverage.read_only(wiki_db)
    stored = conn.execute(
        "SELECT open, high, low, close, volume, ex_dividend, split_ratio"
        " FROM wiki_prices WHERE ticker = 'BRK_B' AND date = '2017-01-04'"
    ).fetchone()
    blank = conn.execute(
        "SELECT close FROM wiki_prices WHERE ticker = 'ENDS' AND date = '2009-06-16'"
    ).fetchone()
    conn.close()
    assert stored == (100.0, 101.0, 99.0, 100.0, 1000.0, 0.5, 2.0)
    assert blank == (None,)


def test_reimport_same_file_writes_nothing(
    wiki_db: Path, wiki_csv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    before = wiki_db.stat().st_mtime_ns
    cli.main(["import", "--csv", str(wiki_csv), "--wiki-db", str(wiki_db)])
    assert "already imported" in capsys.readouterr().out
    assert wiki_db.stat().st_mtime_ns == before


def test_changed_file_replaces_prices(wiki_db: Path, wiki_csv: Path) -> None:
    wiki_csv.write_text(f"{HEADER}\nNEW,2010-01-04,1,1,1,1,1,0,1,1,1,1,1,1\n")
    repo = WikiPriceRepository(db.make_connect(lambda: wiki_db))
    record, created = repo.import_csv(wiki_csv)
    assert created and (record.row_count, record.ticker_count) == (1, 1)
    assert list(repo.ticker_spans()) == ["NEW"]


def test_bad_header_exits_naming_columns(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,day,open\nA,2010-01-04,1\n")
    path = tmp_path / "wiki.db"
    with pytest.raises(SystemExit, match="missing columns: ticker, date, high"):
        cli.main(["import", "--csv", str(bad), "--wiki-db", str(path)])
    assert _repo(path).latest_import() is None
    assert _repo(path).ticker_spans() == {}


def test_wiki_ticker_links_class_shares_and_overrides(overrides: Path) -> None:
    found = wiki_link.load_overrides(overrides)
    assert wiki_link.wiki_ticker("BRK.B", "2010-01-31", found) == "BRK_B"
    assert wiki_link.wiki_ticker("AABA", "2010-01-31", found) == "YHOO"
    assert wiki_link.wiki_ticker("AABA", "2017-07-31", found) == "AABA"
    spans = {"ENDS": ("2008-06-30", "2009-06-15")}
    assert wiki_link.wiki_covers(spans, found, "ENDS", "2009-06-30")
    assert not wiki_link.wiki_covers(spans, found, "ENDS", "2009-07-31")
    assert not wiki_link.wiki_covers(spans, found, "ENDS", "2008-05-31")


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("sp_ticker,wiki_ticker,start_date,end_date\nA,B,2001-01-01\n", 2),
        ("sp_ticker,wiki_ticker,start_date,end_date\nA,B,2001,2002\n", 2),
        ("sp_ticker,wiki_ticker,start_date,end_date\n\nA,,2001-01-01,2002-01-01\n", 3),
        ("sp_ticker,wiki_ticker,start_date,end_date\nA,B,2002-01-01,2001-01-01\n", 2),
        ("ticker,wiki\n", 1),
    ],
)
def test_malformed_override_names_line(tmp_path: Path, text: str, line: int) -> None:
    path = tmp_path / "o.csv"
    path.write_text(text)
    with pytest.raises(ValueError, match=f"line {line}"):
        wiki_link.load_overrides(path)


def test_coverage_wiki_group(
    membership_db: Path, price_db: Path, wiki_db: Path, overrides: Path
) -> None:
    repo = IndexMembershipRepository(lambda: coverage.read_only(membership_db))
    covers = wiki_link.wiki_coverage(wiki_db, overrides)
    spans = coverage.price_spans(price_db)
    rows = {
        r.month: r
        for r in coverage.coverage_report(repo, spans, "2000-01", "2018-02", covers)
    }
    for row in rows.values():
        total = row.priced + row.suspect + row.wiki + row.not_cached + row.missing
        assert total == row.members
    sep = rows["2008-09"]
    assert sep.priced == 1  # BRK.B: yfinance wins over WIKI's BRK_B
    assert sep.wiki_tickers == ["AABA", "ENDS"]  # override YHOO; delisted ENDS
    assert sep.suspect_tickers == ["RE"]  # reused stays suspect under WIKI
    assert sep.missing_tickers == ["LEHMQ"]  # not in WIKI
    assert rows["2009-06"].wiki_tickers == ["AABA", "ENDS"]
    assert rows["2009-07"].missing_tickers == ["ENDS"]
    assert rows["2008-06"].wiki_tickers == ["AABA"]  # yfinance still has ENDS
    plain = coverage.coverage_report(repo, spans, "2008-09", "2008-09")[0]
    assert (plain.wiki, plain.wiki_tickers) == (0, [])


def test_coverage_cli_wiki_column_and_footer(
    membership_db: Path,
    price_db: Path,
    wiki_db: Path,
    overrides: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = ["sp500", "--membership-db", str(membership_db)]
    base += ["--price-db", str(price_db), "--month", "2008-09"]
    coverage_cli.main([*base, "--wiki-db", str(wiki_db), "--overrides", str(overrides)])
    out = capsys.readouterr().out
    assert "suspect=1 wiki=2 not_cached=0 missing=1" in out
    assert "wiki: AABA ENDS" in out
    assert "not loaded" not in out
    coverage_cli.main([*base, "--wiki-db", str(tmp_path / "none.db")])
    out = capsys.readouterr().out
    assert "wiki=0" in out and "WIKI prices not loaded" in out
    assert not (tmp_path / "none.db").exists()
    bad = tmp_path / "bad.csv"
    bad.write_text("sp_ticker,wiki_ticker,start_date,end_date\nA,B,x,y\n")
    with pytest.raises(SystemExit, match="line 2"):
        coverage_cli.main([*base, "--wiki-db", str(wiki_db), "--overrides", str(bad)])


def test_validate_agreement_and_outliers(
    wiki_db: Path, price_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = wiki_link.validate(_repo(wiki_db), price_db, 2017)
    # ENDS and RE have no 2017 yfinance chunk and are skipped.
    # BRK_B agrees once WIKI's split and yfinance's later split are applied.
    assert (result.tickers, result.dates, result.agreeing) == (3, 6, 4)
    assert [t for t, _ in result.outliers] == ["BAD"]
    assert [(t, round(f, 2)) for t, f in result.factors] == [
        ("BAD", 0.5),
        ("BRK_B", 2.0),
    ]
    before = [p.stat().st_mtime_ns for p in (wiki_db, price_db)]
    cli.main(["validate", "--wiki-db", str(wiki_db), "--price-db", str(price_db)])
    out = capsys.readouterr().out
    assert "3 tickers, 6 dates, 66.7% of closes within 1% of yfinance" in out
    assert "  BAD x0.50 BRK_B x2.00" in out
    assert "  BAD 33.3%" in out
    assert [p.stat().st_mtime_ns for p in (wiki_db, price_db)] == before


def test_unmatched_lists_missing_intervals(
    membership_db: Path,
    price_db: Path,
    wiki_db: Path,
    overrides: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(
        [
            "unmatched",
            "--from",
            "2008-01",
            "--membership-db",
            str(membership_db),
            "--price-db",
            str(price_db),
            "--wiki-db",
            str(wiki_db),
            "--overrides",
            str(overrides),
        ]
    )
    lines = capsys.readouterr().out.splitlines()
    # --to defaults to WIKI's last complete month (RE ends 2018-03-27).
    assert lines[0] == "2008-01..2018-02: 2 intervals with missing months"
    assert lines[1] == "LEHMQ 1996-01-02..2008-10-01 missing_months=9"
    assert lines[2] == "ENDS 1996-01-02..2010-01-04 missing_months=6"


def test_cli_missing_database_exits(tmp_path: Path, price_db: Path) -> None:
    absent = tmp_path / "absent.db"
    with pytest.raises(SystemExit, match="absent.db"):
        cli.main(["validate", "--wiki-db", str(absent), "--price-db", str(price_db)])
    assert not absent.exists()


def test_not_cached_comes_before_wiki(membership_db: Path, price_db: Path) -> None:
    repo = IndexMembershipRepository(lambda: coverage.read_only(membership_db))
    spans = coverage.price_spans(price_db)

    def everything(ticker: str, as_of: str) -> bool:
        return True

    # BRK.B is still a member and yfinance starts 1996-05: fetch it, not WIKI.
    row = coverage.coverage_report(repo, spans, "1996-02", "1996-02", everything)[0]
    assert row.not_cached_tickers == ["BRK.B"]
    assert "BRK.B" not in row.wiki_tickers


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "sp_ticker,wiki_ticker,start_date,end_date\nA,B,20010101,20020101\n",
            "line 2",
        ),
        (
            "sp_ticker,wiki_ticker,start_date,end_date\n"
            "A,B,2001-01-01,2002-01-01\nA,C,2001-06-01,2003-01-01\n",
            "line 3: overlaps",
        ),
    ],
)
def test_override_rejects_basic_iso_and_overlaps(
    tmp_path: Path, text: str, match: str
) -> None:
    path = tmp_path / "o.csv"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        wiki_link.load_overrides(path)


def test_override_file_with_bom_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "o.csv"
    path.write_text("﻿" + OVERRIDES)
    assert [o.wiki_ticker for o in wiki_link.load_overrides(path)] == ["YHOO"]
    with pytest.raises(ValueError, match="overrides file not found"):
        wiki_link.load_overrides(tmp_path / "none.csv")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("BRK_B,2017-01-03,1\n", "line 2: expected"),
        (WIKI_ROWS + WIKI_ROWS.splitlines()[0] + "\n", "duplicate ticker/date"),
        ("", "no price rows"),
    ],
)
def test_bad_rows_leave_the_archive_untouched(
    wiki_db: Path, tmp_path: Path, body: str, match: str
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(f"﻿{HEADER}\n{body}")
    with pytest.raises(SystemExit, match=match):
        cli.main(["import", "--csv", str(bad), "--wiki-db", str(wiki_db)])
    record = _repo(wiki_db).latest_import()
    assert record is not None and record.row_count == 13


def test_unmatched_rejects_inverted_range(
    membership_db: Path, price_db: Path, wiki_db: Path, overrides: Path
) -> None:
    args = ["unmatched", "--membership-db", str(membership_db)]
    args += ["--price-db", str(price_db), "--wiki-db", str(wiki_db)]
    args += ["--overrides", str(overrides), "--from", "2018-01", "--to", "2010-01"]
    with pytest.raises(SystemExit, match="after the end month"):
        cli.main(args)
