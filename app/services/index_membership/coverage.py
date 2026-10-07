"""Per-month price coverage of the point-in-time S&P 500 (#74).

For each month the roster on ``AS_OF_RULE`` (the month's last calendar day) is
split into members whose yfinance history spans the month (``priced``), those
whose ticker was reused (``suspect``: the prices may be another company's),
uncovered members still in the index today (``not_cached``: yfinance can
supply them, the app just has not fetched them), leavers only the WIKI
archive covers (``wiki``, #70) and the rest (``missing``: left the index,
the survivorship gap other providers must fill).

The price cache is opened strictly read-only. The cache keeps short-window
revisions too, so the first session comes from each symbol's earliest-starting
yfinance revision and the last from its latest-ending one; only those two
``rows`` chunks are decoded.
"""

import calendar
import json
import sqlite3
import zlib
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.repositories.index_membership_repo import (
    Confidence,
    IndexMembershipRepository,
    Roster,
)
from app.services.index_membership.sp500_import import INDEX_ID

#: Which day of a month its roster is taken on.
AS_OF_RULE = "last calendar day"

#: yfinance symbol -> (first, last) session with a non-null close.
Spans = dict[str, tuple[str, str]]
#: ``(sp_ticker, as_of)`` -> whether another source prices it in that month.
Covers = Callable[[str, str], bool]

#: Only columns stored before the large JSON ones, so the scan stays cheap.
_REVISIONS = """
SELECT r.requested_symbol, r.start_date, r.end_date, r.data_revision, v.revision_id
FROM historical_price_revisions AS r
JOIN historical_price_v2_revisions AS v ON v.data_revision = r.data_revision
WHERE r.provider = 'yfinance'
"""

_ROW_CHUNKS = """
SELECT chunk_digest FROM historical_price_v2_revision_chunks
WHERE revision_id = ? AND chunk_kind = 'rows' ORDER BY chunk_year
"""
_PAYLOAD = (
    "SELECT compressed_payload FROM historical_price_v2_chunks WHERE chunk_digest = ?"
)


class MonthCoverage(BaseModel):
    """How many of one month's index members the price cache can price."""

    model_config = ConfigDict(frozen=True)

    month: str
    as_of: str
    members: int
    priced: int
    suspect: int
    wiki: int
    not_cached: int
    missing: int
    confidence: Confidence
    stale: bool
    missing_tickers: list[str]
    suspect_tickers: list[str]
    wiki_tickers: list[str]
    not_cached_tickers: list[str]


def read_only(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file read-only (never creates or writes it)."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def month_as_of(month: str) -> str:
    """Return the ``AS_OF_RULE`` date of ``YYYY-MM`` (its last calendar day)."""
    year, mon = (int(part) for part in month.split("-"))
    return f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def months_between(start: str, end: str) -> list[str]:
    """Return every ``YYYY-MM`` from ``start`` to ``end`` inclusive."""
    first = int(start[:4]) * 12 + int(start[5:]) - 1
    last = int(end[:4]) * 12 + int(end[5:]) - 1
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(first, last + 1)]


def price_spans(price_db: Path) -> Spans:
    """Return each yfinance symbol's first and last session with a close.

    The first session is read from the earliest-starting revision and the last
    from the latest-ending one (ties: newest ``revision_id``), falling back to
    the next revision when one has no close. Only edge chunks are decoded,
    moving inward past chunks without a close.
    """
    conn = read_only(price_db)
    try:
        revisions: dict[str, list[tuple[str, str, int]]] = {}
        for symbol, start, end, _, revision_id in conn.execute(_REVISIONS):
            revisions.setdefault(symbol, []).append((start, end, revision_id))
        spans: Spans = {}
        for symbol, found in revisions.items():
            by_start = sorted(found, key=lambda r: (r[0], -r[2]))
            by_end = sorted(found, key=lambda r: (r[1], r[2]), reverse=True)
            first = _edge_session(conn, [r[2] for r in by_start], first=True)
            last = _edge_session(conn, [r[2] for r in by_end], first=False)
            if first is not None and last is not None:
                spans[symbol] = (first, last)
        return spans
    finally:
        conn.close()


def _edge_session(
    conn: sqlite3.Connection, revision_ids: list[int], first: bool
) -> str | None:
    """Return the first (or last) closed session of the first revision in
    ``revision_ids`` that has one."""
    for revision_id in revision_ids:
        digests = _digests(conn, revision_id)
        session = (
            _first_session(conn, digests, min)
            if first
            else _first_session(conn, reversed(digests), max)
        )
        if session is not None:
            return session
    return None


