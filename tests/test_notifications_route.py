"""Route tests for the notification centre (#80): the bell badge and the
read/dismiss routes. The dropdown panel is gone (GH-21); the badge shows the
AI Desk's urgent count, faked here (the real count is in
``tests/test_desk_routes.py``)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_desk_service, get_notifications_repository
from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory

client = TestClient(app)


@pytest.fixture
def desk() -> SimpleNamespace:
    """The fake Desk's urgent count the badge shows."""
    return SimpleNamespace(count=0)


@pytest.fixture
def repo(tmp_path, monkeypatch, desk):
    connect = db.make_connect(lambda: str(tmp_path / "notifications.db"))
    repository = NotificationsRepository(connect)
    repository.ensure_schema()
    overrides = app.dependency_overrides
    monkeypatch.setitem(overrides, get_notifications_repository, lambda: repository)
    monkeypatch.setitem(
        overrides,
        get_desk_service,
        lambda: SimpleNamespace(attention_count=lambda _pid: desk.count),
    )
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    return repository


_AUTH = {"X-Auth-Token": "s3cret"}


def test_count_badge_shows_the_desk_attention_count(repo, desk) -> None:
    desk.count = 3

    response = client.get("/notifications/count", params={"portfolio_id": "1"})

    assert response.status_code == 200
    assert 'id="notif-badge"' in response.text
    assert "has-count" in response.text
    assert ">3<" in response.text.replace(" ", "")


def test_count_badge_targets_itself(repo) -> None:
    """The badge swaps itself (``outerHTML``), never an inherited target."""
    response = client.get("/notifications/count")

    assert 'hx-target="this"' in response.text


def test_count_badge_hidden_when_nothing_is_urgent(repo) -> None:
    repo.record(NotificationCategory.ALERT, "breakout", "NVDA")

    response = client.get("/notifications/count")

    assert "has-count" not in response.text


def test_mark_read_updates_state(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "NVDA")

    response = client.post(f"/notifications/{notif_id}/read", headers=_AUTH)

    assert response.status_code == 204
    assert repo.unread_count() == 0


def test_mark_all_read(repo) -> None:
    repo.record(NotificationCategory.ALERT, "breakout", "A")
    repo.record(NotificationCategory.ALERT, "breakout", "B")

    response = client.post("/notifications/read-all", headers=_AUTH)

    assert response.status_code == 204
    assert repo.unread_count() == 0


def test_dismiss_removes_from_feed(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "Bye")

    response = client.post(f"/notifications/{notif_id}/dismiss", headers=_AUTH)

    assert response.status_code == 204
    assert repo.recent() == []
    # The AI Desk re-renders on this event, so the notice leaves it.
    assert response.headers["HX-Trigger"] == "portfolio-agents-refresh"


def test_the_dropdown_panel_is_gone(repo) -> None:
    assert client.get("/partials/notifications").status_code == 404


def test_mutating_endpoints_require_auth(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "Guard")

    # Cross-site fetch metadata is rejected even though a token exists.
    blocked = client.post(
        f"/notifications/{notif_id}/read",
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert blocked.status_code == 403
    assert repo.unread_count() == 1
