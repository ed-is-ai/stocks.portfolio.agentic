"""Story 2.2 coverage: ``discover_strategies`` fail-soft Skill discovery.

Fixtures live under
``tests/fixtures/backtest-strategies/discovery/`` -- a test-only root
mirroring the real ``skills/<name>/`` layout, entirely separate from the
Story 2.1 fixture at ``tests/fixtures/backtest-strategies/minimal-strategy/``
(never modified here) and from the real ``skills/`` tree (never scanned
here).
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping

import pytest

import app.services.backtest.skill_discovery as skill_discovery_module
from app.services.backtest.run_universe import (
    RunUniverseError,
    RunUniverseErrorCode,
    run_universe_digest,
)
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyDiscoveryWarningV1,
    discover_strategies,
)
from app.services.backtest.strategy_protocol import StrategyProtocolV1
from app.services.backtest.worker import _load_strategy_instance

FIXTURES_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "backtest-strategies"
    / "discovery"
)
LIVE_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"

EXPECTED_LIVE_STRATEGY_DEFAULTS = {
    "rtly-backtest-buy-and-hold": {
        "fixed_shares": 10,
        "entry_on_or_after": "2000-01-01",
    },
    "rtly-backtest-darvas-box": {
        "fixed_shares": 10,
        "box_lookback_sessions": 20,
        "maximum_box_depth_pct": 15.0,
        "volume_multiplier": 1.5,
    },
    "rtly-backtest-minervini": {
        "fixed_shares": 10,
        "minimum_vcp_score": 70,
        "minimum_trend_score": 85.0,
        "minimum_relative_volume": 1.5,
        "maximum_pivot_extension_pct": 3.0,
        "maximum_loss_pct": 8.0,
    },
    "rtly-backtest-moving-average": {
        "fixed_shares": 10,
        "fast_window": 50,
        "slow_window": 200,
    },
    "rtly-backtest-turtle-trend": {
        "fixed_shares": 10,
        "entry_lookback_sessions": 20,
        "exit_lookback_sessions": 10,
    },
    "rtly-backtest-weinstein": {
        "fixed_shares": 10,
        "breakout_lookback_sessions": 50,
        "minimum_relative_volume": 1.5,
        "maximum_loss_pct": 10.0,
    },
}

#: The packaging/universe frontmatter every ``kind: backtest-strategy``
#: manifest must now declare, reused by the ad-hoc fixtures below.
_UNIVERSE_BLOCK = (
    "strategy_universe:\n"
    "  schema_version: strategy_universe.v1\n"
    "  mode: selected-securities\n"
    "  parameter: selected_securities\n"
)

_UNIVERSE_FRONTMATTER = "runtime_files:\n  - scripts/strategy.py\n" + _UNIVERSE_BLOCK

OLD_LIVE_STRATEGY_IDS = {
    "buy-and-hold-backtest",
    "darvas-box-backtest",
    "minervini-backtest",
    "moving-average-backtest",
    "turtle-trend-backtest",
    "weinstein-backtest",
}


def _warnings_by_folder(
    warnings: tuple[StrategyDiscoveryWarningV1, ...],
) -> dict[str, StrategyDiscoveryWarningV1]:
    return {warning.folder: warning for warning in warnings}


def _strategies_by_id(
    strategies: tuple[StrategyDescriptorV1, ...],
) -> dict[str, StrategyDescriptorV1]:
    return {descriptor.strategy_id: descriptor for descriptor in strategies}


# ---------------------------------------------------------------------------
# Full-root scan -- every I/O-matrix scenario in one pass
# ---------------------------------------------------------------------------


def test_live_backtest_strategies_discover_with_runnable_defaults() -> None:
    result = discover_strategies(LIVE_SKILLS_ROOT)
    strategies = _strategies_by_id(result.strategies)
    warnings = _warnings_by_folder(result.warnings)

    assert set(EXPECTED_LIVE_STRATEGY_DEFAULTS) <= set(strategies)
    assert OLD_LIVE_STRATEGY_IDS.isdisjoint(strategies)
    assert tuple(
        descriptor.strategy_id
        for descriptor in result.strategies
        if descriptor.strategy_id in EXPECTED_LIVE_STRATEGY_DEFAULTS
    ) == tuple(EXPECTED_LIVE_STRATEGY_DEFAULTS)
    assert not set(EXPECTED_LIVE_STRATEGY_DEFAULTS) & set(warnings)
    assert {
        strategy_id: dict(strategies[strategy_id].default_parameters)
        for strategy_id in EXPECTED_LIVE_STRATEGY_DEFAULTS
    } == EXPECTED_LIVE_STRATEGY_DEFAULTS


def test_live_backtest_strategies_load_through_production_worker() -> None:
    result = discover_strategies(LIVE_SKILLS_ROOT)

    for descriptor in result.strategies:
        if descriptor.strategy_id not in EXPECTED_LIVE_STRATEGY_DEFAULTS:
            continue
        runtime_path = LIVE_SKILLS_ROOT / descriptor.runtime_path
        assert isinstance(_load_strategy_instance(runtime_path), StrategyProtocolV1)


def test_discover_strategies_full_root_scan() -> None:
    result = discover_strategies(FIXTURES_ROOT)

    strategies = _strategies_by_id(result.strategies)
    warnings = _warnings_by_folder(result.warnings)

    # Only the one genuinely valid Strategy is returned.
    assert set(strategies) == {"valid-strategy"}

    # Every malformed/isolated folder produced exactly one warning, with
    # the expected stable code.
    assert warnings["malformed-api-version"].code == "unsupported_api_version"
    assert warnings["missing-description"].code == "malformed_frontmatter"
    assert warnings["invalid-default"].code == "invalid_defaults"
    assert warnings["missing-runtime-entrypoint"].code == "missing_runtime_entrypoint"
    assert warnings["unsafe-yaml-duplicate-key"].code == "malformed_frontmatter"
    assert warnings["unsafe-yaml-alias"].code == "malformed_frontmatter"
    assert warnings["duplicate-strategy-one"].code == "duplicate_identity"
    assert warnings["duplicate-strategy-two"].code == "duplicate_identity"
    assert (
        warnings["duplicate-parameter-declaration"].code == "invalid_parameter_schema"
    )

    # Ordinary Skills and a folder with no SKILL.md at all produce no
    # descriptor and no warning -- fully invisible to discovery.
    assert "ordinary-skill" not in strategies
    assert "ordinary-skill" not in warnings
    assert "no-skill-md" not in strategies
    assert "no-skill-md" not in warnings

    # Exactly one warning per isolated folder -- no folder produced two.
    isolated_folders = [
        "malformed-api-version",
        "missing-description",
        "invalid-default",
        "missing-runtime-entrypoint",
        "unsafe-yaml-duplicate-key",
        "unsafe-yaml-alias",
        "duplicate-strategy-one",
        "duplicate-strategy-two",
        "duplicate-parameter-declaration",
    ]
    assert len(result.warnings) == len(isolated_folders)
    assert set(warnings) == set(isolated_folders)


# ---------------------------------------------------------------------------
# Valid Strategy
# ---------------------------------------------------------------------------


def test_discover_strategies_valid_strategy_descriptor_shape() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    strategies = _strategies_by_id(result.strategies)

    descriptor = strategies["valid-strategy"]
    assert descriptor.strategy_id == "valid-strategy"
    assert descriptor.display_name == "Valid Strategy"
    assert descriptor.description.startswith("A minimal fixture Strategy")
    assert descriptor.api_version == 1
    assert descriptor.runtime_path == "valid-strategy/scripts/strategy.py"
    assert descriptor.source_digest  # non-empty digest
    assert len(descriptor.source_digest) == 64  # sha256 hex digest

    parameter_names = [parameter.name for parameter in descriptor.parameters]
    assert parameter_names == ["watch_security_id", "fixed_shares"]

    # Runnable defaults were already validated -- normalized and complete.
    assert descriptor.default_parameters == {
        "watch_security_id": "sec-aapl",
        "fixed_shares": 1,
    }


def test_discover_strategies_display_name_falls_back_deterministically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    folder = root / "no-display-name"
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "strategy.py").write_text("", encoding="utf-8")
    (folder / "SKILL.md").write_text(
        "---\n"
        "kind: backtest-strategy\n"
        "name: no-display-name\n"
        "description: Fallback display name check.\n"
        "api_version: 1\n"
        "runtime_files:\n"
        "  - scripts/strategy.py\n"
        "strategy_universe:\n"
        "  schema_version: strategy_universe.v1\n"
        "  mode: selected-securities\n"
        "  parameter: selected_securities\n"
        "---\n",
        encoding="utf-8",
    )

    result = discover_strategies(root)

    assert len(result.strategies) == 1
    assert result.strategies[0].display_name == "No Display Name"


# ---------------------------------------------------------------------------
# Ordinary Skill -- silently ignored, no warning
# ---------------------------------------------------------------------------


def test_discover_strategies_ordinary_skill_produces_nothing() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    folders_seen = {descriptor.strategy_id for descriptor in result.strategies} | {
        warning.folder for warning in result.warnings
    }
    assert "ordinary-skill" not in folders_seen
    assert "no-skill-md" not in folders_seen


# ---------------------------------------------------------------------------
# Malformed metadata
# ---------------------------------------------------------------------------


def test_discover_strategies_bad_api_version_type_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["malformed-api-version"]
    assert warning.code == "unsupported_api_version"
    assert warning.field == "api_version"
    assert "malformed-api-version" not in {
        descriptor.strategy_id for descriptor in result.strategies
    }


def test_discover_strategies_missing_required_field_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["missing-description"]
    assert warning.code == "malformed_frontmatter"
    assert warning.field == "description"


# ---------------------------------------------------------------------------
# Duplicate identity
# ---------------------------------------------------------------------------


def test_discover_strategies_duplicate_identity_isolates_both() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    strategy_ids = {descriptor.strategy_id for descriptor in result.strategies}

    assert warnings["duplicate-strategy-one"].code == "duplicate_identity"
    assert warnings["duplicate-strategy-two"].code == "duplicate_identity"
    assert "duplicate-strategy-one" not in strategy_ids
    assert "duplicate-strategy-two" not in strategy_ids


def test_discover_strategies_duplicate_identity_is_case_insensitive(
    tmp_path: Path,
) -> None:
    """The two duplicate fixtures use differently-cased display names
    ('Shared Display Name' vs 'shared display name') -- proving the
    canonicalized (casefolded) comparison, not a byte-for-byte one."""
    root = tmp_path / "skills"
    for folder_name, display_name in (
        ("dup-a", "Shared Name"),
        ("dup-b", "shared name"),
    ):
        folder = root / folder_name
        (folder / "scripts").mkdir(parents=True)
        (folder / "scripts" / "strategy.py").write_text("", encoding="utf-8")
        (folder / "SKILL.md").write_text(
            "---\n"
            "kind: backtest-strategy\n"
            f"name: {folder_name}\n"
            f"display_name: {display_name}\n"
            "description: Case-insensitive duplicate check.\n"
            "api_version: 1\n"
            "runtime_files:\n"
            "  - scripts/strategy.py\n"
            "strategy_universe:\n"
            "  schema_version: strategy_universe.v1\n"
            "  mode: selected-securities\n"
            "  parameter: selected_securities\n"
            "---\n",
            encoding="utf-8",
        )

    result = discover_strategies(root)
    assert result.strategies == ()
    assert {warning.folder for warning in result.warnings} == {"dup-a", "dup-b"}
    assert all(warning.code == "duplicate_identity" for warning in result.warnings)


# ---------------------------------------------------------------------------
# Unsafe / duplicate-key YAML
# ---------------------------------------------------------------------------


def test_discover_strategies_duplicate_yaml_key_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["unsafe-yaml-duplicate-key"]
    assert warning.code == "malformed_frontmatter"
    assert "duplicate" in warning.message.lower()


def test_discover_strategies_yaml_alias_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["unsafe-yaml-alias"]
    assert warning.code == "malformed_frontmatter"
    assert "alias" in warning.message.lower()


def test_discover_strategies_rejects_multi_document_frontmatter(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    folder = root / "multi-doc"
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "strategy.py").write_text("", encoding="utf-8")
    (folder / "SKILL.md").write_text(
        "---\n"
        "kind: backtest-strategy\n"
        "name: multi-doc\n"
        "description: First document.\n"
        "api_version: 1\n"
        "runtime_files:\n"
        "  - scripts/strategy.py\n"
        "strategy_universe:\n"
        "  schema_version: strategy_universe.v1\n"
        "  mode: selected-securities\n"
        "  parameter: selected_securities\n"
        "---\n"
        "---\n"
        "second: document\n"
        "---\n",
        encoding="utf-8",
    )

    result = discover_strategies(root)
    assert result.strategies == ()
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "malformed_frontmatter"


# ---------------------------------------------------------------------------
# Invalid declared default
# ---------------------------------------------------------------------------


def test_discover_strategies_out_of_range_default_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["invalid-default"]
    assert warning.code == "invalid_defaults"
    assert "invalid-default" not in {
        descriptor.strategy_id for descriptor in result.strategies
    }


def test_discover_strategies_duplicate_parameter_declaration_is_isolated() -> None:
    """``validate_strategy_parameters``'s ``DUPLICATE_PARAMETER_DECLARATION``
    check is reachable end-to-end through ``discover_strategies``, not only
    at the ``validate_strategy_parameters`` unit-test layer."""
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["duplicate-parameter-declaration"]
    assert warning.code == "invalid_parameter_schema"
    assert "duplicate-parameter-declaration" not in {
        descriptor.strategy_id for descriptor in result.strategies
    }


# ---------------------------------------------------------------------------
# Missing runtime entrypoint
# ---------------------------------------------------------------------------


def test_discover_strategies_missing_runtime_entrypoint_is_isolated() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    warnings = _warnings_by_folder(result.warnings)
    warning = warnings["missing-runtime-entrypoint"]
    assert warning.code == "missing_runtime_entrypoint"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_discover_strategies_scan_order_is_deterministic() -> None:
    first = discover_strategies(FIXTURES_ROOT)
    second = discover_strategies(FIXTURES_ROOT)
    assert first.strategies == second.strategies
    assert first.warnings == second.warnings


def test_discover_strategies_warning_order_is_sorted_by_folder() -> None:
    result = discover_strategies(FIXTURES_ROOT)
    folders = [warning.folder for warning in result.warnings]
    assert folders == sorted(folders)


# ---------------------------------------------------------------------------
# Never imports/executes scripts/strategy.py
# ---------------------------------------------------------------------------


def test_discover_strategies_never_imports_strategy_module() -> None:
    """``valid-strategy/scripts/strategy.py`` raises ``RuntimeError`` at
    module scope if ever imported/executed -- a successful, warning-free
    discovery of it (already proven above) is itself proof discovery never
    imports it. This test additionally asserts the scan adds *no* new
    module to ``sys.modules`` at all.

    Deliberately compares a before/after snapshot rather than asserting
    ``"strategy" not in sys.modules`` outright: the Story 2.1 fixture's own
    contract tests (``tests/fixtures/backtest-strategies/minimal-strategy/
    scripts/tests/``) legitimately import a module literally named
    ``strategy`` via their own ``conftest.py`` `sys.path` convention, and
    may have already run earlier in the same pytest session -- that is the
    known, already-logged ``sys.modules["strategy"]`` collision risk
    (unrelated to discovery), not something this scan itself does.
    """
    modules_before = set(sys.modules)

    result = discover_strategies(FIXTURES_ROOT)

    assert set(sys.modules) == modules_before
    strategies = _strategies_by_id(result.strategies)
    assert "valid-strategy" in strategies


# ---------------------------------------------------------------------------
# Structural edge cases
# ---------------------------------------------------------------------------


def test_discover_strategies_missing_root_returns_empty_result(
    tmp_path: Path,
) -> None:
    result = discover_strategies(tmp_path / "does-not-exist")
    assert result.strategies == ()
    assert result.warnings == ()


def test_discover_strategies_symlink_escape_is_silently_skipped(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nkind: backtest-strategy\nname: escaped\n"
        "description: d\napi_version: 1\n---\n",
        encoding="utf-8",
    )
    root = tmp_path / "skills"
    root.mkdir()
    escape_link = root / "escaped"
    try:
        escape_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not supported on this filesystem")

    result = discover_strategies(root)
    assert result.strategies == ()
    assert result.warnings == ()


def test_discover_strategies_empty_frontmatter_is_silently_skipped(
    tmp_path: Path,
) -> None:
    """``---\\n---`` parses to ``None`` via YAML, not ``{}`` -- must still be
    treated as "no ``kind`` field", not warned as malformed."""
    root = tmp_path / "skills"
    root.mkdir()
    folder = root / "empty-frontmatter"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\n---\n# just a heading\n", encoding="utf-8")

    result = discover_strategies(root)

    assert result.strategies == ()
    assert result.warnings == ()


def test_discover_strategies_unclosed_frontmatter_warns(tmp_path: Path) -> None:
    """An opening ``---`` with no matching closing ``---`` is malformed --
    it must warn, not be silently skipped like a file with no frontmatter
    at all."""
    root = tmp_path / "skills"
    root.mkdir()
    folder = root / "unclosed"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nkind: backtest-strategy\nname: unclosed\n", encoding="utf-8"
    )

    result = discover_strategies(root)

    assert result.strategies == ()
    warning = _warnings_by_folder(result.warnings)["unclosed"]
    assert warning.code == "malformed_frontmatter"


def test_discover_strategies_unreadable_root_returns_empty_result_not_raise(
    tmp_path: Path,
) -> None:
    """``skills_root`` itself being unreadable (e.g. a permission error)
    degrades to "nothing discovered" -- it must never propagate an
    ``OSError`` out of a module whose whole contract is failing soft."""
    root = tmp_path / "skills"
    root.mkdir()
    root.chmod(0o000)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        result = discover_strategies(root)
    finally:
        root.chmod(0o755)

    assert result.strategies == ()
    assert result.warnings == ()


def test_discover_strategies_one_bad_folder_never_aborts_the_scan() -> None:
    """Several sibling folders malformed in different ways don't stop the
    scan from still discovering ``valid-strategy`` under the same root."""
    result = discover_strategies(FIXTURES_ROOT)
    assert any(
        descriptor.strategy_id == "valid-strategy" for descriptor in result.strategies
    )
    assert len(result.warnings) > 0


def test_discover_strategies_unexpected_exception_is_isolated_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception raised mid-processing of one folder is
    caught by ``discover_strategies``'s catch-all and turned into a
    warning for that folder alone; every other folder still discovers
    normally -- directly exercising the ``except Exception`` isolation
    path, not just its already-covered downstream symptoms."""
    root = tmp_path / "skills"
    root.mkdir()
    (root / "boom").mkdir()
    (root / "boom" / "SKILL.md").write_text(
        "---\nkind: backtest-strategy\nname: boom\n"
        "description: d\napi_version: 1\n---\n",
        encoding="utf-8",
    )
    good = root / "valid"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nkind: backtest-strategy\nname: valid\n"
        "description: d\napi_version: 1\n" + _UNIVERSE_FRONTMATTER + "---\n",
        encoding="utf-8",
    )
    (good / "scripts").mkdir()
    (good / "scripts" / "strategy.py").write_text(
        "STRATEGY_API_VERSION = 1\n", encoding="utf-8"
    )

    real_process_folder = skill_discovery_module._process_folder

    def _process_folder_raising_for_boom(folder: Path, skills_root: Path):
        if folder.name == "boom":
            raise RuntimeError("simulated unexpected failure")
        return real_process_folder(folder, skills_root)

    monkeypatch.setattr(
        skill_discovery_module, "_process_folder", _process_folder_raising_for_boom
    )

    result = discover_strategies(root)

    assert any(descriptor.strategy_id == "valid" for descriptor in result.strategies)
    warning = _warnings_by_folder(result.warnings)["boom"]
    assert "simulated unexpected failure" in warning.message


