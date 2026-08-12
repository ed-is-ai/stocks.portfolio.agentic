from __future__ import annotations

from datetime import date

import re

import pytest

from app.services.backtest.trading_calendar import (
    CalendarContractError,
    TradingCalendar,
)


@pytest.fixture(scope="module")
def calendars() -> TradingCalendar:
    return TradingCalendar()


@pytest.mark.parametrize(
    ("mic", "month", "expected"),
    [
        ("XNYS", "2024-04", "2024-04-30"),
        ("XNYS", "2021-05", "2021-05-28"),
        ("XLON", "2024-04", "2024-04-30"),
        ("XLON", "2021-05", "2021-05-28"),
    ],
)
def test_last_completed_session_uses_exchange_month_end(
    calendars: TradingCalendar, mic: str, month: str, expected: str
) -> None:
    assert calendars.last_session_of_month(mic, month).isoformat() == expected


def test_closed_mic_mapping(calendars: TradingCalendar) -> None:
    assert calendars.calendar_name("XNAS") == "XNYS"
    assert calendars.calendar_name("XNYS") == "XNYS"
    assert calendars.calendar_name("XLON") == "XLON"
    with pytest.raises(ValueError, match="Unsupported MIC"):
        calendars.calendar_name("XPAR")


@pytest.mark.parametrize(
    ("mic", "session", "expected_close"),
    [
        ("XNYS", "2024-11-29", "18:00:00+00:00"),
        ("XLON", "2024-12-24", "12:30:00+00:00"),
    ],
)
def test_early_close_fixtures(
    calendars: TradingCalendar, mic: str, session: str, expected_close: str
) -> None:
    assert str(calendars.session_close(mic, session).timetz()) == expected_close


@pytest.mark.parametrize(
    ("mic", "closed_date"),
    [("XNYS", "2018-12-05"), ("XLON", "2022-09-19")],
)
def test_unscheduled_closure_fixtures(
    calendars: TradingCalendar, mic: str, closed_date: str
) -> None:
    assert not calendars.is_session(mic, closed_date)


def test_dst_changes_utc_open_without_changing_local_session(
    calendars: TradingCalendar,
) -> None:
    assert (
        str(calendars.session_open("XNYS", "2024-03-08").timetz()) == "14:30:00+00:00"
    )
    assert (
        str(calendars.session_open("XNYS", "2024-03-11").timetz()) == "13:30:00+00:00"
    )
    assert (
        str(calendars.session_open("XLON", "2024-03-28").timetz()) == "08:00:00+00:00"
    )
    assert (
        str(calendars.session_open("XLON", "2024-04-02").timetz()) == "07:00:00+00:00"
    )


def test_session_table_digest_is_stable(calendars: TradingCalendar) -> None:
    first = calendars.session_table_digest()
    second = TradingCalendar().session_table_digest()
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert first == "f9ea088dec004ee6ee86542c68a2afc686cb9f60076e19c925eea26757c2d737"


def test_calendar_authority_uses_fixed_bounds_not_rolling_today_window(
    calendars: TradingCalendar,
) -> None:
    schedule = calendars._calendar("XNYS").schedule
    assert schedule.index.min().year == 1970
    assert schedule.index.max().year == 2100


@pytest.mark.parametrize(
    "month", ["2024-1", "24-01", "2024-00", "2024-13", "٢٠٢٤-01", "x"]
)
def test_strict_closed_month_parser_rejects_malformed_values(
    calendars: TradingCalendar, month: str
) -> None:
    with pytest.raises(CalendarContractError, match="Malformed"):
        calendars.closed_month(month, as_of=date(2026, 8, 11))


def test_current_and_future_months_are_rejected_with_injected_date(
    calendars: TradingCalendar,
) -> None:
    assert calendars.closed_month("2026-07", as_of=date(2026, 8, 11)) == "2026-07"
    with pytest.raises(CalendarContractError, match="fully closed"):
        calendars.closed_month("2026-08", as_of=date(2026, 8, 11))
    with pytest.raises(CalendarContractError, match="fully closed"):
        calendars.closed_month("2026-09", as_of=date(2026, 8, 11))


def test_canonical_sessions_and_inclusive_calendar_month_ranges(
    calendars: TradingCalendar,
) -> None:
    sessions = calendars.month_sessions(
        ("XLON", "XNAS", "XNYS"), "2024-11", as_of=date(2024, 12, 1)
    )
    assert tuple(sessions) == ("XLON", "XNAS", "XNYS")
    assert sessions["XNAS"] == sessions["XNYS"] == date(2024, 11, 29)
    assert sessions["XLON"] == date(2024, 11, 29)
    assert calendars.months_inclusive("2023-12", "2024-02") == (
        "2023-12",
        "2024-01",
        "2024-02",
    )
    assert calendars.contiguous_month_intervals(
        ("2024-04", "2023-12", "2024-02", "2024-01")
    ) == (("2023-12", "2024-02"), ("2024-04", "2024-04"))
