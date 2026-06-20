"""Unit tests for AlphaVantageClient EPS-growth math (no network)."""

from __future__ import annotations

import pytest

from app.integrations.alpha_vantage import AlphaVantageClient


def _client_with_earnings(earnings, monkeypatch) -> AlphaVantageClient:
    client = AlphaVantageClient(api_key="test")
    monkeypatch.setattr(client, "get_earnings", lambda symbol: earnings)
    return client


def _quarters(*eps_values: str) -> dict:
    return {"quarterlyEarnings": [{"reportedEPS": v} for v in eps_values]}


def _annual(*eps_values: str) -> dict:
    return {"annualEarnings": [{"reportedEPS": v} for v in eps_values]}


# ---------------------------------------------------------------------------
# get_quarterly_eps_growth
# ---------------------------------------------------------------------------


def test_quarterly_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Index 0=1.50, index 4=1.00 → (1.50-1.00)/1.00 = 0.5."""
    data = _quarters("1.50", "1.20", "1.10", "1.05", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") == 0.5


def test_quarterly_negative_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Index 0=0.50, index 4=1.00 → (0.50-1.00)/1.00 = -0.5."""
    data = _quarters("0.50", "0.60", "0.70", "0.80", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") == -0.5


def test_quarterly_fewer_than_5_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fewer than 5 quarters → None."""
    data = _quarters("1.50", "1.20", "1.10", "1.05")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


def test_quarterly_year_ago_zero_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Year-ago EPS = 0 → None (guard against divide-by-zero)."""
    data = _quarters("1.50", "1.20", "1.10", "1.05", "0")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


def test_quarterly_year_ago_negative_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Year-ago EPS < 0 → None."""
    data = _quarters("1.50", "1.20", "1.10", "1.05", "-0.50")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


def test_quarterly_unparseable_recent_eps_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric string in index 0 → None."""
    data = _quarters("None", "1.20", "1.10", "1.05", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


def test_quarterly_unparseable_year_ago_eps_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric string in index 4 → None."""
    data = _quarters("1.50", "1.20", "1.10", "1.05", "")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


def test_quarterly_get_earnings_none_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_earnings returning None → None."""
    client = _client_with_earnings(None, monkeypatch)
    assert client.get_quarterly_eps_growth("X") is None


# ---------------------------------------------------------------------------
# get_annual_eps_cagr
# ---------------------------------------------------------------------------


def test_annual_cagr_four_years(monkeypatch: pytest.MonkeyPatch) -> None:
    """4 annual entries: (8.00/1.00)^(1/3) - 1 = 1.0."""
    data = _annual("8.00", "4.00", "2.00", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") == pytest.approx(1.0, rel=1e-4)


def test_annual_cagr_three_years(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 annual entries: (4.00/1.00)^(1/2) - 1 = 1.0."""
    data = _annual("4.00", "2.00", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") == pytest.approx(1.0, rel=1e-4)


def test_annual_cagr_fewer_than_3_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fewer than 3 annual entries → None."""
    data = _annual("4.00", "2.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_base_year_zero_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oldest (base) EPS = 0 → None."""
    data = _annual("4.00", "2.00", "0")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_base_year_negative_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oldest (base) EPS < 0 → None."""
    data = _annual("4.00", "2.00", "-1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_newest_negative_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newest (index 0) EPS < 0 → None."""
    data = _annual("-1.00", "2.00", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_unparseable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-numeric EPS string → None."""
    data = _annual("n/a", "2.00", "1.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_get_earnings_none_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_earnings returning None → None."""
    client = _client_with_earnings(None, monkeypatch)
    assert client.get_annual_eps_cagr("X") is None


def test_annual_cagr_more_than_4_uses_first_4(monkeypatch: pytest.MonkeyPatch) -> None:
    """With 6 annual entries n=min(6,4)=4; base is index 3, index 5 is ignored.

    entries: ["8.00", "4.00", "2.00", "1.00", "100.00", "999.00"]
    n=4, newest=annual[0]=8.00, oldest=annual[3]=1.00
    CAGR = (8.00/1.00)^(1/3) - 1 = 1.0  (same as 4-year test above)
    """
    data = _annual("8.00", "4.00", "2.00", "1.00", "100.00", "999.00")
    client = _client_with_earnings(data, monkeypatch)
    assert client.get_annual_eps_cagr("X") == pytest.approx(1.0, rel=1e-4)
