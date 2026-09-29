"""Route tests for the AI Desk screen and the bell that links to it (GH-21).

The Desk service is built over ``tests.test_desk_service``'s ``tmp_path``
stack (``TraderAgent(db_path=...)``), so no real ledger, notification store
or evidence store is ever opened.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.app import app
from app.api.dependencies import (
    get_desk_service,
    get_notifications_repository,
    get_portfolio_recommendation_service,
    get_portfolio_service,
    get_position_thesis_service,
    get_trade_review_service,
    get_trader_service,
)
from app.core import config
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.services.desk_service import ORDERING_NOTE
from tests.test_desk_service import desk_stack  # noqa: F401 -- fixture
from tests.test_portfolio_risk_route import _dump
from tests.test_views_index_freshness import _use_artifact

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stack(desk_stack, monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:  # noqa: F811
    """The Desk stack behind the real routes, with the shell's freshness
    read pointed at a temp artifact."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(UTC))
    monkeypatch.setitem(
        app.dependency_overrides, get_desk_service, lambda: desk_stack.desk()
    )
    yield desk_stack


def _desk(stack: SimpleNamespace, **params: object) -> str:
    resp = client.get("/partials/desk", params={"portfolio_id": stack.pid, **params})
    assert resp.status_code == 200
    return resp.text


def _options(body: str) -> list[tuple[str, str]]:
    """Each queue option's (item id, aria-selected)."""
    return re.findall(
        r'role="option"[^>]*data-item-id="([^"]+)"[^>]*aria-selected="(\w+)"',
        body,
        re.S,
    )


def _counts(body: str) -> dict[str, str]:
    """The queue header's count per chip."""
    return dict(
        (chip, n)
        for n, chip in re.findall(
            r'class="desk-count is-\w+"><strong>(\d+)</strong> (\w+)<', body
        )
    )


def _tab(markup: str, tab_id: str) -> str:
    start = markup.index(f'id="{tab_id}"')
    return markup[markup.rindex("<a", 0, start) : markup.index(">", start)]


def test_desk_is_its_own_screen_in_the_shell(stack) -> None:
    markup = client.get("/desk").text

    desk_tab = _tab(markup, "tab-desk")
    assert "nav-link active" in desk_tab
    assert 'hx-trigger="click, load"' in desk_tab
    assert 'hx-get="/partials/desk"' in desk_tab
    assert 'hx-target="#tab-content"' in desk_tab
    scanner = _tab(markup, "tab-stock-scanner")
    assert '<a class="nav-link" id="tab-stock-scanner"' in scanner
    assert 'hx-trigger="click"' in scanner
    assert 'id="tab-content"' in markup
    assert 'id="boot-splash"' in markup


def test_root_is_unchanged(stack) -> None:
    markup = client.get("/").text

    scanner = _tab(markup, "tab-stock-scanner")
    assert "nav-link active" in scanner
    assert 'hx-trigger="click, load"' in scanner
    desk_tab = _tab(markup, "tab-desk")
    assert '<a class="nav-link" id="tab-desk"' in desk_tab
    assert 'hx-trigger="click"' in desk_tab


def test_tabs_keep_the_url_on_the_desk_without_history_entries() -> None:
    markup = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")

    assert "history.replaceState(null, '', path)" in markup
    assert "link.id === 'tab-desk' ? '/desk' : '/'" in markup
    assert "hx-push-url" not in markup
    # The first load honours a deep link's portfolio and item.
    assert 'deskParam("portfolio_id")' in _tab(markup, "tab-desk")
    assert 'deskParam("item")' in _tab(markup, "tab-desk")


def test_a_deep_link_selects_the_item(stack) -> None:
    items = _options(_desk(stack))
    target = items[-1][0]

    body = _desk(stack, item=target)

    assert [sel for _id, sel in _options(body)] == ["false"] * (len(items) - 1) + [
        "true"
    ]
    assert "no longer in the queue" not in body


def test_an_unknown_item_selects_the_first_with_a_note(stack) -> None:
    body = _desk(stack, item="attention:gone")

    assert _options(body)[0][1] == "true"
    assert "That item is no longer in the queue; showing the first item." in body


