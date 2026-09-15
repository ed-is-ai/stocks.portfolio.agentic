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

from app.core.ticker_identity import (
    AmbiguousTickerAliasError,
    canonical_ticker,
    canonicalize_or_fallback,
    load_aliases,
)
from app.repositories.historical_price_repo import (
    HistoricalEvidenceIntegrityError,
    HistoricalPriceRepository,
)
from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_price_evidence import (
    HistoricalEvidenceRequest,
    YFinanceFxSeriesFetcher,
    YFinanceHistoricalEvidenceAdapter,
    fx_pair_for,
    fx_security_id_for,
)
from app.services.backtest.trading_calendar import TradingCalendar

logger = logging.getLogger(__name__)

#: The pseudo-security namespace evidence acquired here is committed under,
#: mirroring ``fx_security_id_for``'s ``"fx:GBPUSD=X"`` pattern -- never
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


class PriceEvidenceRepairError(RuntimeError):
    """A repair response failed identity or evidence-integrity validation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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

    def repair_coverage(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        expected_sessions: tuple[date, ...],
    ) -> bool:
        """Attempt one bounded, complete provider repair.

        Unlike :meth:`ensure_coverage`, this deliberately bypasses an
        interval-covering revision: that revision may contain a missing
        exchange session. Partial provider responses are not committed; the
        evaluation caller can then apply its ephemeral tolerance policy.
        """
        try:
            symbol = canonical_ticker(ticker, self._aliases)
        except (AmbiguousTickerAliasError, TypeError, ValueError) as exc:
            raise PriceEvidenceRepairError("identity_mismatch") from exc
        security_id = f"{_SECURITY_ID_PREFIX}{symbol}"
        if self._prices.get_unavailable_attempt(security_id) is not None:
            return False
        expected = tuple(expected_sessions)
        mic = "XLON" if symbol.upper().endswith(".L") else "XNYS"
        try:
            authoritative = TradingCalendar().sessions_in_range(mic, start, end)
        except (OverflowError, TypeError, ValueError) as exc:
            raise PriceEvidenceRepairError("integrity_error") from exc
        if expected != authoritative or not expected or expected[-1] != end - timedelta(days=1):
            raise PriceEvidenceRepairError("integrity_error")
        expected_currency = expected_quote_unit = expected_timezone = None
        existing = self._prices.covering_revision(
            security_id=security_id,
            requested_symbol=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if existing is not None:
            handle = None
            try:
                handle = self._prices.open_read(existing)
                metadata = handle.metadata
                expected_currency = metadata.currency
                expected_quote_unit = metadata.quote_unit
                expected_timezone = metadata.exchange_timezone
            except Exception as exc:
                raise PriceEvidenceRepairError("integrity_error") from exc
            finally:
                if handle is not None:
                    handle.close()
        request = HistoricalEvidenceRequest(
            security_id=security_id,
            alias_revision=None,
            symbol=symbol,
            start=start,
            end=end,
            expected_sessions=expected,
            allowed_observed_symbols=(symbol,),
            expected_currency=expected_currency,
            expected_quote_unit=expected_quote_unit,
            expected_timezone=expected_timezone,
            canonical_exchange_sessions=True,
        )
        try:
            payload = self._adapter.fetch(request)
            if (
                payload.security_id != security_id
                or payload.requested_symbol != symbol
                or payload.observed_symbol != symbol
            ):
                raise PriceEvidenceRepairError("identity_mismatch")
            if (
                payload.provider != "yfinance"
                or payload.start != start.isoformat()
                or payload.end != end.isoformat()
                or payload.request_contract.get("start") != start.isoformat()
                or payload.request_contract.get("end") != end.isoformat()
            ):
                raise PriceEvidenceRepairError("integrity_error")
            sessions = tuple(
                date.fromisoformat(str(row["session"])) for row in payload.rows
            )
            if sessions != expected:
                return False
            self._prices.commit(payload)
        except PriceEvidenceRepairError:
            raise
        except ProviderFailure as exc:
            if exc.code is FailureCode.IDENTITY_AMBIGUOUS:
                raise PriceEvidenceRepairError("identity_mismatch") from exc
            if exc.code is FailureCode.PROVIDER_CONTRACT_ERROR:
                raise PriceEvidenceRepairError("integrity_error") from exc
            if exc.code is FailureCode.REQUIRED_DATA_MISSING:
                self._prices.record_unavailable_attempt(
                    security_id=security_id,
                    requested_symbol=symbol,
                    reason=str(exc),
                )
            return False
        except (HistoricalEvidenceIntegrityError, KeyError, TypeError, ValueError) as exc:
            raise PriceEvidenceRepairError("integrity_error") from exc
        return True

    def ensure_fx_coverage(self, start: date, end: date) -> bool:
        """Return True if any ``GBP<CCY>=X`` series was fetched, False if none was.

        One pair per currency the portfolio's own price evidence is quoted
        in, never per ticker: a EUR holding is unpriceable without
        ``GBPEUR=X``, so fetching only ``GBPUSD=X`` blanked every day it was
        held (#516). USD is always included -- it is the pair backtest pins,
        and it must exist even before any evidence has been committed.

        Each pair mirrors :meth:`ensure_coverage`'s negative-cache/covering-
        revision/fetch/commit/classify shape. ``[start, end)`` -- ``end`` is
        exclusive. A failure on one pair propagates immediately, leaving the
        remaining pairs to the next run (where the first pair's recorded
        verdict, if permanent, short-circuits it).
        """
        fetched = False
        for currency in self._fx_currencies():
            fetched = self._ensure_fx_pair(currency, start, end) or fetched
        return fetched

    def _fx_currencies(self) -> list[str]:
        """Return every non-GBP currency needing a rate series, USD included."""
        held = self._prices.quoted_currencies(_SECURITY_ID_PREFIX)
        return sorted({"USD", *held} - {"", "GBP"})

    def _ensure_fx_pair(self, currency: str, start: date, end: date) -> bool:
        """Return True if this currency's pair was fetched, False if skipped."""
        security_id = fx_security_id_for(currency)
        pair = fx_pair_for(currency)
        if self._prices.get_unavailable_attempt(security_id) is not None:
            return False
        if (
            self._prices.covering_revision(
                security_id=security_id,
                requested_symbol=pair,
                start=start.isoformat(),
                end=end.isoformat(),
            )
            is not None
        ):
            return False

        try:
            payload = self._fx_fetcher.fetch(start=start, end=end, currency=currency)
        except ProviderFailure as exc:
            if exc.code in _DEFINITIVE_FAILURE_CODES:
                self._prices.record_unavailable_attempt(
                    security_id=security_id,
                    requested_symbol=pair,
                    reason=str(exc),
                )
                raise PriceEvidenceUnavailable(str(exc)) from exc
            raise
        self._prices.commit(payload)
        return True
