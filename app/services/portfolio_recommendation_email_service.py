"""Per-portfolio daily Strategy recommendation email dispatch (#442).

After the daily scan publishes its analysis artifact, one recommendation
email is sent per assigned portfolio, built from the same typed
``RecommendationResultV1`` the recommendations screen shows. Idempotency is
owned entirely by the ``portfolio_recommendation_dispatches`` receipt table
(claim → sent/failed); one portfolio's evaluation or SMTP failure never
affects the others, the pipeline, or the consolidated digest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable

from app.core.config import ANALYSIS_JSON
from app.repositories.notifications_repo import NotificationsRepository
from app.repositories.portfolio_dispatch_repo import PortfolioDispatchRepository
from app.schemas.analysis_artifact import read_analysis_artifact_meta
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.portfolio_recommendation import (
    NO_ASSIGNMENT,
    EvaluationUnavailable,
    NoAssignment,
)
from app.services.portfolio_recommendation_service import (
    PortfolioRecommendationService,
)
from app.services.strategy_assignment_service import StrategyAssignmentService

logger = logging.getLogger(__name__)

#: Bound ``AlertAgent.send_portfolio_recommendation_email``. The orchestrator
#: may bind ``market_narrative``/``portfolio_summary`` keywords via
#: ``functools.partial``, so the signature is intentionally open (``...``).
Sender = Callable[..., bool]


@dataclass(frozen=True)
class DispatchSummary:
    """One dispatch pass's outcome counts, recorded to the notification centre."""

    sent: int
    failed: int
    skipped: int


