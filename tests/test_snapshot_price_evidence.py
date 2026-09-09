"""Tests for the cache-backed dated GBP price source (#481)."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.repositories import db
from app.repositories.fx_quote_repo import FxQuoteRepository
from app.repositories.fx_rate_cache_repo import FxRateCacheRepository
from app.repositories.historical_price_repo import HistoricalPriceRepository
from app.services import snapshot_price_evidence
from app.services.snapshot_price_evidence import HistoricalCacheGbpPriceSource

_ALIASES = {"HSFWA": "0P00013P6I.L"}


def _cache_repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "historical_price_cache.db")
    )
    repo.ensure_schema()
    return repo


def _add_revision(
    tmp_path: Path,
    *,
    data_revision: str,
    requested_symbol: str,
    currency: str,
    quote_unit: str,
    quote_unit_scale: str,
    first_acquired_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    """Insert one minimal revision row directly (the tables are append-only)."""
    conn = sqlite3.connect(tmp_path / "historical_price_cache.db")
    conn.execute(
        """INSERT INTO historical_price_revisions (
               data_revision, security_id, provider, provider_version,
               request_contract_version, requested_symbol, observed_symbol,
               alias_revision, currency, quote_unit, quote_unit_scale,
               exchange_timezone, start_date, end_date, request_contract_json,
               response_metadata_digest, canonical_manifest_json,
               observation_count, action_count, first_acquired_at
           ) VALUES (?, ?, 'yfinance', '0.2', 'v1', ?, ?, NULL, ?, ?, ?,
                     'Europe/London', '2024-01-01', '2024-12-31', '{}', 'd',
                     '{}', 1, 0, ?)""",
        (
            data_revision,
            f"security-{data_revision}",
            requested_symbol,
            requested_symbol,
            currency,
            quote_unit,
            quote_unit_scale,
            first_acquired_at,
        ),
    )
    conn.commit()
    conn.close()


def _add_observation(
    tmp_path: Path, *, data_revision: str, session_date: str, close: float
) -> None:
    conn = sqlite3.connect(tmp_path / "historical_price_cache.db")
    close_hex = float(close).hex()
    conn.execute(
        """INSERT INTO historical_price_observations (
               data_revision, session_date, open_hex, high_hex, low_hex,
               close_hex, adj_close_hex, volume_hex, dividends_hex,
               stock_splits_hex
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data_revision,
            session_date,
            close_hex,
            close_hex,
            close_hex,
            close_hex,
            close_hex,
            (0.0).hex(),
            (0.0).hex(),
            (0.0).hex(),
        ),
    )
    conn.commit()
    conn.close()


def _trades_connect(tmp_path: Path) -> db.Connect:
    connect = db.make_connect(lambda: tmp_path / "trades.db")
    conn = connect()
    db.init_trades_db(conn)
    conn.commit()
    conn.close()
    return connect


def _source(tmp_path: Path) -> HistoricalCacheGbpPriceSource:
    connect = _trades_connect(tmp_path)
    return HistoricalCacheGbpPriceSource(
        _cache_repo(tmp_path),
        FxQuoteRepository(connect),
        FxRateCacheRepository(connect),
        aliases=_ALIASES,
    )


def _seed_lgen(tmp_path: Path, close: float = 250.0) -> None:
    _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-lgen",
        requested_symbol="LGEN.L",
        currency="GBP",
        quote_unit="GBp",
        quote_unit_scale="0.01",
    )
    _add_observation(
        tmp_path, data_revision="rev-lgen", session_date="2024-06-03", close=close
    )


def _seed_dell(tmp_path: Path, close: float = 120.0) -> None:
    _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-dell",
        requested_symbol="DELL",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
    )
    _add_observation(
        tmp_path, data_revision="rev-dell", session_date="2024-06-03", close=close
    )


def test_pence_quoted_holding_scales_to_pounds(tmp_path: Path) -> None:
    _seed_lgen(tmp_path)

    price = _source(tmp_path).gbp_price("LGEN.L", "2024-06-03")

    assert price == pytest.approx(2.5)


