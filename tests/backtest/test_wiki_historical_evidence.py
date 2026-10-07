"""The WIKI archive as a historical price evidence provider (#82 B)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.repositories import db
from app.repositories.historical_price_repo import (
    HistoricalEvidenceIntegrityError,
    HistoricalPriceRepository,
)
from app.repositories.wiki_price_repo import WikiPriceRepository
from app.services.backtest.historical_data_qualification import (
    FailureCode,
    ProviderFailure,
)
from app.services.backtest.historical_initialization_engine import (
    CanonicalSnapshotMonthProcessor,
)
from app.services.backtest.historical_price_evidence import HistoricalEvidenceRequest
from app.services.backtest.market_planes import HistoricalMarketPlanes
from app.services.backtest.snapshot_profile import FULL_HISTORY_START
from app.services.backtest.wiki_historical_evidence import (
    WikiHistoricalEvidenceAdapter,
)
from tests.backtest.test_historical_price_repository import _LEGACY_REVISIONS_TABLE

HEADER = "ticker,date,open,high,low,close,volume,ex-dividend,split_ratio,adj_close"
# SPLT: $1 dividend on day 1, 2-for-1 split on day 3 (WIKI close halves).
# PLAIN: no actions. WIN: rows either side of a 2008 H1 window.
ROWS = """\
SPLT,2008-01-02,100.0,104.0,98.0,102.0,1000.0,1.0,1.0,1
SPLT,2008-01-03,102.0,106.0,101.0,104.0,1100.0,0.0,1.0,1
SPLT,2008-01-04,52.0,53.0,51.0,52.5,2400.0,0.0,2.0,1
PLAIN,2008-01-02,10.0,11.0,9.5,10.5,10.0,0.0,1.0,1
PLAIN,2008-01-03,10.5,10.75,10.25,10.625,20.0,0.0,1.0,1
WIN,2007-12-31,5.0,5.0,5.0,5.0,1.0,0.0,1.0,1
WIN,2008-03-03,6.0,6.0,6.0,6.0,1.0,0.0,1.0,1
WIN,2008-07-01,7.0,7.0,7.0,7.0,1.0,0.0,1.0,1
BADS,2008-01-02,10.0,10.0,10.0,10.0,1.0,0.0,1.0,1
BADS,2008-01-03,10.0,10.0,10.0,10.0,1.0,0.0,1.0,1
BADS,2008-01-04,0.0,0.0,0.0,0.0,1.0,0.0,2.0,1
"""
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
DAYS = (date(2008, 1, 2), date(2008, 1, 3), date(2008, 1, 4))


@pytest.fixture
def wiki_db(tmp_path: Path) -> Path:
    csv_path = tmp_path / "WIKI.csv"
    csv_path.write_text(f"{HEADER}\n{ROWS}")
    path = tmp_path / "wiki_prices.db"
    repo = WikiPriceRepository(db.make_connect(lambda: path))
    repo.ensure_schema()
    repo.import_csv(csv_path)
    return path


def _request(
    symbol: str,
    start: date = date(2008, 1, 1),
    end: date = date(2008, 2, 1),
    sessions: tuple[date, ...] = DAYS,
    *,
    canonical: bool = False,
    allow_missing_prefix: bool = False,
) -> HistoricalEvidenceRequest:
    return HistoricalEvidenceRequest(
        security_id=f"sec-{symbol}",
        alias_revision="alias-v1",
        symbol=symbol,
        start=start,
        end=end,
        expected_sessions=sessions,
        allowed_observed_symbols=(symbol,),
        expected_currency="USD",
        expected_quote_unit="USD",
        expected_timezone="America/New_York",
        allow_missing_prefix=allow_missing_prefix,
        canonical_exchange_sessions=canonical,
    )


def _fetch(wiki_db: Path, request: HistoricalEvidenceRequest):
    return WikiHistoricalEvidenceAdapter(wiki_db, clock=lambda: NOW).fetch(request)


def _repo(tmp_path: Path) -> HistoricalPriceRepository:
    repo = HistoricalPriceRepository(
        db.make_connect(lambda: tmp_path / "historical-prices.db")
    )
    repo.ensure_schema()
    return repo


def _closes(rows) -> list[Decimal]:
    return [row.close for row in rows]


def test_plain_series_matches_wiki_as_traded(wiki_db: Path) -> None:
    payload = _fetch(wiki_db, _request("PLAIN", sessions=DAYS[:2]))
    source_digest = WikiPriceRepository(
        db.make_connect(lambda: wiki_db)
    ).latest_import()

    assert source_digest is not None
    assert payload.provider == "wiki"
    assert payload.provider_version == source_digest.source_digest
    assert payload.request_contract_version == "WikiArchiveDailyV1"
    assert (payload.requested_symbol, payload.observed_symbol) == ("PLAIN", "PLAIN")
    assert (payload.currency, payload.quote_unit, payload.quote_unit_scale) == (
        "USD",
        "USD",
        "1",
    )
    assert payload.exchange_timezone == "America/New_York"
    assert payload.actions == ()
    assert payload.rows[0] == {
        "session": "2008-01-02",
        "open": (10.0).hex(),
        "high": (11.0).hex(),
        "low": (9.5).hex(),
        "close": (10.5).hex(),
        "adj_close": None,
        "volume": (10.0).hex(),
        "dividends": (0.0).hex(),
        "stock_splits": (0.0).hex(),
    }
    planes = HistoricalMarketPlanes.from_evidence(payload)  # type: ignore[arg-type]
    assert _closes(planes.as_traded()) == [Decimal("10.5"), Decimal("10.625")]


def test_split_rows_are_adjusted_and_as_traded_restores_wiki(wiki_db: Path) -> None:
    payload = _fetch(wiki_db, _request("SPLT"))

    assert [row["close"] for row in payload.rows] == [
        (51.0).hex(),
        (52.0).hex(),
        (52.5).hex(),
    ]
    assert [row["volume"] for row in payload.rows] == [
        (1000.0).hex(),
        (1100.0).hex(),
        (2400.0).hex(),
    ]
    assert payload.actions == (
        {"session": "2008-01-02", "action_type": "dividend", "value": (0.5).hex()},
        {"session": "2008-01-04", "action_type": "split", "value": (2.0).hex()},
    )
    planes = HistoricalMarketPlanes.from_evidence(payload)  # type: ignore[arg-type]
    assert _closes(planes.as_traded()) == [
        Decimal("102"),
        Decimal("104"),
        Decimal("52.5"),
    ]
    continuous = planes.split_continuous_window_as_of(DAYS[2], limit=None)
    assert _closes(continuous) == [Decimal("51"), Decimal("52"), Decimal("52.5")]


def test_window_drops_rows_outside_request(wiki_db: Path) -> None:
    request = _request(
        "WIN",
        date(2008, 1, 1),
        date(2008, 7, 1),
        (date(2008, 3, 3),),
    )
    assert [row["session"] for row in _fetch(wiki_db, request).rows] == ["2008-03-03"]


def test_expected_sessions_follow_yfinance_rules(wiki_db: Path) -> None:
    canonical = _request("SPLT", sessions=DAYS[1:], canonical=True)
    payload = _fetch(wiki_db, canonical)
    assert [row["session"] for row in payload.rows] == ["2008-01-03", "2008-01-04"]
    assert payload.request_contract["observation_policy"] == (
        "canonical_exchange_sessions_v2"
    )

    prefix = (date(2007, 12, 31), *DAYS)
    allowed = _request("SPLT", sessions=prefix, allow_missing_prefix=True)
    assert len(_fetch(wiki_db, allowed).rows) == 3

    with pytest.raises(ProviderFailure) as exc_info:
        _fetch(wiki_db, _request("SPLT", sessions=prefix))
    assert exc_info.value.code is FailureCode.REQUIRED_DATA_MISSING


def test_missing_ticker_is_required_data_missing(wiki_db: Path) -> None:
    for request in (_request("NOPE"), _request("NOPE", canonical=True)):
        with pytest.raises(ProviderFailure) as exc_info:
            _fetch(wiki_db, request)
        assert exc_info.value.code is FailureCode.REQUIRED_DATA_MISSING


def test_missing_database_names_it(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(ProviderFailure, match="absent.db") as exc_info:
        _fetch(missing, _request("SPLT"))
    assert exc_info.value.code is FailureCode.PROVIDER_UNAVAILABLE
    assert not missing.exists()


def test_store_and_read_round_trips_split(wiki_db: Path, tmp_path: Path) -> None:
    payload = _fetch(wiki_db, _request("SPLT"))
    repo = _repo(tmp_path)

    revision = repo.commit(payload)
    stored = repo.verify(revision)
    assert stored.provider == "wiki"
    planes = HistoricalMarketPlanes.from_evidence(stored)
    assert _closes(planes.as_traded())[0] == Decimal("102")

    handle = repo.open_read(revision)
    try:
        assert handle.metadata.provider == "wiki"
        row = handle.as_traded_row(DAYS[0])
        assert row is not None and row.close == Decimal("102")
    finally:
        handle.close()


def test_migration_admits_wiki_on_a_pre_wiki_database(tmp_path: Path) -> None:
    path = tmp_path / "historical-prices.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        _LEGACY_REVISIONS_TABLE.replace(
            "CHECK(provider = 'yfinance')",
            "CHECK(provider IN ('yfinance', 'bank_of_england'))",
        )
    )
    conn.execute(
        """INSERT INTO historical_price_revisions VALUES (
            'rev-old', 'security-1', 'yfinance', 'v1', 'contract-v1',
            'AAPL', 'AAPL', 'alias-v1', 'USD', 'USD', '1',
            'America/New_York', '2024-01-01', '2024-02-01', '{}',
            'digest-1', '{}', 1, 0, '2026-08-11T00:00:00+00:00'
        )"""
    )
    conn.commit()
    indexes_before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    ).fetchall()
    conn.close()

    repo = HistoricalPriceRepository(db.make_connect(lambda: path))
    repo.ensure_schema()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT data_revision FROM historical_price_revisions"
        ).fetchall() == [("rev-old",)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert set(indexes_before) <= set(
            conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        )
        conn.execute(
            """INSERT INTO historical_price_revisions VALUES (
                'rev-wiki', 'security-2', 'wiki', 'digest', 'WikiArchiveDailyV1',
                'ENDS', 'ENDS', NULL, 'USD', 'USD', '1', 'America/New_York',
                '2008-01-01', '2008-02-01', '{}', 'digest-2', '{}', 1, 0,
                '2026-10-07T00:00:00+00:00'
            )"""
        )
    finally:
        conn.close()


def test_unrecognised_provider_check_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "historical-prices.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        _LEGACY_REVISIONS_TABLE.replace(
            "CHECK(provider = 'yfinance')", "CHECK(provider <> '')"
        )
    )
    conn.close()

    with pytest.raises(HistoricalEvidenceIntegrityError):
        HistoricalPriceRepository(db.make_connect(lambda: path)).ensure_schema()


class _NoYFinance:
    def fetch(self, _request):
        raise AssertionError("yfinance must not price a wiki member")


def _processor(tmp_path: Path, wiki_db: Path) -> CanonicalSnapshotMonthProcessor:
    processor = object.__new__(CanonicalSnapshotMonthProcessor)
    setattr(processor, "_price_repository", _repo(tmp_path))
    setattr(processor, "_evidence_adapter", _NoYFinance())
    setattr(
        processor,
        "_evidence_adapters",
        {"wiki": WikiHistoricalEvidenceAdapter(wiki_db, clock=lambda: NOW)},
    )
    setattr(processor, "_alias_revision", "alias-v1")
    setattr(processor, "_evidence_cache", {})
    setattr(processor, "_validated_evidence_cache", set())
    setattr(processor, "_fetched_security_ids", set())
    return processor


def _engine_request() -> HistoricalEvidenceRequest:
    """The engine's full-history canonical request shape."""
    return _request("SPLT", FULL_HISTORY_START, date(2026, 10, 1), canonical=True)


