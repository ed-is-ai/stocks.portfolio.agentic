"""Tests for FMPClient S&P 500 constituent diagnostics and caching (issue #77)."""

import json
from pathlib import Path

from fmp_client import FMPClient


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _make_client(tmp_path: Path) -> FMPClient:
    client = FMPClient(api_key="test-key")
    client.RATE_LIMIT_DELAY = 0  # no sleeping in tests
    client._CONSTITUENTS_CACHE = tmp_path / ".cache" / "sp500_constituents.json"
    return client


def _install_session(client: FMPClient, responses: list[_FakeResponse]) -> None:
    """Make client.session.get return the given responses in order."""
    calls = {"n": 0}

    def _get(url, params=None, timeout=None):
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]

    client._constituents_get = _get


def test_records_last_error_on_http_failure(tmp_path):
    """A non-200 response records the status + body even when quiet."""
    client = _make_client(tmp_path)
    _install_session(
        client,
        [_FakeResponse(401, payload=None, text="Invalid API KEY")],
    )

    result = client.get_sp500_constituents()

    assert result is None
    assert client.last_error is not None
    assert "401" in client.last_error
    assert "Invalid API KEY" in client.last_error


def test_diagnostic_reason_surfaced_to_stderr(tmp_path, capsys):
    """The real failure reason is printed, not a generic message."""
    client = _make_client(tmp_path)
    _install_session(client, [_FakeResponse(403, payload=None, text="Plan limit")])

    client.get_sp500_constituents()

    err = capsys.readouterr().err
    assert "Unable to fetch S&P 500 constituents" in err
    assert "403" in err


def test_successful_fetch_writes_disk_cache(tmp_path):
    """A fresh success persists a last-known-good list to disk."""
    client = _make_client(tmp_path)
    constituents = [{"symbol": "AAPL", "name": "Apple", "sector": "Tech"}]
    _install_session(client, [_FakeResponse(200, payload=constituents)])

    result = client.get_sp500_constituents()

    assert result == constituents
    cache_file = client._CONSTITUENTS_CACHE
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert payload["constituents"] == constituents
    assert "cached_at" in payload


def test_falls_back_to_disk_cache_on_failure(tmp_path, capsys):
    """When every live fetch fails, the cached list is reused with a warning."""
    client = _make_client(tmp_path)
    cached = [{"symbol": "MSFT", "name": "Microsoft", "sector": "Tech"}]
    client._CONSTITUENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    client._CONSTITUENTS_CACHE.write_text(
        json.dumps({"cached_at": "2026-07-01T00:00:00+00:00", "constituents": cached})
    )
    _install_session(client, [_FakeResponse(500, payload=None, text="upstream down")])

    result = client.get_sp500_constituents()

    assert result == cached
    err = capsys.readouterr().err
    assert "falling back to cached" in err


def test_no_fallback_when_cache_missing(tmp_path):
    """Failure with no cached list returns None (hard failure)."""
    client = _make_client(tmp_path)
    _install_session(client, [_FakeResponse(500, payload=None, text="down")])

    assert client.get_sp500_constituents() is None


def test_ignores_corrupt_cache(tmp_path):
    """A corrupt cache file is ignored rather than crashing the fallback."""
    client = _make_client(tmp_path)
    client._CONSTITUENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    client._CONSTITUENTS_CACHE.write_text("{not valid json")
    _install_session(client, [_FakeResponse(500, payload=None, text="down")])

    assert client.get_sp500_constituents() is None


def test_strict_live_constituents_never_uses_disk_fallback(tmp_path, capsys):
    client = _make_client(tmp_path)
    cached = [{"symbol": "MSFT", "name": "Microsoft", "sector": "Tech"}]
    client._CONSTITUENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    client._CONSTITUENTS_CACHE.write_text(
        json.dumps({"cached_at": "2026-07-01T00:00:00+00:00", "constituents": cached})
    )
    _install_session(client, [_FakeResponse(503, payload=None, text="upstream down")])

    assert client.get_sp500_constituents_live() is None
    assert "falling back to cached" not in capsys.readouterr().err


def test_strict_live_constituents_rejects_rows_missing_required_fields(tmp_path):
    client = _make_client(tmp_path)
    _install_session(client, [_FakeResponse(200, payload=[{"symbol": "AAPL"}])])

    assert client.get_sp500_constituents_live() is None
    assert client.last_error is not None
    assert "required name or sector" in client.last_error
