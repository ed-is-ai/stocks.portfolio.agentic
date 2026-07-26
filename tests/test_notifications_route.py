"""Route tests for the notification centre (#80)."""

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_notifications_repository
from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity

client = TestClient(app)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    connect = db.make_connect(lambda: str(tmp_path / "notifications.db"))
    repository = NotificationsRepository(connect)
    repository.ensure_schema()
    app.dependency_overrides[get_notifications_repository] = lambda: repository
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repository
    finally:
        app.dependency_overrides.clear()


_AUTH = {"X-Auth-Token": "s3cret"}


def test_count_badge_reflects_unread(repo) -> None:
    repo.record(NotificationCategory.ALERT, "breakout", "NVDA breakout", ticker="NVDA")

    response = client.get("/notifications/count")

    assert response.status_code == 200
    assert 'id="notif-badge"' in response.text
    assert "has-unread" in response.text
    assert ">1<" in response.text.replace(" ", "")


def test_count_badge_hidden_when_all_read(repo) -> None:
    repo.record(NotificationCategory.ALERT, "breakout", "NVDA")
    repo.mark_all_read()

    response = client.get("/notifications/count")

    assert "has-unread" not in response.text


def test_panel_lists_recent_notifications(repo) -> None:
    repo.record(
        NotificationCategory.SOURCE,
        "source_failed",
        "Source failed — StockTwits",
        severity=NotificationSeverity.ERROR,
    )

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "Source failed — StockTwits" in response.text
    # Out-of-band badge refresh travels with every panel render.
    assert 'hx-swap-oob="true"' in response.text


def test_panel_empty_state(repo) -> None:
    response = client.get("/partials/notifications")
    assert "You&#39;re all caught up." in response.text or "caught up" in response.text


def test_mark_read_updates_state(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "NVDA")

    response = client.post(f"/notifications/{notif_id}/read", headers=_AUTH)

    assert response.status_code == 200
    assert repo.unread_count() == 0


def test_mark_all_read(repo) -> None:
    repo.record(NotificationCategory.ALERT, "breakout", "A")
    repo.record(NotificationCategory.ALERT, "breakout", "B")

    response = client.post("/notifications/read-all", headers=_AUTH)

    assert response.status_code == 200
    assert repo.unread_count() == 0


def test_dismiss_removes_from_feed(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "Bye")

    response = client.post(f"/notifications/{notif_id}/dismiss", headers=_AUTH)

    assert response.status_code == 200
    assert repo.recent() == []


def test_mutating_endpoints_require_auth(repo) -> None:
    notif_id = repo.record(NotificationCategory.ALERT, "breakout", "Guard")

    # Cross-site fetch metadata is rejected even though a token exists.
    blocked = client.post(
        f"/notifications/{notif_id}/read",
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert blocked.status_code == 403
    assert repo.unread_count() == 1