def test_engine_prices_wiki_members_with_the_wiki_adapter(
    wiki_db: Path, tmp_path: Path
) -> None:
    processor = _processor(tmp_path, wiki_db)
    member = SimpleNamespace(
        security_id="sec-SPLT", provider_symbol="SPLT", provider="wiki"
    )

    evidence = processor._evidence_for(member, _engine_request())  # type: ignore[arg-type]
    assert evidence.provider == "wiki"

    # A fresh run finds the stored WIKI revision instead of re-reading WIKI.
    rerun = _processor(tmp_path, tmp_path / "absent.db")
    again = rerun._evidence_for(member, _engine_request())  # type: ignore[arg-type]
    assert again.data_revision == evidence.data_revision


def test_engine_defaults_members_to_yfinance(wiki_db: Path, tmp_path: Path) -> None:
    processor = _processor(tmp_path, wiki_db)
    member = SimpleNamespace(security_id="sec-SPLT", provider_symbol="SPLT")

    with pytest.raises(AssertionError, match="yfinance must not price"):
        processor._evidence_for(member, _engine_request())  # type: ignore[arg-type]


def test_skipped_invalid_split_row_does_not_adjust_earlier_prices(
    wiki_db: Path,
) -> None:
    payload = _fetch(wiki_db, _request("BADS", canonical=True))
    assert [row["session"] for row in payload.rows] == ["2008-01-02", "2008-01-03"]
    assert [float.fromhex(str(row["close"])) for row in payload.rows] == [10.0, 10.0]
    assert payload.actions == ()


def test_unreadable_archive_is_provider_unavailable(tmp_path: Path) -> None:
    broken = tmp_path / "wiki_prices.db"
    broken.write_bytes(b"not a sqlite database at all")
    with pytest.raises(ProviderFailure) as exc:
        _fetch(broken, _request("PLAIN"))
    assert exc.value.code is FailureCode.PROVIDER_UNAVAILABLE
