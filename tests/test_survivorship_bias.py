"""Measure survivorship bias on an equal-weight S&P 500 (#75)."""

import csv
import sqlite3
from pathlib import Path

import pytest

from app.cli import survivorship_bias as cli
from app.repositories import db
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership import survivorship as sv
from tests.test_coverage_report import _PRICE_SCHEMA, _add_revision

WIKI_HEADER = (
    "ticker,date,open,high,low,close,volume,ex-dividend,split_ratio,"
    "adj_open,adj_high,adj_low,adj_close,adj_volume"
)


def test_yf_series_adds_dividends_and_skips_null_closes() -> None:
    series = sv.yf_series(
        [
            ("2000-01-03", 10.0, 0.0),
            ("2000-01-04", None, 0.0),
            ("2000-01-05", 11.0, 1.0),
        ]
    )
    assert series.daily == [("2000-01-05", 1.2)]
    assert series.last_close == 11.0


def test_wiki_series_applies_split_and_dividend() -> None:
    # 2-for-1 split on 01-04 halves the close; holding value is unchanged.
    series = sv.wiki_series(
        [
            ("2000-01-03", 100.0, 0.0, 1.0),
            ("2000-01-04", 50.0, 0.0, 2.0),
            ("2000-01-05", 49.0, 1.0, None),
        ]
    )
    assert series.daily == [("2000-01-04", 1.0), ("2000-01-05", 1.0)]


def test_monthly_needs_a_session_in_the_previous_month() -> None:
    series = sv.Series(
        daily=[("2000-01-31", 1.1), ("2000-02-01", 1.5), ("2000-02-29", 2.0)],
        last_close=1.0,
    )
    assert sv.monthly(series) == {"2000-02": 3.0}  # January has no December


def test_terminal_price_settles_the_exit_month() -> None:
    series = sv.Series(daily=[("2008-09-12", 1.0)], last_close=4.0)
    returns = {"2008-09": 0.5}
    assert sv.with_terminal_price(returns, series, "2008-09-15", 2.0) == {
        "2008-09": 0.25
    }
    # prices end more than DELISTED_WITHIN before the removal: unchanged
    assert sv.with_terminal_price(returns, series, "2008-10-30", 2.0) == returns
    assert sv.with_terminal_price(returns, series, "2008-09-15", None) == returns


def test_portfolio_equal_weights_priced_members() -> None:
    returns = {"A": {"2000-01": 1.2}, "B": {"2000-01": 0.8}}
    results = sv.portfolio(
        ["2000-01", "2000-02"], {"2000-01": ["A", "B", "C"]}, returns
    )
    assert [(r.members, r.priced, round(r.gross, 6)) for r in results] == [
        (3, 2, 1.0),
        (0, 0, 1.0),
    ]


def test_summarise_cagr_and_drawdowns() -> None:
    months = [f"{y}-{m:02d}" for y in (2000, 2001) for m in range(1, 13)]
    grosses = [1.0] * 24
    grosses[1], grosses[2] = 0.5, 1.5  # fall 50%, recover to 75%
    results = [
        sv.MonthResult(month=m, members=2, priced=1, gross=g)
        for m, g in zip(months, grosses, strict=True)
    ]
    summary = sv.summarise(results)
    assert summary.total == pytest.approx(-0.25)
    assert summary.cagr == pytest.approx(0.75**0.5 - 1)
    assert summary.max_drawdown == pytest.approx(-0.5)
    assert summary.drawdown_2000_2002 == pytest.approx(-0.5)
    assert summary.drawdown_2007_2009 == 0.0
    assert summary.coverage == 0.5


