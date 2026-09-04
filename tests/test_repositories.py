"""Unit tests for the repository layer (temp-file SQLite + artifact files)."""

import pytest

from app.repositories import db
from app.repositories.account_repo import AccountStateRepository
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.artifacts_repo import ArtifactsRepository
from app.repositories.cash_flows_repo import CashFlowsRepository
from app.repositories.fx_rate_cache_repo import FxRateCacheRepository
from app.repositories.price_cache_repo import PriceCacheRepository
from app.repositories.results_repo import ResultsRepository
from app.repositories.trades_repo import TradesRepository


@pytest.fixture
def trades_connect(tmp_path):
    """A Connect factory over an initialised temp trades.db."""
    path = tmp_path / "trades.db"
    connect = db.make_connect(lambda: path)
    with db.session(connect) as conn:
        db.init_trades_db(conn)
    return connect


def test_trades_and_cash_flows_seek_by_portfolio_id_rather_than_scanning(
    trades_connect,
):
    """``portfolio_id = ?`` must use an index, not a full table scan.

    The only pre-existing index touching ``portfolio_id`` is a partial
    unique index keyed on ``ifnull(portfolio_id, -1)``, which SQLite can't
    use for a plain equality predicate -- so ``TradesRepository.history()``/
    ``open_rows()`` and ``CashFlowsRepository.history()`` scanned the whole
    (unboundedly growing, across years of quarterly SIPP imports) table on
    every call. Asserts the query plan seeks by index instead.
    """
    with db.session(trades_connect) as conn:
        for table in ("trades", "cash_flows"):
            plan = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE portfolio_id = ?",
                (1,),
            ).fetchall()
            steps = "\n".join(str(row) for row in plan)
            assert "SCAN" not in steps, steps
            assert f"idx_{table}_portfolio_id" in steps, steps


# --- TradesRepository ------------------------------------------------------


def test_trades_insert_and_history(trades_connect):
    repo = TradesRepository(trades_connect)
    tid = repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024", "note")
    assert isinstance(tid, int) and tid > 0
    history = repo.history()
    assert len(history) == 1
    assert history[0].ticker == "AAPL"
    assert history[0].action == "BUY"


def test_trades_history_filter_and_delete(trades_connect):
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024")
    msft_id = repo.insert("MSFT", "BUY", 5, 200.0, "02/02/2024")
    assert len(repo.history("AAPL")) == 1
    assert repo.delete_by_id(msft_id) is True
    assert repo.delete_by_id(msft_id) is False
    repo.delete_by_ticker("AAPL")
    assert repo.history() == []


def test_trades_held_tickers_canonicalizes(trades_connect, monkeypatch):
    """Story 2.1: ``held_tickers()`` agrees with ``get_portfolio()``'s
    canonical identity even when trades are stored under a raw,
    alias-equivalent spelling -- the watchlist/orchestrator "held" check
    must never disagree with the Portfolio tab's canonical display."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    assert repo.held_tickers() == {"ABC"}


def test_trades_held_tickers_resolves_legacy_alias(trades_connect, monkeypatch):
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"HSFWA": "REAL.L"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("HSFWA", "BUY", 10, 100.0, "01/02/2024")
    assert repo.held_tickers() == {"REAL.L"}


def test_trades_delete_by_ticker_matches_alias_equivalent_raw_rows(
    trades_connect, monkeypatch
):
    """``correct_trade()`` regression: deleting by the canonical spelling
    the UI now shows must delete rows stored under a raw alias-equivalent
    spelling too -- never a silent no-op that double-counts shares."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    repo.delete_by_ticker("ABC")
    assert repo.history() == []


def test_trades_delete_by_ticker_alias_expansion_respects_portfolio_scope(
    trades_connect, monkeypatch
):
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024", portfolio_id=1)
    repo.insert("ABC.L", "BUY", 5, 100.0, "01/02/2024", portfolio_id=2)
    repo.delete_by_ticker("ABC", portfolio_id=1)
    assert repo.history(portfolio_id=1) == []
    assert len(repo.history(portfolio_id=2)) == 1


def test_trades_history_filter_matches_alias_equivalent_raw_rows(
    trades_connect, monkeypatch
):
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    history = repo.history("ABC")
    assert len(history) == 1
    assert history[0].ticker == "ABC"


