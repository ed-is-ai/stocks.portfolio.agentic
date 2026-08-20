"""Route tests for the notification centre (#80)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_notifications_repository
from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.services.backtest.strategy_job import StrategyJobNotFound

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


def test_count_badge_targets_itself_not_inherited_list(repo) -> None:
    """The badge must swap itself, never inherit the bell button's target.

    The badge span sits inside the bell button, which sets
    ``hx-target="#notif-list"``. Without its own explicit target the badge's
    ``outerHTML`` self-poll would inherit that and replace the whole
    notification list with the tiny count span (regression: dropdown collapsed
    to a 2px strip). An explicit ``hx-target="this"`` prevents the inheritance.
    """
    repo.record(NotificationCategory.ALERT, "breakout", "NVDA")

    response = client.get("/notifications/count")

    assert 'hx-target="this"' in response.text


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


# ---------------------------------------------------------------------------
# Story 2.8 AC5/AC6: native detail link + separate action/dismiss buttons,
# no nested interactive markup, backtest-aware deep links.
# ---------------------------------------------------------------------------


class FakeStrategyJob:
    def __init__(self, job_id: str, status_version: int = 4) -> None:
        self.id = job_id
        self.status_version = status_version


class FakeBacktestRepoForNotifications:
    def __init__(self, jobs: dict[str, FakeStrategyJob]) -> None:
        self.jobs = jobs

    def strategy_job(self, job_id: str) -> FakeStrategyJob:
        if job_id not in self.jobs:
            raise StrategyJobNotFound("missing")
        return self.jobs[job_id]


class FakeJobServiceForNotifications:
    def __init__(self, actions: tuple[str, ...] = ()) -> None:
        self.actions = actions

    def legal_actions(self, job_id: str):
        return SimpleNamespace(job_id=job_id, legal_actions=self.actions)


def _wire_strategy_backend(monkeypatch, *, jobs, actions=()):
    backtest_repo = FakeBacktestRepoForNotifications(jobs)
    job_service = FakeJobServiceForNotifications(actions)
    monkeypatch.setattr(
        "app.api.routes.notifications.get_backtest_repository", lambda: backtest_repo
    )
    monkeypatch.setattr(
        "app.api.routes.notifications.get_strategy_job_service", lambda: job_service
    )


def _upsert_job_notification(
    repo: NotificationsRepository,
    *,
    job_id: str,
    category: NotificationCategory,
    title: str = "Job event",
) -> None:
    repo.upsert_job_notification(
        job_id=job_id,
        job_status_version=4,
        category=category,
        event_type="strategy_job_running",
        severity=NotificationSeverity.INFO,
        title=title,
        body="Status: running.",
        created_at=datetime.now(timezone.utc),
        target_url=f"/strategy-manager/activities/{job_id}",
        actions=(),
    )


def test_notification_row_has_no_nested_interactive_wrapper(repo) -> None:
    """The whole <li> must no longer be a synthetic ``role="button"`` row
    -- that was the flagged nested-interactive-content bug (#80)."""
    repo.record(NotificationCategory.ALERT, "breakout", "NVDA breakout")

    response = client.get("/partials/notifications")

    assert 'class="notif-item' in response.text
    assert 'role="button"' not in response.text
    assert "notif-item notif-info" in response.text or "notif-item" in response.text


def test_backtest_notification_deep_link_label_and_url(repo, monkeypatch) -> None:
    _wire_strategy_backend(
        monkeypatch, jobs={"job-1": FakeStrategyJob("job-1")}, actions=("cancel",)
    )
    _upsert_job_notification(
        repo, job_id="job-1", category=NotificationCategory.BACKTEST
    )

    response = client.get("/partials/notifications")

    assert response.status_code == 200
    assert "Open backtest activity" in response.text
    assert "Open initialization activity" not in response.text
    assert 'href="/strategy-manager/activities/job-1"' in response.text


def test_initialization_notification_deep_link_label(repo, monkeypatch) -> None:
    _wire_strategy_backend(monkeypatch, jobs={"job-1": FakeStrategyJob("job-1")})
    _upsert_job_notification(
        repo, job_id="job-1", category=NotificationCategory.STRATEGY_INITIALIZATION
    )

    response = client.get("/partials/notifications")

    assert "Open initialization activity" in response.text
    assert "Open backtest activity" not in response.text


def test_notification_actions_render_only_when_legal(repo, monkeypatch) -> None:
    _wire_strategy_backend(
        monkeypatch, jobs={"job-1": FakeStrategyJob("job-1")}, actions=("cancel",)
    )
    _upsert_job_notification(
        repo, job_id="job-1", category=NotificationCategory.BACKTEST
    )

    response = client.get("/partials/notifications")

    assert ">Cancel<" in response.text
    assert ">Restart<" not in response.text
    assert ">Delete<" not in response.text


def test_notification_actions_omitted_when_no_legal_actions(repo, monkeypatch) -> None:
    _wire_strategy_backend(
        monkeypatch, jobs={"job-1": FakeStrategyJob("job-1")}, actions=()
    )
    _upsert_job_notification(
        repo, job_id="job-1", category=NotificationCategory.BACKTEST
    )

    response = client.get("/partials/notifications")

    assert ">Cancel<" not in response.text
    assert ">Restart<" not in response.text
    assert ">Delete<" not in response.text
    # Dismiss remains available regardless of live job legality.
    assert "notif-dismiss" in response.text


def test_notification_deleted_target_renders_unavailable_text(
    repo, monkeypatch
) -> None:
    _wire_strategy_backend(monkeypatch, jobs={})
    _upsert_job_notification(
        repo, job_id="job-missing", category=NotificationCategory.BACKTEST
    )

    response = client.get("/partials/notifications")

    assert "Activity no longer available." in response.text
    assert 'href="/strategy-manager/activities/job-missing"' not in response.text
    # No detail link exists in this branch, so a "Mark read" affordance
    # must be offered directly -- dismiss() never sets read_at, so
    # Dismiss alone is not equivalent to marking it read.
    assert f'hx-post="/notifications/{repo.recent()[0].id}/read"' in (response.text)


def test_notification_deleted_target_mark_read_actually_marks_it_read(
    repo, monkeypatch
) -> None:
    _wire_strategy_backend(monkeypatch, jobs={})
    _upsert_job_notification(
        repo, job_id="job-missing", category=NotificationCategory.BACKTEST
    )
    notification_id = repo.recent()[0].id

    response = client.post(f"/notifications/{notification_id}/read", headers=_AUTH)

    assert response.status_code == 200
    assert repo.recent()[0].is_read is True


def test_notification_deleted_target_already_read_hides_mark_read_button(
    repo, monkeypatch
) -> None:
    _wire_strategy_backend(monkeypatch, jobs={})
    _upsert_job_notification(
        repo, job_id="job-missing", category=NotificationCategory.BACKTEST
    )
    notification_id = repo.recent()[0].id
    repo.mark_read(notification_id)

    response = client.get("/partials/notifications")

    assert "Mark read" not in response.text
