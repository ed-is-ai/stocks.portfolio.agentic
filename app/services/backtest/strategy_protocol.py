"""Versioned Backtest Strategy interface (AD-20).

Defines ``StrategyProtocolV1`` -- the typed, bounded seam a Strategy author
writes against -- plus the immutable ``Signal``/``PortfolioView`` value
objects and the pure result validators a future engine (Story 2.4) calls
before it changes any cash, position, ledger, staging, or run state.

Story 2.2 adds the closed parameter-schema model
(:class:`StrategyParameterV1`) and the one shared
:func:`validate_strategy_parameters` authority that Skill discovery
(``skill_discovery.py``), a future engine launch, and a future UI all
reuse verbatim -- never a second validator.

Scope boundary: this module owns only the minimum typing surface Stories
2.2-2.4 build on. It does not define the concrete pandas-backed
``MarketView`` (``app.services.backtest.market_view``, Story 2.3),
``SKILL.md`` frontmatter discovery itself (Story 2.2's
``skill_discovery.py`` owns that), or any engine invocation order/state
mutation (Story 2.4). No type here accepts or returns a database
connection, repository, ORM/session, job, run, or persistence model, and
none of them may be imported anywhere near a Strategy runtime module --
that boundary is mechanically enforced by
``tests/backtest/test_strategy_runtime_import_boundary.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
import math
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    import pandas as pd

    from app.services.backtest.historical_scan_record import HistoricalScanRecordV1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StrategyProtocolErrorCode(StrEnum):
    """Stable, machine-readable Strategy protocol validation failure codes."""

    INVALID_SIGNAL_CONTAINER = "invalid_signal_container"
    INVALID_SIGNAL_ELEMENT = "invalid_signal_element"
    INVALID_POSITION_SIZE_TYPE = "invalid_position_size_type"
    NEGATIVE_POSITION_SIZE = "negative_position_size"
    FUTURE_DATED_OBSERVATION = "future_dated_observation"
    DUPLICATE_POSITION = "duplicate_position"
    DUPLICATE_VOLATILITY_OBSERVATION = "duplicate_volatility_observation"
    DUPLICATE_PARAMETER_DECLARATION = "duplicate_parameter_declaration"
    INVALID_INITIAL_SELECTION = "invalid_initial_selection"
    INITIAL_SELECTION_SESSION_MISMATCH = "initial_selection_session_mismatch"
    INITIAL_SELECTION_UNIVERSE_MISMATCH = "initial_selection_universe_mismatch"
    INITIAL_SELECTION_DUPLICATE_SECURITY = "initial_selection_duplicate_security"
    INITIAL_SELECTION_RANK_INVALID = "initial_selection_rank_invalid"
    INITIAL_SELECTION_SIGNAL_MISMATCH = "initial_selection_signal_mismatch"
    INITIAL_SELECTION_PROVIDER_FAILURE = "initial_selection_provider_failure"


class StrategyProtocolError(Exception):
    """One typed protocol exception with a stable, inspectable ``.code``.

    Deliberately not a ``ValueError``/``TypeError``/``AssertionError``
    subclass: pydantic only wraps those three exception types into an
    opaque ``ValidationError`` when raised from inside a validator, which
    would discard ``.code``. A plain ``Exception`` subclass instead
    propagates unmodified from both ``PortfolioView`` construction and the
    pure ``validate_*`` functions below, so callers can always do
    ``except StrategyProtocolError as exc: exc.code``.
    """

    def __init__(self, code: StrategyProtocolErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class _StrategyModel(BaseModel):
    """Frozen, strict, extra-forbidding base for every Story 2.1 value object.

    Mirrors ``historical_scan_record.CanonicalModel``'s immutability
    convention (``extra="forbid"``, ``frozen=True``, ``strict=True``,
    ``allow_inf_nan=False``) without importing that module's detector/BAU
    -envelope domain into the Strategy runtime's approved import graph.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class SignalSide(StrEnum):
    """The two-sided trading action a ``Signal`` requests (AD-20)."""

    BUY = "BUY"
    SELL = "SELL"


