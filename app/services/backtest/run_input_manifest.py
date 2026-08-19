"""``RunInputManifestV1`` -- one Backtest Run's complete replay identity.

Mirrors ``source_manifest.ReconstructionInputManifestV1``'s
``canonical_payload()``/``canonical_json()``/``digest()`` pattern exactly
(AD-19/AD-20): a frozen, strict, ``extra="forbid"`` pydantic model whose
canonical payload deterministically sorts every nested collection before
handing it to ``canonical_manifest``'s shared canonicalizer/digest
functions -- never a second canonicalizer.

Two digests exist for two different questions:

- :meth:`RunInputManifestV1.digest` (via :meth:`canonical_payload`) answers
  "replay this exact Run from cache alone" -- it covers everything,
  including the pinned Strategy source, validated parameters, starting
  capital, and every exact per-security price/action/FX revision.
- :meth:`RunInputManifestV1.execution_contract_digest` (via
  :meth:`execution_contract_payload`) answers "are two Runs' *engine
  semantics* comparable" (AD-19's ``is_comparable``) -- a deliberately
  narrower subset that excludes Strategy identity, parameters, capital,
  and evidence revisions, so two Runs of different Strategies (or the
  same Strategy re-run against newer evidence) can still compare cleanly
  when the engine/protocol/view/fill/ledger/action/metrics/numeric/
  rounding/runtime-lock/calendar semantics behind them are identical.

:func:`build_run_input_manifest` is the one pure, read-only builder that
resolves and pins everything: it never writes a job/run/staging row (Story
2.6 owns lifecycle mutation) and never refetches or substitutes a newer
revision for evidence a caller already pinned -- a revision that no longer
resolves fails with ``historical_price_repo.EvidenceMissingError``
(``evidence_missing``) unmodified.
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
from typing import Annotated, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.repositories.backtest_repo import BacktestRepository
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.historical_price_repo import (
    EvidenceMissingError,
    HistoricalPriceRepository,
)
from app.services.backtest.canonical_manifest import (
    canonical_json as shared_canonical_json,
    manifest_digest,
)
from app.services.backtest.detectors import DETECTOR_REGISTRY
from app.services.backtest.historical_scan_record import DetectorId, FrozenDict
from app.services.backtest.market_planes import PRICE_VOLUME_PLANE_VERSION
from app.services.backtest.skill_discovery import StrategyDescriptorV1
from app.services.backtest.source_manifest import (
    SourceManifestArtifact,
    build_source_manifest,
    detector_source_manifests,
)
from app.services.backtest.strategy_protocol import (
    JsonValue,
    validate_strategy_parameters,
)
from app.services.backtest.trading_calendar import TradingCalendar

RUN_INPUT_MANIFEST_VERSION = "run_input_manifest.v1"

#: Story 2.4's Backtest Engine and this protocol schema do not exist as
#: versioned, hashable source yet -- these are stable semantic-version
#: literals (matching ``RECONSTRUCTION_INPUT_MANIFEST_VERSION``'s literal-
#: version convention), not source-code digests. Bump either the moment its
#: real implementation lands or its behavior changes.
ENGINE_VERSION = "backtest_engine.v1"
PROTOCOL_SCHEMA_VERSION = "strategy_protocol.v1"

#: Fill/ledger/action/``metrics.py`` (Story 2.4) do not exist as source
#: files yet, so there is nothing to hash. This policy version is a
#: deliberate, deterministic placeholder identity for that not-yet-built
#: semantic surface -- Story 2.4 replaces :func:`_ledger_action_metrics_digest`
#: with a genuine ``build_source_manifest`` digest over the real files once
#: they exist; bumping this version string beforehand is enough to
#: invalidate any earlier ``execution_contract_digest`` comparison once
#: real fill/ledger/action/metrics semantics land.
LEDGER_ACTION_METRICS_POLICY_VERSION = "ledger_action_metrics.v1"

#: The concrete files behind ``MarketView``'s Strategy-facing contract.
#: Deliberately narrower than ``backtest_repo.py``'s whole surface (most of
#: which is job/roster/snapshot-commit machinery unrelated to what a
#: Strategy can observe through a bound view) -- an unrelated change
#: elsewhere in that file must never bump this identity.
_MARKET_VIEW_ALLOWLIST = (
    "app/services/backtest/market_view.py",
    "app/services/backtest/market_planes.py",
    "app/services/backtest/strategy_protocol.py",
    "app/services/backtest/historical_scan_record.py",
    "app/repositories/historical_price_repo.py",
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
SnapshotMonth = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class RunInputManifestError(ValueError):
    """A stable, machine-readable failure resolving/binding a Run's inputs.

    Reserved for genuine launch-configuration problems this builder is
    the authority on (no active profile, incomplete interval coverage,
    invalid submitted parameters, evidence that resolves but belongs to
    the wrong security). Never used for a previously pinned revision that
    no longer resolves at all -- that is
    ``historical_price_repo.EvidenceMissingError`` (``evidence_missing``),
    reused verbatim rather than reclassified.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _RunInputModel(BaseModel):
    """Frozen, strict, extra-forbidding base, matching ``_ManifestModel``/
    ``_StrategyModel``'s established immutability convention."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class DetectorSourceDigestV1(_RunInputModel):
    """One registered detector's runtime source-code identity."""

    detector_id: DetectorId
    source_digest: Digest


