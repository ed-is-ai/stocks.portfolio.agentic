"""Unit tests for the notification-centre repository (#80)."""

from datetime import datetime, timedelta, timezone

from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.schemas.notification import NotificationCategory, NotificationSeverity


def _repo(tmp_path, retention_days: int = 10) -> NotificationsRepository:
    connect = db.make_connect(lambda: str(tmp_path / "notifications.db"))
    repo = NotificationsRepository(connect, retention_days=retention_days)
    repo.ensure_schema()
    return repo


def test_record_and_recent_orders_newest_first(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.record(NotificationCategory.ALERT, "breakout", "First", ticker="AAA")
    repo.record(NotificationCategory.REFRESH, "refresh_complete", "Second")

    items = repo.recent()

    assert [n.title for n in items] == ["Second", "First"]
    assert items[1].ticker == "AAA"
    assert items[1].category is NotificationCategory.ALERT


def test_unread_count_and_mark_read(tmp_path) -> None:
    repo = _repo(tmp_path)
    first = repo.record(NotificationCategory.ALERT, "breakout", "One")
    repo.record(NotificationCategory.ALERT, "breakout", "Two")
    assert repo.unread_count() == 2

    repo.mark_read(first)
    assert repo.unread_count() == 1

    repo.mark_all_read()
    assert repo.unread_count() == 0


def test_dismiss_hides_from_default_feed(tmp_path) -> None:
    repo = _repo(tmp_path)
    kept = repo.record(NotificationCategory.ALERT, "breakout", "Keep")
    gone = repo.record(NotificationCategory.ALERT, "breakout", "Dismiss")

    repo.dismiss(gone)

    titles = [n.title for n in repo.recent()]
    assert titles == ["Keep"]
    # Dismissed rows never count as unread.
    assert repo.unread_count() == 1
    assert kept != gone
    # ...but are still retrievable when explicitly requested.
    assert len(repo.recent(include_dismissed=True)) == 2


def test_severity_round_trips(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.record(
        NotificationCategory.SOURCE,
        "source_failed",
        "Source failed",
        severity=NotificationSeverity.ERROR,
    )

    item = repo.recent()[0]

    assert item.severity is NotificationSeverity.ERROR
    assert item.deep_link_tab == "tab-runlog"
    assert item.icon == "bi-x-octagon"


def test_prune_older_than_boundary(tmp_path) -> None:
    repo = _repo(tmp_path, retention_days=10)
    connect = db.make_connect(lambda: str(tmp_path / "notifications.db"))

    old = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    with db.session(connect) as conn:
        for created_at, title in ((old, "old"), (fresh, "fresh")):
            conn.execute(
                "INSERT INTO notifications"
                " (category, event_type, severity, title, body, created_at)"
                " VALUES ('alert', 'breakout', 'info', ?, '', ?)",
                (title, created_at),
            )
        conn.commit()

    removed = repo.prune_older_than(10)

    assert removed == 1
    assert [n.title for n in repo.recent()] == ["fresh"]


def test_record_prunes_expired_rows(tmp_path) -> None:
    repo = _repo(tmp_path, retention_days=10)
    connect = db.make_connect(lambda: str(tmp_path / "notifications.db"))
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db.session(connect) as conn:
        conn.execute(
            "INSERT INTO notifications"
            " (category, event_type, severity, title, body, created_at)"
            " VALUES ('alert', 'breakout', 'info', 'ancient', '', ?)",
            (old,),
        )
        conn.commit()

    repo.record(NotificationCategory.ALERT, "breakout", "new")

    assert [n.title for n in repo.recent()] == ["new"]
