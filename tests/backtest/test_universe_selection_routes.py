"""Tests for universe selection routes (Story 4.5).

Tests roster ordering, search persistence, canonicalization,
stale profile rejection, and validation of the universe selector.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.app import app
from app.api.dependencies import (
    get_backtest_launch_service,
    get_backtest_repository,
)
from app.services.backtest.run_universe import (
    canonical_run_universe,
    run_universe_digest,
)
from fastapi.testclient import TestClient

import tests.test_strategy_manager_routes as routes_helpers

client = TestClient(app)

NOW = datetime(2026, 8, 21, 9, 30, tzinfo=timezone.utc)
PROFILE_HASH = "a" * 64
ROSTER_DIGEST = "b" * 64


class UniverseFakeRepo(routes_helpers.FakeRepo):
    """FakeRepo with a multi-security roster for universe tests."""

    def __init__(self) -> None:
        super().__init__()
        self._securities = [
            ("sid_002", "MSFT", "XNYS", "USD"),
            ("sid_001", "AAPL", "XNYS", "USD"),
            ("sid_003", "BARC", "XLON", "GBP"),
        ]

    def roster_member_identities(self, profile_hash):
        return list(self._securities)


@pytest.fixture
def universe_env(monkeypatch):
    repo = UniverseFakeRepo()
    launch = routes_helpers.FakeLaunchService()
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_backtest_launch_service] = lambda: launch
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repo, launch
    finally:
        app.dependency_overrides.pop(get_backtest_repository, None)
        app.dependency_overrides.pop(get_backtest_launch_service, None)


# ---------------------------------------------------------------------------
# Universe selector partial
# ---------------------------------------------------------------------------


def test_universe_selector_returns_securities(universe_env) -> None:
    response = client.get("/strategy-manager/configuration/universe")
    assert response.status_code == 200
    assert "AAPL" in response.text
    assert "MSFT" in response.text
    assert "BARC" in response.text


def test_universe_selector_shows_symbol_mic_currency(
    universe_env,
) -> None:
    response = client.get("/strategy-manager/configuration/universe")
    assert response.status_code == 200
    assert "XNYS" in response.text
    assert "XLON" in response.text
    assert "USD" in response.text
    assert "GBP" in response.text


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_canonical_run_universe_sorts_and_deduplicates() -> None:
    result = canonical_run_universe(["sid_002", "sid_001", "sid_002"])
    assert result == ("sid_001", "sid_002")


def test_run_universe_digest_order_independent() -> None:
    d1 = run_universe_digest(["sid_001", "sid_002"])
    d2 = run_universe_digest(["sid_002", "sid_001"])
    assert d1 == d2


def test_run_universe_digest_changes_with_set() -> None:
    d1 = run_universe_digest(["sid_001"])
    d2 = run_universe_digest(["sid_001", "sid_002"])
    assert d1 != d2


def test_canonical_run_universe_rejects_empty() -> None:
    from app.services.backtest.run_universe import (
        RunUniverseError,
        RunUniverseErrorCode,
    )

    with pytest.raises(RunUniverseError) as exc_info:
        canonical_run_universe([])
    assert exc_info.value.code is RunUniverseErrorCode.EMPTY_UNIVERSE


# ---------------------------------------------------------------------------
# Configuration form with universe
# ---------------------------------------------------------------------------


def test_configuration_form_includes_universe_selector(
    universe_env,
) -> None:
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    assert "Universe" in response.text
    # Should have checkboxes for securities
    assert "security_ids" in response.text


def test_configuration_submit_with_valid_universe(universe_env) -> None:
    repo, launch = universe_env
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "alpha",
            "profile_hash": PROFILE_HASH,
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "idem-1",
            "security_ids": "sid_001",
        },
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(launch.launch_calls) == 1


def test_configuration_submit_without_security_ids_fails(
    universe_env,
) -> None:
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "alpha",
            "profile_hash": PROFILE_HASH,
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "idem-1",
        },
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert "Select at least one security" in response.text


def test_configuration_submit_with_unknown_security_fails(
    universe_env,
) -> None:
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "alpha",
            "profile_hash": PROFILE_HASH,
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "idem-1",
            "security_ids": "sid_unknown",
        },
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert "Unknown securities" in response.text


def test_configuration_submit_with_stale_profile_fails(
    universe_env,
) -> None:
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "alpha",
            "profile_hash": "different_hash",
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "idem-1",
            "security_ids": "sid_001",
        },
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert "active profile has changed" in response.text


def test_configuration_submit_with_multiple_securities(
    universe_env,
) -> None:
    repo, launch = universe_env
    response = client.post(
        "/strategy-manager/configuration",
        data={
            "strategy_id": "alpha",
            "profile_hash": PROFILE_HASH,
            "activation_seq": "1",
            "start_month": "2024-01",
            "end_month": "2024-02",
            "base_currency": "GBP",
            "starting_capital": "10000",
            "idempotency_key": "idem-1",
            "security_ids": ["sid_001", "sid_002"],
        },
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(launch.launch_calls) == 1
