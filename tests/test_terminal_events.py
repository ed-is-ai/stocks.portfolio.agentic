"""SEC EDGAR terminal events for S&P 500 exits (#73). Fixtures only."""

import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.cli import import_terminal_events as cli
from app.repositories import db
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.services.index_membership import edgar
from app.services.index_membership import terminal_events as te
from app.services.index_membership import wikipedia_check as wiki
from app.services.index_membership.sp500_import import Fetch


def _interval(ticker: str, end: str | None) -> MembershipInterval:
    return MembershipInterval(
        ticker=ticker,
        start_date="1996-01-02",
        end_date=end,
        security_key=f"{ticker}@1996-01-02",
    )


INTERVALS = [
    _interval("ACQ", "2010-03-01"),  # cash acquisition
    _interval("STK", "2011-01-03"),  # stock deal
    _interval("BKR", "2008-09-15"),  # bankruptcy (wiki + 8-K)
    _interval("QUI", "2009-06-01"),  # bankruptcy from EDGAR only
    _interval("CAP", "2012-03-19"),  # market-cap removal, name not in EDGAR
    _interval("REN", "2013-05-01"),  # rename
    _interval("DLS", "2014-02-03"),  # delisting only
    _interval("NOW", "2015-06-01"),  # not in Wikipedia
    _interval("AMB", "2016-01-04"),  # ambiguous name
    _interval("ERR", "2017-01-03"),  # SEC request fails
    _interval("OLD", "1999-12-01"),  # before 2000: no event
    _interval("CUR", None),  # still a member: no event
]

WIKI_ROWS = [
    ("March 1, 2010", "ACQ", "Acme Corp.", "Acquired by Big Co."),
    ("January 3, 2011", "STK", "Stock Co Inc", "Merged with Other."),
    ("September 16, 2008", "BKR", "Broke Holdings", "Filed for Chapter 11."),
    ("June 1, 2009", "QUI", "Quiet Co", "Removed."),
    ("March 19, 2012", "CAP", "Small Cap Inc", "Market capitalization change."),
    ("April 29, 2013", "REN", "Renamed Corp", "Renamed to NEWN."),
    ("February 3, 2014", "DLS", "Delisted Ltd", "S&P index change."),
    ("June 1, 2014", "NOW", "Now Inc", "Removed a year too early."),
    ("January 4, 2016", "AMB", "Twin Corp", "Acquired."),
    ("January 3, 2017", "ERR", "Error Inc", "Acquired by Erring."),
]

CIK_LOOKUP = """ACME CORP:0000000001:
STOCK CO INC:0000000002:
BROKE HOLDINGS INC:0000000003:
QUIET CO:0000000004:
DELISTED LTD:0000000005:
TWIN CORP:0000000006:
TWIN CORPORATION /DE/:0000000007:
ERROR INC:0000000008:
NOW INC:0000000009:
"""

MERGER_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000109000002/defm14a.htm"


def _changes_html() -> str:
    rows = "".join(
        f"<tr><td>{d}</td><td>NEWX</td><td>New</td><td>{t}</td><td>{n}</td>"
        f"<td>{r}</td><td></td></tr>"
        for d, t, n, r in WIKI_ROWS
    )
    return f'<table id="changes"><tr><th>Date</th></tr>{rows}</table>'


def _submissions(cik: int, *filings: tuple[str, str, str]) -> bytes:
    """Return a submissions JSON with ``(form, date, items)`` filings."""
    n = range(len(filings))
    return json.dumps(
        {
            "cik": str(cik),
            "filings": {
                "recent": {
                    "form": [f[0] for f in filings],
                    "filingDate": [f[1] for f in filings],
                    "items": [f[2] for f in filings],
                    "accessionNumber": [f"{cik:010d}-09-{i:06d}" for i in n],
                    "primaryDocument": [f"{f[0].lower()}.htm" for f in filings],
                }
            },
        }
    ).encode()


def _sec_files() -> dict[str, bytes]:
    url = edgar.SUBMISSIONS_URL.format
    return {
        edgar.CIK_LOOKUP_URL: CIK_LOOKUP.encode("latin-1"),
        url(cik=1): _submissions(
            1,
            ("10-K", "2009-02-01", ""),
            ("PREM14A", "2009-10-01", ""),
            ("DEFM14A", "2009-12-01", ""),
            ("425", "2010-03-05", ""),  # after exit: not the price source
            ("DEFM14A", "2007-01-01", ""),  # outside the window
        ),
        MERGER_URL: b"<p>receive <b>$45.50</b> per share in cash</p>",
        url(cik=2): _submissions(2, ("425", "2010-11-01", "")),
        "https://www.sec.gov/Archives/edgar/data/2/000000000209000000/425.htm": (
            b"one share of Other for each share"
        ),
        url(cik=3): _submissions(3, ("8-K", "2008-09-15", "1.03,9.01")),
        url(cik=4): _submissions(4, ("8-K", "2009-05-01", "1.03")),
        url(cik=5): _submissions(5, ("25", "2014-01-20", "")),
        url(cik=6): _submissions(6, ("10-K", "2015-03-01", "")),
        url(cik=7): _submissions(7, ("10-Q", "2015-08-01", "")),
        url(cik=9): _submissions(9, ("25", "2015-06-01", "")),
    }