def test_usd_holding_uses_an_exact_dated_fx_quote(tmp_path: Path) -> None:
    _seed_dell(tmp_path)
    connect = _trades_connect(tmp_path)
    conn = connect()
    conn.execute(
        "INSERT INTO fx_quotes (quote_digest, provider, pair, as_of, rate, "
        "fetched_at) VALUES ('d1', 'yfinance', 'GBPUSD=X', '2024-06-03', "
        "'1.25', '2024-06-03T18:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    price = _source(tmp_path).gbp_price("DELL", "2024-06-03")

    assert price == pytest.approx(96.0)


def test_usd_holding_falls_back_to_the_fx_rate_cache(tmp_path: Path) -> None:
    _seed_dell(tmp_path)
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {"2024-06-03": 1.5}, "GBPUSD=X"
    )

    price = _source(tmp_path).gbp_price("DELL", "2024-06-03")

    assert price == pytest.approx(80.0)


def test_usd_holding_without_a_dated_rate_has_no_price(tmp_path: Path) -> None:
    _seed_dell(tmp_path)
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {"2024-06-02": 1.25}, "GBPUSD=X"
    )

    assert _source(tmp_path).gbp_price("DELL", "2024-06-03") is None


def test_a_nearby_session_is_never_substituted(tmp_path: Path) -> None:
    _seed_lgen(tmp_path)

    assert _source(tmp_path).gbp_price("LGEN.L", "2024-06-04") is None


def test_uncached_ticker_has_no_price(tmp_path: Path) -> None:
    _seed_lgen(tmp_path)

    assert _source(tmp_path).gbp_price("WCOG", "2024-06-03") is None


def test_missing_cache_database_has_no_price(tmp_path: Path) -> None:
    """An absent cache (no tables at all) is no evidence, not a crash."""
    connect = _trades_connect(tmp_path)
    source = HistoricalCacheGbpPriceSource(
        HistoricalPriceRepository(db.make_connect(lambda: tmp_path / "absent.db")),
        FxQuoteRepository(connect),
        FxRateCacheRepository(connect),
        aliases=_ALIASES,
    )

    assert source.gbp_price("LGEN.L", "2024-06-03") is None


def test_aliased_ticker_resolves_to_the_cached_symbol(tmp_path: Path) -> None:
    _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-fund",
        requested_symbol="0P00013P6I.L",
        currency="GBP",
        quote_unit="GBP",
        quote_unit_scale="1",
    )
    _add_observation(
        tmp_path, data_revision="rev-fund", session_date="2024-06-03", close=1.75
    )

    price = _source(tmp_path).gbp_price("HSFWA", "2024-06-03")

    assert price == pytest.approx(1.75)


def test_overlapping_revisions_pick_the_newest_acquisition(tmp_path: Path) -> None:
    _cache_repo(tmp_path)
    for revision, acquired, close in (
        ("rev-old", "2026-01-01T00:00:00+00:00", 100.0),
        ("rev-new", "2026-02-01T00:00:00+00:00", 200.0),
    ):
        _add_revision(
            tmp_path,
            data_revision=revision,
            requested_symbol="LGEN.L",
            currency="GBP",
            quote_unit="GBp",
            quote_unit_scale="0.01",
            first_acquired_at=acquired,
        )
        _add_observation(
            tmp_path,
            data_revision=revision,
            session_date="2024-06-03",
            close=close,
        )

    price = _source(tmp_path).gbp_price("LGEN.L", "2024-06-03")

    assert price == pytest.approx(2.0)


def test_nan_close_is_treated_as_absent_evidence(tmp_path: Path) -> None:
    _seed_lgen(tmp_path, close=float("nan"))

    assert _source(tmp_path).gbp_price("LGEN.L", "2024-06-03") is None


