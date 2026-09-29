"""AI Desk routes (GH-21): its own screen, its partials and one inspector.

Every route is a read-only GET: none writes, enqueues a job or calls an
LLM. ``/desk`` is the normal shell with the AI Desk tab active, so a reload
or a shared link lands on the Desk. The partials are plain ``def``s: the
queue reads the ledger, scan artifact and price cache in the threadpool,
exactly as the Portfolio tab's agent layer does.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import get_desk_service
from app.api.params import optional_int
from app.api.routes.views import render_shell
from app.api.templating import templates
from app.services.desk_service import (
    AGENT_BOUNDARY,
    CONTEXT_NOTE,
    MISSING_ITEM,
    ORDERING_NOTE,
    DeskService,
    DeskView,
)

router = APIRouter()

DeskDep = Annotated[DeskService, Depends(get_desk_service)]
ITEM_NOT_FOUND = "That item is no longer in the queue."


@router.get("/desk", response_class=HTMLResponse)
async def desk(request: Request) -> HTMLResponse:
    """Render the shell with the AI Desk tab active; its content loads into
    ``#tab-content`` from the URL's ``portfolio_id`` and ``item``."""
    return render_shell(request, "desk")


@router.get("/partials/desk", response_class=HTMLResponse)
def partial_desk(
    request: Request,
    desk: DeskDep,
    portfolio_id: str | None = None,
    item: str | None = None,
) -> HTMLResponse:
    """Render the whole Desk for a portfolio with ``item`` selected."""
    view = desk.view(optional_int(portfolio_id), item or None)
    return templates.TemplateResponse(request, "_desk.html", _context(view))


@router.get("/partials/desk/queue", response_class=HTMLResponse)
def partial_desk_queue(
    request: Request,
    desk: DeskDep,
    portfolio_id: str | None = None,
    item: str | None = None,
) -> HTMLResponse:
    """Render the queue pane alone, e.g. after a thesis edit elsewhere."""
    view = desk.view(optional_int(portfolio_id), item or None)
    return templates.TemplateResponse(request, "_desk_queue.html", _context(view))


@router.get("/partials/desk/items/{item_id:path}", response_class=HTMLResponse)
def partial_desk_item(
    request: Request, desk: DeskDep, item_id: str, portfolio_id: str | None = None
) -> HTMLResponse:
    """Render one item's inspector, the same markup the queue selects."""
    view = desk.view(optional_int(portfolio_id), item_id)
    if view.missing_item or view.selected is None:
        return HTMLResponse(ITEM_NOT_FOUND, status_code=404)
    return templates.TemplateResponse(
        request, "_desk_inspector.html", {"d": view.selected}
    )


def _context(view: DeskView) -> dict[str, Any]:
    return {
        "view": view,
        "ordering_note": ORDERING_NOTE,
        "agent_boundary": AGENT_BOUNDARY,
        "context_note": CONTEXT_NOTE,
        "missing_item": MISSING_ITEM,
    }
