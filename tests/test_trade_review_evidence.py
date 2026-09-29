"""Tests for the read-only, as-of trade-review evidence reader (GH-17).

The price side runs against a tmp historical price cache committed with the
same fixture helper the portfolio-history tests use, then reopened
``mode=ro``; the backtest side is a stub exposing only the read methods the
reader calls. The real stores are never opened.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agents.trade_review.checklist import review_buy
from app.agents.trade_review.evidence import (
    StoreEvidenceReader,
    open_store_reader,
    provider_candidates,
    read_only_connect,
    store_revision,
)
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.schemas import Trade
from app.services.backtest.trading_calendar import TradingCalendar
from tests.test_portfolio_history_recommendation import _commit_history, _repo

ALIASES = {"WCOG": "WCOG.L"}


def _backtest(identities=(), record=None, profile="profile-1") -> MagicMock:
    backtest = MagicMock()
    backtest.identity_rows.return_value = list(identities)
    backtest.active_snapshot_profile.return_value = (
        None if profile is None else SimpleNamespace(profile_hash=profile)
    )
    backtest.latest_committed_scan_result.return_value = record
    return backtest


def _read_only(tmp_path: Path) -> HistoricalPriceRepository:
    return HistoricalPriceRepository(
        read_only_connect(tmp_path / "historical-prices.db")
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lse_sessions(count: int) -> tuple[date, ...]:
    days = TradingCalendar().sessions_in_range(
        "XLON", date(2024, 1, 2), date(2024, 1, 2) + timedelta(days=count * 2)
    )
    return tuple(days[:count])


def test_bars_are_bounded_scaled_to_pounds_and_read_only(tmp_path: Path) -> None:
    days = _lse_sessions(12)
    _commit_history(_repo(tmp_path), "WCOG.L", days, quote_unit="GBp")
    path = tmp_path / "historical-prices.db"
    before = _digest(path)
    reader = StoreEvidenceReader(
        _read_only(tmp_path), _backtest(), aliases=ALIASES, provider_aliases={}
    )

    security = reader.resolve("WCOG", "GBP")
    assert security == "portfolio:WCOG.L"
    window = reader.bars(security, days[5], 3)
    reader.close()

    assert window is not None
    assert [b.session for b in window.bars] == list(days[3:6])
    assert window.bars[-1].close == Decimal("14.21")  # 1421 pence
    assert all(b.split_factor == 1 for b in window.bars)
    assert not reader.failed
    assert _digest(path) == before


def test_resolve_maps_marked_and_bare_symbols() -> None:
    backtest = _backtest(
        identities=[
            ("uuid-bp", "XLON", "BP.L", "d"),
            ("uuid-azn-us", "XNAS", "AZN", "d"),
            ("uuid-azn-l", "XLON", "AZN.L", "d"),
            ("uuid-bfb", "XNYS", "BF-B", "d"),
        ]
    )
    reader = StoreEvidenceReader(
        MagicMock(), backtest, aliases={}, provider_aliases={"BF.B": "BF-B"}
    )
    reader._prices.latest_revisions_for_securities.return_value = ()

    assert reader.resolve("BP.", "GBP") == "uuid-bp"
    # A bare symbol that exists is that symbol, even for a sterling trade.
    assert reader.resolve("AZN", "GBP") == "uuid-azn-us"
    assert reader.resolve("AZN", "USD") == "uuid-azn-us"
    assert reader.resolve("BF.B", "USD") == "uuid-bfb"
    assert reader.resolve("NOPE", "USD") is None
    assert provider_candidates("VOD.L", "GBP") == ("VOD.L",)


def test_scan_is_read_as_of_the_given_session_in_major_units() -> None:
    record = SimpleNamespace(
        snapshot_month="2024-05",
        as_of_session_date=date(2024, 5, 31),
        currency="GBP",
        quote_unit="GBp",
        stage=SimpleNamespace(value="Stage 2"),
        vcp=SimpleNamespace(pivot_price=Decimal("259")),
    )
    backtest = _backtest(record=record)
    reader = StoreEvidenceReader(MagicMock(), backtest, aliases={}, provider_aliases={})

    scan = reader.scan("uuid-1", date(2024, 6, 3))

    assert scan is not None and scan.pivot == Decimal("2.59")
    backtest.latest_committed_scan_result.assert_called_once_with(
        profile_hash="profile-1", security_id="uuid-1", as_of_session=date(2024, 6, 3)
    )
    no_profile = StoreEvidenceReader(
        MagicMock(), _backtest(profile=None), aliases={}, provider_aliases={}
    )
    assert no_profile.scan("uuid-1", date(2024, 6, 3)) is None


def test_missing_stores_read_as_unknown_and_create_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    prices = tmp_path / "absent" / "historical_price_cache.db"
    backtest = tmp_path / "absent" / "backtest.db"
    monkeypatch.setattr("app.core.config.HISTORICAL_PRICE_CACHE", prices)
    monkeypatch.setattr("app.core.config.BACKTEST_DB", backtest)
    reader = open_store_reader()
    trade = Trade(
        id=1,
        ticker="ZETA",
        action="BUY",
        shares=1,
        price=10.0,
        date="2024-06-03",
        portfolio_id=1,
        currency="USD",
    )

    review = review_buy(trade, reader, annotation=None, lot_open=True, history=[])
    reader.close()

    assert reader.failed
    statuses = {c.kind: c.status for c in review.checks}
    assert statuses["valid_setup"] == "unknown"
    assert statuses["entry_location"] == "unknown"
    assert statuses["exit_signal"] == "unknown"
    assert not prices.parent.exists()


def test_a_locked_or_broken_store_is_caught() -> None:
    prices = MagicMock()
    prices.latest_revisions_for_securities.side_effect = RuntimeError("locked")
    backtest = _backtest()
    backtest.latest_committed_scan_result.side_effect = RuntimeError("locked")
    reader = StoreEvidenceReader(prices, backtest, aliases={}, provider_aliases={})

    assert reader.resolve("ZETA", "USD") is None
    assert reader.scan("uuid-1", date(2024, 6, 3)) is None
    assert reader.failed


def test_bare_sterling_symbol_resolves_to_itself_not_the_lse_line() -> None:
    backtest = _backtest(
        identities=[
            ("uuid-boeing", "XNYS", "BA", "d"),
            ("uuid-bae", "XLON", "BA.L", "d"),
            ("uuid-vod-l", "XLON", "VOD.L", "d"),
        ]
    )
    reader = StoreEvidenceReader(MagicMock(), backtest, aliases={}, provider_aliases={})

    assert reader.resolve("BA", "GBP") == "uuid-boeing"
    assert reader.resolve("BA.", "GBP") == "uuid-bae"
    assert reader.resolve("BA.L", "USD") == "uuid-bae"
    assert reader.resolve("VOD", "GBP") == "uuid-vod-l"
    assert provider_candidates("BA", "GBP") == ("BA", "BA.L")
    assert provider_candidates("BA.", "GBP") == ("BA.L",)
    assert provider_candidates("BA", "USD") == ("BA",)


def test_windows_carry_the_line_currency_and_split_reads_stay_in_bounds(
    tmp_path: Path,
) -> None:
    days = _lse_sessions(12)
    _commit_history(_repo(tmp_path), "WCOG.L", days, quote_unit="GBp")
    reader = StoreEvidenceReader(
        _read_only(tmp_path), _backtest(), aliases=ALIASES, provider_aliases={}
    )

    window = reader.bars("portfolio:WCOG.L", days[5], 3)
    on_session = reader.split_on("portfolio:WCOG.L", days[6])
    outside = reader.split_on("portfolio:WCOG.L", days[-1] + timedelta(days=30))
    reader.close()

    assert window is not None and window.currency == "GBP"
    assert on_session == Decimal(1)
    assert outside is None
    assert not reader.failed


def test_store_revision_stats_the_configured_stores_without_opening(
    tmp_path: Path, monkeypatch
) -> None:
    prices = tmp_path / "prices.db"
    monkeypatch.setattr("app.core.config.HISTORICAL_PRICE_CACHE", prices)
    monkeypatch.setattr("app.core.config.BACKTEST_DB", tmp_path / "backtest.db")

    before = store_revision()
    prices.write_bytes(b"x")
    after = store_revision()

    assert before == (None, None, None, None)
    assert after[0] is not None and after[1:] == (None, None, None)
    assert not (tmp_path / "backtest.db").exists()
