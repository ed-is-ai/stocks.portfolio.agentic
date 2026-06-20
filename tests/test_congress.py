"""Unit tests for CongressClient._parse_stats (pure HTML parsing)."""

from __future__ import annotations

from datetime import date, timedelta

from app.integrations.congress import CongressClient, CongressStats


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
