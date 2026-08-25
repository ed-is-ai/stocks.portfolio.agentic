"""Tests for digest-batched watched signals and immediate held-position
emails (#81).

Watched-setup entry/stop signals are folded into the per-run digest (no
individual emails); held-portfolio stop-loss / profit-target events fire an
immediate individual email and are persisted regardless of send success.
"""

from unittest.mock import MagicMock, patch

from app.agents.alert.alert_agent import AlertAgent
from app.core.alerting import classify_alert
from app.repositories.notifications_repo import build_notifications_repository
from app.schemas import (
    CANSLIMScore,
    EmailConfig,
    MarketNarrative,
    Position,
    StockAnalysis,
    StockRecord,
    StockScan,
)

# A SEPA template with 6/8 checks true — enough to clear the digest's
# LOW-CONVICTION filter (needs sepa_count >= 5) so a setup actually renders.
_STRONG_SEPA = {
    "above_150_200": True,
    "sma150_above_200": True,
    "sma200_rising": True,
    "sma50_above_150_200": True,
    "above_25pct_of_low": True,
    "within_25pct_of_high": True,
    "rs_leader": False,
    "above_sma50": False,
}


def _buy_record(ticker: str, *, breakout: bool) -> StockRecord:
    """Build a conviction-passing setup: a fresh breakout or a Stay-Alert name."""
    scan = StockScan(
        ticker=ticker,
        as_of="2024-01-01",
        price=100.0,
        volume=1_000_000,
        rel_volume=2.0,
        high_52w=110.0,
        low_52w=50.0,
        pct_from_52w_high=-5.0,
        pct_change_week=4.0,
    )
    analysis = StockAnalysis(
        score=9,
        stage="Stage 2",
        entry_zone="approaching",
        fresh_breakout=breakout,
        sepa_template=_STRONG_SEPA,
        strengths=["Strong momentum"],
        risks=[],
        summary="setup",
    )
    return StockRecord.model_validate(
        {**scan.model_dump(), "analysis": analysis.model_dump()}
    )


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

    nvda, amd = _stock("NVDA", 105.0), _stock("AMD", 175.0)
    with patch.object(AlertAgent, "send_email") as spy:
        agent.check_positions([nvda, amd])

    spy.assert_not_called()
    assert agent._entry_triggered == [("NVDA", 105.0, 100.0, nvda)]
    assert agent._watched_stops == [("AMD", 175.0, 180.0, amd)]


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

    assert "BUY" in captured["html"]
    assert "crossed entry" in captured["html"]
    assert "STOPPED OUT" in captured["html"]
    assert "NVDA" in captured["text"]
    assert "AMD" in captured["text"]

    events = {i.event_type for i in build_notifications_repository().recent()}
    assert events == {"entry_triggered", "stop_loss_hit"}


@patch("smtplib.SMTP")
def test_tracked_signals_reuse_current_run_analysis(mock_smtp, tmp_path) -> None:
    """A tracked entry/stop ticker present in the current run's stocks with
    a CANSLIM score renders that real breakdown instead of the unavailable
    state (#312) — analysis is retained, not discarded.
    """
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    nvda = _buy_record("NVDA", breakout=False).model_copy(update={"price": 105.0})
    assert nvda.analysis is not None
    nvda = nvda.model_copy(
        update={
            "analysis": nvda.analysis.model_copy(
                update={"canslim": CANSLIMScore(C=2, A=2, N=1, S=1, L=2, I=0, M=2)}
            )
        }
    )
    agent.check_positions([nvda])

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    assert "CANSLIM 10/14" in captured["html"]
    assert ">unavailable</td>" not in captured["html"]
    assert "CANSLIM 10/14: C=2 A=2 N=1 S=1 L=2 I=0 M=2" in captured["text"]


