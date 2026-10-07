"""Point-in-time S&P 500 roster source for ``PointInTimeRosterPolicyV2`` (#82).

Rows come from the #68 membership intervals, the #73 terminal events and the
#70 WIKI archive, all opened read-only; nothing is fetched from the network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from app.repositories.index_membership_repo import (
    IndexMembershipRepository,
    MembershipInterval,
    TerminalEvent,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.backtest.canonical_manifest import canonical_json, manifest_digest
from app.services.backtest.reconstruction_roster import (
    POINT_IN_TIME_POLICY_VERSION,
    RosterCaptureError,
    RosterSource,
    RosterSourcePayloadV1,
)
from app.services.index_membership.coverage import price_spans, read_only
from app.services.index_membership.sp500_import import INDEX_ID
from app.services.index_membership.wiki_link import (
    Override,
    load_overrides,
    wiki_ticker,
)

#: Event types that end a company, so a later same-ticker interval is another.
EXIT_EVENTS = frozenset({"acquisition", "bankruptcy", "delisting"})
#: Ending events whose prices may live only in WIKI under the old ticker.
WIKI_FALLBACK_EVENTS = EXIT_EVENTS | {"unknown", "rename"}
DEFAULT_SINCE = date(2000, 1, 1)
#: How close a price series' last date must be to a membership end to count
#: as ending there (WIKI provider choice, synthetic exits).
PRICE_END_TOLERANCE = timedelta(days=10)

#: ``symbol -> (first, last)`` dates with a close.
Spans = Mapping[str, tuple[str, str]]
#: ``(ticker, exit_date) -> True`` when the ticker's prices span the exit.
TradesAfter = Callable[[str, str], bool]


def point_in_time_rows(
    intervals: Sequence[MembershipInterval],
    events: Mapping[str, TerminalEvent],
    wiki_spans: Spans,
    overrides: Sequence[Override],
    yf_spans: Spans | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Return one roster row per ticker and the exits kept for continuity.

    ``events`` is keyed by ``security_key``. Every interval up to the last one
    ended by an exit that a later same-ticker interval follows is dropped (the
    ticker was reused by another company), unless a price series of the
    ticker spans that exit: then it is the same security (e.g. a bankruptcy
    it traded through, or a re-domicile), the intervals are kept and
    ``(ticker, exit_date)`` is reported.
    """
    yf = yf_spans or {}
    trades_after = spans_trade_after((wiki_spans, yf))
    rows, continuity = [], []
    ordered = sorted(intervals, key=lambda i: (i.ticker, i.start_date))
    for ticker, group in groupby(ordered, key=lambda i: i.ticker):
        kept, carried = _after_last_exit(list(group), events, trades_after)
        continuity += carried
        rows.append(_row(ticker, kept, events, wiki_spans, overrides, yf))
    return rows, continuity


def _after_last_exit(
    spells: list[MembershipInterval],
    events: Mapping[str, TerminalEvent],
    trades_after: TradesAfter,
) -> tuple[list[MembershipInterval], list[tuple[str, str]]]:
    exits, carried = [], []
    for index, spell in enumerate(spells[:-1]):
        event = events.get(spell.security_key)
        if event is None or event.event_type not in EXIT_EVENTS:
            continue
        if trades_after(spell.ticker, event.exit_date):
            carried.append((index, (spell.ticker, event.exit_date)))
        else:
            exits.append(index)
    cut = exits[-1] + 1 if exits else 0
    return spells[cut:], [exit_ for index, exit_ in carried if index >= cut]


def spans_trade_after(spans: Sequence[Spans]) -> TradesAfter:
    """Return a ``TradesAfter`` over price spans keyed by yfinance (``-``) or
    WIKI (``_``) class-share spelling: a series must start on/before the exit
    and end after it."""

    def trades_after(ticker: str, exit_date: str) -> bool:
        names = {ticker.replace(".", "-"), ticker.replace(".", "_")}
        return any(
            (span := found.get(name)) is not None and span[0] <= exit_date < span[1]
            for found in spans
            for name in names
        )

    return trades_after


def _ends_near(span: tuple[str, str] | None, end: date, *, late_ok: bool) -> bool:
    """True when ``span`` ends within ``PRICE_END_TOLERANCE`` of ``end``
    (or anywhere after it when ``late_ok``)."""
    if span is None:
        return False
    last = date.fromisoformat(span[1])
    return last >= end - PRICE_END_TOLERANCE and (
        late_ok or last <= end + PRICE_END_TOLERANCE
    )


