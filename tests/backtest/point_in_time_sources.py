"""Tmp point-in-time roster sources shared by #82 tests (no test cases)."""

from __future__ import annotations

from pathlib import Path

from app.repositories import db
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership.sp500_import import INDEX_ID

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


def interval(ticker: str, start: str, end: str | None) -> MembershipInterval:
    return MembershipInterval(
        ticker=ticker, start_date=start, end_date=end, security_key=f"{ticker}@{start}"
    )


def event(
    ticker: str, start: str, end: str, kind: str, price: float | None
) -> TerminalEvent:
    return TerminalEvent(
        security_key=f"{ticker}@{start}",
        ticker=ticker,
        exit_date=end,
        event_type=kind,  # type: ignore[arg-type]
        terminal_price=price,
        evidence="wikipedia",
    )


def write_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
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
        intervals=[interval(t, s, e) for t, s, e, _k, _p in spells],
    )
    repo.replace_terminal_events(
        import_id,
        [event(t, s, e, k, p) for t, s, e, k, p in spells if e and k],
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
