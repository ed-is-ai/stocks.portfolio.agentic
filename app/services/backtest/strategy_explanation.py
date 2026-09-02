"""Strategy-owned structured Signal explanations (#472).

A ``Signal`` carries one opaque ``rule_id``; several simultaneously true
conditions collapse into a single unreadable token. This module adds the
versioned, provider-neutral vocabulary a Strategy uses to say *why* it
emitted a Buy or Sell -- deterministic reason codes plus typed
observed/operator/threshold/unit facts -- and the pure, markup-free
formatters the recommendations screen and the recommendation email both
render from, so neither host ever branches on ``strategy_id``.

Import boundary: this module deliberately imports only the standard
library and pydantic. ``strategy_protocol`` imports *it* (to type
``Signal.explanation``), so importing ``_StrategyModel`` back from
``strategy_protocol`` would create a cycle; the identical frozen/strict
``ConfigDict`` is therefore restated here as :class:`_ExplanationModel`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The one explanation contract version a Strategy may emit today.
EXPLANATION_CONTRACT_VERSION: int = 1

#: Reason codes are stable, machine-readable identifiers -- lowercase
#: snake_case, 3-64 characters -- never presentation text.
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: Any ``&name;``/``&#123;`` sequence -- an HTML entity smuggled through a
#: field that must stay plain text.
_HTML_ENTITY_PATTERN = re.compile(
    r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);"
)


#: Characters that are not C0 controls but still break a plain-text
#: contract: DEL, the C1 control block, and the Unicode line/paragraph
#: separators that terminate a line inside HTML or JavaScript.
_FORBIDDEN_CHARACTERS = frozenset(
    "\x7f\u2028\u2029" + "".join(chr(code) for code in range(0x80, 0xA0))
)


class _ExplanationModel(BaseModel):
    """Frozen, strict, extra-forbidding base for every explanation model.

    Mirrors ``strategy_protocol._StrategyModel``'s immutability convention
    (``extra="forbid"``, ``frozen=True``, ``strict=True``,
    ``allow_inf_nan=False``) verbatim. It is restated rather than imported
    because ``strategy_protocol`` imports this module -- see the module
    docstring's import-boundary note.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, allow_inf_nan=False
    )


class ComparisonOperator(StrEnum):
    """The closed comparison vocabulary a fact may state."""

    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    EQ = "=="
    NE = "!="
    CROSSED_ABOVE = "crossed_above"
    CROSSED_BELOW = "crossed_below"
    IS = "is"
    IS_NOT = "is_not"


class EvidenceUnit(StrEnum):
    """The closed unit vocabulary the host formatter renders values with."""

    NONE = "none"
    PRICE = "price"
    PERCENT = "percent"
    RATIO = "ratio"
    SESSIONS = "sessions"
    COUNT = "count"
    SCORE = "score"


def require_plain_text(value: str, *, field: str) -> str:
    """Return ``value`` unchanged, or raise for presentation markup.

    Plain text means: no angle brackets, no HTML entity sequence, and no
    ASCII control characters. Strategy code must never emit markup, and
    the host must never have to escape a Strategy's evidence twice.
    """
    if "<" in value or ">" in value:
        raise ValueError(f"{field} must be plain text (no angle brackets)")
    if _HTML_ENTITY_PATTERN.search(value):
        raise ValueError(f"{field} must be plain text (no HTML entities)")
    if any(
        character < " " or character in _FORBIDDEN_CHARACTERS for character in value
    ):
        raise ValueError(f"{field} must be plain text (no control characters)")
    return value


def _require_finite(value: Decimal | str | None, *, field: str) -> Decimal | str | None:
    """Reject a non-finite ``Decimal`` and any markup in a string value."""
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{field} must be a finite number")
    if isinstance(value, str):
        return require_plain_text(value, field=field)
    return value


def _as_detached_tuple(value: object, *, field: str) -> tuple[object, ...]:
    """Copy a list/tuple input; reject every other collection shape.

    Same convention as ``strategy_protocol._as_detached_tuple``: a ``set``
    /``frozenset``/``dict`` has no deterministic iteration contract, so it
    is a ``TypeError`` rather than a silently reordered explanation.
    """
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise TypeError(f"{field} expected a list or tuple, got {type(value).__name__}")


class ExplanationFactV1(_ExplanationModel):
    """One typed observation behind a reason.

    ``observed``/``threshold`` are either a finite ``Decimal`` or a plain
    text label (e.g. a stage name), so the same fact shape covers both a
    numeric comparison and a categorical one.
    """

    label: str = Field(min_length=1, max_length=80)
    observed: Decimal | str | None = None
    operator: ComparisonOperator | None = None
    threshold: Decimal | str | None = None
    unit: EvidenceUnit = EvidenceUnit.NONE
    as_of: date | None = None

    @field_validator("label")
    @classmethod
    def _plain_label(cls, value: str) -> str:
        return require_plain_text(value, field="label")

    @field_validator("observed", "threshold")
    @classmethod
    def _finite_plain_value(cls, value: Decimal | str | None) -> Decimal | str | None:
        return _require_finite(value, field="value")

    @model_validator(mode="after")
    def _complete_comparison(self) -> "ExplanationFactV1":
        """Reject a half-stated comparison.

        A fact either states a bare observation or a complete one: an
        operator without both sides would render as evidence the Strategy
        never actually produced (``"Close > 2"`` reads as a claim about
        the label itself), so it is a construction error rather than a
        formatting decision.
        """
        if self.operator is None:
            if self.threshold is not None:
                raise ValueError("threshold requires an operator")
            return self
        if self.observed is None or self.threshold is None:
            raise ValueError(
                "an operator requires both an observed value and a threshold"
            )
        return self


