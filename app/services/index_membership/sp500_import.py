"""Import point-in-time S&P 500 membership from fja05680/sp500 (#68).

The point-in-time file lists every member on each change date since 1996.
Membership intervals are derived from it (``[start, end)``: ``end`` is the
first change date the ticker is absent) and cross-checked against the
dataset's own ``sp500_ticker_start_end.csv``; differences are reported, never
merged.

Identity: tickers are as-of-date strings. A rename is a removal plus an
addition, and a ticker can be reused by a different company (``AAL`` 1996-97
vs 2015+). Each contiguous interval is therefore its own security, keyed
``f"{ticker}@{start_date}"``; nothing links intervals across gaps or renames.
Mapping keys to provider securities is left to #70/#71.

Confidence: before 2001-01-16 the dataset has fewer than ~494 members
(dataset README), so dates, and intervals starting, before then are ``low``.
"""

import csv
import hashlib
import io
import json
import logging
from collections.abc import Callable
from datetime import date
from urllib.parse import quote

import requests
from pydantic import BaseModel, ConfigDict

from app.repositories.index_membership_repo import (
    Confidence,
    IndexMembershipRepository,
    MembershipInterval,
)

logger = logging.getLogger(__name__)

INDEX_ID = "sp500"
SOURCE = "fja05680/sp500"
BRANCH = "master"
COMPONENTS_FILE = "S&P 500 Historical Components & Changes (Updated).csv"
START_END_FILE = "sp500_ticker_start_end.csv"
COMMIT_API_URL = f"https://api.github.com/repos/{SOURCE}/commits/{BRANCH}"
LOW_CONFIDENCE_BEFORE = "2001-01-16"

#: Returns the raw bytes at a URL; injectable so tests never hit the network.
Fetch = Callable[[str], bytes]

#: ``(date, members)`` per change date, oldest first.
Snapshots = list[tuple[str, frozenset[str]]]

#: ``(ticker, start_date, end_date)`` with ``end_date`` ``None`` if current.
Span = tuple[str, str, str | None]


class ImportSummary(BaseModel):
    """What an import (or dry run) found and recorded."""

    model_config = ConfigDict(frozen=True)

    source_ref: str
    source_digest: str
    first_date: str
    last_date: str
    snapshot_count: int
    interval_count: int
    ticker_count: int
    low_confidence_intervals: int
    latest_member_count: int
    latest_members: frozenset[str]
    only_in_components: list[Span]
    only_in_start_end: list[Span]
    import_id: int | None
    created: bool


def http_fetch(url: str) -> bytes:
    """Fetch ``url`` over HTTPS and return its body."""
    response = requests.get(
        url, timeout=30, headers={"User-Agent": "stocks-portfolio-agentic/1.0"}
    )
    response.raise_for_status()
    return response.content


def import_sp500(fetch: Fetch, repo: IndexMembershipRepository | None) -> ImportSummary:
    """Fetch, parse and cross-check the dataset; store it unless ``repo`` is
    ``None`` (dry run). Re-importing an unchanged file records nothing."""
    ref = resolve_commit(fetch)
    components = fetch(raw_url(ref, COMPONENTS_FILE))
    start_end = parse_start_end(fetch(raw_url(ref, START_END_FILE)).decode())
    snapshots = parse_components(components.decode())
    intervals = derive_intervals(snapshots)
    only_in_components, only_in_start_end = cross_check(intervals, start_end)
    digest = hashlib.sha256(components).hexdigest()
    source_ref = f"{SOURCE}@{ref}"
    import_id, created = None, False
    if repo is not None:
        import_id, created = repo.record_import(
            index_id=INDEX_ID,
            source=SOURCE,
            source_ref=source_ref,
            source_digest=digest,
            first_date=snapshots[0][0],
            last_date=snapshots[-1][0],
            snapshot_count=len(snapshots),
            intervals=intervals,
        )
    return ImportSummary(
        source_ref=source_ref,
        source_digest=digest,
        first_date=snapshots[0][0],
        last_date=snapshots[-1][0],
        snapshot_count=len(snapshots),
        interval_count=len(intervals),
        ticker_count=len({i.ticker for i in intervals}),
        low_confidence_intervals=sum(i.confidence == "low" for i in intervals),
        latest_member_count=len(snapshots[-1][1]),
        latest_members=snapshots[-1][1],
        only_in_components=only_in_components,
        only_in_start_end=only_in_start_end,
        import_id=import_id,
        created=created,
    )


def resolve_commit(fetch: Fetch) -> str:
    """Return the branch head's commit SHA, or the branch name if the GitHub
    API is unavailable (the import is then recorded as unpinned)."""
    try:
        return str(json.loads(fetch(COMMIT_API_URL))["sha"])
    except Exception as exc:  # noqa: BLE001 -- any failure means "unpinned"
        logger.warning("could not pin %s to a commit: %s", SOURCE, exc)
        return BRANCH


def raw_url(ref: str, file_name: str) -> str:
    """Return the raw.githubusercontent.com URL of a dataset file at ``ref``."""
    return f"https://raw.githubusercontent.com/{SOURCE}/{ref}/{quote(file_name)}"


def parse_components(text: str) -> Snapshots:
    """Parse the point-in-time file (``date,tickers``) oldest first."""
    rows = csv.DictReader(io.StringIO(text))
    snapshots = [
        (
            date.fromisoformat(row["date"].strip()).isoformat(),
            frozenset(t.strip() for t in row["tickers"].split(",") if t.strip()),
        )
        for row in rows
    ]
    if not snapshots:
        raise ValueError("point-in-time membership file has no rows")
    return sorted(snapshots)


def parse_start_end(text: str) -> set[Span]:
    """Parse ``sp500_ticker_start_end.csv`` (``ticker,start_date,end_date``)."""
    return {
        (
            row["ticker"].strip(),
            row["start_date"].strip(),
            row["end_date"].strip() or None,
        )
        for row in csv.DictReader(io.StringIO(text))
    }


def derive_intervals(snapshots: Snapshots) -> list[MembershipInterval]:
    """Turn change-date snapshots into ``[start, end)`` intervals per ticker."""
    open_since: dict[str, str] = {}
    spans: list[Span] = []
    for as_of, members in snapshots:
        for ticker in open_since.keys() - members:
            spans.append((ticker, open_since.pop(ticker), as_of))
        for ticker in members - open_since.keys():
            open_since[ticker] = as_of
    spans += [(ticker, start, None) for ticker, start in open_since.items()]
    return [
        MembershipInterval(
            ticker=ticker,
            start_date=start,
            end_date=end,
            security_key=f"{ticker}@{start}",
            confidence=confidence_on(start),
        )
        for ticker, start, end in sorted(spans)
    ]


def confidence_on(as_of: str) -> Confidence:
    """Return the dataset's confidence in membership on ``as_of``."""
    return "low" if as_of < LOW_CONFIDENCE_BEFORE else "normal"


def member_counts(snapshots: Snapshots) -> dict[str, int]:
    """Return the number of members on each change date."""
    return {as_of: len(members) for as_of, members in snapshots}


def cross_check(
    intervals: list[MembershipInterval], start_end: set[Span]
) -> tuple[list[Span], list[Span]]:
    """Return spans only in the derived intervals and only in the start/end
    file, each sorted."""
    derived = {(i.ticker, i.start_date, i.end_date) for i in intervals}
    return sorted(derived - start_end, key=_span_key), sorted(
        start_end - derived, key=_span_key
    )


def _span_key(span: Span) -> tuple[str, str]:
    return span[0], span[1]
