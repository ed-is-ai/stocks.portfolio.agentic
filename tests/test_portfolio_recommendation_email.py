"""End-to-end tests for the per-portfolio recommendation email dispatch (#442).

Wires the real ``PortfolioRecommendationService`` (scripted Strategy, tmp
artifact) and the real ``AlertAgent`` rendering path (SMTP captured) through
``PortfolioRecommendationEmailService`` against tmp databases.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.agents.alert.alert_agent import AlertAgent
from app.repositories import db
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.portfolio_dispatch_repo import PortfolioDispatchRepository
from app.repositories.portfolio_strategies_repo import PortfolioStrategiesRepository
from app.schemas import EmailConfig
from app.schemas.analysis_artifact import build_analysis_payload
from app.schemas.portfolio_recommendation import RecommendationResultV1
from app.services import portfolio_recommendation_service as svc_module
from app.services import strategy_assignment_service as assignment_module
from app.services.portfolio_recommendation_email_service import (
    PortfolioRecommendationEmailService,
)
from app.services.portfolio_recommendation_service import (
    PortfolioRecommendationService,
)
from app.services.strategy_assignment_service import StrategyAssignmentService
from tests.test_portfolio_recommendation_service import (
    HistoryOnlyStrategy,
    _position,
    _record,
)
from tests.test_strategy_assignment_service import _discovery_result

_EMAIL = EmailConfig(
    host="localhost",
    port=1025,
    user="user@example.com",
    password="pass",
    recipient="to@example.com",
)

_PORTFOLIOS = ((7, "SIPP"), (8, "GIA"))


def _write_artifact(
    path: Path,
    records: list[dict[str, Any]],
    *,
    run_id: str = "run-1",
    generated_at: datetime,
) -> None:
    """Write a published-shape analysis artifact with embedded ownership."""
    path.write_text(
        json.dumps(
            build_analysis_payload(records, run_id=run_id, generated_at=generated_at)
        ),
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Two portfolios, a tmp artifact (AAA falling, BBB rising), full wiring."""
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    db.init_trades_db(conn)
    conn.executemany(
        "INSERT INTO portfolios (id, name, created_at) VALUES (?, ?, 'now')",
        _PORTFOLIOS,
    )
    conn.commit()
    conn.close()

    artifact = tmp_path / "analysis.json"
    records = [_record("AAA", [12.0, 10.0]), _record("BBB", [10.0, 12.0])]
    _write_artifact(artifact, records, generated_at=datetime.now(UTC))

    monkeypatch.setattr(
        assignment_module, "discover_strategies", lambda root: _discovery_result()
    )
    monkeypatch.setattr(svc_module, "ANALYSIS_JSON", artifact)

    assignment = StrategyAssignmentService(
        PortfolioStrategiesRepository(db.make_connect(lambda: db_path)),
        skills_root=tmp_path / "skills",
        analysis_path=artifact,
    )
    trader = MagicMock()
    holdings = {7: [_position("AAA")], 8: [_position("BBB")]}
    trader.get_portfolio.side_effect = lambda portfolio_id=None: list(
        holdings.get(portfolio_id, [])
    )
    trader.get_cash_balance.return_value = 1000.0
    names = dict(_PORTFOLIOS)
    trader.get_portfolio_meta.side_effect = lambda portfolio_id: SimpleNamespace(
        name=names.get(portfolio_id)
    )
    recommendation = PortfolioRecommendationService(
        assignment_service=assignment,
        trader=trader,
        skills_root=tmp_path / "skills",
        loader=lambda path: HistoryOnlyStrategy(),
    )
    alerter = AlertAgent(db_path=str(tmp_path / "alerts.db"), email_config=_EMAIL)
    alerter._alerts.ensure_schema()
    notifications = NotificationsRepository(
        db.make_connect(lambda: str(tmp_path / "notifications.db"))
    )
    notifications.ensure_schema()
    repo = PortfolioDispatchRepository(db.make_connect(lambda: db_path))

    sent: list[tuple[str, str, str]] = []

    def make_service(
        recommendation_service: Any = None,
    ) -> PortfolioRecommendationEmailService:
        return PortfolioRecommendationEmailService(
            assignment_service=assignment,
            recommendation_service=(
                recommendation_service
                if recommendation_service is not None
                else recommendation
            ),
            trader=trader,
            sender=alerter.send_portfolio_recommendation_email,
            notifications=notifications,
            repo=repo,
            analysis_path=artifact,
        )

    def dispatch(
        service: PortfolioRecommendationEmailService | None = None,
        *,
        send_ok: bool = True,
    ) -> tuple[Any, list[tuple[str, str, str]]]:
        """Run one dispatch pass with SMTP captured; return (summary, sends)."""
        target = service if service is not None else make_service()
        sent.clear()

        def _capture(subject: str, html: str, text: str) -> bool:
            sent.append((subject, html, text))
            return send_ok

        with patch.object(AlertAgent, "send_email", side_effect=_capture):
            summary = target.dispatch_all()
        return summary, list(sent)

    def rewrite_artifact(
        *, run_id: str = "run-1", generated_at: datetime | None = None
    ) -> None:
        _write_artifact(
            artifact,
            records,
            run_id=run_id,
            generated_at=(
                generated_at if generated_at is not None else datetime.now(UTC)
            ),
        )

    return SimpleNamespace(
        assignment=assignment,
        recommendation=recommendation,
        notifications=notifications,
        repo=repo,
        artifact=artifact,
        trader=trader,
        tmp_path=tmp_path,
        make_service=make_service,
        dispatch=dispatch,
        rewrite_artifact=rewrite_artifact,
    )