# ---------------------------------------------------------------------------
# Story 4.2: packaging, import safety and the selected-universe contract
# ---------------------------------------------------------------------------


def _write_skill(
    root: Path,
    name: str,
    *,
    runtime_files: str = "runtime_files:\n  - scripts/strategy.py\n",
    universe: str = _UNIVERSE_BLOCK,
    parameters: str = "parameters: []\n",
    files: Mapping[str, str] | None = None,
) -> Path:
    """Write one ``kind: backtest-strategy`` Skill folder under ``root``.

    Every frontmatter block is injectable so a test can vary exactly the
    one rule it is pinning and leave the rest of the manifest valid.
    """
    folder = root / name
    for relative, content in {
        "scripts/strategy.py": "STRATEGY_API_VERSION = 1\n",
        **(files or {}),
    }.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (folder / "SKILL.md").write_text(
        "---\n"
        "kind: backtest-strategy\n"
        f"name: {name}\n"
        f"display_name: {name}\n"
        "description: Story 4.2 contract fixture.\n"
        "api_version: 1\n" + runtime_files + universe + parameters + "---\n",
        encoding="utf-8",
    )
    return folder


def _only_warning(root: Path, name: str) -> StrategyDiscoveryWarningV1:
    """Scan ``root`` and return ``name``'s single isolating warning.

    Also asserts a valid sibling written by the caller still discovers, so
    every rejection case doubles as proof one bad Skill never blocks the
    rest of the scan.
    """
    result = discover_strategies(root)
    assert name not in _strategies_by_id(result.strategies)
    return _warnings_by_folder(result.warnings)[name]


