"""Fail-soft ``skills/`` discovery for ``kind: backtest-strategy`` Skills.

Scans the immediate child folders of an injected root ``Path`` for a
``SKILL.md`` whose frontmatter declares ``kind: backtest-strategy``, and
turns each one into an immutable :class:`StrategyDescriptorV1` -- the
metadata seam a future Backtest launch (Story 2.3) and UI (Story 2.7)
consume without any hand-maintained Strategy registry list.

Design constraints, all deliberate:

- **Metadata-only.** :func:`discover_strategies` never imports or executes
  a discovered Strategy's ``scripts/strategy.py`` -- only its ``SKILL.md``
  frontmatter is read, and only to build a source-identity manifest via
  ``build_strategy_source_manifest`` (never a second canonicalizer).
- **Ordinary Skills are invisible.** A folder with no ``SKILL.md``, or
  whose ``SKILL.md`` frontmatter has no ``kind: backtest-strategy`` line
  at all, is silently skipped -- no descriptor, no warning. Every other
  Skill in this repository (``skills/vcp-screener/``, and friends) must
  keep discovering as nothing.
- **One bad Strategy never aborts the scan.** Once a folder's frontmatter
  *does* declare ``kind: backtest-strategy``, any problem with it --
  malformed metadata, an unsupported ``api_version``, an invalid
  parameter schema, an out-of-range declared default, a missing runtime
  entrypoint, an unsafe YAML construct, or a colliding identity -- isolates
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
- **No import-time singleton.** :func:`discover_strategies` re-reads the
  filesystem on every call; nothing here is cached across calls, so a
  caller always sees the current state of ``skills_root``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
import yaml
from yaml.events import AliasEvent
from yaml.resolver import BaseResolver

from app.services.backtest.source_manifest import (
    SOURCE_MANIFEST_VERSION,
    build_strategy_source_manifest,
)
from app.services.backtest.strategy_protocol import (
    JsonScalar,
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

#: Closed, stable warning-code vocabulary a caller can branch on -- see the
#: module docstring for what triggers each one.
WarningCode = Literal[
    "malformed_frontmatter",
    "unsupported_api_version",
    "invalid_parameter_schema",
    "invalid_defaults",
    "missing_runtime_entrypoint",
    "source_identity_failure",
    "duplicate_identity",
]

WARNING_CODES: tuple[WarningCode, ...] = (
    "malformed_frontmatter",
    "unsupported_api_version",
    "invalid_parameter_schema",
    "invalid_defaults",
    "missing_runtime_entrypoint",
    "source_identity_failure",
    "duplicate_identity",
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


class StrategyDescriptorV1(_DiscoveryModel):
    """One discovered, fully-validated ``kind: backtest-strategy`` Skill.

    ``strategy_id`` is the stable Strategy ID -- always equal to the
    folder name it was discovered under. ``default_parameters`` is the
    normalized mapping :func:`validate_strategy_parameters` returns when
    applying every declared default to an empty submission -- already
    proven runnable, not deferred to launch time.
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

    @field_validator("default_parameters", mode="after")
    @classmethod
    def _immutable_defaults(
        cls, value: Mapping[str, JsonScalar]
    ) -> Mapping[str, JsonScalar]:
        return MappingProxyType(dict(value))


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

    runtime_relative = f"{name}/scripts/strategy.py"
    runtime_path = skills_root / name / "scripts" / "strategy.py"
    if not runtime_path.is_file():
        return _warning(
            name,
            "missing_runtime_entrypoint",
            f"runtime entrypoint is missing: {runtime_relative}",
            field="scripts/strategy.py",
        )

    try:
        manifest_artifact = build_strategy_source_manifest(
            project_root=skills_root,
            strategy_id=name,
            api_version=api_version,
            allowlist=(runtime_relative,),
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


def discover_strategies(skills_root: Path) -> StrategyDiscoveryResultV1:
    """Scan ``skills_root``'s immediate child folders for Strategy Skills.

    Re-reads the filesystem on every call -- no import-time singleton, no
    caching. Returns every valid :class:`StrategyDescriptorV1` and every
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


__all__ = [
    "StrategyDescriptorV1",
    "StrategyDiscoveryResultV1",
    "StrategyDiscoveryWarningV1",
    "WARNING_CODES",
    "WarningCode",
    "discover_strategies",
]
