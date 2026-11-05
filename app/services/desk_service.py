"""The Desk service: Today's three projections over the triage queue.

Today (GH-128, replacing the GH-21 AI Desk) renders #18's attention queue
for one portfolio -- built by ``PortfolioService.agent_view`` exactly as the
Portfolio tab builds it, plus recent warning notifications. The service
exposes exactly three projections, all from one cached build per input
revision (AD-2, AD-5):

* ``today_items(pid)`` -- the whole Today view;
* ``risk_items(pid)`` -- its held-position risks, for the Portfolio card;
* ``badge_count(pid)`` -- how many Today items need action.

Nothing here calls an LLM or computes a cold trade-review cache; the only
write is persisting a "Dismiss all" against each item's stable key.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.agents.triage.sources import NOTIFICATION_WINDOW, notification_events
from app.core.recommendation import buy_signals
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.attention import (
    AttentionCategory,
    AttentionEventV1,
    AttentionItemV1,
    AttentionQueueV1,
    run_key,
)
from app.schemas.notification import Notification
from app.schemas.position_thesis import ThesisSummary
from app.schemas.record import StockRecord
from app.schemas.source_health import SourceHealth, SourceName
from app.services import portfolio_service as portfolio_module
from app.services.portfolio_agent_view import (
    PortfolioAgentView,
    PortfolioSummary,
    RecommendationOutcome,
)
from app.services.portfolio_service import NotificationLoader, PortfolioService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

#: Each category's chip and tone; a stale-only item reads "Stale".
CHIPS: dict[AttentionCategory, tuple[str, str]] = {
    "held_risk": ("Risk", "risk"),
    "exit": ("Exit", "risk"),
    "evidence": ("Check", "info"),
    "new_setup": ("Setup", "good"),
}
#: Chips for rollups whose rule reads better than its category.
RULE_CHIPS: dict[str, tuple[str, str]] = {"no_stop": ("Stops", "warn")}
#: Human names for the queue's internal ``raised_by`` values; notification
#: events already carry their category label.
RAISED_BY_LABELS: dict[str, str] = {
    "risk_coach": "Portfolio risk",
    "strategy": "Strategy",
    "thesis_monitor": "Thesis monitor",
    "pipeline": "System",
    "scanner": "Scanner",
    "alert_agent": "Alerts",
    "trade_review": "Trade review",
}
#: Where each source's items are acted on: (label, shell page).
SOURCE_ACTIONS: dict[str, tuple[str, str]] = {
    "risk_coach": ("Review position", "/portfolio"),
    "alert_agent": ("Review position", "/portfolio"),
    "strategy": ("Review in Portfolio", "/portfolio"),
    "thesis_monitor": ("Review thesis", "/portfolio"),
    "pipeline": ("Open Run Log", "/runlog"),
    "scanner": ("Open Scanner", "/scanner"),
}
#: A rollup rule's own action, when it is clearer than its source's.
RULE_ACTIONS: dict[str, tuple[str, str]] = {"no_stop": ("Set stops", "/portfolio")}
PAGE_LABELS: dict[str, str] = {
    "/scanner": "Open Scanner",
    "/portfolio": "Open Portfolio",
    "/strategies": "Open Strategies",
    "/runlog": "Open Run Log",
}
#: Buy signals Today lists; the rest are one click away in the Scanner.
BUY_SIGNAL_LIMIT = 5
#: Longest a cached Today may be reused for inputs its revision does not
#: capture (prices, freshness labels).
DESK_MAX_AGE = timedelta(minutes=10)
#: How many revisions (portfolios x recent inputs) a cache keeps.
CACHE_SIZE = 16
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class TodayAction:
    """A link to the shell page where the user acts on an item."""

    label: str
    href: str


@dataclass(frozen=True)
class TodayItem:
    """One attention item as Today shows it; ``key`` is its stable id."""

    key: str
    kind: AttentionCategory
    chip: str
    tone: str
    title: str
    detail: str
    action: TodayAction | None
    #: The item's source events, hashed: a dismissal holds while it matches.
    fingerprint: str
    #: Provenance shown only behind ``macros.disclosure`` (AD-10).
    facts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BuySignal:
    """One BUY-family scanner row (AD-6)."""

    ticker: str
    label: str
    sector: str | None
    score: int | None
    price: float | None
    currency: str


@dataclass(frozen=True)
class TodayView:
    """Everything the Today partial renders for one portfolio."""

    portfolio_id: int | None
    needs_action: tuple[TodayItem, ...]
    worth_checking: tuple[TodayItem, ...]
    buy_signals: tuple[BuySignal, ...]
    #: Every BUY-family row, of which ``buy_signals`` is the head.
    buy_signal_count: int
    #: Queued new setups, which Today leaves to the Scanner.
    other_setups: int
    summary: PortfolioSummary | None
    unavailable: tuple[str, ...]
    generated_at: datetime | None
    run_id: str | None


#: Today views cached per input revision, shared by every request (AD-14).
TODAY_CACHE: OrderedDict[str, TodayView] = OrderedDict()


def file_revision(*paths: Path) -> tuple[tuple[int, int] | None, ...]:
    """Each file's ``(mtime_ns, size)``, or None when it is absent."""
    stats = (path.stat() if path.exists() else None for path in paths)
    return tuple(None if st is None else (st.st_mtime_ns, st.st_size) for st in stats)


