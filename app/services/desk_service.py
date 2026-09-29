"""Read-only view model for the AI Desk screen (GH-21).

The Desk renders #18's attention queue for one portfolio -- built by
``PortfolioService.agent_view`` exactly as the Portfolio tab builds it, plus
recent warning notifications -- with a workflows rail, a risk strip and an
evidence inspector. Every figure comes from a typed source or a cheap read;
a failing source is shown unavailable, never guessed. Nothing here writes,
enqueues a job or calls an LLM, and a cold trade-review cache is never
computed.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from app.agents.triage.sources import NOTIFICATION_WINDOW, notification_events
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.attention import (
    AttentionCategory,
    AttentionEventV1,
    AttentionItemV1,
    AttentionQueueV1,
    run_key,
)
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.notification import Notification
from app.schemas.position_thesis import ThesisSummary
from app.schemas.source_health import SourceHealth, SourceName
from app.schemas.trade_review import iso_week
from app.services.freshness_service import Freshness, calculate_freshness
from app.services.portfolio_agent_view import (
    STRATEGY_UNAVAILABLE,
    PortfolioAgentView,
    RecommendationOutcome,
)
from app.services import portfolio_service as portfolio_module
from app.services.portfolio_service import NotificationLoader, PortfolioService
from app.services.trade_review_service import TradeReviewService
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)

Chip = Literal["Risk", "Limited", "Review", "Ready"]
ActionKind = Literal["tab", "href", "thesis"]

CHIPS: dict[AttentionCategory, Chip] = {
    "held_risk": "Risk",
    "exit": "Risk",
    "evidence": "Review",
    "new_setup": "Ready",
}
CHIP_TONES: dict[Chip, str] = {
    "Risk": "risk",
    "Limited": "warn",
    "Review": "info",
    "Ready": "good",
}
CATEGORY_LABELS: dict[AttentionCategory, str] = {
    "held_risk": "Held-position risk",
    "exit": "Exit",
    "evidence": "Evidence",
    "new_setup": "New setup",
}
ORDERING_NOTE = (
    "Ordered by deterministic risk policy: held-position risk, exits, "
    "evidence, then new setups."
)
AGENT_BOUNDARY = (
    "The Desk itself changes nothing: agents explain and propose, typed "
    "services calculate, and its links open the screens where you act "
    "(the thesis editor there can save or draft)."
)
CONTEXT_NOTE = (
    "Desk is read-only; its links open where you act · No broker connection · "
    "Job approvals arrive with Strategy experiments"
)
MISSING_ITEM = "That item is no longer in the queue; showing the first item."
UNAVAILABLE = "Unavailable"
LIVE = "Live"
NOT_COMPUTED = "Not computed yet"
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
#: The tab each source's items are acted on from today.
_RAISED_BY_TABS: dict[str, str] = {
    "risk_coach": "tab-portfolio",
    "strategy": "tab-portfolio",
    "thesis_monitor": "tab-portfolio",
    "alert_agent": "tab-portfolio",
    "pipeline": "tab-runlog",
    "scanner": "tab-stock-scanner",
    "trade_review": "tab-history",
}
TAB_LABELS: dict[str, str] = {
    "tab-portfolio": "Open Portfolio",
    "tab-runlog": "Open Run Log",
    "tab-stock-scanner": "Open Stock Scanner",
    "tab-history": "Open Trade History",
    "tab-strategy-manager": "Open Strategy Manager",
}
#: How long the bell's attention count is reused for unchanged inputs.
COUNT_TTL = timedelta(seconds=60)
#: The bell's cached counts, shared by every request's DeskService.
ATTENTION_COUNTS: dict[tuple[object, ...], tuple[datetime, int]] = {}
_COUNTS_LOCK = threading.Lock()
#: How far back the rail counts copilot questions.
COPILOT_WINDOW = timedelta(days=7)
#: Bytes of the copilot audit log read from its end (one line per question).
AUDIT_TAIL_BYTES = 1 << 20


@dataclass(frozen=True)
class DeskAction:
    """A link to where the user acts on an item today; never a mutation."""

    label: str
    kind: ActionKind
    target: str


@dataclass(frozen=True)
class DeskItem:
    """One queue item with its chip, original events, evidence and links."""

    item: AttentionItemV1
    chip: Chip
    events: tuple[AttentionEventV1, ...]
    evidence: tuple[EvidenceRefV1, ...]
    facts: tuple[tuple[str, str], ...]
    actions: tuple[DeskAction, ...]

    @property
    def tone(self) -> str:
        """The chip's colour tone."""
        return CHIP_TONES[self.chip]


