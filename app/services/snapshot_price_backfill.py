"""Fetch-on-miss historical price evidence for traded (non-roster) tickers (#490).

``historical_price_cache`` is only ever populated by backtest roster
acquisition, so a ticker a user traded but never backtested has no evidence
acquisition path at all -- its portfolio-chart dates stay ``NULL`` forever.
This service is the missing acquisition path: one bulk fetch per ticker,
spanning first-trade-date through the latest snapshot date needing repair,
committed under a ``portfolio:<ticker>`` security identity so it never
satisfies or pollutes backtest's own exact-identity evidence lookups (those
are always keyed by the roster's real ``security_id``).

Read-only ``HistoricalCacheGbpPriceSource`` stays untouched; this is a
separate collaborator that ``SnapshotRepairService`` calls first, once per
distinct held ticker, before its existing read-only reconstruction loop.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging

from app.core.ticker_identity import canonicalize_or_fallback, load_aliases
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    FX_PAIR,
    FX_SERIES_SECURITY_ID,
    HistoricalEvidenceRequest,
    YFinanceFxSeriesFetcher,
    YFinanceHistoricalEvidenceAdapter,
)

logger = logging.getLogger(__name__)

#: The pseudo-security namespace evidence acquired here is committed under,
#: mirroring ``FX_SERIES_SECURITY_ID``'s ``"fx:GBPUSD=X"`` pattern -- never
#: a real roster ``security_id``, so a backtest lookup (always keyed by its
#: own real identity) can never match a row this service wrote.
_SECURITY_ID_PREFIX = "portfolio:"

#: Failure codes that mean "this exact request will never succeed" rather
#: than "try again later". ``REQUIRED_DATA_MISSING`` is a well-formed but
#: empty response (the provider understood the request); a delisted or
#: never-existed symbol instead raises before any response is parsed
#: (yfinance's ``YFTzMissingError``/"possibly delisted"), which
#: ``_classify_exception`` maps to ``PROVIDER_CONTRACT_ERROR`` -- the same
#: shape a genuinely malformed response takes, so both must be treated as
#: definitive here (verified against a live delisted ticker, GH-490).
#: ``PROVIDER_UNAVAILABLE``/``PROVIDER_THROTTLED`` are the only codes that
#: mean a transient, retryable condition (network/rate-limit).
_DEFINITIVE_FAILURE_CODES = frozenset(
    {FailureCode.REQUIRED_DATA_MISSING, FailureCode.PROVIDER_CONTRACT_ERROR}
)


class PriceEvidenceUnavailable(RuntimeError):
    """The provider has no rows for this ticker's range -- permanent.

    Raised only when *this call* recorded the negative-cache row; a ticker
    whose unavailable attempt was already recorded on an earlier run is
    silently skipped (``ensure_coverage`` returns ``False``) instead of
    raising again, so a caller can tell "newly unavailable this run" (surface
    it) apart from "still unavailable, as expected" (stay quiet).
    """


class PriceEvidenceBackfillService:
    """Ensures one ticker's historical price range has committed evidence.

    ``ensure_coverage`` is the only entry point: it checks the negative
    cache, then an existing covering revision, and only fetches when
    neither exists. A definitive failure (see
    :data:`_DEFINITIVE_FAILURE_CODES`) is recorded permanently and raised
    as :class:`PriceEvidenceUnavailable`; any other failure is transient
    and propagates as-is, to be retried on the next run.
    """

    def __init__(
        self,
        prices: HistoricalPriceRepository,
        adapter: YFinanceHistoricalEvidenceAdapter | None = None,
        aliases: dict[str, str] | None = None,
        fx_fetcher: YFinanceFxSeriesFetcher | None = None,
    ) -> None:
        self._prices = prices
        self._adapter = adapter or YFinanceHistoricalEvidenceAdapter()
        self._aliases = load_aliases() if aliases is None else aliases
        self._fx_fetcher = fx_fetcher or YFinanceFxSeriesFetcher()

    def ensure_coverage(self, ticker: str, start: date, end: date) -> bool:
        """Return True if a fetch happened, False if it was skipped.

        ``[start, end)`` -- ``end`` is exclusive, never "today". Skipped
        means either an existing revision already covers the range or this
        ticker has a previously recorded permanent failure. Raises
        :class:`PriceEvidenceUnavailable` on a newly recorded permanent
        failure, or the underlying error for a transient one.
        """
        symbol = canonicalize_or_fallback(
            ticker,
            self._aliases,
            logger=logger,
            context="price evidence backfill",
        )
        security_id = f"{_SECURITY_ID_PREFIX}{symbol}"

        if self._prices.get_unavailable_attempt(security_id) is not None:
            return False
        if (
            self._prices.covering_revision(
                security_id=security_id,
                requested_symbol=symbol,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            is not None
        ):
            return False

        request = HistoricalEvidenceRequest(
            security_id=security_id,
            alias_revision=None,
            symbol=symbol,
            start=start,
            end=end,
            expected_sessions=tuple(
                start + timedelta(days=offset) for offset in range((end - start).days)
            ),
            allowed_observed_symbols=(symbol,),
            canonical_exchange_sessions=True,
        )
        try:
            payload = self._adapter.fetch(request)
        except ProviderFailure as exc:
            if exc.code in _DEFINITIVE_FAILURE_CODES:
                self._prices.record_unavailable_attempt(
                    security_id=security_id,
                    requested_symbol=symbol,
                    reason=str(exc),
                )
                raise PriceEvidenceUnavailable(str(exc)) from exc
            raise
        self._prices.commit(payload)
        return True

    def ensure_fx_coverage(self, start: date, end: date) -> bool:
        """Return True if the ``GBPUSD=X`` series was fetched, False if skipped.

        Mirrors :meth:`ensure_coverage`'s negative-cache/covering-revision/
        fetch/commit/classify shape, scoped to the single
        :data:`FX_SERIES_SECURITY_ID` pair -- one shared FX fetch per
        repair run, never per ticker. ``[start, end)`` -- ``end`` is
        exclusive. Raises :class:`PriceEvidenceUnavailable` on a newly
        recorded permanent failure, or the underlying error for a
        transient one.
        """
        if self._prices.get_unavailable_attempt(FX_SERIES_SECURITY_ID) is not None:
            return False
        if (
            self._prices.covering_revision(
                security_id=FX_SERIES_SECURITY_ID,
                requested_symbol=FX_PAIR,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            is not None
        ):
            return False

        try:
            payload = self._fx_fetcher.fetch(start=start, end=end)
        except ProviderFailure as exc:
            if exc.code in _DEFINITIVE_FAILURE_CODES:
                self._prices.record_unavailable_attempt(
                    security_id=FX_SERIES_SECURITY_ID,
                    requested_symbol=FX_PAIR,
                    reason=str(exc),
                )
                raise PriceEvidenceUnavailable(str(exc)) from exc
            raise
        self._prices.commit(payload)
        return True
