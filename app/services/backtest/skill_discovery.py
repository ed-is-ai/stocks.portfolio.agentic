"""Fail-soft ``skills/`` discovery for ``kind: backtest-strategy`` Skills.

Scans the immediate child folders of an injected root ``Path`` for a
``SKILL.md`` whose frontmatter declares ``kind: backtest-strategy``, and
turns each one into an immutable :class:`StrategyDescriptorV1` -- the
metadata seam a future Backtest launch (Story 2.3) and UI (Story 2.7)
consume without any hand-maintained Strategy registry list.

Design constraints, all deliberate:

- **Never executes a Strategy.** :func:`discover_strategies` never imports
  or runs a discovered Strategy's runtime modules. It reads its
  ``SKILL.md`` frontmatter, hashes the content of every file the manifest
  declares in ``runtime_files`` via ``build_strategy_source_manifest``
  (never a second canonicalizer), and walks those files' import graph by
  static AST parsing only.
- **Packaging and universe contract.** A manifest declares an ordered
  ``runtime_files`` allowlist containing its ``scripts/strategy.py``
  entrypoint, plus ``strategy_universe.v1`` metadata binding
  ``mode: selected-securities`` to exactly one host-bound parameter. That
  parameter is never part of the generic ``parameters`` schema, so it has
  no editable or default identity and is excluded from generic parameter
  rendering; the host injects the canonical selected-security tuple into
  it at launch (:meth:`StrategyDescriptorV1.bind_universe`).
- **Ordinary Skills are invisible.** A folder with no ``SKILL.md``, or
  whose ``SKILL.md`` frontmatter has no ``kind: backtest-strategy`` line
  at all, is silently skipped -- no descriptor, no warning. Every other
  Skill in this repository (``skills/vcp-screener/``, and friends) must
  keep discovering as nothing.
- **One bad Strategy never aborts the scan.** Once a folder's frontmatter
  *does* declare ``kind: backtest-strategy``, any problem with it --
  malformed metadata, an unsupported ``api_version``, an invalid
  parameter schema, an out-of-range declared default, a missing runtime
  entrypoint, an undeclared/escaping runtime file, an unsafe import, a
  malformed universe contract, an unsafe YAML construct, or a colliding
  identity -- isolates
  that one Strategy with a structured :class:`StrategyDiscoveryWarningV1`
  and the scan continues. An unexpected exception anywhere in one folder's
  processing is caught and converted into the same kind of warning rather
  than aborting the whole scan.
- **Safe YAML only.** Frontmatter is parsed via a small ``yaml.SafeLoader``
  subclass that additionally rejects duplicate mapping keys and alias/
  anchor resolution -- never the unrestricted ``yaml.Loader``, and never a
  hand-rolled nested parser. Only the first bounded ``---``-delimited
  document is read; a second frontmatter-shaped block immediately
  following the first is treated as malformed rather than silently
  ignored. Whole-file reads are capped at :data:`_MAX_SKILL_MD_BYTES` and
  decoded strictly as UTF-8.
- **Revision-keyed process cache.** :func:`discover_strategies` first makes
  a deterministic, recursive filesystem revision from paths and stat
  identities.  A process-local cache reuses the immutable result only for
  that exact root and revision; edits, additions, and removals get a new
  revision.  The cache is initially empty -- there is no import-time
  discovery singleton.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
import re
import sys
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
import yaml
from yaml.events import AliasEvent
from yaml.resolver import BaseResolver

from app.services.backtest.run_universe import canonical_run_universe
from app.services.backtest.source_manifest import (
    SOURCE_MANIFEST_VERSION,
    build_strategy_source_manifest,
)
from app.services.backtest.strategy_protocol import (
    JsonScalar,
    JsonValue,
    StrategyParameterV1,
    StrategyProtocolError,
    validate_strategy_parameters,
)


# ---------------------------------------------------------------------------
# Bounds and closed vocabularies
# ---------------------------------------------------------------------------

#: The whole ``SKILL.md`` file (not just its frontmatter block) is rejected
#: outright above this size, before any parsing is attempted -- guards
#: against an unbounded read, not a claim that the frontmatter itself is
#: oversized.
_MAX_SKILL_MD_BYTES = 64 * 1024

#: The only ``kind`` value discovery recognizes. Anything else (or a
#: missing ``kind``) means "ordinary Skill" -- silently ignored.
_STRATEGY_KIND = "backtest-strategy"

#: The only ``api_version`` this discovery generation supports.
_SUPPORTED_API_VERSION = 1

_KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: The one Skill-relative runtime entrypoint every Strategy declares and
#: the worker loads; it must appear in the manifest's ``runtime_files``.
RUNTIME_ENTRYPOINT = "scripts/strategy.py"

#: The only ``strategy_universe`` schema/mode pair this generation binds.
STRATEGY_UNIVERSE_VERSION = "strategy_universe.v1"
SELECTED_SECURITIES_MODE = "selected-securities"

#: Module families a Strategy runtime file may never reach: live agents and
#: repositories (AD-10's trust boundary) plus the process/network clients
#: that would break deterministic replay. Shared verbatim with
#: ``tests/backtest/test_strategy_runtime_import_boundary.py``, which walks
#: the *host* modules a runtime import reaches transitively -- there is one
#: vocabulary, never two.
FORBIDDEN_RUNTIME_PREFIXES: tuple[str, ...] = (
    "app.agents",
    "app.repositories",
    "aiohttp",
    "http",
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib",
    "urllib3",
    "yfinance",
)

#: The closed set of absolute imports a Strategy runtime file may use.
#: Methodology code stays inside the Skill's own declared ``runtime_files``
#: (reachable by same-Skill relative import); the sole first-party
#: dependency is the versioned host protocol.
ALLOWED_RUNTIME_PREFIXES: tuple[str, ...] = (
    "__future__",
    "bisect",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "itertools",
    "math",
    "operator",
    "statistics",
    "typing",
    "app.services.backtest.regime_filter",
    "app.services.backtest.strategy_protocol",
)

#: Opt-in market-regime entry-filter parameters injected into every
#: ``kind: backtest-strategy`` descriptor at discovery time (never authored
#: per skill). One canonical definition keeps the default mapping, the
#: launch validator, and the six runtimes in lock-step; a SKILL.md that
#: also declares one of these names fails discovery with the existing
#: ``duplicate_parameter_declaration`` error. The runtime derivation lives
#: in ``app/services/backtest/regime_filter.py``.
COMMON_BACKTEST_STRATEGY_PARAMETERS: tuple[StrategyParameterV1, ...] = (
    StrategyParameterV1(
        name="regime_filter_enabled",
        type="boolean",
        default=False,
        description=(
            "When true, suppress every entry signal on any session where the "
            "benchmark security closes at or below its regime_filter_ma_length "
            "simple moving average."
        ),
        required=False,
    ),
    StrategyParameterV1(
        name="regime_filter_benchmark_security_id",
        type="string",
        default="",
        description=(
            "Canonical id of the benchmark security whose trend governs the "
            "regime filter. Must be one of the Run's selected securities."
        ),
        required=False,
    ),
    StrategyParameterV1(
        name="regime_filter_ma_length",
        type="integer",
        default=200,
        description=(
            "Length in sessions of the benchmark's trailing simple moving "
            "average used by the regime filter."
        ),
        required=False,
        minimum=2,
        maximum=400,
    ),
)

#: Closed, stable warning-code vocabulary a caller can branch on -- see the
#: module docstring for what triggers each one.
WarningCode = Literal[
    "malformed_frontmatter",
    "unsupported_api_version",
    "invalid_parameter_schema",
    "invalid_defaults",
    "missing_runtime_entrypoint",
    "invalid_runtime_files",
    "unsafe_runtime_import",
    "invalid_universe_metadata",
    "source_identity_failure",
    "duplicate_identity",
]

WARNING_CODES: tuple[WarningCode, ...] = (
    "malformed_frontmatter",
    "unsupported_api_version",
    "invalid_parameter_schema",
    "invalid_defaults",
    "missing_runtime_entrypoint",
    "invalid_runtime_files",
    "unsafe_runtime_import",
    "invalid_universe_metadata",
    "source_identity_failure",
    "duplicate_identity",
)

#: Stable rejection reasons every ``invalid_runtime_files``,
#: ``unsafe_runtime_import``, and ``invalid_universe_metadata`` warning
#: message starts with, so a caller (or a test) can branch on the precise
#: rule broken without matching on prose.
RejectionReason = Literal[
    "missing_runtime_files",
    "malformed_runtime_files",
    "duplicate_runtime_file",
    "undeclared_entrypoint",
    "runtime_file_escapes_skill",
    "runtime_file_symlink",
    "missing_runtime_file",
    "unparsable_runtime_file",
    "forbidden_import",
    "import_outside_runtime_allowlist",
    "undeclared_runtime_import",
    "relative_import_escape",
    "dynamic_import",
    "missing_universe_metadata",
    "malformed_universe_metadata",
    "unsupported_universe_schema",
    "unsupported_universe_mode",
    "empty_universe_parameter",
    "universe_parameter_conflict",
]

REJECTION_REASONS: tuple[RejectionReason, ...] = (
    "missing_runtime_files",
    "malformed_runtime_files",
    "duplicate_runtime_file",
    "undeclared_entrypoint",
    "runtime_file_escapes_skill",
    "runtime_file_symlink",
    "missing_runtime_file",
    "unparsable_runtime_file",
    "forbidden_import",
    "import_outside_runtime_allowlist",
    "undeclared_runtime_import",
    "relative_import_escape",
    "dynamic_import",
    "missing_universe_metadata",
    "malformed_universe_metadata",
    "unsupported_universe_schema",
    "unsupported_universe_mode",
    "empty_universe_parameter",
    "universe_parameter_conflict",
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class _DiscoveryModel(BaseModel):
    """Frozen, strict, extra-forbidding base for every discovery result.

    Mirrors ``strategy_protocol._StrategyModel``'s immutability convention
    without importing across modules -- each module in this package
    defines its own private base, matching ``source_manifest._ManifestModel``
    and ``historical_scan_record.CanonicalModel``'s existing precedent.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class StrategyDiscoveryWarningV1(_DiscoveryModel):
    """One folder isolated from discovery, with safe, structured context.

    Never carries a traceback or arbitrary file content -- only the
    folder name, a stable :data:`WarningCode`, a human-readable message,
    and (when applicable) the specific frontmatter field at fault.
    """

    folder: str = Field(min_length=1)
    code: WarningCode
    message: str = Field(min_length=1)
    field: str | None = None