class PinnedSecurityEvidenceV1(_RunInputModel):
    """One security's exact pinned price/action/FX evidence for a Run.

    ``price_revision``/``action_revision`` are usually the same value --
    this codebase's evidence model stores OHLCV rows and corporate
    actions together under one content-addressed ``data_revision`` -- but
    are kept as two named fields (matching this story's design notes)
    rather than collapsed into one, so a future evidence model that does
    separate them needs no shape change here. ``fx_revision`` is ``None``
    exactly when the security's native quote currency already equals the
    Run's ``base_currency`` (no conversion, hence no FX evidence to pin)
    -- mirroring ``currency.CurrencyConversion.fx_revision``'s own
    same-currency ``None`` convention.
    """

    security_id: NonEmpty
    price_revision: Digest
    action_revision: Digest
    fx_revision: Digest | None = None


class RunInputManifestV1(_RunInputModel):
    """One Backtest Run's complete, canonically pinned replay identity.

    Every field is either a stable semantic-version literal, a source-code
    digest, or an exact evidence/coverage identity -- nothing here is
    resolved implicitly at replay time. :meth:`canonical_payload` is the
    one method every serialization (``canonical_json``/``digest``) goes
    through, matching ``ReconstructionInputManifestV1``'s pattern.
    """

    schema_version: Literal["run_input_manifest.v1"]

    # Engine/protocol/view semantics (also in execution_contract_payload).
    engine_version: NonEmpty
    protocol_schema_version: NonEmpty
    market_view_source_digest: Digest
    ledger_action_metrics_digest: Digest
    numeric_rounding_policy: NonEmpty
    runtime_lock_digest: Digest
    calendar_session_table_digest: Digest

    # Runtime identity (full manifest only -- not in execution_contract).
    python_runtime: NonEmpty
    timezone_dataset_version: NonEmpty

    # Strategy identity and validated launch parameters.
    strategy_id: NonEmpty
    strategy_api_version: Annotated[int, Field(ge=1)]
    strategy_source_digest: Digest
    detector_source_digests: tuple[DetectorSourceDigestV1, ...]
    parameters: dict[str, object]

    # Evidence and coverage identity.
    alias_revision: Digest
    securities: tuple[PinnedSecurityEvidenceV1, ...]
    profile_hash: Digest
    start_month: SnapshotMonth
    end_month: SnapshotMonth
    ordered_month_digest: Digest

    # Capital/currency -- v1 supports only GBP/USD (epics.md Story 2.7 UX:
    # "Capital/currency require positive capital and default GBP (or USD)"),
    # not an open ISO 4217 set.
    base_currency: Literal["GBP", "USD"]
    starting_capital: Decimal = Field(gt=Decimal(0))

    @field_validator("detector_source_digests")
    @classmethod
    def _exact_detector_set(
        cls, value: tuple[DetectorSourceDigestV1, ...]
    ) -> tuple[DetectorSourceDigestV1, ...]:
        expected = {detector.detector_id for detector in DETECTOR_REGISTRY}
        if {item.detector_id for item in value} != expected or len(value) != len(
            expected
        ):
            raise ValueError("manifest requires the exact registered detector set")
        return value

    @field_validator("securities")
    @classmethod
    def _unique_nonempty_securities(
        cls, value: tuple[PinnedSecurityEvidenceV1, ...]
    ) -> tuple[PinnedSecurityEvidenceV1, ...]:
        if not value:
            raise ValueError("securities must pin at least one security")
        security_ids = [item.security_id for item in value]
        if len(set(security_ids)) != len(security_ids):
            raise ValueError("securities must not repeat a security_id")
        return value

    @field_validator("parameters")
    @classmethod
    def _immutable_parameters(cls, value: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], FrozenDict(value))

    @model_validator(mode="after")
    def _normalized_month_range(self) -> "RunInputManifestV1":
        if self.start_month > self.end_month:
            raise ValueError("start_month must not be after end_month")
        return self

    def canonical_payload(self) -> dict[str, object]:
        """Return the flat, sorted mapping every digest is computed over.

        Nested collections are rebuilt in one deterministic sort order
        (by ``detector_id``/``security_id``) regardless of the order a
        caller supplied them in, and ``starting_capital`` is rendered as
        its exact decimal string -- ``canonical_manifest.jsonable`` has
        no ``Decimal`` case, matching every other AD-6 consumer's
        explicit ``str(Decimal(...))`` convention at the JSON boundary.
        """
        payload = self.model_dump(
            mode="python",
            exclude={"detector_source_digests", "securities", "starting_capital"},
        )
        payload["detector_source_digests"] = [
            detector.model_dump(mode="python")
            for detector in sorted(
                self.detector_source_digests, key=lambda item: item.detector_id
            )
        ]
        payload["securities"] = [
            security.model_dump(mode="python")
            for security in sorted(self.securities, key=lambda item: item.security_id)
        ]
        payload["starting_capital"] = str(self.starting_capital)
        return payload

    def canonical_json(self) -> str:
        return shared_canonical_json(self.canonical_payload())

    def digest(self) -> str:
        """The full cache-only replay identity -- see module docstring."""
        return manifest_digest(self.canonical_payload())

    def execution_contract_payload(self) -> dict[str, object]:
        """AD-20's narrower comparison-eligibility subset (AD-19).

        Deliberately excludes Strategy source, parameters, capital, and
        every per-security evidence revision -- only engine/protocol/
        view/fill/ledger/action/metrics/numeric/rounding/runtime-lock/
        calendar-session-table semantics remain, so ``is_comparable`` can
        require this digest's equality while still allowing Strategy,
        parameter, or evidence differences between two Runs.
        """
        return {
            "engine_version": self.engine_version,
            "protocol_schema_version": self.protocol_schema_version,
            "market_view_source_digest": self.market_view_source_digest,
            "ledger_action_metrics_digest": self.ledger_action_metrics_digest,
            "numeric_rounding_policy": self.numeric_rounding_policy,
            "runtime_lock_digest": self.runtime_lock_digest,
            "calendar_session_table_digest": self.calendar_session_table_digest,
        }

    def execution_contract_digest(self) -> str:
        return manifest_digest(self.execution_contract_payload())