@patch("smtplib.SMTP")
def test_tracked_signal_without_analysis_shows_unavailable(mock_smtp, tmp_path) -> None:
    """A tracked entry/stop ticker absent from the current run's stocks (or
    lacking CANSLIM) renders the explicit unavailable state (#312).
    """
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent.check_positions([_stock("NVDA", 105.0)])

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    assert captured["html"].count(">unavailable</td>") == 7
    assert "CANSLIM: unavailable" in captured["text"]


@patch("smtplib.SMTP")
def test_sell_card_canslim_present(mock_smtp, tmp_path) -> None:
    """A held stop-loss tile shows the full CANSLIM breakdown when the
    matched StockRecord has one (#312).
    """
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    tsla = _buy_record("TSLA", breakout=False)
    assert tsla.analysis is not None
    tsla = tsla.model_copy(
        update={
            "analysis": tsla.analysis.model_copy(
                update={"canslim": CANSLIMScore(C=0, A=0, N=1, S=1, L=0, I=1, M=1)}
            )
        }
    )
    agent.check_portfolio_stops([pos], {"TSLA": tsla})

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos])

    assert "CANSLIM 4/14" in captured["html"]
    assert "CANSLIM 4/14: C=0 A=0 N=1 S=1 L=0 I=1 M=1" in captured["text"]


@patch("smtplib.SMTP")
def test_sell_card_canslim_absent(mock_smtp, tmp_path) -> None:
    """A held stop-loss tile with no matched StockRecord renders the
    explicit unavailable state, not an omitted section (#312).
    """
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    agent.check_portfolio_stops([pos], {})

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos])

    assert captured["html"].count(">unavailable</td>") == 7
    assert "CANSLIM: unavailable" in captured["text"]


@patch("smtplib.SMTP")
def test_buy_alerts_split_into_breaking_out_and_approaching(mock_smtp, tmp_path):
    # A fresh breakout and a Stay-Alert (approaching) name must render under
    # separate, correctly-labelled sections rather than one BUY/BREAKOUT
    # bucket (#162).
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    breakout = _buy_record("BRKO", breakout=True)
    approaching = _buy_record("WTCH", breakout=False)
    # Sanity: the classifier agrees on which is which.
    assert classify_alert(breakout).trigger is not None
    assert classify_alert(approaching).trigger is None
    agent._buy_alerts = [
        (breakout, classify_alert(breakout).label or ""),
        (approaching, classify_alert(approaching).label or ""),
    ]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    for body in (captured["html"], captured["text"]):
        assert "BUY" in body
        assert "APPROACHING ENTRY" in body
        # Breaking-out/entry-reached (merged into BUY, #168) precedes approaching.
        assert body.index("BUY") < body.index("APPROACHING ENTRY")
    # CTA is present in both renderings.
    assert "How To Use This Email" in captured["html"]
    assert "HOW TO USE THIS EMAIL" in captured["text"]
    assert "BRKO" in captured["text"] and "WTCH" in captured["text"]


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
    nvda, amd = _stock("NVDA", 105.0), _stock("AMD", 175.0)
    agent.check_positions([nvda, amd])
    assert agent._watched_stops == [("AMD", 175.0, 180.0, amd)]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        return True

    # Nothing held → AMD's stop is suppressed; NVDA's entry still fires.
    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[])

    assert "BUY" in captured["html"]
    assert "STOPPED OUT" not in captured["html"]

    events = {i.event_type for i in build_notifications_repository().recent()}
    assert "entry_triggered" in events
    assert "stop_loss_hit" not in events

    # Bookkeeping retained: AMD left the watchlist (row marked "stopped").
    watching = {ticker for _rowid, ticker, _entry, _stop in agent._alerts.watching()}
    assert "AMD" not in watching


# ── CTA + section layout (#168) ─────────────────────────────────────────────


@patch("smtplib.SMTP")
def test_narrative_snapshot_columns_stack_on_mobile(mock_smtp, tmp_path) -> None:
    """The narrative/snapshot two-column layout must collapse to one column
    under a mobile viewport width, not stay side-by-side.
    """
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    narrative = MarketNarrative(
        headline="Breadth improving", bullets=["Small-caps leading"]
    )

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos], market_narrative=narrative)

    html = captured["html"]
    assert '<meta name="viewport"' in html
    assert "@media only screen and (max-width: 600px)" in html
    assert "stack-col" in html
    assert html.count('class="stack-col"') == 2


