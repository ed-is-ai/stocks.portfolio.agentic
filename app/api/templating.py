"""Shared Jinja2 templates instance for the API routes."""

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core import config
from app.services.backtest.activity_presenter import absolute_time, relative_time

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
templates.env.filters["relative_time"] = relative_time
templates.env.filters["absolute_time"] = absolute_time


def is_htmx_request(request: Request) -> bool:
    """Return True when the request is an htmx swap (``HX-Request: true``)."""
    return request.headers.get("HX-Request", "").lower() == "true"


def template_response(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    """Render a fragment template, wrapping it in the full page shell for
    non-htmx requests (issue #445).

    htmx swaps (``HX-Request: true``) receive the bare fragment exactly as
    before; direct browser navigation receives the fragment rendered inside
    ``page.html`` so the page is styled and in-fragment htmx navigation
    (``hx-target="#tab-content"``) keeps working. Status codes and headers
    are preserved for both request styles, and ``Vary: HX-Request`` is set
    so caches never serve one style's body to the other.

    The wrapped path injects the fragment into ``page.html`` via
    ``| safe``: fragments are trusted, app-owned templates, never
    user-supplied HTML.
    """
    vary_headers: dict[str, str] = {**(headers or {}), "Vary": "HX-Request"}
    if is_htmx_request(request):
        return templates.TemplateResponse(
            request,
            name,
            context or {},
            status_code=status_code,
            headers=vary_headers,
        )
    fragment_html = templates.get_template(name).render(
        {**(context or {}), "request": request}
    )
    return templates.TemplateResponse(
        request,
        "page.html",
        {"page_content": fragment_html},
        status_code=status_code,
        headers=vary_headers,
    )