@dataclass(frozen=True)
class Workflow:
    """One rail row: a capability, its state and its current reading."""

    name: str
    state: str
    detail: str = ""
    action: DeskAction | None = None


@dataclass(frozen=True)
class DeskView:
    """Everything the Desk partial renders for one portfolio."""

    portfolios: tuple[tuple[int, str], ...]
    portfolio_id: int | None
    agent: PortfolioAgentView
    items: tuple[DeskItem, ...]
    selected: DeskItem | None
    missing_item: bool
    workflows: tuple[Workflow, ...]
    freshness: Freshness
    evidence_complete: tuple[int, int]

    @property
    def queue(self) -> AttentionQueueV1:
        """#18's queue the items were built from."""
        return self.agent.attention

    @property
    def chip_counts(self) -> dict[Chip, int]:
        """Items per chip, in chip order: a partition of the list."""
        return {chip: sum(i.chip == chip for i in self.items) for chip in CHIP_TONES}


class DeskService:
    """Assemble the AI Desk from the agents' existing outputs (read-only)."""

    def __init__(
        self,
        trader: TraderService,
        portfolio: PortfolioService,
        recommend: Callable[[int], RecommendationOutcome],
        theses: Callable[[int], Mapping[str, ThesisSummary]],
        notifications: NotificationsRepository,
        trade_reviews: TradeReviewService,
        audit_path: Path,
        source_health: Callable[[], Mapping[SourceName, SourceHealth]],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        count_cache: dict[tuple[object, ...], tuple[datetime, int]] | None = None,
    ) -> None:
        self._trader = trader
        self._portfolio = portfolio
        self._recommend = recommend
        self._theses = theses
        self._notifications = notifications
        self._trade_reviews = trade_reviews
        self._audit_path = audit_path
        self._source_health = source_health
        self._now = now
        self._counts = {} if count_cache is None else count_cache

    def attention_count(self, portfolio_id: int | None) -> int:
        """The urgent attention count the masthead bell shows.

        Reused for ``COUNT_TTL`` while the portfolio, published artifact and
        trade ledgers are unchanged, so the 20s poll does not rebuild the
        agent view. Notifications are not read: they never count urgent.
        """
        now = self._now()
        key = self._count_key(portfolio_id)
        with _COUNTS_LOCK:
            hit = None if key is None else self._counts.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        count = self._portfolio.agent_view(
            portfolio_id, self._recommend, self._theses, self._source_health
        ).attention.urgent_count
        if key is not None:
            with _COUNTS_LOCK:
                for stale in [k for k, (end, _) in self._counts.items() if end <= now]:
                    del self._counts[stale]
                self._counts[key] = (now + COUNT_TTL, count)
        return count

    def _count_key(self, portfolio_id: int | None) -> tuple[object, ...] | None:
        """What the count depends on cheaply, or None (bypass the cache)."""
        try:
            ids = [p.id for p in self._trader.list_portfolios()]
            revisions = tuple(sorted(self._trader.get_trade_revisions(ids).items()))
            artifact = portfolio_module.ANALYSIS_JSON
            mtime = artifact.stat().st_mtime_ns if artifact.exists() else None
        except Exception:
            logger.warning("Attention count key unavailable", exc_info=True)
            return None
        return (portfolio_id, mtime, revisions)

    def view(self, portfolio_id: int | None, item_id: str | None = None) -> DeskView:
        """Build the Desk for ``portfolio_id`` with ``item_id`` selected (the
        first item when it is absent or no longer queued)."""
        notes: dict[str, Notification] = {}
        agent = self._agent_view(portfolio_id, notes)
        portfolios = tuple((p.id, p.name) for p in self._trader.list_portfolios())
        names = dict(portfolios)
        queue = agent.attention
        items = tuple(_desk_item(queue, i, names, notes) for i in queue.items)
        selected = _find(items, item_id, queue.analysis_run_id)
        return DeskView(
            portfolios=portfolios,
            portfolio_id=agent.portfolio_id,
            agent=agent,
            items=items,
            selected=selected or (items[0] if items else None),
            missing_item=item_id is not None and selected is None,
            workflows=self._workflows(agent),
            freshness=calculate_freshness(agent.generated_at),
            evidence_complete=(
                sum(r.evidence.text == "Complete" for r in agent.rows),
                len(agent.rows),
            ),
        )

    def _agent_view(
        self, portfolio_id: int | None, notes: dict[str, Notification]
    ) -> PortfolioAgentView:
        """#18's queue through ``agent_view``, with notification events; the
        notifications read is kept in ``notes`` for the inspector's links."""
        return self._portfolio.agent_view(
            portfolio_id,
            self._recommend,
            self._theses,
            self._source_health,
            notification_loader(self._notifications, self._now(), notes),
        )

    def _workflows(self, agent: PortfolioAgentView) -> tuple[Workflow, ...]:
        """The eight rail rows, each read from its own source."""
        queue = agent.attention
        return (
            _advisor(agent.advisor),
            self._copilot(),
            _thesis(agent.theses),
            (
                Workflow("Portfolio risk", UNAVAILABLE)
                if agent.risk_findings is None
                else Workflow(
                    "Portfolio risk", LIVE, _plural(agent.risk_findings, "finding")
                )
            ),
            self._trade_review(agent.portfolio_id),
            Workflow(
                "Alert triage",
                LIVE,
                f"{_plural(len(queue.items), 'item')} · {queue.urgent_count} urgent",
            ),
            Workflow("Strategy experiments", "Not built yet — #15"),
            Workflow("Data recovery", "Not built yet — planned"),
        )

    def _copilot(self) -> Workflow:
        """Questions asked in the last 7 days, from the audit log's tail."""
        try:
            count, partial = _recent_questions(
                self._audit_path, self._now() - COPILOT_WINDOW
            )
        except OSError:
            logger.warning("Copilot audit unreadable", exc_info=True)
            return Workflow("Research copilot", UNAVAILABLE)
        plus = "+" if partial else ""
        return Workflow("Research copilot", LIVE, f"{count}{plus} questions in 7 days")

    def _trade_review(self, portfolio_id: int | None) -> Workflow:
        """Deviations this week from a warm review cache; a cold one is "Not
        computed yet" with a link to History, never a scan."""
        history = DeskAction("Open History", "tab", "tab-history")
        try:
            reviews = self._trade_reviews.cached_reviews()
        except Exception:
            logger.warning("Trade review cache unreadable", exc_info=True)
            return Workflow("Trade review", UNAVAILABLE)
        if reviews is None:
            return Workflow("Trade review", NOT_COMPUTED, "", history)
        week = iso_week(self._now().date())
        deviations = sum(
            check.status == "deviated"
            for review in reviews.values()
            if review.portfolio_id == portfolio_id and review.week == week
            for check in review.checks
        )
        return Workflow(
            "Trade review",
            LIVE,
            f"{_plural(deviations, 'deviation')} this week",
            history,
        )


