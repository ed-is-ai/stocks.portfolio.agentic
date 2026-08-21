"""AD-10's mechanical trust boundary: a Strategy runtime module must never
reach live trading state, directly or transitively.

Walks a Strategy module's local (first-party ``app.*``) import graph via
static AST parsing -- no module is ever executed -- and fails on any
direct, aliased, ``from``, or transitive import of a forbidden dependency
family: ``app.agents`` (covers ``TraderAgent`` and every live agent,
including ``app.agents.analyst.analyst_agent``) and ``app.repositories``
(covers live portfolio/trade/cash/position repositories and the
``Connect``/DB-session factory in ``app.repositories.db``). It also rejects
process and network client modules that would bypass deterministic replay.

Scope split with production discovery: ``skill_discovery`` now owns the
per-Skill packaging check (every declared ``runtime_files`` entry, same-Skill
relative imports allowed, escapes/dynamic imports rejected) and this module
reuses its :data:`FORBIDDEN_RUNTIME_PREFIXES`/
:data:`ALLOWED_RUNTIME_PREFIXES` vocabularies verbatim -- there is one
allow/deny list, never two. What stays here is the part discovery
deliberately does not do: following an allowed *host* import
(``app.services.backtest.strategy_protocol``) transitively through the
first-party module graph, including ancestor ``__init__.py`` side effects.
The relative-import rule matches discovery's: a same-Skill relative import
is allowed (and followed), one that reaches above the Skill is rejected.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.backtest.skill_discovery import (
    ALLOWED_RUNTIME_PREFIXES as _ALLOWED_RUNTIME_PREFIXES,
    FORBIDDEN_RUNTIME_PREFIXES as _FORBIDDEN_PREFIXES,
    discover_strategies,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_STRATEGY_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "backtest-strategies"
    / "minimal-strategy"
    / "scripts"
    / "strategy.py"
)
LIVE_SKILLS_ROOT = PROJECT_ROOT / "skills"
LIVE_STRATEGY_PATHS = tuple(
    LIVE_SKILLS_ROOT / descriptor.runtime_path
    for descriptor in discover_strategies(LIVE_SKILLS_ROOT).strategies
)


class ForbiddenImportError(AssertionError):
    """Raised naming the offending module and forbidden dependency."""


def _imported_modules(
    source: str, *, enforce_runtime_rules: bool = False
) -> tuple[list[str], list[str]]:
    """Return a module's absolute imports and its same-Skill relative ones.

    Relative imports are reported as bare, current-directory-relative
    dotted names (``.helper`` -> ``helper``). One that reaches above its
    own folder (``from ..other import x``) is rejected outright, matching
    discovery's escape rule.
    """
    tree = ast.parse(source)
    modules: list[str] = []
    siblings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                if enforce_runtime_rules:
                    if node.level > 1:
                        raise ForbiddenImportError(
                            "Strategy runtimes may not use a relative import that "
                            "escapes their own Skill folder"
                        )
                    siblings.extend(
                        [node.module]
                        if node.module is not None
                        else [alias.name for alias in node.names]
                    )
            elif node.module is not None:
                modules.append(node.module)
        elif enforce_runtime_rules and isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                raise ForbiddenImportError(
                    "Strategy runtimes may not use dynamic __import__ calls"
                )
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                raise ForbiddenImportError(
                    "Strategy runtimes may not use dynamic import_module calls"
                )
    return modules, siblings


def _is_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_PREFIXES
    )


def _is_allowed_runtime_import(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _ALLOWED_RUNTIME_PREFIXES
    )


def _first_party_module_path(module: str, project_root: Path) -> Path:
    """Resolve an ``app.*`` dotted module name to its source file.

    Raises if no matching source file exists: an unresolvable first-party
    import must fail loudly rather than let the walk below silently stop
    following that branch, which would otherwise let an unresolved import
    escape the AD-10 boundary check unnoticed.
    """
    candidate = project_root.joinpath(*module.split("."))
    as_module = candidate.with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = candidate / "__init__.py"
    if as_package.is_file():
        return as_package
    raise ForbiddenImportError(
        f"cannot resolve first-party import {module!r} to a source file "
        "under app/ -- the AD-10 import-boundary walk cannot verify it"
    )


def _ancestor_package_paths(module: str, project_root: Path) -> list[Path]:
    """Return every ancestor package's existing ``__init__.py``.

    Importing ``app.services.trader_service`` also executes
    ``app/__init__.py`` and ``app/services/__init__.py`` as a side effect;
    a convenience re-export placed in either would otherwise be invisible
    to a walk that only ever visits the leaf module named by the import
    statement.
    """
    parts = module.split(".")
    paths: list[Path] = []
    for depth in range(1, len(parts)):
        package_init = project_root.joinpath(*parts[:depth]) / "__init__.py"
        if package_init.is_file():
            paths.append(package_init)
    return paths


def assert_no_forbidden_imports(
    entry_path: Path, *, project_root: Path = PROJECT_ROOT
) -> None:
    """Fail if ``entry_path`` imports a forbidden dependency, directly or
    transitively through any local ``app.*`` module it imports.

    Static/AST-only: no module is ever executed, so this is safe to run
    against modules that would otherwise open live database connections
    on import.
    """
    visited: set[Path] = set()
    entry = entry_path.resolve()
    # Every same-Skill module reached by a relative import is itself a
    # Strategy runtime module, so the closed runtime allowlist applies to
    # it too -- a helper may not import what its entrypoint may not.
    runtime_modules: set[Path] = {entry}
    stack = [entry]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        source = current.read_text(encoding="utf-8")
        is_runtime_entry = current in runtime_modules
        modules, siblings = _imported_modules(
            source, enforce_runtime_rules=is_runtime_entry
        )
        for name in siblings:
            sibling = current.parent.joinpath(*name.split(".")).with_suffix(".py")
            if sibling.is_file():
                runtime_modules.add(sibling.resolve())
                stack.append(sibling.resolve())
        for module in modules:
            if is_runtime_entry and not _is_allowed_runtime_import(module):
                raise ForbiddenImportError(
                    f"{current} imports dependency {module!r} outside the closed "
                    "Strategy runtime allowlist"
                )
            if _is_forbidden(module):
                raise ForbiddenImportError(
                    f"{current} imports forbidden dependency {module!r}"
                )
            if module == "app" or module.startswith("app."):
                for ancestor in _ancestor_package_paths(module, project_root):
                    if ancestor not in visited:
                        stack.append(ancestor)
                resolved = _first_party_module_path(module, project_root)
                if resolved not in visited:
                    stack.append(resolved)


# ---------------------------------------------------------------------------
# Forbidden import in Strategy runtime
# ---------------------------------------------------------------------------


def test_direct_forbidden_import_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text("import app.agents.analyst.analyst_agent\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="app.agents.analyst.analyst_agent"):
        assert_no_forbidden_imports(module)


def test_aliased_forbidden_import_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "import app.agents.analyst.analyst_agent as analyst\n", encoding="utf-8"
    )

    with pytest.raises(ForbiddenImportError, match="app.agents.analyst.analyst_agent"):
        assert_no_forbidden_imports(module)


def test_from_import_of_forbidden_module_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "from app.agents.analyst.analyst_agent import AnalystAgent\n",
        encoding="utf-8",
    )

    with pytest.raises(ForbiddenImportError, match="app.agents.analyst.analyst_agent"):
        assert_no_forbidden_imports(module)


def test_from_import_of_forbidden_package_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text("from app.agents import trader\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="app.agents"):
        assert_no_forbidden_imports(module)


def test_forbidden_repository_import_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "from app.repositories.trades_repo import TradesRepository\n",
        encoding="utf-8",
    )

    with pytest.raises(ForbiddenImportError, match="app.repositories.trades_repo"):
        assert_no_forbidden_imports(module)


@pytest.mark.parametrize(
    "module_name",
    ("subprocess", "socket", "requests", "os", "multiprocessing", "ftplib"),
)
def test_process_and_network_imports_are_rejected(
    tmp_path: Path, module_name: str
) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(f"import {module_name}\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match=module_name):
        assert_no_forbidden_imports(module)


def test_transitive_forbidden_import_is_rejected(tmp_path: Path) -> None:
    """A Strategy that only imports a real, non-``app.agents`` project
    module (``app.services.trader_service``) must still be rejected,
    because that module itself imports ``TraderAgent``."""
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "from app.services.trader_service import TraderService\n", encoding="utf-8"
    )

    with pytest.raises(ForbiddenImportError, match="closed Strategy runtime"):
        assert_no_forbidden_imports(module)


def test_allowed_host_protocol_is_still_checked_transitively(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    protocol = project_root / "app" / "services" / "backtest" / "strategy_protocol.py"
    protocol.parent.mkdir(parents=True)
    for package in (
        project_root / "app",
        project_root / "app" / "services",
        project_root / "app" / "services" / "backtest",
    ):
        (package / "__init__.py").write_text("", encoding="utf-8")
    protocol.write_text("import app.agents.trader\n", encoding="utf-8")
    module = project_root / "strategy.py"
    module.write_text(
        "from app.services.backtest.strategy_protocol import Signal\n",
        encoding="utf-8",
    )

    with pytest.raises(ForbiddenImportError, match="app.agents.trader"):
        assert_no_forbidden_imports(module, project_root=project_root)


# ---------------------------------------------------------------------------
# Approved import in Strategy runtime
# ---------------------------------------------------------------------------


def test_app_core_import_is_rejected_for_independently_releasable_skill(
    tmp_path: Path,
) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "from app.core.stage_classification import classify_weinstein_stage\n",
        encoding="utf-8",
    )

    with pytest.raises(ForbiddenImportError, match="closed Strategy runtime"):
        assert_no_forbidden_imports(module)


def test_approved_strategy_protocol_import_passes(tmp_path: Path) -> None:
    module = tmp_path / "good_strategy.py"
    module.write_text(
        "from app.services.backtest.strategy_protocol import Signal, SignalSide\n",
        encoding="utf-8",
    )

    assert_no_forbidden_imports(module)  # does not raise


def test_stdlib_only_import_passes(tmp_path: Path) -> None:
    module = tmp_path / "good_strategy.py"
    module.write_text("import math\nfrom datetime import date\n", encoding="utf-8")

    assert_no_forbidden_imports(module)  # does not raise


def test_same_skill_relative_helper_import_passes(tmp_path: Path) -> None:
    """Matches discovery's rule: a helper alongside the entrypoint (and
    declared in ``runtime_files``) is part of the same hashed Skill."""
    (tmp_path / "helper.py").write_text("import math\n", encoding="utf-8")
    module = tmp_path / "good_strategy.py"
    module.write_text("from .helper import sma\n", encoding="utf-8")

    assert_no_forbidden_imports(module)  # does not raise


def test_same_skill_helper_is_held_to_the_runtime_allowlist(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text("import subprocess\n", encoding="utf-8")
    module = tmp_path / "bad_strategy.py"
    module.write_text("from .helper import run\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="subprocess"):
        assert_no_forbidden_imports(module)


def test_relative_import_escaping_the_skill_is_rejected(tmp_path: Path) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text("from ..other_skill import client\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="escapes their own Skill"):
        assert_no_forbidden_imports(module)


@pytest.mark.parametrize(
    "source", ('__import__("requests")\n', 'importlib.import_module("requests")\n')
)
def test_dynamic_import_is_rejected(tmp_path: Path, source: str) -> None:
    module = tmp_path / "bad_strategy.py"
    module.write_text(source, encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="dynamic"):
        assert_no_forbidden_imports(module)


# ---------------------------------------------------------------------------
# The real fixture Strategy proves the boundary end to end
# ---------------------------------------------------------------------------


def test_fixture_strategy_imports_nothing_forbidden() -> None:
    assert FIXTURE_STRATEGY_PATH.is_file()
    assert_no_forbidden_imports(FIXTURE_STRATEGY_PATH)  # does not raise


@pytest.mark.parametrize(
    "strategy_path", LIVE_STRATEGY_PATHS, ids=lambda path: path.parts[-3]
)
def test_live_strategy_imports_nothing_forbidden(strategy_path: Path) -> None:
    assert strategy_path.is_file()
    assert_no_forbidden_imports(strategy_path)


# ---------------------------------------------------------------------------
# Parent-package __init__.py chain and unresolvable imports
# ---------------------------------------------------------------------------


def test_forbidden_reexport_from_an_ancestor_package_init_is_rejected(
    tmp_path: Path,
) -> None:
    """Importing ``app.services.trader_service`` also executes
    ``app/__init__.py`` and ``app/services/__init__.py`` as a side effect;
    a forbidden re-export placed in an ancestor package's ``__init__.py``
    must be caught even though no import statement names it directly."""
    project_root = tmp_path / "project"
    (project_root / "app" / "services").mkdir(parents=True)
    (project_root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "app" / "services" / "__init__.py").write_text(
        "from app.agents.trader import TraderAgent\n", encoding="utf-8"
    )
    (project_root / "app" / "services" / "leaf.py").write_text("", encoding="utf-8")
    module = project_root / "strategy.py"
    module.write_text("from app.services.leaf import something\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="closed Strategy runtime"):
        assert_no_forbidden_imports(module, project_root=project_root)


def test_unresolvable_first_party_import_fails_loudly(tmp_path: Path) -> None:
    """A first-party ``app.*`` import that cannot be resolved to a source
    file must fail the walk rather than silently stop following it."""
    module = tmp_path / "bad_strategy.py"
    module.write_text("import app.does_not_exist\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="app.does_not_exist"):
        assert_no_forbidden_imports(module)
