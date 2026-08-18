"""Repository for the ``trades`` table in ``trades.db``."""

import logging
from collections.abc import Iterable
from typing import Any

from app.core.ticker_identity import (
    canonicalize_or_fallback,
    load_aliases,
    matching_raw_tickers,
)
from app.repositories.db import Connect, session
from app.schemas import Trade
from app.schemas.trade import SippImportRowOutcome

logger = logging.getLogger(__name__)

# Dates are stored ISO (YYYY-MM-DD), which sorts chronologically as text.
_DATE_SORT = "date"
_REPLAY_COLUMNS = "ticker, action, shares, price, date, stop_loss, entry_price"

# Story 2.2: deterministic same-day replay order, applied identically to
# average-cost (here, in SQL) and FIFO (``RealisedPnlService._sorted_valid_
# trades``, in Python). Within one imported file the first-listed CSV row is
# the most recent execution of that day, so chronological (oldest-first)
# replay must process the *highest* ``source_row_index`` first within a date
# group -- hence ``DESC``. ``source_row_index IS NULL`` (rows imported before
# this story shipped) is coalesced to ``-1``, the lowest possible position,
# so such a row always replays last among same-day peers. The next tiebreak
# is the content-derived ``idempotency_key`` -- never an import-timing
# signal. A trailing ``id`` guarantees a fully deterministic order even when
# both of the above are NULL (e.g. two same-day trades recorded manually via
# ``insert()`` rather than SIPP import, which sets neither column) -- this
# mirrors ``RealisedPnlService._replay_sort_key``'s identical fallback and
# restores the same guarantee the pre-Story-2.2 ``ORDER BY date, id`` gave
# every row, not only imported ones.
_REPLAY_ORDER = (
    f"{_DATE_SORT}, COALESCE(source_row_index, -1) DESC, idempotency_key, id"
)


def _in_placeholders(values: Iterable[Any]) -> str:
    """Return a ``"?, ?, ..."`` placeholder string, one per value.

    Shared by ``delete_by_ticker``/``history`` so the ``WHERE ticker IN
    (...)`` placeholder string is built once instead of inline in each.
    """
    return ", ".join("?" for _ in values)


def _row_to_trade(row: tuple[Any, ...]) -> Trade:
    return Trade(
        id=row[0],
        ticker=row[1],
        action=row[2],
        shares=row[3],
        price=row[4],
        date=row[5],
        notes=row[6],
        stop_loss=row[7] if len(row) > 7 else None,
        entry_price=row[8] if len(row) > 8 else None,
        portfolio_id=row[9] if len(row) > 9 else None,
        realised_pnl_ack_at=row[10] if len(row) > 10 else None,
        currency=row[11] if len(row) > 11 and row[11] is not None else "GBP",
        source_row_index=row[12] if len(row) > 12 else None,
        idempotency_key=row[13] if len(row) > 13 else None,
        source=row[14] if len(row) > 14 else None,
        import_batch_id=row[15] if len(row) > 15 else None,
    )


