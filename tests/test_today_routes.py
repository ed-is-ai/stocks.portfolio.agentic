"""Contract tests for the four-tab shell and Today (GH-128).

AD-3 (full shell per page URL), AD-4 (redirects, no link to a deleted
partial), AD-5 (one projection behind Today, the risk card and the badge),
AD-6 (buy signals) and AD-12 (one portfolio resolver). The Desk service is
built over ``tests.test_desk_service``'s ``tmp_path`` stack, so no real
ledger, notification store or evidence store is ever opened.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_desk_service, get_trader_service
from app.api.routes.views import SECONDARY_PAGES, SHELL_PAGES, SHELL_TABS
from app.services import portfolio_service as portfolio_service_module
from app.services.portfolio_service import resolve_portfolio_id
from tests.test_desk_service import desk_stack  # noqa: F401 -- fixture
from tests.test_views_index_freshness import _use_artifact

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
#: Partials retired by GH-128; nothing may link to them.
DELETED_PARTIALS = (
    "/partials/desk",
    "/partials/desk/queue",
    "/partials/desk/items/x",
    "/partials/stock-scanner",
    "/partials/strategy-manager",
)


@pytest.fixture
def stack(desk_stack, monkeypatch, tmp_path) -> Iterator[SimpleNamespace]:  # noqa: F811
    """The Desk stack behind the real routes, with the shell's freshness
    read pointed at a temp artifact."""
    _use_artifact(monkeypatch, tmp_path, datetime.now(UTC))
    monkeypatch.setitem(
        app.dependency_overrides, get_desk_service, lambda: desk_stack.desk()
    )
    monkeypatch.setitem(
        app.dependency_overrides, get_trader_service, lambda: desk_stack.trader
    )
    yield desk_stack


def _tab(markup: str, slug: str) -> str:
    start = markup.index(f'id="tab-{slug}"')
    return markup[markup.rindex("<a", 0, start) : markup.index(">", start)]


def _api_paths() -> list[APIRoute]:
    routes: list[APIRoute] = []

    def walk(items: list) -> None:
        for route in items:
            if isinstance(route, APIRoute):
                routes.append(route)
            nested = getattr(getattr(route, "original_router", None), "routes", None)
            if nested:
                walk(nested)

    walk(list(app.routes))
    return routes


# --- AD-3: every page URL is the full shell ----------------------------------


@pytest.mark.parametrize("slug", list(SHELL_PAGES))
def test_each_page_url_returns_the_full_shell(stack, slug: str) -> None:
    plain = client.get(f"/{slug}")
    htmx = client.get(f"/{slug}", headers={"HX-Request": "true"})

    assert plain.status_code == 200
    # A pushed URL never branches on HX-Request.
    assert plain.text == htmx.text
    markup = plain.text
    assert 'id="boot-splash"' in markup and 'id="tab-content"' in markup
    loader = markup[markup.index('id="shell-loader"') :].split(">", 1)[0]
    assert f'hx-get="/partials/{slug}"' in loader
    assert 'hx-trigger="load"' in loader
    for tab in SHELL_TABS:
        link = _tab(markup, tab)
        assert ("active" in link) == (tab == slug)
        assert f'hx-get="/partials/{tab}"' in link
        assert f'hx-push-url="/{tab}"' in link
        assert f'href="/{tab}"' in link


def test_the_shell_has_four_tabs_a_gear_menu_and_one_refresh(stack) -> None:
    markup = client.get("/today").text

    tabs = re.findall(r'data-tab="([^"]+)"', markup)
    assert tabs == ["today", "scanner", "portfolio", "strategies"]
    gear = markup[markup.index('id="shell-gear-menu"') :].split("</ul>", 1)[0]
    assert re.findall(r'data-page="([^"]+)"', gear) == list(SECONDARY_PAGES)
    assert markup.count('hx-post="/refresh-data"') == 1
    assert "nav-bell-btn" not in markup
    # The Today tab carries the badge, which polls at most every 30s.
    badge = markup[markup.index('id="notif-badge"') :].split(">", 1)[0]
    assert "every 30s [document.visibilityState === 'visible']" in badge
    assert 'hx-push-url="false"' in badge
    assert markup.index('id="tab-today"') < markup.index('id="notif-badge"')


def test_history_never_restores_a_localstorage_snapshot() -> None:
    shell = (ROOT / "app/api/static/js/shell.js").read_text(encoding="utf-8")

    assert "htmx.config.historyCacheSize = 0;" in shell
    assert "htmx.config.refreshOnHistoryMiss = true;" in shell


def test_template_response_deep_links_are_untouched(stack) -> None:
    markup = client.get("/strategy-manager").text

    assert 'id="shell-tabs"' not in markup
    assert 'id="tab-content"' in markup


# --- AD-4: redirects and retired partials ------------------------------------


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("/", "/today"),
        ("/?portfolio_id=2", "/today?portfolio_id=2"),
        ("/desk", "/today"),
        ("/desk?portfolio_id=2&item=x", "/today?portfolio_id=2&item=x"),
    ],
)
def test_root_and_desk_redirect_to_today_keeping_the_query(
    url: str, target: str
) -> None:
    resp = client.get(url, follow_redirects=False)

    assert resp.status_code == 307
    assert resp.headers["location"] == target


@pytest.mark.parametrize("url", DELETED_PARTIALS)
def test_deleted_partials_are_gone(url: str) -> None:
    assert client.get(url).status_code == 404


def _partial_literals() -> set[str]:
    """Every ``/partials/...`` literal in the app's code, templates and JS."""
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*"):
        if path.suffix not in {".py", ".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r"/partials/[A-Za-z0-9_\-/]*[A-Za-z0-9_\-]", text))
    return found


def test_no_link_points_at_an_unregistered_partial() -> None:
    routes = _api_paths()
    literals = _partial_literals()

    def served(url: str) -> bool:
        return any(
            route.path_regex.match(url) or route.path.startswith(url + "/")
            for route in routes
        )

    assert literals, "the scan found no partial links at all"
    # A literal ending in a path segment that a template completes (an id)
    # is served when some route extends it.
    missing = sorted(u for u in literals if not served(u))
    assert missing == []
    assert not any(u in literals for u in DELETED_PARTIALS)


# --- Today, the badge and the one projection (AD-5, AD-6) --------------------


def test_today_renders_the_projection(stack) -> None:
    body = client.get("/partials/today", params={"portfolio_id": stack.pid}).text

    assert "Needs action" in body and "Worth checking" in body
    assert "Buy signals" in body and 'href="/scanner"' in body
    assert body.count('class="today-act-row"') == 4
    assert body.count('class="today-check-row"') == 1
    assert '<details class="disclosure">' in body
    assert "Dismiss all 1" in body
    # The badge equals the number of Needs action items.
    badge = client.get("/notifications/count", params={"portfolio_id": stack.pid})
    assert ">4<" in badge.text.replace(" ", "")


def test_scanner_setups_are_not_listed_on_today(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        portfolio_service_module,
        "record_setups",
        lambda _records, _held: [("NEWCO", "breakout", "Breakout")],
    )

    body = client.get("/partials/today", params={"portfolio_id": stack.pid}).text

    assert "NEWCO" not in body
    assert "1 other new setup is in the Scanner, not here." in body


def _rev(body: str) -> str:
    match = re.search(r'"rev": "([^"]*)"', body)
    assert match is not None
    return match.group(1)


def test_an_unchanged_today_poll_answers_204(stack) -> None:
    first = client.get("/partials/today", params={"portfolio_id": stack.pid})
    rev = _rev(first.text)

    again = client.get(
        "/partials/today", params={"portfolio_id": stack.pid, "rev": rev}
    )
    stale = client.get(
        "/partials/today", params={"portfolio_id": stack.pid, "rev": "old"}
    )

    assert rev and again.status_code == 204
    assert stale.status_code == 200


def test_dismiss_all_clears_worth_checking_and_refreshes_the_badge(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    denied = client.post("/today/dismiss", params={"portfolio_id": stack.pid})
    resp = client.post(
        "/today/dismiss",
        params={"portfolio_id": stack.pid},
        headers={"X-Auth-Token": "s3cret"},
    )

    assert denied.status_code == 403

    assert resp.status_code == 200
    assert "attention-changed" in resp.headers["HX-Trigger"]
    assert 'class="today-check-row"' not in resp.text
    assert "Nothing to check." in resp.text
    assert resp.text.count('class="today-act-row"') == 4


def test_a_failing_badge_shows_count_unavailable(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        app.dependency_overrides,
        get_desk_service,
        lambda: SimpleNamespace(badge_count=lambda _pid: 1 / 0),
    )

    resp = client.get("/notifications/count")

    assert resp.status_code == 200
    assert "is-unknown" in resp.text
    assert '<span aria-hidden="true">?</span>' in resp.text


# --- AD-12: one portfolio resolver -------------------------------------------


def test_one_active_portfolio_and_no_desk_param() -> None:
    sources = [
        p.read_text(encoding="utf-8")
        for p in (ROOT / "app/api").rglob("*")
        if p.suffix in {".html", ".js"}
    ]

    assert sum(s.count("function activePortfolio(") for s in sources) == 1
    assert not any("deskParam" in s for s in sources)
    shell = (ROOT / "app/api/static/js/shell.js").read_text(encoding="utf-8")
    body = shell[shell.index("function activePortfolio(") :].split("\n}\n", 1)[0]
    # The page URL's ?portfolio_id= wins over localStorage.
    assert body.index("location.search") < body.index("localStorage")


def test_every_tab_request_carries_the_resolved_portfolio(stack) -> None:
    markup = client.get("/today?portfolio_id=7").text

    loader = markup[markup.index('id="shell-loader"') :].split(">", 1)[0]
    assert "shellParams()" in loader
    for tab in SHELL_TABS:
        assert "shellParams()" in _tab(markup, tab)


def test_the_resolver_prefers_a_known_id_then_the_first() -> None:
    portfolios = [SimpleNamespace(id=3), SimpleNamespace(id=5)]

    assert resolve_portfolio_id(5, portfolios) == 5
    assert resolve_portfolio_id(9, portfolios) == 3
    assert resolve_portfolio_id(None, portfolios) == 3
    assert resolve_portfolio_id(5, []) is None


def test_the_account_picker_selects_the_resolved_portfolio(stack) -> None:
    other = stack.agent.create_portfolio("ISA")

    chosen = client.get("/partials/account-picker", params={"portfolio_id": other.id})
    fallback = client.get("/partials/account-picker", params={"portfolio_id": ""})

    assert f'<option value="{other.id}" selected>' in chosen.text
    assert f'<option value="{stack.pid}" selected>' in fallback.text
