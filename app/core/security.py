"""Security helpers for API routes.

``require_local_or_token`` guards money-mutating endpoints. It lives here so the
API layer depends on ``core`` for the policy rather than defining it inline.
"""

from fastapi import HTTPException, Request

from app.core import config

# Fetch-metadata sites the browser reports as safe for a state change:
# the app's own pages ("same-origin") and direct user navigation ("none").
# "cross-site"/"same-site" mean another origin initiated the request — a CSRF
# attempt — so reject it even from loopback. Non-browser clients (curl, the
# scheduler) omit the header entirely and are unaffected.
_CSRF_SAFE_FETCH_SITES = {"same-origin", "none"}


def require_local_or_token(request: Request) -> None:
    """Allow loopback clients, or any client with a valid shared secret.

    Money-mutating endpoints use this. When ``APP_AUTH_TOKEN`` is unset (the
    default local workflow), only loopback clients (127.0.0.1 / ::1) are
    allowed. When it is set, a matching ``X-Auth-Token`` header is also
    accepted from any host. Browser requests carrying a cross-site
    ``Sec-Fetch-Site`` header are rejected (CSRF). Anything else gets HTTP 403.
    """
    token = config.APP_AUTH_TOKEN()
    if token and request.headers.get("X-Auth-Token") == token:
        return
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in _CSRF_SAFE_FETCH_SITES:
        raise HTTPException(status_code=403, detail="Forbidden")
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise HTTPException(status_code=403, detail="Forbidden")
