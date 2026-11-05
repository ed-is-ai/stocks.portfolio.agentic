"""Event-loop guards (#132): sync handlers, a concurrency check and gzip.

Every route handler and dependency is a plain ``def`` so blocking SQLite,
file and LLM work runs in the threadpool, never on uvicorn's single event
loop. The only coroutine allowed is the form-body read in ``request_form``.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from time import perf_counter

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute

import app.api.routes.strategy_manager as strategy_manager_routes
from app.api import dependencies
from app.api.app import app
from app.api.dependencies import (
    PORTFOLIO_WRITE,
    get_integration_config_service,
    serialized_portfolio_write,
)
from app.repositories import db
from app.services.integration_config_service import IntegrationConfigService
from tests.test_strategy_manager_routes import services  # noqa: F401 -- fixture
from tests.test_views_index_freshness import _use_artifact

ASYNC_ALLOWLIST = {"app.api.params:request_form"}
#: The app has ~88 routes; far fewer means FastAPI changed how it nests them.
MIN_API_ROUTES = 50

client = TestClient(app)


def _name(call: Callable[..., object]) -> str:
    return f"{call.__module__}:{call.__qualname__}"


def _calls(dependant: Dependant) -> Iterator[Callable[..., object]]:
    """Every endpoint and dependency callable under *dependant*."""
    if dependant.call is not None:
        yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _api_routes(routes: Sequence[BaseRoute]) -> Iterator[APIRoute]:
    """Every ``APIRoute``, descending into included routers.

    FastAPI 0.137 keeps an included router as one ``_IncludedRouter`` entry
    that points at the ``original_router`` instead of copying its routes.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _api_routes(included.routes)


def _route_calls() -> list[Callable[..., object]]:
    routes = list(_api_routes(app.routes))
    assert len(routes) >= MIN_API_ROUTES, "too few API routes -- walker is broken"
    return [call for route in routes for call in _calls(route.dependant)]


def test_only_allowlisted_callables_are_coroutines() -> None:
    calls = _route_calls()
    coroutines = {_name(c) for c in calls if inspect.iscoroutinefunction(c)}

    assert coroutines - ASYNC_ALLOWLIST == set()
    assert ASYNC_ALLOWLIST - {_name(c) for c in calls} == set()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_a_blocked_strategy_manager_does_not_stall_settings(
    services,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """One event loop serves both requests (no lifespan); settings answers
    while Strategy Manager is still parked inside its handler."""
    entered, release = threading.Event(), threading.Event()

    def blocking_perf_counter() -> float:
        entered.set()
        release.wait(timeout=10)
        return perf_counter()

    monkeypatch.setattr(strategy_manager_routes, "perf_counter", blocking_perf_counter)
    config = IntegrationConfigService(tmp_path / ".env")
    app.dependency_overrides[get_integration_config_service] = lambda: config
    order: list[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:

        async def strategy_manager() -> None:
            await http.get("/partials/strategies")
            order.append("strategy-manager")

        async with anyio.create_task_group() as tg:
            tg.start_soon(strategy_manager)
            assert await anyio.to_thread.run_sync(entered.wait, 10)
            settings = await http.get("/partials/settings")
            order.append("settings" if not release.is_set() else "late")
            release.set()

    assert settings.status_code == 200
    assert order == ["settings", "strategy-manager"]


def test_a_large_html_response_is_gzipped(monkeypatch, tmp_path) -> None:
    _use_artifact(monkeypatch, tmp_path, datetime.now(UTC))

    resp = client.get("/", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


def test_a_small_body_is_not_gzipped() -> None:
    resp = client.get("/startup-status", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert len(resp.content) < 1024
    assert "content-encoding" not in resp.headers


# --- #132 review: money-mutating routes are serialised ----------------------

LOCKED_WRITES = {
    "refresh_portfolio_prices",
    "import_sipp",
    "quick_add_holding",
    "create_opening_lot",
    "edit_opening_lot",
    "delete_opening_lot",
    "create_portfolio",
    "rename_portfolio",
    "delete_portfolio",
    "assign_strategy",
    "clear_strategy",
    "set_position_stop",
    "record_trade",
    "delete_trade",
    "ack_unmatched_sell",
}


def test_every_money_write_route_takes_the_write_lock() -> None:
    locked = {
        route.endpoint.__name__
        for route in _api_routes(app.routes)
        if serialized_portfolio_write in set(_calls(route.dependant))
    }

    assert LOCKED_WRITES <= locked


class _SpyLock:
    """A real lock that signals when a second caller starts to wait on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self.second_waiting = threading.Event()

    def __enter__(self) -> None:
        self._count += 1
        if self._count == 2:
            self.second_waiting.set()
        self._lock.acquire()

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


def test_two_concurrent_writes_run_one_at_a_time(monkeypatch) -> None:
    spy = _SpyLock()
    monkeypatch.setattr(dependencies, "_PORTFOLIO_WRITE_LOCK", spy)
    first_in, release = threading.Event(), threading.Event()
    order: list[str] = []
    mini = FastAPI()

    @mini.post("/first", dependencies=[PORTFOLIO_WRITE])
    def first() -> None:
        first_in.set()
        release.wait(timeout=10)
        order.append("first")

    @mini.post("/second", dependencies=[PORTFOLIO_WRITE])
    def second() -> None:
        order.append("second")

    http = TestClient(mini)
    a = threading.Thread(target=http.post, args=("/first",))
    a.start()
    assert first_in.wait(timeout=10)
    b = threading.Thread(target=http.post, args=("/second",))
    b.start()
    assert spy.second_waiting.wait(timeout=10)
    assert order == []
    release.set()
    a.join(timeout=10)
    b.join(timeout=10)

    assert order == ["first", "second"]


def test_trades_db_connections_wait_on_a_busy_writer(tmp_path) -> None:
    conn = db.connect(tmp_path / "trades.db")
    try:
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
    finally:
        conn.close()

    assert timeout >= db.EVIDENCE_SQLITE_BUSY_TIMEOUT_MS