def test_trades_history_canonicalizes_ticker_regardless_of_filter(
    trades_connect, monkeypatch
):
    """Every returned ``Trade.ticker`` is canonicalized -- one consistent
    display form -- whether or not a ``ticker`` filter was given."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases", lambda: {"ABC.L": "ABC"}
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    history = repo.history()
    assert history[0].ticker == "ABC"


def test_trades_delete_by_ticker_cyclic_alias_still_deletes_own_spelling(
    trades_connect, monkeypatch
):
    """A cyclic (misconfigured) alias chain must not crash or silently
    no-op ``delete_by_ticker`` -- it still deletes rows stored under the
    exact spelling passed in, even though the cycle means no *other* raw
    spelling can be safely assumed to be alias-equivalent."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases",
        lambda: {"ABC.L": "ABC", "ABC": "ABC.L"},
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    repo.delete_by_ticker("ABC.L")
    assert repo.history() == []


def test_trades_history_cyclic_alias_degrades_to_raw_ticker(
    trades_connect, monkeypatch
):
    """A cyclic alias chain degrades ``history()``'s displayed ticker to
    its raw, unresolved spelling rather than raising."""
    monkeypatch.setattr(
        "app.repositories.trades_repo.load_aliases",
        lambda: {"ABC.L": "ABC", "ABC": "ABC.L"},
    )
    repo = TradesRepository(trades_connect)
    repo.insert("ABC.L", "BUY", 10, 100.0, "01/02/2024")
    history = repo.history()
    assert history[0].ticker == "ABC.L"


def test_trades_open_rows_excludes_invalid(trades_connect):
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024")
    repo.insert("n/a", "BUY", 1, 1.0, "01/02/2024")
    rows = repo.open_rows()
    assert [r[0] for r in rows] == ["AAPL"]


def test_trades_insert_ignore_dedupes_idempotency_key(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        first = repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", None, "key-1"
        )
        second = repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", None, "key-1"
        )
    # Story 1.8, AC #2/#3: the method reports what actually happened rather
    # than leaving the caller to infer success from "no exception raised".
    assert (first, second) == ("inserted", "duplicate")
    assert len(repo.history("AAPL")) == 1


def test_trades_idempotency_keys_are_scoped_per_portfolio(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "01/02/2024", "", "REF1", 1, "key-1"
        )
        repo.insert_ignore(
            conn, "MSFT", "BUY", 5, 200.0, "02/02/2024", "", "REF2", 2, "key-2"
        )

    assert repo.idempotency_keys_for_portfolio(1) == {"key-1"}
    assert repo.idempotency_keys_for_portfolio(2) == {"key-2"}
    assert repo.idempotency_keys_for_portfolio(None) == set()


def test_trades_currency_defaults_to_gbp_and_persists(trades_connect):
    """Story 1.4, AC1: a trade's source currency is stored, not assumed."""
    repo = TradesRepository(trades_connect)
    gbp_id = repo.insert("AAPL", "BUY", 10, 100.0, "01/02/2024")
    usd_id = repo.insert("MSFT", "BUY", 5, 200.0, "02/02/2024", currency="USD")
    history = {t.id: t for t in repo.history()}
    assert history[gbp_id].currency == "GBP"
    assert history[usd_id].currency == "USD"


def test_trades_insert_ignore_threads_currency(trades_connect):
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn,
            "TSLA",
            "BUY",
            2,
            300.0,
            "01/02/2024",
            "",
            "REF-HKD",
            None,
            "key-hkd",
            "HKD",
        )
    assert repo.history()[0].currency == "HKD"


def test_trades_set_ack_writes_and_clears(trades_connect):
    """Story 1.5, AC #7: ``set_ack`` writes/clears ``realised_pnl_ack_at``,
    and only the targeted row is affected (an untouched trade stays
    ``None``)."""
    repo = TradesRepository(trades_connect)
    target_id = repo.insert("AAPL", "SELL", 5, 100.0, "01/02/2024")
    other_id = repo.insert("MSFT", "BUY", 5, 200.0, "02/02/2024")

    with db.session(trades_connect) as conn:
        repo.set_ack(conn, target_id, "2026-08-09T12:00:00+00:00")

    history = {t.id: t for t in repo.history()}
    assert history[target_id].realised_pnl_ack_at == "2026-08-09T12:00:00+00:00"
    assert history[other_id].realised_pnl_ack_at is None

    with db.session(trades_connect) as conn:
        repo.set_ack(conn, target_id, None)

    history = {t.id: t for t in repo.history()}
    assert history[target_id].realised_pnl_ack_at is None