@pytest.mark.parametrize(
    ("case", "runtime_files", "files", "reason"),
    (
        ("no-declaration", "", None, "missing_runtime_files"),
        (
            "not-a-list",
            "runtime_files: scripts/strategy.py\n",
            None,
            "malformed_runtime_files",
        ),
        ("empty-list", "runtime_files: []\n", None, "malformed_runtime_files"),
        (
            "blank-entry",
            "runtime_files:\n  - scripts/strategy.py\n  - '  '\n",
            None,
            "malformed_runtime_files",
        ),
        (
            "repeated-entry",
            "runtime_files:\n  - scripts/strategy.py\n  - scripts/strategy.py\n",
            None,
            "duplicate_runtime_file",
        ),
        (
            "no-entrypoint",
            "runtime_files:\n  - scripts/helper.py\n",
            None,
            "undeclared_entrypoint",
        ),
        (
            "traversing-entry",
            "runtime_files:\n  - scripts/strategy.py\n  - ../outside.py\n",
            None,
            "runtime_file_escapes_skill",
        ),
        (
            "absolute-entry",
            "runtime_files:\n  - scripts/strategy.py\n  - /etc/passwd\n",
            None,
            "runtime_file_escapes_skill",
        ),
        (
            "declared-but-absent",
            "runtime_files:\n  - scripts/strategy.py\n  - scripts/helper.py\n",
            None,
            "missing_runtime_file",
        ),
    ),
)
def test_discover_strategies_rejects_a_malformed_runtime_files_allowlist(
    tmp_path: Path,
    case: str,
    runtime_files: str,
    files: Mapping[str, str] | None,
    reason: str,
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, case, runtime_files=runtime_files, files=files)
    _write_skill(root, "good-neighbour")

    warning = _only_warning(root, case)

    assert warning.code == "invalid_runtime_files"
    assert warning.field == "runtime_files"
    assert warning.message.startswith(f"{reason}:")
    assert "good-neighbour" in {
        descriptor.strategy_id for descriptor in discover_strategies(root).strategies
    }


