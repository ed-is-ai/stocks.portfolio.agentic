"""Unit tests for CongressClient (HTML parsing, persistent cache, batch fetch)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from app.integrations.congress import CongressClient, CongressStats, _Lane


def _row(chamber: str, txn_type: str, when: date) -> str:
    """Build one QuiverQuant-style table row the parser will recognize."""
    when_str = when.strftime("%b %d, %Y")
    return (
        "<tr>"
        f'<a href="/congresstrading/trade/{chamber}-Smith">link</a>'
        f"<span class='x'>{txn_type}</span>"
        f"<td>{when_str}</td>"
    )


def _html(*rows: str) -> str:
    return "<table>" + "".join(rows) + "</table>"


def _recent() -> date:
    return date.today() - timedelta(days=30)


def _stale() -> date:
    return date.today() - timedelta(days=400)


# ---------------------------------------------------------------------------
# Step 2: Chamber and type counting
# ---------------------------------------------------------------------------


def test_house_purchase_and_sale_count() -> None:
    client = CongressClient()
    html = _html(
        _row("House", "Purchase", _recent()),
        _row("House", "Sale", _recent()),
    )
    result = client._parse_stats(html)
    assert result.buys == 1
    assert result.sells == 1
    assert result.senate_buys == 0
    assert result.senate_sells == 0


def test_senate_purchase_and_sale_count() -> None:
    client = CongressClient()
    html = _html(
        _row("Senate", "Purchase", _recent()),
        _row("Senate", "Sale", _recent()),
    )
    result = client._parse_stats(html)
    assert result.buys == 1
    assert result.sells == 1
    assert result.senate_buys == 1
    assert result.senate_sells == 1


def test_mixed_house_and_senate_aggregates() -> None:
    client = CongressClient()
    html = _html(
        _row("House", "Purchase", _recent()),
        _row("House", "Sale", _recent()),
        _row("Senate", "Purchase", _recent()),
        _row("Senate", "Sale", _recent()),
    )
    result = client._parse_stats(html)
    assert result.buys == 2
    assert result.sells == 2
    assert result.senate_buys == 1
    assert result.senate_sells == 1


def test_multiple_senate_purchases_accumulate() -> None:
    client = CongressClient()
    html = _html(
        _row("Senate", "Purchase", _recent()),
        _row("Senate", "Purchase", _recent()),
    )
    result = client._parse_stats(html)
    assert result.buys == 2
    assert result.senate_buys == 2
    assert result.sells == 0
    assert result.senate_sells == 0


# ---------------------------------------------------------------------------
# Step 3: Skip branches
# ---------------------------------------------------------------------------


def test_skip_stale_date() -> None:
    client = CongressClient()
    html = _html(_row("House", "Purchase", _stale()))
    result = client._parse_stats(html)
    assert result == CongressStats(0, 0, 0, 0)


def test_skip_no_chamber_link() -> None:
    client = CongressClient()
    # Row has type and date but no congresstrading/trade/ href
    when_str = _recent().strftime("%b %d, %Y")
    row = (
        "<tr>"
        "<a href='/other/path'>link</a>"
        "<span class='x'>Purchase</span>"
        f"<td>{when_str}</td>"
    )
    html = _html(row)
    result = client._parse_stats(html)
    assert result == CongressStats(0, 0, 0, 0)


def test_skip_no_type_span() -> None:
    client = CongressClient()
    when_str = _recent().strftime("%b %d, %Y")
    row = (
        f"<tr><a href='/congresstrading/trade/House-Smith'>link</a><td>{when_str}</td>"
    )
    html = _html(row)
    result = client._parse_stats(html)
    assert result == CongressStats(0, 0, 0, 0)


def test_skip_no_date() -> None:
    client = CongressClient()
    row = (
        "<tr>"
        "<a href='/congresstrading/trade/House-Smith'>link</a>"
        "<span class='x'>Purchase</span>"
        "<td>no-date-here</td>"
    )
    html = _html(row)
    result = client._parse_stats(html)
    assert result == CongressStats(0, 0, 0, 0)


def test_skip_unparseable_date_but_sibling_valid_row_counted() -> None:
    client = CongressClient()
    # "Zzz 99, 9999" matches _DATE_RE but strptime raises ValueError
    bad_row = (
        "<tr>"
        "<a href='/congresstrading/trade/House-Smith'>link</a>"
        "<span class='x'>Purchase</span>"
        "<td>Zzz 99, 9999</td>"
    )
    good_row = _row("House", "Purchase", _recent())
    html = _html(bad_row, good_row)
    result = client._parse_stats(html)
    assert result.buys == 1
    assert result.sells == 0


def test_empty_html_returns_all_zeros() -> None:
    client = CongressClient()
    result = client._parse_stats("")
    assert result == CongressStats(0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Step 4: Exchange behavior (current quirk)
# ---------------------------------------------------------------------------


def test_exchange_not_counted_in_buys_or_sells() -> None:
    # NOTE: This documents current behavior, not necessarily intended behavior.
    # The parser matches "Exchange" as a valid txn_type via _TYPE_RE, but
    # CongressStats only aggregates Purchase and Sale — Exchange silently
    # lands in the Counter under e.g. "House_Exchange" and is then ignored.
    client = CongressClient()
    html = _html(_row("House", "Exchange", _recent()))
    result = client._parse_stats(html)
    assert result.buys == 0
    assert result.sells == 0
    assert result.senate_buys == 0
    assert result.senate_sells == 0


# ---------------------------------------------------------------------------
# Step 5: Persistent cache and batched fetch
# ---------------------------------------------------------------------------


def test_get_stats_caches_result_and_skips_second_fetch(tmp_path) -> None:
    client = CongressClient(cache_db_path=tmp_path / "cache.db")
    html = _html(_row("House", "Purchase", _recent()))

    with patch.object(_Lane, "fetch_html", return_value=html) as mock_fetch:
        first = client.get_stats("AAPL")
        second = client.get_stats("AAPL")

    assert first == CongressStats(1, 0, 0, 0)
    assert second == first
    mock_fetch.assert_called_once()


def test_get_stats_caches_no_data_result_and_skips_second_fetch(tmp_path) -> None:
    client = CongressClient(cache_db_path=tmp_path / "cache.db")

    with patch.object(_Lane, "fetch_html", return_value=None) as mock_fetch:
        first = client.get_stats("ZZZZ")
        second = client.get_stats("ZZZZ")

    assert first is None
    assert second is None
    mock_fetch.assert_called_once()


def test_get_stats_refetches_after_cache_entry_goes_stale(tmp_path) -> None:
    db_path = tmp_path / "cache.db"
    client = CongressClient(cache_db_path=db_path)
    html = _html(_row("House", "Purchase", _recent()))

    with patch.object(_Lane, "fetch_html", return_value=html):
        client.get_stats("AAPL")

    stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE congress_cache SET fetched_at = ? WHERE ticker = ?",
        (stale_timestamp, "AAPL"),
    )
    conn.commit()
    conn.close()

    with patch.object(_Lane, "fetch_html", return_value=html) as mock_fetch:
        client.get_stats("AAPL")

    mock_fetch.assert_called_once()


def test_get_stats_many_resolves_cache_hits_without_fetching(tmp_path) -> None:
    client = CongressClient(cache_db_path=tmp_path / "cache.db")
    html = _html(_row("House", "Purchase", _recent()))

    with patch.object(_Lane, "fetch_html", return_value=html):
        client.get_stats("AAPL")  # warm the cache

    with patch.object(_Lane, "fetch_html", return_value=html) as mock_fetch:
        result = client.get_stats_many(["AAPL"])

    assert result == {"AAPL": CongressStats(1, 0, 0, 0)}
    mock_fetch.assert_not_called()


def test_get_stats_many_fetches_misses_then_caches_them(tmp_path) -> None:
    client = CongressClient(cache_db_path=tmp_path / "cache.db")
    html = _html(_row("House", "Purchase", _recent()))

    with patch.object(_Lane, "fetch_html", return_value=html) as mock_fetch:
        result = client.get_stats_many(["AAPL", "GOOGL", "MSFT"])

    assert set(result) == {"AAPL", "GOOGL", "MSFT"}
    assert all(stats == CongressStats(1, 0, 0, 0) for stats in result.values())
    assert mock_fetch.call_count == 3

    with patch.object(_Lane, "fetch_html", return_value=html) as mock_fetch_again:
        client.get_stats_many(["AAPL", "GOOGL", "MSFT"])

    mock_fetch_again.assert_not_called()


def test_get_stats_many_returns_empty_dict_for_no_tickers(tmp_path) -> None:
    client = CongressClient(cache_db_path=tmp_path / "cache.db")
    assert client.get_stats_many([]) == {}