# --- Story 2.2: deterministic replay ordering -------------------------------


def test_trades_insert_ignore_persists_source_row_index(trades_connect):
    """``source_row_index`` (the row's 0-based position in its source CSV)
    round-trips through ``insert_ignore`` into ``history()`` -- the only
    place a row's file position is captured, so it must survive the write."""
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn,
            "AAPL",
            "BUY",
            10,
            100.0,
            "2024-01-02",
            "",
            "REF1",
            None,
            "key-1",
            "GBP",
            3,
        )
    trade = repo.history()[0]
    assert trade.source_row_index == 3
    assert trade.idempotency_key == "key-1"


def test_trades_insert_ignore_source_row_index_defaults_to_none(trades_connect):
    """A caller that doesn't pass ``source_row_index`` (every pre-Story-2.2
    call site) still inserts cleanly, with the column left ``NULL``."""
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "2024-01-02", "", "REF1", None, "key-1"
        )
    trade = repo.history()[0]
    assert trade.source_row_index is None


def test_trades_open_rows_orders_same_day_by_descending_source_row_index(
    trades_connect,
):
    """Story 2.2: within a same-day group, ``open_rows`` (average-cost
    replay) processes the *highest* ``source_row_index`` first --
    the first-listed CSV row (lowest index) is the most recent execution of
    that day, so chronological (oldest-first) replay must reach it last."""
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        # idx 0 = first-listed/most-recent; idx 2 = last-listed/earliest.
        repo.insert_ignore(
            conn,
            "MOSTRECENT",
            "BUY",
            1,
            10.0,
            "2024-01-02",
            "",
            None,
            None,
            "k0",
            "GBP",
            0,
        )
        repo.insert_ignore(
            conn, "MIDDLE", "BUY", 1, 10.0, "2024-01-02", "", None, None, "k1", "GBP", 1
        )
        repo.insert_ignore(
            conn,
            "EARLIEST",
            "BUY",
            1,
            10.0,
            "2024-01-02",
            "",
            None,
            None,
            "k2",
            "GBP",
            2,
        )
    rows = repo.open_rows()
    assert [r[0] for r in rows] == ["EARLIEST", "MIDDLE", "MOSTRECENT"]


def test_trades_open_rows_null_source_row_index_sorts_last_in_date_group(
    trades_connect,
):
    """A pre-Story-2.2 row (``source_row_index IS NULL``, simulated via
    plain ``insert()``) participates in replay without crashing, and sorts
    as the lowest possible position in its date group -- last among
    same-day peers with a real index."""
    repo = TradesRepository(trades_connect)
    repo.insert("NOINDEX", "BUY", 1, 10.0, "2024-01-02")  # source_row_index NULL
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn,
            "HASINDEX",
            "BUY",
            1,
            10.0,
            "2024-01-02",
            "",
            None,
            None,
            "k0",
            "GBP",
            0,
        )
    rows = repo.open_rows()
    assert [r[0] for r in rows] == ["HASINDEX", "NOINDEX"]


def test_trades_open_rows_both_source_row_index_and_idempotency_key_null_falls_back_to_id(
    trades_connect,
):
    """Two same-day rows with both ``source_row_index`` and
    ``idempotency_key`` NULL -- e.g. two trades recorded manually via
    ``insert()`` rather than SIPP import, which sets neither column --
    must still sort deterministically by ascending ``id``, exactly like
    ``RealisedPnlService._replay_sort_key``'s identical fallback and the
    pre-Story-2.2 ``ORDER BY date, id`` this replaces. Without a trailing
    ``id`` tiebreak in ``_REPLAY_ORDER``, this tie would fall back to
    unspecified SQLite ordering."""
    repo = TradesRepository(trades_connect)
    repo.insert("FIRST", "BUY", 1, 10.0, "2024-01-02")
    repo.insert("SECOND", "BUY", 1, 10.0, "2024-01-02")
    rows = repo.open_rows()
    assert [r[0] for r in rows] == ["FIRST", "SECOND"]


