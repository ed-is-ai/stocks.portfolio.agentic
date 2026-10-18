"""Pinned UCITS ETF universe: load, acquire, BTC alignment, pin and launch."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

import app.services.backtest.backtest_launch_service as launch_module
from app.repositories import db
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.backtest_launch_service import (
    BacktestLaunchService,
    LaunchFieldError,
)
from app.services.backtest.etf_universe import (
    BITCOIN_SLEEVE_PARAM,
    ETF_EVIDENCE_REVISIONS_PARAM,
    ETF_UNIVERSE_DIGEST_PARAM,
    ETF_UNIVERSE_VERSION,
    ETF_UNIVERSE_VERSION_PARAM,
    EtfUniverse,
    EtfUniverseEvidenceService,
    align_btc_to_sessions,
    incomplete_months,
    load_etf_universe,
    validate_etf_universe_pin,
)
from app.services.backtest.historical_price_evidence import (
    YFinanceHistoricalEvidenceAdapter,
)
from app.services.backtest.trading_calendar import TradingCalendar

INCEPTION = date(2020, 1, 2)
END = date(2020, 3, 2)
_ENTRIES = [
    ("AAA.L", "GBP", "Europe/London", False),
    ("BBB.L", "GBp", "Europe/London", False),
    ("BTC-GBP", "GBP", "UTC", True),
]


def _entry(symbol: str, unit: str, tz: str, optional: bool) -> dict[str, object]:
    return {
        "symbol": symbol,
        "role": "test",
        "asset_class": "crypto" if optional else "equity",
        "currency": "GBP",
        "quote_unit": unit,
        "exchange_timezone": tz,
        "inception": INCEPTION.isoformat(),
        "optional": optional,
    }


def _write(path: Path, entries: list[dict[str, object]] | None = None) -> Path:
    payload = {
        "version": ETF_UNIVERSE_VERSION,
        "entries": entries if entries is not None else [_entry(*e) for e in _ENTRIES],
    }
    path.write_text(json.dumps(payload))
    return path


class _Ticker:
    def __init__(self, frame: pd.DataFrame, symbol: str, unit: str, tz: str) -> None:
        self._frame, self._meta = (
            frame,
            {
                "symbol": symbol,
                "currency": unit,
                "exchangeTimezoneName": tz,
            },
        )

    def history(self, **_kwargs: object) -> pd.DataFrame:
        return self._frame.copy()

    def get_history_metadata(self, repair: bool = False) -> dict[str, Any]:
        return self._meta


def _frame(days: list[date], tz: str) -> pd.DataFrame:
    values = [float(100 + i) for i in range(len(days))]
    return pd.DataFrame(
        {
            "Open": values,
            "High": [v + 1 for v in values],
            "Low": [v - 1 for v in values],
            "Close": values,
            "Adj Close": values,
            "Volume": [1_000.0] * len(values),
            "Dividends": [0.0] * len(values),
            "Stock Splits": [0.0] * len(values),
        },
        index=pd.DatetimeIndex([d.isoformat() for d in days], tz=tz),
    )


def _days(symbol: str, start: date = INCEPTION) -> list[date]:
    if symbol == "BTC-GBP":
        return [start + timedelta(n) for n in range((END - start).days)]
    return list(TradingCalendar().sessions_in_range("XLON", start, END))


def _service(
    tmp_path: Path,
    *,
    drop: dict[str, date] | None = None,
    start: dict[str, date] | None = None,
    failing: str | None = None,
) -> tuple[EtfUniverseEvidenceService, HistoricalPriceRepository]:
    tickers = {}
    for symbol, unit, tz, _optional in _ENTRIES:
        days = _days(symbol, (start or {}).get(symbol, INCEPTION))
        days = [d for d in days if d != (drop or {}).get(symbol)]
        tickers[symbol] = _Ticker(_frame(days, tz), symbol, unit, tz)

    def factory(symbol: str) -> _Ticker:
        if symbol == failing:
            raise RuntimeError("provider down")
        return tickers[symbol]

    prices = HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "p.db"))
    prices.ensure_schema()
    adapter = YFinanceHistoricalEvidenceAdapter(
        factory,
        provider_version="test",
        sleeper=lambda _seconds: None,
        clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc),
    )
    return EtfUniverseEvidenceService(prices, adapter), prices


def _universe(tmp_path: Path) -> EtfUniverse:
    return load_etf_universe(_write(tmp_path / "u.json"))


# --- load -----------------------------------------------------------------


def test_real_config_loads_twelve_symbols() -> None:
    universe = load_etf_universe()

    assert len(universe.digest) == 64
    assert [e.symbol for e in universe.entries if e.optional] == ["BTC-GBP"]
    assert {e.symbol for e in universe.entries} == {
        "VUSA.L", "XMWX.L", "VERX.L", "VJPN.L", "VFEM.L", "IBTL.L",
        "SGLO.L", "CMFP.L", "SGLN.L", "AGBP.L", "ERNS.L", "BTC-GBP",
    }  # fmt: skip


def test_digest_changes_with_file_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "u.json")
    before = load_etf_universe(path).digest
    path.write_text(path.read_text() + "\n")

    assert load_etf_universe(path).digest != before


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [_entry("A.L", "USD", "Europe/London", False)],
        [_entry("A.L", "GBP", "Europe/London", False)] * 2,
        [{**_entry("A.L", "GBP", "Europe/London", False), "inception": "x"}],
    ],
)
def test_malformed_universe_rejected(
    tmp_path: Path, entries: list[dict[str, object]]
) -> None:
    with pytest.raises(ValueError):
        load_etf_universe(_write(tmp_path / "u.json", entries))


# --- acquire --------------------------------------------------------------


def test_acquire_happy_path(tmp_path: Path) -> None:
    service, prices = _service(tmp_path)

    outcomes = service.acquire(_universe(tmp_path), end=END)

    assert [o.symbol for o in outcomes] == ["AAA.L", "BBB.L", "BTC-GBP"]
    assert all(o.error is None and not o.incomplete_months for o in outcomes)
    stored = prices.verify(cast(str, outcomes[1].data_revision))
    assert stored.security_id == "etf:BBB.L"
    assert (stored.currency, stored.quote_unit) == ("GBP", "GBp")
    assert stored.rows[0]["session"] == INCEPTION.isoformat()


def test_acquire_skips_optional_sleeve(tmp_path: Path) -> None:
    service, _prices = _service(tmp_path)

    outcomes = service.acquire(_universe(tmp_path), end=END, include_optional=False)

    assert [o.symbol for o in outcomes] == ["AAA.L", "BBB.L"]


def test_one_symbol_failure_does_not_abort_others(tmp_path: Path) -> None:
    service, _prices = _service(tmp_path, failing="AAA.L")

    failed, ok, btc = service.acquire(_universe(tmp_path), end=END)

    assert failed.data_revision is None and failed.error
    assert ok.data_revision and btc.data_revision


def test_late_inception_is_reported(tmp_path: Path) -> None:
    service, prices = _service(tmp_path, start={"BBB.L": date(2020, 1, 10)})

    outcome = service.acquire(_universe(tmp_path), end=END)[1]

    assert outcome.error == "inception mismatch: first session 2020-01-10"
    assert outcome.data_revision is None
    assert (
        prices.covering_revision(
            security_id="etf:BBB.L",
            requested_symbol="BBB.L",
            start=INCEPTION.isoformat(),
            end=END.isoformat(),
        )
        is None
    )


def test_end_on_or_before_inception_is_reported(tmp_path: Path) -> None:
    service, _prices = _service(tmp_path)

    outcomes = service.acquire(_universe(tmp_path), end=INCEPTION)

    assert {o.error for o in outcomes} == {"end precedes inception"}


def test_bitcoin_pin_requires_the_sleeve() -> None:
    revisions = {"AAA.L": "b" * 64, "BTC-GBP": "c" * 64}

    with pytest.raises(ValueError, match="bitcoin"):
        validate_etf_universe_pin(_pin(**{ETF_EVIDENCE_REVISIONS_PARAM: revisions}))
    validate_etf_universe_pin(
        _pin(**{ETF_EVIDENCE_REVISIONS_PARAM: revisions, BITCOIN_SLEEVE_PARAM: True})
    )


def test_gap_month_is_reported_not_filled(tmp_path: Path) -> None:
    gap = date(2020, 2, 12)
    service, prices = _service(tmp_path, drop={"AAA.L": gap})

    outcome = service.acquire(_universe(tmp_path), end=END)[0]

    assert outcome.incomplete_months == ("2020-02",)
    rows = prices.verify(cast(str, outcome.data_revision)).rows
    assert gap.isoformat() not in {row["session"] for row in rows}


def test_btc_missing_bar_leaves_next_session_incomplete(tmp_path: Path) -> None:
    service, _prices = _service(tmp_path, drop={"BTC-GBP": date(2020, 2, 9)})

    outcome = service.acquire(_universe(tmp_path), end=END)[2]

    assert outcome.incomplete_months == ("2020-02",)


# --- pure helpers ---------------------------------------------------------


def test_btc_alignment_uses_prior_utc_bar() -> None:
    rows = [
        {"session": "2020-02-06", "close": (10.0).hex()},
        {"session": "2020-02-07", "close": (11.0).hex()},
        {"session": "2020-02-09", "close": (13.0).hex()},
    ]
    sessions = [date(2020, 2, 7), date(2020, 2, 10), date(2020, 2, 11)]

    # 2020-02-10 needs the 2020-02-09 bar; 2020-02-11 needs the missing 10th.
    assert align_btc_to_sessions(rows, sessions) == {
        date(2020, 2, 7): 10.0,
        date(2020, 2, 10): 13.0,
    }


def test_incomplete_months_lists_only_months_with_missing_sessions() -> None:
    expected = [date(2020, 1, 31), date(2020, 2, 3), date(2020, 3, 2)]

    assert incomplete_months([date(2020, 1, 31)], expected) == ("2020-02", "2020-03")


# --- pin validation -------------------------------------------------------


def _pin(**overrides: object) -> dict[str, object]:
    return {
        ETF_UNIVERSE_VERSION_PARAM: ETF_UNIVERSE_VERSION,
        ETF_UNIVERSE_DIGEST_PARAM: "a" * 64,
        ETF_EVIDENCE_REVISIONS_PARAM: {"AAA.L": "b" * 64},
        **overrides,
    }


def test_valid_pin_and_absent_pin_pass() -> None:
    validate_etf_universe_pin(_pin())
    validate_etf_universe_pin({"other": 1})


@pytest.mark.parametrize(
    "parameters",
    [
        {ETF_UNIVERSE_DIGEST_PARAM: "a" * 64},
        {ETF_EVIDENCE_REVISIONS_PARAM: {"AAA.L": "b" * 64}},
        _pin(**{ETF_UNIVERSE_VERSION_PARAM: "etf_universe.ucits.v0"}),
        _pin(**{ETF_UNIVERSE_DIGEST_PARAM: "nothex"}),
        _pin(**{ETF_EVIDENCE_REVISIONS_PARAM: {}}),
        _pin(**{ETF_EVIDENCE_REVISIONS_PARAM: {"AAA.L": "short"}}),
    ],
)
def test_malformed_or_stray_pin_rejected(parameters: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_etf_universe_pin(parameters)


# --- launch binding -------------------------------------------------------


def _bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prices: HistoricalPriceRepository,
    parameters: dict[str, Any],
    months: tuple[str, str] = ("2020-01", "2020-02"),
) -> tuple[dict[str, Any], list[LaunchFieldError]]:
    universe = _universe(tmp_path)
    monkeypatch.setattr(launch_module, "load_etf_universe", lambda: universe)
    service = BacktestLaunchService.__new__(BacktestLaunchService)
    service._historical_price_repo = prices
    strategy = SimpleNamespace(
        parameters=(SimpleNamespace(name=ETF_UNIVERSE_VERSION_PARAM),)
    )
    command = SimpleNamespace(start_month=months[0], end_month=months[1])
    errors: list[LaunchFieldError] = []
    bound = service._bind_etf_universe_pin(
        cast(Any, strategy), cast(Any, command), parameters, errors
    )
    return bound, errors


_OPT_IN = {ETF_UNIVERSE_VERSION_PARAM: ETF_UNIVERSE_VERSION}


def test_bind_seals_digest_and_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, prices = _service(tmp_path)
    outcomes = service.acquire(_universe(tmp_path), end=END)

    bound, errors = _bind(
        tmp_path, monkeypatch, prices, {**_OPT_IN, BITCOIN_SLEEVE_PARAM: True}
    )

    assert errors == []
    assert bound[ETF_UNIVERSE_DIGEST_PARAM] == _universe(tmp_path).digest
    assert bound[ETF_EVIDENCE_REVISIONS_PARAM] == {
        o.symbol: o.data_revision for o in outcomes
    }
    validate_etf_universe_pin(bound)


def test_bind_excludes_bitcoin_unless_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, prices = _service(tmp_path)
    service.acquire(_universe(tmp_path), end=END, include_optional=False)

    bound, errors = _bind(tmp_path, monkeypatch, prices, dict(_OPT_IN))

    assert errors == []
    assert set(bound[ETF_EVIDENCE_REVISIONS_PARAM]) == {"AAA.L", "BBB.L"}


def test_bind_reports_missing_evidence_per_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, prices = _service(tmp_path, failing="AAA.L")
    service.acquire(_universe(tmp_path), end=END)

    _bound, errors = _bind(tmp_path, monkeypatch, prices, dict(_OPT_IN))

    assert [e.field for e in errors] == ["form"]
    assert "AAA.L: no acquired evidence" in errors[0].message


def test_bind_rejects_incomplete_run_month_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, prices = _service(tmp_path, drop={"BBB.L": date(2020, 2, 12)})
    service.acquire(_universe(tmp_path), end=END)

    _bound, errors = _bind(tmp_path, monkeypatch, prices, dict(_OPT_IN))
    _bound, january = _bind(
        tmp_path, monkeypatch, prices, dict(_OPT_IN), ("2020-01", "2020-01")
    )

    assert len(errors) == 1 and "BBB.L: incomplete months 2020-02" in errors[0].message
    assert january == []


def test_bind_is_noop_without_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BacktestLaunchService.__new__(BacktestLaunchService)
    errors: list[LaunchFieldError] = []

    bound = service._bind_etf_universe_pin(
        cast(Any, SimpleNamespace(parameters=())), cast(Any, None), {"x": 1}, errors
    )

    assert bound == {"x": 1} and errors == []


def test_etf_universe_stays_outside_ingestion_identity() -> None:
    """The ETF reference must never feed ``yfinance_ingestion_version`` (#117)."""
    from app.services.backtest.source_manifest import _YFINANCE_INGESTION_ALLOWLIST

    assert not any("etf_universe" in path for path in _YFINANCE_INGESTION_ALLOWLIST)


def test_bind_reports_failed_verification_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.historical_price_repo import (
        HistoricalEvidenceIntegrityError,
    )

    service, prices = _service(tmp_path)
    service.acquire(_universe(tmp_path), end=END)

    def corrupt(_revision: str) -> None:
        raise HistoricalEvidenceIntegrityError("digest mismatch")

    monkeypatch.setattr(prices, "verify", corrupt)
    _bound, errors = _bind(tmp_path, monkeypatch, prices, dict(_OPT_IN))

    assert len(errors) == 2
    assert all("failed verification" in e.message for e in errors)
