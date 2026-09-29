"""Read-only, as-of evidence for trade reviews (GH-17).

The checklist never opens a store itself: it asks an ``EvidenceReader`` for
a security's split-continuous bars through a session, or the latest
committed monthly scan visible at a session, always passing a bound strictly
before the trade date. ``StoreEvidenceReader`` answers from the historical
price cache and the backtest store, both opened ``mode=ro``; no write path
and no ``ensure_schema`` is ever reached. Tests use fakes behind the same
protocol.

Prices come back in major units of the listing's currency (a ``GBp`` line is
scaled to pounds), so a trade price and a scan pivot can be compared once
their currencies agree.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.core import config
from app.core.ticker_identity import (
    canonicalize_or_fallback,
    load_aliases,
    load_provider_symbol_aliases,
)
from app.repositories.backtest_repo import BacktestRepository
from app.repositories.db import Connect
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalEvidenceReadHandle,
    HistoricalPriceRepository,
)
from app.services.backtest.market_planes import (
    HistoricalMarketPlanes,
    quote_unit_scale,
)

logger = logging.getLogger(__name__)

PRICE_SOURCE = "historical price cache"
SCAN_SOURCE = "committed monthly scan"
_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "stock_splits",
)
_LSE_SUFFIX = ".L"


@dataclass(frozen=True)
class PriceBar:
    """One session, split-continuous as of the read's bound, in major units.

    ``split_factor`` is the as-traded close over this close: dividing a
    price stated in this session's own shares by it restates the price in
    the read bound's shares.
    """

    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    split_factor: Decimal = Decimal(1)


@dataclass(frozen=True)
class PriceWindow:
    """Bars read through ``through`` for one security, oldest first.

    ``currency`` is the price line's own currency (major units), so a stop
    stated in the trade's currency is only compared when the two agree.
    """

    security_id: str
    revision: str
    through: date
    bars: tuple[PriceBar, ...]
    currency: str


@dataclass(frozen=True)
class ScanContext:
    """The latest committed scan visible at a session; pivot in major units."""

    security_id: str
    snapshot_month: str
    as_of: date
    currency: str
    stage: str
    pivot: Decimal | None


class EvidenceReader(Protocol):
    """As-of evidence the checklist may read; every bound is caller-chosen."""

    failed: bool

    def resolve(self, ticker: str, currency: str) -> str | None:
        """Return the store's security id for a trade's ticker, or None."""
        ...

    def bars(self, security_id: str, through: date, limit: int) -> PriceWindow | None:
        """Return up to ``limit`` bars with sessions on or before ``through``."""
        ...

    def scan(self, security_id: str, as_of: date) -> ScanContext | None:
        """Return the latest committed scan with a session on or before ``as_of``."""
        ...

    def split_on(self, security_id: str, session: date) -> Decimal | None:
        """Return the split ratio effective on ``session`` (1 when none).

        Only that session's corporate action is read, never its prices: a
        split's ex-date is fixed before the session opens. None when the
        store cannot tell.
        """
        ...

    def close(self) -> None:
        """Release any open read handles."""
        ...


def read_only_connect(path: Path) -> Connect:
    """Return a ``Connect`` that opens ``path`` read-only (never creates it)."""

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    return _connect


def open_store_reader() -> StoreEvidenceReader:
    """Build a reader over the configured stores, both opened read-only."""
    return StoreEvidenceReader(
        HistoricalPriceRepository(read_only_connect(config.HISTORICAL_PRICE_CACHE)),
        BacktestRepository(read_only_connect(config.BACKTEST_DB)),
    )


def store_revision() -> tuple[int | None, ...]:
    """Return the evidence stores' file mtimes (ns, WAL included; None if absent).

    A stat, never an open: a new price session or committed scan changes it,
    so cached reviews refresh when the stores do.
    """
    stores = (config.HISTORICAL_PRICE_CACHE, config.BACKTEST_DB)
    return tuple(
        _mtime(Path(f"{store}{suffix}")) for store in stores for suffix in ("", "-wal")
    )


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def provider_candidates(canonical: str, currency: str) -> tuple[str, ...]:
    """Return the provider spellings a trade ticker may be listed under.

    A ticker with an LSE marker -- SIPP's trailing ``.`` (``BP.`` for
    ``BP.L``) or an explicit ``.L`` -- is its LSE line only. Otherwise the
    bare symbol comes first, so a sterling ``BA`` stays Boeing rather than
    BAE Systems' ``BA.L``; a sterling trade falls back to the LSE line only
    when the bare symbol is absent.
    """
    if canonical.upper().endswith(_LSE_SUFFIX):
        return (canonical,)
    if canonical.endswith("."):
        return (f"{canonical[:-1]}{_LSE_SUFFIX}",)
    if currency.upper() != "GBP":
        return (canonical,)
    return (canonical, f"{canonical}{_LSE_SUFFIX}")


class StoreEvidenceReader:
    """``EvidenceReader`` over the price cache and backtest store, read-only.

    Any store failure (missing file, lock, corrupt row) is logged, sets
    ``failed`` and reads as "no evidence", so the affected checks become
    unknown; the caller caches that result under the store revision, so it
    refreshes once the store changes.
    """

    def __init__(
        self,
        prices: HistoricalPriceRepository,
        backtest: BacktestRepository,
        aliases: dict[str, str] | None = None,
        provider_aliases: dict[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._backtest = backtest
        self._aliases = load_aliases() if aliases is None else aliases
        self._provider_aliases = (
            load_provider_symbol_aliases()
            if provider_aliases is None
            else provider_aliases
        )
        self._identities: dict[str, str] | None = None
        self._profile_hash: str | None = None
        self._handles: dict[str, HistoricalEvidenceReadHandle | None] = {}
        self.failed = False

    def resolve(self, ticker: str, currency: str) -> str | None:
        """Map a trade ticker to a backtest identity, else portfolio evidence."""
        canonical = canonicalize_or_fallback(
            ticker, self._aliases, logger=logger, context="trade review"
        )
        try:
            for symbol in provider_candidates(canonical, currency):
                spelling = self._provider_aliases.get(symbol, symbol)
                found = self._identity_map().get(spelling)
                if found is not None:
                    return found
            fallback = f"portfolio:{canonical}"
            return fallback if self._handle(fallback) is not None else None
        except Exception:
            return self._fail("resolve", ticker)

    def bars(self, security_id: str, through: date, limit: int) -> PriceWindow | None:
        """Read split-continuous bars through ``through`` (clipped to the data)."""
        try:
            handle = self._handle(security_id)
            if handle is None or through < handle.start:
                return None
            bound = min(through, handle.end - timedelta(days=1))
            bounded = handle.bounded(through=bound, limit=limit, columns=_COLUMNS)
            plane = HistoricalMarketPlanes.from_bounded_evidence(bounded)
            rows = plane.split_continuous_window_as_of(bound, limit=limit)
            traded = {row.session: row.close for row in plane.as_traded()}
            scale = plane.quote_unit_scale
            bars = tuple(
                PriceBar(
                    session=row.session,
                    open=row.open * scale,
                    high=row.high * scale,
                    low=row.low * scale,
                    close=row.close * scale,
                    volume=row.volume,
                    split_factor=traded[row.session] / row.close,
                )
                for row in rows
            )
            return PriceWindow(
                security_id, handle.data_revision, bound, bars, plane.currency
            )
        except Exception:
            return self._fail("bars", security_id)

    def split_on(self, security_id: str, session: date) -> Decimal | None:
        """Return the split ratio on ``session``; None outside the evidence."""
        try:
            handle = self._handle(security_id)
            if handle is None or not handle.start <= session < handle.end:
                return None
            bounded = handle.bounded(through=session, limit=1, columns=_COLUMNS)
            plane = HistoricalMarketPlanes.from_bounded_evidence(bounded)
            ratio = Decimal(1)
            for action in plane.actions_as_of(session):
                if action.action_type == "split" and action.session == session:
                    ratio *= action.value
            return ratio
        except Exception:
            return self._fail("split", security_id)

    def scan(self, security_id: str, as_of: date) -> ScanContext | None:
        """Return the active profile's latest committed scan as of ``as_of``."""
        try:
            profile_hash = self._active_profile()
            if profile_hash is None:
                return None
            record = self._backtest.latest_committed_scan_result(
                profile_hash=profile_hash, security_id=security_id, as_of_session=as_of
            )
            if record is None:
                return None
            pivot = record.vcp.pivot_price
            scale = quote_unit_scale(record.currency, record.quote_unit)
            return ScanContext(
                security_id=security_id,
                snapshot_month=record.snapshot_month,
                as_of=record.as_of_session_date,
                currency=record.currency,
                stage=record.stage.value,
                pivot=None if pivot is None else pivot * scale,
            )
        except Exception:
            return self._fail("scan", security_id)

    def close(self) -> None:
        """Close every read handle this reader opened."""
        for handle in self._handles.values():
            if handle is not None:
                handle.close()
        self._handles.clear()

    def _identity_map(self) -> dict[str, str]:
        if self._identities is None:
            try:
                rows = self._backtest.identity_rows()
            except Exception:
                rows = self._fail("identities", "backtest store") or []
            self._identities = {symbol: sid for sid, _mic, symbol, _ in rows}
        return self._identities

    def _active_profile(self) -> str | None:
        if self._profile_hash is None:
            try:
                active = self._backtest.active_snapshot_profile()
            except Exception:
                active = self._fail("active profile", "backtest store")
            self._profile_hash = "" if active is None else active.profile_hash
        return self._profile_hash or None

    def _handle(self, security_id: str) -> HistoricalEvidenceReadHandle | None:
        """Open (once) the security's latest price revision; None if absent."""
        if security_id not in self._handles:
            try:
                revisions = self._prices.latest_revisions_for_securities((security_id,))
                handle = self._prices.open_read(revisions[0][1]) if revisions else None
            except EvidenceMissingError:
                handle = None
            except Exception:
                handle = self._fail("price revision", security_id)
            self._handles[security_id] = handle
        return self._handles[security_id]

    def _fail(self, what: str, key: str) -> None:
        logger.warning(
            "trade review evidence %s failed for %s", what, key, exc_info=True
        )
        self.failed = True
        return None
