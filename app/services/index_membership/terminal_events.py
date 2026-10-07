"""Classify why each S&P 500 membership interval ended (#73). Report only.

For every interval of the newest ``sp500`` import ending on/after
``SINCE``: find Wikipedia's "Selected changes" removal of that ticker within
``MATCH_WINDOW`` of the interval end, classify its reason text, then confirm
or fill the class from the removed company's EDGAR filings in
``[exit - 18 months, exit + 30 days]``. The company is found by its Wikipedia
name, never by bare ticker (tickers are reused). For a confirmed acquisition
the most recent merger filing before the exit is read for a cash price.

Evidence order: the Wikipedia class wins; EDGAR confirms it or, when there
is none, supplies it. A disagreement keeps Wikipedia's class with a note. A
delisting filing is consistent with an acquisition or bankruptcy, not a
conflict. On EDGAR alone, merger filings count as an acquisition only with a
delisting filing too, since an acquirer files merger forms as well.

A removal matches Wikipedia when the removed ticker is the interval's, its
class-share root (``TMC.A`` -> ``TMC``), or, for a bankruptcy-suffixed ticker,
its prefix (``LEHMQ`` -> ``LEH``); never by date alone, since the table is
"selected" changes and many removals are missing from it.

Still unknown afterwards, a ticker that was not reused is classified from its
price history (yfinance cache and the WIKI archive): trading continuing more
than ``STILL_TRADING_AFTER`` past the removal means ``still_trading``; prices
ending within ``DELISTED_WITHIN`` of it mean ``delisting`` at the last close.

Limits: 8-K item numbers exist only from August 2004, so earlier bankruptcies
rest on Wikipedia's text. Nothing reads these events yet.
"""

import logging
import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import requests

