"""Trade process review service (GH-17) — the seam the History tab uses.

Reviews are computed on demand from the FIFO replay (which BUY lots each
SELL closed, which lots are still open), the append-only annotations and
Strategy history, and read-only evidence stores. They are cached in process
against the trade, annotation and Strategy-history revisions, the evidence
stores' revision and today's date (an open lot's exit check watches new
sessions), the ``RealisedPnlService.compute_summary`` pattern. A cold cache
is computed once under a lock however many requests arrive together; a
result that hit a store failure is cached too, and refreshes when the store
revision changes. One trade that fails to review is reported unknown on its
own. The only write here is appending an annotation; trades, cash flows,
portfolios, Strategy assignments and the evidence stores are never written.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from threading import Lock
from typing import Any

from app.agents.trade_review.checklist import (
    Entry,
    failed_review,
    review_buy,
    review_sell,
)
from app.agents.trade_review.evidence import (
    EvidenceReader,
    open_store_reader,
)
from app.agents.trade_review.evidence import store_revision as _store_revision
from app.agents.trade_review.weekly import (
    TradeReviewClient,
    build_prompt,
    trade_label,
    weekly_facts,
)
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.repositories.trade_annotations_repo import (
    TradeAnnotationsRepository,
    fingerprint_of,
    resolve_annotations,
    ticker_aliases,
)
from app.schemas import Trade
from app.schemas.trade_review import (
    MAX_INTENT_LENGTH,
    TradeAnnotationV1,
    TradeReviewInterpretationV1,
    TradeReviewV1,
    WeeklyFactsV1,
)
from app.services.realised_pnl_service import RealisedPnlService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

_CACHE_LIMIT = 8
_RevisionKey = tuple[Any, ...]


class UnknownTradeError(LookupError):
    """No reviewable trade has that id."""


class AnnotationError(ValueError):
    """The submitted annotation is invalid; the message is user-facing."""


class EmptyWeekError(LookupError):
    """The requested week has no reviewed trades."""


@dataclass(frozen=True)
class WeeklyView:
    """One ISO week's reviews, facts and annotations, with its neighbours."""

    week: str | None = None
    previous: str | None = None
    next: str | None = None
    reviews: tuple[TradeReviewV1, ...] = ()
    labels: dict[int, str] = field(default_factory=dict)
    facts: WeeklyFactsV1 | None = None
    annotations: tuple[TradeAnnotationV1, ...] = ()