def _installed_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError as exc:
        raise ValueError(f"runtime dependency is unavailable: {package}") from exc


def _python_runtime() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _timezone_dataset_version() -> str:
    """Return the installed IANA ``tzdata`` package version, if any.

    Not every platform installs timezone data as a pip package (macOS/
    Linux commonly rely on the system database instead), so a missing
    package is a legitimate, stable identity -- ``"system"`` -- not an
    error.
    """
    try:
        return version("tzdata")
    except PackageNotFoundError:
        return "system"


def _runtime_lock_digest(project_root: Path) -> str:
    """SHA-256 over ``uv.lock``'s newline-normalized UTF-8 bytes.

    Mirrors ``source_manifest._normalized_source_digest``'s newline
    normalization exactly, applied to the one file that pins every
    dependency's exact resolved version.
    """
    lock_path = project_root.resolve() / "uv.lock"
    try:
        text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("runtime lock file is unreadable: uv.lock") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _ledger_action_metrics_digest() -> str:
    """Deterministic placeholder identity -- see
    :data:`LEDGER_ACTION_METRICS_POLICY_VERSION`."""
    return manifest_digest(
        {
            "schema_version": "ledger_action_metrics_policy.v1",
            "policy_version": LEDGER_ACTION_METRICS_POLICY_VERSION,
        }
    )