def test_multi_portfolio_isolation(env: Any) -> None:
    """Each portfolio's email contains only its own scoped actions (#442.5)."""
    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    summary, sent = env.dispatch()
    assert summary.sent == 2
    assert len(sent) == 2
    # Portfolio 7 holds AAA (falling → Sell; BBB rising → Buy).
    assert "AAA" in sent[0][1]
    assert "BBB" in sent[0][1]
    # Portfolio 8 holds BBB (rising → Hold); AAA never leaks in.
    assert "BBB" in sent[1][1]
    assert "AAA" not in sent[1][1]


def test_screen_email_parity(env: Any) -> None:
    """The email renders exactly the typed result's rows, in group order."""
    env.assignment.assign(7, "alpha")
    summary, sent = env.dispatch()
    assert summary.sent == 1
    result = env.recommendation.recommend(7)
    assert isinstance(result, RecommendationResultV1)
    html = sent[0][1]
    for rec in result.recommendations:
        assert rec.ticker in html
        assert rec.rule_id in html
    # Fixed Sell → Hold → Buy section order, matching the screen.
    assert html.index("SELL (") < html.index("HOLD (") < html.index("BUY (")


def test_template_structure(env: Any) -> None:
    """Subject identifies portfolio/Strategy/date; provenance footer present."""
    env.assignment.assign(7, "alpha")
    summary, sent = env.dispatch()
    assert summary.sent == 1
    subject, html, text = sent[0]
    assert "SIPP" in subject
    assert "Alpha" in subject
    assert re.search(r"\d{4}-\d{2}-\d{2}", subject) is not None
    assert "No trades were placed" in html
    assert "run-1" in html
    assert "a" * 8 in html  # strategy_source_digest prefix
    assert "No trades were placed" in text
    assert "run-1" in text


def test_idempotent_retry_and_new_run(env: Any) -> None:
    """One send per (portfolio, run); a later run claims fresh (#442.7)."""
    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    first, sent_first = env.dispatch()
    assert first.sent == 2
    second, sent_second = env.dispatch()
    assert second.sent == 0
    assert second.skipped == 2
    assert sent_second == []
    env.rewrite_artifact(run_id="run-2")
    third, sent_third = env.dispatch()
    assert third.sent == 2
    assert len(sent_third) == 2


def test_no_assignment_suppression(env: Any) -> None:
    """Unassigned portfolios get nothing; clearing stops future sends."""
    env.assignment.assign(7, "alpha")
    summary, sent = env.dispatch()
    assert summary.sent == 1
    assert len(sent) == 1
    assert "SIPP" in sent[0][0]
    env.rewrite_artifact(run_id="run-2")
    env.assignment.clear(7)
    cleared, sent_cleared = env.dispatch()
    assert cleared.sent == 0
    assert cleared.skipped == 0
    assert sent_cleared == []


def test_stale_artifact_still_sends_with_banner(env: Any) -> None:
    """A stale-but-usable result is sent, with the freshness banner (#442.8)."""
    env.assignment.assign(7, "alpha")
    env.rewrite_artifact(generated_at=datetime.now(UTC) - timedelta(hours=25))
    summary, sent = env.dispatch()
    assert summary.sent == 1
    assert "Scan data is stale" in sent[0][1]
    assert "Scan data is stale" in sent[0][2]


