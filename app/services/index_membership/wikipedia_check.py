"""Cross-check S&P 500 membership against Wikipedia (#68). Report only.

Wikipedia keeps today's constituents on "List of S&P 500 companies"
(table ``id="constituents"``) and the "Selected changes" table on
"Historical components of the S&P 500" (table ``id="changes"``). Parsed with
the stdlib ``html.parser`` so no HTML dependency is needed.

Changes inside the dataset's range are checked against the derived intervals:
an added ticker needs an interval starting, and a removed ticker one ending,
within ``MATCH_WINDOW`` of Wikipedia's effective date (the two sources date a
change differently, so an exact match is not expected).
"""

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict

from app.repositories.index_membership_repo import MembershipInterval
from app.services.index_membership.sp500_import import Fetch

CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
_FOOTNOTE = re.compile(r"\[[^\]]*\]")
# ponytail: calendar days approximate ~5 trading days; use a session calendar
# if holidays make this noisy.
MATCH_WINDOW = timedelta(days=7)

#: ``(date, "added" | "removed", ticker)`` of a change the dataset lacks.
Unmatched = tuple[str, str, str]


class WikiChange(BaseModel):
    """One row of Wikipedia's "Selected changes" table."""

    model_config = ConfigDict(frozen=True)

    date: str
    added: str | None
    removed: str | None
    #: The removed company's name; the only name source for old tickers.
    removed_name: str | None
    reason: str


class WikipediaDiff(BaseModel):
    """Differences between the dataset and Wikipedia."""

    model_config = ConfigDict(frozen=True)

    only_in_dataset: list[str]
    only_in_wikipedia: list[str]
    changes_after: list[WikiChange]
    unmatched_changes: list[Unmatched]
    skipped_change_rows: int


def fetch_wikipedia_diff(
    fetch: Fetch,
    intervals: list[MembershipInterval],
    first_date: str,
    last_date: str,
) -> WikipediaDiff:
    """Fetch both Wikipedia pages and diff them against the dataset."""
    return wikipedia_diff(
        fetch(CONSTITUENTS_URL).decode(),
        fetch(CHANGES_URL).decode(),
        intervals,
        first_date,
        last_date,
    )


def wikipedia_diff(
    constituents_html: str,
    changes_html: str,
    intervals: list[MembershipInterval],
    first_date: str,
    last_date: str,
) -> WikipediaDiff:
    """Compare current constituents, check changes within the dataset's range
    and list changes after it.

    A change dated on ``last_date`` is already in the dataset's last snapshot,
    so only strictly later changes are "after"; one on ``first_date`` predates
    the first snapshot's diff, so only strictly later changes are checked.
    Raises ``ValueError`` if either table is missing.
    """
    wikipedia = {row[0] for row in table_rows(constituents_html, "constituents")}
    parsed = [parse_change(row) for row in table_rows(changes_html, "changes")]
    changes = [c for c in parsed if c is not None]
    current = {i.ticker for i in intervals if i.end_date is None}
    starts = {(i.ticker, i.start_date) for i in intervals}
    ends = {(i.ticker, i.end_date) for i in intervals if i.end_date}
    return WikipediaDiff(
        only_in_dataset=sorted(current - wikipedia),
        only_in_wikipedia=sorted(wikipedia - current),
        changes_after=[c for c in changes if c.date > last_date],
        unmatched_changes=sorted(
            u
            for c in changes
            if first_date < c.date <= last_date
            for u in _unmatched(c, starts, ends)
        ),
        skipped_change_rows=len(parsed) - len(changes),
    )


def _unmatched(
    change: WikiChange, starts: set[tuple[str, str]], ends: set[tuple[str, str]]
) -> list[Unmatched]:
    """Return the sides of ``change`` with no interval edge near its date."""
    sides = [("added", change.added, starts), ("removed", change.removed, ends)]
    return [
        (change.date, side, ticker)
        for side, ticker, edges in sides
        if ticker and not any((ticker, d) in edges for d in _near(change.date))
    ]


def _near(as_of: str) -> list[str]:
    """Return the ISO dates within ``MATCH_WINDOW`` of ``as_of``."""
    day = date.fromisoformat(as_of)
    days = MATCH_WINDOW.days
    return [(day + timedelta(days=n)).isoformat() for n in range(-days, days + 1)]


def table_rows(html: str, table_id: str) -> list[list[str]]:
    """Return the ``<td>`` texts of each data row of the table ``table_id``.

    Raises ``ValueError`` if the page has no such table.
    """
    parser = _TableParser(table_id)
    parser.feed(html)
    if not parser.found:
        raise ValueError(f"Wikipedia table {table_id!r} not found")
    return [row for row in parser.rows if row]


def parse_change(row: list[str]) -> WikiChange | None:
    """Map ``date, added, name, removed, name, reason[, refs]`` cells; ``None``
    for a row in another shape (e.g. one sharing a date cell via rowspan)."""
    if len(row) < 6:
        return None
    try:
        as_of = datetime.strptime(row[0], "%B %d, %Y").date().isoformat()
    except ValueError:
        return None
    return WikiChange(
        date=as_of,
        added=row[1] or None,
        removed=row[3] or None,
        removed_name=row[4] or None,
        reason=row[5],
    )


class _TableParser(HTMLParser):
    """Collect the cell texts of one table, ignoring header and nested cells."""

    def __init__(self, table_id: str) -> None:
        super().__init__()
        self._table_id = table_id
        self._depth = 0  # >0 while inside the target table
        self._cell: list[str] | None = None
        self.found = False
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and (self._depth or dict(attrs).get("id") == self._table_id):
            self._depth += 1
            self.found = True
        elif self._depth == 1 and tag == "tr":
            self.rows.append([])
        elif self._depth == 1 and tag == "td" and self.rows:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._depth:
            self._depth -= 1
        elif tag == "td" and self._cell is not None and self._depth == 1:
            text = _FOOTNOTE.sub("", "".join(self._cell))
            self.rows[-1].append(" ".join(text.split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and self._depth == 1:
            self._cell.append(data)
