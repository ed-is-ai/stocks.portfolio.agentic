"""Shared request-parameter helpers for the API routes."""


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
