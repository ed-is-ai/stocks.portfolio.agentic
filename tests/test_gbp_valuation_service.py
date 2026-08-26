"""Unit tests for ``app.services.gbp_valuation_service``.

Story 1.6, AC1-3. Uses a real ``FxQuoteRepository`` against a temp
``trades.db`` (matching this codebase's established repository-testing
style) and an injected fake ticker factory / clock so no test touches
real yfinance or wall-clock time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
import threading

import pandas as pd
import pytest

from app.core.money import Money
from app.repositories import db
from app.repositories.fx_quote_repo import FxQuote, FxQuoteRepository
from app.services.gbp_valuation_service import (
    GbpValuationService,
    _quote_digest,
)


@pytest.fixture
def repo(tmp_path: Path) -> FxQuoteRepository:
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return FxQuoteRepository(connect)


class FakeTicker:
    """Stands in for ``yf.Ticker(pair)``; ``frame`` is what ``.history()``
    returns. ``None`` simulates a total fetch failure (raises)."""

    def __init__(self, frame: pd.DataFrame | None) -> None:
        self.frame = frame
        self.calls: list[dict[str, object]] = []

    def history(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        if self.frame is None:
            raise RuntimeError("simulated network failure")
        return self.frame


def _frame_for(as_of: str, close: float) -> pd.DataFrame:
    return pd.DataFrame({"Close": [close]}, index=pd.DatetimeIndex([as_of], tz="UTC"))


def _service(
    repo: FxQuoteRepository,
    frame: pd.DataFrame | None,
    *,
    today: str = "2026-08-12",
) -> tuple[GbpValuationService, list[str]]:
    calls: list[str] = []

    def ticker_factory(pair: str) -> Any:
        calls.append(pair)
        return FakeTicker(frame)

    clock = lambda: datetime.fromisoformat(f"{today}T12:00:00+00:00")  # noqa: E731
    return (
        GbpValuationService(repo, ticker_factory=ticker_factory, clock=clock),
        calls,
    )


def test_gbp_money_is_a_trivial_identity_projection_with_zero_fetches(
    repo: FxQuoteRepository,
) -> None:
    svc, calls = _service(repo, None)
    money = Money(amount=Decimal("42.50"), currency="GBP")

    projection = svc.value_in_gbp(money)

    assert projection.status == "valued"
    assert projection.quote is None
    assert projection.gbp_amount == Decimal("42.50")
    assert projection.reason is None
    assert calls == []


def test_non_gbp_money_values_correctly_from_a_live_quote(
    repo: FxQuoteRepository,
) -> None:
    svc, calls = _service(repo, _frame_for("2026-08-12", 1.25), today="2026-08-12")
    money = Money(amount=Decimal("125.00"), currency="USD")

    projection = svc.value_in_gbp(money)

    assert projection.status == "valued"
    assert projection.reason is None
    assert calls == ["GBPUSD=X"]
    assert projection.quote is not None
    assert projection.quote.pair == "GBPUSD=X"
    assert projection.quote.provider == "yfinance"
    assert projection.quote.as_of == "2026-08-12"
    assert projection.quote.rate == Decimal("1.25")
    assert projection.quote.digest == _quote_digest(
        "yfinance", "GBPUSD=X", "2026-08-12", Decimal("1.25")
    )
    # 125.00 / 1.25 = 100.00
    assert projection.gbp_amount == Decimal("100.00")


def test_second_call_same_pair_same_day_reuses_the_cached_quote(
    repo: FxQuoteRepository,
) -> None:
    svc, calls = _service(repo, _frame_for("2026-08-12", 1.25), today="2026-08-12")
    money = Money(amount=Decimal("100.00"), currency="USD")

    first = svc.value_in_gbp(money)
    second = svc.value_in_gbp(money)

    assert first.status == second.status == "valued"
    assert calls == ["GBPUSD=X"]  # only one live fetch, not two
    assert first.quote is not None and second.quote is not None
    assert first.quote.digest == second.quote.digest


def test_quote_from_another_provider_is_not_used_for_yfinance_valuation(
    repo: FxQuoteRepository,
) -> None:
    other = FxQuote(
        pair="GBPUSD=X",
        provider="other-provider",
        as_of="2026-08-12",
        rate=Decimal("1.25"),
        digest="other-provider-digest",
    )
    repo.insert_or_get(other)
    svc, calls = _service(repo, _frame_for("2026-08-12", 1.30))

    projection = svc.value_in_gbp(Money(amount=Decimal("100.00"), currency="USD"))

    assert projection.status == "valued"
    assert projection.quote is not None
    assert projection.quote.provider == "yfinance"
    assert projection.quote.rate == Decimal("1.30")
    assert calls == ["GBPUSD=X"]


def test_quote_dated_one_day_off_today_is_stale_not_a_grace_window(
    repo: FxQuoteRepository,
) -> None:
    svc, _calls = _service(repo, _frame_for("2026-08-11", 1.25), today="2026-08-12")
    money = Money(amount=Decimal("100.00"), currency="USD")

    projection = svc.value_in_gbp(money)

    assert projection.status == "valuation_unavailable"
    assert projection.reason == "stale"
    assert projection.gbp_amount is None
    assert projection.quote is None


def test_stale_attempt_is_negative_cached_across_restart(
    repo: FxQuoteRepository, tmp_path: Path
) -> None:
    svc, calls = _service(repo, _frame_for("2026-08-11", 1.25), today="2026-08-12")
    money = Money(amount=Decimal("100.00"), currency="USD")
    assert svc.value_in_gbp(money).reason == "stale"
    assert svc.value_in_gbp(money).reason == "stale"
    assert calls == ["GBPUSD=X"]

    # A fresh repository/service instance still observes the durable attempt.
    path = tmp_path / "restart.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    first, _ = _service(
        FxQuoteRepository(connect), _frame_for("2026-08-11", 1.25), today="2026-08-12"
    )
    assert first.value_in_gbp(money).reason == "stale"
    second, calls_after_restart = _service(
        FxQuoteRepository(connect), _frame_for("2026-08-12", 1.30), today="2026-08-12"
    )
    assert second.value_in_gbp(money).reason == "stale"
    assert calls_after_restart == []


def test_empty_and_malformed_responses_are_negative_cached(
    repo: FxQuoteRepository,
) -> None:
    money = Money(amount=Decimal("100.00"), currency="USD")
    for day, frame in (
        ("2026-08-12", pd.DataFrame()),
        (
            "2026-08-13",
            pd.DataFrame(
                {"Close": ["not-a-rate"]},
                index=pd.DatetimeIndex(["2026-08-13"], tz="UTC"),
            ),
        ),
    ):
        svc, calls = _service(repo, frame, today=day)
        assert svc.value_in_gbp(money).reason == "fetch_failed"
        assert svc.value_in_gbp(money).reason == "fetch_failed"
        assert calls == ["GBPUSD=X"]


def test_negative_cache_separates_pairs_and_rolls_over_by_date(
    repo: FxQuoteRepository,
) -> None:
    money_usd = Money(amount=Decimal("100.00"), currency="USD")
    money_eur = Money(amount=Decimal("100.00"), currency="EUR")
    usd, usd_calls = _service(repo, None, today="2026-08-12")
    eur, eur_calls = _service(repo, None, today="2026-08-12")
    assert usd.value_in_gbp(money_usd).reason == "fetch_failed"
    assert eur.value_in_gbp(money_eur).reason == "fetch_failed"
    assert usd_calls == ["GBPUSD=X"] and eur_calls == ["GBPEUR=X"]

    next_day, next_calls = _service(
        repo, _frame_for("2026-08-13", 1.3), today="2026-08-13"
    )
    assert next_day.value_in_gbp(money_usd).status == "valued"
    assert next_calls == ["GBPUSD=X"]


def test_concurrent_same_key_requests_make_one_provider_call(
    repo: FxQuoteRepository,
) -> None:
    svc, calls = _service(repo, None)
    money = Money(amount=Decimal("100.00"), currency="USD")
    results: list[str | None] = []

    def run() -> None:
        results.append(svc.value_in_gbp(money).reason)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["fetch_failed"] * 8
    assert calls == ["GBPUSD=X"]


def test_fetch_failure_is_valuation_unavailable_not_a_crash(
    repo: FxQuoteRepository,
) -> None:
    svc, _calls = _service(repo, None)
    money = Money(amount=Decimal("100.00"), currency="USD")

    projection = svc.value_in_gbp(money)

    assert projection.status == "valuation_unavailable"
    assert projection.reason == "fetch_failed"


def test_unsupported_currency_is_valuation_unavailable_never_raises(
    repo: FxQuoteRepository,
) -> None:
    svc, calls = _service(repo, None)
    money = Money(amount=Decimal("100.00"), currency="JPY")

    projection = svc.value_in_gbp(money)

    assert projection.status == "valuation_unavailable"
    assert projection.reason == "unsupported_pair"
    assert calls == []  # never even attempted a fetch for an unknown pair


def test_two_different_todays_produce_two_distinct_immutable_quotes(
    repo: FxQuoteRepository,
) -> None:
    money = Money(amount=Decimal("100.00"), currency="USD")

    svc1, _ = _service(repo, _frame_for("2026-08-12", 1.25), today="2026-08-12")
    first = svc1.value_in_gbp(money)

    svc2, _ = _service(repo, _frame_for("2026-08-13", 1.30), today="2026-08-13")
    second = svc2.value_in_gbp(money)

    assert first.quote is not None and second.quote is not None
    assert first.quote.digest != second.quote.digest
    assert first.quote.rate == Decimal("1.25")
    assert second.quote.rate == Decimal("1.30")


def test_insert_or_get_identical_quote_twice_is_idempotent(
    repo: FxQuoteRepository,
) -> None:
    """The digest-verification test: identical (provider, pair, as_of,
    rate) always produces the identical quote_digest, and re-inserting it
    is a no-op at the SQL layer."""
    quote_digest = _quote_digest("yfinance", "GBPUSD=X", "2026-08-12", Decimal("1.25"))

    quote = FxQuote(
        pair="GBPUSD=X",
        provider="yfinance",
        as_of="2026-08-12",
        rate=Decimal("1.25"),
        digest=quote_digest,
    )
    repo.insert_or_get(quote)
    repo.insert_or_get(quote)

    stored = repo.get_for_pair_and_date("GBPUSD=X", "2026-08-12")
    assert stored is not None
    assert stored.digest == quote_digest
