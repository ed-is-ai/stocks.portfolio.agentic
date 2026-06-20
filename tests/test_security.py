"""Unit tests for the require_local_or_token money-endpoint guard."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from app.core.security import require_local_or_token


def _request(
    host: str | None,
    token_header: str | None = None,
    fetch_site: str | None = None,
) -> Request:
    """Build a minimal stand-in for fastapi.Request.

    The guard reads request.headers.get("X-Auth-Token"),
    request.headers.get("Sec-Fetch-Site"), and request.client (.host, or None).
    A SimpleNamespace satisfies all three.
    """
    headers: dict[str, str] = {}
    if token_header is not None:
        headers["X-Auth-Token"] = token_header
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    client = SimpleNamespace(host=host) if host is not None else None
    return cast(Request, SimpleNamespace(headers=headers, client=client))


# ---------------------------------------------------------------------------
# Step 2: No-token (default local) workflow
# ---------------------------------------------------------------------------


def test_loopback_ipv4_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """127.0.0.1 is allowed when no token is configured."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert require_local_or_token(_request("127.0.0.1")) is None


def test_loopback_ipv6_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """::1 is allowed when no token is configured."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert require_local_or_token(_request("::1")) is None


def test_localhost_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """'localhost' hostname is allowed when no token is configured."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert require_local_or_token(_request("localhost")) is None


def test_non_loopback_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-loopback host without a token is rejected with 403."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request("10.0.0.5"))
    assert exc_info.value.status_code == 403


def test_client_none_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """request.client is None (host cannot be determined) is rejected with 403."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request(None))
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Step 3: Token-configured workflow
# ---------------------------------------------------------------------------


def test_matching_token_non_loopback_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct X-Auth-Token from a non-loopback host is allowed."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    assert require_local_or_token(_request("10.0.0.5", token_header="s3cret")) is None


def test_wrong_token_non_loopback_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong X-Auth-Token from a non-loopback host is rejected with 403."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request("10.0.0.5", token_header="nope"))
    assert exc_info.value.status_code == 403


def test_missing_token_header_non_loopback_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No X-Auth-Token header from a non-loopback host is rejected with 403."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request("10.0.0.5"))
    assert exc_info.value.status_code == 403


def test_loopback_still_allowed_with_token_configured_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback host is still allowed even when APP_AUTH_TOKEN is set but no header sent."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    assert require_local_or_token(_request("127.0.0.1")) is None


# ---------------------------------------------------------------------------
# Cross-site (CSRF) fetch-metadata guard
# ---------------------------------------------------------------------------


def test_cross_site_fetch_rejected_from_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-site browser request is rejected even from loopback (CSRF)."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request("127.0.0.1", fetch_site="cross-site"))
    assert exc_info.value.status_code == 403


def test_same_site_fetch_rejected_from_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-site (different-origin) browser request is also rejected."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        require_local_or_token(_request("127.0.0.1", fetch_site="same-site"))
    assert exc_info.value.status_code == 403


def test_same_origin_fetch_allowed_from_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app's own (same-origin) htmx requests are allowed."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert (
        require_local_or_token(_request("127.0.0.1", fetch_site="same-origin")) is None
    )


def test_no_fetch_header_allowed_from_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-browser clients (curl, scheduler) send no Sec-Fetch-Site and pass."""
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert require_local_or_token(_request("127.0.0.1")) is None


def test_token_bypasses_cross_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid shared secret is trusted and bypasses the CSRF check."""
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    assert (
        require_local_or_token(
            _request("10.0.0.5", token_header="s3cret", fetch_site="cross-site")
        )
        is None
    )
