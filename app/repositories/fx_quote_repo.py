"""Repository for the ``fx_quotes`` table in ``trades.db``.

Immutable, content-addressed FX-quote evidence (Story 1.6, AD-24): a quote
is written once at valuation/render time (never at import time, unlike
Story 1.4's ``cash_balances``), keyed by a digest of its own content so an
identical ``(provider, pair, as_of, rate)`` re-insert is a no-op and a
different rate for the same day naturally produces a new row rather than
overwriting the old one.
"""

from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.repositories.db import Connect, session


class FxQuote(BaseModel):
    """An immutable, provider-attributed FX quote (Story 1.6, AD-24).

    Defined here (not in ``gbp_valuation_service.py``, despite that being
    this story's primary spec location for it) so the repository layer has
    no dependency on the service layer -- ``GbpValuationService`` imports
    this type from here instead, keeping the usual Services -> Repositories
    direction intact.
    """

    model_config = ConfigDict(frozen=True)

    pair: str
    provider: str = "yfinance"
    as_of: str
    rate: Decimal
    digest: str


def _row_to_quote(row: tuple[str, str, str, str, str, str]) -> FxQuote:
    quote_digest, provider, pair, as_of, rate, _fetched_at = row
    return FxQuote(
        pair=pair,
        provider=provider,
        as_of=as_of,
        rate=Decimal(rate),
        digest=quote_digest,
    )


class FxQuoteRepository:
    """Typed access to the ``fx_quotes`` table.

    Plain reads/writes with its own ``session()`` -- unlike Story 1.4's
    ``CashBalancesRepository.upsert_on_connection``, this table is written
    at render/valuation time, not import time, so it never needs to join
    the SIPP import's transaction.
    """

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def get_for_pair_and_date(self, pair: str, as_of: str) -> FxQuote | None:
        """Return the stored quote for the exact ``(pair, as_of)``, or None.

        Exact-date match only -- never "most recent for this pair" (Story
        1.6, AC2: same-day only, no grace window).
        """
        with session(self._connect) as conn:
            row = conn.execute(
                "SELECT quote_digest, provider, pair, as_of, rate, fetched_at "
                "FROM fx_quotes WHERE pair = ? AND as_of = ? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (pair, as_of),
            ).fetchone()
        return _row_to_quote(row) if row else None

    def insert_or_get(self, quote: FxQuote) -> None:
        """Persist ``quote``, keyed on its content digest.

        ``INSERT OR IGNORE`` on ``quote_digest``: identical content is a
        no-op (the row already exists), and a different rate for the same
        day produces a new row (a different digest) rather than
        overwriting the old one -- immutability via content-addressing.
        """
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with session(self._connect) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fx_quotes "
                "(quote_digest, provider, pair, as_of, rate, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    quote.digest,
                    quote.provider,
                    quote.pair,
                    quote.as_of,
                    str(quote.rate),
                    fetched_at,
                ),
            )
