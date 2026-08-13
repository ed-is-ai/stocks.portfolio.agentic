"""Currency-preserving ``Money`` type and locale-aware amount parser.

Dependency-free (stdlib ``decimal``/``re`` only), following AD-2's precedent
for a shared type multiple layers need without a reverse dependency from
``app/core/`` into ``app/agents/``.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

#: v1-scoped, documented assumption -- not an architecture-pinned list.
#: AD-24/FR-6 require "explicit ISO 4217 currency," not an enumerated
#: allow-list, but the PRD's own worked examples and issue #210's defects
#: only name GBP (home currency), EUR, USD, and HKD. Any other ISO 4217
#: code is a validation error, never silently accepted or defaulted.
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"GBP", "USD", "EUR", "HKD"})

#: Currency markers checked at the start of a cell. "HK$" must be checked
#: before the bare "$" marker, or "HK$1,234.56" would match "$" first and
#: leave a stray "HK" prefix.
_CURRENCY_MARKERS: tuple[tuple[str, str], ...] = (
    ("HK$", "HKD"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("$", "USD"),
)

_DIGITS_SHAPE_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
_LEADING_NON_DIGIT_RE = re.compile(r"^[^0-9]*")


class MoneyParseError(ValueError):
    """Raised by ``parse_money`` instead of returning a guessed value.

    ``.code`` is stable and never inferred from message text, matching the
    ``CurrencyPolicyError`` convention in ``app/services/backtest/currency.py``.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class Money(BaseModel):
    """An immutable amount with its explicit ISO 4217 currency.

    Generic and reusable: any 3-letter uppercase currency shape is accepted
    here. The v1 ``SUPPORTED_CURRENCIES`` allow-list is a parsing-time
    policy enforced by ``parse_money``, not a ``Money`` invariant, so a
    later story can reuse this type for currencies beyond v1's SIPP-import
    allow-list without touching it.
    """

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str

    @field_validator("currency")
    @classmethod
    def _validate_currency_shape(cls, value: str) -> str:
        upper = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", upper):
            raise ValueError(f"currency must be 3 uppercase ASCII letters: {value!r}")
        return upper


def _split_grouping(int_part: str, grouping_sep: str) -> str | None:
    """Validate ``int_part``'s grouping and return it with separators
    removed, or None if the grouping is invalid (wrong group sizes)."""
    groups = int_part.split(grouping_sep)
    if not (1 <= len(groups[0]) <= 3):
        return None
    if any(len(g) != 3 for g in groups[1:]):
        return None
    return "".join(groups)


def _resolve_digits(digits: str, ref: str) -> str:
    """Resolve a locale-formatted digit string to a plain decimal string
    (``int.frac`` or a bare integer), per the numeric-locale policy.
    """
    if not _DIGITS_SHAPE_RE.fullmatch(digits):
        raise MoneyParseError("malformed_locale", f"unparseable amount {ref!r}")

    has_dot = "." in digits
    has_comma = "," in digits

    if has_dot and has_comma:
        # The separator occurring last in the string is the decimal
        # separator; the other is grouping, wherever it appears.
        if digits.rfind(".") > digits.rfind(","):
            decimal_sep, grouping_sep = ".", ","
        else:
            decimal_sep, grouping_sep = ",", "."
        int_part, _, frac_part = digits.rpartition(decimal_sep)
        resolved_int = _split_grouping(int_part, grouping_sep)
        if resolved_int is None:
            raise MoneyParseError("malformed_locale", f"unparseable amount {ref!r}")
        return f"{resolved_int}.{frac_part}"

    if has_dot or has_comma:
        sep = "." if has_dot else ","
        if digits.count(sep) > 1:
            # Only one separator type, repeated -- unambiguously grouping
            # (a decimal separator can occur at most once).
            resolved = _split_grouping(digits, sep)
            if resolved is None:
                raise MoneyParseError("malformed_locale", f"unparseable amount {ref!r}")
            return resolved
        # Exactly one occurrence of one separator type -- the genuinely
        # ambiguous case. n = digit count after the separator.
        int_part, frac_part = digits.split(sep)
        n = len(frac_part)
        if n == 3:
            # Could be a 3-decimal-place amount or a whole number with one
            # grouping separator and no fraction -- these differ 1000x and
            # neither reading is self-evidently right for a 2dp currency.
            raise MoneyParseError(
                "ambiguous_currency",
                f"amount {ref!r} has exactly 3 digits after a single "
                "separator: could be a 3dp amount or a grouped whole "
                "number",
            )
        return f"{int_part}.{frac_part}"

    return digits


def _strip_sign_and_marker(
    s: str, default_currency: str
) -> tuple[str, bool, str, bool]:
    """Strip a leading paren/sign and currency marker from ``s``.

    Returns ``(remaining, negative, currency, matched_marker)``.
    ``matched_marker`` is True only when an explicit currency marker was
    found in the text -- distinct from ``currency == default_currency``,
    which can also happen when a marker resolves to the same code as the
    default (e.g. a "£" marker when ``default_currency="GBP"``).
    """
    negative = False

    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]

    if s.startswith("-"):
        negative = True
        s = s[1:]

    currency = default_currency
    matched_marker = False
    for marker, code in _CURRENCY_MARKERS:
        if s.startswith(marker):
            currency = code
            s = s[len(marker) :]
            matched_marker = True
            break

    return s, negative, currency, matched_marker


def has_currency_marker(raw: str, *, default_currency: str = "GBP") -> bool:
    """Return True if ``raw`` carries an explicit currency marker.

    Used to tell a field that genuinely names its currency apart from one
    that silently defaulted -- a marker-less field never contradicts
    another field's currency (see ``parse_money``'s sign/marker extraction
    order, which this mirrors for just the marker-detection step).
    """
    s = raw.replace("﻿", "").replace('"', "").strip()
    return _strip_sign_and_marker(s, default_currency)[3]


def parse_money(raw: str, *, default_currency: str = "GBP", ref: str = "") -> Money:
    """Parse a SIPP CSV cell into a ``Money``, raising ``MoneyParseError``
    rather than returning a guessed value.

    Caller contract: ``raw`` is a non-blank, non-``"n/a"`` cell -- blank/
    ``n/a`` handling stays the caller's job (matching ``clean_amount``'s
    existing behaviour), not this function's.
    """
    s = raw.replace("﻿", "").replace('"', "").strip()
    s, negative, currency, matched_marker = _strip_sign_and_marker(s, default_currency)

    if not matched_marker:
        candidate = _LEADING_NON_DIGIT_RE.match(s)
        prefix = candidate.group() if candidate else ""
        if prefix:
            # An unrecognized non-numeric prefix in front of an otherwise
            # well-formed number is a currency this parser doesn't
            # support (e.g. "¥123", "CHF 123.45") -- distinct from
            # genuinely malformed text ("abc"), where nothing but the
            # prefix exists at all. Leave `s` untouched otherwise: the
            # digit-shape check below correctly raises malformed_locale
            # for that case.
            remainder = s[len(prefix) :]
            if _DIGITS_SHAPE_RE.fullmatch(remainder):
                raise MoneyParseError(
                    "unsupported_currency",
                    f"unsupported currency marker {prefix!r} in {ref or raw!r}",
                )

    if s.startswith("-"):
        negative = not negative
        s = s[1:]

    if currency not in SUPPORTED_CURRENCIES:
        raise MoneyParseError(
            "unsupported_currency", f"unsupported currency in {ref or raw!r}"
        )

    resolved = _resolve_digits(s, ref or raw)
    amount = Decimal(("-" if negative else "") + resolved)
    return Money(amount=amount, currency=currency)
