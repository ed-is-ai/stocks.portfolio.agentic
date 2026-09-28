"""Position thesis routes (GH-14): the per-holding thesis editor.

GET renders the editor modal (read-only). The only writes are save (a new
active version in the user's wording), draft (an inactive AI-drafted
version) and confirm (activating the pending draft) — each touches thesis
rows only, never trades, cash flows or portfolios. A successful write sends
``HX-Trigger: portfolio-agents-refresh`` so the Portfolio tab's agent layer
reloads.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.agents.thesis.drafter import ThesisDraftClient
from app.api.dependencies import get_position_thesis_service, get_thesis_draft_client
from app.api.stock_scanner_context import load_source_health
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.schemas.position_thesis import (
    MAX_RULES,
    RULE_ADAPTER,
    RULE_KINDS,
    SMA_PERIODS,
    STATUS_LABELS,
    ThesisContentV1,
    ThesisRuleV1,
    describe_rule,
)
from app.services.portfolio_agent_view import thesis_cell
from app.repositories.position_theses_repo import StaleDraftError
from app.services.position_thesis_service import NotHeldError, PositionThesisService

router = APIRouter(dependencies=[Depends(require_local_or_token)])

ThesisDep = Annotated[PositionThesisService, Depends(get_position_thesis_service)]
DraftClientDep = Annotated[ThesisDraftClient, Depends(get_thesis_draft_client)]
FormList = Annotated[list[str], Form()]

REFRESH = {"HX-Trigger": "portfolio-agents-refresh"}
DRAFT_UNAVAILABLE = "AI draft unavailable"
NOT_HELD = "This security is no longer held in this portfolio."
_OUTCOME_TONES = {"fired": "risk", "clear": "good", "limited": "warn"}
_PATH = "/portfolios/{portfolio_id}/theses/{security_id}"


class ThesisFormError(ValueError):
    """The submitted thesis form is invalid; the message is user-facing."""


def _render(
    request: Request,
    theses: PositionThesisService,
    portfolio_id: int,
    security_id: str,
    *,
    body_only: bool,
    form: Any = None,
    message: str = "",
    message_tone: str = "success",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render the editor (whole modal, or just its body after a POST).

    A security that is not held renders the not-held notice (404).
    """
    try:
        view = theses.editor(portfolio_id, security_id)
    except NotHeldError:
        return _not_held(request, body_only=body_only)
    context = {
        "view": view,
        "body_only": body_only,
        "cell": thesis_cell(view.summary),
        "form": form if form is not None else view.summary.active,
        "message": message,
        "message_tone": message_tone,
        "describe_rule": describe_rule,
        "status_labels": STATUS_LABELS,
        "outcome_tones": _OUTCOME_TONES,
        "rule_kinds": RULE_KINDS,
        "sma_periods": SMA_PERIODS,
        "max_rules": MAX_RULES,
    }
    return templates.TemplateResponse(
        request,
        "_thesis_editor.html",
        context,
        status_code=status_code,
        headers=headers,
    )


def _not_held(request: Request, *, body_only: bool) -> HTMLResponse:
    """Render the 404 warning the editor's before-swap handler shows."""
    return templates.TemplateResponse(
        request,
        "_thesis_notice.html",
        {"body_only": body_only, "message": NOT_HELD},
        status_code=404,
    )


@router.get(_PATH, response_class=HTMLResponse)
def thesis_editor(
    request: Request, theses: ThesisDep, portfolio_id: int, security_id: str
) -> HTMLResponse:
    """Render the thesis editor modal for one open holding (read-only)."""
    return _render(request, theses, portfolio_id, security_id, body_only=False)


@router.post(_PATH, response_class=HTMLResponse)
def save_thesis(
    request: Request,
    theses: ThesisDep,
    portfolio_id: int,
    security_id: str,
    rationale: Annotated[str, Form()] = "",
    expected_setup: Annotated[str, Form()] = "",
    review_date: Annotated[str, Form()] = "",
    rule_kind: FormList = [],
    rule_period: FormList = [],
    rule_min_rel_volume: FormList = [],
    rule_min_score: FormList = [],
) -> HTMLResponse:
    """Save the user's wording as the holding's new active version.

    An invalid form re-renders the editor with the error (200) and writes
    nothing.
    """
    rules = _form_rules(rule_kind, rule_period, rule_min_rel_volume, rule_min_score)
    form = {
        "rationale": rationale,
        "expected_setup": expected_setup,
        "review_date": review_date,
        "rules": rules,
    }
    try:
        content = _content(rationale, expected_setup, review_date, rules)
        saved = theses.save(portfolio_id, security_id, content)
    except NotHeldError:
        return _not_held(request, body_only=True)
    except ThesisFormError as exc:
        return _render(
            request,
            theses,
            portfolio_id,
            security_id,
            body_only=True,
            form=form,
            message=str(exc),
            message_tone="warning",
        )
    return _render(
        request,
        theses,
        portfolio_id,
        security_id,
        body_only=True,
        message=f"Thesis saved as version {saved.version}.",
        headers=REFRESH,
    )


