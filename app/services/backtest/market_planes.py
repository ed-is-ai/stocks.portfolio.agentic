"""Deterministic price, volume, and action projections over pinned evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import (
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)
import math
from typing import TypeVar

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.historical_data_qualification import (
    REQUEST_CONTRACT_VERSION,
)

EIGHT_PLACES = Decimal("0.00000001")
DECIMAL_PRECISION = 50
DETERMINISTIC_DECIMAL_CONTEXT = Context(
    prec=DECIMAL_PRECISION,
    rounding=ROUND_HALF_EVEN,
)
PRICE_VOLUME_PLANE_VERSION = "HistoricalMarketPlanesV1"
QUOTE_UNIT_SCALES = {
    ("USD", "USD"): Decimal("1"),
    ("GBP", "GBP"): Decimal("1"),
    ("GBP", "GBp"): Decimal("0.01"),
}


class MarketDataPolicyError(ValueError):
    """A stable visible failure in deterministic market-data interpretation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CorporateAction:
    session: date
    action_type: str
    value: Decimal
    evidence_revision: str


@dataclass(frozen=True)
class MarketPlaneRow:
    evidence_revision: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True)
class ProviderNativeRow(MarketPlaneRow):
    """Provider-native row; never valid as a fill or indicator price contract."""


@dataclass(frozen=True)
class AsTradedRow(MarketPlaneRow):
    """As-traded row intended for fills and portfolio valuation."""


@dataclass(frozen=True)
class SplitContinuousRow(MarketPlaneRow):
    """Point-in-time split-continuous row intended for detectors only."""


_DerivedRowT = TypeVar("_DerivedRowT", bound=MarketPlaneRow)


def deterministic_decimal_context():
    """Return a fresh arithmetic context independent of process-global settings."""
    return localcontext(DETERMINISTIC_DECIMAL_CONTEXT)


def validate_provider_native_request_contract(
    evidence: StoredHistoricalEvidence,
) -> None:
    expected = {
        "start": evidence.start,
        "end": evidence.end,
        "interval": "1d",
        "prepost": False,
        "auto_adjust": False,
        "back_adjust": False,
        "actions": True,
        "repair": False,
        "keepna": True,
        "rounding": False,
        "timeout": 15,
        "raise_errors": True,
    }
    if (
        evidence.request_contract_version != REQUEST_CONTRACT_VERSION
        or dict(evidence.request_contract) != expected
    ):
        raise MarketDataPolicyError(
            "integrity_error", "Evidence request contract is incompatible."
        )


def provider_decimal(value: object, *, nullable: bool = False) -> Decimal | None:
    """Decode Story 1.3's finite float-hex value into Decimal(str(value))."""
    if value is None:
        if nullable:
            return None
        raise MarketDataPolicyError(
            "integrity_error", "Required market value is missing."
        )
    if not isinstance(value, str):
        raise MarketDataPolicyError(
            "integrity_error", "Market value encoding is invalid."
        )
    try:
        decoded = float.fromhex(value)
        if not math.isfinite(decoded) or decoded.hex() != value:
            raise ValueError
        result = Decimal(str(decoded))
    except (OverflowError, ValueError, InvalidOperation) as exc:
        raise MarketDataPolicyError(
            "integrity_error", "Market value encoding is invalid."
        ) from exc
    if not result.is_finite():
        raise MarketDataPolicyError("integrity_error", "Market value is not finite.")
    return result