def _fetcher(files: dict[str, bytes]) -> Fetch:
    return files.__getitem__


@pytest.fixture
def repo(tmp_path: Path) -> IndexMembershipRepository:
    repo = IndexMembershipRepository(db.make_connect(lambda: tmp_path / "m.db"))
    repo.ensure_schema()
    repo.record_import(
        index_id="sp500",
        source="test",
        source_ref="test@abc",
        source_digest="d1",
        first_date="1996-01-02",
        last_date="2020-01-02",
        snapshot_count=2,
        low_confidence_before="2001-01-16",
        intervals=INTERVALS,
    )
    return repo


def _build(repo: IndexMembershipRepository) -> tuple[int, list[TerminalEvent]]:
    return te.build_events(
        repo,
        _fetcher({wiki.CHANGES_URL: _changes_html().encode()}),
        _fetcher(_sec_files()),
    )


def test_every_matrix_row_gets_one_event(repo: IndexMembershipRepository) -> None:
    _, events = _build(repo)
    got = {
        e.ticker: (e.event_type, e.evidence, e.terminal_price, e.cik) for e in events
    }
    assert got == {
        "ACQ": ("acquisition", "wikipedia+edgar", 45.5, 1),
        "STK": ("acquisition", "wikipedia+edgar", None, 2),
        "BKR": ("bankruptcy", "wikipedia+edgar", None, 3),
        "QUI": ("bankruptcy", "edgar", None, 4),
        "CAP": ("still_trading", "wikipedia", None, None),
        "REN": ("rename", "wikipedia", None, None),
        "DLS": ("delisting", "edgar", None, 5),
        "NOW": ("unknown", "none", None, None),
        "AMB": ("acquisition", "wikipedia", None, None),
        "ERR": ("acquisition", "wikipedia", None, None),
    }
    by = {e.ticker: e for e in events}
    assert by["ACQ"].source_filing == MERGER_URL
    assert by["ACQ"].terms == "cash $45.50 per share"
    assert by["STK"].terms == "terms unknown"
    assert by["BKR"].source_filing is not None
    assert by["BKR"].source_filing.endswith("/8-k.htm")
    assert by["CAP"].note == "wikipedia only: name not found in EDGAR"
    assert by["NOW"].note == "not in Wikipedia changes"  # ticker never by bare name
    assert by["AMB"].note == "wikipedia only: ambiguous name in EDGAR"
    assert by["ERR"].note == "wikipedia only: edgar unavailable"


def test_conflicting_edgar_keeps_wikipedia_class() -> None:
    change = wiki.WikiChange(
        date="2012-03-19",
        added=None,
        removed="CAP",
        removed_name="Cap",
        reason="Market cap.",
    )
    filing = edgar.Filing(form="8-K", date="2012-01-01", items=("1.03",), url="u")
    event = te.classify(INTERVALS[4], change, 1, [filing])
    assert event.event_type == "still_trading" and event.evidence == "wikipedia"
    assert event.note == "conflict: edgar suggests bankruptcy"
    unconfirmed = te.classify(INTERVALS[4], change, 1, [])
    assert unconfirmed.note == "no confirming edgar filing"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Company filed for bankruptcy.", "bankruptcy"),
        ("Chapter 11 filing", "bankruptcy"),
        ("Acquired by Pfizer", "acquisition"),
        ("Merger with X", "acquisition"),
        ("Market capitalization change.", "still_trading"),
        ("Moved to S&P MidCap 400", "still_trading"),
        ("Moved to the S&P 600", "still_trading"),
        ("Company renamed", "rename"),
        ("Ticker change", "rename"),
        ("Spun off", None),
    ],
)
def test_classify_reason(reason: str, expected: str | None) -> None:
    assert te.classify_reason(reason) == expected


