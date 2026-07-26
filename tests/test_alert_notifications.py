"""Alert-agent → notification-centre emit tests (#80/#81).

Watched-setup signals are folded into the digest and their notifications are
recorded only when the digest is actually dispatched, so a suppressed or
unconfigured send leaves the feed empty.
"""

from unittest.mock import MagicMock, patch

from app.agents.alert.alert_agent import AlertAgent
from app.repositories.notifications_repo import build_notifications_repository
from app.schemas import EmailConfig, StockRecord, StockScan

_EMAIL = EmailConfig(
    host="localhost",
    port=1025,
    user="user@example.com",
    password="pass",
    recipient="to@example.com",
)


def _stock(ticker: str, price: float) -> StockRecord:
    scan = StockScan(
        ticker=ticker,
        as_of="2024-01-01",
        price=price,
        volume=1_000_000,
        rel_volume=1.2,
        high_52w=150.0,
        low_52w=50.0,
        pct_from_52w_high=-5.0,
        pct_change_week=3.0,
    )
    return StockRecord.model_validate(scan.model_dump())


@patch("smtplib.SMTP")
def test_entry_trigger_records_notification_when_digest_sent(
    mock_smtp, tmp_path
) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = AlertAgent(db_path=str(tmp_path / "alerts.db"), email_config=_EMAIL)
    agent._alerts.ensure_schema()
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )

    # check_positions only queues; the notification is recorded when the digest
    # (send_summary_email) is actually dispatched.
    agent.check_positions([_stock("NVDA", 105.0)])
    assert build_notifications_repository().recent() == []

    agent.send_summary_email()

    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "entry_triggered"
    assert items[0].ticker == "NVDA"


def test_entry_trigger_records_nothing_when_email_unconfigured(tmp_path) -> None:
    # Empty SMTP credentials -> digest send returns False, nothing recorded.
    agent = AlertAgent(
        db_path=str(tmp_path / "alerts.db"),
        email_config=EmailConfig(
            host="localhost", port=1025, user="", password="", recipient=""
        ),
    )
    agent._alerts.ensure_schema()
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )

    agent.check_positions([_stock("NVDA", 105.0)])
    agent.send_summary_email()

    assert build_notifications_repository().recent() == []
