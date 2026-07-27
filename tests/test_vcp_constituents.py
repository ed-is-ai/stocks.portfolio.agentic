"""Tests for the VCP screener's S&P 500 constituent source (#97).

FMP's constituent endpoints are unavailable on the free plan (402/403), so the
universe is fetched from the free DataHub CSV. These tests exercise the parse,
the FMP-compatible output shape, and the disk-cache fallback — all offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "vcp-screener" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import fmp_client  # noqa: E402
from fmp_client import FMPClient  # noqa: E402

_CSV = (
    "Symbol,Name,Sector,Price,Price/Earnings,Dividend Yield,Earnings/Share,"
    "52 Week Low,52 Week High,Market Cap,EBITDA,Price/Sales,Price/Book,"
    "SEC Filings\n"
    "MMM,3M,Industrial Conglomerates,172.62,30.66,0.0181,5.63,139.34,177.41,"
    "89024004096,6488000000,3.53,27.59,http://sec.gov/MMM\n"
    "AAPL,Apple Inc.,Information Technology,150.0,20.0,0.006,9.2,120.0,180.0,"
    "2500000000000,120000000000,7.0,35.0,http://sec.gov/AAPL\n"
    "BRK.B,Berkshire Hathaway,Financials,300.0,15.0,0.0,20.0,250.0,320.0,"
    "700000000000,40000000000,2.5,1.4,http://sec.gov/BRK.B\n"
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_fetch_datahub_constituents_maps_to_fmp_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fmp_client.requests, "get", lambda *a, **k: _FakeResponse(_CSV))
    client = FMPClient(api_key="dummy")
    client._CONSTITUENTS_CACHE = tmp_path / "sp500.json"

    constituents = client.get_sp500_constituents()

    assert [c["symbol"] for c in constituents] == ["MMM", "AAPL", "BRK.B"]
    assert constituents[1] == {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "sector": "Information Technology",
        "subSector": "",
    }
    # Dotted tickers preserved (FMP used the same format downstream).
    assert "BRK.B" in {c["symbol"] for c in constituents}
    # Fresh success is persisted as last-known-good.
    assert (tmp_path / "sp500.json").exists()


def test_get_constituents_falls_back_to_cache_on_network_failure(
    monkeypatch, tmp_path
) -> None:
    def _boom(*args, **kwargs):
        raise fmp_client.requests.RequestException("network down")

    # First, seed the disk cache from a good fetch.
    monkeypatch.setattr(fmp_client.requests, "get", lambda *a, **k: _FakeResponse(_CSV))
    seed = FMPClient(api_key="dummy")
    seed._CONSTITUENTS_CACHE = tmp_path / "sp500.json"
    seed.get_sp500_constituents()

    # Now the live fetch fails — a fresh client must serve the cached list.
    monkeypatch.setattr(fmp_client.requests, "get", _boom)
    client = FMPClient(api_key="dummy")
    client._CONSTITUENTS_CACHE = tmp_path / "sp500.json"

    constituents = client.get_sp500_constituents()

    assert [c["symbol"] for c in constituents] == ["MMM", "AAPL", "BRK.B"]
    assert "network down" in client.last_error


def test_get_constituents_returns_none_when_no_source_available(
    monkeypatch, tmp_path
) -> None:
    def _boom(*args, **kwargs):
        raise fmp_client.requests.RequestException("network down")

    monkeypatch.setattr(fmp_client.requests, "get", _boom)
    client = FMPClient(api_key="dummy")
    client._CONSTITUENTS_CACHE = tmp_path / "missing.json"  # no cache seeded

    assert client.get_sp500_constituents() is None