class TradeReviewService:
    """Build, cache and present trade reviews; append annotations."""

    def __init__(
        self,
        trader: TraderService,
        realised_pnl: RealisedPnlService,
        annotations: TradeAnnotationsRepository,
        strategies: PortfolioStrategiesRepository,
        reader_factory: Callable[[], EvidenceReader] = open_store_reader,
        store_revision: Callable[[], object] = _store_revision,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._trader = trader
        self._realised = realised_pnl
        self._annotations = annotations
        self._strategies = strategies
        self._reader_factory = reader_factory
        self._store_revision = store_revision
        self._today = today
        # ponytail: one lock for the whole cache -- a cold load computes once
        # while others wait; per-key locks if distinct keys ever contend.
        self._lock = Lock()
        self._cache: OrderedDict[_RevisionKey, dict[int, TradeReviewV1]] = OrderedDict()

    def reviews(self) -> dict[int, TradeReviewV1]:
        """Return every portfolio's reviews keyed by trade id (revision-cached)."""
        ids = [p.id for p in self._trader.list_portfolios()]
        key = self._key(ids)
        if key is None:
            return self._compute(ids)
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                cached = self._compute(ids)
                if self._key(ids) == key:
                    self._cache[key] = cached
                    while len(self._cache) > _CACHE_LIMIT:
                        self._cache.popitem(last=False)
        return dict(cached)

    def review(self, trade_id: int) -> TradeReviewV1 | None:
        """Return one trade's review, or None if it has none.

        A warm cache answers directly; a cold one reviews only the trade's
        own portfolio, so opening one trade never recomputes every account.
        """
        trade = self._find(trade_id)
        if trade is None or trade.portfolio_id is None:
            return None
        key = self._key([p.id for p in self._trader.list_portfolios()])
        with self._lock:
            cached = None if key is None else self._cache.get(key)
        if cached is not None:
            return cached.get(trade_id)
        return self._compute([trade.portfolio_id]).get(trade_id)

    def annotations_for(self, trade_id: int) -> list[TradeAnnotationV1]:
        """Return one trade's annotations, newest (current) first."""
        return self._annotation_map().get(trade_id, [])

    def annotate(
        self, trade_id: int, intent: str, stated_stop: float | None
    ) -> TradeAnnotationV1:
        """Append an annotation for a trade; the trade itself is untouched.

        Raises :class:`UnknownTradeError` for an unknown trade and
        :class:`AnnotationError` for invalid input (nothing is written).
        """
        trade = self._trade(trade_id)
        intent = intent.strip()
        if len(intent) > MAX_INTENT_LENGTH:
            raise AnnotationError(
                f"Intent must be at most {MAX_INTENT_LENGTH} characters."
            )
        if stated_stop is not None and not stated_stop > 0:
            raise AnnotationError("A stated stop must be a positive price.")
        if not intent and stated_stop is None:
            raise AnnotationError("Add an intent, a stated stop, or both.")
        assert trade.portfolio_id is not None
        return self._annotations.add(
            trade.portfolio_id,
            trade_id,
            intent,
            stated_stop,
            fingerprint_of(trade, ticker_aliases()),
        )

    def weekly(self, week: str | None = None) -> WeeklyView:
        """Return ``week``'s view, defaulting to the latest week with trades."""
        reviews = sorted(
            self.reviews().values(), key=lambda r: (r.trade_date, r.trade_id)
        )
        weeks = sorted({r.week for r in reviews})
        if not weeks:
            return WeeklyView()
        current = week if week is not None and week in weeks else weeks[-1]
        index = weeks.index(current)
        in_week = tuple(r for r in reviews if r.week == current)
        latest = {tid: notes[0] for tid, notes in self._annotation_map().items()}
        return WeeklyView(
            week=current,
            previous=weeks[index - 1] if index else None,
            next=weeks[index + 1] if index + 1 < len(weeks) else None,
            reviews=in_week,
            labels={r.trade_id: trade_label(i) for i, r in enumerate(in_week)},
            facts=weekly_facts(current, in_week),
            annotations=tuple(
                latest[r.trade_id] for r in in_week if r.trade_id in latest
            ),
        )

    def interpret(
        self, week: str | None, client: TradeReviewClient
    ) -> TradeReviewInterpretationV1 | None:
        """Ask Claude to interpret one week's anonymised facts; stores nothing.

        Raises :class:`EmptyWeekError` when ``week`` (or, with None, any week)
        has no reviewed trades: a different week is never interpreted.
        """
        view = self.weekly(week)
        if view.facts is None or (week is not None and view.week != week):
            raise EmptyWeekError(week)
        return client.interpret(build_prompt(view.facts, view.reviews))

    def _find(self, trade_id: int) -> Trade | None:
        return next(
            (t for t in self._trader.get_trade_history() if t.id == trade_id), None
        )

    def _trade(self, trade_id: int) -> Trade:
        trade = self._find(trade_id)
        if trade is None or trade.portfolio_id is None:
            raise UnknownTradeError(trade_id)
        return trade

    def _annotation_map(self) -> dict[int, list[TradeAnnotationV1]]:
        """Every trade's annotations, re-attached across corrections."""
        return resolve_annotations(
            self._annotations.all(), self._trader.get_trade_history()
        )

    def _key(self, ids: list[int]) -> _RevisionKey | None:
        """The cache key, or None (bypass the cache) if a revision fails."""
        try:
            revisions = self._trader.get_trade_revisions([*ids, -1])
            return (
                tuple(sorted(revisions.items())),
                self._annotations.revision(),
                self._strategies.history_revision(),
                self._store_revision(),
                self._today(),
            )
        except Exception:
            logger.warning("Trade review: revisions unavailable; bypassing cache")
            return None

    def _compute(self, ids: list[int]) -> dict[int, TradeReviewV1]:
        """Review every trade of the given portfolios."""
        reader = self._reader_factory()
        try:
            notes = self._annotation_map()
            latest = {tid: n[0] for tid, n in notes.items()}
            reviews: dict[int, TradeReviewV1] = {}
            for portfolio_id in ids:
                reviews.update(self._review_portfolio(portfolio_id, reader, latest))
            return reviews
        finally:
            reader.close()

    def _review_portfolio(
        self,
        portfolio_id: int,
        reader: EvidenceReader,
        latest: dict[int, TradeAnnotationV1],
    ) -> dict[int, TradeReviewV1]:
        trades, traces, open_ids = self._realised.fifo_lots(portfolio_id)
        history = self._strategies.history(portfolio_id)
        by_id = {t.id: t for t in trades if t.id is not None}
        today = self._today()
        reviews: dict[int, TradeReviewV1] = {}
        for trade_id, trade in by_id.items():
            lot_open = trade_id in open_ids
            try:
                if trade.action == "BUY":
                    reviews[trade_id] = review_buy(
                        trade,
                        reader,
                        annotation=latest.get(trade_id),
                        lot_open=lot_open,
                        history=history,
                        today=today,
                    )
                    continue
                trace = traces.get(trade_id)
                lots = trace.candidate_lots if trace else []
                entries = [
                    Entry(by_id[lot.trade_id], latest.get(lot.trade_id))
                    for lot in lots
                    if lot.trade_id in by_id
                ]
                reviews[trade_id] = review_sell(
                    trade, reader, entries=entries, history=history
                )
            except Exception:
                logger.warning(
                    "Trade review failed for trade %s", trade_id, exc_info=True
                )
                reviews[trade_id] = failed_review(trade, lot_open=lot_open)
        return reviews