def notification_loader(
    repo: NotificationsRepository,
    now: datetime,
    notes: dict[str, Notification] | None = None,
) -> NotificationLoader:
    """``agent_view``'s notification events: this window's warnings and
    errors, read by the repository's query; each one read is kept in
    ``notes`` by id when given (the Desk's links and facts)."""

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


def _find(
    items: Sequence[DeskItem], item_id: str | None, run_id: str | None
) -> DeskItem | None:
    """The item with ``item_id``, else the same item from another run (ids
    embed the run key, so a deep link survives a new published run)."""
    if not item_id:
        return None
    exact = next((d for d in items if d.item.id == item_id), None)
    if exact is not None or not item_id.startswith("attention:"):
        return exact
    prefix = f"attention:{run_key(run_id)}:"
    return next(
        (d for d in items if item_id.endswith(":" + d.item.id.removeprefix(prefix))),
        None,
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _advisor(advisor: str) -> Workflow:
    if advisor == STRATEGY_UNAVAILABLE:
        return Workflow("Portfolio advisor", UNAVAILABLE)
    return Workflow("Portfolio advisor", LIVE, advisor)


def _thesis(theses: Mapping[str, ThesisSummary] | None) -> Workflow:
    """Active theses and how many the current run found invalidated."""
    if theses is None:
        return Workflow("Thesis monitor", UNAVAILABLE)
    active = [s for s in theses.values() if s.active is not None]
    invalidated = sum(
        s.current and s.latest is not None and s.latest.status == "invalidated"
        for s in active
    )
    return Workflow(
        "Thesis monitor", LIVE, f"{len(active)} active · {invalidated} invalidated"
    )


def _recent_questions(path: Path, since: datetime) -> tuple[int, bool]:
    """Count audit lines since ``since`` in the log's last ``AUDIT_TAIL_BYTES``.

    Returns the count and whether it is a lower bound (the tail was cut
    while still inside the window). A missing log is zero questions.
    """
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - AUDIT_TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return 0, False
    cut = size > AUDIT_TAIL_BYTES
    stamps = [s for s in map(_timestamp, lines[1:] if cut else lines) if s]
    recent = sum(stamp >= since for stamp in stamps)
    # Cut while still inside the window: older questions lie beyond the tail.
    return recent, cut and recent == len(stamps)


def _timestamp(line: str) -> datetime | None:
    """One audit line's aware timestamp, or None if the line is malformed."""
    try:
        stamp = datetime.fromisoformat(json.loads(line)["timestamp"])
    except (ValueError, KeyError, TypeError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _desk_item(
    queue: AttentionQueueV1,
    item: AttentionItemV1,
    portfolios: Mapping[int, str],
    notes: Mapping[str, Notification],
) -> DeskItem:
    """One item with its chip, facts, full provenance and action links."""
    events = tuple(queue.events_for(item))
    chip: Chip = "Limited" if all(e.stale for e in events) else CHIPS[item.kind]
    return DeskItem(
        item=item,
        chip=chip,
        events=events,
        evidence=tuple(
            dict.fromkeys([*item.evidence, *(r for e in events for r in e.evidence)])
        ),
        facts=_facts(item, events, portfolios, notes),
        actions=_actions(events, notes),
    )


def _facts(
    item: AttentionItemV1,
    events: Sequence[AttentionEventV1],
    portfolios: Mapping[int, str],
    notes: Mapping[str, Notification],
) -> tuple[tuple[str, str], ...]:
    """The inspector's fact table; a notification item adds its own
    severity (warning or error)."""
    portfolio = (
        "All portfolios (run-wide)"
        if item.portfolio_id is None
        else portfolios.get(item.portfolio_id, f"Portfolio {item.portfolio_id}")
    )
    observed = item.observed_at
    severities = [
        notes[r.id].severity.value.title()
        for r in item.evidence
        if r.kind == "notification" and r.id in notes
    ]
    facts = (
        ("Category", CATEGORY_LABELS[item.kind]),
        ("Severity", item.severity.title()),
        ("Portfolio", portfolio),
        ("Security", item.security_id or "—"),
        (
            "Observed at",
            observed.strftime("%Y-%m-%d %H:%M UTC") if observed else "Unknown",
        ),
        ("Evidence as of", item.as_of.isoformat() if item.as_of else "Unknown"),
        ("Raised by", raised_by_label(item.raised_by)),
        ("Events", str(len(events))),
    )
    if not severities:
        return facts
    return (*facts, ("Notification severity", ", ".join(severities)))


def _actions(
    events: Sequence[AttentionEventV1], notes: Mapping[str, Notification]
) -> tuple[DeskAction, ...]:
    """Links to where each event is acted on today, in event order."""
    actions: list[DeskAction] = []
    for event in events:
        tab = _RAISED_BY_TABS.get(event.raised_by)
        if tab is not None:
            actions.append(DeskAction(TAB_LABELS[tab], "tab", tab))
        if event.kind == "thesis_invalidated" and event.portfolio_id is not None:
            security = quote(event.security_id or "", safe="")
            actions.append(
                DeskAction(
                    "Open thesis editor",
                    "thesis",
                    f"/portfolios/{event.portfolio_id}/theses/{security}",
                )
            )
        for ref in event.evidence:
            note = notes.get(ref.id) if ref.kind == "notification" else None
            if note is not None:
                actions.append(_notification_action(note))
    return tuple(dict.fromkeys(actions))


def _notification_action(note: Notification) -> DeskAction:
    """The run page a job notification points at, else its category's tab."""
    if note.target_url:
        return DeskAction("Open run", "href", note.target_url)
    tab = note.deep_link_tab
    return DeskAction(TAB_LABELS.get(tab, "Open"), "tab", tab)