class SignalReasonV1(_ExplanationModel):
    """One deterministic reason code with its supporting facts."""

    code: str = Field(pattern=_REASON_CODE_PATTERN.pattern)
    summary: str = Field(min_length=1, max_length=240)
    facts: tuple[ExplanationFactV1, ...] = ()

    @field_validator("facts", mode="before")
    @classmethod
    def _detach_facts(cls, value: object) -> tuple[object, ...]:
        return _as_detached_tuple(value, field="facts")

    @field_validator("summary")
    @classmethod
    def _plain_summary(cls, value: str) -> str:
        return require_plain_text(value, field="summary")

    @model_validator(mode="after")
    def _unique_fact_labels(self) -> "SignalReasonV1":
        labels = [fact.label for fact in self.facts]
        if len(set(labels)) != len(labels):
            raise ValueError("facts must not repeat a label")
        return self


class SignalExplanationV1(_ExplanationModel):
    """The complete, canonically ordered explanation behind one signal.

    Reasons are sorted by ``code`` here rather than by each Strategy's own
    control flow, which makes "deterministic order" a property of the
    model itself.
    """

    contract_version: int = EXPLANATION_CONTRACT_VERSION
    reasons: tuple[SignalReasonV1, ...]

    @field_validator("reasons", mode="before")
    @classmethod
    def _detach_reasons(cls, value: object) -> tuple[object, ...]:
        return _as_detached_tuple(value, field="reasons")

    @field_validator("contract_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != EXPLANATION_CONTRACT_VERSION:
            raise ValueError(
                f"unsupported explanation contract version {value}; "
                f"expected {EXPLANATION_CONTRACT_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _canonical_reasons(self) -> "SignalExplanationV1":
        if not self.reasons:
            raise ValueError("reasons must not be empty")
        codes = [reason.code for reason in self.reasons]
        if len(set(codes)) != len(codes):
            raise ValueError("reasons must not repeat a code")
        ordered = tuple(sorted(self.reasons, key=lambda reason: reason.code))
        if ordered != self.reasons:
            # ``object.__setattr__`` is the only normalization route open
            # here: pydantic does not support an ``mode="after"``
            # validator returning a *different* instance when the model is
            # built through ``__init__``, and the reasons are already
            # validated ``SignalReasonV1`` objects by this point. The model
            # stays frozen to every caller -- only this validator, before
            # the instance escapes, ever writes to it.
            object.__setattr__(self, "reasons", ordered)
        return self

    @property
    def codes(self) -> tuple[str, ...]:
        """Return the canonically ordered reason codes."""
        return tuple(reason.code for reason in self.reasons)

    @property
    def as_of_dates(self) -> tuple[date, ...]:
        """Return every fact observation date, in reason/fact order."""
        return tuple(
            fact.as_of
            for reason in self.reasons
            for fact in reason.facts
            if fact.as_of is not None
        )


# ---------------------------------------------------------------------------
# Pure host formatting -- shared verbatim by screen, HTML email, and text
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """Render a finite ``Decimal`` in plain notation without trailing zeros."""
    if value == 0:
        return "0"
    # ``normalize`` rounds to the *default* context precision (28 digits),
    # which would silently corrupt a larger value; a local context sized to
    # the value's own digits only strips trailing zeros.
    with localcontext() as context:
        context.prec = max(len(value.as_tuple().digits), 1)
        normalized = value.normalize()
    # ``:f`` already renders a positive exponent in plain notation, so no
    # ``quantize`` is needed -- and quantizing would raise
    # ``InvalidOperation`` for a magnitude beyond the default context's
    # precision, turning a render into an exception.
    text = f"{normalized:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_value(value: Decimal | str | None, unit: EvidenceUnit) -> str:
    """Render one observed/threshold value in its declared unit."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    rendered = format_decimal(value)
    if unit is EvidenceUnit.PERCENT:
        return f"{rendered}%"
    if unit is EvidenceUnit.RATIO:
        return f"{rendered}x"
    if unit is EvidenceUnit.SESSIONS:
        return f"{rendered} sessions"
    return rendered


def format_fact(fact: ExplanationFactV1) -> str:
    """Render one fact as a single plain-text line.

    ``"Close 92.10 < 150-session SMA 101.44"`` with a comparison,
    ``"Close: 92.10"`` without one, plus ``" (as of YYYY-MM-DD)"`` when
    the observation is dated.
    """
    observed = format_value(fact.observed, fact.unit)
    if fact.operator is None:
        head = f"{fact.label}: {observed}" if observed else fact.label
    else:
        threshold = format_value(fact.threshold, fact.unit)
        operator = fact.operator.value.replace("_", " ")
        parts = [fact.label, observed, operator, threshold]
        head = " ".join(part for part in parts if part)
    if fact.as_of is not None:
        return f"{head} (as of {fact.as_of.isoformat()})"
    return head


def format_reason(reason: SignalReasonV1) -> tuple[str, tuple[str, ...]]:
    """Return one reason as ``(summary, formatted facts)``."""
    return reason.summary, tuple(format_fact(fact) for fact in reason.facts)


__all__ = [
    "ComparisonOperator",
    "EXPLANATION_CONTRACT_VERSION",
    "EvidenceUnit",
    "ExplanationFactV1",
    "SignalExplanationV1",
    "SignalReasonV1",
    "format_decimal",
    "format_fact",
    "format_reason",
    "format_value",
    "require_plain_text",
]