class TradesRepository:
    """Typed access to the ``trades`` table."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def insert(
        self,
        ticker: str,
        action: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        stop_loss: float | None = None,
        entry_price: float | None = None,
        portfolio_id: int | None = None,
        currency: str = "GBP",
        source: str | None = None,
    ) -> int:
        """Insert a trade and return its new id.

        ``source`` records which write path produced this row (Story 2.4)
        -- e.g. ``"manual"``, ``"quick_add"``, ``"correction"``,
        ``"opening_lot"``. ``None`` for callers that don't tag it.
        """
        with session(self._connect) as conn:
            cur = conn.execute(
                "INSERT INTO trades (ticker, action, shares, price, date, notes,"
                " stop_loss, entry_price, portfolio_id, currency, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker,
                    action,
                    shares,
                    price,
                    date,
                    notes,
                    stop_loss,
                    entry_price,
                    portfolio_id,
                    currency,
                    source,
                ),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def insert_ignore(
        self,
        conn: Any,
        ticker: str,
        action: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        reference: str | None = None,
        portfolio_id: int | None = None,
        idempotency_key: str | None = None,
        currency: str = "GBP",
        source_row_index: int | None = None,
        source: str | None = None,
        import_batch_id: str | None = None,
    ) -> SippImportRowOutcome:
        """Insert a trade with ``INSERT OR IGNORE`` on the given connection.

        Used by the SIPP import, which batches many rows in one transaction.
        Dedupe is keyed on ``(portfolio_id, idempotency_key)`` so the same
        CSV can import into different portfolios independently.

        ``source_row_index`` is the row's 0-based position within its own
        source CSV file (Story 2.2) — used, together with
        ``idempotency_key``, to deterministically order same-day trades for
        FIFO/average-cost replay. ``None`` for rows imported before this
        story shipped.

        ``source``/``import_batch_id`` (Story 2.4) record provenance --
        which write path produced the row, and (for a SIPP import) which
        import call. ``None`` for rows written before this story shipped.

        Returns the row's actual outcome: ``"inserted"`` when the row was
        written, ``"duplicate"`` when the unique index silently suppressed
        it. Never returns ``"skipped"``/``"failed"`` — those are decided at
        plan-build time and such a row never reaches this call.
        """
        cur = conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(ticker, action, shares, price, date, notes, reference, portfolio_id,"
            " idempotency_key, currency, source_row_index, source, import_batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker,
                action,
                shares,
                price,
                date,
                notes,
                reference,
                portfolio_id,
                idempotency_key,
                currency,
                source_row_index,
                source,
                import_batch_id,
            ),
        )
        return "inserted" if cur.rowcount else "duplicate"

    def idempotency_keys_for_portfolio(self, portfolio_id: int | None) -> set[str]:
        """Return every trade idempotency key already stored for a portfolio.

        Read-only: lets the SIPP import classify a row as inserted or
        duplicate before deciding whether to write anything at all.
        """
        bucket = -1 if portfolio_id is None else portfolio_id
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT idempotency_key FROM trades "
                "WHERE ifnull(portfolio_id, -1) = ? AND idempotency_key IS NOT NULL",
                (bucket,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def set_ack(self, conn: Any, trade_id: int, acknowledged_at: str | None) -> None:
        """Set or clear a trade's realised-P&L acknowledgment timestamp.

        ``acknowledged_at`` is an ISO 8601 timestamp string (acknowledged) or
        ``None`` (unacknowledged). Takes the caller's open connection and does
        not commit — mirrors ``insert_ignore``'s batch-friendly shape so the
        caller (``TraderAgent``) controls the transaction boundary.
        """
        conn.execute(
            "UPDATE trades SET realised_pnl_ack_at = ? WHERE id = ?",
            (acknowledged_at, trade_id),
        )

    def update_opening_lot(
        self,
        trade_id: int,
        ticker: str,
        shares: float,
        price: float,
        date: str,
        notes: str = "",
        portfolio_id: int | None = None,
    ) -> bool:
        """Update an existing Opening Lot row's fields in place (Story 2.4).

        Scoped to ``source = 'opening_lot'`` (and ``portfolio_id``, when
        given) so this can never silently rewrite an unrelated trade even
        if called with a stale/wrong id. Returns True if a row was updated.
        """
        with session(self._connect) as conn:
            if portfolio_id is None:
                cur = conn.execute(
                    "UPDATE trades SET ticker = ?, shares = ?, price = ?,"
                    " date = ?, notes = ? WHERE id = ? AND source = 'opening_lot'",
                    (ticker, shares, price, date, notes, trade_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE trades SET ticker = ?, shares = ?, price = ?,"
                    " date = ?, notes = ? WHERE id = ? AND source = 'opening_lot'"
                    " AND portfolio_id = ?",
                    (ticker, shares, price, date, notes, trade_id, portfolio_id),
                )
            return cur.rowcount > 0

    def delete_by_id(self, trade_id: int) -> bool:
        """Delete a trade by id. Returns True if a row was deleted."""
        with session(self._connect) as conn:
            cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            return cur.rowcount > 0

    def delete_by_ticker(self, ticker: str, portfolio_id: int | None = None) -> None:
        """Delete every trade whose canonicalized ticker equals ``ticker``.

        ``ticker`` is treated as a canonical identity (the reverse of
        ``canonical_ticker``): every raw spelling that resolves to it via
        ``matching_raw_tickers`` is deleted, not just rows stored under
        ``ticker``'s exact spelling. This is what makes ``correct_trade()``
        (a live UI action) actually replace a position's trades instead of
        silently no-op-deleting when the stored rows use a different (but
        alias-equivalent) raw spelling than the canonical ticker the UI
        passed back.
        """
        raw_tickers = list(matching_raw_tickers(ticker, load_aliases()))
        placeholders = _in_placeholders(raw_tickers)
        with session(self._connect) as conn:
            if portfolio_id is None:
                conn.execute(
                    f"DELETE FROM trades WHERE ticker IN ({placeholders})",
                    tuple(raw_tickers),
                )
            else:
                conn.execute(
                    f"DELETE FROM trades WHERE ticker IN ({placeholders})"
                    " AND portfolio_id = ?",
                    (*raw_tickers, portfolio_id),
                )

    def history(
        self, ticker: str | None = None, portfolio_id: int | None = None
    ) -> list[Trade]:
        """Return trades newest-first, optionally filtered by ticker/portfolio.

        A ``ticker`` filter is treated as a canonical identity: every raw
        spelling that resolves to it (via ``matching_raw_tickers``) is
        matched, not just rows stored under ``ticker``'s exact spelling.
        Every returned ``Trade.ticker`` is canonicalized (via
        ``canonicalize_or_fallback``), regardless of whether a filter was
        given -- one consistent display form per underlying security.
        """
        aliases = load_aliases()
        base = (
            "SELECT id, ticker, action, shares, price, date, notes, stop_loss,"
            " entry_price, portfolio_id, realised_pnl_ack_at, currency,"
            " source_row_index, idempotency_key, source, import_batch_id"
            " FROM trades"
        )
        order = f"{_DATE_SORT} DESC, id DESC"
        clauses: list[str] = []
        params: list[Any] = []
        if ticker:
            raw_tickers = list(matching_raw_tickers(ticker, aliases))
            clauses.append(f"ticker IN ({_in_placeholders(raw_tickers)})")
            params.extend(raw_tickers)
        if portfolio_id is not None:
            clauses.append("portfolio_id = ?")
            params.append(portfolio_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"{base}{where} ORDER BY {order}"
        with session(self._connect) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        trades = [_row_to_trade(r) for r in rows]
        for trade in trades:
            trade.ticker = canonicalize_or_fallback(
                trade.ticker, aliases, logger=logger, context="history"
            )
        return trades

    def open_rows(self, portfolio_id: int | None = None) -> list[tuple[Any, ...]]:
        """Return valid-ticker trade rows in chronological order for replay.

        Columns: (ticker, action, shares, price, date, stop_loss, entry_price).
        Excludes blank/``n/a`` tickers, matching the legacy portfolio query.
        Scoped to ``portfolio_id`` when given. Ordered by ``_REPLAY_ORDER``
        (Story 2.2): date ascending, then same-day rows deterministically by
        descending ``source_row_index`` (NULL-safe), then ``idempotency_key``
        as the cross-file tiebreak.
        """
        sql = (
            f"SELECT {_REPLAY_COLUMNS} FROM trades"
            " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
        )
        params: tuple[Any, ...] = ()
        if portfolio_id is not None:
            sql += " AND portfolio_id = ?"
            params = (portfolio_id,)
        sql += f" ORDER BY {_REPLAY_ORDER}"
        with session(self._connect) as conn:
            return conn.execute(sql, params).fetchall()

    def open_rows_on_connection(
        self, conn: Any, portfolio_id: int | None = None
    ) -> list[tuple[Any, ...]]:
        """Return valid-ticker trade rows for replay on the caller's connection.

        Identical query (including ``_REPLAY_ORDER``, Story 2.2) to
        ``open_rows`` but executed against the caller's open connection
        instead of a fresh one — used by the SIPP import so its
        in-transaction snapshot calculation sees this transaction's own
        not-yet-committed trade inserts (a separate connection would only
        see the database's last *committed* state and silently miss them).
        """
        sql = (
            f"SELECT {_REPLAY_COLUMNS} FROM trades"
            " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
        )
        params: tuple[Any, ...] = ()
        if portfolio_id is not None:
            sql += " AND portfolio_id = ?"
            params = (portfolio_id,)
        sql += f" ORDER BY {_REPLAY_ORDER}"
        return conn.execute(sql, params).fetchall()

    def held_tickers(self) -> set[str]:
        """Return the set of tickers with a net-positive position in any
        portfolio (used for the watchlist "held" flag, which spans accounts).

        Accumulates net position per canonical ticker (via
        ``canonicalize_or_fallback``, HSFWA protected) -- the same
        "canonicalize before it becomes a dict key" shape ``_replay_trades``
        uses -- so the returned set agrees with ``get_portfolio()``'s
        canonicalized identity even when trades for one security are stored
        under more than one raw spelling.
        """
        from collections import defaultdict

        aliases = load_aliases()
        by_pf: dict[Any, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        with session(self._connect) as conn:
            rows = conn.execute(
                "SELECT portfolio_id, ticker, action, shares FROM trades"
                " WHERE ticker NOT IN ('', 'n/a', 'N/A')"
                " ORDER BY date, id"
            ).fetchall()
        for pid, ticker, action, shares in rows:
            canonical = canonicalize_or_fallback(
                ticker, aliases, logger=logger, context="held_tickers"
            )
            delta = shares if action == "BUY" else -shares
            by_pf[pid][canonical] += delta
        held: set[str] = set()
        for positions in by_pf.values():
            for ticker, net in positions.items():
                if net > 0:
                    held.add(ticker)
        return held