class DeskService:
    """Today's projections from the agents' existing outputs."""

    def __init__(
        self,
        trader: TraderService,
        portfolio: PortfolioService,
        recommend: Callable[[int], RecommendationOutcome],
        theses: Callable[[int], Mapping[str, ThesisSummary]],
        notifications: NotificationsRepository,
        source_health: Callable[[], Mapping[SourceName, SourceHealth]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        cache: OrderedDict[str, TodayView] | None = None,
        input_revision: Callable[[], object] = lambda: None,
    ) -> None:
        self._trader = trader
        self._portfolio = portfolio
        self._recommend = recommend
        self._theses = theses
        self._notifications = notifications
        self._source_health = source_health
        self._now = now
        self._cache: OrderedDict[str, TodayView] = (
            OrderedDict() if cache is None else cache
        )
        self._input_revision = input_revision

    def today_items(self, portfolio_id: int | None) -> TodayView:
        """Today for ``portfolio_id``, reused while its revision holds."""
        key = self.revision(portfolio_id)
        if key is not None:
            with _CACHE_LOCK:
                hit = self._cache.get(key)
                if hit is not None:
                    self._cache.move_to_end(key)
                    return hit
        view = self._build(portfolio_id)
        if key is not None:
            with _CACHE_LOCK:
                self._cache[key] = view
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        return view

    def risk_items(self, portfolio_id: int | None) -> tuple[TodayItem, ...]:
        """Today's held-position risks: the Portfolio risk card's rows."""
        return tuple(
            i
            for i in self.today_items(portfolio_id).needs_action
            if i.kind == "held_risk"
        )

    def badge_count(self, portfolio_id: int | None) -> int:
        """The Today tab's badge: how many items need action."""
        return len(self.today_items(portfolio_id).needs_action)

    def dismiss_worth_checking(self, portfolio_id: int | None) -> int:
        """Persist a dismissal of every Worth checking item; return how many."""
        items = self.today_items(portfolio_id).worth_checking
        self._notifications.dismiss_attention((i.key, i.fingerprint) for i in items)
        return len(items)

    def revision(self, portfolio_id: int | None) -> str | None:
        """A short digest of Today's cheap inputs, or None (always rebuild).

        Covers the portfolio, the published artifact, the trade ledgers and
        portfolio names, the notifications table and dismissals,
        ``input_revision`` (theses, Strategy assignments, source health) and
        a ``DESK_MAX_AGE`` time bucket that bounds staleness from inputs the
        digest does not see (prices).
        """
        try:
            portfolios = self._trader.list_portfolios()
            ids = [p.id for p in portfolios]
            revisions = tuple(sorted(self._trader.get_trade_revisions(ids).items()))
            artifact = portfolio_module.ANALYSIS_JSON
            mtime = artifact.stat().st_mtime_ns if artifact.exists() else None
            names = tuple((p.id, p.name) for p in portfolios)
            notes = self._notifications.revision()
            dismissed = sorted(self._notifications.attention_dismissals().items())
            inputs = self._input_revision()
        except Exception:
            logger.warning("Today revision unavailable", exc_info=True)
            return None
        bucket = int(self._now().timestamp() // DESK_MAX_AGE.total_seconds())
        raw = repr(
            (portfolio_id, mtime, revisions, names, notes, dismissed, inputs, bucket)
        ).encode()
        return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]

    def _build(self, portfolio_id: int | None) -> TodayView:
        """Build Today from the agent view and the published artifact."""
        notes: dict[str, Notification] = {}
        agent = self._agent_view(portfolio_id, notes)
        queue = agent.attention
        dismissed = self._dismissals()
        pid = agent.portfolio_id
        checking = (_today_item(queue, i, notes, pid) for i in queue.worth_checking)
        try:
            signals = buy_signals(
                self._portfolio.load_analysis(), self._trader.held_tickers()
            )
        except Exception:
            logger.warning("Buy signals unavailable for Today", exc_info=True)
            signals = []
        return TodayView(
            portfolio_id=pid,
            needs_action=tuple(
                _today_item(queue, i, notes, pid) for i in queue.needs_action
            ),
            worth_checking=tuple(
                i for i in checking if dismissed.get(i.key) != i.fingerprint
            ),
            buy_signals=tuple(
                _buy_signal(r, rec.text) for r, rec in signals[:BUY_SIGNAL_LIMIT]
            ),
            buy_signal_count=len(signals),
            other_setups=sum(i.kind == "new_setup" for i in queue.items),
            summary=agent.summary,
            unavailable=agent.unavailable,
            generated_at=agent.generated_at,
            run_id=queue.analysis_run_id,
        )

    def _dismissals(self) -> Mapping[str, str]:
        """Persisted dismissals; none when the read fails (items stay shown)."""
        try:
            return self._notifications.attention_dismissals()
        except Exception:
            logger.warning("Attention dismissals unreadable", exc_info=True)
            return {}

    def _agent_view(
        self, portfolio_id: int | None, notes: dict[str, Notification]
    ) -> PortfolioAgentView:
        """#18's queue through ``agent_view``, with notification events; the
        notifications read is kept in ``notes`` for the items' links."""
        return self._portfolio.agent_view(
            portfolio_id,
            self._recommend,
            self._theses,
            self._source_health,
            notification_loader(self._notifications, self._now(), notes),
        )


def notification_loader(
    repo: NotificationsRepository,
    now: datetime,
    notes: dict[str, Notification] | None = None,
) -> NotificationLoader:
    """``agent_view``'s notification events: this window's warnings and
    errors, read by the repository's query; each one read is kept in
    ``notes`` by id when given (Today's links)."""

    def load(portfolio_id: int | None, run_id: str | None) -> list[AttentionEventV1]:
        recent = repo.recent_warnings(now - NOTIFICATION_WINDOW)
        if notes is not None:
            notes.update({str(n.id): n for n in recent})
        return notification_events(
            recent, portfolio_id=portfolio_id, now=now, published_run_id=run_id
        )

    return load


def raised_by_label(raised_by: str) -> str:
    """Human names for an item's or event's ``raised_by`` as a readable list,
    e.g. ``"risk_coach, strategy"`` -> ``"Portfolio risk and Strategy"``."""
    labels = list(
        dict.fromkeys(RAISED_BY_LABELS.get(n, n) for n in raised_by.split(", "))
    )
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def stable_key(item: AttentionItemV1, run_id: str | None) -> str:
    """The item's id without its analysis run, so a dismissal survives a new
    run that raises the same item; rollup ids are already run-free."""
    prefix = f"attention:{run_key(run_id)}:"
    if item.id.startswith(prefix):
        return "attention:" + item.id.removeprefix(prefix)
    return item.id


def fingerprint(events: Sequence[AttentionEventV1]) -> str:
    """A short, order-free, run-free digest of what the item says: each
    event's rule, security and title. A changed finding changes it."""
    parts = sorted(f"{e.kind}|{e.security_id or ''}|{e.title}" for e in events)
    raw = "\n".join(parts).encode()
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]