def _digests(conn: sqlite3.Connection, revision_id: int) -> list[str]:
    return [r[0] for r in conn.execute(_ROW_CHUNKS, (revision_id,))]


def last_complete_month(spans: Spans, today: date) -> str:
    """Return the last month the cache covers fully: the latest session's month
    if that session is on/after the month's last weekday, else the month before
    (``today`` stands in for the latest session when ``spans`` is empty)."""
    latest = max((last for _, last in spans.values()), default=today.isoformat())
    month = latest[:7]
    last_weekday = date.fromisoformat(month_as_of(month))
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    if latest >= last_weekday.isoformat():
        return month
    return (date.fromisoformat(f"{month}-01") - timedelta(days=1)).isoformat()[:7]


def _first_session(
    conn: sqlite3.Connection,
    digests: Iterable[str],
    pick: Callable[[list[str]], str],
) -> str | None:
    """Return ``pick`` of the closed sessions in the first chunk that has any,
    loading chunk payloads one at a time (a digest without a payload is
    skipped)."""
    for digest in digests:
        row = conn.execute(_PAYLOAD, (digest,)).fetchone()
        if row is None:
            continue
        items = json.loads(zlib.decompress(row[0]))["items"]
        sessions = [i["session"] for i in items if i.get("close") is not None]
        if sessions:
            return pick(sessions)
    return None


def month_coverage(
    roster: Roster,
    spans: Spans,
    reused: set[str],
    current: set[str],
    wiki: Covers | None = None,
) -> MonthCoverage:
    """Split ``roster``'s members into priced, suspect, not cached, wiki and
    missing.

    A member is covered when its yfinance history (ticker with ``.`` -> ``-``)
    starts on/before the month's end and ends on/after its start; a covered
    ticker in ``reused`` is suspect instead of priced. An uncovered ticker in
    ``current`` (still a member today, and not reused) is not cached: yfinance
    can supply it, so WIKI does not hide it. Any other uncovered member that
    ``wiki`` covers is wiki (suspect if reused).
    """
    # ponytail: gaps inside a history are not checked; scan every chunk if needed.
    month_start = f"{roster.as_of[:7]}-01"
    missing: list[str] = []
    suspect: list[str] = []
    not_cached: list[str] = []
    in_wiki: list[str] = []
    for member in roster.members:
        span = spans.get(member.ticker.replace(".", "-"))
        if span is None or span[0] > roster.as_of or span[1] < month_start:
            reused_ticker = member.ticker in reused
            if member.ticker in current and not reused_ticker:
                not_cached.append(member.ticker)
            elif wiki is not None and wiki(member.ticker, roster.as_of):
                (suspect if reused_ticker else in_wiki).append(member.ticker)
            else:
                missing.append(member.ticker)
        elif member.ticker in reused:
            suspect.append(member.ticker)
    return MonthCoverage(
        month=roster.as_of[:7],
        as_of=roster.as_of,
        members=len(roster.members),
        priced=len(roster.members)
        - len(missing)
        - len(suspect)
        - len(in_wiki)
        - len(not_cached),
        suspect=len(suspect),
        wiki=len(in_wiki),
        not_cached=len(not_cached),
        missing=len(missing),
        confidence=roster.confidence,
        stale=roster.stale,
        missing_tickers=missing,
        suspect_tickers=suspect,
        wiki_tickers=in_wiki,
        not_cached_tickers=not_cached,
    )


def coverage_report(
    repo: IndexMembershipRepository,
    spans: Spans,
    start_month: str,
    end_month: str,
    wiki: Covers | None = None,
) -> list[MonthCoverage]:
    """Return the S&P 500 coverage of every month in the range (``wiki``
    prices members yfinance does not, see ``month_coverage``).

    Raises ``ValueError`` if the S&P 500 membership was never imported.
    """
    rosters: list[Roster] = []
    for month in months_between(start_month, end_month):
        roster = repo.roster_on(INDEX_ID, month_as_of(month))
        if roster is None:
            raise ValueError(f"no {INDEX_ID} membership imported")
        rosters.append(roster)
    tickers = {m.ticker for r in rosters for m in r.members}
    intervals = {t: repo.intervals_for(INDEX_ID, t) for t in tickers}
    reused = {t for t, spells in intervals.items() if len(spells) > 1}
    current = {t for t, spells in intervals.items() if spells[-1].end_date is None}
    return [month_coverage(r, spans, reused, current, wiki) for r in rosters]
