"""Real-stack tests for the read-only AI Desk view model (GH-21).

Every store lives under ``tmp_path``: the ledger (``TraderAgent(db_path=...)``,
never the default), theses, notifications, FX quotes, the published
artifact and the copilot audit log.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.repositories import db
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.position_theses_repo import PositionThesesRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.portfolio_recommendation import NO_ASSIGNMENT
from app.schemas.trade_review import ReviewRuleV1, TradeCheckV1, TradeReviewV1
from app.services import portfolio_service as portfolio_service_module
from app.services.desk_service import (
    AGENT_BOUNDARY,
    AUDIT_TAIL_BYTES,
    DeskService,
    DeskView,
    raised_by_label,
)
from app.services.gbp_valuation_service import GbpValuationService
from app.services.portfolio_service import PortfolioService
from app.services.position_thesis_service import PositionThesisService
from app.services.trade_review_service import TradeReviewService
from app.services.trader_service import TraderService
from tests.test_portfolio_agents_route import _result, _seed_thesis
from tests.test_portfolio_risk_route import _record
from tests.test_thesis_evaluator import make_record


class FakeReviews:
    """A trade-review cache that is warm (``cached``) or cold (``None``)."""

    def __init__(self, cached: dict[int, TradeReviewV1] | None = None) -> None:
        self.cached = cached

    def cached_reviews(self) -> dict[int, TradeReviewV1] | None:
        return self.cached

    def reviews(self) -> dict[int, TradeReviewV1]:
        raise AssertionError("the Desk must never compute a cold review scan")


def _raise(*_args: object) -> object:
    raise RuntimeError("boom")


@pytest.fixture
def desk_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """AAA (50%, a stop) and BBB (unpriced) in SIPP; a Sell on AAA and an
    exit-evidence gap on BBB; a fresh published run; empty side stores."""
    agent = TraderAgent(db_path=tmp_path / "trades.db")
    pf = agent.create_portfolio("SIPP")
    agent.record_buy("AAA", 10, 90.0, "2026-01-02", stop_loss=85.0, portfolio_id=pf.id)
    agent.record_buy("BBB", 5, 10.0, "2026-01-02", portfolio_id=pf.id)
    agent.set_cash_balance(1000.0, pf.id)
    agent.save_price_cache({"AAA": 100.0}, {"AAA": (100.0, "GBP")})
    trader = TraderService(agent)
    fx = FxQuoteRepository(db.make_connect(lambda: tmp_path / "fx.db"))
    service = PortfolioService(trader, gbp_valuation=GbpValuationService(fx))
    monkeypatch.setattr(
        service, "load_analysis", lambda: [_record("AAA", 100.0, 80.0, "Energy")]
    )
    artifact = tmp_path / "analysis.json"
    artifact.write_text(
        json.dumps(
            build_analysis_payload([], run_id="run-0", generated_at=datetime.now(UTC))
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(portfolio_service_module, "ANALYSIS_JSON", artifact)
    theses = PositionThesisService(
        PositionThesesRepository(db.make_connect(lambda: agent.db_path)),
        trader,
        artifact,
    )
    notifications = NotificationsRepository(
        db.make_connect(lambda: str(tmp_path / "notifications.db"))
    )
    notifications.ensure_schema()
    stack = SimpleNamespace(
        agent=agent,
        pid=pf.id,
        trader=trader,
        service=service,
        artifact=artifact,
        theses=theses,
        notifications=notifications,
        reviews=FakeReviews(),
        audit=tmp_path / "copilot_audit.jsonl",
        recommend=lambda _pid: _result(),
        statuses=theses.statuses,
    )
    stack.desk = lambda: DeskService(
        trader,
        service,
        lambda pid: stack.recommend(pid),
        lambda pid: stack.statuses(pid),
        notifications,
        cast(TradeReviewService, stack.reviews),
        stack.audit,
        dict,
    )
    return stack


def _view(stack: SimpleNamespace, item: str | None = None) -> DeskView:
    return stack.desk().view(stack.pid, item)


def _rail(view: DeskView) -> dict[str, tuple[str, str]]:
    return {w.name: (w.state, w.detail) for w in view.workflows}


def test_the_queue_is_18s_queue_with_deterministic_chips(desk_stack) -> None:
    first, second = _view(desk_stack), _view(desk_stack)

    assert [d.item for d in first.items] == list(first.queue.items)
    assert [(d.item.id, d.chip) for d in first.items] == [
        (d.item.id, d.chip) for d in second.items
    ]
    assert [d.chip for d in first.items] == ["Risk", "Risk", "Risk", "Risk", "Review"]
    assert first.queue.urgent_count == 5
    assert first.chip_counts == {"Risk": 4, "Limited": 0, "Review": 1, "Ready": 0}


def test_the_risk_strip_reads_the_view(desk_stack) -> None:
    view = _view(desk_stack)

    assert view.agent.open_risk.text == "£150.00 · 7.5%"
    assert view.agent.risk_findings is not None and view.agent.risk_findings >= 3
    # AAA's exit evidence is complete; BBB's is 166 / 200.
    assert view.evidence_complete == (1, 2)


def test_the_first_item_is_selected_and_facts_trace_the_item(desk_stack) -> None:
    view = _view(desk_stack)
    selected = view.selected

    assert selected is not None and selected is view.items[0]
    assert not view.missing_item
    facts = dict(selected.facts)
    assert facts["Portfolio"] == "SIPP"
    assert facts["Severity"] == "High"
    assert facts["Events"] == str(len(selected.events))
    assert set(selected.item.evidence) <= set(selected.evidence)
    assert ("Open Portfolio", "tab", "tab-portfolio") in [
        (a.label, a.kind, a.target) for a in selected.actions
    ]


def test_a_deep_linked_item_is_selected(desk_stack) -> None:
    target = _view(desk_stack).items[-1].item.id

    view = _view(desk_stack, target)

    assert view.selected is not None and view.selected.item.id == target
    assert not view.missing_item


def test_an_unknown_item_selects_the_first_with_a_note(desk_stack) -> None:
    view = _view(desk_stack, "attention:gone")

    assert view.missing_item
    assert view.selected is view.items[0]


def test_a_run_wide_thesis_warning_is_one_system_review_item(desk_stack) -> None:
    desk_stack.recommend = lambda _pid: NO_ASSIGNMENT
    desk_stack.notifications.record(
        NotificationCategory.ALERT,
        "thesis_evaluation_failed",
        "Position thesis evaluation failed",
        severity=NotificationSeverity.WARNING,
        body="Thesis evaluation failed for portfolio ids: 1",
        run_id="run-0",
    )
    before = _view(desk_stack)

    (item,) = [d for d in before.items if d.item.raised_by == "System"]
    assert item.chip == "Review"
    assert item.item.portfolio_id is None
    assert item.evidence[0].kind == "notification"
    assert item.evidence[0].source == "alert"
    # Run-wide: listed, never counted urgent.
    desk_stack.notifications.dismiss(desk_stack.notifications.recent()[0].id)
    assert before.queue.urgent_count == _view(desk_stack).queue.urgent_count


def test_alert_and_source_notifications_are_not_duplicated(desk_stack) -> None:
    before = len(_view(desk_stack).items)
    desk_stack.notifications.record(
        NotificationCategory.ALERT,
        "stop_loss_hit",
        "Stop loss hit — AAA",
        severity=NotificationSeverity.WARNING,
        ticker="AAA",
    )
    desk_stack.notifications.record(
        NotificationCategory.SOURCE,
        "source_failed",
        "Source failed — StockTwits",
        severity=NotificationSeverity.ERROR,
        run_id="run-0",  # the published run: its source-health events cover it
    )
    desk_stack.notifications.record(
        NotificationCategory.REFRESH, "refresh_done", "Refresh complete"
    )

    assert len(_view(desk_stack).items) == before


def test_a_job_notification_links_to_its_run_page(desk_stack) -> None:
    desk_stack.notifications.upsert_job_notification(
        job_id="job-1",
        job_status_version=3,
        category=NotificationCategory.BACKTEST,
        event_type="strategy_job_failed",
        severity=NotificationSeverity.ERROR,
        title="Backtest failed",
        body="Status: failed.",
        created_at=datetime.now(UTC),
        target_url="/strategy-manager/activities/job-1",
        actions=(),
    )

    (item,) = [d for d in _view(desk_stack).items if d.item.raised_by == "Backtest"]

    assert [(a.label, a.target) for a in item.actions] == [
        ("Open run", "/strategy-manager/activities/job-1")
    ]


def test_an_invalidated_thesis_links_to_its_editor(desk_stack) -> None:
    _seed_thesis(
        desk_stack, {"kind": "close_below_sma", "period": 50}, make_record(price=94.0)
    )

    (item,) = [d for d in _view(desk_stack).items if len(d.events) == 2]

    assert (
        "Open thesis editor",
        "thesis",
        f"/portfolios/{desk_stack.pid}/theses/AAA",
    ) in [(a.label, a.kind, a.target) for a in item.actions]
    assert _rail(_view(desk_stack))["Thesis monitor"] == (
        "Live",
        "1 active · 1 invalidated",
    )


def test_the_rail_reads_each_source(desk_stack) -> None:
    rail = _rail(_view(desk_stack))

    assert rail["Portfolio advisor"] == ("Live", "1 Sell")
    assert rail["Research copilot"] == ("Live", "0 questions in 7 days")
    assert rail["Thesis monitor"] == ("Live", "0 active · 0 invalidated")
    assert rail["Portfolio risk"][0] == "Live"
    assert rail["Alert triage"] == ("Live", "5 items · 5 urgent")
    assert rail["Strategy experiments"] == ("Not built yet — #15", "")
    assert rail["Data recovery"] == ("Not built yet — planned", "")
    assert len(rail) == 8


def test_no_strategy_is_declared_in_the_rail(desk_stack) -> None:
    desk_stack.recommend = lambda _pid: NO_ASSIGNMENT

    assert _rail(_view(desk_stack))["Portfolio advisor"] == (
        "Live",
        "No Strategy assigned",
    )


def test_a_cold_trade_review_cache_links_to_history(desk_stack) -> None:
    (workflow,) = [w for w in _view(desk_stack).workflows if w.name == "Trade review"]

    assert (workflow.state, workflow.detail) == ("Not computed yet", "")
    assert workflow.action is not None
    assert (workflow.action.label, workflow.action.target) == (
        "Open History",
        "tab-history",
    )


def _review(pid: int, day: date, *statuses: str) -> TradeReviewV1:
    rule = ReviewRuleV1(id="r", wording="w")
    return TradeReviewV1(
        trade_id=1,
        portfolio_id=pid,
        ticker="AAA",
        action="BUY",
        trade_date=day,
        checks=tuple(
            TradeCheckV1.model_validate(
                {"kind": "valid_setup", "status": s, "rule": rule, "calculation": "c"}
            )
            for s in statuses
        ),
    )


def test_a_warm_trade_review_cache_counts_this_weeks_deviations(desk_stack) -> None:
    today = datetime.now(UTC).date()
    desk_stack.reviews.cached = {
        1: _review(desk_stack.pid, today, "deviated", "followed", "deviated"),
        2: _review(desk_stack.pid, today - timedelta(days=14), "deviated"),
        3: _review(desk_stack.pid + 1, today, "deviated"),
    }

    assert _rail(_view(desk_stack))["Trade review"] == (
        "Live",
        "2 deviations this week",
    )


def test_each_failing_source_is_unavailable_alone(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk_stack.service, "risk_report", _raise)
    monkeypatch.setattr(desk_stack.notifications, "recent_warnings", _raise)
    desk_stack.statuses = _raise
    desk_stack.recommend = _raise
    desk_stack.reviews.cached_reviews = _raise
    desk_stack.audit.mkdir()  # a directory: reading it raises OSError

    view = _view(desk_stack)
    rail = _rail(view)

    for name in (
        "Portfolio advisor",
        "Research copilot",
        "Thesis monitor",
        "Portfolio risk",
        "Trade review",
    ):
        assert rail[name] == ("Unavailable", ""), name
    assert rail["Alert triage"][0] == "Live"
    assert "Notifications" in view.agent.unavailable
    assert "Risk unavailable" in view.agent.unavailable


def _audit(path: Path, *stamps: datetime, junk: bool = False) -> None:
    lines = [
        json.dumps({"timestamp": s.isoformat(), "status": "answered"}) for s in stamps
    ]
    if junk:
        lines.append("not json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_copilot_questions_are_counted_for_seven_days(desk_stack) -> None:
    now = datetime.now(UTC)
    _audit(desk_stack.audit, now - timedelta(days=9), now, now, junk=True)

    assert _rail(_view(desk_stack))["Research copilot"] == (
        "Live",
        "2 questions in 7 days",
    )


def test_a_cut_audit_tail_is_a_lower_bound(desk_stack) -> None:
    now = datetime.now(UTC)
    line = json.dumps({"timestamp": now.isoformat(), "pad": "x" * 1000})
    count = AUDIT_TAIL_BYTES // len(line) + 5
    desk_stack.audit.write_text((line + "\n") * count, encoding="utf-8")

    state, detail = _rail(_view(desk_stack))["Research copilot"]

    assert state == "Live"
    assert detail.endswith("+ questions in 7 days")
    assert int(detail.split("+")[0]) < count


def test_an_empty_queue_has_no_items_or_selection(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk_stack.trader, "list_portfolios", lambda: [])

    view = _view(desk_stack)

    assert view.items == () and view.selected is None
    assert view.portfolio_id is None


def test_the_attention_count_is_the_urgent_count(desk_stack) -> None:
    assert desk_stack.desk().attention_count(desk_stack.pid) == 5


# --- GH-21 review fixes -----------------------------------------------------


def _desk_at(stack: SimpleNamespace, clock: list[datetime]) -> DeskService:
    """A Desk on a controllable clock with its own bell count cache."""
    return DeskService(
        stack.trader,
        stack.service,
        stack.recommend,
        stack.statuses,
        stack.notifications,
        cast(TradeReviewService, stack.reviews),
        stack.audit,
        dict,
        now=lambda: clock[0],
        count_cache={},
    )


def test_two_badge_polls_within_the_ttl_compute_once(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    agent_view = desk_stack.service.agent_view

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(args[0])
        return agent_view(*args, **kwargs)

    monkeypatch.setattr(desk_stack.service, "agent_view", spy)
    clock = [datetime.now(UTC)]
    desk = _desk_at(desk_stack, clock)

    assert desk.attention_count(desk_stack.pid) == 5
    clock[0] += timedelta(seconds=20)
    assert desk.attention_count(desk_stack.pid) == 5
    assert len(calls) == 1
    # A new trade changes the key; the TTL expiring recomputes too.
    desk_stack.agent.record_buy(
        "CCC", 1, 1.0, "2026-01-03", portfolio_id=desk_stack.pid
    )
    desk.attention_count(desk_stack.pid)
    assert len(calls) == 2
    clock[0] += timedelta(seconds=61)
    desk.attention_count(desk_stack.pid)
    assert len(calls) == 3


def _note(
    stack: SimpleNamespace,
    title: str,
    severity: NotificationSeverity = NotificationSeverity.ERROR,
    **kwargs: object,
) -> int:
    return stack.notifications.record(
        kwargs.pop("category", NotificationCategory.PORTFOLIO),
        str(kwargs.pop("event_type", "import_failed")),
        title,
        severity=severity,
        **kwargs,
    )


def test_a_portfolio_scoped_error_is_listed_but_never_urgent(desk_stack) -> None:
    _note(desk_stack, "Import rejected", portfolio_id=desk_stack.pid)

    view = _view(desk_stack)

    (item,) = [d for d in view.items if d.item.title == "Import rejected"]
    assert item.item.portfolio_id == desk_stack.pid
    assert item.chip == "Review"
    assert view.queue.urgent_count == 5
    assert desk_stack.desk().attention_count(desk_stack.pid) == 5


def test_each_notification_is_its_own_item_with_its_severity(desk_stack) -> None:
    for job, severity in (
        ("job-1", NotificationSeverity.ERROR),
        ("job-2", NotificationSeverity.WARNING),
    ):
        desk_stack.notifications.upsert_job_notification(
            job_id=job,
            job_status_version=1,
            category=NotificationCategory.BACKTEST,
            event_type="strategy_job_failed",
            severity=severity,
            title=f"Backtest {job} failed",
            body="Status: failed.",
            created_at=datetime.now(UTC),
            target_url=f"/strategy-manager/activities/{job}",
            actions=(),
        )

    items = [d for d in _view(desk_stack).items if d.item.raised_by == "Backtest"]

    assert sorted(d.item.title for d in items) == [
        "Backtest job-1 failed",
        "Backtest job-2 failed",
    ]
    by_title = {d.item.title: d for d in items}
    one, two = by_title["Backtest job-1 failed"], by_title["Backtest job-2 failed"]
    assert dict(one.facts)["Notification severity"] == "Error"
    assert dict(two.facts)["Notification severity"] == "Warning"
    assert [a.target for a in one.actions] == ["/strategy-manager/activities/job-1"]


def test_an_in_window_error_is_not_truncated_by_newer_info_rows(desk_stack) -> None:
    _note(desk_stack, "Import rejected")
    for n in range(205):
        _note(desk_stack, f"info {n}", NotificationSeverity.INFO)
    desk_stack.notifications.upsert_job_notification(
        job_id="old",
        job_status_version=1,
        category=NotificationCategory.BACKTEST,
        event_type="strategy_job_failed",
        severity=NotificationSeverity.ERROR,
        title="Too old",
        body="",
        created_at=datetime.now(UTC) - timedelta(days=8),
        target_url=None,
        actions=(),
    )

    titles = [d.item.title for d in _view(desk_stack).items]
    since = datetime.now(UTC) - timedelta(days=7)
    rows = desk_stack.notifications.recent_warnings(since)

    assert "Import rejected" in titles and "Too old" not in titles
    assert [n.title for n in rows] == ["Import rejected"]


def test_source_notifications_from_an_unpublished_run_are_listed(
    desk_stack,
) -> None:
    for run in ("run-0", "run-failed"):
        _note(
            desk_stack,
            f"Source failed in {run}",
            category=NotificationCategory.SOURCE,
            event_type="source_failed",
            run_id=run,
        )

    titles = [d.item.title for d in _view(desk_stack).items]

    assert "Source failed in run-failed" in titles
    assert "Source failed in run-0" not in titles


def test_a_deep_link_from_an_earlier_run_selects_the_same_item(desk_stack) -> None:
    target = _view(desk_stack).items[-1].item.id
    earlier = target.replace("attention:run-0:", "attention:run-earlier:", 1)

    view = _view(desk_stack, earlier)

    assert view.selected is not None and view.selected.item.id == target
    assert not view.missing_item
    assert _view(desk_stack, "attention:run-earlier:9:exit:ZZZ").missing_item


def test_raised_by_uses_human_labels() -> None:
    assert raised_by_label("risk_coach") == "Portfolio risk"
    assert raised_by_label("risk_coach, strategy") == "Portfolio risk and Strategy"
    assert (
        raised_by_label("pipeline, strategy, thesis_monitor")
        == "System, Strategy and Thesis monitor"
    )
    assert raised_by_label("Backtest") == "Backtest"


def test_the_inspector_shows_raised_by_as_human_labels(desk_stack) -> None:
    facts = [dict(d.facts)["Raised by"] for d in _view(desk_stack).items]

    assert not [f for f in facts if "_" in f]
    assert "Portfolio risk" in facts


def test_chip_counts_partition_the_list(desk_stack) -> None:
    _note(desk_stack, "Import rejected")
    view = _view(desk_stack)

    assert list(view.chip_counts) == ["Risk", "Limited", "Review", "Ready"]
    assert sum(view.chip_counts.values()) == len(view.items)


def test_the_boundary_wording_says_the_desk_changes_nothing_itself() -> None:
    assert "The Desk itself changes nothing" in AGENT_BOUNDARY
    assert "open the screens where you act" in AGENT_BOUNDARY
    assert "nothing here changes" not in AGENT_BOUNDARY


def _review_service(ids: list[int]) -> TradeReviewService:
    trader = SimpleNamespace(
        list_portfolios=lambda: [SimpleNamespace(id=i) for i in ids],
        get_trade_revisions=lambda pids: {p: 1 for p in pids},
    )
    return TradeReviewService(
        cast(TraderService, trader),
        cast(Any, None),
        cast(Any, SimpleNamespace(revision=lambda: 1)),
        cast(Any, SimpleNamespace(history_revision=lambda: 1)),
        store_revision=lambda: 1,
        today=lambda: date(2026, 9, 29),
    )


def test_cached_reviews_never_waits_on_a_compute() -> None:
    service = _review_service([1])
    key = service._key([1])
    assert key is not None
    assert service.cached_reviews() is None  # cold
    service._cache[key] = {}

    with service._lock:  # a compute in progress holds the lock
        assert service.cached_reviews() is None

    assert service.cached_reviews() == {}
