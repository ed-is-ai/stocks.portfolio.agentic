"""Route/auth tests for the Settings/Integrations UI (#47)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.dependencies import get_integration_config_service
from app.services.integration_config_service import (
    ALLOWED_KEYS,
    IntegrationConfigService,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_environ():
    """Isolate tests from both each other and this machine's real .env.

    ``app.services.pipeline_service`` calls ``load_dotenv()`` at import
    time, so a developer's real configured keys may already be sitting in
    ``os.environ`` — clear the allowlisted keys before each test, not just
    restore them after, so "not configured" assertions are meaningful.
    """
    saved = {key: os.environ.get(key) for key in ALLOWED_KEYS}
    for key in ALLOWED_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def config_service(tmp_path):
    service = IntegrationConfigService(tmp_path / ".env")
    app.dependency_overrides[get_integration_config_service] = lambda: service
    try:
        yield service
    finally:
        app.dependency_overrides.clear()


def test_get_settings_shows_missing_badges_and_no_secret_values(
    config_service,
) -> None:
    response = client.get("/partials/settings")

    assert response.status_code == 200
    assert "Missing" in response.text
    assert "sk-" not in response.text


def test_get_settings_never_echoes_a_configured_secret(config_service) -> None:
    config_service.update({"FMP_API_KEY": "sk-super-secret"})

    response = client.get("/partials/settings")

    assert "Configured" in response.text
    assert "sk-super-secret" not in response.text


def test_get_settings_displays_non_secret_email_host_and_port(config_service) -> None:
    config_service.update({"EMAIL_HOST": "smtp.example.com", "EMAIL_PORT": "587"})

    response = client.get("/partials/settings")

    assert "smtp.example.com" in response.text
    assert 'value="587"' in response.text


def test_save_settings_forbidden_without_local_or_token(config_service) -> None:
    os.environ.pop("APP_AUTH_TOKEN", None)
    response = client.post("/settings", data={"fmp_api_key": "sk-123"})
    assert response.status_code == 403
    assert config_service.status()["FMP_API_KEY"]["configured"] is False


def test_save_settings_allowed_with_matching_token(config_service) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        response = client.post(
            "/settings",
            data={"fmp_api_key": "sk-123"},
            headers={"X-Auth-Token": "s3cret"},
        )

    assert response.status_code == 200
    assert "sk-123" not in response.text
    assert config_service.status()["FMP_API_KEY"]["configured"] is True


def test_save_settings_persists_anthropic_api_key(config_service) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        response = client.post(
            "/settings",
            data={"anthropic_api_key": "sk-ant-123"},
            headers={"X-Auth-Token": "s3cret"},
        )
        assert response.status_code == 200
        assert "sk-ant-123" not in response.text
        assert config_service.status()["ANTHROPIC_API_KEY"]["configured"] is True


def test_save_settings_rejected_with_invalid_token(config_service) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        response = client.post(
            "/settings",
            data={"fmp_api_key": "sk-123"},
            headers={"X-Auth-Token": "wrong"},
        )
    assert response.status_code == 403


def test_clear_setting_requires_authorization(config_service) -> None:
    config_service.update({"FMP_API_KEY": "sk-123"})
    os.environ.pop("APP_AUTH_TOKEN", None)

    response = client.post("/settings/clear", data={"key": "FMP_API_KEY"})

    assert response.status_code == 403
    assert config_service.status()["FMP_API_KEY"]["configured"] is True


def test_clear_setting_removes_configured_value(config_service) -> None:
    config_service.update({"FMP_API_KEY": "sk-123"})
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        response = client.post(
            "/settings/clear",
            data={"key": "FMP_API_KEY"},
            headers={"X-Auth-Token": "s3cret"},
        )
        assert response.status_code == 200
        # Assert inside the context: patch.dict restores the *entire*
        # pre-context os.environ snapshot on exit, which would silently
        # resurrect this key from the developer's real, pre-loaded value.
        assert config_service.status()["FMP_API_KEY"]["configured"] is False


def test_rotate_token_forbidden_without_local_or_token(config_service) -> None:
    os.environ.pop("APP_AUTH_TOKEN", None)
    response = client.post("/settings/token/rotate")
    assert response.status_code == 403
    assert config_service.status()["APP_AUTH_TOKEN"]["configured"] is False


def test_rotate_token_returns_token_once_with_no_store(config_service) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        response = client.post(
            "/settings/token/rotate", headers={"X-Auth-Token": "s3cret"}
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'id="settings-revealed-token"' in response.text


def test_rotated_token_is_never_shown_on_a_later_get(config_service) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
        rotate_response = client.post(
            "/settings/token/rotate", headers={"X-Auth-Token": "s3cret"}
        )
    token = config_service.status()["APP_AUTH_TOKEN"]
    assert token["configured"] is True
    assert 'id="settings-revealed-token"' in rotate_response.text

    get_response = client.get("/partials/settings")

    assert 'id="settings-revealed-token"' not in get_response.text


def test_rotating_token_invalidates_the_previous_token_for_remote_clients(
    config_service,
) -> None:
    with patch.dict(os.environ, {"APP_AUTH_TOKEN": "old-token"}):
        client.post("/settings/token/rotate", headers={"X-Auth-Token": "old-token"})
        response = client.post(
            "/settings/clear",
            data={"key": "FMP_API_KEY"},
            headers={"X-Auth-Token": "old-token"},
        )

    assert response.status_code == 403