def test_discover_strategies_rejects_a_symlinked_runtime_file(tmp_path: Path) -> None:
    """Only content provably inside the Skill may participate in its
    ``source_digest`` -- a symlink could point anywhere after hashing."""
    root = tmp_path / "skills"
    outside = tmp_path / "outside.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    folder = _write_skill(
        root,
        "symlinked",
        runtime_files="runtime_files:\n  - scripts/strategy.py\n  - scripts/helper.py\n",
    )
    try:
        (folder / "scripts" / "helper.py").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not supported on this filesystem")

    warning = _only_warning(root, "symlinked")

    assert warning.code == "invalid_runtime_files"
    assert warning.message.startswith("runtime_file_symlink:")


@pytest.mark.parametrize(
    ("case", "source", "reason"),
    (
        ("forbidden-module", "import subprocess\n", "forbidden_import"),
        (
            "forbidden-repository",
            "from app.repositories.trades_repo import TradesRepository\n",
            "forbidden_import",
        ),
        (
            "app-internal",
            "from app.core.stage_classification import classify\n",
            "import_outside_runtime_allowlist",
        ),
        (
            "undeclared-relative",
            "from .helper import sma\n",
            "undeclared_runtime_import",
        ),
        (
            "relative-escape",
            "from ...other_skill import client\n",
            "relative_import_escape",
        ),
        ("dynamic-import", '__import__("requests")\n', "dynamic_import"),
        (
            "dynamic-import-module",
            'importlib.import_module("requests")\n',
            "dynamic_import",
        ),
        ("unparsable", "def broken(:\n", "unparsable_runtime_file"),
    ),
)
def test_discover_strategies_rejects_an_unsafe_runtime_import(
    tmp_path: Path, case: str, source: str, reason: str
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, case, files={"scripts/strategy.py": source})
    _write_skill(root, "good-neighbour")

    warning = _only_warning(root, case)

    assert warning.code == "unsafe_runtime_import"
    assert warning.field == "runtime_files"
    assert warning.message.startswith(f"{reason}:")
    assert "good-neighbour" in {
        descriptor.strategy_id for descriptor in discover_strategies(root).strategies
    }