# --- Story 2.4: trade provenance (source / import_batch_id) ----------------


def test_trades_insert_persists_source(trades_connect):
    """``insert()``'s new ``source`` parameter round-trips through
    ``history()`` -- the generic write path used by ``record_buy``/
    ``record_sell``/``correct_trade``/``record_opening_lot``."""
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "2024-01-02", source="manual")
    assert repo.history()[0].source == "manual"


def test_trades_insert_source_defaults_to_none(trades_connect):
    """A caller that doesn't pass ``source`` (every pre-Story-2.4 call
    site) still inserts cleanly, with the column left ``NULL``."""
    repo = TradesRepository(trades_connect)
    repo.insert("AAPL", "BUY", 10, 100.0, "2024-01-02")
    trade = repo.history()[0]
    assert trade.source is None
    assert trade.import_batch_id is None


def test_trades_insert_ignore_persists_source_and_import_batch_id(trades_connect):
    """``insert_ignore()``'s new ``source``/``import_batch_id`` parameters
    (appended after ``source_row_index``) round-trip through ``history()``
    -- the SIPP import write path."""
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn,
            "AAPL",
            "BUY",
            10,
            100.0,
            "2024-01-02",
            "",
            "REF1",
            None,
            "key-1",
            "GBP",
            3,
            "sipp_import",
            "batch-abc123",
        )
    trade = repo.history()[0]
    assert trade.source == "sipp_import"
    assert trade.import_batch_id == "batch-abc123"


def test_trades_insert_ignore_source_defaults_to_none(trades_connect):
    """A caller that doesn't pass ``source``/``import_batch_id`` still
    inserts cleanly, with both columns left ``NULL``."""
    repo = TradesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(
            conn, "AAPL", "BUY", 10, 100.0, "2024-01-02", "", "REF1", None, "key-1"
        )
    trade = repo.history()[0]
    assert trade.source is None
    assert trade.import_batch_id is None


def test_trades_update_opening_lot_updates_in_place(trades_connect):
    repo = TradesRepository(trades_connect)
    tid = repo.insert("AAPL", "BUY", 10, 100.0, "2024-01-02", source="opening_lot")
    updated = repo.update_opening_lot(tid, "AAPL", 15, 110.0, "2024-01-03", "edited")
    assert updated is True
    trade = repo.history()[0]
    assert trade.shares == 15
    assert trade.price == 110.0
    assert trade.date == "2024-01-03"
    assert trade.notes == "edited"
    assert trade.source == "opening_lot"


def test_trades_update_opening_lot_refuses_non_opening_lot_row(trades_connect):
    """``update_opening_lot`` never rewrites a trade that isn't itself an
    Opening Lot, even if called with its id -- defense in depth against
    editing an unrelated trade through this Opening-Lot-specific path."""
    repo = TradesRepository(trades_connect)
    tid = repo.insert("AAPL", "BUY", 10, 100.0, "2024-01-02", source="manual")
    updated = repo.update_opening_lot(tid, "AAPL", 15, 110.0, "2024-01-03")
    assert updated is False
    trade = repo.history()[0]
    assert trade.shares == 10  # unchanged


def test_trades_update_opening_lot_scoped_to_portfolio(trades_connect):
    repo = TradesRepository(trades_connect)
    tid = repo.insert(
        "AAPL", "BUY", 10, 100.0, "2024-01-02", portfolio_id=1, source="opening_lot"
    )
    updated = repo.update_opening_lot(
        tid, "AAPL", 15, 110.0, "2024-01-03", portfolio_id=2
    )
    assert updated is False


# --- CashFlowsRepository ---------------------------------------------------


def test_cash_flows_insert_ignore_dedupes(trades_connect):
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        first = repo.insert_ignore(
            conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1", None, "key-1"
        )
        second = repo.insert_ignore(
            conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1", None, "key-1"
        )
    assert (first, second) == ("inserted", "duplicate")
    with db.session(trades_connect) as conn:
        count = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    assert count == 1
    assert repo.idempotency_keys_for_portfolio(None) == {"key-1"}


