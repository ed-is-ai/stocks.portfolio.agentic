"""Today routes (GH-128): the Today partial and its "Dismiss all".

The ``/today`` page itself is a shell page in ``views``. Both handlers are
plain ``def``s (AD-1): Today reads the ledger, scan artifact and price
cache, in the threadpool, through the Desk service's revision cache.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.agents.scanner.market_regime_snapshot import load_market_regime
from app.api.dependencies import get_desk_service
from app.api.params import optional_int
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.services.desk_service import DeskService

router = APIRouter()

DeskDep = Annotated[DeskService, Depends(get_desk_service)]
#: Response header that re-renders the Today badge after a dismiss.
BADGE_REFRESH = {"HX-Trigger": "attention-changed"}


@router.get("/partials/today", response_class=HTMLResponse)
def partial_today(
    request: Request,
    desk: DeskDep,
    portfolio_id: str | None = None,
    rev: str | None = None,
) -> Response:
    """Render Today for a portfolio.

    The 60s timer sends the ``rev`` it last rendered; an unchanged
    revision answers 204 so htmx keeps the page without rebuilding it (AD-2).
    """
    pid = optional_int(portfolio_id)
    revision = desk.revision(pid)
    if rev and rev == revision:
        return Response(status_code=204)
    return _render(request, desk, pid, revision)


@router.post(
    "/today/dismiss",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
def dismiss_today(
    request: Request, desk: DeskDep, portfolio_id: str | None = None
) -> HTMLResponse:
    """Dismiss every Worth checking item, then re-render Today.

    Bodyless: ``portfolio_id`` rides the query string. Each dismissal is
    persisted against the item's stable key and holds until its source
    events change.
    """
    pid = optional_int(portfolio_id)
    desk.dismiss_worth_checking(pid)
    response = _render(request, desk, pid, desk.revision(pid))
    response.headers.update(BADGE_REFRESH)
    return response


def _render(
    request: Request, desk: DeskService, pid: int | None, revision: str | None
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_today.html",
        {
            "view": desk.today_items(pid),
            "revision": revision,
            "market_regime": load_market_regime(),
            "today": date.today(),
        },
    )