def test_selection_swaps_from_templates_with_a_live_region(stack) -> None:
    body = _desk(stack)
    count = len(_options(body))

    assert 'role="listbox"' in body
    assert len(re.findall(r'<template id="desk-detail-\d+">', body)) == count
    assert body.count("Evidence and provenance") == count + 1
    assert 'id="desk-announcer" class="visually-hidden" aria-live="polite"' in body
    index = (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")
    assert "'Selected: ' + option.dataset.title" in index
    assert "ArrowDown" in index and "ArrowUp" in index
    assert "template.content.cloneNode(true)" in index


def test_the_queue_states_its_ordering_rule_and_counts(stack) -> None:
    first, second = _desk(stack), _desk(stack)

    assert _options(first) == _options(second)
    assert ORDERING_NOTE in first
    assert _counts(first) == {
        "Risk": "4",
        "Limited": "0",
        "Review": "1",
        "Ready": "0",
    }
    assert "Bell: <strong>5</strong> urgent" in first
    assert re.findall(r'class="desk-chip is-(\w+)">(\w+)<', first) == [
        ("risk", "Risk")
    ] * 4 + [("info", "Review")]


def test_the_risk_strip_and_context_bar_render(stack) -> None:
    body = _desk(stack)

    assert "£150.00 · 7.5%" in body
    assert "1 / 2 complete" in body
    assert "Published analysis <code>run-0</code>" in body
    assert "No broker connection · Job approvals arrive with" in body
    assert '<option value="1" selected>SIPP</option>' in body


def test_a_run_wide_notification_warning_is_one_system_review_item(stack) -> None:
    stack.notifications.record(
        NotificationCategory.ALERT,
        "thesis_evaluation_failed",
        "Position thesis evaluation failed",
        severity=NotificationSeverity.WARNING,
    )

    body = _desk(stack)

    assert "Position thesis evaluation failed" in body
    assert "<strong>System</strong>" in body
    assert "Bell: <strong>5</strong> urgent" in body
    assert _counts(body)["Review"] == "2"


def test_unbuilt_workflows_and_a_cold_trade_review_are_declared(stack) -> None:
    body = _desk(stack)

    assert "Not built yet — #15" in body
    assert "Not built yet — planned" in body
    assert 'data-desk-tab="tab-history">Open History</a>' in body
    assert "Agent boundary" in body


def test_a_failing_source_leaves_the_desk_rendering(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(stack.service, "risk_report", boom)
    stack.statuses = boom
    monkeypatch.setattr(stack.notifications, "recent_warnings", boom)

    body = _desk(stack)

    assert body.count(">Unavailable</span>") == 2
    assert "Partial — not counted:" in body
    assert "Notifications" in body


def test_an_empty_queue_says_nothing_needs_attention(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack.trader, "list_portfolios", lambda: [])

    body = _desk(stack)

    assert "Nothing needs attention." in body
    assert ORDERING_NOTE in body
    assert 'role="listbox"' not in body


def test_the_inspector_route_serves_one_item(stack) -> None:
    item = _options(_desk(stack))[0][0]

    resp = client.get(
        f"/partials/desk/items/{item}", params={"portfolio_id": stack.pid}
    )

    assert resp.status_code == 200
    assert "Evidence and provenance" in resp.text
    assert 'class="desk-facts"' in resp.text
    missing = client.get("/partials/desk/items/nope", params={"portfolio_id": 1})
    assert missing.status_code == 404


def test_the_queue_route_serves_the_queue_pane(stack) -> None:
    resp = client.get("/partials/desk/queue", params={"portfolio_id": stack.pid})

    assert resp.status_code == 200
    assert resp.text.lstrip().startswith('<section class="desk-queue"')


def test_desk_routes_write_nothing(stack) -> None:
    stack.notifications.record(
        NotificationCategory.PORTFOLIO,
        "import_failed",
        "Import failed",
        severity=NotificationSeverity.ERROR,
        portfolio_id=stack.pid,
    )
    ledger = _dump(stack.agent.db_path)
    notes = [n.model_dump() for n in stack.notifications.recent(include_dismissed=True)]
    item = _options(_desk(stack))[0][0]

    for url in (
        "/desk",
        "/partials/desk",
        "/partials/desk/queue",
        f"/partials/desk/items/{item}",
    ):
        assert client.get(url, params={"portfolio_id": stack.pid}).status_code == 200
    assert _dump(stack.agent.db_path) == ledger
    assert [
        n.model_dump() for n in stack.notifications.recent(include_dismissed=True)
    ] == notes
    assert not stack.audit.exists()


def test_desk_markup_has_no_mutating_control_or_approval_dialog(stack) -> None:
    stack.notifications.record(
        NotificationCategory.BACKTEST,
        "strategy_job_failed",
        "Backtest failed",
        severity=NotificationSeverity.ERROR,
    )
    body = _desk(stack)

    posts = re.findall(r'hx-post="([^"]+)"', body)
    assert posts and all(re.fullmatch(r"/notifications/\d+/dismiss", p) for p in posts)
    assert "<form" not in body
    assert "<dialog" not in body and "Approve" not in body


def test_the_bell_is_a_link_to_the_desk_with_no_dropdown(stack) -> None:
    markup = client.get("/").text

    start = markup.index('id="nav-bell-btn"') - 3
    bell = markup[start : markup.index("</a>", start)]
    assert bell.startswith("<a ")
    assert 'href="/desk"' in bell
    assert 'id="notif-badge"' in bell
    assert "notif-list" not in markup
    assert "notif-dropdown" not in markup
    assert "notifDeepLink" not in markup and "markNotificationRead" not in markup
    assert client.get("/partials/notifications").status_code == 404
    assert not (ROOT / "app/api/templates/_notifications.html").exists()


def test_the_badge_counts_the_desks_urgent_items(stack) -> None:
    resp = client.get("/notifications/count", params={"portfolio_id": stack.pid})

    assert resp.status_code == 200
    assert "has-count" in resp.text
    assert ">5<" in resp.text.replace(" ", "")
    assert 'hx-target="this"' in resp.text
    assert "activePortfolio()" in resp.text


def test_a_failing_badge_shows_count_unavailable(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        get_desk_service,
        lambda: SimpleNamespace(attention_count=lambda _pid: 1 / 0),
    )

    resp = client.get("/notifications/count")

    assert resp.status_code == 200
    assert "has-count" not in resp.text
    assert "is-unknown" in resp.text
    assert '<span aria-hidden="true">?</span>' in resp.text
    assert '<span class="visually-hidden">attention count unavailable</span>' in (
        resp.text
    )


def test_the_real_dependency_composes_the_desk(
    desk_stack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_desk_service`` wires the shared services, the audit log path and
    the source-health loader; every one is overridden to the temp stack."""
    monkeypatch.setattr(dependencies, "load_source_health", dict)
    monkeypatch.setattr(config, "COPILOT_AUDIT_JSONL", desk_stack.audit)
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        get_trader_service: lambda: desk_stack.trader,
        get_portfolio_service: lambda: desk_stack.service,
        get_portfolio_recommendation_service: lambda: SimpleNamespace(
            recommend=desk_stack.recommend
        ),
        get_position_thesis_service: lambda: desk_stack.theses,
        get_notifications_repository: lambda: desk_stack.notifications,
        get_trade_review_service: lambda: desk_stack.reviews,
    }
    for dependency, override in overrides.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, override)

    body = _desk(desk_stack)

    assert "Bell: <strong>5</strong> urgent" in body
    assert "1 Sell" in body


# --- GH-21 review fixes -----------------------------------------------------


def _index() -> str:
    return (ROOT / "app/api/templates/index.html").read_text(encoding="utf-8")


def test_the_badge_polls_only_while_the_page_is_visible(stack) -> None:
    visible = "every 20s [document.visibilityState === 'visible'], refresh"
    badge = client.get("/notifications/count", params={"portfolio_id": stack.pid})

    assert f'hx-trigger="{visible}"' in badge.text
    assert f'hx-trigger="load, {visible}"' in client.get("/").text


def test_the_bell_names_its_count_and_announces_changes(stack) -> None:
    markup = client.get("/").text
    start = markup.index('id="nav-bell-btn"') - 3
    bell = markup[start : markup.index("</a>", start)]
    badge = client.get("/notifications/count", params={"portfolio_id": stack.pid})

    # No aria-label overrides the content, so the name is "AI Desk, <count>".
    assert "aria-label" not in bell
    assert '<span class="visually-hidden">AI Desk,</span>' in bell
    assert '<span class="visually-hidden">5 urgent attention items</span>' in (
        badge.text
    )
    assert 'data-announce="5 urgent attention items"' in badge.text
    assert '<span id="notif-live" class="visually-hidden" aria-live="polite">' in markup
    assert "getElementById('notif-live').textContent = text" in _index()


def test_the_whole_desk_refreshes_keeping_the_selection(stack) -> None:
    body = _desk(stack)
    start = body.index('id="desk-refresh"')
    refresher = body[start : body.index(">", start)]

    assert 'hx-get="/partials/desk"' in refresher
    assert 'hx-target="#tab-content"' in refresher
    assert (
        "portfolio-agents-refresh from:body, "
        "every 60s [document.visibilityState === 'visible']"
    ) in refresher
    assert "deskSelectedId()" in refresher
    assert 'hx-sync="#tab-content:drop"' in refresher
    # The queue no longer refreshes on its own.
    assert (
        "hx-get"
        not in body[body.index('<section class="desk-queue"') :].split(">", 1)[0]
    )


def test_a_refreshed_empty_queue_clears_the_inspector(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _options(_desk(stack))[0][0]
    monkeypatch.setattr(stack.trader, "list_portfolios", lambda: [])

    body = _desk(stack, item=item)
    inspector = body[body.index('id="desk-inspector-body"') :]

    assert "Nothing selected — the queue is empty." in inspector
    assert "desk-finding" not in inspector


def test_queue_counts_partition_the_list_and_label_the_bell(stack) -> None:
    body = _desk(stack)

    assert sum(map(int, _counts(body).values())) == len(_options(body))
    assert "Bell: <strong>5</strong> urgent" in body


def test_the_queue_shows_raised_by_as_human_labels(stack) -> None:
    body = _desk(stack)

    # Evidence refs keep their provenance ids; who raised it reads as names.
    assert not re.search(r"<(strong|small)>(risk_coach|strategy|pipeline)", body)
    assert "<dt>Raised by</dt><dd>Portfolio risk" in body
    assert "<strong>Portfolio risk" in body


def test_the_boundary_text_is_accurate(stack) -> None:
    body = _desk(stack)

    assert "only change is dismissing a notice" in body
    assert "nothing here changes trades" not in body


def test_recent_activity_renders_with_a_dismiss_per_notice(stack) -> None:
    note = stack.notifications.record(
        NotificationCategory.BACKTEST,
        "strategy_job_completed",
        "Backtest complete",
        severity=NotificationSeverity.INFO,
    )

    body = _desk(stack)

    section = body[body.index('id="desk-activity-heading"') :]
    assert "Backtest complete" in section
    assert f'hx-post="/notifications/{note}/dismiss"' in section


def test_a_detached_thesis_opener_returns_focus_to_the_selected_option() -> None:
    assert (
        'else document.querySelector(\'#desk [role="option"]'
        '[aria-selected="true"]\')?.focus();'
    ) in _index()


def test_a_desk_render_refreshes_the_bell_for_its_portfolio() -> None:
    assert "htmx.trigger('#notif-badge', 'refresh')" in _index()


def test_url_rewrites_are_debounced_and_guarded() -> None:
    index = _index()

    assert "setTimeout(() => {" in index and "}, 250);" in index
    assert "try { history.replaceState(null, '', path); } catch" in index
    # Every URL rewrite goes through the debounced helper.
    assert index.count("history.replaceState(") == 1


def test_a_late_desk_response_cannot_rewrite_another_tabs_url(stack) -> None:
    markup = client.get("/").text
    index = _index()
    body = _desk(stack)

    assert '<ul class="nav nav-tabs" id="mainTabs" hx-sync="#tab-content:replace">' in (
        markup
    )
    assert "!deskTabActive()) return;" in index
    select = body[body.index('<select id="desk-portfolio"') :].split(">", 1)[0]
    assert 'hx-sync="#tab-content:replace"' in select
