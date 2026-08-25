from __future__ import annotations

from datetime import date
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import json
from pathlib import Path

import pytest

from app.repositories.historical_price_repo import StoredHistoricalEvidence
from app.services.backtest.corporate_actions import PositionState, apply_split
from app.services.backtest.market_planes import (
    AsTradedRow,
    HistoricalMarketPlanes,
    MarketDataPolicyError,
    PRICE_VOLUME_PLANE_VERSION,
    ProviderNativeRow,
    SplitContinuousRow,
    provider_decimal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "market_mechanics_v1.json"


def _hex(value: float) -> str:
    return float(value).hex()


def _row(
    session: str,
    close: float,
    volume: float | None,
    *,
    dividend: float = 0,
    split: float = 0,
    open_value: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> dict[str, object]:
    open_value = close if open_value is None else open_value
    high = max(open_value, close) if high is None else high
    low = min(open_value, close) if low is None else low
    return {
        "session": session,
        "open": _hex(open_value),
        "high": _hex(high),
        "low": _hex(low),
        "close": _hex(close),
        "adj_close": _hex(close / 2),
        "volume": None if volume is None else _hex(volume),
        "dividends": _hex(dividend),
        "stock_splits": _hex(split),
    }


def _evidence(
    rows: tuple[dict[str, object], ...],
    actions: tuple[dict[str, object], ...],
    **overrides: object,
) -> StoredHistoricalEvidence:
    values: dict[str, object] = dict(
        data_revision="revision-1",
        security_id="security-1",
        provider="yfinance",
        provider_version="1.4.1",
        request_contract_version="YFinanceDailyProviderNativeV1",
        requested_symbol="SPLT",
        observed_symbol="SPLT",
        alias_revision="alias-v1",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
        exchange_timezone="America/New_York",
        start="2024-01-01",
        end="2024-01-06",
        request_contract={
            "start": "2024-01-01",
            "end": "2024-01-06",
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
        },
        response_metadata_digest="metadata",
        canonical_manifest_json="{}",
        rows=rows,
        actions=actions,
    )
    values.update(overrides)
    return StoredHistoricalEvidence(**values)  # type: ignore[arg-type]


def test_ordinary_split_planes_reverse_provider_restatement_once() -> None:
    evidence = _evidence(
        (
            _row("2024-01-02", 25, 1000),
            _row("2024-01-03", 25, 4000, split=4),
        ),
        (
            {
                "session": "2024-01-03",
                "action_type": "split",
                "value": _hex(4),
            },
        ),
    )
    planes = HistoricalMarketPlanes.from_evidence(evidence)
    assert planes.policy_version == PRICE_VOLUME_PLANE_VERSION
    assert planes.currency == planes.quote_unit == "USD"
    assert all(row.evidence_revision == "revision-1" for row in planes.as_traded())

    as_traded = planes.as_traded()
    assert [row.close for row in as_traded] == [Decimal("100.0"), Decimal("25.0")]
    assert [row.volume for row in as_traded] == [Decimal("1000.0"), Decimal("4000.0")]

    continuous = planes.split_continuous_as_of(date(2024, 1, 3))
    assert [row.close for row in continuous] == [Decimal("25.0"), Decimal("25.0")]
    assert [row.volume for row in continuous] == [
        Decimal("4000.00000000"),
        Decimal("4000.00000000"),
    ]


def test_reverse_split_and_future_action_are_bounded_without_using_adj_close() -> None:
    evidence = _evidence(
        (
            _row("2024-01-02", 100, 1000),
            _row("2024-01-03", 100, 100, split=0.1),
            _row("2024-01-04", 50, 200, split=2),
        ),
        (
            {
                "session": "2024-01-03",
                "action_type": "split",
                "value": _hex(0.1),
            },
            {
                "session": "2024-01-04",
                "action_type": "split",
                "value": _hex(2),
            },
        ),
    )
    planes = HistoricalMarketPlanes.from_evidence(evidence)

    assert [row.close for row in planes.as_traded()] == [
        Decimal("20.00"),
        Decimal("200.0"),
        Decimal("50.0"),
    ]
    bounded = planes.split_continuous_as_of(date(2024, 1, 3))
    assert [row.session for row in bounded] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert [row.close for row in bounded] == [Decimal("200.0"), Decimal("200.0")]
    assert [action.session for action in planes.actions_as_of(date(2024, 1, 3))] == [
        date(2024, 1, 3)
    ]


def test_volume_zero_and_null_are_preserved() -> None:
    planes = HistoricalMarketPlanes.from_evidence(
        _evidence(
            (
                _row("2024-01-02", 25, 0),
                _row("2024-01-03", 25, None, split=4),
            ),
            (
                {
                    "session": "2024-01-03",
                    "action_type": "split",
                    "value": _hex(4),
                },
            ),
        )
    )
    rows = planes.split_continuous_as_of(date(2024, 1, 3))
    assert rows[0].volume == Decimal("0E-8")
    assert rows[1].volume is None


@pytest.mark.parametrize(
    ("case_id", "as_traded_before", "continuous_volume"),
    [
        ("ordinary_split", "100.0", "4000.00000000"),
        ("reverse_split", "10.00", "100.00000000"),
    ],
)
def test_qualification_split_fixtures_prove_price_and_volume_continuity(
    case_id: str, as_traded_before: str, continuous_volume: str
) -> None:
    case = next(
        item
        for item in json.loads(FIXTURE.read_text())["provider_cases"]
        if item["id"] == case_id
    )
    rows = tuple(
        {
            "session": session,
            "open": _hex(row["Open"]),
            "high": _hex(row["High"]),
            "low": _hex(row["Low"]),
            "close": _hex(row["Close"]),
            "adj_close": _hex(row["Adj Close"]),
            "volume": _hex(row["Volume"]),
            "dividends": _hex(row["Dividends"]),
            "stock_splits": _hex(row["Stock Splits"]),
        }
        for session, row in zip(case["expected_sessions"], case["rows"], strict=True)
    )
    actions = tuple(
        {
            "session": row["session"],
            "action_type": action_type,
            "value": row[field],
        }
        for row in rows
        for action_type, field in (
            ("dividend", "dividends"),
            ("split", "stock_splits"),
        )
        if float.fromhex(str(row[field])) > 0
    )
    planes = HistoricalMarketPlanes.from_evidence(_evidence(rows, actions))
    as_traded = planes.as_traded()
    continuous = planes.split_continuous_as_of(date(2024, 1, 3))

    assert as_traded[0].close == Decimal(as_traded_before)
    assert continuous[0].close == continuous[1].close
    assert continuous[0].volume == continuous[1].volume == Decimal(continuous_volume)


@pytest.mark.parametrize(
    ("ratio", "provider_close", "shares_before"),
    [(4.0, 25.0, "10"), (0.1, 100.0, "100")],
)
def test_split_accounting_and_as_traded_prices_preserve_market_value(
    ratio: float, provider_close: float, shares_before: str
) -> None:
    action_evidence: dict[str, object] = {
        "session": "2024-01-03",
        "action_type": "split",
        "value": _hex(ratio),
    }
    planes = HistoricalMarketPlanes.from_evidence(
        _evidence(
            (
                _row("2024-01-02", provider_close, 100),
                _row("2024-01-03", provider_close, 100, split=ratio),
            ),
            (action_evidence,),
        )
    )
    before_price, after_price = (row.close for row in planes.as_traded())
    action = planes.actions_as_of(date(2024, 1, 3))[0]
    before = PositionState(Decimal(shares_before), before_price)
    split_result = apply_split(
        before,
        action,
        quote_currency="USD",
        quote_unit="USD",
    )
    assert before.shares * before_price == split_result.position.shares * after_price


def test_action_projection_conflict_and_out_of_bounds_view_fail_closed() -> None:
    evidence = _evidence(
        (_row("2024-01-02", 25, 100, split=2),),
        (
            {
                "session": "2024-01-02",
                "action_type": "split",
                "value": _hex(4),
            },
        ),
    )
    with pytest.raises(MarketDataPolicyError, match="conflict") as conflict:
        HistoricalMarketPlanes.from_evidence(evidence)
    assert conflict.value.code == "integrity_error"

    planes = HistoricalMarketPlanes.from_evidence(
        _evidence((_row("2024-01-02", 25, 100),), ())
    )
    with pytest.raises(MarketDataPolicyError) as outside:
        planes.split_continuous_as_of(date(2024, 1, 6))
    assert outside.value.code == "integrity_error"


def test_plane_arithmetic_ignores_ambient_decimal_context() -> None:
    evidence = _evidence(
        (
            _row("2024-01-02", 33.333333333333336, 1000),
            _row("2024-01-03", 100, 300, split=0.3),
        ),
        (
            {
                "session": "2024-01-03",
                "action_type": "split",
                "value": _hex(0.3),
            },
        ),
    )
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        rows = HistoricalMarketPlanes.from_evidence(evidence).split_continuous_as_of(
            date(2024, 1, 3)
        )
    assert rows[0].volume == Decimal("300.00000000")
    assert rows[0].close == Decimal("33.333333333333336")


def test_each_price_plane_has_a_distinct_runtime_contract() -> None:
    planes = HistoricalMarketPlanes.from_evidence(
        _evidence((_row("2024-01-02", 25, 100),), ())
    )
    assert type(planes.provider_native()[0]) is ProviderNativeRow
    assert type(planes.as_traded()[0]) is AsTradedRow
    assert (
        type(planes.split_continuous_as_of(date(2024, 1, 2))[0]) is SplitContinuousRow
    )


def test_ohlc_fields_transform_independently_and_dividends_never_change_prices() -> (
    None
):
    planes = HistoricalMarketPlanes.from_evidence(
        _evidence(
            (
                _row(
                    "2024-01-02",
                    12,
                    100,
                    open_value=10,
                    high=14,
                    low=8,
                    dividend=3,
                ),
                _row("2024-01-03", 6, 200, split=2),
            ),
            (
                {
                    "session": "2024-01-02",
                    "action_type": "dividend",
                    "value": _hex(3),
                },
                {
                    "session": "2024-01-03",
                    "action_type": "split",
                    "value": _hex(2),
                },
            ),
        )
    )
    first = planes.as_traded()[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("20.0"),
        Decimal("28.0"),
        Decimal("16.0"),
        Decimal("24.0"),
    )


@pytest.mark.parametrize("encoding", ["1", " 0x1.0000000000000p+0", "0x1p+0"])
def test_provider_decimal_rejects_noncanonical_hex(encoding: str) -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        provider_decimal(encoding)
    assert exc_info.value.code == "integrity_error"


def test_provider_decimal_translates_float_overflow() -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        provider_decimal("0x1.0p+999999999999999999999")
    assert exc_info.value.code == "integrity_error"


@pytest.mark.parametrize("scale", [None, "sNaN", "Infinity"])
def test_malformed_quote_scale_fails_with_typed_integrity_error(scale: object) -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        HistoricalMarketPlanes.from_evidence(
            _evidence((_row("2024-01-02", 25, 100),), (), quote_unit_scale=scale)
        )
    assert exc_info.value.code == "integrity_error"


def test_incompatible_provider_request_contract_fails_closed() -> None:
    contract = dict(_evidence((_row("2024-01-02", 25, 100),), ()).request_contract)
    contract["auto_adjust"] = True
    with pytest.raises(MarketDataPolicyError) as exc_info:
        HistoricalMarketPlanes.from_evidence(
            _evidence((_row("2024-01-02", 25, 100),), (), request_contract=contract)
        )
    assert exc_info.value.code == "integrity_error"


def test_canonical_exchange_session_observation_policy_is_supported() -> None:
    contract = dict(_evidence((_row("2024-01-02", 25, 100),), ()).request_contract)
    contract["observation_policy"] = "canonical_exchange_sessions_v2"

    planes = HistoricalMarketPlanes.from_evidence(
        _evidence((_row("2024-01-02", 25, 100),), (), request_contract=contract)
    )

    assert planes.provider_native()[0].session == date(2024, 1, 2)


@pytest.mark.parametrize(
    "row",
    [
        _row("2024-01-02", -1, 100),
        _row("2024-01-02", 10, 100, high=9),
        _row("2024-01-02", 10, 100, low=11),
    ],
)
def test_non_positive_or_inconsistent_ohlc_fails_closed(row) -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        HistoricalMarketPlanes.from_evidence(_evidence((row,), ()))
    assert exc_info.value.code == "integrity_error"


@pytest.mark.parametrize(
    ("rows", "actions"),
    [
        ((_row("2024-01-02", 25, 100), _row("2024-01-02", 25, 100)), ()),
        (
            (_row("2024-01-02", 25, 100, split=0),),
            ({"session": "2024-01-02", "action_type": "split", "value": _hex(0)},),
        ),
        (
            (_row("2024-01-02", 25, 100),),
            ({"session": "2024-01-02", "action_type": "merger", "value": _hex(1)},),
        ),
        (({**_row("2024-01-02", 25, 100), "close": "not-hex"},), ()),
    ],
)
def test_malformed_or_unsupported_evidence_fails_visibly(rows, actions) -> None:
    with pytest.raises(MarketDataPolicyError) as exc_info:
        HistoricalMarketPlanes.from_evidence(_evidence(rows, actions))
    assert exc_info.value.code in {"integrity_error", "unsupported_corporate_action"}
