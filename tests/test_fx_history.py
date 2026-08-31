"""Unit tests for the chained historical FX quote fetcher (#452).

All transport is stubbed (``request_get``/``yfinance_fetch``) -- no test
performs real network I/O. Fixtures are shaped like the real provider
responses: the BoE page is a full HTML document (cookie-banner noise,
header row, ``DD Mon YY`` dates) and FRED is its ``fredgraph.csv`` shape
with ``.`` as the missing-value marker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
import requests

from app.integrations.fx_history import (
    ChainedFxQuoteFetcher,
    FxProviderUnavailable,
    FxUnsupportedPair,
    HttpResponseLike,
)

AS_OF = "2000-02-01"

BOE_HTML = """\
<html><body>
<div id="cookie-banner">We use cookies. Accept all.</div>
<script>var boe = {};</script>
<table>
<tr><th>Date</th><th>XUDLUSS</th></tr>
<tr><td>31 Jan 00</td><td>1.6120</td></tr>
<tr><td>01 Feb 00</td><td>1.6145</td></tr>
<tr><td>02 Feb 00</td><td>1.6180</td></tr>
</table>
</body></html>
"""

#: Same shape but with no row for the exact requested date.
BOE_HTML_NO_MATCH = BOE_HTML.replace("<tr><td>01 Feb 00</td><td>1.6145</td></tr>\n", "")

FRED_CSV = "observation_date,DEXUSUK\n2000-02-01,1.6150\n"

#: FRED's missing-value marker for a holiday/weekend observation.
FRED_CSV_MISSING = "observation_date,DEXUSUK\n2000-02-01,.\n"


def _response(status_code: int = 200, text: str = "") -> HttpResponseLike:
    """Build the minimal ``requests.Response`` surface the fetcher reads."""
    return cast(HttpResponseLike, SimpleNamespace(status_code=status_code, text=text))


def _expected_digest(provider: str, rate: str) -> str:
    """The canonical quote-digest formula under test."""
    raw = f"{provider}|GBPUSD=X|{AS_OF}|{rate}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _never_yahoo(pair: str, as_of: str) -> Decimal | None:
    raise AssertionError(f"yahoo must not be called, got {pair} {as_of}")


def test_boe_parses_exact_date_row() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(text=BOE_HTML),
        yahoo_exact=_never_yahoo,
    )

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "bank_of_england"
    assert quote.pair == "GBPUSD=X"
    assert quote.as_of == AS_OF
    assert quote.rate == Decimal("1.6145")
    assert quote.digest == _expected_digest("bank_of_england", "1.6145")


def test_boe_request_targets_xudluss_series_spanning_as_of() -> None:
    seen_url: list[str] = []
    seen_params: list[Mapping[str, str]] = []
    seen_timeout: list[float] = []

    def request_get(url: str, params: Mapping[str, str], timeout: float):
        seen_url.append(url)
        seen_params.append(params)
        seen_timeout.append(timeout)
        return _response(text=BOE_HTML)

    fetcher = ChainedFxQuoteFetcher(request_get=request_get, yahoo_exact=_never_yahoo)

    fetcher.fetch("GBPUSD=X", AS_OF)

    assert "fromshowcolumns.asp" in seen_url[0]
    assert seen_timeout[0] == 15
    params = seen_params[0]
    assert params["SeriesCodes"] == "XUDLUSS"
    assert params["FD"] == "25" and params["FM"] == "Jan" and params["FY"] == "2000"
    assert params["TD"] == "1" and params["TM"] == "Feb" and params["TY"] == "2000"


def test_boe_definitive_miss_falls_through_to_yahoo() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(text=BOE_HTML_NO_MATCH),
        yahoo_exact=lambda _pair, _as_of: Decimal("1.61"),
    )

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "yfinance"
    assert quote.rate == Decimal("1.61")
    assert quote.digest == _expected_digest("yfinance", "1.61")


def test_yahoo_exact_hit_short_circuits_fred() -> None:
    def request_get(url: str, _params: Mapping[str, str], _timeout: float):
        if "bankofengland" in url:
            return _response(text=BOE_HTML_NO_MATCH)
        raise AssertionError(f"FRED must not be called when Yahoo hits, got {url}")

    fetcher = ChainedFxQuoteFetcher(
        request_get=request_get,
        yahoo_exact=lambda _pair, _as_of: Decimal("1.6055"),
    )

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "yfinance"
    assert quote.rate == Decimal("1.6055")


def test_yahoo_definitive_miss_falls_through_to_fred() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=_fred_only_response,
        yahoo_exact=lambda _pair, _as_of: None,
    )

    assert fetcher.fetch("GBPUSD=X", AS_OF) is None


def test_yahoo_never_substitutes_a_nearest_earlier_rate() -> None:
    """The seam is exact-date: a weekend/holiday as_of must stay a miss
    even though Yahoo has rates either side of it (regression for the
    nearest-earlier-substitution hazard)."""

    def yahoo_exact(pair: str, as_of: str) -> Decimal | None:
        assert (pair, as_of) == ("GBPUSD=X", AS_OF)
        return None  # no row whose timestamp equals as_of

    fetcher = ChainedFxQuoteFetcher(
        request_get=_fred_only_response, yahoo_exact=yahoo_exact
    )

    assert fetcher.fetch("GBPUSD=X", AS_OF) is None


def test_boe_page_without_any_date_cells_is_transient() -> None:
    """A layout change must degrade to mode (a), never to a permanent
    negative cache of "no rate exists"."""
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(
            text="<table><tr><td>Header</td><td>Value</td></tr></table>"
        ),
        yahoo_exact=lambda _pair, _as_of: None,
    )

    with pytest.raises(FxProviderUnavailable, match="parseable date cells"):
        fetcher.fetch("GBPUSD=X", AS_OF)


def test_fred_blank_leading_line_is_transient_not_crashing() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(text="\nobservation_date,DEXUSUK\n"),
        yahoo_exact=lambda _pair, _as_of: None,
    )

    with pytest.raises(FxProviderUnavailable):
        fetcher.fetch("GBPUSD=X", AS_OF)


def test_malformed_as_of_is_transient_not_valueerror() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=_fred_only_response,
        yahoo_exact=lambda _pair, _as_of: None,
    )

    with pytest.raises(FxProviderUnavailable, match="Malformed"):
        fetcher.fetch("GBPUSD=X", "not-a-date")


def test_boe_implausible_rate_cell_is_not_pinned() -> None:
    bogus = BOE_HTML.replace("<td>1.6145</td>", "<td>99999999</td>")

    def request_get(url: str, *_args: object) -> HttpResponseLike:
        if "bankofengland" in url:
            return _response(text=bogus)
        return _response(text=FRED_CSV_MISSING)

    fetcher = ChainedFxQuoteFetcher(
        request_get=request_get,
        yahoo_exact=lambda _pair, _as_of: None,
    )

    assert fetcher.fetch("GBPUSD=X", AS_OF) is None


def _never_http(_url: str, _params: Mapping[str, str], _timeout: float):
    raise AssertionError("no HTTP request may be made")


def _fred_only_response(url: str, *_args: object) -> HttpResponseLike:
    """Serve a parseable no-match BoE page, then FRED's missing CSV."""
    if "bankofengland" in url:
        return _response(text=BOE_HTML_NO_MATCH)
    return _response(text=FRED_CSV_MISSING)


