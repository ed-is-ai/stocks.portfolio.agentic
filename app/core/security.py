"""Security helpers for API routes.

``require_local_or_token`` guards money-mutating endpoints. It lives here so the
API layer depends on ``core`` for the policy rather than defining it inline.
"""

from fastapi import HTTPException, Request

from app.core import config


def require_local_or_token(request: Request) -> None:
    """Allow loopback clients, or any client with a valid shared secret.

    Money-mutating endpoints use this. When ``APP_AUTH_TOKEN`` is unset (the
    default local workflow), only loopback clients (127.0.0.1 / ::1) are
    allowed. When it is set, a matching ``X-Auth-Token`` header is also
    accepted from any host. Anything else gets HTTP 403.
    """
    token = config.APP_AUTH_TOKEN()
    if token and request.headers.get("X-Auth-Token") == token:
        return
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "::1", "localhost"}:
        return
    raise HTTPException(status_code=403, detail="Forbidden")