def test_non_positive_close_is_treated_as_absent_evidence(tmp_path: Path) -> None:
    """A stored 0.0 close is not a price -- it would understate the total."""
    _seed_lgen(tmp_path, close=0.0)

    assert _source(tmp_path).gbp_price("LGEN.L", "2024-06-03") is None


def test_close_without_a_currency_is_not_assumed_to_be_gbp(tmp_path: Path) -> None:
    """An undenominated close cannot be converted, so it is no evidence."""
    _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-blank",
        requested_symbol="MYST",
        currency="",
        quote_unit="",
        quote_unit_scale="1",
    )
    _add_observation(
        tmp_path, data_revision="rev-blank", session_date="2024-06-03", close=12.0
    )

    assert _source(tmp_path).gbp_price("MYST", "2024-06-03") is None


def test_build_price_source_never_creates_a_cache_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout that never ran a backtest has no ``data/`` at all (#480).

    The production wiring must report gaps instead of crashing on the
    absent file -- and must not leave an empty database behind, which a
    read-write ``sqlite3.connect`` would silently create.
    """
    absent = tmp_path / "never-created" / "historical_price_cache.db"
    monkeypatch.setattr(snapshot_price_evidence, "HISTORICAL_PRICE_CACHE", absent)

    source = snapshot_price_evidence.build_price_source(_trades_connect(tmp_path))

    assert source.gbp_price("LGEN.L", "2024-06-03") is None
    assert not absent.exists()


def test_an_unreadable_cache_raises_rather_than_reporting_a_gap(
    tmp_path: Path,
) -> None:
    """A read failure must not be mistaken for "no evidence".

    Reporting a gap on a transient fault would let the repair pass null
    every real ``0.00`` row it was asked to reconstruct.
    """
    corrupt = tmp_path / "historical_price_cache.db"
    corrupt.write_text("this is not a database")
    repo = HistoricalPriceRepository(db.make_connect(lambda: corrupt))

    with pytest.raises(sqlite3.DatabaseError):
        repo.dated_close(["LGEN.L"], "2024-06-03")


def test_usd_holding_falls_back_to_the_historical_price_cache(
    tmp_path: Path,
) -> None:
    """A dated rate found only in ``historical_price_cache`` (#496) still
    converts the holding when both ``fx_quotes`` and ``fx_rate_cache`` miss."""
    _seed_dell(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-fx",
        requested_symbol="GBPUSD=X",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1",
    )
    _add_observation(
        tmp_path, data_revision="rev-fx", session_date="2024-06-03", close=1.25
    )

    price = _source(tmp_path).gbp_price("DELL", "2024-06-03")

    assert price == pytest.approx(96.0)


def test_all_three_fx_sources_missing_leaves_no_price(tmp_path: Path) -> None:
    _seed_dell(tmp_path)

    assert _source(tmp_path).gbp_price("DELL", "2024-06-03") is None


def test_dated_close_seeks_the_revisions_table_rather_than_scanning_observations(
    tmp_path: Path,
) -> None:
    """``dated_close`` must use ``idx_historical_revisions_requested_symbol``.

    Without an index on ``requested_symbol``, SQLite's planner chooses to
    scan the whole (many-million-row in production) observations table
    instead of the much smaller revisions table -- turning one lookup into
    tens of seconds (#480/#481 follow-up). This asserts the query plan
    seeks both tables by index rather than scanning either.
    """
    _seed_lgen(tmp_path)
    conn = sqlite3.connect(tmp_path / "historical_price_cache.db")
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT r.security_id FROM historical_price_revisions r "
        "JOIN historical_price_observations o ON o.data_revision = r.data_revision "
        "WHERE r.requested_symbol = ? AND o.session_date = ? "
        "ORDER BY r.first_acquired_at DESC, r.data_revision LIMIT 1",
        ("LGEN.L", "2024-06-03"),
    ).fetchall()
    steps = "\n".join(str(row) for row in plan)
    assert "SCAN" not in steps, steps
    assert "idx_historical_revisions_requested_symbol" in steps


# --- GH-550: cash converts off a bounded nearest-prior rate ------------------


def _counting_source(tmp_path: Path) -> tuple[HistoricalCacheGbpPriceSource, list[str]]:
    """Return a source plus the list of dates its ``_dated_rate`` was asked for.

    The lookback's cost is the whole point of bounding it, so the tests
    assert the number of probes, not just the answer (#550).
    """
    source = _source(tmp_path)
    probed: list[str] = []
    inner = source._dated_rate

    def _spy(pair: str, as_of: str) -> float | None:
        probed.append(as_of)
        return inner(pair, as_of)

    source._dated_rate = _spy  # type: ignore[method-assign]
    return source, probed


def test_gbp_rate_uses_the_exact_day_without_looking_back(tmp_path: Path) -> None:
    """A rate stored for ``as_of`` ends the walk on its first probe (#550)."""
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {"2024-06-03": 1.25, "2024-06-02": 9.99}, "GBPUSD=X"
    )
    source, probed = _counting_source(tmp_path)

    assert source.gbp_rate("USD", "2024-06-03") == pytest.approx(1.25)
    assert probed == ["2024-06-03"]


@pytest.mark.parametrize(
    "offset_days", [1, 2, snapshot_price_evidence.FX_LOOKBACK_DAYS]
)
def test_gbp_rate_resolves_a_hole_from_the_nearest_earlier_rate(
    tmp_path: Path, offset_days: int
) -> None:
    """A hole in the FX series must not discard a stated cash balance (#550)."""
    stored = (date(2024, 6, 3) - timedelta(days=offset_days)).isoformat()
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {stored: 1.25}, "GBPUSD=X"
    )

    assert _source(tmp_path).gbp_rate("USD", "2024-06-03") == pytest.approx(1.25)


def test_gbp_rate_stops_at_the_shared_bound(tmp_path: Path) -> None:
    """One day past the bound is a gap, not an arbitrarily stale figure."""
    stale = (
        date(2024, 6, 3) - timedelta(days=snapshot_price_evidence.FX_LOOKBACK_DAYS + 1)
    ).isoformat()
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {stale: 1.25}, "GBPUSD=X"
    )
    source, probed = _counting_source(tmp_path)

    assert source.gbp_rate("USD", "2024-06-03") is None
    assert len(probed) == snapshot_price_evidence.FX_LOOKBACK_DAYS + 1


def test_gbp_rate_never_uses_a_rate_published_after_the_day(tmp_path: Path) -> None:
    """A later rate was not knowable on ``as_of``; the walk only goes back."""
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {"2024-06-04": 1.25, "2024-06-05": 1.26}, "GBPUSD=X"
    )

    assert _source(tmp_path).gbp_rate("USD", "2024-06-03") is None


def test_gbp_rate_short_circuits_sterling_without_any_lookup(tmp_path: Path) -> None:
    """GBP per GBP is 1.0 by definition, so no evidence is consulted."""
    source, probed = _counting_source(tmp_path)

    assert source.gbp_rate("gbp", "2024-06-03") == pytest.approx(1.0)
    assert probed == []


def test_gbp_rate_does_not_raise_on_a_malformed_date(tmp_path: Path) -> None:
    """A backfill spanning years must not abort on one unparseable day."""
    source, probed = _counting_source(tmp_path)

    assert source.gbp_rate("USD", "not-a-date") is None
    assert probed == ["not-a-date"]


def test_gbp_price_still_refuses_a_nearby_rate(tmp_path: Path) -> None:
    """#550's lookback is for cash only: a holding stays exact-date."""
    _seed_dell(tmp_path)
    FxRateCacheRepository(_trades_connect(tmp_path)).upsert_many(
        {"2024-06-02": 1.25}, "GBPUSD=X"
    )

    assert _source(tmp_path).gbp_price("DELL", "2024-06-03") is None


# --- GH-555: a stored close describes today's shares, not the day's --------


def _add_split(
    tmp_path: Path, *, data_revision: str, session_date: str, ratio: float
) -> None:
    """Record one split action against a revision (append-only tables)."""
    conn = sqlite3.connect(tmp_path / "historical_price_cache.db")
    conn.execute(
        "INSERT INTO historical_corporate_actions"
        " (data_revision, session_date, action_type, value_hex)"
        " VALUES (?, ?, 'split', ?)",
        (data_revision, session_date, float(ratio).hex()),
    )
    conn.commit()
    conn.close()


def test_split_factor_multiplies_every_later_split(tmp_path: Path) -> None:
    repo = _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-1",
        requested_symbol="TSLA",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1.0",
    )
    _add_split(tmp_path, data_revision="rev-1", session_date="2022-08-25", ratio=3.0)
    _add_split(tmp_path, data_revision="rev-1", session_date="2020-08-31", ratio=5.0)

    # Only the 2022 split follows a 2021 trade; the 2020 one is already in
    # the shares that were bought.
    assert repo.split_factor_since(["TSLA"], "2021-03-01") == pytest.approx(3.0)
    assert repo.split_factor_since(["TSLA"], "2019-01-01") == pytest.approx(15.0)
    assert repo.split_factor_since(["TSLA"], "2023-01-01") == pytest.approx(1.0)