def test_cash_flows_currency_defaults_to_gbp_and_persists(trades_connect):
    """Story 1.4, AC1: a cash flow's source currency is stored, not assumed."""
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(conn, "01/02/2024", "DIVIDEND", None, 12.5, "Div", "R1")
        repo.insert_ignore(
            conn,
            "01/02/2024",
            "DIVIDEND",
            None,
            9.5,
            "Div EUR",
            "R2",
            None,
            "key-eur",
            "EUR",
        )
    flows = {f.reference: f for f in repo.history()}
    assert flows["R1"].currency == "GBP"
    assert flows["R2"].currency == "EUR"


def test_cash_flows_history_scopes_and_orders(trades_connect):
    repo = CashFlowsRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_ignore(conn, "2024-01-01", "DIVIDEND", None, 12.5, "Div", "R1", 1)
        repo.insert_ignore(
            conn, "2024-03-01", "CONTRIBUTION", None, 500, "Top-up", "R2", 1
        )
        repo.insert_ignore(conn, "2024-02-01", "DIVIDEND", None, 8.0, "Div B", "R3", 2)
    # Scoped to portfolio 1, newest date first.
    flows = repo.history(portfolio_id=1)
    assert [f.reference for f in flows] == ["R2", "R1"]
    assert flows[0].flow_type == "CONTRIBUTION"
    # Portfolio 2 sees only its own row.
    assert [f.reference for f in repo.history(portfolio_id=2)] == ["R3"]


# --- CashBalancesRepository (Story 1.4) -------------------------------------


def test_cash_balances_upsert_and_get_round_trip(trades_connect):
    """Story 1.4, AC1/AC2: per-(portfolio, currency) balances are Decimal,
    never float — the table exists purely to carry this correctly."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    repo = CashBalancesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.upsert_on_connection(conn, 1, "USD", Decimal("154.86"), "2024-05-29")
    assert repo.get(1, "USD") == (Decimal("154.86"), "2024-05-29")
    assert repo.get(1, "GBP") is None
    assert repo.get(2, "USD") is None


def test_cash_balances_upsert_replaces_existing_row(trades_connect):
    """A later upsert for the same (portfolio, currency) overwrites the
    stored amount/as_of rather than erroring or duplicating (PRIMARY KEY on
    (portfolio_id, currency))."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    repo = CashBalancesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.upsert_on_connection(conn, 1, "USD", Decimal("100.00"), "2024-01-01")
        repo.upsert_on_connection(conn, 1, "USD", Decimal("200.00"), "2024-02-01")
    assert repo.get(1, "USD") == (Decimal("200.00"), "2024-02-01")


def test_cash_balances_get_on_connection_sees_uncommitted_writes(trades_connect):
    """Story 1.2's #160 stale-date guard reads via the same open connection
    as the write, inside the SIPP import's one transaction — so a read must
    see this transaction's own not-yet-committed upsert."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    repo = CashBalancesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.upsert_on_connection(conn, 1, "EUR", Decimal("50.00"), "2024-03-01")
        assert repo.get_on_connection(conn, 1, "EUR") == (
            Decimal("50.00"),
            "2024-03-01",
        )
        assert repo.get_on_connection(conn, 1, "GBP") is None


def test_cash_balances_scoped_per_portfolio_and_currency(trades_connect):
    """A EUR balance and a GBP balance for the same portfolio, and the same
    currency across two portfolios, are independent rows."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    repo = CashBalancesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.upsert_on_connection(conn, 1, "GBP", Decimal("10.00"), "2024-01-01")
        repo.upsert_on_connection(conn, 1, "EUR", Decimal("20.00"), "2024-01-01")
        repo.upsert_on_connection(conn, 2, "GBP", Decimal("30.00"), "2024-01-01")
    assert repo.get(1, "GBP") == (Decimal("10.00"), "2024-01-01")
    assert repo.get(1, "EUR") == (Decimal("20.00"), "2024-01-01")
    assert repo.get(2, "GBP") == (Decimal("30.00"), "2024-01-01")