def test_discover_strategies_allows_a_declared_same_skill_relative_import(
    tmp_path: Path,
) -> None:
    """The one relaxation over the old blanket relative-import ban: a
    helper declared in ``runtime_files`` is part of the same hashed Skill."""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "with-helper",
        runtime_files="runtime_files:\n  - scripts/helper.py\n  - scripts/strategy.py\n",
        files={
            "scripts/helper.py": "import math\n\n\ndef sma(values):\n    return values\n",
            "scripts/strategy.py": "from .helper import sma\n",
        },
    )

    result = discover_strategies(root)

    descriptor = _strategies_by_id(result.strategies)["with-helper"]
    assert result.warnings == ()
    assert descriptor.runtime_files == (
        "with-helper/scripts/helper.py",
        "with-helper/scripts/strategy.py",
    )


def test_discover_strategies_holds_a_declared_helper_to_the_same_import_rules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "unsafe-helper",
        runtime_files="runtime_files:\n  - scripts/helper.py\n  - scripts/strategy.py\n",
        files={
            "scripts/helper.py": "import socket\n",
            "scripts/strategy.py": "from .helper import connect\n",
        },
    )

    warning = _only_warning(root, "unsafe-helper")

    assert warning.code == "unsafe_runtime_import"
    assert warning.message.startswith("forbidden_import:")
    assert "scripts/helper.py" in warning.message