from app.repositories.index_membership_repo import (
    EventType,
    Evidence,
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.index_membership import edgar
from app.services.index_membership.coverage import price_spans, read_only
from app.services.index_membership.edgar import Filing
from app.services.index_membership.sp500_import import INDEX_ID, Fetch
from app.services.index_membership.wikipedia_check import (
    CHANGES_URL,
    MATCH_WINDOW,
    WikiChange,
    parse_change,
    table_rows,
)

__all__ = ["TerminalEvent", "build_events", "classify", "classify_reason"]

logger = logging.getLogger(__name__)

SINCE = "2000-01-01"
LOOKBACK = timedelta(days=548)  # ~18 months
LOOKAHEAD = timedelta(days=30)
MAX_DOC_BYTES = 5_000_000
#: More CIKs than this under one name is treated as ambiguous without lookups.
MAX_CIKS = 5
#: Matched with any ``/A`` amendment suffix removed.
#: Trading this long after the removal means the stock kept trading.
STILL_TRADING_AFTER = timedelta(days=30)
#: Prices ending this close to the removal mean the stock was delisted.
DELISTED_WITHIN = timedelta(days=10)
#: ``ticker -> (last trading date, last close or None)``, or ``None``.
LastTrade = Callable[[str], tuple[str, float | None] | None]
ACQUISITION_FORMS = frozenset(
    {"DEFM14A", "PREM14A", "DEFM14C", "PREM14C", "SC TO-T", "SC 14D9", "425"}
)
DELISTING_FORMS = frozenset({"25", "25-NSE", "15-12B"})
#: Wikipedia reason patterns, checked in order.
_REASONS: tuple[tuple[EventType, re.Pattern[str]], ...] = (
    ("bankruptcy", re.compile(r"bankrupt|chapter 11", re.I)),
    (
        "rename",
        re.compile(r"renam|(name|ticker) change|changed (its )?(name|ticker)", re.I),
    ),
    ("acquisition", re.compile(r"acqui|merge|bought|buyout|taken private", re.I)),
    (
        "still_trading",
        re.compile(r"market cap|mid ?-?cap|small ?-?cap|S&P (400|600)", re.I),
    ),
)
#: EDGAR class priority when Wikipedia gives none.
_EDGAR_ORDER: tuple[EventType, ...] = ("bankruptcy", "acquisition", "delisting")
#: Wikipedia classes a delisting filing is consistent with.
_EXITS = frozenset({"acquisition", "bankruptcy"})
#: Errors from one company's SEC fetch that skip it rather than abort the run.
_SEC_ERRORS = (requests.RequestException, KeyError, ValueError)


def build_events(
    repo: IndexMembershipRepository,
    fetch: Fetch,
    sec: Fetch,
    limit: int | None = None,
    last_trade: LastTrade | None = None,
) -> tuple[int, list[TerminalEvent]]:
    """Return the newest ``sp500`` import's id and one event per interval
    ending on/after ``SINCE`` (the first ``limit`` only, if given).

    ``fetch`` reads Wikipedia, ``sec`` reads EDGAR and ``last_trade`` (when
    given) supplies price evidence for events still unknown. Raises
    ``ValueError`` when no membership has been imported.
    """
    latest = repo.latest_import(INDEX_ID)
    if latest is None:
        raise ValueError("no sp500 membership import; run import_index_membership")
    intervals = repo.intervals_ended_since(latest.id, SINCE)[:limit]
    rows = table_rows(fetch(CHANGES_URL).decode(), "changes")
    changes = [c for c in map(parse_change, rows) if c is not None and c.removed]
    lookup = edgar.parse_cik_lookup(sec(edgar.CIK_LOOKUP_URL).decode("latin-1"))
    events = [_event(i, changes, lookup, sec) for i in intervals]
    if last_trade is None:
        return latest.id, events
    tickers = {i.ticker for i in intervals}
    reused = {t for t in tickers if len(repo.intervals_for(INDEX_ID, t)) > 1}
    return latest.id, [with_price_evidence(e, last_trade, reused) for e in events]


def _event(
    interval: MembershipInterval,
    changes: list[WikiChange],
    lookup: dict[str, set[int]],
    sec: Fetch,
) -> TerminalEvent:
    """Match, look up and classify one interval, then read its cash price."""
    change = match_change(interval, changes)
    if change is None:
        return classify(interval, None, None, None, "not in Wikipedia changes")
    if not change.removed_name:
        return classify(interval, change, None, None, "wikipedia only: no name")
    cik, filings, note = _company_filings(interval, change.removed_name, lookup, sec)
    event = classify(interval, change, cik, filings, note)
    if event.event_type != "acquisition" or event.source_filing is None:
        return event
    return _with_price(event, sec)


def match_change(
    interval: MembershipInterval, changes: list[WikiChange]
) -> WikiChange | None:
    """Return the removal of the interval's ticker nearest its end, if within
    ``MATCH_WINDOW`` (an exact ticker beats a root or prefix match)."""
    assert interval.end_date is not None
    end = date.fromisoformat(interval.end_date)
    near = [
        (abs(date.fromisoformat(c.date) - end), c.removed != interval.ticker, c)
        for c in changes
        if c.removed and same_security(interval.ticker, c.removed)
    ]
    near = [n for n in near if n[0] <= MATCH_WINDOW]
    return min(near, key=lambda n: (n[1], n[0]))[2] if near else None


def same_security(ticker: str, removed: str) -> bool:
    """True when Wikipedia's ``removed`` ticker names the interval's ticker:
    equal, its class-share root, or the prefix of a bankruptcy ``Q`` ticker."""
    if removed in (ticker, ticker.split(".")[0]):
        return True
    return ticker.endswith("Q") and len(removed) >= 2 and ticker.startswith(removed)


def with_price_evidence(
    event: TerminalEvent, last_trade: LastTrade, reused: set[str]
) -> TerminalEvent:
    """Classify a still ``unknown``, non-reused event from its last trade."""
    if event.event_type != "unknown" or event.ticker in reused:
        return event
    found = last_trade(event.ticker)
    if found is None:
        return event
    last, close = found
    gap = date.fromisoformat(last) - date.fromisoformat(event.exit_date)
    if gap > STILL_TRADING_AFTER:
        kind: EventType = "still_trading"
        update = {"note": join_notes(event.note, f"prices continue to {last}")}
    elif gap >= -DELISTED_WITHIN:
        kind = "delisting"
        update = {
            "note": join_notes(event.note, f"prices end {last}"),
            "terminal_price": close,
        }
    else:
        return event
    return event.model_copy(update={**update, "event_type": kind, "evidence": "prices"})


def price_last_trade(price_db: Path, wiki_db: Path) -> LastTrade | None:
    """Return a ``LastTrade`` over the yfinance cache and the WIKI archive
    (each opened read-only, skipped if missing), or ``None`` without either.
    The last close is known only from WIKI."""
    yf = price_spans(price_db) if price_db.exists() else {}
    wiki = WikiPriceRepository(lambda: read_only(wiki_db)) if wiki_db.exists() else None
    wiki_spans = wiki.ticker_spans() if wiki is not None else {}
    if not yf and not wiki_spans:
        return None

    def last_trade(ticker: str) -> tuple[str, float | None] | None:
        found: list[tuple[str, float | None]] = []
        if (span := yf.get(ticker.replace(".", "-"))) is not None:
            found.append((span[1], None))
        wiki_ticker = ticker.replace(".", "_")
        if wiki is not None and (span := wiki_spans.get(wiki_ticker)) is not None:
            closes = wiki.closes(wiki_ticker, int(span[1][:4]))
            found.append((span[1], closes[span[1]][0] if span[1] in closes else None))
        return max(found, key=lambda f: (f[0], f[1] is not None), default=None)

    return last_trade


def _company_filings(
    interval: MembershipInterval, name: str, lookup: dict[str, set[int]], sec: Fetch
) -> tuple[int | None, list[Filing] | None, str]:
    """Return ``(cik, filings in window, note)`` for the single CIK named
    ``name`` with filings in the window; ``filings`` is ``None`` otherwise."""
    key = edgar.normalise_name(name)
    ciks = lookup.get(key, set()) if key else set()
    if len(ciks) > MAX_CIKS:
        return None, None, "wikipedia only: ambiguous name in EDGAR"
    found: dict[int, list[Filing]] = {}
    unavailable = False
    for cik in sorted(ciks):
        try:
            in_window = _in_window(_all_filings(cik, interval, sec), interval)
        except _SEC_ERRORS as exc:
            logger.warning("edgar unavailable for CIK %s: %s", cik, exc)
            unavailable = True
            continue
        if in_window:
            found[cik] = in_window
    if len(found) == 1:
        [(cik, filings)] = found.items()
        return cik, filings, ""
    if found:
        problem = "ambiguous name in EDGAR"
    else:
        problem = "edgar unavailable" if unavailable else "name not found in EDGAR"
    return None, None, f"wikipedia only: {problem}"


def _all_filings(cik: int, interval: MembershipInterval, sec: Fetch) -> list[Filing]:
    """Return the company's recent filings plus older pages overlapping the
    interval's window."""
    first, last = _window(interval)
    data = sec(edgar.SUBMISSIONS_URL.format(cik=cik))
    filings = edgar.parse_filings(data)
    for url in edgar.older_pages(data, first, last):
        filings += edgar.parse_filings(sec(url), cik)
    return filings


def _in_window(filings: list[Filing], interval: MembershipInterval) -> list[Filing]:
    """Return ``filings`` dated within the interval's window."""
    first, last = _window(interval)
    return [f for f in filings if first <= f.date <= last]


def _window(interval: MembershipInterval) -> tuple[str, str]:
    """Return ``(exit - LOOKBACK, exit + LOOKAHEAD)`` as ISO dates."""
    assert interval.end_date is not None
    exit_day = date.fromisoformat(interval.end_date)
    return (
        (exit_day - LOOKBACK).isoformat(),
        (exit_day + LOOKAHEAD).isoformat(),
    )


def classify_reason(reason: str) -> EventType | None:
    """Return the event type Wikipedia's reason text implies, if any."""
    return next((kind for kind, rx in _REASONS if rx.search(reason)), None)


def classify(
    interval: MembershipInterval,
    change: WikiChange | None,
    cik: int | None,
    filings: list[Filing] | None,
    note: str = "",
) -> TerminalEvent:
    """Classify an interval's end from its Wikipedia change and the matched
    company's in-window EDGAR filings (``None`` when no company matched)."""
    assert interval.end_date is not None
    wiki = classify_reason(change.reason) if change else None
    found = _edgar_classes(filings or [], interval.end_date)
    kind: EventType = wiki or "unknown"
    evidence: Evidence = "wikipedia" if wiki else "none"
    source = None
    conflicts = [
        k for k in found if k != wiki and not (k == "delisting" and wiki in _EXITS)
    ]
    usable = [
        k
        for k in _EDGAR_ORDER
        if k in found and (k != "acquisition" or "delisting" in found)
    ]
    if wiki in found:
        evidence, source = "wikipedia+edgar", found[wiki].url
    elif wiki and conflicts:
        note = join_notes(note, f"conflict: edgar suggests {', '.join(conflicts)}")
    elif wiki and filings is not None and "delisting" not in found:
        note = join_notes(note, "no confirming edgar filing")
    elif not wiki and usable:
        kind = usable[0]
        evidence, source = "edgar", found[kind].url
    return TerminalEvent(
        security_key=interval.security_key,
        ticker=interval.ticker,
        exit_date=interval.end_date,
        event_type=kind,
        cik=cik,
        terms="terms unknown" if kind == "acquisition" else None,
        source_filing=source,
        evidence=evidence,
        note=note or ("" if kind != "unknown" else "no reason class or edgar filing"),
    )


def _edgar_classes(filings: list[Filing], exit_date: str) -> dict[EventType, Filing]:
    """Return the evidencing filing per EDGAR class; for acquisitions the most
    recent merger filing on/before the exit (else the earliest after it)."""
    found: dict[EventType, Filing] = {}
    merger = [f for f in filings if f.form.removesuffix("/A") in ACQUISITION_FORMS]
    before = [f for f in merger if f.date <= exit_date]
    if merger:
        found["acquisition"] = (
            max(before, key=_date) if before else min(merger, key=_date)
        )
    for kind, hits in (
        ("bankruptcy", [f for f in filings if f.form == "8-K" and "1.03" in f.items]),
        ("delisting", [f for f in filings if f.form in DELISTING_FORMS]),
    ):
        if hits:
            found[kind] = max(hits, key=_date)
    return found


def _date(filing: Filing) -> str:
    return filing.date


def _with_price(event: TerminalEvent, sec: Fetch) -> TerminalEvent:
    """Read the event's merger document for a cash price per share."""
    assert event.source_filing is not None
    try:
        text = sec(event.source_filing)[:MAX_DOC_BYTES].decode("utf-8", "replace")
    except _SEC_ERRORS as exc:
        logger.warning("merger document unavailable %s: %s", event.source_filing, exc)
        note = join_notes(event.note, "merger document unavailable")
        return event.model_copy(update={"note": note})
    found = edgar.cash_price_per_share(text)
    if found is None:
        return event
    price, mixed = found
    if mixed:  # the stock part has no value here, so no single price
        terms = f"cash ${price:.2f} per share plus stock"
        return event.model_copy(update={"terms": terms})
    return event.model_copy(
        update={"terminal_price": price, "terms": f"cash ${price:.2f} per share"}
    )


def join_notes(*notes: str) -> str:
    """Join the non-empty ``notes`` with ``"; "``."""
    return "; ".join(n for n in notes if n)