@patch("smtplib.SMTP")
def test_cta_sits_between_narrative_snapshot_and_sell_section(
    mock_smtp, tmp_path
) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    agent.check_portfolio_stops([pos], {})
    narrative = MarketNarrative(
        headline="Breadth improving", bullets=["Small-caps leading"]
    )

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos], market_narrative=narrative)

    html = captured["html"]
    assert (
        html.index("Stock Scanner Summary")
        < html.index("Market Narrative")
        < html.index("How To Use This Email")
        < html.index("SELL / STOP LOSS")
    )

    text = captured["text"]
    assert (
        text.index("Stock Scanner Summary")
        < text.index("MARKET NARRATIVE")
        < text.index("HOW TO USE THIS EMAIL")
        < text.index("SELL / STOP LOSS")
    )


@patch("smtplib.SMTP")
def test_cta_group_order_matches_sections(mock_smtp, tmp_path) -> None:
    # SELL -> STOPPED OUT -> BUY -> WATCH, both in the CTA and in the actual
    # sections it precedes (#168 review feedback).
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    agent.check_portfolio_stops([pos], {})
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent.check_positions([_stock("NVDA", 105.0)])  # NVDA -> entry_triggered
    approaching = _buy_record("WTCH", breakout=False)
    agent._buy_alerts = [(approaching, classify_alert(approaching).label or "")]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos])

    # CTA's own group order, via description text unique to the CTA copy.
    html = captured["html"]
    assert (
        html.index("review for exit")
        < html.index("see the reason on each row below")
        < html.index("wait for the trigger")
    )
    # The actual sections' order, via text unique to each real section.
    assert (
        html.index("(held)")
        < html.index("crossed entry")
        < html.index("APPROACHING ENTRY")
    )

    text = captured["text"]
    assert (
        text.index("review for exit")
        < text.index("see the reason on each row below")
        < text.index("wait for the trigger")
    )
    assert (
        text.index("(held)")
        < text.index("crossed entry")
        < text.index("APPROACHING ENTRY")
    )


@patch("smtplib.SMTP")
def test_entry_reached_and_breaking_out_merge_into_one_buy_section(
    mock_smtp, tmp_path
) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "NVDA", 8, "Stage 2", "setup", entry_price=100.0, stop_loss=90.0
    )
    agent.check_positions([_stock("NVDA", 105.0)])  # -> entry_triggered
    breakout = _buy_record("BRKO", breakout=True)
    agent._buy_alerts = [(breakout, classify_alert(breakout).label or "")]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    html = captured["html"]
    text = captured["text"]
    # CTA heading + the one merged section header, in each format.
    assert html.count("BUY") == 2
    assert text.count("BUY") == 2

    for body in (html, text):
        assert "ENTRY REACHED" not in body
        assert "BREAKING OUT NOW" not in body
        # Per-row detail survives the merge: the watched-row reason and the
        # breakout's trigger label are both still present underneath.
        assert "crossed entry" in body
        assert "NVDA" in body
        assert "BRKO" in body


@patch("smtplib.SMTP")
def test_stopped_out_moves_next_to_sell(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    agent.check_portfolio_stops([pos], {})
    agent._alerts.record(
        "AMD", 8, "Stage 2", "setup", entry_price=200.0, stop_loss=180.0
    )
    agent.check_positions([_stock("AMD", 175.0)])  # AMD held & stopped out
    breakout = _buy_record("BRKO", breakout=True)
    agent._buy_alerts = [(breakout, classify_alert(breakout).label or "")]

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos, _position("AMD", 175.0)])

    for body in (captured["html"], captured["text"]):
        assert body.index("(held)") < body.index("STOPPED OUT") < body.index("BRKO")