@pytest.fixture
def dbs(tmp_path: Path) -> dict[str, Path]:
    """Membership: KEEP (today), GONE (left 2000-03, WIKI only, acquired at
    a terminal price), REU (reused: excluded from point in time)."""
    membership = tmp_path / "m.db"
    repo = IndexMembershipRepository(db.make_connect(lambda: membership))
    repo.ensure_schema()
    spells = [
        ("KEEP", "1996-01-02", None),
        ("GONE", "1996-01-02", "2000-03-01"),
        ("REU", "1996-01-02", "1997-01-02"),
        ("REU", "1998-01-02", None),
    ]
    import_id, _ = repo.record_import(
        index_id="sp500",
        source="t",
        source_ref="t",
        source_digest="d",
        first_date="1996-01-02",
        last_date="2018-03-01",
        snapshot_count=2,
        low_confidence_before="1996-01-02",
        intervals=[
            MembershipInterval(
                ticker=t, start_date=s, end_date=e, security_key=f"{t}@{s}"
            )
            for t, s, e in spells
        ],
    )
    repo.replace_terminal_events(
        import_id,
        [
            TerminalEvent(
                security_key="GONE@1996-01-02",
                ticker="GONE",
                exit_date="2000-03-01",
                event_type="acquisition",
                terminal_price=30.0,
                evidence="wikipedia",
            )
        ],
    )
    prices = tmp_path / "p.db"
    conn = sqlite3.connect(prices)
    conn.executescript(_PRICE_SCHEMA)
    for symbol in ("KEEP", "REU", "SPY"):
        _add_revision(
            conn,
            symbol,
            symbol,
            {
                1999: [("1999-12-31", 10.0)],
                2000: [("2000-01-31", 11.0), ("2000-02-29", 12.1)],
            },
        )
    conn.commit()
    conn.close()
    wiki_csv = tmp_path / "w.csv"
    wiki_csv.write_text(
        f"{WIKI_HEADER}\n"
        "GONE,1999-12-31,1,1,1,20,1,0,1,1,1,1,1,1\n"
        "GONE,2000-01-31,1,1,1,20,1,0,1,1,1,1,1,1\n"
        "GONE,2000-02-25,1,1,1,24,1,0,1,1,1,1,1,1\n"
    )
    wiki = tmp_path / "w.db"
    wiki_repo = WikiPriceRepository(db.make_connect(lambda: wiki))
    wiki_repo.ensure_schema()
    wiki_repo.import_csv(wiki_csv)
    overrides = tmp_path / "o.csv"
    overrides.write_text("sp_ticker,wiki_ticker,start_date,end_date\n")
    return {"membership": membership, "price": prices, "wiki": wiki, "o": overrides}


def _args(dbs: dict[str, Path], *extra: str) -> list[str]:
    return [
        "--membership-db",
        str(dbs["membership"]),
        "--price-db",
        str(dbs["price"]),
        "--wiki-db",
        str(dbs["wiki"]),
        "--overrides",
        str(dbs["o"]),
        "--from",
        "2000-01",
        "--to",
        "2000-02",
        *extra,
    ]


def test_cli_compares_today_and_point_in_time(
    dbs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = [p.stat().st_mtime_ns for p in dbs.values()]
    out_csv = tmp_path / "out.csv"
    cli.main(_args(dbs, "--csv", str(out_csv)))
    out = capsys.readouterr().out
    assert "bias from membership alone" in out and "with delisted prices" in out
    rows = {(r[0], r[1]): r for r in _csv(out_csv)}
    # today: KEEP and REU, both +10% a month
    assert rows[("today", "2000-02")][2:] == ["2", "2", "1.100000"]
    # point in time: KEEP (+10%) and GONE settled at 30 vs last close 24
    # (Feb gross 24/20 * 30/24 = 1.5); REU excluded as reused
    assert rows[("point_in_time", "2000-02")][2:] == ["2", "2", "1.300000"]
    # yfinance alone cannot price GONE
    assert rows[("pit_yfinance", "2000-02")][2:] == ["2", "1", "1.100000"]
    assert [p.stat().st_mtime_ns for p in dbs.values()] == before


def _csv(path: Path) -> list[list[str]]:
    with path.open() as handle:
        return list(csv.reader(handle))[1:]


def test_cli_rejects_missing_db_and_inverted_range(
    dbs: dict[str, Path], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit, match="database not found"):
        cli.main(_args({**dbs, "wiki": tmp_path / "none.db"}))
    assert not (tmp_path / "none.db").exists()
    with pytest.raises(SystemExit, match="after --to"):
        cli.main([*_args(dbs), "--from", "2001-01"])
