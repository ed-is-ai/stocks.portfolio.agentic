"""Link S&P 500 members to the WIKI price archive and check it (#70).

An S&P ticker maps to the WIKI ticker of the same name with ``.`` -> ``_``
(``BRK.B`` -> ``BRK_B``), unless a reviewed override in
``config/wiki_ticker_overrides.csv`` (``sp_ticker,wiki_ticker,start_date,
end_date``, dates inclusive) names another one for that date (e.g. AABA was
YHOO). A link counts for a month only where the WIKI history overlaps it.
"""

import csv
import json
import statistics
import zlib
from datetime import date
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.core.config import ROOT_DIR
from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership.coverage import (
    Covers,
    MonthCoverage,
    Spans,
    read_only,
)
from app.services.index_membership.sp500_import import INDEX_ID

OVERRIDES_PATH = ROOT_DIR / "config" / "wiki_ticker_overrides.csv"
_OVERRIDE_FIELDS = ["sp_ticker", "wiki_ticker", "start_date", "end_date"]
#: A WIKI close agrees with yfinance when within this share of it.
AGREE_WITHIN = 0.01
#: Tickers whose median gap exceeds this share are listed as outliers.
OUTLIER_GAP = 0.05

#: Rows of the latest-ending yfinance revision with a chunk for one year.
_YEAR_CHUNK = """
SELECT c.compressed_payload
FROM historical_price_revisions AS r
JOIN historical_price_v2_revisions AS v ON v.data_revision = r.data_revision
JOIN historical_price_v2_revision_chunks AS rc ON rc.revision_id = v.revision_id
JOIN historical_price_v2_chunks AS c ON c.chunk_digest = rc.chunk_digest
WHERE r.provider = 'yfinance' AND r.requested_symbol = ?
  AND rc.chunk_kind = 'rows' AND rc.chunk_year = ?
ORDER BY r.end_date DESC, v.revision_id DESC LIMIT 1
"""


class Override(BaseModel):
    """A reviewed S&P -> WIKI ticker link for an inclusive date range."""

    model_config = ConfigDict(frozen=True)

    sp_ticker: str
    wiki_ticker: str
    start_date: str
    end_date: str


class Validation(BaseModel):
    """How WIKI closes agree with yfinance closes in one sample year."""

    model_config = ConfigDict(frozen=True)

    year: int
    tickers: int
    dates: int
    agreeing: int
    #: ``(wiki_ticker, median relative gap)``, largest gap first.
    outliers: list[tuple[str, float]]
    #: ``(wiki_ticker, factor)`` where yfinance's split adjustment since the
    #: year (WIKI close / yfinance close) is not 1, by ticker.
    factors: list[tuple[str, float]]


