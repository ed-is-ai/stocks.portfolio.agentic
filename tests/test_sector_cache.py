"""Unit tests for the persistent sector cache (#106)."""

from app.agents.scanner.sector_cache import SectorCache


def test_remember_and_get_roundtrip(tmp_path):
    """A remembered sector survives a save/reload cycle."""
    path = tmp_path / "sector_cache.json"
    cache = SectorCache(path)

    assert cache.remember("AAPL", "Technology") is True
    cache.save()

    reloaded = SectorCache(path)
    assert reloaded.get("AAPL") == "Technology"


def test_remember_ignores_null_and_unchanged(tmp_path):
    """Null sectors are not stored, and unchanged values report no dirtiness."""
    cache = SectorCache(tmp_path / "sector_cache.json")

    assert cache.remember("AAPL", None) is False
    assert cache.remember("AAPL", "") is False
    assert cache.get("AAPL") is None

    assert cache.remember("AAPL", "Technology") is True
    assert cache.remember("AAPL", "Technology") is False  # no change


def test_get_unknown_ticker_is_none(tmp_path):
    """An unseen ticker returns None rather than raising."""
    assert SectorCache(tmp_path / "sector_cache.json").get("NOPE") is None


def test_load_tolerates_missing_and_corrupt_file(tmp_path):
    """A missing or unparseable file loads as an empty cache."""
    missing = tmp_path / "absent.json"
    assert SectorCache(missing).get("AAPL") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert SectorCache(corrupt).get("AAPL") is None


def test_load_drops_non_string_and_blank_values(tmp_path):
    """Only non-blank string sectors are retained on load."""
    path = tmp_path / "sector_cache.json"
    path.write_text(
        '{"AAPL": "Technology", "MSFT": null, "GOOG": "   ", "AMZN": 7}',
        encoding="utf-8",
    )
    cache = SectorCache(path)
    assert cache.get("AAPL") == "Technology"
    assert cache.get("MSFT") is None
    assert cache.get("GOOG") is None
    assert cache.get("AMZN") is None