@patch("smtplib.SMTP")
def test_cta_folds_stopped_out_into_sell_group_no_extra_group(
    mock_smtp, tmp_path
) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    agent._alerts.record(
        "AMD", 8, "Stage 2", "setup", entry_price=200.0, stop_loss=180.0
    )
    agent.check_positions([_stock("AMD", 175.0)])

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[_position("AMD", 175.0)])

    html = captured["html"]
    assert "How to read" not in html
    assert "How To Use This Email" in html
    text = captured["text"]
    assert "HOW TO READ" not in text
    assert "HOW TO USE THIS EMAIL" in text

    # Exactly one SELL/stop-loss CTA group — stopped-out isn't a 4th line.
    assert html.count("review for exit") == 1
    assert text.count("review for exit") == 1


def test_cta_no_alerts_fallback(tmp_path) -> None:
    agent = _agent(tmp_path)

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    for body in (captured["html"], captured["text"]):
        assert "No actionable tickers today" in body
        assert "Informational only" in body
    assert "How To Use This Email" not in captured["html"]
    assert "HOW TO USE THIS EMAIL" not in captured["text"]


@patch("smtplib.SMTP")
def test_narrative_and_snapshot_render_as_two_columns(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    narrative = MarketNarrative(headline="Breadth improving", bullets=["Leaders up"])

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(
            positions=[_position("NVDA", 110.0)], market_narrative=narrative
        )

    html = captured["html"]
    assert "<table" in html
    # The 2-column wrapper table opens before either card's content, since
    # both cards are nested inside its <td> cells.
    assert (
        html.index("<table")
        < html.index("Market Narrative")
        < html.index("Portfolio Snapshot")
    )
    # Plain-text has no notion of columns — narrative still precedes snapshot
    # sequentially, with no table markup.
    text = captured["text"]
    assert "<table" not in text
    assert text.index("MARKET NARRATIVE") < text.index("PORTFOLIO SNAPSHOT")


@patch("smtplib.SMTP")
def test_snapshot_positive_pnl_is_blue_not_green(mock_smtp, tmp_path) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[_position("NVDA", 110.0)])

    snapshot_start = captured["html"].index("Portfolio Snapshot")
    snapshot_end = captured["html"].index("</table>", snapshot_start) + len("</table>")
    snapshot_html = captured["html"][snapshot_start:snapshot_end]
    assert "#2980b9" in snapshot_html
    assert "#27ae60" not in snapshot_html


@patch("smtplib.SMTP")
def test_cta_html_collapsed_by_default_text_always_expanded(
    mock_smtp, tmp_path
) -> None:
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    agent = _agent(tmp_path)
    pos = _position("TSLA", 89.0, stop_loss=90.0, entry_price=100.0)
    agent.check_portfolio_stops([pos], {})

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(positions=[pos])

    html = captured["html"]
    assert "<details" in html
    # No `open` attribute -> collapsed by default.
    details_tag = html[html.index("<details") : html.index(">", html.index("<details"))]
    assert "open" not in details_tag
    assert "<summary" in html
    assert "How To Use This Email" in html

    # Plain-text has no collapse concept; content is unconditionally present.
    text = captured["text"]
    assert "<details" not in text
    assert "HOW TO USE THIS EMAIL" in text


def test_cta_no_alerts_fallback_html_not_collapsible(tmp_path) -> None:
    agent = _agent(tmp_path)

    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    # Nothing to hide on a quiet day -> plain div, not a <details> disclosure.
    assert "<details" not in captured["html"]


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
    nvda = _stock("NVDA", 88.0)
    agent.check_positions([nvda])
    assert agent._watched_stops == [("NVDA", 88.0, 90.0, nvda)]

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
    assert "STOPPED OUT" not in digest_html
    # One immediate held notification; digest adds none for this ticker.
    items = [i for i in build_notifications_repository().recent() if i.ticker == "NVDA"]
    assert len(items) == 1
    assert items[0].event_type == "stop_loss_hit"
