"""Tests for the market_narrative seam on AlertAgent.send_summary_email (#109).

Phase 1 passes a deterministic MarketNarrative; the digest just needs to
render whatever typed object it is given (or nothing at all).
"""

from unittest.mock import patch

from app.agents.alert.alert_agent import AlertAgent
from app.schemas import EmailConfig, MarketNarrative
from app.schemas.market_narrative import MarketNarrativeSource

_EMAIL = EmailConfig(
    host="localhost",
    port=1025,
    user="user@example.com",
    password="pass",
    recipient="to@example.com",
)


def _agent(tmp_path) -> AlertAgent:
    agent = AlertAgent(db_path=str(tmp_path / "alerts.db"), email_config=_EMAIL)
    agent._alerts.ensure_schema()
    return agent


def test_narrative_rendered_in_text_and_html(tmp_path) -> None:
    agent = _agent(tmp_path)
    narrative = MarketNarrative(
        headline="Technology leads scan candidates",
        bullets=["Technology gained prevalence", "Mid-cycle between FOMC meetings"],
        not_advice="Informational only, not financial advice.",
    )
    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(market_narrative=narrative)

    assert "MARKET NARRATIVE" in captured["text"]
    assert "Technology leads scan candidates" in captured["text"]
    assert "Technology gained prevalence" in captured["text"]

    assert "Market Narrative" in captured["html"]
    assert "Technology leads scan candidates" in captured["html"]
    assert "Informational only, not financial advice." in captured["html"]


def test_narrative_sources_footnote_rendered_in_text_and_html(tmp_path) -> None:
    """Phase 3's Claude narrative populates `sources` — the digest must render it."""
    agent = _agent(tmp_path)
    narrative = MarketNarrative(
        headline="Technology leads scan candidates",
        bullets=["Reuters reports a rate pause."],
        sources=[
            MarketNarrativeSource(
                label="reuters.com", url="https://reuters.com/article"
            )
        ],
        not_advice="Informational only, not financial advice.",
    )
    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email(market_narrative=narrative)

    assert "reuters.com" in captured["text"]
    assert "https://reuters.com/article" in captured["text"]
    assert "reuters.com" in captured["html"]
    assert "https://reuters.com/article" in captured["html"]


def test_digest_omits_narrative_section_when_absent(tmp_path) -> None:
    agent = _agent(tmp_path)
    captured: dict[str, str] = {}

    def _capture(subject: str, html: str, text: str) -> bool:
        captured["html"] = html
        captured["text"] = text
        return True

    with patch.object(AlertAgent, "send_email", side_effect=_capture):
        agent.send_summary_email()

    assert "MARKET NARRATIVE" not in captured["text"]
    assert "Market Narrative" not in captured["html"]