def test_replace_is_atomic_and_not_duplicated(
    repo: IndexMembershipRepository,
) -> None:
    import_id, events = _build(repo)
    repo.replace_terminal_events(import_id, events)
    repo.replace_terminal_events(import_id, events)
    assert repo.terminal_events("sp500") == sorted(
        events, key=lambda e: (e.exit_date, e.security_key)
    )
    with pytest.raises(sqlite3.IntegrityError):  # duplicate key: rolled back
        repo.replace_terminal_events(import_id, events[:1] * 2)
    assert len(repo.terminal_events("sp500")) == len(events) == 10
    assert repo.terminal_events("ftse100") == []


def test_cli_requires_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "INDEX_MEMBERSHIP_DB", tmp_path / "m.db")
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    def no_network(url: str) -> bytes:
        raise AssertionError(f"request made: {url}")

    with pytest.raises(SystemExit, match="SEC_USER_AGENT"):
        cli.main(["sp500"], fetch=no_network)


def test_cli_stores_and_counts_every_event(
    repo: IndexMembershipRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "INDEX_MEMBERSHIP_DB", tmp_path / "m.db")
    wiki_fetch = _fetcher({wiki.CHANGES_URL: _changes_html().encode()})
    sec = _fetcher(_sec_files())
    cli.main(["sp500", "--dry-run"], fetch=wiki_fetch, sec=sec)
    cli.main(["sp500", "--limit", "2"], fetch=wiki_fetch, sec=sec)
    assert repo.terminal_events("sp500") == []
    cli.main(["sp500"], fetch=wiki_fetch, sec=sec)
    cli.main(["sp500"], fetch=wiki_fetch, sec=sec)
    out = capsys.readouterr().out.split("events: ")[-1]
    counts = [
        int(line.split(": ")[1])
        for line in out.splitlines()
        if line.startswith("  ") and not line.startswith("  unknown ")
    ]
    assert out.startswith("10 ") and sum(counts) == 10
    assert "written: yes" in out
    assert "unknown NOW@1996-01-02 2015-06-01: not in Wikipedia changes" in out
    assert len(repo.terminal_events("sp500")) == 10


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("The Coca-Cola Company", "COCA COLA"),
        ("COCA COLA CO", "COCA COLA"),
        ("McDonald's Corp.", "MCDONALDS"),
        ("U.S. Bancorp", "US BANCORP"),
        ("TWIN CORPORATION /DE/", "TWIN"),
        ("AT&T Inc.", "AT AND T"),
        ("Lehman Brothers Holdings Inc.", "LEHMAN BROTHERS"),
    ],
)
def test_normalise_name(name: str, expected: str) -> None:
    assert edgar.normalise_name(name) == expected


def test_parse_cik_lookup_handles_colons_and_junk() -> None:
    lookup = edgar.parse_cik_lookup(
        "A:B CORP:0000000010:\nAB CORP INC:0000000011:\nno cik here\n"
    )
    assert lookup == {"A B": {10}, "AB": {11}}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$45.50 per share in cash", (45.5, False)),
        ("$45.50 in cash per share", (45.5, False)),
        ("a CASH CONSIDERATION OF $1,045 per share", (1045.0, False)),
        ("receive&nbsp;&#36;12.25 per\nshare in cash", (12.25, False)),
        ("<td>$7.00</td> <td>per share in cash</td>", (7.0, False)),
        ("$20.00 per share in cash and 0.25 of a share of Parent", (20.0, True)),
        ("$9 in cash per share, and 1 Parent share", (9.0, True)),
        (
            "$45 per share in cash, without interest and less taxes, per share",
            (45.0, False),
        ),
        ("0.5 shares of Parent per share", None),
    ],
)
def test_cash_price_per_share(text: str, expected: tuple[float, bool] | None) -> None:
    assert edgar.cash_price_per_share(text) == expected


def test_parse_filings_builds_archive_urls() -> None:
    [filing] = edgar.parse_filings(_submissions(3, ("8-K", "2008-09-15", "1.03")))
    assert filing.items == ("1.03",)
    assert filing.url == (
        "https://www.sec.gov/Archives/edgar/data/3/000000000309000000/8-k.htm"
    )


def test_sec_fetch_spaces_requests_without_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    slept: list[float] = []
    headers: list[Any] = []

    class Response:
        content = b"ok"
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, **kwargs: Any) -> Response:
        headers.append(kwargs["headers"])
        return Response()

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(edgar.requests, "get", fake_get)
    fetch = edgar.sec_fetch("Test test@example.com", fake_sleep, lambda: now[0])
    assert fetch("a") == b"ok" and slept == []
    fetch("b")
    now[0] += 1.0
    fetch("c")
    assert slept == [pytest.approx(edgar.MIN_INTERVAL)]
    assert headers[0] == {"User-Agent": "Test test@example.com"}


