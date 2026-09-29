"""Position thesis service (GH-14) — the seam the routes and orchestrator use.

Save, draft and confirm write thesis rows only; evaluations are appended
against the published analysis artifact. Nothing here ever creates, edits or
submits a trade, and the Strategy recommendation service is never called.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.agents.thesis.drafter import ThesisDraftClient, draft_thesis
from app.agents.thesis.evaluator import evaluate_thesis
from app.core.config import ANALYSIS_JSON
from app.repositories.position_theses_repo import (
    PositionThesesRepository,
    VersionConflictError,
)
from app.schemas.analysis_artifact import (
    AnalysisArtifactMeta,
    read_analysis_artifact_meta,
    read_analysis_snapshot,
)
from app.schemas.position_thesis import (
    PositionThesisV1,
    ThesisContentV1,
    ThesisEvaluationV1,
    ThesisSummary,
)
from app.schemas.record import StockRecord
from app.schemas.source_health import SourceHealth, SourceName
from app.schemas.trade import Position
from app.services.freshness_service import calculate_freshness
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)


class NotHeldError(LookupError):
    """The security is not an open holding of that portfolio."""


class NoAnalysisError(LookupError):
    """The published analysis has no record for the holding to draft from."""


class DraftSupersededError(RuntimeError):
    """A newer version was saved during the model call; the draft was discarded."""


@dataclass(frozen=True)
class EvaluationRun:
    """What one scan-step evaluation pass did across every portfolio."""

    count: int
    failed: tuple[int, ...] = ()


@dataclass(frozen=True)
class ThesisEditorView:
    """Everything the thesis editor modal renders for one holding."""

    portfolio_id: int
    position: Position
    summary: ThesisSummary
    history: tuple[ThesisEvaluationV1, ...]


def valid_records(rows: list[dict[str, Any]]) -> list[StockRecord]:
    """Validate artifact rows, skipping malformed ones as ``load_analysis`` does."""
    records: list[StockRecord] = []
    for row in rows:
        try:
            records.append(StockRecord.model_validate(row))
        except Exception:
            continue
    return records


class PositionThesisService:
    """Versioned theses per held security, and their deterministic checks.

    ``analysis_path`` defaults to the published ``ANALYSIS_JSON``.
    """

    def __init__(
        self,
        repo: PositionThesesRepository,
        trader: TraderService,
        analysis_path: Path | None = None,
    ) -> None:
        self._repo = repo
        self._trader = trader
        self._analysis_path = analysis_path or ANALYSIS_JSON

    # --- reads -----------------------------------------------------------

    def held_position(self, portfolio_id: int, security_id: str) -> Position:
        """Return the open position, or raise :class:`NotHeldError`."""
        position = self._held(portfolio_id).get(security_id)
        if position is None:
            raise NotHeldError(f"{security_id} is not held in portfolio {portfolio_id}")
        return position

    def _held(self, portfolio_id: int) -> dict[str, Position]:
        """Return the portfolio's open positions keyed by canonical ticker."""
        return {
            position.ticker: position
            for position in self._trader.get_portfolio(portfolio_id=portfolio_id)
            if position.shares > 0
        }

    def statuses(self, portfolio_id: int) -> dict[str, ThesisSummary]:
        """Return each security's active/pending thesis and latest evaluation.

        ``current`` says whether that evaluation is of the published run; an
        unreadable artifact counts as not current.
        """
        active = self._repo.active_for_portfolio(portfolio_id)
        pending = self._repo.pending_drafts(portfolio_id)
        latest = self._repo.latest_evaluations([t.id for t in active.values()])
        meta = read_analysis_artifact_meta(Path(self._analysis_path))
        run_id = meta.run_id if meta else None
        today = date.today()  # the user's review date is a local date
        summaries: dict[str, ThesisSummary] = {}
        for security_id in sorted({*active, *pending}):
            thesis = active.get(security_id)
            review = thesis.review_date if thesis else None
            evaluation = latest.get(thesis.id) if thesis else None
            summaries[security_id] = ThesisSummary(
                active=thesis,
                pending=pending.get(security_id),
                latest=evaluation,
                review_due=review is not None and review < today,
                current=evaluation is not None and evaluation.analysis_run_id == run_id,
            )
        return summaries

    def editor(self, portfolio_id: int, security_id: str) -> ThesisEditorView:
        """Gather the editor for one open holding (read-only)."""
        position = self.held_position(portfolio_id, security_id)
        return ThesisEditorView(
            portfolio_id=portfolio_id,
            position=position,
            summary=self.statuses(portfolio_id).get(security_id, ThesisSummary()),
            history=tuple(self._repo.history(portfolio_id, security_id)),
        )

    def published(self) -> tuple[list[StockRecord], AnalysisArtifactMeta | None]:
        """Return the published records and run identity from one read."""
        rows, meta = read_analysis_snapshot(Path(self._analysis_path))
        return valid_records(rows), meta

    # --- writes ----------------------------------------------------------

    def save(
        self, portfolio_id: int, security_id: str, content: ThesisContentV1
    ) -> PositionThesisV1:
        """Store the user's wording as the new active version; evaluate it now."""
        self.held_position(portfolio_id, security_id)
        thesis = self._repo.add_version(
            portfolio_id, security_id, content, "user", active=True
        )
        self._evaluate_now(thesis)
        return thesis

    def draft(
        self,
        portfolio_id: int,
        security_id: str,
        client: ThesisDraftClient,
        source_health: Mapping[SourceName, SourceHealth],
    ) -> PositionThesisV1 | None:
        """Store an inactive AI draft, or return None (nothing written).

        Raises :class:`NoAnalysisError` (no model call) when the published
        analysis has no record for the holding. A draft that returns after
        a newer version was saved is discarded, not stored over it, and
        raises :class:`DraftSupersededError` (nothing written).
        """
        position = self.held_position(portfolio_id, security_id)
        records, meta = self.published()
        record = next((r for r in records if r.ticker == security_id), None)
        if record is None:
            raise NoAnalysisError(f"no published analysis for {security_id}")
        version = self._repo.latest_version(portfolio_id, security_id)
        draft = draft_thesis(
            record,
            client=client,
            meta=meta,
            freshness=calculate_freshness(meta.generated_at if meta else None),
            source_health=source_health,
            display_symbol=position.display_symbol,
        )
        if draft is None:
            return None
        content = ThesisContentV1.model_validate(draft.model_dump())
        try:
            return self._repo.add_version(
                portfolio_id,
                security_id,
                content,
                "ai_draft",
                active=False,
                expected_latest_version=version,
            )
        except VersionConflictError as exc:
            logger.info("Discarding thesis draft superseded during the model call")
            raise DraftSupersededError(
                f"{security_id} was saved during the draft"
            ) from exc

    def confirm(
        self, portfolio_id: int, security_id: str, thesis_id: int
    ) -> PositionThesisV1:
        """Activate the holding's pending draft; evaluate it now.

        Raises ``StaleDraftError`` (from the repository) unless ``thesis_id``
        is still the holding's pending draft.
        """
        self.held_position(portfolio_id, security_id)
        thesis = self._repo.activate_pending_draft(portfolio_id, security_id, thesis_id)
        self._evaluate_now(thesis)
        return thesis

    def evaluate_portfolio(
        self,
        portfolio_id: int,
        records: Sequence[StockRecord],
        meta: AnalysisArtifactMeta,
    ) -> int:
        """Append one evaluation per held active thesis for this run.

        Returns the new rows. A thesis whose security is not currently held
        is skipped and left active, so a wrong holdings read loses nothing
        and a re-buy resumes it. A failing holdings read raises before
        anything is written.
        """
        active = self._repo.active_for_portfolio(portfolio_id)
        held = self._held(portfolio_id)
        by_ticker = {record.ticker: record for record in records}
        freshness = calculate_freshness(meta.generated_at)
        return sum(
            self._repo.append_evaluation(
                evaluate_thesis(thesis, by_ticker.get(security_id), meta, freshness)
            )
            for security_id, thesis in active.items()
            if security_id in held
        )

    def evaluate_all(
        self, records: Sequence[StockRecord], meta: AnalysisArtifactMeta
    ) -> EvaluationRun:
        """Evaluate every portfolio's active theses once for this run.

        Each portfolio is isolated: one failing is logged, skipped and named
        in the result's ``failed`` ids.
        """
        count = 0
        failed: list[int] = []
        for portfolio in self._trader.list_portfolios():
            try:
                count += self.evaluate_portfolio(portfolio.id, records, meta)
            except Exception:
                failed.append(portfolio.id)
                logger.warning(
                    "Thesis evaluation failed for portfolio %s",
                    portfolio.id,
                    exc_info=True,
                )
        return EvaluationRun(count=count, failed=tuple(failed))

    def _evaluate_now(self, thesis: PositionThesisV1) -> None:
        """Evaluate a newly active version against the published artifact.

        Best-effort: the version is already committed, so a failure here is
        logged and the scan step evaluates it later.
        """
        try:
            records, meta = self.published()
            if meta is None:
                return
            record = next((r for r in records if r.ticker == thesis.security_id), None)
            freshness = calculate_freshness(meta.generated_at)
            self._repo.append_evaluation(
                evaluate_thesis(thesis, record, meta, freshness)
            )
        except Exception:
            logger.warning(
                "Immediate evaluation of thesis %s failed", thesis.id, exc_info=True
            )