@pytest.mark.parametrize(
    ("case", "universe", "reason"),
    (
        ("absent", "", "missing_universe_metadata"),
        (
            "not-a-mapping",
            "strategy_universe: selected_securities\n",
            "malformed_universe_metadata",
        ),
        (
            "unknown-key",
            _UNIVERSE_BLOCK + "  maximum: 10\n",
            "malformed_universe_metadata",
        ),
        (
            "wrong-schema",
            "strategy_universe:\n"
            "  schema_version: strategy_universe.v2\n"
            "  mode: selected-securities\n"
            "  parameter: selected_securities\n",
            "unsupported_universe_schema",
        ),
        (
            "wrong-mode",
            "strategy_universe:\n"
            "  schema_version: strategy_universe.v1\n"
            "  mode: whole-market\n"
            "  parameter: selected_securities\n",
            "unsupported_universe_mode",
        ),
        (
            "empty-parameter",
            "strategy_universe:\n"
            "  schema_version: strategy_universe.v1\n"
            "  mode: selected-securities\n"
            "  parameter: ''\n",
            "empty_universe_parameter",
        ),
        (
            "padded-parameter",
            "strategy_universe:\n"
            "  schema_version: strategy_universe.v1\n"
            "  mode: selected-securities\n"
            "  parameter: ' selected_securities '\n",
            "empty_universe_parameter",
        ),
    ),
)
def test_discover_strategies_rejects_malformed_universe_metadata(
    tmp_path: Path, case: str, universe: str, reason: str
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, case, universe=universe)
    _write_skill(root, "good-neighbour")

    warning = _only_warning(root, case)

    assert warning.code == "invalid_universe_metadata"
    assert warning.field == "strategy_universe"
    assert warning.message.startswith(f"{reason}:")
    assert "good-neighbour" in {
        descriptor.strategy_id for descriptor in discover_strategies(root).strategies
    }