def test_fred_parses_exact_date_row() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(text=FRED_CSV),
        yahoo_exact=lambda _pair, _as_of: None,
    )

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "fred"
    assert quote.rate == Decimal("1.6150")
    assert quote.digest == _expected_digest("fred", "1.6150")


def test_chain_order_boe_hit_never_calls_yahoo_or_fred() -> None:
    def request_get(url: str, _params: Mapping[str, str], _timeout: float):
        if "bankofengland" not in url:
            raise AssertionError(f"FRED must not be called, got {url}")
        return _response(text=BOE_HTML)

    fetcher = ChainedFxQuoteFetcher(request_get=request_get, yahoo_exact=_never_yahoo)

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "bank_of_england"


def test_all_providers_definitively_miss_returns_none() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=_fred_only_response,
        yahoo_exact=lambda _pair, _as_of: None,
    )

    assert fetcher.fetch("GBPUSD=X", AS_OF) is None


def test_transient_failures_on_every_provider_raise() -> None:
    def request_get(_url: str, _params: Mapping[str, str], _timeout: float):
        raise requests.ConnectionError("network down")

    fetcher = ChainedFxQuoteFetcher(
        request_get=request_get,
        yahoo_exact=lambda _pair, _as_of: (_ for _ in ()).throw(
            FxProviderUnavailable("yfinance exploded")
        ),
    )

    with pytest.raises(FxProviderUnavailable):
        fetcher.fetch("GBPUSD=X", AS_OF)


def test_non_200_response_is_transient_not_definitive() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(status_code=503, text="busy"),
        yahoo_exact=lambda _pair, _as_of: None,
    )

    with pytest.raises(FxProviderUnavailable):
        fetcher.fetch("GBPUSD=X", AS_OF)


def test_transient_boe_still_lets_yahoo_serve_the_date() -> None:
    fetcher = ChainedFxQuoteFetcher(
        request_get=lambda *_args: _response(status_code=503, text="busy"),
        yahoo_exact=lambda _pair, _as_of: Decimal("1.61"),
    )

    quote = fetcher.fetch("GBPUSD=X", AS_OF)

    assert quote is not None
    assert quote.provider == "yfinance"


def test_unsupported_pair_raises_without_any_fetch() -> None:
    def request_get(_url: str, _params: Mapping[str, str], _timeout: float):
        raise AssertionError("no provider request may be made")

    fetcher = ChainedFxQuoteFetcher(request_get=request_get, yahoo_exact=_never_yahoo)

    with pytest.raises(FxUnsupportedPair, match="EURUSD=X"):
        fetcher.fetch("EURUSD=X", AS_OF)