class StrategyUniverseContractV1(_DiscoveryModel):
    """One Skill's declared ``strategy_universe.v1`` binding.

    ``parameter`` names the single host-bound parameter the canonical
    selected-security tuple is injected into. It is deliberately absent
    from the Strategy's generic ``parameters`` schema, so it has no
    editable or default identity and never reaches generic parameter
    rendering.
    """

    schema_version: Literal["strategy_universe.v1"]
    mode: Literal["selected-securities"]
    parameter: str = Field(min_length=1)


class StrategyDescriptorV1(_DiscoveryModel):
    """One discovered, fully-validated ``kind: backtest-strategy`` Skill.

    ``strategy_id`` is the stable Strategy ID -- always equal to the
    folder name it was discovered under. ``default_parameters`` is the
    normalized mapping :func:`validate_strategy_parameters` returns when
    applying every declared default to an empty submission -- already
    proven runnable, not deferred to launch time. ``runtime_files`` is the
    manifest's ordered allowlist as ``skills_root``-relative POSIX paths
    (the same convention as ``runtime_path``, which is always one of
    them); every one of them participates in ``source_digest``.
    """

    strategy_id: str = Field(min_length=1)
    source_manifest_version: str = Field(min_length=1)
    source_digest: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    api_version: int
    parameters: tuple[StrategyParameterV1, ...]
    default_parameters: Mapping[str, JsonScalar]
    runtime_path: str = Field(min_length=1)
    runtime_files: tuple[str, ...] = Field(min_length=1)
    universe: StrategyUniverseContractV1

    @field_validator("default_parameters", mode="after")
    @classmethod
    def _immutable_defaults(
        cls, value: Mapping[str, JsonScalar]
    ) -> Mapping[str, JsonScalar]:
        return MappingProxyType(dict(value))

    def bind_universe(
        self, selected_security_ids: Iterable[object]
    ) -> Mapping[str, JsonValue]:
        """Return this Strategy's host-bound universe parameter binding.

        The host canonicalizes the selection (sorted, deduplicated) before
        injection, so two selection orders of the same set bind the
        identical value. Raises
        :class:`~app.services.backtest.run_universe.RunUniverseError` for
        an empty selection or a malformed security ID -- the universe
        parameter accepts any non-empty list of unique IDs and has no
        per-Strategy maximum.
        """
        canonical = canonical_run_universe(selected_security_ids)
        return MappingProxyType(
            {self.universe.parameter: cast(JsonValue, list(canonical))}
        )