def test_cash_balances_list_all_enumerates_every_currency_for_a_portfolio(
    trades_connect,
):
    """Story 1.6, Gate 3: enumerate every currency a portfolio holds a
    balance in -- ordered by currency, scoped to one portfolio."""
    from decimal import Decimal

    from app.repositories.cash_balances_repo import CashBalancesRepository

    repo = CashBalancesRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.upsert_on_connection(conn, 1, "USD", Decimal("50.00"), "2024-01-02")
        repo.upsert_on_connection(conn, 1, "GBP", Decimal("10.00"), "2024-01-01")
        repo.upsert_on_connection(conn, 2, "EUR", Decimal("99.00"), "2024-01-01")
    assert repo.list_all(1) == [
        ("GBP", Decimal("10.00"), "2024-01-01"),
        ("USD", Decimal("50.00"), "2024-01-02"),
    ]
    assert repo.list_all(2) == [("EUR", Decimal("99.00"), "2024-01-01")]
    assert repo.list_all(None) == []


# --- CashReconciliationRepository (Story 1.5) -------------------------------


def test_cash_reconciliation_insert_and_list(trades_connect):
    from app.repositories.cash_reconciliation_repo import CashReconciliationRepository

    repo = CashReconciliationRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_issue_on_connection(
            conn, 1, "2024-01-15", 500.0, 550.0, 530.0, -20.0, "REF-1", "GBP"
        )
    issues = repo.list_issues(1)
    assert len(issues) == 1
    issue = issues[0]
    assert issue[2] == "2024-01-15"  # date
    assert issue[3] == 500.0  # prior_balance
    assert issue[4] == 550.0  # expected_balance
    assert issue[5] == 530.0  # actual_balance
    assert issue[6] == -20.0  # difference
    assert issue[7] == "REF-1"  # row_ref
    assert issue[8] == "GBP"  # currency


def test_cash_reconciliation_list_scoped_per_portfolio_newest_first(trades_connect):
    from app.repositories.cash_reconciliation_repo import CashReconciliationRepository

    repo = CashReconciliationRepository(trades_connect)
    with db.session(trades_connect) as conn:
        repo.insert_issue_on_connection(
            conn, 1, "2024-01-01", 100.0, 110.0, 105.0, -5.0, "R1", "GBP"
        )
        repo.insert_issue_on_connection(
            conn, 1, "2024-02-01", 200.0, 210.0, 205.0, -5.0, "R2", "GBP"
        )
        repo.insert_issue_on_connection(
            conn, 2, "2024-01-01", 300.0, 310.0, 305.0, -5.0, "R3", "GBP"
        )
    pf1_issues = repo.list_issues(1)
    assert [i[7] for i in pf1_issues] == ["R2", "R1"]
    assert [i[7] for i in repo.list_issues(2)] == ["R3"]
    assert repo.list_issues(None) == []


# --- PriceCacheRepository --------------------------------------------------


def test_price_cache_upsert_and_load(trades_connect):
    repo = PriceCacheRepository(trades_connect)
    repo.upsert_many({"AAPL": 100.0}, {"AAPL": (130.0, "USD")})
    repo.upsert_many({"AAPL": 110.0})  # update existing
    rows = repo.load_all()
    assert len(rows) == 1
    assert rows[0][0] == "AAPL"
    assert rows[0][1] == 110.0


def test_price_cache_subset_upserts_preserve_other_refresh_results(trades_connect):
    """Overlapping refreshes write only their successful ticker subset.

    This is the persistence invariant used by concurrent price refreshes:
    a later subset upsert may replace its own ticker, but cannot discard a
    valid value that another refresh stored for a different ticker.
    """
    repo = PriceCacheRepository(trades_connect)
    repo.upsert_many({"AAPL": 100.0, "VOD.L": 2.0})
    repo.upsert_many({"AAPL": 101.0})

    values = {row[0]: row[1] for row in repo.load_all()}
    assert values == {"AAPL": 101.0, "VOD.L": 2.0}


# --- FxRateCacheRepository (Story 1.2) --------------------------------------


def test_fx_rate_cache_upsert_and_get_many_round_trip(trades_connect):
    """The real SQL (``IN (...)`` placeholder construction, ``ON CONFLICT``
    upsert) round-trips correctly against a real SQLite DB -- Story 1.2's
    service-layer tests only exercise this repository via an in-memory
    fake, so this is the one test that runs the actual SQL."""
    repo = FxRateCacheRepository(trades_connect)

    repo.upsert_many({"2026-01-01": 1.3456, "2026-01-02": 1.36})
    result = repo.get_many(["2026-01-01", "2026-01-02", "2026-01-03"])
    assert result == {"2026-01-01": 1.3456, "2026-01-02": 1.36}
    assert "2026-01-03" not in result

    # Upsert overwrites an existing row rather than erroring/duplicating.
    repo.upsert_many({"2026-01-01": 1.40})
    assert repo.get_many(["2026-01-01"]) == {"2026-01-01": 1.40}

    # A dumb store: an invalid stored value round-trips unfiltered --
    # PortfolioService, not this repository, is responsible for filtering.
    repo.upsert_many({"2026-01-04": -1.0})
    assert repo.get_many(["2026-01-04"]) == {"2026-01-04": -1.0}

    assert repo.get_many([]) == {}


