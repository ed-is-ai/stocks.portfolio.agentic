"""Real-stack tests for the Desk service's Today projections (GH-128).

Every store lives under ``tmp_path``: the ledger (``TraderAgent(db_path=...)``,
never the default), theses, notifications, FX quotes and the published
artifact.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.trader.trader_agent import TraderAgent
from app.repositories import db
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.position_theses_repo import PositionThesesRepository
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.portfolio_recommendation import NO_ASSIGNMENT
from app.schemas.position_thesis import ThesisContentV1
from app.services import portfolio_service as portfolio_service_module
from app.services import desk_service as desk_module
from app.services.desk_service import (
    DESK_MAX_AGE,
    DeskService,
    TodayView,
    file_revision,
    raised_by_label,
)
from app.services.gbp_valuation_service import GbpValuationService
from app.services.portfolio_service import PortfolioService
from app.services.position_thesis_service import PositionThesisService
from app.services.trader_service import TraderService
from tests.test_portfolio_agents_route import _result
from tests.test_portfolio_risk_route import _record
from tests.test_thesis_evaluator import make_record


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
        recommend=lambda _pid: _result(),
        statuses=theses.statuses,
    )
    stack.desk = lambda: DeskService(
        trader,
        service,
        lambda pid: stack.recommend(pid),
        lambda pid: stack.statuses(pid),
        notifications,
        dict,
    )
    return stack


def _today(stack: SimpleNamespace) -> TodayView:
    return stack.desk().today_items(stack.pid)


def _desk_at(
    stack: SimpleNamespace, clock: list[datetime], **kwargs: Any
) -> DeskService:
    """A Desk on a controllable clock with its own Today cache."""
    return DeskService(
        stack.trader,
        stack.service,
        stack.recommend,
        stack.statuses,
        stack.notifications,
        dict,
        now=lambda: clock[0],
        cache=OrderedDict(),
        **kwargs,
    )


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


def test_today_splits_the_queue_into_action_and_checking(desk_stack) -> None:
    view = _today(desk_stack)

    assert [i.kind for i in view.needs_action] == [
        "held_risk",
        "held_risk",
        "held_risk",
        "exit",
    ]
    assert [i.title for i in view.worth_checking] == [
        "BBB: exit evidence gap (166 / 200)"
    ]
    assert view.needs_action[0].action is not None
    assert (
        view.needs_action[0].action.href == f"/portfolio?portfolio_id={desk_stack.pid}"
    )
    # Provenance is carried for the disclosure, never in the title/detail.
    facts = dict(view.needs_action[0].facts)
    assert facts["Analysis run"] == "run-0"
    assert facts["Raised by"] == "Portfolio risk"


def test_one_projection_feeds_today_the_risk_card_and_the_badge(desk_stack) -> None:
    desk = desk_stack.desk()
    view = desk.today_items(desk_stack.pid)
    risks = desk.risk_items(desk_stack.pid)

    assert desk.badge_count(desk_stack.pid) == len(view.needs_action) == 4
    assert risks == tuple(i for i in view.needs_action if i.kind == "held_risk")
    # Each underlying risk appears once across Today (and so the card).
    keys = [i.key for i in (*view.needs_action, *view.worth_checking)]
    assert len(keys) == len(set(keys))


def test_today_is_deterministic(desk_stack) -> None:
    first, second = _today(desk_stack), _today(desk_stack)

    assert first == second


def test_new_setups_are_counted_for_the_scanner_never_listed(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        portfolio_service_module,
        "record_setups",
        lambda _records, _held: [("NEW", "breakout", "Breakout")],
    )

    view = _today(desk_stack)

    assert view.other_setups == 1
    listed = [i.title for i in (*view.needs_action, *view.worth_checking)]
    assert not any("NEW" in title for title in listed)


def test_buy_signals_are_the_buy_family_only(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    buy = make_record(price=50.0)
    monkeypatch.setattr(
        desk_module,
        "buy_signals",
        lambda records, held: [(r, SimpleNamespace(text="Buy")) for r in records],
    )
    monkeypatch.setattr(desk_stack.service, "load_analysis", lambda: [buy] * 7)

    view = _today(desk_stack)

    assert view.buy_signal_count == 7
    assert len(view.buy_signals) == desk_module.BUY_SIGNAL_LIMIT
    assert view.buy_signals[0].ticker == buy.ticker
    assert view.buy_signals[0].label == "Buy"


def test_the_portfolio_card_reads_the_agent_views_valuation(desk_stack) -> None:
    summary = _today(desk_stack).summary

    assert summary is not None
    assert summary.name == "SIPP"
    assert summary.cash_gbp == 1000.0
    assert summary.positions == 2
    # Only the priced holding (AAA) carries value and unrealised P&L.
    assert summary.value_gbp == 2000.0
    assert summary.unrealised_gbp == 100.0


def test_an_empty_ledger_is_an_empty_today(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk_stack.trader, "list_portfolios", lambda: [])

    view = _today(desk_stack)

    assert view.portfolio_id is None and view.needs_action == ()
    assert view.summary is None


def test_each_failing_source_is_declared_unavailable(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desk_stack.service, "risk_report", _raise)
    monkeypatch.setattr(desk_stack.notifications, "recent_warnings", _raise)
    desk_stack.statuses = _raise
    desk_stack.recommend = _raise

    view = _today(desk_stack)

    assert "Notifications" in view.unavailable
    assert "Risk unavailable" in view.unavailable


def test_a_repeat_badge_poll_with_unchanged_inputs_builds_once(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    agent_view = desk_stack.service.agent_view

    def spy(*args: object, **kwargs: object) -> object:
        calls.append(args[0])
        return agent_view(*args, **kwargs)

    monkeypatch.setattr(desk_stack.service, "agent_view", spy)
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    desk = _desk_at(desk_stack, clock)

    assert desk.badge_count(desk_stack.pid) == 4
    clock[0] += timedelta(seconds=30)
    desk.badge_count(desk_stack.pid)
    desk.today_items(desk_stack.pid)
    desk.risk_items(desk_stack.pid)
    assert len(calls) == 1
    # A new trade changes the revision; so does the time bucket.
    desk_stack.agent.record_buy(
        "CCC", 1, 1.0, "2026-01-03", portfolio_id=desk_stack.pid
    )
    desk.badge_count(desk_stack.pid)
    assert len(calls) == 2
    clock[0] += DESK_MAX_AGE
    desk.badge_count(desk_stack.pid)
    assert len(calls) == 3


def test_an_unavailable_revision_builds_uncached(
    desk_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = desk_stack.desk()
    monkeypatch.setattr(desk_stack.trader, "get_trade_revisions", _raise)

    assert desk.revision(desk_stack.pid) is None
    assert desk.badge_count(desk_stack.pid) == 4


def test_dismiss_all_hides_worth_checking_until_its_evidence_changes(
    desk_stack,
) -> None:
    desk = desk_stack.desk()
    before = desk.today_items(desk_stack.pid)

    assert desk.dismiss_worth_checking(desk_stack.pid) == 1

    after = desk.today_items(desk_stack.pid)
    assert after.worth_checking == ()
    assert after.needs_action == before.needs_action
    (item,) = before.worth_checking
    assert desk_stack.notifications.attention_dismissals() == {
        item.key: item.fingerprint
    }
    # A dismissal against older evidence (another fingerprint) no longer holds.
    desk_stack.notifications.dismiss_attention([(item.key, "older-evidence")])
    assert desk.today_items(desk_stack.pid).worth_checking == before.worth_checking


def test_a_run_wide_thesis_warning_is_worth_checking(desk_stack) -> None:
    desk_stack.recommend = lambda _pid: NO_ASSIGNMENT
    _note(
        desk_stack,
        "Position thesis evaluation failed",
        NotificationSeverity.WARNING,
        category=NotificationCategory.ALERT,
        event_type="thesis_evaluation_failed",
        run_id="run-0",
    )

    view = _today(desk_stack)

    (item,) = [i for i in view.worth_checking if "thesis" in i.title]
    assert item.action is not None and item.action.href == "/scanner"
    assert dict(item.facts)["Raised by"] == "System"


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

    (item,) = [
        i for i in _today(desk_stack).worth_checking if i.title == "Backtest failed"
    ]

    assert item.action is not None
    assert (item.action.label, item.action.href) == (
        "Open run",
        "/strategy-manager/activities/job-1",
    )


def test_a_portfolio_error_is_worth_checking_and_never_counted(desk_stack) -> None:
    _note(desk_stack, "Import rejected", portfolio_id=desk_stack.pid)
    desk = desk_stack.desk()

    titles = [i.title for i in desk.today_items(desk_stack.pid).worth_checking]

    assert "Import rejected" in titles
    assert desk.badge_count(desk_stack.pid) == 4


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

    titles = [i.title for i in _today(desk_stack).worth_checking]

    assert "Source failed in run-failed" in titles
    assert "Source failed in run-0" not in titles


def test_raised_by_uses_human_labels() -> None:
    assert raised_by_label("risk_coach") == "Portfolio risk"
    assert raised_by_label("risk_coach, strategy") == "Portfolio risk and Strategy"
    assert raised_by_label("Backtest") == "Backtest"


def test_the_revision_moves_on_a_trade_a_note_a_dismissal_and_a_bucket(
    desk_stack,
) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    desk = _desk_at(desk_stack, clock)
    first = desk.revision(desk_stack.pid)
    assert first is not None
    clock[0] += DESK_MAX_AGE / 2
    assert desk.revision(desk_stack.pid) == first

    desk_stack.agent.record_buy(
        "CCC", 1, 1.0, "2026-01-03", portfolio_id=desk_stack.pid
    )
    after_trade = desk.revision(desk_stack.pid)
    assert after_trade != first

    _note(desk_stack, "Import failed")
    after_note = desk.revision(desk_stack.pid)
    assert after_note != after_trade

    desk_stack.notifications.dismiss_attention([("k", "f")])
    after_dismiss = desk.revision(desk_stack.pid)
    assert after_dismiss != after_note

    clock[0] += DESK_MAX_AGE
    assert desk.revision(desk_stack.pid) != after_dismiss


def test_the_revision_moves_on_a_portfolio_rename(desk_stack) -> None:
    desk = _desk_at(desk_stack, [datetime(2026, 1, 1, tzinfo=UTC)])
    before = desk.revision(desk_stack.pid)

    assert desk_stack.agent.rename_portfolio(desk_stack.pid, "ISA")

    assert desk.revision(desk_stack.pid) != before


def test_the_revision_moves_on_a_thesis_change(desk_stack) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    desk = _desk_at(
        desk_stack,
        clock,
        input_revision=lambda: file_revision(Path(desk_stack.agent.db_path)),
    )
    before = desk.revision(desk_stack.pid)
    repo = PositionThesesRepository(db.make_connect(lambda: desk_stack.agent.db_path))
    content = ThesisContentV1.model_validate(
        {"rationale": "r", "expected_setup": "s", "rules": [{"kind": "stage_2_lost"}]}
    )

    repo.add_version(desk_stack.pid, "AAA", content, "user", active=True)

    assert desk.revision(desk_stack.pid) != before


def test_holdings_without_a_stop_are_one_rollup_with_one_action(desk_stack) -> None:
    for ticker in ("CCC", "DDD"):
        desk_stack.agent.record_buy(
            ticker, 1, 10.0, "2026-01-02", portfolio_id=desk_stack.pid
        )
    desk_stack.agent.save_price_cache(
        {"AAA": 100.0, "CCC": 10.0, "DDD": 10.0},
        {"AAA": (100.0, "GBP"), "CCC": (10.0, "GBP"), "DDD": (10.0, "GBP")},
    )
    desk = desk_stack.desk()

    view = desk.today_items(desk_stack.pid)

    (rollup,) = [i for i in view.needs_action if i.key.startswith("rollup:")]
    assert rollup.key == f"rollup:held_risk:no_stop:{desk_stack.pid}"
    assert rollup.title == "2 holdings have no evidenced stop"
    assert (rollup.chip, rollup.detail) == ("Stops", "CCC, DDD")
    assert rollup.action is not None and rollup.action.label == "Set stops"
    assert rollup in desk.risk_items(desk_stack.pid)
    assert desk.badge_count(desk_stack.pid) == len(view.needs_action)


def test_a_dismissal_key_and_fingerprint_survive_a_new_run() -> None:
    """Regression: a new analysis run re-raising the same finding keeps it
    dismissed; a changed finding brings it back."""
    from app.agents.triage.queue import build_attention_queue
    from app.agents.triage.sources import source_health_events
    from app.schemas.source_health import SourceHealth, SourceName, SourceState

    def keyed(run: str, state: SourceState) -> tuple[str, str]:
        health = {
            SourceName.STOCKTWITS: SourceHealth(
                source=SourceName.STOCKTWITS, state=state
            )
        }
        queue = build_attention_queue(
            source_health_events(health, run_id=run),
            portfolio_id=1,
            analysis_run_id=run,
            unavailable=(),
        )
        item = queue.worth_checking[0]
        events = queue.events_for(item)
        return desk_module.stable_key(item, run), desk_module.fingerprint(events)

    first = keyed("run-a", SourceState.FAILED)
    assert first == keyed("run-b", SourceState.FAILED)
    assert "run-a" not in first[0]
    assert keyed("run-b", SourceState.SKIPPED)[1] != first[1]
