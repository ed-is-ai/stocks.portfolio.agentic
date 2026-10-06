"""Cross-check S&P 500 membership against Wikipedia (#68). Report only.

Wikipedia keeps today's constituents on "List of S&P 500 companies"
(table ``id="constituents"``) and the "Selected changes" table on
"Historical components of the S&P 500" (table ``id="changes"``). Parsed with
the stdlib ``html.parser`` so no HTML dependency is needed.
"""

import re
from datetime import datetime
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict

from app.services.index_membership.sp500_import import Fetch

CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
_FOOTNOTE = re.compile(r"\[[^\]]*\]")


class WikiChange(BaseModel):
    """One row of Wikipedia's "Selected changes" table."""

    model_config = ConfigDict(frozen=True)

    date: str
    added: str | None
    removed: str | None
    reason: str


class WikipediaDiff(BaseModel):
    """Differences between the dataset's latest members and Wikipedia."""

    model_config = ConfigDict(frozen=True)

    only_in_dataset: list[str]
    only_in_wikipedia: list[str]
    changes_after: list[WikiChange]


def fetch_wikipedia_diff(
    fetch: Fetch, dataset_members: frozenset[str], dataset_last_date: str
) -> WikipediaDiff:
    """Fetch both Wikipedia pages and diff them against the dataset."""
    return wikipedia_diff(
        fetch(CONSTITUENTS_URL).decode(),
        fetch(CHANGES_URL).decode(),
        dataset_members,
        dataset_last_date,
    )


def wikipedia_diff(
    constituents_html: str,
    changes_html: str,
    dataset_members: frozenset[str],
    dataset_last_date: str,
) -> WikipediaDiff:
    """Compare current constituents and list changes after the dataset ends.

    A change dated on ``dataset_last_date`` is already in the dataset's last
    snapshot, so only strictly later changes are reported.
    """
    wikipedia = {row[0] for row in table_rows(constituents_html, "constituents")}
    changes = [_change(row) for row in table_rows(changes_html, "changes")]
    return WikipediaDiff(
        only_in_dataset=sorted(dataset_members - wikipedia),
        only_in_wikipedia=sorted(wikipedia - dataset_members),
        changes_after=[c for c in changes if c.date > dataset_last_date],
    )


def table_rows(html: str, table_id: str) -> list[list[str]]:
    """Return the ``<td>`` texts of each data row of the table ``table_id``."""
    parser = _TableParser(table_id)
    parser.feed(html)
    return [row for row in parser.rows if row]


def _change(row: list[str]) -> WikiChange:
    """Map ``date, added, name, removed, name, reason[, refs]`` cells."""
    as_of = datetime.strptime(row[0], "%B %d, %Y").date().isoformat()
    return WikiChange(
        date=as_of, added=row[1] or None, removed=row[3] or None, reason=row[5]
    )


class _TableParser(HTMLParser):
    """Collect the cell texts of one table, ignoring header and nested cells."""

    def __init__(self, table_id: str) -> None:
        super().__init__()
        self._table_id = table_id
        self._depth = 0  # >0 while inside the target table
        self._cell: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and (self._depth or dict(attrs).get("id") == self._table_id):
            self._depth += 1
        elif self._depth == 1 and tag == "tr":
            self.rows.append([])
        elif self._depth == 1 and tag == "td":
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