def test_fx_rate_cache_keeps_pairs_independent_for_same_date(trades_connect):
    repo = FxRateCacheRepository(trades_connect)
    repo.upsert_many({"2026-01-01": 1.25}, pair="GBPUSD=X")
    repo.upsert_many({"2026-01-01": 10.25}, pair="GBPHKD=X")

    assert repo.get_many(["2026-01-01"], pair="GBPUSD=X") == {"2026-01-01": 1.25}
    assert repo.get_many(["2026-01-01"], pair="GBPHKD=X") == {"2026-01-01": 10.25}


def test_fx_rate_cache_migrates_legacy_gbpusd_rows(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fx_rate_cache (date TEXT PRIMARY KEY, gbpusd_rate REAL)")
    conn.execute("INSERT INTO fx_rate_cache VALUES ('2026-01-01', 1.25)")
    conn.commit()

    db.init_trades_db(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fx_rate_cache)")}
    primary_key = [
        (row[1], row[5])
        for row in conn.execute("PRAGMA table_info(fx_rate_cache)")
        if row[5]
    ]
    rows = conn.execute("SELECT pair, date, rate FROM fx_rate_cache").fetchall()
    db.init_trades_db(conn)
    rows_after_second_init = conn.execute(
        "SELECT pair, date, rate FROM fx_rate_cache"
    ).fetchall()
    conn.close()

    assert columns == {"pair", "date", "rate"}
    assert primary_key == [("pair", 1), ("date", 2)]
    assert rows == [("GBPUSD=X", "2026-01-01", 1.25)]
    assert rows_after_second_init == rows


# --- AccountStateRepository ------------------------------------------------


def test_account_state_set_get_exists(trades_connect):
    repo = AccountStateRepository(trades_connect)
    assert repo.exists("cash_balance") is False
    assert repo.get("cash_balance") is None
    repo.set("cash_balance", "5000.0")
    assert repo.exists("cash_balance") is True
    assert repo.get("cash_balance") == "5000.0"
    repo.set("cash_balance", "6000.0")
    assert repo.get("cash_balance") == "6000.0"


# --- ArtifactsRepository ---------------------------------------------------


def test_artifacts_json_roundtrip(tmp_path):
    repo = ArtifactsRepository()
    path = tmp_path / "data.json"
    assert repo.read_json(path, default=[]) == []
    repo.write_json(path, {"a": 1})
    assert repo.read_json(path) == {"a": 1}


def test_artifacts_csv_append_and_read(tmp_path):
    repo = ArtifactsRepository()
    path = tmp_path / "rows.csv"
    fields = ["ticker", "price"]
    repo.append_csv_row(path, fields, {"ticker": "AAPL", "price": "100"})
    repo.append_csv_row(path, fields, {"ticker": "MSFT", "price": "200"})
    rows = repo.read_csv_dicts(path)
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]


# --- AlertsRepository ------------------------------------------------------


@pytest.fixture
def alerts_repo(tmp_path):
    repo = AlertsRepository(db.make_connect(lambda: tmp_path / "alerts.db"))
    repo.ensure_schema()
    return repo


def test_alerts_record_and_watching(alerts_repo):
    assert alerts_repo.has_watching("AAPL") is False
    alerts_repo.record("AAPL", 9, "Stage 2", "summary", 100.0, 90.0)
    assert alerts_repo.has_watching("AAPL") is True
    assert alerts_repo.last_alerted_at("AAPL") is not None
    rows = alerts_repo.watching()
    assert len(rows) == 1
    rowid = rows[0][0]
    alerts_repo.set_status(rowid, "entered")
    assert alerts_repo.has_watching("AAPL") is False