def test_split_factor_is_read_across_revisions_not_just_the_covering_one(
    tmp_path: Path,
) -> None:
    """The narrow revision a dated close comes from is adjusted for splits
    its own window cannot contain -- it was fetched later (#555)."""
    repo = _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="narrow",
        requested_symbol="TSLA",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1.0",
        first_acquired_at="2026-09-05T00:00:00+00:00",
    )
    _add_revision(
        tmp_path,
        data_revision="wide",
        requested_symbol="TSLA",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1.0",
        first_acquired_at="2026-09-02T00:00:00+00:00",
    )
    # The split lives only in the older, wider revision.
    _add_split(tmp_path, data_revision="wide", session_date="2022-08-25", ratio=3.0)

    assert repo.split_factor_since(["TSLA"], "2021-03-01") == pytest.approx(3.0)


def test_a_split_recorded_by_two_revisions_is_counted_once(tmp_path: Path) -> None:
    repo = _cache_repo(tmp_path)
    for revision in ("rev-a", "rev-b"):
        _add_revision(
            tmp_path,
            data_revision=revision,
            requested_symbol="TSLA",
            currency="USD",
            quote_unit="USD",
            quote_unit_scale="1.0",
        )
        _add_split(
            tmp_path, data_revision=revision, session_date="2022-08-25", ratio=3.0
        )

    assert repo.split_factor_since(["TSLA"], "2021-03-01") == pytest.approx(3.0)


