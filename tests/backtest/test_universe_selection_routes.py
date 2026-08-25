"""Tests for universe selection routes (Story 4.5).

Tests roster ordering, search persistence, canonicalization,
stale profile rejection, and validation of the universe selector.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api.app import app
from app.api.dependencies import (
    get_backtest_launch_service,
    get_backtest_repository,
    get_strategy_job_service,
)
from app.services.backtest.run_universe import (
    canonical_run_universe,
    run_universe_digest,
)
from fastapi.testclient import TestClient
from app.services.backtest.strategy_job import StrategyJobStatus, StrategyJobType

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

    def strategy_job(self, job_id):
        return SimpleNamespace(
            id=job_id,
            job_type=StrategyJobType.PREPARATION,
            status=StrategyJobStatus.COMPLETE,
            status_version=2,
            current_stage=None,
            failure_detail=None,
        )

    def preparation_run(self, job_id):
        return SimpleNamespace(
            job_id=job_id,
            selection=SimpleNamespace(canonical_security_ids=("sid_001",)),
        )

    def preparation_child_backtest_id(self, job_id):
        return "child-1"


@pytest.fixture
def universe_env(monkeypatch):
    repo = UniverseFakeRepo()
    launch = routes_helpers.FakeLaunchService()
    jobs = routes_helpers.FakeJobs()
    app.dependency_overrides[get_backtest_repository] = lambda: repo
    app.dependency_overrides[get_backtest_launch_service] = lambda: launch
    app.dependency_overrides[get_strategy_job_service] = lambda: jobs
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    try:
        yield repo, launch
    finally:
        app.dependency_overrides.pop(get_backtest_repository, None)
        app.dependency_overrides.pop(get_backtest_launch_service, None)
        app.dependency_overrides.pop(get_strategy_job_service, None)


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


def test_preparation_redirect_and_completed_child_navigation(universe_env) -> None:
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
            "idempotency_key": "page",
            "security_ids": "sid_001",
        },
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=True,
    )
    assert (
        response.status_code == 200
        and "Evidence preparation activity" in response.text
        and "/strategy-manager/activities/child-1" in response.text
        and "last_seen_version=" not in response.text
    )


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


# ---------------------------------------------------------------------------
# Whole-universe selection (GH #305)
# ---------------------------------------------------------------------------


def test_universe_selector_fresh_load_defaults_whole_universe_on(
    universe_env,
) -> None:
    response = client.get("/strategy-manager/configuration")
    assert response.status_code == 200
    assert 'name="whole_universe"' in response.text
    checkbox_pos = response.text.index('name="whole_universe"')
    # The checkbox must render checked on a genuinely fresh render.
    surrounding = response.text[checkbox_pos - 200 : checkbox_pos + 200]
    assert "checked" in surrounding


def test_universe_selector_partial_reflects_explicit_whole_universe_false(
    universe_env,
) -> None:
    response = client.get(
        "/strategy-manager/configuration/universe", params={"whole_universe": ""}
    )
    assert response.status_code == 200
    checkbox_pos = response.text.index('name="whole_universe"')
    surrounding = response.text[checkbox_pos - 200 : checkbox_pos + 200]
    assert "checked" not in surrounding


def test_universe_selector_partial_treats_literal_false_string_as_off(
    universe_env,
) -> None:
    # bool("false") is True in Python -- a literal "false" string must
    # not be misread as truthy just because it is a non-empty string.
    response = client.get(
        "/strategy-manager/configuration/universe",
        params={"whole_universe": "false"},
    )
    assert response.status_code == 200
    checkbox_pos = response.text.index('name="whole_universe"')
    surrounding = response.text[checkbox_pos - 200 : checkbox_pos + 200]
    assert "checked" not in surrounding


def test_universe_selector_partial_reflects_explicit_whole_universe_true(
    universe_env,
) -> None:
    response = client.get(
        "/strategy-manager/configuration/universe",
        params={"whole_universe": "true"},
    )
    assert response.status_code == 200
    checkbox_pos = response.text.index('name="whole_universe"')
    surrounding = response.text[checkbox_pos - 200 : checkbox_pos + 200]
    assert "checked" in surrounding


def test_configuration_submit_whole_universe_resolves_full_roster(
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
            "whole_universe": "true",
        },
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(launch.launch_calls) == 1
    command = launch.launch_calls[0]
    assert command.universe_selection is not None
    assert command.universe_selection.canonical_security_ids == (
        "sid_001",
        "sid_002",
        "sid_003",
    )


def test_configuration_submit_whole_universe_empty_roster_fails(
    universe_env,
) -> None:
    repo, launch = universe_env
    repo._securities = []
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
            "whole_universe": "true",
        },
        headers={"X-Auth-Token": "s3cret"},
    )
    assert response.status_code == 422
    assert "The active roster has no securities to select" in response.text


def test_configuration_submit_toggle_off_falls_back_to_manual_ids(
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
            "security_ids": "sid_001",
        },
        headers={"X-Auth-Token": "s3cret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    command = launch.launch_calls[0]
    assert command.universe_selection is not None
    assert command.universe_selection.canonical_security_ids == ("sid_001",)


def test_configuration_submit_whole_universe_stale_profile_fails(
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
            "whole_universe": "true",
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