#: SELL executes before BUY for the same session/security -- AD-20's
#: execution convention, consumed directly by ``Signal.sort_key``.
_SIDE_EXECUTION_RANK: dict[SignalSide, int] = {
    SignalSide.SELL: 0,
    SignalSide.BUY: 1,
}


class Signal(_StrategyModel):
    """One immutable, deterministically ordered trading instruction.

    AD-20 fixes the shape a future engine acts on: a stable security ID, a
    ``BUY``/``SELL`` side, the session the Strategy raised it on, and a
    non-empty rule ID it can be traced back to. ``sort_key`` is a pure
    function of these fields alone -- never insertion order, hash
    iteration, object identity, or locale -- so validating the same
    signals twice, in any input order, always yields one canonical order.
    """

    security_id: str = Field(min_length=1)
    side: SignalSide
    session: date
    rule_id: str = Field(min_length=1)

    @property
    def sort_key(self) -> tuple[date, str, int, str]:
        """Deterministic ``(session, security_id, side_rank, rule_id)`` key."""
        return (
            self.session,
            self.security_id,
            _SIDE_EXECUTION_RANK[self.side],
            self.rule_id,
        )


class EntrySelectionState(StrEnum):
    """Closed audit vocabulary for one initial-universe decision."""

    SELECTED = "selected"
    ELIGIBLE_NOT_SELECTED = "eligible_not_selected"
    EXCLUDED = "excluded"


class EntrySelectionDecisionV1(_StrategyModel):
    """One ranked, immutable decision for a pinned Run security."""

    security_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    state: EntrySelectionState
    score: Decimal | None = None
    reason_code: str | None = Field(default=None, min_length=1)


class InitialEntrySelectionV1(_StrategyModel):
    """One complete initial ranked decision and its executable BUYs."""

    session: date
    metric_id: str = Field(min_length=1)
    metric_version: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    decisions: tuple[EntrySelectionDecisionV1, ...]
    signals: tuple[Signal, ...]

    @field_validator("decisions", "signals", mode="before")
    @classmethod
    def _detach_selection_collections(cls, value: object) -> tuple[object, ...]:
        return _as_detached_tuple(value)


class PositionSummaryV1(_StrategyModel):
    """A read-only simulated position summary bounded to one security.

    ``quantity``/``average_cost`` inherit ``_StrategyModel``'s
    ``allow_inf_nan=False`` config, so a ``NaN``/``Infinity`` value is
    already rejected at construction without a dedicated field validator.
    """

    security_id: str = Field(min_length=1)
    quantity: Decimal = Field(ge=Decimal(0))
    average_cost: Decimal = Field(gt=Decimal(0))


class VolatilityObservationV1(_StrategyModel):
    """A read-only historical-volatility fact bounded to one session.

    ``value`` inherits the same ``allow_inf_nan=False`` finite-value
    guarantee as :class:`PositionSummaryV1`.
    """

    security_id: str = Field(min_length=1)
    session: date
    value: Decimal


def _as_detached_tuple(value: object) -> tuple[object, ...]:
    """Copy a caller-supplied list/tuple so later caller mutation can't
    reach the value being validated into a ``PortfolioView`` field."""
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise TypeError(f"expected a list or tuple, got {type(value).__name__}")


