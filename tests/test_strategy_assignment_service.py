"""Tests for the ``StrategyAssignmentService`` seam (#440).

Discovery is monkeypatched (offline) and the analysis artifact lives in a
tmp_path file, so freshness can be exercised at the exact 24-hour boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.repositories import db
from app.repositories.portfolio_strategies_repo import (
    PortfolioStrategiesRepository,
)
from app.schemas.analysis_artifact import build_analysis_payload
from app.services import strategy_assignment_service as svc_module
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyDiscoveryResultV1,
    StrategyDiscoveryWarningV1,
    StrategyUniverseContractV1,
)
from app.services.backtest.strategy_protocol import (
    JsonScalar,
    StrategyParameterV1,
)
from app.services.strategy_assignment_service import (
    IncompatibleStrategyError,
    StrategyAssignmentService,
    UnknownStrategyError,
)


def _param(**overrides: object) -> StrategyParameterV1:
    defaults: dict[str, object] = dict(
        name="p", type="string", default="x", description="d", required=False
    )
    defaults.update(overrides)
    return StrategyParameterV1(**defaults)  # type: ignore[arg-type]


def _descriptor(
    strategy_id: str = "alpha",
    display_name: str = "Alpha",
    default_parameters: dict[str, object] | None = None,
) -> StrategyDescriptorV1:
    """Build one minimal, valid descriptor with a single integer parameter."""
    return StrategyDescriptorV1(
        strategy_id=strategy_id,
        source_manifest_version="strategy_source_manifest.v1",
        source_digest="a" * 64,
        display_name=display_name,
        description=f"{display_name} strategy",
        api_version=1,
        parameters=(
            _param(
                name="lookback",
                type="integer",
                default=20,
                description="Lookback window",
                required=True,
                minimum=1,
                maximum=100,
            ),
        ),
        default_parameters=cast(
            Mapping[str, JsonScalar],
            {"lookback": 20} if default_parameters is None else default_parameters,
        ),
        runtime_path=f"{strategy_id}/scripts/strategy.py",
        runtime_files=(f"{strategy_id}/scripts/strategy.py",),
        universe=StrategyUniverseContractV1(
            schema_version="strategy_universe.v1",
            mode="selected-securities",
            parameter="selected_securities",
        ),
    )


def _discovery_result(
    warnings: tuple[StrategyDiscoveryWarningV1, ...] = (),
) -> StrategyDiscoveryResultV1:
    return StrategyDiscoveryResultV1(
        strategies=(_descriptor(), _descriptor("beta", "Beta")),
        warnings=warnings,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    db.init_trades_db(conn)
    conn.execute("INSERT INTO portfolios (name, created_at) VALUES ('SIPP', 'now')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def service(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> StrategyAssignmentService:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    service = StrategyAssignmentService(
        repo,
        skills_root=tmp_path / "skills",
        analysis_path=tmp_path / "analysis.json",
    )
    monkeypatch.setattr(
        svc_module, "discover_strategies", lambda root: _discovery_result()
    )
    return service


# --- discovery -------------------------------------------------------------


def test_list_choices_and_warnings(service: StrategyAssignmentService) -> None:
    assert [c.strategy_id for c in service.list_choices()] == ["alpha", "beta"]
    assert service.list_warnings() == ()


def test_warnings_surfaced_without_breaking_choices(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    service = StrategyAssignmentService(
        repo, skills_root=tmp_path, analysis_path=tmp_path / "a.json"
    )
    warning = StrategyDiscoveryWarningV1(
        folder="broken", code="invalid_defaults", message="bad defaults"
    )
    monkeypatch.setattr(
        svc_module,
        "discover_strategies",
        lambda root: _discovery_result(warnings=(warning,)),
    )
    assert service.list_warnings() == (warning,)
    assert [c.strategy_id for c in service.list_choices()] == ["alpha", "beta"]


# --- assign ----------------------------------------------------------------


def test_assign_stores_validated_defaults_canonicalised(
    service: StrategyAssignmentService, db_path: Path
) -> None:
    assignment = service.assign(1, "alpha")
    assert assignment.strategy_id == "alpha"
    assert assignment.parameters == {"lookback": 20}

    conn = sqlite3.connect(db_path)
    raw = conn.execute(
        "SELECT parameters_json FROM portfolio_strategies WHERE portfolio_id = 1"
    ).fetchone()[0]
    conn.close()
    assert raw == '{"lookback":20}'


def test_assign_unknown_strategy_leaves_assignment_untouched(
    service: StrategyAssignmentService,
) -> None:
    service.assign(1, "alpha")
    with pytest.raises(UnknownStrategyError):
        service.assign(1, "ghost")
    assignment = service.get_assignment(1)
    assert assignment is not None
    assert assignment.assignment.strategy_id == "alpha"
    assert assignment.available


def test_assign_incompatible_defaults_raises(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = PortfolioStrategiesRepository(db.make_connect(lambda: db_path))
    service = StrategyAssignmentService(
        repo, skills_root=tmp_path, analysis_path=tmp_path / "a.json"
    )
    broken = _descriptor("broken", default_parameters={"lookback": 0})
    monkeypatch.setattr(
        svc_module,
        "discover_strategies",
        lambda root: StrategyDiscoveryResultV1(strategies=(broken,), warnings=()),
    )
    with pytest.raises(IncompatibleStrategyError):
        service.assign(1, "broken")
    assert service.get_assignment(1) is None


# --- enrich / views --------------------------------------------------------


def test_enrich_returns_unavailable_for_missing_discovery(
    service: StrategyAssignmentService, db_path: Path
) -> None:
    # Store an assignment for a strategy_id discovery no longer returns.
    service._repo.upsert(1, "ghost", {"lookback": 20})
    view = service.assignment_view(1)
    assert view is not None
    assert view.available is False
    assert view.display_name is None
    # The assignment itself is retained, never dropped.
    assert view.assignment.strategy_id == "ghost"


def test_enrich_returns_display_name_for_available(
    service: StrategyAssignmentService,
) -> None:
    view = service.assignment_view(1)
    assert view is None  # nothing assigned yet
    service.assign(1, "beta")
    view = service.assignment_view(1)
    assert view is not None
    assert view.available is True
    assert view.display_name == "Beta"


# --- freshness -------------------------------------------------------------


def _write_artifact(path: Path, generated_at: datetime) -> None:
    payload = build_analysis_payload([], run_id="run-1", generated_at=generated_at)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _freeze_now(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Pin the service module's ``datetime.now`` so the 24h boundary is exact."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return now

    monkeypatch.setattr(svc_module, "datetime", _FrozenDatetime)


def test_freshness_missing_when_file_absent(
    service: StrategyAssignmentService,
) -> None:
    assert service.freshness() == "missing"


def test_freshness_unknown_when_corrupt(
    service: StrategyAssignmentService, tmp_path: Path
) -> None:
    (tmp_path / "analysis.json").write_text("not json", encoding="utf-8")
    assert service.freshness() == "unknown"


def test_freshness_fresh_at_exactly_24h(
    service: StrategyAssignmentService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_at = datetime(2026, 1, 1, tzinfo=UTC)
    _write_artifact(tmp_path / "analysis.json", generated_at)
    _freeze_now(monkeypatch, generated_at + timedelta(hours=24))
    assert service.freshness() == "fresh"


def test_freshness_stale_at_24h_plus_1s(
    service: StrategyAssignmentService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_at = datetime(2026, 1, 1, tzinfo=UTC)
    _write_artifact(tmp_path / "analysis.json", generated_at)
    _freeze_now(monkeypatch, generated_at + timedelta(hours=24, seconds=1))
    assert service.freshness() == "stale"