def test_discover_strategies_rejects_a_host_bound_parameter_declared_generically(
    tmp_path: Path,
) -> None:
    """The universe parameter is host-bound, so a second declaration in
    ``parameters`` would give it a conflicting editable/default identity."""
    root = tmp_path / "skills"
    _write_skill(
        root,
        "conflicting",
        parameters=(
            "parameters:\n"
            "  - name: selected_securities\n"
            "    type: string\n"
            "    default: sec-aapl\n"
            "    description: Duplicated host-bound name.\n"
            "    required: true\n"
        ),
    )

    warning = _only_warning(root, "conflicting")

    assert warning.code == "invalid_universe_metadata"
    assert warning.message.startswith("universe_parameter_conflict:")


def test_universe_parameter_has_no_editable_or_default_identity() -> None:
    """Every live Skill's host-bound parameter stays out of the generic
    parameter schema and its defaults -- nothing to render or edit."""
    result = discover_strategies(LIVE_SKILLS_ROOT)

    for descriptor in result.strategies:
        assert descriptor.universe.schema_version == "strategy_universe.v1"
        assert descriptor.universe.mode == "selected-securities"
        assert descriptor.universe.parameter not in {
            parameter.name for parameter in descriptor.parameters
        }
        assert descriptor.universe.parameter not in descriptor.default_parameters


