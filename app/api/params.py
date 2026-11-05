"""Shared request-parameter helpers for the API routes."""

from fastapi import Request
from fastapi.datastructures import FormData

CHART_RANGES = ("1M", "3M", "12M", "3Y", "5Y")
DEFAULT_CHART_RANGE = "12M"


def chart_range(value: str | None) -> str:
    """Normalise the portfolio-chart range param to a known preset (#421).

    The range selector sends ``range=1M|3M|12M|3Y|5Y``. Anything else —
    absent, blank, or an unknown token — resolves to ``12M`` so a stale
    bookmark or a typo never 422s the Portfolio tab.
    """
    if value is None:
        return DEFAULT_CHART_RANGE
    token = value.strip().upper()
    return token if token in CHART_RANGES else DEFAULT_CHART_RANGE


def optional_int(value: str | None) -> int | None:
    """Coerce an optional string parameter to ``int`` or ``None``.

    The web UI threads the selected ``portfolio_id`` through query params and
    form fields, but when nothing is selected the client sends an **empty
    string** (``?portfolio_id=``). Typing the route param as ``int | None``
    makes FastAPI reject that with 422, breaking the Portfolio tab (#147
    regression). Accept the raw string and normalise here: blank/whitespace →
    ``None``; a non-numeric value → ``None`` (callers validate existence and
    reject unknown ids with an error banner anyway).
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def request_form(request: Request) -> FormData:
    """Read the form body for a sync handler (#132, AD-1).

    A plain ``def`` handler runs in the threadpool and cannot await the
    body, so the one true async read lives in this dependency. It is the
    only allowlisted coroutine in ``tests/test_event_loop_guards.py``.
    """
    return await request.form()
