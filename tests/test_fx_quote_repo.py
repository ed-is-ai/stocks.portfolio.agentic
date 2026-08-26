"""Unit tests for ``FxQuoteRepository`` (Story 1.6, AC1-2)."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.repositories import db
from app.repositories.fx_quote_repo import (
    FxQuote,
    FxQuoteRepository,
    FxUnavailableAttempt,
)


@pytest.fixture
def repo(tmp_path: Path) -> FxQuoteRepository:
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return FxQuoteRepository(connect)


def _quote(as_of: str, rate: str = "1.25", pair: str = "GBPUSD=X") -> FxQuote:
    from app.services.gbp_valuation_service import _quote_digest

    return FxQuote(
        pair=pair,
        provider="yfinance",
        as_of=as_of,
        rate=Decimal(rate),
        digest=_quote_digest("yfinance", pair, as_of, Decimal(rate)),
    )


def test_get_for_pair_and_date_is_an_exact_match_not_latest(
    repo: FxQuoteRepository,
) -> None:
    repo.insert_or_get(_quote("2026-08-10"))

    assert repo.get_for_pair_and_date("GBPUSD=X", "2026-08-10") is not None
    # A stored quote for one date must never satisfy a query for another --
    # this is an exact-date lookup, never "most recent for this pair".
    assert repo.get_for_pair_and_date("GBPUSD=X", "2026-08-11") is None


def test_get_for_pair_and_date_is_scoped_per_pair(repo: FxQuoteRepository) -> None:
    repo.insert_or_get(_quote("2026-08-10", pair="GBPUSD=X"))

    assert repo.get_for_pair_and_date("GBPEUR=X", "2026-08-10") is None


def test_insert_or_get_is_idempotent_at_the_sql_layer(
    repo: FxQuoteRepository, tmp_path: Path
) -> None:
    quote = _quote("2026-08-10")
    repo.insert_or_get(quote)
    repo.insert_or_get(quote)
    repo.insert_or_get(quote)

    with db.session(repo._connect) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM fx_quotes WHERE quote_digest = ?",
            (quote.digest,),
        ).fetchone()[0]
    assert count == 1


def test_different_rate_same_day_produces_a_second_row_not_an_overwrite(
    repo: FxQuoteRepository,
) -> None:
    first = _quote("2026-08-10", rate="1.25")
    second = _quote("2026-08-10", rate="1.30")
    repo.insert_or_get(first)
    repo.insert_or_get(second)

    with db.session(repo._connect) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM fx_quotes WHERE pair = ? AND as_of = ?",
            ("GBPUSD=X", "2026-08-10"),
        ).fetchone()[0]
    assert count == 2
    # Both rows are independently retrievable by their own digest -- an
    # exact-date read returns *a* valid quote for that day, never silently
    # dropping either one.
    stored = repo.get_for_pair_and_date("GBPUSD=X", "2026-08-10")
    assert stored is not None
    assert stored.digest in {first.digest, second.digest}


def test_unavailable_attempt_is_durable_and_idempotent(repo: FxQuoteRepository) -> None:
    attempt = FxUnavailableAttempt(
        provider="yfinance",
        pair="GBPUSD=X",
        requested_date="2026-08-10",
        reason="stale",
    )

    assert repo.record_unavailable_attempt(attempt) == attempt
    assert repo.record_unavailable_attempt(attempt) == attempt
    assert repo.get_unavailable_attempt("yfinance", "GBPUSD=X", "2026-08-10") == attempt

    with db.session(repo._connect) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM fx_unavailable_attempts").fetchone()[0]
            == 1
        )


def test_unavailable_attempt_identity_is_scoped_by_provider_pair_and_date(
    repo: FxQuoteRepository,
) -> None:
    base = FxUnavailableAttempt(
        provider="yfinance",
        pair="GBPUSD=X",
        requested_date="2026-08-10",
        reason="stale",
    )
    repo.record_unavailable_attempt(base)
    repo.record_unavailable_attempt(base.model_copy(update={"provider": "other"}))
    repo.record_unavailable_attempt(base.model_copy(update={"pair": "GBPEUR=X"}))
    repo.record_unavailable_attempt(
        base.model_copy(update={"requested_date": "2026-08-11"})
    )

    with db.session(repo._connect) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM fx_unavailable_attempts").fetchone()[0]
            == 4
        )