def quantize_eight(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise MarketDataPolicyError("integrity_error", "Market value is not finite.")
    try:
        with deterministic_decimal_context():
            return value.quantize(EIGHT_PLACES, rounding=ROUND_HALF_EVEN)
    except DecimalException as exc:
        raise MarketDataPolicyError(
            "integrity_error", "Market value cannot be represented."
        ) from exc


def quote_unit_scale(currency: str, quote_unit: str) -> Decimal:
    try:
        return QUOTE_UNIT_SCALES[(currency, quote_unit)]
    except KeyError as exc:
        raise MarketDataPolicyError(
            "unsupported_quote_unit", f"Unsupported quote unit: {quote_unit}."
        ) from exc


@dataclass(frozen=True)
class HistoricalMarketPlanes:
    """Named projections derived from one exact immutable evidence revision."""

    data_revision: str
    policy_version: str
    currency: str
    quote_unit: str
    quote_unit_scale: Decimal
    exchange_timezone: str
    start: date
    end: date
    _provider_rows: tuple[ProviderNativeRow, ...]
    _actions: tuple[CorporateAction, ...]

    @classmethod
    def from_evidence(
        cls, evidence: StoredHistoricalEvidence
    ) -> HistoricalMarketPlanes:
        try:
            start = date.fromisoformat(evidence.start)
            end = date.fromisoformat(evidence.end)
        except (TypeError, ValueError) as exc:
            raise MarketDataPolicyError(
                "integrity_error", "Evidence interval is invalid."
            ) from exc
        if start >= end or evidence.provider != "yfinance":
            raise MarketDataPolicyError(
                "integrity_error", "Evidence contract is invalid."
            )
        validate_provider_native_request_contract(evidence)
        try:
            quote_scale = Decimal(evidence.quote_unit_scale)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MarketDataPolicyError(
                "integrity_error", "Evidence quote-unit scale is invalid."
            ) from exc
        if not quote_scale.is_finite():
            raise MarketDataPolicyError(
                "integrity_error", "Evidence quote-unit scale is invalid."
            )
        try:
            expected_scale = quote_unit_scale(evidence.currency, evidence.quote_unit)
        except MarketDataPolicyError as exc:
            raise MarketDataPolicyError(
                "integrity_error", "Evidence quote-unit contract is invalid."
            ) from exc
        if quote_scale != expected_scale:
            raise MarketDataPolicyError(
                "integrity_error", "Evidence quote-unit contract is invalid."
            )

        actions: list[CorporateAction] = []
        action_keys: set[tuple[date, str]] = set()
        for raw in evidence.actions:
            try:
                session = date.fromisoformat(str(raw["session"]))
                action_type = str(raw["action_type"])
                value = provider_decimal(raw["value"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataPolicyError(
                    "integrity_error", "Corporate-action evidence is malformed."
                ) from exc
            assert value is not None
            if action_type not in {"split", "dividend"}:
                raise MarketDataPolicyError(
                    "unsupported_corporate_action",
                    f"Unsupported corporate action: {action_type}.",
                )
            if session < start or session >= end:
                raise MarketDataPolicyError(
                    "integrity_error", "Corporate action is outside evidence bounds."
                )
            if value <= 0:
                raise MarketDataPolicyError(
                    "integrity_error", "Corporate-action value must be positive."
                )
            key = (session, action_type)
            if key in action_keys:
                raise MarketDataPolicyError(
                    "integrity_error", "Corporate-action evidence is ambiguous."
                )
            action_keys.add(key)
            actions.append(
                CorporateAction(session, action_type, value, evidence.data_revision)
            )
        actions.sort(key=lambda action: (action.session, action.action_type))

        rows: list[ProviderNativeRow] = []
        row_action_values: dict[tuple[date, str], Decimal] = {}
        seen_sessions: set[date] = set()
        for raw in evidence.rows:
            try:
                session = date.fromisoformat(str(raw["session"]))
                open_value = provider_decimal(raw["open"])
                high = provider_decimal(raw["high"])
                low = provider_decimal(raw["low"])
                close = provider_decimal(raw["close"])
                volume = provider_decimal(raw["volume"], nullable=True)
                dividend = provider_decimal(raw["dividends"])
                split = provider_decimal(raw["stock_splits"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataPolicyError(
                    "integrity_error", "Historical observation is malformed."
                ) from exc
            if session < start or session >= end or session in seen_sessions:
                raise MarketDataPolicyError(
                    "integrity_error", "Historical sessions are invalid or duplicated."
                )
            seen_sessions.add(session)
            assert all(value is not None for value in (open_value, high, low, close))
            assert open_value is not None and high is not None and low is not None
            assert close is not None
            assert dividend is not None and split is not None
            if (
                min(open_value, high, low, close) <= 0
                or low > min(open_value, close)
                or high < max(open_value, close)
            ):
                raise MarketDataPolicyError(
                    "integrity_error", "Historical OHLC geometry is invalid."
                )
            if volume is not None and volume < 0:
                raise MarketDataPolicyError(
                    "integrity_error", "Historical volume cannot be negative."
                )
            if dividend < 0 or split < 0:
                raise MarketDataPolicyError(
                    "integrity_error", "Corporate-action value cannot be negative."
                )
            if dividend > 0:
                row_action_values[(session, "dividend")] = dividend
            if split > 0:
                row_action_values[(session, "split")] = split
            rows.append(
                ProviderNativeRow(
                    evidence.data_revision,
                    session,
                    open_value,
                    high,
                    low,
                    close,
                    volume,
                )
            )
        if not rows or tuple(row.session for row in rows) != tuple(
            sorted(row.session for row in rows)
        ):
            raise MarketDataPolicyError(
                "integrity_error", "Historical sessions are missing or unordered."
            )

        row_sessions = {row.session for row in rows}
        if any(action.session not in row_sessions for action in actions):
            raise MarketDataPolicyError(
                "integrity_error", "Corporate action has no matching observation."
            )
        action_values = {
            (action.session, action.action_type): action.value for action in actions
        }
        if action_values != row_action_values:
            raise MarketDataPolicyError(
                "integrity_error", "Corporate-action projections conflict."
            )
        return cls(
            evidence.data_revision,
            PRICE_VOLUME_PLANE_VERSION,
            evidence.currency,
            evidence.quote_unit,
            quote_scale,
            evidence.exchange_timezone,
            start,
            end,
            tuple(rows),
            tuple(actions),
        )

    def provider_native(self) -> tuple[ProviderNativeRow, ...]:
        """Evidence-only projection; consumers must choose a derived plane."""
        return self._provider_rows

    def as_traded(self) -> tuple[AsTradedRow, ...]:
        """Reverse provider retroactive split factors through the pinned cutoff."""
        splits = tuple(
            action for action in self._actions if action.action_type == "split"
        )
        return tuple(
            self._scale_price(
                row,
                self._split_product(splits, after=row.session, through=None),
                volume=row.volume,
                row_type=AsTradedRow,
            )
            for row in self._provider_rows
        )

    def split_continuous_as_of(self, as_of: date) -> tuple[SplitContinuousRow, ...]:
        """Return detector history bounded to D with only D-effective splits."""
        self._validate_as_of(as_of)
        splits = tuple(
            action
            for action in self._actions
            if action.action_type == "split" and action.session <= as_of
        )
        result: list[SplitContinuousRow] = []
        for row in self.as_traded():
            if row.session > as_of:
                break
            factor = self._split_product(splits, after=row.session, through=as_of)
            try:
                with deterministic_decimal_context():
                    volume = (
                        None
                        if row.volume is None
                        else quantize_eight(row.volume * factor)
                    )
                    price_factor = Decimal(1) / factor
            except DecimalException as exc:
                raise MarketDataPolicyError(
                    "integrity_error", "Market-plane arithmetic failed."
                ) from exc
            result.append(
                self._scale_price(
                    row,
                    price_factor,
                    volume=volume,
                    row_type=SplitContinuousRow,
                )
            )
        return tuple(result)

    def actions_as_of(self, as_of: date) -> tuple[CorporateAction, ...]:
        self._validate_as_of(as_of)
        return tuple(action for action in self._actions if action.session <= as_of)

    def _validate_as_of(self, as_of: date) -> None:
        if as_of < self.start or as_of >= self.end:
            raise MarketDataPolicyError(
                "integrity_error", "Requested market view is outside evidence bounds."
            )

    @staticmethod
    def _split_product(
        splits: tuple[CorporateAction, ...], *, after: date, through: date | None
    ) -> Decimal:
        try:
            with deterministic_decimal_context():
                product = Decimal(1)
                for split in splits:
                    if split.session > after and (
                        through is None or split.session <= through
                    ):
                        product *= split.value
        except DecimalException as exc:
            raise MarketDataPolicyError(
                "integrity_error", "Split factor cannot be represented."
            ) from exc
        if product <= 0 or not product.is_finite():
            raise MarketDataPolicyError("integrity_error", "Split factor is invalid.")
        return product

    @staticmethod
    def _scale_price(
        row: MarketPlaneRow,
        factor: Decimal,
        *,
        volume: Decimal | None,
        row_type: type[_DerivedRowT],
    ) -> _DerivedRowT:
        try:
            with deterministic_decimal_context():
                return row_type(
                    evidence_revision=row.evidence_revision,
                    session=row.session,
                    open=row.open * factor,
                    high=row.high * factor,
                    low=row.low * factor,
                    close=row.close * factor,
                    volume=volume,
                )
        except DecimalException as exc:
            raise MarketDataPolicyError(
                "integrity_error", "Market-plane arithmetic failed."
            ) from exc