def _today_item(
    queue: AttentionQueueV1,
    item: AttentionItemV1,
    notes: Mapping[str, Notification],
    portfolio_id: int | None,
) -> TodayItem:
    """One queue item with its chip, one-line detail, action and provenance."""
    events = queue.events_for(item)
    rollup = item.id.startswith("rollup:")
    rule = events[0].kind if events else ""
    if events and all(e.stale for e in events):
        chip, tone = "Stale", "muted"
    else:
        chip, tone = (RULE_CHIPS.get(rule) if rollup else None) or CHIPS[item.kind]
    if rollup:
        detail = ", ".join(sorted({e.security_id or "" for e in events}))
    else:
        detail = events[0].summary if events else item.summary
    return TodayItem(
        key=stable_key(item, queue.analysis_run_id),
        kind=item.kind,
        chip=chip,
        tone=tone,
        title=item.title,
        detail=detail,
        action=_action(events, rollup, notes, portfolio_id),
        fingerprint=fingerprint(events),
        facts=(
            ("Why it is here", item.summary),
            ("Raised by", raised_by_label(item.raised_by)),
            ("Evidence as of", item.as_of.isoformat() if item.as_of else "Unknown"),
            ("Signals", str(len(events))),
            ("Analysis run", queue.analysis_run_id or "Unknown"),
        ),
    )


def _action(
    events: Sequence[AttentionEventV1],
    rollup: bool,
    notes: Mapping[str, Notification],
    portfolio_id: int | None,
) -> TodayAction | None:
    """The one link for an item: a notification's run or category page,
    else its rollup rule's, else its first source's page."""
    if not events:
        return None
    lead = events[0]
    note = next(
        (
            notes[r.id]
            for r in lead.evidence
            if r.kind == "notification" and r.id in notes
        ),
        None,
    )
    if note is not None:
        if note.target_url:
            return TodayAction("Open run", note.target_url)
        return TodayAction(PAGE_LABELS.get(note.deep_link, "Open"), note.deep_link)
    found = (RULE_ACTIONS.get(lead.kind) if rollup else None) or SOURCE_ACTIONS.get(
        lead.raised_by
    )
    if found is None:
        return None
    label, page = found
    if page == "/portfolio" and portfolio_id is not None:
        page = f"/portfolio?portfolio_id={portfolio_id}"
    return TodayAction(label, page)


def _buy_signal(record: StockRecord, label: str) -> BuySignal:
    """A scanner record's Today row."""
    analysis = record.analysis
    return BuySignal(
        ticker=record.ticker,
        label=label,
        sector=record.sector,
        score=analysis.score if analysis else None,
        price=record.price,
        currency=record.currency,
    )