class Unmatched(BaseModel):
    """A membership interval with months no source prices."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    start_date: str
    end_date: str | None
    missing_months: int


def load_overrides(path: Path) -> list[Override]:
    """Read the override CSV.

    Raises ``ValueError`` when the file is missing, or naming the line of a
    malformed row (wrong header or field count, blank ticker, non-ISO date,
    start after end, range overlapping an earlier row of the same ticker).
    """
    if not path.is_file():
        raise ValueError(f"overrides file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    header = [field.strip() for field in rows[0]] if rows else []
    if header != _OVERRIDE_FIELDS:
        fields = ",".join(_OVERRIDE_FIELDS)
        raise ValueError(f"{path} line 1: header must be {fields}")
    overrides: list[Override] = []
    for line, row in enumerate(rows[1:], 2):
        if not row:
            continue
        found = _override(row, path, line)
        if any(_overlaps(found, earlier) for earlier in overrides):
            raise ValueError(f"{path} line {line}: overlaps an earlier override")
        overrides.append(found)
    return overrides


def _overlaps(a: Override, b: Override) -> bool:
    return (
        a.sp_ticker == b.sp_ticker
        and a.start_date <= b.end_date
        and b.start_date <= a.end_date
    )


def _override(row: list[str], path: Path, line: int) -> Override:
    try:
        sp, wiki, start, end = (field.strip() for field in row)
        iso = [date.fromisoformat(d).isoformat() for d in (start, end)]
        if not sp or not wiki or iso != [start, end] or start > end:
            raise ValueError("blank ticker, non-ISO date or start after end")
    except ValueError as exc:
        raise ValueError(f"{path} line {line}: malformed override {row}") from exc
    return Override(sp_ticker=sp, wiki_ticker=wiki, start_date=start, end_date=end)


def wiki_ticker(sp_ticker: str, as_of: str, overrides: list[Override]) -> str:
    """Return the WIKI ticker of ``sp_ticker`` on ``as_of``."""
    for o in overrides:
        if o.sp_ticker == sp_ticker and o.start_date <= as_of <= o.end_date:
            return o.wiki_ticker
    return sp_ticker.replace(".", "_")


def wiki_covers(
    spans: Spans, overrides: list[Override], sp_ticker: str, as_of: str
) -> bool:
    """True when WIKI history of ``sp_ticker`` overlaps the month of ``as_of``."""
    span = spans.get(wiki_ticker(sp_ticker, as_of, overrides))
    return span is not None and span[0] <= as_of and span[1] >= f"{as_of[:7]}-01"


def wiki_coverage(wiki_db: Path, overrides_path: Path) -> Covers | None:
    """Return WIKI coverage for ``coverage_report``, or ``None`` when
    ``wiki_db`` does not exist (opened read-only).

    Raises ``ValueError`` on a malformed override file and
    ``sqlite3.OperationalError`` on a database without WIKI tables.
    """
    if not wiki_db.exists():
        return None
    overrides = load_overrides(overrides_path)
    spans = WikiPriceRepository(lambda: read_only(wiki_db)).ticker_spans()
    return partial(wiki_covers, spans, overrides)


def validate(wiki: WikiPriceRepository, price_db: Path, year: int) -> Validation:
    """Compare WIKI closes with yfinance closes on identical dates in ``year``.

    yfinance closes are split-adjusted to today while WIKI's are as traded, so
    WIKI closes are first put on the year-end share basis with WIKI's own
    split ratios, then divided by the ticker's median WIKI/yfinance ratio (the
    split factor since ``year``) before the gaps are measured. Each WIKI ticker
    is looked up as yfinance symbol (``_`` -> ``-``) in its latest-ending
    revision with a ``rows`` chunk for ``year``; tickers with no common dates
    are skipped. The price cache is opened read-only.
    """
    conn = read_only(price_db)
    try:
        gaps: dict[str, list[float]] = {}
        factors: dict[str, float] = {}
        for ticker in wiki.ticker_spans():
            row = conn.execute(_YEAR_CHUNK, (ticker.replace("_", "-"), year))
            payload = row.fetchone()
            if payload is None:
                continue
            wiki_closes = _split_adjusted(wiki.closes(ticker, year))
            factor, found = _gaps(wiki_closes, _yf_closes(payload[0]))
            if found:
                gaps[ticker], factors[ticker] = found, factor
    finally:
        conn.close()
    medians = {t: statistics.median(g) for t, g in gaps.items()}
    return Validation(
        year=year,
        tickers=len(gaps),
        dates=sum(len(g) for g in gaps.values()),
        agreeing=sum(gap <= AGREE_WITHIN for g in gaps.values() for gap in g),
        outliers=sorted(
            ((t, m) for t, m in medians.items() if m > OUTLIER_GAP),
            key=lambda item: -item[1],
        ),
        factors=sorted((t, f) for t, f in factors.items() if abs(f - 1) > AGREE_WITHIN),
    )


def _yf_closes(compressed: bytes) -> dict[str, float]:
    items = json.loads(zlib.decompress(compressed))["items"]
    return {
        i["session"]: float.fromhex(i["close"])
        for i in items
        if i.get("close") is not None
    }


def _split_adjusted(rows: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Put WIKI's as-traded closes on the share basis of the last date, using
    WIKI's split ratios (a ratio on a date applies to the days before it)."""
    factor, adjusted = 1.0, {}
    for day in sorted(rows, reverse=True):
        close, ratio = rows[day]
        adjusted[day] = close / factor
        factor *= ratio or 1.0
    return adjusted


def _gaps(wiki: dict[str, float], yf: dict[str, float]) -> tuple[float, list[float]]:
    """Return the median WIKI/yfinance ratio and each common date's relative
    gap after dividing WIKI's close by it."""
    common = [d for d in wiki.keys() & yf.keys() if yf[d] and wiki[d]]
    if not common:
        return 1.0, []
    factor = statistics.median(wiki[d] / yf[d] for d in common)
    return factor, [abs(wiki[d] / factor - yf[d]) / yf[d] for d in common]


def unmatched(
    repo: IndexMembershipRepository, rows: list[MonthCoverage]
) -> list[Unmatched]:
    """Return the membership intervals of ``rows``' missing tickers with how
    many of their months stay missing, most months first."""
    intervals = {
        t: repo.intervals_for(INDEX_ID, t) for r in rows for t in r.missing_tickers
    }
    counts: dict[MembershipInterval, int] = {}
    for row in rows:
        for ticker in row.missing_tickers:
            spell = next(
                (
                    i
                    for i in intervals[ticker]
                    if i.start_date <= row.as_of
                    and (i.end_date is None or i.end_date > row.as_of)
                ),
                None,
            )
            if spell is not None:
                counts[spell] = counts.get(spell, 0) + 1
    result = [
        Unmatched(
            ticker=i.ticker,
            start_date=i.start_date,
            end_date=i.end_date,
            missing_months=n,
        )
        for i, n in counts.items()
    ]
    return sorted(result, key=lambda u: (-u.missing_months, u.ticker, u.start_date))
