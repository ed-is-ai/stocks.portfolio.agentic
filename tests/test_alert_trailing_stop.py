"""Tests for trailing-stop / large-adverse-move detection on held positions
(#82).

A held position that falls ``trailing_stop_pct`` from its persisted
high-water-mark fires one immediate email + one digest entry (``_sell_alerts``)
+ one persisted notification, once per run, deduped against a hard stop-loss
that already triggered for the same ticker in the same run.
"""

from unittest.mock import patch

from app.agents.alert.alert_agent import AlertAgent
from app.repositories.notifications_repo import build_notifications_repository
from app.schemas import EmailConfig, Position

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


def _position(
    ticker: str,
    current_price: float | None,
    *,
    entry_price: float | None = None,
    stop_loss: float | None = None,
) -> Position:
    return Position(
        ticker=ticker,
        shares=100.0,
        avg_cost=100.0,
        total_cost=10_000.0,
        current_price=current_price,
        entry_price=entry_price,
        stop_loss=stop_loss,
    )


def _agent(tmp_path, email: EmailConfig = _EMAIL) -> AlertAgent:
    agent = AlertAgent(db_path=str(tmp_path / "alerts.db"), email_config=email)
    agent._alerts.ensure_schema()
    return agent


# ── High-water-mark tracking ───────────────────────────────────────────────


def test_hwm_seeds_from_entry_price(tmp_path) -> None:
    agent = _agent(tmp_path)
    # Peak seeded from entry (150) even though current (140) is lower.
    agent.check_trailing_stops([_position("NVDA", 140.0, entry_price=150.0)])
    assert agent._position_state.get_hwm("NVDA") == 150.0


def test_hwm_seeds_from_current_when_entry_null(tmp_path) -> None:
    agent = _agent(tmp_path)
    # SIPP-imported style: entry_price is None → seed from current price.
    agent.check_trailing_stops([_position("SIPP", 80.0)])
    assert agent._position_state.get_hwm("SIPP") == 80.0


def test_hwm_rises_but_never_decreases_across_runs(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.check_trailing_stops([_position("AMD", 100.0)])
    agent.check_trailing_stops([_position("AMD", 130.0)])  # new high
    assert agent._position_state.get_hwm("AMD") == 130.0
    agent.check_trailing_stops([_position("AMD", 120.0)])  # dip
    assert agent._position_state.get_hwm("AMD") == 130.0


# ── Trigger boundary ───────────────────────────────────────────────────────


@patch("smtplib.SMTP")
def test_trailing_trigger_fires_at_threshold(mock_smtp, tmp_path) -> None:
    agent = _agent(tmp_path)
    # Establish a peak of 100, then drop exactly to the 15% threshold (85).
    agent.check_trailing_stops([_position("TSLA", 100.0)], trailing_stop_pct=0.15)

    with patch.object(AlertAgent, "send_email", return_value=True) as spy:
        count = agent.check_trailing_stops(
            [_position("TSLA", 85.0)], trailing_stop_pct=0.15
        )

    assert count == 1
    assert spy.call_count == 1
    assert "Trailing Stop: TSLA" in spy.call_args.args[0]
    assert agent._sell_alerts and agent._sell_alerts[0][0].ticker == "TSLA"
    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "trailing_stop"
    assert items[0].ticker == "TSLA"


def test_trailing_no_trigger_just_above_threshold(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent.check_trailing_stops([_position("TSLA", 100.0)], trailing_stop_pct=0.15)

    with patch.object(AlertAgent, "send_email") as spy:
        count = agent.check_trailing_stops(
            [_position("TSLA", 85.01)], trailing_stop_pct=0.15
        )

    assert count == 0
    spy.assert_not_called()
    assert build_notifications_repository().recent() == []


def test_trailing_persists_when_email_unconfigured(tmp_path) -> None:
    agent = _agent(tmp_path, email=_UNCONFIGURED)
    agent.check_trailing_stops([_position("TSLA", 100.0)], trailing_stop_pct=0.15)

    count = agent.check_trailing_stops(
        [_position("TSLA", 80.0)], trailing_stop_pct=0.15
    )

    assert count == 1
    items = build_notifications_repository().recent()
    assert len(items) == 1
    assert items[0].event_type == "trailing_stop"


# ── Graceful skips ─────────────────────────────────────────────────────────


def test_no_current_price_skipped(tmp_path) -> None:
    agent = _agent(tmp_path)
    with patch.object(AlertAgent, "send_email") as spy:
        count = agent.check_trailing_stops(
            [_position("NVDA", None)], trailing_stop_pct=0.15
        )
    assert count == 0
    spy.assert_not_called()
    assert agent._position_state.get_hwm("NVDA") is None


def test_invalid_pct_updates_hwm_but_never_fires(tmp_path) -> None:
    agent = _agent(tmp_path)
    # Peak 100, deep drop, but pct invalid → no alert, peak still tracked.
    agent.check_trailing_stops([_position("TSLA", 100.0)], trailing_stop_pct=0.0)
    with patch.object(AlertAgent, "send_email") as spy:
        count = agent.check_trailing_stops(
            [_position("TSLA", 10.0)], trailing_stop_pct=0.0
        )
    assert count == 0
    spy.assert_not_called()
    assert agent._position_state.get_hwm("TSLA") == 100.0
    assert build_notifications_repository().recent() == []


# ── Dedup with a hard stop-loss in the same run ────────────────────────────


@patch("smtplib.SMTP")
def test_hard_stop_and_trailing_single_alert(mock_smtp, tmp_path) -> None:
    agent = _agent(tmp_path)
    # Seed a peak so the same drop would also breach the trailing threshold.
    agent.check_trailing_stops([_position("NVDA", 100.0)], trailing_stop_pct=0.15)

    # NVDA falls to 80: breaches both its hard stop (90) and the trailing level.
    pos = _position("NVDA", 80.0, entry_price=100.0, stop_loss=90.0)

    with patch.object(AlertAgent, "send_email", return_value=True) as spy:
        agent.check_portfolio_stops([pos], {})  # hard stop fires
        trailing = agent.check_trailing_stops([pos], trailing_stop_pct=0.15)

    assert trailing == 0  # trailing suppressed for the already-alerted ticker
    assert spy.call_count == 1  # only the hard-stop email
    items = [i for i in build_notifications_repository().recent() if i.ticker == "NVDA"]
    assert len(items) == 1
    assert items[0].event_type == "stop_loss_hit"
    # One SELL digest entry for NVDA, not two.
    assert [p.ticker for p, _ in agent._sell_alerts] == ["NVDA"]
