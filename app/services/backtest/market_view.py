"""Concrete no-look-ahead ``MarketView`` bound to one simulated session (AD-3).

Implements the widened :class:`~app.services.backtest.strategy_protocol.
MarketViewV1` seam: a pandas-backed, bounds-checked read surface a future
Backtest Engine (Story 2.4) constructs once per simulated date ``D`` and
hands to a Strategy's ``entry_signals``/``exit_signals``/``position_size``.

``MarketView`` is deliberately *not* a Strategy-runtime module -- it is the
implementation those methods are handed an instance of, never something a
Strategy imports by name -- so, unlike ``strategy_protocol.py``, it is free
to import repositories directly (AD-10's import-boundary guard only walks a
Strategy's own module graph, see
``tests/backtest/test_strategy_runtime_import_boundary.py``).

Construction is deliberately narrow: a caller supplies exactly the
per-security price/action evidence revisions already pinned for this Run
(typically a ``RunInputManifestV1``'s ``securities`` tuple), never "whatever
is currently newest." ``.price_history``/``.scan_result`` never reach past
``as_of_session`` and never silently substitute a different revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping

import pandas as pd

from app.repositories.backtest_repo import BacktestRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_scan_record import HistoricalScanRecordV1
from app.services.backtest.market_planes import HistoricalMarketPlanes

#: Column order every ``MarketView.price_history`` DataFrame uses, whether
#: populated or empty -- a Strategy can rely on this shape regardless of
#: whether ``security_id`` has any evidence.
PRICE_HISTORY_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class MarketViewBoundError(LookupError):
    """A security's pinned evidence does not itself cover ``as_of_session``.

    Distinct from "no evidence pinned for this security at all" (which
    ``price_history`` answers with an empty DataFrame, not an error): this
    fires only when the view *does* track ``security_id`` but that
    security's pinned evidence interval ends before -- or starts after --
    the view's own ``as_of_session`` bound, so honoring the request would
    mean either silently truncating to less than the caller believes is
    current or reaching past the view's bound. Mirrors
    ``EvidenceMissingError``'s stable-``.code`` convention
    (``historical_price_repo.py``).
    """

    code = "bound_violation"

    def __init__(self, *, security_id: str, as_of_session: date) -> None:
        self.security_id = security_id
        self.as_of_session = as_of_session
        super().__init__(
            f"{security_id!r} has no pinned evidence covering "
            f"{as_of_session.isoformat()}"
        )


def _empty_price_history() -> pd.DataFrame:
    return pd.DataFrame(
        columns=PRICE_HISTORY_COLUMNS,
        index=pd.Index([], dtype=object, name="session"),
    )


@dataclass(frozen=True)
class MarketView:
    """Pandas-backed, bounds-checked market view for one simulated session.

    ``security_price_revisions`` maps each security this view tracks to
    its exact pinned Historical Price/Corporate Action ``data_revision``
    (``HistoricalPriceRepository``'s content-addressed evidence key). A
    ``security_id`` absent from this mapping is simply untracked by this
    Run -- ``.price_history`` returns an empty DataFrame and
    ``.scan_result`` still resolves independently through
    ``backtest_repo`` (scan visibility is not gated on price evidence).
    """

    as_of_session: date
    profile_hash: str
    security_price_revisions: Mapping[str, str]
    backtest_repo: BacktestRepository
    historical_price_repo: HistoricalPriceRepository

    def __post_init__(self) -> None:
        # Detach from a caller-supplied dict so later caller-side mutation
        # can never reach this view, matching PortfolioView's convention.
        object.__setattr__(
            self,
            "security_price_revisions",
            MappingProxyType(dict(self.security_price_revisions)),
        )

    def price_history(self, security_id: str) -> pd.DataFrame:
        """Return ``security_id``'s split-continuous OHLCV history through
        ``as_of_session``, oldest first, indexed by session date.

        Uses AD-6's ``split_continuous_as_of_D`` plane -- the one plane a
        Strategy or detector may see: every split effective by
        ``as_of_session`` is already applied, and no future corporate
        action is ever exposed. Values are ``Decimal`` (object dtype),
        matching the deterministic-rounding policy every other AD-6
        consumer in this codebase uses; convert a column explicitly
        (``.astype(float)``) if vectorized numeric libraries are needed.

        Raises :class:`MarketViewBoundError` if ``security_id`` *is*
        tracked by this view but its pinned evidence interval does not
        itself cover ``as_of_session`` -- never silently truncates to an
        earlier, misleadingly-labeled "current" state.
        """
        revision = self.security_price_revisions.get(security_id)
        if revision is None:
            return _empty_price_history()
        evidence = self.historical_price_repo.get(revision)
        plane = HistoricalMarketPlanes.from_evidence(evidence)
        if not (plane.start <= self.as_of_session < plane.end):
            raise MarketViewBoundError(
                security_id=security_id, as_of_session=self.as_of_session
            )
        rows = plane.split_continuous_as_of(self.as_of_session)
        if not rows:
            return _empty_price_history()
        frame = pd.DataFrame(
            {
                "open": [row.open for row in rows],
                "high": [row.high for row in rows],
                "low": [row.low for row in rows],
                "close": [row.close for row in rows],
                "volume": [row.volume for row in rows],
            },
            # ``dtype=object`` keeps the index as plain ``datetime.date``
            # values -- pandas would otherwise infer a tz-naive
            # ``DatetimeIndex`` from a list of ``date`` objects, which
            # compares unreliably against plain ``date`` values callers
            # naturally hold (e.g. ``as_of_session`` itself).
            index=pd.Index([row.session for row in rows], dtype=object, name="session"),
            columns=PRICE_HISTORY_COLUMNS,
        )
        return frame

    def scan_result(self, security_id: str) -> HistoricalScanRecordV1 | None:
        """Return the latest committed monthly scan record visible at
        ``as_of_session``, or ``None`` if none is visible yet.

        Delegates entirely to
        ``BacktestRepository.latest_committed_scan_result`` -- the one
        query authority for monthly-scan visibility timing, so this view
        never re-implements the "own recorded month-end
        ``as_of_session_date`` onward, until superseded" rule itself.
        """
        return self.backtest_repo.latest_committed_scan_result(
            profile_hash=self.profile_hash,
            security_id=security_id,
            as_of_session=self.as_of_session,
        )


__all__ = [
    "MarketView",
    "MarketViewBoundError",
    "PRICE_HISTORY_COLUMNS",
]
