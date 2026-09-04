"""Durable WhaleWisdom heat-map cache + agent integration coverage (#345)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import app.agents.extraction.extraction_agent as extraction_module
from app.agents.extraction import heat_map_cache
from app.agents.extraction.heat_map_cache import CacheEntry
from app.agents.extraction.extraction_agent import ExtractionAgent
from app.schemas.source_health import SourceName, SourceState


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module with a call counter."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return _FakeResponse(self.payload)


def _children(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "overall_rank": i} for i, name in enumerate(names, start=1)]


def _write_entry(
    path: Path,
    period: str,
    fetched_at: str,
    children: list[dict[str, Any]] | None = None,
    heat_map_id: int = 3,
) -> None:
    path.write_text(
        json.dumps(
            {
                "source_period": period,
                "fetched_at": fetched_at,
                "children": _children("AAPL") if children is None else children,
                "heat_map_id": heat_map_id,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "heat_map_cache.json"
    monkeypatch.setattr(heat_map_cache, "CACHE_PATH", path)
    return path


@pytest.fixture()
def isolate_stocktwits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EMAIL_USER", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        extraction_module, "_ST_WATCHLIST", tmp_path / "missing_watchlist.json"
    )
    monkeypatch.setattr(
        ExtractionAgent, "_update_results_with_sources", lambda *_a, **_k: None
    )
    monkeypatch.setattr(ExtractionAgent, "_save_ww_context", lambda *_a, **_k: None)


def _run_agent(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    **kwargs: Any,
) -> tuple[ExtractionAgent, _FakeRequests, list[str]]:
    fake = _FakeRequests(payload)
    monkeypatch.setattr(extraction_module, "requests", fake)
    agent = ExtractionAgent(name="ExtractionAgent", **kwargs)
    result = agent.run()
    return agent, fake, result


# --------------------------------------------------------------------------- #
# Cache unit tests                                                             #
# --------------------------------------------------------------------------- #


def test_load_missing_file_is_none(cache_path: Path) -> None:
    assert heat_map_cache.load() is None


@pytest.mark.parametrize(
    "content",
    [
        "not json {",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"source_period": "Q1", "fetched_at": "x"}),  # missing children
        json.dumps(
            {"source_period": 3, "fetched_at": "x", "children": []}
        ),  # non-str period
        json.dumps(
            {"source_period": "Q1", "fetched_at": "x", "children": {}}
        ),  # non-list children
        json.dumps(
            {"source_period": "Q1", "fetched_at": "x", "children": ["nope"]}
        ),  # non-dict child
    ],
)
def test_load_rejects_invalid_content(cache_path: Path, content: str) -> None:
    cache_path.write_text(content, encoding="utf-8")
    assert heat_map_cache.load() is None


def test_load_rejects_non_utf8(cache_path: Path) -> None:
    cache_path.write_bytes(b"\xff\xfe\x00bad")
    assert heat_map_cache.load() is None


def test_store_round_trip_stamps_period_and_aware_time(cache_path: Path) -> None:
    heat_map_cache.store(
        "Q3 2025", _children("AAPL", "MSFT"), extraction_module.HEAT_MAP_ID
    )
    entry = heat_map_cache.load()
    assert entry is not None
    assert entry["source_period"] == "Q3 2025"
    assert len(entry["children"]) == 2
    fetched_at = heat_map_cache.entry_fetched_at(entry)
    assert fetched_at is not None and fetched_at.tzinfo is not None


def test_is_fresh_window(cache_path: Path) -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    recent: CacheEntry = {
        "source_period": "Q",
        "fetched_at": (now - timedelta(days=3)).isoformat(),
        "children": [],
        "heat_map_id": 1,
    }
    old: CacheEntry = {
        "source_period": "Q",
        "fetched_at": (now - timedelta(days=8)).isoformat(),
        "children": [],
        "heat_map_id": 1,
    }
    future: CacheEntry = {
        "source_period": "Q",
        "fetched_at": (now + timedelta(days=1)).isoformat(),
        "children": [],
        "heat_map_id": 1,
    }
    unparseable: CacheEntry = {
        "source_period": "Q",
        "fetched_at": "nonsense",
        "children": [],
        "heat_map_id": 1,
    }
    assert heat_map_cache.is_fresh(recent, now) is True
    assert heat_map_cache.is_fresh(old, now) is False
    assert heat_map_cache.is_fresh(future, now) is False
    assert heat_map_cache.is_fresh(unparseable, now) is False


# --------------------------------------------------------------------------- #
# Agent integration — the I/O matrix                                           #
# --------------------------------------------------------------------------- #


def _now_iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_fresh_cache_serves_without_network(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(1), _children("AAPL", "MSFT"))
    agent, fake, _result = _run_agent(
        monkeypatch, {"name": "SHOULD_NOT", "children": []}
    )
    assert fake.calls == 0
    health = agent.source_health[SourceName.WHALE_WISDOM]
    assert health.state is SourceState.OK
    assert health.detail_code == "cache_hit"
    assert agent.last_quarter == "Q3 2025"
    assert "Q3 2025" in health.display_message


def test_expired_cache_refetches_and_rewrites(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q2 2025", _now_iso(9), _children("OLD"))
    agent, fake, _result = _run_agent(
        monkeypatch, {"name": "Q3 2025", "children": _children("AAPL")}
    )
    assert fake.calls == 1
    assert agent.source_health[SourceName.WHALE_WISDOM].detail_code == ""
    entry = heat_map_cache.load()
    assert entry is not None and entry["source_period"] == "Q3 2025"


def test_missing_cache_refetches(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, fake, _result = _run_agent(
        monkeypatch, {"name": "Q3 2025", "children": _children("AAPL")}
    )
    assert fake.calls == 1
    assert heat_map_cache.load() is not None


def test_force_whale_wisdom_always_fetches(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(0), _children("AAPL"))
    agent, fake, _result = _run_agent(
        monkeypatch,
        {"name": "Q3 2025", "children": _children("AAPL")},
        force_whale_wisdom=True,
    )
    assert fake.calls == 1
    assert agent.source_health[SourceName.WHALE_WISDOM].detail_code == ""


def test_empty_response_not_cached_prior_entry_intact(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(1), _children("AAPL"))
    agent, fake, _result = _run_agent(
        monkeypatch,
        {"name": "Q4 2025", "children": []},
        force_whale_wisdom=True,
    )
    assert fake.calls == 1
    assert agent.source_health[SourceName.WHALE_WISDOM].state is SourceState.EMPTY
    entry = heat_map_cache.load()
    assert entry is not None and entry["source_period"] == "Q3 2025"


def test_missing_children_key_is_not_key_error(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, fake, _result = _run_agent(
        monkeypatch, {"name": "Q3 2025"}, force_whale_wisdom=True
    )
    # WhaleWisdom has no config fallback; a bad response shape just reports
    # FAILED (not a KeyError escape).
    assert agent.source_health[SourceName.WHALE_WISDOM].state is SourceState.FAILED


def test_raising_fetch_with_cache_serves_stale(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(2), _children("AAPL", "MSFT"))
    agent, fake, result = _run_agent(
        monkeypatch, RuntimeError("boom"), force_whale_wisdom=True
    )
    assert fake.calls == 1
    health = agent.source_health[SourceName.WHALE_WISDOM]
    assert health.state is SourceState.OK
    assert health.detail_code == "stale_cache"
    assert "Q3 2025" in health.display_message
    assert health.count == 2
    assert {"AAPL", "MSFT"} <= set(result)
    assert agent.last_quarter == "Q3 2025"


def test_raising_fetch_without_cache_is_failed(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, fake, _result = _run_agent(monkeypatch, RuntimeError("boom"))
    assert agent.source_health[SourceName.WHALE_WISDOM].state is SourceState.FAILED


def test_new_period_replaces_cache(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(0), _children("OLD"))
    agent, fake, _result = _run_agent(
        monkeypatch,
        {"name": "Q1 2026", "children": _children("NEW")},
        force_whale_wisdom=True,
    )
    entry = heat_map_cache.load()
    assert entry is not None
    assert entry["source_period"] == "Q1 2026"
    assert [c["name"] for c in entry["children"]] == ["NEW"]
    assert agent.last_quarter == "Q1 2026"
    health = agent.source_health[SourceName.WHALE_WISDOM]
    assert health.count == 1
    assert "NEW" in _result


def test_future_fetched_at_triggers_refetch(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(-2), _children("AAPL"))
    agent, fake, _result = _run_agent(
        monkeypatch, {"name": "Q3 2025", "children": _children("AAPL")}
    )
    assert fake.calls == 1
    assert agent.source_health[SourceName.WHALE_WISDOM].detail_code == ""


def test_stored_entry_records_period_and_aware_time(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_agent(monkeypatch, {"name": "Q3 2025", "children": _children("AAPL")})
    entry = heat_map_cache.load()
    assert entry is not None
    assert entry["source_period"] == "Q3 2025"
    fetched_at = heat_map_cache.entry_fetched_at(entry)
    assert fetched_at is not None and fetched_at.utcoffset() is not None


def test_is_fresh_exactly_seven_days_is_stale(cache_path: Path) -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    entry = {
        "source_period": "Q",
        "fetched_at": (now - timedelta(days=7)).isoformat(),
        "children": [],
        "heat_map_id": 3,
    }
    assert heat_map_cache.is_fresh(entry, now) is False


def test_stale_cache_with_empty_children_is_failed(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(cache_path, "Q3 2025", _now_iso(1), [])
    agent, fake, _result = _run_agent(
        monkeypatch, RuntimeError("boom"), force_whale_wisdom=True
    )
    assert agent.source_health[SourceName.WHALE_WISDOM].state is SourceState.FAILED


def test_store_oserror_does_not_fail_good_fetch(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(heat_map_cache, "store", _boom)
    agent, fake, result = _run_agent(
        monkeypatch, {"name": "Q3 2025", "children": _children("AAPL")}
    )
    assert agent.source_health[SourceName.WHALE_WISDOM].state is SourceState.OK
    assert "AAPL" in result


class _FakeStockTwitsEmailSource:
    last_force: bool | None = None

    def __init__(self, *, force: bool = False) -> None:
        type(self).last_force = force
        self.enabled = False

    def load(self) -> dict[str, list[str]]:
        return {}


def test_force_stocktwits_constructs_email_source_forced(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeStockTwitsEmailSource.last_force = None
    monkeypatch.setattr(
        extraction_module, "StockTwitsEmailSource", _FakeStockTwitsEmailSource
    )
    agent = ExtractionAgent(name="ExtractionAgent", force_stocktwits=True)
    agent._load_stocktwits_from_email()
    assert _FakeStockTwitsEmailSource.last_force is True


def test_default_stocktwits_constructs_email_source_unforced(
    cache_path: Path, isolate_stocktwits: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeStockTwitsEmailSource.last_force = None
    monkeypatch.setattr(
        extraction_module, "StockTwitsEmailSource", _FakeStockTwitsEmailSource
    )
    agent = ExtractionAgent(name="ExtractionAgent")
    agent._load_stocktwits_from_email()
    assert _FakeStockTwitsEmailSource.last_force is False