def test_sec_fetch_retries_throttling_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    replies: list[Any] = [429, edgar.requests.ConnectionError("reset"), 200]

    class Response:
        content = b"ok"

        def __init__(self, status: int) -> None:
            self.status_code = status

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise edgar.requests.HTTPError(str(self.status_code))

    def fake_get(url: str, **kwargs: Any) -> Response:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(reply)

    monkeypatch.setattr(edgar.requests, "get", fake_get)
    ticks = itertools.count(step=100.0)  # never inside MIN_INTERVAL
    fetch = edgar.sec_fetch("UA", slept.append, lambda: next(ticks))
    assert fetch("a") == b"ok" and slept == list(edgar.RETRY_WAITS)
    replies[:] = [503, 503, 503]
    with pytest.raises(edgar.requests.HTTPError):
        fetch("b")


def _change(reason: str) -> wiki.WikiChange:
    return wiki.WikiChange(
        date="2010-03-01", added=None, removed="ACQ", removed_name="A", reason=reason
    )


def _filing(form: str, items: tuple[str, ...] = ()) -> edgar.Filing:
    return edgar.Filing(form=form, date="2010-01-04", items=items, url=form)


def test_merger_forms_alone_do_not_make_an_edgar_acquisition() -> None:
    acquirer = te.classify(INTERVALS[0], _change("Removed."), 1, [_filing("425")])
    assert (acquirer.event_type, acquirer.evidence) == ("unknown", "none")
    target = te.classify(
        INTERVALS[0], _change("Removed."), 1, [_filing("425"), _filing("25")]
    )
    assert (target.event_type, target.source_filing) == ("acquisition", "425")


def test_delisting_is_consistent_with_a_wikipedia_exit() -> None:
    event = te.classify(INTERVALS[0], _change("Acquired."), 1, [_filing("15-12B")])
    assert (event.event_type, event.evidence, event.note) == (
        "acquisition",
        "wikipedia",
        "",
    )
    trading = te.classify(INTERVALS[0], _change("Market cap."), 1, [_filing("25")])
    assert trading.note == "conflict: edgar suggests delisting"


@pytest.mark.parametrize("form", ["DEFM14C", "DEFM14A/A", "SC TO-T/A"])
def test_merger_form_variants_confirm_acquisition(form: str) -> None:
    event = te.classify(INTERVALS[0], _change("Acquired."), 1, [_filing(form)])
    assert event.evidence == "wikipedia+edgar"


def test_older_history_pages_are_read_for_the_window() -> None:
    recent = json.loads(_submissions(1, ("10-K", "2020-02-01", "")))
    recent["filings"]["files"] = [
        {"name": "p1.json", "filingFrom": "2008-01-01", "filingTo": "2012-12-31"},
        {"name": "p0.json", "filingFrom": "1999-01-01", "filingTo": "2007-12-31"},
    ]
    page = json.loads(_submissions(1, ("DEFM14A", "2009-12-01", None)))  # type: ignore[arg-type]
    page = page["filings"]["recent"]
    sec = _fetcher(
        {
            edgar.SUBMISSIONS_URL.format(cik=1): json.dumps(recent).encode(),
            edgar.SUBMISSIONS_PAGE_URL.format(name="p1.json"): json.dumps(
                page
            ).encode(),
        }
    )
    filings = te._all_filings(1, INTERVALS[0], sec)  # p0 is outside: never fetched
    assert [(f.form, f.items) for f in filings] == [("10-K", ()), ("DEFM14A", ())]
    assert filings[1].url.startswith("https://www.sec.gov/Archives/edgar/data/1/")


def test_large_name_bucket_is_ambiguous_without_requests() -> None:
    def no_network(url: str) -> bytes:
        raise AssertionError(f"request made: {url}")

    lookup = {"ACME": set(range(1, te.MAX_CIKS + 2))}
    assert te._company_filings(INTERVALS[0], "Acme Inc", lookup, no_network) == (
        None,
        None,
        "wikipedia only: ambiguous name in EDGAR",
    )
    assert te._company_filings(INTERVALS[0], "Inc.", lookup, no_network)[2] == (
        "wikipedia only: name not found in EDGAR"
    )


def test_normalise_name_folds_accents() -> None:
    assert edgar.normalise_name("Nestlé & Co") == "NESTLE AND"


def test_cli_rejects_bad_limit_and_missing_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "INDEX_MEMBERSHIP_DB", tmp_path / "m.db")
    sec = _fetcher({})
    with pytest.raises(SystemExit):
        cli.main(["sp500", "--limit", "0"], fetch=sec, sec=sec)
    with pytest.raises(SystemExit, match="no sp500 membership import"):
        cli.main(["sp500"], fetch=sec, sec=sec)
