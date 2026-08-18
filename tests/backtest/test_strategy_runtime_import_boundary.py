"""AD-10's mechanical trust boundary: a Strategy runtime module must never
reach live trading state, directly or transitively.

Walks a Strategy module's local (first-party ``app.*``) import graph via
static AST parsing -- no module is ever executed -- and fails on any
direct, aliased, ``from``, or transitive import of a forbidden dependency
family: ``app.agents`` (covers ``TraderAgent`` and every live agent,
including ``app.agents.analyst.analyst_agent``) and ``app.repositories``
(covers live portfolio/trade/cash/position repositories and the
``Connect``/DB-session factory in ``app.repositories.db``). This is a
test-time guard, not a runtime sandbox: ``typing.Protocol``/Pyrefly give no
such enforcement at call time, so the safety boundary documented in
``docs/strategy-manager/strategy-authoring-v1.md`` must be proven here
instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Package roots a Strategy runtime module may never import, directly or
#: transitively -- AD-10's forbidden dependency families.
_FORBIDDEN_PREFIXES: tuple[str, ...] = ("app.agents", "app.repositories")

FIXTURE_STRATEGY_PATH = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "backtest-strategies"
    / "minimal-strategy"
    / "scripts"
    / "strategy.py"
)


class ForbiddenImportError(AssertionError):
    """Raised naming the offending module and forbidden dependency."""


def _imported_modules(source: str) -> list[str]:
    """Return every absolute dotted module name a module's AST imports."""
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                modules.append(node.module)
    return modules


def _is_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_PREFIXES
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
    stack = [entry_path.resolve()]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        source = current.read_text(encoding="utf-8")
        for module in _imported_modules(source):
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


def test_transitive_forbidden_import_is_rejected(tmp_path: Path) -> None:
    """A Strategy that only imports a real, non-``app.agents`` project
    module (``app.services.trader_service``) must still be rejected,
    because that module itself imports ``TraderAgent``."""
    module = tmp_path / "bad_strategy.py"
    module.write_text(
        "from app.services.trader_service import TraderService\n", encoding="utf-8"
    )

    with pytest.raises(ForbiddenImportError, match="app.agents"):
        assert_no_forbidden_imports(module)


# ---------------------------------------------------------------------------
# Approved import in Strategy runtime
# ---------------------------------------------------------------------------


def test_approved_dependency_free_core_import_passes(tmp_path: Path) -> None:
    module = tmp_path / "good_strategy.py"
    module.write_text(
        "from app.core.stage_classification import classify_weinstein_stage\n",
        encoding="utf-8",
    )

    assert_no_forbidden_imports(module)  # does not raise


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


# ---------------------------------------------------------------------------
# The real fixture Strategy proves the boundary end to end
# ---------------------------------------------------------------------------


def test_fixture_strategy_imports_nothing_forbidden() -> None:
    assert FIXTURE_STRATEGY_PATH.is_file()
    assert_no_forbidden_imports(FIXTURE_STRATEGY_PATH)  # does not raise


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

    with pytest.raises(ForbiddenImportError, match="app.agents.trader"):
        assert_no_forbidden_imports(module, project_root=project_root)


def test_unresolvable_first_party_import_fails_loudly(tmp_path: Path) -> None:
    """A first-party ``app.*`` import that cannot be resolved to a source
    file must fail the walk rather than silently stop following it."""
    module = tmp_path / "bad_strategy.py"
    module.write_text("import app.does_not_exist\n", encoding="utf-8")

    with pytest.raises(ForbiddenImportError, match="app.does_not_exist"):
        assert_no_forbidden_imports(module)
