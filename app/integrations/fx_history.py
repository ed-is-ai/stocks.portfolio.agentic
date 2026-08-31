"""Chained historical FX quote backfill (BoE -> Yahoo -> FRED) for #452.

Backtest preparation pins FX evidence at an exact date, but ``fx_quotes``
is only written at valuation time -- so a preparation run whose start
month predates every cached quote (e.g. 2000-02, older than Yahoo's FX
history) used to fail outright. :class:`ChainedFxQuoteFetcher` fills that
gap by trying three providers strictly in order and returning the first
exact-date rate, or ``None`` only when every provider definitively has no
rate for the date.

Providers (first hit wins, never mixed for the same ``(pair, date)``):

1. Bank of England daily spot series (``XUDLUSS`` for ``GBPUSD=X``) --
   covers dates back to 1975, i.e. everything Yahoo's FX history misses.
2. Yahoo (``GBPUSD=X`` via ``yfinance``) -- exact-date match only, so a
   weekend/holiday month-start never pins a nearest-earlier rate under
   the requested date.
3. FRED (``DEXUSUK``) -- an independent exact-date CSV source.

Transient failures (HTTP errors, timeouts, unparseable responses) are
swallowed per provider so the chain keeps walking, but if no provider
produced a rate and at least one failed transiently the whole fetch
raises :class:`FxProviderUnavailable` -- the caller's signal for failure
mode (a) "fetchable but fetch failed, retry preparation" (never record a
negative cache attempt for it). A chain where every provider responded
fine but none has the exact date returns ``None`` -- failure mode (b)
"no rate exists, choose a later start month".
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import logging
import re
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol, cast

import requests

from app.repositories.fx_quote_repo import FxQuote

logger = logging.getLogger(__name__)

#: The only cross-currency pair v1 resolves, mapped to its Bank of England
#: statistical series code and FRED series id. Any other pair is rejected
#: (the resolver's own "No supported FX pair" check runs before fetching).
_PAIR_SERIES: dict[str, tuple[str, str]] = {
    "GBPUSD=X": ("XUDLUSS", "DEXUSUK"),
}

#: Network timeout applied to every provider request (seconds).
_REQUEST_TIMEOUT_SECONDS = 15

#: How far before ``as_of`` the BoE date range starts. The series only
#: publishes business days, so the surrounding rows prove the exact date
#: is genuinely absent rather than lost to a too-narrow window.
_BOE_LOOKBACK_DAYS = 7

_BOE_URL = "https://www.bankofengland.co.uk/boeapps/database/fromshowcolumns.asp"
_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_MONTH_ABBREVS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ENGLISH_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

#: Sanity bounds for a plausible FX rate -- a misaligned table column or a
#: footnote number must never be persisted as immutable evidence.
_MIN_PLAUSIBLE_RATE = Decimal("0.000001")
_MAX_PLAUSIBLE_RATE = Decimal("1000000")

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BOE_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{2})")


class FxProviderUnavailable(Exception):
    """A provider (or the whole chain) failed transiently.

    Retrying later may succeed, so callers must never record a negative
    cache attempt for this -- unlike a definitive ``None`` return, which
    means no provider has the rate at all.
    """


class FxUnsupportedPair(FxProviderUnavailable):
    """No provider series is configured for the requested pair.

    A permanent configuration condition -- retrying can never succeed
    until the mapping in ``_PAIR_SERIES`` is extended.
    """


class HttpResponseLike(Protocol):
    """The minimal surface of ``requests.Response`` the fetcher reads."""

    status_code: int
    text: str


def _default_request_get(
    url: str, params: Mapping[str, str], timeout: float
) -> HttpResponseLike:
    """Perform the HTTP GET via ``requests`` (the injectable default)."""
    return cast(HttpResponseLike, requests.get(url, params=params, timeout=timeout))


def _default_yahoo_exact(pair: str, as_of: str) -> Decimal | None:
    """Fetch one exact-date Yahoo rate via a ranged ``yfinance`` download.

    Deliberately does NOT reuse ``PortfolioService._fetch_historical_fx``:
    that seam substitutes the nearest-earlier rate under the requested
    date key, which would pin a weekend/holiday rate as if it were the
    exact-date evidence. Here only a row whose timestamp equals ``as_of``
    counts; anything else is a definitive miss.
    """
    import yfinance as yf

    end = date.fromisoformat(as_of) + timedelta(days=1)
    start = end - timedelta(days=8)
    try:
        data = yf.download(
            pair,
            start=start.isoformat(),
            end=end.isoformat(),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        raise FxProviderUnavailable(
            f"Yahoo historical FX download failed for {pair}: {exc}"
        ) from exc
    if data is None or data.empty:
        return None
    close = data["Close"] if "Close" in data.columns else data.iloc[:, 0]
    series = close.iloc[:, 0] if hasattr(close, "columns") else close
    for timestamp, value in series.items():
        if timestamp.date().isoformat() != as_of:
            continue
        if value is None or value != value:  # None or NaN
            return None
        return Decimal(str(value))
    return None


def _boe_rows(html_text: str) -> list[list[str]]:
    """Extract the text cells of every HTML table row, tolerating noise.

    The BoE response is a full HTML page (cookie banners, scripts, header
    rows), so this walks every ``<tr>`` and strips tags/entities from its
    ``<td>`` cells rather than assuming one rigid table shape.
    """
    rows: list[list[str]] = []
    for tr in _TR_RE.findall(html_text):
        cells = [
            html.unescape(_TAG_RE.sub("", cell)).strip() for cell in _TD_RE.findall(tr)
        ]
        if cells:
            rows.append(cells)
    return rows


def _is_boe_date_for(cell: str, as_of: date) -> bool:
    """Return True when ``cell`` is a ``DD Mon YY`` BoE date == ``as_of``.

    The 2-digit year is normalized against ``as_of``'s own year, so only
    an exact-date match ever counts (the exact-date pin rule).
    """
    match = _BOE_DATE_RE.fullmatch(cell)
    if match is None:
        return False
    day, month_name, yy = match.groups()
    month = _MONTH_ABBREVS.get(month_name[:3].lower())
    if month is None:
        return False
    return (
        int(day) == as_of.day and month == as_of.month and int(yy) == as_of.year % 100
    )


def _parse_rate(text: str) -> Decimal | None:
    """Parse a positive, finite FX rate, or return None."""
    try:
        rate = Decimal(text)
    except ArithmeticError:
        return None
    if not rate.is_finite() or rate <= 0:
        return None
    if not (_MIN_PLAUSIBLE_RATE <= rate <= _MAX_PLAUSIBLE_RATE):
        return None
    return rate


def _build_quote(provider: str, pair: str, as_of: str, rate: Decimal) -> FxQuote:
    """Build an ``FxQuote`` with the canonical content digest.

    Same ``sha256("{provider}|{pair}|{as_of}|{rate}")`` formula as
    ``gbp_valuation_service._quote_digest`` -- the digest is what the
    manifest pins and later replays by.
    """
    digest = hashlib.sha256(
        f"{provider}|{pair}|{as_of}|{rate}".encode("utf-8")
    ).hexdigest()
    return FxQuote(pair=pair, provider=provider, as_of=as_of, rate=rate, digest=digest)


class ChainedFxQuoteFetcher:
    """Fetch one exact-date historical FX quote via a 3-provider chain.

    Transport is injectable for testability: ``request_get`` defaults to
    ``requests.get`` and ``yfinance_fetch`` to a wrapper around
    ``PortfolioService._fetch_historical_fx`` -- tests stub these instead
    of the providers themselves, and no test ever touches the network.
    """

    def __init__(
        self,
        request_get: Callable[[str, Mapping[str, str], float], HttpResponseLike]
        | None = None,
        yahoo_exact: Callable[[str, str], Decimal | None] | None = None,
    ) -> None:
        self._request_get = request_get or _default_request_get
        self._yahoo_exact = yahoo_exact or _default_yahoo_exact

    def fetch(self, pair: str, as_of: str) -> FxQuote | None:
        """Return the first provider's exact-date quote, or None.

        Providers are tried strictly in order BoE -> Yahoo -> FRED; the
        chain stops at the first hit. Definitive no-rate responses move
        on to the next provider; transient failures are remembered and,
        if nothing was fetched, re-raised as one
        :class:`FxProviderUnavailable` so the caller can distinguish
        failure mode (a) from a definitive mode-(b) ``None``.
        """
        if pair not in _PAIR_SERIES:
            raise FxUnsupportedPair(
                f"No supported FX provider series for pair {pair!r}"
            )
        transient: list[str] = []
        for provider in (
            self._fetch_bank_of_england,
            self._fetch_yahoo,
            self._fetch_fred,
        ):
            try:
                quote = provider(pair, as_of)
            except (FxProviderUnavailable, requests.RequestException) as exc:
                # Transport-level failures (timeouts, connection errors)
                # are transient exactly like an explicit
                # FxProviderUnavailable -- swallowed per provider so the
                # chain keeps walking, escalated below if nothing hit.
                logger.warning("FX provider %s failed: %s", provider.__name__, exc)
                transient.append(str(exc))
                continue
            if quote is not None:
                return quote
        if transient:
            raise FxProviderUnavailable("; ".join(transient))
        return None

    def _fetch_bank_of_england(self, pair: str, as_of: str) -> FxQuote | None:
        """Fetch the exact date from the BoE daily spot series, or None.

        Requests a range spanning ``as_of`` (7-day lookback margin) and
        parses the HTML table for the ``DD Mon YY`` row equal to ``as_of``
        -- only an exact-date match counts.
        """
        boe_series, _ = _PAIR_SERIES[pair]
        try:
            as_of_date = date.fromisoformat(as_of)
        except ValueError as exc:
            raise FxProviderUnavailable(f"Malformed FX pin date {as_of!r}") from exc
        start = as_of_date - timedelta(days=_BOE_LOOKBACK_DAYS)
        # Fixed English abbreviations -- strftime("%b") is locale-dependent
        # and would send e.g. "Mär" on a localized host.
        params = {
            "Travel": "NIxAZxSUx",
            "FromSeries": "1",
            "ToSeries": "50",
            "DAT": "RNG",
            "FD": str(start.day),
            "FM": _ENGLISH_MONTHS[start.month - 1],
            "FY": str(start.year),
            "TD": str(as_of_date.day),
            "TM": _ENGLISH_MONTHS[as_of_date.month - 1],
            "TY": str(as_of_date.year),
            "FNY": "Y",
            "CSVF": "TT",
            "SeriesCodes": boe_series,
            "UsingCodes": "Y",
            "Filter": "N",
            "VPD": "Y",
            "VFD": "N",
        }
        response = self._request_get(_BOE_URL, params, _REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise FxProviderUnavailable(
                f"Bank of England returned HTTP {response.status_code} for {boe_series}"
            )
        rows = _boe_rows(response.text)
        if not rows:
            raise FxProviderUnavailable(
                f"Bank of England response for {boe_series} had no table rows"
            )
        saw_any_date = False
        for cells in rows:
            for index, cell in enumerate(cells[:-1]):
                if _BOE_DATE_RE.fullmatch(cell):
                    saw_any_date = True
                if not _is_boe_date_for(cell, as_of_date):
                    continue
                rate = _parse_rate(cells[index + 1])
                if rate is not None:
                    return _build_quote("bank_of_england", pair, as_of, rate)
        if not saw_any_date:
            # The page rendered but contained no parseable date cells at
            # all -- the table structure changed. Never negatively cache
            # that as "no rate exists".
            raise FxProviderUnavailable(
                f"Bank of England response for {boe_series} had no parseable date cells"
            )
        return None

    def _fetch_yahoo(self, pair: str, as_of: str) -> FxQuote | None:
        """Fetch the exact date from Yahoo, or None.

        The seam is exact-date by contract: it returns the rate only when
        Yahoo has a row whose timestamp equals ``as_of`` (never a
        nearest-earlier substitute), ``None`` when it does not, and
        raises :class:`FxProviderUnavailable` on transport failure.
        """
        try:
            rate = self._yahoo_exact(pair, as_of)
        except FxProviderUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - defensive seam guard
            raise FxProviderUnavailable(
                f"Yahoo historical FX fetch failed for {pair}: {exc}"
            ) from exc
        if rate is None:
            return None
        rate = _parse_rate(str(rate))
        if rate is None:
            return None
        return _build_quote("yfinance", pair, as_of, rate)

    def _fetch_fred(self, pair: str, as_of: str) -> FxQuote | None:
        """Fetch the exact date from FRED's daily CSV, or None.

        ``cosd``/``coed`` are both ``as_of`` so the CSV holds at most one
        observation; ``.`` is FRED's missing-value marker.
        """
        _, fred_series = _PAIR_SERIES[pair]
        response = self._request_get(
            _FRED_URL,
            {"id": fred_series, "cosd": as_of, "coed": as_of},
            _REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise FxProviderUnavailable(
                f"FRED returned HTTP {response.status_code} for {fred_series}"
            )
        reader = csv.reader(io.StringIO(response.text))
        header = next(reader, None)
        if not header or "observation_date" not in header[0].lower():
            raise FxProviderUnavailable(
                f"FRED response for {fred_series} was not a CSV observation table"
            )
        for row in reader:
            if len(row) < 2 or row[0].strip() != as_of:
                continue
            value = row[1].strip()
            if value in {"", "."}:
                continue
            rate = _parse_rate(value)
            if rate is not None:
                return _build_quote("fred", pair, as_of, rate)
        return None


__all__ = [
    "ChainedFxQuoteFetcher",
    "FxProviderUnavailable",
    "FxUnsupportedPair",
]