def test_bind_universe_is_identical_across_selection_orders() -> None:
    descriptor = _strategies_by_id(discover_strategies(LIVE_SKILLS_ROOT).strategies)[
        "rtly-backtest-buy-and-hold"
    ]

    first = descriptor.bind_universe(["sec-msft", "sec-aapl", "sec-goog"])
    second = descriptor.bind_universe(["sec-goog", "sec-msft", "sec-aapl", "sec-msft"])

    assert first == second
    assert first[descriptor.universe.parameter] == ["sec-aapl", "sec-goog", "sec-msft"]
    assert run_universe_digest(
        first[descriptor.universe.parameter]  # type: ignore[arg-type]
    ) == run_universe_digest(["sec-msft", "sec-goog", "sec-aapl"])


def test_bind_universe_rejects_an_empty_selection() -> None:
    descriptor = _strategies_by_id(discover_strategies(LIVE_SKILLS_ROOT).strategies)[
        "rtly-backtest-buy-and-hold"
    ]

    with pytest.raises(RunUniverseError) as exc_info:
        descriptor.bind_universe([])

    assert exc_info.value.code is RunUniverseErrorCode.EMPTY_UNIVERSE


def test_source_digest_covers_every_declared_runtime_file(tmp_path: Path) -> None:
    """A declared helper's content is part of Strategy identity: editing it
    moves the digest, while an undeclared sibling file never can."""
    root = tmp_path / "skills"
    folder = _write_skill(
        root,
        "digest-check",
        runtime_files="runtime_files:\n  - scripts/helper.py\n  - scripts/strategy.py\n",
        files={
            "scripts/helper.py": "WINDOW = 20\n",
            "scripts/strategy.py": "from .helper import WINDOW\n",
            "scripts/notes.md": "not declared\n",
        },
    )

    def _digest() -> str:
        return _strategies_by_id(discover_strategies(root).strategies)[
            "digest-check"
        ].source_digest

    original = _digest()
    assert _digest() == original  # stable across repeated scans

    (folder / "scripts" / "notes.md").write_text(
        "still not declared\n", encoding="utf-8"
    )
    assert _digest() == original

    (folder / "scripts" / "helper.py").write_text("WINDOW = 21\n", encoding="utf-8")
    changed = _digest()
    assert changed != original

    (folder / "scripts" / "helper.py").write_text("WINDOW = 20\n", encoding="utf-8")
    assert _digest() == original  # deterministic, content-addressed