@router.post(f"{_PATH}/draft", response_class=HTMLResponse)
def draft_thesis(
    request: Request,
    theses: ThesisDep,
    client: DraftClientDep,
    portfolio_id: int,
    security_id: str,
) -> HTMLResponse:
    """Ask Claude for an inactive draft from anonymised evidence.

    A plain ``def``, so the model call runs in the threadpool. When no draft
    is available nothing is written and the editor says so.
    """
    try:
        draft = theses.draft(portfolio_id, security_id, client, load_source_health())
    except NotHeldError:
        return _not_held(request, body_only=True)
    if draft is None:
        return _render(
            request,
            theses,
            portfolio_id,
            security_id,
            body_only=True,
            message=DRAFT_UNAVAILABLE,
            message_tone="warning",
        )
    return _render(
        request,
        theses,
        portfolio_id,
        security_id,
        body_only=True,
        message="AI draft ready. Review it, then confirm it to start monitoring.",
        headers=REFRESH,
    )


@router.post(f"{_PATH}/confirm", response_class=HTMLResponse)
def confirm_thesis(
    request: Request,
    theses: ThesisDep,
    portfolio_id: int,
    security_id: str,
    thesis_id: Annotated[int, Form()],
) -> HTMLResponse:
    """Make the pending draft the holding's only active version.

    A version id that is no longer the pending draft is a 404.
    """
    try:
        theses.confirm(portfolio_id, security_id, thesis_id)
    except NotHeldError:
        return _not_held(request, body_only=True)
    except StaleDraftError:
        return _render(
            request,
            theses,
            portfolio_id,
            security_id,
            body_only=True,
            message="That draft is no longer pending.",
            message_tone="warning",
            status_code=404,
        )
    return _render(
        request,
        theses,
        portfolio_id,
        security_id,
        body_only=True,
        message="Draft confirmed. It is now the active thesis.",
        headers=REFRESH,
    )


def _form_rules(
    kinds: list[str], periods: list[str], volumes: list[str], scores: list[str]
) -> list[dict[str, str]]:
    """Return the filled rule slots as raw strings (blank kind = unused)."""

    def at(values: list[str], index: int) -> str:
        return values[index].strip() if index < len(values) else ""

    return [
        {
            "kind": kind.strip(),
            "period": at(periods, index),
            "min_rel_volume": at(volumes, index),
            "min_score": at(scores, index),
        }
        for index, kind in enumerate(kinds)
        if kind.strip()
    ]


def _content(
    rationale: str, expected_setup: str, review_date: str, rules: list[dict[str, str]]
) -> ThesisContentV1:
    """Validate the form into thesis content, or raise :class:`ThesisFormError`."""
    if not rules:
        raise ThesisFormError(f"Add at least one rule (up to {MAX_RULES}).")
    typed = tuple(_rule(number, raw) for number, raw in enumerate(rules, start=1))
    try:
        review = date.fromisoformat(review_date) if review_date.strip() else None
    except ValueError as exc:
        raise ThesisFormError("The review date is not a valid date.") from exc
    try:
        return ThesisContentV1(
            rationale=rationale,
            expected_setup=expected_setup,
            rules=typed,
            review_date=review,
        )
    except ValidationError as exc:
        raise ThesisFormError(_first_error(exc)) from exc


def _rule(number: int, raw: dict[str, str]) -> ThesisRuleV1:
    """Validate one rule slot into ``ThesisRuleV1``."""
    payload: dict[str, Any] = {"kind": raw["kind"]}
    try:
        if raw["kind"] == "close_below_sma":
            payload["period"] = int(raw["period"]) if raw["period"] else None
            if raw["min_rel_volume"]:
                payload["min_rel_volume"] = float(raw["min_rel_volume"])
        elif raw["kind"] == "score_below":
            payload["min_score"] = int(raw["min_score"]) if raw["min_score"] else None
        return RULE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ThesisFormError(f"Rule {number}: {_first_error(exc)}") from exc
    except ValueError as exc:
        raise ThesisFormError(f"Rule {number}: parameters must be numbers.") from exc


def _first_error(exc: ValidationError) -> str:
    """Return the first validation error as one readable line."""
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"] if isinstance(part, str))
    return f"{where}: {error['msg']}" if where else str(error["msg"])