def test_evaluation_failure_is_isolated(env: Any) -> None:
    """One portfolio's evaluation failure never blocks the others (#442.8)."""

    class _RaisingFor7:
        """Wrapper raising for portfolio 7, delegating everything else."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def recommend(self, portfolio_id: int) -> Any:
            if portfolio_id == 7:
                raise RuntimeError("boom")
            return self._inner.recommend(portfolio_id)

    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    service = env.make_service(recommendation_service=_RaisingFor7(env.recommendation))
    summary, sent = env.dispatch(service=service)
    assert summary.sent == 1
    assert summary.failed >= 1
    assert len(sent) == 1
    assert "GIA" in sent[0][0]


def test_send_failure_records_failed_receipt_and_notification(env: Any) -> None:
    """SMTP unconfigured → send_email False → failed receipt + notification."""
    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    summary, sent = env.dispatch(send_ok=False)
    assert summary.sent == 0
    assert summary.failed == 2
    assert len(sent) == 2  # sends were attempted, transport refused
    assert env.repo.was_sent(7, "run-1") is False
    assert env.repo.was_sent(8, "run-1") is False
    events = [n.event_type for n in env.notifications.recent(limit=50)]
    assert events.count("recommendation_email_failed") == 2
    assert "recommendation_emails_dispatched" in events


def test_stuck_claimed_receipt_is_surfaced_not_silent(env: Any) -> None:
    """A receipt stuck in 'claimed' (crash between send and mark_sent) is
    reported to the notification centre — at-most-once, but not silent."""
    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    env.repo.claim(7, "run-1", "alpha")  # simulate the interrupted attempt
    summary, sent = env.dispatch()
    assert summary.sent == 1  # portfolio 8 still goes out
    assert summary.skipped >= 1
    events = [
        n
        for n in env.notifications.recent(limit=20)
        if n.event_type == "recommendation_email_failed"
    ]
    assert any("interrupted" in n.title for n in events)


def test_no_assignment_writes_skipped_not_failed_receipt(env: Any) -> None:
    """A cleared assignment is excluded from listing entirely: no email, no
    receipt row, and portfolio 7's dispatch is unaffected."""
    env.assignment.assign(7, "alpha")
    env.assignment.assign(8, "alpha")
    env.assignment._repo.clear(8)
    env.dispatch()
    assert env.repo.status_of(8, "run-1") is None
    assert env.repo.status_of(7, "run-1") == "sent"


def test_unconfigured_smtp_end_to_end(env: Any) -> None:
    """The real unconfigured-EmailConfig path returns False → failed receipt
    + notification, with no SMTP contact and no exception escaping."""
    env.assignment.assign(7, "alpha")
    unconfigured = AlertAgent(
        db_path=str(env.tmp_path / "alerts2.db"),
        email_config=EmailConfig(
            host="localhost", port=1025, user="", password="", recipient=""
        ),
    )
    service = PortfolioRecommendationEmailService(
        assignment_service=env.assignment,
        recommendation_service=env.recommendation,
        trader=env.trader,
        sender=unconfigured.send_portfolio_recommendation_email,
        notifications=env.notifications,
        repo=env.repo,
        analysis_path=env.artifact,
    )
    summary = service.dispatch_all()
    assert summary.sent == 0
    assert summary.failed == 1
    assert env.repo.status_of(7, "run-1") == "failed"


def test_summary_notification_skipped_when_nothing_to_do(env: Any) -> None:
    """No assignments → no '0 sent, 0 failed' notification noise."""
    before = len(env.notifications.recent(limit=50))
    env.dispatch()
    after = len(env.notifications.recent(limit=50))
    assert after == before


def test_orchestrator_hook_wires_and_isolates(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator's extracted hook builds the real service from the DI
    providers, passes the pipeline run_id, and swallows total failures with
    a notification (never propagates into the pipeline)."""
    from app.orchestration.orchestrator import dispatch_recommendation_emails

    env.assignment.assign(7, "alpha")
    captured: dict[str, Any] = {}

    class _FakeAlerter:
        def send_portfolio_recommendation_email(
            self, *args: Any, **kwargs: Any
        ) -> bool:
            captured["args"] = args
            return True

    fake_alerter = _FakeAlerter()

    def fake_recommendation_service() -> Any:
        return env.recommendation

    monkeypatch.setattr(
        "app.api.dependencies.get_strategy_assignment_service",
        lambda: env.assignment,
    )
    monkeypatch.setattr(
        "app.api.dependencies.get_portfolio_recommendation_service",
        fake_recommendation_service,
    )
    monkeypatch.setattr(
        "app.api.dependencies.get_notifications_repository",
        lambda: env.notifications,
    )
    monkeypatch.setattr(
        "app.api.dependencies.get_portfolio_dispatch_repository",
        lambda: env.repo,
    )
    # The hook must not raise even when the whole dispatch explodes...
    monkeypatch.setattr(
        "app.api.dependencies.get_portfolio_dispatch_repository",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    dispatch_recommendation_emails(fake_alerter, env.trader, "run-1")  # swallowed
    # ...and must dispatch end-to-end when the providers are healthy.
    monkeypatch.setattr(
        "app.api.dependencies.get_portfolio_dispatch_repository",
        lambda: env.repo,
    )
    # The email service resolves the artifact path from its own module
    # constant — point it at the tmp artifact like the recommendation service.
    monkeypatch.setattr(
        "app.services.portfolio_recommendation_email_service.ANALYSIS_JSON",
        env.artifact,
    )
    dispatch_recommendation_emails(fake_alerter, env.trader, "run-1")
    assert "args" in captured
    assert env.repo.status_of(7, "run-1") == "sent"