def test_alerts_clear(alerts_repo):
    alerts_repo.record("AAPL", 9, "Stage 2", "summary", 100.0, 90.0)
    alerts_repo.clear()
    assert alerts_repo.last_alerted_at("AAPL") is None


def test_alerts_states_for_tickers_combines_alert_facts(alerts_repo):
    with db.session(alerts_repo._connect) as conn:
        conn.executemany(
            "INSERT INTO alerts (ticker, alerted_at, status) VALUES (?, ?, ?)",
            [
                ("HIST", "2026-01-01T00:00:00+00:00", "entered"),
                ("HIST", "2026-01-02T00:00:00+00:00", "stopped"),
            ],
        )
        conn.commit()
    alerts_repo.record("WATCH", 9, "Stage 2", "summary", 100.0, 90.0)

    states = alerts_repo.states_for_tickers(["NONE", "HIST", "WATCH", "HIST"])

    assert set(states) == {"HIST", "WATCH"}
    assert states["HIST"].has_watching is False
    assert states["HIST"].last_alerted_at == "2026-01-02T00:00:00+00:00"
    assert states["WATCH"].has_watching is True
    assert states["WATCH"].last_alerted_at is not None
    assert alerts_repo.states_for_tickers([]) == {}


def test_alerts_states_for_tickers_uses_one_select_for_many_tickers(tmp_path):
    statements: list[str] = []

    def connect():
        connection = db.connect(tmp_path / "alerts.db")
        connection.set_trace_callback(statements.append)
        return connection

    repo = AlertsRepository(connect)
    repo.ensure_schema()
    repo.record("AAPL", 9, "Stage 2", "summary", 100.0, 90.0)
    statements.clear()

    repo.states_for_tickers(["AAPL", "MSFT", "NVDA", "AAPL"])

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1
    assert selects[0].count('"AAPL"') == 1


# --- ResultsRepository -----------------------------------------------------


def test_results_save_and_latest_scores(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            (
                "AAPL",
                "2024-01-01 09:00",
                7,
                10,
                9,
                "Stage 2",
                100.0,
                101.0,
                95.0,
                "far",
            ),
            (
                "AAPL",
                "2024-01-02 09:00",
                8,
                11,
                9,
                "Stage 2",
                102.0,
                103.0,
                96.0,
                "near",
            ),
        ]
    )
    assert repo.latest_scores() == {"AAPL": 8}


def test_results_latest_scores_multi_ticker(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            # AAPL: two rows — later date has score 9
            (
                "AAPL",
                "2024-01-01 09:00",
                7,
                10,
                9,
                "Stage 2",
                100.0,
                101.0,
                95.0,
                "far",
            ),
            (
                "AAPL",
                "2024-01-03 09:00",
                9,
                12,
                9,
                "Stage 2",
                104.0,
                105.0,
                97.0,
                "near",
            ),
            # MSFT: two rows — later date has score 6
            (
                "MSFT",
                "2024-01-02 09:00",
                5,
                8,
                7,
                "Stage 1",
                200.0,
                201.0,
                190.0,
                "far",
            ),
            (
                "MSFT",
                "2024-01-04 09:00",
                6,
                9,
                7,
                "Stage 1",
                202.0,
                203.0,
                191.0,
                "near",
            ),
        ]
    )
    # Each ticker must resolve to its own latest row, not a global MAX.
    assert repo.latest_scores() == {"AAPL": 9, "MSFT": 6}


def test_results_latest_scores_empty(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    assert repo.latest_scores() == {}


def test_results_latest_scores_single_row_per_ticker(tmp_path):
    repo = ResultsRepository(db.make_connect(lambda: tmp_path / "results.db"))
    repo.ensure_schema()
    repo.save_results(
        [
            (
                "AAPL",
                "2024-01-01 09:00",
                7,
                10,
                9,
                "Stage 2",
                100.0,
                101.0,
                95.0,
                "far",
            ),
            (
                "MSFT",
                "2024-01-01 09:00",
                5,
                8,
                7,
                "Stage 1",
                200.0,
                201.0,
                190.0,
                "far",
            ),
        ]
    )
    # Both tickers must appear; a LIMIT 1 or global MAX would drop one.
    assert repo.latest_scores() == {"AAPL": 7, "MSFT": 5}