def test_split_factor_is_none_for_a_symbol_the_cache_never_saw(
    tmp_path: Path,
) -> None:
    repo = _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-1",
        requested_symbol="TSLA",
        currency="USD",
        quote_unit="USD",
        quote_unit_scale="1.0",
    )

    assert repo.split_factor_since(["NOTHING"], "2021-03-01") is None
    # Known symbol, no split recorded: an honest "no adjustment", not a gap.
    assert repo.split_factor_since(["TSLA"], "2021-03-01") == pytest.approx(1.0)


def test_the_price_source_resolves_the_factor_through_the_same_aliases(
    tmp_path: Path,
) -> None:
    """The factor and the close it corrects must describe one listing."""
    repo = _cache_repo(tmp_path)
    _add_revision(
        tmp_path,
        data_revision="rev-1",
        requested_symbol="0P00013P6I.L",
        currency="GBP",
        quote_unit="GBP",
        quote_unit_scale="1.0",
    )
    _add_split(tmp_path, data_revision="rev-1", session_date="2025-01-01", ratio=2.0)
    source = HistoricalCacheGbpPriceSource(
        repo,
        FxQuoteRepository(_trades_connect(tmp_path)),
        FxRateCacheRepository(_trades_connect(tmp_path)),
        aliases=_ALIASES,
    )

    assert source.split_factor_since("HSFWA", "2024-01-01") == pytest.approx(2.0)
