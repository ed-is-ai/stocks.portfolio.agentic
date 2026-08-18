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

import pytest

import app.services.backtest.skill_discovery as skill_discovery_module
from app.services.backtest.skill_discovery import (
    StrategyDescriptorV1,
    StrategyDiscoveryWarningV1,
    discover_strategies,
)

FIXTURES_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "backtest-strategies"
    / "discovery"
)


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
        "description: d\napi_version: 1\n---\n",
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