class PortfolioRecommendationEmailService:
    """Dispatch one recommendation email per assigned portfolio (#442).

    Every dependency is injectable for tests; the orchestrator wires the
    real ones after the analysis artifact publish so ``recommend()``
    evaluates the current run's data and receipts key on its ``run_id``.
    """

    def __init__(
        self,
        assignment_service: StrategyAssignmentService,
        recommendation_service: PortfolioRecommendationService,
        trader: Any,
        sender: Sender,
        notifications: NotificationsRepository,
        repo: PortfolioDispatchRepository,
        market_narrative: object | None = None,
        analysis_path: Path | None = None,
    ) -> None:
        """Store dependencies; ``analysis_path`` defaults to the published artifact."""
        self._assignment_service = assignment_service
        self._recommendation_service = recommendation_service
        self._trader = trader
        self._sender = sender
        self._notifications = notifications
        self._repo = repo
        self._market_narrative = market_narrative
        self._analysis_path = (
            analysis_path if analysis_path is not None else ANALYSIS_JSON
        )

    def dispatch_all(self, run_id: str | None = None) -> DispatchSummary:
        """Send one email per assigned portfolio for the published run.

        ``run_id`` is the pipeline's run identity when the caller has it
        (the orchestrator does); otherwise it is read from the published
        artifact's metadata. Each portfolio's whole block is
        failure-isolated.
        """
        meta = read_analysis_artifact_meta(Path(self._analysis_path))
        if meta is None:
            logger.warning(
                "Recommendation email dispatch skipped: no readable analysis "
                "artifact metadata at %s",
                self._analysis_path,
            )
            self._record_summary(run_id or "unknown", 0, 0, notify=False)
            return DispatchSummary(sent=0, failed=0, skipped=0)
        if run_id is None:
            run_id = meta.run_id
        elif run_id != meta.run_id:
            logger.warning(
                "Pipeline run id %r differs from artifact meta run id %r; "
                "keying receipts on the artifact",
                run_id,
                meta.run_id,
            )
            run_id = meta.run_id
        sender = self._sender
        if self._market_narrative is not None:
            sender = partial(self._sender, market_narrative=self._market_narrative)
        sent = failed = skipped = 0
        for assignment in self._assignment_service.list_assigned():
            try:
                if not self._repo.claim(
                    assignment.portfolio_id, run_id, assignment.strategy_id
                ):
                    self._handle_already_claimed(assignment.portfolio_id, run_id)
                    skipped += 1
                    continue
                outcome = self._dispatch_one(assignment.portfolio_id, sender)
                if isinstance(outcome, bool):
                    if outcome:
                        try:
                            self._repo.mark_sent(assignment.portfolio_id, run_id)
                        except Exception:
                            # The email went out; never misrecord it as failed.
                            logger.exception(
                                "Could not mark sent receipt for portfolio %s",
                                assignment.portfolio_id,
                            )
                        sent += 1
                    else:
                        self._record_failure(
                            assignment.portfolio_id,
                            run_id,
                            "Recommendation email failed — "
                            f"{self._portfolio_name(assignment.portfolio_id)}",
                        )
                        self._repo.mark_failed(assignment.portfolio_id, run_id)
                        failed += 1
                elif isinstance(outcome, NoAssignment):
                    # Assignment vanished between listing and evaluation:
                    # nothing was sent, and a re-created assignment may still
                    # claim this run — record 'skipped', not 'failed'.
                    self._repo.mark_skipped(assignment.portfolio_id, run_id)
                    skipped += 1
                else:
                    self._repo.mark_failed(assignment.portfolio_id, run_id)
                    self._record_failure(
                        assignment.portfolio_id,
                        run_id,
                        "Recommendation evaluation failed — "
                        f"{self._portfolio_name(assignment.portfolio_id)}",
                        body=outcome.reason,
                    )
                    failed += 1
            except Exception:
                logger.exception(
                    "Recommendation email dispatch failed for portfolio %s",
                    assignment.portfolio_id,
                )
                try:
                    self._repo.mark_failed(assignment.portfolio_id, run_id)
                except Exception:
                    logger.exception(
                        "Could not record failed dispatch receipt for "
                        "portfolio %s run %s",
                        assignment.portfolio_id,
                        run_id,
                    )
                self._record_failure(
                    assignment.portfolio_id,
                    run_id,
                    "Recommendation email dispatch error — "
                    f"{self._portfolio_name(assignment.portfolio_id)}",
                )
                failed += 1
        self._record_summary(run_id, sent, failed)
        return DispatchSummary(sent=sent, failed=failed, skipped=skipped)

    def _handle_already_claimed(self, portfolio_id: int, run_id: str) -> None:
        """Surface a receipt that is stuck in 'claimed' from a dead attempt.

        The dispatch contract is at-most-once (a crash between send and
        ``mark_sent`` never resends), but it must not be silent: an
        interrupted attempt is reported to the notification centre.
        """
        if self._repo.status_of(portfolio_id, run_id) == "claimed":
            self._record_failure(
                portfolio_id,
                run_id,
                "Recommendation email dispatch interrupted — "
                f"{self._portfolio_name(portfolio_id)}",
                body=(
                    "A previous dispatch attempt for this run never completed; "
                    "the email may not have been sent."
                ),
            )

    def _dispatch_one(
        self, portfolio_id: int, sender: Sender
    ) -> bool | NoAssignment | EvaluationUnavailable:
        """Evaluate, build, and send one portfolio's email.

        Returns the send outcome, or the typed non-result when there is
        nothing to send.
        """
        outcome = self._recommendation_service.recommend(portfolio_id)
        if isinstance(outcome, NoAssignment | EvaluationUnavailable):
            return outcome
        view = self._assignment_service.assignment_view(portfolio_id)
        if view is None:
            # The assignment vanished between listing and evaluation —
            # nothing to send (no-assignment suppression, #442).
            return NO_ASSIGNMENT
        display_name = view.display_name or outcome.strategy_id
        return sender(
            outcome,
            self._portfolio_name(portfolio_id),
            display_name,
            portfolio_summary=self._portfolio_summary(portfolio_id),
        )

    def _portfolio_summary(self, portfolio_id: int) -> dict[str, Any] | None:
        """Build the email's portfolio-summary rows from current holdings.

        Scoped strictly to this portfolio; a trader failure degrades to no
        section rather than blocking the email.
        """
        try:
            positions = self._trader.get_portfolio(portfolio_id=portfolio_id)
        except Exception:
            logger.exception(
                "Could not build portfolio summary for portfolio %s", portfolio_id
            )
            return None
        rows = [
            {
                "ticker": position.ticker,
                "shares": f"{position.shares:g}",
                "avg_cost": f"£{position.avg_cost:,.2f}",
            }
            for position in positions
            if position.shares > 0
        ]
        return {"rows": rows} if rows else None

    def _portfolio_name(self, portfolio_id: int) -> str:
        """Return the portfolio's display name, failing soft to a label."""
        get_meta = getattr(self._trader, "get_portfolio_meta", None)
        if get_meta is None:
            return f"Portfolio {portfolio_id}"
        meta = get_meta(portfolio_id)
        name = getattr(meta, "name", None)
        return name if name else f"Portfolio {portfolio_id}"

    def _record_failure(
        self,
        portfolio_id: int,
        run_id: str,
        title: str,
        *,
        body: str = "",
    ) -> None:
        """Record one failed-dispatch notification, never raising."""
        try:
            self._notifications.record(
                NotificationCategory.ALERT,
                "recommendation_email_failed",
                title,
                severity=NotificationSeverity.WARNING,
                body=body,
                portfolio_id=portfolio_id,
                run_id=run_id,
            )
        except Exception:
            logger.exception("Could not record recommendation email failure")

    def _record_summary(
        self, run_id: str, sent: int, failed: int, *, notify: bool = True
    ) -> None:
        """Record one per-run dispatch summary event, never raising.

        Nothing is recorded when there was nothing to do (no assignments,
        unreadable artifact) — a daily "0 sent, 0 failed" event would just
        be notification noise.
        """
        if not notify or (sent == 0 and failed == 0):
            return
        severity = (
            NotificationSeverity.INFO if failed == 0 else NotificationSeverity.WARNING
        )
        try:
            self._notifications.record(
                NotificationCategory.ALERT,
                "recommendation_emails_dispatched",
                f"Strategy recommendation emails — {sent} sent, {failed} failed",
                severity=severity,
                run_id=run_id,
            )
        except Exception:
            logger.exception("Could not record recommendation dispatch summary")