class PortfolioView(_StrategyModel):
    """Read-only simulated portfolio state bounded to one session.

    Exposes only simulated cash, immutable position summaries, and
    historical-volatility observations already authorized as of
    ``as_of_session`` -- never a live SIPP/ISA identifier, repository
    handle, or mutation method (``extra="forbid"`` rejects any other
    field outright). List/tuple inputs are copied before element
    validation and every position/volatility item is itself frozen, so
    mutating a caller-supplied collection -- or a value this view returns
    -- after construction cannot affect the view.
    """

    as_of_session: date
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    cash: Decimal = Field(ge=Decimal(0))
    positions: tuple[PositionSummaryV1, ...]
    volatility_observations: tuple[VolatilityObservationV1, ...]

    @field_validator("positions", "volatility_observations", mode="before")
    @classmethod
    def _detach_collection(cls, value: object) -> tuple[object, ...]:
        return _as_detached_tuple(value)

    @model_validator(mode="after")
    def _no_future_dated_observations(self) -> "PortfolioView":
        for observation in self.volatility_observations:
            if observation.session > self.as_of_session:
                raise StrategyProtocolError(
                    StrategyProtocolErrorCode.FUTURE_DATED_OBSERVATION,
                    f"volatility observation for {observation.security_id!r} "
                    f"on {observation.session.isoformat()} is after "
                    f"as_of_session {self.as_of_session.isoformat()}",
                )
        return self

    @model_validator(mode="after")
    def _no_duplicate_positions(self) -> "PortfolioView":
        security_ids = [position.security_id for position in self.positions]
        if len(set(security_ids)) != len(security_ids):
            raise StrategyProtocolError(
                StrategyProtocolErrorCode.DUPLICATE_POSITION,
                "positions must contain at most one entry per security_id",
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_volatility_observations(self) -> "PortfolioView":
        keys = [
            (observation.security_id, observation.session)
            for observation in self.volatility_observations
        ]
        if len(set(keys)) != len(keys):
            raise StrategyProtocolError(
                StrategyProtocolErrorCode.DUPLICATE_VOLATILITY_OBSERVATION,
                "volatility_observations must contain at most one entry per "
                "(security_id, session) pair",
            )
        return self


# ---------------------------------------------------------------------------
# Parameter and market-view typing seam
# ---------------------------------------------------------------------------

#: A JSON-compatible parameter value a Strategy may receive. Story 2.2 owns
#: schema discovery and required/type/min/max/enum validation; this alias
#: only fixes the shape of the read-only mapping ``StrategyProtocolV1``
#: methods are passed.
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | Mapping[str, "JsonValue"]
StrategyParameters = Mapping[str, JsonValue]


@runtime_checkable
class MarketViewV1(Protocol):
    """Bounded, no-look-ahead market-view typing seam a Strategy receives.

    ``app.services.backtest.market_view.MarketView`` is the concrete
    pandas-backed implementation (AD-3/AD-18): it truncates Historical
    Price/Corporate Action and monthly-scan evidence to
    ``<= as_of_session`` and raises a stable bound-violation error for a
    security whose pinned evidence does not itself cover
    ``as_of_session``, so an accidental look-ahead read fails loudly at
    the point of misuse instead of silently returning future data.
    """

    @property
    def as_of_session(self) -> date: ...

    def price_history(self, security_id: str) -> pd.DataFrame:
        """Return ``security_id``'s split-continuous OHLCV history through
        ``as_of_session``, oldest first.

        An unknown/untracked ``security_id`` returns an empty DataFrame
        (not an error) -- the absence of *any* pinned evidence for a
        security is distinct from a bound violation on a security this
        view *does* track.
        """
        ...

    def scan_result(self, security_id: str) -> HistoricalScanRecordV1 | None:
        """Return the latest committed monthly scan record visible as of
        ``as_of_session``, or ``None`` if none is yet visible.

        A monthly scan candidate enters visibility only from its own
        recorded month-end ``as_of_session_date`` onward and remains the
        answer until superseded by that security's next committed month
        -- never backdated, even when ``as_of_session`` falls inside a
        later month than the one it was produced for.
        """
        ...


# ---------------------------------------------------------------------------
# The versioned Strategy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StrategyProtocolV1(Protocol):
    """The versioned seam a Backtest Strategy implements (AD-20).

    No method accepts or returns a database connection, repository,
    ORM/session, job, run, or persistence model -- only the bounded,
    immutable views and JSON-compatible parameters defined in this
    module. ``typing.Protocol`` only checks attribute/method presence at
    runtime, not full call signatures (Python 3.12 ``typing`` docs), so a
    future engine must still pair an ``isinstance`` conformance check with
    the explicit ``validate_*`` functions below before it acts on a
    Strategy's output. This is a bounded-interface guard, not a sandbox:
    first-party in-process Strategy code is trusted and code-reviewed.
    """

    def entry_signals(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> list[Signal]:
        """Return candidate entry signals for ``view``'s session."""
        ...

    def exit_signals(
        self,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> list[Signal]:
        """Return candidate exit signals given the simulated ``portfolio``."""
        ...

    def position_size(
        self,
        signal: Signal,
        view: MarketViewV1,
        portfolio: PortfolioView,
        parameters: StrategyParameters,
    ) -> int:
        """Return the integer share count to act on ``signal`` with."""
        ...


@runtime_checkable
class InitialEntrySelectionProviderV1(Protocol):
    """Optional capability for one atomic initial ranked entry decision."""

    def initial_entry_selection(
        self, view: MarketViewV1, parameters: StrategyParameters
    ) -> InitialEntrySelectionV1: ...


# ---------------------------------------------------------------------------
# Pure result validators
# ---------------------------------------------------------------------------


def _validate_signal_list(value: object, *, method_name: str) -> tuple[Signal, ...]:
    if not isinstance(value, list):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INVALID_SIGNAL_CONTAINER,
            f"{method_name} must return a list, got {type(value).__name__}",
        )
    for index, item in enumerate(value):
        if not isinstance(item, Signal):
            raise StrategyProtocolError(
                StrategyProtocolErrorCode.INVALID_SIGNAL_ELEMENT,
                f"{method_name}[{index}] must be a Signal, got {type(item).__name__}",
            )
    return tuple(sorted(value, key=lambda signal: signal.sort_key))


def validate_entry_signals(value: object) -> tuple[Signal, ...]:
    """Validate an ``entry_signals`` result before any engine mutation.

    Pure and side-effect-free: rejects a non-``list`` container or any
    non-``Signal`` element with a stable error code, then returns the
    signals in their one canonical ``sort_key`` order -- deterministic
    regardless of the order the Strategy returned them in.
    """
    return _validate_signal_list(value, method_name="entry_signals")


def validate_exit_signals(value: object) -> tuple[Signal, ...]:
    """Validate an ``exit_signals`` result before any engine mutation.

    Same contract as :func:`validate_entry_signals`.
    """
    return _validate_signal_list(value, method_name="exit_signals")


def validate_position_size(value: object) -> int:
    """Validate a ``position_size`` result before any engine mutation.

    Rejects ``bool`` (a ``bool`` is an ``int`` subclass in Python and
    would otherwise silently pass an ``int`` check), any other
    non-``int``/non-integral value, and negative sizes, each with a
    stable error code.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INVALID_POSITION_SIZE_TYPE,
            f"position_size must return a plain int, got {type(value).__name__}",
        )
    if value < 0:
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.NEGATIVE_POSITION_SIZE,
            f"position_size cannot be negative: {value}",
        )
    return value


def validate_initial_entry_selection(
    value: object,
    *,
    pinned_security_ids: Sequence[str],
    expected_session: date,
) -> InitialEntrySelectionV1:
    """Detach, validate, and canonically order one complete initial batch.

    The complete pinned universe and expected first union session are engine
    context, deliberately not inputs a Strategy can choose.  Nothing is
    returned until coverage, ranks, and selected BUY agreement all validate.
    """
    if not isinstance(value, InitialEntrySelectionV1):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INVALID_INITIAL_SELECTION,
            "initial_entry_selection must return InitialEntrySelectionV1",
        )
    if value.session != expected_session:
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INITIAL_SELECTION_SESSION_MISMATCH,
            "initial selection session does not match the first Run session",
        )

    decisions = tuple(sorted(value.decisions, key=lambda item: item.rank))
    ids = [item.security_id for item in decisions]
    if len(ids) != len(set(ids)):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INITIAL_SELECTION_DUPLICATE_SECURITY,
            "initial selection repeats a security_id",
        )
    pinned = tuple(pinned_security_ids)
    if len(pinned) != len(set(pinned)) or set(ids) != set(pinned):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INITIAL_SELECTION_UNIVERSE_MISMATCH,
            "initial selection must cover every pinned security exactly once",
        )
    if [item.rank for item in decisions] != list(range(1, len(decisions) + 1)):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INITIAL_SELECTION_RANK_INVALID,
            "initial selection ranks must be unique and contiguous from one",
        )

    signals = tuple(sorted(value.signals, key=lambda signal: signal.sort_key))
    expected_signals = tuple(
        sorted(
            (
                Signal(
                    security_id=item.security_id,
                    side=SignalSide.BUY,
                    session=value.session,
                    rule_id=value.rule_id,
                )
                for item in decisions
                if item.state is EntrySelectionState.SELECTED
            ),
            key=lambda signal: signal.sort_key,
        )
    )
    if signals != expected_signals:
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.INITIAL_SELECTION_SIGNAL_MISMATCH,
            "selected decisions must agree exactly with canonical BUY signals",
        )
    return InitialEntrySelectionV1(
        session=value.session,
        metric_id=value.metric_id,
        metric_version=value.metric_version,
        rule_id=value.rule_id,
        decisions=decisions,
        signals=signals,
    )


# ---------------------------------------------------------------------------
# Parameter schema and the shared parameter validator (Story 2.2)
# ---------------------------------------------------------------------------

#: The closed set of parameter value shapes a Strategy's ``SKILL.md``
#: frontmatter may declare. Kept as a ``Literal`` (matching
#: ``historical_scan_record.DetectorId``'s ``Literal`` + companion tuple
#: convention) rather than a ``StrEnum`` because parameter *type* is a
#: closed value vocabulary embedded inside a strict model field, not a
#: standalone importable symbol callers construct by name.
ParameterType = Literal["integer", "number", "boolean", "string", "enum"]

#: Kept in sync with :data:`ParameterType` by hand -- mirrors
#: ``historical_scan_record.DETECTOR_IDS``' role alongside ``DetectorId``.
PARAMETER_TYPES: tuple[ParameterType, ...] = (
    "integer",
    "number",
    "boolean",
    "string",
    "enum",
)

_NUMERIC_PARAMETER_TYPES = frozenset({"integer", "number"})


class ParameterValidationErrorCode(StrEnum):
    """Stable, machine-readable :func:`validate_strategy_parameters` codes.

    Kept as a sibling to :class:`StrategyProtocolErrorCode` rather than a
    shared enum: the existing codes describe protocol-boundary result
    failures (signals/position size/portfolio construction), while these
    describe per-submission parameter *field* failures returned inside a
    :class:`ParameterFieldErrorV1` -- a different failure surface with its
    own stable vocabulary, matching this module's one-`StrEnum`-per-
    failure-surface convention.
    """

    MISSING_REQUIRED = "missing_required"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_TYPE = "invalid_type"
    BELOW_MINIMUM = "below_minimum"
    ABOVE_MAXIMUM = "above_maximum"
    NOT_IN_ENUM = "not_in_enum"
    NON_FINITE_VALUE = "non_finite_value"


class StrategyParameterV1(_StrategyModel):
    """One declared parameter in a Strategy's parameter schema.

    Every schema-*authoring* invariant is enforced here, at construction
    time, as a genuine schema bug rather than an ordinary per-submission
    field error: ``minimum``/``maximum`` are only meaningful for
    ``integer``/``number`` parameters and must satisfy ``minimum <=
    maximum`` when both are given; ``enum_values`` are only meaningful for
    ``enum`` parameters and must be a non-empty, duplicate-free,
    *homogeneous* tuple of scalars (a ``bool`` is never treated as
    interchangeable with an ``int``, so ``enum_values`` can never silently
    mix ``True``/``1``); ``default`` must have the shape of the declared
    ``type`` (or, for ``enum``, be an exact type-and-value member of
    ``enum_values``); and no numeric bound or numeric default may be
    non-finite (``NaN``/``Infinity``).

    Deliberately NOT checked here: whether a numeric ``default`` actually
    falls within a declared ``minimum``/``maximum`` range. That is a
    value-vs-constraint question answered uniformly by
    :func:`validate_strategy_parameters` for both a Strategy's own
    declared defaults (Skill discovery calls it with ``apply_defaults=
    True`` against an empty submission) and any future caller-submitted
    value -- so there is exactly one authority for it, and an out-of-range
    declared default surfaces as Skill discovery's ``invalid_defaults``
    warning rather than as a construction-time exception here.

    ``name`` uniqueness is a *schema*-level invariant (no two parameters
    in one schema may share a name), not a per-parameter one, so it is
    enforced by :func:`validate_strategy_parameters` against the whole
    ``Sequence[StrategyParameterV1]`` it receives, not by this model.
    """

    name: str = Field(min_length=1)
    type: ParameterType
    default: JsonScalar
    description: str = Field(min_length=1)
    required: bool
    minimum: int | float | None = None
    maximum: int | float | None = None
    enum_values: tuple[JsonScalar, ...] | None = None

    @model_validator(mode="after")
    def _numeric_constraints(self) -> "StrategyParameterV1":
        is_numeric = self.type in _NUMERIC_PARAMETER_TYPES
        if not is_numeric and (self.minimum is not None or self.maximum is not None):
            raise ValueError(
                "minimum/maximum are only valid for integer or number parameters"
            )
        # Belt-and-suspenders: the field's own ``int | float | None`` type
        # under this model's ``strict=True``/``allow_inf_nan=False`` config
        # already rejects a bool or a non-finite float before this
        # validator ever runs. These checks stay only as an explicit,
        # readable statement of the invariant if that field typing ever
        # changes.
        for bound in (self.minimum, self.maximum):
            if isinstance(bound, bool):
                raise ValueError("minimum/maximum cannot be a bool")
            if isinstance(bound, float) and not math.isfinite(bound):
                raise ValueError("minimum/maximum cannot be non-finite")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum cannot exceed maximum")
        return self

    @model_validator(mode="after")
    def _enum_values(self) -> "StrategyParameterV1":
        if self.type != "enum":
            if self.enum_values is not None:
                raise ValueError("enum_values is only valid for enum parameters")
            return self
        if not self.enum_values:
            raise ValueError("enum parameters require a non-empty enum_values")
        if any(value is None for value in self.enum_values):
            raise ValueError("enum_values cannot contain null")
        first_type = type(self.enum_values[0])
        if any(type(value) is not first_type for value in self.enum_values):
            raise ValueError(
                "enum_values must be homogeneous (no mixed types, "
                "including bool vs int)"
            )
        if len(set(self.enum_values)) != len(self.enum_values):
            raise ValueError("enum_values cannot contain duplicates")
        return self

    @model_validator(mode="after")
    def _default_matches_declared_type(self) -> "StrategyParameterV1":
        default = self.default
        if self.type == "integer":
            valid_shape = isinstance(default, int) and not isinstance(default, bool)
        elif self.type == "number":
            valid_shape = isinstance(default, (int, float)) and not isinstance(
                default, bool
            )
        elif self.type == "boolean":
            valid_shape = isinstance(default, bool)
        elif self.type == "string":
            valid_shape = isinstance(default, str)
        else:  # "enum"
            valid_shape = self.enum_values is not None and any(
                type(default) is type(candidate) and default == candidate
                for candidate in self.enum_values
            )
        if not valid_shape:
            raise ValueError(f"default {default!r} is not a valid {self.type} value")
        # Belt-and-suspenders: ``default: JsonScalar``'s own field typing
        # under ``allow_inf_nan=False`` already rejects a non-finite float
        # before this validator runs; kept as an explicit statement of the
        # invariant, matching ``_numeric_constraints`` above.
        if isinstance(default, float) and not math.isfinite(default):
            raise ValueError("default cannot be non-finite (NaN/Infinity)")
        return self


class ParameterFieldErrorV1(_StrategyModel):
    """One structured, machine-readable parameter-validation failure.

    Returned (never raised) by :func:`validate_strategy_parameters` for
    every ordinary per-submission problem, matching this module's
    established convention that pure ``validate_*`` functions return
    structured results for result checking rather than raise.
    """

    parameter_name: str = Field(min_length=1)
    code: ParameterValidationErrorCode
    message: str = Field(min_length=1)


def _numeric_value_error(
    parameter: StrategyParameterV1, value: int | float
) -> ParameterFieldErrorV1 | None:
    """Validate a submitted ``integer``/``number`` value against bounds."""
    if isinstance(value, float) and not math.isfinite(value):
        return ParameterFieldErrorV1(
            parameter_name=parameter.name,
            code=ParameterValidationErrorCode.NON_FINITE_VALUE,
            message=f"{parameter.name!r} cannot be NaN/Infinity",
        )
    if parameter.minimum is not None and value < parameter.minimum:
        return ParameterFieldErrorV1(
            parameter_name=parameter.name,
            code=ParameterValidationErrorCode.BELOW_MINIMUM,
            message=f"{parameter.name!r} must be >= {parameter.minimum!r}",
        )
    if parameter.maximum is not None and value > parameter.maximum:
        return ParameterFieldErrorV1(
            parameter_name=parameter.name,
            code=ParameterValidationErrorCode.ABOVE_MAXIMUM,
            message=f"{parameter.name!r} must be <= {parameter.maximum!r}",
        )
    return None


def _validate_parameter_value(
    parameter: StrategyParameterV1, value: object
) -> ParameterFieldErrorV1 | None:
    """Validate one submitted value against its declared parameter schema."""
    if parameter.type == "boolean":
        if not isinstance(value, bool):
            return ParameterFieldErrorV1(
                parameter_name=parameter.name,
                code=ParameterValidationErrorCode.INVALID_TYPE,
                message=f"{parameter.name!r} must be a bool",
            )
        return None
    if parameter.type == "string":
        if not isinstance(value, str):
            return ParameterFieldErrorV1(
                parameter_name=parameter.name,
                code=ParameterValidationErrorCode.INVALID_TYPE,
                message=f"{parameter.name!r} must be a string",
            )
        return None
    if parameter.type == "integer":
        # A ``bool`` is an ``int`` subclass in Python, so it is checked and
        # rejected explicitly -- the same care ``validate_position_size``
        # already takes for its own integer result.
        if isinstance(value, bool) or not isinstance(value, int):
            return ParameterFieldErrorV1(
                parameter_name=parameter.name,
                code=ParameterValidationErrorCode.INVALID_TYPE,
                message=f"{parameter.name!r} must be a plain int",
            )
        return _numeric_value_error(parameter, value)
    if parameter.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ParameterFieldErrorV1(
                parameter_name=parameter.name,
                code=ParameterValidationErrorCode.INVALID_TYPE,
                message=f"{parameter.name!r} must be an int or float",
            )
        return _numeric_value_error(parameter, value)
    # "enum": exact type-and-value match -- True must never match 1.
    enum_values = parameter.enum_values or ()
    if not any(
        type(value) is type(candidate) and value == candidate
        for candidate in enum_values
    ):
        return ParameterFieldErrorV1(
            parameter_name=parameter.name,
            code=ParameterValidationErrorCode.NOT_IN_ENUM,
            message=f"{parameter.name!r} must be one of {enum_values!r}",
        )
    return None


def validate_strategy_parameters(
    schema: Sequence[StrategyParameterV1],
    submitted: Mapping[str, JsonValue],
    *,
    apply_defaults: bool,
) -> Mapping[str, JsonValue] | tuple[ParameterFieldErrorV1, ...]:
    """Validate ``submitted`` values against a Strategy's parameter schema.

    The single authority every caller reuses verbatim -- Skill discovery
    (validating a Strategy's own declared defaults), a future engine
    launch, and a future UI never invent a second validator.

    Pure and side-effect-free: for ordinary per-submission problems it
    never raises, instead returning an ordered ``tuple[ParameterFieldErrorV1,
    ...]`` (schema declaration order first, then any unknown submitted
    fields in lexical order) -- matching this module's convention that a
    pure ``validate_*`` function returns a structured result rather than
    raising. When there are no errors, it returns the normalized parameter
    mapping instead: every schema-declared field that was validly
    submitted, plus (when ``apply_defaults`` is ``True``) every field the
    caller omitted, filled in from its declared ``default``.

    Rejects ``bool`` for an ``integer``/``number`` field (a ``bool`` must
    never satisfy an ``int``/``float`` type check) and any field name not
    declared in ``schema``.

    Raises :class:`StrategyProtocolError` (code
    ``duplicate_parameter_declaration``) only for a genuine schema-
    authoring bug -- two schema entries sharing one ``name`` -- since that
    is never a per-submission concern.
    """
    names = [parameter.name for parameter in schema]
    if len(set(names)) != len(names):
        raise StrategyProtocolError(
            StrategyProtocolErrorCode.DUPLICATE_PARAMETER_DECLARATION,
            "parameter schema contains duplicate name declarations",
        )

    errors: list[ParameterFieldErrorV1] = []
    normalized: dict[str, JsonValue] = {}

    for parameter in schema:
        if parameter.name in submitted:
            value = submitted[parameter.name]
            error = _validate_parameter_value(parameter, value)
            if error is not None:
                errors.append(error)
            else:
                normalized[parameter.name] = value
        elif apply_defaults:
            # The declared default's *shape* was already proven correct at
            # ``StrategyParameterV1`` construction, but whether it falls
            # within a declared minimum/maximum was deliberately deferred
            # to here -- the one place that answers "is this value valid"
            # for both a submitted value and a Strategy's own declared
            # default. An out-of-range default surfaces as an ordinary
            # field error here, which is exactly what lets Skill discovery
            # isolate it as ``invalid_defaults`` instead of only failing
            # much later at launch.
            error = _validate_parameter_value(parameter, parameter.default)
            if error is not None:
                errors.append(error)
            else:
                normalized[parameter.name] = parameter.default
        elif parameter.required:
            errors.append(
                ParameterFieldErrorV1(
                    parameter_name=parameter.name,
                    code=ParameterValidationErrorCode.MISSING_REQUIRED,
                    message=f"{parameter.name!r} is required",
                )
            )

    schema_names = set(names)
    for name in sorted(name for name in submitted if name not in schema_names):
        errors.append(
            ParameterFieldErrorV1(
                parameter_name=name,
                code=ParameterValidationErrorCode.UNKNOWN_FIELD,
                message=f"unknown parameter {name!r}",
            )
        )

    if errors:
        return tuple(errors)
    return MappingProxyType(normalized)


__all__ = [
    "EntrySelectionDecisionV1",
    "EntrySelectionState",
    "InitialEntrySelectionProviderV1",
    "InitialEntrySelectionV1",
    "JsonScalar",
    "JsonValue",
    "MarketViewV1",
    "PARAMETER_TYPES",
    "ParameterFieldErrorV1",
    "ParameterType",
    "ParameterValidationErrorCode",
    "PortfolioView",
    "PositionSummaryV1",
    "Signal",
    "SignalSide",
    "StrategyParameterV1",
    "StrategyParameters",
    "StrategyProtocolError",
    "StrategyProtocolErrorCode",
    "StrategyProtocolV1",
    "VolatilityObservationV1",
    "validate_entry_signals",
    "validate_exit_signals",
    "validate_initial_entry_selection",
    "validate_position_size",
    "validate_strategy_parameters",
]
