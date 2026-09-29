"""Trade process review routes (GH-17): History tab's Process column.

GETs are read-only: the lazy out-of-band Process cells, one trade's review
modal and the weekly summary strip. The two POSTs are guarded by
``require_local_or_token``: adding an annotation (the only write, to
``trade_annotations`` only) and asking Claude to interpret a week (writes
nothing). A successful annotation sends ``HX-Trigger: trade-process-refresh``
so the Process column and weekly strip reload.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.agents.trade_review.checklist import RULES
from app.agents.trade_review.weekly import TradeReviewClient
from app.api.dependencies import get_trade_review_client, get_trade_review_service
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.schemas.trade_review import MAX_INTENT_LENGTH, TradeReviewV1
from app.services.trade_review_service import (
    AnnotationError,
    EmptyWeekError,
    TradeReviewService,
    UnknownTradeError,
    WeeklyView,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ReviewDep = Annotated[TradeReviewService, Depends(get_trade_review_service)]
ClientDep = Annotated[TradeReviewClient, Depends(get_trade_review_client)]
Guard = [Depends(require_local_or_token)]

REFRESH = {"HX-Trigger": "trade-process-refresh"}
INTERPRETATION_UNAVAILABLE = "Interpretation unavailable"
REVIEW_UNAVAILABLE = "Review unavailable"
EMPTY_WEEK = "That week has no trades."
STATUS_TONES = {
    "followed": "good",
    "deviated": "warn",
    "unknown": "muted",
    "n_a": "muted",
}
STATUS_LABELS = {
    "followed": "Followed",
    "deviated": "Deviated",
    "unknown": "Unknown",
    "n_a": "N/A",
}
CHECK_LABELS = {
    "valid_setup": "Valid setup",
    "entry_location": "Entry location",
    "evidenced_stop": "Evidenced stop",
    "exit_signal": "Exit signal",
    "strategy_alignment": "Strategy alignment",
    "data_completeness": "Data completeness",
}


@dataclass(frozen=True)
class ProcessCell:
    """The Process column's one-line summary of a review."""

    text: str
    tone: str


def process_cell(review: TradeReviewV1) -> ProcessCell:
    """Summarise a review: deviations first, then gaps, else followed.

    Like ``data_completeness``, a Strategy that was assigned but not
    replayed is not a gap in the evidence.
    """
    deviated = sum(c.status == "deviated" for c in review.checks)
    if deviated:
        return ProcessCell(f"{deviated} deviated", "warn")
    if any(
        c.status == "unknown" and c.kind != "strategy_alignment" for c in review.checks
    ):
        return ProcessCell("Evidence limited", "muted")
    return ProcessCell("Followed", "good")


_CONTEXT = {
    "status_tones": STATUS_TONES,
    "status_labels": STATUS_LABELS,
    "check_labels": CHECK_LABELS,
    "max_intent": MAX_INTENT_LENGTH,
}


@router.get("/partials/history/process", response_class=HTMLResponse)
def partial_history_process(request: Request, reviews: ReviewDep) -> HTMLResponse:
    """Render every reviewed trade's Process cell as an out-of-band swap."""
    cells = {tid: process_cell(r) for tid, r in reviews.reviews().items()}
    return templates.TemplateResponse(request, "_trade_process.html", {"cells": cells})


@router.get("/partials/history/process/{trade_id}", response_class=HTMLResponse)
def trade_review_modal(
    request: Request, reviews: ReviewDep, trade_id: int
) -> HTMLResponse:
    """Render one trade's review details and annotation form (read-only)."""
    return _modal(request, reviews, trade_id, body_only=False)


@router.post(
    "/trades/{trade_id}/annotations", response_class=HTMLResponse, dependencies=Guard
)
def annotate_trade(
    request: Request,
    reviews: ReviewDep,
    trade_id: int,
    intent: Annotated[str, Form()] = "",
    stated_stop: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Append an annotation; an invalid form re-renders with a warning."""
    form = {"intent": intent, "stated_stop": stated_stop}
    try:
        reviews.annotate(trade_id, intent, _stop(stated_stop))
    except UnknownTradeError:
        return _modal(request, reviews, trade_id, body_only=True, status_code=404)
    except AnnotationError as exc:
        return _modal(
            request, reviews, trade_id, body_only=True, form=form, warning=str(exc)
        )
    return _modal(
        request,
        reviews,
        trade_id,
        body_only=True,
        message="Annotation saved.",
        headers=REFRESH,
    )


@router.get("/partials/history/weekly", response_class=HTMLResponse)
def partial_history_weekly(
    request: Request, reviews: ReviewDep, week: str | None = None
) -> HTMLResponse:
    """Render the weekly summary strip for ``week`` (default: latest)."""
    try:
        view, unavailable = reviews.weekly(week), ""
    except Exception:
        logger.warning("Weekly trade review failed", exc_info=True)
        view, unavailable = WeeklyView(), REVIEW_UNAVAILABLE
    return templates.TemplateResponse(
        request,
        "_trade_weekly.html",
        {**_CONTEXT, "view": view, "rules": RULES, "unavailable": unavailable},
    )


@router.post(
    "/partials/history/weekly/interpretation",
    response_class=HTMLResponse,
    dependencies=Guard,
)
def weekly_interpretation(
    request: Request, reviews: ReviewDep, client: ClientDep, week: str | None = None
) -> HTMLResponse:
    """Ask Claude to interpret the week's anonymised facts; nothing is stored.

    A week with no trades says so rather than interpreting another week.
    """
    try:
        interpretation, unavailable = (
            reviews.interpret(week, client),
            INTERPRETATION_UNAVAILABLE,
        )
    except EmptyWeekError:
        interpretation, unavailable = None, EMPTY_WEEK
    return templates.TemplateResponse(
        request,
        "_trade_weekly.html",
        {
            "interpretation_only": True,
            "interpretation": interpretation,
            "unavailable": unavailable,
        },
    )


def _stop(raw: str) -> float | None:
    """Parse the optional stated stop; a non-number is an annotation error."""
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise AnnotationError("A stated stop must be a number.")
    return value


def _modal(
    request: Request,
    reviews: ReviewDep,
    trade_id: int,
    *,
    body_only: bool,
    form: dict[str, str] | None = None,
    message: str = "",
    warning: str = "",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    try:
        review = reviews.review(trade_id)
        annotations = reviews.annotations_for(trade_id)
    except Exception:
        logger.warning("Trade review modal failed for %s", trade_id, exc_info=True)
        review, annotations = None, []
        warning = warning or REVIEW_UNAVAILABLE
    context = {
        **_CONTEXT,
        "trade_id": trade_id,
        "review": review,
        "stated_after": review is not None
        and any(
            ref.kind == "annotation_after_trade"
            for check in review.checks
            for ref in check.evidence
        ),
        "annotations": annotations,
        "body_only": body_only,
        "form": form or {},
        "message": message,
        "warning": warning,
    }
    return templates.TemplateResponse(
        request,
        "_trade_review_modal.html",
        context,
        status_code=status_code,
        headers=headers,
    )