def _market_view_source_manifest(project_root: Path) -> SourceManifestArtifact:
    return build_source_manifest(
        project_root=project_root,
        producer_id="market_view",
        api_version="1",
        allowlist=_MARKET_VIEW_ALLOWLIST,
        defaults={},
        python_runtime=_python_runtime(),
        dependency_versions={
            "pandas": _installed_version("pandas"),
            "pydantic": _installed_version("pydantic"),
        },
    )


def _detector_source_digests(project_root: Path) -> tuple[DetectorSourceDigestV1, ...]:
    manifests = detector_source_manifests(project_root)
    return tuple(
        DetectorSourceDigestV1(
            detector_id=detector.detector_id,
            source_digest=manifests[detector.detector_id].digest,
        )
        for detector in DETECTOR_REGISTRY
    )


def _verify_pinned_evidence(
    historical_price_repo: HistoricalPriceRepository,
    fx_quote_repo: FxQuoteRepository,
    security: PinnedSecurityEvidenceV1,
    base_currency: str,
) -> None:
    """Verify every pinned revision still resolves and matches its security.

    Lets ``EvidenceMissingError`` propagate unmodified for a price/action
    revision that no longer resolves -- never refetches, never falls back
    to a newer revision. A revision that resolves but belongs to a
    *different* security is a caller-side pinning bug, not a
    missing-evidence replay failure, so that raises
    :class:`RunInputManifestError` instead.

    FX evidence lives in ``FxQuoteRepository`` (content-addressed
    ``fx_quotes``, AD-24), a distinct store from
    ``HistoricalPriceRepository``'s ``historical_price_revisions`` --
    resolved by digest via :meth:`FxQuoteRepository.get_by_digest`, never
    the price/action repository. ``fx_revision`` must be ``None`` exactly
    when the security's own evidence currency already equals
    ``base_currency`` (no conversion needed, per
    ``PinnedSecurityEvidenceV1``'s documented invariant); a mismatch
    either way is a caller-side pinning bug.
    """
    price_evidence = historical_price_repo.get(security.price_revision)
    if price_evidence.security_id != security.security_id:
        raise RunInputManifestError(
            "evidence_mismatch",
            f"pinned price revision does not belong to {security.security_id!r}",
        )
    if security.action_revision != security.price_revision:
        action_evidence = historical_price_repo.get(security.action_revision)
        if action_evidence.security_id != security.security_id:
            raise RunInputManifestError(
                "evidence_mismatch",
                f"pinned action revision does not belong to {security.security_id!r}",
            )
    needs_fx = price_evidence.currency != base_currency
    if needs_fx != (security.fx_revision is not None):
        raise RunInputManifestError(
            "evidence_mismatch",
            f"{security.security_id!r} fx_revision must be set exactly when "
            "its evidence currency differs from base_currency",
        )
    if security.fx_revision is not None:
        if fx_quote_repo.get_by_digest(security.fx_revision) is None:
            raise EvidenceMissingError("pinned FX evidence is missing")