class StrategyDiscoveryResultV1(_DiscoveryModel):
    """One discovery scan's complete, ordered outcome."""

    strategies: tuple[StrategyDescriptorV1, ...]
    warnings: tuple[StrategyDiscoveryWarningV1, ...]


# ---------------------------------------------------------------------------
# Safe frontmatter YAML loader
# ---------------------------------------------------------------------------


class _FrontmatterYamlError(ValueError):
    """Frontmatter YAML failed to parse safely (alias/anchor/duplicate key)."""


def _reject_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise _FrontmatterYamlError(f"duplicate frontmatter key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


class _StrictFrontmatterLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that additionally rejects aliases and duplicate keys.

    Plain ``yaml.safe_load`` already refuses to construct arbitrary Python
    objects (unlike ``yaml.load`` with the default ``Loader``), but it
    still resolves YAML aliases/anchors (and, transitively, merge keys,
    which are themselves expressed as an alias) and silently lets a
    duplicate mapping key overwrite an earlier one -- exactly the two
    additional restrictions Strategy frontmatter needs. Subclassing
    ``SafeLoader`` (rather than ``yaml.Loader``) keeps every other
    construction rule exactly as safe as ``yaml.safe_load``.
    """

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        if self.check_event(AliasEvent):
            raise _FrontmatterYamlError(
                "YAML aliases/anchors are not permitted in Strategy frontmatter"
            )
        return super().compose_node(parent, index)


_StrictFrontmatterLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG, _reject_duplicate_keys
)


def _load_frontmatter_document(text: str) -> Any:
    """Parse one bounded frontmatter document with the strict safe loader."""
    try:
        return yaml.load(text, Loader=_StrictFrontmatterLoader)
    except yaml.YAMLError as exc:
        raise _FrontmatterYamlError(f"invalid frontmatter YAML: {exc}") from exc


def _extract_frontmatter(text: str) -> str | None:
    """Return the first ``---``-delimited document's raw text, or ``None``.

    ``None`` means "no frontmatter block at all" -- an ordinary Markdown
    file with no opening ``---``, treated the same as a missing
    ``SKILL.md`` (silently skipped). An opening ``---`` with no matching
    closing ``---`` is a different, *malformed* case -- it promised a
    frontmatter block and never finished one -- and raises
    :class:`_FrontmatterYamlError` rather than being silently skipped, the
    same as a second frontmatter-shaped block immediately following the
    first (multi-document frontmatter).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    closing_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            closing_index = index
            break
    if closing_index is None:
        raise _FrontmatterYamlError("frontmatter has no closing '---'")
    if (
        closing_index + 1 < len(lines)
        and lines[closing_index + 1].rstrip("\r\n") == "---"
    ):
        raise _FrontmatterYamlError("multi-document frontmatter is not permitted")
    return "".join(lines[1:closing_index])


# ---------------------------------------------------------------------------
# Per-folder processing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FolderOutcome:
    """One folder's discovery result: at most one of descriptor/warning."""

    folder: str
    descriptor: StrategyDescriptorV1 | None
    warning: StrategyDiscoveryWarningV1 | None


def _warning(
    folder: str, code: WarningCode, message: str, *, field: str | None = None
) -> _FolderOutcome:
    return _FolderOutcome(
        folder=folder,
        descriptor=None,
        warning=StrategyDiscoveryWarningV1(
            folder=folder, code=code, message=message, field=field
        ),
    )


def _skip(folder: str) -> _FolderOutcome:
    return _FolderOutcome(folder=folder, descriptor=None, warning=None)


def _default_display_name(name: str) -> str:
    """Deterministic Title Case fallback derived from a kebab-case name."""
    return " ".join(word.capitalize() for word in name.split("-"))


def _read_frontmatter_text(skill_md: Path) -> str | None | _FrontmatterYamlError:
    """Read+decode ``skill_md`` and return its raw frontmatter text.

    Returns ``None`` for "no frontmatter" (ordinary Skill, silent skip),
    or a :class:`_FrontmatterYamlError` instance (not raised) describing a
    read/size/encoding/shape problem the caller should warn about.
    """
    try:
        raw = skill_md.read_bytes()
    except OSError as exc:
        return _FrontmatterYamlError(f"could not read SKILL.md: {exc}")
    if len(raw) > _MAX_SKILL_MD_BYTES:
        return _FrontmatterYamlError(
            f"SKILL.md file exceeds the {_MAX_SKILL_MD_BYTES}-byte read bound "
            "(checked before frontmatter is parsed, regardless of how much "
            "of the file is actually frontmatter)"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _FrontmatterYamlError(f"SKILL.md is not valid UTF-8: {exc}")
    try:
        return _extract_frontmatter(text)
    except _FrontmatterYamlError as exc:
        return exc


def _parameter_schema_from_frontmatter(
    raw_parameters: object,
) -> tuple[StrategyParameterV1, ...]:
    """Build the ordered parameter schema, or raise ``ValueError``."""
    if raw_parameters is None:
        return ()
    if not isinstance(raw_parameters, list):
        raise ValueError("'parameters' must be a list")
    parameters: list[StrategyParameterV1] = []
    for index, entry in enumerate(raw_parameters):
        if not isinstance(entry, dict):
            raise ValueError(f"parameters[{index}] must be a mapping")
        try:
            parameters.append(StrategyParameterV1(**entry))
        except (ValidationError, TypeError) as exc:
            raise ValueError(f"parameters[{index}] is invalid: {exc}") from exc
    return tuple(parameters)


# ---------------------------------------------------------------------------
# Packaging, import-safety and universe contract validation
# ---------------------------------------------------------------------------


class _RejectedSkill(ValueError):
    """One Skill rejected by a named, stable :data:`RejectionReason`.

    ``str(exc)`` always starts with the reason token, so a warning message
    stays branchable without matching on prose.
    """

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        self.reason: RejectionReason = reason
        super().__init__(f"{reason}: {detail}")


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _declared_runtime_files(raw: object) -> tuple[PurePosixPath, ...]:
    """Return the manifest's ordered ``runtime_files`` allowlist.

    Rejects a missing/empty/non-list declaration, a non-string or
    absolute/traversing/backslash entry, a repeated entry, and an
    allowlist that does not contain :data:`RUNTIME_ENTRYPOINT`.
    """
    if raw is None:
        raise _RejectedSkill(
            "missing_runtime_files",
            "'runtime_files' must declare every file that can change Strategy behavior",
        )
    if not isinstance(raw, list) or not raw:
        raise _RejectedSkill(
            "malformed_runtime_files", "'runtime_files' must be a non-empty list"
        )
    declared: list[PurePosixPath] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise _RejectedSkill(
                "malformed_runtime_files",
                f"runtime_files entry must be a non-empty string, got {entry!r}",
            )
        path = PurePosixPath(entry)
        if "\\" in entry or path.is_absolute() or ".." in path.parts:
            raise _RejectedSkill(
                "runtime_file_escapes_skill",
                f"runtime_files entry must be a Skill-relative path, got {entry!r}",
            )
        if entry in seen:
            raise _RejectedSkill(
                "duplicate_runtime_file", f"runtime_files repeats {entry!r}"
            )
        seen.add(entry)
        declared.append(path)
    if RUNTIME_ENTRYPOINT not in seen:
        raise _RejectedSkill(
            "undeclared_entrypoint",
            f"runtime_files must declare the {RUNTIME_ENTRYPOINT!r} entrypoint",
        )
    return tuple(declared)


def _resolved_runtime_file(skill_dir: Path, relative: PurePosixPath) -> Path:
    """Return ``relative``'s real file inside ``skill_dir``.

    A symlink at any step, a path that resolves outside the Skill, or a
    missing/non-regular file is rejected: only content that provably lives
    inside this Skill may participate in its ``source_digest``.
    """
    current = skill_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _RejectedSkill(
                "runtime_file_symlink",
                f"{relative.as_posix()} resolves through a symlink",
            )
    try:
        current.resolve().relative_to(skill_dir.resolve())
    except (ValueError, OSError) as exc:
        raise _RejectedSkill(
            "runtime_file_escapes_skill",
            f"{relative.as_posix()} resolves outside its Skill folder",
        ) from exc
    if not current.is_file():
        raise _RejectedSkill(
            "missing_runtime_file", f"declared runtime file is missing: {relative}"
        )
    return current


def _check_absolute_import(module: str, relative: PurePosixPath) -> None:
    if _matches_prefix(module, FORBIDDEN_RUNTIME_PREFIXES):
        raise _RejectedSkill(
            "forbidden_import",
            f"{relative.as_posix()} imports forbidden dependency {module!r}",
        )
    if not _matches_prefix(module, ALLOWED_RUNTIME_PREFIXES):
        raise _RejectedSkill(
            "import_outside_runtime_allowlist",
            f"{relative.as_posix()} imports {module!r}, outside the closed "
            "Strategy runtime allowlist",
        )


def _relative_import_base(relative: PurePosixPath, level: int) -> PurePosixPath:
    base = relative.parent
    for _ in range(level - 1):
        if base == PurePosixPath("."):
            raise _RejectedSkill(
                "relative_import_escape",
                f"{relative.as_posix()} imports above its own Skill folder",
            )
        base = base.parent
    return base


def _check_relative_import(
    node: ast.ImportFrom, relative: PurePosixPath, declared: frozenset[str]
) -> None:
    """Require a relative import to name a declared file in this Skill."""
    base = _relative_import_base(relative, node.level)
    if node.module is not None:
        targets = [base.joinpath(*node.module.split("."))]
    else:
        targets = [base / alias.name for alias in node.names]
    for target in targets:
        candidates = (f"{target.as_posix()}.py", f"{target}/__init__.py")
        if not any(candidate in declared for candidate in candidates):
            raise _RejectedSkill(
                "undeclared_runtime_import",
                f"{relative.as_posix()} imports {target.as_posix()!r}, which is "
                "not a declared runtime_files entry",
            )


def _check_dynamic_import(node: ast.Call, relative: PurePosixPath) -> None:
    is_dynamic = (isinstance(node.func, ast.Name) and node.func.id == "__import__") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
    )
    if is_dynamic:
        raise _RejectedSkill(
            "dynamic_import",
            f"{relative.as_posix()} uses a dynamic import; the import graph "
            "must be statically declarable",
        )


def validate_runtime_import_graph(
    *, skill_dir: Path, declared: tuple[PurePosixPath, ...]
) -> None:
    """Statically walk one Skill's declared runtime files' import graph.

    Production counterpart of AD-10's import boundary: every declared
    ``*.py`` file is AST-parsed (never imported or executed) and must
    import only :data:`ALLOWED_RUNTIME_PREFIXES` absolutely, resolve every
    relative import to another declared file inside the same Skill, and
    never use a dynamic import. Raises :class:`_RejectedSkill` naming the
    stable reason for the first violation found.
    """
    declared_posix = frozenset(path.as_posix() for path in declared)
    for relative in declared:
        if relative.suffix != ".py":
            continue
        path = _resolved_runtime_file(skill_dir, relative)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _RejectedSkill(
                "unparsable_runtime_file", f"{relative.as_posix()} is unreadable"
            ) from exc
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise _RejectedSkill(
                "unparsable_runtime_file", f"{relative.as_posix()} does not parse"
            ) from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _check_absolute_import(alias.name, relative)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module is not None:
                        _check_absolute_import(node.module, relative)
                else:
                    _check_relative_import(node, relative, declared_posix)
            elif isinstance(node, ast.Call):
                _check_dynamic_import(node, relative)


def _universe_contract(
    raw: object, parameter_names: frozenset[str]
) -> StrategyUniverseContractV1:
    """Validate the manifest's ``strategy_universe`` block.

    The declared parameter must not also appear in the generic
    ``parameters`` schema: it is host-bound, so a second declaration would
    give it a conflicting editable/default identity.
    """
    if raw is None:
        raise _RejectedSkill(
            "missing_universe_metadata",
            f"'strategy_universe' must declare {STRATEGY_UNIVERSE_VERSION}",
        )
    if not isinstance(raw, dict):
        raise _RejectedSkill(
            "malformed_universe_metadata", "'strategy_universe' must be a mapping"
        )
    unknown = sorted(set(raw) - {"schema_version", "mode", "parameter"})
    if unknown:
        raise _RejectedSkill(
            "malformed_universe_metadata",
            f"'strategy_universe' declares unknown keys: {unknown}",
        )
    if raw.get("schema_version") != STRATEGY_UNIVERSE_VERSION:
        raise _RejectedSkill(
            "unsupported_universe_schema",
            f"'strategy_universe.schema_version' must be "
            f"{STRATEGY_UNIVERSE_VERSION!r}, got {raw.get('schema_version')!r}",
        )
    if raw.get("mode") != SELECTED_SECURITIES_MODE:
        raise _RejectedSkill(
            "unsupported_universe_mode",
            f"'strategy_universe.mode' must be {SELECTED_SECURITIES_MODE!r}, "
            f"got {raw.get('mode')!r}",
        )
    parameter = raw.get("parameter")
    if (
        not isinstance(parameter, str)
        or not parameter
        or parameter.strip() != parameter
    ):
        raise _RejectedSkill(
            "empty_universe_parameter",
            "'strategy_universe.parameter' must name one unpadded, non-empty "
            f"host-bound parameter, got {parameter!r}",
        )
    if parameter in parameter_names:
        raise _RejectedSkill(
            "universe_parameter_conflict",
            f"{parameter!r} is host-bound and must not also be declared in "
            "'parameters'",
        )
    return StrategyUniverseContractV1(
        schema_version=STRATEGY_UNIVERSE_VERSION,
        mode=SELECTED_SECURITIES_MODE,
        parameter=parameter,
    )


def _process_folder(folder: Path, skills_root: Path) -> _FolderOutcome:
    """Discover at most one Strategy from ``folder``, never raising."""
    name = folder.name
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return _skip(name)

    frontmatter_text = _read_frontmatter_text(skill_md)
    if isinstance(frontmatter_text, _FrontmatterYamlError):
        return _warning(name, "malformed_frontmatter", str(frontmatter_text))
    if frontmatter_text is None:
        return _skip(name)

    try:
        parsed = _load_frontmatter_document(frontmatter_text)
    except _FrontmatterYamlError as exc:
        return _warning(name, "malformed_frontmatter", str(exc))

    if parsed is None:
        # An empty (but well-formed) frontmatter block, e.g. ``---\n---``,
        # parses to ``None`` via ``yaml.load``. Treat it the same as an
        # empty mapping -- no ``kind`` key present -- rather than warning:
        # an ordinary Skill's frontmatter being accidentally empty is not
        # this module's problem to flag.
        parsed = {}
    if not isinstance(parsed, dict):
        return _warning(name, "malformed_frontmatter", "frontmatter is not a mapping")

    if parsed.get("kind") != _STRATEGY_KIND:
        return _skip(name)

    # From here on, every problem is a warning -- ``kind`` is declared.
    declared_name = parsed.get("name")
    if (
        not isinstance(declared_name, str)
        or not _KEBAB_CASE_RE.match(declared_name)
        or declared_name != name
    ):
        return _warning(
            name,
            "malformed_frontmatter",
            f"'name' must be lowercase kebab-case and equal the folder "
            f"name {name!r}, got {declared_name!r}",
            field="name",
        )

    description = parsed.get("description")
    if not isinstance(description, str) or not description.strip():
        return _warning(
            name,
            "malformed_frontmatter",
            "'description' must be a non-empty string",
            field="description",
        )

    display_name_raw = parsed.get("display_name")
    if display_name_raw is not None and (
        not isinstance(display_name_raw, str) or not display_name_raw.strip()
    ):
        return _warning(
            name,
            "malformed_frontmatter",
            "'display_name' must be a non-empty string when present",
            field="display_name",
        )
    display_name = display_name_raw or _default_display_name(name)

    api_version = parsed.get("api_version")
    if (
        isinstance(api_version, bool)
        or not isinstance(api_version, int)
        or api_version != _SUPPORTED_API_VERSION
    ):
        return _warning(
            name,
            "unsupported_api_version",
            f"'api_version' must be the integer {_SUPPORTED_API_VERSION}, "
            f"got {api_version!r}",
            field="api_version",
        )

    try:
        schema = _parameter_schema_from_frontmatter(parsed.get("parameters"))
    except ValueError as exc:
        return _warning(name, "invalid_parameter_schema", str(exc), field="parameters")

    if parsed.get("kind") == _STRATEGY_KIND:
        # Inject the opt-in regime-filter parameters exactly once, before
        # validation. A SKILL.md that also declares one of these reserved
        # names is rejected here rather than silently shadowed.
        declared = {parameter.name for parameter in schema}
        clash = declared.intersection(
            parameter.name for parameter in COMMON_BACKTEST_STRATEGY_PARAMETERS
        )
        if clash:
            return _warning(
                name,
                "invalid_parameter_schema",
                f"'parameters' redeclares reserved regime-filter name(s): "
                f"{', '.join(sorted(clash))}",
                field="parameters",
            )
        schema = schema + COMMON_BACKTEST_STRATEGY_PARAMETERS

    try:
        validated_defaults = validate_strategy_parameters(
            schema, {}, apply_defaults=True
        )
    except StrategyProtocolError as exc:
        return _warning(name, "invalid_parameter_schema", str(exc), field="parameters")
    if isinstance(validated_defaults, tuple):
        detail = "; ".join(
            f"{error.parameter_name}: {error.code.value}"
            for error in validated_defaults
        )
        return _warning(
            name,
            "invalid_defaults",
            f"declared defaults fail validation: {detail}",
            field="parameters",
        )

    runtime_relative = f"{name}/{RUNTIME_ENTRYPOINT}"
    runtime_path = folder / RUNTIME_ENTRYPOINT
    if not runtime_path.is_file():
        return _warning(
            name,
            "missing_runtime_entrypoint",
            f"runtime entrypoint is missing: {runtime_relative}",
            field=RUNTIME_ENTRYPOINT,
        )

    try:
        declared_files = _declared_runtime_files(parsed.get("runtime_files"))
        for relative in declared_files:
            _resolved_runtime_file(folder, relative)
    except _RejectedSkill as exc:
        return _warning(name, "invalid_runtime_files", str(exc), field="runtime_files")

    try:
        universe = _universe_contract(
            parsed.get("strategy_universe"),
            frozenset(parameter.name for parameter in schema),
        )
    except _RejectedSkill as exc:
        return _warning(
            name, "invalid_universe_metadata", str(exc), field="strategy_universe"
        )

    try:
        validate_runtime_import_graph(skill_dir=folder, declared=declared_files)
    except _RejectedSkill as exc:
        return _warning(name, "unsafe_runtime_import", str(exc), field="runtime_files")

    runtime_files = tuple(f"{name}/{path.as_posix()}" for path in declared_files)
    try:
        manifest_artifact = build_strategy_source_manifest(
            project_root=skills_root,
            strategy_id=name,
            api_version=api_version,
            allowlist=runtime_files,
            defaults=dict(validated_defaults),
            python_runtime=f"{sys.version_info.major}.{sys.version_info.minor}",
            dependency_versions={"pydantic": _installed_pydantic_version()},
        )
    except ValueError as exc:
        return _warning(name, "source_identity_failure", str(exc))

    descriptor = StrategyDescriptorV1(
        strategy_id=name,
        source_manifest_version=SOURCE_MANIFEST_VERSION,
        source_digest=manifest_artifact.digest,
        display_name=display_name,
        description=description,
        api_version=api_version,
        parameters=schema,
        # ``validate_strategy_parameters`` is typed for the general
        # ``JsonValue`` a future non-scalar parameter could carry, but
        # ``ParameterType`` is closed to scalar shapes only, so every
        # value it returns here is actually a ``JsonScalar``.
        default_parameters=cast(Mapping[str, JsonScalar], dict(validated_defaults)),
        runtime_path=runtime_relative,
        runtime_files=runtime_files,
        universe=universe,
    )
    return _FolderOutcome(folder=name, descriptor=descriptor, warning=None)


def _installed_pydantic_version() -> str:
    try:
        return version("pydantic")
    except PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _filesystem_revision(root: Path) -> str | None:
    """Return a deterministic stat-based revision of ``root``'s tree.

    A directory's mtime catches child additions/removals; each descendant's
    mode, size, and nanosecond mtime catch normal edits.  Symlinks are
    fingerprinted but never followed, preserving discovery's isolation
    boundary.  ``None`` means the tree could not be inspected safely.
    """
    digest = hashlib.sha256()

    def add_entry(path: Path) -> None:
        stat = path.lstat()
        relative = path.relative_to(root).as_posix()
        digest.update(
            f"{relative}\0{stat.st_dev}\0{stat.st_ino}\0{stat.st_mode}\0"
            f"{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
        )

    def visit(directory: Path) -> None:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
        for entry in entries:
            add_entry(entry)
            if entry.is_dir() and not entry.is_symlink():
                visit(entry)

    try:
        add_entry(root)
        visit(root)
    except OSError:
        return None
    return digest.hexdigest()


@lru_cache(maxsize=32)
def _discover_strategies_for_revision(
    resolved_root: str, revision: str
) -> StrategyDiscoveryResultV1:
    """Compute one immutable discovery result for an observed revision."""
    del revision  # The key, not the scan algorithm, supplies cache identity.
    return _discover_strategies_uncached(Path(resolved_root))


def _discover_strategies_uncached(root: Path) -> StrategyDiscoveryResultV1:
    """Perform the fail-soft scan after the caller has established its key."""
    # The revision has already been obtained from this resolved directory.
    # Keep this guard for direct callers and an unlikely removal race.
    if not root.is_dir():
        return StrategyDiscoveryResultV1(strategies=(), warnings=())

    try:
        candidate_folders = sorted(
            (child for child in root.iterdir() if child.is_dir()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    except OSError:
        # skills_root itself couldn't be listed (e.g. a permission error) --
        # degrade to "nothing discovered" rather than raising out of a
        # module whose whole contract is failing soft.
        return StrategyDiscoveryResultV1(strategies=(), warnings=())

    outcomes: list[_FolderOutcome] = []
    for folder in candidate_folders:
        try:
            resolved = folder.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            # ValueError: resolves outside skills_root (symlink escape).
            # OSError: couldn't be resolved at all (e.g. a broken/circular
            # symlink). Either way, isolate by silently skipping rather
            # than letting it abort the whole scan.
            continue
        try:
            outcomes.append(_process_folder(folder, root))
        except Exception as exc:  # noqa: BLE001 -- isolate, never abort the scan
            outcomes.append(_warning(folder.name, "malformed_frontmatter", str(exc)))

    warnings: list[StrategyDiscoveryWarningV1] = [
        outcome.warning for outcome in outcomes if outcome.warning is not None
    ]
    candidates: list[tuple[str, StrategyDescriptorV1]] = [
        (outcome.folder, outcome.descriptor)
        for outcome in outcomes
        if outcome.descriptor is not None
    ]

    # ``by_id`` can never actually collide: ``_process_folder`` only
    # produces a descriptor when the declared ``name`` equals its own
    # folder name, and a directory can't yield two entries sharing one
    # name, so ``strategy_id`` is unique across ``candidates`` by
    # construction. Kept anyway as an explicit, cheap invariant check
    # rather than relying on that reasoning holding forever -- the
    # collision surface that can actually fire is ``by_display``.
    by_id: dict[str, list[str]] = {}
    by_display: dict[str, list[str]] = {}
    for folder, descriptor in candidates:
        by_id.setdefault(descriptor.strategy_id, []).append(folder)
        canonical_display = descriptor.display_name.strip().casefold()
        by_display.setdefault(canonical_display, []).append(folder)

    conflicted: set[str] = set()
    for group in (*by_id.values(), *by_display.values()):
        if len(group) > 1:
            conflicted.update(group)

    valid_descriptors: list[StrategyDescriptorV1] = []
    for folder, descriptor in candidates:
        if folder in conflicted:
            warnings.append(
                StrategyDiscoveryWarningV1(
                    folder=folder,
                    code="duplicate_identity",
                    message=(
                        f"strategy_id or display identity for {folder!r} "
                        "collides with another discovered Strategy"
                    ),
                )
            )
        else:
            valid_descriptors.append(descriptor)

    warnings.sort(key=lambda warning: warning.folder)

    return StrategyDiscoveryResultV1(
        strategies=tuple(valid_descriptors),
        warnings=tuple(warnings),
    )


def discover_strategies(skills_root: Path) -> StrategyDiscoveryResultV1:
    """Scan ``skills_root``'s immediate child folders for Strategy Skills.

    Reads a recursive filesystem revision on every call, then reuses the
    process-local immutable result for an unchanged root/revision pair.
    Returns every valid :class:`StrategyDescriptorV1` and every
    :class:`StrategyDiscoveryWarningV1`, both in one deterministic order
    (folders sorted by their normalized POSIX-relative path). One
    malformed Skill never aborts the scan: a folder whose ``SKILL.md`` has
    no ``kind: backtest-strategy`` is silently skipped, and a folder that
    declares that kind but is otherwise invalid is isolated with a
    warning naming the folder and, where applicable, the offending field.

    Never imports or executes a discovered Strategy's
    ``scripts/strategy.py`` -- discovery is metadata-only.
    """
    root = skills_root.resolve()
    revision = _filesystem_revision(root) if root.is_dir() else None
    if revision is None:
        return StrategyDiscoveryResultV1(strategies=(), warnings=())
    return _discover_strategies_for_revision(str(root), revision)


__all__ = [
    "ALLOWED_RUNTIME_PREFIXES",
    "FORBIDDEN_RUNTIME_PREFIXES",
    "REJECTION_REASONS",
    "RUNTIME_ENTRYPOINT",
    "RejectionReason",
    "SELECTED_SECURITIES_MODE",
    "STRATEGY_UNIVERSE_VERSION",
    "StrategyDescriptorV1",
    "StrategyDiscoveryResultV1",
    "StrategyDiscoveryWarningV1",
    "StrategyUniverseContractV1",
    "WARNING_CODES",
    "WarningCode",
    "discover_strategies",
    "validate_runtime_import_graph",
]