def _row(
    ticker: str,
    spells: list[MembershipInterval],
    events: Mapping[str, TerminalEvent],
    wiki_spans: Spans,
    overrides: Sequence[Override],
    yf_spans: Spans,
) -> dict[str, Any]:
    """Build one row. The provider is ``wiki`` when the last interval ended
    with a WIKI-fallback event and the WIKI series reaches that end; when no
    exit is pinned but the chosen provider's prices stop at the end, a
    synthetic ``delisting`` (price unknown) is pinned, so a held position is
    settled instead of going stale."""
    last = spells[-1]
    event = events.get(last.security_key) if last.end_date is not None else None
    provider, symbol = "yfinance", ticker.replace(".", "-")
    end = None if last.end_date is None else date.fromisoformat(last.end_date)
    if end is not None and event is not None:
        if event.event_type in WIKI_FALLBACK_EVENTS:
            # Overrides are dated; look the WIKI ticker up on the last member day.
            last_day = (end - timedelta(days=1)).isoformat()
            wiki = wiki_ticker(ticker, last_day, list(overrides))
            if _ends_near(wiki_spans.get(wiki), end, late_ok=True):
                provider, symbol = "wiki", wiki
    exit_ = None
    if event is not None and event.event_type in EXIT_EVENTS:
        exit_ = {
            "exit_date": event.exit_date,
            "event_type": event.event_type,
            "terminal_price": event.terminal_price,
            "event_digest": manifest_digest(event.model_dump()),
        }
    elif end is not None:
        span = (wiki_spans if provider == "wiki" else yf_spans).get(symbol)
        if span is not None and _ends_near(span, end, late_ok=False):
            exit_ = {
                "exit_date": end.isoformat(),
                "event_type": "delisting",
                "terminal_price": None,
                "event_digest": manifest_digest(
                    {
                        "basis": "prices_end",
                        "ticker": ticker,
                        "last_price_date": span[1],
                        "interval_end": end.isoformat(),
                    }
                ),
            }
    return {
        "symbol": ticker,
        "provider": provider,
        "provider_symbol": symbol,
        "membership_intervals": [[s.start_date, s.end_date] for s in spells],
        "terminal_exit": exit_,
    }


class PointInTimeRosterSourceAdapter:
    """Read the point-in-time S&P 500 roster from local databases only."""

    def __init__(
        self,
        membership_db: Path,
        wiki_db: Path,
        overrides_path: Path,
        *,
        price_db: Path | None = None,
        since: date = DEFAULT_SINCE,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._membership_db = membership_db
        self._wiki_db = wiki_db
        self._overrides_path = overrides_path
        self._price_db = price_db
        self._since = since
        self._clock = clock

    def __call__(self) -> RosterSourcePayloadV1:
        try:
            return self._payload()
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise RosterCaptureError(
                f"point-in-time S&P 500 roster unavailable: {exc}",
                code="provider_unavailable",
            ) from exc

    def _payload(self) -> RosterSourcePayloadV1:
        for path in (self._membership_db, self._wiki_db):
            if not path.is_file():
                raise FileNotFoundError(f"missing database: {path}")
        membership = IndexMembershipRepository(lambda: read_only(self._membership_db))
        wiki = WikiPriceRepository(lambda: read_only(self._wiki_db))
        latest = membership.latest_import(INDEX_ID)
        wiki_import = wiki.latest_import()
        if latest is None or wiki_import is None:
            raise ValueError("membership or WIKI import missing")
        open_spells = membership.roster_on(INDEX_ID, latest.last_date)
        intervals = membership.intervals_ended_since(latest.id, self._since.isoformat())
        if open_spells is not None:
            intervals += [i for i in open_spells.members if i.end_date is None]
        events = membership.terminal_events(INDEX_ID)
        wiki_spans = wiki.ticker_spans()
        if self._price_db is not None and not self._price_db.is_file():
            raise FileNotFoundError(f"missing database: {self._price_db}")
        yf_spans = {} if self._price_db is None else price_spans(self._price_db)
        rows, continuity = point_in_time_rows(
            intervals,
            {event.security_key: event for event in events},
            wiki_spans,
            load_overrides(self._overrides_path),
            yf_spans,
        )
        return RosterSourcePayloadV1.build(
            source=RosterSource.SP500_POINT_IN_TIME,
            rows=rows,
            retrieved_at=self._clock(),
            source_version=latest.source_ref,
            package_version=POINT_IN_TIME_POLICY_VERSION,
            config_version=canonical_json(
                {
                    "membership_source_digest": latest.source_digest,
                    "terminal_events_digest": manifest_digest(
                        [event.model_dump() for event in events]
                    ),
                    "wiki_import_digest": wiki_import.source_digest,
                    "since": self._since,
                    "price_db_used": self._price_db is not None,
                    "kept_by_continuity": sorted(continuity),
                }
            ),
        )