def build_run_input_manifest(
    *,
    project_root: Path,
    backtest_repo: BacktestRepository,
    historical_price_repo: HistoricalPriceRepository,
    fx_quote_repo: FxQuoteRepository,
    strategy: StrategyDescriptorV1,
    submitted_parameters: Mapping[str, JsonValue],
    profile_hash: str | None,
    start_month: str,
    end_month: str,
    base_currency: Literal["GBP", "USD"],
    starting_capital: Decimal,
    securities: tuple[PinnedSecurityEvidenceV1, ...],
) -> RunInputManifestV1:
    """Resolve, verify, and canonically bind one Run's complete input identity.

    Pure and read-only against ``backtest_repo``/``historical_price_repo``/
    ``fx_quote_repo`` -- resolves the active snapshot profile only when
    ``profile_hash`` is
    omitted, requires ``[start_month, end_month]`` to already be fully
    Ready coverage, validates ``submitted_parameters`` against
    ``strategy.parameters`` (applying declared defaults, the same
    authority Skill discovery and a future UI both reuse), and verifies
    every caller-pinned price/action/FX revision in ``securities`` still
    resolves to that exact security -- never re-resolving *which*
    revision to use. No job/run/staging row is written; Story 2.6 owns
    that lifecycle mutation.
    """
    if not securities:
        raise RunInputManifestError(
            "invalid_securities", "securities must pin at least one security"
        )
    security_ids = [security.security_id for security in securities]
    if len(set(security_ids)) != len(security_ids):
        raise RunInputManifestError(
            "invalid_securities", "securities must not repeat a security_id"
        )

    resolved_profile_hash = profile_hash
    if resolved_profile_hash is None:
        active = backtest_repo.active_snapshot_profile()
        if active is None:
            raise RunInputManifestError(
                "no_active_profile", "no active snapshot profile is set"
            )
        resolved_profile_hash = active.profile_hash

    profile = backtest_repo.snapshot_profile(resolved_profile_hash)
    if profile is None:
        raise RunInputManifestError(
            "unknown_profile",
            f"snapshot profile does not exist: {resolved_profile_hash}",
        )

    readiness = backtest_repo.interval_readiness(
        resolved_profile_hash, start_month, end_month
    )
    if not readiness.ready or readiness.ordered_month_digest is None:
        raise RunInputManifestError(
            "coverage_incomplete",
            f"snapshot coverage for {start_month}..{end_month} is not Ready "
            f"(missing months: {readiness.missing_months!r})",
        )
    ordered_month_digest = readiness.ordered_month_digest

    alias_revision = backtest_repo.roster_alias_revision(profile.roster_digest)
    if alias_revision is None:
        raise RunInputManifestError(
            "unknown_roster", f"roster does not exist: {profile.roster_digest}"
        )

    validated = validate_strategy_parameters(
        strategy.parameters, submitted_parameters, apply_defaults=True
    )
    if isinstance(validated, tuple):
        detail = "; ".join(
            f"{error.parameter_name}: {error.code.value}" for error in validated
        )
        raise RunInputManifestError(
            "invalid_parameters", f"submitted parameters are invalid: {detail}"
        )

    for security in securities:
        _verify_pinned_evidence(
            historical_price_repo, fx_quote_repo, security, base_currency
        )

    return RunInputManifestV1(
        schema_version=RUN_INPUT_MANIFEST_VERSION,
        engine_version=ENGINE_VERSION,
        protocol_schema_version=PROTOCOL_SCHEMA_VERSION,
        market_view_source_digest=_market_view_source_manifest(project_root).digest,
        ledger_action_metrics_digest=_ledger_action_metrics_digest(),
        numeric_rounding_policy=PRICE_VOLUME_PLANE_VERSION,
        runtime_lock_digest=_runtime_lock_digest(project_root),
        calendar_session_table_digest=TradingCalendar().session_table_digest(),
        python_runtime=_python_runtime(),
        timezone_dataset_version=_timezone_dataset_version(),
        strategy_id=strategy.strategy_id,
        strategy_api_version=strategy.api_version,
        strategy_source_digest=strategy.source_digest,
        detector_source_digests=_detector_source_digests(project_root),
        parameters=dict(validated),
        alias_revision=alias_revision,
        securities=securities,
        profile_hash=resolved_profile_hash,
        start_month=start_month,
        end_month=end_month,
        ordered_month_digest=ordered_month_digest,
        base_currency=base_currency,
        starting_capital=starting_capital,
    )


__all__ = [
    "DetectorSourceDigestV1",
    "ENGINE_VERSION",
    "LEDGER_ACTION_METRICS_POLICY_VERSION",
    "PROTOCOL_SCHEMA_VERSION",
    "PinnedSecurityEvidenceV1",
    "RUN_INPUT_MANIFEST_VERSION",
    "RunInputManifestError",
    "RunInputManifestV1",
    "build_run_input_manifest",
]
