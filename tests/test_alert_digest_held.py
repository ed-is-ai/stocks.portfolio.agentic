"""Tests for digest-batched watched signals and immediate held-position
emails (#81).

Watched-setup entry/stop signals are folded into the per-run digest (no
individual emails); held-portfolio stop-loss / profit-target events fire an
immediate individual email and are persisted regardless of send success.
"""

from unittest.mock import MagicMock, patch

from app.agents.alert.alert_agent import AlertAgent
from app.repositories.notifications_repo import build_notifications_repository
from app.schemas import EmailConfig, Position, StockRecord, StockScan

_EMAIL = EmailConfig(
    host="localhost",
    port=1025,
    user="user@example.com",
    password="pass",
    recipient="to@example.com",
)

_UNCONFIGURED = EmailConfig(
    host="localhost", port=1025, user="", password="", recipient=""
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


def _position(
    ticker: str,
    current_price: float,
    *,
    stop_loss: float | None = None,
    entry_price: float | None = None,
    profit_target_20: float | None = None,
) -> Position:
    return Position(
        ticker=ticker,
        shares=100.0,
        avg_cost=100.0,
        total_cost=10_000.0,
        current_price=current_price,
        current_value=current_price * 100.0,
        unrealised_pnl=(current_price - 100.0) * 100.0,
        unrealised_pnl_pct=(current_price - 100.0),
        entry_price=entry_price,
        stop_loss=stop_loss,
        profit_target_20=profit_target_20,
    )


def _agent(tmp_path, email: EmailConfig = _EMAIL) -> AlertAgent:
    agent = AlertAgent(db_path=str(tmp_path / "alerts.db"), email_config=email)
    agent._alerts.ensure_schema()
    return agent


# ── Watched signals → digest, never individual emails ──────────────────────


def test_check_positions_never_emails(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent._alerts.record(
        "AMD", 8, "Stage 2", "setup", entry_price=200.0, stop_loss=180.0
    )

    with patch.object(AlertAgent, "send_email") as spy:
        agent.check_positions([_stock("NVDA", 105.0), _stock("AMD", 175.0)])

    spy.assert_not_called()
    assert agent._entry_triggered == [("NVDA", 105.0, 100.0)]
    assert agent._watched_stops == [("AMD", 175.0, 180.0)]


@patch("smtplib.SMTP")
def test_watched_signals_appear_in_digest(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent._alerts.record(
        "AMD", 8, "Stage 2", "setup", entry_price=200.0, stop_loss=180.0
    )
    agent.check_positions([_stock("NVDA", 105.0), _stock("AMD", 175.0)])

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    # AMD is held, so its watched stop is a real, actionable SELL signal (#113).
    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[_position("AMD", 175.0)])

    assert "WATCHLIST ENTRIES TRIGGERED" in captured["html"]
    assert "WATCHLIST STOP LOSSES" in captured["html"]
    assert "NVDA" in captured["text"]
    assert "AMD" in captured["text"]

    events = {i.event_type for i in build_notifications_repository().recent()}
    assert events == {"entry_triggered", "stop_loss_hit"}


@patch("smtplib.SMTP")
def test_non_held_watched_stop_is_suppressed(mock_smtp, tmp_path) -> None:
    # #113: a watched setup that breaks its stop while NOT held is noise — no
    # SELL line, no notification — but the entry (buy) side is unaffected and
    # the row is still marked "stopped" in the DB (watchlist bookkeeping).
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent._alerts.record(
        "AMD", 8, "Stage 2", "setup", entry_price=200.0, stop_loss=180.0
    )
    agent.check_positions([_stock("NVDA", 105.0), _stock("AMD", 175.0)])
    assert agent._watched_stops == [("AMD", 175.0, 180.0)]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        return True

    # Nothing held → AMD's stop is suppressed; NVDA's entry still fires.
    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[])

    assert "WATCHLIST ENTRIES TRIGGERED" in captured["html"]
    assert "WATCHLIST STOP LOSSES" not in captured["html"]

    events = {i.event_type for i in build_notifications_repository().recent()}
    assert "entry_triggered" in events
    assert "stop_loss_hit" not in events

    # Bookkeeping retained: AMD left the watchlist (row marked "stopped").
    watching = {ticker for _rowid, ticker, _entry, _stop in agent._alerts.watching()}
    assert "AMD" not in watching


# ── Held-position critical events → immediate individual email ─────────────


@patch("smtplib.SMTP")
def test_held_stop_loss_fires_one_email(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)

    with patch.object(AlertAgent, "send_email", return_value=True) as spy:
        count = agent.check_portfolio_stops([pos], {})

    assert count == 1
    assert spy.call_count == 1
    assert "Stop Loss Hit: TSLA" in spy.call_args.args[0]
    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "stop_loss_hit"
    assert items[0].ticker == "TSLA"


def test_held_stop_loss_persists_when_email_unconfigured(tmp_path) -> None:
    agent = _agent(tmp_path, email=_UNCONFIGURED)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)

    count = agent.check_portfolio_stops([pos], {})

    assert count == 1
    # Held critical events must be recorded even when SMTP is unconfigured.
    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "stop_loss_hit"


@patch("smtplib.SMTP")
def test_held_profit_target_fires_email(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("AAPL", 121.0, entry_price=100.0, profit_target_20=120.0)

    with patch.object(AlertAgent, "send_email", return_value=True) as spy:
        count = agent.check_portfolio_stops([pos], {})

    assert count == 1
    assert spy.call_count == 1
    assert "Profit Target Reached: AAPL" in spy.call_args.args[0]
    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "profit_target"


# ── Edge cases ─────────────────────────────────────────────────────────────


def test_held_null_levels_skipped(tmp_path) -> None:
    agent = _agent(tmp_path)
    # SIPP-imported style position: NULL stop_loss/entry_price/target.
    pos = _position("SIPP", 50.0)

    with patch.object(AlertAgent, "send_email") as spy:
        count = agent.check_portfolio_stops([pos], {})

    assert count == 0
    spy.assert_not_called()
    assert build_notifications_repository().recent() == []


@patch("smtplib.SMTP")
def test_held_and_watched_same_ticker_dedups(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    # NVDA is both a watched row hitting its stop and a held stop-loss.
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent.check_positions([_stock("NVDA", 88.0)])
    assert agent._watched_stops == [("NVDA", 88.0, 90.0)]

    held = _position("NVDA", 88.0, stop_loss=90.0, entry_price=100.0)

    sends: list[str] = []

    def _capture(subject: str, html: str, text: str) -> bool:
        sends.append(html)
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.check_portfolio_stops([held], {})  # 1 immediate held email
        agent.send_summary_email()  # digest

    # Exactly one immediate email + one digest = two sends total.
    assert len(sends) == 2
    digest_html = sends[1]
    # Held SELL card present; watched stop for the same ticker suppressed.
    assert "SELL / STOP LOSS" in digest_html
    assert "WATCHLIST STOP LOSSES" not in digest_html
    # One immediate held notification; digest adds none for this ticker.
    items = [i for i in build_notifications_repository().recent() if i.ticker == "NVDA"]
    assert len(items) == 1
    assert items[0].event_type == "stop_loss_hit"
